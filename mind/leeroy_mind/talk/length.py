"""A game line that is only too long is kept, not asked again (self-play audit, 2026-11-17: 42
of 100 whisper turns took a second model call, most after the "long" tell, and the fresh
generation it asked for lost a correct answer, a "yes" and several clean lines).

The owner's rule is absolute: a cut that removes the answer is never acceptable. Three
reviews of #370 showed that no test of tokens can say where an answer is (it came second or
third; the lead-in held a number, a clock time or a quoted "yes"; the answer held no token at
all: "after the officers' meeting", "a couple", "can't make it"). So (controller ruling):

- **his line asks anything** (a question mark, a question, one buried in his line with no
  question mark, a request to explain): never cut.
  The reply is "kept" as written, whatever its length; the pipeline sends it when it fits her
  turn's game lines by whole clauses (`split_lines`), else asks again with her own draft
  (SHORTER_HINT), and when that is still too long lays it on up to three lines or sends its
  tail (`tail_lines`), never its head. In a game whisper to him a kept answer that fits but
  runs past KEEP_SLACK times her turn's words is asked again once (USUAL_HINT: her usual
  length, the answer kept); the retry is sent as this rule reads it, and if it fails the first
  draft goes: it costs a call, never the answer;
- his line asks nothing, or the turn answers nothing (idle talk): her leading whole clauses
  that fit, as many as fit;
- the turn has a subject of its own (an opener's topic, an outreach item's words): only after
  the sentence of the last clause that shares a content word with it.

The pipeline reads this module only with `[talk] coherence_length` on (default off): with it
off, game lines keep main's length handling whatever `[talk] coherence` says.

A cut never ends on a dangling word or inside a quotation and never leaves fewer than
MIN_WORDS words (or the low end of her voice sheet's range). With no cut a line that asks
nothing is kept as written within KEEP_SLACK times the turn's words; past that the model is
asked again with her own draft, never a fresh start.

`split_lines` says whether a text fits the game lines her turn has, by whole clauses within
the bridge's bytes: a text that does not is never "kept as written" (the styling would trim its
tail word by word, and the answer with it). Discord turns are not cut here.

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
LINE_BYTES = 230  # a game line's words before her typing (the bridge takes 255 bytes)
SHORTER_HINT = ('You wrote: "{draft}" Say the same thing shorter, in at most about {words} '
                "words. Keep the answer in it.")
# a kept answer in a whisper far over her usual length (blind sandbox test, 2026-11-20: whisper
# lines grew from a median 8 words to 13, 26 of 127 over 25 words, and invented more game facts)
USUAL_DRAFT = 'You wrote: "{draft}"'
USUAL_HINT = "Say it in your usual length, about {words} words, and keep the answer in it."
# A content question's words past a question word: "explain the fight", "tell me about ulduar"
_EXPLAIN = re.compile(r"\b(?:explain|describe|tell me (?:about|how|why|what|who|where|when)|"
                      r"remind me|walk me through)\b", re.IGNORECASE)
_WH = re.compile(r"\b(?:" + "|".join(sorted(QUESTION_WORDS)) + r")\b", re.IGNORECASE)
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
    # the whole line, or one clause of it ("nerd question, who ruled the ogres before the war"),
    # a line of a post that has several among them
    parts = [p for x in (line or "").splitlines() for p in re.split(r"[,.;:!]\s+", x.strip())]
    return any(is_question(part) and bool(_WH.search(part)) for part in (text, *parts))


def asks(his: str) -> bool:
    """His line asks her anything: a question mark anywhere in it, a question (one buried in
    the line with no question mark too: "...so did you eat yet", "you ever been out west";
    asking.buried_question), or a request to explain. The reply to it is never cut."""
    if not (his or "").strip():
        return False  # his own text below: its line breaks end clauses
    return "?" in his or is_question(his, buried=True) or content_question(his)


def _shares(words: list[str], other: str) -> bool:
    return bool(set(content_words(other)) & set(content_words(" ".join(words))))


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


def _cut(units, last: int, room: int, sentence: bool = True, floor: int = MIN_WORDS) -> str:
    """The longest run of whole leading clauses within `room` words that holds clause `last`
    and ends cleanly; "" when there is none. `sentence`: clause `last` goes with the rest of
    its sentence (its reason, its "because")."""
    best = ""
    whole = (next((j for j in range(last, len(units)) if units[j][1] == SENT), len(units) - 1)
             if sentence else last)
    for k in range(whole + 1, len(units)):
        head = units[:k]
        if _n(head) > room:
            break
        tail = head[-1][0]
        if units[k - 1][1] != SENT and (_bare(tail[-1]) in _DANGLING or (
                len(tail) <= 3 and _bare(tail[0]) in _CONJUNCTIONS)):
            continue  # never ends on "but only" or "the" (a sentence's own end is whole)
        if len(_QUOTE.findall(_join(head))) % 2 or _n(head) < max(MIN_WORDS, floor):
            continue
        best = _join(head)
    return best


def fit_answer(text: str, limit: int, his: str = "", *, subject: str = "",
               floor: int = 0) -> Fit:
    """What a game reply of `text` becomes on a turn of `limit` words (module docstring).
    `his`: the line of his it answers ("" none); `subject`: what the turn is about when it
    answers nothing; `floor`: the fewest words a cut may leave (the low end of the line her
    voice sheet names; MIN_WORDS at least), so she is never cut below her own shape. Nothing
    is ever added: a line is never padded. An answer to a line of his that asks anything is
    never cut ("kept": the caller sends it when it fits her lines, else asks again)."""
    words = text.split()
    if limit <= 0 or len(words) <= int(LONG_SLACK * limit):
        return Fit(text, "fits")
    if asks(his):
        return Fit(text, "kept")
    units = _clauses(" ".join(words))
    within = len(words) <= int(KEEP_SLACK * limit)
    his = " ".join((his or "").split())
    if not his and subject.strip():
        # only after the last clause that names the turn's subject
        held = [i for i, (ws, _) in enumerate(units) if _shares(ws, subject)]
        last, sentence = (held[-1] if held else None), True
    else:  # nothing to prove: her leading clauses, as many as fit
        last, sentence = 0, False
    if last is not None:
        for room in (int(LONG_SLACK * limit), *(() if within else (int(KEEP_SLACK * limit),))):
            cut = _cut(units, last, room, sentence, floor)
            if cut:
                return Fit(cut, "cut" if room == int(LONG_SLACK * limit) else "kept")
    return Fit(text, "kept" if within else "retry")


def split_lines(text: str, lines: int, max_bytes: int = LINE_BYTES) -> list[str] | None:
    """`text` on at most `lines` game lines by whole clauses, each within `max_bytes` bytes and
    nothing dropped (the fewest lines, the break nearest the middle); None when it does not fit.
    A kept draft that does not fit is never sent as written: the styling would trim its tail
    word by word at the bridge's 255 bytes (review of #370)."""
    flat = " ".join(text.split())
    if not flat:
        return None
    units = _clauses(flat)

    def size(group) -> int:
        return len(" ".join(w for ws, _ in group for w in ws).encode("utf-8"))

    if size(units) <= max_bytes:
        return [flat]
    if lines < 2:
        return None
    total = _n(units)
    best = None
    for k in range(1, len(units)):
        if lines == 2:
            groups = [units[:k], units[k:]]
            if all(size(g) <= max_bytes for g in groups):
                key = abs(_n(units[:k]) - total / 2)
                if best is None or key < best[0]:
                    best = (key, groups)
            continue
        for j in range(k + 1, len(units)):
            groups = [units[:k], units[k:j], units[j:]]
            if all(size(g) <= max_bytes for g in groups):
                key = max(map(_n, groups))
                if best is None or key < best[0]:
                    best = (key, groups)
    if best is None:
        return None
    return [" ".join(w for ws, _ in g for w in ws) for g in best[1]]


def tail_lines(text: str, lines: int, max_bytes: int = LINE_BYTES) -> list[str] | None:
    """The end of `text` that fits `lines` game lines by whole clauses (`split_lines`): its
    last sentences, as many leading clauses dropped as it takes and no more. For an owed answer
    still too long after it was asked again: the answer is more often late than early in a
    draft that ran long, so the tail goes, never the head. None when not even its last clause
    fits a line."""
    units = _clauses(" ".join(text.split()))
    for k in range(len(units)):
        if k and units[k - 1][1] != SENT and any(
                units[j][1] == SENT for j in range(k, len(units) - 1)):
            continue  # start at a sentence while one is left to start at
        tail = " ".join(w for ws, _ in units[k:] for w in ws)
        parts = split_lines(tail, lines, max_bytes)
        if parts is not None:
            return parts
    return None


__all__ = ["Fit", "KEEP_SLACK", "LINE_BYTES", "LONG_SLACK", "MIN_WORDS", "SHORTER_HINT",
           "USUAL_DRAFT", "USUAL_HINT", "asks", "content_question", "fit_answer", "split_lines",
           "tail_lines"]
