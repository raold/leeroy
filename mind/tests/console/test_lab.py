"""Lab read paths and real control dispatch, against invented scratch data only."""
import asyncio
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from console.rig import http, make_app, running
from console.test_console_library import FakeLink
from leeroy_mind import endpoint
from leeroy_mind.console import feed, labview, discordview
from leeroy_mind.console.control import Controls
from leeroy_mind.discord import store as discord
from leeroy_mind.learn import arms, bandit, control, lessons, weights
from leeroy_mind.learn.config import LearnCfg
from leeroy_mind.protocol import Chat
from leeroy_mind.signals import store as signals


def data_path(store):
    return Path(store.conn.execute('PRAGMA database_list').fetchone()[2]).parent


def line(store, now, channel='guild', *, guid=101, to=0, session=None):
    return store.insert_chat_line(Chat(int(now*1000), channel, guid, 'Arkon', to,
        'Invented private line for the Lab.', 0, 10), session_id=session, origin='mind', ts=now)


def lesson(store, now, status='proposed', text='She keeps her guild replies brief.'):
    with store.transaction():
        return store.conn.execute('INSERT INTO talk_lesson(guid,scope,text,status,expires,origin_night) '
            'VALUES (101,?,?,?,?,?)', ('guild', text, status, now+86400, 'test')).lastrowid


def test_absent_and_older_database_reads_never_create_or_write(tmp_path):
    result=labview.read(tmp_path,now=1000)
    assert not (tmp_path/'mind.db').exists()
    assert not result['rewards']['available'] and result['report'] is None
    conn=sqlite3.connect(tmp_path/'mind.db')
    conn.execute('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT)')
    conn.commit(); conn.close()
    before=(tmp_path/'mind.db').read_bytes()
    assert labview.read(tmp_path,now=1000)['weights']['current']['values']==dict(weights.DEFAULTS)
    assert (tmp_path/'mind.db').read_bytes()==before


def test_absent_database_fallback_closes_its_connection(tmp_path,monkeypatch):
    connect=sqlite3.connect; opened=[]
    def track(*args,**kwargs):
        conn=connect(*args,**kwargs); opened.append(conn); return conn
    monkeypatch.setattr(labview.sqlite3,'connect',track)
    assert not labview.read(tmp_path,now=1000)['rewards']['available']
    assert len(opened)==1
    with pytest.raises(sqlite3.ProgrammingError,match='closed'):
        opened[0].execute('SELECT 1')


def test_feedback_snapshot_handles_more_than_sqlite_variable_limit(store,clock):
    lid=line(store,clock.now())
    with store.transaction():
        signals.feedback(store.conn,{'line_id':lid,'value':1,'reasons':['funny']},1,clock.now())
    previous=store.conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER,32)
    try:
        assert labview.feedback(store.conn,range(lid,lid+2000))=={
            lid:{'value':1,'reasons':['funny'],'revision':1}}
    finally: store.conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER,previous)


def test_rewards_numbers_and_on_demand_owner_visible_text(store,clock,caplog):
    now=clock.now(); store.set_meta('human_guid','1')
    session=store.start_session(now-300)
    lid=line(store,now-200,session=session)
    private=line(store,now-200,channel='whisper',to=102)
    with store.transaction():
        version=weights.ensure(store.conn,now=now-250).version
        store.conn.execute('INSERT INTO talk_reward VALUES (?,?,?,1,0,?,?,?)', (lid,101,.6,'{}',version,now))
        signals.feedback(store.conn,{'line_id':lid,'value':1,'reasons':['funny']},1,now)
    result=labview.read(data_path(store),now=now)
    assert 'Invented private line' not in json.dumps(result)
    assert result['rewards']['days'][0]['average']==.6
    member=result['rewards']['members'][0]
    assert (member['average'],member['up'],member['down'])==(.6,1,0)
    assert member['top'][0]['id']==lid
    assert labview.read(data_path(store),now=now,line=lid)['line']['text'].startswith('Invented')
    assert labview.read(data_path(store),now=now,line=private)=={'line':None}
    assert 'Invented private line' not in caplog.text


