"""The live prompt for one line of guild chat.

The system message (core card, sample lines, comms rules) is the same on every turn for a
speaker, so Ollama can reuse its cache. The user message carries, in order, the people
present, memories, the mood, the visible chat, the speaker's own last lines and the
instruction, and is trimmed to the budget (estimated tokens, 4 characters each). Repetition
is handled by showing the speaker's own lines; LIVE never sends repeat penalties.

Built-in text is generic and non-explicit at every dial setting. Romance, bit and aside
wording reaches a prompt only through `extra`, from Task 15 and the owner's local overlays
(prompts.load_prompt), capped at EXTRA_CAP_TOKENS.

The human's real name reaches only his inner circle (knows_real_name: close to him and knowing
him very well): real_name_line gates the one line that tells them, and redact_for scrubs it
from everything anyone else is shown.

A reply to his game question carries facts from the library (talk/facts.py) after the chat,
under FACTS_HEAD; the budget never cuts them and gives up older chat first."""
import logging
import random
import re
from dataclasses import dataclass, field, replace

from leeroy_mind.human import THEY_FORMS, HumanIdentity
from leeroy_mind.personas.schema import Persona, Typing
from leeroy_mind.personas.screens import redact_real_name
from leeroy_mind.storage.rows import ROMANCE_STAGES, ChatLineRow, PersonaStateRow, Relationship
from leeroy_mind.talk.budget import DayPhase
from leeroy_mind.talk.states import RAID_CALLS, Altered, state_line
from leeroy_mind.talk.turns import TurnKind
from leeroy_mind.text import estimate_tokens, strip_wow_markup, truncate_words

log = logging.getLogger("leeroy_mind")

LIVE_BUDGET = 1300
EXTRA_CAP_TOKENS = 300  # an overlay's bit, romance or aside text, at most
MIN_MEMORIES, MIN_CHAT, MIN_REL_CARDS, MIN_OWN = 3, 5, 1, 3
SHORT_CHAT_CHARS = 80
NOTE_CHARS = 100
HUMAN_NOTE_CHARS = 160  # her note toward him: the one fact about him that is hers
LIFE_THREAD_CHARS = 120

# Close to the human (owner, 2026-10-30): romance stage banter or later, or affinity >= 40.
# "ex" sits with "none" for romance purposes.
CLOSE_AFFINITY = 40
CLOSE_STAGES = frozenset(ROMANCE_STAGES[ROMANCE_STAGES.index("banter"):ROMANCE_STAGES.index("ex")])
REAL_NAME_SHARE = 0.15  # of an inner-circle member's prompts carry his real name
# His real name takes more than being close (final fix wave ruling): knowing him this well.
INNER_FAMILIARITY = 80
# Turns where idle detail (what's on their mind, tonight so far, a smoker's afk) belongs in
# "Right now:": openers only (an aside answers a flirt: prompts lens, minor 8).
IDLE_KINDS = frozenset({TurnKind.OPEN})
LATELY_CHARS = 120


@dataclass
class PromptParts:
    """card: the core card. samples: select_samples. rules: comms_rules. rel_cards: the
    addressee's card first. memories: Recall.format lines, best first. mood: mood_line.
    chat: chat_line_text lines, oldest first. own_recent: the speaker's last lines, oldest
    first. instruction: render_instruction. real_name: real_name_line ("" almost always).
    facts: the library's fact lines for his game question (facts.fact_lines), under
    facts_head (facts_head()). pinned: the index in chat of his line the reply answers; with
    facts, the trim never cuts it. kept: the index in chat of the speaker's own line his
    pushback is about (fix wave G6); the trim never cuts it either."""
    card: str
    samples: list[str]
    rules: str
    rel_cards: list[str]
    memories: list[str]
    mood: str
    chat: list[str]
    own_recent: list[str]
    instruction: str
    real_name: str = ""
    facts: list[str] = field(default_factory=list)
    facts_head: str = ""
    pinned: int | None = None
    kept: int | None = None


FACTS_HEAD = ("Facts you know (use them if they help, in your own words; never invent anything "
              "beyond them; if they don't cover it, say you're not sure or point {them} to "
              "someone who'd know):")


def facts_head(them: str) -> str:
    """FACTS_HEAD with the human's object pronoun (HumanIdentity.forms[1])."""
    return FACTS_HEAD.format(them=them)


