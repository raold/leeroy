"""A request's retries re-run only its member's turn, at most twice after the first attempt."""
import pytest

from conftest import run
from photos.test_request_recovery import make_app, queued, ready, initial_words, tick, replies
from photos.test_request_service import drive
from selfies.test_scene_choices import PICKS
from leeroy_mind.photos import request_store as records
from leeroy_mind.selfies import files
from leeroy_mind.selfies.scene import scene_from
from leeroy_mind.selfies.scene_choices import compose_choices


def flaky(app, monkeypatch, wanted, drops):
    """Drop the matching turns `drops` times before letting them speak; returns their tries."""
    original, tries = app.engine._speak_now, []
    async def speak(turn, meta):
        if wanted(turn, meta):
            tries.append(app.clock.now())
            if len(tries) <= drops:
                return []
        return await original(turn, meta)
    monkeypatch.setattr(app.engine, '_speak_now', speak)
    return tries


def authors(app, message):
    return [r[0] for r in app.store.conn.execute(
        "SELECT author FROM discord_message WHERE reply_to=? AND origin='mind' ORDER BY id", (message,))]


@pytest.mark.parametrize('drops', [1, 99])
def test_channel_retry_reruns_only_the_target_and_stops_after_two(tmp_path, monkeypatch, drops):
    async def scene(app, guid, words, audience):
        return scene_from(compose_choices({**PICKS, 'audience': audience, 'when': 'any'}))
    monkeypatch.setattr('leeroy_mind.selfies.request_scene.choose_scene', scene)
    async def scenario():
        app = make_app(tmp_path)
        service = app.photos.requests
        try:
            app.night_scheduler.presence.online = True
            tries = flaky(app, monkeypatch, lambda turn, meta: turn.speaker == 101, drops)
            a, b = [app.world.discord_members()[g].name for g in (101, 102)]
            posted = await app.discord.handle({'action': 'post', 'channel': 'general',
                                               'text': f'@{a} @{b} send me a selfie'})
            message = posted['id']
            await drive(app, lambda: not service._tasks and authors(app, message))
            for _ in range(4):
                await app.clock.advance(181, 181)
                await tick(app)
                await drive(app, lambda: not service._tasks)
            request = records.get(app.store, message)
            assert authors(app, message).count(102) == 1
            if drops == 1:
                assert len(tries) == 2 and authors(app, message).count(101) == 1
                assert request.initial_message == request.fallback_message is not None
                assert request.ended is None
            else:
                assert len(tries) == 3 and 101 not in authors(app, message)
                assert (request.ended, request.failure) == ('passed', 'retry_limit')
                assert records.snap(app.store, request.fallback_snap).ended == 'passed'
                assert files.snap(app.data_dir, 101, request.fallback_snap).is_file()
                assert records.due(app.store, now=app.clock.now()) == []
        finally:
            await app.stop()
    run(scenario())


@pytest.mark.parametrize('part', ['followup', 'failure'])
def test_followup_and_failure_notice_end_after_two_retries(tmp_path, monkeypatch, part):
    async def scenario():
        app = make_app(tmp_path)
        try:
            request, photo = queued(app)
            await initial_words(app, request)
            if part == 'followup':
                ready(app, photo)
            else:
                records.fail(app.store, request.message, 'worker_gone', now=app.clock.now())
            before = replies(app, request.message)
            tries = flaky(app, monkeypatch, lambda turn, meta: meta.photo_request_part == part, 99)
            for _ in range(5):
                await app.clock.advance(61, 61)
                await tick(app)
            ended = records.get(app.store, request.message)
            assert len(tries) == 3 and replies(app, request.message) == before
            assert ended.ended == 'passed'
            assert ended.failure == ('retry_limit' if part == 'followup' else 'worker_gone')
            assert records.snap(app.store, photo.id).status == 'expired'
            if part == 'followup':
                assert records.snap(app.store, photo.id).ended == 'passed'
                assert files.snap(app.data_dir, 101, photo.id).is_file()
            assert records.due(app.store, now=app.clock.now()) == []
        finally:
            await app.stop()
    run(scenario())
