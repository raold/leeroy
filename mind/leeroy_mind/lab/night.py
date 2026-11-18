"""The conversation lab's night stages, attached only with `[lab] enabled = true` at the mind's
start, after `turing` (or `forget` without it). Three stages so the runner swaps the models:

- `lab_propose` (reflection model): unit `plan` creates the night's lab_run: tonight's piece
  (round-robin, propose.pick_piece; a piece whose `lab_<piece>.txt` overlay name the live
  prompts dir has is passed over, never read, and so is one this mind's settings never render,
  pieces.unrendered; a piece with a hand-written variant waiting goes first, lab/authored.py),
  the seeded battery plan of (script, member, channel) pairs. Unit `propose`: the piece's
  waiting hand-written variants first, then one call for the slots left of `[lab] variants`,
  and one more with what was wrong when the screen (propose.screen) refused some; each kept
  variant is stored as a prompt_variant row.
- `lab` (live model): unit `snapshot` copies the guild into scratch/lab (child.take); then one
  unit per arm, the control first: the battery in the sandbox child, its transcripts to
  scratch/lab/runs/<run>/<arm>.json (private, never backed up, only the newest run's kept), its
  rule checks into lab_score. Never while a live lease is held or the GPU is paused: it waits for
  the next night (`lab_waiting`).
- `lab_score` (live model, only with `[lab] grader = "tells"`): one unit per arm and pair, the
  lab judge's 1-5 score of each of her answers (lab/judge.py: model-free features and yes/no
  tells, the same seed for the same pair and turn in every arm), and one per variant and pair:
  its answers against the control's to the same lines, asked both ways round (judge.prefer);
  then the marker unit `scored`.
  Like the battery it never runs while a live lease is held or the GPU is paused.
- `lab_grade` (reflection model): with `grader = "rubric"` one rubric unit per arm and pair;
  one blind-judge unit for the first `[lab] judge` pairs of each arm; then `finish`: the
  comparison (promote.py; with `[lab] promote = false`, the default, nothing becomes an arm) and
  the run's summary. With the tells grader it waits for `scored`.

The run keeps its grader and whether it may promote in its plan, so a resumed run is graded the
way it began.

The budget is hard: `[lab] minutes` over all three, kept in lab_run.spent across resumes. The
battery may use up to BATTERY_SHARE of it, so grading keeps the rest; a pair is never begun past
its share, and the stage's whole work is cancelled at the end of the budget (the child killed).
`finish` always runs; arms the budget cut short are settled `budget`.

Logs carry the night, run, piece, arm and counts, never text."""
import asyncio
import json
import shutil
from pathlib import Path

from leeroy_mind.logs import log
from leeroy_mind.nightly.llm import ReflectionError
from leeroy_mind.talk import pieces

from . import authored, battery, child, grade, judge, promote, propose, store
from .config import load

STAGE_PROPOSE, STAGE_BATTERY, STAGE_GRADE = "lab_propose", "lab", "lab_grade"
STAGE_SCORE = "lab_score"
STAGES = (STAGE_PROPOSE, STAGE_BATTERY, STAGE_SCORE, STAGE_GRADE)
BATTERY_SHARE = 0.6
PROPOSE_TEMPERATURE = 0.8


def attach(runner, data_dir) -> bool:
    cfg = load(Path(data_dir) / "mind.toml")
    if not cfg.enabled:
        return False
    names = [s.name for s in runner.stages]
    after = "turing" if "turing" in names else "forget"
    runner.add_stage(STAGE_PROPOSE, make_propose(cfg), after=after, model="reflection")
    runner.add_stage(STAGE_BATTERY, make_battery(cfg), after=STAGE_PROPOSE, model="live",
                     wants_gpu=lambda ctx: _wants(ctx, STAGE_BATTERY))
    runner.add_stage(STAGE_SCORE, make_score(cfg), after=STAGE_BATTERY, model="live",
                     wants_gpu=lambda ctx: _wants(ctx, STAGE_SCORE))
    runner.add_stage(STAGE_GRADE, make_grade(cfg), after=STAGE_SCORE, model="reflection",
                     wants_gpu=lambda ctx: _wants(ctx, STAGE_GRADE))
    log("lab_attached", minutes=cfg.minutes, pairs=cfg.pairs, judge=cfg.judge,
        variants=cfg.variants, grader=cfg.grader, promote=int(cfg.promote))
    return True


