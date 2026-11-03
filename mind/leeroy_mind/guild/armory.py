"""Read-only character/world queries. Callers run these synchronous methods in a thread."""

import re
from pathlib import Path
from statistics import mean
from typing import Protocol

from leeroy_mind.personas.roster import DbInfo, RosterError, run_mysql

from .upgrades import ItemInfo


class Armory(Protocol):
    def items(self, ids) -> dict[int, ItemInfo]: ...
    def equipped(self, guids) -> dict[int, dict[int, int]]: ...
    def human_tier(self, guid) -> int: ...
    def name(self, guid) -> str | None: ...


def _id(value) -> int:
    if type(value) is not int or not 0 < value <= 0xFFFFFFFF:
        raise ValueError("armory ids must be positive uint32 integers")
    return value


def _ids(values):
    return tuple(sorted({_id(value) for value in values}))


def _chunks(values, size=500):
    for start in range(0, len(values), size):
        yield values[start:start + size]


def _numeric(value, *, nullable=False):
    if nullable and value == "NULL":
        return 0
    if not isinstance(value, str) or not re.fullmatch(r"-?[0-9]+", value):
        raise ValueError("invalid numeric column")
    return int(value)


def parse_items(rows) -> dict[int, ItemInfo]:
    found = {}
    for number, row in enumerate(rows, 1):
        try:
            if len(row) != 18:
                raise ValueError("expected 18 columns")
            entry = _id(_numeric(row[0]))
            values = [_numeric(value) for value in row[2:8]]
            stats = frozenset(value for field in row[8:] if (value := _numeric(field, nullable=True)))
            found[entry] = ItemInfo(entry, row[1], *values, stats)
        except (TypeError, ValueError):
            raise RosterError(f"item row {number}: invalid columns") from None
    return found


def parse_equipped(rows, items) -> dict[int, dict[int, int]]:
    found = {}
    for number, row in enumerate(rows, 1):
        try:
            if len(row) != 3:
                raise ValueError("expected 3 columns")
            guid, slot, entry = (_numeric(value) for value in row)
            _id(guid)
            _id(entry)
            if not 0 <= slot < 19:
                raise ValueError("invalid equipment slot")
        except (TypeError, ValueError):
            raise RosterError(f"equipment row {number}: invalid columns") from None
        if entry in items:
            found.setdefault(guid, {})[slot] = items[entry].item_level
    return found


def average_item_level(slots) -> float:
    levels = [level for slot, level in slots.items() if 0 <= slot < 19 and slot not in {3, 18}]
    return float(mean(levels)) if levels else 0.0


def read_premade_specs(conf: Path) -> dict[int, list[str]]:
    """Keep the configured numeric index, including holes; no PvP/PvE reordering."""
    found = {}
    for line in conf.read_text(encoding="utf-8", errors="replace").splitlines():
        match = re.match(r"^\s*Playerbots\.PremadeSpecName\.([0-9]+)\.([0-9]+)\s*=\s*(.*?)\s*$", line)
        if match is None:
            continue
        cls, index = int(match[1]), int(match[2])
        if cls not in {1, 2, 3, 4, 5, 6, 7, 8, 9, 11} or not 0 <= index <= 9:
            continue
        value = match[3]
        if value.startswith('"'):
            end = value.find('"', 1)
            if end < 0 or value[end + 1:].strip() and not value[end + 1:].lstrip().startswith("#"):
                continue
            value = value[1:end]
        else:
            value = value.split("#", 1)[0].strip()
        names = found.setdefault(cls, [])
        names.extend([""] * (index + 1 - len(names)))
        names[index] = value
    return found


class MysqlArmory:
    def __init__(self, chars: DbInfo, world: DbInfo, mysql_exe: Path):
        self.chars = chars
        self.world = world
        self.mysql_exe = mysql_exe
        self._items: dict[int, ItemInfo] = {}

    def items(self, ids) -> dict[int, ItemInfo]:
        wanted = _ids(ids)
        missing = tuple(entry for entry in wanted if entry not in self._items)
        for chunk in _chunks(missing):
            sql = ("SELECT entry, name, Quality, ItemLevel, InventoryType, class, subclass, AllowableClass, "
                   + ", ".join(f"stat_type{index}" for index in range(1, 11))
                   + " FROM item_template WHERE entry IN (" + ",".join(map(str, chunk)) + ")")
            fetched = parse_items(run_mysql(self.world, sql, self.mysql_exe))
            self._items.update({entry: item for entry, item in fetched.items() if entry in chunk})
        return {entry: self._items[entry] for entry in wanted if entry in self._items}

    def equipped(self, guids) -> dict[int, dict[int, int]]:
        wanted = _ids(guids)
        rows = []
        for chunk in _chunks(wanted):
            sql = ("SELECT ci.guid, ci.slot, ii.itemEntry FROM character_inventory ci "
                   "JOIN item_instance ii ON ii.guid = ci.item "
                   "WHERE ci.bag = 0 AND ci.slot < 19 AND ci.guid IN ("
                   + ",".join(map(str, chunk)) + ")")
            rows.extend(run_mysql(self.chars, sql, self.mysql_exe))
        entries = []
        for number, row in enumerate(rows, 1):
            try:
                if len(row) != 3:
                    raise ValueError("invalid equipment row")
                entries.append(_id(_numeric(row[2])))
            except (TypeError, ValueError):
                raise RosterError(f"equipment row {number}: invalid columns") from None
        equipped = parse_equipped(rows, self.items(entries))
        return {guid: equipped.get(guid, {}) for guid in wanted}

    def human_tier(self, guid) -> int:
        guid = _id(guid)
        rows = run_mysql(self.chars, "SELECT MAX(quest) FROM character_queststatus_rewarded "
                         f"WHERE guid = {guid} AND quest BETWEEN 66001 AND 66018", self.mysql_exe)
        if not rows or rows == [["NULL"]]:
            return 0
        try:
            if len(rows) != 1 or len(rows[0]) != 1:
                raise ValueError("invalid tier result")
            quest = _numeric(rows[0][0])
            if not 66001 <= quest <= 66018:
                raise ValueError("invalid tier quest")
        except (TypeError, ValueError):
            raise RosterError("tier result: invalid columns") from None
        return quest - 66000

    def name(self, guid) -> str | None:
        guid = _id(guid)
        rows = run_mysql(self.chars, f"SELECT name FROM characters WHERE guid = {guid}", self.mysql_exe)
        if not rows:
            return None
        if len(rows) != 1 or len(rows[0]) != 1:
            raise RosterError("character name result: invalid columns")
        return rows[0][0]
