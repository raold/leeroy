"""Who is typing in Comms: the console takes the mind's status `typing` (its turns being composed
or typed) into the strip it already sends, only while a page listens and he is in game, and passes
it through unchanged, keeping only well-formed entries younger than the page's 30 s (`age`, read
by the mind at status time). An age that moved alone sends no new status. An absent mind, a bridge
down or an older mind without it: none. The page's own files: the dots' place, their CSS and
reduced motion."""
import asyncio
import re
from contextlib import asynccontextmanager, suppress
from importlib import resources
from pathlib import Path
from types import SimpleNamespace

import pytest

from leeroy_mind.console import app as console_app
from leeroy_mind.console.mindlink import LinkError
from console.rig import events, http, make_app, running

LINKED = [SimpleNamespace(state=5, remote_port=7878, remote="127.0.0.1", pid=101)]
STATIC = Path(str(resources.files("leeroy_mind.console").joinpath("static")))
ENTRY = {"guid": 101, "name": "Arkon", "channel": "guild", "to": None, "since": 1793304000.25,
         "age": 2.5}
WHISPER = {"guid": 102, "name": "Belra", "channel": "whisper", "to": 1, "since": 1793304001.0,
           "age": 1.75}


@pytest.fixture
def data(store):
    return Path(store.conn.execute("PRAGMA database_list").fetchone()["file"]).parent


@pytest.fixture
def in_game(store):
    return store.start_session(1000.0)


class FakeLink:
    def __init__(self, reply=None, *, error=None):
        self.calls, self.timeouts, self.reply, self.error = [], [], reply, error

    def locate(self, pids):
        return {"pid": 101, "port": 1, "key": "x"}

    def call(self, pids, op, args=None, *, timeout=3.0):
        self.calls.append(op)
        self.timeouts.append(timeout)
        if self.error:
            raise LinkError(self.error)
        return self.reply


def status(typing=None, **kw):
    reply = {"ok": True, "v": 1, "pid": 101, "stopping": False, "raid": None, "world": None, **kw}
    if typing is not None:
        reply["typing"] = typing
    return reply


@asynccontextmanager
async def served(app):
    await app.start()
    try:
        yield app
    finally:
        await app.close()


@asynccontextmanager
async def listening(app):
    """A page with its event stream open for the whole block."""
    task = asyncio.create_task(events(app, count=1000, timeout=30))
    for _ in range(500):
        if app._subs:
            break
        await asyncio.sleep(0.01)
    assert app._subs, "the page never connected"
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def typing_seen(app, *, events_n: int = 2):
    """The strip's typing in the status event after a page starts listening."""
    got = await events(app, count=events_n)
    assert got[0][0] == "snapshot" and got[0][1]["status"]["typing"] == []
    assert got[-1][0] == "status"
    return got[-1][1]["typing"]


def test_a_listening_page_gets_the_minds_typing_unchanged(data, in_game):
    link = FakeLink(status([ENTRY, WHISPER]))

    async def go():
        async with served(make_app(data, mind=running(data), tcp=lambda: LINKED,
                                   link=link)) as app:
            assert app.game.state == "in_game"
            assert link.calls == []  # nobody listening: the mind is not asked
            assert await typing_seen(app) == [ENTRY, WHISPER]
            st = (await http(app, "GET", "/api/state")).json()["status"]
            assert st["typing"] == [ENTRY, WHISPER]
    asyncio.run(go())
    assert set(link.calls) == {"status"}
    assert all(t <= 1.0 for t in link.timeouts)  # a busy mind never holds the strip up long


def test_only_well_formed_entries_pass_and_nothing_else_of_theirs(data, in_game):
    ok = {"since": 1.0, "age": 1.0}
    bad = [None, "x", 7, {"guid": "101", "channel": "guild", **ok},
           {"guid": 101, "channel": 5, **ok}, {"guid": 101, "channel": "guild", "age": 1.0},
           {"guid": 101, "channel": "guild", "since": float("inf"), "age": 1.0},
           {"guid": 0, "channel": "guild", **ok}, {"guid": True, "channel": "guild", **ok},
           {"guid": 101, "channel": "whisper", "to": "1", **ok},
           {"guid": 104, "channel": "guild", "since": 1.0},  # no age: an older mind's entry
           {"guid": 104, "channel": "guild", "since": 1.0, "age": -0.5},
           {"guid": 104, "channel": "guild", "since": 1.0, "age": "2"},
           {"guid": 104, "channel": "guild", "since": 1.0, "age": float("nan")},
           {"guid": 104, "channel": "guild", "since": 1.0, "age": True},
           {"guid": 105, "channel": "guild", "since": 1.0, "age": 30.5}]  # stale
    extra = {**ENTRY, "text": "words the member is typing", "prompt": "the prompt"}
    link = FakeLink(status([*bad, extra, {"guid": 103, "channel": "officer", "since": 5,
                                          "age": 30}]))

    async def go():
        async with served(make_app(data, mind=running(data), tcp=lambda: LINKED,
                                   link=link)) as app:
            return await typing_seen(app)
    assert asyncio.run(go()) == [ENTRY, {"guid": 103, "name": "", "channel": "officer",
                                         "to": None, "since": 5, "age": 30}]


