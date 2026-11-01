"""The output device (Task 7): the Windows default followed, a failed stream reopened, a pinned
device kept and never swapped for the default, on FakeDeviceApi's invented devices (PR15); the
refresh rules every stream keeper follows (review-task7 M1); the COM thread's machinery without
COM; and SoundDeviceApi on a stand-in for the sounddevice module, its refresh always bringing
PortAudio back (M2). Nothing here touches a device or COM."""
import logging
import threading
import time
import warnings

import numpy as np
import pytest

from fakes import FakeDeviceApi
from leeroy_voice import audio_io
from leeroy_voice.audio_io import (BLOCK_FRAMES, DEFAULT_READ_S, EARLY_DEATH_S, OUTPUT_RATE,
                                   REFRESH_S, RETRY_S, AudioOut, ComThread, Dev, DeviceError,
                                   SoundDeviceApi)
from leeroy_voice.capture import Capture, Mic
from leeroy_voice.mixer import Mixer


def started(api, *, pinned: str = "", mixer=None) -> AudioOut:
    out = AudioOut(mixer if mixer is not None else Mixer(), api, pinned=pinned)
    out.start(now=0.0)
    return out


def test_default_device_followed():
    """Review Focus 3: the default moves from Speakers A to Headphones B; one poll later the
    output plays on B, and A's stream was closed first."""
    api = FakeDeviceApi(outputs=["Speakers A", "Headphones B"], default="Speakers A")
    out = started(api)
    assert out.ok and out.device == "Speakers A"
    first = api.streams[-1]
    api.default = "Headphones B"
    assert out.ok and out.device == "Speakers A"  # nothing moves between polls
    out.poll(now=2.0)
    assert out.ok and out.device == "Headphones B"
    assert api.events == [("open", "Speakers A"), ("close", "Speakers A"),
                          ("open", "Headphones B")]
    assert first.closed
    out.poll(now=4.0)
    assert api.events[-1] == ("open", "Headphones B")  # settled: no reopen


def test_a_new_default_is_found_after_a_refresh():
    """A headset plugged in after PortAudio last listed its devices: the poll refreshes the list,
    then opens it."""
    api = FakeDeviceApi(outputs=["Speakers A"], default="Speakers A")
    out = started(api)
    api.plug("Headphones B")
    api.default = "Headphones B"
    out.poll(now=2.0)
    assert out.ok and out.device == "Headphones B"
    assert api.events == [("open", "Speakers A"), ("close", "Speakers A"), ("refresh", ""),
                          ("open", "Headphones B")]


def test_device_error_reopens():
    """A stream that has played a while and dies is reopened at the next poll, after a refresh:
    its device may have come back as a new one."""
    api = FakeDeviceApi(outputs=["Speakers A"], default="Speakers A")
    out = started(api)
    out.poll(now=EARLY_DEATH_S)  # it has played a while
    api.streams[-1].fail()  # what a real stream does when its device resets
    assert not out.ok  # meanwhile
    out.poll(now=EARLY_DEATH_S + 2.0)
    assert out.ok and out.device == "Speakers A"
    assert api.events == [("open", "Speakers A"), ("close", "Speakers A"), ("refresh", ""),
                          ("open", "Speakers A")]
    api.plug("Headphones B")
    api.refresh()  # the mic's: B is listed now
    out.poll(now=EARLY_DEATH_S + 3.0)
    api.default = "Headphones B"
    out.poll(now=EARLY_DEATH_S + 4.0)  # a listed device: no refresh of its own
    assert out.ok and out.device == "Headphones B" and api.count("refresh") == 2


def test_a_keeper_keeps_an_output_or_an_input():
    with pytest.raises(ValueError):
        audio_io.StreamKeeper(FakeDeviceApi(), "both", print, label="x")


def test_a_start_reads_the_default_at_once():
    api = FakeDeviceApi(outputs=["Speakers A", "Headphones B"], default="Speakers A")
    out = started(api)
    out.stop()
    api.default = "Headphones B"
    out.start(now=0.5)  # within DEFAULT_READ_S of the last read
    assert out.ok and out.device == "Headphones B"


