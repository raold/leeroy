"""Small, low-priority conversations in rooms the human cannot hear.

This deliberately has its own burst and guards: the talk engine's burst spends
the human's conversation budget and changes the quiet clock he experiences.
"""
from collections import deque

from leeroy_mind.llm.scheduler import Priority
from leeroy_mind.talk.engine import CONTINUE_S, OPEN_S
from leeroy_mind.talk.guards import Guards
from leeroy_mind.talk.turns import Turn, TurnKind
from leeroy_mind.voice.rooms import PRIVATE

CHECK_S = 60.0
WINDOW_S = 600.0
BUSY = (Priority.REPLY_HUMAN, Priority.RAID_LEAD, Priority.SOCIAL)


class ElsewhereTalk:
    def __init__(self, ext, *, lines_per_10min: int, rng):
        self.ext, self.rng = ext, rng
        self.limit = lines_per_10min
        self.guards = Guards(rng=rng)
        self._lines: deque[float] = deque()
        self._next_check = 0.0
        self._running = False

    def _ready(self) -> bool:
        ext = self.ext
        return (ext.app.world.human_online and ext.comms.available()
                and not any(ext.app.engine.scheduler.pending().get(p, 0) for p in BUSY))

    def _members(self, room: str) -> list[int]:
        ext, world = self.ext, self.ext.app.world
        return sorted(g for g in ext.rooms.occupants(room) & ext.on_comms & world.online
                      if g not in world.afk and ext.app.personas.get(g) is not None)

    def _room_ok(self, room: str) -> bool:
        return room not in (*PRIVATE, "afk", self.ext.rooms.human_room())

    def _remaining(self, now: float) -> int:
        while self._lines and now - self._lines[0] >= WINDOW_S:
            self._lines.popleft()
        return max(0, self.limit - len(self._lines))

    async def tick(self, now: float) -> None:
        if self._running or now < self._next_check:
            return
        self._next_check = now + CHECK_S
        if not self._remaining(now) or not self._ready():
            return
        rooms = sorted({r for r in self.ext.rooms.where.values() if self._room_ok(r)})
        rooms = [r for r in rooms if len(self._members(r)) >= 2]
        if not rooms:
            return
        room = self.rng.choices(rooms, weights=[len(self._members(r)) for r in rooms])[0]
        self._running = True
        try:
            await self._scene(room)
        finally:
            self._running = False

    async def _scene(self, room: str) -> None:
        engine, clock = self.ext.app.engine, self.ext.app.clock
        previous = trigger = None
        for index in range(3):
            now = clock.now()
            if not self._ready() or not self._room_ok(room) or not self._remaining(now):
                return
            members = self._members(room)
            if len(members) < 2 or (previous is not None and previous not in members):
                return
            kind = TurnKind.OPEN if index == 0 else TurnKind.CONTINUE
            pool = [g for g in members if g != previous and self.guards.allow(g, kind, now)
                    and self.ext.app.raid.allows(g, kind, now)]
            if not pool:
                return
            speaker = self.rng.choice(pool)
            delay = self.rng.uniform(*(OPEN_S if index == 0 else CONTINUE_S))
            turn = Turn(kind=kind, speaker=speaker, channel=f"voice:{room}",
                        addressee=previous, trigger_line=trigger, max_lines=1,
                        delay=delay, deadline=now + delay + 120,
                        priority=Priority.BACKGROUND)
            said = await engine.speak(turn, why=f"elsewhere:{kind.value}",
                                      elsewhere=True, guards=self.guards)
            if not said:
                return
            self._lines.extend([clock.now()] * len(said))
            previous, trigger = speaker, engine._last_line_id.get(speaker)
            if trigger is None:
                return
