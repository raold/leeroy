"""`memories seed`: the owner's own memories of the guild's history, as data. Every entry and
name here is invented; the owner's file stays in his private folder."""
import json
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from conftest import HUMAN, seed_test_cast
from leeroy_mind.__main__ import main
from leeroy_mind.app import acquire_instance_lock
from leeroy_mind.clock import SystemClock
from leeroy_mind.protocol import Member
from leeroy_mind.storage.db import open_db
from leeroy_mind.storage.store import Store

DATED = {"guid": 101, "character": "Arkon", "moment": "M3", "kind": "moment", "importance": 8,
         "actors": [101, 102, HUMAN], "date": "2007-06-01",
         "text": "The night the whole raid fell off the bridge and Belra kept healing anyway."}
UNDATED = {"guid": 102, "moment": "J1", "kind": "joke", "importance": 6,
           "text": "Coros still owes everyone a repair bill from the elevator."}
SUFFIXED = {"guid": 103, "moment": "M3b", "kind": "about_testy", "date": "2009-12-01",
            "actors": [HUMAN],
            "text": "Testy waited for me at the summoning stone when nobody else would."}
TEXT_WORDS = ("bridge", "elevator", "summoning", "healing")  # never printed


def data_dir(tmp_path: Path) -> Path:
    root = tmp_path / "mind-data"
    root.mkdir()
    (root / "mind.toml").write_text('[human]\nnickname = "Testy"\nreal_name = "Bill"\n'
                                    'pronouns = "he"\n', encoding="utf-8")
    s = Store(open_db(root / "mind.db"), clock=SystemClock())
    try:
        seed_test_cast(s)
        s.upsert_cast([Member(guid=HUMAN, name="Testmage", race=10, class_=8, gender=0,
                              level=83, rank=0, online=False, map=0, zone=0, spec_tab=-1)], 1.0)
    finally:
        s.close()
    return root


def seed_file(tmp_path: Path, entries, name="history.json") -> Path:
    p = tmp_path / name
    p.write_text(json.dumps(entries), encoding="utf-8")
    return p


