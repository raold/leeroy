"""Small real launcher/child workers, with no models, devices, GPU or private cast."""
import builtins
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import threading
import time
from types import SimpleNamespace

import psutil
import pytest

from leeroy_voice.engines import EngineError
from leeroy_voice.engines import expressive as expressive
from leeroy_voice.engines import worker as wire


LAUNCHER = textwrap.dedent("""\
    import os
    from pathlib import Path
    import subprocess
    import sys
    import time

    child = subprocess.Popen([sys.executable, "-I", "-X", "utf8", *sys.argv[1:5]],
                             stdin=sys.stdin.buffer, stdout=sys.stdout.buffer,
                             stderr=subprocess.DEVNULL)
    while child.poll() is None:
        if Path(sys.argv[5]).exists():
            os._exit(0)
        time.sleep(0.01)
    sys.exit(child.returncode)
    """)

CHILD = textwrap.dedent("""\
    import json
    import os
    from pathlib import Path
    import sys
    import time

    Path(sys.argv[1]).write_text(str(os.getpid()), encoding="ascii")
    mode, marker = sys.argv[2], Path(sys.argv[3])
    if mode == "malformed":
        time.sleep(0.2)
        print("not a protocol answer", flush=True)
        time.sleep(60)
    else:
        print(json.dumps({"id": "ready", "ok": True, "sr": 24000,
                          "frames": 0, "error": ""}), flush=True)
        if mode == "hang":
            time.sleep(60)
        else:
            for line in sys.stdin:
                pass
            marker.write_text("graceful EOF", encoding="ascii")
    """)


def wait_until(condition, timeout=5):
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() >= deadline:
            raise AssertionError("lifecycle condition did not arrive")
        time.sleep(0.01)


@pytest.fixture
def workers(tmp_path):
    launcher, child = tmp_path / "launcher.py", tmp_path / "child.py"
    launcher.write_text(LAUNCHER, encoding="utf-8")
    child.write_text(CHILD, encoding="utf-8")
    created = []

    def make(mode):
        pid_file = tmp_path / f"{len(created)}.pid"
        marker = tmp_path / f"{len(created)}.closed"
        exit_launcher = tmp_path / f"{len(created)}.exit-launcher"
        worker = expressive.WorkerProcess(sys.executable, launcher,
            args=(child, pid_file, mode, marker, exit_launcher), start_s=10)
        created.append(worker)
        return worker, pid_file, marker, exit_launcher

    yield make
    # A regression must not leave its test child behind. Use only tracker-owned
    # identities; never kill a PID read from a filename without its creation time.
    for worker in created:
        try:
            worker.stop()
        finally:
            tree = worker._tree
            if tree is not None:
                tree.stop(timeout=2, grace=0)


def tracked_child(worker, pid_file):
    wait_until(pid_file.exists)
    pid = int(pid_file.read_text(encoding="ascii"))
    wait_until(lambda: pid in worker._tree.owned)
    return psutil.Process(pid), worker._tree.owned[pid]


def original_running(process, created):
    try:
        return (process.create_time() == created and process.is_running()
                and process.status() != psutil.STATUS_ZOMBIE)
    except psutil.NoSuchProcess:
        return False


def test_stop_delivers_eof_before_terminating_launcher_child(workers):
    worker, pid_file, marker, _ = workers("graceful")
    worker.start()
    child, created = tracked_child(worker, pid_file)
    worker.stop()
    worker.stop()
    assert marker.read_text(encoding="ascii") == "graceful EOF"
    assert not original_running(child, created)
    assert worker.process.poll() is not None
    assert not worker._reader.is_alive()


def test_timeout_stops_actual_interpreter_not_only_launcher(workers):
    worker, pid_file, _, _ = workers("hang")
    worker.start()
    child, created = tracked_child(worker, pid_file)
    assert child.pid != worker.process.pid
    started = time.monotonic()
    with pytest.raises(EngineError, match="no answer"):
        worker.ask("synthetic line", "", {}, timeout=0.05)
    assert time.monotonic() - started < 5
    assert not original_running(child, created)
    assert worker.process.poll() is not None
    assert not worker._reader.is_alive()


