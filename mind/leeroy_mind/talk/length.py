"""A draft that is only too long is kept, not asked again (self-play audit, 2026-11-17: 42 of 100
whisper turns took a second model call, most after the "long" tell, and the fresh generation it
asked for lost a correct answer, a "yes" and several clean lines).

`fit_answer` decides in code what a reply over its length becomes:

- cut: its leading whole clauses up to the allowed length (LONG_SLACK times the turn's words),
  ending at a sentence end when that keeps two thirds of what fits, never on a dangling word, a
  filler opener ("lol") giving way when it must, as long as what is left still carries the answer
  (`carries`: more than filler, and for a question that asks for content, who, what, when,
  where, why, how, which or "explain", a word of her own that is not his, a number, or a plain
  "no idea");
- kept: no such cut, but the reply is within KEEP_SLACK times the turn's words: sent as written
  (or cut at the last boundary inside that room that carries the answer);
- retry: longer than that with no cut that keeps the answer. Only then is the model asked
  again, and it is given the draft to say shorter (SHORTER_HINT), never a fresh start.

`content_question` is the same reading of his line for the reply's shape (talk/shape.py: a SHORT
line gets room for an answer).

Pure: no clock, no I/O, no logging."""
from __future__ import annotations

import re
from dataclasses import dataclass

from leeroy_mind.talk.asking import QUESTION_WORDS, is_question
from leeroy_mind.talk.style import (SENT, _CONJUNCTIONS, _DANGLING, _FILLER, _TOKEN,
                                    _bare, _is_filler, _units)

LONG_SLACK = 1.25  # a reply this far over the turn's words is long
KEEP_SLACK = 2.0  # one with no clean cut is sent as written up to this
SHORTER_HINT = ('You wrote: "{draft}" Say the same thing shorter, in at most about {words} '
                "words. Keep the answer in it.")
# A content question's words past a question word: "explain the fight", "tell me about ulduar"
_EXPLAIN = re.compile(r"\b(?:explain|describe|tell me (?:about|how|why|what|who|where|when)|"
                      r"remind me|walk me through)\b", re.IGNORECASE)
_WH = re.compile(r"\b(?:" + "|".join(sorted(QUESTION_WORDS)) + r")\b", re.IGNORECASE)
_NUMBER = re.compile(r"\d")
_YES_NO = frozenset({"yes", "yeah", "yea", "ya", "yep", "yup", "no", "nah", "nope", "sure",
                     "never", "always", "maybe", "probably", "ok", "okay", "kk"})
_UNSURE = re.compile(r"\b(?:idk|dunno|no idea|not sure|no clue|don'?t know|dont remember|"
                     r"don'?t remember|cant remember|can'?t remember|forgot)\b", re.IGNORECASE)
# Little words that carry no answer on their own (beside the style's filler openers)
_LITTLE = frozenset("""a an the of to in on at for with from by into about and or but so if when
than as is was are were be been am it its it's that thats that's this these those i im i'm you
u ur your youre you're we he she they me my mine our their his her them him do does did have
has had will would can could should just really very like not no yes one thing think thought
know mean guess well right here there then now too also got get its""".split())


@dataclass(frozen=True)
class Fit:
    text: str
    action: str  # "fits", "cut", "kept" or "retry"


def content_question(line: str) -> bool:
    """His line asks for content: a question with who, what, when, where, why, how or which in
    it, or a request to explain, describe or tell her about something. Never a bare "?" or a
    yes/no question ("you around?")."""
    text = " ".join((line or "").split())
    if not text:
        return False
    if _EXPLAIN.search(text):
        return True
    return is_question(text) and bool(_WH.search(text))


def carries(words: list[str], his: str = "") -> bool:
    """`words` (a cut of her draft) still answer him: more than filler; on a yes/no question a
    yes or no is enough; on a content question (content_question) they hold a word of her own
    that his line does not (three letters or more, not a little word), a number, or a plain
    "no idea"."""
    bare = [b for b in (_bare(w) for w in words) if b]
    text = " ".join(words)
    if not bare:
        return False
    asked = bool(his) and is_question(his)
    if all(b in _FILLER for b in bare):
        return asked and not content_question(his) and any(b in _YES_NO for b in bare)
    if not asked or not content_question(his):
        return True
    if _NUMBER.search(text) or _UNSURE.search(text):
        return True
    theirs = {_bare(w) for w in his.split()}
    return any(len(b) >= 3 and b not in _LITTLE and b not in _FILLER and b not in theirs
               for b in bare)


def _join(units) -> str:
    return " ".join(w for ws, _ in units for w in ws).rstrip(",;:- ")


def _n(units) -> int:
    return sum(len(ws) for ws, _ in units)


def _cut(units, room: int, his: str) -> str:
    """The longest leading run of whole clauses within `room` words that carries the answer,
    or the longest that ends a sentence when that keeps two thirds as much (the styling's own
    rule); "" when there is none."""
    longest, sentence = "", ""
    for k in range(1, len(units)):
        head = units[:k]
        if _n(head) > room:
            break
        last = head[-1][0]
        if _bare(last[-1]) in _DANGLING or (len(last) <= 3 and _bare(last[0]) in _CONJUNCTIONS):
            continue  # never ends on "but only" or "the"
        if carries([w for ws, _ in head for w in ws], his):
            longest = _join(head)
            if units[k - 1][1] == SENT:
                sentence = longest
    if sentence and 3 * len(sentence.split()) >= 2 * len(longest.split()):
        return sentence
    return longest


def fit_answer(text: str, limit: int, his: str = "") -> Fit:
    """What a reply of `text` becomes on a turn of `limit` words (module docstring). `his`: the
    line of his it answers ("" none)."""
    words = text.split()
    if limit <= 0 or len(words) <= int(LONG_SLACK * limit):
        return Fit(text, "fits")
    units = _units(_TOKEN.findall(" ".join(words)))
    bare = list(units)  # without a filler opener, which is the first to give way
    while len(bare) > 1 and _is_filler(bare[0][0]):
        bare = bare[1:]
    within = len(words) <= int(KEEP_SLACK * limit)
    for room in (int(LONG_SLACK * limit), int(KEEP_SLACK * limit)):
        cut = _cut(units, room, his) or _cut(bare, room, his)
        # a cut that throws away most of a reply she could send as written is no gain
        if cut and not (within and 2 * len(cut.split()) < len(words)):
            return Fit(cut, "cut" if room <= int(LONG_SLACK * limit) else "kept")
        if within:
            return Fit(text, "kept")
    return Fit(text, "retry")


__all__ = ["Fit", "KEEP_SLACK", "LONG_SLACK", "SHORTER_HINT", "carries", "content_question",
           "fit_answer"]
