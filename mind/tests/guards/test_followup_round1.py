"""Adjudicated review regressions for whole bursts and the first question answer."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from .test_live import rig,source,speak,turn
from leeroy_mind.guards import Verdict
from leeroy_mind.talk import awareness,engine,realism
from leeroy_mind.talk.pipeline import TurnMeta
from leeroy_mind.talk.select import ReplyTurn
from leeroy_mind.talk.style import Styled
from leeroy_mind.talk.turns import TurnKind

FULL = "haha! the ferry leaves at seven so bring a flask and meet everyone down at the dock before the raid starts later tonight ok"


@pytest.mark.parametrize("changed",[False,True])
def test_whole_burst_rechecked_once_after_typing(rig,monkeypatch,changed):
    e = rig.engine
    e.cfg = replace(e.cfg,talk=replace(e.cfg.talk,realism=True))
    monkeypatch.setattr(realism,"BURST_RATE",1.)
    monkeypatch.setattr(realism,"CORRECTION_RATE",0.)
    monkeypatch.setattr(e,"_style",lambda *a,**kw:[Styled(FULL,FULL)])
    parent = source(rig,"anyone know when does the ferry leave?")
    seen = []
    original_review = awareness.review
    def review(stores,draft):
        seen.append(draft.text)
        return original_review(stores,draft)
    monkeypatch.setattr(awareness,"review",review)
    original_sleep = e._realistic_sleep
    inserted = False
    async def sleep(t,seconds):
        nonlocal inserted
        await original_sleep(t,seconds)
        if changed and not inserted:
            source(rig,FULL,who=102)
            inserted = True
    monkeypatch.setattr(e,"_realistic_sleep",sleep)
    rig.script(FULL)
    sent,meta = speak(rig,turn(trigger_line=parent),TurnMeta("burst",why="reply:weighted_draw"))
    assert len(seen) == 2 and all(len(s.split()) > 18 for s in seen)
    if changed:
        assert not sent and any(r["dropped"]=="guard_changed_before_send" for r in rig.utterances())
    else:
        assert len(sent) >= 2 and sent[0].lower().startswith("haha")
        assert meta.guard_verdict == "send"


@pytest.mark.parametrize("picker",["topic_fit","memory","weighted_draw"])
def test_only_first_planned_question_answer_is_owed(rig,monkeypatch,picker):
    e = rig.engine
    planned = [ReplyTurn(g,f"test-{i}",TurnKind.REPLY_HUMAN,"guild","guild") for i,g in enumerate((101,102))]
    monkeypatch.setattr(engine,"plan_reply_turns",lambda *a,**kw:planned)
    monkeypatch.setattr(engine,"share_reason_pool",lambda planned,*a:planned)
    monkeypatch.setattr(e,"_weigh",lambda *a,**kw:({101:1.,102:1.},{}))
    if picker == "topic_fit":
        e.facts = SimpleNamespace(start=lambda *a:None)
        monkeypatch.setattr(e,"_fit_replier",lambda *a,**kw:(planned,planned[0].turn_id))
        question = "where is the entrance to Icecrown Citadel?"
    else:
        if picker == "memory":
            monkeypatch.setattr(e,"_plan_lore",lambda *a:(planned,{},planned[0].turn_id))
        question = "who remembers our first raid?"
    queued = []
    monkeypatch.setattr(e,"_queue",lambda items:queued.extend(items))
    rig.line_from_him(question)
    assert len(queued) == 2
    assert queued[0][1].why == "reply:"+picker
    assert [m.first_question_answer for _,m in queued] == [True,False]
    monkeypatch.setattr(awareness,"review",lambda *a:Verdict("drop",("own_repeat_same_channel",)))
    e.facts = None
    rig.script("the ferry leaves at seven","bring a flask for the trip")
    t,m = queued[0]
    m.facts_line = 0
    sent,m = speak(rig,t,m)
    assert sent and m.guard_rewritten and len(rig.chat_calls()) == 2
    t,m = queued[1]
    m.facts_line = 0
    sent,m = speak(rig,t,m)
    assert not sent and not m.guard_rewritten and len(rig.chat_calls()) == 3


@pytest.mark.parametrize("why,owed",[("emote:wave",True),("emote_join:dance",False),("other",False)])
def test_emote_addressee_and_outgoing_target_do_not_make_bystander_owed(rig,why,owed):
    t = turn("say")
    t.kind = TurnKind.EMOTE_REACT
    assert rig.engine.live_review.owed(t,TurnMeta("test",why=why,answers_human=True,emote_target=1)) is owed


@pytest.mark.parametrize("text,expected",[("when does the ferry leave?",[True,False]),("the ferry is here",[False,False])])
def test_discord_drawn_question_keeps_first_answer_in_its_own_medium(rig,monkeypatch,text,expected):
    from tests.discord.test_discord_talk import talk_for,post
    talk = talk_for(rig)
    monkeypatch.setattr(talk,"_draw",lambda *a:[101,102])
    items = talk.plan(post(rig,"general",text))
    assert [m.first_question_answer for _,m in items] == expected
    assert all(t.channel == "discord:general" for t,_ in items)
    assert [rig.engine.live_review.owed(t,m) for t,m in items] == expected
