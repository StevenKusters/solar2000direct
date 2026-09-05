# Changelog

Home Assistant shows this when an update is available. Entries describe what changed for
you, and flag anything that needs action on your side.

## 1.0.1

- **A full battery no longer lands in a charge band that cannot exist.** Battery pack
  balance is grouped in twenties, and integer division gave 100% a band of its own,
  labelled "100-119%" — a range no state of charge can be in. The top band now closes at
  100 inclusive.

  Not cosmetic, and it was hiding the readings that matter most. At full charge the BMS
  clamps every pack to 100% while their measured voltages diverge the furthest apart they
  ever get, which is exactly the imbalance this card exists to show. Because the
  high-charge figure is taken across bands rather than across readings, those samples sat
  in a band of their own and were never consulted. On the installation that turned this
  up, three readings carried more than twice the voltage spread of any other band on the
  card and counted for nothing.

## 1.0.0

First public release.

- **A worked example of every setting**, in `example-config.yaml`. It configures a made-up
  house — a 6 kW three-phase inverter, 20 panels of 430 Wp over two roof faces, one battery
  cabinet, a smart meter read by Home Assistant — so that every option has a value beside
  it that shows what a real one looks like. Paste it into the Configuration tab's YAML view
  and edit down, or just read it. Nothing in it is required.
- **The setup instructions are three steps**: install, enter the inverter's IP, start. That
  is genuinely all of it, because the add-on asks the inverter what it is rather than being
  told. Everything else moved to a short table of what each optional setting buys you.
- The example is checked against the add-on's own option list on every build, so it cannot
  quietly document settings that no longer exist or miss ones that do.
- **The routes that write to the inverter now answer on their own socket.** They used to be
  gated on the `X-Ingress-Path` header the Supervisor sets, which is a claim the caller
  makes about itself: `curl -H 'X-Ingress-Path: /'` from any machine on the network
  satisfied it. Anyone who had mapped the optional API port and turned control on could
  write to a grid-connected inverter unauthenticated, while three places in the
  documentation promised that port was read-only. Writes now answer only on the ingress
  port, which is absent from the manifest's `ports` and so cannot be mapped onto the host.
  Running outside Home Assistant, publish it yourself in `docker-compose.yml` if you want
  the control API — nothing guards it once you do.
- **The hourly history was keeping only part of each hour.** Rolling up folded everything
  older than the cutoff, so the bucket straddling it was written from its first half, its
  source rows were deleted, and the next pass replaced it with an average of the tail
  alone. Since the cutoff advances an hour per pass, nearly every row in the permanent tier
  described part of its hour while presenting as the whole of it. Whole buckets are folded
  now, once, when all of their rows are eligible.
- **The day/night split is measured over the window being priced.** It was read from the
  meter's lifetime registers, so a meter whose lifetime import happened to be 55% nocturnal
  priced every window at a 55% night mix — including one that ran entirely in daylight.
- Every setting the Configuration screen shows now has a name and a description. Nine of
  them had neither, so Home Assistant displayed the raw variable name; two of those invited
  you to type credentials the Supervisor already supplies. The check that was supposed to
  catch this compared against the wrong list and passed while they were missing.
- The dashboard escapes inverter serial numbers and collector error text before putting
  them in the page, which the file's own comment claimed it already did everywhere.
- A malformed request body is a 400 rather than a 500 with a stack trace.

## 0.9.1

- **Network costs can now differ between the day and low tariffs**, as they do on the bill:
  distribution is metered per register, so a dual-tariff meter is charged a lower
  distribution rate at night as well as a lower energy rate -- half a cent per kWh on a
  Flemish grid. Leave the new setting at 0 and the day figure is used for both, as before.
  On a site importing two thirds of its energy at night this is worth about 0.7% of the
  grid cost.

## 0.9.0

Comparing a day on the dashboard against the same day in the energy supplier's app turned
up three separate reasons the numbers did not line up.

- **Every window was losing one sampling interval of energy at its start.** Counter
  differences were computed after narrowing to the window, so the step that crosses into it
  had no predecessor and was dropped -- a full sampling interval, every day, always
  downwards. Measured on a steady counter: a 14.40 kWh advance reported as 14.35. Windows
  now reach back far enough to close, and consecutive days tile with no hole between them.
