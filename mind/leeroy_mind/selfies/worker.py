"""The mind's handle on the selfie worker (`python -m leeroy_selfie worker`, its own venv): JSON
lines on its stdin and stdout, the protocol of selfie/leeroy_selfie/protocol.py spoken from
here (the mind never imports that package). One request at a time; the worker writes the
images itself.

A start or render that times out, a closed stdout, a line longer than the protocol allows or an
answer that is not for the request stops the whole process tree and raises WorkerError
("timeout" or "gone"); a render answer with ok false raises WorkerError(<its error>) and leaves
the worker running. A ready refusal ends the tree, except one that says `retry: true` (vram,
with the pipeline loaded on the CPU): that worker stays up, and the next start() sends it the
retry line instead of starting another, so a retry after make_room loads nothing again. Each
start logs selfies_worker_start with its outcome and timings (the worker's check_ms and
load_ms, the free_gib it read, and ms: from the spawn or the retry to the answer).

Cancelling a start or render kills the tree at once (its answer could no longer be matched);
stop() then waits for it to be gone. A tree being ended is ended to the end, a cancel meanwhile
included (to_the_end), and stays the current one until it is gone, so no stop() can miss it.
Logs carry reasons, counts and timings, never a prompt."""
import asyncio
import json
import logging
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from leeroy_mind.logs import log

from .job import ProcessTree, WorkerError

__all__ = ["GRACE_S", "MAX_LINE", "RENDER_S", "START_S", "STOP_S", "Gridded", "Identity", "Ready",
           "RenderRequest", "Rendered", "SelfieWorker", "WorkerError", "to_the_end"]

START_S = 180.0  # loading the pipeline and the classifier
RENDER_S = 90.0  # one image (about 5 s on the 4090)
STOP_S = 3.0  # after the kill, for the tree to be gone
GRACE_S = 0.5  # after quit and the end of stdin, before the kill
MAX_LINE = 65536  # the protocol's longest line
POLL_S = 0.02
QUIT = b'{"id": "quit", "op": "quit"}\n'
RETRY = b'{"id": "ready", "op": "retry"}\n'
_NUMBERS = ("check_ms", "load_ms", "free_gib")  # what a start's log takes from its answer


@dataclass(frozen=True)
class Ready:
    device: str
    free_gib: float
    versions: dict
    identity: bool = False  # the worker holds the identity adapter (worker --identity)


@dataclass(frozen=True)
class Identity:
    """The reference face of an identity render: `crop` is a square (l, t, r, b) in fractions
    of the image at `ref`."""
    ref: Path
    crop: tuple[float, float, float, float]
    scale: float


@dataclass(frozen=True)
class RenderRequest:
    phrases: tuple[tuple[str, int], ...]  # (text, priority 0-9): the worker drops the lowest
    negative: tuple[tuple[str, int], ...]
    seed: int
    steps: int
    cfg: float
    size: int
    out: Path  # absolute .jpg paths the worker writes
    thumb: Path
    thumb_size: int
    nsfw_max: float
    identity: Identity | None = None  # an identity worker needs it, a plain one refuses it
    shape: str = "square"  # or "portrait" (832 x 1216, needs size 1024)


@dataclass(frozen=True)
class Rendered:
    flagged: bool  # scored nsfw_max or more: nothing was written
    nsfw: float
    prompt: str  # what was rendered, after the worker's fit; stored, never logged
    tokens: int
    seconds: float
    peak_gib: float
    width: int
    height: int


@dataclass(frozen=True)
class Gridded:
    width: int
    height: int
    seconds: float


class _Malformed(ValueError):
    pass


def _field(answer: dict, key: str, *types):
    v = answer.get(key)
    if type(v) not in types:
        raise _Malformed(key)
    return v


def _decode(raw: bytes) -> dict | None:
    """An answer line as {"id": str, "ok": bool, ...}, else None."""
    if not raw.endswith(b"\n"):
        return None
    try:
        answer = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    if type(answer) is not dict or type(answer.get("id")) is not str \
            or type(answer.get("ok")) is not bool:
        return None
    return answer


def _reason(answer: dict) -> str:
    error = answer.get("error")
    return error if type(error) is str and error else "failed"


def _quietly(action) -> None:
    with suppress(Exception):
        action()


def _logged_start(answer: dict, began: float, retried: bool) -> None:
    numbers = {k: v for k in _NUMBERS
               if type(v := answer.get(k)) in (int, float) and v == v and abs(v) < 1e9}
    log("selfies_worker_start", ok=answer["ok"], reason="" if answer["ok"] else _reason(answer),
        retried=retried, ms=max(0, round((asyncio.get_running_loop().time() - began) * 1000)),
        **numbers)


async def to_the_end(aw):
    """`aw`'s result, awaited as a task of its own through a shield until it is done, however
    often the caller is cancelled meanwhile; then the caller's cancel goes on. A killed tree has
    not exited, nor released its VRAM, until _gone says so: its ending is never cut short."""
    task = asyncio.ensure_future(aw)
    cancel = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as e:
            cancel = cancel or e
    if cancel is not None:
        if not task.cancelled():
            task.exception()  # retrieved, so never reported as lost
        raise cancel
    return task.result()


