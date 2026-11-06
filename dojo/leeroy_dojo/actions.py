"""The v1 joint action catalog (D20) and its masks (D23), the same as mod-dojo's src/Lib/DojoActionCatalog.cpp.

An action is (ability, movement, facing); entry 0 is the noop that keeps whatever command is pending. Abilities are
none, one cast per kit spell in manifest order, stop_cast, target_enemy, clear_target, attack_on and attack_off.
Movement is hold or one of eight directions relative to the facing; facing keeps, turns toward or turns away from the
visible enemy. Both implementations hash the same canonical document and compute the same masks from the shared cases
in modules/mod-dojo/tests/fixtures/action_cases.json.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field

from .scenario import canonical, sha256

SCHEMA = "leeroy-dojo-actions-v1"
MOVE_HORIZON_MS = 250
MOVEMENTS = ("hold", "forward", "forward_left", "left", "back_left", "back", "back_right", "right", "forward_right")
FACINGS = ("keep", "toward", "away")
# The fixed abilities after the kit's casts: (name, target).
FIXED = (("stop_cast", "none"), ("target_enemy", "enemy"), ("clear_target", "none"), ("attack_on", "enemy"),
         ("attack_off", "none"))


class ActionError(Exception):
    pass


@dataclass(frozen=True)
class Ability:
    kind: str                  # none, cast or the fixed ability's own name
    name: str
    kit: int = 0
    target: str = "none"
    needs_facing: bool = False


@dataclass(frozen=True)
class Entry:
    name: str
    noop: bool = False
    ability: int = 0
    movement: str = "hold"
    facing: str = "keep"


@dataclass
class Catalog:
    kit: list[dict]
    abilities: list[Ability]
    entries: list[Entry]
    document: dict
    sha256: str

    def index(self, name: str) -> int:
        for i, e in enumerate(self.entries):
            if e.name == name:
                return i
        raise ActionError(f"no action {name!r}")


def ability_name(spell_name: str) -> str:
    return "_".join(part.lower() for part in re.split(r"[^A-Za-z0-9]+", spell_name) if part)


def build_catalog(kit: list[dict]) -> Catalog:
    """kit: [{"spell_id", "name", "target": "self"|"enemy", "facing": bool}] in manifest order."""
    if not kit:
        raise ActionError("the kit is empty")
    names = {"none", "noop", *(n for n, _ in FIXED)}
    abilities = [Ability("none", "none")]
    for i, k in enumerate(kit):
        name = ability_name(k.get("name", ""))
        if not k.get("spell_id") or not name or k.get("target") not in ("self", "enemy"):
            raise ActionError(f"kit spell {i} needs an id, a name and a self or enemy target")
        if name in names:
            raise ActionError(f"kit spell {k['name']} repeats or reuses the ability name {name}")
        names.add(name)
        abilities.append(Ability("cast", name, i, k["target"], bool(k.get("facing"))))
    abilities += [Ability(n, n, 0, t, False) for n, t in FIXED]
    entries = [Entry("noop", noop=True)]
    for a, ability in enumerate(abilities):
        for m in MOVEMENTS:
            for f in FACINGS:
                entries.append(Entry(f"{ability.name}/{m}/{f}", False, a, m, f))
    kit_doc = [{"spell_id": int(k["spell_id"]), "name": k["name"], "target": k["target"], "facing": bool(k.get("facing"))}
               for k in kit]
    document = {"schema": SCHEMA, "move_horizon_ms": MOVE_HORIZON_MS, "kit": kit_doc,
                "abilities": [a.name for a in abilities], "movements": list(MOVEMENTS), "facings": list(FACINGS),
                "size": len(entries)}
    return Catalog(kit_doc, abilities, entries, document, sha256(document))


def canonical_text(catalog: Catalog) -> str:
    return canonical(catalog.document).decode("ascii")


@dataclass
class KitKnowledge:
    known: bool = False
    cooldown_ms: int = 0
    gcd_ms: int = 0
    cost_known: bool = False
    cost: int = 0
    range_known: bool = False
    max_range: float = 0.0


@dataclass
class Knowledge:
    alive: bool = False
    mana: int = 0
    mana_regen_bound: int = 0
    casting: bool = False
    cast_remaining_ms: int = 0
    cast_queued: bool = False
    kit: list[KitKnowledge] = field(default_factory=list)
    enemy_known: bool = False
    enemy_alive: bool = False
    enemy_distance: float = 0.0

    @classmethod
    def from_json(cls, doc: dict) -> "Knowledge":
        fields = dict(doc)
        fields["kit"] = [KitKnowledge(**k) for k in doc.get("kit", [])]
        return cls(**fields)


@dataclass
class Rules:
    perceptual_delay_ms: int = 0
    command_delay_ms: int = 150
    queue_window_ms: int = 400
    range_slack_yards: float = 3.0


@dataclass
class MaskResult:
    mask: list[int]
    ability_reasons: list[str]
    facing_reasons: list[str]


def compute_mask(catalog: Catalog, knowledge: Knowledge, rules: Rules = Rules()) -> MaskResult:
    """Only choices that cannot succeed are masked, and the noop never is (see DojoActionCatalog.h)."""
    wait = rules.perceptual_delay_ms + rules.command_delay_ms + rules.queue_window_ms
    reasons = []
    for ability in catalog.abilities:
        why = ""
        enemy = ability.target == "enemy"
        if not knowledge.alive:
            why = "dead"
        elif ability.kind == "stop_cast":
            if not knowledge.casting and not knowledge.cast_queued:
                why = "not casting"
        elif ability.kind == "target_enemy":
            if not knowledge.enemy_known:
                why = "no visible enemy"
        elif ability.kind == "attack_on":
            if not knowledge.enemy_known:
                why = "no visible enemy"
            elif not knowledge.enemy_alive:
                why = "enemy dead"
        elif ability.kind == "cast":
            k = knowledge.kit[ability.kit] if ability.kit < len(knowledge.kit) else KitKnowledge()
            if not k.known:
                why = "not known"
            elif enemy and not knowledge.enemy_known:
                why = "no visible enemy"
            elif enemy and not knowledge.enemy_alive:
                why = "enemy dead"
            elif enemy and k.range_known and _f32(knowledge.enemy_distance) > _f32(
                    _f32(k.max_range) + _f32(rules.range_slack_yards)):
                why = "out of range"
            elif k.cooldown_ms > wait:
                why = "cooldown"
            elif k.gcd_ms > wait:
                why = "global cooldown"
            elif knowledge.casting and knowledge.cast_remaining_ms > wait:
                why = "casting"
            elif k.cost_known and knowledge.mana + knowledge.mana_regen_bound < k.cost:
                why = "mana"
        reasons.append(why)
    facing_reasons = []
    for f in FACINGS:
        if not knowledge.alive:
            facing_reasons.append("dead")
        elif f != "keep" and not knowledge.enemy_known:
            facing_reasons.append("no visible enemy")
        else:
            facing_reasons.append("")
    mask = []
    for e in catalog.entries:
        if e.noop:
            mask.append(1)
            continue
        ability = catalog.abilities[e.ability]
        is_open = not reasons[e.ability] and not facing_reasons[FACINGS.index(e.facing)]
        if ability.kind == "cast" and ability.needs_facing and e.facing == "away":
            is_open = False
        mask.append(1 if is_open else 0)
    return MaskResult(mask, reasons, facing_reasons)


def _f32(value: float) -> float:
    """The C++ side compares single-precision yards; round through float32 so both agree at the boundary."""
    return struct.unpack("f", struct.pack("f", float(value)))[0]
