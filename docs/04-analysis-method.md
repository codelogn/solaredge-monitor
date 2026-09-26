# Analysis method

Implemented in `src/analysis.py`, exposed at `/api/health` and `/api/hourly`.
The goal is to decide which optimizers/panels are worth investigating — and,
just as importantly, to avoid confidently accusing a healthy one.

## What is compared: interval energy in 15-minute windows

Until 2026-09-25 each poll's instantaneous `power` snapshots were compared.
Two measurements on the live site showed that was the wrong signal:

- **Snapshots under moving cloud aren't comparable.** Each optimizer reports
  on its own schedule, so two readings a minute apart can see different
  skies. To stay fair the old method discarded most cycles — 912 of ~1,300
  in one three-day range — for readings taken too far apart or light
  changing too fast.
- **About 8% of daytime snapshots are 0 W glitches**: 0 A at open-circuit
  voltage (~40 V) while the inverter is in MPPT at ~1 kW. They are not
  clustered in time, and the optimizer's own interval energy for those
  moments equals its neighbours' (median ratio 1.00, n=202). No energy is
  lost; the snapshot is just wrong.

`energy_wh` (SolarEdge's `total_last_telemetry_energy_WH`) integrates over
a roughly **fixed ~15-minute trailing window** — it does not grow with the
gap between reports (tested at 1–10 minute gaps). So:

1. Every FRESH measurement is filed into a **15-minute window by its
   measurement time** (`ts_utc`), never by when we polled — SolarEdge
   delivers minutes to 5+ hours late.
2. Each optimizer's value for the window is the **mean** of its `energy_wh`
   reports in it (mean, not sum, so an extra report isn't extra output).
3. That value is compared to its group's median for the same window.

Two panels reporting a few minutes apart cover nearly the same 15 minutes of
sky, so no light-stability or simultaneity filter is needed.

**`energy_wh`'s absolute scale is not Wh.** Because the windows overlap,
summing it across reports gave 4.34× the inverter's AC energy. It is only
ever used relatively. For absolute energy use the inverter's Modbus
`lifetime_wh`.

## Panels are compared within their own model group

Peers means *comparable* peers. A site can have several panel groups — the
development site had two, and under identical sun one produced 2.5× the
other. Against one array-wide median that read as every panel in the weaker
group failing and every panel in the stronger one exceptional.

Each optimizer is therefore compared against the median of its **own
`panel_model` group** in the same window. A group needs at least
`MIN_GROUP_FOR_OWN_MEDIAN` (5) reporting members for its median to be
trusted; below that it falls back to the array-wide median.

`panel_model` is the best grouping signal SolarEdge exposes. If the two
groups also differ in roof orientation, that is folded in here — which is
usually what you want, since orientation affects output just as model does.
The flip side: within a group, "100%" means "like the rest of that group",
which may itself be on a poorly oriented roof face. Group-relative ratios
say which panel is odd *within its group*, not which panels are well placed.

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

**If you add a metric, scope it the same way.** The value stats are raw
snapshots, so a 0 W daylight minimum is usually the telemetry glitch
described above, not a dropout — check the verdict, which is energy-based.

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
