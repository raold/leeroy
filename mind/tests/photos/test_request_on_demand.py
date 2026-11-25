"""[photos] request_priority on demand: a plain ask takes a ready photo at once; any other ask is
answered at once ("taking it now") and its photo follows from the queue; a failed or late render
sends a photo she already had, said honestly; her day plan does not put it off."""
import asyncio

import pytest

from conftest import run
from photos.test_images import jpeg
from photos.test_request_priority import PRIORITY
from photos.test_request_recovery import advance_to, make_app, queued, tick, workers
from photos.test_request_service import drive, offline
from selfies.fakes import ready_snap
from selfies.test_scene_choices import PICKS
from leeroy_mind.life.day import Block
from leeroy_mind.photos import request_store as records
from leeroy_mind.photos.requests import RENDER_WAIT_S, SUBSTITUTE, TAKING_NOW
from leeroy_mind.selfies import files, snaps
from leeroy_mind.selfies.request_scene import RequestSceneError
from leeroy_mind.selfies.scene import scene_from
from leeroy_mind.selfies.scene_choices import compose_choices
from leeroy_mind.selfies.store import current


@pytest.fixture(autouse=True)
def enum_scene(monkeypatch):
    async def scene(app, guid, words, audience, *, model=True):
        return scene_from(compose_choices({**PICKS, 'audience': audience, 'when': 'any'}))
    monkeypatch.setattr('leeroy_mind.selfies.request_scene.choose_scene', scene)


def stock(app, guid=101):
    """A ready, unsent situational photo of hers for him (the night's stock)."""
    sid = ready_snap(app.store, app.data_dir, guid, audience='him',
                     scene=compose_choices({**PICKS, 'audience': 'him', 'when': 'any'}),
                     pick=current(app.store, guid).pick,
                     looks=snaps.looks_hash(app.personas.get(guid).appearance))
    for thumb in (False, True):
        files.snap(app.data_dir, guid, sid, thumb=thumb).write_bytes(jpeg() + f'/stock/{sid}'.encode())
    return sid


def prompts(app):
    return '\n'.join(m['content'] for call in app.llm.calls for m in call.get('messages', ()))


async def ask(app, text, channel='dm:101'):
    posted = await app.discord.handle({'action': 'post', 'channel': channel, 'text': text})
    assert posted['ok']
    await drive(app, lambda: bool((r := records.get(app.store, posted['id'])) and r.initial_message))
    return posted['id']


@pytest.mark.parametrize('text', ['send me a pic', 'selfie?', 'DM me a selfie', 'what are you wearing?'])
def test_plain_ask_sends_a_ready_photo_with_her_first_answer_and_renders_nothing(tmp_path, text):
    async def go():
        app = make_app(tmp_path, PRIORITY)
        try:
            await offline(app)
            made = workers(app)
            original = stock(app)
            message = await ask(app, text)
            request = records.get(app.store, message)
            assert request.fresh_message == request.initial_message and request.failure is None
            copy = records.snap(app.store, request.fresh_snap)
            assert copy.status == 'sent' and copy.id != original and not made
            assert records.snap(app.store, original).status == 'ready'  # the original is kept
            assert not app.store.conn.execute('SELECT 1 FROM selfie_request_borrow').fetchone()
            assert TAKING_NOW not in prompts(app)
            await tick(app)  # nothing is left for the queue
            assert not made and records.get(app.store, message).followup_message is None
        finally:
            await app.stop()
    run(go())


def test_described_ask_is_answered_at_once_then_its_fresh_photo_follows(tmp_path):
    async def go():
        app = make_app(tmp_path, PRIORITY)
        try:
            await offline(app)
            made = workers(app)
            stock(app)  # a ready photo does not answer an ask that describes the photo
            message = await ask(app, 'send me a sexy selfie from the beach')
            request = records.get(app.store, message)
            assert request.fresh_message is None and request.fallback_message is None and not made
            assert TAKING_NOW in prompts(app)
            await tick(app)
            request = records.get(app.store, message)
            assert request.fresh_message == request.followup_message is not None
            assert len(made) == 1 and records.snap(app.store, request.fresh_snap).status == 'sent'
            assert app.store.conn.execute(
                'SELECT identity FROM selfie_request_image WHERE snap=?',
                (request.fresh_snap,)).fetchone()[0] == f'snap:{request.fresh_snap}'
        finally:
            await app.stop()
    run(go())


