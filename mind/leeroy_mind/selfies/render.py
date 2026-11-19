"""Shared profile-set renderer for nightly work and the optional offline booth.

The prompt, held-set checks, NSFW retries, staging/publish transaction and worker contention
rules are identical for both callers. The caller owns worker shutdown and GPU admission.
"""
import hashlib
import logging
import time
from collections import Counter
from dataclasses import dataclass
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable

from leeroy_mind.logs import log
from leeroy_mind.nightly.gpu import LiveLeaseHeld
from leeroy_mind.personas.schema import Persona
from leeroy_mind.wow import CAST_MIN_LEVEL

from . import files
from .config import SelfieCfg
from .kind import chosen_style, wardrobe_pending
from .style import LUST, PLAIN
from .slot import SetRefused, TRIES, render_slot
from .prompt import HELD, LUST_HELD, PromptSpec, build_prompt, build_varied_prompt, renderable
from .wardrobe import WARDROBE_HELD
from .store import Candidate, ensure_first_sets, mark_ready, needing_first, queued_sets
from .worker import Identity, RenderRequest, SelfieWorker, WorkerError, detail_for

# [selfies] identity_sets (owner, 2026-11-14): a profile set of a member with a chosen photo
# carries that photo's face (store.face_pick: never a back view), cut by its framing (the worker
# cuts it tight around the face it finds), at identity_scale; a varied slot seen from behind
# takes it at REAR_SCALE of that, or the adapter turns her to the camera. A member with no
# chosen photo yet (her first set) renders without one.
REAR_SCALE = 0.5
MIN_SCALE = 0.3  # the worker's floor
# A situational or request photo is a tall portrait: framed from the waist or mid-thigh up, her
# face is small in it and the reference at full identity_scale crowds her hands and limbs. Such a
# scene takes the face at WIDE_SCALE of identity_scale (0.6 x 0.75 = 0.45); a head-and-shoulders
# or face scene keeps identity_scale (controller A/B on the GPU, 2026-11-16: with the anatomy
# negative, 1 of 11 images with a defect at 0.6, 0 of 11 at 0.45, her likeness held in all but
# one seed).
WIDE_SCALE = 0.75
WIDE_FRAMINGS = ("upper_body", "three_quarter")


def scene_scale(identity_scale: float, framing: str) -> float:
    """The face reference's scale for a situational or request photo of scene framing
    `framing`: WIDE_SCALE of identity_scale in a WIDE_FRAMINGS scene, never below MIN_SCALE;
    identity_scale otherwise."""
    if framing not in WIDE_FRAMINGS:
        return identity_scale
    return max(MIN_SCALE, round(identity_scale * WIDE_SCALE, 2))

if TYPE_CHECKING:
    from leeroy_mind.nightly.runner import NightContext

STAGE = "selfies"
SET_SIZE = 4
THUMB_PX = 512
CONTENDED = 2  # worker failures in a row that end the stage for the night
QUEUE = "queue"


@dataclass(frozen=True)
class RenderPolicy:
    """Separate receipts/caps; the booth additionally admits only aged, renderable requests.

    Its attempts consume the day's allowance even when refused or failed, avoiding an
    every-minute retry of the same bad set. The night's existing success/skip cap is unchanged.
    `first_first` (his Render selfies button, manual.py) renders first sets before "4 more" ones;
    the night and the booth keep queued_sets' order. `record_refusals` (that button too) gives
    only a set the photo checks refused its receipt for the day, never one a worker error ended.
    """
    stage: str = STAGE
    cap: int | None = None
    requested_before: float | None = None
    day: str | None = None
    record_failures: bool = False
    first_first: bool = False
    record_refusals: bool = False


NIGHT = RenderPolicy()


class RoomFailed(WorkerError):
    """make_room failed after an oom (Ollama down or hung, not a lease's refusal): the GPU is
    still contended, so it counts as a worker failure."""

    def __init__(self):
        super().__init__("room")


