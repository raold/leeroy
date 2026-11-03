from collections import Counter
from dataclasses import replace
import random

import pytest

from leeroy_mind.guild.comp import (
    TARGETS, Candidate, Leads, NoTankError, candidates_from, choose_leads, select_comp,
)
from .fakes import member, persona


def candidate(guid, role="damage", **over):
    cls, spec = {"tank": (1, 2), "healer": (5, 0), "damage": (8, 1)}[role]
    defaults = dict(guid=guid, name=f"Member{guid}", cls=cls, spec_tab=spec, role=role,
                    dc_tank=role == "tank", guild_role="raider", officer_title="",
                    bench_streak=0, attended_30d=0, available=True)
    defaults.update(over)
    return Candidate(**defaults)


def pool(tanks, healers, damage, **over):
    result = []
    for role, count in (("tank", tanks), ("healer", healers), ("damage", damage)):
        for index in range(count):
            fields = {"cls": (3, 4, 6, 7, 8, 9, 11)[index % 7]} if role == "damage" else {}
            fields.update(over)
            result.append(candidate(101 + len(result), role, **fields))
    return result


def select(cands, size=10, *, required=(), human=True, seed=1):
    return select_comp(size, cands, must_include=required, human_in_raid=human, seed=seed)


def leads(cands, **over):
    args = dict(requested=None, human_leads=False, human_guid=1)
    args.update(over)
    return choose_leads(cands, **args)


def test_ten_man_roles_with_human():
    result = select(pool(4, 5, 12))
    assert TARGETS == {10: (2, 3, 5), 25: (3, 6, 16)}
    assert Counter(result.roles.values()) == {"tank": 2, "healer": 3, "damage": 4}
    assert len(result.members) == 9 and len(set(result.members)) == 9
    assert result.shortfall == {}


def test_twenty_five_without_human():
    result = select(pool(4, 8, 20), 25, human=False)
    assert Counter(result.roles.values()) == {"tank": 3, "healer": 6, "damage": 16}
    assert len(result.members) == 25 and result.shortfall == {}


def test_mandatory_social_tank_keeps_its_role_slot():
    cands = pool(4, 5, 12)
    cands[0] = replace(cands[0], guild_role="social", attended_30d=100)
    result = select(cands, required=[101])
    assert 101 in result.members and result.roles[101] == "tank"
    assert Counter(result.roles.values())["tank"] == 2


def healer_contest(left, right):
    return [candidate(101, "tank"), candidate(102, "tank"),
            candidate(201, "healer"), candidate(202, "healer"), left, right,
            *[candidate(301 + i, cls=3 + i) for i in range(5)]]


def test_bench_streak_breaks_equal_role_priority_before_seed():
    cands = healer_contest(candidate(203, "healer"), candidate(204, "healer", bench_streak=1))
    for seed in (0, 1, 47):
        result = select(cands, required=[201, 202], seed=seed)
        assert 204 in result.members and 203 in result.bench


def test_fewer_raids_wins_when_role_and_bench_streak_tie():
    cands = healer_contest(candidate(203, "healer", attended_30d=5),
                           candidate(204, "healer", attended_30d=2))
    result = select(cands, required=[201, 202])
    assert 204 in result.members and 203 in result.bench


@pytest.mark.parametrize(("preferred", "other"), [("officer", "raider"), ("raider", "social"), ("officer", "social")])
def test_guild_rank_precedes_bench_streak_and_attendance(preferred, other):
    cands = healer_contest(candidate(203, "healer", guild_role=preferred, attended_30d=100),
                           candidate(204, "healer", guild_role=other, bench_streak=50))
    result = select(cands, required=[201, 202])
    assert 203 in result.members and 204 not in result.members


@pytest.mark.parametrize(("size", "cap"), [(10, 2), (25, 3)])
def test_damage_class_cap_prefers_other_classes_while_they_remain(size, cap):
    cands = pool(3, 6, 0)
    mages = [candidate(301 + i, cls=8, guild_role="officer") for i in range(6)]
    others = [candidate(401 + i, cls=(3, 4, 6, 7, 9, 11)[i % 6]) for i in range(20)]
    result = select([*cands, *mages, *others], size, human=False)
    assert sum(guid in result.members for guid in range(301, 307)) == cap


