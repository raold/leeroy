"""Groups: a named, saved predicate over the stored persona and the cast row, evaluated at read
time, so membership moves on its own (a stance group with the attraction curve, a rank group
with the roster). A predicate is an AND of "field is one of these values", and an age range;
there is no free text. "everyone" is built in. The owner's own character and anyone off the cast
are left out before any predicate runs (the caller's job: Tuning.member)."""
from dataclasses import dataclass
from typing import get_args, get_type_hints

from leeroy_mind.personas.schema import (MIN_AGE, Game, Habit, Identity, Persona, Play,
                                         Romance)
from leeroy_mind.wow import CLASSES, RACES

EVERYONE = "everyone"
MAX_VALUES = 12
NAME_MAX = 40


def _enum(cls, name: str) -> tuple:
    t = get_type_hints(cls)[name]
    args = get_args(t)
    if args and all(isinstance(a, str) for a in args):
        return args
    inner = next(a for a in args if a is not type(None))  # Literal[...] | None
    return get_args(inner)


# field -> (the values it may hold, how to read it). A reader gets (persona, cast member or None)
# and returns the member's value (a set for a field a member can hold several of).
FIELDS: dict[str, tuple[tuple, object]] = {
    "gender": (_enum(Identity, "player_gender"), lambda p, m: p.identity.player_gender),
    "region": (_enum(Identity, "region"), lambda p, m: p.identity.region),
    "accent": (_enum(Identity, "accent"), lambda p, m: p.identity.accent),
    "core": ((True, False), lambda p, m: p.core),
    "attachment": (_enum(Persona, "attachment"), lambda p, m: p.attachment),
    "humour": (_enum(Persona, "humour"), lambda p, m: p.humour),
    "stance": (_enum(Romance, "stance"), lambda p, m: p.romance.stance),
    "romance_style": (_enum(Romance, "style"), lambda p, m: p.romance.style),
    "jealousy_mode": (_enum(Romance, "jealousy_mode"), lambda p, m: p.romance.jealousy_mode),
    "motivation": (_enum(Game, "motivation"), lambda p, m: p.game.motivation),
    "thinks_as": (_enum(Game, "thinks_as"), lambda p, m: p.game.thinks_as),
    "raid_role": (_enum(Play, "raid_role"), lambda p, m: p.play.raid_role),
    "skill": (_enum(Play, "skill"), lambda p, m: p.play.skill),
    "guild_role": (_enum(Play, "guild_role"), lambda p, m: p.play.guild_role),
    "night_owl": ((True, False), lambda p, m: p.real_life.night_owl),
    "habit": (_enum(Habit, "substance"), lambda p, m: {h.substance for h in p.habits}),
    "class": (tuple(CLASSES.values()), lambda p, m: CLASSES.get(m.class_) if m else None),
    "race": (tuple(RACES.values()), lambda p, m: RACES.get(m.race) if m else None),
    "rank": (tuple(range(10)), lambda p, m: m.rank if m else None),
}
CAST_FIELDS = frozenset({"class", "race", "rank"})
AGE = "age"


class GroupError(ValueError):
    pass


@dataclass(frozen=True)
class Group:
    target: str  # "everyone" or the group's id as text
    name: str
    where: dict
    builtin: bool = False
    created: float = 0.0


EVERYONE_GROUP = Group(EVERYONE, EVERYONE, {}, builtin=True)


def parse_name(name) -> str:
    if not isinstance(name, str):
        raise GroupError("a group's name is text")
    name = " ".join(name.split())
    if not 1 <= len(name) <= NAME_MAX or not name.isprintable():
        raise GroupError(f"a group's name is 1-{NAME_MAX} printable characters")
    if name.casefold() == EVERYONE:
        raise GroupError('"everyone" is built in')
    return name


def parse_where(where) -> dict:
    """The predicate in its stored form (keys sorted, each list of values sorted and without
    repeats, age as [from, to]); GroupError for anything else."""
    if not isinstance(where, dict) or not where:
        raise GroupError("a group needs at least one condition")
    out = {}
    for key in sorted(where):
        value = where[key]
        if key == AGE:
            if (not isinstance(value, list) or len(value) != 2
                    or any(type(v) is not int for v in value)
                    or not MIN_AGE <= value[0] <= value[1] <= 99):
                raise GroupError(f"age is [from, to], whole years {MIN_AGE}-99")
            out[key] = list(value)
            continue
        if key not in FIELDS:
            raise GroupError(f"{key!r} is not a field a group can use")
        allowed = FIELDS[key][0]
        if (not isinstance(value, list) or not 1 <= len(value) <= MAX_VALUES
                or any(type(v) is not type(allowed[0]) or v not in allowed for v in value)):
            raise GroupError(f"{key}: one or more of " + ", ".join(map(str, allowed)))
        out[key] = sorted(set(value), key=allowed.index)
    return out


def uses_cast(where: dict) -> bool:
    return bool(CAST_FIELDS & set(where))


def matches(where: dict, p: Persona, member) -> bool:
    """Whether the member (persona p, cast row `member` or None) is in a group with this
    predicate. A cast field with no cast row never matches."""
    for key, allowed in where.items():
        if key == AGE:
            if not allowed[0] <= p.identity.age <= allowed[1]:
                return False
            continue
        have = FIELDS[key][1](p, member)
        if isinstance(have, set):
            if not have & set(allowed):
                return False
        elif have is None or have not in allowed:
            return False
    return True


def describe(where: dict) -> str:
    """The predicate in a few plain words, for the history and the page."""
    if not where:
        return "everyone"
    parts = []
    for key, allowed in where.items():
        if key == AGE:
            parts.append(f"aged {allowed[0]}-{allowed[1]}")
        else:
            parts.append(f"{key.replace('_', ' ')}: {' or '.join(map(str, allowed))}")
    return "; ".join(parts)
