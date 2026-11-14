"""The talk engine: the live loop that decides who in the guild speaks, when and where, and
sends what they say over the bridge.

- on_human_chat plans the guild's answers to the human (replies, goodnights);
- on_event reacts to the game (reactions.py: greetings, gz, kills, wipes, the combat cancel);
- on_text_emote answers his text emotes (emotes.py: an emote back, sometimes a line);
- tick runs the social bursts the budget allows, moves the altered states on, fires the whisper
  triggers once per event and polls the registered TurnSources (P1d and phase 3);
- members start conversations with him on two clocks (owner, 2026-11-07: "they only respond,
  never actually initiate"): the WhisperPlanner's whispers (romantic and personal, by whisper)
  and the budget's openers addressed to him (casual, party when he is grouped with them, else
  guild), at mind.toml's [talk] whispers_per_hour and openers_per_hour, both scaled with
  chattiness (whisper_rate) and the member dials, never in a fight, the wipe's silence, its
  debrief or his login's quiet;
- every turn goes through the line pipeline (pipeline.py), which composes the prompt, asks the
  model, filters assistant tells, styles the words as the member types and sends each line.

With the library (fix wave part E), his game question (facts.game_question) gets one library
lookup, started as the replies are planned; every reply to that line carries its facts, and a
member who knows the topic (facts.fit_tier) is among those who answer.

His pushback on what a member just said (fix wave G6, select.pushback_kind) is answered first
by that member, in one line, told he is pushing back (prompt.pushback_help); the rest of the
plan is as usual (_pushback).

Silence is valid: a reply count can be 0, the model can answer (nothing), and every guard, raid
rule and chain cap drops a turn rather than delaying it. Nothing here waits on a turn: on_event,
on_human_chat and tick spawn them and return."""
import dataclasses
import math
import random
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable, Protocol

from leeroy_mind.clock import Clock, night_of
from leeroy_mind.config import MindConfig
from leeroy_mind.human import human_identity
from leeroy_mind.llm.scheduler import LLMScheduler
from leeroy_mind.memory.recall import Recall
from leeroy_mind.personas.io import PersonaCache
from leeroy_mind.personas.schema import Persona
from leeroy_mind.protocol import Ack, Chat, Command
from leeroy_mind.storage.rows import ChatLineRow, MemoryRow
from leeroy_mind.storage.store import Store
from leeroy_mind.talk.bits import BitScheduler, birthday_line, is_birthday_bit
from leeroy_mind.talk.budget import (GAP_REFERENCE_CHATTINESS, BurstBudget, BurstPlan,
                                     DayPhase, day_phase)
from leeroy_mind.talk.channels import choose_channel
from leeroy_mind.talk.emotes import EmoteReactions
from leeroy_mind.talk.facts import LibraryFacts
from leeroy_mind.talk import pastes
from leeroy_mind.talk.guards import Guards
from leeroy_mind.talk.items import ItemLinker
from leeroy_mind.talk.pastes import PASTE_BURST_S, PASTE_SETTLE_S, PasteBurst, is_addon_row
from leeroy_mind.talk.filters import holds, word_counts
from leeroy_mind.talk.pipeline import (FOLLOW_SOURCES, KEEPER, NOBODY, NOBODY_WHO, THEIRS,
                                       TurnMeta, _asks, _names_list, chain_base, log,
                                       picked_why)
from leeroy_mind.talk.prompt import pushback_help
from leeroy_mind.talk.questions import FIT_TIERS, fit_tier, game_question, is_plan
from leeroy_mind.talk.raid import RaidPhase, RaidState
from leeroy_mind.talk.reactions import (EVENING_S, GZ_TEMPLATES, LEAD_CALL, LEAD_GROUP_CALL,
                                        LOGIN_QUIET_S, MID_GROUP, MID_RAID)
from leeroy_mind.talk.romance import JealousyAsides, WhisperPlanner, flirt_chance
from leeroy_mind.talk.select import (PUSHBACK_WINDOW_S, WHISPER_REPLY_P, Pushback, ReplyTurn,
                                     aimed_at_one, asks_the_past, classify_human_line,
                                     is_quest_talk, mentioned, pick_speakers, plan_reply_turns,
                                     pushback_kind, topic_words, whisper_reply_p)
from leeroy_mind.talk.select import _PLAY_WORDS, _interest_pull
from leeroy_mind.talk.states import (Altered, advance_state, capped_states, roll_session_state,
                                     state_cap)
from leeroy_mind.talk.turns import Turn, TurnKind
from leeroy_mind.talk.world import WorldView, looks_like_bot_command
from leeroy_mind.tuning.effective import dials_of
from leeroy_mind.wow import faction_known

__all__ = ["GZ_TEMPLATES", "TalkEngine", "TurnSource", "flirt_chance", "share_reason_pool"]

# Where a member answers the human's line: on his channel, a yell as /say.
REPLY_CHANNEL = {"guild": "guild", "officer": "officer", "party": "party", "raid": "raid",
                 "raid_warning": "raid", "say": "say", "yell": "say", "whisper": "whisper"}
COMMAND_CHANNELS = frozenset({"party", "raid", "raid_warning"})
# Where a line of his can push back on a member (fix wave G6): every reply channel but /say.
PUSHBACK_CHANNELS = frozenset({"guild", "officer", "party", "raid", "whisper"})
REPLY_FIRST_S, REPLY_NEXT_S = (1.0, 4.0), (2.0, 6.0)
PASTE_TEXT = ("{called} pasted an addon's output in chat. React to this one line of it in your own "
              "words, in one short line, and leave the rest.")
OPEN_S, CONTINUE_S = (0.0, 3.0), (4.0, 15.0)
STATE_STEP_S = 600.0
HUMAN_ACTIVE_S = 600.0
# Guild chat this quiet is the "idle" whisper trigger (was 900 s: rehearsal 2, judge C 6, no
# woman could whisper him first before the late hour).
WHISPER_QUIET_S = 600.0
AFTER_S = 300.0  # bit contexts: after_kill, after_wipe
LOOT_S = 120.0  # bit context: loot
# A word of his question about the guild's past is rare, and so names its matter, when this
# few of the guild's memories hold it: at most max(RARE_MIN, RARE_SHARE of them) (fix round 1,
# review C1, measured on the rehearsal's 448 memories).
RARE_MIN = 8
RARE_SHARE = 0.02
# One member's openers to him are at least this far apart (engine._open_to_him).
TO_HIM_GAP_S = 1200.0
# What an opener to him brings up, as OPEN_TO_HIM's {topic} (prompt.py), rotated per member and
# night: something of their own (prompt.open_topics), a question for him, a memory with him
# and, when they have one, tonight's events.
TO_HIM_TOPICS = ("tell {them} about {own}", "ask {them} what {they}'re up to tonight",
                 "ask {them} how {their} day went",
                 "bring up something you remember doing with {them}, if one of your memories "
                 "holds it, else ask what {they}'re up to")
