"""The raid playbooks as data: one TOML file per raid under data/, keyed by map id and the
instance script's boss index (the training ground's MAP:BOSS:DIFFICULTY contexts).

Loading checks only the shape (kinds, roles, difficulties, references, unique ids); whether
every spell id exists in the game's spell book is check.py's job. Nothing here reads the
network, a database or the live data dir.
"""
from __future__ import annotations

import functools
import math
import re
import tomllib
from dataclasses import dataclass, field
from importlib import resources
from types import MappingProxyType
from typing import Mapping

from leeroy_mind.wow import BOSS_NAMES, RAID_INSTANCES

# The training ground's difficulty numbers (the core's Difficulty enum for raids) and the
# keys the data files use for them.
DIFFICULTIES = MappingProxyType({0: "10", 1: "25", 2: "10h", 3: "25h"})
DIFFICULTY_KEYS = tuple(DIFFICULTIES.values())

KINDS = frozenset({
    "avoid_ground",       # leave or never stand in a ground effect
    "move_out_cone",      # leave a frontal (or rear) cone or line
    "spread",             # keep a distance from each other
    "stack",              # gather on one spot or player
    "soak",               # stand in it on purpose to share or absorb it
    "run_out_debuff",     # the marked player leaves the raid until it ends
    "interrupt",          # stop the cast
    "dispel",             # remove the debuff or buff
    "tank_swap_at_stacks",  # the other tank taunts at a stack count
    "switch_adds",        # damage leaves the boss for adds
    "kill_order",         # kill things in a fixed order
    "los",                # break line of sight
    "face_away",          # turn away from the boss or an add
    "positional",         # stand at a set place (polarity sides, platforms, behind, range)
    "kite",               # drag something away and keep it moving
    "raid_cooldowns",     # a raid-wide damage spike healers and cooldowns must meet
    "use_object",         # use a vehicle, cannon, crystal, portal or item
    "heal_target",        # healers heal a friendly boss or add
    "enrage_timer",       # a hard enrage or frenzy the damage must beat
    "stop_casting",       # stop casting or attacking before a stacking debuff gets too high
})
ROLES = ("tank", "healer", "melee", "ranged")
# playerbots: the stock raid strategy (or a stock strategy every such bot has) moves or handles the
# bodies for every role the mechanic names; partial: only for some roles or conditions, said in
# handled_note; none: nothing in stock playerbots does it.
HANDLERS = frozenset({"playerbots", "partial", "none"})
# Kinds that move bodies need a distance: radius (around the danger) or range (how far to go).
MOVEMENT_KINDS = frozenset({"avoid_ground", "move_out_cone", "spread", "stack", "soak",
                            "run_out_debuff", "los"})
# Kinds that may have no triggering spell (a standing rule, or adds summoned without a spell).
NO_SPELL_KINDS = frozenset({"positional", "kill_order"})
# Kinds the native responder moves bodies for at cast start ([raidplay] respond); soak is left to a later one.
RESPOND_KINDS = frozenset({"avoid_ground", "move_out_cone", "spread", "stack", "run_out_debuff", "los"})
# Where a responded kind's danger lies when its spell starts (the responder's `area`), each kind's choices with its
# default first; "none": the cast does not say where, so the responder leaves it (README).
AREAS = MappingProxyType({
    "avoid_ground": ("dest", "caster", "line", "ring", "none"),
    "move_out_cone": ("cone", "none"),
    "spread": ("group", "none"),
    "stack": ("dest", "none"),
    "run_out_debuff": ("target", "none"),
    "los": ("caster", "none"),
})
# The core's cone when a spell names no other (Spell.cpp, 60 degrees).
DEFAULT_CONE_DEGREES = 60
# Checks run on the whole pull without a playbook row of their own (training/checks.py).
GENERIC_CHECKS = frozenset({"behind", "aggro", "tank_range", "healer_oom"})
CHECK_KINDS = frozenset({"avoid", "debuff", "buff", "adds", "spread"})
NUMBERS = ("radius", "range", "stacks", "first", "every", "duration", "cast", "targets", "angle")
# Checks version 1 is the transcription the training ground scored before the playbooks;
# version 2 adds the playbooks' further checks behind [training] playbooks.
CHECK_VERSIONS = frozenset({1, 2})

