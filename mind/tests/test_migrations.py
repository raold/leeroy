"""Opening a populated mind.db from an older schema (tests lens M6) and the vec0 rebuild in
0003 (liveops I2)."""
import json
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


def migration_number(stem: str) -> int:
    """The number of the migration named `stem` (the part after NNNN_); tests find a migration by
    name because a branch's number is renamed at merge."""
    [n] = [v for v, f in db_mod._migration_files() if f.stem.split("_", 1)[1] == stem]
    return n


def db_before(stem: str, path, tmp_path, monkeypatch) -> sqlite3.Connection:
    """mind.db as the release before migration `stem` left it."""
    return _db_at(path, migration_number(stem) - 1, tmp_path, monkeypatch)


def test_discord_photos_preserves_messages_threads_reactions_and_snaps(tmp_path, monkeypatch):
    path = tmp_path / 'mind.db'
    conn = db_before('discord_photos', path, tmp_path, monkeypatch)
    conn.executescript("""
        INSERT INTO discord_message(id,channel,author,origin,text,ts,pinned,turn)
          VALUES(1,'pics',1,'human','dog',1,1,'old');
        INSERT INTO discord_thread VALUES(1,'pics',1,'dogs',1);
        INSERT INTO discord_message(id,channel,author,origin,text,ts,reply_to,thread,deleted)
          VALUES(2,'pics',101,'mind','cute',2,1,1,3);
        INSERT INTO discord_reaction VALUES(1,'👍',101,2);
        INSERT INTO selfie_snap(id,kind,guid,source,ref,night,status,scene,audience,phases,
            pick,looks,created,not_before,expires,rendered,sent,sender,channel,line_id)
          VALUES(1,'situation',101,'everyday','everyday:2026-11-12','2026-11-12','sent',
            '{}','him','[]',1,'hash',1,1,100,2,3,101,'whisper',55);
    """)
    tables = ('discord_message', 'discord_thread', 'discord_reaction', 'selfie_snap')
    before = {t: [dict(r) for r in conn.execute(f'SELECT * FROM {t}')] for t in tables}
    conn.commit()
    conn.close()
    conn = open_db(path)
    try:
        for table in tables:
            after = [dict(r) for r in conn.execute(f'SELECT * FROM {table}')]
            if table == 'discord_message':
                for row in after:
                    assert row.pop('bot') is None
            if table == 'selfie_snap':
                for row in after:
                    assert row.pop('request') is None
            assert after == before[table]
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()
        assert conn.execute('PRAGMA foreign_keys').fetchone()[0] == 1
        for channel in ('discord', 'dm'):
            conn.execute('UPDATE selfie_snap SET channel=?', (channel,))
        assert db_mod.migrate(conn) == latest_version()
    finally:
        conn.close()


def test_claims_upgrades_an_old_db_and_preserves_rows(tmp_path, monkeypatch):
    path = tmp_path / 'mind.db'
    conn = db_before('claims', path, tmp_path, monkeypatch)
    conn.execute("INSERT INTO meta(key,value) VALUES ('claims-test','preserved')")
    conn.commit()
    conn.close()
    conn = open_db(path)
    try:
        assert conn.execute('PRAGMA user_version').fetchone()[0] == latest_version()
        assert conn.execute("SELECT value FROM meta WHERE key='claims-test'").fetchone()[0] == 'preserved'
        names = {r[0] for r in conn.execute('SELECT name FROM sqlite_master')}
        assert {'member_claim', 'member_claim_vec', 'member_claim_cursor'} <= names
        assert conn.execute('SELECT count(*) FROM member_claim').fetchone()[0] == 0
        assert db_mod.migrate(conn) == latest_version()
    finally:
        conn.close()


# The memory columns of schemas v1-v4, before the nightly scope.
_OLD_MEMORY_COLS = ("owner, kind, text, t_event, t_created, t_accessed, access_n, actors, place, "
                    "boss_id, raid_id, importance, valence, source, evidence, confidence, status, "
                    "superseded_by, key, merged_n, embedding")


