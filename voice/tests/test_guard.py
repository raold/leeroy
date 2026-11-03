"""The guard in conftest.py, proved without going near anything real. Most routes are tried on
a listener or a directory the test owns and guards for itself, so a broken guard would only
reach that; the real ports and data dirs are tried only through calls refused before any
socket or file exists, with a probe parent that does not exist, so even a broken guard could
write nothing there."""
import asyncio
import contextlib
import importlib
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import types
import uuid
from pathlib import Path

import pytest

from conftest import (GUARD, REAL_MIND_DATA, REAL_VOICE_DATA, GuardTripped, _main_checkout,
                      guarded_dirs, hide_modules, norm, run)
from leeroy_voice import config


@contextlib.contextmanager
def owned_listener(live_guard, monkeypatch, need: str = "live"):
    """A listener on a free port this test owns, guarded like a live port for this test."""
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


def tripped(live_guard, attempt) -> None:
    with pytest.raises(GuardTripped):
        attempt()
    assert live_guard.hits
    live_guard.hits.clear()


def test_the_guard_is_armed_and_aimed(live_guard, tmp_path):
    assert live_guard is GUARD and live_guard.active
    assert socket.create_connection is not GUARD.originals["socket.create_connection"]
    assert socket.create_server is not GUARD.originals["socket.create_server"]
    assert sys.meta_path[0] is GUARD.originals["finder"]
    defaults = config.load_config(tmp_path / "absent.toml")  # what the service would bind
    assert live_guard.ports == {7878: None, defaults.mind_port: "live",
                                defaults.ui_port: "live", 11434: "ollama"}
    assert set(live_guard.ports) == {7878, 7879, 7880, 11434}
    for d in (REAL_VOICE_DATA, REAL_MIND_DATA):  # checked by name only, never opened
        assert norm(d) in live_guard.dirs
    assert config.default_data_dir() == live_guard.data_dir
    assert norm(live_guard.data_dir).startswith(norm(tmp_path))
    assert norm(os.environ["LEEROY_MIND_DATA"]).startswith(norm(tmp_path))


def test_the_main_checkouts_data_is_guarded_from_a_worktree():
    main = _main_checkout() or REAL_VOICE_DATA.parent
    dirs = guarded_dirs({"LEEROY_VOICE_DATA": "X:/elsewhere/vd", "LEEROY_MIND_DATA": "X:/md"})
    for d in (main / "voice-data", main / "mind-data", Path("X:/elsewhere/vd"), Path("X:/md")):
        assert norm(d) in dirs


def test_guard_blocks_live_ports(live_guard, monkeypatch):
    assert socket.create_connection is not GUARD.originals["socket.create_connection"]
    with pytest.raises(GuardTripped):  # refused before any socket exists
        socket.create_connection(("127.0.0.1", 7879))
    live_guard.hits.clear()
    with owned_listener(live_guard, monkeypatch) as (listener, port):
        def connect():
            with socket.socket() as s:
                s.connect(("127.0.0.1", port))

        def connect_ex():
            with socket.socket() as s:
                s.connect_ex(("127.0.0.1", port))

        async def open_connection():
            await asyncio.open_connection("127.0.0.1", port)

        async def sock_connect():
            with socket.socket() as s:
                s.setblocking(False)
                await asyncio.get_running_loop().sock_connect(s, ("127.0.0.1", port))

        routes = [lambda: socket.create_connection(("127.0.0.1", port), 1).close(), connect,
                  connect_ex, lambda: run(open_connection()), lambda: run(sock_connect())]
        for attempt in routes:
            with pytest.raises(GuardTripped):
                attempt()
        assert nothing_reached(listener)
        assert len(live_guard.hits) == len(routes)
        live_guard.hits.clear()
        monkeypatch.setattr(live_guard, "allowed", frozenset({"live"}))  # a test marked live
        socket.create_connection(("127.0.0.1", port), 1).close()
        assert not nothing_reached(listener) and live_guard.hits == []
    monkeypatch.setattr(live_guard, "allowed", frozenset())
    with socket.create_server(("127.0.0.1", 0)) as own:  # port 0 works
        socket.create_connection(own.getsockname(), 1).close()
    assert live_guard.hits == []