def seed_for(night: str, guid: int, set_no: int, idx: int, attempt: int) -> int:
    """The first 4 bytes, big-endian, of the SHA-256 of "night|guid|set|idx|attempt"."""
    digest = hashlib.sha256(f"{night}|{guid}|{set_no}|{idx}|{attempt}".encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big")


class SetMeasure:
    """[selfies] critic for one profile set: the parameter bandit's scale and steps for a slot
    that carries her face at full strength (params.choose; a back view or a looks-edit set keeps
    its own), and the worker's detector measurements of each candidate, kept as the selfie_critic
    rows the set's commit writes (pending: the judge settles them once the worker has exited)."""

    def __init__(self, ctx, cfg: SelfieCfg, guid: int, set_no: int, framing: str | None,
                 style: str, face_expected: tuple[bool, ...]):
        self.ctx, self.cfg, self.guid, self.set_no = ctx, cfg, guid, set_no
        self.framing, self.style, self.face_expected = framing, style, face_expected
        self.rows: list[dict] = []

    def params(self, identity: Identity | None):
        """(identity, steps, arm) of one slot."""
        from .params import choose
        if (identity is None or self.framing is None
                or abs(identity.scale - self.cfg.identity_scale) > 1e-9):
            return identity, self.cfg.steps, False
        got = choose(self.ctx.store, self.cfg, self.ctx.settings.data_dir, shape="square",
                     framing=self.framing, control=(identity.scale, self.cfg.steps))
        if got.scale != identity.scale:
            identity = Identity(identity.ref, identity.crop, got.scale)
        return identity, got.steps, got.arm

    async def measure(self, worker, idx: int, out: Path, identity: Identity | None, seed: int,
                      steps: int, arm: bool) -> None:
        from . import bestof, critic
        data = self.ctx.settings.data_dir
        measured = await bestof.measure(worker, out, identity)
        self.rows.append(dict(
            run=self.ctx.night, purpose="set", guid=self.guid, set_no=self.set_no, idx=idx,
            seed=seed, file=critic.rel(data, files.candidate(data, self.guid, self.set_no, idx)),
            ref=None if identity is None else critic.rel(data, identity.ref),
            crop=None if identity is None else identity.crop, critique=measured,
            framing=self.framing, outfit=self.style, prop=None,
            scale=None if identity is None else identity.scale, steps=steps, arm=arm,
            face_expected=self.face_expected[idx]))

    def write(self, store, now: float) -> None:
        from . import critic
        for row in self.rows:
            critic.insert(store, now=now, **row)


async def settle_sets(ctx: "NightContext", cfg: SelfieCfg) -> None:
    """[selfies] critic, once the stage's worker has exited: the judge settles the pending
    selfie_critic rows (critic.settle), the parameter bandit takes its credit from them and from
    his picks since the last pass (params.credit_picks), and the judge's model leaves the GPU."""
    if not getattr(cfg, "critic", False):
        return
    from . import critic, params
    from .judge import VisionJudge
    data = ctx.settings.data_dir
    judge = VisionJudge(ctx.reasoner.llm, cfg.critic_model)
    now = ctx.clock.now()

    def credit(row, verdict, judged):
        params.credit_row(ctx.store, cfg, data, row, verdict, judged,
                          shape="square" if row["purpose"] == "set" else "portrait", now=now)
    await critic.settle(ctx.store, data, judge, now=now, on_settled=credit)
    params.credit_picks(ctx.store, cfg, data, now=now)
    if judge.used:
        await critic.unload(ctx.arbiter.admin, judge.model)


async def render_candidates(worker, spec: PromptSpec | tuple[PromptSpec, ...], out_dir: Path, *,
                            night: str, guid: int,
                            set_no: int, cfg: SelfieCfg, make_room: Callable[[], Awaitable[None]],
                            leases: Callable[[], frozenset[str]] | None = None,
                            fallback: PromptSpec | None = None,
                            dropped: set[int] | None = None,
                            identity: tuple[Identity | None, ...] | None = None,
                            measure: SetMeasure | None = None,
                            rear: tuple[bool, ...] | None = None) -> list[Candidate]:
    """Slots 0-3 into out_dir/c<idx>.jpg and c<idx>.thumb.jpg, one Candidate each: every slot
    from `spec`, or slot i from spec[i] when it is a tuple of four (a varied set). A slot whose
    prompt the worker finds too long ("prompt_too_long") renders from `fallback` instead when
    there is one, and its index goes into `dropped`. `leases`
    (the arbiter's live holders) is asked before each render: while one is held, LiveLeaseHeld
    and nothing is sent. Raises SetRefused when a slot is flagged TRIES times, RoomFailed when
    make_room fails other than by LiveLeaseHeld, WorkerError for a second oom of a slot, an
    image answered as written whose two files are not both there ("unwritten") or any other
    worker failure, and LiveLeaseHeld. `identity` gives slot i its face reference (None: none).
    `measure` ([selfies] critic) chooses each slot's parameters and measures each candidate.
    `rear` says which slots show her from behind: [selfies] detailer leaves their faces alone."""
    out_dir = Path(out_dir).absolute()
    specs = (spec,) * SET_SIZE if isinstance(spec, PromptSpec) else tuple(spec)
    if len(specs) != SET_SIZE:
        raise ValueError("a set renders four candidates")
    faces = tuple(identity) if identity is not None else (None,) * SET_SIZE
    if len(faces) != SET_SIZE:
        raise ValueError("a set has four face references")
    behind = tuple(rear) if rear is not None else (False,) * SET_SIZE
    if len(behind) != SET_SIZE:
        raise ValueError("a set has four views")
    made = []
    for idx in range(SET_SIZE):
        try:
            made.append(await _slot(worker, specs[idx], out_dir, idx, night=night, guid=guid,
                                    set_no=set_no, cfg=cfg, make_room=make_room, leases=leases,
                                    identity=faces[idx], measure=measure, rear=behind[idx]))
        except WorkerError as e:
            if e.reason != "prompt_too_long" or fallback is None or specs[idx] is fallback:
                raise
            if dropped is not None:
                dropped.add(idx)
            log("selfies_varied_slot_dropped", night=night, guid=guid, set=set_no, slot=idx,
                reason="too_long")
            front = next((f for f in faces if f is not None and f.scale >= cfg.identity_scale), None)
            made.append(await _slot(worker, fallback, out_dir, idx, night=night, guid=guid,
                                    set_no=set_no, cfg=cfg, make_room=make_room, leases=leases,
                                    tag="o", identity=front or faces[idx], measure=measure))
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
                leases: Callable[[], frozenset[str]] | None, tag: str = "",
                identity: Identity | None = None, measure: SetMeasure | None = None,
                rear: bool = False) -> Candidate:
    out, thumb = out_dir / f"c{idx}.jpg", out_dir / f"c{idx}.thumb.jpg"
    steps, arm = cfg.steps, False
    if measure is not None:
        identity, steps, arm = measure.params(identity)

    def request(attempt: int) -> RenderRequest:
        _unleased(leases)
        return RenderRequest(spec.phrases, spec.negative,
                             seed_for(night, guid, set_no, idx, attempt), steps, cfg.guidance,
                             cfg.size, out, thumb, THUMB_PX, cfg.nsfw_max, identity=identity,
                             detail=detail_for(cfg, rear=rear))

    async def room() -> None:
        await _room(make_room, night=night, guid=guid, set_no=set_no, idx=idx)
        _unleased(leases)
    try:
        got, attempts = await render_slot(
            worker, request, rid=lambda attempt: f"{tag}{guid}:{set_no}:{idx}:{attempt}",
            make_room=room)
    except SetRefused:
        refused = SetRefused(f"slot {idx}: {TRIES} seeds flagged")
        refused.slot = idx
        raise refused from None
    if not (out.is_file() and thumb.is_file()):  # a ready set never points at nothing
        raise WorkerError("unwritten")
    if measure is not None:
        await measure.measure(worker, idx, out, identity,
                              seed_for(night, guid, set_no, idx, attempts - 1), steps, arm)
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
    why = renderable(p, chosen_style(ctx.store, p) if p is not None else PLAIN)
    if why is None and wardrobe_pending(ctx.store, p):
        return WARDROBE_HELD  # the photo pass chooses her kind again for her wardrobe first
    return why


