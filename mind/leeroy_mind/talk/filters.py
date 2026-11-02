"""Assistant tells: what makes a line read like a helper bot instead of a guildie. Three
tiers (controller ruling, Task 14 fix round 1):
- fatal: regenerate once, then drop the turn;
- soft: regenerate once, then accept a retry whose tells are all soft or repairable;
- repairable (neither): fixed in place by repair().
Phrases a real player also types ("want me to tank?", "absolutely not", "let me know when ur
ready") are not tells at all."""
import re
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Sequence

from leeroy_mind.personas.screens import OBJECT_WINDOW, ROMANCE_RE
from leeroy_mind.text import (EMOJI_RE, NAME_PREFIX_RE, NAME_TAG, has_meta, normalize_words,
                              strip_emoji, strip_name_prefix, word_jaccard)

ASSISTANT_PHRASES = ("happy to help", "absolutely", "great question", "let me know",
                     "how can i help", "feel free", "i hope this helps", "as an ai",
                     "i'm here for you")
# Fatal anywhere in a line. "absolutely" (as an opener), "let me know" (as a closer) and
# "feel free" are soft; see find_tells.
FATAL_PHRASES = ("happy to help", "great question", "how can i help", "i hope this helps",
                 "i'm here for you", "as an ai")
SILENT = "(nothing)"
REPEAT_JACCARD = 0.8
MIN_REPEAT_WORDS = 3  # "gz", "gg all", "ok pulling" may repeat
MAX_ACTION_WORDS = 3
ECHO_RUN = 5  # this many of the answered line's words in a row, in order, is an echo
ECHO_SHARE = 0.6  # or a run of 3+ that makes up this share of the reply


@dataclass(frozen=True, slots=True)
class Tell:
    reason: str
    fatal: bool
    soft: bool = False


def _phrase(p: str) -> re.Pattern:
    return re.compile(rf"(?<!\w){re.escape(p)}(?!\w)")


_FATAL_RE = {p: _phrase(p) for p in FATAL_PHRASES}
_FEEL_FREE = _phrase("feel free")
_OPENS_ABSOLUTELY = re.compile(r"^\W*absolutely(?=\W*$|\s*[,.!;:—–-])")
# Closers, tested on the final clause with its trailing punctuation and emoticons removed.
_CLOSERS = (
    ("let me know", re.compile(r"(?:^|\s)let me know(?:\s+if\b.*)?$")),
    ("if you need anything", re.compile(r"(?<!\w)if you need anything$")),
    ("just ask", re.compile(r"(?<!\w)just ask$")),
    ("would you like", re.compile(r"(?<!\w)would you like(?!\w)")),
    ("want me to ... for you", re.compile(r"(?:^|\s)want me to\b.*\bfor you$")),
)
_TRAILING_EMOTES = re.compile(r"(?:\s+[^\w\s]\S{0,3})+\s*$")
_CLAUSE_BREAK = re.compile(r"[,\n—–]|(?<=[.!?])\s+|;(?=\s)")
_LIST_MARK = re.compile(r"^[ \t]*(?:[-*•]|\d{1,2}[.)])[ \t]+(?=\S)", re.MULTILINE)
_DIALOGUE = re.compile(r"\n\s*" + NAME_TAG)
_ACTION = re.compile(r"\*([^*\n]+)\*")
_DASH = re.compile(r"\s*[—–]\s*")
# A semicolon, unless it is the eye of an emoticon: ;) ;-) ;P ;D and both eyes of ;_;
_SEMI = re.compile(r"\s*(?<!_);(?![-']?[)(\]\[DPpOo3*/\\|_])\s*")
# The frame of a prompt chat line the model may copy: "[3m ago]", "(whisper to Zan):", "(raid)",
# "(on vent):".
_AGE = r"(?:just now|\d{1,3}\s?[smhd] ago)"
_COPIED_TAG = re.compile(
    rf"^\s*(?:\[{_AGE}\]|\((?:{_AGE}|whisper(?: (?:to|from) [^)\n]{{1,24}})?|guild|officer"
    r"|party|raid(?: warning)?|say|yell|on vent)\))\s*:?\s*", re.IGNORECASE)
_QUOTES = "\"“”"
# A tag's colon, never the eyes of an emoticon ("zan :) welcome back", "zan: D", "zan :S":
# fix round 1, review M2); "Zan: Done with the boss" is a tag.
_TAG_COLON = r"\s*:(?!\s?[-'^]?[)(\]\[DPpOo3*/\\|SsXxCcLl](?![A-Za-z]))"
# The reply command the model typed in front of a line ("/r on my way"); never "/roll".
_REPLY_CMD = re.compile(rf"^\s*/(?:r|reply)(?![\w/])(?:\s*,|{_TAG_COLON})?\s*", re.IGNORECASE)


def _at_him(him: Sequence[str]) -> re.Pattern | None:
    """A leading whisper command, @handle or "Name:" tag naming him ("/w Zan", "@Zan,",
    "Zan:"): how the game or other chats address him, never words of the line (rehearsal 3,
    finding 5: a whisper to him went out as "/w <his character name> ...")."""
    names = sorted({n for n in him if n}, key=len, reverse=True)
    if not names:
        return None
    alt = "|".join(map(re.escape, names))
    return re.compile(rf"^\s*(?:/(?:w|whisper|t|tell|send)\s+|@)(?:{alt})(?![\w'])"
                      rf"(?:\s*,|{_TAG_COLON})?\s*|^\s*\[?(?:{alt})\]?{_TAG_COLON}\s*",
                      re.IGNORECASE)


