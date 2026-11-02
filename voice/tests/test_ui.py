"""The voice UI on real sockets: each test binds port 0, speaks plain HTTP over a raw socket and
WebSocket through FakePage, and reads page.html as the browser gets it."""
import asyncio
import base64
import hashlib
import importlib.resources
import json
import logging
import re
import socket
import threading

import pytest
from websockets.exceptions import InvalidStatus

from conftest import eventually, run
from fakes import FakePage, RecordingHooks, ui_sample_state
from leeroy_voice.config import UI_DB_LIMIT
from leeroy_voice.ui import server as ui_module
from leeroy_voice.ui.server import UiServer

# the keys of a state message: the brief's twelve, and `human` (his guid, which tells the page
# which `moved` is his)
BRIEF_KEYS = ("tree", "where", "names", "room", "speaking", "ptt", "volumes", "mutes", "invites",
              "transcript", "mind", "status")
STATE_KEYS = {"type", *BRIEF_KEYS, "human"}
ACCENT, SMILE = chr(0xE9), chr(0x1F600)  # text beyond ASCII, built so no tool rewrites an escape


def page_text() -> str:
    return importlib.resources.files("leeroy_voice.ui").joinpath("page.html").read_bytes().decode(
        "utf-8")


def wire(state: dict) -> dict:
    """A state as the page gets it: JSON turns the guids used as keys into strings."""
    return json.loads(json.dumps(state))


class Rig:
    """A UiServer on an ephemeral port over RecordingHooks; the pages it makes are closed on
    the way out."""

    def __init__(self, **state):
        self.hooks = RecordingHooks(**state)
        self.ui = UiServer(0, self.hooks)
        self.pages: list[FakePage] = []

    async def __aenter__(self) -> "Rig":
        await self.ui.start()
        return self

    async def __aexit__(self, *exc) -> None:
        for page in self.pages:
            await page.close()
        await self.ui.close()

    async def page(self, **kw) -> FakePage:
        page = await FakePage.connect(self.ui.port, **kw)
        self.pages.append(page)
        return page

    async def settled(self, page: FakePage) -> list[tuple]:
        """The hook calls once everything `page` sent so far has been handled: a message after
        them is the sentinel, and a connection handles its messages in order."""
        await page.send({"type": "join", "channel": "sentinel"})
        await eventually(lambda: ("join", "sentinel") in self.hooks.calls)
        return [call for call in self.hooks.calls if call != ("join", "sentinel")]


async def fetch(port: int, path: str = "/", *, method: str = "GET", host="auto",
                extra=()) -> tuple[int, dict[str, str], bytes]:
    """One plain HTTP/1.1 request on a raw socket: (status, headers by lower-case name, body).
    `host` "auto" is the server's own, None sends no Host header, a list sends each."""
    hosts = ([f"127.0.0.1:{port}"] if host == "auto" else [] if host is None
             else [host] if isinstance(host, str) else list(host))
    lines = [f"{method} {path} HTTP/1.1", *(f"Host: {h}" for h in hosts), *extra]
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("ascii"))
        await writer.drain()
        raw = await asyncio.wait_for(reader.read(), 5)  # the server closes after its answer
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
    head, _, body = raw.partition(b"\r\n\r\n")
    status_line, *header_lines = head.decode("iso-8859-1").split("\r\n")
    headers = {k.lower(): v for k, v in (line.split(": ", 1) for line in header_lines)}
    return int(status_line.split(" ")[1]), headers, body


async def refused(port: int, **kw) -> int:
    """The status a WebSocket handshake with these Host and Origin headers is answered with."""
    with pytest.raises(InvalidStatus) as e:
        await FakePage.connect(port, **kw)
    return e.value.response.status_code


def ui_records(caplog, event: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records
            if r.name.startswith("leeroy_voice") and r.getMessage().startswith(event)]


# -- the brief's tests: the page, the handshake, the state, the messages ------------------------

def test_page_only_for_our_host(caplog):
    async def go():
        async with Rig() as rig:
            port = rig.ui.port
            status, headers, body = await fetch(port)
            assert status == 200
            assert headers["content-type"] == "text/html; charset=utf-8"
            assert body == page_text().encode("utf-8")
            assert int(headers["content-length"]) == len(body)
            for host in ("evil.example", f"localhost:{port}", f"127.0.0.1:{port + 1}",
                         "127.0.0.1", f"127.0.0.1.:{port}", f"[::1]:{port}", ""):
                status, _, body = await fetch(port, host=host)
                assert status == 403, host
                assert b"<html" not in body.lower(), host
            ours = f"127.0.0.1:{port}"  # no Host at all, ours twice, ours with another behind it
            for host in (None, [ours, ours], [ours, "evil.example"], ["evil.example", ours]):
                assert (await fetch(port, host=host))[0] == 403, host
    with caplog.at_level(logging.INFO):
        run(go())
    assert ui_records(caplog, "ui_refused: reason=host")


