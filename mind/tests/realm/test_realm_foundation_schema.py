import dataclasses
import json
import os
from pathlib import Path
import sqlite3
import struct
import subprocess
import sys

import pytest

from leeroy_mind.realm import records
from leeroy_mind.storage import db


TABLES = {"realm_source", "realm_fact_origin", "realm_kill", "realm_announcement",
          "realm_kick_episode", "realm_memory_dependency", "realm_speech_line"}
H = "a" * 64
BOOT = "b" * 32
KILL = BOOT + ":4"
WINDOW = 259200000


def _insert(conn, table, values):
    return conn.execute(f"INSERT INTO {table}({','.join(values)}) VALUES ({','.join('?' for _ in values)})",
                        tuple(values.values()))


def _source(identifier=5, **overrides):
    values = dict(id=identifier, source_key=f"{identifier:064x}", protocol_version=1, event_type="guard",
        game_event_id=identifier, chat_line_id=None, row_sha256=H, server_ms=1000000, received_at=1000.0,
        guild=50, human=10, top_keys_json='["action","guid","rank","reason","ts","type","v"]')
    values.update(overrides)
    return values


def _kill(**overrides):
    values = dict(kill_id="a" * 32 + ":1", source=1, native_boot="a" * 32,
        native_epoch="18446744073709551615", guild=50, human=10, map=631, instance_id=1,
        boss_id=1, difficulty=0, size=10, heroic=0, killed_ms=1000000, verified_ms=1000001,
        published_ms=1000002, native_cast_json="[20,21]", human_present=1,
        qualified_cast_json="[20]", qualification_sha256=H, fact=None)
    values.update(overrides)
    return values


def _announcement(**overrides):
    values = dict(command_id="announce_1", kill_id=KILL, guild=50, human=10, map=631, boss_id=1,
        difficulty=0, cadence="product-tue04-america-new_york-v1", period_key="2026-11-03", first="week",
        payload_json="{}", payload_sha256=H, process_id="process", generation=0,
        connection_epoch="18446744073709551615", prepared_at=1001.0, expires_at=1030.0,
        dispatched_at=None, settled_at=None, state="prepared", safe_reason="none")
    values.update(overrides)
    return values


def _episode(state="unasked", **overrides):
    values = dict(episode_id="kick:" + f"{2:064x}", source=2, guid=100, guild=50, human=10,
        kick_ms=1000000, old_rank=4, lead=None, ask_id=None, ask_line=None, ask_text_sha256=None,
        ask_state=state, ask_expires_ms=1000000 + WINDOW, ask_process_id=None, ask_generation=None,
        ask_connection_epoch=None, attempted_ms=None, dispatched_ms=None, ack_received_ms=None,
        ask_ack_ms=None, delivered_ms=None, reply_expires_ms=None, answer_source=None,
        answer=None, answered_ms=None, consent_state="none", admission_command_id=None)
    if state in ("reserved", "delivered", "refused", "unknown"):
        values.update(lead=20, ask_id="ask_1", ask_text_sha256=H, ask_process_id="process",
                      ask_generation=0, ask_connection_epoch="1", attempted_ms=1000001)
    if state == "delivered":
        values.update(ask_line=1, dispatched_ms=1000002, ask_ack_ms=1000003, ack_received_ms=1000004,
                      delivered_ms=1000003, reply_expires_ms=1000003 + WINDOW)
    values.update(overrides)
    return values


def _row(table):
    return {
        "realm_source": _source(),
        "realm_fact_origin": dict(fact=1, producer_key=f"event:{1:064x}:verified_raid_kill_v1", source=1,
            policy="verified_raid_kill_v1", assurance="verified_event", proof_json="{}",
            qualification_json="{}", text_sha256=H, revision=1, state="active"),
        "realm_kill": _kill(),
        "realm_announcement": _announcement(),
        "realm_kick_episode": _episode(),
        "realm_memory_dependency": dict(memory=1, fact=1),
        "realm_speech_line": dict(chat_line=1, utterance=1),
    }[table]


