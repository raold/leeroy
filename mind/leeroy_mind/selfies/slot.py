"""One image slot, shared by the night's stages (the profile photos' candidates, a situational
photo, a guild-photo tile): up to `tries` seeds, an image the worker's NSFW gate flagged
(selfie/leeroy_selfie/nsfw.py: the only place the threshold lives) replaced by the next seed's,
and one oom answered by making room and repeating that attempt. Logs carry the request id and
scores, never a prompt."""
from contextlib import suppress
from typing import Awaitable, Callable

from leeroy_mind.logs import log

from .worker import RenderRequest, Rendered, WorkerError

__all__ = ["SetRefused", "TRIES", "render_slot"]

TRIES = 3  # seeds a slot, before a flagged slot refuses its set


class SetRefused(Exception):
    """Every seed of one slot was flagged by the NSFW gate: the set stays queued."""


async def render_slot(worker, request: Callable[[int], RenderRequest], *,
                      rid: Callable[[int], str], make_room: Callable[[], Awaitable[None]],
                      tries: int = TRIES) -> tuple[Rendered, int]:
    """(the rendered answer, the attempts used). `request(attempt)` and `rid(attempt)` build
    attempt n's request and id (attempt counts from 0). A flagged answer is never kept (any file
    is removed) and the next attempt runs; `tries` flagged answers raise SetRefused.
    The first WorkerError("oom") awaits make_room() and
    repeats that attempt once; a second propagates, as does any other WorkerError."""
    roomed = False
    for attempt in range(tries):
        req = request(attempt)
        name = rid(attempt)
        try:
            got = await worker.render(req, rid=name)
        except WorkerError as e:
            if e.reason != "oom" or roomed:
                raise
            roomed = True
            await make_room()
            got = await worker.render(req, rid=name)
        if not got.flagged:
            return got, attempt + 1
        for path in (req.out, req.thumb):  # never kept, whatever a worker wrote
            with suppress(FileNotFoundError):
                path.unlink()
        log("selfies_flagged", rid=name, attempt=attempt, nsfw=round(got.nsfw, 3))
    raise SetRefused(f"{tries} seeds flagged")
