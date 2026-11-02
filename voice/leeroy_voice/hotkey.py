"""Push-to-talk's key (draft section 6.1): `\\` by default, through Raw Input, never swallowed,
and the gate that turns it into presses (I2).

PttMachine owns the gate and is pure (no Win32, no thread, no clock of its own): key() gets
Raw Input's events, tick() runs every few tens of milliseconds, and watch() gets
GetAsyncKeyState every 20 ms while the key is held, so a missed key-up never leaves the mic
open. Nothing downstream (the capture, the `ptt` message, the ducking, the recognizer) sees a
key-down before its Press, which comes once the key has been held min_press_ms: a backslash
typed into WoW chat is a Tap, which reaches nothing (Review Focus 1). The service hands a Press
to the scheduler as set_ptt(True) and a Release as set_ptt(False); the hold itself (p2-p4
lines from the model wait, a template line never does, PTT_GRACE_S after key-up) is the
scheduler's own (Task 6, settled item 13).

RawInputKeys is the key itself: a hidden top-level window on a thread of its own, registered
for the keyboard's raw input with RIDEV_INPUTSINK, so it hears the key while WoW has focus.
Raw Input only reads: it cannot swallow a key, and it has no hook for Windows to time out. Its
events reach the event loop through call_soon_threadsafe, and are ignored there while it is
not armed. The window lives for the service's whole life, and turns the WM_CLOSE a plain
taskkill posts into on_close (M7). One per process: Windows sends a process's raw keyboard
input to one window.

key_pressed() reads GetAsyncKeyState; wow_elevated() and self_elevated() read the processes'
tokens (a non-elevated listener hears nothing while an elevated window has focus);
foreground_process() names the program in front; send_key() injects a key for the live tests.
The Win32 parts import leeroy_voice.winapi when called, never at import, so PttMachine and
RawInputKeys import on Linux (CI)."""
import asyncio
import ctypes
import importlib
import itertools
import logging
import math
import threading
from dataclasses import dataclass
from typing import Callable

from .config import BACKSLASH_VK
from .logs import log

RI_KEY_BREAK = 0x01  # RAWKEYBOARD.Flags: the key went up
GATE_EPS_S = 1e-6  # a time this close to the gate or the cap is at it
START_TIMEOUT_S = 5.0
STOP_TIMEOUT_S = 2.0
_CLASS_SERIAL = itertools.count(1)


@dataclass(frozen=True, slots=True)
class Press:
    """The key has been held min_press_ms: push-to-talk begins. `at` is the key-down's time."""
    at: float


@dataclass(frozen=True, slots=True)
class Release:
    """A press is over: the key came up at `at`, or the hold reached max_hold_s (`capped`)."""
    at: float
    held_ms: int
    capped: bool = False


@dataclass(frozen=True, slots=True)
class Tap:
    """The key came up before its Press: typing, not talking. Nothing downstream hears of it."""
    held_ms: int


def raw_key_down(vkey: int, flags: int, vk: int) -> bool | None:
    """One WM_INPUT from the keyboard: True for `vk` going down (a first press or Raw Input's
    auto-repeat), False for it going up, None for any other key."""
    if vkey != vk:
        return None
    return not flags & RI_KEY_BREAK


