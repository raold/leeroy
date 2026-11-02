"""Offline nightly tools: copied databases by default, exclusive live mutations.

Read-only commands never migrate. A copied database isolates writes, not the GPU:
real Ollama work also holds the service lock so a trial cannot unload live chat.
"""
import argparse
import asyncio
import inspect
import json
import math
import re
import sqlite3
import sys
from contextlib import ExitStack, asynccontextmanager, contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

from leeroy_mind.app import AlreadyRunning, acquire_instance_lock, instance_lock_held
from leeroy_mind.clock import SystemClock, night_of
from leeroy_mind.config import default_data_dir, load_config
from leeroy_mind.human import human_identity
from leeroy_mind.nightly.ground import screen_text
from leeroy_mind.nightly.settings import iso_week, load_settings, night_bounds, week_bounds
from leeroy_mind.storage.db import open_db, snapshot
from leeroy_mind.storage.store import Store
from leeroy_mind.text import strip_wow_markup


def _date(value):
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("invalid calendar date") from exc
    return value


def _week(value):
    if not re.fullmatch(r"[0-9]{4}-W[0-9]{2}", value):
        raise argparse.ArgumentTypeError("expected YYYY-Www")
    try:
        date.fromisocalendar(int(value[:4]), int(value[6:]), 1)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("invalid ISO week") from exc
    return value


def _positive(value):
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected a positive integer") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError("expected a positive integer")
    return number


def _data_arg(parser, *, child=False):
    parser.add_argument("--data-dir", default=argparse.SUPPRESS if child else None,
                        help="directory containing mind.toml and mind.db")


def _nightly_parser():
    parser = argparse.ArgumentParser(prog="leeroy_mind nightly")
    _data_arg(parser)
    subs = parser.add_subparsers(dest="action", required=True)
    for name in ("status", "run", "ledger", "undo", "enrich", "veto"):
        sub = subs.add_parser(name)
        _data_arg(sub, child=True)
        if name in ("status", "run"):
            sub.add_argument("--night", type=_date)
        if name == "run":
            sub.add_argument("--live-db", action="store_true")
        elif name == "ledger":
            sub.add_argument("--guid", type=_positive)
            sub.add_argument("--since", help="YYYY-MM-DD night or Unix timestamp")
        elif name in ("undo", "veto"):
            sub.add_argument("id", type=_positive)
        elif name == "enrich":
            sub.add_argument("--dry-run", action="store_true")
            sub.add_argument("--limit", type=_positive)
            sub.add_argument("--guid", type=_positive)
    return parser


def _root(args):
    root = Path(args.data_dir).resolve() if args.data_dir else default_data_dir().resolve()
    # Reading the files (rather than making defaults) keeps a typo from creating a mind.
    (root / "mind.toml").read_bytes()
    if not (root / "mind.db").is_file():
        raise ValueError("mind.db is missing; pass the existing mind data directory")
    return root


@contextmanager
def _read_store(path, clock):
    conn = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    store = Store(conn, clock=clock)
    try:
        yield store
    finally:
        # Store.close() checkpoints/truncates WAL and is intentionally for writers.
        # A read-only connection must never attempt that write on the live database.
        conn.close()


@contextmanager
def _exclusive(root, *, gpu=False):
    message = "mind service is running"
    if gpu:
        message += "; a trial database still shares Ollama with live chat"
    if instance_lock_held(root):
        raise AlreadyRunning(message)
    try:
        lock = acquire_instance_lock(root)
    except AlreadyRunning:
        raise AlreadyRunning(message) from None
    with lock:
        yield


def _child(root, name):
    child = root / name
    if child.is_symlink() or child.resolve().parent != root.resolve():
        raise ValueError("trial path must remain inside the data directory")
    return child


