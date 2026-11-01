"""Who speaks. A member speaks because the topic touches them, they were addressed, or they
know the person who spoke last: the weights below reward those reasons and the reply planner
never answers a line with the whole guild. A line of his that names one member, and speaks to
no one else, gets that member's answer alone (aimed_at_one).

Answering the human (final fix wave, ruling final I1): the reasons are being addressed or
mentioned, the topic fitting the member (their interests, their role and title, how they play),
an open thread with him (they answered him in the last OPEN_THREAD_S), real closeness from
history (lived_close: well liked or well known in a row with interactions, never the
attraction or the romance stage) and, for women, quest talk. A woman's attitude only weights
the draw, a little (ATTITUDE_WEIGHT). An unlived row (no interactions: a stance's seed, a
relinked friend declaration) is no reason and adds no weight.

His pushback (fix wave G6): a line of his that doubts or is annoyed by what a member just said
(pushback_kind) is answered first by that member (the engine finds them: the one it names, else
whoever spoke last), whose answer is plain and one line."""
import re
from dataclasses import dataclass
from typing import Callable, Collection, Literal, Sequence

from leeroy_mind.personas.schema import Persona
from leeroy_mind.storage.rows import PersonaStateRow, Relationship
from leeroy_mind.talk.budget import DayPhase
from leeroy_mind.talk.filters import subject_words
from leeroy_mind.talk.names import game_thing
from leeroy_mind.text import normalize_words, strip_wow_markup
from leeroy_mind.talk.turns import TurnKind

NON_CORE_FACTOR = 0.08
INTROVERT_OPENER_FACTOR = 0.25
MIN_WORD = 4
REASON_CLOSENESS = 1.2
REASON_HUMAN_AFFINITY = 40  # lived_close: liked this much, or known this well
REASON_HUMAN_FAMILIARITY = 60
OPEN_THREAD_S = 300.0  # answering him keeps a thread with him open this long
# How much a woman's attitude weights the draw for an answer to him (lust > drawn > fond).
ATTITUDE_WEIGHT = {"lust": 1.25, "drawn": 1.15, "fond": 1.08}
WHISPER_REPLY_P = 0.9
# A woman with an attitude toward him answers every whisper of his (rehearsal 3, check 4).
WHISPER_REPLY_P_ATTITUDE = 1.0
REPLY_COUNT_REMARK = ((0.18, 0), (0.70, 1), (1.0, 2))  # cumulative
_WORD = re.compile(r"[a-z0-9']+")
_BRACKETS = re.compile(r"\[[^\]]*\]")
_STOP = frozenset("""about after again also always another around been before being both
but could does doing done each even ever from have having here into just keep know like
little made make many more most much must never only other over really same should some still
such than that their them then there these they thing think this those through very want was
were what when where which while will with would your tonight anyone good people time guys
yeah okay nice thanks back lets gonna pretty maybe getting going something everyone""".split())
_COMMON = frozenset("""have then more just tank raid boss heal heals need good time tonight
anyone people guild back lets gonna what when where that this with from your they them want like
know think really still also even much very last next first again maybe pull pulls wipe wipes
loot drop gear kill dead mana ready wait sure yeah okay nice thanks""".split())
_GREETING = frozenset({"hi", "hey", "yo", "sup", "hello"})
_GREETING_SHORT = 3  # "hey all": a greeting word in a line this short
# Also plain nouns ("evening was fun"): a hello only after "good", or alone, or to someone.
_TIME_GREETING = frozenset({"evening", "morning"})
# Who a hello or a goodbye is said to.
_ADDRESS = frozenset({"all", "guys", "everyone", "everybody", "yall", "folks", "peeps", "ppl",
                      "people", "fam", "gang", "team", "guild", "o"})
_GOODBYE_FIRST = frozenset({"gn", "gnight", "nite", "night", "bye", "cya", "later", "laters",
                            "ttyl", "goodnight"})