class PttMachine:
    """The key's state, from Raw Input's events, the ticks and the watchdog's reads. The times
    are the clock's seconds.

    - key(True) starts a hold; its auto-repeat changes nothing. tick() gives the Press once the
      key has been held min_press_ms, dated at the key-down.
    - key(False) ends it: a Release after the Press, a Tap before it, whatever its length (no
      tick came between the gate and the key-up, so nothing heard of a press).
    - watch(pressed) is GetAsyncKeyState while the key is held. One read of the key up may race
      a key-up still on its way, so two in a row end the hold, dated at the first; a read of
      the key down starts the count again. While one read says up, no Press is given.
    - A hold that reaches max_hold_s is Released (capped), dated at the cap. The key may still
      be down: nothing more comes of it until it is up, by key(False) or the watchdog.

    `held`: the key is down as far as the machine knows, from the key-down until its Tap, its
    Release, or after a cap its key-up: the watchdog's reads are wanted while it is."""

    def __init__(self, *, min_press_ms: int = 150, max_hold_s: float = 30.0):
        if type(min_press_ms) is not int or min_press_ms < 1:
            raise ValueError(f"min_press_ms must be a whole number of ms, 1 or more: "
                             f"{min_press_ms!r}")
        if (isinstance(max_hold_s, bool) or not isinstance(max_hold_s, (int, float))
                or not math.isfinite(max_hold_s) or max_hold_s * 1000 <= min_press_ms):
            raise ValueError(f"max_hold_s must be longer than min_press_ms: {max_hold_s!r}")
        self._gate = min_press_ms / 1000
        self._cap = float(max_hold_s)
        self._down: float | None = None  # the key-down of the hold, None while the key is up
        self._pressed = False  # its Press was given
        self._capped = False  # its Release was given at the cap
        self._up_read: float | None = None  # the watchdog's first read of the key up

    @property
    def held(self) -> bool:
        return self._down is not None

    def key(self, down: bool, now: float) -> Release | Tap | None:
        if down:
            if self._down is None:
                self._down, self._pressed, self._capped = now, False, False
            self._up_read = None  # down after all
            return None
        if self._down is None:
            return None  # a stray key-up: its key-down came before arming, or was handled
        at = now if self._up_read is None else min(self._up_read, now)
        return self._end(at)

    def tick(self, now: float) -> Press | Release | None:
        down = self._down
        if down is None or self._capped:
            return None
        if not self._pressed:
            if self._up_read is None and now >= down + self._gate - GATE_EPS_S:
                self._pressed = True
                return Press(at=down)
            return None
        if now >= down + self._cap - GATE_EPS_S:
            self._capped = True
            return Release(at=down + self._cap, held_ms=round(self._cap * 1000), capped=True)
        return None

    def watch(self, pressed: bool, now: float) -> Release | Tap | None:
        if self._down is None:
            return None
        if pressed:
            self._up_read = None
            return None
        if self._up_read is None:
            self._up_read = now
            return None
        return self._end(self._up_read)

    def _end(self, at: float) -> Release | Tap | None:
        down, pressed, capped = self._down, self._pressed, self._capped
        self._down, self._pressed, self._capped, self._up_read = None, False, False, None
        if capped:
            return None  # its Release went out at the cap
        held_ms = round(max(0.0, at - down) * 1000)
        return Release(at=at, held_ms=held_ms) if pressed else Tap(held_ms=held_ms)


def _win32():
    """leeroy_voice.winapi, imported here only: the voice suite lets `windows` tests alone
    import it."""
    return importlib.import_module("leeroy_voice.winapi")


