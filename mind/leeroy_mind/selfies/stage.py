"""The nightly profile-photo stage; shared rendering lives in selfies.render."""
from typing import Awaitable, Callable, TYPE_CHECKING

from leeroy_mind.nightly.budget import register_counter
from .config import SelfieCfg, worker_command, worker_env
from .render import (CONTENDED, SET_SIZE, STAGE, THUMB_PX, TRIES, SetRefused, RoomFailed,
                     _held, _stored, _unleased, queue, todo, render, render_candidates, seed_for,
                     start_worker)
from .store import needing_first, queued_sets
from .worker import SelfieWorker, to_the_end

if TYPE_CHECKING:
    from leeroy_mind.nightly.runner import NightContext


def wants_gpu(ctx: "NightContext") -> bool:
    queue(ctx)
    return any(_held(ctx, _stored(ctx, int(unit.split(":")[0]))) is None for unit in todo(ctx))


def make_stage(cfg: SelfieCfg, *, worker: Callable[[], SelfieWorker] | None = None
               ) -> Callable[["NightContext"], Awaitable[None]]:
    async def stage_selfies(ctx: "NightContext") -> None:
        queue(ctx)
        if not todo(ctx):
            return
        w = worker() if worker is not None else SelfieWorker(worker_command(cfg), env=worker_env(cfg))
        try:
            await render(ctx, cfg, w)
        finally:
            # Never call the arbiter from cancellation cleanup: hand_over holds its lock.
            # The whole process tree must end before the live preload can start.
            await to_the_end(w.stop())
    return stage_selfies


def _count(ctx: "NightContext") -> int:
    members = [g for g in ctx.members(include_away=True) if _held(ctx, _stored(ctx, g)) is None]
    return min(ctx.settings.selfie_per_night,
               len(queued_sets(ctx.store)) + len(needing_first(ctx.store, members)))


register_counter(STAGE, _count)
