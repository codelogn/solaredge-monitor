"""
Summary statistics for one day of inverter (Modbus) readings.

One definition, used by both the live "today" cards (webapp) and the stored
daily history (src/daily.py), so a day's figures never change meaning when
it moves from "today" into history.

Readings are expected in time order, as dicts with at least fetched_at,
dc_power, ac_power, ac_voltage, temperature, status and lifetime_wh.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# DC power at or above this counts as producing.
PRODUCING_MIN_W = 50
# Samples further apart than this aren't joined when averaging: over a long
# gap (failed reads) we don't know what the power did.
MAX_SAMPLE_GAP_SECONDS = 600
# A reading before local midnight is only trusted as the day's energy
# baseline if it's this recent — i.e. it bridges the night, not a daytime
# outage whose energy belongs to the previous day.
BASELINE_MAX_AGE_HOURS = 12
# SunSpec states that mean the inverter is limiting or has faulted.
ABNORMAL_STATUSES = (5, 7)


def parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def day_bounds(day: date, tz: ZoneInfo) -> tuple[str, str]:
    """UTC [start, end) strings for one site-local calendar day."""
    start = datetime(day.year, day.month, day.day, tzinfo=tz)
    end = datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=tz)
    return iso(start), iso(end)


def drop_stale(rows: list[dict], floor: int = 0) -> list[dict]:
    """Remove stale register snapshots.

    The lifetime counter only rises, so a row behind the running maximum is
    an old snapshot the inverter served under a new timestamp (seen over a
    lossy link). Its power figures belong to an earlier moment.
    """
    kept, high = [], floor or 0
    for r in rows:
        lt = r.get("lifetime_wh")
        if lt and lt < high:
            continue
        if lt:
            high = lt
        kept.append(r)
    return kept


def drop_lagging(rows: list[dict], min_share: float = 0.25) -> list[dict]:
    """Remove readings whose lifetime counter barely moved while the inverter
    was producing — a stuck snapshot that crept forward rather than going
    backwards, so drop_stale() can't see it. Seen 2026-09-26: +2 Wh over
    8 minutes at ~1.1 kW, after a run of failed reads; used at an hour
    boundary it moved ~140 Wh from one hour into the next.

    A reading is dropped when the counter rose by less than min_share of the
    energy the DC power on both sides implies (with 20% allowance for
    conversion losses). Needs dc_power on each row."""
    kept: list[dict] = []
    for r in rows:
        if kept:
            a = kept[-1]
            dt = (parse_ts(r["fetched_at"]) - parse_ts(a["fetched_at"])).total_seconds()
            low = min(a.get("dc_power") or 0, r.get("dc_power") or 0)
            expected = low * dt / 3600 * 0.8
            rise = (r.get("lifetime_wh") or 0) - (a.get("lifetime_wh") or 0)
            if expected >= 20 and rise < min_share * expected:
                continue
        kept.append(r)
    return kept


def summarize(rows: list[dict], baseline: dict | None = None) -> dict:
    """Figures for one day.

    rows: that day's readings, time-ordered (stale snapshots are dropped
    here). baseline: the last reading before the day began, if any — used
    as the energy starting point so production before the first successful
    read of the day isn't lost.
    """
    rows = drop_stale(rows, floor=(baseline or {}).get("lifetime_wh") or 0)
    out = {
        "samples": len(rows),
        "energy_wh": None,
        "peak_dc_w": None, "peak_at": None,
        "avg_dc_w": None, "producing_hours": None,
        "first_producing_at": None, "last_producing_at": None,
        "max_temp_c": None, "min_ac_v": None, "max_ac_v": None,
        "abnormal_samples": 0,
    }
    if not rows:
        return out

    # Energy: rise of the inverter's own counter across the day.
    lifetimes = [r["lifetime_wh"] for r in rows if r.get("lifetime_wh")]
    start = None
    if baseline and baseline.get("lifetime_wh"):
        age_h = (parse_ts(rows[0]["fetched_at"]) - parse_ts(baseline["fetched_at"])).total_seconds() / 3600
        if age_h <= BASELINE_MAX_AGE_HOURS:
            start = baseline["lifetime_wh"]
    if lifetimes:
        out["energy_wh"] = lifetimes[-1] - (start if start is not None else lifetimes[0])

    producing = [r for r in rows if (r.get("dc_power") or 0) >= PRODUCING_MIN_W]
    if producing:
        peak = max(producing, key=lambda r: r["dc_power"])
        out["peak_dc_w"] = peak["dc_power"]
        out["peak_at"] = peak["fetched_at"]
        out["first_producing_at"] = producing[0]["fetched_at"]
        out["last_producing_at"] = producing[-1]["fetched_at"]
        out["producing_hours"] = round(
            (parse_ts(producing[-1]["fetched_at"]) - parse_ts(producing[0]["fetched_at"])).total_seconds() / 3600, 2)
        temps = [r["temperature"] for r in producing if r.get("temperature") is not None]
        volts = [r["ac_voltage"] for r in producing if r.get("ac_voltage")]
        out["max_temp_c"] = max(temps) if temps else None
        out["min_ac_v"] = min(volts) if volts else None
        out["max_ac_v"] = max(volts) if volts else None

    # Time-weighted average DC power while producing: each interval between
    # consecutive samples counts for its duration, and intervals across a
    # long gap are skipped rather than guessed.
    weighted, seconds = 0.0, 0.0
    for a, b in zip(rows, rows[1:]):
        dt = (parse_ts(b["fetched_at"]) - parse_ts(a["fetched_at"])).total_seconds()
        pa, pb = a.get("dc_power") or 0, b.get("dc_power") or 0
        if 0 < dt <= MAX_SAMPLE_GAP_SECONDS and pa >= PRODUCING_MIN_W and pb >= PRODUCING_MIN_W:
            weighted += dt * (pa + pb) / 2
            seconds += dt
    if seconds:
        out["avg_dc_w"] = round(weighted / seconds, 1)

    out["abnormal_samples"] = sum(1 for r in rows if r.get("status") in ABNORMAL_STATUSES)
    return out
