"""Review regressions with disposable SQLite and the recording Realm transport."""
import asyncio
from dataclasses import replace
from types import MethodType, SimpleNamespace as NS

import pytest

from conftest import run
from bond.test_live import live, start, offer, answer, observation
from realm.test_realm_speech import settle
from leeroy_mind.protocol import Hello, Ack
from leeroy_mind.realm import protocol as p
from leeroy_mind.realm.actions import ActionRejected
from leeroy_mind.realm.action_store import ActionCapacity, ActionUnavailable
from leeroy_mind.bodies import BodyEvidenceUnavailable
from leeroy_mind.realm.direct import DirectRefused
from leeroy_mind.talk.reactions import Reactions
from leeroy_mind.talk.raid import RaidPhase


@pytest.mark.parametrize('gate',['login','afk','combat','wipe_silence','wipe_debrief','boss',
                                'recent_line','chattiness','thread','spacing'])
def test_offer_uses_actual_unprompted_whisper_gates(live,gate):
    async def go():
        h=live; await start(h)
        e=h.app.engine
        e.human=NS(guid=1); e.world=h.app.world; e.raid=h.app.raid
        for method in ('_may_whisper','_initiation_held','_login_quiet'):
            setattr(e,method,MethodType(getattr(Reactions,method),e))
        e._in_boss_fight=lambda now:False
        assert h.bond.can_offer(101)
        if gate=='login': e._login_at=h.clock.now()-2
        elif gate=='afk': h.app.world.human_afk=True
        elif gate in ('combat','wipe_silence','wipe_debrief'): e.raid.phase=lambda now:RaidPhase(gate)
        elif gate=='boss': e._in_boss_fight=lambda now:True
        elif gate=='recent_line': e._his_talk_at=lambda:h.clock.now()-59
        elif gate=='chattiness': h.app.cfg=replace(h.app.cfg,talk=replace(h.app.cfg.talk,chattiness=0))
        elif gate=='thread': e._whisper_threads=lambda now:{101}
        elif gate=='spacing': e.whispers.refusal=lambda now:'spacing'
        assert not h.bond.can_offer(101)
        # An observation already in flight cannot bypass the gate.
        h.bond.observed=(p.RealmVisitState(int(h.clock.now()*1000),'q','org_inn',21,1,True,False),h.clock.now(),h.realm.action_context())
        h.bond.offer(101,h.clock.local().date().isoformat(),'org_inn')
        assert h.bond.row() is None
        await h.realm.close()
    run(go())


@pytest.mark.parametrize('error',[ActionRejected,ActionCapacity,ActionUnavailable,BodyEvidenceUnavailable])
def test_rejected_bond_move_keeps_realm_loop_alive(live,monkeypatch,error):
    async def go():
        h=live; await offer(h); await answer(h)
        ident=h.bond.row()['id']
        def refuse(*a,**kw): raise error('test_refusal')
        if error is BodyEvidenceUnavailable: monkeypatch.setattr(h.realm.bodies,'claim',refuse)
        else: monkeypatch.setattr(h.realm.queue,'submit',refuse)
        task=asyncio.create_task(h.realm.run())
        try:
            await h.clock.advance(3)
            assert not task.done()
            assert h.bond.row(ident)['state']=='closed'
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError): await task
    run(go())


@pytest.mark.parametrize('blocked',['held','dungeon','battleground'])
def test_offer_requires_a_movable_world_body(live,blocked):
    async def go():
        h=live; await start(h)
        assert h.bond.available(101)
        if blocked=='held': h.realm.bodies.claim('test', [101],rank='raid',until=h.clock.now()+60)
        else: h.app.world.members[101]=replace(h.app.world.members[101],map=574 if blocked=='dungeon' else 489)
        assert not h.bond.available(101)
        await h.realm.close()
    run(go())


@pytest.mark.parametrize('cancel',['decline','disable','ownership'])
def test_sit_is_abandoned_at_actual_transport_after_cancellation(live,monkeypatch,cancel):
    async def go():
        h=live; await offer(h); await answer(h)
        row=h.bond.row(); ident=row['id']
        h.realm.bodies.claim('bond:'+ident,[101],rank='realm_day',until=row['expires'])
        with h.store.transaction(): h.store.conn.execute("UPDATE bond_invite SET state='seated' WHERE id=?",(ident,))
        h.bond.observed=(p.RealmVisitState(int(h.clock.now()*1000),'q','org_inn',21,1,True,True),h.clock.now(),h.realm.action_context())
        original=h.bond.ports.send
        async def delay(cmd):
            if type(cmd) is p.RealmEmote and cmd.emote=='sit':
                if cancel=='decline': await answer(h,'no')
                elif cancel=='disable': h.app.cfg=replace(h.app.cfg,romance=replace(h.app.cfg.romance,activities=False))
                else: h.realm.bodies.release('bond:'+ident)
            return await original(cmd)
        monkeypatch.setattr(h.bond.ports,'send',delay)
        with pytest.raises(DirectRefused): await h.bond.sit(h.bond.row(ident))
        assert not any(type(c) is p.RealmEmote and c.emote=='sit' for c in h.app.sent)
        command=h.bond.row(ident)['emote_id']
        state=h.store.conn.execute('SELECT state FROM realm_direct WHERE command_id=?',(command,)).fetchone()[0]
        assert state=='abandoned'
        await h.realm.close()
    run(go())


