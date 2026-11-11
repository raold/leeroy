import asyncio
from dataclasses import replace
import json
import random
from types import SimpleNamespace as NS
from unittest.mock import Mock, AsyncMock

import pytest

from banter.test_banter import rig, finish, human
from banter.test_jokes import reward, app
from leeroy_mind.banter.service import Banter, choose_cast, LIMIT_KEY, TEASE_KEY
from leeroy_mind.banter.topics import Topic, event_topic, jokes, joke_uses, JOKE_COOLDOWN
from leeroy_mind.banter.night import reward_jokes
from leeroy_mind.protocol import LootWon
from leeroy_mind.talk.pipeline import TurnMeta
from leeroy_mind.talk.turns import Turn, TurnKind
from tuning.test_tuning_consumers import tune


def topic(rig,kind='quiet',target=0):
    return Topic('fixture',kind,rig.clock.now(),'The cart lost a wheel.',target,target==1)


def test_offline_night_due_cannot_take_lease_or_run_banter(rig,monkeypatch):
    b=rig.banter; rig.world.human_online=False
    arbiter=Mock(); b.app.arbiter=arbiter
    b.app.scheduler=NS(due=True,cancel_run=Mock())
    b.pending.append(topic(rig))
    monkeypatch.setattr(b,'candidates',lambda _: [101,102,103])
    rig.sync(b.tick())
    assert not rig.llm.calls and not b.active and not arbiter.mock_calls
    b.app.scheduler.cancel_run.assert_not_called()
    b.app.discord_talk=NS(_held=True,_until=rig.clock.now()+600)
    assert b.allowed('discord:general',rig.clock.now())
    assert not b.allowed('guild',rig.clock.now())
    assert not b.allowed('discord:general',rig.clock.now()+600)


@pytest.mark.parametrize('channel',['guild','discord:general'])
@pytest.mark.parametrize('gate',['boss','zero'])
def test_five_man_boss_and_zero_chattiness_hold_threads(rig,channel,gate):
    if gate=='boss': rig.engine._group_fights={(574,1):rig.clock.now()}
    else: rig.engine.cfg=replace(rig.engine.cfg,talk=replace(rig.engine.cfg.talk,chattiness=0))
    assert not rig.banter.allowed(channel,rig.clock.now())


def test_silent_member_target_mood_and_active_hurt_are_respected(rig,monkeypatch):
    app=rig.banter.app; original=app.personas.get
    monkeypatch.setattr(app.personas,'get',lambda g:tune(original(g),talkative=0) if g==101 else original(g))
    t=topic(rig,'loot',1)
    rig.store.conn.execute("INSERT INTO attraction_state(guid,stance,version,hurt_until,updated) VALUES(102,'fond',1,?,?)",
                           (rig.clock.now()+600,rig.clock.now()))
    for seed in range(50):
        assert not {101,102}.intersection(choose_cast(app,[101,102,103,104,105],t,random.Random(seed)))
    rig.store.put_state(replace(rig.store.get_state(103),mood=-.7))
    assert not choose_cast(app,[104,105],topic(rig,'loot',103),random.Random(1))
    rig.store.conn.execute("INSERT INTO feud(a,b,started,cause) VALUES(104,105,?,'test')",(rig.clock.now(),))
    assert not choose_cast(app,[103,104],topic(rig,'loot',105),random.Random(1))


def test_rendered_prompt_names_owner_and_library_item_without_conflicting_opener(rig,monkeypatch):
    b=rig.banter
    b.app.library=NS(item_info=lambda ids:{40395:{'name':'Test Sapphire Crown'}})
    ev=LootWon(0,1,40395,'bridge fallback','roll')
    rig.store.insert_game_event(ev,session_id=None,ts=rig.clock.now())
    t=event_topic(b.app,ev)
    monkeypatch.setattr(b,'candidates',lambda _: [101,102,103])
    async def go(): await finish(rig,asyncio.create_task(b.thread('guild',t,[101,102,103])))
    rig.sync(go())
    prompts=json.dumps(rig.llm.calls)
    assert rig.engine.human.called+' received Test Sapphire Crown' in prompts
    assert all(text not in prompts for text in ('Member 1','40395','the owner',"owner's event",'something from your own evening'))


def test_banter_channel_stays_public_when_voice_would_route_it_elsewhere(rig,monkeypatch):
    from voice.conftest import VRig
    voice=VRig(rig)
    voice.him('guild_hall')
    for guid in (101,102,103): voice.seat(guid,'guild_hall')
    ordinary=Turn(TurnKind.ASIDE,101,'guild')
    assert rig.engine._route(ordinary,TurnMeta('ordinary')) and ordinary.channel=='voice:guild_hall'
    fixed=Turn(TurnKind.ASIDE,101,'guild')
    assert not rig.engine._route(fixed,TurnMeta('banter',keep_channel=True))
    assert fixed.channel=='guild'
    monkeypatch.setattr(rig.banter,'candidates',lambda _: [101,102,103])
    async def go(): await finish(rig,asyncio.create_task(rig.banter.thread('guild',topic(rig),[101,102,103])))
    rig.sync(go())
    assert rig.sent.says() and not voice.speaks()
    assert all(row['channel']=='guild' for row in voice.lines())


def test_discord_turns_stay_one_line_and_do_not_pause_game_bursts(rig,monkeypatch):
    b=rig.banter; seen=[]
    monkeypatch.setattr(b,'candidates',lambda _: [101,102,103])
    original=rig.engine._route
    def route(turn,meta):
        seen.append((turn.max_lines,rig.engine.banter_active))
        return original(turn,meta)
    monkeypatch.setattr(rig.engine,'_route',route)
    async def go(): await finish(rig,asyncio.create_task(b.thread('discord:general',topic(rig),[101,102,103])))
    rig.sync(go())
    assert seen and all(item==(1,False) for item in seen)


