import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from conftest import seed_test_cast
from leeroy_mind.discord import layout
from leeroy_mind.server import seeds


@pytest.fixture
def seeded(store):
    cast = seed_test_cast(store)
    store.conn.execute("INSERT INTO cast_member(guid,name,race,class,gender,level,rank,updated) VALUES (1,'Ownerchar',1,1,0,80,0,0)")
    store.set_meta('human_guid', '1')
    return cast


def gather(store, seeded, *, now=None, local=None):
    now = store.clock.now() if now is None else now
    return seeds.gather(store.conn, now=now, local=local or datetime.fromtimestamp(now, timezone.utc),
                        cast=seeded, officers=frozenset({102,104}), human=1,
                        human_name='PRIVATE REAL NAME', trial_rank=4)


def calendar(store, now, *, kind='raid', title='A raid', at=None):
    return store.conn.execute("INSERT INTO realm_calendar(kind,creator,at,title,updated,state) VALUES (?,102,?,?,?,'posted')",
                              (kind, now+3600 if at is None else at, title, now)).lastrowid


def raid(store, now, rid=1):
    store.conn.execute('INSERT INTO guild_raid(raid_id,started,ended,raid_key,size,heroic,offline,lead_guid,tank_guid,loot_guid,kills,wipes) '
                       "VALUES (?,?,?,'icc',10,0,0,102,101,104,4,2)", (rid,now-7200,now-3600))


def test_raid_before_uses_the_next_posted_calendar_event(store, seeded):
    now=store.clock.now()
    calendar(store, now, title='Icecrown run')
    calendar(store, now, title='Later', at=now+50000)
    found=[s for s in gather(store,seeded) if s.kind=='raid_before']
    assert len(found)==1 and 'Icecrown run' in found[0].fact
    assert found[0].channels==('raid-planning','general')


def test_raid_after_uses_real_kills_wipes_and_leader(store, seeded):
    raid(store,store.clock.now())
    found=next(s for s in gather(store,seeded) if s.kind=='raid_after')
    assert found.fact=='Last raid: Icecrown Citadel 10, 4 kills, 2 wipes, led by Belra'


def test_loot_uses_dkp_and_the_winners_class_and_attached_library(store, seeded):
    store.conn.execute("ATTACH DATABASE ':memory:' AS library")
    store.conn.execute('CREATE TABLE library.item(id INTEGER,name TEXT)')
    store.conn.execute("INSERT INTO library.item VALUES (123,'Test blade')")
    store.conn.execute("INSERT INTO dkp_ledger(ts,guid,delta,reason,item_id) VALUES (?,101,-40,'item',123)",(store.clock.now(),))
    found=next(s for s in gather(store,seeded) if s.kind=='loot')
    assert found.channels==('loot-and-dkp','warrior')
    assert found.fact=='Arkon won [Test blade] for 40 DKP'


def test_life_only_uses_the_last_nights_cast_beats(store, seeded):
    now=store.clock.now()
    store.conn.execute("INSERT INTO life_thread(id,guid,status) VALUES (1,101,'active')")
    store.conn.execute("INSERT INTO life_beat(thread_id,night,text,ts) VALUES (1,'2026-10-28','Older beat',?),(1,'2026-10-29','New cooking class',?)",(now-86400,now-20))
    found=[s for s in gather(store,seeded) if s.kind=='life']
    assert len(found)==1 and found[0].fact=='New cooking class' and found[0].about==(101,)
    assert found[0].openers == (101,)


def claim(store, now, channel, audience, *, expires=None, source=True):
    line=store.conn.execute("INSERT INTO chat_line(ts,channel,from_guid,from_name,to_guid,text,origin,audience) VALUES (?,?,101,'Arkon',1,'Source','mind',?)",
                           (now-30,channel,None if audience is None else json.dumps(audience))).lastrowid
    return store.conn.execute("INSERT INTO member_claim(guid,kind,subject,text,polarity,source,source_line,first_seen,last_seen,expires,embedding) "
                              "VALUES (101,'plan','weekend',?,1,'chat',?,?,?,?,zeroblob(2048))",
                              (f'Plan from {channel}',line if source else None,now-20,now-10,expires)).lastrowid


def test_no_seed_from_a_whisper_dm_or_officer_line_reaches_a_public_channel(store, seeded):
    now=store.clock.now()
    for ch,audience in [('whisper',[101,1]),('dm:101',[101,1]),('officer',[102,104]),('discord:officer-chat',[102,104]),('party',[101,102])]:
        claim(store,now,ch,audience)
    assert not [s for s in gather(store,seeded) if s.kind=='plan']


