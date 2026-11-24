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
under FACTS_HEAD; the budget never cuts them and gives up older chat first.

The situation mode (human-likeness core, spec section 9; wired by the pipeline only with
[talk] situation) keeps only the hard rules in the system message (SITUATION_RULES,
situation_rules), moves "Right now:" next to the instruction (PromptParts.situation) and takes
the reply's length from the caller (SITUATION_INSTRUCTIONS, render_instruction's
`length_text`)."""
import logging
import random
import re
from dataclasses import dataclass, field, replace
from pathlib import Path

from leeroy_mind.discord.channels import is_discord, is_private
from leeroy_mind.drama.config import pinned as member_romance_pinned
from leeroy_mind.human import THEY_FORMS, HumanIdentity
from leeroy_mind.personas.schema import Persona, Typing
from leeroy_mind.personas.screens import redact_real_name
from leeroy_mind.storage.rows import ROMANCE_STAGES, ChatLineRow, PersonaStateRow, Relationship
from leeroy_mind.talk.budget import DayPhase
from leeroy_mind.talk.states import RAID_CALLS, Altered, state_line
from leeroy_mind.talk.turns import TurnKind
from leeroy_mind.text import estimate_tokens, strip_wow_markup, truncate_words
from leeroy_mind.tuning.lines import tuning_lines

log = logging.getLogger("leeroy_mind")

# The fixed instruction can reach ~480 tokens on an ordinary late whisper, before
# facts/recent moves. 1300 squeezed out the three-memory/three-own-line floors.
# LIVE has num_ctx=4096 and num_predict=80: 1800 estimated input tokens leaves
# 2216 for token-estimation error and the model's message template/output.
LIVE_BUDGET = 1800
# A Discord DM with him (2026-11-15: all 18 last-resort trims since 11-14 were DM answers). Its
# prompt holds the DM's history in Chat and asks for up to [talk] dm_words, so it gets more
# room. num_ctx 4096 less num_predict for 200 words (discord_length.num_predict: 344) leaves
# 3752; 2600 estimated tokens stays under that even if the real count runs 25% over the
# estimate (3250, plus the chat template's few dozen), with about 400 to spare.
DM_BUDGET = 2600
ESTIMATE_SLACK = 1.25  # how far over the 4-characters estimate a real token count may run
EXTRA_CAP_TOKENS = 300  # an overlay's bit, romance or aside text, at most
MIN_MEMORIES, MIN_CHAT, MIN_REL_CARDS, MIN_OWN = 3, 5, 1, 3
# The last resort keeps this many memories, and the awareness card's claims, until her own
# last lines are gone (2026-11-15: the DM trims emptied memories and kept 640 tokens of her own
# paragraphs).
PROTECTED_MEMORIES = 2
CLAIMS_PART = "claims"  # the awareness card's part (awareness.card PRIORITY) the trim keeps last
# Her own long lines (a Discord message of up to 200 words) are shown as their opening words;
# the repeat check still reads them whole (Pipeline._own_lines).
REPLAY_WORDS = 30
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
    awareness: str = ""
    # One mind per member: their lines from the other medium (a whisper's DMs, a DM's whispers),
    # chat_line_text lines oldest first, shown before the chat and never the line answered
    elsewhere: list[str] = field(default_factory=list)
    style: list[str] = field(default_factory=list)
    # Whole sections the trim never cuts or shortens (the persona's own facts, an "About you"
    # block), each with its own heading, first in the user message
    untrimmed: list[str] = field(default_factory=list)
    # The situation mode: "Right now:" goes after the awareness card, just before the
    # instruction, so her situation is what she reads last (never cut, as before)
    situation: bool = False
    # What this private conversation with him has established (talk/sofar.py: his news, what
    # she said of where she is, open questions and plans), a few short code-built lines under
    # SO_FAR_HEAD after the chat; at most sofar.MAX_TOKENS, so the trim never cuts it
    so_far: list[str] = field(default_factory=list)
    # A DM's chat: how many of its leading rows are older than his DM_KEEP_TURNS-th last message.
    # When the prompt is over its budget these go first, before her style notes, awareness and
    # memories (review of #368: in the longest conversations those gave way to chat from ten
    # turns back; the so-far block carries his older news)
    old_chat: int = 0
    # Her voice sheet's block (personas.speech.voice_block; "" without a sheet): the last
    # thing before the instruction in both modes, never cut
    voice: str = ""
    # [geography] "Where you live" (geography.lines.now_parts: her place, the season there and
    # the light, at most NOW_TOKENS together), just before "Right now:" in either mode, on her
    # private and Discord turns. The first thing the trim lets go, its last part first
    place: list[str] = field(default_factory=list)
    # A raid or guild turn's grounding ([talk] wow_grounding, talk/grounding.py): its whole
    # sections, highest priority first (the card, her example lines, the glossary), after the
    # awareness card; the trim drops them from the end, before memories and chat give way
    wow: list[str] = field(default_factory=list)


def replay(text: str, words: int = REPLAY_WORDS) -> str:
    """A long line of hers as a prompt shows it: its first `words` words and "...", whole when
    it is no longer than that."""
    parts = text.split()
    return text if len(parts) <= words else " ".join(parts[:words]) + "..."


def replay_tail(text: str, words: int = REPLAY_WORDS) -> str:
    """A long line of hers as a DM's chat shows it: "..." and its last `words` words (what she
    ended on, which his next message answers), whole when it is no longer than that."""
    parts = text.split()
    return text if len(parts) <= words else "... " + " ".join(parts[-words:])


ELSEWHERE_HEAD = "Earlier between you two, elsewhere (you remember it; it is not the line you answer):"
SO_FAR_HEAD = ("So far between you two (keep to it, never contradict it; if he asks about "
               "something he told you that is not here or in the chat, say you don't remember, "
               "never guess):")
STYLE_HEAD = "Your recent style notes (observations, not rules; use only what fits this moment):"
STYLE_CAP_TOKENS = 220  # input observations only; output length belongs to the channel preset

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

# The situation mode's rules (spec section 9, bake-off 1: the rule list primed compliance; the
# variant cut to the hard rules scored best on public turns): only the hard ones, the 2009 rule
# without the year leak the DM audit found, the history and guilt-trip rules kept, and the
# nothing-rule, which the measured variant kept (1.71 public alone, 1.82 with the beat; review
# of #333, ruling S1). No length: the instruction carries the reply's (render_instruction).
SITUATION_RULES = """\
How you talk here:
- You're a real person in 2009, in your WoW guild. Real life stays in 2009 or is timeless. Never \
say the year.
- You're not an assistant. Never mention AI, language models, prompts or bots.
- Write only your own words: no name in front, no quotes, no one else's lines, no narrated actions.
- Shared history comes only from your memories. Never invent it.
- Never guilt-trip {human} for being away.
- If you wouldn't say anything, answer exactly (nothing)"""


