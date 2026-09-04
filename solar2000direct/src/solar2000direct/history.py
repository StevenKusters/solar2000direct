"""Local high-resolution history, in SQLite.

The point of the whole project is that FusionSolar aggregates to five minutes and loses
everything interesting in between. Storing it locally at poll resolution only helps if
the storage does not itself become the problem, so this keeps three tiers:

* full resolution, one row per live poll, for the recent past;
* one-minute means, for the last few months;
* hourly means, kept indefinitely.

Power is stored as a wide row rather than key/value pairs. At a four-second cadence,
key/value would write a dozen rows per poll -- hundreds of thousands a day -- to record
what fits in one. Energy counters are different: they change slowly, there are few of
them, and which ones exist varies by installation, so those get a narrow table sampled
once a minute.

Counters are also what makes the daily and monthly figures honest. A day's grid import is
the difference between two counter readings, not the integral of a power signal sampled
every few seconds with gaps wherever the bus stalled.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from solar2000direct.config import HistoryConfig
from solar2000direct.registers import MAX_PV_STRINGS
from solar2000direct.state import State

_LOGGER = logging.getLogger(__name__)

# The wide sample row. Every one of these exists on any installation worth graphing,
# which is what lets them share a fixed schema.
SAMPLE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("pv_w", "pv_power_w"),
    ("inverter_w", "inverter_power_w"),
    ("grid_w", "grid_power_w"),
    ("battery_w", "battery_power_w"),
    ("house_w", "house_load_w"),
    ("soc", "storage_state_of_capacity"),
    # Per-string power is deliberately absent. A wide row has to fix its columns, and the
    # number of strings is a property of the installation -- two columns here would have
    # been this inverter's count baked into everyone's schema, three lines below the
    # comment warning against exactly that. Per-string energy comes from the lifetime
    # counters instead, which are stored by name and so follow whatever the site has.
)

# Slow-moving totals. Day, month and year figures are differences between two readings
# of these, which stays correct across restarts and polling gaps.
COUNTER_REGISTERS: tuple[str, ...] = (
    "accumulated_yield_energy",
    "daily_yield_energy",
    # Every input the hardware can have. Only names the inverter actually reports are ever
    # written, so listing all of them costs nothing and stops the count of the machine this
    # was written against deciding how much of anyone else's array gets recorded.
    *(f"cumulative_dc_energy_yield_mppt{index}" for index in range(1, MAX_PV_STRINGS + 1)),
    "grid_accumulated_energy",
    "grid_exported_energy",
    "storage_total_charge",
    "storage_total_discharge",
    "storage_current_day_charge_capacity",
    "storage_current_day_discharge_capacity",
)

# Battery and grid power are stored signed, one column each, because that is what the
# inverter reports. A single sample is only ever going one way, so at full resolution the
# two directions are just max(x, 0) and max(-x, 0) and there is nothing to store.
#
# An average is different. Roll a minute up in which the battery charged for thirty
# seconds and discharged for thirty, and AVG cancels the two against each other into a
# single 0 W row: the direction is gone before anything reads it, and no query downstream
# can get it back. So the coarser tiers carry the mean of the positive part alongside the
# signed mean, and the negative part follows from the two -- mean(charge) - mean(signed)
# is mean(discharge), exactly, because both are means over the same rows.
#
# Storing the third number as well would be redundant, and redundancy that has to be kept
# consistent is a liability rather than a convenience: it measured 4.87 MB against 2.42 MB
# on a steady-state database, to hold a figure already implied by the other two.
DIRECTED_FLOWS: tuple[tuple[str, str, str], ...] = (
    ("battery_w", "battery_charge_w", "battery_discharge_w"),
    ("grid_w", "grid_import_w", "grid_export_w"),
)

ROLLUP_COLUMNS: tuple[str, ...] = tuple(positive for _signed, positive, _negative in DIRECTED_FLOWS)
"""Extra columns on the rolled-up tiers only. See :data:`DIRECTED_FLOWS`."""


def gap_allowance(table: str) -> int:
    """The largest step between consecutive rows of a tier that is not a gap.

    A single global ceiling cannot work across tiers: hourly rows sit 3600 seconds apart by
    construction, so a 300-second rule discarded every one of them and the hour tier could
    never contribute a kilowatt-hour to any energy figure -- which is every window older
    than the minute retention. The ceiling has to be a property of the tier being read.
    """
    if table == HOUR.table:
        return int(HOUR.seconds * 1.5)
    return MAX_SAMPLE_GAP


def flow_columns(table: str) -> str:
    """Both directions of every flow, expressed however this table happens to keep them.

    Lets one query read across tiers that store the same fact in different shapes: the
    full-resolution table splits a signed reading on the spot, the coarser ones have the
    positive part already averaged and separated. `max(x, 0)` yields NULL when x is NULL,
    so a row with no reading says nothing rather than claiming no flow.
    """
    parts = []
    for signed, positive, negative in DIRECTED_FLOWS:
        if table == FULL.table:
            parts.append(f"max({signed}, 0) AS {positive}")
            parts.append(f"max(-{signed}, 0) AS {negative}")
        else:
            parts.append(positive)
            parts.append(f"max({positive} - {signed}, 0) AS {negative}")
    return ", ".join(parts)

# The utility's own meter, read back from Home Assistant, recorded alongside the
# inverter's. They are two devices measuring the same connection and they disagree: over
# one September day the inverter's CT meter reported 9.5 kWh imported where the billing
# meter reported 10.4. Only one of them decides the invoice, so it is worth differencing
# over the same windows as everything else rather than only comparing instantaneous power.
P1_COUNTERS: tuple[tuple[str, str], ...] = (
    ("p1_import_energy", "import_energy_kwh"),
    ("p1_export_energy", "export_energy_kwh"),
    ("p1_import_energy_low", "import_energy_low_kwh"),
)

COUNTER_INTERVAL = 300.0
"""Seconds between writing the energy counters.

They advance slowly and are only ever read as differences over a window, so recording them
every minute buys no accuracy and costs a third of all database writes. Five minutes keeps
the finest chart bucket -- an hour -- resting on twelve readings."""

ROLLUP_INTERVAL = 3600.0

PACK_HISTORY_INTERVAL = 300.0
"""Seconds between writing per-pack readings, independent of how often they are read."""

RETRY_INTERVAL = 60.0
"""Seconds between attempts to open the database when it cannot be opened."""

MAX_SAMPLE_GAP = 300
"""Seconds. A longer gap between samples is treated as missing rather than integrated:
assuming power held constant across a ten-minute outage invents energy that never flowed."""

MAX_PLAUSIBLE_KW = 100.0
"""Ceiling on how fast a counter may legitimately advance, in kW.

Counters do not only go up smoothly. They reset (a replaced meter, a firmware daily
counter rolling at midnight) and they jump (an inverter swapped out, a restore from
backup, two data sources merged). Last-minus-first turns any of those into a single
enormous quantity of energy that never existed, and it is indistinguishable from a real
figure once it reaches a chart.

