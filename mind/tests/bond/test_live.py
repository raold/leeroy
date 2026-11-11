import asyncio
from dataclasses import replace
from types import SimpleNamespace as NS
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from conftest import run, seed_test_cast
from leeroy_mind.protocol import Ack, Chat, Hello, Group
from leeroy_mind.realm import protocol as p
from leeroy_mind.realm.bond import Bond
from leeroy_mind.realm.places import Places, DIST
from leeroy_mind.realm.service import attach_realm
from leeroy_mind.talk.world import WorldView
from realm.fakes import RealmApp, enabled
from realm.test_realm_speech import settle
from tuning.test_tuning_consumers import tune


@pytest.fixture
def live(tmp_path,store,clock):
    app=RealmApp(tmp_path,store,clock)
    app.personas=seed_test_cast(store)
    for guid in (101,102,103):
        old=app.personas[guid]
        app.personas[guid]=replace(old,identity=replace(old.identity,player_gender='female'),
                                  romance=replace(old.romance,stance='fond'))
    app.cfg=replace(app.cfg,romance=replace(app.cfg.romance,activities=True))
    app.world=WorldView(is_officer=lambda g:False)
    app.world.human_guid=1
    app.world.human_online=True
    app.world.human_zone=1637
    app.world.online={101,102,103}
    app.world.members=dict(store.cast())
    app.engine._login_at=clock.now()
    app.engine._may_whisper=lambda now:True
    app.engine._his_talk_at=lambda:float('-inf')
    app.engine._whisper_threads=lambda now:set()
    app.engine.whispers=NS(refusal=lambda now:None)
    app.raid=NS(phase=lambda now:'none')
    enabled(tmp_path,True)
    realm=attach_realm(app)
    realm.part('speech')._places=Places.load(DIST)
    bond=realm.part('bond')
    async def say(guid,channel,text,**kw):
        if kw['before_transport']():
            kw['on_recorded'](1,Ack(int(clock.now()*1000),'said',True,''))
            return True
        return False
    app.engine.say_as.side_effect=say
    return NS(app=app,realm=realm,bond=bond,store=store,clock=clock)


async def start(h):
    await h.app.connection(True)
    await h.app.event(Hello(int(h.clock.now()*1000),'test','1',21,1))
    await h.bond.tick()
    h.app.engine._login_at=h.clock.now()-1800


async def observation(h,near=False,ready=True,*,mutate=None):
    cmd=h.bond.query[0]
    ev=p.RealmVisitState(int(h.clock.now()*1000),cmd.id,cmd.place,21,1,ready,near)
    await h.app.event(mutate(ev) if mutate else ev)
    await settle(h.realm)


async def offer(h):
    await start(h)
    await h.bond.tick()
    await settle(h.realm)
    await observation(h)
    assert h.bond.row()['state']=='offered'


async def answer(h,text='yes',channel='whisper',to=101):
    await h.app.event(Chat(int(h.clock.now()*1000),channel,1,'Testmage',to,text,1,1637))


@pytest.mark.parametrize('arrival',[False,True])
def test_accept_then_exactly_once_journaled_move_and_sit(live,arrival):
    async def go():
        h=live
        await offer(h)
        if arrival:
            await h.clock.advance(10,step=10)
            await h.bond.tick(); await settle(h.realm)
            await observation(h,near=True)
        else: await answer(h)
        assert h.bond.row()['state']=='accepted'
        await h.bond.tick(); h.realm.queue.tick(); await settle(h.realm)
        moves=[c for c in h.app.sent if isinstance(c,p.RealmMove)]
        assert len(moves)==1 and moves[0].social
        job=p.RealmJob(int(h.clock.now()*1000),moves[0].id,'realm_move','done','',101,0)
        await h.app.event(job); await h.app.event(job)
        await h.clock.advance(10,step=10)
        await h.bond.tick(); await settle(h.realm); await observation(h,near=True)
        await h.bond.tick(); await settle(h.realm)
        await h.bond.tick(); await settle(h.realm)
        emotes=[c for c in h.app.sent if isinstance(c,p.RealmEmote)]
        assert len(emotes)==1 and emotes[0].emote=='sit' and emotes[0].social
        assert h.store.conn.execute("SELECT state FROM realm_direct WHERE command_id=?",(emotes[0].id,)).fetchone()[0]=='acked'
        assert len([c for c in h.app.sent if isinstance(c,p.RealmMove)])==1
        h.app.engine.speak.assert_awaited_once()
        turn=h.app.engine.speak.call_args.args[0]
        assert turn.channel=='whisper' and turn.to_guid==1
        await h.realm.close()
    run(go())


@pytest.mark.parametrize('response',['no thanks','ignore','discord'])
def test_declined_ignored_and_dm_are_not_acceptance(live,response):
    async def go():
        h=live; await offer(h)
        if response=='discord': await answer(h,channel='dm:101')
        elif response!='ignore': await answer(h,response)
        if response!='no thanks':
            assert h.bond.row()['state']=='offered'
            await h.clock.advance(601,step=601)
        await h.bond.tick(); h.realm.queue.tick(); await settle(h.realm)
        assert not any(isinstance(c,p.RealmMove) for c in h.app.sent)
        assert h.bond.row() is None
        await h.realm.close()
    run(go())


