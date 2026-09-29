from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

# Two deliberately separate records, so our own failures can never be
# mistaken for an optimizer being silent:
#
#   poll_cycles — one row per poll attempt, including the ones that failed
#                 (network/auth/etc). This is the only place our own
#                 failures are recorded.
#   readings    — one row per optimizer per *successful* cycle, carrying a
#                 status of FRESH (device reported a new measurement),
#                 STALE (poll succeeded but the device handed back the same
#                 timestamp again) or MISSING (device absent from the
#                 response entirely). Nothing is written here for a failed
#                 cycle, because during one we observed nothing.
#
# Range queries filter on fetched_at (always set), never ts_utc (NULL on
# MISSING rows).
SCHEMA = """
CREATE TABLE IF NOT EXISTS optimizers (
    serial       TEXT PRIMARY KEY,
    label        TEXT,
    -- Panels of different models produce different amounts under the same
    -- sun, so optimizers are compared within their own model group rather
    -- than against one array-wide median. Observed 2.5x between this site's
    -- two groups, which would otherwise read as ten failing panels.
    panel_model  TEXT,
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS poll_cycles (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at       TEXT NOT NULL,
    finished_at      TEXT,
    status           TEXT NOT NULL CHECK (status IN (
                        'RUNNING','OK','NETWORK_ERROR','AUTH_ERROR',
                        'DISCOVERY_ERROR','RATE_LIMITED','ERROR')),
    error_message    TEXT,
    optimizers_known INTEGER,
    fresh_count      INTEGER,
    stale_count      INTEGER,
    missing_count    INTEGER,
    duration_ms      INTEGER,
    requests_made    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_cycles_started ON poll_cycles(started_at);
CREATE INDEX IF NOT EXISTS idx_cycles_status ON poll_cycles(status);

CREATE TABLE IF NOT EXISTS readings (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id            INTEGER NOT NULL REFERENCES poll_cycles(id),
    optimizer_serial    TEXT NOT NULL REFERENCES optimizers(serial),
    status              TEXT NOT NULL CHECK (status IN ('FRESH','STALE','MISSING')),
    fetched_at          TEXT NOT NULL,
    ts_utc              TEXT,
    voltage             REAL,
    optimizer_voltage   REAL,
    current             REAL,
    power               REAL,
    energy_wh           REAL,
    raw_json            TEXT
);
CREATE INDEX IF NOT EXISTS idx_readings_serial_fetched ON readings(optimizer_serial, fetched_at);
CREATE INDEX IF NOT EXISTS idx_readings_serial_status ON readings(optimizer_serial, status);
-- Covers last_measurement_timestamps(), which runs on every single cycle.
CREATE INDEX IF NOT EXISTS idx_readings_serial_ts ON readings(optimizer_serial, ts_utc);

-- Inverter AC-side snapshot, read over Modbus TCP. The cloud API exposes
-- optimizer DC data only, so without this there is no way to see grid
-- voltage, inverter status or AC power — the three things needed to tell a
-- limited or derating inverter apart from an underproducing array.
-- Sampled every MODBUS_INTERVAL_SECONDS by its own thread, independent of
-- the cloud poll, so cycle_id is NULL for rows written since that split.
CREATE TABLE IF NOT EXISTS inverter_readings (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id       INTEGER REFERENCES poll_cycles(id),
    fetched_at     TEXT NOT NULL,
    ac_voltage     REAL,
    ac_current     REAL,
    ac_power       REAL,
    ac_frequency   REAL,
    dc_voltage     REAL,
    dc_current     REAL,
    dc_power       REAL,
    efficiency_pct REAL,
    temperature    REAL,
    status         INTEGER,
    status_vendor  INTEGER,
    lifetime_wh    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_inverter_fetched ON inverter_readings(fetched_at);
CREATE INDEX IF NOT EXISTS idx_inverter_status ON inverter_readings(status);
CREATE INDEX IF NOT EXISTS idx_readings_cycle ON readings(cycle_id);

-- Modbus reads that failed even after retries. Kept apart from
-- inverter_readings for the same reason poll_cycles is kept apart from
-- readings: our own connectivity problems must never look like inverter data.
CREATE TABLE IF NOT EXISTS modbus_failures (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL,
    reason      TEXT
);
CREATE INDEX IF NOT EXISTS idx_modbus_failures_at ON modbus_failures(occurred_at);

-- One row per site-local day, derived from inverter_readings by
-- src/daily.py (definitions in src/inverter_stats.py). Kept indefinitely,
-- so history outlives INVERTER_RETENTION_DAYS of raw 30-second readings.
-- finalized = 1 once the day is old enough that late data can't change it.
CREATE TABLE IF NOT EXISTS inverter_daily (
    day                 TEXT PRIMARY KEY,   -- YYYY-MM-DD, site-local
    energy_wh           INTEGER,
    peak_dc_w           REAL,
    peak_at             TEXT,
    avg_dc_w            REAL,
    producing_hours     REAL,
    first_producing_at  TEXT,
    last_producing_at   TEXT,
    max_temp_c          REAL,
    min_ac_v            REAL,
    max_ac_v            REAL,
    abnormal_samples    INTEGER,
    reads_ok            INTEGER,
    reads_failed        INTEGER,
    se_last_measured_at TEXT,   -- newest SolarEdge per-panel measurement taken that day
    se_coverage_pct     REAL,   -- share of producing 15-min windows with per-panel data
    se_missing_minutes  REAL,   -- producing time with no (or too little) per-panel data
    partial             INTEGER NOT NULL DEFAULT 0,  -- Modbus didn't see the whole production day
    finalized           INTEGER NOT NULL DEFAULT 0,
    updated_at          TEXT NOT NULL
);
-- Per-panel completeness per day looks readings up by measurement time.
CREATE INDEX IF NOT EXISTS idx_readings_ts ON readings(ts_utc);

-- One row per optimizer per site-local day, from src/panel_daily.py. Kept
-- indefinitely, so per-panel history outlives OPTIMIZER_RETENTION_DAYS of
-- raw readings. See that module for measured vs estimated energy.
CREATE TABLE IF NOT EXISTS panel_daily (
    day            TEXT NOT NULL,   -- YYYY-MM-DD, site-local
    serial         TEXT NOT NULL,
    measured_wh    REAL,            -- own readings integrated over delivered time
    estimated_wh   REAL,            -- inverter's day energy split by measured share
    coverage_pct   REAL,            -- producing 15-min windows with this panel's data
    avg_power_w    REAL,
    peak_w         REAL,
    peak_at        TEXT,
    near_zero_pct  REAL,
    vs_peers_pct   REAL,
    readings       INTEGER,
    finalized      INTEGER NOT NULL DEFAULT 0,
    updated_at     TEXT NOT NULL,
    PRIMARY KEY (day, serial)
);
CREATE INDEX IF NOT EXISTS idx_panel_daily_serial ON panel_daily(serial, day);

-- One row per optimizer per site-local clock hour, from
-- src/panel_daily.py:compute_hours. Kept indefinitely, like panel_daily.
-- Ranks exist only for "scored" hours — every active panel's data present —
-- because an hour with a panel missing can't fairly name a winner.
CREATE TABLE IF NOT EXISTS panel_hourly (
    day            TEXT NOT NULL,     -- YYYY-MM-DD, site-local
    hour           INTEGER NOT NULL,  -- 0-23, site-local
    serial         TEXT NOT NULL,
    measured_wh    REAL,              -- own readings integrated within the hour
    estimated_wh   REAL,              -- inverter's hour energy split by measured share
    avg_power_w    REAL,              -- mean of the hour's readings
    coverage_pct   REAL,              -- producing 15-min windows with this panel's data
    scored         INTEGER NOT NULL DEFAULT 0,
    rank_all       INTEGER,           -- 1 = most energy that hour, whole array
    rank_group     INTEGER,           -- 1 = most energy within its panel-model group
    finalized      INTEGER NOT NULL DEFAULT 0,
    updated_at     TEXT NOT NULL,
    PRIMARY KEY (day, hour, serial)
);

-- Small key/value store, e.g. which panel grouping the stored per-panel
-- history was computed with.
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


@contextmanager
def connect(db_path: Path) -> Iterator[sqlite3.Connection]:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    # Must be set per connection — it is NOT stored in the database file, so
    # setting it once at init does nothing when every write opens a fresh
    # connection. Without it the WAL keeps its high-water mark (~4 MB, the
    # default autocheckpoint threshold) instead of shrinking after a
    # checkpoint.
    conn.execute("PRAGMA journal_size_limit=4194304;")
    try:
        yield conn
    finally:
        conn.close()


def init_db(db_path: Path) -> None:
    with connect(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.executescript(SCHEMA)
        _add_missing_columns(conn)
        conn.commit()


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    """CREATE TABLE IF NOT EXISTS won't alter a table that already exists, so
    additive columns are applied here instead. Deliberately minimal — this
    is not a migration framework — but recreating the database to add a
    nullable column would throw away collected readings, which are not
    reproducible."""
    existing = {r[1] for r in conn.execute("PRAGMA table_info(optimizers)")}
    if "panel_model" not in existing:
        conn.execute("ALTER TABLE optimizers ADD COLUMN panel_model TEXT")


def upsert_optimizers(db_path: Path, optimizers: list[dict]) -> None:
    """optimizers: list of {"serial": str, "label": str, "panel_model": str}."""
    now = _now()
    with connect(db_path) as conn:
        for opt in optimizers:
            conn.execute(
                """
                INSERT INTO optimizers (serial, label, panel_model, first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(serial) DO UPDATE SET
                    label = excluded.label,
                    panel_model = excluded.panel_model,
                    last_seen = excluded.last_seen
                """,
                (opt["serial"], opt.get("label", opt["serial"]),
                 opt.get("panel_model"), now, now),
            )
        conn.commit()


def start_cycle(db_path: Path) -> int:
    with connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO poll_cycles (started_at, status) VALUES (?, 'RUNNING')",
            (_now(),),
        )
        conn.commit()
        return cur.lastrowid


def finish_cycle(
    db_path: Path,
    cycle_id: int,
    status: str,
    *,
    error_message: str | None = None,
    optimizers_known: int | None = None,
    fresh: int | None = None,
    stale: int | None = None,
    missing: int | None = None,
    duration_ms: int | None = None,
    requests_made: int | None = None,
) -> None:
    with connect(db_path) as conn:
        conn.execute(
            """
            UPDATE poll_cycles SET
                finished_at = ?, status = ?, error_message = ?,
                optimizers_known = ?, fresh_count = ?, stale_count = ?,
                missing_count = ?, duration_ms = ?, requests_made = ?
            WHERE id = ?
            """,
            (
                _now(), status, error_message, optimizers_known,
                fresh, stale, missing, duration_ms, requests_made, cycle_id,
            ),
        )
        conn.commit()


def last_measurement_timestamps(db_path: Path) -> dict[str, str]:
    """Latest device timestamp seen per optimizer, used to tell a genuinely
    new reading (FRESH) from the same one repeated (STALE). Read from the DB
    rather than kept in memory so a poller restart doesn't mislabel the
    first cycle after startup."""
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT optimizer_serial, MAX(ts_utc) FROM readings
            WHERE ts_utc IS NOT NULL GROUP BY optimizer_serial
            """
        ).fetchall()
    return {serial: ts for serial, ts in rows}