def test_feed_history_and_discord_use_chat_ids_and_stored_votes(store,clock):
    now=clock.now(); store.set_meta('human_guid','1'); session=store.start_session(now-10000)
    lid=line(store,now-9000,session=session)
    dmline=line(store,now-20,channel='dm:101')
    mid=discord.post(store,channel='dm:101',author=101,origin='mind',text='Synthetic DM.',now=now-20)
    with store.transaction():
        store.set_meta(f'discord:line:{mid}',str(dmline))
        signals.feedback(store.conn,{'line_id':lid,'value':-1,'reasons':['repetitive']},1,now)
        signals.feedback(store.conn,{'line_id':dmline,'value':1,'reasons':[]},1,now)
    old=feed.history(store.conn,'guild',lid+1,now=now)['lines'][0]
    assert old['feedback_id']==lid and old['feedback']=={'value':-1,'reasons':['repetitive'],'revision':1}
    message=discordview.window(store.conn,'dm:101')[0]
    assert message['feedback_id']==dmline and message['feedback']['value']==1
    assert labview.line_view(store.conn,dmline,now=now)['line']['text']=='Synthetic DM.'
    with store.transaction():
        store.conn.execute('UPDATE discord_message SET deleted=? WHERE id=?',(now,mid))
    assert discordview.window(store.conn,'dm:101')[0]['feedback_id'] is None
    assert labview.line_view(store.conn,dmline,now=now)=={'line':None}


def test_historical_discord_fallback_is_unique_and_never_crosses_channels(store,clock):
    now=clock.now(); store.set_meta('human_guid','1')
    lid=line(store,now,channel='dm:101',to=1)
    text=store.conn.execute('SELECT text FROM chat_line WHERE id=?',(lid,)).fetchone()[0]
    mid=discord.post(store,channel='dm:101',author=101,origin='mind',text=text,now=now)
    before=store.conn.total_changes
    assert discordview.window(store.conn,'dm:101')[0]['feedback_id']==lid
    assert labview.line_view(store.conn,lid,now=now)['line']['text']==text
    assert store.conn.total_changes==before
    # Another channel with identical words/time cannot borrow this private receipt.
    other=discord.post(store,channel='general',author=101,origin='mind',text=text,now=now)
    assert discordview.window(store.conn,'general')[0]['feedback_id'] is None
    line(store,now,channel='dm:101',to=1)
    assert discordview.window(store.conn,'dm:101')[0]['feedback_id'] is None
    assert labview.line_view(store.conn,lid,now=now)=={'line':None}


def test_every_members_lessons_and_later_pages_are_reachable(store,clock):
    now=clock.now()
    with store.transaction():
        for guid in range(101,193):
            for n in range(5):
                store.conn.execute("INSERT INTO talk_lesson(guid,scope,text,status,expires,origin_night) VALUES (?,'guild',?,'active',?,'test')",
                                   (guid,f'She keeps reply {n} brief.',now+86400))
        for n in range(55): lesson(store,now,text=f'She considers lesson {n}.')
    first=labview.lab_view(store.conn,now=now)['lessons']
    assert len(first['members'])==92 and len(first['items'])==50 and first['more']
    later=labview.lab_view(store.conn,now=now,member=101,offset=50)['lessons']
    assert len(later['items'])==10 and not later['more']
    last=labview.lab_view(store.conn,now=now,member=192)['lessons']
    assert len(last['items'])==5 and all(x['guid']==192 for x in last['items'])


