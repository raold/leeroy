"""How a member types. The model writes the words; this applies the persona's typing in code:
case, end punctuation, abbreviations, emoticons, seeded typos, line length and splitting.

A line is never cut mid-phrase (final fix wave, from the dress rehearsal). The words are cut
into clauses: at sentence ends, at commas, semicolons and colons, after an emoticon or a chat
interjection ("lol"), and before "but", "and", "so", "cause" or "or". The clauses are laid out
on at most the turn's lines (one, for a persona who never splits a line), each line at most
the persona's words_per_line[1] words times the phase's and the state's length multipliers; a
clause with no break inside may run to SOFT_OVER times that. When it all doesn't fit, whole
clauses give way: first a filler opener ("lol", "yeah"), then from the end, preferring to stop
at a sentence end. Only a first clause with no break at all is cut between words, and never
on a little word ("the", "is"). A text that fits on one line is split in two (split_rate)
only at a sentence end or a comma, semicolon or colon.

Every outgoing line is plain ASCII: emoji and other symbols go, accents and typographic marks
are folded. Abbreviations only ever shorten: a map written the wrong way round ("lol" ->
"laughing out loud") is ignored. Item links ("[Shadowmourne]") are kept as written and never
cut or split through."""
import itertools
import random
import re
import unicodedata
from dataclasses import dataclass

from leeroy_mind.personas.schema import Typing
from leeroy_mind.protocol import MAX_TEXT_BYTES, sanitize_say_text
from leeroy_mind.talk.filters import strip_copied
from leeroy_mind.personas.schema import is_mark
from leeroy_mind.talk.states import EFFECTS, RAID_CALLS, Altered, Effects
from leeroy_mind.talk.turns import TurnKind
from leeroy_mind.talk.words import COMMON_WORDS

SOME_DROP_P = 0.5  # end_punct "some": how often a sentence-final period is left off
SHOUT_MIN = 3  # an all-caps word this long is shouting and keeps its case
TYPO_MIN = 4
TYPO_DOUBLE_P = 0.1  # of typos, a doubled letter; the rest swap or drop one
TYPO_TRIES = 4
SOFT_OVER = 1.3  # a clause with no break inside may run this far over a line's cap
_LETTERS = re.compile(r"[^\W\d_]+")
_TYPO_WORD = re.compile(rf"[A-Za-z]{{{TYPO_MIN},}}")
_LINK = re.compile(r"\[[^\]\n]*\]")
_TOKEN = re.compile(r"\S*\[[^\]\n]*\]\S*|\S+")  # a word, or a link with what touches it

# What NFKD leaves outside ASCII, folded by hand; anything else non-ASCII is dropped.
_FOLD = str.maketrans({
    **dict.fromkeys("‘’‚‛′", "'"),
    **dict.fromkeys("“”„″", '"'),
    **dict.fromkeys("‒–—―−", "-"),
    "ı": "i", "ß": "ss", "æ": "ae", "Æ": "AE", "ø": "o",
    "Ø": "O", "œ": "oe", "Œ": "OE", "đ": "d", "Đ": "D",
    "ł": "l", "Ł": "L", "þ": "th", "Þ": "Th", "ð": "d",
    "Ð": "D"})

# How strong a break between two clauses is, and what laying a line break there costs.
SENT, CLAUSE, CONJ = 3, 2, 1
_BREAK_COST = {SENT: 1, CLAUSE: 2, CONJ: 4}
_INTERJECTIONS = frozenset({"lol", "lmao", "lmfao", "rofl", "haha", "hahaha", "hehe", "heh",
                            "xd", "jk", "omg", "ugh"})
_CONJUNCTIONS = frozenset({"but", "and", "so", "cause", "cuz", "coz", "because", "or"})
# A clause of these alone (three words at most) is a filler opener.
_FILLER = _INTERJECTIONS | frozenset({
    "yeah", "yea", "ya", "yep", "yup", "oh", "ah", "aw", "aww", "ok", "okay", "kk", "k", "so",
    "well", "hmm", "hm", "um", "uh", "honestly", "wow", "man", "dude", "anyway", "anyways"})
