import sqlite3

import pytest

from leeroy_mind.discord.store import (
    DiscordRefused, changed_since, delete, edit, last_id, messages, open_thread, pin, pins, post,
    react, reactions, threads, unpin, unreact)


def hi(store, text="hi all", **kw):
    kw = {"channel": "general", "author": 1, "origin": "human", "now": 1.0, **kw}
    return post(store, text=text, **kw)


def test_a_deleted_parent_reads_as_deleted_and_takes_no_reactions(store):
    a = post(store, channel="general", author=1, origin="human", text="hi all", now=1.0)
    b = post(store, channel="general", author=101, origin="mind", text="hey 👋", reply_to=a, now=2.0)
    delete(store, a, author=1, now=3.0)
    rows = {m["id"]: m for m in messages(store.conn, "general")}
    assert rows[a]["text"] == "" and rows[a]["deleted"] == 3.0 and rows[b]["reply_to"] == a
    with pytest.raises(DiscordRefused, match="deleted"):
        react(store, a, member=101, emoji="😂", now=4.0)
    with pytest.raises(DiscordRefused, match="deleted_parent"):
        hi(store, "re", reply_to=a, now=5.0)


def test_post_refuses_bad_text_and_keeps_the_row_shape(store):
    with pytest.raises(DiscordRefused, match="empty"):
        hi(store, " ")
    with pytest.raises(DiscordRefused, match="too_long"):
        hi(store, "x" * 2001)
    a = hi(store, "hello\n\n\n\n\nthere", turn="t1", author=101, origin="mind")
    [m] = messages(store.conn, "general")
    assert m == {"id": a, "channel": "general", "thread": None, "author": 101, "origin": "mind",
                 "text": "hello\n\n\nthere", "reply_to": None, "ts": 1.0, "edited": None,
                 "deleted": None, "pinned": None}


def test_edit_is_only_for_the_author_and_marks_edited(store):
    a = hi(store)
    b = hi(store, "re", reply_to=a, author=101, origin="mind", now=2.0)
    with pytest.raises(DiscordRefused, match="not_yours"):
        edit(store, a, author=101, text="mine now", now=3.0)
    edit(store, a, author=1, text="hi everyone", now=4.0)
    rows = {m["id"]: m for m in messages(store.conn, "general")}
    assert rows[a]["text"] == "hi everyone" and rows[a]["edited"] == 4.0
    assert rows[b]["reply_to"] == a and rows[b]["edited"] is None
    with pytest.raises(DiscordRefused, match="empty"):
        edit(store, a, author=1, text="", now=5.0)
    delete(store, a, author=1, now=6.0)
    with pytest.raises(DiscordRefused, match="deleted"):
        edit(store, a, author=1, text="back", now=7.0)
    with pytest.raises(DiscordRefused, match="not_yours"):
        delete(store, b, author=1, now=8.0)


def test_delete_clears_reactions_and_pin_but_keeps_the_row(store):
    a = hi(store)
    react(store, a, member=101, emoji="👍", now=2.0)
    pin(store, a, now=2.0)
    delete(store, a, author=1, now=3.0)
    assert reactions(store.conn, [a]) == {}
    assert pins(store.conn, "general") == []
    [m] = messages(store.conn, "general")
    assert m["pinned"] is None and m["deleted"] == 3.0
    with pytest.raises(DiscordRefused, match="deleted"):
        pin(store, a, now=4.0)
    with pytest.raises(DiscordRefused, match="deleted"):
        delete(store, a, author=1, now=4.0)


def test_twenty_one_distinct_emoji_are_refused_and_one_member_reacts_once(store):
    a = hi(store)
    assert react(store, a, member=101, emoji="👍", now=2.0) is True
    assert react(store, a, member=101, emoji="👍", now=3.0) is False
    assert react(store, a, member=102, emoji="👍", now=3.0) is True
    for i in range(19):
        assert react(store, a, member=101, emoji=chr(0x1F600 + i), now=4.0)
    with pytest.raises(DiscordRefused, match="too_many"):
        react(store, a, member=101, emoji="🎉", now=5.0)
    assert react(store, a, member=103, emoji="👍", now=5.0)  # an existing emoji still takes more
    with pytest.raises(DiscordRefused, match="not_emoji"):
        react(store, a, member=101, emoji="lol", now=5.0)
    with pytest.raises(DiscordRefused, match="not_emoji"):
        react(store, a, member=101, emoji="👩‍" * 9 + "💻", now=5.0)
    up = [r for r in reactions(store.conn, [a])[a] if r["emoji"] == "👍"][0]
    assert up == {"emoji": "👍", "members": [101, 102, 103], "count": 3}
    assert unreact(store, a, member=101, emoji="👍", now=6.0) is True
    assert unreact(store, a, member=101, emoji="👍", now=6.0) is False