def test_a_stream_that_dies_soon_after_opening_counts_as_a_failed_open():
    """review M1: a stream that opens and dies at once is tried again RETRY_S later, not at every
    poll, and refreshing for it again waits REFRESH_S: each refresh bounces the mic too."""
    api = FakeDeviceApi(outputs=["Speakers A"], default="Speakers A")
    out = started(api)
    api.streams[-1].fail()
    out.poll(now=2.0)
    assert not out.ok and api.events == [("open", "Speakers A"), ("close", "Speakers A")]
    for t in (4.0, 6.0, 8.0, 10.0):
        out.poll(now=t)
    assert api.count("open") == 1 and api.count("refresh") == 0
    out.poll(now=2.0 + RETRY_S)
    assert out.ok and api.events[-2:] == [("refresh", ""), ("open", "Speakers A")]


def test_a_stream_that_keeps_dying_refreshes_rarely():
    """The review's probe: a stream that opens fine and dies at once, polled every 2 s for a
    minute, refreshed PortAudio (and bounced the mic) 30 times. Now it is opened once per
    RETRY_S and refreshed for once per REFRESH_S."""
    api = FakeDeviceApi(outputs=["Speakers A"], default="Speakers A")
    out = started(api)
    for n in range(1, 31):
        for stream in api.streams:
            stream.fail()
        out.poll(now=2.0 * n)
    assert api.count("open") <= 1 + 60 / RETRY_S
    assert api.count("refresh") <= 1 + 60 / REFRESH_S


def test_a_missing_device_is_refreshed_for_every_30_s():
    """review M1: a device PortAudio does not list is refreshed for at once, then at most once
    per REFRESH_S, though the list is looked at for it every RETRY_S."""
    api = FakeDeviceApi(outputs=["Speakers A"], default="Speakers A")
    out = started(api, pinned="Headphones B")
    refreshed = [0.0] * api.count("refresh")
    for n in range(1, 61):  # two minutes, polled every 2 s
        before = api.count("refresh")
        out.poll(now=2.0 * n)
        refreshed += [2.0 * n] * (api.count("refresh") - before)
    assert REFRESH_S == 30.0 and refreshed == [0.0, 30.0, 60.0, 90.0, 120.0]
    assert api.opened() == []  # never the default instead


def test_a_new_device_name_is_refreshed_for_at_once():
    """A wanted device not refreshed for yet (a headset that has just become the default) is
    refreshed for at once; the same one again only REFRESH_S after its last refresh."""
    api = FakeDeviceApi(outputs=["Speakers A"], default="Speakers A")
    out = started(api)
    api.default = "Headphones C"  # PortAudio does not list it
    out.poll(now=2.0)
    assert api.count("refresh") == 1 and not out.ok
    api.default = "Headphones D"
    out.poll(now=4.0)
    assert api.count("refresh") == 2  # another name: at once
    api.default = "Headphones C"
    out.poll(now=6.0)
    assert api.count("refresh") == 2 and not out.ok  # C was refreshed for 4 s ago
    api.plug("Headphones C")
    out.poll(now=2.0 + REFRESH_S)
    assert out.ok and out.device == "Headphones C" and api.count("refresh") == 3


def test_a_stream_another_refresh_closed_reopens_at_once():
    """The mic's refresh (looking for its device) closes the output's stream too: the next poll
    opens it again with no refresh of its own, and however soon after its opening that was, it
    does not count as a death."""
    api = FakeDeviceApi(outputs=["Speakers A"], default="Speakers A")
    out = started(api)
    api.refresh()  # someone else's
    assert not out.ok
    out.poll(now=0.5)
    assert out.ok and api.count("refresh") == 1
    assert api.events == [("open", "Speakers A"), ("close", "Speakers A"), ("refresh", ""),
                          ("open", "Speakers A")]
    api.streams[-1].fail()  # this one dies on its own, soon after its opening
    out.poll(now=1.0)
    assert not out.ok and api.count("open") == 2  # a failed open: tried again RETRY_S later


def test_a_list_someone_else_refreshed_counts_as_fresh():
    """When another keeper's refresh closed the stream and its device is gone from that fresh
    list, no second refresh follows at once: the next one waits REFRESH_S, as its own would."""
    api = FakeDeviceApi(outputs=["Speakers A"], default="Speakers A")
    out = started(api, pinned="Speakers A")
    api.unplug("Speakers A")
    api.refresh()  # the mic's, which no longer finds Speakers A either
    out.poll(now=1.0)
    assert not out.ok and api.count("refresh") == 1
    api.plug("Speakers A")
    out.poll(now=1.0 + RETRY_S)
    assert not out.ok and api.count("refresh") == 1
    out.poll(now=1.0 + REFRESH_S)
    assert out.ok and api.count("refresh") == 2


