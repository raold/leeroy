import asyncio
import sqlite3
from dataclasses import replace
from datetime import datetime,timezone
from types import SimpleNamespace
import pytest

from console.test_console_discord import build,served,post,get,FakeLink,cast,data
from console.rig import http,FakeMind,running
from leeroy_mind.console.control import Controls
from leeroy_mind.console.discordview import discord_view
from leeroy_mind.groups.config import GroupsConfig


@pytest.mark.parametrize('mode',['off','key','origin','no-control','mind-down','ok'])
def test_group_console_control_gates(data,cast,mode):
    link=FakeLink({'ok':True,'channel':'gdm:7'})
    kw={'link':link}
    if mode=='no-control':
        kw['controls']=Controls(enabled=False,reason='Disabled',mind=running(data))
    if mode=='mind-down':
        kw['mind']=FakeMind(data)
    async def go():
        async with served(build(data,**kw)) as app:
            app.groups=GroupsConfig(enabled=mode!='off')
            args={'action':'group_create','members':[101,102]}
            response=await http(app,'POST','/api/discord',body=args,key=mode!='key',origin=mode!='origin')
            assert response.status==({'key':403,'origin':403,'no-control':403,'mind-down':409,'off':409}.get(mode,200))
            if mode=='ok':
                assert response.json()['channel']=='gdm:7'
            elif mode in {'key','origin'}:
                assert b'Forbidden' in response.body
            else:
                assert response.json()['error']
    asyncio.run(go())
    assert bool(link.calls)==(mode=='ok')


def test_group_view_older_database_is_readonly(tmp_path):
    path=tmp_path/'older.db'
    conn=sqlite3.connect(path); conn.row_factory=sqlite3.Row
    conn.execute('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT)'); conn.commit()
    before=path.read_bytes()
    view=discord_view(conn,owner_now=datetime.now(timezone.utc),trial_rank=4,groups=True,channel='gdm:1')
    assert view['groups']==[] and view['messages']==[] and 'discord_group' in view['missing']
    conn.close(); assert path.read_bytes()==before
