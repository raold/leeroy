"""Installed scheduler state and owned asynchronous work at the training boundary."""

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import random
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from leeroy_mind.guild.config import GuildConfig
from leeroy_mind.guild.loot import _put
from leeroy_mind.guild.offline import LOGOUT_KEY, OfflineRaids
from leeroy_mind.guild.raid_night import RaidNight
from leeroy_mind.training.config import TrainingConfig
from leeroy_mind.training.protocol import GetRivals, Rivals
from leeroy_mind.training.service import TrainingGround, attach_training
from leeroy_mind.training.spells import SpellBook
from leeroy_mind.testing.fake_clock import FakeClock

from guild.fakes import FakeArmory
from .test_tg_applier import vocabulary
from .test_tg_gate import _run_async
from .test_tg_ledger import header, member
from .test_tg_service import enable


@pytest.fixture
def jobs(fake):
    fake.clock = FakeClock(datetime(2026, 11, 4, 9, tzinfo=timezone.utc))
    fake.cfg = TrainingConfig(enabled=True)
    fake.connection_epoch = 1
    fake.feed = lambda ev: None
    fake.disconnect = AsyncMock()
    fake.benchmark_allowed = lambda map_, boss, size, heroic: boss == 0
    cfg = GuildConfig(offline_window=("13:00", "23:00"))
    armory = FakeArmory({}, {})
    with fake.conn:
        _put(fake.conn, LOGOUT_KEY, fake.clock.now() - 7200)
        fake.conn.execute("INSERT INTO meta(key,value) VALUES ('guild_id','10')")
    guild = SimpleNamespace(cfg=cfg, _ready=lambda: True,
        offline=OfflineRaids(fake, cfg, armory, random.Random(1)),
        raid_night=RaidNight(fake, cfg, armory, random.Random(1)),
        roster=SimpleNamespace(premade={}))
    ground = TrainingGround(SimpleNamespace(guild=guild), fake)
    ground._connected = ground._presence_known = ground._startup_ready = True
    ground.vocab, ground._book = vocabulary(), SpellBook()
    return ground, guild


def test_schedules_use_actual_config_logout_and_known_scheduler_state(jobs):
    ground, guild = jobs
    lab = ground.jobs.lab_schedule()
    bench = ground.jobs.benchmark_schedule()
    assert lab.last_logout == ground.ports.clock.now() - 7200
    assert lab.offline_due_at == bench.next_offline_window == ground.ports.clock.now() + 4 * 3600
    assert bench.cast_guild == 10 and bench.bodies_present is False
    assert not bench.guild_busy and not bench.opening
    assert ground.jobs.benchmarks._schedule() is not None
    # The weekly rival window reserves this morning before the lab can claim.
    assert lab.benchmark_due_at <= ground.ports.clock.now()
    assert not ground.jobs.lab._due()


def test_disabled_benchmark_does_not_reserve_an_idle_future_window(jobs):
    ground, _ = jobs
    ground.ports.cfg = replace(ground.ports.cfg, benchmark=False)
    ground.jobs.lab.cfg = ground.ports.cfg
    schedule = ground.jobs.lab_schedule()
    assert schedule.benchmark_due_at is None and not schedule.benchmark_active
    assert ground.jobs.lab._due()
    # Disabled admission never releases unresolved existing benchmark ownership.
    ground.jobs.benchmarks._corrupt = True
    assert ground.jobs.lab_schedule().benchmark_active
    assert not ground.jobs.lab._due()


@pytest.mark.parametrize("missing", ["connection", "presence", "startup", "vocabulary", "book", "guild_ready", "controller"])
def test_unknown_readiness_never_becomes_known_idle(jobs, missing):
    ground, guild = jobs
    if missing in {"connection", "presence", "startup"}:
        setattr(ground, {"connection": "_connected", "presence": "_presence_known", "startup": "_startup_ready"}[missing], False)
    elif missing == "vocabulary":
        ground.vocab = replace(ground.vocab, complete=False)
    elif missing == "book":
        ground._book = None
    elif missing == "guild_ready":
        guild._ready = None
    else:
        guild.offline = SimpleNamespace()
    assert ground.jobs.lab_schedule() is None
    assert ground.jobs.benchmark_schedule() is None
    assert ground.jobs.spread_schedule({}) is None


@_run_async
async def test_actual_acquisition_lock_and_retained_cleanup_reserve_schedule(jobs):
    ground, guild = jobs
    async with guild.offline._lock:
        assert ground.jobs.lab_schedule().offline_active
        assert ground.jobs.benchmark_schedule().opening
        assert ground.jobs.spread_schedule({}).acquiring
    ground.listener._ending["retained"] = object()
    assert ground.jobs.lab_schedule().offline_active
    assert ground.jobs.benchmark_schedule().guild_busy
    assert ground.jobs.spread_schedule({}).raid_active


