"""[lab] in mind.toml: off by default, an unknown key or a bad value reads as off."""
import pytest

from leeroy_mind.config import load_config
from leeroy_mind.lab.config import LabCfg, load, parse


def test_defaults_and_values(tmp_path):
    assert load(tmp_path / "absent.toml") == LabCfg()
    assert LabCfg().enabled is False and LabCfg().minutes == 40 and LabCfg().variants == 3
    p = tmp_path / "mind.toml"
    p.write_bytes(b"[lab]\nenabled = true\nminutes = 20\npairs = 4\njudge = 9\nvariants = 0\n")
    assert load(p) == LabCfg(True, 20, 4, 4, 0, False)  # judge never more than pairs
    load_config(p)  # the core loader knows the table
    # the lab only measures until told otherwise, and grades with the rubric until the judge
    # agrees with a strong grader
    assert LabCfg().grader == "rubric" and LabCfg().promote is False
    p.write_bytes(b'[lab]\nenabled = true\ngrader = "tells"\npromote = true\n')
    assert load(p) == LabCfg(enabled=True, grader="tells", promote=True)
    p.write_bytes(b'[lab]\nenabled = true\ngrader = "rubric"\n')
    assert load(p) == LabCfg(enabled=True)


@pytest.mark.parametrize("table", [
    {"enabled": "yes"}, {"minutes": 4}, {"minutes": 181}, {"pairs": 1}, {"variants": 6},
    {"judge": -1}, {"enabled": True, "surprise": 1}, {"overlays": 1}, "x",
    {"enabled": True, "grader": "vibes"}, {"enabled": True, "grader": 3},
    {"enabled": True, "grader": ["tells"]}, {"enabled": True, "promote": 1},
    {"enabled": True, "promote": "yes"}])
def test_anything_odd_reads_as_off(table):
    assert parse(table) == LabCfg()


def test_the_ops_switchboard_lists_the_lab(tmp_path):
    import time
    from leeroy_mind.console import opsview
    for text, state in (("", "off"), ("[lab]\nenabled = true\n", "on"),
                        ("[lab]\nenabled = true\nminutes = 30\n", "on"),
                        ("[lab]\nenabled = false\nminutes = 40\n", "off"),
                        ("[lab]\nenabled = 1\n", "invalid"), ("[lab]\nminutes = 4\n", "invalid"),
                        ("[lab]\nenabled = true\nsurprise = 1\n", "invalid")):
        (tmp_path / "mind.toml").write_text(text)
        rows = opsview.switches_view(tmp_path, time.monotonic() + 10, mind_started=None)["rows"]
        assert next(r for r in rows if r["table"] == "lab")["state"] == state, text
