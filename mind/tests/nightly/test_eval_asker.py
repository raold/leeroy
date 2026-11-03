"""Real prompt composition is scratch-only and owns no caller model or live resources."""
import asyncio
import json
import sqlite3
from dataclasses import replace

import pytest

from conftest import run
from nightly.conftest import IDENT
from leeroy_mind.config import load_config
from leeroy_mind.eval import asker as module
from leeroy_mind.llm.client import LLMError
from leeroy_mind.llm.presets import LIVE
from leeroy_mind.testing.fake_llm import FakeLLM


def configured(h):
    base = load_config(h.settings.data_dir / "absent.toml")
    return replace(base, models=replace(base.models, live="evaluation-live", embedding=h.EMB))


async def opened(h, llm=None, **kwargs):
    return await module.ScratchAsker.open(cfg=configured(h), data_dir=h.settings.data_dir,
        store=h.store, llm=llm or FakeLLM(script=lambda *_: "three i think"),
        ident=IDENT, clock=h.clock, label="eval-test", **kwargs)


def live_rows(h):
    return {name: [tuple(r) for r in h.store.conn.execute(f"SELECT * FROM {name}")]
            for name in ("chat_line", "utterance", "memory", "relationship", "persona_state", "meta", "bit_use")}


def closed(store):
    with pytest.raises(sqlite3.ProgrammingError):
        store.conn.execute("SELECT 1")


def test_scratch_asker_uses_real_question_recall_and_caller_identity_without_live_writes(h):
    memory = h.mem(101, "We wiped 3 times on Sindragosa, then killed it.")
    before = live_rows(h)
    llm = FakeLLM(script=lambda *_: "3 i think")

    async def go():
        asker = await opened(h, llm)
        assert asker.path.parent == h.settings.data_dir / "scratch"
        assert asker.ident == IDENT and asker.store is not h.store
        assert asker.engine.world.online >= {101, 102, 103, 104, 105}
        try:
            result = await asker.ask(101, "How many times did we wipe on Sindragosa on Tuesday night?")
            assert memory in result.memory_ids and result.text == "3 i think"
            assert asker.store.conn.execute("SELECT count(*) FROM chat_line").fetchone()[0] == len(before["chat_line"]) + 1
            assert asker.store.get_memories([memory])[0].access_n == 0
            with pytest.raises(RuntimeError, match="cannot send"):
                await asker.engine.send(object())
            return asker
        finally:
            await asker.aclose()

    asker = run(go())
    closed(asker.store)
    prompt = [c for c in llm.calls if "messages" in c][-1]
    text = prompt["messages"][-1]["content"]
    assert "Testy (whisper to Arkon): How many times did we wipe on Sindragosa on Tuesday night?" in text
    assert "Sindragosa" in text.split("You remember:")[1]
    assert prompt["model"] == "evaluation-live" and prompt["think"] is False
    assert json.loads(json.dumps(dict(LIVE.options))) == prompt["options"]
    assert live_rows(h) == before


def test_synchronous_idle_close_is_idempotent_and_does_not_close_caller_client(h):
    llm = FakeLLM()

    async def go():
        asker = await opened(h, llm)
        asker.close()
        closed(asker.store)
        asker.close()
        await asker.aclose()
        with pytest.raises(RuntimeError, match="closed"):
            await asker.ask(101, "Anything new?")
        assert (await llm.chat("still-open", [], options={}, think=False)).text == "(nothing)"

    run(go())


@pytest.mark.parametrize("guid,question", [(1, "hi"), (999, "hi"), (True, "hi"),
    (101, ""), (101, " " * 3), (101, "x" * (module.QUESTION_MAX_CHARS + 1))])
def test_invalid_question_or_member_is_rejected_before_storing(h, guid, question):
    async def go():
        asker = await opened(h)
        count = asker.store.conn.execute("SELECT count(*) FROM chat_line").fetchone()[0]
        try:
            with pytest.raises(ValueError):
                await asker.ask(guid, question)
            assert asker.store.conn.execute("SELECT count(*) FROM chat_line").fetchone()[0] == count
        finally:
            await asker.aclose()

    run(go())


