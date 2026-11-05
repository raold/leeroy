"""Owner fences at the real pipeline/scheduler suspension boundaries."""

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from conftest import run
from engine_rig import engine_clock, make_rig
from leeroy_mind.llm.scheduler import LLMScheduler, Priority, _Entry
from leeroy_mind.protocol import Ack, Chat, Say
from leeroy_mind.talk.pipeline import TurnMeta, _ScopeChanged
from leeroy_mind.talk.turns import Turn, TurnKind
from leeroy_mind.testing.fake_llm import FakeLLM


@pytest.fixture
def clock():
    return engine_clock()


def turn(**kw):
    return Turn(kind=TurnKind.ASIDE, speaker=101, channel="guild", **kw)


async def until(predicate):
    for _ in range(100):
        if predicate():
            return
        await asyncio.sleep(0)
    assert predicate(), "expected asynchronous boundary was not reached"


def test_false_at_entry_never_starts_recall_or_model(store, clock, tmp_path, monkeypatch):
    r = make_rig(store, clock, tmp_path)
    recall = Mock(side_effect=AssertionError("invalid owner entered recall"))
    monkeypatch.setattr(r.engine.recall, "recall", recall)
    assert run(r.engine.speak(turn(), still_valid=lambda: False)) == []
    recall.assert_not_called()
    assert not r.llm.calls and not r.sent.log
    assert r.utterances()[-1]["dropped"] == "scope_changed"


def test_invalidated_during_real_recall_never_starts_chat(store, clock, tmp_path):
    r = make_rig(store, clock, tmp_path)
    line = Chat(int(clock.now() * 1000), "guild", 1, "Testmage", 0,
                "harbor repair tonight", 0, 10)
    line_id = store.insert_chat_line(line, session_id=None, origin="human")
    r.world.add_line(store.chat_lines([line_id])[0], clock.now())
    valid = [True]
    async def go():
        release = asyncio.Event()
        r.llm._gate = release
        task = asyncio.create_task(r.engine.speak(turn(), still_valid=lambda: valid[0]))
        await until(lambda: any("input" in call for call in r.llm.calls))
        assert r.llm.inflight == 1  # The actual Recall -> Embedder -> scheduler job.
        valid[0] = False
        release.set()
        assert await task == []
        assert not r.chat_calls() and not r.sent.log
        assert r.utterances()[-1]["dropped"] == "scope_changed"
        await r.engine.scheduler.close()
    run(go())


def test_chat_queued_before_invalidation_rechecks_before_job_admission(store, clock, tmp_path):
    r = make_rig(store, clock, tmp_path)
    valid = [True]
    async def go():
        scheduler = r.engine.scheduler
        scheduler._max = 1
        started, release = asyncio.Event(), asyncio.Event()
        async def blocker(llm):
            started.set()
            await release.wait()
        blocking = asyncio.create_task(scheduler.submit(Priority.REPLY_HUMAN, blocker))
        await started.wait()
        asking = asyncio.create_task(r.engine._ask(turn(deadline=clock.now() + 20), [],
            meta=TurnMeta("owner", still_valid=lambda: valid[0])))
        await until(lambda: scheduler.pending()[Priority.SOCIAL] > 0)
        valid[0] = False
        release.set()
        await blocking
        with pytest.raises(_ScopeChanged):
            await asking
        assert not r.chat_calls()
        await scheduler.close()
    run(go())


def test_chat_fence_rechecks_after_live_admission(store, clock, tmp_path):
    r = make_rig(store, clock, tmp_path)
    valid = [True]
    async def go():
        admitted, release = asyncio.Event(), asyncio.Event()
        async def admission():
            admitted.set()
            await release.wait()
        r.engine.live_admission = admission
        task = asyncio.create_task(r.engine._ask(turn(deadline=clock.now() + 20),
            [{"role": "user", "content": "hello"}],
            meta=TurnMeta("owner", still_valid=lambda: valid[0])))
        await admitted.wait()
        valid[0] = False
        release.set()
        with pytest.raises(_ScopeChanged, match="scope_changed"):
            await task
        assert not r.chat_calls()
        await r.engine.scheduler.close()
    run(go())


def test_scheduler_cancel_between_start_and_first_job_step_never_invokes_job(clock):
    async def go():
        scheduler = LLMScheduler(FakeLLM(), clock=clock, max_inflight=1, max_low_inflight=1)
        invoked = []
        async def job(llm):
            invoked.append(True)
        fut = asyncio.get_running_loop().create_future()
        entry = _Entry(Priority.BACKGROUND, 0, job, None, fut)
        scheduler._start(entry)
        # _pump has created a task, but its coroutine has not had its first step.
        fut.cancel()
        await entry.task
        await asyncio.sleep(0)
        assert invoked == [] and not scheduler._running
        assert scheduler._low_running == 0
        async def next_job(llm):
            return "next job completed"
        assert await scheduler.submit(Priority.BACKGROUND, next_job) == "next job completed"
        await asyncio.sleep(0)
        assert not scheduler._running and scheduler._low_running == 0
        await scheduler.close()
    run(go())


