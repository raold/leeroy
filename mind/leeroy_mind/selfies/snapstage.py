"""The night's situational and guild photos (P4 Task 8): stage "snaps", an image stage after P2's
"selfies", renders the photos the scenes stage planned, each with the member's own face.

Units "snap:<id>" for the planned photos, guild photos first, at most snaps_per_night a night
counting the units done tonight. A situation renders only while the member's photo in force and
looks are the ones it was planned from (else it ends photo_changed or looks_changed) and while
its reference, the immutable history/<pick>.jpg of that photo (never chosen.jpg), is there (else
missing). A lust scene renders only while scene_renderable allows it; a held one stays planned.
It is a portrait (832 x 1216) with that photo's face as the identity reference, cut by its photo
kind's framing (ref_crop); three flagged seeds end it flagged. A guild photo renders each of its
members alone, a square tile into its .part folder, then one grid of the tiles; a member whose
photo changed or who cannot render is left out, and fewer than GUILD_MIN tiles end it. The
commit checks the photos once more: a situation whose photo or looks moved during its render
ends, and a guild photo with a member whose photo moved stays planned for a later night. No
file is ever deleted here but a guild photo's own .part tiles, cleared before it renders and
removed once it is ready; an expired photo keeps whatever files it has.

The worker (the identity one) starts only when a photo is about to render, never while a live
lease is held. Contention, a lease and the final stop are P2's (stage.py): an oom makes room and
repeats that attempt once; CONTENDED worker failures in a row end the stage, a worker that died
is started again once; a live lease (LiveLeaseHeld) ends it at once with no unit failed; however
it ends, his login's cancel included, the worker's whole process tree is gone before the
hand-over preloads the live model. Logs carry ids, counts, scores, codes and timings, never a
prompt, a scene or a name."""
import logging
import shutil
import time
from collections import Counter
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable

from leeroy_mind.logs import log
from leeroy_mind.nightly.budget import register_counter
from leeroy_mind.nightly.gpu import LiveLeaseHeld
from leeroy_mind.personas.schema import Persona

from . import files
from .config import SelfieCfg, worker_command, worker_env
from .prompt import LUST_HELD, LUST_NAMED, renderable
from .scene import build_scene_prompt, build_tile_prompt, ref_crop, scene_from, scene_renderable
from .scenes import GUILD_MIN
from .slot import SetRefused, render_slot
from .snaps import SnapRow, expire, looks_hash, mark_rendered, planned
from .stage import CONTENDED, RoomFailed, _unleased, seed_for, start_worker
from .store import current
from .worker import Identity, RenderRequest, SelfieWorker, WorkerError, to_the_end

if TYPE_CHECKING:
    from leeroy_mind.nightly.runner import NightContext

STAGE = "snaps"
SNAP_THUMB_PX = 384  # a situational photo's thumbnail, on its long side
TILE_PX = 512  # a guild photo's tile in the grid
GUTTER_PX = 8
GUILD_THUMB_PX = 512
SIZE = 1024  # every render: a portrait 832 x 1216, a tile 1024 x 1024
_TILE_THUMB_PX = 128  # the worker writes one; nobody shows it
_HELD = {LUST_HELD: "restyle", LUST_NAMED: "name"}


def _unit(row: SnapRow) -> str:
    return f"snap:{row.id}"


def _done_units(ctx: "NightContext") -> int:
    with ctx.store.transaction():
        return ctx.store.conn.execute(
            "SELECT count(*) FROM night_step WHERE night=? AND stage=?",
            (ctx.night, STAGE)).fetchone()[0]


def _todo(ctx: "NightContext") -> list[SnapRow]:
    """The planned photos not done tonight, within what snaps_per_night leaves after the units
    done tonight (a resumed night does not start the count again). A photo done tonight but
    still planned (held) is among the first snaps_per_night planned, so the room is filled."""
    cap = ctx.settings.snaps_per_night
    room = max(0, cap - _done_units(ctx))
    if room == 0:
        return []
    return [r for r in planned(ctx.store, cap) if not ctx.done(STAGE, _unit(r))][:room]


def _stored(ctx: "NightContext", guid: int) -> Persona | None:
    try:
        return ctx.base(guid)
    except KeyError:
        return None


def _pick_of(store, guid: int) -> int | None:
    cur = current(store, guid)
    return None if cur is None else cur.pick


def _looks_of(p: Persona | None) -> str | None:
    return None if p is None or p.appearance is None else looks_hash(p.appearance)


