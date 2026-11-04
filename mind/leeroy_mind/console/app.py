"""The console's server: the page, its event stream and the endpoints behind its tabs.

Every request must carry exactly one Host header, 127.0.0.1:<port> (against DNS rebinding), an
Origin that is the console's own when it carries one, and a Sec-Fetch-Site of same-origin or none
when it carries one. Every request but the page's own load must also carry the session key, once,
in an X-Console-Key header. The printed URL holds the key in its fragment (/#key=...), which a
browser never sends: the page takes it into memory, drops it from the address bar and sends it
with each fetch, the event stream included. There is no cookie, so no other program on another
127.0.0.1 port is ever handed the key. Anything else is 403. A mutating request (POST) must
also carry the console's Origin and a JSON body; there is no CORS, so another origin's script
cannot read an answer or send a preflighted request (the key header needs one).

    GET  /             the page (static; it carries no key)
    GET  /api/state    the strip, the voice mirror, the text feed and the control rules
    GET  /api/events   the same as a first `snapshot` event, then `status`, `voice`, `chat`
                       and `control` events (server-sent events, a comment every 15 s)
    GET  /api/health   the Mind tab: processes, lock, links, Ollama's resident models, GPU,
                       the night, the latest errors and a content-free log tail
    GET  /api/feed     ?after=<id>: text lines after that id
    POST /api/voice    {"type": "join", "channel"} | {"type": "volume", "guid", "db"} |
                       {"type": "mute", "guid", "on"}: on to the voice service
    POST /api/control  {"service": "mind"|"voice", "action": "start"|"restart"|"stop",
                       "confirm": bool}: a stop or restart while the owner is in game, or while
                       nobody can tell, is answered 409 with a question until it is confirmed"""
import asyncio
import base64
import hashlib
import hmac
import logging
import re
import time
from contextlib import suppress
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Callable

from . import feed, mindlog, netinfo, status
from .control import Controls, Refused
from .httpd import Request, Response, Stream, digits, encode_json, json_response, text
from .voicelink import VoiceRelay

HOST = "127.0.0.1"
STATUS_S = 2.0
SNAPSHOT_S = 15.0  # a settled mind's process list is taken this often (an unclear one's at most)
VOICE_LOCK_S = 15.0  # how long the Mind tab reuses its reading of voice.lock
FEED_S = 1.5
PING_S = 15.0
QUEUE = 500  # events a page may fall behind before it is cut off (it reconnects)
FEED_DAYS = 3.0  # how far back a page's first feed goes
KEY_HEADER = "x-console-key"

log = logging.getLogger("leeroy_mind.console")


def _hash_source(block: str) -> str:
    """A CSP hash source for an inline block, newlines as the browser's parser reads them."""
    normal = block.replace("\r\n", "\n").replace("\r", "\n")
    return "'sha256-" + base64.b64encode(hashlib.sha256(normal.encode()).digest()).decode() + "'"


def _sources(html: str, tag: str) -> str:
    blocks = re.findall(rf"<{tag}>(.*?)</{tag}>", html, re.S)
    return " ".join(_hash_source(b) for b in blocks) or "'none'"


@dataclass
class Settings:
    data_dir: Path
    root: Path
    port: int
    key: str
    bridge_host: str = "127.0.0.1"
    bridge_port: int = 7878
    ollama_url: str = "http://127.0.0.1:11434"
    roles: dict = field(default_factory=dict)  # live, embedding, reflection -> model name
    voice_enabled: bool = False
    voice_ui_port: int | None = 7880
    voice_mind_port: int = 7879
    voice_data_dir: Path | None = None
    config_problem: str | None = None


class _Subscriber:
    def __init__(self):
        self.queue: asyncio.Queue = asyncio.Queue(QUEUE)
        self.dead = False


def sse(kind: str, data) -> bytes:
    return f"event: {kind}\ndata: {encode_json(data)}\n\n".encode("ascii")