def test_polls_between_default_reads_only_check_the_stream():
    """The default is read at most every DEFAULT_READ_S, so the service can poll at every tick:
    a stream a refresh closed comes back at the next tick, and Windows is not asked fifty
    times a second."""
    api = FakeDeviceApi(outputs=["Speakers A", "Headphones B"], default="Speakers A")
    reads = []
    real = api.default_output_name
    api.default_output_name = lambda: reads.append(1) or real()
    out = started(api)
    assert len(reads) == 1
    for n in range(1, 25):
        out.poll(now=0.02 * n)
    assert len(reads) == 1
    api.refresh()  # the mic, looking for its device
    out.poll(now=0.5)
    assert out.ok and len(reads) == 1 and api.events[-1] == ("open", "Speakers A")
    api.default = "Headphones B"
    out.poll(now=0.98)
    assert out.device == "Speakers A"  # not read yet
    out.poll(now=DEFAULT_READ_S)
    assert out.device == "Headphones B" and out.ok and len(reads) == 2


def test_an_unplugged_default_moves_to_the_new_one():
    api = FakeDeviceApi(outputs=["Speakers A", "Headphones B"], default="Headphones B")
    out = started(api)
    api.unplug("Headphones B")
    api.default = "Speakers A"  # Windows picks the next device
    assert not out.ok
    out.poll(now=2.0)
    assert out.ok and out.device == "Speakers A"


def test_pinned_device():
    """Pinned Headphones B plays there, not on the default; while B is missing nothing plays, B
    is looked for every 10 s (PortAudio refreshed for it every REFRESH_S, review M1), and the
    default is never taken instead (PR15: invented names)."""
    api = FakeDeviceApi(outputs=["Speakers A", "Headphones B"], default="Speakers A")
    out = started(api, pinned="Headphones B")
    assert out.ok and out.device == "Headphones B" and api.opened() == ["Headphones B"]
    out.poll(now=6.0)  # it has played a while
    api.unplug("Headphones B")
    assert not out.ok
    out.poll(now=8.0)  # tried at once: refreshed, and missing
    assert not out.ok and out.device == "Headphones B" and api.count("refresh") == 1
    for t in (10.0, 12.0, 14.0, 16.0):
        out.poll(now=t)
    out.poll(now=8.0 + RETRY_S)  # looked for again, in the list as last refreshed
    assert api.count("refresh") == 1 and not out.ok
    api.plug("Headphones B")
    out.poll(now=8.0 + 2 * RETRY_S)
    assert not out.ok and api.count("refresh") == 1  # within REFRESH_S of the last refresh
    out.poll(now=8.0 + REFRESH_S)
    assert out.ok and out.device == "Headphones B" and api.count("refresh") == 2
    assert api.opened() == ["Headphones B", "Headphones B"]  # never Speakers A


def test_a_pinned_device_missing_at_start():
    api = FakeDeviceApi(outputs=["Speakers A"], default="Speakers A")
    out = started(api, pinned="Headphones B")
    assert not out.ok and out.device == "Headphones B" and api.opened() == []
    out.poll(now=2.0)
    assert api.count("refresh") == 1  # the one try at the start
    api.plug("Headphones B")
    out.poll(now=RETRY_S)  # the list it looks in was refreshed at the start
    assert not out.ok and api.count("refresh") == 1
    out.poll(now=REFRESH_S)
    assert out.ok and api.opened() == ["Headphones B"] and api.count("refresh") == 2


def test_a_start_tries_at_once():
    api = FakeDeviceApi(outputs=["Speakers A"], default="Speakers A")
    out = started(api, pinned="Headphones B")
    out.stop()
    api.plug("Headphones B")
    out.start(now=2.0)  # within the 10 s, but a start is a fresh try
    assert out.ok and api.opened() == ["Headphones B"]


def test_a_device_that_will_not_open_is_retried_every_10_s():
    api = FakeDeviceApi(outputs=["Speakers A"], default="Speakers A")
    api.failing.add("Speakers A")  # listed, but another program holds it
    out = started(api)
    assert not out.ok and api.count("open_failed") == 1
    out.poll(now=2.0)
    out.poll(now=8.0)
    assert api.count("open_failed") == 1
    api.failing.clear()
    out.poll(now=RETRY_S)
    assert out.ok