def _trial(root, clock, label, filename=None):
    scratch = _child(root, "scratch")
    target = _child(scratch, filename) if filename else None
    if target is not None and target.exists():
        if not target.is_file():
            raise ValueError("trial database path is not a file")
        return target  # Resume this trial's own committed units, never overwrite them.
    with _read_store(root / "mind.db", clock) as source:
        copied = snapshot(source.conn, scratch, label, now=clock.now())
    if target is not None:
        copied.rename(target)
        return target
    return copied


@contextmanager
def _working_store(root, clock, *, live, label, filename=None):
    with ExitStack() as stack:
        if live:
            stack.enter_context(_exclusive(root))
            path = root / "mind.db"
        else:
            # Separate trial locks prevent two CLI invocations from writing the same
            # fixed trial while leaving the live service free to use its own database.
            locks = _child(_child(root, "scratch"), "locks")
            lock_dir = _child(locks, label)
            stack.enter_context(acquire_instance_lock(lock_dir))
            path = _trial(root, clock, label, filename)
        store = Store(open_db(path), clock=clock)
        try:
            yield store, path
        finally:
            store.close()


def _identity(cfg, store):
    cast = store.cast()
    raw = store.get_meta("human_guid")
    if raw and raw.isdigit() and int(raw) > 0:
        guid = int(raw)
    else:
        masters = sorted(m.guid for m in cast.values() if m.rank == 0 and m.guid > 0)
        if len(masters) != 1:
            raise ValueError("cannot identify the human from the stored roster")
        guid = masters[0]
    if guid not in cast:
        raise ValueError("stored human is absent from the roster")
    return human_identity(cfg, store, guid)


def _screen(value, ident):
    if isinstance(value, str):
        raw = screen_text(re.sub(r"\|([TH])", r"|\1 ", value), ident)
        return screen_text(" ".join(strip_wow_markup(raw).split()), ident)
    if isinstance(value, dict):
        return {str(k): _screen(v, ident) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_screen(v, ident) for v in value]
    return value


def _print(value, ident=None):
    print(json.dumps(_screen(value, ident) if ident is not None else value,
                     ensure_ascii=False, indent=2, default=str))


def _status(store, night):
    if night is None:
        row = store.conn.execute("SELECT night FROM night_run ORDER BY night DESC LIMIT 1").fetchone()
        night = row[0] if row else None
    run = store.conn.execute("SELECT * FROM night_run WHERE night=?", (night,)).fetchone()
    if run is None:
        return {"night": night, "status": "not_run", "stages": [], "units": {}, "grounding": {}}
    record = dict(run)
    try:
        record["plan"] = json.loads(record["plan"])
    except (ValueError, TypeError):
        record["plan"] = {"error": "invalid stored plan"}
    stages = []
    for r in store.conn.execute("SELECT * FROM night_stage WHERE night=? ORDER BY started,stage", (night,)):
        row = dict(r)
        row["duration_s"] = None if row["finished"] is None else max(0, row["finished"] - row["started"])
        stages.append(row)
    units = {r[0]: r[1] for r in store.conn.execute(
        "SELECT stage,count(*) FROM night_step WHERE night=? GROUP BY stage ORDER BY stage", (night,))}
    grounding = {}
    for stage, verdict, n in store.conn.execute("SELECT stage,verdict,count(*) FROM grounding_log "
                                               "WHERE night=? GROUP BY stage,verdict", (night,)):
        grounding.setdefault(stage, {"accepted": 0, "rejected": 0})[verdict] = n
    return {"run": record, "stages": stages, "units": units, "grounding": grounding}


class _NoLLM:
    async def chat(self, *args, **kwargs):
        raise RuntimeError("models are disabled for this evaluation")

    async def embed(self, *args, **kwargs):
        raise RuntimeError("models are disabled for this evaluation")

    async def list_models(self):
        raise RuntimeError("models are disabled for this evaluation")


def _make_client(cfg, make_llm):
    if make_llm is None:
        from leeroy_mind.llm.client import OllamaClient
        return OllamaClient(cfg.ollama.url)
    try:
        signature = inspect.signature(make_llm)
    except (TypeError, ValueError):
        return make_llm(cfg)
    try:
        signature.bind(cfg)
    except TypeError:
        signature.bind()
        return make_llm()
    return make_llm(cfg)


