"""Best-of-N for situational and request photos ([selfies] critic, best_of): the pieces both
snapstage and request_render share. A Shot is one seed rendered and measured (its selfie_critic
row); a Pending is a situation's shots waiting for the judge, which runs only after the worker's
process tree has exited; settle gives each its verdict (critic.settle) and the parameter
bandit its credit (params.credit_row)."""
import logging
from dataclasses import dataclass
from pathlib import Path

from leeroy_mind.logs import log

from . import critic, params
from .worker import Identity, Rendered, WorkerError

# A worker that is gone, timed out or stopped is the render loop's to handle; any other refusal
# of a measurement (missing_image, missing_ref, ...) only leaves the shot unmeasured.
_FATAL = ("gone", "timeout", "stopped")


@dataclass(frozen=True)
class Shot:
    row_id: int
    out: Path
    thumb: Path
    got: Rendered
    seed: int
    tries: int


@dataclass(frozen=True)
class Pending:
    row: object  # snaps.SnapRow
    began: float
    shots: tuple[Shot, ...]


async def measure(worker, path: Path, identity: Identity | None) -> "critic.Critique | None":
    """The worker's critic measurements of `path` (against `identity`'s reference), or None
    when it refused them; a worker that died raises as a render would."""
    try:
        if identity is None:
            return await worker.critic(path)
        return await worker.critic(path, ref=identity.ref, crop=identity.crop)
    except WorkerError as e:
        if e.reason in _FATAL:
            raise
        log("critic_measure_failed", level=logging.WARNING, code=e.reason)
        return None


async def settle(store, cfg, data_dir, judge, ids, *, shape: str, now: float) -> dict:
    """critic.settle for these rows, crediting the parameter bandit with each verdict."""
    def credit(row, verdict, judged):
        params.credit_row(store, cfg, data_dir, row, verdict, judged, shape=shape, now=now)
    return await critic.settle(store, data_dir, judge, ids=ids, now=now, on_settled=credit)
