"""Synthetic clocks, real scheduler waits, and existing pipeline cancellation/ACK seams."""
import asyncio
from dataclasses import fields
import json
import logging
from types import SimpleNamespace

import pytest

from conftest import run
from engine_rig import engine_clock, make_rig
from leeroy_mind.bridge import AckTimeout
from leeroy_mind.llm.scheduler import Expired, LLMScheduler, Priority
from leeroy_mind.logs import ConsoleFormatter, JsonFormatter
from leeroy_mind.protocol import Ack, Chat
from leeroy_mind.talk.pipeline import Pipeline, TurnMeta
from leeroy_mind.talk.timing import MAX_ACTIVE, MAX_AGE_S, TurnTiming, voice_token
from leeroy_mind.talk.turns import Turn, TurnKind


class Timer:
    value = 100.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


async def settle():
    for _ in range(24):
        await asyncio.sleep(0)


def observed(engine):
    timer, events = Timer(), []
    engine.timing = TurnTiming(timer=timer, emit=events.append)
    return timer, events


def terminals(events):
    return [e for e in events if e["phase"] == "terminal"]


def turn(**kw):
    return Turn(kind=TurnKind.CALLOUT, speaker=101, channel="guild", **kw)


def test_shared_wire_token_vector():
    assert voice_token("sample.speak-1") == (
        "3aa5c3fbc064aeebe869dfe6b377ed3f85fe1cbf48607db7962f9ccc0cd502cb")


def test_bounds_ttl_late_events_and_close():
    timer, events = Timer(), []
    timing = TurnTiming(timer=timer, emit=events.append)
    tokens = [timing.begin() for _ in range(MAX_ACTIVE)]
    assert timing.begin() == "" and timing.suppressed == 1
    assert len(timing._active) == MAX_ACTIVE
    timing.voice_sent(tokens[0], "sample.speak-1")
    timer.advance(MAX_AGE_S)
    timing.expire()
    assert not timing._active and len(events) == MAX_ACTIVE
    assert all(e["outcome"] == "incomplete" and e["reason"] == "ttl" for e in events)
    timing.played("sample.speak-1", "started")
    timing.pipeline_done(tokens[1])
    timing.model(tokens[2], 0, "call")
    assert len(events) == MAX_ACTIVE and not timing._active
    token = timing.begin()
    assert token not in tokens
    timing.close()
    assert events[-1]["reason"] == "shutdown" and timing.begin() == ""


def test_fixed_records_and_no_private_strings_in_any_timing_log(caplog):
    timer = Timer()
    timing = TurnTiming(timer=timer)
    private = "SECRET speech name GUID-987 room-persona C:/private/reference.wav"
    token = timing.begin()
    timing.voice_sent(token, private)
    record = timing._active[token]
    # The record and two fixed attempt slots contain scalars only, never arbitrary containers.
    for f in fields(record):
        value = getattr(record, f.name)
        if f.name not in {"attempt1", "attempt2"}:
            assert value is None or isinstance(value, (str, float, int, bool))
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        timing.played(private, "started")
        timing.played(private, "started")
        timing.played(private, "cut", private)
        timing.played(private, "done")
    records = [r for r in caplog.records if getattr(r, "leeroy_event", None) == "turn_timing"]
    assert len(records) == 2
    for r in records:
        structured = json.loads(JsonFormatter().format(r))
        assert structured["schema"] == 1 and structured["service"] == "mind"
        assert structured["voice_token"] == voice_token(private)
        assert structured["first_nonzero_output_ms"] is None
        assert structured["physical_audible_ms"] is None
        assert private not in JsonFormatter().format(r) + ConsoleFormatter().format(r)
    assert records[-1].leeroy_fields["reason"] == "other"


def test_sink_and_timer_failure_never_reopen_or_raise():
    timer = Timer()

    def broken(_):
        raise RuntimeError("private sink failure")

    timing = TurnTiming(timer=timer, emit=broken)
    token = timing.begin()
    timing.typed_ack(token)
    timing.finish(token, "completed")
    timing.finish(token, "completed")
    assert not timing._active
    timing = TurnTiming(timer=lambda: float("nan"), emit=broken)
    assert timing.begin() == ""
    timing.close()


def test_duplicate_active_voice_id_is_ambiguous():
    events = []
    timing = TurnTiming(emit=events.append)
    a, b = timing.begin(), timing.begin()
    timing.voice_sent(a, "duplicate")
    timing.voice_sent(b, "duplicate")
    timing.played("duplicate", "started")
    assert not timing._active
    assert len(events) == 2 and all(e["reason"] == "duplicate_id" for e in events)


