"""The expressive engine (Task 22): a cloning engine in its own process (worker.py), speaking each
member from their reference clip.

The engine implements `Engine`, but it may be down at any moment: GpuGuard has refused it, or let
it go, or the worker died. synth() then raises EngineError at once, and never waits for a model:
the service speaks that line with Kokoro, so a line is never lost to it (service.py). Whether it
is up is decided outside it: start() and stop() are called by the service under GpuGuard, and by
the commands that render on the CPU without it.

Levers per attitude (M28): LEVERS holds neutral numbers only, the kit's. Wording never lives in
the code: a local voice-data/attitudes.toml overrides them key by key at start, as
[<engine>.<attitude>] tables. Turbo has neither exaggeration nor cfg, so its attitude rests on the
sounds in its text (textear) and it has no speed either: `speed` is for the engines that have one.

Nothing of a line leaves here but its audio: an EngineError names a type or a count, and the
worker's stderr is thrown away."""
import os
import queue
import subprocess
import threading
import tomllib
from pathlib import Path

import numpy as np

from ..wire import ATTITUDES
from . import EngineError
from .worker import READY, read_answer, request_line

SAMPLE_RATE = 24000  # Chatterbox's, as Kokoro's: the chain takes one rate
START_S = 110.0  # a little under GpuGuard's RESERVE_S, so a start that hangs gives its lease up
LINE_TIMEOUT_S = 20.0  # longer than any line's wait on the card: a stuck worker, not a long line
STOP_S = 5.0
JOIN_S = 2.0
LOCAL_NAME = "attitudes.toml"  # in the data dir
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_BELOW_NORMAL = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)

_TURBO = {"temperature": 0.8, "top_p": 0.95, "top_k": 1000, "repetition_penalty": 1.2}
_INDEX = {"emo_vector": [0, 0, 0, 0, 0, 0, 0, 0.6], "emo_alpha": 0.6}
LEVERS: dict[str, dict[str, dict]] = {
    "chatterbox": {
        "none": {"exaggeration": 0.5, "cfg_weight": 0.5, "temperature": 0.8},
        "fond": {"exaggeration": 0.6, "cfg_weight": 0.45, "temperature": 0.8},
        "drawn": {"exaggeration": 0.75, "cfg_weight": 0.35, "temperature": 0.8},
        "lust": {"exaggeration": 0.9, "cfg_weight": 0.3, "temperature": 0.85}},
    "chatterbox-turbo": dict.fromkeys(ATTITUDES, _TURBO),
    "indextts2": dict.fromkeys(ATTITUDES, _INDEX),
}


class LeverError(ValueError):
    pass


def _read_overlay(path: Path) -> dict:
    try:
        return tomllib.loads(path.read_bytes().decode("utf-8-sig"))
    except OSError as e:
        raise LeverError(f"{path}: cannot read it: {e.strerror or e}") from e
    except UnicodeDecodeError as e:
        raise LeverError(f"{path}: not UTF-8 (byte {e.start}); save it as UTF-8") from e
    except tomllib.TOMLDecodeError as e:
        raise LeverError(f"{path}: {e}") from e


def load_levers(data_dir: Path, engine: str) -> dict[str, dict]:
    """Each attitude's params for `engine`: LEVERS under <data_dir>/attitudes.toml. A file that
    cannot be read, or an attitude that is not one, raises LeverError naming the key, and never
    a value (the wording is private)."""
    levers = {attitude: dict(params) for attitude, params in LEVERS[engine].items()}
    path = Path(data_dir) / LOCAL_NAME
    if not path.exists():
        return levers
    overlay = _read_overlay(path)
    for name, table in overlay.items():
        for attitude, params in (table.items() if isinstance(table, dict) else ((name, table),)):
            if attitude not in ATTITUDES or not isinstance(params, dict):
                raise LeverError(f"{path}: {name}.{attitude}: expected a table for one of "
                                 f"{', '.join(ATTITUDES)}")
    for attitude, params in overlay.get(engine, {}).items():
        levers[attitude].update(params)
    return levers


