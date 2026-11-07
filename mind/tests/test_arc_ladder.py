"""The long-distance arc's ladder (Track S1): the stages, their gates, and the romance_arc
migration that rebuilds relationship for the new stages and maps the stored ones onto them."""
import json
import re
import shutil
import sqlite3
from types import SimpleNamespace

import pytest

from conftest import make_persona
from leeroy_mind.arc import ladder
from leeroy_mind.arc.ladder import (FOND_CAP, FULL_ARC, GATED, GATES, LEGACY, NOTICED,
                                    PACE_ATTITUDE, PACE_STYLE, Gate, best_stage,
                                    map_stored_stages)
from leeroy_mind.personas.io import canonical_json
from leeroy_mind.storage import db as db_mod
from leeroy_mind.storage.db import migrate, open_db
from leeroy_mind.storage.rows import ROMANCE_STAGES

NEW_TABLES = ("romance_arc", "arc_meetup")
OLD_STAGES = ("none", "notices", "banter", "whispers", "vulnerable", "invitation", "acknowledged",
              "ex")
OLD_KINDS = ("note_added", "note_dropped", "note_rolled_back", "drift_flag", "stance_band", "undo")


def _migrations(stem: str, tmp_path, *, through: bool):
    """A copy of the migrations below the one named NNNN_<stem>.sql, or up to and including it.
    Found by its stem, never by number."""
    (version,) = [v for v, f in db_mod._migration_files() if f.stem.split("_", 1)[1] == stem]
    out = tmp_path / f"migrations-{'through' if through else 'before'}-{stem}"
    out.mkdir()
    for v, f in db_mod._migration_files():
        if v < version or through and v == version:
            shutil.copy(f, out / f.name)
    return out


# Local stand-in for the shared stem helper Track A2 adds to tests/test_migrations.py; at the
# merge this file uses that one instead.
def db_at_version_before(stem: str, tmp_path) -> sqlite3.Connection:
    """mind.db built by every migration numbered below the one named NNNN_<stem>.sql, as the
    release before it left the database."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(db_mod, "MIGRATIONS_DIR", _migrations(stem, tmp_path, through=False))
        return open_db(tmp_path / "mind.db")


def migrate_through(conn, stem: str, tmp_path) -> int:
    """Migrate `conn` up to and including NNNN_<stem>.sql, and no further."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(db_mod, "MIGRATIONS_DIR", _migrations(stem, tmp_path, through=True))
        return migrate(conn)


def persona(conn, guid, stance, gender="female"):
    """A stored persona with this attitude toward him, as persona_core holds it."""
    p = make_persona(guid, f"Cast{guid}", stance=stance, player_gender=gender)
    conn.execute("INSERT INTO persona_core(guid, json, card, card_tokens, version, source, "
                 "updated) VALUES (?, ?, 'card', 1, 1, 'seed', 1)", (guid, canonical_json(p)))


def insert_rel(conn, a, b, stage, *, attraction=0.0, affinity=0.0, trust=0.0, **cols):
    row = dict(a=a, b=b, romance_stage=stage, attraction=attraction, affinity=affinity,
               trust=trust, **cols)
    conn.execute(f"INSERT INTO relationship({','.join(row)}) "
                 f"VALUES ({','.join('?' * len(row))})", tuple(row.values()))


def stage(conn, a, b=1) -> str:
    return conn.execute("SELECT romance_stage FROM relationship WHERE a=? AND b=?",
                        (a, b)).fetchone()[0]


def meta(conn, key) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def columns(conn, table) -> list[tuple]:
    return [(r[1], r[2], r[3], r[4], r[5]) for r in conn.execute(f"PRAGMA table_info({table})")]


