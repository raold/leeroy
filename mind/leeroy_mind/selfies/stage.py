"""The night's profile photos (P2 Task 10): stage "selfies", an image stage (the runner has had
the arbiter unload every chat model, nightly/gpu.py make_room), renders four candidates for each
member with looks and a photo kind who has no photo and no set waiting for his choice, and for
each "4 more" he asked for.

Units, in order: "queue", a first set queued for each such member (away ones included); then
"<guid>:<set>" for each queued set, his "4 more" first, at most selfie_per_night a night counting
the sets done tonight. A member who lost his looks or photo kind since his set was queued is
skipped and his set stays queued. A set renders into its staging folder, which is renamed to the
set's folder just before the one commit that marks it ready; a folder a crash left without its
ready row is cleared before the set is rendered again (files.clear_stale).

Each slot tries up to TRIES seeds, the night's own (seed_for), so a resumed night asks for the
same images. A flagged image is never written and the next seed is tried; a slot flagged TRIES
times refuses the set (SetRefused), that unit's failure only. An oom answer (mod-ollama-chat
loaded Orion meanwhile) makes room and repeats that attempt once; a worker that refuses to load
for want of VRAM is started once more after making room. CONTENDED worker failures in a row end
the stage for the night, keeping the sets it finished; a worker that died is started again once.
A live lease refuses make_room (LiveLeaseHeld): that ends the stage at once and the runner logs
it, with no unit failed.

However the stage ends, his login's cancel included, the worker is stopped: its whole process
tree is gone before the hand-over preloads the live model. The cancel path makes no arbiter
call. Logs carry ids, counts, scores and timings, never a prompt, persona text or a name."""
import hashlib
import logging
import os
import time
from collections import Counter
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable

from leeroy_mind.logs import log
from leeroy_mind.nightly.budget import register_counter
from leeroy_mind.nightly.gpu import LiveLeaseHeld
from leeroy_mind.personas.schema import Persona
from leeroy_mind.wow import CAST_MIN_LEVEL

from . import files
from .config import SelfieCfg, worker_command, worker_env
from .prompt import PromptSpec, build_prompt, renderable
from .store import Candidate, ensure_first_sets, mark_ready, needing_first, queued_sets
from .worker import RenderRequest, SelfieWorker, WorkerError

if TYPE_CHECKING:
    from leeroy_mind.nightly.runner import NightContext

STAGE = "selfies"
SET_SIZE = 4
TRIES = 3  # seeds a slot, before a flagged slot refuses the set
THUMB_PX = 512
CONTENDED = 2  # worker failures in a row that end the stage for the night
QUEUE = "queue"


class SetRefused(Exception):
    """Every seed of one slot was flagged by the NSFW check: the set stays queued."""


