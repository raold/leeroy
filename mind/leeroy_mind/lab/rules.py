"""The battery's rule checks, model-free, on one of her answers (every line she said to one of his
lines, joined). Each is a flag, 1 when the rule is broken:

- silent: she said nothing to his line within the wait;
- unanswered: his line asked something (the script's `ask`) and her first sentence holds no
  answer: a yes or a no (or a clear maybe) for yesno, a number or a time for number, a name (one
  of the script's words, or a capitalised word past her first) for name, one of the script's
  words for noun (a direct address of the asked noun);
- wordy: far more words than his line (more than 3 x his + 15);
- repeat: the same subject as one of her earlier answers in this conversation: 3 or more content
  words shared with it (not in his current line) and a Jaccard overlap of 0.35 or more;
- rewritten: a guard rewrote the draft before it was sent;
- echo: she said his line back (guards.review.echoes, the live echo guard's rule);
- stock: a phrase from the guards' curated stock list (guards/stock_phrases.txt).

Nothing here logs, stores or returns her words: only flags and counts."""
import re

from leeroy_mind.guards import phrases
from leeroy_mind.guards.review import echoes

CODES = ("silent", "unanswered", "wordy", "repeat", "rewritten", "echo", "stock")
YES_NO = frozenset("""
yes yeah yea yep yup ya yah ye yess yesss sure ofc course definitely def absolutely totally
certainly obviously no nah nope naw nop not never maybe perhaps probably prob possibly kinda
sorta idk dunno depends mhm uhhuh dont doesnt didnt isnt arent wasnt cant wont wouldnt aint
havent hasnt nope
""".split())
NUMBER_WORDS = frozenset("""
one two three four five six seven eight nine ten eleven twelve fifteen twenty thirty forty fifty
sixty hundred few couple dozen half noon midnight morning afternoon evening night tonight
weekend weekends weekdays am pm oclock hour hours minute minutes year years month months week
weeks
""".split())
_I = frozenset({"i", "im", "ive", "id", "ill"})
_SENTENCE = re.compile(r"[.!?\n]+")
_WORD = re.compile(r"[A-Za-z0-9']+")
FIRST_WORDS = 20   # a first "sentence" without punctuation is its first 20 words
WORDY_FACTOR, WORDY_SLACK = 3, 15
REPEAT_SHARED, REPEAT_JACCARD = 3, 0.35


def words(text: str) -> list[str]:
    return _WORD.findall(text or "")


def _fold(word: str) -> str:
    return word.replace("'", "").casefold()


def first_sentence(text: str) -> str:
    for part in _SENTENCE.split(text or ""):
        if part.strip():
            return " ".join(part.split()[:FIRST_WORDS])
    return ""


def answers(reply: str, ask: str | None, nouns: tuple[str, ...] = ()) -> bool:
    """Her first sentence answers a line that asked `ask` (always True for no ask)."""
    if ask is None:
        return True
    raw = words(first_sentence(reply))
    folded = [_fold(w) for w in raw]
    if not folded:
        return False
    named = any(any(f == n or (len(n) >= 4 and f.startswith(n)) for n in nouns) for f in folded)
    if ask == "yesno":
        return any(f in YES_NO for f in folded) or named
    if ask == "number":
        return any(f.isdigit() or any(c.isdigit() for c in f) or f in NUMBER_WORDS
                   for f in folded) or named
    if ask == "name":
        proper = any(w[:1].isupper() and (i > 0 or len(w) >= 2 and w.isupper())
                     and _fold(w) not in _I for i, w in enumerate(raw))
        return named or proper
    return named  # noun: a direct address of what he asked about


def content(text: str) -> set[str]:
    return {f for f in (_fold(w) for w in words(text))
            if len(f) > 3 and f not in phrases.STOP and not f.isdigit()}


def repeats(reply: str, earlier: list[str], his: str) -> bool:
    mine = content(reply) - content(his)
    for before in earlier:
        theirs = content(before)
        shared = mine & theirs
        union = mine | theirs
        if len(shared) >= REPEAT_SHARED and union and len(shared) / len(union) >= REPEAT_JACCARD:
            return True
    return False


def stock_in(reply: str) -> bool:
    return bool(phrases._stock_spans(phrases.tokens(reply or "")))


def check(his: str, reply: str, *, ask: str | None = None, nouns: tuple[str, ...] = (),
          earlier: list[str] = (), rewritten: bool = False) -> dict[str, int]:
    """The flags for one answer: {code: 0|1} for every code in CODES. `reply` is "" when she
    said nothing; `earlier` her answers to his earlier lines in this conversation."""
    if not reply.strip():
        return {c: int(c == "silent" or (c == "unanswered" and ask is not None)) for c in CODES}
    mine, theirs = len(words(reply)), len(words(his))
    return {
        "silent": 0,
        "unanswered": int(not answers(reply, ask, nouns)),
        "wordy": int(mine > WORDY_FACTOR * theirs + WORDY_SLACK),
        "repeat": int(repeats(reply, list(earlier), his)),
        "rewritten": int(bool(rewritten)),
        "echo": int(echoes(reply, his)),
        "stock": int(stock_in(reply)),
    }


def tally(rows: list[dict]) -> dict[str, int]:
    """Flags summed over rows of check() results."""
    return {c: sum(int(r.get(c, 0)) for r in rows) for c in CODES}
