import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import auth, db
from src.client import RateLimited
from src.poller import _build_readings, _classify_failure

import requests


def _seed(db_path):
    db.init_db(db_path)
    db.upsert_optimizers(
        db_path,
        [
            {"serial": "1A2B3C4D-01", "label": "Optimizer 1.0.23 (REC 365)"},
            {"serial": "1A2B3C4D-02", "label": "Optimizer 1.0.24 (REC 365)"},
        ],
    )


def test_readings_record_status_per_cycle(tmp_path):
    db_path = tmp_path / "test.db"
    _seed(db_path)

    cycle_id = db.start_cycle(db_path)
    db.insert_readings(db_path, cycle_id, [
        {
            "optimizer_serial": "1A2B3C4D-01", "status": "FRESH",
            "ts_utc": "2026-09-23T10:00:00Z", "voltage": 32.1,
            "optimizer_voltage": 38.0, "current": 8.4, "power": 269.6,
            "energy_wh": 12.5, "raw_json": "{}",
        },
        {
            "optimizer_serial": "1A2B3C4D-02", "status": "MISSING",
            "ts_utc": None, "voltage": None, "optimizer_voltage": None,
            "current": None, "power": None, "energy_wh": None, "raw_json": None,
        },
    ])
    db.finish_cycle(db_path, cycle_id, "OK", optimizers_known=2, fresh=1, stale=0, missing=1)

    with db.connect(db_path) as conn:
        counts = dict(conn.execute(
            "SELECT status, COUNT(*) FROM readings GROUP BY status"
        ).fetchall())
        cycle = conn.execute("SELECT status, fresh_count, missing_count FROM poll_cycles").fetchone()

    assert counts == {"FRESH": 1, "MISSING": 1}
    assert cycle == ("OK", 1, 1)


def test_failed_cycle_records_no_readings(tmp_path):
    db_path = tmp_path / "test.db"
    _seed(db_path)

    cycle_id = db.start_cycle(db_path)
    db.finish_cycle(db_path, cycle_id, "NETWORK_ERROR", error_message="ConnectionError: boom")

    with db.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0] == 0
        row = conn.execute("SELECT status, error_message FROM poll_cycles").fetchone()

    assert row == ("NETWORK_ERROR", "ConnectionError: boom")


def test_last_measurement_timestamps_drive_fresh_vs_stale(tmp_path):
    db_path = tmp_path / "test.db"
    _seed(db_path)

    cycle_id = db.start_cycle(db_path)
    db.insert_readings(db_path, cycle_id, [{
        "optimizer_serial": "1A2B3C4D-01", "status": "FRESH",
        "ts_utc": "2026-09-23T10:00:00Z", "voltage": 32.1,
        "optimizer_voltage": 38.0, "current": 8.4, "power": 269.6,
        "energy_wh": 12.5, "raw_json": "{}",
    }])

    previous = db.last_measurement_timestamps(db_path)
    assert previous == {"1A2B3C4D-01": "2026-09-23T10:00:00Z"}

    data = {"serialToLiveData": {
        "1A2B3C4D-01": {"lastMeasurement": "2026-09-23T10:00:00Z", "power_W": 269.6},
        "1A2B3C4D-02": {"lastMeasurement": "2026-09-23T10:05:00Z", "power_W": 100.0},
    }}
    readings = _build_readings(["1A2B3C4D-01", "1A2B3C4D-02", "MISSING-01"], data, previous)
    statuses = {r["optimizer_serial"]: r["status"] for r in readings}

    assert statuses == {
        "1A2B3C4D-01": "STALE",     # same timestamp repeated
        "1A2B3C4D-02": "FRESH",     # never seen before
        "MISSING-01": "MISSING",    # absent from the response
    }


def test_network_failures_classified_separately():
    assert _classify_failure(requests.ConnectionError("down"), "fetch") == "NETWORK_ERROR"
    assert _classify_failure(requests.Timeout("slow"), "fetch") == "NETWORK_ERROR"
    assert _classify_failure(RuntimeError("SolarEdge login failed"), "fetch") == "AUTH_ERROR"
    assert _classify_failure(ValueError("weird json"), "discovery") == "DISCOVERY_ERROR"
    assert _classify_failure(ValueError("weird json"), "fetch") == "ERROR"
    # Throttling must be its own bucket, not lumped in with connectivity.
    assert _classify_failure(RateLimited("HTTP 429"), "fetch") == "RATE_LIMITED"


class _Resp:
    def __init__(self, status, body="", headers=None, ctype="application/json"):
        self.status_code = status
        self.text = body
        self.headers = {"content-type": ctype, **(headers or {})}


def test_throttling_is_detected_and_not_mistaken_for_a_lapsed_session():
    # A plain 429.
    assert auth.is_rate_limited(_Resp(429))
    # Cloudflare's HTML block page, which arrives as a 403 and would
    # otherwise look exactly like an expired session — and answering it by
    # re-authenticating would drive a browser login straight into the block.
    cf = _Resp(403, "<html>Attention Required! | Cloudflare</html>", ctype="text/html")
    assert auth.is_rate_limited(cf)

    # A genuine session lapse must still be treated as one.
    expired = _Resp(401, "")
    assert not auth.is_rate_limited(expired)
    assert auth.is_session_expired(expired)

    # And a normal JSON response is neither.
    ok = _Resp(200, '{"serialToLiveData":{}}')
    assert not auth.is_rate_limited(ok)
    assert not auth.is_session_expired(ok)


