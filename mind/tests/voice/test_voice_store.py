from leeroy_mind.storage.db import latest_version, open_db
from leeroy_mind.storage.store import Store
from leeroy_mind.voice.store import VoiceStore


def make(tmp_path, clock):
    store = Store(open_db(tmp_path / "mind.db"), clock=clock)
    return store, VoiceStore(store), store.start_session(1000.0)


def rows(store, sql, params=()):
    out = store.conn.execute(sql, params).fetchall()
    store.conn.commit()
    return out


def test_migration(tmp_path):
    conn = open_db(tmp_path / "mind.db")
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"voice_presence", "voice_line", "voice_heard"} <= names
    assert conn.execute("PRAGMA user_version").fetchone()[0] == latest_version()
    conn.close()


def test_presence_rows(tmp_path, clock):
    store, vs, sid = make(tmp_path, clock)
    vs.enter(101, "guild_hall", 0, 1000.0, sid)
    vs.leave(101, 1100.0)
    vs.enter(101, "raid", 104, 1100.0, sid)
    got = rows(store, "SELECT room, since, until, by, session_id FROM voice_presence ORDER BY id")
    assert [tuple(r) for r in got] == [("guild_hall", 1000.0, 1100.0, 0, sid),
                                       ("raid", 1100.0, None, 104, sid)]
    assert vs.open_presence(sid) == {101: ("raid", 104)}
    assert vs.open_presence(sid + 1) == {}


def test_enter_closes_a_stretch_left_open(tmp_path, clock):
    store, vs, sid = make(tmp_path, clock)
    vs.enter(101, "guild_hall", 0, 1000.0, sid)
    vs.enter(101, "raid", 0, 1050.0, sid)
    assert rows(store, "SELECT count(*) FROM voice_presence WHERE until IS NULL")[0][0] == 1


def test_close_all_keeps_one_session(tmp_path, clock):
    store, vs, sid = make(tmp_path, clock)
    other = store.start_session(900.0)
    vs.enter(101, "afk", 0, 900.0, other)
    vs.enter(102, "afk", 0, 1000.0, sid)
    assert vs.close_all(1200.0, keep_session=sid) == 1
    assert vs.open_presence(sid) == {102: ("afk", 0)} and vs.open_presence(other) == {}
    assert vs.close_all(1300.0) == 1


def test_lines_and_heard(tmp_path, clock):
    store, vs, sid = make(tmp_path, clock)
    vs.add_line("abc.1", 7, "guild_hall", "sent", 1000.0)
    vs.set_line("abc.1", "dropped", "expired", 1005.0)
    got = rows(store, "SELECT line_id, room, state, reason, ts FROM voice_line")
    assert [tuple(r) for r in got] == [(7, "guild_hall", "dropped", "expired", 1005.0)]
    vs.add_heard(9, "hello there", 0.93, 1200, 340)
    assert tuple(rows(store, "SELECT * FROM voice_heard")[0]) == (9, "hello there", 0.93, 1200, 340)


def test_close_all_without_a_kept_session_closes_null_session_rows(tmp_path, clock):
    store, vs, sid = make(tmp_path, clock)
    vs.enter(101, "afk", 0, 900.0, None)
    vs.enter(102, "afk", 0, 900.0, sid)
    assert vs.close_all(1000.0) == 2
