"""The console's side of the guild's Discord: GET /api/discord (the server, members with the
presence the mind computes, one channel's messages with reactions, threads and pins; read-only),
POST /api/discord (checked as the mind checks it, then on to the mind's discord op), and the
`discord` event the feed pass sends when a channel moved. A real server on a free port; the
mind and its link are fakes, and every name is invented."""
import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo
from urllib.parse import quote

import pytest

from conftest import HUMAN, seed_test_cast
from engine_rig import save_member
from leeroy_mind import clock as mind_clock
from leeroy_mind.console import app as console_app, mindlink
from leeroy_mind.console.control import Controls
from leeroy_mind.console.mindlink import LinkError
from leeroy_mind.discord import store as dstore
from console.rig import FakeMind, http, make_app, running

LINKED = [SimpleNamespace(state=5, remote_port=7878, remote="127.0.0.1", pid=101)]
EASTERN = ZoneInfo("America/New_York")
# Thursday evening in US Eastern, midnight in Britain during the DST-change gap.
NOW = datetime(2026, 10, 29, 20, 0, tzinfo=EASTERN).timestamp()


@pytest.fixture(autouse=True)
def eastern_pc(monkeypatch):
    """This PC's zone (clock.local_at, the mind's local time) as US Eastern, so presence reads
    the same on any test machine."""
    monkeypatch.setattr(mind_clock, "local_at", lambda ts: datetime.fromtimestamp(ts, EASTERN))


@pytest.fixture
def data(store):
    return Path(store.conn.execute("PRAGMA database_list").fetchone()["file"]).parent


@pytest.fixture
def cast(store):
    seed_test_cast(store)
    store.set_meta("human_guid", str(HUMAN))
    store.set_meta("human_name", "Hero")
    save_member(store, 101, region="US")
    save_member(store, 102, region="UK")
    store.conn.commit()
    return store


class FakeLink:
    def __init__(self, reply=None, *, error=None):
        self.calls, self.timeouts, self.reply, self.error = [], [], reply, error

    def locate(self, pids):
        return {"pid": 101, "port": 1, "key": "x"}

    def call(self, pids, op, args=None, *, timeout=3.0):
        self.calls.append((op, args))
        self.timeouts.append(timeout)
        if self.error:
            raise LinkError(self.error)
        return self.reply


@asynccontextmanager
async def served(app):
    await app.start()
    try:
        yield app
    finally:
        await app.close()


def build(data, **kw):
    kw.setdefault("clock", lambda: NOW)
    return make_app(data, mind=kw.pop("mind", None) or running(data), tcp=lambda: LINKED, **kw)


def get(app, query=""):
    return http(app, "GET", "/api/discord" + query)


def post(app, body, **kw):
    return http(app, "POST", "/api/discord", body=body, origin=True, **kw)


def test_the_server_lists_categories_channels_dms_and_members_with_presence(data, cast):
    dstore.post(cast, channel="general", author=101, origin="mind", text="hi", now=NOW)
    dstore.post(cast, channel="dm:101", author=HUMAN, origin="human", text="psst", now=NOW)
    cast.conn.commit()

    async def go():
        async with served(build(data)) as app:
            return (await get(app)).json()
    v = asyncio.run(go())
    assert [c["key"] for c in v["categories"]] == ["info", "general", "raiding", "wow", "officers"]
    general = next(c for cat in v["categories"] for c in cat["channels"] if c["id"] == "general")
    assert general["last_id"] == 1 and general["officers_only"] is False and general["topic"]
    officers = next(cat for cat in v["categories"] if cat["key"] == "officers")
    assert officers["channels"] and all(c["officers_only"] for c in officers["channels"])
    assert "messages" not in v and v["missing"] == []
    assert v["human"] == {"guid": HUMAN, "name": "Hero"}
    dms = {d["guid"]: d["last_id"] for d in v["dms"]}
    assert dms[101] == 2 and dms[102] == 0
    members = {m["guid"]: m for m in v["members"]}
    assert members[101]["presence"]["state"] == "online"      # 20:00 where he is
    assert members[102]["presence"]["state"] == "offline"     # midnight in the UK
    assert members[101]["class_key"] == "warrior" and members[101]["role"] != "trial"
    assert set(members[101]) == {"guid", "name", "class_key", "role", "presence"}


