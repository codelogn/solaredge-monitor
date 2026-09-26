import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import daily, db, inverter_stats  # noqa: E402

UTC = "UTC"


def _row(t: datetime, dc: float, lifetime: int, **kw) -> dict:
    return {"fetched_at": t.strftime("%Y-%m-%dT%H:%M:%SZ"), "dc_power": dc, "ac_power": dc * 0.98,
            "ac_voltage": kw.get("ac_v", 240.0), "temperature": kw.get("temp", 40.0),
            "status": kw.get("status", 4 if dc else 2), "lifetime_wh": lifetime}


def _day(start_hour=6, end_hour=20, step_s=30, dc=lambda h: 1000.0 if 8 <= h < 18 else 0.0):
    """A synthetic day: readings every step_s, counter rising with output."""
    t = datetime(2026, 9, 20, start_hour, tzinfo=timezone.utc)
    rows, lifetime = [], 1_000_000
    while t < datetime(2026, 9, 20, end_hour, tzinfo=timezone.utc):
        p = dc(t.hour + t.minute / 60)
        lifetime += round(p * step_s / 3600)
        rows.append(_row(t, p, lifetime))
        t += timedelta(seconds=step_s)
    return rows


def test_average_is_time_weighted_while_producing():
    s = inverter_stats.summarize(_day())
    assert s["avg_dc_w"] == 1000.0           # night readings don't dilute it
    assert s["peak_dc_w"] == 1000.0
    assert 9.9 < s["producing_hours"] <= 10.0


def test_average_skips_long_gaps_instead_of_guessing():
    rows = _day(dc=lambda h: 500.0 if 8 <= h < 12 else (1500.0 if 12 <= h < 16 else 0.0))
    # Reads fail for most of the 1500 W afternoon: only its first 5 minutes survive.
    gap = [r for r in rows if not ("12:05" <= r["fetched_at"][11:16] < "16:00")]
    s = inverter_stats.summarize(gap)
    # Joining 12:05 to 16:00 across the gap would have invented ~4 h of data.
    assert s["avg_dc_w"] < 600


def test_stale_snapshots_are_dropped():
    rows = _day()
    stale = dict(rows[800], lifetime_wh=rows[700]["lifetime_wh"], dc_power=5.0)
    rows.insert(801, stale)                  # an old register snapshot under a new time
    assert all(r is not stale for r in inverter_stats.drop_stale(rows))


def test_energy_uses_overnight_baseline_but_not_a_stale_one():
    rows = _day(start_hour=9)                # first read of the day is already producing
    baseline = _row(datetime(2026, 9, 20, 2, tzinfo=timezone.utc), 0, 1_000_000)
    with_base = inverter_stats.summarize(rows, baseline)
    without = inverter_stats.summarize(rows)
    assert with_base["energy_wh"] > without["energy_wh"]   # 08:00-09:00 isn't lost
    old = _row(datetime(2026, 9, 19, 12, tzinfo=timezone.utc), 0, 900_000)
    assert inverter_stats.summarize(rows, old)["energy_wh"] == without["energy_wh"]


def _seed(db_path, rows, fresh_ts=()):
    db.init_db(db_path)
    with db.connect(db_path) as conn:
        for r in rows:
            conn.execute(
                """INSERT INTO inverter_readings (fetched_at, dc_power, ac_power, ac_voltage,
                   temperature, status, lifetime_wh) VALUES (?,?,?,?,?,?,?)""",
                (r["fetched_at"], r["dc_power"], r["ac_power"], r["ac_voltage"],
                 r["temperature"], r["status"], r["lifetime_wh"]))
        if fresh_ts:
            cid = conn.execute("INSERT INTO poll_cycles (started_at, status) VALUES ('2026-09-20T00:00:00Z','OK')").lastrowid
            for serial in ("A", "B"):
                conn.execute("INSERT OR IGNORE INTO optimizers (serial, first_seen, last_seen) VALUES (?, 'x', 'x')", (serial,))
                for ts in fresh_ts:
                    conn.execute(
                        """INSERT INTO readings (cycle_id, optimizer_serial, status, fetched_at, ts_utc, power)
                           VALUES (?, ?, 'FRESH', ?, ?, 100)""", (cid, serial, ts, ts))
        conn.commit()


def test_daily_refresh_flags_partial_day_and_missing_per_panel_data(tmp_path):
    db_path = tmp_path / "t.db"
    # Modbus came online at 13:00, mid-production; per-panel data only
    # arrived for 13:00-15:00.
    rows = [r for r in _day() if r["fetched_at"][11:13] >= "13"]
    morning = [f"2026-09-20T{h:02d}:{m:02d}:00Z" for h in range(13, 15) for m in range(0, 60, 5)]
    _seed(db_path, rows, fresh_ts=morning)

    days = daily.refresh(db_path, UTC, today=date(2026, 9, 25))
    assert days == ["2026-09-20"]
    r = sqlite3.connect(db_path).execute(
        "SELECT partial, se_coverage_pct, se_missing_minutes, finalized FROM inverter_daily").fetchone()
    partial, coverage, missing, finalized = r
    assert partial == 1                      # didn't see production start
    assert 35 < coverage < 45                # 13:00-15:00 of 13:00-18:00
    assert missing == 180
    assert finalized == 1                    # old enough to freeze


def test_finalized_days_are_not_recomputed_and_retention_keeps_summaries(tmp_path):
    db_path = tmp_path / "t.db"
    _seed(db_path, _day())
    daily.refresh(db_path, UTC, today=date(2026, 9, 25))
    assert daily.refresh(db_path, UTC, today=date(2026, 9, 25)) == []   # frozen

    db.prune_inverter_raw(db_path, keep_days=1)       # the synthetic day is older than that
    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM inverter_readings").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM inverter_daily").fetchone()[0] == 1
