"""The real app must join training owners before closing bridge or SQLite."""

import asyncio
import json
import sqlite3
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from leeroy_mind.app import MindApp
from leeroy_mind.config import load_config
from leeroy_mind.guild.protocol import Job
from leeroy_mind.protocol import Ack
from leeroy_mind.testing.fake_llm import FakeLLM
from leeroy_mind.training.cards import Directive
from leeroy_mind.training.spells import SpellBook

from .test_tg_applier import Fake, MEMBERS, RUN, card, vocabulary
from .test_tg_gate import _run_async


def create_app(tmp_path, clock):
    path = tmp_path / "mind.toml"
    path.write_text("[training]\nenabled = true\n", encoding="utf-8")
    app = MindApp(load_config(path), tmp_path, llm=FakeLLM(), clock=clock, rng_seed=1)
    app.bridge.close = AsyncMock()
    app.scheduler.close = AsyncMock()
    return app


def observe_drain(app):
    entered = asyncio.Event()
    original = app._shutdown_drains[-1]
    async def drain():
        entered.set()
        await original()
    app._shutdown_drains[-1] = drain
    return entered


@_run_async
async def test_real_app_keeps_resources_for_late_training_job_after_generic_cancel_timeout(tmp_path, clock, monkeypatch):
    import leeroy_mind.app as app_module
    import leeroy_mind.training.applier as applier_module
    import leeroy_mind.training.service as service
    monkeypatch.setattr(app_module, "CANCEL_WAIT_S", 0.001)
    monkeypatch.setattr(applier_module, "_WAIT_S", 0.01)
    monkeypatch.setattr(service, "load_book", lambda path: SpellBook())
    app = create_app(tmp_path, clock)
    ground = app.training
    app._human = 1
    proof = Fake(SimpleNamespace(conn=app.store.conn), clock)
    ground.applier.state_provider = proof.snapshot
    ground.applier.application_allowed = lambda: not ground._stopping
    ground.ledger.first_pull_ts = proof.first_pull_ts
    ground.vocab = vocabulary()
    entered, release = asyncio.Event(), asyncio.Event()
    sent = []
    async def transport(command, *, preserve_id):
        assert preserve_id
        sent.append(command)
        entered.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                pass  # Reproduce a sender that outlives generic cancellation.
        assert not app._closed
        app.store.conn.execute("SELECT 1").fetchone()
        return Ack(0, command.id, True, "")
    app.bridge.send = transport
    app._start_background(ground.run)
    apply = asyncio.create_task(ground.applier.apply(RUN, "boss", "631:0:0",
        card(Directive("threat", "+")), {102: MEMBERS[102]}, tank_guid=101, lead_is_human=False))
    app._bg_tasks.append(apply)
    draining = observe_drain(app)
    caller = None
    try:
        await asyncio.wait_for(entered.wait(), 1)
        caller = asyncio.create_task(app.stop())
        await asyncio.wait_for(draining.wait(), 1)
        assert not caller.done() and not app._closed
        assert app.store.conn.execute("SELECT 1").fetchone()[0] == 1
        app.bridge.close.assert_not_awaited()
        app.scheduler.close.assert_not_awaited()
        # Guild owns a different mailbox and can already have stopped.
        app.guild.ports.jobs.close()
        app.guild.ports.enabled = False
        assert ground.ports.jobs is not app.guild.ports.jobs
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        assert not app._stop_task.done()
        command = sent[0]
        await app.handle(Job(0, command.id, "bot_do", "done", "", "", command.guids,
                             (), 0, 0, 77, 0, 0, 0))
        # Delivery while stopping fills only the correlated training mailbox.
        assert ground.ports.jobs._jobs[command.id] is not None
        release.set()
        await asyncio.wait_for(app.stop(), 1)
        assert app._closed and ground._close_task.done() and not ground._lanes
        assert all(receipt.task is None or receipt.task.done() for receipt in ground.applier._receipts.values())
        assert any(receipt.ok and receipt.settled for receipt in ground.applier._receipts.values())
        app.bridge.close.assert_awaited_once()
        app.scheduler.close.assert_awaited_once()
        with sqlite3.connect(tmp_path / "mind.db") as conn:
            rows = conn.execute("SELECT command FROM tg_applied").fetchall()
            assert rows and any(json.loads(row[0])["settled"] for row in rows)
        await asyncio.sleep(0)
        assert all(task.done() for task in app._bg_tasks)
    finally:
        release.set()
        await app.stop()
        await asyncio.gather(apply, return_exceptions=True)


@_run_async
async def test_real_app_joins_thread_loader_beyond_generic_cancel_timeout(tmp_path, clock, monkeypatch):
    import leeroy_mind.app as app_module
    import leeroy_mind.training.service as service
    monkeypatch.setattr(app_module, "CANCEL_WAIT_S", 0.001)
    entered = asyncio.Event()
    release, finished = threading.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    def load(path):
        loop.call_soon_threadsafe(entered.set)
        release.wait()
        finished.set()
        return SpellBook()
    monkeypatch.setattr(service, "load_book", load)
    app = create_app(tmp_path, clock)
    ground = app.training
    ground._connected = ground._presence_known = True
    # Keep command and sample lanes idle; this test owns only the real loader.
    async def idle():
        await asyncio.Event().wait()
    ground._commands = ground._sampling = ground._maintenance = ground._listening = idle
    app._start_background(ground.run)
    draining = observe_drain(app)
    try:
        await asyncio.wait_for(entered.wait(), 1)
        stopping = asyncio.create_task(app.stop())
        await asyncio.wait_for(draining.wait(), 1)
        assert not stopping.done() and not finished.is_set()
        assert ground._loader is not None and not ground._loader.done()
        assert app.store.conn.execute("SELECT 1").fetchone()[0] == 1
        app.bridge.close.assert_not_awaited()
        release.set()
        await asyncio.wait_for(stopping, 1)
        assert finished.is_set() and ground._loader.done() and app._closed
        assert not ground._lanes
        app.bridge.close.assert_awaited_once()
    finally:
        release.set()
        await app.stop()


@_run_async
async def test_training_close_is_shared_before_run_and_after_background_exit(tmp_path, clock):
    app = create_app(tmp_path, clock)
    ground = app.training
    await asyncio.gather(ground.close(), ground.close(), ground.close())
    drain = ground._close_task
    assert drain.done() and not ground._lanes
    await ground.run()
    await ground.close()
    assert ground._close_task is drain and not ground._running
    await asyncio.gather(app.stop(), app.stop())
    assert app._closed


@_run_async
async def test_drain_exception_cannot_fall_through_to_resource_close(tmp_path, clock):
    app = create_app(tmp_path, clock)
    async def failed():
        raise RuntimeError("synthetic drain failure")
    app.register_shutdown_drain(failed)
    try:
        with pytest.raises(RuntimeError, match="synthetic drain failure"):
            await app.stop()
        assert not app._closed and app.store.conn.execute("SELECT 1").fetchone()[0] == 1
        app.bridge.close.assert_not_awaited()
        app.scheduler.close.assert_not_awaited()
    finally:
        app._shutdown_drains.remove(failed)
        app._stop_task = None
        await app.stop()