def test_scheduler_queue_readiness_and_client_call_use_independent_monotonic_clock():
    async def go():
        wall, timer, events = engine_clock(), Timer(), []
        busy, ready, calling = asyncio.Event(), asyncio.Event(), asyncio.Event()
        busy_entered, ready_entered, call_entered = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def chat(*args, **kwargs):
            call_entered.set()
            await calling.wait()
            return SimpleNamespace(text="synthetic answer")

        async def admission():
            ready_entered.set()
            await ready.wait()

        async def occupying(_):
            busy_entered.set()
            await busy.wait()

        engine = Pipeline()
        engine.clock = wall
        engine.cfg = SimpleNamespace(models=SimpleNamespace(live="synthetic"))
        engine._init_pipeline(wall.now())
        engine.timing = TurnTiming(timer=timer, emit=events.append)
        engine.live_admission = admission
        engine.scheduler = LLMScheduler(SimpleNamespace(chat=chat), clock=wall, max_inflight=1)
        blocker = asyncio.create_task(engine.scheduler.submit(Priority.SOCIAL, occupying))
        await busy_entered.wait()
        trace = engine.timing.begin()
        task = asyncio.create_task(engine._ask(turn(deadline=wall.now() + 1000), [], trace=trace))
        await settle()
        assert not ready_entered.is_set()
        timer.advance(2)
        await wall.advance(70)  # policy clock moves independently of measured duration
        wall._t -= 100  # a backward wall correction must not subtract from local durations
        busy.set()
        await ready_entered.wait()
        timer.advance(3)
        ready.set()
        await call_entered.wait()
        timer.advance(5)
        calling.set()
        assert await task == "synthetic answer"
        await blocker
        engine.timing.pipeline_done(trace)
        await engine.scheduler.close()
        event = terminals(events)[0]
        assert event["model_queue_ms"] == 2000
        assert event["readiness_admission_ms"] == 3000
        assert event["llm_client_call_ms"] == 5000
        assert event["model_attempts"] == 1
        assert event["elapsed_ms"] == 10000
        assert event["deadline_outcome"] == "within_deadline"
    run(go())


@pytest.mark.parametrize("phase", ["queue", "admission", "call"])
def test_cancelled_model_wait_keeps_partial_stage(phase):
    async def go():
        wall, timer, events = engine_clock(), Timer(), []
        gate, entered = asyncio.Event(), asyncio.Event()

        async def waiting(*args, **kwargs):
            entered.set()
            await gate.wait()
            return SimpleNamespace(text="synthetic")

        engine = Pipeline()
        engine.clock = wall
        engine.cfg = SimpleNamespace(models=SimpleNamespace(live="synthetic"))
        engine._init_pipeline(wall.now())
        engine.timing = TurnTiming(timer=timer, emit=events.append)
        engine.scheduler = LLMScheduler(SimpleNamespace(chat=waiting), clock=wall, max_inflight=1)
        blocker = None
        if phase == "queue":
            blocker = asyncio.create_task(engine.scheduler.submit(Priority.SOCIAL, waiting))
            await entered.wait()
        elif phase == "admission":
            engine.live_admission = waiting
        trace = engine.timing.begin()
        task = asyncio.create_task(engine._ask(turn(deadline=wall.now() + 100), [], trace=trace))
        await settle()
        timer.advance(4)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        engine.timing.pipeline_done(trace, "cancelled")
        gate.set()
        if blocker is not None:
            await blocker
        await engine.scheduler.close()
        event = terminals(events)[0]
        key = {"queue": "model_queue_ms", "admission": "readiness_admission_ms",
               "call": "llm_client_call_ms"}[phase]
        assert event[key] == 4000
        assert event["model_attempt1_outcome"] == "cancelled"
        if phase != "call":
            assert event["llm_client_call_ms"] is None
        assert len(events) == 1
    run(go())


def test_expiry_before_scheduler_closure_records_queue_without_inventing_call():
    async def go():
        wall, timer, events = engine_clock(), Timer(), []
        engine = Pipeline()
        engine.clock = wall
        engine.cfg = SimpleNamespace(models=SimpleNamespace(live="synthetic"))
        engine._init_pipeline(wall.now())
        engine.timing = TurnTiming(timer=timer, emit=events.append)
        engine.scheduler = LLMScheduler(object(), clock=wall)
        trace = engine.timing.begin()
        with pytest.raises(Expired):
            await engine._ask(turn(deadline=wall.now() - 1), [], trace=trace)
        engine.timing.pipeline_done(trace)
        event = terminals(events)[0]
        assert event["deadline_decision"] == "mind_model_queue"
        assert event["deadline_outcome"] == "expired"
        assert event["model_queue_ms"] == 0
        assert event["readiness_admission_ms"] is None
        assert event["llm_client_call_ms"] is None
        await engine.scheduler.close()
    run(go())


