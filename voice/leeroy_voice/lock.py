"""One service per data dir: an exclusive OS lock on <data dir>/voice.lock, as the mind holds
mind.lock (msvcrt.locking on Windows, fcntl.flock elsewhere). The OS drops it when the process
dies, so a crash never leaves the data dir locked. voice.pid beside it names the process, for
ac-voice.sh. A module of its own, so `held` needs none of the audio libraries."""
import os
from pathlib import Path

LOCK_FILE = "voice.lock"
PID_FILE = "voice.pid"


class ServiceLocked(RuntimeError):
    """Another service holds this data dir's voice.lock."""


if os.name == "nt":
    import msvcrt

    def _lock(fh) -> None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(fh) -> None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _lock(fh) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fh) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


class ServiceLock:
    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / LOCK_FILE
        self._fh = None

    def acquire(self) -> "ServiceLock":
        """Raises ServiceLocked when a service holds it."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+b")
        try:
            _lock(fh)
        except OSError:
            fh.close()
            raise ServiceLocked(f"{self.path} is held by a running service") from None
        self._fh = fh
        return self

    def release(self) -> None:
        fh, self._fh = self._fh, None
        if fh is not None:
            try:
                _unlock(fh)
            except OSError:
                pass
            fh.close()


def lock_held(data_dir: Path) -> bool:
    """Whether a service holds the lock. It writes nothing but the probe's lock itself."""
    if not (Path(data_dir) / LOCK_FILE).exists():
        return False
    try:
        probe = ServiceLock(data_dir).acquire()
    except ServiceLocked:
        return True
    probe.release()
    return False


def read_pid(data_dir: Path) -> int | None:
    """The pid in voice.pid; None when there is none to read."""
    try:
        text = (Path(data_dir) / PID_FILE).read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return int(text) if text.isdecimal() else None
