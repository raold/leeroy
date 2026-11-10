import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import json
import logging
import sqlite3

import pytest

from conftest import make_persona, seed_test_cast, unit
from leeroy_mind.claims.store import add
from leeroy_mind.eval.talk import repetition, unawareness
from leeroy_mind.eval.talk.common import similarity
from leeroy_mind.guards import Draft, GuardsCfg, GuardStores, HistoryLine, judge_relevance, record_sent, review
from leeroy_mind.guards.calibration import Case, measure
from leeroy_mind.personas.io import save_persona
from leeroy_mind.storage.rows import pack_vec
from leeroy_mind.testing.fake_llm import FakeLLM, fake_embed
from leeroy_mind.nightly.llm import Reasoner

NOW = datetime(2026,10,29,23,tzinfo=timezone.utc).timestamp()


def draft(text='A fresh thought about the invented ferry', **kw):
    return Draft(101,kw.pop('channel','guild'),text,now=kw.pop('now',NOW),**kw)


def sources(store, **kw):
    return GuardStores(store.conn,config=kw.pop('config',GuardsCfg(enabled=True)),**kw)


def line(store,text,*,key=1,guid=101,channel='guild',to=0,ts=NOW-1,audience=None):
    with store.transaction():
        store.conn.execute('INSERT INTO chat_line(id,ts,channel,from_guid,from_name,to_guid,text,origin,audience) '
            'VALUES (?,?,?,?,?,?,?, ?,?)',(key,ts,channel,guid,'Invented',to,text,
            'human' if guid==1 else 'mind',json.dumps(audience) if audience is not None else None))
    return key


def gag(store,term='brass banana',key=7):
    with store.transaction():
        store.conn.execute("INSERT INTO lexicon(id,kind,term,origin_ref,originator,born,last_used) "
            "VALUES (?,'joke',?,'chat:1',101,?,?)",(key,term,NOW-100,NOW-100))
        store.conn.execute('INSERT INTO lexicon_adopter(term,guid,first_use) VALUES (?,101,?)',(key,NOW-100))


def test_off_means_send_without_reading_or_validating_anything():
    stores=GuardStores(None)
    assert review(stores,Draft(0,'invalid',None)).action=='send'
    assert not record_sent(stores,Draft(0,'invalid',None),receipt_id='')


def test_new_content_sends_and_empty_drops(store):
    assert review(sources(store),draft()).action=='send'
    assert review(sources(store),draft('  ')).codes==('empty',)


@pytest.mark.parametrize('room,readers,expected', [
    ('guild_hall', (101,102), 'drop'), ('guild_hall', (102,), 'send'),
    ('guild_hall', None, 'send'), ('healers', (101,102), 'send'),
    ('officers', (101,102), 'send')])
def test_spoken_history_requires_same_room_and_captured_reader(store,room,readers,expected):
    text='The copper lantern hangs above the doorway'
    line(store,text,channel='voice:'+room,audience=readers)
    result=review(sources(store),draft(text,channel='voice',room='guild_hall'))
    assert result.action == expected
    assert review(sources(store),draft(text)).action == 'send'


def test_officers_voice_room_requires_officer_role_even_with_audience(store):
    text='The copper lantern hangs above the doorway'
    line(store,text,channel='voice:officers',audience=(101,102))
    assert review(sources(store),draft(text,channel='voice',room='officers')).action == 'send'
    seed_test_cast(store)
    p=make_persona(101,'Arkon',guild_role='officer')
    save_persona(store,p,source='seed',ts=NOW,race='Orc',class_name='Warrior')
    assert review(sources(store),draft(text,channel='voice',room='officers')).action == 'drop'


@pytest.mark.parametrize('channel,to,expected',[
    ('guild',0,'own_repeat_same_channel'),('say',0,'own_repeat_cross_channel'),
    ('whisper',1,None),('officer',0,None)])
def test_own_repetition_crosses_public_channels_but_not_private_boundaries(store,channel,to,expected):
    text='The copper lantern hangs above the doorway'
    line(store,text,channel=channel,to=to)
    result=review(sources(store),draft(text))
    assert result.action==('drop' if expected else 'send')
    assert (expected in result.codes) if expected else not result.codes
    assert text not in repr(result)


def test_whisper_history_stays_with_its_pair(store):
    text='The copper lantern hangs above the doorway'
    line(store,text,channel='whisper',to=1)
    assert review(sources(store),draft(text,channel='whisper',to_guid=1)).action=='drop'
    assert review(sources(store),draft(text,channel='whisper',to_guid=102)).action=='send'


