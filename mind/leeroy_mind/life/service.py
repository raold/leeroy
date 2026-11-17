"""LifeService: her day for the talk engine (TalkEngine.life).

`configure(cfg)` puts `[life]` in force: it pins the process-wide values (life/config.py) that
Discord presence and the replies on her own time read, and writes the availability to meta
life:availability when it changed, so the console shows what the mind uses. `presence` is where
a member is now and how closely she is watching a medium; None at availability 0 (callers keep
today's behaviour) or for a member without a persona.

`state` is her feeling right now (life/statebook.py), a StateBook only while `[life] state` is on
(None otherwise, so nothing is read or written); the app feeds it every tick, boss and loot event
and each loot lost to his claim through `tick`, `on_event` and `loot_lost`, which do nothing while
it is off. A failure inside them is logged (`life_state_failed op= reason=<class>`) and never
reaches the app's ticker, its event handlers or the guild's loot.

In game (availability above 0; at 0 every caller keeps today's): `weight` is how likely she is the
one who answers (her attention, times her social mood while state is on) and `answer_delay` how
soon (late but at most `game_cap_s` for an answer he is owed, missed past NON_OWED_MAX_S for any
other), both on her state snapshot (STATE_SNAPSHOT_S: StateBook.get reads about three queries,
review-334). `on_logout` (the engine's end_session with him offline) carries each answer to his
whisper not yet said (in its delay, composing or typing, but not at the bridge) to her Discord DM
(CARRY_TEXT): never whispered to an offline player. The carried answers live on through later
logouts (`carried_jobs`); at a mind stop the owed watch keeps them in meta instead
(OwedWatch.persist) and the next start answers them again.

Interruptions (life/interrupt.py, core plan Task 10; availability above 0): `after_line` (the
talk's _after_sent, after each line said) may have her day call her away after a line of hers to
him in their whisper or her DM ("brb boss", 2-6 s later, through Pipeline.say_as); from that line
on she is away until `away_until`. Her answers to him wait: in game `answer_delay` is at least
what is left of it (an answer he is owed at most game_cap_s), in her DM DiscordTalk._due_of defers
his post until she is back; and once back to a conversation he left quiet, `tick` has her say so
half the time ("back"). A line of hers to him in that conversation before then (his urgent words
or goodbye hurried it) ends her away. Aways live in memory only: a restart forgets them."""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import replace

from leeroy_mind.clock import night_of
from leeroy_mind.discord.presence import local_time
from leeroy_mind.logs import log as log_event

from . import config as life_config
from . import interrupt
from .day import Block
from .interrupt import Away, Interruption
from .presence import (Attention, LifePresence, attention_weight, game_cap_s, latency,
                       presence_at)
from .seed import seeded
from .state import State
from .statebook import StateBook

log = logging.getLogger("leeroy_mind.life")

# an answer he is not owed that her latency puts later than this is missed (she never saw it)
NON_OWED_MAX_S = 240.0
# her state is read once per member this often (StateBook.get: about three queries a call)
STATE_SNAPSHOT_S = 30.0
STATE_SNAPSHOTS = 512
# the instruction of her DM answer to a whisper of his still waiting at his logout (on_logout)
CARRY_TEXT = "{He} whispered you in game and logged off before you saw it; answer it here."
CARRY_WHY = "reply:carried"
# her day calls her away mid-conversation (after_line) only while his last line to her (his
# whisper, his DM post) is this fresh, and at most once per member this often
INTERRUPT_FRESH_S = 300.0
INTERRUPT_GAP_S = 1800.0


