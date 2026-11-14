"""Pre-pull plans keyed by the core's encounter indexes, not creature entries."""

from dataclasses import dataclass
import re

from leeroy_mind import raidplay
from leeroy_mind.wow import BOSS_NAMES


@dataclass(frozen=True)
class BossCall:
    pre: str
    lust: bool = True


# The stock pre-pull calls are kept with each boss in the raid playbooks (leeroy_mind/raidplay).
BOSS_CALLS: dict[tuple[int, int], BossCall] = {
    boss.key: BossCall(boss.call, boss.lust) for boss in raidplay.load().bosses() if boss.call is not None}
GENERIC = BossCall("{boss} next. drink up, then on the tank")


def _name(name):
    name = " ".join(name.casefold().split())
    return name[4:] if name.startswith("the ") else name


def call_for(map_id, name) -> tuple[str, BossCall]:
    for key, known in BOSS_NAMES.items():
        if key[0] == map_id and _name(known) == _name(name):
            return known, BOSS_CALLS.get(key, BossCall(GENERIC.pre.format(boss=known)))
    return name, BossCall(GENERIC.pre.format(boss=name))


@dataclass(frozen=True)
class DcState:
    state: str
    next_entry: int
    next_name: str


def parse_dc_status(payload) -> DcState | None:
    if not isinstance(payload, str) or len(payload) > 4096:
        return None
    fields = payload.split("\t")
    if (len(fields) < 7 or fields[0] != "STATUS" or not re.fullmatch(r"[0-9]{1,10}", fields[2])
            or int(fields[2]) > 0xFFFFFFFF or not fields[6] or len(fields[6]) > 64
            or len(fields[3]) > 200 or any(ord(c) < 32 for c in fields[3] + fields[6])):
        return None
    return DcState(fields[6], int(fields[2]), fields[3])
