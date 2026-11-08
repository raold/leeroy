"""The night's profile photos (P2 Task 10): stage "selfies", an image stage (the runner has had
the arbiter unload every chat model, nightly/gpu.py make_room), renders four candidates for each
member with looks and a photo kind who has no photo and no set waiting for his choice, and for
each "4 more" he asked for.

Units, in order: "queue" (its receipt is the night's first queue step; every later pass of the
stage and every round of its render loop queues again, skipping whoever already has a photo or a
set, so a member chosen late or a "4 more" asked meanwhile renders in the same night), a first set
queued for each such member (away ones included); then
"<guid>:<set>" for each queued set, his "4 more" first, at most selfie_per_night a night counting
the sets done tonight. The runner asks wants_gpu (which queues) before it makes room, so a night
with nothing to render leaves the chat models loaded. A member who lost his looks or photo kind
since his set was queued is skipped and his set stays queued. A set renders into its staging
folder, which is renamed to the set's folder (files.publish_set) just before the one commit that
marks it ready, once each candidate's two files are there; a folder a crash left without its
ready row is cleared before the set is rendered again (files.clear_stale). Both retry a file
that Defender or the indexer holds.

Each slot tries up to TRIES seeds, the night's own (seed_for), so a resumed night asks for the
same images. A flagged image is never written and the next seed is tried; a slot flagged TRIES
times refuses the set (SetRefused), that unit's failure only. An oom answer (mod-ollama-chat
loaded Orion meanwhile) makes room and repeats that attempt once; a worker that refuses to load
for want of VRAM is asked once more after making room (start_worker), keeping the pipeline it
loaded on the CPU, so that check comes within seconds of the unload. CONTENDED worker failures
in a row end the stage for the night, keeping the sets it finished; a make_room that fails
(Ollama down or hung) counts as one (RoomFailed), and a worker that died is started again
once. No render is sent while a live lease is held: that, or a lease refusing make_room
(LiveLeaseHeld), ends the stage at once and the runner logs it, with no unit failed.

However the stage ends, his login's cancel included, the worker is stopped and the stop runs to
its end before the cancel goes on (worker.to_the_end): its whole process tree is gone before the
hand-over preloads the live model. The cancel path makes no arbiter call. Logs carry ids,
counts, scores and timings, never a prompt, persona text or a name."""
import hashlib
import logging
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
from .kind import chosen_style
from .style import PLAIN
from .slot import SetRefused, TRIES, render_slot
from .prompt import HELD, LUST_HELD, PromptSpec, build_prompt, renderable
from .store import Candidate, ensure_first_sets, mark_ready, needing_first, queued_sets
from .worker import RenderRequest, SelfieWorker, WorkerError, to_the_end

if TYPE_CHECKING:
    from leeroy_mind.nightly.runner import NightContext

STAGE = "selfies"
SET_SIZE = 4
THUMB_PX = 512
CONTENDED = 2  # worker failures in a row that end the stage for the night
QUEUE = "queue"


class RoomFailed(WorkerError):
    """make_room failed after an oom (Ollama down or hung, not a lease's refusal): the GPU is
    still contended, so it counts as a worker failure."""

    def __init__(self):
        super().__init__("room")