def test_stored_stages_map_onto_the_new_ladder(tmp_path):
    conn = db_at_version_before("romance_arc", tmp_path)
    try:
        for guid, stance in ((102, "lust"), (103, "drawn"), (104, "drawn"), (105, "drawn")):
            persona(conn, guid, stance)
        insert_rel(conn, 102, 1, "vulnerable")
        insert_rel(conn, 104, 1, "invitation", attraction=100, affinity=47.6, trust=59)
        insert_rel(conn, 105, 1, "invitation", attraction=45, affinity=22, trust=12)
        insert_rel(conn, 103, 1, "acknowledged", attraction=10, affinity=5, trust=0)
        migrate(conn)
        assert stage(conn, 102) == "late_nights" and stage(conn, 104) == "planning"
        assert stage(conn, 105) == "whispers"
        assert json.loads(meta(conn, "romance_arc:mapped"))["104:1"] == ["invitation", "planning"]
        # Every remapped row is recorded, the vulnerable and acknowledged ones too (preflight
        # ruling S1); only a former invitation is lowered by its scores.
        assert stage(conn, 103) == "confession"
        assert json.loads(meta(conn, "romance_arc:mapped")) == {
            "102:1": ["vulnerable", "late_nights"], "103:1": ["acknowledged", "confession"],
            "104:1": ["invitation", "planning"], "105:1": ["invitation", "whispers"]}
    finally:
        conn.close()


def test_a_woman_off_the_full_arc_stays_at_whispers(tmp_path):
    """Controller ruling (S1 fix 1): only a drawn or lust woman goes past FOND_CAP. Her stance
    and gender come from her stored persona; a row with no persona, a man and a fond or
    none woman land on whispers whatever their old stage or scores."""
    conn = db_at_version_before("romance_arc", tmp_path)
    try:
        persona(conn, 101, "fond")
        persona(conn, 102, "drawn")
        persona(conn, 103, "lust", gender="male")
        persona(conn, 104, "none")
        persona(conn, 105, "fond")
        high = dict(attraction=99, affinity=99, trust=99)
        insert_rel(conn, 101, 1, "acknowledged", **high)
        insert_rel(conn, 102, 1, "invitation", **high)
        insert_rel(conn, 103, 1, "acknowledged", **high)
        insert_rel(conn, 104, 1, "vulnerable", **high)
        insert_rel(conn, 105, 1, "invitation", attraction=10, affinity=5, trust=0)
        insert_rel(conn, 106, 1, "vulnerable", **high)  # no persona
        insert_rel(conn, 107, 1, "banter", **high)  # not an old stage: never touched
        persona(conn, 107, "fond")
        migrate(conn)
        assert {g: stage(conn, g) for g in range(101, 108)} == {
            101: "whispers", 102: "planning", 103: "whispers", 104: "whispers",
            105: "whispers", 106: "whispers", 107: "banter"}
        assert json.loads(meta(conn, "romance_arc:mapped")) == {
            "101:1": ["acknowledged", "whispers"], "102:1": ["invitation", "planning"],
            "103:1": ["acknowledged", "whispers"], "104:1": ["vulnerable", "whispers"],
            "105:1": ["invitation", "whispers"], "106:1": ["vulnerable", "whispers"]}
    finally:
        conn.close()


def test_the_cap_never_raises_a_stage(tmp_path, monkeypatch):
    """FOND_CAP only ever lowers: an old stage whose new one sits below whispers keeps it."""
    monkeypatch.setitem(ladder.LEGACY, "vulnerable", "banter")
    conn = db_at_version_before("romance_arc", tmp_path)
    try:
        persona(conn, 101, "fond")
        insert_rel(conn, 101, 1, "vulnerable")
        insert_rel(conn, 102, 1, "acknowledged")  # no persona: capped
        migrate(conn)
        assert (stage(conn, 101), stage(conn, 102)) == ("banter", "whispers")
    finally:
        conn.close()