async def _close_client(llm):
    close = getattr(llm, "aclose", None) or getattr(llm, "close", None)
    if close is not None:
        result = close()
        if inspect.isawaitable(result):
            await result


@asynccontextmanager
async def _context(root, store, cfg, clock, night, *, make_llm, use_llm=True, live_locked=False):
    from leeroy_mind.llm.client import OllamaClient
    from leeroy_mind.llm.scheduler import LLMScheduler
    from leeroy_mind.memory.extract import Extractor
    from leeroy_mind.memory.threads import ClosedThread
    from leeroy_mind.memory.writer import Embedder, MemoryWriter
    from leeroy_mind.nightly.gpu import GpuArbiter, HttpOllamaAdmin, NullAdmin
    from leeroy_mind.nightly.llm import Reasoner
    from leeroy_mind.nightly.runner import NightContext
    from leeroy_mind.personas.io import PersonaCache
    ident, settings = _identity(cfg, store), load_settings(cfg, root)
    llm = _make_client(cfg, make_llm) if use_llm else _NoLLM()
    scheduler = None
    client_closed = False
    try:
        with ExitStack() as stack:
            real = isinstance(llm, OllamaClient)
            if real and not live_locked:
                stack.enter_context(_exclusive(root, gpu=True))
            admin = HttpOllamaAdmin(cfg.ollama.url) if real else NullAdmin()
            scheduler = LLMScheduler(llm, clock=clock)
            embedder = Embedder(scheduler, cfg.models.embedding)
            writer = MemoryWriter(store, embedder, clock, human=lambda: ident)
            ctx = NightContext(store, night, night_bounds(night, clock.local().tzinfo), settings,
                Reasoner(llm, cfg.models.reflection, parallel=settings.parallel), embedder, writer,
                GpuArbiter(admin, settings), clock, ident, PersonaCache(store))
            extractor = Extractor(store, scheduler, writer, cfg.models.live,
                human_guid=lambda: ident.guid, bystanders=lambda thread, speakers: [], human=lambda: ident)

            async def extract_thread(tid):
                with store.transaction():
                    thread = store.conn.execute("SELECT key FROM chat_thread WHERE id=?", (tid,)).fetchone()
                    lines = tuple(r[0] for r in store.conn.execute(
                        "SELECT id FROM chat_line WHERE thread_id=? ORDER BY ts,id", (tid,)))
                if thread is None:
                    raise ValueError("pending thread disappeared")
                await extractor.extract(ClosedThread(tid, thread["key"], lines, "flush"))

            async def asker_factory():
                from leeroy_mind.eval.asker import ScratchAsker
                return await ScratchAsker.open(cfg=cfg, data_dir=root, store=store, llm=llm,
                    ident=ident, clock=clock, label=f"eval-{iso_week(night)}")

            ctx.extract_thread = extract_thread
            ctx.asker = asker_factory if use_llm else None
            try:
                yield ctx
            finally:
                # Keep the lock through exceptional and cancelled cleanup too.
                closing_scheduler, scheduler = scheduler, None
                try:
                    await closing_scheduler.close()
                finally:
                    client_closed = True
                    await _close_client(llm)
    finally:
        if scheduler is not None:
            await scheduler.close()
        if not client_closed:
            await _close_client(llm)


def _default_night(clock):
    return (date.fromisoformat(night_of(clock.local())) - timedelta(days=1)).isoformat()


def _default_week(clock):
    current = date.fromisoformat(night_of(clock.local()))
    previous = current - timedelta(days=current.weekday() + 7)
    return iso_week(previous.isoformat())


