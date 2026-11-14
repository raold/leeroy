"""Live list maintenance against real stored sources; no network or model calls."""
import asyncio
from dataclasses import replace
import math
from types import SimpleNamespace

import pytest

from conftest import seed_test_cast
from leeroy_mind.claims import store as claims
from leeroy_mind.discord import store as discord
from leeroy_mind.human import HumanIdentity
from leeroy_mind.outreach import store as records
from leeroy_mind.outreach.service import NUDGE, Outreach
from leeroy_mind.outreach.sources import MISS_YOU_ABOUT
from leeroy_mind.protocol import Chat, Login, Logout
from leeroy_mind.storage.rows import pack_vec
from leeroy_mind.talk.pipeline import TurnMeta
from leeroy_mind.talk.turns import Turn, TurnKind


def vector(score=1):
    return pack_vec([score, math.sqrt(1-score*score)] + [0.] * 510)


class Embedder:
    def __init__(self):
        self.calls = []
        self.query = vector()
        self.entered = asyncio.Event()
        self.gate = None

    async def embed(self, texts, priority):
        self.calls.append(list(texts))
        self.entered.set()
        if self.gate is not None:
            await self.gate.wait()
        return [self.query for _ in texts]


@pytest.fixture
def app(store):
    personas = seed_test_cast(store)
    store.set_meta('human_guid', '1')
    hooks, events, connections, drains = [], {}, [], []
    owed = set()
    engine = SimpleNamespace(cfg=SimpleNamespace(talk=SimpleNamespace(chattiness=4)),
        human=HumanIdentity(1, 'Owner', 'Owner', '', True),
        _unacked={}, add_instruction_hook=hooks.append)
    app = SimpleNamespace(store=store, clock=store.clock, human_guid=1, personas=personas,
        engine=engine, embedder=Embedder(), owed=owed,
        discord_talk=SimpleNamespace(owes=lambda guid: guid in owed),
        world=SimpleNamespace(discord_members=store.cast, human_online=True),
        is_backlog=lambda event: False,
        register_event_handler=lambda kind, fn: events.setdefault(kind, fn),
        register_connection_hook=connections.append, register_shutdown_drain=drains.append)
    app.outreach = Outreach(app)
    yield app
    asyncio.run(app.outreach.close())


def post(app, *, guid=101, author=None, age=60, deleted=False):
    author = guid if author is None else author
    message = discord.post(app.store, channel=f'dm:{guid}', author=author,
        origin='human' if author == 1 else 'mind', text='A private conversation stays here.',
        now=app.clock.now()-age)
    if deleted:
        discord.delete(app.store, message, author=author, now=app.clock.now())
    return message


def chat(app, text, *, guid=101, channel='whisper', owner=True, author=None, audience=None):
    author = (1 if owner else guid) if author is None else author
    to = guid if owner else 1
    ev = Chat(ts=int(app.clock.now()*1000), channel=channel, from_guid=author,
              from_name='Owner' if owner else 'Arkon', to_guid=to, text=text, map=0, zone=0)
    line = app.store.insert_chat_line(ev, session_id=None, origin='human' if owner else 'mind',
                                     audience=audience)
    if not owner:
        app.store.log_utterance(app.clock.now(), author, channel, 'whisper', text, (),
                                dropped=None, session_id=None, to_guid=to)
    message = None
    if channel.startswith('dm:'):
        message = discord.post(app.store, channel=channel, author=author,
            origin='human' if owner else 'mind', text=text, now=app.clock.now())
        app.store.set_meta(f'discord:line:{message}', str(line))
    return line, message


