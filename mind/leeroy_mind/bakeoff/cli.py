"""`python -m leeroy_mind bakeoff run|sheet|tally` (README.md).

run: with the mind stopped (its instance lock held throughout), WoW closed and the GPU not paused
from the console, asks every candidate of `--config` the captured turns again, scores them and
writes `<out>/results.sqlite`, `report.json` and `report.md`. mind.db is only read (mode=ro);
nothing is written to it. It makes room on the GPU first (every loaded model but the embedding
model), unloads each model when the next begins and, at the end, every model it used. A model
that is not pulled is listed as missing and skipped, never pulled. `--dry-run` shows the plan and
the missing models without the lock, the GPU or any write, so it is safe beside a running mind.
sheet and tally read only `<out>`. Output carries counts, codes and paths; never a line, a prompt
or a name."""
import argparse
import asyncio
import json
import subprocess
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

from leeroy_mind.app import AlreadyRunning
from leeroy_mind.clock import SystemClock
from leeroy_mind.config import load_config
from leeroy_mind.nightly.cli import _data_arg, _exclusive, _identity, _positive, _read_store, _root

from . import plan as planning
from . import results, score, sheet
from .capture import RETENTION_S
from .config import CandidateError, load_candidates

GPU_PAUSED = "gpu:paused"  # gpucontrol.KEY: the owner's Free GPU
WINDOWS_MAX = 60
EMBED_BATCH = 64
EMBED_MAX_CHARS = 4000
WOW_PS = ("$ErrorActionPreference='Stop'; "
          "@(Get-CimInstance Win32_Process -Filter \"Name='Wow.exe'\").Count")


class Refused(Exception):
    pass


def wow_running() -> bool | None:
    """Whether WoW's client is running; None when the process list cannot be read."""
    try:
        done = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", WOW_PS],
                              capture_output=True, text=True, timeout=20, check=True,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return int(done.stdout.strip() or "0") > 0
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _kinds(text: str) -> tuple[str, ...]:
    kinds = tuple(dict.fromkeys(k.strip() for k in text.split(",") if k.strip()))
    bad = [k for k in kinds if k not in planning.KINDS]
    if not kinds or bad:
        raise argparse.ArgumentTypeError(f"kinds are {', '.join(planning.KINDS)}")
    return kinds


def _windows(text: str) -> int:
    value = int(text)
    if not 0 <= value <= WINDOWS_MAX:
        raise argparse.ArgumentTypeError(f"windows must be 0-{WINDOWS_MAX}")
    return value


def _parser():
    parser = argparse.ArgumentParser(prog="leeroy_mind bakeoff",
                                     description="Other models and prompts on captured turns.")
    _data_arg(parser)
    subs = parser.add_subparsers(dest="action", required=True)
    run = subs.add_parser("run", help="ask the candidates the captured turns again and score them "
                          "(the mind stopped, WoW closed)")
    _data_arg(run, child=True)
    run.add_argument("--config", type=Path, required=True, help="the candidates' TOML file")
    run.add_argument("--turns", type=_positive, default=150)
    run.add_argument("--windows", type=_windows, default=8, help="blind-judge windows, 0-60")
    run.add_argument("--kinds", type=_kinds, default=planning.KINDS,
                     help="comma-separated: " + ",".join(planning.KINDS))
    run.add_argument("--out", type=Path, help="default: <data>/scratch/bakeoff-<time>; the same "
                     "folder again goes on with an unfinished run")
    run.add_argument("--seed", default="bakeoff")
    run.add_argument("--dry-run", action="store_true",
                     help="the plan and the missing models only: no lock, GPU or write")
    s = subs.add_parser("sheet", help="a blind pick sheet from a finished run")
    s.add_argument("--out", type=Path, required=True)
    s.add_argument("--n", type=_positive, default=20)
    t = subs.add_parser("tally", help="score picks.json against the sheet's key")
    t.add_argument("--picks", type=Path, required=True)
    t.add_argument("--out", type=Path, help="the sheet's folder (default: the picks' folder)")
    return parser


def _out_dir(arg: Path | None, root: Path, clock) -> Path:
    from leeroy_mind.learn.dataset import output_dir
    target = arg if arg is not None else (
        root / "scratch" / ("bakeoff-" + datetime.fromtimestamp(clock.now()).strftime("%Y%m%d-%H%M%S")))
    try:
        return output_dir(target, data_dir=root)
    except ValueError as e:
        why = {"learn_output_in_repository": "it is inside a git checkout",
               "learn_output_linked": "it goes through a link or junction"}.get(str(e), str(e))
        raise Refused(f"cannot write the results there: {why}") from None