- **The utility's meter is now the one that counts.** Two meters measure the grid
  connection -- the inverter's CT clamp and the fiscal meter -- and they disagree by a few
  percent. Only one of them is billed. Where a P1 feed is configured its counters are
  recorded alongside the inverter's and used for the grid figures and the money; the
  inverter's are kept beside them, and the meter card now compares the two over the
  selected period rather than over their lifetimes, which carry whatever happened before
  the clamp was fitted.
- **Money was priced at the energy rate alone.** Distribution, transmission, levies and VAT
  are charged on every imported kilowatt-hour and are often the larger half of the price: a
  contract at 20 cents of energy can be invoiced at over 50 cents delivered. Two new
  settings, **Network costs and levies, per kWh** and **VAT on electricity**, make the
  figures comparable to a bill. They apply to avoided imports as well, because a kilowatt-
  hour taken from your own roof avoids the distribution charge exactly as it avoids the
  energy charge. Both default to 0, so nothing changes until they are filled in.
- The energy note now says which meter the grid figures came from and what a kilowatt-hour
  was priced at.

## 0.8.1

The rest of the pre-publication review — the findings that were not blockers.

- **Old data was reachable but never counted.** Rows in the hourly tier sit an hour apart
  by construction, and were measured against a 300-second ceiling meant for outages, so
  every one was discarded as a gap: every window older than the minute retention integrated
  to nothing. Each tier now carries its own allowance.
- **Buckets are cut against the offset that applied on the day**, not today's. A window
  spanning a clock change had one side cut in the wrong place, and the further back you
  looked the more of it was wrong.
- **The P1 poller no longer downloads Home Assistant's whole state machine every ten
  seconds** to keep at most eight entities out of it. It asks for the ones it was given.
- **The history task survives a database it cannot open**, retrying and saying so, instead
  of ending silently while everything else carries on. Write failures are logged too, on a
  backing-off schedule rather than at the sample rate.
- **A wrong installer password now says so** instead of returning a 500: the branch meant
  to catch it tested a value that is never false.
- **Malformed query parameters return a 400**, not a traceback.
- Per-string columns are gone from the sample schema — a wide row has to fix its columns
  and the number of strings is a property of the installation, which is what the comment
  three lines above it warned against.
- An unrecognised unit is reported once rather than every ten seconds forever; mismatched
  per-phase lists are refused rather than silently truncated; a plan that validates to
  nothing says which device and what was rejected instead of failing on an empty sequence.
- **The register names are checked against the library at startup.** The docstring had
  claimed this for months and no such check existed. Turning it on immediately found one:
  a register added in 0.7.0 that this library build does not have.
- Lint is configured for the rules the code is annotated against, and passes. The image and
  the repository carry the licence, the dashboard carries the source offer that AGPL
  section 13 requires, and nothing claims a Huawei affiliation.

## 0.8.0

Preparing the repository to be published, and fixing what a review of it turned up.

**Security**

- **Changing the inverter now requires going through Home Assistant.** The two endpoints
  that write settings were reachable by anyone who could reach the port, with no
  authentication and nothing to stop a web page on another origin posting to them. They now
  refuse anything that did not arrive through ingress, which is also what makes them
  immune to a cross-site request.
- **A profile name can no longer inject script into the dashboard.** Names went into the
  page as raw markup, and the dashboard is served from the Home Assistant origin, so
  anything injected ran with the frontend's own storage in reach. Names are now restricted
  on the way in and escaped on the way out, along with every other value the page renders
  from a response.

**Data loss**

- **Thinning old history was deleting it.** Counters, per-panel and per-pack readings older
  than the minute window were meant to be kept at one row an hour; the rule tested for
  timestamps landing exactly on the hour, and rows are written on whatever second the tick
  fell on. Measured over 120 days: 25,920 rows older than the cutoff, 0 survivors, about
  2,160 intended. It now keeps the first row of each hour. Nobody has reached 90 days of
  history yet, so no installation has lost anything.

**Correctness**

- **A meter going offline no longer traps the collector in a reconnect loop.** The check
  added in 0.7.4 compared the meter's status against the capability set, but the status
  survived from the previous session, so a meter that had just gone offline reconnected
  forever without pausing.
- **The freshness clock no longer advances on a pass where every read failed**, which made
  the staleness warning, the health endpoint and the history writer all report healthy
  while nothing had arrived.
