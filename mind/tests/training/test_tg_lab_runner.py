"""Owned lab jobs use synthetic receipts, never a bridge, model or game DB."""

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from leeroy_mind.guild.loot import _get, _put
from leeroy_mind.guild.protocol import Job, JobSkip
from leeroy_mind.protocol import Member
from leeroy_mind.training import lab_runner as module, store
from leeroy_mind.training.lab import Candidate
from leeroy_mind.training.lab_runner import (ATTEMPT_PREFIX, LabRunner, LabSchedule,
    OWNED_KEY, SUMMARY_PREFIX, guild_schedule, next_window)
from leeroy_mind.training.protocol import CustomStrategy, DummyResult, DummyTrial
from leeroy_mind.training.vocab import ClassVocab, Vocab


@pytest.fixture
def lab(fake, monkeypatch):
    fake.cfg = replace(fake.cfg, enabled=True, lab=True, lab_window=("00:00", "23:59"), lab_bots=4)
    for guid in range(101, 105):
        fake.members[guid] = Member(guid, "Tester", 1, 8, 0, 80, 0, True, 0, 0, 1)
    cv = ClassVocab(frozenset({"damage"}), frozenset({"bolt"}), frozenset({"bolt"}),
                    True, frozenset({"often"}), 8)
    snapshot = Vocab({8: cv}, dict.fromkeys(fake.members, cv), {}, frozenset(), fake.clock.now(),
                     True, dict.fromkeys(fake.members, (8, 1)))
    refresh = AsyncMock(return_value=snapshot)
    monkeypatch.setattr(module, "refresh", refresh)
    candidates = [Candidate("tg_8_1_" + letter * 16, 8, 1, (f"often>bolt!{rank}",), None, "synthetic")
                  for letter, rank in (("a", 8), ("b", 7))]
    monkeypatch.setattr(module, "propose", lambda *a, **kw: candidates)
    applier = SimpleNamespace(apply_standing=AsyncMock(return_value=0))
    schedule = lambda: LabSchedule(fake.clock.now(), fake.clock.now() - 3600, False, False, None, None)
    runner = LabRunner(fake, fake.cfg, SimpleNamespace(book=lambda: object()), applier, schedule=schedule)
    return runner, refresh, applier


def terminal(fake, command, *, skipped=(), reason="", state="done", **changes):
    dropped = {s.guid for s in skipped}
    return replace(Job(int(fake.clock.now() * 1000), command["id"], "dummy_trial", state,
        reason, "", tuple(g for g in command["guids"] if g not in dropped), skipped,
        0, 0, 0, 0, 0, 0), **changes)


async def complete(fake, runner, *, changes=None, skips=(), reason="", state="done", omit=()):
    current = runner._owned["current"]
    await fake.clock.advance(current["seconds"] + 1, step=current["seconds"] + 1)
    rate = (2200 if current["candidate_name"].endswith("a" * 16) else 2100) if current["candidate_name"] else 2000
    skipmap = {s.guid: s.reason for s in skips}
    for guid in current["guids"]:
        if guid in omit:
            continue
        event = DummyResult(int(fake.clock.now() * 1000), current["id"], guid,
            rate * current["seconds"], current["seconds"] * 1000, 100, skipmap.get(guid, ""),
            "requested_pre_ai", "best_effort_thread_pair")
        if changes:
            event = replace(event, **changes)
        await runner.on_result(event)
    await runner.on_job(terminal(fake, current, skipped=skips, reason=reason, state=state))
    await runner.tick()


async def start(runner):
    await runner.tick()  # Acquire, probe, prime; no body movement yet.
    await runner.tick()  # Dispatch the first stock leg.
    assert isinstance(runner.ports.sent[-1], DummyTrial)


