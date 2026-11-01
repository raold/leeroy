"""WoW's ducking (Task 7): WoW's session down duck_db in 10 steps over 150 ms and back to its own
level after, the level kept in a state file until WoW is back at it, on FakeSessions. The
ducker's pauses are recorded instead of slept, or held by a Gate. Nothing here touches Windows."""
import json
import logging
import threading
import time

import pytest

from fakes import FakeSessions
from leeroy_voice.duck import RAMP_S, RAMP_STEPS, WowDucker

DOWN = 10 ** (-8 / 20)  # 8 dB


class Sleeps(list):
    """The ducker's sleep: each pause recorded, none slept."""

    def __call__(self, seconds: float) -> None:
        self.append(seconds)


class Gate:
    """A sleep that holds the ducker's worker until the test lets each pause through."""

    def __init__(self):
        self._cond = threading.Condition()
        self._permits = 0
        self._open = False
        self.waiting = 0

    def __call__(self, seconds: float) -> None:
        with self._cond:
            self.waiting += 1
            self._cond.notify_all()
            ok = self._cond.wait_for(lambda: self._open or self._permits > 0, timeout=5)
            self.waiting -= 1
            if ok and not self._open:
                self._permits -= 1
            self._cond.notify_all()

    def step(self, n: int = 1) -> None:
        """Let n pauses through, one at a time."""
        for _ in range(n):
            with self._cond:
                assert self._cond.wait_for(lambda: self.waiting > 0 and self._permits == 0, 5)
                self._permits += 1
                self._cond.notify_all()
                assert self._cond.wait_for(lambda: self._permits == 0, 5)

    def open(self) -> None:
        with self._cond:
            self._open = True
            self._cond.notify_all()

    def shut(self) -> None:
        with self._cond:
            self._open = False


def until(predicate, timeout: float = 5.0) -> None:
    end = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < end, "not true in time"
        time.sleep(0.001)


@pytest.fixture
def state(tmp_path):
    return tmp_path / "state" / "wow-volume.json"


@pytest.fixture
def make(state):
    """WowDucker(sessions, state, duck_db=8.0) with recorded sleeps; closed after the test."""
    made = []

    def make(sessions, *, duck_db=8.0, sleep=None):
        d = WowDucker(sessions, state, duck_db=duck_db,
                      sleep=sleep if sleep is not None else Sleeps())
        made.append(d)
        return d
    yield make
    for d in made:
        d.close()


def saved(state) -> dict:
    return json.loads(state.read_text(encoding="utf-8"))


def test_duck_ramps_and_restores(make, state):
    sessions = FakeSessions({"Wow.exe": 0.8})
    sleeps = Sleeps()
    d = make(sessions, sleep=sleeps)
    wow = sessions.handle("Wow.exe")
    d.duck(True)
    assert d.wait(5)
    assert len(wow.sets) == RAMP_STEPS == 10  # 150 ms in 10 steps
    assert sleeps == [pytest.approx(RAMP_S / RAMP_STEPS)] * 10 and sum(sleeps) == pytest.approx(0.15)
    assert all(a > b for a, b in zip([0.8] + wow.sets, wow.sets))  # down, step by step
    assert wow.sets[-1] == pytest.approx(0.8 * DOWN, abs=1e-12)
    assert saved(state) == {"volume": 0.8}
    wow.sets.clear()
    sleeps.clear()
    d.duck(False)
    assert d.wait(5)
    assert len(wow.sets) == 10 and len(sleeps) == 10
    assert all(a < b for a, b in zip([0.8 * DOWN] + wow.sets, wow.sets))
    assert wow.sets[-1] == 0.8 and wow.volume == 0.8
    assert not state.exists()


def test_the_steps_are_even_in_db(make):
    sessions = FakeSessions({"Wow.exe": 0.5})
    d = make(sessions, duck_db=10.0)
    d.duck(True)
    assert d.wait(5)
    levels = [0.5] + sessions.handle("Wow.exe").sets
    assert len(levels) == 11
    for a, b in zip(levels, levels[1:]):
        assert b / a == pytest.approx(10 ** (-1 / 20))  # 1 dB a step


def test_restore_after_crash(make, state):
    state.parent.mkdir(parents=True)
    state.write_text('{"volume": 0.7}', encoding="utf-8")
    sessions = FakeSessions({"Wow.exe": 0.25})  # left ducked by the crash
    make(sessions).restore_after_crash()
    assert sessions.handle("Wow.exe").volume == 0.7
    assert not state.exists()


