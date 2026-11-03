import dataclasses
from types import SimpleNamespace

import pytest

from leeroy_mind.guild.roles import (
    is_achievement_hunter, is_cook, is_loot_officer, is_pvp_lead, is_raid_lead,
    premade_spec_no, reliability, role_of, spec_tab_for, stat_family,
)
from leeroy_mind.guild.upgrades import ItemInfo, upgrade_gain


def card(*, title="", guild_role="raider", score=5):
    return SimpleNamespace(play=SimpleNamespace(officer_title=title, guild_role=guild_role),
                           big_five=SimpleNamespace(conscientiousness=SimpleNamespace(score=score)))


@pytest.mark.parametrize(("cls", "tab", "persona_role", "expected"), [
    (1, 2, None, "tank"), (1, 0, "tank", "damage"),
    (11, 1, "tank", "tank"), (11, 1, "melee", "damage"), (11, 2, None, "healer"),
    (6, 0, None, "tank"), (6, 1, "tank", "damage"), (5, 0, None, "healer"),
    (5, 1, None, "healer"), (5, 2, "healer", "damage"), (2, 0, None, "healer"),
    (2, 1, None, "tank"), (7, 1, None, "damage"), (7, 2, None, "healer"),
    (3, -1, "ranged", "damage"), (1, -1, "tank", "tank"), (5, -1, "healer", "healer"),
    (8, -1, None, "damage"),
])
def test_role_follows_actual_talents_except_missing_tabs_and_feral(cls, tab, persona_role, expected):
    assert role_of(cls, tab, persona_role) == expected


@pytest.mark.parametrize(("cls", "tab", "role", "expected"), [
    (1, 2, "tank", "str"), (11, 1, "tank", "agi"), (7, 1, "damage", "agi"),
    (7, 0, "damage", "int"), (2, 0, "healer", "int"), (2, 2, "damage", "str"),
    (2, 1, "tank", "str"), (6, 1, "damage", "str"), (4, 0, "damage", "agi"),
    (3, 1, "damage", "agi"), (11, 0, "damage", "int"), (11, 2, "healer", "int"),
])
def test_stat_family(cls, tab, role, expected):
    assert stat_family(cls, tab, role) == expected


def test_title_responsibilities_and_reliability():
    assert is_raid_lead(card(title="Raid Lead", guild_role="officer"))
    assert not is_raid_lead(card(title="raid lead"))
    assert is_loot_officer(card(title="Loot and DKP", guild_role="officer"))
    assert not is_loot_officer(card(title="Loot and DKP"))
    assert is_cook(card(title="Guild Baker")) and is_cook(card(title="Raid Chef"))
    assert is_cook(card(title="cook")) and not is_cook(card(title="cookbook collector"))
    assert is_pvp_lead(card(title="Melee & PvP Lead", guild_role="officer"))
    assert not is_pvp_lead(card(title="PvP Lead"))
    assert is_achievement_hunter(card(title="Achievement Hunter"))
    assert reliability(card(score=9)) == 9


@pytest.mark.parametrize(("cls", "specs"), [
    (1, ("Arms", "Fury", "Protection")), (2, ("Holy", "Protection", "Retribution")),
    (3, ("Beast Mastery", "Marksmanship", "Survival")),
    (4, ("Assassination", "Combat", "Subtlety")), (5, ("Discipline", "Holy", "Shadow")),
    (6, ("Blood", "Frost", "Unholy")), (7, ("Elemental", "Enhancement", "Restoration")),
    (8, ("Arcane", "Fire", "Frost")), (9, ("Affliction", "Demonology", "Destruction")),
    (11, ("Balance", "Feral", "Restoration")),
])
def test_standard_talent_order(cls, specs):
    assert [spec_tab_for(cls, spec, None) for spec in specs] == [0, 1, 2]
    assert spec_tab_for(cls, "Unknown", "tank") is None


def test_premade_uses_first_pve_match_and_preserves_configured_indexes():
    premade = {
        1: ["arms pve", "fury pve", "prot pve", "arms pvp"],
        9: ["affli pve", "demo pve", "destro pve", "affli pvp"],
        11: ["balance pve", "bear pve", "resto pve", "cat pve", "balance pvp"],
    }
    assert premade_spec_no(1, "Protection", "tank", premade) == 2
    assert premade_spec_no(11, "Feral", "tank", premade) == 1
    assert premade_spec_no(11, "Feral", "melee", premade) == 3
    assert premade_spec_no(11, "Restoration", "healer", premade) == 2
    assert [premade_spec_no(9, spec, "ranged", premade) for spec in (
        "Affliction", "Demonology", "Destruction")] == [0, 1, 2]
    assert premade_spec_no(1, "Unknown", "melee", premade) is None
    assert premade_spec_no(5, "Holy", "healer", premade) is None
    assert premade_spec_no(1, "Arms", "melee", {1: ["arms pvp", "", "arms pve", "arms pve"]}) == 2
    assert premade_spec_no(1, "Arms", "melee", {1: ["arms pveish"]}) is None