class RawInputKeys:
    """The key `vk` through Raw Input: on_key(down) on the event loop for each of its events
    (Raw Input's auto-repeat included) while `armed`, which is off until the session arms it;
    on_close() when the window gets WM_CLOSE. start() opens the window from a coroutine, whose
    loop then receives the events; stop() posts WM_QUIT to the window's thread and waits for it.
    pressed() is GetAsyncKeyState for `vk`, the watchdog's read. `hwnd` is the window, 0 while
    there is none."""

    def __init__(self, vk: int, on_key: Callable[[bool], None], *,
                 on_close: Callable[[], None]):
        if type(vk) is not int or not 0x01 <= vk <= 0xFE:
            raise ValueError(f"a virtual-key code is 0x01-0xFE, not {vk!r}")
        self.vk = vk
        self._on_key = on_key
        self._on_close = on_close
        self.armed = False
        self.hwnd = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._error: BaseException | None = None
        self._w = None
        self._raw = None
        self._failures = 0  # window procedure errors, on the window's thread

    def start(self) -> None:
        """Raises OSError when Windows refuses the window or the registration, and
        RuntimeError outside a running event loop."""
        if self._thread is not None:
            return
        w = _win32()
        self._loop = asyncio.get_running_loop()
        self._w, self._raw, self._error = w, w.RAWINPUT(), None
        ready = threading.Event()
        thread = threading.Thread(target=self._run, args=(ready,), name="leeroy-voice-keys",
                                  daemon=True)
        thread.start()
        if not ready.wait(START_TIMEOUT_S):
            raise OSError("the key's window did not come up")
        if self._error is not None:
            thread.join(STOP_TIMEOUT_S)
            raise self._error
        self._thread = thread
        log("ptt_keys_started", vk=f"0x{self.vk:02x}")

    def stop(self) -> None:
        thread, self._thread = self._thread, None
        if thread is None:
            return
        self._w.user32.PostThreadMessageW(self._thread_id, self._w.WM_QUIT, 0, 0)
        thread.join(STOP_TIMEOUT_S)
        if thread.is_alive():
            log("ptt_keys_stuck", level=logging.WARNING)
        elif self._failures:
            log("ptt_keys_failures", level=logging.WARNING, count=self._failures)

    def pressed(self) -> bool:
        return key_pressed(self.vk)

    # on the window's thread

    def _run(self, ready: threading.Event) -> None:
        w = self._w
        name = f"LeeroyVoiceKeys{next(_CLASS_SERIAL)}"
        hinstance = w.kernel32.GetModuleHandleW(None)
        proc = w.WNDPROC(self._window_proc)  # kept alive for as long as the window
        wc = w.WNDCLASSEXW(cbSize=ctypes.sizeof(w.WNDCLASSEXW), lpfnWndProc=proc,
                           hInstance=hinstance, lpszClassName=name)
        registered = hwnd = listening = None
        try:
            try:
                registered = w.user32.RegisterClassExW(ctypes.byref(wc)) or _fail()
                hwnd = w.user32.CreateWindowExW(w.WS_EX_TOOLWINDOW, name, "leeroy-voice", 0,
                                                0, 0, 0, 0, None, None, hinstance,
                                                None) or _fail()
                device = w.RAWINPUTDEVICE(w.HID_USAGE_PAGE_GENERIC,
                                          w.HID_USAGE_GENERIC_KEYBOARD, w.RIDEV_INPUTSINK, hwnd)
                listening = w.user32.RegisterRawInputDevices(
                    ctypes.byref(device), 1, ctypes.sizeof(device)) or _fail()
                self._thread_id = w.kernel32.GetCurrentThreadId()
                self.hwnd = hwnd
            except BaseException as e:
                self._error = e
                return
            finally:
                ready.set()
            msg = w.MSG()
            while (got := w.user32.GetMessageW(ctypes.byref(msg), None, 0, 0)) > 0:
                w.user32.DispatchMessageW(ctypes.byref(msg))
            if got < 0:  # 0 is WM_QUIT, from stop()
                log("ptt_keys_loop_failed", level=logging.ERROR, error=ctypes.get_last_error())
        finally:
            if listening:
                gone = w.RAWINPUTDEVICE(w.HID_USAGE_PAGE_GENERIC, w.HID_USAGE_GENERIC_KEYBOARD,
                                        w.RIDEV_REMOVE, None)
                w.user32.RegisterRawInputDevices(ctypes.byref(gone), 1, ctypes.sizeof(gone))
            if hwnd:
                w.user32.DestroyWindow(hwnd)
            if registered:
                w.user32.UnregisterClassW(name, hinstance)
            self.hwnd = 0

    def _window_proc(self, hwnd, msg, wparam, lparam):
        w = self._w
        if msg == w.WM_CLOSE:
            self._guarded(self._post, self._on_close)
            return 0  # never DefWindowProc, which destroys the window: the service stops
        if msg == w.WM_INPUT:
            self._guarded(self._raw_input, lparam)
        return w.user32.DefWindowProcW(hwnd, msg, wparam, lparam)  # WM_INPUT's cleanup too

    def _guarded(self, fn, *args) -> None:
        try:
            fn(*args)
        except Exception:  # an error escaping a window procedure is lost: count it instead
            self._failures += 1

    def _raw_input(self, lparam) -> None:
        w, raw = self._w, self._raw
        size = w.wintypes.UINT(ctypes.sizeof(raw))
        got = w.user32.GetRawInputData(lparam, w.RID_INPUT, ctypes.byref(raw), ctypes.byref(size),
                                       ctypes.sizeof(w.RAWINPUTHEADER))
        if got == w.GETRAWINPUTDATA_FAILED or got < (ctypes.sizeof(w.RAWINPUTHEADER)
                                                     + ctypes.sizeof(w.RAWKEYBOARD)):
            return
        if raw.header.dwType != w.RIM_TYPEKEYBOARD:
            return
        down = raw_key_down(raw.data.keyboard.VKey, raw.data.keyboard.Flags, self.vk)
        if down is not None:
            self._post(self._deliver, down)

    def _post(self, fn, *args) -> None:
        try:
            self._loop.call_soon_threadsafe(fn, *args)
        except RuntimeError:  # the loop has closed: the service is going
            pass

    # on the event loop

    def _deliver(self, down: bool) -> None:
        if self.armed:
            self._on_key(down)


