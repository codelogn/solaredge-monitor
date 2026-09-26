"""
Turns collected readings into a good/bad verdict per optimizer.

Method — why it's peer-relative rather than absolute:
absolute output is meaningless across time (sun angle, cloud cover), but
over any short stretch every optimizer on this roof shares the same sky. So
measurements are grouped into fixed windows of measurement time, each
optimizer is expressed as a ratio of its peers' median in that window, and
the ratios are aggregated with a median again, so one odd window can't swing
the verdict.

What is compared is each optimizer's reported *interval energy*
(`energy_wh`), not its instantaneous power snapshot. Measured on this site
(2026-09-25):
  - snapshot power is a single instant; under broken cloud, optimizers
    sampled a minute apart see different skies, and ~8% of daytime
    snapshots are 0 W glitches (0 A at open-circuit voltage) whose interval
    energy is perfectly normal (median ratio 1.00 vs neighbours, n=202);
  - energy_wh covers a roughly fixed trailing window (~15 min, independent
    of the gap between reports), so it integrates over cloud and two panels
    reporting a few minutes apart cover nearly the same sky.
The old snapshot method had to throw most cycles away for being measured
too far apart or under changing light (912 of ~1,300 in one 3-day run);
energy windows need neither filter.

energy_wh's absolute scale is not Wh (summed across reports it was 4.34x the
inverter's AC energy, because the windows overlap), so it is only ever used
relatively here.

Two independent failure modes are scored separately and then combined:
  reliability — does it report at all (in how many daylight windows)
  performance — when it does report, does it produce like its neighbours

They are multiplied, not averaged. Both are evidence of a fault, so neither
should be able to cancel the other out: a panel producing at 30% of its
neighbours is a swap candidate even if it reports flawlessly, and averaging
would let its perfect reliability score drag it up into "looks fine".

Night and dim windows are excluded: when everything is near 0, ratios are
noise and would dilute a real signal into nothing.

A verdict is only as good as its sample size, so every row also carries a
confidence derived from how many usable daylight windows it was scored on.
Callers should show it — "BAD" off 3 samples is not a finding.
"""
from __future__ import annotations

import math
import sqlite3
from datetime import datetime
from pathlib import Path
from statistics import median
from zoneinfo import ZoneInfo

# Readings are compared within fixed windows of MEASUREMENT time. 15 min
# gives each optimizer ~3 reports per window at its ~4.5 min cadence, and
# matches the span energy_wh itself covers.
WINDOW_SECONDS = 900

# A window's median needs enough reporters to mean anything. Both an
# absolute floor and a share of the optimizers active in the range, so a
# window where a delivery gap left only a handful of panels isn't used to
# judge them against each other.
MIN_REPORTERS_PER_CYCLE = 3
MIN_WINDOW_COVERAGE = 0.5

# Absolute floor on the window's median snapshot power, mainly so a range
# containing only night can't calibrate its own "peak" down to a few watts
# and start scoring darkness. In watts because energy_wh has no reliable
# absolute scale.
MIN_SITE_MEDIAN_W = 20.0

# Weak light. Uniform cloud is harmless: the median falls with everything
# else and the ratios hold. Weak light is not, because ratios are divisions
# by that median: at a dim median a healthy panel slightly below it looks
# like "20% of its peers" off a meaningless absolute difference. So only
# windows where the site is producing a decent fraction of what it has
# actually been seen to produce in this range are scored. Calibrating off
# observed output keeps this correct across seasons, array sizes and
# all-day overcast.
STRONG_LIGHT_FRACTION = 0.40

# Spread between healthy panels on one roof is normal, so "full marks" is
# awarded at 90% of the peer median rather than demanding 100%.
PAR_RATIO = 0.90