def test_failed_generation_leaves_live_unchanged_and_asker_can_be_closed(h):
    before = live_rows(h)

    def fail(*_):
        raise LLMError("synthetic failure")

    async def go():
        asker = await opened(h, FakeLLM(script=fail))
        try:
            with pytest.raises(LLMError):
                await asker.ask(101, "What happened on the raid?")
        finally:
            await asker.aclose()
        closed(asker.store)

    run(go())
    assert live_rows(h) == before


@pytest.mark.parametrize("during", ["embedding", "generation"])
def test_cancel_and_close_drain_owned_work_without_touching_live(h, during):
    before = live_rows(h)

    async def go():
        entered, hold = asyncio.Event(), asyncio.Event()

        class Held(FakeLLM):
            async def embed(self, *args, **kwargs):
                if during == "embedding":
                    entered.set()
                    await hold.wait()
                return await super().embed(*args, **kwargs)

            async def chat(self, *args, **kwargs):
                if during == "generation":
                    entered.set()
                    await hold.wait()
                return await super().chat(*args, **kwargs)

        asker = await opened(h, Held())
        task = asyncio.create_task(asker.ask(101, "What happened on the raid?"))
        await asyncio.wait_for(entered.wait(), 2)
        asker.close()
        await asyncio.wait_for(asker.aclose(), 2)
        with pytest.raises(asyncio.CancelledError):
            await task
        closed(asker.store)
        assert not asker.scheduler._running and not asker.scheduler._heap

    run(go())
    assert live_rows(h) == before


def test_caller_cancellation_during_recall_is_cleaned_up_by_aclose(h):
    async def go():
        gate = asyncio.Event()
        llm = FakeLLM(gate=gate)
        asker = await opened(h, llm)
        task = asyncio.create_task(asker.ask(101, "What happened?"))
        while not llm.calls:
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(asker.aclose(), 2)
        assert llm.inflight == 0
        closed(asker.store)

    run(go())


def test_total_request_timeout_is_bounded_and_cleanup_completes(h, monkeypatch):
    monkeypatch.setattr(module, "ASK_TIMEOUT_S", .02)

    async def go():
        llm = FakeLLM(gate=asyncio.Event())
        asker = await opened(h, llm)
        try:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asker.ask(101, "What happened?"), 2)
        finally:
            await asker.aclose()
        assert llm.inflight == 0
        closed(asker.store)

    run(go())


def test_open_failure_closes_copy_and_preserves_live_database(h, monkeypatch):
    copies = []
    real_store = module.Store

    def track(*args, **kwargs):
        store = real_store(*args, **kwargs)
        copies.append(store)
        return store

    def fail_engine(**kwargs):
        raise RuntimeError("synthetic constructor failure")

    monkeypatch.setattr(module, "Store", track)
    monkeypatch.setattr(module, "TalkEngine", fail_engine)
    before = live_rows(h)
    with pytest.raises(RuntimeError, match="constructor"):
        run(opened(h))
    assert len(copies) == 1
    closed(copies[0])
    assert live_rows(h) == before


def test_concurrent_asks_are_serialized_and_queued_ask_is_cancelled_on_close(h):
    async def go():
        entered, gate = asyncio.Event(), asyncio.Event()

        class Held(FakeLLM):
            async def chat(self, *args, **kwargs):
                entered.set()
                await gate.wait()
                return await super().chat(*args, **kwargs)

        asker = await opened(h, Held())
        tasks = [asyncio.create_task(asker.ask(101, f"What happened at pull {i}?")) for i in range(2)]
        await asyncio.wait_for(entered.wait(), 2)
        assert asker.store.conn.execute("SELECT count(*) FROM chat_line").fetchone()[0] == 1
        await asyncio.wait_for(asker.aclose(), 2)
        for task in tasks:
            with pytest.raises(asyncio.CancelledError):
                await task
        closed(asker.store)

    run(go())