def test_ws_refuses_other_origins():
    async def go():
        async with Rig(**ui_sample_state()) as rig:
            port = rig.ui.port
            for origin in ("http://evil.example", None, f"http://localhost:{port}",
                           f"http://127.0.0.1:{port + 1}", "http://127.0.0.1", "null",
                           f"https://127.0.0.1:{port}", f"http://127.0.0.1:{port}/"):
                assert await refused(port, origin=origin) == 403, origin
            assert rig.ui.clients == 0 and rig.hooks.state_calls == 0  # no state for a refusal
            page = await rig.page()  # its own origin
            assert (await page.next())["type"] == "state"
    run(go())


def test_ws_refuses_other_hosts():
    """The Host check is on every request, the WebSocket handshake's included: a page that
    rebinds its name to 127.0.0.1 sends its own Host, and its own Origin."""
    async def go():
        async with Rig() as rig:
            port = rig.ui.port
            for host in ("evil.example", "localhost"):
                assert await refused(port, host=host, origin=f"http://{host}:{port}") == 403, host
                assert await refused(port, host=host) == 403, host  # with our Origin
            assert rig.hooks.state_calls == 0
    run(go())


def test_state_first():
    async def go():
        async with Rig(**ui_sample_state()) as rig:
            page = await rig.page()
            first = await page.next()
            assert first["type"] == "state"
            assert set(first) == STATE_KEYS
            assert first == {"type": "state", **wire(ui_sample_state())}
            assert first["volumes"] == {"101": -3.0} and first["names"]["101"] == "Arkon"
            assert rig.hooks.state_calls == 1
            await eventually(lambda: rig.ui.clients == 1)
            second = await rig.page()  # each page gets its own snapshot
            assert await second.next() == first and rig.hooks.state_calls == 2
    run(go())


def test_state_fills_what_the_hooks_leave_out():
    async def go():
        async with Rig() as rig:
            first = await (await rig.page()).next()
            assert first == {"type": "state", "tree": [], "where": {}, "names": {}, "room": "",
                             "human": 0, "speaking": [], "ptt": False, "volumes": {},
                             "mutes": [], "invites": [], "transcript": [], "mind": False,
                             "status": {}}
            rig.hooks.data = {"mind": True, "room": "raid", "extra": [1]}
            second = await (await rig.page()).next()
            assert second["mind"] is True and second["room"] == "raid" and second["tree"] == []
            assert second["extra"] == [1]  # what the hooks add is passed on
            rig.hooks.data = {"type": "ptt", "ptt": True}
            third = await (await rig.page()).next()
            assert third["type"] == "state" and third["ptt"] is True
    run(go())


def test_state_keeps_the_last_200_transcript_entries():
    lines = [{"who": "Arkon", "text": f"line {i}", "ts": 1793404809000 + i, "kind": "say"}
             for i in range(250)]

    async def go():
        async with Rig(transcript=lines) as rig:
            sent = (await (await rig.page()).next())["transcript"]
            assert sent == lines[-200:] and sent[-1]["text"] == "line 249"
            rig.hooks.data = {"transcript": lines[:200]}
            assert (await (await rig.page()).next())["transcript"] == lines[:200]
            rig.hooks.data = {"transcript": lines[:3]}
            assert (await (await rig.page()).next())["transcript"] == lines[:3]
    run(go())


def test_state_sets_become_lists():
    """The service keeps its mutes in a set; the page gets a sorted list."""
    async def go():
        mutes, speaking = {8, 1, 17}, frozenset({300, 1, 8, 17, 101})
        assert list(mutes) != sorted(mutes) and list(speaking) != sorted(speaking)  # a real test
        async with Rig(mutes=mutes, speaking=speaking) as rig:
            first = await (await rig.page()).next()
            assert first["mutes"] == [1, 8, 17] and first["speaking"] == [1, 8, 17, 101, 300]
    run(go())


