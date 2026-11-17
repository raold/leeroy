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
              r"|\bdon'?t be late\b|\bpull up\b|\bfind me\b|\bhurry up\b"
              r"|\bto my (place|house|flat|apartment|room|bed)\b",
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
_COARSE = re.compile(r"[.!?;\n]+")
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
    for coarse in _COARSE.split(t):
        clauses = _SENTENCE.split(coarse)
        if any(_RE["invite_game"].search(c) for c in clauses):
            found.add("invite_game")
        elif any(_RE["summon"].search(c) for c in clauses):
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


# Seeing him in person, past the summons words (evening DM audit, 2026-11-16: a member who has
# never met him planned a date at an event of hers, asking him to come along, sit by her, turn
# up and settle things in person). A soft tell of its own ("in person", filters), not
# the summons: an answer owed to him is never pushed to its fallback for it. A clause whose
# place or activity is in the game is no meeting ("come to the raid"), nor a denial ("we've
# never met in person").
_EVENTS = (r"quiz|quiz night|pub quiz|pub|party|gig|show|concert|bar|club|wedding|match|"
           r"festival|meetup|meet-up|dinner|bbq|barbecue|cinema|movies|theatre|theater|karaoke|"
           r"open mic|reception|game night|venue")
_IN_PERSON = re.compile(
    r"\bin person\b|\bface to face\b|\bin the flesh\b|\bcome on over\b"
    r"|\b(?:meet|see|hug|kiss)\s+(?:you|u)\s+(?:irl|in real life)\b"
    r"|\btake (?:you|u) out\b"
    r"|\b(?:you|u)(?:'re| are| r|re)?\s+(?:showing|turning) up\b|\bur (?:showing|turning) up\b"
    r"|\b(?:you|u)(?:'ll| will|'d| would| better| should| could| can| have to| gotta| need to)"
    r"\s+(?:show|turn) up\b"
    r"|\bsit (?:right )?(?:next to|beside|by|with) me\b"
    r"|\bi (?:need|want) (?:you|u) (?:there|here with me)\b"
    rf"|\bcome (?:to|along to) (?:the|my|our|this) (?:{_EVENTS})\b", re.I)
_DENIED = re.compile(r"\b(?:never|not|no|without|cannot|wont|dont|didnt|havent|hasnt|isnt|"
                     r"arent|wasnt|werent|couldnt|wouldnt|shouldnt|aint)\b|\bcant\b(?! wait)")


def in_person(text: str) -> bool:
    """A clause of `text` that sees or brings someone in person (_IN_PERSON), not in a game
    clause and not denied before it in its clause."""
    t = _fold(text or "")
    for coarse in _COARSE.split(t):
        for clause in _SENTENCE.split(coarse):
            m = _IN_PERSON.search(clause)
            if (m and not _RE["invite_game"].search(clause)
                    and not _DENIED.search(clause[:m.start()].replace("'", ""))):
                return True
    return False


# What the "Lately you've..." hint may name as a topic (owner's live DM test, 2026-11-16: "brought
# up the actually twice", "brought up the three twice"): a content word, never a chat word
# (talk.words.COMMON_WORDS, everyday.CHAT_WORDS), a pronoun, a number, a stance adverb or a word
# under four letters, nor a form of a stopword ("telling", "thinking"). Stored rows keep their
# topics; the hint filters them when it reads them.
PRONOUNS = frozenset(
    "something anything nothing everything someone anyone everyone noone somebody anybody "
    "everybody nobody myself yourself himself herself itself ourselves yourselves themselves "
    "yours mine theirs ours hers whatever whoever whichever whomever these those this that "
    "they them their there here where".split())
NUMBERS = frozenset(
    "zero four five seven eight nine eleven twelve thirteen fourteen fifteen sixteen seventeen "
    "eighteen nineteen twenty thirty forty fifty sixty seventy eighty ninety hundred thousand "
    "million first second third fourth fifth sixth seventh eighth ninth tenth twice once half "
    "dozen three".split())
ADVERBS = frozenset(
    "actually really honestly literally basically seriously totally definitely probably maybe "
    "perhaps just still even already always never ever again almost quite pretty kinda sorta "
    "anyway anyways though also very truly surely simply clearly obviously especially finally "
    "usually exactly absolutely completely entirely apparently hopefully certainly genuinely "
    "lowkey highkey super mostly nearly barely hardly sometimes often somehow somewhere "
    "anywhere everywhere nowhere tonight today tomorrow yesterday later soon right okay".split())
# the commonest verbs, whose forms ("saying", "talking") name no subject either
VERBS = frozenset("say tell talk ask look see seem try let keep give take put call mean".split())


def topic_word(word: str) -> bool:
    """Whether `word` is a content word the hint may name as a topic (the rule above)."""
    from .everyday import CHAT_WORDS, is_everyday
    from .words import COMMON_WORDS
    w = _fold(word or "")
    if len(w) < 4 or not w.isalpha():
        return False
    if (w in COMMON_WORDS or w in CHAT_WORDS or w in PRONOUNS or w in NUMBERS or w in ADVERBS
            or w in VERBS):
        return False
    if w in _STOP:
        return False
    # a form of a stopword ("telling", "thinking", "wanted", "getting", "making")
    if any(b in _STOP or b in CHAT_WORDS or b in VERBS for b in _bases(w)):
        return False
    # an adverb of an everyday adjective: "honestly", "literally", "seriously", "totally"
    return not (len(w) >= 7 and w.endswith("ly") and is_everyday(w[:-2]))


def _bases(w: str) -> set[str]:
    """The words `w` may inflect: -ing, -ed, -s and -es off, a doubled consonant halved, an e
    put back ("getting" -> "get", "making" -> "make", "liked" -> "like")."""
    out = set()
    for end in ("ing", "ed", "es", "s"):
        if w.endswith(end) and len(w) - len(end) >= 2:
            stem = w[:-len(end)]
            out |= {stem, stem + "e"}
            if len(stem) >= 3 and stem[-1] == stem[-2]:
                out.add(stem[:-1])
    return out


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
    one topic seen in 2+ rows, a content word only (topic_word). It names moves, never her words.
    "" when there is nothing."""
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
            if topic_word(t):
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


DO_ELSE = ": do something else this time."


def question_first(line: str, their: str) -> str:
    """The instruction's tail on an answer to his question (asking.is_question; owner's live DM
    test, 2026-11-16: the hint's "do something else" overrode a direct question): "Answer his
    question first, plainly, then whatever else." alone, or with the moves_line `line` after it
    as what she already did, its "do something else" gone. `their` is his possessive."""
    from .asking import QUESTION_FIRST
    first = QUESTION_FIRST.format(their=their)
    if not line:
        return first + "."
    rest = line[:-len(DO_ELSE)] if line.endswith(DO_ELSE) else line.rstrip(".")
    return f"{first}; {rest[:1].lower()}{rest[1:]}."
