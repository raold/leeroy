import asyncio
from collections import deque
from dataclasses import asdict, replace
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from leeroy_mind.guild.config import GuildConfig
from leeroy_mind.guild.loot import _get, _put
from leeroy_mind.guild.offline import OfflineRaids, OWNED_KEY
from leeroy_mind.guild.protocol import BotDo, Job, RaidControl
from leeroy_mind.guild.raid_night import RaidRun
from leeroy_mind.guild.session import RaidPlan, RaidSession
from leeroy_mind.protocol import Ack, Boss, ProtocolError, encode_command, parse_event
from leeroy_mind.training import protocol, store as ts
from leeroy_mind.training.applier import Applier, _encode
from leeroy_mind.training.cards import Directive
from leeroy_mind.training.config import TrainingConfig
from leeroy_mind.training.native_cleanup import NativeRun, NativeCleanup, PREFIX, PROOF_PREFIX, DONE_PREFIX, HINT_KEY
from leeroy_mind.training.protocol import CustomStrategy, DcSetting, GetTrainingCleanup, TrainingCleanup
from leeroy_mind.training.ports import LivePorts
from leeroy_mind.training.service import TrainingGround

from guild.fakes import FakeArmory, FakePorts, member, persona
from .test_tg_applier import Fake as ApplierPorts, MEMBERS, RUN, card, vocabulary
from .test_tg_gate import _run_async


def receipt(*, ident="query_1", state="open", **changes):
    fields = dict(ts=1, id=ident, receipt_version=1, training_run="tr_process_a_7", job_id="original_job",
        training_token="7", leader_guid=101, guild_id=10, human_guid=1, map_id=631,
        instance_id=77, group_id="18446744073709551614", members=tuple(MEMBERS), state=state)
    fields.update(changes)
    return TrainingCleanup(**fields)


@pytest.fixture
def native(store, clock):
    ports = ApplierPorts(store, clock)
    binding = NativeRun.bind(RUN, ports.state.session_key, receipt())
    ports.state = replace(ports.state, native_run=binding, native_required=True)
    with ports.conn:
        _put(ports.conn, PREFIX + RUN, binding.saved())
    original = ports.wait_job
    async def wait(ident, timeout_s):
        job = await original(ident, timeout_s)
        return replace(job, training_run=ports.jobs[ident].training_run) if job is not None else None
    ports.wait_job = wait
    return ports, binding


def test_native_wire_roundtrip_keeps_lossless_scope_and_legacy_defaults():
    protocol.register()
    event = receipt(training_token="18446744073709551615")
    decoded = parse_event(json.dumps(dict(v=1, type="training_cleanup", **asdict(event))))
    assert decoded == event
    assert json.loads(encode_command(GetTrainingCleanup("q1", event.training_run), 0))["training_run"] == event.training_run
    assert BotDo("b", (101,), "restore", "*").training_run == ""
    assert RaidControl("c", 101, "dc_pause").training_run == ""
    assert DcSetting("d", 101, "clear").training_run == ""
    unknown = receipt(state="unknown", job_id="", training_token="0", group_id="0", members=(),
                      leader_guid=0, guild_id=0, human_guid=0, map_id=0, instance_id=0)
    assert parse_event(json.dumps(dict(v=1, type="training_cleanup", **asdict(unknown)))) == unknown


@pytest.mark.parametrize("field,value", [
    ("receipt_version", True), ("receipt_version", 2), ("members", [101, 101]),
    ("members", [101, 1]), ("members", [True, 102]), ("members", list(range(101, 142))),
    ("instance_id", -1), ("leader_guid", 999), ("training_token", "01"),
    ("group_id", "18446744073709551616"), ("training_run", "bad:handle"),
    ("training_run", "a" * 129), ("state", "unknown"), ("job_id", ""),
])
def test_native_wire_rejects_malformed_or_inconsistent_proof(field, value):
    protocol.register()
    raw = dict(v=1, type="training_cleanup", **asdict(receipt()))
    raw[field] = value
    with pytest.raises(ProtocolError):
        parse_event(json.dumps(raw))


