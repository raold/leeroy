"""Delayed Discord reactions, drawn from observed ties without a model call.

A message first draws how many members react to it, from a skewed (negative binomial) count
whose mean grows with how reactable it is (salience: a joke, a meme or photo, a win, his post);
then that many of the members who may react are picked, by their tie to its author. Most
messages get none or one, some two to four, and a rare popular one more. Before 2026-11-15
every member reacted on her own at 12%, so with 50-80 members online 62% of member messages
hit the cap of 3."""
import asyncio
import logging
import random
import re
import sqlite3
from collections import OrderedDict

from leeroy_mind.discord import layout, store as discord_store
from leeroy_mind.discord.channels import channel_id
from leeroy_mind.discord.presence import RNG_SEED, her_local, presence_of
from leeroy_mind.storage.db import TRANSIENT_CODES, error_code
from leeroy_mind.talk.moves import classify, main_move
from leeroy_mind.tuning.effective import dials_of

from . import runtime
from .records import BadDayCounter, increment
from .state import pulled_into_play, tie_kind

log = logging.getLogger('leeroy_mind.server')
BASE_MEAN = 0.9  # reactions an ordinary member message draws on average
SHAPE = 0.8  # the count's dispersion: lower is more skewed (more zeros, a longer tail)
MAX_PER_MESSAGE = 8  # a rare popular message's most
MAX_SALIENCE = 5.0
HIS_POST = 2.0  # his posts draw about twice the reactions (was 0.25 against 0.12 a member)
PILE_ON = 0.6  # a later reactor reuses an emoji already on the message, as people pile on
DELAY_S = (20.0, 900.0)
_LAUGH = re.compile(r'\b(?:lol|lmao|lmfao|haha\w*|rofl|kek)\b|😂|🤣|💀', re.I)
_WIN = re.compile(r'\b(?:finally|(?:guild|realm|server) first|first kill|killed|downed|one[- ]?shot\w*'
                  r'|dropped|looted|won|personal best|new record|dinged|achievement|got (?:my|the) )',
                  re.I)
TIE = {'friend':2.5, 'crush':3.0, 'fond':1.5, 'neutral':1.0, 'rival':0.3, 'feud':0.0}
PALETTE = {'joke':('😂','💀','🤣'), 'question':('👀','🤔'), 'raid':('🔥','💪'),
           'gear':('🔥','🎉','👏'), 'complaint':('😬','🫂','😢'), 'plan':('👍','🙋'),
           'compliment':('❤️','😊'), 'tease':('😂','🙄'), None:('👍','💯','😄')}
LOVE = frozenset({'❤️','😍','😊','🫂'})
# The role palette of a signup post (culture.bots, [culture] bots): each member's role from their
# real calendar signup, never a random pick. The guild's own emoji with [culture] emoji on, else
# one Unicode emoji each.
ROLE_PALETTE = (':tank:', ':healer:', ':dps:')
UNICODE_ROLES = {':tank:': '🛡️', ':healer:': '⛑️', ':dps:': '⚔️'}


def role_emoji(raid_role: str, *, custom: bool) -> str:
    from leeroy_mind.discord.custom_emoji import role_code
    code = role_code(raid_role)
    return code if custom else UNICODE_ROLES[code]


def pick_emoji(text: str, tie: str, rng: random.Random) -> str | None:
    if tie == 'feud':
        return None
    if tie == 'rival':
        return '🙄'
    return rng.choice(PALETTE.get(main_move(classify(text)), PALETTE[None]))


def salience(text: str, *, his: bool = False, attachment: str | None = None) -> float:
    """How reactable a message is, 1 for an ordinary one: a joke or a laugh, a meme or a photo,
    a win (a kill, a drop, a first) and his own post draw more; a question (it gets answers)
    and a reply of a few words draw fewer."""
    text = text or ''
    moves = classify(text)
    laugh = 'joke' in moves or 'tease' in moves or _LAUGH.search(text) is not None
    value = HIS_POST if his else 1.0
    if attachment == 'meme':
        value *= 2.5
    elif attachment:
        value *= 1.8
    if laugh:
        value *= 1.8
    win = _WIN.search(text) is not None
    if win:
        value *= 1.7
    elif moves & {'raid', 'gear'}:
        value *= 1.2
    if 'compliment' in moves:
        value *= 1.2
    if 'question' in moves:
        value *= 0.6
    if len(text.split()) <= 3 and not (laugh or win or attachment):
        value *= 0.6
    return min(MAX_SALIENCE, value)


