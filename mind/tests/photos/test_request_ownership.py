"""An initial request owns its ordinary answer until that queued job actually ends."""
import asyncio

import pytest

from conftest import run
from photos.test_request_recovery import make_app, queued, ready, replies
from photos.test_request_service import drive
from leeroy_mind.photos import request_store as records


@pytest.mark.parametrize('stage', ['queued', 'reserved'])
@pytest.mark.parametrize('finish', ['release', 'cancel', 'error'])
def test_retry_waits_for_initial_answer_owner(tmp_path, monkeypatch, stage, finish):
    async def scenario():
        app = make_app(tmp_path)
        service = app.photos.requests
        entered, release = asyncio.Event(), asyncio.Event()
        attempts = []
        first = True
        original_queue = app.engine._queue

        def tracked(items):
            job = original_queue(items)
            attempts.append(job)
            return job

        async def hold():
            nonlocal first
            if not first:
                return
            first = False
            entered.set()
            await release.wait()
            if finish == 'cancel':
                raise asyncio.CancelledError
            if finish == 'error':
                raise RuntimeError('synthetic answer failure')

        original = app.engine._run if stage == 'queued' else app.engine._compose
        if stage == 'queued':
            async def gated(items):
                await hold()
                return await original(items)
            monkeypatch.setattr(app.engine, '_run', gated)
        else:
            async def gated(turn, meta, **kwargs):
                assert meta.photo is not None
                await hold()
                return await original(turn, meta, **kwargs)
            monkeypatch.setattr(app.engine, '_compose', gated)
        monkeypatch.setattr(app.engine, '_queue', tracked)
        try:
            request, photo = queued(app)
            ready(app, photo)
            assert service.begin(request.message)
            owner = service._tasks[request.message]
            await drive(app, entered.is_set)
            assert (photo.id in service._reserved) == (stage == 'reserved')

            # Advance beyond the retry timer while the real queue/turn is gated.
            # No deadline is enlarged: a turn already composing will expire normally.
            await app.clock.advance(181, 181)
            await service.tick()
            assert service.begin(request.message)
            await asyncio.sleep(0)
            assert service._tasks.get(request.message) is owner and not owner.done()
            assert len(attempts) == 1
            assert replies(app, request.message) == []

            release.set()
            await drive(app, lambda: request.message not in service._tasks)
            assert attempts[0].done() and not service._reserved
            if stage == 'queued' and finish == 'release':
                assert records.get(app.store, request.message).fresh_message is not None
            else:
                assert records.get(app.store, request.message).initial_message is None
                assert records.snap(app.store, photo.id).status == 'ready'
                await service.tick()
                await drive(app, lambda: bool(records.get(app.store, request.message).fresh_message))
                assert len(attempts) == 2

            delivered = records.get(app.store, request.message)
            assert delivered.fresh_message == delivered.initial_message
            assert len(replies(app, request.message)) == 1
            assert app.store.conn.execute('SELECT count(*) FROM discord_attachment WHERE snap=?',
                                          (photo.id,)).fetchone()[0] == 1
        finally:
            release.set()
            await app.stop()
    run(scenario())


def test_request_close_cancels_and_drains_its_reserved_answer(tmp_path, monkeypatch):
    async def scenario():
        app = make_app(tmp_path)
        service = app.photos.requests
        entered, release = asyncio.Event(), asyncio.Event()
        jobs = []
        original = app.engine._compose

        async def gated(turn, meta, **kwargs):
            jobs.append(asyncio.current_task())
            assert meta.photo is not None
            entered.set()
            await release.wait()
            return await original(turn, meta, **kwargs)

        monkeypatch.setattr(app.engine, '_compose', gated)
        try:
            request, photo = queued(app)
            ready(app, photo)
            assert service.begin(request.message)
            await drive(app, entered.is_set)
            async with asyncio.timeout(2):
                await service.close()
            assert jobs and all(job.done() for job in jobs)
            assert not service._tasks and not service._reserved
            assert records.get(app.store, request.message).initial_message is None
            assert records.snap(app.store, photo.id).status == 'ready'
            assert replies(app, request.message) == []
        finally:
            release.set()
            await app.stop()
    run(scenario())


def test_no_queued_answer_releases_owner_and_remains_retryable(tmp_path, monkeypatch):
    async def scenario():
        app = make_app(tmp_path)
        service = app.photos.requests
        original = app.discord_talk.plan
        try:
            request, photo = queued(app)
            ready(app, photo)
            monkeypatch.setattr(app.discord_talk, 'plan', lambda message: [])
            assert service.begin(request.message)
            await drive(app, lambda: not service._tasks)
            assert records.get(app.store, request.message).initial_message is None
            monkeypatch.setattr(app.discord_talk, 'plan', original)
            await app.clock.advance(181, 181)
            await service.tick()
            await drive(app, lambda: bool(records.get(app.store, request.message).fresh_message))
            assert len(replies(app, request.message)) == 1
        finally:
            await app.stop()
    run(scenario())
