"""`personas relink-human`: the rows toward the human take what each stored persona declares
toward him (controller ruling "final I2"), and the rebalance's re-seed keeps them."""
import dataclasses
import json
import random
import sqlite3

import pytest

from conftest import _base_persona_dict
from leeroy_mind.__main__ import main
from leeroy_mind.clock import SystemClock
from leeroy_mind.human import HumanIdentity
from leeroy_mind.personas.io import load_persona, save_persona
from leeroy_mind.personas.relink import apply_relink, plan_relink
from leeroy_mind.personas.schema import persona_from_dict
from leeroy_mind.personas.seed import human_seed
from leeroy_mind.personas.stances import apply_reseeds, plan_rebalance, plan_reseeds
from leeroy_mind.protocol import Member
from leeroy_mind.storage.db import open_db
from leeroy_mind.storage.rows import Relationship
from leeroy_mind.storage.store import Store
from leeroy_mind.talk.prompt import is_close

HUMAN = 1
H = HumanIdentity(HUMAN, "Testmage", "Testy", "", True, "he")
NOW = 9000.0


def _member(guid, name, level=80, rank=3):
    return Member(guid=guid, name=name, race=2, class_=1, gender=0, level=level, rank=rank,
                  online=False, map=0, zone=0, spec_tab=-1)


def _persona(guid, name, stance, decls, gender="female"):
    d = _base_persona_dict(guid, name)
    d["identity"]["player_gender"] = gender
    d["romance"] = {"stance": stance, "style": "bold" if stance in ("drawn", "lust") else None,
                    "jealousy_mode": None}
    d["relationships"] = [{"other": o, "kind": k, "note": n} for o, k, n in decls]
    return persona_from_dict(d)


CAST = [
    _persona(101, "Arkon", "lust", [("@human", "friend", "has had his back for years")]),
    _persona(102, "Belra", "fond", [("@human", "friction", "thinks he plays favourites")]),
    _persona(103, "Coros", "none", [("@human", "stance", "rates his calls")], "male"),
    _persona(104, "Dunna", "drawn", [("Testy", "friend", "her old raid buddy")]),
    _persona(105, "Ebbo", "none", [("GM", "friend", "levels alts with him")], "male"),
    _persona(106, "Fenn", "fond", [("Arkon", "friend", "raids together")]),
    _persona(107, "Gruk", "drawn", [("@human", "stance", "a stance note"),
                                    ("@human", "friction", "a friction note")]),
]


def _seeded(store):
    """The cast as the old seeding left it: every row toward the human a plain stance seed."""
    store.upsert_cast([_member(HUMAN, "Testmage", 83, 0)]
                      + [_member(p.guid, p.character_name) for p in CAST], 1000.0)
    for p in CAST:
        save_persona(store, p, source="seed", ts=1000.0, race="Orc", class_name="Warrior")
        store.put_relationship(dataclasses.replace(human_seed(p.guid, HUMAN, p.romance.stance),
                                                   last_seen=1000))
    return {p.guid: p for p in CAST}


def _by_guid(plan):
    return {r.guid: r for r in plan}


def test_the_plan_follows_each_declaration_and_keeps_the_stance(store):
    _seeded(store)
    plan = _by_guid(plan_relink(store, CAST, H, now=NOW))
    assert {g: (r.kind, r.action) for g, r in plan.items()} == {
        101: ("friend", "write"), 102: ("friction", "write"), 103: ("stance", "write"),
        104: ("friend", "write"), 105: ("friend", "write"), 106: ("stance", "unchanged"),
        107: ("friction", "write")}
    row = plan[101].row
    assert (row.affinity, row.trust, row.familiarity, row.label) == (40, 40, 60, "friend")
    assert (row.attraction, row.romance_stage, row.notes) == (60, "notices",
                                                              "has had his back for years")
    assert row.last_seen == 1000 and is_close(row)
    assert [g for g, r in plan.items() if is_close(r.row)] == [101, 104, 105]
    assert (plan[102].row.affinity, plan[102].row.label) == (-20, "friction")
    assert (plan[103].row.affinity, plan[103].row.notes) == (10, "rates his calls")
    assert plan[107].row.notes == "a friction note"
    assert store.get_relationship(101, HUMAN) == dataclasses.replace(
        human_seed(101, HUMAN, "lust"), last_seen=1000)  # planning writes nothing


