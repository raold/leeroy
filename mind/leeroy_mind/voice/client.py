"""Asyncio client for leeroy-voice (NDJSON over TCP). Nothing in the mind waits on it: send()
is synchronous and raises VoiceDown when the service is not there."""
import asyncio
import logging
from typing import Awaitable, Callable

from leeroy_mind.clock import Clock, now_ms
from leeroy_mind.voice import wire

log = logging.getLogger(__name__)

RECONNECT_S = 5.0  # of the clock, between tries
HELLO_S = 5.0  # real seconds the service has to greet


class VoiceDown(Exception):
    """The service is not connected, or has not greeted yet."""


class VoiceClient:
    def __init__(self, host: str, port: int, on_message: Callable[[wire.Message], Awaitable[None]],
                 *, clock: Clock, on_connection: Callable[[bool], Awaitable[None]] | None = None):
        self._host = host
        self._port = port
        self._on_message = on_message
        self._on_connection = on_connection
        self._clock = clock
        self._writer: asyncio.StreamWriter | None = None
        self._closing = False
        self.greeted = False
        self.hello: wire.Hello | None = None
        self.status: wire.Status | None = None

    async def run(self) -> None:
        while not self._closing:
            try:
                reader, writer = await asyncio.open_connection(
                    self._host, self._port, limit=wire.MAX_LINE_BYTES + 1)
            except OSError as exc:
                log.info("voice_connect_failed: reason=%s", exc.__class__.__name__)
            else:
                await self._session(reader, writer)
            if self._closing:
                break
            await self._clock.sleep(RECONNECT_S)

    async def _session(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        up = False
        try:
            hello = await asyncio.wait_for(self._read(reader), HELLO_S)
            if not isinstance(hello, wire.Hello):
                log.warning("voice_no_hello: first=%s", type(hello).__name__)
                return
            self._writer, self.hello, self.greeted = writer, hello, True
            self.status = None
            up = True
            if self._on_connection is not None:
                try:
                    await self._on_connection(True)
                except Exception:
                    log.exception("voice_handler_failed")
            while (msg := await self._read(reader)) is not None:
                if isinstance(msg, wire.Status):
                    self.status = msg
                try:
                    await self._on_message(msg)
                except Exception:
                    log.exception("voice_handler_failed")
        except (asyncio.TimeoutError, OSError, asyncio.IncompleteReadError):
            pass
        finally:
            self.greeted = False
            self._writer = None
            writer.close()
            if up and self._on_connection is not None:
                try:
                    await self._on_connection(False)
                except Exception:
                    log.exception("voice_handler_failed")

    async def _read(self, reader: asyncio.StreamReader):
        """The next parsed message; None at the end of the stream. A bad line is skipped."""
        while True:
            try:
                line = await reader.readuntil(b"\n")
            except asyncio.LimitOverrunError as exc:
                log.warning("voice_line_too_long")
                await reader.readexactly(exc.consumed)
                try:
                    await reader.readuntil(b"\n")
                except asyncio.LimitOverrunError:
                    raise ConnectionError("line too long") from None
                continue
            except asyncio.IncompleteReadError:
                return None
            if not line.strip():
                continue
            try:
                return wire.parse(line)
            except wire.WireError as exc:
                log.warning("voice_bad_line: reply=%s", exc.reply)

    def send(self, msg: wire.Message) -> None:
        writer = self._writer
        if not self.greeted or writer is None:
            raise VoiceDown("leeroy-voice is not connected")
        try:
            writer.write(wire.encode(msg, now_ms(self._clock)))
        except OSError:
            raise VoiceDown("leeroy-voice dropped") from None

    async def close(self) -> None:
        self._closing = True
        writer = self._writer
        if writer is not None:
            writer.close()
