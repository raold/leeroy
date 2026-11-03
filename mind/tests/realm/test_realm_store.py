import hashlib
import shutil
import sqlite3
from dataclasses import FrozenInstanceError

import pytest

from leeroy_mind.realm.store import (
    CanonKill, actor_places, canon_kills, kicked, recent_receipts, record_kick,
    record_receipt, set_actor_place,
)
from leeroy_mind.storage import db


REALM_TABLES = {
    "realm_guild", "realm_actor", "realm_raider", "realm_fact", "realm_knows",
    "realm_progress", "realm_grudge", "realm_post", "realm_line", "realm_issue",
    "realm_stance", "realm_plan", "realm_event", "realm_rsvp", "realm_calendar",
    "realm_mail", "realm_bank", "realm_arena", "realm_match", "realm_recruit",
    "realm_step", "realm_canon", "realm_canon_kill", "realm_kick", "realm_meeting",
    "realm_goal", "realm_notable", "realm_receipt", "realm_action",
    "realm_source", "realm_fact_origin", "realm_kill", "realm_announcement",
    "realm_kick_episode", "realm_memory_dependency", "realm_speech_line",
}


@pytest.fixture
def conn(tmp_path):
    connection = db.open_db(tmp_path / "realm.db")
    yield connection
    connection.close()


def _tables(conn):
    return {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'realm_%'")}


def _unrelated_schema(conn):
    return [tuple(row) for row in conn.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master "
        "WHERE name NOT LIKE 'realm_%' AND tbl_name NOT LIKE 'realm_%' ORDER BY type,name")]


def _actor(conn, guid=100, *, place="", placed=None):
    with conn:
        conn.execute(
            "INSERT INTO realm_actor(guid,guild,name,role,added,place,placed) VALUES (?,?,?,?,?,?,?)",
            (guid, 50, "Example", "ganker", 1.0, place, placed))


def _receipt(conn, command_id="r1", **overrides):
    values = dict(command_id=command_id, kind="realm_move", stage="ack", ok=True,
                  reason="", value=0, ts=100.0)
    values.update(overrides)
    return record_receipt(conn, **values)


def test_fresh_migration_adds_exact_plan_tables_and_diagnostics(conn):
    latest = db.latest_version()
    assert latest == 11
    assert _tables(conn) == REALM_TABLES
    assert conn.execute("PRAGMA user_version").fetchone()[0] == latest
    assert db.migrate(conn) == latest
    assert _tables(conn) == REALM_TABLES
    action_columns = {row[1] for row in conn.execute("PRAGMA table_info(realm_action)")}
    assert {"reserved_manifest", "native_manifest", "transport_state", "evidence", "policy"} <= action_columns


def test_migration_upgrades_only_a_disposable_v8_copy_and_preserves_old_schema_and_rows(
        tmp_path, monkeypatch):
    source = tmp_path / "v8-source.db"
    migration_files = db._migration_files
    with monkeypatch.context() as patch:
        patch.setattr(db, "_migration_files", lambda: [
            (version, path) for version, path in migration_files() if version <= 8])
        old = db.open_db(source)
        try:
            with old:
                old.execute("INSERT INTO meta(key,value) VALUES (?,?)",
                            ("realm-upgrade-sentinel", "preserve-existing-row"))
            schema = _unrelated_schema(old)
            assert old.execute("PRAGMA user_version").fetchone()[0] == 8
            assert not _tables(old)
        finally:
            old.close()
    source_hash = hashlib.sha256(source.read_bytes()).digest()
    copy = tmp_path / "upgrade-copy.db"
    shutil.copy2(source, copy)
    upgraded = db.open_db(copy)
    try:
        assert _tables(upgraded) == REALM_TABLES
        assert _unrelated_schema(upgraded) == schema
        assert upgraded.execute("SELECT value FROM meta WHERE key=?",
                                ("realm-upgrade-sentinel",)).fetchone()[0] == "preserve-existing-row"
        assert db.migrate(upgraded) == db.latest_version()
        assert _unrelated_schema(upgraded) == schema
    finally:
        upgraded.close()
    assert hashlib.sha256(source.read_bytes()).digest() == source_hash


