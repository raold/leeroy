"""Recall integration: real ledger, durable queue and the LivePorts barrier."""

import asyncio
from types import SimpleNamespace

import pytest

from conftest import run
from leeroy_mind.bodies import ALL, BodyHold
from leeroy_mind.guild.protocol import BotDo, Job, JobSkip
from leeroy_mind.protocol import Ack, Hello
from leeroy_mind.realm import protocol as p
from leeroy_mind.realm.service import attach_realm
from realm.fakes import RealmApp, enabled

# Every reason native names members by at a leave's ack, in the order it checks them.
LEAVE_REASONS = ["wg_private_state_unavailable", "cleanup_member_not_admitted", "shared_lfg_queue",
                 "shared_group_leader", "shared_lfg_cleanup", "shared_group_cleanup"]


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


def test_leave_native_refuses_for_a_fenced_body_holds_nobody_and_defaults_follow(pipeline):
    async def scenario():
        async with pipeline:
            h = pipeline
            h.cast(101, 102, 103)
            send = h.app.send

            async def answer(cmd, *, preserve_id=False):
                if type(cmd) is not p.LeavePvp:
                    return await send(cmd, preserve_id=preserve_id)
                h.app.sent.append(cmd)
                return Ack(0, cmd.id, False, "busy:raid_owner")  # 102 is in his group
            h.app.send = answer
            h.realm.recall.on_login(h.clock.now())
            await h.cycle()
            leave, = h.app.sent
            record = h.realm.action_store.get(leave.id)
            assert (record.state, record.result_ok, record.result_reason) == ("completed", False, "refused")
            assert h.realm.action_store.holds() == ()
            assert h.realm.bodies.movable("realm_day", {101, 102, 103}) == {101, 102, 103}
            h.realm.recall.tick(h.clock.now())
            await h.cycle(2)
            assert h.app.sent == [leave]       # the refused recall chains nothing
            h.realm.recall.restore_defaults()
            await h.cycle()
            rearm = h.app.sent[-1]
            assert type(rearm) is p.Rearm and rearm.guids == (101, 102, 103)
            await h.result(rearm)
            await h.cycle(1.01)
            assert type(h.app.sent[-1]) is BotDo and h.app.sent[-1].guids == (101, 102, 103)
    run(scenario())


@pytest.mark.parametrize("reason", LEAVE_REASONS)
def test_a_leave_native_refused_for_a_named_member_recalls_the_others(pipeline, reason):
    async def scenario():
        async with pipeline:
            h = pipeline
            h.cast(101, 102, 103)
            send = h.app.send
            refused = []

            async def answer(cmd, *, preserve_id=False):
                if type(cmd) is not p.LeavePvp or 102 not in cmd.guids:
                    return await send(cmd, preserve_id=preserve_id)
                h.app.sent.append(cmd)
                refused.append(cmd)
                return Ack(0, cmd.id, False, f"{reason}:102")
            h.app.send = answer
            h.realm.recall.on_login(h.clock.now())
            await h.cycle()
            h.realm.recall.tick(h.clock.now())
            await h.cycle()
            first, = refused
            record = h.realm.action_store.get(first.id)
            assert (record.state, record.result_ok) == ("completed", False)
            leaves = [c for c in h.app.sent if type(c) is p.LeavePvp]
            assert leaves[0].guids == (101, 102, 103) and leaves[1].guids == (101, 103)
            assert leaves[1].id != first.id
            await h.result(leaves[1])
            await h.cycle(1.01)
            rearm = h.app.sent[-1]
            assert type(rearm) is p.Rearm and rearm.guids == (101, 103)
    run(scenario())


