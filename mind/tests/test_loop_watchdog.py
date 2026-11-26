import asyncio
import subprocess
import sys
import textwrap
from types import SimpleNamespace

import pytest

from leeroy_mind.config import load_config
from leeroy_mind.watchdog.config import load
from leeroy_mind.watchdog import loop as W


class Timer:
    def __init__(self):
        self.calls = []
        self.stream = None

    def dump_traceback_later(self, timeout, *, repeat, file, exit):
        assert not repeat and not exit and not file.closed
        self.stream = file
        self.calls.append(("arm", timeout))

    def cancel_dump_traceback_later(self):
        assert self.stream is None or not self.stream.closed
        self.calls.append(("cancel",))


@pytest.fixture
def rig(tmp_path, monkeypatch):
    clock = [10.0]
    handles = []
    def later(delay, callback):
        handle = SimpleNamespace(cancelled=False, callback=callback)
        handle.cancel = lambda: setattr(handle, "cancelled", True)
        handles.append(handle)
        return handle
    monkeypatch.setattr(W.asyncio, "get_running_loop", lambda: SimpleNamespace(call_later=later))
    timer = Timer()
    guard = W.LoopWatchdog(tmp_path, backend=timer, now=lambda: clock[0])
    def pulse(at):
        clock[0] = at
        handles.pop(0).callback()
    yield SimpleNamespace(guard=guard, timer=timer, pulse=pulse, handles=handles, root=tmp_path)
    guard.close()
    assert W._owner is None


@pytest.mark.parametrize("raw,on", [
    ("", True), ("[watchdog]\nenabled=true", True), ("[watchdog]\nenabled=false", False),
    ('[watchdog]\nenabled="true"', False), ("[watchdog]\nenabled=1", False),
    ("[watchdog]\nenabled=true\nextra=1", False), ("watchdog=1", False),
    ("[watchdog", False),
])
def test_config_is_on_by_default_and_fail_closed(tmp_path, raw, on):
    path = tmp_path / "mind.toml"
    assert load(path).enabled  # on by default (2026-11-25)
    path.write_text(raw, encoding="utf-8")
    assert load(path).enabled is on
    if on:
        load_config(path)  # the shared loader accepts the extension table


def test_healthy_heartbeat_keeps_resetting_one_shot_timer_and_closes_in_order(rig):
    guard = rig.guard.start()
    for at in (10.25, 10.5, 10.75):
        rig.pulse(at)
    assert rig.timer.calls == [("arm", 2.0)] * 4
    assert guard.deadline == 12.75
    stream = guard.stream
    guard.close()
    guard.close()
    assert rig.timer.calls[-1] == ("cancel",)
    assert stream.closed and rig.handles[-1].cancelled
    rig.handles[-1].callback()  # a stale callback cannot reopen/rearm after shutdown
    assert rig.timer.calls.count(("cancel",)) == 1


def test_stall_is_one_shot_and_recovery_rate_limits_the_next_dump(rig, caplog):
    guard = rig.guard.start()
    rig.pulse(13.0)
    assert rig.timer.calls == [("arm", 2.0), ("cancel",)]
    assert guard.not_before == 73.0
    for at in (14.0, 20.0, 72.9):
        rig.pulse(at)
    assert len(rig.timer.calls) == 2
    rig.pulse(73.0)
    assert rig.timer.calls[-1] == ("arm", 2.0)
    assert "loop_watchdog_delayed delay_ms=3000" in caplog.text
    assert "loop_watchdog_recovered delayed_ms=3000" in guard.path.read_text()


def test_second_owner_does_not_cancel_or_replace_the_active_timer(rig):
    rig.guard.start()
    other = W.LoopWatchdog(rig.root, backend=rig.timer).start()
    other.close()
    assert rig.timer.calls == [("arm", 2.0)]
    assert W._owner is rig.guard


def test_log_failure_disables_only_diagnostics(rig, monkeypatch, caplog):
    def failed():
        raise OSError("must not appear in log")
    monkeypatch.setattr(rig.guard, "_open", failed)
    rig.guard.start()
    assert rig.guard.closed and W._owner is None
    assert "loop_watchdog_disabled reason=diagnostic_io" in caplog.text
    assert "must not appear" not in caplog.text


