"""Content-free observations on fake clocks, workers and devices; no live audio/models."""
import asyncio
from dataclasses import FrozenInstanceError, fields
import json
import logging
import threading

import numpy as np
import pytest

from conftest import eventually, run
from leeroy_voice import logs
from leeroy_voice.timing import MAX_ACTIVE, MAX_AGE_S, TurnTiming, WorkReceipt, voice_token
from leeroy_voice.wire import Cancel
from test_service import Rig


class Timer:
    def __init__(self):
        self.now = 10.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def observe(rig, timer, events):
    loop_thread = threading.get_ident()

    def emit(event):
        assert threading.get_ident() == loop_thread
        events.append(event)

    rig.service._timer = timer
    rig.service._timing = TurnTiming(timer=timer, emit=emit)


def terminal(events, speak_id):
    return [event for event in events if event["voice_token"] == voice_token(speak_id)
            and event["phase"] == "terminal"]


def test_shared_voice_token_known_vector_and_utf8():
    assert voice_token("sample.speak-1") == (
        "3aa5c3fbc064aeebe869dfe6b377ed3f85fe1cbf48607db7962f9ccc0cd502cb")
    assert voice_token("") == (
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855")
    import hashlib
    assert voice_token("synthetic-\u00e9") == hashlib.sha256("synthetic-\u00e9".encode("utf-8")).hexdigest()


def test_local_durations_first_handoff_and_fixed_scalar_totals():
    timer, events = Timer(), []
    timing = TurnTiming(timer=timer, emit=events.append)
    handle = timing.begin("private id")
    timer.advance(1)
    timing.enqueued(handle, "normal")
    timer.now = 18
    receipt = WorkReceipt("normal", 13, 16, 16, 17, 17.5)
    with pytest.raises(FrozenInstanceError):
        receipt.source = "changed"
    timing.worked(handle, receipt, ready=True)
    timer.now = 20
    timing.handoff(handle)
    timer.now = 21
    timing.handoff(handle)  # a resume cannot move the first handoff
    timing.worked(handle, WorkReceipt("normal", 18, 19, 19, 20, 20.5), ready=True)
    timer.now = 22
    timing.finish(handle, "done")
    timing.finish(handle, "done")
    timing.worked(handle, receipt, ready=True)
    assert len(events) == 2 and timing.active_count == 0
    first, final = events
    assert first["event"] == "turn_timing" and first["schema"] == 1
    assert first["service"] == "voice" and first["phase"] == "first_handoff"
    assert first["receive_to_admission_ms"] == 1000
    assert first["enqueue_to_first_worker_ms"] == 2000
    assert first["first_wrapper_synthesis_ms"] == 3000
    assert first["first_native_processing_ms"] == 1000
    assert first["first_worker_post_to_loop_ms"] == 500
    assert first["receive_to_first_ready_ms"] == 8000
    assert first["first_ready_to_handoff_ms"] == 2000
    assert final["receive_to_handoff_ms"] == 10000
    assert final["receive_to_terminal_ms"] == 12000
    assert final["segment_count"] == 2
    assert final["wrapper_synthesis_total_ms"] == 4000
    assert final["native_processing_total_ms"] == 2000
    assert final["first_nonzero_output_ms"] is final["physical_audible_ms"] is None
    assert final["first_nonzero_output_scope"] == final["physical_audible_scope"] == "not_observed"


def test_capacity_expiry_close_and_late_receipts_never_reopen():
    timer, events = Timer(), []
    timing = TurnTiming(timer=timer, emit=events.append)
    handles = [timing.begin(str(index)) for index in range(MAX_ACTIVE + 4)]
    assert timing.active_count == MAX_ACTIVE and handles[-4:] == [None] * 4
    assert timing.suppressed == 4
    # Any number of segments changes scalar totals, not record shape.
    record = timing._active[handles[0]]
    shape = len(fields(record))
    receipt = WorkReceipt("normal", 10, 10, 10, 10, 10)
    for _ in range(1000):
        timing.worked(handles[0], receipt, ready=True)
    assert record.segment_count == 1000 and len(fields(record)) == shape
    assert all(isinstance(getattr(record, f.name), (str, float, int, bool, type(None)))
               for f in fields(record))
    timer.advance(MAX_AGE_S)
    timing.expire()
    assert timing.active_count == 0 and len(events) == MAX_ACTIVE
    assert {event["reason"] for event in events} == {"telemetry_expired"}
    timing.worked(handles[0], receipt, ready=True)
    timing.handoff(handles[0])
    timing.finish(handles[0], "done")
    assert len(events) == MAX_ACTIVE
    handle = timing.begin("new")
    timing.close()
    timing.close()
    timing.handoff(handle)
    assert events[-1]["reason"] == "shutdown" and timing.active_count == 0
    assert timing.begin("closed") is None


def test_missing_failed_stock_and_unknown_fields_stay_honest():
    timer, events = Timer(), []
    timing = TurnTiming(timer=timer, emit=events.append)
    handle = timing.begin("stock")
    timing.enqueued(handle, "stock")
    timer.advance(1)
    timing.worked(handle, WorkReceipt("stock", 10, None, 10, 10.5, 10.5), ready=False)
    timing.finish(handle, "dropped", "engine_error")
    event = events[-1]
    assert event["first_wrapper_synthesis_ms"] is event["wrapper_synthesis_total_ms"] is None
    assert event["receive_to_first_ready_ms"] is event["receive_to_handoff_ms"] is None
    assert event["failed_segment_count"] == 1
    assert event["deadline_scope"] == "no_decision_observed"
    handle = timing.begin("unknown")
    timing.finish(handle, "private outcome", "private exception")
    assert events[-1]["outcome"] == events[-1]["reason"] == "other"


def test_emitter_or_clock_failure_does_not_keep_finished_records():
    timer = Timer()

    def broken(event):
        raise RuntimeError("private exception")

    timing = TurnTiming(timer=timer, emit=broken)
    handle = timing.begin("safe")
    timing.handoff(handle)
    timing.finish(handle, "done")
    assert timing.active_count == 0 and timing.emit_failures == 2
    timer.now = float("nan")
    assert timing.begin("no clock") is None
    timer.now = 20
    handle = timing.begin("lost clock")
    timer.now = float("inf")
    timing.handoff(handle)
    timing.handoff(handle)
    timing.close()
    assert timing.active_count == 0 and timing.emit_failures == 4
    timer.now = 30
    assert TurnTiming(timer=timer).begin("\ud800") is None


def test_structured_console_and_json_events_never_include_private_payload(caplog):
    sentinels = ["SENTINEL speech", "SENTINEL raw", "SENTINEL persona",
                 "SENTINEL guid", "SENTINEL room", "SENTINEL/reference.wav"]
    with caplog.at_level(logging.INFO, logger="leeroy_voice"):
        timing = TurnTiming(timer=Timer())
        handle = timing.begin("|".join(sentinels))
        timing.enqueued(handle, sentinels[2])
        timing.finish(handle, sentinels[0], sentinels[-1])
    records = [r for r in caplog.records if getattr(r, "leeroy_event", "") == "turn_timing"]
    assert len(records) == 1
    record = records[0]
    outputs = [record.getMessage(), logs.ConsoleFormatter().format(record),
               logs.JsonFormatter().format(record), json.dumps(record.leeroy_fields)]
    for output in outputs:
        assert all(sentinel not in output for sentinel in sentinels)
    encoded = json.loads(logs.JsonFormatter().format(record))
    assert encoded["event"] == "turn_timing" and encoded["schema"] == 1
    assert "event_" not in encoded


def test_real_worker_queue_dispatch_and_unpumped_output(tmp_path, fake_bank):
    async def main():
        timer, events = Timer(), []
        rig = Rig(tmp_path, fake_bank, effects_enabled=False)
        observe(rig, timer, events)
        entered, release, posted = threading.Event(), threading.Event(), threading.Event()
        held = []
        native = np.concatenate((np.zeros(100, np.float32), np.full(4700, .1, np.float32)))
        async with rig:
            await rig.snapshot()
            await rig.stock_rendered()
            original_post = rig.service._post

            def dry(job, index):
                if job.speak.id == "blocking":
                    entered.set()
                    assert release.wait(5)
                timer.advance(2)
                return native

            def process(job, audio):
                assert audio is native
                timer.advance(1)
                return audio

            def post(fn, *args):
                if fn == rig.service._on_segment and args[0].speak.id == "queued":
                    held.append((fn, args))
                    posted.set()
                else:
                    original_post(fn, *args)

            rig.service._dry, rig.service._process, rig.service._post = dry, process, post
            try:
                rig.service._on_speak(rig.speak("blocking"))
                await eventually(entered.is_set)
                rig.service._on_speak(rig.speak("queued"))
                timer.advance(4)
                release.set()
                await eventually(posted.is_set)
                # Both receipts have been produced, but the second stays outside the loop.
                timer.advance(3)
                fn, args = held.pop()
                fn(*args)
                line = rig.service._jobs["queued"].line
                assert line.segments[0] is native  # telemetry never copies or scans PCM
                rig.service._plan(rig.service.scheduler.cancel, id="blocking")
                await rig.tick(.5)
                await rig.sees("queued", "started")
                first = next(e for e in events if e["voice_token"] == voice_token("queued"))
                assert first["enqueue_to_first_worker_ms"] == 7000
                assert first["first_wrapper_synthesis_ms"] == 2000
                assert first["first_native_processing_ms"] == 1000
                assert first["first_worker_post_to_loop_ms"] == 3000
                assert first["receive_to_handoff_ms"] == 13000
                # No output pump has run; started measures handoff only.
                assert first["first_nonzero_output_ms"] is first["physical_audible_ms"] is None
                assert rig.played().count(("queued", "started", "")) == 1
                assert np.max(np.abs(rig.out().pump(4800))) > 0
                await rig.play_out()
                await rig.sees("queued", "done")
                assert len(terminal(events, "queued")) == 1
            finally:
                release.set()
    run(main())


def test_cancelled_worker_late_receipt_and_duplicate_do_not_replace_trace(tmp_path, fake_bank):
    async def main():
        timer, events = Timer(), []
        rig = Rig(tmp_path, fake_bank)
        observe(rig, timer, events)
        entered, release, posted = threading.Event(), threading.Event(), threading.Event()
        async with rig:
            await rig.snapshot()
            await rig.stock_rendered()
            original_dry, original_post = rig.service._dry, rig.service._post

            def dry(job, index):
                entered.set()
                assert release.wait(5)
                return original_dry(job, index)

            def post(fn, *args):
                original_post(fn, *args)
                if fn == rig.service._on_segment:
                    posted.set()

            rig.service._dry, rig.service._post = dry, post
            try:
                msg = rig.speak("private duplicate id")
                rig.service._on_speak(msg)
                await eventually(entered.is_set)
                active_handle = rig.service._jobs[msg.id].timing
                rig.service._on_speak(msg)
                assert rig.service._jobs[msg.id].timing == active_handle
                assert terminal(events, msg.id)[0]["reason"] == "duplicate"
                timer.advance(2)
                rig.service._on_message(Cancel(id=msg.id))
                await rig.sees(msg.id, "dropped", "cancelled")
                before = len(events)
                release.set()
                await eventually(posted.is_set)
                await asyncio.sleep(.02)
                assert len(events) == before
                assert (msg.id, "started", "") not in rig.played()
                assert len(terminal(events, msg.id)) == 2  # two separate Speak receipts
                assert rig.service._timing.active_count == 0
            finally:
                release.set()
    run(main())


def test_stock_and_wall_expiry_keep_native_policy(tmp_path, fake_bank):
    async def main():
        timer, events = Timer(), []
        rig = Rig(tmp_path, fake_bank)
        observe(rig, timer, events)
        async with rig:
            await rig.snapshot()
            await rig.stock_rendered()
            rig.service._on_speak(rig.speak("stock", text="oom", kind="callout", priority="p1"))
            await rig.sees("stock", "started")
            first = events[-1]
            assert first["source"] == "stock"
            assert first["wrapper_synthesis_total_ms"] is None
            assert first["first_native_processing_ms"] == 0
            await rig.play_out()
            await rig.sees("stock", "done")
            # Existing wall policy expires a waiting line while the monotonic timer is fixed.
            rig.service._plan(rig.service.scheduler.set_ptt, True)
            rig.service._on_speak(rig.speak("expired", ttl=1))
            await eventually(lambda: rig.service._jobs["expired"].line.segments[0] is not None)
            await rig.tick(2)
            await rig.sees("expired", "dropped", "expired")
            final = terminal(events, "expired")[0]
            assert final["receive_to_terminal_ms"] == 0
            assert final["deadline_scope"] == "voice_before_start"
            assert final["receive_to_handoff_ms"] is None
    run(main())


def test_cpu_worker_and_fallback_receipts_use_existing_selected_path(tmp_path, fake_bank, monkeypatch):
    from leeroy_voice import service
    from leeroy_voice.engines import EngineError
    from test_cpu_fallback import Cpu, CpuRig
    from test_gpu import until

    monkeypatch.setattr(service, "available_ram_gib", lambda: 20.0)

    async def main():
        timer, events, cpu = Timer(), [], Cpu()
        cpu.fail = EngineError("private model failure")
        rig = CpuRig(tmp_path, fake_bank, cpu)
        observe(rig, timer, events)
        async with rig:
            await rig.seat(101)
            await until(rig, lambda: rig.service._cpu_active)
            rig.service._on_speak(rig.speak("cpu", text="We made it. Let us try again.", ttl=30))
            await rig.sees("cpu", "started")
            first = events[-1]
            assert first["source"] == "cpu" and first["segment_count"] == 1
            assert first["wrapper_synthesis_total_ms"] == 0
            assert rig.service._jobs["cpu"].fell_back
            assert rig.engine.live() == ["We made it. Let us try again."]
            await rig.play_out()
            await rig.sees("cpu", "done")
            assert len(terminal(events, "cpu")) == 1
            assert "private model failure" not in json.dumps(events)
    run(main(), 20)


def test_resume_plan_has_no_second_first_handoff(tmp_path, fake_bank):
    async def main():
        timer, events = Timer(), []
        rig = Rig(tmp_path, fake_bank)
        observe(rig, timer, events)
        held = []
        async with rig:
            await rig.snapshot()
            await rig.stock_rendered()
            original_post = rig.service._post

            def post(fn, *args):
                if fn == rig.service._on_segment and args[1] == 1:
                    held.append((fn, args))
                else:
                    original_post(fn, *args)

            rig.service._post = post
            rig.service._on_speak(rig.speak("two", text="First sentence. Second sentence."))
            await rig.sees("two", "started")
            await eventually(lambda: bool(held))
            for _ in range(20):
                rig.out().pump(4800)
                await rig.tick(.1)
                if rig.service.scheduler._playing["two"].stalled_at is not None:
                    break
            assert rig.service.scheduler._playing["two"].stalled_at is not None
            timer.advance(3)
            fn, args = held.pop()
            fn(*args)
            assert rig.played().count(("two", "started", "")) == 1
            assert len([e for e in events if e["phase"] == "first_handoff"]) == 1
            await rig.play_out()
            await rig.sees("two", "done")
            assert terminal(events, "two")[0]["receive_to_handoff_ms"] == 0
    run(main())


@pytest.mark.parametrize("second_segment", [False, True])
def test_failed_worker_keeps_partial_stage_and_existing_played_outcome(
        tmp_path, fake_bank, second_segment):
    async def main():
        timer, events = Timer(), []
        rig = Rig(tmp_path, fake_bank, effects_enabled=False)
        observe(rig, timer, events)
        entered, release = threading.Event(), threading.Event()
        async with rig:
            await rig.snapshot()
            await rig.stock_rendered()

            def dry(job, index):
                if index == int(second_segment):
                    entered.set()
                    assert release.wait(5)
                    timer.advance(2)
                    raise ValueError("SENTINEL failed speech and reference path")
                timer.advance(1)
                return np.full(4800, .1, np.float32)

            rig.service._dry = dry
            rig.service._process = lambda job, audio: audio
            try:
                text = "First sentence. Second sentence." if second_segment else "First sentence."
                rig.service._on_speak(rig.speak("failed", text=text))
                await eventually(entered.is_set)
                if second_segment:
                    await rig.sees("failed", "started")
                release.set()
                if second_segment:
                    for _ in range(20):
                        rig.out().pump(4800)
                        await rig.tick(.1)
                        if terminal(events, "failed"):
                            break
                state = "cut" if second_segment else "dropped"
                await rig.sees("failed", state, "engine_error")
                final = terminal(events, "failed")[0]
                assert final["failed_segment_count"] == 1
                assert final["segment_count"] == 1 + int(second_segment)
                assert final["wrapper_synthesis_total_ms"] == 2000 + 1000 * int(second_segment)
                assert final["deadline_scope"] == "no_decision_observed"
                assert "SENTINEL" not in json.dumps(events)
            finally:
                release.set()
    run(main())


@pytest.mark.parametrize("mode", ["capacity", "expiry", "emit_failure"])
def test_telemetry_limits_and_failures_do_not_change_playback(tmp_path, fake_bank, mode):
    async def main():
        timer, events = Timer(), []
        rig = Rig(tmp_path, fake_bank, effects_enabled=False)
        observe(rig, timer, events)
        async with rig:
            await rig.snapshot()
            if mode == "capacity":
                for index in range(MAX_ACTIVE):
                    rig.service._timing.begin(str(index))
            elif mode == "emit_failure":
                def broken(event):
                    raise RuntimeError("SENTINEL logger failed")
                rig.service._timing._emit = broken
            else:
                rig.service._plan(rig.service.scheduler.set_ptt, True)
            rig.service._on_speak(rig.speak("actual"))
            if mode == "expiry":
                await eventually(lambda: rig.service._jobs["actual"].line.segments[0] is not None)
                timer.advance(MAX_AGE_S)
                await rig.tick(.1)
                assert terminal(events, "actual")[0]["reason"] == "telemetry_expired"
                rig.service._plan(rig.service.scheduler.set_ptt, False)
                await rig.tick(2)
            await rig.sees("actual", "started")
            await rig.play_out()
            await rig.sees("actual", "done")
            assert rig.played() == [("actual", "started", ""), ("actual", "done", "")]
            if mode == "capacity":
                assert not terminal(events, "actual") and rig.service._timing.suppressed == 1
            elif mode == "expiry":
                assert len(terminal(events, "actual")) == 1
            else:
                assert rig.service._timing.emit_failures == 2
    run(main())
