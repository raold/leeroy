"""Addon pastes in his chat (variety track D1): Recount, RBS, flask checks and numbered
damage lines, so a burst of them can be answered once."""
import random
import re
from dataclasses import dataclass
from typing import Sequence

from leeroy_mind.storage.rows import ChatLineRow

ADDON_RE = re.compile(
    r"^\s*(\d+\.\s+\S+\s+[\d.,]+[kKmM]?\s*\(|Recount - |Skada: |Details!|RBS::|<Flask|<Food|\[RBS\])", re.I)
NUMBERED_RE = re.compile(r"^\s*\d+\.\s+(\S+)\s+[\d.,]+[kKmM]?\s*\(", re.I)

PASTE_BURST_S = 60.0
PASTE_SETTLE_S = 5.0


def is_addon_row(text: str) -> bool:
    return bool(ADDON_RE.match(text))


_CHECK_RE = re.compile(r"^\s*(RBS::|<Flask|<Food|\[RBS\])", re.I)


def paste_kind(text: str) -> str:
    """"check" for an RBS, flask or food check, else "meter" (damage and healing meters)."""
    return "check" if _CHECK_RE.match(text) else "meter"


@dataclass
class PasteBurst:
    channel: str
    to_guid: int
    started: float
    rows: list[ChatLineRow]


_RBS_RE = re.compile(r"^\s*RBS::[^:]*:\s*(.+)$", re.I)
_FLASK_RE = re.compile(r"^\s*<(?:Flask|Food)[^>]*>\s*(.+)$", re.I)


def _clean(token: str) -> str:
    """A captured name without trailing punctuation or a "-Realm" suffix."""
    return token.strip(":,.;").split("-")[0].strip(":,.;").casefold()


def row_members(row: ChatLineRow, by_name: dict[str, int]) -> list[int]:
    """The members one row names: the name of a numbered row, or the names after an RBS or
    Flask row's marker."""
    m = NUMBERED_RE.match(row.text)
    if m:
        tokens = [m.group(1)]
    else:
        m = _RBS_RE.match(row.text) or _FLASK_RE.match(row.text)
        tokens = re.split(r"[\s,]+", m.group(1)) if m else []
    out: list[int] = []
    for t in tokens:
        guid = by_name.get(_clean(t))
        if guid is not None and guid not in out:
            out.append(guid)
    return out


def _by_name(names: dict[int, str]) -> dict[str, int]:
    return {n.casefold(): g for g, n in names.items()}


def named(rows: Sequence[ChatLineRow], names: dict[int, str]) -> list[int]:
    """Members named on a row, in row order, each once."""
    by_name = _by_name(names)
    out: list[int] = []
    for r in rows:
        for guid in row_members(r, by_name):
            if guid not in out:
                out.append(guid)
    return out


def row_naming(rows: Sequence[ChatLineRow], guid: int, names: dict[int, str]) -> ChatLineRow | None:
    """The first row that names `guid`."""
    by_name = _by_name(names)
    return next((r for r in rows if guid in row_members(r, by_name)), None)


def focus_row(rows: Sequence[ChatLineRow], rng: random.Random) -> ChatLineRow:
    """The first or last numbered row, else the first row."""
    numbered = [r for r in rows if NUMBERED_RE.match(r.text)]
    if not numbered:
        return rows[0]
    return rng.choice([numbered[0], numbered[-1]])