- `s2d-bench` crashed with a `NameError` before running a single benchmark, and had since
  it was first packaged.

**Packaging**

- The version was declared in three files and had drifted eleven releases apart; they are
  checked against each other now.
- Dependencies gained upper bounds. The image is built on your machine and resolves from
  PyPI at build time, so an upstream major release would otherwise land on everyone at once.
- The add-on now has a LICENCE (AGPL-3.0-or-later, inherited from `huawei-solar`), an icon,
  a logo and a Documentation tab.
- `.env.example` was missing eight settings the code reads, including two whole P1 meter
  shapes. Both READMEs told a stuck user to open an add-on terminal, which Home Assistant
  does not provide.
- Continuous integration runs the three test scripts, a lint pass and a Docker build.

## 0.7.7

- **Tap or click a name in a chart's key to put that line away, and again to bring it
  back.** Six series at once is a lot to read, and the axis rescales around what is left --
  hiding solar is what lets you see the grid and battery detail underneath it, which is the
  whole point, so the chart is redrawn rather than the line merely hidden.
- A series that is put away stays listed, struck through, so there is always a way back.
  The hover readout follows: it lists what is drawn, not what was configured.
- The choice is remembered per chart between visits.

## 0.7.6

The energy card was telling you two incompatible things about the same day, and charging
you money for one of them.

- **"Self-consumed" counted energy that was still in the battery.** It was production minus
  export, which is only self-consumption over a window where the battery ends as it
  started. On the reported day it claimed 26.22 kWh self-consumed against a house that used
  25.53 in total, 9.08 of which came off the grid. The 9.77 kWh gap was exactly the 9.19
  kWh still stored at midnight plus 0.58 kWh of conversion loss.
- **"From solar" counted energy that came out of the battery**, some of which the grid had
  put there on a previous night.
- **Both are now measured rather than inferred.** Every sample already records solar,
  house, grid and battery at the same instant, and at an instant it is not a question where
  a watt went: production splits into used in the house, stored in the battery, fed to the
  grid and conversion loss; consumption splits into from solar, from the battery and from
  the grid. Each set sums to its total, and the two agree with each other.
- **Grid charging is measured too.** How much of the battery's charge came from the grid
  rather than the roof could not be recovered from daily totals; per sample it is simply
  the charge beyond what the panels had spare.
- **Benefit was overstated.** It valued production minus export at the retail price, so it
  booked the kilowatt-hours parked in the battery as money saved today, and the conversion
  loss with them: EUR 4.11 where EUR 2.59 was earned. It now values what the house actually
  took from solar and the battery instead of buying. Solar banked in the battery earns its
  saving on the day it is discharged.

## 0.7.5

- **The energy card now totals both directions of both flows.** It showed what went into
  the battery and what came from the grid, while the chart underneath showed all four, so
  half of each pair had a bar and no total. "From battery" and "To grid" join them.
- **The battery round-trip card fills in as soon as there is history to fill it.** It asked
  a fixed fourteen-day question, so a five-day-old installation answered "36% of the window
  has samples" and refused -- correctly, and uselessly, for another nine days. It now
  measures over whatever history exists and says in the table how long that was.
- When it has no figure it says what it is waiting for rather than only that it is waiting.
- **The add-on has an icon, a logo and a Documentation tab.** The Supervisor looks for
  icon.png, logo.png and DOCS.md by name; without them the add-on shows a placeholder and
  no documentation at all.
- The changelog headings are checked to stay bare version numbers. Home Assistant filters
  release notes to the entries between your version and the latest with a pattern that
  needs the version to be the whole heading; a title after it silently shows you the entire
  changelog instead of what you are about to install.

Two attributes on the update entity cannot be filled from here: Home Assistant's add-on
update entity does not implement Release URL or Release summary at all, whatever the
manifest says.

## 0.7.4

The last of the generalisation audit, and one layout change.

- **"Now" has moved above the charts**, between the power flow and the energy cards, so the
  live figures sit with the diagram they belong to rather than below a screen of history.

Settings that existed in the code but could not be reached:

- **Publish to MQTT** can be turned off, for installers who already run a broker for other
  devices and want the dashboard without a few dozen new entities.
- **MQTT discovery prefix**, for a Home Assistant configured with a non-default one, which
  happens when two instances share a broker. Entities were being published where nothing
  was listening, with no way to correct it.
