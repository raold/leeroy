"""A game line that is only too long is kept, not asked again (self-play audit, 2026-11-17: 42
of 100 whisper turns took a second model call, most after the "long" tell, and the fresh
generation it asked for lost a correct answer, a "yes" and several clean lines).

`fit_answer` decides in code what a game reply over its length becomes. It never cuts anything
before the clause that answers, and it cuts only when it can prove where that clause is (review
of #370: a cut to the first clauses kept the lead-in and dropped the answer whenever the answer
came second or third):

- the answering clause (`answering`): for a question that asks how many, how much, how long or
  when, a clause with a number, a clock time, a weekday or a month; for a yes/no question a
  clause with a yes or a no; for any question a plain "no idea" or a name written with a capital
  inside the clause; for a line of his that asks nothing, or a turn's own subject (an opener's
  topic, an outreach item), a clause that shares a content word with it. A who, what, where,
  which, why or how question answered in other words has no provable answering clause;
- cut: the longest run of whole leading clauses within LONG_SLACK times the turn's words that
  holds the first answering clause and the rest of its sentence, never ending on a dangling
  word or inside a quotation, never under MIN_WORDS words: only what follows the answer's
  sentence goes;
- kept: no such cut and the reply is within KEEP_SLACK times the turn's words: sent as written
  (or cut the same way inside that room);
- retry: longer than that. Only then is the model asked again, and it is given the draft to
  say shorter (SHORTER_HINT), never a fresh start.

A turn that answers nothing and has no subject (idle talk) keeps its leading clauses: there is
no answer to lose. Discord turns are not cut here; they keep their own length rules.

`content_question` is the reading of his line the reply's shape uses (talk/shape.py: a SHORT
line gets room for an answer).

Pure: no clock, no I/O, no logging."""
from __future__ import annotations

import re
from dataclasses import dataclass

from leeroy_mind.talk.asking import QUESTION_WORDS, is_question
from leeroy_mind.talk.select import _words as content_words
from leeroy_mind.talk.style import SENT, _CONJUNCTIONS, _DANGLING, _TOKEN, _bare, _units

LONG_SLACK = 1.25  # a reply this far over the turn's words is long
KEEP_SLACK = 2.0  # one with no provable cut is sent as written up to this
MIN_WORDS = 3  # a cut never leaves fewer words than this
SHORTER_HINT = ('You wrote: "{draft}" Say the same thing shorter, in at most about {words} '
                "words. Keep the answer in it.")
# A content question's words past a question word: "explain the fight", "tell me about ulduar"
_EXPLAIN = re.compile(r"\b(?:explain|describe|tell me (?:about|how|why|what|who|where|when)|"
                      r"remind me|walk me through)\b", re.IGNORECASE)
_WH = re.compile(r"\b(?:" + "|".join(sorted(QUESTION_WORDS)) + r")\b", re.IGNORECASE)
# a question whose answer is a number or a time
_NUMERIC_ASK = re.compile(r"\bhow (?:many|much|long|old|often|far|big)\b|\bwhen\b|\bwhens\b|"
                          r"\bwhat (?:time|level|ilvl|day|date|year)\b", re.IGNORECASE)
_NUMBER = re.compile(
    r"\d|\b(?:two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|"
    r"fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|"
    r"eighty|ninety|hundred|thousand|half|noon|midnight|monday|tuesday|wednesday|thursday|"
    r"friday|saturday|sunday|january|february|march|april|june|july|august|september|october|"
    r"november|december)\b", re.IGNORECASE)
_YES_NO = frozenset({"yes", "yeah", "yea", "yep", "yup", "no", "nah", "nope", "sure", "maybe",
                     "probably", "definitely"})
_UNSURE = re.compile(r"\b(?:idk|dunno|no idea|not sure|no clue|don'?t know|dont remember|"
                     r"don'?t remember|cant remember|can'?t remember|forgot)\b", re.IGNORECASE)
# a name written with a capital inside a clause ("it was Gorak"); never "I", "I'm", "I've"
_NAME = re.compile(r"(?<=[a-z,] )[A-Z][a-z]+(?:'[a-z]+)?\b")
_QUOTE = re.compile(r'["“”]')


@dataclass(frozen=True)
class Fit:
    text: str
    action: str  # "fits", "cut", "kept" or "retry"


