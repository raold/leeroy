"""Bounded tactics, checked against the current server vocabulary."""

import json
import math
import re
from dataclasses import dataclass, field, fields
from types import MappingProxyType

from leeroy_mind.guild.boss_calls import BOSS_CALLS, call_for
from leeroy_mind.personas.screens import has_word, names_real_name
from leeroy_mind.storage.db import VEC_DIM
from leeroy_mind.storage.rows import cosine, unpack_vec
from leeroy_mind.wow import BOSS_NAMES


CLASSES = (1, 2, 3, 4, 5, 6, 7, 8, 9, 11)


@dataclass(frozen=True, slots=True)
class Targets:
    roles: tuple[str, ...] = ()
    classes: tuple[int, ...] = ()
    specs: tuple[tuple[int, int], ...] = ()


@dataclass(frozen=True, slots=True)
class Directive:
    lever: str
    value: str | int
    targets: Targets = Targets()


@dataclass(frozen=True, slots=True)
class Card:
    id: int | None
    context: str
    version: int
    status: str
    origin: str
    directives: tuple[Directive, ...]
    call: str = ""
    assignment: dict[str, str] = field(default_factory=dict)
    reason: str = ""
    evidence: tuple = ()
    vetoes: tuple = ()
    prev_champion: int | None = None
    lineage: int | None = None
    embedding: bytes | None = None
    created: float = 0.0
    changed: float = 0.0

    def __post_init__(self):
        object.__setattr__(self, "assignment", MappingProxyType(dict(self.assignment)))
        for name in ("directives", "evidence", "vetoes"):
            object.__setattr__(self, name, tuple(getattr(self, name)))

    def to_row(self) -> dict:
        row = {f.name: getattr(self, f.name) for f in fields(self)}
        row["assignment"] = dict(self.assignment)
        row["directives"] = [dict(lever=d.lever, value=d.value, targets=dict(
            roles=list(d.targets.roles), classes=list(d.targets.classes),
            specs=[list(s) for s in d.targets.specs])) for d in self.directives]
        return row

    @classmethod
    def from_row(cls, row):
        values = dict(row)
        for name in ("directives", "assignment", "evidence", "vetoes"):
            if isinstance(values.get(name), str):
                values[name] = json.loads(values[name])
        directives = []
        for raw in values["directives"]:
            target = raw.get("targets", {})
            directives.append(Directive(raw["lever"], raw["value"], Targets(
                tuple(target.get("roles", ())), tuple(target.get("classes", ())),
                tuple(tuple(s) for s in target.get("specs", ())))))
        values["directives"] = tuple(directives)
        return cls(**values)


@dataclass(frozen=True, slots=True)
class Lever:
    kind: str
    values: tuple[str | int, ...]
    roles: tuple[str, ...]
    pooled: bool


LEVERS: dict[str, Lever] = {
    "threat": Lever("strategy", ("+",), ("melee", "ranged"), True),
    "dps aoe": Lever("strategy", ("+",), ("melee", "ranged"), True),
    "save mana": Lever("strategy", ("-",), ("healer",), True),
    "formation": Lever("value", tuple("arrow circle chaos line near far melee queue shield".split()),
                       ("ranged", "healer"), False),
    "mana save level": Lever("value", (1, 2), ("healer",), True),
    "wait for attack time": Lever("value", tuple(range(11)), ("melee", "ranged"), True),
    "focus tanks": Lever("value", ("on",), ("healer",), True),
    "lust_at": Lever("call", (0, *range(20, 101, 5)), (), False),
    "rotation": Lever("rotation", (), (), True),
}
for _key in ("PartyMaxSpread", "PullCombatSpread", "CombatRegroupDistance",
             "BossEngageRangeFloor", "BossEngageRangeCap", "HealRepositionMaxRange",
             "HealRepositionHpFloor", "PullPlayerReleaseDelay", "PullPetReleaseDelay",
             "PullThreatLeadPanicHp", "PullSafetyHpPct", "SmartRestHealthPct",
             "SmartRestDpsManaPct", "SmartRestHealerManaPct", "RaidMusterRestTimeoutSecs",
             "RaidMusterRebuffTimeoutSecs", "RaidMusterTotalTimeoutSecs"):
    LEVERS[_key] = Lever("dc", tuple(range(50, 151)), (), _key not in {
        "PartyMaxSpread", "PullCombatSpread", "CombatRegroupDistance",
        "BossEngageRangeFloor", "BossEngageRangeCap"})


def stock_card(context: str) -> Card:
    map_id, boss_id, _ = (int(part) for part in context.split(":"))
    boss = BOSS_CALLS.get((map_id, boss_id))
    if boss is None:
        _, boss = call_for(map_id, BOSS_NAMES.get((map_id, boss_id), "boss"))
    return Card(None, context, 1, "candidate", "stock", (), call=boss.pre)


