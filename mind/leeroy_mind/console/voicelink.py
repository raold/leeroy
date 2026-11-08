"""The console's link to the voice service: it opens the service's own page socket
(ws://127.0.0.1:<ui port>/ws, protocol in voice/leeroy_voice/ui/server.py) as the old page did,
keeps a mirror of the page state, and hands every event on to the console's pages.

The voice page refuses any Origin but its own, and a page served by the console has another, so
a browser cannot open that socket from here: the console, a local program, connects instead and
names the page's own origin. The console's pages reach it only through the console's gated
endpoints, and only with the four page messages (join, volume, mute, self), checked as the voice
service checks them."""
import asyncio
import json
import logging
import math
import re
import random
from typing import Callable

from .ws import WsClient, WsError

HOST = "127.0.0.1"
PATH = "/ws"
RETRY_S = 2.0
TRANSCRIPT = 200  # what a voice state keeps
PEOPLE = 512
INVITES = 32
MAX_CHANNEL = 64
MAX_GUID = 0xFFFFFFFF
DB_LIMIT = 12.0
_IDENT = re.compile(r"[a-z_]{1,32}\Z")  # a message type fit to log

log = logging.getLogger("leeroy_mind.console")

EMPTY = {"tree": [], "where": {}, "names": {}, "room": "", "human": 0, "speaking": [],
         "ptt": False, "volumes": {}, "mutes": [], "invites": [], "transcript": [],
         "mind": False, "status": {}, "self": {"mute": False, "deafen": False}}


def _channel(v) -> str | None:
    return v if isinstance(v, str) and 0 < len(v) <= MAX_CHANNEL and v.isprintable() else None


def _guid(v) -> int | None:
    return v if type(v) is int and 0 < v <= MAX_GUID else None


def _db(v) -> float | None:
    if type(v) is int or (type(v) is float and math.isfinite(v)):
        return float(max(-DB_LIMIT, min(DB_LIMIT, v)))
    return None


def page_message(msg: dict) -> dict | None:
    """The message to send for one of the page's four, its arguments checked; None when it is
    not one of them with good arguments."""
    kind = msg.get("type")
    if kind == "join":
        channel = _channel(msg.get("channel"))
        return None if channel is None else {"type": "join", "channel": channel}
    if kind == "volume":
        guid, db = _guid(msg.get("guid")), _db(msg.get("db"))
        return None if guid is None or db is None else {"type": "volume", "guid": guid, "db": db}
    if kind == "mute":
        guid, on = _guid(msg.get("guid")), msg.get("on")
        return None if guid is None or type(on) is not bool else {"type": "mute", "guid": guid,
                                                                  "on": on}
    if kind == "self":
        mute, deafen = msg.get("mute"), msg.get("deafen")
        if type(mute) is not bool or type(deafen) is not bool:
            return None
        return {"type": "self", "mute": mute, "deafen": deafen}
    return None


class Mirror:
    """The voice page's state, kept as the page keeps it: a `state` replaces it and each event
    updates it, so a page that opens later starts from the same place."""

    def __init__(self):
        self.state: dict = dict(EMPTY)

    def snapshot(self) -> dict:
        return json.loads(json.dumps(self.state))

    def apply(self, msg: dict) -> None:
        s, kind = self.state, msg.get("type")
        if kind == "state":
            self.state = {**EMPTY, **{k: v for k, v in msg.items() if k != "type"}}
            t = self.state.get("transcript")
            if isinstance(t, list):
                self.state["transcript"] = t[-TRANSCRIPT:]
        elif kind == "speaking":
            on = set(s.get("speaking") or [])
            (on.add if msg.get("on") is True else on.discard)(msg.get("guid"))
            s["speaking"] = sorted(g for g in on if isinstance(g, int))
        elif kind == "ptt":
            s["ptt"] = msg.get("down") is True
        elif kind == "moved":
            self._moved(msg)
        elif kind == "invite":
            channel = msg.get("channel")
            invites = list(s.get("invites") or [])
            if isinstance(channel, str) and channel and not any(
                    i.get("from") == msg.get("from") and i.get("channel") == channel
                    for i in invites):
                invites.append({"from": msg.get("from"), "channel": channel})
            s["invites"] = invites
        elif kind == "refused":
            s["invites"] = [i for i in s.get("invites") or []
                            if i.get("channel") != msg.get("channel")]
        elif kind == "transcript":
            t = list(s.get("transcript") or [])
            t.append({k: msg.get(k) for k in ("who", "text", "ts", "kind")})
            s["transcript"] = t[-TRANSCRIPT:]
        elif kind == "status":
            s["status"] = {k: v for k, v in msg.items() if k != "type"}
        elif kind == "mind":
            s["mind"] = msg.get("up") is True
        elif kind == "self":
            s["self"] = {"mute": msg.get("mute") is True, "deafen": msg.get("deafen") is True}
        elif kind == "volume":  # the console's own message, echoed into the mirror
            volumes = dict(s.get("volumes") or {})
            key = str(msg["guid"])
            volumes.pop(key, None)
            volumes.pop(msg["guid"], None)
            if msg["db"]:
                volumes[key] = msg["db"]
            s["volumes"] = volumes
        elif kind == "mute":
            mutes = set(s.get("mutes") or [])
            (mutes.add if msg["on"] else mutes.discard)(msg["guid"])
            s["mutes"] = sorted(mutes)
        self._bound()

    def _bound(self) -> None:
        """Long-lived mirrors retain bounded current state, including departed members."""
        s = self.state
        for key in ("where", "names", "volumes"):
            if isinstance(s.get(key), dict):
                s[key] = dict(list(s[key].items())[-PEOPLE:])
        for key, limit in (("tree", 64), ("speaking", PEOPLE), ("mutes", PEOPLE),
                           ("invites", INVITES), ("transcript", TRANSCRIPT)):
            if isinstance(s.get(key), list):
                s[key] = s[key][-limit:]

    def _moved(self, msg: dict) -> None:
        s = self.state
        guid, room = msg.get("guid"), msg.get("channel") if isinstance(msg.get("channel"),
                                                                        str) else ""
        where = dict(s.get("where") or {})
        before = where.get(str(guid))
        if room:
            where[str(guid)] = room
        else:
            where.pop(str(guid), None)
        s["where"] = where
        invites = list(s.get("invites") or [])
        if s.get("human") and guid == s.get("human"):
            s["room"] = room
            invites = [i for i in invites if i.get("channel") != room]
        s["invites"] = [i for i in invites if not (
            i.get("from") == guid and before == i.get("channel") and room != i.get("channel"))]


