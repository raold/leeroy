"""Requested photos read from his words ([photos] scene_from_words), end to end on fakes: a
Discord request, the structured live call, the borrowed fake identity worker and the ordinary
guarded answer; and her day plan putting the photo off while she is asleep. Every person and
word here is invented."""
import asyncio
import json
import logging

import pytest

from conftest import run
from leeroy_mind.photos import request_store as records
from leeroy_mind.photos.config import PhotosConfig, load_photos_config
from leeroy_mind.photos.requests import CANT_NOW, SENT_LATER
from leeroy_mind.selfies import files
from leeroy_mind.selfies.render import scene_scale
from leeroy_mind.selfies.words_scene import WORDS_RECIPE
from photos.test_request_rehearsal import check_handback, rig, script, snapshot
from photos.test_request_service import ON, drive, offline

WORDS_ON = ON.replace("requests=true\n", "requests=true\nscene_from_words=true\n")
ASLEEP_ON = WORDS_ON + "[life]\navailability=6\n"
REPLY = {"place": "supply cupboard at work", "framing": "mirror_selfie", "wearing": "work_uniform",
         "moment": "slipped out for a minute", "time": "now", "lighting": "fluorescent",
         "prop": None}
ASK = "sneak off from work and send me a selfie from the supply cupboard"
HIVIS = "a hi-vis vest over a work t-shirt and work trousers"  # the test cast's job: warehouse supervisor
TOOK = "You just took this photo of yourself for him: "


def words_script(model, messages):
    last = messages[-1]["content"]
    if '"title": "scene_words"' in last:
        return json.dumps(REPLY)
    text = "\n".join(m.get("content", "") for m in messages)
    if "You're in bed for the night" in text:
        return "in bed already, i'll send you one in the morning"
    if SENT_LATER in text:
        return "as promised, here you go"
    if TOOK in text:
        return "two minutes, if anyone sees me i'm finished"
    return script(model, messages)


def prompts(app):
    return ["\n".join(m.get("content", "") for m in c["messages"]) for c in app.llm.calls
            if "messages" in c]


def test_offline_request_renders_his_scene_with_her_face_and_she_knows_it(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="leeroy_mind")

    async def scenario():
        app, workers, rendered, pids = rig(tmp_path, toml=WORDS_ON)
        app.llm._script = words_script
        try:
            assert app.photos.requests.words is True
            await offline(app)
            reply = await app.discord.handle({"action": "post", "channel": "dm:101", "text": ASK})
            message = reply["id"]
            await drive(app, lambda: bool((r := snapshot(app, message)) and r.initial_message))
            request = snapshot(app, message)
            assert request.fresh_message == request.initial_message and request.failure is None
            photo = records.snap(app.store, request.fresh_snap)
            assert photo.status == "sent" and photo.scene["recipe"] == WORDS_RECIPE
            assert (photo.scene["setting"], photo.scene["outfit"]) == ("supply cupboard at work", HIVIS)
            assert len(rendered) == 1
            req = rendered[0]
            for phrase in ("selfie, close arm's-length shot", f"wearing {HIVIS}",
                           "supply cupboard at work"):
                assert (phrase, 0) in req.phrases
            # her face: the reference is her chosen photo, at the identity scale
            assert req.identity.ref == files.history(app.data_dir, 101, photo.pick).absolute()
            assert req.identity.scale == scene_scale(app.selfies.cfg.identity_scale, "upper_body")
            assert (req.shape, req.size) == ("portrait", 1024)  # the snaps size, 832 x 1216
            assert any(TOOK + photo.scene["shows"] in text for text in prompts(app))
            check_handback(app, workers, pids)
            scene_rows = [r.leeroy_fields for r in caplog.records
                          if getattr(r, "leeroy_event", "") == "selfie_request_scene"]
            assert scene_rows == [{"request": message, "guid": 101, "source": "words",
                                   "framing": "mirror_selfie", "wearing": "work_uniform",
                                   "time": "evening", "lighting": "fluorescent", "place": "words",
                                   "moment": "words", "prop": "none", "fallbacks": []}]
            logged = "\n".join(r.getMessage() for r in caplog.records)
            for secret in ("sneak", "supply", "cupboard", "slipped", "hi-vis"):
                assert secret not in logged
        finally:
            await app.stop()
    run(scenario())


