"""The Guild tab's Decisions and the poll cards in Comms: read-only from mind.db, his vote and his
overrule forwarded to the mind's decision op. Fake mind and link; synthetic rows only."""
import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest

from conftest import seed_test_cast
from console.rig import http, make_app, running
from console.test_console_bank import FakeLink, served
from leeroy_mind.console import decisionsview, discordview, feed
from leeroy_mind.console.control import Controls
from leeroy_mind.decisions import apply, ledger, polls


class Reversible:
    kind = 'loot_rule'

    def reversible(self, row):
        return True

    async def undo(self, app, row):
        pass


@pytest.fixture
def data(store):
    return Path(store.conn.execute("PRAGMA database_list").fetchone()["file"]).parent


@pytest.fixture
def console(store, clock, data):
    seed_test_cast(store)
    store.set_meta('human_guid', '1')
    now = clock.now()
    loot = ledger.propose(store, kind='loot_rule', subject='loot', value={'mode': 'loot_council'},
                          positions=[{'guid': 102, 'side': 'loot_council', 'why': 'low_attendance'}], now=now - 50)
    ledger.settle(store, loot, settled_by=0, now=now - 40)
    ledger.take_effect(store, loot, effect_ref={'ledger': [12, 13], 'receipts': ['guild:loot:7:ab']}, now=now - 30)
    lineup = ledger.propose(store, kind='lineup', subject='raid:9', value={'members': [101], 'bench': [103]},
                            positions=[], now=now - 20)
    ledger.talk_only(store, lineup, reason='no_raid', now=now - 10)
    bank = ledger.propose(store, kind='bank', subject='bank:3', value={'guid': 105, 'kind': 'raid_flasks'},
                          positions=[], now=now - 5)
    with store.transaction():
        message = store.conn.execute("INSERT INTO discord_message(channel,author,origin,text,ts) "
                                     "VALUES ('officer-chat',102,'mind','Vote here',?)", (now - 9,)).lastrowid
        store.conn.execute("INSERT INTO discord_message(channel,author,origin,text,ts) "
                           "VALUES ('officer-chat',104,'mind','No poll here',?)", (now - 8,))
    poll = polls.open_poll(store, channel='officer-chat', question='Loot rules for next week?',
                           options=('dkp', 'loot_council'), voters=(102, 104), closes=now + 86400, now=now - 9,
                           kind='loot_rule', subject='loot')
    polls.attach_message(store, poll, message)
    polls.vote(store, poll, 102, 'dkp', why='high_dkp', now=now - 5)
    polls.vote(store, poll, 1, 'dkp', now=now - 4)
    store.conn.commit()
    yield dict(loot=loot, lineup=lineup, bank=bank, poll=poll, message=message, now=now)
    apply.unregister('loot_rule')


def test_decisions_view_renders_effects(console, data):
    view = feed.read(data / 'mind.db', decisionsview.decisions_view, now=console['now'], enabled=True)
    assert view['enabled'] is True and view['missing'] == [] and view['human'] == 1
    assert [d['id'] for d in view['decisions']] == [console['bank'], console['lineup'], console['loot']]
    bank, lineup, loot = view['decisions']
    assert loot['changed'] == ['DKP ledger rows: 12, 13', 'receipts: guild:loot:7:ab']
    assert (loot['settler'], loot['state'], loot['summary']) == ('a poll', 'in effect', 'mode loot_council')
    assert loot['positions'] == [{'guid': 102, 'name': 'Belra', 'side': 'loot_council', 'why': 'low_attendance'}]
    assert loot['action']['kind'] == 'overrule' and "can't be undone in game" in loot['action']['words'].lower()
    assert loot['action']['permanent'] is True and lineup['action'] is None
    assert lineup['changed'] == ['talk only: no_raid'] and lineup['action'] is None
    assert bank['officers_only'] is True and bank['action']['kind'] == 'overrule'
    assert bank['action']['words'].startswith('Not in effect yet')
    assert bank['action']['permanent'] is False
    [poll] = view['polls']
    assert poll['options'] == [{'code': 'dkp', 'count': 2}, {'code': 'loot_council', 'count': 0}]
    assert (poll['his_vote'], poll['votes'], poll['voters'], poll['open']) == ('dkp', 2, 3, True)
    # Undo appears only where the kind says the game can undo it
    apply.register(Reversible())
    view = feed.read(data / 'mind.db', decisionsview.decisions_view, now=console['now'])
    assert view['decisions'][-1]['action']['kind'] == 'undo' and view['enabled'] is False


def test_a_message_carries_its_poll_card_and_no_other_changes(console, store, data):
    def window(conn):
        return discordview.discord_view(conn, owner_now=datetime(2026, 10, 29, 20, tzinfo=timezone.utc),
                                        trial_rank=4, channel='officer-chat')['messages']
    messages = feed.read(data / 'mind.db', window)
    carrying, plain = messages
    assert carrying['poll']['id'] == console['poll'] and carrying['poll']['his_vote'] == 'dkp'
    assert 'poll' not in plain


def test_his_vote_and_overrule_go_to_the_mind(console, data):
    link = FakeLink({'ok': True, 'message': 'Your vote is in.'})

    async def go():
        async with served(make_app(data, mind=running(data), link=link)) as app:
            got = await http(app, 'GET', '/api/decisions')
            assert got.status == 200 and got.json()['polls']
            vote = await http(app, 'POST', '/api/decision', origin=True,
                              body={'action': 'vote', 'poll': console['poll'], 'option': 'dkp'})
            bad = await http(app, 'POST', '/api/decision', origin=True, body={'action': 'vote', 'poll': 1})
            return vote, bad
    vote, bad = asyncio.run(go())
    assert vote.status == 200 and vote.json()['message'] == 'Your vote is in.'
    assert link.calls == [((101, 100), 'decision', {'action': 'vote', 'poll': console['poll'], 'option': 'dkp'})]
    assert bad.status == 400

    async def readonly():
        controls = Controls(enabled=False, mind=running(data), voice=None)
        async with served(make_app(data, mind=running(data), link=link, controls=controls)) as app:
            return await http(app, 'POST', '/api/decision', origin=True, body={'action': 'overrule', 'id': 1})
    refused = asyncio.run(readonly())
    assert refused.status == 403 and 'read-only' in refused.json()['error']
    assert len(link.calls) == 1
