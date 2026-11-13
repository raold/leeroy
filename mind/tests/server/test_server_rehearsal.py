"""A synthetic 26-hour day through MindApp, real schedulers, guards and transport.

The roster's availability and stock of invented topics are controlled inputs. This
measures wiring and admission, never language quality or live throughput.
"""
import asyncio
from collections import Counter
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import random
import time

from conftest import HUMAN, OFFICERS, TEST_CAST, run, seed_test_cast, unit
from dbtemplate import open_migrated
from leeroy_mind.app import MindApp
from leeroy_mind.config import load_config
from leeroy_mind.discord import layout
from leeroy_mind.discord.channels import is_discord
from leeroy_mind.discord.presence import Presence
from leeroy_mind.guards import Draft, HistoryLine, review
from leeroy_mind.llm.scheduler import Priority
from leeroy_mind.nightly import wiring
from leeroy_mind.nightly.gpu import GpuArbiter
from leeroy_mind.nightly.store import run_get
from leeroy_mind.protocol import Member
from leeroy_mind.personas.io import save_persona
from leeroy_mind.wow import RACES, CLASSES
from leeroy_mind.server import runtime, rhythm, state, engine, reacting, dms
from leeroy_mind.server.records import counts
from leeroy_mind.server.seeds import Seed, fresh
from leeroy_mind.storage.rows import pack_vec, Relationship
from leeroy_mind.storage.store import Store
from leeroy_mind.talk import budget
from leeroy_mind.talk.pipeline import TurnMeta
from leeroy_mind.talk.turns import Turn, TurnKind
from leeroy_mind.testing.fake_bridge import FakeBridge
from leeroy_mind.testing.fake_clock import FakeClock
from leeroy_mind.testing.fake_llm import FakeLLM
from leeroy_mind.tuning.effective import effective, MemberTuning
from nightly.conftest import FakeAdmin
from selfies.fakes import until
from guards.test_review import sources, NOW


START = datetime(2026,11,12,7,30,tzinfo=timezone.utc)
SERVER_GUARD_CODES = ('off_anchor', 'low_parent_and_thread_relevance', 'own_repeat_discord',
    'channel_repeat', 'filler_opener', 'server_filler', 'review_unavailable')


def guard_rates(verdicts):
    """Code frequencies across all reviewed drafts, including rewritten rechecks.

    Codes may co-occur; absence of a code is not an independently measured pass.
    """
    total=len(verdicts)
    actions=('send','rewrite','drop','react')
    overall=Counter(v.action for v in verdicts)
    codes=set(SERVER_GUARD_CODES).union(*(v.codes for v in verdicts))
    per_check={}
    for code in sorted(codes):
        counted=Counter(v.action for v in verdicts if code in v.codes)
        per_check[code]=dict(reviewed=total,flagged=sum(counted.values()),
            actions={a:counted[a] for a in actions},
            rewrite_rate=counted['rewrite']/total if total else 0.,
            drop_rate=counted['drop']/total if total else 0.)
    return dict(reviewed=total,actions={a:overall[a] for a in actions},per_check=per_check)


class MeanGapRandom(random.Random):
    """A controlled draw at the exponential mean; distribution tests live in rhythm."""
    def expovariate(self, rate):
        return 1/rate


class FakeArbiter(GpuArbiter):
    """Real lease/preemption protocol, exclusively in-memory fake administration."""
    def __init__(self, admin, settings):
        super().__init__(FakeAdmin([settings.embedding_model]), settings)
        self.events = []

    def hold(self, holder):
        self.events.append(('hold',holder))
        return super().hold(holder)

    async def release_live(self, holder):
        self.events.append(('release',holder))
        await super().release_live(holder)