def test_raw_json_kept_for_fresh_but_not_duplicated_on_stale():
    # A STALE payload is identical to the FRESH one that introduced that
    # timestamp, so storing it again was 94% of all stored JSON for nothing.
    # The typed columns must still be populated on STALE rows.
    previous = {"A": "2026-09-23T10:00:00Z"}
    data = {"serialToLiveData": {
        "A": {"lastMeasurement": "2026-09-23T10:00:00Z", "power_W": 50.0,
              "voltage_V": 38.0, "current_A": 1.3},
        "B": {"lastMeasurement": "2026-09-23T10:05:00Z", "power_W": 60.0,
              "voltage_V": 37.0, "current_A": 1.6},
    }}
    rows = {r["optimizer_serial"]: r for r in _build_readings(["A", "B"], data, previous)}

    assert rows["A"]["status"] == "STALE"
    assert rows["A"]["raw_json"] is None
    assert rows["A"]["power"] == 50.0        # values still recorded
    assert rows["A"]["voltage"] == 38.0

    assert rows["B"]["status"] == "FRESH"
    assert json.loads(rows["B"]["raw_json"])["power_W"] == 60.0


def test_session_is_only_considered_valid_with_the_auth_cookie(tmp_path):
    # Regression: login used to be declared successful as soon as the browser
    # left the login page — but the portal only exchanges the OAuth code for
    # se_monitoring_auth a few seconds later, on /mfe/auth/callback. Cookies
    # captured before that produce a session that looks fine and 401s on
    # every call, which would silently burn a day of collection.
    pre_exchange = [
        {"name": "JSESSIONID", "value": "x", "domain": "monitoring.solaredge.com"},
        {"name": "CSRF-TOKEN", "value": "y", "domain": "monitoring.solaredge.com"},
        {"name": "cf_clearance", "value": "z", "domain": ".solaredge.com"},
    ]
    assert not auth.has_session_cookie(pre_exchange)

    post_exchange = pre_exchange + [
        {"name": "se_monitoring_auth", "value": "real", "domain": "monitoring.solaredge.com"},
    ]
    assert auth.has_session_cookie(post_exchange)

    # A saved file without it must force a fresh login rather than be loaded.
    path = tmp_path / "cookies.json"
    path.write_text(json.dumps({"user_agent": "UA", "cookies": pre_exchange}))
    assert auth.load_session(path) is None

    path.write_text(json.dumps({"user_agent": "UA", "cookies": post_exchange}))
    assert auth.load_session(path) is not None


def test_cycle_records_request_count_and_rate_limit_status(tmp_path):
    db_path = tmp_path / "test.db"
    _seed(db_path)

    cycle_id = db.start_cycle(db_path)
    db.finish_cycle(db_path, cycle_id, "RATE_LIMITED",
                    error_message="RateLimited: HTTP 429", requests_made=1)

    with db.connect(db_path) as conn:
        row = conn.execute(
            "SELECT status, requests_made FROM poll_cycles"
        ).fetchone()
        assert conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0] == 0

    assert row == ("RATE_LIMITED", 1)


def test_modbus_sampler_rows_are_cycle_independent(tmp_path):
    """The Modbus thread writes on its own cadence (cycle_id NULL) and its
    failures land in their own table, never as inverter data."""
    db_path = tmp_path / "test.db"
    db.init_db(db_path)
    db.insert_inverter_reading(db_path, None, {"dc_power": 1234.5, "status": 4})
    db.insert_modbus_failure(db_path, "failed after 2 attempts: connect failed")

    import sqlite3
    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT cycle_id, dc_power FROM inverter_readings").fetchall() == [(None, 1234.5)]
    assert conn.execute("SELECT reason FROM modbus_failures").fetchall() == [
        ("failed after 2 attempts: connect failed",)
    ]


def test_read_inverter_raises_with_reason_when_unreachable():
    from src import modbus_client
    import pytest
    with pytest.raises(modbus_client.ModbusUnavailable, match="connect failed"):
        # TEST-NET-1 address: guaranteed unroutable
        modbus_client.read_inverter("192.0.2.1", 1502, timeout=1, attempts=1)


def test_layout_optimizer_model_and_status_are_kept(tmp_path):
    from src.discover import extract_optimizers
    layout = {"children": [
        {"type": "OPTIMIZER", "serial": "AAAA-01", "name": "Optimizer 1.0.1", "displayOrder": "1.0.1",
         "properties": {"panelModelName": "ACME 400", "model": "P400-X", "status": "INACTIVE"}},
        # Same fields at the node's top level rather than in "properties".
        {"type": "OPTIMIZER", "serial": "AAAA-02", "name": "Optimizer 1.0.2", "displayOrder": "1.0.2",
         "model": "P370-Y", "status": "ACTIVE", "properties": {"panelModelName": "ACME"}},
    ]}
    found = {o.serial: o for o in extract_optimizers(layout)}
    assert (found["AAAA-01"].optimizer_model, found["AAAA-01"].layout_status) == ("P400-X", "INACTIVE")
    assert (found["AAAA-02"].optimizer_model, found["AAAA-02"].layout_status) == ("P370-Y", "ACTIVE")

    db_path = tmp_path / "t.db"
    db.init_db(db_path)
    db.upsert_optimizers(db_path, [{"serial": "AAAA-02", "label": "x", "optimizer_model": "P370-Y",
                                    "layout_status": "ACTIVE"}])
    # A later layout without the fields must not wipe what was learned.
    db.upsert_optimizers(db_path, [{"serial": "AAAA-02", "label": "x"}])
    with db.connect(db_path) as conn:
        row = conn.execute("SELECT optimizer_model, layout_status FROM optimizers").fetchone()
    assert tuple(row) == ("P370-Y", "ACTIVE")
