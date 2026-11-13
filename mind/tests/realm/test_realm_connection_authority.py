"""Real Bridge/MindApp provenance under delayed hooks and buffered presence."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from conftest import run
from leeroy_mind.app import MindApp
from leeroy_mind.bridge import Backoff, BridgeClient
from leeroy_mind.config import load_config
from leeroy_mind.guild.protocol import BotDo, Job, JobSkip
from leeroy_mind.protocol import Hello, Member
from leeroy_mind.realm import protocol as p
from leeroy_mind.realm.action_policy import classify
from leeroy_mind.realm.action_store import ActionSpec
from leeroy_mind.realm.actions import ActionRejected
from dbtemplate import open_migrated
from leeroy_mind.storage.store import Store
from leeroy_mind.testing.fake_bridge import FakeBridge
from leeroy_mind.testing.fake_llm import FakeLLM


async def until(predicate):
    async def poll():
        while not predicate():
            await asyncio.sleep(0.005)
    await asyncio.wait_for(poll(), 3)


@asynccontextmanager
async def application(tmp_path, clock, *, confirmed=7, original_bridge=False):
    path = tmp_path / "mind.toml"
    path.write_text("[realm]\nenabled = true\n", encoding="utf-8")
    seed = Store(open_migrated(tmp_path / "mind.db"), clock=clock)
    seed.upsert_cast([Member(7, "Rook", 1, 1, 0, 80, 0, False, 0, 0, 2)], clock.now())
    if confirmed is not None:
        seed.set_meta("human_guid", str(confirmed))
    seed.close()
    server = FakeBridge(events=[dict(type="hello", server="test", version="1",
                                    guild_id=21, human_guid=0)], cast=[], known_types=set(p.COMMANDS))
    port = await server.start()
    cfg = load_config(path)
    cfg = replace(cfg, bridge=replace(cfg.bridge, host="127.0.0.1", port=port))
    app = MindApp(cfg, tmp_path, llm=FakeLLM(), clock=clock)
    # Ordinary identity/world bookkeeping remains real. Session/model work is
    # outside these transport/presence regressions and must not block them.
    app._start_session = AsyncMock()
    app.engine.on_event = AsyncMock()
    if original_bridge:
        client = app.bridge
        server._known_types.add("ping")
    else:
        app.bridge = client = BridgeClient("127.0.0.1", port, app.handle,
            on_connection=app._on_connection, backoff=Backoff(0.01, 0.01, 0), ping_interval=3600)
    hellos = []
    async def observed(ev):
        copied = Hello(ev.ts, ev.server, ev.version, ev.guild_id, ev.human_guid)
        assert client.delivery_epoch(copied) is None
        hellos.append((ev, client.delivery_epoch(ev), app.realm.action_context()))
    app.register_event_handler("hello", observed)
    task = asyncio.create_task(client.run())
    try:
        await until(lambda: bool(hellos))
        yield app, client, server, hellos
    finally:
        await asyncio.wait_for(app.stop(), 3)
        await asyncio.wait_for(task, 3)
        await server.stop()


def test_actual_writer_replacement_fences_realm_before_delayed_dispatch(tmp_path, clock):
    async def scenario():
        async with application(tmp_path, clock) as (app, client, server, hellos):
            realm = app.realm
            assert realm.action_context().presence_known
            entered, release = asyncio.Event(), asyncio.Event()
            async def slow_connection(up):
                if not up:
                    entered.set()
                    await release.wait()
            # This hook suspends ahead of Realm, like Training's actual drain.
            app._connection_hooks.insert(0, slow_connection)
            old = realm.action_context()
            realm.queue.submit(p.RealmMove("stale-hook", 501, "harbor"), owner="realm_day")
            try:
                await server.drop_client()
                await asyncio.wait_for(entered.wait(), 3)
                await until(lambda: server.connections == 2 and client.greeted)
                assert client.transport_state.epoch != realm._transport_epoch
                assert realm._connected  # The queued Realm down hook is still waiting.
                assert not realm.action_context().connected and not realm.action_context().presence_known
                realm.queue.tick()
                for _ in range(12):
                    await asyncio.sleep(0)
                assert realm.action_store.get("stale-hook").state == "not_sent"
                assert not any(c["id"] == "stale-hook" for c in server.commands)
                release.set()
                await until(lambda: realm.action_context().presence_known)
                assert realm.action_context().connection_epoch > old.connection_epoch
                assert hellos[-1][1] == client.transport_state.epoch
                realm.queue.submit(p.RealmMove("fresh-hook", 502, "harbor"), owner="realm_day")
                realm.queue.tick()
                await until(lambda: any(c.get("id") == "fresh-hook" for c in server.commands))
            finally:
                release.set()
    run(scenario())


def test_blocked_event_dispatcher_cannot_send_old_work_on_replacement_writer(tmp_path, clock):
    async def scenario():
        async with application(tmp_path, clock) as (app, client, server, hellos):
            realm = app.realm
            entered, release = asyncio.Event(), asyncio.Event()
            async def slow_event(ev):
                entered.set()
                await release.wait()
            app._handlers["guard"].insert(0, slow_event)
            old_epoch = client.transport_state.epoch
            realm.queue.submit(p.RealmMove("stale-event", 501, "harbor"), owner="realm_day")
            try:
                await server.push(dict(type="guard", guid=999, action="his_kick", rank=3, reason=""))
                await asyncio.wait_for(entered.wait(), 3)
                await server.drop_client()
                await until(lambda: server.connections == 2 and client.greeted)
                assert client.transport_state.epoch > old_epoch
                assert not realm.action_context().presence_known
                realm.queue.tick()
                for _ in range(12):
                    await asyncio.sleep(0)
                assert realm.action_store.get("stale-event").state == "not_sent"
                assert not any(c.get("id") == "stale-event" for c in server.commands)
                release.set()
                await until(lambda: realm.action_context().presence_known)
                assert hellos[-1][1] == client.transport_state.epoch
            finally:
                release.set()
    run(scenario())


def test_old_hello_cannot_adopt_replacement_transport_after_ordinary_handler_wait(tmp_path, clock):
    async def scenario():
        async with application(tmp_path, clock) as (app, client, server, hellos):
            entered, release = asyncio.Event(), asyncio.Event()
            old_epoch = client.transport_state.epoch
            blocked = []
            async def slow_hello(ev):
                if not blocked:
                    blocked.append(ev)
                    entered.set()
                    await release.wait()
            # The app's own hello row has completed, but an ordinary extension
            # still suspends before Realm can adopt that exact hello.
            app._handlers["hello"].insert(0, slow_hello)
            try:
                await server.push(dict(type="hello", server="test", version="1", guild_id=21, human_guid=0))
                await asyncio.wait_for(entered.wait(), 3)
                await server.drop_client()
                await until(lambda: server.connections == 2 and client.greeted)
                release.set()
                await until(lambda: len(hellos) == 3)
                assert hellos[1][0] is blocked[0] and hellos[1][1] == old_epoch
                assert not hellos[1][2].presence_known
                assert hellos[2][1] == client.transport_state.epoch and hellos[2][2].presence_known
            finally:
                release.set()
    run(scenario())


@pytest.mark.parametrize("confirmed", [None, 7])
def test_offline_hello_requires_confirmed_human_not_roster_master_guess(tmp_path, clock, confirmed):
    async def scenario():
        async with application(tmp_path, clock, confirmed=confirmed) as (app, client, server, hellos):
            assert app.human_guid == 7
            assert app.confirmed_human_guid == (confirmed or 0)
            assert app.realm.action_context().presence_known is (confirmed is not None)
            if confirmed is None:
                with pytest.raises(ActionRejected):
                    app.realm.queue.submit(p.Rearm("unproved-human", (501,)), owner="realm_day")
                app.realm.recall.restore_defaults((501,))
                assert app.realm.action_store.records() == () and not server.commands
                await server.push(dict(type="hello", server="test", version="1", guild_id=21, human_guid=7))
                await until(lambda: len(hellos) == 2)
                assert app.confirmed_human_guid == 7 and app.realm.action_context().presence_known
                assert app.store.get_meta("human_guid") == "7"
            app.realm.queue.submit(p.Rearm("proved-human", (501,)), owner="realm_day")
            assert app.realm.action_store.get("proved-human").state == "prepared"
    run(scenario())


def test_injected_or_copied_hello_has_no_native_transport_provenance(tmp_path, clock):
    async def scenario():
        async with application(tmp_path, clock) as (app, client, server, hellos):
            first = hellos[0][0]
            assert client.delivery_epoch(first) is None
            injected = Hello(first.ts, "test", "1", 21, 0)
            client.inject(injected)
            await until(lambda: len(hellos) == 2)
            assert hellos[-1][0] is injected and hellos[-1][1] is None
            assert not app.realm.action_context().presence_known
            with pytest.raises(ActionRejected):
                app.realm.queue.submit(p.Rearm("injected", (501,)), owner="realm_day")
            await server.push(dict(type="hello", server="test", version="1", guild_id=21, human_guid=0))
            await until(lambda: len(hellos) == 3)
            assert hellos[-1][1] == client.transport_state.epoch and app.realm.action_context().presence_known
    run(scenario())


def test_replayed_login_preserves_current_claims_actions_and_recall_timers(tmp_path, clock):
    async def scenario():
        async with application(tmp_path, clock) as (app, client, server, hellos):
            realm = app.realm
            assert realm.action_context().presence_known and not app.world.human_online
            assert realm.bodies.claim("realm_day", {501}, rank=4, until=clock.now() + 60) == {501}
            assert realm.bodies.claim("realm_event", {502}, rank=3, until=clock.now() + 60) == {502}
            realm.queue.submit(p.RealmMove("current-day", 501, "harbor"), owner="realm_day")
            realm.queue.submit(p.RealmMove("current-event", 502, "harbor"), owner="realm_event")
            cleanup = realm.queue.request_cleanup = Mock(wraps=realm.queue.request_cleanup)
            initial = (realm.recall._last_recall, realm.recall._second_at, realm._generation)
            logins = []
            async def observed(ev):
                logins.append(ev)
            app.register_event_handler("login", observed)
            old_ts = hellos[0][0].ts - 6000
            (tmp_path / "mind.toml").write_text("[realm]\nenabled = false\n", encoding="utf-8")
            await server.push(dict(type="login", ts=old_ts, guid=7, human=True))
            await until(lambda: len(logins) == 1)
            assert app.is_backlog(logins[0]) and realm.enabled
            assert (realm.recall._last_recall, realm.recall._second_at, realm._generation) == initial
            assert realm.bodies.claims_for("realm_day") == frozenset({501})
            assert realm.bodies.claims_for("realm_event") == frozenset({502})
            assert all(realm.action_store.get(i).state == "prepared" for i in ("current-day", "current-event"))
            cleanup.assert_not_called()
            # Historical private kick bookkeeping remains active.
            await realm.on_event(p.Guard(old_ts, 999, "his_kick", 3, ""))
            assert app.store.conn.execute("SELECT guid FROM realm_kick WHERE guid=999").fetchone()[0] == 999
            (tmp_path / "mind.toml").write_text("[realm]\nenabled = true\n", encoding="utf-8")
            await server.push(dict(type="login", ts=hellos[0][0].ts, guid=7, human=True))
            await until(lambda: len(logins) == 2)
            assert not app.is_backlog(logins[1])
            assert realm.recall._last_recall == clock.now() and realm.recall._second_at == clock.now() + 45
            assert realm.bodies.claims_for("realm_day") == realm.bodies.claims_for("realm_event") == frozenset()
            assert all(realm.action_store.get(i).state == "not_sent" for i in ("current-day", "current-event"))
    run(scenario())


@pytest.mark.parametrize("route", ["enabled", "disabled", "shutdown"])
def test_shared_guild_and_training_jobs_are_unmatched_through_real_app_routes(tmp_path, clock, caplog, route):
    async def scenario():
        async with application(tmp_path, clock) as (app, client, server, hellos):
            realm = app.realm
            context = realm.action_context()
            for cmd in (BotDo("realm-defaults", (501, 502), "nc", "-bg,-lfg"),
                        p.RealmMove("realm-move", 503, "harbor")):
                c = classify(cmd)
                realm.action_store.prepare(ActionSpec(cmd.id, None, "root", c.policy, "realm_day", 4,
                    c.kind, c.payload, c.reserved_manifest, context.guild, context.human), now=clock.now())
                realm.action_store.begin_dispatch(cmd.id, payload=c.payload, process_id=context.process_id,
                    connection_epoch=context.connection_epoch, now=clock.now())
            part = SimpleNamespace(on_event=AsyncMock())
            realm.add_part(part, event_types={"job"})
            ordinary_seen, shutdown_seen = [], []
            async def downstream(ev):
                ordinary_seen.append(ev)
            app.register_event_handler("job", downstream)
            app.register_shutdown_receipt("job", shutdown_seen.append)
            entered, release = asyncio.Event(), asyncio.Event()
            closing = None
            if route == "disabled":
                realm._set_enabled(False)
            elif route == "shutdown":
                async def pause_shutdown_before_database_close():
                    entered.set()
                    await release.wait()
                app.register_shutdown_drain(pause_shutdown_before_database_close)
                closing = asyncio.create_task(app.stop())
            try:
                if closing is not None:
                    await asyncio.wait_for(entered.wait(), 3)
                    assert app._stopping and not app._closed
                records, holds = realm.action_store.records(), realm.action_store.holds()
                diagnostics = tuple(tuple(row) for row in app.store.conn.execute("SELECT * FROM realm_receipt ORDER BY seq"))
                command_count = len(server.commands)
                ts = int(clock.now()*1000)
                def event(command_id, kind, state, scope="", *, guid=0):
                    # GearUp publishes singleton progress; offline raid publishes
                    # stage progress; BotDo echoes the native Training handle.
                    progress = state == "progress"
                    skipped = (JobSkip(501, "cancelled"), JobSkip(502, "cancelled")) if kind == "bot_do" and state == "failed" else ()
                    guids = () if progress and kind == "gear_up" or skipped else (501, 502)
                    return Job(ts, command_id, kind, state, "cancelled" if state == "failed" else "",
                               "assembling" if kind == "offline_raid" and progress else "", guids, skipped,
                               0, 0, 0, guid, 80 if guid else 0, 200 if guid else 0, scope)
                events = [
                    event("gear-progress", "gear_up", "progress", guid=501),
                    event("gear-terminal", "gear_up", "done"),
                    event("raid-progress", "offline_raid", "progress", "training-scope"),
                    event("raid-terminal", "offline_raid", "failed", "training-scope"),
                    event("benchmark-progress", "benchmark", "progress", "benchmark-scope"),
                    event("benchmark-terminal", "benchmark", "done", "benchmark-scope"),
                    event("training-defaults", "bot_do", "done", "training-scope"),
                    event("realm-defaults", "gear_up", "progress", guid=501),
                    event("realm-defaults", "gear_up", "done"),
                    event("realm-defaults", "bot_do", "done", "training-scope"),
                    event("realm-defaults", "bot_do", "failed", "training-scope"),
                    event("realm-move", "bot_do", "done"),
                ]
                for ev in events:
                    await app.handle(ev)
                    assert realm.action_store.records() == records, (route, ev.id, ev.kind, ev.state)
                    assert realm.action_store.holds() == holds
                    assert tuple(tuple(row) for row in app.store.conn.execute("SELECT * FROM realm_receipt ORDER BY seq")) == diagnostics
                assert len(server.commands) == command_count
                assert [call.args[0] for call in part.on_event.await_args_list] == (events if route == "enabled" else [])
                assert ordinary_seen == ([] if route == "shutdown" else events)
                assert shutdown_seen == (events if route == "shutdown" else [])
                assert any(hold.scope_id == "realm-defaults" and hold.guids == frozenset({501, 502}) for hold in holds)
                assert "event_handler_failed" not in caplog.text
                assert "shutdown_receipt_failed" not in caplog.text
                assert "event_failed" not in caplog.text
            finally:
                release.set()
                if closing is not None:
                    await asyncio.wait_for(closing, 3)
    run(scenario())
