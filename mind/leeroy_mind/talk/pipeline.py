"""One turn's life in the talk engine: wait, check, compose, generate, filter, style, type and
send, then record what was said. This is the base layer of TalkEngine (engine.py); reactions.py
builds the event reactions on it. The public names are imported from leeroy_mind.talk.engine.

A turn is checked after its delay, before each typed line and again under the send lock: the
speaker must be online and not AFK (and alive for /say), and the raid rules, the loop guards
and the chain must allow it. The deadline guards how stale the trigger is (final fix wave
ruling): it is checked after the delay and after the model answers. A line the model gave in
time is never dropped for its typing: the first line's typing is capped to land inside the
deadline (fix wave G1, from rehearsal 2), and lines 2+ follow as typed. Guards count turns, so
lines 2+ of a turn pass the guild-wide and per-person limits as continuations; a turn is
recorded once, on its first acknowledged line, and that is when a bit counts as used. A line
the server has taken is always recorded, even when its turn is cancelled mid-send. Every line
gets one row in the utterance log: what was said, or the reason it wasn't.

With voice on (Pipeline.comms, set by the voice extension), a turn is placed once its channel
is known: a member on comms speaks in their room, on a `voice:<room>` channel that never
reaches the bridge. A voiced turn skips the typing: the model's words or the template's go to
the voice service as one line. With voice off, down or failing, every turn takes the typed path
above, unchanged: a line that can't be spoken is typed, and a fault there is logged by name."""
import asyncio
import itertools
import logging
import math
import random
import re
from collections import defaultdict, deque
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Awaitable, Callable

from leeroy_mind.bridge import AckTimeout, BridgeError, ConnectionLost
from leeroy_mind.clock import Clock, night_of
from leeroy_mind.config import MindConfig
from leeroy_mind.human import THEY_FORMS, HumanIdentity, human_identity
from leeroy_mind.llm.client import LLMError
from leeroy_mind.llm.presets import LIVE, LIVE_BIT
from leeroy_mind.llm.scheduler import Expired, LLMScheduler, Priority, SchedulerError
from leeroy_mind.memory.recall import Recall, whisper_ok
from leeroy_mind.personas.card import card_race_class, render_prompt_card
from leeroy_mind.personas.io import PersonaCache
from leeroy_mind.personas.schema import Persona
from leeroy_mind.personas.screens import redact_real_name
from leeroy_mind.protocol import Ack, Chat, Command, Emote, Say
from leeroy_mind.storage.rows import ChatLineRow
from leeroy_mind.storage.store import Store
from leeroy_mind.talk.budget import PHASE_MODS, DayPhase, day_phase
from leeroy_mind.talk.channels import choose_channel
from leeroy_mind.talk.filters import (Tell, aimed_off_him, find_tells, flirty, is_silence,
                                      line_addressee, member_flirt, names_any, repair,
                                      strip_copied, supported)
from leeroy_mind.talk.guards import Guards
from leeroy_mind.talk.facts import LibraryFacts
from leeroy_mind.talk.prompt import (PromptParts, chat_line_text, comms_rules, facts_head,
                                     knows_real_name, mood_line, quest_help, real_name_line,
                                     redact_for, relationship_card, render_instruction,
                                     render_messages, said_hi_text, select_samples,
                                     trim_to_budget)
from leeroy_mind.talk.questions import game_question
from leeroy_mind.talk.raid import RaidState
from leeroy_mind.talk.romance import (FULL_DIAL, NO_BRAKES_DIAL, dial_for, explicit_whisper,
                                      flirt_chance, own_dial, romance_instruction,
                                      whisper_tail)
from leeroy_mind.talk.select import (OPEN_THREAD_S, SpeakerContext, has_reason, is_quest_talk,
                                     mentioned, quest_helper, speaker_weight, typing_delay)
from leeroy_mind.talk.states import EFFECTS, Altered, Effects, effects_for, substance_of
from leeroy_mind.talk.seasons import day_line
from leeroy_mind.talk.style import (ROMANCE_EMOTES, Styled, command_only, emote_words,
                                    line_cap, split_emotes, style_lines, turn_lines)
from leeroy_mind.talk.turns import Turn, TurnKind
from leeroy_mind.talk.world import WorldView
from leeroy_mind.voice.comms import SPOKEN_HINT, typed
from leeroy_mind.wow import CLASSES, RACES

log = logging.getLogger("leeroy_mind.talk")

# Answers to the human. With a reply to his whisper they get Priority.REPLY_HUMAN.
REPLY_KINDS = frozenset({TurnKind.REPLY_HUMAN, TurnKind.GREET, TurnKind.GOODNIGHT})
DEADLINE_S = {Priority.REPLY_HUMAN: 30.0}
OTHER_DEADLINE_S = 20.0  # social talk and the raid lead; counted after the turn's delay
# The deadline governs the model's answer; the first line's typing is capped to land this far
# inside it, and never shorter than MIN_TYPING_S.
SEND_MARGIN_S = 1.0
MIN_TYPING_S = 0.5
# The social talk members make among themselves (controller rulings, Task 16 fix rounds 1-2):
# OPEN, CONTINUE, BIT, FLIRT and ASIDE. It stops when a chain (lines on one channel since the
# human last spoke there) reaches its cap. Answers to the human and reactions to the game
# count toward the chain but are never stopped by it.
CHAINED = frozenset({TurnKind.OPEN, TurnKind.CONTINUE, TurnKind.BIT, TurnKind.ASIDE,
                     TurnKind.FLIRT})
# Idle talk (the CHAINED kinds) sees only this much of the chat: older lines are left out, so
# a 16-minute-old topic never leads the prompt (rehearsal 2, judge A 2). Chat with no line for
# QUIET_S has gone quiet, and only then does an opener say so.
IDLE_WINDOW_S = 300.0
QUIET_S = 120.0
# A channel quiet this long starts a new conversation, as a thread does. Only the lines the
# engine sees (the human's and the mind's) keep a conversation open; other players' lines and
# his bot commands, which threads do count, don't, so this approximates the thread rule.
CHAIN_QUIET_S = 90.0
# The retry's hint says what was wrong with the first answer (prompts lens, minor 14).
REGEN_HINT = "Say it the way {name} would type it in game chat: short, not like a helper."
ECHO_HINT = "Don't say their line back: answer it in your own words."
# Answering his question (rehearsal 2, judge B 2: the echo retry broke a member's right answer).
ECHO_HIM_HINT = ("Don't say {their} line back: the same answer in your own words, and keep "
                 "what your memories say.")
COPY_HINT = "Don't copy a line from the chat: say your own thing."
REPEAT_HINT = "Don't repeat what you said before."
LONG_HINT = "Shorter: {lo}-{hi} words, the way you type."
SAMPLE_HINT = "Say it your own way: the examples only show how you type."
FRIENDS_HINT = "Keep it friendly: you only ever flirt with {human}."
# A turn of his whose line went to a member (fix round 1, review I1: "Keep it friendly" asked
# her flirt at him to stop flirting).
AT_HIM_HINT = "Say it to {human}: the line is for {them}, not for anyone else in the guild."
HISTORY_HINT = "Only bring up the past from what you remember."
SLANG_HINT = "Say it the way people typed in 2009."
# Her silence in answer to his whisper is asked again once (seen live, fix wave G3): his
# whispers get their answer. A second silence stands. At the explicit band a line or two
# (fix round 1, review M5).
SILENT_WHISPER_HINT = "Answer {human}'s whisper in {length}, in your own voice."
_HINTED = frozenset({"echo", "repeat", "near repeat", "long", "member flirt", "sample",
                     "history", "copy", "slang"})
# Soft tells a retry may not keep: a claim of shared history still there is dropped.
DROP_ON_RETRY = frozenset({"history"})
# A reply this far over the words its lines hold is asked again shorter (prompts C1).
LONG_SLACK = 1.25
# The second and third answers to his line see the ones before them (prompts I8), and so do
# the second and third greetings (the rehearsal's second greeter copied the first).
ANSWERED = "{names} already answered {them}. Add something they didn't, or answer (nothing)."
# On his game question only the first answer gets the facts (rehearsal 2, judge A 3).
ANSWERED_FACTS = "{names} already answered that. Add your own take, or answer (nothing)."
# His line about members she has no memory of (judge B 2): theirs to tell.
THEIR_STORY = "{name} asked {names}: it's their story, so react to it and don't add facts."
# His question about the guild's past that names nobody (fix wave G3, rehearsal 3 check 3):
# one who remembers it answers first, from that memory, and the rest react to their story;
# with nobody here who remembers it, nobody states a fact (engine._plan_lore).
KEEPER = "You remember this one: answer from that memory, in your own words."
THEIRS = "{names} remember{s} it: it's their story, so react to it and don't add facts."
NOBODY = ("Nobody here remembers that for sure{who}: joke about it or say you don't know, and "
          "state no fact about it.")