def _set_held(ctx: "NightContext", p: Persona | None, guid: int, set_no: int) -> str | None:
    """_held for one queued set: a set with a varied plan composes its own looks (from her
    wardrobe already) and never reads her photo kind, so it does not wait for the kind's
    re-choice (review of #358). _sets checks again that the plan still renders as varied."""
    why = _held(ctx, p)
    if why == WARDROBE_HELD:
        from . import varied
        with ctx.store.transaction():
            if varied.plan_of(ctx.store.conn, guid, set_no) is not None:
                return None
    return why


def _without_persona(ctx: "NightContext") -> int:
    """Roster members of the cast's level with no stored persona, so not in ctx.members."""
    stored = ctx.store.persona_versions()
    return sum(1 for g, m in ctx.store.cast().items()
               if g != ctx.human and m.level >= CAST_MIN_LEVEL and g not in stored)


def queue(ctx: "NightContext", policy: RenderPolicy = NIGHT) -> None:
    """Queue a first set for each member who needs one and has none. Runs on every pass of the
    stage (wants_gpu, the stage, each round of its render loop), so a member whose kind was
    chosen after an earlier pass is queued at the next; ensure_first_sets skips whoever already
    has a photo or a queued or waiting set, so nothing is queued twice. The "queue" receipt is
    written by the night's first pass only."""
    first = not ctx.done(policy.stage, QUEUE)
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
        if not ctx.commit(policy.stage, QUEUE, write):
            return
    else:
        write(ctx.store)
        if not made:
            return
    log("selfies_queued", night=ctx.night, queued=made, missing=dict(sorted(missing.items())))


