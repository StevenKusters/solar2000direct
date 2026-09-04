"""Serve the real dashboard against fake data, with a stream that can be cut.

For checking what the page does when readings stop arriving -- which is not the same as
the connection failing. A stream that answers and then falls silent is what a sleeping
laptop, a changed network or a restarting add-on leaves behind, and the browser reports
nothing at all about it: no error fires, and the page goes on showing its last reading,
which is self-consistent and looks exactly like a quiet night.

    python solar2000direct/tests/mock_dashboard.py     # then open http://127.0.0.1:8790/

    GET /cut       stop writing to the stream, leaving the connection open
    GET /restore   start writing again

Cutting it should, within SILENCE_S, turn the badge red, raise the banner naming the time
the figures are from, and reconnect on its own. Killing this process entirely exercises
the other path, where the socket really does close.
"""

import asyncio
import json
import math
import time
from pathlib import Path

from aiohttp import web

PAGE = Path(__file__).resolve().parents[1] / "src" / "solar2000direct" / "web" / "index.html"
CUT = {"on": False}

# Installations to render the page against. Chosen to be unlike the machine the dashboard
# was written on, which is the only way to see what it assumes.
SITES = {
    "reference": {
        "name": "Reference site", "strings": 2, "units": 2,
        "caps": ["battery_1", "battery_2", "meter", "three_phase", "backup", "optimizers", "p1"],
        "panel_counts": [12, 14], "labels": ["East", "West"],
    },
    "pv-only": {   # a plain grid-tie: four inputs, no storage, no optimizers, no P1
        "name": "Neighbour, PV only", "strings": 4, "units": 0,
        "caps": ["meter", "three_phase"],
    },
    "unconfigured": {   # optimizers fitted but no panel counts, no prices, no writes
        "name": "Neighbour, nothing configured", "strings": 2, "units": 1,
        "caps": ["battery_1", "meter", "three_phase", "optimizers"],
    },
    "small": {     # single-phase, one string, one battery cabinet, no meter
        "name": "Neighbour, single-phase", "strings": 1, "units": 1,
        "caps": ["battery_1"],
    },
}
SITE = {"key": "reference"}
START = time.time()

def live():
    site = SITES[SITE["key"]]
    caps = site["caps"]
    t = time.time() - START
    pv = max(0.0, 4000 + 1500 * math.sin(t / 20))
    house = 1200.0
    battery = (pv - house) if "battery_1" in caps else 0.0
    grid = 0.0 if "meter" in caps else None

    values = {
        "pv_power_w": pv,
        "pv_power_ac_w": max(0.0, pv),
        "inverter_power_w": pv,
        "internal_temperature": 41.2,
        "device_status": "On-grid",
        "phase_A_voltage": 231.4,
        "accumulated_yield_energy": 20800.0,
    }
    for i in range(1, site["strings"] + 1):
        share = pv / site["strings"]
        values[f"pv_string_{i}_power_w"] = share
        values[f"pv_{i:02d}_voltage"] = 610.0 + i
        values[f"cumulative_dc_energy_yield_mppt{i}"] = 5200.0 * i
    if "three_phase" in caps:
        values.update({"phase_B_voltage": 230.1, "phase_C_voltage": 232.8})
    if "meter" in caps:
        values.update({
            "house_load_w": house, "grid_power_w": grid,
            "grid_import_w": max(grid, 0), "grid_export_w": max(-grid, 0),
            "instant_self_supply_pct": 100.0,
        })
        for phase in (["A", "B", "C"] if "three_phase" in caps else ["A"]):
            values[f"active_grid_{phase}_power"] = -300.0
    if "battery_1" in caps:
        values.update({
            "battery_power_w": battery,
            "battery_charge_w": max(battery, 0), "battery_discharge_w": max(-battery, 0),
            "storage_state_of_capacity": 61.0,
            "battery_pack_count": 3 if site["units"] > 1 else 1,
            "battery_pack_soc_mean_pct": 61.0,
        })
        if site["units"] > 1:
            values["battery_pack_soc_spread_pct"] = 0.4
            values["battery_pack_temp_spread_c"] = 1.2
            values["battery_pack_temp_max_c"] = 24.0
        for u in range(1, site["units"] + 1):
            values[f"storage_unit_{u}_state_of_capacity"] = 61.0
            values[f"storage_unit_{u}_charge_discharge_power"] = battery / site["units"]
    return {
        "site": site["name"],
        "status": "On-grid",
        "device": {"model_name": "SUN2000", "serial_number": "TEST"},
        "stats": {"connected": True, "live_age_s": 1.2, "live_cycle_ms": 640},
        "capabilities": sorted(caps),
        "shape": {"pv_strings": site["strings"], "battery_units": site["units"]},
        "array": {
            "labels": site.get("labels", []),
            "panel_counts": site.get("panel_counts", []),
            "optimizer_strings": [2] if "optimizers" in caps else [],
            "peak_w": sum(site.get("panel_counts", [])) * 410,
        },
        "values": values,
    }


