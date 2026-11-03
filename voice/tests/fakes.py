"""The voice suite's fakes. Later tasks add theirs here (M2)."""
import asyncio
import heapq
import itertools
import json
import threading
from datetime import datetime, timedelta

import numpy as np
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from leeroy_voice.audio_io import Dev, DeviceError
from leeroy_voice.duck import SessionError
from leeroy_voice.wire import MAX_LINE_BYTES, WireError, encode, parse


class FakeClock:
    """The mind's FakeClock: now() in unix seconds, sleep() that wakes only on advance()."""

    def __init__(self, start: datetime, *, yields: int = 0):
        """With `yields`, each step of advance() yields to the event loop that many times
        instead of sleeping 5 ms of real time: for tests whose tasks do no real I/O or
        threads, so the loop settles without real time passing."""
        self._start = start
        self._t = start.timestamp()
        self._sleepers: list[tuple[float, int, asyncio.Future]] = []
        self._seq = itertools.count()
        self._yields = yields

    def now(self) -> float:
        return self._t

    def local(self) -> datetime:
        return self._start + timedelta(seconds=self._t - self._start.timestamp())

    async def sleep(self, s: float) -> None:
        if s <= 0:
            await asyncio.sleep(0)
            return
        fut = asyncio.get_running_loop().create_future()
        heapq.heappush(self._sleepers, (self._t + s, next(self._seq), fut))
        await fut

    async def advance(self, seconds: float, step: float = 0.5) -> None:
        target = self._t + seconds
        while self._t < target:
            self._t = min(self._t + step, target)
            while self._sleepers and self._sleepers[0][0] <= self._t:
                _, _, fut = heapq.heappop(self._sleepers)
                if not fut.done():
                    fut.set_result(None)
            if self._yields:
                for _ in range(self._yields):
                    await asyncio.sleep(0)
            else:
                await asyncio.sleep(0.005)


class FakeMind:
    """A mind for MindServer: a TCP client that sends wire messages or raw bytes and records
    every line it receives, parsed (a line that does not parse is kept as its WireError)."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self._reader = reader
        self._writer = writer
        self.received: list = []
        self._queue: asyncio.Queue = asyncio.Queue()
        self.eof = asyncio.Event()
        self._task = asyncio.create_task(self._read())

    @classmethod
    async def connect(cls, port: int, host: str = "127.0.0.1") -> "FakeMind":
        reader, writer = await asyncio.open_connection(host, port, limit=MAX_LINE_BYTES + 1)
        return cls(reader, writer)

    async def _read(self) -> None:
        try:
            while True:
                line = await self._reader.readuntil(b"\n")
                try:
                    msg = parse(line)
                except WireError as e:
                    msg = e
                self.received.append(msg)
                self._queue.put_nowait(msg)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, OSError):
            pass
        finally:
            self.eof.set()

    async def next(self, timeout: float = 2.0):
        """The next message received and not yet taken."""
        return await asyncio.wait_for(self._queue.get(), timeout)

    async def send(self, msg, ts_ms: int = 1) -> None:
        await self.send_raw(encode(msg, ts_ms))

    async def send_raw(self, data: bytes) -> None:
        self._writer.write(data)
        await self._writer.drain()

    async def wait_eof(self, timeout: float = 2.0) -> None:
        await asyncio.wait_for(self.eof.wait(), timeout)

    async def close(self) -> None:
        self._writer.close()
        try:
            await asyncio.wait_for(self._writer.wait_closed(), 2.0)
        except (OSError, asyncio.TimeoutError):
            pass
        await asyncio.wait({self._task}, timeout=2.0)


# -- devices, sessions and keys (Tasks 7 and 8; Task 11 uses them too) --------------------------

class FakeStream:
    """A stream FakeDeviceApi opened: active until it is closed, or until fail() kills it as a
    real stream dies when its device goes away. The test plays the device: pump() asks an output
    for a block, feed() hands an input one."""

    def __init__(self, api: "FakeDeviceApi", kind: str, name: str, callback, samplerate: float,
                 channels: int):
        self._api = api
        self.kind = kind  # "output" or "input"
        self.name = name
        self.callback = callback
        self.samplerate = samplerate
        self.channels = channels
        self.active = True
        self.closed = False

    def fail(self) -> None:
        self.active = False

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.active = False
            self._api.events.append(("close", self.name))

    def pump(self, frames: int = 480) -> np.ndarray:
        """One block from an output stream's callback, as the device asks for it."""
        out = np.zeros((frames, self.channels), np.float32)
        self.callback(out)
        return out

    def feed(self, block) -> None:
        """One block into an input stream's callback, as the device delivers it."""
        self.callback(np.asarray(block, np.float32))