def done_sets(ctx: "NightContext", policy: RenderPolicy = NIGHT) -> int:
    with ctx.store.transaction():
        return ctx.store.conn.execute(
            "SELECT count(*) FROM night_step WHERE night=? AND stage=? AND unit<>?",
            (ctx.night, policy.stage, QUEUE)).fetchone()[0]


def allows(ctx, guid):
    from leeroy_mind.personas.tiers import is_cast
    scoped = getattr(ctx,"allows_selfie",None)
    return scoped(guid) if scoped else is_cast(ctx.store.conn,guid)


def todo(ctx: "NightContext", policy: RenderPolicy = NIGHT) -> list[str]:
    """The queued sets not done tonight, within what selfie_per_night leaves after the sets
    done tonight (a resumed night does not start the count again)."""
    room = max(0, (ctx.settings.selfie_per_night if policy.cap is None else policy.cap) - done_sets(ctx, policy))
    if policy.day is not None and ctx.clock.local().date().isoformat() != policy.day:
        return []
    sets = queued_sets(ctx.store)
    if policy.first_first:
        sets = sorted(sets, key=lambda s: s.reason != "first")  # stable: first sets, then more
    units = [f"{s.guid}:{s.set_no}" for s in sets
             if allows(ctx,s.guid) and (policy.requested_before is None or (
                 s.requested <= policy.requested_before
                 and _set_held(ctx, _stored(ctx, s.guid), s.guid, s.set_no) is None))]
    return [u for u in units if not ctx.done(policy.stage, u)][:room]


def _unavailable(ctx: "NightContext", err: BaseException, reason: str, policy: RenderPolicy) -> None:
    ctx.fail(policy.stage, "worker", err)
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


async def _start(ctx: "NightContext", worker: SelfieWorker, policy: RenderPolicy) -> bool:
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
            _unavailable(ctx, e, e.reason, policy)
        return False
    except Exception as e:  # OSError: the program cannot be started; a make_room that failed
        _unavailable(ctx, e, type(e).__name__, policy)
        return False
    return True


def _version(ctx: "NightContext", guid: int) -> int | None:
    with ctx.store.transaction():
        return ctx.store.persona_versions(tier=None).get(guid)


def _stale(ctx: "NightContext", cfg: SelfieCfg, p: Persona, guid: int, set_no: int,
           built_from: int | None) -> int | None:
    """A weight or bust edit that landed while set `set_no` rendered from `p` (her stored looks
    now differ from p's): a fresh looks-edit set is queued (service.queue_looks_set) so one
    renders from the persona after the edit; its number, or None."""
    from leeroy_mind.personas.io import load_persona
    from leeroy_mind.personas.schema import appearance_dict
    from .service import queue_looks_set
    try:
        now = load_persona(ctx.store, guid)
    except ValueError:
        return None
    if (now is None or now.appearance is None
            or appearance_dict(now.appearance) == appearance_dict(p.appearance)):
        return None
    why = renderable(now, chosen_style(ctx.store, now) or PLAIN)
    if why is not None and why not in HELD:
        return None
    made, _, _ = queue_looks_set(ctx.store, cfg, now, guid, human=ctx.human,
                                 now=ctx.clock.now(), day=ctx.clock.local().date().isoformat(),
                                 data_dir=ctx.settings.data_dir)
    log("selfies_set_stale", night=ctx.night, guid=guid, set=set_no, requeued=made)
    return made


