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

    async def drain(self):
        tasks = tuple(self._active)
        self._pause_cancelled.update(tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
