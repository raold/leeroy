"""MindServer on real sockets: each test binds port 0 and talks to it with FakeMind."""
import asyncio
import importlib.metadata
import logging
import socket

import leeroy_voice
from conftest import eventually, run
from fakes import FakeMind
from leeroy_voice import server as server_module
from leeroy_voice.server import MindServer
from leeroy_voice.wire import (ANOTHER_MIND, SERVER_NAME, Error, Heard, Hello, Join, Played,
                               Speak, Status, encode)

SPEAK = Speak("s1", 102, "adds on the left", "guild_hall", True, "p1", "urgent", "none",
              "sober", 1793404809000)
STATUS = Status(True, "unset", "none", "parakeet", False, 0, ())


class Rig:
    """A MindServer on an ephemeral port that records its callbacks."""

    def __init__(self, fail_first: bool = False, hello=None):
        self.got: list = []
        self.connects = 0
        self.disconnects = 0
        self.hellos = 0
        self._fail_first = fail_first
        self.server = MindServer("127.0.0.1", 0, self.on_message, on_connect=self.on_connect,
                                 on_disconnect=self.on_disconnect, hello=hello or self.hello)

    def hello(self) -> Hello:
        self.hellos += 1
        return Hello(SERVER_NAME, leeroy_voice.__version__, ("kokoro",), "Mic A", "", "\\")

    async def on_message(self, msg) -> None:
        if self._fail_first:
            self._fail_first = False
            raise RuntimeError("a handler bug")
        self.got.append(msg)

    async def on_connect(self) -> None:
        self.connects += 1

    async def on_disconnect(self) -> None:
        self.disconnects += 1

    async def __aenter__(self) -> "Rig":
        await self.server.start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.server.close()

    async def mind(self) -> FakeMind:
        mind = await FakeMind.connect(self.server.port)
        assert type(await mind.next()) is Hello
        return mind


def test_hello_then_messages():
    async def go():
        async with Rig() as rig:
            assert rig.server.port > 0 and not rig.server.connected
            mind = await FakeMind.connect(rig.server.port)
            hello = await mind.next()
            assert hello == Hello("leeroy-voice", "0.1.0", ("kokoro",), "Mic A", "", "\\")
            assert importlib.metadata.version("leeroy-voice") == leeroy_voice.__version__
            await eventually(lambda: rig.connects == 1)
            assert rig.server.connected
            await mind.send(SPEAK)
            await eventually(lambda: rig.got)
            assert rig.got == [SPEAK]
            assert await rig.server.send(Played("s1", "started")) is True
            assert await mind.next() == Played("s1", "started")
            await mind.close()
    run(go())


def test_first_line_must_be_a_json_object():
    async def go():
        async with Rig() as rig:
            mind = await rig.mind()
            await mind.send_raw(b"GET / HTTP/1.1\r\n" + encode(SPEAK, 1))
            assert await mind.next() == Error("bad json")
            await mind.wait_eof()
            await eventually(lambda: rig.disconnects == 1)
            assert rig.got == [] and not rig.server.connected
            await mind.close()
    run(go())


def test_blank_lines_and_a_bad_message_first_are_kept():
    async def go():
        async with Rig() as rig:
            mind = await rig.mind()
            await mind.send_raw(b"\r\n\n" + b'{"v":1,"type":"nope","ts":1}\n')
            assert await mind.next() == Error("unknown type: nope")  # a JSON object: still a mind
            await mind.send_raw(b'{"v":1,"type":"cancel","ts":1,"guid":"101"}\n')
            assert await mind.next() == Error("bad field: guid")
            await mind.send(SPEAK)
            await eventually(lambda: rig.got)
            assert rig.got == [SPEAK] and rig.server.connected
            await mind.close()
    run(go())


def test_voice_messages_from_the_mind_are_refused():
    async def go():
        async with Rig() as rig:
            mind = await rig.mind()
            await mind.send(SPEAK)
            await mind.send(Join("raid"))
            assert await mind.next() == Error("unknown type: join")
            await mind.send(SPEAK)
            await eventually(lambda: len(rig.got) == 2)
            assert rig.got == [SPEAK, SPEAK]
            await mind.close()
    run(go())


def test_second_mind_refused():
    async def go():
        async with Rig() as rig:
            first = await rig.mind()
            second = await FakeMind.connect(rig.server.port)
            assert await second.next() == Error(ANOTHER_MIND)
            await second.wait_eof()
            assert second.received == [Error(ANOTHER_MIND)]  # no hello for the second
            await first.send(SPEAK)
            await eventually(lambda: rig.got)
            assert await rig.server.send(STATUS) is True
            assert await first.next() == STATUS
            assert (rig.connects, rig.disconnects, rig.hellos) == (1, 0, 1)
            await second.close()
            await first.close()
    run(go())


def test_send_without_a_mind_returns_false():
    async def go():
        rig = Rig()
        assert await rig.server.send(STATUS) is False  # not started
        async with rig:
            assert await rig.server.send(STATUS) is False
            mind = await rig.mind()
            await eventually(lambda: rig.server.connected)
            await mind.close()
            await eventually(lambda: not rig.server.connected)
            assert await rig.server.send(STATUS) is False
    run(go())


def test_reconnect():
    async def go():
        async with Rig() as rig:
            first = await rig.mind()
            await eventually(lambda: rig.connects == 1)
            await first.close()
            await eventually(lambda: rig.disconnects == 1)
            second = await FakeMind.connect(rig.server.port)
            assert type(await second.next()) is Hello  # a fresh hello
            await eventually(lambda: rig.connects == 2)
            assert (rig.disconnects, rig.hellos) == (1, 2)
            await second.send(SPEAK)
            await eventually(lambda: rig.got == [SPEAK])
            await second.close()
            await eventually(lambda: rig.disconnects == 2)
    run(go())