def test_damage_class_cap_relaxes_when_only_that_class_remains():
    cands = [*pool(2, 3, 0), *[candidate(301 + i, cls=8) for i in range(12)]]
    result = select(cands)
    assert len(result.members) == 9
    assert sum(result.roles[g] == "damage" for g in result.members) == 4


def test_damage_cap_does_not_count_tank_or_healer_class_seats():
    cands = [candidate(101, "tank", cls=2), candidate(102, "tank", cls=2),
             candidate(201, "healer", cls=2), candidate(202, "healer", cls=2),
             candidate(203, "healer", cls=2), candidate(301, cls=2, guild_role="officer"),
             candidate(302, cls=2, guild_role="officer"), candidate(303, cls=8), candidate(304, cls=9)]
    result = select(cands)
    assert {301, 302} <= set(result.members)


def test_shortfall_filled_with_damage():
    result = select(pool(2, 1, 12))
    assert result.shortfall == {"healer": 2} and len(result.members) == 9
    assert Counter(result.roles.values()) == {"tank": 2, "healer": 1, "damage": 6}


def test_one_real_tank_can_run_short_and_extras_fill_without_overbooking():
    result = select(pool(1, 3, 12))
    assert result.shortfall == {"tank": 1}
    assert len(result.members) == 9 and Counter(result.roles.values())["damage"] == 5


def test_small_pool_keeps_honest_shortfall_and_no_invented_members():
    result = select(pool(1, 1, 1))
    assert len(result.members) == 3 and result.bench == []
    assert result.shortfall == {"tank": 1, "healer": 2, "damage": 3}


def test_extra_roles_fill_empty_damage_seats_as_last_resort():
    result = select(pool(3, 5, 0), human=False)
    assert len(result.members) == 8
    assert result.shortfall == {"damage": 5}


def test_no_tank_raises_even_when_other_roles_fill_all_seats():
    with pytest.raises(NoTankError):
        select(pool(0, 3, 10))
    with pytest.raises(NoTankError):
        select([replace(c, dc_tank=False) for c in pool(2, 3, 10)])


def test_unavailable_never_selected_or_benched():
    cands = pool(3, 4, 10)
    cands[0] = replace(cands[0], available=False, guild_role="officer", bench_streak=100)
    cands.append(candidate(999, available=False))
    result = select(cands)
    assert not {101, 999} & (set(result.members) | set(result.bench))


def test_same_seed_is_independent_of_input_order_and_global_rng():
    cands = pool(4, 5, 12)
    state = random.getstate()
    assert select(cands, seed=17) == select(reversed(cands), seed=17)
    assert random.getstate() == state
    assert len({tuple(select(cands, seed=n).members) for n in range(8)}) > 1


def test_bench_lists_only_available_raiders_and_officers_not_chosen():
    cands = pool(4, 5, 12)
    cands.extend([candidate(800, guild_role="social"), candidate(801, guild_role="officer"),
                  candidate(802, guild_role="officer", available=False)])
    result = select(cands)
    expected = {c.guid for c in cands if c.available and c.guild_role in {"officer", "raider"}}
    assert set(result.bench) == expected - set(result.members)
    assert set(result.members).isdisjoint(result.bench)


def test_mandatory_duplicates_and_identical_candidate_rows_count_once():
    cands = pool(2, 3, 10)
    result = select([*cands, cands[0]], required=[101, 101])
    assert len(result.members) == 9 and result.members.count(101) == 1


@pytest.mark.parametrize("required", [[999], [True]])
def test_unknown_or_invalid_mandatory_member_is_not_silently_dropped(required):
    with pytest.raises(ValueError, match="mandatory"):
        select(pool(2, 3, 10), required=required)


