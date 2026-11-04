"""The realm's ledger reads, former members and guard alerts over actual Store rows."""
from dataclasses import replace
import json
import logging
from types import SimpleNamespace

import pytest

from conftest import make_persona, run, seed_test_cast
from leeroy_mind.personas.io import save_persona
from leeroy_mind.realm import protocol
from leeroy_mind.realm.calendar_time import local_write_day
from leeroy_mind.realm.fact_store import FactStore
from leeroy_mind.realm.ledger import Ledger, former_members_in_unit
from leeroy_mind.realm.provenance import (audit_realm_reads_in_unit, public_fact_in_unit,
                                          stored_forum_candidate_in_unit)
from leeroy_mind.realm.records import BackIn, FactRow, ForumCandidate, Rejected
from leeroy_mind.realm.service import attach_realm
from leeroy_mind.wow import CLASSES, RACES
from realm.fakes import RealmApp, enabled
from test_realm_live_proof import HUMAN, insert, observe, roster
from test_realm_provenance import event as boss_event, memory as source_memory, rumor
from test_realm_source_admission import TABLES, application, counts, run as run_native

QUILLON = replace(HUMAN, real_name='Quillon')


def ports(store, human=HUMAN):
    return SimpleNamespace(human=lambda: human, now=store.clock.now)


def ledger(store, human=HUMAN):
    return Ledger(store, ports(store, human), FactStore(store))


def record(store, policy='bg_result_v1', *, human=HUMAN, **kw):
    with store.transaction():
        observation = observe(store, policy, human=human, **kw)
    return FactStore(store).record(observation, human=human)


def persona(store, guid, name='Fenwick', **game):
    save_persona(store, make_persona(guid, name, **game), source='seed', ts=1000.0,
                 race=RACES[2], class_name=CLASSES[1])


def realm_rows(store):
    with store.transaction():
        return tuple(store.conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0] for table in TABLES)


def forum_rumor(store, identifier, *, scope='open', last_told=1000.0):
    """One stored game-topic rumor: boss event, open source memory and live rumor."""
    with store.transaction():
        boss_event(store, identifier)
        source_memory(store, identifier, evidence=json.dumps([f'event:{identifier}']), scope=scope)
        rumor(store, identifier, root=f'event:{identifier}', source=identifier, last_told=last_told)


# -- known(): the prompt's facts ---------------------------------------------------------

def test_prompt_facts_are_public_only(roster):
    public = record(roster)
    revoked = record(roster, payload={'ts': 1002000}, captured=1011.0)
    assert type(public) is FactRow and type(revoked) is FactRow
    with roster.transaction():
        roster.conn.execute("UPDATE realm_fact_origin SET state='revoked' WHERE fact=?", (revoked.id,))
        poach = insert(roster, 'realm_fact', kind='poach', text='Synthetic Guild took a raider.',
                       source='rule', ref='', public=0, ts=1003.0)
        insert(roster, 'realm_knows', fact=poach, guild=50, since=1003.0, how='own')
    facts = ledger(roster)
    assert facts.known(50) == (public,)
    assert facts.known(60) == (public,)
    assert {row['id'] for row in facts.known_all(50)} == {public.id, revoked.id, poach}
    assert {row['public'] for row in facts.known_all(50)} == {0, 1}


def test_guild_kill_is_server_wide_fact(roster):
    with roster.transaction():
        for guild in (80, 90):
            insert(roster, 'realm_guild', id=guild, role='gankers', faction='alliance',
                   name='Synthetic Rival', added=1000.0, status='active')
    kill = record(roster, 'verified_raid_kill_v1')
    assert type(kill) is FactRow
    assert (kill.kind, kill.text, kill.public) == ('kill', 'The guild killed Lord Marrowgar.', True)
    with roster.transaction():
        assert [tuple(row) for row in roster.conn.execute('SELECT kind,public FROM realm_fact')] == [('kill', 1)]
        assert [tuple(row) for row in roster.conn.execute(
            'SELECT guild,how FROM realm_knows ORDER BY guild')] == [
            (50, 'own'), (60, 'server'), (80, 'server'), (90, 'server')]
    facts = ledger(roster)
    for guild in (50, 60, 80, 90):
        assert facts.known(guild) == (kill,)


@pytest.mark.parametrize('channel', ['whisper', 'guild', 'officer'])
def test_whispers_and_guild_chat_never_become_facts(tmp_path, clock, monkeypatch, channel):
    async def scenario():
        async with application(tmp_path, clock, monkeypatch) as native:
            app = native.app
            line = dict(v=1, ts=1000000, channel=channel, from_guid=10, from_name='Testmaster',
                        text='Ready for Icecrown Citadel.', zone=0)
            await native.deliver(dict(line, type='chat', to_guid=101 if channel == 'whisper' else 0, map=0))
            with app.store.transaction():
                assert app.store.conn.execute('SELECT channel FROM chat_line').fetchall()[-1][0] == channel
            assert native.captures == []  # Ordinary chat is never a Realm source.
            await native.deliver(dict(line, type='channel_chat', via_bridge=False))
            assert len(native.results) == 1 and type(native.results[0]) is Rejected
            assert counts(app.store) == (0, 0, 0, 0, 0)
            assert app.realm.ledger.known(50) == ()
            # The same text on a public channel is a fact, so only the channel kept it out.
            await native.deliver(dict(line, type='channel_chat', channel='trade', via_bridge=False))
            assert [row.text for row in app.realm.ledger.known(50)] == [
                'A public trade message discussed Icecrown Citadel.']
    run_native(scenario())


