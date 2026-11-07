"""How the mind and the voice service are identified and launched when the console runs under the
logon task's pythonw.exe: the mind's base interpreter comes from the venv's pyvenv.cfg (python.exe,
whatever runs the console), and both services start outside the task's job at normal priority.
Every process and launch is a fake."""
import logging
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from leeroy_mind import lifecycle
from leeroy_mind.console import cli
from leeroy_mind.console.control import control_problems
from leeroy_mind.lifecycle import (CREATE_BREAKAWAY_FROM_JOB, CREATE_NEW_PROCESS_GROUP,
                                   CREATE_NO_WINDOW, DETACHED_PROCESS, NORMAL_PRIORITY_CLASS,
                                   OwnershipError, Process)


class Denied(PermissionError):
    winerror = 5  # ERROR_ACCESS_DENIED: the job forbids breakaway


class Missing(FileNotFoundError):
    winerror = 2


def checkout(tmp_path, home=None):
    """A checkout whose mind venv names `home` in pyvenv.cfg (None: no pyvenv.cfg)."""
    root = tmp_path / "checkout"
    scripts = root / "mind" / ".venv" / "Scripts"
    scripts.mkdir(parents=True)
    if home is not None:
        (root / "mind" / ".venv" / "pyvenv.cfg").write_text(
            f"home = {home}\ninclude-system-site-packages = false\nversion = 3.14.10\n"
            f"executable = {home}{os.sep}python.exe\n", encoding="utf-8")
    return root


# -- the base interpreter ------------------------------------------------------------------------

def test_the_minds_base_is_python_exe_in_the_venvs_home(tmp_path):
    home = tmp_path / "Python314"
    root = checkout(tmp_path, home)
    assert lifecycle.mind_base_python(root) == home / "python.exe"


def test_an_unreadable_home_names_no_base(tmp_path):
    with pytest.raises(OwnershipError, match="mind venv's base interpreter"):
        lifecycle.mind_base_python(checkout(tmp_path / "none"))
    with pytest.raises(OwnershipError):
        lifecycle.mind_base_python(checkout(tmp_path / "relative", "Python314"))
    root = checkout(tmp_path / "two", tmp_path / "a")
    cfg = root / "mind" / ".venv" / "pyvenv.cfg"
    cfg.write_text(cfg.read_text(encoding="utf-8") + f"home = {tmp_path / 'b'}\n",
                   encoding="utf-8")
    with pytest.raises(OwnershipError):
        lifecycle.mind_base_python(root)


class Processes:
    def __init__(self, rows):
        self.rows = list(rows)

    def snapshot(self):
        return list(self.rows)


def test_under_pythonw_the_console_still_finds_the_whole_mind(tmp_path, monkeypatch):
    home = tmp_path / "Python314"
    root = checkout(tmp_path, home)
    data = tmp_path / "mind-data"
    data.mkdir()
    # What the logon task's console sees: its own interpreter is pythonw.exe
    monkeypatch.setattr(sys, "_base_executable", str(home / "pythonw.exe"), raising=False)
    python = lifecycle.venv_python(root)
    args = (str(python), "-m", "leeroy_mind", "run", "--data-dir", str(data))
    launcher = Process(100, 20, 1110, str(python), args)
    child = Process(101, 100, 1120, str(home / "python.exe"), args)  # the mind itself
    mind = cli.mind_handle(root, data, processes=Processes([launcher, child]), link=None)
    mind.locked = lambda _: True
    assert mind.base_python == str(home / "python.exe")
    found, held, error = mind.probe()
    assert error is None and held and [p.pid for p in found] == [101, 100]


def test_a_console_with_no_base_identifies_nothing(tmp_path):
    root = checkout(tmp_path)  # no pyvenv.cfg
    data = tmp_path / "mind-data"
    data.mkdir()
    mind = cli.mind_handle(root, data, processes=Processes([]), link=None)
    mind.locked = lambda _: False
    assert mind.base_python is None
    found, _, error = mind.probe()
    assert found is None and "base interpreter" in error
    with pytest.raises(OwnershipError, match="base interpreter"):
        mind.act("start")


def test_the_venvs_pythonw_may_control_the_mind(tmp_path):
    root = checkout(tmp_path, tmp_path / "Python314")
    scripts = root / "mind" / ".venv" / "Scripts"
    for exe in ("python.exe", "pythonw.exe"):
        problems = control_problems(root, platform="win32", executable=str(scripts / exe))
        assert problems["mind"] is None, exe
    problems = control_problems(root, platform="win32",
                                executable=str(tmp_path / "Python314" / "pythonw.exe"))
    assert "only that venv" in problems["mind"]


