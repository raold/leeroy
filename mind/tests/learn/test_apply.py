"""Fake-model rehearsal of learning application, attribution and audience gates."""
import asyncio
import json
import random
from dataclasses import replace

import pytest

from engine_rig import make_rig
from leeroy_mind.discord import store as discord
from leeroy_mind.discord.channels import channel_id, is_discord
from leeroy_mind.learn import apply, arms, audience, bandit, control, lessons, recipe
from leeroy_mind.learn.config import LearnCfg, load_learn_config
from leeroy_mind.protocol import Chat, Group
from leeroy_mind.talk.pipeline import TurnMeta
from leeroy_mind.talk.turns import Turn, TurnKind

ON = LearnCfg(enabled=True, bandit=True, apply=True)


@pytest.fixture
def rig(store, clock, tmp_path):
    r = make_rig(store, clock, tmp_path)
    r.engine.learn_enabled = True
    r.engine.learn_settings = ON
    yield r
    r.close()


def source(rig, channel='guild', *, guid=101, text='A harmless fixture observation.', readers=None):
    lid = rig.store.insert_chat_line(Chat(0,channel,1,'Invented',guid,text,0,0),
        ts=rig.clock.now()-100, session_id=None, origin='human', audience=readers)
    if is_discord(channel):
        mid = discord.post(rig.store, channel=channel_id(channel), author=1, origin='human',
                           text=text, now=rig.clock.now()-100)
        rig.store.set_meta(f'discord:line:{mid}', str(lid))
    return lid


def lesson(rig, lid, *, guid=101, text='He keeps replies brief and clear.', scope='guild', proof=True):
    with rig.store.transaction():
        lessons.persist(rig.store.conn, guid,
            dict(lessons=[dict(text=text,scope=scope)],reinforce=[],drop=[]), known=set(),
            night='2026-11-13', now=rig.clock.now(), automatic=True,
            sources=audience.evidence(rig.store.conn,[lid]) if proof else ())
    return rig.store.conn.execute('SELECT id FROM talk_lesson WHERE guid=? AND text=?', (guid,text)).fetchone()[0]


def select(monkeypatch, **chosen):
    def choose(conn, guid, context, **kwargs):
        arms.ensure(conn)
        return arms.controls() | {k:v for k,v in chosen.items()
            if k not in ('length','tease') or getattr(context,k)}
    monkeypatch.setattr(bandit, 'choose', choose)


def compose(rig, channel='guild', guid=101):
    turn = Turn(TurnKind.REPLY_HUMAN, guid, channel, to_guid=1, addressee=1, max_lines=1)
    meta = TurnMeta(rig.engine._new_id())
    composed = rig.sync(rig.engine._compose(turn,meta,live=True))
    return turn,meta,composed


@pytest.mark.parametrize('setting', ['apply=true','apply=false','apply=1','apply="true"'])
def test_apply_config_is_boolean_and_off_by_default(tmp_path, setting):
    path=tmp_path/'mind.toml'
    path.write_text('[learn]\nenabled=true\n'+setting)
    cfg=load_learn_config(path)
    assert cfg.apply is (setting == 'apply=true')
    if setting in ('apply=1','apply="true"'):
        assert cfg == LearnCfg()
    assert load_learn_config(tmp_path/'absent') == LearnCfg()


@pytest.mark.parametrize('family,params', [(family,p) for family,values in arms.PRESETS.items() for p in values])
def test_each_arm_value_has_a_closed_renderer_or_scoped_materializer(family,params):
    arm=arms.Arm('test',family,params,1,'active','builtin',0)
    text,predict=apply.render_arm(arm,humour=10,forward=10,romance=10,female=True,rng=random.Random(1))
    assert bool(text) is (family not in ('typing','callback'))
    assert (predict is not None) is (family == 'length')
    assert len(text)<160
    control_arm=replace(arm,status='control',params={'mode':'control'})
    assert apply.render_arm(control_arm,humour=10,forward=10,romance=10,female=True,rng=None)==('',None)


@pytest.mark.parametrize('female,forward,romance', [(False,10,10),(True,0,10),(True,10,0)])
def test_tease_and_humour_cannot_exceed_dials(female,forward,romance):
    tease=arms.Arm('test','tease',{'level':'light'},1,'active','builtin',0)
    assert apply.render_arm(tease,humour=0,forward=forward,romance=romance,female=female,rng=None)[0]=='No added teasing in this reply.'
    humour=replace(tease,family='humour',params={'level':'playful'})
    assert 'straightforward' in apply.render_arm(humour,humour=0,forward=10,romance=10,female=True,rng=None)[0]
    assert 'light humour' in apply.render_arm(humour,humour=1,forward=10,romance=10,female=True,rng=None)[0]


@pytest.mark.parametrize('source_channel,turn_channel,expected', [
    ('whisper','whisper',True),('whisper','dm:101',True),('dm:101','whisper',True),
    ('whisper','guild',False),('whisper','party',False),('whisper','discord:general',False),
    ('whisper','dm:102',False),('officer','officer',True),('officer','discord:officer-chat',True),
    ('discord:officer-chat','officer',True),('officer','guild',False),('officer','discord:general',False),
    ('guild','whisper',True),('guild','discord:general',True)])