async def site(request):
    """Switch which installation the page is being served."""
    key = request.query.get("key", "reference")
    if key not in SITES:
        return web.json_response({"error": "unknown site", "known": sorted(SITES)}, status=404)
    SITE["key"] = key
    return web.json_response({"site": key})

async def events(request):
    r = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"})
    await r.prepare(request)
    try:
        while True:
            if not CUT["on"]:
                await r.write(f"data: {json.dumps(live())}\n\n".encode())
            await asyncio.sleep(1.0)
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    return r

async def cut(request):
    CUT["on"] = True
    return web.json_response({"cut": True})

async def restore(request):
    CUT["on"] = False
    return web.json_response({"cut": False})

async def page(request):
    return web.Response(text=PAGE.read_text(), content_type="text/html")

app = web.Application()
app.add_routes([
    web.get("/", page),
    web.get("/events", events),
    web.get("/site", site),
    web.get("/cut", cut),
    web.get("/restore", restore),
    web.get("/api/live", lambda r: web.json_response(live())),
    web.get("/api/panels", lambda r: web.json_response({"measurable": False, "reason": "mock"})),
    web.get("/api/packs", lambda r: web.json_response({"measurable": False, "reason": "mock"})),
    web.get("/api/efficiency", lambda r: web.json_response({"measurable": False, "reason": "mock"})),
    web.get("/api/control", lambda r: web.json_response(
        {"available": True, "writes_enabled": False, "profiles": {}, "current": {}})),
    # The day the user reported, with the flow split the summary now measures.
    web.get("/api/energy", lambda r: web.json_response({
        "pv_yield_kwh": 26.31, "house_consumption_kwh": 25.53,
        "grid_import_kwh": 9.08, "grid_export_kwh": 0.08,
        "battery_charged_kwh": 12.07, "battery_discharged_kwh": 2.87,
        "solar_to_house_kwh": 13.58, "solar_to_battery_kwh": 12.07,
        "solar_to_grid_kwh": 0.08, "conversion_loss_kwh": 0.58,
        "from_solar_kwh": 13.58, "from_battery_kwh": 2.87, "from_grid_kwh": 9.08,
        "grid_to_battery_kwh": 0.0, "self_sufficiency_pct": 64.4,
        "money": {"symbol": "\u20ac", "saved": 2.59, "earned": 0.00,
                  "grid_cost": 1.42, "benefit": 2.59, "avoided_kwh": 16.45},
    })),
    web.get("/api/series", lambda r: web.json_response({"bucket_s": 216, "rows": [
        {
            "ts": 1756425600 + i * 216,
            "pv_w": max(0.0, 5200 * math.sin((i * 216 / 3600 - 7) / 12 * math.pi))
                    if 7 <= i * 216 / 3600 < 19 else 0.0,
            "house_w": 900.0 + 400 * (i % 5),
            "battery_charge_w": max(0.0, 2400 * math.sin((i * 216 / 3600 - 9) / 8 * math.pi))
                    if 9 <= i * 216 / 3600 < 17 else 0.0,
            "battery_discharge_w": 800.0 if i * 216 / 3600 >= 19 else 0.0,
            "grid_import_w": 700.0 if i * 216 / 3600 < 7 else 0.0,
            "grid_export_w": 0.0,
        }
        for i in range(400)
    ]})),
    web.get("/api/energy/buckets", lambda r: web.json_response({"rows": []})),
    web.get("/api/energy/profile", lambda r: web.json_response({"bucket_s": 3600, "rows": [
        {
            "ts": 1756425600 + i * 3600, "coverage": 1.0,
            "pv_kwh": max(0.0, 3.4 * math.sin((i - 7) / 12 * math.pi)) if 7 <= i < 19 else 0.0,
            "house_kwh": 0.9 + 0.4 * (i % 3),
            "battery_charged_kwh": max(0.0, 1.8 * math.sin((i - 9) / 8 * math.pi)) if 9 <= i < 17 else 0.0,
            "battery_discharged_kwh": 0.7 if i >= 19 else 0.0,
            "grid_import_kwh": 0.8 if i < 7 else 0.0,
            "grid_export_kwh": 0.05 if 12 <= i < 15 else 0.0,
        }
        for i in range(24)
    ]})),
])
web.run_app(app, host="127.0.0.1", port=8790, print=None)
