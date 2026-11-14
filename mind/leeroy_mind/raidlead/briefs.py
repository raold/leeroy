"""A boss brief: the raid lead's pre-pull plan, built by code from the raid playbooks
(mind/leeroy_mind/raidplay/), never by a model, so no mechanic or tactic in it is made up.

`build` picks the 2-4 mechanics that matter most to his group at that difficulty: those only this
difficulty has first, then by kind (KINDS), then by how many of the group they concern, then in
the playbook's order. A mechanic is kept only when the group has one of its roles; one that is
only for tanks only when tanks are named in the group's roles. Each mechanic is said with its own
playbook line (`explain`, whole and unchanged) after a header naming the boss and difficulty, on
at most MAX_LINES raid chat lines of at most LINE_BYTES bytes each; a mechanic that does not fit
whole is left out, never cut. Nothing here logs, calls a model or reads the database.
"""

from collections import Counter
from dataclasses import dataclass
import functools
import re
from types import MappingProxyType
from typing import Mapping

from leeroy_mind.wow import BOSS_NAMES

LINE_BYTES = 255  # one raid chat line on the wire (talk/style.py MAX_TEXT_BYTES)
MAX_LINES = 3
MOST = 4
ROLES = ("tank", "healer", "melee", "ranged")
# What a pre-pull brief names first: what the whole raid kills or must not stand in, then where
# people stand, then one role's job, then what the raid only has to outlast.
KINDS = ("switch_adds", "kill_order", "interrupt", "avoid_ground", "run_out_debuff", "soak",
         "stack", "spread", "move_out_cone", "los", "kite", "use_object", "positional",
         "tank_swap_at_stacks", "face_away", "dispel", "heal_target", "raid_cooldowns",
         "stop_casting", "enrage_timer")
_RANK = {kind: i for i, kind in enumerate(KINDS)}
_WORDS = {"10": "10 normal", "25": "25 normal", "10h": "10 heroic", "25h": "25 heroic"}


@dataclass(frozen=True, slots=True)
class Brief:
    context: str                 # MAP:BOSS:DIFFICULTY, the training ground's context id
    mechanics: tuple[str, ...]   # the playbook mechanic ids it names, in the order said
    lines: tuple[str, ...]       # what is said, each at most LINE_BYTES bytes


def damage_range(cls: int, spec_tab: int) -> str:
    """Where a damage dealer stands: hunters, mages, warlocks, shadow priests, elemental shamans
    and balance druids are ranged; everyone else is melee."""
    if cls in (3, 5, 8, 9):
        return "ranged"
    if cls in (7, 11):
        return "melee" if spec_tab == 1 else "ranged"
    return "melee"


def playbook_role(role: str, cls: int, spec_tab: int) -> str:
    """A member's role as the playbooks name it: tank, healer, or a damage dealer's melee or
    ranged (guild/roles.role_of gives tank, healer or damage)."""
    return role if role in ("tank", "healer") else damage_range(cls, spec_tab)


def difficulty(size: int, heroic: bool) -> int | None:
    """The core's raid difficulty (0 10-man, 1 25-man, 2 10 heroic, 3 25 heroic); None when the
    size is not known."""
    if size not in (10, 25):
        return None
    return int(size == 25) + 2 * int(bool(heroic))


def _sentence(text: str) -> str:
    text = " ".join(text.split())
    return text if text[-1:] in ".!?" else text + "."


def _fits(text: str) -> bool:
    return len(text.encode("utf-8")) <= LINE_BYTES


def _pick(book, all_difficulties, roles):
    present, counts = set(roles), Counter(roles)
    ranked = []
    for order, m in enumerate(book.mechanics):
        if m.roles == ("tank",) and "tank" not in present:
            continue  # a tank's job only when tanks are named in his group
        if present and not present & set(m.roles):
            continue
        only_here = len(m.only) < len(all_difficulties)
        reach = sum(counts[r] for r in m.roles) if present else len(m.roles)
        ranked.append(((not only_here, _RANK.get(m.kind, len(_RANK)), -reach, order), m))
    return [m for _, m in sorted(ranked, key=lambda x: x[0])]


def build_from(playbooks, map_id: int, boss_id: int, diff: int | None,
               roles: tuple[str, ...] = ()) -> Brief | None:
    """The brief for a boss at a difficulty from these playbooks, or None when there is no
    playbook for it there, no mechanic for the roles given, or the difficulty is not known.
    `roles`: the playbook role of each member of his group whose role is known (repeats count);
    empty when nobody's is."""
    if diff is None or any(r not in ROLES for r in roles):
        return None
    book = playbooks.playbook_for(map_id, boss_id, diff)
    if book is None:
        return None
    raid = playbooks.raids[map_id]
    name = book.boss.name[:1].upper() + book.boss.name[1:]
    lines, used = [f"{name}, {_WORDS[book.difficulty]}:"], []
    for m in _pick(book, raid.difficulties, roles):
        if len(used) == MOST:
            break
        sentence = _sentence(m.explain)
        if _fits(f"{lines[-1]} {sentence}"):
            lines[-1] = f"{lines[-1]} {sentence}"
        elif len(lines) < MAX_LINES and _fits(sentence):
            lines.append(sentence)
        else:
            continue  # never cut: a lower one may still fit whole
        used.append(m.id)
    if not used:
        return None
    return Brief(book.context, tuple(used), tuple(lines))


def build(map_id: int, boss_id: int, diff: int | None, roles: tuple[str, ...] = ()) -> Brief | None:
    """build_from the repo's playbooks (raidplay.load, which may raise PlaybookError)."""
    from leeroy_mind import raidplay
    return build_from(raidplay.load(), map_id, boss_id, diff, roles)


_STOP = frozenset({"the", "lord", "lady", "king", "queen", "blood", "prince", "sister", "grand",
                   "general", "instructor", "professor", "watcher", "battle", "council", "twin",
                   "four", "faction", "northrend", "assembly", "iron", "storm", "flame", "stone",
                   "ice", "master", "observer"})


def _forms(text: str) -> set[str]:
    text = " ".join(text.casefold().replace("’", "'").split())
    text = text.removeprefix("the ")
    return {text, text.replace("'", ""), text.replace("-", " "), text.replace("'", "").replace("-", "")}


@functools.cache
def boss_aliases() -> Mapping[str, tuple[int, int]]:
    """What he may call a raid boss in a question ("what do we do on heigan"): its full name, its
    playbook slug, and each distinctive word of its name, lower case. A word two bosses share is
    nobody's. Read from wow.BOSS_NAMES and, when they load, the playbooks' slugs."""
    found: dict[str, set[tuple[int, int]]] = {}
    slugs = {}
    try:
        from leeroy_mind import raidplay
        slugs = {b.key: b.slug.replace("_", " ") for b in raidplay.load().bosses()}
    except Exception:
        pass
    for key, name in BOSS_NAMES.items():
        forms = _forms(name) | ({slugs[key]} if key in slugs else set())
        for word in re.split(r"[ -]+", name.casefold()):
            if len(word) >= 4 and word not in _STOP:
                forms |= _forms(word)
        for form in forms:
            if form:
                found.setdefault(form, set()).add(key)
    return MappingProxyType({form: next(iter(keys)) for form, keys in found.items() if len(keys) == 1})
