"""New private styling reaches Discord through the unchanged fake image worker and guards."""
import json
import logging

import pytest

from conftest import run
from leeroy_mind.photos import request_store as records
from leeroy_mind.selfies.scene import SNAP_NEGATIVE_LUST, SNAP_NEGATIVE_LUST_MIRROR
from photos.test_request_rehearsal import check_handback, lust, rig, script, snapshot
from photos.test_request_service import ON, drive, offline
from photos.test_request_words_scene import REPLY, words_script


@pytest.mark.parametrize("from_words", [False, True])
def test_styled_private_photo_renders_and_sends_one_consistent_recipe(tmp_path, caplog, from_words):
    caplog.set_level(logging.INFO, logger="leeroy_mind")
    toml = ON.replace('[selfies]\n', '[selfies]\nromance_styling=true\n')
    if from_words:
        toml = toml.replace('requests=true\n', 'requests=true\nscene_from_words=true\n')

    def respond(model, messages):
        if '"title": "scene_words"' in messages[-1]['content']:
            return json.dumps({**REPLY, "place": "kitchen at home", "wearing": "casual"})
        return words_script(model, messages) if from_words else script(model, messages)

    async def scenario():
        app, workers, rendered, pids = rig(tmp_path, toml=toml)
        app.llm._script = respond
        lust(app)
        try:
            await offline(app)
            reply = await app.discord.handle({"action": "post", "channel": "dm:101",
                                               "text": "send me a selfie"})
            message = reply["id"]
            await drive(app, lambda: bool((r := snapshot(app, message)) and r.initial_message))
            request = snapshot(app, message)
            assert request.failure is None and request.fresh_message == request.initial_message
            photo = records.snap(app.store, request.fresh_snap)
            assert photo.status == "sent" and photo.audience == "him"
            scene = photo.scene
            assert scene["romance_style"] in ("warm", "playful", "confident")
            assert scene["outfit"] in scene["shows"]
            assert scene["expression"] in scene["shows"]
            assert len(rendered) == 1
            req = rendered[0]
            assert any(scene["outfit"] in text and priority == 0 for text, priority in req.phrases)
            mirror = scene.get("words", {}).get("framing") == "mirror_selfie"
            base = SNAP_NEGATIVE_LUST_MIRROR if mirror else SNAP_NEGATIVE_LUST
            assert set(base) <= set(req.negative)
            assert req.nsfw_max == .2
            assert req.identity is not None
            assert scene["recipe"] == (2 if from_words else 1)
            assert app.store.conn.execute('SELECT count(*) FROM discord_attachment').fetchone()[0] == 1
            check_handback(app, workers, pids)
            logged = '\n'.join(r.getMessage() for r in caplog.records)
            assert scene["outfit"] not in logged and scene["expression"] not in logged
        finally:
            await app.stop()
    run(scenario())
