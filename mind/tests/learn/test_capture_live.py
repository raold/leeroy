import pytest
from guards.test_live import rig,speak,turn,source
from leeroy_mind.guards import Verdict,GuardsCfg
from leeroy_mind.talk import awareness as live
from leeroy_mind.talk.pipeline import TurnMeta


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
    rig.engine.learn_capture_preferences=True
    rig.engine.live_review.config=GuardsCfg(enabled=True,semantic_enabled=True)
    parent=source(rig,'When does the ferry leave?',who=1,to=101)
    rig.sync(rig.engine.live_review.check(turn(trigger_line=parent),TurnMeta('test'),'The ferry leaves at seven.'))
    row=rig.store.conn.execute('SELECT embedding FROM learn_line_vector WHERE line_id=?',(parent,)).fetchone()
    assert row is not None and len(row[0])==2048
