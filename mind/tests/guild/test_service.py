import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from leeroy_mind.config import load_config
from leeroy_mind.guild.config import GuildConfig
from leeroy_mind.guild.ports import JobWaiter, LazyArmory, LivePorts, MaintenancePorts
from leeroy_mind.guild.protocol import BotDo
from leeroy_mind.guild.raid_night import State
from leeroy_mind.guild.service import EVENTS, GuildTurnSource, install
from leeroy_mind.llm.scheduler import Priority
from leeroy_mind.protocol import Ack, Boss, Hello, Login, Logout, Resurrect
from leeroy_mind.storage.store import Store
from leeroy_mind.talk.turns import TurnKind

from .fakes import FakeArmory, job, member
from .test_raid_night import setup as raid_setup, form, inside, advance, controls


@pytest.fixture
def app(fake, tmp_path):
    store = Store(fake.conn, clock=fake.clock)
    cfg = load_config(tmp_path / "mind.toml")
    cfg = replace(cfg, human=replace(cfg.human, nickname="Testy", real_name="Synthetic Owner"))
    store.upsert_cast([member(1, "Testmage", 8, 1)], fake.clock.now())
    store.set_meta("guild_id", "21")
    engine = SimpleNamespace(sources=[], said=[], spoken=[], live_admission=None)
    engine.register_source = engine.sources.append
    engine.say_as = AsyncMock(return_value=True)
    engine.speak = AsyncMock(return_value=["done"])
    app = SimpleNamespace(clock=fake.clock, store=store, cfg=cfg, data_dir=tmp_path,
        human_guid=1, world=SimpleNamespace(human_online=True), engine=engine,
        personas=SimpleNamespace(get=lambda guid: fake.personas.get(guid)),
        raid=SimpleNamespace(set_lead=lambda guid: None),
        writer=SimpleNamespace(write=AsyncMock()),
        event_rules=SimpleNamespace(on_loot_lost=AsyncMock()),
        bridge=SimpleNamespace(send=AsyncMock()), arbiter=None, llm=object(),
        handlers={}, before={}, backgrounds=[], connections=[], is_backlog=lambda ev: False)
    def register(name, handler, *, before=False):
        (app.before if before else app.handlers).setdefault(name, []).append(handler)
    app.register_event_handler = register
    app.register_background = app.backgrounds.append
    app.register_connection_hook = app.connections.append
    return app


def armory():
    result = FakeArmory({}, {})
    result.roster = lambda guild: {}
    result.friends = lambda human: set()
    return result


def ready(app):
    service = install(app, armory=armory())
    service._connected = True
    return service


