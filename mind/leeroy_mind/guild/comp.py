"""Pure raid composition and leadership decisions over a roster snapshot."""

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import math
import random
from types import SimpleNamespace

from .roles import is_loot_officer, is_raid_lead, role_of


TARGETS = {10: (2, 3, 5), 25: (3, 6, 16)}
_ROLES = ("tank", "healer", "damage")
_RANK = {"officer": 0, "raider": 1, "social": 2}
_BATTLEGROUNDS = frozenset({30, 489, 529, 559, 562, 566, 572, 607, 617, 618, 628})


@dataclass(frozen=True, slots=True)
class Candidate:
    guid: int
    name: str
    cls: int
    spec_tab: int
    role: str
    dc_tank: bool
    guild_role: str
    officer_title: str
    bench_streak: int
    attended_30d: int
    available: bool
    ilvl: int = 0  # average item level from the armory; 0 when unread (a lineup reads it)


@dataclass(frozen=True, slots=True)
class Comp:
    members: list[int]
    roles: dict[int, str]
    bench: list[int]
    shortfall: dict[str, int]


@dataclass(frozen=True, slots=True)
class Leads:
    lead_guid: int
    tank_guid: int
    loot_guid: int


class NoTankError(Exception):
    """The proposed group has no available tank with a playable tank spec."""


# The raid-wide buffs a lineup should not lack, as mod-leeroy's add picker weighs them
# (GuildAddSpecFacts): Bloodlust/Heroism from any shaman; Replenishment from a Retribution
# paladin, Survival hunter, Shadow priest, Frost mage or Destruction warlock. Replenishment
# reaches 10 members, so a 25 wants two sources.
_REPLENISHMENT = frozenset({(2, 2), (3, 2), (5, 2), (8, 2), (9, 2)})
RAID_BUFFS = ("bloodlust", "replenishment")


def raid_buffs(candidate: Candidate) -> frozenset[str]:
    out = set()
    if candidate.cls == 7:
        out.add("bloodlust")
    if candidate.role == "damage" and (candidate.cls, candidate.spec_tab) in _REPLENISHMENT:
        out.add("replenishment")
    return frozenset(out)


def wanted_sources(buff: str, size: int) -> int:
    return 2 if buff == "replenishment" and size >= 25 else 1


def coverage(size: int, chosen: Iterable[Candidate]) -> list[str]:
    """The lineup's coverage codes: each raid buff it has enough sources of, else no_<buff>."""
    have = Counter(buff for c in chosen for buff in raid_buffs(c))
    return [buff if have[buff] >= wanted_sources(buff, size) else "no_" + buff for buff in RAID_BUFFS]


def _priority(candidate: Candidate) -> tuple[int, int, int]:
    return (_RANK.get(candidate.guild_role, 2), -candidate.bench_streak,
            candidate.attended_30d)


def _unique(cands: Iterable[Candidate]) -> dict[int, Candidate]:
    result = {}
    for candidate in cands:
        if type(candidate.guid) is not int or candidate.guid <= 0:
            raise ValueError("candidate GUID must be a positive integer")
        if candidate.role not in _ROLES:
            raise ValueError("unknown candidate role")
        previous = result.get(candidate.guid)
        if previous is not None and previous != candidate:
            raise ValueError(f"conflicting candidate rows for GUID {candidate.guid}")
        result[candidate.guid] = candidate
    return result