def test_the_mapped_record_keys_each_row_by_both_guids(tmp_path):
    """One member's rows toward two people (his guid changed once) are both recorded."""
    conn = db_at_version_before("romance_arc", tmp_path)
    try:
        persona(conn, 104, "drawn")
        insert_rel(conn, 104, 1, "invitation", attraction=99, affinity=99, trust=99)
        insert_rel(conn, 104, 2, "invitation", attraction=45, affinity=22, trust=12)
        migrate(conn)
        assert (stage(conn, 104, 1), stage(conn, 104, 2)) == ("planning", "whispers")
        assert json.loads(meta(conn, "romance_arc:mapped")) == {
            "104:1": ["invitation", "planning"], "104:2": ["invitation", "whispers"]}
    finally:
        conn.close()


def test_a_stashed_row_the_step_cannot_place_rolls_the_migration_back(tmp_path, monkeypatch):
    conn = db_at_version_before("romance_arc", tmp_path)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        persona(conn, 103, "drawn")
        insert_rel(conn, 103, 1, "acknowledged", attraction=99, affinity=99, trust=99)
        insert_rel(conn, 104, 1, "invitation", attraction=99, affinity=99, trust=99)
        conn.commit()
        monkeypatch.delitem(ladder.LEGACY, "acknowledged")  # an old stage it has no place for
        with pytest.raises(RuntimeError, match="103:1"):
            migrate(conn)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == version
        assert (stage(conn, 103), stage(conn, 104)) == ("acknowledged", "invitation")
        assert conn.execute(
            "SELECT count(*) FROM meta WHERE key LIKE 'romance_arc:%'").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM sqlite_master WHERE name IN "
                            "('romance_arc', 'arc_meetup', 'relationship_new')").fetchone()[0] == 0
    finally:
        conn.close()


@pytest.mark.parametrize("stash", [None, [[999, 1, "invitation"]], [[105, 1, "flirting"]]])
def test_the_data_step_refuses_what_cannot_happen(tmp_path, stash):
    """No stash, a stashed row that is gone, or an old stage with no place: it raises, so the
    migration's transaction rolls back with the stash and the old stages."""
    conn = open_db(tmp_path / "mind.db")
    try:
        insert_rel(conn, 105, 1, "planning", attraction=45, affinity=22, trust=12)
        if stash is not None:
            conn.execute("INSERT INTO meta(key, value) VALUES ('romance_arc:legacy', ?)",
                         (json.dumps(stash),))
        with pytest.raises(RuntimeError):
            map_stored_stages(conn)
    finally:
        conn.rollback()
        conn.close()


def test_the_rebuild_keeps_every_column_and_every_other_row(tmp_path):
    conn = db_at_version_before("romance_arc", tmp_path)
    try:
        before = columns(conn, "relationship")
        kept = dict(respect=12.5, familiarity=61.25, label="old friends", notes="likes murlocs",
                    pinned="[7, 9]", last_seen=1_700_000_000, interactions=42)
        insert_rel(conn, 101, 1, "banter", attraction=31, affinity=16, trust=6, **kept)
        insert_rel(conn, 102, 1, "ex", attraction=90, affinity=-20, trust=3)
        insert_rel(conn, 103, 104, "none", affinity=55)
        insert_rel(conn, 105, 1, "invitation", attraction=99, affinity=99, trust=99, **kept)
        persona(conn, 105, "lust")
        conn.execute("INSERT INTO meta(key, value) VALUES ('human_guid', '1')")
        snapshot = {r[0]: tuple(r) for r in conn.execute(
            "SELECT a, b, affinity, trust, respect, attraction, familiarity, label, notes, "
            "pinned, last_seen, interactions FROM relationship")}
        migrate(conn)
        assert columns(conn, "relationship") == before
        assert {r[0]: tuple(r) for r in conn.execute(
            "SELECT a, b, affinity, trust, respect, attraction, familiarity, label, notes, "
            "pinned, last_seen, interactions FROM relationship")} == snapshot
        assert [stage(conn, 101), stage(conn, 102), stage(conn, 103, 104), stage(conn, 105)] == [
            "banter", "ex", "none", "planning"]
        assert json.loads(meta(conn, "romance_arc:mapped")) == {"105:1": ["invitation", "planning"]}
        # No rel_delta: her pace clock is unchanged. The data step's own stash is gone.
        assert conn.execute("SELECT count(*) FROM rel_delta").fetchone()[0] == 0
        assert [r[0] for r in conn.execute(
            "SELECT key FROM meta WHERE key LIKE 'romance_arc:%'")] == ["romance_arc:mapped"]
        assert meta(conn, "human_guid") == "1"
    finally:
        conn.close()


