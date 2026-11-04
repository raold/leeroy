"""Independent lifecycle ownership regressions; all process operations are fakes."""
import importlib.util
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

SOURCE = Path(__file__).resolve().parents[3] / "scripts" / "ac-mind.py"
SPEC = importlib.util.spec_from_file_location("ac_mind_reviewed", SOURCE)
control = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = control
SPEC.loader.exec_module(control)


def setup(tmp_path):
    python = tmp_path / "mind/.venv/Scripts/python.exe"
    base = tmp_path / "base/python.exe"
    python.parent.mkdir(parents=True)
    data = tmp_path / "mind-data"
    data.mkdir()
    (data / "mind.toml").write_text("", encoding="utf-8")
    (data / "mind.db").write_bytes(b"synthetic")
    argv = (str(python), "-m", "leeroy_mind", "run", "--data-dir", str(data))
    parent = control.Process(100, 20, 1110, str(python), argv)
    child = control.Process(101, 100, 1120, str(base), argv)
    return python, base, data, parent, child


def test_relative_data_path_cannot_be_resolved_in_the_helpers_working_directory(tmp_path, monkeypatch):
    python, base, data, parent, _ = setup(tmp_path)
    monkeypatch.chdir(tmp_path)
    # The target may have a different cwd; its actual data directory cannot be inferred.
    ambiguous = replace(parent, argv=parent.argv[:-1] + ("mind-data",))
    assert not control.matches(ambiguous, python, base, data)


def test_relative_executable_identity_is_ambiguous_even_when_it_resolves_locally(tmp_path, monkeypatch):
    python, base, data, parent, _ = setup(tmp_path)
    monkeypatch.chdir(python.parent)
    ambiguous = replace(parent, argv=("python.exe", *parent.argv[1:]))
    assert not control.matches(ambiguous, python, base, data)


def test_reused_parent_pid_cannot_join_two_independent_matching_families(tmp_path):
    python, base, data, parent, child = setup(tmp_path)
    # An older child cannot belong to a parent that was created after it.
    child = replace(child, created=parent.created - 10)
    with pytest.raises(control.OwnershipError):
        control.family([parent, child], python, base, data)


def test_start_timeout_reports_the_outstanding_launch_without_claiming_success(tmp_path):
    python, base, data, parent, child = setup(tmp_path)
    rows, killed, closed = [], [], []
    clock = [0.0]

    def terminate(handle):
        killed.append(handle)
        rows[:] = [row for row in rows if (row.pid, row.created) != handle]

    processes = SimpleNamespace(snapshot=lambda: list(rows),
        open=lambda row: (row.pid, row.created), terminate=terminate, close=closed.append)
    spawned = SimpleNamespace(pid=parent.pid, poll=lambda: None)

    def spawn(*_):
        rows[:] = [parent, child]
        return spawned

    def sleep(seconds):
        clock[0] += seconds

    with pytest.raises(control.OwnershipError, match=str(parent.pid)):
        control.run_action("start", tmp_path, data, python, base, processes=processes,
            locked=lambda _: False, spawn=spawn, sleep=sleep, monotonic=lambda: clock[0], timeout=1)
    # An explicit outstanding PID lets the operator inspect/retry; startup failure does
    # not turn an incomplete inventory into authority to terminate unrelated descendants.
    assert killed == closed == [] and rows == [parent, child]
