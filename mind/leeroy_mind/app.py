"""The mind service: MindApp wires the bridge, memory and talk into one running process.

Bridge events arrive one at a time (BridgeClient's single dispatcher) and go through handle():
the app's own row first (the dispatch table below), then the handlers extensions registered
for that event type. Nothing in a row waits on the model: talk is planned and spawned by the
engine, extraction and event memories run in their own workers.

- hello: meta guild_id and human_guid (the last non-zero one), then the session resync: him
  online with none open starts one (or reuses the one a crash left), offline ends it.
- cast: the store and the world.
- the human's login / logout: his session starts (mind.toml reloaded) or ends; a login is
  greeted.
- chat: an echo of the mind's own line is dropped; anything else is stored with its origin
  and audience, and his lines (not his bot commands) go to the world, the threads and the
  engine.
- afk, text_emote: stored and applied to the world, never talked about.
- error: logged only. A type an extension registered: stored only.
- everything else: stored, the world, the raid state, the event memories, the engine.

Events the bridge replays on connect (older than the hello by 5 s or more) are recorded the
same way but never reach the engine or start a session (pre-flight ruling 5). They are stored,
threaded and remembered at the time they happened, not when they were replayed.

P1c and P1d attach with one line each at the end of MindApp.__init__ (self.nightly =
attach(self), then self.guild = install(self)) and use the registries and attributes there."""
import asyncio
import functools
import importlib.metadata
import logging
import math
import os
import random
import re
import statistics
from pathlib import Path
from typing import Awaitable, Callable, Literal

from leeroy_mind.bridge import BridgeClient
from leeroy_mind.clock import Clock, SystemClock
from leeroy_mind.config import ConfigError, MindConfig, load_config
from leeroy_mind.human import HumanIdentity
from leeroy_mind.library.core import Library
from leeroy_mind.llm.client import LLMClient, LLMError, OllamaClient
from leeroy_mind.llm.presets import LIVE
from leeroy_mind.llm.scheduler import LLMScheduler
from leeroy_mind.logs import log as log_event
from leeroy_mind.memory.events import EventMemoryRules
from leeroy_mind.memory.extract import MAX_BYSTANDERS, Extractor
from leeroy_mind.memory.recall import Recall
from leeroy_mind.memory.threads import ClosedThread, ThreadTracker
from leeroy_mind.memory.writer import Embedder, MemoryWriter
from leeroy_mind.personas.io import PersonaCache
from leeroy_mind.protocol import (CORE_EVENT_TYPES, Ack, Achievement, AfkEvent, BgEnd, Boss,
                                  Cast, Chat, Death, Error, Event, Group, Hello, Login, Logout,
                                  LootWon, TextEmote, Zone, event_type)
from leeroy_mind.storage.db import VEC_DIM, check_embedding_meta, checkpoint, open_db
from leeroy_mind.storage.store import Store
from leeroy_mind.talk.engine import COMMAND_CHANNELS, TalkEngine
from leeroy_mind.talk.raid import RaidState
from leeroy_mind.talk.turns import TurnKind
from leeroy_mind.talk.world import WorldView, looks_like_bot_command

log = logging.getLogger("leeroy_mind.app")

GUILD_ID = 21  # the guild mod-leeroy serves (Leeroy.GuildId)
BACKLOG_MS = 5000  # an event this much older than the hello was buffered while we were away
BYSTANDER_S = 900.0
TICK_S = 1.0
EMBED_EVERY_S = 10.0
EXTRACT_BUDGET_S = 10.0
ACTIVITY_BUDGET_S = 10.0
OFFLINE_WAIT_S = 60.0  # the offline hooks wait this long, at most, for the session's last writes
CANCEL_WAIT_S = 5.0
LIST_MODELS_S = 10.0
HEALTH_S = 60.0  # how often the silent latch looks for the live model again
STATS_S = 900.0  # one stats line this often (liveops M5)
# the stats line's replies to him: the kinds talk.select.plan_reply_turns gives the answers to
# his lines (his goodbye gets goodnight turns); a greeting answers his login, not a line
ANSWERS_HIS_LINES = frozenset({TurnKind.REPLY_HUMAN.value, TurnKind.GOODNIGHT.value})
BANNER_WAIT_S = 10.0  # the start banner waits this long for the bridge's hello, at most
# drops_high (liveops M6): this many turns dropped for the model's sake within the window
# warn, at most once per DROPS_WARN_S
MODEL_DROPS = ("expired", "llm_error")
DROPS_HIGH = 5
DROPS_WINDOW_S = 60.0
DROPS_WARN_S = 600.0
RESUME_S = 1800.0  # a session a crash left open is resumed only if it was active this recently
MODEL_AWAY = "model_away"  # the drop reason of a line refused while the model is away
LOCK_FILE = "mind.lock"
LIBRARY_FILE = "library.db"  # the reference shelf `python -m leeroy_mind.library` builds
OLLAMA_CHAT_CONF = Path(
    r"F:\azerothcore\build\bin\RelWithDebInfo\configs\modules\mod_ollama_chat.conf")
_MEMORY_EVENTS = (Boss, Death, LootWon, Achievement, BgEnd, Zone)


class _ActivityFlush:
    """The memory queue's marker where a session ended: write its activity summaries, then
    set `done` (the offline hooks wait for it)."""
    __slots__ = ("done",)

    def __init__(self):
        self.done = asyncio.Event()


_CONF_LINE = re.compile(r"^[ \t]*(OllamaChat\.(?:Model|NumCtx))[ \t]*=[ \t]*(.*?)[ \t]*\r?$",
                        re.MULTILINE)


# -- one service at a time ----------------------------------------------------------------------

class AlreadyRunning(RuntimeError):
    """Another mind service holds mind-data/mind.lock."""


if os.name == "nt":
    import msvcrt

    def _lock(fh) -> None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(fh) -> None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _lock(fh) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fh) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