def _fold(text: str) -> str:
    return text.casefold().replace("’", "'")


def _final_clause(text: str) -> str:
    t = _TRAILING_EMOTES.sub("", _fold(text).strip())
    parts = [p.strip(" .!?") for p in _CLAUSE_BREAK.split(t)]
    parts = [p for p in parts if p]
    return parts[-1] if parts else ""


def _repeat(text: str, own: str) -> str | None:
    """"exact" (equal after normalisation), "near" (word Jaccard >= 0.8) or None. Lines under
    MIN_REPEAT_WORDS words never count."""
    a = normalize_words(text)
    if len(a.split()) < MIN_REPEAT_WORDS:
        return None
    if a == normalize_words(own):
        return "exact"
    return "near" if word_jaccard(text, own) >= REPEAT_JACCARD else None


def strip_copied(text: str, names: Sequence[str] = (), *, him: Sequence[str] = ()) -> str:
    """What the model copied from how the prompt shows chat ("[3m ago] Belra (whisper to Zan):
    text"): wrapping quotes, a leading age, a channel or whisper tag ("(whisper to Zan):",
    "(raid)") and a speaker tag naming one of `names` (the speaker's own character name, their
    real first name), in any order. With `him` (his names), also what addresses him as a
    command or a handle would: a leading "/w Zan" (or /whisper, /t, /tell), "@Zan" or "Zan:",
    and the reply command "/r" for anyone (rehearsal 3, finding 5). A tag naming anyone else
    is an address ("Arkon: you're up") and stays, and so does his name as a word ("zan,
    you're late")."""
    names = [n for n in names if n]
    at_him = _at_him(him)
    t = text.strip()
    while True:
        before = t
        while len(t) >= 2 and t[0] in _QUOTES and t[-1] in _QUOTES:
            t = t[1:-1].strip()
        m = _COPIED_TAG.match(t)
        if m:
            t = t[m.end():]
        for name in names:
            t = strip_name_prefix(t, name)
        if at_him is not None and (m := at_him.match(t)):
            t = t[m.end():]
        if m := _REPLY_CMD.match(t):
            t = t[m.end():]
        t = t.strip()
        if t == before:
            return t


_YOU = re.compile(r"(?<!\w)(?:you|u|ya|ur|your|yours)(?!\w)", re.IGNORECASE)
# A wink, a kiss or a heart typed in chat.
WINK_RE = re.compile(r"(?<!\S)(?:;-?[)DPp]+|<3+|:-?\*|;\*|xoxo)(?=[\s.!?,]|$)")
# A wink or a kiss at a member, but no heart: "ty for the heals coros <3" is banter (fix
# round 1, review I2).
MEMBER_WINK_RE = re.compile(r"(?<!\S)(?:;-?[)DPp]+|:-?\*|;\*)(?=[\s.!?,]|$)")
# Flirting in a line of his own (his "hey you ;) wear that red dress tonight? ... just us"):
# a wink, a romance word (personas.screens.ROMANCE_RE: kiss, but never "kiss of death") or
# one of these. Raid talk is no flirt: "you and me on the left adds", "we missed you last
# week", "beautiful pull guys".
_HIS_FLIRT = re.compile(
    r"\b(?:sexy|handsome|gorgeous|cutie|babe|darling|sweetheart|sweetie"
    r"|miss (?:you|u|me)\b(?!\s+(?:last|on|in|at)\b)|just (?:the two of )?us|date night"
    r"|(?:that|the|your|ur|red|black|little) dress|wink\w*|cuddl\w*|snuggl\w*"
    r"|(?:you're|youre|ur|you are|you look) (?:so )?beautiful)\b", re.IGNORECASE)


def flirty(text: str) -> bool:
    """His line is a flirt: a wink, kiss or heart, a romance word or a flirt phrase. A public
    reply to it carries her romance text every time (rehearsal 2, judge C 1)."""
    return bool(WINK_RE.search(text) or ROMANCE_RE.search(text) or _HIS_FLIRT.search(text))


# Desire and innuendo, wider than ROMANCE_RE's flirt, crush and kiss (rehearsal 2, finding 8:
# "i want you so bad arkon", "coros you can tank me any time ;)" passed the screen). Each is
# worded so raid talk and banter pass (fix round 1, review I2): "i want you healing the tanks",
# "i want you there by 8", "your new tier looks so good", "damn fine tanking", "it's so hot in
# my flat", "thinking of you, hope your mom is ok", "just us two left in vh".
_DESIRE = re.compile(
    r"\bi want (?:you|u) (?:so\b|bad\b|badly\b|now\b|tonight\b|right now\b|all to myself\b)"
    r"|\bi want (?:you|u)\s*[.!?,;:]*\s*$"
    r"|\bi'?d let (?:you|u)\b.{0,30}\b(?:all night|any ?time|anything)\b|\ball night long\b"
    r"|\bworth (?:your|ur) while\b|\bmake it worth\b|\bbuy me a drink\b"
    r"|\btank me (?:any ?time|whenever|all night)\b|\bcome home to me\b|\bdream of me\b"
    r"|\bjust (?:the two of )?us\b(?!\s+(?:two|three|left)\b)"
    r"|\b(?:you're|youre|ur|you are|u r)\s+(?:so\s+|really\s+|soo+\s+)?"
    r"(?:hot|sexy|cute|handsome|gorgeous|beautiful)\b"
    r"|\b(?:you|u)\s+look(?:s|ing)?\s+(?:so|soo+|really)\s+(?:good|hot|fine)\b",
    re.IGNORECASE)
