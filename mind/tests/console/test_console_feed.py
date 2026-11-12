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


def test_say_yell_emote_and_a_voice_line_that_never_played_stay_out(db):
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


# -- spoken lines ----------------------------------------------------------------------------------

from leeroy_mind.voice.store import VoiceStore  # noqa: E402


def spoken(store, sid, frm, text, room, state, *, channel=None, n=[0]):
    """A voice-room line (chat_line `voice:<room>`) and the voice_line row for how it played."""
    line = say(store, channel or f"voice:{room}", frm, text, session=sid)
    if state is not None:
        n[0] += 1
        VoiceStore(store).add_line(f"run.{n[0]}", line, room, state, 1.0)
    return line


def by_text(path, **kw):
    return {line["text"]: line for line in feed.read(path, feed.feed, **kw)}


def test_spoken_lines_join_the_feed_once_they_played_to_him(db):
    store, path = db
    sid = store.start_session(1.0)
    for state in ("done", "started", "cut", "sent", "unheard", "dropped"):
        spoken(store, sid, 101, f"line {state}", "guild_hall", state)
    spoken(store, sid, 101, "no voice row yet", "guild_hall", None)
    spoken(store, sid, 102, "officers talk", "officers", "done")
    mine = spoken(store, sid, HUMAN, "my spoken line", "guild_hall", None)
    VoiceStore(store).add_heard(mine, "my spoken line", 0.9, 800, 300)
    spoken(store, sid, HUMAN, "my unheard line", "guild_hall", None)
    spoken(store, None, 101, "outside a session", "guild_hall", "done")
    got = by_text(path)
    assert set(got) == {"line done", "line started", "line cut", "line sent",
                        "officers talk", "my spoken line"}
    assert [got[k]["heard"] for k in ("line done", "line started", "line cut")] == [True] * 3
    assert got["line sent"]["heard"] is False
    assert got["my spoken line"]["heard"] is True and got["my spoken line"]["you"] is True
    assert got["line done"]["voice"] == "guild_hall" and got["officers talk"]["voice"] == "officers"
    assert got["officers talk"]["channel"] == "voice:officers"


def test_a_spoken_whisper_is_marked(db):
    store, path = db
    line = say(store, "whisper", 101, "said aloud", to=HUMAN)
    VoiceStore(store).add_line("run.w", line, "whisper", "started", 1.0)
    say(store, "whisper", 102, "typed one", to=HUMAN)
    got = by_text(path)
    assert got["said aloud"]["voice"] == "whisper" and got["said aloud"]["heard"] is True
    assert got["typed one"]["voice"] is None and got["typed one"]["heard"] is None


def test_each_view_keeps_its_own_newest_lines(db):
    store, path = db
    sid = store.start_session(1.0)
    whispers = [say(store, "whisper", 101, f"w{i}", to=HUMAN) for i in range(3)]
    for i in range(250):
        say(store, "guild", 101, f"g{i}", session=sid)
    say(store, "officer", 102, "an officer line", session=sid)
    lines = feed.read(path, feed.feed, limit=200)
    assert [line["id"] for line in lines if line["channel"] == "whisper"] == whispers
    guild = [line["text"] for line in lines if line["channel"] == "guild"]
    assert len(guild) == 200 and guild[0] == "g50" and guild[-1] == "g249"
    assert "an officer line" in [line["text"] for line in lines]
    assert [line["id"] for line in lines] == sorted(line["id"] for line in lines)


def test_recheck_reports_what_played_and_what_went(db):
    store, path = db
    sid = store.start_session(1.0)
    voice = VoiceStore(store)
    a = spoken(store, sid, 101, "will play", "guild_hall", "sent")
    b = spoken(store, sid, 101, "will drop", "guild_hall", "sent")
    c = spoken(store, sid, 101, "still waiting", "guild_hall", "sent")
    rows = {r["line_id"]: r["speak_id"] for r in store.conn.execute(
        "SELECT line_id, speak_id FROM voice_line")}
    voice.set_line(rows[a], "done", None, 2.0)
    voice.set_line(rows[b], "dropped", "muted", 2.0)
    heard, gone = feed.read(path, feed.recheck, [a, b, c])
    assert [line["id"] for line in heard] == [a] and heard[0]["heard"] is True
    assert gone == [b]
    assert feed.read(path, feed.recheck, []) == ([], [])


def test_the_mood_reads_without_its_table(db, tmp_path):
    store, path = db
    assert feed.read(path, feed.guild_mood) is None
    with store.transaction():
        store.conn.execute("INSERT INTO guild_mood(night,value,reasons) VALUES (?,?,?)",
                           ("2026-11-01", 0.5, '["a kill", 7]'))
    assert feed.read(path, feed.guild_mood) == {"night": "2026-11-01", "value": 0.5,
                                                "reasons": ["a kill"]}
    with store.transaction():
        store.conn.execute("DROP TABLE guild_mood")
    assert feed.read(path, feed.guild_mood) is None