class Budget:
    """[lab] minutes of seconds, `spent` of them before this stage began."""

    def __init__(self, cfg, spent: float, clock):
        self.total = cfg.minutes * 60.0
        self.spent = float(spent or 0.0)
        self.clock = clock
        self.started = clock.now()

    def used(self) -> float:
        return self.spent + max(0.0, self.clock.now() - self.started)

    def left(self, share: float = 1.0) -> float:
        return self.total * share - self.used()

    def deadline(self, share: float = 1.0) -> float:
        """On this clock: when `share` of the budget is gone."""
        return self.clock.now() + self.left(share)


def _run(ctx, source: str):
    with ctx.store.transaction():
        if not store.available(ctx.store.conn):
            return None
        return store.night_run(ctx.store.conn, ctx.night, source)


def _wants(ctx, stage: str, source: str = "night") -> bool:
    try:
        run = _run(ctx, source)
    except Exception:
        return False
    if run is None or run["status"] != "running":
        return False
    arms = _arms(ctx, run)
    battery_done = all(ctx.done(STAGE_BATTERY, f"arm:{a}") for a in arms)
    if stage == STAGE_SCORE:
        return _grader(run) == "tells" and battery_done and not ctx.done(STAGE_SCORE, "scored")
    if stage == STAGE_GRADE:
        return (not ctx.done(STAGE_GRADE, "finish") and battery_done
                and (_grader(run) != "tells" or ctx.done(STAGE_SCORE, "scored")))
    return not battery_done


def _grader(run) -> str:
    """The run's grader: its plan's (a run from before the plan carried one: the rubric)."""
    return run["plan"].get("grader", "rubric")


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


def live_reasoner(ctx):
    """A Reasoner on the live model at the live num_ctx (the context's own when `lab run
    --grade-model live` already made it one)."""
    from leeroy_mind.nightly.llm import Reasoner
    if ctx.reasoner.model == ctx.settings.live_model:
        return ctx.reasoner
    return Reasoner(LiveContext(ctx.reasoner.llm), ctx.settings.live_model,
                    parallel=ctx.settings.parallel)


def _arms(ctx, run) -> list[str]:
    with ctx.store.transaction():
        found = store.variants(ctx.store.conn, run["id"])
    return ["control", *(f"v{v['id']}" for v in found if v["status"] == "proposed")]


def _members(ctx) -> dict[int, str]:
    """The cast with a persona, each with the stance the battery samples by (a man's: none)."""
    from leeroy_mind.personas.stances import is_woman
    out = {}
    for g in ctx.members():
        try:
            p = ctx.persona(g)
        except KeyError:
            continue
        out[g] = p.romance.stance if is_woman(p) else "none"
    return out


def _live_overlay_pieces(data_dir: Path) -> set[str]:
    """Pieces whose lab overlay name exists in the live prompts dir: passed over, never read."""
    folder = Path(data_dir) / "prompts"
    return {n for n in pieces.NAMES if (folder / f"{pieces.overlay_name(n)}.txt").exists()}


def _unrendered(data_dir: Path) -> set[str]:
    """Pieces this data dir's settings never render (pieces.unrendered); none when its
    mind.toml does not load."""
    from leeroy_mind.config import load_config
    try:
        cfg = load_config(Path(data_dir) / "mind.toml")
        return set(pieces.unrendered(situation=cfg.talk.situation, realism=cfg.romance.realism))
    except Exception:
        return set()


def skipped_pieces(data_dir: Path) -> set[str]:
    return _live_overlay_pieces(data_dir) | _unrendered(data_dir)


def _held(ctx) -> str | None:
    if getattr(ctx.arbiter, "paused", False):
        return "gpu_paused"
    if ctx.arbiter.holders:
        return "lease"
    return None


def _spend(ctx, run_id: int, budget: Budget):
    def write(s):
        store.set_spent(s.conn, run_id, budget.used())
    return write


def _both(*writes):
    def write(s):
        for w in writes:
            w(s)
    return write