def test_complete_paired_lab_keeps_results_dummy_and_records_exact_attempts(fake, lab):
    runner, refresh, applier = lab
    async def scenario():
        await start(runner)
        count = 0
        while runner.active and count < 30:
            if runner._owned["current"]:
                await complete(fake, runner)
            else:
                await runner.tick()
            count += 1
        assert not runner.active
    asyncio.run(scenario())
    commands = [cmd for cmd in fake.sent if isinstance(cmd, DummyTrial)]
    assert len(commands) == 6
    assert [bool(cmd.co[0]) for cmd in commands] == [False, True, False, False, True, False]
    assert commands[1].co == ("+custom::tg_8_1_" + "a" * 16,) * 4
    assert len({cmd.id for cmd in commands}) == 6
    assert runner._owned["attempt_ids"] == [cmd.id for cmd in commands]
    assert all(_get(fake.conn, ATTEMPT_PREFIX + ident)["restoration"] == "complete_no_desired_setup"
               for ident in runner._owned["attempt_ids"])
    assert len(store.rotation_rows(fake.conn, status="dummy")) == 2
    assert not store.rotation_rows(fake.conn, status="field")
    assert not store.rotation_rows(fake.conn, status="champion")
    assert fake.conn.execute("SELECT COUNT(*) FROM tg_dummy").fetchone()[0] == 24
    assert fake.conn.execute("SELECT COUNT(*) FROM tg_dummy WHERE rotation IS NOT NULL AND stock_dps=2000").fetchone()[0] == 8
    assert fake.conn.execute("SELECT COUNT(*) FROM tg_trial").fetchone()[0] == 0
    assert not fake.memories and not fake.said and not fake.proposed
    summary = _get(fake.conn, SUMMARY_PREFIX + runner._night())
    assert summary["pairs"] == 4 and summary["winner"].endswith("a" * 16)
    assert summary["metric"] == "dummy_requested_damage_proxy"
    assert summary["field_performance"] == "unavailable"
    assert not runner.members() and not fake.holders
    assert applier.apply_standing.await_count == 7  # Every returned job and the final release gate.
    assert "requested-damage proxy only" in runner.rotation_facts("mage rotation")[0]
    assert runner.rotation_facts("warrior rotation") == []
    assert runner.rotation_facts("where is Dalaran?") == []
    first = _get(fake.conn, ATTEMPT_PREFIX + commands[0].id)
    assert first["id"] == commands[0].id and first["guids"] == list(commands[0].guids)
    assert first["results"]["101"]["damage"] == 120000
    assert first["results"]["101"]["ms"] == 60000
    sent = len(fake.sent)
    asyncio.run(runner.tick())
    assert len(fake.sent) == sent  # One chosen spec per lab night.


@pytest.mark.parametrize("condition", ["online", "short_logout", "missing_logout", "stale",
    "future", "offline_active", "benchmark_active", "offline_due", "benchmark_due",
    "schedule_missing", "schedule_raises", "window", "disabled", "lab_disabled"])
def test_schedule_admission_requires_complete_fresh_offline_proof(fake, lab, condition):
    runner, refresh, _ = lab
    state = runner.schedule()
    if condition == "online":
        fake.online = True
    elif condition == "short_logout":
        state = replace(state, last_logout=fake.clock.now() - 1799)
    elif condition == "missing_logout":
        state = replace(state, last_logout=None)
    elif condition == "stale":
        state = replace(state, observed_at=state.observed_at - 61)
    elif condition == "future":
        state = replace(state, observed_at=state.observed_at + 1)
    elif condition.endswith("_active"):
        state = replace(state, **{condition: True})
    elif condition.endswith("_due"):
        state = replace(state, **{condition + "_at": fake.clock.now() + 3600})
    elif condition == "window":
        runner.cfg = replace(fake.cfg, lab_window=("02:00", "12:00"))
    elif condition == "disabled":
        runner.cfg = replace(fake.cfg, enabled=False)
    elif condition == "lab_disabled":
        runner.cfg = replace(fake.cfg, lab=False)
    runner.schedule = lambda: state
    if condition == "schedule_missing":
        runner.schedule = None
    if condition == "schedule_raises":
        def fail():
            raise ValueError("sensitive scheduler details")
        runner.schedule = fail
    asyncio.run(runner.tick())
    assert not fake.sent and not fake.claims and not runner.members()
    refresh.assert_not_awaited()