def _has(models: set[str], name: str) -> bool:
    return name in models or (":" not in name and f"{name}:latest" in models)


def _norm(name: str) -> str:
    return name if ":" in name else name + ":latest"


def _make_llm(cfg, make_llm):
    if make_llm is not None:
        return make_llm(cfg)
    from leeroy_mind.llm.client import OllamaClient
    return OllamaClient(cfg.ollama.url)


def _make_admin(cfg, make_admin):
    if make_admin is not None:
        return make_admin(cfg)
    from leeroy_mind.nightly.gpu import HttpOllamaAdmin
    return HttpOllamaAdmin(cfg.ollama.url)


async def _close(llm):
    close = getattr(llm, "aclose", None) or getattr(llm, "close", None)
    if close is not None:
        result = close()
        if asyncio.iscoroutine(result):
            await result


class _Context:
    """What every step reads: the identity, the names and how they are masked."""

    def __init__(self, store, cfg):
        from leeroy_mind.turing.corpus import name_pattern, names_by_guid, persona_names
        from leeroy_mind.turing.judge import masking, name_owners
        from leeroy_mind.turing.night import his_names
        self.ident = _identity(cfg, store)
        self.human = self.ident.guid
        self.his = his_names(self.ident)
        self.by_guid = names_by_guid(store.conn, human=self.human, his=self.his)
        self.owners = name_owners(self.by_guid, human=self.human, his_names=self.his,
                                  aliases=persona_names(store.conn))
        self.mask = masking(self.owners)
        self.names = name_pattern([*self.by_guid.values(), *self.his])


def _plan(store, ctx, args, *, until: float, stored: dict | None):
    from leeroy_mind.turing.corpus import read_lines
    since = stored["since"] if stored else until - RETENTION_S
    until = stored["until"] if stored else until
    seed = stored["seed"] if stored else args.seed
    lines = read_lines(store.conn, human=ctx.human, start=since, end=until)
    ids = stored["plan"]["turns"] if stored else None
    captured = planning.read_captured(store.conn, since=since, until=until, ids=ids)
    built = planning.build(captured, lines, human=ctx.human, turns=args.turns, windows=args.windows,
                           kinds=set(args.kinds), seed=seed, since=since, until=until,
                           fixed=stored["plan"] if stored else None)
    return built, captured


def _summary(captured, built) -> list[str]:
    kinds = Counter(planning.kind_of(c.channel) for c in captured)
    families = Counter(t.family for t in built.turns.values())
    in_windows = len({t for w in built.windows for t in w["turns"]})
    return [f"captured: {len(captured)} turns (" + ", ".join(f"{k} {n}" for k, n in sorted(kinds.items()))
            + ")",
            f"plan: {len(built.turns)} turns (" + ", ".join(f"{f} {n}" for f, n in sorted(families.items()))
            + f"), {len(built.windows)} judge windows holding {in_windows} of them"]


async def _dry(root, cfg, candidates, args, make_llm, clock) -> int:
    with _read_store(root / "mind.db", clock) as store:
        ctx = _Context(store, cfg)
        built, captured = _plan(store, ctx, args, until=clock.now(), stored=None)
    for line in _summary(captured, built):
        print(line)
    llm = _make_llm(cfg, make_llm)
    try:
        models = await llm.list_models()
    except Exception as e:
        print(f"models: cannot ask Ollama ({type(e).__name__})")
        return 0
    finally:
        await _close(llm)
    missing = [c.model for c in candidates if not _has(models, c.model)]
    print("models: " + ("all pulled" if not missing else "missing (not pulled): "
                        + ", ".join(dict.fromkeys(missing))))
    print("reflection model (the judge): " + ("pulled" if _has(models, cfg.models.reflection)
                                              else "missing"))
    return 0


