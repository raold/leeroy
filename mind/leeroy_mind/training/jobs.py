"""Own lab/benchmark lanes and derive admission from the installed schedulers."""

import asyncio
import logging

from leeroy_mind.guild.loot import _get
from leeroy_mind.guild.offline import LOGOUT_KEY
from leeroy_mind.guild.protocol import Job

from .benchmark import BenchmarkSchedule, Benchmarks
from .lab_runner import LabRunner, guild_schedule, next_window
from .protocol import DummyResult, Rivals


log = logging.getLogger(__name__)


class TrainingJobs:
    def __init__(self, ground):
        self.ground, self.ports = ground, ground.ports
        self._closed = False
        self._lab_task = None
        self.lab = LabRunner(self.ports, self.ports.cfg, ground, ground.applier,
                             schedule=self.lab_schedule)
        self.benchmarks = Benchmarks(self.ports, self.ports.cfg,
                                     schedule_provider=self.benchmark_schedule)

    def members(self):
        return self.lab.members() | self.benchmarks.members()

    def _guild(self):
        ground = self.ground
        guild = getattr(ground.app, "guild", None)
        if (self._closed or ground._stopping or not ground._connected
                or not ground._presence_known or not ground._startup_ready
                or not ground.vocab.complete or ground.book() is None
                or guild is None or not callable(getattr(guild, "_ready", None))
                or guild._ready() is not True):
            return None
        # These are installed scheduler interfaces, not default-false guesses.
        controllers = (getattr(guild, "raid_night", None), getattr(guild, "offline", None))
        if (getattr(guild, "cfg", None) is None
                or any(c is None or type(getattr(c, "active", None)) is not bool
                       or not hasattr(c, "_lock") for c in controllers)):
            return None
        return guild

    def _busy(self, guild):
        controllers = (guild.raid_night, guild.offline)
        return bool(any(c.active or c._lock.locked() for c in controllers)
                    or self.ground.listener.run is not None or self.ground.listener._ending
                    or self.ports.conn.execute(
                        "SELECT 1 FROM guild_raid WHERE ended IS NULL LIMIT 1").fetchone())

    def lab_schedule(self):
        guild = self._guild()
        if guild is None:
            return None
        return guild_schedule(self.ports, guild.cfg, offline_active=self._busy(guild),
            benchmark_active=self.benchmarks.active, benchmark_due_at=self.benchmarks.next_due)

    def benchmark_schedule(self):
        guild = self._guild()
        if guild is None:
            return None
        row = self.ports.conn.execute("SELECT value FROM meta WHERE key='guild_id'").fetchone()
        try:
            cast_guild = int(row[0]) if row else 0
            # Reserve the actual configured window, even if its daily quota is
            # exhausted. A missing/closed window cannot establish a finite margin.
            due = next_window(self.ports.clock.local(), guild.cfg.offline_window)
        except (TypeError, ValueError):
            return None
        if due is None:
            return None
        return BenchmarkSchedule(self.ports.clock.now(), _get(self.ports.conn, LOGOUT_KEY),
            due, cast_guild, self._busy(guild), self.lab.active,
            any(c._lock.locked() for c in (guild.raid_night, guild.offline)),
            False, getattr(self.ground.app, "bodies", None) is not None)
        # benchmark_busy is false because this is the one benchmark owner. Its
        # own acquisition task must be able to revalidate this snapshot.

    def feed(self, ev):
        """Synchronous receipts also work during MindApp's shutdown drain."""
        self.lab.feed(ev)
        if isinstance(ev, Job):
            self.benchmarks.feed_job(ev)

    async def after_store(self, record, context):
        await self.benchmarks.on_pull(record.header)

    async def _lab_tick(self):
        try:
            await self.lab.tick()
        except Exception:
            log.warning("training_lab_tick_failed")

    async def tick(self):
        if self._closed or not self.ground._connected or self.ground._stopping:
            return
        # Schedule benchmark first. Its active acquisition task is visible to
        # the lab before the first request yields; both recheck after awaits.
        await self.benchmarks.tick()
        if self._lab_task is None or self._lab_task.done():
            self._lab_task = asyncio.create_task(self._lab_tick(), name="training-lab")

    async def disconnect(self):
        task = self._lab_task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self.benchmarks.disconnect()
        await self.lab.disconnect()

    async def close(self):
        self._closed = True
        await self.disconnect()
        await self.benchmarks.close()
        await self.lab.close()
