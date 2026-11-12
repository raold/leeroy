import asyncio
import json
import random
from types import SimpleNamespace

import pytest

from conftest import make_persona, run
from leeroy_mind.discord import store as ds
from leeroy_mind.discord.presence import Presence
from leeroy_mind.llm.scheduler import Priority
from leeroy_mind.server import dms, records
from leeroy_mind.server.config import ServerConfig
from leeroy_mind.storage.rows import Relationship
from leeroy_mind.talk.turns import TurnKind
from leeroy_mind.tuning.effective import effective, MemberTuning


def fixture(store,clock,monkeypatch,*,cfg=None):
    people = {g:make_persona(g,'Member'+str(g)) for g in range(101,106)}
    cast = {g:SimpleNamespace(zone=0) for g in people}
    world = SimpleNamespace(human_online=False,group=None,online=set(people),discord_members=lambda:cast)
    engine = SimpleNamespace(live_review=SimpleNamespace(config=SimpleNamespace(enabled=True)),
        cfg=SimpleNamespace(romance=SimpleNamespace(dial=5),talk=SimpleNamespace(chattiness=4)),
        _new_id=lambda:'dm-test',_to_him_topic=lambda p,now:'How was your day?',
        planned_turns=lambda:(),active_speakers=lambda **kw:frozenset())
    app = SimpleNamespace(store=store,clock=clock,human_guid=1,engine=engine,world=world,
        personas=people,guild=None,stopping=False,server=SimpleNamespace(lease=SimpleNamespace(ready=lambda:True)))
    snap = SimpleNamespace(personas=people,states={g:SimpleNamespace(presence=Presence('online',False,0)) for g in people},
        relationships={g:Relationship(g,1,affinity=60) for g in people},in_play=set(),away=set(),human=1,seeds=[])
    async def snapshot(*args,**kw):
        snap.meta = dict(store.conn.execute("SELECT key,value FROM meta WHERE key LIKE 'server:%'"))
        return snap
    monkeypatch.setattr(dms.runtime,'snapshot',snapshot)
    monkeypatch.setattr(dms,'presence_of',lambda *args,**kw:Presence('online',False,0))
    calls = []
    async def speak(turn,meta):
        if not meta.still_valid():
            return []
        calls.append((turn,meta))
        with store.transaction():
            ds.post(store,channel=turn.channel,author=turn.speaker,origin='mind',text='How was your day?',now=clock.now())
            records.posted(store,clock.local().date().isoformat(),clock.now(),dm_guid=turn.speaker)
        return ['How was your day?']
    engine.speak_meta = speak
    opener = dms.DmOpeners(app,cfg or ServerConfig(enabled=True),random.Random(25))
    opener._day = clock.local().date().isoformat()
    opener._next_at = clock.now()
    return opener,snap,calls


async def due(opener,clock):
    opener._day,opener._next_at = clock.local().date().isoformat(),clock.now()
    await opener.tick(clock.now())


def test_at_most_dms_per_day_and_restart_preserves_cap(store,clock,monkeypatch):
    async def go():
        opener,_,calls = fixture(store,clock,monkeypatch)
        for _ in range(8):
            await due(opener,clock)
            await clock.advance(121,step=121)
        assert len(calls)==3
        assert len({turn.speaker for turn,_ in calls})==3
        assert records.counts(store,clock.local().date().isoformat())['dms']==3
        restarted,_,fresh = fixture(store,clock,monkeypatch)
        await due(restarted,clock)
        assert not fresh
    run(go())


@pytest.mark.parametrize('condition',['game','window','lease','guards','disabled'])
def test_none_when_in_game_or_other_start_gate_closed(store,clock,monkeypatch,condition):
    async def go():
        opener,_,calls = fixture(store,clock,monkeypatch,cfg=ServerConfig(enabled=condition!='disabled'))
        if condition=='game':
            opener.app.world.human_online = True
        elif condition=='window':
            monkeypatch.setattr(dms,'in_window',lambda *args:True)
        elif condition=='lease':
            opener.app.server.lease.ready = lambda:False
        elif condition=='guards':
            opener.app.engine.live_review.config.enabled = False
        await due(opener,clock)
        assert not calls and store.get_meta('server:day:'+clock.local().date().isoformat()) is None
    run(go())