def test_a_default_portaudio_cannot_see_is_retried_every_10_s():
    """A default that never shows up in PortAudio's list: nothing plays, it is looked for again
    every 10 s rather than at every poll, and a new default is tried at once."""
    api = FakeDeviceApi(outputs=["Speakers A"], default="Speakers A")
    out = started(api)
    api.default = "Headphones C"
    out.poll(now=2.0)
    assert not out.ok and out.device == "Headphones C" and api.count("refresh") == 1
    out.poll(now=4.0)
    assert api.count("refresh") == 1
    api.default = "Speakers A"
    out.poll(now=6.0)
    assert out.ok and out.device == "Speakers A"


def test_an_unreadable_default_keeps_the_output():
    api = FakeDeviceApi(outputs=["Speakers A"], default="Speakers A")
    out = started(api)
    api.default_error = OSError("the endpoint enumerator did not answer")
    out.poll(now=2.0)
    assert out.ok and out.device == "Speakers A"
    assert api.events == [("open", "Speakers A")]


def test_no_default_device():
    api = FakeDeviceApi(outputs=[], default="")
    out = started(api)
    assert not out.ok and api.events == []


def test_the_callback_renders_the_mixer():
    m = Mixer()
    api = FakeDeviceApi(outputs=["Speakers A"], default="Speakers A")
    started(api, mixer=m)
    stream = api.streams[-1]
    assert (stream.samplerate, stream.channels) == (OUTPUT_RATE, 2) and BLOCK_FRAMES == 480
    m.play("a", np.full(4800, 0.25, np.float32), 0.0, fade_in_ms=0)
    out = np.concatenate([stream.pump(BLOCK_FRAMES) for _ in range(4)])
    assert out.dtype == np.float32 and out.shape == (1920, 2)
    assert np.isclose(out[m.latency:], 0.25).all()


class Broken(Mixer):
    def render(self, frames):
        raise RuntimeError("a bug in the mixer")


def test_a_render_error_plays_silence(caplog):
    """An error escaping the callback would abort the stream (sounddevice's callbacks answer an
    exception with paAbort), and the block left in PortAudio's buffer is not silence. So a
    failure gives silence, and the stream plays on."""
    api = FakeDeviceApi(outputs=["Speakers A"], default="Speakers A")
    out = started(api, mixer=Broken())
    held = np.ones((BLOCK_FRAMES, 2), np.float32)  # whatever the buffer held
    api.streams[-1].callback(held)
    assert not held.any() and out.ok
    with caplog.at_level(logging.WARNING, logger="leeroy_voice"):
        out.poll(now=2.0)
    assert "mixer_render_failed" in caplog.text


def test_a_failing_device_layer_is_survived(caplog):
    api = FakeDeviceApi(outputs=["Speakers A"], default="Speakers A")
    out = started(api)
    out.poll(now=EARLY_DEATH_S)  # it has played a while
    stream = api.streams[-1]

    def broken(*args):
        raise OSError("PortAudio is not answering")
    stream.close = broken  # its close fails too
    stream.fail()
    api.refresh = broken
    api.wasapi_devices = broken
    with caplog.at_level(logging.WARNING, logger="leeroy_voice"):
        out.poll(now=6.0)
    assert not out.ok and "device_refresh_failed" in caplog.text
    del api.refresh, api.wasapi_devices, stream.close
    api.open_output = broken  # not even a DeviceError
    out.poll(now=6.0 + RETRY_S)  # the failed refresh is tried again: it never counted
    assert not out.ok and api.events[-1] == ("refresh", "")
    del api.open_output
    out.poll(now=6.0 + 2 * RETRY_S)  # within REFRESH_S of the refresh that worked: none
    assert out.ok and api.events[-2:] == [("refresh", ""), ("open", "Speakers A")]


def test_the_com_thread_runs_every_call_on_one_thread():
    joined, left = [], []

    def join():
        joined.append(threading.get_ident())
        return lambda: left.append(threading.get_ident())

    def boom():
        raise ValueError("a COM error")
    com = ComThread("test-com", join=join)
    assert joined == []  # it starts with the first call
    try:
        assert com.call(threading.get_ident) == joined[0] != threading.get_ident()
        assert com.call(lambda a, b: a + b, 2, 3) == 5
        assert com.submit(threading.get_ident).result(2) == joined[0]
        done = threading.Event()
        com.post(done.set)
        assert done.wait(2)
        with pytest.raises(ValueError) as e:
            com.call(boom)
        tb = e.value.__traceback__
        while tb is not None:  # the frames it raised in stayed on the COM thread
            assert tb.tb_frame.f_code is not boom.__code__
            tb = tb.tb_next
        com.post(boom)  # a posted call's error is dropped, and the thread goes on
        assert com.call(lambda: "still here") == "still here"
    finally:
        com.close()
    assert left == joined  # torn down on its own thread
    with pytest.raises(RuntimeError):
        com.call(lambda: 1)
    com.post(lambda: None)  # closed: dropped quietly
    com.close()


