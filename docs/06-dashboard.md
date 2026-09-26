# Dashboard

`http://<this-machine's-LAN-IP>:8090` — no authentication, intended for LAN
access only. Auto-refreshes every 30s. Start it with
`./scripts/webapp_ctl.sh start`.

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
| *4 | Verdict / vs Peers are relative to the panel-model group; can't tell shade from fault alone |
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
between vs peers, estimated energy, measured energy, time near 0 A and
per-panel data coverage, with a total or median per panel. Colours run red
→ green within the metric; dashed cells had under 90% coverage; ⚠ marks days
the inverter was only partly read. Built for spotting a panel that drifts
down over weeks. Linked from the Health verdict heading; click a panel for
its detail page. The detail page itself now opens with that panel's daily
energy chart (estimated and measured bars, coverage line). See
`panel_daily` in [03-data-model.md](03-data-model.md).

## Optimizers table

Latest actual measurement per optimizer. **SITE IDLE / SITE GENERATING**
above it says whether anything reported new data in the last cycle — after
dark, `STALE` everywhere is expected, not a fault.

"Last Measurement" is SolarEdge's own timestamp for the reading, **not** when
we stored it. Because of backfill, a brand-new database will still show old
timestamps if the hardware hasn't produced anything since.

Click any row to open that optimizer's history page.

## Health verdict table

Ranked worst-first. See [04-analysis-method.md](04-analysis-method.md) for
the method.

| Column | Meaning |
|---|---|
| **Verdict** | GOOD / LIKELY GOOD / WATCH / SUSPECT / BAD / NO DATA |
| **Score** | 0-100, output-vs-peers multiplied by reporting reliability |
| **vs Peers** | Average power over its readings as % of its panel-model group's median *in the same 15-minute window*. 100% = producing like its neighbours |
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
| `/api/inverter/history?days=` | One row per day from `inverter_daily`, today recomputed live |
| `/api/inverter/day?date=YYYY-MM-DD` | That day's raw inverter readings (stale snapshots removed) and summary |
| `/api/inverter` | Today's Modbus series, peak, energy, Modbus failure rate, cloud delay series |
