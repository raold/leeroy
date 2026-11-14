"""Delayed Discord reactions, drawn from observed ties without a model call."""
import asyncio
import logging
import random
from collections import OrderedDict

from leeroy_mind.discord import layout, store as discord_store
from leeroy_mind.discord.channels import channel_id
from leeroy_mind.discord.presence import RNG_SEED, local_time, presence_of
from leeroy_mind.talk.moves import classify, main_move
from leeroy_mind.tuning.effective import dials_of

from . import runtime
from .records import BadDayCounter, increment
from .state import pulled_into_play, tie_kind

log = logging.getLogger('leeroy_mind.server')
BASE_P = 0.12
HIS_POST_P = 0.25
MAX_PER_MESSAGE = 3
DELAY_S = (20.0, 900.0)
TIE = {'friend':2.5, 'crush':3.0, 'fond':1.5, 'neutral':1.0, 'rival':0.3, 'feud':0.0}
PALETTE = {'joke':('😂','💀','🤣'), 'question':('👀','🤔'), 'raid':('🔥','💪'),
           'gear':('🔥','🎉','👏'), 'complaint':('😬','🫂','😢'), 'plan':('👍','🙋'),
           'compliment':('❤️','😊'), 'tease':('😂','🙄'), None:('👍','💯','😄')}
LOVE = frozenset({'❤️','😍','😊','🫂'})


def pick_emoji(text: str, tie: str, rng: random.Random) -> str | None:
    if tie == 'feud':
        return None
    if tie == 'rival':
        return '🙄'
    return rng.choice(PALETTE.get(main_move(classify(text)), PALETTE[None]))


def _audience(snap, channel):
    return layout.audience(channel, cast=snap.cast, officers=snap.officers, human=snap.human)


def _allowed(snap, guid, author, channel):
    return (guid != author and guid != snap.human and guid in snap.personas
            and guid in _audience(snap,channel) and guid not in snap.in_play
            and guid not in snap.away and guid in snap.states
            and snap.states[guid].presence.state in {'online','idle'})


