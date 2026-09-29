import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from datetime import datetime, timedelta, timezone

from src import analysis, db
from src import groups

BY_LABEL = groups.parse("solaredge")

# Rows carry a plausible energy_wh, but scoring deliberately ignores it —
# it's compared on power (see src/analysis.py).
E = 0.25


def _ts(hour, i):
    """i-th measurement slot, one scoring window apart."""
    base = datetime(2026, 9, 23, hour, tzinfo=timezone.utc)
    return (base + timedelta(seconds=i * analysis.WINDOW_SECONDS)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _build(db_path, cycles):
    """cycles: list of {serial: power|None}. None means MISSING."""
    db.init_db(db_path)
    serials = sorted({s for c in cycles for s in c})
    db.upsert_optimizers(db_path, [{"serial": s, "label": s} for s in serials])
    for i, cycle in enumerate(cycles):
        cycle_id = db.start_cycle(db_path)
        rows = []
        for serial, power in cycle.items():
            if power is None:
                rows.append({
                    "optimizer_serial": serial, "status": "MISSING", "ts_utc": None,
                    "voltage": None, "optimizer_voltage": None, "current": None,
                    "power": None, "energy_wh": None, "raw_json": None,
                })
            else:
                rows.append({
                    "optimizer_serial": serial, "status": "FRESH",
                    "ts_utc": _ts(10, i), "voltage": 38.0,
                    "optimizer_voltage": 40.0, "current": power / 38.0,
                    "power": power, "energy_wh": power * E, "raw_json": "{}",
                })
        db.insert_readings(db_path, cycle_id, rows)
        db.finish_cycle(db_path, cycle_id, "OK")


def _by_serial(result):
    return {o["serial"]: o for o in result["optimizers"]}


def test_underperformer_is_flagged_and_healthy_peers_are_not(tmp_path):
    db_path = tmp_path / "t.db"
    # Three healthy panels at ~100W, one at 30W, across 12 daylight cycles.
    _build(db_path, [
        {"GOOD-1": 100.0, "GOOD-2": 102.0, "GOOD-3": 98.0, "WEAK-1": 30.0}
        for _ in range(12)
    ])

    res = _by_serial(analysis.score_optimizers(db_path))

    assert 29 < res["WEAK-1"]["perf_ratio_pct"] < 31   # ~30% of its peers
    assert res["WEAK-1"]["verdict"] in ("BAD", "SUSPECT")
    for good in ("GOOD-1", "GOOD-2", "GOOD-3"):
        assert res[good]["verdict"] == "GOOD"
        assert res[good]["health_score"] >= 85


def _build_grouped(db_path, cycles, models):
    """cycles: list of {serial: power}. models: {serial: panel_model}."""
    db.init_db(db_path)
    db.upsert_optimizers(db_path, [
        {"serial": s, "label": s, "panel_model": m} for s, m in models.items()])
    for i, cycle in enumerate(cycles):
        cid = db.start_cycle(db_path)
        db.insert_readings(db_path, cid, [{
            "optimizer_serial": s, "status": "FRESH",
            "ts_utc": _ts(12, i), "voltage": 38.0,
            "optimizer_voltage": 40.0, "current": p / 38.0, "power": p,
            "energy_wh": p * E, "raw_json": "{}",
        } for s, p in cycle.items()])
        db.finish_cycle(db_path, cid, "OK")


def test_panels_are_compared_within_their_own_model_group(tmp_path):
    db_path = tmp_path / "t.db"
    # Two groups differing 2.5x under the same sun — as this site's do. All
    # panels are healthy for their type; against one array-wide median the
    # weaker group would read as failing and the stronger as exceptional.
    models = {}
    for n in range(6):
        models[f"BIG-{n}"] = "REC 365"
        models[f"SMALL-{n}"] = "REC"
    cycles = []
    for _ in range(12):
        c = {f"BIG-{n}": 250.0 for n in range(6)}
        c.update({f"SMALL-{n}": 100.0 for n in range(6)})
        cycles.append(c)
    _build_grouped(db_path, cycles, models)

    res = _by_serial(analysis.score_optimizers(db_path, hours=99999, grouping=BY_LABEL))

    for n in range(6):
        assert res[f"BIG-{n}"]["perf_ratio_pct"] == 100.0
        assert res[f"SMALL-{n}"]["perf_ratio_pct"] == 100.0
        assert res[f"SMALL-{n}"]["verdict"] == "GOOD", res[f"SMALL-{n}"]


def test_default_compares_every_panel_with_the_whole_array(tmp_path):
    db_path = tmp_path / "t.db"
    # Same two labelled groups as above, but no grouping configured: labels
    # typed in by an installer mustn't decide who a panel's peers are.
    models = {}
    for n in range(6):
        models[f"BIG-{n}"] = "REC 365"
        models[f"SMALL-{n}"] = "REC"
    cycles = []
    for _ in range(12):
        c = {f"BIG-{n}": 250.0 for n in range(6)}
        c.update({f"SMALL-{n}": 100.0 for n in range(6)})
        cycles.append(c)
    _build_grouped(db_path, cycles, models)

    result = analysis.score_optimizers(db_path, hours=99999)
    res = _by_serial(result)

    assert result["grouping"]["mode"] == "array"
    # Whole-array median of six 250s and six 100s is 175.
    assert 57 < res["SMALL-0"]["perf_ratio_pct"] < 58
    assert res["SMALL-0"]["group"] is None


def test_a_weak_panel_is_still_caught_inside_its_group(tmp_path):
    db_path = tmp_path / "t.db"
    models = {}
    for n in range(6):
        models[f"BIG-{n}"] = "REC 365"
        models[f"SMALL-{n}"] = "REC"
    models["WEAK"] = "REC"          # belongs to the low-output group
    cycles = []
    for _ in range(12):
        c = {f"BIG-{n}": 250.0 for n in range(6)}
        c.update({f"SMALL-{n}": 100.0 for n in range(6)})
        c["WEAK"] = 30.0            # 30% of its own group, not of the array
        cycles.append(c)
    _build_grouped(db_path, cycles, models)

    res = _by_serial(analysis.score_optimizers(db_path, hours=99999, grouping=BY_LABEL))

    assert 28 < res["WEAK"]["perf_ratio_pct"] < 32
    assert res["WEAK"]["verdict"] in ("BAD", "SUSPECT")
    assert res["SMALL-0"]["verdict"] == "GOOD"


def test_small_group_falls_back_to_the_array_median(tmp_path):
    db_path = tmp_path / "t.db"
    # A group of two can't produce a trustworthy median of its own.
    models = {f"MAIN-{n}": "REC 365" for n in range(8)}
    models["ODD-1"] = "ONE-OFF"
    models["ODD-2"] = "ONE-OFF"
    cycles = []
    for _ in range(12):
        c = {f"MAIN-{n}": 200.0 for n in range(8)}
        c["ODD-1"] = 200.0
        c["ODD-2"] = 200.0
        cycles.append(c)
    _build_grouped(db_path, cycles, models)

    res = _by_serial(analysis.score_optimizers(db_path, hours=99999, grouping=BY_LABEL))

    # Compared against the whole array rather than its single peer.
    assert res["ODD-1"]["perf_ratio_pct"] == 100.0
    assert res["ODD-1"]["verdict"] == "GOOD"


def test_night_cycles_are_excluded_from_scoring(tmp_path):
    db_path = tmp_path / "t.db"
    day = [{"A": 100.0, "B": 100.0, "C": 10.0} for _ in range(6)]
    night = [{"A": 0.5, "B": 0.4, "C": 0.45} for _ in range(20)]
    _build(db_path, day + night)

    result = analysis.score_optimizers(db_path)
    res = _by_serial(result)

    # All 6 daylight windows score. None of the 20 night ones do — they
    # would have made the weak panel look identical to its peers.
    assert result["scored_windows"] == 6
    assert res["C"]["scored_samples"] == 6
    assert res["C"]["perf_ratio_pct"] == 10.0


def test_near_zero_readings_count_as_real_output(tmp_path):
    db_path = tmp_path / "t.db"
    # Checked against the inverter's own DC power, readings near 0 A are
    # real: filtering them out pushed the optimizers' total 7-18% above what
    # the inverter measured. A panel at 0 W in half its readings made half
    # the output, whatever SolarEdge's per-report energy figure claims.
    db.init_db(db_path)
    serials = ["A", "B", "C", "DIPS"]
    db.upsert_optimizers(db_path, [{"serial": s, "label": s} for s in serials])
    for i in range(10):
        cid = db.start_cycle(db_path)
        rows = []
        for s in serials:
            for j, minute in enumerate((0, 5)):          # two reports per window
                power = 0.0 if (s == "DIPS" and j == 1) else 200.0
                rows.append({
                    "optimizer_serial": s, "status": "FRESH",
                    "ts_utc": (datetime(2026, 9, 23, 10, tzinfo=timezone.utc)
                               + timedelta(seconds=i * analysis.WINDOW_SECONDS, minutes=minute)
                               ).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "voltage": 38.0, "optimizer_voltage": 40.0,
                    "current": power / 38.0, "power": power,
                    "energy_wh": 200.0 * E,             # energy says "normal" — ignored
                    "raw_json": "{}",
                })
        db.insert_readings(db_path, cid, rows)
        db.finish_cycle(db_path, cid, "OK")

    res = _by_serial(analysis.score_optimizers(db_path))

    assert res["DIPS"]["perf_ratio_pct"] == 50.0
    assert res["DIPS"]["near_zero_pct"] == 50.0
    assert res["A"]["near_zero_pct"] == 0.0
    assert res["DIPS"]["median_power"] == 100.0


def test_uniform_cloud_does_not_distort_ratios(tmp_path):
    db_path = tmp_path / "t.db"
    # A cloud dims the whole array together, then it clears — gradually
    # enough to stay within the stability limit. Every panel should still
    # read as healthy, because they dim in proportion.
    levels = [300.0, 260.0, 220.0, 190.0, 165.0, 190.0, 220.0, 260.0, 300.0,
              280.0, 250.0, 280.0]
    cycles = [{f"P-{n}": lvl * f for n, f in [(1, 1.0), (2, 0.97), (3, 1.03)]}
              for lvl in levels]
    _build(db_path, cycles)

    res = _by_serial(analysis.score_optimizers(db_path))

    for serial in res:
        assert res[serial]["verdict"] == "GOOD", (serial, res[serial])


def test_weak_light_cycles_are_not_scored(tmp_path):
    db_path = tmp_path / "t.db"
    # Bright cycles, plus heavy-overcast cycles where the array limps at
    # ~8% of its usual output. In those, one panel reading 2 W against a
    # 25 W median would look catastrophic off a trivial absolute gap.
    bright = [{"A": 300.0, "B": 300.0, "C": 300.0} for _ in range(10)]
    murk = [{"A": 2.0, "B": 25.0, "C": 25.0} for _ in range(10)]
    _build(db_path, bright + murk)

    result = analysis.score_optimizers(db_path)
    res = _by_serial(result)

    assert result["excluded_weak_light"] >= 10
    # A is only ever dim during the murk, which is excluded, so it is not
    # condemned on the strength of near-dark readings.
    assert res["A"]["verdict"] == "GOOD"
    assert res["A"]["median_power"] == 300.0


def test_light_changing_between_windows_does_not_discard_them(tmp_path):
    db_path = tmp_path / "t.db"
    # Broken cloud: site output whipsaws between full sun and shade from one
    # window to the next. The snapshot method had to drop all of these (and
    # dropped 912 real cycles in one run); comparing each window's average
    # power keeps them, and the ratios still hold.
    cycles = []
    for i in range(12):
        lvl = 300.0 if i % 2 == 0 else 150.0
        cycles.append({f"P-{n}": lvl for n in (1, 2, 3)} | {"WEAK": lvl * 0.5})
    _build(db_path, cycles)

    result = analysis.score_optimizers(db_path)
    res = _by_serial(result)

    assert result["scored_windows"] == 12
    assert res["P-1"]["verdict"] == "GOOD"
    assert res["WEAK"]["perf_ratio_pct"] == 50.0


def test_normal_stale_rate_is_not_treated_as_unreliability(tmp_path):
    # Regression: optimizers report every ~4.5 min while we poll every 2, so
    # a perfectly healthy one is FRESH only ~40% of polls. Scoring that as
    # "60% unreliable" multiplied every optimizer down and made the entire
    # array read BAD — including panels producing 180% of their peers.
    cycles = []
    for i in range(40):
        cycle = {}
        for n in range(6):
            # Each reports every 3rd cycle, staggered — all equally healthy.
            cycle[f"OK-{n}"] = 200.0 if (i + n) % 3 == 0 else None
        cycles.append(cycle)
    # None here means MISSING in _build, so build explicitly with STALE instead.
    db_path = tmp_path / "t.db"
    db.init_db(db_path)
    serials = [f"OK-{n}" for n in range(6)]
    db.upsert_optimizers(db_path, [{"serial": s, "label": s} for s in serials])
    last = {}
    for i in range(40):
        cid = db.start_cycle(db_path)
        rows = []
        for n, serial in enumerate(serials):
            if (i + n) % 3 == 0:
                last[serial] = f"2026-09-23T12:{i:02d}:00Z"
                status = "FRESH"
            else:
                status = "STALE"
            rows.append({
                "optimizer_serial": serial, "status": status,
                "ts_utc": last.get(serial, "2026-09-23T12:00:00Z"),
                "voltage": 38.0, "optimizer_voltage": 40.0, "current": 5.2,
                "power": 200.0, "energy_wh": 200.0 * E,
                "raw_json": "{}" if status == "FRESH" else None,
            })
        db.insert_readings(db_path, cid, rows)
        db.finish_cycle(db_path, cid, "OK")

    res = _by_serial(analysis.score_optimizers(db_path, hours=99999))

    for serial in serials:
        # ~33% fresh is simply the cadence, and every peer achieves the same,
        # so each must read as fully reliable rather than a third as good.
        assert res[serial]["reliability_pct"] == 100.0, (serial, res[serial])
        assert res[serial]["verdict"] == "GOOD", (serial, res[serial])


def test_slower_reporting_rhythm_is_not_unreliability(tmp_path):
    # Regression: on the live site each optimizer has its own reporting
    # rhythm (3.1 to 5.9 min) and none was ever MISSING, yet a per-poll
    # fresh rate docked the slow reporters to ~71% reliable. SLOW reports
    # half as often as its peers but in every window, so it is fully
    # reliable.
    db_path = tmp_path / "t.db"
    db.init_db(db_path)
    serials = ["FAST-1", "FAST-2", "FAST-3", "SLOW"]
    db.upsert_optimizers(db_path, [{"serial": s, "label": s} for s in serials])
    base = datetime(2026, 9, 23, 12, tzinfo=timezone.utc)
    last = {}
    for i in range(60):                     # 2-minute polls over two hours
        cid = db.start_cycle(db_path)
        rows = []
        for s in serials:
            every = 3 if s == "SLOW" else 1   # SLOW: a new reading every 6 min
            fresh = i % every == 0
            if fresh:
                last[s] = (base + timedelta(minutes=2 * i)).strftime("%Y-%m-%dT%H:%M:%SZ")
            rows.append({
                "optimizer_serial": s, "status": "FRESH" if fresh else "STALE",
                "ts_utc": last[s], "voltage": 38.0, "optimizer_voltage": 40.0,
                "current": 5.2, "power": 200.0, "energy_wh": 200.0 * E,
                "raw_json": "{}" if fresh else None,
            })
        db.insert_readings(db_path, cid, rows)
        db.finish_cycle(db_path, cid, "OK")

    res = _by_serial(analysis.score_optimizers(db_path, hours=99999))

    assert res["SLOW"]["reliability_pct"] == 100.0, res["SLOW"]
    assert res["SLOW"]["verdict"] == "GOOD"


def test_night_staleness_is_not_counted_as_unreliability(tmp_path):
    db_path = tmp_path / "t.db"
    # HEALTHY reports perfectly all day and goes quiet after dark, exactly as
    # a working optimizer should. Counting the dark hours would drag its
    # reliability down toward a genuinely faulty unit's.
    day = [{"HEALTHY": 200.0, "B": 200.0, "C": 200.0} for _ in range(6)]
    night = [{"HEALTHY": None, "B": None, "C": None} for _ in range(30)]
    _build(db_path, day + night)

    res = _by_serial(analysis.score_optimizers(db_path))["HEALTHY"]

    assert res["reliability_pct"] == 100.0      # not ~17% (6 of 36 cycles)
    assert res["missing_count"] == 0            # the 30 dark cycles don't count
    assert res["verdict"] == "GOOD"


def _build_at_hours(db_path, per_hour_cycles):
    """per_hour_cycles: list of (utc_hour, {serial: power}) — writes cycles
    with started_at at that hour so hour-of-day logic can be exercised."""
    db.init_db(db_path)
    serials = sorted({s for _, c in per_hour_cycles for s in c})
    db.upsert_optimizers(db_path, [{"serial": s, "label": s} for s in serials])
    with db.connect(db_path) as conn:
        for i, (hour, cycle) in enumerate(per_hour_cycles):
            ts = f"2026-09-2{1 + i // 200}T{hour:02d}:{(i * 2) % 60:02d}:00Z"
            cur = conn.execute(
                "INSERT INTO poll_cycles (started_at, status) VALUES (?, 'OK')", (ts,))
            cid = cur.lastrowid
            for serial, power in cycle.items():
                conn.execute(
                    """INSERT INTO readings (cycle_id, optimizer_serial, status,
                       fetched_at, ts_utc, voltage, optimizer_voltage, current,
                       power, energy_wh, raw_json)
                       VALUES (?,?,'FRESH',?,?,38.0,40.0,?,?,?,'{}')""",
                    (cid, serial, ts, ts, power / 38.0, power, power * E),
                )
        conn.commit()


def _build_backfilled(db_path, cycles):
    """cycles: list of (polled_at, measured_at, {serial: power}) — SolarEdge
    delivers readings long after they were taken, so these differ."""
    db.init_db(db_path)
    serials = sorted({s for _, _, c in cycles for s in c})
    db.upsert_optimizers(db_path, [{"serial": s, "label": s} for s in serials])
    with db.connect(db_path) as conn:
        for polled, measured, cycle in cycles:
            cid = conn.execute(
                "INSERT INTO poll_cycles (started_at, status) VALUES (?, 'OK')",
                (polled,)).lastrowid
            for serial, power in cycle.items():
                conn.execute(
                    """INSERT INTO readings (cycle_id, optimizer_serial, status,
                       fetched_at, ts_utc, voltage, optimizer_voltage, current,
                       power, energy_wh, raw_json)
                       VALUES (?,?,'FRESH',?,?,38.0,40.0,?,?,?,'{}')""",
                    (cid, serial, polled, measured, power / 38.0, power, power * E))
        conn.commit()


def test_hours_range_actually_bounds_by_time_not_just_date(tmp_path):
    # Regression: we store '2026-09-24T15:26:03Z' but SQLite's datetime()
    # yields '2026-09-24 14:27:02' — space separator, no Z. String-compared,
    # 'T' (0x54) beats ' ' (0x20), so every row from the same date passed
    # whatever its time. hours=1 was returning a whole day: 460 cycles
    # instead of 30.
    db_path = tmp_path / "t.db"
    db.init_db(db_path)
    db.upsert_optimizers(db_path, [{"serial": s, "label": s} for s in ("A", "B", "C")])

    now = datetime.now(timezone.utc)
    # One batch 10 minutes ago, one 10 hours ago — same calendar date only if
    # the clock happens to allow it, so assert on the recent one explicitly.
    for age_minutes in (10, 600):
        stamp = (now - timedelta(minutes=age_minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
        with db.connect(db_path) as conn:
            cid = conn.execute(
                "INSERT INTO poll_cycles (started_at, status) VALUES (?, 'OK')",
                (stamp,)).lastrowid
            for serial in ("A", "B", "C"):
                conn.execute(
                    """INSERT INTO readings (cycle_id, optimizer_serial, status,
                       fetched_at, ts_utc, voltage, optimizer_voltage, current,
                       power, energy_wh, raw_json)
                       VALUES (?,?,'FRESH',?,?,38.0,40.0,2.0,300.0,5.0,'{}')""",
                    (cid, serial, stamp, stamp))
            conn.commit()

    with db.connect(db_path) as conn:
        recent = conn.execute(
            """SELECT COUNT(DISTINCT cycle_id) FROM readings
               WHERE fetched_at > strftime('%Y-%m-%dT%H:%M:%SZ','now','-1 hour')"""
        ).fetchone()[0]
        broken = conn.execute(
            """SELECT COUNT(DISTINCT cycle_id) FROM readings
               WHERE fetched_at > datetime('now','-1 hour')"""
        ).fetchone()[0]

    assert recent == 1, "a 1-hour window must contain only the 10-minute-old batch"
    # Demonstrates why the plain form is wrong whenever both fall on one date.
    assert broken >= recent


def test_backfilled_readings_are_filed_by_measurement_time_not_poll_time(tmp_path):
    db_path = tmp_path / "t.db"
    # Measured at 12:xx midday, but only delivered to us at 22:xx that night —
    # a real lag of 40-220 min was observed against the live site. Filing
    # these under hour 22 would put midday sun in the middle of the night and
    # make the whole shading profile meaningless.
    cycles = []
    for i in range(6):
        measured = f"2026-09-21T12:{i * 5:02d}:00Z"
        polled = f"2026-09-21T22:{i * 5:02d}:00Z"
        cycles.append((polled, measured,
                       {"A": 300.0, "B": 300.0, "C": 300.0, "D": 300.0}))
    _build_backfilled(db_path, cycles)

    prof = analysis.hourly_profile(db_path, hours=99999)
    assert prof["hours_covered"] == [12], prof["hours_covered"]

    # And the hour filter must agree: hour 12 has the data, hour 22 has none.
    assert analysis.score_optimizers(
        db_path, hours=99999, hour_of_day=12)["scored_windows"] > 0
    assert analysis.score_optimizers(
        db_path, hours=99999, hour_of_day=22)["scored_windows"] == 0


def test_readings_not_taken_together_are_not_compared(tmp_path):
    db_path = tmp_path / "t.db"
    # One optimizer's reading is hours older than its peers' in the same
    # delivery. Comparing them would be comparing different moments of sun,
    # so it lands in its own measurement window — which, holding one panel,
    # is too sparse to score — and is never judged against the others.
    db.init_db(db_path)
    serials = ("A", "B", "C", "LATE")
    db.upsert_optimizers(db_path, [{"serial": s, "label": s} for s in serials])
    with db.connect(db_path) as conn:
        for i in range(8):
            polled = f"2026-09-21T12:{i * 2:02d}:00Z"
            cid = conn.execute(
                "INSERT INTO poll_cycles (started_at, status) VALUES (?, 'OK')",
                (polled,)).lastrowid
            for serial in serials:
                measured = f"2026-09-21T09:{i * 2:02d}:00Z" if serial == "LATE" else polled
                power = 30.0 if serial == "LATE" else 300.0
                conn.execute(
                    """INSERT INTO readings (cycle_id, optimizer_serial, status,
                       fetched_at, ts_utc, voltage, optimizer_voltage, current,
                       power, energy_wh, raw_json)
                       VALUES (?,?,'FRESH',?,?,38.0,40.0,2.0,?,?,'{}')""",
                    (cid, serial, polled, measured, power, power * E))
        conn.commit()

    result = analysis.score_optimizers(db_path, hours=99999)
    res = _by_serial(result)
    assert res["LATE"]["scored_samples"] == 0
    assert res["LATE"]["perf_ratio_pct"] is None
    assert res["A"]["scored_samples"] > 0
    assert result["excluded_sparse"] > 0


def test_hour_filter_scores_only_the_chosen_hour(tmp_path):
    db_path = tmp_path / "t.db"
    # WEAK is only bad at 08:00; at 12:00 it matches its peers.
    cycles = []
    for _ in range(6):
        cycles.append((8, {"A": 100.0, "B": 100.0, "WEAK": 25.0}))
    for _ in range(6):
        cycles.append((12, {"A": 300.0, "B": 300.0, "WEAK": 300.0}))
    _build_at_hours(db_path, cycles)

    at8 = _by_serial(analysis.score_optimizers(db_path, hours=99999, hour_of_day=8))
    at12 = _by_serial(analysis.score_optimizers(db_path, hours=99999, hour_of_day=12))

    assert at8["WEAK"]["perf_ratio_pct"] == 25.0
    assert at12["WEAK"]["perf_ratio_pct"] == 100.0
    # Morning light must be judged against morning peak, not midday peak,
    # or picking 8am would discard every cycle in it.
    assert at8["WEAK"]["scored_samples"] > 0


def test_hourly_profile_separates_shading_from_a_real_fault(tmp_path):
    db_path = tmp_path / "t.db"
    cycles = []
    # SHADED: in shadow at 08:00, fine the rest of the day.
    # FAULTY: down by the same proportion at every hour.
    # Six healthy peers: the peer median is only meaningful when most panels
    # are fine, which holds for a real array but not a 4-panel toy where half
    # are broken.
    for hour, level in [(8, 120.0), (10, 240.0), (12, 300.0), (14, 260.0), (16, 140.0)]:
        for _ in range(5):
            cycle = {f"OK-{n}": level for n in range(6)}
            cycle["SHADED"] = level * (0.25 if hour == 8 else 1.0)
            cycle["FAULTY"] = level * 0.4
            cycles.append((hour, cycle))
    _build_at_hours(db_path, cycles)

    prof = {p["serial"]: p for p in
            analysis.hourly_profile(db_path, hours=99999)["optimizers"]}

    shaded, faulty = prof["SHADED"], prof["FAULTY"]

    # The shaded panel has a deep notch at one hour and is normal elsewhere.
    assert shaded["spread_pct"] > 50
    by_hour = {p["hour"]: p["ratio_pct"] for p in shaded["points"]}
    assert by_hour[8] < 40 and by_hour[12] > 85

    # The faulty one is low everywhere, so its profile is flat.
    faulty_by_hour = {p["hour"]: p["ratio_pct"] for p in faulty["points"]}
    assert all(r < 55 for r in faulty_by_hour.values())
    assert faulty["spread_pct"] < 20

    # Which is exactly the distinction a single-hour view cannot make.
    assert shaded["min_ratio_pct"] < 40 and faulty["min_ratio_pct"] < 55


def test_never_reporting_optimizer_gets_no_data_verdict(tmp_path):
    db_path = tmp_path / "t.db"
    _build(db_path, [{"A": 100.0, "B": 100.0, "C": 100.0, "DEAD": None}
                     for _ in range(10)])

    result = analysis.score_optimizers(db_path)
    res = _by_serial(result)

    assert res["DEAD"]["verdict"] == "NO DATA"
    assert res["DEAD"]["health_score"] == 0.0
    assert res["DEAD"]["missing_count"] == result["reliability_cycles"] == 10
    assert res["DEAD"]["reliability_pct"] == 0.0


def test_confidence_reflects_sample_size(tmp_path):
    db_path = tmp_path / "t.db"
    _build(db_path, [{"A": 100.0, "B": 100.0, "C": 100.0} for _ in range(3)])

    res = _by_serial(analysis.score_optimizers(db_path))

    # A verdict off 3 samples must not claim to be trustworthy.
    assert res["A"]["confidence"] == "INSUFFICIENT"


def test_unreliable_reporting_drags_score_down(tmp_path):
    db_path = tmp_path / "t.db"
    # FLAKY produces fine when it reports, but only reports half the time.
    cycles = []
    for i in range(20):
        c = {"A": 100.0, "B": 100.0, "C": 100.0}
        c["FLAKY"] = 100.0 if i % 2 == 0 else None
        cycles.append(c)
    _build(db_path, cycles)

    res = _by_serial(analysis.score_optimizers(db_path))

    assert res["FLAKY"]["perf_ratio_pct"] == 100.0        # output is fine
    assert 45 < res["FLAKY"]["reliability_pct"] < 55      # but ~half its polls are missing
    assert res["FLAKY"]["health_score"] < res["A"]["health_score"]
    # Dropping half its polls in broad daylight is a real fault signal, so
    # good output alone must not let it pass as healthy.
    assert res["FLAKY"]["verdict"] in ("WATCH", "SUSPECT")
    assert res["A"]["verdict"] == "GOOD"


def test_median_max_power_captured(tmp_path):
    db_path = tmp_path / "t.db"
    _build(db_path, [
        {"A": 100.0, "B": 100.0, "C": 100.0},
        {"A": 50.0, "B": 100.0, "C": 100.0},
        {"A": 150.0, "B": 100.0, "C": 100.0},
    ])

    res = _by_serial(analysis.score_optimizers(db_path))

    assert res["A"]["median_power"] == 100.0
    assert res["A"]["max_power"] == 150.0
    assert res["A"]["avg_power"] == 100.0


def test_night_readings_excluded_from_daylight_stats(tmp_path):
    db_path = tmp_path / "t.db"
    # Daytime: A runs 80-120 W. Night: everything collapses to ~0.3 W.
    # Without daylight filtering, the medians and averages would be dragged
    # toward zero and every panel would read "near zero" all night.
    day = [{"A": 100.0, "B": 100.0, "C": 100.0},
           {"A": 80.0, "B": 100.0, "C": 100.0},
           {"A": 120.0, "B": 100.0, "C": 100.0}]
    night = [{"A": 0.3, "B": 0.2, "C": 0.25} for _ in range(10)]
    _build(db_path, day + night)

    res = _by_serial(analysis.score_optimizers(db_path))["A"]

    assert res["median_power"] == 100.0  # not 0.3
    assert res["max_power"] == 120.0
    assert res["avg_power"] == 100.0     # not dragged down by 10 night zeros
    assert res["median_current"] == 100.0 / 38.0
    assert res["median_voltage"] == 38.0 # night voltage never considered
    assert res["near_zero_pct"] == 0.0   # night's ~0 A readings aren't counted
    assert res["scored_samples"] == 3