def test_frozen_fingerprint_and_capture_do_not_write_but_feedback_still_does(store,clock):
    from leeroy_mind.fingerprint import update
    from leeroy_mind.learn import capture, recipe
    control.change(store,{'action':'freeze','frozen':True},now=clock.now())
    before=store.conn.total_changes
    update(store.conn,101,now=clock.now())
    capture.delivered(store.conn,1,SimpleNamespace())
    capture.vectors(store.conn,[],{},'fake')
    assert not recipe.record(store.conn,1,None,None,None,None)
    assert store.conn.total_changes==before
    lid=line(store,clock.now())
    with store.transaction():
        first=signals.feedback(store.conn,{'line_id':lid,'value':1,'reasons':[]},1,clock.now())
        second=signals.feedback(store.conn,{'line_id':lid,'value':-1,'reasons':['weird']},1,clock.now())
    assert second['revision']==first['revision']+1  # the clock need not advance


@pytest.mark.parametrize('channel',['general','dm:101'])
def test_new_discord_receipts_exist_with_learning_off(store,tmp_path,channel):
    from engine_rig import engine_clock,make_rig
    from discord.test_discord_transport import discord_turn
    clock=engine_clock(); rig=make_rig(store,clock,tmp_path,reply='A synthetic member reply.')
    try:
        rig.engine.learn_enabled=False; rig.engine.learn_capture_preferences=False
        assert rig.speak_turn(discord_turn(rig,101,channel,to=1 if channel.startswith('dm:') else 0))
        message=discord.messages(store.conn,channel)[0]
        receipt=store.get_meta(f'discord:line:{message["id"]}')
        assert receipt is not None
        assert discordview.window(store.conn,channel)[0]['feedback_id']==int(receipt)
        assert not store.conn.execute('SELECT 1 FROM talk_recipe').fetchone()
    finally: rig.close()


def test_each_learning_action_and_weight_version_conflict(store,clock):
    now=clock.now()
    def change(**args): return control.change(store,args,now=now)
    assert change(action='freeze',frozen=True)['ok']
    assert control.effective(store.conn,LearnCfg(enabled=False)).freeze
    assert not control.effective(store.conn,LearnCfg(enabled=False)).enabled
    change(action='freeze',frozen=False)
    change(action='family',family='humour',frozen=True)
    assert control.effective(store.conn).frozen_families==('humour',)
    change(action='family',family='humour',frozen=False)
    assert not control.effective(store.conn).frozen_families
    with store.transaction():
        arms.ensure(store.conn,now=now)
        first=arms.propose(store.conn,'length',{'min_words':5,'max_words':15},now=now)
        second=arms.propose(store.conn,'length',{'min_words':5,'max_words':20},now=now)
    change(action='arm',id=first,verb='accept')
    change(action='arm',id=first,verb='retire')
    change(action='arm',id=second,verb='reject')
    assert control.vetoed(store.conn,'arm',second)
    with pytest.raises(ValueError): change(action='arm',id=arms.controls()['length'],verb='retire')
    for verb in ('approve','veto','expire'):
        ident=lesson(store,now,text='She keeps her '+verb+' replies brief.')
        change(action='lesson',id=ident,verb=verb)
        row=store.conn.execute('SELECT status,expires FROM talk_lesson WHERE id=?',(ident,)).fetchone()
        assert row[0]=='active' if verb=='approve' else row[1]==now
    change(action='weights',values=dict(weights.DEFAULTS),version=0)
    with pytest.raises(ValueError,match='changed'): change(action='weights',values=dict(weights.DEFAULTS),version=0)
    assert len(labview.lab_view(store.conn,now=now)['weights']['history'])==1


def test_veto_cannot_be_reinforced_or_reproposed_and_approve_respects_cap(store,clock):
    now=clock.now(); ident=lesson(store,now)
    control.change(store,{'action':'lesson','id':ident,'verb':'veto'},now=now)
    result={'drop':[],'reinforce':[ident],'lessons':[{'scope':'guild','text':'She keeps her guild replies brief.'}]}
    with store.transaction(): lessons.persist(store.conn,101,result,known={ident},night='next',now=now+1,automatic=True)
    assert store.conn.execute('SELECT expires FROM talk_lesson WHERE id=?',(ident,)).fetchone()[0]==now
    for i in range(5): lesson(store,now,status='active',text=f'She speaks at time {i}.')
    proposed=lesson(store,now,text='She waits for a pause.')
    with pytest.raises(ValueError,match='five'):
        control.change(store,{'action':'lesson','id':proposed,'verb':'approve'},now=now)