def test_presence_and_roles_follow_the_minds_rules(data, cast):
    """The mind's own function, seed and clock, and the trial rank [realm] names."""
    from leeroy_mind.discord import presence
    from leeroy_mind.personas.io import load_persona
    p = load_persona(cast, 101)
    want = presence.presence_of(p, local=presence.local_time(datetime.fromtimestamp(NOW, presence.OWNER_ZONE),
                                                             p.identity.region),
                                body_online=False, body_zone=0, rng_seed=presence.RNG_SEED)
    (data / "mind.toml").write_text("[realm]\ntrial_rank = 3\n")

    async def go():
        async with served(build(data)) as app:
            return (await get(app)).json()
    members = {m["guid"]: m for m in asyncio.run(go())["members"]}
    assert members[101]["presence"] == {"state": want.state, "playing": want.playing,
                                        "zone": want.zone}
    assert members[101]["role"] == "trial"   # the cast is rank 3, the configured trial rank


def test_a_channel_returns_messages_with_reactions_threads_and_pins(data, cast):
    a = dstore.post(cast, channel="general", author=101, origin="mind", text="one", now=NOW)
    b = dstore.post(cast, channel="general", author=HUMAN, origin="human", text="two",
                    reply_to=a, now=NOW + 1)
    t = dstore.open_thread(cast, a, name="side", now=NOW + 2)
    dstore.post(cast, channel="general", author=102, origin="mind", text="in thread", thread=t,
                now=NOW + 3)
    dstore.react(cast, a, member=HUMAN, emoji="\U0001F44D", now=NOW + 4)
    dstore.pin(cast, b, now=NOW + 5)
    cast.conn.commit()

    async def go():
        async with served(build(data)) as app:
            return ((await get(app, "?channel=general")).json(),
                    (await get(app, f"?channel=general&after={a}")).json(),
                    (await get(app, f"?channel=general&thread={t}")).json(),
                    (await get(app, "?channel=nowhere")).status)
    full, later, in_thread, bad = asyncio.run(go())
    assert [m["text"] for m in full["messages"]] == ["one", "two"]
    assert full["messages"][0]["reactions"] == [
        {"emoji": "\U0001F44D", "members": [HUMAN], "count": 1}]
    assert full["messages"][1]["reply_to"] == a and full["messages"][1]["reactions"] == []
    assert [x["id"] for x in full["threads"]] == [t] and full["threads"][0]["count"] == 1
    assert [x["id"] for x in full["pins"]] == [b]
    assert [m["text"] for m in later["messages"]] == ["two"]
    assert [m["text"] for m in in_thread["messages"]] == ["in thread"]
    assert bad == 400


def test_large_thread_shelf_keeps_recent_threads_reopenable(data, cast):
    ids = []
    for i in range(105):
        parent = dstore.post(cast, channel="general", author=101, origin="mind",
                             text=f"fixture {i}", now=NOW + i)
        ids.append(dstore.open_thread(cast, parent, name=f"thread {i}", now=NOW + i))
    async def go():
        async with served(build(data)) as app:
            shelf = (await get(app, "?channel=general")).json()
            reopened = await get(app, f"?channel=general&thread={ids[-1]}")
            return shelf, reopened.status
    shelf, status = asyncio.run(go())
    assert shelf["limited"]["threads"] and status == 200
    assert [t["id"] for t in shelf["threads"]] == ids[-100:][::-1]


def test_he_sees_the_officers_channel(data, cast):
    dstore.post(cast, channel="officer-chat", author=102, origin="mind", text="private", now=NOW)
    cast.conn.commit()

    async def go():
        async with served(build(data)) as app:
            return (await get(app, "?channel=officer-chat")).json()
    v = asyncio.run(go())
    assert [m["text"] for m in v["messages"]] == ["private"]


