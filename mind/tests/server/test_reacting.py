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
        reactor,snap = many(store,clock,monkeypatch,20,rng=random.Random(1))
        snap.ties = {(201,1):'friend'}  # 202-220 neutral
        for _ in range(500):
            mid = post(store,clock)
            reactor.after_post(mid,author=1,channel='discord:general',text='That raid was fun',his=False)
        await settle(reactor,clock)
        counts = dict(store.conn.execute('SELECT member,count(*) FROM discord_reaction GROUP BY member'))
        neutral = sum(counts.get(g,0) for g in range(202,221))/19
        assert counts[201]>neutral*1.7
        assert json.loads(store.get_meta('server:day:'+clock.local().date().isoformat()))['reactions']==sum(counts.values())
        await reactor.close()
    run(go())


def many(store,clock,monkeypatch,n,*,rng):
    """A reactor whose snapshot holds `n` neutral members (201...), all online."""
    reactor,snap = setup(store,clock,monkeypatch,rng=rng)
    people = {g:make_persona(g,'Member'+str(g)) for g in range(201,201+n)}
    snap.personas = snap.cast = people
    snap.states = {g:SimpleNamespace(presence=Presence('online',False,0)) for g in people}
    reactor.app.personas = people
    cast = {g:SimpleNamespace(zone=0) for g in people}
    reactor.app.world.discord_members = lambda:cast
    reactor.app.world.online = set(people)
    return reactor,snap


def reaction_counts(store):
    per = dict(store.conn.execute('SELECT message,count(*) FROM discord_reaction GROUP BY message'))
    return [per.get(m,0) for (m,) in store.conn.execute(
        "SELECT id FROM discord_message WHERE origin='mind' ORDER BY id")]


def test_most_messages_get_none_or_one_and_a_rare_one_many(store,clock,monkeypatch):
    """62% of member messages carried exactly 3 reactions (2026-11-15): with 60 members
    online each reacting at 12%, every message hit the cap. A message's count is now drawn
    once, skewed, whatever the crowd."""
    async def go():
        reactor,_ = many(store,clock,monkeypatch,60,rng=random.Random(8))
        for i in range(600):
            mid = post(store,clock,author=202,text='I fixed the kitchen tap this morning')
            reactor.after_post(mid,author=202,channel='general',text='I fixed the kitchen tap this morning',his=False)
        await settle(reactor,clock)
        counts = reaction_counts(store)
        share = {k: counts.count(k)/len(counts) for k in range(reacting.MAX_PER_MESSAGE+1)}
        assert share[0]+share[1] > .7 and share[0] > .4
        assert .1 < sum(share[k] for k in (2,3,4)) < .35
        assert share[3] < .1 and max(counts) <= reacting.MAX_PER_MESSAGE
        assert sum(share[k] for k in range(5,reacting.MAX_PER_MESSAGE+1)) < .06
        await reactor.close()
    run(go())


def test_a_joke_a_win_or_a_meme_draws_more_than_a_question(store,clock,monkeypatch):
    plain = reacting.salience('I fixed the kitchen tap this morning')
    assert reacting.salience('lmao the tank pulled the whole room') > 1.5*plain
    assert reacting.salience('Finally downed Sindragosa!') > 1.5*plain
    assert reacting.salience('look at this', attachment='meme') > 2*plain
    assert reacting.salience('anyone know where the trainer is?') < plain
    assert reacting.salience('ok sure') < plain
    assert reacting.salience('I fixed the kitchen tap this morning', his=True) == 2*plain
    assert reacting.salience('lmao finally downed him, look', his=True, attachment='meme') == reacting.MAX_SALIENCE
    async def go():
        reactor,_ = many(store,clock,monkeypatch,40,rng=random.Random(4))
        texts = {'plain':'I fixed the kitchen tap this morning','joke':'lmao the tank pulled the whole room'}
        made = {k:[] for k in texts}
        for _ in range(300):
            for kind,text in texts.items():
                mid = post(store,clock,author=202,text=text)
                made[kind].append(mid)
                reactor.after_post(mid,author=202,channel='general',text=text,his=False)
        await settle(reactor,clock)
        per = dict(store.conn.execute('SELECT message,count(*) FROM discord_reaction GROUP BY message'))
        mean = {k:sum(per.get(m,0) for m in ids)/len(ids) for k,ids in made.items()}
        assert mean['joke'] > 1.4*mean['plain']
        await reactor.close()
    run(go())


def test_the_count_draw_is_skewed_and_capped():
    draws = [reacting.draw_count(i/10000, reacting.BASE_MEAN) for i in range(10000)]
    assert abs(sum(draws)/len(draws) - reacting.BASE_MEAN) < .15
    assert max(draws) == reacting.MAX_PER_MESSAGE == reacting.draw_count(0, reacting.BASE_MEAN)
    assert reacting.draw_count(.999999, 5.0) == 0 and reacting.draw_count(.5, 0) == 0
    assert reacting.draw_count(0, 1.0, cap=2) == 2
    assert [reacting.draw_count(u, 1.0) for u in (.9, .5, .2, .05)] == sorted(
        [reacting.draw_count(u, 1.0) for u in (.9, .5, .2, .05)])  # read from the top


def test_a_rival_never_hearts_and_a_feud_never_reacts():
    rng = random.Random(3)
    for _ in range(500):
        for text in ('You look great','Ugh my boss','Haha','Nice gear'):
            assert reacting.pick_emoji(text,'rival',rng)=='🙄'
            assert reacting.pick_emoji(text,'feud',rng) is None
            assert reacting.pick_emoji(text,'rival',rng) not in reacting.LOVE


def test_a_popular_message_is_capped(store,clock,monkeypatch):
    async def go():
        reactor,_ = many(store,clock,monkeypatch,20,rng=Certain(1))  # the draw's top: the cap
        mid = post(store,clock,author=202)
        reactor.after_post(mid,author=202,channel='general',text='That raid was fun',his=False)
        await settle(reactor,clock)
        rows = store.conn.execute('SELECT member,emoji FROM discord_reaction WHERE message=?',(mid,)).fetchall()
        assert len(rows)==reacting.MAX_PER_MESSAGE and 202 not in {r[0] for r in rows}
        assert len({r[1] for r in rows})==1  # they piled onto one emoji
        await reactor.close()
    run(go())


def test_reaction_draw_needs_no_feud_and_rival_needs_drama(store,clock,monkeypatch):
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
        snap.ties = {(g,1):'fond' for g in snap.personas}
        before = store.conn.execute('SELECT count(*) FROM discord_reaction').fetchone()[0]
        for _ in range(500):
            reactor.after_post(post(store,clock),author=1,channel='general',text='That raid was fun',his=his)
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
        assert store.conn.execute('SELECT count(*) FROM discord_reaction').fetchone()[0]==10  # 5 each
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