def _store_plan(res, built, ctx, *, args, candidates):
    for t in built.turns.values():
        res.execute("INSERT OR IGNORE INTO turn(id, family, kind, guid, refs, answers, context) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (t.id, t.family, t.kind, t.captured.guid, json.dumps(list(t.refs)), int(t.answers),
                     json.dumps(planning.context_rows(t, pattern=ctx.mask, owners=ctx.owners),
                                ensure_ascii=False)))
    for w in built.windows:
        res.execute("INSERT OR IGNORE INTO window(unit, family, refs, turns) VALUES (?,?,?,?)",
                    (w["unit"], w["family"], json.dumps(w["refs"]), json.dumps(w["turns"])))
    if results.get(res, "plan") is None:
        results.put(res, "plan", planning.stored(built))
        results.put(res, "since", built.since)
        results.put(res, "until", built.until)
        results.put(res, "seed", built.seed)
        results.put(res, "kinds", list(args.kinds))
    names = results.get(res, "candidates", [])
    for c in candidates:
        if c.name not in names:
            names.append(c.name)
    results.put(res, "candidates", names)
    results.put(res, "config", [c.describe() for c in candidates])


def _baselines(res, built, ctx, typist):
    sent, live = results.results(res, "sent"), results.results(res, "live")
    for t in built.turns.values():
        if t.id not in sent:
            texts = [line.text for line in t.lines]
            results.put_result(res, "sent", t.id, status="ok", lines=texts, text=None,
                               masked=planning.mask_lines(texts, t, pattern=ctx.mask, owners=ctx.owners))
        if t.id not in live:
            _typed(res, "live", t, t.captured.reply, typist, ctx)


def _typed(res, name, t, text, typist, ctx, **timing):
    try:
        status, lines = typist.type(text, t)
        code = None
    except LookupError as e:
        status, lines, code = "error", [], str(e)
    results.put_result(res, name, t.id, status=status, code=code, text=text, lines=lines,
                       masked=planning.mask_lines(lines, t, pattern=ctx.mask, owners=ctx.owners),
                       **timing)
    return status


async def _make_room(admin, keep: str) -> list[str]:
    loaded = await admin.loaded_models()
    for name in loaded:
        if _norm(name) != _norm(keep):
            await admin.unload(name)
    return loaded


async def _unload(admin, model: str) -> None:
    try:
        if any(_norm(n) == _norm(model) for n in await admin.loaded_models()):
            await admin.unload(model)
    except Exception as e:
        print(f"unload {type(e).__name__}", file=sys.stderr)


async def _generate(llm, admin, res, built, candidates, typist, ctx, seed) -> list[str]:
    from . import generate
    used = []
    groups: dict[str, list] = {}
    for c in candidates:
        groups.setdefault(c.model, []).append(c)
    for model, group in groups.items():
        used.append(model)
        for c in group:
            done = results.results(res, c.name)
            counts = Counter()
            for t in built.turns.values():
                if t.id in done and done[t.id]["status"] != "error":
                    counts[done[t.id]["status"]] += 1
                    continue
                r = await generate.ask(llm, c, t, seed)
                timing = dict(thinking_tokens=r["thinking_tokens"], eval_count=r["eval_count"],
                              seconds=r["seconds"])
                if r["status"] == "error":
                    results.put_result(res, c.name, t.id, status="error", code=r["code"], lines=[],
                                       masked=[], **timing)
                    counts["error"] += 1
                else:
                    counts[_typed(res, c.name, t, r["text"], typist, ctx, **timing)] += 1
            print(f"{c.name}: {len(built.turns)} turns, {counts['ok']} ok, {counts['silent']} silent, "
                  f"{counts['error']} errors")
        await _unload(admin, model)
    return used


async def _judge(llm, res, built, names, ctx, cfg) -> int:
    from leeroy_mind.nightly.llm import Reasoner, ReflectionError
    reasoner = Reasoner(llm, cfg.models.reflection, parallel=1)
    calls = 0
    for name in names:
        answers = score.texts(res, name)
        done = {r["unit"]: r["status"] for r in res.execute(
            "SELECT unit, status FROM judged WHERE candidate=?", (name,))}
        for w in built.windows:
            if done.get(w["unit"]) not in (None, "error"):
                continue
            lines = score.window_lines(built, w, answers)
            if lines is None:
                scored = {"status": "skipped", "speakers": 0, "correct": 0, "his": 0,
                          "his_correct": 0, "tells": []}
            else:
                try:
                    scored = await score.judge_window(reasoner, lines, by_guid=ctx.by_guid,
                                                      human=ctx.human, owners=ctx.owners,
                                                      pattern=ctx.mask)
                    calls += 1
                except ReflectionError:
                    scored = {"status": "error", "speakers": 0, "correct": 0, "his": 0,
                              "his_correct": 0, "tells": []}
            res.execute(
                "INSERT INTO judged(candidate, unit, family, status, speakers, correct, his, "
                "his_correct, tells) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(candidate, unit) DO "
                "UPDATE SET status=excluded.status, speakers=excluded.speakers, "
                "correct=excluded.correct, his=excluded.his, his_correct=excluded.his_correct, "
                "tells=excluded.tells",
                (name, w["unit"], w["family"], scored["status"], scored["speakers"],
                 scored["correct"], scored["his"], scored["his_correct"],
                 json.dumps(scored["tells"], ensure_ascii=False)))
    return calls