def test_notes_follow_audience_across_media(rig,monkeypatch,source_channel,turn_channel,expected):
    officer='officer' in source_channel
    guid=102 if officer else 101
    select(monkeypatch)
    lid=source(rig,source_channel,guid=guid)
    note=lesson(rig,lid,guid=guid,scope='group' if officer else 'whisper' if source_channel!='guild' else 'guild')
    _,meta,c=compose(rig,turn_channel,guid)
    assert ('Style observation:' in c.messages[-1]['content']) is expected
    assert (note in meta.recipe_snapshot.lesson_ids) is expected
    assert 'Style observation:' not in c.messages[0]['content']


def test_legacy_expired_vetoed_foreign_and_deleted_notes_fail_closed(rig,monkeypatch):
    select(monkeypatch)
    lid=source(rig,'dm:101')
    ident=lesson(rig,lid,scope='whisper')
    lesson(rig,source(rig),text='He uses an older style without provenance.',proof=False)
    lesson(rig,source(rig,guid=102),guid=102,text='He has another member style.')
    assert ident in compose(rig,'dm:101')[1].recipe_snapshot.lesson_ids
    rig.store.conn.execute('UPDATE discord_message SET deleted=?',(rig.clock.now(),))
    rig.store.conn.commit()
    assert compose(rig,'dm:101')[1].recipe_snapshot.lesson_ids == ()
    fresh=lesson(rig,source(rig),text='He explains one point at a time.')
    control.change(rig.store,dict(action='lesson',id=fresh,verb='veto'),now=rig.clock.now())
    assert compose(rig)[1].recipe_snapshot.lesson_ids == ()


def test_reinforcement_never_widens_private_provenance(rig,monkeypatch):
    select(monkeypatch)
    ident=lesson(rig,source(rig,'whisper'),scope='whisper')
    public=source(rig)
    with rig.store.transaction():
        lessons.persist(rig.store.conn,101,dict(lessons=[],reinforce=[ident],drop=[]),known={ident},
            night='2026-11-14',now=rig.clock.now(),automatic=True,sources=audience.evidence(rig.store.conn,[public]))
    assert ident in compose(rig,'dm:101')[1].recipe_snapshot.lesson_ids
    assert compose(rig,'guild')[1].recipe_snapshot.lesson_ids == ()


def test_current_forward_floor_is_enforced_again_after_coaching(rig,monkeypatch):
    from leeroy_mind.tuning.effective import effective, MemberTuning
    select(monkeypatch)
    lid=source(rig)
    ident=lesson(rig,lid)
    with rig.store.transaction():
        audience.retain(rig.store.conn,ident,[f'chat:{lid}'],forward_min=8)
    get=rig.engine.personas.get
    monkeypatch.setattr(rig.engine.personas,'get',lambda g:effective(get(g),MemberTuning({'forward':1})))
    assert compose(rig)[1].recipe_snapshot.lesson_ids==()


def test_nested_misscoped_memory_and_missing_sources_never_widen_a_note(rig,monkeypatch):
    select(monkeypatch)
    mid=callback_memory(rig,channel='whisper')
    rig.store.conn.execute("UPDATE memory SET scope='open',scope_with=0 WHERE id=?",(mid,))
    ident=lesson(rig,source(rig))
    with rig.store.transaction():
        audience.retain(rig.store.conn,ident,[f'memory:{mid}'])
    assert compose(rig)[1].recipe_snapshot.lesson_ids==()
    assert ident in compose(rig,'dm:101')[1].recipe_snapshot.lesson_ids
    rig.store.conn.execute('DELETE FROM chat_line WHERE id=(SELECT min(id) FROM chat_line)')
    rig.store.conn.commit()
    assert compose(rig,'dm:101')[1].recipe_snapshot.lesson_ids==()


@pytest.mark.parametrize('how', ['apply_off','toml_freeze','lab_freeze','member_freeze'])
def test_off_and_global_freeze_are_byte_identical_and_global_freeze_stops_recipes(rig,how):
    rig.engine.learn_settings=replace(ON,apply=False)
    _,_,baseline=compose(rig)
    state=rig.engine.rng.getstate()
    if how=='toml_freeze':
        rig.engine.learn_settings=replace(ON,freeze=True)
        control.change(rig.store,dict(action='freeze',frozen=False),now=rig.clock.now())
    elif how=='lab_freeze':
        rig.engine.learn_settings=ON
        control.change(rig.store,dict(action='freeze',frozen=True),now=rig.clock.now())
    elif how=='member_freeze':
        rig.engine.learn_settings=replace(ON,frozen_members=(101,))
    turn,meta,c=compose(rig)
    assert c.messages==baseline.messages
    assert meta.learning is None and rig.engine.rng.getstate()==state
    line=rig.engine._record(turn,meta,c.persona,'A plain line.',[])
    row=rig.store.conn.execute('SELECT * FROM talk_recipe WHERE line_id=?',(line,)).fetchone()
    if how in ('toml_freeze', 'lab_freeze'):
        assert row is None
    else:
        assert json.loads(row['arm_ids'])==json.loads(row['lesson_ids'])==[]
    assert not rig.store.conn.execute('SELECT 1 FROM bandit_turn').fetchone()


