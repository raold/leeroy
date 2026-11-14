"""The running side of decisions: members' poll votes at their drawn times, polls closed when due,
each closed poll's result handed to its kind's `closed(app, poll, winner)` hook exactly once (a
durable receipt, retried after a failure or a restart), and his console actions through the
control endpoint's `decision` op: his vote, and his overrule of any live decision."""
import asyncio
from datetime import datetime
import logging
import sqlite3

from . import apply, ledger, polls
from .ledger import DecisionError
from .positions import cast_votes, due_voters

log = logging.getLogger(__name__)
TICK_S = 60.0
PRESENT = ('online', 'idle')
MAX_HOOK_TRIES = 5      # a kind's hook failing this often gives the result up (logged)
RECORD_TRIES = 3        # writes of his overrule after the game was undone, before the tick retries


def presences(app, guids, now: float) -> dict[int, str]:
    """Discord presence (online, idle, dnd, offline) of these members, as server.state computes it
    but from the presence inputs only: no message scan and no database read on the loop."""
    from leeroy_mind.discord.presence import RNG_SEED, local_time, presence_of
    from leeroy_mind.server.state import pulled_into_play
    cast = dict(app.world.discord_members())
    online, playing = frozenset(app.world.online), pulled_into_play(app)
    owner_now = datetime.fromtimestamp(now, app.clock.local().tzinfo)
    out = {}
    for guid in guids:
        p = app.personas.get(guid)
        out[guid] = 'offline' if p is None else presence_of(
            p, local=local_time(owner_now, p.identity.region), body_online=guid in online,
            body_zone=getattr(cast.get(guid), 'zone', 0), rng_seed=RNG_SEED, in_his_group=guid in playing).state
    return out


