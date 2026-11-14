"""The running side of decisions: members' poll votes at their drawn times, polls closed when due
(a kind with a `closed(app, poll, winner)` hook acts on the result), and his console actions
through the control endpoint's `decision` op: his vote, and his overrule of any live decision."""
import asyncio
import logging

from . import apply, ledger, polls
from .positions import cast_votes

log = logging.getLogger(__name__)
TICK_S = 60.0
PRESENT = ('online', 'idle')


class DecisionsService:
    def __init__(self, app, config):
        self.app, self.config = app, config
        self.enabled = True

    def _present(self, guid: int, now: float) -> bool:
        from leeroy_mind.server.state import state_of
        try:
            return state_of(self.app, guid, now).presence.state in PRESENT
        except Exception as error:  # a member the mind cannot place is not present
            log.warning('decisions_presence_failed guid=%d error=%s', guid, type(error).__name__)
            return False

    async def tick(self) -> None:
        app = self.app
        now = app.clock.now()
        cast = cast_votes(app.store, now=now, present=lambda g: self._present(g, now))
        if cast:
            log.info('decisions_votes cast=%d', cast)
        for poll, winner in polls.close_due(app.store, now=now):
            row = polls.get_poll(app.store.conn, poll)
            rules = apply.rules_for(row['kind']) if row is not None and row['kind'] else None
            hook = getattr(rules, 'closed', None)
            if hook is None:
                continue
            try:
                await hook(app, row, winner)
            except Exception as error:
                log.warning('decisions_poll_hook_failed poll=%d kind=%s error=%s', poll, row['kind'],
                            type(error).__name__)

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
                    'message': 'Overruled before it took effect.'}
        if apply.reversible(row):
            try:
                await apply.rules_for(row['kind']).undo(self.app, row)
            except Exception as error:
                log.warning('decision_undo_failed id=%d kind=%s error=%s', row['id'], row['kind'],
                            type(error).__name__)
                return {'ok': False, 'error': 'The undo failed in game; nothing was overruled.'}
            ledger.overrule(store, row['id'], now=self.app.clock.now())
            return {'ok': True, 'outcome': 'undone', 'undone': True,
                    'message': 'Undone in game and overruled.'}
        ledger.overrule(store, row['id'], now=now)
        return {'ok': True, 'outcome': 'overruled', 'undone': False,
                'message': "It can't be undone in game; its future effect is overruled."}