def test_family_freeze_uses_control_even_if_choice_or_read_is_stale(rig,monkeypatch):
    select(monkeypatch,length='length.0.v1',questions='questions.0.v1')
    rig.engine.learn_settings=replace(ON,frozen_families=('length',))
    control.change(rig.store,dict(action='family',family='length',frozen=False),now=rig.clock.now())
    _,meta,c=compose(rig)
    assert 'length.control.v1' in meta.recipe_snapshot.arm_ids
    assert meta.recipe_snapshot.num_predict is None
    assert 'questions.0.v1' in meta.recipe_snapshot.arm_ids
    assert 'without adding a new question' in c.messages[-1]['content']


def test_model_parameters_and_recipe_are_complete_after_acknowledged_delivery(rig,monkeypatch):
    select(monkeypatch,length='length.0.v1',questions='questions.0.v1')
    ident=lesson(rig,source(rig))
    turn,meta,c=compose(rig)
    turn.deadline=rig.clock.now()+60
    rig.sync(rig.engine._ask(turn,c.messages,meta=meta))
    assert rig.chat_calls()[-1]['options']['num_predict']==40
    lid=rig.engine._record(turn,meta,c.persona,'A bounded fixture reply.',[])
    row=rig.store.conn.execute('SELECT * FROM talk_recipe WHERE line_id=?',(lid,)).fetchone()
    assert len(json.loads(row['arm_ids']))==len(arms.FAMILIES)
    assert json.loads(row['lesson_ids'])==[ident]
    assert bandit.valid_key(row['bandit_context'])
    detail=json.loads(rig.store.get_meta(f'learn:recipe:{lid}'))
    assert (detail['version'],detail['callback_id'],detail['num_predict'])==(1,None,40)
    assert detail['parameters']['num_predict']==40 and detail['parameters']['num_ctx']==4096
    second=rig.engine._record(turn,meta,c.persona,'A second fragment.',[])
    assert rig.store.conn.execute('SELECT recipe_line_id FROM talk_recipe_line WHERE line_id=?',(second,)).fetchone()[0]==lid
    assert rig.store.conn.execute('SELECT count(*) FROM talk_recipe').fetchone()[0]==1


def test_trim_removes_attribution_and_parameters_for_discarded_style(rig,monkeypatch):
    select(monkeypatch,length='length.0.v1')
    turn,meta,c=compose(rig)
    apply.used(meta,0)
    assert meta.recipe_snapshot.arm_ids==tuple(arms.controls().values())
    assert meta.recipe_snapshot.num_predict is None and meta.learning.num_predict is None


def test_fixed_style_templates_are_versioned_without_private_content(rig,monkeypatch):
    select(monkeypatch,length='length.0.v1',callback='callback.1.v1')
    rig.engine._learn_features=(True,False)
    rig.store.start_session(rig.clock.now()-7200)
    callback_memory(rig,channel='whisper')
    lesson(rig,source(rig,'whisper'),scope='whisper',text='He remembers the confidential picnic preference.')
    traced=[]
    original=apply.note_template
    def trace(name,text):
        traced.append((name,text))
        original(name,text)
    monkeypatch.setattr(apply,'note_template',trace)
    _,first,_=compose(rig,'dm:101')
    assert {'learn.style.v1','learn.lesson.v1','learn.callback.v1'} <= {k for k,_ in traced}
    assert 'learn.arm.length.v1' not in {k for k,_ in traced}
    assert 'picnic' not in json.dumps(traced) and 'confidential' not in json.dumps(traced)
    apply.release(rig.engine,first)
    monkeypatch.setattr(apply,'LESSON_HEAD','A recent style observation: ')
    _,second,_=compose(rig,'dm:101')
    assert first.recipe_snapshot.template_version!=second.recipe_snapshot.template_version


def test_guard_rejected_choice_gets_no_invented_recipe(rig,monkeypatch):
    select(monkeypatch,length='length.0.v1')
    turn,meta,_=compose(rig)
    rig.engine._drop(turn,meta,'guard_drop')
    rig.engine._drop(turn,meta,'guard_drop')
    assert rig.store.conn.execute('SELECT count(*) FROM bandit_rejection').fetchone()[0]==7
    assert not rig.store.conn.execute('SELECT 1 FROM talk_recipe').fetchone()


def test_freeze_during_snapshot_read_prevents_application(rig,monkeypatch):
    select(monkeypatch,length='length.0.v1')
    old=rig.engine.live_review.read
    async def read(question):
        result=await old(question)
        control.change(rig.store,dict(action='freeze',frozen=True),now=rig.clock.now())
        return result
    monkeypatch.setattr(rig.engine.live_review,'read',read)
    _,meta,c=compose(rig)
    assert meta.learning is None
    assert 'Your recent style notes' not in c.messages[-1]['content']