def plan(app, *, guid=101, about='You plan to visit the quiet garden.', expires=None):
    now = app.clock.now()
    expires = now+86400 if expires is None else expires
    source, _ = chat(app, about, guid=guid, owner=False)
    claim = claims.add(app.store, guid, kind='plan', subject='own outing', text=about,
        polarity=1, embedding=vector(), source_line=source, now=now, expires=expires)
    item = records.add(app.store, guid=guid, kind='plan', ref=f'plan:{claim}', about=about,
        audience='pair', weight=2, created=now, not_before=now, expires=expires)
    assert app.outreach._alive(item), 'test fixture must have a delivered, valid plan source'
    return item


def login(app, *, human=True):
    return Login(ts=int(app.clock.now()*1000), guid=1 if human else 101, human=human)


async def settle(app):
    if app.outreach._tasks:
        await asyncio.gather(*tuple(app.outreach._tasks))


def test_nudge_needs_observed_login_waits_two_to_ten_minutes_and_is_whisper_only(app):
    async def go():
        message = post(app)
        assert records.open_items(app.store) == []
        await app.outreach.presence(login(app, human=False))
        assert records.open_items(app.store) == []
        app.is_backlog = lambda event: True
        await app.outreach.presence(login(app))
        assert records.open_items(app.store) == []
        app.is_backlog = lambda event: False
        await app.outreach.presence(login(app))
        item, = records.open_items(app.store)
        assert item.ref == f'dm_nudge:{message}' and item.about == NUDGE
        assert 120 <= item.not_before-app.clock.now() <= 600
        assert app.outreach.select(101, 'whisper') is None
        await app.clock.advance(601, step=601)
        assert app.outreach.select(101, 'whisper') == item
        assert app.outreach.select(101, 'dm') is None
        assert app.outreach.select(101, 'guild') is None
        assert app.outreach.select(101, 'party') is None
        turn = Turn(TurnKind.WHISPER, 101, 'whisper', to_guid=1, addressee=1)
        meta = TurnMeta('nudge-turn')
        assert app.outreach.bind(turn, meta, item)
        instruction = app.outreach.instruction(turn, meta, 'Talk naturally.')
        assert NUDGE in instruction
        assert 'A private conversation stays here.' not in instruction
    asyncio.run(go())


@pytest.mark.parametrize('case', ['old', 'future', 'answered', 'answer_deleted', 'source_deleted'])
def test_only_a_recent_unanswered_live_dm_creates_a_nudge(app, case):
    async def go():
        post(app, age=3*86400 if case == 'old' else -1 if case == 'future' else 60,
             deleted=case == 'source_deleted')
        if case in ('answered', 'answer_deleted'):
            post(app, author=1, age=30, deleted=case == 'answer_deleted')
        await app.outreach.presence(login(app))
        assert records.open_items(app.store) == []
    asyncio.run(go())


def test_nudge_never_reappears_after_logout_even_on_a_later_login(app):
    async def go():
        message = post(app)
        await app.outreach.presence(login(app))
        item, = records.open_items(app.store)
        await app.outreach.presence(Logout(ts=int(app.clock.now()*1000), guid=1, human=True))
        assert records.get(app.store, item.id).ended == 'expired'
        await app.outreach.presence(login(app))
        assert records.open_items(app.store) == []
        assert app.store._one('SELECT count(*) n FROM outreach_item WHERE ref=?',
                              (f'dm_nudge:{message}',))['n'] == 1
    asyncio.run(go())


@pytest.mark.parametrize('ending', ['restart', 'disconnect'])
def test_nudge_does_not_survive_an_unobserved_game_session(app, ending):
    async def go():
        post(app)
        await app.outreach.presence(login(app))
        item, = records.open_items(app.store)
        if ending == 'restart':
            await app.outreach.close()
            app.outreach = Outreach(app)
        else:
            await app.outreach.connection(False)
        assert records.get(app.store, item.id).ended == 'expired'
    asyncio.run(go())


def test_deleted_later_reply_does_not_revive_an_existing_nudge(app):
    async def go():
        post(app)
        await app.outreach.presence(login(app))
        item, = records.open_items(app.store)
        post(app, author=1, age=0, deleted=True)
        await app.clock.advance(601, step=601)
        assert app.outreach.select(101, 'whisper') is None
        assert records.get(app.store, item.id).ended == 'superseded'
    asyncio.run(go())


