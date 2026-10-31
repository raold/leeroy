"""The tests' guard against the owner's live services. conftest arms it for every test.

The live bridge takes one mind and hands it every event it buffered; Ollama runs the owner's
models; mind-data holds his guild. A test, or a regression in one, that reaches any of them
gets LiveServiceTouched *before* the connection, the open or the launch happens. The hit is also
recorded and fails the test at teardown, so app code that swallows the exception (a worker's
`except Exception`, the LLM scheduler) cannot hide it.

Routes covered:
- socket.create_connection, wrapped so it refuses before making a socket (http.client, urllib
  and the Ollama client connect through it);
- any other blocking connect, through the "socket.connect" audit event: socket.connect and
  connect_ex;
- asyncio's loop.create_connection (so asyncio.open_connection) and loop.sock_connect, patched,
  because the Windows proactor connects without that audit event;
- files and databases under the real mind-data (the package-relative one, the main checkout's
  and the one LEEROY_MIND_DATA named when the run began): open, sqlite3.connect (plain paths
  and file: URIs), mkdir, rename/replace, remove, rmdir, listing, copy and the like;
- launching the mind service (`leeroy_mind` in a subprocess, os.system, spawn or exec), and
  the `ollama` CLI.

Tests marked `live` may reach the bridge's port and launch the service, but never on the real
mind-data (not by --data-dir, not by an env= without LEEROY_MIND_DATA); tests marked `ollama`
may reach Ollama's port and run its CLI. The real mind-data is never allowed. Paths are
compared after abspath and normcase, with an extended-length prefix read as the plain path;
an 8.3 short name or a junction to mind-data is not followed."""
import asyncio
import asyncio.base_events
import asyncio.proactor_events
import asyncio.selector_events
import os
import re
import socket
import subprocess
import sys
import tomllib
from collections.abc import Mapping
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from leeroy_mind import config

# default_data_dir() when LEEROY_MIND_DATA is not set: the package-relative mind-data
REAL_MIND_DATA = Path(config.__file__).resolve().parents[2] / "mind-data"
# the audit events that touch a path, with the positions of their path arguments
_PATH_EVENTS = {
    "open": (0,), "sqlite3.connect": (0,), "os.mkdir": (0,), "os.rename": (0, 1),
    "os.remove": (0,), "os.rmdir": (0,), "os.listdir": (0,), "os.scandir": (0,),
    "os.truncate": (0,), "os.chmod": (0,), "os.chown": (0,), "os.utime": (0,),
    "os.link": (0, 1), "os.symlink": (0, 1), "shutil.copyfile": (0, 1),
    "shutil.copymode": (0, 1), "shutil.copystat": (0, 1), "shutil.copytree": (0, 1),
    "shutil.rmtree": (0,), "shutil.move": (0, 1), "shutil.make_archive": (0, 2, 3),
    "shutil.unpack_archive": (0, 1)}
# where each launch event keeps the program and its arguments, and its environment
_LAUNCH_EVENTS = {"subprocess.Popen": ((0, 1), 3), "os.system": ((0,), None),
                  "os.posix_spawn": ((0, 1), 2), "os.spawn": ((1, 2), 3), "os.exec": ((0, 1), 2),
                  "os.startfile": ((0,), None)}
_OLLAMA_CLI = re.compile(r"""(?:^|[\\/\s'"\[,])ollama(?:\.exe)?(?=['"\s,\]]|$)""",
                         re.IGNORECASE)


class LiveServiceTouched(BaseException):
    """A test reached a live service. A BaseException, so the app's `except Exception`
    handlers cannot swallow it on the way out."""


def norm(p) -> str:
    s = os.fsdecode(os.fspath(p))
    if s.startswith("\\\\?\\UNC\\"):  # \\?\UNC\host\share: \\host\share
        s = "\\\\" + s[8:]
    elif s.startswith("\\\\?\\"):  # \\?\F:\x: F:\x
        s = s[4:]
    return os.path.normcase(os.path.abspath(s))


def live_ports() -> dict[int, str]:
    """The bridge's and Ollama's ports, from the defaults the service would use."""
    defaults = tomllib.loads(config.DEFAULT_TOML)
    ollama = urllib.parse.urlsplit(defaults["ollama"]["url"])
    return {defaults["bridge"]["port"]: "live", ollama.port or 80: "ollama"}


