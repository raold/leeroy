"""Recall integration: real ledger, durable queue and the LivePorts barrier."""

import asyncio
from types import SimpleNamespace

import pytest

from conftest import run
from leeroy_mind.bodies import ALL, BodyHold
from leeroy_mind.guild.protocol import BotDo, Job, JobSkip
from leeroy_mind.protocol import Hello
from leeroy_mind.realm import protocol as p
from leeroy_mind.realm.service import attach_realm
from realm.fakes import RealmApp, enabled


class Pipeline:
    def __init__(self, app, clock):
        self.app, self.clock = app, clock
        self.realm = None
        self.online = set()
        self.members = set()

    async def __aenter__(self):
        self.realm = attach_realm(self.app)
        self.realm.ports.cast = lambda: [SimpleNamespace(guid=g, online=g in self.online)
                                         for g in sorted(self.members)]
        try:
            await self.app.connection(True)
            await self.app.event(Hello(0, "test", "1", 21, 0))
            return self
        except BaseException:
            await self.realm.close()
            raise

    async def __aexit__(self, exc_type, exc, traceback):
        await self.realm.close()

    def cast(self, *guids):
        self.members.update(guids)
        self.online = set(guids)

    async def cycle(self, seconds=0):
        if seconds:
            await self.clock.advance(seconds, step=seconds)
        self.realm.queue.tick()
        for _ in range(12):
            await asyncio.sleep(0)

    async def result(self, cmd, *, ok=True, skipped=()):
        if type(cmd) is BotDo:
            successes = tuple(g for g in cmd.guids if g not in skipped)
            ev = Job(0, cmd.id, "bot_do", "done" if ok else "failed",
                     "" if ok else "partial", "", successes,
                     tuple(JobSkip(g, "busy") for g in skipped), 0, 0, 0, 0, 0, 0)
        else:
            kind = next(k for k, cls in p.COMMANDS.items() if type(cmd) is cls)
            members = cmd.guids
            ev = p.RealmJob(0, cmd.id, kind, "done" if ok else "failed",
                            "" if ok else "partial", members[0] if len(members) == 1 else 0, 0)
        await self.app.event(ev)

    async def finish(self):
        seen = set()
        for _ in range(60):
            await self.cycle()
            fresh = [cmd for cmd in self.app.sent if cmd.id not in seen]
            if not fresh:
                # A rolling pacing slot may still be occupied.
                await self.cycle(1.01)
                fresh = [cmd for cmd in self.app.sent if cmd.id not in seen]
            if not fresh:
                return
            for cmd in fresh:
                seen.add(cmd.id)
                await self.result(cmd)
            await self.cycle(1.01)
        raise AssertionError("recall did not quiesce")


@pytest.fixture
def pipeline(tmp_path, store, clock):
    enabled(tmp_path, True)
    app = RealmApp(tmp_path, store, clock)
    return Pipeline(app, clock)


def test_recall_chains_only_success_and_no_callback_sends_inline(pipeline):
    async def scenario():
        async with pipeline:
            h = pipeline
            h.cast(101, 102, 103)
            h.realm.bodies.add_holder_source("other", lambda: (
                BodyHold("training", "training", 2, {102}, True),
                BodyHold("offline", "offline_raid", 1, {103}, False)))
            h.realm.recall.on_login(h.clock.now())
            await h.cycle()
            assert len(h.app.sent) == 1 and type(h.app.sent[0]) is p.LeavePvp
            assert h.app.sent[0].guids == (101,)
            await h.result(h.app.sent[0])
            assert len(h.app.sent) == 1
            await h.cycle()
            assert type(h.app.sent[1]) is p.Rearm and h.app.sent[1].guids == (101,)
            await h.result(h.app.sent[1])
            assert len(h.app.sent) == 2
            await h.cycle(1.01)
            cmd = h.app.sent[2]
            assert cmd == BotDo(cmd.id, (101,), "nc", "-bg,-lfg", target_guid=0, training_run="")
            await h.result(cmd)
            for _ in range(3):
                h.realm.bodies.poll()
                h.realm.recall.tick(h.clock.now())
                await h.cycle(1.01)
            assert len(h.app.sent) == 3
    run(scenario())


