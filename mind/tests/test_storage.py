import dataclasses
import json
import sqlite3
from pathlib import Path

import pytest

from conftest import make_mem, unit
from leeroy_mind.protocol import Boss, Chat, Death, Member
from leeroy_mind.storage.db import (
    EmbeddingMismatch, check_embedding_meta, checkpoint, latest_version, migrate, open_db,
    snapshot)
from leeroy_mind.storage.rows import (
    PersonaStateRow, Relationship, cosine, pack_vec, unpack_vec)
from leeroy_mind.storage.store import Store, fts_query


def chat(text="hello there", **kw):
    base = dict(ts=1_788_000_000_000, channel="guild", from_guid=1, from_name="Arkon",
                to_guid=0, text=text, map=0, zone=0)
    base.update(kw)
    return Chat(**base)


def test_open_sets_wal_version_and_vec(tmp_path):
    c = open_db(tmp_path / "m.db")
    try:
        assert c.execute("pragma journal_mode").fetchone()[0] == "wal"
        assert c.execute("pragma user_version").fetchone()[0] == latest_version() >= 1
        assert c.execute("select vec_version()").fetchone()[0].startswith("v0.1.9")
        assert c.execute("pragma foreign_keys").fetchone()[0] == 1
        assert c.execute("pragma busy_timeout").fetchone()[0] == 5000
    finally:
        c.close()


def test_migrate_is_idempotent(tmp_path):
    p = tmp_path / "m.db"
    c = open_db(p)
    try:
        c.execute("insert into meta values ('k','v')")
        c.commit()
        assert migrate(c) == latest_version()
    finally:
        c.close()
    c = open_db(p)
    try:
        assert c.execute("pragma user_version").fetchone()[0] == latest_version()
        assert c.execute("select value from meta where key='k'").fetchone()[0] == "v"
    finally:
        c.close()


def test_failed_migration_leaves_no_partial_schema(tmp_path, monkeypatch):
    bad = tmp_path / "mig"
    bad.mkdir()
    (bad / "0001_bad.sql").write_text("CREATE TABLE a(x); CREATE TABLE a(x);")
    monkeypatch.setattr("leeroy_mind.storage.db.MIGRATIONS_DIR", bad)
    with pytest.raises(sqlite3.OperationalError):
        open_db(tmp_path / "m.db")
    c = sqlite3.connect(tmp_path / "m.db")
    try:
        assert c.execute("pragma user_version").fetchone()[0] == 0
        assert c.execute("select count(*) from sqlite_master where name='a'").fetchone()[0] == 0
    finally:
        c.close()


def test_nested_transaction_joins_outer(store):
    with pytest.raises(RuntimeError), store.transaction():
        store.add_rel_delta(1, 2, "trust", 1, "t", ["chat:1"], ts=1, applied=False)
        store.add_rel_delta(1, 2, "trust", 1, "t", ["chat:1"], ts=1, applied=True)
        store.put_relationship(Relationship(a=1, b=2, trust=1.0))
        # control: the writes really happened inside the unit, so the rollback is what removes them
        assert store.conn.execute("select count(*) from rel_delta").fetchone()[0] == 2
        assert len(store.pending_rel_deltas()) == 1
        raise RuntimeError
    assert store.pending_rel_deltas() == []
    assert store.conn.execute("select count(*) from rel_delta").fetchone()[0] == 0
    assert store.get_relationship(1, 2) == Relationship(a=1, b=2)
    with store.transaction():
        store.add_rel_delta(1, 2, "trust", 1, "t", ["chat:1"], ts=1, applied=False)
        store.put_relationship(Relationship(a=1, b=2, trust=1.0))
    assert len(store.pending_rel_deltas()) == 1
    assert store.get_relationship(1, 2).trust == 1.0


def test_inner_write_does_not_commit_before_outer(store, tmp_path):
    other = sqlite3.connect(tmp_path / "mind.db")
    try:
        with store.transaction():
            with store.transaction():
                store.set_meta("k", "v")
            assert other.execute("select count(*) from meta where key='k'").fetchone()[0] == 0
        assert other.execute("select count(*) from meta where key='k'").fetchone()[0] == 1
    finally:
        other.close()


def test_caught_inner_failure_undoes_only_inner_writes(store):
    with store.transaction():
        store.set_meta("outer", "1")
        with pytest.raises(sqlite3.IntegrityError), store.transaction():
            store.set_meta("inner", "1")
            store.conn.execute("insert into session(id, started) values (1, 1), (1, 2)")
        store.set_meta("after", "1")
    assert (store.get_meta("outer"), store.get_meta("inner"), store.get_meta("after")) == (
        "1", None, "1")


def test_relationship_scores_stay_real(store):
    store.put_relationship(Relationship(a=1, b=2, affinity=49.005, familiarity=12.5))
    r = store.get_relationship(1, 2)
    assert (r.affinity, r.familiarity) == (pytest.approx(49.005), 12.5)


