"""The console's HTTP/1.1: one request per connection (every response says Connection: close),
plus a server-sent event stream that stays open. Standard library only, as is the rest of the
mind. Limits: a 16 KiB head of at most 100 headers, a 16 KiB body, 10 s to send a request.

handler(request) returns a Response or a Stream. A handler that raises answers 500 with no
detail; its exception's class is logged, never its text."""
import asyncio
import json
import logging
import socket
from contextlib import suppress
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import AsyncIterator, Awaitable, Callable
from urllib.parse import parse_qs, urlsplit

MAX_HEAD = 16384
MAX_HEADERS = 100
MAX_BODY = 16384
READ_S = 10.0

log = logging.getLogger("leeroy_mind.console")


class BadRequest(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


@dataclass
class Request:
    method: str
    target: str
    path: str
    query: dict[str, list[str]]
    headers: list[tuple[str, str]]  # names lower-cased, in order
    body: bytes = b""

    def all(self, name: str) -> list[str]:
        return [v for k, v in self.headers if k == name]

    def one(self, name: str) -> str | None:
        """The header's value when it is sent exactly once."""
        values = self.all(name)
        return values[0] if len(values) == 1 else None

    def cookies(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for header in self.all("cookie"):
            for part in header.split(";"):
                name, sep, value = part.strip().partition("=")
                if sep and name and name not in out:
                    out[name] = value
        return out

    def json(self):
        """The body as a JSON object; BadRequest otherwise."""
        try:
            value = json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            raise BadRequest(400, "the body is not JSON") from None
        if not isinstance(value, dict):
            raise BadRequest(400, "the body is not a JSON object")
        return value


@dataclass
class Response:
    status: int
    body: bytes = b""
    content_type: str = "text/plain; charset=utf-8"
    headers: list[tuple[str, str]] = field(default_factory=list)

    def encode(self) -> bytes:
        reason = HTTPStatus(self.status).phrase
        lines = [f"HTTP/1.1 {self.status} {reason}", "Connection: close",
                 f"Content-Length: {len(self.body)}", "Cache-Control: no-store",
                 "X-Content-Type-Options: nosniff"]
        if self.body or self.status != 204:
            lines.append(f"Content-Type: {self.content_type}")
        lines += [f"{k}: {v}" for k, v in self.headers]
        return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + self.body


def text(status: int, message: str, headers=()) -> Response:
    return Response(status, (message.rstrip("\n") + "\n").encode("utf-8"), headers=list(headers))


def encode_json(value) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), allow_nan=False)


def json_response(status: int, value, headers=()) -> Response:
    return Response(status, encode_json(value).encode("ascii"), "application/json",
                    list(headers))


class Stream:
    """A response that stays open: the head, then each chunk `chunks` yields, until it ends or
    the client goes. `closed()` is called once at the end either way."""

    def __init__(self, chunks: AsyncIterator[bytes], *, content_type: str,
                 headers=(), closed: Callable[[], None] | None = None):
        self.chunks = chunks
        self.content_type = content_type
        self.headers = list(headers)
        self.closed = closed

    async def run(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = ["HTTP/1.1 200 OK", f"Content-Type: {self.content_type}", "Cache-Control: no-store",
                "Connection: close", "X-Content-Type-Options: nosniff",
                *[f"{k}: {v}" for k, v in self.headers]]
        gone = asyncio.ensure_future(reader.read(1))  # EOF: the client has gone
        try:
            writer.write(("\r\n".join(head) + "\r\n\r\n").encode("latin-1"))
            await writer.drain()
            iterator = aiter(self.chunks)
            while True:
                step = asyncio.ensure_future(anext(iterator))
                done, _ = await asyncio.wait({step, gone}, return_when=asyncio.FIRST_COMPLETED)
                if step not in done:
                    step.cancel()
                    with suppress(asyncio.CancelledError, StopAsyncIteration):
                        await step
                    break
                try:
                    chunk = step.result()
                except StopAsyncIteration:
                    break
                writer.write(chunk)
                await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            gone.cancel()
            with suppress(BaseException):
                await gone
            with suppress(BaseException):
                await self.chunks.aclose()
            if self.closed is not None:
                self.closed()


Handler = Callable[[Request], Awaitable["Response | Stream"]]


async def read_request(reader: asyncio.StreamReader) -> Request:
    try:
        head = await reader.readuntil(b"\r\n\r\n")
    except asyncio.LimitOverrunError:
        raise BadRequest(431, "request head too large") from None
    except asyncio.IncompleteReadError as e:
        raise ConnectionResetError("closed before a request") from e
    if len(head) > MAX_HEAD:
        raise BadRequest(431, "request head too large")
    try:
        lines = head.decode("latin-1").split("\r\n")[:-2]
    except UnicodeDecodeError:  # latin-1 decodes anything; kept for clarity
        raise BadRequest(400, "bad request") from None
    parts = lines[0].split(" ") if lines else []
    if len(parts) != 3 or not parts[2].startswith("HTTP/1."):
        raise BadRequest(400, "bad request line")
    method, target, _ = parts
    if not target.startswith("/"):
        raise BadRequest(400, "bad request target")
    if len(lines) - 1 > MAX_HEADERS:
        raise BadRequest(431, "too many headers")
    headers = []
    for line in lines[1:]:
        name, sep, value = line.partition(":")
        if not sep or not name or name != name.strip():
            raise BadRequest(400, "bad header")
        headers.append((name.lower(), value.strip()))
    split = urlsplit(target)
    request = Request(method, target, split.path, parse_qs(split.query, keep_blank_values=True),
                      headers)
    if request.all("transfer-encoding"):
        raise BadRequest(411, "send a Content-Length, not chunks")
    lengths = request.all("content-length")
    if len(lengths) > 1:
        raise BadRequest(400, "more than one Content-Length")
    if lengths:
        if not lengths[0].isdigit():
            raise BadRequest(400, "bad Content-Length")
        size = int(lengths[0])
        if size > MAX_BODY:
            raise BadRequest(413, "body too large")
        try:
            request.body = await reader.readexactly(size)
        except asyncio.IncompleteReadError as e:
            raise ConnectionResetError("closed inside the body") from e
    return request


async def _connection(handler: Handler, reader, writer) -> None:
    try:
        try:
            request = await asyncio.wait_for(read_request(reader), READ_S)
        except BadRequest as e:
            response = text(e.status, str(e))
        except (asyncio.TimeoutError, ConnectionError, OSError):
            return
        else:
            try:
                response = await handler(request)
            except BadRequest as e:
                response = text(e.status, str(e))
            except Exception as e:
                log.error("console_request_failed: error=%s", type(e).__name__)
                response = text(500, "internal error")
        if isinstance(response, Stream):
            await response.run(reader, writer)
        else:
            writer.write(response.encode())
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        writer.close()
        with suppress(Exception):
            await writer.wait_closed()


def listen_socket(host: str, port: int) -> socket.socket:
    """A listening socket that another process cannot share (SO_EXCLUSIVEADDRUSE on Windows).
    OSError when the port is taken."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        sock.bind((host, port))
        sock.listen(64)
        sock.setblocking(False)
    except BaseException:
        sock.close()
        raise
    return sock


async def serve(handler: Handler, host: str, port: int) -> asyncio.Server:
    sock = listen_socket(host, port)
    try:
        return await asyncio.start_server(lambda r, w: _connection(handler, r, w), sock=sock,
                                          limit=MAX_HEAD)
    except BaseException:
        sock.close()
        raise
