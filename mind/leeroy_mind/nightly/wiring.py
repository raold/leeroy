"""Attach nightly work without starting tasks or sending bridge commands."""
import logging
from dataclasses import replace

from leeroy_mind.config import ConfigError, load_config
from leeroy_mind.human import human_identity
from leeroy_mind.llm.client import OllamaClient
from leeroy_mind.logs import log
from leeroy_mind.memory.threads import ClosedThread
from leeroy_mind.protocol import Hello
from leeroy_mind.nightly.attraction import LiveAttraction
from leeroy_mind.nightly.gpu import GpuArbiter, HttpOllamaAdmin, NullAdmin
from leeroy_mind.nightly.llm import Reasoner
from leeroy_mind.nightly.pipeline import STAGES
from leeroy_mind.nightly.runner import NightContext, NightRunner
from leeroy_mind.nightly.scheduler import NightScheduler
from leeroy_mind.nightly.settings import load_settings


def attach(app):
    startup = app.cfg
    cfg = startup
    settings = load_settings(cfg, app.data_dir)
    admin = HttpOllamaAdmin(startup.ollama.url) if isinstance(app.llm, OllamaClient) else NullAdmin()
    arbiter = GpuArbiter(admin, settings)
    restart_warned = None

    def reload():
        nonlocal cfg, settings, restart_warned
        try:
            path = app.data_dir / "mind.toml"
            candidate = load_config(path) if path.exists() else app.cfg
            # The app owns the open client and embedding space for its whole lifetime.
            # Runtime dials and live/reflection models can change; these require restart.
            changed = (candidate.ollama.url, candidate.models.embedding)
            original = (startup.ollama.url, startup.models.embedding)
            if changed != original and changed != restart_warned:
                log("nightly_config_restart_required", level=logging.WARNING,
                    fields=["ollama.url", "models.embedding"])
                restart_warned = changed
            candidate = replace(candidate,
                ollama=replace(candidate.ollama, url=startup.ollama.url),
                models=replace(candidate.models, embedding=startup.models.embedding))
            updated = load_settings(candidate, app.data_dir)
        except ConfigError:
            # Config text may contain private identity values: log no raw values.
            log("nightly_config_error", level=logging.WARNING)
            return
        arbiter.reconfigure(updated)
        cfg, settings = candidate, updated
        # Extraction shares the app's dynamic model callable. Keep it and live talk
        # coherent with this admission instead of reloading a retired model afterward.
        if candidate.models.live != app.cfg.models.live:
            app._model_known = app._model_ready = False
        app.cfg = candidate
        app.engine.apply_config(candidate)
        scheduler.settings = updated

    def identity():
        stored = app.store.get_meta("human_guid")
        guid = int(stored) if stored and stored.isdigit() and int(stored) else min(
            (m.guid for m in app.store.cast().values() if m.rank == 0), default=0)
        return human_identity(cfg, app.store, guid)

    async def extract_thread(tid):
        with app.store.transaction():
            row = app.store.conn.execute("SELECT key,status FROM chat_thread WHERE id=?", (tid,)).fetchone()
            ids = tuple(r[0] for r in app.store.conn.execute(
                "SELECT id FROM chat_line WHERE thread_id=? ORDER BY ts,id", (tid,)))
        if row is None:
            raise ValueError("pending chat thread disappeared")
        if row["status"] in ("pending", "failed"):
            await app.extractor.extract(ClosedThread(tid, row["key"], ids, "flush"))

    def make_ctx(night, window):
        reload()
        ident = identity()
        # Freeze each run's settings and identity, including its later scratch copy.
        run_cfg, run_settings = cfg, settings

        async def asker():
            from leeroy_mind.eval.asker import ScratchAsker
            return await ScratchAsker.open(cfg=run_cfg, data_dir=app.data_dir,
                store=app.store, llm=app.llm, ident=ident, clock=app.clock, label=f"eval-{night}")

        return NightContext(store=app.store, night=night, window=window,
            settings=run_settings, reasoner=Reasoner(app.llm, run_settings.reflection_model,
                parallel=run_settings.parallel), embedder=app.embedder, writer=app.writer,
            arbiter=arbiter, clock=app.clock, ident=ident, personas=app.personas,
            asker=asker, extract_thread=extract_thread)

    runner = NightRunner(STAGES, make_ctx)
    scheduler = NightScheduler(app.store, settings, arbiter, runner.run, clock=app.clock)
    live = LiveAttraction(app.store, app.personas, app.clock, identity)

    async def live_ready():
        await scheduler.handback()
        # Voice may have acquired the first lease, leaving the human's repeated
        # lease with no scheduler-owned handback of its own.
        voice = getattr(app, "voice", None)
        if voice is not None:
            await voice.handback()

    app.engine.live_admission = live_ready

    async def presence(ev):
        if app.is_backlog(ev) or not (isinstance(ev, Hello) or getattr(ev, "human", False)):
            return
        # This short callback precedes core session startup/model checks. Reconfiguration
        # does no I/O; the scheduler takes its lease and cancels the night immediately.
        await scheduler.on_event(ev)
        # on_event has no suspending I/O: even its release coroutine is inline. The
        # old run is stopped before reconfiguration, and handback cannot start yet.
        reload()

    def current(fn):
        async def handler(ev):
            if not app.is_backlog(ev):
                await fn(ev)
        return handler

    for name in ("hello", "login", "logout"):
        app.register_event_handler(name, presence, before=True)
    for name, fn in (("chat", live.on_chat), ("group", live.on_group),
                     ("boss", live.on_boss), ("logout", live.on_logout), ("hello", live.on_hello)):
        app.register_event_handler(name, current(fn))
    app.register_connection_hook(scheduler.on_connection)
    app.arbiter = arbiter
    app.register_background(scheduler.run_forever)
    return scheduler
