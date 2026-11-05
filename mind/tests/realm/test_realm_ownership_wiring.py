"""Lifecycle/transport integration with the real ownership components."""

import asyncio
from contextlib import asynccontextmanager
import json
import sqlite3
from types import SimpleNamespace

import pytest

from conftest import run
from leeroy_mind.guild.protocol import BotDo, Job
from leeroy_mind.protocol import Ack, Hello
from leeroy_mind.realm import protocol as p, store as observations
from leeroy_mind.realm.action_policy import classify
from leeroy_mind.realm.action_store import ActionSpec, ActionStore
from leeroy_mind.realm.actions import ActionRejected
from leeroy_mind.realm.service import attach_realm
from realm.fakes import RealmApp, enabled


@asynccontextmanager
async def attached_realm(app):
    realm = attach_realm(app)
    try:
        yield realm
    finally:
        await realm.close()


async def cycle(realm, seconds=0):
    if seconds:
        await realm.app.clock.advance(seconds, step=seconds)
    realm.queue.tick()
    for _ in range(12):
        await asyncio.sleep(0)


async def ready(app, realm, *, human=0):
    await app.connection(True)
    await app.event(Hello(0, "test", "1", 21, human))
    assert realm.action_context().presence_known


def prepare(store, cmd, *, dispatched=False):
    c = classify(cmd)
    store.prepare(ActionSpec(cmd.id, None, "root", c.policy, "realm_day", 4,
                             c.kind, c.payload, c.reserved_manifest, 21, 1), now=1)
    if dispatched:
        store.begin_dispatch(cmd.id, payload=c.payload, process_id="previous-process",
                             connection_epoch=1, now=2)


@pytest.fixture
def app(tmp_path, store, clock):
    enabled(tmp_path, True)
    return RealmApp(tmp_path, store, clock)


def test_recovery_is_once_before_ledger_attachment_and_never_replays(app, monkeypatch):
    actions = ActionStore(app.store.conn)
    prepare(actions, p.RealmMove("prepared", 101, "harbor"))
    prepare(actions, p.RealmMove("entered", 102, "harbor"), dispatched=True)
    original = ActionStore.recover
    calls = []
    def recover(self, **kwargs):
        assert not hasattr(app, "bodies")
        calls.append(kwargs)
        return original(self, **kwargs)
    monkeypatch.setattr(ActionStore, "recover", recover)
    async def scenario():
        async with attached_realm(app) as realm:
            assert len(calls) == 1 and calls[0]["process_id"] == realm.action_context().process_id
            assert realm.action_store.get("prepared").state == "not_sent"
            recovered = realm.action_store.get("entered")
            assert recovered.state == "unknown" and recovered.transport_state == "settled"
            assert app.bodies is realm.bodies and not realm.bodies.may_move("raid", 102)
            assert app.store.conn.execute("PRAGMA user_version").fetchone()[0] >= 10
            assert app.sent == [] and app.background == [realm.run]
            app.engine.speak.assert_not_called()
    run(scenario())


def test_training_absent_pending_durable_journal_denies_new_work(app):
    async def scenario():
        with app.store.conn:
            app.store.conn.execute("INSERT INTO tg_applied(ts,run,layer,context,command,inverse) "
                                   "VALUES (1,'unowned','boss','ctx','{}','{}')")
        async with attached_realm(app) as realm:
            await ready(app, realm)
            assert app.training is None and not realm.bodies.may_move("realm_day", 101)
            with pytest.raises(ActionRejected):
                realm.queue.submit(p.RealmMove("denied", 101, "harbor"), owner="realm_day")
            assert realm.action_store.records() == () and app.sent == []
            with app.store.conn:
                app.store.conn.execute("UPDATE tg_applied SET undone=2")
            assert realm.bodies.may_move("realm_day", 101)
    run(scenario())


@pytest.mark.parametrize("command", [p.RealmMove("bypass", 101, "harbor"),
    p.Rearm("bypass", (101,)), p.LeavePvp("bypass", (101,)),
    p.LeavePvp("bypass", (101,), "a" * 32 + "-" + "b" * 16),
    BotDo("bypass", (101,), "nc", "-bg,-lfg"), p.GetRealmPvp("bypass", origin_id="unowned")])