def select_comp(size: int, cands: Iterable[Candidate], *, must_include: Iterable[int],
                human_in_raid: bool, seed: int, buff_ties: bool = False) -> Comp:
    """Keep mandatory members, then fill role targets fairly without overbooking.

    Mandatory members may exceed role targets or soft class caps, but not total
    capacity. Missing/unavailable mandatory GUIDs and impossible capacity raise
    ValueError rather than silently weakening the request. The human occupies a
    damage seat and is not included in the returned bot GUIDs or role mapping.
    buff_ties (a posted lineup, decisions.lineup): among candidates of equal priority, one adding
    a Bloodlust or Replenishment source the picks still lack comes first, then the better gear.
    """
    if type(size) is not int or size not in TARGETS:
        raise ValueError("raid size must be 10 or 25")
    if type(human_in_raid) is not bool or type(seed) is not int:
        raise ValueError("human_in_raid must be boolean and seed must be an integer")
    available = {guid: candidate for guid, candidate in _unique(cands).items()
                 if candidate.available is True}
    capacity = size - int(human_in_raid)
    required = []
    for guid in must_include:
        if type(guid) is not int or guid not in available:
            raise ValueError("mandatory member is missing or unavailable")
        if guid not in required:
            required.append(guid)
    if len(required) > capacity:
        raise ValueError("mandatory members exceed raid capacity")

    rng = random.Random(seed)
    # Assign ties in GUID order so a reordered cast response does not change a
    # seeded decision. GUID is a final tie-breaker even if random values collide.
    ties = {guid: rng.random() for guid in sorted(available)}
    ranked = sorted(available.values(), key=lambda c: (*_priority(c), ties[c.guid], c.guid))
    chosen = [available[guid] for guid in required]
    selected = set(required)
    counts = Counter(candidate.role for candidate in chosen)
    damage_classes = Counter(candidate.cls for candidate in chosen if candidate.role == "damage")
    targets = dict(zip(_ROLES, TARGETS[size]))
    targets["damage"] -= int(human_in_raid)
    class_cap = 2 if size == 10 else 3

    def add(candidate: Candidate) -> None:
        chosen.append(candidate)
        selected.add(candidate.guid)
        counts[candidate.role] += 1
        if candidate.role == "damage":
            damage_classes[candidate.cls] += 1

    def adds(candidate: Candidate) -> int:
        have = Counter(buff for c in chosen for buff in raid_buffs(c))
        return sum(have[buff] < wanted_sources(buff, size) for buff in raid_buffs(candidate))

    def next_of(role: str) -> Candidate | None:
        remaining = [c for c in ranked if c.guid not in selected and c.role == role]
        if role == "damage":
            diverse = [c for c in remaining if damage_classes[c.cls] < class_cap]
            if diverse:
                remaining = diverse
        if buff_ties and remaining:
            return min(remaining, key=lambda c: (*_priority(c), -adds(c), -c.ilvl, ties[c.guid], c.guid))
        return remaining[0] if remaining else None

    for role in _ROLES:
        while len(chosen) < capacity and counts[role] < targets[role]:
            candidate = next_of(role)
            if candidate is None:
                break
            add(candidate)

    # Record real role deficits even when another role can occupy the empty seats.
    # Damage is preferred for those seats; extra tanks/healers are the fallback.
    for role in ("damage", "tank", "healer"):
        while len(chosen) < capacity:
            candidate = next_of(role)
            if candidate is None:
                break
            add(candidate)

    if not any(c.role == "tank" and c.dc_tank for c in chosen):
        raise NoTankError("composition has no playable dungeon-clear tank")
    return Comp(
        [c.guid for c in chosen], {c.guid: c.role for c in chosen},
        [c.guid for c in ranked if c.guid not in selected and c.guild_role in {"officer", "raider"}],
        {role: targets[role] - counts[role] for role in _ROLES if counts[role] < targets[role]},
    )


def choose_leads(cands: Iterable[Candidate], *, requested: int | None, rotate: bool = False,
                 human_leads: bool, human_guid: int,
                 last_led: dict[int, float] | None = None) -> Leads:
    """Resolve leadership separately from the actual tank and loot responsibilities."""
    if type(human_guid) is not int or human_guid <= 0:
        raise ValueError("human GUID must be a positive integer")
    available = [c for c in _unique(cands).values() if c.available is True and c.guid != human_guid]
    ranked = sorted(available, key=lambda c: (*_priority(c), c.guid))
    tanks = [c for c in ranked if c.role == "tank" and c.dc_tank]
    if not tanks:
        raise NoTankError("no available dungeon-clear tank")
    raid_leads = [c for c in ranked if is_raid_lead(SimpleNamespace(play=c))]
    standing = min((c for c in raid_leads if c in tanks), key=lambda c: c.guid, default=None)
    tank = standing or next((c for c in tanks if c.guild_role == "officer"), tanks[0])
    rotation = [c for c in raid_leads if c != standing]
    history = last_led or {}

    def recency(candidate: Candidate) -> tuple[int, float, int]:
        last = history.get(candidate.guid)
        if type(last) not in (int, float) or not math.isfinite(last):
            return 0, 0.0, candidate.guid
        return 1, float(last), candidate.guid

    rotated = min(rotation, key=recency, default=None)
    request = next((c for c in ranked if type(requested) is int and c.guid == requested
                    and c.guild_role == "officer"), None)
    if human_leads:
        lead_guid = human_guid
    else:
        lead = request or (rotated if rotate else None) or standing or rotated or tank
        lead_guid = lead.guid
    loot = next((c for c in ranked if is_loot_officer(SimpleNamespace(play=c))), None)
    return Leads(lead_guid, tank.guid, loot.guid if loot else tank.guid if human_leads else lead_guid)


def candidates_from(members, personas, streaks, attended, away) -> list[Candidate]:
    """Project persona-bearing cast members; unavailable rows remain descriptive.

    Unknown talent tabs can supply a persona role but never prove a real DC tank.
    Feral bears need the persona's tank role because cats use the same talent tab.
    """
    rows = members.values() if isinstance(members, Mapping) else members
    candidates = []
    for member in rows:
        persona = personas.get(member.guid)
        if persona is None:
            continue
        role = role_of(member.class_, member.spec_tab, persona.play.raid_role)
        candidates.append(Candidate(
            member.guid, member.name, member.class_, member.spec_tab, role,
            role == "tank" and member.spec_tab >= 0,
            persona.play.guild_role, persona.play.officer_title,
            streaks.get(member.guid, 0), attended.get(member.guid, 0),
            member.online is True and member.level >= 80 and member.map not in _BATTLEGROUNDS
            and member.guid not in away,
        ))
    return sorted(_unique(candidates).values(), key=lambda c: c.guid)