# Also ordinary words ("night shift sucks", "later today we should raid"): a goodbye only
# alone, with one more word or said to someone (Task 13 carry).
_GOODBYE_LOOSE = frozenset({"night", "later", "laters"})
_GOODBYE_PHRASE = re.compile(
    r"\b(good night(?! el(?:f|ves)\b)|gotta go(?! fast)|logging (?:off|out)|off to bed)\b")
_NIGHT_NOT_AFTER = frozenset({"raid", "last", "a", "this", "tonight", "every"})
_GOODBYE_MAX_TOKENS = 10
_QUESTION_START = frozenset({"who", "anyone", "any1", "what", "how", "where", "when",
                             "should", "does", "is", "are", "can"})
# A line about quests (owner, 2026-10-30): every woman is eager to help him with them.
_QUEST = re.compile(r"\bquest(?:s|ing|ed|line|lines|log|chain|chains|giver|givers)?\b"
                    r"|\bhow do i turn\b")
# "where do i ..." only about something of the game (fix wave part E, round 2): "where do i
# find the argent tournament guy", never "where do i get my car fixed".
_WHERE_DO_I = re.compile(r"\bwhere do i\b")
# "daily" alone is an ordinary word ("my daily commute"): it counts only in quest phrases.
_DAILY = re.compile(r"\bdailies\b|\bdaily (?:quests?|heroics?|hcs?|dungeons?|randoms?)\b"
                    r"|\b(?:do|doing|did|done|finish|finished) (?:my|the|your|our|all my|all "
                    r"the) dail(?:y|ies)\b|\bthe daily\b")
_QUEST_LINK = re.compile(r"\|Hquest:", re.IGNORECASE)
# Game words under MIN_WORD letters that still make a topic.
_SHORT_TOPIC = frozenset({"icc", "toc", "voa", "rs", "pvp", "bgs", "rbg", "dps", "oom", "aoe",
                          "hc", "hcs", "ony", "naxx"})
# What a member's role, title and way of playing make them care about (their topic fit).
_RAID = "raid raids raiding progression strat strats boss bosses icc toc ulduar naxx pull"
_PVP = "pvp arena arenas rating rated battleground battlegrounds bgs duel duels wintergrasp"
_ROLE_TOPICS = {
    "tank": "tank tanks tanking threat aggro pull pulls offtank",
    "healer": "heal heals healing healer healers mana oom",
    "melee": "dps damage meters parse rotation",
    "ranged": "dps damage meters parse rotation",
}
_TITLE_TOPICS = {"raid": _RAID, "pvp": _PVP, "heal": "heal heals healing healer healers mana",
                 "tank": "tank tanks tanking threat aggro", "melee": "dps melee meters",
                 "caster": "dps caster casters meters", "ranged": "dps ranged meters"}
_PLAY_TOPICS = {
    "theorycrafter": "stats rotation gear gearing talents spec specs glyph glyphs numbers",
    "lore": "lore story stories quest quests questline history legend",
    "collector": "mount mounts pet pets achievement achievements title titles rare",
    "competitor": _PVP,
    "achiever": "achievement achievements title titles mount mounts drake meta",
    "explorer": "explore exploring zone zones",
}


@dataclass(frozen=True, slots=True)
class SpeakerContext:
    """quest: the human's line is about quests (is_quest_talk), which gives every woman a
    reason to answer him. to_human: the turn answers or greets the human, so the row to him is
    judged on what was lived (module docstring). thread: the members with an open thread with
    him."""
    now: float
    topic: set[str]
    opener: bool
    phase: DayPhase
    quest: bool = False
    to_human: bool = False
    thread: frozenset[int] = frozenset()


@dataclass(frozen=True, slots=True)
class ReplyTurn:
    """What Task 16 needs to build one reply turn: pass turn_id to Guards.record once, and
    reply_to to Guards.allow / choose_channel (REPLY_HUMAN goes where the human spoke); a
    CONTINUE line of the same turn inherits opener_channel."""
    speaker: int
    turn_id: str
    kind: TurnKind
    reply_to: str
    opener_channel: str
    to_guid: int = 0  # whisper replies: the human the whisper goes to