def owner_memories(root: Path) -> dict[str, sqlite3.Row]:
    c = sqlite3.connect(f"{(root / 'mind.db').as_uri()}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    try:
        return {r["key"]: r for r in c.execute("SELECT * FROM memory WHERE source = 'owner'")}
    finally:
        c.close()


def seed(root, path, *extra):
    return main(["memories", "seed", str(path), "--data-dir", str(root), *extra])


def test_seeds_dated_undated_and_suffixed_entries(tmp_path, capsys):
    root = data_dir(tmp_path)
    assert seed(root, seed_file(tmp_path, [DATED, UNDATED, SUFFIXED])) == 0
    out = capsys.readouterr().out
    m = owner_memories(root)
    assert sorted(m) == ["owner:J1:102", "owner:M3:101", "owner:M3b:103"]  # suffix kept
    a, b, c = m["owner:M3:101"], m["owner:J1:102"], m["owner:M3b:103"]
    assert (a["kind"], b["kind"], c["kind"]) == ("story", "story", "observation")
    assert json.loads(a["evidence"]) == ["owner:M3"] and json.loads(c["evidence"]) == ["owner:M3b"]
    assert json.loads(a["actors"]) == [HUMAN, 101, 102] and json.loads(b["actors"]) == []
    assert (a["importance"], b["importance"], c["importance"]) == (8, 6, 6)
    assert a["text"] == DATED["text"] and b["text"] == UNDATED["text"]
    now = b["t_event"]  # undated: the day the history is seeded
    assert abs(now - SystemClock().now()) < 120
    days = (date(2009, 12, 31) - date(2007, 6, 1)).days
    assert a["t_event"] == now - days * 86400  # about two and a half years ago
    assert c["t_event"] == now - 30 * 86400
    for r in (a, b, c):
        assert r["t_created"] == r["t_accessed"] == r["t_event"]  # old memories, not fresh
        assert r["embedding"] is None  # the running mind embeds them
    assert "new 3" in out and not any(w in out for w in TEXT_WORDS)


def test_a_rerun_changes_nothing_and_an_edit_updates_in_place(tmp_path, capsys):
    root = data_dir(tmp_path)
    path = seed_file(tmp_path, [DATED, UNDATED, SUFFIXED])
    assert seed(root, path) == 0
    first = {k: dict(r) for k, r in owner_memories(root).items()}
    assert seed(root, path) == 0
    assert "unchanged 3" in capsys.readouterr().out
    assert {k: dict(r) for k, r in owner_memories(root).items()} == first
    edited = dict(UNDATED, text="Coros still owes everyone a repair bill, two expansions on.")
    assert seed(root, seed_file(tmp_path, [DATED, edited, SUFFIXED])) == 0
    assert "new 0, updated 1, unchanged 2" in capsys.readouterr().out
    after = owner_memories(root)
    assert after["owner:J1:102"]["id"] == first["owner:J1:102"]["id"]
    assert after["owner:J1:102"]["text"] == edited["text"]
    assert after["owner:J1:102"]["t_event"] == first["owner:J1:102"]["t_event"]  # same anchor


def test_the_dry_run_writes_nothing_and_prints_no_text(tmp_path, capsys):
    root = data_dir(tmp_path)
    before = (root / "mind.db").read_bytes()
    assert seed(root, seed_file(tmp_path, [DATED, UNDATED, SUFFIXED]), "--dry-run") == 0
    out = capsys.readouterr().out
    assert owner_memories(root) == {} and not (root / "snapshots").exists()
    assert (root / "mind.db").read_bytes() == before
    today = date.today()
    ago = (date(2009, 12, 31) - date(2007, 6, 1)).days
    assert f"    101 Arkon: 1 memory, {date.fromordinal(today.toordinal() - ago)}" in out
    assert f"    102 Belra: 1 memory, {today} .. {today}" in out
    assert "would write 3: new 3, updated 0, unchanged 0, retired 0; rejected 0" in out
    assert not any(w in out for w in TEXT_WORDS)


def test_fiction_now_sets_how_long_ago(tmp_path):
    root = data_dir(tmp_path)
    assert seed(root, seed_file(tmp_path, [DATED, UNDATED]), "--fiction-now", "2008-06-01") == 0
    m = owner_memories(root)
    assert m["owner:J1:102"]["t_event"] - m["owner:M3:101"]["t_event"] == 366 * 86400


BAD = [
    ({**UNDATED, "text": "word " * 50}, "text too long (249 > 200 characters)"),
    ({**UNDATED, "guid": 999}, "guid 999 is not on the roster"),
    ({**UNDATED, "guid": HUMAN}, f"guid {HUMAN} has no persona"),
    ({**UNDATED, "guid": "102"}, "guid must be a positive integer"),
    ({**UNDATED, "text": "Bill paid for the whole raid's repairs."},
     "text uses the owner's real name; use the nickname"),
    ({**UNDATED, "text": "BILL! carried us"}, "text uses the owner's real name"),
    ({**UNDATED, "kind": "legend"}, "kind must be one of about_human, about_testy, joke, moment"),
    ({**UNDATED, "importance": 11}, "importance must be an integer 1-10"),
    ({**UNDATED, "importance": "7"}, "importance must be an integer 1-10"),
    ({**UNDATED, "actors": [101, 4242]}, "actor 4242 is not on the roster"),
    ({**UNDATED, "actors": "101"}, "actors must be a list of guids"),
    ({**UNDATED, "date": "2010-01-05"}, "date is after --fiction-now 2009-12-31"),
    ({**UNDATED, "date": "2007-02-30"}, "date must be YYYY-MM-DD"),
    ({**UNDATED, "date": "2007/06/01"}, "date must be YYYY-MM-DD"),
    ({**UNDATED, "character": "Coros"}, "character does not match guid 102's name"),
    ({**UNDATED, "moment": "M 3"}, "moment must be an id such as M17 or M17b"),
    ({**UNDATED, "mood": "warm"}, "unknown field(s): mood"),
    ({**UNDATED, "text": "  "}, "no text"),
    ({**UNDATED, "date": "2007-01-01", "t_event": 1}, "give date or t_event, not both"),
]


@pytest.mark.parametrize("entry,why", BAD, ids=[w.split(" ")[0] + str(i)
                                                 for i, (_, w) in enumerate(BAD)])
def test_a_bad_entry_is_rejected_with_its_reason(tmp_path, capsys, entry, why):
    root = data_dir(tmp_path)
    path = seed_file(tmp_path, [DATED, entry])
    assert seed(root, path, "--dry-run") == 1
    out = capsys.readouterr().out
    line = next(x for x in out.splitlines() if x.startswith("reject entry 2"))
    assert why in line, line
    assert "rejected 1" in out and "new 1" in out
    text = entry.get("text", "")
    assert not (text.strip() and text in out) and "Bill" not in out and "BILL" not in out
    assert seed(root, path) == 1  # the real run writes the good one and reports the other
    assert sorted(owner_memories(root)) == ["owner:M3:101"]


def test_a_duplicate_moment_for_one_member_is_rejected(tmp_path, capsys):
    root = data_dir(tmp_path)
    assert seed(root, seed_file(tmp_path, [UNDATED, dict(UNDATED, importance=9)]),
                "--dry-run") == 1
    assert "reject entry 2 (guid 102, moment J1): same moment and guid as entry 1" in \
        capsys.readouterr().out


def test_a_file_that_is_not_a_list_is_refused(tmp_path, capsys):
    root = data_dir(tmp_path)
    p = tmp_path / "bad.json"
    p.write_text('{"guid": 101}', encoding="utf-8")
    assert seed(root, p, "--dry-run") == 1
    assert "expected a JSON list" in capsys.readouterr().err


def test_seeding_waits_for_the_service_to_stop(tmp_path, capsys):
    root = data_dir(tmp_path)
    with acquire_instance_lock(root):
        assert seed(root, seed_file(tmp_path, [DATED])) == 1
    assert "mind service is running" in capsys.readouterr().out
    assert owner_memories(root) == {}
    assert seed(root, seed_file(tmp_path, [DATED])) == 0
    assert len(list((root / "snapshots").glob("mind-pre-memories-*.db"))) == 1


def _fiction(m: sqlite3.Row, anchor: int) -> date:
    """The in-fiction date a stored owner memory is set at."""
    return date.fromordinal(date(2009, 12, 31).toordinal() - (anchor - m["t_event"]) // 86400)


def test_a_date_moves_into_the_year_its_text_says(tmp_path, capsys):
    """Dress rehearsal 2 (judge B, memory data): seeded memories whose text gave one year were
    dated in another, so their age contradicted their own words. The year the text says it
    happened in wins over the date; a named month takes its day nearest the old date, so the
    move is as small as it can be. A year it counts from (a "since" clause), two different
    years, a story she was not there for, and an explicit t_event stay. The run says what it
    moved, and a re-run that changes nothing says nothing."""
    root = data_dir(tmp_path)
    entries = [
        dict(DATED, moment="M5", date="2007-07-25",
             text="Our first boat race was in 2005 and I still have the trophy."),
        dict(UNDATED, moment="M6", date="2007-10-01",
             text="I caught my first big fish in April 2005, on the old pier."),
        dict(SUFFIXED, moment="J2", date=None,
             text="Belra has kept the guild bank tidy since her first week in 2005."),
        {"guid": 104, "moment": "J3", "kind": "joke", "date": "2008-07-01",
         "text": "The raid horn has sounded the same since 2005 and nobody minds."},
        {"guid": 105, "moment": "M7", "kind": "moment", "date": "2007-06-01",
         "text": "In 2005 and again in 2008 we wiped on the same bridge."},
        {"guid": 105, "moment": "M8", "kind": "moment", "t_event": 1_200_000_000,
         "text": "I joined on a dare in 2005."},
        {"guid": 101, "moment": "J4", "kind": "joke", "date": "2008-07-01",
         "text": "2006! The night the boat finally dropped for me."},
        {"guid": 102, "moment": "J5", "kind": "joke", "date": "2008-07-01",
         "text": "The bridge wipe happened in 2007, before my time, and they still talk about it."},
        {"guid": 104, "moment": "M9", "kind": "moment", "date": "2005-02-10",
         "text": "I bought my first mount in May 2005 and rode it into a tree."},
    ]
    entries[2].pop("date")
    path = seed_file(tmp_path, entries)
    assert seed(root, path, "--dry-run") == 0
    out = capsys.readouterr().out
    for line in ("redate entry 1 (guid 101, moment M5): its text says 2005, so 2005-07-25 "
                 "(was 2007-07-25)",
                 "redate entry 2 (guid 102, moment M6): its text says April 2005, so 2005-04-30 "
                 "(was 2007-10-01)",
                 "redate entry 7 (guid 101, moment J4): its text says 2006, so 2006-07-01 "
                 "(was 2008-07-01)",
                 "redate entry 9 (guid 104, moment M9): its text says May 2005, so 2005-05-01 "
                 "(was 2005-02-10)"):
        assert line in out, line
    assert out.count("redate entry") == 4 and "trophy" not in out and "dare" not in out
    assert seed(root, path) == 0
    m = owner_memories(root)
    anchor = m["owner:J3:104"]["t_event"] + (date(2009, 12, 31) - date(2008, 7, 1)).days * 86400
    assert _fiction(m["owner:M5:101"], anchor) == date(2005, 7, 25)
    assert _fiction(m["owner:M6:102"], anchor) == date(2005, 4, 30)
    assert _fiction(m["owner:M9:104"], anchor) == date(2005, 5, 1)
    assert m["owner:J2:103"]["t_event"] == anchor  # "since ... in 2005" counts from it
    assert _fiction(m["owner:M7:105"], anchor) == date(2007, 6, 1)
    assert _fiction(m["owner:J5:102"], anchor) == date(2008, 7, 1)  # heard of, not there
    assert _fiction(m["owner:J4:101"], anchor) == date(2006, 7, 1)
    assert m["owner:M8:105"]["t_event"] == 1_200_000_000
    capsys.readouterr()
    assert seed(root, path) == 0
    again = capsys.readouterr().out
    assert "unchanged 9" in again and "redate entry" not in again  # nothing moves twice


def test_a_reseed_leaves_a_retired_memory_alone(tmp_path, capsys):
    """A memory the owner or the mind retired (archived, superseded) stays retired when the
    history is seeded again; it is counted, not written."""
    root = data_dir(tmp_path)
    path = seed_file(tmp_path, [DATED, UNDATED, SUFFIXED])
    assert seed(root, path) == 0
    c = sqlite3.connect(root / "mind.db")
    c.execute("UPDATE memory SET status = 'archived' WHERE key = 'owner:J1:102'")
    c.commit()
    c.close()
    capsys.readouterr()
    assert seed(root, seed_file(tmp_path, [DATED, dict(UNDATED, importance=9), SUFFIXED]),
                "--dry-run") == 0
    assert "would write 0: new 0, updated 0, unchanged 2, retired 1" in capsys.readouterr().out
    assert seed(root, seed_file(tmp_path, [DATED, dict(UNDATED, importance=9), SUFFIXED])) == 0
    assert "wrote 0: new 0, updated 0, unchanged 2, retired 1" in capsys.readouterr().out
    j1 = owner_memories(root)["owner:J1:102"]
    assert (j1["status"], j1["importance"]) == ("archived", 6)


def test_only_the_owners_memories_cite_owner_refs_in_the_writer(tmp_path):
    """The command goes through MemoryWriter: source owner with owner: evidence only."""
    root = data_dir(tmp_path)
    assert seed(root, seed_file(tmp_path, [DATED])) == 0
    m = owner_memories(root)["owner:M3:101"]
    assert (m["source"], m["status"], m["confidence"]) == ("owner", "active", 1.0)
