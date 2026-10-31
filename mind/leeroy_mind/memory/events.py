from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from leeroy_mind.clock import Clock, night_of
from leeroy_mind.memory.writer import MemoryCandidate, MemoryWriter, WriteResult
from leeroy_mind.personas.schema import Persona
from leeroy_mind.protocol import Achievement, BgEnd, Boss, Death, LootWon, Zone
from leeroy_mind.storage.store import Store
from leeroy_mind.text import strip_wow_markup
from leeroy_mind.wow import boss_name, faction_of, is_raid_fight

MAX_EVIDENCE = 20


def _ev(event_id: int) -> str:
    return f"event:{event_id}"


@dataclass
class _Encounter:
    instance_id: int
    members: tuple[int, ...]
    started: int
    evidence: list[str] = field(default_factory=list)
    deaths: list[tuple[int, str]] = field(default_factory=list)


class ActivityTracker:
    """Battleground and zone events, summed into one memory per member by flush()."""

    def __init__(self, store: Store, human_guid: Callable[[], int]):
        self._store = store
        self._human_guid = human_guid
        self._bgs: dict[int, list[bool]] = {}
        self._zones: dict[int, Counter] = {}
        self._evidence: dict[int, list[str]] = {}

    def _note(self, guid: int, event_id: int) -> None:
        ev = self._evidence.setdefault(guid, [])
        if len(ev) < MAX_EVIDENCE:
            ev.append(_ev(event_id))

    def on_bg_end(self, ev: BgEnd, event_id: int) -> None:
        cast = self._store.cast()
        for g in ev.members:
            m = cast.get(g)
            if m is None or g == self._human_guid():
                continue
            self._bgs.setdefault(g, []).append(faction_of(m.race) == ev.winner.strip().lower())
            self._note(g, event_id)

    def on_zone(self, ev: Zone, event_id: int) -> None:
        if ev.guid == self._human_guid() or ev.guid not in self._store.cast() or not ev.zone_name:
            return
        self._zones.setdefault(ev.guid, Counter())[strip_wow_markup(ev.zone_name)] += 1
        self._note(ev.guid, event_id)

    async def flush(self, writer: MemoryWriter, now: float) -> list[WriteResult]:
        guids = sorted(set(self._bgs) | set(self._zones))
        bgs, zones, evidence = self._bgs, self._zones, self._evidence
        self._bgs, self._zones, self._evidence = {}, {}, {}
        out = []
        for g in guids:
            results = bgs.get(g, [])
            won = sum(results)
            top = [z for z, _ in zones.get(g, Counter()).most_common(3)]
            parts = []
            if results:
                n = len(results)
                parts.append(f"{n} {'battleground' if n == 1 else 'battlegrounds'} ({won} won)")
            if top:
                parts.append("spent time in " + ", ".join(top))
            text = "Tonight: " + "; ".join(parts)
            out.append(await writer.write(MemoryCandidate(
                owner=g, kind="event", text=text + ".", t_event=int(now), actors=(g,),
                importance=3 if won >= 3 else 2, valence=0.0, source="game",
                evidence=tuple(evidence.get(g, ()))[:MAX_EVIDENCE])))
        return out