# Every woman in the guild is eager to help him with quests (owner, 2026-10-30), and claims
# nothing about what he has done: P1b doesn't know (prompts I7). It ends a woman's reply to his
# quest line, where she reads it last (rehearsal 2, judge C 5), and nowhere else: in the
# standing rules it primed a quest in every greeting (judge B 3).
QUEST_HELP = ("{human} brought up a quest, and you jump at it: end your line with an offer to "
              "help {them} with it or to come along. You only know which quests {human} has "
              "done if {they} told you.")
# Flirting is only ever aimed at him (content invariant, prompts I10; filters.member_flirt screens
# every line). Since 2026-11-15 a woman reads that she may flirt with him, and a man that the women
# only have eyes for him, only on a turn that is his (Pipeline._aimed_at_him; for the men, also
# one where he is in the talk): on every other turn it pulled unrelated talk toward him. Those
# turns read FLIRT_RULE_AWAY, which still keeps flirting off every guildmate.
FLIRT_RULE = {
    "female": ("- You flirt only with {human}. Everyone else is a friend, a rival or just a "
               "guildie."),
    "male": ("- You don't flirt with anyone in the guild. The women only have eyes for {human} "
             "anyway."),
}
FLIRT_RULE_AWAY = {
    "female": "- You don't flirt with anyone here: they're friends, rivals or just guildies.",
    "male": "- You don't flirt with anyone in the guild.",
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


def comms_rules(human: HumanIdentity, persona: Persona | None = None, *,
                data_dir: Path | None = None, now: float | None = None,
                channel: str = "", recipient: int | None = None,
                at_him: bool | None = None, with_him: bool = False) -> str:
    """COMMS_RULES for this guild: who the human is (never his real name), then the rules
    with his name and the persona's line length (length_rule), then the persona's FLIRT_RULE,
    then the persona's tuning lines (tuning.lines: only when the owner has turned a wording dial
    far; the owner's overlays in `data_dir`'s prompts when given). The same on every turn for a
    speaker of the same kind, for Ollama's prompt cache. `at_him`: whether the turn is his (the
    pipeline's _aimed_at_him), `with_him` whether he is in the talk; with at_him False a woman
    reads FLIRT_RULE_AWAY, and so does a man unless `with_him`. None (no turn: a probe, a card
    preview) keeps FLIRT_RULE."""
    from leeroy_mind.prompts import note_template
    note_template("talk.comms",COMMS_RULES)
    rules = COMMS_RULES.format(human=human.called)
    if persona is not None:
        rules = rules.replace(SHORT_LINE, length_rule(persona.typing))
    return _in_guild(human, persona, rules, data_dir=data_dir, now=now, channel=channel,
                     recipient=recipient, at_him=at_him, with_him=with_him)


def situation_rules(human: HumanIdentity, persona: Persona | None = None, *,
                    data_dir: Path | None = None, now: float | None = None,
                    channel: str = "", recipient: int | None = None,
                    at_him: bool | None = None, with_him: bool = False) -> str:
    """comms_rules for the situation mode: the same prefix (who the human is) and suffix (the
    flirt rule, tuning lines and nudges), with SITUATION_RULES in place of COMMS_RULES and no
    length_rule (the instruction carries the length). The same on every turn for a speaker of
    the same kind, for Ollama's prompt cache."""
    from leeroy_mind.prompts import note_template
    note_template("talk.situation_rules", SITUATION_RULES)
    return _in_guild(human, persona, SITUATION_RULES.format(human=human.called),
                     data_dir=data_dir, now=now, channel=channel, recipient=recipient,
                     at_him=at_him, with_him=with_him)


def _in_guild(human: HumanIdentity, persona: Persona | None, rules: str, *,
              data_dir: Path | None, now: float | None, channel: str, recipient: int | None,
              at_him: bool | None, with_him: bool) -> str:
    """`rules` after who the human is (never his real name), then for a persona its flirt rule
    (comms_rules says which), its tuning lines and its nudges."""
    from leeroy_mind.prompts import note_template
    out = f"In your guild: {human.describe(include_real_name=False)}.\n" + rules
    if persona is None:
        return out
    gender = persona.identity.player_gender
    away = at_him is False and not (gender == "male" and with_him)
    if away and gender in FLIRT_RULE_AWAY:
        note_template("talk.flirt_rule_away."+gender,FLIRT_RULE_AWAY[gender])
        out += "\n" + FLIRT_RULE_AWAY[gender]
    elif gender in FLIRT_RULE:
        note_template("talk.flirt_rule."+gender,FLIRT_RULE[gender])
        out += "\n" + FLIRT_RULE[gender].format(human=human.called)
    for line in tuning_lines(persona, data_dir):
        out += "\n- " + line
    from leeroy_mind.tuning.nudges import prompt_lines
    for line in prompt_lines(persona, now=now, channel=channel, recipient=recipient,
                             human=human.guid):
        out += "\n- " + line
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


# Several lines of his wait for one answer ([talk] coherence; Pipeline._his_waiting): each is
# answered, his question first. His own words, already in her chat, quoted so none is missed.
UNANSWERED = ("{He} wrote {n} lines you haven't answered yet: {lines}. Answer every one of "
              "them in this reply, {first}.")
UNANSWERED_QUESTION = "{their} question first"
UNANSWERED_OLDEST = "the oldest first"
UNANSWERED_WORDS = 30  # each quoted line's words
UNANSWERED_MOST = 4  # the newest lines quoted


def unanswered_text(lines: list[str], human: HumanIdentity) -> str:
    """The instruction for an answer that covers several of his lines (oldest first); "" for
    fewer than two."""
    from leeroy_mind.talk.asking import is_question
    raw = [x for x in lines if x and x.strip()][-UNANSWERED_MOST:]
    lines = [" ".join(x.split()) for x in raw]
    if len(lines) < 2:
        return ""
    he, _, their = human.forms
    quoted = " / ".join('"' + " ".join(x.split()[:UNANSWERED_WORDS]) + '"' for x in lines)
    # ([talk] coherence's own instruction: a question buried in a line counts too, read in his
    # own text, whose line breaks end clauses)
    first = (UNANSWERED_QUESTION.format(their=their)
             if any(is_question(x, buried=True) for x in raw)
             else UNANSWERED_OLDEST)
    return UNANSWERED.format(He=he.capitalize(), n=len(lines), lines=quoted, first=first)


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
    # the whole instruction is the named template talk/emotes.py fills (the owner's overlay
    # replaces it), so it comes as the extra
    TurnKind.EMOTE_REACT: "{extra}",
}

