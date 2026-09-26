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
| *1 | SolarEdge snapshots are one instant; occasional 0 W ones are glitches |
| *2 | SolarEdge data is only as current as the inverter's last upload |
| *3 | Daylight min/avg/max come from raw snapshots |
| *4 | Verdict / vs Peers are relative to the panel-model group; can't tell shade from fault alone |
| *5 | Interval energy is relative only, not real Wh |
| *6 | Today's peak is the highest 30 s sample captured |
| *7 | Inverter metering isn't revenue-grade; "now" can be minutes old |
| *8 | Optimizer count is SolarEdge's layout, which can include replaced units |
| *9 | Chart lines bridge short gaps |
| *10 | Gaps / data age can be delivery stalls, not the optimizer |
| *11 | Hourly profile has few samples per hour |

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
peak DC (shown against `ARRAY_NAMEPLATE_W` if set), energy today from the
inverter's own counter, and the Modbus success rate for the last hour.

**Cloud Delay** is poll time minus the newest optimizer measurement in that
poll — normally ~2 min. Red above 15 min while the inverter is producing:
SolarEdge is receiving uploads late and per-panel data for that period will
be late or missing. Blank when the inverter isn't producing, because
optimizers legitimately stop reporting after dark. The chart overlays DC
power and delay for the day.

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
| **vs Peers** | Interval energy as % of its panel-model group's median *in the same 15-minute window*. 100% = producing like its neighbours |
| **F / S / M** | Fresh / Stale / Missing counts across the polls that observed a scored window (informational) |
| **Fresh %** | Reliability: share of scored daylight windows it reported in, vs its peers |
| **Avg/Min/Max W, V, A ☀** | Daylight-only value stats |
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
  reporting, gap count, and daylight min/avg/max for power, voltage and
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
| `/api/inverter` | Today's Modbus series, peak, energy, Modbus failure rate, cloud delay series |
