"""leeroy_mind.lifecycle as the console uses it: structured outcomes, the console's mind handle
and the voice service's control. Every process, port and subprocess is a fake."""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from leeroy_mind import lifecycle
from leeroy_mind.console.control import (Controls, MindHandle, Refused, VoiceHandle,
                                         control_problems)
from leeroy_mind.lifecycle import OwnershipError, Process, VoiceControl, parse_where


class Clock:
    def __init__(self):
        self.t = 0.0

    def sleep(self, s):
        self.t += s

    def monotonic(self):
        return self.t


class Processes:
    def __init__(self, rows=()):
        self.rows, self.killed, self.closed, self.opened = list(rows), [], [], []

    def snapshot(self):
        return list(self.rows)

    def open(self, row):
        self.opened.append(row.pid)
        return (row.pid, row.created)

    def exited(self, handle, seconds=0.0):
        return not any((r.pid, r.created) == handle for r in self.rows)

    def terminate(self, handle):
        self.killed.append(handle)
        self.rows = [r for r in self.rows if (r.pid, r.created) != handle]

    def close(self, handle):
        self.closed.append(handle)


# -- the mind ------------------------------------------------------------------------------------

@pytest.fixture
def mind(tmp_path):
    root = tmp_path / "checkout"
    python, base = lifecycle.venv_python(root), tmp_path / "base/python.exe"
    data = tmp_path / "mind-data"
    data.mkdir()
    (data / "mind.toml").write_text("", encoding="utf-8")
    (data / "mind.db").write_bytes(b"synthetic")
    args = (str(python), "-m", "leeroy_mind", "run", "--data-dir", str(data))
    launcher = Process(100, 20, 1110, str(python), args)
    child = Process(101, 100, 1120, str(base), args)
    return SimpleNamespace(root=root, python=python, base=base, data=data, launcher=launcher,
                           child=child)


def handle(m, processes, *, spawn=None, clock=None):
    clock = clock or Clock()
    return MindHandle(m.root, m.data, python=m.python, base_python=str(m.base),
                      processes=processes, locked=lambda _: bool(processes.rows),
                      spawn=spawn or (lambda *a: pytest.fail("unexpected spawn")),
                      sleep=clock.sleep, monotonic=clock.monotonic, timeout=1)


def test_mind_action_reports_structured_outcomes(mind):
    processes = Processes([mind.launcher, mind.child])
    kw = dict(processes=processes, locked=lambda _: True)
    out = lifecycle.mind_action("status", mind.root, mind.data, mind.python, mind.base, **kw)
    assert (out.code, out.state, out.pid) == (0, "running", 101)
    assert out.message == "mind pid 101 (running)"
    out = lifecycle.mind_action("status", mind.root, mind.data, mind.python, mind.base,
                                processes=processes, locked=lambda _: False)
    assert (out.code, out.state) == (1, "starting")
    out = lifecycle.mind_action("status", mind.root, mind.data, mind.python, mind.base,
                                processes=Processes(), locked=lambda _: False)
    assert (out.code, out.state, out.message) == (0, "stopped", "mind stopped")


def test_run_action_prints_the_scripts_lines(mind, capsys):
    processes = Processes([mind.launcher, mind.child])
    assert lifecycle.run_action("status", mind.root, mind.data, mind.python, mind.base,
                                processes=processes, locked=lambda _: True) == 0
    assert capsys.readouterr().out == "    mind pid 101 (running)\n"


def test_handle_probe_names_the_family_and_ambiguity(mind):
    h = handle(mind, Processes([mind.launcher, mind.child]))
    found, held, error = h.probe()
    assert [p.pid for p in found] == [101, 100] and held and error is None
    other = Process(201, 20, 2, mind.launcher.exe, mind.launcher.argv)
    found, held, error = handle(mind, Processes([mind.launcher, other])).probe()
    assert found is None and "multiple" in error


def test_handle_probe_hides_a_process_list_failure_payload(mind):
    processes = Processes()

    def broken():
        raise OSError("C:/private/command line --secret")
    processes.snapshot = broken
    found, held, error = handle(mind, processes).probe()
    assert found is None and error == "process list unavailable (OSError)"


def test_handle_restart_stops_the_verified_family_then_starts(mind):
    processes = Processes([mind.launcher, mind.child])
    launches = []

    def spawn(root, data, python):
        processes.rows = [mind.launcher, mind.child]
        launches.append(python)
        return SimpleNamespace(pid=100, poll=lambda: None)
    h = handle(mind, processes, spawn=spawn)
    out = h.act("restart")
    assert processes.killed == [(101, 1120), (100, 1110)]
    assert launches == [mind.python]
    assert out.state == "running" and out.pid == 101
    assert h.launch is not None and h.launch_exit() is None


