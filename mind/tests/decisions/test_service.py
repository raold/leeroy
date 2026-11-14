import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from conftest import seed_test_cast
from leeroy_mind.decisions import apply, ledger, polls
from leeroy_mind.decisions.config import DecisionsConfig
from leeroy_mind.decisions.positions import Position, VOTE_AFTER_S
from leeroy_mind.decisions.service import MAX_HOOK_TRIES, DecisionsService, presences
from leeroy_mind.protocol import Member
from leeroy_mind.server.state import StateInputs, state_of


class Kind:
    """A loot kind whose hook records its decision and the receipt in one transaction."""
    kind = 'loot_rule'

    def __init__(self, store, failures=0):
        self.store, self.failures, self.calls = store, failures, []

    def reversible(self, row):
        return False

    async def undo(self, app, row):
        raise AssertionError('never reversible')

    async def closed(self, app, poll, winner):
        self.calls.append((poll['id'], winner))
        if self.failures:
            self.failures -= 1
            raise RuntimeError('busy')
        with self.store.transaction():
            ident = ledger.propose(self.store, kind='loot_rule', subject='loot', value={'mode': winner},
                                   positions=[], poll=poll['id'], now=app.clock.now())
            ledger.settle(self.store, ident, settled_by=0, now=app.clock.now())
            polls.mark_handled(self.store, poll['id'], now=app.clock.now())


def service(store, at, **app):
    return DecisionsService(SimpleNamespace(store=store, clock=SimpleNamespace(now=lambda: at[0]), human_guid=1, **app),
                            DecisionsConfig(enabled=True))


@pytest.fixture
def loot_poll(store, clock):
    now = clock.now()
    store.set_meta('human_guid', '1')
    return polls.open_poll(store, channel='officer-chat', question='Loot rules?', options=('dkp', 'loot_council'),
                           voters=(102, 104), closes=now + 86400, now=now, kind='loot_rule', subject='loot',
                           positions=[Position(102, 'loot_council', 'low_attendance'),
                                      Position(104, 'loot_council', 'wants_change')])


@pytest.fixture
def kind(store):
    rules = Kind(store)
    apply.register(rules)
    yield rules
    apply.unregister('loot_rule')


def test_the_tick_casts_votes_closes_due_polls_and_hands_the_result_to_its_kind(store, clock, loot_poll, kind):
    at = [clock.now()]
    svc = service(store, at)
    away = {104}
    svc._present = lambda guids, now: {g: 'offline' if g in away else 'online' for g in guids}
    at[0] += VOTE_AFTER_S[1]
    asyncio.run(svc.tick())
    assert polls.tally(store.conn, loot_poll) == {'dkp': 0, 'loot_council': 1}
    away.clear()
    at[0] += 86400
    asyncio.run(svc.tick())
    asyncio.run(svc.tick())
    assert polls.tally(store.conn, loot_poll) == {'dkp': 0, 'loot_council': 1}  # 104 came back after it closed
    assert kind.calls == [(loot_poll, 'loot_council')]
    assert polls.get_poll(store.conn, loot_poll)['handled'] is not None


def test_a_failed_hook_is_retried_and_a_crash_after_closing_loses_nothing(store, clock, loot_poll, kind):
    at = [clock.now() + 86400]
    polls.close_due(store, now=at[0])  # closed, then the mind stopped before its kind heard of it
    kind.failures = 1
    first = service(store, at)          # after the restart
    asyncio.run(first.tick())
    assert kind.calls == [(loot_poll, None)] and polls.get_poll(store.conn, loot_poll)['hook_tries'] == 1
    second = service(store, at)          # and another restart
    asyncio.run(second.tick())
    asyncio.run(second.tick())
    assert kind.calls == [(loot_poll, None)] * 2  # retried once, then acted exactly once
    assert [r['poll'] for r in store.conn.execute('SELECT poll FROM guild_decision')] == [loot_poll]


def test_a_hook_that_keeps_failing_is_given_up_after_its_tries(store, clock, loot_poll, kind, caplog):
    at = [clock.now() + 86400]
    kind.failures = 99
    svc = service(store, at)
    for _ in range(MAX_HOOK_TRIES + 2):
        asyncio.run(svc.tick())
    assert len(kind.calls) == MAX_HOOK_TRIES
    assert 'decisions_poll_hook_gave_up' in caplog.text


def test_a_poll_whose_kind_has_no_hook_waits_for_one(store, clock, loot_poll):
    at = [clock.now() + 86400]
    asyncio.run(service(store, at).tick())
    assert [r['id'] for r in polls.unhandled(store.conn)] == [loot_poll]


def test_presence_is_the_servers_without_its_database_reads(store, clock):
    personas = seed_test_cast(store)
    members = {g: Member(guid=g, name=p.character_name, race=1, class_=1, gender=0, level=80, rank=3,
                         online=g != 103, map=0, zone=12, spec_tab=-1) for g, p in personas.items()}
    world = SimpleNamespace(discord_members=lambda: members, online=frozenset(g for g in members if g != 103),
                            human_online=False, group=None)
    for hour in (3, 9, 14, 20, 23):
        local = datetime(2026, 10, 29, hour, 0, tzinfo=timezone.utc)
        app = SimpleNamespace(world=world, personas=personas, guild=None, human_guid=1,
                              clock=SimpleNamespace(local=lambda: local))
        got = presences(app, set(personas), local.timestamp())
        for g, p in personas.items():
            observed = StateInputs(p, members, frozenset(), world.online, frozenset(), 1, timezone.utc, None)
            assert got[g] == state_of(None, g, local.timestamp(), conn=store.conn, observed=observed).presence.state


def test_an_undo_the_ledger_cannot_record_yet_is_reported_and_retried(store, clock):
    class Rank:
        kind = 'rank'
        undone = []

        def reversible(self, row):
            return True

        async def undo(self, app, row):
            self.undone.append(row['id'])

    now = clock.now()
    standing = ledger.propose(store, kind='rank', subject='rank:105', value={'rank': 4}, positions=[], now=now)
    ledger.settle(store, standing, settled_by=102, now=now)
    ledger.take_effect(store, standing, effect_ref={}, now=now)
    promote = ledger.propose(store, kind='rank', subject='rank:105', value={'rank': 3}, positions=[], now=now)
    ledger.settle(store, promote, settled_by=102, now=now)
    ledger.take_effect(store, promote, effect_ref={}, now=now + 1)
    apply.register(Rank())
    with store.transaction():
        store.conn.execute("CREATE TEMP TRIGGER busy BEFORE UPDATE OF status ON guild_decision "
                           "WHEN NEW.status='overruled' BEGIN SELECT RAISE(ABORT, 'busy'); END")
    svc = service(store, [now + 2])
    try:
        reply = asyncio.run(svc.handle({'action': 'overrule', 'id': promote}))
        assert reply['ok'] is False and reply['undone'] is True and reply['outcome'] == 'undone_unrecorded'
        assert 'Undone in game' in reply['error'] and Rank.undone == [promote]
        assert ledger.current(store.conn, 'rank', 'rank:105')['id'] == promote
        with store.transaction():
            store.conn.execute('DROP TRIGGER temp.busy')
        asyncio.run(svc.tick())  # the tick records it; the game is not undone twice
    finally:
        apply.unregister('rank')
    assert ledger.get(store.conn, promote)['status'] == 'overruled' and Rank.undone == [promote]
    assert ledger.current(store.conn, 'rank', 'rank:105')['id'] == standing  # the rank it replaced is back