def test_malformed_ready_cleans_launched_child(workers):
    worker, pid_file, _, _ = workers("malformed")
    with pytest.raises(EngineError, match="did not load"):
        worker.start()
    pid = int(pid_file.read_text(encoding="ascii"))
    assert pid in worker._tree.owned
    assert worker._tree._matching(pid) is None
    assert worker.process.poll() is not None


def test_child_is_retained_after_its_launcher_exits(workers):
    worker, pid_file, _, exit_launcher = workers("hang")
    worker.start()
    child, created = tracked_child(worker, pid_file)
    exit_launcher.write_text("exit", encoding="ascii")
    worker.process.wait(timeout=5)
    assert original_running(child, created)
    worker.stop()
    assert not original_running(child, created)
    assert not worker._reader.is_alive()


ORPHANING = textwrap.dedent("""\
    from pathlib import Path
    import subprocess
    import sys

    sys.stdin.readline()
    child = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(60)"])
    Path(sys.argv[1]).write_text(str(child.pid), encoding="ascii")
    """)


@pytest.mark.skipif(not psutil.WINDOWS, reason="POSIX hands an orphan to init")
def test_interpreter_is_captured_though_its_launcher_exited_before_any_scan(tmp_path):
    script, pid_file = tmp_path / "orphaning.py", tmp_path / "child.pid"
    script.write_text(ORPHANING, encoding="utf-8")
    base = getattr(sys, "_base_executable", sys.executable)  # no venv redirector in between
    launcher = subprocess.Popen([base, "-I", script, pid_file], stdin=subprocess.PIPE)
    tree, child, created = wire.WorkerTree(), None, None
    try:
        tree.attach(launcher)
        tree.settle()
        tree._thread.join(wire.WATCH_S + 2)
        assert tree.owned == {launcher.pid: psutil.Process(launcher.pid).create_time()}
        launcher.communicate(b"go\n", timeout=10)  # it starts the interpreter and exits
        child = psutil.Process(int(pid_file.read_text(encoding="ascii")))
        created = child.create_time()
        assert tree.capture() and tree.owned.get(child.pid) == created
        assert tree.stop(timeout=5, grace=0)
        assert not original_running(child, created)
    finally:
        tree.stop(timeout=2, grace=0)
        if child is not None and original_running(child, created):
            child.kill()


def test_failed_cleanup_keeps_ownership_and_blocks_replacement_until_retry(workers, monkeypatch):
    worker, pid_file, _, _ = workers("hang")
    worker.start()
    child, created = tracked_child(worker, pid_file)
    made = expressive.ExpressiveEngine("chatterbox-turbo", sys.executable, need_gb=3,
        script=Path(worker._command[4]), args=worker._command[5:])
    made.worker = worker
    stop_tree = worker._tree.stop
    original_worker_class = expressive.WorkerProcess
    monkeypatch.setattr(expressive, "JOIN_S", 0.01)
    monkeypatch.setattr(worker._tree, "stop", lambda **kw: False)

    def forbidden_replacement(*args, **kwargs):
        raise AssertionError("old worker still owns a live child")

    monkeypatch.setattr(expressive, "WorkerProcess", forbidden_replacement)
    try:
        with pytest.raises(EngineError, match="cleanup incomplete"):
            made.stop()
        assert made.worker is worker and not worker.up
        assert original_running(child, created)
        with pytest.raises(EngineError, match="cleanup incomplete"):
            made.start()
        assert made.worker is worker

        monkeypatch.setattr(worker._tree, "stop", stop_tree)
        monkeypatch.setattr(expressive, "JOIN_S", 2.0)
        made.stop()  # the original stopped worker must retry, not return early
        assert made.worker is None and worker._cleanup_complete
        assert not original_running(child, created)
        monkeypatch.setattr(expressive, "WorkerProcess", original_worker_class)
        made.start()
        assert made.up and made.worker is not worker
    finally:
        monkeypatch.setattr(worker._tree, "stop", stop_tree)
        made.stop()


def test_concurrent_stop_wakes_ask_and_serializes_cleanup(workers, monkeypatch):
    worker, pid_file, _, _ = workers("hang")
    worker.start()
    child, created = tracked_child(worker, pid_file)
    calls, failures = [], []
    actual_stop = worker._tree.stop

    def counted_stop(**kwargs):
        calls.append(1)
        return actual_stop(**kwargs)

    def asking():
        try:
            worker.ask("synthetic line", "", {}, timeout=30)
        except EngineError as error:
            failures.append(type(error).__name__)

    monkeypatch.setattr(worker._tree, "stop", counted_stop)
    thread = threading.Thread(target=asking)
    thread.start()
    wait_until(lambda: worker._sequence == 1)
    worker.stop()
    thread.join(5)
    assert not thread.is_alive()
    assert failures == ["EngineError"] and calls == [1]
    assert not original_running(child, created)