def _stem(w: str) -> str:
    for suf in ("ing", "ed", "es", "s"):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            w = w[:-len(suf)]
            break
    return w[:-1] if w.endswith("e") and len(w) > 3 else w


def _words(text: str) -> list[str]:
    return [_stem(w) for w in _WORD.findall(text.casefold())
            if (len(w) >= MIN_WORD and w not in _STOP) or w in _SHORT_TOPIC]


def persona_keywords(p: Persona) -> frozenset[str]:
    """What touches this member: their interests and life, their spec, and what their role,
    officer title and way of playing make them care about."""
    title = p.play.officer_title.casefold()
    bits = [*p.likes, *p.dislikes, *p.opinions, *p.game.wants, *p.game.outside_raids,
            p.game.favourite_spot, p.play.main_spec, p.play.off_spec, p.real_life.job,
            p.real_life.life_thread, p.play.officer_title,
            _ROLE_TOPICS.get(p.play.raid_role, ""),
            *(words for key, words in _TITLE_TOPICS.items() if key in title),
            _PLAY_TOPICS.get(p.game.thinks_as, ""), _PLAY_TOPICS.get(p.game.motivation, "")]
    return frozenset(w for b in bits for w in _words(b))


def topic_words(lines: Sequence[str]) -> set[str]:
    return {w for line in lines for w in _words(line)}


def _closeness(rel: Relationship | None) -> float:
    """Rounded, so float error never lifts a seeded 10/20 row (1.2000000000000002) over the
    REASON_CLOSENESS bar."""
    if rel is None:
        return 1.0
    return round(1 + max(0.0, rel.affinity) / 100 + rel.familiarity / 200, 9)


def lived_close(rel: Relationship | None) -> bool:
    """Real closeness to the human from history: liked or known well, in a row that has been
    lived (interactions > 0). An unlived row never is, however warm (a stance's seed, a friend
    declaration relinked from the persona: final fix wave ruling); attraction and the romance
    stage never count."""
    return rel is not None and rel.interactions > 0 and (
        rel.affinity >= REASON_HUMAN_AFFINITY or rel.familiarity >= REASON_HUMAN_FAMILIARITY)


def is_quest_talk(text: str) -> bool:
    """A line about quests: quest, quests, questing, a quest chain, "how do I turn in", "where
    do I" about something of the game (names.game_thing), a quest link, or dailies
    ("dailies", "the daily", "do my daily", "daily quest", "daily heroic"), never "daily" on
    its own."""
    if _QUEST_LINK.search(text):
        return True
    plain = strip_wow_markup(text).casefold()
    return bool(_QUEST.search(plain) or _DAILY.search(plain)
                or (_WHERE_DO_I.search(plain) and game_thing(plain)))


def quest_helper(p: Persona) -> bool:
    """Who jumps at his quests (owner, 2026-10-30): every woman in the guild."""
    return p.identity.player_gender == "female"


def whisper_reply_p(p: Persona | None) -> float:
    """How often the member answers his whisper: every time for a woman fond of him, drawn to
    him or wanting him (WHISPER_REPLY_P_ATTITUDE), else WHISPER_REPLY_P."""
    if (p is not None and p.identity.player_gender == "female"
            and p.romance.stance in ATTITUDE_WEIGHT):
        return WHISPER_REPLY_P_ATTITUDE
    return WHISPER_REPLY_P


def has_reason(p: Persona, rel_to_last: Relationship | None, ctx: SpeakerContext, *,
               rel_to_human: Relationship | None = None, reply: bool = False) -> bool:
    """A real reason to speak: the topic touches them (persona_keywords), or they are close to
    the last speaker. Answering the human (reply), the reasons are his instead (the module
    docstring): an open thread with him (ctx.thread), real closeness from history
    (lived_close), and for a quest helper his line being about quests (ctx.quest)."""
    if ctx.topic & persona_keywords(p):
        return True
    if not reply:
        return _closeness(rel_to_last) > REASON_CLOSENESS
    if ctx.quest and quest_helper(p):
        return True
    return p.guid in ctx.thread or lived_close(rel_to_human)


