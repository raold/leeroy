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
(OwedWatch.persist) and the next start answers them again."""
from __future__ import annotations

import logging
import sqlite3

from leeroy_mind.discord.presence import local_time
from leeroy_mind.logs import log as log_event

from . import config as life_config
from .presence import (Attention, LifePresence, attention_weight, game_cap_s, latency,
                       presence_at)
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


class LifeService:
    def __init__(self, *, store, clock, personas, world, cfg, engine=None):
        """`engine`: the talk engine whose turns on_logout carries (None: none are)."""
        self.store, self.clock, self.personas, self.world = store, clock, personas, world
        self.engine = engine
        self._availability = 0
        self.state: StateBook | None = None
        self._states: dict[int, tuple[float, State]] = {}  # guid -> (read at, her state)
        self._carried: set = set()  # the jobs carrying an answer to her DM, until each ends
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

    def tick(self, now: float) -> None:
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
