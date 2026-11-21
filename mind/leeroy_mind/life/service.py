"""LifeService: her day for the talk engine (TalkEngine.life).

`configure(cfg)` puts `[life]` in force: it pins the process-wide values (life/config.py) that
Discord presence and the replies on her own time read (availability, state and dm_wait_cap_s,
the longest his DM waits), and writes the availability to meta life:availability and the cap to
life:dm_wait_cap_s when they changed, so the console and health show what the mind uses. `presence` is where
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
or goodbye hurried it) ends her away. While she is away nothing of hers he could see goes out but
her answers to him (`is_away`: Pipeline._blocked `stepped_away`, the speaker pools, Discord's
channel answers). Aways live in memory only: a restart forgets them (meta life:away:<guid> is
written for health's owner-replies alone)."""
from __future__ import annotations

import logging
import json
import math
import sqlite3
from dataclasses import replace

from leeroy_mind.clock import night_of
from leeroy_mind.discord.presence import her_local
from leeroy_mind.logs import log as log_event

from . import config as life_config
from leeroy_mind.guards import pronouns

from . import day, interrupt
from .day import Block
from .interrupt import Away, Interruption
from .presence import (Attention, LifePresence, stated_where, attention_weight, game_cap_s, latency,
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
# while she is away (after_line) she is this much less likely to be the one who answers him in
# game (weight): another member takes a line of his she would otherwise have answered
AWAY_WEIGHT = 0.1
# a DM post of his this fresh that no answer of hers replies to yet keeps her (_dm_unanswered)
OPEN_POST_S = 86400.0
# meta life:away:<guid> = "<since>,<until>;..." (unix seconds, numbers only): recent aways, for
# health's owner-replies bound; the mind never reads it back (a restart forgets every away) and
# drops a key AWAY_KEEP_S after its away ended (health looks back a day)
AWAY_KEY = "life:away:{guid}"
AWAY_KEEP_S = 86400.0
AWAY_MAX = 64  # a day's half-hour interruptions plus intervals overlapping its edge


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
        self._presence_at: float | None = None
        self._presence_members: tuple[int, ...] = ()
        self.configure(cfg)

    @property
    def availability(self) -> int:
        return self._availability

    def configure(self, cfg) -> None:
        """`[life]` from a (re)loaded config, with the world dials over it. The pin is the
        mind's truth; the meta copy is for the console, so a failed write is logged
        (`life_meta_failed`) and never raised."""
        life = getattr(cfg, "life", None)
        # [talk] coherence reaches the pure readers through their module switches (read here,
        # so at the mind's start and at each of his logins): her job's shape (life/day.py), her
        # own boss (life/interrupt.py) and "tell X he ..." (guards/pronouns.py)
        coherent = getattr(getattr(cfg, "talk", None), "coherence", False) is True
        day.COHERENT = interrupt.COHERENT = pronouns.COHERENT = coherent
        cap = getattr(life, "dm_wait_cap_s", 0)
        life_config.pin(getattr(life, "availability", 0), getattr(life, "state", False),
                        cap if type(cap) is int else 0)
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
            if life_config.stored_cap(self.store.conn) != life_config.dm_wait_cap():
                self.store.set_meta(life_config.CAP_KEY, str(life_config.dm_wait_cap()))
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
        her = her_local(self.clock.local(), p)
        return presence_at(p, her, medium=medium, in_his_group=in_his_group)

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
        if self.is_away(guid):  # she stepped away (after_line): someone else answers first
            w *= AWAY_WEIGHT
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

    def is_away(self, guid: int) -> bool:
        """She stepped away and is not back yet (after_line): nothing of hers he can see goes
        out but her answers to him, which wait (Pipeline._blocked `stepped_away`); always False
        at availability 0."""
        return self.away_until(guid) > 0

    def last_away(self, guid: int) -> Away | None:
        """Her last time away (after_line), kept after it ends until the next (its night and
        minute seed what follows it); None for none, and always at availability 0."""
        return self._away.get(guid) if self._availability > 0 else None

    def away_between(self, guid: int, start: float, end: float) -> bool:
        """She was away at some time between `start` (his line) and `end` (her answer), and he
        wrote it at least interrupt.EXCUSE_MIN_S before she was back (else there is no lateness
        to excuse): her late answer says she was away (Pipeline._waited_away). False at
        availability 0."""
        if self._availability <= 0:
            return False
        a = self._away.get(guid)
        return (a is not None and a.since < end
                and start < a.until - interrupt.EXCUSE_MIN_S)

    def after_line(self, pipe, turn, meta) -> None:
        """A line of hers was said (the talk's _after_sent). After her line to him in their
        whisper or her DM, while his last line to her there is under INTERRUPT_FRESH_S old,
        outside his raid's fight, wipe silence and debrief, and with no answer of hers to him
        on its way: her day may call her away (interrupt.draw_interruption, seeded by
        (interrupt, night, her, the minute, her line); her block as her presence on that medium,
        her own game's while she plays in his group), at most once per INTERRUPT_GAP_S, never
        within interrupt.MIN_AFTER_HIS_S of his line, after his bad news or in an explicit scene
        he is turning up (_holds_her). Never
        after a template's line (her own brb and back), the watchdog's rescue or a carried DM.
        Her line goes out 2-6 s later (_step_away). A line of hers to him there while she is
        away, or an answer he is owed anywhere (a hurried or capped answer), ends her away
        (_came_back). Nothing at availability 0; a failure is logged (`life_interrupt_failed`),
        never raised."""
        if self._availability <= 0:
            return
        try:
            self._after_line(pipe, turn, meta)
        except Exception as exc:
            log_event("life_interrupt_failed", level=logging.WARNING, reason=type(exc).__name__)

    def _after_line(self, pipe, turn, meta) -> None:
        from leeroy_mind.talk.owed import owes_him
        hg, g = pipe.human.guid, turn.speaker
        if not hg or g == hg or turn.template is not None:
            return  # nobody to step away from, or her own brb and back
        if turn.channel == "whisper" and turn.to_guid == hg:
            medium = "game"
        elif turn.channel == f"dm:{g}":
            medium = "discord"
        else:
            medium = ""  # a channel, a group DM, a voice room: never alone with him
        now = self.clock.now()
        if self.away_until(g) > 0:
            if medium or owes_him(pipe, turn, meta):
                self._came_back(g, now)
            return
        if not medium:
            return
        if meta.owed_rescue or meta.why == CARRY_WHY or meta.elsewhere:
            return
        if g in self._stepping:
            return
        last = self._interrupted.get(g)
        if last is not None and now - last < INTERRUPT_GAP_S:
            return
        if self._quiet(pipe, now):
            return
        his = self._his_last(g, hg, now - INTERRUPT_FRESH_S)
        if his is None or now - his >= INTERRUPT_FRESH_S or self._owes_him(pipe, turn, g, hg):
            return
        if now - his < interrupt.MIN_AFTER_HIS_S:
            return  # he just wrote: she is in the conversation
        lp = self.presence(g, medium=medium, in_his_group=self._in_group(g))
        p = self.personas.get(g)
        if lp is None or p is None:
            return
        if self._holds_her(pipe, turn, p, self._his_last_text(g, hg, his)):
            return
        if getattr(self.world, "human_online", False) and self._in_group(g):
            lp = replace(lp, block=Block.GAMING)  # at her PC playing with him (PM log D15)
        else:
            lp = self._where_she_said(pipe, turn, meta, lp, now)
        local = self.clock.local()
        night, minute = night_of(local), local.hour * 60 + local.minute
        rng = seeded("interrupt", night, g, minute, self._her_line(g, turn.channel, now))
        cap = (interrupt.WHISPER_CAP_SHARE * game_cap_s(self._availability) if medium == "game"
               else interrupt.DM_CAP_S)
        it = interrupt.draw_interruption(lp, self.state_of(g), self._availability, rng,
                                         cap_s=cap, boss=interrupt.own_boss(p),
                                         car=interrupt.drives(p),
                                         place=interrupt.workplace(p))
        if it is None:
            return
        delay = rng.uniform(*interrupt.DELAY_S)
        away = Away(since=0.0, until=0.0, channel=turn.channel, reason=it.reason, night=night,
                    minute=minute, back=interrupt.draw_back(rng))
        self._stepping.add(g)
        try:
            pipe._spawn(self._step_away(pipe, g, it, away, delay, now))
        except BaseException:
            self._stepping.discard(g)  # never stuck: her next line may draw again
            raise

    @staticmethod
    def _where_she_said(pipe, turn, meta, lp, now: float):
        """Her presence with the block of where she said she is in this conversation ([talk]
        coherence, presence.stated_where): home or in bed while her plan has her at work reads
        as free time (never "brb boss" from the sofa), at work while her plan has her free as
        work. Unchanged when she said nothing, it agrees, or the switch is off."""
        coherent = getattr(pipe, "_coherent", None)
        said = getattr(pipe, "_said_here", None)
        if coherent is None or said is None or not coherent():
            return lp
        try:
            stated = stated_where(said(turn, meta, now))
        except Exception as e:  # colour only: her plan's block stands
            log.warning("life_where_failed reason=%s", type(e).__name__)
            return lp
        if stated in ("home", "bed") and lp.block in (Block.WORK, Block.COMMUTE):
            return replace(lp, block=Block.FREE)
        if stated == "work" and lp.block in (Block.FREE, Block.GAMING, Block.HOME_DUTY):
            return replace(lp, block=Block.WORK)
        return lp

    def _came_back(self, guid: int, now: float) -> None:
        """A line of hers to him in their whisper or her DM while she is away (his urgent words,
        his goodbye or his third line hurried it, or an owed answer's cap came before her away's
        end): she is back from that line, with no back line (`life_back: guid= said=0`), and his
        next line is answered at her usual pace, without the away's excuse."""
        a = self._away.get(guid)
        if a is None or a.done:
            return
        a.until, a.done = now, True
        self._mark(guid, a)
        log.info("life_back: guid=%d said=0", guid)

    def _mark(self, guid: int, a: Away) -> None:
        """Keep each recent away for health; an early return replaces that same interval.
        Intervals over for AWAY_KEEP_S are dropped in the same transaction. A
        failed write is logged (`life_meta_failed`), never raised: the away holds either way."""
        try:
            with self.store.transaction():
                key = AWAY_KEY.format(guid=guid)
                history = dict(parse_aways(self.store.get_meta(key)))
                history[round(a.since)] = round(a.until)
                self.store.set_meta(key, ";".join(f"{s:.0f},{u:.0f}" for s, u in sorted(history.items())))
                for key, value in self.store.conn.execute(
                        "SELECT key, value FROM meta WHERE key GLOB 'life:away:*'").fetchall():
                    kept = [(s, u) for s, u in parse_aways(value)
                            if u >= self.clock.now() - AWAY_KEEP_S]
                    if kept:
                        self.store.set_meta(key, ";".join(f"{s:.0f},{u:.0f}" for s, u in kept))
                    else:
                        self.store.conn.execute("DELETE FROM meta WHERE key = ?", (key,))
        except sqlite3.Error as e:
            log.warning("life_meta_failed reason=%s", type(e).__name__)

    def _stays(self, pipe, guid: int, hg: int, said_at: float) -> str:
        """Why she does not step away after all, as her brb is about to go out ("" for none):
        [life] availability went to 0 (`off`), he wrote to her since her line (`his_line`: she
        saw it, she stays and answers it; a brb after his line would read as her answer to it),
        an answer of hers to him is on its way or still owed (`answering`, _owes_him), or a
        quiet began (_quiet: `raid`, `boss`, `afk`, `login`)."""
        if self._availability <= 0:
            return "off"
        if self._wrote_since(guid, hg, said_at):
            return "his_line"
        if self._owes_him(pipe, None, guid, hg):
            return "answering"
        return self._quiet(pipe, self.clock.now())

    async def _step_away(self, pipe, guid: int, it: Interruption, away: Away, delay: float,
                         said_at: float) -> None:
        """Her line (it.line) `delay` after hers, through Pipeline.say_as (a pair line: the
        guild's pace never holds it back), as a whisper in game or her DM message. Its barrier,
        just before it goes out, withholds it when she stays after all (_stays: she is not away,
        `life_interrupt_dropped: guid= reason=off|his_line|answering|raid|boss|afk|login`)
        and, in a whisper,
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
            self._mark(guid, away)

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
            if (not a.back or not hg or self._availability <= 0 or self._back_held(pipe, guid, a)):
                log.info("life_back: guid=%d said=0", guid)
                continue
            pipe._spawn(self._say_back(pipe, guid, a))

    def _back_held(self, pipe, guid: int, a: Away) -> bool:
        """Her back line is not said: he wrote to her since her brb, an answer of hers to him is
        on its way or owed anywhere (it says she was away: one excuse, review-353 P3-4), a quiet
        (_quiet), or, in a whisper, he has left the game."""
        hg = pipe.human.guid
        return bool(self._wrote_since(guid, hg, a.since) or self._owes_him(pipe, None, guid, hg)
                    or self._quiet(pipe, self.clock.now())
                    or (a.channel == "whisper"
                        and not getattr(self.world, "human_online", False)))

    async def _say_back(self, pipe, guid: int, a: Away) -> None:
        """Her back line, as her brb went (a pair line), withheld when _back_held says so just
        before it goes out."""
        from leeroy_mind.talk.turns import TurnKind
        hg = pipe.human.guid
        kind = TurnKind.WHISPER if a.channel == "whisper" else TurnKind.REPLY_HUMAN
        said = await pipe.say_as(guid, a.channel, a.back, kind=kind, to_guid=hg, pair_line=True,
                                 before_transport=lambda: not self._back_held(pipe, guid, a))
        log.info("life_back: guid=%d said=%d", guid, int(bool(said)))

    def _quiet(self, pipe, now: float) -> str:
        """Why nobody steps away or comes back aloud now ("" for no reason), as nothing starts a
        conversation with him then (Reactions._initiation_held, _login_quiet): his raid's fight,
        wipe silence or debrief (`raid`), a boss fight of his group (`boss`), his AFK (`afk`),
        the quiet after his login (`login`)."""
        from leeroy_mind.talk.raid import RaidPhase
        raid = getattr(pipe, "raid", None)
        if raid is not None and raid.phase(now) in (
                RaidPhase.COMBAT, RaidPhase.WIPE_SILENCE, RaidPhase.WIPE_DEBRIEF):
            return "raid"
        boss = getattr(pipe, "_in_boss_fight", None)
        if boss is not None and boss(now):
            return "boss"
        if getattr(getattr(pipe, "world", None), "human_afk", False):
            return "afk"
        login = getattr(pipe, "_login_quiet", None)
        if login is not None and login(now):
            return "login"
        return ""

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

    def _his_last_text(self, guid: int, hg: int, ts: float) -> str:
        """His line to her at `ts` (_his_last's time): his whisper or his post in her DM; ""
        when it is gone. Read to weigh it, never logged."""
        conn = self.store.conn
        row = conn.execute(
            "SELECT text FROM discord_message WHERE channel = ? AND ts = ? AND author = ? "
            "AND origin = 'human' AND deleted IS NULL ORDER BY id DESC LIMIT 1",
            (f"dm:{guid}", ts, hg)).fetchone()
        if row is None:
            row = conn.execute(
                "SELECT text FROM chat_line WHERE channel = 'whisper' AND ts = ? "
                "AND origin = 'human' AND from_guid = ? AND to_guid = ? ORDER BY id DESC LIMIT 1",
                (ts, hg, guid)).fetchone()
        return row[0] if row is not None and isinstance(row[0], str) else ""

    def _holds_her(self, pipe, turn, p, his: str) -> bool:
        """His last line keeps her in the conversation (owner, 2026-11-17): bad news of his
        (talk.hisline.bad_news: nobody says "brb" to "my dad is in hospital"), or an explicit
        scene he is turning up (the explicit overlay in force in this whisper or DM and
        romance.his_heat on his line)."""
        from leeroy_mind.talk.hisline import bad_news
        from leeroy_mind.talk.romance import dm_explicit, explicit_whisper, his_heat
        if not his:
            return False
        if bad_news(his):
            return True
        if not his_heat(his):
            return False
        hg = pipe.human.guid
        rel = self.store.get_relationship(turn.speaker, hg, lens=True)
        dial = pipe.cfg.romance.dial
        return (explicit_whisper(p, rel, dial=dial, channel=turn.channel)
                or dm_explicit(p, rel, dial=dial, channel=turn.channel,
                               data_dir=pipe.data_dir))

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
        `turn`) or still owed: his DM post waits for her own time, a fresh DM post of his has no
        answer of hers replying to it (_dm_unanswered: its answer was dropped and waits for the
        owed watch's rescue, review-353 P1-1), or the owed watch holds a stretch of his to her
        (OwedWatch.open_for). She does not step away from it, nor say she is back."""
        from leeroy_mind.discord.talk import DEFERRED_KEY
        from leeroy_mind.talk.pipeline import REPLY_KINDS
        for t in pipe.planned_turns():
            if (t is turn or t.speaker != guid or t.kind not in REPLY_KINDS
                    or t.template is not None):  # her own brb and back answer nothing
                continue
            if t.to_guid == hg or t.addressee == hg or t.channel == f"dm:{guid}":
                return True
        if self.store.get_meta(DEFERRED_KEY.format(guid=guid)) is not None:
            return True
        if self._dm_unanswered(guid, hg):
            return True
        open_for = getattr(getattr(pipe, "owed", None), "open_for", None)
        return bool(open_for is not None and open_for(guid))

    def _dm_unanswered(self, guid: int, hg: int) -> bool:
        """His newest live post in her DM, of the last OPEN_POST_S, is newer than every post of
        his her messages there reply to: his last post has no answer yet. Reads only the pair's
        rows."""
        conn = self.store.conn
        channel = f"dm:{guid}"
        his = conn.execute(
            "SELECT max(id) FROM discord_message WHERE channel = ? AND origin = 'human' "
            "AND author = ? AND deleted IS NULL AND ts >= ?",
            (channel, hg, self.clock.now() - OPEN_POST_S)).fetchone()[0]
        if his is None:
            return False
        hers = conn.execute(
            "SELECT max(reply_to) FROM discord_message WHERE channel = ? AND origin = 'mind' "
            "AND author = ?", (channel, guid)).fetchone()[0]
        return hers is None or his > hers

    def tick(self, now: float) -> None:
        if self._away:
            try:
                self._returns(now)
            except Exception as exc:
                log_event("life_back_failed", level=logging.WARNING, reason=type(exc).__name__)
        # This service ticks even when the optional server engine is disabled. Publish only
        # active process state, so a restart never revives the health-only away history.
        members = tuple(sorted(g for g, away in self._away.items()
                               if self._availability > 0 and away.until > now))
        if (self._presence_at is None or members != self._presence_members
                or now - self._presence_at >= 60 or now < self._presence_at):
            try:
                self.store.set_meta(life_config.STEPPED_KEY, json.dumps({'guids': members, 'at': now}))
                self._presence_at, self._presence_members = now, members
            except sqlite3.Error as exc:
                log.warning("life_meta_failed reason=%s", type(exc).__name__)
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


def parse_away(value) -> tuple[float, float] | None:
    """(since, until) from a meta life:away:<guid> value; None for anything else."""
    try:
        since, until = (float(x) for x in str(value).split(","))
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(since) and math.isfinite(until) and since <= until):
        return None
    return since, until


def parse_aways(value) -> tuple[tuple[float, float], ...]:
    """Bounded history, also accepting the old single-interval value. Never restores live aways."""
    if not isinstance(value, str) or len(value) > 4096:
        return ()
    return tuple(sorted({a for item in value.split(";") if (a := parse_away(item)) is not None}))[-AWAY_MAX:]


def _failed(op: str, exc: Exception) -> None:
    log_event("life_state_failed", level=logging.WARNING, op=op, reason=type(exc).__name__)