def speaker_weight(p: Persona, state: PersonaStateRow, rel_to_last: Relationship | None,
                   ctx: SpeakerContext, last_spoke: float | None, talk_mult: float) -> float:
    ext = p.big_five.extraversion.score
    interest = 1 + 0.5 * min(2, len(ctx.topic & persona_keywords(p)))
    close = _closeness(rel_to_last)
    if ctx.to_human:  # his row counts for what was lived; her attitude, a little
        close = close if lived_close(rel_to_last) else 1.0
        if p.identity.player_gender == "female":
            close *= ATTITUDE_WEIGHT.get(p.romance.stance, 1.0)
    w = (0.2 + ext / 10) * interest * close * max(0.0, 1 + 0.3 * state.mood) * talk_mult
    if not p.core:
        w *= NON_CORE_FACTOR
    if ctx.opener and ext <= 4:
        w *= INTROVERT_OPENER_FACTOR
    if ctx.phase is DayPhase.LATE:
        w *= 1.5 if p.real_life.night_owl else 0.5
    if last_spoke is not None:
        age = ctx.now - last_spoke
        w *= 0.0 if age < 60 else 0.3 if age < 300 else 1.0
    return w


def pick_speakers(weights: dict[int, float], n: int, rng,
                  reasons: Collection[int] | None = None) -> list[int]:
    """n unique guids by weight. Members in `reasons` are drawn first, so a member with a
    real reason to speak is never crowded out by the rest of the guild."""
    if reasons:
        first = _draw({g: w for g, w in weights.items() if g in reasons}, n, rng)
        rest = {g: w for g, w in weights.items() if g not in first}
        return first + _draw(rest, n - len(first), rng)
    return _draw(weights, n, rng)


def _draw(weights: dict[int, float], n: int, rng) -> list[int]:
    pool = {g: w for g, w in weights.items() if w > 0}
    out: list[int] = []
    while pool and len(out) < n:
        x = rng.random() * sum(pool.values())
        chosen = next(reversed(pool))
        for g, w in pool.items():
            x -= w
            if x < 0:
                chosen = g
                break
        out.append(chosen)
        del pool[chosen]
    return out


def _is_goodbye(t: str, tokens: list[str]) -> bool:
    if not tokens or t.endswith("?") or len(tokens) > _GOODBYE_MAX_TOKENS:
        return False
    if tokens[0] in _GOODBYE_FIRST and tokens[0] not in _GOODBYE_LOOSE:
        return True
    if tokens[0] in _GOODBYE_LOOSE and (len(tokens) == 1 or tokens[1] not in ("elf", "elves")
                                        and (len(tokens) == 2 or tokens[1] in _ADDRESS)):
        return True
    if _GOODBYE_PHRASE.search(t):
        return True
    return tokens[-1] in ("night", "nite") and (
        len(tokens) < 2 or tokens[-2] not in _NIGHT_NOT_AFTER)


def classify_human_line(text: str) -> Literal["greeting", "goodbye", "question_all", "remark"]:
    t = text.casefold().strip()
    tokens = _WORD.findall(t)
    if _is_goodbye(t, tokens):
        return "goodbye"
    if t.endswith("?") or (tokens and tokens[0] in _QUESTION_START):
        return "question_all"
    if tokens and (tokens[0] in _GREETING or (
            len(tokens) <= _GREETING_SHORT and _GREETING & set(tokens))
            or _time_greeting(tokens)):
        return "greeting"
    return "remark"


