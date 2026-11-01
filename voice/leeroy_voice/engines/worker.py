"""The worker protocol (Task 22): an expressive engine, or a design engine, runs in a process of
its own under its own venv, and talks to the service over its stdin and stdout.

A request is one JSON line, {"id", "text", "ref", "params"}. An answer is one JSON line,
{"id", "ok", "sr", "frames", "error"}, followed by `frames` float32 samples, little-endian. The
first answer of a worker's life has the id READY and no samples: its model has loaded, or with
ok false it could not. `error` is the failure's type alone, since an exception's message may
quote the line, and the service's log never holds one. A failed line leaves the worker running.

A library that prints would corrupt the pipe, so serve() keeps stdout for itself and points
everything else at stderr, which the service throws away. The worker's loop ends at EOF, so a
service that dies leaves nothing on the card.

Both sides import this file, and a worker's venv does not have leeroy_voice: it needs the
standard library and numpy, and nothing else of this package."""
import json
import os
import sys
import threading
import time
from dataclasses import dataclass
from typing import BinaryIO, Callable

import numpy as np

READY = "ready"
MAX_HEAD = 1 << 16  # an answer's JSON line
SAMPLE = "<f4"
SAMPLE_BYTES = 4


class WorkerTree:
    """Service-side ownership of a worker, including Windows venv redirector children.

    Import psutil only when the service creates this tracker. Worker venvs importing the
    wire protocol still need only numpy and the standard library. Never find processes by
    executable name: ownership starts with Popen and follows verified parent identities.
    """

    def __init__(self):
        import psutil
        self.ps = psutil
        self.owned: dict[int, float] = {}
        self._lock = threading.RLock()
        self._done = threading.Event()
        self._thread: threading.Thread | None = None

    def attach(self, process) -> None:
        root = self.ps.Process(process.pid)
        self.owned[root.pid] = root.create_time()
        self.capture()
        self._thread = threading.Thread(target=self._watch, name="leeroy-worker-tree", daemon=True)
        self._thread.start()

    def _matching(self, pid):
        try:
            process = self.ps.Process(pid)
            if process.create_time() != self.owned[pid] or not process.is_running():
                return None
            if process.status() == self.ps.STATUS_ZOMBIE:
                return None
            return process
        except self.ps.NoSuchProcess:
            return None

    def capture(self) -> bool:
        """Retain descendants even after their launcher exits; never follow a reused PID."""
        complete = True
        with self._lock:
            pending = list(self.owned)
            seen = set()
            while pending:
                pid = pending.pop()
                if pid in seen:
                    continue
                seen.add(pid)
                try:
                    parent = self._matching(pid)
                    if parent is None:
                        continue
                    children = parent.children()
                    if self._matching(pid) is None:
                        continue  # the parent exited/reused during process enumeration
                    for child in children:
                        created = child.create_time()
                        if created < self.owned[pid] or child.ppid() != pid:
                            continue
                        if self._matching(pid) is None:
                            break
                        if child.pid not in self.owned:
                            self.owned[child.pid] = created
                        if self.owned[child.pid] == created:
                            pending.append(child.pid)
                except self.ps.NoSuchProcess:
                    # Disappearance is different from an unreadable live ancestry.
                    continue
                except self.ps.Error:
                    complete = False
        return complete

    def _watch(self) -> None:
        while not self._done.wait(0.05):
            self.capture()

    def _alive(self):
        living = []
        with self._lock:
            for pid in self.owned:
                try:
                    process = self._matching(pid)
                    if process is not None:
                        living.append(process)
                except self.ps.Error:
                    # Unreadable is not evidence of exit. Cleanup will report failure.
                    living.append(None)
        return living

    def stop(self, *, timeout: float, grace: float = 0.5) -> bool:
        """After stdin EOF, wait briefly, then kill only captured process identities.

        Freeze parents before the final descendant scan, preventing a launcher from
        spawning an unobserved child while it is terminated. Resume any survivors if an
        OS operation fails. The return value reports whether all owned processes exited.
        """
        deadline = time.monotonic() + timeout
        grace_end = min(deadline, time.monotonic() + (0 if self._done.is_set() else grace))
        while time.monotonic() < grace_end:
            self.capture()
            if not self._alive():
                break
            self._done.wait(min(0.025, max(0.0, grace_end - time.monotonic())))
        self._done.set()
        if self._thread is not None:
            self._thread.join(max(0.0, min(0.5, deadline - time.monotonic())))
        frozen = set()
        attempted = set()
        try:
            with self._lock:
                self.capture()
                while time.monotonic() < deadline:
                    pending = [pid for pid in self.owned if pid not in attempted]
                    if not pending:
                        break
                    for pid in pending:  # insertion order: parent before child
                        attempted.add(pid)
                        try:
                            process = self._matching(pid)
                            if process is not None:
                                process.suspend()
                                frozen.add(pid)
                        except self.ps.Error:
                            continue
                    self.capture()  # includes children born just before each suspension
                if (not self.capture()
                        or any(process is None or process.pid not in frozen for process in self._alive())):
                    return False  # unreadable ancestry or an unfrozen parent is not a complete tree
                for pid in reversed(tuple(self.owned)):
                    try:
                        process = self._matching(pid)
                        if process is not None:
                            process.kill()
                    except self.ps.Error:
                        continue
        finally:
            for pid in frozen:
                try:
                    process = self._matching(pid)
                    if process is not None:
                        process.resume()
                except self.ps.Error:
                    pass
        while self._alive() and time.monotonic() < deadline:
            time.sleep(min(0.025, max(0.0, deadline - time.monotonic())))
        return not self._alive()


