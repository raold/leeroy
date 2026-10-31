"""The talk engine: the live loop that decides who in the guild speaks, when and where, and
sends what they say over the bridge.

- on_human_chat plans the guild's answers to the human (replies, goodnights);
- on_event reacts to the game (reactions.py: greetings, gz, kills, wipes, the combat cancel);
- tick runs the social bursts the budget allows, moves the altered states on, fires the whisper
  triggers once per event and polls the registered TurnSources (P1d and phase 3);
- every turn goes through the line pipeline (pipeline.py), which composes the prompt, asks the
  model, filters assistant tells, styles the words as the member types and sends each line.

With the library (fix wave part E), his game question (facts.game_question) gets one library
lookup, started as the replies are planned; every reply to that line carries its facts, and a
member who knows the topic (facts.fit_tier) is among those who answer.

Silence is valid: a reply count can be 0, the model can answer (nothing), and every guard, raid
rule and chain cap drops a turn rather than delaying it. Nothing here waits on a turn: on_event,
on_human_chat and tick spawn them and return."""
import dataclasses
import random
from collections import Counter
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
from leeroy_mind.talk.budget import BurstBudget, BurstPlan, DayPhase, day_phase
from leeroy_mind.talk.channels import choose_channel
from leeroy_mind.talk.facts import LibraryFacts
from leeroy_mind.talk.guards import Guards
from leeroy_mind.talk.filters import holds, word_counts
from leeroy_mind.talk.pipeline import (FOLLOW_SOURCES, KEEPER, NOBODY, NOBODY_WHO, THEIRS,
                                       TurnMeta, _asks, _names_list, chain_base, log,
                                       picked_why)
from leeroy_mind.talk.questions import fit_tier, game_question, is_plan
from leeroy_mind.talk.raid import RaidPhase, RaidState
from leeroy_mind.talk.reactions import (EVENING_S, GZ_TEMPLATES, LEAD_CALL, LEAD_GROUP_CALL,
                                        LOGIN_QUIET_S, MID_GROUP, MID_RAID, Reactions)
from leeroy_mind.talk.romance import JealousyAsides, WhisperPlanner, flirt_chance
from leeroy_mind.talk.select import (WHISPER_REPLY_P, ReplyTurn, asks_the_past,
                                     classify_human_line, is_quest_talk, mentioned,
                                     pick_speakers, plan_reply_turns, topic_words,
                                     whisper_reply_p)
from leeroy_mind.talk.states import (Altered, advance_state, capped_states, roll_session_state,
                                     state_cap)
from leeroy_mind.talk.turns import Turn, TurnKind
from leeroy_mind.talk.world import WorldView, looks_like_bot_command
from leeroy_mind.wow import faction_known

__all__ = ["GZ_TEMPLATES", "TalkEngine", "TurnSource", "flirt_chance", "share_reason_pool"]

# Where a member answers the human's line: on his channel, a yell as /say.
REPLY_CHANNEL = {"guild": "guild", "officer": "officer", "party": "party", "raid": "raid",
                 "raid_warning": "raid", "say": "say", "yell": "say", "whisper": "whisper"}
COMMAND_CHANNELS = frozenset({"party", "raid", "raid_warning"})
REPLY_FIRST_S, REPLY_NEXT_S = (1.0, 4.0), (2.0, 6.0)
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


