"""The audio devices (draft section 5): the device layer, and the output the mixer plays on.

DeviceApi is what the service needs of the devices: the WASAPI devices by name (an index changes
as devices come and go), the Windows default output, output and input streams, and refresh().
PortAudio lists the devices present when it was last initialized, so a device plugged in later is
not seen until refresh(), which re-initializes it and therefore closes every stream first: a
stream refresh() closed is no longer active, and its owner opens it again. SoundDeviceApi is the
real one; tests use FakeDeviceApi (tests/fakes.py) with invented names (PR15).

StreamKeeper keeps one stream open on a device by name, with the rules every stream follows
(review-task7 M1): since a refresh closes the mic's stream and the output's alike, it refreshes
only when it must, at once for a device not yet refreshed for and at most every REFRESH_S for
the same one, and never for a device Windows says it does not have (review-task8), so a missing
mic does not bounce the output, and a device that comes back is found at its next retry. A
stream someone else's refresh closed comes back at once; one that dies soon after opening waits
like a failed open. AudioOut keeps the output with one, and the capture's Mic the microphone.

AudioOut plays a Mixer on the Windows default output and follows it when it changes, or on a
device pinned by name and never another (the owner's settled answer). The service polls it at
least every POLL_S, and may at every tick. The owner's device names stay out of the log, as they
stay out of config errors (PR15).

sounddevice, pycaw and comtypes are imported only inside SoundDeviceApi and the COM thread, so
this module imports on Linux (CI) and in tests (M9)."""
import concurrent.futures
import logging
import queue
import threading
import time
from dataclasses import dataclass
from typing import Callable, Protocol

import numpy as np

from .logs import log

OUTPUT_RATE = 48000  # the mixer's, so WASAPI converts if the device runs at another rate
OUTPUT_CHANNELS = 2
BLOCK_FRAMES = 480  # 10 ms
OUTPUT_LATENCY_S = 0.10  # room for CPU synthesis/GIL contention; the callback still runs at 10 ms
INPUT_BLOCK_S = 0.01
POLL_S = 2.0  # the service polls AudioOut at least this often
RETRY_S = 10.0  # a device that is missing, or will not open, is tried again this often
REFRESH_S = 30.0  # PortAudio is refreshed for the same wanted device at most this often (M1)
EARLY_DEATH_S = 5.0  # a stream that dies this soon after opening counts as a failed open (M1)
DEFAULT_READ_S = 1.0  # the Windows default output is read at most this often, so polls are cheap
COM_TIMEOUT_S = 2.0
DEFAULT_NAME_TIMEOUT_S = 0.5  # the default output is read on the event loop
WASAPI = "Windows WASAPI"
FRIENDLY_NAME = ("{a45c254e-df1c-4efd-8020-67d146a850e0}", 14)  # PKEY_Device_FriendlyName
E_NOTFOUND = 0x80070490  # no such endpoint: Windows has no default output


@dataclass(frozen=True, slots=True)
class Dev:
    name: str
    inputs: int  # channels
    outputs: int
    default_rate: float


class DeviceError(Exception):
    """A stream that cannot be opened. Its message names no device (PR15)."""


class Stream(Protocol):
    @property
    def active(self) -> bool:
        """Running: False once it is closed, or once it died with its device."""

    @property
    def samplerate(self) -> float: ...

    @property
    def channels(self) -> int: ...

    def close(self) -> None: ...