def test_duck_zero_is_off(make, state):
    sessions = FakeSessions({"Wow.exe": 0.8})
    d = make(sessions, duck_db=0.0)
    d.duck(True)
    assert d.wait(5)
    d.duck(False)
    assert d.wait(5)
    assert sessions.handle("Wow.exe").sets == [] and sessions.finds == 0
    assert not state.exists()


def test_no_wow_session(make, state):
    sessions = FakeSessions({"Discord.exe": 0.5})
    d = make(sessions)
    d.duck(True)
    assert d.wait(5)
    d.duck(False)
    assert d.wait(5)
    d.restore_after_crash()
    assert not state.exists() and sessions.handle("Discord.exe").sets == []


def test_the_process_name_is_matched_without_case(make, state):
    sessions = FakeSessions({"WoW.exe": 0.6})
    d = make(sessions)
    d.duck(True)
    assert d.wait(5)
    assert sessions.handle("WoW.exe").volume == pytest.approx(0.6 * DOWN)


def test_duck_returns_at_once(make, state):
    """The service calls it from its event loop: the ramp runs on the ducker's own thread. The
    level is saved before the first change."""
    sessions = FakeSessions({"Wow.exe": 0.8})
    gate = Gate()
    d = make(sessions, sleep=gate)
    began = time.monotonic()
    d.duck(True)
    assert time.monotonic() - began < 0.1
    until(lambda: gate.waiting == 1)  # the worker waits in its first pause
    assert saved(state) == {"volume": 0.8} and sessions.handle("Wow.exe").sets == []
    assert not d.wait(0.01)
    gate.open()
    assert d.wait(5)


def test_a_restore_mid_ramp_turns_back_from_where_it_is(make, state):
    sessions = FakeSessions({"Wow.exe": 0.8})
    gate = Gate()
    d = make(sessions, sleep=gate)
    wow = sessions.handle("Wow.exe")
    d.duck(True)
    gate.step(4)
    until(lambda: len(wow.sets) == 4)
    d.duck(False)  # the last voice ended before WoW was all the way down
    gate.step()  # the worker wakes, sees it, and turns back without a fifth step down
    gate.open()
    assert d.wait(5)
    down, up = wow.sets[:4], wow.sets[4:]
    assert all(a > b for a, b in zip([0.8] + down, down))
    assert len(up) == 10 and all(a < b for a, b in zip([down[-1]] + up, up))
    assert up[-1] == 0.8 and not state.exists()


def test_a_duck_mid_restore_goes_back_down_from_where_it_is(make, state):
    sessions = FakeSessions({"Wow.exe": 0.8})
    gate = Gate()
    d = make(sessions, sleep=gate)
    wow = sessions.handle("Wow.exe")
    d.duck(True)
    gate.open()
    assert d.wait(5)
    looked = sessions.finds
    gate.shut()
    d.duck(False)
    gate.step(3)
    until(lambda: len(wow.sets) == 13)
    d.duck(True)  # a new voice starts during the restore
    gate.step()
    gate.open()
    assert d.wait(5)
    assert wow.sets[-1] == pytest.approx(0.8 * DOWN) and len(wow.sets) == 23
    assert saved(state) == {"volume": 0.8}  # the same episode: the first level kept ...
    assert sessions.finds == looked  # ... on the sessions it holds, not looked up again


def test_close_restores_at_once(make, state):
    sessions = FakeSessions({"Wow.exe": 0.8})
    d = make(sessions)
    wow = sessions.handle("Wow.exe")
    d.duck(True)
    assert d.wait(5)
    n = len(wow.sets)
    d.close()
    assert wow.sets[n:] == [0.8] and not state.exists()  # one step, no ramp
    d.duck(True)  # closed: nothing more
    assert wow.sets[n:] == [0.8]


def test_close_mid_ramp_restores(make, state):
    sessions = FakeSessions({"Wow.exe": 0.8})
    gate = Gate()
    d = make(sessions, sleep=gate)
    wow = sessions.handle("Wow.exe")
    d.duck(True)
    gate.step(2)
    until(lambda: len(wow.sets) == 2)
    closer = threading.Thread(target=d.close)
    closer.start()
    gate.open()
    closer.join(5)
    assert not closer.is_alive()
    assert wow.volume == 0.8 and not state.exists()


