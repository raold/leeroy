"""Requested scenes use the live queue and vetted enums, without a model or GPU."""
import asyncio
from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from conftest import TEST_CAST, run
from leeroy_mind.llm.client import ChatResult, GpuPaused, LLMError
from leeroy_mind.llm.scheduler import Priority
from leeroy_mind.personas.io import load_persona, save_persona
from leeroy_mind.selfies import files
from leeroy_mind.selfies.request_scene import RequestSceneError, choose_scene
from leeroy_mind.selfies.scene import build_scene_prompt, scene_json
from leeroy_mind.selfies.scene_choices import RECIPE
from leeroy_mind.selfies.store import current, pick
from leeroy_mind.wow import CLASSES, RACES
from nightly.conftest import IDENT
from selfies.fakes import ready
from selfies.test_scene_choices import PICKS
from selfies.test_selfie_prompt import woman


class ChoiceModel:
    def __init__(self, events):
        self.events, self.calls, self.answers = events, [], [PICKS]

    async def chat(self, model, messages, **kwargs):
        self.events.append("chat")
        self.calls.append((model, messages, kwargs))
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return ChatResult(json.dumps(answer) if isinstance(answer, dict) else answer, "", 50, .01)


class SceneQueue:
    def __init__(self, llm, events):
        self.llm, self.events, self.priorities = llm, events, []

    async def submit(self, priority, job):
        self.priorities.append(priority)
        self.events.append("queued")
        return await job(self.llm)


def save(app, p):
    _, race, cls = TEST_CAST[p.guid]
    save_persona(app.store, p, source="seed", ts=app.clock.now(),
                 race=RACES[race], class_name=CLASSES[cls])
    app.personas.invalidate(p.guid)


@pytest.fixture
def app(svc):
    app = svc.app
    app.selfies = svc
    ready(app.store, app.data_dir, 101, 1)
    pick(app.store, 101, 1, 0, source="console", now=2)
    files.sync_chosen(app.data_dir, 101, current(app.store, 101))
    # Discord delivery's 25+ floor is independent of the legacy profile floor.
    p = load_persona(app.store, 101)
    save(app, replace(p, identity=replace(p.identity, age=30)))
    app.events = []
    async def admission():
        app.events.append("admitted")
    app.engine = SimpleNamespace(human=IDENT, live_admission=admission)
    app.cfg = SimpleNamespace(models=SimpleNamespace(live="invented-live"))
    app.llm = ChoiceModel(app.events)
    app.scheduler = SceneQueue(app.llm, app.events)
    return app


def test_scene_uses_live_queue_and_admission_inside_job(app):
    scene = run(choose_scene(app, 101, "send me a selfie walking your dog", "him"))
    assert app.events == ["queued", "admitted", "chat"]
    assert app.scheduler.priorities == [Priority.REPLY_HUMAN]
    assert (scene.audience, scene.when, scene.recipe) == ("him", "any", RECIPE)
    assert scene.activity == "walking a dog on a leash, holding a dog leash"
    model, _, options = app.llm.calls[0]
    assert model == "invented-live" and options["think"] is False
    assert options["options"]["num_ctx"] == 4096
    assert options["options"]["num_predict"] >= 256
    assert "stop" not in options["options"]


@pytest.mark.parametrize("bad", ["not json", [], {**PICKS, "outfit": "silk nightwear"},
    {**PICKS, "activity": "cooking", "setting": "desk"},
    {**PICKS, "activity": "cooking", "setting": "kitchen", "prop": "leash"},
    {**PICKS, "photo": False}])
def test_one_retry_then_deterministic_screened_everyday_scene(app, bad):
    app.llm.answers = [bad, bad]
    scene = run(choose_scene(app, 101, "a request the choices cannot meet", "anyone"))
    assert len(app.llm.calls) == 2
    assert (scene.setting, scene.activity, scene.outfit, scene.audience, scene.when) == (
        "in a living room", "taking a quiet break", "a cotton shirt and trousers", "anyone", "any")
    assert scene.recipe == RECIPE