def test_persona_state_round_trips_self_notes(store):
    note = {"text": "I lead when nobody else will.", "evidence": ["memory:3"], "added": 5.0}
    store.put_state(PersonaStateRow(guid=7, goals=("ask about kiting",), self_notes=(note,)))
    assert store.get_state(7).self_notes == (note,)
    assert store.get_state(7).goals == ("ask about kiting",)
    assert store.get_state(99) == PersonaStateRow(guid=99)


def test_memory_is_indexed_for_actors_fts_and_vec(store):
    mid = store.insert_memory(make_mem(owner=7, text="six wipes on Sindragosa then she died",
                                       actors=(7, 9), embedding=pack_vec(unit(0))))
    assert store.memories_with_actors(7, [9])[0].id == mid
    assert store.fts(7, '"sindragosa"')[0][0] == mid
    (kid, dist), = store.knn(7, pack_vec(unit(0)), k=5)
    assert kid == mid and dist == pytest.approx(0, abs=1e-6)
    assert store.knn(8, pack_vec(unit(0)), k=5) == [] and store.fts(8, '"sindragosa"') == []
    assert store.get_memories([mid])[0].embedding == pack_vec(unit(0))


def test_knn_orders_by_distance_and_set_embedding(store):
    a = store.insert_memory(make_mem(text="a", embedding=pack_vec(unit(0))))
    b = store.insert_memory(make_mem(text="b"))
    assert [m.id for m in store.memories_missing_embedding(10)] == [b]
    near = [0.7071 if i < 2 else 0.0 for i in range(512)]
    store.set_embedding(b, pack_vec(near))
    assert store.memories_missing_embedding(10) == []
    res = store.knn(7, pack_vec(unit(0)), k=5)
    assert [i for i, _ in res] == [a, b] and res[0][1] < res[1][1]
    store.set_embedding(b, pack_vec(unit(5)))  # replacing the vector replaces the index entry
    assert len(store.knn(7, pack_vec(unit(0)), k=5)) == 2
    with pytest.raises(ValueError):
        store.set_embedding(b, b"short")


def test_fts_bad_query_returns_empty(store):
    store.insert_memory(make_mem(text="The tank wiped on Sindragosa"))
    assert store.fts(7, '"unbalanced') == []
    assert len(store.fts(7, '"tank"')) == 1  # control: the connection still works


@pytest.mark.parametrize("raw", [
    '"', "tank*", "-tank", "NEAR(tank wiped)", "(tank", "a:b", "tank AND", "it's",
    "\"; DROP TABLE memory; --"])
def test_fts_query_neutralises_operators(store, raw):
    store.insert_memory(make_mem(text="The tank wiped on Sindragosa"))
    q = fts_query(raw)
    if q:  # the parser itself must accept it, not just fts() swallowing the error
        store.conn.execute("SELECT rowid FROM memory_fts WHERE memory_fts MATCH ?", (q,)).fetchall()
    assert isinstance(store.fts(7, q), list)
    assert store.conn.execute("select count(*) from memory").fetchone()[0] == 1


def test_fts_query_matches_words_and_handles_empty(store):
    mid = store.insert_memory(make_mem(text="The tank wiped on Sindragosa"))
    assert fts_query("") == "" and fts_query('"-*()') == ""
    assert [r[0] for r in store.fts(7, fts_query("did the TANK die? (NEAR)"))] == [mid]
    assert store.fts(7, fts_query("nothing here")) == []
    assert store.fts(7, "") == []


def test_fts_skips_inactive_memories(store):
    store.insert_memory(make_mem(text="Sindragosa again", status="archived"))
    live = store.insert_memory(make_mem(text="Sindragosa live"))
    assert [r[0] for r in store.fts(7, '"sindragosa"')] == [live]


def test_keyed_upsert_updates_in_place(store):
    a = store.upsert_keyed_memory(make_mem(owner=7, key="boss:36853:2026-10-29",
                                           text="We wiped 2× on Sindragosa tonight."))
    b = store.upsert_keyed_memory(make_mem(owner=7, key="boss:36853:2026-10-29",
                                           text="We wiped 3× on Sindragosa tonight."))
    assert a == b and store.get_memories([a])[0].text.startswith("We wiped 3×")
    assert [r[0] for r in store.fts(7, '"wiped"')] == [a]
    assert store.fts(7, '"2"') == [] and [r[0] for r in store.fts(7, '"3"')] == [a]  # au trigger
    other = store.upsert_keyed_memory(make_mem(owner=8, key="boss:36853:2026-10-29", text="Other."))
    assert other != a


def test_keyed_upsert_replaces_actors_and_drops_stale_vector(store):
    a = store.upsert_keyed_memory(make_mem(key="k", text="one", actors=(1, 2),
                                           embedding=pack_vec(unit(3))))
    store.upsert_keyed_memory(make_mem(key="k", text="two", actors=(2, 5)))
    assert store.memories_with_actors(7, [1]) == []
    assert store.memories_with_actors(7, [5])[0].id == a
    assert store.knn(7, pack_vec(unit(3))) == []
    assert [m.id for m in store.memories_missing_embedding(5)] == [a]