def test_pending_coach_cannot_undo_owner_expiry_or_approval(store,clock):
    now=clock.now()
    active=[lesson(store,now,status='active',text=f'She keeps response {i} brief.') for i in range(5)]
    proposed=lesson(store,now,text='She waits for a pause.')
    snapshot=lessons.read(store.conn,guid=101,now=now)
    expected={l['id']:(l['status'],l['expires'],l['reinforced']) for l in snapshot}
    known=set(expected)
    control.change(store,{'action':'lesson','id':active[0],'verb':'expire'},now=now)
    control.change(store,{'action':'lesson','id':proposed,'verb':'approve'},now=now)
    result={'drop':[proposed],'reinforce':[active[0]],'lessons':[{'scope':'guild','text':'She keeps response 0 brief.'}]}
    with store.transaction():
        lessons.persist(store.conn,101,result,known=known,expected=expected,night='pending',now=now+1,automatic=True)
    rows=lessons.read(store.conn,guid=101,now=now+1)
    assert len([l for l in rows if l['status']=='active'])==5
    assert active[0] not in {l['id'] for l in rows} and proposed in {l['id'] for l in rows}
    # Expire is not a permanent veto: a later fresh coach may propose the idea.
    with store.transaction():
        lessons.persist(store.conn,101,dict(result,drop=[],reinforce=[]),known=set(),night='next',now=now+2,automatic=True)
    assert store.conn.execute('SELECT status FROM talk_lesson WHERE id=?',(active[0],)).fetchone()[0]=='proposed'


def test_configured_freeze_reaches_each_direct_writer_without_lab_overrides(store,clock,tmp_path):
    from leeroy_mind.fingerprint import update
    from leeroy_mind.learn import capture,recipe,coach
    from leeroy_mind.learn.config import load_learn_config
    path=tmp_path/'mind.toml'; path.write_text('[learn]\nenabled=true\nfreeze=true\nfrozen_families=["length"]\n')
    cfg=load_learn_config(path)
    before=store.conn.total_changes
    assert control.frozen(store.conn,cfg)
    assert not recipe.record(store.conn,1,None,None,None,None,settings=cfg)
    capture.delivered(store.conn,1,SimpleNamespace(),settings=cfg)
    capture.vectors(store.conn,[SimpleNamespace(id=1)],{1:[1]},'fake',settings=cfg)
    update(store.conn,101,now=clock.now(),settings=cfg)
    lessons.persist(store.conn,101,{},known=set(),night='frozen',now=clock.now(),automatic=True,settings=cfg)
    proposal={'family':'length','params':{'min_words':5,'max_words':15}}
    assert not coach.propose_arm(store,arms,proposal,'frozen',clock.now(),settings=cfg)
    assert store.conn.total_changes==before
    # Explicit owner changes remain usable; unfreezing does not enable disabled features.
    control.change(store,{'action':'freeze','frozen':False},now=clock.now())
    assert not control.frozen(store.conn,cfg)
    assert not coach.propose_arm(store,arms,proposal,'family-frozen',clock.now(),settings=cfg)


def test_configured_freeze_is_passed_by_live_pipeline(store,tmp_path):
    from engine_rig import engine_clock,make_rig
    from discord.test_discord_transport import discord_turn
    from leeroy_mind.learn.config import load_learn_config
    clock=engine_clock(); rig=make_rig(store,clock,tmp_path,reply='A synthetic member reply.')
    path=tmp_path/'mind.toml'; path.write_text('[learn]\nenabled=true\nfreeze=true\ncapture_preferences=true\n')
    try:
        rig.engine.learn_enabled=True; rig.engine.learn_capture_preferences=True
        rig.engine.learn_settings=load_learn_config(path)
        assert rig.speak_turn(discord_turn(rig,101,'general'))
        assert not store.conn.execute('SELECT 1 FROM talk_recipe').fetchone()
        assert not store.conn.execute('SELECT 1 FROM learn_rewrite').fetchone()
        assert not store.conn.execute('SELECT 1 FROM learn_line_vector').fetchone()
        assert store.get_meta('discord:line:1') is not None
    finally: rig.close()