_ID = re.compile(r"[a-z0-9]+(?:_[a-z0-9]+)*")
_KEY = re.compile(r"[a-z]{2,8}")
_MAX_TEXT = 200


class PlaybookError(ValueError):
    """A data file breaks the playbook schema; the message names the file and the entry."""


@dataclass(frozen=True)
class Phase:
    id: str
    name: str
    hp_below: int | None = None
    after: Mapping[str, float] = field(default_factory=lambda: MappingProxyType({}))  # by difficulty


@dataclass(frozen=True)
class Check:
    """One scoring row for training/checks.py, with the fields of training.mechanics.Mechanic."""
    id: str
    kind: str
    since: int
    names: tuple[str, ...] = ()
    entries: tuple[int, ...] = ()
    roles: tuple[str, ...] = ()
    max_hits: int = 0
    max_count: int = 0
    max_stack: int = 0
    min_stack: int = 0
    min_pull_s: int = 0
    max_share: float = 0.0
    scope: str = "member"
    exempt: tuple[str, ...] = ()
    exempt_s: int = 0
    tag: str = ""


@dataclass(frozen=True)
class Mechanic:
    """One thing a boss does and what the raid does about it, for every difficulty.

    ``spells`` are the ids the encounter casts (what a responder watches for at cast start);
    ``hits`` are the ids that land on players when they differ (what scoring sees). Both map
    a difficulty key ("10", "25", "10h", "25h") to ids; ``spell_ids`` resolves one.
    """
    id: str
    name: str
    kind: str
    phase: tuple[str, ...]   # phase ids, or ("all",)
    roles: tuple[str, ...]
    spells: Mapping[str, tuple[int, ...]]
    hits: Mapping[str, tuple[int, ...]]
    only: tuple[str, ...]
    handled_by: str
    playerbots: tuple[str, ...]
    explain: str
    check: str
    numbers: Mapping[str, Mapping[str, float]] = field(default_factory=lambda: MappingProxyType({}))
    adds: tuple[int, ...] = ()
    handled_note: str = ""
    # The native responder's reading of it: where the danger lies at cast start ("" for a kind it never
    # responds to), and for a partial mechanic the roles and casts stock does not cover.
    area: str = ""
    respond_roles: tuple[str, ...] | None = None
    respond_spells: Mapping[str, tuple[int, ...]] | None = None
    landing: bool = False   # its place is read when the cast completes; nothing binds before

    def applies(self, difficulty: str) -> bool:
        return difficulty in self.only

    def spell_ids(self, difficulty: str) -> tuple[int, ...]:
        return self.spells.get(difficulty, ())

    def hit_ids(self, difficulty: str) -> tuple[int, ...]:
        return self.hits.get(difficulty, ()) or self.spell_ids(difficulty)

    def number(self, name: str, difficulty: str) -> float | None:
        return self.numbers.get(name, {}).get(difficulty)

    def all_ids(self) -> frozenset[int]:
        return frozenset(i for table in (self.spells, self.hits) for ids in table.values() for i in ids)

    def all_hit_ids(self) -> frozenset[int]:
        return frozenset(i for d in self.only for i in self.hit_ids(d))

    def responder_roles(self) -> tuple[str, ...]:
        """The roles the native responder may move: all of them when stock does nothing, the ones its
        handled_note says stock leaves when partial, none when stock handles it or the cast does not say
        where the danger lies."""
        if self.kind not in RESPOND_KINDS or self.area in ("", "none"):
            return ()
        if self.handled_by == "none":
            return self.roles
        return (self.respond_roles or ()) if self.handled_by == "partial" else ()

    def responder_spells(self, difficulty: str) -> tuple[int, ...]:
        """The casts the responder reacts to at a difficulty: the partial mechanic's own list when it has one."""
        if self.respond_spells is not None:
            return self.respond_spells.get(difficulty, ())
        return self.spell_ids(difficulty)


