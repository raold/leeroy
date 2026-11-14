"""Fake-only rehearsal: Discord request -> live enum choice -> borrowed identity worker.

The subprocess is the existing standard-library fake worker; it loads no models. Its 8px
JPEG fixture is replaced by a protocol-sized synthetic JPEG for Discord's image-size gate.
"""
import asyncio
from dataclasses import replace
from datetime import timedelta
import json
import logging

import pytest

from conftest import TEST_CAST, run
from leeroy_mind.app import MindApp
from leeroy_mind.config import load_config
from leeroy_mind.console.app import typing_entries
from leeroy_mind.endpoint import ControlCfg, ControlEndpoint
from leeroy_mind.personas.io import load_persona, save_persona
from leeroy_mind.photos import request_store as records
from leeroy_mind.photos.notes import by_messages
from leeroy_mind.selfies import files, snaps
from leeroy_mind.selfies.job import pid_alive
from leeroy_mind.selfies.worker import SelfieWorker
from leeroy_mind.testing.fake_clock import FakeClock
from leeroy_mind.testing.fake_llm import FakeLLM
from leeroy_mind.wow import CLASSES, RACES
from photos.test_images import jpeg
from photos.test_request_service import make_app, offline, drive
from nightly.conftest import FakeAdmin
from selfies.fakes import fake_command, until
from selfies.test_scene_choices import PICKS


def script(model, messages):
    """Live JSON and captions are invented, deterministic replies, with no bypassed guards."""
    last = messages[-1]["content"]
    if "matching this schema:" in last:
        schema, _ = json.JSONDecoder().raw_decode(last.rsplit("matching this schema:", 1)[1].lstrip())
        choices = {**PICKS, "when": "any"}
        if "colour" in schema["properties"]:
            choices.update(setting="her living room couch", activity="relaxing", prop="none",
                           outfit="satin midi dress", colour="navy", expression="laughing")
        return json.dumps(choices)
    text = "\n".join(m.get("content", "") for m in messages)
    if "You sent the photo he asked for to him privately." in text:
        return "sent it to you in our dm"
    if "The photo he asked for will be private between you two." in text:
        return "i'll send it in our dm when it's ready"
    if "The new photo you meant to send didn't work out." in text:
        return "that one didn't work out, i'll try another time"
    if "This is the fresh photo he asked you for earlier;" in text:
        return "here's the one i took for you after that"
    return "the quiet afternoon made a good excuse for a photo"


def rig(tmp_path, *, gate=None, entered=None, fail=False):
    app = make_app(tmp_path)
    app.llm._script = script
    workers, requests = [], []
    pids = tmp_path / "worker-pids.json"
    env = {"FAKE_SELFIE_IDENTITY": "1", "FAKE_SELFIE_CHILD": "1", "FAKE_SELFIE_PIDS": str(pids)}
    if fail:
        env["FAKE_SELFIE_DIE"] = "1"
    command, environ = fake_command(tmp_path, **env)

    class Worker(SelfieWorker):
        async def start(self):
            answer = await super().start()
            app.arbiter.admin.calls.append(("worker_start",))
            return answer

        async def render(self, req, *, rid=None):
            requests.append(req)
            if entered is not None:
                entered.set()
            if gate is not None:
                await gate.wait()
            answer = await super().render(req, rid=rid)
            if not answer.flagged:
                req.out.write_bytes(jpeg(answer.width, answer.height))
                req.thumb.write_bytes(jpeg(263, 384))
            app.arbiter.admin.calls.append(("worker_render",))
            return answer

        async def stop(self):
            await super().stop()
            app.arbiter.admin.calls.append(("worker_stop",))

    def factory():
        worker = Worker(command, env=environ)
        workers.append(worker)
        return worker

    app.photos.requests.worker_factory = factory
    return app, workers, requests, pids


def snapshot(app, message):
    return records.get(app.store, message)


def lust(app):
    p = load_persona(app.store, 101)
    _, race, cls = TEST_CAST[101]
    p = replace(p, identity=replace(p.identity, player_gender="female", age=30),
                romance=replace(p.romance, stance="lust"))
    save_persona(app.store, p, source="seed", ts=app.clock.now(), race=RACES[race], class_name=CLASSES[cls])
    app.personas.invalidate(101)