SHORT_LINE = "One short line in your own voice."
COMMS_RULES = f"""\
How you talk here:
- You're a real person in 2009, typing in your WoW guild's chat. Real life talk stays in 2009 \
or is timeless.
- Your slang is 2009 slang.
- You're not an assistant. Never mention AI, language models, prompts or bots.
- {SHORT_LINE} Don't narrate actions or write other people's lines.
- Write only your line: no name in front, no quotes.
- React to what actually happened or was said, and answer people directly. Specific beats \
polite.
- React only to the chat shown. Shared history comes only from your memories, never invent it.
- You can be moody, blunt, bored or wrong.
- Never guilt-trip {{human}} for being away.
- If you wouldn't say anything, answer exactly (nothing)"""


# Every woman in the guild is eager to help him with quests (owner, 2026-10-30), and claims
# nothing about what he has done: P1b doesn't know (prompts I7). It ends a woman's reply to his
# quest line, where she reads it last (rehearsal 2, judge C 5), and nowhere else: in the
# standing rules it primed a quest in every greeting (judge B 3).
QUEST_HELP = ("{human} brought up a quest, and you jump at it: end your line with an offer to "
              "help {them} with it or to come along. You only know which quests {human} has "
              "done if {they} told you.")
# Flirting is only ever aimed at him (content invariant), on every turn (prompts I10).
FLIRT_RULE = {
    "female": ("- You flirt only with {human}. Everyone else is a friend, a rival or just a "
               "guildie."),
    "male": ("- You don't flirt with anyone in the guild. The women only have eyes for {human} "
             "anyway."),
}


def length_rule(t: Typing) -> str:
    """How long this person's lines are, in the words the style holds them to (words_per_line,
    and split_rate for a second line), so the model writes to fit instead of being cut."""
    lo, hi = t.words_per_line
    if t.split_rate <= 0:
        return f"One line in your own voice, usually {lo}-{hi} words."
    if t.split_rate < 0.5:
        return f"One line in your own voice, usually {lo}-{hi} words, now and then two lines."
    return f"One or two lines in your own voice, usually {lo}-{hi} words each."


def comms_rules(human: HumanIdentity, persona: Persona | None = None) -> str:
    """COMMS_RULES for this guild: who the human is (never his real name), then the rules
    with his name and the persona's line length (length_rule), then the persona's FLIRT_RULE.
    The same on every turn for a speaker, for Ollama's prompt cache."""
    rules = COMMS_RULES.format(human=human.called)
    if persona is not None:
        rules = rules.replace(SHORT_LINE, length_rule(persona.typing))
    out = f"In your guild: {human.describe(include_real_name=False)}.\n" + rules
    if persona is None:
        return out
    gender = persona.identity.player_gender
    if gender in FLIRT_RULE:
        out += "\n" + FLIRT_RULE[gender].format(human=human.called)
    return out


def quest_help(human: HumanIdentity) -> str:
    """QUEST_HELP with his name and pronouns, for the end of a woman's reply to his quest
    line."""
    they, them, _ = human.forms
    return QUEST_HELP.format(human=human.called, them=them, they=they)


# His pushback on what a member just said (fix wave G6, seen live: her note toward him said she
# teases him about being too serious, so each serious line drew a harder tease, and nothing in
# her reply knew he was confused or annoyed). It ends that member's answer to the line; when he
# is annoyed, how agreeable they are picks the answer.
PUSHBACK_DOUBT = ("{human} is pushing back on what you just said: {they} didn't follow it or "
                  "think it's wrong. Answer {them} plainly: say what you meant, or admit you got "
                  "it wrong. No joke this time.")
PUSHBACK_SORRY = ("{human} is annoyed with you. Take it seriously: apologise and make it right. "
                  "No joke this time.")
PUSHBACK_FIRM = ("{human} is annoyed with you. Take it seriously: stand your ground if you were "
                 "right, going only by what was actually said. No joke this time.")
PUSHBACK_STRAIGHT = ("{human} is annoyed with you. Take it seriously and answer {them} straight. "
                     "No joke this time.")
SORRY_FROM, FIRM_UNTIL = 7, 4  # agreeableness: who apologises, who stands their ground


