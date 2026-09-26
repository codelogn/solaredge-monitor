"""
Thin HTTP client for the monitoring.solaredge.com internal endpoints, built
on top of auth.py's requests.Session. Transparently re-logs in whenever the
session has expired, so the poller can run unattended for days.

Endpoints below were confirmed on 2026-09-23 by capturing real browser
traffic against the user's own account/site (see git history of this file
and README "Phase 1" for how). They are internal/undocumented and may change
without notice.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import requests

from . import auth
from .config import Config

logger = logging.getLogger(__name__)

BASE_URL = "https://monitoring.solaredge.com"

# Site equipment tree (inverters/strings/optimizers) — used to enumerate
# optimizer serials. Confirmed shape: nested {"type": "OPTIMIZER", "serial":
# ..., "name": ..., "properties": {"panelModelName": ...}} nodes.
SITE_LAYOUT_URL = BASE_URL + "/services/layout/logical/generic/v2/site/{site_id}"

# Per-optimizer live electrical readings. POST body is a JSON array of
# serial strings (any number, batching all of them in one call works).
# Response: {"basicInformationList": [...], "serialToLiveData": {serial:
# {"lastMeasurement": "...Z", "voltage_V", "optimizerVoltage_V", "current_A",
# "power_W", "total_last_telemetry_energy_WH"}}}. Optimizers with no recent
# telemetry are simply absent from serialToLiveData.
OPTIMIZER_LIVE_DATA_URL = BASE_URL + "/services/layout/information/optimizers"

# Carries the site's IANA timezone, which every hour-of-day figure depends on.
SITE_LIST_URL = BASE_URL + "/services/sitelist/{site_id}"


class RateLimited(Exception):
    """SolarEdge (or Cloudflare in front of it) is throttling us."""


class SolarEdgeClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.session = auth.load_session(cfg.cookies_path) or auth.login(cfg)
        # Counts every HTTP call we make to SolarEdge, so the dashboard can
        # report real load rather than an assumption about it.
        self.request_count = 0

    def _get(self, url: str, params: dict[str, Any]) -> requests.Response:
        return self._request("GET", url, params=params)

    def _post_json(self, url: str, payload: Any) -> requests.Response:
        return self._request("POST", url, data=json.dumps(payload))

    def _request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        headers = {"Content-Type": "application/json"} if method == "POST" else {}

        self.request_count += 1
        resp = self.session.request(method, url, headers=headers, timeout=30, **kwargs)

        # Check throttling before session expiry: a 403 can mean either, and
        # re-authenticating launches a browser, which is the worst possible
        # response to being told we're sending too much.
        if auth.is_rate_limited(resp):
            raise RateLimited(f"HTTP {resp.status_code} from {url}")

        if auth.is_session_expired(resp):
            logger.info("Session expired, re-authenticating")
            self.session = auth.login(self.cfg)
            self.request_count += 1
            resp = self.session.request(method, url, headers=headers, timeout=30, **kwargs)
            if auth.is_rate_limited(resp):
                raise RateLimited(f"HTTP {resp.status_code} from {url} after re-auth")

        resp.raise_for_status()
        return resp

    def fetch_site_layout(self) -> dict:
        url = SITE_LAYOUT_URL.format(site_id=self.cfg.site_id)
        return self._get(url, {"include-optimizers": "true"}).json()

    def fetch_optimizer_live_data(self, serials: list[str]) -> dict:
        return self._post_json(OPTIMIZER_LIVE_DATA_URL, serials).json()

    def fetch_site_timezone(self) -> str | None:
        """The site's own IANA timezone, e.g. 'America/New_York'.

        Hour-of-day analysis is meaningless in the wrong zone, and the
        default (this machine's timezone) was silently 5 hours off — so the
        authoritative value is read from SolarEdge rather than assumed.
        """
        url = SITE_LIST_URL.format(site_id=self.cfg.site_id)
        try:
            return self._get(url, {}).json().get("timeZone")
        except Exception:
            return None
