import asyncio
from datetime import datetime,timedelta,timezone
from dataclasses import replace
from types import SimpleNamespace
import pytest

from leeroy_mind.groups.config import GroupsConfig
from leeroy_mind.groups import store as groups
from leeroy_mind.groups.conversations import seeds,reserve,valid
from leeroy_mind.server.seeds import Seed
from conftest import run
from server.test_engine import app, finished
from server.test_runtime import app_for

ON=GroupsConfig(enabled=True)
NOW=datetime(2026,11,13,20,tzinfo=timezone.utc)


def test_groups_only_get_public_or_their_own_life_and_daily_limit(store):
    channel=groups.create(store,human=1,members=[101,102],title='Fixture',created_by=1,now=NOW.timestamp(),config=ON)
    source=[Seed('raid','raid_before',('general',),'Raid'),Seed('life:1','life',('irl',),'Own life',(101,)),
        Seed('life:2','life',('irl',),'Other life',(103,)),Seed('off','officer',('officer-chat',),'Private',officers_only=True),
        Seed('photo','photo',('pics',),'Photo',(101,))]
    out,audiences=seeds(store.conn,source,now=NOW.timestamp(),local=NOW,human=1,config=ON)
    assert {s.kind for s in out}=={'raid_before','life'}
    assert audiences[channel]=={1,101,102}
    life=next(s for s in out if s.kind=='life')
    assert life.openers==(101,)
    assert reserve(store,channel,NOW.date().isoformat())
    assert reserve(store,channel,NOW.date().isoformat())
    assert not reserve(store,channel,NOW.date().isoformat())
    assert not seeds(store.conn,source,now=NOW.timestamp(),local=NOW,human=1,config=ON)[0]


def test_seven_days_without_owner_goes_still_and_membership_change_stops_live(store):
    channel=groups.create(store,human=1,members=[101,102],title='Fixture',created_by=1,now=NOW.timestamp()-7*86400,config=ON)
    seed=Seed('raid','raid_before',('general',),'Raid')
    assert not seeds(store.conn,[seed],now=NOW.timestamp(),local=NOW,human=1,config=ON)[0]
    from leeroy_mind.discord.store import post
    post(store,channel=channel,author=1,origin='human',text='hello',now=NOW.timestamp())
    assert seeds(store.conn,[seed],now=NOW.timestamp(),local=NOW,human=1,config=ON)[0]
    app=SimpleNamespace(groups=SimpleNamespace(readers=lambda ch:groups.readers(store.conn,ch)))
    conversation=SimpleNamespace(channel=channel,readers=frozenset({1,101,102}),seed=seed,messages=[])
    assert valid(app,conversation,101)
    groups.add(store,channel,103,human=1,now=NOW.timestamp())
    assert not valid(app,conversation,101)


def test_member_group_waits_until_next_day_and_only_creator_may_introduce(store):
    channel=groups.create(store,human=1,members=[101,102,103],title='Fixture',created_by=102,
        now=NOW.timestamp(),config=ON)
    key='groups:first:'+channel.removeprefix('gdm:')
    store.set_meta(key,'1')
    source=[Seed('raid','raid_before',('general',),'Raid')]
    assert seeds(store.conn,source,now=NOW.timestamp(),local=NOW,human=1,config=ON)[0]==[]
    tomorrow=NOW+timedelta(days=1)
    out,audiences=seeds(store.conn,source,now=tomorrow.timestamp(),local=tomorrow,human=1,config=ON)
    assert len(out)==1 and out[0].kind=='group_start' and out[0].openers==(102,)
    app=SimpleNamespace(groups=SimpleNamespace(readers=lambda ch:groups.readers(store.conn,ch)))
    conversation=SimpleNamespace(channel=channel,readers=audiences[channel],seed=out[0],messages=[])
    assert valid(app,conversation,102) and not valid(app,conversation,101)
    conversation.messages.append(1)
    assert valid(app,conversation,101)
    groups.remove(store,channel,102,human=1,now=tomorrow.timestamp())
    assert store.get_meta(key) is None
    assert [s.kind for s in seeds(store.conn,source,now=tomorrow.timestamp(),local=tomorrow,human=1,config=ON)[0]]==['raid_before']


