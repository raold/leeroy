"""Queue tests inject real BodyLedger and actual synchronous transport hooks."""
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import sqlite3

import pytest

from leeroy_mind.bodies import ALL, BodyHold, BodyLedger
from leeroy_mind.guild.protocol import BotDo, Job, JobSkip
from leeroy_mind.protocol import Ack, Hello
from leeroy_mind.realm import protocol as p
from leeroy_mind.realm.actions import ActionContext, ActionQueue, ActionRejected
from leeroy_mind.realm.action_policy import classify, document
from leeroy_mind.realm.action_store import ActionSpec, ActionStore


def run(coro):
    return asyncio.run(coro)


class Clock:
    def __init__(self): self.value = 0.0
    def now(self): return self.value
    async def sleep(self, seconds): await asyncio.sleep(seconds)


class Harness:
    def __init__(self, store):
        self.clock = Clock(); self.bodies = BodyLedger(self.clock); self.store = store
        self.current = ActionContext(True, True, True, 1, "process", 1, 50, 1)
        self.sent = []; self.waits = {}; self.settlements = set(); self.entered = asyncio.Event()
        self.source_calls = 0
        self.queue = ActionQueue(self.send, self.bodies, self.clock, store=store, context=lambda: self.current)
        self.bodies.add_holder_source("realm_actions", self.holds)

    def holds(self):
        self.source_calls += 1
        return self.store.holds()

    async def send(self, cmd):
        # Mirror LivePorts: final hook and writer entry have no intervening await.
        self.queue.before_send(cmd)
        self.sent.append((self.clock.now(), cmd)); self.entered.set()
        future = asyncio.get_running_loop().create_future(); self.waits[cmd.id] = future
        async def settle():
            try: ack = await future
            except Exception as exc:
                self.queue.on_send_error(cmd, type(exc).__name__); raise
            self.queue.on_ack(cmd, ack)
            return ack
        task = asyncio.create_task(settle()); self.settlements.add(task)
        task.add_done_callback(self.settlements.discard)
        return await asyncio.shield(task)

    async def cycle(self, now=None):
        if now is not None: self.clock.value = now
        self.queue.tick()
        for _ in range(6): await asyncio.sleep(0)

    def ack(self, command_id, ok=True, error=""):
        self.waits[command_id].set_result(Ack(0, command_id, ok, error))

    async def finish(self):
        for command_id, future in tuple(self.waits.items()):
            if not future.done(): future.set_result(Ack(0, command_id, True, ""))
        if self.settlements: await asyncio.gather(*tuple(self.settlements), return_exceptions=True)
        await self.queue.close()


@pytest.fixture
def harness():
    conn = sqlite3.connect(":memory:", autocommit=False)
    migration = Path(__file__).parents[2] / "leeroy_mind/storage/migrations/0010_realm_actions.sql"
    conn.executescript(migration.read_text(encoding="utf-8-sig"))
    h = Harness(ActionStore(conn))
    yield h
    conn.close()


def done(command_id, guid, *, value=0):
    return p.RealmJob(0, command_id, "realm_move", "done", "", guid, value)


def test_two_body_sends_in_every_rolling_second_without_waiting_for_native_jobs(harness):
    async def scenario():
        h = harness
        for i in range(8): h.queue.submit(p.RealmMove(f"r{i}", i+2, "home"), owner="realm_day")
        await h.cycle(0)
        assert len(h.sent) == 2
        h.ack("r0"); h.ack("r1")
        await h.cycle(0.5)
        assert len(h.sent) == 2
        await h.cycle(0.999)
        assert len(h.sent) == 2
        await h.cycle(1)
        assert len(h.sent) == 4
        h.ack("r2"); h.ack("r3")
        await h.cycle(2)
        assert len(h.sent) == 6
        for moment, _ in h.sent:
            assert sum(moment <= t < moment + 1 for t, _ in h.sent) <= 2
        assert len(h.store.holds()) == 6  # ACKs alone did not finish native jobs.
        await h.finish()

    run(scenario())


def test_eight_limit_counts_actual_settlement_after_outer_await_cancellation_and_reenable(harness):
    async def scenario():
        h = harness
        for i in range(10): h.queue.submit(p.BankMoney(f"r{i}", 1), owner="realm_day")
        await h.cycle()
        assert len(h.sent) == 8
        for task in tuple(h.queue._tasks): task.cancel()
        for _ in range(6): await asyncio.sleep(0)
        h.current = replace(h.current, enabled=False, generation=2); h.queue.context_changed()
        h.current = replace(h.current, enabled=True, generation=3); h.queue.context_changed()
        h.queue.submit(p.BankMoney("fresh", 1), owner="realm_day")
        await h.cycle()
        assert len(h.sent) == 8
        assert sum(r.transport_state == "inflight" for r in h.store.records()) == 8
        h.ack("r0")
        await h.cycle()
        await h.cycle()
        assert len(h.sent) == 9 and h.sent[-1][1].id == "fresh"
        await h.finish()

    run(scenario())