class WorkerProcess:
    """One worker script under a venv's interpreter, from start() to stop(); one line at a time.
    Any thread may call stop(), which ends a start() or an ask() under way with an EngineError.
    Used once: a worker that has stopped is replaced, never restarted."""

    def __init__(self, python: Path, script: Path, *, args=(), env: dict | None = None,
                 below_normal: bool = False, start_s: float = START_S):
        self.name = Path(script).stem
        self.process: subprocess.Popen | None = None
        self._command = [str(python), "-I", "-X", "utf8", str(script), *map(str, args)]
        self._env = {**os.environ, **env} if env else None
        self._flags = _NO_WINDOW | (_BELOW_NORMAL if below_normal else 0)
        self._start_s = start_s
        self._answers: queue.SimpleQueue = queue.SimpleQueue()
        self._reader: threading.Thread | None = None
        self._turn = threading.Lock()  # one line at a time
        self._guard = threading.Lock()  # start against stop
        self._ready = self._stopped = False
        self._sequence = 0

    @property
    def up(self) -> bool:
        return self._ready and not self._stopped and self.process.poll() is None

    def start(self) -> None:
        """Until the model has loaded. Raises EngineError when it cannot."""
        with self._guard:
            if self._stopped:
                raise EngineError(f"{self.name}: stopped")
            try:
                self.process = subprocess.Popen(
                    self._command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, env=self._env, creationflags=self._flags)
            except OSError as e:
                self._stopped = True
                raise EngineError(f"{self.name}: cannot start: {type(e).__name__}") from None
            self._reader = threading.Thread(target=self._read, name=f"leeroy-{self.name}",
                                            daemon=True)
            self._reader.start()
        try:
            ready = self._answers.get(timeout=self._start_s)
        except queue.Empty:
            self.stop()
            raise EngineError(f"{self.name}: no model after {self._start_s:g} s") from None
        if ready is None or ready.id != READY or not ready.ok:
            self.stop()
            raise EngineError(f"{self.name}: did not load: "
                              f"{ready.error if ready is not None else 'it exited'}")
        self._ready = True

    def _read(self) -> None:
        try:
            while True:
                answer = read_answer(self.process.stdout)
                self._answers.put(answer)
                if answer is None:
                    return
        except (OSError, ValueError):  # the pipe has closed under it
            self._answers.put(None)

    def ask(self, text: str, ref: str, params: dict, *, timeout: float) -> tuple[np.ndarray, int]:
        """One line: (audio, rate). Raises EngineError, stopping the worker when it did not
        answer; a line it failed on leaves it running."""
        with self._turn:
            if not self.up:
                raise EngineError(f"{self.name}: not running")
            self._sequence += 1
            id_ = str(self._sequence)
            try:
                self.process.stdin.write(request_line(id_, text, ref, params))
                self.process.stdin.flush()
            except (OSError, ValueError):  # it has gone, or stop() closed the pipe
                self.stop()
                raise EngineError(f"{self.name}: gone") from None
            try:
                answer = self._answers.get(timeout=timeout)
            except queue.Empty:
                self.stop()
                raise EngineError(f"{self.name}: no answer in {timeout:g} s") from None
            if answer is None or answer.id != id_:
                self.stop()
                raise EngineError(f"{self.name}: gone mid-line")
            if not answer.ok:
                raise EngineError(f"{self.name}: {answer.error}")
            return answer.audio, answer.sr

    def stop(self) -> None:
        """The process is killed: the card's memory comes back at once. Idempotent."""
        with self._guard:
            if self._stopped:
                return
            self._stopped = True
            process = self.process
        if process is None:
            return
        try:
            process.kill()
            process.wait(STOP_S)
        except (OSError, subprocess.TimeoutExpired):
            pass
        try:
            process.stdin.close()
        except (OSError, ValueError):
            pass
        self._reader.join(JOIN_S)
        if not self._reader.is_alive():
            process.stdout.close()


class ExpressiveEngine:
    """See the module's docstring. `venv_python` is the engine's own venv's interpreter, `refs_dir`
    the folder of the members' reference clips (mind-data/voices), `levers` load_levers()'s, and
    `args` go to the worker script, worker_<engine>.py beside this file unless `script` says."""

    sample_rate = SAMPLE_RATE

    def __init__(self, engine: str, venv_python: Path, *, need_gb: float, refs_dir: Path = Path(),
                 levers: dict[str, dict] | None = None, script: Path | None = None, args=(),
                 env: dict | None = None, below_normal: bool = False,
                 timeout_s: float = LINE_TIMEOUT_S):
        self.name = engine
        self.need_gb = need_gb
        self.worker: WorkerProcess | None = None
        self._python = Path(venv_python)
        self._script = script or Path(__file__).with_name(f"worker_{engine.replace('-', '_')}.py")
        self._args, self._env, self._below_normal = args, env, below_normal
        self._refs = Path(refs_dir)
        self._levers = levers or {a: dict(p) for a, p in LEVERS[engine].items()}
        self._timeout_s = timeout_s
        self._starting = threading.Lock()

    @property
    def up(self) -> bool:
        worker = self.worker
        return worker is not None and worker.up

    def start(self) -> None:
        """Until the model has loaded; blocking, so off the event loop. EngineError otherwise."""
        with self._starting:
            if self.up:
                return
            self.stop()
            self.worker = WorkerProcess(self._python, self._script, args=self._args,
                                        env=self._env, below_normal=self._below_normal)
            self.worker.start()

    def stop(self) -> None:
        worker, self.worker = self.worker, None
        if worker is not None:
            worker.stop()

    def levers(self, attitude: str) -> dict:
        return self._levers[attitude]

    def _reference(self, spec) -> Path:
        path = (self._refs / spec.ref).resolve()
        if path.parent != self._refs.resolve() or not path.is_file():
            raise EngineError(f"{self.name}: no reference clip for {spec.guid}")
        return path

    def synth(self, text: str, spec, style, speed: float) -> np.ndarray:
        """`style` is the attitude's levers; anything else (StockCache hands the bank's style)
        reads as attitude none. EngineError at once when the engine is down."""
        worker = self.worker
        if worker is None or not worker.up:
            raise EngineError(f"{self.name}: not up")
        params = style if isinstance(style, dict) else self._levers["none"]
        audio, rate = worker.ask(text, str(self._reference(spec)), params,
                                 timeout=self._timeout_s)
        if rate != self.sample_rate or not len(audio):
            raise EngineError(f"{self.name}: {len(audio)} samples at {rate} Hz")
        return audio