def test_join_volume_mute():
    async def go():
        async with Rig() as rig:
            page = await rig.page()
            await page.send({"type": "join", "channel": "raid"})
            await page.send({"type": "volume", "guid": 101, "db": 20})
            await page.send({"type": "volume", "guid": 102, "db": -20})
            await page.send({"type": "volume", "guid": 103, "db": 3.5})
            await page.send({"type": "volume", "guid": 104, "db": 0})
            await page.send({"type": "volume", "guid": 105, "db": 12})
            await page.send({"type": "volume", "guid": 105, "db": -12.0})
            await page.send({"type": "mute", "guid": 101, "on": True})
            await page.send({"type": "mute", "guid": 101, "on": False})
            assert await rig.settled(page) == [
                ("join", "raid"), ("volume", 101, 12.0), ("volume", 102, -12.0),
                ("volume", 103, 3.5), ("volume", 104, 0.0), ("volume", 105, 12.0),
                ("volume", 105, -12.0), ("mute", 101, True), ("mute", 101, False)]
            assert all(type(call[2]) is float for call in rig.hooks.calls if call[0] == "volume")
    run(go())


FRAMES = [json.dumps(m) for m in (
    {"type": "nope"}, {"type": "state"}, {"type": "ptt", "down": True},
    {"type": "speaking", "guid": 101, "on": True}, {"type": "JOIN", "channel": "raid"},
    {"channel": "raid"}, {"type": 5}, {"type": None}, {"type": ["join"], "channel": "raid"},
    {}, [], ["join", "raid"], "join", None, 7, True)] + [
    "", "{", "not json", '{"type":"join"', "[" * 3000, "NaN", "Infinity",
    b"\x00\x01binary", b'{"type":"join","channel":"raid"}']  # the last is a binary frame


def test_unknown_message_ignored():
    async def go():
        async with Rig(**ui_sample_state()) as rig:
            page = await rig.page()
            await page.next()
            for frame in FRAMES:
                await page.send(frame)
            assert await rig.settled(page) == []
            assert not page.closed.is_set() and len(page.received) == 1  # open, nothing back
    run(go())


@pytest.mark.parametrize("message", [
    {"type": "join"}, {"type": "join", "channel": ""}, {"type": "join", "channel": 5},
    {"type": "join", "channel": None}, {"type": "join", "channel": ["raid"]},
    {"type": "join", "channel": "x" * 65}, {"type": "join", "channel": "a\nb"},
    {"type": "join", "channel": "a\x00b"}, {"type": "join", "channel": True},
    {"type": "volume", "db": 1}, {"type": "volume", "guid": 101},
    {"type": "volume", "guid": 0, "db": 1}, {"type": "volume", "guid": -5, "db": 1},
    {"type": "volume", "guid": "101", "db": 1}, {"type": "volume", "guid": 101.0, "db": 1},
    {"type": "volume", "guid": True, "db": 1}, {"type": "volume", "guid": None, "db": 1},
    {"type": "volume", "guid": 2 ** 32, "db": 1}, {"type": "volume", "guid": 2 ** 70, "db": 1},
    {"type": "volume", "guid": [101], "db": 1}, {"type": "volume", "guid": 101, "db": "5"},
    {"type": "volume", "guid": 101, "db": None}, {"type": "volume", "guid": 101, "db": True},
    {"type": "volume", "guid": 101, "db": [3]}, {"type": "volume", "guid": 101, "db": {}},
    {"type": "volume", "guid": 101, "db": float("nan")},
    {"type": "volume", "guid": 101, "db": float("inf")},
    {"type": "volume", "guid": 101, "db": float("-inf")},
    {"type": "mute", "on": True}, {"type": "mute", "guid": 101},
    {"type": "mute", "guid": 0, "on": True}, {"type": "mute", "guid": "101", "on": True},
    {"type": "mute", "guid": 101, "on": "true"}, {"type": "mute", "guid": 101, "on": 1},
    {"type": "mute", "guid": 101, "on": 0}, {"type": "mute", "guid": 101, "on": None},
    {"type": "mute", "guid": True, "on": True},
], ids=lambda m: json.dumps(m)[:60])
def test_a_bad_argument_is_ignored(message):
    async def go():
        async with Rig() as rig:
            page = await rig.page()
            await page.send(message)
            assert await rig.settled(page) == []
            assert not page.closed.is_set()
    run(go())