def test_owner_expiry_during_read_drops_the_pending_lesson(rig,monkeypatch):
    select(monkeypatch)
    ident=lesson(rig,source(rig))
    old=rig.engine.live_review.read
    async def read(question):
        result=await old(question)
        control.change(rig.store,dict(action='lesson',id=ident,verb='expire'),now=rig.clock.now())
        return result
    monkeypatch.setattr(rig.engine.live_review,'read',read)
    assert compose(rig)[1].recipe_snapshot.lesson_ids==()


def callback_memory(rig, *, guid=101, channel='guild', readers=None):
    now=rig.clock.now()
    lid=source(rig,channel,guid=guid,readers=readers)
    rig.store.conn.execute('UPDATE chat_line SET ts=? WHERE id=?',(now-5*86400,lid))
    mid=rig.store.conn.execute("INSERT INTO memory(owner,kind,text,t_event,t_created,t_accessed,importance,"
        "valence,source,evidence,actors,scope,scope_with) VALUES (?,'conversation',?,?,?,?,8,.6,'chat',?,?,?,?)",
        (guid,'The picnic with the red kite was memorable.',now-5*86400,now-5*86400,now-5*86400,
         json.dumps([f'chat:{lid}']),json.dumps([guid,1]),'private' if channel=='whisper' else 'open',
         1 if channel=='whisper' else 0)).lastrowid
    rig.store.conn.commit()
    return mid


def test_callback_receipt_on_success_only_and_quota_blocks_next_offer(rig,monkeypatch):
    select(monkeypatch,callback='callback.1.v1')
    rig.engine._learn_features=(True,False)
    rig.store.start_session(rig.clock.now()-7200)
    mid=callback_memory(rig,channel='whisper')
    turn,meta,c=compose(rig,'dm:101')
    assert meta.recipe_snapshot.callback_id==mid
    assert 'Something you might bring up if it fits:' in c.messages[-1]['content']
    assert mid in c.memory_ids
    assert not rig.store.conn.execute('SELECT 1 FROM callback_use').fetchone()
    # A second in-flight offer cannot bypass the per-member quota.
    assert compose(rig,'dm:101')[1].recipe_snapshot.callback_id is None
    lid=rig.engine._record(turn,meta,c.persona,'A pleasant memory of that kite.',c.memory_ids)
    assert rig.store.conn.execute('SELECT memory_id FROM callback_use').fetchone()[0]==mid
    assert json.loads(rig.store.get_meta(f'learn:recipe:{lid}'))['callback_id']==mid
    apply.release(rig.engine,meta)
    assert compose(rig,'whisper')[1].recipe_snapshot.callback_id is None


def test_callback_failed_send_releases_hold_and_transaction_rolls_back(rig,monkeypatch):
    select(monkeypatch,callback='callback.1.v1')
    rig.engine._learn_features=(True,False)
    rig.world.human_online=True
    rig.store.start_session(rig.clock.now()-7200)
    mid=callback_memory(rig)
    turn,meta,c=compose(rig)
    assert meta.recipe_snapshot.callback_id==mid
    with pytest.raises(RuntimeError), rig.store.transaction():
        rig.engine._record(turn,meta,c.persona,'An invented line.',c.memory_ids)
        raise RuntimeError('rollback')
    assert not rig.store.conn.execute('SELECT 1 FROM callback_use').fetchone()
    assert not rig.store.conn.execute('SELECT 1 FROM talk_recipe').fetchone()
    apply.release(rig.engine,meta)
    assert compose(rig)[1].recipe_snapshot.callback_id==mid


def test_callback_arm_still_honours_its_feature_switch_and_offline_play_quota(rig,monkeypatch):
    select(monkeypatch,callback='callback.1.v1')
    callback_memory(rig)
    rig.engine._learn_features=(True,False)
    assert compose(rig)[1].recipe_snapshot.callback_id is None  # no owner play session
    rig.store.start_session(rig.clock.now()-7200)
    rig.engine._learn_features=(False,False)
    assert compose(rig)[1].recipe_snapshot.callback_id is None


def test_fingerprint_hint_uses_only_permitted_history_and_keeps_private_text_out(rig,monkeypatch):
    select(monkeypatch,typing='typing.2.v1')
    rig.engine._learn_features=(False,True)
    for i in range(55):
        turn=Turn(TurnKind.REPLY_HUMAN,101,'whisper',to_guid=1,addressee=1)
        meta=TurnMeta(rig.engine._new_id())
        rig.engine._record(turn,meta,rig.engine.personas.get(101),'PRIVATE_SENTINEL '+('word '*24),[])
    public=compose(rig)[2].messages[-1]['content']
    private=compose(rig,'dm:101')[2].messages[-1]['content']
    assert 'Typing:' in public and 'Typing:' in private
    assert 'Typing: longer lines' in private and 'Typing: longer lines' not in public
    assert 'PRIVATE_SENTINEL' not in public
    assert 'PRIVATE_SENTINEL' not in private.split('Your recent style notes')[1].split('\n\n')[0]