class FakeProcess:
    def __init__(self, owner, pid, created, parent=0):
        self.owner, self.pid, self.created, self.parent = owner, pid, created, parent
        self.living = True

    def create_time(self):
        return self.created

    def is_running(self):
        return self.living

    def status(self):
        return "running"

    def ppid(self):
        return self.parent

    def suspend(self):
        self.owner.events.append(("suspend", self.pid))

    def resume(self):
        self.owner.events.append(("resume", self.pid))

    def kill(self):
        self.owner.events.append(("kill", self.pid))
        self.living = False


class FakeProcesses:
    Error = psutil.Error
    NoSuchProcess = psutil.NoSuchProcess
    STATUS_ZOMBIE = psutil.STATUS_ZOMBIE

    def __init__(self):
        self.events, self.processes = [], {}

    def add(self, pid, created, parent=0):
        result = FakeProcess(self, pid, created, parent)
        self.processes[pid] = result
        return result

    def Process(self, pid):
        if pid not in self.processes:
            raise self.NoSuchProcess(pid)
        return self.processes[pid]

    def _ppid_map(self):
        self.events.append(("walk", None))
        return {p.pid: p.parent for p in self.processes.values() if p.living}


@pytest.mark.parametrize("reused_root", [False, True])
def test_reused_pid_never_authorizes_foreign_process_or_descendants(reused_root):
    tree, processes = wire.WorkerTree(), FakeProcesses()
    tree.ps = processes
    tree.owned = {10: 100.0, 11: 101.0}
    processes.add(10, 900.0 if reused_root else 100.0)
    processes.add(11, 901.0, parent=999)
    processes.add(12, 902.0, parent=11)
    if reused_root:
        processes.add(13, 903.0, parent=10)
    assert tree.stop(timeout=0.2, grace=0)
    assert all(pid not in {11, 12, 13} for _, pid in processes.events)
    if reused_root:
        assert ("kill", 10) not in processes.events
    else:
        assert ("kill", 10) in processes.events
    assert processes.processes[11].living and processes.processes[12].living


def test_final_snapshot_captures_child_spawned_before_parent_suspension():
    tree, processes = wire.WorkerTree(), FakeProcesses()
    tree.ps = processes
    tree.owned = {10: 100.0}
    root = processes.add(10, 100.0)
    original_suspend = root.suspend

    def spawning_suspend():
        processes.add(11, 101.0, parent=10)
        original_suspend()

    root.suspend = spawning_suspend
    assert tree.stop(timeout=0.2, grace=0)
    assert tree.owned == {10: 100.0, 11: 101.0}
    assert ("kill", 11) in processes.events
    assert processes.events.index(("kill", 11)) < processes.events.index(("kill", 10))


def test_parent_reused_during_enumeration_cannot_lend_ownership():
    tree, processes = wire.WorkerTree(), FakeProcesses()
    tree.ps = processes
    tree.owned = {10: 100.0}
    processes.add(10, 100.0)
    walk = processes._ppid_map

    def replaced_parent():
        processes.add(10, 900.0)
        processes.add(11, 901.0, parent=10)
        return walk()

    processes._ppid_map = replaced_parent
    tree.capture()
    processes._ppid_map = walk
    assert tree.owned == {10: 100.0}
    assert tree.stop(timeout=0.2, grace=0)
    assert processes.processes[11].living
    assert not any(action in {"kill", "suspend"} for action, _ in processes.events)


def test_child_pid_reused_after_the_walk_is_not_adopted():
    """The walk saw 11 as 10's child; before 11's creation time was read, 11 exited and its PID
    went to a stranger younger than 10. The second walk shows the stranger's own parent."""
    tree, processes = wire.WorkerTree(), FakeProcesses()
    tree.ps = processes
    tree.owned = {10: 100.0}
    processes.add(10, 100.0)
    processes.add(11, 101.0, parent=10)
    walk = processes._ppid_map

    def then_reused():
        table = walk()
        processes._ppid_map = walk
        processes.add(11, 950.0, parent=999)
        return table

    processes._ppid_map = then_reused
    assert tree.capture() and tree.owned == {10: 100.0}
    assert tree.stop(timeout=0.2, grace=0)
    assert processes.processes[11].living
    assert ("suspend", 11) not in processes.events and ("kill", 11) not in processes.events


