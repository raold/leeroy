"""Owned lab jobs use synthetic receipts, never a bridge, model or game DB."""

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from leeroy_mind.guild.loot import _get, _put
from leeroy_mind.guild.protocol import Job, JobSkip
from leeroy_mind.protocol import Hello, Member
from leeroy_mind.training import lab_runner as module, store
from leeroy_mind.training.lab import Candidate
from leeroy_mind.training.lab_runner import (ATTEMPT_PREFIX, LabRunner, LabSchedule,
    OWNED_KEY, SUMMARY_PREFIX, guild_schedule, next_window)
from leeroy_mind.training.protocol import CustomStrategy, DummyCleanup, DummyResult, DummyTrial
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
    runner.feed(Hello(1, "mod-leeroy", "0.1.0", 21, 0, features=(module.DUMMY_FEATURE,)))
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


def test_rejected_dummy_ack_keeps_natives_error_in_attempt_and_summary(fake, lab, caplog):
    runner, _, _ = lab
    fake.acks["dummy_trial"].append((False, "instance_bound:101"))
    with caplog.at_level("INFO", logger="leeroy_mind.training.lab_runner"):
        asyncio.run(start(runner))
    ident = fake.sent[-1].id
    attempt = _get(fake.conn, ATTEMPT_PREFIX + ident)
    assert attempt["ack"] == "rejected" and attempt["rejected"] is True
    assert attempt["ack_error"] == "instance_bound:101"
    summary = _get(fake.conn, SUMMARY_PREFIX + runner._night())
    assert summary["reason"] == "dummy_command_rejected" and summary["ack_error"] == "instance_bound:101"
    assert f"training_lab_dummy_rejected id={ident} error=instance_bound:101" in caplog.text
    assert "driver_retry" not in caplog.text


def test_driver_pending_gets_one_retry_of_the_same_leg_a_minute_later(fake, lab, caplog):
    runner, _, _ = lab
    fake.acks["dummy_trial"].append((False, "driver_pending"))
    with caplog.at_level("INFO", logger="leeroy_mind.training.lab_runner"):
        asyncio.run(start(runner))
    first = fake.sent[-1]
    assert f"training_lab_driver_retry id={first.id} in_s=60" in caplog.text
    assert runner.active and runner._owned["phase"] == "ready" and runner._owned["current"] is None
    assert runner.members() == set(fake.members) and not fake.released  # Bodies stay held meanwhile.
    asyncio.run(fake.clock.advance(59, step=59))
    asyncio.run(runner.tick())
    assert [c for c in fake.sent if isinstance(c, DummyTrial)] == [first]
    asyncio.run(fake.clock.advance(1, step=1))
    asyncio.run(runner.tick())
    trials = [c for c in fake.sent if isinstance(c, DummyTrial)]
    assert len(trials) == 2 and trials[1].co == first.co and trials[1].guids == first.guids
    assert runner._owned["current"]["ack"] == "accepted"
    assert runner._owned["attempt_ids"] == [first.id, trials[1].id] and runner._owned["attempts"] == 1
    assert _get(fake.conn, ATTEMPT_PREFIX + first.id)["ack_error"] == "driver_pending"


def test_a_second_driver_pending_ends_the_lab_and_validates(fake, lab):
    runner, _, _ = lab
    fake.acks["dummy_trial"].extend([(False, "driver_pending"), (False, "driver_pending")])
    asyncio.run(start(runner))
    asyncio.run(fake.clock.advance(60, step=60))
    asyncio.run(runner.tick())
    assert not runner.active and runner.reason == "dummy_command_rejected"
    summary = _get(fake.conn, SUMMARY_PREFIX + runner._night())
    assert summary["attempts"] == 2 and summary["ack_error"] == "driver_pending"
    assert len([c for c in fake.sent if isinstance(c, DummyTrial)]) == 2
    assert runner.body_holds() == ()


def test_rejection_log_carries_only_the_errors_leading_code(fake, lab, caplog):
    runner, _, _ = lab
    fake.acks["dummy_trial"].append((False, "driver_unavailable:the harness has no GM " + "x" * 400))
    with caplog.at_level("INFO", logger="leeroy_mind.training.lab_runner"):
        asyncio.run(start(runner))
    attempt = _get(fake.conn, ATTEMPT_PREFIX + fake.sent[-1].id)
    assert len(attempt["ack_error"]) == module.ERROR_LIMIT
    assert "error=driver_unavailable:the\n" in caplog.text + "\n"
    assert "harness" not in caplog.text


