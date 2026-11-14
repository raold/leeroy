"""Opt-in chat admission while a requested image temporarily owns the GPU.

This is separate from PauseGate: borrowing waits for active chats, whereas Free GPU
cancels model work. Embedding calls never enter this gate. Deadline credit measures
the full admission wait, including safe worker teardown and live-model recovery;
the image work itself has a separate 180-second budget.
"""
import asyncio
from contextlib import nullcontext
from contextvars import ContextVar
from dataclasses import dataclass

from .client import LLMError

# A scheduler job can lend its slot while admission waits, then rejoin the normal
# priority queue. Direct client callers have no scheduler slot to lend.
WAIT_SLOT = ContextVar('borrow_wait_slot', default=None)


class LiveReloadFailed(LLMError):
    """Admission stays closed until a later explicit recovery succeeds."""


class BorrowInterrupted(LLMError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class DeadlineCredit:
    gate: 'BorrowGate'
    baseline: float

    @property
    def seconds(self):
        return max(0.0, self.gate.elapsed - self.baseline)

    def deadline(self, base):
        return None if base is None else base + self.seconds


class BorrowGate:
    def __init__(self, clock):
        self.clock = clock
        self._ready = asyncio.Event()
        self._ready.set()
        self._idle = asyncio.Event()
        self._idle.set()
        self._active = set()
        self._since = None
        self._elapsed = 0.0
        self._error = None

    @property
    def blocked(self):
        return self._since is not None

    @property
    def elapsed(self):
        return self._elapsed + (max(0.0, self.clock.now() - self._since)
                                if self._since is not None else 0.0)

    def mark(self):
        return DeadlineCredit(self, self.elapsed)

    def close(self):
        if self.blocked:
            raise LiveReloadFailed('live admission is already closed')
        self._since = self.clock.now()
        self._error = None
        self._ready.clear()

    def open(self):
        self._elapsed = self.elapsed
        self._since = None
        self._error = None
        self._ready.set()

    def fail(self, code='live_reload_failed'):
        self._error = LiveReloadFailed(code)
        self._ready.set()  # wake waiters with an explicit failure, never false readiness

    async def wait(self, *, recover=None):
        while True:
            park = WAIT_SLOT.get()
            async with park(self.blocked) if park is not None else nullcontext():
                if self.blocked and recover is not None:
                    await recover()
                while self.blocked:
                    if self._error is not None:
                        raise self._error
                    await self._ready.wait()
            if not self.blocked:
                return
            # Rejoining the scheduler can itself wait. Another borrow may have
            # closed the gate meanwhile, so repeat before registering an exchange.

    async def run(self, factory):
        await self.wait()
        # No await between the open check and registration. A borrow on this event
        # loop either sees this exchange in drain(), or closes admission before it.
        task = asyncio.create_task(factory())
        self._active.add(task)
        self._idle.clear()
        try:
            return await task
        finally:
            self._active.discard(task)
            if not self._active:
                self._idle.set()

    async def drain(self):
        """Join active HTTP calls without aborting them or releasing their slots."""
        await self._idle.wait()
