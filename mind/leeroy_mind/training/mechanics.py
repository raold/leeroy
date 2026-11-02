"""Encounter facts, independently transcribed as data rather than script logic.

Creature difficulty variants are the first four columns of the core's base
creature_template.sql. Zero difficulty entries are intentionally omitted.
"""

from dataclasses import dataclass
from typing import Literal


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


BOSS_MECHANICS: dict[tuple[int, int], tuple[Mechanic, ...]] = {
    (631, 0): (
        Mechanic("avoid", ("Coldflame",), tag="coldflame"),
        Mechanic("avoid", ("Bone Storm",), roles=("ranged", "healer"), max_hits=6, tag="spread"),
        Mechanic("debuff", ("Impaled",), max_count=2)),
    (631, 1): (
        Mechanic("avoid", ("Death and Decay",), max_hits=1),
        Mechanic("adds", entries=(37890, 38393, 38628, 38629, 37949, 38394, 38625, 38626, 38222), max_share=.25),
        Mechanic("debuff", ("Dominate Mind",), max_count=1)),
    (631, 3): (
        Mechanic("debuff", ("Mark of the Fallen Champion",), max_count=3, scope="raid"),
        Mechanic("adds", entries=(38508, 38596, 38597, 38598), max_share=.15),
        Mechanic("spread", roles=("ranged",))),
    (631, 4): (
        Mechanic("buff", ("Inoculated",), min_stack=3, min_pull_s=150),
        Mechanic("avoid", ("Vile Gas",), roles=("ranged",), max_hits=1),
        Mechanic("spread", roles=("ranged",), exempt=("Gas Spore",), exempt_s=12)),
    (631, 5): (
        Mechanic("avoid", ("Slime Spray", "Unstable Ooze Explosion", "Sticky Ooze"), max_hits=1),),
    (631, 6): (
        Mechanic("avoid", ("Malleable Goo", "Slime Puddle", "Choking Gas Bomb"), max_hits=1),
        Mechanic("adds", entries=(37562, 38602, 38760, 38761, 37697, 38604, 38758, 38759), max_share=.2)),
    (631, 7): (
        Mechanic("avoid", ("Shock Vortex", "Empowered Shock Vortex"), max_hits=1),
        Mechanic("adds", entries=(38454, 38775, 38776, 38777), max_share=.1),
        Mechanic("spread", roles=("ranged",))),
    (631, 8): (
        Mechanic("avoid", ("Swarming Shadows",), max_hits=1),
        Mechanic("avoid", ("Bloodbolt Whirl",), max_hits=2),
        Mechanic("spread", roles=("ranged",), exempt=("Pact of the Darkfallen",), exempt_s=15)),
    (631, 11): (
        Mechanic("debuff", ("Instability",), max_stack=5),
        Mechanic("debuff", ("Unchained Magic",), max_count=1),
        Mechanic("avoid", ("Frost Bomb", "Blistering Cold")),
        Mechanic("spread", roles=("ranged",))),
    (631, 12): (
        Mechanic("avoid", ("Defile", "Shadow Trap")),
        Mechanic("debuff", ("Necrotic Plague",), max_count=1),
        Mechanic("adds", entries=(36609, 39120, 39121, 39122, 36701, 39302, 39303, 39304,
                                  37799, 39284, 39285, 39286), max_share=.2)),
    (649, 0): (
        Mechanic("avoid", ("Slime Pool", "Fire Bomb", "Trample"), max_hits=1),
        Mechanic("debuff", ("Paralytic Toxin", "Burning Bile"), max_count=1)),
    (649, 1): (
        Mechanic("avoid", ("Legion Flame", "Infernal Eruption"), max_hits=1),
        Mechanic("adds", entries=(34815, 35262, 35263, 35264, 34826, 35270, 35271, 35272), max_share=.2)),
    (649, 3): (Mechanic("avoid", ("Unleashed Light", "Unleashed Dark"), max_hits=2),),
    (649, 4): (
        Mechanic("debuff", ("Penetrating Cold",), max_count=1),
        Mechanic("avoid", ("Impale",))),
}

NO_BEHIND = frozenset({(631, 2), (631, 10), (649, 2)})


def watch_names() -> tuple[str, ...]:
    return tuple(sorted({name for rows in BOSS_MECHANICS.values() for row in rows
                         if row.kind == "buff" for name in row.names}))
