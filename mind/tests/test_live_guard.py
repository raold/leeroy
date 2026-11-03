"""The guard in live_guard.py, proved without going near anything real. Each test guards a
listener or a directory of its own, so a broken guard would only reach that target, and the
test would then fail (no LiveServiceTouched, or the listener sees a connection)."""
import asyncio
import contextlib
import http.client
import logging
import os
import socket
import sqlite3
import subprocess
import sys
import urllib.request
from pathlib import Path

import pytest

from conftest import run, seed_test_cast
from live_guard import REAL_MIND_DATA, LiveServiceTouched, live_ports, norm
from leeroy_mind import config
from leeroy_mind.app import MindApp, acquire_instance_lock
from leeroy_mind.bridge import BridgeClient
from leeroy_mind.config import load_config
from leeroy_mind.llm.client import OllamaClient
from leeroy_mind.logs import setup_logging
from leeroy_mind.memory.writer import MemoryCandidate
from leeroy_mind.storage.db import open_db
from leeroy_mind.storage.store import Store


@contextlib.contextmanager
def owned_listener(live_guard, monkeypatch, need: str = "live"):
    """A listener on a free port this test owns, guarded like the bridge for this test."""
    listener = socket.create_server(("127.0.0.1", 0))
    listener.setblocking(False)
    port = listener.getsockname()[1]
    monkeypatch.setitem(live_guard.ports, port, need)
    try:
        yield listener, port
    finally:
        listener.close()


def nothing_reached(listener) -> bool:
    try:
        conn, _ = listener.accept()
    except BlockingIOError:
        return True
    conn.close()
    return False


def test_the_guard_is_armed_and_aimed_at_the_real_services(live_guard, tmp_path):
    assert live_guard.active and "audit" in live_guard.originals
    assert socket.create_connection is not live_guard.originals["socket.create_connection"]
    loops = asyncio.base_events.BaseEventLoop
    assert loops.create_connection is not live_guard.originals["create_connection"]
    for cls in (asyncio.proactor_events.BaseProactorEventLoop,
                asyncio.selector_events.BaseSelectorEventLoop):
        assert cls.sock_connect is not live_guard.originals[f"{cls.__name__}.sock_connect"]
    defaults = load_config(tmp_path / "absent.toml")  # what the service would connect to
    assert live_guard.ports == live_ports() == {defaults.bridge.port: "live", 7879: "live",
                                                11434: "ollama"}
    assert norm(REAL_MIND_DATA) in live_guard.dirs  # checked by name only, never opened
    assert config.default_data_dir() == live_guard.data_dir  # LEEROY_MIND_DATA, per test
    assert norm(live_guard.data_dir).startswith(norm(tmp_path))


def test_every_connect_route_is_refused_before_it_connects(live_guard, monkeypatch):
    with owned_listener(live_guard, monkeypatch) as (listener, port):
        def connect():
            with socket.socket() as s:
                s.connect(("127.0.0.1", port))

        def connect_ex():
            with socket.socket() as s:
                s.connect_ex(("127.0.0.1", port))

        def http_client():
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
            try:
                conn.request("GET", "/")
            finally:
                conn.close()

        async def open_connection():
            await asyncio.open_connection("127.0.0.1", port)

        async def create_connection():
            await asyncio.get_running_loop().create_connection(asyncio.Protocol, "127.0.0.1",
                                                               port)

        async def sock_connect():
            with socket.socket() as s:
                s.setblocking(False)
                await asyncio.get_running_loop().sock_connect(s, ("127.0.0.1", port))

        async def bridge_client():
            async def on_event(ev):
                pass
            await BridgeClient("127.0.0.1", port, on_event).run()

        routes = {
            "socket.create_connection": lambda: socket.create_connection(("127.0.0.1", port),
                                                                         1).close(),
            "socket.connect": connect,
            "socket.connect_ex": connect_ex,
            "http.client": http_client,
            "urllib": lambda: urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1),
            "asyncio.open_connection": lambda: run(open_connection()),
            "loop.create_connection": lambda: run(create_connection()),
            "loop.sock_connect": lambda: run(sock_connect()),
            "BridgeClient": lambda: run(bridge_client()),
            "OllamaClient": lambda: run(OllamaClient(f"http://127.0.0.1:{port}").list_models()),
        }
        for name, attempt in routes.items():
            with pytest.raises(LiveServiceTouched):
                attempt()
        assert nothing_reached(listener)  # refused before any connection was made
        assert len(live_guard.hits) == len(routes)
        live_guard.hits.clear()
        monkeypatch.setattr(live_guard, "allowed", frozenset({"live"}))  # a test marked live
        socket.create_connection(("127.0.0.1", port), 1).close()  # positive control
        assert not nothing_reached(listener) and live_guard.hits == []