class SelfieWorker:
    """`command` starts the worker (selfies.config.worker_command); `env` is its environment
    (None: this process's). start() may be called again after a stop: a fresh tree."""

    def __init__(self, command, *, env=None, start_s: float = START_S,
                 render_s: float = RENDER_S):
        self._command = [str(c) for c in command]
        self._env = env
        self._start_s = start_s
        self._render_s = render_s
        self._tree: ProcessTree | None = None
        self._ready = False
        self._sequence = 0
        self._stopping = asyncio.Lock()  # held while a tree is ended, and while one is spawned
        self._stops = 0  # stop() calls so far: a start under way sees one arrive
        self._waiting = False  # the current tree refused for VRAM and waits for a retry

    @property
    def running(self) -> bool:
        tree = self._tree
        return self._ready and tree is not None and tree.process.returncode is None

    async def start(self) -> Ready:
        """Until the worker is ready. WorkerError: its own refusal (cuda, vram, model, ...),
        timeout, gone, spawn, or stopped (stop() was called meanwhile: the worker it was
        starting is ended before that stop() returns); OSError when the program cannot be
        started. After a vram refusal the worker can retry, it is kept, and this asks it to try
        the GPU again rather than starting another."""
        stops = self._stops
        async with self._stopping:  # a stop() arriving now waits for the tree to exist
            self._ready = False
            tree = self._tree
            retried = self._waiting and tree is not None and tree.process.returncode is None
            self._waiting = False
            if not retried:
                await to_the_end(self._end_locked(tree))
            if self._stops != stops:
                raise WorkerError("stopped")
            if not retried:
                tree = await ProcessTree.spawn(self._command, env=self._env, limit=MAX_LINE)
                if self._stops != stops:
                    await to_the_end(_end(tree))
                    raise WorkerError("stopped")
                self._tree = tree
        began = asyncio.get_running_loop().time()
        try:
            answer = await self._answer(tree, "ready", self._start_s, RETRY if retried else None)
        except WorkerError:
            if self._stops != stops:  # the stop ended it: say so, not "gone"
                raise WorkerError("stopped") from None
            raise
        _logged_start(answer, began, retried)
        if not answer["ok"]:
            if (answer.get("retry") is True and _reason(answer) == "vram"
                    and self._stops == stops and self._tree is tree):
                self._waiting = True  # up, its pipeline on the CPU: the next start retries it
                raise WorkerError("vram")
            await self._end_failed(tree)
            raise WorkerError(_reason(answer))
        try:
            ready = Ready(_field(answer, "device", str),
                          float(_field(answer, "free_gib", int, float)),
                          dict(_field(answer, "versions", dict)),
                          answer.get("identity") is True)
        except _Malformed:
            await self._end_failed(tree)
            raise WorkerError("gone") from None
        if self._stops != stops or self._tree is not tree:  # stopped while it loaded
            raise WorkerError("stopped")
        self._ready = True
        return ready

    async def render(self, req: RenderRequest, *, rid: str | None = None) -> Rendered:
        """One image. `rid` is the request's id (1-32 characters; default r<n>)."""
        self._sequence += 1
        rid = f"r{self._sequence}" if rid is None else rid
        if type(rid) is not str or not 1 <= len(rid) <= 32:
            raise ValueError("a request id is 1-32 characters")
        body = {
            "id": rid, "op": "render", "phrases": [[t, p] for t, p in req.phrases],
            "negative": [[t, p] for t, p in req.negative], "seed": req.seed,
            "steps": req.steps, "cfg": req.cfg, "size": req.size, "out": str(req.out),
            "thumb": str(req.thumb), "thumb_size": req.thumb_size,
            "nsfw_max": req.nsfw_max}
        if req.identity is not None:
            body["identity"] = {"ref": str(req.identity.ref), "crop": list(req.identity.crop),
                                "scale": req.identity.scale}
        if req.shape != "square":
            body["shape"] = req.shape
        line = json.dumps(body).encode("utf-8") + b"\n"
        if len(line) - 1 > MAX_LINE:
            raise WorkerError("bad_request")  # what the worker would answer; it stays up
        tree = self._tree
        if not self.running:
            if tree is not None and tree.process.returncode is not None:
                await self._end_failed(tree)  # a dead launcher may have left its interpreter
            raise WorkerError("gone")
        answer = await self._answer(tree, rid, self._render_s, line)
        if not answer["ok"]:
            raise WorkerError(_reason(answer))
        try:
            return Rendered(_field(answer, "flagged", bool),
                            float(_field(answer, "nsfw", int, float)),
                            _field(answer, "prompt", str), _field(answer, "tokens", int),
                            float(_field(answer, "seconds", int, float)),
                            float(_field(answer, "peak_gib", int, float)),
                            _field(answer, "width", int), _field(answer, "height", int))
        except _Malformed:
            await self._end_failed(tree)
            raise WorkerError("gone") from None

    async def grid(self, tiles: Sequence[Path], *, tile: int, gutter: int, out: Path,
                   thumb: Path, thumb_size: int, rid: str | None = None) -> Gridded:
        """The tiles composed into one image at `out` (and `thumb`). An ok false answer
        (missing_tile, bad_request, ...) raises WorkerError(<error>) and leaves the worker
        running."""
        self._sequence += 1
        rid = f"r{self._sequence}" if rid is None else rid
        if type(rid) is not str or not 1 <= len(rid) <= 32:
            raise ValueError("a request id is 1-32 characters")
        line = json.dumps({
            "id": rid, "op": "grid", "tiles": [str(t) for t in tiles], "tile": tile,
            "gutter": gutter, "out": str(out), "thumb": str(thumb),
            "thumb_size": thumb_size}).encode("utf-8") + b"\n"
        if len(line) - 1 > MAX_LINE:
            raise WorkerError("bad_request")
        tree = self._tree
        if not self.running:
            if tree is not None and tree.process.returncode is not None:
                await self._end_failed(tree)
            raise WorkerError("gone")
        answer = await self._answer(tree, rid, self._render_s, line)
        if not answer["ok"]:
            raise WorkerError(_reason(answer))
        try:
            return Gridded(_field(answer, "width", int), _field(answer, "height", int),
                           float(_field(answer, "seconds", int, float)))
        except _Malformed:
            await self._end_failed(tree)
            raise WorkerError("gone") from None

    async def _answer(self, tree: ProcessTree, rid: str, timeout: float,
                      line: bytes | None = None) -> dict:
        process = tree.process
        try:
            async with asyncio.timeout(timeout):
                if line is not None:
                    process.stdin.write(line)
                    await process.stdin.drain()
                raw = await process.stdout.readline()
        except TimeoutError:
            await self._end_failed(tree)
            raise WorkerError("timeout") from None
        except asyncio.CancelledError:
            if self._tree is tree:
                self._ready = False
            _quietly(tree.kill)
            raise
        except (OSError, ValueError):  # a closed pipe; a line over MAX_LINE
            await self._end_failed(tree)
            raise WorkerError("gone") from None
        answer = _decode(raw)
        if answer is None or answer["id"] != rid:
            await self._end_failed(tree)
            raise WorkerError("gone")
        return answer

    async def _end_failed(self, tree: ProcessTree) -> None:
        """Ends the tree a request failed on, and only that one: after a restart the current
        tree is a fresh worker that this failure says nothing about. Not a stop(): a start
        under way carries on. A cancel meanwhile goes on once the tree is gone."""
        async def end() -> None:
            async with self._stopping:
                await self._end_locked(tree)  # nothing, if a stop() or start() took it
        await to_the_end(end())

    async def _end_locked(self, tree: ProcessTree | None) -> None:
        """Ends `tree` while it is the current one (the caller holds the stop lock), and lets
        it go only once it is gone."""
        if tree is None or self._tree is not tree:
            return
        self._ready = False
        self._waiting = False
        await _end(tree)
        if self._tree is tree:
            self._tree = None

    async def stop(self) -> None:
        """Quit, the end of stdin, GRACE_S for the worker to leave, then kill the tree and wait
        up to STOP_S for it to be gone (logging selfies_worker_left if it is not). Idempotent;
        never raises (but a cancellation propagates, once the tree is gone)."""
        self._ready = False
        self._stops += 1

        async def end() -> None:
            async with self._stopping:
                await self._end_locked(self._tree)
        await to_the_end(end())