def test_real_name_never_reaches_a_fact(roster):
    for text in ('Quillon: Ready for Icecrown Citadel.', 'Ready for Icecrown Citadel, QUILLON.'):
        dropped = record(roster, 'channel_raid_topic_v1', human=QUILLON, payload={'text': text})
        assert dropped == Rejected('real_name')
    assert realm_rows(roster) == (0, 0, 0, 0, 0)
    assert ledger(roster, QUILLON).known(50) == ()
    kept = record(roster, 'channel_raid_topic_v1', human=QUILLON)
    assert type(kept) is FactRow and 'quillon' not in kept.text.casefold()


# -- forum_candidates(): what the forum may talk about -------------------------------------

def test_game_rumor_is_forum_material(store):
    forum_rumor(store, 44)
    with store.transaction():
        expected = stored_forum_candidate_in_unit(store, 44, human=HUMAN)
    assert type(expected) is ForumCandidate and expected.root == 'event:44'
    assert ledger(store).forum_candidates() == (expected,)
    assert expected.safe_text == 'A rumor concerns the raid encounter with Lord Marrowgar.'


@pytest.mark.parametrize('scope', ['private', 'secret'])
def test_private_rumor_is_not_forum_material(store, scope):
    forum_rumor(store, 44, scope=scope)
    forum_rumor(store, 45)
    candidates = ledger(store).forum_candidates()
    assert [c.rumor_id for c in candidates] == [45]
    with store.transaction():
        store.conn.execute("UPDATE rumor SET status='faded' WHERE id=45")
    assert ledger(store).forum_candidates() == ()


def test_forum_candidates_skip_invalid_without_prefix_cap(store):
    forum_rumor(store, 1, last_told=1000.0)
    for identifier in (2, 3, 4):  # Told more recently, so they come first, but all invalid.
        forum_rumor(store, identifier, scope='private', last_told=2000.0 + identifier)
    forum_rumor(store, 5, last_told=1500.0)
    facts = ledger(store)
    assert [c.rumor_id for c in facts.forum_candidates(limit=1)] == [5]
    assert [c.rumor_id for c in facts.forum_candidates(limit=2)] == [5, 1]
    assert [c.rumor_id for c in facts.forum_candidates()] == [5, 1]
    for limit in (0, 21, True, 1.0):
        with pytest.raises(ValueError):
            facts.forum_candidates(limit=limit)


# -- former members ------------------------------------------------------------------------

def test_former_member_is_never_added(roster):
    persona(roster, 106, motivation='social')  # A stored persona whose row left the roster.
    persona(roster, 10, 'Testmaster', motivation='social')
    with roster.transaction():
        roster.conn.execute('UPDATE cast_member SET level=79 WHERE guid=105')
        insert(roster, 'realm_kick', guid=10, ts=1.0, rank=0)  # Never the human, even kicked.
    facts = ledger(roster)
    assert facts.former_members() == frozenset({106})
    with roster.transaction():
        insert(roster, 'cast_member', guid=106, name='Fenwick', race=2, **{'class': 1},
               gender=0, level=80, rank=3, online=0, updated=1000.0)
        insert(roster, 'realm_kick', guid=106, ts=2.0, rank=3)
        roster.conn.execute('DELETE FROM cast_member WHERE guid=10')
        roster.conn.execute("UPDATE persona_core SET json='{' WHERE guid=104")
        roster.conn.execute('DELETE FROM cast_member WHERE guid=104')
        assert former_members_in_unit(roster, human_guid=10) == frozenset({104, 106})
        assert former_members_in_unit(roster, human_guid=0) == frozenset({10, 104, 106})
        for bad in (-1, 1 << 32, True, '10'):
            with pytest.raises(ValueError):
                former_members_in_unit(roster, human_guid=bad)
    with pytest.raises(RuntimeError):
        former_members_in_unit(roster, human_guid=10)
    # A social former member is never chosen for the way back in, kicked or off the roster.
    fact = record(roster)
    assert 106 not in {plan.owner for plan in facts.back_in(fact.id, forum=True)}


@pytest.mark.parametrize('on', [False, True])
def test_his_kick_makes_a_former_member(tmp_path, store, clock, on):
    seed_test_cast(store)
    app = RealmApp(tmp_path, store, clock)
    enabled(tmp_path, on)
    realm = attach_realm(app)
    assert realm.enabled is on
    assert realm.ledger.store is store and realm.ledger.former_members() == frozenset()
    run(app.event(protocol.Guard(1000, 103, 'his_kick', 4, '')))
    assert realm.ledger.former_members() == frozenset({103})
    assert not app.sent
    app.engine.say_as.assert_not_called()
    run(realm.close())


# -- back_in(): the way back in, a plan only ------------------------------------------------