NOBODY_WHO = ", but {names} would know"
# A later answer to such a question: no "add something they didn't".
ANSWERED_LORE = "{names} already answered {them}."
GREETED = "{names} already said hi to {them}. Say hi your own way, or answer (nothing)."
# A later greeter's chat shows the greetings as "Belra said hi to Zan", without their words,
# and her instruction ends on this (rehearsal 2: the second greeter answered the first one).
TALK_TO = "Talk to {name}; nothing in chat is addressed to you."
# The best-known greeter follows up only on one memory of something he said or did in an
# earlier session, quoted (rehearsal 2, judge B 3): never on the owner's seeded canon, which
# made every greeter with a standing memory of him invent a "last time".
FOLLOW_UP = "Follow up on this, if it still fits: {memory}"
FOLLOW_SOURCES = frozenset({"chat", "extract", "game"})
# A woman with an attitude toward him greets him and says goodnight as she feels (rehearsal 2,
# judge C 6: the drawn women read as indifferent).
FEELINGS = "Let how you feel about {them} show, if a memory fits."
REAL_NAME_GAP_S = 600.0  # guild-wide: one inner-circle member is offered his real name at a time
MAX_EMOTES = 2  # the emote commands a line may carry out
ANSWERED_LINES = 256  # his lines whose answers are kept
TURNS_KEPT = 256  # the turns whose lines said are kept, for a turn that follows one (I7)
WORDS_KEPT = 4096  # said lines whose words before styling are kept for extraction
# Lines the typing never adds a mark to: a wipe's (rehearsal 2's "coros first to go again.
# great aww") and the raid calls'.
NO_MARK_KINDS = frozenset({TurnKind.WIPE_REACT, TurnKind.CALLOUT, TurnKind.URGENT})
EMOTE_AFTER_S = (0.5, 2.0)
# How often a woman's turn to the human carries her romance text, so fifteen lust women don't
# read one-note: by attitude, warmer late at night, cooler right after she last carried it.
ROMANCE_SHARE = {"fond": 0.35, "drawn": 0.55, "lust": 0.75}
# Also always: every reply on the whisper channel to him, and a public reply to a line of his
# that is itself a flirt (rehearsal 2, judge C 1: his flirty whisper missed the share's roll).
ROMANCE_ALWAYS = frozenset({TurnKind.FLIRT, TurnKind.WHISPER})
# A drawn woman's share when he is talking to her, from NO_BRAKES_DIAL (controller ruling).
DRAWN_TALKED_TO_SHARE = 0.75
ROMANCE_LATE_BONUS = 0.15
ROMANCE_COOL_S = 300.0
OWN_LINES = 5
DEAD_FORGET_S = 900.0  # a death with no resurrect event this old is forgotten
ECHO_WAIT_S = 30.0  # an echo this late still confirms a line whose ack timed out
SPOKEN_CHANNELS = frozenset({"say", "yell"})
# A line may still be said when its ack timed out or the connection dropped while it was due:
# its row waits for the echo (the bridge keeps events across a reconnect).
AWAIT_ECHO = frozenset({"ack_timeout", "connection_lost"})
_LATE = frozenset({DayPhase.LATE, DayPhase.DEAD})
_CAMEL = re.compile(r"(?<!^)(?=[A-Z])")


@dataclass(slots=True)
class TurnMeta:
    """What the engine knows about a turn beyond the Turn. answers_human: a reaction to the
    human himself (no chain check). whisper_reply: answers his whisper. rivalry: a rival's flirt
    (it feeds the men's asides but no further rivalry). loot_cheer: a woman cheering his loot.
    settled: the turn was cancelled with a line at the bridge, whose outcome is its row.
    said: how many of its lines are in chat (recorded). follow_up: the greeter who knows him
    best, asked to follow up on a memory with him when her prompt holds one. aside: the
    counted AsidePlan of an aside, given back to the hour if the aside is never said.
    offered_real_name, romance and memory_ids: what the live prompt offered (his real name,
    her romance text) and the memories it used; the real-name slot, her romance cooldown and
    the memories' rest are spent when the first line is said (correctness M6). facts_line:
    the id of his game question this reply answers; its prompt carries that line's facts
    (LibraryFacts.for_line; 0: none).

    why: how the turn came about, kept on its utterance row for the phases that learn from it
    (the cognition memo: it cannot be rebuilt later). Every value written:
    - reply:<how>, an answer to the human's line: named (he named them), whisper (he whispered
      them), pushback (his pushback that named nobody was aimed at them, the last to speak:
      engine._pushback), topic_fit (put in to answer his game question: facts.fit_tier),
      memory (put in to answer his question about the guild's past, which she remembers:
      engine._plan_lore), reason_pool (drawn first for a real reason to speak) or
      weighted_draw;
    - greet:<how> (his login) and gz:<how> (an achievement): reason_pool or weighted_draw;
    - gz_loot:weighted_draw (his loot), kill_gz:weighted_draw, wipe_react:weighted_draw or
      wipe_react:died_first (offered to whoever died first);
    - reply:raid_lead, the lead told he is leaving mid-raid;
    - kill:<how> and wipe:<how>: lead (the raid lead) or weighted_draw;
    - burst:<how>, social talk: an opener is weighted_draw, a follow-on reason_pool or
      weighted_draw;
    - aside:<trigger> (public_flirt or loot_cheer), rival:flirt and whisper:<trigger> (idle,
      late, evening or after_kill): the second part names what set it off, not a draw;
    - source:<TurnSource class>, say_as and speak: turns from outside the engine's planning."""
    turn_id: str
    topic: str = ""
    answers_human: bool = False
    whisper_reply: bool = False
    rivalry: bool = False
    loot_cheer: bool = False
    settled: bool = False
    why: str = ""
    said: int = 0
    follow_up: bool = False
    aside: object | None = None
    offered_real_name: bool = False
    romance: bool = False
    memory_ids: list[int] = field(default_factory=list)
    llm_s: float | None = None  # the model's time on this turn, its retry included
    facts_line: int = 0
    two_lines: bool = False  # a whisper to him at the explicit band: a line or two
    emote_target: int = 0  # whom an emote in the line is aimed at by default (a birthday)
    words: str = ""  # the line being sent, before styling (Pipeline.said_words)
    # his question about the guild's past (engine._plan_lore): KEEPER, THEIRS or NOBODY, and
    # the memory a keeper answers from, which her prompt always shows
    lore: str = ""
    lore_memory: int = 0
    # a reaction to a fight that has come before tonight (a second wipe on the boss): its
    # passive "again" is what happened (filters.history_claim)
    again_tonight: bool = False
    # his pushback this turn answers (fix wave G6, engine._pushback): its kind ("doubt" or
    # "anger"), on the turn of the member it is aimed at, whose answer is the first line the
    # model writes and passes their own reply interval; and the id of that member's own last
    # line on its channel, which the prompt's chat always shows (0: none this session)
    pushback: str = ""
    pushback_line: int = 0
    # voice comms: after, the id of the turn this one follows, which must have said a line
    # (I7); elsewhere, a turn in a room he is not in; guards, that talk's own Guards;
    # typed_on, the (channel, to_guid) it came on, kept when Comms.place moves it into a room:
    # where it goes when it can't be spoken (comms.typed)
    after: str = ""
    elsewhere: bool = False
    guards: object = None
    typed_on: tuple[str, int] | None = None


def _secs(s: float | None) -> str:
    return "-" if s is None else f"{s:.1f}"


def picked_why(cause: str, guid: int, reasons) -> str:
    """The why of a speaker drawn by pick_speakers(..., reasons): the pool or the draw."""
    return f"{cause}:{'reason_pool' if guid in (reasons or ()) else 'weighted_draw'}"


def _bit_id(turn: Turn) -> str | None:
    return getattr(turn.bit, "id", None)


@dataclass(slots=True)
class Composed:
    """close: the speaker is in his inner circle (prompt.knows_real_name), so his real name is
    hers to see and use. What the reply must not say back (filters.find_tells): answers, the
    line it answers; others, what other members already said to it; samples, the sample
    lines the prompt showed, never to be said word for word."""
    messages: list[dict]
    memory_ids: list[int]
    persona: Persona
    close: bool
    answers: list[str] = field(default_factory=list)
    others: list[str] = field(default_factory=list)
    samples: list[str] = field(default_factory=list)  # the sample lines the prompt showed
    # what may back a claim of shared history: the memories, cards and notes it showed
    support: list[str] = field(default_factory=list)
    # every other line of its chat window, from anyone: never to be copied
    window: list[str] = field(default_factory=list)
    # the memories and facts it showed: their words in his question are no echo
    held: list[str] = field(default_factory=list)


def aliases(p: Persona) -> tuple[str, ...]:
    """The speaker's real first name: the card introduces them by it ("You are Tarvo"), so the
    model may tag a line with it as with their character name."""
    first = (p.identity.real_name.split() or [""])[0]
    return (first,) if first and first.casefold() != p.character_name.casefold() else ()


def _names_list(names: list[str]) -> str:
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


def chain_base(channel: str, a: int, b: int, zone: int) -> str:
    """The conversation a line belongs to, keyed like chat threads."""
    if channel == "whisper":
        lo, hi = sorted((a, b))
        return f"whisper:{lo}:{hi}"
    if channel in ("guild", "officer") or channel.startswith("voice:"):
        return channel
    if channel in ("party", "raid", "raid_warning"):
        return "group"
    return f"say:{zone}"


_ASKS = re.compile(r"\?|^\W*(?:\w+\W+)?(?:who|what|when|where|why|how|did|do|does|is|are|"
                   r"was|were|can|could|will|would|have|has)\b", re.IGNORECASE)


def _asks(text: str) -> bool:
    """His line asks something: a question mark, or a question word up front."""
    return bool(_ASKS.search(text))


def _quest_line(text: str) -> bool:
    """His line is about a quest: select's quest talk, or the game question detector's quest
    topic."""
    if not text:
        return False
    q = game_question(text)
    return is_quest_talk(text) or (q is not None and q.topic == "quest")


def _with_hint(messages: list[dict], hint: str) -> list[dict]:
    out = [dict(m) for m in messages]
    out[-1]["content"] = f"{out[-1]['content']}\n{hint}"
    return out


