"""The console's reply box, server side: POST /api/say checked as the mind checks it, refused in
words while the console cannot send (control off, the mind down, the bridge down, not in game or
nobody can tell), then on to the mind's say op. A real server on a free port; the mind, its link
and the probes are fakes."""
import asyncio
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from leeroy_mind import endpoint
from leeroy_mind.console import cli, feed, mindlink, status
from leeroy_mind.console.control import Controls
from leeroy_mind.console.mindlink import LinkError
from console.rig import FakeMind, http, make_app, running

LINKED = [SimpleNamespace(state=5, remote_port=7878, remote="127.0.0.1", pid=101)]
SENT = {"ok": True, "message": "sent"}


@pytest.fixture
def data(store):
    return Path(store.conn.execute("PRAGMA database_list").fetchone()["file"]).parent


@pytest.fixture
def in_game(store):
    return store.start_session(1000.0)


class FakeLink:
    """The mind's control endpoint as the raid tests fake it, with each call's timeout kept."""

    def __init__(self, reply=None, *, error=None):
        self.calls, self.timeouts, self.reply, self.error = [], [], reply, error

    def locate(self, pids):
        if self.error:
            raise self.error if isinstance(self.error, LinkError) else LinkError(self.error)
        return {"pid": 101, "port": 1, "key": "x"}

    def call(self, pids, op, args=None, *, timeout=3.0):
        self.calls.append((tuple(pids), op, args))
        self.timeouts.append(timeout)
        if self.error:
            raise self.error if isinstance(self.error, LinkError) else LinkError(self.error)
        return self.reply


@asynccontextmanager
async def served(app):
    await app.start()
    try:
        yield app
    finally:
        await app.close()


def say(app, body, **kw):
    return http(app, "POST", "/api/say", body=body, origin=True, **kw)


def test_a_reply_goes_to_the_minds_say_op(data, in_game):
    link = FakeLink(SENT)

    async def go():
        async with served(make_app(data, mind=running(data), tcp=lambda: LINKED, link=link)) as app:
            assert app.game.state == status.IN_GAME
            kinds = []
            app.broadcast = lambda kind, data: kinds.append(kind)
            r = await say(app, {"channel": "guild", "text": "hi"})
            assert r.status == 200 and r.json() == {"ok": True, "message": "sent"}
            r = await say(app, {"channel": "whisper", "text": "  omw \n 5 min ", "to": 1101})
            assert r.status == 200
            # Nothing is broadcast: the line appears through the feed when the game reports it
            assert set(kinds) <= {"status"}
    asyncio.run(go())
    # The arguments say_args returned, cleaned: what the mind will check again
    assert link.calls == [
        ((101, 100), "say", {"channel": "guild", "text": "hi"}),
        ((101, 100), "say", {"channel": "whisper", "text": "omw 5 min", "to": 1101})]
    assert mindlink.SAY_S == endpoint.SAY_S + 5.0 and link.timeouts == [mindlink.SAY_S] * 2


def test_a_reply_needs_the_key_and_the_origin(data, in_game):
    link = FakeLink(SENT)

    async def go():
        async with served(make_app(data, mind=running(data), tcp=lambda: LINKED, link=link)) as app:
            body = {"channel": "guild", "text": "hi"}
            assert (await http(app, "POST", "/api/say", body=body)).status == 403  # no Origin
            assert (await http(app, "POST", "/api/say", body=body, key=False,
                               origin=True)).status == 403
            assert (await http(app, "POST", "/api/say", body=body,
                               origin="http://127.0.0.1:1")).status == 403
            assert (await http(app, "POST", "/api/say", body=b"channel=guild&text=hi", origin=True,
                               headers={"Content-Type": "text/plain"})).status == 415
            assert (await http(app, "GET", "/api/say")).status == 405
    asyncio.run(go())
    assert link.calls == []


def test_no_control_refuses_replies(data, in_game):
    link = FakeLink(SENT)
    off = Controls(enabled=False, reason="process control is off (--no-control)",
                   mind=running(data))

    async def go():
        async with served(make_app(data, mind=running(data), controls=off, tcp=lambda: LINKED,
                                   link=link)) as app:
            r = await say(app, {"channel": "guild", "text": "hi"})
            assert r.status == 403 and r.json() == {
                "ok": False, "error": "Replies are off: this console runs with --no-control."}
    asyncio.run(go())
    assert link.calls == []
    words = " ".join(cli.parser().format_help().split())
    assert "--no-control" in words and "repl" in words[words.index("--no-control"):]