# His question about the guild's past (fix wave G3, rehearsal 3 check 3): since when, how long
# it has gone on, when it was, who started it, how it began, what year, who remembers, or a
# "who/what/when ... again?" asking to be reminded how things stand. Never a question about
# the members themselves ("how long have you guys been playing?": a guild matter asked of
# them is one when they hold a memory of it, engine._plan_lore), nor a plan or a retry ("who
# wants to go again?", "when do we pull again?").
_NOT_THEM = r"(?!\s+(?:(?:do|does|did|are|is|have|has|were|was)\s+)?(?:you|u|ya)\b)"
_PAST_ASK = re.compile(
    r"\bsince when\b" + _NOT_THEM
    + r"|\bhow long(?:'s|\s+(?:has|had|was|were|did|have))\b" + _NOT_THEM
    + r"|\bwhen (?:did|was|were)\b" + _NOT_THEM
    + r"|\bwho\s+(?:started|began|founded|invented|named|came up with|made up|thought up)\b"
    r"|\bhow did\b" + _NOT_THEM + r"[^?.!]*\b(?:start|begin|happen|come about)\b"
    r"|\bwhat year\b|\bhow many years\b"
    r"|\bremembers?\s+(?:when|the|that|how(?!\s+to\b)|who|what|back|our)\b")
_AGAIN_ASK = re.compile(r"^\W*(?:[\w']+\W+){0,2}?(?:who|whos|who's|what|whats|what's|when|whens"
                        r"|when's|which|where|wheres|where's)\b.*\bagain\W*$")
_PLAN_ASK = re.compile(r"\b(?:wants?|wanna|ready|up for|down (?:for|to)|in for|keen|free|can"
                       r"|could|will|would|should|shall|gonna|going to|coming|joining|try"
                       r"|trying|do we|are we|we're|we are|let'?s)\b")


def asks_the_past(text: str) -> bool:
    """His line asks about the guild's past or how things have long stood (_PAST_ASK,
    _AGAIN_ASK): its answer is someone's memory, never anyone's guess. A bare "...again?",
    with one subject word or none, asks to be reminded of tonight ("who's tanking again?",
    "what's the plan again?": fix round 1, review C1); a matter named in two or more ("who
    gets the even weeks for bank duty again?") asks the past."""
    low = " ".join(strip_wow_markup(text).casefold().split())
    if _PAST_ASK.search(low):
        return True
    return (bool(_AGAIN_ASK.search(low)) and not _PLAN_ASK.search(low)
            and len(subject_words(low)) >= 2)


def _time_greeting(tokens: list[str]) -> bool:
    """ "good evening", "morning", "evening all", "evening zan"; never "evening was fun"."""
    for i, w in enumerate(tokens):
        if w in _TIME_GREETING and ((i > 0 and tokens[i - 1] == "good") or (
                i == 0 and (len(tokens) <= 2 or tokens[1] in _ADDRESS))):
            return True
    return False


def mentioned(text: str, names: dict[int, str]) -> list[int]:
    """Guids the line names, in order of appearance: a whole name, or a prefix of at least
    four letters that only one name starts with."""
    low = {g: n.casefold() for g, n in names.items()}
    out: list[int] = []
    plain = _BRACKETS.sub(" ", strip_wow_markup(text))
    for tok in _WORD.findall(plain.casefold()):
        tok = tok.removesuffix("'s")
        hit = [g for g, n in low.items() if n == tok]
        if not hit and len(tok) >= MIN_WORD and tok not in _STOP and tok not in _COMMON:
            hit = [g for g, n in low.items() if n.startswith(tok)]
            if len(hit) != 1:
                hit = []
        if hit and hit[0] not in out:
            out.append(hit[0])
    return out


def _remark_count(rng) -> int:
    r = rng.random()
    return next(n for cum, n in REPLY_COUNT_REMARK if r < cum)


# Words that speak to the whole guild (fix wave G5): a line with one is for everyone, whoever
# else it names. Not "someone" or "somebody": they point at no one in particular.
_GUILD_WORDS = frozenset("""anyone any1 anybody everyone everybody who whos whoever guys yall
y'all folks peeps ppl""".split())
# A hello, a goodbye, thanks or "you" before one of the _ADDRESS words is a word to the guild
# too: "hey all", "gn team", "thanks people", "you all".
_SALUTE = frozenset("""hey hi hello yo sup evening morning afternoon night nite gn gnight
goodnight bye cya later laters thanks thx ty gz gj you u""".split())
_ADDRESS_WORDS = _ADDRESS - {"o"}