def test_a_database_with_no_old_stage_records_no_mapping(tmp_path):
    conn = db_at_version_before("romance_arc", tmp_path)
    try:
        insert_rel(conn, 101, 1, "whispers", attraction=99, affinity=99, trust=99)
        migrate(conn)
        assert stage(conn, 101) == "whispers"
        assert conn.execute(
            "SELECT count(*) FROM meta WHERE key LIKE 'romance_arc:%'").fetchone()[0] == 0
    finally:
        conn.close()


def test_the_new_check_refuses_an_old_stage(tmp_path):
    conn = open_db(tmp_path / "mind.db")
    try:
        for i, new in enumerate(ROMANCE_STAGES):
            insert_rel(conn, 200 + i, 1, new)
        for old in LEGACY:
            with pytest.raises(sqlite3.IntegrityError):
                insert_rel(conn, 300, 1, old)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE relationship SET romance_stage='invitation' WHERE a=200")
        n = conn.execute("SELECT count(*) FROM relationship").fetchone()[0]
        assert n == len(ROMANCE_STAGES)
    finally:
        conn.close()


def ledger_of_every_kind(conn):
    """A ledger row of each kind the v17 CHECK allows; the undo refers to the stance move."""
    rows = [(1, 101, 3.0, "note_added", "likes the night shift", '["chat:1"]', None, None),
            (2, 101, 4.0, "note_dropped", "likes the night shift", "[]", None, None),
            (3, 102, 5.0, "note_rolled_back", "hums in raids", "[]", None, None),
            (4, 102, 6.5, "drift_flag", "talks shorter", '["memory:7"]', 11, None),
            (5, 101, 10.5, "stance_band", "fond -> drawn: x", '["chat:1"]', None, 41.5),
            (9, 101, 11.0, "undo", "undid stance_band", "[]", 5, None)]
    assert sorted({r[3] for r in rows}) == sorted(OLD_KINDS)
    conn.executemany("INSERT INTO persona_ledger(id, guid, ts, kind, text, evidence, ref, "
                     "score_before) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)


def _norm(sql):
    """CREATE text with its layout folded, and a renamed table's quotes dropped."""
    if sql is None:
        return None
    sql = re.sub(r"\s*([(),])\s*", r"\1", re.sub(r"\s+", " ", sql)).strip()
    return re.sub(r'^CREATE TABLE "(\w+)"', r"CREATE TABLE \1", sql)


def _in(values):
    return "IN(" + ",".join(f"'{v}'" for v in values) + ")"


def _schema(conn) -> dict:
    return {(r[0], r[1]): (r[2], _norm(r[3])) for r in conn.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master")}


def test_the_rebuild_changes_only_the_two_checks_in_the_schema(tmp_path):
    """sqlite_master before and after the migration, CHECKs and indexes included: relationship
    and persona_ledger read the same but for their stage and kind lists, nothing else changes
    and only the arc's two tables are new."""
    conn = db_at_version_before("romance_arc", tmp_path)
    try:
        ledger_of_every_kind(conn)
        insert_rel(conn, 101, 1, "banter")
        rows = [tuple(r) for r in conn.execute("SELECT * FROM persona_ledger ORDER BY id")]
        before = _schema(conn)
        migrate_through(conn, "romance_arc", tmp_path)
        after = _schema(conn)
        assert set(after) - set(before) == {("table", t) for t in NEW_TABLES}
        assert set(before) <= set(after)
        assert ("index", "sqlite_autoindex_relationship_1") in after
        expected = dict(before)
        rel = before[("table", "relationship")]
        assert _in(OLD_STAGES) in rel[1]
        expected[("table", "relationship")] = (rel[0], rel[1].replace(_in(OLD_STAGES),
                                                                      _in(ROMANCE_STAGES)))
        led = before[("table", "persona_ledger")]
        assert _in(OLD_KINDS) in led[1]
        expected[("table", "persona_ledger")] = (led[0], led[1].replace(
            _in(OLD_KINDS), _in(OLD_KINDS + ("household",))))
        assert {k: v for k, v in after.items() if k in before} == expected
        assert [tuple(r) for r in conn.execute("SELECT * FROM persona_ledger ORDER BY id")] == rows
    finally:
        conn.close()


def test_the_persona_ledger_takes_a_household_row_and_keeps_its_history(tmp_path):
    """S6's household rewrite needs a ledger kind of its own; this migration rebuilds the
    ledger's kind CHECK for it and keeps every row, its id and its undo references."""
    conn = db_at_version_before("romance_arc", tmp_path)
    try:
        ledger_of_every_kind(conn)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO persona_ledger(guid, ts, kind, text) "
                         "VALUES (101, 12, 'household', 'separated')")
        before = columns(conn, "persona_ledger")
        rows = [tuple(r) for r in conn.execute("SELECT * FROM persona_ledger ORDER BY id")]
        migrate(conn)
        assert columns(conn, "persona_ledger") == before
        assert [tuple(r) for r in conn.execute("SELECT * FROM persona_ledger ORDER BY id")] == rows
        new = conn.execute("INSERT INTO persona_ledger(guid, ts, kind, text) "
                           "VALUES (101, 12, 'household', 'separated')").lastrowid
        assert new == 10  # ids carry on after the kept ones
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO persona_ledger(guid, ts, kind, text) "
                         "VALUES (101, 13, 'gossip', 'x')")
    finally:
        conn.close()


