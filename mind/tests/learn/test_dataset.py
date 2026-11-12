import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from conftest import seed_test_cast,make_persona
from .test_recipe import rig
from leeroy_mind.learn import dataset,capture,offline_score
from leeroy_mind.learn.cli import learn_main
from leeroy_mind.learn.weights import ensure
from leeroy_mind.personas.io import save_persona
from leeroy_mind.human import HumanIdentity
from leeroy_mind.protocol import Chat
from leeroy_mind.signals.store import feedback
from leeroy_mind.storage.rows import pack_vec
from leeroy_mind.talk.pipeline import TurnMeta
from leeroy_mind.talk.turns import Turn,TurnKind


@pytest.fixture
def data(store,clock,tmp_path):
    seed_test_cast(store)
    store.set_meta('human_guid','1')
    save_persona(store,make_persona(101,'Invented'),source='seed',ts=clock.now(),race='Orc',class_name='Warrior')
    return NS(store=store,clock=clock,root=tmp_path,ident=HumanIdentity(1,'Ownerly','','Avery Example',True),
              now=clock.now(),before=clock.now()-20*dataset.DAY)


def add(data,text,*,channel='guild',ts=None,guid=101,to=1,origin='mind',audience=None):
    return data.store.insert_chat_line(Chat(0,channel,guid,'Invented',to,text,0,0),
        ts=data.before if ts is None else ts,origin=origin,session_id=None,
        audience=audience if audience is not None else [1,101])


def pair(data,*,channel='guild',good='Take the left path past the tower.',bad='Go around the far hill.',offset=0):
    ids=[]
    for i,(text,value) in enumerate(((good,1),(bad,-1))):
        ts=data.before+offset+i*100
        add(data,'Which path should we take?',channel=channel,ts=ts,guid=1,to=101,origin='human')
        lid=add(data,text,channel=channel,ts=ts+1)
        with data.store.transaction():
            feedback(data.store.conn,dict(line_id=lid,value=value,reasons=[]),1,ts+2)
        ids.append(lid)
    return ids


def mine(data,**kw):
    return dataset.mine(data.store.conn,data.ident,since=data.before-1,until=data.now-7*dataset.DAY,**kw)


def test_thumb_mining_context_summary_and_pseudonym(data):
    pair(data)
    pairs,indices,counts=mine(data)
    assert len(pairs)==len(indices)==counts['pairs']==1
    item=pairs[0]
    assert set(item)=={'context','chosen','rejected','source','member_guid_hash'}
    assert item['source']=='thumbs' and item['context']['lines'][-1]['role']=='owner'
    assert item['context']['persona']['age']>=25
    assert len(item['member_guid_hash'])==32 and 'real_name' not in json.dumps(item)
    assert indices[0]['parent_id'] is not None


@pytest.mark.parametrize('channel,visible,private_visible',[
    ('guild',True,True),('discord:general',True,True),('whisper',False,True),('dm:101',False,True),
    ('officer',False,False),('discord:officers',False,False),('discord:unknown',False,False),
    ('voice:private1',False,True),('voice:officers',False,False),('voice:unknown',False,False)])
def test_channel_privacy(data,channel,visible,private_visible):
    pair(data,channel=channel)
    assert bool(mine(data)[0]) is visible
    assert bool(mine(data,include_private=True)[0]) is private_visible


@pytest.mark.parametrize('text',['Avery Example knows the route.','avery example knows the route.',
                               'avery\nexample knows the route.'])
def test_real_name_drops_the_entire_pair_even_with_overlay_flag(data,text):
    pair(data,good=text)
    assert mine(data,include_overlay=True)[0]==[]


def test_real_name_in_context_also_drops_pair(data):
    pair(data)
    with data.store.transaction():
        data.store.conn.execute("UPDATE chat_line SET text='Avery Example asked this.' WHERE from_guid=1")
    assert mine(data)[0]==[]


def test_explicit_filter_requires_owner_flag(data):
    pair(data,good='She discusses sexual details.')
    assert mine(data)[0]==[]
    assert len(mine(data,include_overlay=True)[0])==1