def test_the_edges_of_a_good_argument():
    async def go():
        async with Rig() as rig:
            page = await rig.page()
            await page.send({"type": "join", "channel": "x" * 64})
            await page.send({"type": "join", "channel": "Raid 2 / Left"})
            await page.send({"type": "volume", "guid": 1, "db": 11.99})
            await page.send({"type": "volume", "guid": 2 ** 32 - 1, "db": -11.99})
            await page.send({"type": "volume", "guid": 7, "db": 10 ** 400})  # finite: clamped
            await page.send({"type": "volume", "guid": 8, "db": -10 ** 400})
            await page.send({"type": "mute", "guid": 2 ** 32 - 1, "on": False})
            assert await rig.settled(page) == [
                ("join", "x" * 64), ("join", "Raid 2 / Left"), ("volume", 1, 11.99),
                ("volume", 2 ** 32 - 1, -11.99), ("volume", 7, 12.0), ("volume", 8, -12.0),
                ("mute", 2 ** 32 - 1, False)]
            assert UI_DB_LIMIT == 12.0
    run(go())


def test_an_oversize_message_closes_the_page():
    async def go():
        async with Rig() as rig:
            page = await rig.page()
            await page.next()
            assert ui_module.MAX_MESSAGE_BYTES == 4096
            await page.send({"type": "join", "channel": "raid", "pad": "x" * 4096})
            await page.wait_closed()
            assert page.close_code == 1009
            assert rig.hooks.calls == []
            other = await rig.page()  # the server goes on
            assert (await other.next())["type"] == "state"
    run(go())


def test_a_hook_that_fails_keeps_the_page(caplog):
    async def go():
        async with Rig() as rig:
            page = await rig.page()
            rig.hooks.raises = {"join": RuntimeError("boom"), "volume": ValueError("no"),
                                "mute": KeyError("x")}
            await page.send({"type": "join", "channel": "raid"})
            await page.send({"type": "volume", "guid": 101, "db": 1})
            await page.send({"type": "mute", "guid": 101, "on": True})
            await eventually(lambda: len(rig.hooks.calls) == 3)
            rig.hooks.raises = {}
            await page.send({"type": "mute", "guid": 102, "on": True})
            assert await rig.settled(page) == [
                ("join", "raid"), ("volume", 101, 1.0), ("mute", 101, True), ("mute", 102, True)]
            assert not page.closed.is_set()
    with caplog.at_level(logging.INFO):
        run(go())
    failed = ui_records(caplog, "ui_hook_failed")
    assert [r.getMessage() for r in failed] == [
        "ui_hook_failed: hook=join error=RuntimeError",
        "ui_hook_failed: hook=volume error=ValueError", "ui_hook_failed: hook=mute error=KeyError"]
    assert all(r.levelno == logging.ERROR for r in failed)
    assert not [r for r in caplog.records if "boom" in r.getMessage()]  # no exception text


def test_a_state_that_fails_closes_that_page(caplog):
    async def go():
        async with Rig(**ui_sample_state()) as rig:
            rig.hooks.raises = {"state": RuntimeError("boom")}
            page = await rig.page()
            await page.wait_closed()
            assert page.close_code == 1011 and page.received == []
            await eventually(lambda: rig.ui.clients == 0)
            rig.hooks.raises = {}
            assert (await (await rig.page()).next())["type"] == "state"  # the next page is served
    with caplog.at_level(logging.INFO):
        run(go())
    assert [r.getMessage() for r in ui_records(caplog, "ui_state_failed")] == [
        "ui_state_failed: error=RuntimeError"]


def test_a_state_that_cannot_be_encoded_closes_that_page(caplog):
    async def go():
        async with Rig(mind=True, status={"queue": object()}) as rig:
            page = await rig.page()
            await page.wait_closed()
            assert page.close_code == 1011 and page.received == []
        async with Rig(mind=True, volumes={101: float("nan")}) as rig:  # NaN is not JSON
            page = await rig.page()
            await page.wait_closed()
            assert page.close_code == 1011
    with caplog.at_level(logging.INFO):
        run(go())
    assert [r.getMessage() for r in ui_records(caplog, "ui_state_failed")] == [
        "ui_state_failed: error=TypeError", "ui_state_failed: error=ValueError"]


# -- the other routes ---------------------------------------------------------------------------

