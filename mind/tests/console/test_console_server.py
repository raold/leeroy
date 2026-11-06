"""The console's server: the key, the Origin and Host checks, the routes, the control rules and
the event stream. A real server on a free port; the mind, voice and probes are fakes."""
import asyncio
import socket
import sqlite3
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from leeroy_mind.console import feed, httpd, status
from leeroy_mind.console.control import Controls
from console.rig import KEY, FakeMind, events, http, make_app, page_source, running

LINKED = [SimpleNamespace(state=5, remote_port=7878, remote="127.0.0.1", pid=101)]


@pytest.fixture
def data(store):
    """The store fixture's tmp dir holds a migrated mind.db: the data dir."""
    return store.conn.execute("PRAGMA database_list").fetchone()["file"] and \
        __import__("pathlib").Path(store.conn.execute("PRAGMA database_list").fetchone()["file"]).parent


@asynccontextmanager
async def served(app):
    await app.start()
    try:
        yield app
    finally:
        await app.close()


def run(coro):
    return asyncio.run(coro)


# -- the gate ------------------------------------------------------------------------------------

def test_every_request_needs_the_console_host(data):
    async def go():
        async with served(make_app(data)) as app:
            for host in ("localhost:%d" % app.s.port, "evil.example", "127.0.0.1:1"):
                r = await http(app, "GET", "/api/state", host=host)
                assert r.status == 403 and b"served at" in r.body
            r = await http(app, "GET", "/api/state",
                           headers={"Host": f"127.0.0.1:{app.s.port}"})  # a second Host
            assert r.status == 403
    run(go())


def test_the_key_stays_in_the_page_and_travels_in_a_header_never_a_cookie(data):
    async def go():
        async with served(make_app(data)) as app:
            assert app.url == f"{app.origin}/#key={KEY}"  # a fragment: never sent to a server
            for path in ("/", f"/?key={KEY}"):
                page = await http(app, "GET", path, key=False)
                assert page.status == 200 and page.header("content-type").startswith("text/html")
                assert page.header("set-cookie") is None and KEY.encode() not in page.body
            assert (await http(app, "GET", "/api/state", key=False)).status == 403
            assert (await http(app, "GET", f"/api/state?key={KEY}", key=False)).status == 403
            r = await http(app, "GET", "/api/state", key=False,
                           headers={"Cookie": f"leeroy_console_{app.s.port}={KEY}"})
            assert r.status == 403  # what the console used to set: no longer a key
            assert (await http(app, "GET", "/api/state")).status == 200
            source = page_source()  # the page and its modules
            assert "X-Console-Key" in source and "history.replaceState" in source
            assert "new EventSource" not in source and "document.cookie" not in source
    run(go())


def test_a_wrong_or_repeated_key_is_refused(data):
    async def go():
        async with served(make_app(data)) as app:
            for headers in ({"X-Console-Key": "not-the-key"}, {"X-Console-Key": ""},
                            {"X-Console-Key": KEY[:-1]}):
                r = await http(app, "GET", "/api/state", key=False, headers=headers)
                assert r.status == 403
            r = await http(app, "GET", "/api/state", headers={"X-Console-Key": KEY})  # twice
            assert r.status == 403
    run(go())


def test_another_origin_or_site_is_refused_even_with_the_key(data):
    async def go():
        async with served(make_app(data)) as app:
            r = await http(app, "GET", "/api/state", origin="http://127.0.0.1:7880")
            assert r.status == 403 and b"another origin" in r.body
            for site in ("cross-site", "same-site"):
                r = await http(app, "GET", "/api/state", headers={"Sec-Fetch-Site": site})
                assert r.status == 403
            for site in ("same-origin", "none"):
                assert (await http(app, "GET", "/api/state",
                                   headers={"Sec-Fetch-Site": site})).status == 200
            assert (await http(app, "GET", "/api/state", origin=True)).status == 200
    run(go())


def test_a_change_needs_both_the_key_and_the_origin(data):
    mind = running(data)
    async def go():
        async with served(make_app(data, mind=mind)) as app:
            body = {"service": "mind", "action": "restart"}
            assert (await http(app, "POST", "/api/control", body=body)).status == 403
            assert (await http(app, "POST", "/api/control", body=body, key=False,
                               origin=True)).status == 403
            r = await http(app, "POST", "/api/control", body=b"service=mind", origin=True,
                           headers={"Content-Type": "application/x-www-form-urlencoded"})
            assert r.status == 415
            assert mind.acts == []
            r = await http(app, "POST", "/api/control", body={**body, "confirm": True},
                           origin=True)  # confirmed: its game state is unknown here
            assert r.status == 200 and r.json()["ok"] and mind.acts == ["restart"]
    run(go())