def _discard(ctx: "NightContext", guid: int, set_no: int) -> None:
    """What a failed render left (not on the cancel path: clear_stale clears that later)."""
    with suppress(OSError):
        files.clear_stale(ctx.settings.data_dir, guid, set_no)


def _varied_specs(ctx: "NightContext", cfg: SelfieCfg, p: Persona, guid: int,
                  set_no: int) -> tuple[PromptSpec | None, ...] | None:
    """A varied set's prompts by slot (varied.py; None for a slot that renders her ordinary
    photo) while the switch is on and its plan still holds for her; None renders the set as an
    ordinary one (logged by code when it had a plan)."""
    from . import varied
    if not getattr(cfg, "varied_sets", False):
        return None
    looks = varied.looks_for(ctx.store, p, guid, set_no)
    if looks is not None:
        try:
            return tuple(None if x is None else build_varied_prompt(p, x) for x in looks)
        except ValueError:
            pass
    with ctx.store.transaction():
        planned = varied.plan_of(ctx.store.conn, guid, set_no) is not None
    if planned:
        log("selfies_varied_ordinary", night=ctx.night, guid=guid, set=set_no)
    return None


def _set_faces(ctx: "NightContext", cfg: SelfieCfg, p: Persona, guid: int, set_no: int,
               varied: bool) -> tuple[Identity | None, ...] | None:
    """The set's face references by slot, or None (the switch off, or no chosen photo with a
    face yet): her cover's face (store.face_pick) cut by its framing at identity_scale, a slot
    seen from behind at REAR_SCALE of it. A set his weight or bust edit queued
    (store.looks_edit_set) holds it at MIN_SCALE in every slot: at identity_scale the reference
    carries her cover's slimmer body into the set (measured on the GPU, 2026-11-15)."""
    if not getattr(cfg, "identity_sets", False) or p.photo is None:
        return None
    from .kind import BACK_VIEWS
    from .scene import ref_crop
    from .store import current, face_pick, looks_edit_set
    with ctx.store.transaction():
        cur = current(ctx.store, guid)
        got = None if cur is None else face_pick(ctx.store.conn, guid, cur.pick)
        light = looks_edit_set(ctx.store.conn, guid, set_no)
        plan = None
        if varied:
            from . import varied as varied_mod
            plan = varied_mod.plan_of(ctx.store.conn, guid, set_no)
    if got is None:
        return None
    ref = files.history(ctx.settings.data_dir, guid, got[0]).absolute()
    if not ref.is_file():
        return None
    crop = ref_crop(got[1] or p.photo.framing)
    rear = Identity(ref, crop, max(MIN_SCALE, round(cfg.identity_scale * REAR_SCALE, 2)))
    if light:
        return (Identity(ref, crop, MIN_SCALE),) * SET_SIZE
    full = Identity(ref, crop, cfg.identity_scale)
    views = tuple(None if x is None else x.view for x in plan) if plan else (None,) * SET_SIZE
    return tuple(rear if v in BACK_VIEWS else full for v in views)


def _set_measure(ctx: "NightContext", cfg: SelfieCfg, p: Persona, guid: int, set_no: int,
                 varied: bool) -> SetMeasure | None:
    """The set's SetMeasure while [selfies] critic is on: her photo kind's framing, the style it
    rendered under, and which slots should show her face (a varied slot seen from behind not)."""
    if not getattr(cfg, "critic", False):
        return None
    from .kind import BACK_VIEWS
    views = (None,) * SET_SIZE
    if varied:
        from . import varied as varied_mod
        with ctx.store.transaction():
            plan = varied_mod.plan_of(ctx.store.conn, guid, set_no)
        if plan:
            views = tuple(None if x is None else x.view for x in plan)
    style = "varied" if varied else (chosen_style(ctx.store, p) or PLAIN)
    framing = p.photo.framing if p.photo is not None else None
    return SetMeasure(ctx, cfg, guid, set_no, framing, style,
                      tuple(v not in BACK_VIEWS for v in views))