def test_the_rest_is_404_and_only_get_serves_the_page():
    async def go():
        async with Rig() as rig:
            port = rig.ui.port
            for path in ("/favicon.ico", "/index.html", "/ws/", "/ws2", "/page.html", "/x?/",
                         "//evil.example/", "/%2e", "/WS", "/ws.html"):
                status, headers, body = await fetch(port, path)
                assert status == 404, path
                assert b"<html" not in body.lower() and "content-security-policy" not in headers
            for path in ("/", "/?v=1", "/?"):  # a query string is no other page
                assert (await fetch(port, path))[2] == page_text().encode("utf-8"), path
            for method in ("POST", "PUT", "DELETE", "HEAD", "OPTIONS", "PATCH"):
                status, headers, body = await fetch(port, method=method)
                assert (status, headers.get("allow")) == (405, "GET"), method
                assert b"<html" not in body.lower()
            # a plain GET of /ws is no handshake: the library answers it, with no page
            status, _, body = await fetch(port, "/ws")
            assert status == 426 and b"<html" not in body.lower()
            assert (await fetch(port, "/ws", host="evil.example"))[0] == 403
            assert (await fetch(port, "/nope", host="evil.example"))[0] == 403  # not 404
            assert (await fetch(port, method="POST", host="evil.example"))[0] == 403
            assert rig.hooks.state_calls == 0 and rig.hooks.calls == []
    run(go())


def test_the_page_is_served_with_its_guards():
    async def go():
        async with Rig() as rig:
            port = rig.ui.port
            status, headers, body = await fetch(port)
            assert status == 200
            assert headers["cache-control"] == "no-store"
            assert headers["x-content-type-options"] == "nosniff"
            assert headers["x-frame-options"] == "DENY"
            assert headers["referrer-policy"] == "no-referrer"
            assert headers["connection"] == "close"
            assert "server" not in headers  # nothing about Python or websockets
            csp = dict(part.strip().split(" ", 1) for part in headers["content-security-policy"]
                       .split(";") if part.strip())
            assert csp["default-src"] == "'none'"
            assert csp["connect-src"] == f"ws://127.0.0.1:{port}"  # its own socket and nothing else
            assert csp["frame-ancestors"] == "'none'" and csp["base-uri"] == "'none'"
            assert csp["form-action"] == "'none'" and csp["img-src"] == "data:"
            text = body.decode("utf-8")
            for tag, directive in (("script", "script-src"), ("style", "style-src")):
                (block,) = re.findall(rf"<{tag}>(.*?)</{tag}>", text, re.S)
                digest = hashlib.sha256(block.replace("\r\n", "\n").encode("utf-8")).digest()
                assert csp[directive] == f"'sha256-{base64.b64encode(digest).decode()}'", tag
            assert "unsafe" not in headers["content-security-policy"]
            assert "http" not in headers["content-security-policy"]
    run(go())


def test_the_csp_hash_is_of_the_block_the_parser_reads():
    """A browser hashes an inline block after its HTML parser has turned CRLF and CR into LF."""
    block = "var a = 1;\r\nvar b = 2;\rvar c = 3;\n"
    wanted = hashlib.sha256(b"var a = 1;\nvar b = 2;\nvar c = 3;\n").digest()
    assert ui_module._hash_source(block) == f"'sha256-{base64.b64encode(wanted).decode()}'"
    assert ui_module._block_sources("<p></p>", "script") == "'none'"  # no block, no source
    two = f"<script>{block}</script><script>x</script>"
    assert ui_module._block_sources(two, "script").count("'sha256-") == 2


# -- broadcast, clients, close ------------------------------------------------------------------

def test_broadcast_reaches_every_page():
    async def go():
        async with Rig(**ui_sample_state()) as rig:
            assert rig.ui.clients == 0
            a, b = await rig.page(), await rig.page()
            await a.next()
            await b.next()
            await eventually(lambda: rig.ui.clients == 2)
            event = {"type": "speaking", "guid": 101, "on": True}
            rig.ui.broadcast(event)
            assert await a.next() == event and await b.next() == event
            await a.close()
            await eventually(lambda: rig.ui.clients == 1)
            rig.ui.broadcast({"type": "ptt", "down": True})
            rig.ui.broadcast({"type": "transcript", "who": "Arkon", "text": f"caf{ACCENT} {SMILE}",
                              "ts": 1793404809000, "kind": "say"})
            assert await b.next() == {"type": "ptt", "down": True}
            assert (await b.next())["text"] == f"caf{ACCENT} {SMILE}"  # any text
            assert [m["type"] for m in a.received] == ["state", "speaking"]  # nothing after it left
    run(go())