def test_asleep_she_says_so_and_the_photo_waits_for_her_next_block(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="leeroy_mind")

    async def scenario():
        app, workers, rendered, pids = rig(tmp_path, toml=ASLEEP_ON)
        app.llm._script = words_script
        try:
            assert app.engine.life.availability == 6
            # 21:30 on a Thursday in Britain: an early-shift worker is in bed until 04:45
            await app.clock.advance(90 * 60, 600)
            await offline(app)
            reply = await app.discord.handle({"action": "post", "channel": "dm:101", "text": ASK})
            message = reply["id"]
            await drive(app, lambda: bool((r := snapshot(app, message)) and r.initial_message))
            request = snapshot(app, message)
            photo = records.snap(app.store, request.fresh_snap)
            assert photo.status == "planned" and photo.scene["wait"] == "asleep"
            assert photo.not_before - request.created == pytest.approx(7.25 * 3600, abs=120)
            assert request.fresh_message is None and request.fallback_snap is None
            assert not rendered and not workers
            assert not app.store.conn.execute("SELECT 1 FROM selfie_request_borrow").fetchone()
            assert not app.store.conn.execute("SELECT 1 FROM discord_attachment").fetchone()
            assert any(CANT_NOW["asleep"] in text for text in prompts(app))
            # his words asked for her work clothes: her job decides what they are
            assert photo.scene["outfit"] == HIVIS
            deferred = [r.leeroy_fields for r in caplog.records
                        if getattr(r, "leeroy_event", "") == "selfie_request_deferred"]
            assert deferred and deferred[0]["block"] == "asleep" and deferred[0]["request"] == message
            # nothing happens while she sleeps
            await app.clock.advance(3600, 600)
            await app.photos.requests.tick()
            assert not rendered and snapshot(app, message).fresh_message is None
            # once she is up, the next pass renders it and she sends it with a line
            await app.clock.advance(photo.not_before - app.clock.now() + 1, 600)
            pending = asyncio.create_task(app.photos.requests.tick())
            await drive(app, lambda: bool(snapshot(app, message).fresh_message))
            await pending
            done = snapshot(app, message)
            assert done.fresh_message == done.followup_message != done.initial_message
            assert records.snap(app.store, done.fresh_snap).status == "sent" and len(rendered) == 1
            assert any(SENT_LATER in text and TOOK in text for text in prompts(app))
            check_handback(app, workers, pids)
        finally:
            await app.stop()
    run(scenario())


def test_a_deferred_render_rescreens_the_cast_names(tmp_path):
    """A member renamed while she slept to a word of her place: the render is refused and she
    says it didn't work out, rather than a photo whose scene names a member."""
    from leeroy_mind.protocol import Member

    async def scenario():
        app, workers, rendered, pids = rig(tmp_path, toml=ASLEEP_ON)
        app.llm._script = words_script
        try:
            await app.clock.advance(90 * 60, 600)
            await offline(app)
            reply = await app.discord.handle({"action": "post", "channel": "dm:101", "text": ASK})
            message = reply["id"]
            await drive(app, lambda: bool((r := snapshot(app, message)) and r.initial_message))
            photo = records.snap(app.store, snapshot(app, message).fresh_snap)
            assert photo.status == "planned" and photo.scene["wait"] == "asleep"
            app.store.upsert_cast([Member(guid=102, name="Supply", race=4, class_=11, gender=0,
                                          level=80, rank=3, online=False, map=0, zone=0,
                                          spec_tab=-1)], app.clock.now())
            await app.clock.advance(photo.not_before - app.clock.now() + 1, 600)
            pending = asyncio.create_task(app.photos.requests.tick())
            await drive(app, lambda: bool(snapshot(app, message).failure_message))
            await pending
            done = snapshot(app, message)
            assert done.failure == "scene_names" and done.fresh_message is None
            assert not rendered and not workers
            assert not app.store.conn.execute("SELECT 1 FROM discord_attachment").fetchone()
        finally:
            await app.stop()
    run(scenario())


