import dataclasses
import json
import logging
import sqlite3
from types import SimpleNamespace

import pytest

from conftest import make_persona, seed_test_cast, unit
from leeroy_mind.awareness import Card, CardContext, CardSources, ContextItem, Part, card
from leeroy_mind.awareness.card import PRIORITY
from leeroy_mind.bodies import ALL
from leeroy_mind.claims.store import add, supersede
from leeroy_mind.personas.io import save_persona
from leeroy_mind.protocol import Boss, Chat, Death, Group, Hello, Login, Logout, LootWon, TextEmote, Zone
from leeroy_mind.storage.rows import pack_vec
from leeroy_mind.text import estimate_tokens


def section(result, key):
    return "\n".join(
        line for part in result.parts if part.key == key for line in part.lines)


def say(store, who, text, *, channel="guild", to=0):
    return store.insert_chat_line(Chat(0, channel, who, f"Member{who}", to, text, 631, 210),
                                  session_id=None, origin="mind" if who != 1 else "human")


def claim(store, text, *, guid=101, source="chat", source_line=None, embedding=None, kind="opinion", **kw):
    return add(store, guid, kind=kind, subject="invented boss", text=text, polarity=1,
               source=source, source_line=source_line, embedding=embedding or pack_vec(unit(0)), **kw)


@pytest.fixture
def populated(store, clock):
    seed_test_cast(store)
    now = clock.now()
    store.set_meta("human_guid", "1")
    with store.transaction():
        store.conn.execute("UPDATE cast_member SET online=1,map=631,zone=210,updated=?", (now,))
        store.conn.execute("INSERT INTO guild_raid VALUES (7,?,NULL,'icc',10,0,0,101,101,102,3,2,NULL)", (now-100,))
        store.conn.executemany("INSERT INTO guild_attendance VALUES (7,?,?)", [(101,"raided"),(102,"benched")])
        store.conn.executemany("INSERT INTO dkp_ledger(ts,guid,delta,reason) VALUES (?,?,?,'kill')",
                               [(now,101,20),(now,102,30),(now,103,10)])
        store.conn.execute("INSERT INTO realm_calendar(id,kind,creator,at,title,updated,event_id,state) "
                           "VALUES (1,'raid',102,?,'Invented raid night',?,77,'posted')", (now+86400,now))
        store.conn.executemany("INSERT INTO realm_rsvp VALUES (77,?,'signed_up',?)", [(101,now),(102,now)])
        store.conn.execute("INSERT INTO realm_bank(ts,dir,reason,guid,item_id,count,money) "
                           "VALUES (?,'in','supplies',102,123,4,0)", (now,))
    line = say(store, 101, "I like the invented boss")
    claim(store, "I like the invented boss", source_line=line)
    with store.transaction():
        store.conn.execute("INSERT INTO feud(a,b,started,cause,evidence) VALUES (101,102,?,?,?)",
                           (now,"UNRENDERED_CAUSE",json.dumps([f"chat:{line}"])))
    for ev in (Group(0,1,True,(1,101,102)), TextEmote(0,102,"wave",101),
               LootWon(0,101,123,"Invented boots","need"), Death(0,101,631,7),
               Boss(0,631,9,4,"Invented boss","killed",(101,102))):
        store.insert_game_event(ev, session_id=None)
    return store


def context(clock, **kw):
    return CardContext(now=clock.now(), **kw)