# A line cut between words never ends on one of these.
_DANGLING = frozenset("""a an the of to in on at for with from by into about and or but so
because cause if when than as is was are were be been am it's that's i'm you're we're
they're he's she's my your our their its this that these those like very really just gonna
wanna i""".split())
_EMOTICON = re.compile(r"[:;=8xX][-'^]?[)(\]\[DPpOo3*/\\|]+|[)(\]\[DPp][-']?[:;=]|<3+|\^\^"
                       r"|o7|-_-|;_;|:'\(")


# A slash command the model typed into a line ("happy birthday dunna! /hug /hug"): the game
# would print it as text, so it goes out as the emote instead (rehearsal 2). mod-leeroy knows
# these by name (LeeroyEmotes.cpp).
EMOTE_COMMANDS = frozenset({
    "hug", "cheer", "dance", "wave", "bow", "kiss", "wink", "laugh", "applaud", "clap", "cry",
    "sigh", "smile", "grin", "blush", "giggle", "hello", "salute", "flex", "roar", "cuddle",
    "thank", "congratulate", "golfclap", "facepalm", "highfive"})
# The romantic ones go only to him, or to nobody (the pipeline's _send_emotes).
ROMANCE_EMOTES = frozenset({"kiss", "wink", "cuddle", "blush"})
_SLASH = re.compile(r"(?<!\S)/(?P<emote>[A-Za-z]+)(?=[\s.!?,]|$)")
_ONLY_COMMANDS = re.compile(r"(?:[\s.!?,]*/[A-Za-z]+(?:\s+[A-Za-z]+)?)*[\s.!?,]*$")
_COMMAND = re.compile(r"(?<!\S)/([A-Za-z]+)")


def command_only(text: str) -> bool:
    """The line is nothing but slash commands the game would print as text ("/w Belra",
    "/roll", "/invite arkon"), none of them an emote: there is nothing to say (rehearsal 3,
    finding 5). A line of emotes is its emotes (split_emotes, emote_words)."""
    commands = _COMMAND.findall(text)
    return (bool(commands) and bool(_ONLY_COMMANDS.fullmatch(text))
            and not any(c.casefold() in EMOTE_COMMANDS for c in commands))


def emote_words(text: str) -> str:
    """The emote commands as their words ("/hug you later" -> "hug you later"): a whisper's,
    which sends no emote (fix round 2, re-review NB6)."""
    return _SLASH.sub(lambda m: m.group("emote") if m.group("emote").casefold()
                      in EMOTE_COMMANDS else m.group(0), text)


def split_emotes(text: str, names: dict[str, int]) -> tuple[str, list[tuple[str, int]]]:
    """The words without their emote commands, and the emotes once each, in order, with the
    member a command is aimed at (`names`: lower-case name -> guid, the word right after it;
    0 when none). A command is an emote at the start of the line or among the commands that
    end it ("happy birthday dunna! /hug /hug"); inside a sentence it is its word: "i'd /kiss
    you right now" is "i'd kiss you right now" (fix round 1, review minor 8)."""
    emotes: dict[str, int] = {}
    out, last = [], 0
    for m in _SLASH.finditer(text):
        emote = m.group("emote").casefold()
        if emote not in EMOTE_COMMANDS:
            continue
        end, target = m.end(), 0
        nxt = re.match(r"\s+([A-Za-z]+)", text[end:])
        if nxt and nxt.group(1).casefold() in names:
            target, end = names[nxt.group(1).casefold()], end + nxt.end()
        out.append(text[last:m.start()])
        if text[:m.start()].strip() and not _ONLY_COMMANDS.fullmatch(text, end):
            out.append(m.group("emote"))  # inside a sentence: the word, not the emote
            last = m.end()
            continue
        emotes.setdefault(emote, target)
        last = end
    if not out:
        return text, []
    out.append(text[last:])
    return " ".join("".join(out).split()), list(emotes.items())


@dataclass(frozen=True, slots=True)
class Styled:
    """One typed line, and the same words before the abbreviations, the typos and the
    emoticon: what the speaker's own last lines show and repeats are judged on."""
    line: str
    plain: str


