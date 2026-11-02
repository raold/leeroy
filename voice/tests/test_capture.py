"""His side of push-to-talk (Task 8): the 300 ms ring, the pre-roll and the tail, mono at 16 kHz,
a press discarded, nothing kept outside a press (draft section 6.2, the privacy rule); and the
Mic on FakeDeviceApi's invented devices (PR15): the named device and never another, reopened
after a refresh closed it (review-task7 M1), retried while missing. Nothing here opens a device.

The test audio is a ramp: each sample is 0.1 x its stream time in seconds, so a clip shows what
span it holds."""
import logging
import threading
import time
from collections import deque

import numpy as np
import pytest

from fakes import FakeDeviceApi
from leeroy_voice.audio_io import EARLY_DEATH_S, REFRESH_S, RETRY_S, AudioOut, Dev
from leeroy_voice.capture import STT_RATE, Capture, Mic
from leeroy_voice.mixer import Mixer

RATE = 48000
EDGE = 64  # 16 kHz samples at each end that carry the resampler's edge (measured: under 50)


def ramp(first: int, frames: int, *, rate: int = RATE, channels: int = 1) -> np.ndarray:
    """Frames `first` on of the stream, as (frames, channels)."""
    values = (0.1 * np.arange(first, first + frames) / rate).astype(np.float32)
    return np.repeat(values[:, None], channels, axis=1)


