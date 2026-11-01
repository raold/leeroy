"""`python -m leeroy_voice` beyond fetch-models (Task 11): where, held, doctor's machine checks,
devices, selftest and run, with fake probes and fakes for every device: nothing live is
touched. test_doctor.py holds the doctor's files and models."""
import logging
import os

import numpy as np
import pytest
import soundfile as sf

from conftest import REAL_VOICE_DATA, voices_bin
from fakes import FakeDeviceApi, FakeKeys, FakeProbes, FakeRecognizer, FakeSessions
from leeroy_voice import __main__ as cli
from leeroy_voice import fx, hotkey, models, service
from leeroy_voice.__main__ import main
from leeroy_voice.audio_io import Dev
from leeroy_voice.engines.fake import FakeEngine
from leeroy_voice.service import ServiceLock
from leeroy_voice.stock import STOCK
from leeroy_voice.stt.names import RosterCorrector
from leeroy_voice.voices import StyleBank

DEVICES = [Dev("Mic A", 1, 0, 48000.0), Dev("Speakers A", 0, 2, 48000.0)]
NAMED = '[devices]\ninput_device = "Mic A"\noutput_device = "Speakers A"\n'


def lines(capsys) -> list[str]:
    return capsys.readouterr().out.splitlines()


@pytest.fixture
def data(live_guard):
    live_guard.data_dir.mkdir()
    return live_guard.data_dir


def probes(monkeypatch, **kw) -> FakeProbes:
    p = FakeProbes(**{"devices": DEVICES, "ducking": 3, **kw})
    monkeypatch.setattr(cli, "PROBES", p)
    return p


# -- where --------------------------------------------------------------------------------------

def test_where_follows_the_data_dir(tmp_path, monkeypatch, capsys):
    """N8: LEEROY_VOICE_DATA and that dir's ports; unset, the default dir and 7879, 7880."""
    other = tmp_path / "other"
    other.mkdir()
    (other / "voice.toml").write_text("[mind]\nport = 17879\n[ui]\nport = 17880\n",
                                      encoding="utf-8")
    monkeypatch.setenv("LEEROY_VOICE_DATA", str(other))
    assert main(["where"]) == 0
    assert lines(capsys) == [f"data_dir={other}", "mind_port=17879", "ui_port=17880"]
    monkeypatch.delenv("LEEROY_VOICE_DATA")
    assert main(["where"]) == 0
    assert lines(capsys) == [f"data_dir={REAL_VOICE_DATA}", "mind_port=7879", "ui_port=7880"]


def test_where_on_a_bad_file_is_one_line(data, capsys):
    (data / "voice.toml").write_text('[mind]\nhost = "0.0.0.0"\n', encoding="utf-8")
    assert main(["where"]) == 1
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1 and "mind.host" in err[0]


# -- held ---------------------------------------------------------------------------------------

def test_held_says_whether_a_service_holds_the_lock(data, capsys):
    """What ac-voice.sh asks before it starts one: 0 held, 1 free, and it prints the pid."""
    assert main(["held"]) == 1
    lock = ServiceLock(data).acquire()
    (data / "voice.pid").write_text("4242\n", encoding="utf-8")
    try:
        assert main(["held"]) == 0
        assert lines(capsys) == ["4242"]
    finally:
        lock.release()
    assert main(["held"]) == 1


# -- doctor -------------------------------------------------------------------------------------

def test_doctor_lines(data, capsys, monkeypatch):
    """With fake probes: one line per check, nothing live touched, no device's name."""
    (data / "voice.toml").write_text(NAMED, encoding="utf-8")
    probes(monkeypatch)
    assert main(["doctor"]) == 0
    out = lines(capsys)
    assert "ok   input device: the one voice.toml names is listed" in out
    assert "ok   output device: the one voice.toml names is listed" in out
    assert "ok   mind port 7879: free" in out and "ok   ui port 7880: free" in out
    assert 'ok   communications ducking: 3, "Do nothing"' in out
    assert all(line[:4] in ("ok  ", "note") for line in out)
    assert not any("Mic A" in line or "Speakers A" in line for line in out)


def test_doctor_fails_on_a_missing_device(data, capsys, monkeypatch):
    (data / "voice.toml").write_text(NAMED, encoding="utf-8")
    probes(monkeypatch, devices=[])
    assert main(["doctor"]) == 1
    out = lines(capsys)
    assert any(line.startswith("FAIL input device:") for line in out)
    assert any(line.startswith("FAIL output device:") for line in out)


def test_doctor_on_unnamed_devices(data, capsys, monkeypatch):
    """No device named: nothing to list, and PortAudio is not started for nothing."""
    p = probes(monkeypatch)
    assert main(["doctor"]) == 0
    out = lines(capsys)
    assert any(line.startswith("note input device: none named") for line in out)
    assert "ok   output device: the Windows default" in out
    assert p.device_reads == 0