def test_private_context_excludes_other_pairs_and_media(data):
    pair(data,channel='whisper')
    add(data,'another pair private fact',channel='whisper',ts=data.before-1,guid=102,to=1)
    add(data,'discord private fact',channel='dm:101',ts=data.before-1,guid=1,to=101,origin='human')
    result=mine(data,include_private=True)[0]
    assert len(result)==1
    assert 'private fact' not in json.dumps(result)


def test_reward_pairs_require_matching_stored_parent_vectors(data):
    ids=pair(data)
    with data.store.transaction():
        data.store.conn.execute('DELETE FROM talk_signal')
        weight=ensure(data.store.conn,now=data.before)
        for i,lid in enumerate(ids):
            data.store.conn.execute('INSERT INTO talk_reward(line_id,guid,value,parts,weights_version,scored_at) '
                'VALUES (?,101,?,\'{}\',?,?)',(lid,1 if i==0 else -.5,weight.version,data.before+300))
    assert mine(data)[0]==[]
    with data.store.transaction():
        for lid in ids:
            row=data.store.conn.execute('SELECT * FROM chat_line WHERE id=?',(lid-1,)).fetchone()
            capture.vectors(data.store.conn,[NS(id=row['id'],text=row['text'])],
                            {row['id']:pack_vec([1.]+[0.]*511)},'fixture-model')
    result=mine(data)[0]
    assert len(result)==1 and result[0]['source']=='reward'
    with data.store.transaction():
        data.store.conn.execute("UPDATE learn_line_vector SET model_hash='other-space' WHERE line_id=?",(ids[1]-1,))
    assert mine(data)[0]==[]


def test_capture_needs_confirmed_rewrite_and_turn_ids_can_restart(rig):
    rig.engine.learn_enabled=True
    rig.engine.learn_capture_preferences=True
    turn=Turn(TurnKind.REPLY_HUMAN,101,'guild')
    p=rig.engine.personas.get(101)
    first=TurnMeta('t1',guard_rewritten=True,guard_verdict='rewrite',preference_original='old draft')
    ids=[]
    for text in ('new first','new followup'):
        first.words=text
        ids.append(rig.engine._record(turn,first,p,text,[]))
    later=TurnMeta('t1',guard_rewritten=True,guard_verdict='rewrite',preference_original='different old draft',words='different new text')
    last=rig.engine._record(turn,later,p,'different new text',[])
    rows=rig.store.conn.execute('SELECT * FROM learn_rewrite ORDER BY first_line_id').fetchall()
    assert len(rows)==2 and [r['first_line_id'] for r in rows]==[ids[0],last]
    row=rig.store.conn.execute('SELECT * FROM chat_line WHERE id=?',(ids[0],)).fetchone()
    assert dataset.body(rig.store.conn,row)=='new first new followup'
    # Failed-open fallback is not a preference judgment in favor of a rewrite.
    failed=TurnMeta('t2',guard_rewritten=True,guard_verdict='failed_open',preference_original='old')
    rig.engine._record(turn,failed,p,'fallback',[])
    assert rig.store.conn.execute('SELECT count(*) FROM learn_rewrite').fetchone()[0]==2


def test_guard_pairs_are_mined_only_with_a_sent_rewrite_receipt(rig):
    rig.engine.learn_enabled=True
    rig.engine.learn_capture_preferences=True
    meta=TurnMeta('t1',guard_rewritten=True,guard_verdict='rewrite',preference_original='Original fixture answer.',words='Better fixture answer.')
    turn=Turn(TurnKind.REPLY_HUMAN,101,'guild')
    lid=rig.engine._record(turn,meta,rig.engine.personas.get(101),'Better fixture answer.',[])
    result=dataset.mine(rig.store.conn,rig.engine.human,since=0,until=rig.clock.now()+1)[0]
    assert len(result)==1 and result[0]['source']=='guard_rewrite'
    with rig.store.transaction():
        rig.store.conn.execute("UPDATE utterance SET dropped='guard_drop'")
    assert dataset.mine(rig.store.conn,rig.engine.human,since=0,until=rig.clock.now()+1)[0]==[]