def test_global_and_family_freeze_apply_to_bandit_without_enabling_it(store,clock):
    cfg=LearnCfg(enabled=True,bandit=True)
    control.change(store,{'action':'freeze','frozen':True},now=clock.now())
    before=store.conn.total_changes
    assert bandit.choose(store.conn,101,bandit.Context('guild',settings=cfg))==arms.controls()
    assert store.conn.total_changes==before
    control.change(store,{'action':'freeze','frozen':False},now=clock.now())
    control.change(store,{'action':'family','family':'length','frozen':True},now=clock.now())
    with store.transaction(): bandit.choose(store.conn,101,bandit.Context('guild',settings=cfg))
    assert not store.conn.execute("SELECT 1 FROM bandit_share WHERE arm_id LIKE 'length.%'").fetchone()


@pytest.mark.parametrize('args',[
    {'action':'freeze','frozen':1}, {'action':'family','family':[],'frozen':True},
    {'action':'weights','values':dict(weights.DEFAULTS,quality=float('nan')),'version':0},
    {'action':'lesson','id':True,'verb':'approve'}, {'action':'arm','id':'a','verb':[]},
    {'action':'freeze','frozen':True,'extra':'private text'}])
def test_invalid_actions_fail_in_parser(args):
    with pytest.raises(endpoint.BadRequest): endpoint.learn_args(args)


def test_endpoint_uses_wall_clock_and_refuses_disabled_or_stopping(store,clock):
    app=SimpleNamespace(store=store,clock=clock,data_dir=data_path(store),stopping=False)
    control_endpoint=endpoint.ControlEndpoint(app,endpoint.ControlCfg(enabled=True),clock=lambda:12)
    result=asyncio.run(control_endpoint.learn({'action':'weights','values':dict(weights.DEFAULTS),'version':0}))
    assert result['ok'] and weights.read(store.conn,at=clock.now()).version==1
    assert store.conn.execute('SELECT created FROM learn_weights').fetchone()[0]==clock.now()
    app.stopping=True
    assert not asyncio.run(control_endpoint.learn({'action':'freeze','frozen':True}))['ok']
    app.stopping=False
    control_endpoint.cfg=endpoint.ControlCfg(enabled=False)
    assert not asyncio.run(control_endpoint.learn({'action':'freeze','frozen':True}))['ok']


@pytest.mark.parametrize('path,args', [('/api/learn',{'action':'freeze','frozen':True}),
    ('/api/feedback',{'line_id':1,'value':-1,'reasons':['wrong-facts']})])
def test_console_mutation_gates_and_forwarding(store,path,args):
    data=data_path(store); link=FakeLink({'ok':True,'message':'Saved.'})
    async def run():
        app=make_app(data,mind=running(data),link=link)
        await app.start()
        original_view=app.view
        try:
            for kwargs in ({'key':False,'origin':True},{'origin':False},{'origin':'https://elsewhere.invalid'}):
                assert (await http(app,'POST',path,body=args,**kwargs)).status==403
            assert not link.calls
            app.controls.enabled=False
            assert (await http(app,'POST',path,body=args,origin=True)).status==403
            app.controls.enabled=True
            app.view=SimpleNamespace(state='stopped')
            assert (await http(app,'POST',path,body=args,origin=True)).status==409
            assert not link.calls
            app.view=original_view
            assert (await http(app,'POST',path,body=args,origin=True)).status==200
            assert link.calls[-1][1:]==(path.removeprefix('/api/'),args)
        finally: await app.close()
    asyncio.run(run())