def test_the_live_mind_data_is_refused_before_anything_opens(live_guard, tmp_path,
                                                             monkeypatch):
    import leeroy_mind.__main__ as cli
    main = cli.main
    pretend = tmp_path / "pretend-mind-data"  # stands in for the owner's: guarded alike
    monkeypatch.setattr(live_guard, "dirs", [*live_guard.dirs, norm(pretend)])
    served = []

    async def no_service(root, **kw):  # even a broken guard could never start the real one
        served.append(root)
        return 0
    monkeypatch.setattr(cli, "serve", no_service)
    db = pretend / "mind.db"
    routes = {
        "open": lambda: open(pretend / "notes.txt", "w"),
        "sqlite3.connect": lambda: sqlite3.connect(db),
        "sqlite3 uri": lambda: sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True),
        "open_db": lambda: open_db(db),
        "mkdir": lambda: pretend.mkdir(),
        "listing": lambda: list(pretend.glob("*")),
        "instance lock": lambda: acquire_instance_lock(pretend),
        "logs": lambda: setup_logging(pretend / "logs"),
        "db migrate": lambda: main(["db", "--data-dir", str(pretend), "migrate"]),
        "run": lambda: main(["run", "--data-dir", str(pretend)]),
    }
    for name, attempt in routes.items():
        with pytest.raises(LiveServiceTouched):
            attempt()
    # a regression in how the CLI finds its data dir lands on the guard, not on mind-data
    monkeypatch.setattr(cli, "default_data_dir", lambda: pretend)
    for argv in (["db", "migrate"], ["run"]):
        with pytest.raises(LiveServiceTouched):
            main(argv)
    assert not pretend.exists() and served == []
    assert len(live_guard.hits) == len(routes) + 2
    live_guard.hits.clear()


def test_launching_the_mind_service_is_refused(live_guard):
    # `--help` would be harmless if the guard failed: the test would then fail, not connect
    with pytest.raises(LiveServiceTouched):
        subprocess.run([sys.executable, "-m", "leeroy_mind", "--help"], capture_output=True)
    with pytest.raises(LiveServiceTouched):
        os.system(f'"{sys.executable}" -m leeroy_mind --help')
    assert len(live_guard.hits) == 2
    live_guard.hits.clear()
    done = subprocess.run([sys.executable, "-c", "print('fine')"], capture_output=True,
                          text=True)  # positive control: other programs still run
    assert done.stdout.strip() == "fine"


def test_a_hit_the_app_swallows_still_fails_the_test(live_guard, tmp_path, clock, monkeypatch,
                                                     caplog):
    """The LLM scheduler turns a dead job into SchedulerClosed and the writer stores the memory
    without a vector, so the guard's exception never reaches the test. The recorded hit still
    fails it at teardown."""
    with owned_listener(live_guard, monkeypatch, need="ollama") as (listener, port):
        s = Store(open_db(tmp_path / "mind.db"), clock=clock)
        seed_test_cast(s)
        s.close()
        app = MindApp(load_config(tmp_path / "mind.toml"), tmp_path,
                      llm=OllamaClient(f"http://127.0.0.1:{port}"), clock=clock)

        async def main():
            try:
                return await app.writer.write(MemoryCandidate(
                    owner=101, kind="observation", text="I heard the raid went well.",
                    t_event=1, actors=(101,), importance=3, valence=0.2, source="chat",
                    evidence=("chat:1",)))
            finally:
                await app.stop()
        with caplog.at_level(logging.ERROR, logger="leeroy_mind"):
            result = run(main())
        assert result.action == "inserted"  # the app carried on as if nothing happened
        assert "llm_job_died" in caplog.text  # the scheduler saw its job die
        assert nothing_reached(listener)
        assert any(str(port) in h for h in live_guard.hits)
        with pytest.raises(pytest.fail.Exception, match=str(port)):
            live_guard.verify()  # what the fixture runs at teardown
        assert live_guard.hits == []


