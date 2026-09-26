# Data model

SQLite at `data/solar_monitor.db`, WAL mode. Schema in `src/db.py`.

**The central rule: our own failures and an optimizer's behaviour are
recorded in two different places and can never be confused.** Every query in
the project depends on this, and violating it is how you get reports that
blame hardware for a network outage.

## Tables

### `optimizers`
`serial` (PK), `label`, `panel_model`, `first_seen`, `last_seen` — discovered
equipment. `panel_model` drives group-relative comparison (see
[04-analysis-method.md](04-analysis-method.md)); groups on one site can
differ several-fold in output.

### `poll_cycles` — one row per poll attempt, including failures

`id`, `started_at`, `finished_at`, `status`, `error_message`,
`optimizers_known`, `fresh_count`, `stale_count`, `missing_count`,
`duration_ms`, `requests_made`

`status` is one of:

| Status | Meaning |
|---|---|
| `OK` | Cycle completed, readings written |
| `NETWORK_ERROR` | Transport failure — timeout, reset, 5xx |
| `RATE_LIMITED` | HTTP 429 or a Cloudflare block — *we* are polling too hard |
| `AUTH_ERROR` | Login/re-auth failed |
| `DISCOVERY_ERROR` | The layout call failed |
| `ERROR` | Anything else unexpected |
| `RUNNING` | In flight (or the process died mid-cycle) |

This is the **only** place our own problems are recorded.

### `readings` — one row per optimizer per *successful* cycle

`id`, `cycle_id`, `optimizer_serial`, `status`, `fetched_at`, `ts_utc`,
`voltage`, `optimizer_voltage`, `current`, `power`, `energy_wh`, `raw_json`

| Status | Meaning |
|---|---|
| `FRESH` | The device reported a measurement newer than the last one we stored |
| `STALE` | The poll succeeded but the device returned the same `lastMeasurement` again — it isn't producing new telemetry |
| `MISSING` | The device was absent from the response entirely |

Every known optimizer gets a row every successful cycle, so the data is a
complete matrix with no implicit holes. **A failed cycle writes no reading
rows at all**, because during one we observed nothing — so a network outage
on our side can never inflate an optimizer's `MISSING` count.

### `inverter_readings` — Modbus snapshots of the inverter

`fetched_at`, AC voltage/current/power/frequency, DC voltage/current/power,
`efficiency_pct`, heatsink `temperature`, SunSpec `status` (4 = MPPT/normal,
5 = throttled, 7 = fault), `lifetime_wh` (the inverter's own counter — use
`MAX - MIN` over a period for energy). Written every
`MODBUS_INTERVAL_SECONDS` by a thread independent of the cloud poll, so
`cycle_id` is NULL on rows since 2026-09-25; older rows carry the cycle they
were read in.

### `modbus_failures` — Modbus reads that failed after retries

`occurred_at`, `reason`. Kept out of `inverter_readings` for the same reason
`poll_cycles` is kept out of `readings`: our connectivity problems must never
look like inverter data.

### `inverter_daily` — one row per site-local day, kept indefinitely

Rolled up from `inverter_readings` by `src/daily.py`, using the definitions
in `src/inverter_stats.py` — the same code that computes the dashboard's
"today" cards, so a day's figures don't change meaning at midnight.

| Column | Meaning |
|---|---|
| `energy_wh` | Rise in the inverter's lifetime counter across the day. Starts from the last reading before midnight when that's within 12 h, so production before the day's first successful read isn't lost |
| `peak_dc_w`, `peak_at` | Highest DC reading captured |
| `avg_dc_w` | Time-weighted DC power while producing (≥50 W); intervals over 10 min without a read are skipped, not interpolated |
| `producing_hours`, `first/last_producing_at` | First to last reading ≥50 W |
| `min_ac_v`, `max_ac_v`, `max_temp_c` | While producing |
| `abnormal_samples` | Readings with SunSpec status 5 (throttled) or 7 (fault) |
| `reads_ok`, `reads_failed` | Modbus reads that succeeded / failed after retries |
| `se_coverage_pct`, `se_missing_minutes` | Share of producing 15-min windows with SolarEdge per-panel data from ≥ half the optimizers, and the producing time without it |
| `partial` | Modbus didn't see the start or end of production — energy and averages undercount |
| `finalized` | Set once a day is 2+ days old; finalized days are never recomputed |

