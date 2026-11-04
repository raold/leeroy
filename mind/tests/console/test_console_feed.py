"""What the console's text feed shows: the lines the owner could see in WoW, read-only.
The names and lines are invented."""
import sqlite3

import pytest

from leeroy_mind.console import feed
from leeroy_mind.protocol import Chat, Member

HUMAN = 900


@pytest.fixture
def db(store, tmp_path):
    store.set_meta("human_guid", str(HUMAN))
    store.upsert_cast([Member(guid=g, name=n, race=1, class_=1, gender=0, level=80, rank=3,
                              online=True, map=0, zone=0, spec_tab=-1)
                       for g, n in ((101, "Arkon"), (102, "Belra"), (103, "Coros"))], 1.0)
    return store, tmp_path / "mind.db"


def say(store, channel, frm, text, *, to=0, session=None, audience=None, name=None):
    return store.insert_chat_line(
        Chat(ts=0, channel=channel, from_guid=frm, from_name=name or f"P{frm}", to_guid=to,
             text=text, map=0, zone=0), session_id=session, origin="game", audience=audience)


def shown(path, **kw):
    return [line["text"] for line in feed.read(path, feed.feed, **kw)]


def test_guild_and_officer_lines_show_while_he_was_online(db):
    store, path = db
    sid = store.start_session(1.0)
    say(store, "guild", 101, "g online", session=sid)
    say(store, "guild", 101, "g away")  # no session: he was not there
    say(store, "officer", 102, "o online", session=sid)
    assert shown(path) == ["g online", "o online"]


def test_group_lines_show_only_from_his_group(db):
    store, path = db
    sid = store.start_session(1.0)
    say(store, "party", 101, "in his party", session=sid, audience=[101, 102])
    say(store, "party", 103, "another party", session=sid, audience=[101, 102])
    say(store, "raid", HUMAN, "his own", session=sid, audience=[101])
    say(store, "raid_warning", 102, "warning", session=sid, audience=[102])
    say(store, "party", 101, "before audiences", session=sid)
    assert shown(path) == ["in his party", "his own", "warning"]


def test_whispers_show_only_with_him(db):
    store, path = db
    say(store, "whisper", 101, "to him", to=HUMAN)
    say(store, "whisper", HUMAN, "from him", to=102, name="Owner")
    say(store, "whisper", 101, "between members", to=102)
    lines = feed.read(path, feed.feed)
    assert [line["text"] for line in lines] == ["to him", "from him"]
    assert lines[1]["you"] is True and lines[1]["to"] == "Belra"
    assert lines[0]["you"] is False and lines[0]["from"] == "P101"


def test_say_yell_emote_and_voice_channels_never_show(db):
    store, path = db
    sid = store.start_session(1.0)
    for channel in ("say", "yell", "emote", "voice:raid"):
        say(store, channel, 101, channel, session=sid)
    assert shown(path) == []


def test_paging_by_id_and_the_newest_first_limit(db):
    store, path = db
    sid = store.start_session(1.0)
    ids = [say(store, "guild", 101, f"line {i}", session=sid) for i in range(5)]
    assert shown(path, after=ids[2]) == ["line 3", "line 4"]
    assert shown(path, limit=2) == ["line 3", "line 4"]
    assert shown(path, after=ids[0], upto=ids[2]) == ["line 1", "line 2"]
    assert feed.read(path, feed.last_id) == ids[-1]


def test_without_his_guid_whispers_stay_out(store, tmp_path):
    say(store, "whisper", 101, "to someone", to=0)
    assert shown(tmp_path / "mind.db") == []


def test_reads_are_read_only(db):
    store, path = db
    conn = feed.connect(path)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO meta(key, value) VALUES ('x', 'y')")
    finally:
        conn.close()


def test_open_session_and_the_night(db):
    store, path = db
    assert feed.read(path, feed.open_session) is None
    sid = store.start_session(5.0)
    assert feed.read(path, feed.open_session) == {"id": sid, "started": 5.0}
    store.end_session(sid, 6.0)
    assert feed.read(path, feed.open_session) is None
    assert feed.read(path, feed.night) is None
    store.conn.execute("INSERT INTO night_run(night, window_start, window_end, status, started, "
                       "finished, error) VALUES ('2026-11-03', 1, 2, 'done', 3, 4, 'private')")
    store.conn.commit()
    assert feed.read(path, feed.night) == {"night": "2026-11-03", "status": "done",
                                           "started": 3, "finished": 4, "attempts": 1}
