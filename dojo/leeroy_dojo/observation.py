"""The v1 actor observation: its ordered schema, decoding, and what an initial observation must be.

mod-dojo's DojoPerception.cpp builds the same schema from the same action catalog (the canonical JSON and its SHA-256
are compared in tests/fixtures/observation_cases.json). Every value is a raw quantity divided by the field's scale and
clipped to [low, high]; a field takes its `missing` value when what it describes is absent (no enemy seen, an empty
event slot, nothing pending). Flags are 0 or 1.

Order: context (class one-hot, level, talent points per tree), the match's remaining time, self (alive, health, mana,
global cooldown, cast, spell queue, per kit spell known / cooldown / cost / in range, position in the arena frame,
facing, distance to the edge, velocity, movement, run speed, roots, stuns, silences, freezes, combat, Fingers of
Frost, the pending command), the enemy slot (presence, visibility and age; last-seen health, mana and position;
while visible its facing, velocity, cast bar, effects and target; per kit spell an inferred cooldown from uses the
actor saw, with an unknown flag), then the newest events the actor saw, one slot each.

All of it is what the actor perceived perceptual-delay milliseconds before the decision. Authoritative debug state is
never part of it; the runtime keeps that in separate records.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass

from .actions import Catalog
from .scenario import canonical

SCHEMA = "leeroy-dojo-observation-v1"
EVENT_SLOTS = 4
TALENT_TREES = 3
EVENT_KINDS = ("cast_started", "cast_completed", "cast_stopped", "died")
CLASSES = ((1, "warrior"), (2, "paladin"), (3, "hunter"), (4, "rogue"), (5, "priest"), (6, "death_knight"),
           (7, "shaman"), (8, "mage"), (9, "warlock"), (11, "druid"))


@dataclass(frozen=True)
class Field:
    name: str
    unit: str
    scale: int = 1
    low: int = 0
    high: int = 1
    missing: int = 0


def _fields(kit: list[str], abilities: list[str]) -> list[Field]:
    f: list[Field] = []

    def flag(name: str) -> None:
        f.append(Field(name, "flag"))

    for _, name in CLASSES:
        flag(f"context.class.{name}")
    f.append(Field("context.level", "level", 80))
    for t in range(TALENT_TREES):
        f.append(Field(f"context.tree.{t}", "talent points", 71))
    f.append(Field("match.remaining", "fraction of the match limit"))

    flag("self.alive")
    f.append(Field("self.health", "fraction of max health"))
    f.append(Field("self.mana", "fraction of max mana"))
    f.append(Field("self.gcd", "ms", 1500))
    flag("self.casting")
    for k in kit:
        flag(f"self.cast.{k}")
    f.append(Field("self.cast.remaining", "ms", 3000))
    f.append(Field("self.cast.progress", "fraction"))
    flag("self.queued")
    for k in kit:
        flag(f"self.kit.{k}.known")
        f.append(Field(f"self.kit.{k}.cooldown", "ms", 30000))
        f.append(Field(f"self.kit.{k}.cost", "fraction of max mana"))
        flag(f"self.kit.{k}.in_range")
    f.append(Field("self.x", "fraction of the arena radius", 1, -1, 1))
    f.append(Field("self.y", "fraction of the arena radius", 1, -1, 1))
    f.append(Field("self.z", "fraction of the arena height"))
    f.append(Field("self.facing.sin", "unit", 1, -1, 1))
    f.append(Field("self.facing.cos", "unit", 1, -1, 1))
    f.append(Field("self.edge", "fraction of the arena radius", 1, -1, 1))
    f.append(Field("self.velocity.x", "yd/s", 14, -1, 1))
    f.append(Field("self.velocity.y", "yd/s", 14, -1, 1))
    flag("self.moving")
    f.append(Field("self.speed", "yd/s", 14))
    flag("self.rooted")
    f.append(Field("self.root.remaining", "ms", 10000))
    flag("self.stunned")
    flag("self.silenced")
    flag("self.frozen")
    flag("self.in_combat")
    f.append(Field("self.fingers_of_frost", "charges", 2))
    flag("self.pending")
    for a in abilities:
        flag(f"self.pending.{a}")
    flag("self.pending.moves")
    f.append(Field("self.pending.due", "ms", 200))

    flag("enemy.present")
    flag("enemy.visible")
    f.append(Field("enemy.age", "ms", 10000, 0, 1, 1))
    flag("enemy.alive")
    f.append(Field("enemy.health", "fraction of max health"))
    f.append(Field("enemy.mana", "fraction of max mana"))
    f.append(Field("enemy.forward", "yd", 40, -1, 1))
    f.append(Field("enemy.left", "yd", 40, -1, 1))
    f.append(Field("enemy.up", "yd", 10, -1, 1))
    f.append(Field("enemy.distance", "yd", 40))
    f.append(Field("enemy.facing_me", "cosine", 1, -1, 1))
    f.append(Field("enemy.velocity.forward", "yd/s", 14, -1, 1))
    f.append(Field("enemy.velocity.left", "yd/s", 14, -1, 1))
    flag("enemy.casting")
    for k in kit:
        flag(f"enemy.cast.{k}")
    f.append(Field("enemy.cast.progress", "fraction"))
    flag("enemy.rooted")
    f.append(Field("enemy.root.remaining", "ms", 10000))
    flag("enemy.stunned")
    flag("enemy.frozen")
    flag("enemy.slowed")
    flag("enemy.targeting_me")
    for k in kit:
        f.append(Field(f"enemy.kit.{k}.unknown", "flag", 1, 0, 1, 1))
        f.append(Field(f"enemy.kit.{k}.cooldown", "ms", 30000))

    for i in range(EVENT_SLOTS):
        flag(f"event.{i}.present")
        f.append(Field(f"event.{i}.age", "ms", 2000, 0, 1, 1))
        flag(f"event.{i}.by_self")
        for kind in EVENT_KINDS:
            flag(f"event.{i}.kind.{kind}")
        for k in kit:
            flag(f"event.{i}.kit.{k}")
    return f


@dataclass
class Schema:
    fields: list[Field]
    kit: list[str]
    abilities: list[str]
    document: dict
    sha256: str

    def index(self, name: str) -> int:
        for i, f in enumerate(self.fields):
            if f.name == name:
                return i
        raise KeyError(name)

    @property
    def size(self) -> int:
        return len(self.fields)

    def decode(self, values: list[float]) -> dict[str, float]:
        if len(values) != len(self.fields):
            raise ValueError(f"{len(values)} values for a schema of {len(self.fields)}")
        return {f.name: v for f, v in zip(self.fields, values)}

    def check(self, values: list[float]) -> list[str]:
        """Every value that is not a finite number inside its field's bounds."""
        problems = []
        if len(values) != len(self.fields):
            return [f"{len(values)} values for a schema of {len(self.fields)}"]
        for f, v in zip(self.fields, values):
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
                problems.append(f"{f.name}: {v!r} is not a number")
            elif not f.low <= v <= f.high:
                problems.append(f"{f.name}: {v} outside [{f.low}, {f.high}]")
            elif f.unit == "flag" and v not in (0, 1):
                problems.append(f"{f.name}: {v} is not a flag")
        return problems


