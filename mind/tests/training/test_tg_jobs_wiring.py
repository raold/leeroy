"""Installed scheduler state and owned asynchronous work at the training boundary."""

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import json
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
from leeroy_mind.training.discoveries import ATTEMPT_PREFIX, Discoveries
from leeroy_mind.training.protocol import GetRivals, Rivals
from leeroy_mind.training.service import TrainingGround, attach_training
from leeroy_mind.training.spells import SpellBook
from leeroy_mind.testing.fake_clock import FakeClock

from guild.fakes import FakeArmory, persona
from .test_tg_applier import vocabulary
from .test_tg_gate import _run_async
from .test_tg_ledger import header, member
from .test_tg_lab_runner import lab
from .test_tg_discoveries import finish_lab
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
    ground = TrainingGround(SimpleNamespace(guild=guild, session_id=37), fake)
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
    assert ground.jobs.discovery_schedule() is None


@_run_async
async def test_actual_acquisition_lock_and_retained_cleanup_reserve_schedule(jobs):
    ground, guild = jobs
    async with guild.offline._lock:
        assert ground.jobs.lab_schedule().offline_active
        assert ground.jobs.benchmark_schedule().opening
        assert ground.jobs.spread_schedule({}).acquiring
        assert ground.jobs.discovery_schedule().acquiring
    ground.listener._ending["retained"] = object()
    assert ground.jobs.lab_schedule().offline_active
    assert ground.jobs.benchmark_schedule().guild_busy
    assert ground.jobs.spread_schedule({}).raid_active
    assert ground.jobs.discovery_schedule().raid_active


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


def test_discovery_schedule_uses_actual_current_session_and_presence(jobs):
    ground, _ = jobs
    assert isinstance(ground.jobs.discoveries, Discoveries)
    assert ground.jobs.discoveries.ports is ground.ports
    assert ground.jobs.discoveries.cfg is ground.ports.cfg
    ground.ports.online = True
    proof = ground.jobs.discovery_schedule()
    assert proof.session_id == 37 and proof.observed_at == ground.ports.clock.now()
    assert proof.connected and proof.presence_known and proof.human_online
    assert not proof.raid_active and not proof.lab_active and not proof.benchmark_active and not proof.acquiring
    # A second session on the same wall date is a distinct installed session.
    ground.app.session_id = 38
    assert ground.jobs.discovery_schedule().session_id == 38
    ground.ports.online = False
    assert ground.jobs.discovery_schedule().human_online is False


@pytest.mark.parametrize("session", [None, 0, -1, True, False, "37", 0x100000000])
def test_unknown_or_invalid_application_session_cannot_admit_a_discovery(jobs, session):
    ground, _ = jobs
    ground.app.session_id = session
    ground.ports.online = True
    assert ground.jobs.discovery_schedule() is None


@pytest.mark.parametrize("unknown", ["human", "connected", "presence", "lock", "provider_error", "missing_session"])
def test_discovery_schedule_never_invents_unknown_facts(jobs, unknown):
    ground, guild = jobs
    if unknown == "human":
        ground.ports.online = None
    elif unknown == "connected":
        ground._connected = "yes"
    elif unknown == "presence":
        ground._presence_known = "yes"
    elif unknown == "lock":
        guild.offline._lock = SimpleNamespace(locked=lambda: None)
    elif unknown == "missing_session":
        del ground.app.session_id
    else:
        def unavailable():
            raise RuntimeError("scheduler unavailable")
        guild._ready = unavailable
    assert ground.jobs.discovery_schedule() is None


@pytest.fixture
def discovery_jobs(jobs, lab):
    """A real completed synthetic lab consumed by the installed coordinator."""
    ground, guild = jobs
    runner, _, _ = lab
    asyncio.run(finish_lab(ground.ports, runner))
    ground.jobs.lab = runner
    ground.jobs.discoveries.cfg = ground.jobs.benchmarks.cfg = ground.ports.cfg
    ground.ports.online = True
    for guid in ground.ports.members:
        ground.ports.personas[guid] = persona(guid, "SyntheticMage", main_spec="fire", raid_role="ranged",
                                            thinks_as="theorycrafter")
    assert ground.jobs.discoveries._verified() is not None
    return ground, guild


def discovery_attempts(ground):
    return [json.loads(r[0]) for r in ground.ports.conn.execute(
        "SELECT value FROM meta WHERE key LIKE ?", (ATTEMPT_PREFIX + "%",))]


@_run_async
async def test_coordinator_proposes_before_creating_noop_benchmark_and_lab_tasks(discovery_jobs):
    ground, _ = discovery_jobs
    jobs, order = ground.jobs, []
    discovery_tick, benchmark_tick, lab_tick = jobs.discoveries.tick, jobs.benchmarks.tick, jobs.lab.tick
    async def discovery_first():
        order.append("discovery")
        assert not jobs.discovery_schedule().acquiring
        return await discovery_tick()
    async def benchmark_second():
        order.append("benchmark")
        assert len(ground.ports.proposed) == 1
        await benchmark_tick()
        assert jobs.benchmarks.active  # Even the online no-op owns a fresh task.
    async def lab_last():
        order.append("lab")
        await lab_tick()
    jobs.discoveries.tick, jobs.benchmarks.tick, jobs.lab.tick = discovery_first, benchmark_second, lab_last
    try:
        await jobs.tick()
        await jobs._lab_task
        await jobs.benchmarks._task
        assert order == ["discovery", "benchmark", "lab"]
        assert len(ground.ports.proposed) == 1
        assert all(r["session"] == ground.app.session_id and r["state"] == "proposed_unconfirmed"
                   for r in discovery_attempts(ground))
    finally:
        await jobs.close()