def test_methods_other_than_get_and_post_are_refused(data):
    async def go():
        async with served(make_app(data)) as app:
            for method in ("OPTIONS", "PUT", "DELETE", "HEAD"):
                r = await http(app, method, "/api/control", origin=True)
                assert r.status == 405
                assert r.header("access-control-allow-origin") is None
            assert (await http(app, "GET", "/api/control")).status == 405
            assert (await http(app, "GET", "/nothing")).status == 404
    run(go())


def test_bad_requests_get_short_answers(data):
    async def go():
        async with served(make_app(data)) as app:
            r = await http(app, "POST", "/api/control", body=b"{not json", origin=True)
            assert r.status == 400
            r = await http(app, "POST", "/api/control", body=b"x" * 20000, origin=True)
            assert r.status == 413
    run(go())


# -- control -------------------------------------------------------------------------------------

def test_no_control_refuses_and_launches_nothing(data):
    mind = running(data)
    controls = Controls(enabled=False, reason="process control is off (--no-control)",
                        mind=mind, voice=SimpleNamespace(act=lambda a: pytest.fail("voice")))
    async def go():
        async with served(make_app(data, mind=mind, controls=controls)) as app:
            for service in ("mind", "voice"):
                for action in ("start", "stop", "restart"):
                    r = await http(app, "POST", "/api/control", origin=True,
                                   body={"service": service, "action": action, "confirm": True})
                    assert r.status == 403 and "--no-control" in r.json()["error"]
            state = (await http(app, "GET", "/api/state")).json()
            assert state["status"]["control"]["enabled"] is False
    run(go())
    assert mind.acts == []


def test_stop_while_in_game_asks_first(data, store):
    sid = store.start_session(1000.0)
    mind = running(data)
    async def go():
        app = make_app(data, mind=mind, tcp=lambda: [
            SimpleNamespace(state=5, remote_port=7878, remote="127.0.0.1", pid=101)])
        async with served(app):
            assert app.game.state == status.IN_GAME and app.game.since == 1000.0
            for action in ("stop", "restart"):
                r = await http(app, "POST", "/api/control", origin=True,
                               body={"service": "mind", "action": action})
                assert r.status == 409 and r.json()["confirm"].startswith("You're in game.")
            assert mind.acts == []
            r = await http(app, "POST", "/api/control", origin=True,
                           body={"service": "mind", "action": "stop", "confirm": True})
            assert r.status == 200 and mind.acts == ["stop"]
            r = await http(app, "POST", "/api/control", origin=True,
                           body={"service": "voice", "action": "stop"})
            assert r.status == 409 and "comms go silent" in r.json()["confirm"]
    run(go())
    assert sid


def test_stop_needs_no_question_when_he_is_not_in_game(data):
    mind = running(data)
    async def go():
        app = make_app(data, mind=mind, tcp=lambda: [
            SimpleNamespace(state=5, remote_port=7878, remote="127.0.0.1", pid=101)])
        async with served(app):
            assert app.game.state == status.NOT_IN_GAME
            r = await http(app, "POST", "/api/control", origin=True,
                           body={"service": "mind", "action": "stop"})
            assert r.status == 200 and mind.acts == ["stop"]
    run(go())


def test_an_unreadable_session_is_unknown_and_a_stop_asks_first(data, monkeypatch):
    def unreadable(conn):
        raise sqlite3.OperationalError("database is locked at C:/private/mind.db")
    monkeypatch.setattr(feed, "open_session", unreadable)
    mind = running(data)
    async def go():
        async with served(make_app(data, mind=mind, tcp=lambda: LINKED)) as app:
            assert app.view.state == "running" and app.bridge is True
            assert (app.game.state, app.game.reason) == (
                status.UNKNOWN, "the mind's session could not be read (OperationalError)")
            st = (await http(app, "GET", "/api/state")).json()["status"]
            assert st["game"]["state"] == "unknown" and "private" not in repr(st)
            for service in ("mind", "voice"):
                for action in ("stop", "restart"):
                    r = await http(app, "POST", "/api/control", origin=True,
                                   body={"service": service, "action": action})
                    assert r.status == 409
                    assert r.json()["confirm"].startswith(
                        "The console cannot tell whether you're in game (the mind's session "
                        "could not be read (OperationalError)).")
            assert mind.acts == []
            r = await http(app, "POST", "/api/control", origin=True,
                           body={"service": "mind", "action": "stop", "confirm": True})
            assert r.status == 200 and mind.acts == ["stop"]
    run(go())


