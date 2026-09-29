"""
Daily inverter history: rolls raw Modbus readings up into one row per
site-local day (inverter_daily) and enforces raw-data retention.

Run by the poller on startup and hourly thereafter. Recent days are
recomputed until they're FINALIZE_AFTER_DAYS old, because SolarEdge's
per-panel data for a day can still arrive late; after that a day is frozen.
The daily table is never pruned, so history outlives the raw readings.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from . import db
from .inverter_stats import PRODUCING_MIN_W, day_bounds, parse_ts, summarize

logger = logging.getLogger(__name__)

FINALIZE_AFTER_DAYS = 2
# Same window the optimizer analysis uses (src/analysis.py).
WINDOW_SECONDS = 900

_READING_COLS = "fetched_at, dc_power, ac_power, ac_voltage, temperature, status, lifetime_wh"


def _ro(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def day_readings(conn: sqlite3.Connection, day: date, tz: ZoneInfo) -> tuple[list[dict], dict | None]:
    """A day's raw readings, plus the last reading before it (energy baseline)."""
    start, end = day_bounds(day, tz)
    rows = [dict(r) for r in conn.execute(
        f"SELECT {_READING_COLS} FROM inverter_readings "
        "WHERE fetched_at >= ? AND fetched_at < ? ORDER BY fetched_at", (start, end))]
    base = conn.execute(
        f"SELECT {_READING_COLS} FROM inverter_readings "
        "WHERE fetched_at < ? AND lifetime_wh IS NOT NULL ORDER BY fetched_at DESC LIMIT 1",
        (start,)).fetchone()
    return rows, (dict(base) if base else None)


def compute_day(conn: sqlite3.Connection, day: date, tz: ZoneInfo,
                is_today: bool = False) -> dict | None:
    """Full daily figures, or None if there are no inverter readings that day."""
    rows, baseline = day_readings(conn, day, tz)
    if not rows:
        return None
    start, end = day_bounds(day, tz)
    s = summarize(rows, baseline)
    s["reads_ok"] = s["samples"]
    s["reads_failed"] = conn.execute(
        "SELECT COUNT(*) FROM modbus_failures WHERE occurred_at >= ? AND occurred_at < ?",
        (start, end)).fetchone()[0]

    # Partial: Modbus didn't see the start or end of production, so energy
    # and averages cover only part of the day. At the start that means the
    # day's first read was already producing and there was no overnight
    # baseline; at the end, the last read was still producing (past days only).
    first, last = rows[0], rows[-1]
    no_baseline = not baseline or (
        (parse_ts(first["fetched_at"]) - parse_ts(baseline["fetched_at"])).total_seconds() > 12 * 3600)
    s["partial"] = int(
        (no_baseline and (first.get("dc_power") or 0) >= PRODUCING_MIN_W)
        or (not is_today and (last.get("dc_power") or 0) >= PRODUCING_MIN_W))

    # Per-panel completeness: of the 15-minute windows in which the inverter
    # was producing, how many have SolarEdge measurements from at least half
    # the optimizers? A late backlog can deliver a stretch but skip the middle
    # of the afternoon, so the tail alone says nothing.
    s["se_last_measured_at"] = conn.execute(
        "SELECT MAX(ts_utc) FROM readings WHERE status = 'FRESH' AND ts_utc >= ? AND ts_utc < ?",
        (start, end)).fetchone()[0]
    s["se_coverage_pct"], s["se_missing_minutes"] = _se_coverage(conn, rows, start, end)
    return s


def _se_coverage(conn, rows, start, end) -> tuple[float | None, float | None]:
    producing = {
        int(parse_ts(r["fetched_at"]).timestamp() // WINDOW_SECONDS)
        for r in rows if (r.get("dc_power") or 0) >= PRODUCING_MIN_W
    }
    if not producing:
        return None, None
    per_window: dict[int, set] = {}
    for serial, ts in conn.execute(
            "SELECT optimizer_serial, ts_utc FROM readings "
            "WHERE status = 'FRESH' AND ts_utc >= ? AND ts_utc < ?", (start, end)):
        per_window.setdefault(int(parse_ts(ts).timestamp() // WINDOW_SECONDS), set()).add(serial)
    active = len(set().union(*per_window.values())) if per_window else 0
    need = max(1, active // 2)
    covered = sum(1 for w in producing if len(per_window.get(w, ())) >= need) if active else 0
    return round(100 * covered / len(producing), 1), (len(producing) - covered) * WINDOW_SECONDS / 60


def refresh(db_path: Path, tz_name: str, today: date | None = None) -> list[str]:
    """Recompute every day that isn't finalized yet. Returns the days written."""
    tz = ZoneInfo(tz_name)
    today = today or datetime.now(tz).date()
    conn = _ro(db_path)
    try:
        first = conn.execute("SELECT MIN(fetched_at) FROM inverter_readings").fetchone()[0]
        if not first:
            return []
        done = {r[0] for r in conn.execute("SELECT day FROM inverter_daily WHERE finalized = 1")}
        day = parse_ts(first).astimezone(tz).date()
        results = []
        while day <= today:
            key = day.isoformat()
            if key not in done:
                s = compute_day(conn, day, tz, is_today=(day == today))
                if s is not None:
                    s["finalized"] = 1 if (today - day).days >= FINALIZE_AFTER_DAYS else 0
                    results.append((key, s))
            day += timedelta(days=1)
    finally:
        conn.close()

    for key, s in results:
        db.upsert_inverter_daily(db_path, key, s)
    return [k for k, _ in results]


def run_maintenance(db_path: Path, tz_name: str, retention_days: int,
                    optimizer_retention_days: int = 0, decommissioned=frozenset(),
                    grouping=None) -> None:
    """Daily roll-ups, then raw retention. Inverter first (per-panel
    estimates use its daily energy), and roll-ups before pruning, so a day is
    always summarised before its raw readings can be deleted."""
    from . import panel_daily   # avoid an import cycle at module load

    days = refresh(db_path, tz_name)
    if days:
        logger.info("Inverter daily history updated: %s", ", ".join(days))
    pdays = panel_daily.refresh(db_path, tz_name, decommissioned, grouping=grouping)
    if pdays:
        logger.info("Per-panel daily history updated: %s", ", ".join(pdays))
    if retention_days > 0:
        raw, fails = db.prune_inverter_raw(db_path, retention_days)
        if raw or fails:
            logger.info("Pruned %d inverter readings and %d Modbus failures older than %d days",
                        raw, fails, retention_days)
    if optimizer_retention_days > 0:
        n = db.prune_optimizer_raw(db_path, optimizer_retention_days)
        if n:
            logger.info("Pruned %d optimizer readings older than %d days", n, optimizer_retention_days)