def test_retry_preserves_two_client_calls_and_old_llm_seconds(store, tmp_path):
    async def go():
        rig = make_rig(store, engine_clock(), tmp_path, reply="unused")
        timer, events = observed(rig.engine)
        meta = TurnMeta("synthetic")
        rig.engine._admit_timing(meta)
        t = Turn(kind=TurnKind.REPLY_HUMAN, speaker=101, channel="whisper", to_guid=1,
                 addressee=1, trigger_line=123, deadline=rig.clock.now() + 100)
        composed = await rig.engine._compose(t, meta, live=True)
        calls = []

        async def chat(*args, **kwargs):
            calls.append(1)
            timer.advance(2)
            await rig.clock.advance(1)
            return SimpleNamespace(text="(nothing)")

        rig.llm.chat = chat
        assert await rig.engine._generate(t, meta, composed) == ("(nothing)", "silent")
        rig.engine.timing.pipeline_done(meta.timing_trace)
        event = terminals(events)[0]
        assert len(calls) == event["model_attempts"] == 2
        assert event["llm_client_call_ms"] == 4000
        assert event["readiness_admission_ms"] is None
        assert event["model_attempt1_outcome"] == event["model_attempt2_outcome"] == "completed"
        assert meta.llm_s == 2.0
        await rig.engine.scheduler.close()
    run(go())


@pytest.mark.parametrize("failure", ["readiness_expired", "call_failed", "answer_expired"])
def test_failed_generation_and_readiness_keep_observed_boundaries(store, tmp_path, failure):
    async def go():
        rig = make_rig(store, engine_clock(), tmp_path)
        timer, events = observed(rig.engine)
        meta = TurnMeta("synthetic")
        rig.engine._admit_timing(meta)
        t = turn(deadline=rig.clock.now() + 10)
        composed = await rig.engine._compose(t, meta, live=True)

        async def admission():
            timer.advance(2)
            await rig.clock.advance(11)

        async def chat(*args, **kwargs):
            timer.advance(3)
            if failure == "call_failed":
                raise RuntimeError("private model failure sentinel")
            await rig.clock.advance(11)
            return SimpleNamespace(text="synthetic answer")

        rig.llm.chat = chat
        if failure == "readiness_expired":
            rig.engine.live_admission = admission
        if failure == "call_failed":
            with pytest.raises(RuntimeError):
                await rig.engine._generate(t, meta, composed)
            rig.engine.timing.pipeline_done(meta.timing_trace, "error")
        else:
            _, reason = await rig.engine._generate(t, meta, composed)
            assert reason == "expired"
            rig.engine.timing.decision(meta.timing_trace, reason)
            rig.engine.timing.pipeline_done(meta.timing_trace)
        event = terminals(events)[0]
        assert "private model failure sentinel" not in json.dumps(events)
        if failure == "readiness_expired":
            assert event["deadline_decision"] == "mind_readiness_admission"
            assert event["readiness_admission_ms"] == 2000
            assert event["llm_client_call_ms"] is None
        else:
            assert event["llm_client_call_ms"] == 3000
            if failure == "answer_expired":
                assert event["deadline_decision"] == "mind_after_generation"
            else:
                assert event["model_attempt1_outcome"] == "error"
        await rig.engine.scheduler.close()
    run(go())


def test_human_entry_origin_includes_synchronous_planning(store, tmp_path):
    async def go():
        rig = make_rig(store, engine_clock(), tmp_path)
        timer, events = observed(rig.engine)
        weigh = rig.engine._weigh

        def slow_plan(*args, **kwargs):
            timer.advance(2)
            return weigh(*args, **kwargs)

        rig.engine._weigh = slow_plan
        await rig.human("Arkon, are you there?", channel="whisper", to_guid=101)
        await rig.engine._cancel_all()
        await settle()
        assert events
        assert all(e["origin_scope"] == "mind_human_chat_entry" for e in events)
        assert all(e["planning_ms"] == 2000 for e in events)
        await rig.engine.scheduler.close()
    run(go())


@pytest.mark.parametrize("before_start", [True, False])
def test_queued_turn_cancellation_closes_unstarted_records_without_gameplay_rows(
        store, tmp_path, before_start):
    async def go():
        rig = make_rig(store, engine_clock(), tmp_path)
        timer, events = observed(rig.engine)
        items = [(turn(template="synthetic", delay=50), TurnMeta("first")),
                 (turn(template="synthetic second"), TurnMeta("second"))]
        rig.engine._queue(items)
        if not before_start:
            await settle()
        timer.advance(4)
        await rig.engine._cancel_all()
        await settle()
        assert not rig.engine.timing._active
        assert len(terminals(events)) == 2
        assert all(e["outcome"] == "cancelled" for e in terminals(events))
        assert len(rig.utterances()) == (0 if before_start else 1)
        queued = terminals(events)[-1]
        assert queued["turn_queue_ms"] == 4000  # observed wait, cancelled before dequeue
        assert queued["turn_queue_outcome"] == "cancelled"
        await rig.engine.scheduler.close()
    run(go())