def test_apply_writes_the_rows_once_and_a_second_run_changes_nothing(store):
    _seeded(store)
    done = apply_relink(store, CAST, H, now=NOW)
    assert sorted(r.guid for r in done if r.action == "write") == [101, 102, 103, 104, 105, 107]
    assert store.get_relationship(104, HUMAN).notes == "her old raid buddy"
    assert is_close(store.get_relationship(105, HUMAN))
    again = apply_relink(store, CAST, H, now=NOW + 60)
    assert {r.action for r in again} == {"unchanged"}
    assert store.get_relationship(101, HUMAN).last_seen == 1000


def test_lived_and_hand_set_rows_are_left_alone(store):
    _seeded(store)
    store.put_relationship(dataclasses.replace(store.get_relationship(101, HUMAN),
                                               interactions=3))
    store.put_relationship(dataclasses.replace(store.get_relationship(102, HUMAN), affinity=55))
    plan = _by_guid(apply_relink(store, CAST, H, now=NOW))
    assert (plan[101].action, plan[102].action) == ("skip", "skip")
    assert "lived" in plan[101].why and "seed" in plan[102].why
    assert store.get_relationship(101, HUMAN).label == ""
    assert store.get_relationship(102, HUMAN).affinity == 55


def test_a_missing_row_is_written_with_the_time_of_the_run(store):
    _seeded(store)
    store.conn.execute("DELETE FROM relationship WHERE a = 104")
    store.conn.commit()
    plan = _by_guid(apply_relink(store, CAST, H, now=NOW))
    assert plan[104].action == "write"
    assert store.get_relationship(104, HUMAN).last_seen == int(NOW)


def test_the_rebalance_reseed_keeps_the_declaration(store):
    _seeded(store)
    apply_relink(store, CAST, H, now=NOW)
    planned = plan_reseeds(store, [(101, "lust", "drawn"), (102, "fond", "lust")], HUMAN)
    rows = {r.guid: r.row for r in planned}
    assert (rows[101].attraction, rows[101].label, rows[101].affinity) == (40, "friend", 40)
    assert rows[101].notes == "has had his back for years"
    assert (rows[102].attraction, rows[102].romance_stage, rows[102].affinity) == (
        60, "notices", -20)
    written, skipped = apply_reseeds(store, planned, HUMAN)
    assert len(written) == 2 and skipped == []
    assert store.get_relationship(102, HUMAN).notes == "thinks he plays favourites"


def test_the_rebalance_seed_decides_between_equal_candidates():
    women = [_persona(200 + i, f"Name{i}", "fond", []) for i in range(10)]
    picks = {tuple(plan_rebalance(women, random.Random(s)).lust) for s in range(10)}
    assert len(picks) > 1


# -- the CLI ------------------------------------------------------------------------------------

def _data_dir(tmp_path, *, toml=True):
    d = tmp_path / "data"
    d.mkdir()
    if toml:
        (d / "mind.toml").write_text('[human]\nnickname = "Testy"\npronouns = "he"\n',
                                     encoding="utf-8")
    s = Store(open_db(d / "mind.db"), clock=SystemClock())
    _seeded(s)
    s.close()
    return d


def _row(d, guid):
    conn = sqlite3.connect(d / "mind.db")
    try:
        return conn.execute("SELECT affinity, label, notes FROM relationship WHERE a = ? AND b = ?",
                            (guid, HUMAN)).fetchone()
    finally:
        conn.close()


