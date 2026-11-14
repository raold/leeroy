"""One requested-image worker lifetime, owned by GpuArbiter's lock."""
import asyncio
import time

from leeroy_mind.llm.borrow import BorrowInterrupted
from leeroy_mind.selfies.worker import to_the_end
from .gpu import LiveLeaseHeld, PausedRoom, norm


class BorrowRoom:
    def __init__(self, arbiter, holder):
        self.arbiter, self.holder = arbiter, holder
        self.owner = asyncio.current_task()
        self.revision = arbiter._revision
        self.workers = []
        self.reason = None
        self.cleaning = False
        self.cancelled_owner = False
        self.done = asyncio.Event()
        self.started = time.monotonic()
        self.timings = {'drain_ms': 0, 'unload_ms': 0, 'stop_ms': 0, 'reload_ms': 0, 'ms': 0}

    def blockers(self):
        held = self.arbiter.holders - {'server', 'discord', self.holder}
        if self.arbiter.paused:
            held |= {'gpu_pause'}
        return frozenset(held)

    def check(self):
        if self.arbiter.paused:
            raise PausedRoom()
        if held := self.blockers():
            raise LiveLeaseHeld(held)
        if self.revision != self.arbiter._revision:
            raise BorrowInterrupted('models_changed')
        if self.reason is not None:
            raise BorrowInterrupted(self.reason)
        if self.cleaning or self.arbiter._borrow is not self:
            raise BorrowInterrupted('borrow_ended')

    def cancel(self, reason):
        if self.reason is None:
            self.reason = reason
        if not self.cleaning and not self.cancelled_owner and not self.owner.cancelling():
            self.cancelled_owner = True
            self.owner.cancel()

    def own_worker(self, worker):
        """Register BEFORE start, including a start that fails or is cancelled."""
        self.check()
        if worker not in self.workers:
            self.workers.append(worker)
        return worker

    async def make_room(self):
        """The borrow already owns the lock; server/Discord presence leases may stay."""
        self.check()
        began = time.monotonic()
        self.arbiter._shared_gpu = True
        try:
            listed = await to_the_end(self.arbiter.admin.loaded_models())
            self.check()
            remaining = list(listed)
            for name in sorted(listed, key=lambda n: norm(n) == norm(self.arbiter._live)):
                if norm(name) == norm(self.arbiter._embedding):
                    continue
                # Admin calls use threads too. Cancellation must join an unload;
                # otherwise a late unload could kill the freshly restored runner.
                await to_the_end(self.arbiter.admin.unload(name))
                remaining.remove(name)
                self.check()
            self.arbiter._prune_managed(remaining)
        finally:
            self.timings['unload_ms'] += round((time.monotonic() - began) * 1000)

    async def stop_workers(self):
        began = time.monotonic()
        try:
            failure = None
            for worker in self.workers:
                try:
                    await worker.stop()
                except Exception as error:
                    failure = failure or error
            if failure is not None:
                raise failure
        finally:
            self.timings['stop_ms'] += round((time.monotonic() - began) * 1000)

    async def finish(self):
        self.cleaning = True
        try:
            await self.stop_workers()
            if self.arbiter.paused:
                self.arbiter.needs_live_reload = True
                self.arbiter.live_admission.fail('gpu_paused')
                return
            # A timeout/cancel can arrive during the initial drain, before any
            # unload. Finish those existing calls before reconciliation too.
            # On explicit pause its own PauseGate drain cancels them before unload.
            await self.arbiter.live_admission.drain()
            began = time.monotonic()
            try:
                await self.arbiter._recover_borrow_locked()
            finally:
                self.timings['reload_ms'] += round((time.monotonic() - began) * 1000)
        finally:
            self.timings['ms'] = round((time.monotonic() - self.started) * 1000)
