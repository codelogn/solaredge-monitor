"""
Read-only dashboard for SolarMonitor: shows poller health and lets you
browse/compare collected optimizer readings.

Deliberately a separate process from src/poller.py, talking to the same
SQLite file only through read-only connections. Restarting, redeploying, or
crashing this webapp never touches the poller — and the poller never
notices this app exists. See scripts/webapp_ctl.sh to start/stop it, and
scripts/poller_ctl.sh for the poller (independently).

Throughout, per-optimizer reliability is computed only over cycles that
actually succeeded; cycles that failed on our side are reported separately
(see /api/cycles) and never counted against an optimizer.
"""
from __future__ import annotations

import bisect
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import analysis, daily, inverter_stats, modbus_client, panel_daily  # noqa: E402
from src.config import Config  # noqa: E402

cfg = Config.load()
app = FastAPI(title="SolarMonitor Dashboard")


class AbsoluteFormMiddleware:
    """Accept absolute-form request targets ("GET http://host:8090/ ...").

    Proxies send requests this way, and HTTP/1.1 requires servers to accept
    it, but uvicorn passes the whole URL through as the path, so every such
    request 404'd — seen from a phone loading the dashboard via the home's
    public IP. Rewrite it to the plain path before routing.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"].startswith(("http://", "https://")):
            target = urlsplit(scope["path"])
            path = target.path or "/"
            scope = dict(scope, path=path, raw_path=path.encode())
            if target.query and not scope.get("query_string"):
                scope["query_string"] = target.query.encode()
        await self.app(scope, receive, send)


app.add_middleware(AbsoluteFormMiddleware)

STATIC_DIR = Path(__file__).resolve().parent / "static"


def _connect() -> sqlite3.Connection:
    if not cfg.db_path.exists():
        raise HTTPException(503, "Database not created yet — is the poller running?")
    conn = sqlite3.connect(f"file:{cfg.db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _find_gaps(timestamps: list[str], threshold_seconds: float) -> list[dict]:
    gaps = []
    for a, b in zip(timestamps, timestamps[1:]):
        delta = (_parse_ts(b) - _parse_ts(a)).total_seconds()
        if delta > threshold_seconds:
            gaps.append({"from": a, "to": b, "minutes": round(delta / 60, 1)})
    return gaps


@app.get("/api/status")
def status():
    with _connect() as conn:
        total_optimizers = conn.execute("SELECT COUNT(*) c FROM optimizers").fetchone()["c"]
        total_readings = conn.execute("SELECT COUNT(*) c FROM readings").fetchone()["c"]
        last_cycle = conn.execute(
            """
            SELECT started_at, status, fresh_count, stale_count, missing_count
            FROM poll_cycles ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
        last_ok = conn.execute(
            "SELECT started_at FROM poll_cycles WHERE status = 'OK' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        failures_last_hour = conn.execute(
            """
            SELECT COUNT(*) c FROM poll_cycles
            WHERE status NOT IN ('OK','RUNNING') AND started_at > strftime('%Y-%m-%dT%H:%M:%SZ','now','-1 hour')
            """
        ).fetchone()["c"]
    return {
        "total_optimizers": total_optimizers,
        "total_readings": total_readings,
        "last_cycle_at": last_cycle["started_at"] if last_cycle else None,
        "last_cycle_status": last_cycle["status"] if last_cycle else None,
        # Zero fresh readings across every optimizer means the array isn't
        # generating, so the STALE badges below are night, not faults.
        "last_cycle_fresh": last_cycle["fresh_count"] if last_cycle else None,
        "last_successful_cycle_at": last_ok["started_at"] if last_ok else None,
        "failures_last_hour": failures_last_hour,
        "db_size_bytes": cfg.db_path.stat().st_size,
        "poll_interval_seconds": cfg.poll_interval_seconds,
    }