@pytest.fixture
def conn(tmp_path):
    connection = db.open_db(tmp_path / "foundation.db")
    with connection:
        for identifier in range(1, 8):
            _insert(connection, "game_event", dict(id=identifier, ts=1000.0, type="guard", payload="{}"))
            _insert(connection, "chat_line", dict(id=identifier, ts=1000.0, channel="guild",
                from_guid=20, from_name="Example", to_guid=0, text="A line.", origin="mind"))
            _insert(connection, "utterance", dict(id=identifier, ts=1000, guid=20, channel="guild",
                kind="reply", text="A line."))
        _insert(connection, "memory", dict(id=1, owner=20, kind="event", text="An event.", t_event=1000,
            t_created=1000, t_accessed=1000, importance=5, valence=0, source="game"))
        _insert(connection, "realm_fact", dict(id=1, kind="kill", text="The guild killed a boss.",
            source="event", ts=1000.0))
        for identifier in range(1, 5):
            _insert(connection, "realm_source", _source(identifier))
        _insert(connection, "realm_kill", _kill(kill_id=KILL, source=4, native_boot=BOOT))
    yield connection
    connection.close()


def _reject(conn, table, values):
    with pytest.raises(sqlite3.IntegrityError), conn:
        _insert(conn, table, values)


@pytest.mark.parametrize("table", sorted(TABLES))
def test_required_columns_reject_null_including_exact_fk_primary_keys(conn, table):
    values = _row(table)
    required = {row[1] for row in conn.execute(f"PRAGMA table_info({table})") if row[3]}
    # realm_source.id is the one intentional row allocator.
    for name in required - ({"id"} if table == "realm_source" else set()):
        _reject(conn, table, {**values, name: None})


@pytest.mark.parametrize("table,field,value", [
    ("realm_source", "protocol_version", 2), ("realm_source", "source_key", "A" * 64),
    ("realm_source", "row_sha256", "a" * 63), ("realm_source", "source_key", H + "\0x"),
    ("realm_source", "source_key", b"a" * 64), ("realm_source", "row_sha256", b"a" * 64),
    ("realm_source", "guild", 0), ("realm_source", "human", 4294967296),
    ("realm_source", "received_at", float("inf")), ("realm_source", "server_ms", 0),
    ("realm_source", "server_ms", 253402300800000), ("realm_source", "server_ms", 1.5),
    ("realm_source", "event_type", "guard\0extra"), ("realm_source", "top_keys_json", "{}"),
    ("realm_source", "top_keys_json", "["),
    ("realm_source", "top_keys_json", json.dumps(["v"] * 65, separators=(",", ":"))),
    ("realm_source", "top_keys_json", json.dumps(["é" * 4096], ensure_ascii=False, separators=(",", ":"))),
    ("realm_fact_origin", "policy", "stored_forum_topic_v1"),
    ("realm_fact_origin", "assurance", "stored_topic"), ("realm_fact_origin", "revision", 0),
    ("realm_fact_origin", "state", "pending"), ("realm_fact_origin", "proof_json", "[]"),
    ("realm_fact_origin", "proof_json", "{bad}"),
    ("realm_fact_origin", "qualification_json", '{"x":"' + "é" * 4096 + '"}'),
    ("realm_fact_origin", "producer_key", f"event:{1:064x}:bg_result_v1"),
    ("realm_kill", "native_epoch", "01"), ("realm_kill", "native_epoch", "0"),
    ("realm_kill", "native_epoch", "18446744073709551616"), ("realm_kill", "native_boot", "A" * 32),
    ("realm_kill", "kill_id", "a" * 32 + ":01"),
    ("realm_kill", "kill_id", "a" * 32 + ":18446744073709551616"),
    ("realm_kill", "instance_id", 0), ("realm_kill", "map", -1), ("realm_kill", "size", 5),
    ("realm_kill", "heroic", 2), ("realm_kill", "human_present", 2),
    ("realm_kill", "verified_ms", 999999), ("realm_kill", "published_ms", 1000000),
    ("realm_kill", "difficulty", 2), ("realm_kill", "native_cast_json", "{}"),
    ("realm_kill", "qualified_cast_json", "[NaN]"),
    ("realm_kill", "native_cast_json", json.dumps([1] * 129, separators=(",", ":"))),
    ("realm_announcement", "command_id", "with space"), ("realm_announcement", "command_id", "id\0x"),
    ("realm_announcement", "period_key", "2026-02-30"), ("realm_announcement", "period_key", "2026-1-1"),
    ("realm_announcement", "cadence", "weekly"), ("realm_announcement", "first", "ever"),
    ("realm_announcement", "payload_json", "[]"),
    ("realm_announcement", "payload_json", '{"x":"' + "é" * 2048 + '"}'),
    ("realm_announcement", "process_id", b"process"),
    ("realm_announcement", "process_id", "é" * 129), ("realm_announcement", "generation", -1),
    ("realm_announcement", "connection_epoch", "18446744073709551616"),
    ("realm_announcement", "prepared_at", float("inf")), ("realm_announcement", "state", "retry"),
    ("realm_announcement", "safe_reason", "arbitrary_native_error"),
    ("realm_kick_episode", "episode_id", "kick:" + "A" * 64),
    ("realm_kick_episode", "old_rank", 256), ("realm_kick_episode", "ask_state", "sent"),
    ("realm_kick_episode", "ask_expires_ms", 1000000 + WINDOW + 1),
    ("realm_kick_episode", "admission_command_id", "admit_1"),
    ("realm_memory_dependency", "memory", 0), ("realm_memory_dependency", "fact", -1),
])
def test_schema_rejects_invalid_literal_bounds_hashes_json_and_cross_fields(conn, table, field, value):
    _reject(conn, table, {**_row(table), field: value})