def test_wow_quitting_while_ducked_keeps_its_level_for_later(make, state):
    """Windows gives a program that quits its last level again when it next runs. So when WoW
    quits ducked, the saved level is kept, and used, until WoW is back at it."""
    sessions = FakeSessions({"Wow.exe": 0.8})
    d = make(sessions)
    d.duck(True)
    assert d.wait(5)
    sessions.quit("Wow.exe")
    d.duck(False)
    assert d.wait(5)
    assert saved(state) == {"volume": 0.8}
    wow = sessions.launch("Wow.exe", 0.8 * DOWN)  # WoW again, at the level it quit with
    d.duck(True)
    assert d.wait(5)
    assert saved(state) == {"volume": 0.8}  # the saved level, not the ducked one it came back at
    d.duck(False)
    assert d.wait(5)
    assert wow.volume == 0.8 and not state.exists()


def test_a_crash_file_waits_for_wow(make, state):
    state.parent.mkdir(parents=True)
    state.write_text('{"volume": 0.7}', encoding="utf-8")
    sessions = FakeSessions()
    d = make(sessions)
    d.restore_after_crash()
    assert saved(state) == {"volume": 0.7}  # WoW is not running: kept
    wow = sessions.launch("Wow.exe", 0.7 * DOWN)
    d.restore_after_crash()
    assert wow.volume == 0.7 and not state.exists()


def test_a_crash_file_wins_over_a_first_duck(make, state):
    """Review I1: a crash left WoW ducked and its level in the file, and a voice starts before
    restore_after_crash() runs. The saved level is the baseline, so the file keeps 0.8 throughout
    and WoW comes back to 0.8, not to the ducked level it was found at."""
    state.parent.mkdir(parents=True)
    state.write_text('{"volume": 0.8}', encoding="utf-8")
    sessions = FakeSessions({"Wow.exe": 0.8 * DOWN})  # left ducked by the crash
    seen = []
    d = make(sessions, sleep=lambda s: seen.append(saved(state)))  # the file, at every step
    wow = sessions.handle("Wow.exe")
    d.duck(True)
    assert d.wait(5)
    assert saved(state) == {"volume": 0.8}
    d.duck(False)
    assert d.wait(5)
    assert wow.volume == 0.8 and not state.exists()
    assert len(seen) == 10 and all(s == {"volume": 0.8} for s in seen)
    d.restore_after_crash()  # nothing left to restore
    assert wow.volume == 0.8 and not state.exists()


def test_a_crash_level_is_the_next_ducks_baseline(make, state):
    state.parent.mkdir(parents=True)
    state.write_text('{"volume": 0.7}', encoding="utf-8")
    sessions = FakeSessions()
    d = make(sessions)
    d.restore_after_crash()
    wow = sessions.launch("Wow.exe", 0.7 * DOWN)
    d.duck(True)
    assert d.wait(5)
    d.duck(False)
    assert d.wait(5)
    assert wow.volume == 0.7 and not state.exists()


@pytest.mark.parametrize("text", ["{not json", '{"volume": 3}', '{"volume": "0.5"}', "[0.5]",
                                  '{"volume": NaN}'])
def test_a_damaged_state_file_is_dropped(make, state, caplog, text):
    state.parent.mkdir(parents=True)
    state.write_text(text, encoding="utf-8")
    sessions = FakeSessions({"Wow.exe": 0.8})
    with caplog.at_level(logging.WARNING, logger="leeroy_voice"):
        make(sessions).restore_after_crash()
    assert not state.exists() and sessions.handle("Wow.exe").sets == []
    assert "wow_state_unreadable" in caplog.text


def test_the_level_is_read_at_each_duck(make, state):
    sessions = FakeSessions({"Wow.exe": 0.8})
    d = make(sessions)
    wow = sessions.handle("Wow.exe")
    d.duck(True)
    assert d.wait(5)
    d.duck(False)
    assert d.wait(5)
    wow.volume = 0.5  # he turns WoW down in the volume mixer
    d.duck(True)
    assert d.wait(5)
    assert saved(state) == {"volume": 0.5} and wow.volume == pytest.approx(0.5 * DOWN)


def test_a_failing_session_is_survived(make, state, caplog):
    sessions = FakeSessions({"Wow.exe": 0.8})
    d = make(sessions)
    sessions.handle("Wow.exe").gone = True  # every call on it fails
    with caplog.at_level(logging.WARNING, logger="leeroy_voice"):
        d.duck(True)
        assert d.wait(5)
        d.duck(False)
        assert d.wait(5)
    assert not state.exists()


