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

~26 optimizers x 720 cycles/day ≈ 19k rows/day ≈ **4 MB/day**.

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
