"""The sandbox mind at run time: a real MindApp on DIR/mind-data behind a fake bridge, and what
the HTTP API asks of it (say as the owner, log in and out, form a group, reset, list members).

What keeps it to itself:
- SandboxBridge (the test suite's FakeBridge) greets it with the copy's cast and acks only speech
  (say, emote, ping, get_cast, afk); every other command is refused with "sandbox".
- SandboxLLM is the model client: the real one, but chat only with the live model and embed only
  with the embedding model (a reflection-model call is refused before it reaches Ollama), and not
  an OllamaClient, so the mind treats it as injected: its GPU arbiter gets NullAdmin (no model is
  ever loaded, unloaded or pinned) and guild life never reads the game database.
- quiet() keeps only the background tasks a conversation needs (the control endpoint, the
  server's presence tick and lease, the signals capture) and makes the night never due.
- the copied mind.toml turns the rest off (prepare.QUIET)."""
import asyncio
import json
import logging
import sqlite3
import threading
from collections import Counter, deque
from pathlib import Path

from leeroy_mind.llm.client import LLMError
from leeroy_mind.logs import parse_message
from leeroy_mind.testing.fake_bridge import FakeBridge

from . import prepare

log = logging.getLogger("leeroy_mind")

SPEECH = frozenset({"ping", "get_cast", "say", "emote", "afk"})
CHANNELS = ("dm", "whisper", "guild", "officer", "party")
SETTLE_S = 4.0  # an answered line is done once nothing is planned or typed for this long
SETTLE_FAST_S = 1.0  # the same with --fast, where a follow-up line comes at once
MAX_WAIT_S = 600.0
NL = b"\n"
EVENT_PREFIXES = ("say_", "guard_", "think_beat", "owed_", "discord_", "talk_", "photo_answer",
                  "emote_dropped", "sandbox_", "lab_")


class SandboxError(Exception):
    """A request the sandbox cannot carry out, in words (the API's 400/409)."""

    def __init__(self, words: str, status: int = 400):
        super().__init__(words)
        self.status = status


# -- the model client -------------------------------------------------------------------------------

class SandboxLLM:
    """The real model client with the two limits in the module docstring."""

    def __init__(self, inner, *, chat_models, embed_models):
        self._inner = inner
        self.chat_models = frozenset(chat_models)
        self.embed_models = frozenset(embed_models)
        self.refused: Counter = Counter()

    def _refuse(self, kind: str, model: str):
        self.refused[kind] += 1
        log.warning("sandbox_model_refused: call=%s", kind)
        return LLMError(f"sandbox: the {kind} model {model!r} is not allowed here")

    async def chat(self, model, messages, **kwargs):
        if model not in self.chat_models:
            raise self._refuse("chat", model)
        return await self._inner.chat(model, messages, **kwargs)

    async def embed(self, model, inputs, **kwargs):
        if model not in self.embed_models:
            raise self._refuse("embed", model)
        return await self._inner.embed(model, inputs, **kwargs)

    async def list_models(self):
        return await self._inner.list_models()

    # the Free GPU gate and a photo borrow attach to the client (gpucontrol, nightly.wiring)
    @property
    def gpu_gate(self):
        return getattr(self._inner, "gpu_gate", None)

    @gpu_gate.setter
    def gpu_gate(self, gate):
        self._inner.gpu_gate = gate

    @property
    def live_borrow(self):
        return getattr(self._inner, "live_borrow", None)

    @live_borrow.setter
    def live_borrow(self, gate):
        self._inner.live_borrow = gate


# -- the bridge --------------------------------------------------------------------------------------

class SandboxBridge(FakeBridge):
    """mod-leeroy's bridge as the test suite fakes it, acking speech only."""

    def __init__(self, hello: dict, cast: dict):
        super().__init__([hello, cast], ack=self._policy)
        self.refused: Counter = Counter()

    def _policy(self, cmd: dict):
        kind = cmd.get("type")
        if kind in SPEECH:
            return True, ""
        self.refused[str(kind)] += 1
        return False, "sandbox"

    @property
    def cast(self) -> dict:
        return self._cast


# -- the log tap -------------------------------------------------------------------------------------

