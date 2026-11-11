import asyncio
import time
from datetime import datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now(self) -> float: ...
    def monotonic(self) -> float: ...
    def local(self) -> datetime: ...
    async def sleep(self, s: float) -> None: ...


def local_at(ts: float) -> datetime:
    """The instant `ts` in this PC's zone, aware: the mind's local time (SystemClock.local), and
    the console's for the same instant (its Discord presence), so the two read one zone."""
    return datetime.fromtimestamp(ts).astimezone()


class SystemClock:
    def monotonic(self) -> float:
        return time.monotonic()

    def now(self) -> float:
        return time.time()

    def local(self) -> datetime:
        return local_at(self.now())

    async def sleep(self, s: float) -> None:
        await asyncio.sleep(s)


def now_ms(clock: Clock) -> int:
    return int(clock.now() * 1000)


def night_of(dt: datetime) -> str:
    return (dt - timedelta(hours=6)).date().isoformat()
