"""Deterministic gear eligibility and item-level gains for the planned raid role."""

from dataclasses import dataclass
from statistics import mean


@dataclass(frozen=True, slots=True)
class ItemInfo:
    item_id: int
    name: str
    quality: int
    item_level: int
    inventory_type: int
    item_class: int
    item_subclass: int
    allowable_class: int
    stats: frozenset[int]


_ARMOUR = {1: 4, 2: 4, 3: 3, 4: 2, 5: 1, 6: 4, 7: 3, 8: 1, 9: 1, 11: 2}
_BODY = frozenset({1, 3, 5, 6, 7, 8, 9, 10, 20})
_RELIC = {2: 7, 11: 8, 7: 9, 6: 10}
_WEAPONS = {
    1: {0, 1, 2, 3, 4, 5, 6, 7, 8, 10, 13, 15, 16, 18},
    2: {0, 1, 4, 5, 6, 7, 8},
    3: {0, 1, 2, 3, 6, 7, 8, 10, 13, 15, 16, 18},
    4: {0, 2, 3, 4, 7, 13, 15, 16, 18},
    5: {4, 10, 15, 19},
    6: {0, 1, 4, 5, 6, 7, 8},
    7: {0, 1, 4, 5, 10, 13, 15},
    8: {7, 10, 15, 19},
    9: {7, 10, 15, 19},
    11: {4, 5, 6, 10, 13, 15},
}
_SLOTS = {
    1: (0,), 2: (1,), 3: (2,), 5: (4,), 20: (4,), 6: (5,), 7: (6,), 8: (7,),
    9: (8,), 10: (9,), 11: (10, 11), 12: (12, 13), 16: (14,), 13: (15, 16),
    17: (15,), 21: (15,), 14: (16,), 22: (16,), 23: (16,),
    15: (17,), 25: (17,), 26: (17,), 28: (17,),
}
_PRIMARY = frozenset({3, 4, 5, 45})
_FAMILY_STATS = {"str": frozenset({4}), "agi": frozenset({3}), "int": frozenset({5, 45})}


def upgrade_gain(item: ItemInfo, cls: int, family: str, equipped: dict[int, int]) -> int | None:
    if type(cls) is not int or cls not in _ARMOUR or family not in _FAMILY_STATS:
        return None
    if item.allowable_class != -1 and not item.allowable_class & (1 << (cls - 1)):
        return None
    if item.item_class == 15 and item.inventory_type == 0:
        baseline = round(mean(equipped.values())) if equipped else 200
        gain = item.item_level - baseline
        return gain if gain > 0 else None
    if item.item_class == 4:
        if item.inventory_type in _BODY and item.item_subclass != _ARMOUR[cls]:
            return None
        if item.item_subclass == 6 and cls not in {1, 2, 7}:
            return None
        if item.inventory_type == 28 and item.item_subclass != _RELIC.get(cls):
            return None
    elif item.item_class == 2:
        if item.item_subclass not in _WEAPONS[cls]:
            return None
    else:
        return None
    if item.stats & _PRIMARY and not item.stats & _FAMILY_STATS[family]:
        return None
    slots = _SLOTS.get(item.inventory_type)
    if slots is None:
        return None
    gain = item.item_level - min(equipped.get(slot, 200) for slot in slots)
    return gain if gain > 0 else None
