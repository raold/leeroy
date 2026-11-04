"""Start, stop and identify the mind and the voice service; shared by scripts/ac-mind.py and the
guild console (leeroy_mind.console). Never selects Python by name alone.

The mind is identified by its command line: this checkout's venv python (or the base python
its launcher starts) running `-m leeroy_mind run --data-dir <absolute data dir>`. A held
mind.lock with no such process, two matching families, or a parent PID that changed identity
is ambiguous, and every action refuses it. Stopping is forced, as for the server script's other
console apps. Captured process handles and creation times keep PID reuse from targeting
another process. An unidentified held lock always requires manual intervention.

The voice service is located as scripts/ac-voice.sh locates it: its own `python -m
leeroy_voice where` names the data dir and ports, and `held` the pid holding voice.lock. That
pid must also be this checkout's voice service (the base interpreter its venv launcher started,
running `-m leeroy_voice run` under that launcher) before it is asked to close (WM_CLOSE, so
WoW's volume is restored) and, after five seconds, terminated.

Nothing here prints: an action returns an Outcome, whose message the script prints. Errors are
OwnershipError with a message that never carries a raw process command line."""
import ctypes
import json
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path


class OwnershipError(RuntimeError):
    pass


@dataclass(frozen=True)
class Process:
    pid: int
    parent: int
    created: int
    exe: str
    argv: tuple[str, ...]


@dataclass(frozen=True)
class Outcome:
    """What an action did. code 0 is success; status gives 1 while the mind is starting or
    stopping. state is the service's state afterwards: running, starting or stopped."""
    code: int
    message: str
    state: str = ""
    pid: int | None = None


def canonical(path):
    return os.path.normcase(str(Path(path).resolve()))


def venv_python(root) -> Path:
    return Path(root) / "mind" / ".venv" / "Scripts" / "python.exe"


# -- the mind ----------------------------------------------------------------------------------

def matches(process, python, base_python, data):
    allowed = {canonical(python), canonical(base_python)}
    args = process.argv
    # A target's working directory is not this helper's working directory. Never
    # resolve relative command-line paths on its behalf when identifying an owner.
    if (not Path(process.exe).is_absolute() or len(args) < 5 or not Path(args[0]).is_absolute()
            or canonical(process.exe) not in allowed
            or canonical(args[0]) not in allowed or args[1:4] != ("-m", "leeroy_mind", "run")):
        return False
    # Accept the run command's documented optional arguments, never another Python job.
    options, index = {}, 4
    while index < len(args):
        key, separator, value = args[index].partition("=")
        if key not in ("--data-dir", "--log-level", "--ollama-conf") or key in options:
            return False
        if not separator:
            index += 1
            if index == len(args):
                return False
            value = args[index]
        options[key] = value
        index += 1
    return ("--data-dir" in options and Path(options["--data-dir"]).is_absolute()
            and canonical(options["--data-dir"]) == canonical(data))


def family(processes, python, base_python, data):
    found = {p.pid: p for p in processes if matches(p, python, base_python, data)}
    if any(p.parent in found and p.created < found[p.parent].created for p in found.values()):
        raise OwnershipError("mind parent PID changed identity; refusing ambiguous ancestry")
    roots = [p for p in found.values() if p.parent not in found]
    if found and len(roots) != 1:
        raise OwnershipError("multiple mind process families match; refusing an ambiguous action")
    ordered = []
    while found:
        leaves = [p for p in found.values() if not any(q.parent == p.pid for q in found.values())]
        if not leaves:
            raise OwnershipError("mind process ancestry is inconsistent")
        for p in sorted(leaves, key=lambda p: p.pid):
            ordered.append(p)
            del found[p.pid]
    return ordered  # Actual venv child first, launcher last.


