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

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from zoneinfo import ZoneInfo

from . import analysis, db
from . import groups as panel_groups
from .inverter_stats import PRODUCING_MIN_W, day_bounds, drop_lagging, drop_stale, parse_ts

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


def _day_rows(conn, start: str, end: str, decommissioned) -> list[dict]:
    """A day's FRESH per-optimizer readings, by measurement time."""
    return [
        {"cycle_id": c, "optimizer_serial": s, "status": st, "ts_utc": ts,
         "power": p, "voltage": v, "current": i}
        for c, s, st, ts, p, v, i in conn.execute(
            "SELECT cycle_id, optimizer_serial, status, ts_utc, power, voltage, current "
            "FROM readings WHERE status = 'FRESH' AND power IS NOT NULL "
            "AND ts_utc >= ? AND ts_utc < ? ORDER BY ts_utc", (start, end))
        if s not in decommissioned
    ]


def compute_day(conn, day: date, tz: ZoneInfo, decommissioned=frozenset(),
                inverter_energy_wh: int | None = None,
                grouping: panel_groups.Grouping | None = None) -> dict[str, dict]:
    """{serial: figures} for one day; empty if no per-panel data that day.
    vs_peers_pct compares with the grouping (whole array by default)."""
    start, end = day_bounds(day, tz)
    groups = panel_groups.from_db(conn, grouping or panel_groups.WHOLE_ARRAY)
    rows = _day_rows(conn, start, end, decommissioned)
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


# An hour names a winner only if every active panel has readings in every
# producing 15-minute window of it. Ranking is by the hour's energy, so a
# panel missing a window would be short by up to a quarter and lose unfairly.
SCORED_HOUR_COVERAGE = 100.0
# Inverter lifetime-counter readings bracketing an hour boundary must be
# this close together to interpolate the counter at the boundary. The counter
# only rises, so interpolating across a short outage of failed reads is far
# better than dropping the hour.
BOUNDARY_MAX_GAP_SECONDS = 1200


def _local_hour(t: datetime, tz: ZoneInfo) -> int:
    return t.astimezone(tz).hour


def _next_hour_start(t: datetime, tz: ZoneInfo) -> datetime:
    local = t.astimezone(tz).replace(minute=0, second=0, microsecond=0)
    return (local + timedelta(hours=1)).astimezone(timezone.utc)


def _inverter_hour_energy(conn, start: str, end: str, tz: ZoneInfo) -> dict[int, float]:
    """{local hour: Wh} from the rise of the inverter's lifetime counter,
    interpolated at each hour boundary. Hours whose boundaries aren't
    bracketed by readings close enough together are left out."""
    rows = drop_lagging(drop_stale([
        {"fetched_at": t, "lifetime_wh": lt, "dc_power": p} for t, lt, p in conn.execute(
            "SELECT fetched_at, lifetime_wh, dc_power FROM inverter_readings WHERE lifetime_wh IS NOT NULL "
            "AND fetched_at >= strftime('%Y-%m-%dT%H:%M:%SZ', ?, '-15 minutes') "
            "AND fetched_at <= strftime('%Y-%m-%dT%H:%M:%SZ', ?, '+15 minutes') ORDER BY fetched_at",
            (start, end))
    ]))
    pts = [(parse_ts(r["fetched_at"]), r["lifetime_wh"]) for r in rows]

    def counter_at(t: datetime) -> float | None:
        for (t0, v0), (t1, v1) in zip(pts, pts[1:]):
            if t0 <= t <= t1:
                gap = (t1 - t0).total_seconds()
                if gap > BOUNDARY_MAX_GAP_SECONDS:
                    return None
                return v0 if gap == 0 else v0 + (v1 - v0) * (t - t0).total_seconds() / gap
        return None

    out: dict[int, float] = {}
    t, stop = parse_ts(start), parse_ts(end)
    while t < stop:
        nxt = _next_hour_start(t, tz)
        a, b = counter_at(t), counter_at(nxt)
        if a is not None and b is not None:
            out[_local_hour(t, tz)] = round(b - a, 1)
        t = nxt
    return out