def test_merge_and_touch(store):
    mid = store.insert_memory(make_mem(importance=4, evidence=("chat:1",)))
    store.merge_memory(mid, ["chat:1", "chat:2"], 7)
    m = store.get_memories([mid])[0]
    assert (m.evidence, m.importance, m.merged_n) == (("chat:1", "chat:2"), 7, 2)
    store.touch_memories([mid], 2000)
    m = store.get_memories([mid])[0]
    assert (m.t_accessed, m.access_n) == (2000, 1)


def test_recent_candidates_and_count(store):
    a = store.insert_memory(make_mem(kind="event", actors=(1,), t_created=100))
    store.insert_memory(make_mem(kind="event", actors=(1,), t_created=10))
    store.insert_memory(make_mem(kind="event", actors=(2,), t_created=100))
    store.insert_memory(make_mem(kind="plan", actors=(1,), t_created=100))
    assert [m.id for m in store.recent_similar_candidates(7, "event", (1,), 50)] == [a]
    assert [m.id for m in store.recent_similar_candidates(7, "event", [1, 1], 50)] == [a]
    assert store.recent_similar_candidates(7, "event", (), 50) == []  # none are actor-less
    assert store.count_inserted_since(7, 50) == 3 and store.count_inserted_since(8, 0) == 0


def test_candidates_need_the_exact_actor_set(store):
    exact = store.insert_memory(make_mem(kind="event", actors=(1, 2), t_created=100))
    store.insert_memory(make_mem(kind="event", actors=(1,), t_created=100))
    store.insert_memory(make_mem(kind="event", actors=(1, 2, 3), t_created=100))
    store.insert_memory(make_mem(kind="event", actors=(2, 3), t_created=100))
    none = store.insert_memory(make_mem(kind="event", actors=(), t_created=100))
    assert [m.id for m in store.recent_similar_candidates(7, "event", (2, 1), 50)] == [exact]
    assert [m.id for m in store.recent_similar_candidates(7, "event", (), 50)] == [none]
    assert store.recent_similar_candidates(7, "event", (1, 4), 50) == []


def test_blank_fts_query_is_empty_and_silent(store, caplog):
    assert store.fts(7, "") == [] and store.fts(7, "  ") == []
    assert not caplog.records


def test_unembedded_partial_index_exists(store):
    names = {r[0] for r in store.conn.execute("select name from sqlite_master where type='index'")}
    assert "memory_unembedded" in names


def test_embedding_dim_must_be_512(store):
    with pytest.raises(EmbeddingMismatch):
        check_embedding_meta(store, "embeddinggemma", 768)
    assert store.get_meta("embed_model") is None


def test_migrate_refuses_newer_db_and_duplicate_numbers(tmp_path, monkeypatch):
    c = open_db(tmp_path / "m.db")
    try:
        c.execute(f"pragma user_version = {latest_version() + 5}")
        c.commit()
        with pytest.raises(RuntimeError, match="newer"):
            migrate(c)
        c.execute(f"pragma user_version = {latest_version()}")
        c.commit()
        d = tmp_path / "mig"
        d.mkdir()
        (d / "0001_a.sql").write_text("select 1;")
        (d / "0001_b.sql").write_text("select 2;")
        monkeypatch.setattr("leeroy_mind.storage.db.MIGRATIONS_DIR", d)
        with pytest.raises(RuntimeError, match="duplicate"):
            migrate(c)
    finally:
        c.close()


def test_failed_commit_rolls_the_unit_back(store):
    class Boom:
        def __init__(self, conn):
            self._c = conn

        def commit(self):
            raise sqlite3.OperationalError("disk I/O error")

        def __getattr__(self, n):
            return getattr(self._c, n)

    real = store.conn
    with pytest.raises(sqlite3.OperationalError):
        with store.transaction():
            store.set_meta("k", "v")
            store.conn = Boom(real)
    store.conn = real
    assert store._depth == 0 and store.get_meta("k") is None


def test_inner_rollback_does_not_mask_original_error(store):
    with pytest.raises(sqlite3.IntegrityError):
        with store.transaction():
            with store.transaction():
                store.conn.execute("insert into session(id, started) values (1, 1), (1, 2)")
    assert store.get_meta("x") is None  # still usable


def test_close_inside_transaction_raises(tmp_path, clock):
    s = Store(open_db(tmp_path / "mind.db"), clock=clock)
    try:
        with s.transaction():
            with pytest.raises(RuntimeError):
                s.close()
    finally:
        s.close()


def test_snapshot_same_label_same_second_keeps_both(tmp_path, store):
    d = tmp_path / "s"
    a = snapshot(store.conn, d, "x", now=1_788_000_000)
    b = snapshot(store.conn, d, "x", now=1_788_000_000)
    assert a != b and a.exists() and b.exists()


