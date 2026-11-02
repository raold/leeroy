"""Application attachment, preemption and model coherence without running services."""
import asyncio
from dataclasses import replace

import pytest

from conftest import run
from nightly.conftest import FakeAdmin
from leeroy_mind.app import MindApp
from leeroy_mind.config import load_config
from leeroy_mind.memory.extract import Extractor
from leeroy_mind.memory.threads import ClosedThread
from leeroy_mind.nightly.gpu import NullAdmin
from leeroy_mind.protocol import Hello, Login, Logout
from leeroy_mind.testing.fake_llm import FakeLLM
from leeroy_mind.llm.scheduler import Expired
from leeroy_mind.talk.turns import Turn, TurnKind


def app_for(h):
    return MindApp(load_config(h.settings.data_dir / "mind.toml"), h.settings.data_dir,
                   llm=FakeLLM(models={h.LIVE, h.REFL, h.EMB}), clock=h.clock)


def context(app):
    return app.nightly.run_night.__self__.make_ctx("2026-10-29", (1.0, 2.0))


def test_attachment_registers_without_starting_and_reuses_app_dependencies(h):
    app = app_for(h)  # constructing outside an event loop must be safe
    try:
        assert isinstance(app.arbiter.admin, NullAdmin)
        assert app._background == [app.nightly.run_forever]
        assert app._bg_tasks == [] and app.nightly.running is None
        ctx = context(app)
        assert ctx.writer is app.writer and ctx.embedder is app.embedder
        assert ctx.personas is app.personas and ctx.arbiter is app.arbiter
        assert ctx.ident.guid == 1 and callable(ctx.asker)
    finally:
        run(app.stop())


def test_login_preempts_before_core_awaits_and_uses_reloaded_live_model(h, monkeypatch):
    async def scenario():
        app = app_for(h)
        admin = FakeAdmin([h.REFL, h.EMB])
        app.arbiter.admin = admin
        (app.data_dir / "mind.toml").write_text('[models]\nlive = "replacement"\n', encoding="utf-8")
        entered, stopped = asyncio.Event(), asyncio.Event()
        async def old_night():
            entered.set()
            try:
                await asyncio.Future()
            finally:
                stopped.set()
        old = asyncio.create_task(old_night())
        await entered.wait()
        app.nightly._task, app.nightly._night = old, "2026-10-29"
        async def core(ev, name):
            assert "human" in app.arbiter.holders and old.cancelling()
            assert app.cfg.models.live == "replacement"
            assert not app._model_known and not app._model_ready
            await asyncio.sleep(0)
            return False
        monkeypatch.setattr(app, "_row", core)
        try:
            app._model_known = app._model_ready = True
            await app.handle(Login(ts=0, guid=1, human=True))
            await app.nightly.handback()
            assert stopped.is_set()
            assert ("preload", "replacement", -1) in admin.calls
            assert not any(call[:2] == ("preload", h.LIVE) for call in admin.calls)
        finally:
            await app.stop()
    run(scenario())


def test_backlog_does_not_change_presence_and_no_bridge_commands(h, monkeypatch):
    async def scenario():
        app = app_for(h)
        sent = []
        monkeypatch.setattr(app.bridge, "send", lambda *args, **kwargs: sent.append(args))
        async def core(*_):
            return False
        monkeypatch.setattr(app, "_row", core)
        try:
            app._hello_ts = 20_000
            await app.handle(Login(ts=1, guid=1, human=True))
            assert not app.arbiter.holders and app.nightly.presence.online is None
            await app.handle(Hello(ts=20_000, server="test", version="1", guild_id=21, human_guid=1))
            await app.nightly.handback()
            assert "human" in app.arbiter.holders
            await app.handle(Logout(ts=1, guid=1, human=True))
            assert "human" in app.arbiter.holders
            await app.handle(Logout(ts=20_001, guid=1, human=True))
            assert not app.arbiter.holders and sent == []
        finally:
            await app.stop()
    run(scenario())


def test_context_reload_updates_dials_and_extract_model_keeps_last_good(h, caplog):
    app = app_for(h)
    try:
        path = app.data_dir / "mind.toml"
        path.write_text('[models]\nlive="new-live"\nreflection="new-reflection"\n'
                        '[life]\nwow_vs_life=8\n', encoding="utf-8")
        app._model_known = app._model_ready = True
        ctx = context(app)
        assert ctx.settings.live_model == app.extractor._model() == "new-live"
        assert ctx.settings.reflection_model == ctx.reasoner.model == "new-reflection"
        assert ctx.settings.wow_vs_life == 8
        assert not app._model_known and not app._model_ready
        path.write_text("[life]\nwow_vs_life = 'bad'\n", encoding="utf-8")
        again = context(app)
        assert again.settings == ctx.settings
        assert app.cfg.models.live == "new-live" and "nightly_config_error" in caplog.text
        app.store.set_meta("human_guid", "102")
        assert context(app).ident.guid == 102
    finally:
        run(app.stop())