def test_all_parts_have_real_sources_and_no_generated_cause(populated, clock):
    result = card(populated, 101, context=context(clock, channel="guild", purpose="Raid planning"))
    assert tuple(p.key for p in result.parts) == PRIORITY
    game = section(result,"game")
    assert "online (last stored observation)" in game
    assert "zone: 210" in game and "instance: 9 (last encounter)" in game
    assert "area: unknown" in game and "Combat: unknown" in game
    assert "His raid: #1, #101, #102" in game
    assert "Emote wave from #102" in section(result,"recent")
    assert "Received item #123" in section(result,"recent") and "Died" in section(result,"recent")
    assert "Boss kill" in section(result,"recent")
    assert "3 kills, 2 wipes" in section(result,"raid")
    assert "Next raid" in section(result,"calendar") and "#102 signed_up" in section(result,"calendar")
    assert "DKP: 20; rank 2" in section(result,"standing") and "1 raided" in section(result,"standing")
    assert "Current balance: unknown" in section(result,"bank")
    assert "Feud #1 with #102: active" in section(result,"drama")
    assert "UNRENDERED_CAUSE" not in result.render(10000)


@pytest.mark.parametrize("key", ["context","recent","claims","raid","calendar","standing","bank","drama"])
def test_parts_absent_without_rows(store, clock, key):
    result = card(store,101,context=context(clock))
    assert not section(result,key)


@pytest.mark.parametrize("table", ["cast_member","meta","persona_core","game_event","chat_line",
    "member_claim","member_claim_vec","guild_raid","realm_calendar","realm_rsvp","guild_attendance",
    "dkp_ledger","realm_bank","feud","realm_action"])
def test_older_database_missing_each_table_still_builds(populated, clock, table):
    # Copy ordinary tables and rows, excluding the specified source and vector
    # shadow tables. Foreign-key enforcement is intentionally off in this old db.
    source = sqlite3.connect(":memory:")
    try:
        for name, sql in populated.conn.execute("SELECT name,sql FROM sqlite_master WHERE type='table'"):
            if name != table and name in {"cast_member","meta","persona_core","game_event","chat_line",
                    "member_claim","guild_raid","realm_calendar","realm_rsvp","guild_attendance","dkp_ledger","realm_bank","feud","realm_action"}:
                source.execute(sql)
                rows = populated.conn.execute(f'SELECT * FROM "{name}"').fetchall()
                if rows:
                    source.executemany(f'INSERT INTO "{name}" VALUES ({",".join("?" for _ in rows[0])})', rows)
        result = card(source,101,context=context(clock))
        assert section(result,"game")
        if table not in {"realm_rsvp", "member_claim_vec"}:
            assert table in result.missing
        missing_parts = {"member_claim":{"claims"}, "chat_line":{"claims","drama"},
                         "guild_raid":{"raid"}, "realm_calendar":{"calendar"},
                         "realm_bank":{"bank"}, "feud":{"drama"}, "game_event":{"recent"}}
        for key in ("claims", "raid", "calendar", "standing", "bank", "drama", "recent"):
            assert bool(section(result,key)) is (key not in missing_parts.get(table,set()))
    finally:
        source.close()


def test_empty_and_partial_legacy_database_are_read_only(tmp_path, clock):
    path = tmp_path / "old.db"
    sqlite3.connect(path).close()
    before = path.read_bytes()
    conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    try:
        result = card(conn,101,context=context(clock))
        assert "unknown" in result.render() and len(result.parts)==1
        assert conn.execute("PRAGMA user_version").fetchone()[0]==0
    finally:
        conn.close()
    assert path.read_bytes()==before


@pytest.mark.parametrize("table", ["cast_member","meta","persona_core","game_event","chat_line",
    "member_claim","guild_raid","realm_calendar","guild_attendance","dkp_ledger","realm_bank","feud","realm_action"])
def test_old_table_without_current_columns_is_omitted(table,clock):
    conn=sqlite3.connect(":memory:")
    try:
        conn.execute(f'CREATE TABLE "{table}" (legacy_id INTEGER)')
        result=card(conn,101,context=context(clock,channel="whisper",to_guid=1))
        assert "unknown" in section(result,"game")
        assert table in result.missing or table=="chat_line"  # No owner metadata, so no whisper read.
    finally:
        conn.close()