def test_revision_two_roles_sources_and_canon_default_are_available(conn):
    _actor(conn)
    with conn:
        conn.execute("INSERT INTO realm_progress(guild,map,boss_id,difficulty,state,source,updated) "
                     "VALUES (50,631,1,0,'killed','canon',1.0)")
        conn.execute("INSERT INTO realm_canon(achievement,guid,ts,state) VALUES (1,100,1.0,'planned')")
    assert conn.execute("SELECT tries FROM realm_canon").fetchone()[0] == 0
    with pytest.raises(sqlite3.IntegrityError), conn:
        conn.execute("INSERT INTO realm_actor(guid,guild,name,role,added) "
                     "VALUES (101,50,'Example','invented',1.0)")


@pytest.mark.parametrize("table,columns,values", [
    ("realm_fact", "kind,text,source,ts", ("test", "x" * 201, "rule", 1.0)),
    ("realm_post", "ts,author,author_kind,thread,text", (1.0, 100, "actor", "test", "x" * 401)),
    ("realm_stance", "issue,guid,value,anchor,stubborn,updated", (1, 100, 1.1, 0, 0, 1.0)),
    ("realm_kick", "guid,ts,rank,answer", (100, 1.0, 4, "maybe")),
    ("realm_receipt", "command_id,kind,stage,ok,reason,ts", ("r", "realm_move", "other", 1, "", 1.0)),
    ("realm_receipt", "command_id,kind,stage,ok,reason,ts", ("r", "realm_move", "ack", 2, "", 1.0)),
])
def test_schema_enforces_design_constraints(conn, table, columns, values):
    with pytest.raises(sqlite3.IntegrityError), conn:
        conn.execute(f"INSERT INTO {table}({columns}) VALUES ({','.join('?' for _ in values)})", values)


@pytest.mark.parametrize("answer", ["yes", "no"])
def test_kick_replay_refreshes_observation_but_preserves_owner_decision(conn, answer):
    record_kick(conn, 100, 10.0, 4)
    with conn:
        conn.execute("UPDATE realm_kick SET asked=11,answer=?,answered=12 WHERE guid=100", (answer,))
    record_kick(conn, 100, 20.0, 3)
    record_kick(conn, 100, 20.0, 3)
    assert kicked(conn) == {100}
    assert tuple(conn.execute("SELECT ts,rank,asked,answer,answered FROM realm_kick").fetchone()) == (
        20.0, 3, 11.0, answer, 12.0)


def test_actor_placement_never_invents_identity_and_keeps_other_columns(conn):
    with pytest.raises(ValueError, match="unknown realm actor"):
        set_actor_place(conn, 100, "harbour", 10.0)
    assert conn.execute("SELECT count(*) FROM realm_actor").fetchone()[0] == 0
    _actor(conn)
    set_actor_place(conn, 100, "harbour", 10.0)
    assert actor_places(conn) == {100: ("harbour", 10.0)}
    row = conn.execute("SELECT guild,name,role,status FROM realm_actor WHERE guid=100").fetchone()
    assert tuple(row) == (50, "Example", "ganker", "active")
    set_actor_place(conn, 100, "", 11.0)
    assert actor_places(conn) == {}


def test_actor_places_excludes_missing_or_nonfinite_timestamps(conn):
    _actor(conn, 100, place="harbour", placed=1.0)
    _actor(conn, 101, place="harbour", placed=None)
    _actor(conn, 102, place="harbour", placed=float("inf"))
    _actor(conn, 103, place="harbour", placed="bad")
    _actor(conn, 104, place="", placed=4.0)
    assert actor_places(conn) == {100: ("harbour", 1.0)}


@pytest.mark.parametrize("ts", [True, "1", None, float("inf"), float("-inf"), float("nan"), 10 ** 1000])
def test_public_writes_reject_nonfinite_or_non_numeric_timestamps_without_changes(conn, ts):
    _actor(conn)
    for write in (
        lambda: record_kick(conn, 100, ts, 4),
        lambda: set_actor_place(conn, 100, "harbour", ts),
        lambda: _receipt(conn, ts=ts),
    ):
        with pytest.raises(ValueError, match="ts"):
            write()
    assert kicked(conn) == set()
    assert actor_places(conn) == {}
    assert recent_receipts(conn) == []


def test_canon_kills_have_deterministic_order_and_frozen_typed_records(conn):
    rows = [(631, 2, 1, "first", "2026-01-02"), (631, 1, 0, "first", "2026-01-01"),
            (603, 3, 0, "weekly", "2026-01-03")]
    with conn:
        conn.executemany("INSERT INTO realm_canon_kill VALUES (?,?,?,?,?)", rows)
    got = canon_kills(conn)
    assert got == [CanonKill(*row) for row in sorted(rows)]
    with pytest.raises(FrozenInstanceError):
        got[0].moment = "revised"


