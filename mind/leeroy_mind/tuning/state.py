"""The tuning in force: tuning_value's rows, read once and folded per member.

Precedence (owner, 2026-11-04): a member's own value (a pin, set by the owner for that member)
wins over every group and world change. Otherwise the value is the persona's own plus the
offsets of every group the member is in, "everyone" included, clamped to the dial's range.
World values override mind.toml's knobs. The owner's own character and anyone off the cast are
never tuned."""
import json
import time
import sqlite3
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Mapping

from . import edits as E
from . import groups as G
from . import nudges as N
from .dials import MEMBER_DIALS, WORLD_DIALS, applies, base_value, bounds
from .effective import NEUTRAL, MemberTuning, effective

TABLES = ("tuning_group", "tuning_change", "tuning_row", "tuning_value")


def _frozen(d: dict) -> Mapping:
    return MappingProxyType(dict(d))


@dataclass(frozen=True)
class Tuning:
    """version: the newest change id (0: none). world: dial -> override. groups: target ->
    Group (everyone included). offsets: group target -> dial -> offset. pins: guid -> dial ->
    value, with the persona version each was set against in pin_versions. human: the owner's
    own guid (never tuned)."""
    version: int = 0
    world: Mapping[str, int] = field(default_factory=lambda: _frozen({}))
    groups: Mapping[str, G.Group] = field(
        default_factory=lambda: _frozen({G.EVERYONE: G.EVERYONE_GROUP}))
    offsets: Mapping[str, Mapping[str, int]] = field(default_factory=lambda: _frozen({}))
    pins: Mapping[int, Mapping[str, int]] = field(default_factory=lambda: _frozen({}))
    pin_versions: Mapping[tuple, int | None] = field(default_factory=lambda: _frozen({}))
    human: int = 0
    nudges: tuple[N.Nudge, ...] = ()
    edits: tuple[dict, ...] = ()  # godmode persona overlays in force (tuning.edits.load)

    @property
    def empty(self) -> bool:
        return not (self.world or self.offsets or self.pins or self.nudges or self.edits)

    @property
    def needs_cast(self) -> bool:
        targets = set(self.offsets) | {n.target for n in self.nudges if n.target_kind == "group"}
        return any(G.uses_cast(self.groups[t].where) for t in targets if t in self.groups)

    # -- reading -------------------------------------------------------------------------------

    @classmethod
    def load(cls, conn: sqlite3.Connection, now: float | None = None) -> "Tuning":
        """The values in force; an empty Tuning for a database without the tables (an older
        mind.db opened read-only) or with none set."""
        have = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'tuning_%'")}
        human_row = conn.execute("SELECT value FROM meta WHERE key='human_guid'").fetchone()
        human = int(human_row[0]) if human_row and str(human_row[0]).isdigit() else 0
        if not set(TABLES) <= have:
            return cls(human=human)
        groups = {G.EVERYONE: G.EVERYONE_GROUP}
        for r in conn.execute("SELECT id,name,predicate,created FROM tuning_group "
                              "WHERE retired IS NULL ORDER BY id"):
            try:
                where = G.parse_where(json.loads(r[2]))
            except (ValueError, TypeError):
                continue  # a row this code cannot read never matches anyone
            groups[str(r[0])] = G.Group(str(r[0]), r[1], where, created=float(r[3]))
        world, offsets, pins, versions = {}, {}, {}, {}
        for scope, target, dial, value, version in conn.execute(
                "SELECT scope,target,dial,value,persona_version FROM tuning_value "
                "ORDER BY scope,target,dial"):
            if scope == "world" and dial in WORLD_DIALS:
                world[dial] = value
            elif scope == "group" and dial in MEMBER_DIALS and target in groups:
                offsets.setdefault(target, {})[dial] = value
            elif scope == "member" and dial in MEMBER_DIALS and target.isdigit():
                pins.setdefault(int(target), {})[dial] = value
                versions[(int(target), dial)] = version
        row = conn.execute("SELECT max(id) FROM tuning_change").fetchone()
        return cls(version=int(row[0] or 0), world=_frozen(world), groups=_frozen(groups),
                   offsets=_frozen({t: _frozen(d) for t, d in offsets.items()}),
                   pins=_frozen({g: _frozen(d) for g, d in pins.items()}),
                   pin_versions=_frozen(versions), human=human, nudges=N.load(conn, time.time() if now is None else now),
                   edits=E.load(conn, time.time() if now is None else now))

    # -- folding -------------------------------------------------------------------------------

    def groups_of(self, p, member) -> list[str]:
        """The targets of the groups this member is in, everyone first."""
        if p is None or p.guid == self.human:
            return []
        return [t for t, g in self.groups.items() if g.builtin or G.matches(g.where, p, member)]

    def member(self, p, member=None) -> MemberTuning:
        """The member's effective dial values (only those that differ from the persona as
        written). NEUTRAL for the owner, for no persona, and when nothing reaches them."""
        if p is None or p.guid == self.human or not (self.offsets or self.pins):
            return NEUTRAL
        pins = self.pins.get(p.guid, {})
        mine = [t for t in self.groups_of(p, member) if t in self.offsets]
        values = {}
        for dial in MEMBER_DIALS:
            if not applies(dial, p):
                continue
            base = base_value(dial, p)
            if dial in pins:
                v = pins[dial]
            else:
                extra = sum(self.offsets[t].get(dial, 0) for t in mine)
                if not extra:
                    continue
                lo, hi = bounds(dial)
                v = max(lo, min(hi, base + extra))
            if v != base:
                values[dial] = v
        if not values:
            return NEUTRAL
        return MemberTuning(_frozen(values), frozenset(d for d in pins if d in values))

    def apply(self, p, member=None, *, edits: bool = True):
        """The effective persona (the stored object itself when nothing reaches it). `edits`
        False leaves the owner's godmode edits out: the night reads her as stored and dialled,
        the edits being a lens for talk only."""
        from .effective import with_edits, with_nudges
        q = with_nudges(effective(p, self.member(p, member)),
                        N.for_member(self.nudges, p, member, self.groups, self.human))
        if not edits or not self.edits or p is None or p.guid == self.human:
            return q
        return with_edits(q, E.member_edits(self.edits, p.guid))

    # -- candidates (the preview's "after") ------------------------------------------------------

    def with_value(self, scope: str, target: str, dial: str, value: int | None) -> "Tuning":
        """This tuning with one value set (None: cleared), not stored."""
        if scope == "world":
            world = dict(self.world)
            world.pop(dial, None) if value is None else world.__setitem__(dial, value)
            return replace(self, world=_frozen(world))
        if scope == "group":
            offsets = {t: dict(d) for t, d in self.offsets.items()}
            row = offsets.setdefault(target, {})
            row.pop(dial, None) if value is None else row.__setitem__(dial, value)
            return replace(self, offsets=_frozen({t: _frozen(d) for t, d in offsets.items() if d}))
        guid = int(target)
        pins = {g: dict(d) for g, d in self.pins.items()}
        row = pins.setdefault(guid, {})
        row.pop(dial, None) if value is None else row.__setitem__(dial, value)
        return replace(self, pins=_frozen({g: _frozen(d) for g, d in pins.items() if d}))

    def value_of(self, scope: str, target: str, dial: str) -> int | None:
        if scope == "world":
            return self.world.get(dial)
        if scope == "group":
            return self.offsets.get(target, {}).get(dial)
        return self.pins.get(int(target), {}).get(dial)


EMPTY = Tuning()