@dataclass(frozen=True)
class Boss:
    map_id: int
    index: int
    name: str
    slug: str
    creatures: tuple[int, ...]
    script: str
    enrage: Mapping[str, float]
    melee_behind: bool
    no_behind_since: int | None
    call: str | None
    heroic_call: str | None
    lust: bool
    phases: tuple[Phase, ...]
    mechanics: tuple[Mechanic, ...]
    checks: tuple[Check, ...]

    @property
    def key(self) -> tuple[int, int]:
        return self.map_id, self.index

    def call_at(self, difficulty: str) -> str | None:
        """The stock pre-pull call for a difficulty: the heroic call on heroic when there is one."""
        return self.heroic_call if difficulty.endswith("h") and self.heroic_call else self.call


@dataclass(frozen=True)
class Raid:
    map_id: int
    key: str
    name: str
    difficulties: tuple[str, ...]
    script_dir: str
    playerbots_dir: str
    bosses: tuple[Boss, ...]
    source: str


@dataclass(frozen=True)
class BossPlaybook:
    """A boss's playbook for one difficulty: only the mechanics that exist there."""
    boss: Boss
    difficulty: str
    mechanics: tuple[Mechanic, ...]

    @property
    def context(self) -> str:
        return f"{self.boss.map_id}:{self.boss.index}:{DIFFICULTY_KEYS.index(self.difficulty)}"

    @property
    def enrage(self) -> float | None:
        return self.boss.enrage.get(self.difficulty)


class Playbooks:
    """Every raid's playbooks; lookups never return mutable containers."""

    def __init__(self, raids: tuple[Raid, ...]):
        self.raids = MappingProxyType({r.map_id: r for r in sorted(raids, key=lambda r: r.map_id)})
        self._bosses = MappingProxyType({b.key: b for r in raids for b in r.bosses})
        mechanics, checks = {}, {}
        for boss in self._bosses.values():
            for m in boss.mechanics:
                if m.id in mechanics or m.id in checks:
                    raise PlaybookError(f"duplicate id {m.id}")
                mechanics[m.id] = m
            for c in boss.checks:
                if c.id in mechanics or c.id in checks:
                    raise PlaybookError(f"duplicate id {c.id}")
                checks[c.id] = c
        self._mechanics = MappingProxyType(mechanics)
        self._checks = MappingProxyType(checks)
        by_spell: dict[int, list[Mechanic]] = {}
        for m in mechanics.values():
            for spell in sorted(m.all_ids()):
                by_spell.setdefault(spell, []).append(m)
        self._by_spell = MappingProxyType({k: tuple(v) for k, v in by_spell.items()})

    def covered_maps(self) -> tuple[int, ...]:
        return tuple(sorted(self.raids))

    def bosses(self) -> tuple[Boss, ...]:
        return tuple(self._bosses[k] for k in sorted(self._bosses))

    def boss(self, map_id: int, boss_index: int) -> Boss | None:
        return self._bosses.get((map_id, boss_index))

    def playbook_for(self, map_id: int, boss_index: int, difficulty: int) -> BossPlaybook | None:
        """The boss's playbook at a training difficulty (0 10-man, 1 25-man, 2 10 heroic,
        3 25 heroic); None when the raid is not covered or has no such difficulty."""
        boss, raid = self._bosses.get((map_id, boss_index)), self.raids.get(map_id)
        key = DIFFICULTIES.get(difficulty) if type(difficulty) is int else None
        if boss is None or key is None or key not in raid.difficulties:
            return None
        return BossPlaybook(boss, key, tuple(m for m in boss.mechanics if m.applies(key)))

    def mechanic(self, mechanic_id: str) -> Mechanic | None:
        return self._mechanics.get(mechanic_id)

    def mechanics_for_spell(self, spell_id: int) -> tuple[Mechanic, ...]:
        """Every mechanic that casts or lands this spell id, at any difficulty (a responder's
        lookup from a cast start); check a mechanic's own spell_ids(d) for the difficulty."""
        return self._by_spell.get(spell_id, ())

    def check(self, check_id: str) -> Check | None:
        return self._checks.get(check_id)

    def mechanics(self) -> tuple[Mechanic, ...]:
        return tuple(self._mechanics.values())


def _fail(where: str, why: str):
    raise PlaybookError(f"{where}: {why}")


def _known(table: dict, allowed: set[str], where: str):
    for key in table:
        if key not in allowed:
            _fail(where, f"unknown key {key}")


