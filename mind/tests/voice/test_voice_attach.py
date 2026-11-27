import asyncio
import os
import socket
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from conftest import run
from leeroy_mind.app import MindApp
from leeroy_mind.bridge import BridgeClient
from leeroy_mind.config import load_config
from leeroy_mind.lifecycle import VOICE_HOLD_FILE, hold_voice, voice_hold
from leeroy_mind.protocol import Chat
from leeroy_mind.testing.fake_llm import FakeLLM
from leeroy_mind.voice import attach, wire
from leeroy_mind.voice.config import VoiceCfg
from live_guard import LiveServiceTouched
from voice.conftest import write_spec
from shipped_off import off


def make_app(tmp_path, clock, toml=""):
    (tmp_path / "mind.toml").write_text(off(toml), encoding="utf-8")
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
    assert app._background == [app.nightly.run_forever, app.guild.run, app.control.serve, app.realm.run]
    assert not spawned and not live_guard.hits
    assert app.voice.run_id == f"{int(clock.now()):x}"


def test_enabled_registers_background(tmp_path, clock):
    app = make_app(tmp_path, clock, "[voice]\nenabled = true\nport = 7999\n")
    assert app._background == [app.nightly.run_forever, app.voice._serve, app.guild.run, app.control.serve,
                               app.realm.run]


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
    assert app.voice._relaunch_due(clock.now() + 86400) is None  # nor does the supervisor


def supervised(tmp_path, clock, fake_repo, monkeypatch, *, answers=False):
    """An extension with autostart on, a voice venv, a fake launch and a port that never
    answers (or always does)."""
    calls = spawner(monkeypatch)
    monkeypatch.setattr(subprocess, "DETACHED_PROCESS", 1, raising=False)  # absent on Linux
    monkeypatch.setattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 2, raising=False)
    pythonw = fake_repo / "voice" / ".venv" / "Scripts" / "pythonw.exe"
    pythonw.parent.mkdir(parents=True)
    pythonw.write_bytes(b"")
    app = make_app(tmp_path, clock,
                   f"[voice]\nenabled = true\nautostart = true\nport = {free_port()}\n")
    monkeypatch.setattr(app.voice, "_port_open", lambda: answers)
    return app.voice, calls


def events(caplog, name):
    return [r for r in caplog.records if r.getMessage().startswith(name)]


def test_the_supervisor_starts_a_service_that_went_down_again(tmp_path, fast_clock, voice_data,
                                                               fake_repo, monkeypatch, caplog):
    """The service went down with the mind running (the 11-21/22 outages lasted until the next
    mind restart): the mind starts it again after RELAUNCH_AFTER_S, then backs off."""
    clock = fast_clock
    ext, calls = supervised(tmp_path, clock, fake_repo, monkeypatch)

    async def go():
        ext.client.greeted = True
        await ext.on_connection(True)
        ext.client.greeted = False
        await ext.on_connection(False)
        launched = []
        for _ in range(int(2 * 3600 / attach.SUPERVISE_S)):
            await clock.advance(attach.SUPERVISE_S, step=attach.SUPERVISE_S)
            before = len(calls)
            await ext._supervise_once()
            if len(calls) > before:
                launched.append(clock.now())
        return launched
    start = clock.now()
    with caplog.at_level("INFO"):
        launched = run(go())
    gaps = [round(b - a) for a, b in zip([start, *launched], launched)]
    assert gaps[:5] == [60, 120, 300, 600, 1800] and set(gaps[5:]) <= {1800}
    first = events(caplog, "voice_relaunched")[0]
    assert "why=down" in first.getMessage() and "attempt=1" in first.getMessage()
    args, kw = calls[0]
    assert [str(a) for a in args][1:] == ["-m", "leeroy_voice", "run"]
    assert kw["cwd"] == str(fake_repo / "voice")

    async def back(up_s):  # it greets, stays up for up_s, and goes down again
        ext.client.greeted = True
        await ext.on_connection(True)
        await clock.advance(up_s, step=up_s)
        ext.client.greeted = False
        await ext.on_connection(False)
        await clock.advance(attach.RELAUNCH_AFTER_S, step=attach.RELAUNCH_AFTER_S)
        await ext._supervise_once()
    n = len(calls)
    run(back(30))  # a crash soon after its start: the backoff goes on (30 min)
    assert len(calls) == n
    run(back(attach.STABLE_S))  # up long enough: the next outage starts from the first wait
    assert len(calls) == n + 1