# An opener addressed to him (engine._open_to_him, owner 2026-11-07: nobody ever started a
# conversation with him): a casual line in party or guild that names him; the topic says what
# it brings up (TalkEngine._to_him_topic). It never carries her romance, which goes by whisper.
OPEN_TO_HIM = ("Start a conversation with {addressee} in one short line, the way you'd type it: "
               "say {their} name and {topic}. It's your line to start: nothing in chat is "
               "addressed to you. {extra}")

# The situation mode's templates (render_instruction's `situation`): no length of their own and
# no "respond to what they actually said" (the rule list primed compliance, bake-off 1). The
# caller's length goes before {extra}; every other kind keeps its INSTRUCTIONS template.
SITUATION_INSTRUCTIONS: dict[TurnKind, str] = {
    TurnKind.REPLY_HUMAN: "Reply to {addressee}'s last line, as yourself. {extra}",
    TurnKind.CONTINUE: ("Reply to {addressee}'s last line only if you have something to add. If "
                        "you'd let it go, answer (nothing). {extra}"),
    TurnKind.OPEN: ("{quiet}Bring up {topic}, the way you'd type it. It has to make sense on its "
                    "own: nothing in chat is addressed to you. If it isn't worth saying, answer "
                    "(nothing). {extra}"),
    TurnKind.WHISPER: "You're whispering {addressee} privately. {extra}",
    TurnKind.FLIRT: "Say something to {addressee} in chat, the way you feel about {them}. {extra}",
}
OPEN_TO_HIM_SITUATION = ("Start a conversation with {addressee}, the way you'd type it: say "
                         "{their} name and {topic}. Nothing in chat is addressed to you. {extra}")

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
# The same as a sentence of its own, for a situation template, which has no length to stretch:
# without "more than your usual one line", since a situation prompt states no usual line
# (review-340; core plan Task 8; Task 9's reply shapes take its place)
MORE_LINES_TEXT = "In a line or two."
# What a Discord length says of her game habit; a situation template leaves it out (review-340)
USUAL_LINE = ", so more than your usual one line"

