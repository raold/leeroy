import asyncio
import heapq
import itertools
from datetime import datetime, timedelta


class FakeClock:
    def __init__(self, start: datetime, *, yields: int = 0):
        """With `yields`, each step of advance() yields to the event loop that many times
        instead of sleeping 5 ms of real time (about 15 ms on Windows): for tests whose tasks
        do no real I/O or threads, so the loop settles without real time passing."""
        self._start = start
        self._t = start.timestamp()
        self._sleepers: list[tuple[float, int, asyncio.Future]] = []
        self._seq = itertools.count()
        self._yields = yields

    def now(self) -> float:
        return self._t

    def local(self) -> datetime:
        return self._start + timedelta(seconds=self._t - self._start.timestamp())

    async def sleep(self, s: float) -> None:
        if s <= 0:
            await asyncio.sleep(0)
            return
        fut = asyncio.get_running_loop().create_future()
        heapq.heappush(self._sleepers, (self._t + s, next(self._seq), fut))
        await fut

    async def advance(self, seconds: float, step: float = 0.5) -> None:
        target = self._t + seconds
        while self._t < target:
            self._t = min(self._t + step, target)
            while self._sleepers and self._sleepers[0][0] <= self._t:
                _, _, fut = heapq.heappop(self._sleepers)
                if not fut.done():
                    fut.set_result(None)
            if self._yields:
                for _ in range(self._yields):
                    await asyncio.sleep(0)
            else:
                await asyncio.sleep(0.005)