def test_a_com_thread_that_cannot_join_fails_every_call_at_once():
    def join():
        raise OSError("no COM on this thread")
    com = ComThread("test-com", join=join)
    try:
        began = time.monotonic()
        for _ in range(2):
            with pytest.raises(OSError):
                com.call(lambda: 1, timeout=5)
        assert time.monotonic() - began < 1
    finally:
        com.close()


def test_a_com_call_times_out():
    gate = threading.Event()
    com = ComThread("test-com", join=lambda: (lambda: None))
    try:
        with pytest.raises(TimeoutError):
            com.call(gate.wait, timeout=0.05)
    finally:
        gate.set()
        com.close()


def test_the_mixer_must_run_at_the_output_rate():
    with pytest.raises(ValueError):
        AudioOut(Mixer(44100), FakeDeviceApi(outputs=["Speakers A"], default="Speakers A"))


def test_stop_closes():
    api = FakeDeviceApi(outputs=["Speakers A"], default="Speakers A")
    out = started(api)
    out.stop()
    assert not out.ok and api.events == [("open", "Speakers A"), ("close", "Speakers A")]
    out.poll(now=2.0)  # stopped: nothing opens
    assert api.events[-1] == ("close", "Speakers A")
    out.start(now=4.0)
    assert out.ok and api.opened() == ["Speakers A", "Speakers A"]
    out.stop()
    out.start()  # on its own clock
    out.poll()
    assert out.ok and api.opened() == ["Speakers A"] * 3


def test_device_names_stay_out_of_the_log(caplog):
    """PR15: the owner's device names live only in his voice.toml."""
    api = FakeDeviceApi(outputs=["Speakers A", "Headphones B"], default="Speakers A")
    with caplog.at_level(logging.DEBUG, logger="leeroy_voice"):
        out = started(api)
        api.default = "Headphones B"
        out.poll(now=2.0)
        api.unplug("Headphones B")
        out.poll(now=4.0)
        out.stop()
    assert caplog.records
    assert "Speakers" not in caplog.text and "Headphones" not in caplog.text


# -- SoundDeviceApi on a stand-in for the sounddevice module -----------------------------------

class StubStream:
    def __init__(self, sd: "StubSd", kind: str, settings: dict):
        self.sd, self.kind, self.settings = sd, kind, settings
        self.samplerate, self.channels = settings["samplerate"], settings["channels"]
        self.active = self.closed = False
        sd.calls.append(("open", kind, settings["device"]))
        sd.streams.append(self)

    def start(self):
        if self.sd.fail_start:
            raise self.sd.PortAudioError("Error starting stream: Unanticipated host error")
        self.active = True

    def close(self):
        self.active, self.closed = False, True
        self.sd.calls.append(("close", self.kind, self.settings["device"]))


class StubSd:
    """sounddevice as SoundDeviceApi uses it: MME and WASAPI, with the same invented devices on
    each (WASAPI's at other indexes), streams that keep their settings, and a log of calls. It
    offers the raw streams only, whose callbacks get bytes: sounddevice's array streams set
    `.shape` in place on every block, which NumPy 2.5 deprecates."""

    class PortAudioError(Exception):
        pass

    def __init__(self):
        self.hostapis = [{"name": "MME", "default_output_device": 0},
                         {"name": "Windows WASAPI", "default_output_device": 2}]
        self.devices = [self._dev(0, "Speakers A", 0, 0, 2, 44100.0),
                        self._dev(1, "Mic A", 0, 2, 0, 44100.0),
                        self._dev(2, "Speakers A", 1, 0, 2, 48000.0),
                        self._dev(3, "Mic A", 1, 4, 0, 44100.0)]
        self.calls: list[tuple] = []
        self.streams: list[StubStream] = []
        self.fail_start = False

    @staticmethod
    def _dev(index, name, hostapi, inputs, outputs, rate) -> dict:
        return {"index": index, "name": name, "hostapi": hostapi, "max_input_channels": inputs,
                "max_output_channels": outputs, "default_samplerate": rate}

    def query_hostapis(self, index=None):
        return self.hostapis if index is None else self.hostapis[index]

    def query_devices(self, index=None):
        return list(self.devices) if index is None else self.devices[index]

    def WasapiSettings(self, exclusive=False, auto_convert=False, explicit_sample_format=False):
        return ("wasapi", exclusive, auto_convert, explicit_sample_format)

    def RawOutputStream(self, **settings):
        return StubStream(self, "output", settings)

    def RawInputStream(self, **settings):
        return StubStream(self, "input", settings)

    def _terminate(self):
        self.calls.append(("terminate",))

    def _initialize(self):
        self.calls.append(("initialize",))