def test_zero_chattiness_and_owed_answer_suppress_selection_without_stopping_fill(app):
    async def go():
        app.engine.cfg.talk.chattiness = 0
        post(app)
        await app.outreach.presence(login(app))
        item, = records.open_items(app.store)
        await app.clock.advance(601, step=601)
        assert app.outreach.select(101, 'whisper') is None
        app.engine.cfg.talk.chattiness = 4
        app.owed.add(101)
        assert app.outreach.select(101, 'whisper') is None
        assert records.get(app.store, item.id).state == 'open'
        app.owed.clear()
        assert app.outreach.select(101, 'whisper') == item
    asyncio.run(go())


@pytest.mark.parametrize('named', ['[Copper Lantern]', '“Copper Lantern”'])
def test_named_item_in_his_dm_closes_only_her_item_without_embedding(app, named):
    async def go():
        item = plan(app, about=f'You plan to find {named} tomorrow.')
        other = plan(app, guid=102, about=f'You plan to find {named} tomorrow.')
        line, _ = chat(app, 'Have you found the copper lantern yet?', channel='dm:101')
        app.outreach.on_his_line(line)
        assert records.get(app.store, item.id).ended == 'he_raised'
        assert records.get(app.store, other.id).state == 'open'
        assert app.embedder.calls == []
    asyncio.run(go())


@pytest.mark.parametrize('channel', ['whisper', 'dm:101'])
@pytest.mark.parametrize('score,ended', [(.7, 'he_raised'), (.5, None)])
def test_semantic_match_reuses_existing_claim_vector_and_threshold(app, channel, score, ended):
    async def go():
        item = plan(app)
        app.embedder.query = vector(score)
        text = 'How is your outing going?'
        line, _ = chat(app, text, channel=channel)
        app.outreach.on_his_line(line)
        assert not app.outreach.eligible(101)
        await settle(app)
        assert records.get(app.store, item.id).ended == ended
        assert app.embedder.calls == [[text]]  # stored source vector, no second embedding
        assert app.outreach.eligible(101)
    asyncio.run(go())


@pytest.mark.parametrize('change', ['deleted', 'edited', 'not_owner'])
def test_stale_or_nonowner_named_line_cannot_close_intention(app, change):
    async def go():
        item = plan(app, about='You plan to find [Copper Lantern] tomorrow.')
        line, message = chat(app, 'How is Copper Lantern?', channel='dm:101',
                             author=102 if change == 'not_owner' else None)
        if change == 'deleted':
            discord.delete(app.store, message, author=1, now=app.clock.now())
        elif change == 'edited':
            discord.edit(app.store, message, author=1, text='Never mind.', now=app.clock.now())
        app.outreach.on_his_line(line)
        await settle(app)
        assert records.get(app.store, item.id).state == 'open'
        assert app.embedder.calls == []
    asyncio.run(go())


@pytest.mark.parametrize('change', ['deleted', 'edited', 'source_superseded'])
def test_semantic_wait_rechecks_both_his_line_and_her_source(app, change):
    async def go():
        item = plan(app)
        line, message = chat(app, 'How is your outing going?', channel='dm:101')
        app.embedder.gate = asyncio.Event()
        app.outreach.on_his_line(line)
        await asyncio.wait_for(app.embedder.entered.wait(), 1)
        if change == 'deleted':
            discord.delete(app.store, message, author=1, now=app.clock.now())
        elif change == 'edited':
            discord.edit(app.store, message, author=1, text='Never mind.', now=app.clock.now())
        else:
            with app.store.transaction():
                app.store.conn.execute('UPDATE member_claim SET expires=? WHERE id=?',
                                       (app.clock.now(), int(item.ref.split(':')[1])))
        app.embedder.gate.set()
        await settle(app)
        assert records.get(app.store, item.id).state == 'open'
        assert app.outreach.eligible(101)
    asyncio.run(go())