def test_child_cancelled_before_start_does_not_poison_reconnected_work(live):
    async def go():
        h=live; await start(h)
        ran=[]
        async def child(): ran.append(True)
        assert h.bond.spawn('probe',child)
        h.realm._invalidate()  # No await: child has not entered its try/finally.
        await h.app.connection(False); await h.app.connection(True)
        await h.app.event(Hello(int(h.clock.now()*1000),'test','1',21,1))
        assert h.bond.spawn('probe',child)
        await settle(h.realm)
        assert ran==[True] and not h.bond.work
        await h.realm.close()
    run(go())


def test_slow_probe_keeps_correlation_and_unsupported_stops_until_reconnect(live,monkeypatch):
    async def go():
        h=live; await start(h)
        release=asyncio.Event(); original=h.bond.ports.send
        async def delayed(cmd):
            if type(cmd) is p.RealmVisit: await release.wait()
            return await original(cmd)
        monkeypatch.setattr(h.bond.ports,'send',delayed)
        await h.bond.probe('org_inn',('offer',101,h.clock.local().date().isoformat()))
        await asyncio.sleep(0)
        query=h.bond.query
        await h.clock.advance(31,step=31)
        await h.bond.probe('org_inn',('offer',102,'ignored'))
        assert h.bond.query is query
        release.set(); await settle(h.realm)
        await observation(h)
        assert h.bond.row()['guid']==101
        assert not h.store.conn.execute("SELECT 1 FROM realm_receipt WHERE kind='realm_visit'").fetchone()
        h.bond.close(h.bond.row())
        await h.bond.probe('org_inn','test'); await settle(h.realm)
        cmd=h.bond.query[0]
        h.bond.probe_ack(cmd,Ack(0,cmd.id,False,'unknown type: realm_visit'))
        count=len(h.app.sent)
        await h.clock.advance(60,step=60)
        await h.bond.probe('org_inn','test'); await settle(h.realm)
        assert len(h.app.sent)==count
        await h.app.connection(False); await h.app.connection(True)
        await h.app.event(Hello(int(h.clock.now()*1000),'test','1',21,1))
        await h.bond.probe('org_inn','test'); await settle(h.realm)
        assert h.bond.query is not None
        await h.realm.close()
    run(go())


@pytest.mark.parametrize('state,checks',[('login',0),('offline',0),('grouped',0),('candidate',1),('held',1)])
def test_invitation_tick_limits_body_refresh(live,monkeypatch,state,checks):
    async def go():
        h=live; await start(h)
        if state=='login': h.app.engine._login_at=h.clock.now()
        elif state=='offline': h.app.world.human_online=False
        elif state=='grouped': monkeypatch.setattr(h.bond.ports,'human_grouped',lambda:True)
        calls=[]
        def movable(rank,guids):
            calls.append(tuple(guids))
            return () if state=='held' else guids
        monkeypatch.setattr(h.realm.bodies,'movable',movable)
        await h.bond.tick()
        assert len(calls)==checks
        if state=='held':
            await h.bond.tick()
            assert len(calls)==2 and calls[0]!=calls[1]
        await h.realm.close()
    run(go())


@pytest.mark.parametrize('reason',['realm_off','unknown_place'])
def test_temporary_probe_refusal_does_not_disable_session(live,reason):
    async def go():
        h=live; await start(h)
        await h.bond.probe('org_inn','test'); await settle(h.realm)
        cmd=h.bond.query[0]
        h.bond.probe_ack(cmd,Ack(0,cmd.id,False,reason))
        assert h.bond.unsupported_epoch is None
        count=len(h.app.sent)
        await h.clock.advance(11,step=11)
        await h.bond.probe('org_inn','test'); await settle(h.realm)
        assert len(h.app.sent)==count+1
        await h.realm.close()
    run(go())


def test_logout_resets_zone_and_live_map_evidence_updates_snapshot(live):
    from leeroy_mind.protocol import Chat,Death,Logout,Zone
    h=live; world=h.app.world; now=h.clock.now()
    world.apply(Logout(0,1,True),now)
    assert world.human_zone==0
    world.apply(Chat(0,'guild',101,'Invented',0,'fixture',574,1),now)
    assert world.members[101].map==574
    world.apply(Zone(0,101,1637,'Fixture'),now)
    assert world.members[101].map==574  # Zone carries no map.
    world.apply(Chat(0,'discord:general',101,'Invented',0,'fixture',1,2),now)
    assert world.members[101].map==574
    world.apply(Death(0,101,1,0),now)
    assert world.members[101].map==1


def test_default_invitations_vary_with_reproducible_member_seeds(live):
    phrases=[live.bond.invitation(g,'org_inn') for g in (101,102,103)]
    assert len(set(phrases))>1
    assert phrases==[live.bond.invitation(g,'org_inn') for g in (101,102,103)]
    assert all('the inn in Orgrimmar' in text for text in phrases)
