"""Register groups, organised by how fast the underlying value actually changes.

A Huawei inverter serves exactly one Modbus client at a time and answers slowly,
so polling every register at one interval spends the bus on values that move once
a day. Each group below carries its own cadence and its own capability requirement;
the scheduler interleaves them and skips whatever the installation does not have.

Register names are the lowercase identifiers used by the ``huawei-solar`` library
(``huawei_solar.registers.REGISTERS``). Every name here is checked against that catalog at
import time by :func:`unknown_register_names`, so a typo -- or a name the library has
renamed out from under us -- is reported in the log at startup rather than turning into a
register that is quietly never read on somebody else's inverter.

Reported, not fatal: which names exist varies between library versions, and refusing to
start over one missing register would be a worse failure than skipping it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from huawei_solar.registers import REGISTERS

_LOGGER = logging.getLogger(__name__)

# Capability tokens. The library detects all of these on connect, so a single
# collector image adapts itself to whatever installation it is pointed at.
CAP_BATTERY_1 = "battery_1"
CAP_BATTERY_2 = "battery_2"
CAP_METER = "meter"
CAP_OPTIMIZERS = "optimizers"
CAP_BACKUP = "backup"
CAP_THREE_PHASE = "three_phase"
# Not a property of the inverter: whether a P1 meter feed is configured to be read
# from Home Assistant. It gates entities the same way, so it lives with the others.
CAP_P1 = "p1"

MAX_PV_STRINGS = 24
"""Most inputs a SUN2000 exposes. Only as many as the inverter reports are ever polled."""

MAX_BATTERY_UNITS = 2
MAX_PACKS_PER_UNIT = 3


@dataclass(frozen=True, slots=True)
class Shape:
    """How many of each repeated thing this installation actually has.

    Capability tokens answer "is there a battery"; this answers "how many strings, how
    many units". Both come off the inverter at startup. Written down separately because
    the counts vary between installations of the same shape -- a 3 kW single-string
    inverter and a 20 kW four-string one carry identical capability tokens.
    """

    pv_strings: int = 2
    battery_units: int = 0

    @classmethod
    def of(cls, device: object, capabilities: frozenset[str]) -> Shape:
        """Read the shape off a connected device, falling back to what is safe."""
        # Trust the count when the inverter gives one -- reporting a string that is not
        # fitted is a reading of zero, indistinguishable from a dead one. Two is the
        # fallback only for firmware that does not implement nb_pv_strings at all, where
        # guessing low would hide half of a real array.
        strings = getattr(device, "pv_string_count", 0) or 0
        units = (CAP_BATTERY_1 in capabilities) + (CAP_BATTERY_2 in capabilities)
        return cls(
            pv_strings=min(MAX_PV_STRINGS, int(strings)) if strings else 2,
            battery_units=units,
        )


@dataclass(frozen=True, slots=True)
class RegisterGroup:
    """A set of registers read together on a shared cadence."""

    name: str
    interval: float
    """Target seconds between reads. The scheduler treats this as a floor, not a promise:
    if the inverter cannot keep up it stretches the interval rather than queueing reads."""

    registers: tuple[str, ...]
    requires: frozenset[str] = field(default_factory=frozenset)
    """Capability tokens that must all be present for this group to be polled."""

    critical: bool = False
    """Critical groups keep their cadence under load; non-critical groups are stretched first."""

    def applicable(self, capabilities: frozenset[str]) -> bool:
        """Whether this group can be read on an installation with these capabilities."""
        return self.requires <= capabilities

    def known_registers(self) -> tuple[str, ...]:
        """Registers this build of huawei-solar actually knows about."""
        return tuple(r for r in self.registers if r in REGISTERS)


def _pack_registers(unit: int, suffixes: tuple[str, ...]) -> tuple[str, ...]:
    """Per-battery-pack registers for the packs of one LUNA2000 storage unit.

    Always all three. Which of them are populated is a property of the installation, not
    of the address map, and an absent pack answers zero rather than failing -- so asking
    is cheap and asking for fewer would mean deciding in advance how big the battery is.
    """
    return tuple(
        f"storage_unit_{unit}_battery_pack_{pack}_{suffix}"
        for pack in range(1, MAX_PACKS_PER_UNIT + 1)
        for suffix in suffixes
    )


def string_registers(count: int, template: str) -> tuple[str, ...]:
    """One register per PV input, named by the library's own numbering."""
    return tuple(template.format(index=i, index02=f"{i:02d}") for i in range(1, count + 1))