# -- propose ------------------------------------------------------------------------------------

def make_propose(cfg, *, source: str = "night", scripts: set[str] | None = None,
                 members: set[int] | None = None, piece: str | None = None):
    """`scripts` and `members` narrow the battery (`lab run --scripts/--members`); `piece`
    names the piece instead of the round-robin (`lab run --piece`)."""
    forced = piece
    async def stage_lab_propose(ctx) -> None:
        if not ctx.done(STAGE_PROPOSE, "plan"):
            reason = _held(ctx)
            if reason is not None:
                log("lab_waiting", night=ctx.night, stage=STAGE_PROPOSE, reason=reason)
                return
            with ctx.store.transaction():
                if not store.available(ctx.store.conn):
                    log("lab_not_set_up", night=ctx.night)
                    return
                history = store.pieces_history(ctx.store.conn)
                prefer = authored.pieces_waiting(
                    ctx.settings.data_dir, lambda n: store.earlier_texts(ctx.store.conn, n),
                    names_rx=_names_rx(ctx))
            piece = forced or propose.pick_piece(history, prefer=prefer,
                                                 skip=skipped_pieces(ctx.settings.data_dir))
            seed = f"lab|{ctx.night}"
            try:
                found = [s for s in battery.load() if scripts is None or s.id in scripts]
            except battery.BatteryError:
                ctx.fail(STAGE_PROPOSE, "plan", ValueError("lab_battery_invalid"))
                return
            sampled = {g: st for g, st in _members(ctx).items() if members is None or g in members}
            pairs = battery.plan(found, sampled, seed, cfg.pairs)
            plan = {"pairs": pairs, "judge": cfg.judge, "minutes": cfg.minutes,
                    "grader": cfg.grader, "promote": bool(cfg.promote)}

            def write(s):
                # an earlier night's run the battery never finished (a lease, a crash) ends here
                s.conn.execute("UPDATE lab_run SET status='failed', reason='unfinished', finished=? "
                               "WHERE status='running' AND source=? AND night!=?",
                               (ctx.clock.now(), source, ctx.night))
                rid = store.create_run(s.conn, night=ctx.night, source=source,
                                       piece=piece or "none", seed=seed, plan=plan,
                                       now=ctx.clock.now())
                if piece is None or not pairs:
                    store.finish(s.conn, rid, status="skipped",
                                 reason="no_piece" if piece is None else "no_pairs", summary=None,
                                 now=ctx.clock.now())
            if not ctx.commit(STAGE_PROPOSE, "plan", write):
                return
            log("lab_plan", night=ctx.night, piece=piece or "none", pairs=len(pairs),
                members=len({p["guid"] for p in pairs}), authored=int(piece in prefer))
        run = _run(ctx, source)
        if run is None or run["status"] != "running" or ctx.done(STAGE_PROPOSE, "propose"):
            return
        budget = Budget(cfg, run["spent"], ctx.clock)
        if cfg.variants == 0 or budget.left() <= 0:
            ctx.commit(STAGE_PROPOSE, "propose", _spend(ctx, run["id"], budget))
            return
        piece = run["piece"]
        with ctx.store.transaction():
            feedback = store.feedback(ctx.store.conn, piece)
            earlier = store.earlier_texts(ctx.store.conn, piece)
            names_rx = _names_rx(ctx)
        # the hand-written variants waiting for this piece go first (lab/authored.py)
        kept, refused = authored.waiting(ctx.settings.data_dir, piece, earlier=earlier,
                                         names_rx=names_rx)
        kept = kept[:cfg.variants]
        written = len(kept)
        left = cfg.variants - written
        asks = 0

        async def ask(question: str, k: int) -> list:
            reply = await ctx.reasoner.json(propose.SYSTEM, question, propose.response_schema(k),
                                            temperature=PROPOSE_TEMPERATURE)
            more, codes = propose.judge(piece, reply, k=k, names_rx=names_rx,
                                        earlier=[*earlier, *(v["text"] for v in kept)])
            kept.extend(more)
            refused.extend(c for c in codes if c is not None)
            return [reply, codes, len(more)]
        try:
            if left > 0:
                first = propose.prompt(piece, k=left, feedback=feedback)
                reply, codes, got = await ask(first, left)
                asks = 1
                # the screen refused some and slots are left: once more, with what was wrong
                faults = [c for c in codes if c not in (None, "extra")]
                if got < left and faults and budget.left() > 0:
                    asks = 2
                    await ask(propose.retry_prompt(piece, first, reply, codes, k=left - got),
                              left - got)
        except ReflectionError as err:
            if err.fatal:
                raise
            if not kept:
                ctx.fail(STAGE_PROPOSE, "propose", ReflectionError("lab_propose_failed", fatal=False))
                return
            refused.append("llm")

        def write(s):
            for v in kept:
                vid = store.add_variant(s.conn, run["id"], piece, v["text"], v["diff"],
                                        ctx.clock.now())
                if "name" in v:
                    s.set_meta(f"{authored.META_PREFIX}{vid}", v["name"])
            _spend(ctx, run["id"], budget)(s)
        if ctx.commit(STAGE_PROPOSE, "propose", write):
            log("lab_proposed", night=ctx.night, run=run["id"], piece=piece, kept=len(kept),
                authored=written, asks=asks, refused=len(refused),
                codes=",".join(sorted(set(refused))) or "-")
    return stage_lab_propose