@pytest.mark.parametrize('change,ending', [('expired', 'expired'), ('source_gone', 'superseded')])
def test_late_native_receipt_preserves_delivered_line_when_intention_ends(app, change, ending):
    async def go():
        item = plan(app, expires=app.clock.now()+10)
        turn = Turn(TurnKind.WHISPER, 101, 'whisper', to_guid=1, addressee=1)
        meta = TurnMeta('late-native')
        assert app.outreach.bind(turn, meta, item)
        assert app.outreach.valid(turn, meta)
        if change == 'expired':
            await app.clock.advance(11, step=11)
        else:
            with app.store.transaction():
                app.store.conn.execute('UPDATE member_claim SET expires=? WHERE id=?',
                                       (app.clock.now(), int(item.ref.split(':')[1])))
        with app.store.transaction():
            receipt, _ = chat(app, 'I was thinking of that garden.', owner=False)
            app.outreach.record(turn, meta, receipt, app.clock.now())
        app.outreach.delivered(meta, receipt)
        assert app.store.chat_lines([receipt])
        assert records.get(app.store, item.id).ended == ending
        assert item.id not in app.outreach._held
    asyncio.run(go())


def test_inflight_semantic_matching_is_cancelled_cleanly_on_shutdown(app):
    async def go():
        item = plan(app)
        line, _ = chat(app, 'How is your outing going?')
        app.embedder.gate = asyncio.Event()
        app.outreach.on_his_line(line)
        await asyncio.wait_for(app.embedder.entered.wait(), 1)
        await app.outreach.close()
        assert records.get(app.store, item.id).state == 'open'
        assert app.outreach._checking == {}
    asyncio.run(go())


@pytest.mark.parametrize('channel,via', [('whisper', 'whisper'), ('dm:101', 'dm')])
def test_miss_you_delivery_does_not_invalidate_its_own_receipt(app, channel, via):
    async def go():
        persona = app.personas[101]
        app.personas[101] = replace(persona,
            identity=replace(persona.identity, player_gender='female'),
            romance=replace(persona.romance, stance='drawn'))
        now = app.clock.now()
        item = records.add(app.store, guid=101, kind='miss_you',
            ref=f'miss_you:{app.clock.local().date()}', about=MISS_YOU_ABOUT,
            audience='pair', weight=3, created=now, not_before=now, expires=now+86400)
        turn = Turn(TurnKind.WHISPER, 101, channel, to_guid=1, addressee=1)
        meta = TurnMeta('miss-you-delivery')
        assert app.outreach.bind(turn, meta, item)
        assert app.outreach.valid(turn, meta)
        with app.store.transaction():
            receipt, _ = chat(app, 'It has been a while, how are you?', channel=channel, owner=False)
            app.outreach.record(turn, meta, receipt, now)
        delivered = records.get(app.store, item.id)
        assert (delivered.state, delivered.told_via, delivered.line_id) == ('told', via, receipt)
        assert app.outreach.select(101, 'dm' if via == 'whisper' else 'whisper') is None
    asyncio.run(go())


