import asyncio
import logging

import pytest

from conftest import run
from leeroy_mind.talk.prompt import comms_rules
from leeroy_mind.talk.turns import Turn, TurnKind
from leeroy_mind.tuning import nudges as N
from tuning.test_nudges import ARGS, TEXT
from tuning.test_tuning_preview import _app, _written


def test_service_set_list_retire_undo_and_no_text_logs(tmp_path, caplog):
    async def go():
        app = _app(tmp_path)
        try:
            r = await app.tuning.handle(ARGS)
            assert r["ok"] and r["change"] == 1
            assert len(app.personas.get(102).nudges) == 1
            listed = await app.tuning.handle(dict(action="nudge", verb="list"))
            assert listed["active"][0]["text"] == TEXT
            r = await app.tuning.handle(dict(action="nudge", verb="retire", id=1))
            assert r["ok"] and not getattr(app.personas.get(102), "nudges", ())
            assert (await app.tuning.handle(dict(action="undo", change=2)))["ok"]
            assert len(app.personas.get(102).nudges) == 1
        finally:
            app.store.close()
    with caplog.at_level(logging.DEBUG):
        run(go())
    assert TEXT not in caplog.text


@pytest.mark.parametrize("scope,channel", [("everywhere", "guild"), ("game", "guild"),
                                         ("discord", "discord:general"), ("whispers", "whisper")])
def test_preview_uses_scoped_prompt_without_writes_or_model_loads(tmp_path, caplog, scope, channel):
    async def go():
        app = _app(tmp_path)
        try:
            before = _written(app)
            r = await app.tuning.handle({**ARGS, "verb": "preview", "scope": scope})
            assert r["ok"] and r["kind"] == "lines", r
            chats = [c for c in app.llm.calls if "messages" in c]
            assert len(chats) == 2
            assert TEXT not in "\n".join(m["content"] for m in chats[0]["messages"])
            assert "\n".join(m["content"] for m in chats[1]["messages"]).count(TEXT) == 1
            assert _written(app) == before and not N.values(app.store.conn)
            assert not app.arbiter.holders and not app.arbiter.admin.calls
        finally:
            app.store.close()
    with caplog.at_level(logging.DEBUG):
        run(go())
    assert TEXT not in caplog.text and "line 1 then" not in caplog.text


def test_preview_refuses_absent_model_before_any_generation(tmp_path):
    async def go():
        app = _app(tmp_path, loaded=False)
        try:
            r = await app.tuning.handle({**ARGS, "verb": "preview"})
            assert not r["ok"] and "isn't loaded" in r["error"]
            assert not app.llm.calls and not app.arbiter.admin.calls
        finally:
            app.store.close()
    run(go())


def test_preview_cancellation_releases_lease_without_writing(tmp_path):
    async def go():
        app = _app(tmp_path)
        entered = asyncio.Event()
        async def wait(*args, **kw):
            entered.set()
            await asyncio.Event().wait()
        app.engine.preview_line = wait
        try:
            task = asyncio.create_task(app.tuning.handle({**ARGS, "verb": "preview"}))
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not app.arbiter.holders and not N.values(app.store.conn)
        finally:
            app.store.close()
    run(go())


def test_live_composition_obeys_scope_and_expiry_without_cache_invalidation(tmp_path):
    async def go():
        app = _app(tmp_path)
        try:
            assert (await app.tuning.handle({**ARGS, "scope": "whispers"}))["ok"]
            async def text(channel, to=None):
                messages, _ = await app.engine.compose(Turn(
                    kind=TurnKind.OPEN, speaker=102, channel=channel, to_guid=to))
                return "\n".join(m["content"] for m in messages)
            assert TEXT not in await text("guild")
            assert (await text("whisper", app.human_guid)).count(TEXT) == 1
            await app.clock.advance(3600, step=3600)
            assert TEXT not in await text("whisper", app.human_guid)
        finally:
            app.store.close()
    run(go())
