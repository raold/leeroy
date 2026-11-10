"""Real mind and FakeBridge; invented people, private scratch ports/data only."""
import asyncio
from contextlib import asynccontextmanager, suppress
import threading
from types import SimpleNamespace
import pytest

from conftest import HUMAN
from test_app_lifecycle import App, CAST, boss, chat, fake_clock, hello, seed_db, toml
from leeroy_mind.app import MindApp
from leeroy_mind.config import load_config
from leeroy_mind.endpoint import feedback_args
from leeroy_mind.llm.scheduler import Priority
from leeroy_mind.talk.pipeline import TurnMeta
from leeroy_mind.talk.turns import Turn, TurnKind
from leeroy_mind.testing.fake_bridge import FakeBridge
from leeroy_mind.testing.fake_llm import FakeLLM
from leeroy_mind.voice.comms import Comms
from leeroy_mind.voice.store import VoiceStore
from leeroy_mind.voice.wire import Played
from .test_signals import saved


@asynccontextmanager
async def mind(tmp_path, *, enabled=True):
    clock = fake_clock()
    bridge = FakeBridge([hello(HUMAN), CAST])
    port = await bridge.start()
    (tmp_path / 'mind.toml').write_text(toml(port) + '\n[talk]\nchattiness=0\n'
        f'[signals]\nenabled={str(enabled).lower()}\n', encoding='utf-8')
    seed_db(tmp_path / 'mind.db', clock)
    cfg = load_config(tmp_path / 'mind.toml')
    replies = iter(('the cart wheel needs another spoke', 'your lantern still has enough oil',
                    'those muddy boots belong outside the stable', 'a fresh loaf is waiting by the gate'))
    llm = FakeLLM(lambda *_: next(replies), models={cfg.models.live, cfg.models.embedding, cfg.models.reflection})
    app = MindApp(cfg, tmp_path, llm=llm, clock=clock, rng_seed=1)
    task = asyncio.create_task(app.run())
    r = App(app, bridge, llm, clock)
    try:
        await r.wait(lambda: app.world.human_online and 101 in app.world.members)
        yield r
    finally:
        await app.stop()
        await asyncio.wait_for(task, 5)
        await bridge.stop()


async def send(r, channel='whisper'):
    turn = Turn(TurnKind.REPLY_HUMAN, 101, channel, to_guid=HUMAN if channel == 'whisper' else 0,
                addressee=HUMAN, priority=Priority.REPLY_HUMAN, max_lines=1)
    meta = TurnMeta(r.app.engine._new_id(), answers_human=True, whisper_reply=True)
    task = asyncio.create_task(r.app.engine._speak(turn, meta))
    await r.until(task.done)
    assert await task
    return r.app.engine._last_line_id[101]


def test_fakebridge_reply_emotes_tuning_nudge_and_muted_receipt(tmp_path):
    async def go():
        async with mind(tmp_path) as r:
            app, capture = r.app, r.app.signals.capture
            lid = await send(r)
            for emote in ('comfort', 'hug', 'rude'):
                await r.bridge.push({'type': 'text_emote', 'guid': HUMAN, 'emote': emote, 'target_guid': 101})
            await r.wait(lambda: app.store.conn.execute("SELECT count(*) FROM game_event WHERE type='text_emote'").fetchone()[0] == 3)
            await capture.flush()
            assert {'emote_friendly', 'emote_romantic', 'emote_rude'} <= {x[2] for x in saved(app.store) if x[0] == lid}
            change = await app.tuning.handle({'action': 'set', 'scope': 'member', 'target': '101',
                                             'dial': 'talkative', 'value': 2, 'previewed': False})
            assert change['ok']
            assert (await app.tuning.handle({'action': 'nudge', 'verb': 'set', 'target_kind': 'member',
                'target': '101', 'text': 'stop repeating the invented weather joke', 'strength': 'light',
                'scope': 'whispers', 'seconds': 3600}))['ok']
            voice = VoiceStore(app.store)
            voice.add_line('signal-voice-1', lid, 'whisper', 'sent', r.clock.now())
            Comms(SimpleNamespace(app=app, store=voice)).played(Played('signal-voice-1', 'dropped', 'muted'))
            await capture.flush()
            kinds = {x[2] for x in saved(app.store) if x[0] == lid}
            assert {'dial_down', 'nudged_against', 'muted'} <= kinds
            # A fresh delivered line can receive a reply; the muted one cannot be ignored.
            latest = await send(r)
            await r.bridge.push(chat('thanks for the invented plan', channel='whisper', to_guid=101))
            await r.wait(lambda: app.store.conn.execute("SELECT count(*) FROM chat_line WHERE origin='human'").fetchone()[0] > 0)
            await capture.flush()
            assert {'replied', 'reply_latency_s', 'reply_len'} <= {x[2] for x in saved(app.store) if x[0] == latest}
            assert not [x for x in saved(app.store) if x[2] == 'ignored']
    asyncio.run(go())