@app.get("/api/optimizers")
def optimizers():
    """Latest actual measurement per optimizer (ignoring MISSING rows, which
    carry no values), plus its status in the most recent cycle."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT o.serial, o.label,
                   r.ts_utc, r.voltage, r.optimizer_voltage, r.current, r.power,
                   latest.status AS latest_status, latest.fetched_at AS latest_fetched_at
            FROM optimizers o
            LEFT JOIN readings r ON r.id = (
                SELECT id FROM readings
                WHERE optimizer_serial = o.serial AND ts_utc IS NOT NULL
                ORDER BY ts_utc DESC LIMIT 1
            )
            LEFT JOIN readings latest ON latest.id = (
                SELECT id FROM readings
                WHERE optimizer_serial = o.serial
                ORDER BY fetched_at DESC, id DESC LIMIT 1
            )
            ORDER BY o.serial
            """
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/optimizers/{serial}/readings")
def optimizer_readings(serial: str, hours: int = 24, limit: int = 20000):
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT status, fetched_at, ts_utc, voltage, optimizer_voltage,
                   current, power, energy_wh
            FROM readings
            WHERE optimizer_serial = ? AND fetched_at > strftime('%Y-%m-%dT%H:%M:%SZ','now', ?)
            ORDER BY fetched_at ASC, id ASC
            LIMIT ?
            """,
            (serial, f"-{hours} hours", limit),
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/optimizers/{serial}/analysis")
def optimizer_analysis(serial: str, hours: int = 24):
    with _connect() as conn:
        opt = conn.execute(
            "SELECT label FROM optimizers WHERE serial = ?", (serial,)
        ).fetchone()
        if not opt:
            raise HTTPException(404, "Unknown optimizer")

        status_counts = {
            r["status"]: r["c"]
            for r in conn.execute(
                """
                SELECT status, COUNT(*) c FROM readings
                WHERE optimizer_serial = ? AND fetched_at > strftime('%Y-%m-%dT%H:%M:%SZ','now', ?)
                GROUP BY status
                """,
                (serial, f"-{hours} hours"),
            ).fetchall()
        }

        zero_power = conn.execute(
            """
            SELECT COUNT(*) c FROM readings
            WHERE optimizer_serial = ? AND status = 'FRESH' AND power = 0
              AND fetched_at > strftime('%Y-%m-%dT%H:%M:%SZ','now', ?)
            """,
            (serial, f"-{hours} hours"),
        ).fetchone()["c"]

        distinct_ts = [
            r["ts_utc"]
            for r in conn.execute(
                """
                SELECT DISTINCT ts_utc FROM readings
                WHERE optimizer_serial = ? AND ts_utc IS NOT NULL
                  AND fetched_at > strftime('%Y-%m-%dT%H:%M:%SZ','now', ?)
                ORDER BY ts_utc ASC
                """,
                (serial, f"-{hours} hours"),
            ).fetchall()
        ]

        ok_cycles = conn.execute(
            """
            SELECT COUNT(*) c FROM poll_cycles
            WHERE status = 'OK' AND started_at > strftime('%Y-%m-%dT%H:%M:%SZ','now', ?)
            """,
            (f"-{hours} hours",),
        ).fetchone()["c"]

        tail = conn.execute(
            """
            SELECT status, ts_utc FROM readings
            WHERE optimizer_serial = ? ORDER BY fetched_at DESC, id DESC LIMIT 50
            """,
            (serial,),
        ).fetchall()

    fresh = status_counts.get("FRESH", 0)
    stale = status_counts.get("STALE", 0)
    missing = status_counts.get("MISSING", 0)

    # How long has it been repeating/absent right now?
    stuck_minutes = 0.0
    if tail and tail[0]["status"] != "FRESH":
        last_ts = next((r["ts_utc"] for r in tail if r["ts_utc"]), None)
        if last_ts:
            stuck_minutes = round(
                (datetime.now(timezone.utc) - _parse_ts(last_ts)).total_seconds() / 60, 1
            )

    return {
        "serial": serial,
        "label": opt["label"],
        # Coverage only — how many cycles we saw this optimizer in at all.
        # Status counts and reliability live in /api/health, scoped to
        # daylight; duplicating them here with a different scope invites
        # exactly the kind of mismatch that produced a mis-coloured badge.
        "observed_cycles": fresh + stale + missing,
        "successful_cycles": ok_cycles,
        "gaps": _find_gaps(distinct_ts, cfg.poll_interval_seconds * 2.5),
        "stuck_minutes": stuck_minutes,
        "latest_status": tail[0]["status"] if tail else None,
        # Electrical value stats deliberately live only in /api/health, which
        # computes them over daylight cycles only. Duplicating them here once
        # led to night readings dragging every minimum to ~0 W.
        "zero_power_count": zero_power,
    }


@app.get("/api/cycles")
def cycles(hours: int = 24):
    """Poller-side health, kept entirely separate from optimizer stats."""
    with _connect() as conn:
        by_status = {
            r["status"]: r["c"]
            for r in conn.execute(
                """
                SELECT status, COUNT(*) c FROM poll_cycles
                WHERE started_at > strftime('%Y-%m-%dT%H:%M:%SZ','now', ?) GROUP BY status
                """,
                (f"-{hours} hours",),
            ).fetchall()
        }
        requests_made = conn.execute(
            """
            SELECT COALESCE(SUM(requests_made), 0) n FROM poll_cycles
            WHERE started_at > strftime('%Y-%m-%dT%H:%M:%SZ','now', ?)
            """,
            (f"-{hours} hours",),
        ).fetchone()["n"]
        timestamps = [
            r["started_at"]
            for r in conn.execute(
                """
                SELECT started_at FROM poll_cycles
                WHERE status = 'OK' AND started_at > strftime('%Y-%m-%dT%H:%M:%SZ','now', ?)
                ORDER BY started_at ASC
                """,
                (f"-{hours} hours",),
            ).fetchall()
        ]
    ok = by_status.get("OK", 0)
    failed = sum(v for k, v in by_status.items() if k not in ("OK", "RUNNING"))
    expected = max(1, round(hours * 3600 / cfg.poll_interval_seconds))
    return {
        "ok_cycles": ok,
        "failed_cycles": failed,
        "rate_limited_cycles": by_status.get("RATE_LIMITED", 0),
        "by_status": by_status,
        "expected_cycles": expected,
        "completeness_pct": round(100 * ok / expected, 1),
        "requests_made": requests_made,
        "requests_per_day_projected": round(86400 / cfg.poll_interval_seconds),
        "poll_interval_seconds": cfg.poll_interval_seconds,
        "gaps": _find_gaps(timestamps, cfg.poll_interval_seconds * 2),
        "last_cycle": timestamps[-1] if timestamps else None,
    }


@app.get("/api/health")
def health(hours: int = 72, hour_of_day: int | None = None):
    """Automatic good/bad verdict per optimizer. See src/analysis.py for the
    method — peer-relative interval energy, in 15-minute windows of
    measurement time, good light only.
    hour_of_day (0-23, site local time) restricts scoring to that hour."""
    if not cfg.db_path.exists():
        raise HTTPException(503, "Database not created yet — is the poller running?")
    if hour_of_day is not None and not 0 <= hour_of_day <= 23:
        raise HTTPException(422, "hour_of_day must be between 0 and 23")
    return analysis.score_optimizers(
        cfg.db_path, hours=hours, hour_of_day=hour_of_day,
        cfg_timezone=cfg.site_timezone, decommissioned=cfg.decommissioned,
    )


@app.get("/api/hourly")
def hourly(hours: int = 168):
    """Output-vs-peers by hour of day, per optimizer — the shading-versus-
    fault discriminator. A notch at particular hours is shade moving across
    the panel; a flat low line is the hardware."""
    if not cfg.db_path.exists():
        raise HTTPException(503, "Database not created yet — is the poller running?")
    return analysis.hourly_profile(
        cfg.db_path, hours=hours, cfg_timezone=cfg.site_timezone,
        decommissioned=cfg.decommissioned,
    )


@app.get("/api/errors")
def errors(limit: int = 50):
    """Failed poll cycles only — our own network/auth problems, never
    optimizer behaviour."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT started_at, status, error_message, duration_ms
            FROM poll_cycles
            WHERE status NOT IN ('OK','RUNNING')
            ORDER BY id DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