def test_final_ledger_recheck_blocks_freeing_and_same_owner_retained_scope(harness):
    async def scenario():
        h = harness; result = []
        h.queue.submit(p.Rearm("rearm", (2,)), owner="raid", freeing=True, on_done=lambda *v: result.append(v))
        # A different retained native operation vetoes even the raid scheduling name.
        h.queue.submit(p.RealmMove("move", 2, "home"), owner="realm_day")
        h.queue.before_send(p.RealmMove("move", 2, "home"))
        await h.cycle()
        assert [c.id for _, c in h.sent] == []
        assert result == [(False, "held", 0)]
        assert h.store.get("move").transport_state == "inflight"
        await h.finish()

    run(scenario())


def test_foreign_holder_arriving_after_submit_prevents_send(harness):
    async def scenario():
        h = harness; result = []
        h.queue.submit(p.RealmMove("move", 2, "home"), owner="realm_day", on_done=lambda *v: result.append(v))
        h.bodies.add_holder("raid", 0, lambda: ALL)
        await h.cycle()
        assert h.sent == []
        assert result == [(False, "held", 0)]
        assert h.store.get("move").state == "not_sent"
        await h.finish()

    run(scenario())


def test_dispatch_commit_failure_prevents_transport_entry(harness, monkeypatch):
    async def scenario():
        h = harness
        h.queue.submit(p.RealmMove("move", 2, "home"), owner="realm_day")
        def fail(*args, **kwargs): raise sqlite3.OperationalError("private payload must never be logged")
        monkeypatch.setattr(h.store, "begin_dispatch", fail)
        await h.cycle()
        assert h.sent == []
        assert h.store.get("move").state == "not_sent"
        await h.finish()

    run(scenario())


@pytest.mark.parametrize("job_first", [False, True])
def test_callbacks_once_after_commit_in_either_order_and_chain_only_sends_on_tick(harness, job_first):
    async def scenario():
        h = harness; results = []
        def callback(ok, reason, value):
            assert h.store.get("move").state == "completed"
            assert h.store.holds() == ()
            results.append((ok, reason, value))
            h.queue.submit(p.Rearm("next", (2,)), owner="realm_day")
        h.queue.submit(p.RealmMove("move", 2, "home"), owner="realm_day", on_done=callback)
        await h.cycle()
        if job_first: h.queue.on_realm_job(done("move", 2, value=3))
        h.ack("move")
        for _ in range(6): await asyncio.sleep(0)
        if not job_first: h.queue.on_realm_job(done("move", 2, value=3))
        h.queue.on_realm_job(done("move", 2, value=3))
        assert results == [(True, "", 3)]
        assert len(h.sent) == 1
        await h.cycle()
        assert len(h.sent) == 2
        await h.finish()

    run(scenario())


def test_callback_exception_logs_only_generic_diagnostic(harness, caplog):
    async def scenario():
        h = harness
        def broken(*args): raise RuntimeError("secret_payload")
        h.queue.submit(p.RealmMove("move", 2, "home"), owner="realm_day", on_done=broken)
        await h.cycle(); h.ack("move")
        for _ in range(6): await asyncio.sleep(0)
        h.queue.on_realm_job(done("move", 2))
        assert "realm_action_callback_failed" in caplog.text
        assert "secret_payload" not in caplog.text
        assert h.store.holds() == ()
        await h.finish()

    run(scenario())


def test_disabled_receipts_persist_and_stale_callbacks_never_revive(harness):
    async def scenario():
        h = harness; results = []
        h.queue.submit(p.RealmMove("move", 2, "home"), owner="realm_day", on_done=lambda *v: results.append(v))
        h.queue.submit(p.RealmMove("unsent", 3, "home"), owner="realm_day")
        # Enter one barrier directly to leave the other intent unsent.
        h.queue.before_send(p.RealmMove("move", 2, "home"))
        h.current = replace(h.current, enabled=False, generation=2); h.queue.context_changed()
        assert h.store.get("unsent").state == "not_sent"
        assert h.queue.on_ack(p.RealmMove("move", 2, "home"), Ack(0, "move", True, ""))
        assert h.queue.on_realm_job(done("move", 2))
        assert h.store.get("move").state == "completed"
        assert results == []
        with pytest.raises(ActionRejected): h.queue.submit(p.Rearm("disabled", (2,)), owner="raid")
        await h.cycle()
        assert h.sent == []
        h.current = replace(h.current, enabled=True, generation=3); h.queue.context_changed()
        assert results == []
        await h.finish()

    run(scenario())


def test_drop_owner_only_removes_prepared_and_owns_command_is_exact(harness):
    async def scenario():
        h = harness
        cmd = p.RealmMove("move", 2, "home")
        h.queue.submit(cmd, owner="realm_day")
        h.queue.submit(p.Rearm("prepared", (3,)), owner="realm_day")
        assert h.queue.owns_command(cmd)
        assert not h.queue.owns_command(replace(cmd, guid=3))
        h.queue.before_send(cmd)
        assert h.queue.drop_owner("realm_day") == 1
        assert h.store.get("move").state == "dispatched"
        assert h.store.holds()[0].scope_id == "move"
        await h.finish()

    run(scenario())