# A member said to be desirable, named as its subject: "arkon is so hot when he taunts".
_SUBJECT_DESIRE = re.compile(
    r"(?P<who>[\w']+)\s+(?:is|looks?)\s+(?:so|soo+|really)\s+(?:hot|sexy|handsome|gorgeous)\b",
    re.IGNORECASE)
# A pet name said to someone ("honey" only as an address, never "honey mead").
_PET = re.compile(r"\b(?:darling|babe|sweetheart|sweetie|handsome|gorgeous|sexy|cutie"
                  r"|hottie|hot stuff|my love)\b|(?<![\w'])honey(?=\W*(?:,|$))",
                  re.IGNORECASE)
_SECOND = re.compile(r"(?<!\w)(?:you|u|ya|ur|your|yours|you're|youre)(?!\w)", re.IGNORECASE)
_HIM_WORDS = re.compile(r"\b(?:he|him|his|he's|hes|himself)\b", re.IGNORECASE)
# What a line may open on before the name it speaks to: greetings and interjections ("oh
# arkon you're so hot": fix round 2, re-review NB2).
_GREETS = re.compile(r"^\W*(?:(?:hey|hi|yo|oi|ok|so|and|lol|haha|happy birthday|gz|grats|gratz"
                     r"|congrats|ty|thanks|thx|night|gn|morning|evening|oh|ooh+|mm+|omg|god|wow"
                     r"|aww+|honestly|damn|well|hmm+|ugh)\W+)*", re.IGNORECASE)
# A closing name the word before it takes as its object is no address: "don't tell belra",
# "more fun than belra".
_GOVERNED = re.compile(r"\b(?:tell|than|ask|from|with|about|like|for|to|at|of|and|or)\W*$",
                       re.IGNORECASE)
# Nor, after desire on his turn, a rival it is set against with no comma: "i want you so bad
# not belra", "... more than belra", "... sorry belra" (fix wave G3, G1 re-review 2 R1). Only
# the words that set a rival against him: "come home to me and arkon" and "i want you so bad
# with arkon" take the member in, and are caught (fix round 1, review I1).
_CONTRAST = re.compile(r"\b(?:than|not|unlike|over|vs|versus|instead(?:\s+of)?"
                       r"|forget(?:\s+about)?|sorry|tell|ask)\W*$", re.IGNORECASE)
# Desire phrases that are also everyday words ("we raided all night long and arkon fell
# asleep", "it's just us and dunna in vh"): near a member's name they are no flirt, only said
# to one (G1 re-review 2 R2; _weak_said_to).
_WEAK = re.compile(r"all night long|make it worth|worth (?:your|ur) while|just (?:the two of )?us"
                   r"|buy me a drink|i want (?:you|u) (?:now|tonight|right now)", re.IGNORECASE)
_CLAUSES = re.compile(r"[,;.!?\n]+|\s+(?:and|but|or|so)\s+", re.IGNORECASE)
# What may follow a name the line speaks to: "just us tonight arkon lol".
_TAIL_FILLER = frozenset({"lol", "lmao", "rofl", "haha", "hahaha", "hehe", "xd", "tbh", "honestly",
                          "tonight", "ok", "okay", "pls", "plz", "please", "then", "now", "again"})


def _weak_said_to(text: str, hit: re.Match, members: Sequence[str]) -> bool:
    """A weak phrase of desire (_WEAK) said to a member: its own clause or the one before it
    ends on their name, filler words aside ("just us tonight arkon lol", "so yeah arkon, just
    us tonight"), or the clause after it is the name alone or the name and "you" ("just us
    tonight, arkon, all night", "ok i want you tonight, arkon you in?"). "we raided all night
    long and arkon fell asleep" and "let's make it worth it, arkon is bringing flasks" speak
    to nobody."""
    pat = _address_re(members)
    if pat is None:
        return False
    t = _TRAILING_EMOTES.sub("", text)
    cuts = [0, *(x for m in _CLAUSES.finditer(t) for x in m.span()), len(t)]
    spans = [(a, b) for a, b in zip(cuts[::2], cuts[1::2]) if t[a:b].strip()]
    at = next((i for i, (a, b) in enumerate(spans) if a <= hit.start() < b), None)
    if at is None:
        return False

    def ends_on(i: int) -> bool:
        a, b = spans[i]
        names = list(pat.finditer(t, a, b))
        return bool(names) and all(w.casefold() in _TAIL_FILLER
                                   for w in _TOKENS.findall(_fold(t[names[-1].end():b])))
    if ends_on(at) or (at > 0 and ends_on(at - 1)):
        return True
    if at + 1 == len(spans):
        return False
    after = t[slice(*spans[at + 1])].strip()
    m = pat.match(after)
    rest = after[m.end():].lstrip() if m is not None else None
    return rest is not None and (not rest or bool(_YOU.match(rest)))


# How close after a romance word a member is its object on a line that is his ("a crush on
# belra", "kiss belra"): right after it, not anywhere in the next 40 characters.
NEAR_OBJECT = 16


def _name_re(names: Sequence[str]) -> re.Pattern | None:
    """The names as whole words, in any case; a possessive counts ("zan's")."""
    names = sorted({n for n in names if n}, key=len, reverse=True)
    return re.compile(r"(?<![\w'])(?:" + "|".join(map(re.escape, names))
                      + r")(?!\w)(?!'(?!s(?!\w)))", re.IGNORECASE) if names else None