@pytest.mark.parametrize('channel,why', [('guild','reply:named'),('whisper','reply:whisper'),
    ('discord:general','discord:reply'),('dm:101','discord:reply')])
def test_every_live_transport_applies_and_credits_the_recipe(rig,monkeypatch,channel,why):
    from leeroy_mind.learn import weights
    select(monkeypatch,length='length.0.v1',questions='questions.0.v1')
    rig.script('the harbor path sounds pleasant')
    turn=Turn(TurnKind.REPLY_HUMAN,101,channel,to_guid=1,addressee=1,max_lines=1)
    meta=TurnMeta(rig.engine._new_id(),answers_human=True,why=why)
    async def run():
        task=asyncio.create_task(rig.engine.speak_meta(turn,meta))
        for _ in range(100):
            await rig.advance(1)
            await asyncio.sleep(.002)
            if task.done():
                break
        assert task.done()
        return await task
    assert rig.sync(run())
    from leeroy_mind.llm.presets import LIVE
    from leeroy_mind.talk.discord_length import preset_for
    expected = preset_for(LIVE, rig.engine.cfg.talk.discord_words).options['num_predict'] if is_discord(channel) else 40
    assert rig.chat_calls()[-1]['options']['num_predict']==expected
    rows=rig.store.conn.execute('SELECT p.*,c.channel FROM talk_recipe p JOIN chat_line c ON c.id=p.line_id').fetchall()
    assert len(rows)==1 and rows[0]['channel']==channel
    assert ('length.control.v1' if is_discord(channel) else 'length.0.v1') in json.loads(rows[0]['arm_ids'])
    if is_discord(channel):
        assert not rig.sent.log
        mid=rig.store.conn.execute("SELECT id FROM discord_message WHERE origin='mind'").fetchone()[0]
        assert rig.store.get_meta(f'discord:line:{mid}')==str(rows[0]['line_id'])
    with rig.store.transaction():
        version=weights.ensure(rig.store.conn,now=rig.clock.now()).version
        rig.store.conn.execute('INSERT INTO talk_reward(line_id,guid,value,parts,weights_version,scored_at) VALUES (?,101,1,?,?,?)',
            (rows[0]['line_id'],'{}',version,rig.clock.now()))
        assert bandit.update(rig.store.conn,rows[0]['line_id'],settings=ON,now=rig.clock.now())
    credits=rig.store.conn.execute('SELECT success,failure FROM bandit_credit').fetchall()
    assert len(credits)==7 and sum(r[0] for r in credits)==pytest.approx(1)
    assert sum(r[1] for r in credits)==0


@pytest.mark.parametrize('ref', ['event:1', 'owner:fixture-history'])
def test_non_chat_evidence_can_back_a_lesson(rig,monkeypatch,ref):
    select(monkeypatch)
    mid=callback_memory(rig)
    rig.store.conn.execute('UPDATE memory SET evidence=? WHERE id=?',(json.dumps([ref]),mid))
    ident=lesson(rig,source(rig))
    audience.retain(rig.store.conn,ident,[f'memory:{mid}'])
    rig.store.conn.commit()
    assert ident in compose(rig)[1].recipe_snapshot.lesson_ids


def test_provenance_cap_is_512_distinct_nodes_and_shared_work_is_memoized(rig):
    conn=rig.store.conn
    refs=[f'event:{i}' for i in range(1,513)]
    gate=audience.Visibility(conn,guid=101,channel='guild',to_guid=0)
    assert gate.visible(refs) and len(gate.visited)==512
    assert gate.visible(refs)  # the same graph consumes no second budget
    assert not gate.visible(['event:513'])
    assert not audience.visible(conn,refs+['event:513'],guid=101,channel='guild',to_guid=0)


def test_deep_provenance_is_bounded_without_using_the_python_call_stack(rig):
    conn = rig.store.conn
    ref = 'event:1'
    for _ in range(511):
        mid = conn.execute("INSERT INTO memory(owner,kind,text,t_event,t_created,t_accessed,importance,"
            "valence,source,evidence,actors,scope,scope_with) VALUES (101,'conversation','Fixture',"
            "0,0,0,5,0,'chat',?,'[101]','open',0)", (json.dumps([ref]),)).lastrowid
        ref = f'memory:{mid}'
    assert audience.visible(conn,[ref],guid=101,channel='guild',to_guid=0)
    conn.execute('UPDATE memory SET evidence=? WHERE id=?', (json.dumps([ref]), mid-510))
    assert not audience.visible(conn,[ref],guid=101,channel='guild',to_guid=0)