class EventMemoryRules:
    """Turns game events into memories. Bosses are keyed by (map, boss_id): the bridge sends
    no boss name and boss_id repeats across raids. A death carries no instance, so it joins the
    engaged encounter on its map that lists the guid.

    An event the bridge replayed after a restart comes with `at`, the time it happened (the
    server's clock, the same machine's): its memories, wipe count and night keys are that
    night's, not the night it was replayed on (correctness M2). A live event is now."""

    def __init__(self, writer: MemoryWriter, store: Store, clock: Clock,
                 personas: Callable[[int], Persona | None], human_guid: Callable[[], int]):
        self._writer = writer
        self._store = store
        self._clock = clock
        self._personas = personas
        self._human_guid = human_guid
        self._engaged: dict[tuple[int, int, int], _Encounter] = {}
        self._night_evidence: dict[str, list[str]] = {}
        self.activity = ActivityTracker(store, human_guid)

    # -- helpers -----------------------------------------------------------------------

    def _now(self, at: float | None = None) -> int:
        return int(self._clock.now() if at is None else at)

    def _night(self, at: float | None = None) -> str:
        if at is None:
            return night_of(self._clock.local())
        return night_of(datetime.fromtimestamp(at, tz=self._clock.local().tzinfo))

    @staticmethod
    def _wipes_key(map_id: int, boss_id: int, night: str) -> str:
        return f"wipes:{map_id}:{boss_id}:{night}"

    def wipes_tonight(self, map_id: int, boss_id: int) -> int:
        return int(self._store.get_meta(self._wipes_key(map_id, boss_id, self._night())) or 0)

    def _cast_members(self, members) -> list[int]:
        human = self._human_guid()
        return [g for g in dict.fromkeys(members) if g != human]

    # -- dispatch ----------------------------------------------------------------------

    async def on_event(self, ev, event_id: int, at: float | None = None) -> list[WriteResult]:
        """`at`: when a replayed event happened (None: a live one, now)."""
        if isinstance(ev, Boss):
            return await self._on_boss(ev, event_id, at)
        if isinstance(ev, Death):
            self._on_death(ev, event_id)
        elif isinstance(ev, LootWon):
            return await self._on_loot_won(ev, event_id, at)
        elif isinstance(ev, Achievement):
            return await self._on_achievement(ev, event_id, at)
        elif isinstance(ev, BgEnd):
            self.activity.on_bg_end(ev, event_id)
        elif isinstance(ev, Zone):
            self.activity.on_zone(ev, event_id)
        return []

    # -- bosses ------------------------------------------------------------------------

    def _on_death(self, ev: Death, event_id: int) -> None:
        mine = [e for (m, _, _), e in self._engaged.items() if m == ev.map and ev.guid in e.members]
        if not mine:
            return
        enc = max(mine, key=lambda e: e.started)
        if all(g != ev.guid for g, _ in enc.deaths):
            enc.deaths.append((ev.guid, _ev(event_id)))

    async def _on_boss(self, ev: Boss, event_id: int, at: float | None = None
                       ) -> list[WriteResult]:
        # Raids only, and never the non-fight steps (gauntlets, Halion's intro).
        if not is_raid_fight(ev.map, ev.boss_id):
            return []
        ident = (ev.map, ev.boss_id, ev.instance_id)
        evid = _ev(event_id)
        if ev.state == "engaged":
            self._engaged[ident] = _Encounter(ev.instance_id, tuple(ev.members), self._now(at),
                                              [evid])
            return []
        if ev.state not in ("wiped", "killed"):
            return []
        enc = self._engaged.pop(ident, None) or _Encounter(
            ev.instance_id, tuple(ev.members), self._now(at))
        everyone = tuple(dict.fromkeys([*enc.members, *ev.members]))
        members = self._cast_members(everyone)
        night = self._night(at)
        name = boss_name(ev.map, ev.boss_id, ev.boss_name)
        wkey = self._wipes_key(ev.map, ev.boss_id, night)
        killed_key = f"killed:{ev.map}:{ev.boss_id}:{night}"
        wipes = int(self._store.get_meta(wkey) or 0)
        tkey = f"{ev.map}:{ev.boss_id}:{night}"
        for k in [k for k in self._night_evidence if not k.endswith(":" + night)]:
            del self._night_evidence[k]
        trail = self._night_evidence.setdefault(tkey, [])
        trail.extend(e for e in [*enc.evidence, evid] if e not in trail)
        del trail[:-MAX_EVIDENCE]
        out: list[WriteResult] = []
        if ev.state == "wiped":
            wipes += 1
            self._store.set_meta(wkey, str(wipes))
            if enc.deaths and enc.deaths[0][0] in members:
                first, dev = enc.deaths[0]
                out.append(await self._write(first, f"I died first on {name} and we wiped.",
                                             importance=4, valence=-0.4, actors=(first,),
                                             evidence=(dev, evid), boss=ev.boss_id,
                                             raid=ev.map, at=at))
            if self._store.get_meta(killed_key) is not None:
                return out
            text = (f"We wiped {wipes}× on {name} tonight." if wipes > 1
                    else f"We wiped on {name} tonight.")
            importance, valence = self._boss_scores(wipes, False)
        else:
            self._store.set_meta(killed_key, "1")
            # The guild already holds server firsts on every raid boss: no first-kill note.
            if wipes > 1:
                text = f"We wiped {wipes}× on {name}, then killed it."
            elif wipes == 1:
                text = f"We wiped once on {name}, then killed it."
            else:
                text = f"We killed {name}."
            importance, valence = self._boss_scores(wipes, True)
        key = f"boss:{ev.map}:{ev.boss_id}:{night}"
        for owner in [0, *members]:
            out.append(await self._writer.write(MemoryCandidate(
                owner=owner, kind="event", text=text, t_event=self._now(at),
                actors=everyone, importance=importance, valence=valence, source="game",
                evidence=tuple(trail), boss_id=ev.boss_id, raid_id=ev.map, key=key)))
        return out

    @staticmethod
    def _boss_scores(wipes: int, killed: bool) -> tuple[int, float]:
        importance = min(10, 4 + min(4, wipes // 2))
        if killed:
            return importance, 0.6
        return importance, -min(0.8, 0.15 * wipes)

    # -- loot and achievements ---------------------------------------------------------

    async def _write(self, owner: int, text: str, *, importance: int, valence: float,
                     actors: tuple[int, ...], evidence: tuple[str, ...], boss: int | None = None,
                     raid: int | None = None, at: float | None = None) -> WriteResult:
        return await self._writer.write(MemoryCandidate(
            owner=owner, kind="event", text=text, t_event=self._now(at), actors=actors,
            importance=importance, valence=valence, source="game", evidence=evidence,
            boss_id=boss, raid_id=raid))

    async def _on_loot_won(self, ev: LootWon, event_id: int, at: float | None = None
                           ) -> list[WriteResult]:
        if ev.guid == self._human_guid():
            return []
        item = strip_wow_markup(ev.name).strip()
        if not item:
            return []
        persona = self._personas(ev.guid)
        low = item.casefold()
        wanted = persona is not None and any(
            w and (w.casefold() in low or low in w.casefold()) for w in persona.game.wants)
        if wanted:
            text, importance, valence = f"Finally got {item}.", 6, 0.7
        else:
            text, importance, valence = f"I won {item}.", 3, 0.3
        return [await self._write(ev.guid, text, importance=importance, valence=valence,
                                  actors=(ev.guid,), evidence=(_ev(event_id),), at=at)]

    async def on_loot_lost(self, guid: int, item_name: str, evidence) -> WriteResult:
        """P1d extension point: called with DKP results."""
        if isinstance(evidence, str):
            evidence = (evidence,)
        item = strip_wow_markup(item_name).strip()
        if guid == self._human_guid() or not item:
            return WriteResult(None, "rejected")
        return await self._write(guid, f"I lost the roll for {item}.", importance=4,
                                 valence=-0.4, actors=(guid,), evidence=tuple(evidence))

    async def _on_achievement(self, ev: Achievement, event_id: int, at: float | None = None
                              ) -> list[WriteResult]:
        name = strip_wow_markup(ev.name).strip()
        if ev.guid == self._human_guid() or not name:
            return []
        return [await self._write(ev.guid, f"I earned the achievement {name}.", importance=5,
                                  valence=0.5, actors=(ev.guid,), evidence=(_ev(event_id),),
                                  at=at)]