def test_dense_unicode_window_is_bounded_without_slicing_messages(data, cast, monkeypatch):
    from leeroy_mind.console import discordview
    from leeroy_mind.console.httpd import encode_json
    text = "\U0001f680" * 2000
    parents = []
    for i in range(101):
        parent = dstore.post(cast, channel="general", author=101, origin="mind", text=text, now=NOW + i)
        parents.append(parent)
        dstore.open_thread(cast, parent, name=f"Fixture thread {i}", now=NOW + i)
        if i < 50:
            dstore.pin(cast, parent, now=NOW + i)
    replies = [dstore.post(cast, channel="general", author=102, origin="mind", text=text,
                          reply_to=parent, now=NOW + 200 + i) for i, parent in enumerate(parents)]
    with monkeypatch.context() as patch:
        patch.setattr(discordview, "_bounded_window", lambda out, **kw: out)
        before = discordview.discord_view(cast.conn, owner_now=datetime.fromtimestamp(NOW, EASTERN),
                                         trial_rank=3, channel="general")
        before_bytes = len(encode_json(before).encode("ascii"))
    async def go():
        async with served(build(data)) as app:
            return await get(app, "?channel=general"), await get(app, f"?channel=general&after={replies[0]}")
    response, cursor = asyncio.run(go())
    value = response.json()
    print("dense Discord wire bytes", before_bytes, "->", len(response.body))
    assert before_bytes > 2_000_000
    assert response.status == 200 and len(response.body) <= discordview.WINDOW_BYTES
    assert 0 < len(value["messages"]) < 100
    assert value["messages"][-1]["id"] == replies[-1]
    assert all(row["text"] == text for key in ("messages", "quotes", "pins") for row in value[key])
    assert all(value["limited"][key] for key in ("messages", "quotes", "pins", "threads"))
    assert cursor.json()["messages"][0]["id"] == replies[1]
    assert len(cursor.body) <= discordview.WINDOW_BYTES
    digest = discordview.watched(cast.conn, "general")
    assert len(digest) == 64
    dstore.edit(cast, replies[-1], author=102, text="Changed fixture", now=NOW + 999)
    assert discordview.watched(cast.conn, "general") != digest


def test_an_older_mind_db_lists_the_tables_as_missing(data, cast):
    for t in ("discord_reaction", "discord_thread", "discord_message"):
        cast.conn.execute(f"DROP TABLE {t}")
    cast.conn.commit()

    async def go():
        async with served(build(data)) as app:
            return (await get(app, "?channel=general")).json()
    v = asyncio.run(go())
    assert set(v["missing"]) == {"discord_message", "discord_thread", "discord_reaction"}
    assert v["messages"] == [] and v["threads"] == [] and v["pins"] == []
    assert v["categories"] and v["members"]


def test_posting_relays_to_the_mind_and_broadcasts(data, cast):
    link = FakeLink({"ok": True, "id": 7})

    async def go():
        async with served(build(data, link=link)) as app:
            seen = []
            app.broadcast = lambda kind, d: seen.append((kind, d))
            r = await post(app, {"action": "post", "channel": "general", "text": "  hi  \n there "})
            assert r.status == 200 and r.json() == {"ok": True, "id": 7}
            assert ("discord", {"channel": "general", "id": 7}) in seen
            bad = await post(app, {"action": "post", "channel": "nowhere", "text": "x"})
            assert bad.status == 400
    asyncio.run(go())
    assert [c[0] for c in link.calls] == ["discord"]
    assert link.calls[0][1]["channel"] == "general" and link.calls[0][1]["action"] == "post"
    assert mindlink.DISCORD_S == 10.0 and link.timeouts == [mindlink.DISCORD_S]


def test_the_minds_refusal_comes_back_as_409_with_its_words(data, cast):
    link = FakeLink({"ok": False, "error": "That message was deleted."})

    async def go():
        async with served(build(data, link=link)) as app:
            seen = []
            app.broadcast = lambda kind, d: seen.append(kind)
            r = await post(app, {"action": "react", "message": 3, "emoji": "\U0001F44D"})
            assert r.status == 409 and r.json() == {"ok": False,
                                                    "error": "That message was deleted."}
            assert "discord" not in seen
    asyncio.run(go())


def test_posting_with_control_off_is_403(data, cast):
    link = FakeLink({"ok": True, "id": 1})
    off = Controls(enabled=False, reason="process control is off (--no-control)",
                   mind=running(data))

    async def go():
        async with served(build(data, controls=off, link=link)) as app:
            r = await post(app, {"action": "post", "channel": "general", "text": "hi"})
            assert r.status == 403 and r.json()["ok"] is False
            assert (await get(app)).status == 200   # reading still works
    asyncio.run(go())
    assert link.calls == []


def test_posting_with_no_mind_or_link_is_409(data, cast):
    body = {"action": "post", "channel": "general", "text": "hi"}

    async def go():
        async with served(build(data, mind=FakeMind(data), link=FakeLink({"ok": True}))) as app:
            assert (await post(app, body)).status == 409
        async with served(build(data)) as app:  # no link
            assert (await post(app, body)).status == 409
        async with served(build(data, link=FakeLink(error="the mind is not running"))) as app:
            r = await post(app, body)
            assert r.status == 409 and "not running" in r.json()["error"]
    asyncio.run(go())