def test_line_too_long_closes():
    async def go():
        async with Rig() as rig:
            mind = await rig.mind()
            await mind.send(SPEAK)
            await mind.send_raw(b"x" * 70000 + b"\n")
            assert await mind.next() == Error("line too long")
            await mind.wait_eof()
            await eventually(lambda: rig.disconnects == 1)
            assert rig.got == [SPEAK]
            await mind.close()
    run(go())


def test_a_mind_that_stops_reading_is_dropped(monkeypatch):
    """SEND_TIMEOUT_S: a send never waits longer than that on a mind that reads nothing. That
    mind is dropped, on_disconnect runs once, and the slot is free for the next mind."""
    monkeypatch.setattr(server_module, "SEND_TIMEOUT_S", 0.2)
    big = Heard("x" * 30000, "x" * 30000, 0.5, "raid", 1, 1)  # about 60 KB a line

    async def go():
        async with Rig() as rig:
            loop = asyncio.get_running_loop()
            stuck = socket.socket()  # a mind that never reads, with a small receive buffer
            stuck.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
            stuck.setblocking(False)
            try:
                await loop.sock_connect(stuck, ("127.0.0.1", rig.server.port))
                await eventually(lambda: rig.connects == 1)
                end, sends = loop.time() + 10.0, 0
                while await rig.server.send(big):
                    sends += 1
                    assert loop.time() < end, f"still sending after {sends} lines"
                assert not rig.server.connected and await rig.server.send(STATUS) is False
                await eventually(lambda: rig.disconnects == 1)
                mind = await rig.mind()  # a hello: the slot is free again
                await eventually(lambda: rig.connects == 2)
                await mind.close()
            finally:
                stuck.close()
    run(go())


def test_a_failing_handler_keeps_the_mind():
    async def go():
        async with Rig(fail_first=True) as rig:
            mind = await rig.mind()
            await mind.send(SPEAK)
            await mind.send(SPEAK)
            await eventually(lambda: rig.got)
            assert rig.got == [SPEAK] and rig.server.connected
            await mind.close()
    run(go())


def test_close_ends_the_mind():
    async def go():
        rig = Rig()
        async with rig:
            mind = await rig.mind()
            await eventually(lambda: rig.connects == 1)
        await mind.wait_eof()
        assert rig.disconnects == 1 and not rig.server.connected
        assert await rig.server.send(STATUS) is False
        await rig.server.close()  # a second close is a no-op
        await mind.close()
    run(go())


def test_a_broken_hello_serves_nobody(caplog):
    """A hello that cannot be encoded is logged as the service's own error, never left to
    asyncio's handler, and the connection closes with nothing sent."""
    async def go():
        bad = lambda: Hello(SERVER_NAME, "0.1.0", "kokoro", "", "", "\\")  # noqa: E731 (a str)
        async with Rig(hello=bad) as rig:
            mind = await FakeMind.connect(rig.server.port)
            await mind.wait_eof()
            assert mind.received == [] and rig.connects == 0 and not rig.server.connected
            await mind.close()
    with caplog.at_level(logging.INFO):
        run(go())
    assert any(r.name == "leeroy_voice" and r.getMessage().startswith(
        "mind_hello_failed: error=WireError") for r in caplog.records)
    # asyncio's "Unhandled exception in client_connected_cb" is an ERROR; its debug mode
    # (python -X dev) adds INFO lines and slow-callback WARNINGs of its own
    assert not [r.getMessage() for r in caplog.records
                if r.name == "asyncio" and r.levelno >= logging.ERROR]


def test_on_disconnect_runs_before_the_next_connect():
    """A mind that reconnects while the old connection's on_disconnect is still running is
    connected only after it: the callbacks never interleave."""
    async def go():
        order = []
        gate = asyncio.Event()

        async def on_disconnect():
            order.append("disconnect")
            await gate.wait()
            order.append("disconnect done")

        async def on_connect():
            order.append("connect")

        async def on_message(msg):
            pass

        server = MindServer("127.0.0.1", 0, on_message, on_connect=on_connect,
                            on_disconnect=on_disconnect,
                            hello=lambda: Hello(SERVER_NAME, "0.1.0", (), "", "", "\\"))
        await server.start()
        try:
            first = await FakeMind.connect(server.port)
            await first.next()
            await first.close()
            await eventually(lambda: order == ["connect", "disconnect"])
            second = await FakeMind.connect(server.port)
            await asyncio.sleep(0.1)
            assert order == ["connect", "disconnect"] and second.received == []
            gate.set()
            assert type(await second.next()) is Hello
            await eventually(lambda: order[-1] == "connect")
            assert order == ["connect", "disconnect", "disconnect done", "connect"]
            await second.close()
        finally:
            gate.set()
            await server.close()
    run(go())


def test_a_callback_may_be_a_plain_function(caplog):
    """Sync or async, each runs: the service's handlers do not wait for anything."""
    got, events = [], []

    async def go():
        server = MindServer("127.0.0.1", 0, got.append, on_connect=lambda: events.append("up"),
                            on_disconnect=lambda: events.append("down"),
                            hello=lambda: Hello(SERVER_NAME, "0", (), "", "", "\\"))
        await server.start()
        mind = await FakeMind.connect(server.port)
        await mind.next()
        await mind.send(SPEAK)
        await eventually(lambda: got == [SPEAK])
        await mind.close()
        await eventually(lambda: events == ["up", "down"])
        await server.close()
    with caplog.at_level(logging.INFO, logger="leeroy_voice"):
        run(go())
    assert "failed" not in caplog.text