def content_question(line: str) -> bool:
    """His line asks for content: a question with who, what, when, where, why, how or which in
    it (the whole line or one clause of it), or a request to explain, describe or tell her about
    something (with or without a question mark). Never a bare "?" or a yes/no question ("you
    around?")."""
    text = " ".join((line or "").split())
    if not text:
        return False
    if _EXPLAIN.search(text):
        return True
    # the whole line, or one clause of it ("nerd question, who ruled the ogres before the war")
    return any(is_question(part) and bool(_WH.search(part))
               for part in (text, *re.split(r"[,.;:!]\s+", text)))


def _shares(words: list[str], other: str) -> bool:
    theirs = set(content_words(other))
    return bool(theirs & set(content_words(" ".join(words))))


def answering(words: list[str], his: str = "", subject: str = "") -> bool:
    """The clause `words` provably answers (module docstring). `his`: the line of his the turn
    answers; `subject`: what a turn that answers nothing is about (an opener's topic, an outreach
    item). With neither there is nothing to answer: True."""
    text = " ".join(words)
    bare = [b for b in (_bare(w) for w in words) if b]
    if not bare:
        return False
    his = " ".join((his or "").split())
    if not his:
        return _shares(words, subject) if subject.strip() else True
    asked = is_question(his) or content_question(his)
    if not asked:
        return _shares(words, his) or bool(_NUMBER.search(text))
    if _UNSURE.search(text) or _NAME.search(text):
        return True
    if content_question(his):
        return bool(_NUMERIC_ASK.search(his)) and bool(_NUMBER.search(text))
    return any(b in _YES_NO for b in bare)


def carries(words: list[str], his: str = "", subject: str = "") -> bool:
    """Some clause of `words` provably answers (`answering`): never True for a question just
    because the words are not filler."""
    units = _clauses(" ".join(words))
    return any(answering(ws, his, subject) for ws, _ in units)


def _clauses(text: str):
    """style's clauses, with a break inside an open quotation taken away (a cut there would
    leave the quote unclosed)."""
    out = []
    for ws, strength in _units(_TOKEN.findall(text)):
        if out and len(_QUOTE.findall(" ".join(out[-1][0]))) % 2:
            out[-1] = (out[-1][0] + ws, strength)
        else:
            out.append((list(ws), strength))
    return out


def _n(units) -> int:
    return sum(len(ws) for ws, _ in units)


def _join(units) -> str:
    return " ".join(w for ws, _ in units for w in ws).rstrip(",;:- ")


def _cut(units, first: int, room: int, sentence: bool = True) -> str:
    """The longest run of whole leading clauses within `room` words that holds clause `first`
    (the answering one) and ends cleanly; "" when there is none. `sentence`: the answering
    clause goes with the rest of its sentence (its reason, its "because")."""
    best = ""
    whole = (next((j for j in range(first, len(units)) if units[j][1] == SENT), len(units) - 1)
             if sentence else first)
    for k in range(whole + 1, len(units)):
        head = units[:k]
        if _n(head) > room:
            break
        last = head[-1][0]
        if units[k - 1][1] != SENT and (_bare(last[-1]) in _DANGLING or (
                len(last) <= 3 and _bare(last[0]) in _CONJUNCTIONS)):
            continue  # never ends on "but only" or "the" (a sentence's own end is whole)
        if len(_QUOTE.findall(_join(head))) % 2 or _n(head) < MIN_WORDS:
            continue
        best = _join(head)
    return best


def fit_answer(text: str, limit: int, his: str = "", *, subject: str = "") -> Fit:
    """What a game reply of `text` becomes on a turn of `limit` words (module docstring).
    `his`: the line of his it answers ("" none); `subject`: what the turn is about when it
    answers nothing."""
    words = text.split()
    if limit <= 0 or len(words) <= int(LONG_SLACK * limit):
        return Fit(text, "fits")
    units = _clauses(" ".join(words))
    first = next((i for i, (ws, _) in enumerate(units) if answering(ws, his, subject)), None)
    within = len(words) <= int(KEEP_SLACK * limit)
    if first is not None:
        answers = bool(his.strip() or subject.strip())  # else there is no answer to lose
        cut = _cut(units, first, int(LONG_SLACK * limit), answers)
        if cut:
            return Fit(cut, "cut")
        if not within:
            cut = _cut(units, first, int(KEEP_SLACK * limit), answers)
            if cut:
                return Fit(cut, "kept")
    return Fit(text, "kept" if within else "retry")


__all__ = ["Fit", "KEEP_SLACK", "LONG_SLACK", "MIN_WORDS", "SHORTER_HINT", "answering",
           "carries", "content_question", "fit_answer"]
