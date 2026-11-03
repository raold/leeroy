"""Push-to-talk (Task 8): PttMachine's 150 ms gate (I2), auto-repeat, the watchdog and the hold
cap; its presses into the scheduler's own hold; RawInputKeys' decoding and its armed switch, and
FakeKeys. Nothing here hooks a key or calls Win32: leeroy_voice.winapi is guarded under
`windows`, and test_rawinput_live.py has the real thing."""
import random
import sys
from datetime import datetime, timezone

import numpy as np
import pytest

from fakes import FakeClock, FakeKeys
from leeroy_voice.hotkey import (RI_KEY_BREAK, Press, PttMachine, RawInputKeys, Release, Tap,
                                 raw_key_down)
from leeroy_voice.schedule import PTT_GRACE_S, PTT_DUCK_DB, Line, PlaybackScheduler

START = datetime(2026, 10, 31, 21, 0, tzinfo=timezone.utc)
T = START.timestamp()  # a real clock's size, where float sums round
F24 = 0x87


def test_press_and_release():
    """I2: nothing at key-down; the Press 150 ms after it, dated at the key-down."""
    m = PttMachine()
    assert m.key(True, T) is None and m.held
    assert m.tick(T + 0.149) is None
    assert m.tick(T + 0.15) == Press(at=T)
    assert m.tick(T + 0.5) is None
    assert m.key(False, T + 0.8) == Release(at=T + 0.8, held_ms=800, capped=False)
    assert not m.held and m.tick(T + 1.0) is None


def test_auto_repeat_ignored():
    """Raw Input repeats the key-down while the key is held: only the first counts."""
    m = PttMachine()
    for dt in (0.0, 0.03, 0.06):
        assert m.key(True, T + dt) is None
    events = [e for dt in (0.1, 0.149, 0.15, 0.17, 0.3, 1.0) if (e := m.tick(T + dt))]
    assert events == [Press(at=T)]
    assert m.key(True, T + 1.03) is None and m.tick(T + 1.1) is None


def test_tap_ignored():
    """Review Focus 1: a backslash typed into WoW chat is a Tap, and no tick before or after it
    ever yields a Press."""
    m = PttMachine()
    assert m.key(True, T) is None
    assert m.tick(T + 0.05) is None and m.tick(T + 0.1) is None
    assert m.key(False, T + 0.12) == Tap(held_ms=120)
    assert all(m.tick(T + dt) is None for dt in (0.15, 0.2, 1.0, 31.0))
    assert not m.held


def test_a_key_up_before_its_press_is_a_tap():
    """No tick came between the gate and the key-up: nothing downstream saw a Press, so the
    key-up cannot end one, whatever its length."""
    m = PttMachine()
    m.key(True, T)
    assert m.key(False, T + 0.18) == Tap(held_ms=180)
    assert m.tick(T + 0.2) is None


def test_watchdog_closes_a_missed_release():
    """The draft: GetAsyncKeyState every 20 ms while the key is held. One read of the key up
    may race a key-up still on its way, so two in a row close the press, dated at the first."""
    m = PttMachine()
    m.key(True, T)
    assert m.tick(T + 0.15) == Press(at=T)
    up = T + 0.5  # the key came up here, and Raw Input's key-up never arrived
    assert m.watch(True, up - 0.02) is None
    assert m.watch(False, up + 0.02) is None
    assert m.watch(False, up + 0.04) == Release(at=up + 0.02, held_ms=520, capped=False)
    assert not m.held
    assert m.key(False, up + 0.05) is None  # the late key-up changes nothing


def test_a_down_read_restarts_the_watchdog():
    m = PttMachine()
    m.key(True, T)
    m.tick(T + 0.15)
    assert m.watch(False, T + 0.2) is None
    assert m.watch(True, T + 0.22) is None  # still down after all
    assert m.watch(False, T + 0.24) is None
    assert m.watch(False, T + 0.26) == Release(at=T + 0.24, held_ms=240, capped=False)


def test_an_auto_repeat_restarts_the_watchdog():
    """Raw Input's auto-repeat says the key is still down: an up read before it no longer
    counts."""
    m = PttMachine()
    m.key(True, T)
    m.tick(T + 0.15)
    assert m.watch(False, T + 0.2) is None
    assert m.key(True, T + 0.21) is None
    assert m.watch(False, T + 0.24) is None
    assert m.watch(False, T + 0.26) == Release(at=T + 0.24, held_ms=240, capped=False)


def test_a_key_up_after_one_up_read_is_dated_at_the_read():
    m = PttMachine()
    m.key(True, T)
    m.tick(T + 0.15)
    assert m.watch(False, T + 0.6) is None
    assert m.key(False, T + 0.603) == Release(at=T + 0.6, held_ms=600, capped=False)