def test_a_crash_restore_waits_while_ducking(make, state):
    sessions = FakeSessions({"Wow.exe": 0.8})
    d = make(sessions)
    d.duck(True)
    assert d.wait(5)
    d.restore_after_crash()  # the ducking in progress owns the file
    assert saved(state) == {"volume": 0.8}
    assert sessions.handle("Wow.exe").volume == pytest.approx(0.8 * DOWN)


def test_a_wow_that_cannot_be_set_back_keeps_the_file(make, state):
    sessions = FakeSessions({"Wow.exe": 0.8})
    d = make(sessions)
    d.duck(True)
    assert d.wait(5)
    sessions.quit("Wow.exe")
    sessions.launch("Wow.exe", 0.8 * DOWN).gone = True  # running, but not answering
    d.duck(False)
    assert d.wait(5)
    assert saved(state) == {"volume": 0.8}


def test_sessions_that_fail_to_list_are_survived(make, state, caplog):
    sessions = FakeSessions({"Wow.exe": 0.8})

    def broken(process):
        raise OSError("the audio service is restarting")
    sessions.find = broken
    d = make(sessions)
    with caplog.at_level(logging.WARNING, logger="leeroy_voice"):
        d.duck(True)
        assert d.wait(5)
    assert not state.exists() and "wow_find_failed" in caplog.text


def test_wow_is_never_ducked_without_its_level_saved(make, tmp_path, caplog):
    """The state file cannot be written (here its folder is a file): WoW is left alone, since a
    crash would otherwise leave it ducked with nothing to restore it from."""
    (tmp_path / "state").write_text("not a folder", encoding="utf-8")
    sessions = FakeSessions({"Wow.exe": 0.8})
    d = make(sessions)
    with caplog.at_level(logging.WARNING, logger="leeroy_voice"):
        d.duck(True)
        assert d.wait(5)
    assert sessions.handle("Wow.exe").sets == [] and "wow_duck_failed" in caplog.text
    d.duck(True)  # the thread lives on
    assert d.wait(5)


def test_an_unreadable_state_file_is_left(make, state, caplog):
    state.mkdir(parents=True)  # a folder where the file should be
    sessions = FakeSessions({"Wow.exe": 0.8})
    with caplog.at_level(logging.WARNING, logger="leeroy_voice"):
        make(sessions).restore_after_crash()
    assert state.is_dir() and sessions.handle("Wow.exe").sets == []
    assert "wow_state_unreadable" in caplog.text


def test_bad_settings_are_refused(state):
    for bad in (-1.0, float("nan"), "8"):
        with pytest.raises(ValueError):
            WowDucker(FakeSessions(), state, duck_db=bad)


# -- the watch on WoW's level (Task 11, M30) --------------------------------------------------------

def test_level_reads_wows_session(tmp_path):
    sessions = FakeSessions({"Wow.exe": 0.8})
    d = WowDucker(sessions, tmp_path / "wow.json", duck_db=8.0, sleep=Sleeps())
    assert d.level() == 0.8
    sessions.handle("Wow.exe").volume = 0.5
    assert d.level() == 0.5
    assert not (tmp_path / "wow.json").exists()


def test_level_is_none_with_no_wow(tmp_path):
    assert WowDucker(FakeSessions(), tmp_path / "wow.json", duck_db=8.0).level() is None


def test_level_is_none_while_a_ducking_owns_it(tmp_path):
    """What the ducker sets is not a change from outside, from the first call to the end of
    the ramp back."""
    d = WowDucker(FakeSessions({"Wow.exe": 0.8}), tmp_path / "wow.json", duck_db=8.0,
                  sleep=Sleeps())
    d.duck(True)
    assert d.wait(5)
    assert d.level() is None                    # ducked
    d.duck(False)
    assert d.wait(5)
    assert d.level() == 0.8
    d.close()


def test_level_is_none_while_a_call_waits_for_the_worker(tmp_path):
    gate = threading.Event()
    d = WowDucker(FakeSessions({"Wow.exe": 0.8}), tmp_path / "wow.json", duck_db=8.0,
                  sleep=lambda s: gate.wait(5))
    d.duck(True)
    assert d.level() is None                    # asked for, not done
    gate.set()
    d.close()