def test_broadcast_is_compact_ascii_json_with_sets_as_lists():
    async def go():
        async with Rig() as rig:
            page = await rig.page()
            await page.next()
            rig.ui.broadcast({"type": "x", "mutes": {8, 1, 17}, "t": ("a", "b"), "s": ACCENT})
            assert await page.next() == {"type": "x", "mutes": [1, 8, 17], "t": ["a", "b"],
                                         "s": ACCENT}
            rig.ui.broadcast({"type": "mind", "up": True, "s": ACCENT + SMILE, "n": [1, 2]})
            await page.next()
            assert page.raw[-1] == ('{"type":"mind","up":true,"s":"\\u00e9\\ud83d\\ude00",'
                                    '"n":[1,2]}')  # no spaces; escapes, so any text is safe
    run(go())


def test_the_state_comes_first_even_under_traffic():
    """Events fly, one every turn of the loop, while pages connect: every page's first message
    is still its state, and what follows is in the order sent, none lost."""
    async def go():
        async with Rig(**ui_sample_state()) as rig:
            stop = asyncio.Event()
            sent = []

            async def traffic():
                while not stop.is_set() and len(sent) < 20000:
                    sent.append(len(sent) + 1)
                    rig.ui.broadcast({"type": "status", "queue": sent[-1]})
                    await asyncio.sleep(0)
            task = asyncio.create_task(traffic())
            await asyncio.sleep(0.01)
            pages = [await rig.page() for _ in range(10)]
            stop.set()
            await task
            await eventually(lambda: all(p.received[-1:] and p.received[-1].get("queue") == sent[-1]
                                         for p in pages), timeout=5)
            for page in pages:
                assert page.received[0]["type"] == "state"
                queues = [m["queue"] for m in page.received[1:]]
                assert queues == list(range(queues[0], sent[-1] + 1))  # in order, none missing
            assert sum(len(page.received) for page in pages) > 20  # the traffic really flowed
    run(go())


def test_broadcast_from_another_thread(monkeypatch):
    sent_on = []
    real_send = UiServer._send

    def spy(self, text):
        sent_on.append(threading.get_ident())
        real_send(self, text)
    monkeypatch.setattr(UiServer, "_send", spy)

    async def go():
        loop_thread = threading.get_ident()
        async with Rig() as rig:
            page = await rig.page()
            await page.next()
            await asyncio.to_thread(rig.ui.broadcast, {"type": "ptt", "down": True})
            assert await page.next() == {"type": "ptt", "down": True}
            await asyncio.to_thread(rig.ui.broadcast, {"type": "ptt", "down": False})
            await asyncio.to_thread(rig.ui.broadcast, {"type": "mind", "up": True})
            assert [await page.next(), await page.next()] == [
                {"type": "ptt", "down": False}, {"type": "mind", "up": True}]
            rig.ui.broadcast({"type": "mind", "up": False})  # and from the loop's own thread
            await page.next()
            assert sent_on == [loop_thread] * 4  # the library only ever writes on the loop
    run(go())


def test_broadcast_never_raises(caplog):
    async def go():
        async with Rig() as rig:
            page = await rig.page()
            await page.next()
            rig.ui.broadcast({"type": "x", "o": object()})
            rig.ui.broadcast({"type": "x", "n": float("nan")})
            rig.ui.broadcast({"no": "type"})
            rig.ui.broadcast({"type": 5})
            rig.ui.broadcast([{"type": "x"}])
            rig.ui.broadcast("ptt")
            rig.ui.broadcast(None)
            rig.ui.broadcast({"type": "ptt", "down": True})
            assert await page.next() == {"type": "ptt", "down": True}  # the next one still goes
            assert len(page.received) == 2
    with caplog.at_level(logging.INFO):
        run(go())
    failed = ui_records(caplog, "ui_broadcast_failed")
    assert [r.getMessage() for r in failed] == [  # the event's type is named when it has one
        "ui_broadcast_failed: error=TypeError kind=x",
        "ui_broadcast_failed: error=ValueError kind=x"] + [
        "ui_broadcast_failed: error=ValueError"] * 5
    assert all(r.levelno == logging.ERROR for r in failed)


def test_broadcast_with_no_page_or_no_server_is_a_noop():
    async def go():
        ui = UiServer(0, RecordingHooks())
        ui.broadcast({"type": "ptt", "down": True})  # not started
        assert ui.clients == 0
        async with Rig() as rig:
            rig.ui.broadcast({"type": "ptt", "down": True})  # nobody to send to
            page = await rig.page()
            assert (await page.next())["type"] == "state" and len(page.received) == 1
        rig.ui.broadcast({"type": "ptt", "down": True})  # closed
        await asyncio.to_thread(rig.ui.broadcast, {"type": "ptt", "down": True})
        assert rig.ui.clients == 0
    run(go())


