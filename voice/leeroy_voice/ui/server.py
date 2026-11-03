"""The voice UI's server: one page and one WebSocket on 127.0.0.1:7880 (`[ui] port`).

GET / answers page.html and /ws is the WebSocket; anything else is 404. Every request, the
WebSocket handshake included, must carry exactly one Host header, 127.0.0.1:<this port>, or it
is answered 403 (against DNS rebinding: a page on another name that resolves to 127.0.0.1 sends
its own Host, so it can neither load the page nor open the socket). The handshake must also
carry Origin http://127.0.0.1:<this port>; the library refuses any other, and none at all, with
403, so no other web page can drive it. Open http://127.0.0.1:<port>/, not localhost. It
listens on 127.0.0.1 only, and the page loads nothing from anywhere else.

The hooks run on the event loop and must not block. A hook that raises is logged (ids and
counts, never text) and the page stays connected. Nothing here is needed to play: with no page
open, broadcast() is a no-op.

Service to page, JSON text, compact and ASCII. The first message of a connection is the state,
written in the same step the page is registered for broadcasts, so every later event follows it:

    {"type": "state",
     "tree":       [{"id", "name", "parent", "locked", "capacity"}],  # the mind's order; parent ""
                                                  # is top level; capacity 0 is unlimited
     "where":      {guid: room id},   # who is where; off comms is absent. He may be listed
     "names":      {guid: name},
     "room":       room id,           # the room he is in, "" when none
     "human":      guid,              # his guid, 0 unknown: it tells the page which `moved` is his
     "speaking":   [guid],            # members whose voice is playing
     "ptt":        bool,              # his key is held
     "volumes":    {guid: dB},        # -12 to 12
     "mutes":      [guid],
     "invites":    [{"from": guid, "channel": room id}],
     "transcript": [{"who", "text", "ts" (unix ms), "kind"}],  # the last 200, oldest first;
                                                  # kind: say, you, whisper or system
     "mind":       bool,              # the mind is connected
     "status":     {"ok", "mic", "gpu", "stt", "ui", "queue", "errors"}}   # the wire's status

A key the hooks leave out is sent empty (`mind` false), one they add is passed on, and sets
become sorted lists. Then the service's events, through broadcast(): `speaking {guid, on}`,
`ptt {down}`, `moved {guid, channel}` ("" is off comms), `invite {from, channel}`,
`refused {channel, reason}`, `transcript {who, text, ts, kind}`, `status {ok, mic, ...}` and
`mind {up}`; a fresh `state` replaces everything.

Page to service: `{"type": "join", "channel"}` (an invite's accept is a join; ignoring one stays
in the page), `{"type": "volume", "guid", "db"}` and `{"type": "mute", "guid", "on"}`. A message
that is not one of these with good arguments is ignored and the page stays: a channel is 1-64
printable characters, a guid an integer 1 to 2^32-1, a dB a finite number (clamped to +-12),
`on` a boolean. A message over 4 KiB closes the page."""
import asyncio
import base64
import email.utils
import hashlib
import http
import json
import logging
import math
import re
import socket
from importlib import resources
from typing import Protocol

from websockets.asyncio.server import ServerConnection, broadcast, serve
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request, Response
from websockets.protocol import State

from ..config import UI_DB_LIMIT
from ..logs import log

HOST = "127.0.0.1"
WS_PATH = "/ws"
MAX_MESSAGE_BYTES = 4096  # the page's messages are a few dozen bytes
TRANSCRIPT_ENTRIES = 200  # what a state keeps of the transcript
MAX_CHANNEL_CHARS = 64
MAX_GUID = 0xFFFFFFFF  # a character's guid is 32 bits
CLOSE_TIMEOUT_S = 2.0  # a page that does not answer a close is dropped after this
MAX_PAGE_BUFFER = 1 << 20  # broadcast() cannot wait: a page this far behind is cut off

EMPTY_STATE = {  # what a state carries for a key the hooks leave out
    "tree": [], "where": {}, "names": {}, "room": "", "human": 0, "speaking": [], "ptt": False,
    "volumes": {}, "mutes": [], "invites": [], "transcript": [], "mind": False, "status": {}}

_LOGGER = logging.getLogger("leeroy_voice.ui.websockets")  # the library's records join ours


class UiHooks(Protocol):
    """What the page's actions and a new page's state ask of the service."""

    def state(self) -> dict:
        """The state a page starts from; see the module docstring for its keys."""
        ...

    def join(self, channel: str) -> None: ...

    def volume(self, guid: int, db: float) -> None:
        """db is a float within +-12."""
        ...

    def mute(self, guid: int, on: bool) -> None: ...