def _old_memory(conn, m) -> int:
    """Memory `m`, its actors and its vector in an older schema's own columns, as that
    release's Store wrote them (today's Store writes today's columns)."""
    mid = conn.execute(
        f"INSERT INTO memory({_OLD_MEMORY_COLS}) VALUES ({','.join('?' * 21)})",
        (m.owner, m.kind, m.text, m.t_event, m.t_created, m.t_accessed, m.access_n,
         json.dumps(list(m.actors)), m.place, m.boss_id, m.raid_id, m.importance, m.valence,
         m.source, json.dumps(list(m.evidence)), m.confidence, m.status, m.superseded_by, m.key,
         m.merged_n, m.embedding)).lastrowid
    conn.executemany("INSERT INTO memory_actor(memory_id, guid) VALUES (?, ?)",
                     [(mid, g) for g in m.actors])
    if m.embedding is not None:
        conn.execute("INSERT INTO memory_vec(memory_id, owner, embedding) VALUES (?, ?, ?)",
                     (mid, m.owner, m.embedding))
    return mid


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
        mid = _old_memory(conn, make_mem(owner=101, embedding=pack_vec(fake_embed("old"))))
        conn.commit()
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
        for owner in OWNERS:
            for i in range(3):
                text = f"memory {i} of {owner} about {rng.choice(['loot', 'wipes', 'gz'])}"
                _old_memory(conn, make_mem(owner=owner, text=text,
                                           embedding=pack_vec(fake_embed(text))))
        conn.commit()
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
    # Later migrations add unrelated tables. Measure this fixture's data above an
    # empty current schema so voice/nightly metadata cannot mask vector compaction.
    empty_path = tmp_path / "empty-current.db"
    empty = open_db(empty_path)
    empty.close()
    assert path.stat().st_size - empty_path.stat().st_size < 1_000_000
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


def test_talk_moves_migrates_a_db_from_before_it_and_adds_both_tables(tmp_path, monkeypatch):
    path = tmp_path / "mind.db"
    conn = db_before("talk_moves", path, tmp_path, monkeypatch)
    try:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
        assert "talk_move" not in names and "pair_note" not in names
        assert conn.execute("PRAGMA user_version").fetchone()[0] == migration_number("talk_moves") - 1
    finally:
        conn.close()
    conn = open_db(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == latest_version()
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
        assert {"talk_move", "pair_note", "talk_move_pair", "pair_note_pair"} <= names
        conn.execute("INSERT INTO talk_move(ts, guid, counterpart, channel) VALUES (1, 101, 1, 'guild')")
        assert tuple(conn.execute("SELECT to_guid, moves, main, topics, clause, angle "
                                  "FROM talk_move").fetchone()) == (0, "[]", "", "[]", "", "")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO pair_note(ts, guid, other, kind, text, channel) "
                         "VALUES (1, 101, 1, 'k', ?, 'guild')", ("x" * 81,))
    finally:
        conn.close()