def test_snapshot_failure_leaves_no_partial(tmp_path, store, monkeypatch):
    real = sqlite3.connect

    class Src:
        def __init__(self, c):
            self._c = c

        def backup(self, out):
            raise sqlite3.OperationalError("boom")

        def close(self):
            self._c.close()

    monkeypatch.setattr("leeroy_mind.storage.db.sqlite3.connect",
                        lambda *a, **k: Src(real(*a, **k)) if k.get("uri") else real(*a, **k))
    with pytest.raises(sqlite3.OperationalError):
        snapshot(store.conn, tmp_path / "s", "x", now=1_788_000_000)
    assert list((tmp_path / "s").iterdir()) == []


def test_keyed_upsert_cleans_the_lookup_key(store):
    key = "k" + chr(0xD800)
    a = store.upsert_keyed_memory(make_mem(key=key, text="one"))
    assert store.upsert_keyed_memory(make_mem(key=key, text="two")) == a


def test_memory_checks_reject_bad_rows(store):
    with pytest.raises(sqlite3.IntegrityError):
        store.insert_memory(make_mem(text="x" * 201))
    with pytest.raises(sqlite3.IntegrityError):
        store.insert_memory(make_mem(importance=11))
    assert store.count_inserted_since(7, 0) == 0  # nothing half-written


def test_relationship_default_clamp_and_delta_log(store):
    assert store.get_relationship(1, 2) == Relationship(a=1, b=2)
    store.put_relationship(Relationship(a=1, b=2, affinity=150, familiarity=-5))
    r = store.get_relationship(1, 2)
    assert (r.affinity, r.familiarity) == (100, 0)
    store.put_relationship(Relationship(a=1, b=2, affinity=-150))
    assert store.get_relationship(1, 2).affinity == -100
    store.add_rel_delta(1, 2, "affinity", 3, "joke landed", ["chat:5"], ts=1, applied=False)
    assert len(store.pending_rel_deltas()) == 1
    store.add_rel_delta(1, 2, "romance_stage", 0, "x", [], ts=1, applied=True)
    assert len(store.pending_rel_deltas()) == 1
    with pytest.raises(ValueError):
        store.add_rel_delta(1, 2, "charm", 1, "x", [], ts=1, applied=False)


def test_note_interaction(store):
    store.note_interaction(1, 2, 50)
    store.note_interaction(1, 2, 60)
    r = store.get_relationship(1, 2)
    assert (r.interactions, r.last_seen) == (2, 60)
    assert store.get_relationship(2, 1).interactions == 0


def test_persona_versions_and_card(store):
    assert store.get_card(7) is None and store.get_persona_json(7) is None
    assert store.put_persona(7, '{"a":1}', "CARD ONE", 3, "seed", 1) == 1
    assert store.put_persona(7, '{"a":2}', "CARD TWO", 3, "owner", 2) == 2
    store.put_persona(8, "{}", "c", 1, "generated", 2)
    assert store.get_card(7) == "CARD TWO" and store.get_persona_json(7) == '{"a":2}'
    assert store.persona_versions() == {7: 2, 8: 1}


def test_sessions_and_null_session_ids(store, clock):
    assert store.open_session() is None
    sid = store.start_session(clock.now())
    assert store.open_session() == sid
    store.end_session(sid, clock.now() + 5)
    assert store.open_session() is None
    lid = store.insert_chat_line(chat(), session_id=None, origin="game")
    eid = store.insert_game_event(Death(ts=5, guid=3, map=489, killer_entry=0), session_id=None)
    store.record_bit(3, "bit", 10, "2026-10-29", False, None)
    store.log_utterance(10, 3, "guild", "chat", "hi", [], None, None)
    line, = store.chat_lines([lid])
    assert line.session_id is None
    ev = store.conn.execute("select * from game_event where id=?", (eid,)).fetchone()
    assert ev["session_id"] is None and ev["guid"] == 3 and ev["type"] == "death"


def test_chat_line_and_game_event_use_mind_seconds(store, clock):
    lid = store.insert_chat_line(chat(), session_id=None, origin="human")
    assert store.chat_lines([lid])[0].ts == clock.now() != 1_788_000_000_000
    eid = store.insert_game_event(
        Boss(ts=1_788_000_123_000, map=631, instance_id=5, boss_id=36853, boss_name="Sindragosa",
             state="kill", members=(1, 2)), session_id=None)
    row = store.conn.execute("select * from game_event where id=?", (eid,)).fetchone()
    assert row["ts"] == clock.now() and row["type"] == "boss" and row["guid"] is None
    assert json.loads(row["payload"])["ts"] == 1_788_000_123_000


def test_a_replayed_event_is_stored_at_the_time_given(store, clock):
    """Correctness M2: the app gives a replayed event its own time (MindApp.is_backlog); the
    default is still the mind clock."""
    lid = store.insert_chat_line(chat(), session_id=None, origin="human", ts=1000.5)
    eid = store.insert_game_event(Death(ts=5, guid=3, map=489, killer_entry=0),
                                  session_id=None, ts=2000.25)
    assert store.chat_lines([lid])[0].ts == 1000.5
    row = store.conn.execute("select ts from game_event where id=?", (eid,)).fetchone()
    assert row["ts"] == 2000.25
    assert store.chat_lines([store.insert_chat_line(chat(), session_id=None, origin="game",
                                                    ts=None)])[0].ts == clock.now()


