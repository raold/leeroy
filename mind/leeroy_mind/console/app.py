"""The console's server: the page, its event stream and the endpoints behind its tabs.

Every request must carry exactly one Host header, 127.0.0.1:<port> (against DNS rebinding), an
Origin that is the console's own when it carries one, and a Sec-Fetch-Site of same-origin or none
when it carries one. A tailnet name the console was given (--allow-host, [console] allow_hosts)
is a second address for it, behind `tailscale serve` on this machine (which keeps the browser's
Host): a Host of NAME or NAME:443, whose own Origin is https://NAME. The two addresses' Origins
never cross, and X-Forwarded-Host is ignored, as it always was. Every request but the page's
own load and its static files must also carry the session key (made at each start, or the
stable one in --key-file: keyfile.py), once, in an X-Console-Key header. The printed URL holds
the key in its fragment (/#key=...), which a browser never sends: the page takes it into
memory, drops it from the address bar and sends it with each fetch, the event stream included.
There is no cookie, so no other program on another 127.0.0.1 port is ever handed the key.
Anything else is 403. A mutating request (POST) must also carry the console's Origin and a JSON
body; there is no CORS, so another origin's script cannot read an answer or send a preflighted
request (the key header needs one).
The static files need no key because a browser sends no header with a stylesheet or a module
script; they are the package's own files and carry no data, and the page's policy (CSP) lets it
run nothing else.

    GET  /             the page (static; it carries no key)
    GET  /static/<path> the page's stylesheet and modules (static_files: the package's static/
                       folder, the path looked up exactly as packaged; anything else is 404)
    GET  /api/state    the strip, the voice mirror, the text feed (null when mind.db could not be
                       read for it), the 32 rooms, the guild mood and the control rules
    GET  /api/events   the same as a first `snapshot` event, then `status`, `voice`, `chat`
                       (a repeated id replaces the line), `chat_gone` and `control` events (server-sent events, a comment every 15 s)
    GET  /api/health   the Mind tab: processes, lock, links, Ollama's resident models, GPU,
                       the night, the latest errors and a content-free log tail
    GET  /api/feed     ?after=<id>: text lines after that id
    GET  /api/raid     the Raid tab: mind.db's raids, loot and DKP (raidview.raid_view), and
                       the raid night's live state from the mind's control endpoint
    GET  /api/guild    the Guild tab: roster, DKP ladder, loot history, awards, chronicle
    GET  /api/personas the Personas tab: the cast with each member's mood, stance, ties, memory
                       counts, persona version and drift (personaview.roster_view)
    GET  /api/persona  ?guid=<guid>: one member in detail, its persona card summary, ties,
                       memories by kind and persona ledger (personaview.member_view)
    POST /api/voice    {"type": "join", "channel"} | {"type": "volume", "guid", "db"} |
                       {"type": "mute", "guid", "on"} | {"type": "self", "mute", "deafen"}: on to the
                       voice service
    POST /api/control  {"service": "mind"|"voice", "action": "start"|"restart"|"stop",
                       "confirm": bool}: a stop or restart while the owner is in game, or while
                       nobody can tell, is answered 409 with a question until it is confirmed
    POST /api/raid     {"action": "call"|"summon"|"start"|"stop"|"end"} or {"action": "form",
                       "raid", "size", "heroic", "lead"}: checked as the endpoint checks it,
                       then on to the mind's control endpoint (mindlink); refused with
                       --no-control, while a start or stop runs, or while the mind is not up
    GET  /api/face     ?guid=<guid>&size=sm|md|lg: the member's chosen photo (mind-data/selfies/
                       <guid>/chosen.jpg) as it is, or 404; the browser scales it
    GET  /api/tuning   the Personas tab's dials: world, groups and each member's values, and
                       the change log, from mind.db (tuneview.tuning_view), mind.toml's own
                       world values, and the running mind's tune status
    POST /api/tune     a tune action ({"action": "set"|"undo"|"revert"|"group"|"retire"|
                       "preview", ...}), checked as the endpoint checks it, then on to the
                       mind's tune op, refused as raid actions are; a change is announced as a
                       `tuning` event; a preview's answer goes only to the page that asked
    GET  /api/selfies  the Personas tab's photo queue: the sets waiting for his choice, counts
                       and recent changes from mind.db (selfieview.queue_view), and the
                       running mind's selfie status (its recent changes with their undoability)
    GET  /api/realm    the Realm tab: rival guilds, the race for first kills, poaching, Trade lines, canon
    GET  /api/social   the Social tab: feuds, jealousy, romances, feelings toward him, live
                       rumours and the guild mood, from mind.db (a missing table is listed in
                       `missing`, not an error)
    GET  /api/calendar the Calendar tab: upcoming events with their signups and the raid nights
                       he missed, from mind.db (a missing table is listed in `missing`)
    GET  /api/board    the Board tab: realm forum threads, delivered letters and write-ups from
                       mind.db (a missing table is listed in `missing`; `realm_on` from mind.toml)
    GET  /api/bank     the Bank tab: the mind's bank snapshot (control op `bank`, read-only
                       SELECTs made by the mind) with names, item names from library.db and
                       the realm_bank ledger from mind.db; `why` says when there is no snapshot
    GET  /api/training the Training tab: what `training status`, `gate`, `trials` and `cards`
                       print, from mind.db (`setup` false when it has no training tables)
    GET  /api/dojo     the Dojo tab: the dojo's own files (its layout's data and runtime roots,
                       read-only) and whether its worldserver listens, from the console's TCP table
    GET  /api/selfies/images  ?guid=&set=: a ready set's four thumbnails as data URLs (404
                       unless that set is ready and the member's)
    GET  /api/selfies/member  ?guid=: one member's photo in force, sets and change history
    POST /api/selfie   a selfie action ({"action": "status"|"pick"|"more"|"undo", ...}),
                       checked as the endpoint checks it, then on to the mind's selfie op,
                       refused as raid actions are; a pick, undo or "4 more" is announced as a
                       `selfie` event {"action", "guid"}
    POST /api/say      {"channel": "guild"|"officer"|"group"|"whisper", "text"[, "to"]}: his
                       line, said in game as his: checked and cleaned as the mind checks it (400
                       with its words; a slash command too, which WoW would post as words),
                       refused 403 with --no-control and 409 while nothing can be sent (the mind
                       or its bridge down, a start or stop of the mind under way, not in game,
                       nobody can tell), then on to the mind's say op; nothing is
                       broadcast, the line comes back through the feed when the game reports it

The Raid, Guild and Personas tabs read mind.db read-only. Raid actions, dial changes and his
lines are the mind's to take: the console forwards them and shows the mind's answer, which says
why when it refuses. A preview's text and a line's are never logged, stored or broadcast."""
import asyncio
import hmac
import logging
import os
import time
from contextlib import suppress
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Callable

