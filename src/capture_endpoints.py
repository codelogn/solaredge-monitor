"""
Interactive network-sniffing helper, kept around for if/when SolarEdge
changes their frontend/API and src/client.py's endpoints stop working. Opens
a real (headed) browser, logs in via the Cognito-hosted flow using
SOLAREDGE_USERNAME/PASSWORD from .env, navigates to the site's digital-twin
view, and prints every internal API request/response it observes.

Usage:
    python -m src.capture_endpoints

Then, in the opened browser, click around the "Logical" tab (expand the
inverter/string tree, click an individual optimizer) to trigger the calls
that matter. Watch this terminal for captured request/response pairs. Press
Ctrl+C here when done.
"""
from __future__ import annotations

import json

from playwright.sync_api import sync_playwright

from .config import Config

ENTRY_URL = "https://monitoring.solaredge.com/one#/site-list"
_INTERESTING_HOST = "monitoring.solaredge.com"


def main() -> None:
    cfg = Config.load()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()

        def on_response(response):
            url = response.url
            if _INTERESTING_HOST not in url or "/services/" not in url:
                return
            print(f"\n=== {response.request.method} {url}")
            if response.request.method == "POST":
                print("body:", response.request.post_data)
            try:
                print(json.dumps(response.json(), indent=2)[:2000])
            except Exception:
                pass

        page.on("response", on_response)

        page.goto(ENTRY_URL, wait_until="networkidle", timeout=45000)
        page.get_by_text("Log in", exact=True).click()
        page.wait_for_load_state("networkidle", timeout=45000)

        print(f"If not already filled in, log in manually (account: {cfg.username}).")
        print(f"Then browse to site {cfg.site_id}'s digital-twin / Logical tab and")
        print("click into individual optimizers. Watching network traffic —")
        print("press Ctrl+C here when done.\n")

        try:
            while True:
                page.wait_for_timeout(1000)
        except KeyboardInterrupt:
            pass

        browser.close()


if __name__ == "__main__":
    main()
