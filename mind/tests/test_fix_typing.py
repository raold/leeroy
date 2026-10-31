"""`personas fix-typing`: typing maps turned the right way round (full form -> chat form), emoji
dropped, and old seed names left in free text replaced (rehearsal 1, 2026-10-30)."""
import sqlite3

import pytest

from conftest import _base_persona_dict
from leeroy_mind.__main__ import main
from leeroy_mind.clock import SystemClock
from leeroy_mind.human import HumanIdentity
from leeroy_mind.personas.io import canonical_json, load_persona
from leeroy_mind.personas.schema import persona_from_dict, validate_persona
from leeroy_mind.personas.seed import Assignment, Sketch, _table
from leeroy_mind.personas.typing_fix import (fix_abbreviations, fix_emoticons, is_chat_form,
                                             plan_fix_typing, repair_typing, seed_names)
from leeroy_mind.protocol import Member
from leeroy_mind.storage.db import open_db
from leeroy_mind.storage.store import Store

HUMAN = 1


@pytest.mark.parametrize("key,value,want", [
    ("you", "u", ("you", "u")),
    ("laughing out loud", "lol", ("laughing out loud", "lol")),
    ("heal", "h", ("heal", "h")),
    ("lol", "laughing out loud", ("laughing out loud", "lol")),
    ("brb", "be right back", ("be right back", "brb")),
    ("dps", "damage per second", ("damage per second", "dps")),
    ("idk", "i don't know", ("i don't know", "idk")),
    ("nvm", "nevermind", ("nevermind", "nvm")),
    ("crit", "critical strike", ("critical strike", "crit")),
    ("wtf", "what the f***", ("what the f***", "wtf")),
    ("3s", "3v3s", ("3v3s", "3s")),
    ("AoE", "Area of Effect", ("Area of Effect", "AoE")),
    ("thx", "thanks", ("thanks", "thx")),
    ("plz", "please", ("please", "plz")),
    ("k", "okay", ("okay", "k")),
    ("innit", "isn't it", ("isn't it", "innit")),
    ("ok", "ok", None),                          # no change
    ("cd", "CD", None),
    ("fire", "fire of doom", None),              # one word of a phrase
    ("lol", "only when nervous", None),          # a usage note, not a meaning
    ("brb", "sometimes", None),
    ("brb", "break", None),
    ("aggro", "threat", None),                   # a synonym
    ("tank", "prot warrior", None),
    ("sup", "hello", None),
    ("thanks", "cheers", None),                  # not shorter
])
def test_each_entry_ends_full_form_to_chat_form_or_goes(key, value, want):
    fixed, changes = fix_abbreviations({key: value})
    assert fixed == (dict([want]) if want else {})
    assert bool(changes) == (want != (key, value))


def test_chat_forms_must_be_shorter_and_start_where_the_words_start():
    assert is_chat_form("be right back", "brb") and not is_chat_form("brb", "be right back")
    assert is_chat_form("away from keyboard", "afk") and not is_chat_form("away", "afk")
    assert not is_chat_form("rarely used, only if", "lol")  # every piece starts a word


def test_non_ascii_goes_and_a_repeated_full_form_keeps_the_first():
    fixed, changes = fix_abbreviations({"\u2615": "coffee", "lol": "laughing out loud",
                                        "LOL": "laughing out loud", "brb\u2728": "be right back"})
    assert fixed == {"laughing out loud": "lol", "be right back": "brb"}
    assert [c.kind for c in changes] == ["abbreviation dropped", "abbreviation turned round",
                                         "abbreviation dropped", "abbreviation turned round"]
    assert all(c.text.isascii() for c in changes)


def test_emoticons_keep_plain_text_only():
    kept, changes = fix_emoticons([":)", "\u2615", "xD\u2728", ":)", " ", "<3"])
    assert kept == [":)", "xD", "<3"]
    assert [c.kind for c in changes] == ["emoticon dropped", "emoticon cleaned",
                                         "emoticon dropped", "emoticon dropped"]


def test_a_raw_record_is_repaired_in_place_and_a_malformed_one_left_for_the_parser():
    t = {"abbreviations": {"lol": "laughing out loud"}, "emoticons": ["\u2615", ":P"]}
    assert len(repair_typing(t)) == 2
    assert t == {"abbreviations": {"laughing out loud": "lol"}, "emoticons": [":P"]}
    odd = {"abbreviations": {"lol": 3}, "emoticons": "xD"}
    assert repair_typing(odd) == [] and odd == {"abbreviations": {"lol": 3}, "emoticons": "xD"}


# -- old seed names ------------------------------------------------------------------------------

def _m(guid, name, level=80, rank=3):
    return Member(guid=guid, name=name, race=2, class_=1, gender=0, level=level, rank=rank,
                  online=False, map=0, zone=0, spec_tab=-1)


