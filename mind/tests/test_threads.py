from conftest import seed_test_cast
from leeroy_mind.memory.threads import ThreadTracker, thread_key, worth_extracting
from leeroy_mind.protocol import Chat
from leeroy_mind.storage.rows import ChatLineRow


def chat(channel="guild", frm=101, to=0, text="hello there", zone=0, via_bridge=False):
    return Chat(ts=0, channel=channel, from_guid=frm, from_name=f"N{frm}", to_guid=to, text=text,
                map=0, zone=zone, via_bridge=via_bridge)


def row(frm, text, i=1):
    return ChatLineRow(id=i, ts=0.0, channel="guild", from_guid=frm, from_name="x", to_guid=0,
                       text=text, map=0, zone=0, session_id=None, thread_id=None, origin="game")


def feed(store, tr, ev, ts):
    lid = store.insert_chat_line(ev, session_id=None, origin="game")
    return lid, tr.add(lid, ev, ts)


def test_thread_keys():
    assert thread_key(chat("guild")) == "guild" and thread_key(chat("officer")) == "officer"
    assert {thread_key(chat(c)) for c in ("party", "raid", "raid_warning")} == {"group"}
    assert {thread_key(chat(c, zone=7)) for c in ("say", "yell", "emote")} == {"say:7"}


def test_whisper_threads_are_per_pair():
    assert thread_key(chat("whisper", 1, 101)) == thread_key(chat("whisper", 101, 1)) == "whisper:1:101"
    assert thread_key(chat("whisper", 1, 102)) != thread_key(chat("whisper", 1, 101))


def test_thread_closes_on_12th_line(store):
    seed_test_cast(store)
    tr = ThreadTracker(store)
    ids = []
    for n in range(11):
        lid, closed = feed(store, tr, chat(), n)
        ids.append(lid)
        assert closed is None
    lid, closed = feed(store, tr, chat(), 11)
    ids.append(lid)
    assert closed.reason == "full" and closed.line_ids == tuple(ids) and closed.key == "guild"
    assert store.threads_pending() == [closed.thread_id]
    assert {r.thread_id for r in store.chat_lines(ids)} == {closed.thread_id}
    assert tr.expire(1000) == []
    _, nxt = feed(store, tr, chat(), 12)
    assert nxt is None


def test_thread_closes_after_90s_silence(store):
    seed_test_cast(store)
    tr = ThreadTracker(store)
    feed(store, tr, chat(), 0)
    assert tr.expire(89) == []
    out = tr.expire(91)
    assert len(out) == 1 and out[0].reason == "silence"
    assert tr.expire(200) == []


def test_late_line_closes_the_silent_thread_and_starts_a_new_one(store):
    seed_test_cast(store)
    tr = ThreadTracker(store)
    first, _ = feed(store, tr, chat(), 0)
    second, closed = feed(store, tr, chat(), 100)
    assert closed.line_ids == (first,) and closed.reason == "silence"
    flushed = tr.flush_all()
    assert len(flushed) == 1 and flushed[0].line_ids == (second,) and flushed[0].reason == "flush"
    assert tr.flush_all() == []


def test_channels_do_not_mix(store):
    seed_test_cast(store)
    tr = ThreadTracker(store)
    feed(store, tr, chat("guild"), 0)
    feed(store, tr, chat("officer"), 1)
    assert sorted(c.key for c in tr.flush_all()) == ["guild", "officer"]


def test_whispers_between_strangers_are_ignored(store):
    seed_test_cast(store)
    tr = ThreadTracker(store)
    _, closed = feed(store, tr, chat("whisper", 1, 999), 0)
    assert closed is None and tr.flush_all() == []
    feed(store, tr, chat("whisper", 1, 101), 1)
    assert [c.key for c in tr.flush_all()] == ["whisper:1:101"]


def test_bridge_echoes_never_thread_but_sent_lines_do(store):
    seed_test_cast(store)
    tr = ThreadTracker(store)
    echo = chat(via_bridge=True)
    lid = store.insert_chat_line(echo, session_id=None, origin="mind")
    assert tr.add(lid, echo, 0) is None and tr.flush_all() == []
    assert tr.on_line_sent(lid, echo) is None
    [t] = tr.flush_all()
    assert t.line_ids == (lid,)


def test_on_line_sent_joins_the_open_thread(store, clock):
    seed_test_cast(store)
    tr = ThreadTracker(store)
    a, _ = feed(store, tr, chat(frm=1), clock.now())
    ev = chat(frm=101)
    b = store.insert_chat_line(ev, session_id=None, origin="mind")
    tr.on_line_sent(b, ev)
    [t] = tr.flush_all()
    assert t.line_ids == (a, b)


def test_worth_extracting():
    assert not worth_extracting([row(101, "gz")], 1)
    assert worth_extracting([row(1, "gz")], 1)
    assert worth_extracting([row(101, "a" * 25), row(102, "b" * 25)], 1)
    assert not worth_extracting([row(101, "gz"), row(102, "ty")], 1)