def seed_joke(h,index,channel='guild',*,invalid=False):
    line=h.say(h.T,102,f'the cart wheel escaped {index}',channel=channel)
    if invalid: h.store.conn.execute('DELETE FROM utterance WHERE text=?',(f'the cart wheel escaped {index}',))
    cur=h.store.conn.execute("INSERT INTO lexicon(kind,term,origin_ref,originator,born,last_used,heat) "
        "VALUES('joke',?,?,102,?,?,1)",(f'cart {index}',f'chat:{line}',h.T,h.T))
    return cur.lastrowid


@pytest.mark.parametrize('other',['guild','discord:general','invalid'])
def test_medium_and_invalid_origins_cannot_fill_candidate_window(h,other):
    channel='discord:general' if other=='guild' else 'guild'
    for i in range(17): seed_joke(h,i,channel if other=='invalid' else other,invalid=other=='invalid')
    ident=seed_joke(h,99,channel)
    found=list(jokes(app(h),channel,h.T))
    assert any(t.key==f'joke:{ident}' for t in found)


def test_late_funny_vote_is_ingested_within_seven_days(h):
    line=h.say(h.T-2*86400,102,'the cart wheel escaped again')
    reward(h,line)
    assert reward_jokes(h.ctx(window=(h.T-86400,h.T+1)))==1


def test_joke_origin_join_looks_up_chat_by_primary_key(h):
    seed_joke(h,1)
    statements=[]; h.store.conn.set_trace_callback(statements.append)
    assert list(jokes(app(h),'guild',h.T))
    h.store.conn.set_trace_callback(None)
    query=next(s for s in statements if s.startswith('SELECT l.id,l.term,l.origin_ref'))
    plan=h.store.conn.execute('EXPLAIN QUERY PLAN '+query).fetchall()
    assert any('SEARCH c USING INTEGER PRIMARY KEY' in row[3] for row in plan)


def test_empty_thread_refunds_tease_and_hourly_slot(rig,monkeypatch):
    b=rig.banter; t=topic(rig,'death',1)
    assert b.reserve(t,rig.clock.now())
    monkeypatch.setattr(b,'candidates',lambda _: [101,102,103])
    monkeypatch.setattr(rig.engine,'_speak',AsyncMock(return_value=[]))
    rig.sync(b.thread('guild',t,[101,102,103]))
    assert not b.starts(rig.clock.now())
    assert rig.store.get_meta(TEASE_KEY) is None and b.unused(t,rig.clock.now())


def test_owner_queries_are_indexed_and_cached_then_notice_a_new_line(rig):
    b=rig.banter; statements=[]
    rig.store.conn.set_trace_callback(statements.append)
    b.owner_last('guild')
    assert any('INDEXED BY chat_line_ts' in s for s in statements)
    statements.clear()
    for _ in range(10): b.owner_last('guild')
    assert not any('from_guid' in s for s in statements)
    ident=human(rig)
    assert b.owner_last('guild')[0]==ident
    assert any('WHERE id>' in s for s in statements)
    rig.store.conn.set_trace_callback(None)


def test_joke_provenance_is_cached_but_a_private_origin_invalidates_it(rig,monkeypatch):
    from leeroy_mind.banter import service
    b=rig.banter; t=Topic('joke:1','joke',rig.clock.now(),'quoted',source_channel='guild')
    check=Mock(return_value=True); monkeypatch.setattr(service,'current_joke',check)
    for _ in range(10): assert b.valid('guild',0,t,rig.clock.now())
    assert check.call_count==1
    check.return_value=False
    with rig.store.transaction(): rig.store.set_meta('private-test','changed')
    assert not b.valid('guild',0,t,rig.clock.now()) and check.call_count==2


def test_used_jokes_cool_down_fade_and_cannot_fill_the_evening(rig):
    b=rig.banter; now=rig.clock.now(); t=Topic('joke:1','joke',now,'quoted')
    assert b.reserve(t,now)
    b.settle_reservation(t,1)
    assert joke_uses(rig.store,t.key)==(now,1)
    assert not b.unused(Topic('joke:2','joke',now,'another'),now)
    rig.sync(rig.advance(86400,86400))
    assert not b.unused(t,rig.clock.now())
    rig.sync(rig.advance(JOKE_COOLDOWN-86400+1,JOKE_COOLDOWN-86400+1))
    assert b.unused(t,rig.clock.now())
    rig.store.set_meta('banter:joke-use:'+t.key,json.dumps({'at':now,'count':4}))
    assert not b.unused(t,rig.clock.now())


def test_chattiness_scales_spacing_before_the_next_thread(rig,monkeypatch):
    b=rig.banter; now=rig.clock.now()
    rig.store.set_meta(LIMIT_KEY,json.dumps([now-1900]))
    b.pending.append(topic(rig))
    called=AsyncMock(); monkeypatch.setattr(b,'thread',called)
    monkeypatch.setattr(b,'candidates',lambda _: [101,102,103])
    rig.engine.cfg=replace(rig.engine.cfg,talk=replace(rig.engine.cfg.talk,chattiness=1))
    rig.sync(b.tick()); called.assert_not_called()
    rig.engine.cfg=replace(rig.engine.cfg,talk=replace(rig.engine.cfg.talk,chattiness=4))
    rig.sync(b.tick()); called.assert_awaited_once()
