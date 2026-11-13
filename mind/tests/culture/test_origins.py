import json
import asyncio
from dataclasses import replace
import pytest
from leeroy_mind.culture.origins import event_origins, origin_text, coin, events
from leeroy_mind.protocol import Death
from nightly.test_lexicon_wear import enable


def wipes(h, count=2, boss=11):
    for i in range(count):
        at = h.T - 100 + i*20
        h.boss(at,631,boss,"engaged",(101,102))
        h._event(at+1, Death(int((at+1)*1000),101,631,1))
        h._event(at+2, Death(int((at+2)*1000),102,631,1))
        h.boss(at+3,631,boss,"wiped",(101,102))


def test_wipe_first_death_origin_needs_two_wipes_on_one_boss(h):
    wipes(h,1)
    assert event_origins(h.store,(h.T-200,h.T)) == []
    wipes(h,1,boss=12)
    assert event_origins(h.store,(h.T-200,h.T)) == []
    h.store.conn.execute("DELETE FROM game_event")
    wipes(h,2,boss=13)
    found=event_origins(h.store,(h.T-200,h.T))
    assert len(found)==1 and found[0].butt==101


def receipt(h):
    key="guild:loot:7:abcdef"
    h.store.set_meta("human_guid","1")
    h.store.set_meta(key,json.dumps(dict(state="charged",item_id=123,human_claimed=True,
        bidders=[dict(guid=102,wanted=True)],
        award=dict(guid=1,paid=5,runner_up=101,close=True))))
    h.store.conn.execute("INSERT INTO dkp_ledger(ts,guid,delta,reason,raid_id,item_id,note) VALUES(?,1,-5,'item',7,123,?)",(h.T-1,key))
    return key


def test_close_loot_and_his_claim_origins_have_their_butt(h):
    receipt(h)
    found=event_origins(h.store,(h.T-200,h.T))
    assert [(o.kind,o.butt) for o in found]==[("close_loot",101),("his_claim",102)]
    assert all(origin_text(h.store,o,{}) for o in found)
    h.store.conn.execute("DELETE FROM dkp_ledger")
    assert all(origin_text(h.store,o,{}) is None for o in found)


def test_origin_text_is_built_from_rows_and_unresolvable_retires(h):
    from leeroy_mind.nightly import lexicon
    wipes(h)
    origin=event_origins(h.store,(h.T-200,h.T))[0]
    assert origin_text(h.store,origin,{101:"Testmember"}) == "Testmember died first in 2 wipes on Sindragosa."
    enable(h)
    ctx=h.ctx()
    ident=lexicon.create_term(h.store,ctx,kind="joke",term="floor inspector",origin_ref=origin.refs[0],
        originator=101,now=origin.when,butt=101,origin_kind="event",origin_refs=origin.refs,refs=origin.refs)
    assert ident
    h.store.conn.execute("DELETE FROM game_event WHERE id=?",(int(origin.refs[-1][6:]),))
    assert origin_text(h.store,origin,{}) is None
    lexicon.fade(ctx)
    row=h.store.conn.execute("SELECT * FROM lexicon WHERE id=?",(ident,)).fetchone()
    assert row["status"]=="dormant" and row["retired"] is not None


@pytest.mark.parametrize("term,expected",[("floor inspector","floor inspector"),("Quillon",None),("as an AI",None),("sus",None),("x"*61,None)])
def test_term_is_screened_and_model_never_writes_the_origin(h,term,expected):
    wipes(h)
    origin=event_origins(h.store,(h.T-200,h.T))[0]
    text=origin_text(h.store,origin,{})
    class Reasoner:
        async def json(self, system, user, schema):
            assert user==text
            return {"term":term,"origin":"made up"}
    ctx=h.ctx()
    ctx.reasoner=Reasoner()
    assert asyncio.run(coin(ctx,origin,text))==expected
    assert origin_text(h.store,origin,{})==text