ROSTER = {1: _m(1, "Testmage", 83, 0), 101: _m(101, "Arkon"), 102: _m(102, "Belra"),
          103: _m(103, "Coros"), 104: _m(104, "Dunna")}


def _sketch(i, name):
    return Sketch(index=i, name=name, real_name="X", age=30, race="Orc", spec="Arms",
                  class_name="Warrior", gender="male", region="UK", tags=(), group="Raiders",
                  body="")


def _log(pairs):
    """A seed log as `personas seed` writes it: a progress line, then the assignment table."""
    sketches = [_sketch(i, old) for i, (old, _) in enumerate(pairs, 1)]
    a = Assignment({i: g for i, (_, g) in enumerate(pairs, 1)}, {}, [])
    return "\n".join(["human: Testmage (guid 1)", "converting 1/4",
                      *_table(sketches, a, ROSTER), "created 4, skipped 0, failed 0"])


def test_seed_names_come_from_the_logs_table_and_the_current_roster():
    text = _log([("Quillo", 101), ("Mirel", 102), ("Arkon", 104), ("Sarn", 103)])
    roster = {**ROSTER, 103: _m(103, "Corosnew")}  # renamed since the seed ran
    names, notes = seed_names([text], roster, human=HumanIdentity(1, "Testmage", "Testy", "", True))
    assert names == {"Quillo": "Arkon", "Mirel": "Belra", "Sarn": "Corosnew"}
    assert notes == ["seed name 'Arkon': skipped, it is also the name of someone in the guild"]


def test_a_seed_name_the_logs_disagree_on_or_that_is_a_real_name_is_skipped():
    a, b = _log([("Quillo", 101)]), _log([("Quillo", 102), ("Testy", 103), ("Pella", 104)])
    d = _base_persona_dict(101, "Arkon")
    d["identity"]["real_name"] = "Pella Stone"
    p = persona_from_dict(d)
    names, notes = seed_names([a, b], ROSTER, human=HumanIdentity(1, "Testmage", "Testy", "", True),
                              personas=[p])
    assert names == {}
    assert notes == ["seed name 'Pella': skipped, it is also the name of someone in the guild",
                     "seed name 'Quillo': skipped, the logs map it to 2 members",
                     "seed name 'Testy': skipped, it is also the name of someone in the guild"]


def _typed(guid, name, *, abbreviations=None, emoticons=None, lines=(), secret=None):
    d = _base_persona_dict(guid, name)
    if abbreviations is not None:
        d["typing"]["abbreviations"] = abbreviations
    if emoticons is not None:
        d["typing"]["emoticons"] = emoticons
    if lines:
        d["sample_lines"] = [{"text": t, "approved": True} for t in lines]
    if secret is not None:
        d["secret"] = secret
    return persona_from_dict(d)


def test_the_plan_fixes_typing_and_seed_names_but_never_name_fields():
    d = _base_persona_dict(102, "Belra")
    d["identity"]["real_name"] = "Quillo Brand"  # a name field keeps its value
    d["bits"][0]["id"] = "quillo"
    d["sample_lines"] = [{"text": "quillo pls, QUILLO, Quillo's turn", "approved": True}]
    d["relationships"] = [{"other": "Arkon", "kind": "friend", "note": "Quillo raids with me"}]
    d["typing"]["abbreviations"] = {"lol": "laughing out loud"}
    plan = plan_fix_typing([persona_from_dict(d), _typed(103, "Coros")], {"Quillo": "Arkon"})
    assert [f.guid for f in plan] == [102]  # Coros has nothing to fix
    fixed = plan[0].persona
    assert plan[0].errors == [] and validate_persona(fixed) == []
    assert fixed.sample_lines[0].text == "arkon pls, ARKON, Arkon's turn"
    assert fixed.relationships[0].note == "Arkon raids with me"
    assert fixed.identity.real_name == "Quillo Brand" and fixed.bits[0].id == "quillo"
    assert fixed.typing.abbreviations == {"laughing out loud": "lol"}
    kinds = [c.kind for c in plan[0].changes]
    assert kinds.count("seed name") == 4 and kinds.count("abbreviation turned round") == 1


def test_a_fixed_persona_that_still_fails_validation_is_reported_not_saved():
    d = _base_persona_dict(104, "Dunna")
    d["typing"]["abbreviations"] = {"lol": "laughing out loud"}
    d["identity"]["age"] = 17
    plan = plan_fix_typing([_typed(103, "Coros", abbreviations={"lol": "laughing out loud"}),
                            persona_from_dict(d)], {})
    assert [f.errors for f in plan] == [[], ["identity.age: must be at least 18"]]