def _text(value, where: str, *, empty=False, limit=_MAX_TEXT) -> str:
    if type(value) is not str or (not empty and not value.strip()) or len(value) > limit \
            or any(ord(c) < 32 for c in value):
        _fail(where, f"expected one line of text up to {limit} characters")
    return value


def _int(value, where: str, lo=0, hi=10**7) -> int:
    if type(value) is not int or not lo <= value <= hi:
        _fail(where, f"expected an integer {lo}-{hi}")
    return value


def _ints(value, where: str, *, lo=1, hi=10**7) -> tuple[int, ...]:
    if not isinstance(value, list):
        _fail(where, "expected a list of integers")
    return tuple(_int(v, where, lo, hi) for v in value)


def _number(value, where: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        _fail(where, "expected a finite number of at least 0")
    return value


def _per_difficulty(value, where: str, diffs: tuple[str, ...], parse) -> dict[str, object]:
    """A value for every difficulty, or a table keyed by difficulty. A heroic key missing
    from a table falls back to the normal size's value; a missing normal key means none."""
    if isinstance(value, dict):
        for key in value:
            if key not in diffs:
                _fail(where, f"difficulty {key} is not one of {', '.join(diffs)}")
        out = {}
        for key in diffs:
            if key in value:
                out[key] = parse(value[key], f"{where}.{key}")
            elif key.endswith("h") and key[:-1] in value:
                out[key] = parse(value[key[:-1]], f"{where}.{key[:-1]}")
        return out
    parsed = parse(value, where)
    return {key: parsed for key in diffs}


def _check(raw, where: str, slug_prefix: str) -> Check:
    if not isinstance(raw, dict):
        _fail(where, "expected a table")
    _known(raw, {"id", "kind", "since", "names", "entries", "roles", "max_hits", "max_count",
                 "max_stack", "min_stack", "min_pull_s", "max_share", "scope", "exempt",
                 "exempt_s", "tag"}, where)
    cid = _text(raw.get("id"), f"{where}.id")
    if not cid.startswith(slug_prefix + "check_") or not _ID.fullmatch(cid[len(slug_prefix):]):
        _fail(where, f"check id {cid} must be {slug_prefix}check_<lower_snake>")
    kind = raw.get("kind")
    if kind not in CHECK_KINDS:
        _fail(where, f"check kind {kind!r} is not one of {', '.join(sorted(CHECK_KINDS))}")
    since = raw.get("since")
    if since not in CHECK_VERSIONS:
        _fail(where, "since must be 1 or 2")
    names = tuple(_text(n, f"{where}.names") for n in raw.get("names", []))
    exempt = tuple(_text(n, f"{where}.exempt") for n in raw.get("exempt", []))
    roles = tuple(raw.get("roles", []))
    if any(r not in ROLES for r in roles):
        _fail(where, "roles must be tank, healer, melee or ranged")
    scope = raw.get("scope", "member")
    if scope not in ("member", "raid"):
        _fail(where, "scope must be member or raid")
    share = raw.get("max_share", 0.0)
    if type(share) not in (int, float) or not 0 <= share <= 1:
        _fail(where, "max_share must be 0-1")
    if kind in ("avoid", "debuff", "buff") and not names:
        _fail(where, f"a {kind} check needs names")
    if kind == "adds" and not raw.get("entries"):
        _fail(where, "an adds check needs entries")
    return Check(cid, kind, since, names, _ints(raw.get("entries", []), f"{where}.entries"), roles,
                 *(_int(raw.get(k, 0), f"{where}.{k}", 0, 10**6)
                   for k in ("max_hits", "max_count", "max_stack", "min_stack", "min_pull_s")),
                 float(share), scope, exempt, _int(raw.get("exempt_s", 0), f"{where}.exempt_s", 0, 3600),
                 _text(raw.get("tag", ""), f"{where}.tag", empty=True, limit=40))


def _mechanic(raw, where: str, diffs: tuple[str, ...], phases: set[str], slug_prefix: str) -> Mechanic:
    if not isinstance(raw, dict):
        _fail(where, "expected a table")
    _known(raw, {"id", "name", "kind", "phase", "roles", "spells", "hits", "only", "handled_by",
                 "playerbots", "handled_note", "explain", "check", "adds", "area", "respond_roles",
                 "respond_spells", "landing", *NUMBERS}, where)
    mid = _text(raw.get("id"), f"{where}.id")
    if (not mid.startswith(slug_prefix) or mid.startswith(slug_prefix + "check_")
            or not _ID.fullmatch(mid[len(slug_prefix):])):
        _fail(where, f"mechanic id {mid} must be {slug_prefix}<lower_snake>")
    where = f"{where} ({mid})"
    kind = raw.get("kind")
    if kind not in KINDS:
        _fail(where, f"kind {kind!r} is not a known kind")
    phase = raw.get("phase", "all")
    phase = tuple(phase) if isinstance(phase, list) else (phase,)
    if phase != ("all",) and (not phase or any(p not in phases for p in phase) or len(set(phase)) != len(phase)):
        _fail(where, "phase must be all, or one or more of the boss's phase ids")
    roles = raw.get("roles")
    if not isinstance(roles, list) or not roles or any(r not in ROLES for r in roles) \
            or len(set(roles)) != len(roles):
        _fail(where, "roles must list tank, healer, melee or ranged, each once")
    only = tuple(raw.get("only", diffs))
    if not only or any(d not in diffs for d in only) or len(set(only)) != len(only):
        _fail(where, f"only must list difficulties of the raid: {', '.join(diffs)}")
    ids = functools.partial(_ints, lo=1, hi=10**6)
    if "spells" not in raw:
        _fail(where, "spells is required (an empty list for a rule or adds without a spell)")
    spells = _per_difficulty(raw["spells"], f"{where}.spells", diffs, ids)
    hits = _per_difficulty(raw["hits"], f"{where}.hits", diffs, ids) if "hits" in raw else {}
    adds = _ints(raw.get("adds", []), f"{where}.adds")
    for d in only:
        if not spells.get(d) and not adds and kind not in NO_SPELL_KINDS:
            _fail(where, f"no spell ids for difficulty {d} (only adds or a positional or "
                         "kill_order rule may have none)")
    handled = raw.get("handled_by")
    if handled not in HANDLERS:
        _fail(where, "handled_by must be playerbots, partial or none")
    stock = raw.get("playerbots", [])
    if not isinstance(stock, list):
        _fail(where, "playerbots must be a list of stock action or strategy names")
    playerbots = tuple(_text(a, f"{where}.playerbots", limit=60) for a in stock)
    if (handled != "none") != bool(playerbots) or len(set(playerbots)) != len(playerbots):
        _fail(where, "playerbots names the stock actions exactly when handled_by is playerbots or partial")
    note = _text(raw.get("handled_note", ""), f"{where}.handled_note", empty=True)
    if handled == "partial" and not note.strip() or handled == "none" and note:
        _fail(where, "handled_note says whom and when stock covers: required for partial, "
                     "not allowed for none")
    numbers = {}
    for name in NUMBERS:
        if name in raw:
            values = _per_difficulty(raw[name], f"{where}.{name}", diffs, _number)
            numbers[name] = MappingProxyType({d: v for d, v in values.items() if d in only})
    if kind in MOVEMENT_KINDS:
        for d in only:
            if not any(d in numbers.get(n, {}) for n in ("radius", "range")):
                _fail(where, f"a {kind} mechanic needs a radius or range (yards) for difficulty {d}")
    return Mechanic(mid, _text(raw.get("name"), f"{where}.name", limit=80), kind, phase, tuple(roles),
                    MappingProxyType({d: spells[d] for d in only if spells.get(d)}),
                    MappingProxyType({d: hits[d] for d in only if d in hits}), only, handled,
                    playerbots, _text(raw.get("explain"), f"{where}.explain"),
                    _text(raw.get("check", "none"), f"{where}.check", limit=80),
                    MappingProxyType(numbers), adds, note,
                    *_respond(raw, where, kind, handled, tuple(roles), only, spells, numbers))


def _respond(raw, where, kind, handled, roles, only, spells, numbers):
    """The responder's fields: area (each kind's default when absent), for a partial mechanic the roles
    stock does not cover (no respond_roles: the responder leaves it), the casts it answers (respond_spells,
    the ones stock leaves or that can be answered before they hit) and whether it waits for the landing."""
    choices = AREAS.get(kind)
    allowed = choices or ("none",)
    area = raw.get("area", choices[0] if choices else "")
    if "area" in raw and (type(area) is not str or area not in allowed):
        _fail(where, f"area must be one of {', '.join(allowed)} for a {kind} mechanic")
    if not choices:
        area = ""
    if area in ("line", "ring"):
        for d in only:
            if d not in numbers.get("range", {}) or d not in numbers.get("radius", {}):
                _fail(where, f"a {area} needs a radius (half its width) and a range (its length or its radius) "
                             f"for difficulty {d}")
    if "angle" in numbers and area != "cone":
        _fail(where, "angle (degrees) is for a cone")
    if any(not 0 < v <= 360 for v in numbers.get("angle", {}).values()):
        _fail(where, "angle must be more than 0 and at most 360 degrees")
    if "respond_roles" in raw and handled != "partial":
        _fail(where, "respond_roles is only for a partial mechanic")
    if "respond_spells" in raw and handled == "playerbots":
        _fail(where, "respond_spells is not for a mechanic stock handles")
    landing = raw.get("landing", False)
    if type(landing) is not bool or landing and area not in ("dest", "ring"):
        _fail(where, "landing (true or false) is for a dest or ring area")
    respond_roles = None
    if "respond_roles" in raw:
        value = raw["respond_roles"]
        if (not isinstance(value, list) or any(r not in roles for r in value)
                or len(set(value)) != len(value)):
            _fail(where, "respond_roles lists some of the mechanic's roles, each once")
        respond_roles = tuple(r for r in ROLES if r in value)
    respond_spells = None
    if "respond_spells" in raw:
        ids = functools.partial(_ints, lo=1, hi=10**6)
        table = _per_difficulty(raw["respond_spells"], f"{where}.respond_spells", only, ids)
        for d, chosen in table.items():
            if any(i not in spells.get(d, ()) for i in chosen):
                _fail(where, f"respond_spells for difficulty {d} must be among its spells")
        respond_spells = MappingProxyType({d: table[d] for d in only if table.get(d)})
    return area, respond_roles, respond_spells, landing


def _boss(raw, where: str, raid_key: str, map_id: int, diffs: tuple[str, ...]) -> Boss:
    if not isinstance(raw, dict):
        _fail(where, "expected a table")
    _known(raw, {"index", "name", "slug", "creatures", "script", "enrage", "melee_behind",
                 "no_behind_since", "call", "heroic_call", "lust", "phase", "mechanic", "check"}, where)
    index = _int(raw.get("index"), f"{where}.index", 0, 255)
    name = _text(raw.get("name"), f"{where}.name", limit=80)
    known = BOSS_NAMES.get((map_id, index))
    if known != name:
        _fail(where, f"boss {index} is {known!r} in wow.BOSS_NAMES, not {name!r}")
    where = f"{where} ({name})"
    slug = _text(raw.get("slug"), f"{where}.slug", limit=40)
    if not _ID.fullmatch(slug):
        _fail(where, "slug must be lower_snake")
    prefix = f"{raid_key}.{slug}."
    behind = raw.get("melee_behind", True)
    if type(behind) is not bool:
        _fail(where, "melee_behind must be a boolean")
    since = raw.get("no_behind_since")
    if behind != (since is None) or since is not None and since not in CHECK_VERSIONS:
        _fail(where, "no_behind_since (1 or 2) is set exactly when melee_behind is false")
    call = raw.get("call")
    if call is not None:
        _text(call, f"{where}.call", limit=60)
    heroic_call = raw.get("heroic_call")
    if heroic_call is not None:
        _text(heroic_call, f"{where}.heroic_call", limit=60)
        if call is None or not any(d.endswith("h") for d in diffs):
            _fail(where, "heroic_call needs a call and a raid with heroic difficulties")
    lust = raw.get("lust", True)
    if type(lust) is not bool:
        _fail(where, "lust must be a boolean")
    phases = []
    for i, p in enumerate(raw.get("phase", [])):
        pw = f"{where}.phase[{i}]"
        if not isinstance(p, dict):
            _fail(pw, "expected a table")
        _known(p, {"id", "name", "hp_below", "after"}, pw)
        pid = _text(p.get("id"), f"{pw}.id", limit=40)
        if not _ID.fullmatch(pid) or pid == "all":
            _fail(pw, "phase id must be lower_snake and not all")
        hp = p.get("hp_below")
        after = p.get("after")
        phases.append(Phase(pid, _text(p.get("name"), f"{pw}.name", limit=80),
                            None if hp is None else _int(hp, f"{pw}.hp_below", 1, 100),
                            MappingProxyType({} if after is None else
                                             _per_difficulty(after, f"{pw}.after", diffs, _number))))
    phase_ids = {p.id for p in phases}
    if len(phase_ids) != len(phases):
        _fail(where, "phase ids must be unique")
    mechanics = tuple(_mechanic(m, f"{where}.mechanic[{i}]", diffs, phase_ids, prefix)
                      for i, m in enumerate(raw.get("mechanic", [])))
    checks = tuple(_check(c, f"{where}.check[{i}]", prefix) for i, c in enumerate(raw.get("check", [])))
    if not mechanics:
        _fail(where, "a boss needs at least one mechanic")
    own = {c.id for c in checks}
    for m in mechanics:
        if m.check != "none" and m.check not in GENERIC_CHECKS and m.check not in own:
            _fail(where, f"{m.id} maps to check {m.check}, which is not one of this boss's checks")
    for c in checks:
        if not any(m.check == c.id for m in mechanics):
            _fail(where, f"check {c.id} is not mapped from any mechanic")
    enrage = _per_difficulty(raw["enrage"], f"{where}.enrage", diffs, _number) if "enrage" in raw else {}
    return Boss(map_id, index, name, slug, _ints(raw.get("creatures", []), f"{where}.creatures"),
                _text(raw.get("script"), f"{where}.script", limit=120), MappingProxyType(enrage),
                behind, since, call, heroic_call, lust, tuple(phases), mechanics, checks)


def parse_raid(data: dict, source: str) -> Raid:
    where = source
    _known(data, {"map", "key", "difficulties", "script_dir", "playerbots_dir", "boss"}, where)
    map_id = _int(data.get("map"), f"{where}.map", 1, 10000)
    if map_id not in RAID_INSTANCES:
        _fail(where, f"map {map_id} is not a raid in wow.RAID_INSTANCES")
    key = data.get("key")
    if type(key) is not str or not _KEY.fullmatch(key):
        _fail(where, "key must be 2-8 lowercase letters")
    diffs = data.get("difficulties")
    if not isinstance(diffs, list) or not diffs or any(d not in DIFFICULTY_KEYS for d in diffs) \
            or len(set(diffs)) != len(diffs):
        _fail(where, f"difficulties must list some of {', '.join(DIFFICULTY_KEYS)}")
    diffs = tuple(d for d in DIFFICULTY_KEYS if d in diffs)
    bosses = tuple(_boss(b, f"{where}.boss[{i}]", key, map_id, diffs)
                   for i, b in enumerate(data.get("boss", [])))
    indexes = [b.index for b in bosses]
    if len(set(indexes)) != len(indexes) or len({b.slug for b in bosses}) != len(bosses):
        _fail(where, "boss indexes and slugs must be unique")
    return Raid(map_id, key, RAID_INSTANCES[map_id], diffs,
                _text(data.get("script_dir"), f"{where}.script_dir", limit=120),
                _text(data.get("playerbots_dir", ""), f"{where}.playerbots_dir", empty=True, limit=120),
                bosses, source)


def load_files(files: dict[str, bytes]) -> Playbooks:
    """Parse named TOML documents (a test's or the package's) into Playbooks."""
    raids = []
    for name in sorted(files):
        try:
            data = tomllib.loads(files[name].decode("utf-8"))
        except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            raise PlaybookError(f"{name}: not valid TOML ({type(exc).__name__})") from None
        raids.append(parse_raid(data, name))
    maps = [r.map_id for r in raids]
    if len(set(maps)) != len(maps) or len({r.key for r in raids}) != len(raids):
        raise PlaybookError("two files describe the same raid")
    return Playbooks(tuple(raids))


@functools.cache
def load() -> Playbooks:
    """The repo's playbooks (package data), parsed once per process."""
    folder = resources.files("leeroy_mind.raidplay") / "data"
    return load_files({f.name: f.read_bytes() for f in folder.iterdir() if f.name.endswith(".toml")})