class DeviceApi(Protocol):
    @property
    def generation(self) -> int:
        """How many refresh() calls there have been, a failed one included: a stream that stopped
        after a refresh its owner did not make was closed by it, not by its device."""

    def wasapi_devices(self) -> list[Dev]: ...

    def default_output_name(self) -> str:
        """The Windows default output's name as it is now; "" when there is none."""

    def windows_has(self, name: str) -> bool | None:
        """Whether Windows has an active device of this name now, whatever PortAudio lists;
        None when that cannot be told."""

    def open_output(self, name: str, callback: Callable[[np.ndarray], None]) -> Stream:
        """A running 48 kHz stereo float32 stream in blocks of BLOCK_FRAMES; `callback` fills
        each (frames, 2) block in place. Raises DeviceError."""

    def open_input(self, name: str, callback: Callable[[np.ndarray], None]) -> Stream:
        """A running float32 stream at the device's own rate, with up to 2 of its channels, in
        10 ms blocks; `callback` gets each (frames, channels) block, its own copy. Raises
        DeviceError."""

    def refresh(self) -> None:
        """List the devices present now. Every open stream is closed first."""


def _join_com() -> Callable[[], None]:
    """Join COM on this thread, multithreaded; returns what leaves it."""
    import comtypes  # the first import in the process initializes COM on this thread
    try:
        comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
    except OSError:  # that import made this thread apartment-threaded: also fine here
        return lambda: None
    return comtypes.CoUninitialize


class ComThread:
    """One thread for COM: every call runs there, so the COM objects it makes are used and
    released on it and never cross threads. It starts at the first call. submit() gives a
    Future; call() waits for the result (TimeoutError after `timeout`); post() does not wait.
    An error comes back without its frames, which held COM objects. `join` sets the thread up
    and returns what tears it down (tests pass one without COM); if it fails, every call fails
    at once with its error."""

    def __init__(self, name: str, *, join: Callable[[], Callable[[], None]] = _join_com):
        self._name = name
        self._join = join
        self._queue: queue.SimpleQueue = queue.SimpleQueue()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._closed = False

    def submit(self, fn, *args) -> concurrent.futures.Future:
        future: concurrent.futures.Future = concurrent.futures.Future()
        self._put((fn, args, future))
        return future

    def call(self, fn, *args, timeout: float = COM_TIMEOUT_S):
        return self.submit(fn, *args).result(timeout)

    def post(self, fn, *args) -> None:
        try:
            self._put((fn, args, None))
        except RuntimeError:
            pass  # closed: what it would have released went with the thread

    def close(self, timeout: float = COM_TIMEOUT_S) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            thread = self._thread
            if thread is not None:
                self._queue.put(None)
        if thread is not None:
            thread.join(timeout)

    def _put(self, item) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("the COM thread is closed")
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name=self._name, daemon=True)
                self._thread.start()
            self._queue.put(item)

    def _run(self) -> None:
        leave, failure = None, None
        try:
            leave = self._join()
        except Exception as e:  # no COM on this thread: every call fails with this
            failure = e.with_traceback(None)
        try:
            while (item := self._queue.get()) is not None:
                fn, args, future = item
                if future is not None and not future.set_running_or_notify_cancel():
                    continue
                if failure is not None:
                    if future is not None:
                        future.set_exception(failure)
                    continue
                try:
                    result = fn(*args)
                except BaseException as e:  # its frames held COM objects: keep them here
                    if future is not None:
                        future.set_exception(e.with_traceback(None))
                else:
                    if future is not None:
                        future.set_result(result)
        finally:
            if leave is not None:
                leave()


def _enumerator():
    import comtypes
    from pycaw.api.mmdeviceapi import IMMDeviceEnumerator
    from pycaw.constants import CLSID_MMDeviceEnumerator
    return comtypes.CoCreateInstance(CLSID_MMDeviceEnumerator, IMMDeviceEnumerator,
                                     comtypes.CLSCTX_INPROC_SERVER)


def _friendly_name(device) -> str:
    import comtypes
    from pycaw.api.mmdeviceapi.depend.structures import PROPERTYKEY
    from pycaw.constants import STGM
    store = device.OpenPropertyStore(STGM.STGM_READ.value)
    key = PROPERTYKEY()
    key.fmtid, key.pid = comtypes.GUID(FRIENDLY_NAME[0]), FRIENDLY_NAME[1]
    value = store.GetValue(key)
    try:
        name = value.GetValue()
    finally:
        value.clear()
    return name if isinstance(name, str) else ""