class InstanceLock:
    """An exclusive OS lock on mind-data/mind.lock (msvcrt.locking on Windows, fcntl.flock
    elsewhere), held until release() or the end of the with block. The OS drops it if the
    process dies, so a crash never leaves the data dir locked."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._fh = None

    def acquire(self) -> "InstanceLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+b")
        try:
            _lock(fh)
        except OSError:
            fh.close()
            raise AlreadyRunning(f"{self.path} is held by a running mind service") from None
        self._fh = fh
        return self

    def release(self) -> None:
        fh, self._fh = self._fh, None
        if fh is not None:
            try:
                _unlock(fh)
            except OSError:
                pass
            fh.close()

    def __enter__(self) -> "InstanceLock":
        return self

    def __exit__(self, *exc) -> None:
        self.release()


def acquire_instance_lock(data_dir: Path) -> InstanceLock:
    """Raises AlreadyRunning when a mind service (or P1c's live-db night run) holds it."""
    return InstanceLock(Path(data_dir) / LOCK_FILE).acquire()


def instance_lock_held(data_dir: Path) -> bool:
    path = Path(data_dir) / LOCK_FILE
    if not path.exists():
        return False
    try:
        probe = InstanceLock(path).acquire()
    except AlreadyRunning:
        return True
    probe.release()
    return False


# -- which code runs ---------------------------------------------------------------------------

def _git_commit(repo: Path) -> str | None:
    """The commit (7 hex digits) HEAD names in the git checkout at `repo`, a worktree included,
    read from its files without running git; None when it is not one or cannot be read."""
    try:
        gitdir = Path(repo) / ".git"
        if gitdir.is_file():  # a worktree: "gitdir: <its dir under the main .git>"
            gitdir = Path(repo) / gitdir.read_text(encoding="utf-8").split(":", 1)[1].strip()
        head = (gitdir / "HEAD").read_text(encoding="utf-8").strip()
        if not head.startswith("ref:"):
            return head[:7] or None  # a detached head
        ref = head[4:].strip()
        common = gitdir
        if (gitdir / "commondir").is_file():
            common = gitdir / (gitdir / "commondir").read_text(encoding="utf-8").strip()
        for base in (gitdir, common):
            if (base / ref).is_file():
                return (base / ref).read_text(encoding="utf-8").strip()[:7] or None
        for line in (common / "packed-refs").read_text(encoding="utf-8").splitlines():
            if line.endswith(" " + ref):
                return line[:7]
    except (OSError, IndexError, ValueError):
        pass
    return None


@functools.cache
def code_version() -> str:
    """The package's version, with the commit of the checkout it runs from when it is one
    ("0.1.0+a9f5ed2"), for mind_started (liveops M5)."""
    try:
        version = importlib.metadata.version("leeroy-mind")
    except importlib.metadata.PackageNotFoundError:
        version = "unknown"
    commit = _git_commit(Path(__file__).resolve().parents[2])
    return f"{version}+{commit}" if commit else version


# -- startup checks ----------------------------------------------------------------------------

def _unquote(raw: str) -> str:
    return raw[1:-1] if len(raw) >= 2 and raw[0] == raw[-1] == '"' else raw


def check_ollama_chat(conf: Path | None, cfg: MindConfig) -> list[str]:
    """mod-ollama-chat shares the GPU and must name the mind's live model with the same
    num_ctx (the card holds one live model; another num_ctx reloads it). One warning per
    difference; a missing file gives one. `conf` None means the server's own file."""
    path = OLLAMA_CHAT_CONF if conf is None else Path(conf)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [f"{path} not found: cannot check that mod-ollama-chat uses the live model"]
    values = {key: _unquote(raw) for key, raw in _CONF_LINE.findall(text)}  # the last wins
    out = []
    model, ctx = values.get("OllamaChat.Model"), values.get("OllamaChat.NumCtx")
    live, live_ctx = cfg.models.live, LIVE.options["num_ctx"]
    if model != live:
        out.append(f"OllamaChat.Model is {model!r}, the mind's live model is {live!r}: both "
                   "must name the same model" if model is not None else
                   f"OllamaChat.Model is not set in {path.name}; it must be {live!r}")
    if ctx != str(live_ctx):
        out.append(f"OllamaChat.NumCtx is {ctx}, the mind uses {live_ctx}: another context "
                   "size makes Ollama reload the model" if ctx is not None else
                   f"OllamaChat.NumCtx is not set in {path.name}; it must be {live_ctx}")
    return out


def has_model(names: set[str], model: str) -> bool:
    return model in names or (":" not in model and f"{model}:latest" in names)


async def startup_checks(app: "MindApp", *, ollama_conf: Path | None = None) -> None:
    """What `run` checks before it serves. The embedding model mind.db was built with must be
    the configured one (EmbeddingMismatch is raised: new vectors would not compare with the
    old). mod-ollama-chat's model and context are only logged. The first model check sets the
    silent latch (MindApp.check_model): without the live model the mind keeps going, recording,
    and says nothing until a later check finds it. A missing embedding model is logged too:
    memories are then stored without vectors."""
    check_embedding_meta(app.store, app.cfg.models.embedding, VEC_DIM)
    app.watch_ollama_chat(ollama_conf)  # the health loop reads it again (liveops M6)
    await app.check_model()
    seen = app.models_seen
    if seen is not None and not has_model(seen, app.cfg.models.embedding):
        log_event("ollama_model_missing", level=logging.WARNING,
                  missing=[app.cfg.models.embedding])


# -- the app -----------------------------------------------------------------------------------

def _name(fn) -> str:
    return getattr(fn, "__qualname__", None) or type(fn).__name__


def _cancelling() -> bool:
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0


async def _cancel_wait(tasks) -> None:
    """Cancel the tasks and wait for them to end (CANCEL_WAIT_S at most: a task that ignores
    the cancel is logged and left)."""
    tasks = [t for t in tasks if t is not None and not t.done()]
    for t in tasks:
        t.cancel()
    if tasks:
        _, stuck = await asyncio.wait(tasks, timeout=CANCEL_WAIT_S)
        for t in stuck:
            log.warning("task_ignored_cancel: task=%s", t.get_name())


class MindApp:
    """The running mind. __init__ builds everything and starts nothing; run() serves until
    stop() or a cancel (Ctrl+C under asyncio.run).

    Extensions read the attributes cfg, data_dir, clock, store, llm, scheduler, embedder,
    writer, event_rules, extractor, personas, world, raid, engine, bridge, library (the
    read-only Library on data_dir/library.db, None without one) and arbiter (None until P1c
    sets its GpuArbiter), and hook in with register_event_handler, register_connection_hook,
    register_offline_hook and register_background. Event handlers and connection hooks run on
    the bridge's one dispatcher: keep them short, spawn anything long. Offline hooks run in a
    task of their own (register_offline_hook)."""

    extract_budget_s = EXTRACT_BUDGET_S  # stop(): real seconds for the last extractions
    activity_budget_s = ACTIVITY_BUDGET_S

    def __init__(self, cfg: MindConfig, data_dir: Path, *, llm: LLMClient | None = None,
                 clock: Clock | None = None, rng_seed: int | None = None):
        self.cfg = cfg
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.clock: Clock = clock or SystemClock()
        self.store = Store(open_db(self.data_dir / "mind.db"), clock=self.clock)
        self.llm: LLMClient = llm if llm is not None else OllamaClient(cfg.ollama.url)
        self.scheduler = LLMScheduler(self.llm, clock=self.clock,
                                      max_inflight=cfg.ollama.max_inflight)
        self.embedder = Embedder(self.scheduler, cfg.models.embedding)
        self.personas = PersonaCache(self.store)
        stored = self.store.get_meta("human_guid")
        self._human_named = bool(stored and stored.isdigit() and int(stored))  # by the bridge
        self._human = int(stored) if self._human_named else self._guild_master()
        self._human_name = self.store.get_meta("human_name")
        self.writer = MemoryWriter(self.store, self.embedder, self.clock, human=self._identity)
        self.event_rules = EventMemoryRules(self.writer, self.store, self.clock, self.personas.get,
                                            self._human_guid)
        self.threads = ThreadTracker(self.store)
        self.extractor = Extractor(self.store, self.scheduler, self.writer,
                                   lambda: self.cfg.models.live, human_guid=self._human_guid,
                                   bystanders=self.bystanders, human=self._identity,
                                   said=lambda line_id: self.engine.said_words(line_id))
        self.world = WorldView(is_officer=self._is_officer)
        self.world.human_guid = self._human
        self.raid = RaidState(world=self.world, personas=self.personas.get, clock=self.clock)
        self.recall = Recall(self.store, self.embedder, self.clock, tau_days=cfg.memory.depth_days)
        self.rng = random.Random(rng_seed)
        self.library = self._open_library()
        self.engine = TalkEngine(
            store=self.store, world=self.world, raid=self.raid, recall=self.recall,
            scheduler=self.scheduler, personas=self.personas, cfg=cfg, data_dir=self.data_dir,
            clock=self.clock, rng=self.rng, send=self._send, human_guid=self._human,
            on_line_sent=self._on_line_sent, library=self.library)
        self.bridge = BridgeClient(cfg.bridge.host, cfg.bridge.port, self.handle,
                                   clock=self.clock, on_connection=self._on_connection)
        self.arbiter = None
        self.running = False
        self.models_seen: set[str] | None = None  # what the last model check listed
        self._model_ready = self._model_known = False  # the silent latch (check_model)
        self._handlers: dict[str, list[Callable[[Event], Awaitable[None]]]] = {}
        self._connection_hooks: list[Callable[[bool], Awaitable[None]]] = []
        self._offline_hooks: list[Callable[[], Awaitable[None]]] = []
        self._background: list[Callable[[], Awaitable[None]]] = []
        self._session_id: int | None = None
        self._hello_ts: int | None = None  # the server's ms ts of the last hello
        self._spoke_at: dict[int, float] = {}  # anyone's last line, any channel or origin
        self._extract_queue: asyncio.Queue[ClosedThread] = asyncio.Queue()
        self._memory_queue: asyncio.Queue = asyncio.Queue()
        self._loops: dict[str, asyncio.Task] = {}
        self._bg_tasks: list[asyncio.Task] = []
        self._embed_task: asyncio.Task | None = None
        self._started = self._stopping = self._closed = False
        self._stop_requested = asyncio.Event()
        self._stop_task: asyncio.Task | None = None
        self._offline_tasks: set[asyncio.Task] = set()
        self._offline_lock = asyncio.Lock()  # one offline-hook run at a time (_after_session)
        self._banner_task: asyncio.Task | None = None  # the start banner's, once run() starts it
        # mod-ollama-chat's conf as watch_ollama_chat last read it (None: it never has)
        self._chat_conf: Path | None = None
        self._chat_warnings: frozenset[str] | None = None
        self._drops_warned_at = -math.inf  # the last drops_high (_check_drops)
        self._stats_since: float | None = None  # where the open stats window began (_stats)
        # No thread survives a restart: what a crash left open goes to P1c's threads_pending.
        if orphans := self.store.pend_open_threads(self.clock.now()):
            log_event("threads_reclaimed", threads=orphans)
        # Extensions attach below, one line each: P1c's self.nightly = attach(self), then
        # P1d's self.guild = install(self).

    # -- extension points --------------------------------------------------------------------

    def register_event_handler(self, type_name: str,
                               handler: Callable[[Event], Awaitable[None]]) -> None:
        """Awaited after the app's own row for every event of that type it receives (echoes
        and replayed events included, see is_backlog; ack and pong never arrive). One that
        raises is logged and the others still run. Once stop() has begun, events are only
        recorded and no handler runs."""
        self._handlers.setdefault(type_name, []).append(handler)

    def register_connection_hook(self, hook: Callable[[bool], Awaitable[None]]) -> None:
        """Called with True when the bridge connects and False when it drops."""
        self._connection_hooks.append(hook)

    def register_offline_hook(self, hook: Callable[[], Awaitable[None]]) -> None:
        """Called once the human's session has ended: his logout, or a hello saying he is gone
        (which also ends a session a crash left open). The hooks run one after another in a
        task of their own, once that session's threads are extracted and its activity
        summaries written: up to OFFLINE_WAIT_S after it ended, so he may be back by then.
        They run once per quiet period, and runs never overlap:
        - If he is back (a new session is open), they are skipped: offline_hooks_skipped,
          reason=new_session.
        - If a later session has ended too (logout, relog, logout), only the latest one's run
          happens; the earlier is skipped with reason=newer_session.
        - A run waits for the one before it to end, however long it takes.
        - Shutdown cancels them, and a session that ends once stop() has begun starts none
          (reason=stopping), so a job started here (P1c's night job) must also look for work
          left undone when it starts.
        - A session a crash left open from another evening, found when he is back online, is
          closed without them."""
        self._offline_hooks.append(hook)

    def register_background(self, factory: Callable[[], Awaitable[None]]) -> None:
        """run() starts factory() as a task once its own loops are up (at once if it already
        runs); stop() cancels these first."""
        self._background.append(factory)
        if self.running and not self._stopping:
            self._start_background(factory)

    @property
    def human_guid(self) -> int:
        """The human's guid: the last non-zero one from hello, login or logout (meta
        human_guid); before the bridge has named him, the stored roster's guild master (0 when
        there is none)."""
        return self._human

    def _guild_master(self) -> int:
        """Who the human is on the very first start (an empty meta), as the CLI assumes: the
        lowest guid of rank 0 on the stored roster. Without it his replayed lines would be
        stored as game chat and his whispers with strangers kept (C27)."""
        masters = sorted(g for g, m in self.store.cast().items() if m.rank == 0)
        return masters[0] if masters else 0

    @property
    def session_id(self) -> int | None:
        return self._session_id

    @property
    def model_ready(self) -> bool:
        """The silent latch: False until a check finds the live model, and again whenever one
        does not. While it is False the guild says nothing (_send); everything is still
        recorded."""
        return self._model_ready

    async def check_model(self) -> bool:
        """One health check: Ollama lists its models (LIST_MODELS_S at most) and the live model
        is among them. It moves the silent latch both ways, and logs ollama_available or
        ollama_unavailable only when the latch changes (the first check always logs one)."""
        why: dict = {}
        try:
            names = set(await asyncio.wait_for(self.llm.list_models(), LIST_MODELS_S))
        except Exception as e:  # LLMError, a timeout, anything: the model is not there for us
            self.models_seen = None
            ok, why = False, {"error": str(e) or type(e).__name__}
        else:
            self.models_seen = names
            ok = has_model(names, self.cfg.models.live)
            if not ok:
                why = {"missing": [self.cfg.models.live]}
        if not self._model_known or ok != self._model_ready:
            self._model_known, self._model_ready = True, ok
            if ok:
                log_event("ollama_available", model=self.cfg.models.live)
            else:
                log_event("ollama_unavailable", level=logging.WARNING, **why)
        return ok

    def watch_ollama_chat(self, conf: Path | None = None) -> None:
        """mod-ollama-chat's model and context against the mind's (check_ollama_chat; `conf`
        None is the server's own file). Each difference is logged as ollama_chat_mismatch when
        it first shows, and ollama_chat_matches once there is none again. startup_checks reads
        it first; from then on the health loop reads the same file every HEALTH_S, so a model
        the owner moves mod-ollama-chat to while the mind runs is warned about (liveops M6)."""
        warnings = check_ollama_chat(conf, self.cfg)
        before, self._chat_conf, self._chat_warnings = (self._chat_warnings, conf,
                                                        frozenset(warnings))
        for warning in warnings:
            if before is None or warning not in before:
                log_event("ollama_chat_mismatch", level=logging.WARNING, detail=warning)
        if before and not warnings:
            log_event("ollama_chat_matches", model=self.cfg.models.live)

    def origin_of(self, ev: Chat) -> Literal["human", "mind", "game"]:
        """human: his own line; mind: the bridge's report of a line the mind said; game:
        anyone else (playerbots' own chatter, other players)."""
        if self._human and ev.from_guid == self._human:
            return "human"
        return "mind" if ev.via_bridge else "game"

    def is_backlog(self, ev) -> bool:
        """An event the bridge buffered while no mind was connected and replays after hello."""
        ts = getattr(ev, "ts", None)
        return (self._hello_ts is not None and not isinstance(ev, (Hello, Cast))
                and type(ts) is int and ts < self._hello_ts - BACKLOG_MS)

    @staticmethod
    def _event_at(ev, now: float, backlog: bool) -> float | None:
        """When a replayed event happened: its server ts (ms; the bridge runs on this machine,
        so the same clock), never later than now. None for a live event, which keeps now
        (correctness M2)."""
        return min(now, ev.ts / 1000) if backlog else None

    def bystanders(self, t: ClosedThread, speakers: set[int]) -> list[int]:
        """Who overheard a thread (the extractor's `bystanders`): online cast members with a
        persona who could see one of its lines and spoke on any channel within the last 15
        minutes, not its speakers; the 10 who spoke most recently."""
        now = self.clock.now()
        audience: set[int] = set()
        for row in self.store.chat_lines(t.line_ids):
            audience.update(row.audience or ())
        recent = [g for g in audience
                  if g in self.world.online and g not in speakers
                  and now - self._spoke_at.get(g, -math.inf) <= BYSTANDER_S
                  and self.personas.get(g) is not None]
        recent.sort(key=lambda g: (-self._spoke_at[g], g))
        return recent[:MAX_BYSTANDERS]

    # -- small helpers -----------------------------------------------------------------------

    def _open_library(self) -> Library | None:
        """library.db beside mind.db, read-only (the library CLI builds and migrates it), with
        the app's clock, its scheduler for the query embeds and the mind's embedding model.
        Missing or unreadable, it is logged once (library_unavailable) and talk goes on without
        facts; it is never created or changed here."""
        path = self.data_dir / LIBRARY_FILE
        if not path.exists():
            log_event("library_unavailable", level=logging.WARNING, path=str(path),
                      reason="missing")
            return None
        lib = None
        try:
            lib = Library(path, clock=self.clock, llm=self.scheduler,
                          embed_model=self.cfg.models.embedding, readonly=True)
            model = lib.get_meta("embed_model")
        except Exception as e:  # not a database, another schema, a locked or broken file
            if lib is not None:
                lib.close()
            log_event("library_unavailable", level=logging.WARNING, path=str(path),
                      reason=f"{type(e).__name__}: {e}")
            return None
        log_event("library_opened", path=str(path), model=model or "-")
        if model not in (None, self.cfg.models.embedding):  # its vectors don't compare: text only
            log_event("library_embedding_mismatch", level=logging.WARNING, library=model,
                      mind=self.cfg.models.embedding)
        return lib

    def _human_guid(self) -> int:
        return self._human

    def _identity(self) -> HumanIdentity:
        return self.engine.human

    def _is_officer(self, guid: int) -> bool:
        p = self.personas.get(guid)
        return p is not None and p.play.guild_role == "officer"

    def _is_cast(self, guid: int) -> bool:
        return guid in self.world.cast_guids() or (
            guid != self._human and self.personas.get(guid) is not None)

    async def _send(self, cmd):
        """Every say and emote the engine sends, say_as's included, leaves here. While the live
        model is away it is refused (Ack ok False, "model_away"), so the guild stays silent,
        LLM line or template alike (controller ruling); the turn is dropped with that reason.
        The bridge's own pings and the other commands extensions send through bridge.send do
        not pass here."""
        if not self._model_ready:
            return Ack(ts=0, id=cmd.id, ok=False, error=MODEL_AWAY)
        return await self.bridge.send(cmd)

    def _note_human(self, guid: int) -> None:
        """Keep the last non-zero human guid (pre-flight ruling 6); the first one the bridge
        names is stored even when it is the guild master assumed until then."""
        if not guid or (guid == self._human and self._human_named):
            return
        changed, self._human, self._human_named = guid != self._human, guid, True
        self.world.human_guid = guid
        self.store.set_meta("human_guid", str(guid))
        if changed and self._human_name:  # the old character's chat name: his roster row
            self._human_name = None       # until he chats
            self.store.set_meta("human_name", "")
        self.engine.set_human(guid)
        log_event("human_guid", guid=guid)

    def _note_human_name(self, name: str) -> None:
        """The character name he chats under (ruling 6), for his identity in every prompt."""
        if name and name != self._human_name:
            self._human_name = name
            self.store.set_meta("human_name", name)
            self.engine.set_human(self._human)

    def _queue_closed(self, closed: ClosedThread | None) -> None:
        if closed is not None:
            self._extract_queue.put_nowait(closed)

    def _on_line_sent(self, line_id: int, chat: Chat) -> None:
        """The engine's hook for every line the mind said: the only way they reach threads."""
        self._spoke_at[chat.from_guid] = self.clock.now()
        self._queue_closed(self.threads.on_line_sent(line_id, chat))

    async def _call_hooks(self, kind: str, hooks: list, *args) -> None:
        for hook in list(hooks):
            try:
                await hook(*args)
            except asyncio.CancelledError:
                if _cancelling():
                    raise
                log.warning("%s_hook_cancelled: hook=%s", kind, _name(hook))
            except Exception:
                log.exception("%s_hook_failed: hook=%s", kind, _name(hook))

    async def _on_connection(self, up: bool) -> None:
        log_event("bridge_connected" if up else "bridge_disconnected")
        await self._call_hooks("connection", self._connection_hooks, up)

    # -- the dispatch ------------------------------------------------------------------------

    async def handle(self, ev: Event) -> None:
        """One bridge event: the app's row, then the handlers registered for its type."""
        if self._closed:
            return
        name = event_type(ev)
        if self._stopping:
            self._record_only(ev, name)
            return
        try:
            if await self._row(ev, name) is False:
                return  # ignored outright (C27): no handler sees it either
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("event_failed: type=%s", name)
        for handler in list(self._handlers.get(name, ())):
            try:
                await handler(ev)
            except asyncio.CancelledError:
                if _cancelling():
                    raise
                log.warning("event_handler_cancelled: type=%s handler=%s", name,
                            _name(handler))
            except Exception:
                log.exception("event_handler_failed: type=%s handler=%s", name,
                              _name(handler))

    async def _row(self, ev: Event, name: str) -> bool | None:
        """The app's own handling. False: the event is ignored outright."""
        now = self.clock.now()
        backlog = self.is_backlog(ev)
        if isinstance(ev, Hello):
            await self._on_hello(ev, now)
        elif isinstance(ev, Cast):
            self.store.upsert_cast(ev.members, now)
            self.world.apply(ev, now)
        elif isinstance(ev, Chat):
            return await self._on_chat(ev, now, backlog)
        elif isinstance(ev, (Login, Logout)) and ev.human:
            await self._on_human_presence(ev, now, backlog)
        elif isinstance(ev, (AfkEvent, TextEmote)):
            if not getattr(ev, "via_bridge", False):  # C5: the mind's own emotes, as echoes
                self.store.insert_game_event(ev, session_id=self._session_id,
                                             ts=self._event_at(ev, now, backlog))
                self.world.apply(ev, now)
        elif isinstance(ev, Error):
            log.warning("bridge_error: message=%r", ev.message)
        elif name not in CORE_EVENT_TYPES:
            self.store.insert_game_event(ev, session_id=self._session_id,
                                         ts=self._event_at(ev, now, backlog))
        else:
            at = self._event_at(ev, now, backlog)  # a replayed one is stored and remembered then
            event_id = self.store.insert_game_event(ev, session_id=self._session_id, ts=at)
            self.world.apply(ev, now)
            if isinstance(ev, Group):
                self.raid.on_group(ev)
            elif isinstance(ev, Boss):
                self.raid.on_boss(ev, now)
            if isinstance(ev, _MEMORY_EVENTS):
                self._memory_queue.put_nowait((ev, event_id, at))
            if not backlog:
                await self.engine.on_event(ev, now)

    async def _on_hello(self, ev: Hello, now: float) -> None:
        self._hello_ts = ev.ts
        if ev.guild_id != GUILD_ID:
            log.warning("guild_id_unexpected: guild_id=%d expected=%d", ev.guild_id, GUILD_ID)
        self.store.set_meta("guild_id", str(ev.guild_id))
        self._note_human(ev.human_guid)
        self.world.apply(ev, now)
        log_event("bridge_hello", server=ev.server, version=ev.version,
                  human_online=bool(ev.human_guid))
        if ev.human_guid:
            if self._session_id is None:
                left = self.store.open_session()  # a crash or restart left it open
                if left is None:
                    await self._start_session(now)
                elif now - (last := self.store.session_last_activity(left)) <= RESUME_S:
                    await self._resume_session(left)  # he is still on, in the same evening
                else:  # a session from another evening: closed where it went quiet
                    self.store.end_session(left, last)
                    log_event("session_ended", session=left, stale=True)
                    await self._start_session(now)
        elif (sid := self._session_id or self.store.open_session()) is not None:
            # he left while we were away: the session ends where it went quiet
            await self._end_session(now, at=self.store.session_last_activity(sid))

    async def _on_human_presence(self, ev: Login | Logout, now: float, backlog: bool) -> None:
        """His login opens a session (one already open stays: a repeated login is idempotent)
        and is greeted; his logout ends it. Replayed ones are only recorded: the hello said
        where he is now."""
        self._note_human(ev.guid)
        login = isinstance(ev, Login)
        if login and not backlog and self._session_id is None:
            await self._start_session(now)
        self.store.insert_game_event(ev, session_id=self._session_id,
                                     ts=self._event_at(ev, now, backlog))
        self.world.apply(ev, now)
        if backlog:
            return
        await self.engine.on_event(ev, now)  # the greetings, at most once per login stretch
        if not login and self._session_id is not None:
            await self._end_session(now)

    async def _on_chat(self, ev: Chat, now: float, backlog: bool) -> bool:
        """False for a whisper with nobody from the cast on either side (C27): the bridge
        publishes the human's private whispers only as a side effect, so they are ignored
        outright, handlers included. An echo still goes on to the handlers."""
        if self.engine.is_echo(ev, now):  # the mind's own line: stored on its ack
            return True
        if ev.channel == "whisper" and not (self._is_cast(ev.from_guid)
                                            or self._is_cast(ev.to_guid)):
            return False  # C27: his whispers with anyone outside the cast stay his
        origin = self.origin_of(ev)
        audience = self.world.audience(ev.channel, from_guid=ev.from_guid, to_guid=ev.to_guid,
                                       zone=ev.zone)
        at = self._event_at(ev, now, backlog)
        line_id = self.store.insert_chat_line(ev, session_id=self._session_id, origin=origin,
                                              audience=audience, ts=at)
        if not backlog:
            self._spoke_at[ev.from_guid] = now
        if origin != "human":
            return True  # game lines are kept in the log only
        self._note_human_name(ev.from_name)
        if ev.channel in COMMAND_CHANNELS and looks_like_bot_command(ev.text):
            self.world.apply(ev, now)  # he is active, but a bot command is not talk
            return True
        row = self.store.chat_lines([line_id])[0]
        self.world.add_line(row, now)
        # A replayed line is threaded at the time he typed it (the server's clock, the same
        # machine's), so lines minutes apart are separate conversations.
        self._queue_closed(self.threads.add(line_id, ev, now if at is None else at))
        if not backlog:
            await self.engine.on_human_chat(row)
        return True

    def _record_only(self, ev: Event, name: str) -> None:
        """While stopping: keep what still arrives in the log and start nothing."""
        try:
            at = self._event_at(ev, self.clock.now(), self.is_backlog(ev))
            if isinstance(ev, Chat):
                if ev.via_bridge or (ev.channel == "whisper" and not (
                        self._is_cast(ev.from_guid) or self._is_cast(ev.to_guid))):
                    return
                self.store.insert_chat_line(
                    ev, session_id=self._session_id, origin=self.origin_of(ev),
                    audience=self.world.audience(ev.channel, from_guid=ev.from_guid,
                                                 to_guid=ev.to_guid, zone=ev.zone), ts=at)
            elif not isinstance(ev, (Hello, Cast, Error)) and not getattr(ev, "via_bridge", False):
                self.store.insert_game_event(ev, session_id=self._session_id, ts=at)
        except Exception:
            log.exception("event_failed: type=%s", name)

    # -- sessions ----------------------------------------------------------------------------

    def _reload_config(self) -> bool:
        """mind.toml again; one that cannot be read keeps the running config. True when the
        live model changed."""
        try:
            cfg = load_config(self.data_dir / "mind.toml")
        except ConfigError as e:
            log.warning("config_invalid: kept the running config: %s", e)
            return False
        changed = cfg.models.live != self.cfg.models.live
        self.cfg = cfg
        self.engine.apply_config(cfg)
        return changed

    async def _start_session(self, now: float) -> None:
        """His session starts: mind.toml is read again (chattiness, the dials, the live model,
        his names), personas are reloaded from mind.db, and the session is open before anything
        is awaited: an earlier session's offline hooks, whose waits may end meanwhile, must see
        him back. Then, when the reload changed the live model or no check has run yet, the
        silent latch is checked against it (Task 17 re-review N3), awaited, so the greetings
        planned next meet the right latch, not a stale one for up to a minute."""
        model_changed = self._reload_config()
        self.personas.invalidate()
        sid = self.store.start_session(now)
        self._session_id = sid
        await self.engine.start_session(sid)
        if model_changed or not self._model_known:
            await self.check_model()
        log_event("session_started", session=sid)

    async def _resume_session(self, sid: int) -> None:
        """A session a crash or restart left open while he stayed online: no greeting."""
        self._session_id = sid
        self.personas.invalidate()
        await self.engine.start_session(sid)
        log_event("session_resumed", session=sid)

    async def _end_session(self, now: float, *, at: float | None = None) -> None:
        """His threads go to extraction, the activity summaries are queued after the event
        memories still waiting, the engine and the store end the session (at `at`, else now),
        then the offline hooks run once those last writes are in (_after_session). A session
        that ends once stop() has begun (the engine's wait can outlast a Ctrl+C) starts no
        hooks: the shutdown is closing what they would read (correctness M7)."""
        sid = self._session_id if self._session_id is not None else self.store.open_session()
        if sid is None:
            return
        for closed in self.threads.flush_all():
            self._extract_queue.put_nowait(closed)
        flushed = _ActivityFlush()
        self._memory_queue.put_nowait(flushed)
        await self.engine.end_session()
        self.store.end_session(sid, now if at is None else at)
        self._session_id = None
        log_event("session_ended", session=sid)
        if self._stopping:
            log_event("offline_hooks_skipped", reason="stopping", session=sid)
            return
        task = asyncio.get_running_loop().create_task(self._after_session(flushed, sid),
                                                      name="mind-offline-hooks")
        self._offline_tasks.add(task)
        task.add_done_callback(self._offline_tasks.discard)

    async def _after_session(self, flushed: "_ActivityFlush", sid: int) -> None:
        """The offline hooks, off the dispatcher, once the session's threads are extracted and
        its activity summaries written (OFFLINE_WAIT_S at most), so a night job starting from
        them never finds a thread still queued here. They run once per quiet period, one run at
        a time: skipped if he is back by then (a quick relog: a new session is open; Task 17
        re-review N1) or if a later session has ended too (that one's run covers both), and a
        run waits for the one before it to end (part F review, Minor 2)."""
        waits = []
        if self._worker_running("extraction"):
            waits.append(self._extract_queue.join())
        if self._worker_running("memories"):
            waits.append(flushed.done.wait())
        if waits:
            try:
                await asyncio.wait_for(asyncio.gather(*waits), OFFLINE_WAIT_S)
            except TimeoutError:
                log_event("offline_hooks_late", level=logging.WARNING, waited=OFFLINE_WAIT_S)
        async with self._offline_lock:
            if self._session_id is not None:
                log_event("offline_hooks_skipped", reason="new_session",
                          session=self._session_id)
                return
            newest = self.store.latest_session()
            if newest is not None and newest > sid:
                log_event("offline_hooks_skipped", reason="newer_session", session=sid,
                          newer=newest)
                return
            await self._call_hooks("offline", self._offline_hooks)

    def _worker_running(self, name: str) -> bool:
        task = self._loops.get(name)
        return task is not None and not task.done()

    # -- running -----------------------------------------------------------------------------

    async def run(self) -> None:
        """Start the bridge, the ticker and the extraction and memory workers, then the
        registered background tasks, and serve until stop(). A cancel (Ctrl+C under
        asyncio.run) runs stop() to the end and is then passed on. A loop of the app's own
        that ends by itself is a bug: the app stops and raises."""
        if self._started:
            raise RuntimeError("MindApp.run() runs once")
        self._started = True
        died = None
        try:
            if not self._stopping:
                self._start_loops()
                died = await self._serve()
        finally:
            await self._finish_stop()
        if died is not None:
            raise RuntimeError(f"the mind's {died} loop stopped")

    def _start_loops(self) -> None:
        loop = asyncio.get_running_loop()
        self.running = True
        self._loops = {
            "bridge": loop.create_task(self.bridge.run(), name="mind-bridge"),
            "ticker": loop.create_task(self._ticker(), name="mind-ticker"),
            "extraction": loop.create_task(self.extractor.run(self._extract_queue),
                                           name="mind-extraction"),
            "memories": loop.create_task(self._memory_worker(), name="mind-memories"),
            "health": loop.create_task(self._health(), name="mind-health"),
        }
        self._loops["stats"] = loop.create_task(self._stats(), name="mind-stats")
        self._banner_task = loop.create_task(self._banner(), name="mind-banner")
        for factory in self._background:
            self._start_background(factory)
        log_event("mind_started", bridge=f"{self.cfg.bridge.host}:{self.cfg.bridge.port}",
                  data_dir=str(self.data_dir), personas=len(self.store.persona_versions()),
                  code=str(Path(__file__).resolve().parent),  # which checkout runs
                  version=code_version())

    def _start_background(self, factory: Callable[[], Awaitable[None]]) -> None:
        async def guarded() -> None:
            try:
                await factory()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("background_failed: factory=%s", _name(factory))
        self._bg_tasks.append(asyncio.get_running_loop().create_task(
            guarded(), name=f"mind-background-{_name(factory)}"))

    async def _serve(self) -> str | None:
        """Wait for stop(); the name of one of the app's loops if it ended first."""
        stop = asyncio.ensure_future(self._stop_requested.wait())
        try:
            done, _ = await asyncio.wait({stop, *self._loops.values()},
                                         return_when=asyncio.FIRST_COMPLETED)
        finally:
            stop.cancel()
        if self._stopping:  # stop() may already be cancelling the loops
            return None
        for name, task in self._loops.items():
            if task in done:
                exc = None if task.cancelled() else task.exception()
                log.error("loop_stopped: loop=%s", name, exc_info=exc)
                return name
        return None

    async def _ticker(self) -> None:
        """Every second on the clock: the engine's tick, then the threads that fell silent go
        to extraction; every 10 s the memories without a vector are embedded (a task of its
        own, so a slow model never holds up the tick)."""
        last_embed = self.clock.now()
        while True:
            await self.clock.sleep(TICK_S)
            now = self.clock.now()
            try:
                await self.engine.tick(now)
            except Exception:
                log.exception("tick_failed")
            try:
                for closed in self.threads.expire(now):
                    self._extract_queue.put_nowait(closed)
            except Exception:
                log.exception("thread_expiry_failed")
            if now - last_embed >= EMBED_EVERY_S:
                last_embed = now
                if self._embed_task is None or self._embed_task.done():
                    self._embed_task = asyncio.get_running_loop().create_task(
                        self._embed_pending(), name="mind-embed")

    async def _embed_pending(self) -> None:
        try:
            await self.writer.embed_pending()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("embed_backfill_failed")

    async def _health(self) -> None:
        """The silent latch's recheck: now (unless startup_checks has just checked), then
        every HEALTH_S on the clock. An outage mid-run silences the guild within one check, and
        the next check that finds the model lifts it. Each check also reads mod-ollama-chat's
        conf again (when startup_checks read one) and looks for a pile of model drops
        (liveops M6): a listed model can still be too slow to answer in time."""
        if self._model_known:
            await self.clock.sleep(HEALTH_S)
        while True:
            await self.check_model()
            try:
                if self._chat_warnings is not None:
                    self.watch_ollama_chat(self._chat_conf)
            except Exception:
                log.exception("health_check_failed: check=ollama_chat")
            try:
                self._check_drops(self.clock.now())
            except Exception:
                log.exception("health_check_failed: check=drops")
            await self.clock.sleep(HEALTH_S)

    def _check_drops(self, now: float) -> None:
        """drops_high when DROPS_HIGH turns were dropped as expired or llm_error (MODEL_DROPS)
        within DROPS_WINDOW_S of each other since the last check, at most once per
        DROPS_WARN_S: GPU contention or a model reload, which the latch does not see."""
        if now - self._drops_warned_at < DROPS_WARN_S:
            return
        drops = self.store.drops_since(now - HEALTH_S - DROPS_WINDOW_S, MODEL_DROPS)
        lo, best = 0, (0, 0)
        for hi, (at, _) in enumerate(drops):
            while at - drops[lo][0] > DROPS_WINDOW_S:
                lo += 1
            if hi + 1 - lo > best[1] - best[0]:
                best = (lo, hi + 1)
        burst = [why for _, why in drops[best[0]:best[1]]]
        if len(burst) >= DROPS_HIGH:
            self._drops_warned_at = now
            by_reason = {why: burst.count(why) for why in MODEL_DROPS}
            log_event("drops_high", level=logging.WARNING, drops=len(burst),
                      window_s=int(DROPS_WINDOW_S), **by_reason)

    async def _stats(self) -> None:
        """Every STATS_S on the clock, one stats line (_log_stats). The window still open when
        the mind stops gets its own line from _shutdown."""
        self._stats_since = self.clock.now()
        while True:
            await self.clock.sleep(STATS_S)
            self._log_stats(self.clock.now())

    def _log_stats(self, now: float, *, final: bool = False) -> None:
        """One stats line for the window from the last one to `now`: the lines said, how many
        answered his lines (ANSWERS_HIS_LINES), his own lines with his bot commands counted
        apart, the drops by reason, the model's time per turn (median and max) and the queues'
        depths (Store.talk_stats, Store.human_lines); `final` marks the stop's short last
        window. Counts only, never chat text. A line that fails is logged and the next one
        comes as usual."""
        since = self._stats_since
        if since is None:
            return
        try:
            talk = self.store.talk_stats(since, now)
            said, llm = talk["said"], talk["llm_s"]
            his = self.store.human_lines(since, now)
            commands = sum(channel in COMMAND_CHANNELS and looks_like_bot_command(text)
                           for channel, text in his)  # the dispatch's own rule
            log_event("stats", window_s=round(now - since), said=sum(said.values()),
                      replies=sum(n for kind, n in said.items() if kind in ANSWERS_HIS_LINES),
                      his_lines=len(his) - commands, his_commands=commands,
                      drops=talk["drops"],
                      llm_p50_s=round(statistics.median(llm), 2) if llm else None,
                      llm_max_s=round(max(llm), 2) if llm else None,
                      queue={"llm": sum(self.scheduler.pending().values()),
                             "extract": self._extract_queue.qsize(),
                             "memory": self._memory_queue.qsize(),
                             "bridge": self.bridge.queued},
                      final=final)
        except Exception:
            log.exception("stats_failed")
        self._stats_since = now

    async def _banner(self) -> None:
        """One console line per start, once the bridge has greeted the mind or BANNER_WAIT_S
        has passed on the clock: the data dir, the personas, the live model and whether a
        check found it, the bridge and whether it connected (liveops M5). The log gets it too.
        No name of the human's is in it."""
        try:
            greeted = asyncio.ensure_future(self.bridge.wait_greeted())
            timer = asyncio.ensure_future(self.clock.sleep(BANNER_WAIT_S))
            try:
                await asyncio.wait({greeted, timer}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                greeted.cancel()
                timer.cancel()
            b, where = self.bridge, f"{self.cfg.bridge.host}:{self.cfg.bridge.port}"
            state = ("connected" if b.greeted else "refused: another mind holds it"
                     if b.refused else "not connected yet, still trying")
            personas, model = len(self.store.persona_versions()), self.cfg.models.live
            text = (f"mind running: data dir {self.data_dir}, {personas} personas, live model "
                    f"{model} ({'found' if self.model_ready else 'not found: silent until it is'})"
                    f", bridge {where} {state}")
            log_event("banner", data_dir=str(self.data_dir), personas=personas,
                      live_model=model, model_ready=self.model_ready, bridge=where,
                      connected=b.greeted)
            try:
                print(text, flush=True)
            except UnicodeEncodeError:  # a console that cannot show the data dir's name
                print(text.encode("ascii", "backslashreplace").decode("ascii"), flush=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("banner_failed")

    async def _memory_worker(self) -> None:
        """Event memories in arrival order, off the dispatcher (their writes wait on the
        embedder), and the activity summaries where a session ended."""
        while True:
            item = await self._memory_queue.get()
            try:
                if isinstance(item, _ActivityFlush):
                    try:
                        await self.event_rules.activity.flush(self.writer, self.clock.now())
                    finally:
                        item.done.set()
                else:
                    await self.event_rules.on_event(*item)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("event_memory_failed")
            finally:
                self._memory_queue.task_done()

    # -- stopping ----------------------------------------------------------------------------

    async def stop(self) -> None:
        """Shut down in order (see _shutdown). Idempotent and safe at any time: before run(),
        while it runs, twice at once. A caller cancelled meanwhile leaves the shutdown going."""
        await asyncio.shield(self._begin_stop())

    def _begin_stop(self) -> asyncio.Task:
        if self._stop_task is None:
            self._stopping = True
            self._stop_requested.set()
            self._stop_task = asyncio.ensure_future(self._shutdown())
        return self._stop_task

    async def _finish_stop(self) -> None:
        """stop(), seen through to the end even if run() is cancelled again meanwhile; the
        cancel is passed on afterwards."""
        task = self._begin_stop()
        interrupted = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                interrupted = True
        if interrupted:
            raise asyncio.CancelledError
        task.result()

    async def _step(self, name: str, work) -> None:
        try:
            await work
        except Exception:
            log.exception("shutdown_step_failed: step=%s", name)

    async def _shutdown(self) -> None:
        """1. the extensions' background tasks and any offline hooks still waiting, the ticker
        (with its embed backfill), the health check, the stats loop and the start banner, then
        the pending turns (engine.end_session) and the stats line of the window still open; 2.
        every open thread to extraction, within extract_budget_s, and whatever is left stays
        'pending' for P1c; 3. the queued event memories and the activity summaries; 4. the open
        session ends; 5. checkpoint, then the bridge (its queued events are recorded), any
        offline hooks begun since step 1, and the database close."""
        log_event("shutdown_started")
        await _cancel_wait([*self._bg_tasks, *self._offline_tasks])
        await _cancel_wait([self._loops.get("ticker"), self._embed_task, self._loops.get("health")])
        await _cancel_wait([self._loops.get("stats"), self._banner_task])
        await self._step("turns", self.engine.end_session())
        if self._stats_since is not None and self.clock.now() > self._stats_since:
            self._log_stats(self.clock.now(), final=True)
        await self._step("threads", self._flush_threads())
        await self._step("activity", self._flush_memories())
        try:
            if self._session_id is not None:
                self.store.end_session(self._session_id, self.clock.now())
                log_event("session_ended", session=self._session_id)
                self._session_id = None
        except Exception:
            log.exception("shutdown_step_failed: step=session")
        await self._step("llm", self.scheduler.close())
        try:
            checkpoint(self.store.conn)
        except Exception:
            log.exception("shutdown_step_failed: step=checkpoint")
        await self._step("bridge", self.bridge.close())
        await _cancel_wait([self._loops.get("bridge")])
        await _cancel_wait(list(self._offline_tasks))  # any begun after step 1 took its list
        self._closed = True
        self.running = False
        try:
            self.store.close()
        except Exception:
            log.exception("shutdown_step_failed: step=db")
        try:
            if self.library is not None:
                self.library.close()
        except Exception:
            log.exception("shutdown_step_failed: step=library")
        log_event("shutdown_complete")

    async def _flush_threads(self) -> None:
        for closed in self.threads.flush_all():
            self._extract_queue.put_nowait(closed)
        worker = self._loops.get("extraction")
        if worker is not None and not worker.done():
            try:
                await asyncio.wait_for(self._extract_queue.join(), self.extract_budget_s)
            except TimeoutError:
                log_event("extraction_budget_spent", level=logging.WARNING,
                          left=self._extract_queue.qsize())
        await _cancel_wait([worker])
        left = []
        while not self._extract_queue.empty():
            left.append(self._extract_queue.get_nowait())
        now = self.clock.now()
        for closed in left:  # a thread cut off mid-extraction is still 'pending' as well
            if self.store.thread_status(closed.thread_id) not in ("extracted", "skipped",
                                                                  "failed"):
                self.store.set_thread_status(closed.thread_id, now, "pending")
        if left:
            log_event("threads_left_pending", threads=[t.thread_id for t in left])

    async def _flush_memories(self) -> None:
        self._memory_queue.put_nowait(_ActivityFlush())
        worker = self._loops.get("memories")
        if worker is not None and not worker.done():
            try:
                await asyncio.wait_for(self._memory_queue.join(), self.activity_budget_s)
            except TimeoutError:
                log_event("memory_budget_spent", level=logging.WARNING,
                          left=self._memory_queue.qsize())
        await _cancel_wait([worker])
