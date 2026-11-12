import asyncio
import json
import random
from types import SimpleNamespace

from conftest import make_persona, run
from leeroy_mind.discord import store as ds
from leeroy_mind.discord.presence import Presence
from leeroy_mind.server import reacting
from leeroy_mind.server.config import ServerConfig
from leeroy_mind.tuning.effective import MemberTuning, effective


class Certain(random.Random):
    def random(self):
        return 0

    def uniform(self, low, high):
        return low


def setup(store, clock, monkeypatch, *, config=None, rng=None):
    people = {g:make_persona(g,'Member'+str(g)) for g in range(101,106)}
    snap = SimpleNamespace(personas=people,cast=people,human=1,officers={102,104},
        in_play=set(),away=set(),ties={},local=clock.local(),
        states={g:SimpleNamespace(presence=Presence('online',False,0)) for g in people})
    async def snapshot(*args,**kw):
        snap.local = clock.local()
        return snap
    monkeypatch.setattr(reacting.runtime,'snapshot',snapshot)
    cast = {g:SimpleNamespace(zone=0) for g in people}
    world = SimpleNamespace(discord_members=lambda:cast,is_officer=lambda g:g in snap.officers,
        online=set(people),human_online=False,group=None)
    monkeypatch.setattr(reacting,'presence_of',lambda p,**kw:snap.states[p.guid].presence)
    app = SimpleNamespace(store=store,clock=clock,world=world,personas=people,human_guid=1,
        guild=SimpleNamespace(members_in_play=lambda:snap.in_play))
    return reacting.Reactor(app,config or ServerConfig(enabled=True),rng or Certain(1)),snap


async def settle(reactor, clock):
    for _ in range(8):
        await asyncio.sleep(0)
    await clock.advance(901,step=901)
    while reactor._tasks:
        await asyncio.gather(*tuple(reactor._tasks))


def post(store,clock,*,author=1,channel='general',text='That raid was fun'):
    return ds.post(store,channel=channel,author=author,origin='human' if author==1 else 'mind',text=text,now=clock.now())


def test_friends_react_more_than_neutrals(store,clock,monkeypatch):
    async def go():
        reactor,snap = setup(store,clock,monkeypatch,rng=random.Random(51))
        snap.personas = {g:snap.personas[g] for g in (101,102)}
        snap.ties = {(101,1):'friend',(102,1):'neutral'}
        for _ in range(500):
            mid = post(store,clock)
            reactor.after_post(mid,author=1,channel='discord:general',text='That raid was fun',his=False)
        await settle(reactor,clock)
        counts = dict(store.conn.execute('SELECT member,count(*) FROM discord_reaction GROUP BY member'))
        assert counts[101]>counts[102]*1.7
        assert json.loads(store.get_meta('server:day:'+clock.local().date().isoformat()))['reactions']==sum(counts.values())
        await reactor.close()
    run(go())


def test_a_rival_never_hearts_and_a_feud_never_reacts():
    rng = random.Random(3)
    for _ in range(500):
        for text in ('You look great','Ugh my boss','Haha','Nice gear'):
            assert reacting.pick_emoji(text,'rival',rng)=='🙄'
            assert reacting.pick_emoji(text,'feud',rng) is None
            assert reacting.pick_emoji(text,'rival',rng) not in reacting.LOVE


def test_reaction_draw_caps_three_and_rival_needs_drama(store,clock,monkeypatch):
    async def go():
        reactor,snap = setup(store,clock,monkeypatch)
        snap.ties = {(101,1):'rival',(102,1):'feud',(103,1):'rival'}
        snap.personas[103] = effective(snap.personas[103],MemberTuning({'drama':7}))
        mid = post(store,clock)
        reactor.after_post(mid,author=1,channel='general',text='You look great',his=True)
        reactor.after_post(mid,author=1,channel='general',text='You look great',his=True)
        await settle(reactor,clock)
        rows = dict(store.conn.execute('SELECT member,emoji FROM discord_reaction WHERE message=?',(mid,)))
        assert len(rows)==3 and 101 not in rows and 102 not in rows and rows[103]=='🙄'
        await reactor.close()
    run(go())


def test_reactions_off_writes_none(store,clock,monkeypatch):
    async def go():
        for cfg in (ServerConfig(enabled=False),ServerConfig(enabled=True,reactions=False)):
            reactor,_ = setup(store,clock,monkeypatch,config=cfg)
            reactor.after_post(post(store,clock),author=1,channel='general',text='Hi',his=True)
            assert not reactor._tasks
            await reactor.close()
        assert store.conn.execute('SELECT count(*) FROM discord_reaction').fetchone()[0]==0
    run(go())


def test_deleted_message_and_later_feud_are_skipped(store,clock,monkeypatch,caplog):
    async def go():
        reactor,snap = setup(store,clock,monkeypatch)
        mid = post(store,clock,text='PRIVATE CONTENT')
        reactor.after_post(mid,author=1,channel='general',text='PRIVATE CONTENT',his=True)
        for _ in range(5):
            await asyncio.sleep(0)
        ds.delete(store,mid,author=1,now=clock.now())
        await settle(reactor,clock)
        assert store.conn.execute('SELECT count(*) FROM discord_reaction').fetchone()[0]==0
        mid = post(store,clock)
        reactor.after_post(mid,author=1,channel='general',text='You look great',his=True)
        for _ in range(5):
            await asyncio.sleep(0)
        snap.ties = {(g,1):'feud' for g in snap.personas}
        await settle(reactor,clock)
        assert store.conn.execute('SELECT count(*) FROM discord_reaction').fetchone()[0]==0
        assert 'PRIVATE CONTENT' not in caplog.text
        await reactor.close()
    run(go())