# --- Read once, at startup -------------------------------------------------------

IDENTITY = RegisterGroup(
    name="identity",
    interval=0.0,
    registers=(
        "model_name",
        "serial_number",
        "pn",
        "firmware_version",
        "software_version",
        "rated_power",
        "nb_pv_strings",
    ),
)
# `nb_optimizers` is deliberately absent. IDENTITY is the one group with no capability
# gate and no adaptive fallback -- it is read as a single batch before anything else --
# so an inverter that answers IllegalDataAddress on 37200, which the library documents as
# ordinary for models without optimizer support, took the whole session down with it and
# reconnected forever. The library reads the same number under its own error suppression
# and exposes it as `has_optimizers`, which is where the capability check already looks.

# --- Fast: the numbers a live dashboard is actually about ------------------------
#
# `active_power_fast` exists precisely for high-rate polling and is cheaper than the
# regular `active_power`; we read both once during probing to confirm they agree.

FAST_CORE = RegisterGroup(
    name="fast_core",
    interval=1.0,
    critical=True,
    registers=(
        "active_power",
        "input_power",
    ),
)


def fast_strings(shape: Shape) -> RegisterGroup:
    """Per-string voltage and current, one pair per input the inverter reports.

    These sit consecutively from 32016, inside the span the live block already covers, so
    following the real string count costs no extra round-trip -- the reason the previous
    fixed pair was a limit rather than a saving.
    """
    return RegisterGroup(
        name="fast_strings",
        interval=1.0,
        critical=True,
        registers=(
            string_registers(shape.pv_strings, "pv_{index02}_voltage")
            + string_registers(shape.pv_strings, "pv_{index02}_current")
        ),
    )

FAST_METER = RegisterGroup(
    name="fast_meter",
    interval=1.0,
    critical=True,
    requires=frozenset({CAP_METER}),
    registers=(
        "power_meter_active_power",
        "active_grid_frequency",
    ),
)

FAST_BATTERY = RegisterGroup(
    name="fast_battery",
    interval=1.0,
    critical=True,
    requires=frozenset({CAP_BATTERY_1}),
    registers=(
        "storage_charge_discharge_power",
        "storage_state_of_capacity",
    ),
)

# One group per storage unit, each gated on its own capability. Bundling both behind
# CAP_BATTERY_2 meant a single-unit site never polled its only unit: Home Assistant was
# given a "Battery unit 1 level" that stayed unknown for as long as the add-on ran.
FAST_BATTERY_UNIT_1 = RegisterGroup(
    name="fast_battery_unit1",
    interval=2.0,
    requires=frozenset({CAP_BATTERY_1}),
    registers=(
        "storage_unit_1_charge_discharge_power",
        "storage_unit_1_state_of_capacity",
    ),
)

FAST_BATTERY_UNIT_2 = RegisterGroup(
    name="fast_battery_unit2",
    interval=2.0,
    requires=frozenset({CAP_BATTERY_2}),
    registers=(
        "storage_unit_2_charge_discharge_power",
        "storage_unit_2_state_of_capacity",
    ),
)

# --- Medium: electrical detail and health ---------------------------------------

# Split by phase count. A single-phase inverter answers zero on B and C rather than
# failing, which is worse than failing: it looks like a measurement. Publishing "Phase C
# voltage: 0 V" for a phase that does not exist is a reading, and a reader has no way to
# tell it from a fault.
MEDIUM_AC = RegisterGroup(
    name="medium_ac",
    interval=10.0,
    registers=(
        "phase_A_voltage",
        "phase_A_current",
        "internal_temperature",
        "efficiency",
        "insulation_resistance",
        "device_status",
    ),
)

