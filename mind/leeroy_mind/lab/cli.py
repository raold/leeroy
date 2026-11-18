"""`python -m leeroy_mind lab report|run|variants|add-variant|judge-eval`.

- `report [--night N] [--runs K] [--json]`: the stored runs, read only (mode=ro, no migration).
- `run --dry-run`: tonight's plan as the night would make it (the piece, the pairs, the arms and
  the budget); starts nothing and writes nothing.
- `run`: the whole loop once for the operator, with the mind stopped: plan, propose, the battery
  in a sandbox child under <data dir>/scratch/lab, grading and promotion, stored as a `cli` run.
  It swaps the reflection and live models as the night does; `--grade-model live` grades and
  proposes on the live model instead and swaps nothing (for a run beside a live mind on a copy).
  `--piece P` names the piece instead of the round-robin.
- `variants`: the hand-written variants in <data dir>/lab/variants (lab/authored.py), each
  waiting, tried or refused with its code; read only.
- `add-variant --piece P --file F [--name N]`: screens a hand-written variant and copies it into
  that folder, where the next night (or `lab run`) tries it before the model's own. It writes
  that one file and nothing else, so it works beside a running mind.
- `judge-eval --gold DIR [--part test|dev|train|all] [--judges a,b] [--seed N] [--json]`: how
  well each judge (lab/judge.py's and the old rubric) agrees with a strong grader's grades in a
  directory of gold conversations (lab/gold.py, lab/judge_eval.py): numbers only on stdout. It
  asks the live model at its own num_ctx (think off, temperature 0), so it runs beside a live
  mind; it needs no mind.db, writes nothing and never prints a word of the gold. `--fit` prints
  weights refit on the train part instead (numbers; a new `judge_weights.json`).
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

from leeroy_mind.talk import pieces

from . import authored, battery, gold, judge, judge_eval, propose, report as reporting, store
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
    run.add_argument("--piece", choices=pieces.NAMES,
                     help="the piece to vary (default: the round-robin's)")
    run.add_argument("--json", action="store_true")
    run.add_argument("--data-dir", default=argparse.SUPPRESS)
    v = sub.add_parser("variants", help="the hand-written variants waiting, tried or refused")
    v.add_argument("--data-dir", default=argparse.SUPPRESS)
    a = sub.add_parser("add-variant", help="screen a hand-written variant and queue it for the lab")
    a.add_argument("--piece", required=True, choices=pieces.NAMES)
    a.add_argument("--file", required=True, help="a text file holding the variant, one paragraph")
    a.add_argument("--name", help="the name it is kept under (default: the file's name)")
    a.add_argument("--data-dir", default=argparse.SUPPRESS)
    e = sub.add_parser("judge-eval", help="the judges against a directory of gold conversations")
    e.add_argument("--gold", required=True, help="a directory of conv-*.json (lab/gold.py)")
    e.add_argument("--part", choices=("test", "dev", "train", "all"), default="test")
    e.add_argument("--judges", help="comma-separated: " + ", ".join(judge_eval.DESIGNS)
                   + " (default: all)")
    e.add_argument("--seed", type=int, default=0)
    e.add_argument("--parallel", type=int, default=2)
    e.add_argument("--fit", action="store_true",
                   help="print weights refit on the train part instead of the tables")
    e.add_argument("--json", action="store_true")
    e.add_argument("--data-dir", default=argparse.SUPPRESS)
    return p


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


def _read_only(root: Path):
    return sqlite3.connect(f"{(root / 'mind.db').resolve().as_uri()}?mode=ro", uri=True, timeout=5.0)


def _known_names(conn, root: Path):
    """The cast's, the personas' and his names as the night's screen reads them, from a
    read-only connection; None when they cannot be read (the night screens again)."""
    try:
        from leeroy_mind.turing import judge
        from leeroy_mind.turing.corpus import name_pattern, names_by_guid, persona_names
        row = conn.execute("SELECT value FROM meta WHERE key='human_guid'").fetchone()
        human = int(row[0]) if row and str(row[0]).isdigit() else 0
        his = [r[0] for r in conn.execute("SELECT value FROM meta WHERE key='human_name'") if r[0]]
        try:
            import tomllib
            table = tomllib.loads((root / "mind.toml").read_bytes().decode("utf-8-sig")).get("human", {})
            his += [table[k] for k in ("nickname", "real_name") if isinstance(table.get(k), str)]
        except (OSError, UnicodeError, ValueError, AttributeError):
            pass
        owners = judge.name_owners(names_by_guid(conn, human=human, his=his), human=human,
                                   his_names=his, aliases=persona_names(conn))
        return name_pattern(owners, min_len=3)
    except Exception:
        return None


def _earlier(conn, piece: str) -> list[str]:
    return store.earlier_texts(conn, piece) if store.available(conn) else []


def _variants(root: Path) -> int:
    conn = _read_only(root)
    try:
        names_rx = _known_names(conn, root)
        shown = 0
        for piece in pieces.NAMES:
            found = authored.read(root, piece)
            if not found:
                continue
            earlier = _earlier(conn, piece)
            tried = {propose._norm(t) for t in earlier}
            ready = {v["name"] for v in authored.waiting(root, piece, earlier=earlier,
                                                         names_rx=names_rx)[0]}
            print(f"{piece}:")
            for name, text in found:
                if name in ready:
                    state = "waiting"
                elif propose._norm(text) in tried:
                    state = "tried"
                else:
                    code = propose.screen(piece, text, names_rx=names_rx, authored=True)
                    state = f"refused ({code})" if code else "same as an earlier file"
                print(f"  {name}: {state}, {len(text)} characters")
                shown += 1
    finally:
        conn.close()
    from .night import skipped_pieces
    never = sorted(skipped_pieces(root))
    if not shown:
        print(f"lab variants: none in {authored.folder(root)}")
    if never:
        print("not picked with this mind.toml (never rendered, or an overlay file of that name): "
              + ", ".join(never))
    return 0


def _add_variant(root: Path, args) -> int:
    source = Path(args.file)
    try:
        raw = source.read_bytes()
        if len(raw) > authored.MAX_BYTES:
            raise authored.AuthoredError("the file is too large for one variant")
        text = raw.decode("utf-8-sig")
    except (OSError, UnicodeError) as e:
        print(f"lab add-variant: cannot read the file ({type(e).__name__})", file=sys.stderr)
        return 2
    except authored.AuthoredError as e:
        print(f"lab add-variant: {e}", file=sys.stderr)
        return 2
    conn = _read_only(root)
    try:
        path = authored.add(root, args.piece, args.name or source.stem, text,
                            earlier=_earlier(conn, args.piece), names_rx=_known_names(conn, root))
    except authored.AuthoredError as e:
        print(f"lab add-variant: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    from .night import skipped_pieces
    print(f"lab add-variant: {path} is waiting for the next lab night of {args.piece}")
    if args.piece in skipped_pieces(root):
        print(f"  note: {args.piece} is not rendered with this mind.toml (or has an overlay file), "
              "so the night will not pick it; `lab run --piece` still can")
    return 0


def _dry_run(root: Path, args, cfg) -> int:
    from .night import skipped_pieces
    conn = _read_only(root)
    try:
        history = store.pieces_history(conn) if store.available(conn) else []
        members = _members(conn, args)
        names = dict(conn.execute("SELECT guid, name FROM cast_member"))
        prefer = authored.pieces_waiting(root, lambda n: _earlier(conn, n),
                                         names_rx=_known_names(conn, root))
    finally:
        conn.close()
    night = datetime.now().strftime("%Y-%m-%d")
    piece = args.piece or propose.pick_piece(history, skip=skipped_pieces(root), prefer=prefer)
    pairs = battery.plan(_scripts(args), members, f"lab|{night}", cfg.pairs)
    if piece in prefer:
        print(f"lab run (dry run): hand-written variants are waiting for {piece}")
    print(f"lab run (dry run): piece {piece or 'none'}, {len(pairs)} pairs over "
          f"{len({p['guid'] for p in pairs})} members, arms: control + up to {cfg.variants} "
          f"variants, judge {cfg.judge} pairs an arm, budget {cfg.minutes} min, grading on the "
          f"{args.grade_model} model, grader {cfg.grader}"
          + (" (the judge's scores on the live model)" if cfg.grader == "tells" else "")
          + (", promoting a winner" if cfg.promote else ", measure only (nothing is proposed)")
          + ". Nothing was started or written.")
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
                    ctx.reasoner = Reasoner(lab_night.LiveContext(ctx.reasoner.llm), mind_cfg.models.live,
                                            parallel=ctx.settings.parallel)
                await ctx.reasoner.preflight()
                scripts = {s.id for s in _scripts(args)} if args.scripts else None
                members = ({int(g) for g in args.members.split(",") if g.strip()}
                           if args.members else None)
                stages = (lab_night.make_propose(cfg, source="cli", scripts=scripts, members=members,
                                                 piece=args.piece),
                          lab_night.make_battery(cfg, source="cli", make_child=make_child),
                          lab_night.make_score(cfg, source="cli"),
                          lab_night.make_grade(cfg, source="cli"))
                models = (ctx.reasoner.model, mind_cfg.models.live, mind_cfg.models.live,
                          ctx.reasoner.model)
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


def _judge_eval(root: Path, args, *, make_llm) -> int:
    from leeroy_mind.config import ConfigError, load_config
    from leeroy_mind.llm.client import GpuPaused, LLMError
    from leeroy_mind.nightly.llm import Reasoner
    from . import night as lab_night
    designs = tuple(d.strip() for d in (args.judges or ",".join(judge_eval.DESIGNS)).split(",")
                    if d.strip())
    unknown = [d for d in designs if d not in judge_eval.DESIGNS]
    if unknown or not designs or not 1 <= args.parallel <= 4:
        print("lab judge-eval: judges are " + ", ".join(judge_eval.DESIGNS) + "; parallel 1-4",
              file=sys.stderr)
        return 2
    try:
        cfg = load_config(root / "mind.toml")
        found = gold.load(args.gold)
    except (ConfigError, gold.GoldError) as e:
        print(f"lab judge-eval: {type(e).__name__}"
              + (f": {e}" if isinstance(e, gold.GoldError) else ""), file=sys.stderr)
        return 2
    if not found:
        print("lab judge-eval: no graded reply in that directory", file=sys.stderr)
        return 2
    if _gpu_paused(root):
        print("lab judge-eval: the GPU is paused from the console (Free GPU): resume it first",
              file=sys.stderr)
        return 2
    if make_llm is not None:
        llm = make_llm(cfg)
    else:
        from leeroy_mind.llm.client import OllamaClient
        llm = OllamaClient(cfg.ollama.url)
    reasoner = Reasoner(lab_night.LiveContext(_PauseChecked(llm, root)), cfg.models.live,
                        parallel=args.parallel)

    async def go():
        if args.fit:
            return await judge_eval.refit(reasoner, args.gold, seed=args.seed,
                                          parallel=args.parallel)
        return await judge_eval.evaluate(reasoner, args.gold, designs=designs, part=args.part,
                                         seed=args.seed, parallel=args.parallel)
    try:
        data = asyncio.run(go())
    except GpuPaused:
        print("lab judge-eval: the GPU is paused", file=sys.stderr)
        return 2
    except LLMError as e:
        print(f"lab judge-eval: the model did not answer ({type(e).__name__})", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    print(json.dumps(data, sort_keys=True) if args.json or args.fit else judge_eval.render(data))
    return 0


class _PauseChecked:
    """The judge's model client with Free GPU read again before every request. judge-eval is
    its own process: the running mind's GPU gate never sees its requests, so a pause set from
    the console while it runs would otherwise be met by a reload of the model the console just
    unloaded. A pause stops the evaluation (GpuPaused; requests already sent finish)."""

    def __init__(self, llm, root: Path):
        self._llm, self._root = llm, root

    async def chat(self, *args, **kwargs):
        if _gpu_paused(self._root):
            from leeroy_mind.llm.client import GpuPaused
            raise GpuPaused("The GPU is paused.")
        return await self._llm.chat(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._llm, name)


def _gpu_paused(root: Path) -> bool:
    """Free GPU's durable pause (meta gpu:paused), read only; no mind.db: not paused."""
    from leeroy_mind.gpucontrol import KEY
    db = root / "mind.db"
    if not db.is_file():
        return False
    try:
        conn = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True, timeout=5.0)
        try:
            return conn.execute("SELECT 1 FROM meta WHERE key=?", (KEY,)).fetchone() is not None
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def lab_main(argv=None, *, make_llm=None, make_child=None) -> int:
    args = _parser().parse_args(argv)
    root = Path(args.data_dir).resolve() if args.data_dir else default_data_dir()
    if args.action == "judge-eval":
        return _judge_eval(root, args, make_llm=make_llm)
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
    if args.action in ("variants", "add-variant"):
        try:
            return _variants(root) if args.action == "variants" else _add_variant(root, args)
        except sqlite3.Error as e:
            print(f"lab {args.action} failed: {type(e).__name__}", file=sys.stderr)
            return 1
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
