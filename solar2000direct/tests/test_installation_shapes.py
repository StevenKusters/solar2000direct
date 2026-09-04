"""What the add-on does on installations unlike the one it was written against.

The reference system is a three-phase SUN2000-8KTL-M1 with two MPPT strings, two LUNA2000
units, a Backup Box, a DTSU666 and optimizers on one string. Every count in that sentence
was once a literal somewhere in the code, which is invisible until somebody else installs
it: a four-input inverter reported half its array, a one-cabinet battery never polled its
only unit, and a single-phase site published two phases that read a steady zero.

These check the shape-dependent parts against installations that differ in each of those
ways. They are unit tests over the planner and the entity table, not a live inverter.

Run with: python solar2000direct/tests/test_installation_shapes.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "solar2000direct" / "src"))

from solar2000direct.config import ArrayConfig
from solar2000direct.mqtt import SENSORS
from solar2000direct.registers import (
    CAP_BACKUP,
    CAP_BATTERY_1,
    CAP_BATTERY_2,
    CAP_METER,
    CAP_OPTIMIZERS,
    CAP_P1,
    CAP_THREE_PHASE,
    Shape,
    build_read_plan,
    pack_register_names,
    pollable_register_names,
    split_plan_by_value,
)
from solar2000direct.state import State

FAILURES: list[str] = []


def check(name: str, condition: object, detail: str = "") -> None:
    passed = bool(condition)
    print(f"  {'PASS' if passed else 'FAIL'}  {name}" + (f"  ({detail})" if not passed and detail else ""))
    if not passed:
        FAILURES.append(name)


REFERENCE = frozenset({CAP_BATTERY_1, CAP_BATTERY_2, CAP_METER, CAP_THREE_PHASE,
                       CAP_BACKUP, CAP_OPTIMIZERS, CAP_P1})


def main() -> int:
    # --- the reference installation must not regress -----------------------------
    shape = Shape(pv_strings=2, battery_units=2)
    names = pollable_register_names(REFERENCE, shape)
    live, _slow = split_plan_by_value(build_read_plan(names), shape=shape)
    # Round-trips are what cost; registers riding inside a block already being read are
    # free, so the test is on the number of reads and on nothing having been dropped.
    check("the reference site still polls its live tier in three reads", len(live) == 3, f"got {len(live)}")
    check("and carries at least as many registers as it used to",
          sum(len(block) for block in live) >= 54, f"got {sum(len(b) for b in live)}")

    # --- a four-input inverter ---------------------------------------------------
    four = pollable_register_names(frozenset({CAP_METER, CAP_THREE_PHASE}), Shape(pv_strings=4))
    for index in (1, 2, 3, 4):
        check(f"a four-input inverter polls string {index}",
              f"pv_{index:02d}_voltage" in four and f"cumulative_dc_energy_yield_mppt{index}" in four)
    check("and no fifth string it does not have", "pv_05_voltage" not in four)
    live4, _ = split_plan_by_value(build_read_plan(four), shape=Shape(pv_strings=4))
    check("reading four strings costs no more round-trips than two",
          len(live4) <= len(live), f"{len(live4)} vs {len(live)}")

    # --- a single-input inverter -------------------------------------------------
    one = pollable_register_names(frozenset({CAP_METER}), Shape(pv_strings=1))
    check("a single-input inverter is not asked about a second string",
          "pv_02_voltage" not in one and "cumulative_dc_energy_yield_mppt2" not in one)

    # --- one battery cabinet -----------------------------------------------------
    single = pollable_register_names(frozenset({CAP_BATTERY_1, CAP_METER}), Shape(battery_units=1))
    check("a one-unit battery polls the unit it has",
          "storage_unit_1_state_of_capacity" in single)
    check("and not the unit it does not", "storage_unit_2_state_of_capacity" not in single)
    live1, _ = split_plan_by_value(build_read_plan(single), shape=Shape(battery_units=1))
    live_names = {name for block in live1 for name in block}
    check("its battery power stays in the live tier",
          "storage_charge_discharge_power" in live_names and "storage_state_of_capacity" in live_names,
          f"live tier holds {sorted(n for n in live_names if 'storage' in n)}")

    # --- no battery at all -------------------------------------------------------
    pv_only = pollable_register_names(frozenset({CAP_METER, CAP_THREE_PHASE}), Shape())
    check("a PV-only site polls no storage registers",
          not [n for n in pv_only if n.startswith("storage")])
    check("and no battery packs", not pack_register_names(frozenset({CAP_METER})))

    # --- single phase ------------------------------------------------------------
    single_phase = pollable_register_names(frozenset({CAP_METER}), Shape())
    check("a single-phase site is not asked for phase B or C",
          "phase_B_voltage" not in single_phase and "active_grid_C_power" not in single_phase)
    check("but is still asked for phase A", "phase_A_voltage" in single_phase)

    # --- the identity read that used to end the session --------------------------
    from solar2000direct.registers import IDENTITY
    check("the startup read no longer asks for a register some inverters reject",
          "nb_optimizers" not in IDENTITY.registers)

    # --- entities follow the same rules ------------------------------------------
    def entities(caps: frozenset[str]) -> set[str]:
        return {s.key for s in SENSORS if s.requires <= caps}

    bare = entities(frozenset())
    check("no battery entity is offered to a site with no battery",
          not [k for k in bare if k.startswith(("battery_", "storage_"))],
          f"got {sorted(k for k in bare if k.startswith(('battery_', 'storage_')))}")
    check("no phase B or C entity on a single-phase site",
          "phase_B_voltage" not in bare and "active_grid_C_power" not in bare)
    check("no P1 cross-check entity without a P1 feed",
          not [k for k in bare if k.startswith("p1_") or k.startswith("meter_delta")],
          f"got {sorted(k for k in bare if k.startswith(('p1_', 'meter_delta')))}")
    check("no served-without-the-grid entity without a meter to measure it",
          "instant_self_supply_pct" not in bare)
    check("the reference site still gets all of them",
          entities(REFERENCE) == {s.key for s in SENSORS})

    # --- derived values on a battery-free installation ---------------------------
    state = State()
    state.capabilities = frozenset({CAP_METER})
    # The meter register is raw, and the default convention has export positive, so this
    # is 1 kW going out: 4 kW of solar against a 3 kW house.
    state.update_registers({"active_power": 4000.0, "input_power": 4200.0,
                            "power_meter_active_power": 1000.0})
    derived = state.derived()
    check("a PV-only site still gets its AC-side solar figure",
          derived.get("pv_power_ac_w") == 4000.0, f"got {derived.get('pv_power_ac_w')!r}")
    check("and its served-without-the-grid percentage",
          derived.get("house_load_w") == 3000.0 and derived.get("instant_self_supply_pct") == 100.0,
          f"house {derived.get('house_load_w')!r}, served {derived.get('instant_self_supply_pct')!r}")
    # And when it is drawing from the grid the figure is a real fraction, not a ceiling.
    state.update_registers({"power_meter_active_power": -1000.0})
    check("and reports a partial figure when the grid is carrying some of the load",
          state.derived().get("instant_self_supply_pct") == 80.0,
          f"got {state.derived().get('instant_self_supply_pct')!r}")

    # --- a single battery module -------------------------------------------------
    state = State()
    state.capabilities = frozenset({CAP_BATTERY_1})
    state.update_registers({
        "storage_unit_1_battery_pack_1_voltage": 358.0,
        "storage_unit_1_battery_pack_1_state_of_capacity": 61.0,
        "storage_unit_1_battery_pack_1_maximum_temperature": 24.0,
        "storage_unit_1_battery_pack_1_minimum_temperature": 21.0,
    })
    derived = state.derived()
    check("a one-module battery reports its count", derived.get("battery_pack_count") == 1,
          f"got {derived.get('battery_pack_count')!r}")
    check("and its level", derived.get("battery_pack_soc_mean_pct") == 61.0)
    check("but claims no spread against packs it does not have",
          "battery_pack_soc_spread_pct" not in derived and "battery_pack_temp_spread_c" not in derived,
          f"got {derived.get('battery_pack_temp_spread_c')!r}")
    check("and does not mistake its own internal gradient for pack-to-pack drift",
          derived.get("battery_pack_temp_max_c") == 24.0)

    # --- figures that must not read absence as a measured zero -------------------
    import asyncio
    import tempfile

    from solar2000direct.config import HistoryConfig
    from solar2000direct.history import History

    t0 = 1_756_450_800
    with tempfile.TemporaryDirectory() as tmp:
        history = History(HistoryConfig(enabled=True, path=str(Path(tmp) / "s.db")), State())
        connection = history._connect()
        history._connection = connection
        # A PV-only site: only the counters such an inverter actually has.
        for i in range(25):
            for name, base, step in (
                ("accumulated_yield_energy", 4000.0, 0.2),
                ("cumulative_dc_energy_yield_mppt1", 2000.0, 0.1),
            ):
                connection.execute("INSERT INTO counters (ts, name, value) VALUES (?, ?, ?)",
                                   (t0 + i * 300, name, base + step * i))
        connection.commit()
        row = asyncio.run(history.energy_buckets(t0, t0 + 7200, "day"))["rows"][0]
        check("a site with no meter reports no grid import rather than zero",
              row["grid_import_kwh"] is None, f"got {row['grid_import_kwh']!r}")
        check("and no house consumption rather than its inverter output",
              row["house_consumption_kwh"] is None, f"got {row['house_consumption_kwh']!r}")
        check("and no battery figures rather than zeroes",
              row["battery_charged_kwh"] is None, f"got {row['battery_charged_kwh']!r}")
        check("while still reporting the PV yield it does measure",
              row["pv_yield_kwh"] and row["pv_yield_kwh"] > 0, f"got {row['pv_yield_kwh']!r}")

    with tempfile.TemporaryDirectory() as tmp:
        history = History(HistoryConfig(enabled=True, path=str(Path(tmp) / "e.db")), State())
        connection = history._connect()
        history._connection = connection
        # A battery site with no grid meter: house_w and grid_w are never recorded.
        rows = []
        for i in range(0, 86400, 60):
            battery = 4000.0 if i < 43200 else -3600.0
            rows.append((t0 + i, 3000.0, battery))
        connection.executemany(
            "INSERT INTO samples (ts, pv_w, battery_w) VALUES (?, ?, ?)", rows)
        connection.commit()
        result = asyncio.run(history.round_trip_efficiency(t0, t0 + 86400))
        check("round-trip is still measurable without a meter",
              result.get("measurable") is True, f"got {result!r}")
        check("but the whole-system figure is withheld rather than counting all production as loss",
              "system_round_trip_pct" not in result and result.get("reliable") is False,
              f"got {result.get('system_round_trip_pct')!r}")
        check("and the battery's own ratio is still reported",
              result.get("battery_round_trip_pct", 0) > 0,
              f"got {result.get('battery_round_trip_pct')!r}")

    # --- the array capacity that used to be one roof's watt-peak -----------------
    check("array capacity is computed from the panels",
          ArrayConfig(panel_counts=[12, 8], panel_watts=430).peak_w == 8600)
    check("and is zero, not a guess, when the panels are not described",
          ArrayConfig(panel_counts=[12, 8]).peak_w == 0)

    print("\n" + ("all checks passed" if not FAILURES else f"FAILED: {', '.join(FAILURES)}"))
    return 0 if not FAILURES else 1


if __name__ == "__main__":
    sys.exit(main())