MEDIUM_AC_THREE_PHASE = RegisterGroup(
    name="medium_ac_three_phase",
    interval=10.0,
    requires=frozenset({CAP_THREE_PHASE}),
    registers=(
        "phase_B_voltage",
        "phase_C_voltage",
        "phase_B_current",
        "phase_C_current",
        "line_voltage_A_B",
        "line_voltage_B_C",
        "line_voltage_C_A",
    ),
)

MEDIUM_METER = RegisterGroup(
    name="medium_meter",
    interval=10.0,
    requires=frozenset({CAP_METER}),
    registers=(
        "active_grid_A_power",
        "active_grid_A_current",
        "grid_A_voltage",
        "active_grid_power_factor",
    ),
)

# Ungated, and deliberately so. The meter capability is decided once when the session
# opens, from the library's power_meter_online; a meter still initialising, or on its own
# breaker, or an inverter that was offline at that moment, means the whole session runs
# with no grid power, no house load and none of the entities that rest on them. Reading
# the meter's own status register needs no meter to be present -- it answers OFFLINE --
# so it is the one thing that can tell us the answer has changed. On a site that does
# have a meter it packs into the block the other meter registers are already in.
MEDIUM_METER_STATUS = RegisterGroup(
    name="medium_meter_status",
    interval=30.0,
    registers=("meter_status",),
)

MEDIUM_METER_THREE_PHASE = RegisterGroup(
    name="medium_meter_three_phase",
    interval=10.0,
    requires=frozenset({CAP_METER, CAP_THREE_PHASE}),
    registers=(
        "active_grid_B_power",
        "active_grid_C_power",
        "active_grid_B_current",
        "active_grid_C_current",
        "grid_B_voltage",
        "grid_C_voltage",
    ),
)

# Per-pack health is the reason to do this locally at all: FusionSolar aggregates it
# away, and pack-to-pack drift is how a failing module shows up a year early.
MEDIUM_PACKS_1 = RegisterGroup(
    name="medium_packs_unit1",
    interval=30.0,
    requires=frozenset({CAP_BATTERY_1}),
    registers=_pack_registers(
        1,
        ("state_of_capacity", "charge_discharge_power", "voltage", "current", "maximum_temperature", "minimum_temperature"),
    ),
)

MEDIUM_PACKS_2 = RegisterGroup(
    name="medium_packs_unit2",
    interval=30.0,
    requires=frozenset({CAP_BATTERY_2}),
    registers=_pack_registers(
        2,
        ("state_of_capacity", "charge_discharge_power", "voltage", "current", "maximum_temperature", "minimum_temperature"),
    ),
)

# LFP has a famously flat voltage curve between roughly 20% and 90% state of charge.
# That is what makes it robust, and it is also what makes balancing hard: passive
# balancing only has a usable voltage signal near the top of charge, and the BMS can only
# recalibrate its coulomb count at the voltage knees. A pack that spends a winter parked
# at the discharge floor never reaches either. Huawei tracks this per pack, so we read it.
SOH_CALIBRATION = RegisterGroup(
    name="soh_calibration",
    interval=300.0,
    requires=frozenset({CAP_BATTERY_1}),
    registers=(
        "storage_unit_soh_calibration_status",
        "storage_unit_soh_calibration_release_lower_limit_of_soc",
        "storage_unit_1_battery_pack_1_soh_calibration_status",
        "storage_unit_1_battery_pack_2_soh_calibration_status",
        "storage_unit_1_battery_pack_3_soh_calibration_status",
        "storage_unit_2_battery_pack_1_soh_calibration_status",
        "storage_unit_2_battery_pack_2_soh_calibration_status",
        "storage_unit_2_battery_pack_3_soh_calibration_status",
    ),
)

MEDIUM_BATTERY_HEALTH = RegisterGroup(
    name="medium_battery_health",
    interval=30.0,
    requires=frozenset({CAP_BATTERY_1}),
    registers=(
        "storage_unit_1_battery_temperature",
        "storage_unit_1_bus_voltage",
        "storage_unit_1_bus_current",
        "storage_unit_1_running_status",
        "storage_unit_1_fault_id",
        "storage_maximum_charge_power",
        "storage_maximum_discharge_power",
    ),
)

