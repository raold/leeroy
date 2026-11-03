"""The microphone (Task 8, marked `device`; deselected by default). Run locally with
`pytest -q -m "windows or device" tests/test_mic_live.py`. It opens the input the local
voice.toml names (the session's LEEROY_VOICE_DATA, else voice-data beside the repo) for 1 s
through the Mic and the Capture, and is skipped when that names none. The name comes only from
there (PR15): none is written here, and a failure never prints one. Nothing is written to disk:
the audio stays in memory and is dropped."""
import time
from pathlib import Path

import pytest

from conftest import REAL_VOICE_DATA, SESSION_VOICE_DATA
from leeroy_voice.config import load_config

pytestmark = pytest.mark.device


def test_the_named_mic_opens_and_delivers_frames(recwarn):
    root = Path(SESSION_VOICE_DATA) if SESSION_VOICE_DATA else REAL_VOICE_DATA
    name = load_config(root / "voice.toml").input_device
    if not name:
        pytest.skip("the local voice.toml names no input device")
    pytest.importorskip("sounddevice")
    from leeroy_voice.audio_io import SoundDeviceApi
    from leeroy_voice.capture import STT_RATE, Capture, Mic

    class Counting(Capture):
        def __init__(self, *args):
            super().__init__(*args)
            self.fed = 0

        def feed(self, block):
            super().feed(block)
            self.fed += len(block)

    api = SoundDeviceApi()
    capture = Counting(48000, 1)
    mic = Mic(api, name, capture)
    try:
        mic.open()
        assert mic.ok and mic.state == "ok", "the input the local voice.toml names did not open"
        time.sleep(0.4)  # the ring fills
        capture.begin(0.4)
        time.sleep(0.3)
        capture.end(0.7)
        time.sleep(0.25)  # the tail
        deadline = time.monotonic() + 0.05
        while (clip := capture.take()) is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert mic.ok and mic.state == "ok"
        assert capture.fed >= 0.8 * capture.device_rate, "under 0.8 s of frames in 1 s"
        assert clip is not None and abs(len(clip) - 0.8 * STT_RATE) <= 0.05 * STT_RATE
        caught = [f"{w.category.__name__}: {w.message}" for w in recwarn]
        assert not caught, f"{len(caught)} warnings, the first: {caught[0]}"
    finally:
        mic.close()
        api.close()
    assert mic.state == "closed"