def test_unenrolled_body_and_recovery_commands_cannot_cross_live_ports(app, command):
    async def scenario():
        async with attached_realm(app) as realm:
            await ready(app, realm)
            with pytest.raises(ValueError):
                await realm.ports.send(command)
            assert app.sent == [] and realm.action_store.records() == ()
    run(scenario())


def test_pretransport_commit_failure_blocks_real_bridge_and_redacts_error(app, monkeypatch, caplog):
    async def scenario():
        async with attached_realm(app) as realm:
            await ready(app, realm)
            realm.queue.submit(p.RealmMove("blocked-commit", 101, "harbor"), owner="realm_day")
            def fail(*args, **kwargs):
                raise sqlite3.OperationalError("private-payload-must-not-appear")
            monkeypatch.setattr(realm.action_store, "begin_dispatch", fail)
            await cycle(realm)
            assert app.sent == [] and realm.action_store.get("blocked-commit").state == "not_sent"
            assert "private-payload-must-not-appear" not in caplog.text
    run(scenario())


def test_real_connection_and_hello_boundaries_preserve_zero_human_identity(app):
    async def scenario():
        async with attached_realm(app) as realm:
            first = realm.action_context()
            assert not first.connected and not first.presence_known and first.connection_epoch == 0
            with pytest.raises(ActionRejected):
                realm.queue.submit(p.Rearm("startup", (101,)), owner="realm_day")
            await app.connection(True)
            assert realm.action_context().connection_epoch == 1
            with pytest.raises(ActionRejected):
                realm.queue.submit(p.Rearm("before-hello", (101,)), owner="realm_day")
            await app.event(Hello(0, "test", "1", 21, 0))
            c = realm.action_context()
            assert (c.guild, c.human, c.connected, c.presence_known) == (21, 1, True, True)
            realm.queue.submit(p.Rearm("stale", (101,)), owner="realm_day")
            await realm.on_presence(Hello(0, "test", "1", 21, 0))
            assert not realm.action_context().presence_known
            assert realm.action_store.get("stale").state == "not_sent"
            await realm.on_event(Hello(0, "test", "1", 21, 0))
            realm.queue.off = True
            await app.event(Hello(0, "test", "1", 21, 0))
            assert realm.action_context().human == 1 and not realm.queue.off
            await app.connection(False)
            assert not realm.action_context().presence_known
            await app.connection(True)
            assert realm.action_context().connection_epoch == 2 and not realm.action_context().presence_known
    run(scenario())


@pytest.mark.parametrize("guild,human", [(0, 1), (True, 1), (21, True), (21, -1)])
def test_invalid_hello_never_creates_authority(app, guild, human):
    async def scenario():
        async with attached_realm(app) as realm:
            await app.connection(True)
            # Deliberately bypass the fake ordinary identity adoption to supply bad wire
            # data without changing the app's configured stable identity.
            await realm.on_presence(Hello(0, "test", "1", guild, human))
            await realm.on_event(Hello(0, "test", "1", guild, human))
            assert not realm.action_context().presence_known
            with pytest.raises(ActionRejected):
                realm.queue.submit(p.Rearm("invalid", (101,)), owner="realm_day")
    run(scenario())


def test_second_wg_job_reaches_authority_while_disabled_before_diagnostic_dedup(app):
    async def scenario():
        async with attached_realm(app) as realm:
            await ready(app, realm)
            cmd = p.WgJoin("wg-progress", (101, 102))
            realm.queue.submit(cmd, owner="realm_day")
            await cycle(realm)
            token = "a" * 32 + "-" + "b" * 16
            await app.event(p.RealmJob(0, cmd.id, "wg_join", "done", "", 0, 2, token))
            realm._set_enabled(False)
            await app.event(p.RealmJob(1, cmd.id, "wg_join", "failed", "ending", 0, 0, token))
            data = json.loads(realm.action_store.get(cmd.id).evidence)
            assert data["enrollment_job"]["state"] == "done"
            assert data["origin_failure"]["state"] == "failed"
            assert realm.action_store.holds()[0].guids == frozenset({101, 102})
            rows = [r for r in observations.recent_receipts(app.store.conn) if r.stage == "job"]
            assert len(rows) == 1 and rows[0].ok
    run(scenario())