@pytest.mark.parametrize('channel,contact_channel', [('whisper', 'dm:101'), ('dm:101', 'whisper')])
@pytest.mark.parametrize('contact_owner', [True, False])
def test_miss_you_receipt_excludes_only_itself_not_intervening_pair_contact(
        app, channel, contact_channel, contact_owner):
    async def go():
        persona = app.personas[101]
        app.personas[101] = replace(persona,
            identity=replace(persona.identity, player_gender='female'),
            romance=replace(persona.romance, stance='drawn'))
        now = app.clock.now()
        item = records.add(app.store, guid=101, kind='miss_you',
            ref=f'miss_you:{app.clock.local().date()}', about=MISS_YOU_ABOUT,
            audience='pair', weight=3, created=now, not_before=now, expires=now+86400)
        turn = Turn(TurnKind.WHISPER, 101, channel, to_guid=1, addressee=1)
        meta = TurnMeta('miss-you-late')
        assert app.outreach.bind(turn, meta, item)
        assert app.outreach.valid(turn, meta)
        # Native transport is now in flight. Another pair contact lands before its ack,
        # in the other medium at the same timestamp, so exclusion must use exact ids.
        contact, _ = chat(app, 'A separate conversation has started.',
                          channel=contact_channel, owner=contact_owner)
        with app.store.transaction():
            receipt, _ = chat(app, 'It has been a while, how are you?', channel=channel, owner=False)
            app.outreach.record(turn, meta, receipt, now)
        app.outreach.delivered(meta, receipt)
        assert len(app.store.chat_lines([contact, receipt])) == 2
        ended = records.get(app.store, item.id)
        assert (ended.state, ended.ended, ended.line_id) == ('dropped', 'superseded', None)
        assert item.id not in app.outreach._held
    asyncio.run(go())


@pytest.fixture
def pipeline_rig(store, tmp_path):
    from engine_rig import make_rig
    rig = make_rig(store, store.clock, tmp_path, reply='did you catch my message earlier')
    store.set_meta('human_guid', '1')
    yield rig
    rig.close()


@pytest.mark.parametrize('age', [1200, 2*86400])
def test_actual_nudge_whisper_prompt_contains_earlier_dm_and_only_nudge_instruction(
        pipeline_rig, tmp_path, monkeypatch, age):
    from outreach.test_consumers import app_for, speak
    rig = pipeline_rig
    app = app_for(rig, tmp_path)
    original = 'The small copper lantern arrived this morning.'
    other = 'The green notebook belongs to another conversation.'
    now = rig.clock.now()
    for guid, words in ((101, original), (102, other)):
        message = discord.post(rig.store, channel=f'dm:{guid}', author=guid, origin='mind',
                               text=words, now=now-age)
        line = rig.store.insert_chat_line(Chat(int((now-age)*1000), f'dm:{guid}',
            guid, 'Arkon' if guid == 101 else 'Belra', 1, words, 0, 10),
            session_id=None, origin='mind', audience=(guid, 1), ts=now-age)
        rig.store.set_meta(f'discord:line:{message}', str(line))
        rig.store.log_utterance(now-age, guid, f'dm:{guid}', 'open', words, (), None, None, to_guid=1)
    rig.sync(app.outreach.presence(Login(ts=int(now*1000), guid=1, human=True)))
    rig.sync(rig.clock.advance(601, step=601))
    queued = []
    monkeypatch.setattr(rig.engine, '_queue', queued.extend)
    rig.engine._queue_whisper(101, 'start', rig.clock.now())
    turn, meta = queued[0]
    assert records.get(rig.store, meta.outreach_item).kind == 'dm_nudge'
    said = speak(rig, turn, meta)
    assert len(said) == 1
    prompt = rig.prompts()[-1]
    assert 'Earlier between you two, elsewhere' in prompt
    elsewhere = prompt.split('Earlier between you two, elsewhere', 1)[1].split('\n\n', 1)[0]
    assert original in elsewhere and '(Discord DM' in elsewhere
    assert other not in prompt
    instruction = prompt.split('This is the one thing you mean to tell him now', 1)[1]
    assert NUDGE in instruction and original not in instruction
    assert original not in said[0]
    told = records.get(rig.store, meta.outreach_item)
    assert told.state == 'told' and told.told_via == 'whisper'