def test_a_stop_asks_first_whenever_the_game_state_is_unknown(data):
    async def go():
        async with served(make_app(data, mind=FakeMind(data))) as app:  # the mind is down
            assert app.game.state == status.UNKNOWN
            r = await http(app, "POST", "/api/control", origin=True,
                           body={"service": "voice", "action": "stop"})
            assert r.status == 409 and "(the mind is not running)" in r.json()["confirm"]
            assert "comms go silent" in r.json()["confirm"]
            r = await http(app, "POST", "/api/control", origin=True,
                           body={"service": "mind", "action": "start"})
            assert r.status == 200  # a start never asks
    run(go())


# -- the strip, voice and the stream -------------------------------------------------------------

def test_a_down_mind_is_never_shown_as_not_in_game(data, store):
    store.start_session(1000.0)  # a crash left it open
    async def go():
        async with served(make_app(data, mind=FakeMind(data))) as app:
            st = (await http(app, "GET", "/api/state")).json()["status"]
            assert st["mind"]["state"] == "stopped"
            assert st["game"] == {"state": "unknown", "reason": "the mind is not running",
                                  "since": None}
            assert st["voice"] == {"enabled": False, "service": False, "link": None}
    run(go())


def test_a_join_says_what_is_true_when_it_cannot_be_asked(data):
    async def go():
        async with served(make_app(data, mind=running(data))) as app:
            r = await http(app, "POST", "/api/voice", origin=True,
                           body={"type": "join", "channel": "raid"})
            assert r.status == 409 and r.json()["error"] == "The voice service is not running."
            r = await http(app, "POST", "/api/voice", origin=True, body={"type": "mute",
                                                                         "guid": 101, "on": True})
            assert r.status == 409 and "not running" in r.json()["error"]
            r = await http(app, "POST", "/api/voice", origin=True, body={"type": "speak"})
            assert r.status == 400
    run(go())


def test_the_stream_starts_with_a_snapshot(data):
    async def go():
        async with served(make_app(data)) as app:
            (kind, snap), = await events(app)
            assert kind == "snapshot"
            assert set(snap) == {"status", "voice", "chat", "rooms", "mood"}
            assert snap["voice"] is None and snap["chat"] == []
            r = await http(app, "GET", "/api/events", key=False)
            assert r.status == 403
    run(go())


def test_a_snapshot_whose_feed_read_failed_says_so(data):
    """chat: null, never [] (which would set the page's read floor to 0 and make all history
    unread at the next snapshot that reads)."""
    def boom(*a, **kw):
        raise OSError("mind.db unreadable")

    async def go():
        async with served(make_app(data)) as app:
            app._feed_since = boom
            app._poll = boom
            snap = (await http(app, "GET", "/api/state")).json()
            assert snap["chat"] is None and len(snap["rooms"]) == 32
            (kind, first), = await events(app)
            assert kind == "snapshot" and first["chat"] is None
    run(go())


def test_the_stream_carries_later_events(data):
    async def go():
        async with served(make_app(data)) as app:
            reader = asyncio.create_task(events(app, count=2))
            for _ in range(100):
                if app._subs:
                    break
                await asyncio.sleep(0.02)
            await asyncio.sleep(0.05)
            app.broadcast("control", {"service": "mind", "action": "start", "phase": "running"})
            got = await reader
            assert got[1] == ("control", {"service": "mind", "action": "start",
                                          "phase": "running"})
        assert not app._subs
    run(go())


