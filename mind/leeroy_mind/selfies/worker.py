"""The mind's handle on the selfie worker (`python -m leeroy_selfie worker`, its own venv): JSON
lines on its stdin and stdout, the protocol of selfie/leeroy_selfie/protocol.py spoken from
here (the mind never imports that package). One request at a time; the worker writes the
images itself.

A start or render that times out, a closed stdout, a line longer than the protocol allows or an
answer that is not for the request stops the whole process tree and raises WorkerError
("timeout" or "gone"); an answer with ok false raises WorkerError(<its error>) and leaves the
worker running. Cancelling a start or render kills the tree at once (its answer could no
longer be matched); stop() then waits for it to be gone. Logs carry reasons, counts and
timings, never a prompt."""
import asyncio
import json
import logging
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from leeroy_mind.logs import log

from .job import ProcessTree, WorkerError

__all__ = ["GRACE_S", "MAX_LINE", "RENDER_S", "START_S", "STOP_S", "Ready", "RenderRequest",
           "Rendered", "SelfieWorker", "WorkerError"]

START_S = 180.0  # loading the pipeline and the classifier
RENDER_S = 90.0  # one image (about 5 s on the 4090)
STOP_S = 3.0  # after the kill, for the tree to be gone
GRACE_S = 0.5  # after quit and the end of stdin, before the kill
MAX_LINE = 65536  # the protocol's longest line
POLL_S = 0.02
QUIT = b'{"id": "quit", "op": "quit"}\n'


@dataclass(frozen=True)
class Ready:
    device: str
    free_gib: float
    versions: dict


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


@dataclass(frozen=True)
class Rendered:
    flagged: bool  # scored nsfw_max or more: nothing was written
    nsfw: float
    prompt: str  # what was rendered, after the worker's fit; stored, never logged
    tokens: int
    seconds: float
    peak_gib: float


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
        self._stopping = asyncio.Lock()

    @property
    def running(self) -> bool:
        tree = self._tree
        return self._ready and tree is not None and tree.process.returncode is None

    async def start(self) -> Ready:
        """Until the worker is ready. WorkerError: its own refusal (cuda, vram, model, ...),
        timeout, gone or spawn; OSError when the program cannot be started."""
        await self.stop()
        self._tree = await ProcessTree.spawn(self._command, env=self._env, limit=MAX_LINE)
        answer = await self._answer("ready", self._start_s)
        if not answer["ok"]:
            await self.stop()
            raise WorkerError(_reason(answer))
        try:
            ready = Ready(_field(answer, "device", str),
                          float(_field(answer, "free_gib", int, float)),
                          dict(_field(answer, "versions", dict)))
        except _Malformed:
            await self.stop()
            raise WorkerError("gone") from None
        self._ready = True
        return ready

    async def render(self, req: RenderRequest, *, rid: str | None = None) -> Rendered:
        """One image. `rid` is the request's id (1-32 characters; default r<n>)."""
        self._sequence += 1
        rid = f"r{self._sequence}" if rid is None else rid
        if type(rid) is not str or not 1 <= len(rid) <= 32:
            raise ValueError("a request id is 1-32 characters")
        line = json.dumps({
            "id": rid, "op": "render", "phrases": [[t, p] for t, p in req.phrases],
            "negative": [[t, p] for t, p in req.negative], "seed": req.seed,
            "steps": req.steps, "cfg": req.cfg, "size": req.size, "out": str(req.out),
            "thumb": str(req.thumb), "thumb_size": req.thumb_size,
            "nsfw_max": req.nsfw_max}).encode("utf-8") + b"\n"
        if len(line) - 1 > MAX_LINE:
            raise WorkerError("bad_request")  # what the worker would answer; it stays up
        if not self.running:
            await self.stop()
            raise WorkerError("gone")
        answer = await self._answer(rid, self._render_s, line)
        if not answer["ok"]:
            raise WorkerError(_reason(answer))
        try:
            return Rendered(_field(answer, "flagged", bool),
                            float(_field(answer, "nsfw", int, float)),
                            _field(answer, "prompt", str), _field(answer, "tokens", int),
                            float(_field(answer, "seconds", int, float)),
                            float(_field(answer, "peak_gib", int, float)))
        except _Malformed:
            await self.stop()
            raise WorkerError("gone") from None

    async def _answer(self, rid: str, timeout: float, line: bytes | None = None) -> dict:
        tree = self._tree
        process = tree.process
        try:
            async with asyncio.timeout(timeout):
                if line is not None:
                    process.stdin.write(line)
                    await process.stdin.drain()
                raw = await process.stdout.readline()
        except TimeoutError:
            await self.stop()
            raise WorkerError("timeout") from None
        except asyncio.CancelledError:
            self._ready = False
            _quietly(tree.kill)
            raise
        except (OSError, ValueError):  # a closed pipe; a line over MAX_LINE
            await self.stop()
            raise WorkerError("gone") from None
        answer = _decode(raw)
        if answer is None or answer["id"] != rid:
            await self.stop()
            raise WorkerError("gone")
        return answer

    async def stop(self) -> None:
        """Quit, the end of stdin, GRACE_S for the worker to leave, then kill the tree and wait
        up to STOP_S for it to be gone (logging selfies_worker_left if it is not). Idempotent;
        never raises (but a cancellation propagates, after the kill)."""
        self._ready = False
        async with self._stopping:
            tree, self._tree = self._tree, None
            if tree is None:
                return
            try:
                try:
                    await _goodbye(tree.process)
                finally:
                    _quietly(tree.kill)
                await _gone(tree)
            except Exception as e:
                log("selfies_worker_stop_failed", level=logging.WARNING,
                    error=type(e).__name__)
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