@_run_async
async def test_real_response_mailbox_preserves_proof_before_ack_and_remaps_unique_queries(store, clock):
    protocol.register()
    entered, release = asyncio.Event(), asyncio.Event()
    wire_ids = []
    async def send(command):
        wire_ids.append(command.id)
        assert isinstance(command, GetTrainingCleanup)
        # The actual query event can arrive before its bridge ACK. A stale
        # previous request ID must not satisfy this fresh reserved stream.
        if len(wire_ids) > 1:
            ports.feed(receipt(ident=wire_ids[0], state="complete"))
        ports.feed(receipt(ident=command.id, state="complete"))
        entered.set()
        await release.wait()
        return Ack(0, command.id, True, "")
    guild = SimpleNamespace(send=send)
    app = SimpleNamespace(clock=clock, store=store, guild=SimpleNamespace(ports=guild))
    ports = LivePorts(app, cfg=TrainingConfig(enabled=True))
    coordinator = NativeCleanup(ports, SimpleNamespace())
    query = asyncio.create_task(coordinator._query("tr_process_a_7"))
    await entered.wait()
    assert not query.done()
    release.set()
    proof = await query
    assert proof.state == "complete" and proof.id not in wire_ids
    second = await coordinator._query("tr_process_a_7")
    assert second.state == "complete" and wire_ids[0] != wire_ids[1] and second.id != proof.id
    await ports.disconnect()


@_run_async
async def test_negative_ack_cannot_turn_early_cleanup_event_into_proof(store, clock):
    protocol.register()
    async def send(command):
        ports.feed(receipt(ident=command.id, state="complete"))
        return Ack(0, command.id, False, "disabled")
    app = SimpleNamespace(clock=clock, store=store, guild=SimpleNamespace(ports=SimpleNamespace(send=send)))
    ports = LivePorts(app, cfg=TrainingConfig(enabled=True))
    assert await NativeCleanup(ports, SimpleNamespace())._query("tr_process_a_7") is None
    await ports.disconnect()


@_run_async
async def test_offline_mutation_requires_saved_binding_and_stamps_every_inverse(native):
    ports, binding = native
    ports.conn.execute("DELETE FROM meta WHERE key=?", (PREFIX + RUN,))
    assert not await ports.apply(card(Directive("threat", "+")), members={102: MEMBERS[102]})
    assert not ports.sent
    with ports.conn:
        _put(ports.conn, PREFIX + RUN, binding.saved())
    assert await ports.applier.hold(101)
    assert await ports.apply(card(Directive("threat", "+")), members={102: MEMBERS[102]})
    assert await ports.applier.resume_held(101)
    for row in ports.open():
        saved = json.loads(row["command"])
        assert saved["scope"]["native"] == binding.saved()
        assert saved["wire"]["data"]["training_run"] == binding.training_run
        inverse = json.loads(row["inverse"])
        if inverse:
            assert inverse["data"]["training_run"] == binding.training_run
    await ports.applier.end_run(RUN)
    assert not ports.applier.cleanup_pending(RUN)
    assert all(c.training_run == binding.training_run for c in ports.sent)


@_run_async
async def test_missing_native_binding_blocks_hold_apply_and_resume(native):
    ports, _ = native
    ports.state = replace(ports.state, native_run=None)
    assert not await ports.applier.hold(101)
    assert not await ports.apply(card(Directive("threat", "+")))
    assert not await ports.applier.resume_held(101)
    assert not ports.sent and not ports.open()