def test_handle_keeps_its_launch_to_tell_a_crash_from_a_stop(mind):
    processes = Processes()
    code = [None]

    def spawn(*a):
        processes.rows = [mind.launcher, mind.child]
        return SimpleNamespace(pid=100, poll=lambda: code[0])
    h = handle(mind, processes, spawn=spawn)
    h.act("start")
    code[0] = 3
    assert h.launch_exit() == 3
    processes.rows = [mind.launcher, mind.child]
    h.act("stop")
    assert h.launch is None and h.launch_exit() is None


# -- control rules -------------------------------------------------------------------------------

def test_controls_refuse_everything_when_off():
    controls = Controls(enabled=False, reason="process control is off (--no-control)",
                        mind=SimpleNamespace(act=lambda a: pytest.fail("acted")))
    with pytest.raises(Refused) as e:
        asyncio.run(controls.act("mind", "stop"))
    assert e.value.status == 403 and "--no-control" in str(e.value)
    assert controls.describe()["services"] == {"mind": "process control is off (--no-control)",
                                               "voice": "process control is off (--no-control)"}


def test_controls_name_why_a_service_cannot_be_controlled():
    controls = Controls(enabled=True, mind=SimpleNamespace(act=lambda a: None),
                        problems={"voice": "no voice venv at X"})
    with pytest.raises(Refused, match="no voice venv"):
        controls.check("voice", "start")
    with pytest.raises(Refused) as e:
        controls.check("mind", "explode")
    assert e.value.status == 400


def test_controls_run_one_action_at_a_time_and_translate_ownership_errors():
    async def scenario():
        gate = asyncio.Event()
        started = asyncio.Event()

        def slow(action):
            started.set()
            asyncio.run_coroutine_threadsafe(gate.wait(), loop).result()
            raise OwnershipError("mind.lock is held but its mind process is unidentified")
        loop = asyncio.get_running_loop()
        controls = Controls(enabled=True, mind=SimpleNamespace(act=slow),
                            voice=SimpleNamespace(act=lambda a: None))
        first = asyncio.create_task(controls.act("mind", "stop"))
        await started.wait()
        with pytest.raises(Refused, match="still running"):
            await controls.act("voice", "start")
        gate.set()
        with pytest.raises(Refused) as e:
            await first
        assert e.value.status == 409 and "unidentified" in str(e.value)
        assert controls.busy is None
    asyncio.run(scenario())


def test_controls_never_show_an_unexpected_errors_text():
    def boom(action):
        raise RuntimeError("C:/Users/someone/private payload")
    controls = Controls(enabled=True, mind=SimpleNamespace(act=boom))
    with pytest.raises(Refused) as e:
        asyncio.run(controls.act("mind", "start"))
    assert "private" not in str(e.value) and "RuntimeError" in str(e.value)


def test_control_problems_follow_the_scripts_venv_rule(tmp_path):
    root = tmp_path / "checkout"
    assert control_problems(root, platform="linux") == {
        "mind": "process control is supported only on Windows",
        "voice": "process control is supported only on Windows"}
    problems = control_problems(root, platform="win32", executable=str(tmp_path / "other.exe"))
    assert "only that venv may start or stop the mind" in problems["mind"]
    assert "no voice venv" in problems["voice"]
    python = lifecycle.venv_python(root)
    python.parent.mkdir(parents=True)
    python.write_bytes(b"")
    assert control_problems(root, platform="win32", executable=str(python))["mind"] is None


# -- the voice service ---------------------------------------------------------------------------

def test_parse_where_reads_the_services_three_lines(tmp_path):
    data = tmp_path / "voice-data"
    where = parse_where(f"data_dir={data}\r\nmind_port=7879\r\nui_port=7880\r\n")
    assert (where.data_dir, where.mind_port, where.ui_port) == (data, 7879, 7880)
    for bad in ("", "data_dir=rel\nmind_port=1\nui_port=2", f"data_dir={data}\nmind_port=x\nui_port=1",
                f"data_dir={data}\nmind_port=0\nui_port=1"):
        with pytest.raises(OwnershipError, match="bad output"):
            parse_where(bad)


class VoiceRig:
    """A fake voice service: `where`, `held`, a launch, its ports and its process."""

    def __init__(self, tmp_path):
        self.root = tmp_path / "checkout"
        scripts = self.root / "voice" / ".venv" / "Scripts"
        scripts.mkdir(parents=True)
        for exe in ("python.exe", "pythonw.exe"):
            (scripts / exe).write_bytes(b"")
        self.data = tmp_path / "voice-data"
        self.pid = None
        self.ports: set[int] = set()
        self.commands: list[str] = []
        self.launched: list[list[str]] = []
        self.kills: list[int] = []
        self.clock = Clock()
        self.row = Process(500, 499, 7, str(scripts / "pythonw.exe"),
                           (str(scripts / "pythonw.exe"), "-m", "leeroy_voice", "run"))
        self.processes = Processes()
        self.close_works = True

    def run(self, argv, **kw):
        command = argv[-1]
        self.commands.append(command)
        if command == "where":
            out = f"data_dir={self.data}\nmind_port=7879\nui_port=7880\n"
            return SimpleNamespace(returncode=0, stdout=out)
        if command == "held":
            if self.pid is None:
                return SimpleNamespace(returncode=1, stdout="")
            return SimpleNamespace(returncode=0, stdout=f"{self.pid or ''}\n")
        raise AssertionError(command)

    def popen(self, argv, **kw):
        self.launched.append(list(argv))
        self.pid = 500
        self.processes.rows = [self.row]
        self.ports = {7879, 7880}
        return SimpleNamespace(pid=499, poll=lambda: None)

    def kill(self, pid):
        self.kills.append(pid)
        if self.close_works:
            self.pid = None
            self.processes.rows = []

    def control(self):
        def terminate(handle):
            self.processes.killed.append(handle)
            self.processes.rows = []
            self.pid = None
        self.processes.terminate = terminate
        return VoiceControl(self.root, processes=self.processes, run=self.run, popen=self.popen,
                            listening=lambda: set(self.ports), kill=self.kill,
                            sleep=self.clock.sleep, monotonic=self.clock.monotonic)