from .. import endpoint
from . import (bankview, boardview, calendarview, dojoview, faces, feed, mindlog, netinfo,
               personaview, raidview, realmview, selfieview, socialview, status, trainview, tuneview)
from .control import Controls, Refused
from .httpd import Request, Response, Stream, digits, encode_json, json_response, text
from .mindlink import BANK_S, RAID_S, SAY_S, LinkError
from .voicelink import VoiceRelay
from ..owner_say import BRIDGE_DOWN, NOT_IN_GAME
from ..voice.rooms import ROOMS

HOST = "127.0.0.1"
STATUS_S = 2.0
SNAPSHOT_S = 15.0  # a settled mind's process list is taken this often (an unclear one's at most)
VOICE_LOCK_S = 15.0  # how long the Mind tab reuses its reading of voice.lock
FEED_S = 1.5
PENDING_S = 120.0  # how long a spoken line may stay "on its way" before the page is told it is gone
PENDING_MAX = 200  # how many such lines are watched at once
PING_S = 15.0
QUEUE = 500  # events a page may fall behind before it is cut off (it reconnects)
FEED_DAYS = 3.0  # how far back a page's first feed goes
KEY_HEADER = "x-console-key"
TUNE_S = 10.0  # a dial change's answer
SELFIE_S = 10.0  # a photo change's answer; the call waits this plus SELFIE_MARGIN_S
SELFIE_MARGIN_S = 5.0  # a held chosen.jpg retries for about 2 s inside the op
PREVIEW_S = endpoint.PREVIEW_S + 5.0  # a preview's two lines per member, at background priority
MIND_DOWN = ("The mind isn't running, so it can't preview or change dials. Start it from the "
             "Mind tab.")
# The reply box's words (the page's comms/reply.mjs says the same before a line is sent)
SAY_OFF = "Replies are off: this console runs with --no-control."
SAY_OLD_MIND = "The mind needs the update to send replies."
SAY_NO_LINK = "Nothing was sent: the console has no link to the mind's control endpoint."
SAY_SLASH = "Slash commands aren't sent from here."  # the box's words (comms/reply.mjs)
BANK_OLD_MIND = "The running mind predates the Bank tab: restart it after the update."
OLD_OPS = "bad request: op is one of "  # a mind from before the say op names the ops it has

# The page runs only the console's own files: no inline script or style, nothing from elsewhere
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; font-src 'self'; "
       "img-src blob: data:; connect-src 'self'; base-uri 'none'; form-action 'none'; "
       "frame-ancestors 'none'")
STATIC_TYPES = {".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                ".mjs": "text/javascript; charset=utf-8", ".woff2": "font/woff2",
                ".txt": "text/plain; charset=utf-8"}

SELFIE_DOWN = "The mind isn't running, so it can't set photos. Start it from the Mind tab."
SELFIE_OLD_MIND = ("The running mind is older than this console and cannot set photos: restart "
                   "it from the Mind tab.")