def test_fakebridge_unknown_and_boss_combat_cannot_be_ignored(tmp_path):
    async def go():
        async with mind(tmp_path) as r:
            lid = await send(r)
            await r.bridge.push({'type': 'zone', 'guid': HUMAN, 'zone_id': 10, 'zone_name': 'Test place'})
            await r.advance(120)
            await r.app.signals.capture.flush()
            assert not [x for x in saved(r.app.store) if x[0] == lid and x[2] == 'ignored']
            second = await send(r)
            await r.bridge.push(boss('engaged'))
            await r.bridge.push({'type': 'zone', 'guid': HUMAN, 'zone_id': 11, 'zone_name': 'Test place'})
            await r.advance(120)
            await r.app.signals.capture.flush()
            assert not [x for x in saved(r.app.store) if x[0] == second and x[2] == 'ignored']
    asyncio.run(go())


def test_send_path_does_not_await_capture_writer(tmp_path, monkeypatch):
    async def go():
        async with mind(tmp_path) as r:
            c = r.app.signals.capture
            await c.flush()
            entered, release = threading.Event(), threading.Event()
            original = c._process

            def held(item):
                entered.set()
                assert release.wait(5)
                return original(item)

            monkeypatch.setattr(c, '_process', held)
            r.app.signals.offer('activity')
            try:
                await r.wait(entered.is_set)
                # Real bridge send/ack and chat commit complete while capture cannot run.
                lid = await asyncio.wait_for(send(r), 2)
                assert lid > 0 and not release.is_set()
            finally:
                release.set()
            await c.flush()
    asyncio.run(go())


def test_switch_off_registers_no_worker_or_night_stage(tmp_path):
    async def go():
        async with mind(tmp_path, enabled=False) as r:
            assert r.app.signals is None
            assert not hasattr(r.app.engine, 'signals')
            assert 'signals' not in {s.name for s in r.app.night_runner.stages}
            await send(r)
            assert not saved(r.app.store)
            assert not (await r.app.control.feedback(feedback_args({'line_id': 1, 'value': 1, 'reasons': []})))['ok']
    asyncio.run(go())


@pytest.mark.parametrize('group_changed', [False, True])
def test_fakebridge_ignored_with_synthetic_rest_and_group_transition(tmp_path, group_changed):
    async def go():
        async with mind(tmp_path) as r:
            async def observe_rest():
                while True:
                    r.app.signals.combat_observed(False)
                    await r.clock.sleep(1)
            rest = asyncio.create_task(observe_rest())
            try:
                lid = await send(r)
            finally:
                rest.cancel()
                with suppress(asyncio.CancelledError):
                    await rest
            await r.bridge.push({'type': 'zone', 'guid': HUMAN, 'zone_id': 10, 'zone_name': 'Test place'})
            await r.wait(lambda: r.app.store.conn.execute("SELECT count(*) FROM game_event WHERE type='zone'").fetchone()[0] == 1)
            if group_changed:
                await r.bridge.push({'type': 'group', 'leader_guid': HUMAN, 'raid': False, 'members': [HUMAN, 102]})
                await r.wait(lambda: r.app.signals.group is not None)
            await r.advance(119)
            r.app.signals.combat_observed(False)  # synthetic authority, not inferred from movement
            await r.app.signals.capture.flush()
            await r.advance(1)
            await r.app.signals.capture.flush()
            assert bool([x for x in saved(r.app.store) if x[0] == lid and x[2] == 'ignored']) is not group_changed
    asyncio.run(go())


@pytest.mark.parametrize('channel,state,visible', [
    ('guild', 'online', True), ('guild', 'offline', False),
    ('officer', 'unknown', False), ('officer', 'known', True),
    ('party', 'ingroup', True), ('party', 'othergroup', False),
    ('say', 'same', True), ('say', 'othermap', False),
    ('say', 'otherzone', False), ('say', 'unknown', False)])
def test_pipeline_heard_is_combined_with_actual_owner_visibility(tmp_path, monkeypatch, channel, state, visible):
    async def go():
        async with mind(tmp_path) as r:
            app = r.app
            # Native cast omits the owner; guild visibility must not rely on that list.
            assert HUMAN not in app.world.members
            if state == 'offline':
                await r.bridge.push({'type': 'logout', 'guid': HUMAN, 'human': True})
                await r.wait(lambda: not app.world.human_online)
            if state == 'known':
                monkeypatch.setattr(app.world, 'is_officer', lambda guid: guid == HUMAN)
            if channel in ('party', 'say'):
                await r.bridge.push({'type': 'group', 'leader_guid': HUMAN, 'raid': False,
                                     'members': [HUMAN, 102 if state == 'othergroup' else 101]})
                await r.wait(lambda: app.signals.group is not None)
            if channel == 'say' and state != 'unknown':
                await r.bridge.push(chat('.follow', map=1 if state == 'othermap' else 0,
                                         zone=12 if state == 'otherzone' else 1519))
                await r.wait(lambda: app.signals.owner_location is not None)
            turn = Turn(TurnKind.REPLY_HUMAN, 101, channel, addressee=HUMAN)
            meta = TurnMeta(app.engine._new_id(), answers_human=True)
            lid = app.engine._record(turn, meta, app.personas.get(101), 'the invented cart has a loose wheel', [])
            await app.signals.capture.flush()
            assert (lid in app.signals.capture.recent) is visible
            app.signals.offer('emote', (101,), ('emote_friendly',))
            await app.signals.capture.flush()
            assert bool([row for row in saved(app.store) if row[0] == lid]) is visible
    asyncio.run(go())
