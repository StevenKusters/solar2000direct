"""Configuration, read from environment variables.

Environment rather than a config file because the deliverable is a container that other
installers can point at their own hardware: `docker run -e S2D_INVERTER_HOST=...` needs no
volume mount and no file to get wrong. Every setting has a default that works for a
single-inverter site, so the minimum viable configuration is one variable.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(f"S2D_{name}")
    return value if value not in (None, "") else default


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    return int(raw) if raw is not None else default


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    return float(raw) if raw is not None else default


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_list(name: str) -> list[str]:
    raw = _env(name)
    return [item.strip() for item in raw.split(",") if item.strip()] if raw else []


@dataclass(frozen=True, slots=True)
class InverterConfig:
    host: str
    port: int = 502
    unit_id: int = 1
    timeout: int = 10
    cooldown: float = 0.05
    """Seconds between Modbus requests. Raise if the device drops the connection."""

    @classmethod
    def from_env(cls) -> InverterConfig:
        host = _env("INVERTER_HOST")
        if not host:
            msg = "S2D_INVERTER_HOST is required (the inverter or SDongle IP address)"
            raise ValueError(msg)
        return cls(
            host=host,
            port=_env_int("INVERTER_PORT", 502),
            unit_id=_env_int("INVERTER_UNIT_ID", 1),
            timeout=_env_int("INVERTER_TIMEOUT", 10),
            cooldown=_env_float("INVERTER_COOLDOWN", 0.05),
        )


@dataclass(frozen=True, slots=True)
class PollingConfig:
    """Target intervals. The scheduler stretches these when the bus cannot keep up
    rather than queueing reads, so they are floors and not guarantees."""

    live_interval: float = 4.0
    """Measured on real hardware: a live pass is 3 round-trips at ~1.06 s each in daylight,
    faster after dark. 4 s leaves room for the slow and pack tiers to interleave."""

    slow_interval: float = 60.0
    pack_interval: float = 300.0
    """Per-pack readings change over weeks, and history records them every five minutes.
    Reading them more often than they are recorded spends bus time for nothing."""
    optimizer_interval: float = 300.0
    optimizer_enabled: bool = True

    @classmethod
    def from_env(cls) -> PollingConfig:
        return cls(
            live_interval=_env_float("LIVE_INTERVAL", 4.0),
            slow_interval=_env_float("SLOW_INTERVAL", 60.0),
            pack_interval=_env_float("PACK_INTERVAL", 300.0),
            optimizer_interval=_env_float("OPTIMIZER_INTERVAL", 300.0),
            optimizer_enabled=_env_bool("OPTIMIZER_ENABLED", default=True),
        )


@dataclass(frozen=True, slots=True)
class MqttConfig:
    enabled: bool = False
    host: str = ""
    port: int = 1883
    username: str | None = None
    password: str | None = None
    base_topic: str = "solar2000direct"
    discovery_prefix: str = "homeassistant"
    """Home Assistant's MQTT discovery prefix. Only change it if HA's is non-default."""

    @classmethod
    def from_env(cls) -> MqttConfig:
        host = _env("MQTT_HOST", "")
        return cls(
            enabled=_env_bool("MQTT_ENABLED", default=bool(host)),
            host=host or "",
            port=_env_int("MQTT_PORT", 1883),
            username=_env("MQTT_USERNAME"),
            password=_env("MQTT_PASSWORD"),
            base_topic=_env("MQTT_BASE_TOPIC", "solar2000direct") or "solar2000direct",
            discovery_prefix=_env("MQTT_DISCOVERY_PREFIX", "homeassistant") or "homeassistant",
        )


@dataclass(frozen=True, slots=True)
class HomeAssistantConfig:
    """Read-back from Home Assistant, for the P1 meter it already owns.

    The P1 port is USB-attached to the HA machine, so HA is the only thing that can read
    it. Pulling those states back gives a second, independent measurement of the same
    physical quantity as the Huawei CT-clamp meter -- and, per phase, the only way to
    catch a reversed or swapped clamp, which nets out correctly in the totals.
    """

    url: str = ""
    token: str = ""
    poll_interval: float = 10.0

    net_power_entity: str = ""
    """Single sensor giving net grid power, positive = importing."""

    import_power_entity: str = ""
    export_power_entity: str = ""
    """Separate unsigned sensors, for integrations that publish no net figure at all.

    Home Assistant's DSMR integration is the common one: power_consumption and
    power_production, never a signed total. Those installers could configure nothing here,
    so the whole cross-check -- the reason this module exists -- was unavailable to them."""

    phase_import_entities: list[str] = field(default_factory=list)
    phase_export_entities: list[str] = field(default_factory=list)
    """Three entities each, in L1/L2/L3 order, for per-phase clamp validation."""

    phase_power_entities: list[str] = field(default_factory=list)
    """Three signed per-phase sensors, for meters that report a phase as one value.

    HomeWizard's P1 publishes active_power_l1_w and siblings, already signed. Requiring an
    import/export pair is right where a pair is what exists -- substituting zero for a
    missing half would report a phase as idle when it is unknown -- but it excluded every
    meter that reports the phase directly."""

    import_energy_entities: list[str] = field(default_factory=list)
    export_energy_entities: list[str] = field(default_factory=list)
    """Summed. Split across tariff registers on a dual-tariff meter."""

    import_energy_low_entities: list[str] = field(default_factory=list)
    """The subset of import counters that accumulate during the low tariff. Day import is
    the remainder, so a dual-tariff meter needs no separate day list."""

    active_tariff_entity: str = ""
    current_demand_entity: str = ""
    peak_demand_entity: str = ""
    """Quarter-hour average demand and the month's maximum, which a capacity tariff bills
    on. Belgian digital meters report both directly in the P1 telegram."""

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.token and self.entities)

    @property
    def entities(self) -> list[str]:
        """Every entity this configuration refers to, deduplicated."""
        names = [
            *([self.net_power_entity] if self.net_power_entity else []),
            *([self.import_power_entity] if self.import_power_entity else []),
            *([self.export_power_entity] if self.export_power_entity else []),
            *self.phase_import_entities,
            *self.phase_export_entities,
            *self.phase_power_entities,
            *self.import_energy_entities,
            *self.import_energy_low_entities,
            *self.export_energy_entities,
            *([self.active_tariff_entity] if self.active_tariff_entity else []),
            *([self.current_demand_entity] if self.current_demand_entity else []),
            *([self.peak_demand_entity] if self.peak_demand_entity else []),
        ]
        return list(dict.fromkeys(names))

    @classmethod
    def from_env(cls) -> HomeAssistantConfig:
        return cls(
            url=(_env("HA_URL", "") or "").rstrip("/"),
            token=_env("HA_TOKEN", "") or "",
            poll_interval=_env_float("HA_POLL_INTERVAL", 10.0),
            net_power_entity=_env("HA_P1_NET_POWER", "") or "",
            import_power_entity=_env("HA_P1_IMPORT_POWER", "") or "",
            export_power_entity=_env("HA_P1_EXPORT_POWER", "") or "",
            phase_power_entities=_env_list("HA_P1_PHASE_POWER"),
            phase_import_entities=_env_list("HA_P1_PHASE_IMPORT"),
            phase_export_entities=_env_list("HA_P1_PHASE_EXPORT"),
            import_energy_entities=_env_list("HA_P1_IMPORT_ENERGY"),
            export_energy_entities=_env_list("HA_P1_EXPORT_ENERGY"),
            import_energy_low_entities=_env_list("HA_P1_IMPORT_ENERGY_LOW"),
            active_tariff_entity=_env("HA_P1_ACTIVE_TARIFF", "") or "",
            current_demand_entity=_env("HA_P1_CURRENT_DEMAND", "") or "",
            peak_demand_entity=_env("HA_P1_PEAK_DEMAND", "") or "",
        )


@dataclass(frozen=True, slots=True)
class HistoryConfig:
    enabled: bool = True
    path: str = "/data/history.sqlite"
    retention_full_days: int = 1
    """Days of untouched full-resolution samples before they are rolled up.

    One day by default. Full resolution is what makes today worth looking at closely; for
    anything older, a minute is finer than the question being asked. Raising it is cheap --
    a week costs about 7 MB a year -- so this is a judgement about usefulness, not size."""
    retention_minute_days: int = 90
    """Days of one-minute aggregates before they are rolled up to hourly.

    This is the window that decides how far back you can scroll and still see a real day.
    A year costs about 23 MB."""

    @classmethod
    def from_env(cls) -> HistoryConfig:
        return cls(
            enabled=_env_bool("HISTORY_ENABLED", default=True),
            path=_env("HISTORY_PATH", "/data/history.sqlite") or "/data/history.sqlite",
            retention_full_days=_env_int("HISTORY_FULL_DAYS", 1),
            retention_minute_days=_env_int("HISTORY_MINUTE_DAYS", 90),
        )


@dataclass(frozen=True, slots=True)
class ControlConfig:
    """Write access to the inverter.

    Disabled unless explicitly switched on, and inert without installer credentials. The
    credentials belong in the container's environment, never in the repository -- writing
    to a grid-connected inverter is not something to enable by accident.
    """

    enabled: bool = False
    username: str = "installer"
    password: str | None = None

    @property
    def available(self) -> bool:
        return self.enabled and bool(self.password)

    @classmethod
    def from_env(cls) -> ControlConfig:
        return cls(
            enabled=_env_bool("CONTROL_ENABLED", default=False),
            username=_env("CONTROL_USERNAME", "installer") or "installer",
            password=_env("CONTROL_PASSWORD"),
        )


@dataclass(frozen=True, slots=True)
class MeterConfig:
    """How to read the sign of the grid meter.

    Huawei's `power_meter_active_power` is **positive when exporting** and negative when
    importing. That is the inverter's convention, not a site quirk, and it is the opposite
    of the P1 meter, whose value is consumption minus production.

    It stays configurable because a DTSU666 is installed with current transformers that
    can physically go on either way round. A site whose clamps are reversed reports the
    opposite of the documented convention, and no amount of correct code fixes that.
    """

    import_is_positive: bool = False

    @classmethod
    def from_env(cls) -> MeterConfig:
        return cls(import_is_positive=_env_bool("GRID_IMPORT_IS_POSITIVE", default=False))


@dataclass(frozen=True, slots=True)
class ArrayConfig:
    """How many panels hang off each MPPT, and what to call them.

    Without this, comparing two strings is meaningless: a string with more panels
    produces more energy, and that says nothing about whether its panels are healthy.
    Dividing by panel count turns raw string totals into a like-for-like comparison, which
    is what actually reveals shading, soiling or a failing panel.
    """

    panel_counts: list[int] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    panel_watts: int = 0
    """Nameplate watts of one panel, or 0 if unknown.

    With the panel counts this gives the array's peak DC capacity, which is what a
    production reading is worth comparing against. Without it the dashboard falls back to
    the inverter's rated power -- a poorer yardstick, since arrays are routinely oversized
    against the inverter, but at least it is this inverter's."""

    optimizer_string: int = 0
    """Fallback for which string the optimizers are on, 1-based, or 0 to leave it to the
    inverter.

    Optimizers are routinely fitted to part of an array rather than all of it, so their
    count is not the panel count. But the inverter already answers this: the optimizer file
    carries a string number and a position for every optimizer, which describes an array
    with them on several strings and this cannot. It is read at startup and used in
    preference to this setting, which stays only for firmware that does not report it."""

    @property
    def peak_w(self) -> int:
        """Peak DC capacity of the whole array, or 0 when it cannot be known."""
        return sum(self.panel_counts) * self.panel_watts

    def panels(self, string_index: int) -> int | None:
        index = string_index - 1
        return self.panel_counts[index] if 0 <= index < len(self.panel_counts) else None

    def label(self, string_index: int) -> str:
        index = string_index - 1
        if 0 <= index < len(self.labels) and self.labels[index]:
            return self.labels[index]
        return f"String {string_index}"

    @classmethod
    def from_env(cls) -> ArrayConfig:
        counts = []
        for item in _env_list("STRING_PANEL_COUNTS"):
            try:
                counts.append(int(item))
            except ValueError:
                continue
        return cls(
            panel_counts=counts,
            labels=_env_list("STRING_LABELS"),
            panel_watts=_env_int("PANEL_WATTS", 0),
            optimizer_string=_env_int("OPTIMIZER_STRING", 0),
        )