class VoiceRelay:
    """Connects, mirrors and forwards until close(). `port()` names the voice page's port (it may
    change when the voice config is read again); `on_event(msg)` gets each event from the
    service, and `{"type": "link", "up": bool}` when the connection opens or closes."""

    def __init__(self, port: Callable[[], int | None], on_event: Callable[[dict], None], *,
                 connect=WsClient.connect, retry_s: float = RETRY_S, jitter=random.random):
        self._port = port
        self._on_event = on_event
        self._connect = connect
        self._retry_s = retry_s
        self._jitter = jitter
        self._attempt = 0
        self._closed = asyncio.Event()
        self._ws: WsClient | None = None
        self._closing = False
        self.mirror = Mirror()
        self.connected = False

    def snapshot(self) -> dict | None:
        return self.mirror.snapshot() if self.connected else None

    async def run(self) -> None:
        while not self._closing:
            port = self._port()
            if port:
                try:
                    ws = await self._connect(HOST, port, PATH, origin=f"http://{HOST}:{port}")
                except WsError:
                    ws = None
                except Exception as e:  # never the end of the relay: it tries again
                    log.error("console_voice_connect_failed: error=%s", type(e).__name__)
                    ws = None
                if ws is not None:
                    try:
                        await self._serve(ws)
                    except Exception as e:
                        log.error("console_voice_link_failed: error=%s", type(e).__name__)
            if not self._closing:
                delay = min(30.0, self._retry_s * 2 ** min(20, self._attempt))
                self._attempt += 1
                try:
                    await asyncio.wait_for(self._closed.wait(), delay * (0.5 + 0.5 * self._jitter()))
                except asyncio.TimeoutError:
                    pass

    async def _serve(self, ws: WsClient) -> None:
        self._ws = ws
        first = True
        try:
            while True:
                try:
                    raw = await ws.recv()
                except WsError:
                    return
                try:
                    msg = json.loads(raw)
                except (ValueError, RecursionError):
                    continue
                if not isinstance(msg, dict) or not isinstance(msg.get("type"), str):
                    continue
                if first and msg["type"] != "state":
                    continue  # the service's first message is the state
                try:
                    self.mirror.apply(msg)
                except Exception as e:  # a message of a shape the mirror does not know
                    log.warning("console_voice_message_skipped: type=%s error=%s",
                                msg["type"] if _IDENT.match(msg["type"]) else "other",
                                type(e).__name__)
                    continue
                if first:
                    first = False
                    self._attempt = 0
                    self.connected = True
                    self._emit({"type": "link", "up": True})
                self._emit(msg)
        finally:
            self._ws = None
            was = self.connected
            self.connected = False
            self.mirror = Mirror()
            await ws.close()
            if was:
                self._emit({"type": "link", "up": False})

    def _emit(self, msg: dict) -> None:
        try:
            self._on_event(msg)
        except Exception as e:
            log.error("console_voice_event_failed: error=%s", type(e).__name__)

    async def send(self, msg: dict) -> str | None:
        """Send one page message; the reason when it cannot go (None: sent)."""
        checked = page_message(msg)
        if checked is None:
            return "not a join, volume, mute or self with good arguments"
        ws = self._ws
        if ws is None or not self.connected:
            return "the voice service is not running"
        try:
            await ws.send(json.dumps(checked, ensure_ascii=True, separators=(",", ":")))
        except WsError:
            return "the voice service connection closed"
        if checked["type"] in ("volume", "mute"):
            self.mirror.apply(checked)
        return None

    async def close(self) -> None:
        self._closing = True
        self._closed.set()
        ws = self._ws
        if ws is not None:
            await ws.close()
