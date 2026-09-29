"""
Main polling loop: every POLL_INTERVAL_SECONDS, fetches live readings for
all known optimizers in one batched call and records, for every optimizer,
whether it reported something new (FRESH), repeated its last reading
(STALE), or was absent from the response (MISSING).

Our own failures (network, auth, discovery) are recorded only against the
poll_cycles row and produce no readings at all — so a failure on our side
can never be counted against an optimizer's reliability.

Designed to run unattended for days under systemd or scripts/poller_ctl.sh:
a failed cycle is recorded and logged rather than crashing the process.
"""
from __future__ import annotations

import json
import logging
import signal
import threading
import time
from types import FrameType

import requests

from . import daily, db, modbus_client
from . import groups as panel_groups
from .client import RateLimited, SolarEdgeClient
from .config import Config
from .discover import Optimizer, discover_optimizers

logger = logging.getLogger(__name__)

_shutdown = False
_stop = threading.Event()

MAINTENANCE_INTERVAL_SECONDS = 3600


def _handle_signal(signum: int, frame: FrameType | None) -> None:
    global _shutdown
    logger.info("Received signal %s, shutting down after current cycle", signum)
    _shutdown = True
    _stop.set()


def _modbus_sampler(cfg: Config) -> None:
    """Reads the inverter every MODBUS_INTERVAL_SECONDS on its own thread.

    Kept off the cloud-poll cycle so a slow or unreachable inverter can
    never delay optimizer polling, and so it can run much faster than the
    cloud poll. It lives in this process rather than its own because the
    inverter historically accepts only one Modbus TCP client at a time —
    one sampler guarantees we never collide with ourselves.
    """
    failing = False
    last_lifetime = db.latest_inverter_lifetime(cfg.db_path)
    stale_run = 0
    while not _stop.is_set():
        started = time.monotonic()
        try:
            inv = modbus_client.read_inverter(
                cfg.inverter_host, cfg.inverter_port, timeout=5, attempts=2
            )
            # The lifetime counter only ever rises. Over the lossy link the
            # inverter sometimes answers with a register snapshot ~20 min
            # old (seen 2026-09-25: counter back by up to 488 Wh), which
            # would file old power figures under a new timestamp.
            # After many rejections in a row, trust the inverter again rather
            # than let one bad high reading block every read after it.
            lifetime = inv.get("lifetime_wh")
            if (last_lifetime and lifetime and lifetime < last_lifetime
                    and stale_run < 10):
                stale_run += 1
                raise modbus_client.ModbusUnavailable(
                    f"stale snapshot: lifetime counter {last_lifetime - lifetime} Wh behind"
                )
            stale_run = 0
            if lifetime:
                last_lifetime = lifetime
            db.insert_inverter_reading(cfg.db_path, None, inv)
            if failing:
                logger.info("Modbus reads recovered")
                failing = False
            if inv["status"] in (5, 7):
                logger.warning(
                    "Inverter status %s (%s) — AC %.1f V, %.0f W",
                    inv["status"],
                    modbus_client.STATUS_NAMES.get(inv["status"], "?"),
                    inv["ac_voltage"], inv["ac_power"],
                )
        except modbus_client.ModbusUnavailable as exc:
            # Every failure goes in the DB; the log only records the
            # transition, or an unreachable inverter writes 2,880 lines a day.
            try:
                db.insert_modbus_failure(cfg.db_path, str(exc))
            except Exception:
                logger.exception("Could not record Modbus failure")
            if not failing:
                logger.warning("Modbus read failed: %s", exc)
                failing = True
        except Exception:  # never let the sampler thread die
            logger.exception("Modbus sampler error")
        _stop.wait(max(0.0, cfg.modbus_interval_seconds - (time.monotonic() - started)))


