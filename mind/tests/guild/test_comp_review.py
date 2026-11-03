"""Independent composition checks at the leader/roster boundary."""

from collections import Counter
from dataclasses import replace

import pytest

from leeroy_mind.guild.comp import (
    TARGETS, Candidate, Leads, NoTankError, candidates_from, choose_leads, select_comp,
)
from .fakes import member, persona


def _candidate(guid, role="damage", **overrides):
    cls, tab = {"tank": (1, 2), "healer": (5, 0), "damage": (8, 1)}[role]
    values = dict(guid=guid, name=f"Member{guid}", cls=cls, spec_tab=tab, role=role,
                  dc_tank=role == "tank", guild_role="raider", officer_title="",
                  bench_streak=0, attended_30d=0, available=True)
    values.update(overrides)
    return Candidate(**values)


def _pool():
    return [
        _candidate(101, "tank", guild_role="officer", officer_title="Raid Lead"),
        _candidate(102, "tank"),
        _candidate(201, "healer", guild_role="officer"),
        _candidate(202, "healer"), _candidate(203, "healer"),
        _candidate(301, guild_role="officer", officer_title="Loot and DKP"),
        _candidate(302, guild_role="officer", officer_title="Raid Lead"),
        *[_candidate(400 + index, cls=(3, 4, 6, 7, 8, 9, 11)[index % 7]) for index in range(12)],
    ]


def _leads(cands, **overrides):
    values = dict(requested=None, rotate=False, human_leads=False, human_guid=1)
    values.update(overrides)
    return choose_leads(cands, **values)


@pytest.mark.parametrize(("requested", "rotate", "human_leads"), [
    (201, False, False), (None, True, False), (None, False, True),
])
def test_review_chosen_responsibilities_survive_selection_when_required(
        requested, rotate, human_leads):
    cands = _pool()
    options = dict(requested=requested, rotate=rotate, human_leads=human_leads)
    leaders = _leads(cands, **options)
    required = [guid for guid in (leaders.lead_guid, leaders.tank_guid, leaders.loot_guid) if guid != 1]
    comp = select_comp(10, cands, must_include=required, human_in_raid=True, seed=9)
    assert set(required) <= set(comp.members)
    assert len(comp.members) == len(set(comp.members)) == 9
    selected = [candidate for candidate in cands if candidate.guid in comp.members]
    assert _leads(selected, **options) == leaders
    assert comp.roles[leaders.tank_guid] == "tank"
    assert set(comp.members) == set(comp.roles)
    assert set(comp.members).isdisjoint(comp.bench)


@pytest.mark.parametrize(("size", "human"), [(10, True), (10, False), (25, True), (25, False)])
def test_review_full_mandatory_capacity_keeps_every_member_and_honest_role_deficits(size, human):
    capacity = size - int(human)
    mandatory = [_candidate(101, "tank", guild_role="social")]
    mandatory += [_candidate(200 + index, guild_role="social") for index in range(capacity - 1)]
    extras = [_candidate(900 + index, "healer", guild_role="officer") for index in range(6)]
    required = [candidate.guid for candidate in mandatory]
    comp = select_comp(size, [*extras, *mandatory], must_include=[*required, required[0]],
                       human_in_raid=human, seed=4)
    assert set(comp.members) == set(required) and len(comp.members) == capacity
    assert comp.shortfall == {"tank": TARGETS[size][0] - 1, "healer": TARGETS[size][1]}
    assert set(comp.bench) == {candidate.guid for candidate in extras}
    assert Counter(comp.roles.values()) == {"tank": 1, "damage": capacity - 1}


def test_review_an_unchosen_real_tank_cannot_certify_a_persona_only_tank_comp():
    cands = [
        _candidate(101, "tank", guild_role="officer", dc_tank=False, spec_tab=-1),
        _candidate(102, "tank", guild_role="officer", dc_tank=False, spec_tab=-1),
        _candidate(103, "tank", guild_role="social"),
        *[_candidate(200 + index, "healer") for index in range(3)],
        *[_candidate(300 + index, cls=(3, 4, 8, 9)[index % 4]) for index in range(8)],
    ]
    with pytest.raises(NoTankError):
        select_comp(10, cands, must_include=[], human_in_raid=True, seed=0)
    leaders = _leads(cands)
    assert leaders == Leads(103, 103, 103)
    comp = select_comp(10, cands, must_include=[leaders.tank_guid], human_in_raid=True, seed=0)
    assert 103 in comp.members and comp.roles[103] == "tank"