def test_lone_surrogate_is_stored_without_raising(store):
    lid = store.insert_chat_line(chat("bad \ud800 text", from_name="Be\udc00lra"),
                                 session_id=None, origin="human")
    line, = store.chat_lines([lid])
    assert line.text == "bad � text" and line.from_name == "Be�lra"
    mid = store.insert_memory(make_mem(text="x\ud800y", evidence=("chat:\ud800",)))
    assert store.get_memories([mid])[0].text == "x�y"
    eid = store.insert_game_event(chat("\ud800", from_name="\ud800"), session_id=None)
    assert eid > 0
    assert [r[0] for r in store.fts(7, fts_query("x\ud800y"))] == [mid]


def test_cast_round_trip(store):
    m = Member(guid=5, name="Testmage", race=1, class_=8, gender=0, level=80, rank=3, online=True,
               map=0, zone=1519, spec_tab=2)
    store.upsert_cast([m], 100)
    assert store.cast() == {5: m}
    store.set_online(5, False)
    assert store.cast()[5].online is False
    store.upsert_cast([m], 101)
    assert store.cast() == {5: m}


def _cast_row(guid: int, name: str, rank: int = 3) -> Member:
    return Member(guid=guid, name=name, race=1, class_=8, gender=0, level=80, rank=rank,
                  online=True, map=0, zone=1519, spec_tab=0)


def test_the_stored_cast_follows_the_bridges_snapshot(store):
    """Fix wave G6: each Cast event is the whole cast, so a member missing from it (kicked, or
    a leftover from before the bridge's filter) leaves the stored roster; the kept row (his:
    the bridge never sends him) stays, and so do the persona and relationships of the one who
    left."""
    human, korvin, belra, arkon = (_cast_row(1, "Testmage", 0), _cast_row(106, "Korvin"),
                                   _cast_row(102, "Belra"), _cast_row(101, "Arkon"))
    store.upsert_cast([human, korvin, belra, arkon], 100)
    store.put_persona(106, '{"guid": 106}', "You are Tarvo. You play Korvin.", 9, "seed", 100)
    store.put_relationship(Relationship(a=106, b=1, affinity=-20.0, interactions=3))
    moved = dataclasses.replace(arkon, zone=4395)
    assert store.replace_cast([belra, moved], 200, keep=(1,)) == [106]
    assert store.cast() == {1: human, 101: moved, 102: belra}
    assert store.get_persona_json(106) == '{"guid": 106}'
    assert store.get_relationship(106, 1).affinity == -20.0
    assert store.replace_cast([], 300, keep=(1,)) == []  # a bridge that hasn't loaded the guild
    assert set(store.cast()) == {1, 101, 102}
    assert store.replace_cast([belra], 400) == [1, 101]  # nothing kept: only the snapshot
    assert set(store.cast()) == {102}


def test_lines_before_a_line_newest_first(store):
    """What the pushback window reads (fix wave G6): a channel's lines before a line, from a
    time on, every origin, optionally one speaker's."""
    def line(text, guid, origin, ts, channel="guild"):
        return store.insert_chat_line(chat(text, from_guid=guid, channel=channel),
                                      session_id=None, origin=origin, ts=ts)
    a = line("a", 101, "mind", 1000.0)
    b = line("b", 102, "game", 1010.0)
    p = line("p", 103, "game", 1010.0, channel="party")
    c = line("c", 1, "human", 1010.0)
    later = line("d", 101, "mind", 1020.0)

    def ids(*args, **kw):
        return [r.id for r in store.lines_before(*args, **kw)]
    assert ids(later, ["guild"], since=1000.0) == [c, b, a]
    assert ids(later, ["guild"], since=1000.5) == [c, b]
    assert ids(c, ["guild", "party"], since=1000.0) == [p, b, a]
    assert ids(later, ["guild"], since=1000.0, from_guid=101) == [a]
    assert ids(later, ["guild"], since=1000.0, limit=1) == [c]
    assert ids(a, ["guild"], since=0.0) == []
    got = store.lines_before(later, ["guild"], since=1000.0)[0]
    assert (got.text, got.origin, got.ts, got.from_guid) == ("c", "human", 1010.0, 1)