# The second unit's own health, which was published as an entity without ever being read.
MEDIUM_BATTERY_HEALTH_2 = RegisterGroup(
    name="medium_battery_health2",
    interval=30.0,
    requires=frozenset({CAP_BATTERY_2}),
    registers=(
        "storage_unit_2_battery_temperature",
        "storage_unit_2_running_status",
    ),
)

# --- Slow: energy counters and alarms -------------------------------------------

SLOW_ENERGY = RegisterGroup(
    name="slow_energy",
    interval=60.0,
    registers=(
        "daily_yield_energy",
        "accumulated_yield_energy",
        "alarm_1",
        "alarm_2",
        "alarm_3",
    ),
)


def slow_string_energy(shape: Shape) -> RegisterGroup:
    """Lifetime DC yield per string, one counter per input the inverter reports."""
    return RegisterGroup(
        name="slow_string_energy",
        interval=60.0,
        registers=string_registers(shape.pv_strings, "cumulative_dc_energy_yield_mppt{index}"),
    )

SLOW_GRID_ENERGY = RegisterGroup(
    name="slow_grid_energy",
    interval=60.0,
    requires=frozenset({CAP_METER}),
    registers=(
        "grid_accumulated_energy",
        "grid_exported_energy",
    ),
)

SLOW_BATTERY_ENERGY = RegisterGroup(
    name="slow_battery_energy",
    interval=60.0,
    requires=frozenset({CAP_BATTERY_1}),
    registers=(
        "storage_total_charge",
        "storage_total_discharge",
        "storage_current_day_charge_capacity",
        "storage_current_day_discharge_capacity",
    ),
)

# Which backup registers exist depends on firmware. On a SUN2000-8KTL-M1 running
# V100R001C00SPC162 with a Backup Box fitted, 30373 / 30406 / 47605 return
# IllegalDataAddress while 47102 and 47604 answer normally -- so presence must be
# probed on the registers that actually respond, not the obvious-looking ones.
SLOW_BACKUP = RegisterGroup(
    name="slow_backup",
    interval=60.0,
    requires=frozenset({CAP_BACKUP}),
    registers=(
        "storage_backup_power_state_of_charge",
        "backup_switch_to_off_grid",
    ),
)

BACKUP_PROBE_REGISTERS = (
    "storage_backup_power_state_of_charge",
    "backup_switch_to_off_grid",
    "backup_power_state_of_charge",
    "backup_time_notification_threshold",
    "backup_voltage_independent_operation",
)
"""Every backup register worth trying, most-likely-to-answer first. Probed one at a
time: reading them as a batch lets one unimplemented address mask all the others."""

# Battery configuration: the settings a person changes in the installer app. Read on a
# slow cadence so the dashboard can show what the inverter is actually set to, which is
# also what makes a saved profile a snapshot of reality rather than of an assumption.
BATTERY_CONFIG_REGISTERS: tuple[str, ...] = (
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
    "storage_maximum_charging_power",
    "storage_maximum_discharging_power",
    "storage_rated_capacity",
)

# Read separately: these decode to structured period lists rather than numbers, so they
# cannot ride along in a block read with everything else.
BATTERY_SCHEDULE_REGISTERS: tuple[str, ...] = (
    "storage_huawei_luna2000_time_of_use_charging_and_discharging_periods",
    "storage_capacity_control_periods",
)

CONFIG = RegisterGroup(
    name="config",
    interval=300.0,
    requires=frozenset({CAP_BATTERY_1}),
    registers=BATTERY_CONFIG_REGISTERS,
)