def _address_re(names: Sequence[str]) -> re.Pattern | None:
    """The names as a line speaks to them: bare, in any case. A possessive names someone but
    speaks to nobody ("lol belra's at it again": fix round 2, re-review NB1)."""
    names = sorted({n for n in names if n}, key=len, reverse=True)
    return re.compile(r"(?<![\w'])(?:" + "|".join(map(re.escape, names)) + r")(?![\w'])",
                      re.IGNORECASE) if names else None


def names_any(text: str, names: Sequence[str]) -> bool:
    """The text names one of `names` (_name_re: any case, possessives too)."""
    pat = _name_re(names)
    return pat is not None and bool(pat.search(text))


def line_addressee(text: str, names: Sequence[str], *, ends: bool = True) -> str | None:
    """Whom the line speaks to, of `names` (bare names, _address_re): one it opens on
    ("dunna, ...", "oh dunna you", "happy birthday dunna! ...") or, with `ends`, ends on ("...
    so bad dunna"), unless the word before takes it as its object ("don't tell dunna"); else
    None."""
    pat = _address_re(names)
    if pat is None:
        return None
    t = text.strip()
    m = pat.match(t, _GREETS.match(t).end())
    if m:
        return m.group(0)
    if not ends:
        return None
    tail = _TRAILING_EMOTES.sub("", t).rstrip(" .!?,")
    m = None
    for m in pat.finditer(tail):
        pass
    if m is None or m.end() != len(tail) or _GOVERNED.search(tail[:m.start()]):
        return None
    return m.group(0)


def _closes_on(text: str, names: Sequence[str]) -> bool:
    """The line's last clause ends on one of `names` and either is only that name ("i want you
    so bad, arkon") or itself holds the desire ("i want you so bad arkon"); "don't tell
    belra" and "more fun than belra" hold none (fix round 2, the re-review's rule), and a
    name the desire is set against is a rival, not the one spoken to ("i want you so bad more
    than belra": _CONTRAST)."""
    pat = _address_re(names)
    if pat is None:
        return False
    last = _final_clause(text)
    m = None
    for m in pat.finditer(last):
        pass
    return (m is not None and m.end() == len(last)
            and (m.start() == 0 or (bool(_DESIRE.search(last) or _PET.search(last))
                                    and not _CONTRAST.search(last[:m.start()]))))


def member_flirt(text: str, members: Sequence[str], *, to_member: bool,
                 human: Sequence[str] = (), to_him: bool = False, winks: bool = True) -> bool:
    """Romance aimed at a guildmate (content invariant: only ever at the human). The rule is
    the fix round 2 re-review's, checked on its 100 lines.
    - A romance word (personas.screens.ROMANCE_RE) with a member as its object: named within
      OBJECT_WINDOW characters after it, or "you" there when the turn is addressed to a member
      or the line opens on one ("arkon, flirting with you is my favourite part").
    - On a line that is his (`to_him`: a turn to him, a whisper to him, a FLIRT; or a line
      naming him, possessives too, on a turn not addressed to a member) the romance is his,
      and a rival may be mentioned ("belra's cute but i want you so bad", "belra, back off ;)
      he's mine"). Only a romance word with a member right after it (NEAR_OBJECT), or desire
      or a pet name (no winks) in a line that opens on a member or whose last clause ends on
      one (_closes_on: "i want you so bad arkon"), is a flirt at a member; never in a line
      that opens on his name.
    - Elsewhere, desire or innuendo (_DESIRE), a pet name or (`winks`: a woman's) a wink, in a
      line said to a member (it opens or ends on one: line_addressee) or on a turn addressed
      to a member; desire with a member named within OBJECT_WINDOW of it ("god arkon i want
      you so bad"), its everyday phrases (_WEAK: "all night long", "just us") only when said
      to one (_weak_said_to); or a member named as desirable ("arkon is so hot").
    Talk about a member flirting with him ("Belra is flirting with Zan") is not aimed at a
    member, and neither is banter ("dunna you're a beast tonight", "ty for the heals <3",
    "lol belra's at it again ;)")."""
    his = to_him or (names_any(text, human) and not to_member)
    pats = [re.compile(rf"(?<!\w){re.escape(n)}(?!\w)", re.IGNORECASE) for n in members if n]
    if not his and (to_member or line_addressee(text, members, ends=False) is not None):
        pats.append(_YOU)
    for m in ROMANCE_RE.finditer(text):
        window = text[m.end():m.end() + (NEAR_OBJECT if his else OBJECT_WINDOW)]
        if any(p.search(window) for p in pats):
            return True
    desire, pets = [*_DESIRE.finditer(text)], [*_PET.finditer(text)]
    if his:
        if not (desire or pets) or line_addressee(text, human, ends=False) is not None:
            return False
        return (line_addressee(text, members, ends=False) is not None
                or _closes_on(text, members))
    winked = [*MEMBER_WINK_RE.finditer(text)] if winks else []
    if (desire or pets or winked) and (to_member or line_addressee(text, members)):
        return True
    named = _name_re(members)
    if named is None:
        return False
    if any(_weak_said_to(text, h, members) if _WEAK.fullmatch(h.group(0))
           else named.search(text, max(0, h.start() - OBJECT_WINDOW), h.end() + OBJECT_WINDOW)
           for h in desire):
        return True
    return any(named.fullmatch(m.group("who")) for m in _SUBJECT_DESIRE.finditer(text))


