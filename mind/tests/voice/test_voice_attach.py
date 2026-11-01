import asyncio
import os
import socket
import subprocess
import sys

import pytest

from conftest import run
from leeroy_mind.app import MindApp
from leeroy_mind.bridge import BridgeClient
from leeroy_mind.config import load_config
from leeroy_mind.protocol import Chat
from leeroy_mind.testing.fake_llm import FakeLLM
from leeroy_mind.voice import attach, wire
from leeroy_mind.voice.config import VoiceCfg
from live_guard import LiveServiceTouched
from voice.conftest import write_spec


def make_app(tmp_path, clock, toml=""):
    (tmp_path / "mind.toml").write_text(toml, encoding="utf-8")
    return MindApp(load_config(tmp_path / "mind.toml"), tmp_path, llm=FakeLLM(), clock=clock)


def test_inject_keeps_order():
    async def go():
        seen = []

        async def on_event(ev):
            seen.append(ev)
        bridge = BridgeClient("127.0.0.1", 9, on_event)
        chat = Chat(1, "guild", 1, "Testy", 0, "hello", 0, 0)
        bridge._enqueue(("event", "a"))
        bridge._enqueue(("event", "b"))
        bridge.inject(chat)
        bridge._enqueue(None)
        await bridge._dispatch()
        assert seen == ["a", "b", chat]
    run(go())


def test_attach_starts_nothing(tmp_path, clock, live_guard, monkeypatch):
    spawned = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: spawned.append(a))
    app = make_app(tmp_path, clock)
    assert app.voice.cfg.enabled is False
    assert app._background == [] and not spawned and not live_guard.hits
    assert app.voice.run_id == f"{int(clock.now()):x}"


def test_enabled_registers_background(tmp_path, clock):
    app = make_app(tmp_path, clock, "[voice]\nenabled = true\nport = 7999\n")
    assert len(app._background) == 1


def test_snapshot_order(tmp_path, clock):
    app = make_app(tmp_path, clock)
    write_spec(tmp_path, 101, "Arkon")
    write_spec(tmp_path, 102, "Belra")
    ext = app.voice
    snap = ext.snapshot()
    assert [wire.type_name(m) for m in snap] == ["session", "channels", "voice", "voice", "raid"]
    assert snap[0].online is False and snap[0].live_model == app.cfg.models.live
    assert snap[0].embed_model == app.cfg.models.embedding
    assert [m.guid for m in snap[2:4]] == [101, 102] and snap[2].name == "Arkon"
    assert snap[4] == wire.Raid("none", 0)
    for m in snap:
        wire.encode(m, 1)  # every message is valid on the wire


def test_send_ignores_voice_down(tmp_path, clock):
    app = make_app(tmp_path, clock)
    app.voice.send(wire.Raid("none", 0))  # not greeted: VoiceDown is swallowed


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def fake_repo(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "voice").mkdir(parents=True)
    monkeypatch.setattr(attach, "REPO_ROOT", repo)
    return repo


def spawner(monkeypatch):
    calls = []

    class FakePopen:
        def __init__(self, args, **kw):
            calls.append((args, kw))
    monkeypatch.setattr(attach.subprocess, "Popen", FakePopen)
    return calls