@pytest.mark.parametrize("reason", LEAVE_REASONS)
def test_a_second_pass_within_the_backoff_recalls_everyone_but_the_named_member(pipeline, reason):
    async def scenario():
        async with pipeline:
            h = pipeline
            h.cast(101, 102, 103)
            send = h.app.send

            async def answer(cmd, *, preserve_id=False):
                if type(cmd) is not p.LeavePvp or 102 not in cmd.guids:
                    return await send(cmd, preserve_id=preserve_id)
                h.app.sent.append(cmd)
                return Ack(0, cmd.id, False, f"{reason}:102")
            h.app.send = answer
            start = h.clock.now()
            h.realm.recall.on_login(start)
            seen = set()
            for _ in range(60):
                await h.cycle()
                fresh = [c for c in h.app.sent if c.id not in seen]
                if not fresh:
                    await h.cycle(1.01)
                    fresh = [c for c in h.app.sent if c.id not in seen]
                if not fresh:
                    break
                for c in fresh:
                    seen.add(c.id)
                    if not (type(c) is p.LeavePvp and 102 in c.guids):
                        await h.result(c)
                await h.cycle(1.01)
            await h.clock.advance(start + 45 - h.clock.now(), step=45)
            h.realm.recall.tick(h.clock.now())
            await h.cycle()
            leaves = [c for c in h.app.sent if type(c) is p.LeavePvp]
            assert len(leaves) == 3 and leaves[2].guids == (101, 103)
    run(scenario())


def test_recall_shrinks_145_through_wintergrasp_then_closure_refusals(pipeline, caplog):
    async def scenario():
        async with pipeline:
            h = pipeline
            h.cast(*range(101, 246))
            send = h.app.send
            refused = set()
            wg = set(range(101, 116))
            closure = {116, 117, 118}
            caplog.set_level("INFO", logger="leeroy_mind.realm")

            async def answer(cmd, *, preserve_id=False):
                if type(cmd) is p.LeavePvp:
                    named, reason = ((wg, "wg_private_state_unavailable") if wg & set(cmd.guids)
                                     else (closure, "cleanup_member_not_admitted"))
                    if named & set(cmd.guids):
                        h.app.sent.append(cmd)
                        refused.add(cmd.id)
                        return Ack(0, cmd.id, False, reason + ":" + ",".join(map(str, sorted(named))))
                return await send(cmd, preserve_id=preserve_id)
            h.app.send = answer
            h.realm.recall.on_login(h.clock.now())
            seen = set()
            for _ in range(20):
                await h.cycle(1.01)
                for cmd in h.app.sent[:]:
                    if cmd.id not in seen:
                        seen.add(cmd.id)
                        if cmd.id not in refused:
                            await h.result(cmd)
            leaves = [cmd for cmd in h.app.sent if type(cmd) is p.LeavePvp]
            assert [len(cmd.guids) for cmd in leaves] == [145, 130, 127]
            assert len({cmd.id for cmd in leaves}) == 3
            assert all(set(b.guids) < set(a.guids) for a, b in zip(leaves, leaves[1:]))
            rest = set(range(119, 246))
            assert [set(cmd.guids) for cmd in h.app.sent if type(cmd) is p.Rearm] == [rest]
            assert set().union(*(set(cmd.guids) for cmd in h.app.sent if type(cmd) is BotDo)) == rest
            assert h.realm.action_store.holds() == ()
            assert h.realm.queue.leave_backed_off("raid") == wg | closure
            assert [message for message in caplog.messages if message.startswith("realm_recall_left_out")] == [
                "realm_recall_left_out reason=wg_private_state_unavailable named=15 rest=130",
                "realm_recall_left_out reason=cleanup_member_not_admitted named=3 rest=127"]
    run(scenario())


async def _settle_all(h, leave_answer, *, tick_recall=False):
    """Answer every command the recall sends until it is quiet; leaves through leave_answer.
    tick_recall also runs, as the Realm loop does each second, the body ledger's poll and the
    recall's own tick: a chain that waited for a queue slot goes on, and a hold seen in flight is
    reported, so its end reaches the recall's release callback."""
    seen = set()
    for _ in range(60):
        if tick_recall:
            h.realm.bodies.poll()
            h.realm.recall.tick(h.clock.now())
        await h.cycle()
        fresh = [c for c in h.app.sent if c.id not in seen]
        if not fresh:
            await h.cycle(1.01)
            fresh = [c for c in h.app.sent if c.id not in seen]
        if not fresh:
            return
        if tick_recall:
            await h.cycle(1.01)
            h.realm.bodies.poll()  # the commands in flight are seen holding their bodies
        for c in fresh:
            seen.add(c.id)
            if type(c) is p.LeavePvp:
                await leave_answer(c)
            else:
                await h.result(c)
        await h.cycle(1.01)
    raise AssertionError("recall did not quiesce")


