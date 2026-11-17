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


# -- origins from the life he leads now: the guild Discord and the members' own raids -------------

LAUGH = "\U0001f602"


def dpost(h, t, guid, text, channel="general", reply_to=None, photo=False):
    from leeroy_mind.discord import store as discord
    talk = channel if channel.startswith(("dm:", "gdm:")) else "discord:" + channel
    line = h.say(t, guid, text, channel=talk, to=1 if channel.startswith("dm:") else 0)
    mid = discord.post(h.store, channel=channel, author=guid, origin="mind", text=text, reply_to=reply_to, now=t)
    h.store.set_meta(f"discord:line:{mid}", str(line))
    if photo:
        snap = h.store.conn.execute("INSERT INTO selfie_snap(kind,guid,source,ref,night,status,scene,audience,"
            "phases,pick,looks,created,not_before,expires,rendered) VALUES('situation',?,'life','beat:1','night',"
            "'ready','{}','anyone','[]',1,'looks',1,1,100,2)",
            (guid,)).lastrowid
        h.store.conn.execute("INSERT INTO discord_attachment(message,kind,path,width,height,bytes,snap) "
                             "VALUES (?,'snap','fixture.jpg',64,64,1,?)", (mid, snap))
    return mid


def react(h, mid, member, emoji=LAUGH):
    from leeroy_mind.discord import store as discord
    discord.react(h.store, mid, member=member, emoji=emoji, now=h.T)


def kinds(h, window=None):
    return [(o.kind, o.butt, o.originator) for o in event_origins(h.store, window or (h.T-200, h.T))]


def test_a_discord_message_three_members_laughed_at_is_an_origin(h):
    mid = dpost(h, h.T-100, 101, "floor inspector reporting in")
    react(h, mid, 102)
    react(h, mid, 103, "\U0001f44d")   # a thumbs-up is no laugh
    react(h, mid, 101)                # nor is her own
    dpost(h, h.T-90, 104, "lol", reply_to=mid)
    assert kinds(h) == []
    react(h, mid, 103, "\U0001f480")
    found = event_origins(h.store, (h.T-200, h.T))
    assert kinds(h) == [("discord_laughed", 0, 101)]
    assert origin_text(h.store, found[0], {101: "Testmember"}) == (
        'Testmember wrote in the guild Discord: "floor inspector reporting in" and 3 members laughed.')
    h.store.conn.execute("DELETE FROM discord_reaction WHERE member=102")
    assert origin_text(h.store, found[0], {}) is None
    react(h, mid, 102)
    h.store.conn.execute("UPDATE discord_message SET deleted=? WHERE id=?", (h.T, mid))
    assert origin_text(h.store, found[0], {}) is None and kinds(h) == []


@pytest.mark.parametrize("channel", ["dm:101", "gdm:1", "officer-chat", "unknown-room"])
def test_discord_origins_never_come_from_private_or_restricted_rooms(h, channel):
    mid = dpost(h, h.T-100, 101, "floor inspector?", channel=channel, photo=True)
    for g in (102, 103, 104):
        react(h, mid, g)
    h.clock.set(h.T + 2*3600)
    assert kinds(h, (h.T-200, h.T + 2*3600)) == []


def test_a_line_that_recalled_a_private_memory_roots_nothing(h):
    mid = dpost(h, h.T-100, 101, "floor inspector reporting in")
    for g in (102, 103, 104):
        react(h, mid, g)
    assert kinds(h) == [("discord_laughed", 0, 101)]
    secret = h.mem(101, "A private fixture.", scope="private", scope_with=1)
    h.store.conn.execute("UPDATE utterance SET memory_ids=? WHERE guid=101", (json.dumps([secret]),))
    assert kinds(h) == []


def test_photo_first_answers_and_unanswered_questions(h):
    photo = dpost(h, h.T-150, 103, "look", channel="pets", photo=True)
    react(h, photo, 101, "\U0001f44d")
    assert kinds(h) == []
    react(h, photo, 102, "\U0001f525")
    assert kinds(h) == [("photo_reacted", 103, 103)]
    h.store.conn.execute("DELETE FROM discord_reaction")
    for i in range(8):
        parent = dpost(h, h.T-140+i*4, 102, f"fixture topic {i}")
        dpost(h, h.T-139+i*4, 101, f"fixture answer {i}", reply_to=parent)
        dpost(h, h.T-138+i*4, 103, f"fixture second {i}", reply_to=parent)
    found = event_origins(h.store, (h.T-200, h.T))
    assert [(o.kind, o.butt) for o in found] == [("first_answers", 101)]
    assert len(found[0].refs) == 3
    assert origin_text(h.store, found[0], {101: "Testmember"}) == (
        "Testmember was the first to answer 8 messages in the guild Discord in one day.")
    h.store.conn.execute("UPDATE discord_message SET deleted=? WHERE text='fixture answer 7'", (h.T,))
    assert origin_text(h.store, found[0], {}) is None  # seven is no streak
    assert kinds(h) == []