def test_autostart_once(tmp_path, clock, voice_data, fake_repo, monkeypatch):
    calls = spawner(monkeypatch)
    # This mocks a Windows launch, including constants absent from subprocess on Linux.
    monkeypatch.setattr(subprocess, "DETACHED_PROCESS", 1, raising=False)
    monkeypatch.setattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 2, raising=False)
    pythonw = fake_repo / "voice" / ".venv" / "Scripts" / "pythonw.exe"
    pythonw.parent.mkdir(parents=True)
    pythonw.write_bytes(b"")
    port = free_port()
    app = make_app(tmp_path, clock,
                   f"[voice]\nenabled = true\nautostart = true\nport = {port}\n")
    app.voice.ensure_service()
    assert len(calls) == 1
    args, kw = calls[0]
    assert [str(a) for a in args] == [str(pythonw), "-m", "leeroy_voice", "run"]
    assert kw["cwd"] == str(fake_repo / "voice") and kw["close_fds"] is True
    assert kw["stdin"] == subprocess.DEVNULL
    assert kw["creationflags"] == (subprocess.DETACHED_PROCESS
                                   | subprocess.CREATE_NEW_PROCESS_GROUP)
    assert (voice_data / "logs").is_dir()
    app.voice.ensure_service()  # once per mind start
    assert len(calls) == 1
    # a service that answers on the port is left alone
    listener = socket.create_server(("127.0.0.1", 0))
    try:
        app2 = make_app(tmp_path, clock, "[voice]\nenabled = true\nautostart = true\n"
                        f"port = {listener.getsockname()[1]}\n")
        app2.voice.ensure_service()
        assert len(calls) == 1
    finally:
        listener.close()


def test_autostart_skips_without_venv(tmp_path, clock, voice_data, fake_repo, monkeypatch,
                                      caplog):
    calls = spawner(monkeypatch)
    app = make_app(tmp_path, clock,
                   f"[voice]\nenabled = true\nautostart = true\nport = {free_port()}\n")
    with caplog.at_level("INFO"):
        app.voice.ensure_service()
    assert not calls
    assert any("voice_autostart_skipped" in r.getMessage() for r in caplog.records)


def test_autostart_off_never_spawns(tmp_path, clock, voice_data, fake_repo, monkeypatch):
    calls = spawner(monkeypatch)
    app = make_app(tmp_path, clock, f"[voice]\nenabled = true\nport = {free_port()}\n")
    app.voice.ensure_service()
    assert not calls


def test_live_guard_covers_voice(live_guard):
    with pytest.raises(LiveServiceTouched):
        socket.create_connection(("127.0.0.1", 7879))
    with pytest.raises(LiveServiceTouched):
        subprocess.run([sys.executable, "-m", "leeroy_voice", "run"])
    with pytest.raises(LiveServiceTouched):
        os.system(f'"{sys.executable}" -m leeroy_voice run')
    live_guard.hits.clear()


def test_the_voice_port_is_the_default():
    assert VoiceCfg().port == 7879


# -- fix round 1: nothing a message or a hook does may end the client's loop

async def _until(pred, limit=3.0):
    end = asyncio.get_running_loop().time() + limit
    while not pred():
        assert asyncio.get_running_loop().time() < end, "timed out"
        await asyncio.sleep(0.01)


def test_bad_message_is_skipped_and_snapshot_continues(tmp_path, clock, caplog):
    from leeroy_mind.testing.fake_voice import FakeVoice

    async def go():
        fake = FakeVoice()
        await fake.start()
        app = make_app(tmp_path, clock, f"[voice]\nenabled = true\nport = {fake.port}\n")
        (tmp_path / "voices").mkdir()
        (tmp_path / "voices" / "101.json").write_text(
            '{"guid": 101, "name": "Arkon", "speed": NaN}', encoding="utf-8")
        write_spec(tmp_path, 102, "Belra")
        task = asyncio.create_task(app.voice.client.run())
        with caplog.at_level("WARNING"):
            await _until(lambda: len(fake.received) >= 4)
        assert [wire.type_name(m) for m in fake.received] == [
            "session", "channels", "voice", "raid"]
        assert fake.received[2].guid == 102 and app.voice.client.greeted
        assert any("voice_message_dropped" in r.getMessage() for r in caplog.records)
        assert "Arkon" not in caplog.text
        await app.voice.client.close()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await fake.close()
    run(go())


