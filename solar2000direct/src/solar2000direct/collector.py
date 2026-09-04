"""The one process that talks to the inverter.

A Huawei device serves exactly one Modbus client at a time, so exactly one task here owns
the connection and everything else reads the resulting state. That constraint drives the
whole shape of this module:

* **Tiered scheduling.** Reads are grouped into tiers that each have their own interval.
  A round-trip costs the same regardless of payload, so tiers are defined by how much a
  read buys, not by what the values mean.
* **Adaptive intervals.** Measured round-trip cost roughly doubles while the SDongle is
  uploading to FusionSolar. Rather than queue reads it cannot deliver, a tier that takes
  longer than its interval stretches its own schedule and says so.
* **Polite reconnection.** Losing the connection usually means something else grabbed the
  slot. Exponential backoff gives it room instead of fighting for the bus.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from huawei_solar import SUN2000Device, create_device_instance, create_tcp_client
from huawei_solar.exceptions import ConnectionInterruptedException, HuaweiSolarException

from solar2000direct.blocks import validate_plan
from solar2000direct.capabilities import (
    capabilities_of,
    detect_backup,
    detect_three_phase,
    with_backup,
    with_phases,
)
from solar2000direct.config import Config
from solar2000direct.registers import (
    CAP_METER,
    CAP_OPTIMIZERS,
    IDENTITY,
    Shape,
    build_read_plan,
    pack_register_names,
    pollable_register_names,
    split_plan_by_value,
)
from solar2000direct.state import State, verify_signs

_LOGGER = logging.getLogger(__name__)


class MeterAppeared(Exception):  # noqa: N818 - not an error; the installation changed
    """Raised to end a session whose capability set has gone out of date.

    Not an error: the installation answered differently from how it answered at startup,
    and the read plan and entity list were both derived from the old answer. Unwinding to
    the reconnect loop is how they get rebuilt.
    """

MAX_BACKOFF = 60.0
STRETCH_FACTOR = 1.25
"""How much slack to leave after a tier that overran its interval, so a slow bus degrades
smoothly instead of the scheduler falling permanently behind."""


@dataclass
class Tier:
    """A set of blocks read together on a shared schedule."""

    name: str
    blocks: list[list[str]]
    interval: float
    next_due: float = 0.0
    last_duration: float = 0.0
    overruns: int = 0

    @property
    def register_count(self) -> int:
        return sum(len(block) for block in self.blocks)

    def reschedule(self, now: float) -> None:
        """Set the next due time, stretching if the last pass overran its interval."""
        effective = self.interval
        if self.last_duration > self.interval:
            effective = self.last_duration * STRETCH_FACTOR
            self.overruns += 1
        self.next_due = now + effective


@dataclass
class OptimizerTier:
    """Per-panel data, which travels over the Modbus file extension rather than registers.

    Far more expensive than any register read -- measured at ~6 s -- and it holds the bus
    for the whole duration, so it gets its own tier and a long interval.
    """

    interval: float
    next_due: float = 0.0
    last_duration: float = 0.0
    enabled: bool = True


class Collector:
    """Owns the Modbus session and keeps :class:`State` current."""

    def __init__(
        self,
        config: Config,
        state: State,
        on_device: Callable[[SUN2000Device | None], None] | None = None,
    ) -> None:
        self.config = config
        self.state = state
        # Control shares this connection rather than opening its own: the inverter serves
        # exactly one Modbus client, so a second writer would evict the collector.
        self._on_device = on_device
        self._stop = asyncio.Event()
        self._tiers: list[Tier] = []
        self._optimizers: OptimizerTier | None = None
        self._signs_checked = False

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        """Reconnect forever, running a polling session each time we get the bus."""
        backoff = 1.0
        while not self._stop.is_set():
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except MeterAppeared:
                # Not a failure, so no backoff and no alarming log line: reconnect at once
                # and identify the installation again.
                self.state.stats.connected = False
                self.state.stats.reconnects += 1
                backoff = 1.0
            except Exception as err:  # noqa: BLE001 - any failure means reconnect, not exit
                self.state.stats.connected = False
                self.state.stats.last_error = f"{type(err).__name__}: {err}"
                self.state.stats.reconnects += 1
                _LOGGER.warning(
                    "Session ended (%s: %s). Reconnecting in %.0fs. "
                    "If this repeats, something else is holding the inverter's single Modbus slot.",
                    type(err).__name__,
                    err,
                    backoff,
                )
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)
            else:
                backoff = 1.0

    async def _session(self) -> None:
        """One connected session: identify the device, plan the reads, then poll."""
        inverter = self.config.inverter
        client = create_tcp_client(
            inverter.host,
            inverter.port,
            unit_id=inverter.unit_id,
            timeout=inverter.timeout,
            wait_between_requests=inverter.cooldown,
        )
        await client.connect()
        try:
            device = await create_device_instance(client)
            if not isinstance(device, SUN2000Device):
                msg = f"Expected a SUN2000 at unit {inverter.unit_id}, found {type(device).__name__}"
                raise HuaweiSolarException(msg)

            await self._identify(device)
            await self._plan(device)

            if self._on_device is not None:
                self._on_device(device)
            self.state.stats.connected = True
            self.state.stats.connected_since = time.time()
            self.state.stats.last_error = None
            _LOGGER.info(
                "Connected to %s (%s). Live tier: %d reads / %d registers every %.1fs",
                device.model_name,
                device.serial_number,
                len(self._tiers[0].blocks) if self._tiers else 0,
                self._tiers[0].register_count if self._tiers else 0,
                self.config.polling.live_interval,
            )
            await self._poll_forever(device)
        finally:
            self.state.stats.connected = False
            if self._on_device is not None:
                self._on_device(None)
            with contextlib.suppress(Exception):
                await client.disconnect()

    async def _identify(self, device: SUN2000Device) -> None:
        """Record what this installation is and what it has."""
        capabilities = capabilities_of(device)
        detected, backup_values = await detect_backup(device)
        # Detection keys on a non-zero backup reserve, so an owner who set theirs to 0%
        # reads as having no Backup Box. The override says which, either way.
        forced = self.config.backup_present
        capabilities = with_backup(capabilities, present=detected if forced is None else forced)
        if forced is not None and forced != detected:
            _LOGGER.info(
                "Backup Box taken as %s by configuration; detection said %s",
                "present" if forced else "absent", "present" if detected else "absent",
            )
        capabilities = with_phases(capabilities, three_phase=await detect_three_phase(device))
        self.state.capabilities = capabilities
        # How many of each repeated thing this site has, as opposed to whether it has any.
        # Everything that used to be a fixed count -- strings, storage units -- follows it.
        self.state.shape = Shape.of(device, capabilities)
        # Readings survive a reconnect deliberately, so the dashboard does not blank on
        # every blip. The meter's status must not: it is the one value compared against the
        # capability set to decide whether the session is out of date, and a stale "Normal"
        # from a previous session makes that comparison true forever -- reconnecting, seeing
        # it again, and reconnecting again with no wait, on a site whose meter has just gone
        # offline. It comes back on the first poll of this session or not at all.
        self.state.readings.pop("meter_status", None)

        identity = await device.batch_update(list(IDENTITY.known_registers()))
        self.state.update_registers(identity)
        self.state.device = {
            "model_name": device.model_name,
            "serial_number": device.serial_number,
            "product_number": device.product_number,
            "firmware_version": device.firmware_version,
            "software_version": device.software_version,
            "pv_string_count": device.pv_string_count,
            "battery_1_type": device.battery_1_type.name,
            "battery_2_type": device.battery_2_type.name,
            "power_meter_type": str(device.power_meter_type),
            "supports_capacity_control": device.supports_capacity_control,
            "backup_registers": backup_values,
            "backup_detected": detected,
        }
        _LOGGER.info("Capabilities: %s", sorted(capabilities))

    async def _plan(self, device: SUN2000Device) -> None:
        """Build and validate the read plan against this specific device."""
        capabilities = self.state.capabilities
        polling = self.config.polling

        full_plan = build_read_plan(pollable_register_names(capabilities, self.state.shape))
        live_plan, slow_plan = split_plan_by_value(full_plan, shape=self.state.shape)
        pack_plan = build_read_plan(pack_register_names(capabilities))

        live_blocks, live_bad = await validate_plan(device, live_plan)
        slow_blocks, slow_bad = await validate_plan(device, slow_plan)
        pack_blocks, pack_bad = await validate_plan(device, pack_plan)
        unreadable = [*live_bad, *slow_bad, *pack_bad]
        if unreadable:
            _LOGGER.info("Registers not implemented on this device: %s", ", ".join(sorted(unreadable)))

        now = time.monotonic()
        self._tiers = [
            tier
            for tier in (
                Tier("live", live_blocks, polling.live_interval, next_due=now),
                Tier("slow", slow_blocks, polling.slow_interval, next_due=now),
                Tier("packs", pack_blocks, polling.pack_interval, next_due=now),
            )
            if tier.blocks
        ]

        if not self._tiers:
            # Every block failed validation, so there is nothing to poll. Raising here says
            # which device and what was rejected; letting the empty list reach `min()` in
            # _next_task gave a bare "min() arg is an empty sequence" on a reconnect loop.
            message = (
                f"No register block on {self.config.inverter.host} could be read. "
                f"Unreadable: {', '.join(sorted(unreadable)) or 'nothing reported'}. "
                "Check the unit ID and that Modbus TCP is enabled."
            )
            raise RuntimeError(message)

        self._optimizers = None
        if CAP_OPTIMIZERS in capabilities and polling.optimizer_enabled:
            self._optimizers = OptimizerTier(interval=polling.optimizer_interval, next_due=now)
            # Serial numbers and models are static configuration: read once, not every pass.
            try:
                system_info = await device.get_optimizer_system_information_data()
            except Exception as err:  # noqa: BLE001 - losing panel metadata must not stop polling
                _LOGGER.warning("Could not read optimizer system information: %s", err)
            else:
                self.state.update_optimizer_info(system_info)
                _LOGGER.info("Found %d optimizers", len(system_info))

    async def _poll_forever(self, device: SUN2000Device) -> None:
        """Run whichever tier is due next, forever."""
        while not self._stop.is_set():
            due_at, runner = self._next_task()
            wait = due_at - time.monotonic()
            if wait > 0:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=wait)
                if self._stop.is_set():
                    return
            await runner(device)
            self._check_meter_appeared()

    def _check_meter_appeared(self) -> None:
        """Restart the session if a meter turned up after it started.

        The capability set is decided once, when the session opens. A meter that was still
        initialising then, or on a breaker that was off, or an inverter that happened to be
        offline at that moment, otherwise leaves the whole session with no grid power, no
        house load and none of the entities resting on them -- until something unrelated
        drops the connection. The meter's own status register is polled whatever we think,
        so noticing costs nothing; acting on it means starting over, because the register
        plan and the published entities were both built from the old answer.
        """
        if CAP_METER in self.state.capabilities:
            return
        status = self.state.value("meter_status")
        if str(status) not in ("MeterStatus.NORMAL", "NORMAL", "Normal"):
            return
        _LOGGER.info("Grid meter is online but was not present at startup; reconnecting to pick it up")
        raise MeterAppeared

    def _next_task(self):  # noqa: ANN202 - returns a heterogeneous (float, callable) pair
        candidates: list[tuple[float, object]] = [(tier.next_due, tier) for tier in self._tiers]
        if self._optimizers is not None and self._optimizers.enabled:
            candidates.append((self._optimizers.next_due, self._optimizers))
        due_at, target = min(candidates, key=lambda item: item[0])
        if isinstance(target, Tier):
            return due_at, lambda device: self._run_tier(device, target)
        return due_at, self._run_optimizers

    async def _run_tier(self, device: SUN2000Device, tier: Tier) -> None:
        """Read every block in a tier and fold the values into state."""
        started = time.monotonic()
        collected: dict[str, object] = {}
        for block in tier.blocks:
            try:
                collected.update(await device.client.get_multiple_as_dict(block))
            except ConnectionInterruptedException:
                raise
            except Exception as err:  # noqa: BLE001 - one bad block should not end the session
                self.state.stats.reads_failed += 1
                self.state.stats.last_error = f"{tier.name}: {type(err).__name__}: {err}"
                _LOGGER.debug("Block read failed in tier %s: %s", tier.name, err)
            else:
                self.state.stats.reads_ok += 1

        tier.last_duration = time.monotonic() - started
        if collected:
            self.state.update_registers(collected)
            self._check_signs_once(tier)
            # Only a pass that actually read something counts as a reading. Stamping the
            # time regardless meant a bus where every block was failing still reported an
            # age near zero: the dashboard's own staleness warning, the health endpoint and
            # the history writer all took it as healthy while nothing had arrived for hours.
            self._record_tier_timing(tier)
        tier.reschedule(time.monotonic())

    def _check_signs_once(self, tier: Tier) -> None:
        """Validate Huawei's sign conventions against this site's first live reading.

        The conventions are documented, not guaranteed, and a site that contradicts them
        would otherwise report a confidently inverted house load forever. Cheap to check,
        so check rather than assume -- especially on an installation that is not ours.
        """
        if self._signs_checked or tier.name != "live":
            return
        self._signs_checked = True
        for warning in verify_signs(self.state):
            _LOGGER.warning("Sign convention check: %s", warning)

    def _record_tier_timing(self, tier: Tier) -> None:
        now = time.time()
        if tier.name == "live":
            self.state.stats.last_live_read = now
            self.state.stats.live_cycle_ms = round(tier.last_duration * 1000, 1)
        elif tier.name == "packs":
            self.state.stats.last_pack_read = now

    async def _run_optimizers(self, device: SUN2000Device) -> None:
        """Pull per-panel data. Expensive, and it holds the bus while it runs."""
        assert self._optimizers is not None
        started = time.monotonic()
        try:
            realtime = await device.get_latest_optimizer_history_data()
        except ConnectionInterruptedException:
            raise
        except Exception as err:  # noqa: BLE001
            self.state.stats.reads_failed += 1
            _LOGGER.info("Optimizer read failed: %s", err)
        else:
            self.state.update_optimizers(realtime)
            self.state.stats.last_optimizer_read = time.time()
            self.state.stats.reads_ok += 1

        self._optimizers.last_duration = time.monotonic() - started
        self._optimizers.next_due = time.monotonic() + self._optimizers.interval