def test_his_post_gets_more_reactions_by_warmth(store,clock,monkeypatch):
    async def measure(his):
        reactor,snap = setup(store,clock,monkeypatch,rng=random.Random(31))
        snap.personas = {101:snap.personas[101]}
        snap.ties = {(101,1):'fond'}
        before = store.conn.execute('SELECT count(*) FROM discord_reaction').fetchone()[0]
        for _ in range(500):
            reactor.after_post(post(store,clock),author=1,channel='general',text='Hello',his=his)
        await settle(reactor,clock)
        after = store.conn.execute('SELECT count(*) FROM discord_reaction').fetchone()[0]
        await reactor.close()
        return after-before
    async def go():
        member,human = await measure(False),await measure(True)
        assert human>member*1.6
    run(go())


def test_no_reaction_from_playing_offline_or_outside_audience(store,clock,monkeypatch):
    async def go():
        reactor,snap = setup(store,clock,monkeypatch)
        snap.in_play.add(102)
        snap.states[104] = SimpleNamespace(presence=Presence('offline',False,0))
        reactor.after_post(post(store,clock,channel='officer-chat'),author=1,channel='officer-chat',text='Hi',his=True)
        await settle(reactor,clock)
        assert store.conn.execute('SELECT count(*) FROM discord_reaction').fetchone()[0]==0
        mid = post(store,clock,channel='dm:101')
        reactor.after_post(mid,author=1,channel='dm:101',text='Hi',his=True)
        await settle(reactor,clock)
        assert [r[0] for r in store.conn.execute('SELECT member FROM discord_reaction WHERE message=?',(mid,))]==[101]
        await reactor.close()
    run(go())


def test_close_cancels_delayed_reactions(store,clock,monkeypatch):
    async def go():
        reactor,_ = setup(store,clock,monkeypatch)
        reactor.after_post(post(store,clock),author=1,channel='general',text='Hi',his=True)
        for _ in range(5):
            await asyncio.sleep(0)
        await reactor.close()
        assert not reactor._tasks
        await clock.advance(1000,step=1000)
        assert store.conn.execute('SELECT count(*) FROM discord_reaction').fetchone()[0]==0
    run(go())


def test_counter_failure_rolls_back_reaction(store,clock,monkeypatch):
    def fail(*args,**kwargs):
        raise ValueError('counter_unavailable')
    monkeypatch.setattr(reacting,'increment',fail)
    async def go():
        reactor,_ = setup(store,clock,monkeypatch)
        mid = post(store,clock)
        reactor.after_post(mid,author=1,channel='general',text='Hi',his=True)
        for _ in range(8):
            await asyncio.sleep(0)
        await clock.advance(901,step=901)
        while reactor._tasks:
            await asyncio.gather(*tuple(reactor._tasks),return_exceptions=True)
        assert store.conn.execute('SELECT count(*) FROM discord_reaction').fetchone()[0]==0
        await reactor.close()
    run(go())


def test_malformed_day_counter_keeps_scheduled_reactions_and_warns_once(store,clock,monkeypatch,caplog):
    async def go():
        reactor,_ = setup(store,clock,monkeypatch)
        key = 'server:day:'+clock.local().date().isoformat()
        store.set_meta(key,'not-json')
        for _ in range(2):
            mid = post(store,clock)
            reactor.after_post(mid,author=1,channel='general',text='Hi',his=True)
        await settle(reactor,clock)
        assert store.conn.execute('SELECT count(*) FROM discord_reaction').fetchone()[0]==6
        assert store.get_meta(key)=='not-json'
        assert sum(r.message=='server_day_counter_bad' for r in caplog.records)==1
        assert not any(r.message.startswith('server_reaction_failed') for r in caplog.records)
        await reactor.close()
    run(go())


def test_close_during_snapshot_cannot_spawn_unowned_tasks(store,clock,monkeypatch):
    async def go():
        reactor,snap = setup(store,clock,monkeypatch)
        entered,release = asyncio.Event(),asyncio.Event()
        async def stubborn(*args,**kw):
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            return snap
        monkeypatch.setattr(reacting.runtime,'snapshot',stubborn)
        reactor.after_post(post(store,clock),author=1,channel='general',text='Hi',his=True)
        await entered.wait()
        closing = asyncio.create_task(reactor.close())
        await asyncio.sleep(0)
        release.set()
        await closing
        assert not reactor._tasks
        await clock.advance(1000,step=1000)
        assert store.conn.execute('SELECT count(*) FROM discord_reaction').fetchone()[0]==0
    run(go())


def test_rank_change_during_final_snapshot_prevents_private_reaction(store,clock,monkeypatch):
    async def go():
        reactor,snap = setup(store,clock,monkeypatch)
        mid = post(store,clock,channel='officer-chat')
        reactor.after_post(mid,author=1,channel='officer-chat',text='Hi',his=True)
        for _ in range(8):
            await asyncio.sleep(0)
        async def demoted(*args,**kw):
            reactor.app.world.is_officer = lambda g:False
            return snap
        monkeypatch.setattr(reacting.runtime,'snapshot',demoted)
        await settle(reactor,clock)
        assert store.conn.execute('SELECT count(*) FROM discord_reaction').fetchone()[0]==0
        await reactor.close()
    run(go())