# A Discord turn ([talk] discord_words, talk/discord_length.py; owner 2026-11-13, "Long and
# chatty"): its template's game length goes (DISCORD_DROPS, each with its comma or space, so the
# sentence still reads), and DISCORD_LENGTH ends the template, before its extra (his post, the
# seed), so what she answers stays last, then the Lately line and her tail as before. It says it
# is the exception to the standing rules' one line, as MORE_LINES does, so the system message
# (Ollama's cached prefix) stays the same for the game.
DISCORD_LENGTH = ("This is Discord, not the game, so more than your usual one line: write it the "
                  "way you'd write in {where}, usually a few sentences, a short paragraph when "
                  "you have something to say (at most about {words} words), a single quick "
                  "sentence when that's all it needs. One paragraph, no line breaks.")
DISCORD_WHERE = {"dm": "a Discord DM thread", "channel": "Discord chat"}
# A channel message drawn short (discord_length.band) reads as a quick line or a couple of
# sentences; a DM with him is as full as she likes (owner, 2026-11-13: "DMs max expressive"),
# as long as the moment needs and no longer (owner's live DM test, 2026-11-16: "write as fully
# as you feel" made every answer a paragraph, his quick questions included).
DISCORD_QUICK = ("This is Discord chat, not the game: a quick message the way people type in a "
                 "busy channel, a few words or one sentence (at most about {words} words). "
                 "No line breaks.")
DISCORD_FEW = ("This is Discord chat, not the game, so more than your usual one line: a couple "
               "of sentences (at most about {words} words). One paragraph, no line breaks.")
DISCORD_DM = ("This is your private Discord DM, not the game. Write as much or as little as the "
              "moment needs: a quick line when {they} asked something quick, more when you have "
              "something to say (at most about {words} words). One paragraph, no line breaks.")
DISCORD_DROPS = ((", in one short line", ""), (" in one short line", ""), (", one short line", ""),
                 ("One short line, the way", "Say it the way"), (", in a few words", ""))


def discord_length(channel: str, words: int, they: str = "they") -> str:
    """DISCORD_LENGTH for a turn on `channel` holding about `words`: a DM's or a channel's; ""
    for a game channel or with no words (the one short line, as before). `they`: the
    addressee's subject pronoun (a DM's is his: HumanIdentity.forms)."""
    template = discord_template(channel, words)
    if not template:
        return ""
    return template.format(where=DISCORD_WHERE["channel"], words=words, they=they)


def discord_template(channel: str, words: int) -> str:
    """The wording a Discord turn on `channel` of `words` reads: DISCORD_DM in a DM, else by its
    band (discord_length.QUICK_WORDS, FEW_WORDS), DISCORD_LENGTH for a paragraph; "" for a game
    channel or no words."""
    from leeroy_mind.talk.discord_length import FEW_WORDS, QUICK_WORDS
    if words <= 0 or not is_discord(channel):
        return ""
    if is_private(channel):
        from leeroy_mind.talk.pieces import text
        return text("discord_dm", DISCORD_DM)
    return DISCORD_QUICK if words <= QUICK_WORDS else DISCORD_FEW if words <= FEW_WORDS else DISCORD_LENGTH


def situation_length(kind: TurnKind, *, channel: str, words: int, typing: Typing | None,
                     two_lines: bool = False) -> str:
    """The `length_text` a situation-mode turn passes render_instruction (review of #333,
    requirement 1: every situation template gets its length from the caller), for a
    SITUATION_INSTRUCTIONS kind: an explicit whisper's two lines (MORE_LINES_TEXT); else ""
    where a Discord length applies (discord_length, which render_instruction gives a situation
    template without its "more than your usual one line": USUAL_LINE); else her usual line
    (length_rule; SHORT_LINE without her typing), on a game channel, in a voice room or on
    Discord at 0 words. "" for every other kind: it keeps its INSTRUCTIONS template and length."""
    if kind not in SITUATION_INSTRUCTIONS:
        return ""
    if two_lines:
        return MORE_LINES_TEXT
    if discord_length(channel, words):
        return ""
    return length_rule(typing) if typing is not None else SHORT_LINE