def test_answered_ask_wakes_the_queue_at_once(tmp_path):
    async def go():
        app = make_app(tmp_path, PRIORITY)
        try:
            requests = app.photos.requests
            idle = asyncio.create_task(requests._idle(15))
            await asyncio.sleep(0)
            assert not idle.done()
            requests._wake.set()
            await asyncio.wait_for(idle, 1)  # no fake time passed
            assert not requests._wake.is_set()
        finally:
            await app.stop()
    run(go())


def test_failed_render_sends_a_photo_she_had_and_says_so(tmp_path):
    async def go():
        app = make_app(tmp_path, PRIORITY)
        try:
            await offline(app)
            made = workers(app, fail=True)
            message = await ask(app, 'send me a cute selfie')
            await tick(app)
            request = records.get(app.store, message)
            assert len(made) == 1 and request.failure is None
            assert request.fresh_message == request.followup_message is not None
            identity = app.store.conn.execute('SELECT identity FROM selfie_request_image WHERE snap=?',
                                              (request.fresh_snap,)).fetchone()[0]
            assert identity.startswith('gallery:')  # one of her retained profile photos
            assert SUBSTITUTE in prompts(app)
        finally:
            await app.stop()
    run(go())


def test_failed_render_with_nothing_retained_says_it_did_not_work_out(tmp_path, monkeypatch):
    monkeypatch.setattr('leeroy_mind.photos.request_stock.reserve_fallback',
                        lambda *args, **kwargs: None)
    async def go():
        app = make_app(tmp_path, PRIORITY)
        try:
            await offline(app)
            workers(app, fail=True)
            message = await ask(app, 'send me a cute selfie')
            await tick(app)
            request = records.get(app.store, message)
            assert request.failure == 'worker' and request.failure_message is not None
            assert request.fresh_message is None
            assert "didn't work out" in prompts(app)
        finally:
            await app.stop()
    run(go())


def test_no_scene_she_can_take_sends_a_photo_she_had(tmp_path, monkeypatch):
    async def refused(app, guid, words, audience, *, model=True):
        raise RequestSceneError('unrenderable')
    monkeypatch.setattr('leeroy_mind.selfies.request_scene.choose_scene', refused)
    async def go():
        app = make_app(tmp_path, PRIORITY)
        try:
            await offline(app)
            made = workers(app)
            message = await ask(app, 'send me a cute selfie')
            request = records.get(app.store, message)
            assert request.failure is None and request.fresh_message == request.initial_message
            assert not made
        finally:
            await app.stop()
    run(go())


def test_priority_render_not_started_in_time_takes_a_retained_photo(tmp_path):
    async def go():
        app = make_app(tmp_path, PRIORITY)
        try:
            await offline(app)
            request, photo = queued(app)
            app.discord_talk.on_his_post(request.message, immediate=True)
            await drive(app, lambda: records.get(app.store, request.message).initial_message is not None)
            made = workers(app)
            app.arbiter.hold('offline_raid')  # the queue cannot borrow
            await tick(app)
            assert not made and records.snap(app.store, photo.id).status == 'planned'
            await advance_to(app, request.created + RENDER_WAIT_S + 1)
            await tick(app)
            latest = records.get(app.store, request.message)
            assert latest.failure is None and latest.fresh_message == latest.followup_message is not None
            assert records.snap(app.store, photo.id).status == 'expired' and not made
        finally:
            await app.stop()
    run(go())


def test_priority_takes_it_now_even_when_her_day_plan_has_her_asleep(tmp_path, monkeypatch):
    async def words_scene(app, guid, words, audience, *, request, block=None, hour=None):
        return {**compose_choices({**PICKS, 'audience': audience, 'when': 'any'})}
    monkeypatch.setattr('leeroy_mind.selfies.request_scene.choose_words_scene', words_scene)
    async def go():
        app = make_app(tmp_path, PRIORITY.replace('request_priority=true',
                                                  'request_priority=true\nscene_from_words=true'))
        try:
            requests = app.photos.requests
            assert requests.words and requests.priority
            asked = []
            def asleep(request, p):
                asked.append(request.message)
                return (Block.ASLEEP, app.clock.now() + 3600, 'asleep')
            monkeypatch.setattr(requests, '_wait', asleep)
            request, _ = queued(app)
            scene, not_before = await requests._words_scene(request, 'send me a selfie',
                                                            app.personas.get(101))
            assert not_before is None and 'wait' not in scene and not asked
            requests.priority = False
            scene, not_before = await requests._words_scene(request, 'send me a selfie',
                                                            app.personas.get(101))
            assert not_before is not None and scene['wait'] == 'asleep' and asked
        finally:
            await app.stop()
    run(go())
