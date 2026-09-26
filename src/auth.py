"""
Authenticates against monitoring.solaredge.com. Confirmed (2026-09-23) via
live capture against the real account: login goes through SolarEdge's
Cognito-hosted auth at login.solaredge.com (OAuth2 authorization-code +
PKCE), and the site is behind Cloudflare (cf_clearance/__cf_bm cookies).
There's no 2FA on this account, so the whole flow can run headlessly.

A plain requests.Session CAN reuse the cookies produced by that login for
ongoing polling (verified: a fresh requests.Session with the exported
cookies + matching User-Agent successfully called the live-data endpoint) —
so Playwright is only needed for the login itself, not for every poll.
Session expiry (Cloudflare re-challenge, Cognito session timeout) is
recovered by re-running the Playwright login.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

from .config import Config

logger = logging.getLogger(__name__)

ENTRY_URL = "https://monitoring.solaredge.com/one#/site-list"

# The cookie the portal sets once it has exchanged the OAuth code for a real
# session. Its presence — not any page load — is what "logged in" means.
SESSION_COOKIE = "se_monitoring_auth"
LOGIN_TIMEOUT_SECONDS = 60


def login(cfg: Config) -> requests.Session:
    """Drive a real (headless) browser through the Cognito login flow, then
    return a requests.Session carrying the resulting cookies."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        user_agent = page.evaluate("navigator.userAgent")

        page.goto(ENTRY_URL, wait_until="networkidle", timeout=45000)
        page.wait_for_timeout(1000)
        page.get_by_text("Log in", exact=True).click()
        page.wait_for_load_state("networkidle", timeout=45000)

        page.locator('input[name="username"]').first.fill(cfg.username)
        page.locator('input[name="password"]').first.fill(cfg.password)
        page.locator('button[type="submit"]').first.click()
        page.wait_for_load_state("networkidle", timeout=45000)

        # Wait for the session cookie itself, not for a page load or a fixed
        # sleep. After the credentials are accepted the browser lands on
        # /mfe/auth/callback and only then exchanges the OAuth code for the
        # real session, a few seconds later. Grabbing cookies before that
        # exchange yields a session that looks logged in and 401s on every
        # call — a silent failure that would burn a whole day of collection.
        cookies: list[dict] = []
        deadline = time.monotonic() + LOGIN_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            cookies = context.cookies()
            if has_session_cookie(cookies):
                break
            page.wait_for_timeout(500)

        if not has_session_cookie(cookies):
            still_on_login = "login.solaredge.com" in page.url
            browser.close()
            raise RuntimeError(
                "SolarEdge login failed: "
                + (
                    "still on the login page — check SOLAREDGE_USERNAME/"
                    "PASSWORD in .env."
                    if still_on_login
                    else f"no {SESSION_COOKIE} cookie appeared within "
                    f"{LOGIN_TIMEOUT_SECONDS}s (last URL: {page.url}). The "
                    "login flow may have changed — run "
                    "`python -m src.capture_endpoints` to inspect it."
                )
            )

        browser.close()

    session = _session_from_cookies(cookies, user_agent)
    save_session(cfg.cookies_path, cookies, user_agent)
    logger.info("Logged in to SolarEdge; session saved to %s", cfg.cookies_path)
    return session


def _session_from_cookies(cookies: list[dict], user_agent: str) -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": user_agent})
    for c in cookies:
        session.cookies.set(
            c["name"], c["value"], domain=c["domain"], path=c.get("path", "/")
        )
    return session


def save_session(cookies_path: Path, cookies: list[dict], user_agent: str) -> None:
    cookies_path.parent.mkdir(parents=True, exist_ok=True)
    data = {"saved_at": time.time(), "user_agent": user_agent, "cookies": cookies}
    cookies_path.write_text(json.dumps(data))


def load_session(cookies_path: Path) -> requests.Session | None:
    """Returns None (forcing a fresh login) unless the saved file actually
    carries a session cookie — loading a half-written or superseded one
    would otherwise 401 on every cycle instead of just re-authenticating."""
    if not cookies_path.exists():
        return None
    try:
        data = json.loads(cookies_path.read_text())
    except (json.JSONDecodeError, OSError):
        return None

    cookies = data.get("cookies", [])
    if not has_session_cookie(cookies):
        logger.info("Saved session has no %s cookie — logging in afresh", SESSION_COOKIE)
        return None
    return _session_from_cookies(cookies, data.get("user_agent", ""))


def has_session_cookie(cookies: list[dict]) -> bool:
    return any(c.get("name") == SESSION_COOKIE for c in cookies)


def is_rate_limited(response: requests.Response) -> bool:
    """Distinguishes 'SolarEdge is throttling us' from 'our session lapsed'.

    This matters for more than reporting: re-authenticating means driving a
    whole headless browser through the login flow, so treating a throttle
    as an expired session would answer being told to slow down by hitting
    them considerably harder.
    """
    if response.status_code == 429:
        return True
    # Cloudflare sits in front of this API and serves an HTML challenge or
    # block page (not JSON) when it decides a client is misbehaving.
    if response.status_code in (403, 503):
        body = response.text[:2000].lower()
        markers = ("cloudflare", "cf-ray", "attention required",
                   "rate limit", "too many requests", "access denied")
        if any(m in body for m in markers) or "cf-ray" in response.headers:
            return True
    return False


def is_session_expired(response: requests.Response) -> bool:
    """The live-data endpoint returns JSON on success; an expired session
    redirects to login, 401s, or returns a non-JSON body. Callers must check
    is_rate_limited() first — a throttle can look similar from here."""
    if response.status_code in (401, 403):
        return True
    content_type = response.headers.get("content-type", "")
    if "application/json" not in content_type:
        return True
    return False