def test_group_seed_cooldowns_are_independent_per_destination(store,monkeypatch):
    from leeroy_mind.server import runtime,seeds as subjects
    app,_,_=app_for(store)
    app.groups=SimpleNamespace(config=ON)
    now=app.clock.now()
    channels=[groups.create(store,human=1,members=[101,102],title='Fixture',created_by=1,
        now=now,config=ON) for _ in range(2)]
    seed=Seed('fixture','raid_before',('general',),'Raid')
    monkeypatch.setattr(subjects,'gather',lambda *args,**kwargs:[seed])
    copies=[replace(seed,key=f'group:{ch.removeprefix("gdm:")}:{seed.key}',channels=(ch,)) for ch in channels]
    subjects.mark_used(store,copies[0],now=now)
    store.conn.commit()
    result=asyncio.run(runtime.snapshot(app,now,include_seeds=True))
    assert {s.key for s in result.seeds}=={seed.key,copies[1].key}
    subjects.mark_used(store,seed,now=now)
    store.conn.commit()
    result=asyncio.run(runtime.snapshot(app,now,include_seeds=True))
    assert {s.key for s in result.seeds}=={copies[1].key}
    result=asyncio.run(runtime.snapshot(app,now+subjects.FRESH_DEFAULT_S,include_seeds=True))
    assert {s.key for s in result.seeds}=={seed.key,*(s.key for s in copies)}


def test_pending_introduction_ignores_attempt_cooldown_but_keeps_daily_cap(store,monkeypatch):
    from leeroy_mind.server import runtime,seeds as subjects
    app,_,_=app_for(store)
    app.groups=SimpleNamespace(config=ON)
    now,local=app.clock.now(),app.clock.local()
    channel=groups.create(store,human=1,members=[101,102],title='Fixture',created_by=101,
        now=now-86400,config=ON)
    store.set_meta('groups:first:'+channel.removeprefix('gdm:'),'1')
    monkeypatch.setattr(subjects,'gather',lambda *args,**kwargs:[])
    intro=seeds(store.conn,[],now=now,local=local,human=1,config=ON)[0][0]
    subjects.mark_used(store,intro,now=now)
    assert reserve(store,channel,local.date().isoformat())
    store.conn.commit()
    assert asyncio.run(runtime.snapshot(app,now,include_seeds=True)).seeds==[intro]
    assert reserve(store,channel,local.date().isoformat())
    store.conn.commit()
    assert asyncio.run(runtime.snapshot(app,now,include_seeds=True)).seeds==[]


def test_introduction_stays_pending_until_creator_first_line_is_stored(app,monkeypatch):
    app.groups.config=ON
    now,local=app.clock.now(),app.clock.local()
    channel=groups.create(app.store,human=1,members=[101,102],title='Fixture',created_by=102,
        now=now-86400,config=ON)
    key='groups:first:'+channel.removeprefix('gdm:')
    app.store.set_meta(key,'1')
    app.observed.seeds,app.observed.group_audiences=seeds(app.store.conn,[],now=now,local=local,human=1,config=ON)
    original=app.engine.speak_meta
    attempts=[]
    async def drop_first(turn,meta):
        attempts.append(turn.speaker)
        if len(attempts)==1:
            return []
        return await original(turn,meta)
    monkeypatch.setattr(app.engine,'speak_meta',drop_first)
    async def go():
        engine=app.server.engine
        await engine.tick(now)
        await finished(engine)
        assert attempts==[102] and app.store.get_meta(key)=='1'
        engine.next_start=now
        await engine.tick(now)
        await finished(engine)
    run(go())
    assert app.store.get_meta(key) is None
    rows=app.store.conn.execute('SELECT author FROM discord_message WHERE channel=? ORDER BY id',(channel,)).fetchall()
    assert 2<=len(rows)<=4 and rows[0][0]==102 and {r[0] for r in rows}=={101,102}
    source=[Seed('raid','raid_before',('general',),'Raid')]
    assert [s.kind for s in seeds(app.store.conn,source,now=now+86400,
        local=local+timedelta(days=1),human=1,config=ON)[0]]==['raid_before']