def seed_for(night: str, guid: int, set_no: int, idx: int, attempt: int) -> int:
    """The first 4 bytes, big-endian, of the SHA-256 of "night|guid|set|idx|attempt"."""
    digest = hashlib.sha256(f"{night}|{guid}|{set_no}|{idx}|{attempt}".encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big")


async def render_candidates(worker, spec: PromptSpec, out_dir: Path, *, night: str, guid: int,
                            set_no: int, cfg: SelfieCfg, make_room: Callable[[], Awaitable[None]],
                            leases: Callable[[], frozenset[str]] | None = None
                            ) -> list[Candidate]:
    """Slots 0-3 into out_dir/c<idx>.jpg and c<idx>.thumb.jpg, one Candidate each. `leases`
    (the arbiter's live holders) is asked before each render: while one is held, LiveLeaseHeld
    and nothing is sent. Raises SetRefused when a slot is flagged TRIES times, RoomFailed when
    make_room fails other than by LiveLeaseHeld, WorkerError for a second oom of a slot, an
    image answered as written whose two files are not both there ("unwritten") or any other
    worker failure, and LiveLeaseHeld."""
    out_dir = Path(out_dir).absolute()
    made = []
    for idx in range(SET_SIZE):
        made.append(await _slot(worker, spec, out_dir, idx, night=night, guid=guid,
                                set_no=set_no, cfg=cfg, make_room=make_room, leases=leases))
    return made


def _unleased(leases: Callable[[], frozenset[str]] | None) -> None:
    held = leases() if leases is not None else frozenset()
    if held:
        raise LiveLeaseHeld(held)


async def _room(make_room: Callable[[], Awaitable[None]], *, night: str, guid: int, set_no: int,
                idx: int) -> None:
    try:
        await make_room()
    except LiveLeaseHeld:
        raise
    except Exception as e:
        log("selfies_room_failed", level=logging.WARNING, night=night, guid=guid, set=set_no,
            idx=idx, error=type(e).__name__)
        raise RoomFailed() from e


async def _slot(worker, spec: PromptSpec, out_dir: Path, idx: int, *, night: str, guid: int,
                set_no: int, cfg: SelfieCfg, make_room: Callable[[], Awaitable[None]],
                leases: Callable[[], frozenset[str]] | None) -> Candidate:
    out, thumb = out_dir / f"c{idx}.jpg", out_dir / f"c{idx}.thumb.jpg"

    def request(attempt: int) -> RenderRequest:
        _unleased(leases)
        return RenderRequest(spec.phrases, spec.negative,
                             seed_for(night, guid, set_no, idx, attempt), cfg.steps, cfg.guidance,
                             cfg.size, out, thumb, THUMB_PX, cfg.nsfw_max)

    async def room() -> None:
        await _room(make_room, night=night, guid=guid, set_no=set_no, idx=idx)
        _unleased(leases)
    try:
        got, attempts = await render_slot(
            worker, request, rid=lambda attempt: f"{guid}:{set_no}:{idx}:{attempt}",
            make_room=room)
    except SetRefused:
        raise SetRefused(f"slot {idx}: {TRIES} seeds flagged") from None
    if not (out.is_file() and thumb.is_file()):  # a ready set never points at nothing
        raise WorkerError("unwritten")
    return Candidate(idx, seed_for(night, guid, set_no, idx, attempts - 1), attempts, got.nsfw,
                     got.prompt)


def _stored(ctx: "NightContext", guid: int) -> Persona | None:
    try:
        return ctx.base(guid)
    except KeyError:
        return None


def _held(ctx: "NightContext", p: Persona | None) -> str | None:
    """renderable() under the style that chose p's kind (a lust kind waits while she lacks the
    lust style)."""
    return renderable(p, chosen_style(ctx.store, p) if p is not None else PLAIN)


def _without_persona(ctx: "NightContext") -> int:
    """Roster members of the cast's level with no stored persona, so not in ctx.members."""
    stored = ctx.store.persona_versions()
    return sum(1 for g, m in ctx.store.cast().items()
               if g != ctx.human and m.level >= CAST_MIN_LEVEL and g not in stored)


def _queue(ctx: "NightContext") -> None:
    """Queue a first set for each member who needs one and has none. Runs on every pass of the
    stage (wants_gpu, the stage, each round of its render loop), so a member whose kind was
    chosen after an earlier pass is queued at the next; ensure_first_sets skips whoever already
    has a photo or a queued or waiting set, so nothing is queued twice. The "queue" receipt is
    written by the night's first pass only."""
    first = not ctx.done(STAGE, QUEUE)
    members, missing = [], Counter()
    for guid in ctx.members(include_away=True):
        why = _held(ctx, _stored(ctx, guid))
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
    if first:
        if not ctx.commit(STAGE, QUEUE, write):
            return
    else:
        write(ctx.store)
        if not made:
            return
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


async def start_worker(worker: SelfieWorker, make_room: Callable[[], Awaitable[None]]) -> None:
    """worker.start(); a refusal for VRAM (a chat model loaded since the room was made) makes
    room and asks once more. The worker keeps what it loaded on the CPU and waits for that
    retry, so the VRAM check comes within seconds of the unload. Raises what either raises."""
    try:
        await worker.start()
    except WorkerError as e:
        if e.reason != "vram":
            raise
        await make_room()
        await worker.start()


async def _start(ctx: "NightContext", worker: SelfieWorker) -> bool:
    """The worker started and ready (start_worker). A stop that arrived meanwhile is his login,
    not a broken worker: logged as such, no failure."""
    try:
        await start_worker(worker, ctx.arbiter.make_room)
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
    spec = build_prompt(p, kind_style=chosen_style(ctx.store, p))  # her stance at render time
    made = await render_candidates(worker, spec, staging, night=ctx.night, guid=guid,
                                   set_no=set_no, cfg=cfg, make_room=ctx.arbiter.make_room,
                                   leases=lambda: ctx.arbiter.holders)
    files.publish_set(data, guid, set_no)
    return made


def _ready(ctx: "NightContext", guid: int, set_no: int, made: list[Candidate]):
    def write(store) -> None:
        mark_ready(store, guid, set_no, made, now=ctx.clock.now(), night=ctx.night,
                   persona_version=store.persona_versions()[guid])
    return write


async def _render(ctx: "NightContext", cfg: SelfieCfg, worker: SelfieWorker) -> None:
    """Rounds of sets with the one worker: each round queues what is new (_queue) and renders
    the queued sets not yet tried this run, within the night's cap, so a "4 more" asked, or a
    member whose kind was chosen, while the stage runs is rendered in the same stage. Ends when
    a round finds nothing new or the worker is out of reach."""
    if not await _start(ctx, worker):
        return
    tally = Counter()
    state = {"contended": 0, "restarted": False}
    tried: set[str] = set()
    while True:
        _queue(ctx)
        todo = [u for u in _todo(ctx) if u not in tried]
        if not todo:
            break
        tried.update(todo)
        if not await _sets(ctx, cfg, worker, todo, tally, state):
            break
    log("selfies_done", night=ctx.night, **{k: tally[k] for k in
                                            ("ready", "refused", "failed", "skipped")})


async def _sets(ctx: "NightContext", cfg: SelfieCfg, worker: SelfieWorker, todo: list[str],
                tally: Counter, state: dict) -> bool:
    """One round's sets; False when the stage ends for the night (contended worker)."""
    contended, restarted = state["contended"], state["restarted"]
    for n, unit in enumerate(todo):
        guid, set_no = (int(x) for x in unit.split(":"))
        p = _stored(ctx, guid)
        why = _held(ctx, p)
        if why is not None:
            if why in HELD:  # her kind's text is never rendered plainly, nor without its phrase
                log("selfies_set_held", night=ctx.night, guid=guid, set=set_no,
                    reason="restyle" if why == LUST_HELD else "name")
            if ctx.skip(STAGE, unit):
                tally["skipped"] += 1
            continue
        began = time.monotonic()
        try:
            made = await _render_set(ctx, cfg, worker, p, guid, set_no)
        except LiveLeaseHeld:
            raise
        except WorkerError as e:  # RoomFailed too: contention either way
            _discard(ctx, guid, set_no)
            ctx.fail(STAGE, unit, e)
            tally["failed"] += 1
            contended += 1
            if contended >= CONTENDED or (not worker.running and restarted):
                log("selfies_stopped", level=logging.WARNING, night=ctx.night, reason=e.reason,
                    left=len(todo) - n - 1)
                return False
            if not worker.running:
                restarted = True
                state["restarted"] = True
                if not await _start(ctx, worker):
                    return False
            state["contended"] = contended
            continue
        except Exception as e:  # SetRefused; a folder that could not be made or moved
            _discard(ctx, guid, set_no)
            ctx.fail(STAGE, unit, e)
            if isinstance(e, SetRefused):
                tally["refused"] += 1
                contended = 0  # the worker rendered: not contention
            else:
                tally["failed"] += 1
            state["contended"] = contended
            continue
        contended = 0
        state["contended"] = 0
        if ctx.commit(STAGE, unit, _ready(ctx, guid, set_no, made)):
            tally["ready"] += 1
            log("selfies_set_ready", night=ctx.night, guid=guid, set=set_no,
                seconds=round(time.monotonic() - began, 1), tries=sum(c.tries for c in made))
        else:
            tally["failed"] += 1
    return True


def wants_gpu(ctx: "NightContext") -> bool:
    """The runner asks this before it makes room for the stage: tonight's first sets queued
    (the "queue" unit), then whether any set is left that can render now (a held set waits).
    False leaves the chat models where they are on a night with nothing to render."""
    _queue(ctx)
    return any(_held(ctx, _stored(ctx, int(unit.split(":")[0]))) is None for unit in _todo(ctx))


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
            await _render(ctx, cfg, w)
        finally:
            # his login's cancel too, landing here or before: the stop runs to its end, so the
            # tree is gone before the hand-over's preempt returns and the live model loads
            await to_the_end(w.stop())
    return stage_selfies


def _count(ctx: "NightContext") -> int:
    """The sets the stage would render tonight: those queued and the first ones it would queue."""
    members = [g for g in ctx.members(include_away=True) if _held(ctx, _stored(ctx, g)) is None]
    return min(ctx.settings.selfie_per_night,
               len(queued_sets(ctx.store)) + len(needing_first(ctx.store, members)))


register_counter(STAGE, _count)
