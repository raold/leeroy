"""The rows toward the human from what each stored persona declares toward him (controller
ruling "final I2"). Seeding used to drop every "@human" declaration, so all those rows hold
only their stance's seed. Here each one gets seed.human_seed of its stance and declaration: a
friend starts close to him, a friction annoyed with him, a stance declaration keeps the
stance's values; every one keeps its note and the stance's attraction and romance stage.

Only rows nobody has lived in are written: a row with interactions, or with values that are
not its stance's seed, is reported and left alone. A row that already holds the result is
unchanged, so running this again does nothing."""
from collections.abc import Iterable
from dataclasses import dataclass, replace

from ..human import HumanIdentity
from ..storage.rows import Relationship
from .schema import Persona
from .seed import human_declaration, human_ref, human_seed, seed_values


@dataclass(frozen=True)
class Relink:
    guid: int
    name: str
    kind: str          # the declaration that decides the row: friend, friction or stance
    action: str        # "write", "unchanged" or "skip"
    why: str           # what a skip found
    row: Relationship  # the row it becomes ("write"), or as it stays


def _one(store, p: Persona, toward, human_guid: int, now: float) -> Relink:
    kind, note = human_declaration(p, toward)
    cur = store.get_relationship(p.guid, human_guid)
    target = human_seed(p.guid, human_guid, p.romance.stance, kind, note)

    def out(action: str, row: Relationship, why: str = "") -> Relink:
        return Relink(p.guid, p.character_name, kind, action, why, row)
    if seed_values(cur) == seed_values(target):
        return out("unchanged", cur)
    if cur.interactions:
        return out("skip", cur, f"lived in ({cur.interactions} interactions)")
    if cur == Relationship(a=p.guid, b=human_guid):  # no row yet
        return out("write", replace(target, last_seen=int(now)))
    if seed_values(cur) != seed_values(human_seed(p.guid, human_guid, p.romance.stance)):
        return out("skip", cur, f"not the {p.romance.stance} seed (changed by hand or in play)")
    return out("write", replace(target, last_seen=cur.last_seen))


def plan_relink(store, personas: Iterable[Persona], human: HumanIdentity, *,
                now: float) -> list[Relink]:
    """One Relink per persona, by guid. Writes nothing. `human` decides which targets mean
    him (seed.human_ref: "@human", his names, "GM" when he is the guild master)."""
    toward = human_ref(human, {m.name: m.guid for m in store.cast().values()})
    return [_one(store, p, toward, human.guid, now)
            for p in sorted(personas, key=lambda p: p.guid)]


def apply_relink(store, personas: Iterable[Persona], human: HumanIdentity, *,
                 now: float) -> list[Relink]:
    """Plan and write in one transaction (joining the caller's), so every row is checked as
    it is when it is written. Returns the plan that was applied."""
    with store.transaction():
        plan = plan_relink(store, personas, human, now=now)
        for r in plan:
            if r.action == "write":
                store.put_relationship(r.row)
    return plan
