"""A paused GPU or a still-failed reload refuses a request's render; it never fails the request."""
from contextlib import asynccontextmanager
from dataclasses import replace

import pytest

from conftest import run
from photos.test_request_recovery import advance_to, make_app, tick, workers
from photos.test_request_service import drive, offline
from leeroy_mind.llm.borrow import LiveReloadFailed
from leeroy_mind.llm.client import GpuPaused
from leeroy_mind.photos import request_store as records
from leeroy_mind.photos.requests import RENDER_WAIT_S, SUBSTITUTE, TAKING_NOW


def answered(app, message):
    return bool((r := records.get(app.store, message)) and r.initial_message)


def test_paused_scene_choice_keeps_the_plan_and_sends_a_ready_photo(tmp_path):
    async def scenario():
        app = make_app(tmp_path)
        try:
            app.night_scheduler.presence.online = True
            words = app.llm._script
            def script(model, messages):
                if 'He asked you for a photo' in messages[-1]['content']:
                    raise GpuPaused('A GPU pause interrupted this request.')
                return words(model, messages)
            app.llm._script = script
            posted = await app.discord.handle({'action': 'post', 'channel': 'dm:101', 'text': 'send me a selfie'})
            await drive(app, lambda: answered(app, posted['id']))
            request = records.get(app.store, posted['id'])
            assert request.failure is None and request.ended is None
            assert records.snap(app.store, request.fresh_snap).status == 'planned'
            assert request.fallback_message == request.initial_message
        finally:
            await app.stop()
    run(scenario())


@pytest.mark.parametrize('error', [GpuPaused('The GPU is paused.'), LiveReloadFailed('live_reload_failed')])
@pytest.mark.parametrize('backend', ['realvisxl', 'flux1-dev'])
@pytest.mark.parametrize('priority', [False, True])
def test_borrow_refused_at_admission_keeps_the_plan_and_sends_a_ready_photo(
        tmp_path, monkeypatch, error, backend, priority):
    async def scenario():
        app = make_app(tmp_path)
        app.selfies.cfg = replace(app.selfies.cfg, backend=backend)
        app.photos.requests.priority = priority
        try:
            await offline(app)
            made = workers(app)
            @asynccontextmanager
            async def refused(holder, *, on_admitted=None, work_timeout=None, priority=False):
                assert work_timeout == (600.0 if backend == 'flux1-dev' else None)
                assert priority is app.photos.requests.priority
                raise error
                yield
            monkeypatch.setattr(app.arbiter, 'borrow', refused)
            posted = await app.discord.handle({'action': 'post', 'channel': 'dm:101', 'text': 'send me a selfie'})
            await drive(app, lambda: answered(app, posted['id']))
            request = records.get(app.store, posted['id'])
            assert request.failure is None and request.ended is None and not made
            assert records.snap(app.store, request.fresh_snap).status == 'planned'
            if priority:
                # On demand she says she's taking it; the queue's borrow is what is refused.
                assert request.fallback_message is None
                await tick(app)
                request = records.get(app.store, posted['id'])
                assert request.failure is None and request.fresh_message is None and not made
                assert records.snap(app.store, request.fresh_snap).status == 'planned'
                # Not rendered within RENDER_WAIT_S: a photo she already had, said honestly.
                await advance_to(app, request.created + RENDER_WAIT_S + 1)
                await tick(app)
                request = records.get(app.store, posted['id'])
                assert request.failure is None and request.fresh_message == request.followup_message
                assert request.fresh_message is not None and not made
                instructions = '\n'.join(m['content'] for call in app.llm.calls for m in call.get('messages', ()))
                assert SUBSTITUTE in instructions and TAKING_NOW in instructions
                return
            assert request.fallback_message == request.initial_message
            instructions = '\n'.join(m['content'] for call in app.llm.calls for m in call.get('messages', ()))
            assert "didn't work out" not in instructions
        finally:
            await app.stop()
    run(scenario())
