#!/usr/bin/env python3
"""
Compare a panel's ACTUAL output against what its datasheet says it should
produce at the irradiance and temperature you measured.

This is the test that separates "shaded" from "defective", and it needs no
IV-curve tracer and no disconnection: the optimizers already log per-panel
voltage and current every couple of minutes, so the only reading you have to
take by hand is irradiance (and ideally back-of-panel temperature).

Usage
-----
  # you stood at panel 1.0.6 and the meter read 910 W/m^2, panel back 52 C
  python3 scripts/panel_check.py --pos 1.0.6 --irradiance 910 --temp 52

  # all panels at once, if the whole array is in the same light
  python3 scripts/panel_check.py --all --irradiance 910 --temp 52

  # a measurement you took earlier
  python3 scripts/panel_check.py --all --irradiance 880 --temp 50 --at 12:35

Defaults are for a REC 365 W class panel — override them from YOUR datasheet
(the sticker on the back of the panel), because the correction below is only
as good as the numbers you feed it.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.config import Config  # noqa: E402

# REC 365-class defaults. Check these against your own datasheet.
DEFAULTS = {
    "pmax": 365.0,      # W at STC
    "imp": 9.60,        # A at max power point, STC
    "vmp": 38.0,        # V at max power point, STC
    "gamma": -0.34,     # %/C  power temperature coefficient
    "beta": -0.26,      # %/C  voltage temperature coefficient
}


def expected(pmax: float, irradiance: float, temp_c: float, gamma: float) -> float:
    """Power a healthy panel should make right now.

    Output is very close to linear in irradiance, then derated for cell
    temperature — which matters a lot on a hot roof, where 55-65 C is
    normal and costs 10-14%.
    """
    return pmax * (irradiance / 1000.0) * (1 + (gamma / 100.0) * (temp_c - 25.0))


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--irradiance", type=float, required=True,
                   help="W/m^2 measured IN THE PLANE OF THE PANEL (not flat)")
    p.add_argument("--temp", type=float, default=None,
                   help="back-of-panel temperature in C (assumes ambient+25 if omitted)")
    p.add_argument("--pos", help="panel position, e.g. 1.0.6")
    p.add_argument("--all", action="store_true", help="every reporting panel")
    p.add_argument("--at", help="site-local HH:MM of your reading (default: latest)")
    p.add_argument("--pmax", type=float, default=DEFAULTS["pmax"])
    p.add_argument("--gamma", type=float, default=DEFAULTS["gamma"])
    args = p.parse_args()

    if not args.pos and not args.all:
        p.error("give --pos 1.0.6 or --all")

    cfg = Config.load()
    tz = ZoneInfo(cfg.site_timezone)
    temp_c = args.temp if args.temp is not None else 50.0
    if args.temp is None:
        print("NOTE: no --temp given, assuming 50 C back-of-panel. Measure it "
              "with a cheap IR thermometer for a real answer.\n")

    conn = sqlite3.connect(f"file:{cfg.db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    if args.at:
        hh, mm = args.at.split(":")
        now_local = datetime.now(tz)
        target = now_local.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
    else:
        target = datetime.now(tz)
    target_utc = target.astimezone(ZoneInfo("UTC"))
    lo = (target_utc - timedelta(minutes=8)).strftime("%Y-%m-%dT%H:%M:%SZ")
    hi = (target_utc + timedelta(minutes=8)).strftime("%Y-%m-%dT%H:%M:%SZ")

    rows = conn.execute(
        """
        SELECT o.label, r.optimizer_serial, r.ts_utc, r.power, r.voltage, r.current
        FROM readings r JOIN optimizers o ON o.serial = r.optimizer_serial
        WHERE r.status = 'FRESH' AND r.ts_utc BETWEEN ? AND ?
        ORDER BY r.ts_utc
        """,
        (lo, hi),
    ).fetchall()
    conn.close()

    if not rows:
        print(f"No FRESH readings within +/-8 min of {target:%H:%M %Z}.")
        print("Readings arrive every few minutes — wait a moment, or pass --at.")
        return

    # latest reading per panel inside the window
    latest: dict[str, sqlite3.Row] = {}
    for r in rows:
        latest[r["optimizer_serial"]] = r

    exp = expected(args.pmax, args.irradiance, temp_c, args.gamma)
    print(f"Measured irradiance : {args.irradiance:.0f} W/m^2")
    print(f"Cell temperature    : {temp_c:.0f} C")
    print(f"A healthy {args.pmax:.0f} W panel should make ~{exp:.0f} W in these conditions.\n")
    print(f"  {'pos':8} {'actual W':>9} {'expected W':>11} {'% of spec':>10}   verdict")

    for r in sorted(latest.values(), key=lambda x: x["power"]):
        label = r["label"] or ""
        pos = label.replace("Optimizer ", "").split(" ")[0]
        if args.pos and pos != args.pos:
            continue
        pct = 100 * r["power"] / exp if exp else 0
        if pct >= 85:
            verdict = "healthy"
        elif pct >= 60:
            verdict = "mild loss (soiling/angle?)"
        elif pct >= 25:
            verdict = "SHADED or degraded"
        else:
            verdict = "SEVERE - shaded, or a real fault"
        print(f"  {pos:8} {r['power']:9.1f} {exp:11.1f} {pct:9.0f}%   {verdict}")

    print()
    print("How to read this:")
    print("  If a panel is in FULL measured sun and still reads <60% of spec,")
    print("  that is warranty evidence. If it is low because the meter also")
    print("  reads low at that panel, it is shading and the panel is fine.")
    print("  Measure irradiance AT EACH PANEL you care about - the whole point")
    print("  is that irradiance differs across your array.")


if __name__ == "__main__":
    main()
