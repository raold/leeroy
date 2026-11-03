"""Test doubles for leeroy-voice: FakeVoice is a TCP voice service on the loopback (it greets,
records what it receives and can push messages); FakeVoiceClient stands in for VoiceClient
inside a MindApp."""
import asyncio
import time

from leeroy_mind.voice import wire
from leeroy_mind.voice.client import VoiceDown


def default_hello() -> wire.Hello:
    return wire.Hello(wire.SERVER_NAME, "0.1.0", ("kokoro",), "Mic A", "Speakers A", "\\")


class FakeVoice:
    def __init__(self, port: int = 0, hello: wire.Hello | None = None):
        self.port = port
        self.hello = hello or default_hello()
        self.received: list[wire.Message] = []
        self._server: asyncio.Server | None = None
        self._writers: list[asyncio.StreamWriter] = []

    async def start(self) -> None:
        """Listen (again, after close(), on the same port)."""
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", self.port)
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
            w.write(wire.encode(msg, int(time.time() * 1000)))
            await w.drain()

    async def push_raw(self, data: bytes) -> None:
        for w in list(self._writers):
            w.write(data)
            await w.drain()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writers.append(writer)
        writer.write(wire.encode(self.hello, int(time.time() * 1000)))
        try:
            await writer.drain()
            while line := await reader.readline():
                try:
                    self.received.append(wire.parse(line))
                except wire.WireError:
                    pass
        except OSError:
            pass
        finally:
            writer.close()
            if writer in self._writers:
                self._writers.remove(writer)


class FakeVoiceClient:
    """In process: records what is sent. Set `greeted` and `status` by hand."""

    def __init__(self):
        self.sent: list[wire.Message] = []
        self.greeted = False
        self.hello: wire.Hello | None = None
        self.status: wire.Status | None = None

    def send(self, msg: wire.Message) -> None:
        if not self.greeted:
            raise VoiceDown("leeroy-voice is not connected")
        self.sent.append(msg)

    async def run(self) -> None:
        await asyncio.Event().wait()

    async def close(self) -> None:
        pass