def test_nested_generated_line_expands_its_recalled_private_memory(rig):
    secret=callback_memory(rig,channel='whisper')
    turn=Turn(TurnKind.REPLY_HUMAN,101,'guild',to_guid=1)
    line=rig.engine._record(turn,TurnMeta('nested'),rig.engine.personas.get(101),'A bland generated fixture.',[secret])
    mid=callback_memory(rig)
    rig.store.conn.execute('UPDATE memory SET evidence=? WHERE id=?',(json.dumps([f'chat:{line}']),mid))
    assert not audience.visible(rig.store.conn,[f'memory:{mid}'],guid=101,channel='guild',to_guid=0)
    assert audience.visible(rig.store.conn,[f'memory:{mid}'],guid=101,channel='whisper',to_guid=1)


def local_room(rig, channel):
    from types import SimpleNamespace
    rig.store.set_meta('human_guid','1')  # app.py records this on the bridge hello
    rig.world.human_online=True
    rig.world.group=Group(0,1,False,(1,101,102))
    rig.world.room_audience=lambda _:frozenset({101,102})
    rig.world.human_zone=rig.world.members[101].zone
    rig.engine.comms=SimpleNamespace(available=lambda:True,human_room=lambda:'lounge')
    return rig.world.audience(channel,from_guid=1,to_guid=101,zone=rig.world.human_zone)


@pytest.mark.parametrize('channel', ['party','raid','raid_warning','say','yell','voice:lounge'])
def test_local_sources_use_real_cast_audiences_with_the_owner_present(rig,channel):
    recorded=local_room(rig,channel)
    assert recorded=={101,102} and 1 not in recorded
    lid=source(rig,channel,readers=recorded)
    gate=lambda readers:audience.visible(rig.store.conn,[f'chat:{lid}'],guid=101,
        channel=channel,to_guid=0,readers=readers,human_guid=1)
    readers=rig.world.conversation_readers(channel,from_guid=101,comms=rig.engine.comms)
    assert readers=={1,101,102} and gate(readers)
    assert not gate(readers | {103})
    assert not gate([])


def test_party_lesson_and_typing_history_apply_with_the_owner_present(rig,monkeypatch):
    recorded=local_room(rig,'party')
    select(monkeypatch)
    ident=lesson(rig,source(rig,'party',readers=recorded),scope='group')
    assert ident in compose(rig,'party')[1].recipe_snapshot.lesson_ids
    for _ in range(50):
        turn=Turn(TurnKind.REPLY_HUMAN,101,'party')
        rig.engine._record(turn,TurnMeta(rig.engine._new_id()),rig.engine.personas.get(101),'A fixture line.',[])
    readers=rig.world.conversation_readers('party',from_guid=101)
    profile=lambda current:apply.scoped_profile(rig.store.conn,101,rig.engine.personas.get(101),
        channel='party',to_guid=0,readers=current,officer=False,human_guid=1)
    assert profile(readers).samples==50
    assert profile(readers | {103}).samples==0
    rig.world.group=Group(0,1,False,(1,101,102,103))
    assert ident not in compose(rig,'party')[1].recipe_snapshot.lesson_ids


@pytest.mark.parametrize('channel', ['dm:101','discord:general'])
@pytest.mark.parametrize('words', [0,90,200])
def test_discord_length_draw_keeps_channel_instruction_and_allowance(rig,monkeypatch,channel,words):
    from leeroy_mind.llm.presets import LIVE
    from leeroy_mind.talk.discord_length import preset_for
    rig.engine.cfg=replace(rig.engine.cfg,talk=replace(rig.engine.cfg.talk,discord_words=words))
    select(monkeypatch,length='length.2.v1')
    turn,meta,c=compose(rig,channel)
    turn.deadline=rig.clock.now()+60
    rig.sync(rig.engine._ask(turn,c.messages,meta=meta))
    assert meta.learning.selected['length']==arms.controls()['length']
    assert meta.learning.num_predict is None
    assert '20-60 words' not in str(c.messages)
    expected=preset_for(LIVE,words).options['num_predict'] if words else LIVE.options['num_predict']
    assert rig.chat_calls()[-1]['options']['num_predict']==expected
    if words:
        assert f'at most about {words} words' in str(c.messages)


@pytest.mark.parametrize('empty_sources',[False,True])
def test_duplicate_reinforcement_preserves_the_raised_forward_floor(rig,empty_sources):
    lid=source(rig)
    ident=lesson(rig,lid)
    result=dict(reinforce=[ident],drop=[],lessons=[dict(text='He keeps replies brief and clear.',
                                                    scope='guild',forward_min=8)])
    lessons.persist(rig.store.conn,101,result,known={ident},night='next',now=rig.clock.now(),
                    automatic=True,sources=[] if empty_sources else [f'chat:{lid}'])
    assert audience.forward_floor(rig.store.conn,ident)==8
    assert audience.saved(rig.store.conn,ident)==[f'chat:{lid}']


