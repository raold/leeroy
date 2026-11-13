import asyncio
import json
from types import SimpleNamespace

import pytest

from leeroy_mind.protocol import Ack, parse_event, ProtocolError
from leeroy_mind.realm import protocol as p
from leeroy_mind.realm.action_store import held_manifest, skipped_backoff
from leeroy_mind.realm.forget import ForgetReceipts, FORGOTTEN_RECEIPT
from realm.test_realm_action_store import store, prepare, dispatch


def failed(store, *, kind="leave_pvp", members=(101, 102, 103), **fields):
    cls = p.LeavePvp if kind == "leave_pvp" else p.Rearm
    dispatch(store, prepare(store, cls("original", members)))
    store.observe_ack(Ack(0, "original", True, ""), now=2)
    event = p.RealmJob(0, "original", kind, "failed", "cleanup_manifest_readback_changed", members[-1], 0, **fields)
    store.observe_realm_job(event, now=3)
    return store.get("original")


def test_unknown_holds_only_nonreleased_members_keeps_readback_evidence_and_backoff(store, caplog):
    r = failed(store, released=(101, 103), skipped=(101,), readback_guid=102, check="owner")
    assert r.state == "unknown" and r.reserved_manifest == (101, 102, 103)
    assert held_manifest(r) == (102,)
    assert [h.guids for h in store.holds()] == [frozenset({102})]
    assert json.loads(r.evidence)["job"]["readback_guid"] == 102
    assert "readback_guid=102 check=owner" in caplog.text
    assert skipped_backoff(r, 602) == (101,) and skipped_backoff(r, 603) == ()
    store.recover(process_id="again", now=4)
    assert held_manifest(store.get("original")) == (102,)
    store.observe_realm_job(p.RealmJob(0, "original", "leave_pvp", "done", "", 0, 0), now=5)
    assert store.get("original").state == "conflict"
    assert held_manifest(store.get("original")) == (101, 102, 103)


@pytest.mark.parametrize("field,value", [("released", (104,)), ("readback_guid", 104)])
def test_outside_manifest_receipt_cannot_release_anyone(store, field, value):
    fields = {"released": (101,), "readback_guid": 102, "check": "owner", field: value}
    r = failed(store, **fields)
    assert r.state == "dispatched" and held_manifest(r) == (101, 102, 103)


def test_rearm_does_not_accept_leave_release_or_readback_evidence(store):
    r = failed(store, kind="rearm", released=(101,), readback_guid=102, check="owner")
    assert r.state == "dispatched" and held_manifest(r) == (101, 102, 103)


def test_refused_ack_with_failed_job_never_releases_bodies(store):
    dispatch(store, prepare(store, p.LeavePvp("original", (101, 102, 103))))
    store.observe_realm_job(p.RealmJob(0, "original", "leave_pvp", "failed", "partial", 103, 0,
                                      released=(101, 103)), now=2)
    assert held_manifest(store.get("original")) == (102,)
    store.observe_ack(Ack(0, "original", False, "realm_off"), now=3)
    assert held_manifest(store.get("original")) == (101, 102, 103)


@pytest.mark.parametrize("field,value", [("released", [101, 101]), ("released", [True]), ("released", [0]),
    ("released", [2**32]), ("released", "101"), ("readback_guid", True), ("readback_guid", -1),
    ("readback_guid", 0), ("check", "private text"), ("check", ""), ("state", "done")])
def test_leave_wire_rejects_malformed_release_or_readback(field, value):
    p.register()
    row = dict(v=1, type="realm_job", ts=0, id="original", kind="leave_pvp", state="failed",
               reason="cleanup_manifest_readback_changed", guid=103, value=0, released=[101], readback_guid=102, check="owner")
    row[field] = value
    with pytest.raises(ProtocolError): parse_event(json.dumps(row))


