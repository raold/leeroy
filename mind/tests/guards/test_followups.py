"""Parked guard review items: owed replies, detached world reads, bounded cards."""
import asyncio
import threading
from dataclasses import replace

import pytest
from .test_live import rig, turn, speak
from leeroy_mind.awareness.card import Card, Part
from leeroy_mind.guards import Verdict
from leeroy_mind.talk import awareness as live
from leeroy_mind.talk.pipeline import TurnMeta
from leeroy_mind.talk.prompt import LIVE_BUDGET
from leeroy_mind.talk.turns import TurnKind
from leeroy_mind.text import estimate_tokens


@pytest.mark.parametrize("why",["reply:weighted_draw","reply:reason_pool","reply:topic_fit",
                                    "greet:weighted_draw","goodnight","discord_drawn"])
def test_public_bystander_is_not_an_owed_answer(rig,monkeypatch,why):
    monkeypatch.setattr(live,"review",lambda *a:Verdict("drop",("own_repeat_same_channel",)))
    rig.script("the ferry leaves at seven","bring a flask for the trip")
    sent,meta=speak(rig,turn(),TurnMeta(why,why=why,answers_human=True))
    assert not sent and not meta.guard_rewritten and len(rig.chat_calls())==1


def test_whisper_to_owner_is_regenerated_once_then_sent(rig,monkeypatch):
    monkeypatch.setattr(live,"review",lambda *a:Verdict("drop",("own_repeat_same_channel",)))
    rig.script("the ferry leaves at seven","bring a flask for the trip")
    sent,meta=speak(rig,turn("whisper"),TurnMeta("whisper",whisper_reply=True,answers_human=True))
    assert sent and meta.guard_rewritten and len(rig.chat_calls())==2


@pytest.mark.parametrize("channel,meta,kind,expected",[
    ("guild",TurnMeta("named",why="reply:named",answers_human=True),TurnKind.REPLY_HUMAN,True),
    ("guild",TurnMeta("push",pushback="doubt",answers_human=True),TurnKind.REPLY_HUMAN,True),
    ("discord:general",TurnMeta("named",discord_named=True,answers_human=True),TurnKind.REPLY_HUMAN,True),
    ("dm:101",TurnMeta("dm",answers_human=True),TurnKind.REPLY_HUMAN,True),
    ("say",TurnMeta("emote",why="emote:wave"),TurnKind.EMOTE_REACT,True),
    ("whisper",TurnMeta("idle",initiates=True),TurnKind.OPEN,False),
])
def test_owed_scope(rig,channel,meta,kind,expected):
    t=turn(channel)
    t.kind=kind
    assert rig.engine.live_review.owed(t,meta) is expected


@pytest.mark.parametrize("channel",["say","officer","discord:general"])
@pytest.mark.parametrize("operation",["card","guard"])
def test_worker_never_reads_live_world_during_concurrent_change(rig,monkeypatch,channel,operation):
    e=rig.engine
    e.cfg=replace(e.cfg,talk=replace(e.cfg.talk,aware=True))
    owner=threading.get_ident()
    class LiveMembers(dict):
        def get(self,*args):
            assert threading.get_ident()==owner,"worker read live members"
            return super().get(*args)
    e.world.members=LiveMembers(e.world.members)
    entered,release=threading.Event(),threading.Event()
    original_read=e.live_review.read
    seen=[]
    for name in ("audience","is_officer"):
        original=getattr(e.world,name)
        def checked(*args,_fn=original,**kwargs):
            assert threading.get_ident()==owner,"worker read live world"
            return _fn(*args,**kwargs)
        monkeypatch.setattr(e.world,name,checked)
    async def paused_read(question):
        def delayed(conn):
            entered.set()
            assert release.wait(3)
            return question(conn)
        return await original_read(delayed)
    monkeypatch.setattr(e.live_review,"read",paused_read)
    monkeypatch.setattr(live,"review",lambda stores,draft:seen.append(draft) or Verdict("send"))
    before=e.live_review.block(turn(channel),TurnMeta("snapshot"),close=False) if operation=="card" else None
    async def go():
        meta=TurnMeta("snapshot")
        pending=asyncio.create_task(e.live_review.block_async(turn(channel),meta,close=False)
            if operation=="card" else e.live_review.check(turn(channel),meta,"a synthetic reply"))
        try:
            assert await asyncio.to_thread(entered.wait,3)
            e.world.online.clear()
            e.world.members.clear()
            e.world.group=None
        finally:
            release.set()
        result=await pending
        if operation=="card":
            assert result == before
            if channel!="discord:general":
                assert "Arkon: online" in result
        else:
            assert seen and result.codes!= ("review_failed_open",)
    rig.sync(go())


def test_full_card_is_rerendered_to_keep_its_priority_sections(rig,monkeypatch):
    e=rig.engine
    e.cfg=replace(e.cfg,talk=replace(e.cfg.talk,aware=True))
    original_card=e._card
    monkeypatch.setattr(e,"_card",lambda p:original_card(p)+"\n"+"fixed profile detail "*110)
    c=Card(101,(Part("game","In game",("IMPORTANT_CURRENT_STATE",)),
                Part("context","Conversation",("x "*(950+2*(LIVE_BUDGET-1300)),)),  # over budget at any LIVE_BUDGET
                Part("drama","Social",("LOW_PRIORITY_DETAILS",))))
    monkeypatch.setattr(live,"card",lambda *a,**kw:c)
    messages,_=rig.sync(e.compose(turn()))
    text=str(messages)
    assert "IMPORTANT_CURRENT_STATE" in text and "LOW_PRIORITY_DETAILS" not in text
    assert sum(estimate_tokens(m["content"]) for m in messages)<=LIVE_BUDGET
    assert messages[-1]["content"].index(live.END)<messages[-1]["content"].index("Answer")