class LifeService:
    def __init__(self, *, store, clock, personas, world, cfg, engine=None):
        """`engine`: the talk engine whose turns on_logout carries (None: none are)."""
        self.store, self.clock, self.personas, self.world = store, clock, personas, world
        self.engine = engine
        self._availability = 0
        self.state: StateBook | None = None
        self._states: dict[int, tuple[float, State]] = {}  # guid -> (read at, her state)
        self._carried: set = set()  # the jobs carrying an answer to her DM, until each ends
        # interruptions (after_line), in memory only: her last away (kept after it ends for her
        # late answer's excuse, away_between), when she last stepped away, and who is about to
        self._away: dict[int, Away] = {}
        self._interrupted: dict[int, float] = {}
        self._stepping: set[int] = set()
        self.configure(cfg)

    @property
    def availability(self) -> int:
        return self._availability

    def configure(self, cfg) -> None:
        """`[life]` from a (re)loaded config, with the world dials over it. The pin is the
        mind's truth; the meta copy is for the console, so a failed write is logged
        (`life_meta_failed`) and never raised."""
        life = getattr(cfg, "life", None)
        life_config.pin(getattr(life, "availability", 0), getattr(life, "state", False))
        self._availability = life_config.availability()
        self._states.clear()
        if not life_config.state_on():
            self.state = None
        elif self.state is None:
            self.state = StateBook(self.store, self.clock, self.personas, self.world,
                                   lambda: getattr(self.world, "human_guid", 0))
        try:
            if life_config.parse(self.store.get_meta(life_config.META_KEY)) != self._availability:
                self.store.set_meta(life_config.META_KEY, str(self._availability))
        except sqlite3.Error as e:
            log.warning("life_meta_failed reason=%s", type(e).__name__)

    def presence(self, guid: int, *, medium: str,
                 in_his_group: bool = False) -> LifePresence | None:
        """Where she is now and her attention on `medium` ("game" or "discord")."""
        if self._availability <= 0:
            return None
        p = self.personas.get(guid)
        if p is None:
            return None
        her_local = local_time(self.clock.local(), p.identity.region)
        return presence_at(p, her_local, medium=medium, in_his_group=in_his_group)

    def _in_group(self, guid: int) -> bool:
        in_group = getattr(self.world, "in_group", None)
        return bool(in_group is not None and in_group(guid))

    def state_of(self, guid: int) -> State | None:
        """Her state now while `[life] state` is on (None while off, or when it cannot be
        read), read once per member per STATE_SNAPSHOT_S."""
        if self.state is None:
            return None
        now = self.clock.now()
        hit = self._states.get(guid)
        if hit is not None and 0 <= now - hit[0] < STATE_SNAPSHOT_S:
            return hit[1]
        try:
            st = self.state.get(guid)
        except Exception as exc:
            _failed("get", exc)
            return None
        if len(self._states) >= STATE_SNAPSHOTS:
            self._states.clear()
        self._states[guid] = (now, st)
        return st

    def weight(self, guid: int, *, with_him: bool = False) -> float:
        """How likely she is the one who answers in game, relative to a member giving him full
        attention (a member in his group, or `with_him`: in the voice room he speaks in, always
        does): her attention's weight, times 1 + social / 2 while state is on. 1.0 at
        availability 0."""
        if self._availability <= 0:
            return 1.0
        lp = self.presence(guid, medium="game",
                           in_his_group=with_him or self._in_group(guid))
        if lp is None:
            return 1.0
        w = attention_weight(lp.attention, self._availability)
        st = self.state_of(guid)
        return w * (1 + 0.5 * st.social) if st is not None else w

    def answer_delay(self, guid: int, *, rng, owed: bool) -> float | None:
        """Seconds before her answer in game (presence.latency by her attention, energy and
        stress): an answer he is owed at most game_cap_s; any other drawn past NON_OWED_MAX_S
        is None (she missed it). Availability 0 has no draw (ValueError)."""
        if self._availability <= 0:
            raise ValueError("availability 0 keeps today's delays")
        lp = self.presence(guid, medium="game", in_his_group=self._in_group(guid))
        st = self.state_of(guid)
        d = latency(lp.attention if lp is not None else Attention.FULL, self._availability, rng,
                    medium="game", energy=st.energy if st is not None else 0.0,
                    stress=st.stress if st is not None else 0.0)
        # she stepped away (after_line): not before she is back, an owed answer still within
        # game_cap_s (an away from her DM can be longer)
        d = max(d, self.away_until(guid) - self.clock.now())
        if owed:
            return min(d, game_cap_s(self._availability))
        return None if d > NON_OWED_MAX_S else d

    def carry_turn(self, guid: int, line: int, *, delay: float, heard_at: float = 0.0,
                   life_presence=None):
        """(Turn, TurnMeta) of her answer in her Discord DM to his whisper `line`: REPLY_HUMAN
        on dm:<guid> to him, CARRY_TEXT with his forms, owed, his whisper as recall's query
        (the prompt shows it in its "elsewhere" block), after `delay`."""
        from leeroy_mind.talk.pipeline import TurnMeta
        from leeroy_mind.talk.turns import Turn, TurnKind
        pipe = self.engine
        hg = pipe.human.guid
        rows = self.store.chat_lines([line])
        turn = Turn(kind=TurnKind.REPLY_HUMAN, speaker=guid, channel=f"dm:{guid}", to_guid=hg,
                    addressee=hg, delay=max(0.0, delay),
                    instruction=CARRY_TEXT.format(He=pipe.human.forms[0].capitalize()))
        meta = TurnMeta(pipe._new_id(), answers_human=True, owed_answer=True, why=CARRY_WHY,
                        recall_query=rows[0].text if rows else "", heard_at=heard_at,
                        life_presence=life_presence)
        return turn, meta

    def keep(self, job) -> None:
        """A job carrying her answer to her DM: later logouts leave it running (carried_jobs)."""
        if job is not None and not job.done():
            self._carried.add(job)
            job.add_done_callback(self._carried.discard)

    def carried_jobs(self) -> set:
        return {job for job in self._carried if not job.done()}

    def on_logout(self) -> set:
        """He logged off (the engine's end_session with him offline): each answer to his
        whisper not yet said (in its delay, composing or typing, never one at the bridge) is
        dropped `carried` (Pipeline.withdraw) and, per member, her answer goes to her Discord DM
        instead (carry_turn), after what was left of its delay (none once it was composing);
        the owed watch waits for it (OwedWatch.carried). Returns every job carrying an answer
        to her DM, this logout's and earlier ones' still running, which the session's end
        leaves running. Nothing at availability 0; a failure is logged (`life_carry_failed`),
        never raised, and the answers carried before it stay carried."""
        if self._availability <= 0 or self.engine is None:
            return set()
        try:
            self._carry()
        except Exception as exc:
            log_event("life_carry_failed", level=logging.WARNING, reason=type(exc).__name__)
        return self.carried_jobs()

    def _carry(self) -> None:
        from leeroy_mind.talk.pipeline import REPLY_KINDS
        pipe = self.engine
        hg = pipe.human.guid
        if not hg:
            return
        waiting: dict[int, list] = {}
        for task, turn, meta in pipe.waiting_turns(started=True):
            if (turn.channel == "whisper" and turn.to_guid == hg and turn.kind in REPLY_KINDS
                    and meta.answers_human and turn.trigger_line):
                waiting.setdefault(turn.speaker, []).append((task, turn, meta))
        for g, found in waiting.items():
            left = [pipe.withdraw(task, turn, meta, "carried") for task, turn, meta in found]
            line = max(turn.trigger_line for _, turn, _ in found)
            heard = min((meta.heard_at for _, _, meta in found if meta.heard_at), default=0.0)
            turn, meta = self.carry_turn(g, line, delay=min(left), heard_at=heard,
                                         life_presence=found[0][2].life_presence)
            self.keep(pipe._queue([(turn, meta)]))
            log.info("life_carried: guid=%d line=%d", g, line)
            watch = getattr(pipe, "owed", None)
            if watch is not None:
                watch.carried(g, line)

    # -- interruptions and coming back (life/interrupt.py, core plan Task 10) -----------------

    def away_until(self, guid: int) -> float:
        """When she is back from the time she stepped away (after_line); 0 when she is not away,
        and always at availability 0."""
        if self._availability <= 0:
            return 0.0
        a = self._away.get(guid)
        return a.until if a is not None and a.until > self.clock.now() else 0.0

    def last_away(self, guid: int) -> Away | None:
        """Her last time away (after_line), kept after it ends until the next (its night and
        minute seed what follows it); None for none, and always at availability 0."""
        return self._away.get(guid) if self._availability > 0 else None

    def away_between(self, guid: int, start: float, end: float) -> bool:
        """She was away at some time between `start` (his line) and `end` (her answer): her
        late answer says she was away (Pipeline._waited_away). False at availability 0."""
        if self._availability <= 0:
            return False
        a = self._away.get(guid)
        return a is not None and a.since < end and start < a.until

    def after_line(self, pipe, turn, meta) -> None:
        """A line of hers was said (the talk's _after_sent). After her line to him in their
        whisper or her DM, while his last line to her there is under INTERRUPT_FRESH_S old,
        outside his raid's fight, wipe silence and debrief, and with no answer of hers to him
        on its way: her day may call her away (interrupt.draw_interruption, seeded by
        (interrupt, night, her, the minute, her line); her block as her presence on that medium,
        her own game's while she plays in his group), at most once per INTERRUPT_GAP_S. Never
        after a template's line (her own brb and back), the watchdog's rescue or a carried DM.
        Her line goes out 2-6 s later (_step_away). A line of hers to him there while she is
        away (a hurried or capped answer) ends her away (_came_back). Nothing at availability
        0; a failure is logged (`life_interrupt_failed`), never raised."""
        if self._availability <= 0:
            return
        try:
            self._after_line(pipe, turn, meta)
        except Exception as exc:
            log_event("life_interrupt_failed", level=logging.WARNING, reason=type(exc).__name__)

    def _after_line(self, pipe, turn, meta) -> None:
        hg, g = pipe.human.guid, turn.speaker
        if not hg or g == hg:
            return
        if turn.channel == "whisper" and turn.to_guid == hg:
            medium = "game"
        elif turn.channel == f"dm:{g}":
            medium = "discord"
        else:
            return  # a channel, a group DM, a voice room: never mid-conversation with him alone
        if turn.template is not None:
            return  # her own brb and back
        now = self.clock.now()
        if self.away_until(g) > 0:
            self._came_back(g, now)
            return
        if meta.owed_rescue or meta.why == CARRY_WHY or meta.elsewhere:
            return
        if g in self._stepping:
            return
        last = self._interrupted.get(g)
        if last is not None and now - last < INTERRUPT_GAP_S:
            return
        if self._raid_quiet(pipe, now):
            return
        his = self._his_last(g, hg, now - INTERRUPT_FRESH_S)
        if his is None or now - his >= INTERRUPT_FRESH_S or self._owes_him(pipe, turn, g, hg):
            return
        lp = self.presence(g, medium=medium, in_his_group=self._in_group(g))
        if lp is None:
            return
        if getattr(self.world, "human_online", False) and self._in_group(g):
            lp = replace(lp, block=Block.GAMING)  # at her PC playing with him (PM log D15)
        local = self.clock.local()
        night, minute = night_of(local), local.hour * 60 + local.minute
        rng = seeded("interrupt", night, g, minute, self._her_line(g, turn.channel, now))
        cap = (interrupt.WHISPER_CAP_SHARE * game_cap_s(self._availability) if medium == "game"
               else interrupt.DM_CAP_S)
        it = interrupt.draw_interruption(lp, self.state_of(g), self._availability, rng,
                                         cap_s=cap)
        if it is None:
            return
        delay = rng.uniform(*interrupt.DELAY_S)
        away = Away(since=0.0, until=0.0, channel=turn.channel, reason=it.reason, night=night,
                    minute=minute, back=interrupt.draw_back(rng))
        self._stepping.add(g)
        pipe._spawn(self._step_away(pipe, g, it, away, delay, now))

    def _came_back(self, guid: int, now: float) -> None:
        """A line of hers to him in their whisper or her DM while she is away (his urgent words,
        his goodbye or his third line hurried it, or an owed answer's cap came before her away's
        end): she is back from that line, with no back line (`life_back: guid= said=0`), and his
        next line is answered at her usual pace, without the away's excuse."""
        a = self._away.get(guid)
        if a is None or a.done:
            return
        a.until, a.done = now, True
        log.info("life_back: guid=%d said=0", guid)

    def _stays(self, pipe, guid: int, hg: int, said_at: float) -> str:
        """Why she does not step away after all, as her brb is about to go out ("" for none):
        [life] availability went to 0 (`off`), he wrote to her since her line (`his_line`: she
        saw it, she stays and answers it; a brb after his line would read as her answer to it),
        an answer of hers to him is on its way (`answering`), or his raid's fight, wipe silence
        or debrief began (`raid`)."""
        if self._availability <= 0:
            return "off"
        if self._wrote_since(guid, hg, said_at):
            return "his_line"
        if self._owes_him(pipe, None, guid, hg):
            return "answering"
        if self._raid_quiet(pipe, self.clock.now()):
            return "raid"
        return ""

    async def _step_away(self, pipe, guid: int, it: Interruption, away: Away, delay: float,
                         said_at: float) -> None:
        """Her line (it.line) `delay` after hers, through Pipeline.say_as (a pair line: the
        guild's pace never holds it back), as a whisper in game or her DM message. Its barrier,
        just before it goes out, withholds it when she stays after all (_stays: she is not away,
        `life_interrupt_dropped: guid= reason=off|his_line|answering|raid`) and, in a whisper,
        once he has left the game (never a whisper to an offline player). Otherwise she is away
        from then for it.away_s, said or not: refused by the guards, or not sent to an offline
        player, she is still away without a line (core plan Task 10), and so says no back line
        either. Logged `life_interrupt: guid= reason= away_s= said=0|1` (codes, never her
        line)."""
        from leeroy_mind.talk.turns import TurnKind
        hg = pipe.human.guid
        why = ""

        def go(now: float) -> None:
            away.since, away.until = now, now + it.away_s
            self._away[guid] = away

        def barrier() -> bool:
            nonlocal why
            why = self._stays(pipe, guid, hg, said_at)
            if why:
                return False
            if away.channel == "whisper" and not getattr(self.world, "human_online", False):
                why = "offline"
                return False
            go(self.clock.now())
            return True
        said = False
        try:
            await self.clock.sleep(delay * getattr(pipe, "wait_scale", 1.0))
            kind = TurnKind.WHISPER if away.channel == "whisper" else TurnKind.REPLY_HUMAN
            said = await pipe.say_as(guid, away.channel, it.line, kind=kind, to_guid=hg,
                                     pair_line=True, before_transport=barrier)
        finally:
            self._stepping.discard(guid)
        if not said:
            if not why and self._away.get(guid) is not away:
                why = self._stays(pipe, guid, hg, said_at)  # refused before its barrier
            if why not in ("", "offline"):
                log.info("life_interrupt_dropped: guid=%d reason=%s", guid, why)
                return
            if self._away.get(guid) is not away:
                go(self.clock.now())
            away.back = ""  # he never saw her brb: no "back" either
        self._interrupted[guid] = away.since
        log.info("life_interrupt: guid=%d reason=%s away_s=%d said=%d", guid, it.reason,
                 int(it.away_s), int(bool(said)))

    def _returns(self, now: float) -> None:
        """Each away that has ended: she is back. To a conversation he left quiet since her brb
        (no line of his to her in either medium) she says her drawn back line (half the time;
        a whisper only while he is in game), else nothing. Logs `life_back: guid= said=0|1`."""
        for guid, a in list(self._away.items()):
            if a.done or a.until > now:
                continue
            a.done = True
            pipe = self.engine
            hg = pipe.human.guid if pipe is not None else 0
            if (not a.back or not hg or self._availability <= 0
                    or self._wrote_since(guid, hg, a.since)
                    or (a.channel == "whisper"
                        and not getattr(self.world, "human_online", False))):
                log.info("life_back: guid=%d said=0", guid)
                continue
            pipe._spawn(self._say_back(pipe, guid, a))

    async def _say_back(self, pipe, guid: int, a: Away) -> None:
        """Her back line, as her brb went (a pair line), withheld if he writes first, his
        raid's fight began or (a whisper) he has left the game."""
        from leeroy_mind.talk.turns import TurnKind
        hg = pipe.human.guid

        def barrier() -> bool:
            return not (self._wrote_since(guid, hg, a.since)
                        or self._raid_quiet(pipe, self.clock.now())
                        or (a.channel == "whisper"
                            and not getattr(self.world, "human_online", False)))
        kind = TurnKind.WHISPER if a.channel == "whisper" else TurnKind.REPLY_HUMAN
        said = await pipe.say_as(guid, a.channel, a.back, kind=kind, to_guid=hg, pair_line=True,
                                 before_transport=barrier)
        log.info("life_back: guid=%d said=%d", guid, int(bool(said)))

    def _raid_quiet(self, pipe, now: float) -> bool:
        """His raid's fight, wipe silence or debrief: nobody steps away or comes back aloud."""
        from leeroy_mind.talk.raid import RaidPhase
        raid = getattr(pipe, "raid", None)
        return raid is not None and raid.phase(now) in (
            RaidPhase.COMBAT, RaidPhase.WIPE_SILENCE, RaidPhase.WIPE_DEBRIEF)

    def _his_last(self, guid: int, hg: int, since: float) -> float | None:
        """The time of his last line to her at `since` or later: his whisper, or his post in
        her DM (one conversation, two media); None for none. Reads only the pair's rows."""
        conn = self.store.conn
        whisper = conn.execute(
            "SELECT max(ts) FROM chat_line WHERE channel = 'whisper' AND ts >= ? "
            "AND origin = 'human' AND from_guid = ? AND to_guid = ?",
            (since, hg, guid)).fetchone()[0]
        dm = conn.execute(
            "SELECT max(ts) FROM discord_message WHERE channel = ? AND ts >= ? AND author = ? "
            "AND origin = 'human' AND deleted IS NULL", (f"dm:{guid}", since, hg)).fetchone()[0]
        times = [t for t in (whisper, dm) if t is not None]
        return max(times) if times else None

    def _wrote_since(self, guid: int, hg: int, t: float) -> bool:
        """He wrote to her at `t` or later (the same instant counts: never a brb or back after
        a line of his she has not answered)."""
        return self._his_last(guid, hg, t) is not None

    def _her_line(self, guid: int, channel: str, now: float) -> int:
        """Her newest line on `channel` (the one just said): the interruption's seed."""
        row = self.store.conn.execute(
            "SELECT max(id) FROM chat_line WHERE channel = ? AND ts >= ? AND from_guid = ? "
            "AND origin = 'mind'", (channel, now - INTERRUPT_FRESH_S, guid)).fetchone()
        return row[0] or 0

    def _owes_him(self, pipe, turn, guid: int, hg: int) -> bool:
        """An answer of hers to him is still on its way (a turn of the engine's other than
        `turn`), or his DM post waits for her own time: she does not step away from it."""
        from leeroy_mind.discord.talk import DEFERRED_KEY
        from leeroy_mind.talk.pipeline import REPLY_KINDS
        for t in pipe.planned_turns():
            if (t is turn or t.speaker != guid or t.kind not in REPLY_KINDS
                    or t.template is not None):  # her own brb and back answer nothing
                continue
            if t.to_guid == hg or t.addressee == hg or t.channel == f"dm:{guid}":
                return True
        return self.store.get_meta(DEFERRED_KEY.format(guid=guid)) is not None

    def tick(self, now: float) -> None:
        if self._away:
            try:
                self._returns(now)
            except Exception as exc:
                log_event("life_back_failed", level=logging.WARNING, reason=type(exc).__name__)
        if self.state is not None:
            try:
                self.state.tick(now)
            except Exception as exc:
                _failed("tick", exc)

    async def on_event(self, ev) -> None:
        if self.state is not None:
            try:
                await self.state.on_event(ev)
            except Exception as exc:
                _failed("event", exc)

    def loot_lost(self, guid: int) -> None:
        if self.state is not None:
            try:
                self.state.loot_lost(guid)
            except Exception as exc:
                _failed("loot_lost", exc)


def _failed(op: str, exc: Exception) -> None:
    log_event("life_state_failed", level=logging.WARNING, op=op, reason=type(exc).__name__)