def test_guard_blocks_binding_live_ports(live_guard, monkeypatch):
    with owned_listener(live_guard, monkeypatch) as (listener, port):
        listener.close()  # free the port again; it stays guarded

        async def serve():
            await asyncio.start_server(lambda r, w: None, "127.0.0.1", port)
        tripped(live_guard, lambda: socket.create_server(("127.0.0.1", port)))
        tripped(live_guard, lambda: run(serve()))


def test_the_bridge_port_is_refused_whatever_the_markers(live_guard, monkeypatch):
    """leeroy-voice never talks to the game bridge, which serves one mind: 7878 is refused for
    every test, marked live or not. Tried on a listener of the test's own, guarded the way 7878
    is, so a broken guard reaches only that."""
    every = frozenset({"live", "ollama", "gpu", "device", "windows", "engines", "bridge"})
    with owned_listener(live_guard, monkeypatch, need=live_guard.ports[7878]) as (listener, port):
        monkeypatch.setattr(live_guard, "allowed", every)
        tripped(live_guard, lambda: socket.create_connection(("127.0.0.1", port), 1))
        assert nothing_reached(listener)
    assert live_guard.ports[7878] is None  # no marker allows it


def test_ollama_needs_its_marker(live_guard, monkeypatch):
    with owned_listener(live_guard, monkeypatch, need="ollama") as (listener, port):
        monkeypatch.setattr(live_guard, "allowed", frozenset({"live"}))
        tripped(live_guard, lambda: socket.create_connection(("127.0.0.1", port), 1))
        monkeypatch.setattr(live_guard, "allowed", frozenset({"ollama"}))
        socket.create_connection(("127.0.0.1", port), 1).close()
        assert not nothing_reached(listener)


def test_guard_blocks_devices(live_guard, monkeypatch):
    for name in ("sounddevice", "pycaw", "pycaw.pycaw", "comtypes", "comtypes.client"):
        with pytest.raises(GuardTripped):  # installed or not (M14)
            importlib.import_module(name)
    with pytest.raises(GuardTripped):
        import sounddevice  # noqa: F401
    live_guard.hits.clear()
    finder = GUARD.originals["finder"]
    monkeypatch.setattr(live_guard, "allowed", frozenset({"device"}))  # a test marked device
    assert finder.find_spec("sounddevice") is None  # left to the real finders
    tripped(live_guard, lambda: finder.find_spec("comtypes"))
    monkeypatch.setattr(live_guard, "allowed", frozenset({"windows"}))
    assert finder.find_spec("pycaw") is None and finder.find_spec("comtypes.client") is None
    assert finder.find_spec("soundfile") is None  # not a device library


def test_guard_blocks_win32_input(live_guard, monkeypatch):
    """Task 8: leeroy_voice.winapi (Raw Input, the key state, SendInput, the token checks) is for
    `windows` tests only, and hotkey reaches Win32 through it alone, so no default test hooks,
    reads or presses a real key."""
    from leeroy_voice import hotkey
    for attempt in (lambda: importlib.import_module("leeroy_voice.winapi"),
                    lambda: hotkey.key_pressed(0x87), lambda: hotkey.send_key(0x87, True),
                    hotkey.self_elevated, hotkey.wow_elevated,
                    hotkey.RawInputKeys(0x87, print, on_close=print).start):
        tripped(live_guard, attempt)
    finder = GUARD.originals["finder"]
    monkeypatch.setattr(live_guard, "allowed", frozenset({"windows"}))
    assert finder.find_spec("leeroy_voice.winapi") is None  # left to the real finders
    monkeypatch.setattr(live_guard, "allowed", frozenset({"device"}))
    tripped(live_guard, lambda: finder.find_spec("leeroy_voice.winapi"))
    assert finder.find_spec("leeroy_voice.hotkey") is None  # the rest of the package is free


def test_an_earlier_winapi_import_is_hidden(live_guard, monkeypatch):
    fake = types.ModuleType("leeroy_voice.winapi")
    monkeypatch.setitem(sys.modules, "leeroy_voice.winapi", fake)
    hidden = hide_modules(frozenset())
    try:
        assert hidden == {"leeroy_voice.winapi": fake}
        tripped(live_guard, lambda: importlib.import_module("leeroy_voice.winapi"))
        assert hide_modules(frozenset({"windows"})) == {}
    finally:
        sys.modules.update(hidden)