@pytest.mark.parametrize("unreadable", ["walk", "child"])
def test_unreadable_live_ancestry_refuses_cleanup_then_retries_safely(unreadable):
    tree, processes = wire.WorkerTree(), FakeProcesses()
    tree.ps = processes
    tree.owned = {10: 100.0}
    root = processes.add(10, 100.0)
    child = processes.add(11, 101.0, parent=10)
    target, name = (processes, "_ppid_map") if unreadable == "walk" else (child, "create_time")
    readable = getattr(target, name)

    def denied():
        raise psutil.AccessDenied(11)

    setattr(target, name, denied)
    assert not tree.stop(timeout=0.2, grace=0)
    assert root.living and child.living
    assert not any(action == "kill" for action, _ in processes.events)
    assert ("resume", 10) in processes.events  # a failed inspection leaves no suspension
    setattr(target, name, readable)
    assert tree.stop(timeout=0.2, grace=0)
    assert not root.living and not child.living
    assert processes.events.index(("kill", 11)) < processes.events.index(("kill", 10))


def test_children_of_an_exited_owned_parent_are_captured():
    """Windows keeps an exited parent's PID as its children's parent: an interpreter whose
    launcher exited before any scan saw it, and a grandchild whose captured parent exited,
    are still captured, and killed at stop; a process older than the owned parent is not its."""
    tree, processes = wire.WorkerTree(), FakeProcesses()
    tree.ps = processes
    tree.owned = {10: 100.0, 20: 200.0}
    processes.add(10, 100.0).living = False  # the launcher, gone before a scan saw its child
    processes.add(11, 101.0, parent=10)
    processes.add(12, 99.0, parent=10)  # 10's PID had another holder when 12 was born
    processes.add(20, 200.0).living = False  # a captured helper, gone since
    processes.add(21, 201.0, parent=20)  # born after the last scan
    processes.add(22, 202.0, parent=21)
    assert tree.capture()
    assert tree.owned == {10: 100.0, 20: 200.0, 21: 201.0, 22: 202.0, 11: 101.0}
    assert tree.stop(timeout=0.2, grace=0)
    kills = [pid for action, pid in processes.events if action == "kill"]
    assert sorted(kills) == [11, 21, 22] and kills.index(22) < kills.index(21)
    assert processes.processes[12].living


def test_a_reused_parent_pid_lends_only_children_born_before_its_new_holder():
    tree, processes = wire.WorkerTree(), FakeProcesses()
    tree.ps = processes
    tree.owned = {10: 100.0}
    processes.add(10, 900.0)  # the owned 10 exited; a stranger born at 900 holds its PID
    processes.add(11, 101.0, parent=10)  # born to the owned 10
    processes.add(12, 901.0, parent=10)  # born to the stranger
    assert tree.capture() and tree.owned == {10: 100.0, 11: 101.0}
    processes.processes[10].living = False  # the stranger exits, and one more of its children
    processes.add(13, 950.0, parent=10)  # shows its stale parent PID: still not ours
    assert tree.capture() and tree.owned == {10: 100.0, 11: 101.0}
    assert tree.stop(timeout=0.2, grace=0)
    assert [pid for action, pid in processes.events if action == "kill"] == [11]


def test_no_child_found_after_its_parent_left_the_table_is_its():
    """Once a walk lacks an owned parent, its children were all in that walk: a process that
    names its PID later is a later holder's."""
    tree, processes = wire.WorkerTree(), FakeProcesses()
    tree.ps = processes
    tree.owned = {10: 100.0}
    processes.add(10, 100.0).living = False
    assert tree.capture() and tree.owned == {10: 100.0}
    processes.add(11, 950.0, parent=10)
    assert tree.capture() and tree.owned == {10: 100.0}
    assert tree.stop(timeout=0.2, grace=0)
    assert processes.processes[11].living


