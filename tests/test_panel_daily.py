import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import daily, db, panel_daily  # noqa: E402

UTC = "UTC"
DAY = datetime(2026, 9, 20, tzinfo=timezone.utc)


def _ts(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _seed(db_path, panels: dict[str, float], hours=(8, 16), step_min=5,
          gap=None, inverter=True, inverter_from=None):
    """panels: {serial: watts} held steady from hours[0] to hours[1].
    gap: (start_hour, end_hour) with no per-panel data (an upload stall)."""
    db.init_db(db_path)
    with db.connect(db_path) as conn:
        cid = conn.execute("INSERT INTO poll_cycles (started_at, status) VALUES (?, 'OK')", (_ts(DAY),)).lastrowid
        for s in panels:
            conn.execute("INSERT INTO optimizers (serial, label, panel_model, first_seen, last_seen) "
                         "VALUES (?, ?, 'M', 'x', 'x')", (s, f"Optimizer {s}"))
        t = DAY.replace(hour=hours[0])
        while t < DAY.replace(hour=hours[1]):
            if not (gap and gap[0] <= t.hour < gap[1]):
                for s, w in panels.items():
                    conn.execute(
                        "INSERT INTO readings (cycle_id, optimizer_serial, status, fetched_at, ts_utc, "
                        "voltage, current, power) VALUES (?, ?, 'FRESH', ?, ?, 38, ?, ?)",
                        (cid, s, _ts(t), _ts(t), w / 38, w))
            t += timedelta(minutes=step_min)
        if inverter:
            lifetime = 1_000_000
            t = DAY.replace(hour=5)
            start = DAY.replace(hour=inverter_from) if inverter_from else None
            while t < DAY.replace(hour=20):
                dc = sum(panels.values()) if hours[0] <= t.hour < hours[1] else 0
                lifetime += round(dc * 30 / 3600 * 0.97)
                if not start or t >= start:
                    conn.execute(
                        "INSERT INTO inverter_readings (fetched_at, dc_power, ac_power, ac_voltage, "
                        "temperature, status, lifetime_wh) VALUES (?, ?, ?, 240, 40, 4, ?)",
                        (_ts(t), dc, dc * 0.97, lifetime))
                t += timedelta(seconds=30)
        conn.commit()


def _rows(db_path):
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    return {r["serial"]: dict(r) for r in c.execute("SELECT * FROM panel_daily")}


def _refresh(db_path):
    daily.refresh(db_path, UTC, today=date(2026, 9, 25))
    return panel_daily.refresh(db_path, UTC, today=date(2026, 9, 25))


def test_measured_energy_integrates_readings(tmp_path):
    db_path = tmp_path / "t.db"
    _seed(db_path, {"A": 300.0, "B": 300.0, "C": 150.0})
    assert _refresh(db_path) == ["2026-09-20"]
    r = _rows(db_path)
    # 8 hours at 300 W, minus the last 5-minute step that has no successor.
    assert 2350 < r["A"]["measured_wh"] <= 2400
    assert abs(r["C"]["measured_wh"] - r["A"]["measured_wh"] / 2) < 1
    assert r["A"]["coverage_pct"] == 100.0
    assert r["C"]["vs_peers_pct"] == 50.0
    assert r["A"]["finalized"] == 1


def test_estimates_add_up_to_the_inverter(tmp_path):
    db_path = tmp_path / "t.db"
    _seed(db_path, {"A": 300.0, "B": 200.0, "C": 100.0})
    _refresh(db_path)
    r = _rows(db_path)
    inv = sqlite3.connect(db_path).execute("SELECT energy_wh FROM inverter_daily").fetchone()[0]
    assert abs(sum(v["estimated_wh"] for v in r.values()) - inv) < 1
    assert abs(r["A"]["estimated_wh"] / r["C"]["estimated_wh"] - 3) < 0.01


def test_upload_stall_lowers_coverage_and_measured_but_not_the_estimate(tmp_path):
    db_path = tmp_path / "t.db"
    # Per-panel data missing 12:00-16:00, but the inverter saw it all.
    _seed(db_path, {"A": 300.0, "B": 300.0, "C": 300.0}, gap=(12, 16))
    _refresh(db_path)
    r = _rows(db_path)["A"]
    assert 45 < r["coverage_pct"] < 55
    assert r["measured_wh"] < 1300                # only the morning arrived
    assert 2200 < r["estimated_wh"] < 2400        # a third of the inverter's full day


def test_no_estimate_when_the_inverter_day_is_partial(tmp_path):
    db_path = tmp_path / "t.db"
    # Modbus came online at 13:00, mid-production.
    _seed(db_path, {"A": 300.0, "B": 300.0, "C": 300.0}, inverter_from=13)
    _refresh(db_path)
    r = _rows(db_path)["A"]
    assert r["estimated_wh"] is None
    # Coverage counts the morning from the optimizers' own data, not only the
    # hours the inverter happened to be read.
    assert r["coverage_pct"] == 100.0


def test_retention_prunes_raw_readings_but_keeps_daily_rows(tmp_path):
    db_path = tmp_path / "t.db"
    _seed(db_path, {"A": 300.0, "B": 300.0, "C": 300.0})
    _refresh(db_path)
    db.prune_optimizer_raw(db_path, keep_days=1)      # the synthetic day is older than that
    c = sqlite3.connect(db_path)
    assert c.execute("SELECT COUNT(*) FROM readings").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM panel_daily").fetchone()[0] == 3
    assert _refresh(db_path) == []                    # finalized days aren't recomputed