def test_run_survives_a_failing_connection_hook(tmp_path, clock):
    from leeroy_mind.testing.fake_voice import FakeVoice

    async def go():
        fake = FakeVoice()
        await fake.start()
        app = make_app(tmp_path, clock, f"[voice]\nenabled = true\nport = {fake.port}\n")
        calls = []

        async def boom(up):
            calls.append(up)
            if up:
                raise RuntimeError("hook")
        app.voice.client._on_connection = boom
        task = asyncio.create_task(app.voice.client.run())
        await _until(lambda: calls == [True, False] or calls[:1] == [True])
        await fake.close()
        await _until(lambda: not app.voice.client.greeted)
        await fake.start()
        await clock.advance(5.5)
        await _until(lambda: calls.count(True) == 2)
        assert not task.done()
        await app.voice.client.close()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await fake.close()
    run(go())


def test_run_survives_an_endless_line_and_reconnects(tmp_path, clock):
    from leeroy_mind.testing.fake_voice import FakeVoice

    async def go():
        fake = FakeVoice()
        await fake.start()
        app = make_app(tmp_path, clock, f"[voice]\nenabled = true\nport = {fake.port}\n")
        client = app.voice.client
        task = asyncio.create_task(client.run())
        await _until(lambda: client.greeted)
        await fake.push_raw(b"a" * 400_000)  # no newline: the limit is overrun twice
        await _until(lambda: not client.greeted)
        assert not task.done()
        await clock.advance(5.5)
        await _until(lambda: client.greeted)
        await client.close()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await fake.close()
    run(go())


def test_serve_starts_the_client_when_ensure_service_fails(tmp_path, clock, caplog):
    from leeroy_mind.testing.fake_voice import FakeVoice

    async def go():
        fake = FakeVoice()
        await fake.start()
        app = make_app(tmp_path, clock, f"[voice]\nenabled = true\nport = {fake.port}\n")

        def boom():
            raise OSError("no logs dir")
        app.voice.ensure_service = boom
        task = asyncio.create_task(app.voice._serve())
        await _until(lambda: app.voice.client.greeted)
        assert any("voice_autostart_failed" in r.getMessage() for r in caplog.records)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await fake.close()
    run(go())


def test_bad_voice_config_leaves_the_mind_running(tmp_path, clock, caplog):
    with caplog.at_level("WARNING"):
        app = make_app(tmp_path, clock, '[voice]\nenabled = true\nhost = "0.0.0.0"\n')
    assert app.voice.cfg == VoiceCfg() and app._background == []
    assert any("voice_config_invalid" in r.getMessage() for r in caplog.records)


def test_slow_policy_does_not_stall_ticks_or_survive_shutdown(tmp_path, clock):
    from types import SimpleNamespace
    from leeroy_mind.testing.fake_voice import FakeVoice

    async def go():
        fake = FakeVoice()
        await fake.start()
        app = make_app(tmp_path, clock, f"[voice]\nenabled = true\nport = {fake.port}\n")
        started, ended, calls, ticks = asyncio.Event(), asyncio.Event(), [], []

        async def slow(now):
            calls.append(now)
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                ended.set()

        app.voice.elsewhere = SimpleNamespace(tick=slow)
        app.voice.tickers.append(lambda: ticks.append(clock.now()))
        task = asyncio.create_task(app.voice._serve())
        try:
            await _until(lambda: app.voice.client.greeted)
            await clock.advance(1)
            await _until(started.is_set)
            for _ in range(4):
                await clock.advance(1)
            assert len(calls) == 1 and len(ticks) >= 4
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await fake.close()
        assert ended.is_set() and not app.voice._policy_tasks
    run(go())


def test_policy_failure_is_contained(tmp_path, clock, caplog):
    from types import SimpleNamespace
    app = make_app(tmp_path, clock)

    async def fail(now):
        raise ValueError("private words must stay out of the log")

    run(app.voice._policy_tick("elsewhere", SimpleNamespace(tick=fail), clock.now()))
    assert "voice_handler_failed" in caplog.text
    assert "ValueError" in caplog.text and "private words" not in caplog.text