def test_one_walk_per_scan_and_one_more_only_for_a_new_process():
    tree, processes = wire.WorkerTree(), FakeProcesses()
    tree.ps = processes
    tree.owned = {10: 100.0}
    processes.add(10, 100.0)
    processes.add(11, 101.0, parent=10)
    processes.add(12, 102.0, parent=11)
    assert tree.capture() and tree.owned == {10: 100.0, 11: 101.0, 12: 102.0}
    assert processes.events == [("walk", None)] * 2
    processes.events.clear()
    assert tree.capture()
    assert processes.events == [("walk", None)]


def test_zombie_detection_follows_the_platform():
    assert wire.WorkerTree()._zombies == (not psutil.WINDOWS)


@pytest.mark.parametrize("zombies", [True, False])
def test_a_zombie_is_exited_only_where_there_are_zombies(zombies):
    """POSIX: a zombie has exited and is not suspended or killed. Windows has none, so no
    status() is asked and a process reported as one is still live."""
    tree, processes = wire.WorkerTree(), FakeProcesses()
    tree.ps, tree._zombies = processes, zombies
    tree.owned = {10: 100.0, 11: 101.0}
    processes.add(10, 100.0)
    zombie = processes.add(11, 101.0, parent=10)
    zombie.status = lambda: psutil.STATUS_ZOMBIE
    assert tree._matching(11) is (None if zombies else zombie)
    assert tree.stop(timeout=0.2, grace=0)
    assert (("suspend", 11) in processes.events) is not zombies
    assert (("kill", 11) in processes.events) is not zombies
    assert ("kill", 10) in processes.events


def test_windows_scans_never_ask_status():
    """On Windows status() snapshots every thread on the system while holding the GIL (about
    15 ms a call, and the audio callback waits), and Windows has no zombies to find."""
    tree, processes = wire.WorkerTree(), FakeProcesses()
    tree.ps, tree._zombies = processes, False
    tree.owned = {10: 100.0}
    root = processes.add(10, 100.0)
    processes.add(11, 101.0, parent=10)

    def forbidden():
        raise AssertionError("status() asked on Windows")

    root.status = processes.processes[11].status = forbidden
    assert tree.capture() and tree.owned == {10: 100.0, 11: 101.0}
    assert tree.stop(timeout=0.2, grace=0)
    assert ("kill", 11) in processes.events and ("kill", 10) in processes.events


def test_the_background_scan_ends_once_settled(monkeypatch):
    """A scan walks every process on the system with the GIL held: it runs on a timer only
    while the worker loads, and never once the worker has answered."""
    monkeypatch.setattr(wire, "WATCH_S", 0.01)
    tree, processes = wire.WorkerTree(), FakeProcesses()
    tree.ps = processes
    processes.add(10, 100.0)
    tree.attach(SimpleNamespace(pid=10))
    wait_until(lambda: processes.events.count(("walk", None)) >= 3)
    tree.settle()
    tree._thread.join(2)
    assert not tree._thread.is_alive()
    scans = len(processes.events)
    time.sleep(0.1)
    assert len(processes.events) == scans
    assert tree.stop(timeout=0.2, grace=0)


def test_a_started_worker_has_no_background_scan(workers):
    worker, pid_file, _, _ = workers("hang")
    worker.start()
    child, created = tracked_child(worker, pid_file)
    worker._tree._thread.join(wire.WATCH_S + 2)
    assert not worker._tree._thread.is_alive()
    assert worker._tree.owned[child.pid] == created  # captured at READY, not by the timer
    worker.stop()
    assert not original_running(child, created)


def test_missing_tracker_dependency_refuses_before_launch(monkeypatch, tmp_path):
    real_import = builtins.__import__

    def imports(name, *args, **kwargs):
        if name == "psutil":
            raise ModuleNotFoundError("psutil")
        return real_import(name, *args, **kwargs)

    def forbidden_launch(*args, **kwargs):
        raise AssertionError("must not launch an untracked worker")

    monkeypatch.setattr(builtins, "__import__", imports)
    monkeypatch.setattr(expressive.subprocess, "Popen", forbidden_launch)
    worker = expressive.WorkerProcess(sys.executable, tmp_path / "unused.py")
    with pytest.raises(EngineError, match="process tracking requires psutil"):
        worker.start()
    assert worker.process is None