def social_cast(store, **game):
    for guid, name in ((101, 'Arkon'), (102, 'Belra'), (103, 'Coros'), (104, 'Dunna'), (105, 'Ebbo')):
        persona(store, guid, name, **game.get(str(guid), {'motivation': 'social'}))


def test_back_in_forum_audience(roster):
    social_cast(roster, **{'101': {'motivation': 'social'}, '102': {'motivation': 'competitor'},
                           '103': {'thinks_as': 'social'}, '104': {}, '105': {'motivation': 'explorer'}})
    fact = record(roster)
    before = realm_rows(roster)
    plans = ledger(roster).back_in(fact.id, forum=True)
    assert [plan.owner for plan in plans] == [101, 102, 103]
    with roster.transaction():
        _, proof = public_fact_in_unit(roster, fact.id, human=HUMAN)
        (stamp,) = audit_realm_reads_in_unit(roster, (f'realm:fact:{fact.id}',), human=HUMAN)[1]
        participants = tuple(json.loads(proof.qualification_json)['participants'])
    assert participants == (10, 101, 201)
    assert plans[0] == BackIn(fact.id, 1, stamp.fact_sha256, 101, 'forum', fact.ts, fact.text,
                              participants, proof)
    assert all(replace(plan, owner=101) == plans[0] for plan in plans)
    assert realm_rows(roster) == before
    with roster.transaction():
        for table in ('memory', 'realm_notable', 'realm_memory_dependency'):
            assert roster.conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == 0


def test_back_in_skips_former_human_capped(roster):
    social_cast(roster)
    persona(roster, 10, 'Testmaster', motivation='social')
    fact = record(roster)
    today = local_write_day(roster.clock.now())
    with roster.transaction():
        insert(roster, 'realm_kick', guid=101, ts=1.0, rank=3)
        insert(roster, 'realm_notable', day=today, guid=102, memory=999999)
        roster.conn.execute('DELETE FROM cast_member WHERE guid=103')
        insert(roster, 'realm_notable', day='2000-01-01', guid=104, memory=999998)
    facts = ledger(roster)
    assert [plan.owner for plan in facts.back_in(fact.id, forum=True)] == [104, 105]
    # Without his guid he cannot be told apart from the audience, so nobody is chosen.
    assert ledger(roster, replace(HUMAN, guid=0)).back_in(fact.id, forum=True) == ()
    # A fact without a current active public origin reaches nobody.
    with roster.transaction():
        roster.conn.execute("UPDATE realm_fact_origin SET state='revoked' WHERE fact=?", (fact.id,))
    assert facts.back_in(fact.id, forum=True) == ()


def test_channel_back_in_unsupported(roster):
    social_cast(roster)
    fact = record(roster, 'channel_raid_topic_v1')
    assert fact.source == 'channel'
    facts = ledger(roster)
    assert facts.back_in(fact.id) == ()
    assert facts.back_in(fact.id, place_zone=0) == ()
    assert [plan.owner for plan in facts.back_in(fact.id, forum=True)] == [101, 102, 103, 104, 105]
    for bad in (dict(place_zone=0, forum=True), dict(forum=1), dict(place_zone=-1),
                dict(place_zone=True)):
        with pytest.raises(ValueError):
            facts.back_in(fact.id, **bad)
    for bad in (0, True, '1'):
        with pytest.raises(ValueError):
            facts.back_in(bad, forum=True)


# -- guard alerts ----------------------------------------------------------------------------

@pytest.mark.parametrize('on', [False, True])
def test_guard_alert_logs_ids_only(tmp_path, store, clock, caplog, on):
    seed_test_cast(store)
    app = RealmApp(tmp_path, store, clock)
    enabled(tmp_path, on)
    realm = attach_realm(app)
    caplog.set_level(logging.DEBUG, logger='leeroy_mind')
    for action in ('gone', 'blocked'):
        run(app.event(protocol.Guard(1000, 103, action, 7, 'Quillon left in a huff')))
    for ignored in (protocol.Guard(1000, 104, 'readd', 7, ''), protocol.Guard(1000, 104, 'failed', 7, 'x'),
                    protocol.Guard(1000, 0, 'gone', 7, ''), protocol.Guard(1000, 1 << 32, 'gone', 7, ''),
                    protocol.Guard(1000, True, 'gone', 7, ''), protocol.Guard(1000, '105', 'gone', 7, ''),
                    protocol.Guard(1000, 105, ['gone'], 7, '')):
        run(app.event(ignored))
    alerts = [r for r in caplog.records if 'realm_guard_alert' in r.getMessage()]
    assert [(r.levelno, r.getMessage()) for r in alerts] == [
        (logging.WARNING, 'realm_guard_alert guid=103 action=gone'),
        (logging.WARNING, 'realm_guard_alert guid=103 action=blocked')]
    assert 'Quillon' not in caplog.text and 'huff' not in caplog.text
    assert realm.ledger.former_members() == frozenset()
    with store.transaction():
        assert store.conn.execute('SELECT count(*) FROM realm_kick').fetchone()[0] == 0
    assert not app.sent
    run(realm.close())
