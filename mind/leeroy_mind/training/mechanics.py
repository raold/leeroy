"""Encounter checks for training/checks.py, read from the raid playbooks (leeroy_mind.raidplay).

The playbooks hold every check as data. Version 1 is the original transcription for ICC and
Trial of the Crusader and is what the training ground scores by default; version 2 adds the
playbooks' further checks for every covered raid and is scored only with [training] playbooks.
"""

import functools
from dataclasses import dataclass
from typing import Literal

from leeroy_mind import raidplay


@dataclass(frozen=True)
class Mechanic:
    kind: Literal["avoid", "debuff", "buff", "adds", "spread"]
    names: tuple[str, ...] = ()
    entries: tuple[int, ...] = ()
    roles: tuple[str, ...] = ()
    max_hits: int = 0
    max_count: int = 0
    max_stack: int = 0
    min_stack: int = 0
    min_pull_s: int = 0
    max_share: float = 0.0
    scope: Literal["member", "raid"] = "member"
    exempt: tuple[str, ...] = ()
    exempt_s: int = 0
    tag: str = ""


def _version(playbooks: bool) -> int:
    return 2 if playbooks else 1


@functools.cache
def boss_mechanics(playbooks: bool = False) -> dict[tuple[int, int], tuple[Mechanic, ...]]:
    """Each covered boss's checks in playbook order; bosses with none are left out."""
    out = {}
    for boss in raidplay.load().bosses():
        rows = tuple(Mechanic(c.kind, c.names, c.entries, c.roles, c.max_hits, c.max_count,
                              c.max_stack, c.min_stack, c.min_pull_s, c.max_share, c.scope,
                              c.exempt, c.exempt_s, c.tag)
                     for c in boss.checks if c.since <= _version(playbooks))
        if rows:
            out[boss.key] = rows
    return out


@functools.cache
def no_behind(playbooks: bool = False) -> frozenset[tuple[int, int]]:
    """Bosses whose melee are not scored for standing behind the boss."""
    return frozenset(b.key for b in raidplay.load().bosses()
                     if b.no_behind_since is not None and b.no_behind_since <= _version(playbooks))


def watch_names(playbooks: bool = False) -> tuple[str, ...]:
    return tuple(sorted({name for rows in boss_mechanics(playbooks).values() for row in rows
                         if row.kind == "buff" for name in row.names}))


BOSS_MECHANICS = boss_mechanics()
NO_BEHIND = no_behind()