@_run_async
async def test_complete_proof_settles_lost_terminal_without_old_location_or_owner(native):
    ports, binding = native
    ports.fail_job["+threat"] = "missing"
    assert not await ports.apply(card(Directive("threat", "+")), members={102: MEMBERS[102]})
    assert ports.open() and not json.loads(ports.open()[0]["command"])["settled"]
    ports.state = None
    ports.holders[102] = "new_owner"
    sent = len(ports.sent)
    assert await ports.applier.settle_native(binding, receipt(state="complete"))
    assert not ports.open() and not ports.applier.cleanup_pending(RUN)
    assert len(ports.sent) == sent and ports.holders[102] == "new_owner" and not ports.released
    assert _get(ports.conn, PROOF_PREFIX + binding.training_run)["consumed"]


@pytest.mark.parametrize("changes", [
    {"job_id": "other_job"}, {"training_run": "tr_process_b_7"}, {"training_token": "8"},
    {"group_id": "9"}, {"map_id": 649}, {"instance_id": 78}, {"guild_id": 11},
    {"human_guid": 2}, {"members": (101, 102)}, {"state": "restoring"},
])
@_run_async
async def test_mismatched_or_partial_native_proof_cannot_settle(native, changes):
    ports, binding = native
    assert await ports.applier.hold(101)
    before = [dict(r) for r in ports.open()]
    assert not await ports.applier.settle_native(binding, receipt(**({"state": "complete"} | changes)))
    assert [dict(r) for r in ports.open()] == before
    assert not ports.applier.native_fenced(binding)


@_run_async
async def test_proof_excludes_cache_legacy_foreign_and_uncovered_journals(native):
    ports, binding = native
    assert await ports.applier.hold(101)
    template = json.loads(ports.open()[0]["command"])
    cases = [
        (RUN, CustomStrategy("cache", "tg_test", 8, ("frostbolt",), 102), None),
        (RUN, BotDo("legacy", (102,), "co", "+threat"), "legacy"),
        ("other_run", BotDo("foreign", (102,), "co", "+threat", training_run=binding.training_run), None),
        (RUN, BotDo("outside", (999,), "co", "+threat", training_run=binding.training_run), None),
    ]
    ids = []
    for key, cmd, kind in cases:
        saved = json.loads(json.dumps(template))
        saved["wire"] = _encode(cmd)
        if kind == "legacy":
            saved["scope"].pop("native")
        ids.append(ts.log_applied(ports.conn, key, "standing", "0:0:0", saved, _encode(cmd), ports.clock.now()))
    assert not await ports.applier.settle_native(binding, receipt(state="complete"))
    assert {row["id"] for row in ports.open()} == set(ids)
    assert ports.applier.cleanup_pending(RUN)


@_run_async
async def test_legacy_inverse_does_not_borrow_new_native_binding(native):
    ports, binding = native
    assert await ports.applier.hold(101)
    row = ports.open()[0]
    envelope = json.loads(row["command"])
    envelope["scope"].pop("native")
    envelope["wire"]["data"]["training_run"] = ""
    inverse = json.loads(row["inverse"])
    inverse["data"]["training_run"] = ""
    ports.conn.execute("UPDATE tg_applied SET command=?,inverse=? WHERE id=?",
                       (json.dumps(envelope), json.dumps(inverse), row["id"]))
    sent = len(ports.sent)
    await ports.applier.end_run(RUN)
    assert len(ports.sent) == sent and ports.open()


@_run_async
async def test_completion_never_releases_a_reused_local_owner_claim(native):
    ports, binding = native
    assert await ports.applier.hold(101)
    ports.applier._claims[RUN] = {102}
    ports.holders[102] = "training"
    assert not await ports.applier.settle_native(binding, receipt(state="complete"))
    assert not ports.open() and not ports.released
    assert ports.applier._claims[RUN] == {102} and ports.holders[102] == "training"