def test_a_battleground_leader_named_at_the_ack_is_left_out_and_142_go(pipeline, caplog):
    # 2026-11-10 at his login: 143 online, 1306 leading a battleground raid.
    async def scenario():
        async with pipeline:
            h = pipeline
            cast = set(range(1200, 1343))
            h.cast(*cast)
            send = h.app.send
            caplog.set_level("INFO", logger="leeroy_mind.realm")

            async def answer(cmd, *, preserve_id=False):
                if type(cmd) is p.LeavePvp and 1306 in cmd.guids:
                    h.app.sent.append(cmd)
                    return Ack(0, cmd.id, False, "shared_group_leader:1306")
                return await send(cmd, preserve_id=preserve_id)
            h.app.send = answer

            async def leave_result(cmd):
                if 1306 not in cmd.guids:  # the refused one has no job
                    await h.result(cmd)
            h.realm.recall.on_login(h.clock.now())
            await _settle_all(h, leave_result)
            leaves = [c for c in h.app.sent if type(c) is p.LeavePvp]
            assert [len(c.guids) for c in leaves] == [143, 142] and set(leaves[1].guids) < set(leaves[0].guids)
            rest = cast - {1306}
            assert [set(c.guids) for c in h.app.sent if type(c) is p.Rearm] == [rest]
            assert set().union(*(set(c.guids) for c in h.app.sent if type(c) is BotDo)) == rest
            assert h.realm.action_store.holds() == ()
            assert h.realm.queue.leave_backed_off("raid") == {1306}
            assert "realm_recall_left_out reason=shared_group_leader named=1 rest=142" in caplog.messages
    run(scenario())


def test_a_member_native_skipped_is_backed_off_and_the_rest_are_rearmed(pipeline, caplog):
    # The same leader, found only at its own step: native leaves it as it was and takes out the rest.
    async def scenario():
        async with pipeline:
            h = pipeline
            cast = set(range(1200, 1343))
            h.cast(*cast)
            caplog.set_level("INFO", logger="leeroy_mind.realm")

            async def skip_leader(cmd):
                skipped = (1306,) if 1306 in cmd.guids else ()
                await h.app.event(p.RealmJob(0, cmd.id, "leave_pvp", "done", "", 0, len(cmd.guids) - len(skipped),
                                             "", None, skipped))
            start = h.clock.now()
            h.realm.recall.on_login(start)
            await _settle_all(h, skip_leader)
            leave, = [c for c in h.app.sent if type(c) is p.LeavePvp]
            assert len(leave.guids) == 143
            rest = cast - {1306}
            assert [set(c.guids) for c in h.app.sent if type(c) is p.Rearm] == [rest]
            assert set().union(*(set(c.guids) for c in h.app.sent if type(c) is BotDo)) == rest
            assert h.realm.action_store.holds() == ()
            assert h.realm.queue.leave_backed_off("raid") == {1306}
            assert "realm_recall_skipped named=1 rest=142" in caplog.messages
            # The second pass recalls the others again and leaves 1306 alone while it is backed off.
            await h.clock.advance(start + 45 - h.clock.now(), step=45)
            h.realm.recall.tick(h.clock.now())
            await h.cycle()
            leaves = [c for c in h.app.sent if type(c) is p.LeavePvp]
            assert len(leaves) == 2 and set(leaves[1].guids) == rest
    run(scenario())


