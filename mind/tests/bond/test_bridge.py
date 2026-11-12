"""Real MindApp, talk recording, BridgeClient sockets and Realm journals; all external work fake."""
from dataclasses import replace
import asyncio
from contextlib import suppress

from conftest import run
from test_app import running, hello, CAST, chat, SCHEMA_MARK
from leeroy_mind.config import load_config
from leeroy_mind.personas.io import save_persona
from leeroy_mind.guild.raid_night import State
from leeroy_mind.realm.places import Places, DIST
from leeroy_mind.realm import protocol as p
from leeroy_mind.testing.fake_bridge import CORE_COMMAND_TYPES


def test_fake_bridge_accept_move_sit_and_private_conversation(tmp_path,clock,caplog):
    def prepare(app):
        with (tmp_path/'mind.toml').open('a',encoding='utf-8') as f:
            f.write('\n[realm]\nenabled=true\n[romance]\nactivities=true\n')
        app.cfg=load_config(tmp_path/'mind.toml')
        app.engine.cfg=app.cfg
        app.guild.cfg=replace(app.guild.cfg,worldserver_conf=tmp_path/'absent.conf')
        app.realm.part('speech')._places=Places.load(DIST)
        old=app.personas.get(101)
        person=replace(old,identity=replace(old.identity,player_gender='female'),
                       romance=replace(old.romance,stance='fond'))
        save_persona(app.store,person,source='seed',ts=clock.now(),race='Orc',class_name='Warrior')
        app.personas.invalidate(101)

    async def scenario():
        async with running(tmp_path,clock,events=[hello(1),CAST],prepare=prepare,
            script=lambda model,messages: ('{"memories":[],"overheard":null}' if SCHEMA_MARK in messages[-1]['content']
                                           else 'A quiet corner was a good choice.'),
            bridge_kw={'known_types':CORE_COMMAND_TYPES|set(p.COMMANDS)|{'bot_do'}}) as h:
            await h.until(lambda:h.app.realm.action_context().presence_known and 101 in h.app.world.online)
            bond=h.app.realm.part('bond')
            h.app.guild.raid_night.state=State.IDLE
            h.app.realm._raid_state='IDLE'
            h.app.world.human_zone=1637
            h.app.engine._login_at=clock.now()-1800
            assert bond.enabled(), h.app.cfg.romance
            assert bond.available(101), (h.app.world.human_online,h.app.world.group,
                bond.ports.raid_state(),h.app.personas.get(101).romance.stance,h.app.personas.get(101).identity.player_gender)
            assert h.app.realm.part('speech').places.by_kind('inn',zone=1637)
            await bond.tick()
            def visits(): return [c for c in h.bridge.commands if c['type']=='realm_visit']
            async def reply(query,near):
                await h.bridge.push(dict(type='realm_visit_state',ts=int(clock.now()*1000),id=query['id'],
                    place=query['place'],guild_id=h.app.realm.action_context().guild,human_guid=1,ready=True,near=near))
            proximity={'near':False}
            async def native_observer():
                index=0
                while True:
                    await h.wait(lambda:len(h.bridge.commands)>index)
                    cmd=h.bridge.commands[index]; index+=1
                    if cmd['type']=='realm_visit': await reply(cmd,proximity['near'])
                    elif cmd['type']=='rearm':
                        await h.bridge.push(dict(type='realm_job',ts=int(clock.now()*1000),id=cmd['id'],
                            kind='rearm',state='done',reason='',guid=0,value=len(cmd['guids'])))
                    elif cmd['type']=='bot_do':
                        await h.bridge.push(dict(type='job',ts=int(clock.now()*1000),id=cmd['id'],kind='bot_do',
                            state='done',reason='',stage='',guids=cmd['guids'],skipped=[],kills=0,wipes=0,
                            instance_id=0,guid=0,level=0,avg_ilvl=0))
            observer=asyncio.create_task(native_observer())
            try:
                await exercise(h,bond,visits,proximity)
            finally:
                observer.cancel()
                with suppress(asyncio.CancelledError): await observer

    async def exercise(h,bond,visits,proximity):
        await h.until(lambda:bool(visits()))
        await h.until(lambda:bond.row() is not None and bond.row()['state']=='offered')
        invitations=[c for c in h.says() if 'Say yes or meet me there' in c.get('text','')]
        assert len(invitations)==1 and invitations[0]['channel']=='whisper' and invitations[0]['to_guid']==1
        await h.bridge.push(chat('yes',channel='whisper',to_guid=101,ts=int(clock.now()*1000)))
        await h.until(lambda:any(c['type']=='realm_move' for c in h.bridge.commands))
        move=next(c for c in h.bridge.commands if c['type']=='realm_move')
        assert move['social'] is True and move['guid']==101
        job=dict(type='realm_job',ts=int(clock.now()*1000),id=move['id'],kind='realm_move',state='done',reason='',guid=101,value=0)
        await h.bridge.push(job); await h.bridge.push(job)
        proximity['near']=True
        await h.until(lambda:any(c['type']=='realm_emote' for c in h.bridge.commands))
        await h.until(lambda:any('quiet corner' in c.get('text','') for c in h.says()))
        assert len([c for c in h.bridge.commands if c['type']=='realm_move'])==1
        assert len([c for c in h.bridge.commands if c['type']=='realm_emote'])==1
        assert all(c['channel']=='whisper' and c['to_guid']==1 for c in h.says() if 'quiet corner' in c.get('text',''))
        assert h.count('realm_direct',"WHERE kind='realm_emote' AND state='acked'")==1
    run(scenario())
    assert 'Fancy sitting' not in caplog.text and 'quiet corner' not in caplog.text