def blocked(fake, runner):
    async def scenario():
        await start(runner)
        await complete(fake, runner, skips=(JobSkip(101, "cleanup_failed:ownership_changed"),),
                       state="failed", reason="cleanup_failed:cleanup_pending")
    asyncio.run(scenario())
    assert runner._owned["phase"] == "blocked" and runner._owned["halt"] == "native_cleanup_unproved"
    return runner._owned["current"]["id"]


def test_block_time_is_kept_while_later_reasons_rename_it(fake, lab):
    runner, _, _ = lab
    blocked(fake, runner)
    first = runner._owned["halted_at"]
    asyncio.run(fake.clock.advance(3600, step=3600))
    runner._block("body_claim_incomplete")
    restarted = LabRunner(fake, fake.cfg, runner.ground, runner.applier, schedule=runner.schedule)
    assert restarted._owned["halt"] == "process_restart"
    assert restarted._owned["halted_at"] == first == _get(fake.conn, OWNED_KEY)["halted_at"]


def test_a_record_blocked_before_halted_at_existed_dates_from_its_start(fake, lab):
    runner, _, _ = lab
    blocked(fake, runner)
    owned = _get(fake.conn, OWNED_KEY)
    del owned["halted_at"]
    with fake.conn:
        _put(fake.conn, OWNED_KEY, owned)
    asyncio.run(fake.clock.advance(7200, step=7200))
    restarted = LabRunner(fake, fake.cfg, runner.ground, runner.applier, schedule=runner.schedule)
    assert restarted._owned["halted_at"] == owned["started"]


def test_native_cleanup_receipt_is_kept_only_for_the_labs_own_trial(fake, lab):
    runner, _, _ = lab
    ident = blocked(fake, runner)
    now = int(fake.clock.now() * 1000)
    runner.feed(DummyCleanup(now, "f" * 32, "released", (101,), 600000))  # Not this lab's trial.
    runner.feed(DummyCleanup(now, ident, "pending", (101,), 600000))     # Not a known state.
    assert _get(fake.conn, module.NATIVE_PREFIX + "f" * 32) is None
    assert _get(fake.conn, module.NATIVE_PREFIX + ident) is None
    asyncio.run(runner.tick())
    assert runner._owned["phase"] == "blocked" and runner.members() == set(fake.members)
    runner.feed(DummyCleanup(now, ident, "released", (101,), 600123))
    runner.feed(DummyCleanup(now + 1, ident, "restored", (), 700000))  # The first word stays.
    receipt = _get(fake.conn, module.NATIVE_PREFIX + ident)
    assert receipt["state"] == "released" and receipt["unfinished"] == [101] and receipt["after_ms"] == 600123


def assert_recovered(fake, runner, ident, kind, caplog):
    owned = _get(fake.conn, OWNED_KEY)
    assert owned["phase"] == "complete" and owned["current"] is None and runner._owned == owned
    assert owned["recovered"]["by"] == "native" and owned["recovered"]["evidence"]["kind"] == kind
    assert owned["gaps"] == [ident] and owned["attempts"] == len(owned["attempt_ids"]) == 1
    attempt = _get(fake.conn, ATTEMPT_PREFIX + ident)
    assert attempt["restoration"] == "native_recovered" and attempt["results"]
    summary = _get(fake.conn, SUMMARY_PREFIX + owned["night"])
    assert summary["reason"] == owned["halt"] and summary["recovered"] and summary["winner"] is None
    assert _get(fake.conn, module.CLEARED_PREFIX + owned["id"])["by"] == "native"
    assert not runner.active and runner.members() == set() and runner.body_holds() == ()
    assert not fake.holders and fake.released[-1][0] == "training"
    assert set(fake.released[-1][1]) == {101, 102, 103, 104}
    assert fake.conn.execute("SELECT count(*) FROM tg_dummy").fetchone()[0] == 0  # Never a measurement.
    assert f"training_lab_recovered id={owned['id']} trial={ident} evidence={kind} members=4" in caplog.text
    sent = len(fake.sent)
    asyncio.run(runner.tick())
    assert len(fake.sent) == sent and not runner.active  # The night is over: no new lab, no command.