def test_a_leave_that_skipped_everyone_chains_nothing(pipeline):
    async def scenario():
        async with pipeline:
            h = pipeline
            h.cast(101, 102)

            async def skip_all(cmd):
                await h.app.event(p.RealmJob(0, cmd.id, "leave_pvp", "done", "", 0, 0, "", None, cmd.guids))
            h.realm.recall.on_login(h.clock.now())
            await _settle_all(h, skip_all)
            assert [type(c) for c in h.app.sent] == [p.LeavePvp]
            assert h.realm.action_store.holds() == ()
            assert h.realm.queue.leave_backed_off("raid") == {101, 102}
    run(scenario())


def test_a_failed_leave_sends_its_released_members_a_new_leave_never_a_rearm(pipeline, caplog):
    # Native released 103 although its step never ran (the job failed at 102): release proof is
    # not proof it left PvP, so it gets a new, smaller leave before any rearm or defaults.
    async def scenario():
        async with pipeline:
            h = pipeline
            h.cast(101, 102, 103)
            caplog.set_level("INFO", logger="leeroy_mind.realm")

            async def answer(cmd):
                if 102 in cmd.guids:
                    await h.app.event(p.RealmJob(0, cmd.id, "leave_pvp", "failed", "lfg_queue_leave_unproved",
                                                 102, 0, released=(101, 103)))
                else:
                    await h.result(cmd)
            h.realm.recall.on_login(h.clock.now())
            await _settle_all(h, answer, tick_recall=True)
            kinds = [(type(c), c.guids) for c in h.app.sent]
            first, second = (c for c in h.app.sent if type(c) is p.LeavePvp)
            assert kinds[:2] == [(p.LeavePvp, (101, 102, 103)), (p.LeavePvp, (101, 103))]
            assert second.id != first.id
            assert [c.guids for c in h.app.sent if type(c) is p.Rearm] == [(101, 103)]
            assert set().union(*(set(c.guids) for c in h.app.sent if type(c) is BotDo)) == {101, 103}
            assert kinds.index((p.Rearm, (101, 103))) > kinds.index((p.LeavePvp, (101, 103)))
            assert [h.guids for h in h.realm.action_store.holds()] == [frozenset({102})]
            assert "realm_recall_released held=1 skipped=0 rest=2" in caplog.messages
    run(scenario())


def test_a_member_a_done_leave_skipped_gets_no_defaults_from_its_release(pipeline):
    async def scenario():
        async with pipeline:
            h = pipeline
            h.cast(101, 102, 103)

            async def skip_101(cmd):
                await h.app.event(p.RealmJob(0, cmd.id, "leave_pvp", "done", "", 0, 2, "", None, (101,)))
            h.realm.recall.on_login(h.clock.now())
            await _settle_all(h, skip_101, tick_recall=True)
            assert [c.guids for c in h.app.sent if type(c) is p.Rearm] == [(102, 103)]
            assert set().union(*(set(c.guids) for c in h.app.sent if type(c) is BotDo)) == {102, 103}
    run(scenario())


def test_a_whole_job_leave_refusal_rearms_nobody(pipeline):
    async def scenario():
        async with pipeline:
            h = pipeline
            h.cast(101, 102, 103)

            async def refuse_all(cmd):
                await h.app.event(p.RealmJob(0, cmd.id, "leave_pvp", "failed", "operation_transition_unknown",
                                             cmd.guids[0], 0, changed=False, released=cmd.guids))
            h.realm.recall.on_login(h.clock.now())
            await _settle_all(h, refuse_all, tick_recall=True)
            leave, = h.app.sent
            record = h.realm.action_store.get(leave.id)
            assert (record.state, record.result_ok) == ("completed", False)
            assert h.realm.action_store.holds() == ()
    run(scenario())


def test_failed_leave_retries_shrink_strictly_until_everyone_is_held_or_released(pipeline):
    async def scenario():
        async with pipeline:
            h = pipeline
            h.cast(101, 102, 103)

            async def answer(cmd):
                # Each leave changes (and keeps) its second member; with one member left, native
                # released everyone and the chain ends without a leave of the same size.
                held = cmd.guids[1:2]
                await h.app.event(p.RealmJob(0, cmd.id, "leave_pvp", "failed", "cleanup_manifest_readback_changed",
                                             cmd.guids[-1], 0, released=tuple(g for g in cmd.guids if g not in held)))
            h.realm.recall.on_login(h.clock.now())
            await _settle_all(h, answer, tick_recall=True)
            assert [c.guids for c in h.app.sent] == [(101, 102, 103), (101, 103), (101,)]
            assert sorted(min(h.guids) for h in h.realm.action_store.holds()) == [102, 103]
    run(scenario())