def test_voice_start_launches_pythonw_and_waits_for_both_ports(tmp_path):
    rig = VoiceRig(tmp_path)
    out = rig.control().start()
    assert out.state == "running" and "7879 and 7880" in out.message
    argv = rig.launched[0]
    assert argv[0].endswith("pythonw.exe") and argv[1:] == ["-m", "leeroy_voice", "run"]
    assert (rig.data / "logs" / "voice-stdout.log").is_file()
    assert rig.commands[:2] == ["held", "where"]


def test_voice_start_does_not_launch_a_second_service(tmp_path):
    rig = VoiceRig(tmp_path)
    rig.pid = 500
    out = rig.control().start()
    assert out.message == "voice already running, pid 500" and rig.launched == []


def test_voice_start_reports_an_early_exit_and_a_timeout(tmp_path):
    rig = VoiceRig(tmp_path)
    rig.popen = lambda argv, **kw: SimpleNamespace(pid=1, poll=lambda: 2)
    with pytest.raises(OwnershipError, match="exited with code 2"):
        rig.control().start()
    rig = VoiceRig(tmp_path / "again")
    rig.popen = lambda argv, **kw: SimpleNamespace(pid=1, poll=lambda: None)
    with pytest.raises(OwnershipError, match="not listening after 30 s"):
        rig.control().start()


def test_voice_stop_asks_it_to_close_first(tmp_path):
    rig = VoiceRig(tmp_path)
    rig.pid, rig.processes.rows = 500, [rig.row]
    out = rig.control().stop()
    assert out.message == "voice stopped" and rig.kills == [500]
    assert rig.processes.killed == [] and rig.processes.closed == [(500, 7)]


def test_voice_stop_terminates_the_verified_process_after_five_seconds(tmp_path):
    rig = VoiceRig(tmp_path)
    rig.pid, rig.processes.rows = 500, [rig.row]
    rig.close_works = False
    out = rig.control().stop()
    assert out.message == "voice terminated after 5 s"
    assert rig.processes.killed == [(500, 7)] and rig.processes.closed == [(500, 7)]


def test_voice_stop_refuses_a_pid_that_is_not_this_checkouts_service(tmp_path):
    rig = VoiceRig(tmp_path)
    rig.pid = 500
    rig.processes.rows = [Process(500, 1, 7, "C:/other/pythonw.exe",
                                  ("C:/other/pythonw.exe", "-m", "leeroy_voice", "run"))]
    with pytest.raises(OwnershipError, match="not this checkout's voice service"):
        rig.control().stop()
    rig.processes.rows = [Process(500, 1, 7, rig.row.exe, (rig.row.exe, "-m", "something"))]
    with pytest.raises(OwnershipError, match="not this checkout's"):
        rig.control().stop()
    assert rig.kills == [] and rig.processes.killed == []


def test_voice_stop_refuses_a_lock_without_a_pid_and_is_idempotent(tmp_path):
    rig = VoiceRig(tmp_path)
    assert rig.control().stop().message == "voice already stopped"
    rig.pid = 0
    with pytest.raises(OwnershipError, match="names nobody"):
        rig.control().stop()


def test_voice_restart_through_the_console_handle(tmp_path):
    rig = VoiceRig(tmp_path)
    rig.pid, rig.processes.rows = 500, [rig.row]
    out = VoiceHandle(rig.control()).act("restart")
    assert rig.kills == [500] and len(rig.launched) == 1 and out.state == "running"


def test_voice_control_without_a_venv_says_so(tmp_path):
    control = VoiceControl(tmp_path / "nowhere")
    assert "no voice venv" in control.missing()
    with pytest.raises(OwnershipError, match="no voice venv"):
        control.start()


def test_voice_commands_run_without_the_minds_python_path(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "C:/mind")
    rig = VoiceRig(tmp_path)
    seen = {}

    def run(argv, **kw):
        seen.update(kw)
        return rig.run(argv, **kw)
    VoiceControl(rig.root, run=run).where()
    assert "PYTHONPATH" not in seen["env"] and Path(seen["cwd"]) == rig.root / "voice"