def no_com():
    return lambda: None


def stub_api(sd, default_name=lambda: "Speakers A") -> SoundDeviceApi:
    return SoundDeviceApi(sd=sd, default_name=default_name, join=no_com)


def test_sounddevice_lists_wasapi_only():
    api = stub_api(StubSd())
    assert api.wasapi_devices() == [Dev("Speakers A", 0, 2, 48000.0), Dev("Mic A", 4, 0, 44100.0)]


def test_sounddevice_opens_shared_wasapi_streams():
    sd = StubSd()
    api = stub_api(sd)
    filled = []

    def fill(block):
        filled.append(block)
        block[:] = 0.25
    out = api.open_output("Speakers A", fill)
    settings = sd.streams[-1].settings
    assert settings["device"] == 2  # WASAPI's, by name, not MME's
    assert (settings["samplerate"], settings["channels"], settings["dtype"],
            settings["blocksize"]) == (48000, 2, "float32", 480)
    assert settings["extra_settings"] == ("wasapi", False, True, False)  # shared, auto-convert
    buffer = bytearray(480 * 2 * 4)  # what PortAudio hands a raw output stream's callback
    settings["callback"](buffer, 480, None, 0)
    assert filled[0].shape == (480, 2) and filled[0].dtype == np.float32
    assert (np.frombuffer(buffer, np.float32) == 0.25).all()  # filled in place
    assert out.active and (out.samplerate, out.channels) == (48000, 2)
    taken = []
    mic = api.open_input("Mic A", taken.append)
    settings = sd.streams[-1].settings
    assert (settings["device"], settings["samplerate"], settings["channels"],
            settings["blocksize"]) == (3, 44100.0, 2, 441)  # its rate, 2 of its 4 channels, 10 ms
    assert settings["extra_settings"] == ("wasapi", False, True, False)
    data = np.arange(882, dtype=np.float32).reshape(441, 2)
    buffer = bytearray(data.tobytes())
    settings["callback"](buffer, 441, None, 0)
    buffer[:] = bytes(len(buffer))  # PortAudio reuses its buffer for the next block
    assert np.array_equal(taken[0], data)  # its own copy, as (frames, channels)
    assert mic.active
    for name, opener in (("Headphones B", api.open_output), ("Mic A", api.open_output),
                         ("Speakers A", api.open_input)):
        with pytest.raises(DeviceError) as e:
            opener(name, filled.append)
        assert name not in str(e.value)  # PR15


def test_the_stream_callbacks_raise_no_warning():
    """Both callbacks wrap PortAudio's bytes themselves, with nothing NumPy deprecates: a warning
    raised in a callback would kill the stream under -W error."""
    sd = StubSd()
    api = stub_api(sd)
    taken = []
    out = api.open_output("Speakers A", lambda block: block.fill(0.5))
    output = sd.streams[-1].settings["callback"]
    mic = api.open_input("Mic A", taken.append)
    capture = sd.streams[-1].settings["callback"]
    played = bytearray(480 * 2 * 4)
    heard = bytearray(np.full((441, 2), 0.125, np.float32).tobytes())
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        output(played, 480, None, 0)
        capture(heard, 441, None, 0)
    assert (np.frombuffer(played, np.float32) == 0.5).all()
    assert taken[0].shape == (441, 2) and (taken[0] == 0.125).all()
    out.close()
    mic.close()


def test_sounddevice_refresh_closes_every_stream_first():
    sd = StubSd()
    api = stub_api(sd)
    out = api.open_output("Speakers A", lambda block: None)
    mic = api.open_input("Mic A", lambda block: None)
    sd.calls.clear()
    api.refresh()
    assert sd.calls == [("close", "output", 2), ("close", "input", 3), ("terminate",),
                        ("initialize",)]
    assert not out.active and not mic.active
    sd.calls.clear()
    api.refresh()
    assert sd.calls == [("terminate",), ("initialize",)]  # nothing left to close