def test_timer_failure_cancels_before_closing_descriptor(rig, monkeypatch):
    def failed(*args, **kw):
        rig.timer.stream = kw["file"]
        raise RuntimeError("timer unavailable")
    monkeypatch.setattr(rig.timer, "dump_traceback_later", failed)
    rig.guard.start()
    assert rig.guard.closed and rig.timer.stream.closed
    assert rig.timer.calls == [("cancel",)]


def test_rotates_only_watchdog_logs_and_keeps_three_backups(rig, monkeypatch):
    monkeypatch.setattr(W, "MAX_BYTES", 20)
    rig.guard.path.write_bytes(b"x" * 21)
    for index in (1, 2, 3):
        rig.guard.path.with_name(rig.guard.path.name + f".{index}").write_text(str(index))
    unrelated = rig.root / "mind.log"
    unrelated.write_text("keep")
    rig.guard.start()
    assert rig.guard.path.with_name(rig.guard.path.name + ".1").read_bytes() == b"x" * 21
    assert rig.guard.path.with_name(rig.guard.path.name + ".3").read_text() == "2"
    assert unrelated.read_text() == "keep"


def test_service_wrapper_closes_on_failure(tmp_path, monkeypatch):
    from leeroy_mind import __main__ as cli
    from contextlib import contextmanager
    events = []
    @contextmanager
    def monitored(root):
        assert root == tmp_path
        events.append("start")
        try:
            yield
        finally:
            events.append("close")
    async def failed(*args, **kwargs):
        raise RuntimeError("failed startup")
    monkeypatch.setattr(W, "monitor", monitored)
    monkeypatch.setattr(cli, "_serve", failed)
    with pytest.raises(RuntimeError, match="failed startup"):
        asyncio.run(cli.serve(tmp_path))
    assert events == ["start", "close"]


def test_disabled_monitor_opens_no_file_and_owns_no_timer(tmp_path):
    (tmp_path / "mind.toml").write_text("[watchdog]\nenabled=false\n", encoding="utf-8")
    with W.monitor(tmp_path):
        assert W._owner is None
    assert not (tmp_path / "logs").exists()


@pytest.mark.parametrize("hold_gil", [False, True])
def test_real_native_timer_captures_main_thread_without_source_or_locals(tmp_path, hold_gil):
    # Separate process: do not overwrite pytest's own process-global faulthandler timer.
    script = textwrap.dedent('''
        import asyncio, ctypes, sys, time
        from pathlib import Path
        from leeroy_mind.watchdog.loop import LoopWatchdog
        def block_loop():
            private_value = "NEVER_PRINT_THIS_PRIVATE_LOCAL"
            if sys.argv[2] == "True":
                if sys.platform == "win32":
                    ctypes.PyDLL("kernel32").Sleep(400)
                else:
                    ctypes.PyDLL(None).usleep(400_000)
            else:
                time.sleep(0.4)
        async def run():
            guard = LoopWatchdog(Path(sys.argv[1]), timeout=0.15, heartbeat=0.025, cooldown=20).start()
            try:
                block_loop()
                await asyncio.sleep(0.1)
                block_loop()  # within the cooldown: no second traceback
                await asyncio.sleep(0.1)
            finally:
                guard.close()
        asyncio.run(run())
    ''')
    # A standalone diagnostic script, not a service launch. Keep source out of the
    # Windows command line (and its audit record); -I isolates Python startup.
    child = tmp_path / "watchdog_rehearsal.py"
    child.write_text(script, encoding="utf-8")
    result = subprocess.run([sys.executable, "-I", str(child), str(tmp_path), str(hold_gil)],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    trace = (tmp_path / "mind-loop-stalls.log").read_text()
    assert trace.count("Timeout (") == 1
    assert "block_loop" in trace and "main_thread=0x" in trace
    assert "NEVER_PRINT_THIS_PRIVATE_LOCAL" not in trace
    assert "loop_watchdog_recovered" in trace