def speaks_to_guild(text: str) -> bool:
    """His line speaks to the whole guild: "anyone", "who", "guys", "everyone" and the like
    (_GUILD_WORDS), or an address after a hello, a goodbye, thanks or "you" ("hey all", "gn
    team", "you all"). The names inside item and quest links are not read."""
    plain = _BRACKETS.sub(" ", strip_wow_markup(text)).casefold()
    tokens = [t.removesuffix("'s").strip("'") for t in _WORD.findall(plain)]
    return any(t in _GUILD_WORDS or (t in _ADDRESS_WORDS and i > 0 and tokens[i - 1] in _SALUTE)
               for i, t in enumerate(tokens))


def aimed_at_one(text: str, called: Sequence[int]) -> bool:
    """His line names one member (`called`: the members it names who can answer) and speaks to
    no one else, so that member answers it alone (fix wave G5, seen live: "whatever you need
    <name>, just link it in chat" drew a second replier from the reason pool, who riffed on the
    first one's answer). A second name, or a word to the whole guild (speaks_to_guild),
    leaves it to the usual draw."""
    return len(called) == 1 and not speaks_to_guild(text)


# His pushback (fix wave G6, seen live: a member teased him with the facts inverted, and his
# "what are you talking about?", "are you drunk?" and "you are fixing to get the boot" each drew
# another tease). Whole words or phrases, matched after normalize_words, so "that's" and
# "that’s" are one; ANGER wins over DOUBT.
DOUBT_CUES = (
    "what are you talking about", "what r u talking about", "what do you mean", "what do u mean",
    "wdym", "what does that mean", "makes no sense", "make no sense", "doesn't make sense",
    "doesnt make sense", "don't make sense", "dont make sense", "not following",
    "i don't follow", "i dont follow", "you lost me", "come again", "wtf", "what the fuck",
    "what the hell", "are you drunk", "are u drunk", "you drunk", "are you high",
    "are you serious", "that's not true", "thats not true", "no i didn't", "no i didnt",
    "i never did", "certainly not", "that's wrong", "thats wrong", "you're wrong", "youre wrong",
    "ur wrong")
ANGER_CUES = (
    "get the boot", "you're out", "youre out", "ur out", "gkick", "kick you", "shut up",
    "knock it off", "cut it out", "stop it", "that's enough", "thats enough", "last warning",
    "watch your mouth")
_DOUBT = tuple(f" {normalize_words(c)} " for c in DOUBT_CUES)
_ANGER = tuple(f" {normalize_words(c)} " for c in ANGER_CUES)
# "you're out" is a kick, but "you're out of range" (or mana, line of sight...) is a status
# call: those phrases never count as ANGER ("you're out of line" still does).
_NOT_ANGER = tuple(f" {normalize_words(f'{who} out of {what}')} "
                   for who in ("you're", "youre", "ur")
                   for what in ("range", "mana", "los", "combat", "position", "sight", "reach",
                                "line of sight", "melee range", "healing range", "heal range",
                                "energy", "rage", "runic power", "ammo", "arrows", "reagents",
                                "bag space", "gold"))
# Doubt too: two question marks in a row anywhere, or a line that is only "huh?", "what?" or
# "seriously?".
_ASKED_TWICE = "??"
_ONE_WORD_DOUBT = re.compile(r"\W*(?:huh|what|seriously)\s*\?[\s?!]*")
# A pushback that names nobody is aimed at whoever wrote the last member line in its channel
# this long before it (the engine's _pushback).
PUSHBACK_WINDOW_S = 120.0