def test_native_spec_proof_required_never_roster_substitution(fake, lab):
    runner, refresh, _ = lab
    refresh.return_value = replace(refresh.return_value, probe_specs={})
    asyncio.run(runner.tick())
    assert not fake.sent and not runner.active and not fake.holders
    assert runner.reason == "unproved_probe_spec_or_admission_changed"


def test_away_held_and_desired_standing_bots_never_admitted(fake, lab):
    runner, refresh, _ = lab
    fake.away_set.add(101)
    fake.holders[102] = "offline_raid"
    store.put_setup(fake.conn, dict(guid=103, since=fake.clock.now(), toggles=["+damage"]))
    asyncio.run(runner.tick())
    assert not fake.sent and not fake.claims
    assert fake.holders == {102: "offline_raid"}
    refresh.assert_not_awaited()


def test_claim_precedes_probe_and_every_move_is_owned(fake, lab):
    runner, refresh, _ = lab
    snapshot = refresh.return_value
    async def check_probe(*args, **kwargs):
        assert runner.members() == set(fake.members)
        assert set(fake.holders.values()) == {"training"}
        return snapshot
    refresh.side_effect = check_probe
    asyncio.run(start(runner))
    assert runner.members() == set(fake.members)
    assert fake.claims[0][0] == "training"
    assert not fake.released


def test_results_and_terminal_never_wait_for_ack_inline(fake, lab):
    runner, _, _ = lab
    async def scenario():
        await runner.tick()
        original = fake.send
        pending, release = asyncio.Event(), asyncio.Event()
        async def delayed(command):
            if isinstance(command, DummyTrial):
                pending.set()
                await release.wait()
            return await original(command)
        fake.send = delayed
        task = asyncio.create_task(runner.tick())
        await pending.wait()
        current = runner._owned["current"]
        await fake.clock.advance(61, step=61)
        for guid in current["guids"]:
            event = DummyResult(int(fake.clock.now() * 1000), current["id"], guid,
                120000, 60000, 50, "", "requested_pre_ai", "best_effort_thread_pair")
            await asyncio.wait_for(runner.on_result(event), .1)
        await asyncio.wait_for(runner.on_job(terminal(fake, current)), .1)
        assert not task.done()
        assert fake.conn.execute("SELECT COUNT(*) FROM tg_dummy").fetchone()[0] == 0
        release.set()
        await task
        await runner.tick()
        assert runner._owned["current"] is None
        assert len([c for c in fake.sent if isinstance(c, DummyTrial)]) == 1
    asyncio.run(scenario())


def test_ack_uncertainty_timeout_and_restart_never_retry_or_release(fake, lab):
    runner, _, applier = lab
    async def scenario():
        await runner.tick()
        original = fake.send
        async def fail(command):
            if isinstance(command, DummyTrial):
                await original(command)
                raise TimeoutError
            return await original(command)
        fake.send = fail
        await runner.tick()
        assert runner.reason == "dummy_ack_uncertain"
        ident = runner._owned["current"]["id"]
        await fake.clock.advance(1000, step=1000)
        await runner.tick()
        assert runner.members() == set(fake.members) and not fake.released
        restarted = LabRunner(fake, fake.cfg, runner.ground, applier, schedule=runner.schedule)
        await restarted.tick()
        assert restarted._owned["current"]["id"] == ident
        assert restarted.members() == set(fake.members)
        assert len([c for c in fake.sent if isinstance(c, DummyTrial)]) == 1
    asyncio.run(scenario())