@_run_async
async def test_complete_fences_inflight_ack_and_retains_owned_task_until_join(native, monkeypatch):
    import leeroy_mind.training.applier as module
    monkeypatch.setattr(module, "_WAIT_S", .02)
    ports, binding = native
    entered, release = asyncio.Event(), asyncio.Event()
    async def slow_ack(command):
        entered.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                pass
    ports.hook = slow_ack
    applying = asyncio.create_task(ports.apply(card(Directive("threat", "+")), members={102: MEMBERS[102]}))
    try:
        await entered.wait()
        assert not await ports.applier.settle_native(binding, receipt(state="complete"))
        assert ports.applier.native_fenced(binding) and ports.applier.cleanup_pending(RUN)
        assert _get(ports.conn, PROOF_PREFIX + binding.training_run)["consumed"] is False
        assert not await ports.applier.resume_held(101)
        release.set()
        assert not await applying
        await asyncio.gather(*(r.task for r in ports.applier._receipts.values() if r.task is not None))
        assert await ports.applier.settle_native(binding, receipt(state="complete"))
        assert len(ports.sent) == 1 and not ports.applier._entries and not ports.open()
    finally:
        release.set()
        await applying


@_run_async
async def test_saved_complete_proof_reconciles_after_restart_without_resending(native):
    ports, binding = native
    assert await ports.applier.hold(101)
    with ports.conn:
        _put(ports.conn, PROOF_PREFIX + binding.training_run, dict(receipt=asdict(receipt(state="complete")), consumed=False))
    ports.state = None
    restarted = Applier(ports, ports, state_provider=ports.snapshot)
    ground = SimpleNamespace(applier=restarted, app=SimpleNamespace())
    coordinator = NativeCleanup(ports, ground)
    ports.request = AsyncMock(side_effect=AssertionError("saved proof must not require wire replay"))
    sent = len(ports.sent)
    await coordinator.tick()
    assert not ports.open() and not restarted.cleanup_pending(RUN)
    assert len(ports.sent) == sent
    assert _get(ports.conn, DONE_PREFIX + RUN)["training_run"] == binding.training_run
    # Resetting a scan cursor after restart must skip completed history.
    next_binding = replace(binding, run_key="raid:2", session_key="session:2", training_run="tr_process_a_8",
                           job_id="next_job", training_token="8")
    with ports.conn:
        _put(ports.conn, PREFIX + next_binding.run_key, next_binding.saved())
    coordinator._cursor = ""
    async def query(command, *args, **kwargs):
        assert command.training_run == next_binding.training_run
        return [receipt(ident=command.id, state="complete", training_run=next_binding.training_run,
                        job_id=next_binding.job_id, training_token="8")]
    ports.request = AsyncMock(side_effect=query)
    await coordinator.tick()
    ports.request.assert_awaited_once()


@pytest.fixture
def offline(tmp_path, clock):
    ports = FakePorts(tmp_path, clock)
    ports.cfg, ports.connection_epoch, ports.online = TrainingConfig(enabled=True), 1, False
    ports.holder = lambda guid: None
    ports.feed = lambda ev: None
    for guid in range(101, 109):
        ports.members[guid] = member(guid, f"Member{guid}", 1 if guid == 101 else 8, 2, map=631)
        ports.personas[guid] = persona(guid, f"Member{guid}")
    plan = RaidPlan("icc", 10, False, True, 101, 101, 103, list(ports.members), [], False)
    session = asyncio.run(RaidSession.open(ports, GuildConfig(), FakeArmory({}, {}), plan))
    session.bind_instance(77)
    run = RaidRun(session.plan, session, False, False)
    controller = OfflineRaids.__new__(OfflineRaids)
    controller.ports, controller.cfg = ports, GuildConfig()
    controller.run, controller.session = run, session
    controller._online, controller._lease = False, False
    controller._inputs, controller._votes = deque(), []
    controller._owned = dict(id="original_job", phase="running", cancelled=False,
        terminal=None, sent_at=clock.now()-1, accepted=list(plan.members), actual_plan=asdict(plan),
        plan=asdict(plan), instance=77, raid_id=session.raid_id, started=True, running_seen=True)
    controller._save()
    with ports.conn:
        ports.conn.execute("INSERT INTO meta(key,value) VALUES ('guild_id','10')")
    app = SimpleNamespace(guild=SimpleNamespace(offline=controller))
    ground = TrainingGround(app, ports)
    ground._connected = ground._presence_known = ground._startup_ready = True
    ground.vocab, ground.gate._phase = vocabulary(), "passed"
    ground.ledger.first_pull_ts = lambda: clock.now()-28*86400
    controller._listeners = [ground.listener]
    protocol.register()
    try:
        yield ports, run, controller, ground
    finally:
        ports.close()