ALL_GROUPS: tuple[RegisterGroup, ...] = (
    FAST_CORE,
    FAST_METER,
    FAST_BATTERY,
    FAST_BATTERY_UNIT_1,
    FAST_BATTERY_UNIT_2,
    MEDIUM_AC,
    MEDIUM_AC_THREE_PHASE,
    MEDIUM_METER,
    MEDIUM_METER_STATUS,
    MEDIUM_METER_THREE_PHASE,
    MEDIUM_PACKS_1,
    MEDIUM_PACKS_2,
    MEDIUM_BATTERY_HEALTH,
    MEDIUM_BATTERY_HEALTH_2,
    SLOW_ENERGY,
    SLOW_GRID_ENERGY,
    SLOW_BATTERY_ENERGY,
    SLOW_BACKUP,
    SOH_CALIBRATION,
    CONFIG,
)


def unknown_register_names() -> dict[str, tuple[str, ...]]:
    """Group name -> registers this build of huawei-solar does not recognise.

    Empty on a healthy install. A non-empty result means the library changed a name
    out from under us, which is worth failing loudly on rather than silently skipping.
    """
    missing = {}
    for group in (IDENTITY, *ALL_GROUPS):
        unknown = tuple(r for r in group.registers if r not in REGISTERS)
        if unknown:
            missing[group.name] = unknown
    return missing


# --- Read planning ---------------------------------------------------------------
#
# Measured on real hardware behind an SDongle: a Modbus round-trip costs ~500-600 ms
# whether it carries 1 register or 120. The bus is latency-bound, not bandwidth-bound.
#
# That inverts the usual design. Splitting registers into semantic groups and polling
# each on its own cadence *maximises* the number of round-trips, which is the only
# thing that actually costs anything. The cheaper design is to sort every register by
# address, pack them into the widest spans a single read allows, and accept that a
# daily energy counter gets refreshed at live-data speed because it happens to sit
# eight registers away from the AC power reading. Reading it costs nothing extra.
#
# `huawei_solar.RegisterAwareModbusClient.get_multiple()` already does exactly one read
# for a list of registers, padding the gaps in its struct format. Its only constraint is
# that no single gap exceeds MAX_BATCHED_REGISTERS_GAP_LIMIT registers. The 64-register /
# 16-gap limits that fragment reads come from `batch_update()`, a policy layer above it,
# which we bypass.

MAX_MODBUS_READ_SPAN = 125
"""Modbus caps a single read-holding-registers request at 125 registers."""

MAX_INTERNAL_GAP = 64
"""`_construct_struct_format` rejects a gap larger than this between two registers."""


def build_read_plan(
    register_names: list[str],
    *,
    max_span: int = MAX_MODBUS_READ_SPAN,
    max_gap: int = MAX_INTERNAL_GAP,
) -> list[list[str]]:
    """Pack registers into the fewest single-read blocks that Modbus will accept.

    Returns a list of blocks, each safe to hand to ``client.get_multiple()`` as one
    round-trip. Registers are sorted by address, as ``get_multiple`` requires.
    """
    known = [name for name in dict.fromkeys(register_names) if name in REGISTERS]
    known.sort(key=lambda name: REGISTERS[name].register)

    blocks: list[list[str]] = []
    current: list[str] = []
    for name in known:
        definition = REGISTERS[name]
        if not current:
            current = [name]
            continue

        previous = REGISTERS[current[-1]]
        span = (definition.register + definition.length) - REGISTERS[current[0]].register
        gap = definition.register - (previous.register + previous.length)
        if span <= max_span and gap <= max_gap:
            current.append(name)
        else:
            blocks.append(current)
            current = [name]

    if current:
        blocks.append(current)
    return blocks


MIN_REGISTERS_PER_LIVE_BLOCK = 8
"""A block carrying fewer registers than this is not worth a live round-trip."""

LIVE_CADENCE_THRESHOLD = 5.0
"""A group wanting readings at least this often counts as live-tier."""


def _register_index(shape: Shape) -> dict[str, tuple[float, bool]]:
    """For each register, the fastest cadence asked of it and whether anyone calls it critical.

    Built from `groups_for` rather than the static tuple, so the generated per-string and
    per-unit groups are visible here too. They were not, which left their registers with no
    cadence at all -- they only stayed in the live tier by riding in a block with something
    else that had one.
    """
    index: dict[str, tuple[float, bool]] = {}
    for group in groups_for(shape):
        if group.interval <= 0:
            continue
        for name in group.registers:
            interval, critical = index.get(name, (float("inf"), False))
            index[name] = (min(interval, group.interval), critical or group.critical)
    return index