def pushback_kind(text: str) -> Literal["doubt", "anger"] | None:
    """"anger" when his line is annoyed with someone (ANGER_CUES), "doubt" when he didn't
    follow what they said or thinks it wrong (DOUBT_CUES, "??" anywhere, or the whole line
    "huh?", "what?" or "seriously?"), else None. Case and punctuation don't count, typographic
    apostrophes are typed ones, and the names inside item and quest links are not read."""
    plain = _BRACKETS.sub(" ", strip_wow_markup(text))
    words = f" {normalize_words(plain)} "
    calm = words
    for phrase in _NOT_ANGER:
        calm = calm.replace(phrase, " ")
    if any(c in calm for c in _ANGER):
        return "anger"
    if (any(c in words for c in _DOUBT) or _ASKED_TWICE in plain
            or _ONE_WORD_DOUBT.fullmatch(plain.casefold().strip())):
        return "doubt"
    return None


@dataclass(frozen=True, slots=True)
class Pushback:
    """His line pushing back on one member (fix wave G6): kind is pushback_kind's, target the
    member it is aimed at, who can answer it, and line the id of the target's own last line on
    its channel this session (0: none), which their prompt always shows."""
    kind: Literal["doubt", "anger"]
    target: int
    line: int = 0


def plan_replies(text: str, *, channel: str, to_guid: int, weights: dict[int, float],
                 names: dict[int, str], afk: set[int], rng,
                 reasons: Collection[int] = (),
                 allowed: Callable[[int], bool] | None = None,
                 whisper_p: float = WHISPER_REPLY_P, target: int = 0) -> list[int]:
    """Who answers the human's line, in order. weights already hold recency and the callers'
    reasons to speak; a zero weight never answers (the addressee excepted). `reasons` are the
    members with a real reason (see has_reason) and are drawn first; `allowed` is the raid
    gate, e.g. lambda g: raid.allows(g, TurnKind.REPLY_HUMAN, now). A whisper is answered by
    the one he whispered, `whisper_p` of the time (whisper_reply_p). A line that names one
    member and speaks to no one else is answered by that member alone (aimed_at_one); the
    members it names answer first in any other.
    `target`: the member his pushback is aimed at (Pushback.target, fix wave G6). They answer
    first, a whisper every time, and count among the line's answers as a member it names
    would, whatever their weight; the usual draw follows. A target who can't answer (AFK, no
    weight entry, refused by `allowed`) changes nothing."""
    if allowed is not None:
        weights = {g: w for g, w in weights.items() if allowed(g)}
    if channel == "whisper":
        if to_guid not in weights or to_guid in afk or (
                target != to_guid and rng.random() >= whisper_p):
            return []
        return [to_guid]
    called = [g for g in mentioned(text, names) if g in weights and g not in afk][:3]
    if aimed_at_one(text, called):
        return called
    if target and target in weights and target not in afk and target not in called:
        called = [target, *called]
    kind = classify_human_line(text)
    if kind == "remark":
        n = _remark_count(rng)
        if called:
            n = max(n, 1)
    else:
        n = 2 + (rng.random() >= 0.5)
    n = max(n, len(called))
    rest = {g: w for g, w in weights.items() if g not in called and g not in afk}
    return called + pick_speakers(rest, n - len(called), rng, reasons)


def plan_reply_turns(text: str, *, channel: str, to_guid: int, weights: dict[int, float],
                     names: dict[int, str], afk: set[int], rng, line_id: int,
                     human_guid: int = 0, reasons: Collection[int] = (),
                     allowed: Callable[[int], bool] | None = None,
                     whisper_p: float = WHISPER_REPLY_P, target: int = 0) -> list[ReplyTurn]:
    speakers = plan_replies(text, channel=channel, to_guid=to_guid, weights=weights,
                            names=names, afk=afk, rng=rng, reasons=reasons, allowed=allowed,
                            whisper_p=whisper_p, target=target)
    kind = TurnKind.GOODNIGHT if classify_human_line(text) == "goodbye" else TurnKind.REPLY_HUMAN
    to = human_guid if channel == "whisper" else 0
    return [ReplyTurn(g, f"reply-{line_id}-{i}", kind, channel, channel, to)
            for i, g in enumerate(speakers)]


def typing_delay(text: str, chars_per_sec: float, delay_mult: float, rng) -> float:
    return max(2.0, (rng.uniform(1.5, 4.0) + len(text) / chars_per_sec) * delay_mult)
