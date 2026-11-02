"""The voice suite's guard, armed for every test (the mind's tests/live_guard.py, smaller), and
the helpers and fixtures the tests share. Later tasks add their shared fixtures here (M2).

A test that reaches a live service, a real device or the owner's data gets GuardTripped before
the connect, the import, the launch or the write happens. Each hit is also recorded and fails
the test at teardown, so code that swallows the exception cannot hide it.

- Connecting to a live port: leeroy-voice's two (the mind's 7879 and the UI's 7880, from
  config.DEFAULT_TOML) unless the test is marked `live`, Ollama's 11434 unless it is marked
  `ollama`, and the game bridge's 7878 never, whatever the markers: leeroy-voice never talks to
  it, and it serves one mind. Routes: socket.create_connection (wrapped, so it refuses before
  a socket exists), the "socket.connect" audit event (connect, connect_ex) and asyncio's
  create_connection and sock_connect (the Windows proactor connects without that event).
  Binding one of those ports is refused too (the "socket.bind" audit event, and
  socket.create_server wrapped like create_connection): tests bind their own servers on port 0.
- Launching nvidia-smi unless the test is marked `gpu`.
- Importing sounddevice unless the test is marked `device`, and pycaw, comtypes or
  leeroy_voice.winapi (Raw Input, the key state, SendInput: Task 8) unless it is marked
  `windows`: a meta-path finder ahead of every other refuses before the module is looked up,
  so the import trips whether the package is installed or not (M14), at collection too. A
  module an earlier, marked test imported is hidden from sys.modules for the test.
- Opening for write, creating, renaming or removing anything under the real mind-data or
  voice-data (M17): the package-relative ones, the main checkout's (a run from a worktree
  guards both) and the ones LEEROY_MIND_DATA and LEEROY_VOICE_DATA named when the run began.
  Reads are not refused. Paths are compared after abspath and normcase.

Every test runs with LEEROY_VOICE_DATA and LEEROY_MIND_DATA at tmp dirs of its own.

The shared fixtures (M2): `fake_bank`, a StyleBank on a small voices file of seven packs
(PACK, its hash BANK_SHA); `fake_engine`, a FakeEngine; `kokoro`, a KokoroEngine on the
fetched model files (the session's LEEROY_VOICE_DATA, else voice-data beside the repo), and
`parakeet`, a ParakeetRecognizer behind its SpeechGate on the same, each skipped when its files
are absent."""
import asyncio
import asyncio.base_events
import asyncio.proactor_events
import asyncio.selector_events
import hashlib
import importlib.abc
import io
import os
import re
import socket
import subprocess
import sys
import time
import tomllib
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

import numpy as np
import pytest

from leeroy_voice import config

REPO = Path(config.__file__).resolve().parents[2]
REAL_VOICE_DATA = REPO / "voice-data"  # default_data_dir() without LEEROY_VOICE_DATA
REAL_MIND_DATA = REPO / "mind-data"  # the mind's default data dir
SESSION_VOICE_DATA = os.environ.get("LEEROY_VOICE_DATA")  # the run's own, before any test's
BRIDGE_PORT = 7878  # mod-leeroy's, one mind at a time: leeroy-voice never connects to it
OLLAMA_PORT = 11434
GUARDED_IMPORTS = {"sounddevice": "device", "pycaw": "windows", "comtypes": "windows",
                   "leeroy_voice.winapi": "windows"}
_NVIDIA_SMI = re.compile(r"""(?:^|[\\/\s'"\[,(])nvidia-smi(?:\.exe)?(?=['"\s,\])]|$)""",
                         re.IGNORECASE)
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC
# the audit events that write, create or remove a path, with the positions of those paths
_PATH_EVENTS = {
    "os.mkdir": (0,), "os.rename": (0, 1), "os.remove": (0,), "os.rmdir": (0,),
    "os.truncate": (0,), "os.link": (1,), "os.symlink": (1,), "os.utime": (0,),
    "os.chmod": (0,), "shutil.copyfile": (1,), "shutil.copytree": (1,), "shutil.rmtree": (0,),
    "shutil.move": (0, 1), "sqlite3.connect": (0,)}
# where each launch event keeps the program and its arguments
_LAUNCH_EVENTS = {"subprocess.Popen": (0, 1), "os.system": (0,), "os.posix_spawn": (0, 1),
                  "os.spawn": (1, 2), "os.exec": (0, 1), "os.startfile": (0,)}


