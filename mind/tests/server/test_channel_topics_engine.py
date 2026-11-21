"""[server] channel_topics in the server engine: conversations open only where their subject
fits, #memes holds only a meme post and a few short reactions to it, and no channel floods.
Fake speech only (the pipeline is replaced); invented text."""
import asyncio
import json
import random
from dataclasses import replace

import pytest

from conftest import run
from selfies.fakes import mind_app
from leeroy_mind.discord import store as dstore
from leeroy_mind.server import engine as se, records, runtime, topics
from leeroy_mind.server.seeds import Seed
from leeroy_mind.talk.turns import TurnKind


@pytest.fixture
def app(tmp_path, monkeypatch):
    app = mind_app(tmp_path, toml='[server]\nenabled=true\n[guards]\nenabled=true\n')
    app.server.lease._held = True
    app.engine._login_at = float('-inf')
    app.world.human_online = False
    app.server.engine.rng = random.Random(19)
    observed = run(runtime.snapshot(app, app.clock.now()))
    assert observed.posted == {}

    async def snapshot(*args, **kwargs):
        await asyncio.sleep(0)
        observed.posted = topics.posted_today(app.store.conn, 0.0)  # as the worker counts them
        return observed
    monkeypatch.setattr(runtime, 'snapshot', snapshot)
    monkeypatch.setattr(se.rhythm, 'length', lambda rng: 8)
    monkeypatch.setattr(se, 'answer_delay', lambda state, rng: 0)
    app.server.engine.rng.random = lambda: .5
    app.observed, app.turns = observed, []

    async def speak(turn, meta):
        app.turns.append((turn, meta))
        meta.turn_id = 'test-' + str(len(app.turns))
        with app.store.transaction():
            message = dstore.post(app.store, channel=turn.channel.removeprefix('discord:'),
                                  author=turn.speaker, origin='mind',
                                  text='An invented line ' + str(len(app.turns)),
                                  turn=meta.turn_id, reply_to=meta.discord.reply_to,
                                  now=app.clock.now())
            records.posted(app.store, app.clock.local().date().isoformat(), app.clock.now(),
                           conversation=meta.server_conversation)
        meta.discord_messages.append(message)
        return ['An invented line']
    monkeypatch.setattr(app.engine, 'speak_meta', speak)
    yield app
    run(app.stop())


async def finished(engine):
    for _ in range(400):
        if not engine.live():
            return
        await asyncio.sleep(0)
    pytest.fail('conversation did not end')


def converse(app):
    async def go():
        app.server.engine.next_start = 0
        await app.server.engine.tick(app.clock.now())
        await finished(app.server.engine)
    run(go())


def rows(app):
    return [dict(r) for r in app.store.conn.execute(
        'SELECT id,channel,author,reply_to FROM discord_message ORDER BY id')]


def recent(app):
    return json.loads(app.store.get_meta('server:recent') or '[]')


def test_a_joke_opens_in_general_never_in_memes(app, monkeypatch):
    seen = []
    original = se.rhythm.choose_seed_and_channel

    def spy(seeds, **kw):
        seen.extend(seeds)
        return original(seeds, **kw)
    monkeypatch.setattr(se.rhythm, 'choose_seed_and_channel', spy)
    monkeypatch.setattr(app.server.engine, '_joke_valid', lambda c, s: True)
    app.observed.seeds = [Seed('joke:1', 'joke', ('general', 'memes'), 'a running joke')]
    converse(app)
    assert [s.channels for s in seen] == [('general',)]
    assert {r['channel'] for r in rows(app)} == {'general'}


def test_a_channel_purpose_opens_nothing_in_memes_or_pics(app):
    app.observed.seeds = [Seed('purpose:memes:d', 'purpose', ('memes',), 'Memes'),
                          Seed('purpose:pics:d', 'purpose', ('pics',), 'Pictures')]
    converse(app)
    assert not app.turns and not rows(app)


def test_memes_is_a_meme_post_and_a_few_short_reactions_to_it(app, monkeypatch):
    monkeypatch.setattr(app.server.engine, '_joke_valid', lambda c, s: True)  # culture's own gate
    app.observed.seeds = [Seed('meme:raid:1', 'meme', ('memes',), 'Last raid: two wipes')]
    converse(app)
    posts = rows(app)
    assert {p['channel'] for p in posts} == {'memes'}
    meme = posts[0]
    assert meme['reply_to'] is None
    assert 2 <= len(posts) <= 1 + topics.MEME_REPLIES[1]
    # every reaction answers the meme; only its own poster, who never answers herself, answers
    # a reaction to it instead
    assert all(p['reply_to'] == meme['id'] or p['author'] == meme['author'] for p in posts[1:])
    assert posts[1]['reply_to'] == meme['id']
    opener, *replies = [t for t, m in app.turns]
    assert opener.kind is TurnKind.OPEN and 'meme format' in opener.instruction
    assert 'POV:' in opener.instruction
    for turn in replies:
        assert turn.kind is TurnKind.CONTINUE
        assert 'React to the meme posted here in a few words' in turn.instruction
        assert turn.discord_draw is not None and turn.discord_draw < 0.55  # a quick message
    assert recent(app)[-1]['reason'] == 'length'


