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


@dataclass
class PasteBurst:
    channel: str
    to_guid: int
    started: float
    rows: list[ChatLineRow]


def named(rows: Sequence[ChatLineRow], names: dict[int, str]) -> list[int]:
    """Members named on a numbered row, in row order, each once."""
    by_name = {n.casefold(): g for g, n in names.items()}
    out: list[int] = []
    for r in rows:
        m = NUMBERED_RE.match(r.text)
        guid = by_name.get(m.group(1).casefold()) if m else None
        if guid is not None and guid not in out:
            out.append(guid)
    return out


def focus_row(rows: Sequence[ChatLineRow], rng: random.Random) -> ChatLineRow:
    """The first or last numbered row, else the first row."""
    numbered = [r for r in rows if NUMBERED_RE.match(r.text)]
    if not numbered:
        return rows[0]
    return rng.choice([numbered[0], numbered[-1]])