def _main_checkout() -> Path | None:
    """The repo's main checkout, from git's common dir: the parent of a worktree's mind-data
    is that worktree, not the owner's install."""
    here = Path(config.__file__).resolve().parents[2]
    try:
        out = subprocess.run(["git", "-C", str(here), "rev-parse", "--path-format=absolute",
                              "--git-common-dir"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    common = out.stdout.strip()
    return Path(common).parent if out.returncode == 0 and common else None


def guarded_dirs(environ) -> list[str]:
    """Every mind-data the suite must never touch: the package-relative one, the main
    checkout's (a run from a worktree guards both) and the one LEEROY_MIND_DATA named when the
    run began (conftest then points it at a tmp dir per test)."""
    dirs = [REAL_MIND_DATA]
    if (main := _main_checkout()) is not None:
        dirs.append(main / "mind-data")
    if environ.get("LEEROY_MIND_DATA"):
        dirs.append(Path(environ["LEEROY_MIND_DATA"]))
    return list(dict.fromkeys(norm(d) for d in dirs))


def _as_path(raw) -> str | None:
    if isinstance(raw, int):
        return None  # a file descriptor
    s = os.fsdecode(os.fspath(raw))
    if not s or s == ":memory:":
        return None
    if s.startswith("file:"):  # a sqlite URI, file:///F:/x/mind.db?mode=ro
        s = urllib.request.url2pathname(urllib.parse.urlsplit(s).path)
    return s


class Guard:
    def __init__(self):
        self.active = False
        self.allowed: frozenset[str] = frozenset()  # the running test's markers
        self.ports = live_ports()
        self.dirs = guarded_dirs(os.environ)  # at import: the session's own environment
        self.hits: list[str] = []
        self.data_dir: Path | None = None  # LEEROY_MIND_DATA for the running test
        self.originals: dict[str, object] = {}

    def hit(self, what: str) -> None:
        self.hits.append(what)
        raise LiveServiceTouched(what)

    def port(self, port, how: str) -> None:
        need = self.ports.get(port) if isinstance(port, int) else None
        if self.active and need is not None and need not in self.allowed:
            self.hit(f"{how} to port {port} (only tests marked {need!r} may)")

    def path(self, raw, how: str) -> None:
        if not self.active:
            return
        p = _as_path(raw)
        if p is None:
            return
        n = norm(p)
        if any(n == d or n.startswith(d + os.sep) for d in self.dirs):
            self.hit(f"{how} {p} (the live mind-data)")

    def launch(self, what, how: str, env=None) -> None:
        """`what`: the program and its arguments; `env`: the child's environment (None: this
        process's, where conftest points LEEROY_MIND_DATA at the test's own dir)."""
        if not self.active:
            return
        text = repr(what)
        if _OLLAMA_CLI.search(text) and "ollama" not in self.allowed:
            self.hit(f"{how} of the ollama CLI: {what!r} (only tests marked 'ollama' may)")
        if "leeroy_mind" not in text:
            return
        if "live" not in self.allowed:
            self.hit(f"{how} of the mind service: {what!r}")
        named = env.get("LEEROY_MIND_DATA") if isinstance(env, Mapping) else None
        shown = os.path.normcase(text.replace("\\\\", "\\"))  # repr doubles each backslash
        if (any(d in shown for d in self.dirs) or (named and norm(named) in self.dirs)
                or (isinstance(env, Mapping) and not named and "--data-dir" not in text)):
            self.hit(f"{how} of the mind service on the live mind-data: {what!r}")

    def verify(self) -> None:
        """Fail the test if anything reached a live service, even if the exception was eaten."""
        hits, self.hits = self.hits, []
        if hits:
            pytest.fail("a test reached a live service:\n  " + "\n  ".join(hits), pytrace=False)


GUARD = Guard()


def _audit(event: str, args: tuple) -> None:
    if not GUARD.active:
        return
    if event == "socket.connect":
        address = args[1] if len(args) > 1 else None
        if isinstance(address, tuple) and len(address) >= 2:
            GUARD.port(address[1], "socket.connect")
    elif event in _PATH_EVENTS:
        for i in _PATH_EVENTS[event]:
            if i < len(args) and isinstance(args[i], (str, bytes, os.PathLike)):
                GUARD.path(args[i], event)
    elif event in _LAUNCH_EVENTS:
        where, env_at = _LAUNCH_EVENTS[event]
        env = args[env_at] if env_at is not None and env_at < len(args) else None
        GUARD.launch([args[i] for i in where if i < len(args)], event, env=env)


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


def _patch_create_connection() -> None:
    """socket.create_connection (http.client, urllib and the Ollama client go through it) only
    closes its half-made socket on OSError, so it is refused before any socket exists."""
    real = socket.create_connection

    def create_connection(address, *args, **kw):
        if isinstance(address, tuple) and len(address) >= 2:
            GUARD.port(address[1], "socket.create_connection")
        return real(address, *args, **kw)
    GUARD.originals["socket.create_connection"] = real
    socket.create_connection = create_connection


def install() -> None:
    """Once per test session (conftest imports this module): the audit hook cannot be removed,
    and it does nothing while GUARD.active is off."""
    if "audit" in GUARD.originals:
        return
    sys.addaudithook(_audit)
    GUARD.originals["audit"] = _audit
    _patch_create_connection()
    _patch_asyncio()
