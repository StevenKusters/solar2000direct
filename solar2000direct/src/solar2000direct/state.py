"""The collector's in-memory view of the installation, plus the values it derives.

One process owns the Modbus session, so one object owns the resulting state and every
consumer -- the HTTP API, the live page, MQTT, the history writer -- reads from here.
Nothing else touches the bus.

The derived section is most of the reason to do this locally. The inverter reports string
voltage and string current but never string power, and it reports its own AC output and
the grid meter but never house load. Those are one multiplication and one addition away,
and they are exactly the numbers you want when the question is "which half of the roof is
underperforming" or "what is the house actually drawing right now".
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import Any

from solar2000direct.config import ArrayConfig, MeterConfig
from solar2000direct.registers import (
    CAP_BATTERY_1,
    MAX_BATTERY_UNITS,
    MAX_PACKS_PER_UNIT,
    Shape,
)

# Huawei's sign conventions. `verify_signs()` checks them against live data rather than
# trusting this comment -- which is how the first one was found to be backwards: on a real
# installation at night, with no sun and an idle battery, the derived house load came out
# at -918 W. The grid meter read -919 W while the P1 meter read +938 W for the same
# quantity at the same moment.
GRID_IMPORT_IS_POSITIVE = False
"""power_meter_active_power: NEGATIVE when importing, positive when exporting to grid.

The opposite of the P1 meter, which reports consumption minus production. Overridable per
site through configuration, because current transformers can be fitted either way round."""

BATTERY_CHARGE_IS_POSITIVE = True
"""storage_charge_discharge_power: positive = charging, negative = discharging.