def to_ascii(text: str) -> str:
    """Plain ASCII: typographic marks and letters folded ("é" -> "e", "ß" -> "ss", "—" -> "-",
    "…" -> "..."), everything else (emoji, symbols, other scripts) dropped."""
    t = unicodedata.normalize("NFKD", text.translate(_FOLD))
    return "".join(c for c in t if c.isascii())


def _strip_quotes(text: str) -> str:
    t = text.strip()
    while len(t) >= 2 and t[0] == '"' and t[-1] == '"':
        t = t[1:-1].strip()
    return t


def _clean(text: str, names: tuple[str, ...]) -> str:
    """Step 1: plain ASCII, wrapping quotes and what the model copied from the prompt's chat
    frame (filters.strip_copied: an age, a channel tag, a tag with one of `names`, the
    speaker's own)."""
    return " ".join(_strip_quotes(strip_copied(to_ascii(text), names)).split())


def _outside_links(text: str, fn) -> str:
    out, last = [], 0
    for m in _LINK.finditer(text):
        out += [fn(text[last:m.start()]), m.group(0)]
        last = m.end()
    out.append(fn(text[last:]))
    return "".join(out)


def _lower_word(text: str) -> str:
    return _LETTERS.sub(lambda m: m.group(0) if len(m.group(0)) >= SHOUT_MIN
                        and m.group(0).isupper() else m.group(0).lower(), text)


def _lower(text: str) -> str:
    """Lower case, but shouting keeps its caps and a mark its own case ("xD", ":D")."""
    return re.sub(r"\S+", lambda m: m.group(0) if _EMOTICON.fullmatch(
        m.group(0).rstrip(".,!?")) else _lower_word(m.group(0)), text)


def _case(text: str, mode: str) -> str:
    if mode == "lower":
        return _outside_links(text, _lower)
    if mode == "proper" and text:
        return text[0].upper() + text[1:]
    return text


def _end_punct(text: str, mode: str, drop_some: bool) -> str:
    """none: no sentence-final "." or "!" ("?" and a trailing "..." stay). some: the same, on
    about half the lines. full: a line ending in a letter or digit gets a "."."""
    if not text:
        return text
    if mode == "full":
        return text + "." if text[-1].isalnum() else text
    if mode == "none" or (mode == "some" and drop_some):
        if text.endswith("..") or text.endswith("!!"):
            return text
        return text[:-1].rstrip() if text[-1] in ".!" else text
    return text


def _keep_case(text: str, words: frozenset[str], before: str) -> str:
    """Each of `words` keeps its own spelling where it stood capitalised in `before` (the same
    text before the casing step, letter for letter), as a whole word outside links. A use
    written in lower case is left the ordinary word it may be (Task 16 re-review: "who pays
    the bill" became "who pays the Bill" when the name was Bill)."""
    spelled = {w.casefold(): w for w in words if w.strip()}
    if not spelled or len(before) != len(text):
        return text
    pat = re.compile(r"(?<!\w)(?:" + "|".join(
        re.escape(w) for w in sorted(spelled, key=len, reverse=True)) + r")(?!\w)",
        re.IGNORECASE)
    links = [m.span() for m in _LINK.finditer(before)]
    out, last = [], 0
    for m in pat.finditer(before):
        a, b = m.span()
        if not m.group(0)[:1].isupper() or any(x < b and a < y for x, y in links):
            continue
        out += [text[last:a], spelled[m.group(0).casefold()]]
        last = b
    out.append(text[last:])
    return "".join(out)


def _abbreviate(text: str, abbreviations: dict[str, str]) -> str:
    """The persona's abbreviations on word boundaries, outside links, in any case. Only an
    entry that shortens applies: one written the wrong way round is ignored."""
    table = {}
    for k, v in abbreviations.items():
        key, val = to_ascii(k).strip().casefold(), to_ascii(v).strip()
        if key and val and len(val) <= len(key):
            table[key] = val
    if not table:
        return text
    keys = sorted(table, key=len, reverse=True)
    pat = re.compile(r"(?<!\w)(?:" + "|".join(re.escape(k) for k in keys) + r")(?!\w)",
                     re.IGNORECASE)
    return _outside_links(text, lambda seg: pat.sub(
        lambda m: table.get(m.group(0).casefold(), m.group(0)), seg))