def test_no_second_opener_until_answer_or_48h_and_once_per_member_day(store,clock,monkeypatch):
    async def go():
        opener,snap,calls = fixture(store,clock,monkeypatch)
        snap.personas = {101:snap.personas[101]}
        await due(opener,clock)
        assert len(calls)==1
        now = clock.now()
        # Tomorrow, still unanswered and under 48 hours.
        await clock.advance(86400,step=86400)
        await due(opener,clock)
        assert len(calls)==1
        ds.post(store,channel='dm:101',author=1,origin='human',text='Good, thanks',now=clock.now())
        await due(opener,clock)
        assert len(calls)==2
        await clock.advance(121,step=121)
        await due(opener,clock)
        assert len(calls)==2
        await clock.advance(2*86400,step=86400)
        await due(opener,clock)
        assert len(calls)==3 and clock.now()-now>48*3600
    run(go())


@pytest.mark.parametrize('gender,forward,romantic',[('male',5,False),('female',0,False),('female',5,True)])
def test_romance_follows_whisper_rules(store,clock,monkeypatch,gender,forward,romantic):
    async def go():
        opener,snap,calls = fixture(store,clock,monkeypatch)
        p = make_persona(101,'Arkon',player_gender=gender,stance='lust')
        p = effective(p,MemberTuning({'forward':forward}))
        opener.app.personas[101] = p
        snap.personas = {101:p}
        await due(opener,clock)
        assert len(calls)==1
        turn,meta = calls[0]
        assert meta.casual is not romantic and meta.initiates
        assert turn.kind is TurnKind.OPEN and turn.priority is Priority.BACKGROUND
        assert turn.channel=='dm:101' and turn.to_guid==turn.addressee==1
        assert meta.why=='server:dm' and meta.review_required and meta.single_line
        assert meta.guards is opener._guards
    run(go())


def test_playing_away_and_offline_members_do_not_open(store,clock,monkeypatch):
    async def go():
        opener,snap,calls = fixture(store,clock,monkeypatch)
        snap.in_play={101,102}
        snap.away={103,104}
        snap.states[105]=SimpleNamespace(presence=Presence('offline',False,0))
        await due(opener,clock)
        assert not calls
    run(go())


def test_dm_seed_is_her_own_and_never_an_officer_fact(store,clock,monkeypatch):
    async def go():
        opener,snap,calls = fixture(store,clock,monkeypatch)
        snap.personas = {101:snap.personas[101]}
        snap.seeds = [SimpleNamespace(kind='life',about=(101,),fact='Coffee with a friend',officers_only=False),
            SimpleNamespace(kind='plan',about=(101,),fact='PRIVATE OFFICER FACT',officers_only=True),
            SimpleNamespace(kind='life',about=(102,),fact='OTHER MEMBER DAY',officers_only=False)]
        await due(opener,clock)
        assert calls[0][1].topic=='Coffee with a friend'
    run(go())


def test_login_during_pending_turn_redraws_logout_gap_and_close_joins(store,clock,monkeypatch):
    async def go():
        opener,_,calls = fixture(store,clock,monkeypatch)
        monkeypatch.setattr(opener.rng,'expovariate',lambda rate:0)
        entered,release = asyncio.Event(),asyncio.Event()
        async def delayed(turn,meta):
            entered.set()
            await release.wait()
            assert not meta.still_valid()
            return []
        opener.app.engine.speak_meta = delayed
        task = asyncio.create_task(due(opener,clock))
        await entered.wait()
        opener.app.world.human_online=True
        release.set()
        await task
        assert not calls
        opener.app.world.human_online=False
        # No tick observed his login: only the pending turn's delivery gate did.
        opener._next_at = clock.now()-1
        logged_out = clock.now()
        await opener.tick(logged_out)
        assert opener._next_at>=logged_out+60
        await clock.advance(59,step=59)
        await opener.tick(clock.now())
        assert not calls
        await clock.advance(opener._next_at-clock.now(),step=300)
        entered.clear()
        release.clear()
        task = asyncio.create_task(opener.tick(clock.now()))
        await entered.wait()
        await opener.close()
        await asyncio.gather(task,return_exceptions=True)
        assert opener._pending is None
    run(go())


def test_first_tick_schedules_and_phase_changes_pacing(store,clock,monkeypatch):
    opener,_,calls = fixture(store,clock,monkeypatch)
    opener._day=None
    run(opener.tick(clock.now()))
    assert opener._next_at>clock.now() and not calls
    # Same exponential draw: evening has a shorter gap than the morning.
    opener.rng.seed(4)
    morning=opener._gap(clock.local().replace(hour=9))
    opener.rng.seed(4)
    evening=opener._gap(clock.local().replace(hour=20))
    assert evening<morning