@_run_async
async def test_benchmark_acquisition_blocks_lab_without_blocking_event_lane(jobs, monkeypatch):
    ground, guild = jobs
    entered, release = asyncio.Event(), asyncio.Event()
    async def request(command, *args, **kwargs):
        assert isinstance(command, GetRivals)
        entered.set()
        await release.wait()
        return [Rivals(int(ground.ports.clock.now() * 1000), command.id, ())]
    ground.ports.request = request
    start = AsyncMock(side_effect=AssertionError("lab cannot start during rival acquisition"))
    monkeypatch.setattr(ground.jobs.lab, "_start", start)
    try:
        await asyncio.wait_for(ground.jobs.tick(), .2)
        await asyncio.wait_for(entered.wait(), 1)
        assert ground.jobs.lab_schedule().benchmark_active
        # Its own acquisition is not mistaken for a competing benchmark owner.
        assert ground.jobs.benchmarks._schedule() is not None
        await asyncio.wait_for(ground.jobs.lab.tick(), .2)
        start.assert_not_awaited()
    finally:
        release.set()
        await ground.jobs.close()


@_run_async
async def test_ledger_forwards_only_stored_scored_header_to_benchmark(jobs):
    ground, _ = jobs
    seen = []
    async def observe(event):
        row = ground.ports.conn.execute("SELECT checks_version,score FROM tg_pull WHERE id=?", (event.id,)).fetchone()
        assert row is not None and row[0] == 1 and row[1] is not None
        seen.append(event.id)
    ground.jobs.benchmarks.on_pull = observe
    pull = header(ground.ports)
    await ground.ledger.on_event(member(pull))
    assert not seen
    await ground.ledger.on_event(pull)
    assert seen == [pull.id]


def test_attach_registers_real_library_provider_and_stopping_receipts(app_stub):
    providers = []
    app_stub.engine.facts = SimpleNamespace(add_provider=providers.append)
    ground = attach_training(enable(app_stub))
    assert providers == [ground.jobs.lab.rotation_facts]
    assert ground.jobs.feed in app_stub.receipts["job"]
    assert ground.jobs.feed in app_stub.receipts["dummy_result"]


@_run_async
async def test_shutdown_joins_cancellation_resistant_lab_child_before_return(jobs):
    ground, _ = jobs
    entered, release, late_write = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def slow():
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()
        with ground.ports.conn:
            _put(ground.ports.conn, "synthetic:lab_drained", True)
        late_write.set()
    ground.jobs.lab.tick = slow
    ground.ports.cfg = replace(ground.ports.cfg, benchmark=False)
    await ground.jobs.tick()
    await asyncio.wait_for(entered.wait(), 1)
    close = asyncio.create_task(ground.close())
    try:
        await asyncio.sleep(0)
        assert not close.done() and not late_write.is_set()
        release.set()
        await asyncio.wait_for(close, 2)
        assert late_write.is_set() and ground.jobs._lab_task.done()
    finally:
        release.set()
        await close


@_run_async
async def test_concurrent_connection_loss_and_close_cancel_child_once(jobs):
    ground, _ = jobs
    entered, draining, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    cancellations = []
    async def slow():
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancellations.append("cancel")
            draining.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancellations.append("double cancel")
                raise
        with ground.ports.conn:
            _put(ground.ports.conn, "synthetic:late_lab_receipt", True)
    ground.jobs.lab.tick = slow
    ground.ports.cfg = replace(ground.ports.cfg, benchmark=False)
    await ground.jobs.tick()
    await asyncio.wait_for(entered.wait(), 1)
    connection = asyncio.create_task(ground.on_connection(False))
    await asyncio.wait_for(draining.wait(), 1)
    closing = asyncio.create_task(ground.close())
    try:
        for _ in range(8):
            await asyncio.sleep(0)
        assert cancellations == ["cancel"] and not closing.done() and not connection.done()
        release.set()
        await asyncio.wait_for(asyncio.gather(connection, closing), 2)
        assert ground.ports.conn.execute("SELECT value FROM meta WHERE key='synthetic:late_lab_receipt'").fetchone()
        assert cancellations == ["cancel"]
    finally:
        release.set()
        await asyncio.gather(connection, closing)


@_run_async
async def test_disconnect_and_close_join_the_actual_premade_file_worker(jobs, monkeypatch):
    from leeroy_mind.training import jobs as module
    ground, guild = jobs
    ground.ports.clock = FakeClock(datetime(2026, 11, 8, 13, tzinfo=timezone.utc))
    ground.ports.cfg = replace(ground.ports.cfg, benchmark=False, lab=False)
    ground.jobs.benchmarks.cfg = ground.jobs.lab.cfg = ground.ports.cfg
    ground.applier.acting = lambda now: True
    guild.roster.premade = None
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    def read(path):
        assert path.name == "playerbots.conf"
        entered.set()
        assert release.wait(5)
        finished.set()
        return {}
    monkeypatch.setattr(module, "read_premade_specs", read)
    await ground.jobs.tick()
    assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 2), 3)
    connection = asyncio.create_task(ground.on_connection(False))
    closing = asyncio.create_task(ground.close())
    try:
        for _ in range(10):
            await asyncio.sleep(0)
        assert not closing.done() and not connection.done() and not finished.is_set()
        assert not ground.jobs._premades_task.cancelled()
        release.set()
        await asyncio.wait_for(asyncio.gather(connection, closing), 3)
        assert finished.is_set() and ground.jobs._premades_task.done()
        assert not ground.ports.conn.execute("SELECT 1 FROM meta WHERE key GLOB 'tg:spread:week:*'").fetchone()
    finally:
        release.set()
        await asyncio.gather(connection, closing)
