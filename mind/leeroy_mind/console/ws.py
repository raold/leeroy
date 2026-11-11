"""A small WebSocket client (RFC 6455) for the voice service's page socket, standard library
only. Text messages only, no extensions (the voice service turns compression off), masked as a
client must; it answers pings and close frames. read_frame and encode_frame serve the tests'
fake server too."""
import asyncio
import base64
import hashlib
import os
import struct
from contextlib import suppress

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
CONT, TEXT, BINARY, CLOSE, PING, PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA
MAX_MESSAGE = 4 << 20  # a voice state with its 200 transcript lines is far below this
HEAD_LIMIT = 16384
FRAME_IDLE_S = 45.0  # voice's websockets server sends protocol pings every 20 s
WRITE_S = 5.0


class WsError(Exception):
    """The connection broke the protocol, or could not be made."""


class WsClosed(WsError):
    """The connection has closed."""


def accept_key(key: str) -> str:
    return base64.b64encode(hashlib.sha1((key + GUID).encode("ascii")).digest()).decode("ascii")


def encode_frame(opcode: int, payload: bytes, *, mask: bool, fin: bool = True) -> bytes:
    head = bytearray([(0x80 if fin else 0) | opcode])
    n = len(payload)
    bit = 0x80 if mask else 0
    if n < 126:
        head.append(bit | n)
    elif n < 1 << 16:
        head.append(bit | 126)
        head += struct.pack("!H", n)
    else:
        head.append(bit | 127)
        head += struct.pack("!Q", n)
    if not mask:
        return bytes(head) + payload
    key = os.urandom(4)
    return bytes(head) + key + bytes(b ^ key[i % 4] for i, b in enumerate(payload))


async def read_frame(reader: asyncio.StreamReader, *, max_size: int,
                     masked: bool) -> tuple[bool, int, bytes]:
    """(fin, opcode, payload). `masked`: whether the peer must mask (a server's frames must
    not be; a client's must)."""
    try:
        b0, b1 = await reader.readexactly(2)
        if b0 & 0x70:
            raise WsError("reserved bits set")
        n = b1 & 0x7F
        if n == 126:
            n = struct.unpack("!H", await reader.readexactly(2))[0]
        elif n == 127:
            n = struct.unpack("!Q", await reader.readexactly(8))[0]
        if bool(b1 & 0x80) != masked:
            raise WsError("bad masking")
        if n > max_size:
            raise WsError("frame too large")
        key = await reader.readexactly(4) if masked else b""
        payload = await reader.readexactly(n)
    except asyncio.IncompleteReadError:
        raise WsClosed("connection closed") from None
    if masked:
        payload = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
    return bool(b0 & 0x80), b0 & 0x0F, payload


class WsClient:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, *,
                 max_size: int = MAX_MESSAGE, idle_s: float = FRAME_IDLE_S):
        self._reader, self._writer = reader, writer
        self._max_size = max_size
        self._idle_s = idle_s
        self._write = asyncio.Lock()
        self.closed = False

    @classmethod
    async def connect(cls, host: str, port: int, path: str, *, origin: str,
                      timeout: float = 5.0, max_size: int = MAX_MESSAGE) -> "WsClient":
        """Open and upgrade. The Host header is host:port; Origin is `origin` (the voice page
        refuses any other). WsError when the server does not accept."""
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port, limit=HEAD_LIMIT), timeout)
        except (OSError, asyncio.TimeoutError) as e:
            raise WsError(f"cannot connect ({type(e).__name__})") from None
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
                   f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                   f"Sec-WebSocket-Version: 13\r\nOrigin: {origin}\r\n\r\n")
        try:
            writer.write(request.encode("ascii"))
            await asyncio.wait_for(writer.drain(), timeout)
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout)
        except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError,
                asyncio.LimitOverrunError) as e:
            writer.close()
            raise WsError(f"no upgrade ({type(e).__name__})") from None
        lines = head.decode("latin-1").split("\r\n")
        status = lines[0].split(" ")
        headers = {}
        for line in lines[1:]:
            name, sep, value = line.partition(":")
            if sep:
                headers.setdefault(name.strip().lower(), value.strip())
        if (len(status) < 2 or status[1] != "101"
                or headers.get("upgrade", "").lower() != "websocket"
                or headers.get("sec-websocket-accept") != accept_key(key)):
            writer.close()
            code = status[1] if len(status) > 1 else "?"
            raise WsError(f"upgrade refused (HTTP {code})")
        if headers.get("sec-websocket-extensions"):
            writer.close()
            raise WsError("unexpected extension")
        return cls(reader, writer, max_size=max_size)

    async def _send(self, opcode: int, payload: bytes) -> None:
        async with self._write:
            try:
                self._writer.write(encode_frame(opcode, payload, mask=True))
                await asyncio.wait_for(self._writer.drain(), WRITE_S)
            except (OSError, RuntimeError, asyncio.TimeoutError) as e:
                self.closed = True
                raise WsClosed(f"send failed ({type(e).__name__})") from None

    async def send(self, text: str) -> None:
        if self.closed:
            raise WsClosed("connection closed")
        await self._send(TEXT, text.encode("utf-8"))

    async def recv(self) -> str:
        """The next text message; pings are answered on the way. WsClosed at the end."""
        parts: list[bytes] = []
        size = 0
        while True:
            if self.closed:
                raise WsClosed("connection closed")
            try:
                fin, opcode, payload = await asyncio.wait_for(
                    read_frame(self._reader, max_size=self._max_size, masked=False), self._idle_s)
            except asyncio.TimeoutError:
                await self.close()
                raise WsClosed("voice heartbeat timed out") from None
            except WsError:
                await self.close()
                raise
            if opcode == PING:
                with suppress(WsClosed):
                    await self._send(PONG, payload)
                continue
            if opcode == PONG:
                continue
            if opcode == CLOSE:
                with suppress(WsClosed):
                    await self._send(CLOSE, payload[:2])
                self.closed = True  # answered: close() sends no second frame
                await self.close()
                raise WsClosed("closed by the server")
            if opcode == BINARY or (opcode == CONT and not parts) or (opcode == TEXT and parts):
                await self.close()
                raise WsError("unexpected frame")
            if opcode not in (TEXT, CONT):
                await self.close()
                raise WsError("unknown opcode")
            size += len(payload)
            if size > self._max_size:
                await self.close()
                raise WsError("message too large")
            parts.append(payload)
            if fin:
                try:
                    return b"".join(parts).decode("utf-8")
                except UnicodeDecodeError:
                    await self.close()
                    raise WsError("not UTF-8") from None

    async def close(self) -> None:
        if self.closed and self._writer.is_closing():
            return
        if not self.closed:
            self.closed = True
            with suppress(Exception):
                async with self._write:
                    self._writer.write(encode_frame(CLOSE, struct.pack("!H", 1000), mask=True))
        self._writer.close()
        with suppress(Exception):
            await asyncio.wait_for(self._writer.wait_closed(), 2.0)