@dataclass(frozen=True, slots=True)
class Answer:
    id: str
    ok: bool
    sr: int
    audio: np.ndarray
    error: str


def request_line(id_: str, text: str, ref: str, params: dict) -> bytes:
    return json.dumps({"id": id_, "text": text, "ref": ref, "params": params}).encode() + b"\n"


def read_answer(stream: BinaryIO) -> Answer | None:
    """The next answer, or None at the end of the stream or on anything that is not one."""
    line = stream.readline(MAX_HEAD)
    try:
        head = json.loads(line)
        frames = int(head["frames"])
        size = frames * SAMPLE_BYTES
        body = stream.read(size) if size > 0 else b""
        if len(body) != size:
            return None
        audio = np.frombuffer(body, SAMPLE).astype(np.float32)
        return Answer(str(head["id"]), bool(head["ok"]), int(head["sr"]), audio,
                      str(head["error"]))
    except (ValueError, KeyError, TypeError):
        return None


def _send(out: BinaryIO, id_: str, ok: bool, sr: int, audio, error: str) -> None:
    body = b"" if audio is None else np.asarray(audio, dtype=SAMPLE).reshape(-1).tobytes()
    head = {"id": id_, "ok": ok, "sr": sr, "frames": len(body) // SAMPLE_BYTES, "error": error}
    out.write(json.dumps(head).encode() + b"\n" + body)
    out.flush()


def _claim_stdout() -> BinaryIO:
    """The pipe for the protocol alone: fd 1, and sys.stdout, now lead to stderr."""
    out = os.fdopen(os.dup(1), "wb")
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    return out


def serve(load: Callable[[], tuple[Callable, int]]) -> int:
    """A worker's whole life. `load()` loads the model and returns (speak, rate), where
    speak(text, ref, params) -> mono audio at `rate`."""
    out = _claim_stdout()
    try:
        speak, rate = load()
    except Exception as e:
        _send(out, READY, False, 0, None, type(e).__name__)
        return 1
    try:
        _send(out, READY, True, rate, None, "")
        for line in sys.stdin.buffer:
            id_ = ""
            try:
                request = json.loads(line)
                id_ = str(request["id"])
                audio = speak(request["text"], request["ref"], request["params"])
            except Exception as e:
                _send(out, id_, False, 0, None, type(e).__name__)
            else:
                _send(out, id_, True, rate, audio, "")
    except OSError:  # the service has gone
        return 0
    return 0