def groups_for(shape: Shape = Shape()) -> tuple[RegisterGroup, ...]:
    """Every group for an installation of this shape, fixed and generated alike.

    The generated ones exist because their register list is a function of how many of
    something the site has. Keeping them out of the static tuple is what stops a count
    from the machine it was written against becoming everybody's count.
    """
    return (*ALL_GROUPS, fast_strings(shape), slow_string_energy(shape))


def pollable_register_names(
    capabilities: frozenset[str], shape: Shape = Shape(),
) -> list[str]:
    """Every non-pack register this installation supports.

    Battery packs sit in their own address region far from everything else, so they can
    never ride along in another block and are planned separately.
    """
    pack_groups = {MEDIUM_PACKS_1.name, MEDIUM_PACKS_2.name}
    names: list[str] = []
    for group in groups_for(shape):
        if group.name in pack_groups or not group.applicable(capabilities):
            continue
        names.extend(group.known_registers())
    return names


def split_plan_by_value(
    plan: list[list[str]],
    min_registers: int = MIN_REGISTERS_PER_LIVE_BLOCK,
    shape: Shape = Shape(),
) -> tuple[list[list[str]], list[list[str]]]:
    """Split a read plan into live blocks and slow blocks by how much each read buys.

    Every round-trip costs the same ~1 second, so the question for each block is what
    that second returns. A block carrying 24 registers is worth polling constantly; one
    carrying 2 stranded registers -- an isolated pair of lifetime energy counters, say --
    costs exactly as much and tells you almost nothing new. Demoting those to a slow tier
    is what takes a live pass from 5 round-trips to 3, and it generalises: an installation
    with different hardware gets a different split without anyone re-tuning constants.
    """
    index = _register_index(shape)
    live, slow = [], []
    for block in plan:
        # Two conditions, and both matter. A block must contain something that actually
        # changes fast -- eight calibration statuses that move once a month are not worth a
        # live round-trip however neatly they pack -- and it must carry enough registers to
        # justify the trip. Slow registers sharing an address range with fast ones still
        # ride along for free, which is the whole point of packing by address.
        #
        # Except that the size test is an economy, and a critical register is not subject
        # to it: battery power and state of charge are what the dashboard is about, and on
        # a one-cabinet installation they pack into a block two registers short of the
        # threshold. That demoted them to a sixty-second cadence beside a four-second PV
        # figure, which is not a smaller version of the reading -- everything derived from
        # the two together was wrong for up to a minute on every cloud.
        entries = [index[name] for name in block if name in index]
        wanted = min((interval for interval, _ in entries), default=float("inf"))
        critical = any(flag for _, flag in entries)
        if wanted <= LIVE_CADENCE_THRESHOLD and (critical or len(block) >= min_registers):
            live.append(block)
        else:
            slow.append(block)
    return live, slow


def live_register_names(capabilities: frozenset[str]) -> list[str]:
    """Registers that land in a live-tier block. Kept for the benchmark's A/B comparison."""
    live, _slow = split_plan_by_value(build_read_plan(pollable_register_names(capabilities)))
    return [name for block in live for name in block]


def pack_register_names(capabilities: frozenset[str]) -> list[str]:
    """Per-battery-pack registers for whichever storage units are present."""
    names: list[str] = []
    for group in (MEDIUM_PACKS_1, MEDIUM_PACKS_2):
        if group.applicable(capabilities):
            names.extend(group.known_registers())
    return names


def _warn_about_unknown_registers() -> None:
    """Say once, at import, which configured names this build does not recognise."""
    for group, names in unknown_register_names().items():
        _LOGGER.warning(
            "Register group %r names %d register(s) this build of huawei-solar does not "
            "know: %s. They will be skipped.",
            group, len(names), ", ".join(names),
        )


_warn_about_unknown_registers()
