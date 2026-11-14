"""Bounded, private openers while he is away; their receipts belong to the pipeline."""
import asyncio
import math
import random
from datetime import timedelta

from leeroy_mind.discord import layout
from leeroy_mind.discord.channels import channel_id
from leeroy_mind.discord.presence import RNG_SEED, local_time, presence_of
from leeroy_mind.discord.transport import DiscordTarget
from leeroy_mind.llm.scheduler import Priority
from leeroy_mind.talk.budget import day_phase
from leeroy_mind.talk.guards import Guards
from leeroy_mind.talk.pipeline import TurnMeta
from leeroy_mind.talk.romance import WhisperPlanner, forward, romance_lift, whisper_kind
from leeroy_mind.talk.turns import Turn, TurnKind

from . import runtime
from .clock import in_window
from .rhythm import PHASE_RATE
from .records import BadDayCounter, counts
from .state import pulled_into_play

OPEN_KEY = 'server:dm:open:{guid}'
DAY_MEMBER_KEY = 'server:dm:day:{date}:{guid}'
REOPEN_S = 48*3600.0
RETRY_S = 300.0


def _number(value):
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    except (ValueError,TypeError):
        return None


class DmOpeners:
    def __init__(self, app, config, rng: random.Random):
        self.app, self.config, self.rng = app, config, rng
        self._planner = WhisperPlanner(rng)
        self._guards = Guards(rng=rng)
        self._day = None
        self._next_at = None
        self._pending = None
        self._pending_guid = None
        self._lock = asyncio.Lock()
        self._closed = False
        self._weighted_hours = None
        self._human_generation = {}
        self._human_until = {}
        self._was_in_game = bool(app.world.human_online)

    def his_post(self, channel: str) -> None:
        """His message owns this DM now; discard a planned opener and leave D1 the floor."""
        guid = layout.dm_peer(channel_id(channel))
        if guid is not None:
            self.answer_queued(guid)

    def answer_queued(self, guid: int) -> None:
        """An owed answer gets this member's slot, including during opener selection."""
        if type(guid) is not int or guid<=0:
            return
        self._human_generation[guid] = self._human_generation.get(guid,0)+1
        self._human_until[guid] = self.app.clock.now()+RETRY_S
        if self._pending_guid == guid and self._pending is not None:
            self._pending.cancel()

    def _ready(self):
        app = self.app
        if app.world.human_online:
            # Delivery can observe a login while the ticker awaits this opener.
            self._was_in_game = True
            return False
        return (not self._closed and self.config.enabled and self.config.dms_per_day>0
                and app.engine.cfg.talk.chattiness>0
                and not getattr(app,'stopping',False)
                and app.human_guid>0 and getattr(app,'server',None) is not None
                and app.server.lease.ready() and not in_window(app.clock.local(),self.config.quiet_window)
                and app.engine.live_review.config.enabled)

    def _gap(self, local):
        if self._weighted_hours is None:
            midnight = local.replace(hour=0,minute=0,second=0,microsecond=0)
            self._weighted_hours = sum(PHASE_RATE[day_phase(at)]/60 for minute in range(1440)
                if not in_window(at:=midnight+timedelta(minutes=minute),self.config.quiet_window))
        rate = (self.config.dms_per_day*PHASE_RATE[day_phase(local)]/self._weighted_hours
                *self.app.engine.cfg.talk.chattiness/4)
        if rate<=0:
            return math.inf
        return max(RETRY_S,self.rng.expovariate(rate/3600))

    def _can_open(self, guid, now, day, meta):
        if now<self._human_until.get(guid,0):
            return False
        outreach = getattr(self.app, 'outreach', None)
        if outreach is not None and not outreach.eligible(guid):
            return False
        owes = getattr(getattr(self.app,'discord_talk',None),'owes',None)
        if owes is not None and owes(guid):
            return False  # she owes him an answer she has not seen yet: no opener on top
        if DAY_MEMBER_KEY.format(date=day,guid=guid) in meta:
            return False
        raw = meta.get(OPEN_KEY.format(guid=guid))
        opened = _number(raw)
        if raw is not None and opened is None:
            return False
        if opened is None or now-opened>=REOPEN_S:
            return True
        # Also tolerate a reply recorded before the desk's open-key hook was installed.
        return self.app.store.conn.execute('SELECT 1 FROM discord_message WHERE channel=? '
            "AND author=? AND origin='human' AND deleted IS NULL AND ts>=? AND ts<=? LIMIT 1",
            (f'dm:{guid}',self.app.human_guid,opened,now)).fetchone() is not None

    def _day_count(self, day):
        try:
            return counts(self.app.store,day)['dms']
        except BadDayCounter:
            return math.inf  # the shared parser reports corruption once, even before a draft

    def valid(self, turn, meta=None):
        """Cheap checks repeated at composition and transport, including a newly logged-in owner."""
        if not self._ready():
            return False
        app, guid, now = self.app, turn.speaker, self.app.clock.now()
        if turn.to_guid != app.human_guid or turn.addressee != app.human_guid:
            return False
        if any(queued.speaker==guid for queued in app.engine.planned_turns()):
            return False
        if guid in app.engine.active_speakers(exclude_task=self._pending):
            return False
        p, member = app.personas.get(guid), app.world.discord_members().get(guid)
        if p is None or member is None or guid in pulled_into_play(app):
            return False
        presence = presence_of(p,local=local_time(app.clock.local(),p.identity.region),
            body_online=guid in app.world.online,body_zone=member.zone,rng_seed=RNG_SEED)
        if presence.state not in {'online','idle'}:
            return False
        if app.store.conn.execute('SELECT 1 FROM away WHERE guid=? AND returned_at IS NULL '
            'AND (returns_at IS NULL OR returns_at>?) LIMIT 1',(guid,now)).fetchone():
            return False
        day = app.clock.local().date().isoformat()
        keys = ('server:day:'+day,OPEN_KEY.format(guid=guid),DAY_MEMBER_KEY.format(date=day,guid=guid))
        current = dict(app.store.conn.execute('SELECT key,value FROM meta WHERE key IN (?,?,?)',keys))
        return self._day_count(day)<self.config.dms_per_day and self._can_open(guid,now,day,current)

    async def tick(self, now: float) -> None:
        # Observe play even while an earlier opener is still finishing. On logout,
        # draw a fresh gap instead of spending a deadline that expired in game.
        if self.app.world.human_online:
            self._was_in_game = True
            return
        if self._lock.locked():
            return
        async with self._lock:
            await self._tick(now)

    async def _tick(self, now: float) -> None:
        if not self._ready() or self._pending is not None:
            return
        local = self.app.clock.local()
        day = local.date().isoformat()
        if self._day != day or self._was_in_game:
            self._was_in_game = False
            self._day, self._next_at = day, now+self._gap(local)
        if now<self._next_at:
            return
        self._next_at = now+self._gap(local)
        generations = dict(self._human_generation)
        snap = await runtime.snapshot(self.app,now,include_seeds=True)
        if not self._ready() or self._day_count(day)>=self.config.dms_per_day:
            return
        busy = {turn.speaker for turn in self.app.engine.planned_turns()}
        busy.update(self.app.engine.active_speakers())
        candidates = {g:(p,snap.relationships.get(g)) for g,p in snap.personas.items()
            if g!=snap.human and g not in snap.in_play and g not in snap.away and g not in busy
            and self._human_generation.get(g,0)==generations.get(g,0)
            and snap.states[g].presence.state in {'online','idle'} and self._can_open(g,now,day,snap.meta)}
        guid = self._planner.start(now,candidates,lift=romance_lift(self.app.engine.cfg.romance.dial))
        if guid is None:
            return
        p = snap.personas[guid]
        outreach = getattr(self.app, 'outreach', None)
        item = outreach.select(guid, 'dm') if outreach is not None else None
        own = [s for s in snap.seeds if s.kind in {'life','plan'} and guid in s.about and not s.officers_only]
        if outreach is not None:
            own = [s for s in own if outreach.fallback_seed(guid, s)]
        topic = self.rng.choice(own).fact if own else self.app.engine._to_him_topic(p,now)
        # A bound item's words come from its own hook; this topic is the opener's if a photo takes it.
        turn = Turn(TurnKind.OPEN,guid,f'dm:{guid}',to_guid=snap.human,addressee=snap.human,
            priority=Priority.BACKGROUND,max_lines=1,
            instruction='Start a private Discord message to him in your own words.'+('' if item else ' '+topic))
        meta = TurnMeta(self.app.engine._new_id(),why='server:dm',initiates=True,
            casual=whisper_kind(p)=='casual' or forward(p)<=0,topic=topic,server_anchor=topic,
            discord=DiscordTarget(None,None),single_line=True,review_required=True)
        meta.guards = self._guards
        meta.keep_channel = True
        generation = generations.get(guid,0)
        meta.still_valid = lambda:(self._human_generation.get(guid,0)==generation and self.valid(turn,meta))
        if item is not None and not outreach.bind(turn, meta, item):
            return
        if not meta.still_valid():
            if item is not None:
                outreach.release(meta)
            return
        self._pending_guid = guid
        self._pending = asyncio.create_task(self.app.engine.speak_meta(turn,meta))
        try:
            await self._pending
        except asyncio.CancelledError:
            # Canceling just the opener must not cancel the long-lived 30-second ticker.
            if asyncio.current_task().cancelling():
                raise
        finally:
            self._pending = None
            self._pending_guid = None

    def intent_valid(self, turn) -> bool:
        """An opener he asked for (godmode dm_him): the clocks' own gates (he is away, the lease,
        the guards, chattiness, his newer post), her presence and freedom, and no answer she still
        owes him; not the day's budget or the 48-hour spacing, which pace her own openers."""
        app = self.app
        if (app.world.human_online or self._closed or not self.config.enabled
                or app.engine.cfg.talk.chattiness <= 0 or getattr(app, 'stopping', False)
                or app.human_guid <= 0 or getattr(app, 'server', None) is None
                or not app.server.lease.ready()
                or in_window(app.clock.local(), self.config.quiet_window)
                or not app.engine.live_review.config.enabled):
            return False
        guid, now = turn.speaker, app.clock.now()
        if turn.to_guid != app.human_guid or turn.addressee != app.human_guid:
            return False
        if now < self._human_until.get(guid, 0):
            return False
        if any(queued.speaker == guid for queued in app.engine.planned_turns()):
            return False
        if guid in app.engine.active_speakers(exclude_task=self._pending):
            return False
        owes = getattr(getattr(app, 'discord_talk', None), 'owes', None)
        if owes is not None and owes(guid):
            return False
        p, member = app.personas.get(guid), app.world.discord_members().get(guid)
        if p is None or member is None or guid in pulled_into_play(app):
            return False
        presence = presence_of(p, local=local_time(app.clock.local(), p.identity.region),
            body_online=guid in app.world.online, body_zone=member.zone, rng_seed=RNG_SEED)
        if presence.state not in {'online', 'idle'}:
            return False
        return app.store.conn.execute('SELECT 1 FROM away WHERE guid=? AND returned_at IS NULL '
            'AND (returns_at IS NULL OR returns_at>?) LIMIT 1', (guid, now)).fetchone() is None

    def intent_opener(self, intent, *, settled):
        """A dm_him intent on this DM path: one opener in her own words about his ask, with no
        outreach item (one thing to say a turn), taken when it is sent (`settled`, DB-only in
        its post's transaction). None when she cannot open now; the tick asks again."""
        guid = intent['member']
        if self._lock.locked() or self._pending is not None:
            return None
        turn = Turn(TurnKind.OPEN, guid, f'dm:{guid}', to_guid=self.app.human_guid,
                    addressee=self.app.human_guid, priority=Priority.BACKGROUND, max_lines=1,
                    instruction=("Start a private Discord message to him in your own words. You'd "
                                 "meant to message him about this; bring it up as your own idea: "
                                 + intent['args']['about']))
        if not self.intent_valid(turn):
            return None
        p = self.app.personas.get(guid)
        about = intent['args']['about']
        meta = TurnMeta(self.app.engine._new_id(), why='server:dm', initiates=True,
                        casual=whisper_kind(p) == 'casual' or forward(p) <= 0, topic=about,
                        server_anchor=about, discord=DiscordTarget(None, None), single_line=True,
                        review_required=True)
        meta.guards = self._guards
        meta.keep_channel = True
        meta.godmode_intent = intent['id']
        meta.on_discord_record = lambda line, message, now: settled(intent['id'], message, now)
        generation = self._human_generation.get(guid, 0)
        from leeroy_mind.godmode.intents import waiting
        meta.still_valid = lambda: (self._human_generation.get(guid, 0) == generation
                                    and self.intent_valid(turn)
                                    and waiting(self.app.store.conn, intent['id']))
        return self._speak_intent(guid, turn, meta)

    async def _speak_intent(self, guid, turn, meta):
        async with self._lock:
            if self._pending is not None or not meta.still_valid():
                return
            self._pending_guid = guid
            self._pending = asyncio.create_task(self.app.engine.speak_meta(turn, meta))
            try:
                await self._pending
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    raise
            finally:
                self._pending = None
                self._pending_guid = None

    async def close(self) -> None:
        self._closed = True
        if self._pending is not None:
            self._pending.cancel()
            await asyncio.gather(self._pending,return_exceptions=True)
