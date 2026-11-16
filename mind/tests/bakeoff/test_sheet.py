"""`bakeoff sheet` and `bakeoff tally`: a blind sheet with the key kept apart, names masked as the
judge masks them, and his picks scored against the key. Invented data only."""
import json

import pytest

from bakeoff.rig import CONFIG, ollama, seed
from leeroy_mind.bakeoff.cli import bakeoff_main
from leeroy_mind.bakeoff.sheet import tally


@pytest.fixture
def out(store, clock, tmp_path):
    data = seed(store, clock.now())
    llm, admin, _ = ollama(data)
    (tmp_path / "c.toml").write_text(CONFIG)
    out = tmp_path / "out"
    assert bakeoff_main(["run", "--data-dir", str(data), "--config", str(tmp_path / "c.toml"),
                         "--turns", "30", "--windows", "2", "--out", str(out)],
                        make_llm=lambda cfg: llm, make_admin=lambda cfg: admin,
                        wow_open=lambda: False, clock=clock) == 0
    return out


def test_sheet_key_and_tally_round_trip(out, capsys):
    assert bakeoff_main(["sheet", "--out", str(out), "--n", "5"]) == 0
    sheet = (out / "sheet.md").read_text(encoding="utf-8")
    key = json.loads((out / "key.json").read_text(encoding="utf-8"))
    assert sheet.startswith("# Bake-off") and "**Local only.**" in sheet
    assert sheet.count("\n## ") == 5 and "P1 answers:" in sheet
    assert "Arkon" not in sheet
    import sqlite3
    conn = sqlite3.connect(out / "results.sqlite")
    named = [json.loads(m) for (m,) in conn.execute(
        "SELECT masked FROM result WHERE candidate='terse' AND text LIKE 'Arkon%'")]
    conn.close()
    # a name is its speaker's label when that member speaks there (Arkon's own line), else [name]
    assert named and all(m in (["[name] u there"], ["[P1] u there"]) for m in named)
    assert ["[name] u there"] in named
    assert key["names"] == ["live", "polite", "terse", "terse-think"]  # no sent, no missing model
    assert len(key["turns"]) == 5
    for number, entry in key["turns"].items():
        assert sorted(entry["options"].values()) == sorted(key["names"])
        assert f"\n## {number}. " in sheet
    picks = {n: next(letter for letter, c in e["options"].items() if c == "terse")
             for n, e in key["turns"].items()}
    picks["5"] = "-"
    (out / "picks.json").write_text(json.dumps(picks))
    capsys.readouterr()
    assert bakeoff_main(["tally", "--picks", str(out / "picks.json")]) == 0
    printed = capsys.readouterr().out
    assert "4 picks over 5 turns (1 none" in printed and "terse: picked 4 of 5" in printed
    saved = json.loads((out / "tally.json").read_text())
    assert saved["candidates"][0] == {"candidate": "terse", "picked": 4, "offered": 5, "share": 0.8}


def test_tally_reads_a_list_and_counts_what_it_cannot_read():
    key = {"names": ["live", "a"], "turns": {"1": {"options": {"A": "a", "B": "live"}},
                                             "2": {"options": {"A": "live", "B": "a"}}}}
    t = tally(key, ["b", "Z"])
    assert t["picked"] == 1 and t["unanswered"] == 1
    assert {r["candidate"]: r["picked"] for r in t["candidates"]} == {"live": 1, "a": 0}
    with pytest.raises(ValueError):
        tally(key, "A")


def test_sheet_needs_a_finished_run(tmp_path, capsys):
    assert bakeoff_main(["sheet", "--out", str(tmp_path)]) == 1
    assert "run `bakeoff run` first" in capsys.readouterr().err
