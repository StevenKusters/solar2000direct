"""Energy accounting over a window, against a database built by hand.

Worth its own test because the failures are quiet and plausible-looking. A counter that
was read all window but never advanced used to vanish from the result, which made
"exported nothing" indistinguishable from "no export data" -- and `energy_summary` will
not report house consumption without an export figure, so a two-hour window with no
export showed a blank House use next to a chart full of house consumption.

Run with: python solar2000direct/tests/test_history_energy.py
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "solar2000direct" / "src"))

from solar2000direct.config import HistoryConfig
from solar2000direct.history import (
    FULL,
    HOUR,
    MAX_PLAUSIBLE_KW,
    MINUTE,
    ROLLUP_COLUMNS,
    SAMPLE_COLUMNS,
    History,
)
from solar2000direct.state import State

FAILURES: list[str] = []


def check(name: str, condition: object, detail: str = "") -> None:
    passed = bool(condition)
    print(f"  {'PASS' if passed else 'FAIL'}  {name}" + (f"  ({detail})" if not passed and detail else ""))
    if not passed:
        FAILURES.append(name)


def near(a: object, b: float, tol: float = 0.01) -> bool:
    return isinstance(a, (int, float)) and abs(a - b) <= tol


def build(path: Path, counters: dict[str, list[tuple[int, float]]],
          samples: list[tuple[int, float, float, float, float]]) -> History:
    history = History(HistoryConfig(enabled=True, path=str(path)), State())
    connection = history._connect()
    for name, points in counters.items():
        connection.executemany(
            "INSERT INTO counters (ts, name, value) VALUES (?, ?, ?)",
            [(ts, name, value) for ts, value in points],
        )
    connection.executemany(
        "INSERT INTO samples (ts, pv_w, house_w, grid_w, battery_w) VALUES (?, ?, ?, ?, ?)",
        samples,
    )
    history._connection = connection
    return history


def main() -> int:
    t0 = 1_756_450_800          # a round starting point; the values are what matter
    window = 2 * 3600
    with tempfile.TemporaryDirectory() as tmp:
        # Two hours of sun: the house is covered by PV, a little is drawn from the grid,
        # and nothing at all is exported, so the export counter never moves.
        counters = {
            "grid_accumulated_energy": [(t0 + i * 300, 1000.0 + 0.01 * i) for i in range(25)],
            "grid_exported_energy":    [(t0 + i * 300, 500.0) for i in range(25)],
            "cumulative_dc_energy_yield_mppt1": [(t0 + i * 300, 2000.0 + 0.1 * i) for i in range(25)],
            "cumulative_dc_energy_yield_mppt2": [(t0 + i * 300, 3000.0 + 0.1 * i) for i in range(25)],
            "accumulated_yield_energy": [(t0 + i * 300, 4000.0 + 0.2 * i) for i in range(25)],
        }
        # 1 kW of house load throughout, met by PV; the battery charges and later discharges
        # within the same window, which is the case a single signed figure cannot express.
        samples = []
        for i in range(0, window + 1, 60):
            battery = 2000.0 if i < window / 2 else -1500.0
            samples.append((t0 + i, 4000.0, 1000.0, 0.0, battery))
        history = build(Path(tmp) / "h.db", counters, samples)

        deltas = asyncio.run(history.counter_delta(t0, t0 + window))
        check("a counter that never advanced reads zero, not missing",
              deltas.get("grid_exported_energy") == 0.0, f"got {deltas.get('grid_exported_energy')!r}")
        check("a counter that advanced reads its increment",
              near(deltas.get("grid_accumulated_energy"), 0.24), f"got {deltas.get('grid_accumulated_energy')!r}")
        check("a counter absent from the window stays absent",
              "storage_total_charge" not in deltas, f"got {deltas.get('storage_total_charge')!r}")

        summary = asyncio.run(history.energy_summary(t0, t0 + window))
        check("house consumption is reported when nothing was exported",
              near(summary.get("house_consumption_kwh"), 2.0), f"got {summary.get('house_consumption_kwh')!r}")
        check("self-sufficiency is reported alongside it",
              near(summary.get("self_sufficiency_pct"), 88.0, 1.0), f"got {summary.get('self_sufficiency_pct')!r}")

        # Both directions inside one window: an hour charging at 2 kW, an hour at -1.5 kW.
        check("energy into the battery is counted",
              near(summary.get("battery_charged_kwh"), 2.0, 0.05), f"got {summary.get('battery_charged_kwh')!r}")
        check("energy out of the battery is counted separately",
              near(summary.get("battery_discharged_kwh"), 1.5, 0.05), f"got {summary.get('battery_discharged_kwh')!r}")

        profile = asyncio.run(history.energy_profile(t0, t0 + window, 3600))
        # As the chart does: the sample sitting exactly on the closing boundary opens a
        # one-sample bucket, which is not an hour and would read as a collapse.
        rows = [row for row in profile["rows"] if row["coverage"] > 0.6]
        check("the profile splits the window into whole buckets", len(rows) == 2, f"got {len(rows)}")
        if len(rows) == 2:
            check("the first bucket shows charging and no discharge",
                  near(rows[0]["battery_charged_kwh"], 2.0, 0.05) and rows[0]["battery_discharged_kwh"] == 0.0,
                  f"got {rows[0]['battery_charged_kwh']!r} / {rows[0]['battery_discharged_kwh']!r}")
            check("the second bucket shows discharging and no charge",
                  near(rows[1]["battery_discharged_kwh"], 1.5, 0.05) and rows[1]["battery_charged_kwh"] == 0.0,
                  f"got {rows[1]['battery_charged_kwh']!r} / {rows[1]['battery_discharged_kwh']!r}")

    # The power series must keep both directions of a bucket that went both ways. Averaging
    # the signed column first cannot: charge and discharge cancel before anyone sees them.
    with tempfile.TemporaryDirectory() as tmp:
        samples = []
        for i in range(0, 600, 10):
            samples.append((t0 + i, 3000.0, 1000.0,
                            -800.0 if i < 300 else 400.0,      # grid: exporting, then importing
                            2000.0 if i < 300 else -1500.0))   # battery: charging, then discharging
        # A second bucket with no battery or meter reading at all.
        for i in range(600, 1200, 10):
            samples.append((t0 + i, 3000.0, 1000.0, None, None))
        history = History(HistoryConfig(enabled=True, path=str(Path(tmp) / "s.db")), State())
        connection = history._connect()
        connection.executemany(
            "INSERT INTO samples (ts, pv_w, house_w, grid_w, battery_w) VALUES (?, ?, ?, ?, ?)",
            samples,
        )
        history._connection = connection

        rows = asyncio.run(history.series(t0, t0 + 1200, 2))["rows"]
        check("the window splits into the requested buckets", len(rows) == 2, f"got {len(rows)}")
        if len(rows) == 2:
            mixed, empty = rows
            check("the signed average alone would have hidden the discharge",
                  near(mixed["battery_w"], 250.0, 1.0), f"got {mixed['battery_w']!r}")
            check("mean charging power survives the average",
                  near(mixed["battery_charge_w"], 1000.0, 1.0), f"got {mixed['battery_charge_w']!r}")
            check("mean discharging power survives it too",
                  near(mixed["battery_discharge_w"], 750.0, 1.0), f"got {mixed['battery_discharge_w']!r}")
            check("the two directions still net to the signed average",
                  near(mixed["battery_charge_w"] - mixed["battery_discharge_w"], mixed["battery_w"], 0.2))
            check("the grid splits the same way",
                  near(mixed["grid_import_w"], 200.0, 1.0) and near(mixed["grid_export_w"], 400.0, 1.0),
                  f"got {mixed['grid_import_w']!r} / {mixed['grid_export_w']!r}")
            check("a bucket with no reading says nothing rather than zero",
                  empty["battery_charge_w"] is None and empty["battery_discharge_w"] is None,
                  f"got {empty['battery_charge_w']!r} / {empty['battery_discharge_w']!r}")
            check("a bucket with no reading still reports the columns it does have",
                  near(empty["pv_w"], 3000.0), f"got {empty['pv_w']!r}")
            check("values are rounded rather than carrying false precision",
                  all(v is None or round(v, 1) == v for v in mixed.values()),
                  f"got {mixed!r}")

    # Every figure on the energy card has to describe the same day. It used to report
    # "self-consumed 26.22 kWh" beside a house that used 25.53 in total, 9.08 of it from
    # the grid: production minus export counts what is still in the battery as consumed.
    import math
    with tempfile.TemporaryDirectory() as tmp:
        midnight = t0 - t0 % 86400
        history = History(HistoryConfig(enabled=True, path=str(Path(tmp) / "day.db")), State())
        connection = history._connect()
        history._connection = connection
        counters = dict.fromkeys(
            ("cumulative_dc_energy_yield_mppt1", "grid_accumulated_energy",
             "grid_exported_energy", "accumulated_yield_energy"), 0.0)
        samples = []
        for i in range(0, 86400, 60):
            hour = i / 3600
            solar = max(0.0, 4200 * math.sin((hour - 7) / 12 * math.pi)) if 7 <= hour < 19 else 0.0
            house = 700.0 if hour < 6 else (1400.0 if hour < 18 else 1100.0)
            charge = max(0.0, min(3000.0, solar - house)) if 9 <= hour < 17 else 0.0
            if 2 <= hour < 4:
                charge = 1200.0            # grid-charging overnight, as a winter profile does
            discharge = 900.0 if 19 <= hour < 22 else 0.0
            inverter = solar - charge + discharge
            grid = house - inverter
            samples.append((midnight + i, solar, inverter, grid, charge - discharge, house))
            counters["cumulative_dc_energy_yield_mppt1"] += solar / 60000.0 / 0.97
            counters["grid_accumulated_energy"] += max(grid, 0.0) / 60000.0
            counters["grid_exported_energy"] += max(-grid, 0.0) / 60000.0
            counters["accumulated_yield_energy"] += max(inverter, 0.0) / 60000.0
            if i % 300 == 0:
                for name, value in counters.items():
                    connection.execute(
                        "INSERT INTO counters (ts, name, value) VALUES (?, ?, ?)",
                        (midnight + i, name, value))
        connection.executemany(
            "INSERT INTO samples (ts, pv_w, inverter_w, grid_w, battery_w, house_w) "
            "VALUES (?, ?, ?, ?, ?, ?)", samples)
        connection.commit()
        day = asyncio.run(history.energy_summary(midnight, midnight + 86400))

        produced = day["pv_yield_kwh"]
        slices = sum(day[k] for k in ("solar_to_house_kwh", "solar_to_battery_kwh",
                                      "solar_to_grid_kwh", "conversion_loss_kwh"))
        check("production divides exactly into where it went", near(slices, produced, 0.02),
              f"{slices} vs {produced}")
        used = sum(day[k] for k in ("from_solar_kwh", "from_battery_kwh", "from_grid_kwh"))
        check("consumption divides exactly into where it came from",
              near(used, day["house_consumption_kwh"], 0.02),
              f"{used} vs {day['house_consumption_kwh']}")
        check("self-consumed no longer exceeds what the house actually used",
              day["self_consumed_kwh"] <= day["house_consumption_kwh"],
              f"{day['self_consumed_kwh']} vs {day['house_consumption_kwh']}")
        check("energy stored in the battery is not counted as consumed",
              day["self_consumed_kwh"] < produced - day["solar_to_battery_kwh"] + 0.02,
              f"self-consumed {day['self_consumed_kwh']}, stored {day['solar_to_battery_kwh']}")
        check("grid charging is measured rather than attributed to the roof",
              day["grid_to_battery_kwh"] > 1.0, f"got {day['grid_to_battery_kwh']}")
        check("and the battery's contribution is not called solar",
              day["from_battery_kwh"] > 0 and day["from_solar_kwh"] < day["house_consumption_kwh"],
              f"solar {day['from_solar_kwh']}, battery {day['from_battery_kwh']}")

    # Rows in the hourly tier sit an hour apart by construction. Measured against a single
    # 300-second ceiling meant for outages, every one of them was discarded as a gap, so
    # every window older than the minute retention integrated to nothing at all.
    with tempfile.TemporaryDirectory() as tmp:
        history = History(HistoryConfig(enabled=True, path=str(Path(tmp) / "hour.db")), State())
        connection = history._connect()
        history._connection = connection
        day = t0 - t0 % 86400
        connection.executemany(
            "INSERT INTO samples_hour (ts, pv_w, inverter_w, grid_w, battery_w, house_w) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [(day + i * 3600, 2000.0, 2000.0, 0.0, 0.0, 2000.0) for i in range(24)])
        connection.commit()
        totals = history._integrate(day, day + 86400)
        check("hour-tier rows contribute to the energy figures",
              near(totals["house_kwh"], 46.0, 0.5), f"got {totals['house_kwh']!r}")
        check("and count as covering the window",
              totals["coverage"] > 0.9, f"got {totals['coverage']!r}")

    # Counters are written on whatever second the tick falls on, so a window boundary lands
    # between two readings. Restricting to the window before differencing threw away the
    # step that crosses into it -- one sampling interval of energy, every day, downwards.
    with tempfile.TemporaryDirectory() as tmp:
        midnight = t0 - t0 % 86400
        history = build(Path(tmp) / "edge.db", {}, [])
        connection = history._connection
        value = 1000.0
        for ts in range(midnight - 86400 + 137, midnight + 2 * 86400, 300):
            connection.execute("INSERT INTO counters (ts, name, value) VALUES (?, ?, ?)",
                               (ts, "grid_accumulated_energy", round(value, 4)))
            value += 0.05                      # 0.6 kW, steady
        connection.commit()
        reported = asyncio.run(history.counter_delta(midnight, midnight + 86400))
        before = connection.execute(
            "SELECT value FROM counters WHERE ts < ? ORDER BY ts DESC LIMIT 1",
            (midnight,)).fetchone()[0]
        after = connection.execute(
            "SELECT value FROM counters WHERE ts <= ? ORDER BY ts DESC LIMIT 1",
            (midnight + 86400,)).fetchone()[0]
        check("a day reports the whole advance of its counter, boundary included",
              near(reported["grid_accumulated_energy"], after - before, 0.001),
              f"got {reported['grid_accumulated_energy']!r}, counter moved {after - before:.3f}")

    # Two meters measure the grid connection and disagree. Only one of them is billed.
    with tempfile.TemporaryDirectory() as tmp:
        history = build(Path(tmp) / "meters.db", {}, [])
        connection = history._connection
        for i in range(13):
            ts = t0 + i * 300
            for name, base, step in (
                ("grid_accumulated_energy", 500.0, 0.10),   # the inverter's CT clamp
                ("p1_import_energy", 900.0, 0.12),          # the utility's meter, reading higher
                ("grid_exported_energy", 40.0, 0.01),
                ("p1_export_energy", 70.0, 0.03),
                ("accumulated_yield_energy", 4000.0, 0.2),
                ("cumulative_dc_energy_yield_mppt1", 2000.0, 0.2),
            ):
                connection.execute("INSERT INTO counters (ts, name, value) VALUES (?, ?, ?)",
                                   (ts, name, base + step * i))
        connection.commit()
        summary = asyncio.run(history.energy_summary(t0, t0 + 3600))
        check("the billed meter is the one the grid figures use",
              summary.get("metered_by") == "utility meter" and near(summary["grid_import_kwh"], 1.44, 0.01),
              f"got {summary.get('metered_by')!r} / {summary.get('grid_import_kwh')!r}")
        check("the inverter's own figure is kept alongside it",
              near(summary.get("grid_import_inverter_kwh"), 1.20, 0.01),
              f"got {summary.get('grid_import_inverter_kwh')!r}")
        check("and the disagreement between them is reported",
              near(summary.get("grid_import_delta_kwh"), 0.24, 0.01),
              f"got {summary.get('grid_import_delta_kwh')!r}")

    # With no P1 feed the inverter is the only meter there is.
    with tempfile.TemporaryDirectory() as tmp:
        history = build(Path(tmp) / "solo.db", {}, [])
        connection = history._connection
        for i in range(13):
            for name, base, step in (("grid_accumulated_energy", 500.0, 0.10),
                                     ("grid_exported_energy", 40.0, 0.01),
                                     ("accumulated_yield_energy", 4000.0, 0.2)):
                connection.execute("INSERT INTO counters (ts, name, value) VALUES (?, ?, ?)",
                                   (t0 + i * 300, name, base + step * i))
        connection.commit()
        summary = asyncio.run(history.energy_summary(t0, t0 + 3600))
        check("without a utility feed the inverter's meter is used and said to be",
              summary.get("metered_by") == "inverter" and near(summary["grid_import_kwh"], 1.20, 0.01),
              f"got {summary.get('metered_by')!r} / {summary.get('grid_import_kwh')!r}")
        check("and no disagreement is invented", "grid_import_delta_kwh" not in summary)

    # A kilowatt-hour costs more than the energy in it, and the non-energy part is metered
    # per tariff register just as the energy is.
    from solar2000direct.config import PricingConfig

    tariff = PricingConfig(energy_price=0.2450, low_tariff_price=0.2100,
                           network_cost_per_kwh=0.0850, network_cost_low_per_kwh=0.0780)
    check("the delivered price is energy plus everything billed beside it",
          near(tariff.delivered(tariff.energy_price), 0.3300, 0.00001),
          f"got {tariff.delivered(tariff.energy_price)!r}")
    check("the low tariff carries the lower network rate",
          near(tariff.delivered(tariff.low_tariff_price, tariff.network_cost(low=True)), 0.2880, 0.00001),
          f"got {tariff.delivered(tariff.low_tariff_price, tariff.network_cost(low=True))!r}")
    check("an unset low network rate falls back to the day one",
          PricingConfig(energy_price=0.1, network_cost_per_kwh=0.08).network_cost(low=True) == 0.08)
    check("VAT is applied to energy and network together",
          near(PricingConfig(energy_price=0.10, network_cost_per_kwh=0.10, vat_pct=21).delivered(0.10),
               0.242, 0.0001))
    check("with nothing configured the delivered price is just the energy",
          PricingConfig(energy_price=0.2450).delivered(0.2450) == 0.2450)

    # A jump no elapsed time can justify is still rejected rather than reported as energy.
    # Only the one legitimate increment survives: the spike is too large for the elapsed
    # time, and the fall back to a sane value is a negative step, which is skipped too.
    with tempfile.TemporaryDirectory() as tmp:
        jump = {"grid_accumulated_energy": [
            (t0, 1000.0), (t0 + 300, 1000.1), (t0 + 600, 1000.1 + MAX_PLAUSIBLE_KW * 10), (t0 + 900, 1000.3)]}
        history = build(Path(tmp) / "h.db", jump, [])
        deltas = asyncio.run(history.counter_delta(t0, t0 + 3600))
        check("an implausible jump is excluded, not summed",
              near(deltas.get("grid_accumulated_energy"), 0.1, 0.001),
              f"got {deltas.get('grid_accumulated_energy')!r}")

    # Rolling a minute up used to average the signed column, so a minute that charged for
    # thirty seconds and discharged for thirty was stored as a single 0 W row and no query
    # downstream could tell it apart from an idle battery.
    with tempfile.TemporaryDirectory() as tmp:
        minute = t0 - t0 % 60
        history = History(HistoryConfig(enabled=True, path=str(Path(tmp) / "r.db")), State())
        connection = history._connect()
        history._connection = connection
        samples = []
        for m in range(10):
            for i in range(0, 60, 5):
                charging = i < 30
                samples.append((minute + m * 60 + i, 3000.0, 1000.0,
                                -1000.0 if charging else 1000.0,
                                2000.0 if charging else -2000.0))
        connection.executemany(
            "INSERT INTO samples (ts, pv_w, house_w, grid_w, battery_w) VALUES (?, ?, ?, ?, ?)",
            samples,
        )
        history._aggregate_into(MINUTE, FULL, minute + 86400)
        connection.execute("DELETE FROM samples")

        rolled = connection.execute("SELECT * FROM samples_minute ORDER BY ts").fetchone()
        check("the signed average of a rolled minute still cancels to nothing",
              near(rolled["battery_w"], 0.0, 1.0), f"got {rolled['battery_w']!r}")
        check("the charging half is kept alongside it",
              near(rolled["battery_charge_w"], 1000.0, 1.0), f"got {rolled['battery_charge_w']!r}")

        row = asyncio.run(history.series(minute, minute + 600, 10))["rows"][0]
        check("a rolled minute reports its charging power",
              near(row["battery_charge_w"], 1000.0, 1.0), f"got {row['battery_charge_w']!r}")
        check("and its discharging power, derived from the two it stores",
              near(row["battery_discharge_w"], 1000.0, 1.0), f"got {row['battery_discharge_w']!r}")
        check("the grid survives rollup the same way",
              near(row["grid_import_w"], 500.0, 1.0) and near(row["grid_export_w"], 500.0, 1.0),
              f"got {row['grid_import_w']!r} / {row['grid_export_w']!r}")

        buckets = asyncio.run(history.energy_profile(minute, minute + 600, 300))["rows"]
        moved = [b for b in buckets if b["battery_charged_kwh"] or b["battery_discharged_kwh"]]
        check("energy over rolled-up data counts both directions",
              moved and all(b["battery_charged_kwh"] > 0 and b["battery_discharged_kwh"] > 0
                            for b in moved),
              f"got {buckets!r}")

        # Rolling the minutes on into hours must not undo it.
        history._aggregate_into(HOUR, MINUTE, minute + 86400)
        connection.execute("DELETE FROM samples_minute")
        hourly = asyncio.run(history.series(minute, minute + 3600, 1))["rows"][0]
        check("and it survives the second rollup into hours",
              hourly["battery_charge_w"] > 0 and hourly["battery_discharge_w"] > 0,
              f"got {hourly['battery_charge_w']!r} / {hourly['battery_discharge_w']!r}")

    # A database written before those columns existed must gain them without losing data.
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "old.db"
        legacy = sqlite3.connect(path)
        old_columns = ", ".join(f"{column} REAL" for column, _ in SAMPLE_COLUMNS)
        for table in ("samples", "samples_minute", "samples_hour"):
            legacy.execute(f"CREATE TABLE {table} (ts INTEGER PRIMARY KEY, {old_columns})")
        legacy.execute(
            "INSERT INTO samples_minute (ts, battery_w, grid_w) VALUES (?, ?, ?)", (t0, 900.0, -400.0))
        legacy.execute(
            "INSERT INTO samples_minute (ts, battery_w, grid_w) VALUES (?, ?, ?)", (t0 + 60, -700.0, 250.0))
        legacy.commit()
        legacy.close()

        history = History(HistoryConfig(enabled=True, path=str(path)), State())
        connection = history._connect()
        present = {row["name"] for row in connection.execute("PRAGMA table_info(samples_minute)")}
        check("the migration adds the columns", set(ROLLUP_COLUMNS) <= present,
              f"missing {set(ROLLUP_COLUMNS) - present}")
        backfilled = connection.execute(
            "SELECT battery_charge_w, grid_import_w FROM samples_minute ORDER BY ts").fetchall()
        check("a charging row is backfilled to its charging power",
              near(backfilled[0]["battery_charge_w"], 900.0), f"got {backfilled[0]['battery_charge_w']!r}")
        check("a discharging row is backfilled to zero charge, not to its magnitude",
              backfilled[1]["battery_charge_w"] == 0.0, f"got {backfilled[1]['battery_charge_w']!r}")
        history._connection = connection
        migrated = asyncio.run(history.series(t0, t0 + 120, 2))["rows"]
        check("backfilled rows read back as the dominant direction and nothing else",
              near(migrated[0]["battery_charge_w"], 900.0)
              and migrated[0]["battery_discharge_w"] == 0.0
              and migrated[1]["battery_discharge_w"] == 700.0,
              f"got {migrated!r}")
        check("running the migration twice changes nothing",
              (History._migrate(connection) or True)
              and near(connection.execute(
                  "SELECT battery_charge_w FROM samples_minute ORDER BY ts").fetchone()[0], 900.0))

    # --- rolling up must fold whole buckets, not everything older than the cutoff ------
    #
    # The bucket straddling the cutoff used to be written from the rows before it; those
    # rows were then deleted, and the next pass -- cutoff now past the whole bucket --
    # REPLACEd it with an average of the tail alone. Because the cutoff advances by one
    # hour per pass, the front of nearly every permanent hour row was silently dropped.
    print("\nrollup keeps whole buckets")
    with tempfile.TemporaryDirectory() as tmp:
        history = History(HistoryConfig(enabled=True, path=str(Path(tmp) / "roll.db")), State())
        connection = history._connect()
        history._connection = connection
        hour = 3600
        base = (t0 // hour) * hour          # a bucket edge, so the hour is [base, base+3600)
        # 900 W for the first half of the hour, 100 W for the second: the true mean is 500.
        for offset in range(0, hour, 60):
            connection.execute(
                "INSERT INTO samples_minute (ts, pv_w) VALUES (?, ?)",
                (base + offset, 900.0 if offset < hour / 2 else 100.0))
        connection.commit()

        # A cutoff landing mid-bucket: the old code folded and then deleted the first half.
        history._aggregate_into(HOUR, MINUTE, base + hour // 2)
        folded_to = history._aggregate_into(HOUR, MINUTE, base + hour // 2)
        check("a cutoff inside a bucket folds nothing of it", folded_to == base,
              f"folded up to {folded_to}, expected the bucket edge {base}")
        check("and leaves the hour tier untouched",
              connection.execute("SELECT COUNT(*) FROM samples_hour").fetchone()[0] == 0)

        # Once the whole hour is eligible it is folded exactly once, from all its rows.
        folded_to = history._aggregate_into(HOUR, MINUTE, base + hour + 1)
        connection.execute("DELETE FROM samples_minute WHERE ts < ?", (folded_to,))
        rolled = connection.execute("SELECT ts, pv_w FROM samples_hour").fetchall()
        check("a complete bucket folds to the mean of the whole hour",
              len(rolled) == 1 and near(rolled[0][1], 500.0),
              f"got {rolled!r}, expected one row averaging 500.0")
        check("and every row it covered is gone from the source",
              connection.execute("SELECT COUNT(*) FROM samples_minute").fetchone()[0] == 0)

    # --- the tariff split has to describe the window being priced ---------------------
    #
    # It used to be read from the meter's lifetime registers, so a meter whose lifetime
    # import was mostly nocturnal priced a window that ran entirely in daylight at the
    # night mix. Both counters are recorded, so both are differenced over the window.
    print("\ntariff split follows the window")
    with tempfile.TemporaryDirectory() as tmp:
        # Over this window 10 kWh is imported, of which 2 on the low register -- 20%,
        # while the lifetime registers below sit at 80%.
        history = History(HistoryConfig(enabled=True, path=str(Path(tmp) / "tariff.db")), State())
        connection = history._connect()
        history._connection = connection
        for offset, total, low in ((0, 100.0, 40.0), (3600, 110.0, 42.0)):
            for name, value in (("p1_import_energy", total), ("p1_import_energy_low", low)):
                connection.execute(
                    "INSERT INTO counters (ts, name, value) VALUES (?, ?, ?)",
                    (t0 + offset, name, value))
        connection.commit()
        summary = asyncio.run(history.energy_summary(t0, t0 + 3600))
        check("the window's low-tariff import is reported, not the lifetime register",
              near(summary.get("grid_import_low_kwh"), 2.0),
              f"got {summary.get('grid_import_low_kwh')!r}, expected 2.0")
        check("beside the window's total import",
              near(summary.get("grid_import_kwh"), 10.0), f"got {summary.get('grid_import_kwh')!r}")

    print("\n" + ("all checks passed" if not FAILURES else f"FAILED: {', '.join(FAILURES)}"))
    return 0 if not FAILURES else 1


if __name__ == "__main__":
    sys.exit(main())