def _reference(ctx: "NightContext", guid: int, pick: int) -> Path:
    return files.history(ctx.settings.data_dir, guid, pick).absolute()


def _moved(store, row: SnapRow, p: Persona | None) -> str | None:
    """Why a situation no longer stands for the member as they are: photo_changed or
    looks_changed, the sweep's order."""
    if _pick_of(store, row.guid) != row.pick:
        return "photo_changed"
    if _looks_of(p) != row.looks:
        return "looks_changed"
    return None


def _ended(ctx: "NightContext", row: SnapRow) -> str | None:
    """Why a situation ends before its render: _moved, then missing (no reference file)."""
    why = _moved(ctx.store, row, _stored(ctx, row.guid))
    if why is None and not _reference(ctx, row.guid, row.pick).is_file():
        why = "missing"
    return why


def _held(p: Persona, row: SnapRow) -> str | None:
    why = scene_renderable(p, scene_from(row.scene))
    return None if why is None else _HELD.get(why, why)


def _members(ctx: "NightContext", row: SnapRow) -> list[tuple[int, int, int, Persona]]:
    """(tile index, guid, pick, persona) for each member of a guild photo who can be in it
    now: the photo in force is the one planned, the persona renders and the reference is
    there."""
    out = []
    for i, (guid, pick) in enumerate(row.members):
        p = _stored(ctx, guid)
        if (p is not None and renderable(p) is None and _pick_of(ctx.store, guid) == pick
                and _reference(ctx, guid, pick).is_file()):
            out.append((i, guid, pick, p))
    return out


def _renders(ctx: "NightContext", row: SnapRow) -> bool:
    if row.kind == "guild":
        return len(_members(ctx, row)) >= GUILD_MIN
    return _ended(ctx, row) is None and _held(ctx.base(row.guid), row) is None


def wants_gpu(ctx: "NightContext") -> bool:
    """The runner asks this before it makes room for the stage: whether any photo left tonight
    can render now. A row it cannot read counts as one, so the stage fails it as its unit."""
    for row in _todo(ctx):
        try:
            if _renders(ctx, row):
                return True
        except Exception:
            return True
    return False


def _unavailable(ctx: "NightContext", err: BaseException, reason: str) -> None:
    ctx.fail(STAGE, "worker", err)
    log("selfies_unavailable", level=logging.WARNING, night=ctx.night, stage=STAGE,
        reason=reason)


async def _start(ctx: "NightContext", worker: SelfieWorker) -> bool:
    """P2's start (stage._start) for this stage: a stop meanwhile is his login, no failure."""
    _unleased(lambda: ctx.arbiter.holders)  # never load the pipeline under a live lease
    try:
        await start_worker(worker, ctx.arbiter.make_room)
    except LiveLeaseHeld:
        raise
    except WorkerError as e:
        if e.reason == "stopped":
            log("selfies_preempted", night=ctx.night, stage=STAGE)
        else:
            _unavailable(ctx, e, e.reason)
        return False
    except Exception as e:  # OSError: the program cannot be started; a make_room that failed
        _unavailable(ctx, e, type(e).__name__)
        return False
    return True


class _Renderer:
    """One image at a time through render_slot, each asked only while no live lease is held."""

    def __init__(self, ctx: "NightContext", cfg: SelfieCfg, worker: SelfieWorker):
        self.ctx, self.cfg, self.worker = ctx, cfg, worker

    def _leases(self) -> None:
        _unleased(lambda: self.ctx.arbiter.holders)

    async def _room(self, snap_id: int) -> None:
        try:
            await self.ctx.arbiter.make_room()
        except LiveLeaseHeld:
            raise
        except Exception as e:
            log("selfies_room_failed", level=logging.WARNING, night=self.ctx.night,
                snap=snap_id, error=type(e).__name__)
            raise RoomFailed() from e
        self._leases()

    async def image(self, *, snap_id: int, guid: int, idx: int, spec, ref: Path, framing: str,
                    out: Path, thumb: Path, thumb_size: int, shape: str, rid: str):
        """(the answer, its seed, the attempts used). SetRefused after TRIES flagged seeds;
        WorkerError("unwritten") for an answer whose two files are not both there."""
        ctx, cfg = self.ctx, self.cfg
        identity = Identity(ref, ref_crop(framing), cfg.identity_scale)

        def request(attempt: int) -> RenderRequest:
            self._leases()
            return RenderRequest(spec.phrases, spec.negative,
                                 seed_for(ctx.night, guid, snap_id, idx, attempt), cfg.steps,
                                 cfg.guidance, SIZE, out, thumb, thumb_size, cfg.nsfw_max,
                                 identity=identity, shape=shape)
        got, attempts = await render_slot(self.worker, request,
                                          rid=lambda attempt: f"{rid}:{attempt}",
                                          make_room=lambda: self._room(snap_id))
        if not (out.is_file() and thumb.is_file()):
            raise WorkerError("unwritten")
        return got, seed_for(ctx.night, guid, snap_id, idx, attempts - 1), attempts