def _targets_ok(targets, lever):
    if not isinstance(targets, Targets):
        return False
    roles, classes, specs = targets.roles, targets.classes, targets.specs
    if not all(type(v) is tuple for v in (roles, classes, specs)):
        return False
    if (len(roles) > 2 or len(classes) > 10 or len(specs) > 30
            or any(type(r) is not str or r not in lever.roles for r in roles)
            or any(type(c) is not int or c not in CLASSES for c in classes)
            or any(type(s) is not tuple or len(s) != 2
                   or type(s[0]) is not int or s[0] not in CLASSES
                   or type(s[1]) is not int or s[1] not in (0, 1, 2) for s in specs)):
        return False
    if any(len(set(v)) != len(v) for v in (roles, classes, specs)):
        return False
    if classes and any(c not in classes for c, _ in specs):
        return False
    if lever.kind in {"dc", "call"} and (roles or classes or specs):
        return False
    return lever.kind != "rotation" or len(specs) == 1


def _target_classes(d, lever):
    if d.targets.specs:
        return {c for c, _ in d.targets.specs}
    if d.targets.classes:
        return set(d.targets.classes)
    # Class capabilities only narrow a role selector; the applier still checks
    # each actual member's role (including observed tanks).
    by_role = {"melee": {1, 2, 4, 6, 7, 11}, "ranged": {3, 5, 7, 8, 9, 11},
               "healer": {2, 5, 7, 11}}
    return set().union(*(by_role[r] for r in (d.targets.roles or lever.roles)))


def validate_directive(d: Directive, vocab) -> str | None:
    if not isinstance(d, Directive) or type(d.lever) is not str:
        return "unknown_lever"
    # Defense in depth if the lever table grows later. Stock survival cannot
    # become a removable toggle, even through a future whitelist expansion.
    if d.value == "-" and d.lever in {"avoid aoe", "tank", "tank aoe", "heal", "heal aoe"}:
        return "removes_survival"
    lever = LEVERS.get(d.lever)
    if lever is None:
        return "unknown_lever"
    if lever.kind == "rotation":
        if type(d.value) is not str or re.fullmatch(r"tg_[a-z0-9_]{1,40}", d.value) is None:
            return "bad_value"
    elif type(d.value) not in (str, int) or not any(
            type(d.value) is type(v) and d.value == v for v in lever.values):
        return "bad_value"
    if not _targets_ok(d.targets, lever):
        return "bad_targets"
    if not vocab.complete:
        return "not_in_vocab"
    if lever.kind == "dc":
        return None if d.lever in vocab.dc else "not_in_vocab"
    if lever.kind == "rotation":
        spec = vocab.rotations.get(d.value)
        return None if spec == d.targets.specs[0] else "not_in_vocab"
    required = (d.lever if lever.kind == "strategy" else {
        "wait for attack time": "wait for attack", "focus tanks": "focus heal targets"
    }.get(d.lever))
    if required:
        if required not in vocab.whitelist:
            return "not_in_vocab"
        if any(c not in vocab.classes or required not in vocab.classes[c].strategies
               for c in _target_classes(d, lever)):
            return "not_in_vocab"
    return None


def validate_card(card: Card, vocab, *, names: set[str], human) -> list[str]:
    reasons = []
    if card.status == "candidate" and len(card.directives) > 1:
        reasons.append("too_many_directives")
    for directive in card.directives:
        reason = validate_directive(directive, vocab)
        if reason and reason not in reasons:
            reasons.append(reason)
    if len(card.call) > 60:
        reasons.append("call_too_long")
    if (any(has_word(card.call, name) for name in (*names, human.called, human.character_name))
            or names_real_name(card.call, human)):
        reasons.append("call_names_someone")
    return reasons


def describe(card: Card) -> str:
    parts = []
    for d in card.directives:
        targets = (*d.targets.roles, *(f"class {c}" for c in d.targets.classes),
                   *(f"spec {c}:{s}" for c, s in d.targets.specs))
        parts.append(f"{d.lever} {d.value}" + (f" ({', '.join(targets)})" if targets else ""))
    return "; ".join((*parts, card.call) if card.call else parts) or "stock"


def _vector(blob):
    if not isinstance(blob, bytes) or len(blob) != VEC_DIM * 4:
        raise ValueError("invalid card embedding")
    result = unpack_vec(blob)
    if not all(math.isfinite(x) for x in result) or not any(result):
        raise ValueError("invalid card embedding")
    return result


def nearest(conn, vec: bytes, k=3, *, exclude=None) -> list[Card]:
    if type(k) is not int or not 0 <= k <= 100:
        raise ValueError("invalid nearest-card count")
    query = _vector(vec)
    matches = []
    for row in conn.execute("SELECT * FROM tg_card WHERE embedding IS NOT NULL "
                            "AND status IN ('candidate','trial','champion') "
                            "AND (? IS NULL OR context != ?)", (exclude, exclude)):
        try:
            similarity = cosine(query, _vector(row["embedding"]))
        except ValueError:
            continue
        matches.append((-similarity, row["id"], row))
    matches.sort(key=lambda entry: entry[:2])
    return [Card.from_row(row) for _, _, row in matches[:k]]
