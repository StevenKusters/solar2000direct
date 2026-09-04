"""Translate the library's detected device fields into our capability tokens.

Kept separate from the probe and the collector because both need it, and because it is
the single place where "what does this installation have" is decided. Everything
downstream -- which register groups to poll, which MQTT entities to publish, which
dashboard panels to render -- keys off the result, which is what lets one image serve
installations that differ in battery count, meter presence and optimizers.
"""

from __future__ import annotations

import logging

from huawei_solar import SUN2000Device

from solar2000direct.registers import (
    BACKUP_PROBE_REGISTERS,
    CAP_BACKUP,
    CAP_BATTERY_1,
    CAP_BATTERY_2,
    CAP_METER,
    CAP_OPTIMIZERS,
    CAP_THREE_PHASE,
)

_LOGGER = logging.getLogger(__name__)


def capabilities_of(device: SUN2000Device) -> frozenset[str]:
    """Detected capability tokens for a connected inverter.

    ``StorageProductModel.NONE`` is the library's "no battery on this unit" sentinel, so
    presence is a name comparison rather than a truthiness check -- ``NONE`` has value 0
    and would otherwise read as absent-but-also-falsey for a unit that is genuinely there.
    """
    caps: set[str] = set()
    battery_1 = getattr(device, "battery_1_type", None)
    battery_2 = getattr(device, "battery_2_type", None)
    if battery_1 is not None and battery_1.name != "NONE":
        caps.add(CAP_BATTERY_1)
    if battery_2 is not None and battery_2.name != "NONE":
        caps.add(CAP_BATTERY_2)
    if getattr(device, "power_meter_online", False):
        caps.add(CAP_METER)
    if getattr(device, "has_optimizers", False):
        caps.add(CAP_OPTIMIZERS)
    return frozenset(caps)


async def detect_three_phase(device: SUN2000Device) -> bool:
    """Whether this inverter is wired to three phases.

    Decided by measurement because the library does not report it and the model suffix is
    not a reliable guide. An inverter measures grid voltage whenever it is connected, day
    or night, so phase B carries mains on a three-phase machine and a genuine zero on a
    single-phase one.

    Inconclusive means three-phase. A wrong "single-phase" would silently drop two thirds
    of the per-phase detail and there is no way for the reader to tell; a wrong
    "three-phase" shows two rows of zeroes, which is the older behaviour and is visible.
    """
    try:
        phase_a = (await device.client.get("phase_A_voltage")).value
        phase_b = (await device.client.get("phase_B_voltage")).value
    except Exception as err:  # noqa: BLE001 - an unreadable address is data, not failure
        _LOGGER.debug("Phase detection unreadable (%s); assuming three-phase", type(err).__name__)
        return True
    if not isinstance(phase_a, (int, float)) or not isinstance(phase_b, (int, float)):
        return True
    # Only a live phase A makes a zero on B meaningful: with the inverter disconnected
    # both read zero, which says nothing about how it is wired.
    if phase_a < 50:  # noqa: PLR2004 - well under any mains voltage
        return True
    return phase_b >= 50  # noqa: PLR2004


def with_phases(capabilities: frozenset[str], *, three_phase: bool) -> frozenset[str]:
    """Add the three-phase token to an otherwise library-detected capability set."""
    return capabilities | {CAP_THREE_PHASE} if three_phase else capabilities


async def detect_backup(device: SUN2000Device) -> tuple[bool, dict[str, object]]:
    """Probe for a Backup Box, one register at a time.

    The library does not detect this, and which registers exist varies by firmware: on
    V100R001C00SPC162 the obvious `backup_power_state_of_charge` (30373) returns
    IllegalDataAddress on a system that demonstrably *has* a Backup Box, while
    `storage_backup_power_state_of_charge` (47102) returns the configured reserve.

    Reading them individually is the whole point. Batched, the first unimplemented
    address takes the entire read down and a fitted Backup Box reads as absent.
    """
    values: dict[str, object] = {}
    for name in BACKUP_PROBE_REGISTERS:
        try:
            result = await device.client.get(name)
        except Exception as err:  # noqa: BLE001 - an unreadable address is data, not failure
            _LOGGER.debug("Backup register %s unreadable: %s", name, type(err).__name__)
            continue
        values[name] = result.value

    # A configured backup reserve is the positive signal. The off-grid switch reads 0 on
    # a grid-connected system whether or not a Backup Box exists, so it cannot stand alone.
    reserve = values.get("storage_backup_power_state_of_charge")
    present = isinstance(reserve, (int, float)) and reserve > 0
    return present, values


def with_backup(capabilities: frozenset[str], *, present: bool) -> frozenset[str]:
    """Add the backup token to an otherwise library-detected capability set."""
    return capabilities | {CAP_BACKUP} if present else capabilities
