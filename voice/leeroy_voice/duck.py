"""WoW's ducking (draft section 4.4): while members talk, WoW's own audio session goes down by
duck_db in the Windows volume mixer (the per-program level, not the in-game sliders), and comes
back after. duck_db 0 is off.

- duck(True) and duck(False) return at once: the ducker's own thread moves WoW's level over
  RAMP_S in RAMP_STEPS steps, even in dB, and the latest call wins, so a restore that a new
  voice interrupts turns back down from where it is. The service calls duck(True) as a voice
  starts and duck(False) 400 ms after the last one ends (Task 11).
- The baseline is WoW's level as a ducking starts, written to the state file before the first
  change and removed once WoW is back at it. So a level he sets in the volume mixer between
  duckings is the next one's baseline.
- Windows gives a program its last level again when it next runs, so a WoW that quits while
  ducked would come back ducked for good. So the state file stays until WoW is found and put
  back: its level is the next ducking's baseline, and restore_after_crash() (at the start, after
  a crash, and whenever the service likes) puts a running WoW back at it. A file found at the
  start of a ducking is that baseline too, so a voice that starts before restore_after_crash()
  has run can never write the ducked level over it (review I1).
- level() reads WoW's level for the service's watch on it (M30); it blocks on COM, so call it
  off the event loop.
- close() puts WoW back at once and stops the thread.

SessionApi finds a program's audio sessions. PycawSessions is the real one: pycaw, with every
COM call on a thread of its own, where a Handle's COM object lives and is released (the handle
holds only a key). Tests use FakeSessions (tests/fakes.py). pycaw, comtypes and psutil are
imported only on that thread (M9)."""
import itertools
import json
import logging
import math
import os
import threading
import time
import weakref
from pathlib import Path
from typing import Callable, Protocol

from .audio_io import COM_TIMEOUT_S, ComThread
from .logs import log

PROCESS = "Wow.exe"
RAMP_S = 0.150
RAMP_STEPS = 10
_SAME = 1e-4  # levels this close are the same level
_EXPIRED = 2  # AudioSessionStateExpired: a session whose program has gone


class SessionError(Exception):
    """A session that cannot be read or set: its program quit, or COM did not answer."""


class Handle(Protocol):
    def get(self) -> float:
        """The session's level in the volume mixer, 0-1. Raises SessionError."""

    def set(self, v: float) -> None:
        """Raises SessionError."""


class SessionApi(Protocol):
    def find(self, process: str) -> list[Handle]:
        """The sessions of every running `process` (its name matched without regard to case)
        on every active output; [] when it is not running."""


class _PycawHandle:
    def __init__(self, sessions: "PycawSessions", key: int):
        self._sessions = sessions
        self._key = key
        weakref.finalize(self, sessions._release, key).atexit = False

    def get(self) -> float:
        return self._sessions._call(self._sessions._get, self._key)

    def set(self, v: float) -> None:
        self._sessions._call(self._sessions._set, self._key, min(1.0, max(0.0, float(v))))


