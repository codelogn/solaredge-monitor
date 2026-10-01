import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import analysis, db  # noqa: E402

DAY = datetime(2026, 9, 23, tzinfo=timezone.utc)


def _build(db_path, panels, days=3, hours=range(8, 18)):
    """panels: {serial: f(hour) -> (volts, amps)}; one reading per panel per
    15-minute window, every window of every producing hour."""
    db.init_db(db_path)
    db.upsert_optimizers(db_path, [{"serial": s, "label": f"Optimizer {s}"} for s in panels])
    cycle_id = db.start_cycle(db_path)
    rows = []
    for d in range(days):
        for h in hours:
            for q in range(4):
                t = (DAY + timedelta(days=d, hours=h, minutes=15 * q + 5)).strftime("%Y-%m-%dT%H:%M:%SZ")
                for serial, f in panels.items():
                    v, a = f(h)
                    rows.append({"optimizer_serial": serial, "status": "FRESH", "ts_utc": t,
                                 "voltage": v, "optimizer_voltage": 40.0, "current": a,
                                 "power": v * a, "energy_wh": None, "raw_json": None})
    db.insert_readings(db_path, cycle_id, rows)
    db.finish_cycle(db_path, cycle_id, "OK")


def _flags(db_path):
    out = analysis.wiring_check(db_path, hours=24 * 3650)
    return {o["serial"]: o for o in out["optimizers"]}


def test_steady_voltage_low_current_all_day_is_a_wiring_check(tmp_path):
    db_path = tmp_path / "t.db"
    healthy = {f"OK-{n}": (lambda h: (38.0, 5.0)) for n in range(5)}
    _build(db_path, {**healthy,
                     "DIM": lambda h: (38.0, 3.75),                              # 75% all day
                     "SHADED": lambda h: (38.0, 2.0 if h < 11 else 5.0),         # mornings only
                     "DIODE": lambda h: (25.0, 5.0)})                            # a third of V gone
    f = _flags(db_path)
    assert f["DIM"]["flag"] == "CHECK WIRING"
    assert f["DIM"]["voltage_ratio_pct"] == 100.0 and f["DIM"]["current_ratio_pct"] == 75.0
    assert f["SHADED"]["flag"] == "SHADE PATTERN" and f["SHADED"]["low_hours"] == [8, 9, 10]
    assert f["DIODE"]["flag"] == "LOW VOLTAGE"
    assert all(f[s]["flag"] == "OK" for s in healthy)
    # Ranked with the problems first.
    ranked = sorted(f.values(), key=lambda r: r["rank"])
    assert [r["serial"] for r in ranked[:3]] == ["DIODE", "DIM", "SHADED"]


def test_scattered_low_hours_are_not_called_shade(tmp_path):
    db_path = tmp_path / "t.db"
    healthy = {f"OK-{n}": (lambda h: (38.0, 5.0)) for n in range(5)}
    # Low in three separate stretches through the day — no single shadow does that.
    _build(db_path, {**healthy, "ODD": lambda h: (38.0, 3.0 if h in (8, 9, 12, 13, 16, 17) else 5.0)})
    assert _flags(db_path)["ODD"]["flag"] == "WATCH"


def test_too_few_hours_gives_no_verdict(tmp_path):
    db_path = tmp_path / "t.db"
    healthy = {f"OK-{n}": (lambda h: (38.0, 5.0)) for n in range(5)}
    _build(db_path, {**healthy, "DIM": lambda h: (38.0, 3.0)}, hours=range(11, 14))
    assert _flags(db_path)["DIM"]["flag"] == "NOT ENOUGH DATA"