# Panels are compared within their own model group. This site's two groups
# differ by 2.5x under the same sun, which against a single array-wide
# median read as ten failing panels on one side and ten stellar ones on the
# other. A group needs enough reporting members for its median to mean
# anything; below this it falls back to the array-wide median, which is
# still better than comparing against two or three peers.
MIN_GROUP_FOR_OWN_MEDIAN = 5


def _parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _window_start(ts: str) -> int:
    epoch = _parse_ts(ts).timestamp()
    return int(epoch // WINDOW_SECONDS * WINDOW_SECONDS)


def _group_medians(values: dict[str, float], groups: dict[str, str]) -> dict[str, float]:
    """Median value per panel-model group for one window, plus an overall
    one under the empty-string key for optimizers whose group is too small
    or unknown to stand on its own."""
    buckets: dict[str, list[float]] = {}
    for serial, v in values.items():
        buckets.setdefault(groups.get(serial) or "", []).append(v)

    out = {"": median(values.values())}
    for group, vals in buckets.items():
        if group and len(vals) >= MIN_GROUP_FOR_OWN_MEDIAN:
            out[group] = median(vals)
    return out


def _reference_median(serial: str, groups: dict[str, str], medians: dict[str, float]) -> float:
    return medians.get(groups.get(serial) or "", medians[""])


def _build_windows(fresh_rows: list, tz: ZoneInfo) -> dict[int, dict]:
    """Groups FRESH measurements by the window they were MEASURED in.

    Keyed off ts_utc, never poll time: SolarEdge delivers readings minutes
    to hours after they were taken (5+ hours when the inverter's uploads
    fall behind), and filing them by delivery would compare midday sun with
    dusk and put the hour-of-day profile at the wrong sun position.

    Each FRESH row is a distinct measurement, so nothing is double counted.
    A panel's value for the window is the MEAN of its reports in it, not the
    sum, so one extra report in a window doesn't read as extra output.
    """
    windows: dict[int, dict] = {}
    for r in fresh_rows:
        windows.setdefault(_window_start(r["ts_utc"]), {"rows": []})["rows"].append(r)

    for start, w in windows.items():
        energy: dict[str, list[float]] = {}
        for r in w["rows"]:
            if r["energy_wh"] is not None:
                energy.setdefault(r["optimizer_serial"], []).append(r["energy_wh"])
        w["panel_energy"] = {s: sum(v) / len(v) for s, v in energy.items()}
        w["median_energy"] = median(w["panel_energy"].values()) if energy else 0.0
        powers = [r["power"] for r in w["rows"] if r["power"] is not None]
        w["median_power"] = median(powers) if powers else 0.0
        w["hour"] = datetime.fromtimestamp(start, tz).hour
    return windows


def _select_windows(windows: dict[int, dict], active: int, hour_of_day: int | None) -> dict:
    """Decides which windows are fair to compare optimizers across, and is
    the single definition of that used by every metric here — scores,
    reliability, value stats and the hourly profile alike. Metrics computed
    over different sample sets have bitten this project twice already.

    When hour_of_day is set the light threshold is calibrated from that hour
    alone, otherwise picking 8am would compare morning light against midday
    peak and discard the entire hour.
    """
    required = max(MIN_REPORTERS_PER_CYCLE, math.ceil(MIN_WINDOW_COVERAGE * active))

    candidates: dict[int, dict] = {}
    excluded_sparse = 0
    for start, w in windows.items():
        if hour_of_day is not None and w["hour"] != hour_of_day:
            continue
        if len(w["panel_energy"]) < required:
            excluded_sparse += 1
            continue
        candidates[start] = w

    threshold = 0.0
    if candidates:
        ordered = sorted(w["median_energy"] for w in candidates.values())
        peak = ordered[int(0.9 * (len(ordered) - 1))]
        threshold = STRONG_LIGHT_FRACTION * peak

    scored: list[int] = []
    excluded_weak_light = 0
    for start in sorted(candidates):
        w = candidates[start]
        if (w["median_power"] < MIN_SITE_MEDIAN_W or w["median_energy"] <= 0
                or w["median_energy"] < threshold):
            excluded_weak_light += 1
            continue
        scored.append(start)

    return {
        "scored": scored,
        "excluded_weak_light": excluded_weak_light,
        "excluded_sparse": excluded_sparse,
    }


def _cycle_windows(all_rows: list) -> dict[int, int]:
    """Which measurement window each poll cycle observed, from the median
    ts_utc of its stamped readings — so reliability is counted over exactly
    the daylight windows being scored, even for cycles that fetched a
    backlog hours after it was measured."""
    stamps: dict[int, list[float]] = {}
    for r in all_rows:
        if r["ts_utc"]:
            stamps.setdefault(r["cycle_id"], []).append(_parse_ts(r["ts_utc"]).timestamp())
    out = {}
    for cid, epochs in stamps.items():
        if len(epochs) < MIN_REPORTERS_PER_CYCLE:
            continue
        epochs.sort()
        mid = epochs[len(epochs) // 2]
        out[cid] = int(mid // WINDOW_SECONDS * WINDOW_SECONDS)
    return out


def _verdict(score: float, has_data: bool) -> str:
    if not has_data:
        return "NO DATA"
    if score >= 85:
        return "GOOD"
    if score >= 70:
        return "LIKELY GOOD"
    if score >= 50:
        return "WATCH"
    if score >= 30:
        return "SUSPECT"
    return "BAD"


def _confidence(scored_windows: int) -> str:
    # A clear day yields ~25-35 scored 15-minute windows.
    if scored_windows >= 60:
        return "HIGH"
    if scored_windows >= 20:
        return "MEDIUM"
    if scored_windows >= 6:
        return "LOW"
    return "INSUFFICIENT"


def _load(db_path: Path, hours: int, decommissioned: frozenset):
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        optimizers = [
            o for o in conn.execute(
                "SELECT serial, label, panel_model FROM optimizers").fetchall()
            if o["serial"] not in decommissioned
        ]
        # Filtered on fetched_at, never ts_utc — ts_utc is NULL on MISSING
        # rows and would silently drop them from reliability.
        all_rows = [
            r for r in conn.execute(
                """
                SELECT cycle_id, optimizer_serial, status, ts_utc, power,
                       voltage, current, energy_wh
                FROM readings
                WHERE fetched_at > strftime('%Y-%m-%dT%H:%M:%SZ','now', ?)
                """,
                (f"-{hours} hours",),
            ).fetchall()
            if r["optimizer_serial"] not in decommissioned
        ]
    finally:
        conn.close()
    fresh = [r for r in all_rows if r["status"] == "FRESH" and r["ts_utc"]]
    return optimizers, all_rows, fresh


def score_optimizers(
    db_path: Path,
    hours: int = 72,
    hour_of_day: int | None = None,
    cfg_timezone: str = "UTC",
    decommissioned: frozenset = frozenset(),
) -> dict:
    optimizers, all_rows, fresh = _load(db_path, hours, decommissioned)
    tz = ZoneInfo(cfg_timezone)
    groups = {o["serial"]: o["panel_model"] for o in optimizers}

    windows = _build_windows(fresh, tz)
    active = len({s for w in windows.values() for s in w["panel_energy"]})
    selection = _select_windows(windows, active, hour_of_day)
    scored_windows = set(selection["scored"])

    # Ratios and value stats are both accumulated here, over exactly the same
    # windows. Computing min/max over all readings instead would make every
    # optimizer's minimum ~0 W (its night value) and pull the averages around
    # with the length of the night, so neither would be comparable.
    ratios: dict[str, list[float]] = {}
    samples: dict[str, dict[str, list[float]]] = {}
    for start in selection["scored"]:
        w = windows[start]
        refs = _group_medians(w["panel_energy"], groups)
        for serial, value in w["panel_energy"].items():
            reference = _reference_median(serial, groups, refs)
            if reference > 0:
                ratios.setdefault(serial, []).append(value / reference)
        for r in w["rows"]:
            s = samples.setdefault(r["optimizer_serial"], {"power": [], "voltage": [], "current": []})
            for field in ("power", "voltage", "current"):
                if r[field] is not None:
                    s[field].append(r[field])

    # FRESH/STALE/MISSING counts are reported for the cycles that observed a
    # scored window only. At night a healthy optimizer legitimately stops
    # sending new telemetry, so counting the dark hours would dock every
    # optimizer equally for darkness.
    cycle_window = _cycle_windows(all_rows)
    reliability_cycles = {c for c, w in cycle_window.items() if w in scored_windows}
    counts: dict[str, dict[str, int]] = {}
    for r in all_rows:
        if r["cycle_id"] in reliability_cycles:
            d = counts.setdefault(r["optimizer_serial"], {})
            d[r["status"]] = d.get(r["status"], 0) + 1

    # Reliability = share of scored daylight windows the optimizer reported
    # in at all, relative to the median its peers achieve. Not "FRESH per
    # poll": each optimizer has its own reporting rhythm (3.1 to 5.9 min on
    # this site, with zero MISSING ever), so a per-poll fresh rate docked
    # healthy slow reporters by up to ~30%. Any healthy rhythm lands in every
    # 15-minute window; only a real silence costs points. Peer-relative so
    # a delivery gap that hits everyone can't read as everyone failing.
    coverage = {}
    if selection["scored"]:
        reported: dict[str, int] = {}
        for start in selection["scored"]:
            for serial in {r["optimizer_serial"] for r in windows[start]["rows"]}:
                reported[serial] = reported.get(serial, 0) + 1
        coverage = {s: n / len(selection["scored"]) for s, n in reported.items()}
    positive = [r for r in coverage.values() if r > 0]
    achievable = median(positive) if positive else 0.0
    has_any_fresh = {r["optimizer_serial"] for r in fresh}

    results = []
    for opt in optimizers:
        serial = opt["serial"]
        c = counts.get(serial, {})
        fresh_n = c.get("FRESH", 0)
        stale = c.get("STALE", 0)
        missing = c.get("MISSING", 0)
        reliability = min(1.0, coverage.get(serial, 0.0) / achievable) if achievable else 0.0

        my_ratios = ratios.get(serial, [])
        perf_ratio = median(my_ratios) if my_ratios else None
        s = samples.get(serial, {"power": [], "voltage": [], "current": []})

        def _stat(field: str, fn):
            vals = s[field]
            return fn(vals) if vals else None

        def _avg(vals):
            return sum(vals) / len(vals)

        if serial not in has_any_fresh:
            score, verdict = 0.0, _verdict(0.0, has_data=False)
        elif perf_ratio is None:
            # Reported, but never during a usable daylight window — we can
            # judge whether it talks to us, not how well it produces.
            score = 100 * reliability
            verdict = _verdict(score, has_data=True)
        else:
            perf_component = min(perf_ratio / PAR_RATIO, 1.0)
            score = 100 * perf_component * reliability
            verdict = _verdict(score, has_data=True)

        results.append({
            "serial": serial,
            "label": opt["label"],
            "panel_model": opt["panel_model"],
            "fresh_count": fresh_n,
            "stale_count": stale,
            "missing_count": missing,
            "reliability_pct": round(100 * reliability, 1),
            "perf_ratio_pct": round(100 * perf_ratio, 1) if perf_ratio is not None else None,
            "scored_samples": len(my_ratios),
            "health_score": round(score, 1),
            "verdict": verdict,
            "confidence": _confidence(len(my_ratios)),
            # All daylight-only — see the comment above the accumulation loop.
            "avg_power": _stat("power", _avg),
            "min_power": _stat("power", min),
            "max_power": _stat("power", max),
            "avg_voltage": _stat("voltage", _avg),
            "min_voltage": _stat("voltage", min),
            "max_voltage": _stat("voltage", max),
            "avg_current": _stat("current", _avg),
            "min_current": _stat("current", min),
            "max_current": _stat("current", max),
        })

    results.sort(key=lambda r: (r["health_score"], r["serial"]))
    return {
        "method": "energy",
        "window_minutes": WINDOW_SECONDS // 60,
        "scored_windows": len(selection["scored"]),
        "reliability_cycles": len(reliability_cycles),
        "excluded_weak_light": selection["excluded_weak_light"],
        "excluded_sparse": selection["excluded_sparse"],
        "strong_light_fraction_pct": round(100 * STRONG_LIGHT_FRACTION),
        "hour_of_day": hour_of_day,
        "excluded_decommissioned": sorted(decommissioned),
        "timezone": cfg_timezone,
        "optimizers": results,
    }


def hourly_profile(
    db_path: Path,
    hours: int = 168,
    cfg_timezone: str = "UTC",
    decommissioned: frozenset = frozenset(),
) -> dict:
    """Per-optimizer output-vs-peers broken down by hour of day — the thing
    that separates shading from a fault.

    A shaded panel dips only while the shadow is on it and recovers when the
    sun moves, so its profile has a notch. A failing panel is down by roughly
    the same proportion all day, so its profile is flat and low. Looking at a
    single hour cannot tell those apart no matter how many samples it has,
    because the confound is time of day, not sample size.
    """
    optimizers, _, fresh = _load(db_path, hours, decommissioned)
    tz = ZoneInfo(cfg_timezone)
    labels = {o["serial"]: o["label"] for o in optimizers}
    groups = {o["serial"]: o["panel_model"] for o in optimizers}

    windows = _build_windows(fresh, tz)
    active = len({s for w in windows.values() for s in w["panel_energy"]})

    # Calibrate each hour against itself. Judging 8am against midday peak
    # would throw the morning away for being dim — and the early and late
    # hours are precisely where shade shows up, so a globally-calibrated
    # profile would be blind to the thing it exists to detect. Within one
    # hour every panel still shares the same light, so the comparison holds
    # even when that hour is dimmer overall.
    buckets: dict[str, dict[int, list[float]]] = {}   # serial -> hour -> ratios
    hours_seen: set[int] = set()
    scored_total = 0
    for hour in sorted({w["hour"] for w in windows.values()}):
        selection = _select_windows(windows, active, hour)
        if not selection["scored"]:
            continue
        hours_seen.add(hour)
        scored_total += len(selection["scored"])
        for start in selection["scored"]:
            w = windows[start]
            refs = _group_medians(w["panel_energy"], groups)
            for serial, value in w["panel_energy"].items():
                reference = _reference_median(serial, groups, refs)
                if reference > 0:
                    buckets.setdefault(serial, {}).setdefault(hour, []).append(value / reference)

    profiles = []
    for serial, label in sorted(labels.items()):
        per_hour = buckets.get(serial, {})
        points = [
            {
                "hour": h,
                "ratio_pct": round(100 * median(per_hour[h]), 1),
                "samples": len(per_hour[h]),
            }
            for h in sorted(per_hour)
        ]
        ratios = [p["ratio_pct"] for p in points]
        profiles.append({
            "serial": serial,
            "label": label,
            "points": points,
            # A wide spread across hours points at shade moving over the
            # panel; a consistently low but flat profile points at the
            # hardware.
            "spread_pct": round(max(ratios) - min(ratios), 1) if len(ratios) > 1 else None,
            "min_ratio_pct": min(ratios) if ratios else None,
        })

    return {
        "timezone": cfg_timezone,
        "window_minutes": WINDOW_SECONDS // 60,
        "hours_covered": sorted(hours_seen),
        "scored_windows": scored_total,
        "optimizers": profiles,
    }
