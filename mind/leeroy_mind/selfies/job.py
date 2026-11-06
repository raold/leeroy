"""The selfie worker's process tree, killed as one.

On Windows a venv's python.exe is a launcher that starts the base interpreter as its child,
and that child is the process holding the GPU. Killing the launcher alone can leave it (or
whatever it started) running with 10 GB of VRAM. So the worker starts suspended, joins a job
object with kill-on-close before its first instruction runs, and only then resumes: every
process it ever starts is born inside the job, and terminating the job ends them all. The mind
holds the job's only handle, so a mind that dies takes the worker with it. `left` counts the
processes that have not exited, from handles to each: the job's own count (ActiveProcesses)
reads 0 as soon as termination starts, while they still run and hold their VRAM.

Elsewhere the worker leads a session and process group of its own, which a SIGKILL to the
group ends; `left` can only say whether any member remains (1) or none (0)."""
import asyncio
import ctypes
import os
import signal
import subprocess
from contextlib import suppress

CREATE_SUSPENDED = 0x00000004
PROCESS_TERMINATE = 0x0001
PROCESS_SET_QUOTA = 0x0100
PROCESS_SUSPEND_RESUME = 0x0800
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_PROCESS_ID_LIST = 3  # JobObjectBasicProcessIdList
JOB_EXTENDED_LIMITS = 9  # JobObjectExtendedLimitInformation
ERROR_ACCESS_DENIED = 5
ERROR_MORE_DATA = 234
WAIT_OBJECT_0 = 0
DISCARD_S = 3.0  # waiting for a process that could not be contained to end


class WorkerError(Exception):
    """The worker failed. Its text is the reason only (spawn, timeout, gone, or the worker's
    own error name such as vram or oom), never a prompt, so it can be logged."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class _Win:
    """kernel32 and ntdll with the signatures used here, on instances of our own so nobody
    else's argtypes change."""

    def __init__(self):
        from ctypes import wintypes as w
        k = self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        n = self.ntdll = ctypes.WinDLL("ntdll")
        k.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        k.OpenProcess.restype = w.HANDLE
        k.CloseHandle.argtypes = [w.HANDLE]
        k.CloseHandle.restype = w.BOOL
        k.TerminateProcess.argtypes = [w.HANDLE, w.UINT]
        k.TerminateProcess.restype = w.BOOL
        k.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
        k.WaitForSingleObject.restype = w.DWORD
        k.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]
        k.CreateJobObjectW.restype = w.HANDLE
        k.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
        k.SetInformationJobObject.restype = w.BOOL
        k.QueryInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                                w.DWORD, ctypes.POINTER(w.DWORD)]
        k.QueryInformationJobObject.restype = w.BOOL
        k.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        k.AssignProcessToJobObject.restype = w.BOOL
        k.TerminateJobObject.argtypes = [w.HANDLE, w.UINT]
        k.TerminateJobObject.restype = w.BOOL
        n.NtResumeProcess.argtypes = [w.HANDLE]
        n.NtResumeProcess.restype = ctypes.c_long

        class BasicLimits(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                        ("PerJobUserTimeLimit", ctypes.c_int64), ("LimitFlags", w.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", w.DWORD), ("Affinity", ctypes.c_size_t),
                        ("PriorityClass", w.DWORD), ("SchedulingClass", w.DWORD)]

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BasicLimits), ("IoInfo", IoCounters),
                        ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        self.ExtendedLimits = ExtendedLimits

    @staticmethod
    def error(what: str) -> OSError:
        code = ctypes.get_last_error()
        return OSError(code, f"{what} failed ({code})")


_WIN: _Win | None = None


def _win() -> _Win:
    global _WIN
    if _WIN is None:
        _WIN = _Win()
    return _WIN


