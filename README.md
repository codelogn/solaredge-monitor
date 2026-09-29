# SolarMonitor

Self-hosted per-panel diagnostics for SolarEdge systems.

SolarMonitor polls the SolarEdge monitoring portal every couple of minutes,
stores each power optimizer's voltage, current and energy in a local SQLite
database, and reads the inverter directly over Modbus TCP on your LAN. A
small dashboard then scores every optimizer against its neighbours, so you
can tell a genuinely failing panel or optimizer apart from shade, dirt,
orientation, or a flaky network link — before paying anyone to climb onto
the roof.

- **Per-optimizer history** the SolarEdge app doesn't keep: voltage, current,
  power and interval energy every few minutes, for as long as you like.
- **Peer-relative health verdicts** (GOOD … BAD) with an explicit confidence
  level, comparing each optimizer with its peers (the whole array, or your
  own groups such as roof faces via `PANEL_GROUPS`) in the same
  15-minute window so weather and sun angle cancel out.
- **Hour-by-hour profiles** that separate shade (a dip at certain hours)
  from a fault (low all day).
- **Live inverter data over Modbus** — AC/DC power, status, lifetime energy —
  independent of SolarEdge's cloud, plus a measure of how far behind the
  cloud upload is running.
- **365 days of inverter history** (configurable), with a per-day summary
  kept indefinitely: energy, peak and average power, grid voltage,
  temperature, throttling/fault readings, and how complete each day's
  per-panel data was. Click any day for its full power curve.
- **365 days of per-panel history**: each panel's daily energy (measured,
  and an estimate that adds up to the inverter's total), output vs peers
  and data coverage, in a panels × days grid for spotting a panel that
  drifts down over weeks.
- **Hourly ranking**: every panel's energy for any hour of any day, ranked
  across the whole array or within its group, with each hour's winner and
  a leaderboard of wins, top-3 finishes and average rank.
- Every number on the dashboard is tagged with where it came from (inverter,
  SolarEdge, or computed) and flagged when it may not be exact, and live
  flags say when either source can't currently be trusted — for example
  when SolarEdge's per-panel data is hours behind the inverter.

> **Not affiliated with or endorsed by SolarEdge.** There is no public
> per-optimizer API, so SolarMonitor logs into the monitoring portal with
> your own credentials and calls the same internal endpoints the portal's
> web page uses. Those endpoints are undocumented and can change or break
> at any time. Use your own account, keep the poll interval modest (the
> default is one request every 2 minutes), and check that this use is
> acceptable under SolarEdge's terms for your account. Modbus access is
> read-only; nothing here writes to the inverter.

## Quick start

Requires Python 3.11+ and a SolarEdge monitoring login for your site
(installer or owner account).

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
venv/bin/playwright install chromium        # used only to log in
cp .env.example .env                        # fill in username, password, site ID

./scripts/poller_ctl.sh start               # begin collecting
./scripts/webapp_ctl.sh start               # dashboard on port 8090
```

Then open `http://<this-machine's-LAN-IP>:8090`.

Your site ID is the `siteId=` number in the monitoring portal's URL. For
live inverter data, enable **Modbus TCP** on the inverter (SetApp →
Communication → LAN → Modbus TCP, usually port 1502) and set
`INVERTER_MODBUS_HOST` in `.env`. Every setting is documented in
[`.env.example`](.env.example) and [docs/05-operations.md](docs/05-operations.md).

```bash
./scripts/poller_ctl.sh status              # is it collecting?
tail -5 data/poller.log
venv/bin/python -m pytest tests/ -q
```

For unattended running, `./scripts/install_systemd.sh` (needs sudo) installs
both as systemd services that start at boot and restart on failure; the ctl
scripts then drive those services. Without it they run as `nohup`
background jobs, which don't survive a reboot.

## Security

- **The dashboard has no authentication.** It is meant for your home
  network only. Do not port-forward it to the internet — use a VPN such as
  Tailscale or WireGuard for remote access.
- Credentials live only in `.env`; session cookies in `session/`; all
  collected data in `data/`. All three are gitignored. Keep notes about your
  own installation in `local/`, which is gitignored too.

## Documentation

| Doc | Read it for |
|---|---|
| [Architecture](docs/01-architecture.md) | The two processes and why they're separate |
| [SolarEdge API](docs/02-solaredge-api.md) | Endpoints, login, backfill, request budget |
| [Data model](docs/03-data-model.md) | Schema, FRESH/STALE/MISSING, the two timestamps |
| [Analysis method](docs/04-analysis-method.md) | How verdicts are computed, and their limits |
| [Operations](docs/05-operations.md) | Setup, tuning, troubleshooting |
| [Dashboard](docs/06-dashboard.md) | What every column means; the API |

## Three things not to get wrong

1. **Our failures and optimizer behaviour live in separate tables.** A
   network outage must never be counted against a panel.
2. **Per-optimizer metrics are scoped to the scored daylight windows** —
   stats *and* reliability. Otherwise every minimum becomes its night value
   and every optimizer is docked for darkness.
3. **Time-of-day uses `ts_utc`; range filters use `fetched_at`.** SolarEdge
   delivers readings up to several hours late, so they aren't interchangeable.

## Limitations

- Verdicts are a shortlist to investigate, not a diagnosis: low output can
  be shade, soiling or orientation as easily as a fault.
- Per-optimizer data depends on the inverter uploading to SolarEdge; if its
  network link is weak, that data arrives late or not at all.
- Tested against one HD-Wave single-phase inverter with power optimizers.
  Other SolarEdge models should work but are unverified.
- No alerting or report export yet.

## License

[MIT](LICENSE).

## Third-party

`webapp/static/chart.umd.min.js` is [Chart.js](https://www.chartjs.org)
v4.4.4, MIT licensed, vendored so the dashboard works without internet
access.