def _names_rx(ctx):
    from leeroy_mind.turing import judge
    from leeroy_mind.turing.corpus import names_by_guid, persona_names
    from leeroy_mind.turing.night import his_names
    conn = ctx.store.conn
    owners = judge.name_owners(names_by_guid(conn, human=ctx.human, his=his_names(ctx.ident)),
                               human=ctx.human, his_names=his_names(ctx.ident),
                               aliases=persona_names(conn))
    from leeroy_mind.turing.corpus import name_pattern
    return name_pattern(owners, min_len=3)


# -- the battery ---------------------------------------------------------------------------------

def runs_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "scratch" / "lab" / "runs"


def _transcript_path(data_dir, run_id: int, arm: str) -> Path:
    return runs_dir(data_dir) / str(run_id) / f"{arm}.json"


def _keep_only(data_dir, run_id: int) -> None:
    folder = runs_dir(data_dir)
    if not folder.is_dir():
        return
    for p in folder.iterdir():
        if p.is_dir() and not p.is_symlink() and p.name != str(run_id):
            shutil.rmtree(p, ignore_errors=True)


def drop_copy(data_dir) -> int:
    """The lab's copy of the guild (scratch/lab/mind-data with its library.db, and the
    snapshot), once its run needs it no more: about 2.6 GB a night otherwise stayed until the
    next night replaced it. The transcripts (runs/) and child.log stay. Bytes freed; 0 when a
    child still holds a file (the next pass, or the scratch sweep, takes it)."""
    layout = child.prepare.lab_layout(Path(data_dir))
    if not (layout.root / child.prepare.MARKER).is_file():
        return 0
    freed = 0
    for folder in (layout.data, layout.snapshot):
        if not folder.is_dir() or folder.is_symlink() or folder.is_junction():
            continue
        size = sum(p.stat().st_size for p in folder.rglob("*") if p.is_file() and not p.is_symlink())
        try:
            shutil.rmtree(folder)
            freed += size
        except OSError:
            pass
    return freed