def test_complete_eligible_batches_preserve_per_body_chain_order(pipeline):
    async def scenario():
        async with pipeline:
            h = pipeline
            h.cast(*range(101, 442))
            h.realm.recall.on_login(h.clock.now())
            await h.finish()
            leaves = [c for c in h.app.sent if type(c) is p.LeavePvp]
            rearms = [c for c in h.app.sent if type(c) is p.Rearm]
            defaults = [c for c in h.app.sent if type(c) is BotDo]
            assert [len(c.guids) for c in leaves] == [150, 150, 41]
            assert [c.guids for c in rearms] == [c.guids for c in leaves]
            assert all(1 <= len(c.guids) <= 40 for c in defaults)
            assert set().union(*(set(c.guids) for c in defaults)) == h.online
            for guid in h.online:
                kinds = [type(c) for c in h.app.sent if guid in c.guids]
                assert kinds == [p.LeavePvp, p.Rearm, BotDo]
    run(scenario())


def test_failed_leave_stops_rearm_and_retains_its_manifest(pipeline):
    async def scenario():
        async with pipeline:
            h = pipeline
            h.cast(101, 102)
            h.realm.recall.on_login(h.clock.now())
            await h.cycle()
            await h.result(h.app.sent[0], ok=False)
            h.realm.recall.tick(h.clock.now())
            await h.cycle(2)
            assert len(h.app.sent) == 1
            assert h.realm.action_store.holds()[0].guids == frozenset({101, 102})
    run(scenario())


def test_partial_defaults_stop_later_chunks_and_do_not_restore_on_own_release(pipeline):
    async def scenario():
        async with pipeline:
            h = pipeline
            h.cast(*range(101, 182))
            h.realm.recall.restore_defaults()
            await h.cycle()
            assert type(h.app.sent[0]) is p.Rearm
            await h.result(h.app.sent[0])
            await h.cycle()
            first = h.app.sent[1]
            assert type(first) is BotDo and len(first.guids) == 40
            await h.result(first, ok=False, skipped=(first.guids[0],))
            for _ in range(3):
                h.realm.bodies.poll()
                h.realm.recall.tick(h.clock.now())
                await h.cycle(1.01)
            assert len(h.app.sent) == 2
            assert h.realm.action_store.get(first.id).state == "completed"
    run(scenario())


def test_second_pass_once_and_recall_cooldown(pipeline):
    async def scenario():
        async with pipeline:
            h = pipeline
            h.cast(101)
            start = h.clock.now()
            h.realm.recall.on_login(start)
            await h.finish()
            await h.clock.advance(start + 45 - h.clock.now(), step=45)
            h.realm.recall.tick(h.clock.now())
            await h.cycle()
            assert [type(c) for c in h.app.sent] == [p.LeavePvp, p.Rearm, BotDo, p.LeavePvp]
            await h.result(h.app.sent[-1])
            await h.cycle(46)
            h.realm.recall.tick(h.clock.now())
            h.realm.recall.on_raid_state("ENDED", "FORMING")
            await h.cycle()
            assert len(h.app.sent) == 4
            await h.cycle(start + 121 - h.clock.now())
            h.realm.recall.on_raid_state("IDLE", "FORMING")
            await h.cycle()
            assert len(h.app.sent) == 5 and type(h.app.sent[-1]) is p.LeavePvp
    run(scenario())


def test_empty_cast_sends_nothing_and_offline_defaults_repeat_at_1800(pipeline):
    async def scenario():
        async with pipeline:
            h = pipeline
            h.realm.recall.on_login(h.clock.now())
            await h.cycle(46)
            h.realm.recall.tick(h.clock.now())
            await h.cycle()
            assert h.app.sent == []
            h.cast(101)
            h.realm.recall.on_hello(Hello(0, "test", "1", 21, 0))
            h.realm.recall.restore_defaults((101,))
            await h.finish()
            assert [type(c) for c in h.app.sent] == [p.Rearm, BotDo]
            await h.cycle(1800)
            h.realm.recall.tick(h.clock.now())
            await h.cycle()
            assert type(h.app.sent[-1]) is p.Rearm and len(h.app.sent) == 3
    run(scenario())