async def _render_set(ctx: "NightContext", cfg: SelfieCfg, worker: SelfieWorker, p: Persona,
                      guid: int, set_no: int, varied: tuple[PromptSpec | None, ...] | None,
                      dropped: set[int], measured: list | None = None) -> list[Candidate]:
    """The set's candidates: every slot from her ordinary prompt, or a varied set's slots from
    their own prompts (`dropped` gets the slots that rendered her ordinary photo instead)."""
    data = ctx.settings.data_dir
    files.clear_stale(data, guid, set_no)
    staging = files.staging_dir(data, guid, set_no)
    staging.mkdir(parents=True)
    from .photo_extra import light_for
    ordinary = build_prompt(p, kind_style=chosen_style(ctx.store, p), light=light_for(ctx.store,p))
    if varied is None:
        spec = ordinary
    else:
        dropped.update(i for i, x in enumerate(varied) if x is None)
        spec = tuple(ordinary if x is None else x for x in varied)
    faces = _set_faces(ctx, cfg, p, guid, set_no, varied is not None)
    if faces is not None:
        log("selfies_set_face", night=ctx.night, guid=guid, set=set_no,
            scales=",".join(str(f.scale) for f in faces))
    measure = _set_measure(ctx, cfg, p, guid, set_no, varied is not None)
    if measure is not None and measured is not None:
        measured.append(measure)
    rear = None
    if varied is not None and getattr(cfg, "detailer", False):
        from . import varied as varied_mod
        from .kind import BACK_VIEWS
        with ctx.store.transaction():
            plan = varied_mod.plan_of(ctx.store.conn, guid, set_no)
        if plan and len(plan) == SET_SIZE:
            rear = tuple(x is not None and x.view in BACK_VIEWS for x in plan)
    made = await render_candidates(worker, spec, staging, night=ctx.night, guid=guid,
                                   set_no=set_no, cfg=cfg, make_room=ctx.arbiter.make_room,
                                   leases=lambda: ctx.arbiter.holders,
                                   fallback=None if varied is None else ordinary, dropped=dropped,
                                   identity=faces, measure=measure, rear=rear)
    files.publish_set(data, guid, set_no)
    return made


def _ready(ctx: "NightContext", guid: int, set_no: int, made: list[Candidate],
           lust: bool = False, varied: bool = False, dropped: frozenset[int] = frozenset(),
           built_from: int | None = None, measure: SetMeasure | None = None):
    """Mark the set ready, stamped with the persona version its prompts were built from
    (`built_from`; a weight or bust edit may have landed while it rendered), never the one in
    force at publish. A set that did not render as a varied one loses its plan, and one whose
    slots rendered her ordinary photo loses those slots' looks, so a ready set's plan is always
    what its candidates show."""
    from . import varied as plans

    def write(store) -> None:
        version = (built_from if built_from is not None
                   else store.persona_versions(tier=None)[guid])
        mark_ready(store, guid, set_no, made, now=ctx.clock.now(), night=ctx.night,
                   persona_version=version, lust=lust)
        if measure is not None:  # [selfies] critic: the candidates' rows, pending the judge
            measure.write(store, ctx.clock.now())
        looks = plans.plan_of(store.conn, guid, set_no) if varied else None
        if looks is None or all(i in dropped or x is None for i, x in enumerate(looks)):
            plans.drop_plan(store, guid, set_no)
            return
        if dropped:
            plans.write_plan(store, guid, set_no,
                             [None if i in dropped else x for i, x in enumerate(looks)],
                             plans._day(store, guid, set_no))
        store.conn.execute("DELETE FROM meta WHERE key=?",
                           (plans.REFUSED_KEY.format(guid=guid, set_no=set_no),))
    return write


async def render(ctx: "NightContext", cfg: SelfieCfg, worker: SelfieWorker,
                 policy: RenderPolicy = NIGHT, *, tally: Counter | None = None) -> Counter:
    """Rounds of sets with the one worker: each round queues what is new (_queue) and renders
    the queued sets not yet tried this run, within the night's cap, so a "4 more" asked, or a
    member whose kind was chosen, while the stage runs is rendered in the same stage. Ends when
    a round finds nothing new or the worker is out of reach."""
    if tally is None:
        tally = Counter()
    if not await _start(ctx, worker, policy):
        return tally
    state = {"contended": 0, "restarted": False}
    tried: set[str] = set()
    while True:
        queue(ctx, policy)
        units = [u for u in todo(ctx, policy) if u not in tried]
        if not units:
            break
        tried.update(units)
        if not await _sets(ctx, cfg, worker, units, tally, state, policy):
            break
    log("selfies_done", night=ctx.night, **{k: tally[k] for k in
                                            ("ready", "refused", "failed", "skipped")})
    return tally