def make_battery(cfg, *, source: str = "night", make_child=None):
    make_child = make_child or (lambda data_dir, seed: child.SubprocessChild(data_dir, seed=seed))

    async def stage_lab(ctx) -> None:
        run = _run(ctx, source)
        if run is None or run["status"] != "running":
            return
        arms = _arms(ctx, run)
        todo = [a for a in arms if not ctx.done(STAGE_BATTERY, f"arm:{a}")]
        if not todo:
            return
        try:
            await _battery(ctx, run, arms, todo)
        finally:
            # Every arm settled, or the run failed: the copy is not read again (a resumed
            # battery with arms still to run keeps it; its snapshot unit is not taken twice).
            after = _run(ctx, source)
            if (after is None or after["status"] != "running"
                    or all(ctx.done(STAGE_BATTERY, f"arm:{a}") for a in arms)):
                freed = await asyncio.to_thread(drop_copy, ctx.settings.data_dir)
                if freed:
                    log("lab_copy_removed", night=ctx.night, bytes=freed)

    async def _battery(ctx, run, arms, todo) -> None:
        reason = _held(ctx)
        if reason is not None:
            log("lab_waiting", night=ctx.night, stage=STAGE_BATTERY, reason=reason)
            return
        data_dir = Path(ctx.settings.data_dir)
        budget = Budget(cfg, run["spent"], ctx.clock)
        if budget.left(BATTERY_SHARE) <= 0:
            for arm in todo:
                ctx.commit(STAGE_BATTERY, f"arm:{arm}", _spend(ctx, run["id"], budget))
            log("lab_budget", night=ctx.night, run=run["id"], stage=STAGE_BATTERY, arms=len(todo))
            return
        try:
            if not ctx.done(STAGE_BATTERY, "snapshot"):
                await asyncio.to_thread(child.take, data_dir)
                if not ctx.commit(STAGE_BATTERY, "snapshot", _spend(ctx, run["id"], budget)):
                    return
            await asyncio.to_thread(_keep_only, data_dir, run["id"])
        except Exception as e:
            _failed(ctx, run, budget, f"snapshot_{type(e).__name__}")
            return
        with ctx.store.transaction():
            variants = {f"v{v['id']}": v for v in store.variants(ctx.store.conn, run["id"])}
        scripts = {s.id: s for s in battery.load()}
        pairs = [p for p in run["plan"]["pairs"] if p["script"] in scripts]
        names = {p["guid"]: _name(ctx, p["guid"]) for p in pairs}
        sandbox = make_child(data_dir, _seed(run["seed"]))
        current: dict = {}

        def commit_arm(arm: str, collected: list, complete: bool) -> None:
            _save(data_dir, run["id"], arm, collected)
            rows = child.score_rows(collected)

            def write(s):
                store.put_scores(s.conn, run["id"], arm, rows)
                store.set_spent(s.conn, run["id"], budget.used())
            if ctx.commit(STAGE_BATTERY, f"arm:{arm}", write):
                log("lab_arm", night=ctx.night, run=run["id"], arm=arm, pairs=len(collected),
                    lines=len(rows), answered=sum(r["answered"] for r in rows),
                    applied=sum(r["applied"] for r in rows), complete=int(complete))
        timer = asyncio.timeout(max(1.0, budget.left(BATTERY_SHARE)))
        try:
            async with timer:
                url = await sandbox.start()
                fresh = True
                for arm in todo:
                    variant = variants.get(arm)
                    overlays = {variant["piece"]: variant["text"]} if variant is not None else {}
                    current = {"arm": arm, "collected": []}
                    _, complete = await child.run_arm(
                        url, pairs, scripts, names, piece=run["piece"], overlays=overlays,
                        reset=not fresh or bool(overlays), deadline=budget.deadline(BATTERY_SHARE),
                        clock=ctx.clock.now, out=current["collected"])
                    fresh = False
                    commit_arm(arm, current["collected"], complete)
                    current = {}
                    if not complete:
                        break
        except TimeoutError as e:
            sandbox.kill()
            if not timer.expired():  # the child's own start or a request timed out
                _failed(ctx, run, budget, f"child_{type(e).__name__}")
            else:
                if current:
                    commit_arm(current["arm"], current["collected"], False)
                log("lab_budget", night=ctx.night, run=run["id"], stage=STAGE_BATTERY)
        except Exception as e:
            sandbox.kill()
            _failed(ctx, run, budget, f"child_{type(e).__name__}")
        except BaseException:
            sandbox.kill()  # a cancel (his login, a lease, the mind stopping): at once
            raise
        else:
            await sandbox.stop()
        # arms the budget left unrun are settled empty, so grading can finish the run
        for arm in arms:
            if not ctx.done(STAGE_BATTERY, f"arm:{arm}") and _run(ctx, source)["status"] == "running" \
                    and budget.left(BATTERY_SHARE) <= 0:
                ctx.commit(STAGE_BATTERY, f"arm:{arm}", _spend(ctx, run["id"], budget))
    return stage_lab