def test_health_is_read_only_and_content_free(data):
    logs = data / "logs"
    logs.mkdir(exist_ok=True)
    (logs / "mind.log").write_text(
        '{"ts": "2026-11-04T21:00:00.000+00:00", "level": "INFO", "event": "say_sent", '
        '"guid": 101, "kind": "greet", "topic": "a private word", "detail": "said hi"}\n'
        '{"ts": "2026-11-04T21:00:01.000+00:00", "level": "ERROR", "event": "tick_failed", '
        '"exc": "Traceback...\\nValueError: secret text here"}\n', encoding="utf-8")
    seen = {}

    def ollama(url, roles):
        seen["url"] = url
        return {"ok": True, "url": url, "models": [{"name": "m", "role": "live"}]}

    def files():
        return {p.relative_to(data).as_posix(): (p.stat().st_size, p.stat().st_mtime_ns)
                for p in sorted(data.rglob("*")) if p.is_file()}
    async def go():
        async with served(make_app(data, ollama=ollama,
                                   ollama_url="http://127.0.0.1:1")) as app:
            before = files()
            h = (await http(app, "GET", "/api/health")).json()
            assert files() == before  # nothing in the data dir was created or written
            assert seen["url"] == "http://127.0.0.1:1"
            assert h["ollama"]["models"][0]["role"] == "live"
            assert h["gpu"]["ok"] is False
            text = repr(h["log"]) + repr(h["errors"])
            assert "private" not in text and "said hi" not in text and "secret" not in text
            assert h["log"][0]["fields"] == {"guid": 101, "kind": "greet"}
            assert h["errors"][0]["exc"] == "ValueError"
            assert h["voice"]["detail"] == "no voice venv in this checkout"
    run(go())


def test_health_reuses_the_status_loops_reading_and_its_voice_lock_reading(data):
    mind = running(data)
    reads = []
    voice = SimpleNamespace(missing=lambda: None, held=lambda: reads.append(1) or 500)
    clock = [0.0]
    async def go():
        async with served(make_app(data, mind=mind, voice=voice,
                                   monotonic=lambda: clock[0])) as app:
            lists = mind.lists
            for _ in range(3):
                h = (await http(app, "GET", "/api/health")).json()
            assert mind.lists == lists  # no process list (nor the lock probe that comes with one)
            assert h["mind"]["pids"] == [101, 100] and h["mind"]["lock"] is True
            assert h["voice"]["pid"] == 500 and h["voice"]["lock"] is True and len(reads) == 1
            clock[0] += 15.0
            await http(app, "GET", "/api/health")
            assert len(reads) == 2  # read again once it is VOICE_LOCK_S old
    run(go())


def passes(app, seconds, clock, *, every=2.0, between=None):
    """The status loop's worker-thread half, once every `every` s of a fake clock."""
    for step in range(int(seconds / every)):
        if between is not None:
            between(clock[0])
        app._probe(step == 0)
        app._derive()
        clock[0] += every


@pytest.mark.parametrize("mind_kw", [
    {"error": "multiple mind process families match"},  # unclear: ambiguity
    {"held": True},  # unclear: a lock that is not this checkout's mind
    {"found": [SimpleNamespace(pid=101)]},  # starting: a family without its lock
], ids=["ambiguous", "foreign-lock", "starting"])
def test_an_unclear_mind_is_listed_less_and_less_often(data, mind_kw):
    clock = [0.0]
    mind = FakeMind(data, **mind_kw)
    app = make_app(data, mind=mind, monotonic=lambda: clock[0])
    passes(app, 60, clock)  # 30 passes in a minute
    assert app.view.state in ("unknown", "starting")
    assert mind.lists == 6  # at 0, 2, 6, 14, 30 and 46 s: each wait doubles, up to 15 s
    # a free lock is probed only with a list; a held one (it cannot be taken) each later pass
    assert mind.lock_probes == (29 if mind_kw.get("held") else 0)


def test_a_settled_mind_is_listed_every_15_s_and_a_free_lock_never_probed_between(data):
    clock = [0.0]
    stopped = FakeMind(data)
    app = make_app(data, mind=stopped, monotonic=lambda: clock[0])
    passes(app, 60, clock)
    assert app.view.state == "stopped" and stopped.lists == 4 and stopped.lock_probes == 0
    clock = [0.0]
    up = running(data)
    app = make_app(data, mind=up, monotonic=lambda: clock[0])
    passes(app, 60, clock)
    assert app.view.state == "running" and up.lists == 4 and up.lock_probes == 29