def test_a_reply_with_the_mind_down_says_so(data, in_game):
    """Review Focus 1: a down mind, a down bridge or a start or stop under way is named as what it
    is, never "you're not in game" (the session a crash left open is not his presence)."""
    link = FakeLink(SENT)

    async def go():
        async with served(make_app(data, mind=FakeMind(data), tcp=lambda: LINKED,
                                   link=link)) as app:
            r = await say(app, {"channel": "guild", "text": "hi"})
            assert r.status == 409 and r.json()["error"] == (
                "The mind is stopped, so nothing can be sent.")
        async with served(make_app(data, mind=FakeMind(data, error="ambiguous family"),
                                   tcp=lambda: LINKED, link=link)) as app:
            r = await say(app, {"channel": "guild", "text": "hi"})
            assert r.status == 409 and r.json()["error"] == (
                "The mind's state is unclear, so nothing can be sent.")
        async with served(make_app(data, mind=running(data), tcp=lambda: [], link=link)) as app:
            assert app.bridge is False
            r = await say(app, {"channel": "guild", "text": "hi"})
            assert r.status == 409 and r.json()["error"] == "The game bridge is down."
        async with served(make_app(data, mind=running(data), tcp=lambda: LINKED, link=link)) as app:
            app.controls.busy = ("mind", "restart")
            r = await say(app, {"channel": "guild", "text": "hi"})
            assert r.status == 409 and r.json()["error"] == (
                "A restart of the mind is under way, so nothing can be sent.")
        async with served(make_app(data, mind=running(data), tcp=lambda: LINKED)) as app:
            r = await say(app, {"channel": "guild", "text": "hi"})  # no link at all
            assert r.status == 409 and r.json()["error"] == (
                "Nothing was sent: the console has no link to the mind's control endpoint.")
        gone = FakeLink(error="the mind has no control endpoint (it is off in mind.toml, the "
                              "running mind predates it, or it is still starting)")
        async with served(make_app(data, mind=running(data), tcp=lambda: LINKED, link=gone)) as app:
            r = await say(app, {"channel": "guild", "text": "hi"})
            assert r.status == 409 and r.json()["error"] == (
                "Nothing was sent: the mind has no control endpoint (it is off in mind.toml, the "
                "running mind predates it, or it is still starting).")
    asyncio.run(go())
    assert link.calls == []


def test_not_in_game_and_unknown_are_told_apart(data, store, monkeypatch):
    link = FakeLink(SENT)

    async def go():
        async with served(make_app(data, mind=running(data), tcp=lambda: LINKED, link=link)) as app:
            assert app.game.state == status.NOT_IN_GAME
            r = await say(app, {"channel": "guild", "text": "hi"})
            assert r.status == 409 and r.json()["error"] == "You're not in game."
        store.start_session(1000.0)
        async with served(make_app(data, mind=running(data), link=link)) as app:  # no TCP table
            assert app.game.state == status.UNKNOWN
            r = await say(app, {"channel": "guild", "text": "hi"})
            assert r.status == 409 and r.json()["error"] == (
                "The console can't tell whether you're in game (the mind's game bridge state is "
                "unknown).")

        def unreadable(conn):
            raise sqlite3.OperationalError("database is locked at C:/private/mind.db")
        monkeypatch.setattr(feed, "open_session", unreadable)
        async with served(make_app(data, mind=running(data), tcp=lambda: LINKED, link=link)) as app:
            r = await say(app, {"channel": "guild", "text": "hi"})
            assert r.status == 409 and r.json()["error"] == (
                "The console can't tell whether you're in game (the mind's session could not be "
                "read (OperationalError)).")
            assert "private" not in r.json()["error"]
    asyncio.run(go())
    assert link.calls == []