class ForgetHarness:
    def __init__(self, store):
        self.store = store
        store.conn.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        store.conn.commit()
        failed(store)
        store.abandon("original", reason="invented operator review", now=4)
        self.context = SimpleNamespace(connection_epoch=1, guild=50, human=1, connected=True, presence_known=True)
        self.clock = SimpleNamespace(now=lambda: 5)
        self.sent, self.waiting = [], {}
        self.forget = ForgetReceipts(store, self.send, lambda: self.context, self.clock)

    async def send(self, cmd, *, preserve_id):
        assert preserve_id
        self.sent.append(cmd)
        self.waiting[cmd.id] = asyncio.get_running_loop().create_future()
        return await self.waiting[cmd.id]

    async def tick(self):
        self.forget.tick()
        for _ in range(4): await asyncio.sleep(0)

    async def ack(self, cmd, ok=True, error=""):
        self.waiting[cmd.id].set_result(Ack(0, cmd.id, ok, error))
        for _ in range(4): await asyncio.sleep(0)

    def result(self, cmd, outcome="released", members=3, *, origin_id="original"):
        self.forget.observe(p.RealmForgotten(0, cmd.id, origin_id, outcome, members))

    def receipt(self):
        row = self.store.conn.execute("SELECT value FROM meta WHERE key=?", (FORGOTTEN_RECEIPT + "original",)).fetchone()
        return json.loads(row[0]) if row else None


def test_forget_once_per_connection_and_late_original_receipts_are_separate(store):
    async def run():
        h = ForgetHarness(store)
        try:
            original = store.get("original")
            await h.tick(); first = h.sent[-1]
            assert first.id != "original"
            h.result(first)  # event can precede ACK; not settled yet
            assert h.receipt() is None
            store.observe_ack(Ack(0, "original", True, ""), now=5)
            assert h.receipt() is None
            await h.tick(); assert len(h.sent) == 1
            h.context.connection_epoch += 1
            await h.tick(); second = h.sent[-1]
            assert len(h.sent) == 2 and second.id != first.id
            await h.ack(first)  # answer belongs to the old connection
            assert h.receipt() is None
            h.result(second, origin_id="foreign"); await h.ack(second)
            assert h.receipt() is None
            h.result(second)
            assert h.receipt() == {"outcome": "released", "members": 3, "at": 5}
            assert store.get("original").payload == original.payload
            assert store.get("original").evidence == original.evidence
            await h.tick(); h.context.connection_epoch += 1; await h.tick()
            assert len(h.sent) == 2
        finally: await h.forget.close()
    asyncio.run(run())


@pytest.mark.parametrize("outcome", ["running", "kind", "busy:teleport", "not_held", "unsupported"])
def test_forget_answers_and_retry_policy(store, outcome, caplog):
    async def run():
        h = ForgetHarness(store)
        try:
            await h.tick(); cmd = h.sent[-1]
            if outcome == "unsupported":
                await h.ack(cmd, False, "unknown type: realm_forget")
            elif outcome == "not_held":
                await h.ack(cmd); h.result(cmd, outcome, 0)
            else:
                h.result(cmd, outcome, 0); await h.ack(cmd, False, outcome)
            await h.tick()
            assert len(h.sent) == (2 if outcome == "busy:teleport" else 1)
            if outcome in {"not_held", "unsupported"}:
                assert h.receipt()["outcome"] == outcome
            else:
                assert h.receipt() is None
            if outcome == "busy:teleport":
                newer = h.sent[-1]; await h.ack(newer); h.result(newer)
                assert h.receipt()["outcome"] == "released"
        finally: await h.forget.close()
    asyncio.run(run())


@pytest.mark.parametrize("change", ["no_receipt", "wrong_identity", "conflict", "unsettled", "not_connected"])
def test_forget_never_sends_without_exact_abandoned_authority(store, change):
    async def run():
        h = ForgetHarness(store)
        try:
            if change == "no_receipt": store.conn.execute("DELETE FROM meta")
            if change == "wrong_identity": h.context.human = 2
            if change == "not_connected": h.context.connected = False
            if change == "unsettled": store.conn.execute("UPDATE realm_action SET transport_state='inflight'")
            if change == "conflict":
                store.observe_realm_job(p.RealmJob(0, "original", "leave_pvp", "done", "", 0, 0), now=5)
            if change == "unsettled":
                from leeroy_mind.realm.action_store import ActionUnavailable
                with pytest.raises(ActionUnavailable): await h.tick()
            else: await h.tick()
            assert not h.sent
        finally: await h.forget.close()
    asyncio.run(run())


