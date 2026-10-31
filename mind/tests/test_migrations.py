"""Opening a populated mind.db from an older schema (tests lens M6) and the vec0 rebuild in
0003 (liveops I2)."""
import random
import shutil
import sqlite3

import pytest
import sqlite_vec

from conftest import make_mem
from leeroy_mind.clock import SystemClock
from leeroy_mind.storage import db as db_mod
from leeroy_mind.storage.db import latest_version, open_db
from leeroy_mind.storage.rows import pack_vec
from leeroy_mind.storage.store import Store
from leeroy_mind.testing.fake_llm import fake_embed

OWNERS = range(1000, 1010)


def _db_at(path, version: int, tmp_path, monkeypatch) -> sqlite3.Connection:
    """mind.db built by the migrations up to `version` only, as an older release left it."""
    old = tmp_path / f"migrations-v{version}"
    old.mkdir()
    for v, f in db_mod._migration_files():
        if v <= version:
            shutil.copy(f, old / f.name)
    monkeypatch.setattr(db_mod, "MIGRATIONS_DIR", old)
    try:
        return open_db(path)
    finally:
        monkeypatch.undo()


def _vectors(conn) -> list[tuple]:
    return [tuple(r) for r in conn.execute(
        "SELECT memory_id, owner, embedding FROM memory_vec ORDER BY memory_id")]


def _knn(conn, owner: int, text: str) -> list[int]:
    return [r[0] for r in conn.execute(
        "SELECT memory_id FROM memory_vec WHERE embedding MATCH ? AND k = 5 AND owner = ?",
        (pack_vec(fake_embed(text)), owner))]


def test_a_v1_db_with_rows_opens_at_the_latest_schema(tmp_path, monkeypatch):
    path = tmp_path / "mind.db"
    conn = _db_at(path, 1, tmp_path, monkeypatch)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        conn.execute("INSERT INTO chat_line(ts, channel, from_guid, from_name, to_guid, text, "
                     "origin) VALUES (1, 'guild', 1, 'Testmage', 0, 'evening all', 'human')")
        conn.execute("INSERT INTO utterance(ts, guid, channel, kind, text) "
                     "VALUES (2, 101, 'guild', 'greet', 'o/')")
        conn.execute("INSERT INTO persona_core(guid, json, card, card_tokens, version, source, "
                     "updated) VALUES (101, '{}', 'card', 1, 1, 'seed', 3)")
        store = Store(conn, clock=SystemClock())
        mid = store.insert_memory(make_mem(owner=101, embedding=pack_vec(fake_embed("old"))))
    finally:
        conn.close()
    conn = open_db(path)
    try:
        store = Store(conn, clock=SystemClock())
        assert conn.execute("PRAGMA user_version").fetchone()[0] == latest_version()
        assert store.chat_lines([1])[0].audience is None  # rows from before v2
        assert store.get_persona_json(101) == "{}"
        assert store.get_memories([mid])[0].text == make_mem().text
        assert _knn(conn, 101, "old") == [mid]
        store.log_utterance(4, 101, "guild", "reply_human", "hey", [mid], None, None,
                            bit="b1", why="reply:named")
        assert conn.execute("SELECT count(*) FROM utterance").fetchone()[0] == 2
    finally:
        conn.close()


def test_0003_rebuilds_memory_vec_in_small_chunks_keeping_every_vector(tmp_path, monkeypatch):
    """Liveops I2: vec0 gave every owner a 1,024-vector chunk (2 MB); 0003 rebuilds it with
    small chunks, keeping each vector under its memory id."""
    path = tmp_path / "mind.db"
    conn = _db_at(path, 2, tmp_path, monkeypatch)
    rng = random.Random(3)
    try:
        store = Store(conn, clock=SystemClock())
        for owner in OWNERS:
            for i in range(3):
                text = f"memory {i} of {owner} about {rng.choice(['loot', 'wipes', 'gz'])}"
                store.insert_memory(make_mem(owner=owner, text=text,
                                             embedding=pack_vec(fake_embed(text))))
        before = _vectors(conn)
        knn_before = {o: _knn(conn, o, "wipes") for o in (1000, 1005, 1009)}
    finally:
        conn.close()
    big = path.stat().st_size
    conn = open_db(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == latest_version() >= 3
        assert _vectors(conn) == before and len(before) == 3 * len(OWNERS)
        assert {o: _knn(conn, o, "wipes") for o in knn_before} == knn_before
        chunk = conn.execute("SELECT length(vectors) FROM memory_vec_vector_chunks00 "
                             "LIMIT 1").fetchone()[0]
        assert chunk == 16 * 512 * 4  # 16 vectors a chunk
    finally:
        conn.close()
    plain = sqlite3.connect(path)  # what the owner runs with the mind stopped
    try:
        plain.enable_load_extension(True)
        sqlite_vec.load(plain)
        plain.execute("VACUUM")
    finally:
        plain.close()
    assert big > 2_000_000 * len(OWNERS)  # a 2 MB chunk for every owner
    assert path.stat().st_size < 1_000_000  # 16 vectors a chunk
    conn = open_db(path)
    try:
        mid = Store(conn, clock=SystemClock()).insert_memory(make_mem(
            owner=1000, text="after the rebuild", embedding=pack_vec(fake_embed("after"))))
        assert _knn(conn, 1000, "after")[0] == mid  # the new table takes new vectors
        assert len(_vectors(conn)) == len(before) + 1
    finally:
        conn.close()


def test_0003_on_a_db_without_vectors(tmp_path, monkeypatch):
    path = tmp_path / "mind.db"
    _db_at(path, 2, tmp_path, monkeypatch).close()
    conn = open_db(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == latest_version()
        assert _vectors(conn) == []
    finally:
        conn.close()


@pytest.mark.parametrize("version", [1, 2, 3])
def test_every_older_schema_migrates_to_the_same_tables(tmp_path, monkeypatch, version):
    old = _db_at(tmp_path / "old.db", version, tmp_path, monkeypatch)
    old.close()
    upgraded, fresh = open_db(tmp_path / "old.db"), open_db(tmp_path / "fresh.db")
    try:
        def schema(c):
            return sorted((r[0], r[1]) for r in c.execute(
                "SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"))
        assert schema(upgraded) == schema(fresh)
    finally:
        upgraded.close()
        fresh.close()


def test_0004_keeps_old_utterances_and_adds_the_trigger_line_and_model_time(tmp_path,
                                                                          monkeypatch):
    """Liveops M5: a v3 db's rows survive, with no trigger line, model time or turn, and no
    whisper addressee (0)."""
    path = tmp_path / "mind.db"
    conn = _db_at(path, 3, tmp_path, monkeypatch)
    try:
        conn.execute("INSERT INTO utterance(ts, guid, channel, kind, text, bit, why) "
                     "VALUES (2, 101, 'guild', 'greet', 'o/', NULL, 'greet:reason_pool')")
        conn.commit()
    finally:
        conn.close()
    conn = open_db(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == latest_version() >= 4
        row = conn.execute("SELECT text, why, trigger_line, llm_s, to_guid, turn_id "
                           "FROM utterance").fetchone()
        assert tuple(row) == ("o/", "greet:reason_pool", None, None, 0, None)
    finally:
        conn.close()
