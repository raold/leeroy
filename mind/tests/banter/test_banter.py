"""Invented cast, fake model/time/transport; no live services."""
import asyncio
from dataclasses import replace
import json
import random
from types import SimpleNamespace

import pytest

from conftest import HUMAN
from engine_rig import make_rig, engine_clock
from leeroy_mind.banter.config import BanterConfig, load
from leeroy_mind.banter.service import Banter, choose_cast, role
from leeroy_mind.banter.topics import Topic, event_topic, calendar_topics, rival_topics
from leeroy_mind.protocol import Boss, Chat, Death, Group, LootDrop, LootItem, LootWon
from leeroy_mind.storage.rows import Relationship
from leeroy_mind.talk.raid import RaidPhase


@pytest.fixture
def rig(store,tmp_path,monkeypatch):
    r=make_rig(store,engine_clock(),tmp_path,reply="the cart wheel has escaped again")
    e=r.engine
    e.cfg=replace(e.cfg,talk=replace(e.cfg.talk,realism=True))
    async def read(question):
        return question(store.conn)
    monkeypatch.setattr(e.live_review,"read",read)
    app=SimpleNamespace(engine=e,world=r.world,personas=e.personas,store=store,clock=r.clock,
        rng=random.Random(4),human_guid=HUMAN,arbiter=None,is_backlog=lambda ev:False)
    r.banter=Banter(app,BanterConfig(True,2))
    yield r
    r.close()


def human(rig,channel="guild"):
    return rig.store.insert_chat_line(Chat(0,channel,HUMAN,"Owner",0,"hello",0,0),
        session_id=None,origin="human",ts=rig.clock.now(),audience=(101,102,103,104,105))


@pytest.mark.parametrize("table",["", "[banter]\nenabled=false", "[banter]\nenabled=1",
    "[banter]\nenabled=true\nper_hour=-1", "[banter]\nenabled=true\nper_hour=true",
    "[banter]\nenabled=true\nunknown=1", "broken["])
def test_invalid_and_missing_off(tmp_path,table):
    path=tmp_path/"mind.toml"; path.write_text(table)
    assert not load(path).enabled


def test_load_default_limit(tmp_path):
    path=tmp_path/"mind.toml"; path.write_text("[banter]\nenabled=true")
    assert load(path)==BanterConfig(True,2)


@pytest.mark.parametrize("event,kind",[
    (Death(0,HUMAN,0,42),"death"),
    (Boss(0,631,1,2,"Invented","wiped",(HUMAN,101)),"wipe"),
    (LootWon(0,HUMAN,42,"Invented item","roll"),"loot"),
    (LootDrop(0,0,1,42,(LootItem(42,"Invented item",4,0),)),"loot")])
def test_recorded_event_topics_and_backlog(rig,event,kind):
    rig.world.set_group(Group(0,HUMAN,False,(HUMAN,101)))
    rig.store.insert_game_event(event,session_id=None,ts=rig.clock.now())
    t=event_topic(rig.banter.app,event)
    assert t.kind==kind and t.key.startswith("event:")
    if kind=='loot': assert "Invented item" in t.evidence
    rig.banter.app.is_backlog=lambda ev:True
    assert event_topic(rig.banter.app,event) is None


def test_unrelated_deaths_and_wipes_are_not_owner_teasing(rig):
    assert event_topic(rig.banter.app,Death(0,102,0,0)) is None
    assert event_topic(rig.banter.app,Boss(0,631,1,2,"Invented","wiped",(104,105))) is None


def test_calendar_and_only_public_confirmed_rival_lines(rig):
    s,now=rig.store,rig.clock.now()
    s.conn.execute("INSERT INTO realm_calendar(kind,creator,at,title,ref,updated,state) "
        "VALUES('birthday',101,?,'Invented','birthday:102:2026',?,'posted')",(now,now))
    assert list(calendar_topics(rig.banter.app,now))[0].target==102
    s.conn.execute("INSERT INTO realm_actor(guid,guild,name,role,added) VALUES(900,20,'Rival','officer',?)",(now,))
    for index,(channel,state) in enumerate((("whisper","sent"),("trade","dispatched"),("trade","sent"))):
        s.conn.execute("INSERT INTO realm_line(ts,guid,guild,channel,text,state,kind,command_id) "
            "VALUES(?,900,20,?,'our kettle won',?,'rival',?)",(now,channel,state,f"{index:032x}"))
    topics=list(rival_topics(rig.banter.app,now))
    assert len(topics)==1 and "our kettle won" in topics[0].evidence
    s.conn.execute("DELETE FROM realm_calendar")
    assert rig.banter.topic("guild",now).kind=="rival"
    assert rig.banter.topic("discord:general",now) is None


