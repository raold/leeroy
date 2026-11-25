"""Owner-requested images can take a bounded GPU turn during WoW, opt-in only."""
import asyncio
from types import SimpleNamespace

import pytest

from conftest import run
from photos.test_request_service import ON, drive, offline
from photos.test_request_recovery import make_app, workers, queued, initial_words, tick
from leeroy_mind.photos import request_store as records
from leeroy_mind.photos.requests import TAKING_NOW
from leeroy_mind.selfies.scene import scene_from
from leeroy_mind.selfies.scene_choices import compose_choices
from selfies.test_scene_choices import PICKS
from nightly.test_request_borrow import Worker as HoldingWorker

PRIORITY = ON.replace('requests=true', 'requests=true\nrequest_priority=true')


async def online(app):
    await offline(app)  # establish the current connection's hello
    app.night_scheduler.presence.online = True
    app.world.human_online = True
    app.arbiter.hold('human')
    await app.photos.requests._presence(SimpleNamespace(human=True))


def test_login_event_does_not_cancel_priority_worker(tmp_path):
    async def go():
        app = make_app(tmp_path, PRIORITY)
        try:
            await offline(app)
            worker = HoldingWorker(app.arbiter.admin.calls)
            async with app.arbiter.borrow('request:1', priority=True) as room:
                room.own_worker(worker)
                await worker.start()
                await online(app)
                await asyncio.sleep(0)
                room.check()
                assert worker.running and not app.photos.requests.known_offline()
            assert not worker.running and not app.live_borrow.blocked
        finally:
            await app.stop()
    run(go())


@pytest.mark.parametrize('channel', ['dm:101', 'general'])
def test_online_ask_delivers_new_image_once_without_offline_grace(tmp_path, monkeypatch, channel):
    async def scene(app, guid, words, audience):
        return scene_from(compose_choices({**PICKS, 'audience': audience, 'when': 'any'}))
    monkeypatch.setattr('leeroy_mind.selfies.request_scene.choose_scene', scene)
    async def go():
        app = make_app(tmp_path, PRIORITY)
        try:
            await online(app)
            made = workers(app)
            name = app.world.discord_members()[101].name
            posted = await app.discord.handle({'action': 'post', 'channel': channel,
                                              'text': f'@{name} send me a selfie'})
            assert posted['ok']
            message = posted['id']
            # On demand: her answer comes at once, saying she's taking it, before any render.
            await drive(app, lambda: bool((r := records.get(app.store, message)) and r.initial_message))
            request = records.get(app.store, message)
            assert request.fresh_message is None and not made
            instructions = '\n'.join(m['content'] for call in app.llm.calls for m in call.get('messages', ()))
            assert TAKING_NOW in instructions
            # The queue renders it right after (one borrow) and the photo follows.
            await tick(app)
            request = records.get(app.store, message)
            assert request.fresh_message is not None and request.fresh_message == request.followup_message
            assert request.fallback_snap is None and request.failure is None
            assert records.snap(app.store, request.fresh_snap).status == 'sent'
            assert len(made) == 1 and len(made[0].calls) == 1 and made[0].stops == 1
            assert app.photos.requests._offline_at is None
            assert 'human' in app.arbiter.holders and not app.live_borrow.blocked
            assert app.cfg.models.live in app.arbiter.admin.loaded
            await tick(app)
            assert app.store.conn.execute('SELECT count(*) FROM discord_attachment').fetchone()[0] == 1
            assert app.store.conn.execute('SELECT count(*) FROM selfie_request_borrow').fetchone()[0] == 1
        finally:
            await app.stop()
    run(go())


@pytest.mark.parametrize('owner_online', [True, False])
def test_queued_request_renders_without_logout_grace(tmp_path, owner_online):
    async def go():
        app = make_app(tmp_path, PRIORITY)
        try:
            await online(app) if owner_online else await offline(app)
            request, photo = queued(app)
            await initial_words(app, request)
            made = workers(app)
            await tick(app)
            delivered = records.get(app.store, request.message)
            assert delivered.fresh_message == delivered.followup_message
            assert delivered.fresh_message is not None and len(made) == 1
            assert app.clock.now() - request.created < 600
            assert records.snap(app.store, photo.id).status == 'sent'
        finally:
            await app.stop()
    run(go())


@pytest.mark.parametrize('blocker', ['unknown', 'disconnected', 'pause', 'offline_raid', 'voice_gpu'])
def test_priority_preserves_explicit_pause_and_other_gpu_owners(tmp_path, blocker):
    async def go():
        app = make_app(tmp_path, PRIORITY)
        try:
            await online(app)
            request, photo = queued(app)
            made = workers(app)
            if blocker == 'unknown':
                app.night_scheduler.presence.online = None
            elif blocker == 'disconnected':
                await app.photos.requests._connection(False)
            elif blocker == 'pause':
                app.gpu.gate.paused = True
                app.arbiter.paused = True
            else:
                app.arbiter.hold(blocker)
            assert not await app.photos.requests._render(request, photo)
            assert not made
            assert records.snap(app.store, photo.id).status == 'planned'
            assert not app.store.conn.execute('SELECT 1 FROM selfie_request_borrow').fetchone()
        finally:
            await app.stop()
    run(go())


def test_priority_still_waits_for_members_day_plan(tmp_path):
    async def go():
        app = make_app(tmp_path, PRIORITY)
        try:
            await online(app)
            request, photo = queued(app)
            app.store.conn.execute('UPDATE selfie_snap SET not_before=? WHERE id=?',
                                   (app.clock.now() + 3600, photo.id))
            made = workers(app)
            await app.photos.requests._prepare(request, 'send me a selfie', immediate=True)
            assert not made
            assert records.get(app.store, request.message).fallback_snap is None
        finally:
            await app.stop()
    run(go())