class VariedLLM(FakeLLM):
    def __init__(self):
        self.words = random.Random(741)
        super().__init__(self.answer)

    async def chat(self, model, messages, **kwargs):
        if kwargs.get('format') is not None:
            from leeroy_mind.llm.client import ChatResult
            self.calls.append(dict(model=model,format=kwargs['format']))
            return ChatResult('{"memories":[]}', '', 1, 0.)
        return await super().chat(model,messages,**kwargs)

    def answer(self, model, messages):
        # No fixed prefix: lexical repeats remain enabled, as do semantic checks.
        words = 'harbor bakery lantern copper gravel basket windows footpath meadow kettle garden shore wool bridge acorn cobble amber orchard sail button porch willow plum canvas pebble ribbon'.split()
        return ' '.join(self.words.sample(words, 9))

    async def embed(self, model, inputs, *, dimensions=512, timeout=None):
        self.calls.append(dict(input=list(inputs)))
        vectors = []
        for text in inputs:
            digest = hashlib.shake_256(text.encode()).digest(dimensions)
            v = [.7] + [(1 if b & 1 else -1) * (0.51/(dimensions-1))**.5 for b in digest[1:]]
            vectors.append(v)
        return vectors


def test_whole_server_day(tmp_path, monkeypatch):
    monkeypatch.setattr(wiring, 'GpuArbiter', FakeArbiter)
    # A tiny invented roster cannot cover every region/shift. Hold availability
    # constant so the phase band measures the clock/rate rather than staffing.
    for mod in (state, engine, reacting, dms):
        monkeypatch.setattr(mod, 'presence_of', lambda *a, **k: Presence('online',False,0))
    from leeroy_mind.discord import talk
    monkeypatch.setattr(talk, 'presence_of', lambda *a, **k: Presence('online',False,0))
    observed_snapshot = runtime.snapshot
    async def stocked(app, now, *, include_seeds=False):
        snap = await observed_snapshot(app, now, include_seeds=include_seeds)
        if include_seeds:
            # Ample distinct dated source slots prevent exhausting 24 purpose seeds.
            seeds = [Seed(f'rehearsal:{snap.local.date()}:{n}', 'purpose', (channel,),
                     layout.channel(channel).topic, officers_only=layout.channel(channel).officers_only)
                     for n in range(300) for channel in ('general','food','officer-chat')]
            snap.seeds = fresh(app.store.conn, seeds, now=now)
        return snap
    monkeypatch.setattr(runtime, 'snapshot', stocked)
    result = {}
    rate_samples = []
    starts_per_hour = rhythm.starts_per_hour
    def measured_rate(local, **kwargs):
        rate = starts_per_hour(local,**kwargs)
        if kwargs['in_game']:
            normal = starts_per_hour(local,**{**kwargs,'in_game':False})
            rate_samples.append((rate,normal))
        return rate
    monkeypatch.setattr(rhythm,'starts_per_hour',measured_rate)

    async def go():
        clock = FakeClock(START, yields=64)
        cast = [dict(guid=g,name=n,race=r,**{'class':c},gender=0,level=80,
                     rank=1 if g in OFFICERS else 3,online=True,map=0,zone=0,spec_tab=-1)
                for g,(n,r,c) in TEST_CAST.items()]
        bridge = FakeBridge([dict(type='hello',server='mod-leeroy',version='0.1.0',
                                  guild_id=21,human_guid=0,ts=int(START.timestamp()*1000))], cast=cast)
        port = await bridge.start()
        conf = tmp_path/'mind.toml'
        conf.write_text(f'[bridge]\nport={port}\n[server]\nenabled=true\n'
                        '[guards]\nenabled=true\n[claims]\nenabled=true\n'
                        '[talk]\nchattiness=4\nopeners_per_hour=0\n', encoding='utf-8')
        store = Store(open_migrated(tmp_path/'mind.db'),clock=clock)
        people = seed_test_cast(store)
        for guid in OFFICERS:
            p = people[guid]
            save_persona(store,replace(p,play=replace(p.play,guild_role='officer')),
                         source='seed',ts=clock.now(),race=RACES[TEST_CAST[guid][1]],
                         class_name=CLASSES[TEST_CAST[guid][2]])
        for guid in TEST_CAST:
            store.put_relationship(Relationship(guid,HUMAN,affinity=65))
        store.set_meta('human_guid','1')
        store.upsert_cast([Member(guid=1,name='Testmage',race=10,class_=8,gender=0,level=80,
                                  rank=0,online=False,map=0,zone=0,spec_tab=-1)],clock.now())
        store.put_relationship(Relationship(105,102,affinity=-40))
        store.close()
        llm = VariedLLM()
        cfg = load_config(conf)
        llm.set_models({cfg.models.live,cfg.models.embedding,cfg.models.reflection})
        app = MindApp(cfg,tmp_path,llm=llm,clock=clock,rng_seed=53)
        verdicts=[]
        original_check=app.engine.live_review.check
        async def measured_check(turn,meta,text):
            verdict=await original_check(turn,meta,text)
            if meta.why.startswith('server:'):
                verdicts.append(verdict)
            return verdict
        monkeypatch.setattr(app.engine.live_review,'check',measured_check)
        app.server.engine.rng = MeanGapRandom(19)
        # Game autonomous chatter has separate pace tests; keep this rehearsal's
        # bridge output attributable to the explicit turns under observation.
        async def no_game_bursts(now):
            pass
        monkeypatch.setattr(app.engine,'tick',no_game_bursts)
        # The fake night exercises the production scheduler's persisted pause/resume.
        nights = []
        async def night(key, window):
            nights.append(('start',key,clock.local().isoformat()))
            try:
                await clock.sleep(60 if clock.local().hour == 7 else 2.5*3600)
            except asyncio.CancelledError:
                nights.append(('pause',key,clock.local().isoformat()))
                raise
            nights.append(('done',key,clock.local().isoformat()))
        app.night_scheduler.run_night = night
        starts = []
        queued = []
        original_queue = app.engine._queue
        def queue(items):
            queued.extend((t.channel,t.priority,m.why) for t,m in items)
            original_queue(items)
        monkeypatch.setattr(app.engine,'_queue',queue)
        original_finish = app.server.engine._finished
        def finish(c):
            if c.id in app.server.engine._live:
                starts.append((c.started,c.channel,tuple(c.cast),len(c.messages),c.reason))
            original_finish(c)
        app.server.engine._finished = finish
        task = asyncio.create_task(app.run())
        held_tasks = []
        async def advance_to(hours, minutes=0, *, step=10):
            target = START.replace(hour=0,minute=0).timestamp()+hours*3600+minutes*60
            while clock.now()<target:
                await clock.advance(min(step,target-clock.now()),step)
                # Real SQLite read workers and the loopback dispatcher must settle.
                await asyncio.sleep(.001)
        try:
            await until(lambda: app.hello is not None)
            await until(lambda: (tmp_path/'control.json').exists())
            control_port = json.loads((tmp_path/'control.json').read_text())['port']
            assert control_port != port and control_port not in FakeBridge.AVOID_PORTS
            assert port not in FakeBridge.AVOID_PORTS
            await advance_to(7,59)
            assert not app.server.lease.held and not starts and not app.server.engine.live()
            await advance_to(8)
            await app.server.lease.handback()
            assert app.server.lease.ready() and 'server' in app.arbiter.holders
            await advance_to(19)
            await bridge.push(dict(type='login',guid=HUMAN,human=True,ts=int(clock.now()*1000)))
            await until(lambda: app.world.human_online)
            await bridge.push(dict(type='group',leader_guid=HUMAN,raid=False,members=[HUMAN,101,103],
                                   ts=int(clock.now()*1000)))
            await until(lambda: app.world.group is not None)
            entered, release = asyncio.Event(), asyncio.Event()
            async def occupied(llm):
                entered.set()
                await release.wait()
            blocker = asyncio.create_task(app.scheduler.submit(Priority.EXTRACTION,occupied))
            held_tasks.append(blocker)
            await entered.wait()
            background = asyncio.create_task(app.embedder.embed(['queued server review'],Priority.BACKGROUND))
            held_tasks.append(background)
            await until(lambda: app.scheduler.pending()[Priority.BACKGROUND]>0)
            await bridge.push(dict(type='chat',channel='guild',from_guid=HUMAN,from_name='Testmage',
                to_guid=0,text='Belra, how is the harbor plan?',map=0,zone=0,ts=int(clock.now()*1000)))
            await until(lambda: any(c=='guild' and w.startswith('reply:') for c,p,w in queued))
            # Real read workers need finer fake-clock steps inside the game's
            # short deadline, especially while the broad suite shares the CPU.
            await advance_to(19,2,step=1)
            his_line = app.store.conn.execute('SELECT id FROM chat_line WHERE channel="guild" '
                                              'AND origin="human" ORDER BY id DESC LIMIT 1').fetchone()
            assert his_line and app.engine._answer_lines.get(his_line[0])
            assert not background.done()  # his guild answer passed the held low slot
            release.set()
            await asyncio.gather(blocker,background)
            # His explicit Discord post goes through the desk and D1's queue.
            before_game = app.engine._login_at
            post = await app.discord.handle(dict(action='post',channel='general',text='@Belra harbor picnic?'))
            assert post['ok']
            await advance_to(19,12)
            assert app.engine._login_at == before_game
            assert app.store.conn.execute('SELECT id FROM discord_message WHERE reply_to=? AND origin="mind"',
                                          (post['id'],)).fetchone()
            await advance_to(21)
            await bridge.push(dict(type='logout',guid=HUMAN,human=True,ts=int(clock.now()*1000)))
            await until(lambda: not app.world.human_online)
            await advance_to(27,1)
            assert not app.server.lease.held and app.night_scheduler.running is not None
            await advance_to(29)
            post = await app.discord.handle(dict(action='post',channel='general',text='@Belra harbor breakfast?'))
            assert post['ok']
            await app.discord_talk.handback()
            assert 'discord' in app.arbiter.holders and app.night_scheduler.running is None
            await advance_to(29,15)
            assert app.store.conn.execute('SELECT id FROM discord_message WHERE reply_to=? AND origin="mind"',
                                          (post['id'],)).fetchone()
            assert app.night_scheduler.running is not None
            await advance_to(32,1)
            assert app.server.lease.ready() and 'server' in app.arbiter.holders
            await advance_to(33,30)
            await app.server.engine.close()
            # Force one eligible, dramatic rival's draw against a real stored
            # post. Otherwise an empty rival-reaction subset could pass below.
            parent = app.store.conn.execute('SELECT id,text FROM discord_message WHERE author=102 '
                'AND channel="general" AND deleted IS NULL ORDER BY id DESC LIMIT 1').fetchone()
            assert parent is not None
            original_get = app.personas.get
            class RivalDraw(random.Random):
                def random(self):
                    return 0.
                def uniform(self, low, high):
                    return 0.  # write now, preserving production eligibility checks
                def shuffle(self, candidates):
                    candidates.sort(key=lambda guid: (guid!=105,guid))
            with monkeypatch.context() as probe:
                probe.setattr(app.personas,'get',lambda guid: effective(original_get(guid),
                    MemberTuning({'drama':7})) if guid==105 else original_get(guid))
                probe.setattr(app.server.reactions,'rng',RivalDraw(1))
                snapshot = await runtime.snapshot(app,clock.now())
                assert snapshot.ties[105,102]=='rival'
                assert reacting._allowed(snapshot,105,102,'general')
                assert app.server.reactions._live_allowed(105,102,'general','🙄',clock.now())
                before = set(app.server.reactions._tasks)
                await app.server.reactions._plan(parent['id'],102,'general',parent['text'],False)
                await asyncio.gather(*(app.server.reactions._tasks-before))
                assert app.store.conn.execute('SELECT emoji FROM discord_reaction '
                    'WHERE message=? AND member=105',(parent['id'],)).fetchone()[0]=='🙄'
            result['nights'] = nights
            result['starts'] = starts
            result['counts'] = {day:counts(app.store,day) for day in ('2026-11-12','2026-11-13')}
            assert sum(c['dms'] for c in result['counts'].values()) > 0
            rows = app.store.conn.execute('SELECT channel,author,ts,reply_to FROM discord_message WHERE origin="mind"').fetchall()
            result['messages'] = len(rows)
            result['guards'] = guard_rates(verdicts)
            assert result['guards']['reviewed'] > 0
            assert sum(result['guards']['actions'].values()) == len(verdicts)
            assert result['guards']['actions']['send'] > 0
            assert rows
            assert any(r['channel']=='officer-chat' for r in rows)
            assert all(r['author'] in OFFICERS for r in rows if r['channel']=='officer-chat')
            assert all(not (19<= (r['ts']-START.replace(hour=0,minute=0).timestamp())/3600 <21)
                       for r in rows if layout.dm_peer(r['channel']) is not None)
            assert all(c['dms']<=app.server.config.dms_per_day for c in result['counts'].values())
            reactions = app.store.conn.execute('SELECT member,emoji,message FROM discord_reaction').fetchall()
            result['reactions'] = len(reactions)
            assert reactions
            rival_reactions = [r for r in reactions
                if app.store.conn.execute('SELECT author FROM discord_message WHERE id=?',(r['message'],)).fetchone()[0]==102
                and r['member']==105]
            result['rival_reactions'] = len(rival_reactions)
            assert rival_reactions and all(r['emoji'] not in reacting.LOVE for r in rival_reactions)
            assert all(r['session_id'] is None for r in app.store.conn.execute('SELECT channel,session_id FROM chat_line')
                       if is_discord(r['channel']))
            game = app.store.conn.execute('SELECT text FROM chat_line WHERE channel="guild" AND origin="mind"').fetchall()
            assert game  # his explicit guild question was answered through the game path
            game_text = {r[0] for r in game}
            said = [c for c in bridge.commands if c['type'] in ('say','owner_say','emote')]
            assert said and all(c['type']=='say' and c['text'] in game_text for c in said)
            discord_text = {r[0] for r in app.store.conn.execute('SELECT text FROM discord_message')}
            assert all(c['text'] not in discord_text for c in said)
            assert any(event[0]=='pause' for event in nights)
            assert len({event[1] for event in nights}) == 2
            assert len([event for event in nights if event[0]=='start']) == 3
            assert run_get(app.store,nights[-1][1]).status=='done'
        finally:
            for held in held_tasks:
                held.cancel()
            await asyncio.gather(*held_tasks,return_exceptions=True)
            await app.stop()
            await asyncio.wait_for(task,15)
            await bridge.stop()
        assert not (tmp_path/'control.json').exists()
        assert not app.arbiter.holders

    run(go())
    phase_counts = Counter()
    midnight = START.replace(hour=0,minute=0).timestamp()
    for at,channel,cast,messages,reason in result.pop('starts'):
        local = datetime.fromtimestamp(at,timezone.utc)
        phase_counts[str(budget.day_phase(local))] += 1
        hour = (at-midnight)/3600
        assert not (19<=hour<21) or not set(cast)&{101,103}
    expected = dict(morning=5.25,day=14.,evening=18.75,late=10.)
    result['phase_starts'] = dict(phase_counts)
    result['phase_expected'] = expected
    assert rate_samples and all(rate==normal*.5 for rate,normal in rate_samples)
    print('SERVER_DAY_SYNTHETIC '+json.dumps(result,sort_keys=True))
    assert not phase_counts['dead']
    for phase,n in expected.items():
        assert .6*n <= phase_counts[phase] <= 1.4*n, result