def test_an_age_that_moved_alone_sends_no_new_status(data, in_game):
    """The mind's age grows on every pass: only a change of who, where or since is news. A
    snapshot or /api/state still carries the age as last read."""
    link = FakeLink(status([]))

    async def go():
        async with served(make_app(data, mind=running(data), tcp=lambda: LINKED,
                                   link=link)) as app:
            async with listening(app):
                sent = []
                real = app.broadcast
                app.broadcast = lambda kind, d: (sent.append((kind, d)), real(kind, d))
                strips = lambda: [d["typing"] for k, d in sent if k == "status"]  # noqa: E731
                link.reply = status([ENTRY])
                await app.refresh()
                assert strips() == [[ENTRY]]
                link.reply = status([{**ENTRY, "age": 4.5}])
                await app.refresh()
                assert strips() == [[ENTRY]]  # nothing new to say
                assert app.strip()["typing"] == [{**ENTRY, "age": 4.5}]
                later = {**ENTRY, "since": ENTRY["since"] + 3, "age": 0.5}  # his next line
                link.reply = status([later])
                await app.refresh()
                assert strips() == [[ENTRY], [later]]
    asyncio.run(go())


@pytest.mark.parametrize("reply", [status(), status("typing"), {"ok": False, "error": "busy"}],
                         ids=["older mind", "not a list", "refused"])
def test_an_older_or_refusing_mind_shows_nobody_typing(data, in_game, reply):
    link = FakeLink(reply)

    async def go():
        async with served(make_app(data, mind=running(data), tcp=lambda: LINKED,
                                   link=link)) as app:
            async with listening(app):
                await app.refresh()
                assert app.strip()["typing"] == [] and link.calls
    asyncio.run(go())


def test_a_mind_that_does_not_answer_shows_nobody_typing(data, in_game):
    link = FakeLink(error="the control endpoint did not answer (TimeoutError)")

    async def go():
        async with served(make_app(data, mind=running(data), tcp=lambda: LINKED,
                                   link=link)) as app:
            async with listening(app):
                await app.refresh()
                assert app.strip()["typing"] == [] and "status" in link.calls
    asyncio.run(go())


def test_an_absent_mind_or_bridge_shows_no_dots_with_shared_gpu_status(data, in_game):
    link = FakeLink(status([ENTRY]))

    async def go():
        async with served(make_app(data, link=link, tcp=lambda: LINKED)) as app:  # stopped
            async with listening(app):
                await app.refresh()
                assert app.strip()["typing"] == [] and link.calls == []
        async with served(make_app(data, mind=running(data), tcp=lambda: [],
                                   link=link)) as app:  # its bridge is not linked
            async with listening(app):
                await app.refresh()
                assert app.game.state != "in_game" and app.strip()["typing"] == []
        async with served(make_app(data, mind=running(data), tcp=lambda: LINKED)) as app:
            async with listening(app):  # no link to the endpoint at all
                await app.refresh()
                assert app.strip()["typing"] == []
    asyncio.run(go())
    assert link.calls and set(link.calls) == {"status"}


def test_the_dots_go_when_the_mind_stops(data, in_game):
    link = FakeLink(status([ENTRY]))

    async def go():
        mind = running(data)
        async with served(make_app(data, mind=mind, tcp=lambda: LINKED, link=link)) as app:
            async with listening(app):
                await app.refresh()
                assert app.strip()["typing"] == [ENTRY]
                mind.found, mind._held = [], False
                await app.refresh(force=True)
                assert app.strip()["typing"] == []
    asyncio.run(go())


def test_not_in_game_reads_gpu_status_without_typing(data):
    link = FakeLink(status([ENTRY]))

    async def go():
        async with served(make_app(data, mind=running(data), tcp=lambda: LINKED,
                                   link=link)) as app:
            async with listening(app):
                await app.refresh()
                assert app.game.state == "not_in_game" and app.strip()["typing"] == []
    asyncio.run(go())
    assert link.calls and set(link.calls) == {"status"}


def test_the_strip_loop_is_the_only_poll():
    """No faster poll than the strip's own, and never under a second."""
    assert console_app.STATUS_S >= 1.0
    assert console_app.TYPING_S <= console_app.STATUS_S


# -- the page --------------------------------------------------------------------------------

def _css() -> str:
    return (STATIC / "console.css").read_text("utf-8")


def test_the_dots_sit_under_the_feed_with_their_words():
    page = resources.files("leeroy_mind.console").joinpath("page.html").read_text("utf-8")
    feed, typing, composer = (page.index(f'id="{i}"') for i in ("feed", "typing", "composer"))
    assert feed < typing < composer
    assert re.search(r'<div id="typing" class="typing" aria-live="polite"></div>', page)
    view = (STATIC / "js" / "comms" / "typingview.mjs").read_text("utf-8")
    assert "textContent" in view or "el(" in view
    assert "style" not in view.replace("prefers-reduced-motion", "")


def test_the_dots_animate_only_without_reduced_motion():
    css = _css()
    assert re.search(r"@keyframes typing-dot\b", css)
    assert re.search(r"\.dots i\s*\{[^}]*animation:\s*typing-dot", css)
    m = re.search(r"@media \(prefers-reduced-motion: reduce\) \{(.*?)\n\}", css, re.S)
    assert m, "no reduced-motion rule"
    assert re.search(r"\.dots i\s*\{[^}]*animation:\s*none", m.group(1))


def test_the_whisper_list_marks_a_member_writing_to_him():
    left = (STATIC / "js" / "comms" / "leftview.mjs").read_text("utf-8")
    assert "whisperTypers" in left or "typingWhispers" in left
    assert re.search(r'on\("typing",\s*renderWhispers\)', left)


def test_the_page_never_holds_the_minds_clock_against_its_own():
    """Staleness is the mind's age plus the time since the strip came, on a monotonic clock: a
    device whose clock is off shows the same dots."""
    view = (STATIC / "js" / "comms" / "typingview.mjs").read_text("utf-8")
    model = (STATIC / "js" / "comms" / "typing.mjs").read_text("utf-8")
    assert "Date.now" not in view and "Date.now" not in model
    assert "performance.now()" in view
    assert not re.search(r"now\s*-\s*since|since\s*[-+<>]\s*now", model + view)