def test_a_failed_leave_backs_off_its_skipped_and_releases_only_the_unskipped(pipeline):
    async def scenario():
        async with pipeline:
            h = pipeline
            h.cast(101, 102, 103, 104)

            async def answer(cmd):
                if 102 in cmd.guids:
                    await h.app.event(p.RealmJob(0, cmd.id, "leave_pvp", "failed", "cleanup_manifest_readback_changed",
                                                 104, 0, skipped=(101,), released=(101, 103, 104),
                                                 readback_guid=102, check="owner"))
                else:
                    await h.result(cmd)
            h.realm.recall.on_login(h.clock.now())
            await _settle_all(h, answer, tick_recall=True)
            assert [c.guids for c in h.app.sent if type(c) is p.LeavePvp] == [(101, 102, 103, 104), (103, 104)]
            assert [c.guids for c in h.app.sent if type(c) is p.Rearm] == [(103, 104)]
            assert h.realm.queue.leave_backed_off("raid") == {101}
            assert [h.guids for h in h.realm.action_store.holds()] == [frozenset({102})]
    run(scenario())


def test_repeated_closure_refusals_exhaust_manifest_without_looping(pipeline):
    async def scenario():
        async with pipeline:
            h = pipeline
            h.cast(101, 102, 103, 104)

            async def answer(cmd, *, preserve_id=False):
                assert type(cmd) is p.LeavePvp
                h.app.sent.append(cmd)
                return Ack(0, cmd.id, False, f"cleanup_member_not_admitted:{cmd.guids[0]}")
            h.app.send = answer
            h.realm.recall.on_login(h.clock.now())
            for _ in range(10):
                await h.cycle(1.01)
                h.realm.recall.tick(h.clock.now())
            assert [cmd.guids for cmd in h.app.sent] == [(101, 102, 103, 104), (102, 103, 104), (103, 104), (104,)]
            assert h.realm.action_store.holds() == ()
    run(scenario())


@pytest.mark.parametrize("error", ["cleanup_member_not_admitted:", "cleanup_member_not_admitted:999",
                                   "cleanup_member_not_admitted:101,101"])
def test_invalid_closure_refusal_holds_and_never_retries(pipeline, error):
    async def scenario():
        async with pipeline:
            h = pipeline
            h.cast(101, 102)

            async def answer(cmd, *, preserve_id=False):
                h.app.sent.append(cmd)
                return Ack(0, cmd.id, False, error)
            h.app.send = answer
            h.realm.recall.on_login(h.clock.now())
            for _ in range(4):
                await h.cycle(1.01)
                h.realm.recall.tick(h.clock.now())
            assert len(h.app.sent) == 1
            assert h.realm.action_store.holds()[0].guids == frozenset({101, 102})
    run(scenario())


@pytest.mark.parametrize("refusal", ["step", "ack"])
def test_a_rearm_native_refused_holds_nobody_and_its_defaults_follow(pipeline, refusal):
    async def scenario():
        async with pipeline:
            h = pipeline
            h.cast(101, 102, 103)
            send = h.app.send

            async def answer(cmd, *, preserve_id=False):
                if type(cmd) is not p.Rearm or refusal != "ack":
                    return await send(cmd, preserve_id=preserve_id)
                h.app.sent.append(cmd)
                return Ack(0, cmd.id, False, "busy:raid_owner")
            h.app.send = answer
            h.realm.recall.restore_defaults()
            await h.cycle()
            rearm, = h.app.sent
            assert type(rearm) is p.Rearm and rearm.guids == (101, 102, 103)
            if refusal == "step":  # 102 is in combat: native refuses its step and releases all three
                await h.app.event(p.RealmJob(0, rearm.id, "rearm", "failed", "busy:combat", 102, 0))
            record = h.realm.action_store.get(rearm.id)
            assert (record.state, record.result_ok) == ("completed", False)
            assert h.realm.action_store.holds() == ()
            await h.cycle(1.01)
            defaults = h.app.sent[-1]
            assert type(defaults) is BotDo and defaults.guids == (101, 102, 103)
            await h.result(defaults)
            for _ in range(3):
                h.realm.bodies.poll()
                h.realm.recall.tick(h.clock.now())
                await h.cycle(1.01)
            assert h.app.sent == [rearm, defaults]
            assert h.realm.action_store.holds() == ()
    run(scenario())


