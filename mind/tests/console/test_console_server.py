"""The console's server: the key, the Origin and Host checks, the routes, the control rules and
the event stream. A real server on a free port; the mind, voice and probes are fakes."""
import asyncio
import base64
import hashlib
import re
import socket
import sqlite3
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from leeroy_mind.console import feed, httpd, status
from leeroy_mind.console.control import Controls
from console.rig import KEY, FakeMind, events, http, make_app, running

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
            html = page.body.decode()
            assert "X-Console-Key" in html and "history.replaceState" in html
            assert "new EventSource" not in html and "document.cookie" not in html
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


def test_the_page_carries_a_strict_policy_matching_its_inline_blocks(data):
    async def go():
        async with served(make_app(data)) as app:
            r = await http(app, "GET", "/")
            csp = r.header("content-security-policy")
            html = r.body.decode()
            for tag in ("script", "style"):
                for block in re.findall(rf"<{tag}>(.*?)</{tag}>", html, re.S):
                    digest = base64.b64encode(hashlib.sha256(block.encode()).digest()).decode()
                    assert f"'sha256-{digest}'" in csp
            assert "unsafe-inline" not in csp and "connect-src 'self'" in csp
            assert "frame-ancestors 'none'" in csp and r.header("x-frame-options") == "DENY"
            assert r.header("referrer-policy") == "same-origin"  # a POST keeps its Origin
            assert "http://" not in html.split("<script>")[1]  # it loads nothing from elsewhere
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
            assert set(snap) == {"status", "voice", "chat"}
            assert snap["voice"] is None and snap["chat"] == []
            r = await http(app, "GET", "/api/events", key=False)
            assert r.status == 403
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
    async def go():
        async with served(make_app(data, ollama=ollama,
                                   ollama_url="http://127.0.0.1:1")) as app:
            h = (await http(app, "GET", "/api/health")).json()
            assert seen["url"] == "http://127.0.0.1:1"
            assert h["ollama"]["models"][0]["role"] == "live"
            assert h["gpu"]["ok"] is False
            text = repr(h["log"]) + repr(h["errors"])
            assert "private" not in text and "said hi" not in text and "secret" not in text
            assert h["log"][0]["fields"] == {"guid": 101, "kind": "greet"}
            assert h["errors"][0]["exc"] == "ValueError"
            assert h["voice"]["detail"] == "no voice venv in this checkout"
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