def test_discord_needs_the_key_and_host(data, cast):
    link = FakeLink({"ok": True, "id": 1})
    body = {"action": "post", "channel": "general", "text": "hi"}

    async def go():
        async with served(build(data, link=link)) as app:
            assert (await http(app, "GET", "/api/discord", key=False)).status == 403
            assert (await http(app, "GET", "/api/discord", host="evil.example")).status == 403
            assert (await http(app, "POST", "/api/discord", body=body)).status == 403  # no Origin
            assert (await http(app, "POST", "/api/discord", body=body, key=False,
                               origin=True)).status == 403
            assert (await http(app, "POST", "/api/discord", body=body,
                               host="evil.example", origin=True)).status == 403
    asyncio.run(go())
    assert link.calls == []


async def settled(app):
    """The loop's own baseline has run (it races a test's posts otherwise), then take it again."""
    for _ in range(500):
        if app._discord_last is not None:
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)
    await app._discord_baseline()


def test_unread_counts_rows_after_each_channel_mark_and_omits_own_deleted_and_thread_lines(data, cast):
    first = dstore.post(cast, channel="general", author=101, origin="mind", text="first", now=NOW)
    for _ in range(5):
        dstore.post(cast, channel="pics", author=101, origin="mind", text="elsewhere", now=NOW)
    dstore.post(cast, channel="general", author=101, origin="mind", text="second", now=NOW)
    dstore.post(cast, channel="general", author=HUMAN, origin="human", text="mine", now=NOW)
    gone = dstore.post(cast, channel="general", author=101, origin="mind", text="gone", now=NOW)
    dstore.delete(cast, gone, author=101, now=NOW)
    thread = dstore.open_thread(cast, first, name="a thread", now=NOW)
    dstore.post(cast, channel="general", thread=thread, author=101, origin="mind", text="thread", now=NOW)
    dstore.post(cast, channel="dm:102", author=102, origin="mind", text="private", now=NOW)
    cast.conn.commit()

    async def go():
        async with served(build(data)) as app:
            query = "?read=" + quote(json.dumps({"general": first, "dm:102": 0}))
            response = await get(app, query)
            assert response.status == 200
            view = response.json()
            channels = {c["id"]: c for cat in view["categories"] for c in cat["channels"]}
            assert channels["general"]["unread"] == 1
            assert channels["pics"]["unread"] == 0  # no mark: initial history is not unread
            assert next(d for d in view["dms"] if d["guid"] == 102)["unread"] == 1
            for value in ([], {"general": True}, {"general": -1}, {"general": 2**53}, {"unknown": 0}):
                assert (await get(app, "?read=" + quote(json.dumps(value)))).status == 400
            assert (await get(app, "?read=not-json")).status == 400
    asyncio.run(go())


def test_quotes_outside_the_loaded_window_keep_a_deleted_placeholder(data, cast):
    parent = dstore.post(cast, channel="general", author=101, origin="mind", text="old secret text", now=NOW)
    for _ in range(101):
        dstore.post(cast, channel="general", author=101, origin="mind", text="later", now=NOW)
    dstore.post(cast, channel="general", author=HUMAN, origin="human", text="reply", reply_to=parent, now=NOW)
    dstore.delete(cast, parent, author=101, now=NOW)
    cast.conn.commit()

    async def go():
        async with served(build(data)) as app:
            view = (await get(app, "?channel=general")).json()
            assert len(view["messages"]) == 100
            assert view["quotes"] == [{"id": parent, "author": 101, "text": "", "deleted": NOW}]
            assert "old secret text" not in json.dumps(view)
    asyncio.run(go())


