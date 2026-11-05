"""An arena episode's outcome and rewards (D17, D24), the same rules as mod-dojo's src/Lib/DojoOutcome.cpp.

Both run the shared cases in modules/mod-dojo/tests/fixtures/outcome_cases.json. A team is alive while any member
is. No team alive: simultaneous death, no winner; exactly one team alive: death, that team wins; otherwise the match
limit, when reached, is a draw, and a death at the limit's tick still decides. The terminal reward is +1 to the
winning team, -1 to every other and 0 on a draw. Shaping is scale * (Phi(next) - Phi(current)), Phi being team 0's
mean remaining health fraction minus team 1's (the dead counted as 0), with Phi = 0 at every terminal state and the
opposite sign for team 1.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

SHAPING_SCALE = 0.05
REASONS = ("", "death", "simultaneous_death", "time_limit")


@dataclass(frozen=True)
class Standing:
    team: int
    alive: bool = True
    health: float = 1.0


@dataclass(frozen=True)
class Outcome:
    reason: str = ""
    winner: int | None = None

    @property
    def over(self) -> bool:
        return self.reason != ""


def _round6(value: float) -> float:
    r = round(value * 1e6) / 1e6
    return 0.0 if r == 0 else r


def _mean_health(actors: list[Standing], team: int) -> float:
    members = [a for a in actors if a.team == team]
    if not members:
        return 0.0
    def health(a: Standing) -> float:
        if not a.alive or not math.isfinite(a.health):
            return 0.0
        return min(max(a.health, 0.0), 1.0)
    return sum(health(a) for a in members) / len(members)


def decide(actors: list[Standing], time_up: bool) -> Outcome:
    teams = {a.team for a in actors}
    alive = {a.team for a in actors if a.alive}
    if not teams:
        return Outcome()
    if not alive:
        return Outcome("simultaneous_death")
    if len(alive) == 1 and len(teams) > 1:
        return Outcome("death", next(iter(alive)))
    return Outcome("time_limit") if time_up else Outcome()


def potential(actors: list[Standing]) -> float:
    return _round6(_mean_health(actors, 0) - _mean_health(actors, 1))


def rewards(actors: list[Standing], before: float, outcome: Outcome,
            scale: float = SHAPING_SCALE) -> list[dict[str, float]]:
    after = 0.0 if outcome.over else potential(actors)
    delta = after - before
    out = []
    for a in actors:
        sign = 1.0 if a.team == 0 else -1.0
        terminal = 0.0
        if outcome.winner is not None:
            terminal = 1.0 if a.team == outcome.winner else -1.0
        out.append({"terminal": terminal, "shaping": _round6(scale * sign * delta)})
    return out
