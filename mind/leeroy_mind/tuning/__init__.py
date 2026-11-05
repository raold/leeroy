"""Persona tuning (console phase 3b): the owner's dials at world, group and member level, kept in
mind.db's tuning_* tables as an overlay over the stored personas, never written into them.

- dials: the registry (what each dial is, its range, who it applies to);
- groups: saved predicates over the persona and the cast, "everyone" built in;
- state: the values in force (Tuning) and the precedence that folds them per member;
- effective: the effective persona (score substitution, the member's dial values);
- lines: the tuning lines a wording dial adds to the speaker's rules block;
- changes: the append-only change log, undo and revert;
- world: world dials merged over mind.toml.

The first invariant: with no tuning rows, every prompt, weight and nightly decision is what it
was before tuning existed (tests/tuning/test_tuning_invariant.py)."""
from .dials import LEVEL_NEUTRAL, MEMBER_DIALS, WORLD_DIALS
from .effective import NEUTRAL, EffectivePersona, MemberTuning, base_of, dials_of, is_effective
from .state import EMPTY, Tuning

__all__ = ["EMPTY", "LEVEL_NEUTRAL", "MEMBER_DIALS", "NEUTRAL", "WORLD_DIALS", "EffectivePersona",
           "MemberTuning", "Tuning", "base_of", "dials_of", "is_effective"]