class FakeDeviceApi:
    """DeviceApi with invented devices (PR15): outputs and inputs by name (a Dev for anything
    else), each at 48 kHz. Like PortAudio, wasapi_devices() lists the devices that were present
    at the last refresh(); `present` is what Windows has now, and `default` its default output,
    read live as pycaw reads it. refresh() closes every open stream first, as the real one does,
    and counts itself in `generation`. An open of a device it does not list, of one no longer
    present, or of a name in `failing` raises DeviceError. `events` records ("open" |
    "open_failed" | "close" | "refresh", name) in order. windows_has() cannot tell (None) unless
    `windows_answers` is set."""

    def __init__(self, outputs=(), inputs=(), *, default: str = ""):
        self.present: list[Dev] = ([self._dev(d, inputs=0, outputs=2) for d in outputs]
                                   + [self._dev(d, inputs=1, outputs=0) for d in inputs])
        self._listed = list(self.present)
        self.default = default
        self.default_error: Exception | None = None  # raised by default_output_name when set
        self.failing: set[str] = set()
        self.windows_answers = False
        self.generation = 0
        self.events: list[tuple[str, str]] = []
        self.streams: list[FakeStream] = []

    @staticmethod
    def _dev(d, *, inputs: int, outputs: int) -> Dev:
        return d if isinstance(d, Dev) else Dev(d, inputs, outputs, 48000.0)

    def wasapi_devices(self) -> list[Dev]:
        return list(self._listed)

    def default_output_name(self) -> str:
        if self.default_error is not None:
            raise self.default_error
        return self.default

    def windows_has(self, name: str) -> bool | None:
        if not self.windows_answers:
            return None
        return any(d.name == name for d in self.present)

    def open_output(self, name: str, callback) -> FakeStream:
        return self._open("output", name, callback)

    def open_input(self, name: str, callback) -> FakeStream:
        return self._open("input", name, callback)

    def refresh(self) -> None:
        self.generation += 1
        for stream in self.streams:
            stream.close()
        self._listed = list(self.present)
        self.events.append(("refresh", ""))

    def plug(self, name: str, *, inputs: int = 0, outputs: int = 2) -> None:
        """A device appears in Windows; PortAudio lists it from the next refresh()."""
        self.present.append(Dev(name, inputs, outputs, 48000.0))

    def unplug(self, name: str) -> None:
        """A device goes away: it leaves `present`, and its open streams die."""
        self.present = [d for d in self.present if d.name != name]
        for stream in self.streams:
            if stream.name == name and not stream.closed:
                stream.fail()

    def opened(self) -> list[str]:
        return [name for event, name in self.events if event == "open"]

    def count(self, event: str) -> int:
        return sum(1 for e, _ in self.events if e == event)

    def _open(self, kind: str, name: str, callback) -> FakeStream:
        dev = next((d for d in self._listed if d.name == name
                    and (d.outputs if kind == "output" else d.inputs) > 0), None)
        if dev is None or name in self.failing or all(d.name != name for d in self.present):
            self.events.append(("open_failed", name))
            raise DeviceError(f"no {kind} to open")
        channels = 2 if kind == "output" else min(dev.inputs, 2)
        stream = FakeStream(self, kind, name, callback,
                            48000 if kind == "output" else dev.default_rate, channels)
        self.streams.append(stream)
        self.events.append(("open", name))
        return stream


class FakeKeys:
    """RawInputKeys without Win32 (Task 8; Task 11's service makes it through keys_factory): the
    test plays the keyboard. key(down) is the key going down or up: Raw Input hands it to on_key,
    at once and on the caller's thread (the real one posts it to the event loop), while started
    and armed. With delivered=False the event is lost, as a missed key-up is, and only pressed()
    sees the key's state, as GetAsyncKeyState does. close() is the WM_CLOSE a plain taskkill
    posts to the window, which exists only while started."""

    def __init__(self, vk: int, on_key, *, on_close):
        self.vk = vk
        self._on_key = on_key
        self._on_close = on_close
        self.armed = False
        self.started = False
        self.stopped = False
        self.down = False  # the key as Windows has it
        self.hwnd = 0

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.started = False
        self.stopped = True

    def pressed(self) -> bool:
        return self.down

    def key(self, down: bool, *, delivered: bool = True) -> None:
        self.down = down
        if delivered and self.started and self.armed:
            self._on_key(down)

    def close(self) -> None:
        if self.started:
            self._on_close()


