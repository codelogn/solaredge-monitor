# Analysis method

Implemented in `src/analysis.py`, exposed at `/api/health` and `/api/hourly`.
The goal is to decide which optimizers/panels are worth investigating — and,
just as importantly, to avoid confidently accusing a healthy one.

## What is compared: average power in 15-minute windows

Each FRESH measurement is filed into a **15-minute window by its measurement
time** (`ts_utc`), never by when we polled — SolarEdge delivers minutes to
5+ hours late. An optimizer's value for a window is the **mean power of all
its readings in it** (typically ~3), and that is compared with its peers'
median for the same window.

**Why windows, not single polls.** Each optimizer reports on its own 3–6
minute schedule, so the readings returned by one poll were taken minutes
apart. Comparing them forced the original method to discard most cycles —
912 of ~1,300 in one three-day range — as "not measured together" or "light
changing too fast". Averaging each panel's readings over a shared 15-minute
window needs neither filter.

**Every reading is kept, including near-zero ones.** Readings near 0 A are
common — 4% to 27% of daylight readings per panel on the development site,
mostly with the panel at open-circuit voltage. They look like glitches, and
an earlier version of this method treated them as such. Checked against the
inverter's own DC power over Modbus, they are real: over 27 good-light
windows with every optimizer reporting, the sum of the per-window averages
came to **1.04×** the inverter's DC power (optimizers measure panel power
before their own small losses, so ~1.01–1.03 is ideal). Filtering the
near-zero readings moved it away every time:

| Rule | Optimizers ÷ inverter |
|---|---|
| Keep every reading | **1.04** |
| Drop exact 0 W | 1.07 |
| Drop < 0.2 A at open-circuit voltage | 1.13 |
| Drop readings far below the optimizer's own reported energy | 1.18 |

**SolarEdge's per-report energy (`energy_wh`) is not used.** It covers
overlapping windows, summed to 1.60× the inverter, and scattered about
three times as much window-to-window as the power readings (CV 0.27 vs
0.10) — which is how it misled the earlier version into calling real
readings glitches. Both measures ranked panels almost identically
(r = 0.93, same five weakest), so conclusions drawn under the earlier
version stand.

## Who a panel's peers are: `PANEL_GROUPS`

Each optimizer is compared with the median of its **peers** in the same
window. By default the peers are the **whole array**. `PANEL_GROUPS`
(`src/groups.py`) can change that:

| `PANEL_GROUPS` | Peers |
|---|---|
| empty (default) | every panel |
| `solaredge` | panels with the same SolarEdge panel-model label |
| `South=1.0.1-1.0.10,1.0.18; North=1.0.17,1.0.19-1.0.24` | your own groups, by layout position (`1.0.x`) or serial; unlisted panels use the whole array |

A group needs at least `MIN_GROUP_FOR_OWN_MEDIAN` (5) reporting members for
its median to be trusted; below that it falls back to the whole array. A
spec that can't be parsed is ignored (whole array) and the reason is shown
on the dashboard and logged by the poller.

**Why whole array is the default.** An earlier version grouped by
SolarEdge's panel-model label, assuming it named a real difference. It
doesn't have to: the label is free text typed in by whoever registered the
site. On the development site 20 identical 365 W panels carried two labels,
"REC 365" and "REC", splitting them arbitrarily. Each label held both
well- and poorly-placed panels, and on a complete day their medians were
close (554 vs 486 Wh). The early "2.5× between the groups" reading came
from a partial first evening.

**What groups are for.** Against the whole array, panels on a poorly facing
roof read low for that reason alone. Grouping by **roof face** cancels
orientation, so "within group" then means "compared with panels facing the
same way" — the comparison that exposes shade or a fault. SolarEdge has no
orientation data, so those groups have to come from you.