def test_backoff_is_per_owner_kind_guid_for_600_seconds_and_history_bounded(harness):
    async def scenario():
        h = harness
        h.queue.submit(p.RealmMove("move", 2, "home"), owner="realm_day")
        await h.cycle(); h.ack("move", False, "busy:combat")
        for _ in range(6): await asyncio.sleep(0)
        with pytest.raises(ActionRejected, match="backoff"):
            h.queue.submit(p.RealmMove("retry", 2, "home"), owner="realm_day")
        h.queue.submit(p.RealmMove("other_guid", 3, "home"), owner="realm_day")
        h.queue.submit(p.Rearm("other_kind", (2,)), owner="realm_day")
        h.clock.value = 600
        h.queue.submit(p.RealmMove("after_backoff", 2, "home"), owner="realm_day")
        # Backoff expiry permits enrollment; the retained native hold still vetoes dispatch.
        await h.cycle()
        await h.cycle()
        assert h.store.get("after_backoff").state == "not_sent"
        await h.finish()

    run(scenario())


@pytest.mark.parametrize("reason", ["wg_private_state_unavailable", "cleanup_member_not_admitted"])
def test_named_leave_backoff_filters_only_named_members_for_exactly_600_seconds(harness, reason):
    async def scenario():
        h = harness
        h.queue.submit(p.LeavePvp("leave", (101, 102, 103)), owner="raid")
        await h.cycle(10)
        h.ack("leave", False, f"{reason}:102")
        await h.cycle()
        assert h.store.holds() == ()
        assert h.queue.leave_backed_off("raid") == {102}
        assert h.queue.leave_backed_off("realm_day") == set()
        h.queue.submit(p.LeavePvp("rest", (101, 103)), owner="raid")
        h.clock.value = 609.999
        with pytest.raises(ActionRejected, match="backoff"):
            h.queue.submit(p.LeavePvp("too_soon", (102,)), owner="raid")
        assert h.queue.leave_backed_off("raid") == {102}
        h.clock.value = 610
        assert h.queue.leave_backed_off("raid") == set()
        h.queue.submit(p.LeavePvp("later", (102,)), owner="raid")
        await h.finish()
    run(scenario())


def test_native_off_allows_only_exact_defaults_and_offline_hello_clears_scheduling(harness):
    async def scenario():
        h = harness
        h.queue.submit(p.RealmMove("move", 2, "home"), owner="realm_day")
        await h.cycle(); h.ack("move", False, "realm_off")
        for _ in range(6): await asyncio.sleep(0)
        assert h.queue.off
        with pytest.raises(ActionRejected, match="realm_off"):
            h.queue.submit(p.Rearm("rearm", (3,)), owner="raid", freeing=True)
        h.queue.submit(BotDo("defaults", (3,), "nc", "-bg,-lfg"), owner="raid")
        await h.cycle()
        assert [c.id for _, c in h.sent] == ["move", "defaults"]
        h.queue.on_hello(Hello(0, "server", "version", 51, 0))
        assert h.queue.off
        h.queue.on_hello(Hello(0, "server", "version", 50, 0))
        assert not h.queue.off
        assert h.store.get("move").state == "completed"
        await h.finish()

    run(scenario())


def test_offline_hello_preserves_retained_unknown_and_native_fence(harness):
    async def scenario():
        h = harness
        h.queue.submit(p.RealmMove("move", 2, "home"), owner="realm_day")
        await h.cycle(); h.ack("move", False, "busy:combat")
        for _ in range(6): await asyncio.sleep(0)
        h.queue.off = True
        h.queue.on_hello(Hello(0, "server", "version", 50, 0))
        assert not h.queue.off
        assert h.store.get("move").state == "unknown"
        assert h.store.holds()[0].guids == frozenset({2})
        await h.finish()

    run(scenario())


def test_no_actor_probe_without_actual_callback_and_disabled_means_no_probe(harness):
    async def scenario():
        h = harness; h.queue.off = True
        await h.cycle(601)
        assert h.sent == []
        calls = []
        h.queue.actor_probe = lambda: calls.append(True) or p.RealmActors("probe", (2,), (2,), (50,))
        h.current = replace(h.current, enabled=False, generation=2)
        await h.cycle(1201)
        assert calls == [] and h.sent == []
        h.current = replace(h.current, enabled=True, generation=3)
        await h.cycle(1801)
        assert calls == [True]
        probe = h.sent[0][1]
        assert (probe.guids, probe.gankers, probe.guilds) == ((2,), (2,), (50,))
        h.ack("probe")
        for _ in range(6): await asyncio.sleep(0)
        assert not h.queue.off
        await h.finish()

    run(scenario())


def test_guids_cannot_erase_targets_and_unsafe_freeing_rejects_before_journal(harness):
    h = harness
    h.queue.submit(p.Rearm("extracted", (3, 2)), owner="realm_day")
    assert h.store.get("extracted").reserved_manifest == (2, 3)
    with pytest.raises(ValueError): h.queue.submit(p.Rearm("wrong", (3, 2)), owner="realm_day", guids=(2,))
    with pytest.raises(ValueError): h.queue.submit(p.Rearm("duplicates", (3, 2)), owner="realm_day", guids=(2, 2))
    with pytest.raises(AssertionError): h.queue.submit(p.RealmMove("free", 2, "home"), owner="raid", freeing=True)
    assert len(h.store.records()) == 1


