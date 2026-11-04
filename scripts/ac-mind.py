"""Windows mind lifecycle helper for ac-server.sh; never selects Python by name alone.

Stopping a console service is forced, as for the server script's existing console
apps. Captured process handles and creation times prevent PID reuse from targeting
another process. An unidentified held lock always requires manual intervention.
"""
import argparse
import ctypes
import json
import os
import subprocess
import sys
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


def canonical(path):
    return os.path.normcase(str(Path(path).resolve()))


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


def run_action(action, root, data, python, base_python, *, processes, locked,
               spawn=spawn_mind, sleep=time.sleep, monotonic=time.monotonic, timeout=30):
    def current():
        return family(processes.snapshot(), python, base_python, data)

    found, held = current(), locked(data)
    if held and not found:
        raise OwnershipError("mind.lock is held but its mind process is unidentified; no action taken")
    if action == "status":
        if found:
            print(f"    mind pid {found[0].pid} ({'running' if held else 'starting/stopping'})")
            return 0 if held else 1
        print("    mind stopped")
        return 0
    if action == "start":
        if held:
            print(f"    mind already running, pid {found[0].pid}")
            return 0
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
                    print(f"    mind started, pid {found[0].pid}; log {data / 'logs' / 'mind-stdout.log'}")
                    return 0
                sleep(.25)
        except BaseException as exc:
            raise OwnershipError(launch_problem(launch, f"verification failed ({type(exc).__name__})")) from None
        raise OwnershipError(launch_problem(launch, "did not acquire its lock"))
    if not found:
        print("    mind already stopped")
        return 0
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
            print("    mind stopped")
            return 0
        sleep(.1)
    raise OwnershipError("mind process or lock remains; servers have not been stopped")


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("start", "stop", "status"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    if os.name != "nt":
        parser.error("mind process control is supported only on Windows")
    root, data = args.root.resolve(), args.data_dir.resolve()
    python = root / "mind" / ".venv" / "Scripts" / "python.exe"
    if canonical(sys.executable) != canonical(python):
        print("mind: helper must use this checkout's mind venv", file=sys.stderr)
        return 1
    sys.path.insert(0, str(root / "mind"))
    try:
        from leeroy_mind.app import instance_lock_held
        return run_action(args.action, root, data, python, sys._base_executable,
                          processes=WindowsProcesses(), locked=instance_lock_held)
    except OwnershipError as exc:
        print(f"mind: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        # CIM failures can include whole command lines. Never print their raw payload.
        print(f"mind: {type(exc).__name__}; no unverified process was stopped", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