class Pipeline:
    """The base of TalkEngine: one turn's life. TalkEngine.__init__ sets the collaborators
    declared here and calls _init_pipeline for the bookkeeping this layer owns."""

    store: Store
    world: WorldView
    raid: RaidState
    recall: Recall
    scheduler: LLMScheduler
    personas: PersonaCache
    cfg: MindConfig
    data_dir: Path
    clock: Clock
    rng: random.Random
    send: Callable[[Command], Awaitable[Ack]]
    on_line_sent: Callable[[int, Chat], object] | None
    guards: Guards
    human: HumanIdentity
    facts: LibraryFacts | None  # the library's facts, None without a library.db
    comms = None  # the voice extension's Comms; None: voice off, and nothing here changes

    def _init_pipeline(self, now: float) -> None:
        self.live_admission: Callable[[], Awaitable[None]] | None = None
        self._session_id: int | None = None
        self._altered: dict[int, Altered] = {}  # tonight's state per member
        self._dead: dict[int, float] = {}  # guid -> when they died
        # jobs and sending
        self._ids = itertools.count(1)
        self._tasks: set[asyncio.Task] = set()
        self._current: dict[asyncio.Task, Turn] = {}  # the turn each job is on
        self._cancelling: dict[asyncio.Task, Turn] = {}  # a job's one turn being cancelled
        self._delivering: set[asyncio.Task] = set()
        self._send_lock = asyncio.Lock()
        self._unacked: dict[tuple[int, str, str], tuple] = {}  # ack timed out, echo awaited
        # what has been said; own lines are (channel, to_guid, the words before styling)
        self._own: defaultdict[int, deque[tuple[str, int, str]]] = defaultdict(
            lambda: deque(maxlen=OWN_LINES))
        self._last_line_id: dict[int, int] = {}
        self._chains: dict[str, tuple[int, float | None]] = {}  # base -> (epoch, last line)
        self._romance_at: dict[int, float] = {}
        self._real_name_at = -math.inf  # when an inner-circle member last said a line offered it
        self._real_name_hold: str | None = None  # the turn composed with it, until it ends
        self._last_talk_at, self._quiet_fired = now, False
        # the prompts' chat window starts at the session (TalkEngine.start_session moves it)
        self._chat_from = now
        self._login_at = -math.inf  # when his login was last greeted (reactions.py)
        self._answered_at: dict[int, float] = {}  # a member's last line to the human
        self._greet_lines: set[int] = set()  # the chat lines greetings of his login said
        # his line -> the lines said as answers to it (_earlier_answers), the latest lines only
        self._answer_lines: dict[int, set[int]] = {}
        self._words: dict[int, str] = {}  # a said line's id -> its words before styling
        self._turn_lines: dict[str, int] = {}  # a turn id -> the lines it said
        self._compose_rng = random.Random("compose")  # compose() never draws from self.rng

    # -- identity and lookups ------------------------------------------------------------------

    def set_human(self, guid: int) -> None:
        """Who the human is: character name and rank from the stored roster, nickname, real
        name and pronouns from the config. Task 17 calls it from hello and login."""
        self.human = human_identity(self.cfg, self.store, guid)

    def _new_id(self) -> str:
        return f"t{next(self._ids)}"

    def _names(self) -> dict[int, str]:
        out = {g: m.name for g, m in self.world.members.items()}
        if self.human.guid:
            out[self.human.guid] = self.human.called
        return out

    def _protect(self) -> frozenset[str]:
        h = self.human
        return frozenset({m.name for m in self.world.members.values()}
                         | {x for x in (h.nickname, h.character_name, h.real_name) if x})

    def _zone_of(self, guid: int) -> int:
        m = self.world.members.get(guid)
        return m.zone if m is not None else 0

    def _card(self, p: Persona) -> str:
        """The card a prompt shows, from the live persona (personas.card.render_prompt_card),
        with race and class from the cast, or from the stored card for a member it lacks."""
        m = self.world.members.get(p.guid)
        if m is not None:
            race, class_name = RACES.get(m.race, ""), CLASSES.get(m.class_, "")
        else:
            race, class_name = card_race_class(self.store.get_card(p.guid) or "")
        return render_prompt_card(p, race=race, class_name=class_name)

    def _altered_of(self, guid: int) -> Altered:
        a = self._altered.get(guid)
        if a is None:
            try:
                a = Altered(self.store.get_state(guid).altered_state)
            except ValueError:
                a = Altered.SOBER
        return a

    def _effects(self, guid: int) -> Effects:
        return EFFECTS[self._altered_of(guid)]

    def _is_dead(self, guid: int, now: float) -> bool:
        died = self._dead.get(guid)
        return died is not None and now - died < DEAD_FORGET_S

    def _candidates(self, audience=None, *, exclude=(), hearable=True) -> list[int]:
        """Online cast members with a persona who are not AFK, in `audience` when given, and
        with voice on, unless `hearable` is off, only those he can hear where they sit."""
        w = self.world
        pool = w.online if audience is None else w.online & set(audience)
        out = [g for g in sorted(pool) if g not in w.afk and g not in exclude
               and self.personas.get(g) is not None]
        if hearable and self.comms is not None:
            out = [g for g in out if self._comms_says("hearable", True, g)]
        return out

    def _weigh(self, guids, *, last: int | None, ctx: SpeakerContext, reply: bool,
               fresh=frozenset()) -> tuple[dict[int, float], set[int]]:
        """Speaker weights, and the members with a real reason to speak (select.has_reason):
        `last` is whoever spoke last (the human, for a reply). When it is the human, his row
        counts only as lived and the members with an open thread with him are known
        (SpeakerContext.to_human, .thread). Members in `fresh` are weighed as if they had not
        spoken lately."""
        weights, reasons = {}, set()
        hg = self.human.guid
        if hg and last == hg:
            ctx = replace(ctx, to_human=True, thread=frozenset(
                g for g, at in self._answered_at.items() if ctx.now - at < OPEN_THREAD_S))
        for g in guids:
            p = self.personas.get(g)
            rel = self.store.get_relationship(g, last) if last else None
            rel_h = None
            if reply and hg:
                rel_h = rel if last == hg else self.store.get_relationship(g, hg)
            weights[g] = speaker_weight(p, self.store.get_state(g), rel, ctx,
                                        None if g in fresh else self.world.last_spoke.get(g),
                                        self._effects(g).talk_mult)
            if has_reason(p, rel, ctx, rel_to_human=rel_h, reply=reply):
                reasons.add(g)
        return weights, reasons

    def _context(self, now: float, topic: set[str] | None = None, *, opener: bool = False,
                 quest: bool = False) -> SpeakerContext:
        return SpeakerContext(now=now, topic=topic or set(), opener=opener,
                              phase=day_phase(self.clock.local()), quest=quest)

    # -- jobs ----------------------------------------------------------------------------------

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(self._job(coro))
        self._tasks.add(task)
        task.add_done_callback(self._job_done)
        return task

    def _job_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        self._cancelling.pop(task, None)

    async def _job(self, coro) -> None:
        try:
            await coro
        except Exception:  # a bug in one turn never stops the engine
            log.exception("talk_job_failed")

    async def _run(self, items: list[tuple[Turn, TurnMeta]]) -> None:
        """Speak turns one after another: each waits for the one before it to finish. A turn
        cancelled on its own (_cancel_refused) is dropped and the job goes on; the turns behind
        it are checked as usual. Any other cancel ends the job."""
        task = asyncio.current_task()
        for turn, meta in items:
            try:
                await self._speak(turn, meta)
            except asyncio.CancelledError:
                if self._cancelling.get(task) is not turn or task.cancelling() > 1:
                    raise
                del self._cancelling[task]
                task.uncancel()

    def _queue(self, items: list[tuple[Turn, TurnMeta]]) -> None:
        if items:
            self._spawn(self._run(items))

    def pending_turns(self) -> list[Turn]:
        """The turn each of the engine's running jobs is on (waiting, composing or typing)."""
        return [t for task, t in self._current.items() if task in self._tasks]

    def _cancel_refused(self, now: float) -> None:
        """A fight just started: cancel each pending turn the raid rules now refuse (that turn,
        not the rest of its job)."""
        for task in list(self._tasks):
            turn = self._current.get(task)
            if turn is not None and not self.raid.allows(turn.speaker, turn.kind, now):
                self._cancelling[task] = turn
                task.cancel()
                del self._current[task]  # not pending any more, even before it unwinds

    async def _cancel_all(self) -> None:
        """Cancel every job, then wait for the lines already handed to the bridge (bounded by
        its ack timeout) so they are recorded."""
        me = asyncio.current_task()
        tasks = [t for t in self._tasks if t is not me]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._delivering:
            await asyncio.gather(*self._delivering, return_exceptions=True)

    # -- composing -----------------------------------------------------------------------------

    async def compose(self, turn: Turn) -> tuple[list[dict], list[int]]:
        """The live prompt for `turn` and the ids of the memories it uses (P1c's evaluation and
        memory quiz). It changes no live state: no memory is marked accessed, no cooldown
        starts, and its random choices come from a generator of its own."""
        c = await self._compose(turn, TurnMeta(turn_id=""), live=False)
        return c.messages, c.memory_ids

    def _may_carry(self, turn: Turn, row: ChatLineRow) -> bool:
        """Whether a chat line may reach this turn's prompt (memory.recall.whisper_ok)."""
        return whisper_ok(row.channel, row.from_guid, row.to_guid, speaker=turn.speaker,
                          turn_channel=turn.channel, to_guid=turn.to_guid)

    def _own_of(self, guid: int) -> deque[tuple[str, int, str]]:
        """The member's own lines; the first time this run asks, the last ones she said before
        it, from the utterance log, so a restart keeps her "Your last lines" and the repeat
        check (prompts lens, minor 15)."""
        if guid not in self._own:
            self._own[guid].extend(self.store.recent_mind_lines(guid, OWN_LINES))
        return self._own[guid]

    def _own_lines(self, turn: Turn) -> list[str]:
        """The speaker's last lines, the words before styling, that this turn may show and
        check repeats against: a whisper of hers only in a whisper to the same person."""
        return [text for channel, to, text in self._own_of(turn.speaker)
                if whisper_ok(channel, turn.speaker, to, speaker=turn.speaker,
                              turn_channel=turn.channel, to_guid=turn.to_guid)]

    def _human_in_talk(self, turn: Turn, recent: list[ChatLineRow]) -> bool:
        hg = self.human.guid
        return bool(hg) and (turn.addressee == hg or any(r.from_guid == hg for r in recent[-3:]))

    def _romance(self, turn: Turn, meta: TurnMeta, p: Persona, rel_h, phase: DayPhase,
                 now: float, rng: random.Random, trigger: str = "") -> str | None:
        """Her romance text on this turn, or None: only on turns to the human; every FLIRT,
        WHISPER, loot cheer, reply on the whisper channel to him and reply to a flirty line of
        his (`trigger`); otherwise on a share of turns (ROMANCE_SHARE; a drawn woman's
        DRAWN_TALKED_TO_SHARE when he talks to her, from NO_BRAKES_DIAL), halved within
        ROMANCE_COOL_S of a line of hers that carried it. The dial is hers when he has set one
        for her (romance.dial_for); at her own FULL_DIAL it is every turn to him."""
        hg = self.human.guid
        if (not hg or turn.addressee != hg or p.identity.player_gender != "female"
                or p.romance.stance not in ROMANCE_SHARE):
            return None
        talked_to = turn.kind in (TurnKind.REPLY_HUMAN, TurnKind.GOODNIGHT) and bool(
            turn.trigger_line)
        always = (turn.kind in ROMANCE_ALWAYS or meta.loot_cheer
                  or (turn.channel == "whisper" and turn.to_guid == hg)
                  or (talked_to and flirty(trigger)) or own_dial(p) == FULL_DIAL)
        if not always:
            share = ROMANCE_SHARE[p.romance.stance]
            if (p.romance.stance == "drawn" and talked_to
                    and dial_for(p, self.cfg.romance.dial) >= NO_BRAKES_DIAL):
                share = DRAWN_TALKED_TO_SHARE
            if phase in _LATE:
                share += ROMANCE_LATE_BONUS
            if now - self._romance_at.get(p.guid, -math.inf) < ROMANCE_COOL_S:
                share /= 2
            if rng.random() >= share:
                return None
        return romance_instruction(p, rel_h, dial=self.cfg.romance.dial, channel=turn.channel,
                                   addressee_is_human=True, phase=phase, human=self.human,
                                   data_dir=self.data_dir)

    async def _compose(self, turn: Turn, meta: TurnMeta, *, live: bool) -> Composed:
        """`live`: the prompt is for speaking now, so it may draw from the engine's rng and
        hold the guild-wide real-name slot for the turn; what it offered and used goes on
        `meta`, to be spent if a line is said (_spend). A whisper reaches it only as
        whisper_ok allows: the chat window, the speaker's own last lines, the trigger line's
        words and recall all follow that rule.

        His real name reaches an inner-circle member's prompt only on the turn offered it
        (privacy minor 10): any other turn of theirs is redacted like a non-member's, so a
        name said in chat is not mirrored back until the gap allows it again.

        A reply to his game question (meta.facts_line) carries that line's facts from the
        library, under facts_head; they are reference text, never memories."""
        p = self.personas.get(turn.speaker)
        if p is None:
            raise ValueError(f"no persona for {turn.speaker}")
        rng = self.rng if live else self._compose_rng
        now = self.clock.now()
        human, hg = self.human, self.human.guid
        names = self._names()
        rel_h = self.store.get_relationship(turn.speaker, hg) if hg else None
        close = knows_real_name(rel_h)  # his inner circle: his real name is theirs to use
        recent = self.world.recent_visible(
            turn.speaker, 8,
            keep=lambda row: row.ts >= self._chat_from and self._may_carry(turn, row))
        quiet = not any(now - r.ts < QUIET_S for r in recent)
        if turn.kind in CHAINED:  # idle talk sees only the last few minutes (rehearsal 2)
            recent = [r for r in recent
                      if now - r.ts < IDLE_WINDOW_S or r.id == turn.trigger_line]
        if turn.kind is TurnKind.OPEN and hg:  # an opener never answers his line (rehearsal 3)
            recent = [r for r in recent if r.from_guid != hg]
        if meta.pushback_line and all(r.id != meta.pushback_line for r in recent):
            # her own line his pushback is about, older than the chat shown or one the bridge
            # reported, which only the log holds (fix wave G6)
            own = [r for r in self.store.chat_lines([meta.pushback_line])
                   if r.ts >= self._chat_from and self._may_carry(turn, r)]
            recent = sorted([*recent, *own], key=lambda r: r.id)

        people = [turn.addressee] if turn.addressee else []
        # the addressee and up to 2 others who spoke recently; a whisper is only between two
        room = len(people) + (0 if turn.channel == "whisper" else 2)
        for row in reversed(recent):
            g = row.from_guid
            if len(people) < room and g != turn.speaker and g not in people and g in names:
                people.append(g)
        attitude = p.romance.stance if p.identity.player_gender == "female" else ""
        cards = [relationship_card(names.get(g, "someone"),
                                   self.store.get_relationship(turn.speaker, g),
                                   is_human=g == hg,
                                   is_guild_master=g == hg and human.is_guild_master,
                                   forms=human.forms if g == hg else THEY_FORMS,
                                   attitude=attitude if g == hg else "",
                                   speaker=p.identity.player_gender)
                 for g in people]

        trigger = ""
        if turn.trigger_line:
            rows = self.store.chat_lines([turn.trigger_line])
            query = trigger = rows[0].text if rows and self._may_carry(turn, rows[0]) else ""
        elif turn.kind is TurnKind.OPEN and meta.topic:
            query = meta.topic  # what she will bring up, not what the chat said
        else:
            query = " ".join(r.text for r in recent[-3:])
        earlier = self._earlier_answers(turn, meta, recent)
        present = list(dict.fromkeys(
            [r.from_guid for r in recent if r.from_guid and r.from_guid != turn.speaker]
            + ([hg] if hg and self.world.human_online else [])))
        memories = await self.recall.recall(turn.speaker, query, present=present,
                                            addressee=turn.addressee, k=5,
                                            priority=turn.priority, touch=False,
                                            channel=turn.channel, to_guid=turn.to_guid)
        if meta.lore_memory and all(m.id != meta.lore_memory for m in memories):
            # the memory she answers his question from leads, even while it rests
            memories = [*self.store.get_memories([meta.lore_memory]), *memories][:5]

        state = self.store.get_state(turn.speaker)
        altered = self._altered_of(turn.speaker)
        local = self.clock.local()
        phase = day_phase(local)
        to_human = bool(hg) and turn.addressee == hg
        them = human.forms[1] if to_human else "them"
        greet = turn.kind is TurnKind.GREET
        # the greetings of his login a later greeter sees, as who said hi and never the words
        hi = ({r.id for r in recent if r.id in self._greet_lines and r.from_guid != turn.speaker}
              if greet else set())
        follow = ""
        if meta.follow_up and (m := self._follow_up_memory(memories)) is not None:
            follow = FOLLOW_UP.format(memory=self.recall.format(m).removeprefix("- "))
        before = list(dict.fromkeys(names[r.from_guid] for r in earlier if r.from_guid in names))
        lore = meta.lore if to_human else ""  # his question about the guild's past
        seen = ""
        if before and greet:
            seen = GREETED.format(names=_names_list(before[:2]), them=them)
        elif before and meta.facts_line:  # his game question: the first replier had the facts
            seen = ANSWERED_FACTS.format(names=_names_list(before[:2]))
        elif before and turn.kind in (TurnKind.REPLY_HUMAN, TurnKind.GOODNIGHT):
            seen = (ANSWERED_LORE if lore else ANSWERED).format(names=_names_list(before[:2]),
                                                                 them=them)
        quest = (quest_help(human) if turn.kind is TurnKind.REPLY_HUMAN and to_human
                 and quest_helper(p) and _quest_line(trigger) else "")
        story = self._their_story(turn, meta, trigger, memories) if to_human else ""
        ends = []
        if (to_human and turn.kind in (TurnKind.GREET, TurnKind.GOODNIGHT)
                and dial_for(p, self.cfg.romance.dial) >= NO_BRAKES_DIAL  # the ruling's dial
                and p.identity.player_gender == "female" and p.romance.stance in ROMANCE_SHARE):
            ends.append(FEELINGS.format(them=them))
        if greet and any(r.from_guid not in (hg, turn.speaker) for r in recent):
            ends.append(TALK_TO.format(name=human.called))
        romance = self._romance(turn, meta, p, rel_h, phase, now, rng, trigger)
        tail, two = "", False
        if romance:  # at the explicit band in a whisper: the owner's overlay, read last
            dial = self.cfg.romance.dial
            tail = whisper_tail(p, rel_h, dial=dial, channel=turn.channel,
                                data_dir=self.data_dir)
            # a line or two, unless the turn holds one (her answer to his pushback)
            two = turn.max_lines > 1 and explicit_whisper(p, rel_h, dial=dial,
                                                          channel=turn.channel)
        meta.two_lines = two
        real = ""
        if (close and self._human_in_talk(turn, recent) and self._real_name_hold is None
                and now - self._real_name_at >= REAL_NAME_GAP_S):
            real = real_name_line(human, rel_h, rng)
            if real and live:
                self._real_name_hold = meta.turn_id

        samples = select_samples(p)
        # the facts go to the first answer only: a second recited the same table (rehearsal 2)
        facts = (await self.facts.for_line(meta.facts_line)
                 if meta.facts_line and self.facts is not None and not earlier else [])
        pinned = next((i for i, r in enumerate(recent) if r.id == turn.trigger_line), None)
        kept = next((i for i, r in enumerate(recent) if r.id == meta.pushback_line), None
                    ) if meta.pushback_line else None

        instruction = render_instruction(
            turn.kind, addressee=names.get(turn.addressee, "") if turn.addressee else "",
            topic=meta.topic, lead=romance or "",
            extra=" ".join(x for x in (turn.instruction, follow, seen, story, lore, quest,
                                       *ends) if x),
            forms=human.forms if to_human else THEY_FORMS, tail=tail, long=two, quiet=quiet)
        parts = PromptParts(
            card=self._card(p), samples=samples,
            rules=comms_rules(human, p), rel_cards=cards,
            memories=[self.recall.format(m) for m in memories],
            mood=mood_line(state, altered, substance_of(p, altered, local), phase,
                           kind=turn.kind, life_thread=p.real_life.life_thread,
                           lately=self._lately_line(turn.speaker, now),
                           variant=random.Random(f"{p.guid}:{night_of(local)}:mood")
                           .randrange(1000), today=day_line(local, p.identity.region)),
            chat=[said_hi_text(r, names, human.called, now=now) if r.id in hi
                  else chat_line_text(r, names, now=now) for r in recent],
            own_recent=self._own_lines(turn), instruction=instruction,
            real_name=real, facts=list(facts),
            facts_head=facts_head(human.forms[1]) if facts else "",
            pinned=pinned if facts else None, kept=kept)
        trimmed = trim_to_budget(redact_for(parts, human, bool(real)))
        if live:
            meta.offered_real_name, meta.romance = bool(real), bool(romance)
            meta.memory_ids = [m.id for m in memories[:len(trimmed.memories)]]
        # a bit's own text is the owner's, an aside's what is happening now: either may back
        # the history it tells ("belra is flirting with zan again"); so do her own card and
        # life, the chat she was shown and the memory a greeting quotes (fix round 1, review
        # I5 and minor 11: "we've been married for 10 years" was her household). Not the line
        # she answers nor anything said after it, the other answers included: neither his
        # question's words nor another answer make a span she adds to them true (rehearsal
        # 2's "has been raging for 2 years"; fix round 2, re-review NB4)
        rl = p.real_life
        support = [*trimmed.memories, *trimmed.rel_cards, parts.card, rl.job, rl.rhythm,
                   rl.household, rl.money, rl.sleep, rl.life_thread,
                   *(r.text for r in recent
                     if not turn.trigger_line or r.id < turn.trigger_line),
                   *([follow] if follow else []),
                   *([turn.instruction] if turn.kind in (TurnKind.BIT, TurnKind.ASIDE)
                     else [])]
        return Composed(render_messages(trimmed),
                        [m.id for m in memories[:len(trimmed.memories)]], p, close,
                        [trigger] if trigger else [], [r.text for r in earlier], samples,
                        support, [r.text for r in recent if r.id != turn.trigger_line],
                        [*trimmed.memories, *trimmed.facts])

    def _their_story(self, turn: Turn, meta: TurnMeta, line: str, memories) -> str:
        """For a member answering his public question to other members (it opens on a
        member, not her), when none of her memories shares its words: it's their story to tell
        (rehearsal 2, judge B 2: a third answerer with no memory of the joke gave it "2 years"
        and a winner). Never in a whisper, on a line that asks nothing ("gz arkon on the
        drake!") or on his game question, which has its facts (fix round 1, review minor
        1)."""
        if (turn.kind is not TurnKind.REPLY_HUMAN or not line or meta.facts_line
                or turn.channel == "whisper" or not _asks(line)):
            return ""
        members = {g: m.name for g, m in self.world.members.items() if g != self.human.guid}
        named = mentioned(line, members)
        if (not named or turn.speaker in named
                or line_addressee(line, list(members.values()), ends=False) is None):
            return ""
        if supported(line, [m.text for m in memories]):
            return ""
        who = _names_list([members[g] for g in named[:2]])
        return THEIR_STORY.format(name=self.human.called, names=who)

    def _follow_up_memory(self, memories):
        """The one memory a greeting may follow up on: something he said or did in an earlier
        session (a chat, extract or game memory with him as an actor, from before this
        session started), the best recalled; never the owner's seeded canon. None: a plain
        hello."""
        hg = self.human.guid
        return next((m for m in memories if hg and m.source in FOLLOW_SOURCES
                     and hg in m.actors and m.t_event < self._chat_from), None)

    def _earlier_answers(self, turn: Turn, meta: TurnMeta,
                         recent: list[ChatLineRow]) -> list[ChatLineRow]:
        """Other members' answers in the window to the line this answer to the human answers
        (for a greeting, the greetings of his login): what the guild already said to it. Only
        lines sent as answers to that very line count (fix round 1, review I3: a burst line
        after his game question took the facts from his first answer), so a guild line is no
        answer to his whisper (rehearsal 2, judge B 8: "Dunna already answered him")."""
        if not meta.answers_human:
            return []
        hg = self.human.guid

        def other(r: ChatLineRow) -> bool:
            return r.from_guid not in (hg, turn.speaker) and r.from_guid in self.world.members
        if turn.kind is TurnKind.GREET:
            return [r for r in recent if r.id in self._greet_lines and r.ts >= self._login_at
                    and other(r)]
        answers = self._answer_lines.get(turn.trigger_line or 0, ())
        return [r for r in recent if r.id in answers and other(r)]

    # -- speaking ------------------------------------------------------------------------------

    async def speak(self, turn: Turn, *, why: str = "speak", elsewhere: bool = False,
                    guards: Guards | None = None, after: str = "") -> list[str]:
        """Speak one turn (after turn.delay) through the whole pipeline: the lines sent, or []
        when it was dropped (the reason is in the utterance log). The caller's Turn is left as
        it was: the pipeline fills in its own copy's priority, deadline and channel. The
        keywords go on its TurnMeta."""
        return await self._speak(replace(turn), TurnMeta(
            self._new_id(), why=why, elsewhere=elsewhere, guards=guards, after=after))

    async def say_as(self, guid: int, channel: str, text: str, *, kind: TurnKind,
                     to_guid: int = 0) -> bool:
        """The template path (P1d): guards, raid rules and the member's typing apply, the text
        is used as given and no model is asked. True when a line really went out, to the game
        or to the voice service: one left in a room he is not in did not."""
        turn = Turn(kind=TurnKind(kind), speaker=guid, channel=channel, to_guid=to_guid,
                    template=text)
        meta = TurnMeta(self._new_id(), why="say_as")
        said = await self._speak(turn, meta)
        return bool(said) and not (self.comms is not None and self._comms_says(
            "unheard", False, turn.channel, meta))

    def _rule_priority(self, turn: Turn, meta: TurnMeta) -> Priority:
        if meta.elsewhere:
            return Priority.BACKGROUND
        if turn.kind in REPLY_KINDS or meta.whisper_reply:
            return Priority.REPLY_HUMAN
        g = self.world.group
        if g is not None and g.raid and turn.speaker == self.raid.lead():
            return Priority.RAID_LEAD
        return Priority.SOCIAL

    async def _speak(self, turn: Turn, meta: TurnMeta) -> list[str]:
        task = asyncio.current_task()
        self._current[task] = turn
        try:
            return await self._speak_now(turn, meta)
        except asyncio.CancelledError:
            if not meta.settled:  # else the line at the bridge already has its row
                self._drop(turn, meta, "cancelled")
            raise
        finally:
            if self._current.get(task) is turn:
                del self._current[task]
            if self._real_name_hold == meta.turn_id:  # ended without its line: free the slot
                self._real_name_hold = None

    async def _speak_now(self, turn: Turn, meta: TurnMeta) -> list[str]:
        await self.clock.sleep(turn.delay)
        now = self.clock.now()
        rule = self._rule_priority(turn, meta)
        turn.priority = rule if meta.elsewhere else min(turn.priority, rule)
        if turn.deadline <= 0:
            turn.deadline = now + DEADLINE_S.get(turn.priority, OTHER_DEADLINE_S)
        if not turn.channel:
            turn.channel = choose_channel(turn.kind, speaker=turn.speaker, world=self.world,
                                          raid=self.raid, now=now, rng=self.rng) or ""
            if not turn.channel:
                return self._drop(turn, meta, "no_channel")
        p = self.personas.get(turn.speaker)
        if p is None:
            return self._drop(turn, meta, "no_persona")
        voiced = self._route(turn, meta)
        if self._follows_silence(meta):
            return self._drop(turn, meta, "after_dropped")
        if voiced:
            await self._hold(turn)
        why = self._blocked(turn, meta, self.clock.now(), first=True)
        if why:
            return self._drop(turn, meta, why)
        memory_ids: list[int] = []
        text = turn.template
        if text is None:
            try:
                c = await self._compose(turn, meta, live=True)
            except ValueError as e:  # a turn the instruction can't be written for
                log.warning("talk_bad_turn: guid=%d kind=%s: %s", turn.speaker, turn.kind, e)
                return self._drop(turn, meta, "bad_turn")
            memory_ids, close = c.memory_ids, c.close
            text, why = await self._generate(turn, meta, c, spoken=voiced)
            if why:
                return self._drop(turn, meta, why, memory_ids, text=text)
            if voiced:  # the model takes time: the service may be gone, the speaker moved on
                voiced = self._route(turn, meta)
        else:
            hg = self.human.guid
            close = bool(hg) and knows_real_name(self.store.get_relationship(turn.speaker, hg))
        if turn.channel == "whisper":  # an emote is public: a whisper keeps its words (NB6)
            text, emotes = emote_words(text), []
        else:
            text, emotes = split_emotes(text, {m.name.casefold(): g
                                               for g, m in self.world.members.items()})
        # nothing but commands the game would print as text ("/invite arkon"): nothing to say
        only = command_only(text)
        text = self._spell_real_name(text, close=close, offered=meta.offered_real_name)
        if voiced and text.strip() and not only:
            spoken = await self._speak_voice(turn, meta, p, text, memory_ids)
            if spoken is not None:
                if spoken:
                    await self._send_emotes(turn, meta, emotes)
                return spoken
            turn.channel, turn.to_guid, turn.addressee = typed(turn, meta)  # it never got there
        lines = [] if only else self._style(turn, p, text, close=close,
                                            two_lines=meta.two_lines, romance=meta.romance)
        if not lines:
            if emotes:  # a line that was only an emote: the emote goes out (review minor 8)
                await self._send_emotes(turn, meta, emotes)
            return self._drop(turn, meta, "emote_only" if emotes else "command" if only
                              else "empty", memory_ids, text=text)
        delay_mult = effects_for(self._altered_of(turn.speaker), turn.kind).delay_mult
        sent: list[Styled] = []
        try:
            for i, s in enumerate(lines):
                typing = typing_delay(s.line, p.typing.chars_per_sec, delay_mult, self.rng)
                await self.clock.sleep(self._first_typing(turn, typing) if i == 0 else typing)
                meta.words = s.plain
                why = await self._send_line(turn, meta, p, s.line, memory_ids, first=i == 0)
                if why:
                    if why not in AWAIT_ECHO:  # that row waits for the echo (_settle_unacked)
                        self._drop(turn, meta, why, memory_ids, text=s.line)
                    break
                sent.append(s)
        finally:
            # what was said, before typos and abbreviations: the next prompts and repeats, a
            # line that went out as the turn was cancelled included
            said = lines[:meta.said]
            if said and turn.template is None:
                self._own_of(turn.speaker).append(
                    (turn.channel, turn.to_guid, " ".join(s.plain for s in said)))
        if sent:  # a cancelled turn's follow-ups never start: a fight began or the session ends
            await self._send_emotes(turn, meta, emotes)
            self._after_sent(turn, meta, p, " ".join(s.line for s in sent))
        return [s.line for s in sent]

    def _comms_says(self, question: str, default, *args):
        """Comms' answer, or `default`, phase 1's, when the voice side fails: a fault there
        reaches neither the talk loop nor the log's text, only its name does."""
        try:
            return getattr(self.comms, question)(*args)
        except Exception as e:
            log.warning("voice_failed: call=%s reason=%s", question, type(e).__name__)
            return default

    def _route(self, turn: Turn, meta: TurnMeta) -> bool:
        """Where the turn goes now its channel is known (Comms.place, which also fills in
        whom it is to), and the one decision on whether it is spoken, for the model's turns
        and templates alike (C1). Voice off, down or failing, it is typed as in phase 1."""
        if self.comms is None:
            return False
        if meta.typed_on is None and not turn.channel.startswith("voice:"):
            meta.typed_on = turn.channel, turn.to_guid
        try:
            turn.channel, turn.to_guid, turn.addressee = self.comms.place(turn, meta)
            return bool(self.comms.voiced(turn, meta))
        except Exception as e:
            log.warning("voice_failed: call=route reason=%s", type(e).__name__)
            turn.channel, turn.to_guid, turn.addressee = typed(turn, meta)
            return False

    def _follows_silence(self, meta: TurnMeta) -> bool:
        """The turn this one follows said nothing, so it has nothing to follow (I7). Only with
        voice up: off or down, typed chat goes on as in phase 1."""
        return (bool(meta.after) and self.comms is not None
                and self._comms_says("available", False)
                and not self._turn_lines.get(meta.after))

    async def _hold(self, turn: Turn) -> None:
        """While he is talking (Comms.hold); a fault there holds nothing."""
        try:
            await self.comms.hold(turn, turn.channel)
        except Exception as e:
            log.warning("voice_failed: call=hold reason=%s", type(e).__name__)

    async def _speak_voice(self, turn: Turn, meta: TurnMeta, p: Persona, text: str,
                           memory_ids: list[int]) -> list[str] | None:
        """A voiced turn's one line (settled item 8): the model's words or the template's, with
        no typing, handed to the service and recorded. None: it never reached the service, so
        nothing happened and the caller types it. The service gets it first because a line in
        the log is a line said; there is no await between the two. `text` goes to Comms.deliver
        and to the log as two arguments: what he hears may come to differ from what is kept."""
        text = " ".join(text.split())
        async with self._send_lock:
            why = self._blocked(turn, meta, self.clock.now(), first=True, deadline=False)
            if why:
                return self._drop(turn, meta, why, memory_ids, text=text)
            if not self._comms_says("deliver", False, turn, meta, text):
                return None
            meta.words = text
            try:
                line_id = self._record(turn, meta, p, text, memory_ids)
            except Exception:
                log.exception("say_record_failed: guid=%d channel=%s", turn.speaker, turn.channel)
                return self._drop(turn, meta, "record_error", memory_ids, text=text)
            self._comms_says("log_line", None, turn, meta, line_id)
        if turn.template is None:
            self._own_of(turn.speaker).append((turn.channel, turn.to_guid, text))
        self._after_sent(turn, meta, p, text)
        return [text]

    async def _send_emotes(self, turn: Turn, meta: TurnMeta,
                           emotes: list[tuple[str, int]]) -> None:
        """The emotes her line typed as commands, after it, once each (MAX_EMOTES), at the
        member named after the command or the turn's own target. A romantic one (kiss, wink,
        cuddle, blush) only ever goes to him or to nobody, and the turn's own target takes
        only a friendly one (fix round 1, review I2). A whisper sends none: an emote is
        public. A refused one is logged."""
        if turn.channel == "whisper" or meta.elsewhere or meta.why == "hotmic":
            return
        hg = self.human.guid
        for emote, target in emotes[:MAX_EMOTES]:
            romantic = emote in ROMANCE_EMOTES
            target = target or (0 if romantic else meta.emote_target)
            if romantic and target != hg:
                target = 0
            await self.clock.sleep(self.rng.uniform(*EMOTE_AFTER_S))
            try:
                ack = await self.send(Emote(id="", guid=turn.speaker, emote=emote,
                                            target_guid=target))
            except BridgeError as e:
                log.info("emote_dropped: guid=%d reason=%s", turn.speaker, type(e).__name__)
                continue
            if not ack.ok:
                log.info("emote_dropped: guid=%d reason=%s", turn.speaker, ack.error)

    def _first_typing(self, turn: Turn, typing: float) -> float:
        """The first line's typing, capped so it goes out within the turn's deadline: typed
        faster, never dropped, when the model answered in time (rehearsal 2, finding 5: three
        lines the model gave in half a second were lost to typing alone)."""
        left = turn.deadline - self.clock.now() - SEND_MARGIN_S
        return min(typing, max(MIN_TYPING_S, left)) if turn.deadline > 0 else typing

    def _blocked(self, turn: Turn, meta: TurnMeta, now: float, *, first: bool,
                 deadline: bool = True) -> str | None:
        """Why the turn can't speak now, or None. `deadline`: the turn's deadline counts
        (after its delay); a line the model gave in time is never expired after that."""
        g, w = turn.speaker, self.world
        if meta.elsewhere and not self._comms_says(
                "scene_valid", False, turn, meta.why.startswith("elsewhere:")):
            return "elsewhere_changed"
        if meta.why == "hotmic" and not self._comms_says("hotmic_valid", False, turn):
            return "hotmic_changed"
        if g not in w.online:
            return "offline"
        if g in w.afk:
            return "afk"
        if turn.channel in SPOKEN_CHANNELS and self._is_dead(g, now):
            return "dead"
        if turn.channel == "whisper" and not meta.whisper_reply and not w.human_online:
            return "human_offline"
        if turn.kind is TurnKind.FLIRT:
            if not self.human.guid or turn.addressee != self.human.guid:
                return "flirt_target"  # flirting is only ever aimed at him (content invariant)
            p = self.personas.get(g)
            if p is None or flirt_chance(p, 1.0) <= 0:
                return "flirt_stance"  # and only from a woman who wants him
        if first and deadline and self.scheduler.expired(turn.deadline):
            return "expired"
        if not self.raid.allows(g, turn.kind, now):
            return "raid"
        guards = meta.guards or self.guards
        if not guards.allow(g, turn.kind, now, continuation=not first,
                            reply=meta.whisper_reply, pushback=bool(meta.pushback)):
            return "guards"
        if turn.kind in CHAINED and not meta.answers_human and not guards.chain_open(
                self._chain_key(self._turn_base(turn), now)):
            return "chain"
        return None

    def _turn_base(self, turn: Turn) -> str:
        return chain_base(turn.channel, turn.speaker, turn.to_guid, self._zone_of(turn.speaker))

    def _chain_epoch(self, base: str, now: float) -> int:
        """Which conversation on `base` a line now belongs to: after CHAIN_QUIET_S of quiet, a
        new one. Read only: the epoch moves on when the next line is recorded."""
        epoch, last = self._chains.get(base, (0, None))
        return epoch + 1 if last is not None and now - last >= CHAIN_QUIET_S else epoch

    def _chain_key(self, base: str, now: float) -> str:
        return f"{base}#{self._chain_epoch(base, now)}"

    def _chain_line(self, base: str, now: float, *, from_human: bool, guards=None) -> None:
        epoch = self._chain_epoch(base, now)
        (guards or self.guards).chain_line(f"{base}#{epoch}", from_human)
        self._chains[base] = (epoch, now)

    def _touch_talk(self, now: float) -> None:
        """Chat happened: a quiet stretch (the idle whisper trigger) starts over."""
        self._last_talk_at = now
        self._quiet_fired = False

    async def _ask(self, turn: Turn, messages: list[dict]) -> str:
        # a bit of several rows keeps the rows a blank line separates (prompts lens, minor 9)
        preset = LIVE_BIT if turn.kind is TurnKind.BIT and turn.max_lines > 1 else LIVE

        async def job(llm):
            if self.live_admission is not None:
                await self.live_admission()
            if self.scheduler.expired(turn.deadline):
                raise Expired(turn.deadline)
            return await llm.chat(self.cfg.models.live, messages, options=dict(preset.options),
                                  think=preset.think, timeout=preset.timeout)
        result = await self.scheduler.submit(turn.priority, job, deadline=turn.deadline)
        return result.text

    async def _generate(self, turn: Turn, meta: TurnMeta, c: Composed, *,
                        spoken: bool = False) -> tuple[str, str | None]:
        """The words to style, or a drop reason with the rejected text. What the model copied
        from the prompt's chat frame goes first (strip_copied), and "(nothing)" is looked for
        again after it and after every repair. A fatal or soft tell (an echo of the line it
        answers, a reply far over the speaker's length included) asks once more, with a hint
        that says what was wrong; the retry is taken unless a tell is still fatal. Silence is
        an answer, but in answer to his whisper it is asked once more (_answers_his_whisper).
        A speaker outside his inner circle has his real name taken out of the words before
        anything else, while it is still capitalised. A `spoken` line is asked for as speech
        (SPOKEN_HINT, M6), last of all, so a retry's hint never pushes it up."""
        p = c.persona
        name, own, his = p.character_name, aliases(p), self._his_names()

        def asking(hint: str = "") -> list[dict]:
            out = _with_hint(c.messages, hint) if hint else c.messages
            return _with_hint(out, SPOKEN_HINT) if spoken else out
        messages, text = asking(), ""
        for attempt in (0, 1):
            asked = self.clock.now()
            try:
                raw = await self._ask(turn, messages)
            except Expired:
                return text, "expired"
            except (SchedulerError, LLMError) as e:
                log.warning("talk_llm_failed: guid=%d kind=%s: %s", turn.speaker, turn.kind, e)
                return text, "llm_error"
            finally:
                meta.llm_s = (meta.llm_s or 0.0) + self.clock.now() - asked
            if self.scheduler.expired(turn.deadline):
                return raw, "expired"
            silent, tells = is_silence(raw), []
            if not silent:
                text = raw if c.close else redact_real_name(raw, self.human, self.human.called)
                text = strip_copied(text, (name, *own), him=his)
                if meta.pushback:  # the answer to his pushback is one line: the model's first
                    text = text.split("\n", 1)[0].rstrip()
                silent = is_silence(text)  # a line that was only a command at him included
            if not silent:
                tells = find_tells(text, recent_own=self._own_lines(turn), speaker=name,
                                   answers=c.answers, others=c.others, samples=c.samples,
                                   support=c.support, people=list(self._protect()),
                                   window=c.window, held=c.held, tonight=meta.again_tonight)
                if len(text.split()) > LONG_SLACK * self._word_budget(turn, p, meta.two_lines):
                    tells.append(Tell("long", False, True))
                if self._flirts_at_a_member(turn, text):
                    tells.append(Tell("member flirt", True))
                if not any(t.fatal or (t.soft and (attempt == 0 or t.reason in DROP_ON_RETRY))
                           for t in tells):
                    text = repair(text, speaker=name, aliases=own, him=his)
                    if not is_silence(text):
                        return text, None
                    silent = True
            if silent:
                if attempt == 0 and self._answers_his_whisper(turn):
                    log.info("say_silent: guid=%d kind=%s asked again", turn.speaker, turn.kind)
                    messages = asking(SILENT_WHISPER_HINT.format(
                        human=self.human.called,
                        length="a line or two" if meta.two_lines else "a line"))
                    continue
                return raw, "silent"
            log.info("say_tells: guid=%d kind=%s attempt=%d tells=%s", turn.speaker, turn.kind,
                     attempt, ",".join(t.reason for t in tells))
            messages = asking(self._retry_hint(tells, p, turn))
        return text, "tells"

    def _answers_his_whisper(self, turn: Turn) -> bool:
        """The turn answers a line of his in a whisper to him: its silence is asked again once
        (SILENT_WHISPER_HINT); anywhere else silence is an answer."""
        hg = self.human.guid
        return (bool(hg) and turn.channel == "whisper" and turn.to_guid == hg
                and turn.kind in REPLY_KINDS and bool(turn.trigger_line))

    def _flirts_at_a_member(self, turn: Turn, text: str) -> bool:
        """The output screen for the content invariant (prompts I10), on every turn, his
        included (rehearsal 2, judge B 5: turns to him were never screened): romance, desire
        or innuendo aimed at a guildmate (filters.member_flirt, the re-review's rule: on his
        turns a rival may be mentioned, and only desire spoken to a member is caught). A
        FLIRT turn's line that opens on a member and says nothing of him is no flirt of his
        either (filters.aimed_off_him: "happy birthday dunna! if i was there i'd make you a
        cake"). Either is asked again with a hint, then dropped, and so sets off no aside."""
        hg = self.human.guid
        members = [m.name for g, m in self.world.members.items() if g != hg]
        his = self._his_names()
        p = self.personas.get(turn.speaker)
        if member_flirt(text, members, to_member=self._to_member(turn), human=his,
                        to_him=self._aimed_at_him(turn),
                        winks=p is not None and p.identity.player_gender == "female"):
            return True
        return turn.kind is TurnKind.FLIRT and aimed_off_him(text, members, his)

    def _his_names(self) -> list[str]:
        h = self.human
        return [x for x in (h.called, h.character_name, h.nickname) if x]

    def _to_member(self, turn: Turn) -> bool:
        hg = self.human.guid
        return (turn.addressee is not None and turn.addressee != hg
                and turn.addressee in self.world.members)

    def _aimed_at_him(self, turn: Turn) -> bool:
        """The turn is his: addressed to him, a whisper to him, or a FLIRT (only ever at him)."""
        hg = self.human.guid
        return bool(hg) and (turn.addressee == hg or turn.kind is TurnKind.FLIRT
                             or (turn.channel == "whisper" and turn.to_guid == hg))

    def _retry_hint(self, tells: list[Tell], p: Persona, turn: Turn | None = None) -> str:
        reasons = {t.reason for t in tells if t.fatal or t.soft}
        hints = [REGEN_HINT.format(name=p.character_name)] if reasons - _HINTED else []
        if "member flirt" in reasons:  # a turn of his is told the line is his
            them = self.human.forms[1]
            hints.append(AT_HIM_HINT.format(human=self.human.called, them=them)
                         if turn is not None and self._aimed_at_him(turn)
                         else FRIENDS_HINT.format(human=self.human.called))
        if "echo" in reasons:
            his = (turn is not None and turn.kind in REPLY_KINDS and bool(turn.trigger_line)
                   and bool(self.human.guid) and turn.addressee == self.human.guid)
            hints.append(ECHO_HIM_HINT.format(their=self.human.forms[2]) if his else ECHO_HINT)
        if "copy" in reasons:
            hints.append(COPY_HINT)
        if reasons & {"repeat", "near repeat"}:
            hints.append(REPEAT_HINT)
        if "sample" in reasons:
            hints.append(SAMPLE_HINT)
        if "history" in reasons:
            hints.append(HISTORY_HINT)
        if "slang" in reasons:
            hints.append(SLANG_HINT)
        if "long" in reasons:
            lo, hi = p.typing.words_per_line
            hints.append(LONG_HINT.format(lo=lo, hi=hi))
        return " ".join(hints)

    def _word_budget(self, turn: Turn, p: Persona, two_lines: bool = False) -> int:
        """How many words this turn's lines hold for the speaker now."""
        cap = line_cap(p.typing, effects_for(self._altered_of(turn.speaker), turn.kind),
                       PHASE_MODS[day_phase(self.clock.local())].length_mult)
        lines = (turn.max_lines if turn.kind is TurnKind.BIT or two_lines
                 else turn_lines(p.typing, turn.max_lines))
        return cap * max(1, lines)

    def _spell_real_name(self, text: str, *, close: bool, offered: bool) -> str:
        """The guard every line passes before it is stored or sent, voiced or typed: on the
        turn offered his real name, every use by a member of his inner circle is the name,
        however the model typed it, so the stored line shows it in a form redaction catches
        (it leaves the all-lower-case word alone, as the ordinary word it may also be)."""
        if close and offered:
            for spelt in self._real_name_spelling():
                text = re.sub(rf"(?<!\w){re.escape(spelt)}(?!\w)", spelt, text,
                              flags=re.IGNORECASE)
        return text

    def _style(self, turn: Turn, p: Persona, text: str, *, close: bool,
               two_lines: bool = False, romance: bool = False) -> list[Styled]:
        """The member's typing over the words: at most turn.max_lines lines (style_lines). A
        bit of several lines keeps the lines the model wrote, one each; any other turn is
        styled as one text. A member of his inner circle keeps his real name capitalised
        through it where it was written so (_real_name_spelling)."""
        phase = day_phase(self.clock.local())
        keep = self._real_name_spelling() if close else frozenset()
        pieces = ([x for x in text.split("\n") if x.strip()]
                  if turn.kind is TurnKind.BIT and turn.max_lines > 1 else [text])
        out: list[Styled] = []
        for piece in pieces:
            out += style_lines(piece, p.typing, self._effects(turn.speaker), self.rng,
                               length_mult=PHASE_MODS[phase].length_mult, protect=self._protect(),
                               speaker=p.character_name, kind=turn.kind, keep_case=keep,
                               max_lines=turn.max_lines if len(pieces) == 1 else 1,
                               aliases=aliases(p), force_lines=two_lines,
                               no_emoticon=romance or turn.kind in NO_MARK_KINDS,
                               wink_ok=not (self._to_member(turn) or names_any(
                                   piece, [m.name for g, m in self.world.members.items()
                                           if g != self.human.guid])))
        return out[:max(1, turn.max_lines)]

    def _real_name_spelling(self) -> frozenset[str]:
        """His real name spelt as a name, in a form the redaction catches: as configured when
        that starts with a capital, else its Title form ("o'neil" -> "O'Neil", "mary jane" ->
        "Mary Jane"; Task 16 re-review). An inner-circle member's lower-case line keeps it.
        Only them: anyone else's lower-case word is the ordinary word the name may also be,
        and capitalising it would put the name in their mouth."""
        name = self.human.real_name
        if not name:
            return frozenset()
        return frozenset({name if name[:1].isupper() else name.title()})

    # -- sending -------------------------------------------------------------------------------

    async def _send_line(self, turn: Turn, meta: TurnMeta, p: Persona, text: str,
                         memory_ids: list[int], *, first: bool) -> str | None:
        """Check once more and send under the lock, so two turns never both pass a full window.
        The send itself is shielded: once the server may have the line, it is recorded. A turn
        cancelled meanwhile keeps the lock until the line has settled, and that line's outcome
        is the turn's row."""
        async with self._send_lock:
            why = self._blocked(turn, meta, self.clock.now(), first=first, deadline=False)
            if why:
                return why
            inner = asyncio.ensure_future(self._deliver(turn, meta, p, text, memory_ids))
            self._delivering.add(inner)
            inner.add_done_callback(self._delivering.discard)
            try:
                return await asyncio.shield(inner)
            except asyncio.CancelledError:
                while not inner.done():
                    try:
                        await asyncio.wait({inner})
                    except asyncio.CancelledError:
                        pass  # cancelled again: still raised below, once the line settles
                why = inner.result()
                if why and why not in AWAIT_ECHO:
                    self._drop(turn, meta, why, memory_ids, text=text)
                meta.settled = True
                raise

    async def _deliver(self, turn: Turn, meta: TurnMeta, p: Persona, text: str,
                       memory_ids: list[int]) -> str | None:
        """Send one line and record it on the ack: None, or why it wasn't said. Never raises,
        since nobody may be waiting for it any more. A voice channel never reaches the bridge
        (C1): it would refuse it, and the line belongs in a room."""
        if turn.channel.startswith("voice:"):
            log.error("say_refused_voice_channel: guid=%d channel=%s", turn.speaker, turn.channel)
            return "voice_channel"
        try:
            ack = await self.send(Say(id="", guid=turn.speaker, channel=turn.channel, text=text,
                                      to_guid=turn.to_guid))
        except (AckTimeout, ConnectionLost) as e:
            # The bridge may still have said it: its echo can confirm it (is_echo).
            why = _CAMEL.sub("_", type(e).__name__).lower()
            self._unacked[(turn.speaker, turn.channel, text)] = (
                self.clock.now(), turn, meta, p, list(memory_ids), why)
            return why
        except BridgeError as e:
            return _CAMEL.sub("_", type(e).__name__).lower()
        except Exception:  # a command the protocol refuses: a bug, never the bridge's state
            log.exception("say_send_failed: guid=%d channel=%s", turn.speaker, turn.channel)
            return "send_error"
        if not ack.ok:
            if ack.error == "offline":
                self.world.mark_offline(turn.speaker, self.clock.now())
            return ack.error or "refused"
        try:
            self._record(turn, meta, p, text, memory_ids)
        except Exception:
            log.exception("say_record_failed: guid=%d channel=%s", turn.speaker, turn.channel)
            return "record_error"
        return None

    def _record(self, turn: Turn, meta: TurnMeta, p: Persona, text: str,
                memory_ids: list[int]) -> int:
        """A line was said: the chat log (origin "mind", with who could see it), the world, the
        thread hook, the guards, the chain and the utterance log (with why and the bit). Its
        line id. A line he had no way of hearing (Comms.unheard: I8, N3) moves neither the
        quiet clock nor the open thread with him; every typed line still does."""
        now = self.clock.now()
        heard = self.comms is None or not self._comms_says("unheard", False, turn.channel, meta)
        m = self.world.members.get(turn.speaker)
        zone, map_id = (m.zone, m.map) if m is not None else (0, 0)
        sid = self._session_id
        chat = Chat(ts=int(now * 1000), channel=turn.channel, from_guid=turn.speaker,
                    from_name=p.character_name, to_guid=turn.to_guid, text=text, map=map_id,
                    zone=zone)
        audience = self.world.audience(turn.channel, from_guid=turn.speaker,
                                       to_guid=turn.to_guid, zone=zone)
        line_id = self.store.insert_chat_line(chat, session_id=sid, origin="mind",
                                              audience=audience)
        self.world.add_line(ChatLineRow(
            id=line_id, ts=now, channel=turn.channel, from_guid=turn.speaker,
            from_name=p.character_name, to_guid=turn.to_guid, text=text, map=map_id, zone=zone,
            session_id=sid, thread_id=None, origin="mind"), now)
        self._last_line_id[turn.speaker] = line_id
        if meta.words:
            self._words[line_id] = meta.words
            while len(self._words) > WORDS_KEPT:
                del self._words[next(iter(self._words))]
        if turn.kind is TurnKind.GREET:
            self._greet_lines.add(line_id)
        if meta.answers_human and turn.trigger_line:
            self._answer_lines.setdefault(turn.trigger_line, set()).add(line_id)
            while len(self._answer_lines) > ANSWERED_LINES:
                del self._answer_lines[next(iter(self._answer_lines))]
        hg = self.human.guid
        if heard and hg and (meta.answers_human or turn.addressee == hg or turn.to_guid == hg):
            self._answered_at[turn.speaker] = now  # a thread with him is open
        (meta.guards or self.guards).record(meta.turn_id, turn.speaker, now, turn.kind)
        meta.said += 1
        self._turn_lines[meta.turn_id] = meta.said
        while len(self._turn_lines) > TURNS_KEPT:
            del self._turn_lines[next(iter(self._turn_lines))]
        if meta.said == 1:
            self._spend(turn, meta, now)
            self._first_line_said(turn, meta)
        self._chain_line(chain_base(turn.channel, turn.speaker, turn.to_guid, zone), now,
                         from_human=False, guards=meta.guards)
        if heard and turn.channel != "whisper":
            self._touch_talk(now)
        self.store.log_utterance(now, turn.speaker, turn.channel, turn.kind.value, text,
                                 memory_ids, None, sid, bit=_bit_id(turn), why=meta.why or None,
                                 trigger_line=turn.trigger_line, llm_s=meta.llm_s,
                                 to_guid=turn.to_guid, turn_id=meta.turn_id)
        log.info("say_sent: guid=%d kind=%s channel=%s line=%d trigger=%s why=%s bit=%r "
                 "llm_s=%s", turn.speaker, turn.kind, turn.channel, line_id,
                 turn.trigger_line or "-", meta.why or "-", _bit_id(turn), _secs(meta.llm_s))
        if self.on_line_sent is not None:
            try:
                self.on_line_sent(line_id, chat)
            except Exception:
                log.exception("on_line_sent hook failed")
        return line_id

    def said_words(self, line_id: int) -> str | None:
        """The words before styling (Styled.plain: no typos, abbreviations or emoticon) of a
        line the mind said in this run, for extraction's copy rule (rehearsal 3, check 8: a
        typo in the line as sent hid a memory that said it back). None for any other line."""
        return self._words.get(line_id)

    def _spend(self, turn: Turn, meta: TurnMeta, now: float) -> None:
        """The turn's first line is said: what its prompt offered is used up (correctness M6).
        The guild-wide real-name gap starts, her romance cooldown starts and the memories it
        brought up rest (recall.REST_S)."""
        if meta.offered_real_name:
            self._real_name_at = now
            if self._real_name_hold == meta.turn_id:
                self._real_name_hold = None
        if meta.romance:
            self._romance_at[turn.speaker] = now
        if meta.memory_ids:
            self.store.touch_memories(meta.memory_ids, now)

    def _drop(self, turn: Turn, meta: TurnMeta, reason: str, memory_ids=(), *, text: str = "",
              at: float | None = None) -> list[str]:
        self.store.log_utterance(self.clock.now() if at is None else at, turn.speaker,
                                 turn.channel or "", turn.kind.value, text or "",
                                 list(memory_ids), reason, self._session_id,
                                 bit=_bit_id(turn), why=meta.why or None,
                                 trigger_line=turn.trigger_line, llm_s=meta.llm_s,
                                 to_guid=turn.to_guid, turn_id=meta.turn_id)
        log.info("say_dropped: guid=%d kind=%s channel=%s reason=%s trigger=%s why=%s "
                 "llm_s=%s", turn.speaker, turn.kind, turn.channel, reason,
                 turn.trigger_line or "-", meta.why or "-", _secs(meta.llm_s))
        if meta.said == 0:
            self._turn_dropped(turn, meta)
        return []

    def _turn_dropped(self, turn: Turn, meta: TurnMeta) -> None:
        """A turn ended with none of its lines said (reactions.py gives an aside's place in
        the hour back)."""

    def _settle_unacked(self, now: float, *, everything: bool = False) -> None:
        """Lines waiting for an echo that never came (within ECHO_WAIT_S, or at all when
        `everything`) get their row now: dropped, "ack_timeout" or "connection_lost". It runs
        from is_echo (the app's dispatcher) and tick, so a failing write is logged, not
        raised."""
        for key, (at, turn, meta, _, memory_ids, why) in list(self._unacked.items()):
            if everything or now - at > ECHO_WAIT_S:
                del self._unacked[key]
                try:
                    self._drop(turn, meta, why, memory_ids, text=key[2], at=at)
                except Exception:
                    log.exception("say_drop_failed: guid=%d channel=%s", turn.speaker,
                                  turn.channel)

    def is_echo(self, ev: Chat, now: float) -> bool:
        """The bridge's report of a line a `say` produced (via_bridge). The mind stored its own
        line on the ack; an echo of a line whose ack timed out records it now, and that is the
        line's only row."""
        if not ev.via_bridge:
            return False
        self._settle_unacked(now)
        late = self._unacked.pop((ev.from_guid, ev.channel, ev.text), None)
        if late is None:
            log.debug("say_confirmed: guid=%d channel=%s", ev.from_guid, ev.channel)
            return True
        _, turn, meta, p, memory_ids, _ = late
        try:
            self._record(turn, meta, p, ev.text, memory_ids)
        except Exception:  # the app's dispatcher must never see it
            log.exception("say_record_failed: guid=%d channel=%s", ev.from_guid, ev.channel)
        else:
            log.info("say_confirmed_by_echo: guid=%d channel=%s", ev.from_guid, ev.channel)
        return True

    def _lately_line(self, guid: int, now: float) -> str:
        """What the member did tonight, for an opener's "Right now:" (reactions.py keeps it)."""
        return ""

    def _first_line_said(self, turn: Turn, meta: TurnMeta) -> None:
        """A turn's first line is in chat, even if the turn is cancelled before the rest
        (reactions.py counts a bit as used here)."""

    def _after_sent(self, turn: Turn, meta: TurnMeta, p: Persona, said: str = "") -> None:
        """Follow-ups once a turn has been said, `said` being its lines as sent (reactions.py
        adds them)."""