def test_an_earlier_import_is_hidden(live_guard, monkeypatch):
    """A module a marked test imported earlier is not served from sys.modules."""
    fake = types.ModuleType("sounddevice")
    monkeypatch.setitem(sys.modules, "sounddevice", fake)
    hidden = hide_modules(frozenset())
    try:
        assert hidden == {"sounddevice": fake} and "sounddevice" not in sys.modules
        tripped(live_guard, lambda: importlib.import_module("sounddevice"))
        assert hide_modules(frozenset({"device"})) == {}
    finally:
        sys.modules.update(hidden)


def test_guard_blocks_real_data_writes(live_guard):
    probe = f"guard-probe-{uuid.uuid4().hex}"  # never exists: a broken guard writes nothing
    voice_target = REAL_VOICE_DATA / probe / "x"
    mind_target = REAL_MIND_DATA / probe / "sub"
    tripped(live_guard, lambda: open(voice_target, "w"))
    tripped(live_guard, lambda: os.mkdir(mind_target))
    tripped(live_guard, lambda: mind_target.mkdir())  # no parents=True: nothing is created
    tripped(live_guard, lambda: voice_target.write_text("x"))


def test_every_write_route_is_refused(live_guard, monkeypatch, tmp_path):
    pretend = tmp_path / "pretend-voice-data"  # stands in for the real one: guarded alike
    pretend.mkdir()
    (pretend / "voice.toml").write_text("[sound]\n", encoding="utf-8")
    (pretend / "models").mkdir()
    monkeypatch.setattr(live_guard, "dirs", [*live_guard.dirs, norm(pretend)])
    outside = tmp_path / "outside"
    outside.mkdir()
    routes = [
        lambda: open(pretend / "new.txt", "w"),
        lambda: open(pretend / "voice.toml", "a"),
        lambda: open(pretend / "voice.toml", "r+b"),
        lambda: os.open(pretend / "fd.txt", os.O_WRONLY | os.O_CREAT),
        lambda: (pretend / "made").mkdir(),
        lambda: os.rename(pretend / "voice.toml", outside / "moved.toml"),
        lambda: os.replace(outside, pretend / "in"),
        lambda: os.remove(pretend / "voice.toml"),
        lambda: os.rmdir(pretend / "models"),
        lambda: shutil.rmtree(pretend / "models"),
        lambda: shutil.copyfile(outside / "none", pretend / "copy.txt"),
        lambda: shutil.move(pretend / "voice.toml", outside),
        lambda: sqlite3.connect(pretend / "x.db"),
        lambda: os.utime(pretend / "voice.toml"),
    ]
    for attempt in routes:
        with pytest.raises(GuardTripped):
            attempt()
    assert len(live_guard.hits) == len(routes)
    live_guard.hits.clear()
    assert (pretend / "voice.toml").read_text(encoding="utf-8") == "[sound]\n"  # reads pass
    assert sorted(p.name for p in pretend.iterdir()) == ["models", "voice.toml"]
    (outside / "fine.txt").write_text("ok", encoding="utf-8")  # elsewhere is not guarded
    assert live_guard.hits == []


def test_guard_blocks_nvidia_smi(live_guard, monkeypatch, tmp_path):
    missing = tmp_path / "nvidia-smi.exe"  # never runs: even a broken guard starts nothing
    tripped(live_guard, lambda: subprocess.Popen([str(missing), "-L"]))
    tripped(live_guard, lambda: subprocess.run(f'"{missing}" -L', shell=True))
    tripped(live_guard, lambda: os.system(f'"{missing}" --query-gpu=memory.free'))
    out = subprocess.run([sys.executable, "-c", "print('ok')"], capture_output=True, text=True)
    assert out.stdout.strip() == "ok" and live_guard.hits == []  # other programs run
    monkeypatch.setattr(live_guard, "allowed", frozenset({"gpu"}))
    with pytest.raises(FileNotFoundError):  # allowed: the launch itself is attempted
        subprocess.Popen([str(missing), "-L"])
    assert live_guard.hits == []


def test_a_swallowed_trip_is_still_reported(live_guard):
    try:
        socket.create_connection(("127.0.0.1", 7880))
    except BaseException:  # code under test that eats everything
        pass
    assert live_guard.hits
    with pytest.raises(pytest.fail.Exception, match="7880"):
        live_guard.verify()
    assert live_guard.hits == []  # verify() took them, so this test's teardown passes