def insert_readings(db_path: Path, cycle_id: int, readings: list[dict]) -> None:
    now = _now()
    with connect(db_path) as conn:
        conn.executemany(
            """
            INSERT INTO readings
                (cycle_id, optimizer_serial, status, fetched_at, ts_utc,
                 voltage, optimizer_voltage, current, power, energy_wh, raw_json)
            VALUES
                (:cycle_id, :optimizer_serial, :status, :fetched_at, :ts_utc,
                 :voltage, :optimizer_voltage, :current, :power, :energy_wh, :raw_json)
            """,
            [{**r, "cycle_id": cycle_id, "fetched_at": now} for r in readings],
        )
        conn.commit()


DAILY_COLUMNS = (
    "energy_wh", "peak_dc_w", "peak_at", "avg_dc_w", "producing_hours",
    "first_producing_at", "last_producing_at", "max_temp_c", "min_ac_v",
    "max_ac_v", "abnormal_samples", "reads_ok", "reads_failed",
    "se_last_measured_at", "se_coverage_pct", "se_missing_minutes", "partial", "finalized",
)


def upsert_inverter_daily(db_path: Path, day: str, values: dict) -> None:
    cols = ", ".join(DAILY_COLUMNS)
    marks = ", ".join("?" for _ in DAILY_COLUMNS)
    updates = ", ".join(f"{c}=excluded.{c}" for c in DAILY_COLUMNS)
    with connect(db_path) as conn:
        conn.execute(
            f"""
            INSERT INTO inverter_daily (day, {cols}, updated_at) VALUES (?, {marks}, ?)
            ON CONFLICT(day) DO UPDATE SET {updates}, updated_at=excluded.updated_at
            """,
            (day, *(values.get(c) for c in DAILY_COLUMNS), _now()),
        )
        conn.commit()