class ConsoleApp:
    """`mind`: a control.MindHandle (probe() and launch_exit() are used even with control off);
    `voice`: a lifecycle.VoiceControl or None; the callables are the real probes by default."""

    def __init__(self, settings: Settings, *, mind, controls: Controls, voice=None,
                 connect: Callable | None = None, tcp: Callable = netinfo.tcp_table,
                 ollama: Callable = status.ollama_models, gpu: Callable = status.gpu_headroom,
                 follower: mindlog.LogFollower | None = None, clock: Callable = time.time,
                 monotonic: Callable = time.monotonic):
        self.s = settings
        self.mind = mind
        self.controls = controls
        self.voice = voice
        self.relay = VoiceRelay(lambda: self.s.voice_ui_port, self._voice_event,
                                **({"connect": connect} if connect is not None else {}))
        self._tcp, self._ollama, self._gpu = tcp, ollama, gpu
        self.follower = follower or mindlog.LogFollower(self.s.data_dir / "logs" / "mind.log")
        self._clock, self._monotonic = clock, monotonic
        self.origin = f"http://{HOST}:{settings.port}"
        html = resources.files(__package__).joinpath("page.html").read_bytes()
        self._page = html
        page = html.decode("utf-8")
        self._csp = ("default-src 'none'; "
                     f"script-src {_sources(page, 'script')}; style-src {_sources(page, 'style')}; "
                     "connect-src 'self'; img-src data:; base-uri 'none'; form-action 'none'; "
                     "frame-ancestors 'none'")
        self._subs: set[_Subscriber] = set()
        self._found = None  # the mind's family at the latest process list (None: none taken)
        self._ambiguous: str | None = None
        self._held = False
        self._list_at = float("-inf")  # when the next process list is due
        self._list_every = SNAPSHOT_S
        self._settled = True
        self._voice_lock: tuple | None = None  # (voice.lock's pid or None, when it was read)
        self._conns = None
        self.view = status.MindView("unknown", detail="not checked yet")
        self.bridge: bool | None = None
        self.game = status.GameView(status.UNKNOWN, "not checked yet")
        self._strip: dict = {}
        self._feed_last = 0
        self._tasks: list[asyncio.Task] = []
        self._server = None

    # -- the strip ---------------------------------------------------------------------------

    def strip(self) -> dict:
        voice_up = self.relay.connected
        link = self.relay.mirror.state.get("mind") is True if voice_up else None
        return {"mind": self.view.as_dict(), "bridge": self.bridge,
                "game": {"state": self.game.state, "reason": self.game.reason,
                         "since": self.game.since},
                "voice": {"enabled": self.s.voice_enabled, "service": voice_up, "link": link},
                "control": self.controls.describe()}

    def _probe(self, force: bool) -> None:
        """Worker thread. A probe of a free mind.lock takes it for a moment, and a mind starting
        just then would exit "already running", so the lock is probed each pass only while it is
        held (a held lock cannot be taken), to see a stop at once. Otherwise it is probed only
        with the process list (about a second of CIM), which is taken every SNAPSHOT_S while the
        state is settled, at once when mind.log grows under a free lock (a mind logs only once it
        holds its lock), and while the state is unclear (starting, or a lock or family that is
        not plainly this checkout's mind) after STATUS_S, then twice as long each time up to
        SNAPSHOT_S. While the console starts or stops the mind, the action's own checks are left
        to probe the lock (a starting mind must find it free)."""
        grew = self.follower.poll()
        busy = self.controls.busy is not None and self.controls.busy[0] == "mind"
        if not busy:
            held = self.mind.held() if self._held else False
            now = self._monotonic()
            if (force or now >= self._list_at or held != self._held
                    or (grew and not self._held)):
                found, held, error = self.mind.probe()
                self._found, self._ambiguous = found, error
                settled = error is None and (found is None or bool(found) == bool(held))
                self._list_every = (SNAPSHOT_S if settled else STATUS_S if self._settled
                                    else min(SNAPSHOT_S, self._list_every * 2))
                self._settled = settled
                self._list_at = self._monotonic() + self._list_every
            self._held = held
        self._conns = self._tcp()

    def _derive(self) -> None:
        logview = self.follower.view()
        self.view = status.mind_view(
            self._found, self._held, error=self._ambiguous, run_failed=logview.run_failed,
            failure=logview.failure, launch_exit=self.mind.launch_exit(),
            stopping=bool(self.controls.busy and self.controls.busy[1] in ("stop", "restart")
                          and self.controls.busy[0] == "mind") or self.mind.stopping)
        bridge = None
        if self.view.state == "running":
            tcp = None
            if self.s.bridge_host == "127.0.0.1":
                tcp = netinfo.linked(self._conns, self.s.bridge_port, self.view.pids)
            bridge = tcp if tcp is not None else logview.bridge
        self.bridge = bridge

    def _session(self) -> tuple[dict | None, str | None]:
        """(the session the mind has open or None, why it could not be read or None)."""
        try:
            return feed.read(self.s.data_dir / "mind.db", feed.open_session), None
        except Exception as e:
            log.warning("console_session_unread: error=%s", type(e).__name__)
            return None, type(e).__name__

    async def refresh(self, *, force: bool = False) -> None:
        await asyncio.to_thread(self._probe, force)
        self._derive()
        session, unread = await asyncio.to_thread(self._session) if (
            self.view.state == "running" and self.bridge is True) else (None, None)
        self.game = status.game_view(self.view.state, self.bridge, session, unread=unread)
        self._publish_status()

    def _publish_status(self) -> None:
        strip = self.strip()
        if strip != self._strip:
            self._strip = strip
            self.broadcast("status", strip)

    async def _status_loop(self) -> None:
        while True:
            try:
                await self.refresh()
            except Exception as e:
                log.error("console_status_failed: error=%s", type(e).__name__)
            await asyncio.sleep(STATUS_S)

    # -- the text feed -----------------------------------------------------------------------

    def _feed_since(self, after: int, *, days: float | None = None) -> tuple[list, int]:
        db = self.s.data_dir / "mind.db"

        def question(conn):
            upto = feed.last_id(conn)
            since = self._clock() - days * 86400 if days else None
            return feed.feed(conn, after=after, upto=upto, since=since), upto
        return feed.read(db, question)

    async def _feed_loop(self) -> None:
        try:
            _, self._feed_last = await asyncio.to_thread(self._feed_since, 1 << 62)
        except Exception:
            self._feed_last = 0
        while True:
            await asyncio.sleep(FEED_S)
            if not self._subs:
                continue
            try:
                lines, upto = await asyncio.to_thread(self._feed_since, self._feed_last)
            except Exception as e:
                log.warning("console_feed_unread: error=%s", type(e).__name__)
                continue
            self._feed_last = max(self._feed_last, upto)
            if lines:
                self.broadcast("chat", {"lines": lines})

    # -- voice -------------------------------------------------------------------------------

    def _voice_event(self, msg: dict) -> None:
        if msg.get("type") == "link":
            self._publish_status()
            if not msg.get("up"):
                self.broadcast("voice", {"type": "down"})
            return
        if msg.get("type") == "mind":
            self._publish_status()
        self.broadcast("voice", msg)

    # -- events ------------------------------------------------------------------------------

    def broadcast(self, kind: str, data) -> None:
        if not self._subs:
            return
        chunk = sse(kind, data)
        for sub in tuple(self._subs):
            if sub.dead:
                continue
            try:
                sub.queue.put_nowait(chunk)
            except asyncio.QueueFull:
                sub.dead = True  # too far behind: its stream ends and the page reconnects

    async def snapshot(self) -> dict:
        voice = self.relay.snapshot()
        strip = self.strip()
        try:
            lines, _ = await asyncio.to_thread(self._feed_since, 0, days=FEED_DAYS)
        except Exception as e:
            log.warning("console_feed_unread: error=%s", type(e).__name__)
            lines = []
        return {"status": strip, "voice": voice, "chat": lines}

    async def _events(self, sub: _Subscriber, voice, strip):
        """`voice` and `strip` were taken as the page registered, with no await between: every
        later voice event is in its queue and none before it is."""
        try:
            lines, _ = await asyncio.to_thread(self._feed_since, 0, days=FEED_DAYS)
        except Exception:
            lines = []
        yield sse("snapshot", {"status": strip, "voice": voice, "chat": lines})
        while not sub.dead:
            try:
                chunk = await asyncio.wait_for(sub.queue.get(), PING_S)
            except asyncio.TimeoutError:
                yield b": ping\n\n"
                continue
            if sub.dead:
                return
            yield chunk

    # -- health ------------------------------------------------------------------------------

    def _health(self, tail: list, errors: list) -> dict:
        # The status loop's latest reading: the Mind tab polls, and each process list costs
        # about a second of CIM and each probe of a free lock takes it for a moment
        found, held, error = self._found, self._held, self._ambiguous
        voice = {"enabled": self.s.voice_enabled, "service": self.relay.connected,
                 "ui_port": self.s.voice_ui_port, "mind_port": self.s.voice_mind_port,
                 "pid": None, "detail": ""}
        if self.controls.busy is not None:
            voice["detail"] = "not read while a start, restart or stop runs"
        elif self.voice is not None and self.voice.missing() is None:
            now = self._monotonic()
            if self._voice_lock is None or now - self._voice_lock[1] >= VOICE_LOCK_S:
                try:
                    self._voice_lock = (self.voice.held(), now)
                except Exception as e:
                    voice["detail"] = f"lock not read ({type(e).__name__})"
            if self._voice_lock is not None and not voice["detail"]:
                pid = self._voice_lock[0]
                voice["pid"] = pid or None
                voice["lock"] = pid is not None
        else:
            voice["detail"] = "no voice venv in this checkout"
        try:
            night = feed.read(self.s.data_dir / "mind.db", feed.night)
        except Exception:
            night = None
        return {"mind": {"pids": [p.pid for p in found or []], "lock": held,
                         "ambiguous": error, "data_dir": str(self.s.data_dir),
                         "root": str(self.s.root)},
                "voice": voice,
                "ollama": self._ollama(self.s.ollama_url, self.s.roles),
                "gpu": self._gpu(),
                "night": night,
                "config": self.s.config_problem,
                "errors": errors, "log": tail}

    # -- the gate ----------------------------------------------------------------------------

    def _key_ok(self, value: str | None) -> bool:
        return isinstance(value, str) and hmac.compare_digest(value.encode(), self.s.key.encode())

    def gate(self, req: Request) -> Response | None:
        """None when the request may go on; the refusal otherwise."""
        if req.all("host") != [f"{HOST}:{self.s.port}"]:
            return text(403, f"Forbidden. The console is served at {self.origin}/ only.")
        origins = req.all("origin")
        if len(origins) > 1 or (origins and origins[0] != self.origin):
            return text(403, "Forbidden: another origin.")
        sites = req.all("sec-fetch-site")
        if len(sites) > 1 or (sites and sites[0] not in ("same-origin", "none")):
            return text(403, "Forbidden: not from the console's own page.")
        if req.method == "POST":
            if origins != [self.origin]:
                return text(403, "Forbidden: a change needs the console's own Origin.")
            kind = (req.one("content-type") or "").split(";")[0].strip().lower()
            if kind != "application/json":
                return text(415, "Send JSON.")
        elif req.method != "GET":
            return text(405, "Method not allowed.", [("Allow", "GET, POST")])
        if req.path == "/" and req.method == "GET":
            return None  # the page: static, and its key stays in the browser (the fragment)
        if self._key_ok(req.one(KEY_HEADER)):
            return None
        return text(403, "Forbidden: no console key. Open the URL the console printed when "
                         "it started (the key changes each time it starts).")

    async def handle(self, req: Request):
        refused = self.gate(req)
        if refused is not None:
            return refused
        if req.path == "/":
            if req.method != "GET":
                return text(405, "Method not allowed.", [("Allow", "GET")])
            return self._page_response()
        routes = {("GET", "/api/state"): self._api_state,
                  ("GET", "/api/events"): self._api_events,
                  ("GET", "/api/health"): self._api_health,
                  ("GET", "/api/feed"): self._api_feed,
                  ("POST", "/api/voice"): self._api_voice,
                  ("POST", "/api/control"): self._api_control}
        route = routes.get((req.method, req.path))
        if route is None:
            known = {path for _, path in routes}
            return text(405 if req.path in known else 404,
                        "Method not allowed." if req.path in known else "Not found.")
        return await route(req)

    def _page_response(self) -> Response:
        return Response(200, self._page, "text/html; charset=utf-8", [
            ("Content-Security-Policy", self._csp), ("X-Frame-Options", "DENY"),
            ("Referrer-Policy", "same-origin"), ("Cross-Origin-Opener-Policy", "same-origin"),
            ("Cross-Origin-Resource-Policy", "same-origin")])

    # -- the endpoints -----------------------------------------------------------------------

    async def _api_state(self, req: Request):
        return json_response(200, await self.snapshot())

    async def _api_events(self, req: Request):
        sub = _Subscriber()
        self._subs.add(sub)
        events = self._events(sub, self.relay.snapshot(), self.strip())
        return Stream(events, content_type="text/event-stream",
                      headers=[("X-Accel-Buffering", "no")],
                      closed=lambda: self._subs.discard(sub))

    async def _api_health(self, req: Request):
        tail = self.follower.tail()
        errors = self.follower.view().errors
        health = await asyncio.to_thread(self._health, tail, errors)
        health["strip"] = self.strip()
        return json_response(200, health)

    async def _api_feed(self, req: Request):
        after = digits(req.query.get("after", ["0"])[0], 18)  # an SQLite integer
        if after is None:
            return text(400, "after: a line id")
        lines, _ = await asyncio.to_thread(self._feed_since, after)
        return json_response(200, {"lines": lines})

    async def _api_voice(self, req: Request):
        msg = req.json()
        if msg.get("type") == "join":
            problem = self._join_problem()
            if problem is not None:
                return json_response(409, {"ok": False, "error": problem})
        error = await self.relay.send(msg)
        if error is not None:
            return json_response(400 if error.startswith("not a") else 409,
                                 {"ok": False, "error": error})
        return json_response(200, {"ok": True})

    def _join_problem(self) -> str | None:
        """Why a room cannot be joined right now, in true words; None to ask the mind."""
        if not self.relay.connected:
            return "The voice service is not running."
        if self.view.state != "running":
            return f"The mind is {self.view.state}, so it cannot seat you in a room."
        if self.relay.mirror.state.get("mind") is not True:
            return "The mind is running but not connected to the voice service yet."
        return None

    async def _api_control(self, req: Request):
        msg = req.json()
        service, action = msg.get("service"), msg.get("action")
        if not isinstance(service, str) or not isinstance(action, str):
            return json_response(400, {"ok": False, "error": "service and action"})
        try:
            self.controls.check(service, action)
        except Refused as e:
            return json_response(e.status, {"ok": False, "error": str(e)})
        if (action in ("stop", "restart") and self.game.state in (status.IN_GAME, status.UNKNOWN)
                and msg.get("confirm") is not True):
            what = "the guild goes quiet" if service == "mind" else "comms go silent"
            if self.game.state == status.IN_GAME:
                ask = f"You're in game. If you {action} the {service}, {what} until it is back. "
            else:
                ask = (f"The console cannot tell whether you're in game ({self.game.reason}). "
                       f"If you are and you {action} the {service}, {what} until it is back. ")
            return json_response(409, {"ok": False,
                                       "confirm": ask + f"{action.capitalize()} it anyway?"})
        self.broadcast("control", {"service": service, "action": action, "phase": "running"})
        self._publish_status()
        try:
            outcome = await self.controls.act(service, action)
        except Refused as e:
            self.broadcast("control", {"service": service, "action": action, "phase": "failed",
                                       "message": str(e)})
            await self._after_control()
            return json_response(e.status, {"ok": False, "error": str(e)})
        self.broadcast("control", {"service": service, "action": action, "phase": "done",
                                   "message": outcome.message})
        await self._after_control()
        return json_response(200, {"ok": True, "message": outcome.message,
                                   "state": outcome.state})

    async def _after_control(self) -> None:
        self._voice_lock = None
        with suppress(Exception):
            await self.refresh(force=True)

    # -- running -----------------------------------------------------------------------------

    async def start(self) -> None:
        from .httpd import serve
        self._server = await serve(self.handle, HOST, self.s.port)
        self.s.port = self._server.sockets[0].getsockname()[1]
        self.origin = f"http://{HOST}:{self.s.port}"
        await self.refresh(force=True)
        self._tasks = [asyncio.create_task(self._status_loop(), name="console-status"),
                       asyncio.create_task(self._feed_loop(), name="console-feed"),
                       asyncio.create_task(self.relay.run(), name="console-voice")]

    @property
    def url(self) -> str:
        return f"{self.origin}/#key={self.s.key}"

    async def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with suppress(BaseException):
                await task
        self._tasks = []
        await self.relay.close()
        for sub in tuple(self._subs):
            sub.dead = True
            with suppress(asyncio.QueueFull):
                sub.queue.put_nowait(b"")
        if self._server is not None:
            self._server.close()
            with suppress(Exception):
                await asyncio.wait_for(self._server.wait_closed(), 2)
            self._server = None