class PycawSessions:
    """SessionApi on Windows' audio sessions, through pycaw on a COM thread of its own."""

    def __init__(self, *, timeout_s: float = COM_TIMEOUT_S):
        self._com = ComThread("leeroy-voice-sessions")
        self._timeout = timeout_s
        self._volumes: dict[int, object] = {}  # key -> ISimpleAudioVolume, on the COM thread
        self._keys = itertools.count(1)

    def find(self, process: str) -> list[Handle]:
        return [_PycawHandle(self, key) for key in self._call(self._find, process)]

    def close(self) -> None:
        try:
            self._com.call(self._volumes.clear, timeout=self._timeout)
        except Exception:
            pass
        self._com.close()

    def _call(self, fn, *args):
        try:
            return self._com.call(fn, *args, timeout=self._timeout)
        except SessionError:
            raise
        except Exception as e:
            raise SessionError(f"the session call failed: {type(e).__name__}") from None

    def _release(self, key: int) -> None:
        self._com.post(self._volumes.pop, key, None)

    # on the COM thread

    def _find(self, process: str) -> list[int]:
        import comtypes
        import psutil
        from pycaw.api.audioclient import ISimpleAudioVolume
        from pycaw.api.audiopolicy import IAudioSessionControl2, IAudioSessionManager2
        from pycaw.api.mmdeviceapi import IMMDeviceEnumerator
        from pycaw.constants import DEVICE_STATE, CLSID_MMDeviceEnumerator, EDataFlow
        enumerator = comtypes.CoCreateInstance(CLSID_MMDeviceEnumerator, IMMDeviceEnumerator,
                                               comtypes.CLSCTX_INPROC_SERVER)
        endpoints = enumerator.EnumAudioEndpoints(EDataFlow.eRender.value,
                                                  DEVICE_STATE.ACTIVE.value)
        want, keys = process.lower(), []
        for i in range(endpoints.GetCount()):
            try:
                manager = endpoints.Item(i).Activate(
                    IAudioSessionManager2._iid_, comtypes.CLSCTX_ALL, None
                ).QueryInterface(IAudioSessionManager2)
                sessions = manager.GetSessionEnumerator()
                count = sessions.GetCount()
            except Exception:  # an endpoint going away as we look
                continue
            for j in range(count):
                try:
                    control = sessions.GetSession(j).QueryInterface(IAudioSessionControl2)
                    if control.GetState() == _EXPIRED:
                        continue
                    pid = control.GetProcessId()
                    if not pid or psutil.Process(pid).name().lower() != want:
                        continue
                    volume = control.QueryInterface(ISimpleAudioVolume)
                except Exception:  # a session ending, or its program gone, as we look
                    continue
                key = next(self._keys)
                self._volumes[key] = volume
                keys.append(key)
        return keys

    def _get(self, key: int) -> float:
        volume = self._volumes.get(key)
        if volume is None:
            raise SessionError("the session handle was released")
        return float(volume.GetMasterVolume())

    def _set(self, key: int, v: float) -> None:
        volume = self._volumes.get(key)
        if volume is None:
            raise SessionError("the session handle was released")
        volume.SetMasterVolume(v, None)


def _between(a: float, b: float, f: float) -> float:
    """The level a fraction f of the way from a to b, even in dB."""
    return a * (b / a) ** f if a > 0 and b > 0 else a + (b - a) * f