class Reactor:
    def __init__(self, app, config, rng: random.Random):
        self.app, self.config, self.rng = app, config, rng
        self._tasks = set()
        self._seen = OrderedDict()
        self._closed = False

    def _spawn(self, coro):
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._done)

    def _done(self, task):
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.warning('server_reaction_failed code=%s',type(task.exception()).__name__)

    def after_post(self, message_id: int, *, author: int, channel: str, text: str, his: bool) -> None:
        if not self._ready() or message_id in self._seen:
            return
        self._seen[message_id] = None
        while len(self._seen)>1024:
            self._seen.popitem(last=False)
        self._spawn(self._plan(message_id,author,channel_id(channel),text,his))

    async def _plan(self, mid, author, channel, text, his):
        snap = await runtime.snapshot(self.app,self.app.clock.now())
        if not self._ready():
            return
        candidates = [g for g in snap.personas if _allowed(snap,g,author,channel)]
        self.rng.shuffle(candidates)
        chosen = 0
        for guid in candidates:
            p = snap.personas[guid]
            tie = snap.ties.get((guid,author),'neutral')
            if tie == 'feud' or (tie == 'rival' and dials_of(p).level('drama')<7):
                continue
            chance = (HIS_POST_P if his else BASE_P)*TIE.get(tie,1)*dials_of(p).mult('humour')
            if self.rng.random()>=min(1,chance):
                continue
            emoji = pick_emoji(text,tie,self.rng)
            if emoji is None:
                continue
            low = sum(DELAY_S)/2 if snap.states[guid].presence.state=='idle' else DELAY_S[0]
            self._spawn(self._write(mid,guid,author,channel,emoji,self.rng.uniform(low,DELAY_S[1])))
            chosen += 1
            if chosen>=MAX_PER_MESSAGE:
                break

    def _ready(self):
        return (not self._closed and not getattr(self.app,'stopping',False)
                and self.config.enabled and self.config.reactions)

    def _live_allowed(self, guid, author, channel, emoji, now):
        app, world = self.app, self.app.world
        cast, p = world.discord_members(), app.personas.get(guid)
        officers = {g for g in cast if world.is_officer(g)}
        if (p is None or guid not in cast or guid in {author,app.human_guid}
                or guid in pulled_into_play(app)
                or guid not in layout.audience(channel,cast=cast,officers=officers,human=app.human_guid)):
            return False
        presence = presence_of(p,local=local_time(app.clock.local(),p.identity.region),
            body_online=guid in world.online,body_zone=cast[guid].zone,rng_seed=RNG_SEED)
        if presence.state not in {'online','idle'}:
            return False
        if app.store.conn.execute('SELECT 1 FROM away WHERE guid=? AND returned_at IS NULL '
            'AND (returns_at IS NULL OR returns_at>?) LIMIT 1',(guid,now)).fetchone():
            return False
        tie = tie_kind(app.store.conn,guid,author)
        return tie!='feud' and (tie!='rival' or (emoji=='🙄' and dials_of(p).level('drama')>=7))

    async def _write(self, mid, guid, author, channel, emoji, delay):
        await self.app.clock.sleep(delay)
        if not self._ready():
            return
        now = self.app.clock.now()
        snap = await runtime.snapshot(self.app,now)
        if not self._ready() or not self._live_allowed(guid,author,channel,emoji,self.app.clock.now()):
            return
        if not _allowed(snap,guid,author,channel):
            return
        tie = snap.ties.get((guid,author),'neutral')
        if tie=='feud' or (tie=='rival' and (emoji!='🙄' or dials_of(snap.personas[guid]).level('drama')<7)):
            return
        try:
            with self.app.store.transaction():
                row = self.app.store.conn.execute('SELECT author,channel,deleted FROM discord_message WHERE id=?',(mid,)).fetchone()
                if row is None or row[0]!=author or row[1]!=channel or row[2] is not None:
                    log.info('server_reaction_skipped message=%d code=deleted',mid)
                    return
                if discord_store.react(self.app.store,mid,member=guid,emoji=emoji,now=now):
                    try:
                        increment(self.app.store,snap.local.date().isoformat(),reactions=1)
                    except BadDayCounter:
                        pass  # the malformed server budget must not remove a reaction
        except discord_store.DiscordRefused as error:
            log.info('server_reaction_skipped message=%d code=%s',mid,error.code)

    def intent_react(self, intent, *, settle):
        """A react intent (godmode): her reaction with his emoji, after her presence's own delay,
        under the live checks a reaction has (audience, presence, play, away), and taken in the
        reaction's transaction (`settle`, DB-only). None when reactions are off; the intent then
        waits for its tick."""
        if not self._ready():
            return None
        return self._intent_write(intent, settle)

    async def _intent_write(self, intent, settle):
        guid, mid, emoji = intent['member'], intent['args']['message'], intent['args']['emoji']
        p = self.app.personas.get(guid)
        if p is None:
            return
        presence = presence_of(p, local=local_time(self.app.clock.local(), p.identity.region),
            body_online=guid in self.app.world.online,
            body_zone=getattr(self.app.world.discord_members().get(guid), 'zone', 0),
            rng_seed=RNG_SEED).state
        if presence not in {'online', 'idle'}:
            return
        low = sum(DELAY_S) / 2 if presence == 'idle' else DELAY_S[0]
        await self.app.clock.sleep(self.rng.uniform(low, DELAY_S[1]))
        if not self._ready():
            return
        now = self.app.clock.now()
        p = self.app.personas.get(guid)
        member = self.app.world.discord_members().get(guid)
        if p is None or member is None or presence_of(
                p, local=local_time(self.app.clock.local(), p.identity.region),
                body_online=guid in self.app.world.online, body_zone=member.zone,
                rng_seed=RNG_SEED).state not in {'online', 'idle'}:
            return  # gone offline meanwhile: the intent waits for a later tick
        from leeroy_mind.godmode.intents import fit, get as get_intent, settle as settle_intent
        current = get_intent(self.app.store.conn, intent['id'])
        if current is not None and current['status'] == 'pending':
            ok, why = fit(self.app.store.conn, current, self.app.personas, now=now)
            if not ok:
                settle_intent(self.app.store, intent['id'], status='not_taken', reason=why, now=now)
                return
        row = self.app.store.conn.execute('SELECT author,channel,deleted FROM discord_message '
                                          'WHERE id=?', (mid,)).fetchone()
        from leeroy_mind.godmode.intents import waiting
        if row is None or row[2] is not None or not waiting(self.app.store.conn, intent['id']):
            return
        app, world = self.app, self.app.world
        cast = world.discord_members()
        officers = {g for g in cast if world.is_officer(g)}
        if (guid in pulled_into_play(app) or guid not in cast
                or guid not in layout.audience(row[1], cast=cast, officers=officers,
                                               human=app.human_guid)
                or app.store.conn.execute('SELECT 1 FROM away WHERE guid=? AND returned_at IS NULL '
                    'AND (returns_at IS NULL OR returns_at>?) LIMIT 1', (guid, now)).fetchone()):
            return
        try:
            with app.store.transaction():
                discord_store.react(app.store, mid, member=guid, emoji=emoji, now=now)
                settle(intent['id'], now)
        except discord_store.DiscordRefused as error:
            log.info('server_reaction_skipped message=%d code=%s', mid, error.code)
            from leeroy_mind.godmode.intents import settle as settle_intent
            settle_intent(app.store, intent['id'], status='not_taken', reason=error.code, now=now)

    async def close(self) -> None:
        self._closed = True
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks,return_exceptions=True)
        self._tasks.clear()