def _expiry(ctx: "NightContext", row: SnapRow, why: str) -> bool:
    now = ctx.clock.now()
    if not ctx.commit(STAGE, _unit(row), lambda store: expire(store, row.id, why, now=now)):
        return False
    log("selfies_snap_expired", night=ctx.night, snap=row.id, reason=why)
    return True


async def _situation(ctx: "NightContext", r: _Renderer, row: SnapRow, began: float) -> str:
    data = ctx.settings.data_dir
    p = ctx.base(row.guid)
    out = files.snap(data, row.guid, row.id).absolute()
    thumb = files.snap(data, row.guid, row.id, thumb=True).absolute()
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        got, seed, tries = await r.image(
            snap_id=row.id, guid=row.guid, idx=0, spec=build_scene_prompt(p, scene_from(row.scene)),
            ref=_reference(ctx, row.guid, row.pick), framing=p.photo.framing, out=out,
            thumb=thumb, thumb_size=SNAP_THUMB_PX, shape="portrait", rid=f"s{row.id}")
    except SetRefused:
        return "expired" if _expiry(ctx, row, "flagged") else "failed"
    moved = None

    def write(store) -> None:
        nonlocal moved
        moved = _moved(store, row, ctx.base(row.guid))  # his choice may have moved meanwhile
        if moved is not None:
            expire(store, row.id, moved, now=ctx.clock.now())
            return
        mark_rendered(store, row.id, width=got.width, height=got.height, seed=seed,
                      tries=tries, nsfw=got.nsfw, prompt=got.prompt, now=ctx.clock.now())
    if not ctx.commit(STAGE, _unit(row), write):
        return "failed"
    if moved is not None:
        log("selfies_snap_expired", night=ctx.night, snap=row.id, reason=moved)
        return "expired"
    log("selfies_snap_ready", night=ctx.night, snap=row.id,
        seconds=round(time.monotonic() - began, 1), tries=tries)
    return "ready"


def _clear(path: Path) -> None:
    with suppress(FileNotFoundError):
        shutil.rmtree(path, onexc=files._held)


async def _guild(ctx: "NightContext", r: _Renderer, row: SnapRow, members, began: float) -> str:
    data = ctx.settings.data_dir
    part = files.guild_tiles(data, row.id).absolute()
    _clear(part)  # what an earlier, unfinished render of this photo left
    part.mkdir(parents=True)
    made, refused = [], False  # (guid, pick, tile, tries, nsfw)
    for i, guid, pick, p in members:
        if _pick_of(ctx.store, guid) != pick:  # moved since the stage began: left out
            continue
        tile = part / f"t{i}.jpg"
        try:
            got, _, tries = await r.image(
                snap_id=row.id, guid=guid, idx=i, spec=build_tile_prompt(p),
                ref=_reference(ctx, guid, pick), framing=p.photo.framing, out=tile,
                thumb=part / f"t{i}.thumb.jpg", thumb_size=_TILE_THUMB_PX, shape="square",
                rid=f"g{row.id}:{i}")
        except SetRefused:
            refused = True
            continue
        made.append((guid, pick, tile, tries, got.nsfw))
    made = [m for m in made if _pick_of(ctx.store, m[0]) == m[1]]
    if len(made) < GUILD_MIN:
        return "expired" if _expiry(ctx, row, "flagged" if refused else "photo_changed") \
            else "failed"
    r._leases()
    out = files.guild_photo(data, row.id).absolute()
    thumb = files.guild_photo(data, row.id, thumb=True).absolute()
    gridded = await r.worker.grid([m[2] for m in made], tile=TILE_PX, gutter=GUTTER_PX, out=out,
                                  thumb=thumb, thumb_size=GUILD_THUMB_PX, rid=f"g{row.id}:grid")
    if not (out.is_file() and thumb.is_file()):
        raise WorkerError("unwritten")
    marked = False

    def write(store) -> None:
        nonlocal marked
        if any(_pick_of(store, guid) != pick for guid, pick, *_ in made):
            return  # a face moved meanwhile: rendered again on a later night, without it
        mark_rendered(store, row.id, width=gridded.width, height=gridded.height, seed=None,
                      tries=sum(m[3] for m in made), nsfw=max(m[4] for m in made), prompt=None,
                      now=ctx.clock.now(), members=[(guid, pick) for guid, pick, *_ in made])
        marked = True
    if not ctx.commit(STAGE, _unit(row), write):
        return "failed"
    if not marked:
        log("selfies_snap_held", night=ctx.night, snap=row.id, reason="photo_changed")
        return "held"
    with suppress(OSError):  # the sweep's sync_snaps clears a .part left behind
        _clear(part)
    log("selfies_snap_ready", night=ctx.night, snap=row.id,
        seconds=round(time.monotonic() - began, 1), tries=sum(m[3] for m in made),
        tiles=len(made))
    return "ready"