class _Job:
    """The job's ActiveProcesses drops to 0 the moment TerminateJobObject starts terminating
    its processes, before they have exited (and released their VRAM). So every process listed
    in the job is held by a handle of ours, taken while it was listed (no pid reuse can
    mislead it), and `left` counts those that have not exited."""

    def __init__(self, handle):
        self.handle = handle
        self.members: dict[int, int] = {}  # pid -> a SYNCHRONIZE handle to that process
        self.unopened: set[int] = set()  # listed, but we may not open it

    @classmethod
    def around(cls, pid: int) -> "_Job":
        """The suspended process `pid` in a new kill-on-close job, then resumed. Any failure
        terminates the process (closing the job does too, once it has joined)."""
        api = _win()
        k = api.kernel
        process = k.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE | PROCESS_SUSPEND_RESUME
                                | PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not process:
            raise api.error("OpenProcess")
        job = None
        try:
            job = k.CreateJobObjectW(None, None)
            if not job:
                raise api.error("CreateJobObjectW")
            limits = api.ExtendedLimits()
            limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not k.SetInformationJobObject(job, JOB_EXTENDED_LIMITS, ctypes.byref(limits),
                                             ctypes.sizeof(limits)):
                raise api.error("SetInformationJobObject")
            if not k.AssignProcessToJobObject(job, process):
                raise api.error("AssignProcessToJobObject")
            status = api.ntdll.NtResumeProcess(process)
            if status != 0:
                raise OSError(f"NtResumeProcess failed (0x{status & 0xFFFFFFFF:08x})")
        except BaseException:
            k.TerminateProcess(process, 1)
            if job:
                k.CloseHandle(job)
            raise
        finally:
            k.CloseHandle(process)
        return cls(job)

    def _listed(self) -> list[int]:
        api = _win()
        size = 64
        while True:
            class PidList(ctypes.Structure):
                _fields_ = [("NumberOfAssignedProcesses", ctypes.c_uint32),
                            ("NumberOfProcessIdsInList", ctypes.c_uint32),
                            ("ProcessIdList", ctypes.c_size_t * size)]
            info = PidList()
            ok = api.kernel.QueryInformationJobObject(self.handle, JOB_PROCESS_ID_LIST,
                                                      ctypes.byref(info), ctypes.sizeof(info),
                                                      None)
            if ok and info.NumberOfProcessIdsInList >= info.NumberOfAssignedProcesses:
                return list(info.ProcessIdList[:info.NumberOfProcessIdsInList])
            if (not ok and ctypes.get_last_error() != ERROR_MORE_DATA) or size >= 65536:
                raise api.error("QueryInformationJobObject")
            size *= 4

    def _capture(self) -> int:
        """Holds every process the job lists; how many of them are listed now."""
        k = _win().kernel
        listed = self._listed()
        for pid in listed:
            if pid in self.members:
                continue
            h = k.OpenProcess(SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if h:
                self.members[pid] = h
                self.unopened.discard(pid)
            else:
                self.unopened.add(pid)
        self.unopened &= set(listed)
        return len(listed)

    def kill(self) -> None:
        api = _win()
        try:
            self._capture()  # before the list empties
        finally:  # a capture that failed must not spare the tree
            if not api.kernel.TerminateJobObject(self.handle, 1):
                raise api.error("TerminateJobObject")

    def left(self) -> int:
        """Exited means signalled (WAIT_OBJECT_0); a wait that failed proves nothing and
        counts as still running."""
        k = _win().kernel
        self._capture()
        running = sum(1 for h in self.members.values()
                      if k.WaitForSingleObject(h, 0) != WAIT_OBJECT_0)
        return running + len(self.unopened)

    def close(self) -> None:
        k = _win().kernel
        k.CloseHandle(self.handle)  # kill-on-close: whatever is left ends here
        for h in self.members.values():
            k.CloseHandle(h)
        self.members.clear()


class _Group:
    def __init__(self, pgid: int):
        self.pgid = pgid

    def kill(self) -> None:
        with suppress(ProcessLookupError):
            os.killpg(self.pgid, signal.SIGKILL)

    def left(self) -> int:
        try:
            os.killpg(self.pgid, 0)
        except ProcessLookupError:
            return 0
        except PermissionError:
            return 1
        return 1

    def close(self) -> None:
        self.kill()  # as a job's close would


def _contain(pid: int):
    return _Job.around(pid) if os.name == "nt" else _Group(pid)


async def _discard(process: asyncio.subprocess.Process) -> None:
    with suppress(ProcessLookupError, OSError):
        process.kill()
    with suppress(TimeoutError):
        await asyncio.wait_for(process.wait(), DISCARD_S)


class ProcessTree:
    """A process and everything it starts. `process` has stdin and stdout pipes; stderr is
    discarded. kill() ends the whole tree, left() counts what of it is still alive, close()
    releases it (whatever is left is killed). After close() nothing is left to count: 0."""

    def __init__(self, process: asyncio.subprocess.Process, contained):
        self.process = process
        self._contained = contained
        self._closed = False

    @classmethod
    async def spawn(cls, command, *, env, limit: int = 2 ** 16) -> "ProcessTree":
        """`limit`: the longest stdout line readline() accepts. OSError when the program cannot
        be started; WorkerError("spawn"), with the process ended, when it cannot be
        contained."""
        if os.name == "nt":
            extra = {"creationflags": CREATE_SUSPENDED | subprocess.CREATE_NO_WINDOW
                     | subprocess.BELOW_NORMAL_PRIORITY_CLASS}
        else:
            extra = {"start_new_session": True}
        process = await asyncio.create_subprocess_exec(
            *map(str, command), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, env=env, limit=limit, **extra)
        try:
            contained = _contain(process.pid)
        except Exception:
            await _discard(process)
            raise WorkerError("spawn") from None
        return cls(process, contained)

    def kill(self) -> None:
        if not self._closed:
            self._contained.kill()

    def left(self) -> int:
        return 0 if self._closed else self._contained.left()

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._contained.close()


def pid_alive(pid: int) -> bool:
    """Whether a process with this pid exists and has not exited (one we may not open, or
    whose wait fails, counts as alive)."""
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    api = _win()
    handle = api.kernel.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, False, pid)
    if not handle:
        return ctypes.get_last_error() == ERROR_ACCESS_DENIED
    try:
        return api.kernel.WaitForSingleObject(handle, 0) != WAIT_OBJECT_0
    finally:
        api.kernel.CloseHandle(handle)