async def admitted_wg(h):
    token = "a"*32 + "-" + "b"*16
    h.queue.submit(p.WgJoin("wg", (4, 3, 2)), owner="realm_day")
    await h.cycle()
    h.ack("wg")
    for _ in range(6): await asyncio.sleep(0)
    h.queue.on_realm_job(p.RealmJob(0, "wg", "wg_join", "done", "", 0, 2, token))
    await h.cycle()
    query = next(cmd for _, cmd in h.sent if type(cmd) is p.GetRealmPvp)
    h.queue.on_realm_pvp(p.RealmPvp(0, query.id, token, "wg", "wintergrasp", 1, 1,
                                   50, 1, (2, 3), "active", False, "", ""))
    h.ack(query.id)
    for _ in range(6): await asyncio.sleep(0)
    return token


def test_cleanup_uses_complete_native_subset_and_counts_in_body_pacing(harness):
    async def scenario():
        h = harness; token = await admitted_wg(h)
        h.bodies.add_holder("raid", 0, lambda: ALL)
        h.queue.request_cleanup("wg")
        await h.cycle(0.1)
        cleanup = next(cmd for _, cmd in h.sent if type(cmd) is p.LeavePvp)
        assert cleanup.guids == (2, 3) and cleanup.pvp_owner == token
        assert h.queue.owns_command(cleanup)
        assert h.store.holds()[0].guids == frozenset({2, 3, 4})
        # Origin at0 + cleanup at0.1 consume the two-body rolling window.
        h.queue.submit(BotDo("defaults", (5,), "nc", "-bg,-lfg"), owner="raid")
        await h.cycle(0.99)
        assert not any(cmd.id == "defaults" for _, cmd in h.sent)
        h.ack(cleanup.id)
        for _ in range(6): await asyncio.sleep(0)
        h.queue.on_realm_job(p.RealmJob(0, cleanup.id, "leave_pvp", "done", "", 0, 2, token))
        assert h.store.holds() == ()
        await h.cycle(1)
        assert any(cmd.id == "defaults" for _, cmd in h.sent)
        await h.finish()

    run(scenario())


def test_local_disable_blocks_recovery_query_and_cleanup_but_accepts_receipts(harness):
    async def scenario():
        h = harness; token = await admitted_wg(h)
        h.current = replace(h.current, enabled=False, generation=2); h.queue.context_changed()
        with pytest.raises(ActionRejected): h.queue.request_cleanup("wg")
        before = len(h.sent)
        await h.cycle(100)
        assert len(h.sent) == before
        h.queue.on_realm_job(p.RealmJob(0, "wg", "wg_join", "failed", "ending", 0, 0, token))
        assert h.store.get("wg").state == "unknown"
        assert h.store.holds()
        await h.finish()

    run(scenario())


def test_query_global_pacing_per_root_spacing_and_no_automatic_mutation_replay(harness):
    async def scenario():
        h = harness
        for i in range(3):
            h.queue.submit(p.WgJoin(f"wg{i}", (i+2,)), owner="realm_day")
        await h.cycle(0)
        h.ack("wg0"); h.ack("wg1")
        await h.cycle(0.1)
        # At most two independent read-only queries in a rolling second.
        queries = [(t, cmd) for t, cmd in h.sent if type(cmd) is p.GetRealmPvp]
        assert len(queries) == 1
        h.ack(queries[0][1].id)
        await h.cycle(0.2)
        await h.cycle(0.2)
        queries = [(t, cmd) for t, cmd in h.sent if type(cmd) is p.GetRealmPvp]
        assert len(queries) == 2
        for _, cmd in queries:
            if not h.waits[cmd.id].done(): h.ack(cmd.id)
        await h.cycle(1.0)
        h.ack("wg2")
        await h.cycle(1.1)
        await h.cycle(1.1)
        queries = [(t, cmd) for t, cmd in h.sent if type(cmd) is p.GetRealmPvp]
        assert all(sum(t <= q < t+1 for q, _ in queries) <= 2 for t, _ in queries)
        for _, cmd in queries:
            if not h.waits[cmd.id].done(): h.ack(cmd.id)
        await h.cycle(9.9)
        assert len([cmd for _, cmd in h.sent if type(cmd) is p.WgJoin]) == 3
        per_root = {}
        for t, cmd in h.sent:
            if type(cmd) is p.GetRealmPvp:
                if cmd.origin_id in per_root: assert t-per_root[cmd.origin_id] >= 10
                per_root[cmd.origin_id] = t
        await h.finish()

    run(scenario())


def test_close_awaits_entered_transports_accepts_receipts_and_stops_unsent(harness):
    async def scenario():
        h = harness; results = []
        for i in range(3):
            h.queue.submit(p.RealmMove(f"r{i}", i+2, "home"), owner="realm_day", on_done=lambda *v: results.append(v))
        await h.cycle()
        close = asyncio.create_task(h.queue.close())
        await asyncio.sleep(0)
        assert not close.done()
        assert h.store.get("r2").state == "not_sent"
        h.ack("r0"); h.ack("r1")
        await close
        assert h.queue.on_realm_job(done("r0", 2))
        assert h.queue.on_realm_job(done("r1", 3))
        assert results == [] and h.store.holds() == ()
        assert len(h.sent) == 2
    run(scenario())