def test_commuting_reads_her_way_to_or_from_work_unless_she_drives():
    from datetime import datetime
    from types import SimpleNamespace
    from conftest import make_persona
    from leeroy_mind.life.presence import presence_at
    from leeroy_mind.photos.requests import RequestPhotos

    def wait(p, at):
        clock = SimpleNamespace(local=lambda: at, now=lambda: 1000.0)
        life = SimpleNamespace(presence=lambda guid, medium: presence_at(p, at, medium=medium))
        service = SimpleNamespace(app=SimpleNamespace(clock=clock, engine=SimpleNamespace(life=life)))
        request = SimpleNamespace(guid=101, expires=1000.0 + 86400)
        return RequestPhotos._wait(service, request, p)

    office = make_persona(101, "Arkon", rhythm="office admin, nine till five", region="US")
    assert wait(office, datetime(2026, 11, 19, 8, 30))[2] == "to_work"
    assert wait(office, datetime(2026, 11, 19, 17, 10))[2] == "from_work"
    assert wait(office, datetime(2026, 11, 19, 3, 0))[2] == "asleep"
    assert wait(office, datetime(2026, 11, 19, 12, 0)) is None  # at work she can sneak one
    driver = make_persona(101, "Arkon", job="delivery driver", rhythm="nine till five",
                          region="US")
    assert wait(driver, datetime(2026, 11, 19, 8, 30))[2] == "driving"
    assert "driving" not in CANT_NOW["to_work"] and "on your way to work" in CANT_NOW["to_work"]
    assert "on your way home" in CANT_NOW["from_work"]


def test_switch_off_never_reads_his_words_into_a_scene(tmp_path, monkeypatch):
    def refused(*args, **kwargs):
        raise AssertionError("the words path ran with the switch off")
    monkeypatch.setattr("leeroy_mind.selfies.request_scene.choose_words_scene", refused)

    async def scenario():
        app, workers, rendered, pids = rig(tmp_path, toml=ON.replace(
            "requests=true\n", "requests=true\nscene_from_words=false\n") + "[life]\navailability=6\n")
        try:
            assert app.photos.requests.words is False
            await app.clock.advance(90 * 60, 600)  # asleep: the off path never waits for her
            await offline(app)
            reply = await app.discord.handle({"action": "post", "channel": "dm:101", "text": ASK})
            message = reply["id"]
            await drive(app, lambda: bool((r := snapshot(app, message)) and r.initial_message))
            photo = records.snap(app.store, snapshot(app, message).fresh_snap)
            assert photo.scene["recipe"] != WORDS_RECIPE and "wait" not in photo.scene
            assert photo.status == "sent" and len(rendered) == 1
        finally:
            await app.stop()
    run(scenario())


@pytest.mark.parametrize("text,expected", [
    ("", PhotosConfig()),
    ("[photos]\nsend=true\nrequests=true\n", PhotosConfig(send=True, requests=True)),
    ("[photos]\nsend=true\nrequests=true\nscene_from_words=true\n",
     PhotosConfig(send=True, requests=True, scene_from_words=True)),
    ('[photos]\nsend=true\nrequests=true\nscene_from_words="yes"\n', PhotosConfig()),
])
def test_the_switch_defaults_off_and_an_invalid_value_turns_the_table_off(tmp_path, text, expected):
    (tmp_path / "mind.toml").write_text(text, encoding="utf-8")
    assert load_photos_config(tmp_path / "mind.toml") == expected