def test_empty_provenance_stays_closed_while_its_forward_floor_rises(rig):
    ident=lesson(rig,source(rig))
    audience.retain(rig.store.conn,ident,[f'event:{i}' for i in range(1,514)])
    assert audience.saved(rig.store.conn,ident)==[]
    audience.retain(rig.store.conn,ident,[],forward_min=8)
    assert audience.forward_floor(rig.store.conn,ident)==8
    audience.retain(rig.store.conn,ident,['event:1'],forward_min=2)
    assert audience.saved(rig.store.conn,ident)==[]
    assert audience.forward_floor(rig.store.conn,ident)==8


def test_apply_off_adds_no_recipe_meta_and_retention_removes_old_receipts(rig):
    from leeroy_mind.learn.capture import prune
    rig.engine.learn_settings=replace(ON,apply=False)
    turn,meta,c=compose(rig)
    turn.deadline=rig.clock.now()+60
    rig.sync(rig.engine._ask(turn,c.messages,meta=meta))
    lid=rig.engine._record(turn,meta,c.persona,'A normal fixture line.',[])
    assert rig.store.conn.execute('SELECT 1 FROM talk_recipe WHERE line_id=?',(lid,)).fetchone()
    assert rig.store.get_meta(f'learn:recipe:{lid}') is None
    old=source(rig)
    rig.store.conn.execute('UPDATE chat_line SET ts=? WHERE id=?',(rig.clock.now()-91*86400,old))
    for ident in (old,lid,999999):
        rig.store.set_meta(f'learn:recipe:{ident}','{}')
    prune(rig.store.conn,rig.clock.now())
    assert rig.store.get_meta(f'learn:recipe:{lid}')=='{}'
    assert rig.store.get_meta(f'learn:recipe:{old}') is None
    assert rig.store.get_meta('learn:recipe:999999') is None


def test_apply_off_lesson_evidence_expires_ninety_days_after_the_lesson(rig):
    from leeroy_mind.learn.capture import prune
    rig.engine.learn_settings=replace(ON,apply=False)
    ids=[lesson(rig,source(rig),text=f'He prefers a clear fixture {i}.') for i in range(3)]
    cutoff=rig.clock.now()-90*86400
    for ident,expiry in zip(ids,(cutoff-1,cutoff,rig.clock.now()+1)):
        rig.store.conn.execute('UPDATE talk_lesson SET expires=? WHERE id=?',(expiry,ident))
    prune(rig.store.conn,rig.clock.now())
    assert audience.saved(rig.store.conn,ids[0])==[]
    assert all(audience.saved(rig.store.conn,ident) for ident in ids[1:])
    assert rig.store.conn.execute('SELECT count(*) FROM talk_lesson').fetchone()[0]==3


def test_multi_row_bit_keeps_its_full_output_allowance(rig,monkeypatch):
    from leeroy_mind.llm.presets import LIVE_BIT
    select(monkeypatch,length='length.0.v1')
    turn=Turn(TurnKind.BIT,101,'guild',max_lines=3)
    meta=TurnMeta('multi-row-bit')
    c=rig.sync(rig.engine._compose(turn,meta,live=True))
    turn.deadline=rig.clock.now()+60
    rig.sync(rig.engine._ask(turn,c.messages,meta=meta))
    assert meta.learning.selected['length']==arms.controls()['length']
    assert meta.learning.num_predict is None
    assert 'This reply usually fits in 3-12 words' not in str(c.messages)
    assert rig.chat_calls()[-1]['options']['num_predict']==LIVE_BIT.options['num_predict']


@pytest.mark.parametrize('channel',['dm:101','discord:general'])
@pytest.mark.parametrize('target',['addressee','to_guid'])
def test_discord_reply_to_owner_allows_tease_without_game_presence(rig,monkeypatch,channel,target):
    select(monkeypatch,tease='tease.1.v1')
    rig.world.human_online=False
    turn=Turn(TurnKind.REPLY_HUMAN,101,channel,**{target:1})
    meta=TurnMeta('discord-owner')
    applied=rig.sync(apply.prepare(rig.engine,turn,meta,rig.engine.personas.get(101),now=rig.clock.now()))
    assert applied.context.tease
    assert applied.selected['tease']=='tease.1.v1'


def test_callback_seed_is_held_evidence_and_sent_use_survives_a_freeze(rig,monkeypatch):
    select(monkeypatch,callback='callback.1.v1')
    rig.engine._learn_features=(True,False)
    rig.store.start_session(rig.clock.now()-7200)
    callback_memory(rig,channel='whisper')
    turn,meta,c=compose(rig,'dm:101')
    assert meta.learning.callback.seed in c.held
    control.change(rig.store,dict(action='freeze',frozen=True),now=rig.clock.now())
    rig.engine._record(turn,meta,c.persona,'Remember that red kite?',c.memory_ids)
    assert not rig.store.conn.execute('SELECT 1 FROM talk_recipe').fetchone()
    assert rig.store.conn.execute('SELECT memory_id FROM callback_use').fetchone()[0]==meta.learning.callback.memory_id
    control.change(rig.store,dict(action='freeze',frozen=False),now=rig.clock.now())
    apply.release(rig.engine,meta)
    assert compose(rig,'dm:101')[1].recipe_snapshot.callback_id is None