@dataclass(frozen=True, slots=True)
class PricingConfig:
    """A fixed tariff, for turning kilowatt-hours into money.

    Self-consumed solar is worth the retail price you did not pay; exported solar is worth
    the feed-in rate, which is usually much lower. Treating both as the same number is the
    usual way these figures end up wrong.
    """

    energy_price: float = 0.0
    """Import price during the normal (day) tariff."""

    low_tariff_price: float = 0.0
    """Import price during the low (night) tariff. Falls back to the day price when unset.

    Matters more than it looks on a site that charges the battery from the grid overnight
    in winter: pricing that import at the day rate overstates the cost of the strategy,
    which is the opposite of useful when deciding whether to keep using it."""

    feed_in_price: float = 0.0

    network_cost_per_kwh: float = 0.0
    """Everything billed per imported kilowatt-hour that is not the energy itself.

    Distribution, transmission and levies. On a Belgian bill these are the larger half: a
    contract at 20 cents of commodity was invoiced at over 50 cents delivered, so a dashboard
    pricing only the commodity understates both what import costs and what avoiding it is
    worth -- by more than the part it does count.

    It applies to avoided imports as well as to imports. Distribution is charged on
    offtake, so a kilowatt-hour the house takes from its own roof avoids that charge
    exactly as it avoids the commodity."""

    network_cost_low_per_kwh: float = 0.0
    """The same, for kilowatt-hours billed at the low tariff. Falls back to the day figure.

    Distribution is metered per register, so a dual-tariff meter is charged a lower
    distribution rate at night as well as a lower energy rate -- half a cent per kWh on a
    Flemish grid. It matters in proportion to how much of the import is nocturnal, which on
    a site that grid-charges its battery overnight is most of it."""

    vat_pct: float = 0.0
    """Applied to energy and network together. 6 in Belgium, 21 in the Netherlands.

    Zero where the bill is reclaimed: a VAT-registered business pays the excl-VAT price."""

    capacity_tariff_per_kw_year: float = 0.0
    """Billed on peak demand rather than on energy.

    Belgium's capaciteitstarief works this way: the charge follows the average of the
    monthly peak quarter-hour demands, so shaving a peak is worth money even when the
    energy price itself is fixed. Zero disables the estimate."""

    currency: str = "EUR"
    symbol: str = "\u20ac"

    @property
    def enabled(self) -> bool:
        return self.energy_price > 0

    def network_cost(self, *, low: bool = False) -> float:
        """Non-energy cost of one kilowatt-hour on the given tariff."""
        if low and self.network_cost_low_per_kwh > 0:
            return self.network_cost_low_per_kwh
        return self.network_cost_per_kwh

    def delivered(self, energy_price: float, network: float | None = None) -> float:
        """What a kilowatt-hour actually costs at the door, from a commodity rate."""
        net = self.network_cost_per_kwh if network is None else network
        return (energy_price + net) * (1 + self.vat_pct / 100)

    @classmethod
    def from_env(cls) -> PricingConfig:
        currency = (_env("CURRENCY", "EUR") or "EUR").upper()
        return cls(
            energy_price=_env_float("ENERGY_PRICE_PER_KWH", 0.0),
            low_tariff_price=_env_float("LOW_TARIFF_PRICE_PER_KWH", 0.0),
            feed_in_price=_env_float("FEED_IN_PRICE_PER_KWH", 0.0),
            network_cost_per_kwh=_env_float("NETWORK_COST_PER_KWH", 0.0),
            network_cost_low_per_kwh=_env_float("NETWORK_COST_LOW_PER_KWH", 0.0),
            vat_pct=_env_float("VAT_PCT", 0.0),
            capacity_tariff_per_kw_year=_env_float("CAPACITY_TARIFF_PER_KW_YEAR", 0.0),
            currency=currency,
            # A currency with no symbol here falls back to its own code rather than to
            # nothing: "AUD 3.42" is a price, "3.42" beside a euro figure elsewhere on the
            # page is a number the reader has to assume the units of.
            symbol={"EUR": "\u20ac", "GBP": "\u00a3", "USD": "$"}.get(currency)
            or (f"{currency}\u00a0" if currency else ""),
        )