def _default_render_name() -> str:
    """On a COM thread: the name of the Windows default render endpoint (the multimedia role,
    PortAudio's), "" when there is none."""
    from _ctypes import COMError

    from pycaw.constants import EDataFlow, ERole
    try:
        device = _enumerator().GetDefaultAudioEndpoint(EDataFlow.eRender.value,
                                                       ERole.eMultimedia.value)
    except COMError as e:
        if (e.hresult & 0xFFFFFFFF) == E_NOTFOUND:
            return ""
        raise
    return _friendly_name(device)


def _active_endpoint_names() -> set[str]:
    """On a COM thread: the names of the active render and capture endpoints, as PortAudio's
    WASAPI host names them."""
    from pycaw.constants import DEVICE_STATE, EDataFlow
    endpoints = _enumerator().EnumAudioEndpoints(EDataFlow.eAll.value, DEVICE_STATE.ACTIVE.value)
    return {_friendly_name(endpoints.Item(i)) for i in range(endpoints.GetCount())}


def _why(e: Exception) -> str:
    return str(e.args[0]) if e.args else type(e).__name__


class _OutputStatus:
    """One callback writer, immutable cumulative snapshots; polling never clears a counter."""

    def __init__(self):
        self.counts = (0, 0, 0)  # callbacks, output underflows, output overflows

    def record(self, status) -> None:
        calls, under, over = self.counts
        self.counts = (calls + 1, under + bool(getattr(status, "output_underflow", False)),
                       over + bool(getattr(status, "output_overflow", False)))


class _SdStream:
    def __init__(self, api: "SoundDeviceApi", stream, output_status=None):
        self._api = api
        self._stream = stream
        self._output_status = output_status

    @property
    def active(self) -> bool:
        try:
            return bool(self._stream.active)
        except Exception:  # PortAudio no longer knows it
            return False

    @property
    def samplerate(self) -> float:
        return float(self._stream.samplerate)

    @property
    def channels(self) -> int:
        return int(self._stream.channels)

    @property
    def latency(self) -> float:
        return float(self._stream.latency)

    @property
    def output_counts(self) -> tuple[int, int, int]:
        return self._output_status.counts if self._output_status is not None else (0, 0, 0)

    def close(self) -> None:
        self._api._forget(self)
        try:
            self._stream.close()
        except Exception:
            pass


