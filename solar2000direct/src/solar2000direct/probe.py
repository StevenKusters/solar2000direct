"""Probe a Huawei installation and report what it exposes and how fast it answers.

This is the tool you run first, before any of the collector machinery exists. It
answers the three questions that decide the whole design for a given site:

1. Where is the Modbus endpoint, and which unit ID is the inverter on?
2. What is actually installed — strings, optimizers, one battery unit or two, meter,
   backup box — as reported by the hardware rather than from a spec sheet?
3. How slowly does it answer? Every "real-time" claim downstream is bounded by the
   measured round-trip here, so we measure it instead of assuming it.

The inverter accepts one Modbus client at a time, so everything below runs strictly
sequentially and closes each connection before opening the next. If Home Assistant
or the FusionSolar dongle is mid-poll, expect this to be interrupted; that is a real
finding, not a bug, and it is reported as such.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import statistics
import sys
import time
from dataclasses import dataclass, field
from typing import Any

from huawei_solar import (
    SUN2000Device,
    create_device_instance,
    create_tcp_client,
    get_device_identifiers,
    get_device_infos,
)
from huawei_solar.device import detect_device_type
from huawei_solar.exceptions import HuaweiSolarException

# Not re-exported at package level in 3.0.7, but it is the right client for scanning:
# it disables retries, so a silent unit ID costs one timeout instead of a backoff chain.
from huawei_solar.modbus_client import create_scan_tcp_client

from solar2000direct.capabilities import capabilities_of, detect_backup
from solar2000direct.registers import (
    ALL_GROUPS,
    CAP_BACKUP,
    RegisterGroup,
    unknown_register_names,
)

_LOGGER = logging.getLogger("s2d.probe")

# 502 is the documented Modbus TCP port. 503 shows up in Huawei's own examples on
# some SDongle firmware, and 6607 is used by a few dongle generations, so we look
# at all three rather than making the user guess.
CANDIDATE_PORTS = (502, 503, 6607)

# 0 = inverter over its own WiFi AP, 1 = inverter behind a dongle, 100 = the SDongle
# itself. The rest are cheap to try and occasionally right on multi-inverter sites.
CANDIDATE_UNIT_IDS = (0, 1, 100, 2, 3, 11, 16)

SEPARATOR = "-" * 78


def _hdr(title: str) -> None:
    print(f"\n{SEPARATOR}\n  {title}\n{SEPARATOR}")


@dataclass
class Timing:
    """Round-trip statistics for one kind of read."""

    label: str
    register_count: int
    samples_ms: list[float] = field(default_factory=list)
    failures: int = 0
    error: str | None = None

    def summary(self) -> dict[str, Any]:
        if not self.samples_ms:
            return {
                "label": self.label,
                "registers": self.register_count,
                "ok": 0,
                "failures": self.failures,
                "error": self.error,
            }
        ordered = sorted(self.samples_ms)
        return {
            "label": self.label,
            "registers": self.register_count,
            "ok": len(ordered),
            "failures": self.failures,
            "min_ms": round(ordered[0], 1),
            "median_ms": round(statistics.median(ordered), 1),
            "p95_ms": round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))], 1),
            "max_ms": round(ordered[-1], 1),
            "error": self.error,
        }


async def port_is_open(host: str, port: int, timeout: float = 3.0) -> bool:
    """Plain TCP reachability check, before we spend Modbus timeouts on a dead port."""
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    except (TimeoutError, OSError):
        return False
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()
    return True


async def scan_unit_ids(host: str, port: int, unit_ids: tuple[int, ...]) -> list[dict[str, Any]]:
    """Find which unit IDs answer, and what kind of device sits behind each.

    Uses the library's scan client, which disables retries so a silent unit ID costs
    one timeout instead of an exponential backoff chain.
    """
    found: list[dict[str, Any]] = []
    for unit_id in unit_ids:
        client = create_scan_tcp_client(host, port, unit_id=unit_id, timeout=3)
        try:
            await client.connect()
            device_class, model_name = await detect_device_type(client)
            found.append(
                {"unit_id": unit_id, "device_class": device_class.__name__, "model_name": model_name},
            )
            print(f"  unit {unit_id:>3}: {device_class.__name__:<18} {model_name}")
        except (TimeoutError, HuaweiSolarException, OSError) as err:
            print(f"  unit {unit_id:>3}: no response ({type(err).__name__})")
        except Exception as err:  # noqa: BLE001 - a scan must never abort on one unit
            print(f"  unit {unit_id:>3}: unexpected {type(err).__name__}: {err}")
        finally:
            with contextlib.suppress(Exception):
                await client.disconnect()
            # Give the device a moment to release the single connection slot.
            await asyncio.sleep(0.5)
    return found


async def time_group(device: SUN2000Device, group: RegisterGroup, rounds: int) -> Timing:
    """Read one register group repeatedly and record how long each read took."""
    registers = list(group.known_registers())
    timing = Timing(label=group.name, register_count=len(registers))
    if not registers:
        timing.error = "no known registers in this group"
        return timing

    for _ in range(rounds):
        started = time.perf_counter()
        try:
            await device.batch_update(registers)
        except Exception as err:  # noqa: BLE001 - a slow/failing group is a result, not a crash
            timing.failures += 1
            if timing.error is None:
                timing.error = f"{type(err).__name__}: {err}"
        else:
            timing.samples_ms.append((time.perf_counter() - started) * 1000)
    return timing


async def probe_backup_box(device: SUN2000Device) -> dict[str, Any]:
    """Backup box presence is not a library-detected capability, so read for it directly.

    A successful read is *not* proof of presence: inverters without a backup box still
    answer these addresses, they just answer with zeros. So we distinguish three states
    and refuse to guess between the last two, because silently enabling a backup group
    on every installation would ship broken entities to everyone who lacks the hardware.
    """
    # Read one register at a time, which is the whole point: batched, the first address a
    # given firmware does not implement takes the rest down with it, and a fitted Backup
    # Box reads as absent. The reference site's own probe report records exactly that --
    # an IllegalDataAddress on backup_power_state_of_charge, on a system that has one.
    # detect_backup is what the collector uses, so the probe now agrees with the add-on.
    present, values = await detect_backup(device)
    result: dict[str, Any] = {"values": values}
    if present:
        result["state"] = "present"
    elif values:
        result["state"] = "inconclusive"
    else:
        result["state"] = "absent"
        result["error"] = "none of the backup registers could be read"
    return result


async def probe_optimizers(device: SUN2000Device) -> dict[str, Any]:
    """Per-panel data travels over the Modbus file extension, not registers.

    It is by far the most expensive read on the bus, so we time it explicitly: the
    result decides whether per-panel data belongs on a 5-minute cadence or a 15-minute one.
    """
    out: dict[str, Any] = {}

    started = time.perf_counter()
    try:
        system_info = await device.get_optimizer_system_information_data()
    except Exception as err:  # noqa: BLE001
        out["system_information"] = {"error": f"{type(err).__name__}: {err}"}
    else:
        out["system_information"] = {
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
            "count": len(system_info),
            "optimizers": {
                str(addr): {
                    "sn": getattr(info, "sn", None),
                    "model": getattr(info, "model", None),
                    "software_version": getattr(info, "software_version", None),
                }
                for addr, info in sorted(system_info.items())
            },
        }

    started = time.perf_counter()
    try:
        realtime = await device.get_latest_optimizer_history_data()
    except Exception as err:  # noqa: BLE001
        out["realtime"] = {"error": f"{type(err).__name__}: {err}"}
    else:
        out["realtime"] = {
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
            "count": len(realtime),
            "sample": {
                str(addr): {
                    "output_power": getattr(data, "output_power", None),
                    "voltage": getattr(data, "output_voltage", None),
                    "temperature": getattr(data, "temperature", None),
                }
                for addr, data in sorted(realtime.items())[:4]
            },
        }
    return out


def recommend_cadence(timings: list[Timing]) -> dict[str, Any]:
    """Turn measured round-trips into an honest statement of achievable poll rates."""
    by_label = {t.label: t for t in timings if t.samples_ms}
    fast_labels = [g.name for g in ALL_GROUPS if g.interval <= 2.0]
    fast_cost_ms = sum(
        statistics.median(by_label[label].samples_ms) for label in fast_labels if label in by_label
    )
    # The library also waits `wait_between_requests` between reads; assume the default.
    overhead_ms = 50 * len([label for label in fast_labels if label in by_label])
    cycle_ms = fast_cost_ms + overhead_ms
    return {
        "measured_fast_cycle_ms": round(cycle_ms, 1),
        "sustainable_fast_interval_s": round(max(1.0, (cycle_ms * 1.5) / 1000), 1),
        "note": (
            "Sustainable interval includes 50% headroom so slow-register groups and "
            "optimizer file reads can be interleaved without starving the fast loop."
        ),
    }


async def run_probe(args: argparse.Namespace) -> dict[str, Any]:
    report: dict[str, Any] = {"host": args.host, "library": "huawei-solar"}

    if bad := unknown_register_names():
        _hdr("REGISTER CATALOG MISMATCH")
        for group_name, regs in bad.items():
            print(f"  {group_name}: {', '.join(regs)}")
        report["unknown_registers"] = {k: list(v) for k, v in bad.items()}

    # --- 1. Find the endpoint ----------------------------------------------------
    _hdr("1. PORT REACHABILITY")
    ports = (args.port,) if args.port else CANDIDATE_PORTS
    open_ports = []
    for port in ports:
        is_open = await port_is_open(args.host, port)
        print(f"  {args.host}:{port:<5} {'open' if is_open else 'closed / filtered'}")
        if is_open:
            open_ports.append(port)
    report["open_ports"] = open_ports

    if not open_ports:
        print(
            "\n  No Modbus port answered. Either the host is wrong, or Modbus TCP is not\n"
            "  enabled. In the installer app: Communication configuration ->\n"
            "  Modbus TCP -> Connection = 'Unrestricted'."
        )
        return report

    port = open_ports[0]

    # --- 2. Find the inverter ----------------------------------------------------
    _hdr(f"2. UNIT ID SCAN on port {port}")
    unit_ids = (args.unit_id,) if args.unit_id is not None else CANDIDATE_UNIT_IDS
    devices = await scan_unit_ids(args.host, port, unit_ids)
    report["scan"] = devices

    inverters = [d for d in devices if d["device_class"] == "SUN2000Device"]
    if not inverters:
        print("\n  No SUN2000 inverter found on any scanned unit ID.")
        return report

    primary = inverters[0]
    report["primary"] = primary
    print(f"\n  Using unit {primary['unit_id']} ({primary['model_name']}) as the primary device.")

    # --- 3. Capabilities ---------------------------------------------------------
    client = create_tcp_client(
        args.host,
        port,
        unit_id=primary["unit_id"],
        timeout=args.timeout,
        wait_between_requests=args.cooldown,
    )
    await client.connect()
    try:
        device = await create_device_instance(client)
        if not isinstance(device, SUN2000Device):
            print(f"  Expected a SUN2000Device, got {type(device).__name__}")
            return report

        caps = capabilities_of(device)
        _hdr("3. DETECTED INSTALLATION")
        print(f"  model                {device.model_name}")
        print(f"  serial               {device.serial_number}")
        print(f"  part number          {device.product_number}")
        print(f"  firmware             {device.firmware_version}")
        print(f"  software             {device.software_version}")
        print(f"  PV strings           {device.pv_string_count}")
        print(f"  optimizers           {device.has_optimizers}")
        print(f"  battery unit 1       {device.battery_1_type.name}")
        print(f"  battery unit 2       {device.battery_2_type.name}")
        print(f"  capacity control     {device.supports_capacity_control}")
        print(f"  power meter online   {device.power_meter_online}")
        print(f"  power meter type     {device.power_meter_type}")
        print(f"  capabilities         {sorted(caps) or '(none)'}")

        report["device"] = {
            "model_name": device.model_name,
            "serial_number": device.serial_number,
            "product_number": device.product_number,
            "firmware_version": device.firmware_version,
            "software_version": device.software_version,
            "pv_string_count": device.pv_string_count,
            "has_optimizers": device.has_optimizers,
            "battery_1_type": device.battery_1_type.name,
            "battery_2_type": device.battery_2_type.name,
            "supports_capacity_control": device.supports_capacity_control,
            "power_meter_online": device.power_meter_online,
            "power_meter_type": str(device.power_meter_type),
            "capabilities": sorted(caps),
        }

        # --- 4. Everything on the bus --------------------------------------------
        _hdr("4. DEVICE INVENTORY")
        try:
            identifiers = await get_device_identifiers(client)
            print(f"  vendor {identifiers.vendor} / {identifiers.product_code} rev {identifiers.main_revision_version}")
            report["identifiers"] = {
                "vendor": identifiers.vendor,
                "product_code": identifiers.product_code,
                "revision": identifiers.main_revision_version,
            }
        except Exception as err:  # noqa: BLE001
            print(f"  device identifiers unavailable: {type(err).__name__}: {err}")

        try:
            infos = await get_device_infos(client)
            for info in infos:
                print(f"  - {info.model or '?':<24} sw={info.software_version or '?':<16} esn={info.esn or '?'}")
            report["device_infos"] = [
                {
                    "model": i.model,
                    "software_version": i.software_version,
                    "esn": i.esn,
                    "device_id": i.device_id,
                    "product_type": i.product_type,
                }
                for i in infos
            ]
        except Exception as err:  # noqa: BLE001
            print(f"  device infos unavailable: {type(err).__name__}: {err}")

        # --- 5. Backup box --------------------------------------------------------
        _hdr("5. BACKUP BOX")
        backup = await probe_backup_box(device)
        if backup["state"] == "present":
            print(f"  present - {backup['values']}")
            caps = caps | {CAP_BACKUP}
        else:
            if backup["state"] == "absent":
                print(f"  absent - {backup.get('error')}")
            else:
                print(f"  inconclusive - registers readable but all zero: {backup['values']}")
                print("    An inverter with no backup box answers these the same way, and so does")
                print("    one whose owner set the backup reserve to 0%.")
            # The override applies to both outcomes now. It used to be offered only for the
            # inconclusive one, which is not the case a fitted box actually lands in when
            # its firmware refuses the address.
            print("    If you know the box is fitted, re-run with --assume-backup.")
            if args.assume_backup:
                caps = caps | {CAP_BACKUP}
                print("    --assume-backup given: enabling the backup group.")
        report["backup"] = backup

        # --- 6. Timing ------------------------------------------------------------
        _hdr(f"6. ROUND-TRIP TIMING ({args.rounds} rounds per group)")
        print(f"  {'group':<24} {'regs':>5} {'ok':>4} {'fail':>5} {'median':>9} {'p95':>9}")
        timings: list[Timing] = []
        for group in ALL_GROUPS:
            if not group.applicable(caps):
                continue
            timing = await time_group(device, group, args.rounds)
            timings.append(timing)
            s = timing.summary()
            median = f"{s['median_ms']} ms" if "median_ms" in s else "-"
            p95 = f"{s['p95_ms']} ms" if "p95_ms" in s else "-"
            print(
                f"  {timing.label:<24} {s['registers']:>5} {s.get('ok', 0):>4} "
                f"{s['failures']:>5} {median:>9} {p95:>9}"
            )
            if timing.error:
                print(f"      ! {timing.error}")
        report["timings"] = [t.summary() for t in timings]

        cadence = recommend_cadence(timings)
        print(f"\n  measured fast-loop cycle     {cadence['measured_fast_cycle_ms']} ms")
        print(f"  sustainable fast interval    {cadence['sustainable_fast_interval_s']} s")
        report["cadence"] = cadence

        # --- 7. Optimizers --------------------------------------------------------
        if device.has_optimizers and not args.skip_optimizers:
            _hdr("7. PER-PANEL OPTIMIZER DATA")
            opt = await probe_optimizers(device)
            for key, section in opt.items():
                if "error" in section:
                    print(f"  {key}: FAILED - {section['error']}")
                else:
                    print(f"  {key}: {section['count']} optimizers in {section['elapsed_ms']} ms")
            report["optimizers"] = opt
        elif device.has_optimizers:
            print("\n  Optimizer read skipped (--skip-optimizers).")

    finally:
        with contextlib.suppress(Exception):
            await client.disconnect()

    return report


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="s2d-probe",
        description="Probe a Huawei SUN2000 installation over Modbus TCP.",
    )
    parser.add_argument("host", help="IP address of the inverter or SDongle")
    parser.add_argument("--port", type=int, default=None, help="Modbus TCP port (default: try 502, 503, 6607)")
    parser.add_argument("--unit-id", type=int, default=None, help="Modbus unit ID (default: scan)")
    parser.add_argument("--timeout", type=int, default=10, help="Modbus response timeout in seconds")
    parser.add_argument(
        "--cooldown",
        type=float,
        default=0.05,
        help="Seconds to wait between requests. Raise if the device drops the connection.",
    )
    parser.add_argument("--rounds", type=int, default=5, help="Timing samples per register group")
    parser.add_argument("--skip-optimizers", action="store_true", help="Skip the slow per-panel file read")
    parser.add_argument(
        "--assume-backup",
        action="store_true",
        help="Poll backup-box registers even if they read as all-zero (use when you know it is fitted)",
    )
    parser.add_argument("--json", dest="json_path", default=None, help="Write the full report to this JSON file")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable library debug logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    try:
        report = asyncio.run(run_probe(args))
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130

    if args.json_path:
        with open(args.json_path, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, default=str)
        print(f"\nFull report written to {args.json_path}")

    return 0 if report.get("device") else 1


if __name__ == "__main__":
    sys.exit(main())