def render_instruction(kind: TurnKind, *, addressee: str = "", topic: str = "",
                       extra: str = "", forms: tuple[str, str, str] = THEY_FORMS,
                       lead: str = "", after: str = "", tail: str = "", long: bool = False,
                       quiet: bool = True, to_him: bool = False, channel: str = "",
                       discord_words: int = 0, length_text: str = "",
                       situation: bool = False, typing: Typing | None = None) -> str:
    """The TurnKind template on one line. `lead` (her romance text) comes before it, so the
    turn's own instruction is what the model reads last (prompts I5); `extra` (a bit, an aside,
    a fact) goes after it, cut to EXTRA_CAP_TOKENS, and a cut is logged without the text.
    `after` (the "Lately you've..." line) goes after that and is never cut.
    `tail` (the owner's whisper overlay, capped by its own rule) goes after everything, the
    last thing she reads; `long` lets the line run to "a line or two". An opener says "Chat
    has gone quiet." only when `quiet` (rehearsal 2: it said so under lines from just now).
    `forms` are the addressee's pronouns (HumanIdentity.forms when it is the human); nothing
    is assumed without them. `to_him` on an OPEN: the opener addressed to him (OPEN_TO_HIM).
    A turn on a Discord `channel` with `discord_words` writes as on Discord (discord_length, in
    place of the one short line and of `long`, at the template's end before `extra`, never cut);
    any other turn is as before. `length_text` (the reply's drawn length) goes in the same place
    the same way and wins over the Discord length and `long`.
    `situation` takes the template from SITUATION_INSTRUCTIONS (OPEN_TO_HIM_SITUATION for an
    opener to him; a kind it has none for keeps its INSTRUCTIONS template). Those carry no
    length of their own, so one with neither `length_text` nor a Discord length (a game channel,
    a voice room, Discord at 0 words) takes the speaker's usual line, length_rule(`typing`), or
    SHORT_LINE without one: never an error, so no turn is dropped for it. `long` does not apply
    to them: the pipeline passes the shape, two-lines or Discord length explicitly."""
    from leeroy_mind.talk import pieces
    own = situation and kind in SITUATION_INSTRUCTIONS
    if own:
        tmpl = (OPEN_TO_HIM_SITUATION if to_him and kind is TurnKind.OPEN
                else pieces.instruction(kind.value, True, SITUATION_INSTRUCTIONS[kind]))
    else:
        tmpl = (OPEN_TO_HIM if to_him and kind is TurnKind.OPEN
                else pieces.instruction(kind.value, False, INSTRUCTIONS[kind]))
    shaped = length_text.strip()
    on_discord = "" if shaped else discord_length(channel, discord_words, forms[0])
    length = shaped or on_discord
    if own and not length:  # her usual line, in the words comms_rules has for it
        length = length_rule(typing) if typing is not None else SHORT_LINE
    if own:  # no usual line stated in a situation prompt to be more than (review-340)
        length = length.replace(USUAL_LINE, "")
    if length:
        for old, new in DISCORD_DROPS:
            tmpl = tmpl.replace(old, new)
        tmpl = (tmpl.replace("{extra}", "{length} {extra}") if "{extra}" in tmpl
                else tmpl + " {length}")
    elif long:
        tmpl = tmpl.replace("in one short line", MORE_LINES).replace(
            "One short line", MORE_LINES[3:].capitalize())
    from leeroy_mind.prompts import note_template
    note_template(("talk.situation_instruction." if own else "talk.instruction.")+kind.value,tmpl)
    if on_discord:
        noted = discord_template(channel, discord_words)
        note_template(*(("talk.situation_discord_length", noted.replace(USUAL_LINE, "")) if own
                        else ("talk.discord_length", noted)))
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
                       quiet="Chat has gone quiet. " if quiet else "", length=length)
    return " ".join(f"{lead} {text} {after} {tail}".split())


def select_samples(p: Persona, n: int = 5) -> list[str]:
    """Approved sample lines first, each group in persona order, so the prefix is stable."""
    ordered = ([s for s in p.sample_lines if s.approved]
               + [s for s in p.sample_lines if not s.approved])
    return [t for t in (" ".join(s.text.split()) for s in ordered) if t][:n]


# How a label reads on a card (prompts lens, minor 13: the schema word reached the model).
LABEL_TEXT = {"friend": "you're friends", "friction": "you two clash", "rival": "you're rivals"}
# A member romance on a card between two members ([drama] member_romance writes these stages on
# member pairs; nothing else does), only while the mind started with that switch on. Group
# turns never show it (friendship_only).
MEMBER_ROMANCE_TEXT = {"notices": "there's a spark between you", "banter": "you flirt",
                       "whispers": "you've got a crush on each other", "late_nights": "you're dating",
                       "confession": "you're together", "ex": "you broke up"}
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