def check_handback(app, workers, pids):
    assert workers and all(not worker.running for worker in workers)
    ids = json.loads(pids.read_text())
    assert not pid_alive(ids["pid"]) and not pid_alive(ids["child"])
    calls = app.arbiter.admin.calls
    stopped = max(i for i, call in enumerate(calls) if call == ("worker_stop",))
    preload = max(i for i, call in enumerate(calls) if call == ("preload", app.cfg.models.live, -1))
    assert stopped < preload
    assert app.cfg.models.live in app.arbiter.admin.loaded
    assert ("unload", app.cfg.models.embedding) not in calls
    assert not app.live_borrow.blocked


def metrics(caplog, record_property, message):
    rows = [record.leeroy_fields for record in caplog.records
            if getattr(record, "leeroy_event", None) == "selfie_request_render"
            and record.leeroy_fields["request"] == message]
    assert len(rows) == 1
    measured = rows[0]
    assert all(measured[key] >= 0 for key in
               ("ms", "start_ms", "render_ms", "unload_ms", "stop_ms", "reload_ms"))
    assert measured["start_ms"] > 0
    record_property("fake_borrow_metrics", json.dumps({"fake": True, **measured}, sort_keys=True))
    return measured


@pytest.mark.parametrize("channel", ["dm:101", "general"])
def test_offline_request_sends_one_fresh_photo_after_handback(tmp_path, caplog, record_property, channel):
    caplog.set_level(logging.INFO, logger="leeroy_mind")
    async def scenario():
        app, workers, rendered, pids = rig(tmp_path)
        try:
            await offline(app)
            name = app.world.discord_members()[101].name
            reply = await app.discord.handle({"action": "post", "channel": channel,
                                               "text": f"@{name} send me a selfie walking your dog"})
            assert reply["ok"]
            message = reply["id"]
            await drive(app, lambda: bool((r := snapshot(app, message)) and r.initial_message))
            request = snapshot(app, message)
            assert request.fresh_message == request.initial_message
            assert request.fallback_snap is None and request.failure is None
            photo = records.snap(app.store, request.fresh_snap)
            assert (photo.source, photo.status, photo.request) == ("request", "sent", message)
            assert len(rendered) == 1 and rendered[0].identity.ref.name == f"{photo.pick}.jpg"
            assert app.store.conn.execute("SELECT count(*) FROM discord_attachment").fetchone()[0] == 1
            assert app.store.conn.execute("SELECT count(*) FROM discord_message WHERE origin='mind' AND reply_to=?",
                                          (message,)).fetchone()[0] == 1
            assert snaps.sent_count(app.store, 0) == 0
            check_handback(app, workers, pids)
            assert metrics(caplog, record_property, message)["outcome"] == "ready"
        finally:
            await app.stop()
    run(scenario())


def test_online_gallery_then_logout_grace_delivers_fresh_followup(tmp_path, caplog, record_property):
    caplog.set_level(logging.INFO, logger="leeroy_mind")
    async def scenario():
        app, workers, rendered, pids = rig(tmp_path)
        try:
            app.night_scheduler.presence.online = True
            retained = {path: path.read_bytes() for path in tmp_path.rglob("*.jpg")}
            reply = await app.discord.handle({"action": "post", "channel": "dm:101", "text": "send me a selfie"})
            message = reply["id"]
            await drive(app, lambda: bool((r := snapshot(app, message)) and r.initial_message))
            initial = snapshot(app, message)
            assert initial.fallback_message == initial.initial_message and initial.fresh_message is None
            assert records.snap(app.store, initial.fresh_snap).status == "planned"
            assert not workers and not app.store.conn.execute("SELECT 1 FROM selfie_request_borrow").fetchone()
            await offline(app)
            await app.clock.advance(599, 599)
            await app.photos.requests.tick()
            assert not workers and snapshot(app, message).fresh_message is None
            # The service's next bounded queue pass follows the 600-second grace.
            await app.clock.advance(61, 61)
            pending = asyncio.create_task(app.photos.requests.tick())
            await drive(app, lambda: bool(snapshot(app, message).fresh_message))
            await pending
            request = snapshot(app, message)
            assert request.initial_message == initial.initial_message
            assert request.fresh_message == request.followup_message != request.fallback_message
            assert records.snap(app.store, request.fresh_snap).status == "sent"
            assert len(rendered) == 1
            assert app.store.conn.execute("SELECT count(*) FROM discord_attachment").fetchone()[0] == 2
            assert all(path.read_bytes() == data for path, data in retained.items())
            check_handback(app, workers, pids)
            metrics(caplog, record_property, message)
        finally:
            await app.stop()
    run(scenario())


