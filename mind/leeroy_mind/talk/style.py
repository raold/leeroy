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

Every line for the game is plain ASCII: emoji and other symbols go, accents and typographic
marks are folded. A Discord line (style_lines' `game` off) keeps them, with no byte cut and no
chat-line cleaning (the Discord store cleans its text); its typing is the same. Abbreviations
only ever shorten: a map written the wrong way round ("lol" -> "laughing out loud") is ignored. Item links ("[Shadowmourne]") are kept as written and never
cut or split through.

With [talk] texture on, the typing is a person's rather than a rule's (the "typing texture"
section below): apostrophes left out and fragments run on as her declared case and punctuation
suggest, shorthand some of the time, a spelling habit or two, and at most one slip a line."""
import functools
import itertools
import random
import re
import unicodedata
from dataclasses import dataclass

from leeroy_mind.personas.schema import Typing
from leeroy_mind.protocol import MAX_TEXT_BYTES, sanitize_say_text
from leeroy_mind.talk.filters import strip_copied
from leeroy_mind.text import EMOJI_RE
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
# A whole word: neither a letter nor an apostrophe (ASCII, or a Discord line's phone ones) on
# either side, so "you" is no word inside "you're".
_APOS = "'’ʼ"
_WB = rf"(?<![\w{_APOS}])"
_WE = rf"(?![\w{_APOS}])"

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
    emoticon: what the speaker's own last lines show and repeats are judged on. `slip` is a
    slip of the fingers in `line` worth a "*word" follow-up (texture only), as typed, and `fix`
    the word she meant; both empty when there is none."""
    line: str
    plain: str
    slip: str = ""
    fix: str = ""


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


def _clean(text: str, names: tuple[str, ...], *, fold: bool = True) -> str:
    """Step 1: plain ASCII (unless not `fold`), wrapping quotes and what the model copied from
    the prompt's chat frame (filters.strip_copied: an age, a channel tag, a tag with one of
    `names`, the speaker's own)."""
    return " ".join(_strip_quotes(strip_copied(to_ascii(text) if fold else text,
                                               names)).split())


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


def _shortening(abbreviations: dict[str, str]) -> dict[str, str]:
    """The persona's entries that shorten, by their casefolded key."""
    table = {}
    for k, v in abbreviations.items():
        key, val = to_ascii(k).strip().casefold(), to_ascii(v).strip()
        if key and val and len(val) <= len(key):
            table[key] = val
    return table


def _abbreviate(text: str, abbreviations: dict[str, str]) -> str:
    """The persona's abbreviations on word boundaries, outside links, in any case. Only an
    entry that shortens applies: one written the wrong way round is ignored. A word joined to
    an apostrophe is no whole word: "you're" never becomes "u're" (repetition audit, 5)."""
    table = _shortening(abbreviations)
    if not table:
        return text
    keys = sorted(table, key=len, reverse=True)
    pat = re.compile(_WB + "(?:" + "|".join(re.escape(k) for k in keys) + ")" + _WE,
                     re.IGNORECASE)
    return _outside_links(text, lambda seg: pat.sub(
        lambda m: table.get(m.group(0).casefold(), m.group(0)), seg))


# What a typo may never make of a word the model wrote otherwise: a swear, a sexual word or a
# slur ("sec" -> "sex", "count" -> "cunt", "bigger" -> a slur; review of #321, P3-1). The guards
# never see the typed line, so the typo step itself refuses them and tries another.
NEVER_TYPED = frozenset("""
anal anus arse ass asses asshole bitch bitches boob boobs butt chink cock cocks coon crap cum
cumming cums cunt cunts damn dick dicks dildo dyke fag fags faggot fuck fucked fucker fucking
fucks gook hell homo horny jizz kike nazi nigga nigger niggers nude nudes penis piss pissed porn
pussy rape raped rapes rapist retard retarded sex sexy shag shit shits shitty slut sluts spic
tit tits titty tranny twat vagina wank wanker whore whores wop
""".split())


def _never(*words: str) -> bool:
    """A typo would type one of NEVER_TYPED."""
    return any(w.casefold() in NEVER_TYPED for w in words)


def _typo(word: str, rng: random.Random) -> str:
    """Swap two neighbouring letters or drop one, now and then double one (TYPO_DOUBLE_P:
    rehearsal 2's "reead" and "thhat" looked machine-made); the first letter stays, and the
    result is never another real word (words.COMMON_WORDS: "still" became "sill") nor one of
    NEVER_TYPED ("count"). A word every try of which is one of those is left as it is."""
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
        if out.casefold() not in COMMON_WORDS and not _never(out):
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


# -- typing texture ([talk] texture) -----------------------------------------------------------
# How people really type in chat, over the persona's declared typing (dynamics audit F5,
# repetition audit 5): apostrophes left out, fragments run together, real shorthand some of the
# time, a spelling habit or two, and in some lines one slip of the fingers, now and then followed
# by a "*word" (talk/realism.py). A member's habits are fixed (seeded by her name) and drawn
# around her declared case and punctuation: a lower-case typer with no full stops drops nearly
# every apostrophe and runs her fragments on; a "proper" typer keeps both. A line's last full
# stop is a habit too (Texture.stops): most who declare full stops leave it off most of the
# time. Only the typed line changes, except the joined fragments and that last full stop, which
# are punctuation and so in `plain` too.

TEXTURE_SEED = "texture-v1"


@dataclass(frozen=True, slots=True)
class Calibration:
    """The numbers a member's habits are drawn from (texture_of). Fitted on 2026-11-17 to the one
    human sample there is, the owner's own typed lines, with the Turing stage's features and its
    stylometric AUC (turing/README.md, "Calibrating the texture"): the cast's average sits near
    his rates while members differ. Most leave out most apostrophes and their last full stop; a
    minority keep them (careful_apostrophes, and whoever declares proper case and full stops);
    "u" for "you" is a habit of a few (texters), not of everyone whose persona lists it."""
    period_typers: float = 0.12  # of members: keep their full stops whatever their case
    period_typers_full: float = 0.35  # the same, of those whose persona declares full stops
    careful_apostrophes: float = 0.12  # of members: keep most apostrophes whatever their case
    careful_keep: float = 0.3  # a careful typer's share of the rate she would have had
    apos_base: float = 0.12  # apostrophes left out: base + span x casual ** pow
    apos_span: float = 0.85
    apos_pow: float = 0.5
    join_span: float = 0.85  # fragments run on: span x casual ** pow
    join_pow: float = 1.3
    comma_base: float = 0.8  # of those, joined with a comma: base - slope x casual
    comma_slope: float = 0.3
    texters: float = 0.15  # of members: type their own one-word shorthand ("u", "rly") as a habit
    light_shorthand: float = 0.08  # the others: this share of that rate
    light_extra: float = 0.25  # and this share of the extra shorthand's
    stops_full: float = 0.15  # a line's last full stop typed: she declares them, no period typer
    stops_some: float = 0.1  # she declares some
    stops_typer: tuple[float, float] = (0.7, 1.0)  # a period typer who declares full stops


CAL = Calibration()
SHORT_FRAGMENT = 6  # words: a fragment this short runs on most readily
LONG_JOIN = 0.45  # two longer sentences run together this much less often
AND_SHARE = 0.15  # of the joins that may take "and", joined by it
INVENTED_SHARE = 0.35  # a persona's own shorthand that no one types ("tht"): this much of her rate
SLIP_K = 5.0  # a line's chance of a slip: SLIP_K x typo_rate x the state's typo_mult
SLIP_CAP = 0.6
SLIP_FULL = 6  # words: a shorter line has a smaller chance, in proportion
DISCORD_SLIP = 0.8  # Discord, often on a phone with autocorrect: fewer slips, and other kinds
FIRST_LETTER = 0.08  # of slips that can, how often the first letter is hit
FIX_SHARE = (0.12, 0.32)  # of slips worth it, followed by "*word": a casual .. a careful typer
_CASE_CASUAL = {"lower": 1.0, "mixed": 0.5, "proper": 0.0}
_PUNCT_CASUAL = {"none": 1.0, "some": 0.5, "full": 0.0}

_CONTRACTIONS = """i'm i've i'll i'd don't doesn't didn't can't couldn't won't wouldn't shouldn't
isn't aren't wasn't weren't haven't hasn't hadn't ain't it's that's what's there's here's who's
how's where's let's you're you've you'll you'd they're they've they'll they'd we've he's she's
he'd y'all c'mon would've should've could've it'll that'll""".split()
# Never dropped: without the apostrophe each is another word ("were", "well", "hell", "shed").
KEEP_APOSTROPHE = frozenset({"we're", "we'll", "he'll", "she'll", "we'd", "she'd"})


def _apos_pattern(word: str, optional: bool = False) -> str:
    return re.escape(word).replace("'", f"[{_APOS}]" + ("?" if optional else ""))


_CONTRACTION = re.compile(_WB + "(?:" + "|".join(
    _apos_pattern(c) for c in sorted(_CONTRACTIONS, key=len, reverse=True)) + ")" + _WE,
    re.IGNORECASE)

# "gonna" only for "going to" before a verb: "going to bed" stays.
_GONNA = """be do get go have make try need take see say tell kill pull tank heal raid run wipe
log play eat sleep buy start stop ask call help win lose give put check come leave head grab
hit miss stay wait sit think look find keep let send show bring cry die""".split()
# Real chat shorthand: (pattern, shorthand, weight of being one of a casual typer's own).
SHORTHAND = (
    ("you", "u", 1.0), (f"your|you[{_APOS}]re|youre", "ur", 1.0), ("right now", "rn", 2.5),
    (f"i don[{_APOS}]?t know", "idk", 3.0), ("to be honest", "tbh", 2.0),
    ("not (?:gonna|going to) lie", "ngl", 1.5), ("in my opinion", "imo", 1.0),
    ("of course", "ofc", 1.0), ("probably", "prob", 2.0), ("definitely", "def", 1.5),
    ("whatever", "w/e", 0.5), ("because", "bc", 2.0), ("though", "tho", 3.0),
    (r"going to(?= (?:" + "|".join(_GONNA) + r")\b)", "gonna", 4.0), ("want to", "wanna", 3.5))
# What tells a persona's own entry for real shorthand from one no one types ("tht", "wt", "nt");
# a multi-word key shortened to its initials ("damage per second" -> "dps") is real too.
REAL_SHORTHAND = frozenset("""u ur rn idk tbh ngl imo imho ofc prob probs def w/e bc cuz cos tho
gonna wanna gotta lol lmao lmfao rofl omg wtf brb afk gg wp gl hf ty thx tyvm np pls plz k kk ok
y ya yea nvm btw fyi irl jk smh fr ily omw gtg g2g cya ttyl idc ikr iirc tbf dm w/ w/o rly sry
abt atm asap bday thurs innit brill dps hps tps aoe cd gcd oom dk dh gm rl mt ot cc dkp crit int
str agi sta spi sp ap arp mp5 lfg lfm lfd pug hc nm rdy ench ooc""".split())

# Spelling habits a member may have, two or three of them, typed some of the time.
_SPELLING = (
    ("definitely", "definately"), ("a lot", "alot"), ("weird", "wierd"), ("receive", "recieve"),
    ("separate", "seperate"), ("believe", "beleive"), ("tomorrow", "tommorow"),
    ("until", "untill"), ("really", "realy"), ("probably", "probly"),
    ("especially", "expecially"), ("basically", "basicly"), ("literally", "literaly"),
    ("finally", "finaly"), ("interesting", "intresting"), ("embarrassing", "embarassing"),
    ("necessary", "neccessary"), ("immediately", "immediatly"), ("which", "wich"),
    ("every time", "everytime"), ("no one", "noone"), ("a while", "awhile"),
    ("apparently", "apparantly"), ("beginning", "begining"), ("disappointed", "dissapointed"),
    ("calendar", "calender"), ("achievement", "acheivement"), ("argument", "arguement"),
    ("occasionally", "occasionaly"), ("guarantee", "garantee"), ("tonight", "tonite"),
    ("whether", "wether"))
_LOOSE = (("you're", "your"), ("than", "then"), ("lose", "loose"))  # casual typers only
_FINGERS = (("the", "teh"), ("and", "adn"), ("just", "jsut"), ("what", "waht"), ("that", "taht"),
            ("with", "wiht"), ("have", "ahve"), ("know", "konw"), ("about", "baout"),
            ("think", "thnik"))  # casual typers only
HABIT_RATE = {"spelling": 0.7, "loose": 0.45, "fingers": 0.15}

# A slip's kinds and how often each: at a keyboard, and on a phone (Discord) with autocorrect.
_KEYS = ("auto", "near", "drop", "swap", "double", "merge", "shift")
_GAME_KINDS = (0.0, 0.30, 0.22, 0.22, 0.08, 0.14, 0.04)
_PHONE_KINDS = (0.35, 0.20, 0.10, 0.10, 0.05, 0.15, 0.05)
_FIXABLE = frozenset({"auto", "near", "drop", "swap"})
# Autocorrect's wrong real words (Discord).
_AUTOCORRECT = {
    "from": ("form",), "form": ("from",), "were": ("where",), "where": ("were",),
    "than": ("then",), "then": ("than",), "though": ("through", "thought"),
    "through": ("though",), "quite": ("quiet",), "quiet": ("quite",), "lose": ("loose",),
    "loose": ("lose",), "its": ("it's",), "well": ("we'll",), "raid": ("rain",),
    "heal": ("head",), "loot": ("lot",), "tank": ("thank",), "wipe": ("wife",),
    "gear": ("hear",), "mana": ("mama",), "pull": ("pill",), "boss": ("bass",)}
# Little words a slip in which nobody stops to correct.
_SMALL = frozenset("""that this with have from they them then than when what were your just been
will would could should there their about into some like over also only even very much more
here well back still""".split())
_ROWS = ("qwertyuiop", "asdfghjkl", "zxcvbnm")


def _keyboard() -> dict[str, tuple[str, str]]:
    """Each letter's neighbours on a QWERTY keyboard: on its row, and above and below."""
    out = {}
    for r, row in enumerate(_ROWS):
        for i, ch in enumerate(row):
            side = "".join(row[j] for j in (i - 1, i + 1) if 0 <= j < len(row))
            diag = "".join(_ROWS[rr][j] for rr, js in ((r - 1, (i, i + 1)), (r + 1, (i - 1, i)))
                           if 0 <= rr < len(_ROWS) for j in js if 0 <= j < len(_ROWS[rr]))
            out[ch] = (side, diag)
    return out


_NEAR = _keyboard()
_DOTTED = frozenset("mr. mrs. ms. dr. st. vs. etc. jr. sr. approx.".split())
_AND_STARTS = frozenset("i we you he she they it that this there the my our".split())
_I_FORMS = frozenset({"i", "i'm", "i'll", "i've", "i'd"})


@dataclass(frozen=True, slots=True)
class Texture:
    """One member's typing habits ([talk] texture), fixed per member (texture_of)."""
    casual: float  # 0 a careful typer (proper case, full stops) .. 1 lower case, no punctuation
    apostrophes: float  # how often she leaves out a contraction's apostrophe
    join: float  # how often two fragments run on ("x. y" -> "x, y", "x y", "x and y")
    commas: float  # of those, joined with a comma
    shorthand: float  # how often she uses one of her own real shorthand entries
    extra: tuple[int, ...] = ()  # SHORTHAND entries she uses beyond her own list
    extra_rate: float = 0.0
    habits: tuple[tuple[str, str, float], ...] = ()  # (word, her spelling, how often)
    stops: float = 0.5  # how often she types a line's last full stop, when she declares any


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def casualness(t: Typing) -> float:
    """0 for proper case and full stops .. 1 for lower case and no end punctuation."""
    return (_CASE_CASUAL.get(t.case, 0.5) + _PUNCT_CASUAL.get(t.end_punct, 0.5)) / 2


def fix_share(t: Typing) -> float:
    """How often she follows a slip worth it with "*word": a careful typer more often."""
    lo, hi = FIX_SHARE
    return hi - (hi - lo) * casualness(t)


def texture_of(t: Typing, who: str) -> Texture:
    """Her habits, the same every time for the same name and declared typing."""
    return _texture(who, t.case, t.end_punct, t.typo_rate > 0)


@functools.lru_cache(maxsize=4096)
def _texture(who: str, case: str, end_punct: str, typos: bool, cal: Calibration = CAL) -> Texture:
    rng = random.Random(f"{TEXTURE_SEED}:{who}")
    casual = (_CASE_CASUAL.get(case, 0.5) + _PUNCT_CASUAL.get(end_punct, 0.5)) / 2
    period = rng.random() < (cal.period_typers_full if end_punct == "full" else cal.period_typers)
    careful = rng.random() < cal.careful_apostrophes
    apostrophes = _clamp(cal.apos_base + cal.apos_span * casual ** cal.apos_pow
                         + rng.uniform(-.08, .08), 0, .97)
    join = _clamp(cal.join_span * casual ** cal.join_pow + rng.uniform(-.08, .08), 0, .95)
    commas = _clamp(cal.comma_base - cal.comma_slope * casual + rng.uniform(-.1, .1), .3, .9)
    shorthand = _clamp(0.55 + 0.35 * casual + rng.uniform(-.08, .08), .3, .95)
    pool = list(range(len(SHORTHAND)))
    extra = []
    for _ in range(2 + round(2 * casual)):
        pick = rng.choices(pool, weights=[SHORTHAND[i][2] for i in pool])[0]
        pool.remove(pick)
        extra.append(pick)
    extra_rate = _clamp(0.35 * casual + rng.uniform(-.08, .08), 0, .5)
    kinds = [("spelling", w, s) for w, s in _SPELLING]
    if casual >= 0.5:
        kinds += [("loose", w, s) for w, s in _LOOSE] + [("fingers", w, s) for w, s in _FINGERS]
    habits = []
    for _ in range(rng.choice((2, 2, 3))):
        kind, word, spelt = kinds.pop(rng.randrange(len(kinds)))
        habits.append((word, spelt, HABIT_RATE[kind]))
    # drawn last, so the habits above are the ones the member has always had
    texter, u = rng.random() < cal.texters, rng.random()
    if end_punct == "full":
        lo, hi = cal.stops_typer
        stops = lo + (hi - lo) * u if period else cal.stops_full * (0.5 + u)
    else:
        stops = SOME_DROP_P if period else cal.stops_some * (0.5 + u)
    return Texture(
        casual=casual, apostrophes=apostrophes * (cal.careful_keep if careful else 1),
        join=join * (.3 if period else 1), commas=commas,
        shorthand=shorthand * (1 if texter else cal.light_shorthand),
        extra=tuple(sorted(extra)) if casual >= 0.5 else (),
        extra_rate=(extra_rate if casual >= 0.5 else 0.0) * (1 if texter else cal.light_extra),
        habits=tuple(habits) if typos else (),
        stops=_clamp(stops, 0, 1) if end_punct in ("full", "some") else 0.0)


def _words_cf(words) -> frozenset[str]:
    return frozenset(part.casefold() for w in words for part in w.split())


# What the texture leaves as written, beside item links (review of #321, P3-4): a quoted phrase,
# in straight or curly quotes (a straight single quote only where it cannot be an apostrophe),
# and a web address.
_QUOTED = re.compile(r'"[^"\n]*"|“[^”\n]*”|‘[^’\n]*’'
                     r"|(?<![\w'’])'(?=\S)[^\n]*?(?<=\S)'(?!\w)")
_URL = re.compile(r"\S*(?:://|www\.)\S*", re.IGNORECASE)


def _kept_spans(text: str) -> list[tuple[int, int]]:
    """Links, quoted phrases and web addresses: merged, in order."""
    spans = sorted(m.span() for p in (_LINK, _QUOTED, _URL) for m in p.finditer(text))
    out: list[tuple[int, int]] = []
    for a, b in spans:
        if out and a < out[-1][1]:
            out[-1] = (out[-1][0], max(b, out[-1][1]))
        else:
            out.append((a, b))
    return out


def _sub_each(text: str, pat: re.Pattern, fn) -> str:
    """pat's matches outside links, quoted phrases and web addresses, each replaced by
    fn(match), in order."""
    out, last = [], 0
    for a, b in _kept_spans(text):
        out += [pat.sub(fn, text[last:a]), text[a:b]]
        last = b
    out.append(pat.sub(fn, text[last:]))
    return "".join(out)


def _like(found: str, spelt: str) -> str:
    """`spelt` with `found`'s leading capital."""
    return spelt[:1].upper() + spelt[1:] if found[:1].isupper() else spelt


def _real_entry(key: str, val: str) -> bool:
    initials = "".join(w[0] for w in key.split())
    return val.casefold() in REAL_SHORTHAND or (" " in key and val.casefold() == initials)


def _shorthand(text: str, t: Typing, tex: Texture, rng: random.Random,
               protect: frozenset[str]) -> tuple[str, frozenset[str]]:
    """Her own shortening entries and her real shorthand, each use on its own draw: her real
    entries at tex.shorthand (most uses for a texter, few for anyone else: Calibration.texters;
    a multi-word one, "brb", nearly always for all), one no one types at INVENTED_SHARE of that,
    the extra ones at tex.extra_rate. Also what they write."""
    own = _shortening(t.abbreviations)
    entries = []
    for key, val in own.items():
        rate = tex.shorthand if _real_entry(key, val) else tex.shorthand * INVENTED_SHARE
        if " " in key and _real_entry(key, val):
            rate = max(rate, .9)
        entries.append((_apos_pattern(key, optional=True), val, rate))
    vals = {v.casefold() for v in own.values()}
    for i in tex.extra:
        pat, val, _ = SHORTHAND[i]
        if val not in vals and not any(re.fullmatch(pat, k, re.IGNORECASE) for k in own):
            entries.append((pat, val, tex.extra_rate))
    if not entries:
        return text, frozenset()
    entries.sort(key=lambda e: len(e[0]), reverse=True)
    pat = re.compile(_WB + "(?:" + "|".join(f"(?P<g{i}>{p})" for i, (p, _, _) in
                                             enumerate(entries)) + ")" + _WE, re.IGNORECASE)

    def one(m: re.Match) -> str:
        _, val, rate = entries[int(m.lastgroup[1:])]
        if m.group(0).casefold() in protect or rng.random() >= rate:
            return m.group(0)
        return val
    return _sub_each(text, pat, one), frozenset(v.casefold() for _, v, _ in entries)


def _habits(text: str, tex: Texture, rng: random.Random,
            protect: frozenset[str]) -> tuple[str, frozenset[str]]:
    """Her spelling habits, each use on its own draw. Also the spellings."""
    if not tex.habits:
        return text, frozenset()
    pat = re.compile(_WB + "(?:" + "|".join(f"(?P<h{i}>{_apos_pattern(w)})" for i, (w, _, _) in
                                             enumerate(tex.habits)) + ")" + _WE, re.IGNORECASE)

    def one(m: re.Match) -> str:
        _, spelt, rate = tex.habits[int(m.lastgroup[1:])]
        if m.group(0).casefold() in protect or rng.random() >= rate:
            return m.group(0)
        return _like(m.group(0), spelt)
    return _sub_each(text, pat, one), frozenset(s.casefold() for _, s, _ in tex.habits)


def _apostrophes(text: str, rate: float, rng: random.Random) -> str:
    """Contractions without their apostrophe ("dont", "im", "thats"), each use on its own draw;
    never one that would read as another word (KEEP_APOSTROPHE)."""
    if rate <= 0:
        return text

    def one(m: re.Match) -> str:
        if rng.random() >= rate:
            return m.group(0)
        return re.sub(f"[{_APOS}]", "", m.group(0))
    return _sub_each(text, _CONTRACTION, one)


def _full_stop(token: str) -> bool:
    """A token ending a sentence with one full stop (not "...", a link or "mr.")."""
    return (len(token) > 1 and token.endswith(".") and not token.endswith("..")
            and "." not in token[:-1] and "[" not in token and token.casefold() not in _DOTTED)


def _soften(token: str, protect: frozenset[str]) -> str:
    """A sentence's first word in the middle of a run-on: an ordinary word loses its capital;
    "I", a name, an acronym or shouting keep theirs."""
    core = token.lstrip("\"'(")
    bare = _bare(token)
    letters = _LETTERS.findall(core)
    if (not core[:1].isupper() or bare in _I_FORMS or bare in protect or not letters
            or (len(letters[0]) >= 2 and letters[0].isupper())
            or not (bare in COMMON_WORDS or bare in _FILLER or bare in _AND_STARTS)):
        return token
    at = len(token) - len(core)
    return token[:at] + core[0].lower() + core[1:]


def _run_on(text: str, tex: Texture, rng: random.Random, protect: frozenset[str],
            room: int) -> str:
    """Some of her full stops between fragments, run on the way chat is typed: "x. y" becomes
    "x, y", "x y" or "x and y", short fragments most readily; "?" and "!" stay, and so does
    "...". A fragment of one or two words takes a comma, never a bare space ("stop, pull the
    boss", not "stop pull the boss": review of #321, P3-3). After a mark it is just a space, and
    after "lol" ending a longer fragment too. "and" only between two clauses of three words or
    more, when the line has a word to spare (room). A quoted phrase keeps its full stops."""
    if tex.join <= 0 or ". " not in text:
        return text
    found = list(_TOKEN.finditer(text))
    kept = _kept_spans(text)
    toks = [m.group(0) for m in found]
    ends = [i for i, w in enumerate(toks) if w.rstrip("\"')").endswith((".", "!", "?"))]
    out, n = list(toks), len(toks)
    for b in range(n - 1):
        if not _full_stop(toks[b]) or any(a < found[b + 1].end() and found[b].start() < z
                                          for a, z in kept):
            continue
        prev = max((e for e in ends if e < b), default=-1)
        nxt = min((e for e in ends if e > b), default=n - 1)
        left, right = b - prev, nxt - b
        if rng.random() >= tex.join * (1 if min(left, right) <= SHORT_FRAGMENT else LONG_JOIN):
            continue
        head = out[b][:-1]
        if _EMOTICON.fullmatch(head):
            pass
        elif min(left, right) <= 2:
            head += ","
        elif _bare(head) in _INTERJECTIONS:
            pass
        elif (left >= 3 and right >= 3 and _bare(toks[b + 1]) in _AND_STARTS and n < room
              and rng.random() < AND_SHARE):
            head, n = head + " and", n + 1
        elif rng.random() < tex.commas:
            head += ","
        out[b] = head
        out[b + 1] = _soften(out[b + 1], protect)
    return " ".join(out)


def _misstrike(word: str, kind: str, rng: random.Random) -> str | None:
    """`word` with one slip of `kind`: a neighbouring key, a letter left out (often the last), a
    letter doubled or two swapped ("teh", "ponit"); the first letter only now and then."""
    n = len(word)
    if kind == "near":
        i = 0 if rng.random() < FIRST_LETTER else rng.randrange(1, n)
        side, diag = _NEAR.get(word[i].lower(), ("", ""))
        keys = side if side and (not diag or rng.random() < .7) else diag
        if not keys:
            return None
        c = rng.choice(keys)
        return word[:i] + (c.upper() if word[i].isupper() else c) + word[i + 1:]
    if kind == "drop":
        r = rng.random()
        i = n - 1 if r < .3 else 0 if r < .3 + FIRST_LETTER else rng.randrange(1, n)
        return word[:i] + word[i + 1:]
    if kind == "double":
        i = rng.randrange(n)
        return word[:i] + word[i] + word[i:]
    i = 0 if rng.random() < .2 else rng.randrange(1, n - 1)  # swap
    if word[i] == word[i + 1]:
        return None
    return word[:i] + word[i + 1] + word[i] + word[i + 2:]


def _slip_words(text: str, skip: frozenset[str]) -> list[tuple[int, int, bool]]:
    """The plain words a slip may hit: (start, end, whether its token is the bare word), outside
    links, quoted phrases and web addresses, never a word in `skip`, a mark ("xD") or
    shouting."""
    kept = _kept_spans(text)
    out = []
    for m in re.finditer(r"\S+", text):
        if any(a < m.end() and m.start() < b for a, b in kept):
            continue
        tok = m.group(0)
        core = tok.lstrip("\"'(").rstrip(".,!?;:\"')")
        if (not core.isascii() or not core.isalpha() or core.casefold() in skip
                or _EMOTICON.fullmatch(core) or (len(core) >= SHOUT_MIN and core.isupper())):
            continue
        start = m.start() + len(tok) - len(tok.lstrip("\"'("))
        out.append((start, start + len(core), core == tok))
    return out


def _slip_once(text: str, kind: str, words: list[tuple[int, int, bool]], rng: random.Random,
               skip: frozenset[str]) -> tuple[str, str, str] | None:
    if kind in ("merge", "shift"):  # a space left out ("inthe"), or hit a letter early
        pairs = [(a, b) for a, b in zip(words, words[1:])
                 if a[2] and b[2] and a[1] + 1 == b[0] and text[a[1]] == " "]
        if kind == "merge":
            pairs = [(a, b) for a, b in pairs if a[1] - a[0] <= 5 and b[1] - b[0] <= 5]
        else:
            pairs = [(a, b) for a, b in pairs if a[1] - a[0] >= 3]
        if not pairs:
            return None
        a, b = rng.choice(pairs)
        first, second = text[a[0]:a[1]], text[b[0]:b[1]]
        new = first + second if kind == "merge" else first[:-1] + " " + first[-1] + second
        if (kind == "merge" and new.casefold() in COMMON_WORDS) or _never(*new.split()):
            return None  # "in"+"to" is "into"
        return text[:a[0]] + new + text[b[1]:], "", ""
    if kind == "auto":
        cands = [w for w in words if text[w[0]:w[1]].casefold() in _AUTOCORRECT]
    else:
        cands = [w for w in words if w[1] - w[0] >= 3]
    if not cands:
        return None
    a, b, _ = rng.choices(cands, weights=[w[1] - w[0] for w in cands])[0]
    word = text[a:b]
    if kind == "auto":
        new = _like(word, rng.choice(_AUTOCORRECT[word.casefold()]))
    else:
        new = _misstrike(word, kind, rng)
        if new is None or new.casefold() in COMMON_WORDS or new.casefold() in skip:
            return None
    if _never(new):  # never a swear, sexual word or slur she did not write ("sec" -> "sex")
        return None
    fixable = kind in _FIXABLE and (kind == "auto" or (len(word) >= 4
                                                        and word.casefold() not in _SMALL))
    return text[:a] + new + text[b:], new if fixable else "", word if fixable else ""


def _slip(text: str, rate: float, rng: random.Random, skip: frozenset[str],
          phone: bool) -> tuple[str, str, str]:
    """At most one slip of the fingers in the line, in some lines: SLIP_K x `rate` of the lines
    at SLIP_FULL words or more (at most SLIP_CAP), fewer in shorter ones. (text, the slip as
    typed, the word she meant) when it is worth a "*word", else the slip's word and fix are
    empty."""
    if rate <= 0:
        return text, "", ""
    words = _slip_words(text, skip)
    if not words or rng.random() >= min(SLIP_CAP, SLIP_K * rate * min(1.0, len(words) / SLIP_FULL)):
        return text, "", ""
    weights = _PHONE_KINDS if phone else _GAME_KINDS
    for _ in range(TYPO_TRIES):
        got = _slip_once(text, rng.choices(_KEYS, weights=weights)[0], words, rng, skip)
        if got is not None:
            return got
    return text, "", ""


def _textured(plain: str, t: Typing, tex: Texture, eff: Effects, rng: random.Random,
              protected: frozenset[str], *, phone: bool) -> tuple[str, str, str]:
    """The typed line from its plain words: shorthand, spelling habits, apostrophes, then at
    most one slip. (typed, slip, fix) as _slip."""
    protect = _words_cf(protected)
    typed, short = _shorthand(plain, t, tex, rng, protect)
    typed, spelt = _habits(typed, tex, rng, protect)
    typed = _apostrophes(typed, tex.apostrophes, rng)
    skip = protect | short | spelt | REAL_SHORTHAND | _INTERJECTIONS
    return _slip(typed, t.typo_rate * eff.typo_mult * (DISCORD_SLIP if phone else 1), rng, skip,
                 phone)


def carries(line: str, slip: str) -> bool:
    """`slip` is still a whole word of `line`."""
    return bool(slip) and re.search(_WB + re.escape(slip) + _WE, line) is not None


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
    """The line ends in a mark, an ending such as "lol", or an emoji (only a Discord line keeps
    one)."""
    words = line.split()
    if not words:
        return False
    last = words[-1].rstrip(".,!?")
    return (bool(_EMOTICON.fullmatch(last)) or _bare(words[-1]) in _ENDINGS
            or EMOJI_RE.search(last) is not None)


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
                no_emoticon: bool = False, wink_ok: bool = True,
                game: bool = True, cap: int | None = None,
                max_chars: int | None = None, texture: bool = False) -> list[Styled]:
    """apply_style's lines with their plain words (Styled). `force_lines`: the turn may take
    all of max_lines whatever the persona's split_rate (a whisper to him at the explicit band
    runs to a line or two). `no_emoticon`: no mark is added (a line that carried her romance
    text, a wipe line: rehearsal 2's "dream of me ;) lmao", "great aww"). Without `wink_ok`
    (a line to or naming a member) no wink, kiss or heart is added, a smile still may be (fix
    round 1, review I2: ";)" went onto a line the member screen had passed). Without `game` (a
    Discord line) nothing is folded to ASCII and no line is cut to the bridge's bytes or
    cleaned as a chat line: the Discord store cleans and limits its text. `cap`: a line's
    words instead of line_cap's (a longer Discord message, talk/discord_length.py);
    `max_chars`: a line loses whole words until it has at most that many characters (the
    Discord store's MAX_TEXT, which would refuse it whole). `texture` ([talk] texture): her
    typing habits (texture_of, seeded by `speaker`) instead of every abbreviation every time
    and typos word by word; a Discord line's slips are a phone's."""
    if kind in RAID_CALLS:
        eff = EFFECTS[Altered.SOBER]
    asc = to_ascii if game else str
    body = _clean(text, tuple(x for x in (speaker, *aliases) if x), fold=game)
    if not body:
        return []
    keep = frozenset(asc(w) for w in keep_case)
    body = _keep_case(_case(body, t.case), keep, body)
    tex = texture_of(t, speaker) if texture else None
    punct = t.end_punct
    if tex is not None and punct in ("full", "some"):
        # her last full stop is a habit of hers, not a rule: typed as declared, or left off
        punct, drop_some = (punct if rng.random() < tex.stops else "none"), False
    else:
        drop_some = punct == "some" and rng.random() < SOME_DROP_P
    # only marks are added (a stored word among them is a word: schema.is_mark)
    emoticons = [e for e in (asc(x).strip() for x in t.emoticons) if e and is_mark(e)
                 and (wink_ok or not _WINKY.fullmatch(e))]
    emoticon = ""
    if emoticons and not no_emoticon and rng.random() < t.emoticon_rate:
        emoticon = rng.choice(emoticons)
    lines = (max(1, max_lines) if kind is TurnKind.BIT or force_lines
             else turn_lines(t, max_lines))
    split = rng.random() < t.split_rate
    room = cap or line_cap(t, eff, length_mult)
    laid = _fit(_TOKEN.findall(body), room, lines)
    if split and lines > 1 and len(laid) == 1:
        laid = _split_one(laid[0], t.words_per_line[0]) or laid
    protected = frozenset(asc(w) for w in protect) | keep
    out: list[Styled] = []
    for i, units in enumerate(laid):
        ws = [w for clause, _ in units for w in clause]
        while ws and not ws[-1].rstrip(",;:-"):
            ws.pop()  # a dangling "," or "-"
        if not ws:
            continue
        plain = _end_punct(" ".join(ws).rstrip(",;:"), punct, drop_some)
        if tex is not None:
            plain = _run_on(plain, tex, rng, _words_cf(protected), room)
        if i > 0 and t.case == "proper":
            plain = _case(plain, "proper")
        slip = fix = ""
        if tex is None:
            typed = _typos(_abbreviate(plain, t.abbreviations), t.typo_rate * eff.typo_mult,
                           rng, protected)
        else:
            typed, slip, fix = _textured(plain, t, tex, eff, rng, protected, phone=not game)
        tail = (f" {emoticon}" if emoticon and i == len(laid) - 1 and not _ends_marked(typed)
                else "")
        while game and len((typed + tail).encode("utf-8")) > MAX_TEXT_BYTES:
            tokens = _TOKEN.findall(typed)
            if len(tokens) <= 1:
                break
            typed = _end_punct(" ".join(tokens[:-1]).rstrip(",;:"), punct, drop_some)
        while max_chars is not None and len((typed + tail).strip()) > max_chars:
            tokens = _TOKEN.findall(typed)
            if len(tokens) <= 1:
                break
            typed = _end_punct(" ".join(tokens[:-1]).rstrip(",;:"), punct, drop_some)
            # the plain text loses the same tail, so what is stored is what was sent
            words = plain.split()
            if len(words) > 1:
                plain = _end_punct(" ".join(words[:-1]).rstrip(",;:"), punct, drop_some)
        line = sanitize_say_text(typed + tail) if game else (typed + tail).strip()
        if line:
            if not carries(line, slip):  # cut off with the words the line lost
                slip = fix = ""
            out.append(Styled(line, plain, slip, fix))
    return out


def apply_style(text: str, t: Typing, eff: Effects, rng: random.Random, *,
                length_mult: float = 1.0, protect: frozenset[str] = frozenset(),
                speaker: str = "", kind: TurnKind | None = None,
                keep_case: frozenset[str] = frozenset(), max_lines: int = 2,
                aliases: tuple[str, ...] = (), texture: bool = False) -> list[str]:
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
        keep_case=keep_case, max_lines=max_lines, aliases=aliases, texture=texture)]