- **Backup Box fitted**, as auto/yes/no. Detection reads the backup reserve, so an owner
  who set theirs to 0% read as having no Backup Box at all.
- **Currencies outside EUR, GBP and USD** now print their code rather than nothing. "3.42
  saved" gave no clue what it was 3.42 of.

Figures that reported an absence as a measurement:

- **A site with no grid meter no longer reports 0.00 kWh imported and exported** every day,
  alongside a house consumption equal to inverter output -- which is only true if there is
  no grid. Those now read as unmeasured.
- **The whole-system round-trip figure is withheld when there is no house measurement**
  rather than counting every kWh of production as a loss. The battery's own ratio needs
  none of that and is still shown.
- **A single-module battery is told it has one module**, not that there are no pack
  readings, which was untrue while thousands sat in the table.

Installations that could not be configured at all:

- **P1 meters that publish no signed total** -- Home Assistant's DSMR integration, which
  has separate consumption and production sensors -- can now be used for the cross-check.
- **P1 meters reporting each phase as one signed value**, such as HomeWizard, can now be
  used for the per-phase comparison, which is the part that catches a reversed clamp.

And two detection fixes:

- **A grid meter that comes online after startup is now picked up.** Presence was decided
  once when the session opened, so a meter still initialising, on its own breaker, or an
  inverter that happened to be offline at that moment left the whole session with no grid
  power and no house load until something unrelated dropped the connection.
- **s2d-probe now agrees with the add-on about the Backup Box.** It read both registers in
  one batch, so on firmware that refuses one of the addresses a fitted box reported as
  absent -- which is what the reference site's own probe report records.

## 0.7.3

- **The dashboard's own hints now say where to go and what to type.** "Set an energy price
  in the add-on options" named no field and no format; it now names the path, the label, and
  the figure -- type 0.245 for 24.5 cents, not 24.5.
- The strings card explains its own blanks. The per-panel columns are dashes until it knows
  how many panels each string holds, which the inverter cannot work out, so the note says
  exactly that and how to fill it in: add "8", then add "12".
- The panel card distinguishes "no optimizers fitted, so there is nothing finer than string
  totals" from "optimizers are reported but no data has arrived yet", which used to be one
  message reading "No optimizer data."
- The read-only battery-mode card names both settings that unlock it, and says the password
  is the local commissioning one rather than the FusionSolar account.
- The profile name box suggests "Winter" instead of "name", and the note says a profile is
  a season rather than a setting.
- Every option label the dashboard quotes is checked against the options page at test time.
  Renaming a setting without updating the hint sends the reader hunting for a field that is
  no longer called that, and now fails the build instead.

## 0.7.2

- **Every option that expects a particular format now shows the exact text to type.**
  "Panels per string: in string order, for example 8 and 12" said what the values were and
  left you to guess the form -- comma-separated, one box, or separate entries. It is the
  third, and it says so: add "8", then add "12".
- The same treatment throughout. Prices show a worked figure, because typing 24.5 where
  0.245 belongs is a hundredfold error that looks perfectly reasonable on the page. The
  P1 fields show a whole entity ID, domain included. Every repeating field says that
  entries go in one at a time.
- Descriptions that had none now have one: per-panel interval, currency, the P1 export,
  tariff and peak-demand entities.
- Three tests hold the convention: repeating fields must say entries are added separately,
  entity fields must show a complete entity ID, price fields must show a worked figure. The
  first of them immediately caught one description I had left vague.

## 0.7.1

- **Fixed the add-on options page showing every setting by its variable name.** 0.7.0 added
  a description for the new `panel_watts` option in the wrong shape -- a bare line of text
  where Home Assistant expects a name and a description. It is still valid YAML, so nothing
  complained; Home Assistant simply could not read the file and fell back to raw keys for
  the whole page. There is now a test for the shape, not just for the presence.
- **Which strings carry optimizers now comes from the inverter.** The optimizer file
  reports a string number and a position for every optimizer, and that was being thrown
  away and asked for in the configuration instead -- as a single string number, which
  cannot describe an array with optimizers on more than one string. The panel view now says
  what it actually covers, whether that is one string, several, or all of them.
- **Panels are compared against the other panels on their own string.** Ranking an
  east-facing panel against a west-facing median marks a whole orientation as
  underperforming, which it is not. Where the inverter reports the wiring, each panel is
  measured against its own string, and the note says which comparison was used. Strings
  with fewer than three panels still fall back to the whole array.