def test_a_log_that_grows_under_a_free_lock_is_listed_at_once(data):
    clock = [0.0]
    mind = FakeMind(data)
    app = make_app(data, mind=mind, monotonic=lambda: clock[0])
    logs = data / "logs"
    logs.mkdir(exist_ok=True)

    def start_at_6(now):  # a mind started elsewhere: it logs once it holds its lock
        if now == 6.0:
            mind.found, mind._held = [SimpleNamespace(pid=101)], True
            with (logs / "mind.log").open("a", encoding="utf-8") as fh:
                fh.write('{"ts": "t", "level": "INFO", "event": "mind_started"}\n')
    passes(app, 10, clock, between=start_at_6)
    assert app.view.state == "running" and mind.lists == 2  # at 0 and 6 s, not at 16
    assert mind.lock_probes == 1  # at 8 s, held: harmless; none while it was free


def test_a_stream_drops_a_client_that_stops_reading(data):
    async def go():
        closed = asyncio.Event()

        async def endless():
            while True:
                yield b"data: " + b"x" * 65536 + b"\n\n"

        async def handler(req):
            return httpd.Stream(endless(), content_type="text/event-stream",
                                closed=closed.set, drain_s=0.3)
        server = await httpd.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        client = socket.socket()
        client.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        client.settimeout(10)
        try:
            client.connect(("127.0.0.1", port))
            client.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
            await asyncio.wait_for(closed.wait(), 10)  # it never reads, and is dropped

            def until_dropped():
                try:
                    while client.recv(65536):
                        pass
                except ConnectionError:
                    pass
            await asyncio.wait_for(asyncio.to_thread(until_dropped), 10)
        finally:
            client.close()
            server.close()
            await asyncio.wait_for(server.wait_closed(), 5)
    run(go())


async def raw(app, head: str) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", app.s.port)
    writer.write(head.encode("latin-1"))
    await writer.drain()
    reply = await asyncio.wait_for(reader.read(), 10)
    writer.close()
    return reply


def test_numbers_are_ascii_digits_of_a_bounded_length(data):
    async def go():
        async with served(make_app(data)) as app:
            for value in (chr(0xB2), "1" * 5000, "-1", "+5", "0x10"):
                reply = await raw(app, f"POST /api/control HTTP/1.1\r\nHost: 127.0.0.1:"
                                       f"{app.s.port}\r\nContent-Length: {value}\r\n\r\n")
                assert reply.startswith(b"HTTP/1.1 400 ") and b"bad Content-Length" in reply, \
                    value[:8]
            for value in ("%C2%B2", "%D9%A1", "9" * 19, "1" * 5000, "-1", ""):
                r = await http(app, "GET", f"/api/feed?after={value}")
                assert r.status == 400, value[:8]
            r = await http(app, "GET", f"/api/feed?after={'9' * 18}")
            assert r.status == 200 and r.json() == {"lines": []}
    run(go())
    assert httpd.digits("0", 1) == 0 and httpd.digits("12", 1) is None
    assert httpd.digits(chr(0x661), 5) is None and httpd.digits("", 5) is None


# -- the spoken lines of the feed ----------------------------------------------------------------

def _spoken_setup(store, state, speak_id="run.1"):
    from leeroy_mind.protocol import Chat, Member
    from leeroy_mind.voice.store import VoiceStore
    store.set_meta("human_guid", "900")
    store.upsert_cast([Member(guid=101, name="Arkon", race=1, class_=1, gender=0, level=80,
                              rank=3, online=True, map=0, zone=0, spec_tab=-1)], 1.0)
    sid = store.start_session(1.0)
    line = store.insert_chat_line(Chat(ts=0, channel="voice:guild_hall", from_guid=101,
                                       from_name="Arkon", to_guid=0, text="hello hall", map=0,
                                       zone=0), session_id=sid, origin="game", audience=None)
    VoiceStore(store).add_line(speak_id, line, "guild_hall", state, 1.0)
    return line, VoiceStore(store)


def test_the_snapshot_lists_the_32_rooms_and_the_mood(data, store):
    from leeroy_mind.voice.rooms import ROOMS

    async def go():
        async with served(make_app(data)) as app:
            snap = (await http(app, "GET", "/api/state")).json()
            assert [r["id"] for r in snap["rooms"]] == [r.id for r in ROOMS]
            assert len(snap["rooms"]) == 32
            assert set(snap["rooms"][0]) == {"id", "name", "parent", "capacity"}
            assert snap["mood"] is None
    run(go())


