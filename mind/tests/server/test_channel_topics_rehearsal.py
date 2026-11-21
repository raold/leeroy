"""A fake evening and night of the guild's Discord with [server] channel_topics on: the real server
engine, rhythm, seed routing, daily room, talk pipeline and guards, a fake model and fake
vectors. The seed pool is every channel's purpose plus running jokes, text memes, life beats
about food, work and a dog, and members' plans. The model drifts on purpose: every first draft
of a reaction in #memes is plain chat about her day.

It shows the wiring, not language quality: #memes holds only meme posts and short reactions to
them (a drifting draft is rewritten once by the guard, and never sent as plain chat), #pics
nothing (no photo was posted), #food only food, jokes only #general, and no channel goes past its
daily room. Invented cast, seeds and lines only."""
import asyncio
import json
import random
from collections import Counter, defaultdict
from datetime import timedelta
from types import SimpleNamespace

from engine_rig import engine_clock, make_rig
from leeroy_mind.discord import layout, topics as channel_topics
from leeroy_mind.server import engine as se, rhythm, runtime, seeds, topics
from leeroy_mind.server.config import ServerConfig
from leeroy_mind.server.engine import ServerEngine
from leeroy_mind.server.seeds import Seed
from leeroy_mind.storage.rows import pack_vec

HOURS = 6
WORDS = ('amber birch cobalt dapple ember fennel garnet harbor indigo jasper kestrel lantern meadow '
         'nutmeg orchid pebble quartz russet saffron thistle umber velvet willow yarrow zephyr '
         'acorn bramble cinder drizzle eclipse fable glimmer hollow ivory juniper kindle lumen '
         'mosaic nimbus opal prism quill ripple sable tundra unity vortex wander zenith').split()
DRIFT = "lol i need so much coffee after work, the kids were up all night"


class SteadyRandom(random.Random):
    def random(self):
        return .5  # no late reply: the fake clock never waits out a ten-minute gap


class DriftingModel:
    """A fake model whose first reaction to a meme is plain chat about her day; told by the
    guard that this is #memes, it reacts to the meme. Every line has words of its own, so no
    repeat check ever fires."""
    def __init__(self):
        self.words = random.Random(5)
        self.drifted = 0

    def fresh(self, n=4):
        return " ".join(self.words.sample(WORDS, n))

    def __call__(self, model, messages):
        text = "\n".join(str(m.get("content", "")) for m in messages)
        last = str(messages[-1].get("content", ""))
        if "original text meme" in text:
            return f"POV: {self.fresh(6)}"
        if "React to the meme posted here" in text:
            if "this is #memes" in last:  # the guard's rewrite hint
                return self.fresh(4)
            self.drifted += 1
            return f"{DRIFT} {self.fresh(2)}"
        if "#food" in text:
            return f"{self.fresh(2)} soup {self.fresh(3)}"
        return self.fresh(6)


def extra(now):
    """The seeds beside the real gather's (every channel's purpose): jokes, memes, life beats,
    plans."""
    return [
        Seed('joke:1', 'joke', ('general', 'memes'), 'the guild keeps joking about the warlock',
             (0,)),
        *(Seed(f'meme:raid:{n}', 'meme', ('memes',), f'Last raid: Naxxramas 10, {n} wipes')
          for n in (1, 2, 3)),
        Seed('life:1', 'life', ('irl', 'general', 'food'), 'Made lasagna for the whole family',
             (101,)),
        Seed('life:2', 'life', ('irl', 'general', 'food'), 'Long day at the office again', (102,)),
        Seed('life:3', 'life', ('irl', 'general', 'food'), 'The dog stole a sock again', (103,)),
        Seed('plan:1:w', 'plan', ('general', 'lfg-and-alts', 'professions-and-gold'),
             'farming saronite for the auction house tomorrow', (101,)),
        Seed('plan:2:w', 'plan', ('general', 'lfg-and-alts', 'professions-and-gold'),
             'trying the new raid strat on saturday', (102,)),
    ]