def test_cast_respects_ties_current_mood_and_stance(rig):
    app=rig.banter.app
    rig.store.put_relationship(Relationship(101,HUMAN,affinity=-60))
    rig.store.put_state(replace(rig.store.get_state(102),mood=-.8))
    t=Topic("test","death",rig.clock.now(),"Recorded death",HUMAN,True)
    picked=choose_cast(app,[101,102,103,104,105],t,random.Random(1))
    assert 2<=len(picked)<=3 and not {101,102}.intersection(picked)
    p=app.personas.get(103)
    # A stance is a role in the riff, never a public disclosure of a private bond.
    original=app.personas.get
    app.personas.get=lambda g:replace(p,romance=replace(p.romance,stance="fond")) if g==103 else original(g)
    assert role(app,103,HUMAN)=="defend"


@pytest.mark.parametrize("phase",[RaidPhase.COMBAT,RaidPhase.WIPE_SILENCE,RaidPhase.WIPE_DEBRIEF])
@pytest.mark.parametrize("channel",["guild","discord:general"])
def test_raid_exclusions(rig,monkeypatch,phase,channel):
    monkeypatch.setattr(rig.raid,"phase",lambda now:phase)
    assert not rig.banter.allowed(channel,rig.clock.now())


def test_quiet_window_is_separate_by_medium_and_caps_survive_restart(rig):
    b=rig.banter; now=rig.clock.now()
    human(rig,"discord:general")
    assert b.allowed("guild",now) and not b.allowed("discord:general",now)
    human(rig)
    assert not b.allowed("guild",now)
    a=Topic("a","death",now,"Recorded death",HUMAN,True)
    assert b.reserve(a,now)
    b.pending.append(Topic("b","loot",now,"Recorded loot",HUMAN,True))
    assert b.topic("guild",now) is None
    assert b.reserve(Topic("c","birthday",now,"Recorded birthday"),now)
    restarted=Banter(b.app,b.cfg)
    assert not restarted.reserve(Topic("d","birthday",now,"Recorded birthday"),now)


async def finish(rig,task):
    for _ in range(600):
        if task.done():
            return await task
        await rig.advance(.5,.5)
        # Await a worker snapshot deterministically instead of relying on fake time to schedule OS threads.
        await asyncio.sleep(0)
    assert task.done(), "bounded banter did not finish"


@pytest.mark.parametrize("channel",["guild","discord:general"])
def test_real_pipeline_card_guards_fingerprint_and_natural_end(rig,monkeypatch,channel):
    game_before=(rig.engine._last_talk_at,rig.world.human_last_active,dict(rig.engine._last_line_id))
    answers=iter(["the kettle has declared independence", "a flag would suit that tiny kingdom",
        "its first law should concern biscuits", "the pantry can provide an ambassador",
        "i nominate the wooden spoon", "diplomacy might require more tea", "that settles the kitchen treaty", "long live the lid"])
    rig.llm.set_script(lambda *_:next(answers,"the pantry treaty is signed"))
    # Deterministic availability; production presence selection has its own test below.
    monkeypatch.setattr(rig.banter,"candidates",lambda _: [101,102,103,104])
    topic=Topic("fixture","quiet",rig.clock.now(),"The guild channel is quiet.")
    async def go():
        await finish(rig,asyncio.create_task(rig.banter.thread(channel,topic,[101,102,103])))
    rig.sync(go())
    lines=rig.store.conn.execute("SELECT * FROM chat_line WHERE origin='mind'").fetchall()
    assert 3<=len(lines)<=8
    assert {row["channel"] for row in lines}=={channel}
    assert len({row["from_guid"] for row in lines})>=2
    prompts=json.dumps(rig.llm.calls)
    assert "awareness_observations" in prompts and "Typing:" in prompts
    assert "Do not invent traits" in prompts
    assert not rig.engine.banter_active
    if channel=="discord:general":
        assert not rig.sent.says() and all(row["session_id"] is None for row in lines)
        assert (rig.engine._last_talk_at,rig.world.human_last_active,dict(rig.engine._last_line_id))==game_before


@pytest.mark.parametrize("channel",["guild","discord:general"])
def test_a_riff_takes_its_mediums_length(rig,monkeypatch,channel):
    """[talk] discord_words: a Discord riff writes as on Discord; the game's keeps one short line."""
    answers=iter(["the kettle has declared independence","a flag would suit that tiny kingdom",
        "its first law should concern biscuits"])
    rig.llm.set_script(lambda *_:next(answers,"the pantry treaty is signed"))
    monkeypatch.setattr(rig.banter,"candidates",lambda _: [101,102,103])
    topic=Topic("fixture","quiet",rig.clock.now(),"The guild channel is quiet.")
    async def go():
        await finish(rig,asyncio.create_task(rig.banter.thread(channel,topic,[101,102,103])))
    rig.sync(go())
    asked=[c["messages"][1]["content"] for c in rig.llm.calls if "messages" in c]
    assert asked
    if channel=="guild":
        assert all("One short line." in a and "This is Discord" not in a for a in asked)
    else:
        assert all("short line" not in a and "write in Discord chat" in a for a in asked)