def test_two_questions_nobody_answered_are_an_origin_until_someone_does(h):
    day = 86400
    first = dpost(h, h.T-3*day, 104, "anyone up for heroics?", channel="irl")
    assert kinds(h) == []
    dpost(h, h.T-2*3600, 104, "nobody?", channel="irl")
    found = event_origins(h.store, (h.T-day, h.T))
    assert [(o.kind, o.butt) for o in found] == [("left_on_read", 104)]
    h.clock.set(h.T)
    assert origin_text(h.store, found[0], {104: "Testmember"}) == (
        "Testmember asked 2 questions in the guild Discord that nobody answered.")
    assert event_origins(h.store, (h.T, h.T+day)) == []  # nothing new the next night
    dpost(h, h.T+10, 102, "sure", channel="irl", reply_to=first)
    assert origin_text(h.store, found[0], {}) is None


def test_first_dead_in_wipes_on_two_nights_is_one_origin(h):
    """The raids members run by themselves wipe once a night."""
    day = 86400
    for at in (h.T-2*day, h.T-100):
        h.boss(at, 631, 11, "engaged", (101, 102))
        h._event(at+1, Death(int((at+1)*1000), 101, 631, 1))
        h.boss(at+3, 631, 11, "wiped", (101, 102))
    assert event_origins(h.store, (h.T-3*day, h.T-day)) == []
    found = event_origins(h.store, (h.T-200, h.T))
    assert [(o.kind, o.butt) for o in found] == [("wipe_first_death", 101)]
    assert origin_text(h.store, found[0], {101: "Testmember"}) == "Testmember died first in 2 wipes on Sindragosa."
    assert event_origins(h.store, (h.T, h.T+day)) == []
    h.store.conn.execute("DELETE FROM game_event WHERE id=?", (int(found[0].refs[1][6:]),))
    assert origin_text(h.store, found[0], {}) is None


def test_a_discord_origin_is_coined_stored_with_its_kind_and_retired_when_it_goes(h):
    from leeroy_mind.culture.origins import term_origin
    from leeroy_mind.nightly import lexicon
    mid = dpost(h, h.T-100, 101, "floor inspector reporting in")
    for g in (102, 103, 104):
        react(h, mid, g)
    enable(h)
    ctx = h.ctx()
    class Reasoner:
        async def json(self, *args):
            return {"term": "floor inspector"}
    ctx.reasoner = Reasoner()
    asyncio.run(events(ctx))
    row = h.store.conn.execute("SELECT * FROM lexicon").fetchone()
    assert row["origin_kind"] == "event" and row["butt"] == 0 and row["originator"] == 101
    assert json.loads(row["origin_refs"])[-1] == "kind:discord_laughed"
    origin = term_origin(row)
    assert origin.kind == "discord_laughed" and origin.refs == (row["origin_ref"],)
    assert origin_text(h.store, origin, {}) is not None
    assert term_origin(dict(row) | {"origin_refs": json.dumps([row["origin_ref"], "kind:nonsense"])}).kind == "invalid"
    h.store.conn.execute("UPDATE discord_message SET deleted=? WHERE id=?", (h.T, mid))
    lexicon.fade(ctx)
    row = h.store.conn.execute("SELECT * FROM lexicon").fetchone()
    assert row["retired"] is not None


def test_a_group_dm_gives_no_streak_either(h):
    """A group DM's rows count for nothing: not a first answer, not a question left hanging."""
    for i in range(8):
        parent = dpost(h, h.T-140+i*4, 102, f"fixture topic {i}", channel="gdm:1")
        dpost(h, h.T-139+i*4, 101, f"fixture answer {i}", channel="gdm:1", reply_to=parent)
    dpost(h, h.T-3*86400, 104, "anyone up for heroics?", channel="gdm:1")
    dpost(h, h.T-2*3600, 104, "nobody?", channel="gdm:1")
    assert kinds(h, (h.T-86400, h.T)) == []


def test_a_discord_streak_joke_has_her_as_its_butt_so_the_tease_gate_decides(h):
    from leeroy_mind.culture.seeds import _culture_seeds
    from leeroy_mind.culture.tease import tease_ok
    for i in range(8):
        parent = dpost(h, h.T-140+i*4, 102, f"fixture topic {i}")
        dpost(h, h.T-139+i*4, 101, f"fixture answer {i}", reply_to=parent)
    enable(h)
    ctx = h.ctx()
    class Reasoner:
        async def json(self, *args):
            return {"term": "first responder"}
    ctx.reasoner = Reasoner()
    asyncio.run(events(ctx))
    row = h.store.conn.execute("SELECT * FROM lexicon").fetchone()
    assert row["butt"] == 101 and json.loads(row["origin_refs"])[-1] == "kind:first_answers"
    personas = {g: ctx.personas.get(g) for g in ctx.members()}
    seed, = _culture_seeds(h.store, now=h.T+4*86400, personas=personas, human=1, only=row["id"])
    assert seed.about[0] == 101                      # the target every teasing use is gated on
    gate = lambda: tease_ok(h.store.conn, 102, 101, personas=personas, human=1, now=h.T+4*86400)
    assert gate() == (False, "dislikes")             # 102 does not like her enough to tease her
    h.store.put_relationship(replace(h.store.get_relationship(102, 101), affinity=40))
    assert gate() == (True, "ok")