def pushback_help(human: HumanIdentity, kind: str, agreeableness: float) -> str:
    """The text for the answer of the member his pushback (select.pushback_kind) is aimed at,
    with his name and pronouns as the reply's own instruction has them: PUSHBACK_DOUBT, or for
    "anger" by the member's agreeableness, PUSHBACK_SORRY from SORRY_FROM, PUSHBACK_FIRM up to
    FIRM_UNTIL and PUSHBACK_STRAIGHT between."""
    they, them, _ = human.forms
    if kind == "doubt":
        text = PUSHBACK_DOUBT
    elif agreeableness >= SORRY_FROM:
        text = PUSHBACK_SORRY
    elif agreeableness <= FIRM_UNTIL:
        text = PUSHBACK_FIRM
    else:
        text = PUSHBACK_STRAIGHT
    return text.format(human=human.called, they=they, them=them)


INSTRUCTIONS: dict[TurnKind, str] = {
    TurnKind.REPLY_HUMAN: (
        "Answer {addressee}'s last line. Respond to what {they} actually said, as yourself, in "
        "one short line. Bring in a memory or a running joke only if it really fits. {extra}"),
    TurnKind.CONTINUE: (
        "Reply to {addressee}'s last line only if you have something real to add: a reaction, "
        "a tease, a disagreement or something you both remember. Don't just agree or echo it. "
        "If you'd let it go, answer (nothing). {extra}"),
    TurnKind.OPEN: (
        "{quiet}Bring up {topic} in one short line, the way you'd type it. It has to make sense "
        "on its own, without the chat above: nothing in chat is addressed to you. If it isn't "
        "worth saying, answer (nothing). {extra}"),
    TurnKind.GREET: "{addressee} just logged on. Greet {them} your way, in a few words. {extra}",
    TurnKind.GZ: "{addressee} just got {topic}. Congratulate {them} your way, in a few words. {extra}",
    TurnKind.KILL: (
        "The raid just killed {topic}. React in one short line to how that fight actually "
        "went. {extra}"),
    TurnKind.WIPE_REACT: (
        "The raid just wiped on {topic}. React in one short line: frustration, a joke or what "
        "went wrong. No speeches, no blaming everyone. If you'd keep quiet, answer (nothing). "
        "{extra}"),
    TurnKind.CALLOUT: "You're leading the raid. Call {topic} in one short line, clear and direct. {extra}",
    TurnKind.URGENT: "Warn the raid about {topic} right now, in a few words. {extra}",
    TurnKind.BIT: "Your bit, in your own voice, as if it just occurred to you. {extra}",
    TurnKind.ASIDE: (
        "A quick aside in chat, one short line in your own voice. If it would land badly, "
        "answer (nothing). {extra}"),
    TurnKind.FLIRT: (
        "Say something to {addressee} in chat, one short line, the way you feel about {them}. "
        "{extra}"),
    TurnKind.WHISPER: (
        "You're whispering {addressee} privately. One short line, the way you'd really "
        "whisper {them}. {extra}"),
    TurnKind.GOODNIGHT: "{addressee} is logging off. Say goodnight your way, in a few words. {extra}",
}

_TOPIC = {
    TurnKind.OPEN: "something from your own evening so far",
    TurnKind.GZ: "an achievement",
    TurnKind.KILL: "the boss",
    TurnKind.WIPE_REACT: "the boss",
    TurnKind.CALLOUT: "the next pull",
    TurnKind.URGENT: "what's about to kill people",
}


# The explicit whisper may run longer than her usual one line: the instruction says it is
# the exception to the standing rule (fix round 1, review minor 10).
MORE_LINES = "in a line or two, more than your usual one line"


def render_instruction(kind: TurnKind, *, addressee: str = "", topic: str = "",
                       extra: str = "", forms: tuple[str, str, str] = THEY_FORMS,
                       lead: str = "", tail: str = "", long: bool = False,
                       quiet: bool = True) -> str:
    """The TurnKind template on one line. `lead` (her romance text) comes before it, so the
    turn's own instruction is what the model reads last (prompts I5); `extra` (a bit, an aside,
    a fact) goes after it, cut to EXTRA_CAP_TOKENS, and a cut is logged without the text.
    `tail` (the owner's whisper overlay, capped by its own rule) goes after everything, the
    last thing she reads; `long` lets the line run to "a line or two". An opener says "Chat
    has gone quiet." only when `quiet` (rehearsal 2: it said so under lines from just now).
    `forms` are the addressee's pronouns (HumanIdentity.forms when it is the human); nothing
    is assumed without them."""
    tmpl = INSTRUCTIONS[kind]
    if long:
        tmpl = tmpl.replace("in one short line", MORE_LINES).replace(
            "One short line", MORE_LINES[3:].capitalize())
    if "{addressee}" in tmpl and not addressee:
        raise ValueError(f"a {kind} turn needs an addressee")
    extra = " ".join(extra.split())
    if estimate_tokens(extra) > EXTRA_CAP_TOKENS:
        log.warning("%s instruction extra capped at %d estimated tokens (was %d)", kind,
                    EXTRA_CAP_TOKENS, estimate_tokens(extra))
        extra = truncate_words(extra, EXTRA_CAP_TOKENS * 4)
    they, them, their = forms
    text = tmpl.format(addressee=addressee, topic=topic or _TOPIC.get(kind, "what just happened"),
                       extra=extra, they=they, them=them, their=their,
                       quiet="Chat has gone quiet. " if quiet else "")
    return " ".join(f"{lead} {text} {tail}".split())