def test_synthetic_guard_classification_per_check(store):
    """Deliberate clean/failing pairs exercise classification, not measured rates."""
    seed_test_cast(store)
    good, bad = pack_vec(unit(0)), pack_vec(unit(1))
    text = 'A copper lantern hangs beside the invented harbor'
    old = HistoryLine(10,NOW-60,102,text,channel='discord',room='general',
                      readers=(1,101,102),embedding=good)
    base = Draft(101,'discord','Fresh wool blankets make the picnic more comfortable',
                 room='general',server=True,now=NOW,embedding=good)
    cases = {
        'off_anchor': (sources(store), [replace(base,anchor='harbor',anchor_embedding=v) for v in (good,bad)]),
        'parent_thread_relevance': (sources(store,history=(old,)),
            [replace(base,parent_id=10,embedding=v) for v in
             (pack_vec([.7*x+(1-.7**2)**.5*y for x,y in zip(unit(0),unit(2))]),bad)]),
        'own_repeat_discord': (sources(store,history=(replace(old,guid=101,room='irl'),)),
            [replace(base,embedding=bad),replace(base,text=text)]),
        'channel_repeat': (sources(store,history=(old,)),
            [replace(base,embedding=bad),replace(base,text=text)]),
        'filler_opener': (sources(store), [base,replace(base,text='lol')]),
    }
    measured = {}
    for check,(inputs,drafts) in cases.items():
        actions = Counter(review(inputs,d).action for d in drafts)
        measured[check] = dict(samples=len(drafts),actions=dict(actions),
            rewrite_rate=actions['rewrite']/len(drafts),drop_rate=actions['drop']/len(drafts))
    for key in ('off_anchor','parent_thread_relevance'):
        assert measured[key]['actions'] == {'send':1,'rewrite':1}
    for key in ('own_repeat_discord','channel_repeat','filler_opener'):
        assert measured[key]['actions'] == {'send':1,'drop':1}
    print('SERVER_GUARD_CLASSIFICATION '+json.dumps(measured,sort_keys=True))