def test_expired_claim_under_retained_opaque_owner_hands_back_once(pipeline):
    async def scenario():
        async with pipeline:
            h = pipeline
            h.cast(101)
            owner = "opaque-trial-42"
            retained = [BodyHold("native-operation", owner, 2, {101}, True)]
            h.realm.bodies.add_holder_source("dynamic", lambda: tuple(retained))
            assert h.realm.bodies.claim(owner, {101}, rank=2, until=h.clock.now() + 3) == {101}
            h.realm.recall.restore_defaults((101,))
            await h.cycle(4)
            h.realm.bodies.poll()
            h.realm.recall.tick(h.clock.now())
            assert h.realm.bodies.claims_for(owner) == frozenset()
            assert h.app.sent == []
            retained.clear()
            h.realm.bodies.poll()
            h.realm.recall.tick(h.clock.now())
            await h.finish()
            assert [type(c) for c in h.app.sent] == [p.Rearm, BotDo]
            for _ in range(3):
                await h.cycle(2)
                h.realm.bodies.poll()
                h.realm.recall.tick(h.clock.now())
            assert len(h.app.sent) == 2
    run(scenario())


def test_native_off_fallback_only_eligible_bodies_then_rearm_on_clear(pipeline):
    async def scenario():
        async with pipeline:
            h = pipeline
            h.cast(101, 102)
            h.realm.bodies.add_holder_source("retained", lambda: (BodyHold("foreign", "training", 2, {102}, True),))
            h.realm.queue.off = True
            h.realm.recall.tick(h.clock.now())
            await h.cycle()
            assert len(h.app.sent) == 1 and type(h.app.sent[0]) is BotDo and h.app.sent[0].guids == (101,)
            await h.result(h.app.sent[0])
            await h.cycle(2)
            h.realm.recall.tick(h.clock.now())
            await h.cycle()
            assert len(h.app.sent) == 1
            h.realm.queue.off = False
            h.realm.recall.tick(h.clock.now())
            await h.cycle()
            assert type(h.app.sent[-1]) is p.Rearm and h.app.sent[-1].guids == (101,)
            h.realm._set_enabled(False)
            h.realm.queue.off = True
            h.realm.recall.tick(h.clock.now())
            await h.cycle(601)
            assert len(h.app.sent) == 2 and h.realm.queue.actor_probe is None
    run(scenario())


def test_owned_cleanup_keeps_offline_native_member_then_defaults_handback(pipeline):
    async def scenario():
        async with pipeline:
            h = pipeline
            h.cast(101, 102)
            origin = p.WgJoin("owned-wg", (101, 102))
            h.realm.queue.submit(origin, owner="realm_day")
            await h.cycle()
            token = "a" * 32 + "-" + "b" * 16
            await h.app.event(p.RealmJob(0, origin.id, "wg_join", "done", "", 0, 2, token))
            await h.cycle()
            query = next(c for c in h.app.sent if type(c) is p.GetRealmPvp)
            await h.app.event(p.RealmPvp(0, query.id, token, origin.id, "wintergrasp", 99, 1,
                                       21, 1, (101, 102), "active", False, "", ""))
            h.online = {101}
            h.realm.recall.on_login(h.clock.now())
            await h.cycle(1.01)
            cleanup = next(c for c in h.app.sent if type(c) is p.LeavePvp)
            assert cleanup.guids == (101, 102) and cleanup.pvp_owner == token
            assert h.realm.action_store.holds()[0].guids == frozenset({101, 102})
            await h.app.event(p.RealmJob(0, cleanup.id, "leave_pvp", "done", "", 0, 2, token))
            h.realm.recall.tick(h.clock.now())
            await h.cycle(1.01)
            ordinary = [c for c in h.app.sent if type(c) is p.Rearm]
            assert len(ordinary) == 1 and ordinary[0].guids == (101,)
            assert h.realm.action_store.get(origin.id).state == "completed"
    run(scenario())


def test_source_unavailable_defers_intent_without_false_free_set(pipeline):
    async def scenario():
        async with pipeline:
            h = pipeline
            h.cast(101)
            available = False
            def snapshot():
                if not available:
                    raise RuntimeError("private source detail")
                return ()
            h.realm.bodies.add_holder_source("unavailable", snapshot)
            h.realm.recall.restore_defaults((101,))
            await h.cycle()
            assert h.app.sent == []
            available = True
            h.realm.recall.tick(h.clock.now())
            await h.cycle()
            assert type(h.app.sent[0]) is p.Rearm
    run(scenario())