def _competition_rank(values: dict[str, float]) -> dict[str, int]:
    """1 = highest; equal values (to 0.1) share a rank, and the next rank
    skips accordingly (1, 2, 2, 4)."""
    rounded = {k: round(v, 1) for k, v in values.items()}
    return {k: 1 + sum(1 for o in rounded.values() if o > v) for k, v in rounded.items()}


def compute_hours(conn, day: date, tz: ZoneInfo, decommissioned=frozenset(),
                  grouping: panel_groups.Grouping | None = None) -> dict[int, dict[str, dict]]:
    """{local hour: {serial: figures}} for one day.

    Energy per hour integrates each panel's readings, splitting a reading
    interval that straddles an hour boundary in proportion. Panels are ranked
    by that energy — the figure the page lists and sorts by — so an hour is
    scored (ranked) only when the array is producing and every panel that
    reported that day has data in all of the hour's producing windows.
    """
    start, end = day_bounds(day, tz)
    rows = _day_rows(conn, start, end, decommissioned)
    if not rows:
        return {}
    groups = panel_groups.from_db(conn, grouping or panel_groups.WHOLE_ARRAY)
    producing = _producing_windows(conn, start, end, rows)
    by_serial: dict[str, list[dict]] = {}
    for r in rows:
        by_serial.setdefault(r["optimizer_serial"], []).append(r)
    active = set(by_serial)

    energy: dict[int, dict[str, float]] = {}
    powers: dict[int, dict[str, list[float]]] = {}
    covered: dict[int, dict[str, set[int]]] = {}
    for s, rs in by_serial.items():
        for r in rs:
            t = parse_ts(r["ts_utc"])
            h = _local_hour(t, tz)
            powers.setdefault(h, {}).setdefault(s, []).append(r["power"])
            covered.setdefault(h, {}).setdefault(s, set()).add(_window(r["ts_utc"]))
        for a, b in zip(rs, rs[1:]):
            ta, tb = parse_ts(a["ts_utc"]), parse_ts(b["ts_utc"])
            dt = (tb - ta).total_seconds()
            if not 0 < dt <= MAX_GAP_SECONDS:
                continue
            pa, pb = a["power"], b["power"]
            t = ta
            while t < tb:                       # split at hour boundaries
                seg_end = min(tb, _next_hour_start(t, tz))
                p1 = pa + (pb - pa) * (t - ta).total_seconds() / dt
                p2 = pa + (pb - pa) * (seg_end - ta).total_seconds() / dt
                h = _local_hour(t, tz)
                energy.setdefault(h, {}).setdefault(s, 0.0)
                energy[h][s] += (seg_end - t).total_seconds() / 3600 * (p1 + p2) / 2
                t = seg_end

    producing_by_hour: dict[int, set[int]] = {}
    for w in producing:
        producing_by_hour.setdefault(
            _local_hour(datetime.fromtimestamp(w * WINDOW_SECONDS, timezone.utc), tz), set()).add(w)
    inverter = _inverter_hour_energy(conn, start, end, tz)

    out: dict[int, dict[str, dict]] = {}
    for h in sorted(set(powers) | set(energy)):
        prod = producing_by_hour.get(h, set())
        per: dict[str, dict] = {}
        for s in active:
            pw = powers.get(h, {}).get(s)
            cov = (100 * len(covered.get(h, {}).get(s, set()) & prod) / len(prod)) if prod else None
            per[s] = {
                "measured_wh": round(energy.get(h, {}).get(s, 0.0), 1),
                "avg_power_w": round(sum(pw) / len(pw), 1) if pw else None,
                "coverage_pct": round(cov, 1) if cov is not None else None,
                "estimated_wh": None, "scored": 0, "rank_all": None, "rank_group": None,
            }
        total = sum(v["measured_wh"] for v in per.values())
        if h in inverter and total > 0:
            for v in per.values():
                v["estimated_wh"] = round(inverter[h] * v["measured_wh"] / total, 1)

        avgs = [v["avg_power_w"] for v in per.values() if v["avg_power_w"] is not None]
        complete = bool(prod) and len(avgs) == len(active) and all(
            (v["coverage_pct"] or 0) >= SCORED_HOUR_COVERAGE for v in per.values())
        if complete and median(avgs) >= analysis.MIN_SITE_MEDIAN_W:
            wh = {s: v["measured_wh"] for s, v in per.items()}
            rank_all = _competition_rank(wh)
            by_group: dict[str, dict[str, float]] = {}
            for s, v in wh.items():
                by_group.setdefault(groups.get(s) or "", {})[s] = v
            for s, v in per.items():
                g = groups.get(s) or ""
                members = by_group.get(g, {})
                own = members if g and len(members) >= analysis.MIN_GROUP_FOR_OWN_MEDIAN else wh
                v.update(scored=1, rank_all=rank_all[s], rank_group=_competition_rank(own)[s])
        out[h] = per
    return out


