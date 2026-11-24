"""A member's talent tree from the talents she has (the guild's loot wishes, 2026-11-24: a spot
check found wishes that followed her persona's raid role or a stale tree, never her bot's talents:
a one-hand dagger for an Arms warrior, agility legs for a Restoration druid, a tank's neck for a
Balance druid).

The characters database keeps her learned talent spells (character_talent: guid, spell and the
specMask of the talent groups that have it) and which group is active (characters
.activeTalentGroup); the server's own DBC files say where each spell sits: Talent.dbc (a talent's
tab and its rank spells) and TalentTab.dbc (a tab's class mask and its page, 0-2, in the class's
order). Her tree is the page with the most points, as playerbots counts them for the cast snapshot
(AiFactory::GetPlayerSpecTab: a spell of rank n is n points, the first page wins a tie); -1 with
no points. Pure but for read_tables (the two files) and read_data_dir (worldserver.conf)."""
from __future__ import annotations

import re
import struct
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

TALENT_FILE, TAB_FILE = "Talent.dbc", "TalentTab.dbc"
TALENT_FIELDS, TAB_FIELDS = 23, 24  # 3.3.5a's layouts
_TALENT_TAB, _TALENT_RANKS = 1, range(4, 13)  # Talent.dbc: TabID, SpellRank[9]
_TAB_CLASS, _TAB_PAGE = 20, 22  # TalentTab.dbc: ClassMask, OrderIndex
PAGES = 3


@dataclass(frozen=True, slots=True)
class TalentSpell:
    class_mask: int
    page: int
    points: int


def records(data: bytes, fields: int):
    """A WDBC file's records as tuples of `fields` unsigned 32-bit fields."""
    if len(data) < 20:
        raise ValueError("not a DBC file")
    magic, count, have, size, _strings = struct.unpack_from("<4s4I", data)
    if magic != b"WDBC" or have != fields or size != 4 * fields or len(data) < 20 + count * size:
        raise ValueError("unexpected DBC layout")
    for i in range(count):
        yield struct.unpack_from(f"<{fields}I", data, 20 + i * size)


def tables(talent: bytes, tab: bytes) -> dict[int, TalentSpell]:
    """{a talent's rank spell: its class mask, page and points} from the two files' bytes.
    Pet talent tabs (no class mask) are left out."""
    pages = {r[0]: (r[_TAB_CLASS], r[_TAB_PAGE]) for r in records(tab, TAB_FIELDS)
             if r[_TAB_CLASS] and 0 <= r[_TAB_PAGE] < PAGES}
    out = {}
    for r in records(talent, TALENT_FIELDS):
        found = pages.get(r[_TALENT_TAB])
        if found is None:
            continue
        for rank, field in enumerate(_TALENT_RANKS, 1):
            if r[field]:
                out[r[field]] = TalentSpell(found[0], found[1], rank)
    return out


def read_tables(dbc_dir: Path) -> dict[int, TalentSpell]:
    """tables() from Talent.dbc and TalentTab.dbc in `dbc_dir`."""
    d = Path(dbc_dir)
    return tables((d / TALENT_FILE).read_bytes(), (d / TAB_FILE).read_bytes())


def spec_tab(cls: int, spells: Iterable[int], table: dict[int, TalentSpell]) -> int:
    """Her talent tree's page (0-2) by the points her `spells` put in each of class `cls`'s
    pages; -1 with none."""
    if type(cls) is not int or not 0 < cls <= 32:
        return -1
    bit, points = 1 << (cls - 1), [0] * PAGES
    for spell in spells:
        t = table.get(spell)
        if t is not None and t.class_mask & bit:
            points[t.page] += t.points
    if not any(points):
        return -1
    return points.index(max(points))


def read_data_dir(worldserver_conf: Path) -> Path | None:
    """worldserver.conf's DataDir (the folder holding dbc/), or None without one."""
    text = Path(worldserver_conf).read_text(encoding="utf-8", errors="replace")
    m = re.search(r'^\s*DataDir\s*=\s*"?([^"\r\n]*?)"?\s*$', text, re.MULTILINE)
    return Path(m.group(1)) if m and m.group(1).strip() else None


__all__ = ["PAGES", "TalentSpell", "read_data_dir", "read_tables", "records", "spec_tab",
           "tables"]