TO_HIM_TONIGHT = "bring up what happened tonight ({lately})"
# The guild's next calendar event (Store.next_calendar_event) this soon is a topic too.
TO_HIM_EVENT = "bring up what's on the guild calendar: {event}"
GUILD_EVENT_AHEAD_S = 86400.0
GUILD_EVENT_KINDS = ("raid", "meeting", "birthday", "anniversary")
GROUP_PARTY_MAX = 5  # an opener to him goes to party when his group is this small


def whisper_rate(cfg: MindConfig) -> float:
    """The WhisperPlanner's rate: [talk] whispers_per_hour, scaled with chattiness as the
    bursts are (4 is the reference: 0 starts nothing)."""
    return cfg.talk.whispers_per_hour * cfg.talk.chattiness / GAP_REFERENCE_CHATTINESS


def share_reason_pool(planned: list[ReplyTurn], pool: set[int], weights: dict[int, float],
                      called: list[int], rng) -> list[ReplyTurn]:
    """When every drawn reply came from a reason pool no bigger than the number of replies,
    the last one goes back into a single weighted draw over everyone not already answering
    (that member included), so the same few never answer every line (Task 13 carry). Members
    named in the line keep their place and don't count."""
    drawn = [t for t in planned if t.speaker not in called]
    if not pool or not drawn or len(pool) > len(planned) or any(
            t.speaker not in pool for t in drawn):
        return planned
    last = drawn[-1]
    taken = {t.speaker for t in planned if t is not last}
    pick = pick_speakers({g: w for g, w in weights.items() if g not in taken}, 1, rng)
    if not pick:
        return planned
    return [dataclasses.replace(t, speaker=pick[0]) if t is last else t for t in planned]


class TurnSource(Protocol):
    """The P1d and phase-3 extension point (raid calls, DKP posts, combat callouts). tick polls
    every registered source; the turns one poll proposes are spoken one after another through
    the same pipeline as the engine's own. A turn without a channel gets choose_channel's."""

    async def propose(self, now: float) -> list[Turn]: ...