@dataclass(frozen=True, slots=True)
class HttpConfig:
    host: str = "0.0.0.0"  # noqa: S104 - binding all interfaces is the point of a container
    port: int = 8480

    ingress_port: int = 8099
    """The socket Home Assistant's ingress connects to, and the only one that accepts writes.

    Separate from `port` because they carry different authority. `port` is offered in the
    add-on manifest for mapping onto the host, so anything on the network may reach it;
    this one is not offered there, so in an add-on nothing outside the Supervisor network
    can open it. That difference is what the write gate rests on -- a listener that was
    never exposed, rather than a header a caller supplies about itself.
    """

    @classmethod
    def from_env(cls) -> HttpConfig:
        return cls(
            host=_env("HTTP_HOST", "0.0.0.0") or "0.0.0.0",  # noqa: S104
            port=_env_int("HTTP_PORT", 8480),
            ingress_port=_env_int("HTTP_INGRESS_PORT", 8099),
        )


@dataclass(frozen=True, slots=True)
class Config:
    inverter: InverterConfig
    polling: PollingConfig
    mqtt: MqttConfig
    home_assistant: HomeAssistantConfig
    history: HistoryConfig
    control: ControlConfig
    http: HttpConfig
    pricing: PricingConfig
    array: ArrayConfig
    meter: MeterConfig
    site_name: str = "Solar"
    log_level: str = "INFO"
    backup_present: bool | None = None
    """Whether a Backup Box is fitted: True, False, or None to detect it.

    Detection reads the configured backup reserve, because the off-grid switch reads zero
    on a grid-connected system whether or not a Backup Box exists. That makes an owner who
    set the reserve to 0% indistinguishable from one who has no Backup Box at all, and
    there was no way to say which. Left at None the detection is unchanged."""

    @classmethod
    def from_env(cls) -> Config:
        return cls(
            inverter=InverterConfig.from_env(),
            polling=PollingConfig.from_env(),
            mqtt=MqttConfig.from_env(),
            home_assistant=HomeAssistantConfig.from_env(),
            history=HistoryConfig.from_env(),
            control=ControlConfig.from_env(),
            http=HttpConfig.from_env(),
            pricing=PricingConfig.from_env(),
            array=ArrayConfig.from_env(),
            meter=MeterConfig.from_env(),
            site_name=_env("SITE_NAME", "Solar") or "Solar",
            log_level=(_env("LOG_LEVEL", "INFO") or "INFO").upper(),
            backup_present={"yes": True, "no": False}.get(
                (_env("BACKUP_PRESENT", "auto") or "auto").lower(),
            ),
        )
