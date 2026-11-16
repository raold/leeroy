"""`python -m leeroy_mind lab report|run`.

- `report [--night N] [--runs K] [--json]`: the stored runs, read only (mode=ro, no migration).
- `run --dry-run`: tonight's plan as the night would make it (the piece, the pairs, the arms and
  the budget); starts nothing and writes nothing.
- `run`: the whole loop once for the operator, with the mind stopped: plan, propose, the battery
  in a sandbox child under <data dir>/scratch/lab, grading and promotion, stored as a `cli` run.
  It swaps the reflection and live models as the night does; `--grade-model live` grades and
  proposes on the live model instead and swaps nothing (for a run beside a live mind on a copy).
"""
import argparse
import asyncio
import json
import logging
import sqlite3
import sys
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from leeroy_mind.config import default_data_dir

from . import battery, propose, report as reporting, store
from .config import load


def _parser():
    p = argparse.ArgumentParser(prog="leeroy_mind lab",
                                description="The conversation lab: battery, judge, prompt variants.")
    p.add_argument("--data-dir")
    sub = p.add_subparsers(dest="action", required=True)
    r = sub.add_parser("report", help="the stored runs: means, flags, variants and promotions")
    r.add_argument("--night")
    r.add_argument("--runs", type=int, default=7)
    r.add_argument("--json", action="store_true")
    r.add_argument("--data-dir", default=argparse.SUPPRESS)
    run = sub.add_parser("run", help="the loop once (mind stopped); --dry-run prints the plan")
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--grade-model", choices=("reflection", "live"), default="reflection")
    run.add_argument("--pairs", type=int)
    run.add_argument("--variants", type=int)
    run.add_argument("--minutes", type=int)
    run.add_argument("--scripts", help="comma-separated script ids (default: the whole battery)")
    run.add_argument("--members", help="comma-separated member guids (default: the sample)")
    run.add_argument("--json", action="store_true")
    run.add_argument("--data-dir", default=argparse.SUPPRESS)
    return p


class LiveContext:
    """The model client with every chat request at the live preset's num_ctx (4096). The
    reflection presets ask 8192, and Ollama reloads a loaded model for another context size:
    beside a running live mind that would reload its pinned live model twice a call."""

    def __init__(self, llm):
        from leeroy_mind.llm.presets import LIVE
        self._llm = llm
        self.num_ctx = LIVE.options["num_ctx"]

    async def chat(self, model, messages, *, options, **kwargs):
        return await self._llm.chat(model, messages, options={**options, "num_ctx": self.num_ctx},
                                    **kwargs)

    async def embed(self, *args, **kwargs):
        return await self._llm.embed(*args, **kwargs)

    async def list_models(self):
        return await self._llm.list_models()

    def __getattr__(self, name):
        return getattr(self._llm, name)


class _Events(logging.Handler):
    """The lab's own log events on stdout while `lab run` runs: codes and counts only."""

    def emit(self, record):
        event = getattr(record, "leeroy_event", None)
        if event and (event.startswith("lab_") or event == "nightly_unit_failed"):
            fields = getattr(record, "leeroy_fields", {})
            print(f"  {event} " + " ".join(f"{k}={v}" for k, v in fields.items()), flush=True)


def _cfg(root: Path, args):
    cfg = replace(load(root / "mind.toml"), enabled=True)
    over = {k: getattr(args, k) for k in ("pairs", "variants", "minutes") if getattr(args, k) is not None}
    if over:
        cfg = replace(cfg, **over)
    if not (2 <= cfg.pairs <= 60 and 0 <= cfg.variants <= 5 and 5 <= cfg.minutes <= 180):
        raise ValueError("pairs 2-60, variants 0-5, minutes 5-180")
    return replace(cfg, judge=min(cfg.judge, cfg.pairs))


def _scripts(args) -> list:
    scripts = battery.load()
    if args.scripts:
        wanted = {s.strip() for s in args.scripts.split(",") if s.strip()}
        unknown = wanted - {s.id for s in scripts}
        if unknown:
            raise ValueError(f"unknown scripts: {', '.join(sorted(unknown))}")
        scripts = [s for s in scripts if s.id in wanted]
    return scripts


def _members(conn, args) -> dict[int, str]:
    """guid -> the stance the sampler reads, from the stored personas (a man's: none)."""
    out = {}
    human = conn.execute("SELECT value FROM meta WHERE key='human_guid'").fetchone()
    human = int(human[0]) if human and str(human[0]).isdigit() else 0
    for guid, raw in conn.execute("SELECT p.guid, p.json FROM persona_core p JOIN cast_member c "
                                  "ON c.guid=p.guid WHERE c.level >= 80"):
        if guid == human:
            continue
        try:
            doc = json.loads(raw)
            woman = doc["identity"]["player_gender"] == "female"
            out[guid] = doc["romance"]["stance"] if woman else "none"
        except (ValueError, KeyError, TypeError):
            continue
    if args.members:
        wanted = {int(g) for g in args.members.split(",") if g.strip()}
        out = {g: s for g, s in out.items() if g in wanted}
    return out