def test_threads_and_bits_and_utterances(store):
    l1 = store.insert_chat_line(chat("a"), session_id=None, origin="game")
    l2 = store.insert_chat_line(chat("b"), session_id=None, origin="game")
    t = store.open_thread("guild:1", 10)
    store.set_thread([l1, l2], t)
    assert {r.thread_id for r in store.chat_lines([l1, l2])} == {t}
    assert store.threads_pending() == []
    store.close_thread(t, 20, "pending")
    t2 = store.open_thread("guild:2", 30)
    store.close_thread(t2, 40, "failed")
    t3 = store.open_thread("guild:3", 50)
    store.close_thread(t3, 60, "extracted")
    assert store.threads_pending() == [t, t2]
    with pytest.raises(sqlite3.IntegrityError):
        store.close_thread(t, 70, "bogus")
    assert store.last_bit_use(1, "b") is None and not store.voice_bit_used_on("n1")
    store.record_bit(1, "b", 5, "n1", True, None)
    store.record_bit(1, "b", 9, "n1", False, None)
    assert store.last_bit_use(1, "b") == 9 and store.voice_bit_used_on("n1")
    assert not store.voice_bit_used_on("n2")
    assert store.log_utterance(1, 1, "guild", "chat", "hi", [3], "too long", None) > 0


def test_chat_lines_keep_who_saw_them_and_utterances_why_they_were_said(store):
    """Carried to Task 17 from the cognition memo: neither can be rebuilt later."""
    seen = store.insert_chat_line(chat(), session_id=None, origin="human",
                                  audience=[103, 101, 101])
    unseen = store.insert_chat_line(chat("b"), session_id=None, origin="game", audience=[])
    unknown = store.insert_chat_line(chat("c"), session_id=None, origin="game")
    assert [r.audience for r in store.chat_lines([seen, unseen, unknown])] == [
        (101, 103), (), None]
    uid = store.log_utterance(10, 101, "guild", "bit", "the bit", [], None, None,
                              bit="bit101", why="burst:weighted_draw")
    plain = store.log_utterance(11, 102, "guild", "open", "hi", [], "guards", None)
    rows = store.conn.execute("SELECT id, bit, why FROM utterance WHERE id IN (?, ?) ORDER BY id",
                              (uid, plain)).fetchall()
    assert [tuple(r) for r in rows] == [(uid, "bit101", "burst:weighted_draw"),
                                        (plain, None, None)]


def test_snapshot_prunes_only_its_own_files(tmp_path, store):
    d = tmp_path / "snaps"
    d.mkdir()
    (d / "owner-copy.db").write_bytes(b"x")
    (d / "mind-notes.db").write_bytes(b"x")
    for i in range(12):
        snapshot(store.conn, d, "t", keep=10, now=1_788_000_000 + i)
    kept = sorted(p.name for p in d.glob("mind-t-*.db"))
    assert len(kept) == 10
    oldest = {snapshot_name("t", 1_788_000_000 + i) for i in (0, 1)}
    assert not oldest & set(kept)  # the two oldest went
    assert (d / "owner-copy.db").exists() and (d / "mind-notes.db").exists()
    assert not list(d.glob("*.partial"))


def snapshot_name(label: str, now: float) -> str:
    from datetime import datetime
    return f"mind-{label}-{datetime.fromtimestamp(now):%Y%m%d-%H%M%S}.db"


def test_each_label_keeps_its_own_snapshots(tmp_path, store):
    """Liveops M2: a night of restarts must not rotate out the pre-go-live snapshot."""
    d = tmp_path / "snaps"
    keep = snapshot(store.conn, d, "pre-golive", keep=10, now=1_788_000_000)
    for i in range(1, 13):
        snapshot(store.conn, d, "start", keep=10, now=1_788_000_000 + i)
    snapshot(store.conn, d, "pre", keep=10, now=1_788_000_100)
    snapshot(store.conn, d, "start", keep=10, now=1_788_000_200)  # same second as nothing else
    assert keep.exists()
    assert len(list(d.glob("mind-start-*.db"))) == 10 and len(list(d.glob("mind-pre-2*.db"))) == 1
    dup = snapshot(store.conn, d, "start", keep=10, now=1_788_000_200)  # a same-second twin
    assert dup.name.startswith("mind-start-2-") and len(list(d.glob("mind-start-*.db"))) == 10


def test_snapshot_is_a_usable_copy_of_committed_data_only(tmp_path, store):
    store.set_meta("committed", "yes")
    with store.transaction():
        store.set_meta("pending", "no")
        p = snapshot(store.conn, tmp_path / "snaps", "mid", now=1_788_000_000)
    c = sqlite3.connect(p)
    try:
        assert c.execute("pragma integrity_check").fetchone()[0] == "ok"
        got = dict(c.execute("select key, value from meta").fetchall())
    finally:
        c.close()
    assert got.get("committed") == "yes" and "pending" not in got
    assert store.get_meta("pending") == "no"  # the live unit was not disturbed


def test_snapshot_with_concurrent_writer_connection(tmp_path, store):
    store.set_meta("a", "1")
    other = sqlite3.connect(tmp_path / "mind.db", timeout=5)
    try:
        other.execute("insert into meta values ('b','2')")
        other.commit()
        p = snapshot(store.conn, tmp_path / "snaps", "w", now=1_788_000_000)
    finally:
        other.close()
    c = sqlite3.connect(p)
    try:
        assert dict(c.execute("select key, value from meta").fetchall()) == {"a": "1", "b": "2"}
    finally:
        c.close()