def draw_count(u: float, mean: float, cap: int = MAX_PER_MESSAGE) -> int:
    """How many react to a message, from one uniform draw `u` in [0, 1): a negative binomial
    count of `mean` and SHAPE, at most `cap`, read from the top (u = 0 is the cap), so most
    messages get none or one and a few get many."""
    if mean <= 0 or cap <= 0:
        return 0
    p = mean / (SHAPE + mean)
    pmf = (1 - p) ** SHAPE
    cdf, n, v = pmf, 0, 1 - u
    while n < cap and v >= cdf:
        n += 1
        pmf *= (SHAPE + n - 1) / n * p
        cdf += pmf
    return n


def _weighted_pick(candidates, weights, n, rng):
    """`n` of `candidates`, without replacement, each likelier by its weight (each one's key is
    u ** (1 / weight), the largest keys win)."""
    keys = [(rng.random() ** (1 / w), i) for i, w in enumerate(weights)]
    keys.sort(key=lambda k: -k[0])
    return [candidates[i] for _, i in keys[:n]]


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
            log.warning('server_reaction_failed code=%s',error_code(task.exception()))

    def after_post(self, message_id: int, *, author: int, channel: str, text: str, his: bool) -> None:
        if not self._ready() or message_id in self._seen:
            return
        self._seen[message_id] = None
        while len(self._seen)>1024:
            self._seen.popitem(last=False)
        self._spawn(self._plan(message_id,author,channel_id(channel),text,his))

    def _attachment(self, mid):
        try:
            row = self.app.store.conn.execute('SELECT kind FROM discord_attachment WHERE message=?',
                                              (mid,)).fetchone()
        except sqlite3.OperationalError:
            return None
        return row[0] if row else None

    async def _plan(self, mid, author, channel, text, his):
        snap = await runtime.snapshot(self.app,self.app.clock.now())
        if not self._ready():
            return
        candidates, weights = [], []
        for guid in [g for g in snap.personas if _allowed(snap,g,author,channel)]:
            p = snap.personas[guid]
            tie = snap.ties.get((guid,author),'neutral')
            if tie == 'feud' or (tie == 'rival' and dials_of(p).level('drama')<7):
                continue
            weight = TIE.get(tie,1)*dials_of(p).mult('humour')
            if weight > 0:
                candidates.append(guid)
                weights.append(weight)
        order = list(range(len(candidates)))
        self.rng.shuffle(order)
        candidates, weights = [candidates[i] for i in order], [weights[i] for i in order]
        mean = BASE_MEAN*salience(text, his=his, attachment=self._attachment(mid))
        count = draw_count(self.rng.random(), mean, min(MAX_PER_MESSAGE, len(candidates)))
        piled = []
        for guid in _weighted_pick(candidates, weights, count, self.rng):
            tie = snap.ties.get((guid,author),'neutral')
            emoji = pick_emoji(text,tie,self.rng)
            if emoji is None:
                continue
            if tie != 'rival' and piled and self.rng.random() < PILE_ON:
                emoji = self.rng.choice(piled)
            if emoji != '🙄':
                piled.append(emoji)
            low = sum(DELAY_S)/2 if snap.states[guid].presence.state=='idle' else DELAY_S[0]
            self._spawn(self._write(mid,guid,author,channel,emoji,self.rng.uniform(low,DELAY_S[1])))

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
        presence = presence_of(p,local=her_local(app.clock.local(), p),
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
        for attempt in (1, 2):
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
            except sqlite3.OperationalError as error:
                # The unit reads before it writes, so another connection's commit between the
                # two refuses the write at once (busy_snapshot). Rolled back whole: ask again once.
                if attempt == 2 or error_code(error) not in TRANSIENT_CODES:
                    raise
                log.info('server_reaction_retry message=%d code=%s',mid,error_code(error))
                continue
            return

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
        presence = presence_of(p, local=her_local(self.app.clock.local(), p),
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
                p, local=her_local(self.app.clock.local(), p),
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
