"""Running as a Home Assistant add-on.

An add-on is a container the Supervisor builds and runs, so the application itself is
unchanged. What differs is where configuration comes from, and the Supervisor supplies
two things that are otherwise manual work for the user:

* **MQTT credentials.** An add-on declaring ``services: mqtt:want`` can ask the Supervisor
  for the broker's host, port and credentials. No dedicated Home Assistant user, no
  password in a config file.
* **Home Assistant API access.** ``homeassistant_api: true`` puts a ``SUPERVISOR_TOKEN``
  in the environment, which authenticates against the core API through the Supervisor
  proxy. The long-lived access token disappears entirely.

Both are translated into the same ``S2D_*`` environment variables the container build
uses, so exactly one configuration path exists rather than two that can drift apart.
Anything the user set explicitly in the add-on options wins over what is discovered.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger(__name__)

OPTIONS_PATH = Path("/data/options.json")
SUPERVISOR_URL = "http://supervisor"

# Add-on option -> environment variable. Options absent or blank are skipped, so the
# defaults in config.py remain the single source of truth for what a setting means.
OPTION_ENV: dict[str, str] = {
    "inverter_host": "S2D_INVERTER_HOST",
    "inverter_port": "S2D_INVERTER_PORT",
    "inverter_unit_id": "S2D_INVERTER_UNIT_ID",
    "inverter_timeout": "S2D_INVERTER_TIMEOUT",
    "inverter_cooldown": "S2D_INVERTER_COOLDOWN",
    "site_name": "S2D_SITE_NAME",
    "log_level": "S2D_LOG_LEVEL",
    "live_interval": "S2D_LIVE_INTERVAL",
    "slow_interval": "S2D_SLOW_INTERVAL",
    "pack_interval": "S2D_PACK_INTERVAL",
    "optimizer_interval": "S2D_OPTIMIZER_INTERVAL",
    "optimizer_enabled": "S2D_OPTIMIZER_ENABLED",
    "energy_price_per_kwh": "S2D_ENERGY_PRICE_PER_KWH",
    "feed_in_price_per_kwh": "S2D_FEED_IN_PRICE_PER_KWH",
    "network_cost_per_kwh": "S2D_NETWORK_COST_PER_KWH",
    "network_cost_low_per_kwh": "S2D_NETWORK_COST_LOW_PER_KWH",
    "vat_pct": "S2D_VAT_PCT",
    "currency": "S2D_CURRENCY",
    "p1_net_power": "S2D_HA_P1_NET_POWER",
    "p1_import_power": "S2D_HA_P1_IMPORT_POWER",
    "p1_export_power": "S2D_HA_P1_EXPORT_POWER",
    "p1_phase_power": "S2D_HA_P1_PHASE_POWER",
    "p1_phase_import": "S2D_HA_P1_PHASE_IMPORT",
    "p1_phase_export": "S2D_HA_P1_PHASE_EXPORT",
    "p1_import_energy": "S2D_HA_P1_IMPORT_ENERGY",
    "p1_import_energy_low": "S2D_HA_P1_IMPORT_ENERGY_LOW",
    "p1_active_tariff": "S2D_HA_P1_ACTIVE_TARIFF",
    "low_tariff_price_per_kwh": "S2D_LOW_TARIFF_PRICE_PER_KWH",
    "p1_export_energy": "S2D_HA_P1_EXPORT_ENERGY",
    "p1_current_demand": "S2D_HA_P1_CURRENT_DEMAND",
    "p1_peak_demand": "S2D_HA_P1_PEAK_DEMAND",
    "string_panel_counts": "S2D_STRING_PANEL_COUNTS",
    "string_labels": "S2D_STRING_LABELS",
    "mqtt_enabled": "S2D_MQTT_ENABLED",
    "mqtt_discovery_prefix": "S2D_MQTT_DISCOVERY_PREFIX",
    "panel_watts": "S2D_PANEL_WATTS",
    "optimizer_string": "S2D_OPTIMIZER_STRING",
    "backup_present": "S2D_BACKUP_PRESENT",
    "grid_import_is_positive": "S2D_GRID_IMPORT_IS_POSITIVE",
    "capacity_tariff_per_kw_year": "S2D_CAPACITY_TARIFF_PER_KW_YEAR",
    "history_full_days": "S2D_HISTORY_FULL_DAYS",
    "history_minute_days": "S2D_HISTORY_MINUTE_DAYS",
    "control_enabled": "S2D_CONTROL_ENABLED",
    "control_username": "S2D_CONTROL_USERNAME",
    "control_password": "S2D_CONTROL_PASSWORD",
}


def is_addon() -> bool:
    """Whether we are running under the Home Assistant Supervisor."""
    return bool(os.environ.get("SUPERVISOR_TOKEN")) and OPTIONS_PATH.exists()


def _supervisor_get(path: str) -> dict[str, Any] | None:
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return None
    request = urllib.request.Request(  # noqa: S310 - fixed internal scheme and host
        f"{SUPERVISOR_URL}{path}",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
            payload = json.load(response)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as err:
        _LOGGER.info("Supervisor request %s failed: %s", path, err)
        return None
    if payload.get("result") != "ok":
        _LOGGER.info("Supervisor request %s returned %s", path, payload.get("result"))
        return None
    data = payload.get("data")
    return data if isinstance(data, dict) else None


def _set_default(name: str, value: Any) -> None:
    """Set an environment variable only if it is not already meaningfully set."""
    if value in (None, "") or os.environ.get(name):
        return
    os.environ[name] = str(value).lower() if isinstance(value, bool) else str(value)


def load_options() -> dict[str, Any]:
    try:
        return json.loads(OPTIONS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as err:
        _LOGGER.warning("Could not read add-on options: %s", err)
        return {}


def apply_environment() -> None:
    """Translate add-on options and Supervisor services into S2D_* variables."""
    options = load_options()
    for option, variable in OPTION_ENV.items():
        value = options.get(option)
        # A list option (the P1 phase entities) becomes the comma-separated form the
        # existing config parser already understands.
        if isinstance(value, list):
            value = ",".join(str(item) for item in value if str(item).strip())
        _set_default(variable, value)

    # Data written to /data survives add-on restarts and updates, and is included in
    # Home Assistant backups.
    _set_default("S2D_HISTORY_PATH", "/data/history.sqlite")
    # Ingress terminates on the add-on's own port; binding all interfaces is required.
    _set_default("S2D_HTTP_HOST", "0.0.0.0")  # noqa: S104
    _set_default("S2D_HTTP_PORT", "8480")

    _apply_mqtt(options)
    _apply_home_assistant(options)


def _apply_mqtt(options: dict[str, Any]) -> None:
    """Use the broker the Supervisor knows about, unless the user named one."""
    if options.get("mqtt_host"):
        _set_default("S2D_MQTT_HOST", options.get("mqtt_host"))
        _set_default("S2D_MQTT_PORT", options.get("mqtt_port") or 1883)
        _set_default("S2D_MQTT_USERNAME", options.get("mqtt_username"))
        _set_default("S2D_MQTT_PASSWORD", options.get("mqtt_password"))
        return

    service = _supervisor_get("/services/mqtt")
    if not service or not service.get("host"):
        _LOGGER.info(
            "No MQTT service registered with the Supervisor. Install the Mosquitto broker "
            "add-on, or set mqtt_host in the options, to publish to Home Assistant.",
        )
        return

    _set_default("S2D_MQTT_HOST", service.get("host"))
    _set_default("S2D_MQTT_PORT", service.get("port") or 1883)
    _set_default("S2D_MQTT_USERNAME", service.get("username"))
    _set_default("S2D_MQTT_PASSWORD", service.get("password"))
    _LOGGER.info("Using the MQTT broker registered with the Supervisor at %s", service.get("host"))


def _apply_home_assistant(options: dict[str, Any]) -> None:
    """Reach the core API through the Supervisor proxy, so no user token is needed."""
    if options.get("home_assistant_url"):
        _set_default("S2D_HA_URL", options["home_assistant_url"])
        _set_default("S2D_HA_TOKEN", options.get("home_assistant_token"))
        return

    if os.environ.get("SUPERVISOR_TOKEN"):
        _set_default("S2D_HA_URL", f"{SUPERVISOR_URL}/core")
        _set_default("S2D_HA_TOKEN", os.environ["SUPERVISOR_TOKEN"])