def _failed(ctx, run, budget: Budget, code: str) -> None:
    """The run ends `failed` with a code (a broken copy or child does not fix itself tonight)."""
    def write(s):
        store.finish(s.conn, run["id"], status="failed", reason=code[:60], summary=None,
                     now=ctx.clock.now())
        store.set_spent(s.conn, run["id"], budget.used())
    ctx.commit(STAGE_BATTERY, "failed", write)
    ctx.fail(STAGE_BATTERY, "child", ValueError(f"lab_{code}"))


def _seed(text: str) -> int:
    import hashlib
    return int.from_bytes(hashlib.sha256(text.encode()).digest()[:4], "big")


def _save(data_dir, run_id: int, arm: str, transcripts: list[dict]) -> None:
    path = _transcript_path(data_dir, run_id, arm)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(json.dumps(transcripts, ensure_ascii=False).encode("utf-8"))


def _load(data_dir, run_id: int, arm: str) -> list[dict]:
    try:
        return json.loads(_transcript_path(data_dir, run_id, arm).read_bytes())
    except (OSError, ValueError):
        return []


# -- the judge's scores ----------------------------------------------------------------------------

def make_score(cfg, *, source: str = "night"):
    async def stage_lab_score(ctx) -> None:
        run = _run(ctx, source)
        if (run is None or run["status"] != "running" or _grader(run) != "tells"
                or ctx.done(STAGE_SCORE, "scored")):
            return
        arms = _arms(ctx, run)
        if not all(ctx.done(STAGE_BATTERY, f"arm:{a}") for a in arms):
            return
        reason = _held(ctx)
        if reason is not None:
            log("lab_waiting", night=ctx.night, stage=STAGE_SCORE, reason=reason)
            return
        data_dir = Path(ctx.settings.data_dir)
        budget = Budget(cfg, run["spent"], ctx.clock)
        transcripts = {a: {t["pair"]: t for t in _load(data_dir, run["id"], a)} for a in arms}
        units = [f"score:{a}:{p}" for a in arms for p in sorted(transcripts[a])]
        # each variant's answers against the control's to the same lines
        units += [f"prefer:{a}:{p}" for a in arms if a != "control"
                  for p in sorted(transcripts[a]) if p in transcripts.get("control", {})]
        reasoner = live_reasoner(ctx)
        before = (reasoner.calls, reasoner.seconds)
        base = _seed(run["seed"])

        async def one(unit: str) -> None:
            kind, arm, pair = unit.split(":")
            if kind == "prefer":
                shares = await _prefer(reasoner, transcripts[arm][int(pair)],
                                       transcripts["control"][int(pair)], base)

                def write(s):
                    store.set_prefer(s.conn, run["id"], arm, int(pair), shares)
                    store.set_spent(s.conn, run["id"], budget.used())
            else:
                marks = await _score(reasoner, transcripts[arm][int(pair)], base)

                def write(s):
                    store.set_score(s.conn, run["id"], arm, int(pair), marks)
                    store.set_spent(s.conn, run["id"], budget.used())
            ctx.commit(STAGE_SCORE, unit, write)

        left = budget.left()
        cut = left <= 0
        if not cut:
            try:
                async with asyncio.timeout(left):
                    await ctx.map_units(STAGE_SCORE, units, one)
            except TimeoutError:
                cut = True
        calls, seconds = reasoner.calls - before[0], reasoner.seconds - before[1]
        if reasoner is not ctx.reasoner:  # the runner meters the context's own reasoner
            ctx.reasoner.calls += calls
            ctx.reasoner.seconds += seconds
        if ctx.commit(STAGE_SCORE, "scored", _spend(ctx, run["id"], budget)):
            log("lab_scored", night=ctx.night, run=run["id"], units=len(units),
                done=sum(1 for u in units if ctx.done(STAGE_SCORE, u)),
                calls=calls, seconds=round(seconds, 1), cut=int(cut))
    return stage_lab_score


