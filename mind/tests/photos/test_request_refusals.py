"""A paused GPU or a still-failed reload refuses a request's render; it never fails the request."""
from contextlib import asynccontextmanager

import pytest

from conftest import run
from photos.test_request_recovery import make_app, workers
from photos.test_request_service import drive, offline
from leeroy_mind.llm.borrow import LiveReloadFailed
from leeroy_mind.llm.client import GpuPaused
from leeroy_mind.photos import request_store as records


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
def test_borrow_refused_at_admission_keeps_the_plan_and_sends_a_ready_photo(tmp_path, monkeypatch, error):
    async def scenario():
        app = make_app(tmp_path)
        try:
            await offline(app)
            made = workers(app)
            @asynccontextmanager
            async def refused(holder, *, on_admitted=None):
                raise error
                yield
            monkeypatch.setattr(app.arbiter, 'borrow', refused)
            posted = await app.discord.handle({'action': 'post', 'channel': 'dm:101', 'text': 'send me a selfie'})
            await drive(app, lambda: answered(app, posted['id']))
            request = records.get(app.store, posted['id'])
            assert request.failure is None and request.ended is None and not made
            assert records.snap(app.store, request.fresh_snap).status == 'planned'
            assert request.fallback_message == request.initial_message
            instructions = '\n'.join(m['content'] for call in app.llm.calls for m in call.get('messages', ()))
            assert "didn't work out" not in instructions
        finally:
            await app.stop()
    run(scenario())