def _json_default(o):
    if isinstance(o, (set, frozenset)):
        return sorted(o)
    raise TypeError(f"{type(o).__name__} is not JSON")


def _encode(obj) -> str:
    """Strict JSON: NaN and infinity would be rejected by the page's parser, so they raise."""
    return json.dumps(obj, ensure_ascii=True, separators=(",", ":"), allow_nan=False,
                      default=_json_default)


def _hash_source(block: str) -> str:
    """A CSP source for an inline block. A browser hashes the block as its HTML parser read it,
    with the newlines normalized."""
    text = block.replace("\r\n", "\n").replace("\r", "\n")
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return "'sha256-" + base64.b64encode(digest).decode("ascii") + "'"


def _block_sources(html: str, tag: str) -> str:
    blocks = re.findall(rf"<{tag}>(.*?)</{tag}>", html, re.S)
    return " ".join(_hash_source(b) for b in blocks) or "'none'"


def _channel(v) -> str | None:
    ok = isinstance(v, str) and 0 < len(v) <= MAX_CHANNEL_CHARS and v.isprintable()
    return v if ok else None


def _guid(v) -> int | None:
    return v if type(v) is int and 0 < v <= MAX_GUID else None


def _db(v) -> float | None:
    """A finite number, held to the page's +-12 dB. (An int is compared as it is: a huge one has
    no float to convert to.)"""
    if type(v) is int or (type(v) is float and math.isfinite(v)):
        return float(max(-UI_DB_LIMIT, min(UI_DB_LIMIT, v)))
    return None