class WowDucker:
    """See the module's docstring. `sleep` is the pause between steps (tests replace it)."""

    def __init__(self, sessions: SessionApi, state_file: Path, *, duck_db: float,
                 process: str = PROCESS, sleep: Callable[[float], None] = time.sleep):
        if type(duck_db) not in (int, float) or not math.isfinite(duck_db) or duck_db < 0:
            raise ValueError(f"duck_db: expected 0 dB or more, got {duck_db!r}")
        self._sessions = sessions
        self._file = Path(state_file)
        self._factor = 10 ** (-duck_db / 20)
        self._on = duck_db > 0
        self._process = process
        self._sleep = sleep
        self._cond = threading.Condition()
        self._want = False
        self._asked = 0  # calls made ...
        self._done = 0  # ... and carried out
        self._closing = False
        self._thread: threading.Thread | None = None
        self._work = threading.Lock()  # the sessions and the state below: one user at a time
        self._handles: list[Handle] = []  # WoW's sessions while it is ducked
        self._baseline: float | None = None  # the level to come back to, while ducked
        self._level: float | None = None  # the level last set
        self._pending: float | None = None  # a level WoW is still to be put back at

    def duck(self, on: bool) -> None:
        if not self._on:
            return
        with self._cond:
            if self._closing:
                return
            self._want = bool(on)
            self._asked += 1
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="leeroy-voice-duck",
                                                daemon=True)
                self._thread.start()
            self._cond.notify_all()

    def wait(self, timeout: float | None = None) -> bool:
        """Whether every call so far has been carried out, within `timeout`."""
        with self._cond:
            return self._cond.wait_for(lambda: self._done == self._asked, timeout)

    def level(self) -> float | None:
        """WoW's level as it stands, or None: WoW is not running, it cannot be read, or a
        ducking owns the level (a call not yet carried out included)."""
        with self._cond:
            if self._done != self._asked:
                return None
        with self._work:
            if self._baseline is not None:
                return None
            handles = self._find()
            try:
                return handles[0].get() if handles else None
            except SessionError:
                return None

    def restore_after_crash(self) -> None:
        with self._work:
            if self._baseline is not None:  # a ducking in progress owns the state file
                return
            saved = self._read()
            if saved is not None:
                self._baseline = saved
                self._finish()

    def close(self) -> None:
        with self._cond:
            if self._closing:
                return
            self._closing = True
            thread = self._thread
            self._cond.notify_all()
        if thread is not None:
            thread.join(RAMP_S + 2 * COM_TIMEOUT_S)
        with self._work:
            if self._baseline is not None:
                self._set_all(self._baseline)
                self._finish()
        with self._cond:
            self._done = self._asked
            self._cond.notify_all()

    # the thread

    def _run(self) -> None:
        while True:
            with self._cond:
                self._cond.wait_for(lambda: self._done != self._asked or self._closing)
                if self._closing:
                    return
                asked, want = self._asked, self._want
            with self._work:
                try:
                    if want:
                        self._down(asked)
                    elif self._baseline is not None and self._ramp(self._baseline, asked):
                        self._finish()
                except Exception:  # never let the thread die: the next call tries again
                    log("wow_duck_failed", level=logging.WARNING, exc_info=True)
            with self._cond:
                if self._asked == asked:
                    self._done = asked
                self._cond.notify_all()

    def _down(self, asked: int) -> None:
        if self._baseline is None:  # a ducking starts
            handles = self._find()
            if not handles:
                return
            try:
                level = handles[0].get()
            except SessionError:
                log("wow_session_failed", level=logging.WARNING)
                return
            if self._pending is None:  # a file a crash left is the truth (a damaged one goes)
                self._pending = self._read()
            baseline = self._pending if self._pending is not None else level
            self._write(baseline)  # before the first change
            self._handles, self._baseline, self._level = handles, baseline, level
            self._pending = None
            log("wow_ducked", level=logging.DEBUG, baseline=round(baseline, 4))
        self._ramp(self._baseline * self._factor, asked)

    def _ramp(self, target: float, asked: int) -> bool:
        """WoW's sessions to `target`; False when a newer call, or close(), interrupts it."""
        start = self._level
        if start is None or not self._handles or abs(start - target) < _SAME:
            return True
        for i in range(1, RAMP_STEPS + 1):
            self._sleep(RAMP_S / RAMP_STEPS)
            with self._cond:
                if self._asked != asked or self._closing:
                    return False
            self._set_all(target if i == RAMP_STEPS else _between(start, target, i / RAMP_STEPS))
            if not self._handles:  # WoW quit: nothing left to move
                return True
        return True

    def _set_all(self, level: float) -> None:
        alive = []
        for handle in self._handles:
            try:
                handle.set(level)
                alive.append(handle)
            except SessionError:
                log("wow_session_gone", level=logging.DEBUG)
        self._handles, self._level = alive, level

    def _finish(self) -> None:
        """A ducking's end, or a crash's restore: every WoW session running now goes to the
        baseline (a WoW that ran again came back at the level it quit with), and the state file
        goes once WoW is back at it; until then it stays, as the next ducking's baseline."""
        baseline, back = self._baseline, False
        for handle in self._find():
            try:
                if abs(handle.get() - baseline) > _SAME:
                    handle.set(baseline)
                back = True
            except SessionError:
                pass
        if back:
            self._remove()
            self._pending = None
            log("wow_restored", level=logging.DEBUG, baseline=round(baseline, 4))
        else:
            self._pending = baseline
            log("wow_restore_pending", baseline=round(baseline, 4))
        self._handles, self._baseline, self._level = [], None, None

    def _find(self) -> list[Handle]:
        try:
            return list(self._sessions.find(self._process))
        except Exception as e:
            log("wow_find_failed", level=logging.WARNING, error=type(e).__name__)
            return []

    def _read(self) -> float | None:
        try:
            text = self._file.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as e:
            log("wow_state_unreadable", level=logging.WARNING, error=type(e).__name__)
            return None
        try:
            v = json.loads(text)["volume"]
            good = type(v) in (int, float) and math.isfinite(v) and 0 <= v <= 1
        except (ValueError, TypeError, KeyError, IndexError):
            good = False
        if not good:
            log("wow_state_unreadable", level=logging.WARNING, error="not a volume")
            self._remove()
            return None
        return float(v)

    def _write(self, v: float) -> None:
        self._file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._file.with_name(self._file.name + ".tmp")
        tmp.write_text(json.dumps({"volume": v}) + "\n", encoding="utf-8")
        os.replace(tmp, self._file)

    def _remove(self) -> None:
        try:
            self._file.unlink(missing_ok=True)
        except OSError as e:
            log("wow_state_unremovable", level=logging.WARNING, error=type(e).__name__)
