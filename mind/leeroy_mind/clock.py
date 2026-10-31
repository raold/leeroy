import asyncio
import time
from datetime import datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now(self) -> float: ...
    def local(self) -> datetime: ...
    async def sleep(self, s: float) -> None: ...


class SystemClock:
    def now(self) -> float:
        return time.time()

    def local(self) -> datetime:
        return datetime.now().astimezone()

    async def sleep(self, s: float) -> None:
        await asyncio.sleep(s)


def now_ms(clock: Clock) -> int:
    return int(clock.now() * 1000)


def night_of(dt: datetime) -> str:
    return (dt - timedelta(hours=6)).date().isoformat()
