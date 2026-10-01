import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import daily, db, groups, panel_daily  # noqa: E402

UTC = ZoneInfo("UTC")
DAY = datetime(2026, 9, 20, tzinfo=timezone.utc)


def _ts(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _seed(db_path, panels: dict[str, float], models: dict[str, str] | None = None,
          hours=(8, 16), step_min=5, missing=None, inverter=True):
    """panels: {serial: watts}, steady from hours[0] to hours[1].
    missing: (serial, start_hour, end_hour) with no data for that panel."""
    db.init_db(db_path)
    with db.connect(db_path) as conn:
        cid = conn.execute("INSERT INTO poll_cycles (started_at, status) VALUES (?, 'OK')", (_ts(DAY),)).lastrowid
        for s in panels:
            conn.execute("INSERT INTO optimizers (serial, label, panel_model, first_seen, last_seen) "
                         "VALUES (?, ?, ?, 'x', 'x')", (s, f"Optimizer {s}", (models or {}).get(s, "M")))
        t = DAY.replace(hour=hours[0])
        while t < DAY.replace(hour=hours[1]):
            for s, w in panels.items():
                if missing and s == missing[0] and missing[1] <= t.hour < missing[2]:
                    continue
                conn.execute(
                    "INSERT INTO readings (cycle_id, optimizer_serial, status, fetched_at, ts_utc, "
                    "voltage, current, power) VALUES (?, ?, 'FRESH', ?, ?, 38, ?, ?)",
                    (cid, s, _ts(t), _ts(t), w / 38, w))
            t += timedelta(minutes=step_min)
        if inverter:
            lifetime, t = 1_000_000, DAY.replace(hour=5)
            while t < DAY.replace(hour=20):
                dc = sum(panels.values()) if hours[0] <= t.hour < hours[1] else 0
                lifetime += dc * 30 / 3600 * 0.97
                conn.execute(
                    "INSERT INTO inverter_readings (fetched_at, dc_power, ac_power, ac_voltage, "
                    "temperature, status, lifetime_wh) VALUES (?, ?, ?, 240, 40, 4, ?)",
                    (_ts(t), dc, dc * 0.97, round(lifetime)))
                t += timedelta(seconds=30)
        conn.commit()


def _hours(db_path, grouping=None):
    conn = sqlite3.connect(db_path)
    try:
        return panel_daily.compute_hours(conn, DAY.date(), UTC, grouping=grouping)
    finally:
        conn.close()


def test_interval_across_an_hour_boundary_is_split(tmp_path):
    db_path = tmp_path / "t.db"
    db.init_db(db_path)
    with db.connect(db_path) as conn:
        cid = conn.execute("INSERT INTO poll_cycles (started_at, status) VALUES ('x','OK')").lastrowid
        conn.execute("INSERT INTO optimizers (serial, label, panel_model, first_seen, last_seen) "
                     "VALUES ('A','A','M','x','x')")
        for t in ("2026-09-20T11:55:00Z", "2026-09-20T12:05:00Z"):
            conn.execute("INSERT INTO readings (cycle_id, optimizer_serial, status, fetched_at, ts_utc, "
                         "voltage, current, power) VALUES (?, 'A', 'FRESH', ?, ?, 38, 8, 300)", (cid, t, t))
        conn.commit()
    h = _hours(db_path)
    # 10 minutes at 300 W = 50 Wh, half on each side of noon.
    assert h[11]["A"]["measured_wh"] == 25.0
    assert h[12]["A"]["measured_wh"] == 25.0


def test_hour_estimates_add_up_to_the_inverter(tmp_path):
    db_path = tmp_path / "t.db"
    _seed(db_path, {"A": 300.0, "B": 200.0, "C": 100.0})
    h = _hours(db_path)[10]
    total = sum(f["estimated_wh"] for f in h.values())
    assert abs(total - 600 * 0.97) < 2              # the inverter's AC energy that hour
    assert abs(h["A"]["estimated_wh"] / h["C"]["estimated_wh"] - 3) < 0.01


def test_hour_is_not_ranked_when_a_panel_is_missing(tmp_path):
    db_path = tmp_path / "t.db"
    _seed(db_path, {"A": 300.0, "B": 200.0, "C": 100.0}, missing=("C", 12, 13))
    hours = _hours(db_path)
    assert all(f["scored"] == 0 and f["rank_all"] is None for f in hours[12].values())
    assert hours[11]["A"]["scored"] == 1 and hours[11]["A"]["rank_all"] == 1
    assert hours[12]["C"]["coverage_pct"] == 0.0


def test_whole_array_and_group_ranks(tmp_path):
    db_path = tmp_path / "t.db"
    big = {f"BIG-{n}": 250.0 - 10 * n for n in range(5)}
    small = {f"SMALL-{n}": 100.0 - 10 * n for n in range(5)}
    models = {**{s: "Big" for s in big}, **{s: "Small" for s in small}}
    _seed(db_path, {**big, **small}, models)
    h = _hours(db_path, groups.parse("solaredge"))[10]
    assert h["BIG-0"]["rank_all"] == 1 and h["BIG-0"]["rank_group"] == 1
    # Best of the weaker group: 6th overall, but first among its own group.
    assert h["SMALL-0"]["rank_all"] == 6
    assert h["SMALL-0"]["rank_group"] == 1
    assert h["SMALL-4"]["rank_group"] == 5


def test_equal_output_shares_a_rank(tmp_path):
    db_path = tmp_path / "t.db"
    _seed(db_path, {"A": 300.0, "B": 300.0, "C": 100.0})
    h = _hours(db_path)[10]
    assert h["A"]["rank_all"] == h["B"]["rank_all"] == 1
    assert h["C"]["rank_all"] == 3                 # competition ranking: 1, 1, 3


def test_leaderboard_counts_wins_top3_and_average_rank():
    rows = [
        {"serial": "A", "scored": 1, "rank_all": 1, "rank_group": 2},
        {"serial": "A", "scored": 1, "rank_all": 1, "rank_group": 1},
        {"serial": "A", "scored": 1, "rank_all": 4, "rank_group": 1},
        {"serial": "B", "scored": 1, "rank_all": 2, "rank_group": 3},
        {"serial": "B", "scored": 0, "rank_all": None, "rank_group": None},   # unranked hour
    ]
    lb = panel_daily.leaderboard(rows, "all")
    assert lb["A"] == {"wins": 2, "top3": 2, "scored_hours": 3, "avg_rank": 2.0}
    assert lb["B"] == {"wins": 0, "top3": 1, "scored_hours": 1, "avg_rank": 2.0}
    assert panel_daily.leaderboard(rows, "group")["A"]["wins"] == 2


def test_refresh_stores_hours_and_freezes_old_days(tmp_path):
    db_path = tmp_path / "t.db"
    _seed(db_path, {"A": 300.0, "B": 200.0, "C": 100.0})
    daily.refresh(db_path, "UTC", today=date(2026, 9, 25))
    assert panel_daily.refresh(db_path, "UTC", today=date(2026, 9, 25)) == ["2026-09-20"]
    conn = sqlite3.connect(db_path)
    n, scored, final = conn.execute(
        "SELECT COUNT(*), SUM(scored), MIN(finalized) FROM panel_hourly").fetchone()
    assert n == 8 * 3 and scored == 8 * 3 and final == 1
    assert panel_daily.refresh(db_path, "UTC", today=date(2026, 9, 25)) == []


def test_whole_array_default_makes_group_ranks_match(tmp_path):
    db_path = tmp_path / "t.db"
    _seed(db_path, {"A": 300.0, "B": 200.0, "C": 100.0, "D": 50.0, "E": 25.0},
          {"A": "X", "B": "X", "C": "Y", "D": "Y", "E": "Y"})
    h = _hours(db_path)[10]              # no grouping: labels are ignored
    assert all(f["rank_group"] == f["rank_all"] for f in h.values())


def test_changing_the_grouping_recomputes_stored_history(tmp_path):
    db_path = tmp_path / "t.db"
    _seed(db_path, {"A": 300.0, "B": 200.0, "C": 100.0})
    daily.refresh(db_path, "UTC", today=date(2026, 9, 25))
    assert panel_daily.refresh(db_path, "UTC", today=date(2026, 9, 25)) == ["2026-09-20"]
    assert panel_daily.refresh(db_path, "UTC", today=date(2026, 9, 25)) == []     # frozen
    custom = groups.parse("Front=1.0.1; Back=A,B")
    assert panel_daily.refresh(db_path, "UTC", today=date(2026, 9, 25), grouping=custom) == ["2026-09-20"]
    assert panel_daily.refresh(db_path, "UTC", today=date(2026, 9, 25), grouping=custom) == []


def test_day_profile_survives_a_collection_gap(tmp_path):
    # A reboot stopped collection from 08:00 to 10:00. The hours inside the
    # gap can't be split out, but the running total must pick up again at
    # 10:00 at the right height — the inverter's counter kept counting.
    db_path = tmp_path / "t.db"
    _seed(db_path, {"A": 300.0, "B": 200.0, "C": 100.0})
    with db.connect(db_path) as conn:
        conn.execute("INSERT INTO inverter_readings (fetched_at, dc_power, lifetime_wh) "
                     "VALUES ('2026-09-19T23:00:00Z', 0, 1000000)")      # last night's baseline
        conn.execute("DELETE FROM inverter_readings WHERE fetched_at >= '2026-09-20T07:55:00Z' "
                     "AND fetched_at < '2026-09-20T10:00:00Z'")
        conn.commit()
    conn = sqlite3.connect(db_path)
    now = DAY.replace(hour=12, minute=30)
    prof = panel_daily.inverter_day_profile(conn, DAY.date(), UTC, now=now)
    assert 8 not in prof["hours"] and 9 not in prof["hours"]          # inside the gap
    assert abs(prof["hours"][10] - 600 * 0.97) < 3
    assert abs(prof["cumulative"][10] - 600 * 2 * 0.97) < 10         # 08:00-10:00 still counted
    assert abs(prof["cumulative"][12] - 600 * 4 * 0.97) < 10
    assert prof["in_progress"]["hour"] == 12
    assert abs(prof["in_progress"]["wh"] - 600 * 0.5 * 0.97) < 10   # 12:00-12:30 only