def test_zero_chattiness_never_opens_over_a_day(store,clock,monkeypatch):
    async def go():
        opener,_,calls = fixture(store,clock,monkeypatch)
        opener.app.engine.cfg.talk.chattiness = 0
        for _ in range(48):
            await due(opener,clock)
            await clock.advance(1800,step=1800)
        assert not calls
        assert not store.conn.execute("SELECT 1 FROM meta WHERE key LIKE 'server:day:%'").fetchone()
    run(go())


def test_chattiness_eight_has_shorter_mean_gap_than_four(store,clock,monkeypatch):
    opener,_,_ = fixture(store,clock,monkeypatch)
    local = clock.local().replace(hour=20)
    opener.rng.seed(72)
    normal = sum(opener._gap(local) for _ in range(1000))/1000
    opener.app.engine.cfg.talk.chattiness = 8
    opener.rng.seed(72)
    chatty = sum(opener._gap(local) for _ in range(1000))/1000
    assert chatty<normal


@pytest.mark.parametrize('busy',[False,True])
def test_logout_redraws_expired_dm_deadline_and_keeps_first_minute_quiet(store,clock,monkeypatch,busy):
    async def go():
        opener,_,calls = fixture(store,clock,monkeypatch)
        monkeypatch.setattr(opener.rng,'expovariate',lambda rate:0)
        if busy:
            await opener._lock.acquire()
        opener.app.world.human_online = True
        await opener.tick(clock.now())
        if busy:
            opener._lock.release()
        await clock.advance(3600,step=3600)
        opener.app.world.human_online = False
        logged_out = clock.now()
        await opener.tick(logged_out)
        assert opener._next_at>=logged_out+60 and not calls
        await clock.advance(59,step=59)
        await opener.tick(clock.now())
        assert not calls
        await clock.advance(opener._next_at-clock.now(),step=300)
        await opener.tick(clock.now())
        assert len(calls)==1
    run(go())


@pytest.mark.parametrize('source',['queued','active'])
def test_busy_turn_keeps_member_out_of_dm_candidates(store,clock,monkeypatch,source):
    async def go():
        opener,snap,calls = fixture(store,clock,monkeypatch)
        snap.personas = {101:snap.personas[101]}
        if source=='queued':
            opener.app.engine.planned_turns = lambda:(SimpleNamespace(speaker=101),)
        else:
            opener.app.engine.active_speakers = lambda **kw:frozenset({101})
        await due(opener,clock)
        assert not calls
    run(go())


@pytest.mark.parametrize('source',['queued','active'])
def test_new_busy_turn_invalidates_pending_dm_before_delivery(store,clock,monkeypatch,source):
    async def go():
        opener,_,calls = fixture(store,clock,monkeypatch)
        original = opener.app.engine.speak_meta
        async def queued(turn,meta):
            if source=='queued':
                opener.app.engine.planned_turns = lambda:(SimpleNamespace(speaker=turn.speaker),)
            else:
                opener.app.engine.active_speakers = lambda **kw:frozenset({turn.speaker})
            return await original(turn,meta)
        opener.app.engine.speak_meta = queued
        await due(opener,clock)
        assert not calls
    run(go())


def test_pending_dm_validity_excludes_its_own_active_task(store,clock,monkeypatch):
    async def go():
        opener,_,calls = fixture(store,clock,monkeypatch)
        original = opener.app.engine.speak_meta
        async def speaking(turn,meta):
            current = asyncio.current_task()
            opener.app.engine.active_speakers = lambda *,exclude_task=None: (
                frozenset() if exclude_task is current else frozenset({turn.speaker}))
            return await original(turn,meta)
        opener.app.engine.speak_meta = speaking
        await due(opener,clock)
        assert len(calls)==1
    run(go())


@pytest.mark.parametrize('raw',['not-json','[]','{"dms":true}','{"messages":-1}'])
def test_bad_day_receipt_fails_closed_and_logs_once(store,clock,monkeypatch,caplog,raw):
    async def go():
        opener,_,calls = fixture(store,clock,monkeypatch)
        store.set_meta('server:day:'+clock.local().date().isoformat(),raw)
        await due(opener,clock)
        await due(opener,clock)
        assert not calls
        assert sum(r.message=='server_day_counter_bad' for r in caplog.records)==1
    run(go())


def test_bad_open_receipt_fails_closed(store,clock,monkeypatch):
    async def go():
        opener,snap,calls = fixture(store,clock,monkeypatch)
        snap.personas = {101:snap.personas[101]}
        store.set_meta(dms.OPEN_KEY.format(guid=101),'not-a-time')
        await due(opener,clock)
        assert not calls
    run(go())