PLATE_CHEST = ItemInfo(1, "Testplate", 4, 264, 5, 4, 4, -1, frozenset({4}))


def test_upgrade_gain_plan_examples():
    assert upgrade_gain(PLATE_CHEST, 1, "str", {4: 251}) == 13
    assert upgrade_gain(PLATE_CHEST, 8, "int", {4: 200}) is None
    assert upgrade_gain(ItemInfo(2, "Testrobe", 4, 264, 20, 4, 1, -1, frozenset({5, 45})),
                        1, "str", {}) is None
    assert upgrade_gain(ItemInfo(3, "Testring", 4, 264, 11, 4, 0, -1, frozenset({7})),
                        8, "int", {10: 251, 11: 258}) == 13
    assert upgrade_gain(ItemInfo(4, "Testtrinket", 4, 264, 12, 4, 0, -1, frozenset()),
                        8, "int", {}) == 64
    assert upgrade_gain(ItemInfo(5, "Testaxe", 4, 264, 17, 2, 1, -1, frozenset({4})),
                        7, "int", {15: 200}) is None
    token = ItemInfo(6, "Testtoken", 4, 264, 0, 15, 0, 1 | 4 | 64, frozenset())
    assert upgrade_gain(token, 3, "agi", {0: 251, 4: 251}) == 13
    assert upgrade_gain(token, 8, "int", {0: 251}) is None
    assert upgrade_gain(ItemInfo(7, "Testshield", 4, 264, 14, 4, 6, -1, frozenset({5})),
                        7, "int", {16: 251}) == 13
    assert upgrade_gain(ItemInfo(8, "Testshield2", 4, 264, 14, 4, 6, -1, frozenset({4})),
                        6, "str", {16: 200}) is None
    assert upgrade_gain(PLATE_CHEST, 1, "str", {4: 264}) is None


@pytest.mark.parametrize(("cls", "subclass"), [(1, 4), (2, 4), (6, 4), (3, 3), (7, 3),
                                               (4, 2), (11, 2), (5, 1), (8, 1), (9, 1)])
def test_body_armour_matches_class_not_a_lower_armour_type(cls, subclass):
    item = dataclasses.replace(PLATE_CHEST, item_subclass=subclass, stats=frozenset())
    assert upgrade_gain(item, cls, "int", {4: 251}) == 13
    wrong = dataclasses.replace(item, item_subclass=subclass % 4 + 1)
    assert upgrade_gain(wrong, cls, "int", {4: 251}) is None


@pytest.mark.parametrize(("cls", "subclass"), [(2, 7), (11, 8), (7, 9), (6, 10)])
def test_relics_are_class_specific(cls, subclass):
    relic = ItemInfo(9, "Testrelic", 4, 264, 28, 4, subclass, -1, frozenset())
    assert upgrade_gain(relic, cls, "str", {17: 251}) == 13
    assert upgrade_gain(relic, 8, "int", {17: 251}) is None


@pytest.mark.parametrize(("cls", "subclass", "allowed"), [
    (1, 18, True), (2, 10, False), (3, 3, True), (4, 1, False), (5, 19, True),
    (6, 19, False), (7, 10, True), (8, 7, True), (9, 13, False), (11, 6, True),
])
def test_weapon_proficiencies(cls, subclass, allowed):
    item = ItemInfo(10, "Testweapon", 4, 264, 21, 2, subclass, -1, frozenset())
    assert upgrade_gain(item, cls, "int", {15: 251}) == (13 if allowed else None)


def test_stat_fit_masks_and_dual_slots():
    ring = ItemInfo(11, "Testring", 4, 264, 11, 4, 0, -1, frozenset({45}))
    assert upgrade_gain(ring, 8, "int", {10: 260, 11: 251}) == 13
    assert upgrade_gain(ring, 1, "str", {10: 200}) is None
    assert upgrade_gain(dataclasses.replace(ring, stats=frozenset({4, 5})), 1, "str", {}) == 64
    assert upgrade_gain(dataclasses.replace(ring, allowable_class=1), 8, "int", {}) is None
    assert upgrade_gain(dataclasses.replace(ring, allowable_class=0), 1, "str", {}) is None
    assert upgrade_gain(ring, 0, "int", {}) is None
    assert upgrade_gain(ring, 8, "unknown", {}) is None
    assert upgrade_gain(dataclasses.replace(ring, inventory_type=18), 8, "int", {}) is None


def test_token_mean_and_empty_baseline():
    token = ItemInfo(12, "Testtoken", 4, 264, 0, 15, 0, -1, frozenset())
    assert upgrade_gain(token, 8, "int", {0: 251, 4: 258}) == 10
    assert upgrade_gain(token, 8, "int", {}) == 64
    assert upgrade_gain(token, 8, "int", {0: 277}) is None