class TalkEngine(EmoteReactions):
    """The guild's voice. The app feeds it the human's lines (on_human_chat), game events
    (on_event), his text emotes (on_text_emote), a tick and the session's start and end; P1c
    reads prompts through compose, P1d speaks through say_as and register_source. The pipeline
    (speak), the reactions and the answers to his emotes live in pipeline.py, reactions.py and
    emotes.py."""

    def __init__(self, *, store: Store, world: WorldView, raid: RaidState, recall: Recall,
                 scheduler: LLMScheduler, personas: PersonaCache, cfg: MindConfig,
                 data_dir: Path, clock: Clock, rng, send: Callable[[Command], Awaitable[Ack]],
                 human_guid: int = 0,
                 on_line_sent: Callable[[int, Chat], object] | None = None, library=None,
                 library_search=None, facts: LibraryFacts | None = None,
                 items: ItemLinker | None = None):
        """send is BridgeClient.send. on_line_sent(line_id, chat) is told about every line the
        mind says once it is acknowledged and stored: the app wires it to
        ThreadTracker.on_line_sent, the only way the mind's own lines reach the threads.
        facts is the caller-owned service; the engine triggers its session reset. For standalone
        callers, library constructs a compatible service using the current human's faction."""
        if facts is not None and (library is not None or library_search is not None):
            raise ValueError("facts cannot be combined with library or library_search")
        if library is None and library_search is not None:
            raise ValueError("library_search requires library")
        self.store, self.world, self.raid, self.recall = store, world, raid, recall
        self.scheduler, self.personas, self.cfg = scheduler, personas, cfg
        self.data_dir, self.clock, self.rng, self.send = Path(data_dir), clock, rng, send
        self.on_line_sent = on_line_sent
        self.items = items  # the item links (talk/items.py); None: lines link nothing
        self.facts = facts if facts is not None else (
            LibraryFacts(library, scheduler, faction=lambda: faction_known(self.human.race),
                         search=library_search) if library is not None else None)
        self.guards = Guards(rng=rng)
        self.budget = BurstBudget(cfg.talk.chattiness, rng,
                                  to_him_per_hour=cfg.talk.openers_per_hour)
        self.bits = BitScheduler(store, clock, rng)
        self.whispers = WhisperPlanner(rng, per_hour=whisper_rate(cfg),
                                       events=whisper_rate(cfg) > 0)
        self._to_him_at: dict[int, float] = {}  # a member's last opener to him (TO_HIM_GAP_S)
        self.human = human_identity(cfg, store, human_guid)
        self.asides = JealousyAsides(rng, jealousy=cfg.romance.jealousy, human=self.human)
        now = clock.now()
        self._init_pipeline(now)
        from leeroy_mind.talk.awareness import LiveReview
        self.live_review = LiveReview(self)
        self._init_reactions()
        self._init_emotes()
        self._sources: list[TurnSource] = []
        self._pastes: dict[tuple[str, int, str], PasteBurst] = {}
        self._session_at = self._states_at = now
        self._late_night = ""  # the night the late-hour whisper trigger last fired
        # (active memories, how many of them hold each word): _rare_words
        self._word_df: tuple[int, Counter[str]] | None = None

    # -- configuration and sessions ------------------------------------------------------------

    def apply_config(self, cfg: MindConfig) -> None:
        """A reloaded mind.toml: chattiness, the initiation rates, jealousy, the romance dial,
        the live model, the memory depth and the human's names take effect from the next
        turn."""
        self.cfg = cfg
        self.live_review.configure()
        self.budget.set_chattiness(cfg.talk.chattiness)
        self.budget.set_to_him_rate(cfg.talk.openers_per_hour)
        self.whispers.set_rate(whisper_rate(cfg))
        self.whispers.events = whisper_rate(cfg) > 0  # chattiness 0: no unprompted whisper
        self.recall.set_tau(cfg.memory.depth_days)
        self.asides.set_jealousy(cfg.romance.jealousy)
        self.set_human(self.human.guid)

    def register_source(self, src: TurnSource) -> None:
        self._sources.append(src)

    async def start_session(self, session_id: int) -> None:
        """The human logged in: roll everyone's altered state for the night, start the whisper
        planner, and start the prompts' chat window here (nothing from a session before it
        reaches a prompt). The greetings come from on_event(Login)."""
        if self.cfg.talk.realism:
            self.realism.fights.clear()
        now = self.clock.now()
        self._session_id = session_id
        self._session_at = self._states_at = self._chat_from = now
        self._gz_seen.clear()
        self._gz_waiting.clear()
        self._touch_talk(now)
        self.whispers.start_session()
        self.budget.reset_to_him()
        self._evening_at = now + random.Random(f"{session_id}:evening").uniform(*EVENING_S)
        self._evening_done = False
        local = self.clock.local()
        personas = self.personas.all()
        olds, rolled = {}, {}
        for g, p in personas.items():
            st = self.store.get_state(g)
            olds[g] = (st, self._altered_of(g))
            last = (olds[g][1], float(st.updated)) if st.updated else None
            rolled[g] = roll_session_state(p, local, last, now, self.rng)
        rolled = capped_states(rolled, state_cap(len(personas)), night_of(local))
        with self.store.transaction():
            for g, new in rolled.items():
                st, old = olds[g]
                self._altered[g] = new
                self.store.put_state(dataclasses.replace(
                    st, altered_state=new.value, state_session=session_id, updated=int(now),
                    state_since=int(now) if new != old else st.state_since))

    async def end_session(self) -> None:
        """The human logged off (or the mind is stopping): cancel every pending turn, give each
        line still waiting for an echo its row, end his login stretch (the next login greets),
        and keep the altered states, with the time they were last in effect, for the next
        session."""
        await self._cancel_all()
        if self.cfg.talk.realism:
            self.realism.fights.clear()
        self._pastes.clear()
        self._settle_unacked(self.clock.now(), everything=True)
        self.timing.flush("session_end")
        if self.facts is not None:
            await self.facts.reset_session()
        self._greeted_login = False
        if self._session_id is None:
            return
        now = int(self.clock.now())
        with self.store.transaction():
            for g, a in self._altered.items():
                st = self.store.get_state(g)
                self.store.put_state(dataclasses.replace(st, altered_state=a.value, updated=now))
        self._session_id = None

    # -- the human speaks ----------------------------------------------------------------------

    async def on_human_chat(self, row: ChatLineRow) -> None:
        """Plan the answers to one of the human's lines, which the app has stored and added to
        the world. Bot commands in party or raid (the app also keeps them out of threads: the
        same check, a different job) and emote lines get nothing. Replies are spoken one after
        another, so each can see the one before."""
        timing_origin = self.timing.now()
        reply_to = REPLY_CHANNEL.get(row.channel) or (
            row.channel if row.channel.startswith("voice:") else None)  # a line he spoke
        if reply_to is None:
            return
        if row.channel in COMMAND_CHANNELS and looks_like_bot_command(row.text):
            return
        now = self.clock.now()
        if is_addon_row(row.text):
            self._on_paste(row, reply_to, now)
            return
        if row.from_guid and row.from_guid != self.human.guid:
            self.set_human(row.from_guid)
        hg = self.human.guid
        self._chain_line(chain_base(row.channel, row.from_guid, row.to_guid, row.zone), now,
                         from_human=True)
        self._touch_talk(now)
        whisper = reply_to == "whisper"
        audience, cands = self._reply_candidates(row, reply_to, now)
        if not cands:  # liveops M5: a line nobody answers says why in the log
            log.info("reply_skipped: line=%d reason=no_candidates channel=%s audience=%d "
                     "online=%d", row.id, reply_to, len(audience), len(self.world.online))
            return
        ctx = self._context(now, topic_words([row.text]), quest=is_quest_talk(row.text))
        weights, pool = self._weigh(cands, last=hg, ctx=ctx, reply=True)
        names = {g: self.world.members[g].name for g in cands if g in self.world.members}
        push = self._pushback(row, reply_to, cands)
        planned = plan_reply_turns(row.text, channel=reply_to, to_guid=row.to_guid,
                                   weights=weights, names=names, afk=self.world.afk,
                                   rng=self.rng, line_id=row.id, human_guid=hg, reasons=pool,
                                   whisper_p=whisper_reply_p(self.personas.get(row.to_guid))
                                   if whisper else WHISPER_REPLY_P,
                                   target=push.target if push else 0)
        called = mentioned(row.text, names)
        # the members he named and the one his pushback is aimed at keep their place
        held = called if push is None or push.target in called else [push.target, *called]
        shared = share_reason_pool(planned, pool, weights, held, self.rng)
        # the turn share_reason_pool re-drew (it hands back a new list) came from the draw
        redrawn = ([t.turn_id for t in planned if t.speaker not in held][-1:]
                   if shared is not planned else [])
        planned = shared
        question = game_question(row.text) if self.facts is not None else None
        fitted = None
        # a question put to the one member he named is answered by that member, not by whoever
        # knows the topic (fix wave G5)
        if question is not None and not whisper and not aimed_at_one(row.text, called):
            planned, fitted = self._fit_replier(planned, question.topic, weights, held,
                                                line_id=row.id, channel=reply_to)
            # only the first answer gets the facts: the one who knows the topic goes first,
            # after anyone he named
            fit = [t for t in planned if t.turn_id == fitted and t.speaker not in held]
            if fit:
                named = [t for t in planned if t.speaker in held]
                planned = named + fit + [t for t in planned if t not in named and t not in fit]
        lore: dict[str, tuple[str, int]] = {}
        kept = None  # the turn a keeper of the guild's past was put in
        if not whisper and not called and push is None and question is None and (
                self.facts is not None or game_question(row.text) is None):
            planned, lore, kept = self._plan_lore(row, planned, weights, reply_to)
        if (not whisper and self._login_quiet(now)
                and classify_human_line(row.text) == "greeting"):
            planned = self._cap_hellos(planned, held)  # the login's 2-3 hellos in all
        log.info("reply_planned: line=%d channel=%s pool=%d replies=%d game=%s", row.id,
                 reply_to, len(pool), len(planned), question.topic if question else "-")
        if question is not None and planned:
            self.facts.start(row.id, question)  # one lookup, read by every reply below
        # his goodnight from a formed raid, or a group he leads: the members in it and the
        # lead are told he is leaving mid-raid (rehearsal 2, judge A 9)
        group_chat = (self._mid_raid() if not whisper and any(
            rt.kind is TurnKind.GOODNIGHT for rt in planned) else None)
        lead = self.raid.lead() if group_chat else None
        items = []
        for i, rt in enumerate(planned):
            channel = choose_channel(rt.kind, speaker=rt.speaker, world=self.world,
                                     raid=self.raid, now=now, rng=self.rng, reply_to=reply_to)
            if channel is None:
                log.info("reply_skipped: line=%d reason=no_channel guid=%d", row.id, rt.speaker)
                continue
            delay = self.rng.uniform(*(REPLY_FIRST_S if i == 0 else REPLY_NEXT_S))
            pushed = push is not None and rt.speaker == push.target
            how = ("whisper" if whisper else "named" if rt.speaker in called
                   else "pushback" if pushed
                   else "topic_fit" if rt.turn_id == fitted
                   else "memory" if rt.turn_id == kept
                   else "weighted_draw" if rt.turn_id in redrawn
                   else "reason_pool" if rt.speaker in pool else "weighted_draw")
            facts = row.id if question is not None and rt.kind is TurnKind.REPLY_HUMAN else 0
            told = ""
            if group_chat and rt.kind is TurnKind.GOODNIGHT and self.world.in_group(rt.speaker):
                told = self._mid_raid_text(group_chat, lead=rt.speaker == lead)
            said_lore, pin = lore.get(rt.turn_id, ("", 0))
            turn = Turn(kind=rt.kind, speaker=rt.speaker,
                        channel=reply_to if rt.kind is TurnKind.GOODNIGHT else channel,
                        to_guid=rt.to_guid, addressee=hg, trigger_line=row.id,
                        instruction=told, delay=delay * self._effects(rt.speaker).delay_mult)
            meta = TurnMeta(rt.turn_id, answers_human=True, whisper_reply=whisper,
                            why=f"reply:{how}", facts_line=facts, lore=said_lore,
                            lore_memory=pin, first_question_answer=(rt.kind is TurnKind.REPLY_HUMAN
                                and not any(t.kind is TurnKind.REPLY_HUMAN for t,_ in items)
                                and classify_human_line(row.text) == "question_all"))
            if pushed:  # one line, told he is pushing back, by how agreeable they are
                ease = self.personas.get(rt.speaker).big_five.agreeableness.score
                turn.instruction = " ".join(
                    x for x in (told, pushback_help(self.human, push.kind, ease)) if x)
                turn.max_lines = 1
                meta.pushback, meta.pushback_line = push.kind, push.line
            items.append((turn, meta))
        if (lead and lead not in {rt.speaker for rt in planned} and lead in cands
                and self.world.in_group(lead)):
            items.append((Turn(kind=TurnKind.GOODNIGHT, speaker=lead, channel=group_chat,
                               addressee=hg, trigger_line=row.id,
                               instruction=self._mid_raid_text(group_chat, lead=True),
                               delay=self.rng.uniform(*REPLY_NEXT_S)),
                          TurnMeta(f"reply-{row.id}-lead", answers_human=True,
                                   why="reply:raid_lead")))
        for _, meta in items:
            meta.timing_origin = timing_origin
        self._queue(items)

    def _reply_candidates(self, row: ChatLineRow, reply_to: str, now: float
                          ) -> tuple[set[int] | list[int], list[int]]:
        """Who heard his line on `reply_to` (a whisper: only the one it went to) and the members
        among them who may answer it now."""
        whisper = reply_to == "whisper"
        audience = {row.to_guid} if whisper else self.world.audience(
            reply_to, from_guid=self.human.guid, to_guid=row.to_guid, zone=row.zone)
        cands = [g for g in self._candidates(audience, hearable=False)
                 if self.raid.allows(g, TurnKind.REPLY_HUMAN, now)
                 and not (reply_to == "say" and self._is_dead(g, now))]
        return audience, cands

    def _on_paste(self, row: ChatLineRow, reply_to: str, now: float) -> None:
        """An addon paste row: it joins the burst on its channel that began less than
        PASTE_BURST_S ago and plans nothing, or starts a burst whose one answer is planned
        after PASTE_SETTLE_S."""
        self._touch_talk(now)
        key = (reply_to, row.to_guid, pastes.paste_kind(row.text))
        burst = self._pastes.get(key)
        if burst is not None and now - burst.started < PASTE_BURST_S:
            burst.rows.append(row)
            return
        burst = self._pastes[key] = PasteBurst(reply_to, row.to_guid, now, [row])
        self._spawn(self._answer_paste(burst))

    async def _answer_paste(self, burst: PasteBurst) -> None:
        """One reply to a whole paste burst, from a member named in it (else anyone who may
        answer), about the row naming them (else its top or bottom line)."""
        await self.clock.sleep(PASTE_SETTLE_S)
        now = self.clock.now()
        rows = list(burst.rows)
        first = rows[0]
        reply_to, whisper = burst.channel, burst.channel == "whisper"
        _, cands = self._reply_candidates(first, reply_to, now)
        if not cands:
            log.info("reply_skipped: line=%d reason=no_candidates channel=%s paste=1", first.id,
                     reply_to)
            return
        names = {g: self.world.members[g].name for g in cands if g in self.world.members}
        hit = [g for g in pastes.named(rows, names) if g in cands] if not whisper else []
        pool = hit or cands
        focus = pastes.focus_row(rows, self.rng)
        ctx = self._context(now, topic_words([focus.text]))
        weights, _ = self._weigh(pool, last=self.human.guid, ctx=ctx, reply=True)
        picked = pick_speakers(weights, 1, self.rng)
        if not picked:
            return
        speaker = picked[0]
        line = (pastes.row_naming(rows, speaker, names) or focus) if hit else focus
        channel = choose_channel(TurnKind.REPLY_HUMAN, speaker=speaker, world=self.world,
                                 raid=self.raid, now=now, rng=self.rng, reply_to=reply_to)
        if channel is None:
            log.info("reply_skipped: line=%d reason=no_channel guid=%d", first.id, speaker)
            return
        turn = Turn(kind=TurnKind.REPLY_HUMAN, speaker=speaker, channel=channel,
                    to_guid=first.to_guid, addressee=self.human.guid, trigger_line=line.id,
                    instruction=PASTE_TEXT.format(called=self.human.called),
                    delay=self.rng.uniform(*REPLY_FIRST_S) * self._effects(speaker).delay_mult)
        meta = TurnMeta(f"paste-{first.id}", answers_human=True, whisper_reply=whisper,
                        why="reply:paste", first_question_answer=classify_human_line(line.text) == "question_all")
        log.info("paste_burst: channel=%s rows=%d guid=%d", reply_to, len(rows), speaker)
        self._queue([(turn, meta)])

    def _mid_raid(self) -> str | None:
        """His group's channel ("raid" or "party") when he is in a formed raid or leads his
        group, else None."""
        g, hg = self.world.group, self.human.guid
        if g is None or not hg or not self.world.in_group(hg):
            return None
        if g.raid:
            return "raid"
        return "party" if g.leader_guid == hg else None

    def _mid_raid_text(self, group_chat: str, *, lead: bool) -> str:
        """MID_RAID, or LEAD_CALL for the lead; a party he leads is "the group"."""
        if group_chat == "raid":
            text = LEAD_CALL if lead else MID_RAID
        else:
            text = LEAD_GROUP_CALL if lead else MID_GROUP
        return text.format(name=self.human.called, them=self.human.forms[1])

    def _pushback(self, row: ChatLineRow, reply_to: str, cands: list[int]) -> Pushback | None:
        """His line pushing back on a member (select.pushback_kind), and who it is aimed at (fix
        wave G6): the one member it names; naming nobody, the one he whispers, or the one who
        wrote the last member line on its channel within PUSHBACK_WINDOW_S before it, whether
        the mind said that line or the bridge reported it. With voice on, a typed line is read
        against the room he sits in too, since what he heard is what he is answering; the
        answer stays typed (Comms.place). The names read are the whole cast's,
        so a line naming a member who is away is no pushback on someone else. None for a line
        naming two members or more, with nobody in the window, or when the one it is aimed at
        can't answer it (not among `cands`: AFK, offline, the raid gate)."""
        pushes = reply_to in PUSHBACK_CHANNELS or reply_to.startswith("voice:")
        kind = pushback_kind(row.text) if pushes else None
        if kind is None:
            return None
        hg = self.human.guid
        named = mentioned(row.text, {g: m.name for g, m in self.world.members.items() if g != hg})
        channels = [c for c, to in REPLY_CHANNEL.items() if to == reply_to] or [reply_to]
        if self.comms is not None and not reply_to.startswith("voice:"):
            room = self._comms_says("human_room", None)
            if room:
                channels.append(f"voice:{room}")
        line = None
        if len(named) > 1:
            target, why = 0, "two_names"
        elif named or reply_to == "whisper":
            target, why = (named[0] if named else row.to_guid), "cannot_answer"
        else:
            line = self._line_before(row, channels,
                                     since=max(row.ts - PUSHBACK_WINDOW_S, self._chat_from))
            target, why = (line.from_guid, "cannot_answer") if line else (0, "nobody")
        if target not in cands:
            log.info("reply_pushback_none: line=%d kind=%s reason=%s", row.id, kind, why)
            return None
        if line is None:  # their own last line on its channel, for their prompt to show
            line = self._line_before(row, channels, since=self._chat_from, guid=target)
        push = Pushback(kind, target, line.id if line is not None else 0)
        log.info("reply_pushback: line=%d kind=%s target=%d own_line=%d", row.id, kind, target,
                 push.line)
        return push

    def _line_before(self, row: ChatLineRow, channels: list[str], *, since: float,
                     guid: int = 0) -> ChatLineRow | None:
        """The last line a cast member (`guid` when given) wrote on `channels` before his line
        `row` and no older than `since`, from the log, so the mind's lines and those the
        bridge reported alike; a whisper only one to him."""
        cast, hg = self.world.cast_guids(), self.human.guid
        return next((r for r in self.store.lines_before(row.id, channels, since=since,
                                                        from_guid=guid)
                     if r.from_guid in cast and (r.channel != "whisper" or r.to_guid == hg)),
                    None)

    def _fit_replier(self, planned: list[ReplyTurn], topic: str, weights: dict[int, float],
                     called: list[int], *, line_id: int,
                     channel: str) -> tuple[list[ReplyTurn], str | None]:
        """His game question is answered by a member who knows its topic (facts.fit_tier):
        unless one of the best tier on hand (a weight above 0) is planned already, one of them,
        drawn by weight, takes the last drawn reply, or answers as well when everyone planned
        is someone he named (three at most) or nobody is. The plan, and the id of the turn
        that was fitted (None when the plan stands)."""
        tiers: dict[int, dict[int, float]] = {}
        for g, w in weights.items():
            t = fit_tier(topic, self.personas.get(g))
            if t is not None and w > 0:
                tiers.setdefault(t, {})[g] = w
        if not tiers:
            return planned, None
        best = tiers[min(tiers)]
        if any(t.speaker in best for t in planned):
            return planned, None
        # the tie-break among the best of a theorycrafter or lore tier: how far the owner has
        # turned their interest dial (tuning), as written for everyone else
        tag = FIT_TIERS[topic][min(tiers)]
        if tag in _PLAY_WORDS:
            pull = {g: _interest_pull(self.personas.get(g), set(_PLAY_WORDS[tag])) for g in best}
            if any(v != 1.0 for v in pull.values()):
                best = {g: w * pull[g] for g, w in best.items()}
        pick = pick_speakers(best, 1, self.rng)
        drawn = [t for t in planned if t.speaker not in called]
        if drawn:
            new = dataclasses.replace(drawn[-1], speaker=pick[0])
            return [new if t is drawn[-1] else t for t in planned], new.turn_id
        if len(planned) >= 3:
            return planned, None
        kind = planned[0].kind if planned else TurnKind.REPLY_HUMAN
        new = ReplyTurn(pick[0], f"reply-{line_id}-{len(planned)}", kind, channel, channel)
        return [*planned, new], new.turn_id

    def _plan_lore(self, row: ChatLineRow, planned: list[ReplyTurn], weights: dict[int, float],
                   channel: str) -> tuple[list[ReplyTurn], dict[str, tuple[str, int]],
                                          str | None]:
        """His question about the guild's past that names nobody (fix wave G3, rehearsal 3
        check 3: two such questions drew five answers from members who held no memory of it,
        one of them false, while the two who held it were never drawn). Who holds it:
        _lore_holders; with nobody holding it the plan stands (fix round 1, review C1).
        - With a holder here (one of `weights`, the members who may answer), the best of them
          answers first: planned already, she moves to the front; else she takes the last
          drawn reply, or is the only one. Every holder among the answers answers from her
          memory, which her prompt shows (KEEPER); the rest react to their story (THEIRS).
        - With none here, nobody states a fact (NOBODY), and up to two who would know are
          named.
        The plan, each turn's (instruction, memory id), and the id of the turn a keeper was
        put in (None when she was planned already, or none was)."""
        holders = self._lore_holders(row.text)
        if not holders:
            return planned, {}, None
        here = [g for g in holders if g in weights]
        log.info("reply_lore: line=%d holders=%d here=%d", row.id, len(holders), len(here))
        if not here:
            away = [self._name_of(g) for g in list(holders)[:2]]
            who = NOBODY_WHO.format(names=_names_list(away)) if away else ""
            return planned, {t.turn_id: (NOBODY.format(who=who), 0) for t in planned}, None
        keeper = next((g for g in here if weights[g] > 0), here[0])
        kept = None
        if any(t.speaker == keeper for t in planned):
            first = next(t for t in planned if t.speaker == keeper)
            planned = [first, *(t for t in planned if t is not first)]
        elif planned:
            first = dataclasses.replace(planned[-1], speaker=keeper)
            planned, kept = [first, *planned[:-1]], first.turn_id
        else:
            first = ReplyTurn(keeper, f"reply-{row.id}-0", TurnKind.REPLY_HUMAN, channel,
                              channel)
            planned, kept = [first], first.turn_id
        tellers = [self._name_of(t.speaker) for t in planned if t.speaker in holders][:2]
        theirs = THEIRS.format(names=_names_list(tellers), s="s" if len(tellers) == 1 else "")
        return planned, {t.turn_id: (KEEPER, holders[t.speaker].id) if t.speaker in holders
                         else (theirs, 0) for t in planned}, kept

    def _lore_holders(self, text: str) -> dict[int, MemoryRow]:
        """Who holds a memory of what his question asks about the guild's past, best match
        first; {} when it is no such question. It is one when it asks (pipeline._asks), is no
        plan (questions.is_plan) and either asks about the past by its words
        (select.asks_the_past: since when, how long, who started it, who/what ... again about
        a matter named in two words or more) or someone holds it from before tonight (the
        owner's seeded history, or a chat, extract or game memory of an earlier session). A
        member holds it when a public memory of theirs, never hearsay (Recall.holders), shares
        a subject word that is rare across the guild's memories, and two subject words, or the
        question's only one; a question that does not ask about the past by its words needs
        two rare shared words (filters.holds; fix round 1, review C1: "raid", "night",
        "guild", "chat" and "bank" never count)."""
        if not _asks(text) or is_plan(text):
            return {}
        hg, past, rare = self.human.guid, asks_the_past(text), self._rare_words()
        holders: dict[int, MemoryRow] = {}
        for m in self.recall.holders(text):
            if (m.owner != hg and m.owner not in holders and self.personas.get(m.owner)
                    is not None and holds(text, m.text, rare=rare, past=past)):
                holders[m.owner] = m
        if past or any(m.source == "owner" or (m.source in FOLLOW_SOURCES
                                                and m.t_event < self._chat_from)
                       for m in holders.values()):
            return holders
        return {}

    def _rare_words(self) -> Callable[[str], bool]:
        """Whether a word is rare across the guild's active memories: in at most
        max(RARE_MIN, RARE_SHARE of them). The counts (filters.word_counts) are kept until the
        number of active memories changes."""
        n = self.store.active_memory_count()
        if self._word_df is None or self._word_df[0] != n:
            self._word_df = (n, word_counts(self.store.active_memory_texts()))
        counts, most = self._word_df[1], max(RARE_MIN, int(n * RARE_SHARE))
        return lambda w: counts[w] <= most

    def _name_of(self, guid: int) -> str:
        """A member's character name, online or not."""
        m = self.world.members.get(guid)
        if m is not None:
            return m.name
        p = self.personas.get(guid)
        return p.character_name if p is not None else "someone"

    # -- the clock -----------------------------------------------------------------------------

    async def tick(self, now: float) -> None:
        """About once a second (the app's ticker): a social burst when the budget allows one,
        the altered states every 10 minutes, the whisper triggers once per event, the end of
        any "offline" mark that has run out (world.refresh) and the registered TurnSources.
        In the quiet after his login (reactions.LOGIN_QUIET_S) there are no bursts, openers
        to him or whisper triggers; a trigger due then fires after it."""
        if self._session_id is not None and now - self._states_at >= STATE_STEP_S:
            self._advance_states(now)
        self.world.refresh(now)
        self._settle_unacked(now)
        self.timing.expire()
        self._gz_after_fight(now)
        if self.world.human_online:
            quiet = self._login_quiet(now)
            quiet_until = self._login_at + LOGIN_QUIET_S if quiet else None
            plan = None if getattr(self,"banter_active",False) else self.budget.tick(now, phase=day_phase(self.clock.local()),
                                    human_idle=self.world.human_idle(now),
                                    raid=self.raid.phase(now), quiet_until=quiet_until)
            if plan is not None:
                self._spawn(self._run_burst(plan))
            plan = None if getattr(self,"banter_active",False) else self.budget.to_him(now, raid=self.raid.phase(now), quiet_until=quiet_until,
                                      hold=self._initiation_held(now),
                                      talk_at=self._his_talk_at())
            if plan is not None:
                self._spawn(self._run_burst(plan))
            if not quiet:
                self._whisper_triggers(now)
        for src in list(self._sources):
            try:
                turns = await src.propose(now)
            except Exception:
                log.exception("turn_source_failed: %s", type(src).__name__)
                continue
            # copies: a source may propose the same Turn again, and the pipeline fills them in
            why = f"source:{type(src).__name__}"
            self._queue([(dataclasses.replace(t), TurnMeta(self._new_id(), why=why))
                         for t in turns or ()])

    def _advance_states(self, now: float) -> None:
        self._states_at = now
        local = self.clock.local()
        minutes = (now - self._session_at) / 60
        changed = {}
        personas = self.personas.all()
        cap = state_cap(len(personas))
        counts = Counter(self._altered_of(g) for g in personas)
        for g, p in personas.items():
            cur = self._altered_of(g)
            new = advance_state(p, cur, minutes, local, self.rng)
            if new != cur and (new is Altered.SOBER or counts[new] < cap):
                counts[cur] -= 1
                counts[new] += 1
                self._altered[g] = changed[g] = new
        if changed:
            with self.store.transaction():
                for g, a in changed.items():
                    self.store.put_state(dataclasses.replace(
                        self.store.get_state(g), altered_state=a.value, state_since=int(now),
                        updated=int(now)))

    def _whisper_triggers(self, now: float) -> None:
        """The WhisperPlanner's own clock (reactions._start_whisper), then each trigger as one
        draw: a quiet stretch of guild chat (re-armed by the next line), the evening (once a
        session, reactions._evening_whisper) and entering the late hour (once a night, unless
        the evening's draw was made that night). The clock holds while he is AFK, so it never
        fires the moment he is back; the triggers skip it."""
        self._start_whisper(now)
        if self.world.human_afk:
            return
        if not self._quiet_fired and now - self._last_talk_at >= WHISPER_QUIET_S:
            self._quiet_fired = True
            self._maybe_whisper("idle", now)
        if now >= self._evening_at:
            self._evening_whisper(now, f"{self.human.called} has been on for a while tonight.")
        local = self.clock.local()
        if day_phase(local) is DayPhase.LATE and self._late_night != night_of(local):
            self._late_night = night_of(local)
            if self._evening_night != night_of(local):
                self._maybe_whisper("late", now)

    # -- bursts --------------------------------------------------------------------------------

    async def _run_burst(self, plan: BurstPlan) -> None:
        mult = None
        try:
            if plan.to_him:
                mult = await self._open_to_him()
            else:
                await self._burst(plan)
        finally:
            if plan.to_him:
                self.budget.to_him_done(self.clock.now(), mult)
            elif plan.channel_hint is None:  # raid-cadence plans keep the budget's own clock
                self.budget.burst_done(self.clock.now())

    async def _open_to_him(self) -> float | None:
        """One opener addressed to him (the budget's to_him plan): a member who may speak now,
        has not opened to him for TO_HIM_GAP_S and is in no whisper with him
        (_whisper_threads), drawn as an answer to him is (_weigh with him as the last speaker:
        closeness, the talkative dial, nobody who just spoke), says his name and brings
        something up (_to_him_topic), never carrying romance (TurnMeta.casual). With him in a
        group of GROUP_PARTY_MAX or fewer, a group-mate who can is drawn first, in party
        (_to_him_channel); else anyone, in guild. Its silence is asked again once, and a chain
        never stops it (TurnMeta.initiates). The talkative dials' pull on the clock (their mean
        over who may speak), or None with nobody. Nobody may speak in his raid's fight, its
        wipe or its debrief (RaidState.allows), and a fight that begins before the line goes
        out drops it (Pipeline._blocked)."""
        now, hg = self.clock.now(), self.human.guid
        if not hg or not self.world.human_online:
            return None
        outreach = getattr(self, 'outreach', None)
        every = [g for g in self._candidates() if self.raid.allows(g, TurnKind.OPEN, now)]
        if outreach is not None:
            every = [g for g in every if outreach.eligible(g)]
        if not every:
            log.info("open_to_him_skipped: reason=no_candidates")
            return None
        mult = sum(dials_of(self.personas.get(g)).mult("talkative") for g in every) / len(every)
        busy = self._whisper_threads(now)
        cands = [g for g in every if g not in busy
                 and now - self._to_him_at.get(g, -math.inf) >= TO_HIM_GAP_S]
        weights, _ = self._weigh(cands, last=hg, ctx=self._context(now, opener=True),
                                 reply=False)
        mates = {g: w for g, w in weights.items() if self._to_him_channel(g) == "party"}
        picked = pick_speakers(mates, 1, self.rng) or pick_speakers(weights, 1, self.rng)
        if not picked:
            log.info("open_to_him_skipped: reason=nobody_free candidates=%d", len(every))
            return mult
        g = picked[0]
        self._to_him_at[g] = now
        turn = Turn(kind=TurnKind.OPEN, speaker=g, channel=self._to_him_channel(g), addressee=hg,
                    max_lines=1,
                    delay=self.rng.uniform(*OPEN_S) * self._effects(g).delay_mult)
        item = outreach.select(g, turn.channel) if outreach is not None else None
        meta = TurnMeta(self._new_id(), topic=item.about if item else self._to_him_topic(self.personas.get(g), now),
                        why="burst:to_him", initiates=True, casual=True)
        if item is not None and not outreach.bind(turn, meta, item):
            return mult
        await self._speak(turn, meta)
        return mult

    def _to_him_channel(self, speaker: int) -> str:
        """Party when he and the speaker are in one group of GROUP_PARTY_MAX or fewer (not a
        raid), else guild."""
        g, hg = self.world.group, self.human.guid
        if (g is not None and not g.raid and len(g.members) <= GROUP_PARTY_MAX
                and self.world.in_group(hg) and self.world.in_group(speaker)):
            return "party"
        return "guild"

    def _to_him_topic(self, p: Persona, now: float) -> str:
        """What this member's next opener to him brings up (TO_HIM_TOPICS, and TO_HIM_TONIGHT
        when they did something tonight), in an order fixed per member and night, so their
        openers go round it instead of asking the same thing. The guild's next calendar event
        within GUILD_EVENT_AHEAD_S (TO_HIM_EVENT) is one of them while there is one."""
        night = night_of(self.clock.local())
        key = (p.guid, f"{night}:to_him")
        n = self._opens.get(key, 0)
        self._opens[key] = n + 1
        options = list(TO_HIM_TOPICS)
        lately = self._lately_line(p.guid, now, outreach=True)
        if lately:
            options.append(TO_HIM_TONIGHT)
        event = self._guild_event(now)
        if event:
            options.append(TO_HIM_EVENT)
        random.Random(f"{p.guid}:{night}:to_him").shuffle(options)
        pick = options[n % len(options)]
        own = self._open_topic(p, now) if "{own}" in pick else ""
        they, them, their = self.human.forms
        return pick.format(own=own, lately=lately, event=event, they=they, them=them,
                           their=their)

    def _guild_event(self, now: float) -> str:
        """The guild's next calendar event within GUILD_EVENT_AHEAD_S, as its title and when
        ("Raid night tonight at 20:00"), read from what the calendar desk posted; "" when
        there is none or the calendar cannot be read."""
        try:
            found = self.store.next_calendar_event(now, now + GUILD_EVENT_AHEAD_S,
                                                   GUILD_EVENT_KINDS)
        except sqlite3.Error:
            return ""
        if found is None:
            return ""
        title, at = found
        local = self.clock.local()
        when = datetime.fromtimestamp(at, tz=local.tzinfo)
        day = (("tonight" if when.hour >= 17 else "today") if when.date() == local.date()
               else "tomorrow")
        return f"{' '.join(title.split())} {day} at {when:%H:%M}"

    async def _burst(self, plan: BurstPlan) -> None:
        """One OPEN, then CONTINUE turns answering the line before: plan.size lines or fewer
        from plan.max_speakers members or fewer. A dropped or silent turn ends it."""
        now = self.clock.now()
        cands = [g for g in self._candidates()
                 if (plan.channel_hint != "raid" or self.world.in_group(g))
                 and self.raid.allows(g, TurnKind.OPEN, now)]
        weights, _ = self._weigh(cands, last=None, ctx=self._context(now, opener=True),
                                 reply=False)
        first = pick_speakers(weights, 1, self.rng)
        if not first:
            return
        speaker = first[0]
        channel = plan.channel_hint or choose_channel(
            TurnKind.OPEN, speaker=speaker, world=self.world, raid=self.raid, now=now,
            rng=self.rng)
        if channel is None:
            return
        kind, addressee, trigger, delay = TurnKind.OPEN, None, None, OPEN_S
        why = "burst:weighted_draw"
        lines, speakers = 0, {speaker}
        typed_on = None  # the game channel it began on, for a line that can't be spoken
        while True:
            now = self.clock.now()
            turn, meta = self._social_turn(kind, speaker, channel, addressee=addressee,
                                           trigger=trigger, room=plan.size - lines, now=now,
                                           delay=self.rng.uniform(*delay), why=why)
            meta.typed_on = typed_on
            sent = await self._speak(turn, meta)
            typed_on = meta.typed_on
            lines += len(sent)
            if not sent or lines >= plan.size:
                return
            last, now, channel = speaker, self.clock.now(), turn.channel  # where it was said
            pool = [g for g in self._candidates(self._audience(channel, last), exclude=(last,))
                    if (len(speakers) < plan.max_speakers or g in speakers)
                    and self.raid.allows(g, TurnKind.CONTINUE, now)]
            said = next((t for ch, _, t in reversed(self._own[last]) if ch == channel), "")
            weights, reasons = self._weigh(pool, last=last, ctx=self._context(
                now, topic_words([said])), reply=False)
            nxt = pick_speakers(weights, 1, self.rng, reasons)
            if not nxt:
                return
            speaker = nxt[0]
            why = picked_why("burst", speaker, reasons)
            channel = choose_channel(TurnKind.CONTINUE, speaker=speaker, world=self.world,
                                     raid=self.raid, now=now, rng=self.rng,
                                     opener_channel=channel)
            if channel is None:
                return
            speakers.add(speaker)
            kind, addressee, trigger, delay = (TurnKind.CONTINUE, last,
                                               self._last_line_id.get(last), CONTINUE_S)

    def _social_turn(self, kind: TurnKind, speaker: int, channel: str, *, addressee, trigger,
                     room: int, now: float, delay: float,
                     why: str = "") -> tuple[Turn, TurnMeta]:
        """An OPEN or CONTINUE turn, unless the speaker brings out a bit (one that fits the
        lines left) or, being a woman who wants him, flirts with the human instead."""
        p = self.personas.get(speaker)
        meta = TurnMeta(self._new_id(), why=why)
        delay *= self._effects(speaker).delay_mult
        whose, bday = self._birthday_tonight(speaker) or ("", 0)
        bit = self.bits.pick(p, context=self._bit_context(now), raid=self.raid.phase(now),
                             mood=self.store.get_state(speaker).mood, local=self.clock.local(),
                             birthday=whose or None)
        if bit is not None and bit.lines <= room:
            if whose and is_birthday_bit(bit):
                meta.emote_target = bday  # her /hug goes to them
            return Turn(kind=TurnKind.BIT, speaker=speaker, channel=channel,
                        instruction=self.bits.turn_instruction(bit, whose), bit=bit,
                        max_lines=bit.lines, delay=delay), meta
        hg = self.human.guid
        chance = flirt_chance(p, self._effects(speaker).candour)
        if hg and chance > 0 and self.world.human_online and not self.world.human_idle(
                now, HUMAN_ACTIVE_S) and self.rng.random() < chance:
            return Turn(kind=TurnKind.FLIRT, speaker=speaker, channel=channel, addressee=hg,
                        max_lines=min(2, room), delay=delay), meta
        if kind is TurnKind.OPEN:
            meta.topic = self._open_topic(p, now)
        return Turn(kind=kind, speaker=speaker, channel=channel, addressee=addressee,
                    trigger_line=trigger, max_lines=min(2, room), delay=delay), meta

    def _birthday_tonight(self, speaker: int) -> tuple[str, int] | None:
        """Whose birthday tonight is by the cast's cards, as birthday_line says it (one or two
        named, the rest counted), and the first named member's guid; None when it is nobody's
        but the speaker's. The order is drawn once a night over everyone born tonight, so the
        same members are named all night (fix round 1, review I6)."""
        night = night_of(self.clock.local())
        born = sorted(g for g, q in self.personas.all().items()
                      if q.real_life.birthday == night[5:])
        random.Random(f"{night}:birthday").shuffle(born)
        others = [g for g in born if g != speaker]
        if not others:
            return None
        named = others[:2]
        return (birthday_line([self.personas.get(g).character_name for g in named],
                              len(others)), named[0])

    def _bit_context(self, now: float) -> set[str]:
        """The situation tags a bit can ask for: org (a raid forming), trash, after_kill,
        after_wipe, late_night and loot."""
        ctx = set()
        g = self.world.group
        if g is not None and g.raid:
            ctx.add("trash" if self._raid_fought else "org")
        if self.raid.phase(now) is RaidPhase.KILL or now - self._kill_at < AFTER_S:
            ctx.add("after_kill")
        if now - self._wipe_at < AFTER_S:
            ctx.add("after_wipe")
        if day_phase(self.clock.local()) in (DayPhase.LATE, DayPhase.DEAD):
            ctx.add("late_night")
        if now - self._loot_at < LOOT_S:
            ctx.add("loot")
        return ctx