class WindowsProcesses:
    def __init__(self):
        from ctypes import wintypes as w
        self.w = w
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.shell = ctypes.WinDLL("shell32", use_last_error=True)
        self.kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        self.kernel.OpenProcess.restype = w.HANDLE
        self.kernel.CloseHandle.argtypes = [w.HANDLE]
        self.kernel.GetProcessTimes.argtypes = [w.HANDLE, *([ctypes.POINTER(w.FILETIME)] * 4)]
        self.kernel.GetProcessTimes.restype = w.BOOL
        self.kernel.TerminateProcess.argtypes = [w.HANDLE, w.UINT]
        self.kernel.TerminateProcess.restype = w.BOOL
        self.kernel.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
        self.kernel.WaitForSingleObject.restype = w.DWORD
        self.shell.CommandLineToArgvW.argtypes = [w.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
        self.shell.CommandLineToArgvW.restype = ctypes.POINTER(w.LPWSTR)
        self.kernel.LocalFree.argtypes = [ctypes.c_void_p]
        self.kernel.LocalFree.restype = ctypes.c_void_p

    def argv(self, command):
        n = ctypes.c_int()
        args = self.shell.CommandLineToArgvW(command, ctypes.byref(n))
        if not args:
            raise OwnershipError("cannot parse a Python process command line")
        try:
            return tuple(args[i] for i in range(n.value))
        finally:
            self.kernel.LocalFree(args)

    def snapshot(self):
        # No paths, process arguments or private identity values are interpolated.
        script = """$ErrorActionPreference='Stop';
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false);
@(Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
ForEach-Object { [pscustomobject]@{pid=$_.ProcessId;parent=$_.ParentProcessId;
created=$_.CreationDate.ToUniversalTime().ToFileTimeUtc();exe=$_.ExecutablePath;cmd=$_.CommandLine} }) |
ConvertTo-Json -Compress"""
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, encoding="utf-8", timeout=15, check=True,
            creationflags=subprocess.CREATE_NO_WINDOW)
        rows = json.loads(result.stdout or "[]")
        if isinstance(rows, dict):
            rows = [rows]
        return [Process(int(r["pid"]), int(r["parent"]), int(r["created"]), r["exe"], self.argv(r["cmd"]))
                for r in rows if r.get("exe") and r.get("cmd")]

    def open(self, process):
        handle = self.kernel.OpenProcess(0x1000 | 0x100000 | 0x0001, False, process.pid)
        if not handle:
            if ctypes.get_last_error() == 87:  # It has already exited.
                return None
            raise OwnershipError("cannot obtain a verified mind process handle")
        created, exited, kernel, user = (self.w.FILETIME() for _ in range(4))
        if not self.kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                            ctypes.byref(kernel), ctypes.byref(user)):
            self.close(handle)
            raise OwnershipError("cannot verify mind process creation time")
        stamp = (created.dwHighDateTime << 32) | created.dwLowDateTime
        # WMI's DMTF creation time has microsecond precision; the kernel's FILETIME
        # retains one additional decimal place. Compare at the source's precision.
        if stamp // 10 != process.created // 10:
            self.close(handle)
            raise OwnershipError("mind PID changed identity; refusing to stop it")
        return handle

    def exited(self, handle, seconds=0.0):
        """Whether the process has exited, waiting up to `seconds` for it."""
        return self.kernel.WaitForSingleObject(handle, int(seconds * 1000)) == 0

    def terminate(self, handle):
        if self.kernel.WaitForSingleObject(handle, 0) == 0:
            return
        if not self.kernel.TerminateProcess(handle, 1):
            raise OwnershipError("could not terminate the verified mind process")
        if self.kernel.WaitForSingleObject(handle, 5000) != 0:
            raise OwnershipError("mind process did not exit within five seconds")

    def close(self, handle):
        self.kernel.CloseHandle(handle)


def spawn_mind(root, data, python):
    logs = data / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.pop("PYTHONHOME", None)
    env.update(PYTHONPATH=str(root / "mind"), PYTHONNOUSERSITE="1", PYTHONUNBUFFERED="1",
               LEEROY_MIND_DATA=str(data))
    with (logs / "mind-stdout.log").open("ab") as output:
        return subprocess.Popen([str(python), "-m", "leeroy_mind", "run", "--data-dir", str(data)],
            cwd=root / "mind", env=env, stdin=subprocess.DEVNULL, stdout=output,
            stderr=subprocess.STDOUT, close_fds=True,
            creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)


