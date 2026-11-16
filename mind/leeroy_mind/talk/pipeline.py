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
above, unchanged: a line that can't be spoken is typed, and a fault there is logged by name.

A turn on the guild's Discord (discord.transport: a `discord:<id>` or `dm:<guid>` channel) is
composed and typed as any other, his dots included, but it never reaches the game: it is never
placed in a room or voiced, sends no emote, keeps its emoji and its letters (no ASCII fold, no
emoji tell), and its up to three typed lines become one message, stored as a discord_message row
(transport.store_line) where a typed line would go to the bridge as a Say. The line is recorded
as said like any other, under its Discord channel, while the game's own chat state (the world's
history, the quiet clock, the say chains, a member's last line) never sees it. The game's rules
for whispers to him and for /say (he must be online, a dead member can't talk) and for the
speaker's body (online in the game) are for those channels only; every other guard holds, but
an answer to his DM, like one to his whisper, is never held back by the guild's pace and its
first silence is asked again. A Discord turn's deadline is DISCORD_DEADLINE_S, an emoji-only line
is a message there, not silence, and with no trigger line its recall looks for
TurnMeta.recall_query (his post)."""
import asyncio
import itertools
import logging
import math
import random
import re
import secrets
import sqlite3
import zlib
from collections import defaultdict, deque
from contextlib import asynccontextmanager, nullcontext
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Awaitable, Callable
from leeroy_mind.talk.realism import Realism

from leeroy_mind.arc.live import AWAY_CACHE_S, ArcView, arc_view, away_now
from leeroy_mind.bridge import AckTimeout, BridgeError, ConnectionLost
from leeroy_mind.clock import Clock, night_of
from leeroy_mind.config import MindConfig
from leeroy_mind.discord.channels import is_private, same_medium, is_group
from leeroy_mind.discord.presence import local_time
from leeroy_mind.discord.store import DiscordRefused
from leeroy_mind.discord.text import MAX_TEXT as DISCORD_MAX_TEXT
from leeroy_mind.discord.transport import LINES as DISCORD_LINES
from leeroy_mind.discord.transport import DM_PREFIX, DiscordTarget, is_discord, store_line
from leeroy_mind.human import THEY_FORMS, HumanIdentity, human_identity
from leeroy_mind.life import state as life_state
from leeroy_mind.life.day import has_kids
from leeroy_mind.life.presence import where_text
from leeroy_mind.life.seed import seeded
from leeroy_mind.llm.client import LLMError, GpuPaused
from leeroy_mind.llm.presets import BEAT, LIVE, LIVE_BIT
from leeroy_mind.llm.scheduler import Expired, LLMScheduler, Priority, SchedulerError
from leeroy_mind.memory.recall import Recall, whisper_ok
from leeroy_mind.nightly.settings import night_bounds
from leeroy_mind.personas.card import card_race_class, render_prompt_card
from leeroy_mind.personas.facts import pronoun_of
from leeroy_mind.personas.io import PersonaCache
from leeroy_mind.personas.schema import Persona
from leeroy_mind.personas.screens import redact_real_name
from leeroy_mind.photos.offer import PhotoOffer, caption_ok
from leeroy_mind.protocol import Ack, Chat, Command, Emote, Say
from leeroy_mind.storage.rows import ChatLineRow
from leeroy_mind.storage.store import Store
from leeroy_mind.talk import beat as beats
from leeroy_mind.talk.budget import PHASE_MODS, DayPhase, day_phase
from leeroy_mind.talk.channels import choose_channel
from leeroy_mind.talk.discord_length import (FEW_WORDS, QUICK_WORDS, SHORT_WORDS,
                                             TYPING_CAP_S as DISCORD_TYPING_S,
                                             band, fit_words, opener_draw, preset_for,
                                             trim_to_sentence,
                                             words_for)
from leeroy_mind.talk.filters import (Tell, aimed_off_him, find_tells, flirty, is_silence,
                                      line_addressee, member_flirt, names_any, repair,
                                      strip_copied, supported)
from leeroy_mind.talk.guards import Guards
from leeroy_mind.talk.intent import (INTENT_KINDS, Intent, IntentContext, allowed, draw_intent,
                                     ordered, topic_for)
from leeroy_mind.guards.review import HARD_CODES, fresh_hint, rewrite_hint
from leeroy_mind.guards.phrases import CODES as PHRASE_CODES, hint as phrase_hint, strip as strip_phrases
from leeroy_mind.talk.facts import LibraryFacts
from leeroy_mind.talk.gamenames import misplaced_battleground, squashed, unknown_bosses
from leeroy_mind.talk.items import defuse, fold
from leeroy_mind.talk.movelog import MoveLog, MoveRow, PairNotes
from leeroy_mind.talk.asking import asks, is_question
from leeroy_mind.talk.moves import MOVE_PHRASES, classify, is_summon, moves_line, question_first
from leeroy_mind.talk.owed import (HIS_TALK_WAIT_S, KEEP_WHISPERS, RESCUABLE as OWED_RESCUABLE,
                                   answers_in_flight, extend_owed_deadline, his_talk_after,
                                   owes_him, rescue_text)
from leeroy_mind.talk.prompt import (DM_BUDGET, LIVE_BUDGET, PromptParts, chat_line_text,
                                     comms_rules, facts_head, knows_real_name, mood_line,
                                     quest_help, real_name_line, redact_for, relationship_card,
                                     render_instruction, render_messages, replay,
                                     said_hi_text, select_samples, situation_length,
                                     situation_rules, trim_to_budget)
from leeroy_mind.talk.questions import game_question
from leeroy_mind.talk.raid import RaidState
from leeroy_mind.talk.romance import (FULL_DIAL, NO_BRAKES_DIAL, dial_for, dm_explicit,
                                      explicit_whisper, flirt_chance, forward, his_heat,
                                      his_moves, own_dial, romance_instruction, scene_notes,
                                      whisper_tail)
from leeroy_mind.talk.select import (OPEN_THREAD_S, SpeakerContext, has_reason, is_quest_talk,
                                     mentioned, quest_helper, speaker_weight, typing_delay)
from leeroy_mind.talk.states import (EFFECTS, RAID_CALLS, Altered, Effects, effects_for,
                                     substance_of)
from leeroy_mind.talk.seasons import day_line
from leeroy_mind.talk.style import (ROMANCE_EMOTES, Styled, command_only, emote_words,
                                    line_cap, split_emotes, style_lines, turn_lines)
from leeroy_mind.talk.turns import Turn, TurnKind
from leeroy_mind.talk.timing import TurnTiming
from leeroy_mind.talk.words import COMMON_WORDS
from leeroy_mind.talk.world import WorldView
from leeroy_mind.text import EMOJI_RE, estimate_tokens, strip_wow_markup
from leeroy_mind.voice.comms import SPOKEN_HINT, typed
from leeroy_mind.wow import CAST_MIN_LEVEL, CLASSES, RACES

log = logging.getLogger("leeroy_mind.talk")

# Answers to the human. With a reply to his whisper they get Priority.REPLY_HUMAN.
REPLY_KINDS = frozenset({TurnKind.REPLY_HUMAN, TurnKind.GREET, TurnKind.GOODNIGHT})
DEADLINE_S = {Priority.REPLY_HUMAN: 30.0}
# An answer to him in the guild's Discord (discord.talk): his post may have to wait for the GPU
# the night holds, and nobody is standing in the game for it (ruling R9).
DISCORD_DEADLINE_S = 120.0
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
# One mind per member: a private turn between her and him also shows their lines from the other
# medium (her DMs in a whisper, their whispers in a DM), at most PAIR_LINES from PAIR_WINDOW_S
PAIR_LINES = 6
PAIR_WINDOW_S = 86400.0
# A channel quiet this long starts a new conversation, as a thread does. Only the lines the
# engine sees (the human's and the mind's) keep a conversation open; other players' lines and
# his bot commands, which threads do count, don't, so this approximates the thread rule.
CHAIN_QUIET_S = 90.0
# The retry's hint says what was wrong with the first answer (prompts lens, minor 14).
REGEN_HINT = "Say it the way {name} would type it in game chat: short, not like a helper."
# The same for a longer Discord message ([talk] discord_words), which is never told "short"
DISCORD_REGEN_HINT = "Say it the way {name} would write it on Discord, not like a helper."
ECHO_HINT = "Don't say their line back: answer it in your own words."
# Answering his question (rehearsal 2, judge B 2: the echo retry broke a member's right answer).
ECHO_HIM_HINT = ("Don't say {their} line back: the same answer in your own words, and keep "
                 "what your memories say.")
COPY_HINT = "Don't copy a line from the chat: say your own thing."
REPEAT_HINT = "Don't repeat what you said before."
LONG_HINT = "Shorter: {lo}-{hi} words, the way you type."
DISCORD_LONG_HINT = "Shorter: at most about {words} words."
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
# The same for a turn that starts a conversation with him (TurnMeta.initiates, 2026-11-07: 5
# of 8 unprompted whispers were lost to a first "(nothing)").
SILENT_START_HINT = "Start the conversation with {human} in {length}, in your own voice."
# And for her answer to his Discord DM (ruling R9).
SILENT_DM_HINT = "Answer {human}'s message in {length}, in your own voice."
DISCORD_SILENT_LENGTH = "a few sentences"  # a longer Discord message's {length} above
DISCORD_SILENT_SHORT = {SHORT_WORDS: "a few words", QUICK_WORDS: "a sentence",
                        FEW_WORDS: "a couple of sentences"}  # by band
# A move she just made with this person, made again (filters.move_repeat: "move repeat:
# <move>"; "clause", her final clause again, takes REPEAT_HINT): the phrase carries the
# pronoun (moves.MOVE_PHRASES). A summons out of the game (filters "summons").
MOVE_REPEAT = "move repeat: "
MOVE_HINT = "You just {move}: say something new instead."
SUMMONS_HINT = ("You two only talk online, in the game and on Discord: don't ask {them} over or "
                "say you're waiting; answer what {they} said.")
# A boss or battleground that does not exist, or a battleground put in a zone (talk/gamenames.py)
GAME_NAME_HINT = ("Name only bosses and battlegrounds that really exist and that you were in: a "
                  "battleground is a map of its own, never somewhere in a zone. Say \"the boss\" "
                  "if you're not sure of its name.")
# A Discord message that names its own speaker talks about her as someone else ("mejith being
# a corporate strategist now", posted by that member, 2026-11-15 audit): asked again once.
SELF_NAME_HINT = 'You are {name}: talk about yourself in the first person, never by your own name.'
_HINTED = frozenset({"echo", "repeat", "near repeat", "long", "member flirt", "sample",
                     "history", "copy", "slang", "summons", "game name", "self name",
                     "phrase: resting"})
# Soft tells a retry may not keep: a claim of shared history still there is dropped, and so is
# a summons, a boss or battleground that does not exist, and a message still naming its own
# speaker.
DROP_ON_RETRY = frozenset({"history", "summons", "game name", "self name"})
# A reply this far over the words its lines hold is asked again shorter (prompts C1).
LONG_SLACK = 1.25
# An answer he is owed when no draft can stand (owner, 2026-11-15: every message of his gets a
# reply that adds something; Pipeline.owed_fallback): one more short generation, told only this,
# reviewed only for OWED_HARD_TELLS.
OWED_FALLBACK_HINT = ("Answer what {human} just said in one or two short sentences, in your own "
                      "voice.")
OWED_HARD_TELLS = frozenset({"meta", "member flirt", "echo", "summons", "dialogue", "list",
                             "action", "sample"})
# A refused fallback is asked once more with what was wrong, in words (review-325-r2 P3-C).
OWED_REFUSED = {
    "meta": "don't talk about being an AI, a prompt or instructions",
    "member flirt": "keep it to {human}: no flirting with anyone else",
    "echo": "don't say {their} words back",
    "summons": "don't ask {them} over, you two only talk online",
    "dialogue": "write only your own words, no one else's lines",
    "list": "no list, just talk",
    "action": "no *actions*, only what you'd type",
    "sample": "don't copy your example lines",
    "silent": "say something back, not (nothing)",
}
OWED_RETRY_HINT = "Not like that: {reasons}. " + OWED_FALLBACK_HINT
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
SHORT_ANSWER_WORDS = 3  # an owed answer cut short of the sentences that slipped, at least
ANSWERED_LINES = 256  # his lines whose answers are kept
# A common stock phrase an answer owed to him leaned on is no rewrite then: it is named on her
# next turn instead (guards/phrases.py Found.pending), within this long
PHRASE_PENDING_S = 12 * 3600.0
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
WANTS_KEPT = 256  # her last want per conversation (_draw_situation's `last`), the latest kept
DEAD_FORGET_S = 900.0  # a death with no resurrect event this old is forgotten
ECHO_WAIT_S = 30.0  # an echo this late still confirms a line whose ack timed out
SPOKEN_CHANNELS = frozenset({"say", "yell"})
# A line may still be said when its ack timed out or the connection dropped while it was due:
# its row waits for the echo (the bridge keeps events across a reconnect).
AWAIT_ECHO = frozenset({"ack_timeout", "connection_lost"})
# A photo a turn cannot carry after all; an answer owed to him goes on without it (_photo_owed).
PHOTO_LOST = frozenset({"photo_words", "photo_stale"})
DISCORD_REPLY_WHY = "discord:reply"  # discord.talk.WHY, which imports this module
_LATE = frozenset({DayPhase.LATE, DayPhase.DEAD})
# Who is typing (the console's Comms dots, Pipeline.typing_now): a model turn is composing from
# its compose call until its text exists, and typing over the typing delay before each line is
# sent. Only where Comms shows the line: guild and officer, his group's party and raid, and a
# whisper to him (typed or voiced); a voice room, say and yell, and templates show nothing.
# Recording: a member's DM line being rendered as a voice note (voice_notes), shown as such.
COMPOSING, TYPING, RECORDING = "composing", "typing", "recording"
COMMS_CHANNELS = frozenset({"guild", "officer"})
GROUP_CHANNELS = frozenset({"party", "raid", "raid_warning"})
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
    - reply:carried, her answer to his whisper in her Discord DM, carried there at his logout
      while it was still waiting ([life] availability, LifeService.on_logout);
    - kill:<how> and wipe:<how>: lead (the raid lead) or weighted_draw;
    - burst:<how>, social talk: an opener is weighted_draw, a follow-on reason_pool or
      weighted_draw;
    - burst:to_him, an opener addressed to him on its own clock (engine._open_to_him);
    - aside:<trigger> (public_flirt or loot_cheer), rival:flirt and whisper:<trigger> (idle,
      late, evening or after_kill, or start: the WhisperPlanner's own clock): the second part
      names what set it off, not a draw;
    - emote:<his emote> (her line after his emote at her) and emote_join:<his emote> (one who
      joined his social emote; talk/emotes.py): the second part is his emote;
    - source:<TurnSource class>, say_as and speak: turns from outside the engine's planning."""
    turn_id: str
    topic: str = ""
    answers_human: bool = False
    whisper_reply: bool = False
    first_question_answer: bool = False
    rivalry: bool = False
    loot_cheer: bool = False
    settled: bool = False
    why: str = ""
    said: int = 0
    realism_correction: bool = False
    realism_followup: bool = False
    realism_correction_until: float = 0
    realism_sequence: int = 0
    realism_planned: bool = False
    realism_review_lines: tuple[str, ...] = ()
    realism_own: tuple[str, int, str] | None = None
    follow_up: bool = False
    aside: object | None = None
    offered_real_name: bool = False
    romance: bool = False
    memory_ids: list[int] = field(default_factory=list)
    llm_s: float | None = None  # the model's time on this turn, its retry included
    recipe_snapshot: object | None = None  # optional numeric/identifier attribution at composition
    recipe_line_id: int | None = None  # first acknowledged fragment of this generation
    learning: object | None = field(default=None, repr=False)  # scoped style for this generation
    lab: object | None = field(default=None, repr=False)  # lab/live.py: the turn's piece draw
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
    timing_trace: str | None = None  # opaque scalar; empty means telemetry admission skipped
    timing_origin: float | None = None  # on_human_chat's local monotonic entry, when observed
    # Exact event behind a lead's debrief/kill; absent for unrelated turns.
    fight_map: int = 0
    fight_boss: int = 0
    fight_instance: int = 0
    fight_end_ms: int = 0
    # An optional owner fence; omitted by ordinary engine callers.
    still_valid: Callable[[], bool] | None = None
    # The retry hint naming the phrases a model line says that rest (a greeting the guild used
    # lately, talk/greetings.py), "" when none does: a fatal tell, asked again once with that
    # hint, then dropped. None: nothing rests.
    phrase_rests: Callable[[str], str] | None = None
    # Optional DB-only receipt writer. Runs with the Discord post and chat mirror in
    # one transaction; it must not mutate memory-only delivery state before commit.
    on_discord_record: Callable[[int, int, float], None] | None = None
    photo_request: int | None = None
    photo_request_part: str = 'initial'
    borrow_credit: object | None = None
    outreach_item: int | None = None
    # initiates: the turn starts a conversation with him unprompted (whisper:<trigger>,
    # burst:to_him): its silence is asked again once (SILENT_START_HINT), and a chain never
    # stops it, since its own clock paces it. casual: an opener to him in party or guild,
    # which never carries her romance (that goes by whisper).
    initiates: bool = False
    casual: bool = False
    # Optional exact transport hooks of a say_as sender; ordinary turns leave both None, and
    # then nothing below changes. before_transport() runs synchronously right before the
    # line's Say is written and may refuse it (a turn with one is typed, one line, never
    # voiced: the voice path has no barrier; and sent as given, with none of the member's
    # typing style, so what the sender vouched for is what goes out). on_recorded(line_id,
    # ack) runs once the acked line is stored.
    before_transport: Callable[[], bool] | None = None
    on_recorded: Callable[[int, Ack], None] | None = None
    # the console's tuning preview (Pipeline.preview_line): the Ollama seed both of its
    # requests carry, so before and after differ by the dial and not the sampler
    seed: int | None = None
    # a Discord turn's message: the one it replies to and its thread (discord.transport); a
    # Discord turn is known by its channel, never by this
    voice_note: object = None
    discord: DiscordTarget | None = None
    photo: PhotoOffer | None = None
    photo_asked: bool = False
    photo_seed: int | None = None
    photo_finished: bool = False
    single_line_before_photo: bool = False  # restored when an owed answer goes on without it
    discord_named: bool = False  # he addressed this member in a channel: re-ask one silence
    # what recall looks for when the turn has no trigger line: for an answer to his Discord post,
    # its words and its thread's last lines (discord.talk, ruling R13); for a turn carrying an
    # outreach item, its words (outreach.service); "" for every other turn
    recall_query: str = ""
    # an answer to his Discord DM: the DM's history as the chat she answers in (discord.talk
    # dm_chat, rows keyed by Discord message ids); None for every other turn
    discord_chat: tuple[ChatLineRow, ...] | None = None
    guard_verdict: str = "not_run"
    guard_rewritten: bool = False
    preference_original: str = field(default='',repr=False)
    preference_generation: str = ''
    guard_codes: tuple[str,...] = ()
    review_required: bool = False  # opt-in sources require their card and deterministic guards
    single_line: bool = False  # a bounded multi-member conversation owns its line count
    # a server reply drawn to go out as a quick burst of 2-3 messages (realism.plan splits it)
    burst: bool = False
    # the Discord messages this turn stored, in order, once each is committed (_deliver_discord)
    discord_messages: list[int] = field(default_factory=list)
    keep_channel: bool = False  # a public thread must stay on its chosen transport
    server_anchor: str = ''
    server_thread: tuple[int, ...] = ()
    server_conversation: str = ''
    godmode_intent: int | None = None  # the owner's intent this turn carries out (godmode)
    # the answer he is owed for his line or post (talk/owed.py): the guild's pace never holds it
    # back, its review keeps a draft, and a lost draft is rescued; owed_rescue marks the watchdog's
    # rescue turn; owed_rescued names the drop reason a late draft or fallback answered (one
    # rescue a turn); owed_cap is the deadline's hard ceiling, set at its first extension
    owed_answer: bool = False
    owed_rescue: bool = False
    owed_rescued: str = ""
    owed_cap: float = 0.0
    # when the turn was queued (Pipeline._queue): an initiation he spoke after is dropped while
    # an answer to him is in flight, one planned before his line waits (his_talk_waited)
    planned_at: float = 0.0
    his_talk_waited: bool = False
    # the beat's lines for this answer (talk/beat.py): None not asked yet, "" skipped; never
    # logged or stored
    beat: str | None = field(default=None, repr=False)
    # the beat's conversation ledger key (beat.ledger_key; "" before a beat was asked) and the
    # key of what it brought in ("m<id>"/"c<id>", None for nothing): her said turn is recorded
    # there (_spend), so the conversation's next beats follow the bring-in rules
    beat_conversation: str = ""
    beat_use: str | None = None
    # the common stock phrases the reviewed draft leaned on while owed to him (no rewrite: her
    # next turn is told), and those this turn's prompt was told about (phrase_hint_pending)
    phrase_pending: tuple[str, ...] = field(default=(), repr=False)
    phrase_hint: tuple[str, ...] = field(default=(), repr=False)
    # [bakeoff] capture: the model calls this turn made since its last recorded line (the sent
    # words pick theirs), and the row the generation's first line wrote (bakeoff/capture.py);
    # empty and None while capture is off
    bakeoff_requests: list = field(default_factory=list, repr=False)
    bakeoff_row: int | None = None
    # [life] availability above 0 (life/service.py): wake, set by Pipeline.hurry, ends the
    # turn's delay at once (None: a plain delay, today's); wake_at, when its delay ends (0 before
    # it begins); heard_at, his line's time; life_presence, her LifePresence when it was planned
    # (a Discord turn's, a carried DM's included, as she starts composing: _draw_situation)
    wake: asyncio.Event | None = field(default=None, repr=False)
    wake_at: float = 0.0
    heard_at: float = 0.0
    life_presence: object | None = None
    # core plan Task 8 (Pipeline._draw_situation, as she starts composing): intent, her want this
    # turn ([talk] intent; an Intent code, "" for none) and want_source where it came from ("draw":
    # the code's draw, "beat": the beat chose it); state, her feeling then ([life] state; a
    # life.state.State, None while off); lands, how his line lands on her by it ("" for none);
    # intent_ctx, the IntentContext the want was drawn from (the beat's wants, the want's topic).
    # Only the want's code is ever logged
    intent: str = ""
    want_source: str = ""
    state: object | None = None
    lands: str = ""
    intent_ctx: object | None = field(default=None, repr=False)


class _ScopeChanged(SchedulerError):
    """The owner invalidated a turn before its next side effect."""


class TurnWithdrawn(Exception):
    """An instruction hook's word that its turn has nothing left to answer (discord.talk: his
    post was deleted): the turn is dropped with `reason` before any model call."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _scope_valid(meta: TurnMeta | None) -> bool:
    if meta is None or meta.still_valid is None:
        return True
    try:
        return bool(meta.still_valid())
    except Exception:
        return False


def _transport_admitted(meta: TurnMeta) -> bool:
    """The sender's final barrier, when it gave one; a failing barrier refuses."""
    if meta.before_transport is None:
        return True
    try:
        return meta.before_transport() is True
    except Exception:
        log.warning("say_barrier_failed")
        return False


def _emoji_only(text: str) -> bool:
    """Emoji and nothing else that speaks: no letter or digit."""
    return EMOJI_RE.search(text) is not None and not any(c.isalnum() for c in text)


def _names_self(text: str, name: str, *, labels: tuple[str, ...] = ()) -> bool:
    """The model's words name their own speaker (`name`, her character name, as a whole word,
    as written or in capitals; "@Name" too): she talks about herself as someone else. Never for
    a name that is an everyday word (words.COMMON_WORDS: "Dawn" opening a sentence is the word),
    nor inside one of `labels`, the guild's running jokes and slang that carry her name ("the
    Belra bridge"). A lower-case word is left alone, as the ordinary word a name may also be."""
    if not name or name.casefold() in COMMON_WORDS:
        return False
    for label in labels:
        text = re.sub(rf"(?<!\w){re.escape(label)}(?!\w)", " ", text, flags=re.IGNORECASE)
    return any(re.search(rf"(?<!\w)@?{re.escape(form)}(?!\w)", text)
               for form in {name, name.upper()})


def _secs(s: float | None) -> str:
    return "-" if s is None else f"{s:.1f}"


def picked_why(cause: str, guid: int, reasons) -> str:
    """The why of a speaker drawn by pick_speakers(..., reasons): the pool or the draw."""
    return f"{cause}:{'reason_pool' if guid in (reasons or ()) else 'weighted_draw'}"


def _bit_id(turn: Turn) -> str | None:
    return getattr(turn.bit, "id", None)


@dataclass(slots=True)
class Composed:
    """close: the speaker is in his inner circle (prompt.knows_real_name) and talks with him in
    private (Pipeline._real_name_private), so his real name is hers to see and use; in public
    her words are redacted like anyone's. What the reply must not say back
    (filters.find_tells): answers, the
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
    # her last moves with this turn's counterpart, as its Lately line read them: the repeat
    # check uses the same rows
    moves: list[MoveRow] = field(default_factory=list)
    # the memories and chat lines it showed, as the beat may offer them (beat.keyed_items), and
    # beside each its key ("m<id>" a memory, "c<id>" a chat line; "" none)
    beat_items: list[str] = field(default_factory=list)
    # his explicit whisper overlay is after its instruction (romance.whisper_tail): the beat
    # then offers none of the wants it rules out (beat.wants_for's `tail`)
    tail: bool = False
    beat_keys: list[str] = field(default_factory=list)
    # the turn answers his line and it asks her something (asking.asks): the beat's answering
    # wants lead (beat.wants_for's `asking`)
    asking: bool = False


def _pending_of(verdicts) -> tuple[str, ...]:
    """The common stock phrases the reviewed lines leaned on while owed to him (Verdict.pending),
    once each."""
    return tuple(dict.fromkeys(x for v in verdicts for x in getattr(v, "pending", ())))


def _hinted(composed: "Composed", hint: str) -> "Composed":
    """The composed prompt with `hint` at the end of its instruction (the last user message),
    and no draft as an assistant turn."""
    msgs = list(composed.messages)
    if msgs and msgs[-1].get("role") == "user":
        msgs[-1] = {**msgs[-1], "content": msgs[-1]["content"].rstrip() + "\n\n" + hint}
    else:
        msgs.append({"role": "user", "content": hint})
    return replace(composed, messages=msgs)


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
    if channel in ("guild", "officer") or channel.startswith("voice:") or is_discord(channel):
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
    # what a turn's delay and its typing are waited out at: 1 everywhere but the sandbox mind's
    # --fast (sandbox/README.md), whose 0 sends each line as soon as it is made
    wait_scale: float = 1.0
    # when the members away were read, and who they were (_away_now)
    _away_cache: tuple[float, frozenset[int]] = (-math.inf, frozenset())

    def _init_pipeline(self, now: float) -> None:
        self.timing = TurnTiming()
        self.realism = Realism()
        self.live_admission: Callable[[], Awaitable[None]] | None = None
        self._instruction_hooks: list[Callable[[Turn, TurnMeta, str], str]] = []
        self._session_id: int | None = None
        self._altered: dict[int, Altered] = {}  # tonight's state per member
        self._dead: dict[int, float] = {}  # guid -> when they died
        # jobs and sending; a turn id is unique across runs of the mind (_new_id)
        self._ids = itertools.count(1)
        self._id_run = secrets.token_hex(4)
        self._tasks: set[asyncio.Task] = set()
        self._current: dict[asyncio.Task, Turn] = {}  # the turn each job is on
        self._behind: dict[asyncio.Task, list[Turn]] = {}  # the turns a job has not begun yet
        self._cancelling: dict[asyncio.Task, Turn] = {}  # a job's one turn being cancelled
        # a model turn composing or typing, and since when (turn_phases); cleared at its end
        self._phases: dict[asyncio.Task, tuple[Turn, str, float]] = {}
        self._delivering: set[asyncio.Task] = set()
        # each queued job's turns with their metas (_queue), the turn each job is waiting out
        # the delay of with a wake (_delay) and the jobs whose line is at the bridge
        # (_send_line): waiting_turns, hurry and withdraw read them
        self._job_items: dict[asyncio.Task, list[tuple[Turn, TurnMeta]]] = {}
        self._delaying: dict[asyncio.Task, Turn] = {}
        self._sending: set[asyncio.Task] = set()
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
        # a member's common stock phrases her last owed answer leaned on, for her next turn's
        # prompt (phrase_hint_pending): (when, the stock list's own words)
        self._phrase_pending: dict[int, tuple[float, tuple[str, ...]]] = {}
        self._words: dict[int, str] = {}  # a said line's id -> its words before styling
        self._turn_lines: dict[str, int] = {}  # a turn id -> the lines it said
        self._compose_rng = random.Random("compose")  # compose() never draws from self.rng
        self._length_rng = random.Random()  # a channel message's length band (_discord_words)
        # her last want in each conversation (speaker, chain base), the latest WANTS_KEPT
        self._wants: dict[tuple[int, str], Intent] = {}
        # what each member did with each counterpart, kept in mind.db across restarts
        self.moves = MoveLog(self.store)
        self.notes = PairNotes(self.store)

    def add_instruction_hook(self, fn: Callable[[Turn, TurnMeta, str], str]) -> None:
        """Extend live, generated instructions immediately before composition. A hook may
        raise TurnWithdrawn to drop the turn there, before the model is asked."""
        self._instruction_hooks.append(fn)

    def attach_photos(self, fn: Callable[[Turn, TurnMeta, float], PhotoOffer | None]) -> None:
        if getattr(self, '_photo_offer', None) is not None:
            raise ValueError('a photo sender is already attached')
        self._photo_offer = fn

    @staticmethod
    def _release_photo(meta: TurnMeta, reason: str) -> None:
        if meta.photo is None or meta.photo_finished:
            return
        meta.photo_finished = True
        meta.photo.reason = reason
        try:
            meta.photo.release()
        except Exception:
            log.warning('photo_release_failed')

    def _photo_owed(self, turn: Turn, meta: TurnMeta) -> bool:
        """The turn answers his Discord post (discord.talk, on time or on her own time) and drew a
        photo it cannot carry before any line was said: the answer goes on without the photo.
        An opener (server:dm, server:photo) has nothing owed and is dropped with it."""
        return (meta.photo is not None and not meta.photo_finished and meta.said == 0
                and (meta.why in (DISCORD_REPLY_WHY, 'selfie:request') or self._answers_his_dm(turn, meta)))

    async def _answer_without_photo(self, turn: Turn, meta: TurnMeta, reason: str) -> list[str]:
        """His answer's photo was refused (its caption tripped the screens) or went stale at
        send: release it, spending nothing, and ask the answer once more without it, as an
        ordinary answer. A turn's photo is asked for only once, so this never repeats."""
        self._release_photo(meta, reason)
        meta.photo = None
        meta.single_line = meta.single_line_before_photo
        turn.delay = 0  # its answer delay has passed
        meta.guard_verdict, meta.guard_rewritten, meta.guard_codes = "not_run", False, ()
        meta.preference_original = ""
        meta.realism_planned, meta.realism_review_lines, meta.words = False, (), ""
        if self._real_name_hold == meta.turn_id:  # composed again below
            self._real_name_hold = None
        log.info("photo_answer_without: guid=%d reason=%s", turn.speaker, reason)
        return await self._speak_now(turn, meta)

    # -- identity and lookups ------------------------------------------------------------------

    def set_human(self, guid: int) -> None:
        """Who the human is: character name and rank from the stored roster, nickname, real
        name and pronouns from the config. Task 17 calls it from hello and login."""
        self.human = human_identity(self.cfg, self.store, guid)

    def _new_id(self) -> str:
        """A turn id no earlier run of the mind used: its run's nonce, then its number. Stored
        rows keep it (discord_message.turn, utterance.turn_id), so an id that restarted at t1
        found another run's messages under the same id."""
        return f"t{self._id_run}.{next(self._ids)}"

    def _members_for(self, channel: str):
        return self.world.discord_members() if is_discord(channel) else self.world.members

    def _names(self, channel: str = "") -> dict[int, str]:
        out = {g: m.name for g, m in self._members_for(channel).items()}
        if self.human.guid:
            out[self.human.guid] = self.human.called
        return out

    def _protect(self, channel: str = "") -> frozenset[str]:
        h = self.human
        return frozenset({m.name for m in self._members_for(channel).values()}
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

    def _pronoun_tags(self, turn: Turn, people: list[int], recent) -> dict:
        """Each member shown other than the speaker and him, tagged once with their persona's
        pronoun (the player's, never the character's: owner ruling 2026-11-15): on their card
        under "People here", else on their newest chat line, the one a trim keeps longest. Keys
        ("card", guid) and ("line", chat line id)."""
        hg = self.human.guid

        def of(g: int) -> str:
            return (pronoun_of(self.personas.get(g))
                    if g and g not in (hg, turn.speaker) else "")
        out = {("card", g): t for g in people if (t := of(g))}
        greet = turn.kind is TurnKind.GREET  # a greeter sees who said hi, never their line
        newest = {r.from_guid: r.id for r in recent
                  if not (greet and r.id in self._greet_lines and r.from_guid != turn.speaker)}
        for g, line in newest.items():
            if g not in people and (t := of(g)):
                out[("line", line)] = t
        return out

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

    def _away_now(self) -> frozenset[int]:
        """The members away (arc.live.away_now), read at most once every AWAY_CACHE_S."""
        now = self.clock.now()
        at, away = self._away_cache
        if not 0 <= now - at < AWAY_CACHE_S:
            away = away_now(self.store, now)
            self._away_cache = now, away
        return away

    def _candidates(self, audience=None, *, exclude=(), hearable=True) -> list[int]:
        """Online cast members with a persona who are not AFK or away (_away_now), in
        `audience` when given, and with voice on, unless `hearable` is off, only those he can
        hear where they sit."""
        w = self.world
        pool = w.online if audience is None else w.online & set(audience)
        away = self._away_now()
        out = [g for g in sorted(pool) if g not in w.afk and g not in exclude
               and g not in away and self.personas.get(g) is not None]
        if hearable and self.comms is not None:
            out = [g for g in out if self._comms_says("hearable", True, g)]
        return out

    def _weigh(self, guids, *, last: int | None, ctx: SpeakerContext, reply: bool,
               fresh=frozenset(), with_him=frozenset()) -> tuple[dict[int, float], set[int]]:
        """Speaker weights, and the members with a real reason to speak (select.has_reason):
        `last` is whoever spoke last (the human, for a reply). When it is the human, his row
        counts only as lived and the members with an open thread with him are known
        (SpeakerContext.to_human, .thread). Members in `fresh` are weighed as if they had not
        spoken lately; members in `with_him` (the voice room he speaks in) give him full attention
        ([life] availability)."""
        weights, reasons = {}, set()
        hg = self.human.guid
        if hg and last == hg:
            ctx = replace(ctx, to_human=True, thread=frozenset(
                g for g, at in self._answered_at.items() if ctx.now - at < OPEN_THREAD_S))
        for g in guids:
            p = self.personas.get(g)
            rel = self.store.get_relationship(g, last, lens=True) if last else None
            rel_h = None
            if reply and hg:
                rel_h = rel if last == hg else self.store.get_relationship(g, hg, lens=True)
            weights[g] = speaker_weight(p, self.store.get_state(g, lens=True), rel, ctx,
                                        None if g in fresh else self.world.last_spoke.get(g),
                                        self._effects(g).talk_mult)
            if has_reason(p, rel, ctx, rel_to_human=rel_h, reply=reply):
                reasons.add(g)
        life = getattr(self, "life", None)
        if life is not None and life.availability > 0:  # her attention in game (life/service.py)
            weights = {g: w * life.weight(g, with_him=g in with_him)
                       for g, w in weights.items()}
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
        for i, (turn, meta) in enumerate(items):
            if task in self._behind:
                self._behind[task] = [t for t, _ in items[i + 1:]]
            if meta.settled:  # withdrawn before it began (withdraw): its row is written
                continue
            try:
                said = await self._speak(turn, meta)
                if not said and owes_him(self, turn, meta) and i + 1 < len(items):
                    nxt, nmeta = items[i + 1]  # the next answer to his line takes on the debt
                    if (nxt.trigger_line == turn.trigger_line and nxt.kind in REPLY_KINDS
                            and (nmeta.discord or None) == (meta.discord or None)):
                        nmeta.owed_answer = True
            except asyncio.CancelledError:
                if self._cancelling.get(task) is not turn or task.cancelling() > 1:
                    raise
                del self._cancelling[task]
                task.uncancel()

    def _queue(self, items: list[tuple[Turn, TurnMeta]]) -> asyncio.Task | None:
        """Queue the complete turn chain and return its terminal job to an optional owner."""
        if items:
            now = self.clock.now()
            for turn, meta in items:
                if not meta.planned_at:
                    meta.planned_at = now
                self._mark_borrow(meta)
                self._answer_queued(turn, meta)
                self._admit_timing(meta)
            tokens = tuple(meta.timing_trace for _, meta in items)
            run = self._run(items)
            task = self._spawn(run)
            self._behind[task] = [t for t, _ in items]
            self._job_items[task] = items
            # A task cancelled before its first step never enters _run or _speak.
            def done(t):
                run.close()  # release an unawaited chain if the wrapper never started
                self._behind.pop(t, None)
                self._job_items.pop(t, None)
                self.timing.queued_done(tokens, cancelled=t.cancelled())
                if (outreach := getattr(self, 'outreach', None)) is not None:
                    for _, meta in items:
                        outreach.release(meta)
            task.add_done_callback(done)
            return task

    def _answer_queued(self, turn: Turn, meta: TurnMeta) -> None:
        """An answer owed to him frees this speaker's background review lock."""
        server = getattr(getattr(self, 'server_app', None), 'server', None)
        if (server is None or self._server_turn(turn, meta)
                or not (turn.kind is TurnKind.REPLY_HUMAN or meta.answers_human
                        or self.live_review.owed(turn, meta))):
            return
        for source in (getattr(server, 'engine', None), getattr(server, 'dms', None)):
            if source is not None:
                source.answer_queued(turn.speaker)

    def _admit_timing(self, meta: TurnMeta) -> None:
        if meta.timing_trace is None:
            meta.timing_trace = self.timing.begin(origin=meta.timing_origin)

    @asynccontextmanager
    async def _timed_send_lock(self, meta: TurnMeta):
        with self.timing.span(meta.timing_trace, "send_lock"):
            await self._send_lock.acquire()
        try:
            yield
        finally:
            self._send_lock.release()

    def pending_turns(self) -> list[Turn]:
        """The turn each of the engine's running jobs is on (waiting, composing or typing)."""
        return [t for task, t in self._current.items() if task in self._tasks]

    def active_speakers(self, *, exclude_task: asyncio.Task | None = None) -> frozenset[int]:
        """Include source-owned turns; task ownership survives a copied Turn."""
        return frozenset(turn.speaker for task,turn in self._current.items()
                         if task is not exclude_task and not task.done())

    def _phase(self, turn: Turn, phase: str | None) -> None:
        """The current job's turn is now composing or typing (since now), or neither (None)."""
        task = asyncio.current_task()
        if phase is not None:
            self._phases[task] = (turn, phase, self.clock.now())
        elif task in self._phases and self._phases[task][0] is turn:
            del self._phases[task]

    def turn_phases(self) -> list[tuple[Turn, str, float]]:
        """(turn, COMPOSING or TYPING, since) for each model turn being composed or typed."""
        return list(self._phases.values())

    def typing_now(self) -> list[dict]:
        """The control endpoint's status `typing`: each model turn being composed or typed where
        Comms shows its line, oldest first, as {"guid", "name", "channel", "to", "since", "age",
        "phase"} (`phase`: COMPOSING, TYPING or RECORDING; `to`: a whisper's target, him; else
        None; `age`: seconds since the phase's latest refresh, at least every ten seconds during
        a realism wait, read now, so a page judges staleness without comparing clocks; `since` is
        for matching the line when it lands). Ids, names and times only, never text; the game's places none while he is
        offline, a Discord turn's (channel as given, `to` him in a DM to him) always."""
        w, hg = self.world, self.human.guid
        if not hg:
            return []
        now = self.clock.now()
        out = []
        for turn, phase, since in self._phases.values():
            channel, to = turn.channel, None
            if is_discord(channel):
                to = hg if turn.to_guid == hg else None
            elif not w.human_online:
                continue
            elif channel == "whisper" and turn.to_guid == hg:
                to = hg
            elif not (channel in COMMS_CHANNELS
                      or (channel in GROUP_CHANNELS and w.in_group(turn.speaker))):
                continue
            m = w.members.get(turn.speaker)
            out.append({"guid": turn.speaker, "name": m.name if m is not None else "",
                        "channel": channel, "to": to, "since": since,
                        "age": max(0.0, now - since), "phase": phase})
        return sorted(out, key=lambda e: (e["since"], e["guid"]))

    def planned_turns(self) -> list[Turn]:
        """Every turn the engine's jobs have yet to finish: the one each is on and the queued
        ones behind it."""
        return self.pending_turns() + [t for task, behind in self._behind.items()
                                       if task in self._tasks for t in behind]

    def waiting_turns(self, *, started: bool = False) -> list[tuple[asyncio.Task, Turn, TurnMeta]]:
        """(job, turn, meta) for each queued turn that has not begun yet or is waiting out its
        delay with a wake (_delay; [life] availability above 0): what hurry and coalescing act
        on. With `started`, also each job's turn past its delay (composing, typing) that has
        said nothing and has no line at the bridge: what his logout can still carry
        (LifeService.on_logout)."""
        out = []
        for task, items in list(self._job_items.items()):
            if task not in self._tasks or task.done():
                continue
            behind, delaying = self._behind.get(task, ()), self._delaying.get(task)
            current = self._current.get(task) if started and task not in self._sending else None
            for turn, meta in items:
                if meta.settled:
                    continue
                if (turn is delaying or any(turn is b for b in behind)
                        or (turn is current and meta.said == 0)):
                    out.append((task, turn, meta))
        return out

    @staticmethod
    def hurry(meta: TurnMeta) -> None:
        """End the turn's delay now (a turn that has not begun yet skips it)."""
        if meta.wake is None:
            meta.wake = asyncio.Event()
        meta.wake.set()

    def withdraw(self, task: asyncio.Task, turn: Turn, meta: TurnMeta,
                 reason: str) -> float:
        """Take one turn of waiting_turns out of its job, dropped with `reason`, as
        _cancel_refused cancels one turn of a job; the rest of the job goes on. What was left
        of its delay: its full delay if it had not begun, 0 if hurried or past its delay
        (composing, typing)."""
        now = self.clock.now()
        left = turn.delay
        if self._current.get(task) is turn:  # in its delay, composing or typing
            left = meta.wake_at - now if self._delaying.get(task) is turn else 0.0
            self._cancelling[task] = turn
            task.cancel()
            self._delaying.pop(task, None)
            self._current.pop(task, None)
            self._phases.pop(task, None)
        if meta.wake is not None and meta.wake.is_set():
            left = 0.0
        self._drop(turn, meta, reason)
        meta.settled = True  # its row is written: no "cancelled" row, and _run skips it
        return max(0.0, left)

    def _cancel_refused(self, now: float) -> None:
        """A fight just started: cancel each pending turn the raid rules now refuse (that turn,
        not the rest of its job)."""
        for task in list(self._tasks):
            turn = self._current.get(task)
            if turn is not None and not self.raid.allows(turn.speaker, turn.kind, now):
                self._cancelling[task] = turn
                task.cancel()
                del self._current[task]  # not pending any more, even before it unwinds
                self._phases.pop(task, None)

    async def _cancel_all(self, keep=frozenset()) -> None:
        """Cancel every job but `keep` (the answers his logout carried to Discord), then wait
        for the lines already handed to the bridge (bounded by its ack timeout) so they are
        recorded."""
        me = asyncio.current_task()
        tasks = [t for t in self._tasks if t is not me and t not in keep]
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

    async def preview_line(self, turn: Turn, persona: Persona, *, trigger: str, seed: int,
                           dial: int | None = None) -> tuple[str, str | None]:
        """The console's tuning preview: the line `persona` (a candidate effective persona)
        would say for `turn`, answering his line `trigger` (text, never a stored line), through
        the live prompt and the model with the tells, repairs and retries a line gets, styled
        as she types with a generator of its own. Never sent, recorded or spent: no memory is
        recalled or touched, no cooldown starts, his real name is never offered, and the
        engine's own generator is never drawn from. Both of a preview's requests carry `seed`.
        (the words, None) or ("", why it said nothing)."""
        hg = self.human.guid
        now = self.clock.now()
        channel = turn.channel or "guild"
        row = ChatLineRow(id=0, ts=now, channel=channel, from_guid=hg,
                          from_name=self.human.character_name, to_guid=turn.to_guid,
                          text=trigger, map=0, zone=0, session_id=None, thread_id=None,
                          origin="human") if trigger else None
        meta = TurnMeta(turn_id="preview", seed=seed)
        c = await self._compose(turn, meta, live=False, persona=persona,
                                window=[row] if row is not None else [],
                                trigger_text=trigger or None, dial=dial)
        text, why = await self._generate(turn, meta, c)
        if why:
            return "", why
        styled = style_lines(text, persona.typing, EFFECTS[Altered.SOBER],
                             random.Random(seed), protect=self._protect(),
                             speaker=persona.character_name, kind=turn.kind,
                             max_lines=max(1, turn.max_lines), aliases=aliases(persona),
                             texture=self.cfg.talk.texture)
        return "\n".join(s.line for s in styled), None

    def _may_carry(self, turn: Turn, row: ChatLineRow) -> bool:
        """Whether a chat line may be in this turn's chat window, the conversation she answers
        (memory.recall.whisper_ok, in the turn's own medium: R11). The other medium's lines
        between her and him come only through _pair_lines, in a block of their own."""
        from leeroy_mind.groups.audience import turn_readers
        return same_medium(row.channel, turn.channel) and whisper_ok(
            row.channel, row.from_guid, row.to_guid, speaker=turn.speaker,
            turn_channel=turn.channel, to_guid=turn.to_guid, source_readers=row.audience,
            readers=turn_readers(self.store.conn,turn.channel,turn.speaker,turn.to_guid))

    def _pair_lines(self, turn: Turn, recent: list[ChatLineRow], now: float) -> list[ChatLineRow]:
        """One mind per member: in a private turn between her and him (a whisper or a Discord DM),
        their lines from the other medium within PAIR_WINDOW_S that the chat shown lacks, at most
        PAIR_LINES, oldest first, each labelled with where it was said (prompt.chat_line_text), for
        the prompt's own block (PromptParts.elsewhere), never the chat she answers (R11). An opener
        sees only hers; a deleted DM post of his is left out, as the DM's own context leaves it."""
        hg = self.human.guid
        if not hg or turn.speaker == hg or turn.to_guid != hg or not is_private(turn.channel):
            return []
        have = {r.id for r in recent}
        try:
            rows = self.store.pair_lines(turn.speaker, hg, since=now - PAIR_WINDOW_S,
                                         limit=PAIR_LINES, discord=not is_discord(turn.channel))
        except Exception as e:
            log.warning("pair_lines_failed: guid=%d reason=%s", turn.speaker, type(e).__name__)
            return []
        return [r for r in rows if r.id not in have
                and whisper_ok(r.channel, r.from_guid, r.to_guid, speaker=turn.speaker,
                               turn_channel=turn.channel, to_guid=turn.to_guid)
                and (turn.kind is not TurnKind.OPEN or r.from_guid != hg)]

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

    def _counterpart(self, turn: Turn) -> int:
        """Whom the turn is with: the one a whisper goes to, else the member or him it is
        addressed to; 0 for a turn to nobody in particular."""
        return turn.to_guid if turn.channel == "whisper" else turn.addressee or 0

    def _forms_of(self, guid: int) -> tuple[str, str, str]:
        """The pronouns for `guid`: his own, and "they" for anyone else."""
        return self.human.forms if guid and guid == self.human.guid else THEY_FORMS

    def _recent_moves(self, turn: Turn) -> list[MoveRow]:
        """Her last turns to this turn's counterpart that this turn may see (a whisper's only
        in a whisper between the same two), oldest first; none for a turn to nobody."""
        other = self._counterpart(turn)
        if not other:
            return []
        return self.moves.recent(turn.speaker, other, turn_channel=turn.channel,
                                 to_guid=turn.to_guid, now=self.clock.now())

    def _summons_ok(self, turn: Turn) -> bool:
        """Whether asking him over may stand in her line to him (_generate checks only those):
        in a voice room, where the two are together; and in her whisper or Discord DM to him
        inside a plan he has talked about or said yes to (arc.live's summons_ok). Everywhere else
        they only talk through the game."""
        if turn.channel.startswith("voice:"):
            return True
        p = self.personas.get(turn.speaker)
        return (p is not None and is_private(turn.channel) and turn.to_guid == self.human.guid
                and self._arc_view(turn, p, self.clock.now()).summons_ok)

    def _arc_view(self, turn: Turn, p: Persona, now: float,
                  rng: random.Random | None = None) -> ArcView:
        """Her arc as this turn may show it (arc.live.arc_view: its private part only in a
        whisper or Discord DM to him; `rng` draws which of its lines fit this turn)."""
        return arc_view(self.store, p, self.human.guid, now=now, channel=turn.channel,
                        to_guid=turn.to_guid, forms=self.human.forms, names=self._names(turn.channel),
                        rng=rng)

    def _record_moves(self, turn: Turn, words: str) -> None:
        """Her said turn's moves (MoveLog), for the next prompts to the same person. A failing
        write is logged, never with the words, and never raised."""
        try:
            self.moves.record(guid=turn.speaker, counterpart=self._counterpart(turn),
                              channel=turn.channel, to_guid=turn.to_guid, text=words,
                              ts=self.clock.now())
        except Exception as e:
            log.warning("move_record_failed: guid=%d channel=%s reason=%s", turn.speaker,
                        turn.channel, type(e).__name__)

    def _human_in_talk(self, turn: Turn, recent: list[ChatLineRow]) -> bool:
        hg = self.human.guid
        return bool(hg) and (turn.addressee == hg or any(r.from_guid == hg for r in recent[-3:]))

    def _real_name_private(self, turn: Turn) -> bool:
        """Where his inner circle may use his real name: a private conversation with him, i.e. a
        whisper to him, his Discord DM with her (a DM is always to him, _dm_to_him) or a group
        DM (he is in every open one). Guild, officer, party, raid, say, a voice room and the
        Discord channels are public: there he is his nickname or character name."""
        hg = self.human.guid
        if not hg:
            return False
        if turn.channel == "whisper":
            return turn.to_guid == hg
        return turn.channel.startswith(DM_PREFIX) or is_group(turn.channel)

    def _romance(self, turn: Turn, meta: TurnMeta, p: Persona, rel_h, phase: DayPhase,
                 now: float, rng: random.Random, trigger: str = "",
                 dial: int | None = None, tail_follows: bool = False,
                 vary: random.Random | None = None) -> str | None:
        """Her romance text on this turn, or None: only on turns to the human; every FLIRT,
        WHISPER, loot cheer, reply on the whisper channel to him and reply to a flirty line of
        his (`trigger`); otherwise on a share of turns (ROMANCE_SHARE; a drawn woman's
        DRAWN_TALKED_TO_SHARE when he talks to her, from NO_BRAKES_DIAL), halved within
        ROMANCE_COOL_S of a line of hers that carried it. The dial is hers when he has set one
        for her (romance.dial_for); at her own FULL_DIAL it is every turn to him. `rng` rolls
        the share and `vary` (`rng` without it) picks the variants (stage passage, heat, band
        overlay); `tail_follows`: the whisper tail says the distance frame. Her scene notes
        about him go in as the turn may see them
        (PairNotes.recent: a whisper's only in a whisper between the two, a DM's only in that
        DM), then her arc's recent events, and with them her arc's lines, rival and cooling
        (arc.live.arc_view, all of them for a whisper or DM to him only); the romance text
        shows them only in a whisper or a Discord DM (is_private). With [romance] realism the
        text is romance_instruction's `realism` one, turned up only when his line (`trigger`, or
        on Discord his message: _his_words) is (romance.his_heat)."""
        hg = self.human.guid
        if (is_group(turn.channel) or not hg or turn.addressee != hg or p.identity.player_gender != "female"
                or p.romance.stance not in ROMANCE_SHARE or meta.casual):
            return None
        dial = self.cfg.romance.dial if dial is None else dial
        talked_to = turn.kind in (TurnKind.REPLY_HUMAN, TurnKind.GOODNIGHT) and bool(
            turn.trigger_line)
        always = (turn.kind in ROMANCE_ALWAYS or meta.loot_cheer
                  or (turn.channel == "whisper" and turn.to_guid == hg)
                  or (talked_to and flirty(trigger)) or own_dial(p) == FULL_DIAL)
        if not always:
            share = ROMANCE_SHARE[p.romance.stance]
            if (p.romance.stance == "drawn" and talked_to
                    and dial_for(p, dial) >= NO_BRAKES_DIAL):
                share = DRAWN_TALKED_TO_SHARE
            if phase in _LATE:
                share += ROMANCE_LATE_BONUS
            if now - self._romance_at.get(p.guid, -math.inf) < ROMANCE_COOL_S:
                share /= 2
            f = forward(p)  # her forward dial (tuning): how often her turns carry it
            if f != 1.0:
                share = min(1.0, share * f)
            if rng.random() >= share:
                return None
        arc = self._arc_view(turn, p, now, vary or rng)
        scene = [*self.notes.recent(p.guid, hg, turn_channel=turn.channel, to_guid=turn.to_guid,
                                    now=now), *arc.scene]
        realism = self.cfg.romance.realism
        heat = realism and his_heat(self._his_words(meta, trigger))
        return romance_instruction(p, rel_h, dial=dial, channel=turn.channel,
                                   addressee_is_human=True, phase=phase, human=self.human,
                                   data_dir=self.data_dir, rng=vary or rng,
                                   tail_follows=tail_follows, scene=scene,
                                   arc_lines=arc.arc_lines, rival=arc.rival, cooling=arc.cooling,
                                   realism=realism, heat=heat)

    def _his_words(self, meta: TurnMeta, trigger: str) -> str:
        """His line the turn answers, for [romance] realism's his_heat: the trigger line's text,
        or on a Discord answer (no trigger line) his message it answers, "" when it is gone."""
        if trigger:
            return trigger
        found = self._his_message(meta)
        return found[0] if found else ""

    def _his_message(self, meta: TurnMeta) -> tuple[str, float] | None:
        """The text and time of his Discord message a turn answers (meta.discord.reply_to; his,
        not deleted), or None."""
        if meta.discord is None or meta.discord.reply_to is None or not self.human.guid:
            return None
        with self.store.transaction():
            row = self.store.conn.execute(
                "SELECT text, ts FROM discord_message WHERE id = ? AND author = ? "
                "AND origin = 'human' AND deleted IS NULL",
                (meta.discord.reply_to, self.human.guid)).fetchone()
        return (row[0], row[1]) if row else None

    def _his_dm_row(self, turn: Turn, meta: TurnMeta) -> ChatLineRow | None:
        """His DM a member answers, as the last line of her Chat, on every DM answer to him
        (controller ruling 2026-11-16, a correctness fix, not behind a switch): discord.talk's
        dm_chat leaves it out (the instruction quotes it), so the chat she read ended on his
        previous message and her answer to it, and she answered that one (the owner's sandbox
        audit, 2026-11-16). Shown as dm_chat shows his lines."""
        found = self._his_message(meta)
        if found is None:
            return None
        from leeroy_mind.discord.talk import DM_HIS_CHARS, _clip
        from leeroy_mind.photos.notes import by_messages
        hg, mid = self.human.guid, meta.discord.reply_to
        text = _clip(fold(found[0]), DM_HIS_CHARS)
        photo = by_messages(self.store.conn, [mid],
                            uploads=getattr(self, 'photos_enabled', False),
                            members=getattr(self, 'photos_send', False),
                            memes=getattr(self, 'culture_memes', False)).get(mid)
        if photo:
            text += ' ' + photo
        return ChatLineRow(id=mid, ts=found[1], channel=turn.channel, from_guid=hg,
                           from_name=self.human.called, to_guid=turn.speaker, text=text,
                           map=0, zone=0, session_id=None, thread_id=None, origin="human")

    async def _compose(self, turn: Turn, meta: TurnMeta, *, live: bool,
                       persona: Persona | None = None, window: list | None = None,
                       trigger_text: str | None = None, dial: int | None = None) -> Composed:
        """A live turn composes inside lab/live.composing: the conversation lab's prompt pieces
        in force for it (a sandbox's lab overlays, a lab arm the learn bandit drew), or nothing
        at all without them."""
        if live and (getattr(self, "lab_overlays", None) is not None
                     or getattr(self, "learn_enabled", False)):
            from leeroy_mind.lab.live import composing
            with composing(self, turn, meta):
                return await self._compose_turn(turn, meta, live=live, persona=persona,
                                                window=window, trigger_text=trigger_text, dial=dial)
        return await self._compose_turn(turn, meta, live=live, persona=persona, window=window,
                                        trigger_text=trigger_text, dial=dial)

    async def _compose_turn(self, turn: Turn, meta: TurnMeta, *, live: bool,
                            persona: Persona | None = None, window: list | None = None,
                            trigger_text: str | None = None, dial: int | None = None) -> Composed:
        kwargs = dict(live=live,persona=persona,window=window,trigger_text=trigger_text,dial=dial)
        if live and getattr(self,"learn_enabled",False):
            from leeroy_mind.prompts import trace_templates
            from leeroy_mind.learn.recipe import templates_used
            with trace_templates() as used:
                result = await self._compose_prompt(turn,meta,**kwargs)
            try:
                templates_used(meta,used)
            except Exception:
                log.warning("learn_recipe_describe_failed")
            return await self._with_beat(turn, meta, result)
        result = await self._compose_prompt(turn,meta,**kwargs)
        return await self._with_beat(turn, meta, result) if live else result

    def _beat_on(self, turn: Turn, meta: TurnMeta | None) -> bool:
        """[talk] think_private (read at mind start, TalkEngine): an answer to his whisper or to
        his Discord DM gets a beat first (talk/beat.py). Never game channels, Discord channels,
        group DMs, voice rooms or a template's turn."""
        if meta is None or not getattr(self, "think_private", False):
            return False
        return self._his_private(turn, meta)

    def _his_private(self, turn: Turn, meta: TurnMeta) -> bool:
        """His private turn: an answer to his whisper or to his Discord DM, never a template's."""
        if turn.template is not None:
            return False
        hg = self.human.guid
        whisper = self._answers_his_whisper(turn) or (
            bool(hg) and meta.whisper_reply and turn.channel == "whisper" and turn.to_guid == hg)
        return bool(whisper or self._answers_his_dm(turn, meta))

    async def _with_beat(self, turn: Turn, meta: TurnMeta, c: "Composed") -> "Composed":
        """`c` with the one block after its instruction (beat.render). On an answer that gets a
        beat, the beat's lines: it is asked once a turn, so a later composition (a guard's
        rewrite, a fresh answer) reuses them, and every request of the turn, the owed fallback's
        included, carries the same lines. On any other turn, or when the beat was skipped, the
        code's block (_block: her drawn want, her state's "It lands"; "" with [talk] intent and
        [life] state off, so the turn is as before)."""
        if self._beat_on(turn, meta):
            if meta.beat is None:
                meta.beat = await self._beat(turn, meta, c)
            if meta.beat:
                return replace(c, messages=_with_hint(c.messages, meta.beat))
        block = self._block(turn, meta)
        return replace(c, messages=_with_hint(c.messages, block)) if block else c

    # -- her state, want and situation (human-likeness core, plan Task 8) ----------------------

    def _core_on(self) -> bool:
        """A switch of the core prompt is on: [talk] intent or situation, or [life] state."""
        talk = getattr(self.cfg, "talk", None)
        life = getattr(self, "life", None)
        return (getattr(talk, "intent", False) is True or getattr(talk, "situation", False) is True
                or getattr(life, "state", None) is not None)

    def _want_eligible(self, turn: Turn, meta: TurnMeta) -> bool:
        """A turn that gets a want and "It lands" (intent.INTENT_KINDS, the model's own line):
        never a template's, his pushback's answer, a photo's or selfie request's, an outreach
        item's, the owner's string, his lore or game question with its facts, the watchdog's
        rescue, a room he is not in, a hot mic or the 24/7 server's turn: each of those carries
        its own reason to speak."""
        return (turn.kind in INTENT_KINDS and turn.template is None and not meta.pushback
                and meta.photo is None and meta.photo_request is None
                and meta.outreach_item is None and meta.godmode_intent is None
                and not meta.lore and not meta.facts_line and not meta.owed_rescue
                and not meta.elsewhere and meta.why != "hotmic"
                and not self._server_turn(turn, meta))

    def _his_line(self, turn: Turn, meta: TurnMeta) -> str:
        """The line of his this turn answers, "" when it answers none: its trigger line when it is
        his (and hers to see), his Discord post it replies to, or his whisper a carried DM answer
        answers (its recall query). Read only to weigh it; never shown from here."""
        hg = self.human.guid
        if not hg:
            return ""
        if turn.trigger_line:
            rows = self.store.chat_lines([turn.trigger_line])
            ok = rows and rows[0].from_guid == hg and self._may_carry(turn, rows[0])
            return rows[0].text if ok else ""
        if meta.answers_human and meta.discord is not None and meta.discord.reply_to:
            found = self._his_message(meta)  # his own message, not deleted
            return found[0] if found is not None else ""
        if meta.why == "reply:carried":  # life.service CARRY_WHY: his whisper, in her DM
            return meta.recall_query
        return ""

    def _want_key(self, turn: Turn) -> tuple[int, str]:
        return turn.speaker, self._turn_base(turn)

    def _private_to_him(self, turn: Turn) -> bool:
        """A turn only he reads: a whisper to him (his answer or her own start) or his DM."""
        hg = self.human.guid
        return bool(hg) and ((turn.channel == "whisper" and turn.to_guid == hg)
                             or turn.channel.startswith(DM_PREFIX))

    def _intent_context(self, turn: Turn, meta: TurnMeta, p: Persona, his: str) -> IntentContext:
        """Her context for the want's draw: the turn, her persona and dials, her day
        (meta.life_presence; neutral at availability 0) and feeling (meta.state), his explicit
        overlay as `tail` (whisper_tail in a whisper, romance.dm_explicit in his DM: whether it
        applies, never which variant, and whether or not her romance text is rolled in) and her
        last want in this conversation. On a turn only he reads (_private_to_him: his answers
        and her own whisper starts) flirting follows the beat's own rule (romance.flirt_chance;
        review-332, review-340): a woman who may not flirt with him there draws none, so the
        beat is never leant toward a want it does not offer."""
        hg, channel = self.human.guid, turn.channel
        family = ("group" if is_group(channel) else "dm" if channel.startswith(DM_PREFIX)
                  else "discord" if is_discord(channel) else "whisper" if channel == "whisper"
                  else "game")
        lp = meta.life_presence
        rel = self.store.get_relationship(turn.speaker, hg, lens=True) if hg else None
        dial = self.cfg.romance.dial
        tail = bool(hg and turn.to_guid == hg and (
            whisper_tail(p, rel, dial=dial, channel=channel, data_dir=self.data_dir,
                         forms=self.human.forms)
            or dm_explicit(p, rel, dial=dial, channel=channel, data_dir=self.data_dir)))
        fwd = forward(p)
        if self._private_to_him(turn) and flirt_chance(p, 1.0) <= 0:
            fwd = 0.0
        return IntentContext(
            kind=turn.kind, to_him=self._aimed_at_him(turn), owed=owes_him(self, turn, meta),
            his_line=his, family=family, female=p.identity.player_gender == "female",
            stance=p.romance.stance, style=p.romance.style, attachment=p.attachment,
            humour=p.humour, agreeableness=p.big_five.agreeableness.score,
            extraversion=p.big_five.extraversion.score, tail=tail,
            attention=lp.attention if lp is not None else None, state=meta.state,
            event=lp.event if lp is not None else "none",
            block=lp.block if lp is not None else None,
            kids=lp.kids if lp is not None else has_kids(p),
            last=self._wants.get(self._want_key(turn)), forward=fwd)

    def _draw_situation(self, turn: Turn, meta: TurnMeta, p: Persona, now: float) -> None:
        """Her situation as she starts composing (core plan Task 8; nothing with every switch
        off): where she is (meta.life_presence, [life] availability above 0: her Discord presence
        now on a Discord turn, a carried DM included, her game presence on a game turn when the
        engine did not plan it), her feeling (meta.state, [life] state: the 30 s snapshot), and
        on an eligible turn (_want_eligible) how his line lands on her (meta.lands) and her want
        ([talk] intent: one draw seeded by (intent, night, her, the minute, his line or post),
        never the engine's rng). Logs `talk_want: guid= kind= want= source=draw owed=` (codes
        only). A failure is logged by its class and leaves the turn as it would be without it."""
        try:
            self._situation(turn, meta, p, now)
        except Exception as e:  # her situation is colour: nothing in it may cost him an answer
            meta.intent, meta.want_source, meta.lands, meta.intent_ctx = "", "", "", None
            log.warning("talk_situation_failed: guid=%d reason=%s", turn.speaker,
                        type(e).__name__)

    def _situation(self, turn: Turn, meta: TurnMeta, p: Persona, now: float) -> None:
        life = getattr(self, "life", None)
        intent_on = getattr(getattr(self.cfg, "talk", None), "intent", False) is True
        avail = life is not None and life.availability > 0
        book = getattr(life, "state", None) is not None
        if not (avail or book or intent_on):
            return
        if avail:
            if is_discord(turn.channel):
                meta.life_presence = life.presence(turn.speaker, medium="discord")
            elif meta.life_presence is None:
                meta.life_presence = life.presence(
                    turn.speaker, medium="game", in_his_group=self.world.in_group(turn.speaker))
        if book:
            meta.state = life.state_of(turn.speaker)
        if not self._want_eligible(turn, meta):
            return
        his = self._his_line(turn, meta) if (meta.state is not None or intent_on) else ""
        if meta.state is not None and his:
            meta.lands = life_state.lands(meta.state, life_state.tone(his),
                                          agreeableness=p.big_five.agreeableness.score,
                                          forms=self.human.forms)
        if not intent_on:
            return
        ctx = self._intent_context(turn, meta, p, his)
        meta.intent_ctx = ctx
        local = self.clock.local()
        trigger = turn.trigger_line or (meta.discord.reply_to if meta.discord is not None
                                        else 0) or 0
        want = draw_intent(ctx, seeded("intent", night_of(local), turn.speaker,
                                       local.hour * 60 + local.minute, trigger))
        if want is None:
            return
        meta.intent, meta.want_source = want.value, "draw"
        log.info("talk_want: guid=%d kind=%s want=%s source=draw owed=%d", turn.speaker,
                 turn.kind.value, want.value, ctx.owed)

    def _where_line(self, turn: Turn, meta: TurnMeta) -> str:
        """Where she is, as "Right now" says it ([life] availability above 0, her presence on
        the meta): "" in a raid call (CALLOUT, URGENT) and in a voice room he is in (the room
        says where she is, review-340); at her PC playing while she is in his group as he plays
        (PM log D15); else by her block (life.presence.where_text). The awareness card leaves
        her Discord status out while this line shows (review-340 P2-1)."""
        lp = meta.life_presence if self._life_on() else None
        if (lp is None or turn.kind in RAID_CALLS
                or (turn.channel.startswith("voice:") and not meta.elsewhere)):
            return ""
        return where_text(lp, "discord" if is_discord(turn.channel) else "game",
                          playing=bool(self.world.human_online
                                       and self.world.in_group(turn.speaker)))

    def _remember_want(self, turn: Turn, meta: TurnMeta) -> None:
        """Her want on a line she said, as `last` for her next turn in this conversation."""
        key = self._want_key(turn)
        self._wants.pop(key, None)
        self._wants[key] = Intent(meta.intent)
        while len(self._wants) > WANTS_KEPT:
            del self._wants[next(iter(self._wants))]

    def _want_who(self, turn: Turn) -> str:
        """Whom "You want to:" names: him by what the guild calls him, a member by name,
        "everyone" on an opener (no addressee)."""
        hg = self.human.guid
        if turn.addressee and hg and turn.addressee == hg:
            return self.human.called
        if turn.addressee:
            return self._names(turn.channel).get(turn.addressee) or "everyone"
        return "everyone"

    def _block(self, turn: Turn, meta: TurnMeta) -> str:
        """The code's block after the instruction (beat.render): her drawn want with its topic
        and addressee, and "It lands" from her state on an answer to his line; "" when neither
        [talk] intent nor [life] state gives it a line."""
        want = Intent(meta.intent) if meta.intent else None
        if want is None and not meta.lands:
            return ""
        ctx = meta.intent_ctx
        topic = topic_for(want, ctx) if want is not None and ctx is not None else ""
        return beats.render(lands=meta.lands, want=want, who=self._want_who(turn), topic=topic)

    def _state_lands(self, turn: Turn, meta: TurnMeta) -> bool:
        """Her state says how his line lands ([life] state, an eligible turn): the beat is then
        asked without `lands` (beat.instruction's ask_lands)."""
        return meta.state is not None and self._want_eligible(turn, meta)

    def _block_reserve(self, turn: Turn, meta: TurnMeta) -> int:
        """The room the block after the instruction takes in the prompt's budget (review of #333,
        requirement 3; Review Focus 5), while a core switch is on (_core_on): before the beat is
        asked its block at its caps (beat.block_room), once it has run its own lines, else the
        code's block. 0 with every core switch off: as before, the beat's block then rides
        outside the budget and inside num_ctx."""
        if not self._core_on():
            return 0
        if self._beat_on(turn, meta):
            if meta.beat is None:
                state = self._state_lands(turn, meta)
                return beats.block_room(self.human.called, lands=meta.lands if state else None)
            if meta.beat:
                return estimate_tokens("\n" + meta.beat)
        block = self._block(turn, meta)
        return estimate_tokens("\n" + block) if block else 0

    async def _beat(self, turn: Turn, meta: TurnMeta, c: "Composed") -> str:
        """One short call on the turn's own prompt, constrained to beat.schema with think false:
        the lines it gives the answer, or "" when it is skipped (an error, BEAT.timeout seconds
        passed, the queue's included, or a reply that is empty or not the schema), and the line
        goes as it would without it. Its preparation runs inside the same guard, and after a wait
        for admission the scope and the deadline are checked again, as _ask does. Its time counts
        in the turn's llm_s. Logs carry the want code and times, never its words."""
        started, reason, wants, items, order = self.clock.now(), "", (), [], []
        # core plan Task 8: with [talk] intent the wants are the ones the turn allows, leaning
        # toward the drawn one; with [life] state her state says how his line lands
        ctx = meta.intent_ctx if isinstance(meta.intent_ctx, IntentContext) else None
        lean, ask_lands = None, not self._state_lands(turn, meta)
        try:
            human, their = self.human.called, self.human.forms[2]
            wants = beats.wants_for(flirt_chance(c.persona, 1.0) > 0, tail=c.tail,
                                    asking=c.asking)
            if ctx is not None:
                wants = tuple(i.value for i in allowed(ctx) if i.value in wants) or wants
                lean = Intent(meta.intent) if meta.intent in wants else None
            if c.asking:  # his line asks her something: the answering wants lead (intent.ordered)
                wants = tuple(i.value for i in ordered(wants, True))
            options = dict(BEAT.options)
            options["num_predict"] = getattr(self, "think_budget", options["num_predict"])
            offered = self._beat_offer(turn, meta, c, started)
            items = beats.fitting(sum(estimate_tokens(m["content"]) for m in c.messages), human,
                                  wants, [x for _, x in offered], predict=options["num_predict"],
                                  num_ctx=options["num_ctx"], their=their, lean=lean,
                                  ask_lands=ask_lands, asking=c.asking)
            if items is None:
                reason = "room"
            else:
                # what fits, in the turn's own order (beat.shuffled): position carries no bias
                order = beats.shuffled(offered[:len(items)], self._beat_rng(turn, meta, started))
                items = [x for _, x in order]
                messages = _with_hint(c.messages, beats.instruction(
                    human, wants, items, their, lean=lean, ask_lands=ask_lands, asking=c.asking))
                fmt = beats.schema(wants, human, their, ask_lands=ask_lands, offered=bool(items),
                                   asking=c.asking)

                async def job(llm):
                    if not _scope_valid(meta):
                        raise _ScopeChanged("scope_changed")
                    if self.live_admission is not None:
                        await self.live_admission()
                        if not _scope_valid(meta):
                            raise _ScopeChanged("scope_changed")
                        if self.scheduler.expired(self._deadline(turn, meta)):
                            raise Expired(turn.deadline)
                    return await llm.chat(self.cfg.models.live, messages, options=options,
                                          think=False, format=fmt, timeout=BEAT.timeout)
                credit = meta.borrow_credit
                extra = {'credit': credit} if credit is not None else {}
                result = await asyncio.wait_for(
                    self.scheduler.submit(turn.priority, job, deadline=turn.deadline, **extra),
                    BEAT.timeout)
        except TimeoutError:
            reason = "timeout"
        except GpuPaused:
            reason = "gpu_paused"
        except Expired:
            reason = "expired"
        except _ScopeChanged:
            reason = "scope_changed"
        except (SchedulerError, LLMError) as e:
            reason = "timeout" if "timed out" in str(e) else "llm_error"
        except Exception as e:  # the beat is optional: nothing in it may cost him his answer
            reason = "error"
            log.warning("think_beat_failed: guid=%d error=%s", turn.speaker, type(e).__name__)
        elapsed = self.clock.now() - started
        meta.llm_s = (meta.llm_s or 0.0) + elapsed
        found = None
        if not reason:
            found, reason = beats.parse(result.text, wants, items, ask_lands=ask_lands)
        if found is None:
            log.info("think_beat_skipped: guid=%d reason=%s ms=%d", turn.speaker, reason,
                     round(elapsed * 1000))
            return ""
        meta.beat_use = next((k for k, x in order if x == found.use), None) if found.use else None
        if ctx is None:
            log.info("think_beat: guid=%d want=%s use=%d ms=%d", turn.speaker, found.want,
                     found.use is not None, round(elapsed * 1000))
        else:  # the beat's want is the turn's; the drawn one was its lean
            meta.intent, meta.want_source = found.want, "beat"
            log.info("think_beat: guid=%d want=%s use=%d ms=%d lean=%s", turn.speaker,
                     found.want, found.use is not None, round(elapsed * 1000),
                     lean.value if lean is not None else "none")
        if ctx is None and ask_lands:
            return beats.lines(found, human)
        want = Intent(found.want)
        return beats.render(read=found.read, lands=meta.lands or found.lands, want=want,
                            who=human, topic=topic_for(want, ctx) if ctx is not None else "",
                            use=found.use)

    def _beat_offer(self, turn: Turn, meta: TurnMeta, c: "Composed",
                    now: float) -> list[tuple[str, str]]:
        """What the beat may bring in this turn (beat.offerable): the composed prompt's items
        without what this conversation's ledger says she already brought in, none while she
        brought something in within her last turns there. The turn's ledger key goes on `meta`,
        so her said turn is recorded (_spend). A ledger that cannot be read offers as an empty
        one would."""
        meta.beat_conversation = beats.ledger_key(turn.speaker, turn.channel, turn.to_guid)
        meta.beat_use = None
        keys = list(c.beat_keys) + [""] * (len(c.beat_items) - len(c.beat_keys))
        try:
            entries = beats.ledger(self.store.get_meta(meta.beat_conversation), now)
        except sqlite3.Error:
            entries = []
        return beats.offerable(list(zip(keys, c.beat_items)), entries)

    def _pending_phrases(self, guid: int, now: float) -> tuple[str, ...]:
        """Her common stock phrases waiting to be named on her next turn, within
        PHRASE_PENDING_S."""
        at, named = self._phrase_pending.get(guid, (0.0, ()))
        if named and now - at > PHRASE_PENDING_S:
            self._phrase_pending.pop(guid, None)
            return ()
        return named

    def _settle_phrase_hint(self, turn: Turn, meta: TurnMeta, now: float) -> None:
        """Her turn is said: the phrases its prompt named are done with; those her owed answer
        leaned on (a common stock entry, no rewrite) wait for her next turn
        (`phrase_hint_pending`, a count only)."""
        if meta.phrase_hint and self._phrase_pending.get(turn.speaker, (0.0, ()))[1] == meta.phrase_hint:
            self._phrase_pending.pop(turn.speaker, None)
        if meta.phrase_pending:
            self._phrase_pending[turn.speaker] = (now, meta.phrase_pending)
            log.info("phrase_hint_pending guid=%d n=%d", turn.speaker, len(meta.phrase_pending))

    def _beat_rng(self, turn: Turn, meta: TurnMeta, now: float) -> random.Random:
        """The turn's own seeded draw for the order of the beat's items (life.seed.seeded): the
        night, the member, the minute and the turn."""
        from leeroy_mind.life.seed import seeded
        return seeded("beat_items", night_of(self.clock.local()), turn.speaker, int(now // 60),
                      zlib.crc32(str(meta.turn_id).encode()))

    def _record_beat(self, turn: Turn, meta: TurnMeta, now: float) -> None:
        """Her turn in a conversation whose beat was asked is said: the ledger gains it (what it
        brought in, or nothing), ids and times only; her ledgers there older than LEDGER_TTL go.
        Never costs the line: a failing write is logged by its class."""
        key = meta.beat_conversation
        if not key:
            return
        try:
            entries = beats.ledger(self.store.get_meta(key), now)
            self.store.set_meta(key, beats.recorded(entries, now, meta.beat_use))
            prefix = beats.LEDGER_KEY.format(guid=turn.speaker, conversation="")
            with self.store.transaction():
                stale = [k for k, v in self.store.conn.execute(
                    "SELECT key, value FROM meta WHERE substr(key, 1, ?) = ?",
                    (len(prefix), prefix)) if k != key and not beats.ledger(v, now)]
                self.store.conn.executemany("DELETE FROM meta WHERE key = ?",
                                            [(k,) for k in stale])
        except sqlite3.Error as e:
            log.warning("think_beat_ledger_failed: guid=%d error=%s", turn.speaker,
                        type(e).__name__)

    async def _compose_prompt(self, turn: Turn, meta: TurnMeta, *, live: bool,
                       persona: Persona | None = None, window: list | None = None,
                       trigger_text: str | None = None, dial: int | None = None) -> Composed:
        """`live`: the prompt is for speaking now, so it may draw from the engine's rng and
        hold the guild-wide real-name slot for the turn; what it offered and used goes on
        `meta`, to be spent if a line is said (_spend). A whisper reaches it only as
        whisper_ok allows: the chat window, the speaker's own last lines, the trigger line's
        words and recall all follow that rule.

        His real name reaches an inner-circle member's prompt only on the turn offered it
        (privacy minor 10): any other turn of theirs is redacted like a non-member's, so a
        name said in chat is not mirrored back until the gap allows it again.

        A reply to his game question (meta.facts_line) carries that line's facts from the
        library, under facts_head; they are reference text, never memories.

        The console's tuning preview (preview_line) composes with `persona` (a candidate
        effective persona), `window` (the chat it shows: a line of his given as text, never a
        stored one), `trigger_text` (that line) and `dial` (a candidate romance dial): no
        memories are recalled, and his real name is never offered. Its romance draws come from
        generators seeded by the preview's seed, new for each composition and one each for the
        share's roll, the variants (passage, heat, band overlay) and his whisper overlay, so
        before and after pick alike and differ by the dial, even one that decides whether the
        share is rolled at all."""
        preview = window is not None
        p = persona if persona is not None else self.personas.get(turn.speaker)
        if p is None:
            raise ValueError(f"no persona for {turn.speaker}")
        dial = self.cfg.romance.dial if dial is None else dial
        rng = self.rng if live else self._compose_rng
        if live and getattr(self, "learn_enabled", False):
            from leeroy_mind.learn.recipe import describe
            try:
                meta.recipe_snapshot = describe(turn, p, self.cfg)
            except Exception:
                log.warning("learn_recipe_describe_failed")
        now = self.clock.now()
        human, hg = self.human, self.human.guid
        names = self._names(turn.channel)
        rel_h = self.store.get_relationship(turn.speaker, hg, lens=True) if hg else None
        # his inner circle: his real name is theirs to use, only where they talk with him
        # privately; anywhere else they call him what everyone does (_real_name_private)
        close = knows_real_name(rel_h) and self._real_name_private(turn)
        recent = list(window) if preview else self.world.recent_visible(
            turn.speaker, 8,
            keep=lambda row: row.ts >= self._chat_from and self._may_carry(turn, row))
        quiet = not any(now - r.ts < QUIET_S for r in recent)
        if turn.kind in CHAINED:  # idle talk sees only the last few minutes (rehearsal 2)
            recent = [r for r in recent
                      if now - r.ts < IDLE_WINDOW_S or r.id == turn.trigger_line]
        if turn.kind is TurnKind.OPEN and hg:  # an opener never answers his line (rehearsal 3)
            recent = [r for r in recent if r.from_guid != hg]
        pair = [] if preview else self._pair_lines(turn, recent, now)
        if not preview and meta.outreach_item is not None:
            from leeroy_mind.outreach.context import nudge_lines
            pair = nudge_lines(self.outreach, turn, meta, pair, recent, limit=PAIR_LINES)
        if meta.pushback_line and all(r.id != meta.pushback_line for r in recent):
            # her own line his pushback is about, older than the chat shown or one the bridge
            # reported, which only the log holds (fix wave G6)
            own = [r for r in self.store.chat_lines([meta.pushback_line])
                   if r.ts >= self._chat_from and self._may_carry(turn, r)]
            recent = sorted([*recent, *own], key=lambda r: r.id)

        from_thread = False
        if not preview and self._server_turn(turn, meta) and meta.server_thread:
            from_thread = True  # rows keyed by Discord message ids, their notes already added
            observed = self.live_review.observe(turn, meta)
            _, context = await self.live_review.read(
                lambda conn:self.live_review.history(turn, meta, conn, observed=observed))
            recent = [ChatLineRow(h.id, h.ts, turn.channel, h.guid, names.get(h.guid, 'someone'),
                                 h.to_guid, h.text, 0, 0, None, h.thread_id, h.origin, h.readers)
                      for h in context]
        dm_rows, answered = False, None  # answered: his DM shown at the chat's end
        if not preview and not from_thread and meta.discord_chat is not None:
            # his Discord DM's history (discord.talk dm_chat): the chat she answers in, in place
            # of the game's window, which never holds a Discord line (2026-11-15: it was packed
            # into the instruction, 200 characters a message, and starved the prompt)
            dm_rows = True
            recent = [r for r in meta.discord_chat if whisper_ok(
                r.channel, r.from_guid, r.to_guid, speaker=turn.speaker,
                turn_channel=turn.channel, to_guid=turn.to_guid)]
            # his DM she answers ends the chat (_his_dm_row): kept out of the copy window, the
            # claim support and the beat's items below, as a trigger line is
            if ((row := self._his_dm_row(turn, meta)) is not None
                    and all(r.id != row.id for r in recent)):
                recent.append(row)
                answered = row.id

        people = [turn.addressee] if turn.addressee else []
        # the addressee and up to 2 others who spoke recently; a whisper is only between two
        room = len(people) + (0 if turn.channel == "whisper" else 2)
        for row in reversed(recent):
            g = row.from_guid
            if len(people) < room and g != turn.speaker and g not in people and g in names:
                people.append(g)
        attitude = p.romance.stance if p.identity.player_gender == "female" else ""
        realism = self.cfg.romance.realism  # [romance] realism: his card's note, stale memories
        pronouns = self._pronoun_tags(turn, people, recent)
        cards = [relationship_card(names.get(g, "someone"),
                                   self.store.get_relationship(turn.speaker, g, lens=True),
                                   is_human=g == hg,
                                   is_guild_master=g == hg and human.is_guild_master,
                                   forms=human.forms if g == hg else THEY_FORMS,
                                   attitude=attitude if g == hg else "",
                                   speaker=p.identity.player_gender,
                                   friendship_only=is_group(turn.channel),
                                   pronoun=pronouns.get(("card", g), ""),
                                   realism=realism and g == hg)
                 for g in people]

        trigger = his = ""  # his: the line of his the turn answers, on any channel ("" none)
        if trigger_text is not None:
            query = trigger = his = trigger_text  # a preview's line is his
        elif turn.trigger_line:
            rows = self.store.chat_lines([turn.trigger_line])
            query = trigger = rows[0].text if rows and self._may_carry(turn, rows[0]) else ""
            his = trigger if trigger and hg and rows[0].from_guid == hg else ""
        elif meta.recall_query:  # his Discord post and its thread, no trigger line (discord.talk)
            query = meta.recall_query
        elif turn.kind is TurnKind.OPEN and meta.topic:
            query = meta.topic  # what she will bring up, not what the chat said
        else:
            query = " ".join(r.text for r in recent[-3:])
        earlier = self._earlier_answers(turn, meta, recent)
        present = list(dict.fromkeys(
            [r.from_guid for r in recent if r.from_guid and r.from_guid != turn.speaker]
            + ([hg] if hg and self.world.human_online else [])))
        from leeroy_mind.bond.policy import eligible as bond_eligible
        bond_options = {'include_bond': True} if self.cfg.romance.activities and bond_eligible(p, self.cfg.romance.dial) else {}
        diverse = None if preview else self._recall_diversity(turn, meta, trigger, live=live)
        aged = {"aged": True} if diverse is not None else {}
        memories = [] if preview else await self.recall.recall(
            turn.speaker, query, present=present, addressee=turn.addressee, k=5,
            priority=turn.priority, touch=False, channel=turn.channel, to_guid=turn.to_guid,
            **bond_options, **({"stale_plans": True} if realism else {}),
            **({"diversity": diverse} if diverse is not None else {}))
        if meta.lore_memory and all(m.id != meta.lore_memory for m in memories):
            # the memory she answers his question from leads, even while it rests
            pinned = self.recall._shareable(self.store.get_memories([meta.lore_memory]),
                turn.speaker, turn.channel, turn.to_guid, **bond_options)
            memories = [*pinned, *memories][:5]

        state = self.store.get_state(turn.speaker, lens=True)
        altered = self._altered_of(turn.speaker)
        local = self.clock.local()
        phase = day_phase(local)
        to_human = bool(hg) and turn.addressee == hg
        if to_human and not his and not preview:
            his = self._his_line(turn, meta)  # his Discord post, DM or carried whisper
        them = human.forms[1] if to_human else "them"
        greet = turn.kind is TurnKind.GREET
        # the greetings of his login a later greeter sees, as who said hi and never the words
        hi = ({r.id for r in recent if r.id in self._greet_lines and r.from_guid != turn.speaker}
              if greet else set())
        follow = ""
        if meta.follow_up and (m := self._follow_up_memory(memories)) is not None:
            follow = FOLLOW_UP.format(
                memory=self.recall.format(m, past=realism, **aged).removeprefix("- "))
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
                and dial_for(p, dial) >= NO_BRAKES_DIAL  # the ruling's dial
                and p.identity.player_gender == "female" and p.romance.stance in ROMANCE_SHARE):
            ends.append(FEELINGS.format(them=them))
        if greet and any(r.from_guid not in (hg, turn.speaker) for r in recent):
            ends.append(TALK_TO.format(name=human.called))
        def draws(what: str) -> random.Random:  # the romance's draws (a preview's: its seed's)
            return (random.Random(f"{meta.seed}:{what}") if preview and meta.seed is not None
                    else rng)
        # at the explicit band in a whisper to him: the owner's overlay, read last, then the
        # distance frame, which her romance text then leaves out, so it is said once
        tail_text = (whisper_tail(p, rel_h, dial=dial, channel=turn.channel,
                                  data_dir=self.data_dir, forms=human.forms, rng=draws("tail"),
                                  **({"realism": True} if realism else {}))
                     if hg and turn.to_guid == hg else "")
        romance = self._romance(turn, meta, p, rel_h, phase, now, draws("roll"), trigger, dial,
                                tail_follows=bool(tail_text), vary=draws("vary"))
        tail = tail_text if romance else ""
        # his explicit overlay in his DM (the band overlay in her romance text): the beat drops
        # the same brakes as under the whisper tail (PM log D13, review-340 S1)
        overlaid = bool(tail) or bool(romance and hg and turn.to_guid == hg and dm_explicit(
            p, rel_h, dial=dial, channel=turn.channel, data_dir=self.data_dir))
        two = False
        if romance:  # a line or two, unless the turn holds one (her answer to his pushback)
            two = turn.max_lines > 1 and explicit_whisper(p, rel_h, dial=dial,
                                                          channel=turn.channel)
        meta.two_lines = two
        real = ""
        if (close and not preview and self._human_in_talk(turn, recent)
                and self._real_name_hold is None
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

        # what she already did with this person lately ("Tonight you've already..."): after
        # the instruction's extra, never cut
        recent_moves = self._recent_moves(turn)
        after = moves_line(recent_moves,
                           night_start=night_bounds(night_of(local), local.tzinfo)[0],
                           forms=self._forms_of(self._counterpart(turn)))
        # an answer to his question, on any channel: answer it first (asking.QUESTION_FIRST,
        # in place of the hint's "do something else"), unless another member already answered
        # it (`seen` then says to add what they didn't) or it is his pushback (its own
        # instruction already says to answer him plainly); a question or a request leads the
        # beat's answering wants (Composed.asking)
        his = his if to_human else ""
        if his and is_question(his) and not before and not meta.pushback_line:
            after = question_first(after, human.forms[2])
        if live and not preview:  # her last owed answer's common stock phrases, named now
            named = self._pending_phrases(turn.speaker, now)
            if named:
                meta.phrase_hint = named
                after = " ".join(x for x in (after, phrase_hint(named)) if x)
        # [talk] situation (core plan Task 8): the hard rules, Right now by the instruction, and
        # every situation template's length from here (situation_length)
        situation = getattr(self.cfg.talk, "situation", False) is True
        words = self._discord_words(turn)
        instruction = render_instruction(
            turn.kind, addressee=names.get(turn.addressee, "") if turn.addressee else "",
            topic=meta.topic, lead=romance or "",
            extra=" ".join(x for x in (turn.instruction, follow, seen, story, lore, quest,
                                       *ends) if x),
            forms=human.forms if to_human else THEY_FORMS, after=after, tail=tail, long=two,
            quiet=quiet, to_him=to_human and turn.kind is TurnKind.OPEN,
            channel=turn.channel, discord_words=words,
            length_text=situation_length(turn.kind, channel=turn.channel, words=words,
                                         typing=p.typing, two_lines=two) if situation else "",
            situation=situation, typing=p.typing)
        # her day ([life] availability above 0: her own clock and where she is; never in a raid
        # call) and her feeling ([life] state; how she feels about him only on a turn of his)
        today, where = day_line(local, p.identity.region), self._where_line(turn, meta)
        if self._life_on() and meta.life_presence is not None:
            today = day_line(local_time(local, p.identity.region), p.identity.region)
        feels = (life_state.words(meta.state, to_him=self._aimed_at_him(turn), forms=human.forms)
                 if meta.state is not None else "")
        rules_of = situation_rules if situation else comms_rules
        chat_notes, pair_notes = {}, {}
        if getattr(self, 'photos_send', False):
            from leeroy_mind.photos.notes import by_lines
            pair_notes = by_lines(self.store.conn, [(r.id, r.channel) for r in pair])
            if not from_thread and not dm_rows:  # those rows carry their notes already
                chat_notes = by_lines(self.store.conn, [(r.id, r.channel) for r in recent])
        parts = PromptParts(
            card=self._card(p), samples=samples,
            rules=rules_of(human, p, data_dir=self.data_dir, now=now,
                           channel=turn.channel, recipient=turn.to_guid,
                           at_him=self._aimed_at_him(turn),
                           with_him=self._human_in_talk(turn, recent)), rel_cards=cards,
            memories=[self.recall.format(m, past=realism, **aged) for m in memories],
            mood=mood_line(state, altered, substance_of(p, altered, local), phase,
                           kind=turn.kind, life_thread=p.real_life.life_thread,
                           lately=self._lately_line(turn.speaker, now),
                           variant=random.Random(f"{p.guid}:{night_of(local)}:mood")
                           .randrange(1000), today=today, where=where, state_words=feels),
            chat=[said_hi_text(r, names, human.called, now=now) if r.id in hi
                  else chat_line_text(r, names, now=now, photo=chat_notes.get(r.id,''),
                                      pronoun=pronouns.get(("line", r.id), ""))
                  for r in recent],
            # her own long lines (a Discord message) as their opening words; the repeat check
            # reads them whole (_generate)
            own_recent=[replay(x) for x in self._own_lines(turn)], instruction=instruction,
            elsewhere=[chat_line_text(replace(r, text=replay(r.text)) if r.from_guid == turn.speaker
                                      else r, names, now=now, photo=pair_notes.get(r.id,''))
                       for r in pair],
            real_name=real, facts=list(facts),
            facts_head=facts_head(human.forms[1]) if facts else "",
            pinned=pinned if facts else None, kept=kept, situation=situation)
        if live and getattr(self, 'learn_enabled', False):
            from leeroy_mind.learn.apply import prepare, yield_families
            meta.learning = await prepare(self, turn, meta, p, now=now)
            if meta.learning is not None and meta.intent:
                yield_families(meta.learning, {"tease"})  # her want says whether she teases
            if meta.learning is not None:
                parts.style = [entry[2] for entry in meta.learning.entries]
        if self.cfg.talk.aware or meta.review_required:
            try:
                parts.awareness = await self.live_review.block_async(turn,meta,close=bool(real))
            except Exception:
                log.warning("awareness_card_failed guid=%d",turn.speaker)
                if meta.review_required:
                    raise ValueError("awareness_unavailable") from None
        trimmed = trim_to_budget(redact_for(parts, human, bool(real)),
                                 budget=self._prompt_budget(turn) - (
                                     self._block_reserve(turn, meta) if live else 0))
        if meta.learning is not None:
            from leeroy_mind.learn.apply import used
            used(meta, len(trimmed.style))
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
                     if (not turn.trigger_line or r.id < turn.trigger_line) and r.id != answered),
                   *(r.text for r in pair),
                   *([follow] if follow else []),
                   *([turn.instruction] if turn.kind in (TurnKind.BIT, TurnKind.ASIDE)
                     else [])]
        callback = (meta.learning.callback if meta.learning is not None and
                    any(k == 'callback' for k,_,_ in meta.learning.used) else None)
        if callback is not None:
            support.append(callback.seed)
            meta.memory_ids = list(dict.fromkeys([*meta.memory_ids, callback.memory_id]))
        messages = render_messages(trimmed)

        def plain(text: str) -> str:  # a chat line as a beat item, redacted as redact_for does
            text = strip_wow_markup(text)
            return text if real or not human.real_name else redact_real_name(text, human,
                                                                            human.called)
        shown = memories[:len(trimmed.memories)]
        beat_offer = beats.keyed_items(
            [(f"m{m.id}" if m.id else "", beats.unframed(x))
             for m, x in zip(shown, trimmed.memories)],
            [(f"c{r.id}" if r.id else "", plain(r.text)) for r in recent[:-1]
             if r.from_guid != turn.speaker and r.id != turn.trigger_line
             and r.id != answered])
        return Composed(messages,
                        list(dict.fromkeys([m.id for m in memories[:len(trimmed.memories)]] +
                             ([callback.memory_id] if callback else []))), p, close,
                        [trigger] if trigger else [], [r.text for r in earlier], samples,
                        # a DM's copy check reads his earlier messages there (her own are the
                        # repeat check's): the audit's answer that sent his message back
                        support, [r.text for r in recent
                                  if r.from_guid == hg and r.id != answered] if dm_rows
                        else [r.text for r in recent if r.id != turn.trigger_line],
                        [*trimmed.memories, *trimmed.facts, *([callback.seed] if callback else [])], moves=recent_moves,
                        # as plain text (no age, speaker or bullet): the shown memories, then the
                        # others' earlier lines, never hers nor the line she answers
                        beat_items=[x for _, x in beat_offer],
                        tail=overlaid, beat_keys=[k for k, _ in beat_offer],
                        asking=bool(his) and asks(his))

    @staticmethod
    def _prompt_budget(turn: Turn) -> int:
        """The input budget of the turn's prompt: DM_BUDGET in a Discord DM (its history in
        Chat, a reply of up to dm_words), LIVE_BUDGET everywhere else."""
        return DM_BUDGET if turn.channel.startswith(DM_PREFIX) else LIVE_BUDGET

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

    def _recall_diversity(self, turn: Turn, meta: TurnMeta, trigger: str, *, live: bool):
        """[talk] recall_diversity (memory/diversity.py, read at each turn): what its rules need
        from this turn, or None with the switch off (recall and its lines exactly as before). His
        line is the line the turn answers when it is his, or his post or whisper a Discord answer
        recalls on (`recall_query` of a turn that answers him). A live turn's recipe records the
        rules' version (learn.recipe.recall_used)."""
        if getattr(self.cfg.talk, "recall_diversity", False) is not True:
            return None
        from leeroy_mind.memory.diversity import VERSION, Diversity, name_words
        hg = self.human.guid
        his = ""
        if hg and turn.trigger_line and trigger:
            rows = self.store.chat_lines([turn.trigger_line])
            his = trigger if rows and rows[0].from_guid == hg else ""
        elif hg and meta.answers_human and meta.recall_query:
            his = meta.recall_query
        if live and getattr(self, "learn_enabled", False):
            from leeroy_mind.learn.recipe import recall_used
            try:
                recall_used(meta, VERSION)
            except Exception:
                log.warning("learn_recipe_describe_failed")
        return Diversity(human=hg or 0, his_line=his, names=name_words(self._protect(turn.channel)))

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
                    guards: Guards | None = None, after: str = "", topic: str = "",
                    still_valid: Callable[[], bool] | None = None) -> list[str]:
        """Speak one turn (after turn.delay) through the whole pipeline: the lines sent, or []
        when it was dropped (the reason is in the utterance log). The caller's Turn is left as
        it was: the pipeline fills in its own copy's priority, deadline and channel. The
        keywords go on its TurnMeta."""
        return await self._speak(replace(turn), TurnMeta(
            self._new_id(), why=why, elsewhere=elsewhere, guards=guards, after=after, topic=topic,
            still_valid=still_valid))

    async def speak_meta(self, turn: Turn, meta: TurnMeta) -> list[str]:
        """Await a source-owned turn, retaining its receipt id and cancellation fence."""
        if not meta.turn_id:
            meta.turn_id = self._new_id()
        if self._server_turn(turn, meta):
            meta.review_required = meta.single_line = meta.keep_channel = True
        return await self._speak(replace(turn), meta)

    def _server_turn(self, turn: Turn, meta: TurnMeta) -> bool:
        app = getattr(self, 'server_app', None)
        return (getattr(app, 'server', None) is not None and is_discord(turn.channel)
                and meta.why.startswith('server:'))

    async def say_as(self, guid: int, channel: str, text: str, *, kind: TurnKind,
                     to_guid: int = 0,
                     still_valid: Callable[[], bool] | None = None,
                     before_transport: Callable[[], bool] | None = None,
                     on_recorded: Callable[[int, Ack], None] | None = None) -> bool:
        """The template path (P1d): guards, raid rules and the member's typing apply, the text
        is used as given and no model is asked. True when a line really went out, to the game
        or to the voice service: one left in a room he is not in did not. A sender's
        before_transport barrier makes the turn one typed line, sent unstyled (TurnMeta)."""
        turn = Turn(kind=TurnKind(kind), speaker=guid, channel=channel, to_guid=to_guid,
                    template=text)
        if before_transport is not None:
            turn.max_lines = 1
        meta = TurnMeta(self._new_id(), why="say_as", still_valid=still_valid,
                        before_transport=before_transport, on_recorded=on_recorded)
        said = await self._speak(turn, meta)
        return bool(said) and not (self.comms is not None and self._comms_says(
            "unheard", False, turn.channel, meta))

    async def _delay(self, turn: Turn, meta: TurnMeta) -> None:
        """Wait out turn.delay on the clock, or until meta.wake is set (hurry). Without a wake
        (every turn at availability 0) it is the clock's plain sleep, as before."""
        delay = turn.delay * self.wait_scale
        if meta.wake is None:
            await self.clock.sleep(delay)
            return
        task = asyncio.current_task()
        meta.wake_at = self.clock.now() + max(0.0, delay)
        if meta.wake.is_set() or delay <= 0:
            await self.clock.sleep(0)
            return
        self._delaying[task] = turn
        sleep = asyncio.ensure_future(self.clock.sleep(delay))
        wake = asyncio.ensure_future(meta.wake.wait())
        try:
            await asyncio.wait({sleep, wake}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            sleep.cancel()
            wake.cancel()
            if self._delaying.get(task) is turn:
                del self._delaying[task]

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
        if not meta.planned_at:  # a turn spoken without _queue (an opener to him, a DM opener)
            meta.planned_at = self.clock.now()
        self._mark_borrow(meta)
        self._answer_queued(turn, meta)
        self._admit_timing(meta)
        self.timing.dequeued(meta.timing_trace)
        task = asyncio.current_task()
        self._current[task] = turn
        outcome = "completed"
        try:
            return await self._speak_now(turn, meta)
        except asyncio.CancelledError:
            outcome = "cancelled"
            if not meta.settled:  # else the line at the bridge already has its row
                self._drop(turn, meta, "cancelled")
            raise
        except Exception:
            outcome = "error"
            raise
        finally:
            self._release_photo(meta, outcome)
            if meta.outreach_item is not None:
                self.outreach.release(meta)
            self.timing.pipeline_done(meta.timing_trace, outcome)
            if self._current.get(task) is turn:
                del self._current[task]
            self._phase(turn, None)  # sent, dropped, cancelled or raised: nobody is typing
            if self._real_name_hold == meta.turn_id:  # ended without its line: free the slot
                self._real_name_hold = None
            if meta.learning is not None:
                from leeroy_mind.learn.apply import release
                release(self, meta)

    async def _speak_now(self, turn: Turn, meta: TurnMeta) -> list[str]:
        if getattr(getattr(self.scheduler, 'gpu_gate', None), 'paused', False):
            return []
        if not _scope_valid(meta):
            return self._drop(turn, meta, "scope_changed")
        with self.timing.span(meta.timing_trace, "delay"):
            await self._delay(turn, meta)
        if not _scope_valid(meta):
            return self._drop(turn, meta, "scope_changed")
        now = self.clock.now()
        rule = self._rule_priority(turn, meta)
        turn.priority = (Priority.BACKGROUND if self._server_turn(turn, meta) else
                         rule if meta.elsewhere else min(turn.priority, rule))
        if turn.deadline <= 0:
            turn.deadline = now + (DISCORD_DEADLINE_S if is_discord(turn.channel)
                                   else DEADLINE_S.get(turn.priority, OTHER_DEADLINE_S))
            if owes_him(self, turn, meta):  # by the answers to him in flight (talk/owed.py)
                extend_owed_deadline(self, turn, meta, now)
            self._mark_borrow(meta, reset=True)
        if not turn.channel:
            turn.channel = choose_channel(turn.kind, speaker=turn.speaker, world=self.world,
                                          raid=self.raid, now=now, rng=self.rng) or ""
            if not turn.channel:
                return self._drop(turn, meta, "no_channel")
        discord = is_discord(turn.channel)
        if discord:  # one message of up to three typed lines; a DM is always to him
            turn.max_lines = 1 if meta.single_line or self._server_turn(turn, meta) else DISCORD_LINES
            self._dm_to_him(turn)
        p = self.personas.get(turn.speaker)
        if p is None:
            return self._drop(turn, meta, "no_persona")
        offer = getattr(self, '_photo_offer', None)
        if (discord and turn.template is None and meta.before_transport is None
                and not meta.photo_asked and offer is not None):
            meta.photo_asked = True
            try:
                meta.photo = offer(turn, meta, now)
            except Exception:
                log.warning('photo_offer_failed')
            if meta.photo is not None:
                if meta.outreach_item is not None:
                    self.outreach.unbind(meta)  # her photo is this turn's news; the item waits
                turn.max_lines = 1
                meta.single_line_before_photo = meta.single_line
                meta.single_line = True
                if meta.why == 'server:dm':
                    # The real image is now this opener's subject. Its caption need not
                    # refer to the idle/life topic chosen before the photo was offered.
                    meta.server_anchor = meta.photo.snap.scene['shows']
        if meta.photo_seed is not None and meta.photo is None:
            return self._drop(turn, meta, 'photo_unavailable')
        voiced = self._route(turn, meta)
        if self._follows_silence(meta):
            return self._drop(turn, meta, "after_dropped")
        if voiced:
            with self.timing.span(meta.timing_trace, "hold"):
                await self._hold(turn)
        if (meta.initiates and not meta.his_talk_waited and not meta.why.startswith(KEEP_WHISPERS)
                and answers_in_flight(self)):
            # a conversation starter waits for the answers to him on their way (review-327
            # P2-2), once, HIS_TALK_WAIT_S at most; dropped below only if he spoke after it was
            # planned and an answer is still in flight
            meta.his_talk_waited = True
            with self.timing.span(meta.timing_trace, "hold"):
                started = self.clock.now()
                until = started + HIS_TALK_WAIT_S
                while answers_in_flight(self) and self.clock.now() < until:
                    await self.clock.sleep(2.0)
            if not _scope_valid(meta):
                return self._drop(turn, meta, "scope_changed")
            turn.deadline += self.clock.now() - started  # the wait is not its own time
            self._mark_borrow(meta, reset=True)
        why = self._blocked(turn, meta, self.clock.now(), first=True)
        if why:
            return self._drop(turn, meta, why)
        memory_ids: list[int] = []
        c = None
        text = turn.template
        if text is not None and getattr(self, "learn_enabled", False):
            from leeroy_mind.learn.recipe import describe
            try:
                meta.recipe_snapshot = describe(turn, p, self.cfg)
            except Exception:
                log.warning("learn_recipe_describe_failed")
        if text is None:
            original_instruction = turn.instruction
            self._phase(turn, COMPOSING)
            try:
                with self.timing.span(meta.timing_trace, "compose"):
                    for hook in tuple(self._instruction_hooks):
                        previous = turn.instruction
                        try:
                            instruction = hook(turn, meta, previous)
                            if not isinstance(instruction, str):
                                raise TypeError("instruction hook must return text")
                            turn.instruction = instruction
                        except TurnWithdrawn as e:
                            return self._drop(turn, meta, e.reason)
                        except Exception:
                            turn.instruction = previous
                            log.warning("instruction_hook_failed")
                    if meta.photo is not None:
                        turn.instruction += '\n' + meta.photo.instruction
                    self._draw_situation(turn, meta, p, now)
                    c = await self._compose(turn, meta, live=True)
            except ValueError as e:  # a turn the instruction can't be written for
                log.warning("talk_bad_turn: guid=%d kind=%s: %s", turn.speaker, turn.kind, e)
                return self._drop(turn, meta, "bad_turn")
            finally:
                # A reusable mutable Turn must not retain facts from a previous
                # composition, even if a later lookup has no matching record.
                turn.instruction = original_instruction
            if not _scope_valid(meta):
                return self._drop(turn, meta, "scope_changed", c.memory_ids)
            memory_ids, close = c.memory_ids, c.close
            text, why = await self._generate(turn, meta, c, spoken=voiced)
            if why in OWED_RESCUABLE and _scope_valid(meta):  # his answer, asked once more
                text, why = await rescue_text(self, turn, meta, c, why, text, spoken=voiced)
            self._phase(turn, None)  # its text exists
            if not _scope_valid(meta):
                return self._drop(turn, meta, "scope_changed", memory_ids, text=text)
            if why:
                return self._drop(turn, meta, why, memory_ids, text=text)
            if voiced:  # the model takes time: the service may be gone, the speaker moved on
                voiced = self._route(turn, meta)
        else:
            hg = self.human.guid
            close = (bool(hg) and knows_real_name(self.store.get_relationship(turn.speaker, hg, lens=True))
                     and self._real_name_private(turn))
        if meta.photo is not None and not caption_ok(text):
            if self._photo_owed(turn, meta):
                return await self._answer_without_photo(turn, meta, 'photo_words')
            return self._drop(turn, meta, 'photo_words', memory_ids)
        text = self._link_items(turn, meta, text)
        if is_group(turn.channel) and any(command.casefold() in ROMANCE_EMOTES
                for command in re.findall(r'(?<!\S)/([A-Za-z]+)(?=[\s.!?,]|$)',text)):
            return self._drop(turn,meta,'command',memory_ids,text=text)
        # an emote is public: a whisper keeps its words (NB6), and so does a Discord line,
        # which has no game to emote in
        if turn.channel == "whisper" or discord:
            text, emotes = emote_words(text), []
        else:
            text, emotes = split_emotes(text, {m.name.casefold(): g
                                               for g, m in self.world.members.items()})
        # nothing but commands the game would print as text ("/invite arkon"): nothing to say
        only = command_only(text)
        text = self._spell_real_name(text, close=close, offered=meta.offered_real_name)
        if voiced and text.strip() and not only:
            checked = await self._guarded_lines(turn, meta, p, [Styled(text, text)], c,
                                                close=close, spoken=True)
            if not checked:
                return []
            text = checked[0].line
            if meta.guard_rewritten:
                emotes = []
            spoken = await self._speak_voice(turn, meta, p, text, memory_ids)
            if spoken is not None:
                if spoken and meta.intent:
                    self._remember_want(turn, meta)
                if spoken:
                    await self._send_emotes(turn, meta, emotes)
                return spoken
            turn.channel, turn.to_guid, turn.addressee = typed(turn, meta)  # it never got there
            if self._summons_typed_to_him(turn, text):
                return self._drop(turn, meta, "summons", memory_ids, text=text)
        if meta.before_transport is not None:  # its sender's barrier vouches for exactly this text
            lines = [] if only or not text.strip() else [Styled(text, text)]
        else:
            lines = [] if only else self._style(turn, p, text, close=close,
                                                two_lines=meta.two_lines, romance=meta.romance,
                                                realism=self.realism.eligible(self, turn, meta))
        # The typing's ASCII fold can join two "{" (fullwidth braces, a zero-width space between):
        # each line is defused once more as it is kept and sent (talk/items.py).
        lines = [replace(s, line=defuse(s.line), plain=defuse(s.plain)) for s in lines]
        if not lines:
            if emotes:  # a line that was only an emote: the emote goes out (review minor 8)
                await self._send_emotes(turn, meta, emotes)
            return self._drop(turn, meta, "emote_only" if emotes else "command" if only
                              else "empty", memory_ids, text=text)
        lines = await self._guarded_lines(turn, meta, p, lines, c, close=close)
        if not lines:
            return []
        if meta.guard_rewritten:
            emotes = []
        delay_mult = effects_for(self._altered_of(turn.speaker), turn.kind).delay_mult
        plan = None
        if self.realism.eligible(self, turn, meta):
            meta.realism_review_lines = tuple(s.plain for s in lines)
            baseline = [typing_delay(s.line, p.typing.chars_per_sec, delay_mult, self.rng)
                        for s in lines]
            baseline[0] = self._first_typing(turn, baseline[0], meta)
            plan = self.realism.plan(self, turn, meta, p, lines, baseline)
            if not plan.burst:  # a busy plan may retain only the first complete line
                meta.realism_review_lines = meta.realism_review_lines[:len(plan.lines)]
            meta.realism_planned = True
            lines = list(plan.lines)
        sent: list[Styled] = []
        without_photo = ''
        try:
            for i, s in enumerate(lines):
                if i and plan and not self.realism.safe(self, turn):
                    for skipped in lines[i:]:
                        self._drop(turn,meta,"realism_busy",memory_ids,text=skipped.line)
                    break
                typing = (plan.delays[i] if plan else
                          typing_delay(s.line, p.typing.chars_per_sec, delay_mult, self.rng))
                if turn.template is None:
                    self._phase(turn, TYPING)
                with self.timing.span(meta.timing_trace, "typing"):
                    wait = (self._first_typing(turn, typing, meta) if i == 0 else typing
                            ) * self.wait_scale
                    if plan:
                        await self._realistic_sleep(turn, wait)
                    else:
                        await self.clock.sleep(wait)
                if i and plan and not self.realism.safe(self, turn):
                    self._phase(turn, None)
                    for skipped in lines[i:]:
                        self._drop(turn,meta,"realism_busy",memory_ids,text=skipped.line)
                    break
                meta.words = s.plain
                meta.realism_followup = bool(i and plan)
                why = await self._send_line(turn, meta, p, s.line, memory_ids, first=i == 0)
                self._phase(turn, None)  # sent, or refused
                if why in PHOTO_LOST and self._photo_owed(turn, meta):
                    without_photo = why  # nothing is said yet: asked again below
                    break
                if why:
                    if why not in AWAIT_ECHO:  # that row waits for the echo (_settle_unacked)
                        self._drop(turn, meta, why, memory_ids, text=s.line)
                        if plan and i:
                            for skipped in lines[i+1:]:
                                self._drop(turn,meta,why,memory_ids,text=skipped.line)
                    break
                sent.append(s)
            if plan and plan.correction and len(sent) == len(lines) and self.realism.safe(self, turn):
                correction = Styled(plan.correction, plan.correction)
                meta.realism_correction_until = self.clock.monotonic() + 10
                self._phase(turn, TYPING)
                with self.timing.span(meta.timing_trace, "typing"):
                    await self._realistic_sleep(turn, plan.correction_delay)
                if self.realism.safe(self, turn):
                    meta.words = correction.plain
                    meta.realism_correction = True
                    meta.realism_followup = True
                    why = await self._send_line(turn, meta, p, correction.line, memory_ids, first=False)
                    if not why:
                        sent.append(correction)
                    elif why not in AWAIT_ECHO:
                        self._drop(turn, meta, why, memory_ids, text=correction.line)
                else:
                    self._drop(turn,meta,"realism_busy",memory_ids,text=correction.line)
                self._phase(turn, None)
        finally:
            # what was said, before typos and abbreviations: the next prompts and repeats, a
            # line that went out as the turn was cancelled included
            said = lines[:meta.said]
            if said and turn.template is None:
                words = " ".join(s.plain for s in said)
                if not plan:  # realism records each confirmed line before a newer turn composes
                    self._own_of(turn.speaker).append((turn.channel, turn.to_guid, words))
                self._record_moves(turn, words)
        if without_photo:
            return await self._answer_without_photo(turn, meta, without_photo)
        if sent:  # a cancelled turn's follow-ups never start: a fight began or the session ends
            if meta.intent:
                self._remember_want(turn, meta)
            await self._send_emotes(turn, meta, emotes)
            self._after_sent(turn, meta, p, " ".join(s.line for s in sent))
        return [s.line for s in sent]

    async def _guarded_lines(self, turn, meta, p, lines, composed, *, close, spoken=False):
        if not self.live_review.enabled(turn, meta):
            return lines
        async with self.live_review.serial(turn, meta):
            return await self._review_lines(turn,meta,p,lines,composed,close=close,spoken=spoken)

    async def _review_lines(self,turn,meta,p,lines,composed,*,close,spoken=False):
        owed = self.live_review.owed(turn,meta)
        best = None
        slipped = []  # every candidate whose only hard problem was speaking as the other sex
        hard_seen = set()  # the hard problems met, for an owed answer's fresh draft
        def keep(candidate,verdicts):
            nonlocal best
            hard = {c for v in verdicts for c in v.codes} & HARD_CODES
            if hard:
                hard_seen.update(hard)
                if 'echo_parent' not in hard:
                    slipped.append(list(candidate))
                return  # his own words said back, or her as a man, are never the answer he is owed
            severity = {"send":0,"react":1,"rewrite":2,"drop":3}
            worst = max(verdicts,key=lambda v:severity[v.action])
            rank = (severity[worst.action],sum(len(v.codes) for v in verdicts))
            if best is None or rank<best[0]:
                best = (rank,list(candidate),worst.action,tuple(dict.fromkeys(c for v in verdicts for c in v.codes)))
        async def fallback():
            if best is None:
                # Owner, 2026-11-15: every message from him gets a reply. The longest clean part
                # of a draft that slipped, else one fresh draft, before an owed answer drops.
                short = await self._short_answer(turn,meta,p,slipped,close=close,spoken=spoken)
                if short is None:
                    short = await self._fresh_answer(turn,meta,p,composed,hard_seen,close=close,
                                                     spoken=spoken)
                if short is not None:
                    return short
                meta.guard_verdict = "drop"
                return drop("guard_identity" if slipped else "guard_echo")
            meta.guard_verdict = best[2]
            meta.guard_codes = best[3]
            if ' '.join(' '.join(s.plain for s in best[1]).split()) == ' '.join(meta.preference_original.split()):
                meta.preference_original = ''
            return best[1]
        def drop(code):
            return self._drop(turn,meta,code,meta.memory_ids)
        for attempt in range(2):
            if not lines:
                return (await fallback() if owed and (best is not None or hard_seen
                                                      or owes_him(self, turn, meta))
                        else drop("guard_rewrite_failed"))
            verdicts = []
            for line in lines:
                verdict = await self.live_review.check(turn, meta, line.plain)
                verdicts.append(verdict)
                if not _scope_valid(meta) or not self.live_review.valid(turn, meta):
                    return self._drop(turn, meta, "scope_changed")
                if self.scheduler.expired(self._deadline(turn, meta)):
                    return self._drop(turn, meta, "expired")
            keep(lines,verdicts)
            meta.phrase_pending = _pending_of(verdicts)
            for verdict in verdicts:
                meta.guard_codes = verdict.codes
                if verdict.action == "send":
                    continue
                meta.guard_verdict = verdict.action
                if verdict.action == "react" and not owed:
                    await self._guard_react(turn, meta, verdict.reaction)
                    return drop("guard_react")
                if verdict.action == "drop" and not owed:
                    return drop("guard_drop")
                if meta.guard_rewritten and all(v.action == "send" or v.codes and set(v.codes).issubset(PHRASE_CODES)
                                                for v in verdicts):
                    return await self._without_phrases(turn, meta, p, lines, verdicts, owed,
                                                       close=close, spoken=spoken)
                if meta.guard_rewritten or meta.before_transport is not None:
                    if owed:
                        return await fallback()
                    meta.guard_verdict = "drop"
                    return drop("guard_rewrite_refused")
                meta.guard_rewritten = True
                if getattr(self,'learn_capture_preferences',False):
                    meta.preference_original = ' '.join(s.plain for s in lines)[:4001]
                composed = composed or await self._compose(turn, meta, live=True)
                codes = tuple(dict.fromkeys(c for v in verdicts for c in v.codes))
                if set(codes) & HARD_CODES:
                    # never his words or her as a man as the turn the model goes on from (the
                    # identity audit: it reads the last text and continues it)
                    retry = _hinted(composed, fresh_hint(codes))
                else:
                    retry = replace(composed, messages=[*composed.messages,
                        {"role": "assistant", "content": "\n".join(s.plain for s in lines)},
                        {"role": "user", "content": rewrite_hint(
                            verdict.codes, tuple(x for v in verdicts for x in v.phrases))}])
                text, why = await self._generate(turn, meta, retry, spoken=spoken, max_attempts=1)
                if why:
                    if owed:
                        return await fallback()
                    meta.guard_verdict = "drop"
                    return drop("guard_rewrite_failed")
                text = self._spell_real_name(emote_words(self._link_items(turn, meta, text)),
                                            close=close, offered=meta.offered_real_name)
                if command_only(text):
                    return await fallback() if owed else drop("guard_rewrite_failed")
                lines = ([Styled(text, text)] if spoken else
                         self._style(turn, p, text, close=close, two_lines=meta.two_lines, romance=meta.romance))
                lines = [replace(s, line=defuse(s.line), plain=defuse(s.plain)) for s in lines]
                break
            else:
                return lines
        meta.guard_verdict = "drop"
        return (await fallback() if owed and (best is not None or hard_seen
                                              or owes_him(self, turn, meta))
                else drop("guard_rewrite_failed"))

    async def _short_answer(self, turn, meta, p, slipped, *, close, spoken=False):
        """An owed answer whose every candidate spoke as the other sex: of all those candidates,
        the one whose clean part is longest (each sentence that slips left out:
        guards.identity.without_slips; a tie goes to the earlier draft), styled and reviewed once
        more (_reviewed_once), kept when SHORT_ANSWER_WORDS words or more are left. None when
        there is no such answer; a drop's [] when the turn's scope changed meanwhile."""
        from leeroy_mind.guards.identity import without_slips

        def clean(candidate) -> str:
            # line by line: the styling split the draft and may have dropped its periods
            kept = [x for x in (without_slips(s.plain, p.identity.player_gender)
                                for s in candidate) if x]
            return " ".join(x if x[-1] in ".!?" else x + "." for x in kept)
        parts = [clean(c) for c in slipped]
        text = max(parts, key=lambda t: len(t.split()), default="")  # max keeps the first of a tie
        if len(text.split()) < SHORT_ANSWER_WORDS:
            return None
        return await self._reviewed_once(turn, meta, p, text, close=close, spoken=spoken)

    async def _fresh_answer(self, turn, meta, p, composed, codes, *, close, spoken=False):
        """An owed answer's last try before it would drop (owner, 2026-11-15: every message from
        him gets a reply): one fresh generation from the composed prompt, the failed drafts left
        out and what was wrong said at the end of its instruction (review.fresh_hint), reviewed
        once (_reviewed_once). None when there is none to send (no hard problem was met, a
        transport already vouches for its text, the deadline passed, or the draft failed); a
        drop's [] when the turn's scope changed meanwhile."""
        if (not codes or meta.before_transport is not None
                or self.scheduler.expired(self._deadline(turn, meta))):
            return None
        composed = composed or await self._compose(turn, meta, live=True)
        text, why = await self._generate(turn, meta, _hinted(composed, fresh_hint(codes)),
                                         spoken=spoken, max_attempts=1)
        if why:
            return None
        text = self._spell_real_name(emote_words(self._link_items(turn, meta, text)),
                                     close=close, offered=meta.offered_real_name)
        if command_only(text):
            return None
        return await self._reviewed_once(turn, meta, p, text, close=close, spoken=spoken)

    async def _reviewed_once(self, turn, meta, p, text, *, close, spoken=False):
        """`text` styled as the turn's lines and reviewed once: the lines when nothing hard
        (HARD_CODES) is left, else None; a drop's [] when the turn's scope changed meanwhile."""
        lines = ([Styled(text, text)] if spoken else
                 self._style(turn, p, text, close=close, two_lines=meta.two_lines,
                             romance=meta.romance))
        lines = [Styled(defuse(s.line), defuse(s.plain)) for s in lines]
        if not lines:
            return None
        codes, verdicts = [], []
        for line in lines:
            verdict = await self.live_review.check(turn, meta, line.plain)
            if not _scope_valid(meta) or not self.live_review.valid(turn, meta):
                return self._drop(turn, meta, "scope_changed")
            if set(verdict.codes) & HARD_CODES:
                return None
            codes.extend(verdict.codes)
            verdicts.append(verdict)
        meta.guard_codes = tuple(dict.fromkeys(codes))
        meta.phrase_pending = _pending_of(verdicts)
        return lines

    async def _without_phrases(self, turn, meta, p, lines, verdicts, owed, *, close, spoken):
        """The retry still leans on a phrase the cooldown named (guards/phrases.py): cut it and
        send what survives, checked once more, else drop the line. An answer owed to him only
        loses a closing epithet (`epithet_only`: a clause cut can reverse what she said, review
        326), is never dropped for a phrase alone and never dropped `expired` here, its retry
        being in hand (#325): it goes as it was written."""
        named = tuple(dict.fromkeys(x for v in verdicts for x in v.phrases))
        joined = "\n" if is_discord(turn.channel) or turn.kind is TurnKind.BIT else " "
        text = (strip_phrases(joined.join(s.plain for s in lines), named, epithet_only=owed)
                if named else None)
        if text is not None:
            kept = ([Styled(text, text)] if spoken else
                    self._style(turn, p, text, close=close, two_lines=meta.two_lines, romance=meta.romance))
            kept = [replace(s, line=defuse(s.line), plain=defuse(s.plain)) for s in kept]
            checked = []
            for line in kept:
                checked.append(await self.live_review.check(turn, meta, line.plain))
                if not _scope_valid(meta) or not self.live_review.valid(turn, meta):
                    return self._drop(turn, meta, "scope_changed")
                if not owed and self.scheduler.expired(self._deadline(turn, meta)):
                    return self._drop(turn, meta, "expired")
            if kept and all(v.action == "send" for v in checked):
                meta.phrase_pending = _pending_of(checked)
                return kept
        meta.guard_codes = tuple(dict.fromkeys(c for v in verdicts for c in v.codes))
        if owed:
            meta.guard_verdict = "rewrite"
            return lines
        meta.guard_verdict = "drop"
        return self._drop(turn, meta, "guard_phrase_refused", meta.memory_ids)

    async def _guard_react(self, turn, meta, reaction):
        self._release_photo(meta, 'reaction')
        # A private game whisper has no private emote transport. It stays silent.
        if turn.channel == "whisper" or meta.elsewhere or meta.why == "hotmic":
            return
        async with self._timed_send_lock(meta):
            if (not _scope_valid(meta) or not self.live_review.valid(turn, meta)
                    or self._blocked(turn, meta, self.clock.now(), first=True)):
                return
            if is_discord(turn.channel):
                from leeroy_mind.discord.store import react
                from leeroy_mind.guards.review import FILLER, REACTIONS
                target = meta.discord.reply_to if meta.discord else None
                if target is None:
                    return
                try:
                    server = getattr(getattr(self, 'server_app', None), 'server', None)
                    with self.store.transaction() if server is not None else nullcontext():
                        added = react(self.store, target, member=turn.speaker,
                              emoji=reaction if reaction in {*REACTIONS, *(v[1] for v in FILLER.values())}
                              else "👍", now=self.clock.now())
                        if server is not None and added:
                            from leeroy_mind.server.records import BadDayCounter, increment
                            try:
                                increment(self.store, self.clock.local().date().isoformat(), reactions=1)
                            except BadDayCounter:
                                pass  # a broken server budget never takes his reaction away
                except DiscordRefused:
                    return
            else:
                try:
                    ack = await self.send(Emote(id="", guid=turn.speaker,
                        emote="laugh" if reaction == "😄" else "nod", target_guid=meta.emote_target))
                except BridgeError:
                    return
                if not ack.ok:
                    return
            (meta.guards or self.guards).record(meta.turn_id, turn.speaker, self.clock.now(),
                                               turn.kind, initiated=meta.initiates)

    async def _realistic_sleep(self, turn: Turn, seconds: float) -> None:
        """Refresh the dots during long waits without extending the planned duration."""
        remaining = max(0.,seconds)
        while remaining > 0:
            self._phase(turn, TYPING)
            chunk = min(10,remaining)
            await self.clock.sleep(chunk)  # asyncio sleep uses monotonic time
            remaining -= chunk

    def _summons_typed_to_him(self, turn: Turn, text: str) -> bool:
        """A voiced line that falls back to typed chat with him: a summons could stand in the
        room, typed it is checked again (_summons_ok of where it now goes)."""
        hg = self.human.guid
        return (turn.template is None and bool(hg) and self._counterpart(turn) == hg
                and not self._summons_ok(turn) and is_summon(text))

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
        if self.comms is None or meta.before_transport is not None or meta.keep_channel or is_discord(turn.channel):
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
        async with self._timed_send_lock(meta):
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
            self._record_moves(turn, text)
        self._after_sent(turn, meta, p, text)
        return [text]

    async def _send_emotes(self, turn: Turn, meta: TurnMeta,
                           emotes: list[tuple[str, int]]) -> None:
        """The emotes her line typed as commands, after it, once each (MAX_EMOTES), at the
        member named after the command or the turn's own target. A romantic one (kiss, wink,
        cuddle, blush) only ever goes to him or to nobody, and the turn's own target takes
        only a friendly one (fix round 1, review I2). A whisper sends none: an emote is
        public; nor does a Discord line. A refused one is logged."""
        if (turn.channel == "whisper" or is_discord(turn.channel) or meta.elsewhere
                or meta.why == "hotmic"):
            return
        hg = self.human.guid
        for emote, target in emotes[:MAX_EMOTES]:
            romantic = emote in ROMANCE_EMOTES
            target = target or (0 if romantic else meta.emote_target)
            if romantic and target != hg:
                target = 0
            await self.clock.sleep(self.rng.uniform(*EMOTE_AFTER_S))
            if not _scope_valid(meta):
                return
            try:
                ack = await self.send(Emote(id="", guid=turn.speaker, emote=emote,
                                            target_guid=target))
            except BridgeError as e:
                log.info("emote_dropped: guid=%d reason=%s", turn.speaker, type(e).__name__)
                continue
            if not ack.ok:
                log.info("emote_dropped: guid=%d reason=%s", turn.speaker, ack.error)

    def _mark_borrow(self, meta: TurnMeta, *, reset=False) -> None:
        gate = getattr(self, 'live_borrow', None)
        if gate is not None and (reset or meta.borrow_credit is None):
            meta.borrow_credit = gate.mark()

    @staticmethod
    def _deadline(turn: Turn, meta: TurnMeta | None = None) -> float:
        credit = meta.borrow_credit if meta is not None else None
        return credit.deadline(turn.deadline) if credit is not None else turn.deadline

    def _first_typing(self, turn: Turn, typing: float, meta: TurnMeta | None = None) -> float:
        """The first line's typing, capped so it goes out within the turn's deadline: typed
        faster, never dropped, when the model answered in time (rehearsal 2, finding 5: three
        lines the model gave in half a second were lost to typing alone). A longer Discord
        message ([talk] discord_words) types for DISCORD_TYPING_S at most, not minutes."""
        if self._discord_words(turn):
            typing = min(typing, DISCORD_TYPING_S)
        left = self._deadline(turn, meta) - self.clock.now() - SEND_MARGIN_S
        return min(typing, max(MIN_TYPING_S, left)) if turn.deadline > 0 else typing

    def _blocked(self, turn: Turn, meta: TurnMeta, now: float, *, first: bool,
                 deadline: bool = True) -> str | None:
        """Why the turn can't speak now, or None. `deadline`: the turn's deadline counts
        (after its delay); a line the model gave in time is never expired after that."""
        if not _scope_valid(meta):
            return "scope_changed"
        if getattr(getattr(self.scheduler, 'gpu_gate', None), 'paused', False):
            return 'gpu_paused'
        g, w = turn.speaker, self.world
        if meta.elsewhere and not self._comms_says(
                "scene_valid", False, turn, meta.why.startswith("elsewhere:")):
            return "elsewhere_changed"
        if meta.why == "hotmic" and not self._comms_says("hotmic_valid", False, turn):
            return "hotmic_changed"
        if g not in w.online and not is_discord(turn.channel):  # Discord needs no body (R14)
            return "offline"
        if g in w.afk and not is_discord(turn.channel):
            return "afk"
        if g in self._away_now():
            return "away"
        if turn.channel in SPOKEN_CHANNELS and self._is_dead(g, now):
            return "dead"
        if turn.channel == "whisper" and not w.human_online and (
                not meta.whisper_reply or self._life_on()):
            return "human_offline"  # with [life] on, his whisper's answer too (life.on_logout)
        if turn.kind is TurnKind.FLIRT:
            if not self.human.guid or turn.addressee != self.human.guid:
                return "flirt_target"  # flirting is only ever aimed at him (content invariant)
            p = self.personas.get(g)
            if p is None or flirt_chance(p, 1.0) <= 0:
                return "flirt_stance"  # and only from a woman who wants him
        if first and deadline:
            expired = self.scheduler.expired(self._deadline(turn, meta))
            self.timing.deadline_check(meta.timing_trace, "mind_before_generation", expired=expired)
            if expired:
                return "expired"
        if not self.raid.allows(g, turn.kind, now):
            return "raid"
        # a conversation starter he spoke after, while an answer to him is on its way, is
        # dropped: his talk comes first (review-327 P2-2); a whisper the game set off (a kill,
        # his loot) never is
        if (meta.initiates and not meta.why.startswith(KEEP_WHISPERS)
                and his_talk_after(self, meta) and answers_in_flight(self)):
            return "his_talk"
        guards = meta.guards or self.guards
        # a whisper to him that starts a conversation is, like any answer whispered to him or an
        # answer to his Discord DM, read by no one else: the guild's pace and her lines elsewhere
        # never hold it back; nor do they hold back the one answer he is owed for a line of his
        # (owed_answer, talk/owed.py)
        unread = (meta.whisper_reply or (meta.initiates and turn.channel == "whisper")
                  or self._answers_his_dm(turn, meta) or meta.owed_answer
                  or (turn.channel == "whisper" and turn.to_guid == self.human.guid
                      and turn.kind in REPLY_KINDS))
        if not guards.allow(g, turn.kind, now, continuation=not first,
                            reply=unread, pushback=bool(meta.pushback)):
            return "guards"
        if (turn.kind in CHAINED and not (meta.answers_human or meta.initiates)
                and not guards.chain_open(self._chain_key(self._turn_base(turn), now))):
            return "chain"
        return None

    def _life_on(self) -> bool:
        life = getattr(self, "life", None)
        return life is not None and life.availability > 0

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

    async def _ask(self, turn: Turn, messages: list[dict], *,
                   meta: TurnMeta | None = None, attempt: int = 0) -> str:
        # a bit of several rows keeps the rows a blank line separates (prompts lens, minor 9)
        preset = LIVE_BIT if turn.kind is TurnKind.BIT and turn.max_lines > 1 else LIVE
        words = self._discord_words(turn)
        if words:  # a longer Discord message: room for it in this request only
            preset = preset_for(preset, words)
        trace = meta.timing_trace if meta is not None else None

        async def job(llm):
            self.timing.model(trace, attempt, "started")
            self.timing.deadline_check(trace, "mind_model_queue", expired=False)
            outcome = "completed"
            try:
                if not _scope_valid(meta):
                    raise _ScopeChanged("scope_changed")
                if self.live_admission is not None:
                    self.timing.model(trace, attempt, "admission")
                    try:
                        await self.live_admission()
                    finally:
                        self.timing.model(trace, attempt, "started")
                if not _scope_valid(meta):
                    raise _ScopeChanged("scope_changed")
                expired = self.scheduler.expired(self._deadline(turn, meta))
                self.timing.deadline_check(trace, "mind_readiness_admission", expired=expired)
                if expired:
                    raise Expired(turn.deadline)
                options = dict(preset.options)
                if (not is_discord(turn.channel) and meta is not None and meta.learning is not None
                        and meta.learning.num_predict is not None):
                    options['num_predict'] = min(options.get('num_predict', 80), meta.learning.num_predict, 80)
                if meta is not None and meta.seed is not None:  # a tuning preview's
                    options["seed"] = meta.seed
                self.timing.model(trace, attempt, "call")
                model = self.cfg.models.live
                if meta is not None and getattr(self, "learn_enabled", False):
                    from leeroy_mind.learn.recipe import called_model
                    called_model(meta, model, options, beat=bool(meta.beat))
                result = await llm.chat(model, messages, options=options,
                                        think=preset.think, timeout=preset.timeout)
                if meta is not None and getattr(self, "bakeoff_capture", False):
                    self._bakeoff_keep(turn, meta, model, messages, options, preset.think,
                                       result, words)
                return result
            except asyncio.CancelledError:
                outcome = "cancelled"
                raise
            except Expired:
                outcome = "expired"
                raise
            except _ScopeChanged:
                outcome = "scope_changed"
                raise
            except Exception:
                outcome = "error"
                raise
            finally:
                self.timing.model(trace, attempt, outcome)
        if not _scope_valid(meta):
            raise _ScopeChanged("scope_changed")
        self.timing.model(trace, attempt, "queue")
        outcome = "completed"
        try:
            credit = meta.borrow_credit if meta is not None else None
            extra = {'credit': credit} if credit is not None else {}
            result = await self.scheduler.submit(turn.priority, job, deadline=turn.deadline, **extra)
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except Expired:
            outcome = "expired"
            self.timing.decision(trace, "expired", deadline="mind_model_queue")
            raise
        except _ScopeChanged:
            outcome = "scope_changed"
            raise
        except Exception:
            outcome = "error"
            raise
        finally:
            self.timing.model(trace, attempt, outcome)
        text = result.text
        if words and getattr(result.diagnostics, "done_reason", None) == "length":
            # cut off by num_predict: back to its last full sentence (none: as it is)
            cut = trim_to_sentence(text)
            if cut != text:
                log.info("discord_line_trimmed: guid=%d kind=%s words=%d kept=%d", turn.speaker,
                         turn.kind, len(text.split()), len(cut.split()))
            text = cut
        return text

    @staticmethod
    def _bakeoff_keep(turn: Turn, meta: TurnMeta, model, messages, options, think, result,
                      words) -> None:
        """[bakeoff] capture: keep what this call asked and answered on the turn, for _record to
        store once its line is sent. Never raises: capture must not cost the turn anything."""
        try:
            from leeroy_mind.bakeoff.capture import keep, request
            keep(meta, request(model, messages, options, think, result.text,
                               max_lines=turn.max_lines, words=words or 0,
                               two_lines=meta.two_lines,
                               done_reason=getattr(result.diagnostics, "done_reason", None),
                               beat=getattr(meta, "beat", None)))
        except Exception as e:
            log.warning("bakeoff_capture_failed: guid=%d code=%s", turn.speaker, type(e).__name__)

    async def _generate(self, turn: Turn, meta: TurnMeta, c: Composed, *,
                        spoken: bool = False, max_attempts: int = 2) -> tuple[str, str | None]:
        async with self.live_review.serial(turn, meta):
            return await self._generate_serial(turn,meta,c,spoken=spoken,max_attempts=max_attempts)

    async def _generate_serial(self, turn: Turn, meta: TurnMeta, c: Composed, *,
                        spoken: bool = False, max_attempts: int = 2) -> tuple[str, str | None]:
        """The words to style, or a drop reason with the rejected text. What the model copied
        from the prompt's chat frame goes first (strip_copied), and "(nothing)" is looked for
        again after it and after every repair. A fatal or soft tell (an echo of the line it
        answers, a reply far over the speaker's length included) asks once more, with a hint
        that says what was wrong; the retry is taken unless a tell is still fatal. Silence is
        an answer, but in answer to his whisper or his Discord DM it is asked once more
        (_answers_his_whisper, _answers_his_dm); a Discord line of emoji alone is no silence.
        A speaker outside his inner circle has his real name taken out of the words before
        anything else, while it is still capitalised. A `spoken` line is asked for as speech
        (SPOKEN_HINT, M6), last of all, so a retry's hint never pushes it up."""
        p = c.persona
        name, own, his = p.character_name, aliases(p), self._his_names()

        def asking(hint: str = "") -> list[dict]:
            out = _with_hint(c.messages, hint) if hint else c.messages
            return _with_hint(out, SPOKEN_HINT) if spoken else out
        messages, text = asking(), ""
        # her last moves with this person (filters.find_tells), and in her lines to him only,
        # whether a summons may stand: a call to the raid or a word to a member is no summons
        moves = [(r.clause, r.main) for r in c.moves]
        hg = self.human.guid
        summons_ok = self._summons_ok(turn) if hg and self._counterpart(turn) == hg else None
        emoji = is_discord(turn.channel)  # Discord keeps emoji as text: no tell, no repair

        def quiet(t: str) -> bool:  # an emoji-only line is a Discord message (ruling R9)
            return is_silence(t) and not (emoji and _emoji_only(t))
        # An answer he is owed (LiveReview.owed) never ends in nothing (owner, 2026-11-15: every
        # message of his gets a reply that adds something; two checks had dropped her answer
        # when he asked her to leave her husband): a draft with soft tells only is kept and sent
        # when the retry is worse or silent, else owed_fallback asks once more, simply.
        owed = max_attempts > 1 and self.live_review.owed(turn, meta)
        fit = (int(LONG_SLACK * self._word_budget(turn, p, meta.two_lines))
               if owed and emoji else 0)  # an owed Discord answer is cut, never asked again
        kept, why = "", "tells"
        for attempt in range(max_attempts):
            if not _scope_valid(meta):
                return text, "scope_changed"
            asked = self.clock.now()
            try:
                raw = await self._ask(turn, messages, meta=meta, attempt=attempt)
            except _ScopeChanged:
                return text, "scope_changed"
            except Expired:
                return text, "expired"
            except GpuPaused:
                return text, 'gpu_paused'
            except (SchedulerError, LLMError) as e:
                log.warning("talk_llm_failed: guid=%d kind=%s: %s", turn.speaker, turn.kind, e)
                return text, "llm_error"
            finally:
                meta.llm_s = (meta.llm_s or 0.0) + self.clock.now() - asked
            if not _scope_valid(meta):
                return raw, "scope_changed"
            expired = self.scheduler.expired(self._deadline(turn, meta))
            self.timing.deadline_check(meta.timing_trace, "mind_after_generation", expired=expired)
            if expired:
                return raw, "expired"
            silent, tells = quiet(raw), []
            rest_hint = ""
            if not silent:
                text = raw if c.close else redact_real_name(raw, self.human, self.human.called)
                text = strip_copied(text, (name, *own), him=his)
                if meta.pushback:  # the answer to his pushback is one line: the model's first
                    text = text.split("\n", 1)[0].rstrip()
                silent = quiet(text)  # a line that was only a command at him included
            if not silent and fit and len(text.split()) > fit:
                text = fit_words(text, fit)
            if not silent:
                tells = find_tells(text, recent_own=self._own_lines(turn), speaker=name,
                                   answers=c.answers, others=c.others, samples=c.samples,
                                   support=c.support, people=list(self._protect(turn.channel)),
                                   window=c.window, held=c.held, tonight=meta.again_tonight,
                                   recent_moves=moves, summons_ok=summons_ok,
                                   keep_emoji=emoji)
                if len(text.split()) > LONG_SLACK * self._word_budget(turn, p, meta.two_lines):
                    tells.append(Tell("long", False, True))
                if self._flirts_at_a_member(turn, text):
                    tells.append(Tell("member flirt", True))
                if self._invents_game_name(text):
                    tells.append(Tell("game name", False, True))
                # never on an answer to him, which a second miss must not drop
                if (is_discord(turn.channel) and not meta.answers_human
                        and not self.live_review.owed(turn, meta) and self._self_named(text, name)):
                    tells.append(Tell("self name", False, True))
                if meta.phrase_rests is not None and (rest_hint := meta.phrase_rests(text)):
                    tells.append(Tell("phrase: resting", True))
                if not any(t.fatal or (t.soft and (attempt == 0 or t.reason in DROP_ON_RETRY))
                           for t in tells):
                    text = repair(text, speaker=name, aliases=own, him=his, keep_emoji=emoji)
                    if not quiet(text):
                        return text, None
                    silent = True
                elif not kept and not any(t.fatal or t.reason in DROP_ON_RETRY for t in tells):
                    kept = text  # soft tells only: what a retry may still not beat
            if silent:
                dm = self._answers_his_dm(turn, meta)
                named = is_discord(turn.channel) and meta.discord_named and meta.answers_human
                whisper = self._answers_his_whisper(turn)
                if attempt == 0 and (meta.initiates or owed or whisper or dm or named):
                    log.info("say_silent: guid=%d kind=%s asked again", turn.speaker, turn.kind)
                    hint = (SILENT_START_HINT if meta.initiates else SILENT_WHISPER_HINT if whisper
                            else SILENT_DM_HINT)
                    messages = asking(hint.format(
                        human=self.human.called,
                        length=(self._silent_length(self._discord_words(turn)) if self._discord_words(turn)
                                else "a line or two" if meta.two_lines else "a line")))
                    continue
                if not owed:
                    return raw, "silent"
                text, why = raw, "silent"  # never silence for an answer he is owed: below
                break
            log.info("say_tells: guid=%d kind=%s attempt=%d tells=%s", turn.speaker, turn.kind,
                     attempt, ",".join(t.reason for t in tells))
            messages = asking(" ".join(x for x in (self._retry_hint(tells, p, turn), rest_hint)
                                       if x))
        if owed:
            if kept and not quiet(best := repair(kept, speaker=name, aliases=own, him=his,
                                                 keep_emoji=emoji)):
                log.info("say_owed_kept: guid=%d kind=%s", turn.speaker, turn.kind)
                return best, None
            line = await self.owed_fallback(turn, meta, c, spoken=spoken)
            if line:
                log.info("say_owed_fallback: guid=%d kind=%s", turn.speaker, turn.kind)
                return line, None
        return text, why

    def owed_clean_draft(self, turn: Turn, meta: TurnMeta, c: Composed,
                         raw: str) -> tuple[str | None, list[str]]:
        """A draft of an owed answer against the hard screens only (OWED_HARD_TELLS, a flirt at a
        member, silence): its words repaired, or None with what refused it."""
        p = c.persona
        name, own, his = p.character_name, aliases(p), self._his_names()
        emoji = is_discord(turn.channel)

        def quiet(t: str) -> bool:
            return is_silence(t) and not (emoji and _emoji_only(t))
        if not raw or quiet(raw):
            return None, ["silent"]
        text = raw if c.close else redact_real_name(raw, self.human, self.human.called)
        text = strip_copied(text, (name, *own), him=his)
        if meta.pushback:
            text = text.split("\n", 1)[0].rstrip()
        if quiet(text):
            return None, ["silent"]
        hg = self.human.guid
        summons_ok = self._summons_ok(turn) if hg and self._counterpart(turn) == hg else None
        tells = find_tells(text, answers=c.answers, held=c.held, samples=c.samples,
                           summons_ok=summons_ok, keep_emoji=emoji)
        refused = list(dict.fromkeys(t.reason for t in tells if t.reason in OWED_HARD_TELLS))
        if self._flirts_at_a_member(turn, text):
            refused.append("member flirt")
        if refused:
            return None, refused
        text = repair(text, speaker=name, aliases=own, him=his, keep_emoji=emoji)
        return (None, ["silent"]) if quiet(text) else (text, [])

    def owed_late_draft(self, turn: Turn, meta: TurnMeta, c: Composed, raw: str) -> str | None:
        """A draft that arrived after its deadline (owed.rescue_text): the hard screens
        (owed_clean_draft) and then the kept-draft rule of _generate (review-327-r2 P3-N6): a
        fatal tell, or one a retry may not keep (DROP_ON_RETRY), refuses it."""
        text, _ = self.owed_clean_draft(turn, meta, c, raw)
        if not text:
            return None
        p = c.persona
        hg = self.human.guid
        summons_ok = self._summons_ok(turn) if hg and self._counterpart(turn) == hg else None
        tells = find_tells(text, recent_own=self._own_lines(turn), speaker=p.character_name,
                           answers=c.answers, others=c.others, samples=c.samples,
                           support=c.support, people=list(self._protect(turn.channel)),
                           window=c.window, held=c.held, tonight=meta.again_tonight,
                           recent_moves=[(r.clause, r.main) for r in c.moves],
                           summons_ok=summons_ok, keep_emoji=is_discord(turn.channel))
        if self._invents_game_name(text):
            tells.append(Tell("game name", False, True))
        if any(t.fatal or t.reason in DROP_ON_RETRY for t in tells):
            log.info("owed_late_draft_refused: guid=%d tells=%s", turn.speaker,
                     ",".join(sorted({t.reason for t in tells if t.fatal or t.reason in DROP_ON_RETRY})))
            return None
        return text

    async def owed_fallback(self, turn: Turn, meta: TurnMeta, c: Composed, *,
                            spoken: bool = False) -> str | None:
        """The last answer to a message of his when no draft could stand (owner, 2026-11-15:
        every message of his gets a reply that adds something): one more short generation on
        the turn's own prompt, told only to answer what he just said (OWED_FALLBACK_HINT), and
        reviewed only for silence and the hard screens (OWED_HARD_TELLS: talk of AI or prompts,
        a flirt at a member, his words said back, asking him over, other people's lines, a list,
        an *action*, a sample line copied; owed_clean_draft). A refused or silent one is asked
        once more with what was wrong in words (OWED_RETRY_HINT). Its words, repaired, or None
        when even that fails: the turn is then dropped and talk/owed.py's watchdog holds the
        message and tries a rescue generation again later. Never a canned line (review-327
        P2-1). The one fallback the lanes share."""
        they, them, their = self.human.forms
        hint = OWED_FALLBACK_HINT.format(human=self.human.called)
        for attempt in range(2):
            messages = _with_hint(c.messages, hint)
            if spoken:
                messages = _with_hint(messages, SPOKEN_HINT)
            asked = self.clock.now()
            try:
                raw = await self._ask(turn, messages, meta=meta, attempt=2 + attempt)  # untimed
            except (_ScopeChanged, GpuPaused):
                return None
            except (Expired, SchedulerError, LLMError):
                return None
            finally:
                meta.llm_s = (meta.llm_s or 0.0) + self.clock.now() - asked
            if not _scope_valid(meta) or self.scheduler.expired(self._deadline(turn, meta)):
                return None
            text, refused = self.owed_clean_draft(turn, meta, c, raw)
            if text:
                return text
            log.info("say_owed_fallback_refused: guid=%d kind=%s attempt=%d tells=%s",
                     turn.speaker, turn.kind, attempt, ",".join(refused))
            hint = OWED_RETRY_HINT.format(
                human=self.human.called,
                reasons="; ".join(OWED_REFUSED[r].format(human=self.human.called, they=they,
                                                         them=them, their=their)
                                  for r in refused))
        return None

    def _answers_his_whisper(self, turn: Turn) -> bool:
        """The turn answers a line of his in a whisper to him: its silence is asked again once
        (SILENT_WHISPER_HINT); anywhere else silence is an answer."""
        hg = self.human.guid
        return (bool(hg) and turn.channel == "whisper" and turn.to_guid == hg
                and turn.kind in REPLY_KINDS and bool(turn.trigger_line))

    def _answers_his_dm(self, turn: Turn, meta: TurnMeta) -> bool:
        """The turn answers his message in a Discord DM (discord.talk): like an answer to his
        whisper, its first silence is asked again (SILENT_DM_HINT) and the guards' pace never
        holds it back."""
        return (bool(self.human.guid) and turn.channel.startswith(DM_PREFIX)
                and turn.kind in REPLY_KINDS and meta.answers_human)

    def _flirts_at_a_member(self, turn: Turn, text: str) -> bool:
        """The output screen for the content invariant (prompts I10), on every turn, his
        included (rehearsal 2, judge B 5: turns to him were never screened): romance, desire
        or innuendo aimed at a guildmate (filters.member_flirt, the re-review's rule: on his
        turns a rival may be mentioned, and only desire spoken to a member is caught). A
        FLIRT turn's line that opens on a member and says nothing of him is no flirt of his
        either (filters.aimed_off_him: "happy birthday dunna! if i was there i'd make you a
        cake"). Either is asked again with a hint, then dropped, and so sets off no aside."""
        hg = self.human.guid
        members = [m.name for g, m in self._members_for(turn.channel).items() if g != hg]
        his = self._his_names()
        p = self.personas.get(turn.speaker)
        if member_flirt(text, members, to_member=self._to_member(turn), human=his,
                        to_him=self._aimed_at_him(turn),
                        winks=p is not None and p.identity.player_gender == "female"):
            return True
        return turn.kind is TurnKind.FLIRT and aimed_off_him(text, members, his)

    def _invents_game_name(self, text: str) -> bool:
        """The line names a boss or battleground the library does not have, or puts a
        battleground in a zone (talk/gamenames.py); never without the library."""
        items = getattr(self, "items", None)
        try:
            places = items.places() if items is not None else frozenset()
        except Exception:
            places = frozenset()
        if misplaced_battleground(text, places):
            return True
        known = getattr(getattr(getattr(self, "facts", None), "library", None), "known_words", None)
        if not callable(known):
            return False
        people = [m.name for m in self.world.members.values()] + self._his_names()
        cached = getattr(self, "_place_words", None)
        if cached is None or cached[0] is not places:  # the places' words, squashed, once
            cached = self._place_words = (places, squashed(places))
        return bool(unknown_bosses(text, known, people, cached[1]))

    def _self_named(self, text: str, name: str) -> bool:
        """_names_self, once the guild's own terms that carry her name (lexicon: running jokes,
        slang) are set aside; they are read only when her name is there at all."""
        if not _names_self(text, name):
            return False
        try:
            labels = tuple(r[0] for r in self.store.conn.execute(
                "SELECT term FROM lexicon WHERE instr(lower(term), lower(?)) > 0 LIMIT 50", (name,)))
        except sqlite3.OperationalError:
            labels = ()
        return _names_self(text, name, labels=labels)

    def _his_names(self) -> list[str]:
        h = self.human
        return [x for x in (h.called, h.character_name, h.nickname) if x]

    def _to_member(self, turn: Turn) -> bool:
        hg = self.human.guid
        return (turn.addressee is not None and turn.addressee != hg
                and turn.addressee in self._members_for(turn.channel))

    def _aimed_at_him(self, turn: Turn) -> bool:
        """The turn is his: addressed to him, a whisper to him, or a FLIRT (only ever at him)."""
        hg = self.human.guid
        return bool(hg) and (turn.addressee == hg or turn.kind is TurnKind.FLIRT
                             or (turn.channel == "whisper" and turn.to_guid == hg))

    def _retry_hint(self, tells: list[Tell], p: Persona, turn: Turn | None = None) -> str:
        reasons = {t.reason for t in tells if t.fatal or t.soft}
        repeated = {r.removeprefix(MOVE_REPEAT) for r in reasons if r.startswith(MOVE_REPEAT)}
        unhinted = {r for r in reasons - _HINTED if not r.startswith(MOVE_REPEAT)}
        words = 0 if turn is None else self._discord_words(turn)
        hints = ([(DISCORD_REGEN_HINT if words else REGEN_HINT).format(name=p.character_name)]
                 if unhinted else [])
        forms = dict(zip(("they", "them", "their"), THEY_FORMS if turn is None
                         else self._forms_of(self._counterpart(turn))))
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
        if reasons & {"repeat", "near repeat"} or "clause" in repeated:
            hints.append(REPEAT_HINT)
        for move in sorted(repeated - {"clause"}):
            hints.append(MOVE_HINT.format(move=MOVE_PHRASES[move].format(**forms)))
        if "summons" in reasons:
            hints.append(SUMMONS_HINT.format(**forms))
        if "sample" in reasons:
            hints.append(SAMPLE_HINT)
        if "history" in reasons:
            hints.append(HISTORY_HINT)
        if "slang" in reasons:
            hints.append(SLANG_HINT)
        if "game name" in reasons:
            hints.append(GAME_NAME_HINT)
        if "self name" in reasons:
            hints.append(SELF_NAME_HINT.format(name=p.character_name))
        if "long" in reasons and words:
            hints.append(DISCORD_LONG_HINT.format(words=words))
        elif "long" in reasons:
            lo, hi = p.typing.words_per_line
            hints.append(LONG_HINT.format(lo=lo, hi=hi))
        return " ".join(hints)

    def _discord_words(self, turn: Turn) -> int:
        """The words a longer Discord message of this turn's holds (talk/discord_length.py): a DM
        with him its [talk] dm_words, another Discord channel its discord_words cut to the band
        the turn drew once (so its wording, room, typing and trim agree); 0 for a game turn,
        which never reads it, or with the setting at 0."""
        if not is_discord(turn.channel):
            return 0
        words = words_for(getattr(self.cfg, "talk", None), turn.channel)
        if not words or is_private(turn.channel):
            return words
        if turn.discord_draw is None:
            # an answer to him (D1, a group DM, a selfie request) is owed something real: the
            # opener's mix, never only a few words
            to_him = bool(self.human.guid) and turn.addressee == self.human.guid
            turn.discord_draw = (opener_draw(self._length_rng) if to_him
                                 else self._length_rng.random())
        return band(words, turn.discord_draw)

    @staticmethod
    def _silent_length(words: int) -> str:
        """The {length} a silent Discord answer is asked again for, by its band."""
        for most, length in sorted(DISCORD_SILENT_SHORT.items()):
            if words <= most:
                return length
        return DISCORD_SILENT_LENGTH

    def _word_budget(self, turn: Turn, p: Persona, two_lines: bool = False) -> int:
        """How many words this turn's lines hold for the speaker now: a longer Discord
        message's discord_words, whatever her game line."""
        if words := self._discord_words(turn):
            return words
        cap = line_cap(p.typing, effects_for(self._altered_of(turn.speaker), turn.kind),
                       PHASE_MODS[day_phase(self.clock.local())].length_mult)
        lines = (turn.max_lines if turn.kind is TurnKind.BIT or two_lines
                 else turn_lines(p.typing, turn.max_lines))
        return cap * max(1, lines)

    def _link_level(self, guid: int) -> int:
        m = self.world.members.get(guid)
        return m.level if m is not None and m.level > 0 else CAST_MIN_LEVEL

    def _link_items(self, turn: Turn, meta: TurnMeta, text: str) -> str:
        """The turn's words with its item mentions as "[Name]" (ItemLinker.canonical), before the
        typing styles them: the form every record keeps. A sender's transport barrier vouches for
        its exact words, which are only defused, as every line is: no {{ reaches the bridge but
        the placeholders _wire_items makes."""
        items = getattr(self, "items", None)
        if items is None or meta.before_transport is not None:
            return defuse(text)
        return items.canonical(text, self._link_level(turn.speaker))

    def _wire_items(self, turn: Turn, meta: TurnMeta, text: str) -> str:
        """The say command's text: the line folded as the wire carries it (items.fold), and its
        "[Name]"s as native's {{item:<id>}} placeholders when native has them (ItemLinker.wire).
        Only the bridge sees it; the line is recorded as `text`."""
        items = getattr(self, "items", None)
        if items is None or meta.before_transport is not None:
            return fold(text)
        return items.wire(text, self._link_level(turn.speaker))

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
               two_lines: bool = False, romance: bool = False,
               realism: bool = False) -> list[Styled]:
        """The member's typing over the words: at most turn.max_lines lines (style_lines). A
        bit of several lines keeps the lines the model wrote, one each; any other turn is
        styled as one text. A member of his inner circle keeps his real name capitalised
        through it where it was written so (_real_name_spelling). A Discord turn keeps its
        letters and emoji (no ASCII fold) and its lines become one message, joined by "\n", at
        most the store's MAX_TEXT. A longer Discord message ([talk] discord_words) is one
        paragraph of up to LONG_SLACK times its words, whatever her game line."""
        discord = is_discord(turn.channel)
        words = self._discord_words(turn)
        phase = day_phase(self.clock.local())
        keep = self._real_name_spelling() if close else frozenset()
        pieces = ([x for x in text.split("\n") if x.strip()]
                  if turn.kind is TurnKind.BIT and turn.max_lines > 1 and not words else [text])
        out: list[Styled] = []
        typing = p.typing
        most = 1 if words else turn.max_lines
        for piece in pieces:
            out += style_lines(piece, typing, self._effects(turn.speaker), self.rng,
                               length_mult=PHASE_MODS[phase].length_mult,
                               protect=self._protect(turn.channel),
                               speaker=p.character_name, kind=turn.kind, keep_case=keep,
                               max_lines=most if len(pieces) == 1 else 1,
                               aliases=aliases(p), force_lines=two_lines and not words,
                               no_emoticon=romance or turn.kind in NO_MARK_KINDS,
                               wink_ok=not (self._to_member(turn) or names_any(
                                   piece, [m.name for g, m in self._members_for(turn.channel).items()
                                           if g != self.human.guid])), game=not discord,
                               cap=math.ceil(words * LONG_SLACK) if words else None,
                               max_chars=DISCORD_MAX_TEXT if discord else None,
                               texture=self.cfg.talk.texture)
        out = out[:max(1, most)]
        if discord and out:
            slipped = next((s for s in reversed(out) if s.slip), Styled("", ""))
            return [Styled("\n".join(s.line for s in out), " ".join(s.plain for s in out),
                           slipped.slip, slipped.fix)]
        return out

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
        if meta.photo is not None and not meta.photo_finished:
            if not caption_ok(text):
                return 'photo_words'
            if not meta.photo.valid():
                return 'photo_stale'
        if self.live_review.enabled(turn, meta) and not meta.realism_correction:
            # Recheck the reviewed text after typing, before its first fragment. A
            # fragment can look like filler by itself; later fragments inherit this
            # verdict, while transport scope, sequence and busy fences still run.
            candidates = (meta.realism_review_lines if first else ()) if meta.realism_planned else (meta.words or text,)
            for candidate in candidates:
                verdict = await self.live_review.check(turn, meta, candidate)
                if verdict.action != "send" and not self.live_review.owed(turn,meta):
                    return "guard_changed_before_send"
            if not self.live_review.valid(turn, meta):
                return "scope_changed"
        meta.voice_note = None
        notes = getattr(self,'voice_notes',None)
        # a line that carries a photo is its caption: typed, never a voice note
        if notes is not None and getattr(meta,'photo',None) is None and notes.eligible(turn,meta,p,text):
            self._phase(turn,RECORDING)
            try:
                meta.voice_note = await notes.prepare(turn,meta,p,text)
            finally:
                self._phase(turn,TYPING)
            if not _scope_valid(meta) or not self.live_review.valid(turn,meta):
                return 'scope_changed'
        async with self._timed_send_lock(meta):
            if meta.realism_followup and meta.realism_sequence != self.realism.member(turn.speaker).sequence:
                return "realism_stale"
            if meta.realism_followup and not self.realism.safe(self, turn):
                return "realism_busy"
            if meta.realism_correction and self.clock.monotonic() > meta.realism_correction_until:
                return "correction_expired"
            why = self._blocked(turn, meta, self.clock.now(), first=first, deadline=False)
            if why:
                return why
            inner = asyncio.ensure_future(self._deliver(turn, meta, p, text, memory_ids))
            self._delivering.add(inner)
            inner.add_done_callback(self._delivering.discard)
            job = asyncio.current_task()
            self._sending.add(job)  # its line is at the bridge: no logout carries it
            inner.add_done_callback(lambda _: self._sending.discard(job))
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
        # This shielded child may first run after its parent was invalidated.
        if not _scope_valid(meta):
            return "scope_changed"
        if turn.channel.startswith("voice:"):
            log.error("say_refused_voice_channel: guid=%d channel=%s", turn.speaker, turn.channel)
            return "voice_channel"
        if not _transport_admitted(meta):
            return "transport_refused"
        if is_discord(turn.channel):  # never a Say: its message is stored, then recorded
            return self._deliver_discord(turn, meta, p, text, memory_ids)
        try:
            self.timing.typed_send(meta.timing_trace)
            with self.timing.span(meta.timing_trace, "bridge_ack"):
                ack = await self.send(Say(id="", guid=turn.speaker, channel=turn.channel,
                                          text=self._wire_items(turn, meta, text),
                                          to_guid=turn.to_guid))
        except (AckTimeout, ConnectionLost) as e:
            # The bridge may still have said it: its echo can confirm it (is_echo).
            why = _CAMEL.sub("_", type(e).__name__).lower()
            self._unacked[(turn.speaker, turn.channel, text)] = (
                self.clock.now(), turn, meta, p, list(memory_ids), why)
            self.timing.uncertain(meta.timing_trace, why)
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
        self.timing.typed_ack(meta.timing_trace)
        try:
            self._record(turn, meta, p, text, memory_ids, ack=ack)
        except Exception:
            log.exception("say_record_failed: guid=%d channel=%s", turn.speaker, turn.channel)
            return "record_error"
        return None

    def _dm_to_him(self, turn: Turn) -> None:
        """A dm:<guid> turn is a line to him, whatever its turn said (ruling R12)."""
        if turn.channel.startswith(DM_PREFIX) and self.human.guid:
            turn.to_guid = self.human.guid

    def _deliver_discord(self, turn: Turn, meta: TurnMeta, p: Persona, text: str,
                         memory_ids: list[int]) -> str | None:
        """A Discord line: stored as its message (transport.store_line), then recorded with that
        message's id; nothing goes to the bridge, so there is no ack and no echo to wait for.
        None, or why it wasn't said: discord_<code> for what the store refused."""
        self._dm_to_him(turn)
        server_turn = self._server_turn(turn, meta)
        from leeroy_mind.server.records import BadDayCounter
        stage = 'store'
        photo = meta.photo if not meta.said else None
        atomic_record = photo is not None or meta.on_discord_record is not None or meta.outreach_item is not None
        after_commit = []
        try:
            with self.store.transaction() if (server_turn or atomic_record
                                              or meta.voice_note is not None) else nullcontext():
                if photo is not None and (meta.photo_finished or not photo.valid()):
                    return 'photo_stale'
                posted_at = self.clock.now()
                message = store_line(self.store, turn, meta, text, posted_at,
                                     audience=self.world.audience(
                                         turn.channel, from_guid=turn.speaker, to_guid=turn.to_guid))
                stage = 'record'
                if server_turn:
                    from leeroy_mind.server.records import posted
                    posted(self.store, self.clock.local().date().isoformat(), self.clock.now(),
                           conversation=meta.server_conversation,
                           dm_guid=turn.speaker if meta.why == 'server:dm' and not meta.said else None)
                if meta.voice_note is not None:
                    self.voice_notes.record(message,turn.speaker,meta.voice_note)
                if atomic_record:
                    line = self._record(turn, meta, p, text, memory_ids, discord_id=message,
                                        after_commit=after_commit)
                    if meta.on_discord_record is not None:
                        meta.on_discord_record(line, message, posted_at)
            # The post and its budget are durable before _record updates memory-only
            # guards, chain state and hooks. Those effects cannot survive a rollback.
            if not meta.realism_correction:  # the turn's own messages, never a stored id lookup
                meta.discord_messages.append(message)
            if not atomic_record:
                self._record(turn, meta, p, text, memory_ids, discord_id=message)
            else:
                if photo is not None:
                    meta.photo_finished = True
                    if photo.snap is not None:  # a culture meme (MemeOffer) logs its own posting
                        log.info('photo_sent snap=%d line=%d', photo.snap.id, line)
                for effect in after_commit:
                    effect()
        except DiscordRefused as e:
            log.info("discord_line_refused: guid=%d channel=%s code=%s", turn.speaker,
                     turn.channel, e.code)
            return f"discord_{e.code}"
        except BadDayCounter:
            return 'record_error'  # counts() reports this once; server posts fail closed
        except Exception:
            log.exception(("discord_store_failed: guid=%d channel=%s" if stage == 'store'
                           else "say_record_failed: guid=%d channel=%s"),
                          turn.speaker, turn.channel)
            return stage + '_error'
        server = getattr(getattr(self, 'server_app', None), 'server', None)
        reactor = getattr(server, 'reactions', None)
        if reactor is not None:
            try:
                reactor.after_post(message, author=turn.speaker, channel=turn.channel,
                                   text=text, his=False)
            except Exception:
                log.warning('server_reaction_schedule_failed message=%d', message)
        remember = getattr(self, 'remember_discord_photo', None)
        if remember is not None and meta.discord is not None and meta.discord.reply_to is not None:
            try:
                remember(turn.speaker, meta.discord.reply_to)
            except Exception as error:
                log.warning('discord_photo_memory_failed: guid=%d code=%s',
                            turn.speaker, type(error).__name__)
        return None

    def _record(self, turn: Turn, meta: TurnMeta, p: Persona, text: str,
                memory_ids: list[int], *, ack: Ack | None = None,
                discord_id: int | None = None,
                after_commit: list[Callable[[], None]] | None = None) -> int:
        """A line was said: the chat log (origin "mind", with who could see it), the world, the
        thread hook, the guards, the chain and the utterance log (with why and the bit). Its
        line id. A line he had no way of hearing (Comms.unheard: I8, N3) moves neither the
        quiet clock nor the open thread with him; every typed line still does. A Discord line
        (`discord_id`, its message) keeps its channel in the logs and counts as said, but stays
        out of the game's chat state and clocks: the world's history, the quiet clock, the last
        line a member said there, the open thread with him and his game session, whose last
        activity a restart reads (no session id on its line or utterance; rulings R11)."""
        if meta.outreach_item is not None and after_commit is None:
            effects = []
            with self.store.transaction():
                line = self._record(turn, meta, p, text, memory_ids, ack=ack,
                                    discord_id=discord_id, after_commit=effects)
            for effect in effects:
                effect()
            return line
        now = self.clock.now()
        discord = is_discord(turn.channel)
        heard = discord or self.comms is None or not self._comms_says(
            "unheard", False, turn.channel, meta)
        m = self.world.members.get(turn.speaker)
        zone, map_id = (m.zone, m.map) if m is not None else (0, 0)
        sid = None if discord else self._session_id
        chat = Chat(ts=int(now * 1000), channel=turn.channel, from_guid=turn.speaker,
                    from_name=p.character_name, to_guid=turn.to_guid, text=text, map=map_id,
                    zone=zone)
        audience = self.world.audience(turn.channel, from_guid=turn.speaker,
                                       to_guid=turn.to_guid, zone=zone)
        learn = getattr(self, "learn_enabled", False) and not meta.realism_correction
        review = self.live_review.enabled(turn, meta) and not meta.realism_correction
        if learn or review:
            with self.store.transaction():
                line_id = self.store.insert_chat_line(chat, session_id=sid, origin="mind", audience=audience)
                if discord_id is not None:
                    self.store.set_meta(f'discord:line:{discord_id}',str(line_id))
                self._bakeoff_record(turn, meta, line_id, discord_id, now, text)
                if review:
                    self.live_review.record(turn, meta, meta.words or text, line_id)
                if learn:
                    from leeroy_mind.learn.recipe import record as record_recipe
                    try:
                        with self.store.transaction():
                            record_recipe(self.store.conn, line_id, turn, meta, p, self.cfg,
                                          settings=getattr(self,'learn_settings',None))
                            if meta.said == 0 and meta.learning is not None:
                                from leeroy_mind.learn.apply import record as record_learning
                                record_learning(self, turn, meta)
                    except Exception:
                        log.warning("learn_recipe_record_failed")
                    if getattr(self,'learn_capture_preferences',False):
                        try:
                            with self.store.transaction():
                                from leeroy_mind.learn.capture import delivered
                                delivered(self.store.conn,line_id,meta,settings=getattr(self,'learn_settings',None))
                        except Exception:
                            log.warning('learn_preference_capture_failed')
        else:
            with self.store.transaction():
                line_id = self.store.insert_chat_line(chat, session_id=sid, origin="mind", audience=audience)
                if discord_id is not None:
                    self.store.set_meta(f'discord:line:{discord_id}',str(line_id))
                self._bakeoff_record(turn, meta, line_id, discord_id, now, text)
        if meta.photo is not None and discord_id is not None and not meta.said:
            with self.store.transaction():
                meta.photo.on_sent(line_id, discord_id)
        if meta.outreach_item is not None:
            self.outreach.record(turn, meta, line_id, now)
        def finished():
            self._after_record(turn, meta, p, text, memory_ids, line_id=line_id, chat=chat,
                now=now, discord=discord, heard=heard, sid=sid, zone=zone, map_id=map_id,
                ack=ack, discord_id=discord_id)
        if after_commit is None:
            finished()
        else:
            after_commit.append(finished)
        return line_id

    def _bakeoff_record(self, turn: Turn, meta: TurnMeta, line_id: int,
                        discord_id: int | None, now: float, text: str) -> None:
        """[bakeoff] capture, inside the line's transaction: a savepoint of its own that never
        raises (bakeoff/capture.py on_record)."""
        if not meta.bakeoff_requests and meta.bakeoff_row is None:
            return
        from leeroy_mind.bakeoff.capture import on_record
        on_record(self, turn, meta, line_id=line_id, discord_id=discord_id, now=now,
                  words=meta.words or text)

    def _after_record(self, turn, meta, p, text, memory_ids, *, line_id, chat, now,
                      discord, heard, sid, zone, map_id, ack, discord_id):
        """Memory-only guards and hooks run only after an atomic post commits."""
        if meta.outreach_item is not None:
            self.outreach.delivered(meta, line_id)
        if self.cfg.talk.realism:
            meta.realism_sequence = self.realism.record_line(turn.speaker,credit=not meta.realism_correction)
            if meta.realism_planned and not meta.realism_correction and meta.words:
                own = self._own_of(turn.speaker)
                if meta.realism_own is None:
                    meta.realism_own = (turn.channel,turn.to_guid,meta.words)
                    own.append(meta.realism_own)
                else:
                    # Grow the same history turn as confirmed lines arrive. Never
                    # resurrect an entry evicted by newer turns in the bounded deque.
                    for i, entry in enumerate(own):
                        if entry is meta.realism_own:
                            meta.realism_own = (*entry[:2], entry[2] + " " + meta.words)
                            own[i] = meta.realism_own
                            break
        if not discord and not meta.realism_correction:
            self.world.add_line(ChatLineRow(
                id=line_id, ts=now, channel=turn.channel, from_guid=turn.speaker,
                from_name=p.character_name, to_guid=turn.to_guid, text=text, map=map_id,
                zone=zone, session_id=sid, thread_id=None, origin="mind"), now)
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
        if (heard and hg and not discord
                and (meta.answers_human or turn.addressee == hg or turn.to_guid == hg)):
            self._answered_at[turn.speaker] = now  # a thread with him is open, in the game
        (meta.guards or self.guards).record(meta.turn_id, turn.speaker, now, turn.kind,
                                            initiated=meta.initiates)
        meta.said += 1
        self._turn_lines[meta.turn_id] = meta.said
        while len(self._turn_lines) > TURNS_KEPT:
            del self._turn_lines[next(iter(self._turn_lines))]
        if meta.said == 1:
            self._spend(turn, meta, now)
            self._first_line_said(turn, meta)
        self._chain_line(chain_base(turn.channel, turn.speaker, turn.to_guid, zone), now,
                         from_human=False, guards=meta.guards)
        if heard and turn.channel != "whisper" and not discord:
            self._touch_talk(now)
        self.store.log_utterance(now, turn.speaker, turn.channel, turn.kind.value, text,
                                 memory_ids, None, sid, bit=_bit_id(turn),
                                 why="realism_correction" if meta.realism_correction else meta.why or None,
                                 trigger_line=turn.trigger_line, llm_s=meta.llm_s,
                                 to_guid=turn.to_guid, turn_id=meta.turn_id,
                                 guard_verdict=meta.guard_verdict,guard_rewritten=meta.guard_rewritten)
        self._record_notes(turn, meta, p, meta.words or text, now)
        if discord_id is None:
            log.info("say_sent: guid=%d kind=%s channel=%s line=%d trigger=%s why=%s bit=%r "
                     "llm_s=%s", turn.speaker, turn.kind, turn.channel, line_id,
                     turn.trigger_line or "-", meta.why or "-", _bit_id(turn), _secs(meta.llm_s))
        else:
            log.info("say_sent: guid=%d kind=%s channel=%s line=%d message=%d trigger=%s why=%s "
                     "bit=%r llm_s=%s", turn.speaker, turn.kind, turn.channel, line_id,
                     discord_id, turn.trigger_line or "-", meta.why or "-", _bit_id(turn),
                     _secs(meta.llm_s))
        if self.on_line_sent is not None:
            try:
                self.on_line_sent(line_id, chat)
            except Exception:
                log.exception("on_line_sent hook failed")
        if (signals := getattr(self, "signals", None)) is not None:
            signals.sent(line_id, chat, addressed=bool(hg and
                         (meta.answers_human or turn.addressee == hg or turn.to_guid == hg)),
                         heard=heard)
        if meta.on_recorded is not None and ack is not None:
            try:
                meta.on_recorded(line_id, ack)
            except Exception:
                log.warning("say_recorded_hook_failed: guid=%d line=%d", turn.speaker, line_id)
        return line_id

    def _record_notes(self, turn: Turn, meta: TurnMeta, p: Persona, words: str,
                      now: float) -> None:
        """A said line's scene notes about him (romance.scene_notes, PairNotes), for a woman with
        an attitude toward him on a turn to him that is her own (not a template): the moves in
        her line, and on the turn's first line his in the line it answers (romance.his_moves),
        when that line is his, was to her (his whisper to her, or a public line that speaks to
        her by name: filters.line_addressee) and this turn may see it (whisper_ok). Each is
        filled with his pronouns, so the stored note is what a prompt shows, and kept under the
        turn's channel, so the whisper rule applies on reading. A failing write is logged,
        never with the words, and never raised."""
        hg = self.human.guid
        if (not hg or turn.template is not None or self._counterpart(turn) != hg
                or p.identity.player_gender != "female"
                or p.romance.stance not in ROMANCE_SHARE):
            return
        try:
            his: frozenset[str] = frozenset()
            if meta.said == 1 and turn.trigger_line:
                rows = self.store.chat_lines([turn.trigger_line])
                row = rows[0] if rows else None
                if (row is not None and row.from_guid == hg and self._may_carry(turn, row)
                        and (row.to_guid == p.guid if row.channel == "whisper" else
                             line_addressee(row.text, (p.character_name, *aliases(p)))
                             is not None)):
                    his = his_moves(row.text)
            they, them, their = self.human.forms
            for kind, note in scene_notes(classify(words), his):
                self.notes.add(p.guid, hg, kind, note.format(they=they, them=them, their=their),
                               channel=turn.channel, to_guid=turn.to_guid, ts=now)
        except Exception as e:
            log.warning("note_record_failed: guid=%d channel=%s reason=%s", turn.speaker,
                        turn.channel, type(e).__name__)

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
        self._record_beat(turn, meta, now)
        self._settle_phrase_hint(turn, meta, now)

    def _drop(self, turn: Turn, meta: TurnMeta, reason: str, memory_ids=(), *, text: str = "",
              at: float | None = None) -> list[str]:
        if meta.learning is not None and meta.said == 0:
            from leeroy_mind.learn.apply import rejected
            rejected(self, turn, meta, reason)
        self._release_photo(meta, reason)
        if meta.outreach_item is not None:
            self.outreach.release(meta)
        self.timing.decision(meta.timing_trace, reason)
        if reason.startswith("guard_"):
            text = ""
            log.info("guard_drop guid=%d code=%s codes=%s memory_ids=%s",turn.speaker,reason,
                     ",".join(meta.guard_codes),list(memory_ids))
        sid = None if is_discord(turn.channel) else self._session_id  # no game activity (R11)
        self.store.log_utterance(self.clock.now() if at is None else at, turn.speaker,
                                 turn.channel or "", turn.kind.value, text or "",
                                 list(memory_ids), reason, sid,
                                 bit=_bit_id(turn), why=meta.why or None,
                                 trigger_line=turn.trigger_line, llm_s=meta.llm_s,
                                 to_guid=turn.to_guid, turn_id=meta.turn_id,
                                 guard_verdict=meta.guard_verdict,guard_rewritten=meta.guard_rewritten)
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
                finally:
                    self.timing.echo_settled(meta.timing_trace, confirmed=False)

    def is_echo(self, ev: Chat, now: float) -> bool:
        """The bridge's report of a line a `say` produced (via_bridge). The mind stored its own
        line on the ack; an echo of a line whose ack timed out records it now, and that is the
        line's only row. The echo carries native's item links, which read as the "[Name]" the
        line was sent as (strip_wow_markup): that plain line is what is matched and recorded."""
        if not ev.via_bridge:
            return False
        self._settle_unacked(now)
        text = strip_wow_markup(ev.text)
        late = self._unacked.pop((ev.from_guid, ev.channel, text), None)
        if late is None:
            log.debug("say_confirmed: guid=%d channel=%s", ev.from_guid, ev.channel)
            return True
        _, turn, meta, p, memory_ids, _ = late
        try:
            self._record(turn, meta, p, text, memory_ids)
        except Exception:  # the app's dispatcher must never see it
            log.exception("say_record_failed: guid=%d channel=%s", ev.from_guid, ev.channel)
            if meta.outreach_item is not None:
                self.outreach.release(meta)
        else:
            log.info("say_confirmed_by_echo: guid=%d channel=%s", ev.from_guid, ev.channel)
        self.timing.echo_settled(meta.timing_trace, confirmed=True)
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
