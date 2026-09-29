"""
Which panels count as each other's peers.

"vs Peers", the hourly "within group" ranks and every per-group view compare
a panel with the median of its group. What a group is comes from the
PANEL_GROUPS setting:

  (empty)    whole array — every panel compared with every other. The
             default: SolarEdge exposes nothing about roof orientation, and
             on the development site its panel-model labels ("REC" vs
             "REC 365", typed in by the installer for identical 365 W
             panels) turned out to split the array arbitrarily.
  solaredge  SolarEdge's panel-model label for each optimizer.
  custom     your own groups by layout position (or serial), e.g.
               South=1.0.1-1.0.10,1.0.18,1.0.25,1.0.26; North=1.0.17,1.0.19-1.0.24
             Positions are the "1.0.x" numbers in each optimizer's name.
             Panels not listed are compared with the whole array.

Groups smaller than analysis.MIN_GROUP_FOR_OWN_MEDIAN fall back to the
whole array (see analysis._group_medians).
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

_RANGE = re.compile(r"^(?P<prefix>(?:\d+\.)*)(?P<a>\d+)\s*-\s*(?:(?P=prefix))?(?P<b>\d+)$")


@dataclass(frozen=True)
class Grouping:
    mode: str                                   # "array" | "solaredge" | "custom"
    members: dict[str, str] = field(default_factory=dict)   # position or serial -> group
    names: tuple[str, ...] = ()
    error: str | None = None

    @property
    def label(self) -> str:
        if self.mode == "solaredge":
            return "SolarEdge panel-model labels"
        if self.mode == "custom":
            return "Your groups: " + ", ".join(self.names)
        return "Whole array"

    def signature(self) -> str:
        """Changes whenever the grouping would change any comparison."""
        raw = self.mode + "|" + ";".join(f"{k}={v}" for k, v in sorted(self.members.items()))
        return hashlib.sha1(raw.encode()).hexdigest()[:12]

    def describe(self) -> dict:
        return {"mode": self.mode, "label": self.label, "groups": list(self.names), "error": self.error}


WHOLE_ARRAY = Grouping("array")


def position(label: str | None) -> str:
    """'Optimizer 1.0.17 (REC)' -> '1.0.17'."""
    return (label or "").replace("Optimizer ", "").split(" ")[0]


def parse(spec: str | None) -> Grouping:
    """Parse PANEL_GROUPS. Never raises: a bad spec falls back to the whole
    array and carries the reason in .error, so the dashboard can show it
    rather than the poller refusing to start."""
    spec = (spec or "").strip()
    if not spec or spec.lower() in ("array", "none", "whole", "all"):
        return WHOLE_ARRAY
    if spec.lower() == "solaredge":
        return Grouping("solaredge")
    try:
        members: dict[str, str] = {}
        names: list[str] = []
        for part in filter(None, (p.strip() for p in spec.split(";"))):
            if "=" not in part:
                raise ValueError(f"'{part}' needs the form Name=positions")
            name, items = (x.strip() for x in part.split("=", 1))
            if not name:
                raise ValueError(f"a group in '{part}' has no name")
            names.append(name)
            for item in filter(None, (i.strip() for i in items.split(","))):
                for key in _expand(item):
                    if key in members and members[key] != name:
                        raise ValueError(f"{key} is in both {members[key]} and {name}")
                    members[key] = name
        if not members:
            raise ValueError("no panels listed")
        return Grouping("custom", members, tuple(names))
    except ValueError as exc:
        return Grouping("array", error=f"PANEL_GROUPS ignored ({exc}); comparing with the whole array")


def _expand(item: str) -> list[str]:
    m = _RANGE.match(item)
    if m:
        a, b = int(m["a"]), int(m["b"])
        if b < a:
            raise ValueError(f"range '{item}' runs backwards")
        return [f"{m['prefix']}{n}" for n in range(a, b + 1)]
    return [item]


def assign(optimizers, grouping: Grouping) -> dict[str, str]:
    """{serial: group name} for rows with serial/label/panel_model. Panels
    without a group map to "" and are compared with the whole array."""
    out: dict[str, str] = {}
    for o in optimizers:
        serial, label, model = o["serial"], o["label"], o["panel_model"]
        if grouping.mode == "solaredge":
            out[serial] = model or ""
        elif grouping.mode == "custom":
            out[serial] = grouping.members.get(serial) or grouping.members.get(position(label), "")
        else:
            out[serial] = ""
    return out


def from_db(conn, grouping: Grouping) -> dict[str, str]:
    """assign() for every optimizer in the database."""
    rows = [{"serial": s, "label": l, "panel_model": m}
            for s, l, m in conn.execute("SELECT serial, label, panel_model FROM optimizers")]
    return assign(rows, grouping)