def test_the_arc_tables_start_empty_with_their_defaults_and_checks(tmp_path):
    conn = open_db(tmp_path / "mind.db")
    try:
        for t in NEW_TABLES:
            assert conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] == 0
        conn.execute("INSERT INTO romance_arc(guid, human, updated) VALUES (101, 1, 1000.5)")
        row = conn.execute("SELECT * FROM romance_arc WHERE guid=101").fetchone()
        assert dict(row) == dict(
            guid=101, human=1, plan="{}", plan_state="none", plan_at=None, planning_since=None,
            weekends=0, last_meetup=None, milestone=0, milestone_at=None, partner="none",
            ended=None, events="[]", updated=1000.5)
        for col, ok, bad in (("plan_state", ("hoped", "talked", "set", "done"), "maybe"),
                             ("partner", ("together", "separated"), "married"),
                             ("ended", ("he_ended", "found_out", "owner"), "faded")):
            for value in ok:
                conn.execute(f"UPDATE romance_arc SET {col}=? WHERE guid=101", (value,))
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(f"UPDATE romance_arc SET {col}=? WHERE guid=101", (bad,))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO romance_arc(guid, updated) VALUES (102, 1)")  # human
        conn.execute("INSERT INTO arc_meetup(absence, guid, away_id, kind, written) "
                     "VALUES (7, 101, 3, 'weekend', 1000)")
        conn.execute("INSERT INTO arc_meetup(absence, guid, away_id, kind, written) "
                     "VALUES (8, 101, 4, 'visit', 1000)")
        with pytest.raises(sqlite3.IntegrityError):  # one meetup an absence
            conn.execute("INSERT INTO arc_meetup(absence, guid, away_id, kind, written) "
                         "VALUES (7, 102, 5, 'visit', 1000)")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO arc_meetup(absence, guid, away_id, kind, written) "
                         "VALUES (9, 101, 6, 'date', 1000)")
    finally:
        conn.close()


