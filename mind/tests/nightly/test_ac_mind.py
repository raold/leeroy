"""Pure fake process-control tests: no subprocesses, Windows APIs or service changes."""
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

PATH = Path(__file__).resolve().parents[3] / "scripts" / "ac-mind.py"
SPEC = importlib.util.spec_from_file_location("ac_mind_tested", PATH)
control = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = control
SPEC.loader.exec_module(control)


class Processes:
    def __init__(self, rows):
        self.rows, self.killed, self.closed = list(rows), [], []
        self.reused = None

    def snapshot(self):
        return list(self.rows)

    def open(self, row):
        if row.pid == self.reused:
            raise control.OwnershipError("mind PID changed identity")
        return row.pid, row.created

    def terminate(self, handle):
        self.killed.append(handle)
        self.rows = [row for row in self.rows if (row.pid, row.created) != handle]

    def close(self, handle):
        self.closed.append(handle)


@pytest.fixture
def rig(tmp_path):
    python, base = tmp_path / "mind/.venv/Scripts/python.exe", tmp_path / "base/python.exe"
    data = tmp_path / "mind-data"
    data.mkdir()
    (data / "mind.toml").write_text("", encoding="utf-8")
    (data / "mind.db").write_bytes(b"synthetic")
    args = (str(python), "-m", "leeroy_mind", "run", "--data-dir", str(data))
    launcher = control.Process(100, 20, 1110, str(python), args)
    child = control.Process(101, 100, 1120, str(base), args)
    return tmp_path, data, python, base, launcher, child


def invoke(rig, action, processes, *, locked=None, spawn=None):
    root, data, python, base, *_ = rig
    time = [0.0]
    def sleep(seconds):
        time[0] += seconds
    return control.run_action(action, root, data, python, base, processes=processes,
        locked=locked or (lambda _: bool(processes.rows)),
        spawn=spawn or (lambda *args: pytest.fail("unexpected spawn")),
        monotonic=lambda: time[0], sleep=sleep, timeout=1)


def test_status_identifies_actual_child_not_venv_launcher(rig, capsys):
    processes = Processes(rig[-2:])
    assert invoke(rig, "status", processes) == 0
    assert "mind pid 101 (running)" in capsys.readouterr().out
    assert processes.killed == []


def test_stop_terminates_only_verified_child_then_launcher(rig):
    processes = Processes(rig[-2:])
    assert invoke(rig, "stop", processes) == 0
    assert processes.killed == [(101, 1120), (100, 1110)]
    assert processes.closed == processes.killed


def test_reused_launcher_prevents_any_termination_and_closes_acquired_child(rig):
    processes = Processes(rig[-2:])
    processes.reused = 100
    with pytest.raises(control.OwnershipError, match="identity"):
        invoke(rig, "stop", processes)
    assert processes.killed == []
    assert processes.closed == [(101, 1120)]


def test_other_python_jobs_and_data_dirs_never_match(rig):
    root, data, python, base, launcher, child = rig
    foreign = [
        control.Process(200, 20, 1, str(base), (str(base), "-m", "leeroy_voice", "run")),
        control.Process(201, 20, 1, str(base), (str(base), "-m", "leeroy_mind", "eval", "--data-dir", str(data))),
        control.Process(202, 20, 1, str(base), (str(base), "-m", "leeroy_mind", "run", "--data-dir", str(root / "other"))),
        control.Process(203, 20, 1, str(root / "wrong/python.exe"), child.argv),
        control.Process(204, 20, 1, str(base), (str(base), "-c", "print('leeroy_mind run')")),
    ]
    assert control.family(foreign, python, base, data) == []
    assert control.family([*foreign, launcher, child], python, base, data) == [child, launcher]


def test_unknown_lock_owner_and_ambiguous_instances_fail_closed(rig):
    with pytest.raises(control.OwnershipError, match="unidentified"):
        invoke(rig, "stop", Processes([]), locked=lambda _: True)
    launcher = rig[-2]
    other = control.Process(201, 20, 2, launcher.exe, launcher.argv)
    with pytest.raises(control.OwnershipError, match="multiple"):
        invoke(rig, "stop", Processes([launcher, other]))


def test_start_waits_for_lock_and_reports_its_actual_child(rig, capsys):
    processes = Processes([])
    spawned = []
    def spawn(*args):
        spawned.append(args)
        processes.rows = list(rig[-2:])
        return SimpleNamespace(pid=100, poll=lambda: None)
    assert invoke(rig, "start", processes, spawn=spawn) == 0
    assert len(spawned) == 1
    output = capsys.readouterr().out
    assert "mind started, pid 101" in output and "mind-stdout.log" in output


def test_start_does_not_duplicate_existing_instance_or_claim_timeout_success(rig):
    assert invoke(rig, "start", Processes(rig[-2:])) == 0
    with pytest.raises(control.OwnershipError, match="did not acquire"):
        invoke(rig, "start", Processes([]), locked=lambda _: False,
               spawn=lambda *args: SimpleNamespace(pid=100, poll=lambda: None))


def test_postlaunch_snapshot_error_reports_owned_pid_without_unsafe_cleanup(rig):
    processes = Processes([])
    def spawn(*args):
        def unavailable():
            raise OSError("private command-line payload")
        processes.snapshot = unavailable
        return SimpleNamespace(pid=100, poll=lambda: None)
    with pytest.raises(control.OwnershipError) as exc:
        invoke(rig, "start", processes, locked=lambda _: False, spawn=spawn)
    assert "launched pid 100" in str(exc.value) and "still running" in str(exc.value)
    assert "private command-line" not in str(exc.value)
    assert processes.killed == processes.closed == []


def test_competing_process_cannot_be_mistaken_for_our_successful_launch(rig):
    processes = Processes([])
    original = rig[-2]
    def spawn(*args):
        processes.rows = [control.Process(201, 20, 9, original.exe, original.argv)]
        return SimpleNamespace(pid=100, poll=lambda: 1)
    with pytest.raises(control.OwnershipError, match="launched pid 100"):
        invoke(rig, "start", processes, spawn=spawn)
    assert processes.killed == []


def test_stop_refuses_to_claim_success_while_lock_remains(rig):
    processes = Processes(rig[-2:])
    with pytest.raises(control.OwnershipError, match="servers have not been stopped"):
        invoke(rig, "stop", processes, locked=lambda _: True)


def test_explicit_equals_data_argument_is_recognized(rig):
    _, data, python, base, launcher, _ = rig
    args = launcher.argv[:4] + ("--data-dir=" + str(data), "--log-level", "INFO")
    row = control.Process(100, 20, 1110, str(python), args)
    assert control.matches(row, python, base, data)