def _fail():
    raise ctypes.WinError(ctypes.get_last_error())


def key_pressed(vk: int) -> bool:
    """Whether Windows has the key down now (GetAsyncKeyState), for the watchdog."""
    w = _win32()
    return bool(w.user32.GetAsyncKeyState(vk) & w.KEY_DOWN_BIT)


def send_key(vk: int, down: bool) -> None:
    """Inject the key going down or up, for the live tests: F24 (0x87), never backslash.
    Raises OSError when Windows refused it."""
    if vk == BACKSLASH_VK:
        raise ValueError("backslash is his push-to-talk key: it is never injected")
    w = _win32()
    event = w.INPUT(type=w.INPUT_KEYBOARD)
    event.ki = w.KEYBDINPUT(wVk=vk, wScan=0, dwFlags=0 if down else w.KEYEVENTF_KEYUP,
                            time=0, dwExtraInfo=0)
    if w.user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(event)) != 1:
        _fail()


def _token_elevated(w, process) -> bool:
    """Whether a process's token is elevated; one that cannot be read counts as elevated."""
    token = w.wintypes.HANDLE()
    if not w.advapi32.OpenProcessToken(process, w.TOKEN_QUERY, ctypes.byref(token)):
        if ctypes.get_last_error() == w.ERROR_ACCESS_DENIED:
            return True
        _fail()
    try:
        elevation = w.TOKEN_ELEVATION()
        size = w.wintypes.DWORD()
        if not w.advapi32.GetTokenInformation(token, w.TOKEN_ELEVATION_CLASS,
                                              ctypes.byref(elevation),
                                              ctypes.sizeof(elevation), ctypes.byref(size)):
            if ctypes.get_last_error() == w.ERROR_ACCESS_DENIED:
                return True
            _fail()
        return bool(elevation.TokenIsElevated)
    finally:
        w.kernel32.CloseHandle(token)


def self_elevated() -> bool:
    """Whether this process runs as administrator (it should not: WoW does not)."""
    w = _win32()
    return _token_elevated(w, w.kernel32.GetCurrentProcess())


def foreground_process() -> str:
    """The foreground window's program, its name only: "Wow.exe". "" when there is none (a
    secure desktop) or it has gone. Task 23 reads this to confirm Raw Input with WoW focused."""
    w = _win32()
    hwnd = w.user32.GetForegroundWindow()
    if not hwnd:
        return ""
    pid = w.wintypes.DWORD()
    if not w.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid)):
        return ""
    import psutil
    try:
        return psutil.Process(pid.value).name()
    except psutil.Error:
        return ""


def wow_elevated(process: str = "Wow.exe") -> bool | None:
    """Whether WoW runs elevated, so that a non-elevated listener hears nothing while it has
    focus; None when it is not running. A WoW whose token cannot be opened counts as elevated.
    Raises OSError for any other failure."""
    w = _win32()
    import psutil
    want, found = process.lower(), None
    for p in psutil.process_iter(["name"]):
        if (p.info.get("name") or "").lower() != want:
            continue
        handle = w.kernel32.OpenProcess(w.PROCESS_QUERY_LIMITED_INFORMATION, False, p.pid)
        if not handle:
            error = ctypes.get_last_error()
            if error == w.ERROR_ACCESS_DENIED:
                return True
            if error == w.ERROR_INVALID_PARAMETER:
                continue  # it has exited meanwhile
            _fail()
        try:
            if _token_elevated(w, handle):
                return True
        finally:
            w.kernel32.CloseHandle(handle)
        found = False
    return found