def test_install_only_registers_shared_ordered_listeners(app, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("construction must not create tasks")
    monkeypatch.setattr(asyncio, "create_task", forbidden)
    service = install(app, armory=armory())
    assert set(app.handlers) == set(EVENTS)
    assert set(app.before) == {"hello", "login", "logout"}
    assert app.backgrounds == [service.run] and app.engine.sources == [service.source]
    assert service.raid_night._listeners == service.offline._listeners == list(service.listeners)
    assert [type(item).__name__ for item in service.listeners] == ["Play", "RaidCalls", "BetweenPulls"]
    assert service.calls.gap is service.habits.gap is service.gap


def test_install_missing_database_logs_once_without_reading_credentials(app, monkeypatch, caplog):
    from leeroy_mind.personas import roster
    monkeypatch.setattr(roster, "read_db_info", lambda *a, **k: pytest.fail("credential read during install"))
    cfg = replace(GuildConfig(), worldserver_conf=app.data_dir / "missing.conf")
    service = install(app, cfg=cfg)
    assert not service.armory.available and service.armory._backend is None
    assert caplog.text.count("guild_armory_unavailable") == 1


def test_injected_client_never_queries_configured_database(app, monkeypatch):
    from leeroy_mind.personas import roster
    config = app.data_dir / "worldserver.conf"
    config.write_text("synthetic", encoding="utf-8")
    monkeypatch.setattr(roster, "read_db_info", lambda *a, **k: pytest.fail("live credential read"))
    service = install(app, cfg=replace(GuildConfig(), worldserver_conf=config))
    service._connected = True
    asyncio.run(service.roster_tick())
    assert not service.armory.available


def test_real_client_boundary_keeps_armory_lazy_and_injectable(app, monkeypatch):
    from leeroy_mind.llm.client import OllamaClient
    from leeroy_mind.personas import roster
    from leeroy_mind.guild import armory as module
    config = app.data_dir / "worldserver.conf"
    config.write_text("synthetic", encoding="utf-8")
    reads = []
    monkeypatch.setattr(roster, "read_db_info", lambda *a: reads.append(a) or object())
    monkeypatch.setattr(module, "MysqlArmory", lambda *a: SimpleNamespace(human_tier=lambda guid: 16))
    app.llm = OllamaClient("http://127.0.0.1:1")
    service = install(app, cfg=replace(GuildConfig(), worldserver_conf=config))
    assert reads == [] and service.armory.available
    assert service.armory.human_tier(1) == 16
    assert len(reads) == 2


def test_ports_propose_identity_and_priority(app):
    service = ready(app)
    ports = service.ports
    ports.propose(101, "raid", "The next pull.")
    ports.propose(102, "guild", "A quiet remark.", kind=TurnKind.ASIDE)
    turns = asyncio.run(service.source.propose(app.clock.now()))
    assert [(t.kind, t.priority, t.max_lines) for t in turns] == [
        (TurnKind.CALLOUT, Priority.RAID_LEAD, 1), (TurnKind.ASIDE, Priority.SOCIAL, 1)]
    assert [t.deadline - app.clock.now() for t in turns] == [30, 20]
    assert ports.human_name() == ports.human().called == "Testy"
    assert all("Synthetic Owner" not in t.instruction for t in turns)


def test_identity_is_refreshed_after_login_config_reload(app):
    service = ready(app)
    assert service.ports.human_name() == "Testy"
    app.cfg = replace(app.cfg, human=replace(app.cfg.human, nickname="Newnick"))
    asyncio.run(service.event(Login(1, 1, True), "login"))
    assert service.ports.human_name() == "Newnick"


def test_job_can_arrive_before_ack_and_unknown_jobs_never_satisfy_waiter(app):
    async def go():
        ports = LivePorts(app)
        command = BotDo("owned-command", (101,), "do", "drink")
        async def send(cmd, **kwargs):
            assert kwargs == {"preserve_id": True}
            ports.jobs.feed(job("bot_do", "done", id="unrelated", guids=(101,)))
            ports.jobs.feed(job("bot_do", "progress", id=cmd.id, guids=(101,)))
            ports.jobs.feed(job("bot_do", "done", id=cmd.id, guids=(101,)))
            return Ack(1, cmd.id, True, "")
        app.bridge.send = send
        assert (await ports.send(command)).id == command.id
        result = await ports.wait_job(command.id, .1)
        assert result.id == command.id and result.state == "done"
        assert await ports.wait_job("unrelated", .1) is None
    asyncio.run(go())


def test_job_mailbox_is_bounded_and_cancel_closes_waiters():
    async def go():
        waiter = JobWaiter(capacity=2)
        waiter.expect("one")
        pending = asyncio.create_task(waiter.wait("one", 20))
        await asyncio.sleep(0)
        waiter.expect("two")
        waiter.expect("three")
        assert set(waiter._jobs) == {"one", "three"}
        waiter.close()
        assert await pending is None
        assert not waiter._jobs and not waiter._wake
    asyncio.run(go())


def test_ports_follow_current_arbiter_and_join_other_admission(app):
    async def go():
        ports = LivePorts(app)
        await ports.acquire_gpu("offline_raid")
        app.arbiter = SimpleNamespace(acquire_live=AsyncMock(), release_live=AsyncMock())
        app.engine.live_admission = AsyncMock()
        await ports.acquire_gpu("offline_raid")
        app.arbiter.acquire_live.assert_awaited_once_with("offline_raid")
        app.engine.live_admission.assert_awaited_once()
        await ports.release_gpu("offline_raid")
        app.arbiter.release_live.assert_awaited_once_with("offline_raid")
    asyncio.run(go())


def test_second_guild_lease_cannot_overtake_first_model_handback(app):
    async def go():
        ports = LivePorts(app)
        release = asyncio.Event()
        entered = []
        async def acquire(holder):
            entered.append(holder)
            if holder == "offline_raid":
                await release.wait()
        app.arbiter = SimpleNamespace(acquire_live=acquire, release_live=AsyncMock())
        first = asyncio.create_task(ports.acquire_gpu("offline_raid"))
        second = asyncio.create_task(ports.acquire_gpu("guild_speech"))
        await asyncio.sleep(0)
        assert entered == ["offline_raid"]
        release.set()
        await asyncio.gather(first, second)
        assert entered == ["offline_raid", "guild_speech"]
    asyncio.run(go())


def test_offline_remote_ownership_routes_events_and_blocks_online_even_without_session(app):
    async def go():
        service = ready(app)
        service.offline._owned = {"phase": "uncertain"}
        service.offline.on_boss = AsyncMock()
        service.raid_night.on_boss = AsyncMock()
        service.raid_night.tick = AsyncMock()
        service.habits.on_boss = AsyncMock()
        event = Boss(1, 631, 9, 1, "", "killed", (101,))
        await service.event(event, "boss")
        await service.online_tick()
        service.offline.on_boss.assert_awaited_once_with(event)
        service.raid_night.on_boss.assert_not_awaited()
        service.raid_night.tick.assert_not_awaited()
    asyncio.run(go())


def test_resurrection_reaches_selected_controller_and_immediate_habit_observer(app):
    async def go():
        service = ready(app)
        service.raid_night.on_resurrect = AsyncMock()
        service.habits.on_resurrect = AsyncMock()
        event = Resurrect(1, 101)
        await service.event(event, "resurrect")
        service.raid_night.on_resurrect.assert_awaited_once_with(event)
        service.habits.on_resurrect.assert_awaited_once_with(event)
    asyncio.run(go())


def test_before_login_invalidates_offline_listeners_before_core_model_await(app):
    async def go():
        service = ready(app)
        run = SimpleNamespace(plan=SimpleNamespace(offline=True))
        for listener in service.listeners:
            listener._run = run
        await service.before_presence(Login(1, 1, True))
        assert all(listener._run is None for listener in service.listeners)
        assert not service._ready()
        await service.event(Login(1, 1, True), "login")
        assert service._ready()
    asyncio.run(go())


def test_stale_end_cannot_retire_new_listener_run(app):
    async def go():
        service = ready(app)
        old, current = object(), object()
        for listener in service.listeners:
            listener._run = current
            listener.invalidate(old)
            await listener.on_end("finished", run=old)
            assert listener._run is current
    asyncio.run(go())


def test_full_guild_snapshot_is_maintenance_only_and_uses_actual_membership(app):
    app.store.upsert_cast([member(101, "Arkon", 1, 2)], app.clock.now())
    ports = LivePorts(app)
    maintenance = MaintenancePorts(ports)
    maintenance.members = {101: member(101, "Arkon", 1, -1, online=False),
                           102: member(102, "Belra", 8, -1, level=50)}
    assert maintenance.cast()[101].spec_tab == 2
    assert maintenance.cast()[101].online is False
    assert maintenance.cast()[102].level == 50
    assert 102 not in ports.cast()
    maintenance.members.pop(101)
    assert 101 not in maintenance.cast() and 101 in ports.cast()


def test_friends_snapshot_reads_actual_social_flags_without_implicit_additions(app, monkeypatch):
    from leeroy_mind.personas import roster
    lazy = LazyArmory(replace(GuildConfig(), worldserver_conf=app.data_dir / "absent"))
    lazy.available = True
    lazy._backend = SimpleNamespace(chars=object())
    calls = []
    monkeypatch.setattr(roster, "run_mysql", lambda *args: calls.append(args) or [["101"], ["999"]])
    assert lazy.friends(1) == {101, 999}
    assert "character_social" in calls[0][1] and "(flags & 1)<>0" in calls[0][1]
    assert app.bridge.send.await_count == 0


def test_terminal_offline_proposal_speaks_with_own_lease_after_raid_lease_ended(app):
    async def go():
        service = ready(app)
        app.world.human_online = False
        app.arbiter = SimpleNamespace(acquire_live=AsyncMock(), release_live=AsyncMock())
        service.ports.propose(101, "guild", "Summarize this completed raid.")
        assert await service.source.propose(app.clock.now()) == []
        await service.speech_tick()
        app.engine.speak.assert_awaited_once()
        app.arbiter.acquire_live.assert_awaited_once_with("guild_speech")
        app.arbiter.release_live.assert_awaited_once_with("guild_speech")
        assert service.source.take(app.clock.now()) is None
    asyncio.run(go())


def test_offline_speech_login_during_admission_drops_stale_turn_and_releases(app):
    async def go():
        service = ready(app)
        app.world.human_online = False
        async def acquire(holder):
            app.world.human_online = True
        app.arbiter = SimpleNamespace(acquire_live=acquire, release_live=AsyncMock())
        service.ports.propose(101, "guild", "A completed raid.")
        await service.speech_tick()
        app.engine.speak.assert_not_awaited()
        app.arbiter.release_live.assert_awaited_once_with("guild_speech")
        assert await service.source.propose(app.clock.now()) == []
    asyncio.run(go())


def test_offline_speech_cancellation_releases_lease(app):
    async def go():
        service = ready(app)
        app.world.human_online = False
        entered = asyncio.Event()
        async def speak(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        app.engine.speak = speak
        app.arbiter = SimpleNamespace(acquire_live=AsyncMock(), release_live=AsyncMock())
        service.ports.propose(101, "guild", "A completed raid.")
        task = asyncio.create_task(service.speech_tick())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        app.arbiter.release_live.assert_awaited_once_with("guild_speech")
    asyncio.run(go())


def test_online_work_wait_never_stalls_dispatch_or_other_lanes_and_shutdown_drains(app):
    async def go():
        service = ready(app)
        blocked, other = asyncio.Event(), asyncio.Event()
        async def online():
            blocked.set()
            await asyncio.Event().wait()
        async def roster():
            other.set()
        service.online_tick = online
        service.roster_tick = roster
        service.offline.on_job = AsyncMock()
        service.roster.on_job = AsyncMock()
        task = asyncio.create_task(service.run())
        await asyncio.wait_for(blocked.wait(), 1)
        await asyncio.wait_for(other.wait(), 1)
        await service.event(job("bot_do", "done", id="unknown"), "job")
        service.roster.on_job.assert_awaited_once()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        assert not service._tasks and not service.ports.enabled
    asyncio.run(go())


def test_timed_countdown_uses_last_five_seconds_of_configured_dc_delay(raid_setup):
    from leeroy_mind.guild.calls import CallGap, RaidCalls
    async def go():
        ports, _, night = raid_setup
        calls = RaidCalls(ports, gap=CallGap(ports.clock))
        night.add_listener(calls)
        await form(raid_setup)
        ports.members = {g: replace(m, map=631) for g, m in ports.members.items()}
        await inside(raid_setup)
        await advance(ports, 30)
        await night.tick()
        assert controls(ports, "ready_check")
        assert not any("pulling in" in text for _, _, text in ports.said)
        await advance(ports, 14)
        await night.tick()
        assert not controls(ports, "dc_on")
        await advance(ports, 1)
        await night.tick()
        assert any("pulling in 5" in text for _, _, text in ports.said)
        assert not controls(ports, "dc_on")
        await advance(ports, 5)
        await night.tick()
        assert ports.said[-1][2] == "go" and controls(ports, "dc_on")
        assert night.state is State.RUNNING
    asyncio.run(go())


def test_invalidating_run_cancels_typing_and_queued_raid_lines_but_keeps_wrapup(app):
    async def go():
        service = ready(app)
        run = object()
        service.ports.run_for_speech = lambda: run
        entered = asyncio.Event()
        sent = []
        async def say(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
            sent.append(args)
        app.engine.say_as = say
        service.ports.propose(101, "raid", "A pending fight line.")
        service.ports.propose(101, "guild", "A completed raid wrap-up.")
        task = asyncio.create_task(service.ports.say(101, "raid", "go"))
        await entered.wait()
        service.ports.invalidate_run(run)
        assert await task is False and not sent
        pending = await service.source.propose(app.clock.now())
        assert len(pending) == 1 and pending[0].channel == "guild"
        assert not service.ports._speech
    asyncio.run(go())


def test_login_identity_change_invalidates_before_core_updates_owner(app):
    async def go():
        service = ready(app)
        current = SimpleNamespace(plan=SimpleNamespace(offline=False))
        for listener in service.listeners:
            listener._run = current
        service.raid_night._login_seen = True
        await service.before_presence(Login(1, 2, True))
        assert all(listener._run is None for listener in service.listeners)
        assert not service.raid_night._login_seen
    asyncio.run(go())


def test_failed_full_roster_snapshot_waits_sixty_seconds_and_blocks_maintenance(app):
    async def go():
        service = ready(app)
        queries = []
        def fail(guild):
            queries.append(guild)
            raise RuntimeError("synthetic unavailable")
        service.armory.roster = fail
        service.roster.tick = AsyncMock()
        with pytest.raises(RuntimeError):
            await service.roster_tick()
        await service.roster_tick()
        assert queries == [21] and not service.maintenance.members
        service.roster.tick.assert_not_awaited()
        await app.clock.advance(60, step=60)
        with pytest.raises(RuntimeError):
            await service.roster_tick()
        assert queries == [21, 21]
        service.armory.roster = lambda guild: {102: member(102, "Belra", 8, -1, level=50)}
        await app.clock.advance(60, step=60)
        await service.roster_tick()
        assert service.maintenance.cast()[102].level == 50
        service.roster.tick.assert_awaited_once()
    asyncio.run(go())


def test_habits_does_not_act_until_startup_controller_backlog_is_drained(app):
    async def go():
        service = ready(app)
        service.habits.tick = AsyncMock()
        service.habits.on_boss = AsyncMock()
        service.raid_night.on_boss = AsyncMock()
        event = Boss(1, 631, 9, 1, "", "engaged", (101,))
        async with service.raid_night._lock:
            await service.event(event, "boss")
            service.habits.on_boss.assert_awaited_once_with(event)
            await service.listener_tick(service.habits)
            service.habits.tick.assert_not_awaited()
            # Controller replay remains enabled: a first observation which
            # preceded initialization is not permanently lost.
            service.calls.on_boss = AsyncMock()
            await service.raid_night._notify("on_boss", event)
            assert service.habits.on_boss.await_count == 2
        await service.listener_tick(service.habits)
        service.habits.tick.assert_awaited_once()
    asyncio.run(go())


@pytest.mark.parametrize("online", [False, True])
def test_managed_generation_keeps_scope_and_releases_gpu_on_run_cancel(app, online):
    async def go():
        service = ready(app)
        service.source.managed = True
        app.world.human_online = online
        run = object()
        service.ports.run_for_speech = lambda: run
        app.arbiter = SimpleNamespace(acquire_live=AsyncMock(), release_live=AsyncMock())
        entered = asyncio.Event()
        finish = asyncio.Event()
        delivered = []
        async def speak(turn, **kwargs):
            entered.set()
            await finish.wait()
            delivered.append(turn.channel)
            return ["done"]
        app.engine.speak = speak
        service.ports.propose(101, "raid", "A scoped raid line.")
        service.ports.propose(101, "guild", "A completed raid wrap-up.")
        assert await service.source.propose(app.clock.now()) == []
        task = asyncio.create_task(service.speech_tick())
        await entered.wait()
        service.ports.invalidate_run(run)
        await task
        assert not delivered and not service.ports._speech
        app.arbiter.release_live.assert_awaited_once_with("guild_speech")
        # The terminal guild line is deliberately not an old raid action.
        entered.clear()
        terminal = asyncio.create_task(service.speech_tick())
        await entered.wait()
        service.ports.invalidate_run(run)
        await asyncio.sleep(0)
        assert not terminal.done()
        finish.set()
        await terminal
        assert delivered == ["guild"]
        assert app.arbiter.acquire_live.await_count == app.arbiter.release_live.await_count == 2
        assert service.source.take(app.clock.now()) is None
    asyncio.run(go())


def test_run_management_begins_before_lanes_produce_and_never_double_drains(app):
    async def go():
        service = ready(app)
        observed = asyncio.Event()
        async def online():
            assert service.source.managed
            service.ports.propose(101, "guild", "An ordinary guild proposal.")
            assert await service.source.propose(app.clock.now()) == []
            observed.set()
        service.online_tick = online
        task = asyncio.create_task(service.run())
        await observed.wait()
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert service.source.managed and not service._tasks
        assert await service.source.propose(app.clock.now()) == []
        assert not service.ports._speech
    asyncio.run(go())


@pytest.mark.parametrize("phase", ["before_ready", "during_countdown"])
def test_verified_early_pull_accounts_kill_and_restarts_readiness_after_terminal(raid_setup, phase):
    from leeroy_mind.guild.calls import CallGap, RaidCalls
    async def go():
        ports, _, night = raid_setup
        calls = RaidCalls(ports, gap=CallGap(ports.clock))
        night.add_listener(calls)
        await form(raid_setup)
        ports.members = {g: replace(m, map=631) for g, m in ports.members.items()}
        await inside(raid_setup)
        if phase == "during_countdown":
            await advance(ports, 30)
            await night.tick()
            await advance(ports, 15)
            await night.tick()
            assert "pulling in 5" in [row[2] for row in ports.said]
        engaged = Boss(int(ports.clock.now() * 1000), 631, 77, 0, "Testboss", "engaged", tuple(night.run.plan.members))
        await night.on_boss(engaged)
        await advance(ports, 5)
        await calls.tick()
        await night.tick()
        assert night.session is not None and night._assembly_pulls
        assert not controls(ports, "dc_on") and "go" not in [row[2] for row in ports.said]
        await advance(ports, 1)
        await night.on_boss(replace(engaged, ts=int(ports.clock.now() * 1000), state="killed"))
        await night.tick()
        assert night.session.kills == 1 and not night._assembly_pulls
        # A replayed engage must not resurrect this completed pull or delay DC.
        await night.on_boss(engaged)
        await night.tick()
        assert not night._assembly_pulls
        await advance(ports, 30)
        await night.tick()
        await advance(ports, 15)
        await night.tick()
        expected = 2 if phase == "during_countdown" else 1
        assert [row[2] for row in ports.said].count("pulling in 5") == expected
        await advance(ports, 5)
        await night.tick()
        assert night.state is State.RUNNING and len(controls(ports, "dc_on")) == 1
        assert ports.conn.execute("SELECT kills FROM guild_raid WHERE raid_id=?", (night.session.raid_id,)).fetchone()[0] == 1
    asyncio.run(go())


def test_early_pull_without_persisted_instance_or_with_stale_timestamp_is_ignored(raid_setup):
    async def go():
        ports, _, night = raid_setup
        await form(raid_setup)
        event = Boss(int(ports.clock.now() * 1000), 631, 77, 0, "Testboss", "engaged", tuple(night.run.plan.members))
        await night.on_boss(event)
        assert not night._events and not night._assembly_pulls
        await inside(raid_setup)
        await night.on_boss(replace(event, ts=event.ts - 61000))
        await night.on_boss(replace(event, instance_id=999))
        assert not night._events and not night._assembly_pulls
        ports.conn.execute("DELETE FROM meta WHERE key=?", (f"guild:raid:{night.session.raid_id}:instance",))
        await night.on_boss(event)
        assert not night._events and not night._assembly_pulls
    asyncio.run(go())


def test_early_pull_lost_terminal_is_bounded_and_replay_does_not_extend_deadline(raid_setup):
    async def go():
        ports, _, night = raid_setup
        night.cfg = replace(night.cfg, assemble_timeout_min=1)
        await form(raid_setup)
        await inside(raid_setup)
        event = Boss(int(ports.clock.now() * 1000), 631, 77, 0, "Testboss", "engaged", tuple(night.run.plan.members))
        await night.on_boss(event)
        deadline = night._assemble_due
        await night.tick()
        await advance(ports, 30)
        await night.on_boss(event)
        assert night._assemble_due == deadline
        await advance(ports, 31)
        await night.tick()
        assert night.state is State.ENDED and not controls(ports, "dc_on")
        assert ports.conn.execute("SELECT outcome FROM guild_raid ORDER BY raid_id DESC LIMIT 1").fetchone()[0] == "assemble_timeout"
    asyncio.run(go())


def test_engagement_during_countdown_delivery_cannot_recreate_go_timer(raid_setup):
    from leeroy_mind.guild.calls import CallGap, RaidCalls
    async def go():
        ports, _, night = raid_setup
        calls = RaidCalls(ports, gap=CallGap(ports.clock))
        night.add_listener(calls)
        await form(raid_setup)
        ports.members = {g: replace(m, map=631) for g, m in ports.members.items()}
        await inside(raid_setup)
        await advance(ports, 30)
        await night.tick()
        original = ports.say
        async def say(guid, channel, text):
            if text == "pulling in 5":
                await night.on_boss(Boss(int(ports.clock.now() * 1000), 631, 77, 0, "Testboss",
                                         "engaged", tuple(night.run.plan.members)))
            return await original(guid, channel, text)
        ports.say = say
        await advance(ports, 15)
        await night.tick()
        assert night._assembly_pulls and night._dc_due is None and calls._go_due is None
        await advance(ports, 5)
        await calls.tick()
        await night.tick()
        assert "go" not in [row[2] for row in ports.said] and not controls(ports, "dc_on")
    asyncio.run(go())
