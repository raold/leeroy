import asyncio
import base64
from dataclasses import replace

from conftest import run
from leeroy_mind.photos.look import LOOK_PROMPT, look_at, photo_note
from photos.test_images import jpeg
from selfies.fakes import mind_app


def test_look_call_and_screening(tmp_path, caplog):
    app = mind_app(tmp_path, toml='[photos]\nenabled=true')
    human = replace(app.engine.human, real_name='Quillon', nickname='Captain')
    app.engine.human = human
    app.llm._script = lambda *_: 'Quillon smiles at the camera. Captain is there. A dog lies on a blue rug. It has a yellow ball.'
    async def go():
        try:
            result = await look_at(app, jpeg())
            assert result == 'A dog lies on a blue rug. It has a yellow ball.'
            call = app.llm.calls[-1]
            assert call['think'] is False and call['options']['num_ctx'] == 4096
            assert call['messages'] == [{'role':'user', 'content':LOOK_PROMPT,
                                        'images':[base64.b64encode(jpeg()).decode('ascii')]}]
            assert 'Quillon' not in caplog.text and 'yellow ball' not in caplog.text
        finally:
            await app.stop()
    run(go())


def test_prompt_never_assumes_poster_identity():
    assert 'you' not in LOOK_PROMPT.lower() and 'selfie of' not in LOOK_PROMPT.lower()
    assert 'without guessing who anyone is' in LOOK_PROMPT
    assert photo_note(None) == '[photo]'
    assert photo_note('One. Two. Three.') == '[photo: One. Two.]'


def test_look_failure_and_timeout(tmp_path, monkeypatch):
    app = mind_app(tmp_path, toml='[photos]\nenabled=true')
    async def go():
        try:
            app.llm.fail_next()
            assert await look_at(app, jpeg()) is None
            monkeypatch.setattr('leeroy_mind.photos.look.LOOK_TIMEOUT_S', .01)
            app.llm._gate = asyncio.Event()
            assert await look_at(app, jpeg()) is None
        finally:
            await app.stop()
    run(go())