PANEL_DAILY_COLUMNS = (
    "measured_wh", "estimated_wh", "coverage_pct", "avg_power_w", "peak_w",
    "peak_at", "near_zero_pct", "vs_peers_pct", "readings",
)


def upsert_panel_daily(db_path: Path, day: str, figures: dict[str, dict], finalized: int) -> None:
    cols = ", ".join(PANEL_DAILY_COLUMNS)
    marks = ", ".join("?" for _ in PANEL_DAILY_COLUMNS)
    updates = ", ".join(f"{c}=excluded.{c}" for c in PANEL_DAILY_COLUMNS)
    now = _now()
    with connect(db_path) as conn:
        conn.executemany(
            f"""
            INSERT INTO panel_daily (day, serial, {cols}, finalized, updated_at)
            VALUES (?, ?, {marks}, ?, ?)
            ON CONFLICT(day, serial) DO UPDATE SET {updates},
                finalized=excluded.finalized, updated_at=excluded.updated_at
            """,
            [(day, serial, *(f.get(c) for c in PANEL_DAILY_COLUMNS), finalized, now)
             for serial, f in figures.items()],
        )
        conn.commit()


PANEL_HOURLY_COLUMNS = (
    "measured_wh", "estimated_wh", "avg_power_w", "coverage_pct",
    "scored", "rank_all", "rank_group",
)


