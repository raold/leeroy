"""The mind's port: one mind at a time on 127.0.0.1:7879, the bridge's conventions.

On accept a mind gets `hello` at once. A second connection while one is held gets error
"another mind is connected" and is closed. The first non-empty line must be a JSON object, or
the connection is answered "bad json" and closed before anything it sent is handled (a web page
posting to the port). Later bad lines are answered with an error and skipped; a line over
MAX_LINE_BYTES is answered "line too long" and the connection closed. The mind's messages reach
on_message one at a time, in order; a message type the mind does not send is answered
"unknown type: <t>". on_connect runs after the hello and on_disconnect once when that mind is
gone, and the two never overlap: a mind that reconnects meanwhile waits for on_disconnect. Each
callback may be a plain function or a coroutine."""
import asyncio
import inspect
import logging
import time
from typing import Callable

from .logs import log
from .wire import (ANOTHER_MIND, BAD_JSON, FROM_MIND, LINE_TOO_LONG, MAX_LINE_BYTES,
                   UNKNOWN_TYPE, Error, Hello, Message, WireError, encode, parse, type_name)

SEND_TIMEOUT_S = 5.0  # a mind that stops reading for this long is dropped, so sends never stall
CLOSE_TIMEOUT_S = 1.0


def _now_ms() -> int:
    return int(time.time() * 1000)


class _Mind:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.reader = reader
        self.writer = writer
        self.ready = False  # its hello is out: sends may go to it
        self.closing = False
        peer = writer.get_extra_info("peername")
        self.peer = f"{peer[0]}:{peer[1]}" if isinstance(peer, tuple) else str(peer)


class MindServer:
    def __init__(self, host: str, port: int, on_message: Callable[[Message], object],
                 *, on_connect: Callable[[], object] | None = None,
                 on_disconnect: Callable[[], object] | None = None,
                 hello: Callable[[], Hello]):
        self._host = host
        self._port = port
        self._on_message = on_message
        self._on_connect = on_connect
        self._on_disconnect = on_disconnect
        self._hello = hello
        self._server: asyncio.Server | None = None
        self._mind: _Mind | None = None  # the connection holding the slot
        self._lifecycle = asyncio.Lock()  # on_connect and on_disconnect never overlap
        self._tasks: set[asyncio.Task] = set()

    @property
    def connected(self) -> bool:
        """A mind holds the port and has had its hello."""
        mind = self._mind
        return mind is not None and mind.ready and not mind.closing

    @property
    def port(self) -> int:
        """The bound port (tests bind 0); 0 before start()."""
        if self._server is None or not self._server.sockets:
            return 0
        return self._server.sockets[0].getsockname()[1]

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._accept, self._host, self._port,
                                                  limit=MAX_LINE_BYTES + 1)
        log("mind_port_open", host=self._host, port=self.port)

    async def close(self) -> None:
        """Stop listening and drop the mind (on_disconnect runs). Idempotent. Never await it
        from a callback: it waits for the connection's handler to end."""
        server, self._server = self._server, None
        if server is not None:
            server.close()
        mind = self._mind
        if mind is not None:
            mind.closing = True
            mind.writer.close()
        others = self._tasks - {asyncio.current_task()}
        if others:
            await asyncio.wait(others)
        if server is not None:
            await server.wait_closed()

    async def send(self, msg: Message) -> bool:
        """Send a message to the mind: False when none is connected or the write failed. A
        message that breaks the protocol raises WireError (a bug, not a dead mind)."""
        line = encode(msg, _now_ms())
        mind = self._mind
        if mind is None or not mind.ready or mind.closing:
            return False
        return await self._write(mind, line)

    async def _write(self, mind: _Mind, line: bytes) -> bool:
        try:
            mind.writer.write(line)
            await asyncio.wait_for(mind.writer.drain(), SEND_TIMEOUT_S)
            return True
        except asyncio.TimeoutError:
            log("mind_send_stalled", level=logging.WARNING, peer=mind.peer)
            mind.closing = True
            mind.writer.transport.abort()  # close() would wait for the stuck buffer to flush
            return False
        except (ConnectionError, OSError, RuntimeError):
            pass  # the read side ends the connection
        mind.closing = True
        mind.writer.close()
        return False

    async def _refuse(self, mind: _Mind, message: str) -> None:
        """Answer with an error and close."""
        mind.closing = True
        try:
            mind.writer.write(encode(Error(message), _now_ms()))
            await asyncio.wait_for(mind.writer.drain(), CLOSE_TIMEOUT_S)
        except (asyncio.TimeoutError, ConnectionError, OSError, RuntimeError):
            pass
        mind.writer.close()

    async def _call(self, what: str, callback, *args) -> None:
        if callback is None:
            return
        try:
            result = callback(*args)
            if inspect.isawaitable(result):
                await result
        except Exception as e:
            log(f"mind_{what}_failed", level=logging.ERROR, error=e.__class__.__name__,
                exc_info=True)

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        self._tasks.add(task)
        mind = _Mind(reader, writer)
        try:
            if self._server is None:  # closing
                writer.close()
                return
            if self._mind is not None:
                log("mind_refused", level=logging.WARNING, peer=mind.peer)
                await self._refuse(mind, ANOTHER_MIND)
                return
            self._mind = mind  # the slot is taken here, before any await
            try:
                await self._serve(mind)
            finally:
                if self._mind is mind:
                    self._mind = None
                mind.closing = True
                writer.close()
                if mind.ready:
                    async with self._lifecycle:
                        await self._call("disconnect", self._on_disconnect)
                    log("mind_disconnected", peer=mind.peer)
        finally:
            try:
                await asyncio.wait_for(writer.wait_closed(), CLOSE_TIMEOUT_S)
            except (asyncio.TimeoutError, ConnectionError, OSError):
                pass
            self._tasks.discard(task)

    async def _serve(self, mind: _Mind) -> None:
        async with self._lifecycle:
            if mind.closing:
                return
            try:
                line = encode(self._hello(), _now_ms())
            except Exception as e:  # a bug in the hello callable: no mind is served
                log("mind_hello_failed", level=logging.ERROR, error=e.__class__.__name__,
                    exc_info=True)
                return
            if not await self._write(mind, line):
                return
            mind.ready = True
            log("mind_connected", peer=mind.peer)
            await self._call("connect", self._on_connect)
        await self._read(mind)

    async def _read(self, mind: _Mind) -> None:
        first = True
        while not mind.closing:
            try:
                raw = await mind.reader.readuntil(b"\n")
            except asyncio.LimitOverrunError:
                log("mind_line_too_long", level=logging.WARNING, peer=mind.peer)
                await self._refuse(mind, LINE_TOO_LONG)
                return
            except (asyncio.IncompleteReadError, ConnectionError, OSError):
                return
            line = raw.strip()
            if not line:
                continue
            try:
                msg = parse(line)
            except WireError as e:
                if e.reply == LINE_TOO_LONG or (first and e.reply == BAD_JSON):
                    log("mind_line_refused", level=logging.WARNING, peer=mind.peer,
                        reply=e.reply, first=first)
                    await self._refuse(mind, e.reply)
                    return
                first = False
                log("mind_line_rejected", level=logging.WARNING, reply=e.reply)
                await self._write(mind, encode(Error(e.reply), _now_ms()))
                continue
            first = False
            name = type_name(msg)
            if name not in FROM_MIND:
                log("mind_line_rejected", level=logging.WARNING, reply=UNKNOWN_TYPE + name)
                await self._write(mind, encode(Error(UNKNOWN_TYPE + name), _now_ms()))
                continue
            await self._call("message", self._on_message, msg)