def test_snapshot_default_now_and_label_sanitised(tmp_path, store):
    p = snapshot(store.conn, tmp_path / "s", "../evil name")
    assert p.parent == tmp_path / "s" and p.name.startswith("mind-_evil_name-")


def test_close_truncates_wal(tmp_path, clock):
    path = tmp_path / "mind.db"
    s = Store(open_db(path), clock=clock)
    wal = Path(str(path) + "-wal")
    try:
        s.set_meta("k", "v")
        assert wal.exists() and wal.stat().st_size > 0
        checkpoint(s.conn)
        assert wal.stat().st_size == 0
        s.set_meta("k2", "v")
        assert wal.stat().st_size > 0
    finally:
        s.close()
    assert not wal.exists() or wal.stat().st_size == 0
    c = open_db(path)
    try:
        assert c.execute("select count(*) from meta").fetchone()[0] == 2
    finally:
        c.close()


def test_reads_do_not_pin_a_snapshot(store, tmp_path):
    assert store.get_meta("k") is None
    other = sqlite3.connect(tmp_path / "mind.db")
    try:
        other.execute("insert into meta values ('k','v')")
        other.commit()
    finally:
        other.close()
    assert store.get_meta("k") == "v"


def test_embedding_meta_mismatch(store):
    check_embedding_meta(store, "qwen3-embedding:0.6b", 512)
    check_embedding_meta(store, "qwen3-embedding:0.6b", 512)
    with pytest.raises(EmbeddingMismatch):
        check_embedding_meta(store, "embeddinggemma", 768)
    with pytest.raises(EmbeddingMismatch):
        check_embedding_meta(store, "qwen3-embedding:0.6b", 768)


def test_vec_helpers():
    assert cosine(unit(0), unit(0)) == pytest.approx(1) and cosine(unit(0), unit(1)) == 0
    assert cosine([0.0], [1.0]) == 0
    assert unpack_vec(pack_vec([1.0, -2.5])) == [1.0, -2.5]


def test_note_interaction_never_moves_last_seen_back(store):
    store.note_interaction(1, 2, 60)
    store.note_interaction(1, 2, 50)
    r = store.get_relationship(1, 2)
    assert (r.interactions, r.last_seen) == (2, 60)


def test_set_thread_status_keeps_the_close_time(store):
    t = store.open_thread("guild", 1.0)
    store.set_thread_status(t, 9.0, "failed")
    assert store.thread_status(t) == "failed" and store.thread_closed(t) == 9.0
    store.set_thread_status(t, 99.0, "extracted")
    assert store.thread_status(t) == "extracted" and store.thread_closed(t) == 9.0
    assert store.thread_status(12345) is None


# -- part F: the trigger line, the model's time, own lines, stats ------------------------------

def test_an_utterance_keeps_the_line_it_answered_and_the_models_time(store):
    """Liveops M5: say_sent/say_dropped log trigger= and llm_s=; the row keeps them too."""
    line = store.insert_chat_line(chat("anyone up for heroics"), session_id=None, origin="human")
    said = store.log_utterance(10, 101, "guild", "reply_human", "sure", [], None, None,
                               why="reply:named", trigger_line=line, llm_s=0.42)
    plain = store.log_utterance(11, 102, "guild", "open", "", [], "guards", None)
    rows = store.conn.execute("SELECT trigger_line, llm_s FROM utterance WHERE id IN (?, ?) "
                              "ORDER BY id", (said, plain)).fetchall()
    assert [tuple(r) for r in rows] == [(line, 0.42), (None, None)]


def _said(store, ts, guid, text, turn, *, channel="guild", to_guid=0, llm=0.5, dropped=None):
    """One utterance row as the talk pipeline writes it for a line (once it passes these)."""
    store.log_utterance(ts, guid, channel, "open", text, [], dropped, None, llm_s=llm,
                        to_guid=to_guid, turn_id=turn)


def test_recent_mind_lines_are_a_members_own_turns_the_model_wrote_newest_last(store):
    """Prompts minor 15 (and part F review, Minor 6): after a restart, "Your last lines" and
    the repeat check start from the turns the member said before it, as the pipeline keeps
    its own: one entry per turn, its lines joined, the model's turns only (no template gz,
    kill cheers or say_as lines)."""
    _said(store, 10, 101, "so the tank", "t1")
    _said(store, 14, 101, "pulls the whole room", "t1")  # one turn, two lines
    _said(store, 20, 101, "gz", "t2", llm=None)  # a template gz: no model asked
    _said(store, 30, 101, "between us", "t3", channel="whisper", to_guid=1)
    _said(store, 40, 102, "not hers", "t4")
    _said(store, 50, 101, "dropped line", "t5", dropped="guards")
    _said(store, 60, 101, "ok story time", "t6")
    _said(store, 63, 101, "so it goes", "t6")
    _said(store, 66, 101, "and the punchline", "t6")  # a bit's three rows
    assert store.recent_mind_lines(101, 3) == [
        ("guild", 0, "so the tank pulls the whole room"), ("whisper", 1, "between us"),
        ("guild", 0, "ok story time so it goes and the punchline")]
    assert store.recent_mind_lines(101, 1) == [
        ("guild", 0, "ok story time so it goes and the punchline")]
    assert [t for _, _, t in store.recent_mind_lines(101, 5, model_only=False)] == [
        "so the tank pulls the whole room", "gz", "between us",
        "ok story time so it goes and the punchline"]
    assert store.recent_mind_lines(103, 5) == [] and store.recent_mind_lines(101, 0) == []


