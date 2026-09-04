# solar2000direct

Real-time telemetry read straight off a Huawei SUN2000 over Modbus TCP. One process owns
the inverter's single Modbus session and serves a dashboard, a JSON API, Home Assistant
entities over MQTT, and local high-resolution history.

## Quick start

1. **Install it.** If you are reading this in Home Assistant you already have.
2. **Open Configuration** and put your inverter's IP address in **Inverter address**.
   That is the only required setting.
3. **Start**, then **Open Web UI**.

That is the whole of it. The add-on asks the inverter what it is and configures itself:
how many strings to read, whether there is a battery and how many cabinets, whether a
meter is fitted, single- or three-phase, optimizers, a Backup Box. Hardware you do not
have produces no entity and no card.

**One thing to check first.** The inverter serves exactly one Modbus client at a time. If
the `wlcrs/huawei_solar` integration or the FusionSolar cloud integration is pointed at
the same inverter, disable it before starting this. Two clients fighting over the one slot
is the usual cause of sensors flapping between a value and *unavailable*.

**If you do not know the IP**, start it anyway and read the **Log** tab — it names what it
connected to. Your router's client list is the other place; the SDongle shows up under a
Huawei MAC address.

## Worth adding once it runs

None of these are required, and the dashboard works without every one of them.

| If you want | Set |
|---|---|
| per-panel comparisons that mean something | **Panels per string**, and **Watts per panel** |
| money figures | **Import price, day tariff** — and see below |
| a cross-check against your utility meter | the **P1** settings, if Home Assistant reads one |
| to switch battery modes from the dashboard | **Allow writing to the inverter** and the installer password |

**Panels per string** is the one worth doing. The inverter reports how many strings it has
but not how many panels hang off each, and a bigger string producing more says nothing
about panel health. Give it the counts and every per-panel figure becomes comparable.

**On prices, enter both halves.** Every price is per kWh in whole currency units, never
cents — 24.5 cents is `0.245`. A bill has the energy and everything charged alongside it:
distribution, transmission, levies, taxes. On many bills the second half is comparable to
the first, so entering only **Import price** understates both what importing costs and
what avoiding it saves. Find the rest on an invoice — total variable charges divided by
kWh billed, minus your energy price — and put it in **Network costs and levies**.

A fully worked example of every setting, with a made-up house to show what real values
look like, is in
[`example-config.yaml`](https://github.com/StevenKusters/solar2000direct/blob/main/solar2000direct/example-config.yaml).
You can paste it wholesale into the YAML view of the Configuration tab and edit down.

## What you get

**The dashboard** is served inside Home Assistant, under Settings → Add-ons →
solar2000direct → Open Web UI. There is no port to expose and no separate login: Home
Assistant authenticates it. To put it in the sidebar, use the *Show in sidebar* toggle on
the add-on page.

**Home Assistant entities** appear automatically over MQTT if you have a broker; the
Supervisor supplies the credentials. Everything is under one device, and the energy
counters are suitable as Energy Dashboard sources. Turn off **Publish to MQTT** if you
would rather have only the dashboard.

**Local history** is kept in SQLite at three resolutions: full poll resolution for the
recent past, one-minute means for a few months, hourly means indefinitely. Both retention
windows are configurable, and the option text says what each costs in disk.

## Changing inverter settings

Saving a *profile* — a snapshot of what the inverter is currently set to — needs nothing
enabled: it reads the inverter and writes a file here.

*Switching* to a saved profile is what the gate protects. With **Allow writing to the
inverter** on and the local installer password filled in, the dashboard can apply one. It never invents a setting, it
only replays one it has seen, and applying a profile always shows the diff and waits for
confirmation first.

The intended use is the seasonal change on a battery system: maximise self-consumption
while the sun is useful, time-of-use with overnight grid charging when it is not.

## The P1 cross-check

If Home Assistant reads a P1 smart meter, point the P1 options at those entities and the
add-on compares them against the inverter's own CT meter. Two independent measurements of
the same thing give the only honest error bar on either. Per-phase entities are what catch
a reversed clamp, which nets out correctly in the totals and so is invisible there.

Three shapes of P1 integration are supported: one signed net sensor, separate
consumption/production sensors (DSMR), and signed per-phase sensors (HomeWizard).

## Troubleshooting

**Nothing appears, and the log says the session keeps ending.** Something else holds the
Modbus slot. Check for the `huawei_solar` integration, another add-on, or the FusionSolar
app's local commissioning mode.

**The dashboard shows a banner saying the figures are not current.** The stream stopped.
It reconnects on its own; the banner appears once the readings are more than five minutes
old, and names the time they were taken.

**Entities exist but stay unknown.** Check that the MQTT integration is configured in Home
Assistant, not merely that a broker is installed. They are separate things.

**Battery round-trip says it has no figure yet.** It needs enough history to see a full
charge and discharge, and enough of the window covered by samples. It measures over
whatever history exists, up to a fortnight, so it fills in as the add-on runs.

## Licence and attribution

Licensed under the **GNU Affero General Public License, version 3 or later**. That is not
a preference: this add-on is built on [`huawei-solar`](https://github.com/wlcrs/huawei-solar-lib),
which carries Huawei's register map and is itself AGPL, and a program that links AGPL code
is covered by the same terms.

What it means in practice:

- **Running it at home costs you nothing.** Use it, change it, keep your changes private.
- **If you give it to someone else** -- a modified image, a fork, a copy for a customer --
  they are entitled to the source of what you gave them.
- **Section 13 goes further than the GPL.** Because this serves a dashboard and an API over
  the network, anyone you let use it over that network is entitled to the source too, even
  if you never hand them a copy of the software.

The full text is in [LICENSE](https://github.com/StevenKusters/solar2000direct/blob/main/LICENSE).

**Not affiliated with Huawei.** This is an independent project. Huawei, SUN2000 and
LUNA2000 are trademarks of Huawei Technologies Co., Ltd., used here only to say which
hardware this software talks to. It is not endorsed by or connected with Huawei, and
nothing here is warranted — reading and writing an inverter's registers is done at your
own risk.

## Support

Issues and source: <https://github.com/StevenKusters/solar2000direct>