def test_a_mind_db_without_voice_tables_still_feeds(db):
    store, path = db
    sid = store.start_session(1.0)
    say(store, "guild", 101, "typed", session=sid)
    say(store, "voice:guild_hall", 101, "spoken", session=sid)
    with store.transaction():
        store.conn.execute("DROP TABLE voice_line")
        store.conn.execute("DROP TABLE voice_heard")
    assert shown(path) == ["typed"]


@pytest.mark.parametrize("reason", ["deafened", "muted"])
def test_a_voiced_whisper_he_did_not_hear_shows_as_text_marked(db, reason):
    """The owner's ruling (2026-11-06): a whisper the voice service dropped because he was
    deafened or had muted the member is never typed in WoW, so the feed shows it as text, marked,
    and never as heard."""
    store, path = db
    voice = VoiceStore(store)
    line = say(store, "whisper", 101, "while you were away", to=HUMAN)
    voice.add_line("run.u", line, "whisper", "sent", 1.0)
    voice.set_line("run.u", "dropped", reason, 2.0)
    at_once = say(store, "whisper", 102, "dropped before it played", to=HUMAN)
    voice.add_line("run.v", at_once, "whisper", "dropped", 1.0, reason=reason)
    say(store, "whisper", 103, "typed one", to=HUMAN)
    got = by_text(path)
    for text in ("while you were away", "dropped before it played"):
        assert got[text]["unheard"] == reason
        assert got[text]["voice"] is None and got[text]["heard"] is None
    assert got["typed one"]["unheard"] is None
    heard, gone = feed.read(path, feed.recheck, [line])  # a page that had it on its way
    assert [x["id"] for x in heard] == [line] and gone == []


@pytest.mark.parametrize("reason", ["expired", "moved", "no_voice", "offline", "cancelled",
                                    "engine_error", "mind_gone", "no_ack", "voice_down", None])
def test_a_voiced_whisper_dropped_for_another_reason_stays_out(db, reason):
    store, path = db
    line = say(store, "whisper", 101, "gone", to=HUMAN)
    VoiceStore(store).add_line("run.x", line, "whisper", "dropped", 1.0, reason=reason)
    assert shown(path) == []


def test_only_whispers_are_marked_not_heard(db):
    """A room line dropped as muted or deafened stays out (Settled 7), and every other line carries
    unheard None."""
    store, path = db
    sid = store.start_session(1.0)
    voice = VoiceStore(store)
    room = spoken(store, sid, 101, "room line", "guild_hall", "sent")
    voice.set_line(store.conn.execute("SELECT speak_id FROM voice_line WHERE line_id = ?",
                                      (room,)).fetchone()["speak_id"], "dropped", "deafened", 2.0)
    say(store, "guild", 101, "typed", session=sid)
    lines = feed.read(path, feed.feed)
    assert [x["text"] for x in lines] == ["typed"] and lines[0]["unheard"] is None


def test_each_whisper_thread_keeps_its_own_newest_lines(db):
    store, path = db
    for i in range(250):
        say(store, "whisper", 101, f"a{i}", to=HUMAN)
    quiet = say(store, "whisper", 102, "the quiet one", to=HUMAN)
    lines = feed.read(path, feed.feed, limit=200)
    assert quiet in [line["id"] for line in lines]
    assert len([line for line in lines if line["from_guid"] == 101]) == 200


def test_history_reads_a_view_back_to_its_first_line(db):
    store, path = db
    sid = store.start_session(1.0)
    ids = [say(store, "guild", 101, f"g{i}", session=sid) for i in range(250)]
    say(store, "officer", 102, "not guild", session=sid)
    page = feed.read(path, feed.history, "guild", ids[-1] + 10, 100)
    assert [line["text"] for line in page["lines"]] == [f"g{i}" for i in range(150, 250)]
    assert page["more"] is True
    page = feed.read(path, feed.history, "guild", page["lines"][0]["id"], 100)
    assert page["lines"][0]["text"] == "g50" and page["more"] is True
    page = feed.read(path, feed.history, "guild", page["lines"][0]["id"], 100)
    assert [line["text"] for line in page["lines"]] == [f"g{i}" for i in range(50)]
    assert page["more"] is False
    assert feed.read(path, feed.history, "guild", ids[0], 100) == {"lines": [], "more": False}


def test_a_whisper_threads_history_is_that_member_and_him_only(db):
    store, path = db
    theirs = [say(store, "whisper", 101, "to him", to=HUMAN),
              say(store, "whisper", HUMAN, "back", to=101, name="Owner")]
    say(store, "whisper", 102, "someone else", to=HUMAN)
    say(store, "whisper", 101, "between members", to=102)
    page = feed.read(path, feed.history, "w:101", 1 << 40)
    assert [line["id"] for line in page["lines"]] == theirs and page["more"] is False
    for view in ("whisper", "w:0", "voice:guild_hall", ""):
        with pytest.raises(ValueError):
            feed.read(path, feed.history, view, 1 << 40)