def test_scheduler_ordinary_submit_without_owner_still_runs(clock):
    async def go():
        scheduler = LLMScheduler(FakeLLM(), clock=clock)
        async def job(llm):
            return 17
        assert await scheduler.submit(Priority.SOCIAL, job) == 17
        await scheduler.close()
    run(go())


def test_shielded_delivery_rechecks_at_child_first_step(store, clock, tmp_path, monkeypatch):
    r = make_rig(store, clock, tmp_path)
    valid = [True]
    meta = TurnMeta("owner", still_valid=lambda: valid[0])
    async def go():
        original = asyncio.ensure_future
        def scheduled(awaitable):
            child = original(awaitable)
            # The parent already passed _blocked. Disable before the newly
            # shielded _deliver child takes its first step.
            valid[0] = False
            return child
        with monkeypatch.context() as patch:
            patch.setattr(asyncio, "ensure_future", scheduled)
            why = await r.engine._send_line(turn(), meta, r.engine.personas.get(101),
                                             "fair enough", [], first=True)
        assert why == "scope_changed" and not r.sent.log
    run(go())


def test_say_as_waiting_for_send_lock_drops_after_invalidation(store, clock, tmp_path):
    r = make_rig(store, clock, tmp_path)
    valid = [True]
    async def go():
        await r.engine._send_lock.acquire()
        task = asyncio.create_task(r.engine.say_as(101, "guild", "fair enough",
            kind=TurnKind.ASIDE, still_valid=lambda: valid[0]))
        await r.advance(10)
        assert not task.done() and not r.sent.log
        valid[0] = False
        r.engine._send_lock.release()
        assert await task is False
        assert not r.sent.log and not r.chat_calls()
        assert r.utterances()[-1]["dropped"] == "scope_changed"
    run(go())


def test_voice_waiting_for_send_lock_never_delivers_after_invalidation(store, clock, tmp_path):
    r = make_rig(store, clock, tmp_path)
    deliver = Mock(return_value=True)
    r.engine.comms = SimpleNamespace(deliver=deliver)
    valid = [True]
    async def go():
        await r.engine._send_lock.acquire()
        task = asyncio.create_task(r.engine._speak_voice(turn(),
            TurnMeta("owner", still_valid=lambda: valid[0]),
            r.engine.personas.get(101), "fair enough", []))
        await asyncio.sleep(0)
        valid[0] = False
        r.engine._send_lock.release()
        assert await task == []
        deliver.assert_not_called()
        assert not r.sent.log
    run(go())


def test_emotes_recheck_after_each_delay(store, clock, tmp_path):
    r = make_rig(store, clock, tmp_path)
    valid = [True]
    async def go():
        task = asyncio.create_task(r.engine._send_emotes(turn(),
            TurnMeta("owner", still_valid=lambda: valid[0]), [("wave", 0)]))
        await asyncio.sleep(0)
        valid[0] = False
        await r.advance(3)
        await task
        assert not r.sent.emotes()
    run(go())


def test_line_already_entered_transport_still_records_ack_after_disable_and_cancel(store, clock, tmp_path):
    r = make_rig(store, clock, tmp_path)
    valid = [True]
    meta = TurnMeta("owner", still_valid=lambda: valid[0])
    async def go():
        written, release = asyncio.Event(), asyncio.Event()
        commands = []
        async def send(cmd):
            commands.append(cmd)
            written.set()
            await release.wait()
            return Ack(0, cmd.id, True, "")
        r.engine.send = send
        task = asyncio.create_task(r.engine._send_line(turn(), meta, r.engine.personas.get(101),
                                                     "fair enough", [], first=True))
        await written.wait()
        valid[0] = False
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(commands) == 1 and isinstance(commands[0], Say)
        assert meta.settled and meta.said == 1
        assert store.conn.execute("SELECT count(*) FROM chat_line WHERE origin='mind'").fetchone()[0] == 1
        assert r.utterances()[-1]["dropped"] is None
    run(go())


def test_invalidated_generation_does_not_retry_model_or_become_valid_on_reenable(store, clock, tmp_path):
    generation = [0]
    def answer(model, messages):
        generation[0] = 1
        return "(nothing)"
    r = make_rig(store, clock, tmp_path, script=answer)
    async def go():
        ticket = generation[0]
        task = asyncio.create_task(r.engine.speak(
            Turn(kind=TurnKind.REPLY_HUMAN, speaker=101, channel="whisper", to_guid=1, addressee=1),
            still_valid=lambda: generation[0] == ticket))
        await r.advance(10)
        assert await task == []
        assert len(r.chat_calls()) == 1 and not r.sent.log
        generation[0] = 2  # A new enabled generation cannot revive the old ticket.
        assert r.utterances()[-1]["dropped"] == "scope_changed"
        await r.engine.scheduler.close()
    run(go())


def test_omitted_predicate_preserves_model_and_template_defaults(store, clock, tmp_path):
    r = make_rig(store, clock, tmp_path)
    async def go():
        speaking = asyncio.create_task(r.engine.speak(turn()))
        await r.advance(40)
        assert await speaking
        saying = asyncio.create_task(r.engine.say_as(102, "guild", "sure thing", kind=TurnKind.ASIDE))
        await r.advance(40)
        assert await saying is True
        assert len(r.chat_calls()) == 1 and len(r.sent.says()) == 2
        await r.engine.scheduler.close()
    run(go())
