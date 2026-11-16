"""His login's greetings (reactions._greet), so they read like a guild that half noticed
(2026-11-15: every login drew 2-5 greeters, and 23 of 147 greetings opened "there he is").

- How many greet him: none about a third of the time, else one or two (greeters).
- A greeter after the first who has no romance toward him may just type a plain hello, a
  template (TEMPLATES) said through their typing style, never one the guild said lately.
- A phrase rests: a stock greeting (STOCK) anyone used in a greeting within STOCK_COOL_S, this
  login's included, and the opening words (OPENING_WORDS, his names folded together) of a
  greeting of an earlier login within OPENING_COOL_S, are named in the model's instruction
  (avoid_line) and its line is screened for them (repeats), so a greeting that still uses one is
  asked again, then dropped.
Reads utterance rows only; writes nothing."""
import random
import sqlite3
from collections.abc import Iterable, Sequence

from leeroy_mind.text import normalize_words

# (greeters, chance): nobody notices about 30% of his logins (owner's Turing push, 2026-11-15)
GREETERS = ((0, 0.30), (1, 0.45), (2, 0.25))
# A later greeter with no romance toward him types a plain hello this often
TEMPLATE_P = 0.5
# no time of day in them ("evening" at noon reads wrong)
TEMPLATES = ("o/", "hey", "heya", "yo", "hi", "hey {name}", "hi {name}", "yo {name}",
             "o/ {name}", "sup {name}", "wb", "wb {name}")
STOCK = ("there he is", "there she is", "there they are", "look who", "welcome back",
         "man of the hour", "woman of the hour", "finally", "i was starting to think",
         "about time", "miss me", "speak of the devil", "look what the cat dragged in",
         "the legend", "he lives", "she lives", "they live")
STOCK_COOL_S = 3 * 86400.0
OPENING_COOL_S = 86400.0
OPENING_WORDS = 3
HIM = "{him}"  # his names in an opening key
AVOID = "Don't say {phrases}: the guild has greeted {them} that way lately."
AVOID_MAX = 4  # phrases named in the instruction


def greeter_count(rng: random.Random) -> int:
    """How many members greet his login (GREETERS)."""
    roll, acc = rng.random(), 0.0
    for n, p in GREETERS:
        acc += p
        if roll < acc:
            return n
    return GREETERS[-1][0]


def _words(text: str, him: Sequence[str]) -> list[str]:
    names = {w for n in him for w in normalize_words(n).split()}
    return [HIM if w in names else w for w in normalize_words(text).split()]


def opening(text: str, him: Sequence[str]) -> str:
    """The first OPENING_WORDS words of a greeting, casefolded, his names as HIM."""
    return " ".join(_words(text, him)[:OPENING_WORDS])


def stock_in(text: str) -> set[str]:
    """The STOCK phrases a line says."""
    folded = " " + normalize_words(text) + " "
    return {s for s in STOCK if " " + s + " " in folded}


def recent(conn: sqlite3.Connection, now: float) -> list[tuple[float, str]]:
    """(ts, text) of every greeting said within STOCK_COOL_S up to `now`, newest first; []
    when the log cannot be read."""
    try:
        return [(r[0], r[1]) for r in conn.execute(
            "SELECT ts, text FROM utterance WHERE kind='greet' AND dropped IS NULL "
            "AND ts>? AND ts<=? ORDER BY ts DESC, id DESC LIMIT 200",
            (now - STOCK_COOL_S, now))]
    except sqlite3.Error:
        return []


class Cooled:
    """What rests at `now` for a greeting of the login at `login_at`: the stock phrases of the
    greetings in `said`, and the openings of those said before that login."""

    def __init__(self, said: Iterable[tuple[float, str]], now: float, him: Sequence[str], *,
                 login_at: float | None = None):
        self.him = tuple(him)
        self.stock: list[str] = []
        self.openings: list[str] = []
        login_at = now if login_at is None else login_at
        for ts, text in said:
            for s in sorted(stock_in(text)):
                if now - ts < STOCK_COOL_S and s not in self.stock:
                    self.stock.append(s)
            key = opening(text, self.him)
            if (key and ts < login_at and login_at - ts < OPENING_COOL_S
                    and key not in self.openings):
                self.openings.append(key)

    def repeats(self, text: str) -> bool:
        """The line says a resting stock phrase or opens as a recent greeting did."""
        return bool(stock_in(text) & set(self.stock)) or opening(text, self.him) in self.openings

    def avoid_line(self, called: str, them: str) -> str:
        """AVOID naming the resting phrases (stock first), "" when nothing rests."""
        shown = [*self.stock, *(o.replace(HIM, called) for o in self.openings
                                if o.split()[0] != HIM)][:AVOID_MAX]
        if not shown:
            return ""
        quoted = [f'"{s}"' for s in shown]
        phrases = quoted[0] if len(quoted) == 1 else ", ".join(quoted[:-1]) + " or " + quoted[-1]
        return AVOID.format(phrases=phrases, them=them)

    def template(self, rng: random.Random, called: str) -> str | None:
        """A plain hello whose opening does not rest, or None when every one does."""
        name = called.lower()
        free = [t.format(name=name) for t in TEMPLATES
                if opening(t.format(name=name), self.him) not in self.openings]
        return rng.choice(free) if free else None
