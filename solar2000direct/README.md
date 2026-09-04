# solar2000direct

Local, real-time telemetry for Huawei SUN2000 / LUNA2000 installations, read straight
off the inverter over Modbus TCP — no FusionSolar cloud, no five-minute aggregation.

Built on [`huawei-solar`](https://github.com/wlcrs/huawei-solar-lib) (v3.x), which carries
the register map for Huawei's *Solar Inverter Modbus Interface Definitions*. This project
is the layer above it: one process owns the single Modbus session and fans the data out to
a live page, a JSON API, MQTT for Home Assistant, and local high-resolution history.

## What it adapts to

Nothing about the installation is configured by hand. On connect the inverter is asked what
it is, and everything follows: how many MPPT inputs to read and chart, whether there is a
battery and how many storage units and packs, whether a grid meter is fitted, whether the
supply is single- or three-phase, whether optimizers are present, whether there is a Backup
Box. Hardware that is not there gets no register read, no Home Assistant entity, and no
card on the dashboard -- an empty card reads as equipment that is fitted and silent, which
is worse than none at all.

Two things it cannot infer, both optional: how many panels are on each string, and what one
panel is rated at. Give it those and the per-panel comparisons and the capacity bar become
meaningful; leave them out and it falls back to figures it can measure for itself.

## Why a single collector

A Huawei inverter accepts **one Modbus client at a time**, and it answers slowly. Running
the Home Assistant integration *and* a dashboard *and* an API as three separate clients is
the usual cause of flapping, unavailable sensors. So:

```
                          one Modbus session
[ SUN2000 / SDongle ] <---------------------- [ collector ]
                                                    |
                        +---------------------------+---------------+
                        |            |              |               |
                    REST/JSON   SSE/WebSocket   MQTT (HA        Prometheus
                    (mini API)  (live page)     autodiscovery)  /metrics
```

Everything downstream reads the collector's in-memory state. Nothing else touches the bus.

## Why wide block reads

Measured against a real SUN2000-8KTL-M1 behind an SDongle, a Modbus round-trip costs
**~500-600 ms whether it carries 1 register or 120**. The bus is latency-bound, not
bandwidth-bound, so the only thing that costs anything is the number of round-trips.

That inverts the obvious design. Polling semantic groups on separate cadences maximises
round-trips; sorting every register by address and packing it into the widest spans a
single Modbus read allows minimises them. `build_read_plan()` does the packing, and
`s2d-bench` proves on real hardware both that the device tolerates the spans and that the
fast path decodes to the same values as the slow one.

## Status

| Component | State |
|---|---|
| `s2d-probe` — capability and latency probe | working |
| `s2d-bench` — wide-block read benchmark and validator | working |
| `tests/mock_inverter.py` — fake SUN2000 for development | working |
| Collector daemon — one Modbus session, tiered adaptive polling | working |
| MQTT publisher with Home Assistant discovery | working |
| P1 meter read-back from Home Assistant | working |
| HTTP API, SSE stream, Prometheus metrics, dashboard | working |
| Docker image and compose file | working |
| Local high-resolution history with tiered rollups | working |
| Energy accounting, charts and tariff valuation | working |
| Home Assistant add-on (ingress, auto MQTT, no token) | working |
| `s2d-read` — ad-hoc register reader | working |
| Per-panel history and sibling comparison | working |
| Battery mode profiles (manual switch) | working |

## Before you start

Modbus TCP must be enabled on the inverter. In the FusionSolar installer app:
**Communication configuration → Modbus TCP → Connection = "Unrestricted"**. This needs
installer-level access.

The inverter serves one client at a time. If the SDongle is mid-upload to FusionSolar, or
another integration is polling, reads will be interrupted. That is expected behaviour, not
a fault in this tool — the probe reports it rather than hiding it.

## Feeding the Home Assistant Energy Dashboard

Use the **P1 meter** for grid import and export. It is the fiscal meter, it is what the
bill is based on, and on this installation it differs from the inverter's meter by 2.7% on
import -- and by far more on export, because the two counters started at different times.

Use this add-on for the inverter side:

| Energy Dashboard slot | entity |
|---|---|
| Solar production | `accumulated_yield_energy` -- PV after conversion |
| Battery in | `storage_total_charge` |
| Battery out | `storage_total_discharge` |

**Do not** use `daily_yield_energy` as solar production. It is the inverter's AC output,
which on a hybrid system includes battery discharge, so it counts stored solar twice. It
is named "Inverter output today" for that reason.

The per-string lifetime counters are also offered, one per MPPT input the inverter
reports, and can be used as separate solar sources where the strings face different ways.
They are measured on the DC side, so they read a percent or two above the AC figure --
pick one approach or the other, not both.

## Battery mode profiles

The seasonal change on a battery installation is usually one setting: maximise
self-consumption while the sun is useful, time-of-use with overnight grid charging when it
is not. Doing that through the installer app means retyping a schedule twice a year.

A **profile** is a snapshot of what the inverter is actually set to. Configure it once in
the app, save it under a name, and switch between saved profiles from the dashboard. The
tool never invents a setting — it only replays one it has seen.

Nothing runs on a schedule or reacts to conditions. Applying a profile requires
`control_enabled`, an installer password, and a press of a button that first shows exactly
what would change. Only settings that actually differ are written, and each write is read
back to confirm the inverter accepted it.

## Installing as a Home Assistant add-on

Add this repository under Settings → Add-ons → Add-on Store → ⋮ → Repositories, then
install **solar2000direct**. On a Supervised installation the Supervisor supplies two
things that would otherwise be manual:

* **MQTT credentials**, from the broker registered with it — no dedicated Home Assistant
  user and no password in a config file;
* **core API access**, via `SUPERVISOR_TOKEN` — the long-lived access token used to read
  the P1 meter disappears entirely.

The dashboard is served through **ingress**, so it appears in the Home Assistant sidebar,
authenticated by Home Assistant, with no port exposed. Only `inverter_host` is required.

## Running it as a plain container

```bash
cp .env.example .env    # set S2D_INVERTER_HOST at minimum
docker compose up -d
```

Then open `http://<host>:8480`. (Commands in this file run from the `solar2000direct/`
directory of a checkout — that is where the `Dockerfile` and `pyproject.toml` live.)

| endpoint | purpose |
|---|---|
| `/` | live dashboard |
| `/events` | server-sent event stream, one snapshot per poll |
| `/api/live` | current values: derived plus raw registers |
| `/api/state` | everything, including per-register age |
| `/api/registers` | raw register readings |
| `/api/optimizers` | per-panel data |
| `/api/health` | liveness — 503 when the data goes stale |
| `/metrics` | Prometheus exposition |
| `/api/control` | current inverter settings and saved profiles |
| `POST /api/control/save` | save the current settings as a named profile |
| `POST /api/control/apply` | write a saved profile back to the inverter |

The two `POST` routes are the only ones that change anything, and they answer on a
different socket from every route above them: `S2D_HTTP_INGRESS_PORT` (8099), not
`S2D_HTTP_PORT` (8480). Nothing in this process authenticates a caller, so the only thing
a write can rely on is which door it came through. As an add-on that door is the one Home
Assistant's ingress connects to internally; it is absent from the manifest's `ports`, so
the Supervisor will not map it onto the host. That is why 8480 can be published without
exposing the inverter: over it, this API is read-only.

In plain Docker, `docker-compose.yml` publishes 8480 only. Add 8099 yourself if you want
the control API reachable — and be aware that nothing guards it once you do.

An earlier version tested for the `X-Ingress-Path` header the Supervisor sets. That is a
claim a caller makes about itself, and `curl -H 'X-Ingress-Path: /'` satisfied it, so the
read-only guarantee above was not true for anyone who had mapped the port and enabled
control. The header is still required, but as corroboration rather than as the gate.

With `S2D_MQTT_HOST` set, entities appear in Home Assistant automatically via MQTT
discovery — no custom integration, and nothing else competing for the inverter's single
Modbus slot.

## Probing an installation

Run this first. It reports where the Modbus endpoint is, what the hardware actually says
is installed, and — crucially — how fast it answers, which bounds every "real-time" claim
downstream.

Requires Python 3.12 or newer.

```bash
python3 -m venv .venv && ./.venv/bin/pip install -e .
./.venv/bin/s2d-probe 192.168.1.50 --json probe-report.json
```

Or without installing anything locally:

```bash
docker build -t solar2000direct . \
  && docker run --rm --network host --entrypoint s2d-probe solar2000direct 192.168.1.50
```

Useful flags:

| Flag | Purpose |
|---|---|
| `--port` | Skip port discovery (default: try 502, 503, 6607) |
| `--unit-id` | Skip the unit-ID scan (default: try 0, 1, 100, 2, 3, 11, 16) |
| `--cooldown` | Seconds between requests. Raise to 0.5 if the device drops the connection |
| `--rounds` | Timing samples per register group (default 5) |
| `--skip-optimizers` | Skip the slow per-panel file read |
| `--assume-backup` | Poll backup-box registers even when they read as all-zero |
| `--json` | Write the full machine-readable report to a file |

## Developing without hardware

Pointing test code at a live installation risks knocking Home Assistant off the bus. The
mock speaks enough Modbus TCP to exercise the whole probe path, including realistic
per-request latency and silence on unserved unit IDs:

```bash
python tests/mock_inverter.py --port 5502 --unit-id 1 --battery-units 2 --meter
./.venv/bin/s2d-probe 127.0.0.1 --port 5502 --unit-id 1
```

## Licence

AGPL-3.0-or-later, inherited from `huawei-solar`.

*Not affiliated with Huawei. Huawei, SUN2000 and LUNA2000 are trademarks of Huawei
Technologies Co., Ltd., used only to identify the hardware this talks to.*