def test_the_watchdog_in_the_gate_gives_a_tap():
    """A missed key-up within the gate: still a tap, and a pending up read holds the Press
    back, so a tap with a lost key-up never ducks anything either."""
    m = PttMachine()
    m.key(True, T)
    assert m.watch(False, T + 0.05) is None
    assert m.watch(False, T + 0.07) == Tap(held_ms=50)
    assert m.tick(T + 0.2) is None and not m.held
    m.key(True, T + 1.0)
    assert m.watch(False, T + 1.14) is None
    assert m.tick(T + 1.15) is None  # one read said up: no Press on it
    assert m.key(False, T + 1.151) == Tap(held_ms=140)


def test_watch_while_the_key_is_up_does_nothing():
    m = PttMachine()
    assert m.watch(False, T) is None and m.watch(False, T + 0.02) is None
    assert m.key(False, T + 0.03) is None  # a stray key-up: its key-down came before arming
    assert not m.held and m.tick(T + 1.0) is None
    m.key(True, T + 2.0)  # and the next press is a press
    assert m.tick(T + 2.15) == Press(at=T + 2.0)


def test_hold_capped():
    """A hold is closed at max_hold_s and transcribed; the key still down after it starts no
    new press, and its key-up ends nothing."""
    m = PttMachine()
    m.key(True, T)
    assert m.tick(T + 0.15) == Press(at=T)
    assert m.tick(T + 29.99) is None
    assert m.tick(T + 30.0) == Release(at=T + 30.0, held_ms=30000, capped=True)
    assert m.held  # the key itself is still down
    assert m.key(True, T + 30.03) is None and m.tick(T + 31.0) is None
    assert m.key(False, T + 32.0) is None and not m.held
    m.key(True, T + 33.0)
    assert m.tick(T + 33.15) == Press(at=T + 33.0)


def test_a_late_tick_dates_the_cap_at_the_cap():
    m = PttMachine()
    m.key(True, T)
    m.tick(T + 0.15)
    assert m.tick(T + 30.4) == Release(at=T + 30.0, held_ms=30000, capped=True)


def test_the_watchdog_ends_a_capped_hold_quietly():
    m = PttMachine()
    m.key(True, T)
    m.tick(T + 0.15)
    m.tick(T + 30.0)
    assert m.watch(False, T + 31.0) is None and m.watch(False, T + 31.02) is None
    assert not m.held  # its key-up was missed: the next key-down is a new press
    m.key(True, T + 32.0)
    assert m.tick(T + 32.15) == Press(at=T + 32.0)


def test_the_gate_and_the_cap_come_from_the_config():
    m = PttMachine(min_press_ms=300, max_hold_s=5.0)
    m.key(True, T)
    assert m.tick(T + 0.299) is None and m.tick(T + 0.3) == Press(at=T)
    assert m.tick(T + 5.0) == Release(at=T + 5.0, held_ms=5000, capped=True)
    m = PttMachine(min_press_ms=300)
    m.key(True, T)
    assert m.key(False, T + 0.25) == Tap(held_ms=250)


@pytest.mark.parametrize("kw", [{"min_press_ms": 0}, {"min_press_ms": -5},
                                {"min_press_ms": 1.5}, {"min_press_ms": True},
                                {"max_hold_s": 0.1}, {"max_hold_s": float("nan")},
                                {"max_hold_s": float("inf")}, {"min_press_ms": 2000,
                                                               "max_hold_s": 1.0}])
def test_bad_settings_are_refused(kw):
    with pytest.raises(ValueError):
        PttMachine(**kw)


# -- his key and the scheduler's hold ------------------------------------------------------------

ROOM = "guild_hall"


def line(id, guid, tier=4, *, at, template=False) -> Line:
    return Line(id=id, guid=guid, tier=tier, kind="open", channel=ROOM, whisper=False,
                expires_at=at + 15.0, after="", segments=[np.full(48000, 0.1, np.float32)],
                template=template)


def started(plan) -> list[str]:
    return [i for i, _, _ in plan.starts]


class Wired:
    """The wiring Task 11 does: a Press and a Release are the scheduler's set_ptt, which owns
    the hold (p2-p4 model lines, never a template, PTT_GRACE_S after key-up); a Tap reaches
    nothing. `calls` records every set_ptt."""

    def __init__(self, scheduler):
        self.machine = PttMachine()
        self.scheduler = scheduler
        self.calls: list[bool] = []

    def apply(self, event, now):
        if isinstance(event, (Press, Release)):
            down = isinstance(event, Press)
            self.calls.append(down)
            return self.scheduler.set_ptt(down, now)
        return None

    def key(self, down, now):
        return self.apply(self.machine.key(down, now), now)

    def tick(self, now):
        return self.apply(self.machine.tick(now), now)