def test_a_start_settles_rearms_an_older_build_kept_unknown_and_recall_proceeds(pipeline, store, clock):
    from leeroy_mind.realm.action_policy import classify, document

    def stored(command_id, cmd, reason, guid, value):
        c, at = classify(cmd), clock.now() - 9 * 3600
        ev = {"ack": {"error": "", "ok": True}, "failed_at": at + 0.1,
              "job": {"guid": guid, "kind": c.kind, "pvp_owner": "", "reason": reason, "state": "failed",
                      "value": value}}
        with store.conn:
            store.conn.execute(
                "INSERT INTO realm_action(command_id,parent_id,role,policy,owner,rank,kind,payload,"
                "reserved_manifest,native_manifest,guild,human,state,transport_state,process_id,connection_epoch,"
                "send_error,evidence,result_ok,result_reason,created,updated,dispatched) VALUES "
                "(?,NULL,'root','ordinary','realm_day',4,?,?,?,NULL,21,1,'unknown','settled','old','1','',?,0,"
                "'job_failed',?,?,?)",
                (command_id, c.kind, c.payload, document(list(c.reserved_manifest)), document(ev), at, at + 0.1,
                 at + 0.01))
    stored("first", p.Rearm("first", (101, 102)), "busy:combat", 102, 0)
    stored("late", p.Rearm("late", (103,)), "busy:combat", 103, 0)
    stored("grow", p.GuildGrow("grow", 13, (401, 402, 403)), "operation_transition_unknown", 402, 1)

    async def scenario():
        async with pipeline:
            h = pipeline
            assert {hold.scope_id for hold in h.realm.action_store.holds()} == {"grow"}
            h.cast(101, 102, 103)
            assert h.realm.bodies.movable("realm_day", {101, 102, 103}) == {101, 102, 103}
            h.realm.recall.restore_defaults()
            await h.cycle()
            rearm = h.app.sent[-1]
            assert type(rearm) is p.Rearm and rearm.guids == (101, 102, 103)
    run(scenario())


def test_a_rearm_that_ended_unknown_stops_its_chain_and_holds_its_manifest(pipeline):
    async def scenario():
        async with pipeline:
            h = pipeline
            h.cast(101, 102, 103)
            h.realm.recall.restore_defaults()
            await h.cycle()
            rearm, = h.app.sent
            await h.app.event(p.RealmJob(0, rearm.id, "rearm", "failed", "operation_transition_unknown", 102, 0))
            h.realm.recall.tick(h.clock.now())
            await h.cycle(2)
            assert h.app.sent == [rearm]
            assert h.realm.action_store.holds()[0].guids == frozenset({101, 102, 103})
    run(scenario())