# [romance] realism: her note toward him on his card is one sentence about how things are, never
# one with a time word (the night writes the note as of then, so "...to me tomorrow" read as
# current for a week in every prompt) nor one citing words she picked up (dm-quality cleans
# those from storage; this keeps a stale row off the card meanwhile).
_SENTENCE = re.compile(r"(?<=[.!?])\s+")
_NOTE_DATED = re.compile(
    r"\b(?:tonight|tomorrow|today|right now|next week"
    r"|this (?:morning|afternoon|evening|weekend|week))\b|\bpicked up\b", re.IGNORECASE)


def realism_note(notes: str) -> str:
    """Her note's first sentence without a time word or a picked-up word list ([romance]
    realism), "" when none is left."""
    return next((s for s in (x.strip() for x in _SENTENCE.split(notes))
                 if s and not _NOTE_DATED.search(s)), "")


def far_note(notes: str) -> str:
    """Her note toward him without the sentences that see him in person (talk.moves.in_person:
    "a rematch she wants to settle in person"), for a member whose arc has not met him (evening
    DM audit, 2026-11-16: such a note was in every prompt and her answers planned a date with
    him). The stored note is unchanged; with nothing to leave out, the note as it was."""
    from leeroy_mind.talk.moves import in_person
    sentences = [s for s in (x.strip() for x in _SENTENCE.split(notes)) if s]
    kept = [s for s in sentences if not in_person(s)]
    return notes if len(kept) == len(sentences) else " ".join(kept)


def relationship_card(name: str, r: Relationship | None, *, is_human: bool,
                      is_guild_master: bool = False,
                      forms: tuple[str, str, str] = THEY_FORMS, attitude: str = "",
                      speaker: str = "", friendship_only: bool = False,
                      pronoun: str = "", realism: bool = False, far: bool = False,
                      show_member_romance: bool = True) -> str:
    """How the speaker feels about one person, in a few words. Only the warm side of trust
    and respect is shown: a low score usually just means "not yet". The human is tagged "(the
    guild master)" when he is one (HumanIdentity.is_guild_master) and referred to by `forms`
    (HumanIdentity.forms); anyone else as them/their, a member tagged with their `pronoun`
    ("Razke (she)": identity audit, 2026-11-15, the officers called women "him" for want of
    it). On his card a woman's `attitude` (her stance) shows as her attraction band, and her
    note toward him gets HUMAN_NOTE_CHARS. A member with no row at all is GUILDMATE; a label
    reads as how the two get on. `realism` ([romance] realism, his card only): the note is
    realism_note's one sentence. `far` (his card, her arc has not met him): far_note's."""
    if pronoun and not is_human:
        name = f"{name} ({pronoun})"
    if not is_human and (r is None or r == Relationship(a=r.a, b=r.b)):
        return f"{name}: {GUILDMATE}"
    r = r or Relationship(a=0, b=0)
    _, them, their = forms if is_human else THEY_FORMS
    bits = []
    if not friendship_only and not is_human and r.label.strip() in LABEL_TEXT:
        bits.append(LABEL_TEXT[r.label.strip()])
    if (show_member_romance and not friendship_only and not is_human and r.romance_stage in MEMBER_ROMANCE_TEXT
            and member_romance_pinned()):
        bits.append(MEMBER_ROMANCE_TEXT[r.romance_stage])  # a member romance (drama.couples)
    band = attraction_band(r, attitude) if is_human and not friendship_only else ""
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
    # Group turns keep friendship scores, never romantic labels or free-form
    # relationship notes (which can contain private romantic material).
    notes = "" if friendship_only else " ".join(r.notes.split()).rstrip(".")
    if far and is_human:
        notes = far_note(notes).rstrip(".")
    if realism and is_human:
        notes = realism_note(notes).rstrip(".")
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
              lately: str = "", variant: int = 0, today: str = "", where: str = "",
              state_words: str = "", held: bool = False) -> str:
    """The "Right now:" line: time of day (`today`, the weekday and tonight's holidays from
    seasons.day_line, in its place when given), mood and any altered state. Openers
    (IDLE_KINDS) also get what has been on their mind (state.life_thread, else the persona's
    own `life_thread`: P1b writes no state, spec I3), what they did tonight (`lately`) and a
    smoker's habit; raid calls (CALLOUT, URGENT) get no altered state. `where` (where she is in
    her day) follows the time of day; `state_words` (how she feels right now) takes the place
    of the mood words when given. `held` (a turn held to its channel's business): nothing of
    what has been on her mind or what she did tonight, the state's own thread included."""
    idle = kind in IDLE_KINDS and not held
    life = " ".join((state.life_thread or life_thread).split()).rstrip(".") if idle else ""
    tonight = " ".join(lately.split()).rstrip(".") if idle else ""
    shown = "" if kind in RAID_CALLS else state_line(
        altered, substance if idle or Altered(altered) is not Altered.SOBER else None,
        variant=variant)
    parts = [today or _PHASE[phase], " ".join(where.split()),
             " ".join(state_words.split()) or _mood_words(state.mood), shown,
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
                   now: float | None = None, photo: str = '', pronoun: str = '') -> str:
    """'Name: text' for guild chat, 'Name (raid): text' elsewhere, 'Name (on vent): text' in a
    voice room, markup stripped, and with `now` its age in front ('[3m ago] Name: text'), so an
    old line never reads as current. A `pronoun` goes in the same brackets ('Razke (she):',
    'Razke (she, raid):'), so the frame keeps one tag a copied speaker tag is read by.
    `names` maps guids to how the speaker knows them (the human by his nickname)."""
    who = names.get(row.from_guid) or row.from_name or "someone"
    text = " ".join(strip_wow_markup(row.text).split())
    if photo:
        text += ' ' + ' '.join(photo.split())
    if row.channel == "guild":
        tag = ""
    elif row.channel == "whisper":
        to = names.get(row.to_guid)
        tag = f" (whisper to {to})" if to else " (whisper)"
    elif row.channel.startswith("voice:"):
        tag = " (on vent)"
    elif row.channel.startswith("dm:"):
        to = names.get(row.to_guid)
        tag = f" (Discord DM to {to})" if to else " (Discord DM)"
    elif row.channel.startswith("discord:"):
        tag = f" (Discord #{row.channel.removeprefix('discord:')})"
    else:
        tag = f" ({row.channel})"
    if pronoun:
        tag = f" ({pronoun}, {tag[2:-1]})" if tag else f" ({pronoun})"
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
    """The two messages, untrimmed. Empty sections are left out. In the situation mode
    (p.situation) "Right now:" comes after the awareness card, just before the instruction."""
    system = "\n\n".join(x for x in (
        p.card.strip(), _section(SAMPLES_HEAD, p.samples), p.rules.strip()) if x)
    people = [*p.rel_cards, *([p.real_name] if p.real_name else [])]
    right_now = "\n\n".join(x for x in (" ".join(p.place), _section("Right now:", [p.mood]))
                            if x)
    user = "\n\n".join(x for x in (
        *(u.strip() for u in p.untrimmed),
        _section("People here:", people),
        _section("You remember:", p.memories),
        "" if p.situation else right_now,
        _section(STYLE_HEAD, p.style),
        _section(ELSEWHERE_HEAD, p.elsewhere),
        _section("Chat:", p.chat),
        _section(SO_FAR_HEAD, p.so_far),
        _section(p.facts_head or facts_head("them"), p.facts),
        _section("Your last lines (don't make the same move again):", p.own_recent),
        p.awareness.strip(),
        *(w.strip() for w in p.wow),
        right_now if p.situation else "",
        p.voice.strip(),
        p.instruction.strip()) if x)
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _tokens(msgs: list[dict]) -> int:
    return sum(estimate_tokens(m["content"]) for m in msgs)