def test_receipts_keep_first_stage_result_and_full_unsigned_64_bit_value(conn):
    maximum = (1 << 64) - 1
    assert _receipt(conn, stage="job", ok=False, reason="first", value=maximum)
    assert not _receipt(conn, kind="other", stage="job", ok=True, reason="changed", value=1, ts=200)
    assert _receipt(conn, stage="ack")
    ack, job = recent_receipts(conn)
    assert ack.stage == "ack" and ack.ok is True and ack.value == 0
    assert (job.command_id, job.kind, job.stage, job.ok, job.reason, job.value, job.ts) == (
        "r1", "realm_move", "job", False, "first", maximum, 100.0)
    stored = conn.execute("SELECT value,typeof(value) FROM realm_receipt WHERE stage='job'").fetchone()
    assert tuple(stored) == (str(maximum), "text")
    assert recent_receipts(conn, failures_only=True) == [job]
    with pytest.raises(FrozenInstanceError):
        job.value = 0


@pytest.mark.parametrize("value", [0, (1 << 63), (1 << 64) - 1])
def test_receipt_uint64_boundaries_round_trip_exactly(conn, value):
    _receipt(conn, value=value)
    assert recent_receipts(conn)[0].value == value


@pytest.mark.parametrize("field,value", [
    ("command_id", ""), ("command_id", 1), ("kind", ""), ("kind", None),
    ("stage", "other"), ("stage", True), ("ok", 1), ("ok", "false"),
    ("reason", None), ("value", True), ("value", -1), ("value", 1.0),
    ("value", "1"), ("value", 1 << 64),
])
def test_receipt_validation_rejects_wrong_types_and_ranges_before_writing(conn, field, value):
    with pytest.raises(ValueError, match=field):
        _receipt(conn, **{field: value})
    assert recent_receipts(conn) == []


def test_receipts_retain_newest_256_in_insert_order_without_refreshing_duplicate(conn):
    for index in range(300):
        _receipt(conn, f"r{index}", ts=1000 - index, ok=index % 2 == 0)
    got = recent_receipts(conn, limit=256)
    assert len(got) == conn.execute("SELECT count(*) FROM realm_receipt").fetchone()[0] == 256
    assert [row.command_id for row in got] == [f"r{index}" for index in range(299, 43, -1)]
    assert not _receipt(conn, "r44", reason="duplicate", ts=9999)
    assert recent_receipts(conn, limit=256)[-1] == got[-1]
    assert len(recent_receipts(conn)) == 20
    assert [row.command_id for row in recent_receipts(conn, failures_only=True, limit=3)] == [
        "r299", "r297", "r295"]


@pytest.mark.parametrize("limit", [0, 257, True, 1.0, "20"])
def test_receipt_reads_require_bounded_exact_integer_limits(conn, limit):
    with pytest.raises(ValueError, match="limit"):
        recent_receipts(conn, limit=limit)


def test_failure_filter_requires_exact_boolean(conn):
    with pytest.raises(ValueError, match="failures_only"):
        recent_receipts(conn, failures_only=1)


def test_public_writes_commit_with_canonical_always_open_transaction(conn):
    assert conn.autocommit is False and conn.in_transaction
    _actor(conn)
    record_kick(conn, 100, 1.0, 4)
    set_actor_place(conn, 100, "harbour", 2.0)
    _receipt(conn)
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    reader = sqlite3.connect(path)
    try:
        assert reader.execute("SELECT count(*) FROM realm_kick").fetchone()[0] == 1
        assert reader.execute("SELECT place,placed FROM realm_actor").fetchone() == ("harbour", 2.0)
        assert reader.execute("SELECT count(*) FROM realm_receipt").fetchone()[0] == 1
    finally:
        reader.close()


def test_receipt_insert_and_retention_rollback_together_on_failure(conn):
    for index in range(256):
        _receipt(conn, f"r{index}")
    with conn:
        conn.execute("CREATE TRIGGER reject_receipt_prune BEFORE DELETE ON realm_receipt "
                     "BEGIN SELECT RAISE(ABORT,'test prune failure'); END")
    before = recent_receipts(conn, limit=256)
    with pytest.raises(sqlite3.IntegrityError, match="test prune failure"):
        _receipt(conn, "r-new")
    assert recent_receipts(conn, limit=256) == before