- `optimizer_string` stays only as a fallback for firmware that does not report the wiring,
  and says so. Leave it at 0.

## 0.7.0

This release is about installations that are not the one it was developed on. An audit
turned up 44 places where a property of that system had been written down as if it were
universal -- the optimizer arrangement was one, and the least of them.

- **The number of MPPT strings now comes from the inverter.** Two were polled, charted and
  published, because the reference inverter has two. A three- or four-input inverter had
  half its array invisible everywhere -- no power, no voltage, no lifetime yield, and a
  headline lifetime figure that was the sum of the first two strings presented as the whole
  roof. Reading all of them costs no extra round-trip: they sit inside a block already
  being read.
- **A single-battery installation now polls the battery it has.** Both storage units were
  behind the "a second unit exists" check, so a one-cabinet site never read its only unit
  and Home Assistant was given a battery entity that stayed unknown for as long as the
  add-on ran. Its battery power and charge level were also arriving once a minute beside a
  four-second solar figure; they are live now.
- **Single-phase installations no longer report two phases that are not there.** Phase B
  and C read zero rather than failing, which is worse than failing: a reading of zero is
  indistinguishable from a fault.
- **A PV-only installation gets its figures.** "PV production (AC)" and "Served without the
  grid" were computed only where a battery was fitted, but published everywhere, so on a
  system with no battery both entities existed and never filled in.
- **A single-module battery is described rather than ignored.** Pack figures needed two
  packs to appear at all, and the temperature spread mistook one module's own top-to-bottom
  gradient for drift between modules.
- **Home Assistant is no longer given entities for absent hardware.** The five P1
  cross-check entities were created on every installation although the P1 feed is off by
  default; phase B and C entities on single-phase sites; a battery unit 2 status naming a
  register that was never read.
- **The dashboard shows only what is fitted.** Cards for the battery, its mode, its pack
  balance, its round-trip, the panel view and the meter cross-check appear when that
  hardware is present. The flow diagram drops nodes for what is not there, the string and
  phase and battery tables follow the real counts, and the solar tile is measured against
  this array rather than against a array it was developed on.
- **An inverter without optimizer support can now start at all.** The startup read asked
  for the optimizer count in one batch with the model and serial. On a model that answers
  "no such register" -- which the library documents as ordinary -- the whole read failed,
  the session ended, and it reconnected forever without ever collecting anything.
- New option, both optional: `panel_watts`, which with the panel counts gives the array's
  capacity. Without it the inverter's rated power is used.

Nothing changes for an installation like the reference one: same three live reads, same
entities, same cards.

## 0.6.10

- **The stale-data warning now waits five minutes instead of fifteen seconds.** Fifteen
  was under four missed readings -- a brief stall on the Modbus bus was enough to raise a
  banner, and a banner you see for every hiccup is one you learn to ignore.
- Reconnecting still happens after fifteen seconds, because that part was never the
  problem: the stream is plainly dead by then and there is nothing to gain by waiting. It
  is silent now, so a short interruption is usually repaired before you notice it.
- In between the two, the indicator in the corner turns amber and says how long it has
  been. Quiet, but never showing an old reading as though it were current.

## 0.6.9

- **The dashboard now notices when it stops receiving readings.** If the live stream went
  quiet without the browser registering an error -- a laptop coming back from sleep, a
  network that changed underneath it, the add-on restarting -- the page kept showing its
  last reading indefinitely. Nothing looked wrong: the figures were consistent with each
  other and a stale night-time reading looks exactly like a quiet night. Reloading was the
  only way to find out.
- The cause was that the freshness indicator was written only when a reading arrived, so
  it could report freshness but never the absence of it. It now runs on a clock of its own.
- After fifteen seconds of silence a strip appears across the top saying the figures are
  not current and naming the time they were taken, the indicator turns red and counts up,
  and the page reconnects by itself. Coming back to the tab or regaining a network
  connection prompts an immediate attempt rather than waiting.

## 0.6.8