def test_a_reply_to_another_channel_is_refused(store):
    a = hi(store)
    with pytest.raises(DiscordRefused, match="no_parent"):
        hi(store, "x", channel="raids", reply_to=a)
    with pytest.raises(DiscordRefused, match="no_parent"):
        hi(store, "x", reply_to=999)


def test_pins_cap_at_fifty(store):
    ids = [hi(store, f"m{i}", now=float(i)) for i in range(51)]
    for i in ids[:50]:
        pin(store, i, now=100.0 + i)
    pin(store, ids[0], now=200.0)  # already pinned: not a 51st
    with pytest.raises(DiscordRefused, match="too_many_pins"):
        pin(store, ids[50], now=300.0)
    other = hi(store, "elsewhere", channel="raids")
    pin(store, other, now=300.0)  # the cap is per channel
    unpin(store, ids[3])
    pin(store, ids[50], now=301.0)
    got = pins(store.conn, "general")
    assert len(got) == 50 and got[0]["id"] == ids[50] and got[0]["pinned"] == 301.0


def test_one_thread_per_parent(store):
    a = hi(store)
    t = open_thread(store, a, name="  plans  for\nfriday ", now=2.0)
    with pytest.raises(DiscordRefused, match="exists"):
        open_thread(store, a, name="again", now=3.0)
    for bad in ("", "  ", "x" * 61):
        with pytest.raises(DiscordRefused, match="bad_name"):
            open_thread(store, hi(store, "other"), name=bad, now=3.0)
    r = hi(store, "in thread", thread=t, author=101, origin="mind", now=4.0)
    with pytest.raises(DiscordRefused, match="no_thread"):
        hi(store, "wrong room", thread=t, channel="raids")
    assert [m["id"] for m in messages(store.conn, "general", thread=t)] == [r]
    assert r not in [m["id"] for m in messages(store.conn, "general")]
    [th] = threads(store.conn, "general")
    assert th["id"] == t and th["parent"] == a and th["name"] == "plans for friday"
    assert th["count"] == 1
    d = hi(store, "gone")
    delete(store, d, author=1, now=5.0)
    with pytest.raises(DiscordRefused, match="deleted"):
        open_thread(store, d, name="n", now=6.0)


def test_reads_page_and_report_changes(store):
    ids = [hi(store, f"m{i}", now=float(i + 1)) for i in range(5)]
    assert last_id(store.conn) == ids[-1]
    assert [m["id"] for m in messages(store.conn, "general", limit=2)] == ids[3:]
    assert [m["id"] for m in messages(store.conn, "general", after=ids[1], limit=2)] == ids[2:4]
    assert messages(store.conn, "raids") == []
    edit(store, ids[0], author=1, text="m0!", now=10.0)
    react(store, ids[1], member=101, emoji="👍", now=11.0)
    pin(store, ids[2], now=12.0)
    delete(store, ids[3], author=1, now=13.0)
    assert changed_since(store.conn, 10.0) == ids[:4]
    assert changed_since(store.conn, 11.5) == [ids[2], ids[3]]
    assert changed_since(store.conn, 99.0) == []


def test_reads_work_on_a_read_only_connection(store):
    a = hi(store)
    react(store, a, member=101, emoji="🇬🇧", now=2.0)
    pin(store, a, now=3.0)
    t = open_thread(store, a, name="t", now=4.0)
    path = store.conn.execute("PRAGMA database_list").fetchone()[2]
    ro = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    ro.row_factory = sqlite3.Row
    try:
        assert [m["id"] for m in messages(ro, "general")] == [a]
        assert reactions(ro, [a])[a][0]["emoji"] == "🇬🇧"
        assert [p["id"] for p in pins(ro, "general")] == [a]
        assert threads(ro, "general")[0]["id"] == t
        assert last_id(ro) == a and changed_since(ro, 0.0) == [a]
    finally:
        ro.close()