def test_recent_mind_lines_keep_a_turn_id_used_again_after_a_restart_apart(store):
    """Turn ids count from t1 in every run of the mind: the same id far apart, or on another
    channel, is another turn; a row from before the pipeline passed ids is a turn of its own."""
    _said(store, 1000, 101, "before the restart", "t1")
    _said(store, 5000, 101, "after it", "t1")
    _said(store, 5003, 101, "in the raid now", "t1", channel="raid")
    _said(store, 6000, 101, "old row one", None)
    _said(store, 6001, 101, "old row two", None)
    assert [t for _, _, t in store.recent_mind_lines(101, 9)] == [
        "before the restart", "after it", "in the raid now", "old row one", "old row two"]
    plan = " ".join(r[-1] for r in store.conn.execute(
        "EXPLAIN QUERY PLAN SELECT ts, channel, to_guid, text, turn_id FROM utterance "
        "WHERE guid = 101 AND dropped IS NULL AND llm_s IS NOT NULL ORDER BY id DESC LIMIT 9"))
    assert "utterance_said" in plan  # one member's said rows, not a scan of the whole log


def test_talk_stats_count_a_window_and_never_carry_text(store, clock):
    t = clock.now()
    for dt, guid, kind, text, dropped, llm in [
            (-5, 101, "open", "before the window", None, 9.0),
            (1, 101, "reply_human", "hey you", None, 0.4),
            (2, 101, "reply_human", "second line", None, 0.4),
            (3, 102, "open", "so anyway", None, 1.2),
            (4, 103, "open", "", "expired", 6.0),
            (5, 104, "gz", "", "guards", None),
            (100, 105, "open", "after it", None, 0.1)]:
        store.log_utterance(t + dt, guid, "guild", kind, text, [], dropped, None, llm_s=llm)
    store.insert_chat_line(chat("evening all"), session_id=None, origin="human", ts=t + 2)
    store.insert_chat_line(chat("follow", channel="party"), session_id=None, origin="human",
                           ts=t + 3)
    store.insert_chat_line(chat("much earlier"), session_id=None, origin="human", ts=t - 60)
    store.insert_chat_line(chat("a bot's own line"), session_id=None, origin="game", ts=t + 4)
    stats = store.talk_stats(t, t + 10)
    assert stats == {"said": {"open": 1, "reply_human": 2},
                     "drops": {"expired": 1, "guards": 1}, "llm_s": [0.4, 0.4, 1.2, 6.0]}
    assert store.human_lines(t, t + 10) == [("guild", "evening all"), ("party", "follow")]
    assert store.drops_since(t, ("expired", "llm_error", "guards")) == [
        (t + 4, "expired"), (t + 5, "guards")]
    assert store.drops_since(t + 4, ("expired",)) == []


def test_the_latest_session_is_the_newest_open_or_ended(store, clock):
    assert store.latest_session() is None
    first = store.start_session(clock.now())
    store.end_session(first, clock.now() + 5)
    assert store.latest_session() == first
    second = store.start_session(clock.now() + 10)
    assert store.latest_session() == second > first and store.open_session() == second


def test_fts_all_searches_every_owner_best_first(store):
    """Fix wave G3: who holds a memory on his line, whoever they are."""
    a = store.insert_memory(make_mem(owner=7, text="The potion feud started over a cauldron."))
    b = store.insert_memory(make_mem(owner=8, text="The potion feud is still on."))
    store.insert_memory(make_mem(owner=9, text="The potion feud", status="archived"))
    store.insert_memory(make_mem(owner=9, text="Nothing to see here."))
    assert [i for i, _ in store.fts_all(fts_query("potion feud cauldron"))] == [a, b]
    assert store.fts_all('"unbalanced') == [] and store.fts_all("") == []
    assert [i for i, _ in store.fts_all(fts_query("potion feud"), k=1)] in ([a], [b])


def test_active_memory_texts_and_count(store):
    """Fix round 1 (review C1): how rare a word is across the guild's memories."""
    store.insert_memory(make_mem(owner=7, text="The potion feud started over a cauldron."))
    store.insert_memory(make_mem(owner=8, text="Raid night again.", status="archived"))
    store.insert_memory(make_mem(owner=9, text="Raid night once more."))
    assert store.active_memory_count() == 2
    assert sorted(store.active_memory_texts()) == ["Raid night once more.",
                                                   "The potion feud started over a cauldron."]