class GuardTripped(BaseException):
    """A test reached something live. A BaseException, so an `except Exception` in the code
    under test cannot swallow it on the way out."""


def needs(name: str) -> str | None:
    """The marker an import of `name` needs: its own entry, else its top-level package's."""
    return GUARDED_IMPORTS.get(name) or GUARDED_IMPORTS.get(name.partition(".")[0])


def norm(p) -> str:
    s = os.fsdecode(os.fspath(p))
    if s.startswith("\\\\?\\UNC\\"):  # \\?\UNC\host\share: \\host\share
        s = "\\\\" + s[8:]
    elif s.startswith("\\\\?\\"):  # \\?\F:\x: F:\x
        s = s[4:]
    return os.path.normcase(os.path.abspath(s))


def live_ports() -> dict[int, str | None]:
    """The ports a test may not reach, with the marker that allows it (None: no marker does)."""
    defaults = tomllib.loads(config.DEFAULT_TOML)
    return {BRIDGE_PORT: None, defaults["mind"]["port"]: "live",
            defaults["ui"]["port"]: "live", OLLAMA_PORT: "ollama"}


def _main_checkout() -> Path | None:
    """The repo's main checkout, from git's common dir: a worktree's own data dirs are not the
    owner's."""
    try:
        out = subprocess.run(["git", "-C", str(REPO), "rev-parse", "--path-format=absolute",
                              "--git-common-dir"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    common = out.stdout.strip()
    return Path(common).parent if out.returncode == 0 and common else None


def guarded_dirs(environ) -> list[str]:
    dirs = [REAL_VOICE_DATA, REAL_MIND_DATA]
    if (main := _main_checkout()) is not None:
        dirs += [main / "voice-data", main / "mind-data"]
    for var in ("LEEROY_VOICE_DATA", "LEEROY_MIND_DATA"):
        if environ.get(var):
            dirs.append(Path(environ[var]))
    return list(dict.fromkeys(norm(d) for d in dirs))


def _as_path(raw) -> str | None:
    if isinstance(raw, int):
        return None  # a file descriptor
    s = os.fsdecode(os.fspath(raw))
    if not s or s == ":memory:":
        return None
    if s.startswith("file:"):  # a sqlite URI
        s = urllib.request.url2pathname(urllib.parse.urlsplit(s).path)
    return s


def _writes(mode, flags) -> bool:
    if isinstance(mode, str) and any(c in mode for c in "wax+"):
        return True
    return isinstance(flags, int) and bool(flags & _WRITE_FLAGS)


class Guard:
    def __init__(self):
        self.active = False
        self.allowed: frozenset[str] = frozenset()  # the running test's markers
        self.ports = live_ports()
        self.dirs = guarded_dirs(os.environ)  # at import: the session's own environment
        self.hits: list[str] = []
        self.data_dir: Path | None = None  # LEEROY_VOICE_DATA for the running test
        self.originals: dict[str, object] = {}

    def hit(self, what: str) -> None:
        self.hits.append(what)
        raise GuardTripped(what)

    def port(self, port, how: str) -> None:
        if not self.active or not isinstance(port, int) or port not in self.ports:
            return
        need = self.ports[port]
        if need is None:
            self.hit(f"{how} to port {port} (no test may)")
        elif need not in self.allowed:
            self.hit(f"{how} to port {port} (only tests marked {need!r} may)")

    def path(self, raw, how: str) -> None:
        if not self.active:
            return
        p = _as_path(raw)
        if p is None:
            return
        n = norm(p)
        if any(n == d or n.startswith(d + os.sep) for d in self.dirs):
            self.hit(f"{how} {p} (a real data dir)")

    def launch(self, what, how: str) -> None:
        if self.active and "gpu" not in self.allowed and _NVIDIA_SMI.search(repr(what)):
            self.hit(f"{how} of nvidia-smi: {what!r} (only tests marked 'gpu' may)")

    def module(self, name: str) -> None:
        """Armed outside tests too: a test file that imports a device library at module level
        fails at collection."""
        need = needs(name)
        if need is not None and need not in self.allowed:
            self.hit(f"import {name} (only tests marked {need!r} may)")

    def verify(self) -> None:
        """Fail the test if anything reached something live, even if the exception was eaten."""
        hits, self.hits = self.hits, []
        if hits:
            pytest.fail("a test reached something live:\n  " + "\n  ".join(hits), pytrace=False)


GUARD = Guard()


def _audit(event: str, args: tuple) -> None:
    if not GUARD.active:
        return
    if event in ("socket.connect", "socket.bind"):
        address = args[1] if len(args) > 1 else None
        if isinstance(address, tuple) and len(address) >= 2:
            GUARD.port(address[1], event)
    elif event == "open":
        if len(args) >= 3 and _writes(args[1], args[2]) and isinstance(
                args[0], (str, bytes, os.PathLike)):
            GUARD.path(args[0], "open for write")
    elif event in _PATH_EVENTS:
        for i in _PATH_EVENTS[event]:
            if i < len(args) and isinstance(args[i], (str, bytes, os.PathLike)):
                GUARD.path(args[i], event)
    elif event in _LAUNCH_EVENTS:
        GUARD.launch([args[i] for i in _LAUNCH_EVENTS[event] if i < len(args)], event)


class ImportGuard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        GUARD.module(fullname)
        return None  # not ours: the other finders go on as usual


def hide_modules(allowed: frozenset[str]) -> dict:
    """Take the guarded modules this test may not import out of sys.modules (the fixture puts
    them back), so an import goes to the finder instead of the cache."""
    hidden = {}
    for name in list(sys.modules):
        need = needs(name)
        if need is not None and need not in allowed:
            hidden[name] = sys.modules.pop(name)
    return hidden


def _patch_asyncio() -> None:
    base = asyncio.base_events.BaseEventLoop
    real_create = base.create_connection

    async def create_connection(self, protocol_factory, host=None, port=None, *args, **kw):
        GUARD.port(port, "loop.create_connection")
        return await real_create(self, protocol_factory, host, port, *args, **kw)
    GUARD.originals["create_connection"] = real_create
    base.create_connection = create_connection
    for cls in (asyncio.proactor_events.BaseProactorEventLoop,
                asyncio.selector_events.BaseSelectorEventLoop):
        real = cls.sock_connect

        async def sock_connect(self, sock, address, _real=real):
            if isinstance(address, tuple) and len(address) >= 2:
                GUARD.port(address[1], "loop.sock_connect")
            return await _real(self, sock, address)
        GUARD.originals[f"{cls.__name__}.sock_connect"] = real
        cls.sock_connect = sock_connect


def _patch_socket_helpers() -> None:
    """socket.create_connection and create_server only close their half-made socket on
    OSError, so they are refused before any socket exists."""
    for helper in ("create_connection", "create_server"):
        real = getattr(socket, helper)

        def guarded(address, *args, _real=real, _how=f"socket.{helper}", **kw):
            if isinstance(address, tuple) and len(address) >= 2:
                GUARD.port(address[1], _how)
            return _real(address, *args, **kw)
        GUARD.originals[f"socket.{helper}"] = real
        setattr(socket, helper, guarded)


def install() -> None:
    """Once per test session: an audit hook cannot be removed, and it does nothing while
    GUARD.active is off."""
    if "audit" in GUARD.originals:
        return
    sys.addaudithook(_audit)
    GUARD.originals["audit"] = _audit
    finder = ImportGuard()
    sys.meta_path.insert(0, finder)
    GUARD.originals["finder"] = finder
    _patch_socket_helpers()
    _patch_asyncio()


install()


@pytest.fixture(autouse=True)
def live_guard(request, tmp_path, monkeypatch):
    """Every test runs with the guard armed and with LEEROY_VOICE_DATA and LEEROY_MIND_DATA at
    tmp dirs of its own (not created), so a default data dir is never the owner's. A test can
    take this fixture to read the guard."""
    guard = GUARD
    guard.data_dir = tmp_path / "voice-data"
    monkeypatch.setenv("LEEROY_VOICE_DATA", str(guard.data_dir))
    monkeypatch.setenv("LEEROY_MIND_DATA", str(tmp_path / "mind-data"))
    guard.allowed = frozenset(m.name for m in request.node.iter_markers())
    hidden = hide_modules(guard.allowed)
    guard.hits.clear()
    guard.active = True
    try:
        yield guard
    finally:
        guard.active = False
        guard.allowed = frozenset()
        sys.modules.update(hidden)
    guard.verify()


def run(coro, timeout: float = 10.0):
    """asyncio.run with a bound, so a hung socket fails the test instead of the suite."""
    async def bounded():
        return await asyncio.wait_for(coro, timeout)
    return asyncio.run(bounded())


async def eventually(predicate, timeout: float = 2.0) -> None:
    end = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > end:
            raise AssertionError(f"not true within {timeout} s")
        await asyncio.sleep(0.01)


# -- voices and engines (Task 4) ----------------------------------------------------------------

FAKE_VOICES = ("am_michael", "am_fenrir", "bf_emma", "af_kore", "af_sarah", "af_nicole", "bf_lily")
PACK = {name: np.random.default_rng(4000 + i).standard_normal((510, 1, 256)).astype(np.float32)
        for i, name in enumerate(FAKE_VOICES)}


def voices_bin(packs: dict[str, np.ndarray]) -> bytes:
    """Packs laid out as voices-v1.0.bin is: an uncompressed zip of <name>.npy (NPY 1.0), every
    member dated 1980-01-01 as there, so the bytes and their hash are the same on every run."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as z:
        for name, pack in packs.items():
            npy = io.BytesIO()
            np.lib.format.write_array(npy, pack, version=(1, 0))
            z.writestr(zipfile.ZipInfo(f"{name}.npy", date_time=(1980, 1, 1, 0, 0, 0)),
                       npy.getvalue())
    return out.getvalue()


BANK_BYTES = voices_bin(PACK)
BANK_SHA = hashlib.sha256(BANK_BYTES).hexdigest()


@pytest.fixture(scope="session")
def fake_voices_bin(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("fake-voices") / "voices-v1.0.bin"
    path.write_bytes(BANK_BYTES)
    return path


@pytest.fixture
def fake_bank(fake_voices_bin):
    """A StyleBank on the seven packs, its cache empty."""
    from leeroy_voice.voices import StyleBank
    return StyleBank(fake_voices_bin)


@pytest.fixture
def fake_engine():
    from leeroy_voice.engines.fake import FakeEngine
    return FakeEngine()


@pytest.fixture(scope="session")
def kokoro():
    """A KokoroEngine on 6 CPU threads over the fetched Kokoro files; skipped when they are not
    fetched, an error when they are not the pinned ones."""
    pytest.importorskip("kokoro_onnx")
    pytest.importorskip("misaki")
    from leeroy_voice import models
    from leeroy_voice.engines.kokoro import KokoroEngine
    root = Path(SESSION_VOICE_DATA) if SESSION_VOICE_DATA else REAL_VOICE_DATA
    lines = models.check(root, "kokoro")
    if any(ok is None for ok, _ in lines):
        pytest.skip(f"Kokoro is not fetched under {root} (fetch-models --only kokoro)")
    problems = [text for ok, text in lines if ok is False]
    if problems:
        pytest.fail("; ".join(problems), pytrace=False)
    d = models.model_dir(root, "kokoro")
    return KokoroEngine(d / "kokoro-v1.0.onnx", d / "voices-v1.0.bin", threads=6)


# -- recognition (Task 9) -----------------------------------------------------------------------

@pytest.fixture(scope="session")
def parakeet():
    """A ParakeetRecognizer on 4 CPU threads (the [stt] default) behind a SpeechGate, over the
    fetched Parakeet and Silero files; skipped when they are not fetched, an error when they
    are not the pinned ones."""
    pytest.importorskip("onnx_asr")
    from leeroy_voice import models
    from leeroy_voice.stt.parakeet import ParakeetRecognizer
    from leeroy_voice.stt.vad import SpeechGate
    root = Path(SESSION_VOICE_DATA) if SESSION_VOICE_DATA else REAL_VOICE_DATA
    for name in ("parakeet", "silero"):
        lines = models.check(root, name)
        if any(ok is None for ok, _ in lines):
            pytest.skip(f"{name} is not fetched under {root} (fetch-models --only {name})")
        problems = [text for ok, text in lines if ok is False]
        if problems:
            pytest.fail("; ".join(problems), pytrace=False)
    gate = SpeechGate(models.model_dir(root, "silero"))
    return ParakeetRecognizer(models.model_dir(root, "parakeet"), gate, threads=4)