def test_loaded_thread_unreact_and_old_pin_removal_refresh_the_channel(data, cast):
    parent = dstore.post(cast, channel="general", author=HUMAN, origin="human", text="parent", now=NOW - 100)
    thread = dstore.open_thread(cast, parent, name="thread", now=NOW - 100)
    child = dstore.post(cast, channel="general", thread=thread, author=101, origin="mind", text="child", now=NOW - 100)
    dstore.react(cast, child, member=HUMAN, emoji="👍", now=NOW - 100)
    dstore.pin(cast, parent, now=NOW - 100)
    for _ in range(101):
        dstore.post(cast, channel="general", author=101, origin="mind", text="later", now=NOW - 100)
    cast.conn.commit()

    async def go():
        async with served(build(data)) as app:
            seen = collect(app)
            await settled(app)
            await get(app, "?channel=general&thread=" + str(thread))
            for change in (lambda: dstore.unreact(cast, child, member=HUMAN, emoji="👍", now=0),
                           lambda: dstore.unpin(cast, parent)):
                seen.clear()
                await app._feed_pass()
                assert not seen
                change()
                cast.conn.commit()
                await app._feed_pass()
                assert [d for kind, d in seen if kind == "discord"] == [{"channels": ["general"]}]
    asyncio.run(go())


def collect(app):
    seen = []
    app.broadcast = lambda kind, d: seen.append((kind, d))
    return seen


def test_a_new_mind_line_triggers_a_discord_event(data, cast):
    async def go():
        async with served(build(data)) as app:
            seen = collect(app)
            await settled(app)
            await app._feed_pass()
            assert [k for k, _ in seen if k == "discord"] == []
            dstore.post(cast, channel="general", author=101, origin="mind", text="hi", now=NOW)
            cast.conn.commit()
            await app._feed_pass()
            await app._feed_pass()
            return [d for k, d in seen if k == "discord"]
    assert asyncio.run(go()) == [{"channels": ["general"]}]


def test_a_burst_across_channels_is_never_dropped(data, cast):
    async def go():
        async with served(build(data)) as app:
            seen = collect(app)
            await settled(app)
            for ch in ("general", "pics", "general", "irl"):
                dstore.post(cast, channel=ch, author=101, origin="mind", text="x", now=NOW)
            cast.conn.commit()
            await app._feed_pass()
            return [d for k, d in seen if k == "discord"]
    (event,) = asyncio.run(go())
    assert sorted(event["channels"]) == ["general", "irl", "pics"]


def test_changes_to_a_loaded_window_show_without_timestamps(data, cast):
    """Unreact, unpin, edit and delete: the pass re-reads the loaded window and diffs it."""
    a = dstore.post(cast, channel="general", author=HUMAN, origin="human", text="one", now=NOW - 100)
    b = dstore.post(cast, channel="general", author=HUMAN, origin="human", text="two", now=NOW - 100)
    dstore.react(cast, a, member=101, emoji="\U0001F44D", now=NOW - 100)
    dstore.pin(cast, b, now=NOW - 100)
    cast.conn.commit()

    async def go():
        async with served(build(data)) as app:
            seen = collect(app)
            await settled(app)
            await get(app, "?channel=general")     # the page has this window loaded
            outcomes = []
            for change in (lambda: dstore.unreact(cast, a, member=101, emoji="\U0001F44D", now=0),
                           lambda: dstore.unpin(cast, b),
                           lambda: dstore.edit(cast, a, author=HUMAN, text="uno", now=0),
                           lambda: dstore.delete(cast, b, author=HUMAN, now=0)):
                seen.clear()
                await app._feed_pass()                   # quiet
                assert seen == []
                change()
                cast.conn.commit()
                await app._feed_pass()
                outcomes.append([d for k, d in seen if k == "discord"])
            seen.clear()
            await app._feed_pass()
            return outcomes, seen
    outcomes, after = asyncio.run(go())
    assert outcomes == [[{"channels": ["general"]}]] * 4
    assert after == []


def test_the_pass_with_no_discord_tables_is_quiet(data, cast):
    for t in ("discord_reaction", "discord_thread", "discord_message"):
        cast.conn.execute(f"DROP TABLE {t}")
    cast.conn.commit()

    async def go():
        async with served(build(data)) as app:
            seen = collect(app)
            await settled(app)
            await app._feed_pass()
            return seen
    assert [k for k, _ in asyncio.run(go()) if k == "discord"] == []


def test_typing_is_asked_for_while_a_discord_page_listens_even_out_of_game(data, cast):
    entry = {"guid": 101, "name": "Arkon", "channel": "discord:general", "to": None,
             "since": 1.0, "age": 1.0}
    link = FakeLink({"ok": True, "typing": [entry]})

    async def go():
        async with served(build(data, link=link)) as app:
            assert app.game.state != "in_game"
            app._subs.add(console_app._Subscriber())
            await app.refresh()
            assert link.calls and app._typing == []  # shared GPU status, no Discord dots yet
            await get(app)                                  # a page opens the Discord
            await app.refresh()
            return app._typing
    typing = asyncio.run(go())
    assert [e["channel"] for e in typing] == ["discord:general"]
    assert all(t <= 1.0 for t in link.timeouts)