def test_single_retry_accepts_corrected_choice_without_a_second_retry_loop(app):
    app.llm.answers = [{**PICKS, "setting": "desk"}, PICKS]
    scene = run(choose_scene(app, 101, "send a photo", "him"))
    assert len(app.llm.calls) == 2 and scene.setting == "on a quiet park path"


def test_raw_words_and_unexpected_keys_do_not_escape_into_scene_or_retry_diagnostics(app):
    secret = "PRIVATE_UNTRUSTED_REQUEST_WORDS"
    app.llm.answers = [{**PICKS, secret: secret}, PICKS]
    scene = run(choose_scene(app, 101, secret, "him"))
    assert secret not in json.dumps(scene_json(scene))
    assert secret not in str(build_scene_prompt(load_persona(app.store, 101), scene))
    retry = app.llm.calls[1][1][-1]["content"]
    assert secret not in retry.split("Your previous reply was invalid")[1]
    assert secret in app.llm.calls[0][1][-1]["content"]
    assert not app.store.conn.execute("SELECT count(*) FROM selfie_snap").fetchone()[0]


@pytest.mark.parametrize("audience", ["him", "anyone"])
def test_model_cannot_change_destination_or_time(app, audience):
    app.llm.answers = [{**PICKS, "audience": "anyone" if audience == "him" else "him", "when": "late"}]
    scene = run(choose_scene(app, 101, "photo please", audience))
    assert (scene.audience, scene.when) == (audience, "any")


def test_lust_uses_its_schema_and_channel_still_means_private(app):
    save(app, woman(stance="lust", age=30))
    app.llm.answers = ["invalid", "invalid"]
    scene = run(choose_scene(app, 101, "photo please", "anyone"))
    assert (scene.style, scene.audience, scene.outfit) == ("lust", "him", "a navy satin midi dress")
    assert "colour" in app.llm.calls[0][2]["format"]["properties"]


def test_lust_swimwear_pairing_gets_only_one_retry(app):
    save(app, woman(stance="lust", age=30))
    bad = {**PICKS, "setting": "her living room couch", "activity": "relaxing", "prop": "none",
           "outfit": "string bikini", "colour": "navy", "expression": "laughing"}
    app.llm.answers = [bad, bad]
    scene = run(choose_scene(app, 101, "photo please", "him"))
    assert "bikini" not in scene.outfit and len(app.llm.calls) == 2


def test_fallback_obeys_the_same_name_screen(app):
    app.engine.human = replace(IDENT, nickname="Living")
    app.llm.answers = ["invalid", "invalid"]
    with pytest.raises(RequestSceneError, match="unrenderable"):
        run(choose_scene(app, 101, "photo please", "him"))


@pytest.mark.parametrize("missing", ["age", "looks", "kind", "pick", "history"])
def test_missing_identity_or_under25_never_calls_model(app, missing):
    p = load_persona(app.store, 101)
    if missing == "age":
        save(app, replace(p, identity=replace(p.identity, age=24)))
    elif missing == "looks":
        save(app, replace(p, appearance=None))
    elif missing == "kind":
        save(app, replace(p, photo=None))
    elif missing == "pick":
        with app.store.transaction():
            app.store.conn.execute("DELETE FROM selfie_current WHERE guid=101")
    else:
        files.history(app.data_dir, 101, current(app.store, 101).pick).unlink()
    with pytest.raises(RequestSceneError):
        run(choose_scene(app, 101, "photo please", "him"))
    assert app.llm.calls == []


def test_model_failure_uses_fallback_without_repeating_failed_service(app):
    app.llm.answers = [LLMError("unavailable")]
    assert run(choose_scene(app, 101, "photo please", "him")).setting == "in a living room"
    assert len(app.llm.calls) == 1


def test_without_model_takes_the_screened_everyday_choice(app):
    assert run(choose_scene(app, 101, "photo please", "him", model=False)).setting == "in a living room"
    assert app.llm.calls == []


@pytest.mark.parametrize("error", [GpuPaused("paused"), asyncio.CancelledError()])
def test_pause_or_cancellation_is_not_converted_to_a_scene(app, error):
    app.llm.answers = [error]
    with pytest.raises(type(error)):
        run(choose_scene(app, 101, "photo please", "him"))