def _typo(word: str, rng: random.Random) -> str:
    """Swap two neighbouring letters or drop one, now and then double one (TYPO_DOUBLE_P:
    rehearsal 2's "reead" and "thhat" looked machine-made); the first letter stays, and the
    result is never another real word (words.COMMON_WORDS: "still" became "sill"). A word
    every try of which is a real word is left as it is."""
    for _ in range(TYPO_TRIES):
        r = rng.random()
        i = rng.randrange(1, len(word) - 1)
        if r < (1 - TYPO_DOUBLE_P) / 2:
            if word[i] == word[i + 1]:
                continue
            out = word[:i] + word[i + 1] + word[i] + word[i + 2:]
        elif r < 1 - TYPO_DOUBLE_P:
            out = word[:i] + word[i + 1:]
        else:
            out = word[:i] + word[i] + word[i:]
        if out.casefold() not in COMMON_WORDS:
            return out
    return word


def _typos(text: str, p: float, rng: random.Random, protect: frozenset[str]) -> str:
    if p <= 0:
        return text
    keep = {w.casefold() for w in protect}
    links = [m.span() for m in _LINK.finditer(text)]
    words = [m for m in _TYPO_WORD.finditer(text) if m.group(0).casefold() not in keep
             and not any(a <= m.start() < b for a, b in links)]
    # Every eligible word is decided before any typo is made, so a higher rate on the same
    # seed only ever adds typos.
    hits = [m for m in words if rng.random() < p]
    out, last = [], 0
    for m in hits:
        out += [text[last:m.start()], _typo(m.group(0), rng)]
        last = m.end()
    out.append(text[last:])
    return "".join(out)


# -- laying the words out on lines -------------------------------------------------------------

Unit = tuple[list[str], int]  # a clause's words, and the strength of the break after it


def _bare(token: str) -> str:
    return token.strip(".,!?;:\"'()").casefold()


def _break_after(token: str, clause_len: int, nxt: str, left_after_next: int) -> int:
    if _EMOTICON.fullmatch(token):
        return SENT
    end = token.rstrip("\"')")
    if end.endswith((".", "!", "?")):
        return SENT
    if end.endswith((",", ";", ":")):
        return CLAUSE
    if _bare(token) in _INTERJECTIONS:
        return CLAUSE
    if _bare(nxt) in _CONJUNCTIONS and clause_len >= 2 and left_after_next >= 1:
        return CONJ
    return 0


def _units(words: list[str]) -> list[Unit]:
    out: list[Unit] = []
    cur: list[str] = []
    for i, w in enumerate(words):
        cur.append(w)
        if i + 1 < len(words):
            strength = _break_after(w, len(cur), words[i + 1], len(words) - i - 2)
            if strength:
                out.append((cur, strength))
                cur = []
    if cur:
        out.append((cur, 0))
    return out


def _n(units: list[Unit]) -> int:
    return sum(len(ws) for ws, _ in units)


def _soft(cap: int) -> int:
    return max(cap, int(cap * SOFT_OVER + 1e-9))


def _partition(units: list[Unit], cap: int, max_lines: int) -> list[list[Unit]] | None:
    """The fewest lines that hold every unit, at most max_lines: each line at most cap words,
    or one unbroken clause of at most _soft(cap). Among those, the cheapest breaks (sentence
    ends first), then the shortest longest line. None when they don't fit."""
    n, soft = len(units), _soft(cap)
    for breaks in range(min(max_lines, n)):
        best = None
        for cuts in itertools.combinations(range(1, n), breaks):
            bounds = (0, *cuts, n)
            lines = [units[a:b] for a, b in zip(bounds, bounds[1:])]
            if not all(_n(x) <= cap or (len(x) == 1 and _n(x) <= soft) for x in lines):
                continue
            key = (sum(_BREAK_COST[units[c - 1][1]] for c in cuts), max(map(_n, lines)), cuts)
            if best is None or key < best[0]:
                best = (key, lines)
        if best is not None:
            return best[1]
    return None


