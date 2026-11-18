"""GET /api/unread: what reached him since the ids the page holds, for the tab title while the
page is hidden. Counts, the newest sender's name and row ids only. Every name is invented."""
import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import HUMAN, seed_test_cast
from console.rig import http, make_app, running
from leeroy_mind.console import feed, unreadview
from leeroy_mind.discord import store as dstore
from leeroy_mind.protocol import Chat

NOW = 1_800_000_000.0
SECRET = "PRIVATE_SENTINEL the words of the message"


@pytest.fixture
def data(store):
    return Path(store.conn.execute("PRAGMA database_list").fetchone()["file"]).parent


@pytest.fixture
def cast(store):
    seed_test_cast(store)
    store.set_meta("human_guid", str(HUMAN))
    store.set_meta("human_name", "Hero")
    store.conn.commit()
    return store


def whisper(store, frm, to, text=SECRET):
    return store.insert_chat_line(
        Chat(ts=0, channel="whisper", from_guid=frm, from_name="Arkon" if frm == 101 else "Hero", to_guid=to,
             text=text, map=0, zone=0), session_id=None, origin="mind" if frm != HUMAN else "human")


def read(data, **kw):
    kw.setdefault("now", NOW)
    return feed.read(data / "mind.db", unreadview.unread_view, **kw)


def test_nothing_is_counted_before_the_page_has_a_mark(data, cast):
    whisper(cast, 101, HUMAN)
    posted = dstore.post(cast, channel="dm:101", author=101, origin="mind", text=SECRET, now=NOW)
    cast.conn.commit()
    v = read(data, chat=None, discord=None)
    assert (v["whispers"], v["dms"], v["mentions"], v["latest"]) == (0, 0, 0, None)
    assert v["chat_top"] >= 1 and v["discord_top"] == posted


def test_an_unread_dm_is_never_lost_behind_newer_channel_rows(data, cast):
    cast.conn.commit()
    top = read(data, chat=None, discord=None)
    dstore.post(cast, channel="dm:102", author=102, origin="mind", text=SECRET, now=NOW - 5000)
    dstore.post(cast, channel="gdm:1", author=101, origin="mind", text=SECRET, now=NOW - 4999)
    dstore.post(cast, channel="general", author=101, origin="mind", text="@hero early one", now=NOW - 4998)
    for i in range(unreadview.SCAN + 100):
        dstore.post(cast, channel="general", author=101, origin="mind", text="guild talk", now=NOW - 4000 + i)
    cast.conn.commit()
    v = read(data, chat=top["chat_top"], discord=top["discord_top"], now=NOW + 60)
    assert v["dms"] == 2 and v["mentions"] == 0     # the mention scan stays bounded, the DM count is whole
    assert v["latest"]["kind"] == "dm" and v["latest"]["name"] == "Arkon"
    assert SECRET not in json.dumps(v)


def test_whispers_dms_and_mentions_after_the_marks(data, cast):
    old = whisper(cast, 101, HUMAN)
    mine = dstore.post(cast, channel="general", author=HUMAN, origin="human", text="who is on tonight", now=NOW - 50)
    cast.conn.commit()
    top = read(data, chat=None, discord=None)
    whisper(cast, 101, HUMAN)                    # counted
    whisper(cast, HUMAN, 101, "my own line")     # his own: never
    whisper(cast, 101, 102)                      # between members: not his
    dstore.post(cast, channel="dm:102", author=102, origin="mind", text=SECRET, now=NOW - 5)
    dstore.post(cast, channel="dm:102", author=HUMAN, origin="human", text="mine", now=NOW - 4)
    dstore.post(cast, channel="general", author=101, origin="mind", text="nothing about him", now=NOW - 3)
    dstore.post(cast, channel="general", author=101, origin="mind", text="@hero are you in? " + SECRET, now=NOW - 2)
    dstore.post(cast, channel="general", author=102, origin="mind", text="yes " + SECRET,
                reply_to=mine, now=NOW - 1)
    dstore.post(cast, channel="general", author=101, origin="mind", text="@heroic is not him", now=NOW - 1)
    cast.conn.commit()
    v = read(data, chat=top["chat_top"], discord=top["discord_top"], now=NOW + 60)
    assert (v["whispers"], v["dms"], v["mentions"]) == (1, 1, 2)
    assert v["latest"]["kind"] in ("whisper", "mention") and v["latest"]["name"] in ("Arkon", "Belra")
    assert SECRET not in json.dumps(v) and "PRIVATE" not in json.dumps(v)
    assert old < top["chat_top"] + 1
    # everything read: nothing new
    again = read(data, chat=v["chat_top"], discord=v["discord_top"], now=NOW + 60)
    assert (again["whispers"], again["dms"], again["mentions"], again["latest"]) == (0, 0, 0, None)


def test_a_deleted_message_is_not_counted(data, cast):
    cast.conn.commit()
    top = read(data, chat=None, discord=None)
    gone = dstore.post(cast, channel="dm:101", author=101, origin="mind", text=SECRET, now=NOW)
    cast.conn.execute("UPDATE discord_message SET deleted = ? WHERE id = ?", (NOW, gone))
    cast.conn.commit()
    assert read(data, chat=top["chat_top"], discord=top["discord_top"])["dms"] == 0


def test_an_older_database_counts_nothing(tmp_path):
    import sqlite3
    sqlite3.connect(tmp_path / "mind.db").close()
    v = feed.read(tmp_path / "mind.db", unreadview.unread_view, chat=0, discord=0, now=NOW)
    assert (v["whispers"], v["dms"], v["mentions"], v["chat_top"], v["discord_top"]) == (0, 0, 0, 0, 0)


@asynccontextmanager
async def served(app):
    await app.start()
    try:
        yield app
    finally:
        await app.close()


def test_the_route_needs_the_key_and_row_ids(data, cast):
    whisper(cast, 101, HUMAN)
    cast.conn.commit()

    async def go():
        linked = [SimpleNamespace(state=5, remote_port=7878, remote="127.0.0.1", pid=101)]
        async with served(make_app(data, mind=running(data), tcp=lambda: linked, clock=lambda: NOW + 60)) as app:
            ok = await http(app, "GET", "/api/unread?chat=0&discord=0")
            first = await http(app, "GET", "/api/unread")
            bad = await http(app, "GET", "/api/unread?chat=abc")
            nokey = await http(app, "GET", "/api/unread?chat=0", key=False)
            return ok, first, bad, nokey
    ok, first, bad, nokey = asyncio.run(go())
    assert ok.status == 200 and ok.json()["whispers"] == 1 and ok.json()["latest"]["name"] == "Arkon"
    assert SECRET not in json.dumps(ok.json())
    assert first.status == 200 and first.json()["whispers"] == 0 and first.json()["chat_top"] >= 1
    assert bad.status == 400 and nokey.status == 403
    assert len(ok.body) < 400