def select_samples(p: Persona, n: int = 5) -> list[str]:
    """Approved sample lines first, each group in persona order, so the prefix is stable."""
    ordered = ([s for s in p.sample_lines if s.approved]
               + [s for s in p.sample_lines if not s.approved])
    return [t for t in (" ".join(s.text.split()) for s in ordered) if t][:n]


# How a label reads on a card (prompts lens, minor 13: the schema word reached the model).
LABEL_TEXT = {"friend": "you're friends", "friction": "you two clash", "rival": "you're rivals"}
# A woman's pull toward him on his card (prompts I4), by attraction band.
ATTRACTION_TEXT = {"lust": "you want {them}", "drawn": "you're into {them}",
                   "fond": "you're fond of {them}"}
GUILDMATE = "a guildmate, you know them from raids"
_SHE_HER = re.compile(r"\b(?:she|her|hers|herself)\b", re.IGNORECASE)
_HE_HIM = re.compile(r"\b(?:he|him|his|himself)\b", re.IGNORECASE)


def attraction_band(r: Relationship, attitude: str) -> str:
    """lust, drawn or fond: her stance when it is one of them (P1c pre-flight PR21: the owner's
    attitude mix is what she feels), else from her row's attraction once it has a value; ""
    for none."""
    if attitude in ATTRACTION_TEXT:
        return attitude
    if r.attraction >= 60:
        return "lust"
    if r.attraction >= 40:
        return "drawn"
    if r.attraction >= 20:
        return "fond"
    return ""


_SHE_SELF = re.compile(r"\b(?:she|herself)\b", re.IGNORECASE)
_HE_SELF = re.compile(r"\b(?:he|himself)\b", re.IGNORECASE)


def _note_fits(note: str, forms: tuple[str, str, str], speaker: str = "") -> bool:
    """A note about him that refers to him with the other set of pronouns is a generation
    slip: left out. One that uses them for the speaker herself is hers and stays (rehearsal 2,
    judge C 10: "Competitive rivalry, but she's drawn to him" was dropped). So a note with
    his own pronouns in it fits, and so does a woman's (`speaker`, her player_gender) that
    calls herself "she"; only one with the other pronouns alone is left out."""
    if forms[0] == "he":
        return (not _SHE_HER.search(note) or bool(_HE_HIM.search(note))
                or (speaker == "female" and bool(_SHE_SELF.search(note))))
    if forms[0] == "she":
        return (not _HE_HIM.search(note) or bool(_SHE_HER.search(note))
                or (speaker == "male" and bool(_HE_SELF.search(note))))
    return True