def _is_filler(words: list[str]) -> bool:
    return len(words) <= 3 and all(_bare(w) in _FILLER or _EMOTICON.fullmatch(w)
                                   for w in words)


def _cut(words: list[str], soft: int) -> list[str]:
    """A first clause too long for any line: its filler opener goes, then whatever runs past
    `soft` words, then any little word left at the end."""
    ws = list(words)
    while len(ws) > 1 and _bare(ws[0]) in _FILLER:
        ws.pop(0)
    if len(ws) <= soft:
        return ws
    ws = ws[:soft]
    while len(ws) > 1 and _bare(ws[-1]) in _DANGLING:
        ws.pop()
    return ws


def _fit(words: list[str], cap: int, max_lines: int) -> list[list[Unit]]:
    """Whole clauses on at most max_lines lines (the module docstring has the rules)."""
    units = _units(words)
    got = _partition(units, cap, max_lines)
    while got is None and len(units) > 1 and _is_filler(units[0][0]):
        units = units[1:]
        got = _partition(units, cap, max_lines)
    if got is not None:
        return got
    fits = [(k, p) for k in range(len(units) - 1, 0, -1)
            if (p := _partition(units[:k], cap, max_lines)) is not None]
    if fits:
        k, longest = fits[0]
        sentence = next(((j, p) for j, p in fits if units[j - 1][1] == SENT), None)
        # stop at a sentence end when that keeps two thirds of what the longest fit keeps
        if sentence is not None and 3 * _n(units[:sentence[0]]) >= 2 * _n(units[:k]):
            return sentence[1]
        return longest
    return [[(_cut(units[0][0], _soft(cap)), 0)]]


def _split_one(line: list[Unit], min_words: int) -> list[list[Unit]] | None:
    """A line that fits, typed as two: at its strongest sentence end or punctuation nearest
    the middle, each part at least max(2, min_words) words; None when there is no such break."""
    lo, total, best = max(2, min_words), _n(line), None
    for c in range(1, len(line)):
        strength, left = line[c - 1][1], _n(line[:c])
        if strength < CLAUSE or left < lo or total - left < lo:
            continue
        key = (-strength, abs(left - total / 2), c)
        if best is None or key < best[0]:
            best = (key, c)
    return None if best is None else [line[:best[1]], line[best[1]:]]


# A wink, a kiss or a heart: never added to a line to or naming a member.
_WINKY = re.compile(r";-?[)DPp]+|<3+|:-?\*|;\*")
# A line ending in one of these, or in a mark, gets no mark added ("see you soon :) xD").
_ENDINGS = _INTERJECTIONS | frozenset({"aww", "aw", "gg", "brb", "ily", "o7", "ty", "gz"})


def _ends_marked(line: str) -> bool:
    words = line.split()
    if not words:
        return False
    last = words[-1].rstrip(".,!?")
    return bool(_EMOTICON.fullmatch(last)) or _bare(words[-1]) in _ENDINGS


def line_cap(t: Typing, eff: Effects, length_mult: float = 1.0) -> int:
    """A line's word cap: words_per_line[1] x the phase's and the state's length multipliers."""
    return max(1, int(t.words_per_line[1] * length_mult * eff.length_mult + 1e-9))


def turn_lines(t: Typing, max_lines: int) -> int:
    """How many lines a turn of this persona's may take: the turn's, or one for a persona who
    never splits a line. A bit (TurnKind.BIT) always has its own."""
    return max(1, max_lines) if t.split_rate > 0 else 1