def test_lust_channel_request_sends_private_and_public_receipt_has_no_scene(tmp_path, caplog, record_property):
    caplog.set_level(logging.INFO, logger="leeroy_mind")
    async def scenario():
        app, workers, rendered, pids = rig(tmp_path)
        try:
            lust(app)
            await offline(app)
            name = app.world.discord_members()[101].name
            reply = await app.discord.handle({"action": "post", "channel": "general", "text": f"@{name} selfie?"})
            message = reply["id"]
            await drive(app, lambda: bool((r := snapshot(app, message)) and r.initial_message and r.fresh_message))
            request = snapshot(app, message)
            assert (request.audience, request.photo_channel) == ("him", "dm:101")
            assert request.initial_message != request.fresh_message
            public = app.store.conn.execute("SELECT * FROM discord_message WHERE id=?", (request.initial_message,)).fetchone()
            private = app.store.conn.execute("SELECT * FROM discord_message WHERE id=?", (request.fresh_message,)).fetchone()
            assert public["channel"] == "general" and "dm" in public["text"]
            assert private["channel"] == "dm:101"
            assert not app.store.conn.execute("SELECT 1 FROM discord_attachment WHERE message=?", (request.initial_message,)).fetchone()
            photo = records.snap(app.store, request.fresh_snap)
            public_prompts = ["\n".join(m.get("content", "") for m in call["messages"])
                              for call in app.llm.calls if "messages" in call
                              and any("You sent the photo he asked for to him privately." in m.get("content", "")
                                      for m in call["messages"])]
            assert public_prompts
            assert all(photo.scene["shows"] not in prompt for prompt in public_prompts)
            assert by_messages(app.store.conn, [request.initial_message], members=True) == {}
            assert len(rendered) == 1
            check_handback(app, workers, pids)
            metrics(caplog, record_property, message)
        finally:
            await app.stop()
    run(scenario())


def test_worker_failure_stops_child_reloads_live_and_answers_once(tmp_path, caplog, record_property):
    caplog.set_level(logging.INFO, logger="leeroy_mind")
    async def scenario():
        app, workers, rendered, pids = rig(tmp_path, fail=True)
        try:
            await offline(app)
            reply = await app.discord.handle({"action": "post", "channel": "dm:101", "text": "send me a selfie"})
            message = reply["id"]
            await drive(app, lambda: bool((r := snapshot(app, message)) and r.initial_message))
            request = snapshot(app, message)
            assert request.failure and request.failure_message == request.initial_message
            assert not app.store.conn.execute("SELECT 1 FROM discord_attachment").fetchone()
            assert records.snap(app.store, request.fresh_snap).status == "expired"
            assert len(rendered) == 1
            check_handback(app, workers, pids)
            assert metrics(caplog, record_property, message)["outcome"] == "failed"
        finally:
            await app.stop()
    run(scenario())


def test_comms_typing_stays_visible_while_worker_is_slow(tmp_path, caplog, record_property):
    caplog.set_level(logging.INFO, logger="leeroy_mind")
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        app, workers, rendered, pids = rig(tmp_path, gate=release, entered=entered)
        try:
            await offline(app)
            reply = await app.discord.handle({"action": "post", "channel": "dm:101", "text": "send me a selfie"})
            async with asyncio.timeout(5):
                await entered.wait()
            message = reply["id"]
            assert app.live_borrow.blocked and snapshot(app, message).initial_message is None
            endpoint = ControlEndpoint(app, ControlCfg(), key="K" * 43)
            for _ in range(3):
                await app.clock.advance(5, 5)
                strip = typing_entries(endpoint.status()["typing"])
                assert any(row["guid"] == 101 and row["channel"] == "dm:101"
                           and row["to"] == 1 and row["phase"] == "composing" and row["age"] <= 5
                           for row in strip)
            release.set()
            await drive(app, lambda: bool(snapshot(app, message).initial_message))
            check_handback(app, workers, pids)
            metrics(caplog, record_property, message)
        finally:
            release.set()
            await app.stop()
    run(scenario())