class SoundDeviceApi:
    """DeviceApi on the real devices: PortAudio's WASAPI host API through sounddevice, in shared
    mode with auto-convert (sd.WasapiSettings(auto_convert=True)), so other programs keep the
    device and WASAPI converts rate and channels. The default output is read live from the
    Windows render endpoint through pycaw, on a COM thread of its own; if that fails, PortAudio's
    default as of its last refresh. Call it from one thread (the service's event loop). Tests
    pass a stand-in for the sounddevice module, the default's reader and the COM join."""

    def __init__(self, *, sd=None, default_name: Callable[[], str] = _default_render_name,
                 active_names: Callable[[], set[str]] = _active_endpoint_names,
                 join: Callable[[], Callable[[], None]] = _join_com):
        if sd is None:
            import sounddevice as sd  # initializes PortAudio
        self._sd = sd
        self._default_name = default_name
        self._active_names = active_names
        self._join = join
        self.generation = 0
        self._lock = threading.Lock()
        self._streams: list[_SdStream] = []
        self._com: ComThread | None = None
        self._asking: concurrent.futures.Future | None = None  # the last ask for the default
        self._known: str | None = None  # the default Windows last gave

    def wasapi_devices(self) -> list[Dev]:
        api = self._wasapi()
        if api is None:
            return []
        return [Dev(d["name"], int(d["max_input_channels"]), int(d["max_output_channels"]),
                    float(d["default_samplerate"]))
                for d in self._sd.query_devices() if d["hostapi"] == api]

    def default_output_name(self) -> str:
        """Asked on the event loop, so it never waits on COM for long, nor again while an
        earlier ask is still stuck. Then, or when COM fails, the last name Windows gave
        answers (a stale default must not move the output), or PortAudio's default if Windows
        never answered."""
        if self._asking is None or self._asking.done():
            self._asking = self._on_com().submit(self._default_name)
            try:
                self._known = self._asking.result(DEFAULT_NAME_TIMEOUT_S)
                return self._known
            except Exception as e:
                log("default_output_unread", level=logging.DEBUG, error=type(e).__name__)
        if self._known is not None:
            return self._known
        api = self._wasapi()
        index = self._sd.query_hostapis(api)["default_output_device"] if api is not None else -1
        return self._sd.query_devices(index)["name"] if index >= 0 else ""

    def windows_has(self, name: str) -> bool | None:
        """Asked on the event loop, like the default: None when COM fails or is slow."""
        try:
            return name in self._on_com().call(self._active_names,
                                               timeout=DEFAULT_NAME_TIMEOUT_S)
        except Exception as e:
            log("device_presence_unread", level=logging.DEBUG, error=type(e).__name__)
            return None

    def _on_com(self) -> ComThread:
        if self._com is None:
            self._com = ComThread("leeroy-voice-devices", join=self._join)
        return self._com

    # The streams are sounddevice's raw ones, whose callbacks get PortAudio's bytes: its array
    # streams set `.shape` in place on every block, which NumPy 2.5 deprecates, and a warning
    # raised there kills the stream (under -W error now, and for good once NumPy drops it). The
    # blocks are wrapped here instead: a reshaped view to fill, a copy of what was heard.

    def open_output(self, name: str, callback: Callable[[np.ndarray], None]) -> Stream:
        sd = self._sd
        index, _ = self._find(name, "output")
        counts = _OutputStatus()

        def fill(outdata, frames, time_info, status):
            counts.record(status)  # no logging, IO, locks or counter resets on the audio thread
            callback(np.frombuffer(outdata, np.float32).reshape(frames, OUTPUT_CHANNELS))
        return self._start(lambda: sd.RawOutputStream(
            device=index, samplerate=OUTPUT_RATE, blocksize=BLOCK_FRAMES,
            channels=OUTPUT_CHANNELS, dtype="float32", latency=OUTPUT_LATENCY_S,
            extra_settings=sd.WasapiSettings(auto_convert=True), callback=fill),
            output_status=counts)

    def open_input(self, name: str, callback: Callable[[np.ndarray], None]) -> Stream:
        sd = self._sd
        index, dev = self._find(name, "input")
        rate = float(dev["default_samplerate"])
        channels = min(int(dev["max_input_channels"]), 2)

        def take(indata, frames, time_info, status):
            callback(np.frombuffer(indata, np.float32).reshape(frames, channels).copy())
        return self._start(lambda: sd.RawInputStream(
            device=index, samplerate=rate, blocksize=round(rate * INPUT_BLOCK_S),
            channels=channels, dtype="float32", latency="low",
            extra_settings=sd.WasapiSettings(auto_convert=True), callback=take))

    def refresh(self) -> None:
        """PortAudio terminated and initialized again, every stream closed first. Pa_Terminate
        fails only while PortAudio is down, after an initialize that failed, so initialize runs
        whatever terminate did (review M2): a failed refresh is never down for good, and the
        next one brings PortAudio back. Raises when initialize fails."""
        self.generation += 1  # every stream is closed now, whatever follows
        with self._lock:
            streams, self._streams = self._streams, []
        for stream in streams:
            stream.close()
        try:
            self._sd._terminate()
        except Exception as e:  # down already: initializing is all it needs
            log("portaudio_terminate_failed", level=logging.WARNING, error=type(e).__name__)
        finally:
            self._sd._initialize()

    def close(self) -> None:
        with self._lock:
            streams, self._streams = self._streams, []
        for stream in streams:
            stream.close()
        if self._com is not None:
            self._com.close()

    def _wasapi(self) -> int | None:
        for i, api in enumerate(self._sd.query_hostapis()):
            if api["name"] == WASAPI:
                return i
        return None

    def _find(self, name: str, kind: str):
        api = self._wasapi()
        for d in (self._sd.query_devices() if api is not None else ()):
            if d["hostapi"] == api and d["name"] == name and d[f"max_{kind}_channels"] > 0:
                return d["index"], d
        raise DeviceError(f"no WASAPI {kind} by that name")

    def _start(self, make, *, output_status=None) -> Stream:
        sd = self._sd
        try:
            stream = make()
        except (sd.PortAudioError, ValueError) as e:
            raise DeviceError(f"cannot open the stream: {_why(e)}") from None
        try:
            stream.start()
        except sd.PortAudioError as e:
            stream.close()
            raise DeviceError(f"cannot start the stream: {_why(e)}") from None
        kept = _SdStream(self, stream, output_status)
        with self._lock:
            self._streams.append(kept)
        return kept

    def _forget(self, stream: _SdStream) -> None:
        with self._lock:
            if stream in self._streams:
                self._streams.remove(stream)