def aimed_off_him(text: str, members: Sequence[str], human: Sequence[str]) -> bool:
    """A FLIRT turn's line, his by construction, that went to a member instead (rehearsal 2:
    "happy birthday dunna! if i was there i'd make you a cake"): it opens on a member's name
    and says nothing of him (no name of his, no he/him). "miss me?", "back off belra, he's
    mine ;)" and "you know i'm more fun than belra" are his (fix round 1, review I1)."""
    if line_addressee(text, members, ends=False) is None:
        return False
    return not (names_any(text, human) or _HIM_WORDS.search(text))


def _longest_run(a: list[str], b: list[str], allowed: frozenset[str] = frozenset()) -> int:
    best, prev = 0, [0] * (len(b) + 1)
    for x in a:
        cur = [0] * (len(b) + 1)
        for j, y in enumerate(b, 1):
            if x == y and x not in allowed:
                cur[j] = prev[j - 1] + 1
                best = max(best, cur[j])
        prev = cur
    return best


def echoes(text: str, line: str, *, share: bool = True,
           allowed: frozenset[str] = frozenset()) -> bool:
    """The reply says `line` back: ECHO_RUN of its words in a row, or (with `share`) a run of
    3 or more that is at least ECHO_SHARE of the reply. A leading name tag is ignored; a reply
    under 3 words ("gz", "evening all") never counts. `allowed` words never make a run: the
    subject of his question that the reply's memories or facts hold (rehearsal 2, judge B 2:
    the words of his question were in the answerer's memory, and the retry broke the fact)."""
    a = normalize_words(NAME_PREFIX_RE.sub("", text, count=1)).split()
    b = normalize_words(line).split()
    if len(a) < 3 or not b:
        return False
    run = _longest_run(a, b, allowed)
    return run >= ECHO_RUN or (share and run >= 3 and run >= ECHO_SHARE * len(a))


COPY_MIN_WORDS = 4  # "evening, rough one?" or "gz on bracers" from two people is chat


def copies(text: str, line: str) -> bool:
    """The reply is a copy of a chat line: an exact or near repeat (word Jaccard >=
    REPEAT_JACCARD) of COPY_MIN_WORDS words or more, or ECHO_RUN of its words in a row."""
    if len(normalize_words(text).split()) >= COPY_MIN_WORDS and _repeat(text, line):
        return True
    return echoes(text, line, share=False)


# A claim of shared history (rehearsal 2, finding 1: "remember when you pulled that entire wing
# in vanilla?", "you've been a theatre kid since 2005", "the loot war has been raging for 2
# years", "you're the first to go again" on the night's first wipe). Without a memory in the
# prompt that shares its words it is a soft tell, and a retry that still has it is dropped.
_YEARS = r"(?:\d+|a couple of|two|three|four|five|six|seven|eight|nine|ten|many|so many)"
_ERA = (r"(?:vanilla|classic|t?bc|the burning crusade|burning crusade|launch|beta|release"
        r"|19\d\d|200[0-8])")  # the guild lives in 2009: an earlier year is the past
# A past act after "the time/day/night we": "the night we downed kt", never a plan ("by the
# time we pull", "the day we kill lk": fix round 1, review I5).
_PAST = (r"(?:\w+ed|was|were|did|had|got|went|made|took|ran|fell|lost|won|saw|said|told|came|"
         r"left|broke|wiped|died|met|kept|paid|sat|stood|fought|caught|threw|spent|bought|"
         r"brought|thought|forgot|drank|ate|sang|swam)")
_SHARED_CLAIM = re.compile(
    r"\bremember (?:when|that time|the time|back when|that night|the night|that day)\b"
    r"|\bremember how (?:you|u|we|he|she|they)\b"
    rf"|\b(?:that|the) (?:time|night|day) (?:you|u|we|he|she|they) {_PAST}\b"
    r"|\blast time (?:you|u|we|he|she|they)\b"
    r"|\b(?:than|like|as) (?:you|u|we) did\b")
# Her own life, not the guild's past: "my" anything, or her family with "we" or "she"
# ("we've been married for 10 years", "my daughter's recital. she's been practicing").
_MINE = frozenset({"my", "mine", "myself"})
_YOU_WE = frozenset({"you", "u", "you're", "youre", "you've", "youve", "we", "we've", "weve",
                     "we're"})
_HOUSEHOLD = frozenset({"we", "we've", "weve", "we're", "she", "she's", "shes"})
_FAMILY = frozenset({"married", "marriage", "wife", "husband", "kids", "kid", "son", "sons",
                     "daughter", "daughters", "baby", "twins", "mom", "mum", "dad", "mother",
                     "father", "family", "boyfriend", "girlfriend", "partner", "fiance",
                     "fiancee", "brother", "sister", "nan", "grandma", "grandpa", "dog", "cat"})
# Claims of an era or a span that may also be the speaker's own life ("i've raided since
# 2005"): a claim only when the sentence is not about the speaker alone.
_ERA_CLAIM = re.compile(
    rf"\b(?:back )?in {_ERA}\b|\bback (?:when|in the day|in the old days)\b"
    rf"|\bsince (?:{_ERA}|wrath|wotlk)\b|\bfor {_YEARS} (?:years|months)\b|\bfor years\b")
_AGAIN = re.compile(r"\bagain\b")
_SENTENCE = re.compile(r"[^.!?;\n]+")
_TOKENS = re.compile(r"[a-z0-9']+")
_ME = frozenset({"i", "i'm", "im", "i've", "ive", "i'd", "id", "i'll", "me", "my", "mine",
                 "myself"})
