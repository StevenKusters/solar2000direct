"""Measure whether wide single-read blocks beat the library's default batching.

The probe established that on a real SDongle a Modbus round-trip costs ~500-600 ms
almost regardless of how many registers it carries. If that holds, the collector should
read a handful of wide address spans rather than many small semantic groups.

"Should" is not "does". Inverters return IllegalDataAddress for spans covering registers
their firmware does not implement, and a wide read that fails takes every register in it
down at once. So this tool does three things against real hardware:

* reads each planned block and reports whether the device tolerates the full span,
  narrowing the block and retrying when it does not;
* times the planned blocks against ``batch_update`` over the identical register set;
* compares the decoded values from both paths, because a read that is fast and wrong is
  worse than one that is slow and right.

It also probes the backup-box registers one at a time. Reading them as a batch lets a
single unimplemented address mask the rest, which is exactly what happened on the first
probe run.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import statistics
import sys
import time
from typing import Any

from huawei_solar import SUN2000Device, create_device_instance, create_tcp_client
from huawei_solar.registers import REGISTERS

from solar2000direct.blocks import block_span as _span
from solar2000direct.blocks import validate_plan
from solar2000direct.capabilities import capabilities_of
from solar2000direct.registers import (
    build_read_plan,
    live_register_names,
    pack_register_names,
)

SEPARATOR = "-" * 78


def _hdr(title: str) -> None:
    print(f"\n{SEPARATOR}\n  {title}\n{SEPARATOR}")


async def probe_backup_registers(device: SUN2000Device) -> dict[str, Any]:
    """Read each backup register separately so one bad address cannot mask the others."""
    candidates = [
        "backup_power_state_of_charge",
        "storage_backup_power_state_of_charge",
        "backup_time_notification_threshold",
        "backup_switch_to_off_grid",
        "backup_voltage_independent_operation",
    ]
    results: dict[str, Any] = {}
    for name in candidates:
        if name not in REGISTERS:
            results[name] = {"status": "unknown_register"}
            continue
        try:
            result = await device.client.get(name)
        except Exception as err:  # noqa: BLE001 - an unreadable address is the answer, not a crash
            results[name] = {"status": "unreadable", "error": f"{type(err).__name__}: {err}"}
            print(f"  {name:<42} unreadable  ({type(err).__name__})")
        else:
            results[name] = {"status": "ok", "value": result.value, "unit": result.unit}
            print(f"  {name:<42} = {result.value!r} {result.unit or ''}")
    return results


async def time_plan(device: SUN2000Device, plan: list[list[str]], rounds: int) -> tuple[list[float], dict[str, Any]]:
    """Time a full pass over every block in a plan."""
    samples: list[float] = []
    last: dict[str, Any] = {}
    for _ in range(rounds):
        started = time.perf_counter()
        collected: dict[str, Any] = {}
        for block in plan:
            with contextlib.suppress(Exception):
                collected.update(await device.client.get_multiple_as_dict(block))
        samples.append((time.perf_counter() - started) * 1000)
        last = collected
    return samples, last


async def time_batch_update(device: SUN2000Device, names: list[str], rounds: int) -> tuple[list[float], dict[str, Any]]:
    """Time the library's default batching over the identical register set."""
    samples: list[float] = []
    last: dict[str, Any] = {}
    for _ in range(rounds):
        started = time.perf_counter()
        try:
            last = await device.batch_update(list(names))
        except Exception:  # noqa: BLE001
            last = {}
        samples.append((time.perf_counter() - started) * 1000)
    return samples, last


def _stats(samples: list[float]) -> dict[str, float]:
    ordered = sorted(samples)
    return {
        "min_ms": round(ordered[0], 1),
        "median_ms": round(statistics.median(ordered), 1),
        "max_ms": round(ordered[-1], 1),
    }