@pytest.mark.parametrize("recovery", ["repair", "restart"])
def test_render_survives_failed_live_reload_and_delivers_once(tmp_path, caplog, record_property, recovery):
    caplog.set_level(logging.INFO, logger="leeroy_mind")
    async def scenario():
        app, workers, rendered, pids = rig(tmp_path)
        old_admin = app.arbiter.admin
        original = old_admin.preload
        repaired, failures = False, []
        async def preload(model, keep_alive, *, options=None):
            if ("worker_stop",) in old_admin.calls and not repaired:
                failures.append(model)
                raise RuntimeError("synthetic live reload failure")
            await original(model, keep_alive, options=options)
        old_admin.preload = preload
        try:
            await offline(app)
            reply = await app.discord.handle({"action": "post", "channel": "dm:101", "text": "send me a selfie"})
            message = reply["id"]
            await until(lambda: any(getattr(record, "leeroy_event", None) == "selfie_request_render"
                and record.leeroy_fields.get("outcome") == "ready_reload_pending" for record in caplog.records))
            request = snapshot(app, message)
            photo = records.snap(app.store, request.fresh_snap)
            assert photo.status == "ready" and request.failure is None
            assert request.initial_message is None and request.fresh_message is None
            assert len(failures) >= 2 and len(rendered) == 1
            retained = files.snap(app.data_dir, 101, photo.id).read_bytes()
            ids = json.loads(pids.read_text())
            assert not pid_alive(ids["pid"]) and not pid_alive(ids["child"])
            if recovery == "repair":
                repaired = True
                await app.arbiter.recover_borrow()
                await app.clock.advance(181, 181)
            else:
                next_day = app.clock.local() + timedelta(days=2)
                await app.stop()
                # Reopen the durable application state, without reseeding any photo or receipt.
                app = MindApp(load_config(tmp_path / "mind.toml"), tmp_path,
                              llm=FakeLLM(script=script), clock=FakeClock(next_day))
                app.llm.set_models({app.cfg.models.live, app.cfg.models.embedding})
                app.arbiter.admin = FakeAdmin([app.cfg.models.live, app.cfg.models.embedding])
                def unexpected_worker():
                    pytest.fail("ready recovery must not render again")
                app.photos.requests.worker_factory = unexpected_worker
            await app.photos.requests.tick()
            await drive(app, lambda: bool(snapshot(app, message).fresh_message))
            delivered = snapshot(app, message)
            assert delivered.fresh_message == delivered.initial_message
            assert delivered.failure is None
            assert records.snap(app.store, photo.id).status == "sent"
            assert files.snap(app.data_dir, 101, photo.id).read_bytes() == retained
            assert app.store.conn.execute("SELECT count(*) FROM selfie_request_borrow").fetchone()[0] == 1
            assert app.store.conn.execute("SELECT count(*) FROM discord_attachment").fetchone()[0] == 1
            await app.photos.requests.tick()
            assert app.store.conn.execute("SELECT count(*) FROM discord_attachment").fetchone()[0] == 1
            assert metrics(caplog, record_property, message)["outcome"] == "ready_reload_pending"
        finally:
            await app.stop()
    run(scenario())


@pytest.mark.parametrize("change", ["delete", "withdraw"])
def test_withdrawn_channel_request_cannot_send_private_photo_or_failure(tmp_path, change):
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        app, workers, _, pids = rig(tmp_path, gate=release, entered=entered)
        try:
            lust(app)
            await offline(app)
            name = app.world.discord_members()[101].name
            reply = await app.discord.handle({"action": "post", "channel": "general", "text": f"@{name} selfie?"})
            message = reply["id"]
            async with asyncio.timeout(5):
                await entered.wait()
            action = {"action": "delete" if change == "delete" else "edit", "message": message}
            if change == "withdraw":
                action["text"] = f"@{name} do not send a selfie"
            assert (await app.discord.handle(action))["ok"]
            release.set()
            await drive(app, lambda: snapshot(app, message).ended == "cancelled")
            assert not app.store.conn.execute("SELECT 1 FROM discord_message WHERE origin='mind'").fetchone()
            assert not app.store.conn.execute("SELECT 1 FROM discord_attachment").fetchone()
            await app.clock.advance(181, 181)
            await app.photos.requests.tick()
            assert not app.store.conn.execute("SELECT 1 FROM discord_message WHERE origin='mind'").fetchone()
            check_handback(app, workers, pids)
        finally:
            release.set()
            await app.stop()
    run(scenario())
