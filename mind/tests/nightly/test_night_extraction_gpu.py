"""Ordinary extraction cannot reload live chat while the night owns model work."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from conftest import run
from leeroy_mind.app import MindApp
from leeroy_mind.llm.scheduler import LLMScheduler
from leeroy_mind.memory.extract import Extractor
from leeroy_mind.memory.threads import ClosedThread
from leeroy_mind.nightly.gpu import GpuArbiter
from leeroy_mind.nightly.pipeline import stage_threads
from leeroy_mind.nightly.scheduler import NightScheduler
from nightly.conftest import FakeAdmin


def setup(h, run_night, extract):
    ctx = h.ctx()
    extractor = Extractor(h.store, LLMScheduler(ctx.reasoner.llm, clock=h.clock), ctx.writer,
                          h.LIVE, human_guid=lambda: h.human,
                          bystanders=lambda *_: [], human=lambda: h.ident)
    extractor._extract = extract  # keep the real per-thread serialization and worker loop
    settings = replace(h.settings, grace_minutes=0, min_window_hours=0, min_gap_hours=0)
    nightly = NightScheduler(h.store, settings, GpuArbiter(FakeAdmin([]), settings),
                             run_night, clock=h.clock)
    app = SimpleNamespace(_extract_queue=asyncio.Queue(), nightly=nightly, extractor=extractor,
                          engine=SimpleNamespace(live_admission=None))
    return app, ctx


def pending(h, key):
    tid = h.store.open_thread(key, h.T)
    h.store.close_thread(tid, h.T, "pending")
    return ClosedThread(tid, key, (), "flush")


async def cancel(task):
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def settle():
    for _ in range(8):
        await asyncio.sleep(0)


def test_new_ordinary_thread_waits_through_reflection_then_runs(h):
    async def go():
        reflecting, finished = asyncio.Event(), asyncio.Event()
        calls = []
        async def night(*_):
            reflecting.set()
            await finished.wait()
        async def extract(closed):
            assert finished.is_set()
            calls.append(closed.thread_id)
            h.store.set_thread_status(closed.thread_id, h.T, "extracted")
        app, _ = setup(h, night, extract)
        await app.nightly.tick()
        await reflecting.wait()
        closed = pending(h, "guild-later")
        app._extract_queue.put_nowait(closed)
        worker = asyncio.create_task(MindApp._extract_worker(app))
        try:
            await settle()
            assert calls == [] and h.store.thread_status(closed.thread_id) == "pending"
            finished.set()
            await asyncio.wait_for(app._extract_queue.join(), 1)
            assert calls == [closed.thread_id]
        finally:
            await cancel(worker)
            await app.nightly.cancel_run()
    run(go())


def test_wait_includes_cancel_cleanup_even_after_running_becomes_none(h):
    async def go():
        entered, cleaning, cleaned = asyncio.Event(), asyncio.Event(), asyncio.Event()
        calls = []
        async def night(*_):
            entered.set()
            try:
                await asyncio.Future()
            finally:
                cleaning.set()
                await cleaned.wait()
        async def extract(closed):
            assert cleaned.is_set()
            calls.append(closed.thread_id)
            h.store.set_thread_status(closed.thread_id, h.T, "extracted")
        app, _ = setup(h, night, extract)
        await app.nightly.tick()
        await entered.wait()
        stopping = asyncio.create_task(app.nightly.cancel_run())
        await cleaning.wait()
        assert app.nightly.running is None
        app._extract_queue.put_nowait(pending(h, "guild-cleanup"))
        worker = asyncio.create_task(MindApp._extract_worker(app))
        try:
            await settle()
            assert calls == []
            cleaned.set()
            await stopping
            await asyncio.wait_for(app._extract_queue.join(), 1)
            assert len(calls) == 1
        finally:
            cleaned.set()
            await cancel(worker)
            await stopping
    run(go())


def test_cancelling_waiter_leaves_pending_database_thread_and_balances_queue(h):
    async def go():
        entered = asyncio.Event()
        calls = []
        async def night(*_):
            entered.set()
            await asyncio.Future()
        async def extract(closed):
            calls.append(closed.thread_id)
            h.store.set_thread_status(closed.thread_id, h.T, "extracted")
        app, ctx = setup(h, night, extract)
        await app.nightly.tick()
        await entered.wait()
        closed = pending(h, "guild-cancelled")
        app._extract_queue.put_nowait(closed)
        worker = asyncio.create_task(MindApp._extract_worker(app))
        await settle()
        await cancel(worker)
        await asyncio.wait_for(app._extract_queue.join(), 1)
        assert calls == [] and h.store.thread_status(closed.thread_id) == "pending"
        assert app.nightly.running is not None  # cancelling the waiter did not cancel its night
        await app.nightly.cancel_run()
        async def direct(tid):
            await app.extractor.extract(ClosedThread(tid, closed.key, (), "flush"))
        ctx.extract_thread = direct
        await stage_threads(ctx)
        assert calls == [closed.thread_id] and ctx.done("threads", str(closed.thread_id))
    run(go())


def test_night_joins_inflight_ordinary_thread_before_reflection(h):
    async def go():
        extracting, release, reflecting, finish = [asyncio.Event() for _ in range(4)]
        calls = []
        closed = pending(h, "guild-inflight")
        async def extract(item):
            calls.append(item.thread_id)
            extracting.set()
            await release.wait()
            h.store.set_thread_status(item.thread_id, h.T, "extracted")
        async def night(*_):
            await stage_threads(ctx)
            reflecting.set()
            await finish.wait()
        app, ctx = setup(h, night, extract)
        # The fake extraction respects terminal status; extract() still owns the real lock.
        admitted = app.extractor._extract
        async def once(item):
            if h.store.thread_status(item.thread_id) != "extracted":
                await admitted(item)
        app.extractor._extract = once
        async def direct(tid):
            await app.extractor.extract(ClosedThread(tid, closed.key, (), "flush"))
        ctx.extract_thread = direct
        app._extract_queue.put_nowait(closed)
        worker = asyncio.create_task(MindApp._extract_worker(app))
        try:
            await extracting.wait()
            await app.nightly.tick()
            await settle()
            assert not reflecting.is_set() and calls == [closed.thread_id]
            release.set()
            await asyncio.wait_for(reflecting.wait(), 1)
            assert calls == [closed.thread_id]
            assert ctx.done("threads", str(closed.thread_id))
            finish.set()
            await app.nightly.wait_idle()
        finally:
            release.set()
            finish.set()
            await cancel(worker)
            await app.nightly.cancel_run()
    run(go())


def test_ordinary_extractor_failure_keeps_existing_worker_recovery(h):
    async def go():
        async def night(*_):
            raise AssertionError("No night should start in this test")
        first, second = pending(h, "guild-fail"), pending(h, "guild-next")
        async def extract(closed):
            if closed.thread_id == first.thread_id:
                raise ValueError("synthetic extraction failure")
            h.store.set_thread_status(closed.thread_id, h.T, "extracted")
        app, _ = setup(h, night, extract)
        app._extract_queue.put_nowait(first)
        app._extract_queue.put_nowait(second)
        worker = asyncio.create_task(MindApp._extract_worker(app))
        try:
            await asyncio.wait_for(app._extract_queue.join(), 1)
            assert h.store.thread_status(first.thread_id) == "failed"
            assert h.store.thread_status(second.thread_id) == "extracted"
            assert not worker.done()
        finally:
            await cancel(worker)
    run(go())


def test_existing_extractor_run_remains_the_worker_entry_point(h):
    async def go():
        async def die(_queue):
            raise RuntimeError("synthetic dead extraction loop")
        app = SimpleNamespace(extractor=SimpleNamespace(run=die))
        with pytest.raises(RuntimeError, match="dead extraction loop"):
            await MindApp._extract_worker(app)
    run(go())


def test_ordinary_extraction_also_waits_for_voice_live_admission(h):
    async def go():
        entered, released = asyncio.Event(), asyncio.Event()
        calls = []
        async def night(*_):
            raise AssertionError("No night should start in this test")
        async def extract(closed):
            assert released.is_set()
            calls.append(closed.thread_id)
            h.store.set_thread_status(closed.thread_id, h.T, "extracted")
        async def admission():
            entered.set()
            await released.wait()
        app, _ = setup(h, night, extract)
        app.engine.live_admission = admission
        closed = pending(h, "guild-voice-handback")
        app._extract_queue.put_nowait(closed)
        worker = asyncio.create_task(MindApp._extract_worker(app))
        try:
            await entered.wait()
            assert calls == [] and app.nightly.running is None
            released.set()
            await asyncio.wait_for(app._extract_queue.join(), 1)
            assert calls == [closed.thread_id]
        finally:
            await cancel(worker)
    run(go())