def test_public_plan_is_live_attributed_and_reached_the_whole_cast(store, seeded):
    now=store.clock.now()
    good=claim(store,now,'discord:general',[1,*seeded])
    claim(store,now,'discord:general',[101,102])
    claim(store,now,'discord:general',None)
    claim(store,now,'guild',None,expires=now-1)
    claim(store,now,'guild',None,source=False)
    found=[s for s in gather(store,seeded) if s.kind=='plan']
    assert [s.key for s in found]==[f'plan:{good}:2026-W44']


@pytest.mark.parametrize('kind',['calendar','birthday'])
def test_calendar_and_birthdays_are_posted_events_in_the_next_week(store, seeded, kind):
    now=store.clock.now()
    calendar(store,now,kind=kind,title='Test celebration')
    calendar(store,now,kind=kind,title='Far away',at=now+9*86400)
    found=[s for s in gather(store,seeded) if s.kind==kind]
    assert len(found)==1 and 'Test celebration on' in found[0].fact


def test_reset_and_weekend_match_the_local_day(store, seeded):
    for local,expected in [(datetime(2026,11,10,8),set()),(datetime(2026,11,10,9),{'reset'}),
                           (datetime(2026,11,14,12),{'weekend'}),(datetime(2026,11,15,12),{'weekend'})]:
        assert {s.kind for s in gather(store,seeded,local=local) if s.kind in {'reset','weekend'}}==expected


@pytest.mark.parametrize('accepted', [False, True])
def test_rivals_use_only_public_events_and_never_poaching_offers(store, seeded, accepted):
    now=store.clock.now()
    store.conn.execute("INSERT INTO realm_guild(id,role,faction,name,added) VALUES (9,'progression','horde','Test rivals',?)",(now,))
    for i,ch in enumerate(('trade','whisper','officer'),1):
        store.conn.execute("INSERT INTO realm_line(ts,guid,guild,channel,text,command_id,kind,state) VALUES (?,102,9,?,'PRIVATE GENERATED PROSE',?,'rival','sent')",(now,ch,f'{i:032x}'))
    store.set_meta('realm:poach:week',json.dumps({'period':'week','guild':9,'officer':102,'member':103,
                                                'ts':now,'accepted':accepted}))
    found=[s for s in gather(store,seeded) if s.kind=='rival']
    assert len(found)==1
    assert found[0].fact == 'Test rivals was active in trade this week; nothing more is known'
    assert found[0].channels == ('general',)
    store.conn.execute('DELETE FROM realm_line')
    assert not [s for s in gather(store,seeded) if s.kind=='rival']


def test_his_day_only_reads_his_recent_session_kills_and_loot(store, seeded):
    now=store.clock.now()
    store.conn.execute('INSERT INTO session(id,started,ended) VALUES (1,?,?)',(now-4000,now-2000))
    for kind,guid,payload,sid in [('boss',None,{'state':'killed','map':631,'boss_id':0,'members':[1,101],'boss_name':'Lord Marrowgar'},1),
                                 ('loot_won',1,{'item_id':123,'members':[1,102,104]},1),('loot_won',101,{'item_id':999},1),
                                 ('loot_won',1,{'item_id':998},None)]:
        store.conn.execute('INSERT INTO game_event(ts,type,guid,payload,session_id) VALUES (?,?,?,?,?)',(now-3000,kind,guid,json.dumps(payload),sid))
    found=[s for s in gather(store,seeded) if s.kind=='his_day']
    assert len(found)==2
    assert any('Lord Marrowgar' in s.fact for s in found)
    assert all('Ownerchar' in s.fact and 'PRIVATE REAL NAME' not in s.fact for s in found)
    assert next(s for s in found if 'killed' in s.fact).openers == (101,)
    assert next(s for s in found if 'won' in s.fact).openers == (102,104)


@pytest.mark.parametrize('members', [None, [], [1], [1,999], [True,101], '101', {'101':True}])
@pytest.mark.parametrize('kind', ['boss','loot_won'])
def test_his_day_requires_known_cast_witnesses(store, seeded, members, kind):
    now = store.clock.now()
    store.conn.execute('INSERT INTO session(id,started) VALUES (1,?)',(now-60,))
    payload = dict(state='killed',map=631,boss_id=0,item_id=123,members=members)
    store.conn.execute('INSERT INTO game_event(ts,type,guid,payload,session_id) VALUES (?,?,1,?,1)',
                       (now-30,kind,json.dumps(payload)))
    assert not [s for s in gather(store,seeded) if s.kind=='his_day']


def test_officer_meeting_never_becomes_a_public_calendar_seed(store, seeded):
    calendar(store,store.clock.now(),kind='meeting',title='Officer meeting')
    found = [s for s in gather(store,seeded) if s.kind=='calendar']
    assert len(found) == 1
    assert found[0].channels == ('officer-chat',) and found[0].officers_only