def test_the_scheduler_holds_lines_only_from_a_press_to_its_release():
    """The key-state signal the scheduler's push-to-talk hold reads is the Press and the
    Release: a tap holds and ducks nothing; within the gate a model line still starts; after
    the Press a model line waits and a template line goes, ducked under him; after the
    Release the waiting line starts PTT_GRACE_S later."""
    s = PlaybackScheduler(FakeClock(START), random.Random(1), max_voices=3)
    s.set_session(True, T)
    s.set_human_room(ROOM, T)
    w = Wired(s)
    w.key(True, T)  # a backslash typed into chat
    assert started(s.submit(line("a", 101, at=T + 0.05), T + 0.05)) == ["a"]
    assert w.tick(T + 0.1) is None
    assert w.key(False, T + 0.12) is None and w.calls == []
    w.key(True, T + 2.0)  # now he talks
    assert started(s.submit(line("b", 102, 3, at=T + 2.1), T + 2.1)) == ["b"]  # in the gate
    plan = w.tick(T + 2.15)
    assert w.calls == [True] and plan.gains == {"a": PTT_DUCK_DB, "b": PTT_DUCK_DB}
    assert started(s.submit(line("c", 103, 4, at=T + 2.2), T + 2.2)) == []  # held
    plan = s.submit(line("cheer", 104, 4, at=T + 2.3, template=True), T + 2.3)
    assert [(i, g) for i, _, g in plan.starts] == [("cheer", PTT_DUCK_DB)]
    s.finished("a", T + 2.4)
    s.finished("b", T + 2.5)
    s.finished("cheer", T + 2.6)
    w.key(False, T + 3.0)
    assert w.calls == [True, False]
    assert started(s.tick(T + 3.0 + PTT_GRACE_S - 0.01)) == []
    assert started(s.tick(T + 3.0 + PTT_GRACE_S + 0.01)) == ["c"]


# -- RawInputKeys, without Win32 -----------------------------------------------------------------

def test_raw_input_is_decoded_by_its_break_flag():
    """WM_INPUT for the keyboard: our key only; key-up when Flags has RI_KEY_BREAK (the E0 and
    E1 prefix bits change nothing)."""
    assert RI_KEY_BREAK == 1
    assert raw_key_down(F24, 0, F24) is True
    assert raw_key_down(F24, 1, F24) is False
    assert raw_key_down(F24, 2, F24) is True and raw_key_down(F24, 3, F24) is False
    assert raw_key_down(F24, 4, F24) is True and raw_key_down(F24, 5, F24) is False
    assert raw_key_down(0x86, 0, F24) is None and raw_key_down(0xDC, 1, F24) is None


def test_keys_are_ignored_while_disarmed():
    """The armed switch is read on the event loop, where every event lands, so a key event
    already on its way when he goes offline is ignored too."""
    got, closed = [], []
    keys = RawInputKeys(0xDC, got.append, on_close=lambda: closed.append(1))
    assert keys.vk == 0xDC and not keys.armed and keys.hwnd == 0
    keys._deliver(True)
    assert got == []
    keys.armed = True
    keys._deliver(True)
    keys._deliver(False)
    keys.armed = False
    keys._deliver(True)
    assert got == [True, False] and closed == []
    keys.stop()  # never started: nothing to stop


@pytest.mark.parametrize("vk", [0, 0xFF, -1, 0x100, "0xDC", True])
def test_a_bad_key_is_refused(vk):
    with pytest.raises(ValueError):
        RawInputKeys(vk, print, on_close=print)


def test_the_key_needs_no_win32_until_it_starts():
    """PttMachine and RawInputKeys import and construct without leeroy_voice.winapi, so they do
    on Linux (CI); the guard would trip the import here."""
    m = PttMachine()
    m.key(True, T)
    RawInputKeys(F24, print, on_close=print)
    assert "leeroy_voice.winapi" not in sys.modules


def test_fake_keys_play_the_keyboard():
    """FakeKeys, for Task 11: events reach on_key at once while started and armed; a lost
    event (a missed key-up) is seen only by pressed(), as GetAsyncKeyState sees it; close() is
    the WM_CLOSE a plain taskkill posts."""
    got, closed = [], []
    keys = FakeKeys(0xDC, got.append, on_close=lambda: closed.append(1))
    keys.armed = True
    keys.key(True)
    assert got == [] and keys.pressed()  # not started: no window, no events
    keys.start()
    keys.key(True)
    keys.key(False, delivered=False)
    assert got == [True] and not keys.pressed()
    keys.armed = False
    keys.key(True)
    assert got == [True] and keys.pressed()
    keys.close()
    assert closed == [1]
    keys.stop()
    keys.close()
    assert closed == [1] and keys.stopped
