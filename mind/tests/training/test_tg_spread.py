"""Synthetic weekly planning; no bridge, model or live database is opened."""

import asyncio
from collections import Counter
from dataclasses import replace
from datetime import datetime, timezone
import json
import random
from types import SimpleNamespace

import pytest

from leeroy_mind.testing.fake_clock import FakeClock
from leeroy_mind.training import spread, store
from leeroy_mind.training.applier import Applier
from leeroy_mind.training.levers import MemberInfo
from leeroy_mind.training.spread import (DEFERRED, HEALING_BASIS, WEEK_PREFIX, MemberPolicy,
    PolicySnapshot, RotationOption, SetupChange, SpreadSchedule, member_scores, plan_week,
    prestige, run_week, score_evidence, week_key)
from guild.fakes import member, persona
from .test_tg_store import context, record


class NoExplore(random.Random):
    """High-fidelity deterministic draws, with no 20% exploration event."""
    def random(self):
        return .99


def policy(n=8, **changes):
    members = {g: MemberPolicy(8, 1, "ranged", "fire", 10) for g in range(101, 101+n)}
    rotations = (RotationOption(11, 8, 1, "field"), RotationOption(12, 8, 1, "champion"))
    return replace(PolicySnapshot(members, rotations=rotations,
        premades={8: ("fire pve", "fire pve alternative", "frost pve")}), **changes)


def pure_inputs(n=8):
    groups = {(8, 1, "ranged"): list(range(101, 101+n))}
    scores = {g: float(g) for g in groups[8, 1, "ranged"]}
    setups = {g: SetupChange(g, vals={"wait for attack time": g-101}) for g in scores}
    return groups, scores, setups


def test_bottom_quarter_copies_top_quarter_and_does_not_mutate_inputs():
    groups, scores, setups = pure_inputs()
    original = dict(setups)
    result = plan_week(groups, scores, setups, policy(), NoExplore())
    assert {r.guid for r in result} == {101, 102}
    assert all(r.copied_from in {107, 108} for r in result)
    assert all(r.vals == setups[r.copied_from].vals for r in result)
    assert setups == original and groups == {(8, 1, "ranged"): list(range(101, 109))}
    with pytest.raises(TypeError):
        result[0].vals["wait for attack time"] = 4


def test_bottom_quarter_rounds_up_and_small_groups_wait():
    groups, scores, setups = pure_inputs(5)
    assert len(plan_week(groups, scores, setups, policy(5), NoExplore())) == 2
    groups, scores, setups = pure_inputs(2)
    assert plan_week(groups, scores, setups, policy(2), NoExplore()) == []


def test_prestige_exact_multipliers_and_directed_affinity(fake):
    fake.personas[108] = persona(108, "Tester", guild_role="officer", thinks_as="theorycrafter")
    fake.mentors[101] = 108
    fake.affinities[101, 108] = 50
    fake.affinities[108, 101] = 99
    assert prestige(101, 108, fake) == 18
    fake.affinities[101, 108] = -100
    assert prestige(101, 108, fake) == 12
    assert prestige(102, 108, fake) == 4


def test_mentor_above_copier_has_priority_even_outside_top_quarter():
    groups, scores, setups = pure_inputs()
    result = plan_week(groups, scores, setups, policy(mentors={101: 104, 102: 101}), NoExplore())
    assert result[0].copied_from == 104
    assert result[1].copied_from in {107, 108}


@pytest.mark.parametrize("conscientiousness,sloppy,expected", [(2, False, .2), (8, False, .8), (9, True, .5)])
def test_each_part_has_independent_seeded_fidelity(monkeypatch, conscientiousness, sloppy, expected):
    # Isolate fidelity from the separately tested exploration step.
    monkeypatch.setattr(spread, "_explore", lambda setup, *args: setup)
    groups, scores, setups = pure_inputs()
    snapshots = policy(mentors={101: 108})
    members = dict(snapshots.members)
    members[101] = replace(members[101], conscientiousness=conscientiousness, sloppy=sloppy)
    snapshots = replace(snapshots, members=members)
    setups[101] = SetupChange(101, 11, (), {"wait for attack time": 0}, 0)
    setups[108] = SetupChange(108, 12, ("+threat",), {"wait for attack time": 10}, 1)
    count = Counter()
    rng = random.Random(9123)
    for _ in range(1200):
        result = {r.guid: r for r in plan_week(groups, scores, setups, snapshots, rng)}
        actual = result.get(101, setups[101])
        for part in ("rotation", "toggles", "vals", "premade"):
            count[part] += getattr(actual, part) == getattr(setups[108], part)
    assert all(abs(count[part]/1200 - expected) < .06 for part in ("rotation", "toggles", "vals", "premade"))