# Inverter producing at least this much DC counts as daylight for the
# cloud-delay check; below it, optimizers legitimately stop reporting.
PRODUCING_MIN_W = 50
# Normal cloud delay is ~2 min. Past this while producing, SolarEdge is
# receiving the inverter's uploads late (seen: 5+ hours on 2026-09-24).
BACKLOG_MINUTES = 15


@app.get("/api/inverter")
def inverter():
    """Today (site-local) from Modbus, plus how far behind the cloud feed is.

    Cloud delay = poll time minus the newest optimizer measurement in that
    poll. It is only meaningful while the inverter is producing — after dark
    optimizers stop reporting and the gap grows for a legitimate reason — so
    points where Modbus shows no production are returned as null.
    """
    tz = ZoneInfo(cfg.site_timezone)
    midnight = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    since = midnight.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with _connect() as conn:
        inv = [dict(r) for r in conn.execute(
            """
            SELECT fetched_at, dc_power, ac_power, ac_voltage, temperature, status, lifetime_wh
            FROM inverter_readings WHERE fetched_at >= ? ORDER BY fetched_at
            """,
            (since,),
        )]
        latest = conn.execute(
            """
            SELECT fetched_at, dc_power, ac_power, ac_voltage, dc_voltage, temperature, status
            FROM inverter_readings ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
        reads_1h = conn.execute(
            "SELECT COUNT(*) FROM inverter_readings "
            "WHERE fetched_at > strftime('%Y-%m-%dT%H:%M:%SZ','now','-1 hour')"
        ).fetchone()[0]
        fails_1h = conn.execute(
            "SELECT COUNT(*) FROM modbus_failures "
            "WHERE occurred_at > strftime('%Y-%m-%dT%H:%M:%SZ','now','-1 hour')"
        ).fetchone()[0]
        cycles = conn.execute(
            """
            SELECT c.started_at, MAX(r.ts_utc) AS newest
            FROM poll_cycles c JOIN readings r ON r.cycle_id = c.id
            WHERE c.status = 'OK' AND c.started_at >= ?
            GROUP BY c.id ORDER BY c.id
            """,
            (since,),
        ).fetchall()

    # Drop stale snapshots: the lifetime counter only rises, so a row behind
    # the running maximum is an old register snapshot under a new timestamp
    # (the sampler rejects these since 2026-09-25; older rows still have them).
    fresh_inv, high = [], 0
    for r in inv:
        if r["lifetime_wh"] and r["lifetime_wh"] < high:
            continue
        high = max(high, r["lifetime_wh"] or 0)
        fresh_inv.append(r)
    inv = fresh_inv

    inv_times = [_parse_ts(r["fetched_at"]) for r in inv]

    def producing_at(t: datetime) -> bool | None:
        i = bisect.bisect_left(inv_times, t)
        near = [j for j in (i - 1, i) if 0 <= j < len(inv)]
        if not near:
            return None
        j = min(near, key=lambda k: abs((inv_times[k] - t).total_seconds()))
        if abs((inv_times[j] - t).total_seconds()) > 300:
            return None
        return (inv[j]["dc_power"] or 0) >= PRODUCING_MIN_W

    delay = []
    for c in cycles:
        t = _parse_ts(c["started_at"])
        minutes = (t - _parse_ts(c["newest"])).total_seconds() / 60 if c["newest"] else None
        delay.append({
            "at": c["started_at"],
            "minutes": round(minutes, 1) if minutes is not None and producing_at(t) else None,
        })

    current_delay = next((d["minutes"] for d in reversed(delay)), None) if delay else None
    # Today's figures use the same definitions as the stored daily history
    # (src/inverter_stats.py), so today never changes meaning at midnight.
    with _connect() as conn:
        today = daily.compute_day(conn, midnight.date(), tz, is_today=True) or {}

    # For the data-health flags: how old the newest SolarEdge measurement is
    # regardless of production (the chart series blanks it after dark), and
    # whether the inverter is producing *now* per a recent Modbus read.
    now = datetime.now(timezone.utc)
    last_cycle = cycles[-1] if cycles else None
    newest_se = last_cycle["newest"] if last_cycle else None
    latest_age = (now - _parse_ts(latest["fetched_at"])).total_seconds() if latest else None
    producing = None
    if latest and latest_age is not None and latest_age < 600:
        producing = (latest["dc_power"] or 0) >= PRODUCING_MIN_W
    # After production stops, "how old is the newest SolarEdge data" says
    # nothing on its own; what matters is whether it reaches the time the
    # inverter last produced. If not, that stretch hasn't arrived.
    last_producing = next(
        (r["fetched_at"] for r in reversed(inv) if (r["dc_power"] or 0) >= PRODUCING_MIN_W), None)

    return {
        "latest": dict(latest) if latest else None,
        "latest_age_seconds": round(latest_age) if latest_age is not None else None,
        "latest_status_name": modbus_client.STATUS_NAMES.get(latest["status"], "?") if latest else None,
        "modbus_configured": bool(cfg.inverter_host),
        "inverter_producing": producing,
        "last_producing_at": last_producing,
        "solaredge_behind_production_minutes": (
            round((_parse_ts(last_producing) - _parse_ts(newest_se)).total_seconds() / 60, 1)
            if last_producing and newest_se else None),
        "newest_solaredge_measurement": newest_se,
        "solaredge_age_minutes": round((now - _parse_ts(newest_se)).total_seconds() / 60, 1) if newest_se else None,
        "timezone": cfg.site_timezone,
        "array_nameplate_w": cfg.array_nameplate_w or None,
        "modbus_interval_seconds": cfg.modbus_interval_seconds,
        "reads_last_hour": reads_1h,
        "failures_last_hour": fails_1h,
        "today_peak_dc_w": today.get("peak_dc_w"),
        "today_peak_at": today.get("peak_at"),
        "today_energy_wh": today.get("energy_wh"),
        "today_avg_dc_w": today.get("avg_dc_w"),
        "today_producing_hours": today.get("producing_hours"),
        "today_first_producing_at": today.get("first_producing_at"),
        "today_partial": bool(today.get("partial")),
        "cloud_delay_minutes": current_delay,
        "cloud_backlogged": current_delay is not None and current_delay > BACKLOG_MINUTES,
        "backlog_threshold_minutes": BACKLOG_MINUTES,
        "series": [{"at": r["fetched_at"], "dc": r["dc_power"], "ac": r["ac_power"]} for r in inv],
        "delay_series": delay,
    }


@app.get("/api/inverter/history")
def inverter_history(days: int = 365):
    """One row per site-local day from inverter_daily (kept indefinitely),
    with today recomputed live so it's never up to an hour stale."""
    tz = ZoneInfo(cfg.site_timezone)
    today = datetime.now(tz).date()
    since = (today - timedelta(days=max(1, days) - 1)).isoformat()
    with _connect() as conn:
        try:
            rows = {r["day"]: dict(r) for r in conn.execute(
                "SELECT * FROM inverter_daily WHERE day >= ? ORDER BY day", (since,))}
        except sqlite3.OperationalError:     # poller hasn't created the table yet
            rows = {}
        live = daily.compute_day(conn, today, tz, is_today=True)
    if live:
        rows[today.isoformat()] = {"day": today.isoformat(), "finalized": 0, **live}
    return {
        "timezone": cfg.site_timezone,
        "array_nameplate_w": cfg.array_nameplate_w or None,
        "retention_days": cfg.inverter_retention_days,
        "days": [rows[k] for k in sorted(rows)],
    }


def _panel_days(conn, since: str, serial: str | None = None) -> dict[str, dict[str, dict]]:
    """{day: {serial: figures}} from panel_daily, with today computed live."""
    tz = ZoneInfo(cfg.site_timezone)
    today = datetime.now(tz).date()
    out: dict[str, dict[str, dict]] = {}
    try:
        q = "SELECT * FROM panel_daily WHERE day >= ?" + (" AND serial = ?" if serial else "")
        for r in conn.execute(q, (since, serial) if serial else (since,)):
            if r["serial"] not in cfg.decommissioned:
                out.setdefault(r["day"], {})[r["serial"]] = dict(r)
    except sqlite3.OperationalError:          # poller hasn't created the table yet
        pass
    inv_today = daily.compute_day(conn, today, tz, is_today=True) or {}
    energy = inv_today.get("energy_wh") if not inv_today.get("partial") else None
    live = panel_daily.compute_day(conn, today, tz, cfg.decommissioned, energy)
    if serial:
        live = {k: v for k, v in live.items() if k == serial}
    if live:
        out[today.isoformat()] = {k: {"day": today.isoformat(), "serial": k, "finalized": 0, **v}
                                  for k, v in live.items()}
    return out


@app.get("/api/optimizers/{serial}/daily")
def optimizer_daily(serial: str, days: int = 365):
    """One row per day for one optimizer: measured and estimated energy,
    coverage, average/peak power, near-0 share, vs peers."""
    tz = ZoneInfo(cfg.site_timezone)
    since = (datetime.now(tz).date() - timedelta(days=max(1, days) - 1)).isoformat()
    with _connect() as conn:
        per_day = _panel_days(conn, since, serial)
    return {
        "serial": serial,
        "timezone": cfg.site_timezone,
        "retention_days": cfg.optimizer_retention_days,
        "days": [per_day[d][serial] for d in sorted(per_day) if serial in per_day[d]],
    }


@app.get("/api/panels/daily")
def panels_daily(days: int = 30):
    """Every optimizer's daily figures over a range — the panel comparison page."""
    tz = ZoneInfo(cfg.site_timezone)
    since = (datetime.now(tz).date() - timedelta(days=max(1, days) - 1)).isoformat()
    with _connect() as conn:
        per_day = _panel_days(conn, since)
        opts = [dict(r) for r in conn.execute("SELECT serial, label, panel_model FROM optimizers")
                if r["serial"] not in cfg.decommissioned]
        try:
            inv = {r["day"]: dict(r) for r in conn.execute(
                "SELECT day, energy_wh, partial, se_coverage_pct FROM inverter_daily WHERE day >= ?", (since,))}
        except sqlite3.OperationalError:
            inv = {}
    return {
        "timezone": cfg.site_timezone,
        "retention_days": cfg.optimizer_retention_days,
        "optimizers": opts,
        "days": sorted(per_day),
        "values": per_day,
        "inverter": inv,
    }


@app.get("/api/inverter/day")
def inverter_day(date: str):
    """Every inverter reading for one site-local day (while raw readings are
    retained), stale snapshots removed, plus that day's summary."""
    tz = ZoneInfo(cfg.site_timezone)
    try:
        day = datetime.strptime(date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(422, "date must be YYYY-MM-DD")
    with _connect() as conn:
        rows, baseline = daily.day_readings(conn, day, tz)
        summary = daily.compute_day(conn, day, tz, is_today=(day == datetime.now(tz).date()))
    rows = inverter_stats.drop_stale(rows, floor=(baseline or {}).get("lifetime_wh") or 0)
    return {
        "date": date,
        "timezone": cfg.site_timezone,
        "summary": summary,
        "series": [
            {"at": r["fetched_at"], "dc": r["dc_power"], "ac": r["ac_power"],
             "ac_v": r["ac_voltage"], "temp": r["temperature"], "status": r["status"]}
            for r in rows
        ],
    }


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