Summing bounded increments handles both: a reset gives a negative step, which is skipped,
and a jump exceeds what the elapsed time allows, which is also skipped. The bound is
deliberately far above any residential system, so it rejects discontinuities without ever
clipping real production."""

_COLUMN_SQL = ", ".join(f"{column} REAL" for column, _ in SAMPLE_COLUMNS)
_ROLLUP_SQL = ", ".join(f"{column} REAL" for column in ROLLUP_COLUMNS)

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS samples (ts INTEGER PRIMARY KEY, {_COLUMN_SQL});
CREATE TABLE IF NOT EXISTS samples_minute (ts INTEGER PRIMARY KEY, {_COLUMN_SQL}, {_ROLLUP_SQL});
CREATE TABLE IF NOT EXISTS samples_hour (ts INTEGER PRIMARY KEY, {_COLUMN_SQL}, {_ROLLUP_SQL});
CREATE TABLE IF NOT EXISTS counters (
    ts INTEGER NOT NULL, name TEXT NOT NULL, value REAL NOT NULL,
    PRIMARY KEY (name, ts)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS counters_ts ON counters (ts);
CREATE TABLE IF NOT EXISTS optimizer_samples (
    ts INTEGER NOT NULL, address INTEGER NOT NULL,
    power_w REAL, voltage REAL, current REAL, temperature REAL,
    PRIMARY KEY (address, ts)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS optimizer_ts ON optimizer_samples (ts);
CREATE TABLE IF NOT EXISTS battery_pack_samples (
    ts INTEGER NOT NULL, unit INTEGER NOT NULL, pack INTEGER NOT NULL,
    soc REAL, voltage REAL, current REAL, temperature REAL,
    PRIMARY KEY (unit, pack, ts)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS battery_pack_ts ON battery_pack_samples (ts);
"""


@dataclass(frozen=True, slots=True)
class Resolution:
    """One storage tier. Tiers are disjoint in time: rolling up deletes what it folds."""

    table: str
    seconds: int


FULL = Resolution("samples", 1)
MINUTE = Resolution("samples_minute", 60)
HOUR = Resolution("samples_hour", 3600)