class UiServer:
    """The page and its socket. start() raises OSError when the port is taken. broadcast() is safe
    from any thread. Port 0 binds a free one (tests)."""

    def __init__(self, port: int, hooks: UiHooks):
        if type(port) is not int or not 0 <= port <= 65535:
            raise ValueError(f"port: expected an integer 0-65535, got {port!r}")
        self._requested = port
        self._bound = 0
        self._hooks = hooks
        html = resources.files(__package__).joinpath("page.html").read_bytes()
        text = html.decode("utf-8")
        self._page = html
        self._script_src = _block_sources(text, "script")
        self._style_src = _block_sources(text, "style")
        self._server = None  # websockets' Server while listening
        self._loop: asyncio.AbstractEventLoop | None = None
        self._pages: set[ServerConnection] = set()  # the pages that have had their state

    @property
    def port(self) -> int:
        """The bound port while listening, else the one asked for."""
        return self._bound or self._requested

    @property
    def clients(self) -> int:
        """The pages open now: what the mind's `status.ui` reports."""
        return sum(1 for page in tuple(self._pages) if page.protocol.state is State.OPEN)

    async def start(self) -> None:
        sock = socket.create_server((HOST, self._requested))
        self._bound = sock.getsockname()[1]
        try:
            self._server = await serve(
                self._handle, sock=sock, origins=[f"http://{HOST}:{self._bound}"],
                process_request=self._process_request, server_header=None, compression=None,
                close_timeout=CLOSE_TIMEOUT_S, max_size=MAX_MESSAGE_BYTES, logger=_LOGGER)
        except BaseException:
            sock.close()
            self._bound = 0
            raise
        self._loop = asyncio.get_running_loop()
        log("ui_port_open", host=HOST, port=self._bound)

    async def close(self) -> None:
        """Stop listening and close the pages (going away). Idempotent."""
        server, self._server, self._loop = self._server, None, None
        if server is None:
            return
        server.close()
        await server.wait_closed()
        self._bound = 0
        log("ui_port_closed")

    def broadcast(self, event: dict) -> None:
        """Send one event, a dict with a `type`, to every open page. It never waits and never
        raises: an event that cannot be encoded is logged and dropped."""
        loop = self._loop
        if loop is None:
            return
        try:
            if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                raise ValueError("an event is a dict with a type")
            text = _encode(event)
        except (TypeError, ValueError, RecursionError) as e:
            kind = event.get("type") if isinstance(event, dict) else None
            named = {"kind": kind} if isinstance(kind, str) and len(kind) <= 32 else {}
            log("ui_broadcast_failed", level=logging.ERROR, error=e.__class__.__name__, **named)
            return
        try:
            on_loop = asyncio.get_running_loop() is loop
        except RuntimeError:
            on_loop = False
        if on_loop:
            self._send(text)
            return
        try:
            loop.call_soon_threadsafe(self._send, text)
        except RuntimeError:  # the loop has closed
            pass

    def _send(self, text: str) -> None:
        pages = []
        for page in tuple(self._pages):
            if page.transport.get_write_buffer_size() > MAX_PAGE_BUFFER:
                log("ui_page_stalled", level=logging.WARNING)
                page.transport.abort()  # its handler ends; a reconnect brings a fresh state
            else:
                pages.append(page)
        broadcast(pages, text)  # skips any that is not open

    # -- HTTP --------------------------------------------------------------------------------

    def _process_request(self, connection: ServerConnection, request: Request) -> Response | None:
        host = f"{HOST}:{self.port}"
        if request.headers.get_all("Host") != [host]:
            log("ui_refused", level=logging.WARNING, reason="host")
            return connection.respond(http.HTTPStatus.FORBIDDEN,
                                      f"Forbidden. This page is served at http://{host}/ only.\n")
        path = request.path.partition("?")[0]
        if path == WS_PATH:
            return None  # the handshake goes on; the library checks the Origin
        if path != "/":
            return connection.respond(http.HTTPStatus.NOT_FOUND, "Not found.\n")
        if request.method != "GET":
            response = connection.respond(http.HTTPStatus.METHOD_NOT_ALLOWED,
                                          "Method not allowed.\n")
            response.headers["Allow"] = "GET"
            return response
        return self._page_response()

    def _page_response(self) -> Response:
        # It may load nothing and talk to nothing but its own socket, whatever the page says
        csp = ("default-src 'none'; "
               f"script-src {self._script_src}; style-src {self._style_src}; "
               f"connect-src ws://{HOST}:{self.port}; img-src data:; base-uri 'none'; "
               "form-action 'none'; frame-ancestors 'none'")
        headers = Headers([
            ("Date", email.utils.formatdate(usegmt=True)),
            ("Connection", "close"),
            ("Content-Length", str(len(self._page))),
            ("Content-Type", "text/html; charset=utf-8"),
            ("Cache-Control", "no-store"),
            ("Content-Security-Policy", csp),
            ("X-Content-Type-Options", "nosniff"),
            ("X-Frame-Options", "DENY"),
            ("Referrer-Policy", "no-referrer"),
        ])
        return Response(200, "OK", headers, self._page)

    # -- WebSocket ---------------------------------------------------------------------------

    def _state_text(self) -> str:
        state = {"type": "state", **EMPTY_STATE, **self._hooks.state(), "type": "state"}
        transcript = state["transcript"]
        if isinstance(transcript, (list, tuple)) and len(transcript) > TRANSCRIPT_ENTRIES:
            state["transcript"] = list(transcript[-TRANSCRIPT_ENTRIES:])
        return _encode(state)

    async def _handle(self, ws: ServerConnection) -> None:
        try:
            text = self._state_text()
        except Exception as e:  # the hooks' bug, or a state that is not JSON
            log("ui_state_failed", level=logging.ERROR, error=e.__class__.__name__, exc_info=True)
            await ws.close(code=1011, reason="state unavailable")
            return
        # Written and registered in one step, with no await between: no event can reach this
        # page before its state, and none sent after the snapshot can miss it
        broadcast([ws], text)
        self._pages.add(ws)
        log("ui_page_open", pages=self.clients)
        try:
            async for raw in ws:
                self._on_message(raw)
        except ConnectionClosed:
            pass
        finally:
            self._pages.discard(ws)
            log("ui_page_closed", pages=self.clients)

    def _on_message(self, raw: str | bytes) -> None:
        if not isinstance(raw, str):
            self._ignored("binary")
            return
        try:
            msg = json.loads(raw)
        except (ValueError, RecursionError):
            self._ignored("bad json")
            return
        if not isinstance(msg, dict):
            self._ignored("not an object")
            return
        kind = msg.get("type")
        if kind == "join":
            channel = _channel(msg.get("channel"))
            if channel is None:
                self._ignored("bad field")
            else:
                self._call("join", self._hooks.join, channel)
        elif kind == "volume":
            guid, db = _guid(msg.get("guid")), _db(msg.get("db"))
            if guid is None or db is None:
                self._ignored("bad field")
            else:
                self._call("volume", self._hooks.volume, guid, db)
        elif kind == "mute":
            guid, on = _guid(msg.get("guid")), msg.get("on")
            if guid is None or type(on) is not bool:
                self._ignored("bad field")
            else:
                self._call("mute", self._hooks.mute, guid, on)
        else:
            self._ignored("unknown type")

    @staticmethod
    def _ignored(reason: str) -> None:
        log("ui_message_ignored", reason=reason)

    @staticmethod
    def _call(name: str, hook, *args) -> None:
        try:
            hook(*args)
        except Exception as e:
            log("ui_hook_failed", level=logging.ERROR, hook=name, error=e.__class__.__name__,
                exc_info=True)