async def _vectors(llm, cfg, built, names, res, recent) -> dict | None:
    from leeroy_mind.storage.rows import pack_vec
    texts: set[str] = set()
    for name in names:
        for lines in score.texts(res, name).values():
            texts.add(" ".join(lines))
    for t in built.turns.values():
        texts.update(line.text for line in t.context)
        texts.update(text for _, text in recent.get(t.id, []))
    wanted = sorted(x for x in texts if x and len(x) <= EMBED_MAX_CHARS)
    out = {}
    try:
        for i in range(0, len(wanted), EMBED_BATCH):
            batch = wanted[i:i + EMBED_BATCH]
            for text, vec in zip(batch, await llm.embed(cfg.models.embedding, batch), strict=True):
                out[text] = pack_vec(vec)
    except Exception as e:
        print(f"embeddings: {type(e).__name__}; drift from his line not measured", file=sys.stderr)
        return None
    return out


def _report(res, built, ctx, *, names, config, missing, judge_ran, judge_note, vectors, recent) -> dict:
    sent = score.texts(res, "sent")
    cands = []
    by_name = {c["name"]: c for c in config}
    for name in names:
        answers = score.texts(res, name)
        families = score.stylometry(built, answers, names=ctx.names, by_guid=ctx.by_guid, tz=None,
                                    seed=built.seed)
        judged = score.judge_totals(res, name)
        c = by_name.get(name, {"name": name, "model": {"sent": "(as sent)", "live": "(captured reply)"}
                               .get(name, "-"), "think": False, "think_budget": 0,
                               "prompt_variant": "as_is"})
        cands.append(dict(c, families=families, judge=judged,
                          detectors=score.detectors(built, answers, recent=recent, vectors=vectors),
                          lines=score.line_stats(res, name, built, sent),
                          tells=score.tells(families, judged)))
    fmt = "%Y-%m-%d %H:%M"
    return {"version": 1, "turns": len(built.turns), "windows": len(built.windows),
            "window_turns": len({t for w in built.windows for t in w["turns"]}),
            "since": built.since, "until": built.until,
            "since_text": datetime.fromtimestamp(built.since).strftime(fmt),
            "until_text": datetime.fromtimestamp(built.until).strftime(fmt),
            "missing": missing, "judge_ran": judge_ran, "judge_note": judge_note,
            "candidates": cands}