def compare_values(baseline: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    """Check the two read paths decode to the same thing.

    Live electrical values legitimately move between two reads seconds apart, so a
    difference is reported rather than treated as a failure. What would be a real defect
    is a register present in one path and missing from the other, or a decode that
    produces a different type.
    """
    missing = sorted(set(baseline) - set(candidate))
    extra = sorted(set(candidate) - set(baseline))
    differing: dict[str, Any] = {}
    for name in sorted(set(baseline) & set(candidate)):
        a, b = baseline[name].value, candidate[name].value
        if type(a) is not type(b):
            differing[name] = {"batch_update": repr(a), "block_read": repr(b), "kind": "type mismatch"}
        elif a != b:
            differing[name] = {"batch_update": a, "block_read": b, "kind": "value moved"}
    return {"missing_from_blocks": missing, "extra_in_blocks": extra, "differing": differing}


async def run_bench(args: argparse.Namespace) -> dict[str, Any]:
    report: dict[str, Any] = {"host": args.host}
    client = create_tcp_client(
        args.host,
        args.port,
        unit_id=args.unit_id,
        timeout=args.timeout,
        wait_between_requests=args.cooldown,
    )
    await client.connect()
    try:
        device = await create_device_instance(client)
        if not isinstance(device, SUN2000Device):
            print(f"Expected a SUN2000, got {type(device).__name__}")
            return report

        caps = capabilities_of(device)
        print(f"\n  {device.model_name} ({device.serial_number}) capabilities: {sorted(caps)}")
        report["capabilities"] = sorted(caps)

        _hdr("1. BACKUP REGISTERS, PROBED INDIVIDUALLY")
        report["backup"] = await probe_backup_registers(device)

        live = live_register_names(caps)
        packs = pack_register_names(caps)

        def _report_block(block: list[str], worked: list[list[str]]) -> None:
            """Print what a planned span turned into once the device had its say."""
            start, width = _span(block)
            where = f"{start}..{start + width - 1}"
            if len(worked) == 1 and len(worked[0]) == len(block):
                print(f"    {where:<18} {len(block):>3} registers  ok")
            elif worked:
                print(f"    {where:<18} {len(block):>3} registers  narrowed to "
                      f"{len(worked)} reads of {[len(part) for part in worked]}")
            else:
                print(f"    {where:<18} {len(block):>3} registers  UNREADABLE")

        _hdr("2. DOES THE DEVICE TOLERATE WIDE SPANS?")
        print("  -- live registers --")
        live_plan, live_bad = await validate_plan(device, build_read_plan(live), _report_block)
        print("  -- battery pack registers --")
        pack_plan, pack_bad = await validate_plan(device, build_read_plan(packs), _report_block)
        report["unreadable"] = {"live": live_bad, "packs": pack_bad}
        report["plan"] = {
            "live_blocks": [{"start": _span(b)[0], "span": _span(b)[1], "registers": len(b)} for b in live_plan],
            "pack_blocks": [{"start": _span(b)[0], "span": _span(b)[1], "registers": len(b)} for b in pack_plan],
        }

        _hdr(f"3. BLOCK READS vs batch_update ({args.rounds} rounds, {len(live)} live registers)")
        batch_samples, batch_values = await time_batch_update(device, live, args.rounds)
        block_samples, block_values = await time_plan(device, live_plan, args.rounds)

        batch_stats, block_stats = _stats(batch_samples), _stats(block_samples)
        print(f"  batch_update    {len(live)} regs   median {batch_stats['median_ms']:>8.1f} ms")
        print(f"  block reads     {len(live)} regs   median {block_stats['median_ms']:>8.1f} ms"
              f"   in {len(live_plan)} reads")
        speedup = batch_stats["median_ms"] / block_stats["median_ms"] if block_stats["median_ms"] else 0
        print(f"  speedup         {speedup:.2f}x")
        report["live_timing"] = {"batch_update": batch_stats, "blocks": block_stats, "speedup": round(speedup, 2)}

        pack_batch, _ = await time_batch_update(device, packs, args.rounds)
        pack_block, _ = await time_plan(device, pack_plan, args.rounds)
        pb, pk = _stats(pack_batch), _stats(pack_block)
        print(f"\n  packs: batch_update median {pb['median_ms']:.1f} ms  ->  blocks {pk['median_ms']:.1f} ms"
              f"  ({pb['median_ms'] / pk['median_ms'] if pk['median_ms'] else 0:.2f}x)")
        report["pack_timing"] = {"batch_update": pb, "blocks": pk}

        _hdr("4. DO BOTH PATHS DECODE THE SAME VALUES?")
        comparison = compare_values(batch_values, block_values)
        if comparison["missing_from_blocks"]:
            print(f"  MISSING from block reads: {comparison['missing_from_blocks']}")
        if comparison["extra_in_blocks"]:
            print(f"  extra in block reads: {comparison['extra_in_blocks']}")
        type_errors = {k: v for k, v in comparison["differing"].items() if v["kind"] == "type mismatch"}
        moved = {k: v for k, v in comparison["differing"].items() if v["kind"] == "value moved"}
        if type_errors:
            print(f"  TYPE MISMATCHES ({len(type_errors)}):")
            for name, info in type_errors.items():
                print(f"    {name}: {info['batch_update']} vs {info['block_read']}")
        print(f"  {len(batch_values)} registers compared, {len(type_errors)} type mismatches, "
              f"{len(moved)} values moved between reads (expected for live electrical data)")
        if moved and args.show_moved:
            for name, info in list(moved.items())[:20]:
                print(f"    {name}: {info['batch_update']} -> {info['block_read']}")
        report["comparison"] = comparison

        _hdr("5. RESULTING CADENCE")
        live_cycle = block_stats["median_ms"]
        print(f"  live block pass          {live_cycle:.0f} ms  ({len(live_plan)} reads, {len(live)} registers)")
        print(f"  pack block pass          {pk['median_ms']:.0f} ms  ({len(pack_plan)} reads, {len(packs)} registers)")
        print(f"  sustainable live interval {max(1.0, live_cycle * 1.5 / 1000):.1f} s  (50% headroom)")
        report["cadence"] = {
            "live_cycle_ms": live_cycle,
            "pack_cycle_ms": pk["median_ms"],
            "sustainable_live_interval_s": round(max(1.0, live_cycle * 1.5 / 1000), 1),
        }
    finally:
        with contextlib.suppress(Exception):
            await client.disconnect()
    return report


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="s2d-bench",
        description="Compare wide block reads against the library's default batching.",
    )
    parser.add_argument("host")
    parser.add_argument("--port", type=int, default=502)
    parser.add_argument("--unit-id", type=int, default=1)
    parser.add_argument("--timeout", type=int, default=10)
    parser.add_argument("--cooldown", type=float, default=0.05)
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--show-moved", action="store_true", help="List registers whose value changed between reads")
    parser.add_argument("--json", dest="json_path", default=None)
    args = parser.parse_args()

    try:
        report = asyncio.run(run_bench(args))
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130

    if args.json_path:
        with open(args.json_path, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, default=str)
        print(f"\nFull report written to {args.json_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