_PERSONS = _ME | frozenset({"you", "u", "ya", "ur", "your", "you're", "youre", "you've",
                            "youve", "we", "us", "our", "we've", "weve", "he", "she", "he's",
                            "she's", "hes", "shes", "they", "they're", "theyre", "him", "her",
                            "them"})
# Who a span or an era is claimed of: someone else, or the guild ("you've been at it since
# 2005"); "had a crush on you since wrath" is her own feeling, not a shared event.
_SUBJECTS = frozenset({"you've", "youve", "you're", "youre", "we", "we've", "weve", "we're",
                       "they", "they've", "theyve", "they're", "theyre", "he", "she", "he's",
                       "she's", "hes", "shes", "guild"})
_YOU_DID = frozenset({"been", "have", "has", "were", "was", "did", "used", "always", "never",
                      "kept", "still"})
# Whose past act an "again" is about: a person before it ("coros first to go again", "you'd
# been eaten by a gnome again": rehearsal 3); never a plan ("we go again", "let's try again")
# or a thing ("wiped again").
_AGAIN_WHO = frozenset({"you", "u", "ur", "you're", "youre", "you've", "youve", "you'd", "youd",
                        "he", "she", "he's", "she's", "hes", "shes", "he'd", "she'd", "they",
                        "they're", "theyre", "they'd", "theyd"})
_AGAIN_PLAN = frozenset({"we", "let's", "lets", "us", "try", "never", "once", "we'll"})
# Or a past act told with nobody named, opening its sentence: "eaten by a gnome again",
# "lol one-shot by the dragon again" (rehearsal 3); "i got eaten again" is her own.
_PASSIVE_AGAIN = re.compile(
    r"\W*(?:(?:lol|lmao|haha|omg|oh|ah|aww*|ugh|wow|rip|so|and|or|got|get|gets|been|getting)"
    r"\W+)*(?:[a-z]{2,}[^e\W]ed|eaten|taken|beaten|bitten|stolen|thrown|frozen|broken|gotten"
    r"|caught|shot|hit|stuck|struck|one-?shot)\s+by\b")
_SUPPORT_STOP = frozenset("""the and you your you're youre that this with for was were have has
had are but not all just like what when then than there they them she her him his its it's our
out get got can will would could should been being from into over about still even also too
very really some any one much more most now how who why where which way lol lmao haha hehe
yeah yes nah okay i'm i've i'd i'll me my mine we us did does don't dont didn't gonna wanna man
dude guys guy because cause cuz really thing things know see say said remember time times
again since year years month months back last ago day days tonight""".split())


# Idioms from after 2012 in a 2009 guild (rehearsal 2, judge A 7: "the drake is fire", "the
# vibes in 25", "the most on brand thing"): a soft tell, asked again in 2009's words. Each is
# worded so the game's own words pass: a fire mage, "based on", "no cap on emblems", "time to
# slay", "the floor is fire, move", "it's giving 2 emblems now" (fix round 1, review minor 7).
_SLANG = re.compile(
    r"\bvib(?:es|ing)\b|\bvibe check\b"
    r"|(?<!floor )(?<!ground )(?<!room )(?<!\bit )(?<!this )(?<!that )"
    r"\b(?:is|was|so|that's|thats|lowkey|straight|pure|absolute)\s+fire\b(?!\s+(?:mage|resist"
    r"\w*|damage|dmg|spec|tree|res|elemental|totem|aura|ward|shield|nova|blast|ball|on|in|at)\b)"
    r"|\bon[- ]brand\b|\bwhole personality\b|\blow-?key\b(?!\s+(?:night|evening|raid|run|week"
    r"|event|affair)\b)|\bno cap\b(?!\s+(?:on|for|to|at|in|of)\b)"
    r"|\bslay(?:ed|ing)?\s+(?:it|queen)\b|(?<!\bto )(?<!\bgo )\bslay\b(?=\W*$)"
    r"|\bit'?s giving\b(?!\s+(?:\d|me|you|us|them|him|her|out|up|away|back|off)\b)"
    r"|\bmain character (?:energy|vibes?|syndrome"
    r"|moment)\b|\brent[- ]free\b|\bsus\b|\b(?:so|that's|thats|is|very|pretty|kinda)\s+based\b"
    r"(?!\s+(?:on|off|in|out|around|upon|at)\b)|\brizz\w*|\byeet\w*|\bbestie\b"
    r"|\bsimp(?:s|ing|ed)?\b|\bbussin\w*|\btouch grass\b|\bunderstood the assignment\b"
    r"|\bhits different\b"
    # rehearsal 3: "that kill was so hype", "literally so down"; "so hyped", "literally just"
    # and "he literally said so" are 2009's
    r"|\bso hype\b(?!-?d)|\bhype af\b"
    r"|\bliterally so\b(?=\s+[a-z])(?!\s+(?:that|as|far as|long as)\b)"
    # live session 1: "u dont gotta apologize for being a flex machine". Flex for showing off
    # is 2010s. Flexible, flexweave, reflexes, the /flex emote, flex time at work and a raid's
    # flex spot or spec are older
    r"|(?<![\w/])flex(?:ing|in)?\b(?!\s+(?:time|hours?|schedule|days?|shifts?|spots?|slots?"
    r"|specs?|roles?|dps|tanks?|healers?|heals)\b)", re.IGNORECASE)


def slang(text: str) -> bool:
    """A post-2012 idiom ("vibes", "is fire", "on brand", "lowkey", "no cap", "sus", "rizz",
    "so hype", "literally so down", "flex", "flexing")."""
    return bool(_SLANG.search(text))


