"""
Daily per-panel (per-optimizer) history: one row per optimizer per
site-local day in panel_daily, rolled up from the raw SolarEdge readings.

Two energy figures are kept, because per-panel data only arrives through
SolarEdge's cloud and some days it arrives incomplete:

  measured_wh   — the panel's own power readings integrated over the time
                  they cover (time-weighted; gaps over MAX_GAP_SECONDS are
                  skipped, not guessed). Exact for what was delivered, but
                  short on days when stretches never arrived.
  estimated_wh  — the inverter's measured energy for the day (Modbus) split
                  between panels in proportion to their measured_wh. The 20
                  figures add up to what the inverter actually produced, but
                  the split assumes each panel's share during undelivered
                  hours matched the delivered ones — false for a panel
                  shaded only in the (missing) afternoon.

coverage_pct says how much to trust either: the share of the day's
producing 15-minute windows in which this optimizer has readings. Checked
on a complete day, the panels' measured total came within ~10% of the
inverter (DC before conversion losses vs AC after).

Same finalisation rule as the inverter history (src/daily.py): recomputed
until FINALIZE_AFTER_DAYS old, then frozen. The table is never pruned.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import median
from zoneinfo import ZoneInfo

from . import analysis, db
from .inverter_stats import PRODUCING_MIN_W, day_bounds, parse_ts

FINALIZE_AFTER_DAYS = 2
WINDOW_SECONDS = analysis.WINDOW_SECONDS
# Optimizers report every 3-6 min; a gap longer than this is missing data.
MAX_GAP_SECONDS = 900


def _window(ts: str) -> int:
    return int(parse_ts(ts).timestamp() // WINDOW_SECONDS)


def _producing_windows(conn, start: str, end: str, rows: list[dict]) -> set[int]:
    """Windows in which the array was producing, by either source: the
    inverter sees production the optimizers' data never delivered, and the
    optimizers cover stretches before Modbus was read (or when it failed).
    Using only one would inflate or deflate coverage on exactly the days
    one source is incomplete."""
    inv = {
        _window(t) for t, p in conn.execute(
            "SELECT fetched_at, dc_power FROM inverter_readings "
            "WHERE fetched_at >= ? AND fetched_at < ?", (start, end))
        if (p or 0) >= PRODUCING_MIN_W
    }
    per: dict[int, list[float]] = {}
    for r in rows:
        per.setdefault(_window(r["ts_utc"]), []).append(r["power"])
    return inv | {w for w, v in per.items() if median(v) >= 5}


def compute_day(conn, day: date, tz: ZoneInfo, decommissioned=frozenset(),
                inverter_energy_wh: int | None = None) -> dict[str, dict]:
    """{serial: figures} for one day; empty if no per-panel data that day."""
    start, end = day_bounds(day, tz)
    groups = {s: m for s, m in conn.execute("SELECT serial, panel_model FROM optimizers")}
    rows = [
        {"cycle_id": c, "optimizer_serial": s, "status": st, "ts_utc": ts,
         "power": p, "voltage": v, "current": i}
        for c, s, st, ts, p, v, i in conn.execute(
            "SELECT cycle_id, optimizer_serial, status, ts_utc, power, voltage, current "
            "FROM readings WHERE status = 'FRESH' AND power IS NOT NULL "
            "AND ts_utc >= ? AND ts_utc < ? ORDER BY ts_utc", (start, end))
        if s not in decommissioned
    ]
    if not rows:
        return {}

    producing = _producing_windows(conn, start, end, rows)
    by_serial: dict[str, list[dict]] = {}
    for r in rows:
        by_serial.setdefault(r["optimizer_serial"], []).append(r)

    # vs peers for the day, with exactly the dashboard's method.
    windows = analysis._build_windows(rows, tz)
    active = len({s for w in windows.values() for s in w["panel_power"]})
    scored = analysis._select_windows(windows, active, None)["scored"]
    ratios: dict[str, list[float]] = {}
    for k in scored:
        refs = analysis._group_medians(windows[k]["panel_power"], groups)
        for s, v in windows[k]["panel_power"].items():
            ref = analysis._reference_median(s, groups, refs)
            if ref > 0:
                ratios.setdefault(s, []).append(v / ref)

    out: dict[str, dict] = {}
    for s, rs in by_serial.items():
        wh = 0.0
        for a, b in zip(rs, rs[1:]):
            dt = (parse_ts(b["ts_utc"]) - parse_ts(a["ts_utc"])).total_seconds()
            if 0 < dt <= MAX_GAP_SECONDS:
                wh += dt / 3600 * (a["power"] + b["power"]) / 2
        day_rs = [r for r in rs if _window(r["ts_utc"]) in producing]
        peak = max(rs, key=lambda r: r["power"])
        cur = [r["current"] for r in day_rs if r["current"] is not None]
        covered = {_window(r["ts_utc"]) for r in rs} & producing
        out[s] = {
            "measured_wh": round(wh, 1),
            "coverage_pct": round(100 * len(covered) / len(producing), 1) if producing else None,
            "avg_power_w": round(sum(r["power"] for r in day_rs) / len(day_rs), 1) if day_rs else None,
            "peak_w": peak["power"],
            "peak_at": peak["ts_utc"],
            "near_zero_pct": round(100 * sum(1 for c in cur if c < analysis.NEAR_ZERO_A) / len(cur), 1) if cur else None,
            "vs_peers_pct": round(100 * median(ratios[s]), 1) if ratios.get(s) else None,
            "readings": len(rs),
            "estimated_wh": None,
        }

    total = sum(v["measured_wh"] for v in out.values())
    if inverter_energy_wh and total > 0:
        for v in out.values():
            v["estimated_wh"] = round(inverter_energy_wh * v["measured_wh"] / total, 1)
    return out


def _inverter_energy(conn, day: date) -> int | None:
    """The day's inverter energy, or None if Modbus didn't see the whole
    production day — splitting a partial total would understate every panel."""
    try:
        row = conn.execute("SELECT energy_wh, partial FROM inverter_daily WHERE day = ?",
                           (day.isoformat(),)).fetchone()
    except Exception:
        return None
    return row[0] if row and not row[1] else None


def refresh(db_path: Path, tz_name: str, decommissioned=frozenset(),
            today: date | None = None) -> list[str]:
    """Recompute every day that isn't finalized. Run after the inverter
    history refresh, which supplies each day's inverter energy."""
    import sqlite3
    tz = ZoneInfo(tz_name)
    today = today or datetime.now(tz).date()
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        first = conn.execute("SELECT MIN(ts_utc) FROM readings WHERE status = 'FRESH'").fetchone()[0]
        if not first:
            return []
        done = {r[0] for r in conn.execute("SELECT DISTINCT day FROM panel_daily WHERE finalized = 1")}
        results = []
        day = parse_ts(first).astimezone(tz).date()
        while day <= today:
            if day.isoformat() not in done:
                figures = compute_day(conn, day, tz, decommissioned, _inverter_energy(conn, day))
                if figures:
                    final = 1 if (today - day).days >= FINALIZE_AFTER_DAYS else 0
                    results.append((day.isoformat(), figures, final))
            day += timedelta(days=1)
    finally:
        conn.close()
    for key, figures, final in results:
        db.upsert_panel_daily(db_path, key, figures, final)
    return [k for k, _, _ in results]