def test_officer_history_needs_stored_role_and_current_officer_turn(store):
    seed_test_cast(store)
    text='The copper lantern hangs above the doorway'
    line(store,text,channel='officer')
    assert review(sources(store),draft(text,channel='officer')).action=='send'
    save_persona(store,make_persona(101,'Arkon',guild_role='officer'),race='Human',class_name='Warrior',source='seed',ts=NOW)
    assert review(sources(store),draft(text,channel='officer')).action=='drop'
    assert review(sources(store),draft(text)).action=='send'


def test_repeat_window_and_future_rows(store):
    text='The copper lantern hangs above the doorway'
    line(store,text,ts=NOW-10)
    cfg=GuardsCfg(enabled=True,repeat_seconds=10)
    assert review(sources(store,config=cfg),draft(text)).action=='drop'
    assert review(sources(store,config=cfg),draft(text,now=NOW+.001)).action=='send'
    assert review(sources(store),draft(text,now=NOW-11)).action=='send'


@pytest.mark.parametrize('kind', ['filler','relevance','nothing_new','evening_phrase'])
def test_short_own_repeat_window_preserves_other_guard_windows(store,kind):
    cfg=GuardsCfg(enabled=True,semantic_enabled=True,repeat_seconds=10)
    if kind=='filler':
        line(store,'Where is the ferry?',guid=1,ts=NOW-20)
        d=draft('lol',parent_id=1)
        expected='filler_after_question'
    elif kind=='relevance':
        line(store,'Where is the ferry?',guid=1,ts=NOW-20)
        d=draft('Purple planets orbit silent stars',parent_id=1,embedding=pack_vec(unit(1)))
        expected='low_parent_and_thread_relevance'
    elif kind=='nothing_new':
        text='The copper lantern hangs above the doorway'
        line(store,text,guid=102,ts=NOW-20)
        d=draft(text,thread_ids=(1,))
        expected='nothing_new'
    else:
        line(store,'Keep your muddy boots outside the doorway',guid=102,ts=NOW-20)
        line(store,'Keep your muddy boots outside the bedroom',key=2,guid=103,ts=NOW-19)
        d=draft('Keep your muddy boots outside the kitchen')
        expected='guild_evening_phrase'
    result=review(sources(store,config=cfg,vectors={1:pack_vec(unit(0))}),d)
    assert expected in result.codes


def test_semantic_repeat_needs_separate_opt_in_and_honors_threshold_boundary(store):
    line(store,'The small vessel arrived at the harbour')
    vec=pack_vec(unit(0))
    d=draft('A tiny boat reached its final destination',embedding=vec)
    assert review(sources(store,vectors={1:vec}),d).action=='send'
    cfg=GuardsCfg(enabled=True,semantic_enabled=True,repeat_cosine=1.0)
    assert review(sources(store,config=cfg,vectors={1:vec}),d).action=='drop'
    assert review(sources(store,config=cfg,vectors={1:pack_vec(unit(1))}),d).action=='send'


def test_overlap_override_uses_shared_detector_score(store):
    earlier='copper lantern hangs above doorway'
    later='copper lantern hangs above window'
    line(store,earlier)
    overlap=repetition.shingle_overlap(repetition.ngrams(tuple(earlier.split())),repetition.ngrams(tuple(later.split())))
    assert review(sources(store,config=GuardsCfg(enabled=True,repeat_overlap=overlap)),draft(later)).action=='drop'
    assert review(sources(store,config=GuardsCfg(enabled=True,repeat_overlap=overlap+.001)),draft(later)).action=='send'


def test_shared_evening_phrase_requires_two_other_members_and_exempts_stop_phrases(store):
    line(store,'Keep your muddy boots outside the doorway',guid=102)
    d=draft('Keep your muddy boots outside the kitchen')
    assert review(sources(store),d).action=='send'
    line(store,'Keep your muddy boots outside the bedroom',key=2,guid=103)
    assert 'guild_evening_phrase' in review(sources(store),d).codes
    assert review(sources(store),replace(d,now=NOW+86400)).action=='send'
    line(store,'What do you think about the plan',key=3,guid=104)
    line(store,'What do you think about the plan',key=4,guid=105)
    assert review(sources(store),draft('What do you think about the plan')).action=='send'