@pytest.mark.parametrize("state", ["released", "restored"])
def test_natives_cleanup_receipt_frees_a_blocked_lab_on_the_next_tick(fake, lab, caplog, state):
    # 2026-11-23: native released its trial at 07:01 (unfinished=1507) and the lab still held its
    # 8 members until an operator stopped the mind for `training lab-clear`.
    caplog.set_level("INFO")
    runner, _, _ = lab
    ident = blocked(fake, runner)
    assert set(fake.holders) == {101, 102, 103, 104}
    runner.feed(DummyCleanup(int(fake.clock.now() * 1000), ident, state,
                             (101,) if state == "released" else (), 600000))
    assert runner._owned["phase"] == "blocked"  # The receipt alone writes nothing but itself.
    asyncio.run(runner.tick())
    assert_recovered(fake, runner, ident, "dummy_cleanup", caplog)


def test_a_worldserver_started_after_the_trial_frees_a_blocked_lab(fake, lab, caplog):
    caplog.set_level("INFO")
    runner, _, _ = lab
    ident = blocked(fake, runner)
    current = runner._owned["current"]
    runner.feed(Hello(2, "mod-leeroy", "0.1.0", 21, 0, features=(module.DUMMY_FEATURE,),
                      started_ms=current["sent_ms"] - 1))  # The process that ran it.
    asyncio.run(runner.tick())
    assert runner._owned["phase"] == "blocked" and runner.members() == set(fake.members)
    runner.feed(Hello(3, "mod-leeroy", "0.1.0", 21, 0, features=(module.DUMMY_FEATURE,),
                      started_ms=current["terminal"]["ts"] + 1))
    asyncio.run(runner.tick())
    assert_recovered(fake, runner, ident, "native_restart", caplog)


def test_a_restarted_mind_frees_a_lab_whose_receipt_came_before_it_stopped(fake, lab, caplog):
    caplog.set_level("INFO")
    runner, _, _ = lab
    ident = blocked(fake, runner)
    runner.feed(DummyCleanup(int(fake.clock.now() * 1000), ident, "released", (101,), 600000))
    restarted = LabRunner(fake, fake.cfg, runner.ground, runner.applier, schedule=runner.schedule)
    assert restarted._owned["phase"] == "blocked" and restarted._owned["halt"] == "process_restart"
    asyncio.run(restarted.tick())
    assert_recovered(fake, restarted, ident, "dummy_cleanup", caplog)


def test_a_standing_setup_keeps_the_lab_blocked_and_is_said_once(fake, lab, caplog):
    caplog.set_level("INFO")
    runner, _, _ = lab
    ident = blocked(fake, runner)
    runner.feed(DummyCleanup(int(fake.clock.now() * 1000), ident, "released", (101,), 600000))
    store.put_setup(fake.conn, dict(guid=103, since=fake.clock.now(), toggles=["+damage"]))
    before = dict(fake.conn.execute("SELECT key,value FROM meta WHERE key GLOB 'tg:lab:*'").fetchall())
    asyncio.run(runner.tick())
    asyncio.run(runner.tick())
    assert runner._owned["phase"] == "blocked" and runner.members() == set(fake.members)
    after = dict(fake.conn.execute("SELECT key,value FROM meta WHERE key GLOB 'tg:lab:*'").fetchall())
    assert after.keys() == before.keys() and _get(fake.conn, SUMMARY_PREFIX + runner._night()) is None
    assert caplog.text.count("training_lab_recovery_refused reason=standing_setup members=1\n") == 1


def test_hello_records_the_worldserver_start_once_per_process(fake, lab):
    runner, _, _ = lab
    hello = Hello(1, "mod-leeroy", "0.1.0", 21, 0)
    runner.feed(hello)
    assert _get(fake.conn, module.NATIVE_START_KEY) is None  # An older native names no process.
    runner.feed(replace(hello, started_ms=1794300000000))
    stored = _get(fake.conn, module.NATIVE_START_KEY)
    assert stored["started_ms"] == 1794300000000
    runner.feed(replace(hello, ts=5, started_ms=1794300000000))
    assert _get(fake.conn, module.NATIVE_START_KEY) == stored
    runner.feed(replace(hello, started_ms=1794400000000))
    assert _get(fake.conn, module.NATIVE_START_KEY)["started_ms"] == 1794400000000