async def _run(root, store, cfg, clock, args, make_llm):
    from leeroy_mind.nightly.pipeline import STAGES
    from leeroy_mind.nightly.runner import NightRunner
    from leeroy_mind.nightly.store import UNFINISHED, run_begin, run_finish, run_get
    night = args.night or _default_night(clock)
    async with _context(root, store, cfg, clock, night, make_llm=make_llm,
                        live_locked=args.live_db) as ctx:
        prior = run_get(store, night)
        record = run_begin(store, night, *ctx.window, now=clock.now(),
                           count_attempt=prior is None or prior.status not in UNFINISHED)
        # A trial or live scheduler may have frozen a narrower original window.
        # Resuming must use that receipt, not recompute sources from calendar bounds.
        ctx.window = (record.window_start, record.window_end)
        try:
            await NightRunner(STAGES, lambda n, w: ctx).run(night, ctx.window)
        except BaseException as exc:
            run_finish(store, night, now=clock.now(),
                status="interrupted" if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt)) else "failed",
                error=type(exc).__name__)
            raise
        run_finish(store, night, now=clock.now(), status="done",
                   error=json.dumps(dict(ctx.failures)) if ctx.failures else None)
        _print(_status(store, night), ctx.ident)
        return int(bool(ctx.failures))


async def _enrich(root, store, cfg, clock, args, make_llm):
    from leeroy_mind.nightly.enrich import APPROVED, run_enrichment
    night = _default_night(clock)
    async with _context(root, store, cfg, clock, night, make_llm=make_llm,
                        live_locked=not args.dry_run) as ctx:
        members = ctx.members(include_away=True)
        if args.guid is not None:
            if args.guid not in members:
                raise ValueError("guid is not an eligible cast member")
            members = [args.guid]
        members = [g for g in members if ctx.persona(g).appearance is None]
        if args.limit is not None:
            members = members[:args.limit]
        if args.dry_run:
            # This is a fresh copied database. A previous refused live pass can have
            # a done unit but no appearance; the owner still needs its preview entry.
            with store.transaction():
                store.conn.executemany("DELETE FROM night_step WHERE night=? AND stage='enrich' AND unit=?",
                                       [(night, str(g)) for g in members])
        await ctx.reasoner.preflight()
        await ctx.arbiter.ensure_model(ctx.settings.reflection_model)
        report = await run_enrichment(ctx, members, dry_run=args.dry_run)
        if not args.dry_run and not ctx.failures:
            store.set_meta(APPROVED, "1")
        _print({"done": len(report.done), "refused": len(report.refused),
                "failed": sum(ctx.failures.values()), "path": str(report.path) if report.path else None})
        return int(bool(ctx.failures or report.refused))


def _since(value, clock):
    if value is None:
        return None
    if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        return night_bounds(_date(value), clock.local().tzinfo)[0]
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError("invalid --since timestamp")
    return number


def nightly_main(argv=None, *, make_llm=None):
    args = _nightly_parser().parse_args(argv)
    clock = SystemClock()
    try:
        root = _root(args)
        cfg = load_config(root / "mind.toml")
        if args.action in ("status", "ledger"):
            with _read_store(root / "mind.db", clock) as store:
                ident = _identity(cfg, store)
                if args.action == "status":
                    _print(_status(store, args.night), ident)
                else:
                    from leeroy_mind.nightly.store import ledger_rows
                    cast = store.cast()
                    for row in ledger_rows(store, args.guid, since=_since(args.since, clock)):
                        _print({"id": row["id"], "member": cast[row["guid"]].name if row["guid"] in cast else row["guid"],
                                "when": datetime.fromtimestamp(row["ts"], clock.local().tzinfo).isoformat(),
                                "kind": row["kind"], "text": row["text"]}, ident)
            return 0
        if args.action in ("undo", "veto"):
            with _working_store(root, clock, live=True, label=args.action) as (store, path):
                if args.action == "undo":
                    from leeroy_mind.nightly.growth import undo
                    row = store.conn.execute("SELECT kind FROM persona_ledger WHERE id=?", (args.id,)).fetchone()
                    changed = undo(store, args.id, now=clock.now())
                    print(f"reversed {row['kind']} {args.id}" if changed else "nothing to undo")
                else:
                    from leeroy_mind.nightly.language import veto
                    changed = veto(store, args.id, now=clock.now())
                    print(f"vetoed term {args.id}" if changed else "nothing to veto")
            return 0 if changed else 1
        night = args.night or _default_night(clock) if args.action == "run" else _default_night(clock)
        if args.action == "run" and night_bounds(night, clock.local().tzinfo)[1] > clock.now():
            raise ValueError("night is not complete yet")
        live = args.live_db if args.action == "run" else not args.dry_run
        label = f"{night}-trial" if args.action == "run" else f"enrich-{night}"
        filename = f"mind-{night}-trial.db" if args.action == "run" else None
        with _working_store(root, clock, live=live, label=label, filename=filename) as (store, path):
            print(f"database {path}")
            function = _run if args.action == "run" else _enrich
            return asyncio.run(function(root, store, cfg, clock, args, make_llm))
    except AlreadyRunning as exc:
        print(str(exc))
        return 1
    except KeyboardInterrupt:
        print("nightly interrupted")
        return 130
    except Exception as exc:
        # A model error can quote a private prompt, real name or local secret. Retain
        # only its type here; command-specific validation messages are safe literals.
        print(f"nightly failed: {type(exc).__name__}", file=sys.stderr)
        return 1