async def _render(ctx: "NightContext", cfg: SelfieCfg, worker: SelfieWorker,
                  todo: list[SnapRow]) -> None:
    r = _Renderer(ctx, cfg, worker)
    tally = Counter()
    started = restarted = False
    contended = 0
    for n, row in enumerate(todo):
        unit = _unit(row)
        try:
            if row.kind == "guild":
                members = _members(ctx, row)
                if len(members) < GUILD_MIN:
                    tally["expired" if _expiry(ctx, row, "photo_changed") else "failed"] += 1
                    continue
            else:
                why = _ended(ctx, row)
                if why is not None:
                    tally["expired" if _expiry(ctx, row, why) else "failed"] += 1
                    continue
                held = _held(ctx.base(row.guid), row)
                if held is not None:
                    log("selfies_snap_held", night=ctx.night, snap=row.id, reason=held)
                    tally["held" if ctx.skip(STAGE, unit) else "failed"] += 1
                    continue
        except Exception as e:  # a row it cannot read: that unit's failure only
            ctx.fail(STAGE, unit, e)
            tally["failed"] += 1
            continue
        if not started:
            if not await _start(ctx, worker):
                break
            started = True
        began = time.monotonic()
        try:
            if row.kind == "guild":
                tally[await _guild(ctx, r, row, members, began)] += 1
            else:
                tally[await _situation(ctx, r, row, began)] += 1
        except LiveLeaseHeld:
            raise
        except WorkerError as e:  # RoomFailed too: contention either way
            ctx.fail(STAGE, unit, e)
            tally["failed"] += 1
            contended += 1
            if contended >= CONTENDED or (not worker.running and restarted):
                log("selfies_snaps_stopped", level=logging.WARNING, night=ctx.night,
                    reason=e.reason, left=len(todo) - n - 1)
                break
            if not worker.running:
                restarted = True
                if not await _start(ctx, worker):
                    break
            continue
        except Exception as e:  # a folder that could not be made or cleared
            ctx.fail(STAGE, unit, e)
            tally["failed"] += 1
            continue
        contended = 0
    log("selfies_snaps_done", night=ctx.night,
        **{k: tally[k] for k in ("ready", "expired", "held", "failed")})


def make_snaps_stage(cfg: SelfieCfg, *, worker: Callable[[], SelfieWorker] | None = None
                     ) -> Callable[["NightContext"], Awaitable[None]]:
    """The "snaps" stage, an image stage. `worker` makes the night's worker (tests pass the
    fake); by default the selfie/ venv's identity worker, from `cfg`."""
    def make() -> SelfieWorker:
        if worker is not None:
            return worker()
        return SelfieWorker(worker_command(cfg, identity=True), env=worker_env(cfg))

    async def stage_snaps(ctx: "NightContext") -> None:
        todo = _todo(ctx)
        if not todo:
            return
        w = make()
        try:
            await _render(ctx, cfg, w, todo)
        finally:
            # his login's cancel too, landing here or before: the stop runs to its end, so the
            # tree is gone before the hand-over's preempt returns and the live model loads
            await to_the_end(w.stop())
    return stage_snaps


def _count(ctx: "NightContext") -> int:
    """The images the stage would render tonight: one a situation, a guild photo one a member,
    at most snaps_per_night."""
    images = sum(1 if r.kind == "situation" else len(r.members) for r in _todo(ctx))
    return min(ctx.settings.snaps_per_night, images)


register_counter(STAGE, _count)
