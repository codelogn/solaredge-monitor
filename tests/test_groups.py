import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import groups  # noqa: E402

OPTS = [
    {"serial": f"S{n}", "label": f"Optimizer 1.0.{n} ({'REC 365' if n <= 10 else 'REC'})",
     "panel_model": "REC 365" if n <= 10 else "REC"}
    for n in list(range(1, 11)) + list(range(17, 27))
]


def test_empty_means_whole_array():
    for spec in ("", None, "  ", "array", "whole"):
        g = groups.parse(spec)
        assert g.mode == "array" and g.error is None
        assert set(groups.assign(OPTS, g).values()) == {""}


def test_solaredge_uses_the_panel_model_label():
    a = groups.assign(OPTS, groups.parse("solaredge"))
    assert a["S1"] == "REC 365" and a["S17"] == "REC"


def test_custom_groups_by_position_ranges_and_serials():
    g = groups.parse("South=1.0.1-1.0.10,1.0.18,1.0.25,1.0.26; North=1.0.17,1.0.19-1.0.24")
    assert g.mode == "custom" and g.names == ("South", "North")
    a = groups.assign(OPTS, g)
    assert a["S3"] == "South" and a["S26"] == "South"
    assert a["S21"] == "North" and a["S17"] == "North"
    # A serial works too, and unlisted panels fall back to the whole array.
    a2 = groups.assign(OPTS, groups.parse("Odd=S5"))
    assert a2["S5"] == "Odd" and a2["S6"] == ""


def test_bad_spec_falls_back_with_a_reason():
    for spec in ("South 1.0.1", "=1.0.1", "A=1.0.1; B=1.0.1", "A=1.0.9-1.0.2"):
        g = groups.parse(spec)
        assert g.mode == "array" and g.error, spec


def test_signature_changes_with_the_grouping():
    a = groups.parse("")
    b = groups.parse("solaredge")
    c = groups.parse("X=1.0.1-1.0.5")
    assert len({a.signature(), b.signature(), c.signature()}) == 3
    assert c.signature() == groups.parse("X=1.0.1-1.0.5").signature()
