# SolarMonitor documentation

| Doc | Read it for |
|---|---|
| [01-architecture.md](01-architecture.md) | How the poller and dashboard fit together, and why they're separate processes |
| [02-solaredge-api.md](02-solaredge-api.md) | The endpoints, the Cognito/Cloudflare login, backfill, request budget |
| [03-data-model.md](03-data-model.md) | Schema, FRESH/STALE/MISSING, the two timestamps |
| [04-analysis-method.md](04-analysis-method.md) | How good/bad verdicts are computed and what they can't tell you |
| [05-operations.md](05-operations.md) | Setup, running, tuning, troubleshooting, recreating the DB |
| [06-dashboard.md](06-dashboard.md) | What every column and card means; the API |

## If you read nothing else

Three invariants hold this project together. Breaking any of them produces
results that look fine and are wrong:

1. **Our failures and an optimizer's behaviour live in different tables.**
   A network outage must never be counted against a panel.
   ([03](03-data-model.md))

2. **Every per-optimizer metric is scoped to the scored daylight windows** —
   value stats *and* reliability. Metrics computed over all readings put
   every minimum at its night value and dock every optimizer for darkness.
   ([04](04-analysis-method.md))

3. **Time-of-day uses `ts_utc`, range filters use `fetched_at`.** SolarEdge
   delivers readings up to several hours after they were taken, so these are
   not interchangeable. ([02](02-solaredge-api.md), [03](03-data-model.md))

## Notes about your own installation

Keep them in `local/` at the project root — it is gitignored, so findings
about your specific site (serial numbers, locations, diagnosis history)
never end up in a commit.