def test_discord_evening_phrase_respects_room_and_captured_readers(store):
    first=HistoryLine(77,NOW-2,102,'Keep your muddy boots outside the doorway',
                      channel='discord',room='general',readers=(101,102,103))
    second=replace(first,id=78,guid=103,text='Keep your muddy boots outside the bedroom')
    d=draft('Keep your muddy boots outside the kitchen',channel='discord',room='general')
    assert review(sources(store,history=(first,second)),d).codes==('guild_evening_phrase',)
    for hidden in (replace(second,room='other'),replace(second,readers=(102,103)),
                   replace(second,channel='dm',to_guid=102)):
        assert review(sources(store,history=(first,hidden)),d).action=='send'


@pytest.mark.parametrize('text,action',[('lol','react'),('haha lol','react'),('yes','send'),('no','send'),('The ferry departs beside the northern pier','send')])
def test_filler_after_real_question_and_clean_answers(store,text,action):
    line(store,'Where does the ferry depart?',guid=1,to=101)
    result=review(sources(store),draft(text,parent_id=1,thread_ids=(1,)))
    assert result.action==action
    assert result.reaction==('😄' if action=='react' else None)


def test_filler_without_parent_or_question_does_not_invent_answer_obligation(store):
    assert review(sources(store),draft('lol')).action=='send'
    assert review(sources(store),draft('lol',real_answer_due=True)).action=='react'
    line(store,'The ferry has arrived',guid=1,to=101)
    assert review(sources(store),draft('lol',parent_id=1)).action=='send'
    assert review(sources(store),draft('lol',parent_id=1,real_answer_due=True)).action=='react'


def test_no_new_content_drops_only_with_explicit_visible_thread(store):
    text='The ferry departs beside the northern pier'
    line(store,text,guid=102)
    assert review(sources(store),draft(text)).action=='send'
    assert review(sources(store),draft(text,thread_ids=(1,))).codes==('nothing_new',)
    assert review(sources(store),draft(text,thread_ids=(1,),now=NOW+301)).action=='send'


def test_relevance_thresholds_are_strict_and_thread_can_explain_answer(store):
    line(store,'What did the northern ferryman say?',guid=1)
    line(store,'The stars look unusually bright tonight',key=2,guid=102)
    zero,other=pack_vec(unit(0)),pack_vec(unit(1))
    d=draft('The stars look especially bright tonight',parent_id=1,thread_ids=(1,),embedding=other)
    cfg=GuardsCfg(enabled=True,semantic_enabled=True,parent_cosine=0,thread_cosine=0)
    assert review(sources(store,config=cfg,vectors={1:zero}),d).action=='send'
    cfg=replace(cfg,parent_cosine=.001,thread_cosine=.001)
    assert review(sources(store,config=cfg,vectors={1:zero}),d).codes==('low_parent_and_thread_relevance',)
    # A related thread vector prevents the non-sequitur finding; a repeated thought
    # may still independently be dropped as adding nothing new.
    result=review(sources(store,config=cfg,vectors={1:zero,2:other}),replace(d,thread_ids=(1,2)))
    assert 'low_parent_and_thread_relevance' not in result.codes


def test_separate_parent_survives_full_thread_without_cached_thread_vectors(store):
    line(store,'What did the northern ferryman say?',guid=1,ts=NOW-20)
    for key in range(2,18):
        line(store,'A separate unembedded thread observation',key=key,guid=102,ts=NOW-18+key)
    d=draft('Purple planets orbit silent stars',parent_id=1,thread_ids=tuple(range(2,18)),
            embedding=pack_vec(unit(1)))
    s=sources(store,config=GuardsCfg(enabled=True,semantic_enabled=True),vectors={1:pack_vec(unit(0))})
    assert review(s,d).codes==('low_parent_and_thread_relevance',)


def test_discord_and_dm_need_captured_audience_and_matching_room(store):
    parent=HistoryLine(77,NOW-1,102,'Where is the ferry?',channel='discord',room='room7',readers=(101,102))
    d=draft('lol',channel='discord',room='room7',parent_id=77)
    assert review(sources(store,history=(parent,)),d).action=='react'
    for hidden in (replace(parent,readers=None),replace(parent,readers=(102,)),replace(parent,room='room8')):
        assert review(sources(store,history=(hidden,)),d).action=='send'
    dm=replace(parent,channel='dm',to_guid=101)
    assert review(sources(store,history=(dm,)),replace(d,channel='dm',to_guid=102)).action=='react'
    assert review(sources(store,history=(dm,)),replace(d,channel='dm',to_guid=103)).action=='send'