def test_explicit_rejection_releases_only_after_no_setup_restoration_needed(fake, lab):
    runner, _, applier = lab
    fake.acks["dummy_trial"].append((False, "busy"))
    asyncio.run(start(runner))
    assert not runner.active and not fake.holders
    assert runner.reason == "dummy_command_rejected"
    applier.apply_standing.assert_awaited_once()


def test_cleanup_failure_retains_membership_and_claim_even_after_timeout(fake, lab):
    runner, _, applier = lab
    async def scenario():
        await start(runner)
        await complete(fake, runner, skips=(JobSkip(101, "cleanup_failed:return_pending"),),
                       state="failed", reason="cleanup_failed:return_timeout")
        assert runner.reason == "native_cleanup_unproved"
        await fake.clock.advance(10000, step=10000)
        await runner.tick()
        assert runner.members() == set(fake.members)
        assert not fake.released
        assert len([c for c in fake.sent if isinstance(c, DummyTrial)]) == 1
    asyncio.run(scenario())
    applier.apply_standing.assert_not_awaited()


def test_standing_setup_appearing_during_job_blocks_release_even_with_zero_applier(fake, lab):
    runner, _, applier = lab
    async def scenario():
        await start(runner)
        store.put_setup(fake.conn, dict(guid=101, since=fake.clock.now(), toggles=["+damage"]))
        await complete(fake, runner)
        assert runner.reason == "standing_restoration_unavailable"
        assert runner.members() == set(fake.members) and not fake.released
    asyncio.run(scenario())
    applier.apply_standing.assert_not_awaited()


@pytest.mark.parametrize("changes", [{"damage_basis": "unknown"}, {"damage_basis": "actual_health"},
    {"spell_basis": "unknown"}, {"ms": 59999}, {"ms": 0}, {"damage": 0}])
def test_bad_metric_or_elapsed_receipts_record_gaps_and_never_score(fake, lab, changes):
    runner, _, _ = lab
    async def scenario():
        await start(runner)
        await complete(fake, runner, changes=changes)
        assert not runner.active  # Every participant was unscoreable; no invented next leg.
    asyncio.run(scenario())
    rows = fake.conn.execute("SELECT * FROM tg_dummy").fetchall()
    assert len(rows) == 4 and all(row["dropped"] for row in rows)
    summary = _get(fake.conn, SUMMARY_PREFIX + runner._night())
    assert summary["winner"] is None and summary["pairs"] == 0 and summary["gaps"]


def test_result_replay_id_mismatch_and_duplicate_do_not_inflate_attempt(fake, lab):
    runner, _, _ = lab
    async def scenario():
        await start(runner)
        current = runner._owned["current"]
        await fake.clock.advance(61, step=61)
        event = DummyResult(int(fake.clock.now() * 1000), current["id"], 101, 120000,
                            60000, 20, "", "requested_pre_ai", "best_effort_thread_pair")
        await runner.on_result(replace(event, id="unrelated"))
        await runner.on_result(event)
        await runner.on_result(event)
        await runner.tick()
        assert len(current["results"]) == 1
        assert not fake.released and runner.members()
    asyncio.run(scenario())


def test_wrong_terminal_roster_is_not_cleanup_proof(fake, lab):
    runner, _, _ = lab
    async def scenario():
        await start(runner)
        await runner.on_job(terminal(fake, runner._owned["current"], guids=(101, 102, 103, 999)))
        await runner.tick()
        assert runner.reason == "terminal_roster_mismatch"
        assert runner.members() == set(fake.members) and not fake.released
    asyncio.run(scenario())


def test_missing_result_becomes_explicit_gap_after_bounded_reorder_grace(fake, lab):
    runner, _, _ = lab
    async def scenario():
        await start(runner)
        await complete(fake, runner, omit=(101,))
        assert runner._owned["current"] is not None
        await fake.clock.advance(6, step=6)
        await runner.tick()
        row = fake.conn.execute("SELECT * FROM tg_dummy WHERE guid=101").fetchone()
        assert row["dropped"] == "missing_result" and row["damage_basis"] == "unknown"
        await runner.tick()
        assert 101 not in runner._owned["current"]["guids"]
        assert 101 in runner.members()  # Keep excluded bots reserved until final restoration.
    asyncio.run(scenario())