def test_unavailable_mandatory_member_and_capacity_overflow_are_explicit():
    cands = pool(2, 3, 10)
    with pytest.raises(ValueError, match="unavailable"):
        select([replace(c, available=False) if c.guid == 101 else c for c in cands], required=[101])
    with pytest.raises(ValueError, match="capacity"):
        select(cands, required=[c.guid for c in cands[:10]])


def test_mandatory_role_and_class_overflow_kept_with_explicit_shortfall():
    cands = [*pool(2, 3, 0), *[candidate(301 + i, cls=8) for i in range(6)]]
    mandatory = list(range(301, 307))
    result = select(cands, required=mandatory)
    assert set(mandatory) <= set(result.members)
    assert len(result.members) == 9
    assert Counter(result.roles.values()) == {"tank": 2, "healer": 1, "damage": 6}
    assert result.shortfall == {"healer": 2}


def test_mandatory_members_are_not_ejected_to_sneak_in_a_tank():
    cands = pool(2, 3, 10)
    mandatory = [c.guid for c in cands if c.role == "damage"][:9]
    with pytest.raises(NoTankError):
        select(cands, required=mandatory)


def test_conflicting_duplicate_guids_are_not_resolved_by_input_order():
    good = candidate(101, "tank")
    bad = replace(good, available=False)
    for cands in ([good, bad], [bad, good]):
        with pytest.raises(ValueError, match="conflicting"):
            select(cands)
        with pytest.raises(ValueError, match="conflicting"):
            leads(cands)


@pytest.mark.parametrize("size", [0, 20, True, 10.0])
def test_unsupported_sizes_fail_coherently(size):
    with pytest.raises(ValueError, match="size"):
        select(pool(2, 3, 10), size)


def lead_pool():
    return [candidate(101, "tank", guild_role="officer", officer_title="Raid Lead"),
            candidate(102, "healer", guild_role="officer"),
            candidate(104, guild_role="officer", officer_title="Raid Lead, DKP"),
            candidate(108, cls=3, guild_role="officer", officer_title="Raid Lead, Historian"),
            candidate(109, cls=6, guild_role="officer", officer_title="Raid Lead, Theorycraft"),
            candidate(107, "tank", cls=2, spec_tab=1, guild_role="officer")]


def test_standing_requested_and_human_leads_keep_tank_and_loot_independent():
    cands = lead_pool()
    assert leads(cands) == Leads(101, 101, 104)
    assert leads(cands, requested=102) == Leads(102, 101, 104)
    assert leads(cands, requested=102, human_leads=True, rotate=True) == Leads(1, 101, 104)


def test_rotation_uses_never_led_then_oldest_then_guid_excluding_standing():
    cands = lead_pool()
    assert leads(cands, rotate=True, last_led={104: 50.0, 108: 10.0}) == Leads(109, 101, 104)
    assert leads(cands, rotate=True, last_led={104: 50.0, 108: 10.0, 109: 30.0}) == Leads(108, 101, 104)
    assert leads(reversed(cands), rotate=True, last_led={104: 10.0, 108: 10.0, 109: 10.0}) == Leads(104, 101, 104)
    assert leads(cands, requested=102, rotate=True).lead_guid == 102


def test_standing_tank_tie_uses_guid_even_when_other_is_more_fair():
    cands = [*lead_pool(), candidate(100, "tank", guild_role="officer", officer_title="RAID LEAD", attended_30d=99)]
    assert leads(cands) == Leads(100, 100, 104)
    assert leads(cands, rotate=True).lead_guid == 101


def test_fallback_lead_is_rotation_then_tank_and_loot_follows_safely():
    cands = lead_pool()
    assert leads([c for c in cands if c.guid != 101]) == Leads(104, 107, 104)
    assert leads([c for c in cands if c.guid not in {101, 104, 108, 109}]) == Leads(107, 107, 107)
    without_loot = [c for c in cands if c.guid != 104]
    assert leads(without_loot, requested=102) == Leads(102, 101, 102)
    assert leads(without_loot, human_leads=True) == Leads(1, 101, 101)