def test_gag_ration_records_only_sent_receipts_once_and_resets_by_local_day(store):
    gag(store)
    d=draft('brass banana')
    s=sources(store,config=GuardsCfg(enabled=True,gag_caps={7:1}))
    assert review(s,d).action=='send'
    assert review(s,d).action=='send'  # merely considering a draft spends nothing
    with store.transaction():
        assert record_sent(s,d,receipt_id='delivery1')
        assert not record_sent(s,d,receipt_id='delivery1')
    result=review(s,d)
    assert result.action=='rewrite' and result.codes==('gag_ration',)
    assert result.related[0].id==7
    assert review(s,replace(d,now=NOW+86400)).action=='send'
    assert review(s,replace(d,guid=102)).action=='send'
    raw=store.conn.execute("SELECT value FROM meta WHERE key LIKE 'guards:sent:%'").fetchone()[0]
    assert d.text not in raw and 'delivery1' not in raw


def test_stock_phrase_rations_share_across_channels_and_survive_reopened_sources(store):
    s=sources(store,config=GuardsCfg(enabled=True,stock_daily_cap=2))
    for number,text in enumerate(('Copper lantern fans love harbours','Copper lantern fans adore boats')):
        with store.transaction():
            record_sent(s,draft(text),receipt_id=f'sent{number}')
    d=draft('Copper lantern fans enjoy sailing',channel='game',room='say')
    result=review(replace(s),d)
    assert result.action=='rewrite' and 'stock_phrase_ration' in result.codes


def test_ration_uses_local_midnight_and_both_fall_back_hours_share_one_day(store):
    gag(store)
    s=sources(store,config=GuardsCfg(enabled=True,gag_daily_cap=1))
    first=datetime(2026,11,1,5,30,tzinfo=timezone.utc).timestamp()
    with store.transaction():
        record_sent(s,draft('brass banana',now=first),receipt_id='first_hour')
    assert review(s,draft('brass banana',now=first+3600)).action=='rewrite'
    midnight=datetime(2026,11,2,5,tzinfo=timezone.utc).timestamp()
    assert review(s,draft('brass banana',now=midnight)).action=='send'


def test_delivery_retry_requires_fixed_timestamp_and_does_not_charge_next_day(store):
    from types import SimpleNamespace
    before=datetime(2026,11,2,4,59,tzinfo=timezone.utc).timestamp()
    clock=SimpleNamespace(now=lambda:before)
    s=sources(store,clock=clock)
    d=draft(now=None)
    with pytest.raises(ValueError,match='guard_delivery_time_required'):
        record_sent(s,d,receipt_id='same_delivery')
    delivered=replace(d,now=before)
    with store.transaction():
        assert record_sent(s,delivered,receipt_id='same_delivery')
    clock.now=lambda:before+120
    with store.transaction():
        assert not record_sent(s,delivered,receipt_id='same_delivery')
    assert store.conn.execute("SELECT COUNT(*) FROM meta WHERE key LIKE 'guards:sent:%'").fetchone()[0]==1


def test_corrupt_receipts_cannot_reset_a_ration_or_leak_stored_content(store):
    from leeroy_mind.guards.review import _receipt_key
    d=draft()
    store.set_meta(_receipt_key(d,NOW),'PRIVATE_CORRUPT_RECEIPT')
    result=review(sources(store),d)
    assert result.action=='rewrite' and 'ration_unavailable' in result.codes
    assert 'PRIVATE' not in repr(result)
    with pytest.raises(ValueError,match='guard_receipt_invalid'):
        record_sent(sources(store),d,receipt_id='never_written')


def test_receipt_write_joins_rollback_and_rejects_reused_identity_with_different_payload(store):
    s=sources(store)
    with pytest.raises(RuntimeError),store.transaction():
        record_sent(s,draft(),receipt_id='rolled_back')
        raise RuntimeError()
    assert not store.conn.execute("SELECT 1 FROM meta WHERE key LIKE 'guards:sent:%'").fetchone()
    with store.transaction():
        record_sent(s,draft(),receipt_id='sent')
        with pytest.raises(ValueError,match='guard_receipt_conflict'):
            record_sent(s,draft('Completely unrelated words stand over here'),receipt_id='sent')