def test_login_mid_job_waits_for_native_return_then_finishes_without_new_command(fake, lab):
    runner, _, _ = lab
    async def scenario():
        await start(runner)
        fake.online = True
        await runner.tick()
        assert runner.members() and not fake.released
        await complete(fake, runner, skips=tuple(JobSkip(g, "human_online") for g in fake.members),
                       reason="human_online", state="failed")
        assert not runner.active
        assert len([c for c in fake.sent if isinstance(c, DummyTrial)]) == 1
    asyncio.run(scenario())


def test_cancelled_dispatch_is_durable_and_does_not_release(fake, lab):
    runner, _, _ = lab
    async def scenario():
        await runner.tick()
        entered = asyncio.Event()
        async def wait(command):
            entered.set()
            await asyncio.Event().wait()
        fake.send = wait
        task = asyncio.create_task(runner.tick())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert runner.reason == "dummy_send_cancelled"
        assert _get(fake.conn, OWNED_KEY)["current"]["id"]
        assert runner.members() == set(fake.members) and not fake.released
    asyncio.run(scenario())


def test_window_adapter_uses_real_guild_logout_and_wraps_midnight(fake):
    from leeroy_mind.guild.config import GuildConfig
    from leeroy_mind.guild.offline import LOGOUT_KEY
    with fake.conn:
        _put(fake.conn, LOGOUT_KEY, fake.clock.now() - 4000)
    snapshot = guild_schedule(fake, GuildConfig(), offline_active=False,
        benchmark_active=False, benchmark_due_at=None)
    assert snapshot.last_logout == fake.clock.now() - 4000
    assert snapshot.offline_due_at > fake.clock.now()
    midnight = datetime(2026, 11, 3, 1, tzinfo=timezone.utc)
    assert next_window(midnight, ("23:00", "03:00")) == midnight.timestamp()
    assert next_window(midnight, ("03:00", "03:00")) is None


def test_every_returned_job_restores_before_next_leg_with_durable_receipt(fake, lab):
    runner, _, applier = lab
    async def scenario():
        await start(runner)
        ident = runner._owned["current"]["id"]
        entered, release = asyncio.Event(), asyncio.Event()
        async def restore(guids):
            assert set(guids) == runner.members()
            entered.set()
            await release.wait()
            return 0
        applier.apply_standing.side_effect = restore
        settling = asyncio.create_task(complete(fake, runner))
        await entered.wait()
        assert _get(fake.conn, ATTEMPT_PREFIX + ident)["restoration"] == "pending"
        assert runner.members() == set(fake.members) and not fake.released
        assert runner._owned["current"]["id"] == ident
        release.set()
        await settling
        assert _get(fake.conn, ATTEMPT_PREFIX + ident)["restoration"] == "complete_no_desired_setup"
        assert runner._owned["current"] is None
        assert runner._owned["leg"] == 1
        assert applier.apply_standing.await_count == 1
    asyncio.run(scenario())


def test_uncertain_restoration_is_not_retried_or_released(fake, lab):
    runner, _, applier = lab
    applier.apply_standing.side_effect = TimeoutError
    async def scenario():
        await start(runner)
        await complete(fake, runner)
        assert runner.reason == "standing_restoration_uncertain"
        ident = runner._owned["current"]["id"]
        assert _get(fake.conn, ATTEMPT_PREFIX + ident)["restoration"] == "pending"
        await runner.tick()
        await runner.tick()
        assert applier.apply_standing.await_count == 1
        assert runner.members() == set(fake.members) and not fake.released
        assert len([c for c in fake.sent if isinstance(c, DummyTrial)]) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("dropped", ["cleanup_failed:return_pending", "offline"])