The poller refreshes non-finalized days at startup and hourly, then prunes
`inverter_readings` and `modbus_failures` older than
`INVERTER_RETENTION_DAYS` (default 365). Summaries always exist before raw
data can be pruned, and the summary table itself is never pruned.

### `panel_daily` — one row per optimizer per site-local day, kept indefinitely

Rolled up from `readings` by `src/panel_daily.py`, after the inverter roll-up
(it needs that day's inverter energy). Primary key `(day, serial)`.

| Column | Meaning |
|---|---|
| `measured_wh` | The optimizer's own power readings integrated over the time they cover; gaps over 15 min are skipped. Exact for what SolarEdge delivered, short when stretches never arrived |
| `estimated_wh` | The inverter's day energy split in proportion to `measured_wh`, so all panels add up to the inverter's total. NULL when the inverter day is `partial`. Assumes each panel's share in undelivered hours matched delivered ones |
| `coverage_pct` | Share of the day's producing 15-min windows with this optimizer's readings. "Producing" is the union of what the inverter and the optimizers saw, so neither source's gaps distort it |
| `avg_power_w`, `peak_w`, `peak_at`, `near_zero_pct` | Over the producing windows |
| `vs_peers_pct` | The day's median of the dashboard's per-window vs-peers ratio |
| `readings`, `finalized`, `updated_at` | As for `inverter_daily` |

On a complete day the panels' `measured_wh` total came within ~10% of the
inverter (DC before conversion losses vs AC after); `estimated_wh` matches
it by construction.

Raw `readings` older than `OPTIMIZER_RETENTION_DAYS` (default 365) are
deleted after the roll-up; `panel_daily` is never pruned. `poll_cycles` is
kept (720 small rows a day).

## The two timestamps

Confusing these is the most common mistake when reading this data.

| Column | Meaning | Nullable |
|---|---|---|
| `fetched_at` | When **we** polled | never |
| `ts_utc` | When the **optimizer measured** (`lastMeasurement`) | NULL on `MISSING` |

They can differ by **hours** — SolarEdge backfills (see
[02-solaredge-api.md](02-solaredge-api.md)). So:

- **Filter time ranges on `fetched_at`** — it's always set. Filtering on
  `ts_utc` silently drops every `MISSING` row.
- **Do time-of-day and ordering on `ts_utc`** — it's when the sun was in
  that position. Using poll time here files dusk readings under midnight.

## Why `STALE` exists rather than deduplicating

SolarEdge frequently returns the same `lastMeasurement` across consecutive
polls. Those repeats are not noise to be discarded:

- Counting them as new readings would weight every average by *how often we
  happened to poll* rather than by what the hardware did — and polling
  faster would "improve" the statistics while adding no information.
- A rising stale rate is itself a diagnostic signal. Optimizers talk to the
  inverter over the DC power lines; a device that stops advancing its
  timestamp while its neighbours keep updating points at the communication
  path, which is a different repair from low output.

In one overnight sample: 200 rows carried data, but only 20 were distinct
measurements. The other 180 were the same reading served again.

## Growth

~26 optimizers x 720 cycles/day ≈ 19k rows/day ≈ **4–5 MB/day**, so about
**1.8–2 GB** at the default 365-day retention. Daily summaries add a few
hundred KB a year.

## Changing the schema

`CREATE TABLE IF NOT EXISTS` will **not** alter a table that already exists.
Two consequences:

- **Adding a nullable column** — do it in `db._add_missing_columns()`, which
  checks `PRAGMA table_info` and `ALTER TABLE ... ADD COLUMN`. This is how
  `panel_model` was added without discarding collected readings. It is
  deliberately not a migration framework; it handles additive columns only.
- **Changing a `CHECK` constraint** (e.g. adding a status value) cannot be
  done by `ALTER` and requires recreating the database. Until then the write
  silently violates the old constraint — and it throws *inside the poller's
  error handler*, which is the worst place for it.

To recreate, back up first — see [05-operations.md](05-operations.md).
Readings are not reproducible, so prefer an additive column where possible.