def _stem(w: str) -> str:
    for suf in ("ing", "ed", "es", "s"):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            w = w[:-len(suf)]
            break
    return w[:-1] if w.endswith("e") and len(w) > 3 else w


def _content_words(text: str) -> set[str]:
    return {_stem(w) for w in _TOKENS.findall(_fold(text))
            if len(w) >= 3 and w not in _SUPPORT_STOP}


def _of_others(words: list[str]) -> bool:
    """A sentence whose subject is not the speaker: someone else or the guild (_SUBJECTS, or
    "you" opening it or before "been", "did", ...), or nobody at all ("the war has been
    raging")."""
    if _SUBJECTS & set(words):
        return True
    if any(w in ("you", "u") and (i == 0 or words[i + 1:i + 2] and words[i + 1] in _YOU_DID)
           for i, w in enumerate(words)):
        return True
    return not _PERSONS & set(words)


def history_claim(text: str, people: Sequence[str] = (), *, tonight: bool = False) -> bool:
    """The line claims something from the shared past: "remember when", "remember how we",
    "that time you pulled", "last time you", "than you did"; an era or a span ("back in
    vanilla", "since 2005", "for 2 years") claimed of someone else, the guild or a thing
    (_of_others), in a line not about her own life ("i've raided since 2005", "we've been
    married for 10 years", "my daughter's been practicing for 3 years"); or "again" about a
    person's past act, with them (`people`, or you/he/she/they, "you'd" too) before it, or
    told with nobody named in a passive that opens its sentence ("eaten by a gnome again"),
    and no plan ("we", "let's", "try"). Plans are no claim: "by the time we pull", "the day
    we kill lk". `tonight`: the line reacts to a fight that has come before tonight (a second
    wipe on the boss), so its passive "again" is what happened (fix round 1, review M3)."""
    low = _fold(text)
    if _SHARED_CLAIM.search(low):
        return True
    who = {_fold(n) for n in people if n} | _AGAIN_WHO
    every = set(_TOKENS.findall(low))
    family = bool(_HOUSEHOLD & every and _FAMILY & every)
    for m in _SENTENCE.finditer(low):
        sentence = m.group(0)
        words = _TOKENS.findall(sentence)
        # "my" is her own life only when the claim is not about you or us ("you've been
        # stealing my loot for 2 years" is: fix round 2, re-review NB5)
        own = family or bool(_MINE & every and not _YOU_WE & set(words))
        if _ERA_CLAIM.search(sentence) and _of_others(words) and not own:
            return True
        for hit in _AGAIN.finditer(sentence):
            before = _TOKENS.findall(sentence[:hit.start()])
            if before and not _AGAIN_PLAN & set(before) and (
                    who & set(before) or (not tonight and _PASSIVE_AGAIN.match(
                        sentence, 0, hit.start()))):
                return True
    return False


# Words of his question that say how it asks, not what it asks about ("how long has the feud
# been going", "who gets the odd weeks again": fix wave G3).
_ASKING = frozenset("""long going gone gets goes went started start starts begin began begun
first ever anyone anybody happen happened remind tell know knows who's whos what's whats when's
whens where's wheres how's hows""".split())


def subject_words(text: str) -> set[str]:
    """What a question asks about: its content words (stemmed), those of the asking aside
    (_ASKING)."""
    return {s for w in _TOKENS.findall(_fold(text))
            if len(w) >= 3 and w not in _SUPPORT_STOP and w not in _ASKING
            and (s := _stem(w)) not in _SUPPORT_STOP and s not in _ASKING}


def word_counts(texts: Iterable[str]) -> Counter[str]:
    """How many of `texts` hold each content word, as holds reads them (whole stems, never a
    prefix: "war" is not in "warlock")."""
    return Counter(w for t in texts for w in _content_words(t))


def holds(line: str, memory: str, *, rare: Callable[[str], bool], past: bool) -> bool:
    """The memory is on what his question asks about (fix wave G3, rehearsal 3 check 3). A
    shared subject word counts only when it is `rare` across the guild's memories (fix round
    1, review C1: "raid", "night", "guild" and "bank" are in hundreds and match everything).
    A question that asks about the past by its words (`past`: select.asks_the_past) is held by
    a memory sharing a rare word and two subject words, or its only one; any other needs two
    rare shared words."""
    words = subject_words(line)
    shared = words & _content_words(memory)
    rares = sum(1 for w in shared if rare(w))
    if not past:
        return rares >= 2
    return rares >= 1 and len(shared) >= min(2, len(words))


def supported(text: str, support: Sequence[str]) -> bool:
    """A memory in the prompt (or a note the owner wrote) backs the line: one of `support`
    shares two of its content words, or all of them when it has only one."""
    words = _content_words(text)
    if not words:
        return False
    need = min(2, len(words))
    return any(len(words & _content_words(s)) >= need for s in support)


