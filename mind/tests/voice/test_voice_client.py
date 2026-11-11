import asyncio
import json

import pytest

from conftest import run
from leeroy_mind.config import ConfigError, load_config
from leeroy_mind.testing.fake_voice import FakeVoice, FakeVoiceClient
from leeroy_mind.voice import wire
from leeroy_mind.voice.brief import load_specs
from leeroy_mind.voice.client import VoiceClient, VoiceDown
from leeroy_mind.voice.config import VoiceCfg, load_voice_config


async def until(pred, limit=3.0):
    end = asyncio.get_running_loop().time() + limit
    while not pred():
        assert asyncio.get_running_loop().time() < end, "timed out"
        await asyncio.sleep(0.01)


async def stop(client, task):
    await client.close()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def make_client(port, clock, log):
    async def on_message(msg):
        log.append(("msg", msg))

    async def on_connection(up):
        log.append(("conn", up))
    return VoiceClient("127.0.0.1", port, on_message, clock=clock, on_connection=on_connection)


def test_client_greets_and_snapshots(clock):
    async def go():
        fake = FakeVoice()
        await fake.start()
        log = []
        client = make_client(fake.port, clock, log)

        async def on_connection(up):
            log.append(("conn", up))
            if up:
                for m in (wire.Session(True, 1, "live:test", "embed:test"),
                          wire.VoiceDef(101, "Arkon", {"version": 1}), wire.Raid("none", 0)):
                    client.send(m)
        client._on_connection = on_connection
        task = asyncio.create_task(client.run())
        await until(lambda: ("conn", True) in log and len(fake.received) >= 3)
        assert client.greeted and client.hello.server == "leeroy-voice"
        assert [wire.type_name(m) for m in fake.received] == ["session", "voice", "raid"]
        await fake.push(wire.Status(True, "ok", "none", "parakeet", False, 0, ()))
        await until(lambda: client.status is not None)
        assert client.status.stt == "parakeet"
        await until(lambda: any(k == "msg" for k, _ in log))
        await stop(client, task)
        await fake.close()
        assert ("conn", False) in log
    run(go())


def test_send_when_down(clock):
    async def go():
        fake = FakeVoice()
        await fake.start()
        client = make_client(fake.port, clock, [])
        with pytest.raises(VoiceDown):
            client.send(wire.Raid("none", 0))
        task = asyncio.create_task(client.run())
        await until(lambda: client.greeted)
        client.send(wire.Raid("none", 0))
        await fake.close()
        await until(lambda: not client.greeted)
        with pytest.raises(VoiceDown):
            client.send(wire.Raid("none", 0))
        await stop(client, task)
    run(go())


def test_reconnects_every_5_s(clock):
    async def go():
        fake = FakeVoice()
        await fake.start()
        port = fake.port
        log = []
        client = make_client(port, clock, log)
        task = asyncio.create_task(client.run())
        await until(lambda: client.greeted)
        await fake.close()
        await until(lambda: not client.greeted)
        await fake.start()  # the same port again
        assert fake.port == port
        await clock.advance(5.5)
        await until(lambda: client.greeted)
        assert log.count(("conn", True)) == 2
        await stop(client, task)
        await fake.close()
    run(go())


class Refusing:
    """asyncio.open_connection while nothing listens: the clock time of each try."""

    def __init__(self, clock, real):
        self.clock, self.real, self.down, self.tries = clock, real, True, []

    async def __call__(self, *args, **kwargs):
        self.tries.append(self.clock.now())
        if self.down:
            raise ConnectionRefusedError("refused")
        return await self.real(*args, **kwargs)


def voice_lines(caplog):
    return [r.getMessage() for r in caplog.records if r.name == "leeroy_mind.voice.client"]


def test_a_stopped_service_is_logged_on_change_and_retried_at_most_once_a_minute(fast_clock, monkeypatch, caplog):
    async def go():
        refusing = Refusing(fast_clock, asyncio.open_connection)
        monkeypatch.setattr(asyncio, "open_connection", refusing)
        client = make_client(1, fast_clock, [])
        task = asyncio.create_task(client.run())
        await fast_clock.advance(2 * 3600 + 30, step=5)
        await stop(client, task)
        return refusing.tries

    with caplog.at_level("INFO", logger="leeroy_mind.voice.client"):
        tries = run(go())
    start = tries[0]
    gaps = [b - a for a, b in zip(tries, tries[1:])]
    assert gaps[:5] == [5, 10, 20, 40, 60] and set(gaps[4:]) == {60}
    assert 120 <= len(tries) <= 126  # not one every 7 s
    lines = voice_lines(caplog)
    assert lines[0] == "voice_connect_failed: reason=ConnectionRefusedError"
    still = [line for line in lines[1:] if line.startswith("voice_still_down: ")]
    assert len(lines) == 3 and len(still) == 2
    assert still[0].startswith("voice_still_down: failures=") and "reason=ConnectionRefusedError" in still[0]
    assert tries[-1] - start >= 2 * 3600


