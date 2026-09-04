"""Publish to MQTT with Home Assistant discovery.

MQTT rather than a custom Home Assistant integration for one structural reason: the
inverter serves a single Modbus client, and a custom integration would be a second one
competing with this collector for the slot. Publishing to a broker keeps exactly one
process on the bus while Home Assistant gets fully-formed entities it created itself.

Every entity reads one key out of a single retained JSON payload, so one publish updates
all of them, and a restarting Home Assistant repopulates immediately from the retained
message instead of showing dashes until the next poll.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass
from typing import Any

import aiomqtt

from solar2000direct.config import Config
from solar2000direct.registers import (
    CAP_BACKUP,
    CAP_BATTERY_1,
    CAP_BATTERY_2,
    CAP_METER,
    CAP_P1,
    CAP_THREE_PHASE,
)
from solar2000direct.state import State

_LOGGER = logging.getLogger(__name__)

POWER = ("W", "power", "measurement")
ENERGY = ("kWh", "energy", "total_increasing")
DERIVED_ENERGY = ("kWh", None, "total_increasing")
"""Kilowatt-hours that are not a metered quantity.

Deliberately not device_class 'energy': the Energy Dashboard offers any such entity as a
source, and a per-panel average presented as a solar source would be silently wrong."""
PERCENT = ("%", "battery", "measurement")
TEMPERATURE = ("°C", "temperature", "measurement")
VOLTAGE = ("V", "voltage", "measurement")
CURRENT = ("A", "current", "measurement")
FREQUENCY = ("Hz", "frequency", "measurement")
DURATION = ("s", "duration", "measurement")
PLAIN = (None, None, "measurement")
TEXT = (None, None, None)


@dataclass(frozen=True, slots=True)
class Sensor:
    """One Home Assistant entity, backed by one key of the state payload."""

    key: str
    name: str
    spec: tuple[str | None, str | None, str | None]
    requires: frozenset[str] = frozenset()
    diagnostic: bool = False
    icon: str | None = None

    @property
    def unit(self) -> str | None:
        return self.spec[0]

    @property
    def device_class(self) -> str | None:
        return self.spec[1]

    @property
    def state_class(self) -> str | None:
        return self.spec[2]


BATTERY = frozenset({CAP_BATTERY_1})
BATTERY_2 = frozenset({CAP_BATTERY_2})
METER = frozenset({CAP_METER})
THREE_PHASE = frozenset({CAP_THREE_PHASE})
P1 = frozenset({CAP_P1})

SENSORS: tuple[Sensor, ...] = (
    # --- the numbers a person actually looks at ---
    Sensor("pv_power_w", "PV production", POWER),
    Sensor("pv_power_ac_w", "PV production (AC)", POWER),
    Sensor("house_load_w", "House load", POWER, requires=METER),
    Sensor("grid_power_w", "Grid power", POWER, requires=METER),
    Sensor("grid_import_w", "Grid import", POWER, requires=METER),
    Sensor("grid_export_w", "Grid export", POWER, requires=METER),
    Sensor("inverter_power_w", "Inverter output", POWER),
    Sensor("battery_power_w", "Battery power", POWER, requires=BATTERY),
    Sensor("battery_charge_w", "Battery charging", POWER, requires=BATTERY),
    Sensor("battery_discharge_w", "Battery discharging", POWER, requires=BATTERY),
    Sensor("storage_state_of_capacity", "Battery level", PERCENT, requires=BATTERY),
    # Needs the meter: it is a fraction of house load, and house load is inverter output
    # plus grid flow. Published unconditionally, it was an entity that could never fill in
    # on a meterless site -- and it is the one headline percentage on the dashboard.
    Sensor("instant_self_supply_pct", "Served without the grid", ("%", None, "measurement"),
           requires=METER, icon="mdi:home-lightning-bolt-outline"),
    # Per-string entities are built from the inverter's own string count, not listed here.
    # See _string_sensors.
    Sensor("string_imbalance_live_pct", "String imbalance now", ("%", None, "measurement"),
           icon="mdi:scale-unbalanced"),
    Sensor("string_imbalance_lifetime_pct", "String imbalance lifetime", ("%", None, "measurement"),
           icon="mdi:scale-unbalanced"),
    # --- energy ---
    Sensor("daily_yield_energy", "Inverter output today", ENERGY),
    Sensor("accumulated_yield_energy", "Solar production (lifetime)", ENERGY),
    Sensor("grid_accumulated_energy", "Grid imported", ENERGY, requires=METER),
    Sensor("grid_exported_energy", "Grid exported", ENERGY, requires=METER),
    Sensor("storage_total_charge", "Battery lifetime charged", ENERGY, requires=BATTERY),
    Sensor("storage_total_discharge", "Battery lifetime discharged", ENERGY, requires=BATTERY),
    Sensor("storage_current_day_charge_capacity", "Battery charged today", ENERGY, requires=BATTERY),
    Sensor("storage_current_day_discharge_capacity", "Battery discharged today", ENERGY, requires=BATTERY),
    # --- battery health: pack spread is the early warning the cloud portal averages away ---
    Sensor("battery_pack_soc_spread_pct", "Battery pack SOC spread", ("%", None, "measurement"), requires=BATTERY),
    Sensor("battery_pack_voltage_spread_v", "Battery pack voltage spread", VOLTAGE, requires=BATTERY,
           icon="mdi:scale-unbalanced"),
    Sensor("battery_pack_soc_mean_pct", "Battery pack mean level", PERCENT, requires=BATTERY, diagnostic=True),
    Sensor("battery_pack_temp_spread_c", "Battery pack temperature spread", TEMPERATURE, requires=BATTERY),
    Sensor("battery_pack_temp_max_c", "Battery pack hottest", TEMPERATURE, requires=BATTERY),
    Sensor("storage_unit_1_state_of_capacity", "Battery unit 1 level", PERCENT, requires=BATTERY),
    Sensor("storage_unit_2_state_of_capacity", "Battery unit 2 level", PERCENT, requires=BATTERY_2),
    Sensor("storage_unit_1_battery_temperature", "Battery unit 1 temperature", TEMPERATURE, requires=BATTERY),
    Sensor("storage_backup_power_state_of_charge", "Backup reserve", PERCENT, requires=frozenset({CAP_BACKUP})),
    # Whether the BMS has been able to recalibrate. A pack parked at low charge all winter
    # never reaches the voltage knee it needs, and both balancing and the SOC estimate drift.
    Sensor("storage_unit_soh_calibration_status", "Battery calibration", TEXT, requires=BATTERY,
           diagnostic=True, icon="mdi:battery-sync"),
    # --- inverter health ---
    Sensor("internal_temperature", "Inverter temperature", TEMPERATURE),
    Sensor("efficiency", "Inverter efficiency", ("%", None, "measurement"), diagnostic=True),
    Sensor("inverter_load_factor", "Inverter load factor", PLAIN, diagnostic=True),
    Sensor("conversion_loss_w", "Conversion loss", POWER, diagnostic=True),
    Sensor("insulation_resistance", "Insulation resistance", ("MOhm", None, "measurement"), diagnostic=True),
    Sensor("device_status", "Inverter status", TEXT),
    Sensor("active_alarms", "Active alarms", PLAIN, icon="mdi:alert-circle-outline"),
    Sensor("alarm_summary", "Alarm detail", TEXT, icon="mdi:alert-circle-outline"),
    Sensor("meter_status", "Meter status", TEXT, requires=METER, diagnostic=True),
    Sensor("storage_unit_1_running_status", "Battery unit 1 status", TEXT, requires=BATTERY, diagnostic=True),
    Sensor("storage_unit_2_running_status", "Battery unit 2 status", TEXT, requires=BATTERY_2, diagnostic=True),
    # --- AC side ---
    Sensor("phase_A_voltage", "Phase A voltage", VOLTAGE, diagnostic=True),
    Sensor("phase_B_voltage", "Phase B voltage", VOLTAGE, requires=THREE_PHASE, diagnostic=True),
    Sensor("phase_C_voltage", "Phase C voltage", VOLTAGE, requires=THREE_PHASE, diagnostic=True),
    Sensor("active_grid_frequency", "Grid frequency", FREQUENCY, requires=METER, diagnostic=True),
    Sensor("active_grid_A_power", "Meter phase A power", POWER, requires=METER, diagnostic=True),
    Sensor("active_grid_B_power", "Meter phase B power", POWER, requires=METER | THREE_PHASE, diagnostic=True),
    Sensor("active_grid_C_power", "Meter phase C power", POWER, requires=METER | THREE_PHASE, diagnostic=True),
    # --- the P1 cross-check ---
    # All of it needs a P1 feed read from Home Assistant, which is off by default. Without
    # the gate these five were created on every installation and stayed unknown forever.
    Sensor("p1_grid_power_w", "P1 grid power", POWER, requires=P1),
    Sensor("meter_delta_w", "Meter disagreement", POWER, requires=P1, icon="mdi:scale-balance"),
    Sensor("meter_delta_pct", "Meter disagreement percent", ("%", None, "measurement"),
           requires=P1, diagnostic=True),
    # A capacity tariff bills on these, so they are worth an entity of their own.
    Sensor("p1_current_demand_kw", "Current average demand", ("kW", "power", "measurement"), requires=P1),
    Sensor("p1_peak_demand_kw", "Peak demand this month", ("kW", "power", "measurement"),
           requires=P1, icon="mdi:transmission-tower-export"),
    # --- is any of this fresh? a dashboard that hides its own staleness is worse than none ---
    Sensor("live_age_s", "Data age", DURATION, diagnostic=True),
    Sensor("live_cycle_ms", "Poll cycle time", ("ms", None, "measurement"), diagnostic=True),
    Sensor("reads_failed", "Failed reads", PLAIN, diagnostic=True),
    Sensor("reconnects", "Reconnects", PLAIN, diagnostic=True),
)


class MqttPublisher:
    """Maintains the broker connection, the discovery configs and the state topic."""

    def __init__(self, config: Config, state: State) -> None:
        self.config = config
        self.state = state
        self._stop = asyncio.Event()
        self._discovery_sent = False

    def stop(self) -> None:
        self._stop.set()

    # --- topics ------------------------------------------------------------------

    @property
    def _node_id(self) -> str:
        serial = self.state.device.get("serial_number") or "unknown"
        return f"s2d_{serial}"

    @property
    def _state_topic(self) -> str:
        return f"{self.config.mqtt.base_topic}/{self._node_id}/state"

    @property
    def _availability_topic(self) -> str:
        return f"{self.config.mqtt.base_topic}/{self._node_id}/status"

    def _device_block(self) -> dict[str, Any]:
        device = self.state.device
        return {
            "identifiers": [self._node_id],
            "name": self.config.site_name,
            "manufacturer": "Huawei",
            "model": device.get("model_name") or "SUN2000",
            "sw_version": device.get("software_version"),
            "serial_number": device.get("serial_number"),
        }

    # --- discovery ---------------------------------------------------------------

    def _applicable_sensors(self) -> list[Sensor]:
        capabilities = self.state.all_capabilities
        return [sensor for sensor in SENSORS if sensor.requires <= capabilities]

    def _string_sensors(self) -> list[Sensor]:
        """One set of entities per PV input the inverter reports.

        Listed statically they were String 1 and String 2 -- the reference inverter's
        count -- so a four-input machine had half its array missing from Home Assistant
        and a single-input one had a String 2 reading a steady zero.
        """
        sensors: list[Sensor] = []
        labels = self.state.array.labels if hasattr(self.state.array, "labels") else []
        for index in range(1, self.state.shape.pv_strings + 1):
            label = (labels[index - 1] if index <= len(labels) and labels[index - 1] else f"String {index}")
            sensors.extend((
                Sensor(f"pv_string_{index}_power_w", f"{label} power", POWER),
                Sensor(f"pv_{index:02d}_voltage", f"{label} voltage", VOLTAGE, diagnostic=True),
                Sensor(f"pv_{index:02d}_current", f"{label} current", CURRENT, diagnostic=True),
                Sensor(f"cumulative_dc_energy_yield_mppt{index}", f"{label} lifetime yield", ENERGY),
                # Normalised by panel count, which is the only way unequal strings compare.
                Sensor(f"pv_string_{index}_w_per_panel", f"{label} watts per panel", POWER),
                Sensor(f"pv_string_{index}_kwh_per_panel", f"{label} lifetime per panel", DERIVED_ENERGY),
            ))
        return sensors

    def _optimizer_sensors(self) -> list[Sensor]:
        """One entity per panel, named by optimizer address.

        This is the data that makes local polling worth doing at all: which individual
        panel is shaded, soiled or failing. Built from what the inverter reports rather
        than from configuration, so it adapts to any array.
        """
        sensors: list[Sensor] = []
        for address in sorted(self.state.optimizer_info or self.state.optimizers):
            sensors.append(Sensor(f"optimizer_{address}_power_w", f"Panel {address} power", POWER))
            sensors.append(
                Sensor(f"optimizer_{address}_temperature_c", f"Panel {address} temperature", TEMPERATURE,
                       diagnostic=True),
            )
        return sensors

    def _discovery_payload(self, sensor: Sensor) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": sensor.name,
            "unique_id": f"{self._node_id}_{sensor.key}",
            "object_id": f"{self.config.site_name.lower().replace(' ', '_')}_{sensor.key}",
            "state_topic": self._state_topic,
            "availability_topic": self._availability_topic,
            "value_template": f"{{{{ value_json.{sensor.key} | default('unknown') }}}}",
            "device": self._device_block(),
        }
        if sensor.unit:
            payload["unit_of_measurement"] = sensor.unit
        if sensor.device_class:
            payload["device_class"] = sensor.device_class
        if sensor.state_class:
            payload["state_class"] = sensor.state_class
        if sensor.icon:
            payload["icon"] = sensor.icon
        if sensor.diagnostic:
            payload["entity_category"] = "diagnostic"
        return payload

    def wanted_sensors(self) -> list[Sensor]:
        """Every entity this installation should have, given what is known right now."""
        return [*self._applicable_sensors(), *self._string_sensors(), *self._optimizer_sensors()]

    async def _publish_discovery(self, client: aiomqtt.Client, sensors: list[Sensor]) -> None:
        prefix = self.config.mqtt.discovery_prefix
        for sensor in sensors:
            topic = f"{prefix}/sensor/{self._node_id}/{sensor.key}/config"
            await client.publish(topic, json.dumps(self._discovery_payload(sensor)), retain=True)
        _LOGGER.info("Published Home Assistant discovery for %d entities", len(sensors))

    async def _publish_connectivity(self, client: aiomqtt.Client) -> None:
        prefix = self.config.mqtt.discovery_prefix
        # A problem sensor so an automation can fire on "something is wrong" without
        # anyone having to enumerate Huawei's alarm catalogue first.
        await client.publish(
            f"{prefix}/binary_sensor/{self._node_id}/problem/config",
            json.dumps(
                {
                    "name": "Inverter problem",
                    "unique_id": f"{self._node_id}_problem",
                    "state_topic": self._state_topic,
                    "availability_topic": self._availability_topic,
                    "value_template": "{{ 'ON' if (value_json.active_alarms | default(0) | int) > 0 else 'OFF' }}",
                    "device_class": "problem",
                    "device": self._device_block(),
                },
            ),
            retain=True,
        )
        # Connection state as its own binary sensor, so an alert can fire on the collector
        # dying rather than on values quietly going stale.
        await client.publish(
            f"{prefix}/binary_sensor/{self._node_id}/connected/config",
            json.dumps(
                {
                    "name": "Inverter connection",
                    "unique_id": f"{self._node_id}_connected",
                    "state_topic": self._availability_topic,
                    "payload_on": "online",
                    "payload_off": "offline",
                    "device_class": "connectivity",
                    "entity_category": "diagnostic",
                    "device": self._device_block(),
                },
            ),
            retain=True,
        )

    # --- run ---------------------------------------------------------------------

    async def run(self) -> None:
        mqtt = self.config.mqtt
        if not mqtt.enabled or not mqtt.host:
            _LOGGER.info("MQTT not configured; skipping")
            return

        backoff = 1.0
        while not self._stop.is_set():
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - broker outages must not stop collection
                _LOGGER.warning("MQTT session ended (%s: %s), retrying in %.0fs", type(err).__name__, err, backoff)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                backoff = min(backoff * 2, 60)
            else:
                backoff = 1.0

    async def _session(self) -> None:
        mqtt = self.config.mqtt
        # The collector needs a serial number before it can name its topics, and that only
        # exists once the inverter has been identified. Wait rather than publish to a topic
        # keyed on "unknown" that would later have to be orphaned.
        while not self.state.device.get("serial_number"):
            if self._stop.is_set():
                return
            await asyncio.sleep(1)

        will = aiomqtt.Will(self._availability_topic, "offline", qos=1, retain=True)
        async with aiomqtt.Client(
            hostname=mqtt.host,
            port=mqtt.port,
            username=mqtt.username,
            password=mqtt.password,
            identifier=self._node_id,
            will=will,
        ) as client:
            _LOGGER.info("Connected to MQTT broker at %s:%d", mqtt.host, mqtt.port)
            await self._publish_connectivity(client)
            await client.publish(self._availability_topic, "online", qos=1, retain=True)

            # Discovery cannot be a one-off. The serial number arrives while the inverter
            # is being identified, but the optimizer list only arrives later, when the
            # collector reads it -- so publishing once on connect creates every entity
            # except the per-panel ones, and restarting just loses the same race again.
            # Instead the wanted set is recomputed each pass and anything new is announced.
            published: set[str] = set()

            while not self._stop.is_set():
                fresh = [sensor for sensor in self.wanted_sensors() if sensor.key not in published]
                if fresh:
                    await self._publish_discovery(client, fresh)
                    published.update(sensor.key for sensor in fresh)

                payload = {
                    key: value
                    for key, value in self.state.flat().items()
                    if value is not None and not isinstance(value, (dict, list))
                }
                await client.publish(self._state_topic, json.dumps(payload, default=str), retain=True)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=self.config.polling.live_interval)

            await client.publish(self._availability_topic, "offline", qos=1, retain=True)