def leaderboard(rows, basis: str = "all") -> dict[str, dict]:
    """Per serial: wins (hours ranked 1), top-3 finishes, average rank and
    scored hours, from panel_hourly-shaped rows. basis 'all' or 'group'."""
    key = "rank_group" if basis == "group" else "rank_all"
    out: dict[str, dict] = {}
    for r in rows:
        if not r.get("scored") or r.get(key) is None:
            continue
        d = out.setdefault(r["serial"], {"wins": 0, "top3": 0, "scored_hours": 0, "_sum": 0})
        rank = r[key]
        d["wins"] += rank == 1
        d["top3"] += rank <= 3
        d["scored_hours"] += 1
        d["_sum"] += rank
    for d in out.values():
        d["avg_rank"] = round(d.pop("_sum") / d["scored_hours"], 2)
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
            today: date | None = None,
            grouping: panel_groups.Grouping | None = None) -> list[str]:
    """Recompute every day that isn't finalized. Run after the inverter
    history refresh, which supplies each day's inverter energy.

    If the panel grouping changed since the stored history was computed,
    every day whose raw readings still exist is recomputed with the new one
    (older days keep the grouping they were computed with)."""
    import sqlite3
    grouping = grouping or panel_groups.WHOLE_ARRAY
    if db.get_meta(db_path, "panel_grouping") != grouping.signature():
        db.unfinalize_panel_history(db_path)
        db.set_meta(db_path, "panel_grouping", grouping.signature())
    tz = ZoneInfo(tz_name)
    today = today or datetime.now(tz).date()
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        first = conn.execute("SELECT MIN(ts_utc) FROM readings WHERE status = 'FRESH'").fetchone()[0]
        if not first:
            return []
        # A day is done only once both its daily and hourly rows are frozen,
        # so days finalized before the hourly table existed still get hours.
        done = ({r[0] for r in conn.execute("SELECT DISTINCT day FROM panel_daily WHERE finalized = 1")}
                & {r[0] for r in conn.execute("SELECT DISTINCT day FROM panel_hourly WHERE finalized = 1")})
        results = []
        day = parse_ts(first).astimezone(tz).date()
        while day <= today:
            if day.isoformat() not in done:
                figures = compute_day(conn, day, tz, decommissioned, _inverter_energy(conn, day), grouping)
                if figures:
                    final = 1 if (today - day).days >= FINALIZE_AFTER_DAYS else 0
                    hours = compute_hours(conn, day, tz, decommissioned, grouping)
                    results.append((day.isoformat(), figures, hours, final))
            day += timedelta(days=1)
    finally:
        conn.close()
    for key, figures, hours, final in results:
        db.upsert_panel_daily(db_path, key, figures, final)
        db.upsert_panel_hourly(db_path, key, hours, final)
    return [k for k, _, _, _ in results]