def build_schema(catalog: Catalog) -> Schema:
    abilities = [a.name for a in catalog.abilities]
    kit = [a.name for a in catalog.abilities if a.kind == "cast"]
    fields = _fields(kit, abilities)
    document = {
        "schema": SCHEMA, "kit": kit, "abilities": abilities,
        "classes": [{"id": i, "name": n} for i, n in CLASSES],
        "event_kinds": list(EVENT_KINDS), "event_slots": EVENT_SLOTS, "size": len(fields),
        "fields": [{"name": x.name, "unit": x.unit, "scale": x.scale, "low": x.low, "high": x.high,
                    "missing": x.missing} for x in fields],
    }
    return Schema(fields, kit, abilities, document, hashlib.sha256(canonical(document)).hexdigest())


def round_value(v: float) -> float:
    r = round(v * 1e6) / 1e6
    return 0.0 if r == 0 else r


def _put(values: list[float], schema: Schema, name: str, raw: float) -> None:
    f = schema.fields[schema.index(name)]
    values[schema.index(name)] = round_value(min(max(raw / f.scale, f.low), f.high))


def expected_initial(schema: Schema, catalog: Catalog, manifest: dict, slot: int, context: dict, *,
                     enemy_visible: bool, start_offset_ms: int = 0, kit_costs: list[int] | None = None,
                     run_speed: float = 7.0) -> list[float]:
    """The observation an actor must have at an episode's first decision, from the manifest alone: at its start
    position and facing, full health and mana, nothing cast, on cooldown, queued or pending, not in combat, the match
    ahead less start_offset_ms (how long after the episode's start its first matured percept was taken), and no enemy
    seen while the start positions are out of each other's sight.

    context is the worker's published context (class id, level, talent points per tree), checked against the manifest
    by context_problems(). kit_costs, when given, are the mana costs the worker reported (spell data, not the
    manifest). When enemy_visible, the enemy's slot is filled from its start position too, and the in-range flags of
    enemy-targeted spells (which need the spells' ranges) are left to the caller."""
    values = [float(f.missing) for f in schema.fields]
    for _, name in CLASSES:
        _put(values, schema, f"context.class.{name}", 0.0)
    class_name = dict(CLASSES).get(manifest["loadout"]["class"]["id"])
    if class_name:
        _put(values, schema, f"context.class.{class_name}", 1.0)
    _put(values, schema, "context.level", manifest["loadout"]["level"])
    for t, points in enumerate(context["tree_points"]):
        _put(values, schema, f"context.tree.{t}", points)
    match_ms = manifest["rules"]["match_seconds"] * 1000
    _put(values, schema, "match.remaining", max(0, match_ms - start_offset_ms) / match_ms)
    _put(values, schema, "self.alive", 1)
    _put(values, schema, "self.health", 1)
    _put(values, schema, "self.mana", 1)
    max_mana = manifest["loadout"]["initial"]["max_mana"]
    casts = [a for a in catalog.abilities if a.kind == "cast"]
    for i, k in enumerate(schema.kit):
        _put(values, schema, f"self.kit.{k}.known", 1)
        if kit_costs is not None:
            _put(values, schema, f"self.kit.{k}.cost", kit_costs[i] / max_mana)
        # A self-centred spell is always in range; an enemy-targeted one needs a visible enemy in range.
        _put(values, schema, f"self.kit.{k}.in_range", 1 if casts[i].target == "self" else 0)
    b = manifest["arena"]["bounds"]
    me = manifest["actors"][slot]["start"]
    other = manifest["actors"][1 - slot]["start"]
    dx, dy = me["x"] - b["x"], me["y"] - b["y"]
    _put(values, schema, "self.x", dx / b["radius"])
    _put(values, schema, "self.y", dy / b["radius"])
    _put(values, schema, "self.z", (me["z"] - b["z_min"]) / (b["z_max"] - b["z_min"]))
    _put(values, schema, "self.facing.sin", math.sin(me["o"]))
    _put(values, schema, "self.facing.cos", math.cos(me["o"]))
    _put(values, schema, "self.edge", (b["radius"] - math.hypot(dx, dy)) / b["radius"])
    _put(values, schema, "self.speed", run_speed)
    if enemy_visible:
        ex, ey, ez = other["x"] - me["x"], other["y"] - me["y"], other["z"] - me["z"]
        co, si = math.cos(me["o"]), math.sin(me["o"])
        _put(values, schema, "enemy.present", 1)
        _put(values, schema, "enemy.visible", 1)
        _put(values, schema, "enemy.age", 0)
        _put(values, schema, "enemy.alive", 1)
        _put(values, schema, "enemy.health", 1)
        _put(values, schema, "enemy.mana", 1)
        _put(values, schema, "enemy.forward", ex * co + ey * si)
        _put(values, schema, "enemy.left", -ex * si + ey * co)
        _put(values, schema, "enemy.up", ez)
        _put(values, schema, "enemy.distance", math.sqrt(ex * ex + ey * ey + ez * ez))
        _put(values, schema, "enemy.facing_me", math.cos(math.atan2(-ey, -ex) - other["o"]))
        for name in ("enemy.velocity.forward", "enemy.velocity.left", "enemy.casting", "enemy.cast.progress",
                     "enemy.rooted", "enemy.root.remaining", "enemy.stunned", "enemy.frozen", "enemy.slowed",
                     "enemy.targeting_me", *(f"enemy.cast.{k}" for k in schema.kit)):
            _put(values, schema, name, 0)
    return values


def context_problems(context: dict, manifest: dict) -> list[str]:
    """The worker's published context against the manifest: class, level and every talent point placed."""
    problems = []
    loadout = manifest["loadout"]
    if context.get("class_id") != loadout["class"]["id"]:
        problems.append(f"class {context.get('class_id')} is not the manifest's {loadout['class']['id']}")
    if context.get("level") != loadout["level"]:
        problems.append(f"level {context.get('level')} is not the manifest's {loadout['level']}")
    points = sum(t["rank"] for t in loadout["talents"])
    if sum(context.get("tree_points", [])) != points:
        problems.append(f"{sum(context.get('tree_points', []))} talent points, the manifest places {points}")
    return problems


def compare(expected: list[float], actual: list[float], schema: Schema, tolerance: float = 1e-4,
            skip: tuple[str, ...] = ()) -> list[str]:
    """Each field where actual differs from expected by more than tolerance, by name."""
    if len(expected) != len(actual):
        return [f"{len(actual)} values, expected {len(expected)}"]
    out = []
    for f, e, a in zip(schema.fields, expected, actual):
        if f.name in skip:
            continue
        if abs(e - a) > tolerance:
            out.append(f"{f.name}: {a} (expected {e})")
    return out
