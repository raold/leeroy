from dataclasses import replace
import json

import pytest

from conftest import run
from leeroy_mind.awareness import CardContext, card
from leeroy_mind.bond.night import stage_bond
from leeroy_mind.memory.callbacks import CallbackContext, pick_callback
from leeroy_mind.memory.recall import scope_ok, Recall
from leeroy_mind.realm.protocol import MailTaken

DAY = 86400


def night(h):
    h.clock.set(h.T)
    h.settings = replace(h.settings, romance_activities=True)
    ctx = h.ctx(window=(h.T-DAY, h.T))
    run(stage_bond(ctx))
    return ctx


def test_first_shared_kill_is_private_idempotent_and_whisper_callback(h, caplog):
    h.woman(101, 'fond')
    h.rel(101, 1, affinity=25)
    ref = h.boss(h.T-100, 631, 1, 'killed', (1,101))
    ctx = night(h)
    run(stage_bond(ctx))
    assert h.count('bond_moment') == 1
    memory = h.rows(101)[0]
    assert (memory.scope, memory.scope_with, memory.evidence) == ('private',1,(f'event:{ref}',))
    # one mind per member: their DM is the same conversation as their whispers
    assert scope_ok(memory, turn_channel='dm:101', to_guid=1)
    for channel, peer in [('guild',0), ('officer',0), ('say',1), ('dm:102',1), ('whisper',102)]:
        assert not scope_ok(memory, turn_channel=channel, to_guid=peer)
    now = h.T+3*DAY
    h.session(now-100, now+100)
    chosen = pick_callback(h.store.conn,101,CallbackContext(True,'whisper',1,(1,101),bond_enabled=True),now=now)
    assert chosen and chosen.memory_id == memory.id
    assert not pick_callback(h.store.conn,101,CallbackContext(True,'whisper',1,(1,101)),now=now)
    recall = Recall(h.store,None,h.clock,tau_days=20)
    assert not run(recall.recall(101,'',present=[1],addressee=1,channel='whisper',to_guid=1))
    assert run(recall.recall(101,'',present=[1],addressee=1,channel='whisper',to_guid=1,include_bond=True))
    assert not pick_callback(h.store.conn,101,CallbackContext(True,'guild',0,(1,101)),now=now)
    assert 'first recorded' not in caplog.text


def test_prior_recorded_kill_does_not_become_first_again(h):
    h.woman(101,'drawn')
    h.boss(h.T-2*DAY,631,1,'killed',(1,101))
    h.boss(h.T-100,631,2,'killed',(1,101))
    night(h)
    assert h.count('bond_moment') == 0


@pytest.mark.parametrize('channel,reciprocal,expected', [('whisper',True,1),('whisper',False,0),('dm:101',True,0),('guild',True,0)])
def test_only_reciprocal_late_game_whisper_makes_moment(h,channel,reciprocal,expected):
    h.woman(101,'lust')
    h.rel(101,1,affinity=30)
    at = h.T-22*3600
    h.say(at,1,'PRIVATE_SENTINEL',channel=channel,to=101)
    if reciprocal: h.say(at+15,101,'quiet company',channel=channel,to=1)
    night(h)
    assert h.count('bond_moment') == expected
    assert all(m.scope == 'private' and m.scope_with == 1 for m in h.rows(101))


@pytest.mark.parametrize('state,event,expected', [('sent',True,0),('taken',False,0),('taken',True,1)])
def test_gift_requires_actual_collection_receipt(h,state,event,expected):
    h.woman(101,'fond')
    at = h.T-100
    with h.store.transaction():
        h.store.conn.execute('INSERT INTO realm_mail(ts,sender,kind,subject,body,item_guid,state,taken) '
            'VALUES (?,?,?,?,?,?,?,?)', (at-100,101,'gift','Private subject','Private body',123,state,at if state=='taken' else None))
        if event: h.store.insert_game_event(MailTaken(int(at*1000),123,43015,1),session_id=None,ts=at)
    night(h)
    assert h.count('bond_moment') == expected
    if expected:
        now = h.T+3*DAY
        h.session(now-100,now+100)
        assert pick_callback(h.store.conn,101,CallbackContext(True,'whisper',1,(1,101),bond_enabled=True),now=now)