def upsert_panel_hourly(db_path: Path, day: str, hours: dict[int, dict[str, dict]],
                        finalized: int) -> None:
    cols = ", ".join(PANEL_HOURLY_COLUMNS)
    marks = ", ".join("?" for _ in PANEL_HOURLY_COLUMNS)
    updates = ", ".join(f"{c}=excluded.{c}" for c in PANEL_HOURLY_COLUMNS)
    now = _now()
    with connect(db_path) as conn:
        conn.executemany(
            f"""
            INSERT INTO panel_hourly (day, hour, serial, {cols}, finalized, updated_at)
            VALUES (?, ?, ?, {marks}, ?, ?)
            ON CONFLICT(day, hour, serial) DO UPDATE SET {updates},
                finalized=excluded.finalized, updated_at=excluded.updated_at
            """,
            [(day, hour, serial, *(f.get(c) for c in PANEL_HOURLY_COLUMNS), finalized, now)
             for hour, per in hours.items() for serial, f in per.items()],
        )
        conn.commit()


def get_meta(db_path: Path, key: str) -> str | None:
    with connect(db_path) as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_meta(db_path: Path, key: str, value: str) -> None:
    with connect(db_path) as conn:
        conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
        conn.commit()


def unfinalize_panel_history(db_path: Path) -> int:
    """Mark per-panel daily/hourly rows for recomputation (the roll-up only
    recomputes days whose raw readings still exist)."""
    with connect(db_path) as conn:
        n = conn.execute("UPDATE panel_daily SET finalized = 0").rowcount
        conn.execute("UPDATE panel_hourly SET finalized = 0")
        conn.commit()
    return n


def prune_optimizer_raw(db_path: Path, keep_days: int) -> int:
    """Delete raw per-optimizer readings older than keep_days (by poll time,
    as every range query here is). panel_daily is never pruned."""
    with connect(db_path) as conn:
        n = conn.execute(
            "DELETE FROM readings WHERE fetched_at < strftime('%Y-%m-%dT%H:%M:%SZ','now', ?)",
            (f"-{int(keep_days)} days",)).rowcount
        conn.commit()
    return n


def prune_inverter_raw(db_path: Path, keep_days: int) -> tuple[int, int]:
    """Delete raw Modbus rows (and failure rows) older than keep_days. The
    daily summary table is never pruned."""
    cutoff = f"-{int(keep_days)} days"
    with connect(db_path) as conn:
        a = conn.execute(
            "DELETE FROM inverter_readings "
            "WHERE fetched_at < strftime('%Y-%m-%dT%H:%M:%SZ','now', ?)", (cutoff,)).rowcount
        b = conn.execute(
            "DELETE FROM modbus_failures "
            "WHERE occurred_at < strftime('%Y-%m-%dT%H:%M:%SZ','now', ?)", (cutoff,)).rowcount
        conn.commit()
    return a, b


def latest_inverter_lifetime(db_path: Path) -> int | None:
    """Highest lifetime counter seen, so stale snapshots are still caught
    straight after a restart."""
    with connect(db_path) as conn:
        row = conn.execute("SELECT MAX(lifetime_wh) FROM inverter_readings").fetchone()
    return row[0] if row else None


def insert_modbus_failure(db_path: Path, reason: str) -> None:
    with connect(db_path) as conn:
        conn.execute(
            "INSERT INTO modbus_failures (occurred_at, reason) VALUES (?, ?)",
            (_now(), reason),
        )
        conn.commit()


def insert_inverter_reading(db_path: Path, cycle_id: int | None, data: dict) -> None:
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO inverter_readings
                (cycle_id, fetched_at, ac_voltage, ac_current, ac_power,
                 ac_frequency, dc_voltage, dc_current, dc_power,
                 efficiency_pct, temperature, status, status_vendor, lifetime_wh)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                cycle_id, _now(), data.get("ac_voltage"), data.get("ac_current"),
                data.get("ac_power"), data.get("ac_frequency"),
                data.get("dc_voltage"), data.get("dc_current"), data.get("dc_power"),
                data.get("efficiency_pct"), data.get("temperature"),
                data.get("status"), data.get("status_vendor"), data.get("lifetime_wh"),
            ),
        )
        conn.commit()


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