class _Monotonic:
    @staticmethod
    def now() -> float:
        return time.monotonic()


class StreamKeeper:
    """One stream kept open on a device by name: what AudioOut and the capture's Mic share
    (review-task7 M1). keep(want, now) runs at each poll with the device wanted ("" while that
    is not known: whatever runs keeps running).

    - A live stream on another device is closed, and the wanted one opened.
    - A stream that stopped is closed and opened again. After a refresh its keeper did not make
      (DeviceApi.generation moved since it opened), at once and with no refresh of its own: the
      refresh closed it, and the list is as fresh as one of its own would make it. After dying
      on its own, with a refresh first, since its device may have come back as a new one; but a
      stream that died within EARLY_DEATH_S of opening counts as a failed open.
    - A device PortAudio does not list is looked for after a refresh. Every refresh closes every
      stream, the other keeper's too, so for one wanted device PortAudio is refreshed at once
      the first time and at most once per REFRESH_S after, and not at all while Windows says it
      does not have the device; a refresh that failed never counts.
    - A device still missing, or one that will not open, is tried again RETRY_S later, while a
      different one is tried at once.

    `stream`, `on` (the device it is open on), `ok` (a stream is open and running), `opens`.
    on_stream(stream) runs as a stream opens and on_stream(None) as one closes. The log events
    are "<label>_open", "_closed", "_missing", "_open_failed" and "_died_early", with `fields`;
    no device name is logged (PR15). Call it from one thread."""

    def __init__(self, devices: DeviceApi, kind: str, callback: Callable[[np.ndarray], None], *,
                 label: str, fields: dict | None = None,
                 on_stream: Callable[[Stream | None], None] | None = None):
        if kind not in ("output", "input"):
            raise ValueError(f"a stream is an output or an input, not {kind!r}")
        self._devices = devices
        self._kind = kind
        self._callback = callback
        self._label = label
        self._fields = dict(fields or {})
        self._on_stream = on_stream
        self.stream: Stream | None = None
        self.on = ""
        self.opens = 0
        self._opened_at = 0.0
        self._opened_gen = 0
        self._stale = False  # refresh before the next open: a stream died on its own
        self._retry_name = ""  # the device that was missing or would not open ...
        self._retry_at = 0.0  # ... and when to try it again
        self._refreshed: dict[str, float] = {}  # wanted device -> when it was last refreshed for

    @property
    def ok(self) -> bool:
        stream = self.stream
        return stream is not None and stream.active

    def fresh(self) -> None:
        """A start: the next keep() tries at once, and may refresh at once, whatever was
        waiting."""
        self._retry_name = ""
        self._refreshed.clear()

    def keep(self, want: str, now: float) -> None:
        stream = self.stream
        if stream is not None:
            live = stream.active
            if live and (want == self.on or not want):
                return
            if live:
                self.close("default_changed")
            else:
                self._stopped(now)
        if not want or (want == self._retry_name and now < self._retry_at):
            return
        self._open(want, now)

    def close(self, why: str) -> None:
        stream, self.stream, self.on = self.stream, None, ""
        if stream is None:
            return
        try:
            stream.close()
        except Exception:
            pass
        log(f"{self._label}_closed", level=logging.WARNING if why == "failed" else logging.INFO,
            why=why, **self._fields)
        if self._on_stream is not None:
            self._on_stream(None)

    def _stopped(self, now: float) -> None:
        """The stream is no longer active: a refresh closed it, or it died."""
        name = self.on
        if self._generation() != self._opened_gen:
            self._refreshed[name] = now  # someone else's refresh, as good as one of ours
            self.close("refreshed")
            return
        self._stale = True  # its device may have come back as a new one
        self.close("failed")
        if now - self._opened_at < EARLY_DEATH_S:
            self._miss(name, now, "died_early")

    def _generation(self) -> int:
        return getattr(self._devices, "generation", 0)

    def _lists(self, name: str) -> bool:
        try:
            return any(d.name == name and (d.outputs if self._kind == "output" else d.inputs) > 0
                       for d in self._devices.wasapi_devices())
        except Exception:
            return False

    def _open(self, want: str, now: float) -> None:
        if self._stale or not self._lists(want):
            last = self._refreshed.get(want)
            if ((last is None or not 0 <= now - last < REFRESH_S)
                    and self._devices.windows_has(want) is not False):
                self._refresh(want, now)
        if not self._lists(want):
            self._miss(want, now, "missing")
            return
        opener = self._devices.open_output if self._kind == "output" else self._devices.open_input
        try:
            stream = opener(want, self._callback)
        except Exception as e:  # DeviceError, or anything the device layer let through
            self._stale = True
            self._miss(want, now, "open_failed", error=type(e).__name__)
            return
        self.stream, self.on = stream, want
        self._opened_at, self._opened_gen = now, self._generation()
        self._retry_name = ""
        self._stale = False  # it opened: the list was good enough
        self.opens += 1
        log(f"{self._label}_open", opens=self.opens, **self._fields)
        if self._on_stream is not None:
            self._on_stream(stream)

    def _refresh(self, want: str, now: float) -> None:
        try:
            self._devices.refresh()
        except Exception as e:  # tried again with the next open
            log("device_refresh_failed", level=logging.WARNING, error=type(e).__name__)
            return
        self._stale = False
        self._refreshed = {name: t for name, t in self._refreshed.items()
                           if 0 <= now - t < REFRESH_S}
        self._refreshed[want] = now

    def _miss(self, want: str, now: float, event: str, **fields) -> None:
        first = want != self._retry_name
        self._retry_name, self._retry_at = want, now + RETRY_S
        log(f"{self._label}_{event}", level=logging.WARNING if first else logging.DEBUG,
            retry_s=RETRY_S, **self._fields, **fields)


