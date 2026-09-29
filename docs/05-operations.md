# Operations

## First-time setup

```bash
cd SolarMonitor
python3 -m venv venv
venv/bin/pip install -r requirements.txt
venv/bin/playwright install chromium        # needed for login only
cp .env.example .env                        # then fill in credentials
```

`.env` keys:

| Key | Notes |
|---|---|
| `SOLAREDGE_USERNAME` / `SOLAREDGE_PASSWORD` | Portal login (installer account) |
| `SOLAREDGE_SITE_ID` | Your site's numeric ID — the `siteId=` in the monitoring portal's URL |
| `POLL_INTERVAL_SECONDS` | `120`. This *is* the request rate — one request per cycle |
| `DISCOVERY_REFRESH_HOURS` | `24` |
| `SITE_TIMEZONE` | IANA name for hour labels; blank = machine's timezone |
| `DB_PATH` / `COOKIES_PATH` | Defaults are fine |
| `INVERTER_MODBUS_HOST` / `INVERTER_MODBUS_PORT` | Inverter's LAN IP and `1502`; blank host disables Modbus |
| `MODBUS_INTERVAL_SECONDS` | `30`. Local only — costs no SolarEdge requests. Runs on its own thread inside the poller (the inverter accepts one Modbus client at a time, so don't run a second reader alongside it) |
| `DECOMMISSIONED_SERIALS` | Comma-separated serials of physically replaced optimizers, excluded from scoring |
| `INVERTER_RETENTION_DAYS` | `365`. Days of raw 30-second inverter readings kept (~3,000 rows/day). Daily summaries are kept forever regardless; `0` keeps raw readings forever |
| `OPTIMIZER_RETENTION_DAYS` | `365`. Days of raw per-optimizer readings kept (~19,000 rows / ~5 MB a day). Per-panel daily summaries are kept forever regardless; `0` keeps raw readings forever |
| `PANEL_GROUPS` | Who each panel is compared with. Empty = whole array (default); `solaredge` = SolarEdge's panel-model labels; or your own groups by roof face, e.g. `South=1.0.1-1.0.10,1.0.18; North=1.0.17,1.0.19-1.0.24`. See [04](04-analysis-method.md) |
| `ARRAY_NAMEPLATE_W` | Optional. Total panel rating in watts (e.g. 20 × 365 = `7300`); shown next to today's peak for comparison |

Never commit `.env`, `session/cookies.json` or `data/` — all gitignored.

## Running

For unattended running, install both processes as systemd services — they
then **start at boot and restart within seconds if they crash**:

```bash
./scripts/install_systemd.sh               # needs sudo; safe to re-run
./scripts/install_systemd.sh --uninstall   # remove both services
```

This creates `solarmonitor.service` (collector) and
`solarmonitor-web.service` (dashboard on port 8090, or
`SOLARMONITOR_WEBAPP_PORT`). They stay separate on purpose: restarting the
dashboard never interrupts collection. `.env` is read by the app itself,
not passed to systemd, which parses such files differently (e.g. `$`).

The two processes are controlled independently, with or without systemd:

```bash
./scripts/poller_ctl.sh  {start|stop|restart|status}    # data collection
./scripts/webapp_ctl.sh  {start|stop|restart|status}    # dashboard
```

Once the services are installed these scripts drive `systemctl`, so a
second, unmanaged collector can never be started alongside the service —
two collectors would fight over the inverter's single Modbus connection.
Without systemd they fall back to `nohup` background processes (PIDs in
`data/*.pid`), which survive a closed terminal but **not a reboot**.

Logs: `data/poller.log`, `data/webapp.log`.

If the poller is stopped, starting it resumes on the same schedule — there
is no resume state, since everything collected is already in SQLite. What a
gap costs: the inverter's energy total survives (its own counter keeps
counting, and the day's baseline spans gaps up to 12 h), but per-panel
readings from the gap are lost, because SolarEdge's live endpoint only ever
shows each optimizer's latest measurement.

## Health checks

```bash
./scripts/poller_ctl.sh status
tail -5 data/poller.log
```

A healthy cycle line looks like:

```
Cycle OK: 18 fresh, 2 stale, 6 missing (1 request(s))
```

Or in SQL:

```sql
SELECT status, COUNT(*) FROM poll_cycles GROUP BY status;       -- our reliability
SELECT COUNT(*) FROM readings;                                  -- volume
SELECT MAX(started_at) FROM poll_cycles;                        -- still alive?
```

The dashboard's top row shows the same: poller state, cycles completed vs
expected, measured request count, and rate-limit count.

## Tuning the request rate

Raise `POLL_INTERVAL_SECONDS` and restart the poller if the **Rate Limited**
card is non-zero. Nothing is lost by changing it — the analysis works off
observed cycles, not an assumed rate.

Don't go below ~120s: the optimizers only produce a new measurement every
~4.5 min, so faster polling collects duplicates. See
[02-solaredge-api.md](02-solaredge-api.md).

## Troubleshooting

| Symptom | Cause / action |
|---|---|
| `AUTH_ERROR` cycles | Credentials wrong, or the login flow changed. Run `venv/bin/python -m src.capture_endpoints` and compare against `src/auth.py`. |
| `RATE_LIMITED` cycles | Raise `POLL_INTERVAL_SECONDS`. The poller already backs off 5 intervals automatically. |
| A few `NETWORK_ERROR` | Normal. ~2.5% observed. They write no readings, so they don't pollute results. |
| Dashboard "Database not created yet" | The poller hasn't run yet — start it. |
| Everything `STALE`, 0 fresh | Almost certainly night. The dashboard shows **SITE IDLE** when no optimizer reported anything new. |
| All verdicts `INSUFFICIENT` | Not enough scored daylight cycles yet. Expect `MEDIUM` after a day. |
| Hour labels look shifted | `SITE_TIMEZONE` is wrong. The poller logs `Site timezone confirmed: <zone>` at startup and warns on mismatch. Peak output should land near midday. |
| Dashboard looks hours behind reality | Expected. SolarEdge can deliver readings hours late when the inverter's uploads fall behind; watch the dashboard's *Cloud Delay* card. Not a poller fault, and polling faster won't help. |
| 404 on an endpoint you remember | `/api/summary` was removed — it was unused and its averages weren't daylight-scoped. Use `/api/health`. |

## Recreating the database

There is no migration machinery. When the schema changes:

```bash
./scripts/poller_ctl.sh stop
mkdir -p data/backup
mv data/solar_monitor.db data/backup/solar_monitor.db.$(date +%Y%m%d-%H%M)
./scripts/poller_ctl.sh start          # recreates with the current schema
```

Back up rather than delete — it costs nothing and the data isn't
reproducible.

## Tests

```bash
venv/bin/python -m pytest tests/ -q
```

Tests cover the status classification, failure separation, rate-limit
detection, the session-cookie regression, Modbus storage, and the analysis
method (energy windows, daylight scoping, backfill handling, reporting
rhythm, shade-vs-fault).