def test_the_supervisor_waits_for_a_launch_and_never_doubles_a_live_service(
        tmp_path, fast_clock, voice_data, fake_repo, monkeypatch):
    clock = fast_clock
    ext, calls = supervised(tmp_path, clock, fake_repo, monkeypatch)
    ext.ensure_service()  # the mind's start: the service loads its models before it listens
    assert len(calls) == 1

    async def go():
        await clock.advance(attach.RELAUNCH_AFTER_S + 5, step=attach.RELAUNCH_AFTER_S + 5)
        await ext._supervise_once()
        assert len(calls) == 1  # its start still has START_GRACE_S
        await clock.advance(attach.START_GRACE_S, step=attach.START_GRACE_S)
        await ext._supervise_once()
        assert len(calls) == 2
    run(go())


def test_the_supervisor_leaves_a_service_that_answers_alone(tmp_path, fast_clock, voice_data,
                                                            fake_repo, monkeypatch):
    """Started by the console or ac-voice.sh while the client waits out its retry: listening,
    so nothing is launched beside it."""
    clock = fast_clock
    ext, calls = supervised(tmp_path, clock, fake_repo, monkeypatch, answers=True)

    async def go():
        await ext.on_connection(False)
        await clock.advance(3600, step=3600)
        await ext._supervise_once()
    run(go())
    assert calls == []


def test_an_operator_stop_holds_until_his_login(tmp_path, fast_clock, voice_data, fake_repo,
                                                monkeypatch, caplog):
    """A stop from the console (or ac-voice.sh) leaves voice.stopped: the mind leaves the service
    down for the rest of his session and starts it at his next login."""
    clock = fast_clock
    ext, calls = supervised(tmp_path, clock, fake_repo, monkeypatch)

    async def go():
        ext.client.greeted = True
        await ext.on_connection(True)
        hold_voice(voice_data, "console")
        ext.client.greeted = False
        await ext.on_connection(False)
        for _ in range(int(3 * 3600 / attach.SUPERVISE_S)):
            await clock.advance(attach.SUPERVISE_S, step=attach.SUPERVISE_S)
            await ext._supervise_once()
        assert calls == []
        await ext._on_login(SimpleNamespace(human=True, guid=1))
        await ext._supervise_once()
    with caplog.at_level("INFO"):
        run(go())
    assert len(calls) == 1 and voice_hold(voice_data) is None
    held = events(caplog, "voice_relaunch_held")
    assert len(held) == 1 and "by=console" in held[0].getMessage()
    assert events(caplog, "voice_hold_lifted")
    assert "why=login" in events(caplog, "voice_relaunched")[0].getMessage()


def test_a_stop_after_a_login_that_found_it_up_still_holds(tmp_path, fast_clock, voice_data,
                                                           fake_repo, monkeypatch):
    clock = fast_clock
    ext, calls = supervised(tmp_path, clock, fake_repo, monkeypatch)

    async def go():
        ext.client.greeted = True
        await ext.on_connection(True)
        await ext._on_login(SimpleNamespace(human=True, guid=1))
        await clock.advance(attach.SUPERVISE_S, step=attach.SUPERVISE_S)
        await ext._supervise_once()
        hold_voice(voice_data, "console")  # he stops it mid-session
        ext.client.greeted = False
        await ext.on_connection(False)
        await clock.advance(3600, step=3600)
        await ext._supervise_once()
    run(go())
    assert calls == [] and voice_hold(voice_data) is not None


@pytest.mark.parametrize("already_up", [True, False])
def test_new_stop_wins_with_an_older_or_reused_filesystem_timestamp(
        tmp_path, fast_clock, voice_data, fake_repo, monkeypatch, already_up):
    ext, calls = supervised(tmp_path, fast_clock, fake_repo, monkeypatch)
    hold_voice(voice_data, "console")
    old = time.time() - 600
    os.utime(voice_data / VOICE_HOLD_FILE, (old, old))

    async def go():
        ext.client.greeted = already_up
        if already_up:
            await ext.on_connection(True)
        await ext._on_login(SimpleNamespace(human=True, guid=1))
        hold_voice(voice_data, "console")
        # A new stop can share an old mtime on a coarse filesystem. It must not
        # be removed as the stop observed before the greeting/login.
        os.utime(voice_data / VOICE_HOLD_FILE, (old, old))
        ext.client.greeted = False
        await ext.on_connection(False)
        await fast_clock.advance(3600, step=3600)
        await ext._supervise_once()
        assert calls == [] and voice_hold(voice_data) is not None
        await ext._on_login(SimpleNamespace(human=True, guid=1))
        await ext._supervise_once()
        assert len(calls) == 1 and voice_hold(voice_data) is None

    run(go())