def style_lines(text: str, t: Typing, eff: Effects, rng: random.Random, *,
                length_mult: float = 1.0, protect: frozenset[str] = frozenset(),
                speaker: str = "", kind: TurnKind | None = None,
                keep_case: frozenset[str] = frozenset(), max_lines: int = 2,
                aliases: tuple[str, ...] = (), force_lines: bool = False,
                no_emoticon: bool = False, wink_ok: bool = True) -> list[Styled]:
    """apply_style's lines with their plain words (Styled). `force_lines`: the turn may take
    all of max_lines whatever the persona's split_rate (a whisper to him at the explicit band
    runs to a line or two). `no_emoticon`: no mark is added (a line that carried her romance
    text, a wipe line: rehearsal 2's "dream of me ;) lmao", "great aww"). Without `wink_ok`
    (a line to or naming a member) no wink, kiss or heart is added, a smile still may be (fix
    round 1, review I2: ";)" went onto a line the member screen had passed)."""
    if kind in RAID_CALLS:
        eff = EFFECTS[Altered.SOBER]
    body = _clean(text, tuple(x for x in (speaker, *aliases) if x))
    if not body:
        return []
    keep = frozenset(to_ascii(w) for w in keep_case)
    body = _keep_case(_case(body, t.case), keep, body)
    drop_some = t.end_punct == "some" and rng.random() < SOME_DROP_P
    # only marks are added (a stored word among them is a word: schema.is_mark)
    emoticons = [e for e in (to_ascii(x).strip() for x in t.emoticons) if e and is_mark(e)
                 and (wink_ok or not _WINKY.fullmatch(e))]
    emoticon = ""
    if emoticons and not no_emoticon and rng.random() < t.emoticon_rate:
        emoticon = rng.choice(emoticons)
    lines = (max(1, max_lines) if kind is TurnKind.BIT or force_lines
             else turn_lines(t, max_lines))
    split = rng.random() < t.split_rate
    laid = _fit(_TOKEN.findall(body), line_cap(t, eff, length_mult), lines)
    if split and lines > 1 and len(laid) == 1:
        laid = _split_one(laid[0], t.words_per_line[0]) or laid
    protected = frozenset(to_ascii(w) for w in protect) | keep
    out: list[Styled] = []
    for i, units in enumerate(laid):
        ws = [w for clause, _ in units for w in clause]
        while ws and not ws[-1].rstrip(",;:-"):
            ws.pop()  # a dangling "," or "-"
        if not ws:
            continue
        plain = _end_punct(" ".join(ws).rstrip(",;:"), t.end_punct, drop_some)
        if i > 0 and t.case == "proper":
            plain = _case(plain, "proper")
        typed = _typos(_abbreviate(plain, t.abbreviations), t.typo_rate * eff.typo_mult, rng,
                       protected)
        tail = (f" {emoticon}" if emoticon and i == len(laid) - 1 and not _ends_marked(typed)
                else "")
        while len((typed + tail).encode("utf-8")) > MAX_TEXT_BYTES:
            tokens = _TOKEN.findall(typed)
            if len(tokens) <= 1:
                break
            typed = _end_punct(" ".join(tokens[:-1]).rstrip(",;:"), t.end_punct, drop_some)
        line = sanitize_say_text(typed + tail)
        if line:
            out.append(Styled(line, plain))
    return out


def apply_style(text: str, t: Typing, eff: Effects, rng: random.Random, *,
                length_mult: float = 1.0, protect: frozenset[str] = frozenset(),
                speaker: str = "", kind: TurnKind | None = None,
                keep_case: frozenset[str] = frozenset(), max_lines: int = 2,
                aliases: tuple[str, ...] = ()) -> list[str]:
    """At most turn_lines(t, max_lines) lines, each plain ASCII and through
    sanitize_say_text; [] when nothing is left to say. `protect` (cast and human names) never
    gets a typo. `keep_case` words (the human's real name) keep their spelling through the
    casing where they were written capitalised, and never get a typo, so the stored line
    still shows the name for what it is.
    `speaker` and `aliases` (the speaker's real first name): a leading tag with one of them is
    dropped. CALLOUT and URGENT are typed sober whatever `eff` says. The emoticon is kept off
    the typo and length steps and ends the last line; a line over the bridge's byte limit
    loses whole words, never part of one."""
    return [s.line for s in style_lines(
        text, t, eff, rng, length_mult=length_mult, protect=protect, speaker=speaker, kind=kind,
        keep_case=keep_case, max_lines=max_lines, aliases=aliases)]
