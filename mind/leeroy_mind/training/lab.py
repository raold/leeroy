"""Pure, conservative rotation proposals and paired trial arithmetic.

The base-cast heuristic is not measured DPS. It omits talents, gear, resource
limits, cooldown use, periodic effects and server corrections. Callers must bind
the checked vocabulary to the requested spec and choose comparable completed
trial pairs. In particular requested_pre_ai throughput is only a requested
throughput proxy, never verified health loss or effective damage.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from hashlib import sha256
from math import fsum, isfinite
import re

from .spells import SpellBook, SpellInfo
from .vocab import ClassVocab, GENERIC_TRIGGERS


# guild.roles' known damage tabs, excluding feral: this API has no raid-role
# receipt with which to distinguish a cat from a bear. Unknown tabs fail closed.
_DAMAGE_SPECS = frozenset({
    (1, 0), (1, 1), (2, 2), (3, 0), (3, 1), (3, 2),
    (4, 0), (4, 1), (4, 2), (5, 2), (6, 1), (6, 2),
    (7, 0), (7, 1), (8, 0), (8, 1), (8, 2),
    (9, 0), (9, 1), (9, 2), (11, 0),
})

# Verified in installed mod-playerbots src/Ai/Class: WarriorTriggers.h and
# ArmsWarriorStrategy.cpp; MageTriggers.h and Arcane/Fire/FrostMageStrategy.cpp.
# Only exact proc-aura names are included. BrainFreezeTrigger actually checks
# "fireball!", and Eclipse names have parentheses forbidden by line grammar.
# Unknown/new proc mappings remain unavailable until reviewed against the core.
_PROC_AURAS = {
    (1, 0): frozenset({"sudden death"}),
    (8, 0): frozenset({"missile barrage"}),
    (8, 1): frozenset({"hot streak"}),
    (8, 2): frozenset({"fingers of frost"}),
}
_NAME = re.compile(r"[a-z0-9 ]{1,40}")
_LINE = re.compile(r"([a-z0-9 ]{1,40})>([a-z0-9 ]{1,40})!([6789])")
_WEAPON_EFFECTS = frozenset({17, 31, 58})
_MAX_LINES = 12
_MAX_CANDIDATES = 12


def value_per_cast_second(info: SpellInfo, coeff: tuple[int, int], *, power: int = 2000) -> float:
    """Base school-damage value per cast second, with coefficients in hundredths.

    Apply the direct coefficient once per cast, after summing damage slots. Raw
    DBC base/die fields are used as the stated heuristic, not effective values.
    Effect 31 is weapon *percent* damage, not a flat amount (SharedDefines.h).
    Weapon-dependent spells cannot be valued without weapon context and return
    zero, as do spells with no school-damage effect. A negative direct bonus or
    cast time is unknown evidence and raises rather than becoming a penalty or
    an instant cast. Callers also need row availability proof from SpellBook.
    """
    if (not isinstance(coeff, tuple) or len(coeff) != 2
            or any(type(c) is not int for c in coeff) or coeff[0] < 0
            or type(power) is not int or power < 0
            or type(info.cast_ms) is not int or info.cast_ms < 0
            or type(info.gcd_ms) is not int or info.gcd_ms < 0):
        raise ValueError("base-cast value requires known nonnegative timing and direct coefficient")
    if any(e.kind in _WEAPON_EFFECTS for e in info.effects):
        return 0.0
    damage = [e for e in info.effects if e.kind == 2]
    if not damage:
        return 0.0
    # Keep centiunit arithmetic exact until the final division.
    numerator = sum(100 * e.base + 50 * e.die for e in damage) + coeff[0] * power
    try:
        value = numerator * 10 / max(info.gcd_ms, info.cast_ms, 1000)
    except OverflowError:
        raise ValueError("base-cast value exceeds finite arithmetic") from None
    if not isfinite(value):
        raise ValueError("base-cast value exceeds finite arithmetic")
    return value


@dataclass(frozen=True)
class Candidate:
    name: str
    cls: int
    spec_tab: int
    lines: tuple[str, ...]
    parent: str | None
    why: str


def _rotation_name(cls: int, spec_tab: int, lines: tuple[str, ...]) -> str:
    # The server's cache is immutable by name. Ordinal names would reuse an old
    # name for a changed definition on the next call. Hash exact ordered lines;
    # the server still rejects any name collision with different definitions.
    digest = sha256("\n".join(lines).encode("ascii")).hexdigest()[:16]
    return f"tg_{cls}_{spec_tab}_{digest}"


def _checked_lines(lines: tuple[str, ...], vocab: ClassVocab) -> bool:
    if len(lines) > _MAX_LINES:
        return False
    seen = set()
    for line in lines:
        match = _LINE.fullmatch(line) if isinstance(line, str) else None
        if match is None:
            return False
        trigger, action, _ = match.groups()
        key = (trigger, action)
        if (key in seen or trigger not in vocab.triggers
                or action not in vocab.actions or action not in vocab.spells):
            return False
        seen.add(key)
    return True


def propose(cls: int, spec_tab: int, book: SpellBook, vocab: ClassVocab, *,
            champion: tuple[str, ...] = (), k: int = 12) -> list[Candidate]:
    """Return up to 12 deterministic proposals over six eligible damage spells.

    ``vocab`` must be a checked probe that the caller authoritatively binds to
    ``spec_tab``; ClassVocab alone has no wire spec field. Missing class identity,
    spell checks, individual timing/bonus rows or current action/trigger names
    exclude proposals. The top six are the highest eligible base-cast heuristics
    among class spells actually known by that probe, not a spec inferred by DBC.

    A supplied champion must pass the same admission rules in full. Each edit
    replaces an existing trigger/action pair or appends a new one, preserving
    other champion lines. Invalid or overlong champions produce no candidates.
    Parent is the content-addressed champion name, or None for stock; persistence
    remains the caller's responsibility. No command or trial is sent here.
    """
    if type(k) is not int or k < 0:
        raise ValueError("candidate count must be a nonnegative integer")
    if (type(cls) is not int or type(spec_tab) is not int
            or (cls, spec_tab) not in _DAMAGE_SPECS or not k
            or type(vocab.class_) is not int or vocab.class_ != cls or vocab.spells_checked is not True
            or not vocab.actions or not vocab.spells or not vocab.strategies):
        return []
    if not isinstance(champion, tuple) or not _checked_lines(champion, vocab):
        return []

    ranked = {}
    for spell in book.class_spells(cls):
        name = spell.name.lower()
        if (not _NAME.fullmatch(name) or name not in vocab.actions or name not in vocab.spells
                or not book.cast_time_known(spell.id) or not book.coefficient_known(spell.id)):
            continue
        try:
            value = value_per_cast_second(spell, book.coefficient(spell.id))
        except ValueError:
            continue
        if value > 0 and isfinite(value):
            # Book normally supplies one rank/name; keep duplicate test/custom
            # providers deterministic too, without counting one action twice.
            ranked[name] = max(value, ranked.get(name, 0.0))
    top = sorted(ranked, key=lambda name: (-ranked[name], name))[:6]
    if not top:
        return []

    parent = _rotation_name(cls, spec_tab, champion) if champion else None
    proposals = {}

    def add(edits, why):
        lines = list(champion)
        before = {(line.split(">")[0], line.split(">")[1].split("!")[0]): int(line[-1])
                  for line in champion}
        for trigger, action, relevance in edits:
            prefix = f"{trigger}>{action}!"
            replacement = prefix + str(relevance)
            index = next((i for i, line in enumerate(lines) if line.startswith(prefix)), len(lines))
            if index == len(lines):
                lines.append(replacement)
            else:
                lines[index] = replacement
        frozen = tuple(lines)
        if frozen == champion or not _checked_lines(frozen, vocab) or frozen in proposals:
            return
        raised = {action for trigger, action, relevance in edits
                  if relevance > before.get((trigger, action), 5)}
        priority = fsum(ranked[action] for action in raised)
        candidate = Candidate(_rotation_name(cls, spec_tab, frozen), cls, spec_tab, frozen, parent,
                              why + "; base-cast heuristic only, requires paired trial evidence")
        proposals[frozen] = (priority, candidate)

    # Generate paired orders first so equal summed values retain a stable and
    # useful raise-then-swap ordering. No default trigger is assumed available.
    if "often" in GENERIC_TRIGGERS and "often" in vocab.triggers:
        if len(top) >= 2:
            add((("often", top[0], 8), ("often", top[1], 7)), "raise the top two spells")
            add((("often", top[1], 8), ("often", top[0], 7)), "swap the top two spells")
        for name in top:
            for relevance in (6, 7, 8):
                add((("often", name, relevance),), f"insert {name} at {relevance}")
    for trigger in sorted(_PROC_AURAS.get((cls, spec_tab), frozenset()) & vocab.triggers):
        for name in top:
            add(((trigger, name, 9),), f"try {name} on the verified {trigger} proc trigger")
    ordered = sorted(proposals.values(), key=lambda item: -item[0])
    return [candidate for _, candidate in ordered[:min(k, _MAX_CANDIDATES)]]


def halving_rounds(n: int) -> list[tuple[int, int]]:
    """Up to three rounds while more than one candidate remains."""
    if type(n) is not int or n < 0:
        raise ValueError("candidate count must be a nonnegative integer")
    rounds = []
    kept = n
    for seconds in (60, 120, 180):
        if kept <= 1:
            break
        rounds.append((kept, seconds))
        kept = (kept + 1) // 2
    return rounds


def paired_score(cand_dps: Sequence[int], stock_dps: Sequence[int]) -> float:
    """Mean paired relative improvement in caller-supplied throughput values.

    Legacy argument names do not establish actual DPS. The caller must supply
    same-bot adjacent completed runs with the same explicit measurement basis,
    e.g. requested throughput proxy. Numbers alone cannot prove identity, timing,
    completion or coverage. Missing/nonpositive pairs are unscoreable, not zero.
    """
    if (not cand_dps or len(cand_dps) != len(stock_dps)
            or any(type(value) is not int or value <= 0 for value in (*cand_dps, *stock_dps))):
        raise ValueError("paired score requires aligned nonempty positive integer pairs")
    try:
        score = fsum((candidate / stock - 1) / len(cand_dps)
                     for candidate, stock in zip(cand_dps, stock_dps))
    except OverflowError:
        raise ValueError("paired score exceeds finite arithmetic") from None
    if not isfinite(score):
        raise ValueError("paired score exceeds finite arithmetic")
    return score