def test_tables_that_appear_after_the_baseline_are_announced(data, cast):
    """A console started against a mind.db without the Discord tables, and a mind that migrates
    later: the cursor stays at 0, so the first message is news."""
    schema = [r[0] for r in cast.conn.execute(
        "SELECT sql FROM sqlite_master WHERE name LIKE 'discord%' AND sql IS NOT NULL "
        "ORDER BY CASE name WHEN 'discord_thread' THEN 0 WHEN 'discord_message' THEN 1 "
        "WHEN 'discord_reaction' THEN 2 ELSE 3 END")]
    for t in ("discord_group_member", "discord_group", "discord_voice", "discord_attachment", "discord_reaction", "discord_message", "discord_thread"):
        cast.conn.execute(f"DROP TABLE {t}")
    cast.conn.commit()

    async def go():
        async with served(build(data)) as app:
            seen = collect(app)
            await settled(app)
            assert app._discord_last == 0
            for sql in schema:
                cast.conn.execute(sql)
            dstore.post(cast, channel="general", author=101, origin="mind", text="hi", now=NOW)
            cast.conn.commit()
            await app._feed_pass()
            return [d for k, d in seen if k == "discord"]
    assert asyncio.run(go()) == [{"channels": ["general"]}]


def test_members_are_the_minds_audience_never_him_or_below_the_cast_level(data, cast):
    """The stored roster keeps his own row and members below level 80; the mind's Discord
    audience has neither, so the member list and the DMs leave them out."""
    from leeroy_mind.protocol import Member
    cast.upsert_cast([
        Member(guid=HUMAN, name="Hero", race=10, class_=8, gender=0, level=83, rank=0,
               online=True, map=0, zone=0, spec_tab=-1),
        Member(guid=105, name="Ebbo", race=7, class_=9, gender=0, level=70, rank=3,
               online=False, map=0, zone=0, spec_tab=-1)], NOW)
    cast.conn.commit()

    async def go():
        async with served(build(data)) as app:
            return (await get(app)).json()
    v = asyncio.run(go())
    assert sorted(m["guid"] for m in v["members"]) == [101, 102, 103, 104]
    assert sorted(d["guid"] for d in v["dms"]) == [101, 102, 103, 104]
    assert v["human"] == {"guid": HUMAN, "name": "Hero"}


def test_the_console_and_the_mind_read_presence_in_one_zone(data, cast, monkeypatch):
    """Both take the local time from clock.local_at (the mind's SystemClock.local, this PC's
    zone), so for the same instant they agree on every member's presence even on a PC outside
    US Eastern, where an Eastern console would disagree."""
    from leeroy_mind.discord import presence
    from leeroy_mind.discord.talk import DiscordTalk
    from leeroy_mind.personas.io import load_persona
    from leeroy_mind.talk.world import WorldView
    tokyo = ZoneInfo("Asia/Tokyo")
    monkeypatch.setattr(mind_clock, "local_at", lambda ts: datetime.fromtimestamp(ts, tokyo))

    class At(mind_clock.SystemClock):
        def now(self):
            return NOW
    world = WorldView(is_officer=lambda g: False, discord_roster=cast.cast)
    world.human_guid = HUMAN
    talk = DiscordTalk(SimpleNamespace(store=cast, clock=At(), world=world, human_guid=HUMAN))
    personas = {g: load_persona(cast, g) for g in (101, 102, 103, 104, 105)}
    mind = {g: talk._presence(g, p, talk.app.clock.local()).state for g, p in personas.items()}

    async def go():
        async with served(build(data)) as app:
            return (await get(app)).json()
    console = {m["guid"]: m["presence"]["state"] for m in asyncio.run(go())["members"]}
    assert console == mind
    eastern = {g: presence.presence_of(p, local=presence.local_time(
        datetime.fromtimestamp(NOW, EASTERN), p.identity.region), body_online=False,
        body_zone=0, rng_seed=presence.RNG_SEED).state for g, p in personas.items()}
    assert eastern != mind  # the zone matters at this instant: Friday morning in Tokyo