def test_render_keeps_only_a_priority_prefix_and_counts_full_separators():
    result = Card(101,tuple(Part(k,k,("x"*31,)) for k in PRIORITY))
    for budget in range(0,300):
        text = result.render(budget)
        assert estimate_tokens(text)<=budget
        kept=[p for p in result.parts if p.render() in text]
        assert kept==list(result.parts[:len(kept)])
        assert text=="\n\n".join(p.render() for p in kept)
    assert result.render(0)=="" and result.render(1)==""


@pytest.mark.parametrize("budget", [-1,True,1.5,"10"])
def test_bad_budget_is_rejected(budget):
    with pytest.raises(ValueError,match="awareness_budget_invalid"):
        Card(101,()).render(budget)


def test_same_state_is_deterministic_without_committing_caller_work(populated,clock):
    trace=[]
    populated.conn.set_trace_callback(trace.append)
    try:
        with pytest.raises(RuntimeError), populated.transaction():
            populated.conn.execute("INSERT INTO meta VALUES ('uncommitted-card-test','sentinel')")
            first=card(populated,101,context=context(clock))
            assert first==card(populated,101,context=context(clock))
            assert first.render()==card(populated,101,context=context(clock)).render()
            assert not any(sql.startswith(("COMMIT","UPDATE","DELETE")) for sql in trace)
            raise RuntimeError("rollback")
        assert populated.get_meta("uncommitted-card-test") is None
    finally:
        populated.conn.set_trace_callback(None)


def test_whispers_and_claims_stay_with_their_pair(populated,clock):
    say(populated,1,"OTHER_PAIR_SECRET",channel="whisper",to=102)
    say(populated,1,"OWN_PAIR_SECRET",channel="whisper",to=101)
    private=say(populated,101,"OWN_PRIVATE_CLAIM",channel="whisper",to=1)
    claim(populated,"OWN_PRIVATE_CLAIM",source_line=private)
    public=card(populated,101,context=context(clock,channel="guild")).render(10000)
    own=card(populated,101,context=context(clock,channel="whisper",to_guid=1)).render(10000)
    wrong=card(populated,101,context=context(clock,channel="whisper",to_guid=102)).render(10000)
    assert "SECRET" not in public and "PRIVATE_CLAIM" not in public
    assert "OWN_PAIR_SECRET" in own and "OWN_PRIVATE_CLAIM" in own
    assert "OTHER_PAIR_SECRET" not in own and "SECRET" not in wrong and "PRIVATE_CLAIM" not in wrong


def test_officer_chat_requires_role_and_current_officer_context(populated,clock):
    line=say(populated,101,"OFFICER_SECRET",channel="officer")
    claim(populated,"OFFICER_SECRET",source_line=line)
    ctx=context(clock,channel="officer",thread=(ContextItem("OFFICER_THREAD",channel="officer"),))
    assert "OFFICER" not in card(populated,101,context=ctx).render(10000)
    save_persona(populated,make_persona(101,"Arkon",guild_role="officer"),race="Human",
                 class_name="Warrior",source="seed",ts=clock.now())
    assert "OFFICER_SECRET" in card(populated,101,context=ctx).render(10000)
    assert "OFFICER_THREAD" in card(populated,101,context=ctx).render(10000)
    assert "OFFICER" not in card(populated,101,context=context(clock,channel="guild")).render(10000)


def test_context_slots_have_captured_audiences_and_no_implicit_discord_access(store,clock):
    visible=ContextItem("visible",channel="room:17",readers=(101,102))
    hidden=ContextItem("ROOM_SECRET",channel="room:17",readers=(102,103))
    ctx=context(clock,channel="room:17",readers=(101,),purpose="Planning",thread=(visible,hidden),
                reply_to=visible,pins=(visible,),discord_events=(visible,hidden))
    out=card(store,101,context=ctx).render(10000)
    assert all(s in out for s in ("Planning","Thread: visible","Reply to: visible","Pin: visible","Directed at you: visible"))
    assert "ROOM_SECRET" not in out
    assert not section(card(store,101,context=context(clock,channel="room:17",purpose="HIDDEN")),"context")