def relationship_card(name: str, r: Relationship | None, *, is_human: bool,
                      is_guild_master: bool = False,
                      forms: tuple[str, str, str] = THEY_FORMS, attitude: str = "",
                      speaker: str = "") -> str:
    """How the speaker feels about one person, in a few words. Only the warm side of trust
    and respect is shown: a low score usually just means "not yet". The human is tagged "(the
    guild master)" when he is one (HumanIdentity.is_guild_master) and referred to by `forms`
    (HumanIdentity.forms); anyone else as them/their. On his card a woman's `attitude` (her
    stance) shows as her attraction band, and her note toward him gets HUMAN_NOTE_CHARS. A
    member with no row at all is GUILDMATE; a label reads as how the two get on."""
    if not is_human and (r is None or r == Relationship(a=r.a, b=r.b)):
        return f"{name}: {GUILDMATE}"
    r = r or Relationship(a=0, b=0)
    _, them, their = forms if is_human else THEY_FORMS
    bits = []
    if not is_human and r.label.strip() in LABEL_TEXT:
        bits.append(LABEL_TEXT[r.label.strip()])
    band = attraction_band(r, attitude) if is_human else ""
    if band:
        bits.append(ATTRACTION_TEXT[band].format(them=them))
    if r.affinity >= CLOSE_AFFINITY:
        bits.append("you're close")
    elif r.affinity >= 15:
        bits.append(f"you like {them}")
    elif r.affinity <= -60:
        bits.append(f"you can't stand {them}")
    elif r.affinity <= -15:
        bits.append(f"you're annoyed by {them}")
    f = r.familiarity
    bits.append(f"you barely know {them}" if f < 20 else
                f"you know {them} a bit" if f < 60 else f"you know {them} well")
    if f >= 20 and r.trust >= 70:
        bits.append(f"you trust {them}")
    if f >= 20 and r.respect >= 70:
        bits.append(f"you rate {their} play")
    tag = " (the guild master)" if is_human and is_guild_master else ""
    card = f"{name}{tag}: {', '.join(bits)}"
    notes = " ".join(r.notes.split()).rstrip(".")
    if is_human and not _note_fits(notes, forms, speaker):
        notes = ""
    room = HUMAN_NOTE_CHARS if is_human else NOTE_CHARS
    return f"{card}. {truncate_words(notes, room)}" if notes else card


def is_close(r: Relationship | None) -> bool:
    return r is not None and (r.affinity >= CLOSE_AFFINITY or r.romance_stage in CLOSE_STAGES)


def knows_real_name(r: Relationship | None) -> bool:
    """His inner circle: close to him (is_close) and knowing him very well (familiarity
    INNER_FAMILIARITY or more). Only they may hear or use his real name; a close friend short
    of it is a friend who doesn't."""
    return is_close(r) and r.familiarity >= INNER_FAMILIARITY


def real_name_line(human: HumanIdentity, r: Relationship | None, rng: random.Random, *,
                   share: float = REAL_NAME_SHARE) -> str:
    """The hook for the human's real name (owner, 2026-10-30): only a speaker in his inner
    circle (knows_real_name; r is the speaker's relationship to him) may get it, in about
    `share` of their prompts,
    decided on the seeded rng. The name comes from the local config through HumanIdentity,
    never from code. Meant for turns where he is in the conversation. Every other speaker's
    parts go through redact_for."""
    if not human.real_name or not knows_real_name(r) or rng.random() >= share:
        return ""
    return (f"You're close enough to {human.called} to know {human.forms[2]} real name, "
            f"{human.real_name}. Use it once in a while, the way old friends do, never in every "
            "line.")


_PHASE = {DayPhase.MORNING: "It's morning.", DayPhase.DAY: "It's the middle of the day.",
          DayPhase.EVENING: "It's evening.", DayPhase.LATE: "It's late at night.",
          DayPhase.DEAD: "It's the small hours."}


def _mood_words(mood: float) -> str:
    if mood >= 0.5:
        return "You're in a great mood."
    if mood >= 0.2:
        return "You're in a good mood."
    if mood > -0.2:
        return ""
    if mood > -0.5:
        return "You're a bit off today."
    return "You're in a bad mood."


def mood_line(state: PersonaStateRow, altered: Altered, substance: str | None,
              phase: DayPhase, *, kind: TurnKind | None = None, life_thread: str = "",
              lately: str = "", variant: int = 0, today: str = "") -> str:
    """The "Right now:" line: time of day (`today`, the weekday and tonight's holidays from
    seasons.day_line, in its place when given), mood and any altered state. Openers
    (IDLE_KINDS) also get what has been on their mind (state.life_thread, else the persona's
    own `life_thread`: P1b writes no state, spec I3), what they did tonight (`lately`) and a
    smoker's habit; raid calls (CALLOUT, URGENT) get no altered state."""
    idle = kind in IDLE_KINDS
    life = " ".join((state.life_thread or life_thread).split()).rstrip(".") if idle else ""
    tonight = " ".join(lately.split()).rstrip(".") if idle else ""
    shown = "" if kind in RAID_CALLS else state_line(
        altered, substance if idle or Altered(altered) is not Altered.SOBER else None,
        variant=variant)
    parts = [today or _PHASE[phase], _mood_words(state.mood), shown,
             f"On your mind lately: {truncate_words(life, LIFE_THREAD_CHARS)}." if life else "",
             f"Tonight so far: {truncate_words(tonight, LATELY_CHARS)}." if tonight else ""]
    return " ".join(x for x in parts if x)


