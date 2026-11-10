"""Deterministic persona-to-supported-playerbots mapping, with no model or prompt changes."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Style:
    wait: int
    potions: bool

    @property
    def param(self):
        return f'{self.wait}:{int(self.potions)}'


def style(persona) -> Style:
    """Cautious/reliable damage members wait longer; healers/tanks never gain a delay.

    Base 2s, +1 for neuroticism>=7, +1 for conscientiousness>=7, plus guid-seeded
    0..2s, capped at6. Conscientiousness>=6 or 'clutch' skill adds potion use;
    the other bucket keeps stock use (never disables it). Native rechecks actual
    role, preserves longer active holds and defers entirely to instance/raid owners.
    """
    jitter = ((persona.guid * 1103515245 + 12345) & 0xFFFFFFFF) % 3
    careful = int(persona.big_five.neuroticism.score >= 7)
    reliable = int(persona.big_five.conscientiousness.score >= 7)
    damage = persona.play.raid_role in ('melee','ranged')
    wait = min(6, 2 + careful + reliable + jitter) if damage else 0
    potions = persona.big_five.conscientiousness.score >= 6 or persona.play.skill == 'clutch'
    return Style(wait, potions)