async def _score(reasoner, t: dict, base: int) -> list[tuple[float, int]]:
    """(score, calls) for each turn of a pair: her answer judged with the pair's earlier turns
    as its context. The seed is the run's, the pair's and the turn's, never the arm's: the same
    question is asked the same way of every arm."""
    out, context, earlier = [], [], []
    for n, turn in enumerate(t["turns"], 1):
        got = await judge.score(reasoner, turn["his"], turn["reply"], mode=judge.LAB_MODE,
                                context=tuple(context), earlier=tuple(earlier),
                                rewritten=bool(turn.get("rewritten")),
                                seed=judge.seed_of(base, t["pair"], n))
        out.append((got["score"], got["calls"]))
        context.append(("him", turn["his"]))
        if turn["reply"]:
            context.append(("her", turn["reply"]))
            earlier.append(turn["reply"])
    return out


async def _prefer(reasoner, variant: dict, control: dict, base: int) -> list[float | None]:
    """For each turn of a pair, the variant's answer's share against the control's answer to
    the same line (judge.prefer: asked both ways round, 2 calls), each read with its own arm's
    turns before; None where either arm said nothing or the lines differ."""
    out, ctx_v, ctx_c = [], [], []
    for n, (v, c) in enumerate(zip(variant["turns"], control["turns"]), 1):
        if v["reply"] and c["reply"] and v["his"] == c["his"]:
            out.append(await judge.prefer(reasoner, (v["his"], v["reply"], tuple(ctx_v)),
                                          (c["his"], c["reply"], tuple(ctx_c)),
                                          seed=judge.seed_of(base, variant["pair"], n, "prefer")))
        else:
            out.append(None)
        for ctx, turn in ((ctx_v, v), (ctx_c, c)):
            ctx.append(("him", turn["his"]))
            if turn["reply"]:
                ctx.append(("her", turn["reply"]))
    out += [None] * (len(variant["turns"]) - len(out))
    return out


# -- grading ---------------------------------------------------------------------------------------

def make_grade(cfg, *, source: str = "night"):
    async def stage_lab_grade(ctx) -> None:
        run = _run(ctx, source)
        if run is None or run["status"] != "running" or ctx.done(STAGE_GRADE, "finish"):
            return
        arms = _arms(ctx, run)
        if not all(ctx.done(STAGE_BATTERY, f"arm:{a}") for a in arms):
            return  # the battery waits for the next night (a lease, the GPU paused)
        tells = _grader(run) == "tells"
        if tells and not ctx.done(STAGE_SCORE, "scored"):
            return  # so does the judge's scoring
        data_dir = Path(ctx.settings.data_dir)
        budget = Budget(cfg, run["spent"], ctx.clock)
        transcripts = {a: {t["pair"]: t for t in _load(data_dir, run["id"], a)} for a in arms}
        judged_pairs = sorted(p["pair"] for p in run["plan"]["pairs"])[:run["plan"].get("judge", cfg.judge)]
        units = [] if tells else [f"rubric:{a}:{p}" for a in arms for p in sorted(transcripts[a])]
        units += [f"judge:{a}:{p}" for a in arms for p in judged_pairs if p in transcripts[a]]
        names: dict = {}

        async def one(unit: str) -> None:
            kind, arm, pair = unit.split(":")
            t = transcripts[arm][int(pair)]
            if kind == "rubric":
                grades = await _rubric(ctx, t)
                if grades is None:
                    raise ValueError("lab_rubric_invalid")

                def write(s):
                    store.set_rubric(s.conn, run["id"], arm, int(pair), grades)
                    store.set_spent(s.conn, run["id"], budget.used())
            else:
                if not names:
                    names["owners"] = _owners(ctx)
                seen = await _judge(ctx, t, names["owners"])
                if seen is None:
                    raise ValueError("lab_judge_invalid")

                def write(s):
                    store.set_judged(s.conn, run["id"], arm, int(pair), seen["her"], seen["him"])
                    store.set_spent(s.conn, run["id"], budget.used())
            ctx.commit(STAGE_GRADE, unit, write)

        cut = tells and any(not ctx.done(STAGE_SCORE, f"score:{a}:{p}")
                            for a in arms for p in transcripts[a])
        left = budget.left()
        if left <= 0:
            cut = cut or any(not ctx.done(STAGE_GRADE, u) for u in units)
        else:
            try:
                async with asyncio.timeout(left):
                    await ctx.map_units(STAGE_GRADE, units, one)
            except TimeoutError:
                cut = True
        planned = len(run["plan"]["pairs"])
        short = any(len(transcripts[a]) < planned for a in arms)
        learn_cfg = _learn_cfg(data_dir)
        done: dict = {}

        def finish(s):
            result = promote.promote(s.conn, run["id"], settings=learn_cfg, now=ctx.clock.now(),
                                     budget_cut=cut or short, key="score" if tells else "rubric",
                                     allow=bool(run["plan"].get("promote", True)))
            summary = {"arms": result["summary"], "grader": _grader(run),
                       "promote": bool(run["plan"].get("promote", True)),
                       "outcomes": {str(k): v for k, v in result["outcomes"].items()},
                       "deltas": {str(k): v for k, v in result["deltas"].items()},
                       "promoted": result["promoted"], "cut": bool(cut or short)}
            store.finish(s.conn, run["id"], status="budget" if cut or short else "done",
                         reason="budget" if cut or short else None, summary=summary,
                         now=ctx.clock.now())
            store.set_spent(s.conn, run["id"], budget.used())
            done["result"] = result
        if ctx.commit(STAGE_GRADE, "finish", finish):
            result = done["result"]
            deltas = [d for d in result["deltas"].values() if d is not None]
            log("lab_done", night=ctx.night, run=run["id"], piece=run["piece"],
                variants=len(result["outcomes"]), promoted=result["promoted"] or "-",
                control=result["summary"].get("control", {}).get("score" if tells else "rubric"),
                best_delta=max(deltas) if deltas else None, cut=int(cut or short),
                grader=_grader(run), promote=int(bool(run["plan"].get("promote", True))))
    return stage_lab_grade


