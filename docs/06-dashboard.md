# Dashboard

`http://<this-machine's-LAN-IP>:8090` — no authentication, intended for LAN
access only. Auto-refreshes every 30s. Start it with
`./scripts/webapp_ctl.sh start`.

## Menu

Every page opens with the same menu bar, pinned to the top while scrolling:
**Dashboard · Inverter history · Panel history · Hourly ranking**, with the
current page highlighted (an optimizer's detail page counts as Panel
history). On phones the four links sit in a 2 × 2 grid so none is hidden.
Defined once in `renderNav()` / `NAV` in `webapp/static/sources.js` — add a
page there, not in each HTML file.

## Data health flags

A strip at the top of both pages judges each source *right now*:

| Flag | States |
|---|---|
| **Inverter data** (Modbus, LAN) | `LIVE` (≥90% of reads in the last hour succeeded) · `DEGRADED` (50–90%) · `UNRELIABLE` (<50%; "now" figures may be minutes old, energy totals still correct) · `UNAVAILABLE` (no read for 10+ min) · `NOT CONFIGURED` |
| **Per-panel data** (SolarEdge, internet) | `CURRENT` (newest measurement ≤15 min old) · `LAGGING` (15–60 min) · `BEHIND` (>60 min) · after production stops: `IDLE` if today's data reaches the time the inverter last produced, otherwise `MISSING <duration>` |

When per-panel data is lagging or missing, sections built on it (the
Optimizers table, Health verdict, and the optimizer detail page) show a
warning banner. The **Data sources & reliability** section at the bottom
of the dashboard explains what each source provides, how each fails, and
which to trust for what. In short: late SolarEdge readings are still
correct and analysed at their measurement time, so verdicts stay valid;
what's lost is freshness and completeness. For whole-system figures the
inverter is the more reliable source; for individual panels SolarEdge is
the only one.

## Where each number comes from

Every card, section and table column carries a source tag (cards and
headings) or a coloured underline (table columns), defined once in
`webapp/static/sources.js`:

| Tag | Source |
|---|---|
| **Inverter** (cyan) | Read directly from the inverter over Modbus on the LAN — real time |
| **SolarEdge** (violet) | From SolarEdge's servers — what the SolarEdge app shows, and can lag hours |
| **Computed** (pink) | Calculated by our code; "Computed · Inverter data" / "· SolarEdge data" names what it was derived from |

When the dashboard disagrees with the SolarEdge app, check the tags first:
Inverter figures are live, SolarEdge figures (and the app) are only as
current as the inverter's last successful upload — see *Cloud Delay*.

## Accuracy markers

Figures that may not be exact carry an orange asterisk and number (`*6`).
Hovering gives the one-line reason; the **Accuracy notes** panel at the
bottom of each page explains the ones used on that page. Numbers are fixed
across pages (defined in `ACCURACY` in `webapp/static/sources.js`), so `*2`
always means SolarEdge delivery lag.

