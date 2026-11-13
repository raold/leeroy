"""One process-wide admission gate for the owner's persisted GPU pause."""
import asyncio


class PauseGate:
    def __init__(self, paused=False):
        self.paused = paused
        self._ready = asyncio.Event()
        if not paused:
            self._ready.set()
        self._active = set()
        self._pause_cancelled = set()
        self._lingering = set()

    def set(self, paused):
        self.paused = paused
        (self._ready.clear if paused else self._ready.set)()

    def check(self):
        if self.paused:
            from .client import GpuPaused
            raise GpuPaused('The GPU is paused.')

    async def wait(self):
        await self._ready.wait()

    async def run(self, factory):
        self.check()
        task = asyncio.create_task(factory())
        self._active.add(task)
        try:
            return await task
        except asyncio.CancelledError:
            if task in self._pause_cancelled and not asyncio.current_task().cancelling():
                from .client import GpuPaused
                raise GpuPaused('A GPU pause interrupted this request.') from None
            raise
        finally:
            self._active.discard(task)
            self._pause_cancelled.discard(task)

    def linger(self, work):
        """A request whose caller stopped waiting while its thread still runs: the next drain
        waits for it too, so a pause never unloads before a late load lands."""
        self._lingering.add(work)
        work.add_done_callback(self._lingered)

    def _lingered(self, work):
        self._lingering.discard(work)
        if not work.cancelled():
            work.exception()  # retrieved: the caller that would have read it is gone

    async def drain(self):
        tasks = tuple(self._active)
        self._pause_cancelled.update(tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._lingering:
            await asyncio.wait(tuple(self._lingering))