def test_result_drop_cannot_disagree_with_success_terminal(fake, lab, dropped):
    runner, _, applier = lab
    async def scenario():
        await start(runner)
        await complete(fake, runner, changes={"dropped": dropped})
        assert runner.reason in {"native_cleanup_unproved", "result_terminal_conflicted"}
        assert runner.members() == set(fake.members) and not fake.released
    asyncio.run(scenario())
    applier.apply_standing.assert_not_awaited()


def test_skip_partition_must_match_result_drop(fake, lab):
    runner, _, applier = lab
    async def scenario():
        await start(runner)
        await complete(fake, runner, changes={"dropped": ""}, skips=(JobSkip(101, "offline"),))
        assert runner.reason == "result_terminal_conflicted"
        assert runner.members() == set(fake.members) and not fake.released
    asyncio.run(scenario())
    applier.apply_standing.assert_not_awaited()


def test_terminal_conflict_poison_survives_later_malformed_metrics(fake, lab):
    runner, _, applier = lab
    async def scenario():
        await start(runner)
        current = runner._owned["current"]
        good = terminal(fake, current)
        await runner.on_job(good)
        await runner.on_job(replace(good, state="failed", reason="cleanup_failed:return_pending"))
        await runner.on_result(DummyResult(good.ts, good.id, 101, True, 60000, 1, ""))
        await runner.tick()
        assert set(current["faults"]) == {"conflicting_terminal", "result_shape_invalid"}
        assert runner.reason == "native_terminal_conflicted"
        assert runner.members() == set(fake.members) and not fake.released
    asyncio.run(scenario())
    applier.apply_standing.assert_not_awaited()


def test_preemption_does_not_block_terminal_drain_or_touch_new_holder(fake, lab):
    runner, _, applier = lab
    async def scenario():
        await start(runner)
        fake.holders[101] = "offline_raid"
        fake.online = True  # Complete this session after the proven return.
        await complete(fake, runner)
        assert not runner.active
        assert fake.holders == {101: "offline_raid"}
        assert all(101 not in call.args[0] for call in applier.apply_standing.await_args_list)
        assert all(101 not in guids for owner, guids in fake.released)
        assert runner._owned["preempted"] == [101]
    asyncio.run(scenario())


def test_preempted_body_never_returns_in_later_legs(fake, lab):
    runner, _, _ = lab
    async def scenario():
        await start(runner)
        fake.holders[101] = "offline_raid"
        await complete(fake, runner)
        fake.holders.pop(101)  # New owner's eventual release does not revive old ownership.
        await runner.tick()
        assert 101 not in runner._owned["current"]["guids"]
        assert 101 not in fake.claims[-1][1]
    asyncio.run(scenario())


def test_final_round_evidence_decides_winner_and_keeps_earlier_pairs(fake, lab):
    runner, _, _ = lab
    async def scenario():
        await runner.tick()
        first, second = runner._owned["remaining"]
        runner._owned["round_scores"] = [
            {"round": 0, "scores": {first: [[6000, 2000]] * 4, second: [[2100, 2000]] * 4}},
            {"round": 1, "scores": {first: [[4000, 2000]] * 4, second: [[2200, 2000]] * 4}},
            {"round": 2, "scores": {first: [[2040, 2000]] * 4, second: [[2400, 2000]] * 4}},
        ]
        runner._owned["totals"] = {
            first: [[6000, 2000]] * 4 + [[4000, 2000]] * 4 + [[2040, 2000]] * 4,
            second: [[2100, 2000]] * 4 + [[2200, 2000]] * 4 + [[2400, 2000]] * 4,
        }
        await runner._finish("dummy_rounds_complete")
        summary = _get(fake.conn, SUMMARY_PREFIX + runner._night())
        assert summary["winner"] == second and summary["winner_round"] == 2
        assert summary["pairs"] == 4 and summary["aggregate_pairs"] == 12
        assert len(_get(fake.conn, OWNED_KEY)["round_scores"]) == 3
    asyncio.run(scenario())