def _changed_knobs(before, after):
    return (int(before.rotation != after.rotation) + int(before.premade != after.premade)
            + len(set(before.toggles) ^ set(after.toggles))
            + sum(before.vals.get(k) != after.vals.get(k) for k in before.vals.keys() | after.vals.keys()))


def test_theorycrafter_explores_exactly_one_legal_knob_instead_of_copying():
    groups, scores, setups = pure_inputs()
    snapshot = policy()
    members = dict(snapshot.members)
    members[101] = replace(members[101], theorycrafter=True)
    snapshot = replace(snapshot, members=members)
    for seed in range(100):
        result = {r.guid: r for r in plan_week(groups, scores, setups, snapshot, random.Random(seed))}[101]
        assert result.copied_from is None and result.explored
        assert _changed_knobs(setups[101], result) == 1
        assert spread._setup(101, result, members[101], snapshot) is not None
        if "wait for attack time" == result.explored:
            assert abs(result.vals[result.explored] - setups[101].vals[result.explored]) == 1


def test_other_copiers_explore_at_twenty_percent():
    groups, scores, setups = pure_inputs()
    snapshot = policy(mentors={101: 108})
    explored = 0
    rng = random.Random(200)
    for _ in range(1000):
        rows = {r.guid: r for r in plan_week(groups, scores, setups, snapshot, rng)}
        explored += bool(rows[101].explored)
    assert .15 < explored/1000 < .25


def test_diversity_counts_prospective_changes_and_redraws_source():
    groups, scores, setups = pure_inputs()
    for guid in (104, 105, 106):
        setups[guid] = replace(setups[108], guid=guid)
    # First copy may bring the source's four members to five. The second must
    # use the other top-quarter source; six of eight would exceed two thirds.
    result = plan_week(groups, scores, setups, policy(), NoExplore())
    assert [r.copied_from for r in result] == [108, 107]
    final = dict(setups) | {r.guid: r for r in result}
    assert max(Counter(spread._signature(s) for s in final.values()).values()) == 5


def test_fidelity_noop_does_not_get_another_source_attempt():
    groups, scores, setups = pure_inputs()
    snapshot = policy()
    members = {g: replace(p, conscientiousness=0) for g, p in snapshot.members.items()}
    assert plan_week(groups, scores, setups, replace(snapshot, members=members), random.Random(44)) == []


def test_diversity_counts_same_spec_members_in_other_roles():
    members = {g: MemberPolicy(11, 1, "melee", "feral", 10) for g in range(101, 104)}
    members.update({g: MemberPolicy(11, 1, "tank", "feral", 10) for g in range(104, 107)})
    snapshot = PolicySnapshot(members, rotations=(RotationOption(11, 11, 1, "field"),))
    groups = {(11, 1, "melee"): [101, 102, 103], (11, 1, "tank"): [104, 105, 106]}
    setups = {g: SetupChange(g, rotation=11 if g != 101 else None) for g in members}
    # Copying source103 would produce six identical setups across both roles.
    assert plan_week(groups, {101: 1, 102: 2, 103: 3}, setups, snapshot, NoExplore()) == []


@pytest.mark.parametrize("bad", [
    {"toggles": ["-threat"]}, {"toggles": ["+damage"]}, {"toggles": ["+threat", "+threat"]},
    {"vals": {"formation": "stock"}}, {"vals": {"PartyMaxSpread": 100}},
    {"vals": {"wait for attack time": True}}, {"vals": {"mana save level": 2}},
    {"rotation": 999}, {"rotation": True}, {"premade": 2}, {"premade": 99},
    {"guessed_provenance": "field"}, {"guid": 999},
])
def test_invalid_or_cross_role_setup_fails_closed(bad):
    groups, scores, setups = pure_inputs()
    setups[108] = bad
    assert plan_week(groups, scores, setups, policy(), NoExplore()) == []