async def _go(root, store, cfg, candidates, args, out, make_llm, make_admin, clock) -> int:
    from leeroy_mind.personas.io import PersonaCache
    from .generate import Typist
    ctx = _Context(store, cfg)
    res = results.connect(out)
    try:
        stored = None
        if results.get(res, "plan") is not None:
            stored = {k: results.get(res, k) for k in ("plan", "since", "until", "seed")}
        built, captured = _plan(store, ctx, args, until=clock.now(), stored=stored)
        for line in _summary(captured, built):
            print(line)
        if not built.turns:
            print("nothing to ask: turn on [bakeoff] capture in mind.toml and let the guild talk "
                  "first (only turns of the last 14 days that are in the Turing corpus count)")
            return 1
        _store_plan(res, built, ctx, args=args, candidates=candidates)
        typist = Typist(PersonaCache(store), his=ctx.his,
                        protect=frozenset(ctx.by_guid.values()), texture=cfg.talk.texture,
                        seed=built.seed)
        _baselines(res, built, ctx, typist)
        llm, admin = _make_llm(cfg, make_llm), _make_admin(cfg, make_admin)
        used: list[str] = []
        try:
            try:
                models = await llm.list_models()
            except Exception as e:
                raise Refused(f"cannot ask Ollama which models are pulled ({type(e).__name__})") from None
            missing = list(dict.fromkeys(c.model for c in candidates if not _has(models, c.model)))
            ready = [c for c in candidates if c.model not in missing]
            if missing:
                print("missing (not pulled, skipped): " + ", ".join(missing))
            loaded = await _make_room(admin, cfg.models.embedding)
            embedding_loaded = any(_norm(n) == _norm(cfg.models.embedding) for n in loaded)
            used += await _generate(llm, admin, res, built, ready, typist, ctx, built.seed)
            names = [*score.BASELINES, *(c.name for c in ready)]
            judge_ran, judge_note = False, ""
            if not built.windows:
                judge_note = "no window where every member line was a captured turn"
            elif not _has(models, cfg.models.reflection):
                judge_note = "the reflection model is not pulled"
            else:
                used.append(cfg.models.reflection)
                calls = await _judge(llm, res, built, names, ctx, cfg)
                await _unload(admin, cfg.models.reflection)
                judge_ran = True
                print(f"judge: {len(built.windows)} windows x {len(names)}, {calls} calls")
            recent = score.recent_own(built)
            vectors = (await _vectors(llm, cfg, built, names, res, recent)
                       if embedding_loaded else None)
            report = _report(res, built, ctx, names=names, config=[c.describe() for c in ready],
                             missing=missing, judge_ran=judge_ran, judge_note=judge_note,
                             vectors=vectors, recent=recent)
        finally:
            for model in dict.fromkeys(used):
                await _unload(admin, model)
            await _close(llm)
        (out / "report.json").write_bytes(
            (json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8"))
        (out / "report.md").write_bytes(score.render(report).encode("utf-8"))
        print(f"report: {out / 'report.md'}")
        return 0
    finally:
        res.close()


def _run(args, *, make_llm, make_admin, wow_open, clock) -> int:
    root = _root(args)
    cfg = load_config(root / "mind.toml")
    candidates = load_candidates(args.config)
    if args.dry_run:
        return asyncio.run(_dry(root, cfg, candidates, args, make_llm, clock))
    state = (wow_open or wow_running)()
    if state is None:
        raise Refused("cannot tell whether WoW is open; the bake-off takes the GPU, so it waits")
    if state:
        raise Refused("WoW is open: the bake-off takes the GPU; close WoW first")
    with _exclusive(root, gpu=True):
        with _read_store(root / "mind.db", clock) as store:
            if store.get_meta(GPU_PAUSED) is not None:
                raise Refused("the GPU is paused from the console (Free GPU): resume it first")
            out = _out_dir(args.out, root, clock)
            print(f"bakeoff: results in {out}")
            return asyncio.run(_go(root, store, cfg, candidates, args, out, make_llm, make_admin,
                                   clock))


def _sheet(args) -> int:
    out = args.out.resolve()
    conn = results.open_existing(out)
    try:
        have = set(results.candidates(conn))
        names = ["live", *(n for n in results.get(conn, "candidates", [])
                           if n not in score.BASELINES and n in have)]
        seed = results.get(conn, "seed", "bakeoff")
        done = sheet.write(conn, out, n=args.n, names=names, seed=seed)
    finally:
        conn.close()
    print(f"sheet: {done['turns']} turns, {done['candidates']} candidates -> {out / 'sheet.md'} "
          f"(key in {out / 'key.json'}; local only)")
    return 0


def _tally(args) -> int:
    out = (args.out or args.picks.parent).resolve()
    key = json.loads((out / "key.json").read_text(encoding="utf-8"))
    picks = json.loads(args.picks.read_text(encoding="utf-8"))
    t = sheet.tally(key, picks)
    print(sheet.render_tally(t))
    (out / "tally.json").write_bytes((json.dumps(t, indent=2) + "\n").encode("utf-8"))
    return 0


def bakeoff_main(argv=None, *, make_llm=None, make_admin=None, wow_open=None, clock=None) -> int:
    args = _parser().parse_args(argv)
    clock = clock or SystemClock()
    try:
        if args.action == "sheet":
            return _sheet(args)
        if args.action == "tally":
            return _tally(args)
        return _run(args, make_llm=make_llm, make_admin=make_admin, wow_open=wow_open, clock=clock)
    except AlreadyRunning:
        print("bakeoff run: the mind is running; stop it first (the bake-off would share its GPU "
              "and could unload its live model)", file=sys.stderr)
        return 1
    except (Refused, CandidateError) as e:
        print(f"bakeoff {args.action}: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("bakeoff interrupted; the same --out goes on where it stopped", file=sys.stderr)
        return 130
    except (FileNotFoundError, ValueError) as e:
        print(f"bakeoff {args.action} failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"bakeoff {args.action} failed: {type(e).__name__}", file=sys.stderr)
        return 1
