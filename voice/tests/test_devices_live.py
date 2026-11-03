"""The real devices (Task 7, marked `device`; deselected by default). Run locally with
`pytest -q -m "device or windows" tests/test_devices_live.py`. Device names come only from the
local voice.toml (PR15): none is written here, and a failure never prints one. Nothing is
written to disk."""
import time
from pathlib import Path

import pytest

from conftest import REAL_VOICE_DATA, SESSION_VOICE_DATA
from leeroy_voice.config import load_config

pytestmark = pytest.mark.device


def local_config():
    """The local voice.toml: the session's LEEROY_VOICE_DATA, else voice-data beside the repo."""
    root = Path(SESSION_VOICE_DATA) if SESSION_VOICE_DATA else REAL_VOICE_DATA
    return load_config(root / "voice.toml")


def test_the_configured_microphone_is_listed():
    name = local_config().input_device
    if not name:
        pytest.skip("the local voice.toml names no input device")
    pytest.importorskip("sounddevice")
    from leeroy_voice.audio_io import SoundDeviceApi
    api = SoundDeviceApi()
    try:
        listed = any(d.name == name and d.inputs > 0 for d in api.wasapi_devices())
        assert listed, "the input device the local voice.toml names is not a WASAPI input"
    finally:
        api.close()


@pytest.mark.windows
def test_windows_names_its_devices_as_portaudio_does():
    """windows_has() rests on this: a name it denies is never refreshed for."""
    from leeroy_voice.audio_io import SoundDeviceApi
    api = SoundDeviceApi()
    try:
        names = api.wasapi_devices()
        assert names and all(api.windows_has(d.name) is True for d in names)
        assert api.windows_has("no such device") is False
    finally:
        api.close()


@pytest.mark.windows  # the default comes from the Windows render endpoint, through pycaw
def test_an_output_opens_on_the_default(recwarn):
    """Half a second of silence on the Windows default output, through AudioOut, without a
    warning: sounddevice's array streams set `.shape` in place on every block, which NumPy 2.5
    deprecates (a stream dies on it under -W error, or once NumPy drops the setter), so the
    streams are raw and the blocks wrapped here."""
    pytest.importorskip("sounddevice")
    pytest.importorskip("pycaw")
    from leeroy_voice.audio_io import BLOCK_FRAMES, AudioOut, SoundDeviceApi
    from leeroy_voice.mixer import Mixer

    class Counting(Mixer):
        def __init__(self):
            super().__init__()
            self.blocks: list[int] = []

        def render(self, frames):
            self.blocks.append(frames)
            return super().render(frames)

    api = SoundDeviceApi()
    mixer = Counting()
    out = AudioOut(mixer, api, pinned="")
    try:
        assert api.default_output_name(), "Windows has no default output"
        out.start()
        assert out.ok, "the default output did not open"
        time.sleep(0.5)
        assert out.ok
        assert len(mixer.blocks) >= 20 and set(mixer.blocks) == {BLOCK_FRAMES}
        caught = [f"{w.category.__name__}: {w.message}" for w in recwarn]
        assert not caught, f"{len(caught)} warnings, the first: {caught[0]}"
    finally:
        out.stop()
        api.close()