def test_concurrent_ticks_share_one_admission(store,clock,monkeypatch):
    async def go():
        opener,snap,calls = fixture(store,clock,monkeypatch)
        entered,release = asyncio.Event(),asyncio.Event()
        reads = []
        snapshot = dms.runtime.snapshot
        async def held(*args,**kw):
            reads.append(1)
            entered.set()
            await release.wait()
            return await snapshot(*args,**kw)
        monkeypatch.setattr(dms.runtime,'snapshot',held)
        opener._gap = lambda local:0
        task = asyncio.create_task(due(opener,clock))
        await entered.wait()
        await due(opener,clock)
        assert len(reads)==1
        release.set()
        await task
        assert len(calls)==1
    run(go())


def test_changed_owner_cannot_receive_pending_old_pair_opener(store,clock,monkeypatch):
    async def go():
        opener,_,calls = fixture(store,clock,monkeypatch)
        async def changed(turn,meta):
            opener.app.human_guid = 105
            assert not meta.still_valid()
            return []
        opener.app.engine.speak_meta = changed
        await due(opener,clock)
        assert not calls
    run(go())


@pytest.mark.parametrize('interrupt',['his_post','answer_queued'])
def test_human_answer_cancels_pending_opener_without_stopping_the_ticker(store,clock,monkeypatch,interrupt):
    async def go():
        opener,snap,calls = fixture(store,clock,monkeypatch)
        snap.personas = {101:snap.personas[101]}
        original = opener.app.engine.speak_meta
        entered,release = asyncio.Event(),asyncio.Event()
        captured = []
        async def pending(turn,meta):
            captured.append(meta)
            entered.set()
            await release.wait()
            return await original(turn,meta)
        opener.app.engine.speak_meta = pending
        resumed = []
        async def ticker():
            await due(opener,clock)
            resumed.append(True)
        task = asyncio.create_task(ticker())
        await entered.wait()
        ds.post(store,channel='dm:101',author=1,origin='human',text='How are you?',now=clock.now())
        getattr(opener,interrupt)('dm:101' if interrupt=='his_post' else 101)
        await task
        assert resumed==[True] and not task.cancelled() and not calls
        assert opener._pending is None and opener._pending_guid is None
        assert not captured[0].still_valid()
        assert store.get_meta('server:day:'+clock.local().date().isoformat()) is None
        opener.app.engine.speak_meta = original
        await due(opener,clock)
        assert not calls  # his new conversation gets at least the quiet interval
        await clock.advance(601,step=601)
        assert not captured[0].still_valid()  # its old admission never revives
        await due(opener,clock)
        assert len(calls)==1
    run(go())


@pytest.mark.parametrize('interrupt',['his_post','answer_queued'])
def test_human_answer_during_selection_invalidates_that_candidate(store,clock,monkeypatch,interrupt):
    async def go():
        opener,snap,calls = fixture(store,clock,monkeypatch)
        snap.personas = {101:snap.personas[101]}
        entered,release = asyncio.Event(),asyncio.Event()
        original = dms.runtime.snapshot
        async def held(*args,**kw):
            entered.set()
            await release.wait()
            return await original(*args,**kw)
        monkeypatch.setattr(dms.runtime,'snapshot',held)
        task = asyncio.create_task(due(opener,clock))
        await entered.wait()
        getattr(opener,interrupt)('dm:101' if interrupt=='his_post' else 101)
        release.set()
        await task
        assert not calls and opener._pending is None
    run(go())


@pytest.mark.parametrize('interrupt',['his_post','answer_queued'])
def test_another_member_answer_does_not_cancel_the_pending_pair(store,clock,monkeypatch,interrupt):
    async def go():
        opener,snap,calls = fixture(store,clock,monkeypatch)
        snap.personas = {101:snap.personas[101]}
        entered,release = asyncio.Event(),asyncio.Event()
        original = opener.app.engine.speak_meta
        async def pending(turn,meta):
            entered.set()
            await release.wait()
            return await original(turn,meta)
        opener.app.engine.speak_meta = pending
        task = asyncio.create_task(due(opener,clock))
        await entered.wait()
        getattr(opener,interrupt)('dm:102' if interrupt=='his_post' else 102)
        opener.his_post('discord:general')
        release.set()
        await task
        assert len(calls)==1 and calls[0][0].speaker==101
    run(go())


def test_canceling_the_ticker_itself_still_propagates(store,clock,monkeypatch):
    async def go():
        opener,_,calls = fixture(store,clock,monkeypatch)
        entered,release = asyncio.Event(),asyncio.Event()
        async def pending(turn,meta):
            entered.set()
            await release.wait()
            return []
        opener.app.engine.speak_meta = pending
        task = asyncio.create_task(due(opener,clock))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert opener._pending is None and not calls
    run(go())