def test_a_day_of_discord_keeps_every_channel_on_its_topic(store, tmp_path, monkeypatch):
    (tmp_path / 'mind.toml').write_text('[guards]\nenabled=true\n', encoding='utf-8')
    clock = engine_clock()
    model = DriftingModel()
    rig = make_rig(store, clock, tmp_path, script=model)
    rig.engine._length_rng = SteadyRandom(1)
    rig.world.human_online = False
    vocabulary = {}

    async def embed(texts, priority):
        out = []
        for text in texts:
            vocabulary.setdefault(text, len(vocabulary) + 1)
            vector = [0.0] * 512
            axis = 1 + vocabulary[text] % 500
            vector[0], vector[axis] = .7, (1 - .7 ** 2) ** .5
            out.append(pack_vec(vector))
        return out
    monkeypatch.setattr(rig.engine.recall._embedder, 'embed', embed)
    start = clock.local()
    window = ((start - timedelta(hours=2)).time(), (start - timedelta(hours=1)).time())
    config = ServerConfig(enabled=True, reactions=False, dms_per_day=0, quiet_window=window,
                          conversations_per_hour=6.0)
    assert config.channel_topics
    app = SimpleNamespace(engine=rig.engine, world=rig.world, store=store, clock=clock,
                          personas=rig.engine.personas, human_guid=1, guild=None, realm=None,
                          data_dir=tmp_path, stopping=False, rng=SteadyRandom(5),
                          server=SimpleNamespace(lease=SimpleNamespace(ready=lambda: True),
                                                 reactions=None, config=config))
    rig.engine.server_app = app
    service = ServerEngine(app, config, app.rng)
    app.server.engine = service
    monkeypatch.setattr(service, '_joke_valid', lambda c, s: True)  # culture's own gate
    monkeypatch.setattr(rhythm, 'length', lambda rng: 4)
    monkeypatch.setattr(se, 'answer_delay', lambda state, rng: 2)
    real_gather = seeds.gather
    monkeypatch.setattr(seeds, 'gather', lambda conn, **kw: real_gather(conn, **kw) + extra(kw['now']))
    draws, choose = random.Random(11), rhythm.choose_seed_and_channel
    opened = []

    def chosen(pool, **kw):
        out = choose(pool, **{**kw, 'rng': draws})  # the real weights, a real draw
        if out is not None:
            opened.append((out[0].kind, out[0].key, out[1]))
        return out
    monkeypatch.setattr(rhythm, 'choose_seed_and_channel', chosen)
    end = clock.now() + HOURS * 3600

    async def day():
        try:
            while clock.now() < end:
                await service.tick(clock.now())
                for _ in range(900):
                    if not service.live():
                        break
                    await rig.advance(1)
                    await asyncio.sleep(.002)  # bounded read workers finish between steps
                assert not service.live()
                await rig.advance(max(60.0, service.next_start - clock.now()), 30)
        finally:
            await service.close()
    rig.sync(day())
    try:
        posts = [dict(r) for r in store.conn.execute(
            "SELECT id,channel,author,reply_to,text FROM discord_message WHERE origin='mind' "
            "ORDER BY id")]
        by_channel = defaultdict(list)
        for p in posts:
            by_channel[p['channel']].append(p)
        kinds = Counter((k, c) for k, _, c in opened)
        ended = json.loads(store.get_meta('server:recent') or '[]')
        dropped = Counter(r[0] for r in store.conn.execute(
            "SELECT dropped FROM utterance WHERE channel='discord:memes'"))
        summary = json.dumps({'posts': {c: len(v) for c, v in by_channel.items()},
                              'opened': Counter(f'{k}@{c}' for k, _, c in opened),
                              'drifted': model.drifted, 'memes_utterances': dropped,
                              'ended': Counter(f"{e['seed']}@{e['channel']}:{e['reason']}"
                                               for e in ended)})
        print('CHANNEL_TOPICS_DAY', summary)
        assert len(opened) >= 10, summary
        # #memes: only memes opened there, a meme post and at most three short reactions each
        assert {k for k, c in kinds if c == 'memes'} == {'meme'}, summary
        memes = by_channel['memes']
        assert memes, summary
        posts_of = {p['id']: p for p in memes}
        roots = [p for p in memes if p['reply_to'] is None]
        assert all(channel_topics.meme_format(p['text']) for p in roots)
        for p in memes:
            if p['reply_to'] is not None:
                parent = posts_of[p['reply_to']]
                root = parent if parent['reply_to'] is None else posts_of[parent['reply_to']]
                assert root in roots
                assert len(p['text'].split()) <= channel_topics.MEME_WORDS
                assert 'coffee' not in p['text'] and 'work' not in p['text']  # never sent drift
        replies = Counter(p['reply_to'] for p in memes if p['reply_to'] in {r['id'] for r in roots})
        assert replies and all(n <= topics.MEME_REPLIES[1] for n in replies.values())
        assert model.drifted >= 1  # the model did drift, and the guard kept it out
        # no picture was posted: nothing in #pics; jokes only in #general
        assert not by_channel['pics'] and ('joke', 'memes') not in kinds
        # #food only from food: its own purpose, and the life beat about cooking
        assert {key for k, key, c in opened if c == 'food'} <= {'life:1'} | {
            key for k, key, c in opened if key.startswith('purpose:food:')}
        assert not any(key == 'life:2' and c == 'food' for k, key, c in opened)
        assert not any(key == 'plan:2:w' and c != 'general' for k, key, c in opened)
        # every channel within its daily room
        for cid, rows in by_channel.items():
            if layout.channel(cid) is not None:
                assert len(rows) <= topics.daily(cid), (cid, summary)
        # the reactions in #memes were told what the channel is about
        prompts = rig.prompts()
        reactions = [x for x in prompts if 'React to the meme posted here' in x]
        assert reactions and all(channel_topics.hold_text('memes') in x for x in reactions)
        # #general never gets a hold line
        general = [x for x in prompts if "You're in #general" in x]
        assert general and not any('Stay on what this channel is about' in x for x in general)
        assert not rig.sent.log  # nothing reached the game
    finally:
        rig.close()