def event_of(record: logging.LogRecord) -> dict:
    """A log record as the JSON log has it: {"event", fields...}; "name: k=v" and "name k=v"
    messages alike."""
    event = getattr(record, "leeroy_event", None)
    if event is not None:
        return {"event": event, **getattr(record, "leeroy_fields", {})}
    msg = record.getMessage()
    out = parse_message(msg)
    if out.get("event") == "log":
        head, _, rest = msg.partition(" ")
        if head and head.isidentifier() and head.islower() and "=" in rest:
            out = parse_message(f"{head}: {rest}")
    return out


class EventTap(logging.Handler):
    """The mind's log events in memory, numbered, for a /say's report. The mind logs ids, codes
    and counts only, never a line's words."""
    KEEP = 20000

    def __init__(self):
        super().__init__(logging.INFO)
        self._events: deque = deque(maxlen=self.KEEP)
        self._seq = 0
        self._lock = threading.Lock()

    @property
    def seq(self) -> int:
        return self._seq

    def emit(self, record: logging.LogRecord) -> None:
        try:
            ev = event_of(record)
        except Exception:
            return
        with self._lock:
            self._seq += 1
            self._events.append((self._seq, ev))

    def since(self, seq: int) -> list[dict]:
        with self._lock:
            return [ev for n, ev in self._events if n > seq]


# -- quiet ---------------------------------------------------------------------------------------------

def quiet(app) -> list[str]:
    """Keep only the background tasks a conversation needs and make the night never due. The
    names of the tasks left out."""
    from leeroy_mind.app import _name
    keep = []
    if getattr(app, "control", None) is not None:
        keep.append(app.control.serve)
    if getattr(app, "signals", None) is not None:
        keep.append(app.signals.capture.run)
    if getattr(app, "server", None) is not None:
        keep.append(app.server.lease.run)
    kept, dropped = [], []
    for factory in app._background:
        wanted = factory in keep or _name(factory).endswith("play_tick")
        (kept if wanted else dropped).append(factory)
    app._background[:] = kept
    app.night_scheduler.due = lambda *, gate=True: None  # nothing can start a night
    return sorted(_name(f) for f in dropped)


# -- the sandbox ------------------------------------------------------------------------------------

def _summary(p) -> str:
    """age, gender, region, stance and job: never the real name or anything private."""
    return " · ".join(str(x) for x in (
        f"{p.identity.age} {p.identity.player_gender}", p.identity.region,
        p.romance.stance, p.real_life.job) if x)