def section_tokens(p: PromptParts) -> dict[str, int]:
    """Rendered section estimates, including headings; never any prompt text."""
    return {name: estimate_tokens(text) for name, text in (
        *((("voice", p.voice.strip()),) if p.voice.strip() else ()),
        ("card", p.card.strip()), ("samples", _section(SAMPLES_HEAD, p.samples)),
        ("rules", p.rules.strip()),
        ("untrimmed", "\n\n".join(u.strip() for u in p.untrimmed if u.strip())),
        ("people", _section("People here:", [*p.rel_cards, *([p.real_name] if p.real_name else [])])),
        ("memories", _section("You remember:", p.memories)),
        *((("place", " ".join(p.place)),) if p.place else ()),
        ("mood", _section("Right now:", [p.mood])),
        ("style", _section(STYLE_HEAD, p.style)),
        ("elsewhere", _section(ELSEWHERE_HEAD, p.elsewhere)), ("chat", _section("Chat:", p.chat)),
        ("so_far", _section(SO_FAR_HEAD, p.so_far)),
        ("facts", _section(p.facts_head or facts_head("them"), p.facts)),
        ("own_recent", _section("Your last lines (don't make the same move again):", p.own_recent)),
        ("awareness", p.awareness.strip()),
        *((("wow", "\n\n".join(w.strip() for w in p.wow)),) if p.wow else ()),
        ("instruction", p.instruction.strip()))}