def test_review_class_cap_overflow_from_mandatory_members_prefers_remaining_diversity():
    cands = [_candidate(101, "tank"), _candidate(102, "tank")]
    cands += [_candidate(200 + index, "healer") for index in range(3)]
    cands += [_candidate(300 + index, cls=8, guild_role="officer") for index in range(6)]
    cands += [_candidate(500, cls=3, guild_role="social")]
    comp = select_comp(10, cands, must_include=[300, 301, 302], human_in_raid=True, seed=2)
    assert {300, 301, 302, 500} <= set(comp.members)
    assert not {303, 304, 305} & set(comp.members)
    assert comp.shortfall == {} and len(comp.members) == 9


def test_review_seeded_fallback_is_stable_under_roster_order_and_unavailable_additions():
    cands = [candidate for candidate in _pool() if candidate.guid not in {202, 203}]
    required = [101, 301]
    expected = select_comp(10, cands, must_include=required, human_in_raid=True, seed=17)
    orders = [list(reversed(cands)), cands[4:] + cands[:4],
              [replace(_candidate(99, "tank"), available=False, guild_role="officer"), *cands]]
    for order in orders:
        assert select_comp(10, order, must_include=required, human_in_raid=True, seed=17) == expected
    assert expected.shortfall == {"healer": 2}
    assert len(expected.members) == 9


def test_review_rotation_never_uses_an_unavailable_or_nonofficer_lead():
    cands = [
        _candidate(101, "tank", guild_role="officer", officer_title="Raid Lead"),
        _candidate(201, guild_role="officer", officer_title="Raid Lead", available=False),
        _candidate(202, guild_role="officer", officer_title="Raid Lead"),
        _candidate(203, guild_role="officer", officer_title="Raid Lead"),
        _candidate(204, officer_title="Raid Lead, DKP"),
    ]
    history = {201: 1, 202: 20}
    assert _leads(cands, requested=204, rotate=True, last_led=history) == Leads(203, 101, 203)
    assert _leads(reversed(cands), requested=201, rotate=True, last_led=history) == Leads(203, 101, 203)
    assert history == {201: 1, 202: 20}


def test_review_battleground_and_away_tanks_cannot_be_made_leads_or_mandatory_members():
    members = {
        101: member(101, "Testguard", 1, 2, map=489),
        102: member(102, "Testbear", 11, 1),
        103: member(103, "Testpaladin", 2, 1),
    }
    personas = {guid: persona(guid, member_row.name, guild_role="officer",
                              officer_title="Raid Lead", raid_role="tank")
                for guid, member_row in members.items()}
    cands = candidates_from(members, personas, {101: 99}, {}, {102})
    assert _leads(cands, requested=101) == Leads(103, 103, 103)
    assert _leads(cands, requested=102) == Leads(103, 103, 103)
    for unavailable in (101, 102):
        with pytest.raises(ValueError, match="unavailable"):
            select_comp(10, cands, must_include=[unavailable], human_in_raid=True, seed=1)


def test_review_unknown_talents_do_not_promote_a_persona_tank_to_dc_tank():
    members = [member(101, "Testguard", 1, -1), member(102, "Testbear", 11, -1)]
    personas = {row.guid: persona(row.guid, row.name, raid_role="tank") for row in members}
    cands = candidates_from(members, personas, {}, {}, set())
    assert all(candidate.role == "tank" and not candidate.dc_tank for candidate in cands)
    with pytest.raises(NoTankError):
        _leads(cands, human_leads=True)
    with pytest.raises(NoTankError):
        select_comp(10, cands, must_include=[], human_in_raid=True, seed=1)