def test_transport_and_embedding_stay_coherent_until_restart(h):
    app = app_for(h)
    original = app.cfg
    try:
        (app.data_dir / "mind.toml").write_text('[models]\nlive="new-live"\nembedding="other-embed"\n'
            '[ollama]\nurl="http://127.0.0.1:11111"\n', encoding="utf-8")
        ctx = context(app)
        app._reload_config()
        assert ctx.settings.ollama_url == app.cfg.ollama.url == original.ollama.url
        assert ctx.settings.embedding_model == app.cfg.models.embedding == original.models.embedding
        assert ctx.settings.live_model == app.cfg.models.live == "new-live"
    finally:
        run(app.stop())


def test_extraction_adapter_reconstructs_ordered_thread(h, monkeypatch):
    app = app_for(h)
    try:
        tid = app.store.open_thread("guild", h.T)
        later = h.say(h.T + 10, 101, "later")
        earlier = h.say(h.T, 102, "earlier")
        app.store.set_thread([later, earlier], tid)
        app.store.close_thread(tid, h.T + 20, "pending")
        seen = []
        async def extract(thread):
            seen.append(thread)
            app.store.set_thread_status(tid, h.T + 20, "skipped")
        monkeypatch.setattr(app.extractor, "extract", extract)
        ctx = context(app)
        run(ctx.extract_thread(tid))
        run(ctx.extract_thread(tid))
        assert seen == [ClosedThread(tid, "guild", (earlier, later), "flush")]
    finally:
        run(app.stop())


def test_live_and_nightly_extraction_cannot_duplicate_the_same_thread(h, monkeypatch):
    async def scenario():
        app = app_for(h)
        entered, release = asyncio.Event(), asyncio.Event()
        tid = app.store.open_thread("guild", h.T)
        app.store.close_thread(tid, h.T, "pending")
        writes = []
        async def body(thread):
            if app.store.thread_status(tid) == "extracted":
                return
            entered.set()
            await release.wait()
            writes.append(tid)
            app.store.set_thread_status(tid, h.T, "extracted")
        monkeypatch.setattr(app.extractor, "_extract", body)
        try:
            first = asyncio.create_task(app.extractor.extract(ClosedThread(tid, "guild", (), "flush")))
            await entered.wait()
            second = asyncio.create_task(context(app).extract_thread(tid))
            await asyncio.sleep(0)
            release.set()
            await asyncio.gather(first, second)
            assert writes == [tid] and app.extractor._thread_locks == {}
        finally:
            await app.stop()
    run(scenario())


def test_bot_presence_cannot_reload_models_during_a_night(h, monkeypatch):
    async def scenario():
        app = app_for(h)
        (app.data_dir / "mind.toml").write_text('[models]\nlive="next-login"\n', encoding="utf-8")
        async def core(*_):
            return False
        monkeypatch.setattr(app, "_row", core)
        try:
            await app.handle(Login(ts=0, guid=101, human=False))
            assert app.cfg.models.live == h.LIVE and app.nightly.presence.online is None
        finally:
            await app.stop()
    run(scenario())


@pytest.mark.parametrize("owner", ["human", "voice"])
@pytest.mark.parametrize("expired", [False, True])
def test_live_generation_waits_for_both_handback_owners_and_rechecks_deadline(h, owner, expired):
    async def scenario():
        app = app_for(h)
        entered, release = asyncio.Event(), asyncio.Event()
        async def handback():
            entered.set()
            await release.wait()
        task = asyncio.create_task(handback())
        owners = app.nightly._handbacks if owner == "human" else app.voice._gpu_handbacks
        owners.add(task)
        task.add_done_callback(owners.discard)
        turn = Turn(kind=TurnKind.REPLY_HUMAN, speaker=101, channel="guild", deadline=h.T + 10)
        try:
            await entered.wait()
            answer = asyncio.create_task(app.engine._ask(turn, [{"role": "user", "content": "hello"}]))
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert not app.llm.calls and not answer.done()
            if expired:
                h.clock.advance(11)
            release.set()
            if expired:
                with pytest.raises(Expired):
                    await answer
                assert not app.llm.calls
            else:
                await answer
                assert len(app.llm.calls) == 1
        finally:
            release.set()
            await task
            await app.stop()
    run(scenario())