class CountingSd(StubSd):
    """StubSd keeping PortAudio's own count: Pa_Terminate fails only while PortAudio is not
    initialized, and Pa_Initialize can be made to fail."""

    def __init__(self):
        super().__init__()
        self.initialized = 1  # by the import
        self.init_failures = 0

    def _terminate(self):
        self.calls.append(("terminate",))
        if not self.initialized:
            raise self.PortAudioError("Error terminating PortAudio: PortAudio not initialized")
        self.initialized -= 1

    def _initialize(self):
        self.calls.append(("initialize",))
        if self.init_failures:
            self.init_failures -= 1
            raise self.PortAudioError("Error initializing PortAudio: Unanticipated host error")
        self.initialized += 1


def test_a_failed_refresh_is_never_down_for_good():
    """review M2: an initialize that fails leaves PortAudio down, and every later terminate then
    fails. A refresh initializes whatever terminate did, so the next one brings it back."""
    sd = CountingSd()
    api = stub_api(sd)
    sd.init_failures = 1
    with pytest.raises(sd.PortAudioError):
        api.refresh()
    assert sd.initialized == 0  # down
    sd.calls.clear()
    api.refresh()  # its terminate fails: PortAudio is not up
    assert sd.calls == [("terminate",), ("initialize",)] and sd.initialized == 1
    api.refresh()
    assert sd.initialized == 1


def test_a_refresh_initializes_whatever_terminate_raised():
    class Raising(CountingSd):
        def _terminate(self):
            self.calls.append(("terminate",))
            raise KeyboardInterrupt  # not even an Exception
    sd = Raising()
    api = stub_api(sd)
    with pytest.raises(KeyboardInterrupt):
        api.refresh()
    assert sd.calls == [("terminate",), ("initialize",)]


def test_refreshes_are_counted():
    """DeviceApi.generation: how many refreshes there have been, a failed one too (it closed
    every stream all the same), so a keeper knows a refresh closed its stream."""
    sd = CountingSd()
    api = stub_api(sd)
    assert api.generation == 0
    api.refresh()
    sd.init_failures = 1
    with pytest.raises(sd.PortAudioError):
        api.refresh()
    assert api.generation == 2
    fake = FakeDeviceApi(outputs=["Speakers A"])
    fake.refresh()
    assert fake.generation == 1


def test_sounddevice_closes_a_stream_that_will_not_start():
    sd = StubSd()
    sd.fail_start = True
    api = stub_api(sd)
    with pytest.raises(DeviceError) as e:
        api.open_output("Speakers A", lambda block: None)
    assert "Speakers" not in str(e.value) and sd.streams[-1].closed
    sd.fail_start = False
    api.open_output("Speakers A", lambda block: None)
    sd.calls.clear()
    api.close()
    assert sd.calls == [("close", "output", 2)]  # the failed one was never kept


def test_the_default_comes_from_windows(monkeypatch):
    monkeypatch.setattr(audio_io, "DEFAULT_NAME_TIMEOUT_S", 0.05)
    sd = StubSd()
    api = stub_api(sd, default_name=lambda: "Headphones B")
    try:
        assert api.default_output_name() == "Headphones B"
    finally:
        api.close()

    def broken():
        raise OSError("COM is not answering")
    api = stub_api(sd, default_name=broken)
    try:
        assert api.default_output_name() == "Speakers A"  # Windows never answered: PortAudio's
    finally:
        api.close()
    answers = iter(["Headphones B"])

    def once():
        for answer in answers:
            return answer
        raise OSError("COM is not answering")
    api = stub_api(sd, default_name=once)
    try:
        assert api.default_output_name() == "Headphones B"
        assert api.default_output_name() == "Headphones B"  # the last name Windows gave: a
        # stale PortAudio default ("Speakers A") must not move the output
    finally:
        api.close()


