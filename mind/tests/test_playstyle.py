import asyncio
import json
import logging
from dataclasses import replace
from types import SimpleNamespace

import pytest

from conftest import make_persona, run, seed_test_cast
from leeroy_mind.playstyle import style
from leeroy_mind.playstyle.service import KEY, PlayStyles, attach, enabled
from leeroy_mind.protocol import Boss, Group, Hello, Login
from leeroy_mind.personas.io import PersonaCache
from leeroy_mind.talk.world import WorldView


@pytest.mark.parametrize('guid',[101,102,103,104,105])
@pytest.mark.parametrize('role',['tank','healer','melee','ranged'])
@pytest.mark.parametrize('score',[1,6,7,10])
def test_map_is_deterministic_and_bounded(guid,role,score):
    p = make_persona(guid,'Arkon')
    p = replace(p,play=replace(p.play,raid_role=role,skill='solid'),
                big_five=replace(p.big_five,neuroticism=replace(p.big_five.neuroticism,score=score),
                                 conscientiousness=replace(p.big_five.conscientiousness,score=score)))
    got = style(p)
    assert style(p)==got
    assert (2<=got.wait<=6) if role in ('melee','ranged') else got.wait==0
    assert got.potions is (score>=6)
    assert len(got.param)==3 and got.param[1]==':'


def app_for(store,tmp_path):
    seed_test_cast(store)
    world = WorldView(is_officer=lambda g:False)
    world.members = store.cast()
    world.online = set(world.members)
    app = SimpleNamespace(store=store,data_dir=tmp_path,human_guid=1,world=world,
                          personas=PersonaCache(store),stopping=False,is_backlog=lambda ev:False)
    return app


@pytest.mark.parametrize('text',['','[playstyle]\nenabled=false','[playstyle]\nenabled="true"',
                               '[playstyle]\nenabled=true\nextra=1','invalid ['])
def test_off_or_invalid_is_inert(store,tmp_path,text):
    (tmp_path/'mind.toml').write_text(text)
    app = app_for(store,tmp_path)
    before = store.conn.total_changes
    assert not enabled(tmp_path/'mind.toml')
    assert attach(app) is None
    assert store.conn.total_changes==before


def test_observed_encounters_instances_raids_and_disconnection_gate_additions(store,tmp_path):
    async def scenario():
        (tmp_path/'mind.toml').write_text('[playstyle]\nenabled=true')
        app = app_for(store,tmp_path)
        service = PlayStyles(app)
        await service.connection(True)
        await service.event(Hello(0,'fake','1',21,1,('playstyle',)))
        assert service.safe(101)
        await service.event(Boss(0,0,1,4,'Invented world boss','engaged',(101,)))
        assert not service.safe(101) and service.safe(102)
        await service.event(Boss(0,0,1,4,'Invented world boss','killed',(101,)))
        assert service.safe(101)
        await service.event(Boss(0,0,1,4,'Invented world boss','engaged',(101,)))
        await service.event(Hello(0,'fake','1',21,1,('playstyle',)))
        assert not service.fights and service.safe(101)
        app.world.members[101] = replace(app.world.members[101],map=631)
        assert not service.safe(101)
        app.world.members[101] = replace(app.world.members[101],map=574)
        assert not service.safe(101)
        app.world.members[101] = replace(app.world.members[101],map=0)
        app.world.group = Group(0,1,True,(1,101))
        assert not service.safe(101)
        app.world.group = None
        await service.connection(False)
        assert not service.safe(101)
    run(scenario())


def test_unknown_receipt_is_durable_before_dispatch_and_bad_receipt_fails_closed(store,tmp_path):
    async def scenario():
        (tmp_path/'mind.toml').write_text('[playstyle]\nenabled=true')
        app = app_for(store,tmp_path)
        service = PlayStyles(app)
        service.supported = service.connected = True
        async def lost(cmd,**kwargs):
            assert json.loads(store.get_meta(KEY))==[101]
            raise ConnectionError('private transport text')
        app.bridge = SimpleNamespace(send=lost)
        assert not await service.command([101],False)
        assert service.owned=={101}
        recovered = PlayStyles(app)
        await recovered.event(Hello(0,'fake','1',21,1,('playstyle',)))
        assert recovered.pending=={101:True}
        store.set_meta(KEY,'"private canary"')
        broken = PlayStyles(app)
        broken.supported = broken.connected = True
        assert not broken.safe(101)
    run(scenario())


