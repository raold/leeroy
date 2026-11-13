"""Real mind, control and console sockets; only fake models, bridge and rendered files."""
import asyncio
from dataclasses import replace
from datetime import datetime
import logging
import os
import re
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from conftest import run
from console.rig import FakeMind, http, make_app
from console.test_console_discord import served
from console.test_discord_upload import upload
from discord.test_talk_photo import photo_tasks
from photos.test_images import jpeg
from photos.test_send import setup, stock, ON
from photos.test_pipeline_photo import finish
from leeroy_mind.console.mindlink import MindLink
from leeroy_mind.console.netinfo import LISTEN
from leeroy_mind.photos.send import photo_seeds, channel_count
from leeroy_mind.selfies import snaps
from leeroy_mind.selfies.scene_choices import compose_choices
from selfies.test_scene_choices import PICKS
from leeroy_mind.server.engine import Conversation
from leeroy_mind.storage.rows import pack_vec
from leeroy_mind.testing.fake_bridge import FakeBridge


def inventory(root):
    return {str(p.relative_to(root)):p.read_bytes() for name in ('selfies','discord')
            for p in (root/name).rglob('*') if p.is_file()}


def test_photos_end_to_end_keep_files_and_never_use_game_transport(tmp_path,caplog):
    caplog.set_level(logging.INFO)
    async def go():
        bridge=FakeBridge(events=[{'type':'hello','server':'mod-leeroy','version':'0.1.0',
                                  'guild_id':21,'human_guid':0}],cast=[])
        port=await bridge.start()
        config=ON.replace('send=true','send=true\nenabled=true').replace(
            '[server]\nenabled=true','[server]\nenabled=true\nconversations_per_hour=0\ndms_per_day=0\nreactions=false')
        app=setup(tmp_path,config+f'\n[bridge]\nport={port}\n[guards]\nenabled=true\n')
        bridge._cast={'type':'cast','members':[
            {'guid':g,'name':m.name,'race':m.race,'class':m.class_,'gender':m.gender,
             'level':m.level,'rank':m.rank,'online':False,'map':0,'zone':0,'spec_tab':-1}
            for g,m in app.world.discord_members().items()]}
        app.clock._start=datetime(2026,10,29,20,tzinfo=ZoneInfo('America/New_York'))
        app.clock._t=app.clock._start.timestamp()
        original=app.personas.get
        people={g:replace(p,identity=replace(p.identity,region='US'),real_life=replace(p.real_life,night_owl=False))
                for g in app.world.discord_members() if (p:=original(g)) is not None}
        app.personas.get=lambda guid:people.get(guid)
        app.llm.set_models([app.cfg.models.live,app.cfg.models.embedding])
        vocabulary={}
        async def embed(texts,priority):
            result=[]
            for text in texts:
                vocabulary.setdefault(text,len(vocabulary)+1)
                vector=[0.0]*512
                vector[0],vector[vocabulary[text]]=.7,(1-.7**2)**.5
                result.append(pack_vec(vector))
            return result
        app.engine.recall._embedder.embed=embed
        def script(model,messages):
            if any('images' in message for message in messages):return 'A dog naps on a blue rug.'
            if 'Reply with only a JSON object matching this schema' in messages[-1]['content']:
                return '{"memories":[],"overheard":null}'
            text=str(messages)
            if "You're posting a photo of yourself" in text:
                return ('the soup experiment survived another evening in my kitchen' if 'stirring a pot' in text
                        else 'the dog has appointed himself our evening tour guide')
            if '#food' in text:return 'that soup looks like proper comfort after a long day'
            return 'that dog has claimed the blue rug as a permanent kingdom'
        app.llm._script=script
        task=asyncio.create_task(app.run())
        try:
            await asyncio.wait_for(app.control.listening.wait(),5)
            await app.server.lease.tick(app.clock.now())
            await app.server.lease.handback()
            sockets=lambda:[SimpleNamespace(state=LISTEN,local='127.0.0.1',local_port=app.control.port,pid=os.getpid())]
            console=make_app(tmp_path,mind=FakeMind(tmp_path,found=[SimpleNamespace(pid=os.getpid())],held=True),
                             link=MindLink(tmp_path,tcp=sockets),tcp=lambda:[],clock=app.clock.now)
            async with served(console):
                token=(await upload(console,jpeg())).json()['image']
                staged=inventory(tmp_path)
                name=app.world.discord_members()[101].name
                posted=await http(console,'POST','/api/discord',body={
                    'action':'post','channel':'pets','text':f'@{name} look at the dog','image':token},origin=True)
                assert posted.status==200,posted.body
                mid=posted.json()['id']
                await photo_tasks(app)
                for _ in range(100):
                    if app.store.conn.execute("SELECT 1 FROM memory WHERE key=?",(f'discord_photo:{mid}',)).fetchone():break
                    await app.clock.advance(2,2)
                assert app.store.conn.execute("SELECT 1 FROM discord_message WHERE reply_to=? AND text LIKE '%dog%'",(mid,)).fetchone()
                assert app.store.conn.execute("SELECT 1 FROM memory WHERE key=?",(f'discord_photo:{mid}',)).fetchone()
                assert (await http(console,'GET',f'/api/discord/image?id={mid}&size=lg')).body==jpeg()
                assert all(data in inventory(tmp_path).values() for data in staged.values())
                await app.clock.advance(65,65)
                food=stock(app,103,audience='anyone',scene=compose_choices({**PICKS,'setting':'kitchen',
                    'activity':'cooking','prop':'none','outfit':'casual','when':'evening'}))
                dm=stock(app,102)
                before=inventory(tmp_path)
                seed=next(s for s in photo_seeds(app.store.conn,now=app.clock.now(),local=app.clock.local(),dial=3) if s.key==f'photo:{food}')
                conv=Conversation('photo-rehearsal','food',seed,[103,101],app.clock.now(),2)
                app.server.engine._live[conv.id]=conv
                conv.task=asyncio.create_task(app.server.engine._conversation(conv))
                await finish(app,conv.task)
                public=app.store.conn.execute('SELECT m.id,m.channel,m.author FROM discord_message m JOIN discord_attachment a ON a.message=m.id WHERE a.snap=?',(food,)).fetchone()
                assert public and tuple(public)[1:]==('food',103)
                assert len(conv.messages)==2
                assert '[photo:' in str(app.llm.calls)
                app.server.dms.config=replace(app.server.dms.config,dms_per_day=3)
                app.server.dms._day=app.clock.local().date().isoformat()
                app.server.dms._next_at=app.clock.now()-1
                app.server.dms._planner.start=lambda now,candidates,**kw:102 if 102 in candidates else None
                await finish(app,asyncio.create_task(app.server.dms.tick(app.clock.now())))
                private=app.store.conn.execute('SELECT m.id,m.channel,m.author FROM discord_message m JOIN discord_attachment a ON a.message=m.id WHERE a.snap=?',(dm,)).fetchone()
                assert private and tuple(private)[1:]==('dm:102',102)
                assert channel_count(app.store.conn,app.clock.local().date().isoformat())==1
                assert snaps.sent_count(app.store,app.clock.now()-86400,to_him=True)==1
                for message in (public[0],private[0]):
                    assert (await http(console,'GET',f'/api/discord/image?id={message}&size=sm')).status==200
                    assert (await http(console,'GET',f'/api/discord/image?id={message}&size=lg')).status==200
                assert all(inventory(tmp_path).get(path)==data for path,data in before.items())
                assert not [c for c in bridge.commands if c['type']=='say']
        finally:
            await app.stop()
            await asyncio.wait_for(task,5)
            await bridge.stop()
        assert not (tmp_path/'control.json').exists()
        measured=re.findall(r'discord_look: ms=([\d.]+) ok=true',caplog.text)
        assert len(measured)==1,caplog.text
        print('FakeLLM photo look ms='+measured[0])
    run(go())