def test_unknown_command_ack_for_another_command_never_settles_forget(store):
    async def run():
        h = ForgetHarness(store)
        try:
            await h.tick(); cmd = h.sent[-1]
            h.waiting[cmd.id].set_result(Ack(0, "different", False, "unknown type: realm_forget"))
            await h.tick()
            assert h.receipt() is None and len(h.sent) == 1
        finally: await h.forget.close()
    asyncio.run(run())


def test_late_original_result_prevents_forget_settlement(store):
    async def run():
        h = ForgetHarness(store)
        try:
            await h.tick(); cmd = h.sent[-1]
            store.observe_realm_job(p.RealmJob(0, "original", "leave_pvp", "done", "", 0, 0), now=5)
            await h.ack(cmd); h.result(cmd)
            assert h.receipt() is None
            assert store.get("original").state == "conflict"
            assert held_manifest(store.get("original")) == (101, 102, 103)
        finally: await h.forget.close()
    asyncio.run(run())


@pytest.mark.parametrize("event_first", [False, True])
def test_contradictory_forget_ack_and_result_never_settle_or_retry(store, event_first):
    async def run():
        h = ForgetHarness(store)
        try:
            await h.tick(); cmd = h.sent[-1]
            if event_first: h.result(cmd, "busy:teleport", 0)
            await h.ack(cmd)
            if not event_first: h.result(cmd, "busy:teleport", 0)
            await h.tick()
            assert h.receipt() is None and len(h.sent) == 1
        finally: await h.forget.close()
    asyncio.run(run())


def test_fakebridge_109_member_leave_waits_then_releases_untouched_recall(tmp_path, clock):
    """Actual writer/parser/MindApp/ledger; invented native final-readback script.

    C++ policy tests prove the native wait/skip decisions. This fixture supplies
    their wire outcome after 60s and proves recall never replays the leave or
    sends rearm/defaults to either the skipped or retained member. Release proof
    is not proof a body left PvP, so the 107 released members get a new, smaller
    leave (a no-op for those already out) before their rearm and defaults.
    """
    from conftest import make_persona
    from leeroy_mind.personas.io import save_persona
    from realm.test_realm_connection_authority import application, until
    async def run():
        async with application(tmp_path, clock, original_bridge=True) as (app, client, server, hellos):
            realm = app.realm
            server._known_types.add("bot_do")
            members = tuple(range(101, 210))
            for g in members:
                save_persona(app.store, make_persona(g, f"Fake{g}"), source="seed", ts=clock.now(), race="Human", class_name="Warrior")
            app.personas.invalidate()
            await server.push(dict(type="cast", members=[dict(guid=g, name=f"Fake{g}", race=1,
                gender=0, level=80, rank=3, online=True, map=0, zone=0, spec_tab=0)
                | {"class": 1} for g in members]))
            await until(lambda: set(members) <= app.store.cast().keys())
            realm.recall.on_login(clock.now()); realm.queue.tick()
            leave = await server.wait_for_command("leave_pvp")
            assert leave["guids"] == list(members)
            await until(lambda: realm.action_store.get(leave["id"]).transport_state == "settled")
            # Untouched 101 is still teleporting at the last step. No terminal
            # result exists until the native 60-second readback window expires.
            await clock.advance(59, step=59)
            realm.queue.tick()
            assert realm.action_store.get(leave["id"]).state == "dispatched"
            assert len([c for c in server.commands if c["type"] != "ping"]) == 1
            await clock.advance(1, step=1)
            await server.push(dict(type="realm_job", id=leave["id"], kind="leave_pvp", state="failed",
                reason="cleanup_manifest_readback_changed", guid=209, value=0,
                skipped=[101], released=[g for g in members if g != 102], readback_guid=102, check="owner"))
            await until(lambda: realm.action_store.get(leave["id"]).state == "unknown")
            r = realm.action_store.get(leave["id"])
            assert held_manifest(r) == (102,) and skipped_backoff(r, clock.now()) == (101,)
            assert realm.bodies.movable("realm_day", set(members)) == set(members) - {102}
            realm.recall.tick(clock.now()); realm.queue.tick()
            await until(lambda: sum(c["type"] == "leave_pvp" for c in server.commands) == 2)
            again = [c for c in server.commands if c["type"] == "leave_pvp"][-1]
            assert again["guids"] == list(range(103, 210)) and again["id"] != leave["id"]
            assert not any(c["type"] in {"rearm", "bot_do"} for c in server.commands)
            await until(lambda: realm.action_store.get(again["id"]).transport_state == "settled")
            await server.push(dict(type="realm_job", id=again["id"], kind="leave_pvp", state="done", reason="",
                                   guid=0, value=0))
            await until(lambda: realm.action_store.get(again["id"]).state == "completed")
            await clock.advance(1.01, step=1.01); realm.recall.tick(clock.now()); realm.queue.tick()
            rearm = await server.wait_for_command("rearm")
            assert rearm["guids"] == list(range(103, 210))
            await server.push(dict(type="realm_job", id=rearm["id"], kind="rearm", state="done", reason="", guid=0, value=0))
            await until(lambda: realm.action_store.get(rearm["id"]).state == "completed")
            defaulted = set()
            for _ in range(3):
                await clock.advance(1.01, step=1.01); realm.queue.tick()
                cmd = await server.wait_for_command("bot_do")
                defaulted.update(cmd["guids"])
                await server.push(dict(type="job", id=cmd["id"], kind="bot_do", state="done", reason="", stage="",
                    guids=cmd["guids"], skipped=[], kills=0, wipes=0, instance_id=0, guid=0, level=0, avg_ilvl=0))
                await until(lambda: realm.action_store.get(cmd["id"]).state == "completed")
            assert defaulted == set(range(103, 210))
            assert sum(c["type"] == "leave_pvp" for c in server.commands) == 2
            assert realm.action_store.holds()[0].guids == frozenset({102})
            assert skipped_backoff(r, 600 + json.loads(r.evidence)["skipped_at"]) == ()
    asyncio.run(run())