def test_full_direct_diagnostics_capacity_does_not_block_query_or_cleanup(app):
    async def scenario():
        async with attached_realm(app) as realm:
            await ready(app, realm)
            for i in range(realm.CAPACITY):
                realm.on_send(p.ItemCount(f"direct-{i}", 500 + i, (5976,)))
            origin = p.WgJoin("cleanup-origin", (101, 102))
            realm.queue.submit(origin, owner="realm_day")
            await cycle(realm)
            token = "a" * 32 + "-" + "b" * 16
            await app.event(p.RealmJob(0, origin.id, "wg_join", "done", "", 0, 2, token))
            await cycle(realm)
            query = next(c for c in app.sent if type(c) is p.GetRealmPvp)
            await app.event(p.RealmPvp(0, query.id, token, origin.id, "wintergrasp", 77, 1,
                                     21, 1, (101, 102), "active", False, "", ""))
            realm.queue.request_cleanup(origin.id)
            await cycle(realm, 1.01)
            cleanup = next(c for c in app.sent if type(c) is p.LeavePvp)
            assert cleanup.guids == (101, 102) and cleanup.pvp_owner == token
            assert len(realm._pending) == realm.CAPACITY
            assert not any(c.id in realm._pending for c in app.sent)
    run(scenario())


def test_canceled_caller_keeps_actual_settlement_and_close_waits(app):
    async def scenario():
        async with attached_realm(app) as realm:
            await ready(app, realm)
            started, release = asyncio.Event(), asyncio.Event()
            async def transport(cmd, *, preserve_id):
                app.sent.append(cmd)
                started.set()
                await release.wait()
                return Ack(0, cmd.id, True, "")
            app.bridge.send = transport
            try:
                cmd = p.RealmMove("inflight", 101, "harbor")
                callbacks = []
                realm.queue.submit(cmd, owner="realm_day", on_done=lambda *args: callbacks.append(args))
                sending = asyncio.create_task(realm.ports.send(cmd))
                await started.wait()
                sending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await sending
                assert realm.action_store.get(cmd.id).transport_state == "inflight"
                closing = asyncio.create_task(realm.close())
                for _ in range(6):
                    await asyncio.sleep(0)
                assert not closing.done()
                for sink in app.receipts["realm_job"]:
                    sink(p.RealmJob(0, cmd.id, "realm_move", "done", "", 101, 0))
                assert realm.action_store.holds()
                release.set()
                await closing
                assert realm.action_store.get(cmd.id).state == "completed"
                assert callbacks == [] and realm.action_store.holds() == ()
                assert {r.stage for r in observations.recent_receipts(app.store.conn)} == {"ack", "job"}
            finally:
                release.set()
    run(scenario())


def test_all_three_shutdown_receipt_families_still_commit_after_close(app):
    async def scenario():
        async with attached_realm(app) as realm:
            await ready(app, realm)
            move = p.RealmMove("shutdown-move", 101, "harbor")
            defaults = BotDo("shutdown-defaults", (102,), "nc", "-bg,-lfg")
            origin = p.WgJoin("shutdown-wg", (103,))
            for cmd in (move, defaults, origin):
                realm.queue.submit(cmd, owner="realm_day")
            await cycle(realm)
            await cycle(realm, 1.01)
            await cycle(realm)
            query = next(c for c in app.sent if type(c) is p.GetRealmPvp)
            await realm.close()
            token = "a" * 32 + "-" + "b" * 16
            events = {"realm_job": p.RealmJob(0, move.id, "realm_move", "done", "", 101, 0),
                      "job": Job(0, defaults.id, "bot_do", "done", "", "", (102,), (), 0, 0, 0, 0, 0, 0),
                      "realm_pvp": p.RealmPvp(0, query.id, token, origin.id, "wintergrasp", 77, 1,
                                              21, 1, (103,), "active", False, "", "")}
            for name, ev in events.items():
                for sink in app.receipts[name]:
                    sink(ev)
            assert realm.action_store.get(move.id).state == "completed"
            assert realm.action_store.get(defaults.id).state == "completed"
            assert json.loads(realm.action_store.get(origin.id).evidence)["status"]["stage"] == "active"
            assert not realm.enabled and len(app.sent) == 4
    run(scenario())