def test_real_pipeline_delay_compose_typing_lock_and_bridge_ack(store, tmp_path):
    async def go():
        rig = make_rig(store, engine_clock(), tmp_path)
        timer, events = observed(rig.engine)
        composing, compose_gate = asyncio.Event(), asyncio.Event()
        sending, send_gate = asyncio.Event(), asyncio.Event()
        original = rig.engine._compose

        async def compose(*args, **kwargs):
            composing.set()
            await compose_gate.wait()
            return await original(*args, **kwargs)

        async def send(cmd):
            sending.set()
            await send_gate.wait()
            return Ack(ts=0, id=cmd.id, ok=True, error="")

        rig.engine._compose, rig.engine.send = compose, send
        meta = TurnMeta("synthetic", timing_origin=timer.value - 1)
        rig.engine._queue([(turn(delay=2), meta)])
        await settle()
        timer.advance(2)
        await rig.advance(2)
        await composing.wait()
        timer.advance(3)
        await rig.engine._send_lock.acquire()
        compose_gate.set()
        await settle()
        timer.advance(4)
        await rig.advance(20)
        timer.advance(5)
        rig.engine._send_lock.release()
        await sending.wait()
        timer.advance(6)
        send_gate.set()
        await settle()
        event = terminals(events)[0]
        assert event["origin_scope"] == "mind_human_chat_entry"
        assert event["planning_ms"] == 1000
        assert event["delay_ms"] == 2000
        assert event["compose_ms"] == 3000
        assert event["typing_ms"] == 4000
        assert event["send_lock_ms"] == 5000
        assert event["bridge_ack_ms"] == 6000
        assert event["origin_to_typed_ack_ms"] == 21000
        assert len(events) == 2 and event["outcome"] == "completed"
        await rig.engine.scheduler.close()
    run(go())


@pytest.mark.parametrize("echo", [True, False])
def test_ack_timeout_waits_for_existing_echo_settlement(store, tmp_path, echo):
    async def go():
        rig = make_rig(store, engine_clock(), tmp_path)
        timer, events = observed(rig.engine)

        async def send(cmd):
            raise AckTimeout("private exception contents")

        rig.engine.send = send
        task = asyncio.create_task(rig.engine.speak(turn(template="synthetic line")))
        await rig.advance(20)
        assert await task == []
        assert not events and len(rig.engine.timing._active) == 1
        assert not rig.utterances()
        timer.advance(7)
        if echo:
            (key,) = rig.engine._unacked
            ev = Chat(ts=0, channel=key[1], from_guid=key[0], from_name="synthetic",
                      to_guid=0, text=key[2], map=0, zone=10, via_bridge=True)
            assert rig.engine.is_echo(ev, rig.clock.now())
        else:
            rig.engine._settle_unacked(rig.clock.now() + 31)
        event = terminals(events)[0]
        assert event["outcome"] == ("echo_confirmed" if echo else "incomplete")
        assert event["origin_to_typed_ack_ms"] is None
        assert event["first_ack_scope"] == ("typed_echo" if echo else "not_observed")
        assert len(rig.utterances()) == 1 and not rig.engine.timing._active
        await rig.engine.scheduler.close()
    run(go())


def test_cancel_during_shielded_ack_waits_and_records_once(store, tmp_path):
    async def go():
        rig = make_rig(store, engine_clock(), tmp_path)
        timer, events = observed(rig.engine)
        entered, gate = asyncio.Event(), asyncio.Event()

        async def send(cmd):
            entered.set()
            await gate.wait()
            return Ack(ts=0, id=cmd.id, ok=True, error="")

        rig.engine.send = send
        task = asyncio.create_task(rig.engine.speak(turn(template="synthetic line")))
        await rig.advance(20)
        await entered.wait()
        task.cancel()
        await settle()
        assert not events and not task.done()
        timer.advance(3)
        gate.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(events) == 2 and len(rig.utterances()) == 1
        assert terminals(events)[0]["outcome"] == "cancelled"
        assert terminals(events)[0]["bridge_ack_ms"] == 3000
        assert terminals(events)[0]["typed_acks"] == 1
        await rig.engine.scheduler.close()
    run(go())