def test_live_claims_are_nearest_and_filter_expiry_supersession_other_members_and_unknown_source(populated,clock):
    public=say(populated,101,"nearest invented opinion")
    old=claim(populated,"SUPERSEDED",source="owner")
    claim(populated,"EXPIRED",kind="plan",source="owner",expires=clock.now()-1)
    claim(populated,"OTHER_MEMBER",guid=102,source="owner")
    claim(populated,"UNPROVEN_CHAT")
    newest=claim(populated,"nearest invented opinion",source_line=public,embedding=pack_vec(unit(1)))
    supersede(populated,old,newest)
    result=section(card(populated,101,context=context(clock,embedding=pack_vec(unit(1)))),"claims")
    assert result.splitlines()[0]=="opinion: nearest invented opinion"
    assert all(word not in result for word in ("EXPIRED","SUPERSEDED","OTHER_MEMBER","UNPROVEN_CHAT"))


def test_source_snapshots_add_only_known_game_state_and_fresh_bank(populated,clock):
    world=SimpleNamespace(members={101:SimpleNamespace(map=631,zone=99)},online=set(),human_guid=1,
                          group=Group(0,1,False,(1,101,102)))
    source=CardSources(populated.conn,world=world,game={101:{"area":7,"instance_id":88,"combat":False}},
                       bank_snapshot={"money":12345,"read_at":clock.now()},
                       holds=(SimpleNamespace(owner="training",guids=(101,)),
                              SimpleNamespace(owner="PRIVATE_OWNER_TEXT",guids=(101,))))
    result=card(source,101,context=context(clock))
    assert all(x in section(result,"game") for x in ("offline (current observer)","zone: 99","area: 7","instance: 88","Combat: rest","Holds: training"))
    assert "PRIVATE_OWNER_TEXT" not in result.render(10000)
    assert "12345 copper" in section(result,"bank")
    stale=dataclasses.replace(source,bank_snapshot={"money":12345,"read_at":clock.now()-901})
    assert "12345" not in card(stale,101,context=context(clock)).render(10000)


def test_durable_training_hold_exposes_code_only(populated,clock):
    populated.set_meta("tg:lab:owned",json.dumps({"phase":"blocked","guids":[101],"id":"PRIVATE_RUN_ID","secret":"PRIVATE_DATA"}))
    out=card(populated,101,context=context(clock)).render(10000)
    assert "Holds: training" in out and "PRIVATE" not in out


def test_all_body_hold_snapshot_and_finished_training(store,clock):
    source=CardSources(store.conn,holds=(SimpleNamespace(owner="training",guids=ALL),))
    assert "Holds: training" in card(source,101,context=context(clock)).render()
    store.set_meta("tg:lab:owned",json.dumps({"phase":"complete","guids":[101]}))
    store.set_meta("tg:benchmark:active",json.dumps({"members":[102],"owner":"PRIVATE"}))
    assert "Holds: none recorded" in card(store,101,context=context(clock)).render()


def test_durable_realm_hold_codes_ignore_completed_and_other_members(clock):
    conn=sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE TABLE realm_action(owner, reserved_manifest, role, state, transport_state)")
        conn.executemany("INSERT INTO realm_action VALUES (?,?,?,?,?)",[
            ("realm_day","[101]","root","waiting","entered"),
            ("PRIVATE_OWNER","[101]","root","blocked","entered"),
            ("raid","[101]","root","completed","entered"),
            ("training","[102]","root","waiting","entered"),
            ("benchmark","[101]","root","waiting","not_entered"),
        ])
        out=card(conn,101,context=context(clock)).render()
        assert "Holds: realm, realm_day" in out and "PRIVATE" not in out
    finally:
        conn.close()