def test_only_actual_field_champion_rotation_and_own_tab_premade():
    snapshot = policy(rotations=(RotationOption(11, 8, 1, "dummy"), RotationOption(12, 8, 2, "champion")))
    assert spread._setup(101, {"rotation": 11}, snapshot.members[101], snapshot) is None
    assert spread._setup(101, {"rotation": 12}, snapshot.members[101], snapshot) is None
    assert spread._setup(101, {"premade": 1}, snapshot.members[101], snapshot).premade == 1
    assert spread._setup(101, {"premade": 1}, snapshot.members[101], replace(snapshot, premades={})) is None


def test_no_unknown_persona_spec_or_role_defaults_and_no_tie_copying():
    groups, scores, setups = pure_inputs()
    snapshot = policy()
    for invalid in (replace(snapshot.members[101], main_spec="unknown"),
                    replace(snapshot.members[101], spec_tab=-1),
                    replace(snapshot.members[101], role="healer")):
        assert plan_week(groups, scores, setups, replace(snapshot, members=dict(snapshot.members) | {101: invalid}), NoExplore()) == []
    assert plan_week(groups, dict.fromkeys(scores, 0.0), setups, snapshot, NoExplore()) == []
    with pytest.raises(ValueError, match="one group"):
        plan_week({**groups, (8, 1, "melee"): [101, 102, 103]}, scores, setups, snapshot, NoExplore())