@pytest.mark.parametrize("interrupt",["owner","fight"])
def test_owner_or_fight_interrupts_before_next_transport(rig,monkeypatch,interrupt):
    monkeypatch.setattr(rig.banter,"candidates",lambda _: [101,102,103])
    topic=Topic("fixture","quiet",rig.clock.now(),"The guild channel is quiet.")
    original=rig.engine._record
    def record(*a,**kw):
        result=original(*a,**kw)
        if interrupt=="owner":
            human(rig)
        else:
            monkeypatch.setattr(rig.raid,"phase",lambda _:RaidPhase.COMBAT)
        return result
    monkeypatch.setattr(rig.engine,"_record",record)
    async def go():
        await finish(rig,asyncio.create_task(rig.banter.thread("guild",topic,[101,102,103])))
    rig.sync(go())
    assert len(rig.sent.says())==1


def test_quiet_topic_requires_a_real_quiet_stretch(rig):
    b=rig.banter
    assert b.topic("guild",rig.clock.now()) is None
    rig.sync(rig.advance(601))
    assert b.topic("guild",rig.clock.now()).kind=="quiet"


def test_discord_presence_needs_no_online_body(rig):
    # The fixture clock is in the evening; its UK members are online by their own schedule.
    rig.world.online.clear()
    rig.world.human_online=False
    assert not rig.banter.candidates("guild")
    assert rig.banter.candidates("discord:general")


@pytest.mark.parametrize("failure",["card","review"])
def test_required_card_and_guards_fail_closed(rig,monkeypatch,failure):
    async def fail(*a,**kw):
        raise RuntimeError("fixture")
    if failure=="card":
        monkeypatch.setattr(rig.engine.live_review,"block_async",fail)
    else:
        original=rig.engine.live_review.read
        calls=0
        async def read(question):
            nonlocal calls
            calls+=1
            if calls>1:
                raise RuntimeError("fixture")
            return await original(question)
        monkeypatch.setattr(rig.engine.live_review,"read",read)
    monkeypatch.setattr(rig.banter,"candidates",lambda _: [101,102,103])
    topic=Topic("fixture","quiet",rig.clock.now(),"The guild channel is quiet.")
    async def go():
        await finish(rig,asyncio.create_task(rig.banter.thread("guild",topic,[101,102,103])))
    rig.sync(go())
    assert not rig.sent.says()


def test_single_line_keeps_typing_but_cannot_split_or_add_correction(rig):
    from leeroy_mind.talk.pipeline import TurnMeta
    from leeroy_mind.talk.turns import Turn,TurnKind
    from leeroy_mind.talk.style import Styled
    from test_timing_realism import Always,LONG
    e=rig.engine
    e.rng=Always()
    e.realism.member(101).credit=1
    plan=e.realism.plan(e,Turn(TurnKind.OPEN,101,"guild"),TurnMeta("bounded",single_line=True),
        e.personas.get(101),[Styled(LONG,LONG)],[2])
    assert len(plan.lines)==1 and not plan.correction and not plan.burst
    assert plan.delays[0]>2


def test_stale_topic_and_zero_hourly_limit_do_not_start(rig):
    b=rig.banter; now=rig.clock.now()
    b.pending.append(Topic("old","death",now-901,"Recorded old death",HUMAN,True))
    assert b.topic("guild",now) is None
    b.cfg=BanterConfig(True,0)
    assert not b.allowed("guild",now) and not b.allowed("discord:general",now)


def test_discord_cancellation_never_takes_a_gpu_lease(rig,monkeypatch):
    class Arbiter:
        def __init__(self): self.held=set()
        def hold(self,name): self.held.add(name); return True
        async def hand_over(self): pass
        async def release_live(self,name): self.held.remove(name)
    arbiter=Arbiter()
    rig.banter.app.arbiter=arbiter
    monkeypatch.setattr(rig.banter,"candidates",lambda _:[101,102,103])
    async def go():
        entered=asyncio.Event()
        async def speak(*args):
            entered.set()
            await asyncio.Future()
        monkeypatch.setattr(rig.engine,"_speak",speak)
        task=asyncio.create_task(rig.banter.thread("discord:general",
            Topic("fixture","quiet",rig.clock.now(),"Quiet"),[101,102,103]))
        await entered.wait()
        assert not arbiter.held
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        assert not arbiter.held and not rig.engine.banter_active
    rig.sync(go())
