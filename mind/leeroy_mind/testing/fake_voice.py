"""Test doubles for leeroy-voice: FakeVoice is a TCP voice service on the loopback (it greets,
records what it receives and can push messages); FakeVoiceClient stands in for VoiceClient
inside a MindApp. Both hold the wire as the real ends do: a message the mind's VoiceClient could
not encode raises its WireError, and a line leeroy-voice's MindServer would refuse is answered
(or the connection closed) as it answers it, and kept in `refused`."""
import asyncio
import time

from leeroy_mind.voice import wire
from leeroy_mind.voice.client import VoiceDown


def _now_ms() -> int:
    return int(time.time() * 1000)


def default_hello() -> wire.Hello:
    return wire.Hello(wire.SERVER_NAME, "0.1.0", ("kokoro",), "Mic A", "Speakers A", "\\")


class FakeVoice:
    def __init__(self, port: int = 0, hello: wire.Hello | None = None):
        self.port = port
        self.hello = hello or default_hello()
        self.received: list[wire.Message] = []
        self.refused: list[str] = []  # the error each refused line was answered with
        self._server: asyncio.Server | None = None
        self._writers: list[asyncio.StreamWriter] = []

    async def start(self) -> None:
        """Listen (again, after close(), on the same port)."""
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", self.port,
                                                  limit=wire.MAX_LINE_BYTES + 1)
        self.port = self._server.sockets[0].getsockname()[1]

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            self._server = None
        for w in self._writers:
            w.close()
        self._writers.clear()
        await asyncio.sleep(0)

    async def push(self, msg: wire.Message) -> None:
        for w in list(self._writers):
            w.write(wire.encode(msg, _now_ms()))
            await w.drain()

    async def push_raw(self, data: bytes) -> None:
        for w in list(self._writers):
            w.write(data)
            await w.drain()

    async def _error(self, writer: asyncio.StreamWriter, reply: str) -> None:
        self.refused.append(reply)
        writer.write(wire.encode(wire.Error(reply), _now_ms()))
        await writer.drain()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """MindServer._read's rules: a bad first line (or any line too long) is answered and the
        connection closed; a later bad line or a type the mind does not send is answered and
        skipped."""
        self._writers.append(writer)
        writer.write(wire.encode(self.hello, _now_ms()))
        try:
            await writer.drain()
            first = True
            while True:
                try:
                    raw = await reader.readuntil(b"\n")
                except asyncio.LimitOverrunError:
                    await self._error(writer, wire.LINE_TOO_LONG)
                    return
                except asyncio.IncompleteReadError:
                    return
                line = raw.strip()
                if not line:
                    continue
                try:
                    msg = wire.parse(line)
                except wire.WireError as e:
                    await self._error(writer, e.reply)
                    if e.reply == wire.LINE_TOO_LONG or (first and e.reply == wire.BAD_JSON):
                        return
                    first = False
                    continue
                first = False
                name = wire.type_name(msg)
                if name not in wire.FROM_MIND:
                    await self._error(writer, wire.UNKNOWN_TYPE + name)
                    continue
                self.received.append(msg)
        except OSError:
            pass
        finally:
            writer.close()
            if writer in self._writers:
                self._writers.remove(writer)


class FakeVoiceClient:
    """In process: records what is sent, once it has been through the real client's encode and
    the service's parse (a WireError raises, as VoiceClient.send does, and is kept in
    `refused`). Set `greeted` and `status` by hand."""

    def __init__(self):
        self.sent: list[wire.Message] = []
        self.refused: list[wire.WireError] = []
        self.greeted = False
        self.hello: wire.Hello | None = None
        self.status: wire.Status | None = None

    def send(self, msg: wire.Message) -> None:
        if not self.greeted:
            raise VoiceDown("leeroy-voice is not connected")
        try:
            heard = wire.parse(wire.encode(msg, _now_ms()))
            if wire.type_name(heard) not in wire.FROM_MIND:
                raise wire.WireError(wire.UNKNOWN_TYPE + wire.type_name(heard))
        except wire.WireError as e:
            self.refused.append(e)
            raise
        self.sent.append(msg)

    async def run(self) -> None:
        await asyncio.Event().wait()

    async def close(self) -> None:
        pass