def progress(ports, run, **changes):
    return replace(Job(int(ports.clock.now()*1000), "original_job", "offline_raid", "progress", "", "running",
                       tuple(run.plan.members), (), 0, 0, 77, 0, 0, 0, "tr_process_a_7"), **changes)


@_run_async
async def test_delayed_native_hint_and_query_retry_before_single_startup_hold(offline):
    ports, run, controller, ground = offline
    event = receipt(members=tuple(run.plan.members))
    queries = 0
    async def query(command, cls, *, until, timeout_s):
        nonlocal queries
        queries += 1
        return [] if queries == 1 else [replace(event, id=command.id)]
    ports.request = query
    original = ports.send
    async def send(command):
        binding = _get(ports.conn, PREFIX + ground.listener.run_key(run))
        assert binding and command.training_run == binding["training_run"]
        return await original(command)
    ports.send = send
    await ground.listener.on_start(run)
    assert not ground.listener._start_ready and not ports.sent
    ground.native.feed(progress(ports, run))
    await ports.clock.advance(5, step=5)
    await ground.listener.tick()
    assert queries == 1 and not ports.sent and not ground.listener._start_attempted
    await ports.clock.advance(5, step=5)
    await ground.listener.tick()
    assert ground.listener._start_ready and queries == 2
    assert len(ports.sent) == 1 and isinstance(ports.sent[0], DcSetting)
    await ground.listener.tick()
    assert len(ports.sent) == 1


@_run_async
async def test_real_offline_terminal_barrier_waits_for_correlated_native_complete(offline):
    ports, run, controller, ground = offline
    phase = "open"
    async def query(command, cls, *, until, timeout_s):
        return [receipt(ident=command.id, members=tuple(run.plan.members), state=phase)]
    ports.request = query
    ground.native.feed(progress(ports, run))
    await ground.listener.on_start(run)
    assert ground.listener._start_ready and ts.open_applied(ports.conn, 0)
    terminal = progress(ports, run, state="failed", reason="cleanup_failed", stage="tearing_down")
    await controller.on_job(terminal)
    ground.native.feed(terminal)
    assert ground.listener.run is None and controller.run is run
    assert not await ground.listener.on_before_end(run, "cleanup_failed")
    assert not run.session._closing
    phase = "restoring"
    await ground.native.tick()
    assert not await ground.listener.on_before_end(run, "cleanup_failed")
    phase = "complete"
    await ground.native.tick()  # cursor wrap is deliberately bounded.
    await ground.native.tick()
    assert await ground.listener.on_before_end(run, "cleanup_failed")
    assert not ts.open_applied(ports.conn, 0)
    assert len(ports.sent) == 1  # No late restore against returned bodies.


@_run_async
async def test_foreign_jobs_conflicting_handles_and_changed_scope_never_bind(offline):
    ports, run, controller, ground = offline
    ground.listener.run = run
    ports.request = AsyncMock(side_effect=lambda command, *a, **k: [receipt(ident=command.id, members=tuple(run.plan.members))])
    ground.native.feed(progress(ports, run, id="another_job"))
    assert not await ground.native.prepare(run)
    ports.request.assert_not_awaited()
    ground.native.feed(progress(ports, run))
    assert await ground.native.prepare(run)
    binding = ground.native.for_run(run)
    ground.native.feed(progress(ports, run, training_run="tr_process_b_7"))
    assert ground.native.for_run(run) is None and ground.applier.native_fenced(binding)
    assert not await ground.native.prepare(run)