- **Older data now keeps charge and discharge apart too.** 0.6.7 noted a limit: readings
  are averaged down to one a minute after a week, and that averaging cancelled charging
  against discharging before it was ever stored, so a minute that did both was kept as a
  single idle-looking row. The coarser tiers now carry the charging half alongside the
  signed average, and the discharging half follows exactly from the two -- so a minute
  that charged at 2 kW for thirty seconds and discharged at 2 kW for thirty now reads as
  1 kW each way instead of nothing at all. It survives the second rollup into hours as
  well, and the energy charts benefit alongside the power chart.
- Costs about 11% more disk (2.4 MB on a database of 22 MB at the default retention).
  Storing all four directions outright would have been 22%; the fourth number is implied
  by the other three, and a redundant copy is one more thing that can disagree.
- Your existing database is upgraded in place on first start. Readings already rolled up
  cannot be recovered -- the information was discarded when they were written -- so they
  are filled in with what the charts already showed for them: the dominant direction, and
  nothing the other way. Nothing you have been looking at changes; everything from here on
  is exact.

## 0.6.7

- **The power chart now splits the battery and the grid by direction too.** One signed
  line per flow made charging and discharging something you had to read off the zero axis,
  and it hid the mixed case entirely -- which is what you were seeing when the line chart
  showed a clear discharge that the bar underneath reported as pure charging.
- **The split is computed before the readings are averaged.** Each point on that chart is
  a mean over a bucket, so a few minutes of charging against a few of discharging came
  back as gentle charging. Splitting first keeps both: a bucket that charged at 2 kW and
  discharged at 1.5 kW now reports 1000 W in and 750 W out rather than 250 W in.
- With every series now non-negative, the chart starts at zero instead of reserving a
  third of its height for momentary spikes below the axis.
- **A series whose first reading was missing used to vanish for the whole window.** The
  path opened with a lineto, which is invalid, so the browser drew nothing at all. It now
  starts where its data starts.
- Missing data is a break in the line rather than a straight line drawn across it, and a
  lone reading between two gaps is drawn as a dot instead of nothing.
- Readings just under a kilowatt read "1.00 kW" rather than "1000 W".
- Series values are rounded to a tenth of a watt, which pays for the four new fields: the
  day response grew 8%, not 46%.

Older data is coarser than it looks: readings are averaged down to one a minute after a
week and one an hour after ninety days, and that averaging cancels charge against
discharge before it is stored. The split is exact for the last week and progressively
understates both directions before that.

## 0.6.6

- **The charts now show energy going *into* the battery and the grid, not only what came
  out.** Solar above house load has to go somewhere, and within one bucket it can go both
  ways -- charging early in the quarter-hour and covering a peak later in it. "To battery"
  and "To grid" are drawn in the same colour as their opposite, faintly, so each pair
  reads as one quantity in two directions. The period totals gained a "To battery" tile.
- **Fixed House use, Self-sufficiency and Benefit reading blank over any window with no
  export.** A counter that was read all window but never advanced was dropped from the
  results entirely, so "exported nothing" was indistinguishable from "no export data" --
  and the house consumption figure is not computed without an export figure. It now
  reports zero, which is what actually happened. Counters genuinely absent stay absent.
- The readout no longer hangs out of the card when it is taller than the chart.

## 0.6.5

- **The selected bar is now shaded rather than marked with a hairline.** A bar already
  occupies a width, so a dashed line through it said less than shading the column it
  stands in. The shading takes in the time label below the bars as well.
- **Tapping a bar on a phone now works.** There is no hover on a touch screen, so a tap
  is the question: it highlights the bar and shows the readout, and stays there until you
  scroll or touch something else. Dragging across the chart still zooms.
- **The readout no longer covers the bar you are reading.** It goes beside the highlighted
  column when there is room and below the chart when there is not, instead of floating
  over the bars on a narrow screen.

## 0.6.4

- **Bucketed charts now name both ends of the period they cover.** "Energy per hour" put
  a single time under each bar, leaving it ambiguous whether 12:00 AM meant the hour
  before or the hour after. Hovering a bar now reads "Aug 29, 12:00 AM-01:00 AM".
- The heading says which convention the axis follows: each bar covers the hour (or 15
  minutes, or day) *starting* at its label.
- Day and month bars are headed by the period itself -- "Sat, Aug 29", "August 2026" --
  rather than a raw 2026-08-29.

## 0.6.3

- **Fixed a negative "met without the grid" percentage.** It was computed as one minus
  grid import over house load, which assumes every watt drawn from the grid goes to the
  house. When the grid is also charging the battery, import exceeds house load and the
  figure goes negative -- it read -33% while the grid supplied 5.25 kW to a 3.95 kW house
  and a 1.30 kW charge.
