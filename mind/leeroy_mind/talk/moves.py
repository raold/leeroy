"""What a chat turn does, as a few named moves, and what it is about. Rule-based: no model
call, no I/O. The patterns are non-explicit on purpose; explicit wording belongs only to the
local prompt overlays."""
from __future__ import annotations

import re
from collections.abc import Collection, Sequence
from typing import TYPE_CHECKING

from ..memory.recall import _STOPWORDS

if TYPE_CHECKING:
    from .movelog import MoveRow

MOVES: tuple[str, ...] = ("summon", "invite_game", "declare", "compliment", "tease", "question",
                          "callback", "plan", "complaint", "joke", "later", "petname", "raid",
                          "gear", "gift")
MAIN_MOVES = ("summon", "declare", "plan", "later", "callback", "complaint")

_PATTERNS = {
    "summon": r"\b(get|come|coe|pull) (over|ovre|here|heer|hre|find|get|take|sit|to me)\b"
              r"|\bget (your|ur) ass\b|\bover (here|hre|heer)\b"
              r"|\b(i'?m|i've been|been|i am|just sitting here) (wa?it+ing|waitiing|wating)\b"
              r"|\bwaiting (for (you|u)|all day)\b|\bdoor'?s open\b|\byour move\b"
              r"|\bdon'?t be late\b|\bpull up\b|\bfind me\b|\bhurry up\b",
    "invite_game": r"\b(in|to|at|into|inside)\s+(the\s+|my\s+|our\s+|a\s+)?(dalaran|orgrimmar|org"
                   r"|stormwind|shattrath|shatt|ironforge|undercity|vent|voice|discord|ts3|mumble"
                   r"|dungeon|raid|bg|battleground|arena|queue|group|party|lfg)\b"
                   r"|\b(duel\w*|inv|invite\w*|quests?)\b"
                   r"|\b(summon|port|portal)\s+(me|us|you)\b"
                   r"|\bjoin\b.{0,12}\b(raid|group|bg|party|queue|dungeon|arena|vent|voice)\b"
                   r"|\bqueu(e|es|ed|eing|ing)\s+(with|up|for)\b|\bwe'?re queu(e|eing|ing)\b",
    "declare": r"\bi (want|need|love|loev|luv) (you|u|ya)\b|\bily\b|\blove (you|u) (too|more)\b"
               r"|\bmissed (you|u) more\b|\bi miss (you|u)\b",
    "compliment": r"\byou (look|looks|are|'re|re) (so |really |very )?(good|great|amazing|cute|"
                  r"gorgeous|beautiful|handsome|hot|awesome)\b|\blooking (good|great|amazing)\b"
                  r"|\bwell played\b|\bnice (work|one|gear)\b|\bimpressive\b",
    "tease": r"\bbet you (can'?t|won'?t|couldn'?t)\b|\byou wish\b|\bas if\b|\bsure you did\b"
             r"|\bcan'?t even\b|\bshow-?off\b|\bdream on\b",
    "question": r"\?\s*(?:[;:=xX]-?[)(dDpP]\s*)*$",
    "callback": r"\byou said\b|\blike you (said|promised)\b|\bremember\b|\blast (night|time|week)\b",
    "plan": r"\bwe should (actually )?(meet|visit|get together|hang)\b|\bone day\b|\bsomeday\b"
            r"|\bmeet (up )?(irl|in person)\b|\bhang out sometime\b|\bsome weekend\b",
    "complaint": r"\bugh+\b|\bmy boss\b|\bkept me (late|up)\b|\bso (tired|stressed|annoyed)\b"
                 r"|\bhate (my|this|that)\b|\bsuch a (day|week)\b|\bexhausted\b",
    "joke": r"\blol\b|\blmao\b|\bhaha+\b|\bjk\b|\bjust kidding\b|\brofl\b",
    "later": r"\btonight\b|\bafter (raid|this)\b|\blater\b",
    "petname": r"\b(darling|babe|baby|hun|honey|sweetie|sweetheart|cutie|handsome|lover)\b",
    "raid": r"\b(raid|raids|boss|bosses|wipe|wiped|wipes|tank|tanks|heals|healer|healers|dps"
            r"|trash|kill|kills)\b",
    "gear": r"\b(tier|gear|ilvl|loot|epics?|enchant\w*|gems?|upgrade|upgrades|set bonus)\b",
    "gift": r"\bsent you \d+ ?g\b|\bsent you (a |some |the )?(gift|present|gold|mail)\b"
            r"|\bmailed you\b|\bgift for you\b|\bgot you (a|some) (gift|present)\b",
}
_RE = {k: re.compile(v, re.I) for k, v in _PATTERNS.items()}
_SENTENCE = re.compile(r"[.!?;,\n]+|\s(?:and|but)\s")
_WORD = re.compile(r"[a-z]{4,}")