def test_the_voice_transcript_is_every_room_he_heard(db):
    store, path = db
    sid = store.start_session(1.0)
    hall = spoken(store, sid, 101, "how was your day", "guild_hall", "done")
    officers = spoken(store, sid, 102, "officer talk", "officers", "done")
    spoken(store, sid, 103, "a room he was not in", "kitchen", "unheard")
    say(store, "guild", 101, "typed", session=sid)
    page = feed.read(path, feed.history, "voice", 1 << 40)
    assert [line["id"] for line in page["lines"]] == [hall, officers]
    assert [(line["from"], line["voice"]) for line in page["lines"]] == [
        ("P101", "guild_hall"), ("P102", "officers")]


def test_threads_list_every_member_he_whispered_with_newest_first(db):
    store, path = db
    say(store, "whisper", 101, "long ago", to=HUMAN)
    say(store, "whisper", HUMAN, "to Coros", to=103, name="Owner")
    newest = say(store, "whisper", 102, "latest", to=HUMAN, name="Belra")
    say(store, "whisper", 101, "between members", to=102)
    got = feed.read(path, feed.threads)
    assert [(t["guid"], t["name"]) for t in got] == [(102, "Belra"), (103, "Coros"), (101, "P101")]
    assert got[0]["last"]["id"] == newest and got[0]["last"]["text"] == "latest"


def test_history_and_threads_hold_back_a_whisper_inside_the_grace(db):
    store, path = db
    line = say(store, "whisper", 101, "just now", to=HUMAN)
    ts = store.conn.execute("SELECT ts FROM chat_line WHERE id = ?", (line,)).fetchone()[0]
    assert feed.read(path, feed.history, "w:101", 1 << 40, now=ts + 1)["lines"] == []
    assert feed.read(path, feed.threads, now=ts + 1) == []
    later = feed.read(path, feed.history, "w:101", 1 << 40, now=ts + 5)["lines"]
    assert [x["id"] for x in later] == [line]
    assert [t["guid"] for t in feed.read(path, feed.threads, now=ts + 5)] == [101]


def test_threads_skip_a_whisper_still_on_its_way(db):
    store, path = db
    shown_line = say(store, "whisper", HUMAN, "his answer", to=101, name="Owner")
    pending = say(store, "whisper", 101, "queued on voice", to=HUMAN)
    VoiceStore(store).add_line("w.pending", pending, "whisper", "sent", 1.0)
    got = feed.read(path, feed.threads)
    assert [(t["guid"], t["last"]["id"]) for t in got] == [(101, shown_line)]
    VoiceStore(store).set_line("w.pending", "done", None, 2.0)
    assert feed.read(path, feed.threads)[0]["last"]["id"] == pending


def test_a_snapshot_stays_far_under_the_event_frame_limit(db):
    import json
    store, path = db
    sid = store.start_session(1.0)
    text = "x" * 250
    for i in range(40 * 100):
        say(store, "whisper", 101 + i % 40, text, to=HUMAN, name=f"P{i % 40}")
    for i in range(300):
        say(store, "guild", 101, text, session=sid)
    quiet = say(store, "officer", 102, "one officer line", session=sid)
    lines = feed.read(path, feed.feed, limit=feed.FEED_LIMIT + 1)
    kept, partial = feed.snapshot_lines(lines)
    assert len(json.dumps(kept)) < 600_000
    assert len([x for x in kept if x["channel"] == "whisper"]) == feed.SNAPSHOT_WHISPERS
    assert len([x for x in kept if x["channel"] == "guild"]) == feed.FEED_LIMIT
    assert "guild" in partial and "officer" not in partial and quiet in [x["id"] for x in kept]
    assert len([v for v in partial if v.startswith("w:")]) == 40  # every thread was cut
    assert [x["id"] for x in kept] == sorted(x["id"] for x in kept)


def test_history_leaves_out_a_whisper_still_on_its_way(db):
    store, path = db
    typed = say(store, "whisper", 101, "typed", to=HUMAN)
    pending = say(store, "whisper", 101, "queued on voice", to=HUMAN)
    VoiceStore(store).add_line("w.queued", pending, "whisper", "sent", 1.0)
    page = feed.read(path, feed.history, "w:101", 1 << 40)
    assert [x["id"] for x in page["lines"]] == [typed]
    VoiceStore(store).set_line("w.queued", "done", None, 2.0)
    page = feed.read(path, feed.history, "w:101", 1 << 40)
    assert [x["id"] for x in page["lines"]] == [typed, pending]