def test_a_stop_after_his_login_cancels_the_logins_pending_start(tmp_path, fast_clock,
                                                                 voice_data, fake_repo,
                                                                 monkeypatch):
    """Launched at 0, his login at 5 (the launch still has its grace), a Stop at 10: the
    stop is the newer wish, so the login does not start it at 90; his next login does."""
    clock = fast_clock
    ext, calls = supervised(tmp_path, clock, fake_repo, monkeypatch)
    ext.ensure_service()
    assert len(calls) == 1

    async def go():
        await clock.advance(5, step=5)
        await ext._on_login(SimpleNamespace(human=True, guid=1))
        await ext._supervise_once()
        assert len(calls) == 1  # the launch is still loading
        ext._login_wall -= 5  # the wall clock as the fake one: his login came 5 s before
        await clock.advance(5, step=5)
        hold_voice(voice_data, "console")
        await clock.advance(3600, step=3600)
        await ext._supervise_once()
        assert len(calls) == 1 and voice_hold(voice_data) is not None
        await ext._on_login(SimpleNamespace(human=True, guid=1))
        await ext._supervise_once()
    run(go())
    assert len(calls) == 2 and voice_hold(voice_data) is None


def test_a_hold_older_than_the_last_greeting_is_stale(tmp_path, fast_clock, voice_data,
                                                      fake_repo, monkeypatch):
    """Something started the service after the stop (by hand, say): the hold no longer
    applies, and the next outage is supervised as any other."""
    clock = fast_clock
    ext, calls = supervised(tmp_path, clock, fake_repo, monkeypatch)
    hold_voice(voice_data, "script")
    old = time.time() - 600
    os.utime(voice_data / VOICE_HOLD_FILE, (old, old))

    async def go():
        ext.client.greeted = True
        await ext.on_connection(True)
        ext.client.greeted = False
        await ext.on_connection(False)
        await clock.advance(attach.RELAUNCH_AFTER_S, step=attach.RELAUNCH_AFTER_S)
        await ext._supervise_once()
    run(go())
    assert len(calls) == 1 and voice_hold(voice_data) is None


def test_the_minds_own_start_lifts_a_hold(tmp_path, clock, voice_data, fake_repo, monkeypatch):
    """A mind restart has always brought the service back: a hold from before it ends there."""
    ext, calls = supervised(tmp_path, clock, fake_repo, monkeypatch)
    hold_voice(voice_data, "console")
    ext.ensure_service()
    assert len(calls) == 1 and voice_hold(voice_data) is None


def test_the_supervisor_runs_only_with_autostart(tmp_path, clock, voice_data, fake_repo,
                                                 monkeypatch):
    from leeroy_mind.testing.fake_voice import FakeVoice
    spawner(monkeypatch)
    seen = []

    async def go(toml):
        fake = FakeVoice()
        await fake.start()
        app = make_app(tmp_path, clock, toml.format(port=fake.port))
        app.voice.ensure_service = lambda: None

        async def supervise():
            seen.append(toml)
        app.voice._supervise = supervise
        task = asyncio.create_task(app.voice._serve())
        await _until(lambda: app.voice.client.greeted)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await fake.close()
    run(go("[voice]\nenabled = true\nport = {port}\n"))
    assert seen == []
    run(go("[voice]\nenabled = true\nautostart = true\nport = {port}\n"))
    assert len(seen) == 1


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
    assert app.voice.cfg == VoiceCfg(enabled=False, autostart=False)  # an invalid table is off
    assert app._background == [app.nightly.run_forever, app.guild.run, app.control.serve, app.realm.run]
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


def test_real_events_drive_group_afk_and_member_rejoin(vapp):
    from leeroy_mind.protocol import AfkEvent, Group, Login, Logout
    from voice.test_voice_movement import ready

    async def check():
        await ready(vapp)
        await vapp.app.handle(Group(vapp._ts(), 102, True, (1, 101, 102)))
        assert vapp.ext.rooms.where[1] == vapp.ext.rooms.where[101] == "raid"
        await vapp.app.handle(AfkEvent(vapp._ts(), 101, True))
        assert vapp.ext.rooms.where[101] == "afk"
        await vapp.app.handle(AfkEvent(vapp._ts(), 101, False))
        assert vapp.ext.rooms.where[101] == "raid"
        await vapp.app.handle(Logout(vapp._ts(), 101, False))
        assert 101 not in vapp.ext.rooms.where and 101 not in vapp.ext.on_comms
        assert not vapp.rows("SELECT 1 FROM voice_presence WHERE guid=101 AND until IS NULL")
        await vapp.app.handle(Login(vapp._ts(), 101, False))
        assert vapp.ext.rooms.where[101] == "raid" and 101 in vapp.ext.on_comms
        await vapp.app.handle(Group(vapp._ts(), 0, False, ()))
        assert vapp.ext.rooms.where[101] == "guild_hall"
        assert vapp.rows("SELECT room FROM voice_presence WHERE guid=101 AND until IS NULL")[0][0] == "guild_hall"
    run(check())
