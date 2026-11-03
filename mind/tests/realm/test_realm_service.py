import asyncio
from types import SimpleNamespace

import pytest

from conftest import run
from leeroy_mind.protocol import Ack, Hello, Login
from leeroy_mind.realm import protocol, store as realm_store
from leeroy_mind.realm.ports import RealmDisabled
from leeroy_mind.realm.service import JOB_COMMANDS, attach_realm
from realm.fakes import RealmApp, enabled



def enroll(realm, cmd):
    """Enter the real synchronous queue barrier without starting its worker."""
    async def establish():
        if not realm.action_context().presence_known:
            await realm.app.connection(True)
            await realm.app.event(Hello(0, "test", "1", 21, realm.app.human_guid))
    run(establish())
    realm.queue.submit(cmd, owner="realm_day")
    realm.on_send(cmd)


@pytest.fixture
def app(tmp_path, store, clock):
    return RealmApp(tmp_path, store, clock)


def test_disabled_attach_registers_only_without_runtime_work(app, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("attachment started asynchronous work")
    monkeypatch.setattr(asyncio, "create_task", forbidden)
    realm = attach_realm(app)
    assert realm.enabled is False
    assert app.background == [realm.run]
    assert app.drains == [realm.close]
    assert set(protocol.EVENTS) <= app.handlers.keys()
    assert all(len(handlers) == 1 for handlers in app.handlers.values())
    assert app.skip_event_rows >= (protocol.NOISY - {"channel_chat"}) | {"training_test"}
    assert "channel_chat" not in app.skip_event_rows
    assert app.handlers["bg_end"] == [realm.on_event]
    assert app.bodies is realm.bodies and not hasattr(app, "night_runner")
    assert app.sent == []
    app.engine.speak.assert_not_called()
    app.engine.say_as.assert_not_called()


def test_disabled_hour_and_events_do_no_game_or_model_work(app):
    realm = attach_realm(app)
    async def go():
        await app.event(Hello(0, "test", "1", 21, 1))
        background = asyncio.create_task(realm.run())
        await asyncio.sleep(0)
        await app.clock.advance(3600, step=3600)
        realm.ports.propose(101, "guild", "hello")
        assert await realm.ports.say(101, "guild", "hello") is False
        with pytest.raises(RealmDisabled):
            await realm.ports.send(protocol.RealmMove("disabled", 101, "harbor"))
        background.cancel()
        with pytest.raises(asyncio.CancelledError):
            await background
    run(go())
    assert not app.sent
    app.engine.speak.assert_not_called()
    app.engine.say_as.assert_not_called()


@pytest.mark.parametrize("presence", [Hello(0, "test", "1", 21, 1), Login(0, 1, True)])
def test_reload_enables_real_part_at_presence_without_synthetic_send(app, presence):
    realm = attach_realm(app)
    ticks, events = [], []
    async def tick():
        ticks.append("tick")
    async def on_event(ev):
        events.append(ev)
    realm.add_part(SimpleNamespace(tick=tick, on_event=on_event), event_types=("hello", "login"))
    enabled(app.data_dir, True)
    async def go():
        await app.event(presence)
        await realm.tick()
        assert realm.enabled and ticks == ["tick"] and events == [presence]
        assert app.sent == []
        realm.ports.propose(101, "guild", "hello")
        await realm.tick()
        app.engine.speak.assert_awaited_once()
        assert app.engine.speak.call_args.kwargs["still_valid"]()
        await realm.close()
    run(go())


def test_foreign_login_does_not_reload(app):
    realm = attach_realm(app)
    enabled(app.data_dir, True)
    run(app.event(Login(0, 103, False)))
    assert realm.enabled is False


def test_presence_false_cancels_parts_before_other_app_await_and_old_tick_stays_dead(app):
    enabled(app.data_dir, True)
    realm = attach_realm(app)
    started, cancelled, fresh = [], [], []
    async def first():
        started.append(True)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(True)
            raise
    async def second():
        fresh.append(True)
    realm.add_part(SimpleNamespace(tick=first))
    realm.add_part(SimpleNamespace(tick=second))
    async def go():
        running = asyncio.create_task(realm.tick())
        for _ in range(100):
            if started:
                break
            await asyncio.sleep(0)
        assert started
        enabled(app.data_dir, False)
        await realm.on_presence(Hello(0, "test", "1", 21, 1))
        assert not realm.enabled
        # Reenable before cancelled work resumes: its old tick must stay dead.
        enabled(app.data_dir, True)
        realm.reload()
        await running
        assert cancelled == [True] and fresh == []
        await realm.close()
    run(go())


def test_bad_reload_fails_closed_without_private_error(app, caplog):
    enabled(app.data_dir, True)
    realm = attach_realm(app)
    (app.data_dir / "mind.toml").write_text('[realm]\nenabled = "private-value"\n')
    realm.reload()
    assert not realm.enabled and "private-value" not in caplog.text


def test_hello_identity_change_cancels_old_parts_even_when_switch_stays_on(app):
    enabled(app.data_dir, True)
    realm = attach_realm(app)
    async def go():
        started, cancelled = asyncio.Event(), []
        async def tick():
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.append(True)
                raise
        realm.add_part(SimpleNamespace(tick=tick))
        running = asyncio.create_task(realm.tick())
        await started.wait()
        await realm.on_presence(Hello(0, "test", "1", 21, 2))
        app.human_guid = 2  # The app's ordinary hello row adopts it next.
        await running
        assert realm.enabled and cancelled == [True]
        await realm.close()
    run(go())


def test_his_kick_disabled_preserves_owner_answer(app):
    realm = attach_realm(app)
    async def go():
        await app.event(protocol.Guard(1000, 103, "his_kick", 4, ""))
        with app.store.conn:
            app.store.conn.execute("UPDATE realm_kick SET asked=2,answer='no',answered=3 WHERE guid=103")
        await app.event(protocol.Guard(4000, 103, "his_kick", 5, ""))
    run(go())
    row = app.store.conn.execute("SELECT ts,rank,asked,answer,answered FROM realm_kick").fetchone()
    assert tuple(row) == (4, 5, 2, "no", 3)
    assert not app.sent and not realm.enabled


@pytest.mark.parametrize("job_first", [False, True])
def test_already_sent_job_and_ack_settle_once_while_disabled(app, job_first):
    enabled(app.data_dir, True)
    realm = attach_realm(app)
    cmd = protocol.RealmMove("moving", 101, "harbor")
    enroll(realm, cmd)
    enabled(app.data_dir, False)
    realm.reload()
    job = protocol.RealmJob(1000, cmd.id, "realm_move", "done", "", 101, 9)
    ack = Ack(0, cmd.id, True, "")
    actions = [lambda: realm.on_job(job), lambda: realm.on_ack(cmd, ack)]
    if not job_first:
        actions.reverse()
    for action in actions * 2:
        action()
    rows = realm_store.recent_receipts(app.store.conn)
    assert {(r.stage, r.value) for r in rows} == {("ack", 0), ("job", 9)}
    assert not realm._pending and realm.action_store.get(cmd.id).state == "completed"
    assert not app.sent


def test_only_exact_sent_identity_kind_and_target_are_recorded(app):
    enabled(app.data_dir, True)
    realm = attach_realm(app)
    cmd = protocol.ItemCount("count", 101, (5976,))
    realm.on_send(cmd)
    realm.on_ack(protocol.ItemCount("count", 101, (5976,)), Ack(0, "count", True, ""))
    realm.on_ack(cmd, Ack(0, "other", True, ""))
    for identity, kind, guid, owner in [("other", "item_count", 101, ""),
                                       ("count", "rearm", 101, ""),
                                       ("count", "item_count", 102, ""),
                                       ("count", "item_count", 101, "a" * 32 + "-" + "b" * 16)]:
        realm.on_job(protocol.RealmJob(0, identity, kind, "done", "", guid, 0, owner))
    assert realm_store.recent_receipts(app.store.conn) == []
    realm.on_ack(cmd, Ack(0, "count", True, ""))
    realm.on_job(protocol.RealmJob(0, "count", "item_count", "done", "", 101, 1))
    assert len(realm_store.recent_receipts(app.store.conn)) == 2

def test_ack_only_commands_do_not_leak_pending_capacity(app):
    enabled(app.data_dir, True)
    realm = attach_realm(app)
    for i in range(realm.CAPACITY * 3):
        cmd = protocol.BankMoney(f"money_{i}", 1)
        realm.on_send(cmd)
        realm.on_ack(cmd, Ack(0, cmd.id, True, ""))
    assert not realm._pending and len(realm._completed) == realm.COMPLETED
    assert len(realm_store.recent_receipts(app.store.conn, limit=256)) == 256


def test_negative_calendar_ack_keeps_bounded_identity_for_later_job(app):
    enabled(app.data_dir, True)
    realm = attach_realm(app)
    cmd = protocol.CalendarPost("calendar", 101, "Meeting", "meeting", 1234567890)
    enroll(realm, cmd)
    realm.on_ack(cmd, Ack(0, "calendar", False, "calendar_outcome_unknown"))
    assert not realm._pending
    enabled(app.data_dir, False)
    realm.reload()
    job = protocol.RealmJob(0, "calendar", "calendar_post", "failed", "calendar_outcome_unknown", 101, 777)
    assert realm.on_job(job) is True
    assert realm.on_job(job) is False
    assert {(r.stage, r.value) for r in realm_store.recent_receipts(app.store.conn)} == {("ack", 0), ("job", 777)}


def test_native_arena_team_create_requires_job_and_keeps_value(app):
    enabled(app.data_dir, True)
    realm = attach_realm(app)
    cmd = protocol.ArenaTeam("team", "create", captain_guid=101, arena_type=2, name="Test Team")
    enroll(realm, cmd)
    realm.on_ack(cmd, Ack(0, "team", True, ""))
    assert realm.action_store.get("team").state != "completed"
    realm.on_job(protocol.RealmJob(0, "team", "arena_team", "done", "", 101, 777))
    assert realm.action_store.get("team").state == "completed"
    assert realm_store.recent_receipts(app.store.conn)[0].value == 777
    assert "arena_team" in JOB_COMMANDS


@pytest.mark.parametrize("action", ["add", "drop"])
def test_arena_roster_mutation_requires_future_full_roster_admission(app, action):
    enabled(app.data_dir, True)
    realm = attach_realm(app)
    cmd = protocol.ArenaTeam("team", action, team_id=7, guid=102)
    with pytest.raises(ValueError):
        enroll(realm, cmd)
    assert not app.sent

def test_pending_capacity_denies_before_dispatch_and_uncertain_errors_stay(app):
    enabled(app.data_dir, True)
    realm = attach_realm(app)
    for i in range(realm.CAPACITY):
        realm.on_send(protocol.ItemCount(f"move_{i}", 101, (5976,)))
    cmd = protocol.ItemCount("overflow", 101, (5976,))
    with pytest.raises(RuntimeError, match="capacity"):
        realm.on_send(cmd)
    first = realm._pending["move_0"].command
    realm.on_send_error(first, "ConnectionLost")
    assert len(realm._pending) == realm.CAPACITY
    realm.on_send_error(first, "NotConnected")
    assert len(realm._pending) == realm.CAPACITY - 1


def test_real_ports_transport_started_before_disable_settles_through_close(app):
    enabled(app.data_dir, True)
    realm = attach_realm(app)
    async def go():
        started, release = asyncio.Event(), asyncio.Event()
        async def send(cmd, *, preserve_id):
            app.sent.append(cmd)
            started.set()
            await release.wait()
            return Ack(0, cmd.id, True, "")
        app.bridge.send = send
        cmd = protocol.BankMoney("issued", 1)
        sending = asyncio.create_task(realm.ports.send(cmd))
        await started.wait()
        enabled(app.data_dir, False)
        realm.reload()
        closing = asyncio.create_task(realm.close())
        await asyncio.sleep(0)
        assert not closing.done()
        release.set()
        await sending
        await closing
        assert realm_store.recent_receipts(app.store.conn)[0].stage == "ack"
        assert len(app.sent) == 1
    run(go())