GAME_WORDS = frozenset(
    "raid raids raiding boss bosses wipe wiped wipes wiping tank tanks tanking heal heals healer "
    "healers healing damage trash kill kills killed pull pulls pulled gear tier loot epic epics "
    "enchant enchants gems upgrade upgrades quest quests dungeon dungeons arena arenas "
    "battleground battlegrounds queue queued queues group groups guild guilds paladin paladins "
    "priest priests warrior warriors hunter hunters mage mages rogue rogues druid druids shaman "
    "shamans warlock warlocks death knight knights spec specs talent talents glyph glyphs buff "
    "buffs debuff cooldown cooldowns aggro threat mana rage energy flask flasks potion potions "
    "dalaran orgrimmar stormwind shattrath ulduar icecrown naxxramas".split())

_STOP = _STOPWORDS | frozenset(
    "also really very still much been like some more here only even again today tomorrow "
    "tonight yesterday okay yeah nope well sure maybe just thing things stuff gonna wanna "
    "gotta kinda sorta something anything nothing everything someone anyone about because "
    "while after before until being doing going make made know think thought feel feels "
    "good great nice come came back want need said says tell told right guys babe baby".split())


def _fold(text: str) -> str:
    return text.casefold().replace("’", "'")


def classify(text: str) -> frozenset[str]:
    """The moves a line makes. A clause whose destination or activity is in the game is an
    in-game invitation (invite_game) and its summons words do not count; a game word that is
    only a time or an aside (after raid, the bg is up) does not hide a summons."""
    t = _fold(text or "")
    found: set[str] = set()
    for sentence in _SENTENCE.split(t):
        if _RE["invite_game"].search(sentence):
            found.add("invite_game")
        elif _RE["summon"].search(sentence):
            found.add("summon")
    for move, rx in _RE.items():
        if move not in ("summon", "invite_game") and rx.search(t):
            found.add(move)
    return frozenset(found)


def main_move(moves: frozenset[str]) -> str:
    for m in MAIN_MOVES:
        if m in moves:
            return m
    for m in MOVES:
        if m in moves:
            return m
    return ""


def is_summon(text: str) -> bool:
    return "summon" in classify(text)


def topics(text: str, *, names: Collection[str] = ()) -> tuple[str, ...]:
    """Up to three content words (4+ letters, not stopwords, game words or names), lower-case,
    once each, in order of appearance."""
    skip = {_fold(n) for n in names}
    out: list[str] = []
    for w in _WORD.findall(_fold(text or "")):
        if w in _STOP or w in GAME_WORDS or w in skip or w in out:
            continue
        out.append(w)
        if len(out) == 3:
            break
    return tuple(out)


MOVE_PHRASES: dict[str, str] = {
    "summon": "asked {them} over",
    "invite_game": "invited {them} to something in the game",
    "declare": "told {them} you want {them}",
    "compliment": "complimented {them}",
    "tease": "teased {them}",
    "question": "asked {them} a question",
    "callback": "brought up something {they} said before",
    "plan": "talked about meeting up",
    "complaint": "complained about your day",
    "joke": "made a joke",
    "later": "said you'd see {them} later",
    "petname": "called {them} a pet name",
    "raid": "talked raid",
    "gear": "talked gear",
    "gift": "sent {them} a gift",
}
MOVES_LINE_CHARS = 200
_TIMES = {1: "", 2: " twice", 3: " three times"}


def _times(n: int) -> str:
    return _TIMES.get(n, f" {n} times")


def moves_line(rows: "Sequence[MoveRow]", *, night_start: float,
               forms: tuple[str, str, str]) -> str:
    """The "Lately you've..." line from her last turns to one person (oldest first): the newest
    row's main move and any move seen in 2+ rows (at most two, by count, ties newest first), and
    one topic seen in 2+ rows. It names moves, never her words. "" when there is nothing."""
    if not rows:
        return ""
    they, them, their = forms
    count: dict[str, int] = {}
    newest: dict[str, int] = {}
    tcount: dict[str, int] = {}
    for i, r in enumerate(rows):
        for m in (r.moves | {r.main}) - {""}:
            if m in MOVE_PHRASES:
                count[m] = count.get(m, 0) + 1
                newest[m] = i
        for t in set(r.topics):
            tcount[t] = tcount.get(t, 0) + 1
    last = rows[-1].main
    names = [m for m in count if m == last or count[m] >= 2]
    names.sort(key=lambda m: (-count[m], -newest[m]))
    names = names[:2]
    if last in count and last not in names:
        names = [last, names[0]] if names else [last]
    parts = [MOVE_PHRASES[m].format(they=they, them=them, their=their) + _times(count[m])
             for m in names]
    topic = max((t for t in tcount if tcount[t] >= 2), key=lambda t: tcount[t], default="")
    topic_part = f"brought up the {topic}{_times(tcount[topic])}" if topic else ""
    when = "Tonight" if all(r.ts >= night_start for r in rows) else "Lately"

    def build(items: list[str]) -> str:
        body = items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]
        return f"{when} you've already {body}: do something else this time."

    for items in ([*parts, topic_part] if topic_part else [], parts, parts[:1]):
        if items and len(build(items)) <= MOVES_LINE_CHARS:
            return build(items)
    return build(parts[:1]) if parts else ""
