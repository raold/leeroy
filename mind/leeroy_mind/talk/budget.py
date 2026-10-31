"""When the guild talks. Social chat comes in bursts: a gap of minutes, then a few lines. The
budget sits above the loop guards in guards.py (6 turns a minute, 1 per person a minute): it
decides whether a burst starts at all, the guards still brake each turn."""
import random
from collections import namedtuple
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from leeroy_mind.talk.raid import RaidPhase

GAP_REFERENCE_CHATTINESS = 4
IDLE_GAP_MULT = 3.0
RAID_GAP_S = (90.0, 150.0)
HOLD_PUSH_S = 60.0  # in a fight, look again no sooner than this
BURST_TIMEOUT_S = 300.0  # a burst whose burst_done never came is abandoned
_HOLD = frozenset({RaidPhase.COMBAT, RaidPhase.WIPE_SILENCE, RaidPhase.WIPE_DEBRIEF})


class DayPhase(StrEnum):
    MORNING = "morning"
    DAY = "day"
    EVENING = "evening"
    LATE = "late"
    DEAD = "dead"


def day_phase(dt: datetime) -> DayPhase:
    """MORNING 06-10, DAY 10-17, EVENING 17-23, LATE 23-03, DEAD (the small hours) 03-06."""
    h = dt.hour
    if 6 <= h < 10:
        return DayPhase.MORNING
    if 10 <= h < 17:
        return DayPhase.DAY
    if 17 <= h < 23:
        return DayPhase.EVENING
    if h >= 23 or h < 3:
        return DayPhase.LATE
    return DayPhase.DEAD


PhaseMods = namedtuple("PhaseMods", "gap_mult max_speakers length_mult")
PHASE_MODS = {
    DayPhase.MORNING: PhaseMods(2.0, 2, 1.0),
    DayPhase.DAY: PhaseMods(1.2, 3, 1.0),
    DayPhase.EVENING: PhaseMods(1.0, 3, 1.0),
    DayPhase.LATE: PhaseMods(1.5, 2, 1.5),
    DayPhase.DEAD: PhaseMods(2.0, 2, 1.2),
}


@dataclass(frozen=True, slots=True)
class BurstPlan:
    size: int
    max_speakers: int
    channel_hint: str | None = None


class BurstBudget:
    def __init__(self, chattiness: float, rng: random.Random, *,
                 gap_s: tuple[float, float] = (240, 720), size: tuple[int, int] = (3, 6)):
        self._c = chattiness
        self._rng = rng
        self._gap = gap_s
        self._size = size
        self._next_at: float | None = None
        self._mode: str | None = None  # "social" | "raid"; a fight does not change it
        self._burst_since: float | None = None
        self._phase = DayPhase.EVENING
        self._idle = False
        self._last_now = 0.0

    def set_chattiness(self, c: float) -> None:
        old, self._c = self._c, c
        if c == old or self._burst_since is not None:
            return
        if self._next_at is not None and old > 0 and c > 0:
            left = max(0.0, self._next_at - self._last_now)
            self._next_at = self._last_now + left * old / c
        else:
            self._next_at = None

    def _social_gap(self) -> float:
        base = self._rng.uniform(*self._gap) * GAP_REFERENCE_CHATTINESS / self._c
        return base * PHASE_MODS[self._phase].gap_mult * (IDLE_GAP_MULT if self._idle else 1)

    def tick(self, now: float, *, phase: DayPhase, human_idle: bool, raid: RaidPhase,
             quiet_until: float | None = None) -> BurstPlan | None:
        """`quiet_until`: the end of his login's quiet. The schedule runs as usual, and a
        burst that falls due before it waits until then."""
        was_idle = self._idle
        self._phase, self._idle, self._last_now = phase, human_idle, now
        if self._c <= 0:
            return None
        if raid in _HOLD:
            if self._next_at is not None:
                self._next_at = max(self._next_at, now + HOLD_PUSH_S)
            return None
        if self._burst_since is not None:
            if now - self._burst_since < BURST_TIMEOUT_S:
                return None
            self.burst_done(self._burst_since + BURST_TIMEOUT_S)
        elif (was_idle and not human_idle and self._next_at is not None
              and raid is not RaidPhase.TRASH):
            self._next_at = min(self._next_at, now + self._social_gap())
        mode = "raid" if raid is RaidPhase.TRASH else "social"
        if self._next_at is None or (self._mode is not None and mode != self._mode):
            self._next_at = now + (self._rng.uniform(*RAID_GAP_S) if mode == "raid"
                                   else self._social_gap())
        self._mode = mode
        if quiet_until is not None and now < quiet_until:
            self._next_at = max(self._next_at, quiet_until)
            return None
        if now < self._next_at:
            return None
        if mode == "raid":
            self._next_at = now + self._rng.uniform(*RAID_GAP_S)
            return BurstPlan(1, 1, "raid")
        self._burst_since = now
        return BurstPlan(self._rng.randint(*self._size), PHASE_MODS[phase].max_speakers)

    def burst_done(self, now: float) -> None:
        self._burst_since = None
        self._next_at = now + self._social_gap() if self._c > 0 else None
