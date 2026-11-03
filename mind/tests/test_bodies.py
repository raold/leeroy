"""Pure scheduling tests: sources and run proofs are injected, never native facts."""

from dataclasses import FrozenInstanceError
import logging

import pytest

from leeroy_mind.bodies import (
    ALL, RANKS, BodyEvidenceUnavailable, BodyHold, BodyLedger, RunBodyProof,
)


class FakeClock:
    def __init__(self):
        self.value = 1000.0

    def now(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class Snapshot:
    def __init__(self, *rows):
        self.rows = tuple(rows)
        self.failure = False
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if self.failure:
            raise RuntimeError("private source payload must not be logged")
        return self.rows


_UNSET = object()


def hold(owner, guids, *, scope="operation", rank=_UNSET, retained=False):
    return BodyHold(scope, owner, RANKS.get(owner, 2) if rank is _UNSET else rank, guids, retained)


def ledger_with(source, *, proof=None):
    clock = FakeClock()
    ledger = BodyLedger(clock, run_proof=proof)
    ledger.add_holder_source("source", source)
    return ledger, clock


def claim(ledger, clock, owner, guids, *, rank=_UNSET, seconds=60):
    return ledger.claim(
        owner, guids, rank=RANKS.get(owner, 2) if rank is _UNSET else rank,
        until=clock.now() + seconds,
    )


def proof_for(candidate_run, *, owner="raid", scope="session", members=(101, 102), **changes):
    fields = dict(
        run=candidate_run, session=object(), owner=owner, scope_id=scope, session_key="session-key",
        connection_epoch=7, instance_id=33, members=members,
    )
    fields.update(changes)
    return RunBodyProof(**fields)


def test_historical_ranks_and_immutable_record_copies():
    assert RANKS == {
        "raid": 0, "offline_raid": 1, "training": 2, "benchmark": 2,
        "realm_event": 3, "realm_day": 4,
    }
    members = {101, 102}
    row = hold("training", members)
    proof = proof_for(object(), members=members)
    members.clear()
    assert row.guids == proof.members == frozenset({101, 102})
    with pytest.raises(FrozenInstanceError):
        row.retained = True
    with pytest.raises(FrozenInstanceError):
        proof.scope_id = "other"
    assert repr(ALL) == "ALL"


@pytest.mark.parametrize("identity", ["", "bad\0name", "x" * 257, "é" * 129, "\ud800", 3, True])
def test_hold_rejects_invalid_bounded_identities(identity):
    with pytest.raises(ValueError):
        BodyHold(identity, "training", 2, {101}, False)
    with pytest.raises(ValueError):
        BodyHold("scope", identity, 2, {101}, False)


@pytest.mark.parametrize("guid", [0, -1, True, 1.0, "101", 1 << 32])
def test_records_and_queries_reject_non_uint32_members(guid):
    with pytest.raises(ValueError):
        hold("training", {guid})
    with pytest.raises(ValueError):
        proof_for(object(), members={guid})
    with pytest.raises(ValueError):
        BodyLedger(FakeClock()).holder(guid)


@pytest.mark.parametrize("retained", [0, 1, "true", None])
def test_retained_is_exact_bool(retained):
    with pytest.raises(ValueError):
        hold("training", {101}, retained=retained)


@pytest.mark.parametrize("rank", [True, -1, 5, 2.0, "unknown", None])
def test_invalid_rank_inputs_never_enter_claims_or_fixed_sources(rank):
    clock = FakeClock()
    ledger = BodyLedger(clock)
    with pytest.raises(ValueError):
        claim(ledger, clock, "custom", {101}, rank=rank)
    with pytest.raises(ValueError):
        ledger.add_holder("custom", rank, lambda: {101})
    assert ledger.claims_for("custom") == frozenset()


@pytest.mark.parametrize("owner, rank", [("raid", 1), ("training", "realm_day"), ("benchmark", 3)])
def test_builtin_owners_cannot_change_rank(owner, rank):
    clock = FakeClock()
    ledger = BodyLedger(clock)
    with pytest.raises(ValueError):
        claim(ledger, clock, owner, {101}, rank=rank)
    with pytest.raises(ValueError):
        hold(owner, {101}, rank=rank)


def test_rank_names_preserve_exact_dynamic_owner_and_no_prefix_authority():
    clock = FakeClock()
    ledger = BodyLedger(clock)
    owner = "benchmark:exact-command-id"
    assert claim(ledger, clock, owner, {101}, rank="training") == {101}
    assert ledger.holder(101) == owner
    assert ledger.rank_for(owner) == 2
    assert ledger.rank_for("benchmark:another-command") is None
    assert ledger.rank_for("raid:pretend") is None
    assert ledger.rank_for("realm_day") == 4
    assert not ledger.may_move("benchmark", 101)
    ledger.add_holder("custom", "realm_event", lambda: {102})
    assert ledger.rank_for("custom") == 3


def test_higher_rank_claim_preempts_only_selected_local_claims_once():
    clock = FakeClock()
    ledger = BodyLedger(clock)
    preempted, released = [], []
    ledger.on_preempt("realm_day", preempted.append)
    ledger.on_release(lambda owner, guids: released.append((owner, guids)))
    assert claim(ledger, clock, "realm_day", {101, 102}) == {101, 102}
    assert claim(ledger, clock, "offline_raid", {102}) == {102}
    assert ledger.holder(102) == "offline_raid"
    assert ledger.may_move("realm_day", 101)
    assert ledger.claims_for("realm_day") == frozenset({101})
    assert preempted == [{102}]
    assert released == [("realm_day", {102})]
    ledger.poll()
    assert preempted == [{102}]


def test_provider_arrival_refreshes_before_decision_and_preempts_local_claim():
    source = Snapshot()
    ledger, clock = ledger_with(source)
    preempted = []
    ledger.on_preempt("realm_day", preempted.append)
    claim(ledger, clock, "realm_day", {101, 102})
    source.rows = (hold("offline_raid", {102}),)
    assert not ledger.may_move("realm_day", 102)
    assert ledger.holder(102) == "offline_raid"
    assert ledger.claims_for("realm_day") == frozenset({101})
    assert preempted == [{102}]


def test_lower_priority_provider_can_be_scheduling_preempted_without_erasure():
    source = Snapshot(hold("realm_day", {101, 102}))
    ledger, clock = ledger_with(source)
    preempted, released = [], []
    ledger.on_preempt("realm_day", preempted.append)
    ledger.on_release(lambda owner, guids: released.append((owner, guids)))
    assert ledger.holder(101) == "realm_day"
    assert claim(ledger, clock, "training", {101}) == {101}
    assert ledger.holder(101) == "training"
    assert ledger.others(101, besides={"training"}) == {"realm_day"}
    assert preempted == [{101}]
    assert released == []
    assert ledger.release("training") == {101}
    assert ledger.holder(101) == "realm_day"
    assert preempted == [{101}]


def test_raid_all_reservation_has_no_invented_universe_and_ends_normally():
    active = [True]
    ledger = BodyLedger(FakeClock())
    ledger.add_holder("raid", "raid", lambda: ALL if active[0] else set())
    assert ledger.holder((1 << 32) - 1) == "raid"
    assert ledger.may_move("raid", 101)
    assert not ledger.may_move("realm_event", 101)
    assert ledger.claim("realm_event", {102}, rank=3, until=1100) == set()
    active[0] = False
    assert ledger.claim("realm_event", {102}, rank=3, until=1100) == {102}


def test_equal_rank_foreign_claim_and_provider_never_preempt():
    source = Snapshot(hold("training", {101}))
    ledger, clock = ledger_with(source)
    assert claim(ledger, clock, "benchmark:one", {101}, rank="training") == set()
    assert claim(ledger, clock, "benchmark:one", {102}, rank="training") == {102}
    assert claim(ledger, clock, "benchmark:two", {102}, rank="training") == set()
    assert ledger.claims_for("benchmark:one") == frozenset({102})


def test_provider_before_claim_tie_is_deterministic_but_cannot_authorize_movement():
    source = Snapshot()
    ledger, clock = ledger_with(source)
    preempted = []
    ledger.on_preempt("benchmark:one", preempted.append)
    claim(ledger, clock, "benchmark:one", {101}, rank=2)
    source.rows = (hold("training", {101}),)
    assert ledger.holder(101) == "training"
    assert not ledger.may_move("training", 101)
    assert not ledger.may_move("benchmark:one", 101)
    assert ledger.claims_for("benchmark:one") == frozenset({101})
    assert preempted == []


def test_equal_rank_provider_conflict_has_deterministic_holder_and_no_mover():
    source = Snapshot(hold("training", {101}, scope="lab"))
    ledger, _ = ledger_with(source)
    ledger.add_holder_source("second", Snapshot(hold("benchmark:one", {101}, scope="bench")))
    assert ledger.holder(101) == "training"
    assert ledger.others(101, besides=set()) == {"training", "benchmark:one"}
    assert not ledger.may_move("training", 101)
    assert not ledger.may_move("benchmark:one", 101)


def test_source_precedes_claim_and_survives_release_then_hands_back_once():
    owner = "benchmark:exact-command"
    source = Snapshot(hold(owner, {101}, scope="command", retained=True))
    ledger, clock = ledger_with(source)
    released = []
    ledger.on_release(lambda owner, guids: released.append((owner, guids)))
    assert claim(ledger, clock, owner, {101}, rank="training") == {101}
    assert not ledger.may_move(owner, 101)
    assert claim(ledger, clock, owner, {101}, rank=2, seconds=120) == {101}
    assert ledger.release(owner, {101, 999}) == {101}
    assert not (ledger.claims_for(owner) & {101, 999})
    assert ledger.holder(101) == owner
    assert released == []
    assert ledger.release(owner, {101, 999}) == set()
    source.rows = ()
    ledger.poll()
    assert released == [(owner, {101})]
    assert ledger.holder(101) is None
    assert ledger.may_move("realm_day", 101)
    assert released == [(owner, {101})]


def test_renewal_cannot_resurrect_body_under_higher_priority_owner():
    source = Snapshot(hold("training", {101}, retained=True))
    ledger, clock = ledger_with(source)
    assert claim(ledger, clock, "training", {101}) == {101}
    source.rows += (hold("raid", {101}, scope="raid"),)
    assert claim(ledger, clock, "training", {101}) == set()
    assert ledger.claims_for("training") == frozenset()
    assert ledger.others(101, besides=set()) == {"raid", "training"}


@pytest.mark.parametrize("owner, rank", [("raid", 0), ("offline_raid", 1), ("realm_day", 4)])
def test_foreign_retained_reservation_vetoes_claim_even_at_higher_rank(owner, rank):
    source = Snapshot(hold("benchmark:one", {101}, retained=True))
    ledger, clock = ledger_with(source)
    assert claim(ledger, clock, owner, {101}, rank=rank) == set()
    assert not ledger.may_move("benchmark:one", 101)
    assert not ledger.may_move(owner, 101)


def test_retained_reservation_vetoes_movement_of_identical_owner():
    source = Snapshot(hold("training", {101}, scope="first", retained=True))
    ledger, clock = ledger_with(source)
    assert claim(ledger, clock, "training", {101}) == {101}
    assert not ledger.may_move("training", 101)
    # Renewal is scheduling metadata only, including under two operations of one owner.
    source.rows += (hold("training", {101}, scope="second", retained=True),)
    assert claim(ledger, clock, "training", {101}) == {101}
    assert not ledger.may_move("training", 101)


def test_claim_expiry_is_a_release_before_poll_and_missing_release_is_harmless():
    clock = FakeClock()
    ledger = BodyLedger(clock)
    released = []
    ledger.on_release(lambda owner, guids: released.append((owner, guids)))
    claim(ledger, clock, "realm_day", {101, 102}, seconds=60)
    clock.advance(60)
    assert ledger.claims_for("realm_day") == frozenset()
    assert ledger.release("realm_day", {101, 999}) == set()
    assert ledger.holder(101) is None
    assert released == [("realm_day", {101, 102})]


def test_expiry_beneath_retained_source_never_emits_handback():
    source = Snapshot(hold("training", {101}, retained=True))
    ledger, clock = ledger_with(source)
    released = []
    ledger.on_release(lambda owner, guids: released.append((owner, guids)))
    claim(ledger, clock, "training", {101})
    clock.advance(61)
    assert ledger.claims_for("training") == frozenset()
    assert ledger.holder(101) == "training"
    assert released == []
    source.rows = ()
    assert ledger.holder(101) is None
    assert released == [("training", {101})]


@pytest.mark.parametrize("deadline", [1000, 999, float("inf"), float("-inf"), float("nan"), True, "1001"])
def test_claim_requires_finite_future_deadline(deadline):
    ledger = BodyLedger(FakeClock())
    with pytest.raises(ValueError):
        ledger.claim("realm_day", {101}, rank=4, until=deadline)
    assert ledger.claims_for("realm_day") == frozenset()


def test_provider_failure_blocks_globally_without_false_release_or_preemption(caplog):
    source = Snapshot(hold("training", {101}, retained=True))
    ledger, clock = ledger_with(source)
    released, preempted = [], []
    ledger.on_release(lambda owner, guids: released.append((owner, guids)))
    ledger.on_preempt("realm_day", preempted.append)
    assert ledger.holder(101) == "training"
    assert claim(ledger, clock, "realm_day", {102}) == {102}
    source.failure = True
    with caplog.at_level(logging.WARNING):
        assert claim(ledger, clock, "raid", {999}) == set()
        assert not ledger.may_move("training", 101)
        assert not ledger.may_move("realm_day", 999)
        assert ledger.rank_for("raid") is None
        with pytest.raises(BodyEvidenceUnavailable, match="^body evidence unavailable$"):
            ledger.holder(999)
        with pytest.raises(BodyEvidenceUnavailable):
            ledger.others(101, besides=set())
        assert ledger.owner_for_run(object()) is None
    assert ledger.release("realm_day", {102}) == {102}
    assert ledger.claims_for("realm_day") == frozenset()
    assert released == preempted == []
    assert "private source payload" not in caplog.text
    source.failure = False
    source.rows = ()
    assert ledger.holder(101) is None
    assert released == [("realm_day", {102}), ("training", {101})]
    assert preempted == []
    assert claim(ledger, clock, "raid", {999}) == {999}


def test_failed_initial_source_is_unavailable_rather_than_empty():
    source = Snapshot()
    source.failure = True
    ledger, clock = ledger_with(source)
    assert claim(ledger, clock, "raid", {101}) == set()
    with pytest.raises(BodyEvidenceUnavailable):
        ledger.holder(101)
    source.failure = False
    assert ledger.holder(101) is None


@pytest.mark.parametrize("bad_rows", [
    [],
    (object(),),
    (hold("training", {102}, scope="new-valid-row"), object()),
    (hold("training", {101}, scope="duplicate"), hold("training", {102}, scope="duplicate")),
])
def test_malformed_snapshot_rejected_atomically_then_recovery_hands_back(bad_rows):
    source = Snapshot(hold("training", {101}))
    ledger, _ = ledger_with(source)
    released = []
    ledger.on_release(lambda owner, guids: released.append((owner, guids)))
    assert ledger.holder(101) == "training"
    source.rows = bad_rows
    with pytest.raises(BodyEvidenceUnavailable):
        ledger.holder(101)
    assert released == []
    source.rows = ()
    assert ledger.holder(101) is None
    assert released == [("training", {101})]


def test_one_bad_source_does_not_partially_apply_another_sources_shrink():
    first = Snapshot(hold("training", {101}))
    second = Snapshot(hold("offline_raid", {102}))
    ledger, _ = ledger_with(first)
    ledger.add_holder_source("second", second)
    released = []
    ledger.on_release(lambda owner, guids: released.append((owner, guids)))
    assert ledger.holder(101) == "training"
    first.rows = ()
    second.failure = True
    assert not ledger.may_move("realm_day", 101)
    assert released == []
    second.failure = False
    assert ledger.holder(101) is None
    assert released == [("training", {101})]


def test_source_failure_and_unchanged_recovery_do_not_invent_handback():
    source = Snapshot(hold("training", {101}, retained=True))
    ledger, _ = ledger_with(source)
    released = []
    ledger.on_release(lambda owner, guids: released.append((owner, guids)))
    assert ledger.holder(101) == "training"
    source.failure = True
    assert not ledger.may_move("raid", 101)
    source.failure = False
    assert ledger.holder(101) == "training"
    assert released == []
    source.rows = ()
    assert ledger.holder(101) is None
    assert released == [("training", {101})]


def test_mutated_record_in_snapshot_is_revalidated_and_not_partially_applied():
    source = Snapshot(hold("training", {101}))
    ledger, _ = ledger_with(source)
    assert ledger.holder(101) == "training"
    bad = hold("training", {102})
    object.__setattr__(bad, "rank", 4)
    source.rows = (bad,)
    with pytest.raises(BodyEvidenceUnavailable):
        ledger.holder(101)
    source.rows = (hold("training", {101}),)
    assert ledger.holder(101) == "training"


def test_active_dynamic_rank_conflict_fails_closed_and_recovers():
    source = Snapshot(hold("custom", {101}, rank=2))
    ledger, clock = ledger_with(source)
    assert claim(ledger, clock, "custom", {102}, rank=2) == {102}
    # An inconsistent new claim is refused before it becomes active evidence.
    assert claim(ledger, clock, "custom", {103}, rank=3) == set()
    source.rows = (hold("custom", {101}, rank=3),)
    assert ledger.rank_for("custom") is None
    assert not ledger.may_move("raid", 999)
    with pytest.raises(BodyEvidenceUnavailable):
        ledger.holder(101)
    assert ledger.release("custom") == {102}
    assert ledger.rank_for("custom") == 3
    assert ledger.holder(101) == "custom"


def test_cross_source_owner_rank_conflict_fails_closed():
    ledger, clock = ledger_with(Snapshot(hold("custom", {101}, rank=2)))
    ledger.add_holder_source("second", Snapshot(hold("custom", {102}, rank=3)))
    assert ledger.rank_for("custom") is None
    assert claim(ledger, clock, "raid", {999}) == set()
    assert not ledger.may_move("custom", 101)


def test_multiple_source_rows_and_claim_changes_coalesce_effective_release():
    source = Snapshot(
        hold("training", {101, 102}, scope="lab"),
        hold("training", {102, 103}, scope="benchmark"),
    )
    ledger, clock = ledger_with(source)
    released = []
    ledger.on_release(lambda owner, guids: released.append((owner, guids)))
    claim(ledger, clock, "training", {101, 102, 103})
    assert ledger.release("training", {101, 102}) == {101, 102}
    assert released == []
    source.rows = (hold("training", {102}, scope="remaining"),)
    assert ledger.holder(101) is None
    assert released == [("training", {101})]
    assert ledger.release("training") == {103}
    assert released == [("training", {101}), ("training", {103})]
    source.rows = ()
    assert ledger.holder(102) is None
    assert released == [("training", {101}), ("training", {103}), ("training", {102})]


def test_simultaneous_expiry_and_provider_shrink_emit_one_union_release():
    source = Snapshot(hold("training", {101, 102}, retained=True))
    ledger, clock = ledger_with(source)
    released = []
    ledger.on_release(lambda owner, guids: released.append((owner, guids)))
    claim(ledger, clock, "training", {101, 102})
    clock.advance(61)
    source.rows = ()
    assert ledger.holder(101) is None
    assert released == [("training", {101, 102})]


def test_all_to_set_and_empty_release_only_previously_observed_members():
    source = Snapshot(hold("raid", ALL, scope="intent"))
    ledger, clock = ledger_with(source)
    released = []
    ledger.on_release(lambda owner, guids: released.append((owner, guids)))
    assert ledger.holder(101) == "raid"
    assert ledger.others(102, besides=set()) == {"raid"}
    assert claim(ledger, clock, "realm_day", {103}) == set()
    # Finite snapshots also contribute observed GUIDs, even at a hidden rank.
    ledger.add_holder_source("finite", Snapshot(hold("training", {104}, scope="lab")))
    assert ledger.holder(102) == "raid"
    source.rows = (hold("raid", {102}, scope="intent"),)
    assert ledger.holder(102) == "raid"
    assert released == [("raid", {101, 103, 104})]
    source.rows = ()
    assert ledger.holder(102) is None
    assert released == [("raid", {101, 103, 104}), ("raid", {102})]


def test_all_handback_excludes_guid_first_seen_after_all_ended():
    source = Snapshot(hold("raid", ALL))
    ledger, _ = ledger_with(source)
    released = []
    ledger.on_release(lambda owner, guids: released.append((owner, guids)))
    assert ledger.holder(101) == "raid"
    source.rows = ()
    assert ledger.holder(999) is None
    assert released == [("raid", {101})]


def test_poll_throttles_source_reads_but_decisions_always_refresh():
    source = Snapshot(hold("training", {101}))
    ledger, clock = ledger_with(source)
    ledger.poll()
    assert source.calls == 1
    source.rows = ()
    clock.advance(0.99)
    ledger.poll()
    assert source.calls == 1
    assert ledger.holder(101) is None
    assert source.calls == 2
    ledger.poll()
    assert source.calls == 2
    clock.advance(0.01)
    ledger.poll()
    assert source.calls == 3


def test_claims_for_and_local_release_do_not_inspect_sources():
    source = Snapshot()
    ledger, clock = ledger_with(source)
    claim(ledger, clock, "realm_day", {101})
    calls = source.calls
    source.failure = True
    assert ledger.claims_for("realm_day") == frozenset({101})
    assert ledger.release("realm_day") == {101}
    assert source.calls == calls


def test_callback_reentrancy_commits_before_delivery_and_queues_new_events():
    clock = FakeClock()
    ledger = BodyLedger(clock)
    events = []
    claim(ledger, clock, "realm_day", {101, 102})

    def first(owner, guids):
        events.append(("first", owner, set(guids)))
        assert ledger.holder(next(iter(guids))) is None
        assert not (ledger.claims_for(owner) & guids)
        if guids == {101}:
            assert ledger.release(owner, {101}) == set()
            assert ledger.release(owner, {102}) == {102}

    def second(owner, guids):
        events.append(("second", owner, set(guids)))

    ledger.on_release(first)
    ledger.on_release(second)
    assert ledger.release("realm_day", {101}) == {101}
    assert events == [
        ("first", "realm_day", {101}), ("second", "realm_day", {101}),
        ("first", "realm_day", {102}), ("second", "realm_day", {102}),
    ]


def test_callback_exception_is_safe_and_does_not_break_following_callbacks(caplog):
    source = Snapshot()
    ledger, clock = ledger_with(source)
    claim(ledger, clock, "realm_day", {101})
    events = []

    def fail(*args):
        raise RuntimeError("secret callback payload")

    ledger.on_preempt("realm_day", fail)
    ledger.on_preempt("realm_day", lambda guids: events.append(("preempt", guids)))
    ledger.on_release(fail)
    ledger.on_release(lambda owner, guids: events.append((owner, guids)))
    source.rows = (hold("offline_raid", {101}, retained=True),)
    with caplog.at_level(logging.WARNING):
        assert ledger.holder(101) == "offline_raid"
    assert ledger.claims_for("realm_day") == frozenset()
    assert not ledger.may_move("offline_raid", 101)
    assert events == [("preempt", {101}), ("realm_day", {101})]
    assert caplog.text.count("body_callback_failed") == 2
    assert "secret callback payload" not in caplog.text


def test_callbacks_run_after_all_sources_are_inspected():
    source = Snapshot(hold("training", {101}))
    ledger, _ = ledger_with(source)
    inspecting = []

    def second_source():
        inspecting.append("second")
        return ()

    ledger.add_holder_source("second", second_source)
    assert ledger.holder(101) == "training"
    inspecting.clear()
    ledger.on_release(lambda owner, guids: inspecting.append("release"))
    source.rows = ()
    ledger.holder(101)
    assert inspecting == ["second", "release", "second"]


def test_outer_may_move_returns_false_after_release_callback_claims_tested_guid():
    source = Snapshot(hold("training", {101}, scope="lab"))
    ledger, clock = ledger_with(source)
    assert ledger.holder(101) == "training"
    callback_grants = []

    def handoff(owner, guids):
        if owner == "training":
            callback_grants.append(claim(ledger, clock, "offline_raid", guids))

    ledger.on_release(handoff)
    source.rows = ()
    returned = ledger.may_move("realm_day", 101)
    assert returned is False
    assert callback_grants == [{101}]
    assert ledger.claims_for("offline_raid") == frozenset({101})


def test_outer_may_move_refreshes_retained_source_changed_only_by_callback():
    source = Snapshot(hold("training", {101}, scope="lab"))
    ledger, _ = ledger_with(source)
    assert ledger.holder(101) == "training"

    def retain(owner, guids):
        if owner == "training":
            source.rows = (hold("offline_raid", guids, scope="new-job", retained=True),)

    ledger.on_release(retain)
    source.rows = ()
    returned = ledger.may_move("realm_day", 101)
    assert returned is False
    assert ledger.holder(101) == "offline_raid"


def test_batch_gate_refresh_callback_acquires_an_earlier_batch_member():
    source = Snapshot(hold("training", {102}, scope="lab"))
    ledger, clock = ledger_with(source)
    assert ledger.holder(102) == "training"
    callback_grants = []

    def handoff(owner, guids):
        if owner == "training":
            callback_grants.append(claim(ledger, clock, "offline_raid", {101}))

    ledger.on_release(handoff)
    source.rows = ()
    returned = ledger.may_move_all("realm_day", (101, 102), rank="realm_day")
    assert returned is False
    assert callback_grants == [{101}]
    assert ledger.claims_for("offline_raid") == frozenset({101})


def test_batch_gate_denies_when_later_member_refresh_invalidates_an_earlier_approval():
    def setup_scripted_handoff():
        clock = FakeClock()
        ledger = BodyLedger(clock)
        training = hold("training", {102}, scope="lab")
        reads = []
        callback_grants = []

        def snapshot():
            # Baseline and first prospective check still hold 102; every later
            # read observes release, including reads from reentrant callbacks.
            held = len(reads) < 2
            reads.append(held)
            return (training,) if held else ()

        def handoff(owner, guids):
            if owner == "training":
                callback_grants.append(claim(ledger, clock, "offline_raid", {101}))

        ledger.add_holder_source("scripted", snapshot)
        assert ledger.holder(102) == "training"
        ledger.on_release(handoff)
        return ledger, reads, callback_grants

    control, control_reads, control_grants = setup_scripted_handoff()
    earlier = control.may_move("realm_day", 101)
    assert earlier is True
    assert control_reads == [True, True]
    assert control_grants == []
    later = control.may_move("realm_day", 102)
    assert later is True
    assert control_grants == [{101}]
    assert not any(control_reads[2:])
    assert control.claims_for("offline_raid") == frozenset({101})
    assert control.holder(101) == "offline_raid"
    assert (earlier, later) == (True, True)

    batch, batch_reads, batch_grants = setup_scripted_handoff()
    returned = batch.may_move_all("realm_day", (101, 102), rank="realm_day")
    assert returned is False
    assert batch_reads == [True, True]
    assert batch_grants == []


@pytest.mark.parametrize("unavailable", [False, True])
def test_batch_gate_rechecks_callback_only_retained_or_unavailable_evidence(unavailable):
    source = Snapshot(hold("training", {103}, scope="lab"))
    ledger, _ = ledger_with(source)
    assert ledger.holder(103) == "training"

    def change(owner, guids):
        if owner == "training":
            if unavailable:
                source.failure = True
            else:
                source.rows = (hold("offline_raid", {101}, scope="new-job", retained=True),)

    ledger.on_release(change)
    source.rows = ()
    returned = ledger.may_move_all("realm_day", (101, 102), rank=4)
    assert returned is False


def test_batch_gate_copies_members_before_source_refresh_and_listener_mutation():
    selected = {101, 102}
    source = Snapshot(hold("training", {103}, scope="lab"))
    ledger, _ = ledger_with(source)
    assert ledger.holder(103) == "training"

    def change(owner, guids):
        if owner == "training":
            selected.clear()
            source.rows = (hold("offline_raid", {101}, scope="new-job", retained=True),)

    ledger.on_release(change)
    source.rows = ()
    returned = ledger.may_move_all("realm_day", selected)
    assert returned is False
    assert selected == set()


@pytest.mark.parametrize("size", [1, 2, 40, 150])
def test_batch_gate_refresh_count_is_independent_of_batch_size_and_never_cached(size):
    source = Snapshot()
    ledger, _ = ledger_with(source)
    selected = tuple(range(101, 101 + size))
    assert ledger.may_move_all("realm_day", selected, rank=4)
    assert source.calls == 1
    source.rows = (hold("offline_raid", {101}),)
    assert not ledger.may_move_all("realm_day", selected, rank=4)
    assert source.calls == 2


def test_batch_gate_consumes_finite_members_once_and_accepts_duplicates():
    source = Snapshot()
    ledger, _ = ledger_with(source)
    yielded = []

    def members():
        for guid in (101, 101, 102):
            yielded.append(guid)
            yield guid

    assert ledger.may_move_all("realm_day", members())
    assert yielded == [101, 101, 102]
    assert source.calls == 1


@pytest.mark.parametrize("members", [{0}, {True}, {1 << 32}, {"101"}, ALL, None])
def test_batch_gate_rejects_invalid_members_before_source_inspection(members):
    source = Snapshot()
    ledger, _ = ledger_with(source)
    with pytest.raises(ValueError):
        ledger.may_move_all("realm_day", members)
    assert source.calls == 0


@pytest.mark.parametrize("owner, expected", [
    ("realm_day", 4), ("realm_day", "realm_day"), ("raid", 0), ("raid", "raid"),
])
def test_batch_gate_normalizes_expected_rank_and_preserves_exact_owner(owner, expected):
    source = Snapshot(hold(owner, {101}))
    ledger, _ = ledger_with(source)
    assert ledger.may_move_all(owner, {101, 102}, rank=expected)
    assert source.calls == 1


@pytest.mark.parametrize("expected", [True, -1, 5, 2.0, "unknown"])
def test_batch_gate_rejects_invalid_expected_rank_before_source_inspection(expected):
    source = Snapshot()
    ledger, _ = ledger_with(source)
    with pytest.raises(ValueError):
        ledger.may_move_all("realm_day", {101}, rank=expected)
    assert source.calls == 0


def test_batch_gate_expected_rank_must_match_current_builtin_or_opaque_owner():
    source = Snapshot()
    ledger, _ = ledger_with(source)
    assert not ledger.may_move_all("realm_day", {101}, rank="training")
    assert ledger.may_move_all("benchmark:unregistered", {101})
    assert not ledger.may_move_all("benchmark:unregistered", {101}, rank=2)
    source.rows = (hold("benchmark:exact-command", {101}, rank=2),)
    assert ledger.may_move_all("benchmark:exact-command", {101, 102}, rank="training")
    assert not ledger.may_move_all("benchmark:another-command", {102}, rank=2)


def test_batch_gate_checks_callback_changed_rank_in_its_movement_snapshot():
    source = Snapshot(
        hold("custom", {101}, rank=2, scope="custom-job"),
        hold("training", {103}, scope="lab"),
    )
    ledger, _ = ledger_with(source)
    assert ledger.may_move_all("custom", (101, 102), rank=2)

    def replace(owner, guids):
        if owner == "training":
            source.rows = (hold("custom", {101}, rank=3, scope="custom-job"),)

    ledger.on_release(replace)
    source.rows = (source.rows[0],)
    returned = ledger.may_move_all("custom", (101, 102), rank="training")
    assert returned is False
    assert ledger.may_move_all("custom", (101, 102), rank="realm_event")


def test_batch_gate_denies_global_rank_conflict_even_outside_selected_members():
    source = Snapshot(hold("custom", {103}, rank=2))
    ledger, _ = ledger_with(source)
    assert ledger.may_move_all("realm_day", (101, 102), rank=4)
    ledger.add_holder_source("conflict", Snapshot(hold("custom", {104}, rank=3)))
    assert not ledger.may_move_all("realm_day", (101, 102), rank=4)
    assert not ledger.may_move_all("custom", (101, 102))


@pytest.mark.parametrize("rows, owner, expected", [
    ((), "realm_day", True),
    ((hold("realm_day", {101}),), "realm_day", True),
    ((hold("realm_day", {101}, retained=True),), "realm_day", False),
    ((hold("offline_raid", {102}),), "realm_day", False),
    ((hold("raid", ALL),), "raid", True),
    ((hold("raid", ALL),), "realm_day", False),
    ((hold("training", {101}, scope="lab"),
      hold("benchmark:one", {101}, scope="bench")), "training", False),
])
def test_batch_gate_applies_movement_arbitration_to_every_selected_member(rows, owner, expected):
    ledger, _ = ledger_with(Snapshot(*rows))
    assert ledger.may_move_all(owner, (101, 102)) is expected


def test_empty_batch_still_requires_fresh_healthy_evidence_and_expected_rank():
    source = Snapshot()
    ledger, _ = ledger_with(source)
    assert ledger.may_move_all("realm_day", (), rank=4)
    assert not ledger.may_move_all("custom", (), rank=2)
    source.failure = True
    assert not ledger.may_move_all("realm_day", ())


def test_batch_gate_callback_evidence_churn_is_bounded_and_fails_closed(caplog):
    source = Snapshot(hold("training", {103}))
    ledger, _ = ledger_with(source)
    assert ledger.holder(103) == "training"

    def churn(owner, guids):
        next_owner = "training" if source.rows[0].owner == "benchmark:churn" else "benchmark:churn"
        source.rows = (hold(next_owner, {103}),)

    ledger.on_release(churn)
    source.rows = (hold("benchmark:churn", {103}),)
    before = source.calls
    with caplog.at_level(logging.WARNING):
        returned = ledger.may_move_all("realm_day", (101, 102), rank=4)
    assert returned is False
    assert source.calls - before < 100
    assert "body_evidence_unstable" in caplog.text


@pytest.mark.parametrize("method, expected", [("holder", "offline_raid"), ("others", {"offline_raid"})])
def test_outer_ownership_reads_include_release_callback_handoff(method, expected):
    source = Snapshot(hold("training", {101}))
    ledger, clock = ledger_with(source)
    assert ledger.holder(101) == "training"
    ledger.on_release(
        lambda owner, guids: claim(ledger, clock, "offline_raid", guids)
        if owner == "training" else None
    )
    source.rows = ()
    returned = ledger.holder(101) if method == "holder" else ledger.others(101, besides=set())
    assert returned == expected


def test_outer_rank_read_refreshes_callback_only_rank_replacement():
    source = Snapshot(
        hold("custom", {101}, rank=2, scope="custom-job"),
        hold("training", {103}, scope="lab"),
    )
    ledger, _ = ledger_with(source)
    assert ledger.rank_for("custom") == 2

    def replace(owner, guids):
        if owner == "training":
            source.rows = (hold("custom", {101}, rank=3, scope="custom-job"),)

    ledger.on_release(replace)
    source.rows = (source.rows[0],)
    returned = ledger.rank_for("custom")
    assert returned == 3


def test_outer_local_claim_read_includes_reentrant_renewal_after_expiry():
    clock = FakeClock()
    ledger = BodyLedger(clock)
    claim(ledger, clock, "realm_day", {101}, seconds=1)
    renewed = []

    def renew(owner, guids):
        renewed.append(claim(ledger, clock, owner, guids))

    ledger.on_release(renew)
    clock.advance(2)
    returned = ledger.claims_for("realm_day")
    assert returned == frozenset({101})
    assert renewed == [{101}]


def test_outer_claim_does_not_return_guid_taken_by_reentrant_preemption_callback():
    clock = FakeClock()
    ledger = BodyLedger(clock)
    claim(ledger, clock, "realm_day", {101})
    callback_grants = []
    ledger.on_preempt(
        "realm_day", lambda guids: callback_grants.append(claim(ledger, clock, "offline_raid", guids))
    )
    returned = claim(ledger, clock, "realm_event", {101})
    assert returned == set()
    assert callback_grants == [{101}]
    assert ledger.claims_for("realm_event") == frozenset()
    assert ledger.claims_for("offline_raid") == frozenset({101})


def test_release_receipt_reports_removed_claim_while_local_read_reports_callback_reclaim():
    clock = FakeClock()
    ledger = BodyLedger(clock)
    claim(ledger, clock, "realm_day", {101})
    ledger.on_release(lambda owner, guids: claim(ledger, clock, owner, guids))
    removed = ledger.release("realm_day", {101})
    assert removed == {101}
    assert ledger.claims_for("realm_day") == frozenset({101})


@pytest.mark.parametrize("unavailable", [False, True])
@pytest.mark.parametrize("refresh_in_callback", [False, True])
def test_outer_run_proof_denies_callback_retained_overlap_or_unavailable_evidence(
    unavailable, refresh_in_callback,
):
    run = object()
    proof = proof_for(run)
    raid = hold("raid", {101, 102}, scope="session", retained=True)
    source = Snapshot(raid, hold("training", {103}, scope="lab"))
    ledger, _ = ledger_with(source, proof=lambda candidate: proof)
    assert ledger.owner_for_run(run) == "raid"

    def change(owner, guids):
        if owner != "training":
            return
        if unavailable:
            source.failure = True
        else:
            source.rows = (raid, hold("training", {102}, scope="other-job", retained=True))
        if refresh_in_callback:
            assert not ledger.may_move("realm_day", 101)

    ledger.on_release(change)
    source.rows = (raid,)
    returned = ledger.owner_for_run(run)
    assert returned is None


def test_outer_run_proof_is_resolved_after_callback_changes_session_truth():
    run = object()
    proof = proof_for(run)
    open_session = [True]
    raid = hold("raid", {101, 102}, scope="session", retained=True)
    source = Snapshot(raid, hold("training", {103}, scope="lab"))
    ledger, _ = ledger_with(source, proof=lambda candidate: proof if open_session[0] else None)
    assert ledger.owner_for_run(run) == "raid"
    ledger.on_release(lambda owner, guids: open_session.__setitem__(0, False))
    source.rows = (raid,)
    returned = ledger.owner_for_run(run)
    assert returned is None


def test_callback_driven_evidence_instability_is_bounded_and_fails_closed(caplog):
    source = Snapshot(hold("training", {101}))
    ledger, _ = ledger_with(source)
    assert ledger.holder(101) == "training"
    active = [True]

    def churn(owner, guids):
        if active[0]:
            next_owner = "training" if source.rows[0].owner == "benchmark:churn" else "benchmark:churn"
            source.rows = (hold(next_owner, {101}),)

    ledger.on_release(churn)
    source.rows = (hold("benchmark:churn", {101}),)
    before = source.calls
    with caplog.at_level(logging.WARNING):
        returned = ledger.may_move("training", 101)
    assert returned is False
    assert source.calls - before < 100
    assert "body_evidence_unstable" in caplog.text
    active[0] = False
    source.rows = ()
    assert ledger.may_move("realm_day", 101)


def test_source_reentry_fails_closed_even_if_source_swallows_exception():
    clock = FakeClock()
    ledger = BodyLedger(clock)

    def source():
        try:
            ledger.claims_for("training")
        except BodyEvidenceUnavailable:
            pass
        return ()

    ledger.add_holder_source("source", source)
    assert claim(ledger, clock, "raid", {101}) == set()
    with pytest.raises(BodyEvidenceUnavailable):
        ledger.holder(101)


@pytest.mark.parametrize("owner", ["raid", "offline_raid"])
@pytest.mark.parametrize("retained", [False, True])
def test_exact_injected_run_proof_accepts_current_or_retained_reservation(owner, retained):
    run = object()
    proof = proof_for(run, owner=owner)
    source = Snapshot(hold(owner, {101, 102}, scope="session", retained=retained))
    ledger, _ = ledger_with(source, proof=lambda candidate: proof)
    assert ledger.owner_for_run(run) == owner
    if retained:
        assert not ledger.may_move(owner, 101)
    # This result is scheduling ownership only; no native cleanup is asserted.


def test_absent_run_proof_callback_never_derives_authority_from_owner_name():
    source = Snapshot(hold("raid", {101, 102}, scope="session", retained=True))
    ledger, _ = ledger_with(source)
    assert ledger.owner_for_run(object()) is None


def test_proof_uses_run_object_identity_without_reading_neighbor_attributes():
    class Run:
        def __getattr__(self, name):
            raise AssertionError("ledger inspected neighbor state")

        def __eq__(self, other):
            return True

    run, replacement = Run(), Run()
    proof = proof_for(run)
    ledger, _ = ledger_with(
        Snapshot(hold("raid", {101, 102}, scope="session", retained=True)),
        proof=lambda candidate: proof,
    )
    assert ledger.owner_for_run(run) == "raid"
    assert ledger.owner_for_run(replacement) is None


@pytest.mark.parametrize("changes", [
    {"scope": "wrong-session"},
    {"members": {101, 999}},
    {"owner": "training"},
])
def test_run_proof_rejects_mismatched_scope_members_and_owner(changes):
    run = object()
    proof = proof_for(run, **changes)
    ledger, _ = ledger_with(
        Snapshot(hold("raid", {101, 102}, scope="session", retained=True)),
        proof=lambda candidate: proof,
    )
    assert ledger.owner_for_run(run) is None


def test_run_proof_requires_finite_matching_reservation_and_effective_holder():
    run = object()
    proof = proof_for(run, owner="offline_raid")
    source = Snapshot(hold("offline_raid", ALL, scope="session"))
    ledger, _ = ledger_with(source, proof=lambda candidate: proof)
    assert ledger.owner_for_run(run) is None
    source.rows = (
        hold("offline_raid", {101, 102}, scope="session", retained=True),
        hold("raid", ALL, scope="intent"),
    )
    assert ledger.owner_for_run(run) is None
    source.rows = (source.rows[0],)
    assert ledger.owner_for_run(run) == "offline_raid"


@pytest.mark.parametrize("other_owner", ["raid", "training"])
def test_foreign_retained_operation_vetoes_run_proof_even_under_same_owner(other_owner):
    run = object()
    proof = proof_for(run)
    source = Snapshot(
        hold("raid", {101, 102}, scope="session", retained=True),
        hold(other_owner, {102}, scope="different-operation", retained=True),
    )
    ledger, _ = ledger_with(source, proof=lambda candidate: proof)
    assert ledger.owner_for_run(run) is None
    source.rows = (source.rows[0],)
    assert ledger.owner_for_run(run) == "raid"


def test_duplicate_matching_scope_across_sources_denies_run_proof():
    run = object()
    proof = proof_for(run)
    source = Snapshot(hold("raid", {101, 102}, scope="session", retained=True))
    ledger, _ = ledger_with(source, proof=lambda candidate: proof)
    duplicate = Snapshot(hold("raid", {101, 102}, scope="session"))
    ledger.add_holder_source("duplicate", duplicate)
    assert ledger.owner_for_run(run) is None
    duplicate.rows = ()
    assert ledger.owner_for_run(run) == "raid"


def test_unavailable_source_prevents_proof_provider_call_and_recovers():
    run = object()
    proof = proof_for(run)
    calls = []
    source = Snapshot(hold("raid", {101, 102}, scope="session", retained=True))

    def resolver(candidate):
        calls.append(candidate)
        return proof

    ledger, _ = ledger_with(source, proof=resolver)
    source.failure = True
    assert ledger.owner_for_run(run) is None
    assert calls == []
    source.failure = False
    assert ledger.owner_for_run(run) == "raid"
    assert calls == [run]


@pytest.mark.parametrize("field, invalid", [
    ("run", None), ("session", None), ("session_key", ""),
    ("connection_epoch", 0), ("connection_epoch", True),
    ("connection_epoch", 1 << 64), ("connection_epoch", "7"),
    ("instance_id", 0), ("instance_id", True), ("instance_id", 1 << 32),
    ("members", frozenset()),
])
def test_run_proof_validates_session_epoch_instance_and_nonempty_members(field, invalid):
    with pytest.raises(ValueError):
        proof_for(object(), **{field: invalid})


@pytest.mark.parametrize("field, invalid", [
    ("connection_epoch", 0), ("instance_id", 0), ("members", frozenset()),
])
def test_ledger_revalidates_returned_proof_instead_of_trusting_frozen_marker(field, invalid):
    run = object()
    proof = proof_for(run)
    object.__setattr__(proof, field, invalid)
    ledger, _ = ledger_with(
        Snapshot(hold("raid", {101, 102}, scope="session", retained=True)),
        proof=lambda candidate: proof,
    )
    assert ledger.owner_for_run(run) is None


def test_trusted_resolver_owns_semantic_epoch_instance_and_session_validation():
    run, session = object(), object()
    truth = dict(current=run, session=session, epoch=7, instance=33, open=True)
    proof = proof_for(run, session=session)

    def resolver(candidate):
        if (candidate is not truth["current"] or proof.session is not truth["session"]
                or proof.connection_epoch != truth["epoch"]
                or proof.instance_id != truth["instance"] or not truth["open"]):
            return None
        return proof

    ledger, _ = ledger_with(
        Snapshot(hold("raid", {101, 102}, scope="session", retained=True)), proof=resolver,
    )
    assert ledger.owner_for_run(run) == "raid"
    for field, wrong in [("epoch", 8), ("instance", 34), ("session", object()), ("open", False)]:
        previous = truth[field]
        truth[field] = wrong
        assert ledger.owner_for_run(run) is None
        truth[field] = previous
    truth["current"] = object()
    assert ledger.owner_for_run(run) is None


def test_proof_provider_failure_is_safe_and_preserves_reservation(caplog):
    run = object()

    def resolver(candidate):
        raise RuntimeError("private proof payload")

    ledger, _ = ledger_with(
        Snapshot(hold("raid", {101, 102}, scope="session", retained=True)), proof=resolver,
    )
    with caplog.at_level(logging.WARNING):
        assert ledger.owner_for_run(run) is None
    assert ledger.holder(101) == "raid"
    assert not ledger.may_move("raid", 101)
    assert "body_run_proof_unavailable" in caplog.text
    assert "private proof payload" not in caplog.text