@pytest.mark.parametrize("busy", ["active_raid", "retained_raid", "open_sql", "closing_raid", "offline", "lab", "benchmark"])
@_run_async
async def test_installed_retained_or_active_work_blocks_real_discovery(discovery_jobs, busy):
    ground, guild = discovery_jobs
    if busy == "active_raid":
        ground.listener.run = object()
    elif busy == "retained_raid":
        ground.listener._ending["retained"] = object()
    elif busy == "open_sql":
        with ground.ports.conn:
            ground.ports.conn.execute("INSERT INTO guild_raid(started,raid_key,size,heroic,offline,lead_guid,tank_guid,loot_guid) "
                "VALUES (0,'icc',10,0,0,101,101,101)")
    elif busy == "closing_raid":
        guild.raid_night._closing.append(object())
    elif busy == "offline":
        guild.offline._owned = {"phase": "ending"}
    elif busy == "lab":
        ground.jobs.lab._owned["phase"] = "blocked"
    else:
        ground.jobs.benchmarks._corrupt = True
    proof = ground.jobs.discovery_schedule()
    assert proof.raid_active or proof.lab_active or proof.benchmark_active
    assert not await ground.jobs.discoveries.tick()
    assert not ground.ports.proposed and not discovery_attempts(ground)


@pytest.mark.parametrize("lane", ["lab", "benchmark"])
@_run_async
async def test_unfinished_acquisition_without_owned_members_blocks_discovery(discovery_jobs, lane):
    ground, _ = discovery_jobs
    jobs, release = ground.jobs, asyncio.Event()
    pending = asyncio.create_task(release.wait())
    if lane == "lab":
        jobs._lab_task = pending
        assert not jobs.lab.active
    else:
        jobs.benchmarks._task = pending
    try:
        assert jobs.members() == set()
        assert jobs.discovery_schedule().acquiring
        assert not await jobs.discoveries.tick() and not ground.ports.proposed
        release.set()
        await pending
        assert not jobs.discovery_schedule().acquiring
        # The next coordinator tick can speak before it creates new no-op work.
        await jobs.tick()
        assert len(ground.ports.proposed) == 1
    finally:
        release.set()
        await jobs.close()


@_run_async
async def test_real_session_change_after_reservation_cancels_the_proposal(discovery_jobs):
    ground, _ = discovery_jobs
    safe, calls = ground.jobs.discoveries._safe, 0
    def session_change():
        nonlocal calls
        calls += 1
        if calls == 2:
            ground.app.session_id += 1
        return safe()
    ground.jobs.discoveries._safe = session_change
    assert not await ground.jobs.discoveries.tick() and not ground.ports.proposed
    rows = discovery_attempts(ground)
    assert len(rows) == 2 and all(r["state"] == "cancelled" and r["session"] == 37 for r in rows)


@pytest.mark.parametrize("boundary", ["disconnect", "stopping", "closed"])
@_run_async
async def test_real_disconnect_or_shutdown_prevents_discovery_and_child_launch(discovery_jobs, boundary):
    ground, _ = discovery_jobs
    jobs = ground.jobs
    if boundary == "disconnect":
        await ground.on_connection(False)
    elif boundary == "stopping":
        ground._stopping = True
    else:
        await jobs.close()
    assert jobs.discovery_schedule() is None
    assert not await jobs.discoveries.tick()
    await jobs.tick()
    assert jobs._lab_task is None and jobs.benchmarks._task is None
    assert not ground.ports.proposed and not discovery_attempts(ground)
    await jobs.close()


@_run_async
async def test_inflight_disconnect_fences_discovery_even_before_transport_flag_changes(discovery_jobs):
    ground, _ = discovery_jobs
    jobs, entered, release = ground.jobs, asyncio.Event(), asyncio.Event()
    original = jobs.benchmarks.disconnect
    async def draining():
        entered.set()
        await release.wait()
        await original()
    jobs.benchmarks.disconnect = draining
    disconnect = asyncio.create_task(jobs.disconnect())
    try:
        await entered.wait()
        assert ground._connected  # The jobs boundary supplies its own drain fence.
        assert jobs.discovery_schedule() is None
        assert not await jobs.discoveries.tick()
        await jobs.tick()
        assert not ground.ports.proposed and not discovery_attempts(ground)
    finally:
        release.set()
        await disconnect
        await jobs.close()


@_run_async
async def test_social_failure_does_not_starve_owned_job_lanes(jobs):
    ground, _ = jobs
    ground.jobs.discoveries.tick = AsyncMock(side_effect=RuntimeError("synthetic social failure"))
    ground.jobs.benchmarks.tick = AsyncMock()
    ground.jobs.lab.tick = AsyncMock()
    try:
        await ground.jobs.tick()
        await ground.jobs._lab_task
        ground.jobs.benchmarks.tick.assert_awaited_once()
        ground.jobs.lab.tick.assert_awaited_once()
    finally:
        await ground.jobs.close()


def test_a_failing_lab_tick_logs_its_exception_class_only(jobs, caplog):
    ground, _ = jobs
    async def broken():
        raise KeyError("PRIVATE_SENTINEL")
    ground.jobs.lab.tick = broken
    with caplog.at_level("WARNING", logger="leeroy_mind.training.jobs"):
        asyncio.run(ground.jobs._lab_tick())
    assert "training_lab_tick_failed error=KeyError" in caplog.text
    assert "PRIVATE_SENTINEL" not in caplog.text


def test_attach_keeps_dummy_cleanup_receipts_while_stopping(app_stub):
    ground = attach_training(enable(app_stub))
    assert ground.jobs.feed in app_stub.receipts["dummy_cleanup"]