def seed_for(night: str, guid: int, set_no: int, idx: int, attempt: int) -> int:
    """The first 4 bytes, big-endian, of the SHA-256 of "night|guid|set|idx|attempt"."""
    digest = hashlib.sha256(f"{night}|{guid}|{set_no}|{idx}|{attempt}".encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big")


async def render_candidates(worker, spec: PromptSpec, out_dir: Path, *, night: str, guid: int,
                            set_no: int, cfg: SelfieCfg,
                            make_room: Callable[[], Awaitable[None]]) -> list[Candidate]:
    """Slots 0-3 into out_dir/c<idx>.jpg and c<idx>.thumb.jpg, one Candidate each. Raises
    SetRefused when a slot is flagged TRIES times, WorkerError for a second oom of a slot or any
    other worker failure, and whatever make_room raises."""
    out_dir = Path(out_dir).absolute()
    made = []
    for idx in range(SET_SIZE):
        made.append(await _slot(worker, spec, out_dir, idx, night=night, guid=guid,
                                set_no=set_no, cfg=cfg, make_room=make_room))
    return made


async def _slot(worker, spec: PromptSpec, out_dir: Path, idx: int, *, night: str, guid: int,
                set_no: int, cfg: SelfieCfg,
                make_room: Callable[[], Awaitable[None]]) -> Candidate:
    out, thumb = out_dir / f"c{idx}.jpg", out_dir / f"c{idx}.thumb.jpg"
    roomed = False
    for attempt in range(TRIES):
        seed = seed_for(night, guid, set_no, idx, attempt)
        req = RenderRequest(spec.phrases, spec.negative, seed, cfg.steps, cfg.guidance, cfg.size,
                            out, thumb, THUMB_PX, cfg.nsfw_max)
        rid = f"{guid}:{set_no}:{idx}:{attempt}"
        try:
            got = await worker.render(req, rid=rid)
        except WorkerError as e:
            if e.reason != "oom" or roomed:
                raise
            roomed = True
            await make_room()
            got = await worker.render(req, rid=rid)
        if not got.flagged and got.nsfw < cfg.nsfw_max:
            return Candidate(idx, seed, attempt + 1, got.nsfw, got.prompt)
        for path in (out, thumb):  # never kept, whatever a worker wrote
            with suppress(FileNotFoundError):
                path.unlink()
        log("selfies_flagged", night=night, guid=guid, set=set_no, idx=idx, attempt=attempt,
            nsfw=round(got.nsfw, 3))
    raise SetRefused(f"slot {idx}: {TRIES} seeds flagged")


def _stored(ctx: "NightContext", guid: int) -> Persona | None:
    try:
        return ctx.base(guid)
    except KeyError:
        return None


def _without_persona(ctx: "NightContext") -> int:
    """Roster members of the cast's level with no stored persona, so not in ctx.members."""
    stored = ctx.store.persona_versions()
    return sum(1 for g, m in ctx.store.cast().items()
               if g != ctx.human and m.level >= CAST_MIN_LEVEL and g not in stored)


def _queue(ctx: "NightContext") -> None:
    if ctx.done(STAGE, QUEUE):
        return
    members, missing = [], Counter()
    for guid in ctx.members(include_away=True):
        why = renderable(_stored(ctx, guid))
        if why is None:
            members.append(guid)
        else:
            missing[why] += 1
    if n := _without_persona(ctx):
        missing["no persona"] += n
    made = 0

    def write(store) -> None:
        nonlocal made
        made = ensure_first_sets(store, members, now=ctx.clock.now())
    if ctx.commit(STAGE, QUEUE, write):
        log("selfies_queued", night=ctx.night, queued=made, missing=dict(sorted(missing.items())))


def _done_sets(ctx: "NightContext") -> int:
    with ctx.store.transaction():
        return ctx.store.conn.execute(
            "SELECT count(*) FROM night_step WHERE night=? AND stage=? AND unit<>?",
            (ctx.night, STAGE, QUEUE)).fetchone()[0]


def _todo(ctx: "NightContext") -> list[str]:
    """The queued sets not done tonight, within what selfie_per_night leaves after the sets
    done tonight (a resumed night does not start the count again)."""
    room = max(0, ctx.settings.selfie_per_night - _done_sets(ctx))
    units = [f"{s.guid}:{s.set_no}" for s in queued_sets(ctx.store)]
    return [u for u in units if not ctx.done(STAGE, u)][:room]


def _unavailable(ctx: "NightContext", err: BaseException, reason: str) -> None:
    ctx.fail(STAGE, "worker", err)
    log("selfies_unavailable", level=logging.WARNING, night=ctx.night, reason=reason)


async def _start(ctx: "NightContext", worker: SelfieWorker) -> bool:
    """The worker started and ready. A refusal for VRAM makes room and starts it once more. A
    stop that arrived meanwhile is his login, not a broken worker: logged as such, no failure."""
    try:
        try:
            await worker.start()
        except WorkerError as e:
            if e.reason != "vram":
                raise
            await ctx.arbiter.make_room()  # a chat model loaded since the runner made room
            await worker.start()
    except LiveLeaseHeld:
        raise
    except WorkerError as e:
        if e.reason == "stopped":
            log("selfies_preempted", night=ctx.night)
        else:
            _unavailable(ctx, e, e.reason)
        return False
    except Exception as e:  # OSError: the program cannot be started; a make_room that failed
        _unavailable(ctx, e, type(e).__name__)
        return False
    return True


def _discard(ctx: "NightContext", guid: int, set_no: int) -> None:
    """What a failed render left (not on the cancel path: clear_stale clears that later)."""
    with suppress(OSError):
        files.clear_stale(ctx.settings.data_dir, guid, set_no)


async def _render_set(ctx: "NightContext", cfg: SelfieCfg, worker: SelfieWorker, p: Persona,
                      guid: int, set_no: int) -> list[Candidate]:
    data = ctx.settings.data_dir
    files.clear_stale(data, guid, set_no)
    staging = files.staging_dir(data, guid, set_no)
    staging.mkdir(parents=True)
    made = await render_candidates(worker, build_prompt(p), staging, night=ctx.night, guid=guid,
                                   set_no=set_no, cfg=cfg, make_room=ctx.arbiter.make_room)
    os.replace(staging, files.set_dir(data, guid, set_no))
    return made


def _ready(ctx: "NightContext", guid: int, set_no: int, made: list[Candidate]):
    def write(store) -> None:
        mark_ready(store, guid, set_no, made, now=ctx.clock.now(), night=ctx.night,
                   persona_version=store.persona_versions()[guid])
    return write


async def _render(ctx: "NightContext", cfg: SelfieCfg, worker: SelfieWorker,
                  todo: list[str]) -> None:
    if not await _start(ctx, worker):
        return
    tally = Counter()
    contended, restarted = 0, False
    for n, unit in enumerate(todo):
        guid, set_no = (int(x) for x in unit.split(":"))
        p = _stored(ctx, guid)
        if renderable(p) is not None:
            if ctx.skip(STAGE, unit):
                tally["skipped"] += 1
            continue
        began = time.monotonic()
        try:
            made = await _render_set(ctx, cfg, worker, p, guid, set_no)
        except LiveLeaseHeld:
            raise
        except WorkerError as e:
            _discard(ctx, guid, set_no)
            ctx.fail(STAGE, unit, e)
            tally["failed"] += 1
            contended += 1
            if contended >= CONTENDED or (not worker.running and restarted):
                log("selfies_stopped", level=logging.WARNING, night=ctx.night, reason=e.reason,
                    left=len(todo) - n - 1)
                break
            if not worker.running:
                restarted = True
                if not await _start(ctx, worker):
                    break
            continue
        except Exception as e:  # SetRefused; a folder that could not be made or moved
            _discard(ctx, guid, set_no)
            ctx.fail(STAGE, unit, e)
            if isinstance(e, SetRefused):
                tally["refused"] += 1
                contended = 0  # the worker rendered: not contention
            else:
                tally["failed"] += 1
            continue
        contended = 0
        if ctx.commit(STAGE, unit, _ready(ctx, guid, set_no, made)):
            tally["ready"] += 1
            log("selfies_set_ready", night=ctx.night, guid=guid, set=set_no,
                seconds=round(time.monotonic() - began, 1), tries=sum(c.tries for c in made))
        else:
            tally["failed"] += 1
    log("selfies_done", night=ctx.night, **{k: tally[k] for k in
                                            ("ready", "refused", "failed", "skipped")})


def make_stage(cfg: SelfieCfg, *, worker: Callable[[], SelfieWorker] | None = None
               ) -> Callable[["NightContext"], Awaitable[None]]:
    """The "selfies" stage, an image stage. `worker` makes the night's worker (tests pass the
    fake); by default the selfie/ venv's, from `cfg`."""
    def make() -> SelfieWorker:
        if worker is not None:
            return worker()
        return SelfieWorker(worker_command(cfg), env=worker_env(cfg))

    async def stage_selfies(ctx: "NightContext") -> None:
        _queue(ctx)
        todo = _todo(ctx)
        if not todo:
            return
        w = make()
        try:
            await _render(ctx, cfg, w, todo)
        finally:
            await w.stop()  # his login's cancel too: the tree is gone before the hand-over
    return stage_selfies


def _count(ctx: "NightContext") -> int:
    """The sets the stage would render tonight: those queued and the first ones it would queue."""
    members = [g for g in ctx.members(include_away=True) if renderable(_stored(ctx, g)) is None]
    return min(ctx.settings.selfie_per_night,
               len(queued_sets(ctx.store)) + len(needing_first(ctx.store, members)))


register_counter(STAGE, _count)