def test_recent_presence_zone_and_hello_use_receipt_time(populated,clock):
    populated.insert_game_event(Logout(0,101,False),session_id=None,ts=clock.now()+1)
    populated.insert_game_event(Zone(0,101,99,"Invented zone"),session_id=None,ts=clock.now()+1)
    populated.insert_game_event(Hello(0,"test","1",1,1),session_id=None,ts=clock.now()+2)
    game=section(card(populated,101,context=CardContext(now=clock.now()+2)),"game")
    assert "offline" in game and "zone: 99" in game and "His group: unknown" in game
    populated.insert_game_event(Login(0,101,False),session_id=None,ts=clock.now()+3)
    assert "online" in section(card(populated,101,context=CardContext(now=clock.now()+3)),"game")


def test_stale_future_and_malformed_events_cannot_leak_text(store,clock):
    store.insert_game_event(TextEmote(0,102,"STALE",101),session_id=None,ts=clock.now()-901)
    store.insert_game_event(TextEmote(0,102,"FUTURE",101),session_id=None,ts=clock.now()+1)
    with store.transaction():
        store.conn.execute("INSERT INTO game_event(ts,type,guid,payload) VALUES (?,'loot_won',101,?)",
            (clock.now(),json.dumps({"guid":101,"item_id":"PRIVATE_PAYLOAD","name":"PRIVATE_NAME"})))
        store.conn.execute("INSERT INTO game_event(ts,type,guid,payload) VALUES (?,'group',101,'[]')",(clock.now(),))
    out=card(store,101,context=context(clock)).render(10000)
    assert "Received item #unknown" in out
    assert all(word not in out for word in ("STALE","FUTURE","PRIVATE"))


def test_next_raid_survives_a_full_upcoming_calendar(populated,clock):
    with populated.transaction():
        populated.conn.execute("UPDATE realm_calendar SET at=? WHERE id=1",(clock.now()+30*86400,))
        populated.conn.executemany("INSERT INTO realm_calendar(id,kind,creator,at,title,updated,state) "
            "VALUES (?,'event',101,?,'Invented social event',?,'posted')",
            [(i,clock.now()+i,clock.now()) for i in range(2,8)])
        populated.conn.execute("INSERT INTO realm_calendar(id,kind,creator,at,title,updated,state) "
            "VALUES (9,'raid',101,?,'UNPOSTED',?,'pending')",(clock.now(),clock.now()))
    calendar=section(card(populated,101,context=context(clock)),"calendar")
    assert len(calendar.splitlines())==5 and "Next raid #1" in calendar and "#102 signed_up" in calendar
    assert "UNPOSTED" not in calendar


def test_app_adapter_reads_snapshots_without_service_callbacks(populated,clock):
    def forbidden(*args,**kw):
        raise AssertionError("service callback")
    world=SimpleNamespace(members={},group=None,online=set(),human_guid=1,is_officer=forbidden)
    app=SimpleNamespace(store=populated,clock=clock,world=world,
        control=SimpleNamespace(_bank_cache=(0,{"money":123,"read_at":clock.now()}),bank=forbidden),
        bodies=SimpleNamespace(holder=forbidden,holds=forbidden),ollama=forbidden)
    assert "123 copper" in section(card(app,101,context=CardContext()),"bank")


def test_many_signups_do_not_crowd_out_standing_and_bank(populated,clock):
    with populated.transaction():
        populated.conn.executemany("INSERT INTO realm_rsvp VALUES (77,?,'signed_up',?)",
            [(g,clock.now()) for g in range(1000,1200)])
    result = card(populated,101,context=context(clock))
    assert section(result,'calendar').count('signed_up') == 4
    rendered = result.render(600)
    assert 'DKP:' in rendered and 'Current balance:' in rendered