# -- the CLI ---------------------------------------------------------------------------------------

def _data_dir(tmp_path):
    d = tmp_path / "data"
    (d / "logs").mkdir(parents=True)
    (d / "logs" / "seed-20261030.log").write_text(_log([("Quillo", 101), ("Mirel", 102)]),
                                                  encoding="utf-8")
    s = Store(open_db(d / "mind.db"), clock=SystemClock())
    s.upsert_cast(list(ROSTER.values()), 1000.0)
    personas = [
        _typed(101, "Arkon", abbreviations={"lol": "laughing out loud", "u": "you"},
               emoticons=[":)", "\u2615"], lines=("ask mirel, she pulled",)),
        _typed(102, "Belra", lines=("ok", "quillo is late again")),
        _typed(103, "Coros"),
    ]
    for p in personas:  # as the old code stored them: save_persona would refuse them now
        s.put_persona(p.guid, canonical_json(p), "card", 1, "seed", 1000.0)
    s.close()
    return d


def _versions(d):
    conn = sqlite3.connect(d / "mind.db")
    try:
        return dict(conn.execute("SELECT guid, version FROM persona_core").fetchall())
    finally:
        conn.close()


def test_cli_dry_run_prints_every_change_and_writes_nothing(tmp_path, capsys):
    d = _data_dir(tmp_path)
    before = (d / "mind.db").read_bytes()
    assert main(["personas", "fix-typing", "--dry-run", "--data-dir", str(d)]) == 0
    out = capsys.readouterr().out
    assert "seed names from 1 log: 2 to replace: Mirel -> Belra, Quillo -> Arkon" in out
    assert "    101  Arkon" in out and "    102  Belra" in out and "103  Coros" not in out
    assert ("abbreviation 'lol' -> 'laughing out loud': turned round to 'laughing out loud' -> "
            "'lol'") in out
    assert "emoticon '\\u2615': dropped, not plain text" in out
    assert "seed name 'mirel' -> 'belra' in sample_lines[0].text" in out
    assert ("would change 2 personas: 2 abbreviations turned round, 0 dropped, 0 cleaned; "
            "1 emoticons dropped, 0 cleaned; 2 seed names replaced") in out
    assert "nothing was written" in out and (d / "mind.db").read_bytes() == before


def test_cli_saves_new_versions_after_a_snapshot_and_a_rerun_changes_nothing(tmp_path, capsys):
    d = _data_dir(tmp_path)
    assert main(["personas", "--data-dir", str(d), "fix-typing"]) == 0
    out = capsys.readouterr().out
    assert "snapshot " in out and "changed 2 personas" in out
    assert len(list((d / "snapshots").glob("mind-pre-fix-typing-*.db"))) == 1
    assert _versions(d) == {101: 2, 102: 2, 103: 1}
    s = Store(open_db(d / "mind.db"), clock=SystemClock())
    try:
        arkon = load_persona(s, 101)
        assert arkon.typing.abbreviations == {"laughing out loud": "lol", "you": "u"}
        assert arkon.typing.emoticons == (":)",)
        assert arkon.sample_lines[0].text == "ask belra, she pulled"
        assert "You play Arkon" in s.get_card(101)
        row = s.conn.execute("SELECT source FROM persona_core WHERE guid = 101").fetchone()
        assert row["source"] == "owner"
    finally:
        s.close()
    assert main(["personas", "--data-dir", str(d), "fix-typing"]) == 0
    assert "changed 0 personas" in capsys.readouterr().out and _versions(d)[101] == 2


def test_cli_takes_named_seed_logs_and_refuses_to_write_while_the_mind_runs(tmp_path, capsys):
    from leeroy_mind.app import acquire_instance_lock
    d = _data_dir(tmp_path)
    other = tmp_path / "other.log"
    other.write_text(_log([("Sarn", 103)]), encoding="utf-8")
    assert main(["personas", "--data-dir", str(d), "fix-typing", "--dry-run",
                 "--seed-log", str(other)]) == 0
    assert "seed names from 1 log: 1 to replace: Sarn -> Coros" in capsys.readouterr().out
    with acquire_instance_lock(d):
        assert main(["personas", "--data-dir", str(d), "fix-typing"]) == 1
        assert "mind service is running" in capsys.readouterr().out
    assert _versions(d) == {101: 1, 102: 1, 103: 1}


def test_a_seed_name_touching_the_real_name_is_skipped_without_printing_it():
    names, notes = seed_names([_log([("Patsy", 101), ("Quillo", 102)])], ROSTER,
                              human=HumanIdentity(1, "Testmage", "Testy", "Patsy Ray", True))
    assert names == {"Quillo": "Belra"}
    assert notes == ["a seed name was skipped: it touches the owner's real name"]