def test_final_batch_gate_rejects_cross_member_callback_invalidation(harness):
    h = harness

    def install_script(ledger):
        state = {"armed": False, "reads": 0}
        callback_grants = []
        held = (BodyHold("training-lab", "training", 2, frozenset({102}), False),)

        def snapshot():
            if state["armed"]:
                state["reads"] += 1
                # Model the former queue gate: rank read, first member read,
                # then a later member read releases102 and claims checked101.
                if state["reads"] >= 3: return ()
            return held

        def handoff(owner, guids):
            if owner == "training" and 102 in guids:
                callback_grants.append(ledger.claim("offline_raid", (101,), rank=1, until=h.clock.now()+60))

        ledger.add_holder_source("scripted_training", snapshot)
        assert ledger.holder(102) == "training"  # Prime the notification baseline.
        ledger.on_release(handoff)
        return state, callback_grants

    # An explicit control proves this fixture distinguishes the unsafe gate:
    # both captured member results are true, yet the earlier member was taken
    # by the callback during the later refresh.
    control = BodyLedger(h.clock)
    control_state, control_grants = install_script(control)
    control_state["armed"] = True
    assert control.rank_for("realm_day") == 4
    first = control.may_move("realm_day", 101)
    second = control.may_move("realm_day", 102)
    assert first is True and second is True
    assert control_grants == [{101}]
    assert control.claims_for("offline_raid") == frozenset({101})

    state, grants = install_script(h.bodies)
    cmd = p.Rearm("batch", (101, 102))
    h.queue.submit(cmd, owner="realm_day")
    state["armed"] = True
    with pytest.raises(ActionRejected, match="held"):
        h.queue.before_send(cmd)
    assert state["reads"] == 1
    assert grants == []
    assert h.store.get(cmd.id).state == "prepared"
    assert h.store.holds() == ()
    assert h.sent == []


def test_final_150_member_gate_reads_real_store_source_once_and_rechecks_next_barrier(harness):
    h = harness
    # Match the profiled near-capacity journal while keeping all uncertain
    # native reservations disjoint from the150 requested members.
    for index in range(127):
        cmd = p.RealmMove(f"uncertain{index}", 1000+index, "home")
        c = classify(cmd)
        h.store.prepare(ActionSpec(cmd.id, None, "root", c.policy, "realm_event", 3,
                                  c.kind, c.payload, c.reserved_manifest, 50, 1), now=0)
        h.store.begin_dispatch(cmd.id, payload=c.payload, process_id="process", connection_epoch=1, now=0)
        h.store.observe_send_error(cmd.id, "TimeoutError", now=0)
    members = tuple(range(101, 251))
    cmd = p.Rearm("batch", members)
    h.queue.submit(cmd, owner="realm_day")
    h.source_calls = 0  # Enrollment is separate from the final transport barrier.
    h.queue.before_send(cmd)
    assert h.source_calls == 1
    assert h.store.get(cmd.id).reserved_manifest == members
    assert h.store.get(cmd.id).state == "dispatched"

    h.queue.on_ack(cmd, Ack(0, cmd.id, True, ""))
    h.queue.on_realm_job(p.RealmJob(0, cmd.id, "rearm", "done", "", 0, 0))
    next_cmd = p.Rearm("next_batch", members)
    h.queue.submit(next_cmd, owner="realm_day")
    h.bodies.add_holder("raid", 0, lambda: {250})
    h.source_calls = 0
    with pytest.raises(ActionRejected, match="held"):
        h.queue.before_send(next_cmd)
    assert h.source_calls == 1
    assert h.store.get(next_cmd.id).state == "prepared"
    assert len(h.store.holds()) == 127
    assert h.sent == []


async def flush():
    for _ in range(8): await asyncio.sleep(0)


def defaults_result(command_id, guids, *, failed=False):
    return Job(0, command_id, "bot_do", "failed" if failed else "done", "failed" if failed else "", "",
               () if failed else tuple(guids), tuple(JobSkip(g, "offline") for g in guids) if failed else (),
               0, 0, 0, 0, 0, 0)


async def finish_success(h, cmd):
    c = classify(cmd, child=True)
    if c.policy == "defaults":
        h.queue.on_job(defaults_result(cmd.id, cmd.guids))
    elif c.policy == "ordinary":
        guid = c.reserved_manifest[0] if len(c.reserved_manifest) == 1 else 0
        h.queue.on_realm_job(p.RealmJob(0, cmd.id, c.kind, "done", "", guid, 0))
    h.ack(cmd.id)
    await flush()


