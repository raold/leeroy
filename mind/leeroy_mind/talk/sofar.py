"""What a private conversation with him has established so far, kept by code (self-play audit,
2026-11-17: 43 of 67 DM replies forgot or contradicted the conversation they were in; his news
from six turns back had left the Chat window and nothing else in the prompt carried it).

From the lines of one conversation between a member and him (their whispers and her Discord DM
are one conversation: memory/AUDIENCES.md), `block` builds a few short lines for the prompt's
"So far between you two" section (PromptParts.so_far):

- his news: lines of his that state something about himself, a first-person statement with a
  person, place, time, health, work or plan word (`his_facts`: at most FACTS, the newest wins,
  and a later statement on the same subject replaces the earlier one);
- what he asked and she has not answered yet, other than the message this turn answers
  (`open_questions`: he wrote several messages in a row and the instruction quotes the last);
- where she said she is and what state she said she is in, from her own lines (`her_place`,
  `her_state`; the newest wins, so "at the studio" then "home now" is home). Her day plan's
  place is the prompt's "Right now" line and is not said twice;
- plans between them: a line of either with a time and a plan word (`plans`).

`her_said` is her own stated place and state as plain strings, for a caller that wants what
she has claimed so far in this conversation (the beat).

A conversation is the newest run of lines with no gap over IDLE_S (3 h) between two lines and
its last line no older than that (`conversation`). SoFar keeps each member's block in memory,
keyed by the conversation's newest line, and forgets one idle past IDLE_S; after a restart it
is built again from the stored lines. Every line is bounded (LINE_WORDS) and the conversation's
lines together (MAX_TOKENS estimated tokens, 4 characters a token; the heading and the caller's
lead, one standing rule, are not the conversation's): what does not fit is left out, the oldest
of his news first.

Rule-based: no model call, no I/O beyond the rows handed in, no logging of any text. The
patterns are non-explicit on purpose."""
from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from leeroy_mind.talk.asking import is_question
from leeroy_mind.text import estimate_tokens

HEAD = ("So far between you two (keep to it, never contradict it; if he asks about something "
        "he told you that is not here or in the chat, say you don't remember, never guess):")
IDLE_S = 3 * 3600.0
MAX_TOKENS = 120
FACTS = 6
LINE_WORDS = 12
OPEN_QUESTIONS = 2
PLANS = 2
MAX_LINES = 200  # of a conversation, the newest

_APOSTROPHES = str.maketrans({"’": "'", "ʼ": "'"})
_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+|\s+/\s+")
_FIRST_PERSON = re.compile(r"\b(?:i|i'm|im|i've|ive|i'll|ill|i'd|my|me|mine|we|we're|our|ours)\b",
                           re.IGNORECASE)
_PEOPLE = (r"dad|father|mum|mom|mother|brother|sister|wife|husband|girlfriend|boyfriend|partner"
           r"|ex|son|daughter|kids?|baby|boss|manager|coworker|colleague|friend|mate|buddy"
           r"|roommate|flatmate|landlord|neighbou?r|grandma|grandpa|gran|nan|uncle|aunt|cousin"
           r"|dog|cat|puppy|kitten|vet|doctor|dentist")
_PLACES = (r"hospital|work|office|home|house|flat|apartment|gym|airport|school|uni|college"
           r"|shop|store|pub|bar|garage|station|hotel|beach|town|city|church|clinic|site")
_TIMES = (r"today|tonight|tomorrow|yesterday|this (?:morning|afternoon|evening|weekend|week)"
          r"|last (?:night|week|weekend)|next (?:week|weekend|month)|monday|tuesday|wednesday"
          r"|thursday|friday|saturday|sunday|at \d{1,2}(?::\d\d)?\s?(?:am|pm)?")
_HEALTH = (r"sick|ill|flu|cold|covid|fever|headache|migraine|hurt|injured|surgery|operation"
           r"|heart|tired|exhausted|hungover|sleep|slept|insomnia|pain|broke my|sprained"
           r"|appointment|test results?|scan|diagnos\w+")
_WORK = (r"job|shift|overtime|meeting|deadline|interview|promotion|raise|fired|laid off|quit"
         r"|client|project|exam|class|lecture|day off|holiday|vacation|payday")
_PLAN = (r"going to|gonna|have to|gotta|got to|need to|heading|flying|driving|leaving|moving"
         r"|visiting|planning|booked|will be|i'll be|ill be|about to|supposed to|might")
_SUBJECT = re.compile(
    rf"\b(?:(?P<person>{_PEOPLE})|(?P<place>{_PLACES})|(?P<health>{_HEALTH})"
    rf"|(?P<work>{_WORK})|(?P<time>{_TIMES})|(?P<plan>{_PLAN}))\b", re.IGNORECASE)
# a plan between the two: a time and a word for doing something together or next
_WHEN = re.compile(rf"\b(?:{_TIMES}|later|after (?:work|raid|dinner|this))\b", re.IGNORECASE)
_TOGETHER = re.compile(
    r"\b(?:talk|call|speak|chat|catch up|see (?:you|ya|u)|play|raid|run|queue|watch|let's|lets"
    r"|we'll|we will|we can|we could|i'll|ill|deal|it's a date|its a date|promise|plan)\b",
    re.IGNORECASE)