def test_his_real_name_never_appears_without_a_character_row(store, seeded):
    store.conn.execute('DELETE FROM cast_member WHERE guid=1')
    store.conn.execute("INSERT INTO dkp_ledger(ts,guid,delta,reason,item_id) VALUES (?,1,-20,'item',123)",(store.clock.now(),))
    assert all('PRIVATE REAL NAME' not in s.fact for s in gather(store,seeded))


def test_officer_seeds_only_in_officer_channels(store, seeded):
    now=store.clock.now()
    for rid in range(1,5):
        raid(store,now,rid)
        store.conn.execute("INSERT INTO guild_attendance VALUES (?,101,'raided')",(rid,))
    store.conn.execute('UPDATE cast_member SET rank=4 WHERE guid=103')
    found=gather(store,seeded)
    private=[s for s in found if s.officers_only]
    assert any('missed 4 of the last 4' in s.fact for s in private)
    assert any(s.key=='officer:trial:103:2026-W44' for s in private)
    assert all(layout.channel(ch).officers_only for s in private for ch in s.channels)
    assert not any(s.kind=='officer' and s.about==(101,) for s in private)


@pytest.mark.parametrize('start_day', [9,15])
def test_long_lived_seeds_return_only_in_a_new_iso_week(store, seeded, start_day):
    local = datetime(2026,11,start_day,20,tzinfo=timezone.utc)
    now = local.timestamp()
    claim(store,now,'guild',None)
    raid(store,now)
    store.conn.execute('UPDATE cast_member SET rank=4 WHERE guid=103')
    original = [s for s in gather(store,seeded,now=now,local=local) if s.kind in {'plan','officer'}]
    assert any(s.kind=='plan' for s in original)
    assert any(s.key.startswith('officer:trial:') for s in original)
    assert any(s.key.startswith('officer:attendance:') for s in original)
    assert all(s.key.endswith(':2026-W46') for s in original)
    for seed in original:
        seeds.mark_used(store,seed,now=now)
    for day in range(7-local.weekday()):
        at = local + timedelta(days=day)
        found = [s for s in gather(store,seeded,now=at.timestamp(),local=at) if s.kind in {'plan','officer'}]
        assert not seeds.fresh(store.conn,found,now=at.timestamp())
    next_week = datetime(2026,11,16,20,tzinfo=timezone.utc)
    found = [s for s in gather(store,seeded,now=next_week.timestamp(),local=next_week) if s.kind in {'plan','officer'}]
    assert len(found) == len(original)
    assert all(s.key.endswith(':2026-W47') for s in found)
    assert seeds.fresh(store.conn,found,now=next_week.timestamp()) == found


def test_a_used_seed_is_not_reused_for_three_days(store):
    seed=seeds.Seed('life:1','life',('irl',),'A fact')
    seeds.mark_used(store,seed,now=100)
    assert not seeds.fresh(store.conn,[seed],now=100+3*86400-1)
    assert seeds.fresh(store.conn,[seed],now=100+3*86400)==[seed]


def test_purpose_seed_returns_daily(store, seeded):
    local=datetime(2026,11,12,19)
    found=next(s for s in gather(store,seeded,local=local) if s.key.startswith('purpose:general:'))
    seeds.mark_used(store,found,now=100)
    assert not seeds.fresh(store.conn,[found],now=101)
    assert seeds.fresh(store.conn,[found],now=100+86400)==[found]
    assert all(layout.channel(ch).category!='info' for s in gather(store,seeded) if s.kind=='purpose' for ch in s.channels)


def test_missing_sources_are_skipped_and_logged_once_without_values(caplog):
    seeds._unavailable.clear()
    conn=sqlite3.connect(':memory:')
    try:
        kwargs=dict(now=0,local=datetime(2026,11,12,19),cast={},officers=frozenset(),human=1,human_name='PRIVATE',trial_rank=4)
        first=seeds.gather(conn,**kwargs)
        count=len(caplog.records)
        assert first and all(s.kind=='purpose' for s in first)
        assert seeds.gather(conn,**kwargs)==first and len(caplog.records)==count
        assert 'PRIVATE' not in caplog.text
    finally:
        conn.close()


def test_gather_and_fresh_are_safe_on_a_query_only_connection(store, seeded):
    store.conn.execute('PRAGMA query_only=ON')
    assert seeds.fresh(store.conn,gather(store,seeded),now=store.clock.now())


def test_future_and_stale_rows_do_not_become_current_events(store, seeded):
    now=store.clock.now()
    for when in (now+60,now-4*86400):
        store.conn.execute("INSERT INTO dkp_ledger(ts,guid,delta,reason,item_id) VALUES (?,101,-20,'item',123)",(when,))
        raid(store,when+3600,1 if when>now else 2)
    assert not [s for s in gather(store,seeded) if s.kind in {'raid_after','loot'}]