def test_events_births_are_checkpointed_and_off_is_inert(h):
    wipes(h)
    ctx=h.ctx()
    class Reasoner:
        calls=0
        async def json(self,*args):
            self.calls+=1
            return {"term":"floor inspector"}
    ctx.reasoner=Reasoner()
    asyncio.run(events(ctx))
    assert ctx.reasoner.calls==0
    enable(h)
    ctx.settings = h.settings
    asyncio.run(events(ctx))
    asyncio.run(events(ctx))
    assert ctx.reasoner.calls==1
    row=h.store.conn.execute("SELECT * FROM lexicon").fetchone()
    assert row["origin_kind"]=="event" and row["butt"]==101


@pytest.mark.parametrize("channel,to",[("whisper",102),("dm:101",1),("officer",0),("voice:officers",0),("discord:unknown",0)])
def test_scoped_laughed_lines_never_become_public_origins(h,channel,to):
    h.say(h.T-100,101,"floor inspector",channel=channel,to=to)
    h.say(h.T-99,102,"haha",channel=channel,to=to)
    h.say(h.T-98,103,"haha",channel=channel,to=to)
    assert event_origins(h.store,(h.T-200,h.T)) == []


def test_laughed_line_keeps_todays_two_distinct_laughers(h):
    ident=h.say(h.T-100,101,"floor inspector")
    h.say(h.T-99,102,"haha")
    assert event_origins(h.store,(h.T-200,h.T)) == []
    h.say(h.T-98,103,"haha")
    found=event_origins(h.store,(h.T-200,h.T))
    assert len(found)==1 and found[0].kind=="laughed_line" and found[0].originator==101
    assert origin_text(h.store,found[0],{101:"Testmember"})=='Testmember said: "floor inspector"'


def test_typed_loot_origin_cannot_change_fact_kind(h):
    from leeroy_mind.culture.origins import term_origin
    receipt(h)
    found=event_origins(h.store,(h.T-200,h.T))
    for origin in found:
        row=dict(origin_refs=json.dumps(origin.refs),butt=origin.butt,originator=origin.originator,born=origin.when)
        assert term_origin(row)==origin
    broken=replace(found[0],refs=(found[0].refs[0].replace("close_loot:","his_claim:",1),))
    assert origin_text(h.store,broken,{}) is None


def test_retired_jokes_never_return_and_births_replace_them(h):
    from nightly.test_night_lexicon import term, spoken
    from leeroy_mind.nightly import lexicon
    ids=[term(h,word=f"old joke {i}") for i in range(8)]
    enable(h)
    wipes(h)
    ctx=h.ctx()
    class Reasoner:
        calls=0
        async def json(self,*args):
            self.calls+=1
            return {"term":"floor inspector"}
    ctx.reasoner=Reasoner()
    asyncio.run(events(ctx))
    assert ctx.reasoner.calls==0
    h.store.conn.execute("DELETE FROM night_step WHERE unit='events'")
    h.store.conn.execute("UPDATE lexicon SET uses_total=12 WHERE id=?",(ids[0],))
    lexicon.fade(ctx)
    spoken(h,"old joke 0",guid=1)
    lexicon.spread(ctx)
    assert h.store.conn.execute("SELECT status FROM lexicon WHERE id=?",(ids[0],)).fetchone()[0]=="dormant"
    asyncio.run(events(ctx))
    assert ctx.reasoner.calls==1
    assert h.store.conn.execute("SELECT count(*) FROM lexicon WHERE kind='joke' AND status='active'").fetchone()[0]==8


def test_events_recheck_origins_after_model_and_leave_failed_unit_retryable(h):
    wipes(h)
    enable(h)
    ctx=h.ctx()
    class Reasoner:
        async def json(self,*args):
            h.store.conn.execute("DELETE FROM game_event WHERE id=(SELECT max(id) FROM game_event)")
            return {"term":"floor inspector"}
    ctx.reasoner=Reasoner()
    asyncio.run(events(ctx))
    assert h.count("lexicon")==0
    assert not ctx.done("language","events")
