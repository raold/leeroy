"""The loot master never offers a drop to a member whose race or class cannot use it (VoA,
2026-11-25: Alliance-only Malfurion's Handguards offered to a Horde bidder, refused not_eligible)."""

import json
from dataclasses import replace

import pytest

from leeroy_mind.guild.armory import parse_items
from leeroy_mind.guild.upgrades import ItemInfo, can_use

from .fakes import FakeArmory
from .test_session import _run_async, advance, drop, items_charged, loot_states, opened, set_balance, setup  # noqa: F401

ALLIANCE = 1101  # human, dwarf, night elf, gnome, draenei


def _chest(item_id=50001, race=-1, cls=-1):
    return ItemInfo(item_id, "Testplate", 4, 264, 5, 4, 4, cls, frozenset({4}), allowable_race=race)


def _bidders(ports):
    return [[b["guid"] for b in json.loads(value)["bidders"]] for (value,) in
            ports.conn.execute("SELECT value FROM meta WHERE key GLOB 'guild:loot:*' ORDER BY key")]


def _arm(setup, item):
    ports, _, armory, _ = setup
    armory._items[item.item_id] = item
    armory._equipped.update({105: {4: 200}, 106: {4: 200}, 102: {}})
    ports.members[105] = replace(ports.members[105], race=2)  # orc, Horde
    ports.members[106] = replace(ports.members[106], race=1)  # human, Alliance
    set_balance(ports, 105, 100)
    set_balance(ports, 106, 100)


@_run_async
async def test_alliance_only_item_is_never_offered_to_a_horde_bidder(setup):
    ports, _, _, _ = setup
    _arm(setup, _chest(race=ALLIANCE))
    session = await opened(setup)
    await session.on_loot_drop(drop())
    assert 105 not in _bidders(ports)[0] and 106 in _bidders(ports)[0]
    await advance(ports, 20)
    await session.tick()
    assert [c.to_guid for c in ports.sent] == [106]


@_run_async
async def test_alliance_only_item_with_only_horde_bidders_ends_unclaimed(setup):
    ports, _, _, _ = setup
    _arm(setup, _chest(race=ALLIANCE))
    for guid in (101, 102, 103, 104, 105, 106, 107):
        ports.members[guid] = replace(ports.members[guid], race=2)
    session = await opened(setup)
    await session.on_loot_drop(drop())
    await advance(ports, 20)
    await session.tick()
    assert loot_states(ports) == ["unclaimed"] and ports.sent == [] and items_charged(ports) == []


@_run_async
async def test_a_both_faction_item_is_still_offered_to_both(setup):
    ports, _, _, _ = setup
    _arm(setup, _chest(race=-1))
    session = await opened(setup)
    await session.on_loot_drop(drop())
    assert {105, 106} <= set(_bidders(ports)[0])
    _arm(setup, _chest(item_id=50002, race=1101 | 690))
    await session.on_loot_drop(drop(item=50002, index=1))
    assert {105, 106} <= set(_bidders(ports)[1])


@_run_async
async def test_a_class_restricted_item_skips_other_classes(setup):
    ports, _, _, _ = setup
    _arm(setup, _chest(cls=1 << 1))  # paladin only
    session = await opened(setup)
    await session.on_loot_drop(drop())
    assert 105 not in _bidders(ports)[0] and 101 not in _bidders(ports)[0]  # warriors


@_run_async
async def test_a_class_restricted_token_goes_only_to_its_classes(setup):
    ports, _, _, _ = setup
    token = ItemInfo(40616, "Helm of the Fallen Champion", 4, 80, 0, 15, 0, 274, frozenset())
    _arm(setup, token)
    session = await opened(setup)
    await session.on_loot_drop(drop(item=40616))
    guids = _bidders(ports)[0]
    assert 102 in guids and not {101, 105, 106} & set(guids)  # priest yes, warriors and DK no


def test_can_use_masks():
    item = _chest(race=ALLIANCE, cls=1 << 0)
    assert can_use(item, 1, 1) and not can_use(item, 2, 1) and not can_use(item, 1, 2)
    assert can_use(_chest(race=0, cls=0), 2, 9) and can_use(_chest(), 10, 11)


def test_parse_items_reads_the_race_mask_as_the_last_column():
    row = ["1001", "Testplate", "4", "264", "5", "4", "4", "-1", "4", "7", "NULL",
           "0", "0", "0", "0", "0", "0", "0", "NULL", "0", "1101"]
    assert parse_items([row])[1001].allowable_race == 1101
    assert parse_items([row[:20]])[1001].allowable_race == -1