class FakeHandle:
    """One audio session's volume in the Windows mixer (0-1). Every set() is kept in `sets`;
    once its process has quit, get() and set() raise SessionError."""

    def __init__(self, volume: float):
        self.volume = float(volume)
        self.sets: list[float] = []
        self.gone = False

    def get(self) -> float:
        if self.gone:
            raise SessionError("the session is gone")
        return self.volume

    def set(self, v: float) -> None:
        if self.gone:
            raise SessionError("the session is gone")
        self.volume = float(v)
        self.sets.append(float(v))


class FakeSessions:
    """SessionApi on named sessions: FakeSessions({"Wow.exe": 0.8}) holds one session of Wow.exe
    at 0.8. find() matches the process name without regard to case, as Windows does."""

    def __init__(self, volumes: dict[str, float] | None = None):
        self._lock = threading.Lock()
        self._sessions: dict[str, list[FakeHandle]] = {}
        self.finds = 0
        for process, volume in (volumes or {}).items():
            self.launch(process, volume)

    def find(self, process: str) -> list[FakeHandle]:
        with self._lock:
            self.finds += 1
            return [h for name, handles in self._sessions.items()
                    if name.lower() == process.lower() for h in handles]

    def handle(self, process: str) -> FakeHandle:
        """The first session of `process`."""
        with self._lock:
            return self._sessions[process][0]

    def launch(self, process: str, volume: float) -> FakeHandle:
        """The process starts and opens a session at `volume`."""
        handle = FakeHandle(volume)
        with self._lock:
            self._sessions.setdefault(process, []).append(handle)
        return handle

    def quit(self, process: str) -> None:
        """The process exits: its sessions go, and their handles stop working."""
        with self._lock:
            for handle in self._sessions.pop(process, []):
                handle.gone = True


# -- recognition (Task 9; Task 11's service gets it as `recognizer`) ----------------------------

class FakeRecognizer:
    """stt.Recognizer without a model: each transcribe() hands back the next of `results` (a
    Transcript, or None for a press with no speech), then `default`, and records its audio
    (copied, as the caller may reuse the buffer) and hotwords in `calls`; `called` is set at
    the first call, for a test waiting on the recognizer thread. With `error` set, a call is
    recorded and then raises it. `name` is what status.stt reports."""

    def __init__(self, results=(), *, default=None, name: str = "parakeet"):
        self.name = name
        self.results = list(results)
        self.default = default
        self.error: Exception | None = None
        self.calls: list[tuple[np.ndarray, list[str]]] = []
        self.called = threading.Event()

    def transcribe(self, audio16k, hotwords):
        self.calls.append((np.array(audio16k, dtype=np.float32), list(hotwords)))
        self.called.set()
        if self.error is not None:
            raise self.error
        return self.results.pop(0) if self.results else self.default


# -- the doctor's machine (Task 11) -------------------------------------------------------------

class FakeProbes:
    """__main__.Probes on an invented machine: `devices` the WASAPI list, `owners` port -> the
    pid listening on it, `ducking` UserDuckingPreference (None when it is not set), `ollama`
    what /api/ps lists (name -> (size, size_vram); None when Ollama cannot be asked). With
    `device_error` set, reading the list raises it. `device_reads` counts the reads."""

    def __init__(self, *, devices=(), owners=None, ducking=3, ollama=None):
        self.ollama = ollama
        self._devices = list(devices)
        self.owners = dict(owners or {})
        self.ducking = ducking
        self.device_error: Exception | None = None
        self.device_reads = 0

    def devices(self) -> list[Dev]:
        self.device_reads += 1
        if self.device_error is not None:
            raise self.device_error
        return list(self._devices)

    def port_owner(self, port: int) -> int | None:
        return self.owners.get(port)

    def ducking_preference(self) -> int | None:
        return self.ducking

    def ollama_loaded(self):
        return self.ollama


# -- the voice UI (Task 10; Task 11's service is its UiHooks) -----------------------------------

