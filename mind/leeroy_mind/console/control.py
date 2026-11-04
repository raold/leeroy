"""Start, restart and stop from the console, through leeroy_mind.lifecycle (the rules
scripts/ac-mind.py uses). One action at a time; each runs in a worker thread, since the rules
wait on processes. With control off (--no-control, or nothing here can control a service)
every action is refused and nothing is launched or stopped."""
import asyncio
import sys
import threading
import time
from pathlib import Path

from .. import lifecycle
from ..lifecycle import OwnershipError, Outcome

SERVICES = ("mind", "voice")
ACTIONS = ("start", "stop", "restart")


class Refused(Exception):
    """An action that was not attempted. status: the HTTP status to answer with."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class MindHandle:
    """This checkout's mind on one data dir. The console's own launch is kept, so an exit it
    did not ask for can be told from a stop."""

    def __init__(self, root: Path, data: Path, *, python: Path, base_python: str,
                 processes, locked, spawn=lifecycle.spawn_mind, sleep=time.sleep,
                 monotonic=time.monotonic, timeout: float = 30):
        self.root, self.data = Path(root), Path(data)
        self.python, self.base_python = Path(python), base_python
        self.processes, self.locked = processes, locked
        self._spawn, self._sleep, self._monotonic = spawn, sleep, monotonic
        self.timeout = timeout
        self.launch = None  # the console's latest launch
        self.stopping = False
        # A lock probe takes the lock for a moment when nobody holds it: two at once would each
        # see the other's, so the console's probes go one at a time
        self._probe = threading.Lock()

    def held(self, data=None) -> bool:
        with self._probe:
            return bool(self.locked(self.data if data is None else data))

    def probe(self):
        """(family or None, lock held, ambiguity or None). None for the family when no process
        list can be taken here."""
        held = self.held()
        if self.processes is None:
            return None, held, None
        try:
            found = lifecycle.family(self.processes.snapshot(), self.python, self.base_python,
                                     self.data)
        except OwnershipError as e:
            return None, held, str(e)
        except Exception as e:  # a process list's failure can carry whole command lines
            return None, held, f"process list unavailable ({type(e).__name__})"
        return found, held, None

    def launch_exit(self) -> int | None:
        launch = self.launch
        if launch is None or self.stopping:
            return None
        try:
            return launch.poll()
        except Exception:
            return None

    def _spawned(self, root, data, python):
        self.launch = self._spawn(root, data, python)
        return self.launch

    def _run(self, action: str) -> Outcome:
        return lifecycle.mind_action(
            action, self.root, self.data, self.python, self.base_python,
            processes=self.processes, locked=self.held, spawn=self._spawned,
            sleep=self._sleep, monotonic=self._monotonic, timeout=self.timeout)

    def act(self, action: str) -> Outcome:
        if action in ("stop", "restart"):
            self.stopping = True
            try:
                stopped = self._run("stop")
            finally:
                self.stopping = False
            self.launch = None
            if action == "stop":
                return stopped
        return self._run("start")


class VoiceHandle:
    def __init__(self, control: lifecycle.VoiceControl):
        self.control = control

    def act(self, action: str) -> Outcome:
        if action == "start":
            return self.control.start()
        stopped = self.control.stop()
        return stopped if action == "stop" else self.control.start()


class Controls:
    """`problems[service]`: why that service cannot be controlled here (None: it can)."""

    def __init__(self, *, enabled: bool, reason: str = "", mind: MindHandle | None = None,
                 voice: VoiceHandle | None = None, problems: dict[str, str | None] | None = None):
        self.enabled = enabled
        self.reason = reason
        self.handles = {"mind": mind, "voice": voice}
        self.problems = {s: (problems or {}).get(s) for s in SERVICES}
        self.busy: tuple[str, str] | None = None
        self._lock = asyncio.Lock()

    def describe(self) -> dict:
        return {"enabled": self.enabled, "reason": self.reason,
                "services": {s: self.problem(s) for s in SERVICES},
                "busy": list(self.busy) if self.busy else None}

    def problem(self, service: str) -> str | None:
        if not self.enabled:
            return self.reason or "process control is off"
        if self.problems.get(service):
            return self.problems[service]
        if self.handles.get(service) is None:
            return f"{service} control is unavailable here"
        return None

    def check(self, service: str, action: str) -> None:
        if service not in SERVICES or action not in ACTIONS:
            raise Refused(400, "unknown service or action")
        if (problem := self.problem(service)) is not None:
            raise Refused(403 if not self.enabled else 409, problem)
        if self.busy is not None or self._lock.locked():
            raise Refused(409, "another start, restart or stop is still running")

    async def act(self, service: str, action: str) -> Outcome:
        self.check(service, action)
        async with self._lock:
            self.busy = (service, action)
            try:
                return await asyncio.to_thread(self.handles[service].act, action)
            except OwnershipError as e:
                raise Refused(409, str(e)) from None
            except Exception as e:  # never a raw payload: CIM errors carry command lines
                raise Refused(500, f"{service} {action} failed ({type(e).__name__}); no "
                                   "unverified process was stopped") from None
            finally:
                self.busy = None


def control_problems(root: Path, *, platform: str = sys.platform,
                     executable: str = sys.executable) -> dict[str, str | None]:
    """Why each service cannot be controlled from this process (None: it can). The mind's
    rule is the script's: only this checkout's own mind venv may launch or stop it."""
    out: dict[str, str | None] = {"mind": None, "voice": None}
    if platform != "win32":
        return {s: "process control is supported only on Windows" for s in SERVICES}
    python = lifecycle.venv_python(root)
    if lifecycle.canonical(executable) != lifecycle.canonical(python):
        out["mind"] = (f"the console is not running in this checkout's mind venv ({python}); "
                       "only that venv may start or stop the mind")
    voice = lifecycle.VoiceControl(root)
    out["voice"] = voice.missing()
    return out