def test_source_requires_one_existing_exact_raw_row_and_unique_envelope_key(conn):
    _reject(conn, "realm_source", _source(game_event_id=None))
    _reject(conn, "realm_source", _source(chat_line_id=1))
    _reject(conn, "realm_source", _source(game_event_id=999))
    _reject(conn, "realm_source", _source(game_event_id=None, chat_line_id=999))
    _reject(conn, "realm_source", _source(source_key=f"{1:064x}"))
    with conn:
        _insert(conn, "realm_source", _source(game_event_id=None, chat_line_id=1, event_type="channel_chat"))
    assert conn.execute("SELECT chat_line_id FROM realm_source WHERE id=5").fetchone()[0] == 1


def test_every_new_foreign_key_uses_no_action_and_retains_referenced_rows(conn):
    for table in TABLES:
        for foreign_key in conn.execute(f"PRAGMA foreign_key_list({table})"):
            assert foreign_key[5:7] == ("NO ACTION", "NO ACTION")
    for table, key in (("game_event", 1), ("realm_source", 4)):
        with pytest.raises(sqlite3.IntegrityError), conn:
            conn.execute(f"DELETE FROM {table} WHERE id=?", (key,))
    assert conn.execute("SELECT count(*) FROM game_event WHERE id=1").fetchone()[0] == 1


def test_domain_unique_source_fact_and_dependency_keys(conn):
    with conn:
        _insert(conn, "realm_fact_origin", _row("realm_fact_origin"))
        _insert(conn, "realm_memory_dependency", _row("realm_memory_dependency"))
        _insert(conn, "realm_fact", dict(id=2, kind="kill", text="Another claim.", source="event", ts=1000.0))
        _insert(conn, "realm_kill", _kill(fact=1))
    _reject(conn, "realm_fact_origin", _row("realm_fact_origin"))
    _reject(conn, "realm_fact_origin", {**_row("realm_fact_origin"), "fact": 2})
    _reject(conn, "realm_memory_dependency", _row("realm_memory_dependency"))
    _reject(conn, "realm_kill", _kill(kill_id="a" * 32 + ":2", source=4))
    _reject(conn, "realm_kill", _kill(kill_id="a" * 32 + ":2", source=2, fact=1))
    _reject(conn, "realm_kill", _kill(kill_id="a" * 32 + ":3", source=3, fact=999))
    _reject(conn, "realm_kick_episode", _episode(source=999))


@pytest.mark.parametrize("state", ["prepared", "dispatched", "sent", "refused", "unknown", "expired", "abandoned", "conflict"])
def test_all_announcement_states_permanently_consume_the_period_slot(conn, state):
    with conn:
        _insert(conn, "realm_kill", _kill())
        _insert(conn, "realm_announcement", _announcement(state=state,
            dispatched_at=1001.0 if state in ("dispatched", "sent") else None,
            settled_at=1002.0 if state == "sent" else None))
    _reject(conn, "realm_announcement", _announcement(command_id="announce_2", kill_id="a" * 32 + ":1"))
    _reject(conn, "realm_announcement", _announcement(command_id="announce_2", period_key="2026-11-10"))