class Stream:
    """Plays the input stream into a capture in 10 ms blocks; its time is the clock's."""

    def __init__(self, sink, *, rate: int = RATE, channels: int = 1):
        self.sink, self.rate, self.channels = sink, rate, channels
        self.frames = 0

    def until(self, t: float) -> None:
        end = round(t * self.rate)
        while self.frames < end:
            n = min(self.rate // 100, end - self.frames)
            self.sink(ramp(self.frames, n, rate=self.rate, channels=self.channels))
            self.frames += n


def spans(clip: np.ndarray, start_s: float, end_s: float, *, scale: float = 0.1) -> bool:
    """The clip is the ramp from start_s to end_s, at 16 kHz."""
    if len(clip) != round((end_s - start_s) * STT_RATE):
        return False
    k = np.arange(len(clip))
    expected = scale * (start_s + k / STT_RATE)
    return bool(np.abs(clip - expected)[EDGE:-EDGE].max() < 1e-4)


def held_frames(capture) -> int:
    """Every frame of audio the capture keeps, wherever it keeps it."""
    seen: set[int] = set()

    def walk(v) -> int:
        if id(v) in seen:
            return 0
        seen.add(id(v))
        if isinstance(v, np.ndarray):
            return v.shape[0] if v.ndim else 0
        if isinstance(v, (list, tuple, deque, set)):
            return sum(walk(x) for x in v)
        if isinstance(v, dict):
            return sum(walk(x) for x in v.values())
        attrs = getattr(v, "__dict__", None)
        if attrs is None and hasattr(type(v), "__slots__"):
            attrs = {s: getattr(v, s, None) for s in type(v).__slots__}
        if attrs is not None and not isinstance(v, type):
            return sum(walk(x) for x in attrs.values())
        return 0
    return walk(capture)


def test_preroll_and_tail():
    """Key down at t, the Press at t + 0.15 (the gate), begin there; key up at t + 1.0: the clip
    runs from t - 0.15 (the whole 300 ms ring) to t + 1.2 (the 200 ms tail), at 16 kHz."""
    c = Capture(RATE, 1)
    s = Stream(c.feed)
    t = 1.0
    s.until(t + 0.15)
    c.begin(t + 0.15)
    s.until(t + 1.0)
    c.end(t + 1.0)
    assert c.take() is None  # the tail is still coming
    s.until(t + 1.19)
    assert c.take() is None
    s.until(t + 1.5)
    clip = c.take()
    assert clip.dtype == np.float32 and clip.ndim == 1 and len(clip) == 21600
    assert spans(clip, t - 0.15, t + 1.2)
    assert c.take() is None  # once


def test_nothing_outside_a_press():
    """The privacy rule: audio outside a press is overwritten within 300 ms and never handed
    on. 2 s with no press: take() has nothing, the capture holds 300 ms of audio at most, and
    a press then reaches back no further than that."""
    c = Capture(RATE, 1)
    s = Stream(c.feed)
    s.until(2.0)
    assert c.take() is None
    assert held_frames(c) <= 0.3 * RATE
    c.begin(2.0)
    c.end(2.0)
    s.until(2.3)
    assert spans(c.take(), 1.7, 2.2)


def test_stereo_downmixed():
    """Both channels averaged: the left at the ramp, the right silent, gives half the ramp."""
    c = Capture(RATE, 2)

    def feed(block):
        block[:, 1] = 0.0
        c.feed(block)
    s = Stream(feed, channels=2)
    s.until(1.0)
    c.begin(1.0)
    s.until(2.0)
    c.end(2.0)
    s.until(2.5)
    assert spans(c.take(), 0.7, 2.2, scale=0.05)


def test_session_off_discards_a_press():
    """Review Focus 2: logout mid-press. discard() drops the press, and a clip still waiting
    for its tail too."""
    c = Capture(RATE, 1)
    s = Stream(c.feed)
    s.until(1.0)
    c.begin(1.0)
    s.until(2.0)
    c.discard()
    c.end(2.0)
    s.until(2.5)
    assert c.take() is None
    c.begin(2.5)
    s.until(3.0)
    c.end(3.0)
    c.discard()
    s.until(3.5)
    assert c.take() is None
    c.begin(3.5)  # the next press is whole
    s.until(4.0)
    c.end(4.0)
    s.until(4.5)
    assert spans(c.take(), 3.2, 4.2)
    c.begin(4.5)
    s.until(5.0)
    c.end(5.0)
    s.until(5.5)
    c.discard()  # a whole clip not yet taken when he went offline: gone too
    assert c.take() is None


def test_a_quick_second_press_keeps_the_first():
    """Pressed again before the first press's tail was in: the first clip ends where the
    audio got to, and both are taken, in order."""
    c = Capture(RATE, 1)
    s = Stream(c.feed)
    s.until(1.0)
    c.begin(1.0)
    s.until(2.0)
    c.end(2.0)
    s.until(2.1)
    c.begin(2.1)
    s.until(3.0)
    c.end(3.0)
    s.until(3.5)
    assert spans(c.take(), 0.7, 2.1)
    assert spans(c.take(), 1.8, 3.2)
    assert c.take() is None


def test_a_press_with_no_end_stops_at_max_s():
    """max_s bounds what a press records after begin(); its tail follows, so a capped hold
    (PttMachine's max_hold_s from key-down) is never cut short."""
    c = Capture(RATE, 1, max_s=2.0)
    s = Stream(c.feed)
    s.until(1.0)
    c.begin(1.0)
    s.until(3.1)
    assert c.take() is None  # its tail is still coming
    s.until(4.0)
    assert spans(c.take(), 0.7, 3.2)
    c.end(4.0)  # too late: nothing changes
    s.until(4.5)
    assert c.take() is None


def test_a_late_end_still_ends_the_tail_after_the_key_up():
    """end(now) dates the key-up: called 50 ms late (the watchdog's Release is dated at its
    first up read), the clip still ends 200 ms after that time, not after the call."""
    c = Capture(RATE, 1)
    s = Stream(c.feed)
    s.until(1.0)
    c.begin(1.0)
    s.until(2.05)
    c.end(2.0)
    s.until(2.5)
    assert spans(c.take(), 0.7, 2.2)
    c.begin(2.5)  # and 600 ms late: cut back to 200 ms after the key-up
    s.until(3.6)
    c.end(3.0)
    assert spans(c.take(), 2.2, 3.2)
    c.begin(3.6)  # the cut falling inside a block
    s.until(4.5)
    c.end(3.9975)
    assert spans(c.take(), 3.3, 4.1975)


def test_no_preroll():
    c = Capture(RATE, 1, preroll_ms=0)
    s = Stream(c.feed)
    s.until(1.0)
    c.begin(1.0)
    s.until(2.0)
    c.end(2.0)
    s.until(2.5)
    assert spans(c.take(), 1.0, 2.2)


def test_the_clip_ends_on_its_frame():
    """An end that falls inside a block cuts the block there."""
    c = Capture(RATE, 1)
    s = Stream(c.feed)
    s.until(1.0)
    c.begin(1.0)
    s.until(2.0)
    c.end(1.995)
    s.until(2.5)
    assert spans(c.take(), 0.7, 2.195)


def test_blocks_that_straddle_the_ring():
    """Blocks of any size, wrapping round the ring's end or longer than the whole ring."""
    c = Capture(RATE, 1)
    frames = 0
    for n in (700, 13000, 1100, 333, 20000, 9999, 4447):
        c.feed(ramp(frames, n))
        frames += n
    t = frames / RATE
    c.begin(t)
    c.end(t)
    c.feed(ramp(frames, 9600))
    assert spans(c.take(), t - 0.3, t + 0.2)


def test_an_end_never_reaches_past_the_audio_fed():
    """end(now) with a `now` ahead of the stream (a stall in the audio thread): the clip ends
    200 ms of audio after where the stream had got to, never on audio from after the key-up."""
    c = Capture(RATE, 1)
    s = Stream(c.feed)
    s.until(1.0)
    c.begin(1.0)
    s.until(1.4)
    c.end(2.0)
    s.until(3.0)
    assert spans(c.take(), 0.7, 1.6)


def test_the_ring_reaches_back_only_as_far_as_the_stream():
    c = Capture(RATE, 1)
    s = Stream(c.feed)
    s.until(0.1)
    c.begin(0.1)
    s.until(0.5)
    c.end(0.5)
    s.until(1.0)
    assert spans(c.take(), 0.0, 0.7)


def test_a_capture_at_another_rate():
    c = Capture(44100, 1)
    s = Stream(c.feed, rate=44100)
    s.until(1.0)
    c.begin(1.0)
    s.until(2.0)
    c.end(2.0)
    s.until(2.5)
    assert spans(c.take(), 0.7, 2.2)


def test_reset_drops_a_press_and_empties_the_ring():
    """The stream stopped (a refresh, the device gone): what the ring held is no longer the
    last 300 ms, and a press across the break is dropped."""
    c = Capture(RATE, 1)
    s = Stream(c.feed)
    s.until(1.0)
    c.begin(1.0)
    s.until(1.5)
    c.reset()
    c.end(1.5)
    s.until(2.0)
    assert c.take() is None
    s.until(2.1)
    c.reset()
    s.until(2.2)
    c.begin(2.2)
    c.end(2.2)
    s.until(2.5)
    assert spans(c.take(), 2.1, 2.4)


def test_reset_keeps_a_clip_waiting_for_its_tail():
    c = Capture(RATE, 1)
    s = Stream(c.feed)
    s.until(1.0)
    c.begin(1.0)
    s.until(2.0)
    c.end(2.0)
    s.until(2.1)
    c.reset()
    assert spans(c.take(), 0.7, 2.1)


def test_reset_to_a_new_format():
    c = Capture(RATE, 1)
    c.reset(device_rate=44100, channels=2)
    assert (c.device_rate, c.channels) == (44100, 2)
    s = Stream(c.feed, rate=44100, channels=2)
    s.until(1.0)
    c.begin(1.0)
    s.until(1.5)
    c.end(1.5)
    s.until(2.0)
    assert spans(c.take(), 0.7, 1.7)
    with pytest.raises(ValueError):
        c.feed(np.zeros((480, 1), np.float32))


def test_blocks_are_checked():
    c = Capture(RATE, 1)
    c.feed(np.zeros(480, np.float32))  # a mono block may come flat
    c.feed(np.zeros((480, 1), np.float64))
    for bad in (np.zeros((480, 2), np.float32), np.zeros((2, 2, 2), np.float32),
                np.float32(0.0)):
        with pytest.raises(ValueError):
            c.feed(bad)
    with pytest.raises(ValueError):
        Capture(RATE, 2).feed(np.zeros(480, np.float32))


def test_a_fed_block_is_copied():
    """PortAudio reuses its buffers: what the capture keeps is its own."""
    c = Capture(RATE, 1)
    c.begin(0.0)
    block = ramp(0, 4800)
    c.feed(block)
    block[:] = 9.0
    c.end(0.1)
    c.feed(ramp(4800, 9600))
    assert spans(c.take(), 0.0, 0.3)


def test_a_press_with_no_audio_is_no_clip():
    """A press before any audio came, or one too short to resample, hands nothing on."""
    c = Capture(RATE, 1)
    c.begin(0.0)
    c.begin(0.0)  # the first had no audio at all
    c.feed(np.zeros((1, 1), np.float32))
    c.begin(0.0)  # one frame: nothing at 16 kHz
    c.end(0.0)
    c.reset()
    assert c.take() is None and c.take() is None
    c.begin(0.0)
    s = Stream(c.feed)
    s.until(0.5)
    c.end(0.3)
    s.until(1.0)
    assert spans(c.take(), 0.0, 0.5)  # and a press after them is whole


@pytest.mark.parametrize("args,kw", [((0, 1), {}), ((RATE, 0), {}), ((48000.5, 1), {}),
                                     ((RATE, 1), {"preroll_ms": -1}),
                                     ((RATE, 1), {"preroll_ms": float("nan")}),
                                     ((RATE, 1), {"preroll_ms": float("inf")}),
                                     ((RATE, 1), {"preroll_ms": True}),
                                     ((RATE, 1), {"tail_ms": -1}),
                                     ((RATE, 1), {"tail_ms": float("nan")}),
                                     ((RATE, 1), {"max_s": 0}), ((RATE, 1), {"max_s": True}),
                                     ((RATE, 1), {"max_s": float("nan")}), ((True, 1), {})])
def test_bad_settings_are_refused(args, kw):
    with pytest.raises(ValueError):
        Capture(*args, **kw)


def test_the_stream_thread_feeds_while_the_loop_presses():
    """feed() runs on PortAudio's thread; begin, end and take on the event loop. At 16 kHz no
    resampler blurs the clip, so a frame lost or doubled between the two would show."""
    c = Capture(STT_RATE, 1)
    s = Stream(c.feed, rate=STT_RATE)
    s.until(0.5)
    stop = threading.Event()

    def run():
        while not stop.is_set():
            s.until(s.frames / STT_RATE + 0.01)
            time.sleep(0.0005)
    feeder = threading.Thread(target=run)
    feeder.start()
    clips = []
    deadline = time.monotonic() + 10.0
    try:
        for _ in range(20):
            c.begin(0.0)
            c.end(0.0)
            while (clip := c.take()) is None:
                assert time.monotonic() < deadline
                time.sleep(0.0005)
            clips.append(clip)
    finally:
        stop.set()
        feeder.join()
    for clip in clips:  # the ring and the tail, one unbroken stretch of the ramp each
        assert len(clip) == round(0.5 * STT_RATE)
        assert np.allclose(np.diff(clip), 0.1 / STT_RATE, rtol=0, atol=5e-6)


# -- the Mic -------------------------------------------------------------------------------------

def mic_on(api, name="Mic A", *, now=0.0):
    capture = Capture(RATE, 1)
    mic = Mic(api, name, capture)
    mic.open(now=now)
    return mic, capture


def to_mic(api):
    """Where the room's sound goes: into the input stream open now, else nowhere."""
    def sink(block):
        live = [st for st in api.streams if st.kind == "input" and st.active]
        if live:
            live[-1].feed(block)
    return sink


def test_the_named_mic_feeds_the_capture():
    api = FakeDeviceApi(inputs=["Mic A", "Mic B"])
    mic, capture = mic_on(api, "Mic B")
    assert mic.ok and mic.state == "ok" and api.opened() == ["Mic B"]
    stream = api.streams[-1]
    s = Stream(stream.feed)
    s.until(1.0)
    capture.begin(1.0)
    s.until(2.0)
    capture.end(2.0)
    s.until(2.5)
    assert spans(capture.take(), 0.7, 2.2)
    mic.close()  # the session goes offline: the device is let go
    assert stream.closed and mic.state == "closed" and not mic.ok


def test_missing_mic_never_falls_back():
    """The owner's answer: by its name only, never another device (PR15: invented names).
    Missing, it is retried every 10 s, and the push-to-talk light reads 'missing'; with no
    name it reads 'unset' and nothing opens."""
    api = FakeDeviceApi(inputs=["Mic A"])
    mic, _ = mic_on(api, "Mic Z")
    assert not mic.ok and mic.state == "missing" and api.opened() == []
    for t in (2.0, RETRY_S, 2 * RETRY_S, REFRESH_S, 2 * REFRESH_S):
        mic.poll(now=t)
    assert api.opened() == [] and mic.state == "missing"
    unset = Mic(api, "", Capture(RATE, 1))
    assert unset.state == "unset" and not unset.ok
    unset.open(now=0.0)
    unset.poll(now=RETRY_S)
    assert unset.state == "unset" and not unset.ok and api.opened() == []


def test_a_missing_mic_is_found_once_it_is_back():
    """Looked for every RETRY_S, PortAudio refreshed for it every REFRESH_S (each refresh
    bounces the output too)."""
    api = FakeDeviceApi(inputs=["Mic A"])
    mic, _ = mic_on(api, "Mic Z")
    assert api.count("refresh") == 1
    api.plug("Mic Z", inputs=1, outputs=0)
    mic.poll(now=RETRY_S)
    assert not mic.ok and api.count("refresh") == 1
    mic.poll(now=REFRESH_S)
    assert mic.ok and mic.state == "ok" and api.opened() == ["Mic Z"]


def test_an_open_is_a_fresh_try():
    """Plugged in while the session was offline: the next open finds it at once, whatever
    retry and refresh the last session left waiting."""
    api = FakeDeviceApi(inputs=["Mic A"])
    mic, _ = mic_on(api, "Mic Z")
    mic.close()
    api.plug("Mic Z", inputs=1, outputs=0)
    mic.open(now=2.0)
    assert mic.ok and api.count("refresh") == 2


def test_states():
    api = FakeDeviceApi(inputs=["Mic A"])
    mic = Mic(api, "Mic A", Capture(RATE, 1))
    assert mic.state == "closed" and not mic.ok
    mic.open(now=0.0)
    assert mic.state == "ok"
    api.streams[-1].fail()
    assert mic.state == "missing" and not mic.ok
    mic.close()
    assert mic.state == "closed" and not mic.ok
    mic.poll(now=RETRY_S)  # closed: nothing opens
    assert api.count("open") == 1
    mic.open(now=RETRY_S)
    assert mic.state == "ok" and api.count("open") == 2


def test_a_refresh_closes_the_mic_and_it_comes_back_at_once():
    """Carried from Task 7: the output's refresh closes the mic's stream. The mic treats a
    stream that is no longer active as failed and opens it again at its next poll, with no
    refresh of its own; the press across the break is dropped and the ring emptied."""
    api = FakeDeviceApi(outputs=["Speakers A"], inputs=["Mic A"], default="Speakers A")
    out = AudioOut(Mixer(), api)
    out.start(now=0.0)
    mic, capture = mic_on(api)
    s = Stream(to_mic(api))
    s.until(1.0)
    capture.begin(1.0)
    s.until(1.5)
    api.plug("Headphones B")  # a headset PortAudio has not listed yet ...
    api.default = "Headphones B"  # ... becomes the default
    out.poll(now=1.5)  # the output refreshes for it: the mic's stream closes too
    assert out.ok and api.count("refresh") == 1 and not mic.ok and mic.state == "missing"
    mic.poll(now=1.52)
    assert mic.ok and api.count("refresh") == 1 and api.opened()[-1] == "Mic A"
    capture.end(1.55)  # the key comes up: that press was dropped across the break
    s.until(1.6)
    capture.begin(1.6)
    capture.end(1.6)
    s.until(1.9)
    assert spans(capture.take(), 1.5, 1.8)  # the new stream's audio only, in the ring too
    assert capture.take() is None


def test_a_press_across_an_outage_is_dropped():
    """The stream dies mid-press and the key comes up before the mic is back: no clip of the
    audio from before the break is handed on, then or after the mic returns."""
    api = FakeDeviceApi(inputs=["Mic A"])
    mic, capture = mic_on(api)
    s = Stream(to_mic(api))
    s.until(1.0)
    capture.begin(1.0)
    s.until(1.5)
    api.streams[-1].fail()  # soon after its opening: tried again RETRY_S later
    mic.poll(now=1.5)
    assert not mic.ok
    capture.end(2.0)  # the key comes up during the outage
    mic.poll(now=1.5 + RETRY_S)
    assert mic.ok
    s.until(3.0)
    assert capture.take() is None


def test_a_mic_that_dies_soon_after_opening_waits():
    api = FakeDeviceApi(inputs=["Mic A"])
    mic, _ = mic_on(api)
    api.streams[-1].fail()
    mic.poll(now=2.0)
    assert not mic.ok and api.count("open") == 1
    mic.poll(now=RETRY_S)
    assert api.count("open") == 1
    mic.poll(now=2.0 + RETRY_S)
    assert mic.ok and api.count("open") == 2


def test_a_mic_that_dies_later_is_opened_again_at_once():
    api = FakeDeviceApi(inputs=["Mic A"])
    mic, _ = mic_on(api)
    mic.poll(now=EARLY_DEATH_S)
    api.streams[-1].fail()
    mic.poll(now=EARLY_DEATH_S + 0.02)
    assert mic.ok and api.events[-2:] == [("refresh", ""), ("open", "Mic A")]


def test_the_capture_takes_the_mics_format():
    api = FakeDeviceApi(inputs=[Dev("Mic S", 4, 0, 44100.0)])
    mic, capture = mic_on(api, "Mic S")
    assert mic.ok and (capture.device_rate, capture.channels) == (44100, 2)
    s = Stream(api.streams[-1].feed, rate=44100, channels=2)
    s.until(1.0)
    capture.begin(1.0)
    s.until(1.5)
    capture.end(1.5)
    s.until(2.0)
    assert spans(capture.take(), 0.7, 1.7)


def test_a_bad_block_never_reaches_portaudio(caplog):
    """An error in the stream's callback would abort the stream: it is counted and logged at
    the next poll instead."""
    api = FakeDeviceApi(inputs=["Mic A"])
    mic, _ = mic_on(api)
    api.streams[-1].feed(np.zeros((480, 3), np.float32))
    with caplog.at_level(logging.WARNING, logger="leeroy_voice"):
        mic.poll(now=2.0)
    assert mic.ok and "mic_feed_failed" in caplog.text


def test_mic_names_stay_out_of_the_log(caplog):
    api = FakeDeviceApi(inputs=["Mic A"])
    with caplog.at_level(logging.DEBUG, logger="leeroy_voice"):
        mic, _ = mic_on(api, "Mic Z")
        mic.poll(now=RETRY_S)
        api.plug("Mic Z", inputs=1, outputs=0)
        mic.poll(now=REFRESH_S)
        api.streams[-1].fail()
        mic.poll(now=REFRESH_S + 1.0)
        mic.close()
    assert caplog.records and "Mic" not in caplog.text


# -- what the service asks of it (Task 11, review-task8 M2 and M3) ---------------------------------

def test_end_tells_whether_a_clip_is_coming():
    """So the service never waits for a clip a dropped press will not give (M2)."""
    c = Capture(RATE, 1)
    s = Stream(c.feed)
    s.until(0.3)
    c.begin(0.3)
    s.until(0.5)
    assert c.end(0.5) is True          # its tail is still to come
    s.until(0.8)
    assert c.ready and c.take() is not None and not c.ready
    c.begin(0.8)
    s.until(1.0)
    c.reset()                          # a break in the stream drops the press
    assert c.end(1.0) is False and c.take() is None
    c.begin(1.0)
    c.discard()
    assert c.end(1.1) is False


def test_end_counts_a_clip_already_whole():
    """A press that ran to max_s and its tail before the Release is a clip all the same."""
    c = Capture(RATE, 1, preroll_ms=0, tail_ms=0, max_s=0.1)
    s = Stream(c.feed)
    c.begin(0.0)
    s.until(0.2)
    assert c.ready
    assert c.end(0.2) is True
    assert c.take() is not None


def test_a_new_streams_first_blocks_wait_for_its_format():
    """A block that wins the race with the stream's on_stream is dropped quietly, not counted
    as a failure against the old format (M3)."""
    api = FakeDeviceApi(inputs=[Dev("Mic A", 2, 0, 44100.0)])
    capture = Capture(RATE, 1)
    mic = Mic(api, "Mic A", capture)
    fed = []
    capture.feed = fed.append
    opener = api.open_input

    def open_and_race(name, callback):
        stream = opener(name, callback)
        callback(np.zeros((441, 2), np.float32))  # a block before on_stream has run
        return stream
    api.open_input = open_and_race
    mic.open(0.0)
    assert fed == [] and mic.ok
    api.streams[-1].feed(np.zeros((441, 2), np.float32))
    assert len(fed) == 1