| # | Caveat |
|---|---|
| *1 | SolarEdge readings are one instant each; near-zero ones are real but individual readings say little |
| *2 | SolarEdge data is only as current as the inverter's last upload |
| *3 | Daylight avg/median/max/near-0 come from individual readings |
| *4 | Verdict / vs Peers are relative to peers (whole array unless `PANEL_GROUPS` is set); can't tell shade from fault alone |
| *5 | Per-report energy is shown but not used — not real Wh and tracks the inverter poorly |
| *6 | Today's peak is the highest 30 s sample captured |
| *7 | Inverter metering isn't revenue-grade; "now" can be minutes old |
| *8 | Optimizer count is SolarEdge's layout, which can include replaced units |
| *9 | Chart lines bridge short gaps |
| *10 | Gaps / data age can be delivery stalls, not the optimizer |
| *11 | Hourly profile has few samples per hour |
| *12 | Average DC power is time-weighted over the readings captured |
| *13 | Partial day — Modbus didn't see all of production |
| *14 | Per-panel daily energy: measured (delivered readings only) vs estimated (inverter's energy shared out), and coverage |
| *15 | Hourly ranking: only complete hours are ranked; close finishes are near-ties; whole-array ranks reflect placement |

## Status cards

| Card | Meaning |
|---|---|
| **Poller** | ACTIVE if a cycle succeeded within 3 intervals |
| **Poll Cycles (24h)** | Completed vs expected, plus failures on our side |
| **SolarEdge Requests (24h)** | Measured request count, and the projected daily rate |
| **Rate Limited** | Non-zero means SolarEdge is throttling — raise the interval |
| **Optimizers / Reading Records / DB Size** | Volume |
| **Failures (1h)** | Our network/auth problems only |

## Inverter today & cloud upload delay

Live from Modbus, sampled every 30 s: DC power, inverter status, today's
peak DC (shown against `ARRAY_NAMEPLATE_W` if set), average DC power while
producing (time-weighted), energy today from the
inverter's own counter, and the Modbus success rate for the last hour.

**Cloud Delay** is poll time minus the newest optimizer measurement in that
poll — normally ~2 min. Red above 15 min while the inverter is producing:
SolarEdge is receiving uploads late and per-panel data for that period will
be late or missing. Blank when the inverter isn't producing, because
optimizers legitimately stop reporting after dark. The chart overlays DC
power and delay for the day.

## Inverter history (`inverter.html`)

The page opens with **Last 7 days — energy hour by hour**: whole-system
energy in each clock hour from the inverter's own counter. *Compare days*
draws one line per day across the hours (older days dimmer, today
brightest, each line named at its end); *Timeline* joins the same days into
one continuous line. Days without inverter readings are listed, not drawn
as zero.

Next, **Last 7 days — running total through the day**: energy produced so
far at each hour boundary, one line per day, ending at the day's total — to
see whether today is ahead of or behind earlier days at the same time.
Days the inverter was only partly read are left out (their total would
start mid-day), and a line stops at a missing hour rather than
under-reading. Below both:

Every card in the inverter section links here, opened on the matching
metric: energy per day, peak and average DC power, production hours, grid
AC voltage range, heatsink temperature and throttled/fault readings, Modbus
read success, and SolarEdge per-panel coverage. Pick 30 / 90 / 365 days or
everything. Clicking a day (bar or table row) shows that day's full
30-second power curve, with AC power, grid voltage and heatsink temperature
available as toggles. Days marked **partial** weren't fully seen by Modbus;
per-panel coverage below 100% means part of that day's SolarEdge data never
arrived. See `inverter_daily` in [03-data-model.md](03-data-model.md).

## Panel history (`panels.html`)

A grid of every panel × every day (14 / 30 / 90 / 365 days), switchable
between vs peers, estimated energy, measured energy, time near 0 A,
per-panel data coverage and **change vs previous day**, with a total or
median per panel.

*Change vs previous day* colours each cell by that panel's energy against
its own previous day — green ▲ more, red ▼ less, grey ≈ within ±2%, with
stronger colour for bigger changes — and counts days up / down per panel.
A **Whole system** row from the inverter's daily total sits on top: day to
day, weather moves every panel together, so the panel that is red while the
system is green is the one worth a look. Estimated energy is used when both
days have it, otherwise measured (never mixed). A day isn't compared when
it's too incomplete to mean anything — under 75% of its per-panel data, a
partial inverter day, or the first day of collection (which starts part-way
through the day) — and shows "·" with the reason on hover. Colours run red
→ green within the metric; dashed cells had under 90% coverage; ⚠ marks days
the inverter was only partly read. Built for spotting a panel that drifts
down over weeks. Linked from the Health verdict heading; click a panel for
its detail page. The detail page itself now opens with that panel's daily
energy chart (estimated and measured bars, coverage line). See
`panel_daily` in [03-data-model.md](03-data-model.md).

## Hourly panel ranking (`hourly.html`)

Pick a **date** and an **hour** (or *All day*) and rank against the **whole
array** or **within each group** (groups come from `PANEL_GROUPS`; with
the whole-array default the *Within group* button is disabled):

- **One hour**: every panel's energy that hour, highest first, with 🏆 🥈 🥉,
  % of the hour's best (or of its group's best), average power and coverage,
  alongside the inverter's own energy for the hour.
- **All day**: panels × hours grid of energy, shaded by share of the hour's
  best (single-hue scale, brighter = closer), 🏆 on each hour's winner,
  faded columns for hours that aren't ranked, and the day's wins / top-3 /
  average rank per panel. Click an hour header to open its ranking.
- **Leaderboard** over 7 / 30 / 90 / 365 days: wins, top-3 finishes,
  average rank and ranked hours, sortable.

Whole-array rankings mostly reflect roof placement; rankings within
roof-face groups (set in `PANEL_GROUPS`) cancel orientation and are the
ones to read for shade or faults. Only hours
where every panel's data arrived are ranked (\*15). Linked from the
Optimizers heading, the Energy-today total row, the panel history page and
each detail page. See `panel_hourly` in [03-data-model.md](03-data-model.md).

## Optimizers table

Latest actual measurement per optimizer. **SITE IDLE / SITE GENERATING**
above it says whether anything reported new data in the last cycle — after
dark, `STALE` everywhere is expected, not a fault.