def launch_problem(launch, reason):
    """A failed verifier cannot safely discover all descendants; retain their owner ID."""
    try:
        code = launch.poll()
        state = "still running" if code is None else f"exited with code {code}"
    except Exception:
        state = "state unknown"
    return (f"mind start {reason}; launched pid {launch.pid} is {state}; "
            "descendants may remain unverified. No launched process was terminated; "
            "inspect that launch before retrying. See mind-data/logs/mind-stdout.log")


def mind_action(action, root, data, python, base_python, *, processes, locked,
                spawn=spawn_mind, sleep=time.sleep, monotonic=time.monotonic, timeout=30):
    """status, start or stop the mind on `data`; an Outcome, or OwnershipError."""
    def current():
        return family(processes.snapshot(), python, base_python, data)

    found, held = current(), locked(data)
    if held and not found:
        raise OwnershipError("mind.lock is held but its mind process is unidentified; no action taken")
    if action == "status":
        if found:
            state = "running" if held else "starting"
            return Outcome(0 if held else 1,
                           f"mind pid {found[0].pid} ({'running' if held else 'starting/stopping'})",
                           state, found[0].pid)
        return Outcome(0, "mind stopped", "stopped")
    if action == "start":
        if held:
            return Outcome(0, f"mind already running, pid {found[0].pid}", "running", found[0].pid)
        if found:
            raise OwnershipError("mind process exists without its lock; wait for it to finish before starting another")
        if not (data / "mind.toml").is_file() or not (data / "mind.db").is_file():
            raise OwnershipError("mind data is not initialized; start skipped")
        launch = spawn(root, data, python)
        if type(getattr(launch, "pid", None)) is not int or launch.pid <= 0:
            raise OwnershipError("mind launch returned no process identity; inspect startup before retrying")
        deadline = monotonic() + timeout
        try:
            while monotonic() < deadline:
                found = current()
                if (locked(data) and found and launch.poll() is None
                        and any(p.pid == launch.pid for p in found)):
                    return Outcome(0, f"mind started, pid {found[0].pid}; "
                                      f"log {data / 'logs' / 'mind-stdout.log'}",
                                   "running", found[0].pid)
                sleep(.25)
        except BaseException as exc:
            raise OwnershipError(launch_problem(launch, f"verification failed ({type(exc).__name__})")) from None
        raise OwnershipError(launch_problem(launch, "did not acquire its lock"))
    if action != "stop":
        raise ValueError(f"unknown mind action {action!r}")
    if not found:
        return Outcome(0, "mind already stopped", "stopped")
    handles = []
    try:
        # Verify every identity before terminating any member of the family.
        for process in found:
            handle = processes.open(process)
            if handle is not None:
                handles.append(handle)
        for handle in handles:
            processes.terminate(handle)
    finally:
        for handle in handles:
            processes.close(handle)
    deadline = monotonic() + 5
    while monotonic() < deadline:
        if not locked(data) and not current():
            return Outcome(0, "mind stopped", "stopped")
        sleep(.1)
    raise OwnershipError("mind process or lock remains; servers have not been stopped")


def run_action(action, root, data, python, base_python, *, processes, locked,
               spawn=spawn_mind, sleep=time.sleep, monotonic=time.monotonic, timeout=30):
    """mind_action for scripts/ac-mind.py: prints its message and returns its exit code."""
    outcome = mind_action(action, root, data, python, base_python, processes=processes,
                          locked=locked, spawn=spawn, sleep=sleep, monotonic=monotonic,
                          timeout=timeout)
    print(f"    {outcome.message}")
    return outcome.code


# -- the voice service -------------------------------------------------------------------------

VOICE_START_S = 30  # ac-voice.sh's wait for both ports
VOICE_CLOSE_S = 5  # after WM_CLOSE, before it is terminated
_LISTENING = re.compile(r"^\s*TCP\s+127\.0\.0\.1:(\d+)\s+\S+\s+LISTENING\b", re.MULTILINE)


@dataclass(frozen=True)
class VoiceWhere:
    data_dir: Path
    mind_port: int
    ui_port: int