async def _sets(ctx: "NightContext", cfg: SelfieCfg, worker: SelfieWorker, todo: list[str],
                tally: Counter, state: dict, policy: RenderPolicy) -> bool:
    """One round's sets; False when the stage ends for the night (contended worker)."""
    contended, restarted = state["contended"], state["restarted"]
    for n, unit in enumerate(todo):
        if policy.day is not None and ctx.clock.local().date().isoformat() != policy.day:
            return False
        guid, set_no = (int(x) for x in unit.split(":"))
        if not allows(ctx,guid):
            continue
        p = _stored(ctx, guid)
        built_from = _version(ctx, guid)  # read with p (no await between): what p is
        why = _held(ctx, p)
        if why == WARDROBE_HELD and getattr(cfg, "varied_sets", False):
            from . import varied as varied_mod
            if varied_mod.looks_for(ctx.store, p, guid, set_no) is not None:
                why = None  # a varied set renders its own looks, not her kind
        if why is not None:
            if why in HELD:  # her kind's text is never rendered plainly, nor without its phrase
                log("selfies_set_held", night=ctx.night, guid=guid, set=set_no,
                    reason="restyle" if why == LUST_HELD else "name")
            elif why == WARDROBE_HELD:
                log("selfies_set_held", night=ctx.night, guid=guid, set=set_no,
                    reason="wardrobe")
            if ctx.skip(policy.stage, unit):
                tally["skipped"] += 1
            continue
        began = time.monotonic()
        try:
            lust = chosen_style(ctx.store, p) == LUST
            specs = _varied_specs(ctx, cfg, p, guid, set_no)
            varied = specs is not None
            lust = lust or varied  # a varied set's looks are the lust style's
            dropped: set[int] = set()
            measured: list = []
            made = await _render_set(ctx, cfg, worker, p, guid, set_no, specs, dropped, measured)
        except LiveLeaseHeld:
            raise
        except WorkerError as e:  # RoomFailed too: contention either way
            _discard(ctx, guid, set_no)
            ctx.fail(policy.stage, unit, e)
            if policy.record_failures:
                ctx.skip(policy.stage, unit)
            tally["failed"] += 1
            contended += 1
            if contended >= CONTENDED or (not worker.running and restarted):
                log("selfies_stopped", level=logging.WARNING, night=ctx.night, reason=e.reason,
                    left=len(todo) - n - 1)
                return False
            if not worker.running:
                restarted = True
                state["restarted"] = True
                if not await _start(ctx, worker, policy):
                    return False
            state["contended"] = contended
            continue
        except Exception as e:  # SetRefused; a folder that could not be made or moved
            _discard(ctx, guid, set_no)
            ctx.fail(policy.stage, unit, e)
            if policy.record_failures or (policy.record_refusals and isinstance(e, SetRefused)):
                ctx.skip(policy.stage, unit)
            if isinstance(e, SetRefused):
                tally["refused"] += 1
                contended = 0  # the worker rendered: not contention
                if varied:
                    from . import varied as plans
                    plans.refused(ctx.store, p, guid, set_no, getattr(e, "slot", None),
                                  night=ctx.night,
                                  share=getattr(cfg, "body_shot_share", plans.BODY_SHOT_SHARE))
            else:
                tally["failed"] += 1
            state["contended"] = contended
            continue
        contended = 0
        state["contended"] = 0
        if policy.day is not None and ctx.clock.local().date().isoformat() != policy.day:
            _discard(ctx, guid, set_no)
            return False
        if not allows(ctx,guid):
            _discard(ctx,guid,set_no)
            continue
        if ctx.commit(policy.stage, unit, _ready(ctx, guid, set_no, made, lust, varied,
                                                 frozenset(dropped), built_from,
                                                 measured[0] if measured else None)):
            tally["ready"] += 1
            log("selfies_set_ready", night=ctx.night, guid=guid, set=set_no,
                seconds=round(time.monotonic() - began, 1), tries=sum(c.tries for c in made),
                **({"varied": True} if varied else {}))
            try:  # the set is saved ready: a failed check never ends the stage
                _stale(ctx, cfg, p, guid, set_no, built_from)
            except Exception as e:
                log("selfies_stale_check_failed", level=logging.WARNING, night=ctx.night,
                    guid=guid, set=set_no, error=type(e).__name__)
        else:
            tally["failed"] += 1
    return True