@pytest.mark.parametrize("supported", [True, False])
def test_actual_bridge_forget_runs_while_realm_off_and_rejects_replaced_bridge(tmp_path, clock, supported):
    from realm.test_realm_connection_authority import application, until
    from leeroy_mind.realm.action_store import ActionSpec
    from leeroy_mind.realm.action_policy import classify
    async def run():
        async with application(tmp_path, clock, original_bridge=True) as (app, client, server, hellos):
            realm = app.realm; store = realm.action_store
            c = classify(p.LeavePvp("old-root", (101,)))
            r = store.prepare(ActionSpec("old-root", None, "root", c.policy, "realm_day", 4, c.kind,
                c.payload, c.reserved_manifest, 21, 7), now=clock.now())
            dispatch(store, r, now=clock.now())
            store.observe_ack(Ack(0, "old-root", True, ""), now=clock.now())
            store.observe_realm_job(p.RealmJob(0, "old-root", "leave_pvp", "failed", "partial", 101, 0), now=clock.now())
            store.abandon("old-root", reason="invented review", now=clock.now())
            realm._set_enabled(False)
            app.bridge = SimpleNamespace(transport_state=client.transport_state)
            try:
                await realm.tick(); await asyncio.sleep(0)
                assert not server.commands
            finally: app.bridge = client
            if not supported: server._known_types.remove("realm_forget")
            await realm.tick()
            cmd = await server.wait_for_command("realm_forget")
            assert cmd["id"] != "old-root" and cmd["origin_id"] == "old-root"
            if supported:
                await server.push(dict(type="realm_forgotten", id=cmd["id"], origin_id="old-root", outcome="released", members=1))
            def receipt():
                return store.conn.execute("SELECT value FROM meta WHERE key=?", (FORGOTTEN_RECEIPT+"old-root",)).fetchone()
            await until(receipt)
            assert json.loads(receipt()[0])["outcome"] == ("released" if supported else "unsupported")
            await realm.tick()
            assert len(server.commands) == 1
            assert store.get("old-root").result_reason == "abandoned"
    asyncio.run(run())