def test_source_loss_between_steps_defers_chain_until_fresh_evidence(pipeline):
    async def scenario():
        async with pipeline:
            h = pipeline
            h.cast(101)
            available = True
            def snapshot():
                if not available:
                    raise RuntimeError("private source detail")
                return ()
            h.realm.bodies.add_holder_source("interrupted", snapshot)
            h.realm.recall.restore_defaults((101,))
            await h.cycle()
            available = False
            await h.result(h.app.sent[0])
            await h.cycle(2)
            assert len(h.app.sent) == 1 and h.realm.recall._chains
            available = True
            h.realm.recall.tick(h.clock.now())
            await h.cycle()
            assert len(h.app.sent) == 2 and type(h.app.sent[-1]) is BotDo
    run(scenario())


def test_offline_hello_resync_preserves_recall_cooldown(pipeline):
    async def scenario():
        async with pipeline:
            h = pipeline
            h.cast(101)
            h.realm.recall.on_login(h.clock.now())
            await h.finish()
            await h.app.event(Hello(1, "test", "1", 21, 1))
            await h.cycle(2)
            assert sum(type(c) is p.LeavePvp for c in h.app.sent) == 1
    run(scenario())


def test_off_during_login_fallback_clears_into_rearm_defaults(pipeline):
    async def scenario():
        async with pipeline:
            h = pipeline
            h.cast(101)
            h.realm.queue.off = True
            h.realm.recall.on_login(h.clock.now())
            await h.cycle()
            assert type(h.app.sent[0]) is BotDo
            await h.result(h.app.sent[0])
            h.realm.recall.tick(h.clock.now())
            h.realm.queue.off = False
            h.realm.recall.tick(h.clock.now())
            await h.cycle(1.01)
            assert type(h.app.sent[-1]) is p.Rearm
    run(scenario())


def test_real_raid_release_beneath_old_generation_queue_hold_hands_back_once(pipeline):
    async def scenario():
        async with pipeline:
            h = pipeline
            h.cast(101)
            raid = [BodyHold("external-raid", "raid", 0, {101}, False)]
            h.realm.bodies.add_holder_source("external-raid", lambda: tuple(raid))
            h.realm.recall.on_login(h.clock.now())
            await h.cycle()
            old = h.app.sent[0]
            assert type(old) is p.LeavePvp
            h.realm._set_enabled(False)
            raid.clear()
            h.realm.bodies.poll()
            assert h.realm.bodies.holder(101) == "raid"
            h.realm._set_enabled(True)
            await h.result(old)
            # The old callback cannot prepare its old Rearm. The effective release of
            # the combined raid scope now legitimately requests a fresh defaults chain.
            assert not h.realm.recall._chains
            assert h.realm.bodies.holder(101) is None
            h.realm.recall.tick(h.clock.now())
            await h.cycle(1.01)
            assert [type(c) for c in h.app.sent] == [p.LeavePvp, p.Rearm]
            await h.result(h.app.sent[-1])
            await h.cycle(1.01)
            assert [type(c) for c in h.app.sent] == [p.LeavePvp, p.Rearm, BotDo]
            await h.result(h.app.sent[-1])
            for _ in range(3):
                h.realm.bodies.poll()
                h.realm.recall.tick(h.clock.now())
                await h.cycle(1.01)
            assert len(h.app.sent) == 3
    run(scenario())


def test_foreign_owner_at_login_gets_defaults_only_after_real_release(pipeline):
    async def scenario():
        async with pipeline:
            h = pipeline
            h.cast(101)
            held = [BodyHold("foreign-offline", "offline_raid", 1, {101}, True)]
            h.realm.bodies.add_holder_source("offline-example", lambda: tuple(held))
            h.realm.recall.on_login(h.clock.now())
            await h.cycle()
            assert h.app.sent == []
            held.clear()
            assert h.realm.bodies.holder(101) is None
            h.realm.recall.tick(h.clock.now())
            await h.cycle()
            assert len(h.app.sent) == 1 and type(h.app.sent[0]) is p.Rearm
    run(scenario())