def test_a_spoken_line_is_sent_again_once_it_plays(data, store, monkeypatch):
    from leeroy_mind.console import app as console_app
    monkeypatch.setattr(console_app, "FEED_S", 0.05)
    line, voice = _spoken_setup(store, "sent")

    async def go():
        async with served(make_app(data, clock=store.clock.now)) as app:
            reader = asyncio.create_task(events(app, count=2))
            for _ in range(200):
                if line in app._pending:
                    break
                await asyncio.sleep(0.02)
            voice.set_line("run.1", "done", None, 2.0)
            (_, snap), (kind, data_) = await reader
            assert snap["chat"][0]["heard"] is False
            assert kind == "chat" and data_["lines"][0]["id"] == line
            assert data_["lines"][0]["heard"] is True
            assert line not in app._pending
    run(go())


def test_a_line_that_never_plays_is_given_up(data, store, monkeypatch):
    from leeroy_mind.console import app as console_app
    monkeypatch.setattr(console_app, "FEED_S", 0.05)
    line, voice = _spoken_setup(store, "sent")

    async def go():
        async with served(make_app(data, clock=store.clock.now)) as app:
            reader = asyncio.create_task(events(app, count=2))
            for _ in range(200):
                if line in app._pending:
                    break
                await asyncio.sleep(0.02)
            voice.set_line("run.1", "dropped", "muted", 2.0)
            _, (kind, data_) = await reader
            assert (kind, data_) == ("chat_gone", {"ids": [line]})
    run(go())
    line2, _ = _spoken_setup(store, "sent", "run.2")
    monkeypatch.setattr(console_app, "PENDING_S", 0.0)

    async def aged():
        async with served(make_app(data, clock=store.clock.now)) as app:
            reader = asyncio.create_task(events(app, count=2))
            _, (kind, data_) = await reader
            assert kind == "chat_gone" and line2 in data_["ids"]
    run(aged())


# -- fix round 1: one read snapshot, voiced whispers ---------------------------------------------

def _pass_rig(app):
    sent = []
    app.broadcast = lambda kind, payload: sent.append((kind, payload))
    app._subs.add(object())
    return sent


def _chat_ids(sent):
    return [(line["id"], line["heard"]) for kind, p in sent if kind == "chat" for line in p["lines"]]


@pytest.mark.parametrize("first,then", [("sent", "done"), (None, "done")])
def test_a_voice_row_landing_mid_poll_is_not_lost(data, store, monkeypatch, first, then):
    from leeroy_mind.protocol import Chat, Member
    from leeroy_mind.voice.store import VoiceStore
    store.set_meta("human_guid", "900")
    store.upsert_cast([Member(guid=101, name="Arkon", race=1, class_=1, gender=0, level=80,
                              rank=3, online=True, map=0, zone=0, spec_tab=-1)], 1.0)
    sid = store.start_session(1.0)
    voice = VoiceStore(store)
    real_feed = feed.feed
    state = {}

    def feed_then_commit(conn, **kw):
        out = real_feed(conn, **kw)
        if "line" in state and not state.get("done"):
            state["done"] = True
            other = sqlite3.connect(data / "mind.db")  # the mind's commit, from its own connection
            with other:
                other.execute("INSERT INTO voice_line(speak_id, line_id, room, state, ts) "
                              "VALUES ('run.1', ?, 'guild_hall', ?, 1.0)",
                              (state["line"], first or then))
            other.close()
        return out
    monkeypatch.setattr(feed, "feed", feed_then_commit)

    async def go():
        app = make_app(data, clock=store.clock.now)
        sent = _pass_rig(app)
        _, app._feed_last = await asyncio.to_thread(app._feed_since, 1 << 62)
        state["line"] = store.insert_chat_line(
            Chat(ts=0, channel="voice:guild_hall", from_guid=101, from_name="Arkon", to_guid=0,
                 text="late row", map=0, zone=0), session_id=sid, origin="mind")
        await app._feed_pass()
        await app._feed_pass()
        if first:
            voice.set_line("run.1", then, None, 2.0)
            await app._feed_pass()
        await app._feed_pass()
        got = _chat_ids(sent)
        assert got[-1] == (state["line"], True)
        assert [g for g in got if g[1] is True] == [(state["line"], True)]
        assert len([g for g in got if g[1] is False]) == (1 if first else 0)
    run(go())