@pytest.mark.parametrize('channel',['guild','discord:general'])
@pytest.mark.parametrize('origin',['whisper','officer'])
def test_private_and_officer_lessons_do_not_enter_public_banter(rig,monkeypatch,channel,origin):
    select(monkeypatch)
    guid=102 if origin=='officer' else 101
    lesson(rig,source(rig,origin,guid=guid),guid=guid,scope='group' if origin=='officer' else 'whisper')
    turn=Turn(TurnKind.ASIDE,guid,channel)
    meta=TurnMeta('public-banter',why='banter:fixture')
    c=rig.sync(rig.engine._compose(turn,meta,live=True))
    assert 'Style observation:' not in str(c.messages)


@pytest.mark.parametrize('game_online',[False,True])
def test_owner_absent_discord_discussion_does_not_offer_a_tease_arm(rig,monkeypatch,game_online):
    select(monkeypatch,tease='tease.1.v1')
    rig.world.human_online=game_online
    turn=Turn(TurnKind.CONTINUE,101,'discord:general',addressee=102)
    meta=TurnMeta('absent-owner')
    rig.sync(rig.engine._compose(turn,meta,live=True))
    assert meta.learning.selected['tease']==arms.controls()['tease']


@pytest.mark.parametrize('channel',['party','raid','raid_warning','voice:lounge','say','yell','discord:general'])
def test_callbacks_use_real_recorded_audiences_with_the_owner_present(rig,monkeypatch,channel):
    select(monkeypatch,callback='callback.1.v1')
    rig.engine._learn_features=(True,False)
    rig.store.start_session(rig.clock.now()-7200)
    recorded=local_room(rig,channel)
    assert (1 in recorded) is is_discord(channel)
    mid=callback_memory(rig,channel=channel,readers=recorded)
    turn=Turn(TurnKind.REPLY_HUMAN,101,channel,addressee=1)
    meta=TurnMeta('callback-room')
    applied=rig.sync(apply.prepare(rig.engine,turn,meta,rig.engine.personas.get(101),now=rig.clock.now()))
    assert 1 in applied.callback_context.readers if applied.callback_context else False
    assert applied.callback and applied.callback.memory_id==mid
    from leeroy_mind.memory.callbacks import pick_callback
    # The owner stays in participants for the actor check; cast-only readers are valid too.
    ctx=replace(applied.callback_context,readers=tuple(recorded))
    assert pick_callback(rig.store.conn,101,ctx,now=rig.clock.now()).memory_id==mid
    if not is_discord(channel):
        ctx=replace(ctx,readers=(*ctx.readers,103))
        assert pick_callback(rig.store.conn,101,ctx,now=rig.clock.now()) is None
    rig.store.conn.execute('UPDATE memory SET actors=? WHERE id=?',('[101,102]',mid))
    assert pick_callback(rig.store.conn,101,applied.callback_context,now=rig.clock.now()) is None
    rig.store.conn.commit()
    apply.release(rig.engine,meta)
    assert rig.sync(apply.prepare(rig.engine,turn,TurnMeta('cast-only-actors'),
        rig.engine.personas.get(101),now=rig.clock.now())).callback is None


def test_provenance_at_twenty_thousand_discord_receipts_is_indexed_and_bounded(rig):
    from statistics import median
    from time import perf_counter
    conn=rig.store.conn
    mids=[callback_memory(rig) for _ in range(3)]
    for i in range(300):
        ts=rig.clock.now()-i-1
        text=f'A synthetic garden observation {i}.'
        lid=rig.store.insert_chat_line(Chat(0,'dm:101',101,'Invented',1,text,0,0),
            ts=ts,session_id=None,origin='mind',audience=[1,101])
        rig.store.log_utterance(ts,101,'dm:101','reply_human',text,mids,None,None,to_guid=1)
        mid=discord.post(rig.store,channel='dm:101',author=101,origin='mind',text=text,now=ts)
        rig.store.set_meta(f'discord:line:{mid}',str(lid))
    conn.executemany('INSERT INTO meta(key,value) VALUES (?,?)',
        [(f'discord:line:{100000+i}',str(100000+i)) for i in range(19700)])
    conn.commit()
    plan=conn.execute("EXPLAIN QUERY PLAN SELECT value FROM meta WHERE key GLOB 'discord:line:*' AND value=?",('1',)).fetchall()
    assert any('meta_discord_line_value' in str(tuple(r)) for r in plan)
    times=[]
    for _ in range(5):
        begin=perf_counter()
        profile=apply.scoped_profile(conn,101,rig.engine.personas.get(101),channel='dm:101',
                                     to_guid=1,readers=(1,101),officer=False)
        times.append(perf_counter()-begin)
    print(f'provenance_20000_receipts_median_ms={median(times)*1000:.2f}')
    # 512 distinct nodes maximum and indexed receipts: no linear history scan.
    # Generous wall-clock ceiling tolerates shared CI hosts; report the actual median.
    assert median(times)<.25
