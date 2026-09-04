"""Pull the P1 meter's readings back out of Home Assistant.

The P1 port is USB-attached to the Home Assistant machine, so HA is the only thing that
can read it. What makes it worth fetching is that it measures the same physical quantity
as the Huawei CT-clamp meter, independently -- and it is the fiscal meter, so where the
two disagree, this one is right by definition.

Per-phase is where it earns its keep. A reversed or swapped CT clamp still sums to the
correct total, so only a phase-by-phase comparison can catch one.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

import aiohttp

from solar2000direct.config import HomeAssistantConfig
from solar2000direct.state import State

_LOGGER = logging.getLogger(__name__)

UNAVAILABLE = {"unknown", "unavailable", "none", ""}

# Home Assistant reports power in whatever unit the integration chose. Normalise to watts
# and kilowatt-hours so downstream arithmetic never has to ask.
POWER_TO_WATTS = {"W": 1.0, "kW": 1000.0, "MW": 1_000_000.0}
ENERGY_TO_KWH = {"Wh": 0.001, "kWh": 1.0, "MWh": 1000.0}


def _as_float(state: str | None) -> float | None:
    if state is None or str(state).strip().lower() in UNAVAILABLE:
        return None
    try:
        return float(state)
    except (TypeError, ValueError):
        return None


_WARNED_UNITS: set[str] = set()


def _convert(value: float | None, unit: str | None, table: dict[str, float]) -> float | None:
    if value is None:
        return None
    factor = table.get(unit or "")
    if factor is None:
        # An unrecognised unit is worse than a missing reading: it would silently enter
        # the comparison off by a factor of a thousand. Said once per unit rather than on
        # every poll -- a misconfigured entity would otherwise write a warning every ten
        # seconds for as long as the add-on runs.
        if unit not in _WARNED_UNITS:
            _WARNED_UNITS.add(unit or "")
            _LOGGER.warning(
                "Unrecognised unit %r from Home Assistant; ignoring readings from that entity",
                unit,
            )
        return None
    return value * factor


class HomeAssistantClient:
    """Polls Home Assistant's REST API and folds the P1 readings into state."""

    def __init__(self, config: HomeAssistantConfig, state: State) -> None:
        self.config = config
        self.state = state
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        if not self.config.enabled:
            _LOGGER.info("Home Assistant P1 read-back not configured; skipping")
            return

        headers = {"Authorization": f"Bearer {self.config.token}"}
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
            while not self._stop.is_set():
                try:
                    states = await self._fetch_states(session)
                except Exception as err:  # noqa: BLE001 - HA being down must not stop the collector
                    self.state.p1 = {"error": f"{type(err).__name__}: {err}"}
                    _LOGGER.warning("Could not read Home Assistant states: %s", err)
                else:
                    self.state.p1 = self._interpret(states)

                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=self.config.poll_interval)

    async def _fetch_states(self, session: aiohttp.ClientSession) -> dict[str, dict[str, Any]]:
        """Fetch the entities that were configured, one request each.

        `/api/states` returns the whole state machine, which on a real installation is
        hundreds of entities with all their attributes -- serialised by Home Assistant and
        parsed here every ten seconds, to keep at most eight of them. On a Raspberry Pi
        that is a steady, pointless load on the machine this add-on is a guest of.

        At most eight small requests, issued together, cost less than one large one. A
        missing entity 404s and is simply absent, which is the same outcome the filter
        gave and needs no special case.
        """
        wanted = self.config.entities
        if not wanted:
            return {}

        async def one(entity: str) -> tuple[str, dict[str, Any]] | None:
            try:
                async with session.get(f"{self.config.url}/api/states/{entity}") as response:
                    if response.status == 404:  # noqa: PLR2004 - not configured yet, or renamed
                        return None
                    response.raise_for_status()
                    return entity, await response.json()
            except aiohttp.ClientError:
                return None

        results = await asyncio.gather(*(one(entity) for entity in wanted))
        return {entity: payload for found in results if found for entity, payload in (found,)}

    def _power(self, states: dict[str, dict[str, Any]], entity: str) -> float | None:
        item = states.get(entity)
        if item is None:
            return None
        unit = (item.get("attributes") or {}).get("unit_of_measurement")
        return _convert(_as_float(item.get("state")), unit, POWER_TO_WATTS)

    def _energy_sum(self, states: dict[str, dict[str, Any]], entities: list[str]) -> float | None:
        """Sum a set of energy counters, e.g. the day and night tariff registers."""
        total = 0.0
        seen = False
        for entity in entities:
            item = states.get(entity)
            if item is None:
                continue
            unit = (item.get("attributes") or {}).get("unit_of_measurement")
            value = _convert(_as_float(item.get("state")), unit, ENERGY_TO_KWH)
            if value is not None:
                total += value
                seen = True
        return round(total, 3) if seen else None

    def _interpret(self, states: dict[str, dict[str, Any]]) -> dict[str, Any]:
        config = self.config
        result: dict[str, Any] = {}

        net = self._power(states, config.net_power_entity) if config.net_power_entity else None
        if net is None and config.import_power_entity and config.export_power_entity:
            # Two unsigned halves, which is all some integrations publish. Both are
            # required for the same reason the per-phase pair is: a missing half is
            # unknown, and treating it as zero would report a direction that was never
            # measured.
            imported = self._power(states, config.import_power_entity)
            exported = self._power(states, config.export_power_entity)
            if imported is not None and exported is not None:
                net = imported - exported
        if net is not None:
            result["grid_power_w"] = round(net, 1)

        # Per-phase net power. The comparison against the Huawei meter's per-phase figures
        # is the only thing that can catch a reversed clamp on a single phase.
        phases: dict[str, float] = {}
        # A meter that reports each phase as one signed value needs no pairing, and
        # requiring a pair excluded those meters from the comparison entirely.
        for index, entity in enumerate(config.phase_power_entities, start=1):
            signed = self._power(states, entity)
            if signed is not None:
                phases[f"L{index}"] = round(signed, 1)
        if len(config.phase_import_entities) != len(config.phase_export_entities):
            # zip(strict=False) would quietly drop the tail, and the phases that survived
            # would then be summed and compared against the meter total as though they
            # were all of them -- reported as a meter disagreement rather than as a
            # configuration mistake.
            _LOGGER.warning(
                "P1 per-phase import (%d entities) and export (%d) lists differ in length; "
                "skipping the per-phase comparison until they match",
                len(config.phase_import_entities), len(config.phase_export_entities),
            )
            config_pairs: list[tuple[str, str]] = []
        else:
            config_pairs = list(zip(
                config.phase_import_entities, config.phase_export_entities, strict=True))
        for index, (import_entity, export_entity) in enumerate(config_pairs, start=1):
            imported = self._power(states, import_entity)
            exported = self._power(states, export_entity)
            if imported is None or exported is None:
                # Both halves are required. A P1 meter always reports both directions --
                # the idle one simply reads 0.0 -- so a missing half means the reading
                # failed, and substituting zero would report a phase as idle when it is
                # actually unknown. That is the exact failure this comparison exists to catch.
                continue
            phases[f"L{index}"] = round(imported - exported, 1)
        if phases:
            result["phase_power_w"] = phases
            # A net sensor is preferable, but phases summing to it is a free consistency check.
            if net is not None:
                result["phase_sum_delta_w"] = round(sum(phases.values()) - net, 1)

        imported_energy = self._energy_sum(states, config.import_energy_entities)
        exported_energy = self._energy_sum(states, config.export_energy_entities)
        if imported_energy is not None:
            result["import_energy_kwh"] = imported_energy
        if exported_energy is not None:
            result["export_energy_kwh"] = exported_energy

        low_import = self._energy_sum(states, config.import_energy_low_entities)
        if low_import is not None:
            result["import_energy_low_kwh"] = low_import
            if imported_energy is not None:
                result["import_energy_normal_kwh"] = round(imported_energy - low_import, 3)

        if config.active_tariff_entity:
            item = states.get(config.active_tariff_entity)
            if item is not None and str(item.get("state")).strip().lower() not in UNAVAILABLE:
                result["active_tariff"] = item["state"]

        # A capacity tariff bills on the peak quarter-hour, not on energy, so these are
        # worth tracking separately from everything else the meter reports.
        for key, entity in (("current_demand_kw", config.current_demand_entity),
                            ("peak_demand_kw", config.peak_demand_entity)):
            if not entity:
                continue
            watts = self._power(states, entity)
            if watts is not None:
                result[key] = round(watts / 1000, 3)

        missing = [entity for entity in config.entities if entity not in states]
        if missing:
            result["missing_entities"] = missing
        return result