def test_capture_off_writes_no_private_evidence(rig):
    rig.engine.learn_enabled=True
    meta=TurnMeta('t1',guard_rewritten=True,guard_verdict='rewrite',preference_original='old')
    rig.engine._record(Turn(TurnKind.REPLY_HUMAN,101,'guild'),meta,rig.engine.personas.get(101),'new',[])
    assert rig.store.conn.execute('SELECT count(*) FROM learn_rewrite').fetchone()[0]==0


def test_output_refuses_repository_worktree_marker(tmp_path):
    root=tmp_path/'repository'
    root.mkdir()
    (root/'.git').write_text('fixture linked worktree')
    with pytest.raises(ValueError,match='learn_output_in_repository'):
        dataset.output_dir(root/'data'/'scratch'/'private')


def test_output_refuses_a_symlink_into_a_repository(tmp_path):
    root=tmp_path/'repo'
    root.mkdir()
    (root/'.git').mkdir()
    alias=tmp_path/'alias'
    try:
        alias.symlink_to(root,target_is_directory=True)
    except OSError:
        pytest.skip('creating symlinks requires host permission')
    with pytest.raises(ValueError,match='learn_output_in_repository'):
        dataset.output_dir(alias/'private')


def test_followup_vote_and_burst_crossing_holdout_boundary(data):
    first,other=pair(data)
    fragment=add(data,'follow-up clause',ts=data.before+2)
    with data.store.transaction():
        data.store.conn.execute('CREATE TABLE IF NOT EXISTS talk_recipe_line(line_id INTEGER PRIMARY KEY,recipe_line_id INTEGER)')
        data.store.conn.execute("INSERT OR IGNORE INTO talk_recipe(line_id,template_id,template_version,dials_hash,"
                                "model_id,guard_verdict) VALUES (?,'t',?,?,'m','not_run')",(first,'0'*16,'0'*64))
        data.store.conn.executemany('INSERT OR REPLACE INTO talk_recipe_line VALUES (?,?)',[(first,first),(fragment,first)])
        data.store.conn.execute('DELETE FROM talk_signal WHERE line_id=?',(first,))
        feedback(data.store.conn,dict(line_id=fragment,value=1,reasons=[]),1,data.before+3)
    pairs=mine(data)[0]
    assert len(pairs)==1 and pairs[0]['chosen'].endswith('follow-up clause')
    with data.store.transaction():
        data.store.conn.execute('UPDATE chat_line SET ts=? WHERE id=?',(data.now-2*dataset.DAY,fragment))
    assert mine(data)[0]==[]


def test_stale_source_and_foreign_embedding_are_rejected_by_scorer(data):
    pair(data)
    _,indices,_=mine(data)
    candidate=dict(id=indices[0]['id'],text='A different answer.',embedding=[1.]+[0.]*511,model_hash='wrong')
    with pytest.raises(ValueError,match='learn_candidate_embedding_invalid'):
        offline_score.score(data.store.conn,indices,[candidate],owner=1,model_hash='right')
    with data.store.transaction():
        data.store.conn.execute('UPDATE chat_line SET text=? WHERE id=?',('edited',indices[0]['line_id']))
    with pytest.raises(ValueError,match='learn_eval_source_changed'):
        offline_score.score(data.store.conn,indices,[dict(id=indices[0]['id'],text='answer')],owner=1,model_hash='right')


@pytest.mark.parametrize('change',['parent_text','parent_audience','source_channel','source_audience'])
def test_changed_context_and_scope_are_refused(data,change):
    pair(data)
    _,indices,_=mine(data)
    case=indices[0]
    statements={
        'parent_text':("UPDATE chat_line SET text='edited parent' WHERE id=?",case['parent_id']),
        'parent_audience':("UPDATE chat_line SET audience='[1,102]' WHERE id=?",case['parent_id']),
        'source_channel':("UPDATE chat_line SET channel='say' WHERE id=?",case['line_id']),
        'source_audience':("UPDATE chat_line SET audience='[1,102]' WHERE id=?",case['line_id']),
    }
    sql,lid=statements[change]
    with data.store.transaction():
        data.store.conn.execute(sql,(lid,))
    with pytest.raises(ValueError,match='learn_eval_source_changed'):
        offline_score.score(data.store.conn,indices,[dict(id=case['id'],text='answer')],owner=1,model_hash='right')


