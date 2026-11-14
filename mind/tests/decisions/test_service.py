import asyncio
from types import SimpleNamespace

from leeroy_mind.decisions import apply, polls
from leeroy_mind.decisions.config import DecisionsConfig
from leeroy_mind.decisions.positions import Position, VOTE_AFTER_S
from leeroy_mind.decisions.service import DecisionsService


class Closing:
    kind = 'loot_rule'

    def __init__(self):
        self.results = []

    def reversible(self, row):
        return False

    async def undo(self, app, row):
        raise AssertionError('never reversible')

    async def closed(self, app, poll, winner):
        self.results.append((poll['id'], winner))


def test_the_tick_casts_votes_closes_due_polls_and_hands_the_result_to_its_kind(store, clock):
    now = clock.now()
    store.set_meta('human_guid', '1')
    poll = polls.open_poll(store, channel='officer-chat', question='Loot rules?', options=('dkp', 'loot_council'),
                           voters=(102, 104), closes=now + 86400, now=now, kind='loot_rule', subject='loot',
                           positions=[Position(102, 'loot_council', 'low_attendance'),
                                      Position(104, 'loot_council', 'wants_change')])
    at = [now]
    service = DecisionsService(SimpleNamespace(store=store, clock=SimpleNamespace(now=lambda: at[0]), human_guid=1),
                               DecisionsConfig(enabled=True))
    away = {104}
    service._present = lambda guid, at: guid not in away
    rules = Closing()
    apply.register(rules)
    try:
        at[0] += VOTE_AFTER_S[1]
        asyncio.run(service.tick())
        assert polls.tally(store.conn, poll) == {'dkp': 0, 'loot_council': 1}
        away.clear()
        at[0] += 86400
        asyncio.run(service.tick())
    finally:
        apply.unregister('loot_rule')
    assert polls.tally(store.conn, poll) == {'dkp': 0, 'loot_council': 1}  # 104 came back after it closed
    assert rules.results == [(poll, 'loot_council')]
    assert polls.get_poll(store.conn, poll)['status'] == 'closed'