def test_cli_dry_run_prints_the_table_and_writes_nothing(tmp_path, capsys):
    d = _data_dir(tmp_path)
    before = (d / "mind.db").read_bytes()
    assert main(["personas", "relink-human", "--dry-run", "--data-dir", str(d)]) == 0
    out = capsys.readouterr().out
    assert "human: Testmage (guid 1)" in out
    assert "   101  Arkon         friend    yes    write" in out
    assert "   102  Belra         friction  no     write" in out
    assert "   106  Fenn          stance    no     unchanged" in out
    assert "would write 6 rows (friend 3, friction 2, stance 1), unchanged 1, skipped 0" in out
    assert "close to him afterwards: 3 of 7" in out and "nothing was written" in out
    assert (d / "mind.db").read_bytes() == before and _row(d, 101) == (30.0, "", "")
    assert not (d / "snapshots").exists()


def test_cli_writes_after_a_snapshot_and_is_idempotent(tmp_path, capsys):
    d = _data_dir(tmp_path)
    assert main(["personas", "--data-dir", str(d), "relink-human"]) == 0
    out = capsys.readouterr().out
    assert "snapshot " in out and "wrote 6 rows" in out
    assert len(list((d / "snapshots").glob("mind-pre-relink-human-*.db"))) == 1
    assert _row(d, 101) == (40.0, "friend", "has had his back for years")
    assert main(["personas", "--data-dir", str(d), "relink-human"]) == 0
    assert "wrote 0 rows (friend 0, friction 0, stance 0), unchanged 7" in capsys.readouterr().out


def test_cli_refuses_to_write_while_the_mind_runs(tmp_path, capsys):
    from leeroy_mind.app import acquire_instance_lock
    d = _data_dir(tmp_path)
    with acquire_instance_lock(d):
        assert main(["personas", "--data-dir", str(d), "relink-human"]) == 1
        assert "mind service is running" in capsys.readouterr().out
        assert main(["personas", "--data-dir", str(d), "relink-human", "--dry-run"]) == 0
    assert _row(d, 101) == (30.0, "", "")


def test_cli_needs_the_data_dir_to_hold_a_mind(tmp_path, capsys):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert main(["personas", "--data-dir", str(empty), "relink-human", "--dry-run"]) == 1
    assert "no mind.db at" in capsys.readouterr().err and not (empty / "mind.db").exists()
    d = _data_dir(tmp_path, toml=False)
    assert main(["personas", "--data-dir", str(d), "relink-human", "--dry-run"]) == 1
    assert "no mind.toml at" in capsys.readouterr().err


def test_cli_reports_a_persona_it_cannot_read_and_carries_on(tmp_path, capsys):
    d = _data_dir(tmp_path)
    s = Store(open_db(d / "mind.db"), clock=SystemClock())
    bad = json.loads(s.get_persona_json(106))
    bad["romance"]["stance"] = "smitten"
    s.conn.execute("UPDATE persona_core SET json = ? WHERE guid = 106", (json.dumps(bad),))
    s.conn.commit()
    s.close()
    assert main(["personas", "--data-dir", str(d), "relink-human", "--dry-run"]) == 1
    out = capsys.readouterr().out
    assert "cannot read persona 106" in out and "   101  Arkon" in out


@pytest.mark.parametrize("household", ["spoils her two grand-kids",
                                       "the grand kids visit on sundays",
                                       "her grandkids live nearby"])
def test_grandchildren_however_they_are_spelled_make_the_grandmother_type(household):
    from leeroy_mind.personas.stances import none_fit_reason
    d = _base_persona_dict(201, "Quella")
    d["identity"]["player_gender"] = "female"
    d["real_life"]["household"] = household
    assert none_fit_reason(persona_from_dict(d)) == "grandchildren"


def test_cli_reads_a_relative_data_dir(tmp_path, capsys, monkeypatch):
    """`personas relink-human --dry-run --data-dir data` from the folder above it: the dry run's
    read-only URI needs an absolute path (fix wave G2 review, Minor 4)."""
    _data_dir(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert main(["personas", "relink-human", "--dry-run", "--data-dir", "data"]) == 0
    assert "nothing was written" in capsys.readouterr().out