def test_unavailable_nonofficer_and_unknown_requested_leads_are_ignored():
    cands = [*lead_pool(), candidate(201, officer_title="Raid Lead, DKP"),
             candidate(202, guild_role="officer", officer_title="Raid Lead", available=False)]
    for requested in (201, 202, 999, True):
        assert leads(cands, requested=requested) == Leads(101, 101, 104)


def test_missing_actual_tank_rejects_even_a_human_or_requested_leader():
    cands = [candidate(101, "tank", dc_tank=False, guild_role="officer", officer_title="Raid Lead")]
    with pytest.raises(NoTankError):
        leads(cands, human_leads=True)
    with pytest.raises(NoTankError):
        leads(cands, requested=101)


def test_tank_fallback_respects_officer_then_fairness_priority():
    cands = [candidate(101, "tank", bench_streak=20), candidate(102, "tank", guild_role="officer"),
             candidate(103, "tank", guild_role="officer", bench_streak=1)]
    assert leads(cands) == Leads(103, 103, 103)
    assert leads([c for c in cands if c.guild_role != "officer"]) == Leads(101, 101, 101)


def test_rotation_empty_falls_back_and_does_not_mutate_history():
    history = {101: 10.0}
    assert leads([lead_pool()[0]], rotate=True, last_led=history) == Leads(101, 101, 101)
    assert history == {101: 10.0}


def test_human_is_never_mistaken_for_the_bot_tank_or_loot_officer():
    cands = [*lead_pool(), candidate(1, "tank", guild_role="officer", officer_title="Raid Lead, DKP")]
    assert leads(cands, requested=1) == Leads(101, 101, 104)


def test_candidates_use_current_specs_and_preserve_fairness_fields():
    members = {101: member(101, "Arkon", 1, 2), 102: member(102, "Belra", 5, 1),
               103: member(103, "Cerin", 1, 0), 104: member(104, "Dara", 11, 1),
               105: member(105, "Elin", 11, 1), 106: member(106, "Fara", 1, -1)}
    personas = {guid: persona(guid, row.name, raid_role="tank") for guid, row in members.items()}
    personas[105] = persona(105, "Elin", raid_role="melee")
    result = {c.guid: c for c in candidates_from(members, personas, {101: 3}, {101: 7}, set())}
    assert [(result[g].role, result[g].dc_tank) for g in members] == [
        ("tank", True), ("healer", False), ("damage", False),
        ("tank", True), ("damage", False), ("tank", False)]
    assert result[101].bench_streak == 3 and result[101].attended_30d == 7
    assert result[102].bench_streak == 0 and result[102].attended_30d == 0


@pytest.mark.parametrize("map_id", [30, 489, 529, 559, 562, 566, 572, 607, 617, 618, 628])
def test_candidates_on_every_battleground_map_are_unavailable(map_id):
    result = candidates_from([member(101, "Arkon", 1, 2, map=map_id)],
                             {101: persona(101, "Arkon")}, {}, {}, set())
    assert len(result) == 1 and not result[0].available


def test_candidates_filter_missing_persona_and_mark_offline_low_level_or_away_unavailable():
    members = [member(101, "Arkon", 1, 2), member(102, "Belra", 5, 0, online=False),
               member(103, "Cerin", 8, 1, level=79), member(104, "Dara", 8, 1),
               member(105, "Elin", 8, 1)]
    personas = {m.guid: persona(m.guid, m.name) for m in members if m.guid != 105}
    result = candidates_from(reversed(members), personas, {}, {}, {104})
    assert [c.guid for c in result] == [101, 102, 103, 104]
    assert [c.available for c in result] == [True, False, False, False]
    ready = candidates_from(members, personas, {}, {}, set())
    assert next(c for c in ready if c.guid == 104).available


def test_select_does_not_mutate_candidates_required_list_or_targets():
    cands = pool(2, 3, 10)
    original = list(cands)
    required = [101, 101]
    select(cands, required=required)
    assert cands == original and required == [101, 101]
    assert TARGETS == {10: (2, 3, 5), 25: (3, 6, 16)}
