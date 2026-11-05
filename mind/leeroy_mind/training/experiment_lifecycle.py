"""Owned experiment preparation, recovery and delayed summary delivery.

The guild calls preparation before its training listener. Only local SQL/file
work occurs there; speech belongs to TrainingGround's independently owned lane.
"""

import asyncio
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
import json
import logging
import math
from types import SimpleNamespace

from leeroy_mind.guild.loot import _get, _put


log = logging.getLogger(__name__)
_PENDING = "tg:summary_pending:"
_QUEUED = "tg:summary_queued:"


class ExperimentLifecycle:
    CAPACITY = 32
    REPLAY_BATCH = 8

    def __init__(self, ground, experimenter):
        self.ground, self.experimenter = ground, experimenter
        self.ports, self.conn = ground.ports, ground.ports.conn
        self._prepared = None
        self._generation = 0
        self._closed = False
        self._speech = None
        self._lock = asyncio.Lock()
        self._replay_after = 0
        self._replay_due = 0.0

    def _token(self, run):
        if (not is_dataclass(getattr(run, "plan", None))
                or not is_dataclass(getattr(getattr(run, "session", None), "plan", None))):
            return None
        return (run, asdict(run.plan), run.session.raid_id, run.session.started,
                run.session.ctx.map_id, run.session.ctx.instance_id,
                getattr(self.ports, "connection_epoch", None), self.ground._epoch, self._generation)

    def _offline_run(self):
        controller = getattr(getattr(self.ground.app, "guild", None), "offline", None)
        allowed = getattr(controller, "_actions_allowed", None)
        if not callable(allowed) or not allowed():
            return None  # A retained cancelled/terminal run only owns cleanup.
        return getattr(controller, "run", None)

    def _valid(self, token):
        if token is None or type(token[6]) is not int:
            return False
        run = token[0]
        if (self._closed or not self.ground.base_application_allowed()
                or self._offline_run() is not run or self._token(run) != token
                or not self.experimenter.ready() or not self.experimenter._offline(run)
                or run.session._closing):
            return False
        row = self.conn.execute("SELECT started FROM guild_raid WHERE raid_id=?", (run.session.raid_id,)).fetchone()
        return bool(row and row[0] == run.session.started and asdict(run.session.plan) == token[1]
                    and _get(self.conn, f"guild:raid:{run.session.raid_id}:plan") == token[1]
                    and type(run.session.ctx.instance_id) is int and run.session.ctx.instance_id > 0
                    and _get(self.conn, f"guild:raid:{run.session.raid_id}:instance") == run.session.ctx.instance_id)

    def admission_allowed(self, run):
        if run is None or not run.plan.offline or not self.ports.cfg.experiments:
            return True
        return bool(self._prepared is not None and self._prepared[0] is run
                    and self._valid(self._prepared))

    def invalidate(self, run=None):
        if run is None or self._prepared is None or self._prepared[0] is run:
            self._generation += 1
            self._prepared = None
            self.ground.listener.set_experimenter(None)

    def preempt(self):
        self.invalidate()
        if self._speech is not None:
            self._speech.cancel()

    async def prepare(self, run):
        if run is None or not self.ports.cfg.experiments:
            return False
        if self.admission_allowed(run):
            return self._prepared is not None
        token = self._token(run)
        self.ground.listener.set_experimenter(None)
        if not self._valid(token) or not self.experimenter.ready():
            return False
        # The accepted methods currently do not suspend or call transport. Keep
        # checks around each await so a future adapter cannot install an old run.
        if not await self.experimenter.drift_check(run) or not self._valid(token):
            return False
        await self.experimenter.open_trials()
        if not self._valid(token) or not self.experimenter.ready():
            return False
        with self.conn:
            _put(self.conn, "tg:experiment_refusals", dict(ts=self.ports.clock.now(),
                 raid=run.session.raid_id, reasons=self.experimenter.refusals))
        self._prepared = token
        self.ground.listener.set_experimenter(self.experimenter)
        return True

    async def on_start(self, run):
        self.invalidate()
        if run.plan.offline:
            await self.prepare(run)

    async def on_dc_status(self, ev):
        run = self._offline_run()
        if (run is not None and ev.tank_guid == run.plan.tank_guid
                and type(ev.ts) is int and 0 <= self.ports.clock.now() - ev.ts / 1000 <= 60):
            await self.prepare(run)

    async def on_before_end(self, run, outcome="ended"):
        self.invalidate(run)
        return True  # The following training listener owns the cleanup barrier.

    async def on_end(self, outcome, *, run=None):
        self.invalidate(run)
        if (self._closed or run is None or not run.plan.offline or run.human_in_raid
                or not getattr(run.session, "_close_done", False)):
            return
        row = self.conn.execute("SELECT * FROM guild_raid WHERE raid_id=?", (run.session.raid_id,)).fetchone()
        now = self.ports.clock.now()
        if (row is None or row["offline"] != 1 or row["ended"] is None or row["ended"] > now
                or row["started"] != run.session.started or row["lead_guid"] != run.plan.lead_guid):
            return
        key = _PENDING + str(run.session.raid_id)
        queued = _QUEUED + str(run.session.raid_id)
        if _get(self.conn, key) is not None or _get(self.conn, queued) is not None:
            return
        # The receipt is written only after the controller's mandatory cleanup
        # barrier and completed RaidSession.close, never from generic job.done.
        pending = self.conn.execute("SELECT count(*) FROM meta WHERE key GLOB ?", (_PENDING + "*",)).fetchone()[0]
        if pending >= self.CAPACITY:
            log.warning("training_summary_queue_full")
            return
        with self.conn:
            _put(self.conn, key, dict(raid=run.session.raid_id, started=run.session.started,
                ended=row["ended"], completed=now, plan=asdict(run.plan)))
            _put(self.conn, queued, dict(completed=now))

    async def after_store(self, rec, ctx):
        # This accepted interface is SQL-only and reads the committed scored row.
        # It remains attached even when no offline experimental arm is installed,
        # allowing the core's eligible human-champion observations.
        if (not self._closed and not self.ground._stopping and self.ports.cfg.enabled
                and self.ports.cfg.act and self.ports.cfg.experiments):
            await self.experimenter.on_pull(rec.header.id)

    def replay_again(self):
        self._replay_after, self._replay_due = 0, 0.0

    async def _replay(self):
        now = self.ports.clock.now()
        if (not self.ports.cfg.enabled or not self.ports.cfg.act or not self.ports.cfg.experiments
                or now < self._replay_due or not self.experimenter.ready()):
            return
        rows = self.conn.execute(
            "SELECT rowid AS ordinal,id FROM tg_pull WHERE rowid>? AND complete=1 AND scoreable=1 "
            "AND score IS NOT NULL AND (arm_id IS NOT NULL OR (kind='his' AND card_id IS NOT NULL)) "
            "ORDER BY rowid LIMIT ?", (self._replay_after, self.REPLAY_BATCH)).fetchall()
        for row in rows:
            if self._closed or self.ground._stopping:
                return
            await self.experimenter.on_pull(row["id"])
            self._replay_after = row["ordinal"]
        if len(rows) < self.REPLAY_BATCH:
            self._replay_after, self._replay_due = 0, now + 60

    def _summary_run(self, saved):
        if (type(saved) is not dict or type(saved.get("raid")) is not int
                or type(saved.get("plan")) is not dict
                or type(saved.get("completed")) not in (int, float)
                or not math.isfinite(saved["completed"])):
            return None
        row = self.conn.execute("SELECT * FROM guild_raid WHERE raid_id=?", (saved["raid"],)).fetchone()
        plan = saved["plan"]
        if (row is None or row["offline"] != 1 or row["ended"] is None
                or row["started"] != saved.get("started") or row["ended"] != saved.get("ended")
                or saved["completed"] < row["ended"]
                or plan.get("offline") is not True or plan.get("human_in_raid") is not False
                or any(row[key] != plan.get(key) for key in ("raid_key", "size", "heroic", "lead_guid"))):
            return None
        return SimpleNamespace(human_in_raid=False, plan=SimpleNamespace(**plan),
                               session=SimpleNamespace(raid_id=saved["raid"]))

    async def tick(self):
        async with self._lock:
            if (self._closed or self.ground._stopping or not self.ground._connected
                    or not self.ground._presence_known):
                return
            # Startup vocabulary/receipt evidence may become ready after the
            # guild's first notification. Prepare the exact still-owned run;
            # the native readiness adapter then retries listener startup.
            await self.prepare(self._offline_run())
            await self._replay()
            if (self._closed or self.ground._stopping or not self.ground._connected
                    or not self.ground._presence_known or self.ports.human_online()
                    or self.ground._guild_busy()):
                return
            now = self.ports.clock.now()
            for row in self.conn.execute("SELECT key,value FROM meta WHERE key GLOB ? ORDER BY key LIMIT ?",
                                         (_PENDING + "*", self.CAPACITY)).fetchall():
                try:
                    saved = json.loads(row["value"])
                    run = self._summary_run(saved)
                except (ValueError, TypeError):
                    run = None
                if run is None or now >= saved["completed"] + 86400:
                    with self.conn:
                        self.conn.execute("DELETE FROM meta WHERE key=?", (row["key"],))
                    continue
                if now < saved["completed"] + 60:
                    continue
                epoch = self.ground._epoch
                self._speech = asyncio.create_task(self.experimenter.offline_summary(run, saved["completed"]))
                day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
                try:
                    async with asyncio.timeout(15):
                        await self._speech
                except asyncio.CancelledError:
                    if asyncio.current_task().cancelling():
                        raise
                    return  # Presence/connection cancellation does not kill the lane.
                finally:
                    if self._speech.done():
                        self._speech = None
                    # Consume an attempted delivery even on cancellation or an
                    # ambiguous transport exception. No late replay tomorrow.
                    if _get(self.conn, f"tg:summary:{day}") is not None:
                        with self.conn:
                            self.conn.execute("DELETE FROM meta WHERE key=?", (row["key"],))
                if self._closed or self.ground._stopping or epoch != self.ground._epoch:
                    return
                # No observed trial yet is retryable: late pull receipts can be
                # ingested after a terminal. An actual daily attempt is final.
                return  # One owned speech attempt per tick.

    async def close(self):
        self._closed = True
        self.preempt()
        task = self._speech
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        async with self._lock:
            self._speech = None