def test_topic_memory_is_scored_but_a_new_fact_on_same_topic_is_allowed(store):
    with store.transaction():
        store.conn.execute("INSERT INTO talk_move(ts,guid,counterpart,channel,topics) VALUES (?,101,1,'guild','[\"ferry\"]')",(NOW-1,))
    result=review(sources(store),draft('The ferry now leaves an hour earlier'))
    assert result.action=='send'
    assert any(score.code=='recent_topic_uses' and score.value==1 for score in result.scores)


def test_claims_reuse_existing_check_only_when_enabled_and_keep_private_provenance(store):
    vec=pack_vec(unit(0))
    line(store,'I love Saurfang',channel='whisper',to=1)
    ident=add(store,101,kind='opinion',subject='saurfang',text='I love Saurfang',polarity=1,
              embedding=vec,source_line=1,now=NOW-1)
    d=draft('I hate Saurfang',embedding=vec,channel='whisper',to_guid=1)
    assert review(sources(store),d).action=='send'
    result=review(sources(store,claims_enabled=True),d)
    assert result.action=='rewrite' and result.related[0].id==ident
    assert 'Saurfang' not in repr(result)
    assert review(sources(store,claims_enabled=True),replace(d,channel='guild',to_guid=0)).action=='send'


def test_optional_world_facts_use_shared_positive_evidence_checker(store):
    facts=unawareness.Facts({'invented boss':[(NOW-1,7)]},{},{},{})
    result=review(sources(store,facts=facts),draft('We have never killed invented boss.'))
    assert result.action=='rewrite' and result.codes==('never_killed_after_verified_kill',)
    assert review(sources(store,facts=facts),draft('We have never killed invented boss.',now=NOW-2)).action=='send'


@pytest.mark.parametrize('source_channel,target_channel,peer,expected', [
    ('whisper','guild',0,False), ('whisper','whisper',1,True),
    ('whisper','whisper',102,False), ('whisper','discord',0,False),
    ('dm:101','whisper',1,False), ('dm:101','dm',1,True),
    ('discord:general','guild',0,False), ('discord:general','discord',0,True),
    ('officer','guild',0,False)])
def test_owner_claim_guard_requires_visible_provenance(store, source_channel, target_channel, peer, expected):
    vec = pack_vec(unit(0))
    line(store, 'I love Saurfang', channel=source_channel,
         to=1 if source_channel in {'whisper','dm:101'} else 0, audience=[1,101])
    add(store,101,kind='opinion',subject='saurfang',text='I love Saurfang',polarity=1,
        embedding=vec,source='owner',source_line=1,now=NOW-1)
    room = '101' if target_channel == 'dm' else 'general' if target_channel == 'discord' else ''
    result = review(sources(store,claims_enabled=True),
        draft('I hate Saurfang',embedding=vec,channel=target_channel,to_guid=peer,room=room))
    assert ('claim_conflict' in result.codes) is expected


def test_unscoped_owner_claim_never_reaches_a_guard(store):
    vec = pack_vec(unit(0))
    add(store,101,kind='opinion',subject='saurfang',text='I love Saurfang',polarity=1,
        embedding=vec,source='owner',now=NOW-1)
    assert review(sources(store,claims_enabled=True),draft('I hate Saurfang',embedding=vec)).action == 'send'


def test_receipt_pruning_preserves_other_meta_and_joins_rollback(store):
    store.set_meta('guards:sent:2026-01-01:101', '{}')
    store.set_meta('guards:sent:2026-10-29:101', '{}')
    store.set_meta('other:2026-01-01', '{}')
    with pytest.raises(RuntimeError), store.transaction():
        record_sent(sources(store), draft(), receipt_id='line-1')
        assert store.get_meta('guards:sent:2026-01-01:101') is None
        raise RuntimeError('rollback')
    assert store.get_meta('guards:sent:2026-01-01:101') == '{}'
    with store.transaction():
        record_sent(sources(store), draft(), receipt_id='line-1')
    assert store.get_meta('guards:sent:2026-01-01:101') is None
    assert store.get_meta('guards:sent:2026-10-29:101') is not None
    assert store.get_meta('other:2026-01-01') == '{}'


def test_older_database_reads_no_schema_and_never_commits(tmp_path,caplog):
    path=tmp_path/'old.db'
    sqlite3.connect(path).close()
    before=path.read_bytes()
    conn=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)
    try:
        assert review(GuardStores(conn,config=GuardsCfg(enabled=True)),draft()).action=='send'
        assert not caplog.text
    finally:
        conn.close()
    assert path.read_bytes()==before


