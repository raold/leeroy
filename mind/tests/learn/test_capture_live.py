import pytest
from guards.test_live import rig,speak,turn,source
from leeroy_mind.guards import Verdict,GuardsCfg
from leeroy_mind.talk import awareness as live
from leeroy_mind.talk.pipeline import TurnMeta
from leeroy_mind.learn import dataset


@pytest.mark.parametrize('enabled',[False,True])
def test_real_rewrite_capture_reuses_existing_calls_only(rig,monkeypatch,enabled):
    rig.engine.learn_enabled=True
    rig.engine.learn_capture_preferences=enabled
    calls=[]
    def checked(stores,draft):
        calls.append(draft.text)
        return Verdict('rewrite' if len(calls)==1 else 'send',('gag_daily_cap',))
    monkeypatch.setattr(live,'review',checked)
    rig.script('the ferry leaves at seven','bring a flask for the trip')
    sent,meta=speak(rig,turn())
    assert sent and len(rig.chat_calls())==2
    rows=rig.store.conn.execute('SELECT original FROM learn_rewrite').fetchall()
    assert len(rows)==int(enabled)
    if enabled:
        assert rows[0][0]=='the ferry leaves at seven'


def test_semantic_review_reuses_cached_parent_vector(rig):
    rig.engine.learn_enabled=True
    rig.engine.learn_capture_preferences=True
    rig.engine.live_review.config=GuardsCfg(enabled=True,semantic_enabled=True)
    parent=source(rig,'When does the ferry leave?',who=1,to=101)
    rig.sync(rig.engine.live_review.check(turn(trigger_line=parent),TurnMeta('test'),'The ferry leaves at seven.'))
    row=rig.store.conn.execute('SELECT embedding FROM learn_line_vector WHERE line_id=?',(parent,)).fetchone()
    assert row is not None and len(row[0])==2048


def test_owed_fallback_is_not_a_rewrite_pair(rig,monkeypatch):
    rig.store.clock=rig.clock
    rig.engine.learn_enabled=rig.engine.learn_capture_preferences=True
    calls=[]
    def checked(stores,draft):
        calls.append(draft.text)
        return Verdict('rewrite',('gag_daily_cap',) if len(calls)==1 else ('gag_daily_cap','claim_conflict'))
    monkeypatch.setattr(live,'review',checked)
    rig.script('the ferry leaves at seven','bring a flask for the trip')
    sent,meta=speak(rig,turn())
    assert sent and not meta.preference_original
    assert rig.store.conn.execute('SELECT count(*) FROM learn_rewrite').fetchone()[0]==0
    assert dataset.mine(rig.store.conn,rig.engine.human,since=0,until=rig.clock.now()+1)[0]==[]


def test_rewrite_pair_keeps_both_sides_plain(rig,monkeypatch):
    from leeroy_mind.talk.style import Styled
    rig.store.clock=rig.clock
    rig.engine.learn_enabled=rig.engine.learn_capture_preferences=True
    calls=[]
    def checked(stores,draft):
        calls.append(draft.text)
        return Verdict('rewrite' if len(calls)==1 else 'send',('gag_daily_cap',))
    monkeypatch.setattr(live,'review',checked)
    monkeypatch.setattr(rig.engine,'_style',lambda t,p,text,**kw:[Styled(text.upper()+' typo',text)])
    rig.script('the ferry leaves at seven','bring a flask for the trip')
    sent,_=speak(rig,turn())
    pairs=dataset.mine(rig.store.conn,rig.engine.human,since=0,until=rig.clock.now()+1)[0]
    assert sent==['BRING A FLASK FOR THE TRIP typo']
    assert [(p['chosen'],p['rejected']) for p in pairs]==[('bring a flask for the trip','the ferry leaves at seven')]


def test_capture_without_learning_does_not_store_vectors(rig):
    rig.engine.learn_enabled=False
    rig.engine.learn_capture_preferences=True
    rig.engine.live_review.config=GuardsCfg(enabled=True,semantic_enabled=True)
    parent=source(rig,'When does the ferry leave?',who=1,to=101)
    rig.sync(rig.engine.live_review.check(turn(trigger_line=parent),TurnMeta('test'),'The ferry leaves at seven.'))
    assert rig.store.conn.execute('SELECT count(*) FROM learn_line_vector').fetchone()[0]==0