@_run_async
async def test_boss_engage_queued_during_proof_wait_prevents_startup_mutation(offline):
    ports, run, controller, ground = offline
    async def query(command, cls, *, until, timeout_s):
        await controller.on_boss(Boss(int(ports.clock.now()*1000), 631, 77, 0, "", "engaged", tuple(run.plan.members)))
        return [receipt(ident=command.id, members=tuple(run.plan.members))]
    ports.request = query
    ground.native.feed(progress(ports, run))
    await ground.listener.on_start(run)
    assert not ground.listener._start_attempted and not ports.sent
    assert not _get(ports.conn, PREFIX + ground.listener.run_key(run))


@_run_async
async def test_malformed_existing_binding_cannot_be_overwritten_by_new_open_proof(offline):
    ports, run, controller, ground = offline
    ground.listener.run = run
    ground.native.feed(progress(ports, run))
    key = PREFIX + ground.listener.run_key(run)
    malformed = dict(training_run="tr_original_1", members="lost manifest")
    with ports.conn:
        _put(ports.conn, key, malformed)
    ports.request = AsyncMock(side_effect=AssertionError("immutable invalid identity must remain blocked"))
    assert not await ground.native.prepare(run)
    assert _get(ports.conn, key) == malformed
    ports.request.assert_not_awaited()


@pytest.mark.parametrize("change,flag", [
    ({"training_run": "tr_process_b_7"}, "poisoned"),
    ({"stage": "tearing_down"}, "sealed"),
])
@_run_async
async def test_first_open_query_cannot_outlive_conflicting_or_sealed_hint(offline, change, flag):
    ports, run, controller, ground = offline
    entered, release = asyncio.Event(), asyncio.Event()
    queries = []
    async def query(command, cls, *, until, timeout_s):
        queries.append(command)
        entered.set()
        await release.wait()
        return [receipt(ident=command.id, members=tuple(run.plan.members))]
    ports.request = query
    ground.native.feed(progress(ports, run))
    starting = asyncio.create_task(ground.listener.on_start(run))
    try:
        await entered.wait()
        assert ground.native._open == {}
        original_owned = _get(ports.conn, OWNED_KEY)
        ground.native.feed(progress(ports, run, **change))
        assert _get(ports.conn, OWNED_KEY) == original_owned
        release.set()
        await starting
        assert _get(ports.conn, HINT_KEY)[flag] is True
        assert not ports.sent and not ground.listener._start_attempted
        assert not _get(ports.conn, PREFIX + ground.listener.run_key(run))
        # A delayed old progress event can never reopen the hint after either
        # poison or teardown, even though no binding existed at that boundary.
        ground.native.feed(progress(ports, run))
        assert _get(ports.conn, HINT_KEY)[flag] is True
        await ports.clock.advance(5, step=5)
        await ground.listener.tick()
        assert len(queries) == 1 and not ports.sent
    finally:
        release.set()
        await starting