@pytest.mark.parametrize("policy", ["refusal", "failed_defaults"])
@pytest.mark.parametrize("per_s", [1, 2])
def test_prequeued_retry_observes_new_backoff_without_extending_window(harness, policy, per_s):
    async def scenario():
        h = harness; h.queue.per_s = per_s; results = []
        def command(command_id, guid):
            return (p.Rearm(command_id, (guid,)) if policy == "refusal"
                    else BotDo(command_id, (guid,), "nc", "-bg,-lfg"))
        a, b, retry = command("a", 2), command("b", 3), command("retry", 2)
        h.queue.submit(a, owner="realm_day")
        h.queue.submit(b, owner="realm_day")
        h.queue.submit(retry, owner="realm_day", on_done=lambda *v: results.append(v))
        h.queue.submit(command("other_guid", 4), owner="realm_day")
        h.queue.submit(command("other_owner", 2), owner="raid")
        await h.cycle(0)
        if policy == "refusal":
            h.ack("a", False, "unsupported")  # Exact released no-admission refusal.
        else:
            h.queue.on_job(defaults_result("a", (2,), failed=True))
            h.ack("a")  # Full accounting ends this failed restricted native job.
        await flush()
        assert h.store.get("a").state == "completed"
        assert h.store.get("a").result_ok is False
        settled = {"a"}
        for moment in range(1, 9):
            # Finish currently dispatched unaffected work before another body
            # command can collide with its temporary retained reservation.
            for _, cmd in tuple(h.sent):
                if cmd.id not in settled:
                    await finish_success(h, cmd); settled.add(cmd.id)
            await h.cycle(moment)
        for _, cmd in tuple(h.sent):
            if cmd.id not in settled:
                await finish_success(h, cmd); settled.add(cmd.id)
        assert {cmd.id for _, cmd in h.sent} == {"a", "b", "other_guid", "other_owner"}
        victim = h.store.get("retry")
        assert victim.state == "not_sent" and victim.result_reason == "backoff"
        assert "failed_at" not in json.loads(victim.evidence)
        assert results == [(False, "backoff", 0)]
        # A different kind remains eligible for the failed GUID/owner triple.
        other_kind = p.RealmMove("other_kind", 2, "home")
        h.queue.submit(other_kind, owner="realm_day")
        await h.cycle(9)
        assert any(cmd.id == "other_kind" for _, cmd in h.sent)
        await finish_success(h, other_kind)
        h.clock.value = 599
        with pytest.raises(ActionRejected, match="backoff"):
            h.queue.submit(command("too_soon", 2), owner="realm_day")
        h.clock.value = 600
        h.queue.submit(command("at_expiry", 2), owner="realm_day")
        await h.cycle()
        assert any(cmd.id == "at_expiry" for _, cmd in h.sent)
        await h.finish()
    run(scenario())


def test_final_backoff_reads_failure_committed_by_ledger_callback(harness):
    async def scenario():
        h = harness
        rows = {"value": (BodyHold("lab", "training", 2, frozenset({99}), False),)}
        h.bodies.add_holder_source("training", lambda: rows["value"])
        assert h.bodies.holder(99) == "training"
        first = p.Rearm("first", (2,)); retry = p.Rearm("retry", (2,))
        h.queue.submit(first, owner="realm_day")
        h.queue.before_send(first)
        h.queue.submit(retry, owner="realm_day")
        seen = []
        def released(owner, guids):
            if owner == "training":
                seen.append(guids)
                h.queue.on_ack(first, Ack(0, first.id, False, "unsupported"))
        h.bodies.on_release(released)
        rows["value"] = ()
        await h.cycle()
        assert seen == [{99}]
        assert h.store.get("first").state == "completed"
        assert h.store.get("retry").result_reason == "backoff"
        assert "failed_at" not in json.loads(h.store.get("retry").evidence)
        assert h.sent == []
        await h.finish()
    run(scenario())


@pytest.mark.parametrize("change", ["disabled", "generation", "process", "epoch", "guild", "human", "connection", "presence", "cancel_entry"])
def test_final_dispatch_rechecks_context_and_entry_after_ledger_callback(harness, change):
    async def scenario():
        h = harness; callbacks = []; receipts = []
        rows = {"value": (BodyHold("lab", "training", 2, frozenset({99}), False),)}
        h.bodies.add_holder_source("training", lambda: rows["value"])
        assert h.bodies.holder(99) == "training"
        cmd = p.Rearm("candidate", (2,))
        h.queue.submit(cmd, owner="realm_day", on_done=lambda *v: receipts.append(v))
        def released(owner, guids):
            if owner != "training": return
            callbacks.append(guids)
            if change == "cancel_entry":
                assert h.queue.drop_owner("realm_day") == 1
            else:
                changes = {"disabled": {"enabled": False}, "generation": {"generation": 2},
                           "process": {"process_id": "replacement"}, "epoch": {"connection_epoch": 2},
                           "guild": {"guild": 51}, "human": {"human": 5},
                           "connection": {"connected": False, "presence_known": False},
                           "presence": {"presence_known": False}}
                h.current = replace(h.current, **changes[change])
                # Intentionally omit context_changed: the queue's final barrier
                # must independently reject return-time stale admission truth.
        h.bodies.on_release(released)
        rows["value"] = ()
        await h.cycle()
        assert callbacks == [{99}]
        assert h.sent == [] and receipts == []
        record = h.store.get(cmd.id)
        assert record.state == "not_sent" and record.transport_state == "not_entered"
        assert record.process_id == "" and record.connection_epoch == 0
        assert "failed_at" not in json.loads(record.evidence)
        await h.finish()
    run(scenario())


