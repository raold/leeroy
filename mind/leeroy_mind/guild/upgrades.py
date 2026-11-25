"""Deterministic gear eligibility and item-level gains for the planned raid role."""

from dataclasses import dataclass
from statistics import mean

from .tokens import tier_target


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
    bonding: int | None = None
    bag_family: int = 0
    allowable_race: int = -1


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
# What a stat says about who wears it (item_template stat types). Physical: attack power, ranged
# attack power, armour penetration, expertise. Caster: intellect, spirit, spell power, mp5,
# spell penetration and the old healing and spell damage stats. Tank: defense, dodge, parry,
# block and block value. Hit, crit, haste, stamina and resilience say nothing.
_PHYSICAL = frozenset({3, 4, 37, 38, 39, 44})
_CASTER = frozenset({5, 6, 41, 42, 43, 45, 47})
_TANK = frozenset({12, 13, 14, 15, 48})
_FAMILY_MARKS = {"str": _PHYSICAL, "agi": _PHYSICAL, "int": _CASTER}
_NOT_HEALING = frozenset({31, 37, 44})  # hit, expertise, armour penetration: no healer's stats
_TRINKET = 12
# The talent trees (class, tab) that fight with a two-handed weapon: Arms, Retribution, Blood,
# Unholy and Feral (the spot check of 2026-11-24: a one-hand dagger was an Arms warrior's wish).
# A one-hand or off-hand weapon, a shield or an off-hand item is none of theirs.
TWO_HANDED = frozenset({(1, 0), (2, 2), (6, 0), (6, 2), (11, 1)})
_ONE_HANDED = frozenset({13, 14, 21, 22, 23})  # one-hand, shield, main hand, off hand, held


def suits(item: ItemInfo, family: str, role: str | None, *, cls: int | None = None,
          spec_tab: int | None = None) -> bool:
    """The item's stats are hers (culture's loot wishes and the raid's bids, 2026-11-20: a physical
    damage trinket of crit rating alone, its attack power a proc, went to healers and casters):
    no stat of another family's and none of hers is no item of hers, a tank's stats are a tank's
    alone, a healer wears no hit, expertise or armour penetration, and a trinket must show a stat
    of hers: its worth is its proc, which nothing here reads, so a trinket of shared stats alone
    is nobody's (nothing is guessed). `role` None (a caller without one) skips the role rules.
    `cls` and `spec_tab`, her class and talent tree (the tree her talents say: guild/talents.py),
    when the caller knows them: a tree that fights with a two-hander (TWO_HANDED) wears no
    one-hand or off-hand weapon, shield or off-hand item."""
    if (cls, spec_tab) in TWO_HANDED and item.inventory_type in _ONE_HANDED:
        return False
    mine = _FAMILY_MARKS.get(family, frozenset()) | (_TANK if role == "tank" else frozenset())
    others = (_PHYSICAL | _CASTER) - mine
    if item.stats & others and not item.stats & mine:
        return False
    if role is not None and role != "tank" and item.stats & _TANK:
        return False
    if role == "healer" and item.stats & _NOT_HEALING:
        return False
    if item.item_class == 4 and item.inventory_type == _TRINKET and not item.stats & mine:
        return False
    return True


def can_use(item: ItemInfo, race: int, cls: int) -> bool:
    """False when the item's AllowableRace or AllowableClass mask (-1 or 0: everyone) shuts out a
    member of this race and class. A tier token is judged by its own masks: the piece it buys is
    her faction's, since each side's vendor sells its own."""
    for mask, bit in ((item.allowable_race, race), (item.allowable_class, cls)):
        if mask > 0 and type(bit) is int and bit > 0 and not mask & (1 << (bit - 1)):
            return False
    return True


def _tier_pieces(item: ItemInfo, cls: int) -> tuple[ItemInfo, ...] | None:
    """The tier pieces a token (tokens.TIER_TOKENS) buys a member of class `cls`: her armour, its
    item level, one per slot it buys for. A tier set is always its class's, so no stats are
    read (each spec has its own set). None when `item` is no tier token."""
    target = tier_target(item.item_id) if item.item_class == 15 else None
    if target is None:
        return None
    item_level, slots = target
    return tuple(ItemInfo(item.item_id, item.name, item.quality, item_level, slot, 4, _ARMOUR[cls],
                          item.allowable_class, frozenset()) for slot in slots)


def upgrade_gain(item: ItemInfo, cls: int, family: str, equipped: dict[int, int], *,
                 role: str | None = None, spec_tab: int | None = None) -> int | None:
    """The item levels `item` would gain her (a member of class `cls`, stat `family`, raid `role`
    and, when the caller knows it, talent tree `spec_tab`) over what she wears; None when it is no
    upgrade or none of hers (suits)."""
    if type(cls) is not int or cls not in _ARMOUR or family not in _FAMILY_STATS:
        return None
    if item.allowable_class != -1 and not item.allowable_class & (1 << (cls - 1)):
        return None
    pieces = _tier_pieces(item, cls)
    if pieces is not None:
        # A tier token is worth the tier piece it buys her, in the slot it gains her most
        return max((gain for piece in pieces
                    if (gain := upgrade_gain(piece, cls, family, equipped, role=role,
                                             spec_tab=spec_tab)) is not None), default=None)
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
    if not suits(item, family, role, cls=cls, spec_tab=spec_tab):
        return None
    slots = _SLOTS.get(item.inventory_type)
    if slots is None:
        return None
    gain = item.item_level - min(equipped.get(slot, 200) for slot in slots)
    return gain if gain > 0 else None