async def _evaluate(root, store, cfg, clock, args, make_llm):
    from leeroy_mind.eval import run_weekly
    from leeroy_mind.eval.probes import record_probe, run_secret_probes
    week = args.week or _default_week(clock)
    monday = date.fromisocalendar(int(week[:4]), int(week[6:]), 1)
    night = (monday + timedelta(days=6)).isoformat()
    async with _context(root, store, cfg, clock, night, make_llm=make_llm,
                        use_llm=not args.no_llm, live_locked=args.live_db or args.probe_secrets) as ctx:
        if args.probe_secrets:
            await ctx.arbiter.ensure_model(ctx.settings.live_model)
            result = await run_secret_probes(ctx.reasoner.llm, ctx.settings.live_model, ctx.ident)
            record_probe(store, result, model=ctx.settings.live_model, now=clock.now())
            print(f"n {result.n} leaks {result.leaks} rate {result.rate:.6g}")
            return 0
        asker = None
        try:
            if not args.no_llm:
                asker = await ctx.asker()
                ctx.asker = asker
            path = await run_weekly(ctx, week, use_llm=not args.no_llm)
            print(f"report {path}")
            # The JSON carries partial/failure statuses; an artifact alone is not a pass.
            sections = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
            return int(any(section.get("status") in ("partial", "failed")
                           for section in sections.values() if isinstance(section, dict)))
        finally:
            if asker is not None:
                await asker.aclose()


def eval_main(argv=None, *, make_llm=None):
    parser = argparse.ArgumentParser(prog="leeroy_mind eval")
    _data_arg(parser)
    parser.add_argument("--week", type=_week)
    parser.add_argument("--no-llm", action="store_true")
    parser.add_argument("--live-db", action="store_true")
    parser.add_argument("--probe-secrets", action="store_true")
    args = parser.parse_args(argv)
    if args.no_llm and args.probe_secrets:
        parser.error("--probe-secrets cannot be combined with --no-llm")
    clock = SystemClock()
    try:
        root = _root(args)
        cfg = load_config(root / "mind.toml")
        week = args.week or _default_week(clock)
        live = args.live_db or args.probe_secrets
        with _working_store(root, clock, live=live, label=f"eval-{week}-trial",
                            filename=f"mind-eval-{week}-trial.db") as (store, path):
            if not live:
                print(f"database {path}")
            return asyncio.run(_evaluate(root, store, cfg, clock, args, make_llm))
    except AlreadyRunning as exc:
        print(str(exc))
        return 1
    except KeyboardInterrupt:
        print("evaluation interrupted")
        return 130
    except Exception as exc:
        print(f"evaluation failed: {type(exc).__name__}", file=sys.stderr)
        return 1