@_run_async
async def test_cancelled_owned_run_cannot_dispatch_queued_application_but_proof_still_reconciles(offline):
    ports, run, controller, ground = offline
    phase = "open"
    async def query(command, cls, *, until, timeout_s):
        return [receipt(ident=command.id, members=tuple(run.plan.members), state=phase)]
    ports.request = query
    ground.listener.run = run
    ground.native.feed(progress(ports, run))
    assert await ground.native.prepare(run)
    binding = ground.native.for_run(run)
    entered, release = asyncio.Event(), asyncio.Event()
    original = ground.applier._perform
    async def queued(receipt_, command):
        entered.set()
        await release.wait()
        await original(receipt_, command)
    ground.applier._perform = queued
    key = ground.listener.run_key(run)
    members = ground.members_for(run)
    applying = asyncio.create_task(ground.applier.apply(key, "boss", "631:0:0",
        card(Directive("threat", "+")), {102: members[102]}, tank_guid=101, lead_is_human=False))
    try:
        await entered.wait()
        assert ts.open_applied(ports.conn, 0) and not ports.sent
        controller._cancel()  # Actual deadline/queue/login cancellation path.
        assert ground.listener.retained_run(key) is run and ground.listener.run is None
        assert ground.native.for_run(run) is None
        assert ground.application_allowed()  # Global readiness alone is insufficient.
        release.set()
        assert not await applying
        assert not ports.sent
        assert all(r.reason == "unsent" for r in ground.applier._receipts.values())
        phase = "complete"
        await ground.native.tick()
        assert _get(ports.conn, PROOF_PREFIX + binding.training_run)["consumed"] is True
        assert await ground.listener.on_before_end(run)
    finally:
        release.set()
        await applying


@_run_async
async def test_native_dispatch_binding_must_still_belong_to_the_original_start_command(offline):
    ports, run, controller, ground = offline
    ground.listener.run = run
    ports.request = AsyncMock(side_effect=lambda command, *a, **k:
        [receipt(ident=command.id, members=tuple(run.plan.members))])
    ground.native.feed(progress(ports, run))
    assert await ground.native.prepare(run)
    assert ground.native.for_run(run) is not None
    controller._owned["id"] = "replacement_job"
    controller._save()
    assert ground.native.for_run(run) is None


@pytest.mark.parametrize("boundary", ["native", "hold"])
@_run_async
async def test_consumed_first_engage_fences_native_only_startup(offline, monkeypatch, boundary):
    from .test_tg_gate import boss
    ports, run, controller, ground = offline
    entered, release = asyncio.Event(), asyncio.Event()
    holds = []
    async def query(command, *args, **kwargs):
        if boundary == "native":
            entered.set()
            await release.wait()
        return [receipt(ident=command.id, members=tuple(run.plan.members))]
    async def hold(guid):
        holds.append(guid)
        if boundary == "hold":
            entered.set()
            await release.wait()
        return True
    ports.request = query
    ground.native.feed(progress(ports, run))
    monkeypatch.setattr(ground.applier, "hold", hold)
    standing = AsyncMock()
    monkeypatch.setattr(ground.applier, "apply_standing", standing)
    starting = asyncio.create_task(controller._notify("on_start", run))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await controller.on_boss(boss(ports, instance_id=77))
        await asyncio.wait_for(controller._drain(), 1)
        assert not controller._inputs and ground.listener._pull
        release.set()
        await asyncio.wait_for(starting, 1)
        assert holds == ([] if boundary == "native" else [101])
        standing.assert_not_awaited()
        assert not ground.listener._start_ready
    finally:
        release.set()
        await starting


@_run_async
async def test_reconnect_queries_detached_receipts_and_unknown_is_not_reset_proof(offline):
    ports, run, controller, ground = offline
    phase = "open"
    async def query(command, cls, *, until, timeout_s):
        if phase == "unknown":
            return [receipt(ident=command.id, state="unknown", job_id="", training_token="0", group_id="0",
                members=(), leader_guid=0, guild_id=0, human_guid=0, map_id=0, instance_id=0)]
        return [receipt(ident=command.id, state=phase, members=tuple(run.plan.members))]
    ports.request = query
    ground.native.feed(progress(ports, run))
    await ground.listener.on_start(run)
    ground.listener.invalidate(run)
    ground.native.disconnect()
    ports.connection_epoch += 1
    phase = "unknown"
    await ground.native.tick()
    assert ts.open_applied(ports.conn, 0)
    phase = "complete"
    await ground.native.tick()
    await ground.native.tick()
    assert not ts.open_applied(ports.conn, 0)
    assert await ground.listener.on_before_end(run)