def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    cfg = Config.load()
    db.init_db(cfg.db_path)
    client = SolarEdgeClient(cfg)

    optimizers: list[Optimizer] = []
    last_discovery = 0.0
    discovery_interval = cfg.discovery_refresh_hours * 3600

    logger.info(
        "Starting poller: site=%s interval=%ss", cfg.site_id, cfg.poll_interval_seconds
    )
    _check_timezone(client, cfg)

    if cfg.inverter_host:
        threading.Thread(
            target=_modbus_sampler, args=(cfg,), name="modbus", daemon=True
        ).start()
        logger.info(
            "Modbus sampler: %s:%s every %ss",
            cfg.inverter_host, cfg.inverter_port, cfg.modbus_interval_seconds,
        )

    backoff_until = 0.0
    last_maintenance = 0.0

    while not _shutdown:
        cycle_start = time.monotonic()
        cycle_id = db.start_cycle(cfg.db_path)
        phase = "discovery"
        requests_before = client.request_count
        try:
            if time.monotonic() < backoff_until:
                raise RateLimited("backing off after an earlier throttle")
            if not optimizers or (time.monotonic() - last_discovery) > discovery_interval:
                optimizers = discover_optimizers(client)
                db.upsert_optimizers(
                    cfg.db_path,
                    [
                        {
                            "serial": o.serial,
                            "label": f"{o.name} ({o.panel_model})",
                            "panel_model": o.panel_model,
                        }
                        for o in optimizers
                    ],
                )
                last_discovery = time.monotonic()

            phase = "fetch"
            serials = [o.serial for o in optimizers]
            data = client.fetch_optimizer_live_data(serials)

            previous = db.last_measurement_timestamps(cfg.db_path)
            readings = _build_readings(serials, data, previous)
            db.insert_readings(cfg.db_path, cycle_id, readings)

            counts = {"FRESH": 0, "STALE": 0, "MISSING": 0}
            for r in readings:
                counts[r["status"]] += 1
            db.finish_cycle(
                cfg.db_path, cycle_id, "OK",
                optimizers_known=len(serials),
                fresh=counts["FRESH"], stale=counts["STALE"], missing=counts["MISSING"],
                duration_ms=int((time.monotonic() - cycle_start) * 1000),
                requests_made=client.request_count - requests_before,
            )
            logger.info(
                "Cycle OK: %d fresh, %d stale, %d missing (%d request(s))",
                counts["FRESH"], counts["STALE"], counts["MISSING"],
                client.request_count - requests_before,
            )
        except Exception as exc:  # keep the loop alive no matter what fails
            status = _classify_failure(exc, phase)
            if status == "RATE_LIMITED":
                # Throttled: wait several intervals before trying again
                # rather than retrying straight into the same wall.
                backoff_until = time.monotonic() + cfg.poll_interval_seconds * 5
                logger.warning("Rate limited by SolarEdge — backing off: %s", exc)
            else:
                logger.exception("Polling cycle failed (%s)", status)
            db.finish_cycle(
                cfg.db_path, cycle_id, status,
                error_message=f"{type(exc).__name__}: {exc}",
                duration_ms=int((time.monotonic() - cycle_start) * 1000),
                requests_made=client.request_count - requests_before,
            )

        # Daily inverter + per-panel history and raw retention, hourly.
        # Between polls, and isolated, so it can never cost a cycle.
        if time.monotonic() - last_maintenance > MAINTENANCE_INTERVAL_SECONDS:
            last_maintenance = time.monotonic()
            try:
                grouping = panel_groups.parse(cfg.panel_groups)
                if grouping.error:
                    logger.warning(grouping.error)
                daily.run_maintenance(
                    cfg.db_path, cfg.site_timezone, cfg.inverter_retention_days,
                    cfg.optimizer_retention_days, cfg.decommissioned, grouping)
            except Exception:
                logger.exception("Daily history update failed")

        elapsed = time.monotonic() - cycle_start
        _stop.wait(max(0.0, cfg.poll_interval_seconds - elapsed))


def _check_timezone(client: SolarEdgeClient, cfg: Config) -> None:
    """Warn loudly if SITE_TIMEZONE disagrees with the site's own setting.

    This shipped wrong once: the default is the machine's timezone (UTC),
    while the site was five hours behind it, so every hour-of-day figure was off
    by five hours — readings taken at 2pm were labelled 7pm, which points
    shading analysis at entirely the wrong sun position.
    """
    try:
        actual = client.fetch_site_timezone()
    except Exception:
        return
    if actual and actual != cfg.site_timezone:
        logger.warning(
            "SITE_TIMEZONE is %r but SolarEdge reports the site is in %r. "
            "Every hour-of-day figure will be wrong until this is fixed in .env.",
            cfg.site_timezone, actual,
        )
    elif actual:
        logger.info("Site timezone confirmed: %s", actual)


def _classify_failure(exc: Exception, phase: str) -> str:
    """Kept distinct so reports can exclude cycles that failed on our side
    rather than blaming an optimizer. RATE_LIMITED is split out from
    NETWORK_ERROR specifically so 'we are polling too hard' is visible as
    its own number rather than buried among ordinary connectivity blips."""
    if isinstance(exc, RateLimited):
        return "RATE_LIMITED"
    if isinstance(exc, requests.RequestException):
        return "NETWORK_ERROR"
    if isinstance(exc, RuntimeError) and "login" in str(exc).lower():
        return "AUTH_ERROR"
    if phase == "discovery":
        return "DISCOVERY_ERROR"
    return "ERROR"


def _build_readings(
    serials: list[str], data: dict, previous: dict[str, str]
) -> list[dict]:
    """One row per known optimizer, every cycle — present-and-newer is
    FRESH, present-but-same-timestamp is STALE, absent is MISSING."""
    live = data.get("serialToLiveData", {})
    readings = []
    for serial in serials:
        d = live.get(serial)
        if d is None:
            readings.append({
                "optimizer_serial": serial, "status": "MISSING", "ts_utc": None,
                "voltage": None, "optimizer_voltage": None, "current": None,
                "power": None, "energy_wh": None, "raw_json": None,
            })
            continue

        ts = d.get("lastMeasurement")
        prev = previous.get(serial)
        status = "FRESH" if (ts and (prev is None or ts > prev)) else "STALE"
        readings.append({
            "optimizer_serial": serial,
            "status": status,
            "ts_utc": ts,
            "voltage": d.get("voltage_V"),
            "optimizer_voltage": d.get("optimizerVoltage_V"),
            "current": d.get("current_A"),
            "power": d.get("power_W"),
            "energy_wh": d.get("total_last_telemetry_energy_WH"),
            # Only kept for FRESH. A STALE payload is byte-identical to the
            # FRESH one that introduced that timestamp (verified across 170
            # groups, one with 50 repeats and a single distinct payload), so
            # storing it again was 94% of all stored JSON for no new
            # information. The typed columns are still written either way.
            "raw_json": json.dumps(d) if status == "FRESH" else None,
        })
    return readings