class Sandbox:
    def __init__(self, layout: prepare.Layout, *, make_llm=None, online: bool = False,
                 seed: int | None = None, clock=None, ollama_conf: Path | None = None,
                 fast: bool = False):
        self.layout = layout
        self.fast = fast
        self.make_llm = make_llm
        self.start_online = online
        self.seed = seed
        self.clock = clock
        self.ollama_conf = ollama_conf
        self.tap = EventTap()
        self.bridge: SandboxBridge | None = None
        self.app = None
        self.task: asyncio.Task | None = None
        self.dropped_tasks: list[str] = []
        self.human = 0
        self.human_name = ""
        self._where = (0, 0)
        self._ro: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()
        self.stopped = asyncio.Event()
        self.resets = 0

    # -- lifecycle ---------------------------------------------------------------------------------

    async def start(self) -> None:
        logging.getLogger("leeroy_mind").addHandler(self.tap)
        hello, cast = {"type": "hello"}, {"type": "cast", "members": []}
        self.bridge = SandboxBridge(hello, cast)
        await self.bridge.start()
        await self._boot()

    def _greeting(self, online: bool) -> tuple[dict, dict]:
        from leeroy_mind.app import GUILD_ID
        hello = {"type": "hello", "server": "mod-leeroy", "version": "sandbox",
                 "guild_id": GUILD_ID, "human_guid": self.human if online else 0}
        members = [dict(m, online=online) if m["guid"] == self.human else m
                   for m in self.bridge.cast.get("members", [])]
        return hello, {"type": "cast", "members": members}

    def _read_copy(self) -> None:
        """The owner and the cast, from the restored copy: the whole cast online, as the
        pinned cast is in the live game; him as the sandbox starts."""
        from leeroy_mind.clock import SystemClock
        from leeroy_mind.storage.db import open_db
        from leeroy_mind.storage.store import Store
        store = Store(open_db(self.layout.data / prepare.DB), clock=self.clock or SystemClock())
        try:
            stored = store.get_meta("human_guid")
            if not (stored and stored.isdecimal() and int(stored) > 0):
                raise prepare.Refused("the copy names no owner (meta human_guid)")
            self.human = int(stored)
            self.human_name = store.get_meta("human_name") or ""
            members = []
            for m in store.cast().values():
                members.append({"guid": m.guid, "name": m.name, "race": m.race,
                                "class": m.class_, "gender": m.gender, "level": m.level,
                                "rank": m.rank, "online": m.guid != self.human, "map": m.map,
                                "zone": m.zone, "spec_tab": m.spec_tab})
                if m.guid == self.human:
                    self._where = (m.map, m.zone)
                    self.human_name = self.human_name or m.name
        finally:
            store.close()
        self.bridge.set_greeting(cast={"type": "cast", "members": members})

    async def _boot(self, overlays: dict | None = None) -> None:
        from leeroy_mind.app import MindApp, startup_checks
        from leeroy_mind.config import load_config
        from leeroy_mind.llm.client import OllamaClient
        prepare.restore(self.layout, bridge_port=self.bridge.port, overlays=overlays)
        self._read_copy()
        hello, cast = self._greeting(self.start_online)
        self.bridge.set_greeting(hello=hello, cast=cast)
        cfg = load_config(self.layout.data / prepare.TOML)
        inner = self.make_llm(cfg) if self.make_llm is not None else OllamaClient(cfg.ollama.url)
        llm = SandboxLLM(inner, chat_models={cfg.models.live}, embed_models={cfg.models.embedding})
        app = MindApp(cfg, self.layout.data, llm=llm, clock=self.clock, rng_seed=self.seed)
        self.app = app
        if self.fast:  # no reading delay or typing time is waited out: answers in model time
            app.engine.wait_scale = 0.0
        self.dropped_tasks = quiet(app)
        armory = getattr(getattr(app, "guild", None), "armory", None)
        if armory is not None and getattr(armory, "available", False):
            raise RuntimeError("sandbox: guild life could reach the game database")
        await startup_checks(app, ollama_conf=self.ollama_conf)
        self.task = asyncio.create_task(app.run(), name="sandbox-mind")
        try:
            async with asyncio.timeout(60):
                while not (app.hello is not None and app.world.members):
                    if self.task.done():
                        self.task.result()
                        raise RuntimeError("sandbox: the mind stopped while starting")
                    await asyncio.sleep(0.02)
        except BaseException:
            await self._halt()
            raise
        self._ro = sqlite3.connect(f"{(self.layout.data / prepare.DB).resolve().as_uri()}?mode=ro",
                                   uri=True, check_same_thread=False)
        self._ro.row_factory = sqlite3.Row
        log.info("sandbox_started: cast=%d dropped=%d online=%d fast=%d resets=%d",
                 len(app.world.members), len(self.dropped_tasks), int(self.start_online),
                 int(self.fast), self.resets)

    async def _halt(self) -> None:
        if self._ro is not None:
            self._ro.close()
            self._ro = None
        app, task, self.app, self.task = self.app, self.task, None, None
        if app is None:
            return
        await app.stop()
        if task is not None:
            try:
                await asyncio.wait_for(task, 120)
            except Exception as e:
                log.warning("sandbox_mind_ended: error=%s", type(e).__name__)

    async def close(self) -> None:
        async with self._lock:
            await self._halt()
            if self.bridge is not None:
                await self.bridge.stop()
            logging.getLogger("leeroy_mind").removeHandler(self.tap)
        self.stopped.set()

    async def reset(self, *, overlays: dict | None = None) -> dict:
        """Stop the mind, restore DIR/mind-data from the snapshot and start a new mind on it,
        with him as the sandbox started (the bridge and the API stay up). `overlays` (piece name
        to text, talk/pieces.py) go into the restored prompts/ as the lab's overlays."""
        try:
            prepare.check_overlays(overlays or {})
        except prepare.Refused as e:
            raise SandboxError(str(e)) from None
        async with self._lock:
            await self._halt()
            self.resets += 1
            await self._boot(overlays)
            return self.status()

    # -- what the API reads ------------------------------------------------------------------------

    def _now(self) -> float:
        return self.app.clock.now()

    def status(self) -> dict:
        app = self.app
        info = prepare.snapshot_info(self.layout) or {}
        world = app.world if app is not None else None
        llm = app.llm if app is not None else None
        group = world.group if world is not None else None
        return {
            "ok": True, "running": app is not None and app.running,
            "data_dir": str(self.layout.data), "snapshot_taken": info.get("taken"),
            "resets": self.resets, "fast": self.fast,
            "human": {"guid": self.human, "name": self.human_name,
                      "online": bool(world and world.human_online)},
            "group": list(group.members) if group is not None else [],
            "cast": len(world.cast_guids()) if world is not None else 0,
            "model": {"live": app.cfg.models.live if app else None,
                      "ready": bool(app and app.model_ready),
                      "refused": dict(llm.refused) if isinstance(llm, SandboxLLM) else {}},
            "gpu_paused": bool(app and getattr(app.arbiter, "paused", False)),
            "quiet": self.dropped_tasks,
            "bridge_refused": dict(self.bridge.refused) if self.bridge else {},
        }

    def members(self, *, online_only: bool = False) -> list[dict]:
        app = self._need_app()
        out = []
        for guid in sorted(app.world.cast_guids()):
            if guid == self.human:
                continue
            m = app.world.members.get(guid)
            online = guid in app.world.online
            if online_only and not online:
                continue
            p = app.personas.get(guid)
            out.append({"guid": guid, "name": m.name if m else "", "online": online,
                        "persona": p is not None,
                        "stance": p.romance.stance if p is not None else None,
                        "summary": _summary(p) if p is not None else ""})
        return out

    def read_log(self, since: int | None, limit: int = 200) -> dict:
        """mind.log's JSON lines: the last `limit` without `since`, else those after the byte
        offset `since` (the `next` an earlier read gave; a rotated, shorter log starts over)."""
        limit = max(1, min(int(limit), 2000))
        try:
            data = (self.layout.data / "logs" / "mind.log").read_bytes()
        except FileNotFoundError:
            return {"lines": [], "next": 0}
        end = data.rfind(NL) + 1  # whole lines only
        if since is None:
            raw = data[:end].split(NL)[:-1][-limit:]
            start = end - sum(len(x) + 1 for x in raw)
        else:
            start = since if 0 <= since <= end else 0
            raw = data[start:end].split(NL)[:-1][:limit]
        lines = []
        for line in raw:
            try:
                lines.append(json.loads(line))
            except ValueError:
                lines.append({"raw": line.decode("utf-8", "replace")})
        return {"lines": lines, "next": start + sum(len(x) + 1 for x in raw)}

    def _need_app(self):
        if self.app is None:
            raise SandboxError("the sandbox mind is not running", 409)
        return self.app

    # -- acting as him -----------------------------------------------------------------------------

    async def _wait(self, check, what: str, seconds: float = 15.0) -> None:
        try:
            async with asyncio.timeout(seconds):
                while not check():
                    await asyncio.sleep(0.02)
        except TimeoutError:
            raise SandboxError(f"the mind did not take {what} in time", 409) from None

    async def login(self, *, wait: float = 0.0) -> dict:
        async with self._lock:
            app = self._need_app()
            marks = self._marks()
            hello, cast = self._greeting(True)
            self.bridge.set_greeting(hello=hello, cast=cast)
            if not app.world.human_online:
                await self.bridge.push({"type": "login", "guid": self.human, "human": True})
                await self._wait(lambda: app.world.human_online, "the login")
            lines = []
            if wait > 0:
                await self._sleep(min(wait, MAX_WAIT_S))
                lines = [self._line(r, None) for r in self._lines_after(marks)]
            return {"ok": True, "online": True, "lines": lines}

    async def logout(self) -> dict:
        async with self._lock:
            app = self._need_app()
            hello, cast = self._greeting(False)
            self.bridge.set_greeting(hello=hello, cast=cast)
            if app.world.human_online:
                await self.bridge.push({"type": "logout", "guid": self.human, "human": True})
                await self._wait(lambda: not app.world.human_online, "the logout")
            return {"ok": True, "online": False}

    async def group(self, members) -> dict:
        async with self._lock:
            app = self._need_app()
            if not isinstance(members, list) or not all(type(g) is int for g in members):
                raise SandboxError("members: a list of guids")
            cast = app.world.cast_guids()
            if any(g not in cast or g == self.human for g in members):
                raise SandboxError("members: guids of the cast, not him")
            if not app.world.human_online and members:
                raise SandboxError("he is not in game: POST /login first", 409)
            ids = [self.human, *dict.fromkeys(members)] if members else []
            if len(ids) > 40:
                raise SandboxError("members: at most 39")
            ev = {"type": "group", "leader_guid": self.human if ids else 0,
                  "raid": len(ids) > 5, "members": ids}
            await self.bridge.push(ev)
            await self._wait(lambda: (list(app.world.group.members) if app.world.group else [])
                             == ids, "the group")
            return {"ok": True, "group": ids, "raid": len(ids) > 5}

    async def say(self, channel: str, to, text: str, *, wait: float = 120.0) -> dict:
        if channel not in CHANNELS:
            raise SandboxError("channel: one of " + ", ".join(CHANNELS))
        if not isinstance(text, str) or not text.strip():
            raise SandboxError("text: his line")
        if type(wait) not in (int, float) or not 1 <= wait <= MAX_WAIT_S:
            raise SandboxError(f"wait: 1 to {int(MAX_WAIT_S)} seconds")
        async with self._lock:
            app = self._need_app()
            target = 0
            if channel in ("dm", "whisper"):
                if type(to) is not int or to not in app.world.cast_guids() or to == self.human:
                    raise SandboxError("to: a cast member's guid")
                target = to
            if channel != "dm" and not app.world.human_online:
                raise SandboxError("he is not in game: POST /login first", 409)
            if channel == "party" and not app.world.in_group(self.human):
                raise SandboxError("he has no group: POST /group first", 409)
            marks = self._marks()
            t0 = self._now()
            message = None
            if channel == "dm":
                message = await self._post_dm(target, text)
            else:
                game = "whisper" if channel == "whisper" else channel
                await self.bridge.push({"type": "chat", "channel": game,
                                        "from_guid": self.human, "from_name": self.human_name,
                                        "to_guid": target, "text": text,
                                        "map": self._where[0], "zone": self._where[1]})
            his = await self._his_line(marks)
            result = await self._settle(channel, target, marks, his, t0, t0 + wait, message)
            log.info("sandbox_say: channel=%s to=%d answers=%d dropped=%d settled=%d",
                     channel, target, len(result["answers"]), len(result["dropped"]),
                     int(result["settled"]))
            return result

    async def _post_dm(self, target: int, text: str) -> int:
        from leeroy_mind.endpoint import BadRequest, discord_args
        app = self.app
        try:
            args = discord_args({"action": "post", "channel": f"dm:{target}", "text": text})
        except BadRequest as e:
            raise SandboxError(str(e)) from None
        control = getattr(app, "control", None)
        reply = await (control.discord(args) if control is not None else app.discord.handle(args))
        if not reply.get("ok"):
            raise SandboxError(reply.get("error") or "the DM was refused", 409)
        return reply["id"]

    async def _his_line(self, marks: dict) -> dict:
        found: list = []

        def check():
            row = self._ro.execute(
                "SELECT id, ts, channel, text FROM chat_line WHERE id > ? AND from_guid = ? "
                "AND origin = 'human' ORDER BY id LIMIT 1", (marks["chat"], self.human)).fetchone()
            if row is not None:
                found.append(dict(row))
            return bool(found)
        await self._wait(check, "his line")
        return found[0]

    async def _sleep(self, seconds: float) -> None:
        end = self._now() + seconds
        while self._now() < end:
            await asyncio.sleep(0.05)

    # -- what came back ------------------------------------------------------------------------------

    def _marks(self) -> dict:
        def top(table: str) -> int:
            try:
                return self._ro.execute(f"SELECT coalesce(max(id), 0) FROM {table}").fetchone()[0]
            except sqlite3.OperationalError:
                return 0
        return {"chat": top("chat_line"), "discord": top("discord_message"),
                "utterance": top("utterance"), "bakeoff": top("bakeoff_turn"),
                "events": self.tap.seq}

    def _busy(self) -> bool:
        engine = self.app.engine
        return bool(engine.planned_turns()) or bool(engine.typing_now())

    def _lines_after(self, marks: dict) -> list[dict]:
        """Member lines since the marks that he could see: every game line but whispers between
        two others, and every Discord line (DMs and group DMs always have him in them)."""
        rows = self._ro.execute(
            "SELECT id, ts, channel, from_guid, from_name, to_guid, text FROM chat_line "
            "WHERE id > ? AND origin = 'mind' ORDER BY id", (marks["chat"],)).fetchall()
        h = self.human
        return [dict(r) for r in rows
                if not (r["channel"] == "whisper" and h not in (r["from_guid"], r["to_guid"]))]

    def _line(self, row: dict, his: dict | None) -> dict:
        return {"line": row["id"], "guid": row["from_guid"], "name": row["from_name"],
                "channel": row["channel"], "text": row["text"],
                "latency_s": None if his is None else round(row["ts"] - his["ts"], 2)}

    def _answers(self, channel: str, target: int, row: dict) -> bool:
        if channel == "dm":
            return row["channel"] == f"dm:{target}" and row["from_guid"] == target
        if channel == "whisper":
            return (row["channel"] == "whisper" and row["from_guid"] == target
                    and row["to_guid"] == self.human)
        return row["channel"] == channel

    async def _settle(self, channel, target, marks, his, t0, deadline, message) -> dict:
        seen, last = -1, self._now()
        settled, settle_s = False, SETTLE_FAST_S if self.fast else SETTLE_S
        while True:
            rows = self._lines_after(marks)
            now = self._now()
            if len(rows) != seen:
                seen, last = len(rows), now
            answered = any(self._answers(channel, target, r) for r in rows)
            busy = self._busy()
            if answered and not busy and now - last >= settle_s:
                settled = True
                break
            if now >= deadline:
                settled = answered and not busy
                break
            await asyncio.sleep(0.05)
        return self._report(channel, target, marks, his, rows, settled, round(now - t0, 2),
                            message)

    def _report(self, channel, target, marks, his, rows, settled, waited, message) -> dict:
        ro = self._ro
        events = self.tap.since(marks["events"])
        utterances = [dict(r) for r in ro.execute(
            "SELECT * FROM utterance WHERE id > ? ORDER BY id", (marks["utterance"],))]
        try:
            captured = [dict(r) for r in ro.execute(
                "SELECT * FROM bakeoff_turn WHERE id > ? ORDER BY id", (marks["bakeoff"],))]
        except sqlite3.OperationalError:
            captured = []
        discord = {}
        for r in ro.execute("SELECT id FROM discord_message WHERE id > ? AND origin = 'mind'",
                            (marks["discord"],)):
            line = ro.execute("SELECT value FROM meta WHERE key = ?",
                              (f"discord:line:{r['id']}",)).fetchone()
            if line is not None and str(line[0]).isdecimal():
                discord[int(line[0])] = r["id"]
        by_ref: dict[str, dict] = {}
        for c in captured:
            for ref in json.loads(c["refs"] or "[]"):
                by_ref[ref] = c
        names = {r["from_guid"]: r["from_name"] for r in rows}
        answers, others, shown_prompt, used = [], [], set(), set()
        for row in rows:
            out = self._line(row, his)
            if row["id"] in discord:
                out["discord_message"] = discord[row["id"]]
            utt = next((u for u in utterances if u["id"] not in used and u["dropped"] is None
                        and u["guid"] == row["from_guid"] and u["channel"] == row["channel"]
                        and u["text"] == row["text"]), None)
            if utt is not None:
                used.add(utt["id"])
                out.update(kind=utt["kind"], why=utt["why"], turn_id=utt["turn_id"],
                           llm_s=utt["llm_s"],
                           guard={"verdict": utt["guard_verdict"],
                                  "rewritten": bool(utt["guard_rewritten"])})
            ref = f"d:{discord[row['id']]}" if row["id"] in discord else f"c:{row['id']}"
            cap = by_ref.get(ref)
            if cap is not None:
                out["attempts"], out["sent_attempt"] = cap["calls"], cap["attempt"]
                out["beat"] = {"asked": bool(cap["beat"]), "want": cap["want"]}
                if cap["id"] in shown_prompt:
                    out["prompt"] = {"same_as_line": next(
                        a["line"] for a in answers + others if a.get("capture") == cap["id"])}
                else:
                    shown_prompt.add(cap["id"])
                    out["prompt"] = _prompt(cap["messages"], cap["options"], cap["reply"],
                                            model=cap["model"], think=cap["think"])
                    if cap["first_messages"] is not None:
                        out["first_prompt"] = _prompt(cap["first_messages"], cap["first_options"],
                                                      cap["first_reply"])
                out["capture"] = cap["id"]
            mine = [e for e in events if e.get("guid") == row["from_guid"]]
            reviews = [{"action": e.get("action"), "codes": _codes(e.get("codes"))}
                       for e in mine if e.get("event") == "guard_review"]
            if reviews:
                out.setdefault("guard", {})["reviews"] = reviews
                out["guard"]["codes"] = sorted({c for r in reviews for c in r["codes"]})
            beats = [e for e in mine if e.get("event") in ("think_beat", "think_beat_skipped")]
            if beats:
                b = beats[-1]
                beat = out.setdefault("beat", {"asked": b.get("event") == "think_beat",
                                               "want": b.get("want")})
                if b.get("event") == "think_beat":
                    beat.update(use=bool(b.get("use")), ms=b.get("ms"))
                else:
                    beat.update(skipped=b.get("reason"), ms=b.get("ms"))
            (answers if self._answers(channel, target, row) else others).append(out)
        drops = []
        for u in utterances:
            if u["dropped"] is None:
                continue
            codes = [_codes(e.get("codes")) for e in events
                     if e.get("event") == "guard_drop" and e.get("guid") == u["guid"]]
            name = names.get(u["guid"])
            if name is None and self.app is not None:
                p = self.app.personas.get(u["guid"])
                name = p.character_name if p is not None else ""
            drops.append({"guid": u["guid"], "name": name, "channel": u["channel"],
                          "kind": u["kind"], "reason": u["dropped"], "why": u["why"],
                          "guard_verdict": u["guard_verdict"],
                          "codes": sorted({c for cs in codes for c in cs}),
                          "draft": u["text"] or None})
        reactions = [] if message is None else [dict(r) for r in ro.execute(
            "SELECT member AS guid, emoji FROM discord_reaction WHERE message = ? AND member != ? "
            "ORDER BY ts", (message, self.human))]
        for a in answers + others:
            a.pop("capture", None)
        return {"ok": True, "channel": channel, "to": target or None,
                "his": {"line": his["id"], "message": message, "channel": his["channel"],
                        "text": his["text"]},
                "answers": answers, "others": others, "dropped": drops,
                "reactions": reactions, "settled": settled, "waited_s": waited,
                "events": [e for e in events if str(e.get("event", "")).startswith(EVENT_PREFIXES)
                           ][:300]}


def _codes(raw) -> list[str]:
    if isinstance(raw, list):
        return [str(c) for c in raw]
    return [c for c in str(raw or "").split(",") if c and c != "-"]


def _prompt(messages, options, reply, *, model=None, think=None) -> dict:
    from leeroy_mind.text import estimate_tokens
    msgs = json.loads(messages) if isinstance(messages, str) else messages
    out = {"messages": msgs,
           "tokens": sum(estimate_tokens(str(m.get("content", ""))) for m in msgs),
           "options": json.loads(options) if isinstance(options, str) else options,
           "reply": reply}
    if model is not None:
        out["model"] = model
        out["think"] = think
    return out