class AudioOut:
    """A Mixer on an output device: 48 kHz stereo float32 in blocks of BLOCK_FRAMES, the
    stream's callback only rendering the mixer. If rendering ever fails, the block is silence
    and the stream plays on: an error escaping the callback would abort the stream.

    Unpinned, it plays on the Windows default output: poll() reads the default, at most every
    DEFAULT_READ_S, and when it changed closes the old stream and opens the new one. Pinned, it
    plays on that device and never on another. Its StreamKeeper reopens a stream that stopped
    and retries a device that is missing or will not open, by the rules review M1 set. While no
    stream runs, nothing renders, so the voices wait where they are. Between reads of the
    default a poll only checks the stream, so the service may poll at every tick: a stream the
    mic's refresh closed then comes back at the next one.

    `ok`: a stream is open and running. `device`: the device it plays on or is waiting for: the
    pinned name, else the default as last read. `now` (the clock's seconds) is optional on
    start() and poll(). Call it from one thread."""

    def __init__(self, mixer, devices: DeviceApi, *, pinned: str = "", clock=None):
        if mixer.sample_rate != OUTPUT_RATE:
            raise ValueError(f"the mixer runs at {mixer.sample_rate} Hz; the output at "
                             f"{OUTPUT_RATE} Hz")
        self._mixer = mixer
        self._devices = devices
        self._pinned = pinned
        self._clock = clock if clock is not None else _Monotonic()
        self._keeper = StreamKeeper(devices, "output", self._fill, label="output",
                                    fields={"pinned": bool(pinned)}, on_stream=self._stream_changed)
        self._running = False
        self._default_name = ""  # the Windows default as last read ...
        self._read_at: float | None = None  # ... and when
        self._render_failures = 0
        self._reported_render_failures = 0
        self._diagnostic_stream: Stream | None = None
        self._reported_counts = (0, 0, 0)
        self._total_counts = (0, 0, 0)
        self.device = pinned

    @property
    def ok(self) -> bool:
        return self._keeper.ok

    def start(self, now: float | None = None) -> None:
        self._running = True
        self._read_at = None
        self._keeper.fresh()
        self.poll(now)

    def stop(self) -> None:
        self._running = False
        self._keeper.close("stop")

    def poll(self, now: float | None = None) -> None:
        if not self._running:
            return
        now = self._clock.now() if now is None else now
        self._report_status()
        want = self._pinned or self._default(now)
        if want:
            self.device = want
        self._keeper.keep(want, now)

    def _report_status(self) -> None:
        failures = self._render_failures
        if failures != self._reported_render_failures:
            log("mixer_render_failed", level=logging.WARNING,
                blocks=failures - self._reported_render_failures, total_blocks=failures)
            self._reported_render_failures = failures
        stream = self._diagnostic_stream
        counts = getattr(stream, "output_counts", (0, 0, 0))
        delta = tuple(n - old for n, old in zip(counts, self._reported_counts))
        self._reported_counts = counts
        self._total_counts = tuple(n + d for n, d in zip(self._total_counts, delta))
        if delta[1] or delta[2]:
            log("output_xrun", level=logging.WARNING, underflows=delta[1], overflows=delta[2],
                total_underflows=self._total_counts[1], total_overflows=self._total_counts[2],
                total_callbacks=self._total_counts[0])

    def _stream_changed(self, stream: Stream | None) -> None:
        """Service thread only. Closing has joined the callback, so its final counts survive."""
        if stream is None:
            self._report_status()
            log("output_summary", callbacks=self._total_counts[0],
                underflows=self._total_counts[1], overflows=self._total_counts[2],
                render_failures=self._render_failures)
        self._diagnostic_stream = stream
        self._reported_counts = (0, 0, 0)
        if stream is not None:
            latency = getattr(stream, "latency", None)
            log("output_timing", sample_rate=stream.samplerate, channels=stream.channels,
                block_frames=BLOCK_FRAMES, requested_latency_ms=OUTPUT_LATENCY_S * 1000,
                actual_latency_ms=latency * 1000 if latency is not None else None)

    def _fill(self, outdata: np.ndarray) -> None:
        """The stream's callback, on PortAudio's thread."""
        try:
            outdata[:] = self._mixer.render(len(outdata))
        except Exception:
            outdata.fill(0)
            self._render_failures += 1

    def _default(self, now: float) -> str:
        """The default output, read again once DEFAULT_READ_S has passed; "" while unknown."""
        if self._read_at is None or not 0 <= now - self._read_at < DEFAULT_READ_S:
            self._read_at = now
            try:
                self._default_name = self._devices.default_output_name()
            except Exception as e:  # unknown for now: what plays keeps playing
                log("default_output_unread", level=logging.DEBUG, error=type(e).__name__)
                self._default_name = ""
        return self._default_name