def test_lab_starts_only_on_a_native_that_forms_the_party_at_the_dummy(fake, lab, caplog):
    # An older native forms each bot's solo party before the teleport, and the core disbands a one-member
    # group as its bot lands on another map: every trial it accepted failed ownership_changed (2026-11-10..14).
    runner, refresh, _ = lab
    fresh = LabRunner(fake, fake.cfg, runner.ground, runner.applier, schedule=runner.schedule)
    asyncio.run(fresh.tick())  # No hello yet: nothing is claimed, probed or sent.
    assert fresh.reason == "native_dummy_unsupported" and not fresh.active and not fake.sent
    old = Hello(2, "mod-leeroy", "0.1.0", 21, 0, features=("item_links", "regear"), started_ms=1794300000000)
    fresh.feed(old)
    asyncio.run(fresh.tick())
    asyncio.run(fresh.tick())
    assert fresh.reason == "native_dummy_unsupported" and not fresh.active
    assert not fake.sent and not refresh.await_count and _get(fake.conn, OWNED_KEY) is None
    assert caplog.text.count("training_lab_native_unsupported missing=dummy_party_on_arrival") == 1
    fresh.feed(replace(old, ts=3, features=old.features + (module.DUMMY_FEATURE,)))
    asyncio.run(start(fresh))
    assert fresh.active and fresh.members() == set(fake.members)


def _old_eligible(runner):
    """The pre-batch rule, one holder() per member, as the reference."""
    away, groups = runner.ports.away(), {}
    desired = runner._desired(set(runner.ports.cast()))
    for guid, member in sorted(runner.ports.cast().items()):
        spec = (member.class_, member.spec_tab)
        if (guid != runner.ports.human_guid and member.online is True and member.level == 80
                and spec in module._DAMAGE_SPECS and guid not in away and guid not in desired
                and runner.ports.holder(guid) is None):
            groups.setdefault(spec, []).append(guid)
    return {spec: guids[:min(10, runner.cfg.lab_bots)] for spec, guids in groups.items()
            if len(guids) >= 2 and runner.cfg.lab_bots >= 2}


def test_eligibility_reads_the_cast_bodies_in_one_batch(fake, lab, monkeypatch):
    runner, _, _ = lab
    for guid in range(105, 255):
        fake.members[guid] = Member(guid, "Tester", 1, 8 if guid % 3 else 4, 0, 80, 0, guid % 7 != 0, 0, 0,
                                    1 if guid % 3 else 2)
    fake.holders.update({101: "raid", 104: "training", 110: "realm_day", 111: "offline_raid"})
    fake.away_set = {103}
    expected = _old_eligible(runner)
    batches = []
    real = fake.holders_of
    monkeypatch.setattr(fake, "holder", lambda guid: pytest.fail("a body read per member"))
    monkeypatch.setattr(fake, "holders_of", lambda guids: batches.append(set(guids)) or real(guids))
    assert runner._eligible() == expected
    assert expected[(8, 1)] == [102, 106, 107, 109] and expected[(4, 2)][:2] == [108, 114]
    assert len(batches) == 1 and {101, 102, 104} <= batches[0] and 103 not in batches[0]
    fake.members.clear()
    batches.clear()
    assert runner._eligible() == {} and batches == []  # nobody to ask about: no body read


def test_eligibility_over_a_live_sized_journal_reads_it_once(fake, lab, monkeypatch):
    from leeroy_mind.bodies import BodyLedger
    from realm.journal import CAST, realistic_journal
    runner, _, _ = lab
    store, held = realistic_journal()
    rows = store.conn.execute("SELECT count(*) FROM realm_action").fetchone()[0]
    reads = []
    ledger = BodyLedger(fake.clock)
    ledger.add_holder_source("realm_actions", lambda: reads.append(1) or store.holds())
    fake.members.clear()
    for guid in CAST:
        fake.members[guid] = Member(guid, "Tester", 1, 8, 0, 80, 0, True, 0, 0, 1)
    monkeypatch.setattr(fake, "holder", ledger.holder)
    monkeypatch.setattr(fake, "holders_of", ledger.holders_of)
    decoded = []
    real = store._decode_row
    monkeypatch.setattr(store, "_decode_row", lambda names, row: decoded.append(1) or real(names, row))
    started = time.perf_counter()
    groups = runner._eligible()
    elapsed = time.perf_counter() - started
    assert groups == {(8, 1): sorted(set(CAST) - held)[:4]}
    assert len(reads) == 1 and len(decoded) <= rows  # one read and decode, not one per member
    assert elapsed < 1.0  # one holder() per member took about 7 s on this journal
