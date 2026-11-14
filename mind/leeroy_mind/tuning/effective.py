"""The effective persona: the stored one with the owner's dials folded in.

With no dial set for a member, the effective persona IS the stored persona object: same type,
same values, so every prompt, weight and nightly decision is what it was before tuning existed.
With one set, it is an EffectivePersona: the same fields (so every reader takes it as a
Persona), with

- score substitution: agreeableness (argumentative, the other way up) and habit odds (party);
  every reader of those fields follows, the night's trait sample included;
- `tuned`, the member's effective dial values, read by the multipliers at named points
  (dials_of) and by the reliability accessor;
- `base`, the stored persona it was made from.

An EffectivePersona is never saved (personas.io.save_persona refuses one), so an overlay can
never be written back into persona_core by accident. dataclasses.replace() on one gives an
EffectivePersona without its dials: never replace one, take base_of() first."""
from dataclasses import dataclass, fields, replace
from types import MappingProxyType
from typing import Mapping

from leeroy_mind.personas.schema import Persona, Trait

from .dials import (LEVEL_NEUTRAL, MEMBER_DIALS, PARTY_SUBSTANCES, SCORE_HI, WORDING_STEP,
                    base_value)


@dataclass(frozen=True)
class MemberTuning:
    """values: the member's effective dial values that differ from the persona as written.
    pinned: the dials the owner set for this member himself (they win over any group)."""
    values: Mapping[str, int] = MappingProxyType({})
    pinned: frozenset = frozenset()

    def value(self, dial: str, p: Persona) -> int:
        return self.values.get(dial, base_value(dial, p))

    def mult(self, dial: str) -> float:
        """A level dial as a multiplier: value / 5, exactly 1.0 when it is not set."""
        v = self.values.get(dial)
        return 1.0 if v is None else v / LEVEL_NEUTRAL

    def level(self, dial: str) -> int:
        return self.values.get(dial, LEVEL_NEUTRAL)

    def score(self, dial: str) -> int | None:
        return self.values.get(dial)


NEUTRAL = MemberTuning()


class EffectivePersona(Persona):
    """A Persona with dials folded in (module docstring). Not a dataclass of its own: it has
    exactly Persona's fields."""
    tuned = NEUTRAL
    base = None
    nudges = ()
    edits = None  # tuning.edits.MemberEdits: the owner's godmode overlays in force


def is_effective(p) -> bool:
    return isinstance(p, EffectivePersona)


def dials_of(p) -> MemberTuning:
    """The member's dials; NEUTRAL for a stored persona or None."""
    return p.tuned if isinstance(p, EffectivePersona) else NEUTRAL


def base_of(p):
    """The stored persona behind an effective one (itself for a stored persona)."""
    if isinstance(p, EffectivePersona) and p.base is not None:
        return p.base
    return p


def with_nudges(p, nudges):
    """Attach immutable private overlays to a fresh wrapper, never the stored persona."""
    if p is None or not nudges:
        return p
    out = EffectivePersona(**{f.name: getattr(p, f.name) for f in fields(Persona)})
    object.__setattr__(out, "tuned", dials_of(p))
    object.__setattr__(out, "base", base_of(p))
    object.__setattr__(out, "nudges", tuple(nudges))
    object.__setattr__(out, "edits", getattr(p, "edits", None))
    return out


def edits_of(p):
    """The member's godmode overlays (tuning.edits.MemberEdits); NO_EDITS for a stored persona,
    None or a member with none in force."""
    from .edits import NO_EDITS
    e = getattr(p, "edits", None) if isinstance(p, EffectivePersona) else None
    return NO_EDITS if e is None else e


def with_edits(p, edits):
    """`p` with the owner's godmode overlays (MemberEdits) folded in: trait edits replace the
    persona's values at their whitelisted paths (a trait edit wins over a dial on the same
    score), and the overlays ride along for their readers (edits_of). `p` itself when there are
    none. The stored persona stays `base`, so a writer never saves an overlay."""
    if p is None or edits is None or edits.empty:
        return p
    q = p
    if edits.traits:
        from leeroy_mind.personas.schema import persona_from_dict, persona_to_dict
        data = persona_to_dict(p)
        for path, value in edits.traits.items():
            node, keys = data, path.split(".")
            for k in keys[:-1]:
                node = node[k]
            node[keys[-1]] = value
        try:
            q = persona_from_dict(data)
        except ValueError:
            q = p  # a stored persona that no longer takes the edit: the persona as it is
    out = EffectivePersona(**{f.name: getattr(q, f.name) for f in fields(Persona)})
    object.__setattr__(out, "tuned", dials_of(p))
    object.__setattr__(out, "base", base_of(p))
    object.__setattr__(out, "nudges", tuple(getattr(p, "nudges", ())))
    object.__setattr__(out, "edits", edits)
    return out


def band(dial: str, p: Persona, mt: MemberTuning) -> str | None:
    """"high" or "low" when a wording dial sits WORDING_STEP or more from the persona as
    written (5, or its own score), else None."""
    v = mt.values.get(dial)
    if v is None or not MEMBER_DIALS[dial].wording:
        return None
    d = v - base_value(dial, p)
    if d >= WORDING_STEP:
        return "high"
    if d <= -WORDING_STEP:
        return "low"
    return None


def effective(p: Persona | None, mt: MemberTuning):
    """The persona `p` with `mt` folded in; `p` itself when nothing is set (or p is None)."""
    if p is None or not mt.values:
        return p
    base = base_of(p)
    q = base
    agree = mt.values.get("argumentative")
    if agree is not None:
        a = base.big_five.agreeableness
        # a description written for the persona's own score would contradict a far move: the
        # tuning line says it instead (tuning.lines)
        text = "" if band("argumentative", base, mt) else a.behaviour
        q = replace(q, big_five=replace(q.big_five, agreeableness=Trait(
            score=SCORE_HI + 1 - agree, behaviour=text)))
    party = mt.values.get("party")
    if party is not None:
        m = party / LEVEL_NEUTRAL
        q = replace(q, habits=tuple(
            replace(h, odds=min(1.0, h.odds * m)) if h.substance in PARTY_SUBSTANCES else h
            for h in q.habits))
    out = EffectivePersona(**{f.name: getattr(q, f.name) for f in fields(Persona)})
    object.__setattr__(out, "tuned", mt)
    object.__setattr__(out, "base", base)
    return out
