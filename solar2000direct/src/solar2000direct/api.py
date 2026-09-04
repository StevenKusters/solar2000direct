"""HTTP API, live event stream, Prometheus metrics and the dashboard.

Everything here reads the collector's in-memory state. Nothing here touches the Modbus
bus, which is what lets the live page, the API and Home Assistant all exist at once
without competing for the inverter's single connection slot.

The event stream is server-sent events rather than a websocket: the traffic is one-way,
SSE reconnects on its own, and it survives a proxy that would need explicit configuration
to pass websockets.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from aiohttp import web

from solar2000direct.config import Config
from solar2000direct.control import ControlError, ControlManager, describe_schedule
from solar2000direct.history import History
from solar2000direct.state import State

_LOGGER = logging.getLogger(__name__)

WEB_ROOT = Path(__file__).parent / "web"

# Prometheus metric names must match [a-zA-Z_:][a-zA-Z0-9_:]*
_METRIC_SAFE = str.maketrans(dict.fromkeys(" -./()%", "_"))


def _json(payload: Any, status: int = 200) -> web.Response:  # noqa: ANN401
    return web.json_response(payload, status=status, dumps=lambda obj: json.dumps(obj, default=str))


async def _json_body(request: web.Request) -> dict[str, Any] | None:
    """The request body as an object, or None for anything that is not one.

    `request.json()` raises on a malformed body, and an unhandled exception in a handler is
    a 500 with a stack trace -- the same reason `_int_param` exists for query strings. A
    body that parses but is a list or a string is equally unusable to the callers here,
    which all do `body.get(...)`, so it is refused in the same breath.
    """
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return body if isinstance(body, dict) else None


class Api:
    """The HTTP surface: JSON API, SSE stream, metrics and the dashboard."""

    def __init__(
        self,
        config: Config,
        state: State,
        history: History | None = None,
        control: ControlManager | None = None,
    ) -> None:
        self.config = config
        self.state = state
        self.history = history
        self.control = control
        self._runner: web.AppRunner | None = None

    def build_app(self) -> web.Application:
        app = web.Application()
        app.add_routes(
            [
                web.get("/", self.dashboard),
                web.get("/api/state", self.full_state),
                web.get("/api/live", self.live),
                web.get("/api/registers", self.registers),
                web.get("/api/optimizers", self.optimizers),
                web.get("/api/health", self.health),
                web.get("/events", self.events),
                web.get("/metrics", self.metrics),
                web.get("/api/series", self.series),
                web.get("/api/energy", self.energy),
                web.get("/api/energy/buckets", self.energy_buckets),
                web.get("/api/energy/profile", self.energy_profile),
                web.get("/api/panels", self.panels),
                web.get("/api/efficiency", self.efficiency),
                web.get("/api/packs", self.packs),
                web.get("/api/control", self.control_state),
                web.get("/api/control/diff", self.control_diff),
                web.post("/api/control/save", self.control_save),
                web.post("/api/control/apply", self.control_apply),
                web.get("/api/history", self.history_stats),
            ],
        )
        return app

    async def start(self) -> None:
        app = self.build_app()
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        # Two sockets, one application. They differ only in who can reach them, and that
        # is exactly the distinction the write routes need: see _through_ingress.
        for port in (self.config.http.port, self.config.http.ingress_port):
            await web.TCPSite(self._runner, self.config.http.host, port).start()
        _LOGGER.info(
            "HTTP listening on %s:%d, ingress on %d",
            self.config.http.host, self.config.http.port, self.config.http.ingress_port,
        )

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    # --- handlers ----------------------------------------------------------------

    async def dashboard(self, _request: web.Request) -> web.StreamResponse:
        index = WEB_ROOT / "index.html"
        if not index.exists():
            return web.Response(text="Dashboard not installed", status=404)
        return web.FileResponse(index, headers={"Cache-Control": "no-cache"})

    async def full_state(self, _request: web.Request) -> web.Response:
        return _json(self.state.snapshot())

    async def live(self, _request: web.Request) -> web.Response:
        """The compact payload the dashboard polls, and the one worth scripting against."""
        return _json(self._live_payload())

    async def registers(self, _request: web.Request) -> web.Response:
        return _json(
            {
                name: {"value": reading.value, "unit": reading.unit, "age_s": round(reading.age, 2)}
                for name, reading in sorted(self.state.readings.items())
            },
        )

    async def optimizers(self, _request: web.Request) -> web.Response:
        merged = {}
        for address in sorted(set(self.state.optimizer_info) | set(self.state.optimizers)):
            merged[str(address)] = {
                **self.state.optimizer_info.get(address, {}),
                **self.state.optimizers.get(address, {}),
            }
        return _json({"optimizers": merged, "count": len(merged)})

    async def health(self, _request: web.Request) -> web.Response:
        """Liveness for a container orchestrator.

        Unhealthy means the data is stale, not merely that the process is up: a collector
        that has lost the bus but is still serving its last reading is the failure mode
        worth catching.
        """
        stats = self.state.stats.as_dict()
        age = stats.get("live_age_s")
        stale_after = max(self.config.polling.live_interval * 5, 30.0)
        healthy = bool(stats["connected"]) and age is not None and age < stale_after
        return _json(
            {"healthy": healthy, "stale_after_s": stale_after, **stats},
            status=200 if healthy else 503,
        )

    def _live_payload(self) -> dict[str, Any]:
        return {
            "timestamp": time.time(),
            "site": self.config.site_name,
            "device": self.state.device,
            "capabilities": sorted(self.state.all_capabilities),
            "shape": {
                "pv_strings": self.state.shape.pv_strings,
                "battery_units": self.state.shape.battery_units,
            },
            "array": {
                "labels": [
                    self.config.array.label(i)
                    for i in range(1, self.state.shape.pv_strings + 1)
                ],
                "panel_counts": self.config.array.panel_counts,
                "panel_watts": self.config.array.panel_watts,
                "peak_w": self.config.array.peak_w,
                # Reported by the inverter, one entry per string that carries optimizers.
                # The configured single string is only a fallback for firmware that does
                # not say, and is empty rather than guessed when nothing is known.
                "optimizer_strings": self.state.optimizer_strings or (
                    [self.config.array.optimizer_string] if self.config.array.optimizer_string else []
                ),
            },
            "pricing": {
                "symbol": self.config.pricing.symbol,
                "capacity_tariff_per_kw_year": self.config.pricing.capacity_tariff_per_kw_year,
            },
            # The flat payload, which merges derived values with raw register readings.
            # Consumers want "battery level" without caring that state of charge is a
            # register while house load is arithmetic.
            "values": self.state.flat(),
            "optimizers": self.state.optimizers,
            "optimizer_info": self.state.optimizer_info,
            "p1": self.state.p1,
            "stats": self.state.stats.as_dict(),
            "status": self.state.value("device_status"),
            "alarms": [self.state.value(f"alarm_{n}") for n in (1, 2, 3)],
        }

    async def events(self, request: web.Request) -> web.StreamResponse:
        """Server-sent events, one snapshot per poll interval."""
        response = web.StreamResponse(
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",  # stop nginx buffering the stream into uselessness
            },
        )
        await response.prepare(request)
        interval = max(1.0, self.config.polling.live_interval)
        try:
            while True:
                payload = json.dumps(self._live_payload(), default=str)
                await response.write(f"data: {payload}\n\n".encode())
                await asyncio.sleep(interval)
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            with contextlib.suppress(Exception):
                await response.write_eof()
        return response

    @staticmethod
    def _int_param(request: web.Request, name: str, default: int) -> int:
        """One integer out of the query string, or a 400 rather than a traceback.

        `int("today")` raises, and an unhandled ValueError in a handler is a 500 with a
        stack trace in the response -- which tells a stranger the framework, the file
        layout and the line numbers, in exchange for telling them nothing useful.
        """
        raw = request.query.get(name)
        if raw is None:
            return default
        try:
            return int(raw)
        except ValueError:
            raise web.HTTPBadRequest(
                text=json.dumps({"error": f"{name!r} must be a whole number, got {raw!r}"}),
                content_type="application/json",
            ) from None

    def _window(self, request: web.Request) -> tuple[int, int]:
        """Parse a time window, defaulting to the last 24 hours."""
        now = int(time.time())
        until = self._int_param(request, "until", now)
        since = self._int_param(request, "since", until - 86400)
        return since, until

    async def series(self, request: web.Request) -> web.Response:
        if self.history is None:
            return _json({"error": "history is disabled"}, status=404)
        since, until = self._window(request)
        points = min(2000, max(10, self._int_param(request, "points", 400)))
        return _json(await self.history.series(since, until, points))

    async def energy(self, request: web.Request) -> web.Response:
        if self.history is None:
            return _json({"error": "history is disabled"}, status=404)
        since, until = self._window(request)
        summary = await self.history.energy_summary(since, until)
        return _json(self._with_money(summary))

    async def energy_profile(self, request: web.Request) -> web.Response:
        """Energy per fixed-width bucket, for the finer-grained companion charts."""
        if self.history is None:
            return _json({"error": "history is disabled"}, status=404)
        since, until = self._window(request)
        bucket = max(300, min(86400, self._int_param(request, "bucket", 3600)))
        return _json(await self.history.energy_profile(since, until, bucket))

    async def energy_buckets(self, request: web.Request) -> web.Response:
        if self.history is None:
            return _json({"error": "history is disabled"}, status=404)
        since, until = self._window(request)
        bucket = request.query.get("bucket", "day")
        if bucket not in {"day", "month"}:
            return _json({"error": "bucket must be day or month"}, status=400)
        return _json(await self.history.energy_buckets(since, until, bucket))

    async def efficiency(self, request: web.Request) -> web.Response:
        """Measured battery round-trip, and the day/night gap it implies is needed."""
        if self.history is None:
            return _json({"error": "history is disabled"}, status=404)
        now = int(time.time())
        until = self._int_param(request, "until", now)
        since = self._int_param(request, "since", until - 14 * 86400)
        result = await self.history.round_trip_efficiency(since, until)
        pricing = self.config.pricing
        if pricing.enabled and result.get("required_day_night_gap_pct") is not None:
            night = pricing.low_tariff_price or pricing.energy_price
            actual_gap = 100 * (pricing.energy_price / night - 1) if night else None
            result["configured_day_night_gap_pct"] = round(actual_gap, 1) if actual_gap is not None else None
            if actual_gap is not None:
                result["night_charging_pays"] = actual_gap > result["required_day_night_gap_pct"]
        return _json(result)

    async def packs(self, request: web.Request) -> web.Response:
        """Battery pack balance over time, grouped by the charge level it was measured at."""
        if self.history is None:
            return _json({"error": "history is disabled"}, status=404)
        now = int(time.time())
        until = self._int_param(request, "until", now)
        since = self._int_param(request, "since", until - 90 * 86400)
        return _json(await self.history.pack_balance(since, until))

    async def panels(self, request: web.Request) -> web.Response:
        """Per-panel performance relative to siblings, over a window (default 7 days)."""
        if self.history is None:
            return _json({"error": "history is disabled"}, status=404)
        now = int(time.time())
        until = self._int_param(request, "until", now)
        since = self._int_param(request, "since", until - 7 * 86400)
        result = await self.history.panel_performance(since, until)
        for panel in result.get("panels", []):
            panel.update(self.state.optimizer_info.get(panel["address"], {}))
        return _json(result)

    # --- control ------------------------------------------------------------------
    #
    # Reading is always available. Writing needs control explicitly enabled with an
    # installer password, and always names a saved profile: nothing here acts on its own.

    async def control_state(self, _request: web.Request) -> web.Response:
        if self.control is None:
            return _json({"available": False})
        try:
            current = await self.control.read_configuration()
        except ControlError as err:
            return _json({"available": False, "error": str(err)})

        readable = {}
        for register, value in current.items():
            readable[register] = describe_schedule(value) if isinstance(value, (list, tuple)) else (
                value.name if hasattr(value, "name") and not isinstance(value, str) else value
            )
        return _json(
            {
                "available": True,
                "writes_enabled": self.config.control.available,
                "current": readable,
                "profiles": {
                    name: {"note": profile.get("note", "")}
                    for name, profile in self.control.profiles().items()
                },
            },
        )

    async def control_diff(self, request: web.Request) -> web.Response:
        if self.control is None:
            return _json({"error": "control is unavailable"}, status=404)
        name = request.query.get("profile", "")
        try:
            return _json(await self.control.compare(name))
        except ControlError as err:
            return _json({"error": str(err)}, status=400)

    def _through_ingress(self, request: web.Request) -> bool:
        """Whether this request arrived on the ingress socket, which only the Supervisor can open.

        Nothing in this process authenticates anybody: the listener is plain HTTP with no
        session, no token and no login. So the question a write has to answer is not who is
        asking but which door they came through.

        This used to test for the `X-Ingress-Path` header the Supervisor sets. A header is
        a claim the caller makes about itself: `curl -H 'X-Ingress-Path: /'` from any host
        on the network satisfied it, which made the port's advertised read-only guarantee
        untrue whenever somebody mapped it and turned control on.

        The port is not a claim. `ingress_port` is absent from the manifest's `ports`, so
        the Supervisor will not map it to the host and only the internal network reaches
        it; `port` is offered for mapping and must therefore be assumed hostile. Running
        outside Home Assistant there is no ingress at all, and writes are refused until the
        operator deliberately publishes that second port themselves.

        The header is still required on top, as corroboration that the Supervisor -- and
        not something else already inside the container network -- proxied this.
        """
        sock = request.transport.get_extra_info("sockname") if request.transport else None
        arrived_on = sock[1] if isinstance(sock, tuple) and len(sock) >= 2 else None  # noqa: PLR2004
        return arrived_on == self.config.http.ingress_port and bool(
            request.headers.get("X-Ingress-Path"))

    def _refuse_direct(self, request: web.Request) -> web.Response | None:
        if self._through_ingress(request):
            return None
        return _json(
            {
                "error": "Changing the inverter is only possible through Home Assistant. "
                         "Open the add-on's Web UI rather than calling this port directly.",
                "detail": "This port serves reads. The routes that write to the inverter "
                          "answer only on the ingress port, which is not mapped onto the "
                          "host.",
            },
            status=403,
        )

    # A profile name is echoed back into the dashboard and used as a filename key, so it is
    # held to characters that cannot end a quoted attribute or a path segment.
    _SAFE_NAME = re.compile(r"^[\w][\w .-]{0,39}$")

    async def control_save(self, request: web.Request) -> web.Response:  # noqa: PLR0911
        if self.control is None:
            return _json({"error": "control is unavailable"}, status=404)
        if (refusal := self._refuse_direct(request)) is not None:
            return refusal
        if (body := await _json_body(request)) is None:
            return _json({"error": "body must be JSON"}, status=400)
        name = str(body.get("name", "")).strip()
        if not name:
            return _json({"error": "a profile name is required"}, status=400)
        if not self._SAFE_NAME.match(name):
            return _json(
                {"error": "A name may use letters, digits, spaces, dots, dashes and "
                          "underscores, up to 40 characters."},
                status=400,
            )
        try:
            saved = await self.control.snapshot(name, str(body.get("note", "")))
        except ControlError as err:
            return _json({"error": str(err)}, status=400)
        return _json({"saved": name, "settings": len(saved["settings"]), "schedules": len(saved["schedules"])})

    async def control_apply(self, request: web.Request) -> web.Response:
        if self.control is None:
            return _json({"error": "control is unavailable"}, status=404)
        if (refusal := self._refuse_direct(request)) is not None:
            return refusal
        if (body := await _json_body(request)) is None:
            return _json({"error": "body must be JSON"}, status=400)
        name = str(body.get("name", "")).strip()
        try:
            result = await self.control.apply(name, force=bool(body.get("force")))
        except ControlError as err:
            return _json({"error": str(err)}, status=403 if "disabled" in str(err) else 400)
        return _json(result, status=200 if result["ok"] else 207)

    def _with_money(self, summary: dict[str, Any]) -> dict[str, Any]:
        """Value the energy, keeping avoided imports and exports apart.

        Energy the house used but did not buy is worth the retail price avoided; exported
        solar is worth the feed-in rate, which is usually far lower. One blended number
        would flatter the export side and understate the value of using it in the house.

        What counts as avoided is what actually reached the house -- solar directly, plus
        whatever the battery delivered. It used to be production minus export, which on any
        single day also books the kilowatt-hours still sitting in the battery, and the
        inverter's conversion loss with them. On the day this was reported that was 26.22
        kWh valued against a house that consumed 25.53, of which 9.08 came off the grid:
        EUR 4.11 where EUR 2.59 was earned. Solar banked in the battery is worth money on
        the day it is discharged, not the day it is stored.
        """
        pricing = self.config.pricing
        if not pricing.enabled:
            return summary

        from_solar = summary.get("from_solar_kwh")
        from_battery = summary.get("from_battery_kwh")
        avoided = (
            from_solar + from_battery
            if isinstance(from_solar, (int, float)) and isinstance(from_battery, (int, float))
            # Without a meter there is no house figure to measure against, so the older
            # production-minus-export estimate is all there is.
            else summary.get("self_consumed_kwh")
        )
        exported = summary.get("grid_export_kwh")
        imported = summary.get("grid_import_kwh")
        money: dict[str, Any] = {"currency": pricing.currency, "symbol": pricing.symbol}

        # Split import across tariffs where the meter reports them separately. On a site
        # that grid-charges the battery overnight, most import lands on the low tariff,
        # and pricing it all at the day rate would overstate what that strategy costs.
        #
        # Both figures describe THIS window. They used to come from State, which holds the
        # meter's lifetime registers: a meter whose lifetime import happens to be 55%
        # nocturnal priced every window at a 55% night mix, including a window that ran
        # entirely in daylight. The same counters are recorded in history and differenced
        # over the window like everything else, so the split now moves with the period.
        low_share = summary.get("grid_import_low_kwh")
        low_price = pricing.low_tariff_price or pricing.energy_price
        if isinstance(imported, (int, float)) and isinstance(low_share, (int, float)) \
                and imported > 0:
            low_fraction = min(1.0, max(0.0, low_share / imported))
            blended = low_fraction * low_price + (1 - low_fraction) * pricing.energy_price
            # Distribution is metered per register too, so the night share carries the
            # lower network rate along with the lower energy rate.
            blended_network = (
                low_fraction * pricing.network_cost(low=True)
                + (1 - low_fraction) * pricing.network_cost()
            )
            money["grid_cost"] = round(imported * pricing.delivered(blended, blended_network), 2)
            money["low_tariff_share_pct"] = round(100 * low_fraction, 1)
        # Priced at what a kilowatt-hour costs at the door, not at the commodity rate
        # alone. Distribution, levies and VAT are charged on every imported unit, so they
        # are equally avoided by one the house did not import -- and on a Belgian bill they
        # are the larger half of the price.
        if isinstance(avoided, (int, float)):
            money["saved"] = round(avoided * pricing.delivered(pricing.energy_price), 2)
            money["avoided_kwh"] = round(avoided, 2)
        if isinstance(exported, (int, float)):
            # Injection earns the feed-in rate and nothing else: no network charge is
            # levied on it, and none is avoided by it.
            money["earned"] = round(exported * pricing.feed_in_price, 2)
        if isinstance(imported, (int, float)) and "grid_cost" not in money:
            money["grid_cost"] = round(imported * pricing.delivered(pricing.energy_price), 2)
        money["delivered_price_per_kwh"] = round(pricing.delivered(pricing.energy_price), 4)
        if "saved" in money or "earned" in money:
            money["benefit"] = round(money.get("saved", 0) + money.get("earned", 0), 2)
        return {**summary, "money": money}

    async def history_stats(self, _request: web.Request) -> web.Response:
        if self.history is None:
            return _json({"enabled": False})
        return _json(await self.history.stats())

    async def metrics(self, _request: web.Request) -> web.Response:
        """Prometheus exposition, for whoever already runs Grafana."""
        lines: list[str] = []
        for key, value in sorted(self.state.flat().items()):
            if value is None or isinstance(value, (bool, dict, list, str)):
                continue
            name = f"s2d_{key}".translate(_METRIC_SAFE)
            lines.append(f"# TYPE {name} gauge")
            lines.append(f"{name} {value}")
        lines.append("# TYPE s2d_connected gauge")
        lines.append(f"s2d_connected {int(self.state.stats.connected)}")
        return web.Response(text="\n".join(lines) + "\n", content_type="text/plain")