def test_defaults_preparation_source_reads_do_not_scale_with_150_member_batch(pipeline):
    async def scenario():
        async with pipeline:
            h = pipeline
            reads = []
            def snapshot():
                reads.append(True)
                return ()
            h.realm.bodies.add_holder_source("counted-selection", snapshot)
            counts = []
            for size in (1, 150):
                h.cast(*range(101, 101 + size))
                reads.clear()
                h.realm.recall.restore_defaults()
                counts.append(len(reads))
                record = h.realm.action_store.records()[-1]
                assert record.kind == "rearm" and record.reserved_manifest == tuple(range(101, 101 + size))
                # No transport worker runs: measure actual ledger/source work
                # while the real store/queue prepares the complete next batch.
                assert h.app.sent == []
                h.realm.queue.drop_owner("realm_day")
                h.realm.recall.invalidate()
            assert counts[0] == counts[1]
            assert 0 < counts[1] <= 6
    run(scenario())


def test_bulk_recall_selection_excludes_lower_foreign_and_same_owner_retained_rows(pipeline):
    async def scenario():
        async with pipeline:
            h = pipeline
            h.cast(101, 102, 103)
            h.realm.bodies.add_holder_source("mixed-selection", lambda: (
                BodyHold("raid-intent", "raid", 0, ALL, False),
                BodyHold("foreign-intent", "training", 2, {102}, False),
                BodyHold("retained-raid", "raid", 0, {103}, True)))
            h.realm.recall.on_login(h.clock.now())
            await h.cycle()
            assert len(h.app.sent) == 1
            assert type(h.app.sent[0]) is p.LeavePvp and h.app.sent[0].guids == (101,)
            assert h.realm.recall._desired[102] == h.realm.recall._desired[103] == "defaults"
    run(scenario())


@pytest.mark.parametrize("boundary", ["flush", "rank", "selection", "enrollment"])
def test_ledger_release_callback_cannot_reenroll_an_invalidated_recall_chain(pipeline, boundary):
    async def scenario():
        async with pipeline:
            h = pipeline
            h.cast(101)
            phase = dict(armed=False, reads=0)
            trigger_at = {"flush": 1, "rank": 1, "selection": 2, "enrollment": 3}[boundary]
            def snapshot():
                if phase["armed"]:
                    phase["reads"] += 1
                if phase["armed"] and phase["reads"] >= trigger_at:
                    return ()
                return (BodyHold("release-trigger", "training", 2, {9000}, False),)
            h.realm.bodies.add_holder_source("reentrant-generation", snapshot)
            assert h.realm.bodies.rank_for("raid") == 0
            invalidated = []
            def invalidate(owner, guids):
                if owner == "training" and 9000 in guids:
                    invalidated.append(h.realm.action_context().generation)
                    # Return to an eligible live context before the original
                    # ledger call returns: the old callback must stay stale.
                    h.realm._set_enabled(False)
                    h.realm._set_enabled(True)
            h.realm.bodies.on_release(invalidate)
            if boundary == "flush":
                phase["armed"] = True
                h.realm.recall.restore_defaults()
                assert h.realm.action_store.records() == ()
                assert not h.app.sent
            else:
                h.realm.recall.on_login(h.clock.now())
                await h.cycle()
                old = h.app.sent[0]
                assert type(old) is p.LeavePvp
                # Read the durable hold without refreshing the ledger here;
                # the scripted counts isolate the unrelated release at the
                # rank, selection, or enrollment read being exercised.
                assert h.realm.action_store.holds()[0].guids == frozenset({101})
                phase["armed"] = True
                await h.result(old)
                records = h.realm.action_store.records()
                assert records[0].state == "completed"
                assert all(r.state == "not_sent" for r in records if r.command_id != old.id)
                assert len(records) == 1
                await h.cycle(2)
                assert h.app.sent == [old]
            assert len(invalidated) == 1 and h.realm.action_context().generation > invalidated[0]
            assert h.realm.action_context().enabled and h.realm.action_context().presence_known
            assert not h.realm.recall._chains and not h.realm.queue._pending
    run(scenario())