@pytest.mark.parametrize("overrides", [
    {"state": "sent"}, {"state": "sent", "dispatched_at": 1001.0},
    {"state": "dispatched"}, {"state": "expired", "dispatched_at": 1001.0},
    {"state": "abandoned", "dispatched_at": 1001.0},
])
def test_announcement_state_requires_the_specified_transport_timestamps(conn, overrides):
    _reject(conn, "realm_announcement", _announcement(**overrides))


@pytest.mark.parametrize("overrides", [
    {"lead": None}, {"lead": 10}, {"lead": 100}, {"ask_id": "bad id"},
    {"ask_connection_epoch": "01"}, {"ask_connection_epoch": "1\0x"},
    {"ask_generation": -1}, {"ask_process_id": "é" * 129}, {"attempted_ms": None},
    {"dispatched_ms": 1000000}, {"dispatched_ms": 1000000 + WINDOW},
    {"ask_line": 1}, {"delivered_ms": 1000003}, {"reply_expires_ms": 1000003 + WINDOW},
    {"answer": "yes", "answer_source": 3, "answered_ms": 1000005, "consent_state": "pending_native"},
])
def test_reserved_question_rejects_partial_identity_delivery_and_unsourced_answer(conn, overrides):
    _reject(conn, "realm_kick_episode", _episode("reserved", **overrides))


@pytest.mark.parametrize("overrides", [
    {"ask_line": None}, {"ask_ack_ms": None}, {"ack_received_ms": 1000002},
    {"delivered_ms": 1000004}, {"reply_expires_ms": 1000003 + WINDOW + 1},
    {"ask_state": "unknown"}, {"ask_state": "refused"}, {"ask_line": 999},
    {"answer": "yes", "answer_source": 3, "answered_ms": 1000003 + WINDOW, "consent_state": "pending_native"},
    {"answer": "yes", "answer_source": 3, "answered_ms": 1000005, "consent_state": "declined"},
    {"answer": "no", "answer_source": 3, "answered_ms": 1000005, "consent_state": "pending_native"},
    {"answer": "yes", "answer_source": None, "answered_ms": 1000005, "consent_state": "pending_native"},
    {"consent_state": "pending_native"},
])
def test_delivery_triple_and_answer_consent_are_atomic_and_time_bounded(conn, overrides):
    _reject(conn, "realm_kick_episode", _episode("delivered", **overrides))


def test_unknown_partial_diagnostics_remain_active_without_manufacturing_delivery(conn):
    with conn:
        _insert(conn, "realm_kick_episode", _episode("unknown", dispatched_ms=1000002,
            ask_ack_ms=1000003, ack_received_ms=1000004))
    other = _episode("reserved", episode_id="kick:" + f"{3:064x}", source=3, guid=101, ask_id="ask_2")
    _reject(conn, "realm_kick_episode", other)
    row = conn.execute("SELECT ask_line,delivered_ms,reply_expires_ms,answer FROM realm_kick_episode").fetchone()
    assert tuple(row) == (None, None, None, None)


@pytest.mark.parametrize("answer,consent", [("yes", "pending_native"), ("no", "declined")])
def test_answered_delivery_is_inactive_but_keeps_its_exact_delivery_and_source(conn, answer, consent):
    with conn:
        _insert(conn, "realm_kick_episode", _episode("delivered", answer=answer, answer_source=3,
            answered_ms=1000005, consent_state=consent))
        _insert(conn, "realm_kick_episode", _episode("reserved", episode_id="kick:" + f"{3:064x}",
            source=3, guid=101, ask_id="ask_2"))
    assert conn.execute("SELECT count(*) FROM realm_kick_episode").fetchone()[0] == 2