def test_daily_moods_and_card_privacy(h):
    for guid in (101,102,103): h.woman(guid,'fond')
    h.rel(102,1,affinity=30)
    h.session(h.T-5*DAY,h.T-5*DAY+3600)
    h.boss(h.T-100,631,1,'killed',(1,102))
    # A current session would contradict absence, so give the playful member paired whispers.
    h.say(h.T-90,1,'nice evening',channel='whisper',to=102)
    h.say(h.T-80,102,'agreed',channel='whisper',to=1)
    with h.store.transaction():
        h.store.conn.execute('DELETE FROM session WHERE started>?',(h.T-DAY,))
        h.store.conn.execute("INSERT INTO jealousy VALUES (103,102,'rivalry',90,?,?)",(h.T-50,json.dumps(['event:1'])))
    night(h)
    rows = {r['guid']:r for r in h.store.conn.execute('SELECT * FROM bond_mood')}
    assert rows[101]['mood']=='missing'
    assert rows[102]['mood']=='playful'
    # Absence wins when there is no actual recent company. Check jealousy separately below.
    private = card(h.store,101,context=CardContext(channel='whisper',to_guid=1,now=h.T,
                                                  romance_activities=True,jealousy=5)).render()
    assert 'missed his company' in private
    for channel,peer in [('guild',0),('officer',0),('dm:1',1),('whisper',102)]:
        assert 'missed his company' not in card(h.store,101,context=CardContext(channel=channel,to_guid=peer,
            now=h.T,romance_activities=True,jealousy=5)).render()
    assert 'missed his company' not in card(h.store,101,context=CardContext(channel='whisper',to_guid=1,now=h.T)).render()


@pytest.mark.parametrize('dial,expected', [(0,'quiet'),(2,'jealous'),(10,'jealous')])
def test_jealous_mood_is_capped_by_global_dial(h,dial,expected):
    h.woman(101,'drawn')
    h.session(h.T-100,h.T+100)
    with h.store.transaction():
        h.store.conn.execute("INSERT INTO jealousy VALUES (101,102,'rivalry',90,?,?)",(h.T-50,json.dumps(['chat:1'])))
    h.settings=replace(h.settings,jealousy=dial)
    night(h)
    row=h.store.conn.execute('SELECT * FROM bond_mood WHERE guid=101').fetchone()
    assert row['mood']==expected and row['strength']<=dial/10


def test_disabled_stage_changes_nothing_and_men_none_do_not_join(h):
    h.persona(101,player_gender='male',stance='none')
    h.woman(102,'none')
    h.boss(h.T-100,631,1,'killed',(1,101,102))
    run(stage_bond(h.ctx(window=(h.T-DAY,h.T))))
    assert h.count('bond_moment')==h.count('bond_mood')==0
    night(h)
    assert h.count('bond_moment')==h.count('bond_mood')==0


@pytest.mark.parametrize('positive',[False,True])
def test_discord_dm_counts_as_company_and_positive_memories_can_make_playful(h,positive):
    h.woman(101,'fond'); h.rel(101,1,affinity=30)
    h.session(h.T-5*DAY,h.T-5*DAY+3600)
    line=h.say(h.T-100,1,'good company',channel='dm:101',to=101)
    if positive:
        h.mem(101,'We had a good conversation.',t=h.T-90,valence=.7,actors=(1,101),
              evidence=(f'chat:{line}',),scope='private',scope_with=1)
    with h.store.transaction(): h.store.conn.execute('DELETE FROM session WHERE started>?',(h.T-DAY,))
    night(h)
    row=h.store.conn.execute('SELECT mood FROM bond_mood WHERE guid=101').fetchone()
    assert row['mood']==('playful' if positive else 'quiet')


@pytest.mark.parametrize('enabled',[False,True])
def test_bond_stage_is_only_registered_when_enabled(enabled):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from leeroy_mind.bond.night import attach
    runner=Mock()
    attach(runner,SimpleNamespace(romance_activities=enabled))
    assert runner.add_stage.called is enabled


@pytest.mark.parametrize('days,expected',[(1,'quiet'),(2,'quiet'),(4,'missing')])
def test_missing_uses_last_dm_even_before_this_nights_window(h,days,expected):
    h.woman(101,'fond')
    h.session(h.T-6*DAY,h.T-5*DAY)
    h.say(h.T-days*DAY,1,'fixture company',channel='dm:101',to=101)
    with h.store.transaction(): h.store.conn.execute('DELETE FROM session WHERE started>?',(h.T-5*DAY,))
    night(h)
    assert h.store.conn.execute('SELECT mood FROM bond_mood WHERE guid=101').fetchone()[0]==expected
