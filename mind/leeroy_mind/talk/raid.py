"""Raid comms state. Fights are keyed by (map, boss_id), never boss_id alone, and the engaged
state is kept per instance as well. Only the raid's own fights count: a boss event whose
members share nobody with the raid group is another group's, and when a raid group forms it
keeps only the pulls its members were in (Task 12 carry). Wipe counts live in memory, for the
night: a restart starts them over."""
from datetime import datetime
from enum import StrEnum
from typing import Callable

from leeroy_mind.clock import Clock, night_of
from leeroy_mind.personas.schema import Persona
from leeroy_mind.protocol import Boss, Group
from leeroy_mind.talk.turns import TurnKind
from leeroy_mind.talk.world import WorldView
from leeroy_mind.wow import is_raid_fight

WIPE_SILENCE_S = 10.0
WIPE_DEBRIEF_S = 60.0
KILL_S = 45.0
MAX_FIGHT_S = 20 * 60.0  # a fight this old has lost its end event; leave COMBAT

_URGENT = frozenset({TurnKind.URGENT})
_LEAD_COMBAT = frozenset({TurnKind.URGENT, TurnKind.CALLOUT, TurnKind.REPLY_HUMAN})
_DEBRIEF_ANY = frozenset({TurnKind.URGENT, TurnKind.WIPE_REACT})


def _guids(g: Group) -> frozenset[int]:
    return frozenset(g.members) | {g.leader_guid}


class RaidPhase(StrEnum):
    NONE = "none"
    TRASH = "trash"
    COMBAT = "combat"
    WIPE_SILENCE = "wipe_silence"
    WIPE_DEBRIEF = "wipe_debrief"
    KILL = "kill"


class RaidState:
    def __init__(self, *, world: WorldView, personas: Callable[[int], Persona | None],
                 clock: Clock):
        self._clock = clock
        self._world = world
        self._personas = personas
        # (map, instance, boss) -> (start, who was in the fight)
        self._engaged: dict[tuple[int, int, int], tuple[float, frozenset[int]]] = {}
        self._ended: tuple[str, float] | None = None  # ("wiped" | "killed", at)
        self._wipes: dict[tuple[int, int, str], int] = {}
        self._fights: dict[tuple[int, int], tuple[str, float | None]] = {}  # last_fight
        self._lead_override: int | None = None

    def on_group(self, ev: Group) -> None:
        old = self._world.group
        self._world.set_group(ev)
        g = self._world.group
        if g is None or not g.raid:
            self._engaged.clear()
            self._ended = None
        elif old is None or not old.raid:  # a raid formed: only the pulls it was in count
            mine = _guids(g)
            self._engaged = {k: v for k, v in self._engaged.items() if v[1] & mine}

    def on_boss(self, ev: Boss, now: float) -> None:
        """A boss event, taken at its own server time (ms, the same machine's clock) when it
        has one, so a backlog replayed after a restart counts when it happened: last night's
        wipes are last night's, and a replayed pull whose end was lost is long over
        (correctness M2). `now` stands in for an event without one."""
        if not is_raid_fight(ev.map, ev.boss_id):
            return
        if ev.state not in ("engaged", "wiped", "killed"):
            return
        g = self._world.group
        if g is not None and g.raid and not set(ev.members) & _guids(g):
            return  # another group's fight
        at = ev.ts / 1000 if ev.ts > 0 else now
        key = (ev.map, ev.instance_id, ev.boss_id)
        if ev.state == "engaged":
            self._engaged[key] = (at, frozenset(ev.members))
            self._ended = None
            return
        start = self._engaged.pop(key, (None, None))[0]
        self._fights[(ev.map, ev.boss_id)] = (ev.state, None if start is None else at - start)
        self._ended = (ev.state, at)
        if ev.state == "wiped":
            local = datetime.fromtimestamp(at, tz=self._clock.local().tzinfo)
            wk = (ev.map, ev.boss_id, night_of(local))
            self._wipes[wk] = self._wipes.get(wk, 0) + 1

    def phase(self, now: float) -> RaidPhase:
        g = self._world.group
        if g is None or not g.raid:
            return RaidPhase.NONE
        if any(now - at < MAX_FIGHT_S for at, _ in self._engaged.values()):
            return RaidPhase.COMBAT
        if self._ended is not None:
            what, at = self._ended
            age = now - at
            if what == "wiped":
                if age < WIPE_SILENCE_S:
                    return RaidPhase.WIPE_SILENCE
                if age < WIPE_DEBRIEF_S:
                    return RaidPhase.WIPE_DEBRIEF
            elif age < KILL_S:
                return RaidPhase.KILL
        return RaidPhase.TRASH

    def set_lead(self, guid: int | None) -> None:
        """The P1d extension point: an explicit lead wins over the group's."""
        self._lead_override = guid

    def lead(self) -> int | None:
        if self._lead_override is not None:
            return self._lead_override
        g = self._world.group
        if g is None:
            return None
        cast = self._world.cast_guids()
        if g.leader_guid in cast:
            return g.leader_guid
        officers = [m for m in g.members if m in cast and self._world.is_officer(m)]
        for m in officers:
            p = self._personas(m)
            if p is not None and "raid lead" in p.play.officer_title.casefold():
                return m
        return officers[0] if officers else None

    def wipes_tonight(self, map_id: int, boss_id: int) -> int:
        return self._wipes.get((map_id, boss_id, night_of(self._clock.local())), 0)

    def last_fight(self, map_id: int, boss_id: int) -> tuple[str, float | None] | None:
        """How the last pull on this boss ended ("wiped" or "killed") and how long it lasted
        in seconds (None when its start was never seen), or None before any pull ended."""
        return self._fights.get((map_id, boss_id))

    def allows(self, guid: int, kind: TurnKind, now: float) -> bool:
        ph = self.phase(now)
        if ph in (RaidPhase.NONE, RaidPhase.TRASH, RaidPhase.KILL):
            return True
        if ph is RaidPhase.WIPE_SILENCE:
            return kind in _URGENT
        leader = guid == self.lead() or (self._world.is_officer(guid)
                                         and self._world.in_group(guid))
        if ph is RaidPhase.COMBAT:
            return kind in (_LEAD_COMBAT if leader else _URGENT)
        if kind in _DEBRIEF_ANY:
            return True
        return leader and kind in (TurnKind.CALLOUT, TurnKind.REPLY_HUMAN)
