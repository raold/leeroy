"""A near-copy of her own earlier message (self-play audit, 2026-11-17: back from a "brb" she
sent her previous message again, almost word for word; it sat in the prompt twice and a retry
copied the line it was shown, and no check named it: each message of her burst was compared
alone against the whole earlier message, so no pair of lines looked alike).

`copies(text, earlier)`: OVERLAP of the draft's words (SHORT_OVERLAP for one under LONG words)
are words of the earlier message (counted with repeats) and BIGRAMS of its word pairs are pairs
of it, for a draft of WORDS words or more:
a message said again, whole or as one piece of it. A draft that holds the whole earlier message
and little more counts too. Short lines never count ("lol ok", "night x"), and nothing counts
when he asked her to say it again (`asked_again`).

Pure: no I/O, no logging."""
from __future__ import annotations

import re
from collections import Counter

OVERLAP = 0.8
SHORT_OVERLAP = 0.9  # a draft under LONG words: one changed word is not a new message
LONG = 12
BIGRAMS = 0.6
WORDS = 6
LINES = 4  # of her last messages in the conversation, each alone and all together
GROWN = 1.5  # a draft up to this many times the earlier message that holds it whole
_WORD = re.compile(r"[a-z0-9]+")
_ASKED_AGAIN = re.compile(
    r"\b(?:again|repeat|say that|what did (?:you|u) (?:say|mean)|didn'?t (?:get|see|catch|read)"
    r"|missed (?:that|it)|come again|one more time|resend|send (?:it|that) again)\b",
    re.IGNORECASE)


def _words(text: str) -> list[str]:
    return _WORD.findall((text or "").casefold().replace("’", "").replace("'", ""))


def _share(a: list[str], b: list[str]) -> tuple[float, float]:
    """How much of `a` is in `b`: its words (with repeats) and its word pairs, each 0-1."""
    pool = Counter(b)
    words = sum(min(n, pool[w]) for w, n in Counter(a).items()) / len(a)
    pairs = list(zip(a, a[1:]))
    grams = set(zip(b, b[1:]))
    return words, (sum(p in grams for p in pairs) / len(pairs) if pairs else 0.0)


def copies(text: str, earlier: str) -> bool:
    """`text` says `earlier` again (the module docstring)."""
    a, b = _words(text), _words(earlier)
    if len(a) < WORDS or len(b) < WORDS:
        return False
    words, pairs = _share(a, b)
    if words >= (OVERLAP if len(a) >= LONG else SHORT_OVERLAP) and pairs >= BIGRAMS:
        return True
    if len(a) > GROWN * len(b):
        return False
    words, pairs = _share(b, a)
    return words >= (OVERLAP if len(b) >= LONG else SHORT_OVERLAP) and pairs >= BIGRAMS


def copies_any(text: str, earlier: list[str]) -> bool:
    """`text` says again one of her last LINES messages (`earlier`, oldest first), or all of
    them together (a burst of hers sent again as one message)."""
    last = [x for x in earlier if x][-LINES:]
    if any(copies(text, x) for x in last):
        return True
    return len(last) > 1 and copies(text, " ".join(last[-2:]))


def asked_again(his: str) -> bool:
    """He asked her to say it again: her saying it again is the answer."""
    return bool(his) and bool(_ASKED_AGAIN.search(his))
