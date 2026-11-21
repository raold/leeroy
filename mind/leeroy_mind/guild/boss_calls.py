"""Pre-pull plans keyed by the core's encounter indexes, not creature entries."""

from collections.abc import Mapping
from dataclasses import dataclass
import logging
import re

from leeroy_mind import raidplay
from leeroy_mind.wow import BOSS_NAMES

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class BossCall:
    pre: str
    lust: bool = True


# The playbooks' calls (the ICC and Trial of the Crusader ones as they were before they moved
# there, then Ruby Sanctum's, Ulduar's and Malygos's); used only when the playbook data cannot be read, so guild life's
# calls never fail with it. A test keeps the two equal.
_FALLBACK = {
    (631, 0): BossCall("marrowgar: spread out, kill spikes, run from coldflame"),
    (631, 1): BossCall("deathwhisper: kill adds fast, interrupt frostbolts"),
    (631, 2): BossCall("gunship: jump over, kill the mage, then back to the ship", False),
    (631, 3): BossCall("saurfang: ranged spread, kill blood beasts fast"),
    (631, 4): BossCall("festergut: ranged spread, stack on gas spores"),
    (631, 5): BossCall("rotface: kite the big ooze, merge the little ones"),
    (631, 6): BossCall("putricide: kite the oozes, stay out of puddles"),
    (631, 7): BossCall("princes: ranged spread, shoot the kinetic bombs"),
    (631, 8): BossCall("blood queen: spread, bite on time, take swarm out"),
    (631, 9): BossCall("svalna: grab a spear, free whoever she impales"),
    (631, 10): BossCall("valithria: heal her to full, dps kill the adds", False),
    (631, 11): BossCall("sindragosa: spread, hide behind ice tombs for bombs"),
    (631, 12): BossCall("lich king: stack for infest, move out of defile"),
    (649, 0): BossCall("beasts: kill the snakes, dodge icehowl's charge"),
    (649, 1): BossCall("jaraxxus: interrupt, dodge eruptions, kill the mistress"),
    (649, 2): BossCall("champions: focus the healers, cc what you can"),
    (649, 3): BossCall("twins: match your orb colour, swap on vortex"),
    (649, 4): BossCall("anub'arak: kite the spike over frost patches"),
    (603, 0): BossCall("leviathan: kite when pursued, shut him down, keep pyrite up", False),
    (603, 1): BossCall("ignis: melt constructs, cool them in water, dodge scorch"),
    (603, 2): BossCall("razorscale: fire harpoons, kill dwarves, out of fire"),
    (603, 3): BossCall("xt-002: bomb and light run out, everyone on the heart"),
    (603, 4): BossCall("iron council: steelbreaker first, out of overload and runes"),
    (603, 5): BossCall("kologarn: break the right arm, kill rubble, kite eye beams"),
    (603, 6): BossCall("auriaya: all in front, kick the blast, out of pools"),
    (603, 7): BossCall("hodir: keep moving, snowdrift at flash freeze, break ice"),
    (603, 8): BossCall("thorim: gauntlet team runs, arena holds the middle"),
    (603, 9): BossCall("freya: kill the gift, trio together, out of bombs"),
    (603, 10): BossCall("mimiron: out of shock blast, dodge rockets and lasers"),
    (603, 11): BossCall("vezax: kick searing flames, dodge crash, marked run out"),
    (603, 12): BossCall("yogg: guardians die by sara, avoid clouds, look away"),
    (603, 13): BossCall("algalon: black hole for big bang, swap at four punches"),
    (616, 0): BossCall("malygos: kill sparks, bubbles on deep breath, shield surges"),
    (724, 0): BossCall("baltharus: branded run out, off-tank takes the clone", False),
    (724, 1): BossCall("zarithrian: swap on armor, kill the flamecallers", False),
    (724, 2): BossCall("saviana: beacons spread out, dodge her breath", False),
    (724, 6): BossCall("halion: dodge meteors, carry combustion out, even realms"),
}


# Heroic calls where heroic differs (on normal the volcano and portal cannot be targeted).
_FALLBACK_HEROIC = {
    (649, 1): BossCall("jaraxxus: interrupt, kill portals and volcanoes"),
}


class _Calls(Mapping):
    """The stock pre-pull calls, kept with each boss in the raid playbooks and read on first use;
    heroic=True holds only the bosses whose heroic call differs."""

    def __init__(self, heroic: bool = False):
        self._calls = None
        self._heroic = heroic

    def _read(self) -> dict:
        if self._calls is None:
            try:
                self._calls = {boss.key: BossCall(boss.heroic_call if self._heroic else boss.call, boss.lust)
                               for boss in raidplay.load().bosses()
                               if (boss.heroic_call if self._heroic else boss.call) is not None}
            except Exception as exc:
                log.warning("boss_calls_fallback error=%s", type(exc).__name__)
                self._calls = dict(_FALLBACK_HEROIC if self._heroic else _FALLBACK)
        return self._calls

    def __getitem__(self, key):
        return self._read()[key]

    def __iter__(self):
        return iter(self._read())

    def __len__(self):
        return len(self._read())


BOSS_CALLS: Mapping[tuple[int, int], BossCall] = _Calls()
HEROIC_CALLS: Mapping[tuple[int, int], BossCall] = _Calls(heroic=True)
GENERIC = BossCall("{boss} next. drink up, then on the tank")


def _name(name):
    name = " ".join(name.casefold().split())
    return name[4:] if name.startswith("the ") else name


def call_for(map_id, name, difficulty=None) -> tuple[str, BossCall]:
    """The boss's stock call; difficulty 2 or 3 (heroic) picks its heroic call when it has one."""
    for key, known in BOSS_NAMES.items():
        if key[0] == map_id and _name(known) == _name(name):
            call = BOSS_CALLS.get(key, BossCall(GENERIC.pre.format(boss=known)))
            return known, (HEROIC_CALLS.get(key, call) if difficulty in (2, 3) else call)
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
