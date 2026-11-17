from dataclasses import replace
from itertools import permutations
import pytest

from leeroy_mind.groups.config import GroupsConfig
from leeroy_mind.groups.night import settle
from leeroy_mind.groups import store as groups
from leeroy_mind.protocol import Chat

ON=GroupsConfig(enabled=True)


def wrote(h,guid,n=1,channel='whisper',age=0):
    for _ in range(n):
        h.store.insert_chat_line(Chat(0,channel,1,'Invented',guid,'A fixture line.',0,0),
            ts=h.clock.now()-age,session_id=None,origin='human')


def friends(h,guids,affinity=50,his=True):
    for g in guids:
        h.persona(g)
        if his:
            wrote(h,g)
    for a,b in permutations(guids,2):
        h.store.put_relationship(replace(h.store.get_relationship(a,b),affinity=affinity))


def count(h):
    return h.store.conn.execute('SELECT count(*) FROM discord_group').fetchone()[0]


def test_a_seeded_friendship_that_regressed_under_forty_still_forms_a_group(h):
    """Live, 2026-11-17: every seeded friendship sat at 39 after the first weekly regression,
    one point under the old bar, so no group could ever form."""
    friends(h,[101,102],affinity=39)
    settle(h.ctx(),ON,can_start=True)
    assert groups.readers(h.store.conn,'gdm:1')=={1,101,102}


def test_a_group_is_made_of_members_he_talks_to(h):
    friends(h,[101,102],his=False)
    settle(h.ctx(),ON,can_start=True)
    assert count(h)==0
    wrote(h,101,age=61*86400)
    wrote(h,102,channel='guild')
    settle(h.ctx(),ON,can_start=True)
    assert count(h)==0
    wrote(h,101,channel='dm:101')
    wrote(h,102)
    settle(h.ctx(),ON,can_start=True)
    assert count(h)==1


def test_the_starter_is_who_he_writes_most_and_seats_go_to_the_closest(h):
    friends(h,[101,102,103],affinity=35)
    wrote(h,103,5)
    for a,b in permutations([103,102],2):
        h.store.put_relationship(replace(h.store.get_relationship(a,b),affinity=60))
    h.store.put_relationship(replace(h.store.get_relationship(101,102),affinity=-5))
    settle(h.ctx(),ON,can_start=True)
    row=h.store.conn.execute('SELECT created_by FROM discord_group').fetchone()
    assert row['created_by']==103
    assert groups.readers(h.store.conn,'gdm:1')=={1,102,103}  # 101 and 102 do not get on


def test_a_mild_liking_needs_things_done_together(h):
    friends(h,[101,102],affinity=5)
    settle(h.ctx(),ON,can_start=True)
    assert count(h)==0
    conn=h.store.conn
    for raid in (1,2,3):
        conn.execute("INSERT INTO guild_raid(raid_id,started,raid_key,size,heroic,offline,lead_guid,tank_guid,"
            "loot_guid) VALUES(?,?,'naxx',10,0,1,101,101,101)",(raid,h.clock.now()-86400*(1 if raid<3 else 40)))
        conn.executemany("INSERT INTO guild_attendance(raid_id,guid,status) VALUES(?,?,?)",
                         [(raid,101,'raided'),(raid,102,'raided' if raid<3 else 'benched')])
    settle(h.ctx(),ON,can_start=True)
    assert count(h)==0  # two raids together in the window, one long ago
    conn.execute("UPDATE guild_raid SET started=? WHERE raid_id=3",(h.clock.now()-86400,))
    conn.execute("UPDATE guild_attendance SET status='raided' WHERE raid_id=3")
    settle(h.ctx(),ON,can_start=True)
    assert count(h)==1


@pytest.mark.parametrize('server',[False,True])
def test_nightly_starts_only_when_server_can_introduce_the_group(h,server):
    from conftest import run
    from leeroy_mind.groups.night import stage_groups
    friends(h,[101,102])
    ctx=h.ctx()
    (ctx.settings.data_dir/'mind.toml').write_text(
        '[groups]\nenabled=true\n[server]\nenabled='+str(server).lower(),encoding='utf-8')
    run(stage_groups(ctx))
    assert h.store.conn.execute('SELECT count(*) FROM discord_group').fetchone()[0]==int(server)


def test_member_group_weekly_cap_pairwise_affinity_and_next_day_receipt(h):
    friends(h,[101,102,103,104])
    ctx=h.ctx()
    settle(ctx,ON,can_start=True)
    rows=h.store.conn.execute('SELECT * FROM discord_group').fetchall()
    assert len(rows)==1 and rows[0]['created_by']==101
    assert groups.readers(h.store.conn,'gdm:1')=={1,101,102,103,104}
    assert h.store.get_meta('groups:first:1')=='1'
    settle(ctx,ON,can_start=True)
    assert h.store.conn.execute('SELECT count(*) FROM discord_group').fetchone()[0]==1


def test_group_needs_mutual_friendship_and_no_feud(h):
    friends(h,[101,102])
    h.store.put_relationship(replace(h.store.get_relationship(102,101),affinity=29))
    settle(h.ctx(),ON,can_start=True)
    assert h.store.conn.execute('SELECT count(*) FROM discord_group').fetchone()[0]==0
    h.store.put_relationship(replace(h.store.get_relationship(102,101),affinity=50))
    h.store.conn.execute("INSERT INTO feud(a,b,started,cause,evidence) VALUES(101,102,?,'Fixture', '[]')",(h.clock.now(),))
    settle(h.ctx(),ON,can_start=True)
    assert h.store.conn.execute('SELECT count(*) FROM discord_group').fetchone()[0]==0


def test_feud_departure_is_system_line_and_closes_small_group(h):
    friends(h,[101,102])
    channel=groups.create(h.store,human=1,members=[101,102],title='Fixture',created_by=1,now=h.clock.now(),config=ON)
    h.store.conn.execute("INSERT INTO feud(a,b,started,cause,evidence) VALUES(101,102,?,'Fixture', '[]')",(h.clock.now(),))
    settle(h.ctx(),replace(ON,member_starts_per_week=0))
    assert groups.get(h.store.conn,channel)['closed'] is not None
    row=h.store.conn.execute('SELECT origin,author,text,bot FROM discord_message').fetchone()
    assert row['origin']=='bot' and row['author']==0 and row['bot']=='group-system'
    assert row['text'].endswith(' left the group')