Changing `PANEL_GROUPS` recomputes the stored per-panel daily and hourly
history for every day whose raw readings are still kept (the poller stores
the grouping's signature in the `meta` table and notices the change).

## Why peer-relative, not absolute

Absolute output says nothing on its own: a panel making 40 W is fine at dusk
and terrible at noon. But within one window every optimizer on one roof
shares the same sky, so expressing each as a percentage of its peers'
median ("vs Peers") cancels sun angle and cloud. Per-window ratios are then
aggregated with a **median**, so one odd window can't swing a verdict. The
median needs most panels to be healthy to be meaningful — true for this
array.

## The score

Two independent failure modes, **multiplied**:

```
performance = min(peer_ratio / 0.90, 1.0)       # full marks at 90% of peer median
reliability = min(coverage / peer_coverage, 1)  # coverage = share of scored windows it reported in
score       = 100 * performance * reliability
```

Multiplied rather than averaged, because both are evidence of a fault and
neither should cancel the other out. Averaging let a panel producing at 30%
of its neighbours score "WATCH" — its perfect reporting record propped it
up — when it's exactly the panel you'd want to swap.

**Reliability is window coverage, not FRESH-per-poll.** Each optimizer has
its own reporting rhythm — 3.1 to 5.9 minutes on the development site, with zero
`MISSING` rows ever — so a per-poll fresh rate docked healthy slow reporters
to ~71%. Any healthy rhythm lands in every 15-minute window; only a real
silence costs points. It is relative to the median peer's coverage so a
delivery gap that hits everyone can't read as everyone failing.
FRESH/STALE/MISSING counts are still returned for the cycles that observed a
scored window, for information.

| Score | Verdict |
|---|---|
| ≥85 | `GOOD` |
| 70-85 | `LIKELY GOOD` |
| 50-70 | `WATCH` |
| 30-50 | `SUSPECT` |
| <30 | `BAD` |
| never reported | `NO DATA` |

## Which windows are scored

`_select_windows()` is the single definition, shared by scores,
reliability, value stats and hourly profiles — metrics computed over
different sample sets have caused bugs here twice.

1. **Enough reporters** — at least 3, and at least half of the optimizers
   active in the range (`excluded_sparse` otherwise). A window left thin by
   a delivery gap can't fairly compare the few that made it.
2. **Light is strong enough** — the window's median energy is at least 40%
   of the range's bright level (90th percentile of window medians), and its
   median snapshot power is at least 20 W. Ratios are divisions by that
   median, so in weak light a trivial absolute gap reads as a huge ratio.

A clear day yields roughly 25–35 scored windows.

## Everything is daylight-scoped

Value stats (**min/avg/max of power, voltage and current**) and
**reliability** are computed over the scored windows only. This has been
the source of two separate bugs:

- min/max over all readings pinned every optimizer's minimum at its ~0 W
  night value, identical for all and useless for comparison;
- reliability over all cycles docked every optimizer for going quiet after
  dark.

**If you add a metric, scope it the same way.**

The value stats show **median**, not minimum: every panel genuinely touches
~0 A at some point, so the minimum was 0 for 13 of 20 panels and told them
apart not at all. Alongside it, **near 0** is the share of daylight readings
below 0.2 A (`NEAR_ZERO_A`) — how much of the good-light time a panel spent
producing almost nothing. It leans higher on weaker panels, but only
moderately (r = −0.53 against vs Peers on the development site, where
panels producing above their group median still ranged 4–21%), so it's
supporting evidence, not a verdict.

## Confidence is not decoration

| Scored windows | Confidence |
|---|---|
| ≥60 | `HIGH` (~2–3 days) |
| ≥20 | `MEDIUM` (~1 day) |
| ≥6 | `LOW` |
| <6 | `INSUFFICIENT` |

A `BAD` verdict off 3 samples is noise.

## Hour of day: shade versus fault

**A single hour cannot distinguish shade from a fault**, however much data
it holds. A panel shadowed by a chimney at 9am looks exactly like a failing
panel at 9am. The confound is time, not sample size.

The discriminator is the shape across the day, which `/api/hourly` builds:

- **Shade moves** — deep dip at some hours, normal at others. Wide spread.
- **A fault doesn't** — down by roughly the same proportion whenever the sun
  is up. Flat, low line.

Each hour is calibrated against **its own** light level, otherwise 8am would
be judged against midday peak and the whole morning discarded — and the
early and late hours are exactly where shade shows up.

Hours use the window's measurement time converted to `SITE_TIMEZONE`. If
peak output doesn't land near midday in the profile chart, that setting is
wrong and every hour label is offset.

## Limits worth remembering

- Consistently low output can mean shade, soiling or orientation — not
  necessarily a fault. This is a shortlist to investigate, not a diagnosis.
- Afternoons are thin when the inverter's cloud uploads fall behind (see
  the dashboard's Cloud Delay card): windows that never got delivered can't
  be scored.
- A `MISSING` optimizer can't be assessed for output at all — we only know
  it isn't talking.