@pytest.mark.parametrize('channel',['guild','whisper','discord:general','dm:101','voice:private1'])
def test_scorer_replays_question_in_its_own_medium(data,channel):
    pair(data,channel=channel)
    _,indices,_=mine(data,include_private=True)
    result=offline_score.score(data.store.conn,indices,[dict(id=indices[0]['id'],text='lol')],
                               owner=1,model_hash='fixture',include_private=True)
    assert 'filler_after_question' in result['results'][0]['codes']


def test_scorer_reports_semantic_relevance_with_supplied_matching_vectors(data):
    pair(data)
    _,indices,_=mine(data)
    parent=data.store.conn.execute('SELECT * FROM chat_line WHERE id=?',(indices[0]['parent_id'],)).fetchone()
    with data.store.transaction():
        capture.vectors(data.store.conn,[NS(id=parent['id'],text=parent['text'])],
                        {parent['id']:pack_vec([1.]+[0.]*511)},'fixture')
    candidate=dict(id=indices[0]['id'],text='The clouds above the mountain are bright today.',
                   embedding=[0.,1.]+[0.]*510,model_hash=capture.digest('fixture'))
    result=offline_score.score(data.store.conn,indices,[candidate],owner=1,model_hash=capture.digest('fixture'))
    assert result['coverage']=={'candidate_embeddings':1,'parent_embeddings':1}
    assert 'low_parent_and_thread_relevance' in result['results'][0]['codes']


def test_snapshot_never_changes_source_or_retains_copy(data,tmp_path):
    before=(data.root/'mind.db').read_bytes()
    with dataset.snapshot(data.root,tmp_path) as conn:
        conn.execute("UPDATE meta SET value='changed' WHERE key='human_guid'")
        conn.commit()
    assert (data.root/'mind.db').read_bytes()==before
    assert not list(tmp_path.glob('.learn-snapshot-*'))


def test_export_excludes_last_week_evalset_holds_it_and_scorer_uses_no_models(data,tmp_path,monkeypatch,capsys):
    pair(data)
    pair(data,offset=18*dataset.DAY,good='Follow the river beyond the bridge.',bad='Wait in the courtyard.')
    monkeypatch.setattr('leeroy_mind.learn.preference_cli.time.time',lambda:data.now)
    export=tmp_path/'training'
    held=tmp_path/'heldout'
    before=data.store.conn.total_changes
    assert learn_main(['export','--data-dir',str(data.root),'--out',str(export)])==0
    assert learn_main(['evalset','--data-dir',str(data.root),'--out',str(held)])==0
    training=list(offline_score.read_jsonl(export/'pairs.jsonl'))
    cases=list(offline_score.read_jsonl(held/'pairs.jsonl'))
    assert len(training)==len(cases)==1
    assert training[0]['chosen']!=cases[0]['chosen']
    candidates=tmp_path/'candidate.jsonl'
    dataset.write_jsonl(candidates,[dict(id=cases[0]['id'],text='lol')])
    capsys.readouterr()
    assert learn_main(['score','--data-dir',str(data.root),'--evalset',str(held),'--candidates',str(candidates)])==0
    report=json.loads(capsys.readouterr().out)
    assert report['cases']==1 and 'filler_after_question' in report['results'][0]['codes']
    assert report['coverage']['candidate_embeddings']==0
    assert data.store.conn.total_changes==before


def test_export_overlay_warning_and_no_text_in_logs(data,tmp_path,monkeypatch,capsys,caplog):
    pair(data,good='She discusses sexual details.')
    monkeypatch.setattr('leeroy_mind.learn.preference_cli.time.time',lambda:data.now)
    assert learn_main(['export','--data-dir',str(data.root),'--out',str(tmp_path/'explicit'),
                       '--include-overlay-content'])==0
    output=capsys.readouterr()
    assert 'Warning' in output.err and 'sexual details' not in output.out+output.err+caplog.text