@pytest.mark.parametrize("answer,consent", [("yes", "pending_native"), ("no", "declined")])
@pytest.mark.parametrize("offset,accepted", [(-1, False), (0, True), (WINDOW - 1, True), (WINDOW, False)])
def test_answer_receipt_must_be_in_the_half_open_delivered_reply_window(conn, answer, consent, offset, accepted):
    values = _episode("delivered", answer=answer, answer_source=3, consent_state=consent)
    values["answered_ms"] = values["delivered_ms"] + offset
    if accepted:
        with conn:
            _insert(conn, "realm_kick_episode", values)
        row = conn.execute("SELECT answer,answered_ms,consent_state FROM realm_kick_episode").fetchone()
        assert tuple(row) == (answer, values["answered_ms"], consent)
    else:
        _reject(conn, "realm_kick_episode", values)
        assert conn.execute("SELECT count(*) FROM realm_kick_episode").fetchone()[0] == 0


def test_expired_unasked_and_expired_delivered_windows_have_no_partial_triple(conn):
    with conn:
        _insert(conn, "realm_kick_episode", _episode("expired", consent_state="expired"))
        _insert(conn, "realm_kick_episode", _episode("delivered", episode_id="kick:" + f"{3:064x}",
            source=3, guid=101, ask_id="ask_2", ask_state="expired", consent_state="expired"))
    assert conn.execute("SELECT count(*) FROM realm_kick_episode WHERE ask_state='expired'").fetchone()[0] == 2


def test_speech_marker_requires_both_exact_output_ids_and_never_guesses_a_pair(conn):
    _reject(conn, "realm_speech_line", dict(utterance=1))
    _reject(conn, "realm_speech_line", dict(chat_line=None, utterance=1))
    _reject(conn, "realm_speech_line", dict(chat_line=1, utterance=None))
    _reject(conn, "realm_speech_line", dict(chat_line=999, utterance=1))
    _reject(conn, "realm_speech_line", dict(chat_line=1, utterance=999))
    with conn:
        _insert(conn, "realm_speech_line", dict(chat_line=1, utterance=1))
    _reject(conn, "realm_speech_line", dict(chat_line=1, utterance=2))
    _reject(conn, "realm_speech_line", dict(chat_line=2, utterance=1))
    for table in ("chat_line", "utterance"):
        with pytest.raises(sqlite3.IntegrityError), conn:
            conn.execute(f"DELETE FROM {table} WHERE id=1")
    # Equal text at another durable identity is not marked by similarity.
    assert conn.execute("SELECT count(*) FROM realm_speech_line WHERE chat_line=2 OR utterance=2").fetchone()[0] == 0