def _dry_run(root: Path, args, cfg) -> int:
    from .night import _live_overlay_pieces
    conn = sqlite3.connect(f"{(root / 'mind.db').resolve().as_uri()}?mode=ro", uri=True, timeout=5.0)
    try:
        history = store.pieces_history(conn) if store.available(conn) else []
        members = _members(conn, args)
        names = dict(conn.execute("SELECT guid, name FROM cast_member"))
    finally:
        conn.close()
    night = datetime.now().strftime("%Y-%m-%d")
    piece = propose.pick_piece(history, skip=_live_overlay_pieces(root))
    pairs = battery.plan(_scripts(args), members, f"lab|{night}", cfg.pairs)
    print(f"lab run (dry run): piece {piece or 'none'}, {len(pairs)} pairs over "
          f"{len({p['guid'] for p in pairs})} members, arms: control + up to {cfg.variants} "
          f"variants, judge {cfg.judge} pairs an arm, budget {cfg.minutes} min, grading on the "
          f"{args.grade_model} model. Nothing was started or written.")
    for p in pairs:
        print(f"  pair {p['pair']}: {p['script']} ({p['kind']}) with {names.get(p['guid'], '?')} "
              f"#{p['guid']} by {p['channel']}")
    return 0


async def _run(root: Path, args, cfg, *, make_llm, make_child) -> int:
    from leeroy_mind.clock import SystemClock
    from leeroy_mind.config import load_config
    from leeroy_mind.nightly.cli import _context, _exclusive
    from leeroy_mind.nightly.llm import Reasoner
    from leeroy_mind.storage.db import open_db
    from leeroy_mind.storage.store import Store
    from . import night as lab_night
    mind_cfg = load_config(root / "mind.toml")
    clock = SystemClock()
    key = "cli-" + datetime.now().strftime("%Y%m%d-%H%M%S")
    events = _Events(logging.INFO)
    logger = logging.getLogger("leeroy_mind")
    level = logger.level
    logger.addHandler(events)
    logger.setLevel(logging.INFO)
    with _exclusive(root, gpu=True):
        st = Store(open_db(root / "mind.db"), clock=clock)
        try:
            async with _context(root, st, mind_cfg, clock, datetime.now().strftime("%Y-%m-%d"),
                                make_llm=make_llm, live_locked=True) as ctx:
                ctx.night = key
                swap = args.grade_model == "reflection"
                if not swap:  # the live model at its own num_ctx: Ollama never reloads it
                    ctx.reasoner = Reasoner(LiveContext(ctx.reasoner.llm), mind_cfg.models.live,
                                            parallel=ctx.settings.parallel)
                await ctx.reasoner.preflight()
                scripts = {s.id for s in _scripts(args)} if args.scripts else None
                members = ({int(g) for g in args.members.split(",") if g.strip()}
                           if args.members else None)
                stages = (lab_night.make_propose(cfg, source="cli", scripts=scripts, members=members),
                          lab_night.make_battery(cfg, source="cli", make_child=make_child),
                          lab_night.make_grade(cfg, source="cli"))
                models = (ctx.reasoner.model, mind_cfg.models.live, ctx.reasoner.model)
                for stage, model in zip(stages, models):
                    if swap:
                        await ctx.arbiter.ensure_model(model)
                    print(f"lab run: {stage.__name__.removeprefix('stage_')}...", flush=True)
                    await stage(ctx)
                with ctx.store.transaction():
                    found = store.night_run(ctx.store.conn, key, "cli")
                    data = reporting.report(ctx.store.conn, night=key) if found else None
                failures = dict(ctx.failures)
        finally:
            st.close()
            logger.removeHandler(events)
            logger.setLevel(level)
    if data is None:
        print("lab run: no run was made (see mind.log: lab_waiting or lab_not_set_up)")
        return 1
    print(json.dumps(data, sort_keys=True) if args.json else reporting.render(data))
    if failures:
        print(f"lab run: failed units {json.dumps(failures, sort_keys=True)}", file=sys.stderr)
    return 0 if data["runs"] and data["runs"][0]["status"] in ("done", "budget") else 1


def lab_main(argv=None, *, make_llm=None, make_child=None) -> int:
    args = _parser().parse_args(argv)
    root = Path(args.data_dir).resolve() if args.data_dir else default_data_dir()
    if not (root / "mind.db").is_file():
        print(f"lab: no mind.db in {root}", file=sys.stderr)
        return 2
    if args.action == "report":
        try:
            conn = sqlite3.connect(f"{(root / 'mind.db').resolve().as_uri()}?mode=ro", uri=True,
                                   timeout=5.0)
            try:
                data = reporting.report(conn, night=args.night, runs=max(1, min(args.runs, 100)))
            finally:
                conn.close()
        except sqlite3.Error as e:
            print(f"lab report failed: {type(e).__name__}", file=sys.stderr)
            return 1
        print(json.dumps(data, sort_keys=True) if args.json else reporting.render(data))
        return 0
    try:
        cfg = _cfg(root, args)
        if args.dry_run:
            return _dry_run(root, args, cfg)
        _scripts(args)
    except (ValueError, battery.BatteryError, sqlite3.Error) as e:
        print(f"lab run: {e}", file=sys.stderr)
        return 2
    from leeroy_mind.app import AlreadyRunning
    try:
        return asyncio.run(_run(root, args, cfg, make_llm=make_llm, make_child=make_child))
    except AlreadyRunning as e:
        print(f"lab run: {e}; stop the mind first, or run it on a copy of the data dir",
              file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    finally:
        time.sleep(0)