class TalkEngine(Reactions):
    """The guild's voice. The app feeds it the human's lines (on_human_chat), game events
    (on_event), a tick and the session's start and end; P1c reads prompts through compose, P1d
    speaks through say_as and register_source. The pipeline (speak) and the reactions live in
    pipeline.py and reactions.py."""

    def __init__(self, *, store: Store, world: WorldView, raid: RaidState, recall: Recall,
                 scheduler: LLMScheduler, personas: PersonaCache, cfg: MindConfig,
                 data_dir: Path, clock: Clock, rng, send: Callable[[Command], Awaitable[Ack]],
                 human_guid: int = 0,
                 on_line_sent: Callable[[int, Chat], object] | None = None, library=None):
        """send is BridgeClient.send. on_line_sent(line_id, chat) is told about every line the
        mind says once it is acknowledged and stored: the app wires it to
        ThreadTracker.on_line_sent, the only way the mind's own lines reach the threads.
        library: the app's read-only Library (None: no facts, talk as before part E); its
        lookups queue on `scheduler`, and its facts are for the faction of his race."""
        self.store, self.world, self.raid, self.recall = store, world, raid, recall
        self.scheduler, self.personas, self.cfg = scheduler, personas, cfg
        self.data_dir, self.clock, self.rng, self.send = Path(data_dir), clock, rng, send
        self.on_line_sent = on_line_sent
        self.facts = (LibraryFacts(library, scheduler,
                                   faction=lambda: faction_known(self.human.race))
                      if library is not None else None)
        self.guards = Guards(rng=rng)
        self.budget = BurstBudget(cfg.talk.chattiness, rng)
        self.bits = BitScheduler(store, clock, rng)
        self.whispers = WhisperPlanner(rng)
        self.human = human_identity(cfg, store, human_guid)
        self.asides = JealousyAsides(rng, jealousy=cfg.romance.jealousy, human=self.human)
        now = clock.now()
        self._init_pipeline(now)
        self._init_reactions()
        self._sources: list[TurnSource] = []
        self._session_at = self._states_at = now
        self._late_night = ""  # the night the late-hour whisper trigger last fired
        # (active memories, how many of them hold each word): _rare_words
        self._word_df: tuple[int, Counter[str]] | None = None

    # -- configuration and sessions ------------------------------------------------------------

    def apply_config(self, cfg: MindConfig) -> None:
        """A reloaded mind.toml: chattiness, jealousy, the romance dial, the live model, the
        memory depth and the human's names take effect from the next turn."""
        self.cfg = cfg
        self.budget.set_chattiness(cfg.talk.chattiness)
        self.recall.set_tau(cfg.memory.depth_days)
        self.asides.set_jealousy(cfg.romance.jealousy)
        self.set_human(self.human.guid)

    def register_source(self, src: TurnSource) -> None:
        self._sources.append(src)

    async def start_session(self, session_id: int) -> None:
        """The human logged in: roll everyone's altered state for the night, start the whisper
        planner, and start the prompts' chat window here (nothing from a session before it
        reaches a prompt). The greetings come from on_event(Login)."""
        now = self.clock.now()
        self._session_id = session_id
        self._session_at = self._states_at = self._chat_from = now
        self._gz_seen.clear()
        self._gz_waiting.clear()
        self._touch_talk(now)
        self.whispers.start_session()
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
        self._settle_unacked(self.clock.now(), everything=True)
        if self.facts is not None:
            self.facts.clear()
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
        if row.channel not in REPLY_CHANNEL:
            return
        if row.channel in COMMAND_CHANNELS and looks_like_bot_command(row.text):
            return
        now = self.clock.now()
        if row.from_guid and row.from_guid != self.human.guid:
            self.set_human(row.from_guid)
        hg = self.human.guid
        self._chain_line(chain_base(row.channel, row.from_guid, row.to_guid, row.zone), now,
                         from_human=True)
        self._touch_talk(now)
        reply_to = REPLY_CHANNEL[row.channel]
        whisper = reply_to == "whisper"
        audience = {row.to_guid} if whisper else self.world.audience(
            reply_to, from_guid=hg, to_guid=row.to_guid, zone=row.zone)
        cands = [g for g in self._candidates(audience)
                 if self.raid.allows(g, TurnKind.REPLY_HUMAN, now)
                 and not (reply_to == "say" and self._is_dead(g, now))]
        if not cands:  # liveops M5: a line nobody answers says why in the log
            log.info("reply_skipped: line=%d reason=no_candidates channel=%s audience=%d "
                     "online=%d", row.id, reply_to, len(audience), len(self.world.online))
            return
        ctx = self._context(now, topic_words([row.text]), quest=is_quest_talk(row.text))
        weights, pool = self._weigh(cands, last=hg, ctx=ctx, reply=True)
        names = {g: self.world.members[g].name for g in cands if g in self.world.members}
        planned = plan_reply_turns(row.text, channel=reply_to, to_guid=row.to_guid,
                                   weights=weights, names=names, afk=self.world.afk,
                                   rng=self.rng, line_id=row.id, human_guid=hg, reasons=pool,
                                   whisper_p=whisper_reply_p(self.personas.get(row.to_guid))
                                   if whisper else WHISPER_REPLY_P)
        called = mentioned(row.text, names)
        shared = share_reason_pool(planned, pool, weights, called, self.rng)
        # the turn share_reason_pool re-drew (it hands back a new list) came from the draw
        redrawn = ([t.turn_id for t in planned if t.speaker not in called][-1:]
                   if shared is not planned else [])
        planned = shared
        question = game_question(row.text) if self.facts is not None else None
        fitted = None
        if question is not None and not whisper:
            planned, fitted = self._fit_replier(planned, question.topic, weights, called,
                                                line_id=row.id, channel=reply_to)
            # only the first answer gets the facts: the one who knows the topic goes first,
            # after anyone he named
            fit = [t for t in planned if t.turn_id == fitted and t.speaker not in called]
            if fit:
                named = [t for t in planned if t.speaker in called]
                planned = named + fit + [t for t in planned if t not in named and t not in fit]
        lore: dict[str, tuple[str, int]] = {}
        kept = None  # the turn a keeper of the guild's past was put in
        if not whisper and not called and question is None and (
                self.facts is not None or game_question(row.text) is None):
            planned, lore, kept = self._plan_lore(row, planned, weights, reply_to)
        if (not whisper and self._login_quiet(now)
                and classify_human_line(row.text) == "greeting"):
            planned = self._cap_hellos(planned, called)  # the login's 2-3 hellos in all
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
            how = ("whisper" if whisper else "named" if rt.speaker in called
                   else "topic_fit" if rt.turn_id == fitted
                   else "memory" if rt.turn_id == kept
                   else "weighted_draw" if rt.turn_id in redrawn
                   else "reason_pool" if rt.speaker in pool else "weighted_draw")
            facts = row.id if question is not None and rt.kind is TurnKind.REPLY_HUMAN else 0
            told = ""
            if group_chat and rt.kind is TurnKind.GOODNIGHT and self.world.in_group(rt.speaker):
                told = self._mid_raid_text(group_chat, lead=rt.speaker == lead)
            said_lore, pin = lore.get(rt.turn_id, ("", 0))
            items.append((Turn(kind=rt.kind, speaker=rt.speaker,
                               channel=reply_to if rt.kind is TurnKind.GOODNIGHT else channel,
                               to_guid=rt.to_guid, addressee=hg, trigger_line=row.id,
                               instruction=told,
                               delay=delay * self._effects(rt.speaker).delay_mult),
                          TurnMeta(rt.turn_id, answers_human=True, whisper_reply=whisper,
                                   why=f"reply:{how}", facts_line=facts, lore=said_lore,
                                   lore_memory=pin)))
        if (lead and lead not in {rt.speaker for rt in planned} and lead in cands
                and self.world.in_group(lead)):
            items.append((Turn(kind=TurnKind.GOODNIGHT, speaker=lead, channel=group_chat,
                               addressee=hg, trigger_line=row.id,
                               instruction=self._mid_raid_text(group_chat, lead=True),
                               delay=self.rng.uniform(*REPLY_NEXT_S)),
                          TurnMeta(f"reply-{row.id}-lead", answers_human=True,
                                   why="reply:raid_lead")))
        self._queue(items)

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
        In the quiet after his login (reactions.LOGIN_QUIET_S) there are no bursts and no
        whisper triggers; a trigger due then fires after it."""
        if self._session_id is not None and now - self._states_at >= STATE_STEP_S:
            self._advance_states(now)
        self.world.refresh(now)
        self._settle_unacked(now)
        self._gz_after_fight(now)
        if self.world.human_online:
            quiet = self._login_quiet(now)
            plan = self.budget.tick(now, phase=day_phase(self.clock.local()),
                                    human_idle=self.world.human_idle(now),
                                    raid=self.raid.phase(now),
                                    quiet_until=self._login_at + LOGIN_QUIET_S if quiet else None)
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
        """Each trigger is one WhisperPlanner draw: a quiet stretch of guild chat (re-armed by
        the next line), the evening (once a session, reactions._evening_whisper) and entering
        the late hour (once a night, unless the evening's draw was made that night)."""
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
        try:
            await self._burst(plan)
        finally:
            if plan.channel_hint is None:  # raid-cadence plans keep the budget's own clock
                self.budget.burst_done(self.clock.now())

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
        while True:
            now = self.clock.now()
            turn, meta = self._social_turn(kind, speaker, channel, addressee=addressee,
                                           trigger=trigger, room=plan.size - lines, now=now,
                                           delay=self.rng.uniform(*delay), why=why)
            sent = await self._speak(turn, meta)
            lines += len(sent)
            if not sent or lines >= plan.size:
                return
            last, now = speaker, self.clock.now()
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