SELFIE_WORDS = {
    "photos are not running in this mind": "Photos are off in this mind.",
    "another photo change is still being written":
        "Another change is still being saved; try again in a moment."}

log = logging.getLogger("leeroy_mind.console")


def static_files() -> dict[str, tuple[bytes, str]]:
    """The page's own files: each path under /static/ (js/main.mjs) with its bytes and content
    type, read from the package's static/ folder. A file of a kind not in STATIC_TYPES is left
    out, so it is never served."""
    files: dict[str, tuple[bytes, str]] = {}

    def walk(folder, prefix: str) -> None:
        for entry in folder.iterdir():
            if entry.is_dir():
                walk(entry, f"{prefix}{entry.name}/")
            elif (kind := STATIC_TYPES.get(os.path.splitext(entry.name)[1])) is not None:
                files[prefix + entry.name] = (entry.read_bytes(), kind)
    walk(resources.files(__package__).joinpath("static"), "")
    return files


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
    allow_hosts: tuple[str, ...] = ()  # tailnet names (lower case) served at https://NAME too
    stable_key: bool = False  # the key is --key-file's, not this start's
    dojo_layout: Path | None = None  # the dojo's layout.json (the Dojo tab reads its files only)


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
                 monotonic: Callable = time.monotonic, link=None):
        self.s = settings
        self.mind = mind
        self.controls = controls
        self.voice = voice
        self.link = link  # a mindlink.MindLink: the mind's control endpoint; None: none here
        self.relay = VoiceRelay(lambda: self.s.voice_ui_port, self._voice_event,
                                **({"connect": connect} if connect is not None else {}))
        self._tcp, self._ollama, self._gpu = tcp, ollama, gpu
        self.follower = follower or mindlog.LogFollower(self.s.data_dir / "logs" / "mind.log")
        self._clock, self._monotonic = clock, monotonic
        self.origin = f"http://{HOST}:{settings.port}"
        self._page = resources.files(__package__).joinpath("page.html").read_bytes()
        self._static = static_files()
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
        self._pending: dict[int, float] = {}  # sent to a page as heard False: id -> first seen
        self._waiting: dict[int, float] = {}  # spoken, voice row not written yet: id -> first seen
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
            now = self._clock()
            since = now - days * 86400 if days else None
            return feed.feed(conn, after=after, upto=upto, since=since, now=now), upto
        return feed.read(db, question)

    def _poll(self, after: int, ids: list[int], days: float | None = None):
        """Worker thread. Feed, waiting set and recheck of `ids` (what is on its way, listed on
        the loop: _pending and _waiting are the loop's), from one read snapshot."""
        now = self._clock()
        since = now - days * 86400 if days else None
        return feed.read(self.s.data_dir / "mind.db", feed.poll, after=after, since=since,
                         now=now, ids=ids)

    def _on_way(self) -> list[int]:
        return list(self._pending) + list(self._waiting)

    def _track(self, lines: list[dict]) -> None:
        """Lines a page was given as still on their way are rechecked until they settle."""
        now = self._monotonic()
        for line in lines:
            if line["heard"] is False:
                self._pending.setdefault(line["id"], now)
                self._waiting.pop(line["id"], None)

    def _settle(self, late: list[int], seen: list[dict], lines: list[dict],
                out: dict[int, dict]) -> list[int]:
        """Apply a recheck: lines now heard go into `out`; the ids returned are gone, aged out
        or pushed past the limit."""
        now = self._monotonic()
        for i in late:
            if i not in self._pending:
                self._waiting.setdefault(i, now)
        by_id = {line["id"]: line for line in seen + lines}
        gone = []
        for i in list(self._pending) + list(self._waiting):
            line = by_id.get(i)
            if line is None:
                if i in self._pending:
                    del self._pending[i]
                    gone.append(i)
            elif line["heard"] is not False:
                out[i] = line
                self._pending.pop(i, None)
                self._waiting.pop(i, None)
            elif i in self._waiting:
                out[i] = line
                del self._waiting[i]
                self._pending[i] = now
        for table in (self._pending, self._waiting):
            for i, since in list(table.items()):
                if now - since >= PENDING_S:
                    del table[i]
                    if table is self._pending:
                        gone.append(i)
        while len(self._pending) > PENDING_MAX:
            gone.append(next(iter(self._pending)))
            del self._pending[gone[-1]]
        while len(self._waiting) > PENDING_MAX:
            del self._waiting[next(iter(self._waiting))]
        return [i for i in dict.fromkeys(gone) if i not in out]

    async def _feed_loop(self) -> None:
        try:
            _, self._feed_last = await asyncio.to_thread(self._feed_since, 1 << 62)
        except Exception:
            self._feed_last = 0
        while True:
            await asyncio.sleep(FEED_S)
            if not self._subs:
                continue
            await self._feed_pass()

    async def _feed_pass(self) -> None:
        try:
            lines, upto, late, seen = await asyncio.to_thread(self._poll, self._feed_last,
                                                              self._on_way())
            out = {line["id"]: line for line in lines}
            self._track(lines)
            gone = self._settle(late, seen, lines, out)
        except Exception as e:
            log.warning("console_feed_unread: error=%s", type(e).__name__)
            return
        self._feed_last = max(self._feed_last, upto)
        if out:
            self.broadcast("chat", {"lines": sorted(out.values(), key=lambda x: x["id"])})
        if gone:
            self.broadcast("chat_gone", {"ids": gone})

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
            lines = None  # not [], which would tell the page the feed is empty
        return {"status": strip, "voice": voice, "chat": lines, "rooms": self._rooms(),
                "mood": await self._mood()}

    @staticmethod
    def _rooms() -> list[dict]:
        return [{"id": r.id, "name": r.name, "parent": r.parent, "capacity": r.capacity}
                for r in ROOMS]

    async def _mood(self) -> dict | None:
        try:
            return await asyncio.to_thread(feed.read, self.s.data_dir / "mind.db", feed.guild_mood)
        except Exception as e:
            log.warning("console_mood_unread: error=%s", type(e).__name__)
            return None

    async def _events(self, sub: _Subscriber, voice, strip):
        """`voice` and `strip` were taken as the page registered, with no await between: every
        later voice event is in its queue and none before it is."""
        try:
            lines, _, late, _ = await asyncio.to_thread(self._poll, 0, self._on_way(), FEED_DAYS)
            self._track(lines)
            now = self._monotonic()
            for i in late:
                if i not in self._pending:
                    self._waiting.setdefault(i, now)
        except Exception as e:
            log.warning("console_feed_unread: error=%s", type(e).__name__)
            lines = None
        yield sse("snapshot", {"status": strip, "voice": voice, "chat": lines,
                               "rooms": self._rooms(), "mood": await self._mood()})
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

    def _site(self, req: Request) -> str | None:
        """The origin the request was sent to: the console's own, https://NAME for an allowed
        tailnet name (a Host of NAME or NAME:443), or None for any other Host."""
        hosts = req.all("host")
        if len(hosts) != 1:
            return None
        if hosts[0] == f"{HOST}:{self.s.port}":
            return self.origin
        name = hosts[0].lower()
        if name.endswith(":443"):
            name = name[:-4]
        return f"https://{name}" if name in self.s.allow_hosts else None

    def gate(self, req: Request) -> Response | None:
        """None when the request may go on; the refusal otherwise."""
        site = self._site(req)
        if site is None:
            where = [f"{self.origin}/"] + [f"https://{n}/" for n in self.s.allow_hosts]
            return text(403, f"Forbidden. The console is served at {' and '.join(where)} only.")
        origins = req.all("origin")
        if len(origins) > 1 or (origins and origins[0] != site):
            return text(403, "Forbidden: another origin.")
        sites = req.all("sec-fetch-site")
        if len(sites) > 1 or (sites and sites[0] not in ("same-origin", "none")):
            return text(403, "Forbidden: not from the console's own page.")
        if req.method == "POST":
            if origins != [site]:
                return text(403, "Forbidden: a change needs the console's own Origin.")
            kind = (req.one("content-type") or "").split(";")[0].strip().lower()
            if kind != "application/json":
                return text(415, "Send JSON.")
        elif req.method != "GET":
            return text(405, "Method not allowed.", [("Allow", "GET, POST")])
        if req.method == "GET" and (req.path == "/" or req.path.startswith("/static/")):
            return None  # the page and its files: static, and its key stays in the browser
        if self._key_ok(req.one(KEY_HEADER)):
            return None
        if self.s.stable_key:
            return text(403, "Forbidden: no console key. Open the console's URL with its key "
                             "(your bookmark, or the URL it printed when it started).")
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
        if req.path.startswith("/static/"):
            if req.method != "GET":
                return text(405, "Method not allowed.", [("Allow", "GET")])
            return self._static_response(req.path[len("/static/"):])
        routes = {("GET", "/api/state"): self._api_state,
                  ("GET", "/api/events"): self._api_events,
                  ("GET", "/api/health"): self._api_health,
                  ("GET", "/api/feed"): self._api_feed,
                  ("GET", "/api/raid"): self._api_raid,
                  ("GET", "/api/guild"): self._api_guild,
                  ("GET", "/api/social"): self._api_social,
                  ("GET", "/api/realm"): self._api_realm,
                  ("GET", "/api/calendar"): self._api_calendar,
                  ("GET", "/api/board"): self._api_board,
                  ("GET", "/api/bank"): self._api_bank,
                  ("GET", "/api/training"): self._api_training,
                  ("GET", "/api/dojo"): self._api_dojo,
                  ("GET", "/api/personas"): self._api_personas,
                  ("GET", "/api/persona"): self._api_persona,
                  ("GET", "/api/face"): self._api_face,
                  ("GET", "/api/tuning"): self._api_tuning,
                  ("POST", "/api/tune"): self._api_tune,
                  ("GET", "/api/selfies"): self._api_selfies,
                  ("GET", "/api/selfies/images"): self._api_selfie_images,
                  ("GET", "/api/selfies/member"): self._api_selfie_member,
                  ("POST", "/api/selfie"): self._api_selfie,
                  ("POST", "/api/voice"): self._api_voice,
                  ("POST", "/api/control"): self._api_control,
                  ("POST", "/api/raid"): self._api_raid_action,
                  ("POST", "/api/say"): self._api_say}
        route = routes.get((req.method, req.path))
        if route is None:
            known = {path for _, path in routes}
            return text(405 if req.path in known else 404,
                        "Method not allowed." if req.path in known else "Not found.")
        return await route(req)

    def _page_response(self) -> Response:
        return Response(200, self._page, "text/html; charset=utf-8", [
            ("Content-Security-Policy", CSP), ("X-Frame-Options", "DENY"),
            ("Referrer-Policy", "same-origin"), ("Cross-Origin-Opener-Policy", "same-origin"),
            ("Cross-Origin-Resource-Policy", "same-origin")])

    def _static_response(self, name: str) -> Response:
        found = self._static.get(name)  # exactly as packaged: nothing is normalised or joined
        if found is None:
            return text(404, "Not found.")
        body, kind = found
        return Response(200, body, kind, [("Cross-Origin-Resource-Policy", "same-origin")])

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
        health["endpoint"] = await asyncio.to_thread(self._endpoint_found)
        health["strip"] = self.strip()
        return json_response(200, health)

    # -- the mind's control endpoint ---------------------------------------------------------

    def _mind_pids(self) -> list[int]:
        return [p.pid for p in self._found or []] if self.view.state == "running" else []

    def _endpoint_found(self) -> dict:
        """Worker thread. Whether a stop can be clean: the endpoint is located and checked,
        and nothing is sent to it."""
        if self.link is None:
            return {"ok": False, "detail": "no control endpoint here"}
        if self.view.state != "running":
            return {"ok": False, "detail": f"the mind is {self.view.state}"}
        try:
            self.link.locate(self._mind_pids())
        except LinkError as e:
            return {"ok": False, "detail": str(e)}
        return {"ok": True, "detail": ""}

    async def _mind_status(self) -> dict:
        """The raid night's state as the mind tells it (a read: it changes nothing)."""
        if self.view.state != "running":
            return {"ok": False, "error": f"the mind is {self.view.state}"}
        if self.link is None:
            return {"ok": False, "error": "no control endpoint here"}
        try:
            reply = await asyncio.to_thread(self.link.call, self._mind_pids(), "status")
        except LinkError as e:
            return {"ok": False, "error": str(e)}
        if reply.get("ok") is not True:
            return {"ok": False, "error": str(reply.get("error") or "refused")[:200]}
        raid = reply.get("raid")
        return {"ok": True, "stopping": reply.get("stopping") is True,
                "raid": raid if isinstance(raid, dict) else None}

    # -- the raid and guild tabs ---------------------------------------------------------------

    async def _api_raid(self, req: Request):
        try:
            view = await asyncio.to_thread(feed.read, self.s.data_dir / "mind.db",
                                           raidview.raid_view, now=self._clock())
        except Exception as e:
            log.warning("console_raid_unread: error=%s", type(e).__name__)
            view = None
        return json_response(200, {"view": view, "mind": await self._mind_status(),
                                   "control": self.controls.describe()})

    async def _api_guild(self, req: Request):
        try:
            view = await asyncio.to_thread(feed.read, self.s.data_dir / "mind.db",
                                           raidview.guild_view)
        except Exception as e:
            log.warning("console_guild_unread: error=%s", type(e).__name__)
            return json_response(503, {"ok": False, "error": f"mind.db could not be read "
                                                             f"({type(e).__name__})"})
        return json_response(200, view)

    async def _db_view(self, label: str, question, *args, **kw) -> Response:
        """question(conn, ...) on mind.db read-only as a 200; any read failure is a 503 that
        names the exception class only, which is also all that is logged."""
        try:
            view = await asyncio.to_thread(feed.read, self.s.data_dir / "mind.db", question,
                                           *args, **kw)
        except Exception as e:
            log.warning("console_%s_unread: error=%s", label, type(e).__name__)
            return json_response(503, {"ok": False, "error": f"mind.db could not be read "
                                                             f"({type(e).__name__})"})
        return json_response(200, view)

    async def _api_social(self, req: Request):
        return await self._db_view("social", socialview.social_view, now=self._clock())

    async def _api_realm(self, req: Request):
        return await self._db_view("realm", realmview.realm_view, now=self._clock())

    async def _api_calendar(self, req: Request):
        return await self._db_view("calendar", calendarview.calendar_view, now=self._clock())

    async def _api_board(self, req: Request):
        return await self._db_view("board", boardview.board_view, now=self._clock(),
                                   data_dir=self.s.data_dir)

    async def _bank_snapshot(self) -> tuple[dict | None, str | None]:
        """(the mind's bank, None) or (None, why): the bank is read through the mind."""
        if self.view.state != "running":
            return None, f"The bank is read through the mind, which is {self.view.state}."
        if self.link is None:
            return None, "The bank is read through the mind, and the console has no link to its control endpoint."
        try:
            reply = await asyncio.to_thread(self.link.call, self._mind_pids(), "bank",
                                            timeout=BANK_S)
        except LinkError as e:
            words = str(e)
            return None, words[:1].upper() + words[1:] + "."
        if reply.get("ok") is not True:
            error = str(reply.get("error") or "")
            if error.startswith(OLD_OPS):
                return None, BANK_OLD_MIND
            return None, error[:300] or "The mind refused it."
        snapshot = bankview.clean_snapshot(reply)
        if snapshot is None:
            return None, "The mind's bank answer could not be read."
        return snapshot, None

    async def _api_bank(self, req: Request):
        snapshot, why = await self._bank_snapshot()
        return await self._db_view("bank", bankview.bank_view, snapshot=snapshot, why=why,
                                   data_dir=self.s.data_dir)
    async def _api_training(self, req: Request):
        return await self._db_view("training", trainview.training_view, now=self._clock())

    async def _api_dojo(self, req: Request):
        view = await asyncio.to_thread(dojoview.dojo_view, self.s.dojo_layout, conns=self._conns)
        return json_response(200, view)

    # -- the personas tab (read-only) ----------------------------------------------------------

    async def _api_personas(self, req: Request):
        try:
            view = await asyncio.to_thread(feed.read, self.s.data_dir / "mind.db",
                                           personaview.roster_view, now=self._clock())
        except Exception as e:
            log.warning("console_personas_unread: error=%s", type(e).__name__)
            return json_response(503, {"ok": False, "error": f"mind.db could not be read "
                                                             f"({type(e).__name__})"})
        return json_response(200, view)

    async def _api_persona(self, req: Request):
        values = req.query.get("guid", [])
        guid = digits(values[0], 10) if len(values) == 1 else None
        if guid is None:
            return json_response(400, {"ok": False, "error": "guid: one member's guid"})
        try:
            view = await asyncio.to_thread(feed.read, self.s.data_dir / "mind.db",
                                           personaview.member_view, guid, now=self._clock())
        except Exception as e:
            log.warning("console_persona_unread: error=%s", type(e).__name__)
            return json_response(503, {"ok": False, "error": f"mind.db could not be read "
                                                             f"({type(e).__name__})"})
        if view is None:
            return json_response(404, {"ok": False, "error": f"mind.db has no member {guid}"})
        return json_response(200, view)

    async def _api_face(self, req: Request):
        guids, sizes = req.query.get("guid", []), req.query.get("size", [])
        guid = digits(guids[0], 10) if len(guids) == 1 else None
        if guid is None or not 0 < guid < 1 << 32 or len(sizes) != 1 or sizes[0] not in faces.SIZES:
            return json_response(400, {"ok": False, "error": "guid: one member's guid; size: sm, md or lg"})
        data = await asyncio.to_thread(faces.read_face, self.s.data_dir, guid)
        if data is None:
            return text(404, "no photo")
        return Response(200, data, "image/jpeg", [("Cache-Control", "private, max-age=60"),
                                                  ("Cross-Origin-Resource-Policy", "same-origin")])

    # -- the personas tab's dials ---------------------------------------------------------------

    def _file_values(self) -> dict | None:
        """mind.toml's own world values (read-only), or None when it cannot be read."""
        from ..config import ConfigError, load_config
        from ..tuning.world import file_values
        try:
            return file_values(load_config(self.s.data_dir / "mind.toml"))
        except ConfigError:
            return None

    async def _tune_status(self) -> dict:
        if self.view.state != "running":
            return {"ok": False, "error": MIND_DOWN}
        if self.link is None:
            return {"ok": False, "error": "no control endpoint here"}
        try:
            reply = await asyncio.to_thread(self.link.call, self._mind_pids(), "tune",
                                            {"action": "status"})
        except LinkError as e:
            return {"ok": False, "error": str(e)}
        if reply.get("ok") is not True:
            return {"ok": False, "error": str(reply.get("error") or "refused")[:200]}
        return {"ok": True, "version": reply.get("version"),
                "pinned_members": reply.get("pinned_members")}

    async def _api_tuning(self, req: Request):
        files = await asyncio.to_thread(self._file_values)
        try:
            view = await asyncio.to_thread(feed.read, self.s.data_dir / "mind.db",
                                           tuneview.tuning_view, file_values=files,
                                           now=self._clock())
        except Exception as e:
            log.warning("console_tuning_unread: error=%s", type(e).__name__)
            return json_response(503, {"ok": False, "error": f"mind.db could not be read "
                                                             f"({type(e).__name__})"})
        return json_response(200, {"view": view, "mind": await self._tune_status(),
                                   "control": self.controls.describe()})

    async def _api_tune(self, req: Request):
        try:
            args = endpoint.tune_args(req.json())  # exactly as the mind will check it
        except endpoint.BadRequest as e:
            return json_response(400, {"ok": False, "error": str(e)})
        problem = self._raid_problem()
        if problem is not None:
            status_code, words = problem
            if self.view.state != "running" and status_code == 409 and self.controls.busy is None:
                words = MIND_DOWN
            return json_response(status_code, {"ok": False, "error": words})
        preview = args["action"] == "preview"
        try:
            reply = await asyncio.to_thread(self.link.call, self._mind_pids(), "tune", args,
                                            timeout=PREVIEW_S if preview else TUNE_S)
        except LinkError as e:
            return json_response(409, {"ok": False, "error": str(e)})
        if reply.get("ok") is not True:
            return json_response(409, {"ok": False,
                                       "error": str(reply.get("error") or "refused")[:300]})
        if not preview and args["action"] != "status" and reply.get("change"):
            self.broadcast("tuning", {"change": reply.get("change"),
                                      "version": reply.get("version")})
        return json_response(200, reply)  # a preview's text: to this page only, never logged

    # -- the personas tab's profile photos ---------------------------------------------------

    @staticmethod
    def _selfie_words(reply: dict) -> str:
        said = str(reply.get("error") or "refused")[:300]
        if said.startswith(OLD_OPS):  # a mind from before the selfie op names the ops it has
            return SELFIE_OLD_MIND
        return SELFIE_WORDS.get(said, said)

    async def _selfie_status(self) -> dict:
        if self.view.state != "running":
            return {"ok": False, "error": SELFIE_DOWN}
        if self.link is None:
            return {"ok": False, "error": "no control endpoint here"}
        try:
            reply = await asyncio.to_thread(self.link.call, self._mind_pids(), "selfie",
                                            {"action": "status"})
        except LinkError as e:
            return {"ok": False, "error": str(e)}
        if reply.get("ok") is not True:
            return {"ok": False, "error": self._selfie_words(reply)}
        recent = reply.get("recent")
        return {"ok": True, "enabled": reply.get("enabled") is True,
                "recent": recent if isinstance(recent, list) else []}

    async def _read_selfies(self, name: str, question, *args, **kw):
        """question on mind.db read-only; (view, None), or (None, a 503 response)."""
        try:
            return await asyncio.to_thread(feed.read, self.s.data_dir / "mind.db", question,
                                           self.s.data_dir, *args, **kw), None
        except Exception as e:
            log.warning("console_%s_unread: error=%s", name, type(e).__name__)
            return None, json_response(503, {"ok": False, "error": f"mind.db could not be read "
                                                                    f"({type(e).__name__})"})

    async def _api_selfies(self, req: Request):
        view, failed = await self._read_selfies("selfies", selfieview.queue_view,
                                                now=self._clock())
        if failed is not None:
            return failed
        return json_response(200, {"view": view, "mind": await self._selfie_status(),
                                   "control": self.controls.describe()})

    async def _api_selfie_images(self, req: Request):
        g, n = req.query.get("guid", []), req.query.get("set", [])
        guid = digits(g[0], 10) if len(g) == 1 else None
        set_no = digits(n[0], 10) if len(n) == 1 else None
        if guid is None or set_no is None:
            return json_response(400, {"ok": False, "error": "guid and set: whole numbers"})
        view, failed = await self._read_selfies("selfie_images", selfieview.set_images, guid,
                                                set_no)
        if failed is not None:
            return failed
        if view is None:
            return json_response(404, {"ok": False, "error": "no such ready set for that member"})
        return json_response(200, view)

    async def _api_selfie_member(self, req: Request):
        values = req.query.get("guid", [])
        guid = digits(values[0], 10) if len(values) == 1 else None
        if guid is None:
            return json_response(400, {"ok": False, "error": "guid: one member's guid"})
        view, failed = await self._read_selfies("selfie_member", selfieview.member_photos, guid)
        if failed is not None:
            return failed
        if view is None:
            return json_response(404, {"ok": False, "error": f"no photos for member {guid}"})
        return json_response(200, view)

    async def _api_selfie(self, req: Request):
        try:
            args = endpoint.selfie_args(req.json())  # exactly as the mind will check it
        except endpoint.BadRequest as e:
            return json_response(400, {"ok": False, "error": str(e)})
        problem = self._raid_problem()
        if problem is not None:
            status_code, words = problem
            if self.view.state != "running" and status_code == 409 and self.controls.busy is None:
                words = SELFIE_DOWN
            return json_response(status_code, {"ok": False, "error": words})
        try:
            reply = await asyncio.to_thread(self.link.call, self._mind_pids(), "selfie", args,
                                            timeout=SELFIE_S + SELFIE_MARGIN_S)
        except LinkError as e:
            return json_response(409, {"ok": False, "error": str(e)})
        if reply.get("ok") is not True:
            return json_response(409, {"ok": False, "error": self._selfie_words(reply)})
        if args["action"] != "status":
            self.broadcast("selfie", {"action": args["action"], "guid": reply.get("guid")})
        return json_response(200, reply)

    def _raid_problem(self) -> tuple[int, str] | None:
        if not self.controls.enabled:
            return 403, self.controls.reason or "control is off"
        if self.controls.busy is not None:
            service, action = self.controls.busy
            return 409, f"a {action} of the {service} is still running"
        if self.view.state != "running":
            return 409, f"the mind is {self.view.state}: raid actions need it running"
        if self.link is None:
            return 409, "no control endpoint here"
        return None

    async def _api_raid_action(self, req: Request):
        try:
            args = endpoint.raid_args(req.json())  # exactly as the mind will check it
        except endpoint.BadRequest as e:
            return json_response(400, {"ok": False, "error": str(e)})
        problem = self._raid_problem()
        if problem is not None:
            return json_response(problem[0], {"ok": False, "error": problem[1]})
        try:
            reply = await asyncio.to_thread(self.link.call, self._mind_pids(), "raid", args,
                                            timeout=RAID_S)
        except LinkError as e:
            return json_response(409, {"ok": False, "error": str(e)})
        if reply.get("ok") is not True:
            return json_response(409, {"ok": False,
                                       "error": str(reply.get("error") or "refused")[:300]})
        message = str(reply.get("message") or "done")[:300]
        self.broadcast("raid", {"action": args["action"], "message": message})
        return json_response(200, {"ok": True, "message": message})

    # -- the reply box -------------------------------------------------------------------------

    def _say_problem(self) -> tuple[int, str] | None:
        """Why his line cannot be sent now, in the reply box's words; None: on to the mind. The
        raid actions' checks (but a start or stop of the voice service, which a typed line
        never goes near, holds nothing), then the game as the console reads it: a mind or bridge
        that is down, or a game state nobody can read, is named as what it is, never as his
        absence."""
        if not self.controls.enabled:
            return 403, SAY_OFF
        if self.controls.busy is not None and self.controls.busy[0] == "mind":
            action = self.controls.busy[1]
            return 409, f"A {action} of the mind is under way, so nothing can be sent."
        if self.view.state != "running":
            return 409, ("The mind's state is unclear, so nothing can be sent."
                         if self.view.state == "unknown"
                         else f"The mind is {self.view.state}, so nothing can be sent.")
        if self.link is None:
            return 409, SAY_NO_LINK
        if self.bridge is False:
            return 409, BRIDGE_DOWN
        if self.game.state == status.NOT_IN_GAME:
            return 409, NOT_IN_GAME
        if self.game.state != status.IN_GAME:
            return 409, f"The console can't tell whether you're in game ({self.game.reason})."
        return None

    async def _api_say(self, req: Request):
        try:
            args = endpoint.say_args(req.json())  # checked and cleaned exactly as the mind will
        except endpoint.BadRequest as e:
            return json_response(400, {"ok": False, "error": str(e)})
        if args["text"].startswith("/"):  # cleaned: no space, "|" or control character before it
            return json_response(400, {"ok": False, "error": SAY_SLASH})
        problem = self._say_problem()
        if problem is not None:
            return json_response(problem[0], {"ok": False, "error": problem[1]})
        try:
            # The cleaned text is at most 255 bytes, so the request stays under the endpoint's
            # MAX_LINE however much of it is escaped
            reply = await asyncio.to_thread(self.link.call, self._mind_pids(), "say", args,
                                            timeout=SAY_S)
        except LinkError as e:
            words = str(e)
            if e.unanswered:  # it may have reached the mind, and the game
                return json_response(409, {"ok": False, "error": words[:1].upper() + words[1:]
                                           + ". Check WoW before sending it again."})
            return json_response(409, {"ok": False, "error": f"Nothing was sent: {words}."})
        if reply.get("ok") is not True:
            error = str(reply.get("error") or "")
            if error.startswith(OLD_OPS):
                error = SAY_OLD_MIND
            return json_response(409, {"ok": False,
                                       "error": error[:300] or "The mind refused it."})
        # Nothing is broadcast: the line comes back through the feed when the game reports it
        return json_response(200, {"ok": True,
                                   "message": str(reply.get("message") or "sent")[:300]})

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

    @property
    def tailnet_urls(self) -> list[str]:
        return [f"https://{name}/#key={self.s.key}" for name in self.s.allow_hosts]

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
