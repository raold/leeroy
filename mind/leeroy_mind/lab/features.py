"""Model-free features of one reply to one of his lines: what a reader sees at a glance before
reading a word of it. Each is a number in a small fixed range; `vector` returns them all in
NAMES' order. They are the judge's baseline (lab/judge.py scores them with a small linear model)
and the inputs its model-asked tells are combined with.

- ratio: her words against his, log2((hers + 1) / (his + 1)) clipped to -3..4;
- over: how far past three times his line she went, log2((hers + 1) / (3 his + 1)), 0..4;
- words: her length alone, log2(hers + 1) / 8;
- short_both: he wrote 3 words or fewer and she 6 or fewer (a one-word line met in kind);
- overlap: the share of his content words that are in her reply;
- run: the longest run of his words she has in the same order, up to 8, over 8;
- echo: the live echo guard's rule (guards.review.echoes);
- stock: stock phrases of the guards' list in it (guards/stock_phrases.txt), up to 2, over 2;
- repeat: the largest Jaccard overlap of content words with one of her last 6 replies;
- rerun: the longest run of words she already wrote in one of them, up to 8, over 8;
- dodge: he asked a yes/no, how-many/when or who question and her first sentence holds no
  such answer (lab/rules.answers, the ask read from his words);
- asked: his line asks something;
- asks_back: hers does;
- sentences: her sentences, up to 6, over 6;
- polish: written prose: the share of her sentences that open with a capital, plus a closing
  full stop, a semicolon and a dash, over 4 (people text in lower case and fragments);
- lines: her separate messages past the first, up to 3, over 3;
- rewritten: a guard rewrote the draft before it was sent.

Nothing here logs, stores or returns words."""
import math
import re
from difflib import SequenceMatcher

from leeroy_mind.guards.review import echoes

from . import rules

NAMES = ("ratio", "over", "words", "short_both", "overlap", "run", "echo", "stock", "repeat",
         "rerun", "dodge", "asked", "asks_back", "sentences", "polish", "lines", "rewritten")
EARLIER = 6
RUN_CAP = 8
_SENTENCE = re.compile(r"[^.!?\n]+")
_YESNO = re.compile(
    r"^(?:so |and |but |wait |ok |hey |lol )*(?:do|does|did|is|are|was|were|will|would|can|could|"
    r"should|have|has|had|u|you|ya|wanna|want|got|any|still|really)\b", re.I)
_NUMBER = re.compile(r"\b(?:how (?:many|much|long|old|often|late|early)|what time|when)\b", re.I)
_NAME = re.compile(r"\b(?:who|whose|whos)\b", re.I)


def ask_of(his: str) -> str | None:
    """What his line asks, read from its words: "number", "name", "yesno" or None. Only a line
    with a question mark asks; an open question (what, why, how) has no checkable answer."""
    if "?" not in (his or ""):
        return None
    for part in reversed([p.strip() for p in re.split(r"(?<=[.!?])\s+|\n+", his) if p.strip()]):
        if not part.endswith("?"):
            continue
        if _NUMBER.search(part):
            return "number"
        if _NAME.search(part):
            return "name"
        if _YESNO.match(part):
            return "yesno"
        return None
    return None


def _folded(text: str) -> list[str]:
    return [rules._fold(w) for w in rules.words(text)]


def _run(a: list[str], b: list[str]) -> int:
    if not a or not b:
        return 0
    return max(block.size for block in SequenceMatcher(None, a, b, autojunk=False).get_matching_blocks())


def _polish(reply: str) -> float:
    parts = [p.strip() for p in _SENTENCE.findall(reply) if p.strip()]
    if not parts:
        return 0.0
    caps = sum(1 for p in parts if p[:1].isupper() and not p[:2].isupper()) / len(parts)
    stripped = reply.rstrip()
    return (caps + (stripped.endswith(".") and not stripped.endswith(".."))
            + (";" in reply) + ("—" in reply or " - " in reply)) / 4


def vector(his: str, reply: str, *, earlier=(), rewritten: bool = False) -> dict[str, float]:
    """The features of one reply, {name: value} for every name in NAMES. `earlier`: her earlier
    replies in this conversation, oldest first."""
    his, reply = his or "", reply or ""
    mine, theirs = _folded(reply), _folded(his)
    w, h = len(mine), len(theirs)
    his_content = rules.content(his)
    my_content = rules.content(reply) - his_content
    before = list(earlier)[-EARLIER:]
    jaccard = 0.0
    for line in before:
        other = rules.content(line)
        union = my_content | other
        if union and len(my_content & other) >= 2:
            jaccard = max(jaccard, len(my_content & other) / len(union))
    ask = ask_of(his)
    lines = [p for p in reply.split("\n") if p.strip()]
    return {
        "ratio": max(-3.0, min(4.0, math.log2((w + 1) / (h + 1)))),
        "over": max(0.0, min(4.0, math.log2((w + 1) / (3 * h + 1)))),
        "words": min(1.0, math.log2(w + 1) / 8),
        "short_both": float(h <= 3 and 0 < w <= 6),
        "overlap": len(his_content & rules.content(reply)) / len(his_content) if his_content else 0.0,
        "run": min(RUN_CAP, _run(theirs, mine)) / RUN_CAP,
        "echo": float(bool(reply.strip()) and echoes(reply, his)),
        "stock": min(2, len(_stock(reply))) / 2,
        "repeat": jaccard,
        "rerun": max((min(RUN_CAP, _run(_folded(line), mine)) for line in before), default=0) / RUN_CAP,
        "dodge": float(ask is not None and not rules.answers(reply, ask)),
        "asked": float("?" in his),
        "asks_back": float("?" in reply),
        "sentences": min(6, len([p for p in _SENTENCE.findall(reply) if p.strip()])) / 6,
        "polish": _polish(reply),
        "lines": min(3, max(0, len(lines) - 1)) / 3,
        "rewritten": float(bool(rewritten)),
    }


def _stock(reply: str):
    from leeroy_mind.guards import phrases
    return phrases._stock_spans(phrases.tokens(reply))