async def _end(tree: ProcessTree | None) -> None:
    """Ends a tree no longer anyone's current one (the caller holds the stop lock)."""
    if tree is None:
        return
    try:
        try:
            await _goodbye(tree.process)
        finally:
            _quietly(tree.kill)
        await _gone(tree)
    except Exception as e:
        log("selfies_worker_stop_failed", level=logging.WARNING, error=type(e).__name__)
    finally:
        _quietly(tree.close)


async def _goodbye(process: asyncio.subprocess.Process) -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + GRACE_S
    with suppress(Exception):  # a closed pipe; a worker that does not read
        process.stdin.write(QUIT)
        await asyncio.wait_for(process.stdin.drain(), GRACE_S)
    with suppress(Exception):
        process.stdin.close()
    with suppress(TimeoutError):
        await asyncio.wait_for(process.wait(), max(0.0, end - loop.time()))


async def _gone(tree: ProcessTree) -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + STOP_S
    while (left := tree.left()) and loop.time() < end:
        await asyncio.sleep(POLL_S)
    if left:
        log("selfies_worker_left", level=logging.WARNING, left=left)
        return
    with suppress(TimeoutError):  # the pipes' transport closes once the tree is gone
        await asyncio.wait_for(tree.process.wait(), max(POLL_S, end - loop.time()))