def _old_schema(conn):
    return [tuple(row) for row in conn.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name")]


def test_disposable_v10_upgrade_preserves_every_existing_schema_and_cap_row(tmp_path, monkeypatch):
    files = db._migration_files()
    with monkeypatch.context() as patch:
        patch.setattr(db, "_migration_files", lambda: [(n, p) for n, p in files if n <= 10])
        conn = db.open_db(tmp_path / "old-copy.db")
    try:
        with conn:
            conn.execute("INSERT INTO realm_notable VALUES ('2026-01-01',20,999999)")
            conn.execute("INSERT INTO realm_fact(id,kind,text,source,ts) VALUES (1,'old','Keep.','rule',1)")
            conn.execute("INSERT INTO realm_knows VALUES (1,50,1,'own')")
            conn.execute("INSERT INTO realm_canon_kill VALUES (631,1,0,'first','2026-01-01')")
            conn.execute("INSERT INTO realm_kick(guid,ts,rank,answer,answered) VALUES (100,1,4,'no',2)")
            conn.execute("INSERT INTO realm_action(command_id,role,policy,owner,rank,kind,payload,"
                "reserved_manifest,guild,human,state,transport_state,created,updated) "
                "VALUES ('old','root','ordinary','owner',1,'realm_move','{}','[]',50,10,'prepared','not_entered',1,1)")
        schema = _old_schema(conn)
        old_tables = {row[1] for row in schema if row[0] == "table" and row[1].startswith("realm_")}
        rows = {table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table}")] for table in old_tables}
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 10
        assert db.migrate(conn) == 11
        after = _old_schema(conn)
        assert all(entry in after for entry in schema)
        assert {row[1] for row in after if row[0] == "table"} - {row[1] for row in schema if row[0] == "table"} == TABLES
        for table in old_tables:
            assert [tuple(row) for row in conn.execute(f"SELECT * FROM {table}")] == rows[table]
        assert all(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0 for table in TABLES)
        assert db.migrate(conn) == 11
        assert tuple(conn.execute("SELECT * FROM realm_notable").fetchone()) == ("2026-01-01", 20, 999999)
    finally:
        conn.close()


def test_failed_0011_rolls_back_every_new_table_and_version(tmp_path, monkeypatch):
    files = db._migration_files()
    with monkeypatch.context() as patch:
        patch.setattr(db, "_migration_files", lambda: [(n, p) for n, p in files if n <= 10])
        conn = db.open_db(tmp_path / "failed-copy.db")
    try:
        schema = _old_schema(conn)
        migration = next(path for version, path in files if version == 11)
        broken = tmp_path / "0011_realm_foundation.sql"
        broken.write_text(migration.read_text(encoding="utf-8") + "\nCREATE TABLE realm_source(x);", encoding="utf-8")
        monkeypatch.setattr(db, "_migration_files", lambda: [(n, p) for n, p in files if n <= 10] + [(11, broken)])
        with pytest.raises(sqlite3.OperationalError):
            db.migrate(conn)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 10
        assert _old_schema(conn) == schema
    finally:
        conn.close()


def _proof():
    return records.PublicProof("bg_result_v1", "observed_event", ("event:1",), (1,),
        (records.ProofNode("game_event", "1", 1, H),), "{}", "{}", H)


def test_records_copy_mutable_inputs_into_frozen_bounded_domain_shapes():
    fields = [["bg_type", 1], ["winner", "horde"]]
    draft = records.FactDraft(f"event:{1:064x}:bg_result_v1", "bg_result_v1", 1.0, "bg_result_v1", fields)
    fields[0][1] = 2
    fields.append(["extra", True])
    assert draft.output_fields == (("bg_type", 1), ("winner", "horde"))
    with pytest.raises(dataclasses.FrozenInstanceError):
        draft.public = False
    ids = [1]
    stamp = records.RealmReadStamp(1, H, ids, H)
    ids.append(2)
    assert stamp.fact_ids == (1,)
    assert not hasattr(stamp, "__dict__")
    assert not hasattr(_proof(), "accepted")


@pytest.mark.parametrize("change", [
    {"output_fields": (("winner", "horde"), ("bg_type", 1))},
    {"output_fields": (("bg_type", 1), ("bg_type", 2))},
    {"output_fields": (("bg_type", 1.0),)}, {"output_fields": (("bad key", 1),)},
    {"output_fields": tuple((f"k{n:02}", n) for n in range(33))},
    {"source_at": True}, {"source_at": float("inf")}, {"public": 1}, {"policy": "arbitrary_v1"},
])
def test_fact_draft_rejects_mutable_or_unbounded_scalar_shape(change):
    values = dict(producer_key=f"event:{1:064x}:bg_result_v1", policy="bg_result_v1", source_at=1.0,
                  template_id="bg_result_v1", output_fields=(("bg_type", 1),))
    with pytest.raises(ValueError):
        records.FactDraft(**{**values, **change})


def test_actor_snapshot_and_read_stamps_reject_wrong_exact_numeric_types_and_duplicates():
    actor = records.ActorQualification(20, 50, "officer", "active", H)
    cast = [20]
    snapshot = records.QualificationSnapshot(50, 10, 1.0, True, cast, [actor], [60], "none", 0, 0,
                                             7, "", "none", None)
    cast.append(21)
    assert snapshot.cast == (20,) and snapshot.actors == (actor,)
    with pytest.raises(ValueError):
        records.ActorQualification(True, 50, "officer", "active", H)
    with pytest.raises(ValueError):
        records.RealmReadStamp(1, H, (1, 1), H)
    with pytest.raises(ValueError):
        records.RealmInputs(("memory:1",), (records.RealmReadStamp(1, H, (1,), H),) * 2, "memory")


@pytest.mark.parametrize("change", [
    {"transport_epoch": True}, {"transport_epoch": 0}, {"transport_epoch": 1 << 64},
    {"speaker_name": "Arkon"}, {"speaker_name_sha256": H}, {"speaker_name_source": "human_meta"},
    {"speaker_kind": "human", "speaker_guid": 10, "speaker_guild": 50},
])
def test_qualification_requires_exact_transport_and_coherent_absent_speaker(change):
    values = dict(guild=50, human=10, captured_at=1.0, human_confirmed=True, cast=(), actors=(),
        rival_guilds=(), speaker_kind="none", speaker_guid=0, speaker_guild=0,
        transport_epoch=7, speaker_name="", speaker_name_source="none", speaker_name_sha256=None)
    with pytest.raises(ValueError):
        records.QualificationSnapshot(**{**values, **change})


@pytest.mark.parametrize("name,source,digest", [
    ("é" * 24, "human_meta", H), ("Arkon", "cast_member", H),
])
def test_qualification_name_shape_does_not_claim_channel_legal_name_or_source_authority(name, source, digest):
    snapshot = records.QualificationSnapshot(50, 10, 1.0, True, (), (), (), "human", 10, 50,
                                             (1 << 64) - 1, name, source, digest)
    assert snapshot.speaker_name == name and snapshot.speaker_name_sha256 == digest
    with pytest.raises(ValueError):
        dataclasses.replace(snapshot, speaker_name="é" * 25)
    with pytest.raises(ValueError):
        dataclasses.replace(snapshot, speaker_name_source="realm_actor")
    with pytest.raises(ValueError):
        dataclasses.replace(snapshot, speaker_name_sha256="A" * 64)


def test_prepared_embedding_is_an_exact_finite_packed_vector_and_question_times_are_integer_ms():
    plan = records.BackIn(1, 1, H, 20, "channel", 1.0, "A supported claim.", (20,), _proof())
    assert records.PreparedBackIn(plan, bytes(2048), H).embedding == bytes(2048)
    assert records.PreparedBackIn(plan, None, H).embedding is None
    for value in (bytearray(2048), [0.0] * 512, bytes(4), struct.pack("<f", float("inf")) + bytes(2044)):
        with pytest.raises(ValueError, match="embedding"):
            records.PreparedBackIn(plan, value, H)
    receipt = records.QuestionReceipt("ask", "command", 20, 10, 1, H, 1000000, 1000001)
    assert records.QuestionSendResult("delivered", receipt, "none").receipt is receipt
    with pytest.raises(ValueError):
        records.QuestionReceipt("ask", "command", 20, 10, 1, H, 1000.0, 1001)
    with pytest.raises(ValueError):
        records.QuestionSendResult("unknown", None, "raw_native_error")
    with pytest.raises(ValueError, match="receipt"):
        records.QuestionSendResult("delivered", None, "none")


@pytest.mark.parametrize("state,reason", [("refused", "native_refused"), ("unknown", "transport_unknown")])
def test_nondelivered_question_result_has_no_combined_receipt(state, reason):
    receipt = records.QuestionReceipt("ask", "command", 20, 10, 1, H, 1000000, 1000001)
    with pytest.raises(ValueError, match="receipt"):
        records.QuestionSendResult(state, receipt, reason)
    with pytest.raises(ValueError, match="receipt"):
        records.QuestionSendResult(state, object(), reason)
    result = records.QuestionSendResult(state, None, reason)
    assert (result.state, result.receipt, result.safe_reason) == (state, None, reason)


def test_records_import_does_not_load_operational_or_wire_modules(tmp_path):
    package_root = Path(records.__file__).parents[2]
    script = """
import sys
sys.path.insert(0, sys.argv[1])
import leeroy_mind.realm.records
for name in ('leeroy_mind.ingress', 'leeroy_mind.protocol', 'leeroy_mind.storage.store',
             'leeroy_mind.realm.protocol', 'leeroy_mind.realm.actions', 'leeroy_mind.realm.ports',
             'leeroy_mind.realm.service', 'leeroy_mind.realm.fact_store', 'leeroy_mind.realm.provenance'):
    assert name not in sys.modules, name
"""
    probe = tmp_path / "import_probe.py"
    probe.write_text(script, encoding="utf-8")
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    subprocess.run([sys.executable, "-I", str(probe), str(package_root)], cwd=tmp_path,
        env={**os.environ, "LEEROY_MIND_DATA": str(data_dir)}, timeout=10,
        check=True, capture_output=True, text=True)