# What an opener makes of a substance on tonight (states.substance_of).
_HABIT_TOPIC = {"alcohol": "what you're drinking tonight", "weed": "being a bit high tonight",
                "caffeine": "how much coffee you've had today",
                "tobacco": "stepping out for a smoke"}


def open_topics(p: Persona, lately: str = "", *, substance: str | None = None) -> list[str]:
    """What an opener could bring up (prompts I9), each as the OPEN template's {topic}: a
    concrete situation from the persona's own life and from tonight, never a bare label
    (rehearsal 2, judge A 2: "something you're into (Mediating fights)" gave the model nothing
    to say, so it grabbed nouns from the chat): today with their life thread, something at
    home, their day at work, what tonight's habit makes of it (`substance`), how close they
    are to a want in game, what they were doing before raid, what they did tonight; the
    template's default when there is nothing else. The holidays come from seasons."""
    def one(s: str) -> str:
        return " ".join(s.split()).rstrip(".")
    rl = p.real_life
    out = []
    if one(rl.life_thread):
        out.append(f"something that happened today with {one(rl.life_thread)}")
    if one(rl.household):
        out.append(f"something going on at home right now ({one(rl.household)})")
    if one(rl.job):
        out.append(f"your day at work today ({one(rl.job)})")
    if substance in _HABIT_TOPIC:
        out.append(_HABIT_TOPIC[substance])
    out += [f"how close you are to {one(x)} in game" for x in p.game.wants if one(x)]
    out += [f"what you were doing before raid tonight ({one(x).replace('_', ' ')})"
            for x in p.game.outside_raids if one(x)]
    if one(lately):
        out.append(f"what you've been up to tonight ({one(lately)})")
    return out or [_TOPIC[TurnKind.OPEN]]


def line_age(seconds: float) -> str:
    """How long ago a chat line was said, as the prompt shows it: "just now" under a minute,
    then whole minutes, hours or days ("3m ago")."""
    s = max(0, int(seconds))
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


def chat_line_text(row: ChatLineRow, names: dict[int, str], *,
                   now: float | None = None) -> str:
    """'Name: text' for guild chat, 'Name (raid): text' elsewhere, 'Name (on vent): text' in a
    voice room, markup stripped, and with `now` its age in front ('[3m ago] Name: text'), so an
    old line never reads as current.
    `names` maps guids to how the speaker knows them (the human by his nickname)."""
    who = names.get(row.from_guid) or row.from_name or "someone"
    text = " ".join(strip_wow_markup(row.text).split())
    if row.channel == "guild":
        tag = ""
    elif row.channel == "whisper":
        to = names.get(row.to_guid)
        tag = f" (whisper to {to})" if to else " (whisper)"
    elif row.channel.startswith("voice:"):
        tag = " (on vent)"
    else:
        tag = f" ({row.channel})"
    age = "" if now is None else f"[{line_age(now - row.ts)}] "
    return f"{age}{who}{tag}: {text}"


def said_hi_text(row: ChatLineRow, names: dict[int, str], him: str, *,
                 now: float | None = None) -> str:
    """A greeting of his login as a later greeter's chat shows it: who said hi to him, never
    the words (rehearsal 2: the second greeter answered the first one's question)."""
    who = names.get(row.from_guid) or row.from_name or "someone"
    age = "" if now is None else f"[{line_age(now - row.ts)}] "
    return f"{age}{who} said hi to {him}"


def _section(head: str, lines: list[str]) -> str:
    lines = [x for x in lines if x]
    return head + "\n" + "\n".join(lines) if lines else ""


SAMPLES_HEAD = "How you type (examples of your style; don't repeat them):"


def render_messages(p: PromptParts) -> list[dict]:
    """The two messages, untrimmed. Empty sections are left out."""
    system = "\n\n".join(x for x in (
        p.card.strip(), _section(SAMPLES_HEAD, p.samples), p.rules.strip()) if x)
    people = [*p.rel_cards, *([p.real_name] if p.real_name else [])]
    user = "\n\n".join(x for x in (
        _section("People here:", people),
        _section("You remember:", p.memories),
        _section("Right now:", [p.mood]),
        _section("Chat:", p.chat),
        _section(p.facts_head or facts_head("them"), p.facts),
        _section("Your last lines (don't reuse their wording):", p.own_recent),
        p.instruction.strip()) if x)
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _tokens(msgs: list[dict]) -> int:
    return sum(estimate_tokens(m["content"]) for m in msgs)