- It now measures what is actually serving the house: solar plus battery *discharge*. A
  charging battery contributes nothing, because it is a consumer. The result cannot fall
  below 0% or exceed 100%.
- Also published as an entity, "Served without the grid".

## 0.6.2

- **Power-flow labels no longer collide with the diagram on a wide screen.** Two faults,
  both mine. The labels were anchored so they grew *towards* their node rather than away
  from it, which is invisible while the text is small and lands on top of the circle once
  it is not. And because text inside an SVG scales with the SVG, a wide window drew them
  at 18 pixels against geometry laid out for 12.
- Labels are now sized in viewBox units derived from the measured scale, so they render at
  a constant 13 pixels at any window width instead of growing with it. Measured across
  five widths from 286 to 1072 pixels: no overlap, nothing clipped, identical text size
  throughout.

## 0.6.1

- **Readable power-flow labels on a phone.** Everything inside an SVG scales with it, so a
  diagram drawn 720 units wide inside a 340-pixel card rendered its 12-pixel labels at
  under six. Below 520 pixels the diagram now uses a compact geometry in a smaller
  viewBox, so it is scaled down far less: labels land at about 12 pixels rather than 6,
  and the values under them at 10.5 rather than 5. Wider screens are unchanged, and the
  layout switches on its own when the window is resized.

## 0.6.0

- **Drag a range on any energy chart to zoom into it.** Works with a mouse and with a
  finger. The selection snaps to whole intervals -- five minutes on the day chart, an hour
  or a day on the longer views -- so choosing a range is not a test of aim, and a drag that
  lands near either end takes the end exactly. Dragging to the end of the graph is a
  gesture rather than a pixel hunt.
- **Zooming reveals more, not the same data stretched.** Resolution follows the window:
  under six hours the finer chart switches to fifteen-minute buckets, and the main chart
  re-fetches at the resolution the new window allows.
- The selected range appears beside the period label with a control to clear it. Stepping
  to another period or switching tabs clears it too, since a zoom belongs to the period it
  was taken from, and the background refresh leaves a selected range alone.

## 0.5.1

- **Fixed the dashboard being wider than a phone screen.** The page is a CSS grid, and a
  grid column is sized to its widest child's minimum content -- so one unbreakable row
  anywhere widened every card on the page and the whole document scrolled sideways. The
  culprit was the chart legends, which forced four entries onto a single row.
- Legends now wrap, tables that genuinely need the width scroll inside their own card
  rather than taking the page with them, and columns are capped so nothing can widen the
  layout again.
- Tighter padding and slightly smaller tiles and panel cells below 640 pixels.

## 0.5.0

- **Every setting is now visible in the configuration, with its value.** Options that
  relied on a default built into the code did not appear at all, so the only way to learn
  what `history_full_days` was set to was to read the source. All 32 now carry an explicit
  value.
- **Each one explains itself in the Home Assistant UI**, including why it is set the way
  it is -- what a live interval costs in Modbus round-trips, why panel counts cannot be
  read from the inverter, which password the installer field wants.
- **Battery packs are read every five minutes rather than every minute**, matching how
  often they are recorded. Reading them more often than they are stored spent bus time
  for nothing.
- Escape hatches stay settable but out of the way: MQTT and Home Assistant connection
  details come from the Supervisor, and the Modbus timeouts and meter sign override only
  matter when something is unusual.

## 0.4.0

- **Scroll back through history.** Arrows either side of the period label step to the
  previous day, month or year, with a "back to now" shortcut. The forward arrow stops at
  the present, switching period returns you to the current one, and a browsed period is
  not dragged back to now by the background refresh.
- **Full-resolution history now covers one day, not seven.** Full detail is what makes
  today worth looking at closely; for anything older a minute is finer than the question
  being asked. **Action:** on first run after updating, samples older than a day roll down
  to one-minute averages. Raise `history_full_days` before updating if you want to keep
  more -- a week costs about 7 MB a year, so this is a judgement about usefulness rather
  than space.
- **A third fewer database writes.** Energy counters and per-pack readings were recorded
  every minute, which was 37% of all writes for data that barely moves and is only ever
  read as differences over a window. Both now record every five minutes. Combined with the
  retention change, the ninety-day working set drops from about 118 MB to 28 MB.