def trim_to_budget(p: PromptParts, *, budget: int = LIVE_BUDGET) -> PromptParts:
    """A copy that fits the budget. Her "where you live" line ([geography], PromptParts.place)
    shrinks first, its last part first; then a DM's chat rows older than his last few turns go
    (old_chat), then learned style, then the awareness card is
    re-rendered into the remaining room, but never below its claims (CLAIMS_PART, kept whole).
    Then, in order: a raid or guild turn's grounding sections (wow: the glossary, her example
    lines, then the card), the other medium's lines, memories down to 3 (the least relevant go), chat
    down to 5 (the oldest go), relationship cards down to 1 (the addressee's stays), own lines
    down to 3 (the oldest go), then the oldest chat lines are shortened (never the newest).
    Past that, a logged last resort empties her own lines first, then lets the claims go, then
    memories down to PROTECTED_MEMORIES, then relationship cards, drops chat down to the newest
    line, and only then the last memories and the rest of the awareness card (2026-11-15: her
    memories and claims outrank her own long lines). With facts, the older chat goes first in
    both rounds (fix wave E: the facts answer his line, the chat before it matters less), and
    his line the reply answers (pinned) is never cut nor shortened: it is the floor, even
    when later answers came after it. The speaker's own line his pushback is about (kept, fix
    wave G6) is never cut nor shortened either, and the last resort keeps it with the floor.
    The system message, the untrimmed sections, mood, real-name line, facts, the conversation's
    "so far" lines (bounded where they are built), her voice block and instruction are never
    cut (the mood in either place: PromptParts.situation). Memories stay a prefix of the input,
    so len(result.memories) says which recalled memories were used."""
    t = replace(p, rel_cards=list(p.rel_cards), memories=list(p.memories), chat=list(p.chat),
                elsewhere=list(p.elsewhere), style=list(p.style), so_far=list(p.so_far),
                own_recent=list(p.own_recent), untrimmed=list(p.untrimmed), place=list(p.place),
                wow=list(p.wow))
    pin = p.pinned if p.facts and p.pinned is not None and 0 <= p.pinned < len(p.chat) else None
    kept = p.kept if p.kept is not None and 0 <= p.kept < len(p.chat) else None
    least = 1 if kept is None else 2  # the chat the last resort leaves

    def over() -> bool:
        return _tokens(render_messages(t)) > budget

    # Her "where you live" line shrinks first, its last part first ([geography]: her card's
    # head still says where she lives).
    while t.place and over():
        t.place.pop()

    # A long DM's oldest chat rows go before anything else (PromptParts.old_chat).
    old = max(0, min(p.old_chat, len(t.chat) - MIN_CHAT))
    while old > 0 and over():
        i = next((j for j in range(old) if j not in (pin, kept)), None)
        if i is None:
            break
        t.chat.pop(i)
        old -= 1
        if pin is not None and i < pin:
            pin -= 1
        if kept is not None and i < kept:
            kept -= 1
    t.old_chat = old

    # Learned style is expendable before evidence, including the other-medium
    # timeline. Drop whole notes so attribution remains a prefix, never half a fact.
    while t.style and (estimate_tokens(_section(STYLE_HEAD, t.style)) > STYLE_CAP_TOKENS or over()):
        t.style.pop()

    def fit_awareness(keep: tuple[str, ...] = ()) -> None:
        if not t.awareness or not over():
            return
        awareness, t.awareness = t.awareness, ""
        room = budget - _tokens(render_messages(t)) - 2  # separators and rounding
        if hasattr(awareness, "fit"):
            # the highest-priority whole sections; with `keep`, those stay whole past the room
            t.awareness = awareness.fit(room, keep=keep) if keep else awareness.fit(room)
    fit_awareness((CLAIMS_PART,))

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

    drop(t.wow, 0, False)  # the grounding's lowest sections first (PromptParts.wow)
    drop(t.elsewhere, 0, True)  # the other medium's lines go first, oldest first
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
        log.warning("prompt over its %d-token budget after the usual trims: last-resort trim; "
                    "total_tokens=%d section_tokens=%s", budget, _tokens(render_messages(t)),
                    section_tokens(t))
    if t.facts:
        drop_chat(least)
    drop(t.own_recent, 0, True)  # her own lines before her claims and memories
    fit_awareness()  # now the claims may go too
    drop(t.memories, PROTECTED_MEMORIES, False)
    drop(t.rel_cards, 0, False)
    drop_chat(least)
    drop(t.memories, 0, False)
    if t.awareness and over():
        t.awareness = ""
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
                   elsewhere=[r(x) for x in parts.elsewhere],
                   so_far=[r(x) for x in parts.so_far],
                   style=[r(x) for x in parts.style],
                   untrimmed=[r(x) for x in parts.untrimmed], voice=r(parts.voice),
                   wow=[r(x) for x in parts.wow],
                   instruction=r(parts.instruction), awareness=(parts.awareness.redacted(r)
                       if hasattr(parts.awareness,"redacted") else r(parts.awareness)), facts=[r(x) for x in parts.facts],
                   real_name="")


def build_messages(parts: PromptParts, *, budget: int = LIVE_BUDGET) -> list[dict]:
    return render_messages(trim_to_budget(parts, budget=budget))
