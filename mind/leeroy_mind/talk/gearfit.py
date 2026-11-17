"""The library's item facts, kept to what the speaker could wear or wield ([talk] coherence;
self-play audit, 2026-11-17: an Enhancement shaman asked what he wanted out of a raid named a
one-hand sword of item level 200 and cloth shoulders, because the lookup is one per line of his,
shared by every replier, and knows nobody's class).

`for_speaker` reads the library's own wording (library/world.py Item.slot: "one-hand sword",
"two-hand axe", "mail legs", "shield", "totem"; a loot table's "Name (251 cloth head)"):

- an item line ("Name: epic one-hand sword, item level 200.") is dropped when her class cannot
  use it, when it lists classes ("Classes: Mage.") without hers, or, for a member at the level
  cap, when its item level is under MIN_ITEM_LEVEL (never something she would want);
- a loot table loses the entries she cannot use (a weapon her class cannot wield, armour that
  is not her class's own type), and is dropped when none is left;
- every other line (a boss, a place, a quest) is left as written.

Pure: no I/O, no logging. Library text is public reference text."""
from __future__ import annotations

import re
from collections.abc import Sequence

MIN_ITEM_LEVEL = 200  # under this a level-80 raider would not name it as a wish
LEVEL_CAP = 80
ARMOUR = {"Warrior": "plate", "Paladin": "plate", "Death Knight": "plate", "Hunter": "mail",
          "Shaman": "mail", "Rogue": "leather", "Druid": "leather", "Mage": "cloth",
          "Priest": "cloth", "Warlock": "cloth"}
WEAPONS = {
    "Warrior": {"sword", "axe", "mace", "dagger", "fist weapon", "polearm", "staff", "bow",
                "gun", "crossbow", "thrown weapon", "shield"},
    "Paladin": {"sword", "axe", "mace", "polearm", "shield", "libram"},
    "Death Knight": {"sword", "axe", "mace", "polearm", "sigil"},
    "Hunter": {"sword", "axe", "dagger", "fist weapon", "polearm", "staff", "bow", "gun",
               "crossbow"},
    "Shaman": {"axe", "mace", "dagger", "fist weapon", "staff", "shield", "totem"},
    "Rogue": {"sword", "axe", "mace", "dagger", "fist weapon", "bow", "gun", "crossbow",
              "thrown weapon"},
    "Druid": {"mace", "dagger", "fist weapon", "polearm", "staff", "idol"},
    "Mage": {"sword", "dagger", "staff", "wand"},
    "Priest": {"mace", "dagger", "staff", "wand"},
    "Warlock": {"sword", "dagger", "staff", "wand"},
}
# classes that wield swords, axes and maces in one hand only
ONE_HAND_ONLY = frozenset({"Rogue", "Mage", "Priest", "Warlock"})
_KINDS = sorted({k for kinds in WEAPONS.values() for k in kinds} | {"spear"}, key=len,
                reverse=True)
_KIND = re.compile(r"\b(" + "|".join(re.escape(k) for k in _KINDS) + r")\b")
_ARMOUR = re.compile(r"\b(cloth|leather|mail|plate)\b")
# a loot table's entry: "Grim Edge (251 one-hand axe)", "Ice Robe (264 cloth chest; 95 Emblem)".
# Its descriptor is what is matched; its name is everything since the entry before it (or the
# table's "...: "), commas included ("Mantle of Fire, Ice and Shadow (251 cloth shoulder)").
_ENTRY = re.compile(r" \((?P<ilvl>\d{2,3}) (?P<desc>[a-z][a-z -]*?)(?:;[^()]*)?\)")
_SEPARATOR = re.compile(r"^[,;]\s+")
# an item's own line: "Avool's Sword: epic one-hand sword, item level 200."
_ITEM = re.compile(r"^[^:]{2,90}: (?:poor|common|uncommon|rare|epic|legendary|artifact|heirloom) "
                   r"(?P<desc>[a-z][a-z -]*?)(?:, item level (?P<ilvl>\d+))?(?:, requires level "
                   r"\d+)?\.")
_CLASSES = re.compile(r"\bClasses: (?P<names>[A-Za-z ,]+?)\.")


def usable(desc: str, class_name: str) -> bool:
    """A member of `class_name` could wear or wield what `desc` describes (the library's slot
    words). Anything that names no weapon kind and no armour type (a ring, a cloak, a trinket)
    is usable; an unknown class can use everything."""
    kinds = WEAPONS.get(class_name)
    if kinds is None:
        return True
    desc = desc.casefold()
    kind = _KIND.search(desc)
    if kind is not None:
        if kind.group(1) not in kinds:
            return False
        return not ("two-hand" in desc and kind.group(1) in ("sword", "axe", "mace")
                    and class_name in ONE_HAND_ONLY)
    armour = _ARMOUR.search(desc)
    return armour is None or armour.group(1) == ARMOUR[class_name]


def _wanted(desc: str, ilvl: str | None, class_name: str, level: int) -> bool:
    if not usable(desc, class_name):
        return False
    return not (ilvl and level >= LEVEL_CAP and int(ilvl) < MIN_ITEM_LEVEL)


def _table(line: str, class_name: str, level: int) -> str | None:
    """A loot table without the entries she cannot use; None when none is left. An entry is
    its whole name with its descriptor: a comma inside a name is part of the name."""
    marks = list(_ENTRY.finditer(line))
    # the first entry's name starts after the table's last ": " before its descriptor
    start = line.rfind(": ", 0, marks[0].start())
    begin = start + 2 if start >= 0 else 0
    entries, at = [], begin
    for m in marks:
        name = _SEPARATOR.sub("", line[at:m.start()])
        entries.append((name + m.group(0), m))
        at = m.end()
    keep = [text for text, m in entries if _wanted(m["desc"], m["ilvl"], class_name, level)]
    if len(keep) == len(entries):
        return line
    if not keep:
        return None
    return line[:begin] + ", ".join(keep) + line[marks[-1].end():]


def for_speaker(lines: Sequence[str], class_name: str, *, level: int = LEVEL_CAP) -> list[str]:
    """`lines` (LibraryFacts' fact lines for his line) as this speaker's prompt shows them
    (module docstring). An unknown class keeps every line."""
    if class_name not in WEAPONS:
        return list(lines)
    out = []
    for line in lines:
        item = _ITEM.match(line)
        if item is not None:
            listed = _CLASSES.search(line)
            if listed is not None and class_name not in {
                    n.strip() for n in listed["names"].split(",")}:
                continue
            if not _wanted(item["desc"], item["ilvl"], class_name, level):
                continue
            out.append(line)
        elif _ENTRY.search(line):
            kept = _table(line, class_name, level)
            if kept is not None:
                out.append(kept)
        else:
            out.append(line)
    return out


__all__ = ["ARMOUR", "MIN_ITEM_LEVEL", "WEAPONS", "for_speaker", "usable"]