def test_a_life_beat_about_food_may_open_in_food_and_one_about_work_never(app, monkeypatch):
    chosen = []
    original = se.rhythm.choose_seed_and_channel

    def spy(seeds, **kw):
        chosen.append(tuple(s.channels for s in seeds))
        return original(seeds, **kw)
    monkeypatch.setattr(se.rhythm, 'choose_seed_and_channel', spy)
    app.observed.seeds = [Seed('life:1', 'life', ('irl', 'general', 'food'),
                               'Long day at the office again', (101,))]
    converse(app)
    assert chosen == [(('irl', 'general'),)]


def test_no_conversation_opens_in_a_full_channel(app):
    with app.store.transaction():
        for _ in range(topics.daily('irl') - topics.MIN_ROOM + 1):
            dstore.post(app.store, channel='irl', author=101, origin='mind',
                        text='an invented line', now=app.clock.now())
    app.observed.seeds = [Seed('purpose:irl:d', 'purpose', ('irl',), 'Real life')]
    converse(app)
    assert not app.turns


def test_a_conversation_stops_when_its_channel_fills(app):
    with app.store.transaction():
        for _ in range(topics.daily('irl') - 4):
            dstore.post(app.store, channel='irl', author=101, origin='mind',
                        text='an invented line', now=app.clock.now())
    app.observed.seeds = [Seed('purpose:irl:d', 'purpose', ('irl',), 'Real life')]
    converse(app)
    assert len(app.turns) == 4  # the room left, not the drawn length of 8
    assert app.store.conn.execute("SELECT count(*) FROM discord_message WHERE channel='irl'"
                                  ).fetchone()[0] == topics.daily('irl')


def test_a_running_conversation_ends_channel_full(app, monkeypatch):
    original = app.engine.speak_meta

    async def busy(turn, meta):
        result = await original(turn, meta)
        if len(app.turns) == 2:  # D1 answers and others fill the channel meanwhile
            with app.store.transaction():
                for _ in range(topics.daily('food')):
                    dstore.post(app.store, channel='food', author=102, origin='mind',
                                text='an invented line', now=app.clock.now())
        return result
    monkeypatch.setattr(app.engine, 'speak_meta', busy)
    app.observed.seeds = [Seed('purpose:food:d', 'purpose', ('food',), 'Food')]
    converse(app)
    assert len(app.turns) == 2 and recent(app)[-1]['reason'] == 'channel_full'


def test_his_intents_go_where_he_said_whatever_the_room(app, monkeypatch):
    monkeypatch.setattr(app.server.engine, '_valid', lambda c, g, p=None: True)
    with app.store.transaction():
        for _ in range(topics.daily('memes')):
            dstore.post(app.store, channel='memes', author=101, origin='mind',
                        text='an invented line', now=app.clock.now())
    chosen = []
    original = se.rhythm.choose_seed_and_channel

    def spy(seeds, **kw):
        chosen.append(tuple((s.kind, s.channels) for s in seeds))
        return None
    monkeypatch.setattr(se.rhythm, 'choose_seed_and_channel', spy)
    app.observed.seeds = [Seed('intent:1:post', 'intent', ('memes',), 'his ask', (101,))]
    converse(app)
    assert chosen == [(('intent', ('memes',)),)]


def test_off_keeps_the_old_routing(app, monkeypatch):
    app.server.engine.config = replace(app.server.engine.config, channel_topics=False)
    seen = []
    original = se.rhythm.choose_seed_and_channel

    def spy(seeds, **kw):
        seen.extend(seeds)
        return original(seeds, **kw)
    monkeypatch.setattr(se.rhythm, 'choose_seed_and_channel', spy)
    app.observed.seeds = [Seed('purpose:memes:d', 'purpose', ('memes',), 'Memes')]
    converse(app)
    assert [s.channels for s in seen] == [('memes',)]
    assert len(app.turns) == 8  # the drawn length, as before
    assert all('React to the meme' not in t.instruction for t, m in app.turns)


def test_a_burst_is_drawn_only_where_the_channel_has_room_for_it(app, monkeypatch):
    # [talk] realism's burst sends 2-3 posts for one turn (or 2 and a correction): never into
    # the last posts of a channel's day
    app.engine.cfg = replace(app.engine.cfg, talk=replace(app.engine.cfg.talk, realism=True))
    monkeypatch.setattr(se, 'BURST_SHARE', 1.0)
    with app.store.transaction():
        for _ in range(topics.daily('irl') - 5):
            dstore.post(app.store, channel='irl', author=101, origin='mind',
                        text='an invented line', now=app.clock.now())
    app.observed.seeds = [Seed('purpose:irl:d', 'purpose', ('irl',), 'Real life')]
    converse(app)
    # room 5: the opener, then replies with room 4, 3 (a burst fits) and 2, 1 (it does not)
    assert [m.burst for t, m in app.turns] == [False, True, True, False, False]
