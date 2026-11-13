import json
from types import SimpleNamespace

import pytest

from leeroy_mind.discord import channels
from leeroy_mind.groups.config import GroupsConfig, load_groups_config
from leeroy_mind.groups import store as groups
from leeroy_mind.memory.privacy import derive_scope, restricted_channels, GROUP
from leeroy_mind.memory.recall import hidden_ok, scope_ok, whisper_ok
from leeroy_mind.protocol import Chat

ON = GroupsConfig(enabled=True)


def make_group(store,members=(101,102),now=10):
    return groups.create(store,human=1,members=members,title='Fixture friends',created_by=1,now=now,config=ON)


def test_group_channel_classification_and_sql_agree(store):
    channel='gdm:1'
    assert channels.is_group(channel) and channels.is_discord(channel)
    assert channels.is_restricted(channel) and not channels.is_private(channel) and not channels.is_public(channel)
    for sql,expected in [(channels.discord_sql(),True),(channels.group_sql(),True),
                         (channels.private_sql(),False),(channels.restricted_sql(),True)]:
        assert bool(store.conn.execute(f'SELECT {sql} FROM (SELECT ? AS channel)',(channel,)).fetchone()[0]) is expected


@pytest.mark.parametrize('table',['enabled=1','enabled=true\nmax_open=0','enabled=true\nmember_starts_per_week=4',
                                 'enabled=true\nunknown=true'])
def test_invalid_group_table_is_off(tmp_path,table):
    path=tmp_path/'mind.toml'
    path.write_text('[groups]\n'+table)
    assert load_groups_config(path) == GroupsConfig()


def test_membership_periods_do_not_change_historical_audiences(store):
    channel=make_group(store)
    prior=groups.readers(store.conn,channel,at=11)
    groups.add(store,channel,103,human=1,now=12)
    assert prior == {1,101,102} and groups.readers(store.conn,channel) == {1,101,102,103}
    assert groups.readers(store.conn,channel,at=11) == prior
    groups.remove(store,channel,103,human=1,now=13)
    groups.remove(store,channel,102,human=1,now=14)
    assert groups.get(store.conn,channel)['closed'] == 14 and not groups.readers(store.conn,channel)
    assert groups.readers(store.conn,channel,at=11,include_closed=True) == prior


MATRIX = [
    ('gdm:1',101,0,(1,101,102),True),
    ('dm:101',101,1,(),True),
    ('whisper',101,1,(),True),
    ('whisper',101,102,(),True),
    ('gdm:2',101,0,(1,101,102),True),
    ('gdm:2',101,0,(1,101,103),False),
    ('gdm:1',103,0,(1,101,102,103),False),
    ('guild',101,0,(),False),('party',101,0,(),False),('raid',101,0,(),False),
    ('officer',101,0,(),False),('voice:officers',101,0,(),False),('discord:general',101,0,(),False),
]


@pytest.mark.parametrize('target,speaker,peer,current,allowed',MATRIX)
def test_group_line_audience_matrix(target,speaker,peer,current,allowed):
    assert whisper_ok('gdm:1',101,0,speaker=speaker,turn_channel=target,to_guid=peer,
                      source_readers=(1,101,102),readers=current) is allowed


@pytest.mark.parametrize('nested',[False,True])
@pytest.mark.parametrize('target,speaker,peer,current,allowed',MATRIX)
def test_group_memory_audience_matrix(store,target,speaker,peer,current,allowed,nested):
    lid=store.insert_chat_line(Chat(0,'gdm:1',101,'Fixture',0,'A group observation.',0,0),
                              ts=10,session_id=None,origin='human',audience=(1,101,102))
    refs=[f'chat:{lid}']
    if nested:
        mid=store.conn.execute("INSERT INTO memory(owner,kind,text,t_event,t_created,t_accessed,importance,"
            "valence,source,evidence,actors,scope,scope_with) VALUES(101,'conversation','Fixture',10,10,10,5,0,"
            "'chat',?,'[1,101,102]','private',?)",(json.dumps(refs),GROUP)).lastrowid
        refs=[f'memory:{mid}']
    scope=derive_scope(store.conn,speaker,refs)
    memory=SimpleNamespace(owner=speaker,scope=scope.scope,scope_with=scope.scope_with,key=None)
    hidden=restricted_channels(store.conn,{0:refs})[0]
    assert (scope_ok(memory,turn_channel=target,to_guid=peer) and
        hidden_ok(hidden,target,speaker=speaker,to_guid=peer,readers=current)) is allowed


