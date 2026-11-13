from dataclasses import replace
from itertools import permutations
import pytest

from leeroy_mind.groups.config import GroupsConfig
from leeroy_mind.groups.night import settle
from leeroy_mind.groups import store as groups

ON=GroupsConfig(enabled=True)


def friends(h,guids):
    for g in guids:
        h.persona(g)
    for a,b in permutations(guids,2):
        h.store.put_relationship(replace(h.store.get_relationship(a,b),affinity=50))


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
    h.store.put_relationship(replace(h.store.get_relationship(102,101),affinity=39))
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