@pytest.mark.parametrize("body, words", [
    ({"channel": "trade", "text": "hi"}, "A line goes to guild, officer, group or whisper."),
    ({"channel": "guild", "text": "hi", "to": 1101},
     "A guild line takes exactly channel and text."),
    ({"channel": "whisper", "text": "hi"}, "A whisper takes exactly channel, text and to."),
    ({"channel": "whisper", "text": "hi", "to": True}, "A whisper goes to a member's guid."),
    ({"channel": "guild", "text": 5}, "A line is text."),
    ({"channel": "guild", "text": "   "}, "There's nothing to send."),
    ({"channel": "guild", "text": "a" * 256}, "That's too long for WoW chat (255 bytes at most)."),
    ({"channel": "guild", "text": "  .gm on"}, "Commands can't be sent from the console."),
    ({"channel": "guild", "text": "hi \ud800"}, "That line has a character WoW chat can't carry."),
])
def test_a_bad_shape_is_400_with_its_words(data, body, words):
    """Checked first, as the mind will check it: a bad line is named even while nothing could be
    sent anyway (the mind is down here)."""
    link = FakeLink(SENT)

    async def go():
        async with served(make_app(data, link=link)) as app:
            r = await say(app, body)
            assert r.status == 400 and r.json() == {"ok": False, "error": words}
            r = await http(app, "POST", "/api/say", body=b'["guild", "hi"]', origin=True)
            assert r.status == 400
    asyncio.run(go())
    assert link.calls == []


def test_the_minds_refusal_comes_back_409(data, in_game):
    async def go():
        for reply, words in (
                ({"ok": False, "error": "Too fast: wait a moment before the next line."},
                 "Too fast: wait a moment before the next line."),
                ({"ok": False, "error": "The game server needs the rebuild that adds console "
                                        "replies."},
                 "The game server needs the rebuild that adds console replies."),
                # A mind from before the say op
                ({"ok": False, "error": "bad request: op is one of status, stop, raid, tune"},
                 "The mind needs the update to send replies."),
                ({"ok": False}, "The mind refused it.")):
            link = FakeLink(reply)
            async with served(make_app(data, mind=running(data), tcp=lambda: LINKED,
                                       link=link)) as app:
                r = await say(app, {"channel": "officer", "text": "hi"})
                assert r.status == 409 and r.json() == {"ok": False, "error": words}
                assert link.calls == [((101, 100), "say", {"channel": "officer", "text": "hi"})]
        silent = FakeLink(error=LinkError("the control endpoint did not answer (TimeoutError)",
                                          unanswered=True))
        async with served(make_app(data, mind=running(data), tcp=lambda: LINKED,
                                   link=silent)) as app:
            r = await say(app, {"channel": "guild", "text": "hi"})
            assert r.status == 409 and r.json()["error"] == (
                "The control endpoint did not answer (TimeoutError). Check WoW before sending it "
                "again.")
    asyncio.run(go())


@pytest.mark.parametrize("text", ["/w Arkon hi", "   /dance", "| /2 lfg", "\t/"])
def test_a_slash_command_is_400_and_never_sent(data, in_game, text):
    """WoW would post it as words on the channel ("/w Arkon hi" in guild): the console refuses it
    before anything else, as its box does."""
    link = FakeLink(SENT)

    async def go():
        async with served(make_app(data, mind=running(data), tcp=lambda: LINKED, link=link)) as app:
            r = await say(app, {"channel": "guild", "text": text})
            assert r.status == 400 and r.json() == {
                "ok": False, "error": "Slash commands aren't sent from here."}
            r = await say(app, {"channel": "whisper", "text": text, "to": 1101})
            assert r.status == 400
            r = await say(app, {"channel": "guild", "text": "hi /wave"})
            assert r.status == 200
        async with served(make_app(data, link=link)) as app:  # the mind down: the text first
            r = await say(app, {"channel": "guild", "text": text})
            assert r.status == 400
    asyncio.run(go())
    assert link.calls == [((101, 100), "say", {"channel": "guild", "text": "hi /wave"})]


def test_a_voice_start_or_stop_does_not_hold_a_reply(data, in_game):
    """A typed reply never goes near the voice service; the mind's own start or stop still holds
    it, and the raid actions keep waiting for either."""
    link = FakeLink(SENT)

    async def go():
        async with served(make_app(data, mind=running(data), tcp=lambda: LINKED, link=link)) as app:
            for action in ("start", "stop", "restart"):
                app.controls.busy = ("voice", action)
                r = await say(app, {"channel": "guild", "text": "hi"})
                assert r.status == 200, action
            app.controls.busy = ("voice", "start")
            r = await http(app, "POST", "/api/raid", body={"action": "summon"}, origin=True)
            assert r.status == 409 and "still running" in r.json()["error"]
            app.controls.busy = ("mind", "start")
            r = await say(app, {"channel": "guild", "text": "hi"})
            assert r.status == 409 and r.json()["error"] == (
                "A start of the mind is under way, so nothing can be sent.")
    asyncio.run(go())
    assert [c[1] for c in link.calls] == ["say"] * 3