@pytest.mark.parametrize("skipped", [(), (102,), (101, 103), (101, 102, 103)])
def test_a_rearm_that_skipped_members_is_done_and_its_defaults_go_to_everyone(pipeline, skipped, caplog):
    # 2026-11-10/11: native refused all 15 overnight rearms of the cast for one member in combat or
    # dead, or for his group's members at the ack. It now leaves them out and rearms the rest; a
    # skipped member needs no backoff, the next pass rearms it again. () is an older native's event.
    async def scenario():
        async with pipeline:
            h = pipeline
            h.cast(101, 102, 103)
            caplog.set_level("INFO", logger="leeroy_mind.realm")
            h.realm.recall.restore_defaults()
            await h.cycle()
            rearm, = h.app.sent
            assert type(rearm) is p.Rearm and rearm.guids == (101, 102, 103)
            await h.app.event(p.RealmJob(0, rearm.id, "rearm", "done", "", 0, 3 - len(skipped), "", None, skipped))
            record = h.realm.action_store.get(rearm.id)
            assert (record.state, record.result_ok, record.result_reason) == ("completed", True, "")
            assert h.realm.action_store.holds() == ()
            await h.cycle(1.01)
            defaults = h.app.sent[-1]
            assert type(defaults) is BotDo and defaults.guids == (101, 102, 103)
            await h.result(defaults)
            for _ in range(3):
                h.realm.bodies.poll()
                h.realm.recall.tick(h.clock.now())
                await h.cycle(1.01)
            assert h.app.sent == [rearm, defaults]
            assert h.realm.queue.leave_backed_off("realm_day") == set()
            assert h.realm.action_store.holds() == ()
            # The next pass rearms everyone again, the skipped included.
            await h.clock.advance(1800, step=1800)
            h.realm.recall.tick(h.clock.now())
            await h.cycle()
            assert type(h.app.sent[-1]) is p.Rearm and h.app.sent[-1].guids == (101, 102, 103)
            expected = [f"realm_rearm_skipped id={rearm.id} kind=rearm skipped={len(skipped)} members=3"]
            assert [m for m in caplog.messages if m.startswith("realm_rearm_skipped")] == (expected if skipped else [])
    run(scenario())


def test_defaults_that_skipped_a_dead_member_still_send_the_later_batches(pipeline):
    # 81 of 140 defaults batches since 2026-11-09 skipped a member or two (148 dead, 26 busy); each
    # stopped its chain, so the batches after it never went.
    async def scenario():
        async with pipeline:
            h = pipeline
            h.cast(*range(101, 182))
            h.realm.recall.restore_defaults()
            await h.cycle()
            await h.result(h.app.sent[0])
            await h.cycle()
            first = h.app.sent[1]
            assert type(first) is BotDo and len(first.guids) == 40
            await h.result(first, skipped=(first.guids[0], first.guids[7]))  # done, two of them dead
            record = h.realm.action_store.get(first.id)
            assert (record.state, record.result_ok, record.result_reason) == ("completed", False, "defaults_failed")
            answered = {first.id}
            for _ in range(6):
                await h.cycle(1.01)
                for cmd in [c for c in h.app.sent if type(c) is BotDo and c.id not in answered]:
                    answered.add(cmd.id)
                    await h.result(cmd)
            batches = [c for c in h.app.sent if type(c) is BotDo]
            assert [len(c.guids) for c in batches] == [40, 40, 1]
            assert set().union(*(set(c.guids) for c in batches)) == set(range(101, 182))
            for _ in range(3):
                h.realm.bodies.poll()
                h.realm.recall.tick(h.clock.now())
                await h.cycle(1.01)
            assert len([c for c in h.app.sent if type(c) is BotDo]) == 3
            assert h.realm.action_store.holds() == ()
    run(scenario())


def test_partial_defaults_stop_later_chunks_and_do_not_restore_on_own_release(pipeline):
    # A batch native aborted (failed: cancelled, timed out) still stops its chain.
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
            seen = set()
            for _ in range(60):
                await h.cycle()
                fresh = [c for c in h.app.sent if c.id not in seen]
                if not fresh:
                    await h.cycle(1.01)
                    fresh = [c for c in h.app.sent if c.id not in seen]
                if not fresh:
                    break
                for c in fresh:
                    seen.add(c.id)
                    if not (type(c) is p.LeavePvp and 102 in c.guids):
                        await h.result(c)
                await h.cycle(1.01)
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
            # As native's realm_off answer leaves it: off, its actor-set probe 600 s away.
            h.realm.queue.off, h.realm.queue._probe_at = True, h.clock.now() + 600
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
            # Disabled, not even the actor-set probe goes out.
            assert len(h.app.sent) == 2 and not any(type(c) is p.RealmActors for c in h.app.sent)
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
