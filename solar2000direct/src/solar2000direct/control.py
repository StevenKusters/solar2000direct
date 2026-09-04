"""Saving and restoring battery configuration profiles.

The seasonal change on this kind of installation is one setting: maximise self-consumption
while the sun is useful, time-of-use with overnight grid charging when it is not. Doing
that through the installer app means retyping a schedule twice a year and hoping it matches
last time.

So rather than encoding schedules in configuration -- which would mean transcribing them by
hand, and being wrong in a way nobody notices until a quarterly bill -- a profile is a
**snapshot of what the inverter is actually set to**. Configure it once in the app, save it
under a name, and restore it later. The tool never invents a setting; it only replays one
it has seen.

Applying is a deliberate act: it needs control explicitly enabled, an installer password,
and a named profile. Nothing here runs on a schedule or reacts to conditions. Every write
is logged and read back to confirm it took.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from huawei_solar import SUN2000Device
from huawei_solar import register_values as rv
from huawei_solar.register_definitions.periods import (
    ChargeFlag,
    HUAWEI_LUNA2000_TimeOfUsePeriod,
    PeakSettingPeriod,
)

from solar2000direct.config import ControlConfig
from solar2000direct.registers import BATTERY_CONFIG_REGISTERS, BATTERY_SCHEDULE_REGISTERS
from solar2000direct.state import State

_LOGGER = logging.getLogger(__name__)

# Applying a profile writes these back. Deliberately a subset of what is read: the rest
# are either read-only or belong to commissioning rather than to a seasonal switch.
WRITABLE_SETTINGS: tuple[str, ...] = (
    "storage_working_mode_settings",
    "storage_maximum_power_of_charge_from_grid",
    "storage_grid_charge_cutoff_state_of_charge",
    "storage_charging_cutoff_capacity",
    "storage_discharging_cutoff_capacity",
    "storage_charge_from_grid_function",
    "storage_excess_pv_energy_use_in_tou",
    "storage_backup_power_state_of_charge",
    "storage_capacity_control_mode",
    "storage_capacity_control_soc_peak_shaving",
)

WRITABLE_SCHEDULES: tuple[str, ...] = (
    "storage_huawei_luna2000_time_of_use_charging_and_discharging_periods",
    "storage_capacity_control_periods",
)

_PERIOD_TYPES = {
    "HUAWEI_LUNA2000_TimeOfUsePeriod": HUAWEI_LUNA2000_TimeOfUsePeriod,
    "PeakSettingPeriod": PeakSettingPeriod,
}


class ControlError(RuntimeError):
    """Raised when a control operation cannot be carried out."""


def encode(value: Any) -> Any:  # noqa: ANN401
    """Make a register value JSON-serialisable without losing what it is.

    Enums keep their name rather than their number, and period dataclasses record their
    type, so a profile written today still decodes if the library renumbers something.
    """
    if isinstance(value, Enum):
        return {"__enum__": type(value).__name__, "name": value.name}
    if is_dataclass(value) and not isinstance(value, type):
        return {"__type__": type(value).__name__, "fields": {k: encode(v) for k, v in asdict(value).items()}}
    if isinstance(value, (list, tuple)):
        return [encode(item) for item in value]
    return value


def decode(value: Any) -> Any:  # noqa: ANN401
    """Reverse :func:`encode`."""
    if isinstance(value, list):
        return [decode(item) for item in value]
    if not isinstance(value, dict):
        return value
    if "__enum__" in value:
        return _resolve_enum(value["__enum__"], value["name"])
    if "__type__" in value:
        cls = _PERIOD_TYPES.get(value["__type__"])
        if cls is None:
            msg = f"Unknown period type in profile: {value['__type__']}"
            raise ControlError(msg)
        fields = {k: decode(v) for k, v in value["fields"].items()}
        if "days_effective" in fields:
            fields["days_effective"] = tuple(fields["days_effective"])
        if "charge_flag" in fields and not isinstance(fields["charge_flag"], ChargeFlag):
            fields["charge_flag"] = ChargeFlag[str(fields["charge_flag"])]
        return cls(**fields)
    return value


def _resolve_enum(enum_name: str, member: str) -> Any:  # noqa: ANN401
    """Turn a stored enum back into the enum itself.

    Returning the bare name instead would compare unequal against a freshly-read value and
    show as a spurious difference -- and worse, applying it would hand a string to a
    register expecting a number. Every enum the register map uses lives in
    ``register_values``, apart from ``ChargeFlag``, which belongs to the period definitions.
    """
    candidate = ChargeFlag if enum_name == "ChargeFlag" else getattr(rv, enum_name, None)
    if candidate is None:
        msg = f"Profile refers to an unknown value type {enum_name!r}"
        raise ControlError(msg)
    try:
        return candidate[member]
    except KeyError as err:
        msg = f"Profile refers to {enum_name}.{member}, which this library version does not define"
        raise ControlError(msg) from err


def describe_schedule(periods: Any) -> list[str]:  # noqa: ANN401
    """Render a schedule the way the installer app shows it."""
    if not isinstance(periods, (list, tuple)):
        return []
    lines = []
    for period in periods:
        start = getattr(period, "start_time", None)
        end = getattr(period, "end_time", None)
        if start is None or end is None:
            continue
        action = getattr(period, "charge_flag", None)
        label = action.name.replace("_", " ").title() if isinstance(action, Enum) else (
            f"{getattr(period, 'power', '')} W" if hasattr(period, "power") else ""
        )
        lines.append(f"{start // 60:02d}:{start % 60:02d}-{end // 60:02d}:{end % 60:02d} {label}".strip())
    return lines


class ControlManager:
    """Reads the inverter's configuration, and saves or restores it under a name."""

    def __init__(self, config: ControlConfig, state: State, profiles_path: str) -> None:
        self.config = config
        self.state = state
        self.profiles_path = Path(profiles_path)
        self._device: SUN2000Device | None = None
        self._lock = asyncio.Lock()

    def attach(self, device: SUN2000Device | None) -> None:
        """Called by the collector. Control shares its connection rather than opening a
        second one, because the inverter serves exactly one Modbus client."""
        self._device = device

    # --- profiles ----------------------------------------------------------------

    def _load(self) -> dict[str, Any]:
        try:
            return json.loads(self.profiles_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError) as err:
            _LOGGER.warning("Could not read saved profiles: %s", err)
            return {}

    def _store(self, profiles: dict[str, Any]) -> None:
        self.profiles_path.parent.mkdir(parents=True, exist_ok=True)
        # Written via a temporary file: a half-written profiles file would be worse than
        # none, because it silently loses the settings someone is relying on restoring.
        temporary = self.profiles_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(profiles, indent=2), encoding="utf-8")
        temporary.replace(self.profiles_path)

    def profiles(self) -> dict[str, Any]:
        return self._load()

    # --- reading ------------------------------------------------------------------

    async def read_configuration(self) -> dict[str, Any]:
        """Everything a profile covers, read fresh from the inverter."""
        device = self._device
        if device is None:
            raise ControlError("Not connected to the inverter")

        values: dict[str, Any] = {}
        async with device.update_lock:
            for name in (*BATTERY_CONFIG_REGISTERS, *BATTERY_SCHEDULE_REGISTERS):
                try:
                    result = await device.client.get(name)
                except Exception as err:  # noqa: BLE001 - an unreadable setting is reportable
                    _LOGGER.debug("Configuration register %s unreadable: %s", name, err)
                    continue
                values[name] = result.value
        return values

    async def snapshot(self, name: str, note: str = "") -> dict[str, Any]:
        """Save the inverter's current configuration under a name.

        Deliberately not behind the write gate: this reads the inverter and writes a file
        here. `control_enabled` and the installer password guard changes to the inverter,
        and saving makes none -- requiring them would mean nobody could record what their
        system is set to without first granting permission to change it.
        """
        configuration = await self.read_configuration()
        if not configuration:
            raise ControlError("Read no configuration from the inverter; refusing to save an empty profile")

        profiles = self._load()
        profiles[name] = {
            "note": note,
            "settings": {k: encode(v) for k, v in configuration.items() if k in WRITABLE_SETTINGS},
            "schedules": {k: encode(v) for k, v in configuration.items() if k in WRITABLE_SCHEDULES},
        }
        self._store(profiles)
        _LOGGER.info("Saved profile %r from the inverter's current configuration", name)
        return profiles[name]

    async def compare(self, name: str) -> dict[str, Any]:
        """What applying a profile would change. Reads the inverter; writes nothing."""
        profiles = self._load()
        if name not in profiles:
            raise ControlError(f"No profile named {name!r}")

        current = await self.read_configuration()
        profile = profiles[name]
        changes = []
        wanted_all = {**profile["settings"], **profile["schedules"]}
        compared = len(wanted_all)
        for register, stored in wanted_all.items():
            wanted = decode(stored)
            present = current.get(register)
            if _differs(present, wanted):
                changes.append(
                    {
                        "register": register,
                        "current": _readable(present),
                        "wanted": _readable(wanted),
                    },
                )
        # Counted over the settings actually compared, not over the stored ones. A profile
        # holding a register this inverter no longer reports produced more changes than
        # settings, and the difference went negative.
        return {"profile": name, "changes": changes, "unchanged": max(0, compared - len(changes))}

    # --- writing ------------------------------------------------------------------

    async def apply(self, name: str, *, force: bool = False) -> dict[str, Any]:
        """Write a saved profile back to the inverter.

        Only settings that actually differ are written. Every write to a grid-connected
        inverter is a small risk and a flash operation, and re-writing ten values that
        already hold the wanted number buys nothing. ``force`` writes everything, for the
        case where a setting reads back plausibly but is not really in effect.

        Requires control to be explicitly enabled with an installer password. Each write is
        read back: a write the inverter silently ignored -- which it does for settings that
        are invalid in the current mode -- would otherwise be reported as success.
        """
        if not self.config.available:
            raise ControlError(
                "Control is disabled. Set control_enabled and an installer password to allow writes.",
            )
        device = self._device
        if device is None:
            raise ControlError("Not connected to the inverter")

        profiles = self._load()
        if name not in profiles:
            raise ControlError(f"No profile named {name!r}")
        profile = profiles[name]

        wanted_values = {
            register: decode(stored)
            for register, stored in {**profile["settings"], **profile["schedules"]}.items()
        }
        current = await self.read_configuration()
        pending = {
            register: value
            for register, value in wanted_values.items()
            if force or _differs(current.get(register), value)
        }
        skipped = [register for register in wanted_values if register not in pending]
        if not pending:
            _LOGGER.info("Profile %r already applied; nothing to write", name)
            return {"profile": name, "applied": [], "failed": [], "skipped": skipped, "ok": True}

        async with self._lock:
            applied: list[dict[str, Any]] = []
            failed: list[dict[str, Any]] = []
            async with device.update_lock:
                # The library raises on a bad password rather than returning False, so the
                # falsy branch this used to test was unreachable and a wrong password
                # surfaced as an unhandled exception and an HTTP 500.
                try:
                    await device.login(self.config.username, self.config.password or "")
                except Exception as err:
                    raise ControlError(
                        "The inverter rejected the installer login. This is the local "
                        "commissioning password, not the FusionSolar account.",
                    ) from err
                _LOGGER.info("Applying profile %r", name)

                # Working mode first: several other settings are only accepted once the
                # inverter is in the mode they belong to.
                ordered = sorted(
                    pending.items(),
                    key=lambda item: 0 if item[0] == "storage_working_mode_settings" else 1,
                )
                for register, wanted in ordered:
                    try:
                        await device.set(register, wanted)
                        await asyncio.sleep(0.2)
                        confirmed = (await device.client.get(register)).value
                    except Exception as err:  # noqa: BLE001 - one rejected setting is not fatal
                        failed.append({"register": register, "error": f"{type(err).__name__}: {err}"})
                        _LOGGER.warning("Could not write %s: %s", register, err)
                        continue

                    if _differs(confirmed, wanted):
                        failed.append(
                            {
                                "register": register,
                                "error": "written but read back different",
                                "wanted": _readable(wanted),
                                "read_back": _readable(confirmed),
                            },
                        )
                        _LOGGER.warning("Wrote %s but it read back as %r", register, confirmed)
                    else:
                        applied.append({"register": register, "value": _readable(wanted)})
                        _LOGGER.info("Set %s = %r", register, wanted)

        with contextlib.suppress(Exception):
            device.stop_heartbeat()
        return {"profile": name, "applied": applied, "failed": failed, "skipped": skipped, "ok": not failed}


def _readable(value: Any) -> Any:  # noqa: ANN401
    if isinstance(value, Enum):
        return value.name
    if isinstance(value, (list, tuple)):
        return describe_schedule(value) or [_readable(item) for item in value]
    return value


def _differs(present: Any, wanted: Any) -> bool:  # noqa: ANN401
    """Whether two register values disagree, comparing schedules by content."""
    if isinstance(present, (list, tuple)) or isinstance(wanted, (list, tuple)):
        return list(present or []) != list(wanted or [])
    return present != wanted