class History:
    """Writes samples and counters, rolls them up, and answers range queries."""

    def __init__(self, config: HistoryConfig, state: State, sample_interval: float = 5.0) -> None:
        self.config = config
        self.state = state
        # Match the collector's cadence: sampling slower loses readings, sampling faster
        # just rewrites the same ones.
        self.sample_interval = max(1.0, sample_interval)
        self._stop = asyncio.Event()
        self._connection: sqlite3.Connection | None = None
        self._last_counter_write = 0.0
        self._last_rollup = 0.0
        self._last_recorded_read: float | None = None
        self._last_recorded_optimizer: float | None = None
        self._last_recorded_packs: float | None = None
        self._last_pack_write = 0.0

    def stop(self) -> None:
        self._stop.set()

    # --- lifecycle ---------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        path = Path(self.config.path)
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        # WAL keeps the readers that serve chart queries from blocking the writer.
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.row_factory = sqlite3.Row
        connection.executescript(SCHEMA)
        self._migrate(connection)
        return connection

    @staticmethod
    def _migrate(connection: sqlite3.Connection) -> None:
        """Add the rolled-up direction columns to a database that predates them.

        Backfilled from the signed average rather than left empty. That reproduces exactly
        what those rows already yield today -- the dominant direction, and zero for the
        other -- so nothing a reader has been looking at changes, while everything rolled
        up from here on carries both directions. Leaving them NULL would have been more
        honest about the uncertainty and would also have wiped every historical battery
        figure on the charts, which is a worse answer than the lower bound.
        """
        for table in (MINUTE.table, HOUR.table):
            existing = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
            for signed, positive, _negative in DIRECTED_FLOWS:
                if positive in existing:
                    continue
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {positive} REAL")
                connection.execute(f"UPDATE {table} SET {positive} = max({signed}, 0)")
                _LOGGER.info("Added %s.%s and backfilled it from %s", table, positive, signed)

    async def run(self) -> None:
        if not self.config.enabled:
            _LOGGER.info("History disabled")
            return

        # Opening the database can fail -- a full disk, a permission problem, a file left
        # by a newer version. Doing it outside the loop meant the task simply ended, with
        # the exception going to whoever was awaiting the gather and no line in the log
        # saying history had stopped. Everything else keeps running, so nobody notices
        # until a chart is empty weeks later.
        while self._connection is None and not self._stop.is_set():
            try:
                self._connection = await asyncio.to_thread(self._connect)
            except Exception as err:  # noqa: BLE001 - retry rather than end the task
                _LOGGER.error(  # noqa: TRY400 - the traceback adds nothing here
                    "Cannot open the history database at %s (%s: %s). Retrying in %.0fs; "
                    "everything else keeps running without it.",
                    self.config.path, type(err).__name__, err, RETRY_INTERVAL,
                )
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=RETRY_INTERVAL)
        if self._connection is None:
            return
        _LOGGER.info("History at %s", self.config.path)

        try:
            failures = 0
            while not self._stop.is_set():
                try:
                    await asyncio.to_thread(self._tick)
                except Exception as err:  # noqa: BLE001 - one bad pass must not end the task
                    # Logged, but not once per tick: a database that has gone read-only
                    # would otherwise fill the log at the sample rate. Loud on the first
                    # failure, then progressively quieter.
                    failures += 1
                    if failures & (failures - 1) == 0:  # 1st, 2nd, 4th, 8th, ...
                        _LOGGER.warning(
                            "History write failed (%s: %s). %d failure(s) so far.",
                            type(err).__name__, err, failures,
                        )
                else:
                    if failures:
                        _LOGGER.info("History writing again after %d failure(s).", failures)
                    failures = 0
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=self.sample_interval)
        finally:
            if self._connection is not None:
                with contextlib.suppress(Exception):
                    self._connection.close()

    def _tick(self) -> None:
        """One write pass. Runs in a worker thread; sqlite calls are blocking."""
        if not self.state.stats.connected:
            return

        # Only record readings the collector actually refreshed. During a bus stall the
        # state still holds its last values, and writing those repeatedly would fabricate
        # a flat line that looks like real measurement rather than like missing data.
        last_read = self.state.stats.last_live_read
        if last_read is None or last_read == self._last_recorded_read:
            return
        self._last_recorded_read = last_read

        now = time.time()
        self._write_sample(now)
        self._write_optimizers(now)
        self._write_battery_packs(now)
        if now - self._last_counter_write >= COUNTER_INTERVAL:
            self._write_counters(now)
            self._last_counter_write = now
        if now - self._last_rollup >= ROLLUP_INTERVAL:
            self._roll_up()
            self._last_rollup = now

    # --- writing -----------------------------------------------------------------

    def _write_sample(self, now: float) -> None:
        assert self._connection is not None
        values = self.state.flat()
        row = [values.get(source) for _column, source in SAMPLE_COLUMNS]
        if all(value is None for value in row):
            return
        columns = ", ".join(column for column, _ in SAMPLE_COLUMNS)
        placeholders = ", ".join("?" for _ in SAMPLE_COLUMNS)
        self._connection.execute(
            f"INSERT OR REPLACE INTO samples (ts, {columns}) VALUES (?, {placeholders})",
            [int(now), *row],
        )

    def _write_optimizers(self, now: float) -> None:
        """Record per-panel readings, once per optimizer poll.

        Per-panel data is the reason to read the inverter locally at all: it is what shows
        which individual panel is shaded, soiled or failing, and the cloud portal does not
        expose it. Reads are expensive and infrequent, so each one is written exactly once
        rather than on every tick, which would otherwise duplicate the same reading dozens
        of times between refreshes.
        """
        assert self._connection is not None
        last_read = self.state.stats.last_optimizer_read
        if last_read is None or last_read == self._last_recorded_optimizer:
            return
        self._last_recorded_optimizer = last_read

        rows = [
            (
                int(now), int(address),
                data.get("output_power"), data.get("output_voltage"),
                data.get("output_current"), data.get("temperature"),
            )
            for address, data in self.state.optimizers.items()
        ]
        if rows:
            self._connection.executemany(
                "INSERT OR REPLACE INTO optimizer_samples VALUES (?, ?, ?, ?, ?, ?)", rows,
            )

    def _write_battery_packs(self, now: float) -> None:
        """Record each pack separately, so balance can be trended rather than sampled once."""
        assert self._connection is not None
        last_read = self.state.stats.last_pack_read
        if last_read is None or last_read == self._last_recorded_packs:
            return
        # Pack balance is a slow trend measured over weeks. Recording every read costs a
        # fifth of all writes to resolve a drift that takes months to appear.
        if now - self._last_pack_write < PACK_HISTORY_INTERVAL:
            return
        self._last_recorded_packs = last_read
        self._last_pack_write = now

        rows = []
        for unit in (1, 2):
            for pack in (1, 2, 3):
                prefix = f"storage_unit_{unit}_battery_pack_{pack}"
                voltage = self.state.number(f"{prefix}_voltage")
                if voltage is None or voltage <= 0:
                    continue  # a pack that is not installed reads zero volts
                temps = [
                    self.state.number(f"{prefix}_{bound}_temperature") for bound in ("maximum", "minimum")
                ]
                known = [t for t in temps if t is not None]
                rows.append(
                    (
                        int(now), unit, pack,
                        self.state.number(f"{prefix}_state_of_capacity"),
                        voltage,
                        self.state.number(f"{prefix}_current"),
                        sum(known) / len(known) if known else None,
                    ),
                )
        if rows:
            self._connection.executemany(
                "INSERT OR REPLACE INTO battery_pack_samples VALUES (?, ?, ?, ?, ?, ?, ?)", rows,
            )

    def _write_counters(self, now: float) -> None:
        assert self._connection is not None
        rows = [
            (int(now), name, float(value))
            for name in COUNTER_REGISTERS
            if isinstance(value := self.state.value(name), (int, float)) and not isinstance(value, bool)
        ]
        # The billing meter's counters, where a P1 feed is configured. Same table, same
        # differencing, so every window and bucket query gets them for free.
        rows += [
            (int(now), name, float(value))
            for name, key in P1_COUNTERS
            if isinstance(value := self.state.p1.get(key), (int, float)) and not isinstance(value, bool)
        ]
        if rows:
            self._connection.executemany("INSERT OR REPLACE INTO counters VALUES (?, ?, ?)", rows)

    # --- rollup ------------------------------------------------------------------

    def _roll_up(self) -> None:
        """Fold old full-resolution rows into coarser tiers, then discard them."""
        assert self._connection is not None
        now = int(time.time())
        full_cutoff = now - self.config.retention_full_days * 86400
        minute_cutoff = now - self.config.retention_minute_days * 86400

        # Delete exactly what was folded, which is whole buckets and not everything older
        # than the cutoff: see _aggregate_into for why the difference matters.
        self._connection.execute(
            "DELETE FROM samples WHERE ts < ?", (self._aggregate_into(MINUTE, FULL, full_cutoff),))
        self._connection.execute(
            "DELETE FROM samples_minute WHERE ts < ?",
            (self._aggregate_into(HOUR, MINUTE, minute_cutoff),))

        # Counters are small and are what the long-range energy figures rest on; panel data
        # is sparse and its whole value is the long baseline that makes slow degradation
        # visible. Both are thinned rather than deleted: one reading an hour.
        #
        # "One an hour" has to mean the first row of each hour, not a row landing exactly on
        # the hour. Rows are written on whatever second the tick fell on -- `int(time.time())`
        # -- so `ts % 3600 = 0` is true about once in 3600 rows, and the delete that tested
        # for it removed everything. Measured on 120 days of five-minute counter writes:
        # 25,920 rows older than the cutoff, 0 survivors, about 2,160 intended.
        for table in ("counters", "optimizer_samples", "battery_pack_samples"):
            self._connection.execute(
                f"DELETE FROM {table} WHERE ts < ? AND ts NOT IN ("
                f"  SELECT MIN(ts) FROM {table} WHERE ts < ? GROUP BY ts / 3600)",
                (minute_cutoff, minute_cutoff),
            )

    def _aggregate_into(self, target: Resolution, source: Resolution, cutoff: int) -> int:
        """Fold whole source buckets into the target tier. Returns the boundary folded up to.

        The cutoff is rounded down to a bucket edge, and the caller deletes to the same
        edge, so that a bucket is folded exactly once, when every row it covers is eligible.

        Taking the cutoff literally lost the front of nearly every hour. The bucket
        straddling the cutoff was written from the rows before it, and those rows were then
        deleted; an hour later the cutoff had moved past the whole bucket, so it was folded
        a second time -- from the tail alone -- and INSERT OR REPLACE overwrote the first
        result rather than adding to it. Because the cutoff advances by one hour per pass,
        the same phase repeated, and essentially every row in the permanent hour tier was
        an average of part of its hour while presenting as the whole of it.
        """
        assert self._connection is not None
        cutoff -= cutoff % target.seconds
        names = [column for column, _ in SAMPLE_COLUMNS]
        averages = [f"AVG({column})" for column in names]
        # Averaged separately from the signed column, so that a minute which charged and
        # then discharged is not flattened into the difference between the two. Where the
        # source keeps the positive part already, average that; where it does not, take
        # the positive part of each reading first.
        for signed, positive, _negative in DIRECTED_FLOWS:
            names.append(positive)
            averages.append(
                f"AVG({positive})" if source.table != FULL.table else f"AVG(max({signed}, 0))",
            )
        self._connection.execute(
            f"INSERT OR REPLACE INTO {target.table} (ts, {', '.join(names)}) "
            f"SELECT (ts / {target.seconds}) * {target.seconds}, {', '.join(averages)} "
            f"FROM {source.table} WHERE ts < ? GROUP BY ts / {target.seconds}",
            (cutoff,),
        )
        return cutoff

    # --- reading -----------------------------------------------------------------

    async def series(self, since: int, until: int, points: int = 400) -> dict[str, Any]:
        if self._connection is None:
            return {"bucket_s": None, "rows": []}
        return await asyncio.to_thread(self._series, since, until, points)

    def _series(self, since: int, until: int, points: int) -> dict[str, Any]:
        """Read a time range across every tier at once.

        Rolling up moves rows down a tier and deletes the originals, so the three tables
        are disjoint in time: recent data exists only in `samples`, older data only in
        `samples_minute`, oldest only in `samples_hour`. Querying a single tier chosen by
        span therefore returns nothing whenever the requested window sits in a different
        tier than the span suggests -- a request for the last 24 hours picking the minute
        table finds an empty result, because yesterday has not been rolled up yet.

        Reading the union and bucketing afterwards is both correct and simpler: whatever
        resolution survives for a given moment is what gets used.
        """
        assert self._connection is not None
        bucket = max(1, (until - since) // max(1, points))
        columns = ", ".join(column for column, _ in SAMPLE_COLUMNS)
        # Rounded because an average of a few hundred readings otherwise carries fifteen
        # digits of false precision, and there are twelve columns on four hundred rows.
        parts = [f"ROUND(AVG({column}), 1) AS {column}" for column, _ in SAMPLE_COLUMNS]
        for _signed, positive, negative in DIRECTED_FLOWS:
            parts.append(f"ROUND(AVG({positive}), 1) AS {positive}")
            parts.append(f"ROUND(AVG({negative}), 1) AS {negative}")
        averages = ", ".join(parts)
        # Each tier contributes both directions in its own way, so the bucket average
        # never sees a signed figure to cancel.
        union = " UNION ALL ".join(
            f"SELECT ts, {columns}, {flow_columns(table)} FROM {table} WHERE ts BETWEEN ? AND ?"
            for table in (FULL.table, MINUTE.table, HOUR.table)
        )
        rows = self._connection.execute(
            f"WITH combined AS ({union}) "
            f"SELECT (ts / ?) * ? AS ts, {averages} FROM combined GROUP BY ts / ? ORDER BY ts",
            (since, until, since, until, since, until, bucket, bucket, bucket),
        ).fetchall()
        return {
            "bucket_s": bucket,
            "since": since,
            "until": until,
            "rows": [dict(row) for row in rows],
        }

    async def counter_delta(self, since: int, until: int) -> dict[str, float]:
        if self._connection is None:
            return {}
        return await asyncio.to_thread(self._counter_delta, since, until)

    def _counter_delta(self, since: int, until: int) -> dict[str, float]:
        """How much each counter advanced over a window.

        Computed as the sum of plausible positive increments rather than last minus
        first, so counter resets and discontinuities are excluded instead of being
        reported as energy. See :data:`MAX_PLAUSIBLE_KW`.

        A counter that was read over the window but never moved advances by zero, which is
        a fact and not a gap: filtering those rows away instead made "exported nothing"
        indistinguishable from "no export data", and a caller that requires the figure --
        `energy_summary` needs export before it will report house consumption -- then had
        nothing to work with on any window without export. Counters absent from the window
        are still absent from the result.
        """
        assert self._connection is not None
        # Reach back past the start of the window for one extra reading. LAG has no
        # predecessor for the first row it sees, so restricting to the window first threw
        # away the step that crosses into it: every window lost the energy accumulated
        # between the last reading before it began and the first reading inside it. One
        # sampling interval, every day, always downwards -- measured at exactly that, a
        # 14.40 kWh advance reported as 14.35.
        #
        # The recovered step straddles the boundary, so up to one interval of the previous
        # window's energy lands in this one. Five minutes misattributed beats five minutes
        # discarded, and consecutive windows now tile with no hole between them.
        lookback = int(COUNTER_INTERVAL * 2)
        rows = self._connection.execute(
            "WITH stepped AS ("
            "  SELECT name, ts, "
            "         value - LAG(value) OVER (PARTITION BY name ORDER BY ts) AS step, "
            "         ts - LAG(ts) OVER (PARTITION BY name ORDER BY ts) AS gap "
            "  FROM counters WHERE ts BETWEEN ? AND ?"
            ") "
            "SELECT name, SUM(CASE WHEN step > 0 AND step <= ? * (gap / 3600.0) "
            "                      THEN step ELSE 0 END) AS delta FROM stepped "
            "WHERE gap > 0 AND ts > ? "
            "GROUP BY name",
            (since - lookback, until, MAX_PLAUSIBLE_KW, since),
        ).fetchall()
        return {row["name"]: round(row["delta"], 3) for row in rows if row["delta"] is not None}

    async def stats(self) -> dict[str, Any]:
        if self._connection is None:
            return {"enabled": False}
        return await asyncio.to_thread(self._stats)

    def _stats(self) -> dict[str, Any]:
        assert self._connection is not None
        out: dict[str, Any] = {"enabled": True, "path": self.config.path}
        for table in ("samples", "samples_minute", "samples_hour", "counters"):
            row = self._connection.execute(
                f"SELECT COUNT(*) AS n, MIN(ts) AS oldest, MAX(ts) AS newest FROM {table}",
            ).fetchone()
            out[table] = {"rows": row["n"], "oldest": row["oldest"], "newest": row["newest"]}
        with contextlib.suppress(Exception):
            size = Path(self.config.path).stat().st_size
            out["size_bytes"] = size
        return out


    async def energy_summary(self, since: int, until: int) -> dict[str, Any]:  # noqa: PLR0912, PLR0915
        """Energy accounting over a window, from counter differences.

        This is what the FusionSolar Statistics screen shows, computed from counters
        rather than by integrating power, so a polling gap costs resolution but not
        accuracy.

        The distinction that matters: `accumulated_yield_energy` is total inverter AC
        output, which on a hybrid system *includes battery discharge* -- energy the panels
        produced hours earlier. FusionSolar's "Yield" is PV production only, which is why
        the two disagree. Both are reported here, separately and labelled, because they
        answer different questions: PV yield is what the roof did, inverter output is what
        the house saw.
        """
        deltas = await self.counter_delta(since, until)
        integrated = await asyncio.to_thread(self._integrate, since, until) if self._connection else None

        pv_dc = sum(
            deltas.get(f"cumulative_dc_energy_yield_mppt{index}", 0.0)
            for index in range(1, MAX_PV_STRINGS + 1)
        ) or None
        inverter_out = deltas.get("accumulated_yield_energy")

        # Two meters measure this connection: the inverter's CT clamp and the utility's own.
        # They disagree by a few percent, and only one of them is billed, so where the
        # billing meter is readable it is the one that counts. The inverter's figures are
        # kept alongside, because the difference between them is the only error bar
        # available on either.
        huawei_import = deltas.get("grid_accumulated_energy")
        huawei_export = deltas.get("grid_exported_energy")
        billed_import = deltas.get("p1_import_energy")
        billed_export = deltas.get("p1_export_energy")
        # The low-tariff part of THIS window's import. Pricing needs the split over the
        # window being priced; the lifetime registers in State describe every night since
        # the meter was installed, which is a different number entirely.
        billed_import_low = deltas.get("p1_import_energy_low")
        grid_import = billed_import if billed_import is not None else huawei_import
        grid_export = billed_export if billed_export is not None else huawei_export
        charged = deltas.get("storage_total_charge")
        discharged = deltas.get("storage_total_discharge")

        summary: dict[str, Any] = {
            "since": since,
            "until": until,
            "pv_yield_kwh": round(pv_dc, 2) if pv_dc is not None else None,
            "inverter_output_kwh": inverter_out,
            "grid_import_kwh": grid_import,
            "grid_export_kwh": grid_export,
            "battery_charged_kwh": charged,
            "battery_discharged_kwh": discharged,
            "metered_by": "utility meter" if billed_import is not None else "inverter",
        }
        if billed_import_low is not None:
            summary["grid_import_low_kwh"] = billed_import_low
        if billed_import is not None and huawei_import is not None:
            summary["grid_import_inverter_kwh"] = round(huawei_import, 2)
            summary["grid_import_delta_kwh"] = round(billed_import - huawei_import, 2)
        if billed_export is not None and huawei_export is not None:
            summary["grid_export_inverter_kwh"] = round(huawei_export, 2)

        # House consumption comes from integrating the signed samples, not from a counter.
        # See _integrate: no register reports net inverter AC, and the one-way yield counter
        # overstates consumption by the whole charging energy whenever the battery is
        # charged from the grid.
        house = None
        if integrated is not None and integrated["coverage"] >= 0.8:  # noqa: PLR2004
            house = integrated["house_kwh"]
            summary["sample_coverage_pct"] = round(100 * integrated["coverage"], 1)
            summary["battery_charged_kwh"] = round(integrated["charged_kwh"], 2)
            summary["battery_discharged_kwh"] = round(integrated["discharged_kwh"], 2)

        if house is not None and grid_import is not None and grid_export is not None:
            # A house cannot consume a negative amount. If the counters say otherwise they
            # disagree with each other -- a gap that swallowed one counter's increments but
            # not another's, or a partial period at the very start of collection. Reporting
            # the inconsistency is useful; reporting negative consumption is not, and it
            # would propagate into every percentage and money figure downstream.
            if house >= 0:
                summary["house_consumption_kwh"] = round(house, 2)
                if house > 0:
                    # Measured per sample rather than inferred from the daily totals.
                    # "Everything the grid did not supply" was being labelled solar, but a
                    # third of it here came out of the battery, and the battery is not the
                    # roof -- part of what went in came from the grid overnight.
                    summary["from_solar_kwh"] = round(integrated["solar_to_house_kwh"], 2)
                    summary["from_battery_kwh"] = round(integrated["battery_to_house_kwh"], 2)
                    summary["from_grid_kwh"] = round(integrated["grid_to_house_kwh"], 2)
                    summary["grid_to_battery_kwh"] = round(integrated["grid_to_battery_kwh"], 2)
                    summary["self_sufficiency_pct"] = round(100 * max(0.0, 1 - grid_import / house), 1)
            else:
                summary["inconsistent"] = (
                    f"Counters imply {house:.1f} kWh of house consumption, which is impossible. "
                    "The window probably spans a collection gap."
                )

        production = pv_dc if pv_dc is not None else inverter_out
        if production and grid_export is not None and production > 0:
            # Exporting more than was produced is the same class of inconsistency: it means
            # the export counter advanced over a period the production counter did not cover.
            if grid_export <= production:
                # Production minus export is only self-consumption over a window where the
                # battery ends as it started. Over a single day it counts every kilowatt-
                # hour still sitting in the battery as though the house had burned it, and
                # the conversion loss with it: 26.22 "self-consumed" against a house that
                # used 25.53 in total, 9.08 of which came off the grid.
                #
                # Where the samples can say, production is split into what it actually did.
                if integrated is not None and integrated["coverage"] >= 0.8:  # noqa: PLR2004
                    to_house = integrated["solar_to_house_kwh"]
                    to_battery = integrated["solar_to_battery_kwh"]
                    to_grid = integrated["solar_to_grid_kwh"]
                    summary["solar_to_house_kwh"] = round(to_house, 2)
                    summary["solar_to_battery_kwh"] = round(to_battery, 2)
                    summary["solar_to_grid_kwh"] = round(to_grid, 2)
                    # What the panels made on the DC side and never reached anything on the
                    # AC side. A residual of separately measured quantities, so it is
                    # floored rather than allowed to go negative on a short window.
                    summary["conversion_loss_kwh"] = round(
                        max(0.0, production - to_house - to_battery - to_grid), 2,
                    )
                    summary["self_consumed_kwh"] = round(to_house, 2)
                    summary["self_consumption_pct"] = round(100 * to_house / production, 1)
                else:
                    summary["self_consumed_kwh"] = round(production - grid_export, 2)
                    summary["self_consumption_pct"] = round(100 * (1 - grid_export / production), 1)
            else:
                summary.setdefault(
                    "inconsistent",
                    f"Export ({grid_export:.1f} kWh) exceeds production ({production:.1f} kWh) "
                    "over this window, so the counters do not cover the same period.",
                )
        return summary

    async def energy_buckets(self, since: int, until: int, bucket: str = "day") -> dict[str, Any]:
        """Energy per calendar day or month, for the bar charts.

        Buckets are calendar-aligned in local time rather than fixed-width from an
        arbitrary epoch offset, because "yesterday" and "last month" are what a person
        reading the chart means, and a 30-day window is not a month.
        """
        if self._connection is None:
            return {"bucket": bucket, "rows": []}
        return await asyncio.to_thread(self._energy_buckets, since, until, bucket)

    def _energy_buckets(self, since: int, until: int, bucket: str) -> dict[str, Any]:
        assert self._connection is not None
        fmt = "%Y-%m" if bucket == "month" else "%Y-%m-%d"

        # Same bounded-increment approach as _counter_delta, grouped by calendar period.
        # Counters that reset daily are excluded rather than silently contributing a
        # day's worth of nonsense to a monthly bar.
        monotonic = tuple(
            name for name in COUNTER_REGISTERS
            if not name.startswith(("daily_", "storage_current_day_"))
        )
        placeholders = ", ".join("?" for _ in monotonic)
        rows = self._connection.execute(
            "WITH stepped AS ("
            f"  SELECT name, strftime('{fmt}', ts, 'unixepoch', 'localtime') AS period, "
            "         value - LAG(value) OVER (PARTITION BY name ORDER BY ts) AS step, "
            "         ts - LAG(ts) OVER (PARTITION BY name ORDER BY ts) AS gap "
            f"  FROM counters WHERE ts BETWEEN ? AND ? AND name IN ({placeholders})"
            ") "
            "SELECT period, name, SUM(step) AS delta FROM stepped "
            "WHERE step > 0 AND gap > 0 AND step <= ? * (gap / 3600.0) "
            "GROUP BY period, name ORDER BY period",
            (since, until, *monotonic, MAX_PLAUSIBLE_KW),
        ).fetchall()

        periods: dict[str, dict[str, float]] = {}
        for row in rows:
            if row["delta"] is not None:
                periods.setdefault(row["period"], {})[row["name"]] = round(row["delta"], 3)

        out = []
        for period, values in sorted(periods.items()):
            # A timestamp alongside the label, so a chart drawn from these rows has a time
            # axis to select ranges on rather than only a set of labels.
            try:
                fmt_in = "%Y-%m" if bucket == "month" else "%Y-%m-%d"
                started = int(datetime.strptime(period, fmt_in).astimezone().timestamp())
            except ValueError:
                started = None
            pv = sum(
                values.get(f"cumulative_dc_energy_yield_mppt{i}", 0.0)
                for i in range(1, MAX_PV_STRINGS + 1)
            )
            # A counter this installation does not have is absent, not zero. Defaulting it
            # to zero made a meterless site report grid import and export of exactly 0.00
            # every day -- a measurement it never took -- and a house consumption equal to
            # inverter output, which is only true when there is no grid at all.
            grid_import = values.get("grid_accumulated_energy")
            grid_export = values.get("grid_exported_energy")
            inverter = values.get("accumulated_yield_energy")
            row = {
                "period": period,
                "ts": started,
                "pv_yield_kwh": round(pv, 2),
                "inverter_output_kwh": None if inverter is None else round(inverter, 2),
                "grid_import_kwh": None if grid_import is None else round(grid_import, 2),
                "grid_export_kwh": None if grid_export is None else round(grid_export, 2),
                "battery_charged_kwh": values.get("storage_total_charge"),
                "battery_discharged_kwh": values.get("storage_total_discharge"),
                "house_consumption_kwh": None,
            }
            if None not in (inverter, grid_import, grid_export):
                row["house_consumption_kwh"] = round(inverter + grid_import - grid_export, 2)
            out.append(row)
        return {"bucket": bucket, "since": since, "until": until, "rows": out}


    async def panel_performance(self, since: int, until: int) -> dict[str, Any]:
        if self._connection is None:
            return {"panels": [], "samples": 0}
        return await asyncio.to_thread(self._panel_performance, since, until)

    def _panel_performance(self, since: int, until: int) -> dict[str, Any]:
        """Rank each panel against its siblings over a window.

        Absolute output tells you almost nothing -- it moves with the weather, the season
        and the time of day. What is diagnostic is a panel's output *relative to the panels
        beside it*, which share all of that. A panel steadily at 95% of its neighbours is
        soiled, shaded or failing, and it will look completely normal on any absolute chart.

        Compared against the median rather than the mean: with a dozen-odd panels, one bad
        one drags a mean down and flatters itself in the comparison. The median barely
        moves.

        "Beside it" means on the same string, where the inverter says which string each
        optimizer is on. Panels facing east and panels facing west have genuinely different
        daily totals, and ranking them against one median marks the whole of the lesser
        orientation as underperforming -- an orientation is not a fault.

        Only daylight samples count. At night every panel reads zero, and including those
        dilutes the ratios toward 1.0 -- burying exactly the differences being looked for.
        """
        assert self._connection is not None
        rows = self._connection.execute(
            "SELECT address, "
            "       SUM(power_w) AS total, AVG(power_w) AS mean_power, "
            "       MAX(power_w) AS peak, AVG(temperature) AS mean_temp, COUNT(*) AS n "
            "FROM optimizer_samples WHERE ts BETWEEN ? AND ? AND power_w > 5 "
            "GROUP BY address ORDER BY address",
            (since, until),
        ).fetchall()
        if not rows:
            return {"panels": [], "samples": 0, "since": since, "until": until}

        def median_of(values: list[float]) -> float:
            ordered = sorted(values)
            middle = len(ordered) // 2
            return ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2

        strings = {
            address: info.get("string")
            for address, info in (self.state.optimizer_info or {}).items()
        }
        # One median per string where the wiring is known, one for everything otherwise.
        by_string: dict[Any, list[float]] = {}
        for row in rows:
            by_string.setdefault(strings.get(row["address"]), []).append(row["total"])
        # A string with one or two panels has no useful median of its own; those fall back
        # to the whole array rather than being compared against themselves.
        medians = {
            key: median_of(totals) for key, totals in by_string.items() if len(totals) >= 3  # noqa: PLR2004
        }
        overall = median_of([row["total"] for row in rows])

        panels = []
        for row in rows:
            string = strings.get(row["address"])
            median = medians.get(string, overall)
            ratio = row["total"] / median if median else None
            panels.append(
                {
                    "address": row["address"],
                    "string": string,
                    "position": (self.state.optimizer_info or {}).get(row["address"], {}).get("position"),
                    "relative": round(ratio, 4) if ratio is not None else None,
                    "deviation_pct": round(100 * (ratio - 1), 1) if ratio is not None else None,
                    "mean_power_w": round(row["mean_power"], 1),
                    "peak_power_w": round(row["peak"], 1),
                    "mean_temperature_c": round(row["mean_temp"], 1) if row["mean_temp"] is not None else None,
                    "samples": row["n"],
                },
            )
        worst = min(panels, key=lambda panel: panel["relative"] or 1)
        return {
            "panels": panels,
            "samples": sum(row["n"] for row in rows),
            "median_total": round(overall, 1),
            # Named so the reader knows what each panel was measured against.
            "compared_within_string": bool(medians) and set(medians) != {None},
            "worst": worst["address"],
            "worst_deviation_pct": worst["deviation_pct"],
            "since": since,
            "until": until,
        }


    def _earliest_sample(self, since: int, until: int) -> int | None:
        """Timestamp of the first reading inside a window, across every tier."""
        assert self._connection is not None
        union = " UNION ALL ".join(
            f"SELECT MIN(ts) AS ts FROM {table} WHERE ts BETWEEN ? AND ?"
            for table in (FULL.table, MINUTE.table, HOUR.table)
        )
        row = self._connection.execute(
            f"SELECT MIN(ts) AS ts FROM ({union})",
            (since, until, since, until, since, until),
        ).fetchone()
        return row["ts"] if row and row["ts"] is not None else None

    def _integrate(self, since: int, until: int) -> dict[str, float]:
        """Integrate the signed power samples into energy over a window.

        House consumption cannot come from a counter. The obvious candidate,
        `daily_yield_energy`, works in summer -- it matches the portal to 0.1 kWh -- but it
        is a one-way yield counter, and an inverter charging its battery from the grid is
        absorbing AC, not producing it. On a winter night it reports the discharge and
        ignores the charge, overstating house consumption by the whole charging energy.

        No register reports net inverter AC, so this integrates what the collector already
        stores. Gaps longer than MAX_SAMPLE_GAP are excluded rather than bridged, and the
        fraction of the window actually covered is reported so the caller can judge it.
        """
        assert self._connection is not None
        union = " UNION ALL ".join(
            f"SELECT ts, pv_w, house_w, inverter_w, {gap_allowance(table)} AS max_gap, "
            f"{flow_columns(table)} FROM {table} WHERE ts BETWEEN ? AND ?"
            for table in (FULL.table, MINUTE.table, HOUR.table)
        )
        # Where each watt went, decided sample by sample rather than inferred from daily
        # totals. Over a day the totals cannot say how much of the charging came from the
        # grid and how much from the roof; at any single instant it is not a question --
        # the surplus over house load is what the panels could offer, and anything charged
        # beyond that came from the grid. `solar` is the AC-side figure: inverter output
        # plus whatever the battery was absorbing, since output is net of charging.
        # Inverter output is net of the battery, so adding the battery back gives what the
        # panels were putting out on the AC side. Written from the charge/discharge pair
        # rather than the signed column so it reads the same on every tier.
        solar = "max(inverter_w + battery_charge_w - battery_discharge_w, 0)"
        surplus = f"max({solar} - house_w, 0)"
        flows = (
            f"{solar} AS solar, "
            f"min({solar}, house_w) AS solar_house, "
            f"min({surplus}, battery_charge_w) AS solar_battery, "
            f"max({surplus} - battery_charge_w, 0) AS solar_grid, "
            f"min(battery_discharge_w, max(house_w - {solar}, 0)) AS battery_house, "
            f"max(house_w - {solar} - battery_discharge_w, 0) AS grid_house, "
            f"max(battery_charge_w - {surplus}, 0) AS grid_battery"
        )
        row = self._connection.execute(
            "WITH combined AS (" + union + "), "
            "flowed AS (SELECT ts, pv_w, house_w, max_gap, grid_import_w, grid_export_w, "
            f"       battery_charge_w, battery_discharge_w, {flows} FROM combined), "
            "stepped AS (SELECT *, ts - LAG(ts) OVER (ORDER BY ts) AS gap FROM flowed) "
            "SELECT SUM(pv_w * gap) / 3600000.0 AS pv, "
            "       SUM(house_w * gap) / 3600000.0 AS house, "
            "       SUM(grid_import_w * gap) / 3600000.0 AS grid_in, "
            "       SUM(grid_export_w * gap) / 3600000.0 AS grid_out, "
            "       SUM(battery_charge_w * gap) / 3600000.0 AS charged, "
            "       SUM(battery_discharge_w * gap) / 3600000.0 AS discharged, "
            "       SUM(solar * gap) / 3600000.0 AS solar_ac, "
            "       SUM(solar_house * gap) / 3600000.0 AS solar_house, "
            "       SUM(solar_battery * gap) / 3600000.0 AS solar_battery, "
            "       SUM(solar_grid * gap) / 3600000.0 AS solar_grid, "
            "       SUM(battery_house * gap) / 3600000.0 AS battery_house, "
            "       SUM(grid_house * gap) / 3600000.0 AS grid_house, "
            "       SUM(grid_battery * gap) / 3600000.0 AS grid_battery, "
            "       SUM(gap) AS covered "
            "FROM stepped WHERE gap > 0 AND gap <= max_gap",
            (since, until, since, until, since, until),
        ).fetchone()
        window = max(1, until - since)
        return {
            "pv_kwh": row["pv"] or 0.0,
            "house_kwh": row["house"] or 0.0,
            "grid_import_kwh": row["grid_in"] or 0.0,
            "grid_export_kwh": row["grid_out"] or 0.0,
            "charged_kwh": row["charged"] or 0.0,
            "discharged_kwh": row["discharged"] or 0.0,
            "solar_ac_kwh": row["solar_ac"] or 0.0,
            "solar_to_house_kwh": row["solar_house"] or 0.0,
            "solar_to_battery_kwh": row["solar_battery"] or 0.0,
            "solar_to_grid_kwh": row["solar_grid"] or 0.0,
            "battery_to_house_kwh": row["battery_house"] or 0.0,
            "grid_to_house_kwh": row["grid_house"] or 0.0,
            "grid_to_battery_kwh": row["grid_battery"] or 0.0,
            "coverage": min(1.0, (row["covered"] or 0) / window),
        }

    async def round_trip_efficiency(self, since: int, until: int) -> dict[str, Any]:
        if self._connection is None:
            return {"measurable": False, "reason": "history is disabled"}
        return await asyncio.to_thread(self._round_trip_efficiency, since, until)

    def _round_trip_efficiency(self, since: int, until: int) -> dict[str, Any]:
        """Measure what the battery actually returns per kilowatt-hour put in.

        This is the number that decides whether overnight grid-charging pays: energy is
        bought at the night price and delivered at some efficiency, so the day price must
        beat night divided by that efficiency. Measured two ways.

        The battery figure is discharge over charge, at the battery power register. The
        system figure is an energy balance -- everything in, minus everything out, is loss
        -- which also catches the conversion losses that register never sees, and is the
        one that maps to money. The balance only isolates the battery when PV is
        negligible, so it is flagged unreliable otherwise.
        """
        assert self._connection is not None
        # Measure over history that exists, not over a fixed fortnight. Coverage is the
        # fraction of the window carrying samples, so a fortnight-wide question asked of a
        # four-day-old installation answers "29% covered" and refuses -- correctly, and
        # uselessly, for another ten days. Starting at the first reading asks the same
        # question of the period that can answer it.
        earliest = self._earliest_sample(since, until)
        if earliest is not None and earliest > since:
            since = earliest
        totals = self._integrate(since, until)
        charged = totals["charged_kwh"]

        result: dict[str, Any] = {
            "since": since,
            "until": until,
            "charged_kwh": round(charged, 2),
            "discharged_kwh": round(totals["discharged_kwh"], 2),
            "house_kwh": round(totals["house_kwh"], 2),
            "pv_kwh": round(totals["pv_kwh"], 2),
            "grid_import_kwh": round(totals["grid_import_kwh"], 2),
            "coverage_pct": round(100 * totals["coverage"], 1),
        }

        minimum_window_s = 6 * 3600
        if until - since < minimum_window_s:
            result["measurable"] = False
            hours = max(0, until - since) / 3600
            result["reason"] = (
                f"only {hours:.0f} hours of history so far; a round-trip figure needs at "
                "least one full charge and discharge to measure"
            )
            return result

        minimum_throughput_kwh = 5.0
        if charged < minimum_throughput_kwh:
            result["measurable"] = False
            result["reason"] = "the battery barely cycled over this window"
            return result
        if totals["coverage"] < 0.8:  # noqa: PLR2004
            result["measurable"] = False
            result["reason"] = f"only {totals['coverage']:.0%} of the window has samples"
            return result

        result["battery_round_trip_pct"] = round(100 * totals["discharged_kwh"] / charged, 1)

        result["measurable"] = True

        # The whole-system figure is an energy balance, and house consumption is one of its
        # four terms. Without a grid meter there is no house figure at all -- _integrate
        # returns zero, which the balance reads as a house that consumed nothing, and every
        # kWh of production then looks like a loss. The battery-only figure needs none of
        # this, so it stands on its own.
        if totals["house_kwh"] <= 0:
            result["reliable"] = False
            result["reason"] = (
                "No house consumption is measured here, which needs a grid meter, so only "
                "the battery's own charge-to-discharge ratio can be shown. The whole-system "
                "figure would count all production as loss."
            )
            efficiency = result["battery_round_trip_pct"]
            if efficiency and efficiency > 0:
                result["required_day_night_gap_pct"] = round(100 * (100 / efficiency - 1), 1)
            return result

        losses = (
            totals["pv_kwh"] + totals["grid_import_kwh"] - totals["grid_export_kwh"] - totals["house_kwh"]
        )
        result["losses_kwh"] = round(losses, 2)
        result["system_round_trip_pct"] = round(100 * (1 - losses / charged), 1)

        pv_share = totals["pv_kwh"] / totals["house_kwh"]
        result["pv_share_pct"] = round(100 * pv_share, 1)
        result["reliable"] = pv_share < 0.05  # noqa: PLR2004
        if not result["reliable"]:
            result["reason"] = (
                f"PV supplied {pv_share:.0%} of consumption here. The system figure only isolates "
                "the battery when solar is negligible, so measure it across winter nights."
            )

        efficiency = result["system_round_trip_pct"] if result["reliable"] else result["battery_round_trip_pct"]
        if efficiency and efficiency > 0:
            result["required_day_night_gap_pct"] = round(100 * (100 / efficiency - 1), 1)
        return result


    async def pack_balance(self, since: int, until: int) -> dict[str, Any]:
        if self._connection is None:
            return {"measurable": False, "reason": "history is disabled"}
        return await asyncio.to_thread(self._pack_balance, since, until)

    def _pack_balance(self, since: int, until: int) -> dict[str, Any]:
        """Trend how far the battery packs drift from each other.

        Spread has to be read against the state of charge it was measured at. LFP's
        voltage plateau is flat between roughly 20% and 90%, so the BMS infers charge by
        counting coulombs, and that estimate is at its worst near the bottom. A five-point
        spread at 14% charge may be nothing but drift; the same spread at 90%, where the
        curve steepens and the estimate is anchored, is real imbalance. So results are
        grouped by charge band and never averaged across them.

        Voltage is reported alongside, because it is measured rather than inferred. When
        the two rankings disagree, the SOC estimate is the one to distrust.
        """
        assert self._connection is not None
        rows = self._connection.execute(
            "WITH per_sample AS ("
            "  SELECT ts, AVG(soc) AS mean_soc, MAX(soc) - MIN(soc) AS soc_spread, "
            "         MAX(voltage) - MIN(voltage) AS voltage_spread, COUNT(*) AS packs "
            "  FROM battery_pack_samples WHERE ts BETWEEN ? AND ? AND voltage > 0 "
            "  GROUP BY ts HAVING COUNT(*) > 1"
            ") "
            "SELECT CAST(mean_soc / 20 AS INTEGER) * 20 AS band, COUNT(*) AS samples, "
            "       AVG(soc_spread) AS soc_spread, AVG(voltage_spread) AS voltage_spread, "
            "       AVG(mean_soc) AS mean_soc "
            "FROM per_sample GROUP BY band ORDER BY band",
            (since, until),
        ).fetchall()
        if not rows:
            # The query needs two packs in a sample to have a spread at all, so a
            # single-module battery falls out here with thousands of readings in the table.
            # "No pack readings" is then simply untrue, and it reads as a fault.
            packs = self._connection.execute(
                "SELECT COUNT(DISTINCT unit || '-' || pack) AS n FROM battery_pack_samples "
                "WHERE ts BETWEEN ? AND ? AND voltage > 0",
                (since, until),
            ).fetchone()["n"]
            reason = (
                "this battery has one module, so there is nothing to compare it against"
                if packs == 1
                else "no pack readings in this window"
            )
            return {"measurable": False, "packs": packs, "reason": reason}

        # Which pack sits lowest most often. One pack consistently at the bottom is the
        # signature of a genuinely weak module, as opposed to estimation noise that would
        # move the bottom position around between packs.
        lowest = self._connection.execute(
            "WITH ranked AS ("
            "  SELECT ts, unit, pack, voltage, "
            "         RANK() OVER (PARTITION BY ts ORDER BY voltage) AS position "
            "  FROM battery_pack_samples WHERE ts BETWEEN ? AND ? AND voltage > 0"
            ") "
            "SELECT unit, pack, COUNT(*) AS times_lowest FROM ranked WHERE position = 1 "
            "GROUP BY unit, pack ORDER BY times_lowest DESC",
            (since, until),
        ).fetchall()
        total_lowest = sum(row["times_lowest"] for row in lowest) or 1

        result: dict[str, Any] = {
            "measurable": True,
            "since": since,
            "until": until,
            "bands": [
                {
                    "soc_band": f"{row['band']}-{row['band']+19}%",
                    "mean_soc_pct": round(row["mean_soc"], 1),
                    "samples": row["samples"],
                    "soc_spread_pct": round(row["soc_spread"], 2),
                    "voltage_spread_v": round(row["voltage_spread"], 3),
                }
                for row in rows
            ],
            "lowest_pack_share": [
                {
                    "pack": f"U{row['unit']}P{row['pack']}",
                    "share_pct": round(100 * row["times_lowest"] / total_lowest, 1),
                }
                for row in lowest
            ],
        }

        # Only a high-charge band says anything trustworthy about balance.
        high = [row for row in rows if row["band"] >= 80]  # noqa: PLR2004
        if high:
            worst = max(high, key=lambda r: r["soc_spread"])
            result["balance_at_high_soc_pct"] = round(worst["soc_spread"], 2)
        else:
            result["reason"] = (
                "The battery has not been above 80% charge in this window, so nothing here "
                "distinguishes real imbalance from coulomb-count drift. A full charge would "
                "settle it, and would let the BMS rebalance and recalibrate at the same time."
            )
        return result


    async def energy_profile(self, since: int, until: int, bucket: int = 3600) -> dict[str, Any]:
        if self._connection is None:
            return {"bucket_s": bucket, "rows": []}
        return await asyncio.to_thread(self._energy_profile, since, until, bucket)

    def _energy_profile(self, since: int, until: int, bucket: int) -> dict[str, Any]:
        """Energy per fixed-width bucket, integrated from the power samples.

        Distinct from `energy_buckets`, which differences the meter counters by calendar
        period. Counters are the more accurate source but exist only for the quantities the
        inverter counts, and only at whole days or months. Integrating the samples gives
        arbitrary resolution and covers house consumption, which no counter reports at all.

        Buckets are offset to local time before being cut. Cutting from the UTC epoch is
        harmless for hours in whole-hour timezones, but a daily bucket then runs from
        02:00 to 02:00 in CEST rather than midnight to midnight -- which is not a day, and
        reads as one on a chart.
        """
        assert self._connection is not None
        # The offset is resolved per row, not once for today. Taken from `now`, every
        # summer reading in a winter-time window was bucketed an hour out and vice versa,
        # so the day either side of a clock change was cut in the wrong place -- and the
        # further back the window, the more of it was wrong.
        local = "CAST(strftime('%s', ts, 'unixepoch', 'localtime') AS INTEGER)"
        union = " UNION ALL ".join(
            f"SELECT ts, pv_w, house_w, {gap_allowance(table)} AS max_gap, "
            f"{flow_columns(table)} FROM {table} WHERE ts BETWEEN ? AND ?"
            for table in (FULL.table, MINUTE.table, HOUR.table)
        )
        rows = self._connection.execute(
            "WITH combined AS (" + union + "), "
            "stepped AS ("
            f"  SELECT (({local} / ?) * ? - ({local} - ts)) AS bucket, pv_w, house_w, max_gap, "
            "         grid_import_w, grid_export_w, battery_charge_w, battery_discharge_w, "
            "         ts - LAG(ts) OVER (ORDER BY ts) AS gap FROM combined"
            ") "
            "SELECT bucket, "
            "  SUM(pv_w * gap) / 3600000.0 AS pv, "
            "  SUM(house_w * gap) / 3600000.0 AS house, "
            "  SUM(grid_import_w * gap) / 3600000.0 AS grid_in, "
            "  SUM(grid_export_w * gap) / 3600000.0 AS grid_out, "
            "  SUM(battery_charge_w * gap) / 3600000.0 AS charged, "
            "  SUM(battery_discharge_w * gap) / 3600000.0 AS discharged, "
            "  SUM(gap) AS covered "
            "FROM stepped WHERE gap > 0 AND gap <= max_gap GROUP BY bucket ORDER BY bucket",
            (since, until, since, until, since, until, bucket, bucket),
        ).fetchall()

        return {
            "bucket_s": bucket,
            "since": since,
            "until": until,
            "rows": [
                {
                    "ts": row["bucket"],
                    "pv_kwh": round(row["pv"] or 0.0, 3),
                    "house_kwh": round(row["house"] or 0.0, 3),
                    "grid_import_kwh": round(row["grid_in"] or 0.0, 3),
                    "grid_export_kwh": round(row["grid_out"] or 0.0, 3),
                    "battery_charged_kwh": round(row["charged"] or 0.0, 3),
                    "battery_discharged_kwh": round(row["discharged"] or 0.0, 3),
                    # How much of the bucket actually has samples, so a partial hour at the
                    # start of collection is recognisable rather than looking like a quiet one.
                    "coverage": round(min(1.0, (row["covered"] or 0) / bucket), 3),
                }
                for row in rows
            ],
        }