## 0.3.0

- **A second chart under each period.** Every view now has a coarse chart answering "what
  did this period do" and a finer one answering "when within it":
  - **Day** — the power line, with energy per hour beneath it.
  - **Month** — daily totals, with energy per hour across the whole month.
  - **Year** — monthly totals, with energy per day.
- The fine charts are integrated from the power samples rather than differenced from the
  meter counters, because no counter is reported at that resolution -- and it is the only
  way to include house consumption, which no counter reports at all.
- Buckets are cut in local time. Cutting from the UTC epoch made a "day" run 02:00 to
  02:00 in CEST, which is not a day.
- Only whole buckets are drawn: the hour in progress is always short and would read as a
  collapse rather than as an hour that has not finished.

## 0.2.1

- **Fixed the chart tooltip being squeezed at the edges.** The box had no fixed width, so
  near the right-hand edge the browser had only a sliver to render into and wrapped the
  text mid-value. It is now measured before it is placed, pinned to that width so nothing
  can re-squeeze it, and flipped to the other side of the cursor when it would run off the
  end. Where the chart is narrower than the box itself and no placement fits, it wraps and
  fills the width instead.

## 0.2.0

- **Changelog.** Releases now carry one, shown here in the update dialog. A test refuses
  to accept a version without an entry, so it cannot quietly stop happening.

## 0.1.9

- **Hover readout on the energy charts.** Move the pointer over the Day chart and every
  series is shown for that exact moment, with a guide line and a marker on each line. The
  Month and Year bars behave the same way, keyed to the period under the cursor.

## 0.1.8

- **New sensor: PV production (AC).** `pv_power_w` is measured on the DC side, so any flow
  diagram built on it never quite closes — solar reads a couple of percent above the sum
  of everything it feeds. The new `pv_power_ac_w` is in the same units as the rest of the
  diagram and balances exactly. The DC figure remains, since it is the right one for
  judging what the roof produced rather than what reached the house.

## 0.1.7

- **Status entities read as text, not numbers.** Several registers decode to enumerations,
  which were being published as their underlying integer — a meter status reading `1`
  instead of `Normal`. Also improves the battery working mode shown in the control panel.

## 0.1.6

- **Alarms are published.** The three alarm registers were read every few seconds and
  discarded, because they decode to lists and the payload carried only single values. Now
  exposed as a count, a severity, and a readable line, plus a **problem** binary sensor to
  automate on without having to enumerate Huawei's alarm catalogue.
- Meter and battery-unit running statuses are published too.

## 0.1.5

- **Fixed misleading Energy Dashboard candidates.** The per-string *per-panel* figures were
  offered as energy sources; selecting one would have reported a fraction of actual
  generation. They keep their unit but no longer advertise as energy.
- **Renamed two entities.** "Yield today" is now **"Inverter output today"** — it is the
  inverter's AC output, which includes battery discharge, so choosing it as solar
  production would count stored solar twice. The lifetime PV counter is now **"Solar
  production (lifetime)"**, which is the one the Energy Dashboard wants.

## 0.1.4

- **Battery pack balance** and **battery round-trip** sections on the dashboard. Pack
  spread is grouped by the charge level it was measured at, because only a reading above
  80% distinguishes real imbalance from the drift in the charge estimate. Round-trip shows
  measured efficiency against the day/night price gap it requires.

## 0.1.3

- **Corrected the grid meter sign.** Huawei's meter is *negative* when importing; this
  add-on assumed the opposite. Confirmed against a P1 meter reading the same instant.
  **Action:** `grid_power_w` is now positive when importing, so it reads opposite to
  before. That matches the P1 convention. Per-phase meter figures changed with it. Sites
  whose current transformers are genuinely reversed can set `grid_import_is_positive`.

## 0.1.2

- **Fixed missing per-panel entities.** Discovery was published once, before the optimizer
  list had been read, so the per-panel entities were never created and a restart lost the
  same race again. Entities are now announced as they become known.

## 0.1.1

- First working add-on release. The previous manifest was not valid YAML, so the
  Supervisor skipped it silently — nothing appeared in the store and nothing said why.

## 0.1.0

- Initial version: one Modbus session, tiered polling with reads packed by address, MQTT
  discovery, local history, a live dashboard, and battery mode profiles.