"Last Measurement" is SolarEdge's own timestamp for the reading, **not** when
we stored it. Because of backfill, a brand-new database will still show old
timestamps if the hardware hasn't produced anything since.

**Energy today (Wh)** is each panel's estimated energy so far today — the
inverter's own measured energy shared out by each panel's measured portion
(see `panel_daily` in [03-data-model.md](03-data-model.md)) — so the total
row matches the inverter's figure. When the inverter wasn't read all day
the panel's measured energy is shown instead, marked *measured*; an amber
"cov." badge means under 90% of today's per-panel data has arrived from
SolarEdge yet. Hover a cell for estimated, measured and coverage.

Click any row to open that optimizer's history page.

## Health verdict table

Ranked worst-first. See [04-analysis-method.md](04-analysis-method.md) for
the method.

| Column | Meaning |
|---|---|
| **Verdict** | GOOD / LIKELY GOOD / WATCH / SUSPECT / BAD / NO DATA |
| **Score** | 0-100, output-vs-peers multiplied by reporting reliability |
| **Compared with** | The panel's peer group — "whole array" unless `PANEL_GROUPS` sets groups |
| **vs Peers** | Average power over its readings as % of its peers' median *in the same 15-minute window*. 100% = producing like its neighbours |
| **F / S / M** | Fresh / Stale / Missing counts across the polls that observed a scored window (informational) |
| **Fresh %** | Reliability: share of scored daylight windows it reported in, vs its peers |
| **Avg/Median/Max W, V, A ☀** | Daylight-only value stats (median, not minimum — see [04](04-analysis-method.md)) |
| **Near 0 ☀** | Share of daylight readings under 0.2 A. Amber above 10%. Leans higher on weaker panels, but loosely — supporting evidence only |
| **Confidence** | Sample-size based. **Check this before acting** |

Two dropdowns:

- **Range** — how far back to look.
- **Hour** — restrict scoring to one hour of the day (site local time). Only
  hours with usable data are offered. Remember that a single hour cannot
  tell shade from a fault.

Every column sorts, both directions. The note under the heading reports how
many cycles were scored and how many were skipped for weak light, unstable
light, or readings not taken close enough together.

## Optimizer detail page

`optimizer.html?serial=...` — reached by clicking any row.

- **Verdict banner** — score, output vs peers, reporting %, and confidence,
  with an explicit warning when confidence is weak.
- **Cards** — freshness and F/S/M (daylight), whether it's currently
  reporting, gap count, and daylight avg/median/max for power, voltage and
  current.
- **Voltage/current/power over time** — breaks in the line are cycles where
  it reported nothing; flat stretches are repeated stale readings.
- **Output vs peers by hour of day** — the shade-versus-fault
  discriminator, with an automatic `SHADE-LIKE` / `FAULT-LIKE` / `NORMAL`
  read once the profile spans enough of the day.
- **Detected communication gaps** — when its own timestamp jumped by more
  than ~2.5 intervals.
- **Full reading history** — one row per cycle with its FRESH/STALE/MISSING
  status, showing both the poll time and the device measurement time.

## API

All read-only JSON.

| Endpoint | Purpose |
|---|---|
| `/api/status` | Poller health, totals |
| `/api/cycles?hours=` | Cycle completeness, failures by type, request count |
| `/api/health?hours=&hour_of_day=` | Verdicts and daylight value stats |
| `/api/hourly?hours=` | Output vs peers by hour, per optimizer |
| `/api/optimizers` | Latest reading per optimizer |
| `/api/optimizers/{serial}/readings?hours=` | Raw history |
| `/api/optimizers/{serial}/analysis?hours=` | Gaps, coverage, stuck time |
| `/api/errors?limit=` | Failed poll cycles — our problems only |
| `/api/optimizers/{serial}/daily?days=` | One optimizer's `panel_daily` rows, today live |
| `/api/panels/daily?days=` | Every optimizer's daily rows plus the inverter's per-day energy — the panel history page |
| `/api/panels/hourly?date=YYYY-MM-DD` | Every panel's figures for each hour of one day, ranks, and that day's wins / top-3 / average rank (both bases); today live |
| `/api/panels/leaderboard?days=` | Wins, top-3, average rank and ranked hours per panel over a range, both bases |
| `/api/inverter/hourly?days=` | Whole-system energy per clock hour for the last N days (max 31), from the inverter's counter |
| `/api/inverter/history?days=` | One row per day from `inverter_daily`, today recomputed live |
| `/api/inverter/day?date=YYYY-MM-DD` | That day's raw inverter readings (stale snapshots removed) and summary |
| `/api/inverter` | Today's Modbus series, peak, energy, Modbus failure rate, cloud delay series |
