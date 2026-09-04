<img src="solar2000direct/logo.png" alt="solar2000direct" width="250">

Local, real-time telemetry for Huawei SUN2000 / LUNA2000 installations — read straight off
the inverter over Modbus TCP, with no FusionSolar cloud and no five-minute aggregation.

[![Add repository to your Home Assistant][add-repo-badge]][add-repo]

[![Home Assistant add-on][addon-badge]][addon-docs]
[![Supports aarch64][aarch64-badge]][addon-docs]
[![Supports amd64][amd64-badge]][addon-docs]
[![Licence][licence-badge]](LICENSE)

## What it is

A Home Assistant **add-on**, not a HACS integration — HACS does not distribute add-ons, so
this installs through Home Assistant's own add-on store using the button above.

A Huawei inverter serves exactly one Modbus client at a time and answers slowly. This runs
one process that owns that single session and fans the data out four ways:

- a **dashboard** served inside Home Assistant through ingress, with no port to expose and
  no separate login;
- **Home Assistant entities** over MQTT, discovered automatically, suitable as Energy
  Dashboard sources;
- a **JSON API** and Prometheus metrics, for whoever already runs Grafana;
- **local history** in SQLite at poll resolution, so you can look at a past afternoon
  second by second rather than in five-minute averages.

## Install

1. Press the button above, or add `https://github.com/StevenKusters/solar2000direct`
   under **Settings → Add-ons → Add-on Store → ⋮ → Repositories**.
2. Install **solar2000direct** from the store.
3. Put your inverter's IP address in **Inverter address**. That is the only required
   setting — the add-on works the rest out by asking the inverter.
4. Start it, then **Open Web UI**.

Every setting is explained with a worked example in
[`example-config.yaml`](solar2000direct/example-config.yaml), which you can paste whole
into the Configuration tab's YAML view and edit down. Nothing in it is required.

If you are not sure of the address, start it anyway and read the add-on's log: it says what
it connected to and what it found — model, firmware, battery, meter, optimizers. The
`s2d-probe` tool scans ports and unit IDs more thoroughly, but it needs a checkout rather
than the add-on; see [the technical README](solar2000direct/README.md).

> **One client at a time.** If you already run the `wlcrs/huawei_solar` integration, or the
> FusionSolar cloud integration, against the same inverter, disable it first. Two clients
> competing for the single Modbus slot is the usual cause of sensors flapping between a
> value and *unavailable*.

## What it adapts to

Nothing about the installation is configured by hand. On connect the inverter is asked what
it is, and everything follows: how many MPPT strings to read and chart, whether there is a
battery and how many storage units and packs, whether a grid meter is fitted, whether the
supply is single- or three-phase, whether optimizers are present and which strings they are
on, whether there is a Backup Box. Hardware that is not there gets no register read, no
entity, and no card on the dashboard.

Two things it cannot infer, both optional: how many panels are on each string, and what one
panel is rated at. Give it those and the per-panel comparisons and the capacity bar become
meaningful.

## Requirements

- Home Assistant OS or Supervised, on `aarch64` or `amd64`
- A Huawei SUN2000 with Modbus TCP reachable on the network, usually through an SDongle
- An MQTT broker if you want Home Assistant entities — the dashboard works without one

Without Supervisor, the same image runs as a plain Docker container: see
[`solar2000direct/README.md`](solar2000direct/README.md).

## Documentation

- [Add-on documentation](solar2000direct/DOCS.md) — setup, options, troubleshooting
- [Example configuration](solar2000direct/example-config.yaml) — every setting, explained,
  with values from a made-up house
- [Architecture and internals](solar2000direct/README.md) — how the polling is planned,
  what the diagnostic tools do, the API surface
- [Changelog](solar2000direct/CHANGELOG.md)

## Contributing

Issues and pull requests are welcome. There is no test runner to configure: each file is
a script that exits non-zero and says what it checked. Run them from the repository root.

```sh
pip install ./solar2000direct pyyaml
python solar2000direct/tests/test_addon_manifest.py
python solar2000direct/tests/test_history_energy.py
python solar2000direct/tests/test_installation_shapes.py
```

PyYAML is needed only by the manifest test — it parses `config.yaml` with a real parser,
because a manifest was once shipped that was not valid YAML and the add-on silently never
appeared. Nothing at runtime parses YAML, so it stays out of the image.

`solar2000direct/tests/mock_inverter.py` serves a fake SUN2000 over Modbus TCP, and
`mock_dashboard.py` serves the real dashboard against fabricated installations, so most of
this can be worked on without an inverter.

## Licence

[AGPL-3.0-or-later](LICENSE), which is not a free choice: this is built on
[`huawei-solar`](https://github.com/wlcrs/huawei-solar-lib), which carries the register map
and is itself AGPL. Because the add-on also serves a dashboard and an API over the network,
section 13 applies — anyone you let use it is entitled to its source. Running it at home
costs you nothing; publishing a modified version, or offering it as a service, means
publishing your changes too. See [Licence and
attribution](solar2000direct/DOCS.md#licence-and-attribution).

*Not affiliated with Huawei. Huawei, SUN2000 and LUNA2000 are trademarks of Huawei
Technologies Co., Ltd., used only to identify the hardware this talks to.*

[add-repo]: https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2FStevenKusters%2Fsolar2000direct
[add-repo-badge]: https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg
[addon-badge]: https://img.shields.io/badge/Home%20Assistant-add--on-41BDF5?logo=home-assistant&logoColor=white
[addon-docs]: solar2000direct/DOCS.md
[aarch64-badge]: https://img.shields.io/badge/aarch64-yes-success
[amd64-badge]: https://img.shields.io/badge/amd64-yes-success
[licence-badge]: https://img.shields.io/badge/licence-AGPL--3.0-blue