@pytest.mark.parametrize('state', ['told', 'held'])
@pytest.mark.parametrize('kind', ['plan', 'life'])
def test_actual_dm_opener_cannot_reuse_tracked_source_as_legacy_seed(
        pipeline_rig, tmp_path, monkeypatch, state, kind):
    import random
    from conftest import make_mem
    from outreach.test_consumers import app_for, bind, plan as real_plan
    from server.test_dms import fixture as dm_fixture, due
    from leeroy_mind.nightly.life import ensure_thread
    from leeroy_mind.server.seeds import Seed
    rig = pipeline_rig
    app = app_for(rig, tmp_path)
    now = rig.clock.now()
    topic = 'The rosemary plant now has a bright blue pot.'
    if kind == 'plan':
        item = real_plan(rig, about=topic)
        seed_key = item.ref + ':2026-W44'
    else:
        thread = ensure_thread(rig.store, 101, 'A small invented garden', now-100, random.Random(1))
        with rig.store.transaction():
            beat = rig.store.conn.execute('INSERT INTO life_beat(thread_id,night,text,ts) VALUES (?,?,?,?)',
                (thread, '2026-10-29', topic, now-10)).lastrowid
        rig.store.insert_memory(make_mem(owner=101, text=topic, t_event=now-10, t_created=now-10,
            t_accessed=now-10, source='offscreen', evidence=(f'beat:{beat}',), actors=(101,)))
        item = records.add(rig.store, guid=101, kind='life', ref=f'life:{beat}', about=topic,
            audience='pair', weight=3, created=now, not_before=now, expires=now+86400)
        seed_key = item.ref
    assert app.outreach._alive(item)
    turn, meta = bind(app, item, 'whisper')
    if state == 'told':
        with rig.store.transaction():
            receipt = rig.store.insert_chat_line(Chat(int(now*1000), 'whisper', 101, 'Arkon', 1,
                'The rosemary plant has its new blue pot.', 0, 10), session_id=None, origin='mind')
            assert records.told(rig.store, item.id, via='whisper', line_id=receipt, now=now)
        app.outreach.delivered(meta, receipt)
    opener, snap, _ = dm_fixture(rig.store, rig.clock, monkeypatch)
    opener.app = app
    app.guild = None
    app.server = SimpleNamespace(lease=SimpleNamespace(ready=lambda: True), reactions=None, dms=opener)
    rig.world.human_online = False
    rig.engine.live_review.config = replace(rig.engine.live_review.config, enabled=True)
    snap.personas = {101: rig.engine.personas.get(101)}
    fresh = 'An untracked walk through the harbor went well.'
    snap.seeds = [Seed(seed_key, kind, ('general',), topic, (101,)),
                  Seed('life:99999', 'life', ('general',), fresh, (101,))]
    captured = []
    async def capture(outgoing, metadata):
        assert metadata.still_valid()
        captured.append((outgoing, metadata))
        return []
    monkeypatch.setattr(rig.engine, 'speak_meta', capture)
    async def go():
        try:
            await due(opener, rig.clock)
        finally:
            await opener.close()
    rig.sync(go())
    assert len(captured) == 1
    outgoing, metadata = captured[0]
    assert metadata.outreach_item is None and metadata.topic == fresh
    assert topic not in outgoing.instruction
    assert records.get(rig.store, item.id).state == ('told' if state == 'told' else 'open')