def trim_to_budget(p: PromptParts, *, budget: int = LIVE_BUDGET) -> PromptParts:
    """A copy that fits the budget. In order: memories down to 3 (the least relevant go), chat
    down to 5 (the oldest go), relationship cards down to 1 (the addressee's stays), own lines
    down to 3 (the oldest go), then the oldest chat lines are shortened (never the newest).
    Past that, a logged last resort empties memories, then own lines, then relationship
    cards, and drops chat down to the newest line. With facts, the older chat goes first in
    both rounds (fix wave E: the facts answer his line, the chat before it matters less), and
    his line the reply answers (pinned) is never cut nor shortened: it is the floor, even
    when later answers came after it. The speaker's own line his pushback is about (kept, fix
    wave G6) is never cut nor shortened either, and the last resort keeps it with the floor.
    The system message, mood, real-name line, facts and instruction are never cut. Memories
    stay a prefix of the input, so len(result.memories) says which recalled memories were
    used."""
    t = replace(p, rel_cards=list(p.rel_cards), memories=list(p.memories), chat=list(p.chat),
                own_recent=list(p.own_recent))
    pin = p.pinned if p.facts and p.pinned is not None and 0 <= p.pinned < len(p.chat) else None
    kept = p.kept if p.kept is not None and 0 <= p.kept < len(p.chat) else None
    least = 1 if kept is None else 2  # the chat the last resort leaves

    def over() -> bool:
        return _tokens(render_messages(t)) > budget

    def drop(items: list[str], floor: int, oldest_first: bool) -> None:
        while len(items) > floor and over():
            items.pop(0 if oldest_first else -1)

    def drop_chat(floor: int) -> None:
        nonlocal pin, kept
        while len(t.chat) > floor and over():
            # the oldest line that is neither his pinned line nor her kept one
            i = next(j for j in range(len(t.chat)) if j not in (pin, kept))
            t.chat.pop(i)
            if pin is not None and i < pin:
                pin -= 1
            if kept is not None and i < kept:
                kept -= 1

    if t.facts:
        drop_chat(MIN_CHAT)
    drop(t.memories, MIN_MEMORIES, False)
    drop_chat(MIN_CHAT)
    drop(t.rel_cards, MIN_REL_CARDS, False)
    drop(t.own_recent, MIN_OWN, True)
    for i in range(len(t.chat) - 1):  # never the newest line, the one being answered
        if not over():
            break
        if i not in (pin, kept):
            t.chat[i] = truncate_words(t.chat[i], SHORT_CHAT_CHARS)
    if over():
        log.warning("prompt over its %d-token budget after the usual trims: last-resort trim",
                    budget)
    if t.facts:
        drop_chat(least)
    drop(t.memories, 0, False)
    drop(t.own_recent, 0, True)
    drop(t.rel_cards, 0, False)
    drop_chat(least)
    t.pinned, t.kept = pin, kept
    return t


def redact_for(parts: PromptParts, human: HumanIdentity, close: bool) -> PromptParts:
    """What a speaker outside his inner circle (`close` False: knows_real_name) may see: his
    real name in its
    configured, Title or UPPER form (personas.screens.redact_real_name; lower case is the
    ordinary word it may also be) becomes human.called in the chat, cards and notes,
    memories, mood, own lines and instruction, and the real-name line is dropped. A close
    speaker's parts come back unchanged.

    Task 16 calls this for every speaker who is not close, and also runs redact_real_name over
    such a speaker's own output before it is sent."""
    if close or not human.real_name:
        return parts

    def r(text: str) -> str:
        return redact_real_name(text, human, human.called)

    return replace(parts, rel_cards=[r(x) for x in parts.rel_cards],
                   memories=[r(x) for x in parts.memories], mood=r(parts.mood),
                   chat=[r(x) for x in parts.chat], own_recent=[r(x) for x in parts.own_recent],
                   instruction=r(parts.instruction), facts=[r(x) for x in parts.facts],
                   real_name="")


def build_messages(parts: PromptParts, *, budget: int = LIVE_BUDGET) -> list[dict]:
    return render_messages(trim_to_budget(parts, budget=budget))