def test_doctor_reports_an_unreadable_device_list(data, capsys, monkeypatch):
    (data / "voice.toml").write_text(NAMED, encoding="utf-8")
    probes(monkeypatch).device_error = OSError("no PortAudio")
    assert main(["doctor"]) == 1
    assert any(line.startswith("FAIL devices: OSError") for line in lines(capsys))


def test_doctor_on_the_ports(data, capsys, monkeypatch):
    """A port another program holds fails; one this service holds is fine, by its voice.pid."""
    (data / "voice.pid").write_text("4242\n", encoding="utf-8")
    lock = ServiceLock(data).acquire()
    try:
        probes(monkeypatch, owners={7879: 4242, 7880: 9999})
        assert main(["doctor"]) == 1
        out = lines(capsys)
        assert "ok   mind port 7879: held by this service" in out
        assert "FAIL ui port 7880: held by process 9999" in out
    finally:
        lock.release()


def test_doctor_reads_the_configured_ports(data, capsys, monkeypatch):
    (data / "voice.toml").write_text("[mind]\nport = 17879\n[ui]\nport = 17880\n",
                                     encoding="utf-8")
    probes(monkeypatch)
    assert main(["doctor"]) == 0
    out = lines(capsys)
    assert "ok   mind port 17879: free" in out and "ok   ui port 17880: free" in out


@pytest.mark.parametrize("value", [0, 1, 2, None])
def test_doctor_warns_on_communications_ducking(data, capsys, monkeypatch, value):
    """M30: anything but 3 is a warning: it does not fail the doctor."""
    probes(monkeypatch, ducking=value)
    assert main(["doctor"]) == 0
    out = [line for line in lines(capsys) if "communications ducking" in line]
    assert len(out) == 1 and out[0].startswith("note ") and "warning" in out[0]


def test_doctor_fails_on_a_bad_lexicon(data, capsys, monkeypatch):
    (data / "lexicon.toml").write_text('Arkon = "not ipa g"\n', encoding="utf-8")
    probes(monkeypatch)
    assert main(["doctor"]) == 1
    assert any(line.startswith("FAIL lexicon:") for line in lines(capsys))


# -- devices ------------------------------------------------------------------------------------

def test_devices_lists_them(data, capsys, monkeypatch):
    monkeypatch.setattr(cli, "device_api", lambda: FakeDeviceApi(
        outputs=["Speakers A", "Headphones B"], inputs=["Mic A"], default="Headphones B"))
    assert main(["devices"]) == 0
    out = lines(capsys)
    assert "input   Mic A" in out and "output  Speakers A" in out
    assert "output  Headphones B  (default)" in out


# -- selftest -----------------------------------------------------------------------------------

@pytest.fixture
def small_voice(data, monkeypatch):
    """A FakeEngine and a bank on one pack named as the test voice."""
    pack = np.random.default_rng(1).standard_normal((510, 1, 256)).astype(np.float32)
    path = data / "voices.bin"
    path.write_bytes(voices_bin({"af_heart": pack}))
    monkeypatch.setattr(cli, "load_engine", lambda root, cfg: (FakeEngine(), StyleBank(path)))


def test_selftest_writes_a_stock_line(data, small_voice, capsys):
    assert main(["selftest"]) == 0
    audio, rate = sf.read(data / "debug" / "selftest.wav", dtype="float32")
    assert rate == 48000 and audio.ndim == 1
    assert 0.1 < len(audio) / rate < 5 and np.abs(audio).max() > 0.01
    assert any("selftest.wav" in line for line in lines(capsys))


def test_selftest_follows_the_grit(data, small_voice, monkeypatch):
    seen = []
    real = fx.process

    def spy(audio, sr, spec, *, grit, whisper):
        seen.append((spec.timbre, grit, whisper))
        return real(audio, sr, spec, grit=grit, whisper=whisper)
    monkeypatch.setattr(fx, "process", spy)
    (data / "voice.toml").write_text("[sound]\ncomms_grit = 7\n", encoding="utf-8")
    assert main(["selftest"]) == 0
    assert seen == [({"af_heart": 1.0}, 7, False)]


def test_selftest_play_opens_the_output(data, small_voice, monkeypatch):
    api = FakeDeviceApi(outputs=["Speakers A"], default="Speakers A")
    monkeypatch.setattr(cli, "device_api", lambda: api)
    monkeypatch.setattr(cli, "PLAY_MARGIN_S", 0.0)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    assert main(["selftest", "--play"]) == 0
    assert api.opened() == ["Speakers A"] and api.events[-1] == ("close", "Speakers A")