def parse_where(text: str) -> VoiceWhere:
    """`leeroy_voice where`: data_dir=, mind_port= and ui_port=, one per line."""
    values = {}
    for line in text.splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key in ("data_dir", "mind_port", "ui_port"):
            values[key] = value.strip()
    try:
        where = VoiceWhere(Path(values["data_dir"]), int(values["mind_port"]),
                           int(values["ui_port"]))
    except (KeyError, ValueError):
        raise OwnershipError("bad output from the voice service's where") from None
    if not where.data_dir.is_absolute() or not all(
            0 < p < 65536 for p in (where.mind_port, where.ui_port)):
        raise OwnershipError("bad output from the voice service's where")
    return where


def netstat_listening() -> set[int]:
    """The ports listening on 127.0.0.1, as ac-voice.sh reads them (netstat -an -p tcp)."""
    result = subprocess.run(["netstat", "-an", "-p", "tcp"], capture_output=True, text=True,
                            timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return {int(m) for m in _LISTENING.findall(result.stdout or "")}


def _voice_env() -> dict:
    env = os.environ.copy()
    for key in ("PYTHONHOME", "PYTHONPATH"):  # the mind's, not the voice venv's
        env.pop(key, None)
    return env


class VoiceControl:
    """The voice service of the checkout at `root`, with its own voice/.venv. The callables are
    the real thing by default; tests pass fakes. `processes` verifies the held pid's identity
    (WindowsProcesses), `kill(pid)` asks it to close (taskkill without /F)."""

    def __init__(self, root, *, processes=None, run=subprocess.run, popen=subprocess.Popen,
                 listening=netstat_listening, kill=None, sleep=time.sleep,
                 monotonic=time.monotonic, start_s=VOICE_START_S, close_s=VOICE_CLOSE_S):
        self.root = Path(root)
        scripts = self.root / "voice" / ".venv" / "Scripts"
        self.python, self.pythonw = scripts / "python.exe", scripts / "pythonw.exe"
        self.processes = processes
        self._run, self._popen, self._listening = run, popen, listening
        self._kill = kill or self._taskkill
        self._sleep, self._monotonic = sleep, monotonic
        self.start_s, self.close_s = start_s, close_s
        # Two probes of voice.lock at once would each see the other's: one at a time
        self._probe = threading.Lock()

    def missing(self) -> str | None:
        """Why this checkout cannot run the voice service; None when it can."""
        if not self.python.is_file() or not self.pythonw.is_file():
            return f"no voice venv at {self.python.parent}"
        return None

    def _voice(self, command: str):
        return self._run([str(self.python), "-m", "leeroy_voice", command], capture_output=True,
                         text=True, timeout=30, env=_voice_env(), cwd=str(self.root / "voice"),
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def where(self) -> VoiceWhere:
        result = self._voice("where")
        if result.returncode != 0:
            raise OwnershipError("cannot read the voice config (leeroy_voice where failed)")
        return parse_where(result.stdout or "")

    def held(self) -> int | None:
        """The pid holding voice.lock; 0 when it is held but voice.pid names nobody; None when
        no service holds it."""
        with self._probe:
            result = self._voice("held")
        if result.returncode != 0:
            return None
        text = (result.stdout or "").strip()
        return int(text) if text.isdigit() else 0

    def _bases(self) -> dict[str, str]:
        """{venv interpreter: the base interpreter its launcher starts}, from the venv's
        pyvenv.cfg `home` (python.exe starts python.exe, pythonw.exe starts pythonw.exe)."""
        try:
            text = (self.python.parent.parent / "pyvenv.cfg").read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            text = ""
        homes = [value.strip() for key, sep, value in (line.partition("=")
                 for line in text.splitlines()) if sep and key.strip().lower() == "home"]
        if len(homes) != 1 or not Path(homes[0]).is_absolute():
            raise OwnershipError("cannot read the voice venv's base interpreter from its "
                                 "pyvenv.cfg; no action taken")
        home = Path(homes[0])
        return {canonical(self.python): canonical(home / "python.exe"),
                canonical(self.pythonw): canonical(home / "pythonw.exe")}

    def _identify(self, pid: int):
        """The process row for `pid`, which must be this checkout's voice service: the base
        interpreter that this checkout's venv launcher started (Windows runs a venv that way),
        whose parent is that launcher running `-m leeroy_voice run` and was created before it;
        or, in a venv without a launcher, the venv's interpreter itself. Every executable path
        must be absolute, and the image (exe) must be the one named, as for the mind."""
        bases = self._bases()

        def runs(row):
            return (Path(row.exe).is_absolute() and len(row.argv) == 4
                    and Path(row.argv[0]).is_absolute()
                    and row.argv[1:] == ("-m", "leeroy_voice", "run"))
        rows = {row.pid: row for row in self.processes.snapshot()}
        row = rows.get(pid)
        if row is not None and runs(row):
            exe, first = canonical(row.exe), canonical(row.argv[0])
            if exe in bases and first == exe:
                return row
            venv = next((v for v, base in bases.items() if base == exe), None)
            parent = rows.get(row.parent)
            if (venv is not None and first in (venv, exe) and parent is not None
                    and runs(parent) and canonical(parent.exe) == venv
                    and canonical(parent.argv[0]) == venv and parent.created < row.created):
                return row
        raise OwnershipError(f"voice.lock is held by pid {pid}, which is not this checkout's "
                             "voice service; no action taken")

    def status(self) -> Outcome:
        pid = self.held()
        if pid is None:
            return Outcome(0, "voice stopped", "stopped")
        if pid == 0:
            return Outcome(1, "voice.lock is held, but voice.pid names nobody", "running")
        return Outcome(0, f"voice pid {pid} (running)", "running", pid)

    def start(self) -> Outcome:
        if (problem := self.missing()) is not None:
            raise OwnershipError(problem)
        pid = self.held()
        if pid is not None:
            return Outcome(0, f"voice already running, pid {pid}", "running", pid or None)
        where = self.where()
        logs = where.data_dir / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        with open(logs / "voice-stdout.log", "wb") as out:
            launch = self._popen(
                [str(self.pythonw), "-m", "leeroy_voice", "run"], cwd=str(self.root / "voice"),
                env=_voice_env(), stdin=subprocess.DEVNULL, stdout=out,
                stderr=subprocess.STDOUT, close_fds=True,
                creationflags=(getattr(subprocess, "DETACHED_PROCESS", 0)
                               | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)))
        deadline = self._monotonic() + self.start_s
        while self._monotonic() < deadline:
            if {where.mind_port, where.ui_port} <= self._listening():
                return Outcome(0, f"voice listening on {where.mind_port} and {where.ui_port}",
                               "running", getattr(launch, "pid", None))
            code = launch.poll()
            if code is not None:
                raise OwnershipError(f"voice exited with code {code} before listening; see "
                                     f"{logs / 'voice-stdout.log'} and voice.log")
            self._sleep(1)
        raise OwnershipError(f"voice not listening after {self.start_s} s: see "
                             f"{logs / 'voice-stdout.log'} and voice.log")

    def stop(self) -> Outcome:
        pid = self.held()
        if pid is None:
            return Outcome(0, "voice already stopped", "stopped")
        if pid == 0:
            raise OwnershipError("voice.lock is held, but voice.pid names nobody; no action taken")
        if self.processes is None:
            raise OwnershipError("voice process identity cannot be verified here")
        row = self._identify(pid)
        handle = self.processes.open(row)
        if handle is None:  # it exited meanwhile
            return self._released("voice stopped")
        forced = False
        try:
            self._kill(pid)  # WM_CLOSE: it closes cleanly and restores WoW's volume
            if not self.processes.exited(handle, self.close_s):
                forced = True
                self.processes.terminate(handle)
        finally:
            self.processes.close(handle)
        return self._released("voice terminated after 5 s" if forced else "voice stopped")

    def _released(self, message: str) -> Outcome:
        deadline = self._monotonic() + 5
        while self._monotonic() < deadline:
            if self.held() is None:
                return Outcome(0, message, "stopped")
            self._sleep(.25)
        raise OwnershipError("voice process exited but voice.lock remains held")

    @staticmethod
    def _taskkill(pid: int) -> None:
        subprocess.run(["taskkill", "/PID", str(pid)], capture_output=True, timeout=15,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