@pytest.mark.parametrize("boundary", ["leave", "rearm", "defaults"])
@pytest.mark.parametrize("change", ["disable", "human", "connection"])
def test_generation_invalidates_each_recall_chain_boundary(app, boundary, change):
    async def scenario():
        async with attached_realm(app) as realm:
            await ready(app, realm)
            realm.ports.cast = lambda: [SimpleNamespace(guid=101, online=True)]
            realm.recall.on_login(app.clock.now())
            await cycle(realm)
            if boundary != "leave":
                await app.event(p.RealmJob(0, app.sent[-1].id, "leave_pvp", "done", "", 101, 0))
                await cycle(realm)
            if boundary == "defaults":
                await app.event(p.RealmJob(0, app.sent[-1].id, "rearm", "done", "", 101, 0))
                await cycle(realm, 1.01)
            old = app.sent[-1]
            before = len(app.sent)
            if change == "disable":
                realm._set_enabled(False)
                realm._set_enabled(True)
            elif change == "human":
                await app.event(Hello(1, "test", "1", 21, 2))
            else:
                await app.connection(False)
                await app.connection(True)
            assert not realm.recall._chains
            if change != "human":
                assert not realm.recall._desired
            if type(old) is BotDo:
                await app.event(Job(0, old.id, "bot_do", "done", "", "", (101,), (), 0, 0, 0, 0, 0, 0))
            else:
                kind = "leave_pvp" if type(old) is p.LeavePvp else "rearm"
                await app.event(p.RealmJob(0, old.id, kind, "done", "", 101, 0))
            await cycle(realm, 2)
            assert len(app.sent) == before and not realm.recall._chains
    run(scenario())


def test_login_drops_unsent_realm_work_and_claims_but_keeps_dispatched_holds(app):
    async def scenario():
        async with attached_realm(app) as realm:
            await ready(app, realm)
            assert realm.bodies.claim("realm_day", {101}, rank=4, until=app.clock.now() + 60) == {101}
            assert realm.bodies.claim("realm_event", {103}, rank=3, until=app.clock.now() + 60) == {103}
            realm.queue.submit(p.RealmMove("unsent-day", 101, "harbor"), owner="realm_day")
            realm.queue.submit(p.RealmMove("unsent-event", 103, "harbor"), owner="realm_event")
            entered = p.RealmMove("entered-day", 102, "harbor")
            realm.queue.submit(entered, owner="realm_day")
            realm.on_send(entered)
            realm.recall.on_login(app.clock.now())
            assert realm.action_store.get("unsent-day").state == "not_sent"
            assert realm.action_store.get("unsent-event").state == "not_sent"
            assert realm.bodies.claims_for("realm_day") == realm.bodies.claims_for("realm_event") == frozenset()
            assert realm.action_store.holds()[0].guids == frozenset({102})
            assert not realm.bodies.may_move("raid", 102)
    run(scenario())


def test_disabled_service_still_polls_retained_provider_handback(app):
    async def scenario():
        from leeroy_mind.bodies import BodyHold
        async with attached_realm(app) as realm:
            realm._set_enabled(False)
            rows = [BodyHold("retained-training", "training", 2, {101}, True)]
            realm.bodies.add_holder_source("changing-provider", lambda: tuple(rows))
            released = []
            realm.bodies.on_release(lambda owner, guids: released.append((owner, guids)))
            assert realm.bodies.holder(101) == "training"
            rows.clear()
            await realm.tick()
            assert released == [("training", {101})]
            assert app.bodies is realm.bodies and app.sent == []
    run(scenario())
