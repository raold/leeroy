import asyncio
import heapq
import itertools
import logging
from enum import IntEnum
from typing import Awaitable, Callable, TypeVar

from leeroy_mind.clock import Clock
from leeroy_mind.llm.client import LLMClient

T = TypeVar("T")
log = logging.getLogger("leeroy_mind")


class Priority(IntEnum):
    REPLY_HUMAN = 0
    RAID_LEAD = 1
    SOCIAL = 2
    EXTRACTION = 3
    BACKGROUND = 4


_LOW = (Priority.EXTRACTION, Priority.BACKGROUND)


class SchedulerError(Exception):
    pass


class Expired(SchedulerError):
    """The job's deadline passed before it could start; the LLM was never called."""

    def __init__(self, deadline: float):
        super().__init__(f"deadline {deadline} passed before the job started")
        self.deadline = deadline


class SchedulerClosed(SchedulerError):
    pass


class _Entry:
    __slots__ = ("priority", "seq", "job", "deadline", "fut", "task")

    def __init__(self, priority, seq, job, deadline, fut):
        self.priority = priority
        self.seq = seq
        self.job = job
        self.deadline = deadline
        self.fut = fut
        self.task: asyncio.Task | None = None

    def __lt__(self, other):
        return (self.priority, self.seq) < (other.priority, other.seq)


class LLMScheduler:
    """Priority queue over a few Ollama slots.

    Errors from a job (LLMError, anything else) reach the submitter as ordinary
    exceptions and never stop the scheduler. A deadline is checked only when a
    job would start; callers re-check it after generation with expired().
    """

    def __init__(self, llm: LLMClient, *, clock: Clock, max_inflight: int = 3,
                 max_low_inflight: int = 1):
        self._llm = llm
        self._clock = clock
        self._max = max_inflight
        self._max_low = max_low_inflight
        self._heap: list[_Entry] = []
        self._seq = itertools.count()
        self._running: set[_Entry] = set()
        self._low_running = 0
        self._closed = False

    def pending(self) -> dict[Priority, int]:
        out = {p: 0 for p in Priority}
        for e in self._heap:
            if not e.fut.done():
                out[e.priority] += 1
        return out

    def expired(self, deadline: float | None) -> bool:
        return deadline is not None and self._clock.now() > deadline

    async def submit(self, priority: Priority,
                     job: Callable[[LLMClient], Awaitable[T]], *,
                     deadline: float | None = None) -> T:
        if self._closed:
            raise SchedulerClosed("scheduler is closed")
        fut = asyncio.get_running_loop().create_future()
        entry = _Entry(Priority(priority), next(self._seq), job, deadline, fut)
        heapq.heappush(self._heap, entry)
        fut.add_done_callback(lambda f, e=entry: self._on_future_done(e))
        self._pump()
        return await fut

    def _on_future_done(self, entry: _Entry) -> None:
        # The submitter was cancelled: stop a running job so its slot is freed.
        if entry.fut.cancelled() and entry.task is not None and not entry.task.done():
            entry.task.cancel()

    def _pump(self) -> None:
        if self._closed:
            return
        deferred: list[_Entry] = []
        while self._heap and len(self._running) < self._max:
            entry = heapq.heappop(self._heap)
            if entry.fut.done():
                continue
            if entry.deadline is not None and self._clock.now() > entry.deadline:
                entry.fut.set_exception(Expired(entry.deadline))
                continue
            if entry.priority in _LOW and self._low_running >= self._max_low:
                deferred.append(entry)  # held back; higher priorities behind it may still run
                continue
            self._start(entry)
        for e in deferred:
            heapq.heappush(self._heap, e)

    def _start(self, entry: _Entry) -> None:
        self._running.add(entry)
        if entry.priority in _LOW:
            self._low_running += 1
        entry.task = asyncio.get_running_loop().create_task(self._run(entry))
        # Bookkeeping lives in a done callback: it also fires for a task that was
        # cancelled before its first step and so never entered _run.
        entry.task.add_done_callback(lambda t, e=entry: self._task_done(e))

    async def _run(self, entry: _Entry) -> None:
        try:
            result = await entry.job(self._llm)
        except Exception as e:
            if not entry.fut.done():
                entry.fut.set_exception(e)
        else:
            if not entry.fut.done():
                entry.fut.set_result(result)

    def _task_done(self, entry: _Entry) -> None:
        self._running.discard(entry)
        if entry.priority in _LOW:
            self._low_running -= 1
        task = entry.task
        if task is not None and not task.cancelled() and task.exception() is not None:
            # Only a BaseException gets here (_run hands every Exception to the submitter).
            # Retrieved and logged now, not reported as "never retrieved" whenever it is freed.
            log.error("llm_job_died: %r", task.exception())
        if not entry.fut.done():
            entry.fut.set_exception(SchedulerClosed("job cancelled"))
        self._pump()

    async def close(self) -> None:
        self._closed = True
        for e in self._heap:
            if not e.fut.done():
                e.fut.set_exception(SchedulerClosed("scheduler closed"))
        self._heap.clear()
        tasks = [e.task for e in self._running if e.task is not None]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
