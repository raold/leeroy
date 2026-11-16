"""`bakeoff sheet` and `bakeoff tally`: a blind sheet with the key kept apart, the three baselines
among the options (alike replies shown once), names masked as the judge masks them, and his 0-3
ratings and picks scored against the key. Invented data only."""
import json
import sqlite3

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
    assert sheet.startswith("# Bake-off") and "**Local only.**" in sheet and "rate every reply 0-3" in sheet
    assert sheet.count("\n## ") == 5 and "P1 answers:" in sheet
    assert "Arkon" not in sheet
    conn = sqlite3.connect(out / "results.sqlite")
    named = [json.loads(m) for (m,) in conn.execute(
        "SELECT masked FROM result WHERE candidate='terse' AND text LIKE 'Arkon%'")]
    conn.close()
    # a name is its speaker's label when that member speaks there (Arkon's own line), else [name]
    assert named and all(m in (["[name] u there"], ["[P1] u there"]) for m in named)
    assert ["[name] u there"] in named
    # the baselines are options too; no missing model
    assert key["names"] == ["sent", "first", "live", "polite", "terse", "terse-think", "deep"]
    assert len(key["turns"]) == 5
    for number, entry in key["turns"].items():
        offered = sorted(c for names in entry["options"].values() for c in names)
        assert offered == sorted(key["names"]) and f"\n## {number}. " in sheet
        texts = sheet.split(f"\n## {number}. ")[1].split("\n## ")[0]
        assert texts.count("- **") == len(entry["options"])  # alike replies are one option
    picks, terse_letters = {}, {}
    for n, e in key["turns"].items():
        letter = next(letter for letter, names in e["options"].items() if "terse" in names)
        terse_letters[n] = letter
        picks[n] = {"pick": letter, "scores": {letter: 3, **{x: 0 for x in e["options"] if x != letter}}}
    picks["5"] = {"pick": "-", "scores": {terse_letters["5"]: 1, "ZZ": 2}}
    (out / "picks.json").write_text(json.dumps(picks))
    capsys.readouterr()
    assert bakeoff_main(["tally", "--picks", str(out / "picks.json")]) == 0
    printed = capsys.readouterr().out
    assert "5 turns: 4 picks, 1 none, 0 unanswered, 1 ratings or picks unreadable." in printed
    saved = json.loads((out / "tally.json").read_text())
    terse = next(r for r in saved["candidates"] if r["candidate"] == "terse")
    assert terse == {"candidate": "terse", "picked": 4, "offered": 5, "pick_rate": 0.8, "rated": 5,
                     "mean": 2.6, "good": 0.8}
    assert saved["candidates"][0]["candidate"] in ("terse", "terse-think")
    sent = next(r for r in saved["candidates"] if r["candidate"] == "sent")
    assert sent["rated"] >= 4 and "sent (baseline)" in printed


def test_tally_reads_letters_lists_and_counts_what_it_cannot_read():
    key = {"names": ["live", "first", "a"],
           "turns": {"1": {"options": {"A": ["a"], "B": ["live", "first"]}},
                     "2": {"options": {"A": ["live", "first"], "B": ["a"]}}}}
    t = tally(key, ["b", "Z"])
    assert t["picked"] == 1 and t["unreadable"] == 1
    assert {r["candidate"]: r["picked"] for r in t["candidates"]} == {"live": 1, "first": 1, "a": 0}
    t = tally(key, {"1": {"scores": {"A": 3, "B": 1}}, "2": {"scores": {"A": 2, "B": 5}}})
    means = {r["candidate"]: r["mean"] for r in t["candidates"]}
    assert means == {"a": 3.0, "live": 1.5, "first": 1.5} and t["unreadable"] == 1
    assert t["unanswered"] == 0 and t["picked"] == 0
    with pytest.raises(ValueError):
        tally(key, "A")


def test_sheet_needs_a_finished_run(tmp_path, capsys):
    assert bakeoff_main(["sheet", "--out", str(tmp_path)]) == 1
    assert "run `bakeoff run` first" in capsys.readouterr().err