def test_fast_path_is_read_only_bounded_deterministic_and_logs_no_text(store,caplog):
    s=sources(store)
    statements=[]
    store.conn.set_trace_callback(statements.append)
    try:
        with caplog.at_level(logging.DEBUG):
            first=review(s,draft('PRIVATE_CANDIDATE must stay out of logs'))
        assert all(sql.startswith('SELECT') for sql in statements) and len(statements)<=8
        assert not caplog.text and 'PRIVATE_CANDIDATE' not in repr(first)
        assert first==review(s,draft('PRIVATE_CANDIDATE must stay out of logs'))
    finally:
        store.conn.set_trace_callback(None)


def test_historical_candidate_excludes_itself_and_later_rows(store):
    text='A copper lantern hangs above the doorway'
    line(store,text,ts=NOW)
    line(store,text,key=2,ts=NOW)
    assert review(sources(store),draft(text,line_id=1)).action=='send'
    assert review(sources(store),draft(text,line_id=2)).action=='drop'


@pytest.mark.parametrize('answer,action',[(True,'send'),(False,'rewrite')])
def test_optional_relevance_judge_calls_fake_model_only_on_request(store,answer,action):
    line(store,'Where is the ferry?',guid=1)
    s=sources(store,config=GuardsCfg(enabled=True,semantic_enabled=True),vectors={1:pack_vec(unit(0))})
    d=draft('Purple planets orbit silent stars',parent_id=1,embedding=pack_vec(unit(1)))
    fake=FakeLLM(lambda *_:json.dumps({'relevant':answer}))
    reasoner=Reasoner(fake,'fake')
    assert review(s,d).action=='rewrite' and not fake.calls
    result=asyncio.run(judge_relevance(reasoner,s,d))
    assert result.action==action and len(fake.calls)==1


def test_relevance_judge_cannot_clear_other_guards_or_approve_changed_context(store):
    line(store,'Where is the ferry?',guid=1)
    text='Purple planets orbit silent stars'
    line(store,text,key=2)
    s=sources(store,config=GuardsCfg(enabled=True,semantic_enabled=True),vectors={1:pack_vec(unit(0))})
    d=draft(text,parent_id=1,embedding=pack_vec(unit(1)))
    reasoner=Reasoner(FakeLLM(lambda *_:'{"relevant":true}'),'fake')
    result=asyncio.run(judge_relevance(reasoner,s,d))
    assert result.action=='drop' and 'own_repeat_same_channel' in result.codes
    def changed(*_):
        with store.transaction():
            store.conn.execute("UPDATE chat_line SET text='A changed question?' WHERE id=1")
        return '{"relevant":true}'
    result=asyncio.run(judge_relevance(Reasoner(FakeLLM(changed),'fake'),s,d))
    assert result.action=='rewrite' and 'relevance_context_changed' in result.codes


def test_optional_judge_error_is_content_free_and_stays_a_rewrite(store,caplog):
    line(store,'Where is the ferry?',guid=1)
    s=sources(store,config=GuardsCfg(enabled=True,semantic_enabled=True),vectors={1:pack_vec(unit(0))})
    d=draft('Purple planets orbit silent stars',parent_id=1,embedding=pack_vec(unit(1)))
    def fail(*_):
        raise ValueError('PRIVATE_JUDGE_ERROR')
    result=asyncio.run(judge_relevance(Reasoner(FakeLLM(fail),'fake'),s,d))
    assert result.action=='rewrite' and 'relevance_judge_unavailable' in result.codes
    assert 'PRIVATE' not in repr(result) and not caplog.text


def test_calibration_counts_are_reproducible_and_unlabeled_is_not_a_false_positive_estimate(store):
    text='The copper lantern hangs above the doorway'
    line(store,text)
    line(store,'Where is the ferry?',key=2,guid=1)
    cases=[Case(draft('A completely different invented answer'),True),Case(draft(text),False),
           Case(draft('lol',parent_id=2),False)]
    ticks=iter((0,.001,1,1.002,2,2.003))
    report=measure(sources(store),cases,timer=lambda:next(ticks))
    assert report['actions']=={'send':1,'drop':1,'react':1}
    assert report['flagged_per_100']==66.666667 and report['false_positive_rate']==0.0
    assert report['median_ms']==2 and report['p95_ms']==3
    unknown=measure(sources(store),[Case(draft(text))])
    assert unknown['false_positive_rate'] is None and unknown['labeled']==0