Documentation, not a switch. The grid convention is overridable because a current
transformer is fitted by an installer and can go on either way round; the battery register
is reported by the inverter itself, so there is nothing site-specific to get wrong. The
branch that flipped it was unreachable and has been removed -- `verify_signs` still checks
the assumption against live readings rather than trusting this note."""

SYMMETRIC_INJECTION_TOLERANCE = 0.05
"""Fractional spread in inverter phase currents still counted as symmetric injection."""


@dataclass(slots=True)
class Reading:
    """One register value and when it was last successfully read."""

    value: Any
    unit: str | None
    timestamp: float

    @property
    def age(self) -> float:
        return time.time() - self.timestamp


@dataclass(slots=True)
class PollStats:
    """How the bus is behaving. Exposed because a dashboard that hides its own staleness
    is worse than no dashboard: every number here is only as true as its last read."""

    connected: bool = False
    connected_since: float | None = None
    last_live_read: float | None = None
    last_pack_read: float | None = None
    last_optimizer_read: float | None = None
    live_cycle_ms: float | None = None
    reads_ok: int = 0
    reads_failed: int = 0
    reconnects: int = 0
    last_error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        now = time.time()
        return {
            "connected": self.connected,
            "uptime_s": round(now - self.connected_since, 1) if self.connected_since else None,
            "live_age_s": round(now - self.last_live_read, 2) if self.last_live_read else None,
            "pack_age_s": round(now - self.last_pack_read, 1) if self.last_pack_read else None,
            "optimizer_age_s": round(now - self.last_optimizer_read, 1) if self.last_optimizer_read else None,
            "live_cycle_ms": self.live_cycle_ms,
            "reads_ok": self.reads_ok,
            "reads_failed": self.reads_failed,
            "reconnects": self.reconnects,
            "last_error": self.last_error,
        }


class State:
    """Latest known value for everything, and the values computed from them."""

    def __init__(self, array: ArrayConfig | None = None, meter: MeterConfig | None = None) -> None:
        self.array = array or ArrayConfig()
        self.meter = meter or MeterConfig(import_is_positive=GRID_IMPORT_IS_POSITIVE)
        self.readings: dict[str, Reading] = {}
        self.optimizers: dict[int, dict[str, Any]] = {}
        self.optimizer_info: dict[int, dict[str, Any]] = {}
        self.p1: dict[str, Any] = {}
        self.device: dict[str, Any] = {}
        self.capabilities: frozenset[str] = frozenset()
        # Capabilities that are configured rather than detected -- a P1 feed exists because
        # somebody pointed the add-on at one, not because the inverter reports it. Kept
        # apart so a reconnect, which rewrites what was detected, cannot clear them.
        self.site_capabilities: frozenset[str] = frozenset()
        self.shape = Shape()
        self.stats = PollStats()

    @property
    def all_capabilities(self) -> frozenset[str]:
        """Everything this installation has, however it came to be known."""
        return self.capabilities | self.site_capabilities

    # --- ingest ------------------------------------------------------------------

    def update_registers(self, values: dict[str, Any], timestamp: float | None = None) -> None:
        stamp = timestamp if timestamp is not None else time.time()
        for name, result in values.items():
            self.readings[name] = Reading(
                value=getattr(result, "value", result),
                unit=getattr(result, "unit", None),
                timestamp=stamp,
            )

    def update_optimizers(self, realtime: dict[int, Any], timestamp: float | None = None) -> None:
        stamp = timestamp if timestamp is not None else time.time()
        self.optimizers = {
            address: {
                "output_power": getattr(data, "output_power", None),
                "output_voltage": getattr(data, "output_voltage", None),
                "output_current": getattr(data, "output_current", None),
                "input_voltage": getattr(data, "input_voltage", None),
                "input_current": getattr(data, "input_current", None),
                "temperature": getattr(data, "temperature", None),
                "running_status": str(getattr(data, "running_status", "")) or None,
                "timestamp": stamp,
            }
            for address, data in realtime.items()
        }

    def update_optimizer_info(self, system_information: dict[int, Any]) -> None:
        """Optimizer serials, models and wiring, read once at startup.

        The inverter says which string each optimizer is on and where in that string it
        sits. That was being discarded and then asked for in the configuration instead, as
        a single string number -- which cannot describe an array with optimizers on more
        than one string, and had no business being a question at all.
        """
        self.optimizer_info = {
            address: {
                "sn": getattr(info, "sn", None),
                "model": getattr(info, "model", None),
                "software_version": getattr(info, "software_version", None),
                "string": getattr(info, "string_number", None),
                "position": getattr(info, "position_in_current_string", None),
                "rated_power": getattr(info, "rated_power", None),
            }
            for address, info in system_information.items()
        }

    @property
    def optimizer_strings(self) -> list[int]:
        """Which strings carry optimizers, as reported by the inverter.

        Empty when the inverter does not say -- which is a fact about the reading, not a
        claim that they are all on one string.
        """
        found = {
            info["string"]
            for info in self.optimizer_info.values()
            if isinstance(info.get("string"), int)
        }
        return sorted(found)

    # --- access ------------------------------------------------------------------

    def value(self, name: str) -> Any:
        reading = self.readings.get(name)
        return reading.value if reading else None

    def number(self, name: str) -> float | None:
        """Numeric value, or None if absent or not a number.

        Several registers decode to enums or strings, and a derived metric that silently
        coerces those would produce a plausible-looking wrong number.
        """
        value = self.value(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return float(value)

    # --- derived -----------------------------------------------------------------

    def derived(self) -> dict[str, Any]:  # noqa: PLR0912 - a flat table of independent formulas
        """Values the inverter does not report but which follow from ones it does."""
        out: dict[str, Any] = {}

        # Per-string power. The inverter gives volts and amps per MPPT but never watts,
        # which is the number that tells you one array is shaded and the other is not.
        for index in range(1, self.shape.pv_strings + 1):
            voltage = self.number(f"pv_{index:02d}_voltage")
            current = self.number(f"pv_{index:02d}_current")
            if voltage is not None and current is not None:
                out[f"pv_string_{index}_power_w"] = round(voltage * current, 1)

        out.update(self._per_panel())

        pv_dc = self.number("input_power")
        inverter_ac = self.number("active_power")
        grid = self.number("power_meter_active_power")
        battery = self.number("storage_charge_discharge_power")

        # Normalise to "positive means importing" for everything downstream.
        if not self.meter.import_is_positive and grid is not None:
            grid = -grid

        if pv_dc is not None:
            out["pv_power_w"] = pv_dc
        if inverter_ac is not None:
            out["inverter_power_w"] = inverter_ac
        if grid is not None:
            out["grid_power_w"] = grid
            out["grid_import_w"] = max(grid, 0.0)
            out["grid_export_w"] = max(-grid, 0.0)
        if battery is not None:
            out["battery_power_w"] = battery
            out["battery_charge_w"] = max(battery, 0.0)
            out["battery_discharge_w"] = max(-battery, 0.0)

        # Everything the house draws must arrive either from the inverter or the grid.
        if inverter_ac is not None and grid is not None:
            out["house_load_w"] = round(inverter_ac + grid, 1)

        # What fraction of the house's current draw is being served without the grid.
        #
        # Not one minus grid-import over house load: that assumes every watt drawn from
        # the grid goes to the house. When the grid is also charging the battery, import
        # exceeds house load and the fraction goes negative -- which it did, reading -33%
        # while the grid supplied 5.25 kW to a 3.95 kW house and a 1.30 kW charge.
        #
        # The house is served locally by whatever the panels and the battery are putting
        # out right now. A charging battery contributes nothing; it is a consumer.
        # An installation with no battery has no battery register, but it does have a
        # definite answer: nothing flows to or from a battery that is not fitted. Reading
        # that as unknown left both AC-side figures below uncomputed on every PV-only
        # site, while the entities for them were published all the same and stayed blank.
        flow = battery
        if flow is None and CAP_BATTERY_1 not in self.capabilities:
            flow = 0.0

        local = None
        if inverter_ac is not None and flow is not None:
            local = max(0.0, inverter_ac + flow) + max(0.0, -flow)
        if local is not None and isinstance(out.get("house_load_w"), (int, float)):
            house = out["house_load_w"]
            if house > 0:
                out["instant_self_supply_pct"] = round(100 * min(house, local) / house, 1)
            elif house == 0:
                out["instant_self_supply_pct"] = 100.0

        # Solar measured on the AC side. `input_power` is DC, so a flow diagram built on it
        # never quite balances: a couple of percent disappears into conversion between the
        # panels and everything else on the diagram. Backing the battery out of the
        # inverter's AC output gives the solar contribution in the same units as the rest.
        if inverter_ac is not None and flow is not None:
            out["pv_power_ac_w"] = round(max(0.0, inverter_ac + flow), 1)

        # Inverter conversion loss: DC in versus AC out, including battery flows.
        if pv_dc is not None and inverter_ac is not None and pv_dc > 0:
            out["conversion_loss_w"] = round(pv_dc - inverter_ac, 1)

        # Clipping headroom. An 8 kW inverter under 10.66 kWp of panels will clip; knowing
        # how close it is running to rated power is how you quantify what that costs.
        rated = self.number("rated_power")
        if rated and inverter_ac is not None and rated > 0:
            out["inverter_load_factor"] = round(inverter_ac / rated, 3)

        out.update(self._alarms())
        out.update(self._battery_pack_health())
        out.update(self._per_phase_load(inverter_ac))
        out.update(self._p1_reconciliation(grid))
        return out

    def _per_phase_load(self, inverter_ac: float | None) -> dict[str, Any]:
        """House load per phase, when the inverter is injecting symmetrically.

        The grid meter sees injection minus load on each phase, so neither term is
        directly visible. But if the inverter puts the same current on all three phases,
        its contribution is simply a third of total output, and per-phase load falls out:

            load_phase = meter_phase + inverter_total / 3

        Symmetry is verified from the inverter's own phase currents rather than assumed,
        because the arithmetic is meaningless under asymmetric injection and a plausible
        wrong number is worse than no number. Where it holds, this answers "which phase
        are my loads actually on", which is the question behind any rebalancing decision.
        """
        currents = [self.number(f"phase_{phase}_current") for phase in ("A", "B", "C")]
        # Per-phase meter powers follow the same convention as the total, so they need the
        # same normalisation before being mixed with the inverter's output.
        sign = 1.0 if self.meter.import_is_positive else -1.0
        meters = [
            None if (raw := self.number(f"active_grid_{phase}_power")) is None else sign * raw
            for phase in ("A", "B", "C")
        ]
        if inverter_ac is None or any(value is None for value in (*currents, *meters)):
            return {}

        spread = max(currents) - min(currents)  # type: ignore[type-var]
        reference = max(max(currents), 0.1)  # type: ignore[type-var,call-overload]
        if spread / reference > SYMMETRIC_INJECTION_TOLERANCE:
            return {"injection_symmetric": False}

        share = inverter_ac / 3
        out: dict[str, Any] = {"injection_symmetric": True}
        for phase, meter in zip(("A", "B", "C"), meters, strict=True):
            out[f"house_load_phase_{phase}_w"] = round(meter + share, 1)  # type: ignore[operator]
        return out

    def _alarms(self) -> dict[str, Any]:
        """Flatten the inverter's active alarms into something alertable.

        The three alarm registers decode to lists of Alarm objects, which the MQTT payload
        drops because it carries only scalars -- so the most important thing the inverter
        can tell you was being read every few seconds and thrown away. Reduced here to a
        count worth alerting on and a line worth reading.
        """
        alarms: list[Any] = []
        for index in (1, 2, 3):
            value = self.value(f"alarm_{index}")
            if isinstance(value, (list, tuple)):
                alarms.extend(value)
        if not any(self.readings.get(f"alarm_{i}") for i in (1, 2, 3)):
            return {}

        out: dict[str, Any] = {"active_alarms": len(alarms)}
        out["alarm_summary"] = (
            ", ".join(
                f"{getattr(a, 'name', a)} ({getattr(a, 'level', '?')})" for a in alarms
            )
            if alarms
            else "None"
        )
        levels = {str(getattr(a, "level", "")).lower() for a in alarms}
        if levels & {"major", "critical"}:
            out["alarm_severity"] = "major"
        elif alarms:
            out["alarm_severity"] = "minor"
        else:
            out["alarm_severity"] = "none"
        return out

    def _per_panel(self) -> dict[str, Any]:
        """Normalise each string by its panel count.

        Two strings of different sizes cannot be compared directly: the bigger one
        produces more, which says nothing about the health of its panels. Dividing by
        panel count is what turns "the west string makes 13% more energy" into "the west
        panels each make 3% less", and only the second statement is diagnostic.

        Needs the panel counts, which the inverter does not report -- optimizers are
        usually fitted to only part of an array, so their count is not the panel count.
        """
        out: dict[str, Any] = {}
        live: dict[int, float] = {}
        lifetime: dict[int, float] = {}

        for index in (1, 2, 3, 4):
            panels = self.array.panels(index)
            if not panels:
                continue

            power = self.number(f"pv_string_{index}_power_w") or (
                (self.number(f"pv_{index:02d}_voltage") or 0) * (self.number(f"pv_{index:02d}_current") or 0)
            )
            if power:
                live[index] = power / panels
                out[f"pv_string_{index}_w_per_panel"] = round(live[index], 1)

            total = self.number(f"cumulative_dc_energy_yield_mppt{index}")
            if total:
                lifetime[index] = total / panels
                out[f"pv_string_{index}_kwh_per_panel"] = round(lifetime[index], 1)

        # The headline diagnostic: how far apart the strings are once size is accounted
        # for. A few percent is orientation. A step change is a fault.
        for label, values in (("live", live), ("lifetime", lifetime)):
            if len(values) < 2:
                continue
            best, worst = max(values.values()), min(values.values())
            if best > 0:
                out[f"string_imbalance_{label}_pct"] = round(100 * (1 - worst / best), 1)
        return out

    def _battery_pack_health(self) -> dict[str, Any]:
        """Spread across battery packs.

        A pack drifting away from its siblings in state of charge or temperature is the
        earliest visible sign of a failing module, and it is precisely what the cloud
        portal averages away.

        Presence is decided on pack voltage, not on whether the other readings are
        non-zero. A battery is never at 0 V, so voltage cleanly separates "this pack is
        not installed" from "this pack is reading zero" -- and the readings genuinely can
        be zero: an outdoor LUNA2000 hits 0 degrees in winter and a drained pack reports
        0% state of charge. Filtering those out would quietly drop the spread figures
        exactly when they matter most.
        """
        out: dict[str, Any] = {}
        socs: list[float] = []
        voltages: list[float] = []
        # Each pack's own warmest and coldest cell, kept apart. Flattening both bounds
        # into one list made a single-pack battery look like two: its internal top-to-
        # bottom gradient was reported as drift between modules, which is a different
        # measurement and a much more alarming one.
        pack_temps: list[tuple[float, float]] = []
        for unit in range(1, MAX_BATTERY_UNITS + 1):
            for pack in range(1, MAX_PACKS_PER_UNIT + 1):
                prefix = f"storage_unit_{unit}_battery_pack_{pack}"
                voltage = self.number(f"{prefix}_voltage")
                if voltage is None or voltage <= 0:
                    continue
                voltages.append(voltage)

                soc = self.number(f"{prefix}_state_of_capacity")
                if soc is not None:
                    socs.append(soc)
                warmest = self.number(f"{prefix}_maximum_temperature")
                coldest = self.number(f"{prefix}_minimum_temperature")
                if warmest is not None and coldest is not None:
                    pack_temps.append((warmest, coldest))

        # A one-pack battery has a count and a level, and no spread -- there is nothing
        # for it to be spread against. Reporting the count only when there were two or
        # more meant a single-module owner was told nothing at all about their battery.
        if socs:
            out["battery_pack_count"] = len(socs)
            # The mean is here because the spread below cannot be read without it: near
            # the bottom of LFP's flat voltage plateau the charge estimate is at its least
            # reliable, so the same spread means less down there than it does near the top.
            out["battery_pack_soc_mean_pct"] = round(sum(socs) / len(socs), 1)
        if len(socs) > 1:
            out["battery_pack_soc_spread_pct"] = round(max(socs) - min(socs), 2)
        if len(voltages) > 1:
            # Measured rather than inferred, so this is the more trustworthy of the two.
            out["battery_pack_voltage_spread_v"] = round(max(voltages) - min(voltages), 3)
        if pack_temps:
            out["battery_pack_temp_max_c"] = round(max(warm for warm, _ in pack_temps), 1)
        if len(pack_temps) > 1:
            # Pack against pack, each represented by its own warmest cell.
            warmest = [warm for warm, _ in pack_temps]
            out["battery_pack_temp_spread_c"] = round(max(warmest) - min(warmest), 2)
        return out

    def _p1_reconciliation(self, huawei_grid_w: float | None) -> dict[str, Any]:
        """Compare the Huawei meter against the P1 meter Home Assistant reads.

        Two independent measurements of the same physical quantity. Their difference is
        the only honest error bar available on either of them, and a persistent offset
        means one of the two is drifting.
        """
        p1_power = self.p1.get("grid_power_w")
        if huawei_grid_w is None or not isinstance(p1_power, (int, float)):
            return {}
        delta = float(p1_power) - huawei_grid_w
        out: dict[str, Any] = {
            "p1_grid_power_w": float(p1_power),
            "meter_delta_w": round(delta, 1),
        }
        if abs(huawei_grid_w) > 100:
            out["meter_delta_pct"] = round(100 * delta / abs(huawei_grid_w), 2)
        return out

    # --- snapshot ----------------------------------------------------------------

    def snapshot(self, *, include_raw: bool = True) -> dict[str, Any]:
        """The complete current view, as returned by the API and pushed to MQTT."""
        snapshot: dict[str, Any] = {
            "timestamp": time.time(),
            "device": self.device,
            "capabilities": sorted(self.all_capabilities),
            "derived": self.derived(),
            "optimizers": self.optimizers,
            "optimizer_info": self.optimizer_info,
            "p1": self.p1,
            "stats": self.stats.as_dict(),
        }
        if include_raw:
            snapshot["registers"] = {
                name: {"value": reading.value, "unit": reading.unit, "age_s": round(reading.age, 2)}
                for name, reading in sorted(self.readings.items())
            }
        return snapshot


    def flat(self) -> dict[str, Any]:
        """A single flat dict of everything publishable.

        MQTT discovery entities each pull one key out of one retained JSON payload, so a
        single publish updates every entity at once. Flat rather than nested keeps the
        Home Assistant value templates to `{{ value_json.<key> }}` with no path handling.
        """
        payload: dict[str, Any] = dict(self.derived())
        # A snapshot: this runs in the history writer's thread while the collector
        # mutates the same dict, and iterating it directly can raise mid-pass.
        for name, reading in list(self.readings.items()):
            payload.setdefault(name, _readable(reading.value))
        for key, value in self.p1.items():
            if isinstance(value, (int, float, str)):
                payload[f"p1_{key}"] = value
        payload.update(
            {
                "live_age_s": round(time.time() - self.stats.last_live_read, 2)
                if self.stats.last_live_read
                else None,
                "live_cycle_ms": self.stats.live_cycle_ms,
                "reads_ok": self.stats.reads_ok,
                "reads_failed": self.stats.reads_failed,
                "reconnects": self.stats.reconnects,
            },
        )
        for address, data in self.optimizers.items():
            payload[f"optimizer_{address}_power_w"] = data.get("output_power")
            payload[f"optimizer_{address}_temperature_c"] = data.get("temperature")
        return payload


def _readable(value: Any) -> Any:
    """Render a register value for humans.

    Several registers decode to IntEnum members, which JSON serialises as their integer:
    a meter status entity reading "1" instead of "Normal" is worse than not publishing it.
    Enum names are title-cased and de-underscored so they read as text in Home Assistant.
    """
    if isinstance(value, Enum):
        return value.name.replace("_", " ").title()
    return value


def verify_signs(state: State) -> list[str]:
    """Sanity-check the sign conventions against live readings.

    Checks the *derived* values, not the raw registers, so it tests the conventions as
    actually applied. Reading the registers directly would report a fault on every site
    whose meter follows the documented convention -- which was this function's own first
    bug, firing correctly about a wrong default and then continuing to fire after the
    default was corrected.

    Most useful after dark, when there is no solar to mask an inverted meter. A daytime
    reading is consistent with either convention, which is exactly why the fault survived
    an afternoon of benchmarking and surfaced on the first night.
    """
    warnings: list[str] = []
    derived = state.derived()
    load = derived.get("house_load_w")
    pv = state.number("input_power")
    battery = derived.get("battery_power_w")

    if isinstance(load, (int, float)) and load < -50:
        warnings.append(
            f"Derived house load is {load:.0f} W, which is not physically possible. "
            "The grid meter sign convention is probably inverted for this site: set "
            "grid_import_is_positive to flip it.",
        )
    if pv is not None and pv < -50:
        warnings.append(f"PV input power is negative ({pv:.0f} W), which should never happen.")
    if (
        isinstance(battery, (int, float))
        and pv is not None
        and pv < 50
        and battery > 100
    ):
        warnings.append(
            f"Battery reads as charging ({battery:.0f} W) with no PV production. "
            "Either it is charging from the grid, or the battery sign convention is inverted.",
        )

    # Two independent measurements of one quantity should not disagree about direction.
    p1 = state.p1.get("grid_power_w")
    grid = derived.get("grid_power_w")
    if isinstance(p1, (int, float)) and isinstance(grid, (int, float)) and abs(p1) > 100 and abs(grid) > 100:
        if (p1 > 0) != (grid > 0):
            warnings.append(
                f"The Huawei meter reads {grid:.0f} W while the P1 meter reads {p1:.0f} W. "
                "They disagree about which way the power is flowing, so one of them is inverted.",
            )
        elif abs(abs(p1) - abs(grid)) / max(abs(p1), abs(grid)) > 0.15:
            warnings.append(
                f"The Huawei meter reads {grid:.0f} W against the P1 meter's {p1:.0f} W, a "
                f"{abs(abs(p1) - abs(grid)) / max(abs(p1), abs(grid)):.0%} disagreement. "
                "Worth checking the current transformers.",
            )
    return warnings
