# Architecture

Two processes that share a SQLite file and nothing else.

```
                 SolarEdge portal (monitoring.solaredge.com)
                              |  1 HTTPS request per cycle
                              v
   [ poller ]  src/main.py  --------> data/solar_monitor.db  <-------- [ webapp ]
   scripts/poller_ctl.sh                  (WAL mode)                   webapp/app.py
   writes only                                                         reads only
                                                                       scripts/webapp_ctl.sh
                                                                              |
                                                                    http://<lan-ip>:8090
```

## Why two processes

The poller has to survive for days unattended. The dashboard gets edited,
restarted and broken while that's happening. Keeping them as separate OS
processes makes interference structurally impossible rather than a matter of
care: restarting the webapp cannot touch the poller, and a crash in one
leaves the other running. This was verified repeatedly during development —
the poller kept its PID across a dozen webapp restarts.

SQLite runs in WAL mode so the writer and the read-only reader never block
each other. The webapp opens the database with `mode=ro` so it cannot write
even by accident.

## The poller

`src/poller.py` loops forever:

1. Open a `poll_cycles` row (status `RUNNING`).
2. Discover optimizers if the cached list is stale (once per day).
3. Fetch live data for every optimizer in **one** batched request.
4. Classify each optimizer as `FRESH` / `STALE` / `MISSING` and write the rows.
5. Close the cycle with `OK`, or with a typed failure status.
6. Sleep until the next interval.

A failed cycle is recorded and the loop continues. It never exits on error —
losing a day of collection to one transient timeout would defeat the point.

## The webapp

`webapp/app.py` is FastAPI serving a JSON API plus three static files. It
holds no state; every request reads the database. All analysis lives in
`src/analysis.py`, which is pure computation over the database with no HTTP
concerns, so it is unit-tested directly and could equally back a CLI report.

## Module map

| File | Responsibility |
|---|---|
| `src/config.py` | `.env` loading; credentials, interval, paths, timezone |
| `src/auth.py` | Cognito login via Playwright, session persistence, throttle/expiry detection |
| `src/client.py` | HTTP wrapper over the two SolarEdge endpoints, re-auth, request counting |
| `src/discover.py` | Walks the site layout tree for optimizer serials |
| `src/db.py` | Schema and writes |
| `src/poller.py` | The collection loop and status classification |
| `src/analysis.py` | Peer-relative scoring, cloud filtering, hourly profiles |
| `src/capture_endpoints.py` | Headed browser network sniffer, for when SolarEdge changes things |
| `webapp/app.py` | Read-only JSON API + static hosting |
| `webapp/static/` | Dashboard, optimizer detail page, vendored Chart.js |

## Dependencies

`requests` does the polling. Playwright is needed only to log in (the portal
uses Cognito with PKCE behind Cloudflare, which a plain HTTP client cannot
complete) — once logged in, the cookies work fine from `requests`, so no
browser runs during normal collection. Chart.js is vendored locally rather
than loaded from a CDN, so the dashboard works with no internet access.