def test_close_ends_the_pages():
    async def go():
        async with Rig() as rig:
            port = rig.ui.port
            a, b = await rig.page(), await rig.page()
            await a.next()
            await b.next()
            await eventually(lambda: rig.ui.clients == 2)
            await rig.ui.close()
            await a.wait_closed()
            await b.wait_closed()
            assert a.close_code == b.close_code == 1001  # going away
            assert rig.ui.clients == 0
            await rig.ui.close()  # a second close is a no-op
            with pytest.raises(OSError):
                await FakePage.connect(port)
        await UiServer(0, RecordingHooks()).close()  # so is closing one never started
    run(go())


def test_it_can_start_again_after_a_close():
    async def go():
        ui = UiServer(0, RecordingHooks(mind=True))
        await ui.start()
        await ui.close()
        await ui.start()
        try:
            page = await FakePage.connect(ui.port)
            assert (await page.next())["mind"] is True
            await page.close()
        finally:
            await ui.close()
    run(go())


def test_port_is_the_bound_one_once_started():
    async def go():
        ui = UiServer(0, RecordingHooks())
        assert ui.port == 0
        await ui.start()
        try:
            assert ui.port > 0 and (await fetch(ui.port))[0] == 200
        finally:
            await ui.close()
        assert ui.port == 0  # the port it was asked for
        assert UiServer(7880, RecordingHooks()).port == 7880
    run(go())


def test_start_on_a_taken_port_raises():
    async def go():
        async with Rig() as first:
            second = UiServer(first.ui.port, RecordingHooks())
            with pytest.raises(OSError):
                await second.start()
            assert second.clients == 0 and second.port == first.ui.port
            await second.close()
            assert (await fetch(first.ui.port))[0] == 200  # the first one is untouched
    run(go())


def test_a_start_that_fails_leaves_nothing_bound(monkeypatch):
    made = []
    real_create = socket.create_server

    def create(*args, **kw):
        made.append(real_create(*args, **kw))
        return made[-1]

    def refuse(*args, **kw):
        raise RuntimeError("no server today")
    monkeypatch.setattr(ui_module.socket, "create_server", create)
    monkeypatch.setattr(ui_module, "serve", refuse)

    async def go():
        ui = UiServer(0, RecordingHooks())
        with pytest.raises(RuntimeError):
            await ui.start()
        assert len(made) == 1 and made[0].fileno() == -1  # the socket is closed
        assert ui.port == 0 and ui.clients == 0
        ui.broadcast({"type": "ptt", "down": True})  # and it is not half started
        await ui.close()
    run(go())


@pytest.mark.parametrize("port", [-1, 65536, 1.5, "7880", None, True])
def test_a_bad_port_is_refused(port):
    with pytest.raises(ValueError):
        UiServer(port, RecordingHooks())


def test_it_listens_on_loopback_only():
    """Bound to 127.0.0.1 and nothing wider: another loopback address (127.0.0.2) gets no
    answer, where a socket on all interfaces would take it. The control proves this machine
    reaches 127.0.0.2 at all."""
    control = socket.create_server(("0.0.0.0", 0))
    try:
        try:
            socket.create_connection(("127.0.0.2", control.getsockname()[1]), timeout=2).close()
        except OSError:
            pytest.skip("127.0.0.2 is not reachable on this machine")
    finally:
        control.close()

    async def go():
        assert ui_module.HOST == "127.0.0.1"
        async with Rig() as rig:
            assert (await fetch(rig.ui.port))[0] == 200
            with pytest.raises(OSError):
                await asyncio.open_connection("127.0.0.2", rig.ui.port)
    run(go())


# -- the page -----------------------------------------------------------------------------------

def test_no_external_resources():
    page = page_text()
    for forbidden in ("http://", "https://", "<script src"):
        assert forbidden not in page, forbidden
    for forbidden in ("@import", "<iframe", "<object", "<embed", "<img", "fetch(",
                      "XMLHttpRequest", "sendBeacon", "EventSource", "importScripts", "//cdn"):
        assert forbidden not in page, forbidden
    assert re.search(r"\bsrc\s*=", page) is None  # nothing is loaded by address
    assert re.findall(r"<link\b[^>]*>", page) == ['<link rel="icon" href="data:,">']
    assert re.search(r"url\(", page) is None  # CSS loads nothing
    # the one socket it opens is to the host it came from
    sockets = re.findall(r"ws://(.*)", page)
    assert sockets and all(s.startswith('" + location.host + "/ws"') for s in sockets)