def test_a_v17_db_gains_the_empty_selfie_tables(tmp_path, monkeypatch):
    """P2: the selfies migration (migrations/*_selfies.sql, the one after tuning's) adds the
    profile photo tables to a database the tuning release left, and keeps its rows."""
    path = tmp_path / "mind.db"
    conn = _db_at(path, 17, tmp_path, monkeypatch)
    try:
        assert not {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE 'selfie_%'")}
        conn.execute("INSERT INTO tuning_change(id,ts,source,label) VALUES (1,1000,'console','')")
        conn.commit()
    finally:
        conn.close()
    conn = open_db(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == latest_version() >= 18
        names = {r[0]: r[1] for r in conn.execute(
            "SELECT name, type FROM sqlite_master WHERE name LIKE 'selfie_%' "
            "AND name NOT LIKE 'selfie_snap%' AND name NOT LIKE 'selfie_request%'")}
        assert names == {"selfie_set": "table", "selfie_candidate": "table",
                         "selfie_pick": "table", "selfie_current": "table",
                         "selfie_set_one_queued": "index", "selfie_embedding": "table"}
        for t in ("selfie_set", "selfie_candidate", "selfie_pick", "selfie_current"):
            assert conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM tuning_change").fetchone()[0] == 1
    finally:
        conn.close()


def test_the_board_reply_migration_adds_board_reply(tmp_path, monkeypatch):
    """P3: the Board replies migration (migrations/*_board_reply.sql, found by its name so a
    renumbering at merge changes nothing here) adds his replies' table and its one-waiting index
    to a database the release before it left, and keeps that database's rows."""
    number = next(v for v, p in db_mod._migration_files() if p.stem.endswith("_board_reply"))
    path = tmp_path / "mind.db"
    conn = _db_at(path, number - 1, tmp_path, monkeypatch)
    try:
        assert not conn.execute("SELECT name FROM sqlite_master WHERE name LIKE 'board_%'").fetchall()
        conn.execute("INSERT INTO realm_post(id,ts,author,author_kind,thread,text) "
                     "VALUES (7,1000,1101,'member','alpha','an invented post')")
        conn.commit()
    finally:
        conn.close()
    conn = open_db(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == latest_version() >= number
        names = {r[0]: r[1] for r in conn.execute(
            "SELECT name, type FROM sqlite_master WHERE name LIKE 'board_%'")}
        assert names == {"board_reply": "table", "board_reply_one_waiting": "index"}
        assert conn.execute("SELECT count(*) FROM board_reply").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM realm_post").fetchone()[0] == 1

        def add(**kw):
            row = {"ts": 1, "kind": "post", "parent": 7, "to_guid": 1101, "text": "hi",
                   "state": "waiting", **kw}
            conn.execute(f"INSERT INTO board_reply({','.join(row)}) "
                         f"VALUES ({','.join('?' * len(row))})", tuple(row.values()))
        add()
        for bad in ({"kind": "say"}, {"parent": 0}, {"parent": "7"}, {"to_guid": 0},
                    {"to_guid": 1 << 32}, {"text": ""}, {"text": "a" * 201},
                    {"kind": "letter", "text": "a" * 401}, {"state": "lost"},
                    {"state": "posted"},  # settled without a time
                    {"state": "waiting", "settled": 5, "parent": 8},
                    {"state": "refused", "settled": 5, "parent": 8},  # refused without a reason
                    {"state": "refused", "settled": 5, "reason": "Bad Reason", "parent": 8},
                    {"kind": "letter", "fact": 1}, {"kind": "letter", "answer_post": 1},
                    {"kind": "post", "answer_mail": 1, "parent": 8},
                    {"parent": 7}):  # a second waiting reply to post 7
            with pytest.raises(sqlite3.IntegrityError):
                add(**bad)
        add(kind="letter", parent=7, text="a" * 400, state="answered", settled=5, answer_mail=3)
        add(state="refused", settled=5, reason="screen_name_2", night="2026-11-07")
    finally:
        conn.close()


def test_the_snaps_migration_adds_selfie_snap(tmp_path, monkeypatch):
    """P4: the snaps migration (found by its name, so a renumbering at merge changes nothing)
    adds the situational photo table to an older database and keeps its rows."""
    number = next(v for v, p in db_mod._migration_files() if p.stem.endswith("_selfie_snaps"))
    path = tmp_path / "mind.db"
    conn = _db_at(path, number - 1, tmp_path, monkeypatch)
    try:
        assert not conn.execute("SELECT name FROM sqlite_master WHERE name='selfie_snap'").fetchall()
        conn.execute("INSERT INTO tuning_change(id,ts,source,label) VALUES (1,1000,'console','')")
        conn.commit()
    finally:
        conn.close()
    conn = open_db(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == latest_version() >= number
        names = {r[0]: r[1] for r in conn.execute(
            "SELECT name, type FROM sqlite_master WHERE name LIKE 'selfie_snap%'")}
        assert names == {"selfie_snap": "table", "selfie_snap_open": "index",
                         "selfie_snap_sent": "index", "selfie_snap_request": "index"}
        assert conn.execute("SELECT count(*) FROM selfie_snap").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM tuning_change").fetchone()[0] == 1
    finally:
        conn.close()


def test_an_older_db_gains_the_discord_tables_and_keeps_its_rows(tmp_path, monkeypatch):
    path = tmp_path / "mind.db"
    conn = db_before("discord", path, tmp_path, monkeypatch)
    conn.execute("INSERT INTO meta(key, value) VALUES ('keep', 'me')")
    conn.commit()
    conn.close()
    number = migration_number("discord")
    conn = open_db(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == latest_version() >= number
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE name LIKE 'discord_%'")}
        assert {"discord_message", "discord_thread", "discord_reaction"} <= names
        assert conn.execute("SELECT value FROM meta WHERE key = 'keep'").fetchone()[0] == "me"
        assert conn.execute("SELECT count(*) FROM discord_message").fetchone()[0] == 0
        for bad in ("origin='bot'", "author=0"):
            row = {"origin": "human", "text": "x", "author": 1}
            k, v = bad.split("=")
            row[k] = eval(v)
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO discord_message(channel, author, origin, text, ts) "
                             "VALUES ('g', ?, ?, ?, 1)", (row["author"], row["origin"], row["text"]))
    finally:
        conn.close()
