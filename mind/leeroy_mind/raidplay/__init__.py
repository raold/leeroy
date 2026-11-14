"""Raid playbooks: one entry per boss mechanic, as data, for the raid lead's callouts, a
native responder and the training ground's per-mechanic scoring. See README.md."""

from .model import (CHECK_KINDS, DIFFICULTIES, DIFFICULTY_KEYS, GENERIC_CHECKS, HANDLERS, KINDS,
                    MOVEMENT_KINDS, ROLES, Boss, BossPlaybook, Check, Mechanic, Phase, PlaybookError,
                    Playbooks, Raid, load, load_files)


def playbook_for(map_id: int, boss_index: int, difficulty: int) -> BossPlaybook | None:
    """The repo playbook for a training context (MAP:BOSS:DIFFICULTY), or None."""
    return load().playbook_for(map_id, boss_index, difficulty)


def mechanic(mechanic_id: str) -> Mechanic | None:
    return load().mechanic(mechanic_id)


def mechanics_for_spell(spell_id: int) -> tuple[Mechanic, ...]:
    """Every repo mechanic that casts or lands this spell id, at any difficulty."""
    return load().mechanics_for_spell(spell_id)


__all__ = ["CHECK_KINDS", "DIFFICULTIES", "DIFFICULTY_KEYS", "GENERIC_CHECKS", "HANDLERS", "KINDS",
           "MOVEMENT_KINDS", "ROLES", "Boss", "BossPlaybook", "Check", "Mechanic", "Phase", "PlaybookError",
           "Playbooks", "Raid", "load", "load_files", "mechanic", "mechanics_for_spell",
           "playbook_for"]