def test_claims_fall_back_if_read_only_connection_has_no_vector_extension(populated,clock):
    # The CLI intentionally loads no extension and supplies no embedding; an API
    # caller can also provide an embedding on an extension-free legacy connection.
    conn=sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE TABLE member_claim (id,guid,kind,subject,text,source,source_line,superseded_by,expires,last_seen,first_seen)")
        conn.execute("INSERT INTO member_claim VALUES (1,101,'opinion','invented','Visible owner claim','owner',1,NULL,NULL,1,1)")
        conn.execute("CREATE TABLE chat_line (id,channel,from_guid,to_guid,audience,ts)")
        conn.execute("INSERT INTO chat_line VALUES (1,'guild',1,0,NULL,1)")
        conn.execute("CREATE TABLE member_claim_vec (legacy_id)")
        result=card(conn,101,context=context(clock,embedding=pack_vec(unit(0))))
        assert "Visible owner claim" in section(result,"claims") and "member_claim_vec" in result.missing
    finally:
        conn.close()


@pytest.mark.parametrize("evidence", ["[]", '["memory:1"]', '["chat:99999"]', '["chat:1","chat:99999"]', '"PRIVATE"'])
def test_unproven_drama_is_omitted(populated,clock,evidence):
    with populated.transaction():
        populated.conn.execute("UPDATE feud SET evidence=?",(evidence,))
    assert not section(card(populated,101,context=context(clock)),"drama")


@pytest.mark.parametrize("private,channel,partner,shown", [(0,"guild",0,True),(1,"guild",0,False),(1,"whisper",102,True),(1,"whisper",103,False)])
def test_encounter_drama_follows_existing_private_scope(populated,clock,private,channel,partner,shown):
    with populated.transaction():
        populated.conn.execute("DELETE FROM feud")
        populated.conn.execute("INSERT INTO encounter(id,a,b,private) VALUES (50,101,102,?)",(private,))
        populated.conn.execute("INSERT INTO feud(a,b,started,cause,evidence) VALUES (101,102,?,'SECRET_CAUSE','[\"encounter:50\"]')",(clock.now(),))
    result=card(populated,101,context=context(clock,channel=channel,to_guid=partner))
    assert bool(section(result,"drama")) is shown
    assert "SECRET_CAUSE" not in result.render(10000)


@pytest.mark.parametrize("root", ["feud:1", "encounter:50"])
def test_ledger_secret_drama_stays_with_its_pair(populated,clock,root):
    with populated.transaction():
        populated.conn.execute("INSERT INTO encounter(id,a,b,private) VALUES (50,101,102,0)")
        populated.conn.execute("UPDATE feud SET evidence='[\"encounter:50\"]'")
        populated.conn.execute("INSERT INTO fact(kind,root,secret,created) VALUES ('feud_cause',?,1,?)",(root,clock.now()))
    assert not section(card(populated,101,context=context(clock,channel="guild")),"drama")
    assert section(card(populated,101,context=context(clock,channel="whisper",to_guid=102)),"drama")
    assert not section(card(populated,101,context=context(clock,channel="whisper",to_guid=103)),"drama")


def test_drama_cannot_reveal_another_private_pair(populated,clock):
    private=say(populated,102,"OTHER_PAIR_PRIVATE",channel="whisper",to=103)
    with populated.transaction():
        populated.conn.execute("UPDATE feud SET evidence=?",(json.dumps([f"chat:{private}"]),))
    assert not section(card(populated,101,context=context(clock,channel="whisper",to_guid=102)),"drama")


def test_no_card_content_is_logged_and_reads_are_bounded(populated,clock,caplog):
    statements=[]
    populated.conn.set_trace_callback(statements.append)
    try:
        with caplog.at_level(logging.DEBUG):
            result=card(populated,101,context=context(clock,purpose="CONTEXT_SECRET",channel="guild"))
            result.render()
    finally:
        populated.conn.set_trace_callback(None)
    assert caplog.text==""
    assert len(statements)<=16
    assert all(sql.lstrip().startswith(("SELECT","WITH")) for sql in statements)