def test_the_data_step_never_commits(tmp_path):
    conn = open_db(tmp_path / "mind.db")
    try:
        insert_rel(conn, 105, 1, "planning", attraction=45, affinity=22, trust=12)
        conn.execute("INSERT INTO meta(key, value) VALUES ('romance_arc:legacy', ?)",
                     (json.dumps([[105, 1, "invitation"]]),))
        conn.commit()
        map_stored_stages(conn)
        assert conn.in_transaction and stage(conn, 105) == "whispers"
        conn.rollback()
        assert stage(conn, 105) == "planning" and meta(conn, "romance_arc:mapped") is None
    finally:
        conn.close()


def rel(at, af, tr):
    return SimpleNamespace(attraction=at, affinity=af, trust=tr)


def test_best_stage_never_goes_below_whispers_or_past_the_ceiling():
    assert best_stage(rel(0, -50, 0), ceiling="planning") == "whispers"
    assert best_stage(rel(100, 100, 100), ceiling="planning") == "planning"
    assert best_stage(rel(100, 100, 100), ceiling="confession") == "confession"
    assert best_stage(rel(100, 100, 100), ceiling="whispers") == "whispers"
    # the highest stage whose attraction, affinity and trust all hold
    assert best_stage(rel(100, 47.6, 59), ceiling="planning") == "planning"
    assert best_stage(rel(69.9, 100, 100), ceiling="planning") == "confession"
    assert best_stage(rel(100, 100, 44), ceiling="planning") == "confession"
    assert best_stage(rel(57, 33, 34), ceiling="planning") == "voice"
    assert best_stage(rel(50, 25, 20), ceiling="planning") == "late_nights"
    assert best_stage(rel(45, 22, 12), ceiling="planning") == "whispers"
    for ceiling in ("none", "notices", "banter", "weekend", "after", "ex", "invitation"):
        with pytest.raises(ValueError):
            best_stage(rel(100, 100, 100), ceiling=ceiling)


def test_the_ladder():
    assert ROMANCE_STAGES == ("none", "notices", "banter", "whispers", "late_nights", "voice",
                              "picture", "confession", "planning", "weekend", "after", "ex")
    assert GATED == ROMANCE_STAGES[:9] and GATED[-1] == "planning"
    assert list(GATES) == list(GATED[1:])  # keyed by the stage entered, in ladder order
    assert GATES["notices"] == Gate(20, 10, 0, 1, 0) and GATES["planning"] == Gate(70, 45, 45, 3, 2)
    assert [tuple(vars(g).values()) for g in GATES.values()] == [
        (20, 10, 0, 1, 0), (30, 15, 5, 2, 0), (40, 20, 10, 2, 0), (50, 25, 20, 3, 1),
        (55, 30, 30, 3, 1), (60, 35, 35, 2, 1), (65, 40, 40, 3, 2), (70, 45, 45, 3, 2)]
    for dim in ("attraction", "affinity", "trust", "depth"):  # a higher gate holds the lower ones
        values = [getattr(g, dim) for g in GATES.values()]
        assert values == sorted(values), dim
    assert set(LEGACY.values()) <= set(GATED) and not set(LEGACY) & set(ROMANCE_STAGES)
    assert LEGACY == {"vulnerable": "late_nights", "invitation": "planning",
                      "acknowledged": "confession"}
    assert NOTICED <= set(ROMANCE_STAGES) and FOND_CAP in GATED
    assert FULL_ARC == {"drawn", "lust"} and set(PACE_ATTITUDE) == FULL_ARC | {"fond"}
    assert PACE_ATTITUDE == {"lust": 0.7, "drawn": 1.0, "fond": 2.0}
    assert PACE_STYLE == {"bold": .8, "theatrical": .8, "earnest": 1., "competitive": 1.,
                          "shy": 1.5, "guarded": 2.0}