# where she says she is: "im at the studio till ten", "back home now", "still in bed"
_HER_PLACE = re.compile(
    r"\b(?:i'm|im|i am|i'm just|im just|still|just got|got|back)\s+"
    r"(?:(?:still|just|back|currently|already|stuck|sat|sitting|lying|laying|curled up|standing"
    r"|working|hiding|waiting)\s+){0,2}"
    r"(?P<where>(?:home\b|(?:at|in|on|behind|outside|by)\s+(?:the|my|a|an|our|work|home|bed)\b)"
    r"[^.,!?;\n]{0,40})", re.IGNORECASE)
_NOT_A_PLACE = re.compile(
    r"^(?:in|on|at)\s+(?:the|my|a|an|our)\s+(?:mood|way|middle of|bit|sec|second|minute|moment"
    r"|end|least|same|edge|verge|fence|phone with|game|raid|queue|group|guild|dungeon|bg)\b"
    r"|^(?:in|on)\s+(?:love|trouble|fire|it|top)\b", re.IGNORECASE)
# what she says she has on or off (clothing words only)
_HER_STATE = re.compile(
    r"\b(?:(?:my\s+)?(?:shirt|top|jumper|hoodie|jeans|trousers|pants|shorts|dress|skirt|socks"
    r"|shoes|coat|jacket|pyjamas|pajamas|pjs|uniform|apron)(?:'s|\s+is|\s+are|s)?\s+(?:off|on|gone)"
    r"|(?:i'm|im|i am)\s+(?:wearing|in (?:just\s+)?my)\s+[^.,!?;\n]{2,40}"
    r"|(?:just\s+)?(?:got|getting) (?:changed|dressed|out of the shower)"
    r"|(?:i'm|im|i am)\s+(?:in the (?:bath|shower)|half asleep|wide awake|a bit drunk|tipsy"
    r"|on my break|on a break|done for the day|off (?:work|shift)|closing up|closed up))\b"
    r"|\b(?:i\s+)?(?:just\s+)?closed up\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class Line:
    """One line of the conversation: `his` (he wrote it, else she did), oldest first."""
    id: int
    ts: float
    his: bool
    text: str


def conversation(lines: Sequence[Line], now: float) -> list[Line]:
    """The newest run of `lines` (oldest first) with no gap over IDLE_S between two lines and
    its last line no older than IDLE_S: the conversation going on now; [] for none."""
    out: list[Line] = []
    last = now
    for line in reversed(lines[-MAX_LINES:]):
        if last - line.ts > IDLE_S:
            break
        out.append(line)
        last = line.ts
    return out[::-1]


def _clean(text: str) -> str:
    return " ".join((text or "").translate(_APOSTROPHES).split())


def _clip(text: str, words: int = LINE_WORDS) -> str:
    parts = text.split()
    return text if len(parts) <= words else " ".join(parts[:words]) + "..."


def _sentences(text: str) -> list[str]:
    return [s.strip(" /") for s in _SENTENCE.split(_clean(text)) if s.strip(" /")]


def his_facts(lines: Sequence[Line], *, skip: int = 0) -> list[tuple[int, str]]:
    """(line id, clipped sentence) for each thing he said about himself, oldest first: a
    sentence of his that is no question, speaks in the first person and names a person, place,
    time, health, work or plan word. A later one on the same subject (the same word) replaces
    the earlier; at most FACTS, the newest. `skip`: the line this turn answers (the instruction
    carries it)."""
    found: dict[str, tuple[int, str]] = {}
    for line in lines:
        if not line.his or line.id == skip:
            continue
        for sentence in _sentences(line.text):
            if is_question(sentence) or not _FIRST_PERSON.search(sentence):
                continue
            m = _SUBJECT.search(sentence)
            if m is None or len(sentence.split()) < 3:
                continue
            key = (m.lastgroup or "") + ":" + m.group(0).casefold()
            found.pop(key, None)  # the newer statement moves to the end
            found[key] = (line.id, _clip(sentence))
    seen, out = set(), []
    for item in list(found.values())[::-1]:  # newest first; one sentence once
        if item[1] in seen:
            continue
        seen.add(item[1])
        out.append(item)
        if len(out) == FACTS:
            break
    return out[::-1]


def open_questions(lines: Sequence[Line], *, skip: int = 0) -> list[str]:
    """His questions after her last line that are not the line this turn answers (`skip`): he
    wrote several messages and only the last is quoted to her. At most OPEN_QUESTIONS, oldest
    first."""
    tail: list[Line] = []
    for line in reversed(lines):
        if not line.his:
            break
        tail.append(line)
    out = []
    for line in reversed(tail):
        if line.id == skip:
            continue
        out.extend(_clip(s) for s in _sentences(line.text) if is_question(s))
    return out[-OPEN_QUESTIONS:]


def her_place(lines: Sequence[Line]) -> str:
    """Where she last said she is, in her words ("at the studio till ten"); "" for nowhere."""
    for line in reversed(lines):
        if line.his:
            continue
        for sentence in reversed(_sentences(line.text)):
            if is_question(sentence):
                continue
            for m in reversed(list(_HER_PLACE.finditer(sentence))):
                where = m.group("where").strip()
                if not _NOT_A_PLACE.search(where):
                    return _clip(where, 8)
    return ""


def her_state(lines: Sequence[Line]) -> str:
    """The state she last said she is in (what she has on or off, on her break, closed up), in
    her words; "" for none."""
    for line in reversed(lines):
        if line.his:
            continue
        for sentence in reversed(_sentences(line.text)):
            m = None if is_question(sentence) else _HER_STATE.search(sentence)
            if m is not None:
                return _clip(m.group(0).strip(), 8)
    return ""


def her_said(lines: Sequence[Line]) -> list[str]:
    """What she has claimed about herself so far in this conversation (her place, her state),
    as plain strings; for a caller that wants her own claims (the beat)."""
    return [x for x in (her_place(lines), her_state(lines)) if x]


def plans(lines: Sequence[Line], *, skip: int = 0, taken: Sequence[int] = ()) -> list[tuple[bool, str]]:
    """(his, clipped sentence) for the newest PLANS plans between them, oldest first: a sentence
    of either that is no question and has a time and a word for doing something together or
    next. Not the line this turn answers, nor a line of his already among his news (`taken`)."""
    out: list[tuple[bool, str]] = []
    for line in reversed(lines):
        if line.id == skip or line.id in taken:
            continue
        for sentence in _sentences(line.text):
            if (not is_question(sentence) and _WHEN.search(sentence)
                    and _TOGETHER.search(sentence)):
                out.append((line.his, _clip(sentence)))
                break
        if len(out) == PLANS:
            break
    return out[::-1]


def block(lines: Sequence[Line], *, forms: tuple[str, str, str], skip: int = 0,
          lead: Sequence[str] = ()) -> list[str]:
    """The section's lines for a conversation (`conversation`'s rows), "- " each: `lead` (lines
    the caller always wants first), where and how she said she is, what he asked and is still
    owed, his news, the plans; the conversation's lines within MAX_TOKENS, what is over left out
    (his oldest news first, then the plans). `forms` are his (they, them, their); `skip` the line this turn
    answers. [] when there is nothing to say."""
    they = forms[0].capitalize()
    facts = his_facts(lines, skip=skip)
    place, state = her_place(lines), her_state(lines)
    first = [f"- {x}" for x in lead if x]
    if place:
        first.append(f"- You said you're {place}.")
    if state:
        first.append(f"- You said: {state}.")
    first += [f"- {they} also asked: {q}" for q in open_questions(lines, skip=skip)]
    news = [f"- {they} told you: {text}" for _, text in facts]
    later = [f"- {they if his else 'You'} said: {text}"
             for his, text in plans(lines, skip=skip, taken=[i for i, _ in facts])]

    n = len([x for x in lead if x])
    lead_lines, said = first[:n], first[n:]  # the caller's lead is not the conversation's

    def size(items: list[str]) -> int:
        return estimate_tokens("\n".join(items))
    while (news or later) and size([*said, *news, *later]) > MAX_TOKENS:
        if len(news) > 1 or not later:
            news.pop(0)  # his oldest news goes first
        else:
            later.pop(0)
    while said and size([*said, *news, *later]) > MAX_TOKENS:
        said.pop()
    return [*lead_lines, *said, *news, *later]


class SoFar:
    """Each member's block for her conversation with him, in memory: built from the stored
    lines `read(guid)` returns (oldest first), kept while the conversation's newest line and
    the turn's `skip` and `lead` are the same, forgotten IDLE_S after its newest line."""

    def __init__(self, read: Callable[[int], Sequence[Line]]):
        self._read = read
        self._kept: dict[int, tuple[tuple, float, list[str], list[str]]] = {}

    def lines(self, guid: int, now: float, *, forms: tuple[str, str, str], skip: int = 0,
              lead: Sequence[str] = ()) -> list[str]:
        """Her block now (see `block`); [] with no conversation and no `lead`."""
        return self._build(guid, now, forms, skip, tuple(lead))[0]

    def her_said(self, guid: int, now: float) -> list[str]:
        """What she has claimed about herself so far in the conversation going on (`her_said`)."""
        return list(self._build(guid, now, ("they", "them", "their"), 0, ())[1])

    def _build(self, guid, now, forms, skip, lead):
        for g in [g for g, kept in self._kept.items() if now - kept[1] > IDLE_S]:
            del self._kept[g]
        talk = conversation(self._read(guid), now)
        key = (talk[-1].id if talk else 0, len(talk), skip, lead, forms)
        kept = self._kept.get(guid)
        if kept is not None and kept[0] == key:
            return kept[2], kept[3]
        built = block(talk, forms=forms, skip=skip, lead=lead)
        said = her_said(talk)
        if talk:
            self._kept[guid] = (key, talk[-1].ts, built, said)
        return built, said
