"""Keep live chat resident outside the quiet window; give a running night one hour."""
import asyncio
import logging

from leeroy_mind.logs import log
from .clock import in_window, latest_window_bounds
from .config import ServerConfig

HOLDER = 'server'
OVERRUN_S = 3600.0
TICK_S = 30.0


class ServerLease:
    def __init__(self, app, config: ServerConfig):
        self.app, self.config = app, config
        self._held = False
        self._closed = False
        self._handbacks: set[asyncio.Task] = set()
        self._waited_since: float | None = None

    @property
    def held(self) -> bool:
        return self._held

    def ready(self) -> bool:
        return self._held and not self._handbacks

    async def tick(self, now: float) -> None:
        if self._closed:
            return
        local = self.app.clock.local()
        if in_window(local, self.config.quiet_window):
            self._waited_since = None
            await self._release('window')
            return
        if self._held:
            return
        # window_bounds outside the window points to the NEXT window. Overrun
        # belongs to the most recent end, including a window crossing midnight.
        _, end = latest_window_bounds(local, self.config.quiet_window, clock=self.app.clock)
        if self.app.night_scheduler.running_night() is not None and now < end + OVERRUN_S:
            if self._waited_since is None:
                self._waited_since = now
            return
        first = self.app.arbiter.hold(HOLDER)
        self._held = True
        if first:
            task = asyncio.create_task(self.app.arbiter.hand_over(), name='server-gpu-handback')
            self._handbacks.add(task)
            task.add_done_callback(self._handback_done)
        if self._waited_since is not None:
            log('server_lease_waited_for_night', s=max(0., now - self._waited_since))
            self._waited_since = None
        log('server_lease_held')

    def _handback_done(self, task: asyncio.Task) -> None:
        self._handbacks.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            # As with Discord, keep the lease on failure. Live admission can load
            # the model on its next request, and the night must stay excluded.
            log('server_handback_failed', level=logging.WARNING, error=type(error).__name__)

    async def handback(self) -> None:
        while self._handbacks:
            await asyncio.wait(set(self._handbacks))

    async def _release(self, reason: str) -> None:
        if self._held:
            await self.app.arbiter.release_live(HOLDER)
            self._held = False
            log('server_lease_released', reason=reason)

    async def run(self) -> None:
        while not self._closed:
            try:
                await self.tick(self.app.clock.now())
            except Exception as error:
                log('server_lease_tick_failed', level=logging.WARNING, error=type(error).__name__)
            await self.app.clock.sleep(TICK_S)

    async def close(self) -> None:
        self._closed = True
        tasks = list(self._handbacks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self._release('shutdown')
        await self.app.arbiter.settled()