def test_a_service_that_comes_back_is_one_line_and_starts_a_new_count(fast_clock, monkeypatch, caplog):
    async def go():
        fake = FakeVoice()
        await fake.start()
        refusing = Refusing(fast_clock, asyncio.open_connection)
        monkeypatch.setattr(asyncio, "open_connection", refusing)
        log = []
        client = make_client(fake.port, fast_clock, log)
        task = asyncio.create_task(client.run())
        await fast_clock.advance(200, step=5)
        refusing.down = False
        await fast_clock.advance(60, step=5)
        await until(lambda: client.greeted)
        await fake.close()
        await until(lambda: not client.greeted)
        refusing.down = True
        await fast_clock.advance(30, step=5)
        await stop(client, task)
        return log

    with caplog.at_level("INFO", logger="leeroy_mind.voice.client"):
        log = run(go())
    lines = voice_lines(caplog)
    assert [line.split(":")[0] for line in lines] == ["voice_connect_failed", "voice_reconnected",
                                                      "voice_connect_failed"]
    assert lines[1].startswith("voice_reconnected: failures=7 down_s=")
    assert log.count(("conn", True)) == 1


def test_fake_voice_client_sends_only_when_greeted():
    f = FakeVoiceClient()
    with pytest.raises(VoiceDown):
        f.send(wire.Raid("none", 0))
    f.greeted = True
    f.send(wire.Raid("none", 0))
    assert f.sent == [wire.Raid("none", 0)] and f.status is None


def test_load_specs(tmp_path):
    folder = tmp_path / "voices"
    folder.mkdir()
    for name, body in (("101", {"guid": 101, "name": "Arkon"}), ("102", {"guid": 102}),
                       ("103", {"guid": 999}), ("105", [1, 2]), ("106", {"guid": "106"}),
                       ("brief", {"guid": 1})):
        (folder / f"{name}.json").write_text(json.dumps(body), encoding="utf-8")
    (folder / "104.json").write_text("not json {", encoding="utf-8")
    assert sorted(load_specs(tmp_path)) == [101, 102]
    assert load_specs(tmp_path / "nowhere") == {}


def test_config_defaults_and_loopback(tmp_path):
    cfg = load_voice_config(tmp_path / "mind.toml")  # no file
    assert cfg == VoiceCfg()
    assert (cfg.enabled, cfg.host, cfg.port, cfg.autostart) == (False, "127.0.0.1", 7879, False)
    assert (cfg.on_comms_raider, cfg.on_comms_social) == (0.9, 0.6)
    assert (cfg.hot_mic_per_month, cfg.elsewhere_lines_per_10min) == (1.0, 6)
    p = tmp_path / "mind.toml"
    p.write_text("[bridge]\nport = 7878\n", encoding="utf-8")
    assert load_voice_config(p) == VoiceCfg()
    p.write_text("[voice]\nenabled = true\nport = 7999\non_comms_raider = 1\n", encoding="utf-8")
    cfg = load_voice_config(p)
    assert cfg.enabled and cfg.port == 7999 and cfg.on_comms_raider == 1.0
    for bad in ('host = "0.0.0.0"', "port = 0", 'enabled = "yes"', "on_comms_social = 2",
                "elsewhere_lines_per_10min = 1.5", "autostart = 1"):
        p.write_text(f"[voice]\n{bad}\n", encoding="utf-8")
        with pytest.raises(ConfigError):
            load_voice_config(p)
    p.write_text("voice = 3\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_voice_config(p)


def test_voice_section_not_warned(tmp_path, caplog):
    p = tmp_path / "mind.toml"
    p.write_text("[voice]\nenabled = true\n", encoding="utf-8")
    with caplog.at_level("WARNING"):
        load_config(p)
    assert not [r for r in caplog.records if "voice" in r.getMessage()]