def test_guard_report_preserves_overall_actions_and_cooccurring_codes():
    from leeroy_mind.guards.types import Verdict
    report=guard_rates([Verdict('send'),Verdict('rewrite',('off_anchor',)),
        Verdict('drop',('own_repeat_discord','channel_repeat')),
        Verdict('react',('server_filler',))])
    assert report['reviewed']==4
    assert report['actions']==dict(send=1,rewrite=1,drop=1,react=1)
    assert report['per_check']['off_anchor']['rewrite_rate']==.25
    assert report['per_check']['off_anchor']['actions']['drop']==0
    assert report['per_check']['own_repeat_discord']['drop_rate']==.25
    assert report['per_check']['channel_repeat']['drop_rate']==.25
    assert report['per_check']['filler_opener']['flagged']==0


def test_low_slot_contention_measures_guard_timeout_and_foreground_reserve(tmp_path, monkeypatch):
    """Review-259 P3-7: actual five-second wait, real queue, fake held jobs."""
    from selfies.fakes import mind_app
    app = mind_app(tmp_path,toml='[server]\nenabled=true\n[guards]\nenabled=true\n')
    app.scheduler._max = 2
    measurements = []
    async def go():
        try:
            for priority in (Priority.EXTRACTION,Priority.BACKGROUND):
                entered, release = asyncio.Event(), asyncio.Event()
                async def occupied(llm):
                    entered.set()
                    await release.wait()
                blocker = asyncio.create_task(app.scheduler.submit(priority,occupied))
                await entered.wait()
                before = time.monotonic()
                turn = Turn(TurnKind.OPEN,101,'discord:general',priority=Priority.BACKGROUND)
                meta = TurnMeta('contention',why='server:purpose',review_required=True,
                                server_anchor='harbor lanterns')
                check = asyncio.create_task(app.engine.live_review.check(turn,meta,
                    'A fresh harbor plan while the low slot is held '+str(int(priority))))
                # A human's request uses the reserved slot even with review queued.
                human_at = time.monotonic()
                foreground = await asyncio.wait_for(app.embedder.embed(['his fresh question'],Priority.REPLY_HUMAN),2)
                human_wait = time.monotonic()-human_at
                verdict = await asyncio.wait_for(check,10)
                elapsed = time.monotonic()-before
                measurements.append(dict(blocker=priority.name,guard_wait_s=round(elapsed,3),
                    human_wait_s=round(human_wait,3),action=verdict.action,codes=verdict.codes))
                assert foreground and verdict.action=='drop' and verdict.codes==('review_unavailable',)
                assert 4.8<=elapsed<10 and human_wait<2
                release.set()
                await blocker
                # Timed-out embeddings leave no live pending job after cancellation.
                await asyncio.sleep(0)
                assert app.scheduler.pending()[Priority.BACKGROUND] == 0
        finally:
            await app.stop()
    run(go())
    print('SERVER_CONTENTION_SYNTHETIC '+json.dumps(measurements,sort_keys=True))