def find_tells(text: str, *, recent_own: Sequence[str] = (), speaker: str = "",
               answers: Sequence[str] = (), others: Sequence[str] = (),
               samples: Sequence[str] = (), support: Sequence[str] | None = None,
               people: Sequence[str] = (), window: Sequence[str] = (),
               held: Sequence[str] = (), tonight: bool = False) -> list[Tell]:
    """Every tell in the text.
    Fatal: FATAL_PHRASES; meta talk (text.META_RE's unambiguous terms only); 2+ list lines;
    dialogue for other people ("\\nName:"); an *action* over 3 words; an exact repeat of one
    of the speaker's own recent lines (3+ words), or of one of the `samples` their prompt
    showed ("sample"); an echo (echoes) of `answers`, the line the turn answers, the words
    that `held` (the memories and facts it showed) hold never counting, or a long run
    (ECHO_RUN words) from `others`, what other members already said to it (a short line two
    people both type is chat); a copy (copies) of any other line of its prompt's chat
    `window`, from anyone, on any kind of turn ("copy": rehearsal 2, one member sent
    another's line word for word).
    Soft: "feel free"; "absolutely" as the opening word; a closing "let me know (if ...)",
    "if you need anything", "just ask", "would you like" or "want me to ... for you"; a near
    repeat (word Jaccard >= 0.8, not exact); post-2012 slang ("slang"); with `support` (the
    memories and notes the
    prompt showed), a claim of shared history (history_claim, `people` being the names in
    the guild, `tonight` a reaction to a fight that has come before tonight) that none of
    them backs (supported): "history", which the pipeline drops when its retry still has it.
    Repairable: emoji, dashes, semicolons, a single list marker, a leading tag with the
    speaker's own name."""
    out: list[Tell] = []
    if (support is not None and history_claim(text, people, tonight=tonight)
            and not supported(text, support)):
        out.append(Tell("history", False, True))
    low = _fold(text)
    out += [Tell(f"phrase: {p}", True) for p, rx in _FATAL_RE.items() if rx.search(low)]
    if has_meta(text):
        out.append(Tell("meta", True))
    marks = len(_LIST_MARK.findall(text))
    if marks >= 2:
        out.append(Tell("list", True))
    if _DIALOGUE.search(text):
        out.append(Tell("dialogue", True))
    if any(len(m.group(1).split()) > MAX_ACTION_WORDS for m in _ACTION.finditer(text)):
        out.append(Tell("action", True))
    if any(_repeat(text, s) == "exact" for s in samples):
        out.append(Tell("sample", True))
    repeats = {_repeat(text, own) for own in recent_own}
    if "exact" in repeats:
        out.append(Tell("repeat", True))
    elif "near" in repeats:
        out.append(Tell("near repeat", False, True))
    kept = frozenset(w for h in held for w in normalize_words(h).split()
                     if len(w) >= 3 and w not in _SUPPORT_STOP)
    if any(echoes(text, line, allowed=kept & set(normalize_words(line).split()))
           for line in answers) or any(echoes(text, line, share=False) for line in others):
        out.append(Tell("echo", True))
    if any(copies(text, line) for line in window):
        out.append(Tell("copy", True))
    if _FEEL_FREE.search(low):
        out.append(Tell("phrase: feel free", False, True))
    if slang(text):
        out.append(Tell("slang", False, True))
    if _OPENS_ABSOLUTELY.match(low):
        out.append(Tell("opener: absolutely", False, True))
    clause = _final_clause(text)
    out += [Tell(f"closer: {name}", False, True) for name, rx in _CLOSERS if rx.search(clause)]
    if EMOJI_RE.search(text):
        out.append(Tell("emoji", False))
    if _DASH.search(text):
        out.append(Tell("dash", False))
    if _SEMI.search(text):
        out.append(Tell("semicolon", False))
    if marks == 1:
        out.append(Tell("list marker", False))
    m = NAME_PREFIX_RE.match(text)
    if m and speaker and m.group("name").casefold() == speaker.casefold():
        out.append(Tell("name prefix", False))
    return out


def repair(text: str, *, speaker: str = "", aliases: Sequence[str] = (),
           him: Sequence[str] = ()) -> str:
    """Fix the repairable tells: drop emoji, what the model copied from the prompt
    (strip_copied, with the speaker's own name and `aliases`, their real first name, and a
    command or handle at him, `him` being his names) and list markers; a dash becomes ", "
    and a semicolon ". "."""
    t = strip_copied(strip_emoji(text), (speaker, *aliases), him=him)
    t = _LIST_MARK.sub("", t)
    t = _DASH.sub(", ", t)
    t = _SEMI.sub(". ", t)
    lines = (" ".join(line.split()) for line in t.split("\n"))
    t = "\n".join(line for line in lines if line)
    return t.strip().lstrip(", ").rstrip(", ")


_QUIET = re.compile(r"(?:says? |stays? |keeps? |remains? )?"
                    r"(?:nothing|silent|silence|quiet|no (?:reply|response|comment))")


def is_silence(text: str) -> bool:
    """The model chose not to speak: "(nothing)" however it is dressed up (a bare "Nothing."
    too; "nothing much" is speech), any reply that is a parenthesised aside, a bracketed stage
    direction ("[sighs]", "[Nothing]"), or one without a letter or digit ("..."). A reply that
    is only an item ([Shadowmourne], in title case) is speech."""
    t = text.strip().strip("\"'“”").strip()
    if not any(c.isalnum() for c in t):
        return True
    if t.casefold().startswith(SILENT.rstrip(")")) or t.casefold().strip(".! ") == "nothing":
        return True
    if t[0] == "(" and t[-1] == ")":
        return True
    if t[0] == "[" and t[-1] == "]" and "]" not in t[1:-1]:
        inner = t[1:-1].strip(" .!")
        return not any(c.isupper() for c in inner) or bool(_QUIET.fullmatch(_fold(inner)))
    return t[0] == "*" and t[-1] == "*" and bool(_QUIET.fullmatch(_fold(t.strip("* .")).strip()))
