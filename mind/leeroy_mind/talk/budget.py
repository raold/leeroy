"""When the guild talks. Social chat comes in bursts: a gap of minutes, then a few lines. The
budget sits above the loop guards in guards.py (6 turns a minute, 1 per person a minute): it
decides whether a burst starts at all, the guards still brake each turn.

The openers addressed to him (owner, 2026-11-07: nobody ever started a conversation with him)
run on a clock of their own here (to_him): mind.toml's [talk] openers_per_hour at chattiness
4, scaled with chattiness and the talkative dials of who could speak (engine._open_to_him)."""
import math
import random
from collections import deque, namedtuple
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from leeroy_mind.talk.raid import RaidPhase

GAP_REFERENCE_CHATTINESS = 4
IDLE_GAP_MULT = 3.0
RAID_GAP_S = (90.0, 150.0)
HOLD_PUSH_S = 60.0  # in a fight, look again no sooner than this
BURST_TIMEOUT_S = 300.0  # a burst whose burst_done never came is abandoned
# A gap of an initiation clock (the openers to him here, romance.WhisperPlanner's whispers) as a
# share of its mean, 3600 / the rate: roughly regular, never bunched.
GAP_SPREAD = (0.6, 1.4)
# An opener to him waits this long after a burst, and after his own line or an answer to him: it
# never talks over a conversation, nor meets the guards' full minute.
AFTER_BURST_S = 60.0
# Whatever the rate and the dials, at most this many openers to him in any hour (controller
# ruling, 2026-11-07).
TO_HIM_HOUR_MAX = 12
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
    to_him: bool = False  # one opener addressed to him (BurstBudget.to_him)


class BurstBudget:
    def __init__(self, chattiness: float, rng: random.Random, *,
                 gap_s: tuple[float, float] = (240, 720), size: tuple[int, int] = (3, 6),
                 to_him_per_hour: float = 0.0):
        self._c = chattiness
        self._rng = rng
        self._gap = gap_s
        self._size = size
        self._next_at: float | None = None
        self._mode: str | None = None  # "social" | "raid"; a fight does not change it
        self._burst_since: float | None = None
        self._burst_end = -math.inf
        self._phase = DayPhase.EVENING
        self._idle = False
        self._last_now = 0.0
        # the openers to him: their rate at chattiness 4, the talkative dials' pull on it,
        # when the next is due and when the one under way began
        self._him_rate = max(0.0, to_him_per_hour)
        self._him_mult = 1.0
        self._him_next: float | None = None
        self._him_since: float | None = None
        self._him_hour: deque[float] = deque()  # when each opener of the last hour was planned

    @property
    def to_him_next(self) -> float | None:
        """When the next opener to him is due (None: not drawn yet, or off)."""
        return self._him_next

    def reset_to_him(self) -> None:
        """His new session: the openers' clock starts over at its next call, as the
        WhisperPlanner's does; the hour's count stays."""
        if self._him_since is None:
            self._him_next = None

    def _rescaled(self, at: float | None, old: float, new: float) -> float | None:
        if at is not None and old > 0 and new > 0:
            left = max(0.0, at - self._last_now)
            return self._last_now + left * old / new
        return None

    def set_chattiness(self, c: float) -> None:
        old, self._c = self._c, c
        if c == old:
            return
        if self._him_since is None:
            self._him_next = self._rescaled(self._him_next, old, c)
        if self._burst_since is not None:
            return
        self._next_at = self._rescaled(self._next_at, old, c)

    def set_to_him_rate(self, per_hour: float) -> None:
        """A new [talk] openers_per_hour: the pending gap is rescaled; 0 stops the clock."""
        old, self._him_rate = self._him_rate, max(0.0, per_hour)
        if self._him_rate != old and self._him_since is None:
            self._him_next = self._rescaled(self._him_next, old, self._him_rate)

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
        self._burst_end = now
        self._next_at = now + self._social_gap() if self._c > 0 else None

    def _him_gap(self) -> float | None:
        """A gap of the openers' clock, its rate capped at TO_HIM_HOUR_MAX; a pull of 0
        (nobody would speak) keeps the written rate, whose draws then find nobody, so a later
        dial change is still read."""
        rate = min(float(TO_HIM_HOUR_MAX),
                   self._him_rate * self._c / GAP_REFERENCE_CHATTINESS * (self._him_mult or 1.0))
        return 3600.0 / rate * self._rng.uniform(*GAP_SPREAD) if rate > 0 else None

    def to_him(self, now: float, *, raid: RaidPhase, quiet_until: float | None = None,
               hold: bool = False, talk_at: float = -math.inf) -> BurstPlan | None:
        """The openers addressed to him: a one-line plan (to_him) when the clock's next is
        due. The first call draws the first gap. A due one waits while a burst runs and
        AFTER_BURST_S after it or after `talk_at` (his last line, or the last answer to him),
        until the end of his login's quiet (`quiet_until`), while TO_HIM_HOUR_MAX were planned
        in the last hour, and while `hold` (he is AFK, his group is in a boss fight) or his
        raid is in a fight, its wipe or its debrief (then HOLD_PUSH_S after them at the
        soonest). At chattiness 0 or a rate of 0 there are none."""
        self._last_now = now
        while self._him_hour and now - self._him_hour[0] >= 3600.0:
            self._him_hour.popleft()
        if self._c <= 0 or self._him_rate <= 0:
            self._him_next = None
            return None
        if self._him_since is not None:
            if now - self._him_since < BURST_TIMEOUT_S:
                return None
            self.to_him_done(self._him_since + BURST_TIMEOUT_S)
        if self._him_next is None:
            self._him_next = now + self._him_gap()
            return None
        if raid in _HOLD or hold:
            self._him_next = max(self._him_next, now + HOLD_PUSH_S)
            return None
        if quiet_until is not None and now < quiet_until:
            self._him_next = max(self._him_next, quiet_until)
            return None
        if (now < self._him_next or self._burst_since is not None
                or now - max(self._burst_end, talk_at) < AFTER_BURST_S
                or len(self._him_hour) >= TO_HIM_HOUR_MAX):
            return None
        self._him_since = now
        self._him_hour.append(now)
        return BurstPlan(1, 1, to_him=True)

    def to_him_done(self, now: float, mult: float | None = None) -> None:
        """The opener to him is over (said, dropped or skipped); `mult` is the talkative
        dials' pull on the rate (their mean over who could speak), when it was read."""
        self._him_since = None
        if mult is not None:
            self._him_mult = max(0.0, mult)
        gap = self._him_gap()
        self._him_next = None if gap is None else now + gap
