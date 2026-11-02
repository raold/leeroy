"""Rare household asides overheard on comms, at most one attempt per guild night."""
import math

from leeroy_mind.clock import night_of
from leeroy_mind.talk.raid import RaidPhase
from leeroy_mind.talk.turns import Turn, TurnKind
from leeroy_mind.voice.rooms import PRIVATE

CHECK_S = 60.0
CHECKS_PER_MONTH = 30 * 4 * 60
NIGHT_KEY = "voice_hotmic_night"
HOT_MIC = ("You forgot to let go of push-to-talk: say one short thing off-mic to someone at "
           "home ({household}), the way you would, not to the guild.")
_BLOCKED = (RaidPhase.COMBAT, RaidPhase.WIPE_SILENCE, RaidPhase.WIPE_DEBRIEF)


class HotMic:
    def __init__(self, ext, *, per_month: float, rng):
        if not math.isfinite(per_month) or per_month < 0:
            raise ValueError("hot-mic rate must be finite and nonnegative")
        self.ext, self.rng = ext, rng
        self.chance = min(1.0, per_month / CHECKS_PER_MONTH)
        self._next_check = 0.0
        self._running = False

    async def tick(self, now: float) -> None:
        if self._running or now < self._next_check:
            return
        self._next_check = now + CHECK_S
        app, ext = self.ext.app, self.ext
        world = app.world
        if (not self.chance or not world.human_online or world.human_afk
                or ext.comms is None or not ext.comms.available()
                or app.raid.phase(now) in _BLOCKED):
            return
        room = ext.rooms.human_room()
        if room is None or room in (*PRIVATE, "afk"):
            return
        night = night_of(app.clock.local())
        if app.store.get_meta(NIGHT_KEY) == night:
            return
        members = sorted(g for g in ext.rooms.occupants(room) & ext.on_comms & world.online
                         if g not in world.afk and app.personas.get(g) is not None
                         and app.raid.allows(g, TurnKind.OPEN, now))
        if not members or self.rng.random() >= self.chance:
            return
        speaker = self.rng.choice(members)
        persona = app.personas.get(speaker)
        turn = Turn(kind=TurnKind.OPEN, speaker=speaker, channel=f"voice:{room}", max_lines=1,
                    instruction=HOT_MIC.format(household=persona.real_life.household))
        # Reserve before yielding: a concurrent tick, reconnect or restart cannot produce
        # two moments. A cancelled or refused attempt conservatively consumes this night.
        app.store.set_meta(NIGHT_KEY, night)
        self._running = True
        try:
            await app.engine.speak(turn, why="hotmic")
        finally:
            self._running = False