def _main_checkout_mind_data() -> Path | None:
    here = Path(config.__file__).resolve().parents[2]
    done = subprocess.run(["git", "-C", str(here), "rev-parse", "--path-format=absolute",
                           "--git-common-dir"], capture_output=True, text=True)
    common = done.stdout.strip()
    return Path(common).parent / "mind-data" if done.returncode == 0 and common else None


def test_the_guard_covers_the_main_checkout_and_the_sessions_mind_data(live_guard):
    """Privacy minor 4, tests M10: run from a worktree, the suite still may not touch the main
    checkout's mind-data, nor the dir LEEROY_MIND_DATA named when the run began (by name
    only: nothing here opens them). Outside a git checkout there is no main checkout."""
    main_data = _main_checkout_mind_data()
    assert norm(REAL_MIND_DATA) in live_guard.dirs
    assert main_data is None or norm(main_data) in live_guard.dirs
    from live_guard import guarded_dirs
    named = Path("Z:/elsewhere/mind-data")
    assert norm(named) in guarded_dirs({"LEEROY_MIND_DATA": str(named)})
    assert norm(named) not in guarded_dirs({})
    assert main_data is None or norm(main_data) in guarded_dirs({})
    assert len(set(guarded_dirs({}))) == len(guarded_dirs({}))


def test_the_default_data_dir_is_the_one_the_guard_guards(monkeypatch):
    """Task 17 re-review N4: REAL_MIND_DATA restates config's formula; pin them together."""
    monkeypatch.delenv("LEEROY_MIND_DATA", raising=False)
    assert norm(config.default_data_dir()) == norm(REAL_MIND_DATA)


def test_norm_reads_an_extended_length_path_as_the_plain_one():
    plain = r"F:\somewhere\mind-data"
    assert norm("\\\\?\\" + plain) == norm(plain)
    assert norm(r"\\?\UNC\host\share\mind-data") == norm(r"\\host\share\mind-data")


def test_the_ollama_cli_is_refused_unless_the_test_is_marked(live_guard, monkeypatch):
    """N4: a child that is not leeroy_mind can still reach Ollama, `ollama ps` first."""
    with pytest.raises(LiveServiceTouched):
        subprocess.run(["ollama", "ps"], capture_output=True)  # refused before it starts
    with pytest.raises(LiveServiceTouched):
        live_guard.launch([r"C:\Programs\Ollama\ollama.exe", ["ollama.exe", "list"]], "x")
    assert len(live_guard.hits) == 2
    live_guard.hits.clear()
    monkeypatch.setattr(live_guard, "allowed", frozenset({"ollama"}))
    live_guard.launch(["ollama", ["ollama", "ps"]], "x")  # checked only, never launched
    live_guard.launch([sys.executable, ["-c", "print('ollamas are llamas')"]], "x")
    assert live_guard.hits == []


def test_a_live_launch_still_never_gets_the_real_mind_data(live_guard, monkeypatch):
    """N4: a test marked live may launch the service, never on the owner's mind-data: not
    by --data-dir, and not by an env= that drops LEEROY_MIND_DATA (the child would fall back
    to its default, the real one)."""
    monkeypatch.setattr(live_guard, "allowed", frozenset({"live"}))
    cmd = [sys.executable, "-m", "leeroy_mind", "run"]
    for args, env in ((cmd + ["--data-dir", str(REAL_MIND_DATA)], None),
                      (cmd, {"PATH": "x"}),
                      (cmd, {"LEEROY_MIND_DATA": str(REAL_MIND_DATA)})):
        with pytest.raises(LiveServiceTouched):
            live_guard.launch([sys.executable, args], "subprocess.Popen", env=env)
    assert len(live_guard.hits) == 3
    live_guard.hits.clear()
    with pytest.raises(LiveServiceTouched):  # the real route: refused before the launch
        subprocess.run([sys.executable, "-m", "leeroy_mind", "--help"], env={"X": "1"},
                       capture_output=True)
    live_guard.hits.clear()
    own = cmd + ["--data-dir", str(live_guard.data_dir)]
    live_guard.launch([sys.executable, own], "subprocess.Popen", env={"PATH": "x"})
    live_guard.launch([sys.executable, cmd], "subprocess.Popen")  # it inherits this test's
    assert live_guard.hits == []