@pytest.mark.skipif(os.name != "nt", reason="ac-mind.py controls the mind on Windows only")
def test_ac_mind_takes_the_base_from_pyvenv_cfg(tmp_path, monkeypatch):
    import importlib.util
    script = Path(__file__).resolve().parents[3] / "scripts" / "ac-mind.py"
    monkeypatch.setattr(sys, "path", list(sys.path))  # the script and main() each add to it
    spec = importlib.util.spec_from_file_location("ac_mind_under_test", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    home = tmp_path / "Python314"
    root = checkout(tmp_path, home)
    data = tmp_path / "mind-data"
    data.mkdir()
    monkeypatch.setattr(sys, "executable", str(lifecycle.venv_python(root)))
    monkeypatch.setattr(sys, "_base_executable", str(home / "pythonw.exe"), raising=False)
    seen = {}

    def run_action(action, root, data, python, base, **kw):
        seen.update(action=action, base=base)
        return 0
    monkeypatch.setattr(module, "run_action", run_action)
    monkeypatch.setattr(module, "WindowsProcesses", lambda: None)
    assert module.main(["status", "--root", str(root), "--data-dir", str(data)]) == 0
    assert seen == {"action": "status", "base": home / "python.exe"}


# -- the launches --------------------------------------------------------------------------------

class Popen:
    """Records each launch's creation flags; `refuse` is raised by the first launch."""

    def __init__(self, refuse=None):
        self.flags, self.refuse = [], refuse

    def __call__(self, argv, **kw):
        self.flags.append(kw["creationflags"])
        if self.refuse is not None and len(self.flags) == 1:
            raise self.refuse
        return SimpleNamespace(pid=4242, poll=lambda: None)


def test_the_mind_starts_outside_the_job_at_normal_priority(tmp_path, caplog):
    popen = Popen()
    data = tmp_path / "mind-data"
    with caplog.at_level(logging.WARNING, logger="leeroy_mind.lifecycle"):
        launch = lifecycle.spawn_mind(tmp_path, data, tmp_path / "python.exe", popen=popen)
    assert launch.pid == 4242
    assert popen.flags == [CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP | NORMAL_PRIORITY_CLASS
                           | CREATE_BREAKAWAY_FROM_JOB]
    assert caplog.records == []


def test_a_job_that_forbids_breakaway_starts_the_mind_inside_it(tmp_path, caplog):
    popen = Popen(refuse=Denied(13, "Access is denied"))
    with caplog.at_level(logging.WARNING, logger="leeroy_mind.lifecycle"):
        launch = lifecycle.spawn_mind(tmp_path, tmp_path / "mind-data", tmp_path / "python.exe",
                                      popen=popen)
    assert launch.pid == 4242
    base = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP | NORMAL_PRIORITY_CLASS
    assert popen.flags == [base | CREATE_BREAKAWAY_FROM_JOB, base]
    assert [r.getMessage().split(":")[0] for r in caplog.records] == ["mind_breakaway_refused"]


def test_any_other_launch_failure_is_not_retried(tmp_path):
    popen = Popen(refuse=Missing(2, "The system cannot find the file specified"))
    with pytest.raises(FileNotFoundError):
        lifecycle.spawn_mind(tmp_path, tmp_path / "mind-data", tmp_path / "python.exe",
                             popen=popen)
    assert len(popen.flags) == 1


class Voice:
    """Just enough of a voice venv for VoiceControl.start: held, where and the two ports."""

    def __init__(self, tmp_path, popen):
        self.root = tmp_path / "checkout"
        scripts = self.root / "voice" / ".venv" / "Scripts"
        scripts.mkdir(parents=True)
        for exe in ("python.exe", "pythonw.exe"):
            (scripts / exe).write_bytes(b"")
        self.data = tmp_path / "voice-data"
        self.popen, self.up = popen, False

    def run(self, argv, **kw):
        if argv[-1] == "held":
            return SimpleNamespace(returncode=1, stdout="")
        return SimpleNamespace(returncode=0, stdout=f"data_dir={self.data}\nmind_port=17879\n"
                                                    "ui_port=17880\n")

    def launch(self, argv, **kw):
        out = self.popen(argv, **kw)
        self.up = True
        return out

    def control(self):
        return lifecycle.VoiceControl(self.root, run=self.run, popen=self.launch,
                                      listening=lambda: {17879, 17880} if self.up else set(),
                                      sleep=lambda s: None)


def test_the_voice_starts_outside_the_job_at_normal_priority(tmp_path, caplog):
    popen = Popen()
    with caplog.at_level(logging.WARNING, logger="leeroy_mind.lifecycle"):
        out = Voice(tmp_path, popen).control().start()
    assert out.state == "running"
    assert popen.flags == [DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | NORMAL_PRIORITY_CLASS
                           | CREATE_BREAKAWAY_FROM_JOB]
    assert caplog.records == []


def test_a_job_that_forbids_breakaway_starts_the_voice_inside_it(tmp_path, caplog):
    popen = Popen(refuse=Denied(13, "Access is denied"))
    with caplog.at_level(logging.WARNING, logger="leeroy_mind.lifecycle"):
        out = Voice(tmp_path, popen).control().start()
    assert out.state == "running"
    base = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | NORMAL_PRIORITY_CLASS
    assert popen.flags == [base | CREATE_BREAKAWAY_FROM_JOB, base]
    assert [r.getMessage().split(":")[0] for r in caplog.records] == ["voice_breakaway_refused"]


def test_the_flags_are_windows_own():
    assert (DETACHED_PROCESS, NORMAL_PRIORITY_CLASS, CREATE_NEW_PROCESS_GROUP,
            CREATE_BREAKAWAY_FROM_JOB, CREATE_NO_WINDOW) == (
        0x8, 0x20, 0x200, 0x01000000, 0x08000000)
    if os.name == "nt":
        import subprocess
        assert (subprocess.DETACHED_PROCESS, subprocess.NORMAL_PRIORITY_CLASS,
                subprocess.CREATE_NEW_PROCESS_GROUP, subprocess.CREATE_BREAKAWAY_FROM_JOB,
                subprocess.CREATE_NO_WINDOW) == (DETACHED_PROCESS, NORMAL_PRIORITY_CLASS,
                                                 CREATE_NEW_PROCESS_GROUP,
                                                 CREATE_BREAKAWAY_FROM_JOB, CREATE_NO_WINDOW)