def test_login_and_group_queue_only_once_and_backlog_is_ignored(store,tmp_path):
    async def scenario():
        app = app_for(store,tmp_path)
        service = PlayStyles(app)
        await service.event(Login(0,101,False))
        assert service.pending=={101:False}
        service.pending.clear()
        await service.event(Group(0,1,False,(1,101,102)))
        assert service.pending=={101:False,102:False}
        service.pending.clear()
        await service.event(Group(0,1,False,(1,101,102)))
        assert not service.pending
        app.is_backlog = lambda ev:True
        await service.event(Login(0,103,False))
        assert not service.pending
        for g in range(1000,3000): service.queue(g)
        assert len(service.pending)==512
    run(scenario())


@pytest.mark.parametrize('change',[{'kind':'summon'}, {'state':'failed'}, {'reason':'busy'},
                                  {'skipped':(101,)}, {'guids':(102,)}])
def test_only_exact_successful_cleanup_receipt_releases_ownership(store,tmp_path,change):
    async def scenario():
        app = app_for(store,tmp_path)
        store.set_meta(KEY,'[101]')
        service = PlayStyles(app)
        service.connected = service.supported = True
        async def send(cmd,**kwargs):
            row = dict(id=cmd.id,kind='bot_do',state='done',reason='',skipped=(),guids=(101,))
            service.jobs.feed(SimpleNamespace(**(row|change)))
            return SimpleNamespace(ok=True)
        app.bridge = SimpleNamespace(send=send)
        assert not await service.command([101],True)
        assert json.loads(store.get_meta(KEY))==[101]
    run(scenario())


def test_mind_switch_off_enqueues_undo_without_new_additions(store,tmp_path):
    async def scenario():
        app = app_for(store,tmp_path)
        store.set_meta(KEY,'[101]')
        service = PlayStyles(app)
        service.on = service.connected = service.supported = True
        (tmp_path/'mind.toml').write_text('[playstyle]\nenabled=false')
        await service.event(Login(0,101,False))
        assert service.pending=={101:True} and not service.safe(101)
        async def send(cmd,**kwargs):
            assert cmd.action=='restore' and cmd.param=='playstyle'
            service.jobs.feed(SimpleNamespace(id=cmd.id,kind='bot_do',state='done',reason='',skipped=(),guids=(101,)))
            return SimpleNamespace(ok=True)
        app.bridge = SimpleNamespace(send=send)
        assert await service.command([101],True)
        assert store.get_meta(KEY) is None
    run(scenario())


def test_real_mind_fake_bridge_applies_and_shutdown_restores(tmp_path,clock,caplog):
    from leeroy_mind.app import MindApp
    from leeroy_mind.config import load_config
    from leeroy_mind.testing.fake_bridge import FakeBridge, CORE_COMMAND_TYPES
    from leeroy_mind.testing.fake_llm import FakeLLM
    from test_app import CAST, hello, seed_db, toml

    async def scenario():
        tasks = set()
        applied = set()
        def ack(cmd):
            if cmd['type']=='bot_do':
                if cmd['action']=='playstyle': applied.update(cmd['guids'])
                else: applied.difference_update(cmd['guids'])
                job = dict(type='job',id=cmd['id'],kind='bot_do',state='done',reason='',stage='',
                           guids=cmd['guids'],skipped=[],kills=0,wipes=0,instance_id=0,guid=0,level=0,avg_ilvl=0)
                task = asyncio.create_task(bridge.push(job))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
            return True,''
        bridge = FakeBridge([{**hello(0),'features':['playstyle']},CAST],ack=ack,
                            known_types=CORE_COMMAND_TYPES|{'bot_do'})
        port = await bridge.start()
        (tmp_path/'mind.toml').write_text(toml(port,chattiness=0)+'\n[playstyle]\nenabled=true\n')
        seed_db(tmp_path/'mind.db',clock)
        cfg = load_config(tmp_path/'mind.toml')
        llm = FakeLLM(models={cfg.models.live,cfg.models.reflection,cfg.models.embedding})
        app = MindApp(cfg,tmp_path,llm=llm,clock=clock)
        runner = asyncio.create_task(app.run())
        try:
            async with asyncio.timeout(5):
                while len(applied)!=5: await asyncio.sleep(.01)
            assert app.playstyle.owned=={101,102,103,104,105}
            await app.stop()
            assert not applied and app.playstyle.owned==set()
            assert not runner.done() or runner.exception() is None
            assert all(c['type'] in CORE_COMMAND_TYPES|{'bot_do'} for c in bridge.commands)
            assert any(c['action']=='restore' for c in bridge.commands if c['type']=='bot_do')
        finally:
            await app.stop()
            await runner
            if tasks: await asyncio.gather(*tasks)
            await bridge.stop()
        assert 'playstyle_result' in caplog.text
        assert 'private' not in caplog.text
    caplog.set_level(logging.INFO,'leeroy_mind')
    run(scenario())
