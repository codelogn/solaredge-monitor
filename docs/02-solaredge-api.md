# The SolarEdge side

There is no official API key for this account, so the collector uses the
same internal endpoints the monitoring portal's own frontend calls. These
are undocumented and can change without notice. Everything below was
established by capturing real traffic against the live account on
2026-09-23; if something breaks, re-run the capture rather than guessing:

```bash
venv/bin/python -m src.capture_endpoints    # headed browser, prints API traffic
```

## Authentication

The portal uses **AWS Cognito hosted login** (OAuth2 authorization code +
PKCE) at `login.solaredge.com`, and sits behind **Cloudflare**. There is no
2FA on this account, so the whole flow runs headlessly.

Flow: `monitoring.solaredge.com/one#/site-list` → "Log in" →
`login.solaredge.com/login?...code_challenge=...` → credentials →
`/mfe/auth/callback?code=...` → **the SPA exchanges the code for a session**
→ `#/site-list`.

### The trap: success is a cookie, not a page load

That token exchange completes **several seconds after** the browser leaves
the login page. Measured:

| Moment | URL | Auth cookies present |
|---|---|---|
| networkidle | login.solaredge.com | no |
| +2s | /mfe/auth/callback | no |
| **+5s** | #/site-list | **`se_monitoring_auth`, `se_monitoring_refresh`** |

An earlier version waited `networkidle + 2s` and checked only that the URL
had left `login.solaredge.com`. That check passed on the callback page, so
login reported success while handing back a session that **401'd on every
call** — a silent failure that would have burned a full day of collection.

`src/auth.py` now waits for the `se_monitoring_auth` cookie itself, and
`load_session()` refuses a saved file that lacks it. Never reintroduce a
fixed sleep here.

Cookies (`se_monitoring_auth`, `se_monitoring_refresh`, `JSESSIONID`,
`CSRF-TOKEN`, `cf_clearance`) are saved to `session/cookies.json` and reused
by a plain `requests.Session` with the same User-Agent. No browser runs
during normal polling.

## Endpoints

### Optimizer discovery

```
GET /services/layout/logical/generic/v2/site/{siteId}?include-optimizers=true
```

Returns the inverter → string → optimizer tree. Optimizer nodes look like:

```json
{"type": "OPTIMIZER", "serial": "1A2B3C4D-01", "name": "Optimizer 1.0.1",
 "displayOrder": "1.0.1", "properties": {"panelModelName": "ACME 400"}}
```

### Live readings — the one that matters

```
POST /services/layout/information/optimizers
Body: ["1A2B3C4D-01", "1A2B3C4D-02", ...]      // all serials, one request
```

```json
{"basicInformationList": [...],
 "serialToLiveData": {
   "1A2B3C4D-01": {"lastMeasurement": "2026-09-23T19:08:37Z",
                   "voltage_V": 37.25, "optimizerVoltage_V": 55.62,
                   "current_A": 3.88, "power_W": 144.8,
                   "total_last_telemetry_energy_WH": 4.5}}}
```

- `voltage_V` tracks panel Vmp (~30-40 V here) — the panel-side voltage.
- `optimizerVoltage_V` is the optimizer's output onto the string bus and
  swings much more. Both are stored; neither is discarded.
- **Optimizers with no recent telemetry are absent from `serialToLiveData`
  entirely** — not zero. That absence is recorded as `MISSING`.

### Site metadata (timezone)

```
GET /services/sitelist/{siteId}        ->  {"timeZone": "America/New_York", ...}
```

Every hour-of-day figure depends on this. `SITE_TIMEZONE` defaults to the
*machine's* timezone, which was `Etc/UTC` while the site is UTC-5 — so hour
labels were five hours off until corrected. `client.fetch_site_timezone()`
reads the authoritative value and the poller warns at startup on mismatch.

### Hourly energy series (not currently collected)

```
GET /services/layout/energy-graph/site/{siteId}/optimizers
    ?chart-time-unit=hours&start-date=YYYY-MM-DD&end-date=YYYY-MM-DD
    &optimizer-serials=<serial>
->  {"totalEnergy": 282.0,
     "energyBars": [{"measurementTime": "2026-09-23T07:00:00-05:00", "energy": 3.2}, ...]}
```

`chart-time-unit` accepts `hours`; `minutes`, `QUARTER_HOUR`, `DAY` and
`WEEK` all return `BAD_ARGUMENTS`. This gives **energy per hour**, not
voltage/current, so it can't replace the live-data endpoint — but it is
useful as an independent cross-check of daily totals, and its timestamps
carry the site's UTC offset.

## Backfill: readings arrive long after they were taken

`lastMeasurement` is when the optimizer *measured*, not when we received it.
These differ substantially — observed lags of **40 to 220 minutes**. A
reading measured at 19:36 was delivered at 22:26.

Consequences, all handled in `src/analysis.py`:

- Every time-of-day bucket and ordering keys off `ts_utc`. Keying off poll
  time filed dusk readings under midnight and made the hourly shading
  profile meaningless.
- Readings are only compared to each other if they were *taken* close
  together. The inverter appears to upload in lockstep batches (one observed
  batch spanned 51 seconds), but this is enforced, not trusted.

Whether the lag is smaller during the day is still unknown — it was measured
overnight and may have been a backlog draining. Worth re-measuring:

```sql
SELECT ROUND((julianday(fetched_at) - julianday(ts_utc)) * 1440) AS lag_min,
       COUNT(*) FROM readings WHERE status='FRESH' GROUP BY lag_min ORDER BY lag_min;
```

If daytime lag is also hours, polling every 2 minutes is largely pointless
and the interval should be raised.

## Request load and throttling

One request per cycle covers every optimizer, plus one discovery call a day.

| Interval | Requests/day |
|---|---|
| 5 min | ~289 |
| **2 min** (current) | ~721 |
| 1 min | ~1441 |

The optimizers only produce a new measurement every **~4.5 min** (median;
p25 3.1, p75 7.1), so polling much faster returns duplicates rather than
resolution. For scale, opening SolarEdge's own dashboard once fires ~46 API
calls.

Throttling is tracked separately from ordinary connectivity errors:

- `RATE_LIMITED` covers HTTP 429 and Cloudflare block pages.
- It is checked **before** session expiry, because a 403 can look like
  either and re-authenticating launches a browser — the worst possible
  reply to being told to slow down.
- On a throttle the poller backs off for 5 intervals.

The dashboard shows measured request volume and a rate-limit count. If it is
non-zero, raise `POLL_INTERVAL_SECONDS`.