def _whisper_setup(store):
    from leeroy_mind.protocol import Chat, Member
    store.set_meta("human_guid", "900")
    store.upsert_cast([Member(guid=101, name="Arkon", race=1, class_=1, gender=0, level=80,
                              rank=3, online=True, map=0, zone=0, spec_tab=-1)], 1.0)

    def whisper(text):
        return store.insert_chat_line(Chat(ts=0, channel="whisper", from_guid=101,
                                           from_name="Arkon", to_guid=900, text=text, map=0,
                                           zone=0), session_id=None, origin="mind")
    return whisper


def test_a_dropped_voiced_whisper_never_shows_and_a_pending_one_is_gone(data, store):
    from leeroy_mind.voice.store import VoiceStore
    whisper = _whisper_setup(store)
    voice = VoiceStore(store)
    a, b = whisper("dropped at once"), whisper("sent then dropped")
    voice.add_line("w.1", a, "whisper", "dropped", 1.0)
    voice.add_line("w.2", b, "whisper", "sent", 1.0)

    async def go():
        app = make_app(data, clock=lambda: store.clock.now() + 60)
        sent = _pass_rig(app)
        _, app._feed_last = await asyncio.to_thread(app._feed_since, 1 << 62)
        app._feed_last = 0
        await app._feed_pass()
        assert _chat_ids(sent) == [(b, False)]
        voice.set_line("w.2", "dropped", "expired", 2.0)
        await app._feed_pass()
        assert ("chat_gone", {"ids": [b]}) in sent
    run(go())


@pytest.mark.parametrize("reason", ["deafened", "muted"])
def test_a_pending_whisper_he_did_not_hear_comes_back_as_text_marked(data, store, reason):
    """The owner's ruling: a whisper on its way that the voice service drops because he is
    deafened or has muted the member is sent again as text marked unheard, never gone."""
    from leeroy_mind.voice.store import VoiceStore
    whisper = _whisper_setup(store)
    voice = VoiceStore(store)

    async def go():
        app = make_app(data, clock=lambda: store.clock.now() + 60)
        sent = _pass_rig(app)
        _, app._feed_last = await asyncio.to_thread(app._feed_since, 1 << 62)
        line = whisper("on its way")
        voice.add_line("w.1", line, "whisper", "sent", 1.0)
        await app._feed_pass()
        assert _chat_ids(sent) == [(line, False)] and line in app._pending
        voice.set_line("w.1", "dropped", reason, 2.0)
        sent.clear()
        await app._feed_pass()
        lines = [x for kind, p in sent if kind == "chat" for x in p["lines"]]
        assert [(x["id"], x["voice"], x["heard"], x["unheard"]) for x in lines] == [
            (line, None, None, reason)]
        assert "chat_gone" not in [kind for kind, _ in sent] and not app._pending
    run(go())


def test_a_whisper_dropped_deafened_inside_the_grace_shows_marked(data, store):
    from leeroy_mind.voice.store import VoiceStore
    whisper = _whisper_setup(store)
    now = [store.clock.now()]

    async def go():
        app = make_app(data, clock=lambda: now[0])
        sent = _pass_rig(app)
        _, app._feed_last = await asyncio.to_thread(app._feed_since, 1 << 62)
        line = whisper("not for a deaf ear")
        await app._feed_pass()
        assert _chat_ids(sent) == [] and line in app._waiting  # inside the grace: held
        VoiceStore(store).add_line("w.1", line, "whisper", "dropped", 1.0, reason="deafened")
        now[0] += 1
        await app._feed_pass()
        lines = [x for kind, p in sent if kind == "chat" for x in p["lines"]]
        assert [(x["id"], x["unheard"]) for x in lines] == [(line, "deafened")]
        assert not app._waiting
    run(go())


def test_a_whisper_whose_voice_row_comes_late_is_never_shown_as_typed(data, store):
    from leeroy_mind.voice.store import VoiceStore
    whisper = _whisper_setup(store)
    now = [store.clock.now()]

    async def go():
        app = make_app(data, clock=lambda: now[0])
        sent = _pass_rig(app)
        _, app._feed_last = await asyncio.to_thread(app._feed_since, 1 << 62)
        spoken, typed = whisper("said aloud"), whisper("typed")
        await app._feed_pass()
        assert _chat_ids(sent) == []  # inside the grace: held
        now[0] += 1
        VoiceStore(store).add_line("w.1", spoken, "whisper", "sent", 1.0)
        await app._feed_pass()
        assert _chat_ids(sent) == [(spoken, False)]
        now[0] += 3
        await app._feed_pass()
        assert _chat_ids(sent) == [(spoken, False), (typed, None)]
    run(go())
