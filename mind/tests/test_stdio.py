import logging
import os
import subprocess
import sys
import atexit
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
import pytest

from leeroy_mind.lifecycle import spawn_mind
from leeroy_mind.stdio import RotatingOutput, capture


def test_midnight_rollover_keeps_prints_and_fourteen_backups(tmp_path, monkeypatch):
    output = RotatingOutput(tmp_path)
    assert output.write("before\n") == 7
    for day in range(1, 16):
        (tmp_path / f"mind-stdout.log.2026-10-{day:02}").write_text("older\n")
    # Simulate the next midnight without sleeping or changing the system clock.
    midnight = int(datetime(2026, 10, 17).timestamp())
    monkeypatch.setattr("logging.handlers.time.time", lambda: midnight)
    output.handler.rolloverAt = midnight
    output.handler.doRollover()
    output.write("after\n")
    output.close()
    assert (tmp_path / "mind-stdout.log").read_text() == "after\n"
    assert (tmp_path / "mind-stdout.log.2026-10-16").read_text() == "before\n"
    assert len(list(tmp_path.glob("mind-stdout.log.*"))) == 14
    assert not (tmp_path / "mind-stdout.log.2026-10-02").exists()


def test_capture_preserves_fragments_and_restores_streams(tmp_path):
    before = sys.stdout, sys.stderr
    restore = capture(tmp_path)
    try:
        print("stdout", end=" ")
        sys.stderr.write("stderr\n")
        handler = logging.StreamHandler()
        handler.handle(logging.LogRecord("test", logging.WARNING, "", 0, "warning", (), None))
    finally:
        restore()
    restore()
    assert (sys.stdout, sys.stderr) == before
    assert (tmp_path / "mind-stdout.log").read_text() == "stdout stderr\nwarning\n"


def test_detached_entrypoint_captures_early_cli_errors_without_a_service(tmp_path, monkeypatch):
    from leeroy_mind.__main__ import main
    from leeroy_mind.stdio import capture_detached
    monkeypatch.setenv("LEEROY_CAPTURE_STDIO", "1")
    monkeypatch.setenv("LEEROY_MIND_DATA", str(tmp_path))
    cleanups = []
    monkeypatch.setattr(atexit, "register", cleanups.append)
    capture_detached()
    try:
        with pytest.raises(SystemExit) as caught:
            main(["--invalid-test-option"])
        assert caught.value.code == 2
        try:
            raise RuntimeError("synthetic failure")
        except RuntimeError:
            sys.excepthook(*sys.exc_info())
    finally:
        cleanups[0]()
    assert "error:" in (tmp_path / "logs" / "mind-stdout.log").read_text()
    assert "RuntimeError: synthetic failure" in (tmp_path / "logs" / "mind-stdout.log").read_text()


def test_launcher_leaves_log_ownership_in_the_child(tmp_path):
    calls = []
    def popen(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(pid=101)
    spawn_mind(tmp_path, tmp_path / "data", Path(sys.executable), popen=popen)
    args, options = calls[0]
    assert options["stdout"] == subprocess.DEVNULL
    assert options["env"]["LEEROY_CAPTURE_STDIO"] == "1"
    assert args[0][1:4] == ["-m", "leeroy_mind", "run"]
    assert not (tmp_path / "data" / "logs" / "mind-stdout.log").exists()