class DecisionsService:
    def __init__(self, app, config):
        self.app, self.config = app, config
        self.enabled = True
        self._unrecorded: set[int] = set()  # undone in game, his overrule not yet in the ledger

    def _present(self, guids, now: float) -> dict[int, str]:
        try:
            return presences(self.app, guids, now)
        except Exception as error:  # members the mind cannot place are not present
            log.warning('decisions_presence_failed error=%s', type(error).__name__)
            return {}

    async def tick(self) -> None:
        app = self.app
        now = app.clock.now()
        due = due_voters(app.store.conn, now=now)
        if due:
            states = self._present(due, now)
            cast = cast_votes(app.store, now=now, present=lambda g: states.get(g) in PRESENT)
            if cast:
                log.info('decisions_votes cast=%d', cast)
        polls.close_due(app.store, now=now)
        await self.deliver()
        for ident in sorted(self._unrecorded):
            await self._record_undo(ident)
        await self.kinds(now)

    async def kinds(self, now: float) -> None:
        """Each registered kind's own minute work (proposing, posting, taking effect, ending): a
        kind that fails is logged and the others still run."""
        for kind in ledger.KINDS:
            hook = getattr(apply.rules_for(kind), 'tick', None)
            if hook is None:
                continue
            try:
                await hook(self.app, now)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                log.warning('decisions_kind_tick_failed kind=%s error=%s', kind, type(error).__name__)

    async def on_hello(self, ev) -> None:
        """A worldserver (re)connected: kinds holding state in game send it again."""
        for kind in ledger.KINDS:
            hook = getattr(apply.rules_for(kind), 'on_hello', None)
            if hook is not None:
                hook(ev)

    async def on_connection(self, connected: bool) -> None:
        for kind in ledger.KINDS:
            hook = getattr(apply.rules_for(kind), 'on_connection', None)
            if hook is not None:
                hook(connected)

    async def deliver(self) -> None:
        """Hand each closed poll's result to its kind, once. A kind should write
        polls.mark_handled in the transaction that records its decision; otherwise the receipt is
        written when the hook returns. A failed hook is tried again on later ticks, at most
        MAX_HOOK_TRIES times; a poll whose kind has no hook (yet) waits for one."""
        store = self.app.store
        for row in polls.unhandled(store.conn):
            hook = getattr(apply.rules_for(row['kind']), 'closed', None)
            if hook is None:
                continue
            try:
                await hook(self.app, row, row['result'])
            except Exception as error:
                tries = polls.hook_failed(store, row['id'])
                if tries >= MAX_HOOK_TRIES:
                    polls.mark_handled(store, row['id'], now=self.app.clock.now())
                    log.warning('decisions_poll_hook_gave_up poll=%d kind=%s tries=%d error=%s', row['id'],
                                row['kind'], tries, type(error).__name__)
                else:
                    log.warning('decisions_poll_hook_failed poll=%d kind=%s tries=%d error=%s', row['id'],
                                row['kind'], tries, type(error).__name__)
                continue
            polls.mark_handled(store, row['id'], now=self.app.clock.now())

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                log.warning('decisions_tick_failed error=%s', type(error).__name__)
            await self.app.clock.sleep(TICK_S)

    async def handle(self, args: dict) -> dict:
        """His console action, checked by endpoint.decision_args: {"action": "vote", "poll",
        "option"} or {"action": "overrule", "id"}."""
        reply = await (self._vote(args) if args['action'] == 'vote' else self._overrule(args))
        log.info('decision_op action=%s id=%d outcome=%s', args['action'],
                 args['poll'] if args['action'] == 'vote' else args['id'], reply.get('outcome', 'refused'))
        return reply

    async def _vote(self, args) -> dict:
        store, human, now = self.app.store, self.app.human_guid, self.app.clock.now()
        if polls.vote(store, args['poll'], human, args['option'], now=now):
            return {'ok': True, 'outcome': 'voted', 'message': 'Your vote is in.'}
        row = polls.get_poll(store.conn, args['poll'])
        if row is None:
            error = 'There is no such poll.'
        elif row['status'] != 'open' or now >= row['closes']:
            error = 'That poll has closed.'
        elif store.conn.execute('SELECT 1 FROM guild_poll_vote WHERE poll=? AND member=?',
                                (args['poll'], human)).fetchone():
            error = 'You already voted in this poll; a vote never changes.'
        elif args['option'] not in polls.options_of(row):
            error = 'That is not one of the poll\'s options.'
        else:
            error = 'You are not a voter in this poll.'
        return {'ok': False, 'error': error}

    async def _overrule(self, args) -> dict:
        store, now = self.app.store, self.app.clock.now()
        row = ledger.get(store.conn, args['id'])
        if row is None:
            return {'ok': False, 'error': 'There is no such decision.'}
        if row['status'] not in ledger.LIVE:
            return {'ok': False, 'error': 'That decision is no longer live.'}
        if row['status'] != 'in_effect':
            ledger.overrule(store, row['id'], now=now)
            return {'ok': True, 'outcome': 'overruled', 'undone': False,
                    'message': 'Overruled before it took effect; the rule in force stays.'}
        if apply.reversible(row):
            try:
                await apply.rules_for(row['kind']).undo(self.app, row)
            except Exception as error:
                log.warning('decision_undo_failed id=%d kind=%s error=%s', row['id'], row['kind'],
                            type(error).__name__)
                return {'ok': False, 'error': 'The undo failed in game; nothing was overruled.'}
            return await self._record_undo(row['id'])
        ledger.overrule(store, row['id'], now=now)
        return {'ok': True, 'outcome': 'overruled', 'undone': False,
                'message': "It can't be undone in game; its future effect is overruled."}

    async def _record_undo(self, ident: int) -> dict:
        """The game was undone: record his overrule, putting back the rule it replaced. The row is
        read again after the undo's await; a write that keeps failing is retried by the tick."""
        store = self.app.store
        for attempt in range(RECORD_TRIES):
            try:
                row = ledger.get(store.conn, ident)
                if row['status'] != 'in_effect':
                    self._unrecorded.discard(ident)
                    return {'ok': True, 'outcome': 'undone', 'undone': True,
                            'message': f"Undone in game; the ledger had already ended it ({row['status']})."}
                back = ledger.overrule(store, ident, now=self.app.clock.now(), restore_previous=True)
                self._unrecorded.discard(ident)
                return {'ok': True, 'outcome': 'undone', 'undone': True,
                        'message': 'Undone in game and overruled.'
                                   + (' The rule it replaced is back in effect.' if back else '')}
            except (DecisionError, sqlite3.Error) as error:
                failure = error
                await asyncio.sleep(0.05 * (attempt + 1))
        self._unrecorded.add(ident)
        log.warning('decision_overrule_unrecorded id=%d error=%s', ident, type(failure).__name__)
        return {'ok': False, 'outcome': 'undone_unrecorded', 'undone': True,
                'error': 'Undone in game, but the ledger could not record the overrule yet; '
                         'the mind keeps trying.'}