def _put_meta(fake, key, value):
    with fake.conn:
        fake.conn.execute("INSERT INTO meta(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                          (key, json.dumps(value)))


def _receipt(fake, week):
    row = fake.conn.execute("SELECT value FROM meta WHERE key=?", (WEEK_PREFIX + week,)).fetchone()
    return json.loads(row[0]) if row else None


def healing_pull(fake, ident, end, *, raid=1, kind="offline", basis=HEALING_BASIS,
                 complete=True, outcome="killed", received=None, members=None):
    rec = record(ident, complete=complete, outcome=outcome)
    guids = list(fake.members) if members is None else members
    rec.header = replace(rec.header, start_ms=int((end-100)*1000), end_ms=int(end*1000),
                         members=len(guids), healing_basis=basis)
    rec.members = tuple(replace(rec.members[0], guid=g, class_=5, spec_tab=1, tank=False,
        healing=(g-100)*100000, active_ms=100000, damage=999999, damage_boss=999999,
        taken=999999) for g in guids)
    rec.deaths, rec.tracks = (), {}
    store.put_pull(fake.conn, rec, context(kind=kind, raid_id=raid), dict.fromkeys(guids, "healer"),
                   received=end if received is None else received)


@pytest.fixture
def weekly(fake):
    fake.clock = FakeClock(datetime(2026, 11, 1, 13, tzinfo=timezone.utc))
    fake.cfg = replace(fake.cfg, enabled=True)
    now = fake.clock.now()
    for guid in range(101, 109):
        fake.members[guid] = member(guid, "Tester", 5, 1)
        fake.personas[guid] = persona(guid, "Tester", main_spec="holy", raid_role="healer", conscientiousness=10)
        store.put_setup(fake.conn, dict(guid=guid, since=now-86400,
            vals={"formation": ("arrow", "circle", "chaos", "line", "near", "far", "melee", "queue")[guid-101]}))
    with fake.conn:
        fake.conn.execute("INSERT INTO guild_raid(raid_id,started,ended,raid_key,size,heroic,offline,lead_guid,tank_guid,loot_guid) "
            "VALUES (1,?,?,'icc',10,0,1,101,101,101)", (now-40*86400, now-1))
    # Received observations genuinely span 35 days. Three pre-week kills make
    # the four current-week pulls farm without retroactive classification.
    healing_pull(fake, "old", now-35*86400)
    monday = datetime(2026, 10, 26, tzinfo=timezone.utc).timestamp()
    for i in range(3):
        healing_pull(fake, f"history-{i}", monday-1000+i*200)
    for i in range(4):
        healing_pull(fake, f"week-{i}", monday+1000+i*200)
    _put_meta(fake, "tg_first_pull", now-35*86400)
    _put_meta(fake, "tg_gate", {"phase": "passed", "verdict_ms": .5})
    applier = Applier(fake, SimpleNamespace(first_pull_ts=lambda: now-35*86400))
    schedule = SpreadSchedule(now, True, False, False, False, False, False,
        {g: MemberInfo(g, "healer", 5, 1) for g in fake.members}, {5: ("holy pve",)})
    return applier, schedule, week_key(fake.clock.local())


def test_effective_healing_scores_exact_context_class_spec_and_four_farm_pulls(fake, weekly):
    _, _, week = weekly
    evidence = score_evidence(fake.conn, week, now=fake.clock.now(), tz=timezone.utc)
    assert set(evidence) == set(fake.members)
    assert evidence[101]["score"] < 0 < evidence[108]["score"]
    assert all(len(r["pulls"]) == 4 and r["identity"] == (5, 1, "healer") for r in evidence.values())
    assert {r["pull"] for r in evidence[101]["pulls"]} == {f"week-{i}" for i in range(4)}
    assert all(r["basis"] == HEALING_BASIS for r in evidence[101]["pulls"])
    assert member_scores(fake.conn, week, now=fake.clock.now(), tz=timezone.utc)[108] == evidence[108]["score"]


@pytest.mark.parametrize("update", [
    "healing_basis='unknown'", "healing_basis='raw_healing'", "complete=0", "scoreable=0",
    "kind='benchmark'", "kind='other'", "outcome='aborted'", "raid_id=NULL",
    "context='631:1:0'", "received=9999999999", "members=99", "lead_guid=999",
])
def test_ineligible_pull_never_supplies_fourth_member_observation(fake, weekly, update):
    _, _, week = weekly
    with fake.conn:
        fake.conn.execute("UPDATE tg_pull SET " + update + " WHERE id='week-3'")
    assert member_scores(fake.conn, week, now=fake.clock.now(), tz=timezone.utc) == {}


@pytest.mark.parametrize("role,cls,tab", [("ranged", 8, 1), ("tank", 1, 2), ("melee", 4, 1), ("healer", 8, 1)])
def test_requested_damage_or_taken_never_becomes_a_rank(fake, weekly, role, cls, tab):
    _, _, week = weekly
    with fake.conn:
        fake.conn.execute("UPDATE tg_member SET role=?,class=?,spec_tab=? WHERE guid=108", (role, cls, tab))
    assert 108 not in member_scores(fake.conn, week, now=fake.clock.now(), tz=timezone.utc)


def test_corrupt_member_values_and_changed_spec_are_excluded(fake, weekly):
    _, _, week = weekly
    with fake.conn:
        fake.conn.execute("UPDATE tg_member SET healing=-1 WHERE guid=108 AND pull='week-3'")
        fake.conn.execute("UPDATE tg_member SET spec_tab=0 WHERE guid=107 AND pull='week-3'")
    scores = member_scores(fake.conn, week, now=fake.clock.now(), tz=timezone.utc)
    assert 108 not in scores and 107 not in scores and 106 in scores


def test_normalization_never_pools_other_classes_or_specs(fake, weekly):
    _, _, week = weekly
    with fake.conn:
        fake.conn.execute("UPDATE tg_member SET class=2,spec_tab=0,healing=healing*1000 WHERE guid>=105")
    scores = member_scores(fake.conn, week, now=fake.clock.now(), tz=timezone.utc)
    # Both classes have the same relative ranks, despite a thousandfold scale.
    assert scores[101] == pytest.approx(scores[105])
    assert scores[104] == pytest.approx(scores[108])
    with fake.conn:
        fake.conn.execute("UPDATE tg_member SET spec_tab=0 WHERE guid>=103 AND guid<=104")
    scores = member_scores(fake.conn, week, now=fake.clock.now(), tz=timezone.utc)
    assert scores[101] == pytest.approx(-1)
    assert scores[102] == pytest.approx(1)
    assert scores[103] == pytest.approx(-1)
    assert scores[104] == pytest.approx(1)


@pytest.mark.parametrize("column,value", [("healing", -1), ("healing", "unknown"),
                                         ("active_ms", -1), ("active_ms", 100001)])
def test_unknown_member_measurements_are_never_zero_filled(fake, weekly, column, value):
    _, _, week = weekly
    with fake.conn:
        fake.conn.execute("UPDATE tg_member SET " + column + "=? WHERE guid=108 AND pull='week-3'", (value,))
    assert 108 not in member_scores(fake.conn, week, now=fake.clock.now(), tz=timezone.utc)


def test_historical_week_and_iso_year_boundary(fake, weekly):
    assert week_key(datetime(2027, 1, 1)) == "2026-W53"
    assert member_scores(fake.conn, "2026-W43", now=fake.clock.now(), tz=timezone.utc) == {}
    with pytest.raises(ValueError):
        member_scores(fake.conn, "2026-W99", now=fake.clock.now(), tz=timezone.utc)


def test_farm_history_is_not_borrowed_from_future_wins(fake, weekly):
    _, _, week = weekly
    with fake.conn:
        fake.conn.execute("UPDATE tg_pull SET outcome='wiped' WHERE id IN ('history-0','history-1','history-2')")
    assert member_scores(fake.conn, week, now=fake.clock.now(), tz=timezone.utc) == {}


def test_week_runs_once_and_records_desired_provenance_without_social_or_application_claims(fake, weekly):
    applier, proof, week = weekly
    assert asyncio.run(run_week(fake, applier, week, schedule=lambda: proof, rng=NoExplore())) == 2
    receipt = _receipt(fake, week)
    assert receipt["state"] == "desired" and receipt["reason"] == DEFERRED
    cohort = json.loads(json.dumps(score_evidence(fake.conn, week, now=receipt["at"], tz=timezone.utc)))
    assert receipt["normalization_version"] == 1 and receipt["utc_offset_seconds"] == 0
    assert receipt["score_cohort"] == cohort
    assert receipt["cohort_kind"] == "rank_eligible_scores_requires_original_sql"
    assert len(receipt["score_cohort"]) == 8  # Retain the cohort, not only the two planned recipients.
    assert receipt["week_bounds"][1] - receipt["week_bounds"][0] == 7 * 86400
    assert receipt["metric_coverage"]["damage"].startswith("unavailable")
    assert len(receipt["changes"]) == 2
    for change in receipt["changes"]:
        assert change["application"] == "deferred"
        assert change["previous_setup"] and change["source_setup"]
        assert len(change["evidence"][str(change["copied_from"])]["pulls"]) == 4
    assert asyncio.run(run_week(fake, applier, week, schedule=lambda: proof, rng=NoExplore())) == 0
    assert fake.conn.execute("SELECT COUNT(*) FROM tg_setup").fetchone()[0] == 10
    assert fake.conn.execute("SELECT COUNT(*) FROM tg_setup WHERE applied IS NOT NULL").fetchone()[0] == 0
    assert not fake.sent and not fake.memories and not fake.proposed and not fake.said and not fake.claims


@pytest.mark.parametrize("field,value", [
    ("connected", False), ("connected", None), ("human_online", True), ("human_online", None),
    ("raid_active", True), ("raid_active", None), ("lab_active", True), ("benchmark_active", True),
    ("acquiring", True), ("observed_at", -1), ("observed_at", 9999999999),
])
def test_unknown_busy_or_stale_proof_blocks_planning(fake, weekly, field, value):
    applier, proof, week = weekly
    proof = replace(proof, **{field: value})
    assert asyncio.run(run_week(fake, applier, week, schedule=lambda: proof)) == 0
    assert _receipt(fake, week) is None


def test_missing_provider_and_actual_acting_readiness_cannot_be_bypassed(fake, weekly):
    applier, proof, week = weekly
    assert asyncio.run(run_week(fake, applier, week)) == 0
    applier.ledger.first_pull_ts = lambda: fake.clock.now()-27*86400
    assert asyncio.run(run_week(fake, applier, week, schedule=lambda: proof)) == 0
    assert _receipt(fake, week) is None


@pytest.mark.parametrize("condition", ["receipt_span", "gate", "owner_online", "disabled", "spread_off", "saturday", "before_noon", "open_raid", "retained_mutation"])
def test_all_weekly_admission_requirements_are_binding(fake, weekly, condition):
    applier, proof, week = weekly
    if condition == "receipt_span":
        with fake.conn:
            fake.conn.execute("UPDATE tg_pull SET received=? WHERE id='old'", (fake.clock.now()-86400,))
    elif condition == "gate":
        _put_meta(fake, "tg_gate", {"phase": "passed", "verdict_ms": 2})
    elif condition == "owner_online":
        fake.online = True
    elif condition == "disabled":
        fake.cfg = replace(fake.cfg, enabled=False)
    elif condition == "spread_off":
        fake.cfg = replace(fake.cfg, spread=False)
    elif condition in {"saturday", "before_noon"}:
        fake.clock = FakeClock(datetime(2026, 10, 31, 13, tzinfo=timezone.utc) if condition == "saturday"
                               else datetime(2026, 11, 1, 11, tzinfo=timezone.utc))
        proof = replace(proof, observed_at=fake.clock.now())
    elif condition == "open_raid":
        with fake.conn:
            fake.conn.execute("UPDATE guild_raid SET ended=NULL")
    else:
        with fake.conn:
            fake.conn.execute("INSERT INTO tg_applied(ts,run,layer,context,command,inverse) VALUES (0,'old','standing','0:0:0','{}','{}')")
    assert asyncio.run(run_week(fake, applier, week, schedule=lambda: proof)) == 0
    assert _receipt(fake, week) is None


def test_current_identity_and_holder_exclusion_are_not_defaults(fake, weekly):
    applier, proof, week = weekly
    # Leave only two eligible members: no population group may be formed.
    fake.holders.update(dict.fromkeys(range(103, 109), "other-owner"))
    assert asyncio.run(run_week(fake, applier, week, schedule=lambda: proof)) == 0
    fake.holders.clear()
    for guid in range(103, 109):
        fake.members[guid] = replace(fake.members[guid], spec_tab=0)
    assert asyncio.run(run_week(fake, applier, week, schedule=lambda: proof)) == 0


def test_changed_inputs_leave_durable_cancelled_week_without_setups(fake, weekly):
    applier, proof, week = weekly
    calls = 0
    def schedule():
        nonlocal calls
        calls += 1
        return proof if calls == 1 else replace(proof, acquiring=True)
    assert asyncio.run(run_week(fake, applier, week, schedule=schedule, rng=NoExplore())) == 0
    assert _receipt(fake, week)["state"] == "cancelled"
    assert fake.conn.execute("SELECT COUNT(*) FROM tg_setup").fetchone()[0] == 8
    assert asyncio.run(run_week(fake, applier, week, schedule=lambda: proof)) == 0


@pytest.mark.parametrize("change", ["holder", "identity", "latest_setup", "evidence", "rotation"])
def test_commit_rechecks_holder_identity_setup_candidates_and_score_sources(fake, weekly, change):
    applier, proof, week = weekly
    calls = 0
    def schedule():
        nonlocal calls
        calls += 1
        if calls == 2:
            if change == "holder":
                fake.holders[101] = "raid"
            elif change == "identity":
                fake.members[101] = replace(fake.members[101], spec_tab=0)
            elif change == "latest_setup":
                fake.conn.execute("UPDATE tg_setup SET vals='{}' WHERE guid=108")
            elif change == "evidence":
                fake.conn.execute("UPDATE tg_member SET healing=0 WHERE guid=108 AND pull='week-3'")
            else:
                fake.conn.execute("INSERT INTO tg_rotation(name,class,spec_tab,lines,origin,status,created) "
                                  "VALUES ('tg_synthetic',5,1,'[]','synthetic','field',0)")
        return proof
    assert asyncio.run(run_week(fake, applier, week, schedule=schedule, rng=NoExplore())) == 0
    assert _receipt(fake, week)["state"] == "cancelled"
    assert fake.conn.execute("SELECT COUNT(*) FROM tg_setup").fetchone()[0] == 8


def test_cancel_during_claim_rolls_back_receipt_and_rows_together(fake, weekly):
    applier, proof, week = weekly
    calls = 0
    def schedule():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise asyncio.CancelledError()
        return proof
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run_week(fake, applier, week, schedule=schedule, rng=NoExplore()))
    assert _receipt(fake, week) is None
    assert fake.conn.execute("SELECT COUNT(*) FROM tg_setup").fetchone()[0] == 8


def test_weekly_uses_installed_always_open_transaction_connection(fake, weekly):
    applier, proof, week = weekly
    assert fake.conn.autocommit is False and fake.conn.in_transaction
    assert asyncio.run(run_week(fake, applier, week, schedule=lambda: proof, rng=NoExplore())) == 2
    fake.conn.rollback()  # The operation already committed its rows and receipt together.
    assert _receipt(fake, week)["state"] == "desired"
    assert fake.conn.execute("SELECT COUNT(*) FROM tg_setup").fetchone()[0] == 10