def test_selftest_ptt_reports_the_injected_key(data, small_voice, monkeypatch, capsys):
    """F24 injected and seen down and up, through the same keys the service uses."""
    made = {}

    def keys(vk, on_key, *, on_close):
        made["keys"] = FakeKeys(vk, on_key, on_close=on_close)
        return made["keys"]
    monkeypatch.setattr(cli, "key_factory", lambda: keys)
    monkeypatch.setattr(cli, "inject_key", lambda vk, down: made["keys"].key(down))
    assert main(["selftest", "--ptt"]) == 0
    assert made["keys"].vk == 0x87 and made["keys"].stopped
    assert "ok   ptt: F24 seen down and up" in lines(capsys)


def test_selftest_ptt_fails_when_the_key_is_not_seen(data, small_voice, monkeypatch, capsys):
    monkeypatch.setattr(cli, "key_factory", lambda: FakeKeys)
    monkeypatch.setattr(cli, "inject_key", lambda vk, down: None)
    monkeypatch.setattr(cli, "KEY_WAIT_S", 0.02)
    assert main(["selftest", "--ptt"]) == 1
    assert any(line.startswith("FAIL ptt:") for line in lines(capsys))


def test_selftest_ptt_wants_both_edges(data, small_voice, monkeypatch, capsys):
    """Down without up is a key that would stick."""
    made = {}

    def keys(vk, on_key, *, on_close):
        made["keys"] = FakeKeys(vk, on_key, on_close=on_close)
        return made["keys"]
    monkeypatch.setattr(cli, "key_factory", lambda: keys)
    monkeypatch.setattr(cli, "inject_key", lambda vk, down: down and made["keys"].key(True))
    monkeypatch.setattr(cli, "KEY_WAIT_S", 0.02)
    assert main(["selftest", "--ptt"]) == 1


def test_selftest_without_kokoro_is_one_line(data, capsys):
    assert main(["selftest"]) == 1
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1 and "kokoro" in err[0]


# -- run ----------------------------------------------------------------------------------------

@pytest.fixture
def small_run(data, small_voice, monkeypatch):
    """`run` over fakes, and a service that leaves at its first idle check."""
    monkeypatch.setattr(cli, "device_api", lambda: FakeDeviceApi(outputs=["Speakers A"],
                                                                 default="Speakers A"))
    monkeypatch.setattr(cli, "session_api", lambda: FakeSessions({"Wow.exe": 0.8}))
    monkeypatch.setattr(cli, "key_factory", lambda: FakeKeys)
    monkeypatch.setattr(cli, "load_recognizer", lambda root, cfg: FakeRecognizer())
    monkeypatch.setattr(cli, "load_corrector",
                        lambda root: RosterCorrector((), (), common=frozenset()))
    monkeypatch.setattr(service, "IDLE_EXIT_S", 0.0)
    monkeypatch.setattr(hotkey, "wow_elevated", lambda: None)
    monkeypatch.setattr(hotkey, "foreground_process", lambda: "")
    (data / "voice.toml").write_text("[mind]\nport = 17879\n[ui]\nport = 17880\n",
                                     encoding="utf-8")


def test_run_holds_the_lock_writes_the_pid_and_logs(data, small_run, monkeypatch):
    seen = {}
    real = service.VoiceService.run

    async def spy(self):
        seen["cfg"] = self._cfg
        return await real(self)
    monkeypatch.setattr(service.VoiceService, "run", spy)
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    monkeypatch.setenv("HF_HUB_OFFLINE", "0")
    assert main(["run"]) == 0
    assert seen["cfg"].mind_port == 17879
    assert os.environ["HF_HUB_OFFLINE"] == "1" and os.environ["HF_HUB_DISABLE_TELEMETRY"] == "1"
    assert os.environ["HF_HOME"] == str(data / "hf-cache")
    text = (data / "logs" / "voice.log").read_text(encoding="utf-8")
    assert "mind_port_open" in text and "ui_port_open" in text and "service_stopped" in text
    assert not (data / "voice.pid").exists()
    ServiceLock(data).acquire().release()
    assert not logging.getLogger("leeroy_voice").handlers


def test_run_while_one_runs_is_one_line(data, small_run, capsys):
    lock = ServiceLock(data).acquire()
    try:
        assert main(["run"]) == 1
    finally:
        lock.release()
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1 and "voice.lock" in err[0]


def test_run_with_no_recognizer_reports_stt_off(data, small_run, monkeypatch):
    """A model that will not load costs his side only."""
    monkeypatch.setattr(cli, "load_recognizer", lambda root, cfg: None)
    assert main(["run"]) == 0


def test_run_without_its_engine_is_one_line(data, capsys, monkeypatch):
    def nothing(root, cfg):
        raise models.ModelError("kokoro: not fetched")
    monkeypatch.setattr(cli, "load_engine", nothing)
    assert main(["run"]) == 1
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1 and "kokoro" in err[0]


def test_the_stock_lines_are_in_the_selftest():
    assert cli.SELFTEST_KEY in STOCK
