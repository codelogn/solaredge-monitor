from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Config:
    username: str
    password: str
    site_id: str
    poll_interval_seconds: int
    discovery_refresh_hours: int
    db_path: Path
    cookies_path: Path
    # IANA name (e.g. "Europe/London"). Readings are stored in UTC, but
    # hour-of-day analysis has to be in the site's own time or "12pm" means
    # nothing. Defaults to this machine's timezone.
    site_timezone: str
    # Inverter Modbus TCP endpoint. The cloud API has no AC-side data at
    # all, so without this a limited inverter can't be told apart from an
    # underproducing array. Blank disables the read.
    inverter_host: str
    inverter_port: int
    # Modbus is local and cheap, so it's sampled far more often than the
    # cloud poll: 2-minute snapshots missed short midday peaks entirely.
    modbus_interval_seconds: int
    # Optional total panel rating (W), shown beside today's peak. 0 = unset.
    array_nameplate_w: int
    # Optimizers physically removed/replaced. They remain in SolarEdge's
    # layout forever and would otherwise sit at the bottom of every report
    # as NO DATA, hiding real faults behind known-dead hardware.
    decommissioned: frozenset

    @classmethod
    def load(cls) -> "Config":
        username = os.environ.get("SOLAREDGE_USERNAME", "")
        password = os.environ.get("SOLAREDGE_PASSWORD", "")
        site_id = os.environ.get("SOLAREDGE_SITE_ID", "")
        if not username or not password or not site_id:
            raise RuntimeError(
                "SOLAREDGE_USERNAME, SOLAREDGE_PASSWORD and SOLAREDGE_SITE_ID "
                "must be set (copy .env.example to .env and fill it in)."
            )
        return cls(
            username=username,
            password=password,
            site_id=site_id,
            poll_interval_seconds=int(os.environ.get("POLL_INTERVAL_SECONDS", "300")),
            discovery_refresh_hours=int(os.environ.get("DISCOVERY_REFRESH_HOURS", "24")),
            db_path=BASE_DIR / os.environ.get("DB_PATH", "data/solar_monitor.db"),
            cookies_path=BASE_DIR / os.environ.get("COOKIES_PATH", "session/cookies.json"),
            site_timezone=os.environ.get("SITE_TIMEZONE", "") or _system_timezone(),
            inverter_host=os.environ.get("INVERTER_MODBUS_HOST", ""),
            inverter_port=int(os.environ.get("INVERTER_MODBUS_PORT", "1502")),
            modbus_interval_seconds=int(os.environ.get("MODBUS_INTERVAL_SECONDS", "30")),
            array_nameplate_w=int(os.environ.get("ARRAY_NAMEPLATE_W", "0") or 0),
            decommissioned=frozenset(
                s.strip() for s in os.environ.get("DECOMMISSIONED_SERIALS", "").split(",")
                if s.strip()
            ),
        )


def _system_timezone() -> str:
    try:
        return datetime.now().astimezone().tzinfo.key  # type: ignore[attr-defined]
    except AttributeError:
        return time.strftime("%Z") or "UTC"