def test_mind_offline_in_state():
    """Review Focus 5: a push-to-talk press while the mind is down is discarded, and the page
    says so."""
    async def go():
        async with Rig(**{**ui_sample_state(), "mind": False}) as rig:
            assert (await (await rig.page()).next())["mind"] is False
            rig.hooks.data = ui_sample_state()
            assert (await (await rig.page()).next())["mind"] is True
    run(go())
    page = page_text()
    assert "mind offline" in page
    assert 'id="banner-mind"' in page and "mind offline" in page.split('id="banner-mind"')[1][:400]


def test_the_page_has_what_the_brief_lists():
    page = page_text()
    for part in ('id="tree"', 'id="transcript"', 'id="st-ptt"', 'id="toasts"', 'id="menu"',
                 'id="banner-mind"', 'id="banner-lost"', "contextmenu", "Join", "Ignore",
                 "lock", "light", "collapsed", "textContent"):
        assert part in page, part
    # the per-member volume: a slider from -12 to +12 dB, the limit the service keeps
    assert re.search(r'<input id="menu-vol" type="range" min="-12" max="12" step="0\.5"', page)
    assert '<input id="menu-mute" type="checkbox"' in page
    assert re.search(r"<title>[^<]+</title>", page) and '<meta charset="utf-8">' in page
    assert "ventrilo" not in page.lower()  # no name and no logo


def test_the_page_is_plain_ascii_and_bare_blocks():
    page = page_text()
    assert page.isascii() and "\r" not in page
    assert page.count("<script>") == 1 and page.count("<style>") == 1  # the CSP hashes these
    assert re.findall(r"<script\b[^>]*>", page) == ["<script>"]
    assert re.findall(r"<style\b[^>]*>", page) == ["<style>"]


def test_the_page_never_builds_markup_from_text():
    """Member lines and his own words reach the page as text: it sets textContent and
    nothing that parses markup, and it has no inline handler attributes."""
    page = page_text()
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(",
                      "new Function", "createContextualFragment", "DOMParser", "srcdoc",
                      "javascript:"):
        assert forbidden not in page, forbidden
    assert re.search(r"<[a-zA-Z][^>]*\son[a-z]+\s*=", page) is None
    assert re.search(r"""setTimeout\(\s*["']|setInterval\(\s*["']""", page) is None


def test_every_id_the_script_uses_is_in_the_markup():
    page = page_text()
    markup, script = page.split("<script>")
    used = set(re.findall(r'\$\("([\w-]+)"\)', script))
    defined = set(re.findall(r'\bid="([\w-]+)"', markup))
    assert used and used <= defined, used - defined
    assert len(re.findall(r'\bid="([\w-]+)"', markup)) == len(defined)  # no id twice


def test_a_hooks_exception_message_stays_out_of_the_log(tmp_path):
    """A hook's error may quote what someone said: the log keeps its type and frames only
    (review-task10)."""
    from leeroy_voice import logs

    async def go():
        async with Rig() as rig:
            rig.hooks.raises["join"] = ValueError("a line someone said")
            page = await rig.page()
            await page.next()
            await page.send({"type": "join", "channel": "raid"})
            await eventually(lambda: rig.hooks.calls)
    teardown = logs.setup(tmp_path)
    try:
        run(go())
    finally:
        teardown()
    text = (tmp_path / "logs" / "voice.log").read_text(encoding="utf-8")
    assert "ui_hook_failed" in text and "ValueError" in text
    assert "a line someone said" not in text


def test_a_page_that_stops_reading_is_dropped():
    """broadcast() has no backpressure: a page whose buffer has grown past the bound is cut
    off, and its reconnect brings a fresh state, rather than the buffer growing for the 40 s
    the ping needs (review-task10)."""
    async def go():
        async with Rig() as rig:
            page = await rig.page()
            await page.next()
            stalled = next(iter(rig.ui._pages))
            stalled.transport.get_write_buffer_size = lambda: ui_module.MAX_PAGE_BUFFER + 1
            rig.ui.broadcast({"type": "ptt", "down": True})
            await page.wait_closed()
            assert rig.ui.clients == 0
    run(go())
