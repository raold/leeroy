"""Raw Input and the token checks on the real Windows (Task 8, marked `windows`; deselected by
default). Run locally with `pytest -q -m "windows or device" tests/test_rawinput_live.py`. It
injects F24 (0x87), a key no keyboard has, and never backslash: the injected key goes to the
foreground window, and nothing binds it. Nothing is written to disk."""
import asyncio
import ctypes
import os

import pytest

from conftest import run

pytestmark = pytest.mark.windows
F24 = 0x87


async def until(predicate, timeout: float = 2.0) -> bool:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not predicate():
        if loop.time() > end:
            return False
        await asyncio.sleep(0.01)
    return True


def top_level_windows(w, pid: int) -> set[int]:
    """The process's top-level windows, as a plain taskkill finds them (EnumWindows never lists
    a message-only window)."""
    found: set[int] = set()

    def each(hwnd, _):
        owner = w.wintypes.DWORD()
        w.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid:
            found.add(hwnd)
        return True
    w.user32.EnumWindows(w.WNDENUMPROC(each), 0)
    return found


def test_the_structures_have_their_windows_sizes():
    from leeroy_voice import winapi as w
    if ctypes.sizeof(ctypes.c_void_p) != 8:
        pytest.skip("64-bit Python only")
    sizes = {w.RAWINPUTHEADER: 24, w.RAWKEYBOARD: 16, w.RAWINPUT: 48, w.RAWINPUTDEVICE: 16,
             w.INPUT: 40, w.MSG: 48, w.WNDCLASSEXW: 80, w.TOKEN_ELEVATION: 4}
    assert {s.__name__: ctypes.sizeof(s) for s in sizes} == {
        s.__name__: n for s, n in sizes.items()}


def test_raw_input_hears_the_key_while_armed():
    """Armed, F24 down and up reach on_key as [True, False], and GetAsyncKeyState (the
    watchdog's read) follows the key; disarmed, nothing; a WM_CLOSE posted to the window (what
    a plain taskkill posts) calls on_close and leaves the window; stop() takes it down."""
    from leeroy_voice import hotkey
    from leeroy_voice import winapi as w
    got, closed = [], []

    async def main():
        keys = hotkey.RawInputKeys(F24, got.append, on_close=lambda: closed.append(1))
        keys.start()
        hwnd = keys.hwnd
        try:
            assert hwnd and hwnd in top_level_windows(w, os.getpid())
            assert not w.user32.IsWindowVisible(hwnd)
            keys.armed = True
            hotkey.send_key(F24, True)
            assert await until(lambda: hotkey.key_pressed(F24) and keys.pressed())
            hotkey.send_key(F24, False)
            assert await until(lambda: len(got) >= 2), got
            assert got == [True, False]
            assert await until(lambda: not keys.pressed())
            keys.armed = False
            hotkey.send_key(F24, True)
            hotkey.send_key(F24, False)
            await asyncio.sleep(0.5)
            assert got == [True, False]
            keys.armed = True
            hotkey.send_key(F24, True)
            hotkey.send_key(F24, False)
            assert await until(lambda: len(got) >= 4), got
            assert got == [True, False, True, False]
            assert w.user32.PostMessageW(hwnd, w.WM_CLOSE, 0, 0)
            assert await until(lambda: closed)
            assert closed == [1] and w.user32.IsWindow(hwnd)
        finally:
            keys.stop()
        assert not w.user32.IsWindow(hwnd)
    run(main())


def test_backslash_is_never_injected():
    from leeroy_voice import hotkey
    with pytest.raises(ValueError):
        hotkey.send_key(0xDC, True)


def test_the_foreground_process_is_named():
    """Only reads: the name of the program in front, whatever it is."""
    from leeroy_voice.hotkey import foreground_process
    name = foreground_process()
    assert isinstance(name, str) and "\\" not in name


def test_elevation():
    """This process runs as the owner, not as administrator; WoW, if it is running, is one or
    the other."""
    pytest.importorskip("psutil")
    from leeroy_voice import hotkey
    assert hotkey.self_elevated() is False
    assert hotkey.wow_elevated() in (None, True, False)