def ui_sample_state() -> dict:
    """A full state() for the UI hooks, on the invented cast: Testy (guid 1) is in the Guild Hall
    with Arkon, who is speaking and turned down 3 dB; Belra is in the Raid and muted; Dunna has
    asked him into Private 1. A fresh dict each call."""
    return {
        "tree": [
            {"id": "guild_hall", "name": "Guild Hall", "parent": "", "locked": False,
             "capacity": 0},
            {"id": "raid", "name": "Raid", "parent": "", "locked": False, "capacity": 0},
            {"id": "raid_2", "name": "Raid 2", "parent": "raid", "locked": False, "capacity": 0},
            {"id": "officers", "name": "Officers", "parent": "", "locked": True, "capacity": 0},
            {"id": "private_1", "name": "Private 1", "parent": "", "locked": True,
             "capacity": 2},
        ],
        "where": {1: "guild_hall", 101: "guild_hall", 102: "raid"},
        "names": {1: "Testy", 101: "Arkon", 102: "Belra", 104: "Dunna"},
        "room": "guild_hall",
        "human": 1,
        "speaking": [101],
        "ptt": False,
        "volumes": {101: -3.0},
        "mutes": [102],
        "invites": [{"from": 104, "channel": "private_1"}],
        "transcript": [{"who": "Arkon", "text": "adds on the left", "ts": 1793404809000,
                        "kind": "say"}],
        "mind": True,
        "status": {"ok": True, "mic": "ok", "gpu": "none", "stt": "parakeet", "ui": True,
                   "queue": 0, "errors": []},
    }


class RecordingHooks:
    """UiHooks for tests. state() hands out a copy of `data` (what the test gave, or what it
    sets later; UiServer fills in what is missing); join, volume and mute are recorded in
    `calls` as (name, *arguments) tuples. A hook named in `raises` records its call and then
    raises that exception."""

    def __init__(self, **state):
        self.data: dict = dict(state)
        self.calls: list[tuple] = []
        self.state_calls = 0
        self.raises: dict[str, Exception] = {}

    def _fail(self, name: str) -> None:
        if name in self.raises:
            raise self.raises[name]

    def state(self) -> dict:
        self.state_calls += 1
        self._fail("state")
        return dict(self.data)

    def join(self, channel: str) -> None:
        self.calls.append(("join", channel))
        self._fail("join")

    def volume(self, guid: int, db: float) -> None:
        self.calls.append(("volume", guid, db))
        self._fail("volume")

    def mute(self, guid: int, on: bool) -> None:
        self.calls.append(("mute", guid, on))
        self._fail("mute")


class FakePage:
    """A browser page for UiServer: a WebSocket client that connects to /ws as the page does,
    and records every message it receives, parsed (a text frame is JSON; anything else is kept
    as it came). `origin` "same" is the page's own, http://127.0.0.1:<port>; None sends none.
    `host` is the Host header's (the TCP connection always goes to 127.0.0.1). A refused
    handshake raises websockets' InvalidStatus. After the connection ends, `close_code` is the
    close frame's."""

    def __init__(self, ws):
        self._ws = ws
        self.received: list = []
        self.raw: list = []  # the same frames as they came, before parsing
        self._queue: asyncio.Queue = asyncio.Queue()
        self.closed = asyncio.Event()
        self.close_code: int | None = None
        self._task = asyncio.create_task(self._read())

    @classmethod
    async def connect(cls, port: int, *, origin: str | None = "same",
                      host: str = "127.0.0.1") -> "FakePage":
        if origin == "same":
            origin = f"http://127.0.0.1:{port}"
        ws = await connect(f"ws://{host}:{port}/ws", host="127.0.0.1", port=port, origin=origin,
                           proxy=None, compression=None, open_timeout=5)
        return cls(ws)

    async def _read(self) -> None:
        try:
            async for raw in self._ws:
                msg = json.loads(raw) if isinstance(raw, str) else raw
                self.raw.append(raw)
                self.received.append(msg)
                self._queue.put_nowait(msg)
        except ConnectionClosed:
            pass
        finally:
            self.close_code = self._ws.close_code
            self.closed.set()

    async def next(self, timeout: float = 2.0):
        """The next message received and not yet taken."""
        return await asyncio.wait_for(self._queue.get(), timeout)

    async def send(self, msg) -> None:
        """A dict goes out as JSON text; a str or bytes as it is."""
        await self._ws.send(json.dumps(msg) if isinstance(msg, dict) else msg)

    async def wait_closed(self, timeout: float = 2.0) -> None:
        await asyncio.wait_for(self.closed.wait(), timeout)

    async def close(self) -> None:
        await self._ws.close()
        await asyncio.wait({self._task}, timeout=2.0)
