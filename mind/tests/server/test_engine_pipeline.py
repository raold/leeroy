"""A real server conversation through the talk pipeline, with fake model vectors."""
import asyncio
import json
import random
from types import SimpleNamespace

import pytest

from engine_rig import engine_clock, make_rig
from leeroy_mind.server import rhythm
from leeroy_mind.server.config import ServerConfig
from leeroy_mind.server.engine import ServerEngine
from leeroy_mind.server.records import counts
from leeroy_mind.server.seeds import Seed
from leeroy_mind.storage.rows import pack_vec


class SteadyRandom(random.Random):
    def random(self):
        return .5  # deterministic selection and no ten-minute late reply in this test


@pytest.mark.parametrize('learning', [False, True])
@pytest.mark.parametrize('kind,drop', [('purpose',False),('life',False),('his_day',False),
                                      ('life',True),('his_day',True)])
def test_real_server_engine_posts_or_drops_guarded_openers_without_game_commands(store, tmp_path, monkeypatch,kind,drop,learning):
    (tmp_path/'mind.toml').write_text('[guards]\nenabled=true\n', encoding='utf-8')
    clock = engine_clock()
    rig = make_rig(store, clock, tmp_path)
    if learning:
        from leeroy_mind.learn import arms, bandit
        from leeroy_mind.learn.config import LearnCfg
        rig.engine.learn_enabled=True
        rig.engine.learn_settings=LearnCfg(enabled=True,bandit=True,apply=True)
        def choose_arms(conn, guid, context, **kwargs):
            arms.ensure(conn)
            return arms.controls() | {'length':'length.1.v1', 'questions':'questions.1.v1'}
        monkeypatch.setattr(bandit,'choose',choose_arms)
    rig.world.human_online = False
    rig.script(
        'the harbor walk tomorrow sounds worthwhile if the weather holds',
        'I can bring a thermos along and meet everyone near the old bridge',
        'that path gets muddy after rain so sturdy shoes would help',
        'lets take the shorter route before lunch and leave time for the bakery',
        'a warm cinnamon roll after all that walking would be a fine reward')
    if drop:
        rig.script('lol')
    # Each distinct string has a shared topical component and its own axis:
    # related enough for relevance, distinct enough for repetition, all fake.
    vocabulary, batches = {}, []
    async def embed(texts, priority):
        batches.append(tuple(texts))
        output = []
        for text in texts:
            vocabulary.setdefault(text,len(vocabulary)+1)
            vector = [0.0]*512
            vector[0],vector[vocabulary[text]] = .7,(1-.7**2)**.5
            output.append(pack_vec(vector))
        return output
    monkeypatch.setattr(rig.engine.recall._embedder,'embed',embed)
    config = ServerConfig(enabled=True,reactions=False,dms_per_day=0)
    app = SimpleNamespace(engine=rig.engine,world=rig.world,store=store,clock=clock,
        personas=rig.engine.personas,human_guid=1,guild=None,realm=None,data_dir=tmp_path,
        stopping=False,rng=SteadyRandom(5),server=SimpleNamespace(lease=SimpleNamespace(ready=lambda:True),
                                                              reactions=None))
    rig.engine.server_app = app
    service = ServerEngine(app,config,app.rng)
    app.server.engine = service
    chosen = []
    undisclosed = 'An unpublished detail about the harbor plan'
    def choose(seeds, **kwargs):
        seed = (next(s for s in seeds if s.kind=='purpose' and s.channels==('general',))
                if kind=='purpose' else Seed('private:1',kind,('general',),undisclosed,
                    (101,) if kind=='life' else (1,),witnesses=(101,)))
        chosen.append(seed)
        return seed,'general'
    monkeypatch.setattr(rhythm,'choose_seed_and_channel',choose)
    monkeypatch.setattr(rhythm,'length',lambda rng:5)
    async def run():
        try:
            await service.tick(clock.now())
            assert service.live()
            for _ in range(600):
                await rig.advance(1)
                await asyncio.sleep(.003)  # bounded read workers finish between fake-clock steps
                if not service.live():
                    break
            assert not service.live()
        finally:
            await service.close()
    try:
        rig.sync(run())
        rows = store.conn.execute('SELECT id,author,reply_to,turn FROM discord_message ORDER BY id').fetchall()
        history = json.loads(store.get_meta('server:recent') or '[]')
        dropped = [tuple(r) for r in store.conn.execute('SELECT dropped,guard_verdict FROM utterance')]
        requests = [call for call in rig.llm.calls if 'messages' in call]
        if drop:
            assert not rows
            assert history[-1]['reason']=='opener_dropped'
            assert len(dropped)==1 and dropped[0][1]=='drop', dropped
            assert len(requests)==1
            assert store.get_meta('server:seed:'+chosen[0].key) is not None
            return
        assert len(rows)==5, (history,dropped)
        if learning:
            recipes=store.conn.execute('SELECT arm_ids FROM talk_recipe').fetchall()
            assert len(recipes)==5
            assert all('length.control.v1' in json.loads(r[0]) for r in recipes)
            assert all('questions.1.v1' in json.loads(r[0]) for r in recipes)
            assert all('Your recent style notes' in r['messages'][-1]['content'] for r in requests)
        if kind != 'purpose':
            assert rows[0]['author']==101
            assert undisclosed in json.dumps(requests[0]['messages'])
            assert all(undisclosed not in json.dumps(call['messages']) for call in requests[1:])
        assert rows[0]['reply_to'] is None
        for i,row in enumerate(rows[1:],1):
            assert row['reply_to'] in {r['id'] for r in rows[:i]}
            assert row['author'] != rows[i-1]['author']
        assert len({r['turn'] for r in rows}) == 5
        assert history[-1]['reason']=='length' and history[-1]['messages']==5
        assert store.get_meta('server:seed:'+chosen[0].key) is not None
        assert counts(store,clock.local().date().isoformat()) == {
            'conversations':1,'messages':5,'reactions':0,'dms':0}
        assert batches and max(map(len,batches)) <= 32
        assert all(r['guard_verdict']=='send' for r in store.conn.execute('SELECT guard_verdict FROM utterance'))
        assert not rig.sent.log
        assert not store.conn.execute('SELECT id FROM chat_line WHERE session_id IS NOT NULL').fetchall()
    finally:
        rig.close()