@pytest.mark.parametrize('enabled', [True, False])
@pytest.mark.parametrize('state', ['told', 'held'])
@pytest.mark.parametrize('newer', [True, False])
def test_tracked_boss_event_cannot_return_through_legacy_game_topic(
        pipeline_rig, tmp_path, enabled, state, newer):
    from outreach.test_consumers import app_for, bind
    from leeroy_mind.outreach.sources import his_day
    from leeroy_mind.protocol import Boss
    rig = pipeline_rig
    app = app_for(rig, tmp_path) if enabled else None
    now = rig.clock.now()
    event = Boss(int(now*1000), 631, 1, 0, '', 'killed', (1, 101))
    source = rig.store.insert_game_event(event, session_id=None)
    rig.engine._note_lately(event, now)
    about = his_day(rig.store, source, 101, 1, now=now, ident=rig.engine.human)
    item = records.add(rig.store, guid=101, kind='his_day', ref=f'his_day:{source}', about=about,
        audience='guild', weight=2, created=now, not_before=now, expires=now+86400)
    if app is not None:
        assert app.outreach._alive(item)
    if state == 'told':
        with rig.store.transaction():
            receipt = rig.store.insert_chat_line(Chat(int(now*1000), 'whisper', 101, 'Arkon', 1,
                'You handled that fight well.', 0, 10), session_id=None, origin='mind')
            assert records.told(rig.store, item.id, via='whisper', line_id=receipt, now=now)
    elif app is not None:
        bind(app, item, 'whisper')
    if app is not None:
        assert app.outreach.select(101, 'guild') is None
    if newer:
        later = replace(event, ts=event.ts+1000)
        rig.store.insert_game_event(later, session_id=None)
        rig.engine._note_lately(later, now+1)
    assert 'Marrowgar' in rig.engine._lately_line(101, now+1)  # ordinary prompt knowledge remains
    topics = [rig.engine._to_him_topic(rig.engine.personas.get(101), now) for _ in range(20)]
    assert any('Marrowgar' in topic for topic in topics) is (not enabled or newer)


@pytest.mark.parametrize('case', ['deleted', 'other_pair', 'other_recipient', 'different_text',
                                 'widened_turn', 'bounded'])
def test_nudge_context_rechecks_source_pair_and_keeps_normal_row_limit(app, case):
    from leeroy_mind.outreach.context import nudge_lines
    from leeroy_mind.talk.pipeline import PAIR_LINES
    async def go():
        message = post(app, age=2*86400)
        text = 'A private conversation stays here.'
        mapped = app.store.insert_chat_line(Chat(0, 'dm:101', 101, 'Arkon', 1, text, 0, 10),
            session_id=None, origin='mind', ts=app.clock.now()-2*86400)
        app.store.set_meta(f'discord:line:{message}', str(mapped))
        await app.outreach.presence(login(app))
        item, = records.open_items(app.store)
        turn = Turn(TurnKind.WHISPER, 101, 'whisper', to_guid=1, addressee=1)
        meta = TurnMeta('nudge-context')
        assert app.outreach.bind(turn, meta, item)
        if case == 'deleted':
            discord.delete(app.store, message, author=101, now=app.clock.now())
        elif case in ('other_pair', 'other_recipient', 'different_text'):
            with app.store.transaction():
                app.store.conn.execute('UPDATE chat_line SET channel=?,to_guid=?,text=? WHERE id=?',
                    ('dm:102' if case == 'other_pair' else 'dm:101',
                     102 if case == 'other_recipient' else 1,
                     'Different words.' if case == 'different_text' else text, mapped))
        elif case == 'widened_turn':
            turn.channel = 'guild'
        if case == 'widened_turn':
            with pytest.raises(AssertionError, match='audience widened'):
                nudge_lines(app.outreach, turn, meta, [], [], limit=PAIR_LINES)
        elif case != 'bounded':
            assert nudge_lines(app.outreach, turn, meta, [], [], limit=PAIR_LINES) == []
        else:
            candidates = []
            for i in range(PAIR_LINES+3):
                line = app.store.insert_chat_line(Chat(0, 'dm:101', 101, 'Arkon', 1,
                    f'Old pair line {i}.', 0, 10), session_id=None, origin='mind',
                    ts=app.clock.now()-100+i)
                candidates.extend(app.store.chat_lines([line]))
            actual = nudge_lines(app.outreach, turn, meta, candidates, [], limit=PAIR_LINES)
            assert len(actual) == PAIR_LINES
            assert actual[0].id == mapped
            assert actual[1:] == candidates[-(PAIR_LINES-1):]
    asyncio.run(go())