def _learn_cfg(data_dir):
    from leeroy_mind.learn.config import load_learn_config
    return load_learn_config(Path(data_dir) / "mind.toml")


def _name(ctx, guid: int) -> str:
    try:
        return ctx.name(guid)
    except KeyError:
        return ""


def _owners(ctx):
    from leeroy_mind.turing import judge
    from leeroy_mind.turing.corpus import names_by_guid, persona_names
    from leeroy_mind.turing.night import his_names
    with ctx.store.transaction():
        conn = ctx.store.conn
        names = his_names(ctx.ident)
        return judge.name_owners(names_by_guid(conn, human=ctx.human, his=names),
                                 human=ctx.human, his_names=names, aliases=persona_names(conn))


async def _judge(ctx, t: dict, owners) -> dict | None:
    """The blind judge on the pair's window ({"her", "him"}); she said nothing: not human."""
    from leeroy_mind.turing import judge
    lines = grade.window(t["turns"], human=ctx.human, guid=t["guid"], channel=t["channel"])
    if all(line.human for line in lines):
        return {"her": 0, "him": None}
    by_guid = {ctx.human: ctx.ident.called, t["guid"]: _name(ctx, t["guid"])}
    shown = judge.render(lines, by_guid=by_guid, human=ctx.human, owners=owners)
    try:
        reply = await ctx.reasoner.json(judge.SYSTEM, shown.prompt,
                                        judge.response_schema(list(shown.letters)))
    except ReflectionError as err:
        raise ReflectionError("lab_judge_failed", fatal=err.fatal) from None
    return grade.judged(reply, shown.letters, human=ctx.human)


async def _rubric(ctx, t: dict) -> list[tuple[int, str]] | None:
    """Her answers' grades, (score, code) per turn: an answer she never gave is 0 `empty`
    without asking; None for a reply that does not parse."""
    turns = t["turns"]
    if not any(turn["reply"] for turn in turns):
        return [(0, "empty")] * len(turns)
    try:
        card = ctx.card(t["guid"])
    except KeyError:
        card = ""
    try:
        reply = await ctx.reasoner.json(grade.SYSTEM, grade.rubric_prompt(card, turns),
                                        grade.rubric_schema(len(turns)))
    except ReflectionError as err:
        raise ReflectionError("lab_rubric_failed", fatal=err.fatal) from None
    grades = grade.rubric_parse(reply, len(turns))
    if grades is None:
        return None
    return [(0, "empty") if not turn["reply"] else g for turn, g in zip(turns, grades)]