def test_pair_and_bond_memories_never_enter_group_turn():
    for scope,key in [('private',None),('open','bond:fixture')]:
        memory=SimpleNamespace(owner=101,scope=scope,scope_with=1,key=key)
        assert not scope_ok(memory,turn_channel='gdm:1',to_guid=1)


def test_group_history_without_its_reader_snapshot_is_not_shareable():
    assert not whisper_ok('gdm:1',101,0,speaker=101,turn_channel='gdm:1',to_guid=0,readers=(1,101,102))
    assert not hidden_ok({'gdm:1'},'gdm:1',speaker=101,readers=(1,101,102))


@pytest.mark.parametrize('target,speaker,peer,current,allowed',MATRIX)
def test_group_claim_audience_matrix(target,speaker,peer,current,allowed):
    from leeroy_mind.claims.visibility import claim_visible
    assert claim_visible(source_channel='gdm:1',source_from=101,source_to=0,source_readers=(1,101,102),
        draft_channel=target,speaker=speaker,to_guid=peer,officer=True,cross_medium=True,draft_readers=current) is allowed


@pytest.mark.parametrize('target,speaker,peer,current,allowed',MATRIX)
def test_group_awareness_audience_matrix(target,speaker,peer,current,allowed):
    from leeroy_mind.awareness.card import _visible
    ctx=SimpleNamespace(channel=target,to_guid=peer,readers=current)
    assert _visible('gdm:1',101,0,speaker,True,ctx,(1,101,102)) is allowed


@pytest.mark.parametrize('target,speaker,peer,current,allowed',MATRIX)
def test_group_guard_audience_matrix(target,speaker,peer,current,allowed):
    from leeroy_mind.guards.review import _line,_visible
    from leeroy_mind.guards.types import Draft
    line=_line(dict(id=1,ts=10,channel='gdm:1',from_guid=101,to_guid=0,text='A group observation.',
        audience='[1,101,102]',origin='human'),{})
    category,room = (('group',target[4:]) if channels.is_group(target) else
                     ('dm',target[3:]) if target.startswith('dm:') else (target,''))
    assert _visible(line,Draft(speaker,category,'Fixture',to_guid=peer,room=room,readers=current),True) is allowed


def test_deep_group_memory_is_never_made_public(store):
    lid=store.insert_chat_line(Chat(0,'gdm:1',101,'Fixture',0,'PRIVATE_GROUP_FIXTURE',0,0),
        ts=10,session_id=None,origin='human',audience=(1,101,102))
    ref=f'chat:{lid}'
    for _ in range(24):
        mid=store.conn.execute("INSERT INTO memory(owner,kind,text,t_event,t_created,t_accessed,importance,"
            "valence,source,evidence,actors) VALUES(101,'conversation','Fixture',10,10,10,5,0,'chat',?,'[]')",
            (json.dumps([ref]),)).lastrowid
        ref=f'memory:{mid}'
    hidden=restricted_channels(store.conn,{0:[ref]})[0]
    assert not hidden_ok(hidden,'guild',speaker=101)
    assert hidden_ok(hidden,'dm:101',speaker=101,to_guid=1)


def test_readonly_console_groups_and_off_switch(store,clock):
    from leeroy_mind.console.discordview import discord_view
    store.set_meta('human_guid','1')
    channel=make_group(store)
    assert 'groups' not in discord_view(store.conn,owner_now=clock.local(),trial_rank=4)
    view=discord_view(store.conn,owner_now=clock.local(),trial_rank=4,groups=True,channel=channel)
    assert view['groups'][0]['members']==[101,102] and view['groups'][0]['closed'] is None
    groups.remove(store,channel,1,human=1,now=20)
    assert discord_view(store.conn,owner_now=clock.local(),trial_rank=4,groups=True)['groups'][0]['closed']==20
