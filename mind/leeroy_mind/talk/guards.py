"""Loop guards. They count TURNS, not lines.

Task 16 calls allow() for every line, with continuation=True for lines 2+ of a turn (which
skips the per-person and guild checks), calls record(turn_id, ...) once per turn on the first
acked line (repeats are ignored), and still calls raid.allows and chain_open for every line."""
from collections import deque

from leeroy_mind.talk.turns import TurnKind

WINDOW_S = 60.0
REPLY_S = 10.0  # per-person interval for answers to the human in the channels all can read
_MAX_TURN_IDS = 256
CHAIN_CAP = (4, 6)
_MAX_CHAINS = 512
_NO_PERSON_LIMIT = frozenset({TurnKind.URGENT, TurnKind.CALLOUT})
_FULL_WINDOW = frozenset({TurnKind.REPLY_HUMAN, TurnKind.GREET, TurnKind.GOODNIGHT,
                          TurnKind.WHISPER})


class Guards:
    def __init__(self, *, per_person_s: float = 60, guild_per_min: int = 6,
                 social_ceiling: int = 4, rng):
        self.per_person_s = per_person_s
        self.guild_per_min = guild_per_min
        self.social_ceiling = social_ceiling
        self._rng = rng
        self._last: dict[int, float] = {}
        self._window: deque[tuple[float, int]] = deque()
        self._turn_ids: dict[str, None] = {}
        self._chains: dict[str, list[int]] = {}  # key -> [cast lines since the human, cap]

    def _in_window(self, now: float) -> int:
        while self._window and now - self._window[0][0] >= WINDOW_S:
            self._window.popleft()
        return len(self._window)

    def allow(self, guid: int, kind: TurnKind, now: float, *, continuation: bool = False,
              reply: bool = False, pushback: bool = False) -> bool:
        """`reply` marks an answer to the human's whisper: it always may go out (rehearsal 3,
        check 4: her reply to his whisper was dropped 4 s after a guild bit of hers). Her lines
        elsewhere and the guild's pace hold back only what the guild reads; it still counts
        toward them once said (record). `pushback` marks the answer of the member his
        pushback is aimed at (fix wave G6): their own interval never holds it back, since his
        "are you drunk?" comes seconds after their line; the guild's pace still does."""
        if continuation or reply:
            return True
        n = self._in_window(now)
        if kind not in _NO_PERSON_LIMIT and not pushback:
            interval = REPLY_S if kind is TurnKind.REPLY_HUMAN else self.per_person_s
            last = self._last.get(guid)
            if last is not None and now - last < interval:
                return False
        if kind in _NO_PERSON_LIMIT or kind in _FULL_WINDOW:
            return n < self.guild_per_min
        return n < min(self.social_ceiling, self.guild_per_min)

    def record(self, turn_id: str, guid: int, now: float,
               kind: TurnKind = TurnKind.OPEN) -> None:
        """Count a turn once. URGENT and CALLOUT count guild-wide but leave the speaker's own
        clock alone, so a callout never blocks that person's next reply."""
        if turn_id in self._turn_ids:
            return
        if len(self._turn_ids) >= _MAX_TURN_IDS:
            del self._turn_ids[next(iter(self._turn_ids))]
        self._turn_ids[turn_id] = None
        self._in_window(now)
        self._window.append((now, guid))
        if kind not in _NO_PERSON_LIMIT:
            self._last[guid] = now

    def _roll(self) -> int:
        return self._rng.randint(*CHAIN_CAP)

    def chain_line(self, key: str, from_human: bool) -> None:
        chain = self._chains.get(key)
        if chain is None:
            if len(self._chains) >= _MAX_CHAINS:
                del self._chains[next(iter(self._chains))]
            chain = self._chains[key] = [0, self._roll()]
        if from_human:
            chain[0] = 0
            chain[1] = self._roll()
        else:
            chain[0] += 1

    def chain_open(self, key: str) -> bool:
        chain = self._chains.get(key)
        return chain is None or chain[0] < chain[1]
