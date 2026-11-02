import logging
from pathlib import Path

import pytest

from leeroy_voice import config
from leeroy_voice.config import (DEFAULT_TOML, ConfigError, UiState, default_data_dir,
                                 load_config, load_ui_state, save_ui_state, vk_for)


def write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "voice.toml"
    p.write_text(text, encoding="utf-8")
    return p


def test_defaults_carry_the_owner_answers(tmp_path):
    c = load_config(tmp_path / "absent.toml")
    assert (c.mind_host, c.mind_port, c.ui_port) == ("127.0.0.1", 7879, 7880)
    assert c.input_device == "" and c.output_device == ""          # PR15: names only in the local file
    assert c.ptt_key == "\\" and c.ptt_vk == 0xDC and c.min_press_ms == 150 and c.max_hold_s == 30.0
    assert c.duck_db == 8.0 and c.comms_grit == 3 and c.stereo_spread == 0.0
    assert c.recognizer == "parakeet" and c.tts_threads == 6 and c.stt_threads == 4
    assert c.min_confidence == 0.4 and c.gpu_margin_gb == 1.0 and c.debug_record is False


def test_default_toml_is_the_defaults(tmp_path):
    assert load_config(write(tmp_path, DEFAULT_TOML)) == load_config(tmp_path / "absent.toml")


def test_devices_are_named_only_in_the_local_file(tmp_path):
    """PR15: the code names no device; the comments say what the empty defaults mean."""
    assert "the Windows default device" in DEFAULT_TOML
    assert "no microphone until you name one here" in DEFAULT_TOML
    c = load_config(write(tmp_path, '[devices]\ninput_device = "  Mic A "\n'
                                    'output_device = "Headphones B"\n'))
    assert (c.input_device, c.output_device) == ("Mic A", "Headphones B")


def test_local_values_win(tmp_path):
    c = load_config(write(tmp_path, '[ptt]\nkey = "vk:0x87"\nmax_hold_s = 20\n'
                                    '[sound]\nduck_db = 0\ncomms_grit = 0\n'
                                    '[debug]\nrecord = true\n'))
    assert (c.ptt_key, c.ptt_vk) == ("vk:0x87", 0x87)
    assert c.max_hold_s == 20.0 and type(c.max_hold_s) is float
    assert c.duck_db == 0.0 and type(c.duck_db) is float and c.comms_grit == 0
    assert c.debug_record is True


def test_loopback_only(tmp_path):
    with pytest.raises(ConfigError, match=r"mind\.host"):
        load_config(write(tmp_path, '[mind]\nhost = "0.0.0.0"\n'))
    for host in ("localhost", "::1"):
        assert load_config(write(tmp_path, f'[mind]\nhost = "{host}"\n')).mind_host == host


@pytest.mark.parametrize("text, key", [
    ("[sound]\nduck_db = -1", "sound.duck_db"),
    ("[sound]\nduck_db = 20.5", "sound.duck_db"),
    ("[sound]\ncomms_grit = 11", "sound.comms_grit"),
    ("[sound]\ncomms_grit = -1", "sound.comms_grit"),
    ("[sound]\ncomms_grit = 2.5", "sound.comms_grit"),
    ("[sound]\nstereo_spread = 0.3", "sound.stereo_spread"),
    ("[sound]\nstereo_spread = -0.1", "sound.stereo_spread"),
    ('[stt]\nrecognizer = "whisper"', "stt.recognizer"),
    ("[stt]\nmin_confidence = 1.5", "stt.min_confidence"),
    ("[ptt]\nmin_press_ms = 0", "ptt.min_press_ms"),
    ('[ptt]\nkey = "ctrl"', "ptt.key"),
    ("[tts]\nthreads = 0", "tts.threads"),
    ("[mind]\nport = 70000", "mind.port"),
    ("[mind]\nhost = []", "mind.host"),  # unhashable values are ConfigErrors too
    ("[mind]\nhost = {}", "mind.host"),
    ("[ui]\nport = 7879", "ui.port"),
    ("[debug]\nrecord = 1", "debug.record"),
    ("[gpu]\nmargin_gb = true", "gpu.margin_gb"),
])
def test_ranges(tmp_path, text, key):
    with pytest.raises(ConfigError, match=key.replace(".", r"\.")):
        load_config(write(tmp_path, text + "\n"))


def test_device_names_stay_out_of_errors(tmp_path):
    with pytest.raises(ConfigError, match=r"devices\.input_device") as e:
        load_config(write(tmp_path, '[devices]\ninput_device = ["Mic A"]\n'))
    assert "Mic A" not in str(e.value)


def test_unreadable_files(tmp_path):
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, "[sound\nduck_db = 8\n"))
    p = tmp_path / "voice.toml"
    p.write_bytes(b'[devices]\ninput_device = "\xff"\n')
    with pytest.raises(ConfigError, match="UTF-8"):
        load_config(p)
    with pytest.raises(ConfigError, match="sound"):
        load_config(write(tmp_path, 'sound = 3\n'))


def test_unknown_keys_are_warned(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="leeroy_voice"):
        c = load_config(write(tmp_path, "[sound]\nduck = 3\n[nope]\nx = 1\n"))
    assert c.duck_db == 8.0
    text = caplog.text
    assert "sound.duck" in text and "[nope]" in text


def test_vk_for():
    assert vk_for("\\") == 0xDC and vk_for("vk:0x87") == 0x87
    with pytest.raises(ConfigError): vk_for("ctrl")
    for bad in ("", "vk:0x00", "vk:0xff", "vk:0x100", "vk:87", "\\\\", "VK:0x87x"):
        with pytest.raises(ConfigError):
            vk_for(bad)
    assert vk_for("vk:0X7b") == 0x7B


def test_default_data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("LEEROY_VOICE_DATA", str(tmp_path / "vd"))
    assert default_data_dir() == tmp_path / "vd"
    monkeypatch.delenv("LEEROY_VOICE_DATA")
    assert default_data_dir() == Path(config.__file__).resolve().parents[2] / "voice-data"


def test_ui_state_round_trip(tmp_path):
    p = tmp_path / "ui-state.json"
    save_ui_state(p, UiState({101: 20.0, 102: -3.0}, frozenset({103})))
    assert load_ui_state(p) == UiState({101: 12.0, 102: -3.0}, frozenset({103}))  # clamped to ±12 dB


def test_ui_state_missing_or_damaged(tmp_path):
    p = tmp_path / "ui-state.json"
    assert load_ui_state(p) == UiState({}, frozenset())
    p.write_text("{not json", encoding="utf-8")
    assert load_ui_state(p) == UiState({}, frozenset())
    p.write_text('{"volumes": {"101": 3, "x": 1, "102": "loud", "104": -40, "105": NaN},'
                 ' "mutes": [103, "y", true, -2]}', encoding="utf-8")
    assert load_ui_state(p) == UiState({101: 3.0, 104: -12.0}, frozenset({103}))
    p.write_text("[1, 2]", encoding="utf-8")
    assert load_ui_state(p) == UiState({}, frozenset())


def test_ui_state_save_replaces_whole(tmp_path):
    p = tmp_path / "state" / "ui-state.json"
    save_ui_state(p, UiState({101: 1.5}, frozenset()))
    save_ui_state(p, UiState({}, frozenset({102})))
    assert load_ui_state(p) == UiState({}, frozenset({102}))
    assert [f.name for f in p.parent.iterdir()] == ["ui-state.json"]  # no temp file left