def seed_wg(h, index=0):
    command_id = f"wg{index}"
    token = f"{index+1:032x}-{1:016x}"
    cmd = p.WgJoin(command_id, (200+index,))
    c = classify(cmd)
    h.store.prepare(ActionSpec(command_id, None, "root", c.policy, "realm_day", 4,
                              c.kind, c.payload, c.reserved_manifest, 50, 1), now=0)
    h.store.begin_dispatch(command_id, payload=c.payload, process_id="process", connection_epoch=1, now=0)
    h.store.observe_ack(Ack(0, command_id, True, ""), now=0)
    h.store.observe_realm_job(p.RealmJob(0, command_id, "wg_join", "done", "", 0, 1, token), now=0)
    return command_id, token, cmd.guids


def stored_snapshot(h, origin_id, token, members, query_id, *, now, revision, stage="active", fenced=False):
    query = p.GetRealmPvp(query_id, origin_id=origin_id)
    record = h.store.prepare_query(origin_id, query, now=now)
    h.store.begin_dispatch(query.id, payload=record.payload, process_id="process", connection_epoch=1, now=now)
    h.store.observe_pvp(p.RealmPvp(0, query_id, token, origin_id, "wintergrasp", 1, revision,
                                 50, 1, members, stage, fenced, "", ""), now=now)
    h.store.observe_ack(Ack(0, query_id, True, ""), now=now)


def test_query_recovery_fairly_serves_24_unresolved_roots_with_actual_dispatch_pacing(harness):
    async def scenario():
        h = harness
        origins = {origin: (token, members) for origin, token, members in (seed_wg(h, i) for i in range(24))}
        seen = set()
        for step in range(65):
            await h.cycle(step / 4)  # Exact quarter seconds avoid timing tolerance.
            for _, query in tuple(h.sent):
                assert type(query) is p.GetRealmPvp
                if query.id in seen: continue
                seen.add(query.id)
                token, members = origins[query.origin_id]
                h.queue.on_realm_pvp(p.RealmPvp(0, query.id, token, query.origin_id, "wintergrasp", 1, 1,
                                               50, 1, members, "active", False, "", ""))
                h.ack(query.id)
            await flush()
        queries = [(moment, query) for moment, query in h.sent if type(query) is p.GetRealmPvp]
        assert {query.origin_id for _, query in queries} == set(origins)
        # At saturation the first20 must not repeat before roots20..23 get
        # their first query. Fixed root-order selection fails this assertion.
        assert len({query.origin_id for _, query in queries[:24]}) == 24
        assert all(h.store.get(origin).native_manifest == members for origin, (_, members) in origins.items())
        for moment, _ in queries:
            assert sum(moment <= other < moment+1 for other, _ in queries) <= 2
        per_origin = {}
        for moment, query in queries:
            if query.origin_id in per_origin: assert moment-per_origin[query.origin_id] >= 10
            per_origin[query.origin_id] = moment
        assert len(h.store.unresolved()) == len(h.store.holds()) == 24
        assert all(h.store.get(origin).state != "completed" for origin in origins)
        await h.finish()
    run(scenario())


def test_exhausted_cleanup_controller_capacity_keeps_worker_and_unrelated_sends_alive(harness, caplog):
    async def scenario():
        h = harness
        origin, token, members = seed_wg(h)
        stored_snapshot(h, origin, token, members, "initial_query", now=0, revision=1)
        for index in range(17):
            now = 20*index+1
            cmd = p.LeavePvp(f"cleanup{index}", members, token)
            record = h.store.prepare_cleanup(origin, cmd, now=now)
            h.store.begin_dispatch(cmd.id, payload=record.payload, process_id="process", connection_epoch=1, now=now)
            h.store.observe_send_error(cmd.id, "TimeoutError", now=now)
            stored_snapshot(h, origin, token, members, f"cancel_query{index}", now=now+19,
                            revision=index+2, stage="ending", fenced=True)
        h.clock.value = 360
        assert len(json.loads(h.store.get(origin).evidence)["cleanup_ids"]) == 17
        h.queue.request_cleanup(origin)
        unrelated = p.RealmMove("unrelated", 2, "home")
        h.queue.submit(unrelated, owner="realm_day")
        worker = asyncio.create_task(h.queue.run())
        try:
            await flush(); await flush()
            assert not worker.done() and h.queue._running
            assert origin not in h.queue._cleanup_wanted
            assert "realm_action_cleanup_capacity" in caplog.text
            assert any(cmd.id == "unrelated" for _, cmd in h.sent)
            assert not any(type(cmd) is p.LeavePvp for _, cmd in h.sent)
            await finish_success(h, unrelated)
            assert h.store.get("unrelated").state == "completed"
            h.queue.wake(); await flush()
            assert not worker.done()
            assert len(json.loads(h.store.get(origin).evidence)["cleanup_ids"]) == 17
            assert [hold.scope_id for hold in h.store.holds()] == [origin]
        finally:
            await h.finish()
            await worker
    run(scenario())


