"""Bits: a member's running joke or ritual (persona.bits), brought out now and then.

A bit never comes up in a fight or a wipe, or from someone in a bad mood, and plays at most
once a night (even with min_days_between 0). Otherwise it needs a matching context and a
cooled-down last use, and only one voice bit plays per night across the whole guild. A bit
with `dates` (MM-DD of the night) plays only on those dates, where it skips the cooldown and
the voice-bit rule and fires at SPECIAL_P instead of p_fire (rehearsal 2, judge B 1: dates only
raised the odds, and a birthday was announced on any night). A birthday bit plays only on a
night that is a cast member's birthday, by their card, and on its own dates when it has any
(fix round 1, review I6), and names whose birthday it is: one or two, and how many others
share it (birthday_line). Nights follow clock.night_of, so 01:00 still belongs to the
evening before."""
import random
from datetime import datetime

from leeroy_mind.clock import Clock, night_of
from leeroy_mind.personas.schema import Bit, Persona
from leeroy_mind.storage.store import Store
from leeroy_mind.talk.raid import RaidPhase

DAY_S = 86400.0
JITTER = (0.8, 1.2)
SPECIAL_P = 0.5
BAD_MOOD = -0.5
_HOLD = frozenset({RaidPhase.COMBAT, RaidPhase.WIPE_SILENCE, RaidPhase.WIPE_DEBRIEF})


def _night_of_ts(ts: float, tz) -> str:
    return night_of(datetime.fromtimestamp(ts, tz=tz))


def birthday_line(named: list[str], total: int) -> str:
    """Whose birthday tonight is, for a birthday bit: `named` (one or two members) of `total`,
    for when several members share a birthday."""
    if total <= 1 and named:
        return f"Tonight is {named[0]}'s birthday: it's {named[0]} you wish a happy birthday."
    others = total - len(named)
    who = named + ([f"{others} others in the guild"] if others > 0 else [])
    joined = who[0] if len(who) == 1 else f"{', '.join(who[:-1])} and {who[-1]}"
    return f"Tonight is the birthday of {joined}: wish them {'all ' if others > 0 else ''}a " \
           "happy birthday."


def is_birthday_bit(bit: Bit) -> bool:
    """A bit about someone's birthday (a member's birthday_announce): it needs a birthday."""
    return "birthday" in bit.id.casefold() or "birthday" in bit.instruction.casefold()


class BitScheduler:
    def __init__(self, store: Store, clock: Clock, rng: random.Random, *, p_fire: float = 0.15):
        self._store = store
        self._clock = clock
        self._rng = rng
        self.p_fire = p_fire

    def _cooled(self, guid: int, bit: Bit, now: float) -> bool:
        """Due again at last_use + min_days_between days x U(0.8, 1.2). The jitter is seeded by
        (guid, bit id, last use), so it is fixed for each use and never drawn from self._rng."""
        last = self._store.last_bit_use(guid, bit.id)
        if last is None:
            return True
        jitter = random.Random(f"{guid}:{bit.id}:{last}").uniform(*JITTER)
        return now >= last + bit.min_days_between * DAY_S * jitter

    def _used_tonight(self, guid: int, bit: Bit, night: str, tz) -> bool:
        last = self._store.last_bit_use(guid, bit.id)
        return last is not None and _night_of_ts(last, tz) == night

    def pick(self, p: Persona, *, context: set[str], raid: RaidPhase, mood: float,
             local: datetime, birthday: str | None = None) -> Bit | None:
        """A bit for this speaker now, or None. `local` is the mind clock's local time (its
        timestamp is "now"); `context` holds the situation tags (org, trash, after_kill,
        after_wipe, late_night, loot). A bit whose contexts are empty or include "any" fits
        every situation. `birthday`: the cast member whose birthday tonight is, if anyone's
        (a birthday bit needs one)."""
        if raid in _HOLD or mood < BAD_MOOD:
            return None
        now, night = local.timestamp(), night_of(local)
        today = night[5:]
        special, usual = [], []
        for b in p.bits:
            if b.contexts and "any" not in b.contexts and not set(b.contexts) & context:
                continue
            if self._used_tonight(p.guid, b, night, local.tzinfo):
                continue
            if is_birthday_bit(b):  # a cast birthday tonight, and its own dates if any
                if birthday and (not b.dates or today in b.dates):
                    special.append(b)
                continue
            if b.dates:  # a dated bit plays only on its dates
                if today in b.dates:
                    special.append(b)
                continue
            if b.voice_bit and self._store.voice_bit_used_on(night):
                continue
            if self._cooled(p.guid, b, now):
                usual.append(b)
        pool, chance = (special, SPECIAL_P) if special else (usual, self.p_fire)
        if not pool or self._rng.random() >= chance:
            return None
        return pool[0] if len(pool) == 1 else self._rng.choice(pool)

    def record(self, guid: int, bit: Bit, now: float, session_id: int | None) -> None:
        night = _night_of_ts(now, self._clock.local().tzinfo)
        self._store.record_bit(guid, bit.id, now, night, bit.voice_bit, session_id)

    def turn_instruction(self, bit: Bit, birthday: str = "") -> str:
        """The `extra` for a BIT turn (Task 16 sets Turn.max_lines to bit.lines). Bits are
        written as a habit or a condition ("once in a while", "if someone mentions..."), so
        the instruction is given as how they usually do it, then "do it now" (prompts lens,
        minor 9). The rules say one short line, so a longer bit says it is the exception, one
        line per row: the live preset stops on a blank line. A birthday bit says whose
        birthday tonight is (`birthday`: birthday_line's sentence)."""
        what = " ".join(bit.instruction.split()).rstrip(" .")
        if not what.endswith(("!", "?")):
            what += "."
        whose = f" {birthday}" if birthday and is_birthday_bit(bit) else ""
        size = ("Do it now, in one short line." if bit.lines == 1 else
                f"Do it now, in {bit.lines} short lines typed one after another, one line per "
                "row, with no blank lines between them: a bit is the one time you go past one "
                "line.")
        return (f"Your running bit, the way you usually do it: {what}{whose} {size} Hook it on "
                "what's happening right now and never announce it as a bit.")
