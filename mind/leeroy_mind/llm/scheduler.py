import asyncio
import heapq
import itertools
import logging
from contextlib import asynccontextmanager
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
    __slots__ = ("priority", "seq", "job", "deadline", "credit", "fut", "task", "parked", "resume", "requeued")

    def __init__(self, priority, seq, job, deadline, fut, credit=None):
        self.priority = priority
        self.seq = seq
        self.job = job
        self.deadline = deadline
        self.credit = credit
        self.fut = fut
        self.task: asyncio.Task | None = None
        self.parked = False
        self.resume = None
        self.requeued = False

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
        self.gpu_gate = None
        self.live_borrow = None

    def pending(self) -> dict[Priority, int]:
        out = {p: 0 for p in Priority}
        for e in self._heap:
            if not e.fut.done():
                out[e.priority] += 1
        for e in self._running:
            if e.parked and not e.requeued and not e.fut.done():
                out[e.priority] += 1
        return out

    def expired(self, deadline: float | None, *, credit=None) -> bool:
        if credit is not None:
            deadline = credit.deadline(deadline)
        return deadline is not None and self._clock.now() > deadline

    async def submit(self, priority: Priority,
                     job: Callable[[LLMClient], Awaitable[T]], *,
                     deadline: float | None = None, credit=None) -> T:
        if self._closed:
            raise SchedulerClosed("scheduler is closed")
        if self.gpu_gate is not None:
            self.gpu_gate.check()
        fut = asyncio.get_running_loop().create_future()
        if credit is None and self.live_borrow is not None and deadline is not None:
            credit = self.live_borrow.mark()
        entry = _Entry(Priority(priority), next(self._seq), job, deadline, fut, credit)
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
        while self._heap and sum(not e.parked for e in self._running) < self._max:
            entry = heapq.heappop(self._heap)
            if entry.fut.done():
                continue
            if self.expired(entry.deadline, credit=entry.credit):
                error = Expired(entry.credit.deadline(entry.deadline)
                                if entry.credit is not None else entry.deadline)
                if entry.resume is not None:
                    entry.resume.set_exception(error)
                else:
                    entry.fut.set_exception(error)
                continue
            if entry.priority in _LOW and self._low_running >= self._max_low:
                deferred.append(entry)  # held back; higher priorities behind it may still run
                continue
            self._start(entry)
        for e in deferred:
            heapq.heappush(self._heap, e)

    def _start(self, entry: _Entry) -> None:
        self._running.add(entry)
        entry.parked = False
        if entry.priority in _LOW:
            self._low_running += 1
        if entry.resume is not None:
            ready, entry.resume = entry.resume, None
            entry.requeued = False
            ready.set_result(None)
            return
        entry.task = asyncio.get_running_loop().create_task(self._run(entry))
        # Bookkeeping lives in a done callback: it also fires for a task that was
        # cancelled before its first step and so never entered _run.
        entry.task.add_done_callback(lambda t, e=entry: self._task_done(e))

    async def _run(self, entry: _Entry) -> None:
        # Cancellation can beat this task's first step after _pump admitted it.
        if entry.fut.done():
            return
        from .borrow import WAIT_SLOT
        token = WAIT_SLOT.set(lambda blocked: self._borrow_wait(entry,blocked)) if self.live_borrow is not None else None
        try:
            if self.gpu_gate is not None:
                result = await self.gpu_gate.run(lambda: entry.job(self._llm))
            else:
                result = await entry.job(self._llm)
        except Exception as e:
            if not entry.fut.done():
                entry.fut.set_exception(e)
        else:
            if not entry.fut.done():
                entry.fut.set_result(result)
        finally:
            if token is not None:
                WAIT_SLOT.reset(token)

    @asynccontextmanager
    async def _borrow_wait(self, entry, blocked):
        if not blocked and not entry.parked:
            yield
            return
        if not entry.parked:
            entry.parked = True
            entry.resume = asyncio.get_running_loop().create_future()
            entry.requeued = False
            if entry.priority in _LOW:
                self._low_running -= 1
            self._pump()
        try:
            yield
        finally:
            # gather/PauseGate children inherit this context. Every waiter joins
            # the same slot future; none may start an exchange while a sibling is
            # still queued to regain the job's slot. Shield prevents one cancelled
            # child from cancelling admission for all its siblings.
            if not self._closed and not entry.fut.done() and entry.parked:
                ready = entry.resume
                if not entry.requeued:
                    entry.requeued = True
                    heapq.heappush(self._heap, entry)
                    self._pump()
                await asyncio.shield(ready)

    def _task_done(self, entry: _Entry) -> None:
        self._running.discard(entry)
        if entry.priority in _LOW and not entry.parked:
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