@pytest.mark.parametrize("capacity", ["roots", "backoff_history"])
def test_configured_probe_capacity_is_deferred_while_worker_and_receipts_continue(harness, caplog, capacity):
    async def scenario():
        h = harness
        count = 127 if capacity == "roots" else 255
        for index in range(count):
            cmd = p.RealmMove(f"old{index}", 1000+index, "home")
            c = classify(cmd)
            h.store.prepare(ActionSpec(cmd.id, None, "root", c.policy, "realm_event", 3,
                                      c.kind, c.payload, c.reserved_manifest, 50, 1), now=0)
            h.store.begin_dispatch(cmd.id, payload=c.payload, process_id="process", connection_epoch=1, now=0)
            if capacity == "roots": h.store.observe_send_error(cmd.id, "TimeoutError", now=0)
            else: h.store.observe_ack(Ack(0, cmd.id, False, "unsupported"), now=0)
        defaults = BotDo("eligible_defaults", (2,), "nc", "-bg,-lfg")
        h.queue.submit(defaults, owner="raid")
        h.queue.off = True
        probes = []
        def actor_probe():
            cmd = p.RealmActors(f"probe{len(probes)}", (2,), (2,), (50,))
            probes.append(cmd)
            return cmd
        h.queue.actor_probe = actor_probe
        worker = asyncio.create_task(h.queue.run())
        try:
            await flush(); await flush()
            assert not worker.done() and h.queue._running
            assert len(probes) == 1 and h.store.get(probes[0].id) is None
            assert "realm_action_probe_capacity" in caplog.text
            assert [cmd.id for _, cmd in h.sent] == ["eligible_defaults"]
            if capacity == "roots":
                assert len(h.store.unresolved()) == 128
                h.clock.value = 600; h.queue.wake()
                await flush(); await flush()
                assert not worker.done() and len(probes) == 2
                assert len(h.store.unresolved()) == 128
                assert all(h.store.get(cmd.id) is None for cmd in probes)
            else:
                h.queue.wake(); await flush()
                assert len(probes) == 1  # No hot retry before its600s deadline.
                assert len([r for r in h.store.records() if r.role == "root"]) == 256
            await finish_success(h, defaults)
            assert h.store.get(defaults.id).state == "completed"
            assert not worker.done()
            if capacity == "roots":
                # Matched native evidence persists and can settle a different
                # old operation even while probe admission is deferred/off.
                old = p.RealmMove("old0", 1000, "home")
                h.queue.on_ack(old, Ack(0, old.id, True, ""))
                h.queue.on_realm_job(p.RealmJob(0, old.id, "realm_move", "done", "", old.guid, 0))
                assert h.store.get(old.id).state == "completed"
                assert len(h.store.holds()) == 126
            else:
                assert len([r for r in h.store.records() if r.result_ok is False]) == 255
            assert not worker.done()
        finally:
            await h.finish()
            await worker
    run(scenario())


@pytest.mark.parametrize("state,transport", [
    ("prepared", "not_entered"), ("dispatched", "inflight"),
    ("unknown", "settled"), ("completed", "settled"),
])
def test_recovered_legacy_announce_never_enters_queue_transport_or_body_ledger(harness, state, transport):
    async def scenario():
        h = harness
        payload = document(dict(type="announce", id="legacy", map=631, instance_id=123, boss_id=0,
                                boss_name="Example", attempts=0, fight_s=0, first="week"))
        entered = transport != "not_entered"
        values = dict(command_id="legacy", role="root", policy="ack", owner="realm_day", rank=4,
                      kind="announce", payload=payload, reserved_manifest="[]", guild=50, human=1,
                      state=state, transport_state=transport, created=0.0, updated=1.0,
                      process_id="old" if entered else "", connection_epoch="7" if entered else "0",
                      dispatched=1.0 if entered else None, result_ok=1 if state == "completed" else None,
                      evidence=document({"ack": {"ok": True, "error": ""}}) if state == "completed" else "{}")
        with h.store.conn:
            h.store.conn.execute("INSERT INTO realm_action(" + ",".join(values) + ") VALUES("
                                 + ",".join("?" for _ in values) + ")", tuple(values.values()))
        h.store.recover(process_id="new", now=20)
        old = h.store.get("legacy")
        assert h.store.holds() == () and old.payload == payload and old.native_manifest is None
        await h.cycle(20)
        await h.cycle(30)
        assert h.sent == [] and h.queue._pending == {} and h.queue._entries == {}
        assert h.store.get("legacy") == old
        assert h.bodies.may_move_all("realm_day", (2,))
        # Ordinary work and actual receipt routing continue beside the history.
        h.queue.submit(p.RealmMove("move_after_legacy", 2, "home"), owner="realm_day")
        await h.cycle(31)
        assert [cmd.id for _, cmd in h.sent] == ["move_after_legacy"]
        h.ack("move_after_legacy")
        await h.cycle(32)
        h.queue.on_realm_job(done("move_after_legacy", 2))
        assert h.store.get("move_after_legacy").state == "completed"
        assert h.store.holds() == () and h.store.get("legacy").payload == payload
        await h.finish()
    run(scenario())


def test_queue_rejects_mutated_announce_before_creating_journal_or_transport_entry(harness):
    cmd = p.Announce("announce", "a" * 32 + ":1", 7, 50, 1, 631, 123, 0, 1, 25, False,
                     "Example", first="week")
    object.__setattr__(cmd, "kill_id", "")
    with pytest.raises(ValueError):
        harness.queue.submit(cmd, owner="realm_day")
    assert harness.store.records() == () and harness.queue._pending == {} and harness.sent == []