def test_group_change_at_actual_dispatch_refuses_move(live):
    async def go():
        h=live; await offer(h); await answer(h); await h.bond.tick()
        h.app.world.set_group(Group(0,1,False,(1,102)))
        h.realm.queue.tick(); await settle(h.realm)
        assert not any(isinstance(c,p.RealmMove) for c in h.app.sent)
        assert h.bond.row() is None
        await h.realm.close()
    run(go())


def test_unknown_move_survives_restart_without_resend_or_release(live):
    async def go():
        h=live; await offer(h); await answer(h); await h.bond.tick()
        h.realm.queue.tick(); await settle(h.realm)
        ident=h.bond.row()['id']
        restarted=Bond(h.realm)
        await restarted.tick()
        assert h.bond.row(ident)['state']=='unknown'
        assert h.realm.action_store.holds()
        assert len([c for c in h.app.sent if isinstance(c,p.RealmMove)])==1
        await h.realm.close()
    run(go())


def test_day_limits_include_declines_and_use_local_calendar(live):
    async def go():
        h=live; await offer(h); await answer(h,'no')
        await h.clock.advance(10,step=10)
        await h.bond.tick(); await settle(h.realm); await observation(h)
        row=h.bond.row()
        assert row['guid']==102 and row['day']==h.clock.local().date().isoformat()
        await answer(h,'no',to=102)
        await h.clock.advance(10,step=10)
        await h.bond.tick(); await settle(h.realm)
        assert h.store.conn.execute('SELECT count(*) FROM bond_invite').fetchone()[0]==2
        await h.clock.advance(86400,step=86400)
        await h.bond.tick(); await settle(h.realm); await observation(h)
        assert h.bond.row()['guid']==103  # The same two do not monopolize each new day.
        await h.realm.close()
    run(go())


@pytest.mark.parametrize('mutation',[lambda e:replace(e,id='wrong'),lambda e:replace(e,human_guid=2),
    lambda e:replace(e,guild_id=22),lambda e:replace(e,ts=e.ts-20000)])
def test_unmatched_native_observations_do_not_offer(live,mutation):
    async def go():
        h=live; await start(h); await h.bond.tick(); await settle(h.realm)
        await observation(h,mutate=mutation)
        assert h.bond.row() is None
        await h.realm.close()
    run(go())


def test_off_switch_and_owner_already_there_do_nothing(live,caplog):
    async def go():
        h=live; await start(h)
        h.app.cfg=replace(h.app.cfg,romance=replace(h.app.cfg.romance,activities=False))
        await h.bond.tick(); assert not h.app.sent
        h.app.cfg=replace(h.app.cfg,romance=replace(h.app.cfg.romance,activities=True))
        await h.bond.tick(); await settle(h.realm); await observation(h,near=True)
        assert h.bond.row() is None
        await h.realm.close()
    run(go())
    assert 'Fancy sitting' not in caplog.text and 'Testmage' not in caplog.text


def test_forward_zero_and_midnight_do_not_grant_extra_invitations(live,monkeypatch):
    async def go():
        h=live
        monkeypatch.setattr(h.clock,'local',lambda:datetime.fromtimestamp(h.clock.now(),ZoneInfo('America/New_York')))
        await offer(h); await answer(h,'no')
        h.app.personas[102]=tune(h.app.personas[102],forward=0)
        h.app.personas[103]=tune(h.app.personas[103],forward=0)
        # UTC date changes; New York's day has not. Member 101 stays used.
        await h.clock.advance(4*3600+10,step=4*3600+10)
        await h.bond.tick(); await settle(h.realm)
        assert h.bond.row() is None
        assert h.store.conn.execute('SELECT count(*) FROM bond_invite').fetchone()[0]==1
        await h.realm.close()
    run(go())


@pytest.mark.parametrize('change',['preempt','disconnect','disable'])
def test_higher_priority_or_disabled_visit_never_sits(live,change):
    async def go():
        h=live; await offer(h); await answer(h); await h.bond.tick()
        h.realm.queue.tick(); await settle(h.realm)
        row=h.bond.row(); ident=row['id']
        move=next(c for c in h.app.sent if isinstance(c,p.RealmMove))
        await h.app.event(p.RealmJob(int(h.clock.now()*1000),move.id,'realm_move','done','',101,0))
        if change=='preempt': h.realm.bodies.claim('training',[101],rank='training',until=h.clock.now()+100)
        elif change=='disconnect': await h.app.connection(False)
        else: h.app.cfg=replace(h.app.cfg,romance=replace(h.app.cfg.romance,activities=False))
        await h.bond.tick(); await settle(h.realm)
        assert not any(isinstance(c,p.RealmEmote) for c in h.app.sent)
        assert h.bond.row(ident)['state'] not in ('offered','moving','seated')
        assert 101 not in h.realm.bodies.claims_for('bond:'+ident)
        await h.realm.close()
    run(go())


@pytest.mark.parametrize('value',['"yes"','1','[]','false'])
def test_invalid_or_false_activities_switch_is_off(tmp_path,value):
    from leeroy_mind.config import load_config
    path=tmp_path/'mind.toml'
    path.write_text('[romance]\nactivities='+value)
    assert load_config(path).romance.activities is False