def test_a_stuck_default_never_holds_the_loop_twice(monkeypatch):
    """The first ask waits DEFAULT_NAME_TIMEOUT_S; while it is stuck, later asks answer at once;
    once it answers, Windows is asked again."""
    monkeypatch.setattr(audio_io, "DEFAULT_NAME_TIMEOUT_S", 0.05)
    free = threading.Event()

    def slow():
        free.wait(5)
        return "Headphones B"
    api = stub_api(StubSd(), default_name=slow)
    try:
        assert api.default_output_name() == "Speakers A"  # Windows never answered: PortAudio's
        began = time.monotonic()
        assert api.default_output_name() == "Speakers A"
        assert time.monotonic() - began < 0.04  # no second wait while it is stuck
        free.set()
        end = time.monotonic() + 5
        while api.default_output_name() != "Headphones B":
            assert time.monotonic() < end
            time.sleep(0.01)
        free.clear()  # stuck again: the last name Windows gave
        assert api.default_output_name() == "Headphones B"
        began = time.monotonic()
        assert api.default_output_name() == "Headphones B"
        assert time.monotonic() - began < 0.04
    finally:
        free.set()
        api.close()


def test_a_good_reopen_ends_the_need_for_a_refresh():
    """A stream that died and reopened with no refresh (within REFRESH_S of the last) leaves
    nothing owed: the next device, listed from the start, needs none (review-task8 M1)."""
    api = FakeDeviceApi(outputs=["Speakers A", "Headphones B"], default="Speakers A")
    out = started(api)
    out.poll(now=6.0)
    api.streams[-1].fail()                  # played a while, then died
    out.poll(now=6.0)
    assert out.ok and api.count("refresh") == 1
    out.poll(now=13.0)
    api.streams[-1].fail()                  # within REFRESH_S of that refresh
    out.poll(now=13.0)
    assert out.ok and api.count("refresh") == 1
    api.default = "Headphones B"
    out.poll(now=14.5)
    assert out.ok and out.device == "Headphones B" and api.count("refresh") == 1


# -- Windows' own list (review-task8: a missing mic's refresh bounces the output) ------------------

def windows_list(api) -> FakeDeviceApi:
    api.windows_answers = True
    return api


def test_a_device_windows_does_not_have_costs_no_refresh():
    """A missing mic would bounce the output every REFRESH_S for the whole session. Windows
    says it is not there, so PortAudio is left alone, and the output plays on."""
    api = windows_list(FakeDeviceApi(outputs=["Speakers A"], default="Speakers A"))
    mic = Mic(api, "Mic A", Capture(48000, 1))
    out = started(api)
    mic.open(now=0.0)
    for n in range(1, 91):
        out.poll(now=2.0 * n)
        mic.poll(now=2.0 * n)
    assert api.count("refresh") == 0 and out.ok and api.opened() == ["Speakers A"]
    assert mic.state == "missing"


def test_a_device_back_in_windows_is_found_at_its_next_retry():
    """Not REFRESH_S later: no refresh was spent while it was gone."""
    api = windows_list(FakeDeviceApi(outputs=["Speakers A"], default="Speakers A"))
    mic = Mic(api, "Mic A", Capture(48000, 1))
    mic.open(now=0.0)
    for n in range(1, 6):
        mic.poll(now=2.0 * n)
    assert api.count("refresh") == 0
    api.plug("Mic A", inputs=1, outputs=0)
    mic.poll(now=20.0)
    assert api.count("refresh") == 1 and mic.ok


def test_an_answer_of_unknown_keeps_the_refresh_limit():
    """COM failed or was slow: REFRESH_S still bounds it, as with no list at all."""
    api = FakeDeviceApi(outputs=["Speakers A"], default="Speakers A")
    assert api.windows_has("Mic A") is None
    mic = Mic(api, "Mic A", Capture(48000, 1))
    mic.open(now=0.0)
    assert api.count("refresh") == 1


def test_a_stream_that_died_with_its_device_costs_no_refresh():
    api = windows_list(FakeDeviceApi(outputs=["Speakers A"], default="Speakers A"))
    api.plug("Headphones B")
    api.refresh()
    out = started(api, pinned="Headphones B")
    out.poll(now=EARLY_DEATH_S + 1.0)
    before = api.count("refresh")
    api.unplug("Headphones B")
    out.poll(now=EARLY_DEATH_S + 2.0)
    assert api.count("refresh") == before and not out.ok


def test_the_real_api_asks_windows_on_its_com_thread():
    names = {"Speakers A", "Mic A"}
    api = SoundDeviceApi(sd=StubSd(), active_names=lambda: names, join=lambda: lambda: None)
    assert api.windows_has("Mic A") is True and api.windows_has("Mic B") is False
    api.close()


def test_the_real_api_answers_unknown_when_com_fails():
    def broken():
        raise OSError("no COM")
    api = SoundDeviceApi(sd=StubSd(), active_names=broken, join=lambda: lambda: None)
    assert api.windows_has("Mic A") is None
    api.close()
