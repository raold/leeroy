"""Distant hot-mic scenes use local DSP and fake engines, never model files or devices."""
import threading

import numpy as np
import pytest

from conftest import eventually, run
from leeroy_voice import hotmic
from leeroy_voice.voices import spec_from_dict
from test_fx import band, noise, spec
from test_service import Rig


class BedRng:
    def __init__(self, bed, seed=5):
        self.bed, self.rng = bed, np.random.default_rng(seed)
    def choice(self, options):
        return self.bed if options == hotmic.BEDS else options[0]
    def standard_normal(self, size):
        return self.rng.standard_normal(size)
    def integers(self, *args, **kwargs):
        return self.rng.integers(*args, **kwargs)


def test_render(fake_engine, fake_bank, tmp_path):
    plain = noise(3, rms=0.05)
    result = hotmic.render_hotmic(plain, 24000, spec(), rng=BedRng("room"),
                                   engine=fake_engine, bank=fake_bank)
    assert result.dtype == np.float32 and result.ndim == 1 and np.isfinite(result).all()
    assert 2 <= len(result) / 48000 <= 6
    prefix = result[:24000]
    speech = result[24000:24000 + 3 * 48000]
    assert 0 < np.sqrt(np.mean(prefix ** 2)) < 0.003
    assert band(speech, 3000, 12000, 48000) < band(speech, 300, 2400, 48000) - 20
    assert np.max(np.abs(result[-336:])) > np.max(np.abs(prefix)) * 5
    assert not list(tmp_path.rglob("*.wav"))


def test_beds_differ(fake_engine, fake_bank):
    rendered = [hotmic.render_hotmic(noise(3), 24000, spec(), rng=BedRng(bed),
                                      engine=fake_engine, bank=fake_bank) for bed in hotmic.BEDS]
    assert all(not np.array_equal(a, b) for i, a in enumerate(rendered) for b in rendered[i + 1:])
    bands = [(band(x, 300, 1000, 48000), band(x, 1000, 3000, 48000)) for x in rendered]
    assert len({tuple(round(value, 1) for value in pair) for pair in bands}) == 3


@pytest.mark.parametrize("seconds", [.1, 20])
def test_total_duration_bound(fake_engine, fake_bank, seconds):
    result = hotmic.render_hotmic(noise(seconds), 24000, spec(), rng=BedRng("room"),
                                   engine=fake_engine, bank=fake_bank)
    assert 2 <= len(result) / 48000 <= 6


def test_tv_uses_another_base_and_never_writes(fake_bank, tmp_path):
    calls = []
    class Engine:
        sample_rate = 24000
        def synth(self, text, voice, style, speed):
            calls.append((text, voice))
            return noise(2)
    original = spec()
    hotmic.render_hotmic(noise(3), 24000, original, rng=BedRng("tv"), engine=Engine(), bank=fake_bank)
    assert len(calls) == 1 and calls[0][0] == hotmic.TV_LINE
    assert not set(calls[0][1].timbre) & set(original.timbre)
    assert calls[0][1].engine == "kokoro" and calls[0][1].ref == ""
    assert not list(tmp_path.rglob("*.wav"))


def test_hotmic_service_synthesizes_once_on_tts_thread_and_skips_stock(tmp_path, fake_bank):
    async def test():
        async with Rig(tmp_path, fake_bank) as rig:
            await rig.snapshot()
            await rig.stock_rendered()
            before = {p: p.read_bytes() for p in rig.data.rglob("*.wav")}
            calls = []
            def render(audio, sr, voice, **kwargs):
                calls.append((threading.current_thread().name, len(audio)))
                return np.full(48000 * 2, .01, np.float32)
            rig.service._hotmic = render
            rig.service._fx = lambda *a, **kw: pytest.fail("hotmic reached ordinary mic chain")
            # 'Ready.' is cached normally; hotmic must take the synthesis thread anyway.
            await rig.mind.send(rig.speak("hm", text="Ready.", kind="hotmic"))
            await eventually(lambda: bool(calls))
            assert calls[0][0] == "leeroy-voice-tts"
            assert "Ready." in rig.engine.said
            await rig.play_out()
            assert len(calls) == 1
            assert {p: p.read_bytes() for p in rig.data.rglob("*.wav")} == before
    run(test())


def test_hotmic_multi_sentence_is_one_bounded_scene(tmp_path, fake_bank):
    async def test():
        async with Rig(tmp_path, fake_bank) as rig:
            await rig.snapshot()
            calls = []
            rig.service._hotmic = lambda audio, *a, **kw: (calls.append(len(audio)) or np.zeros(96000, np.float32))
            await rig.mind.send(rig.speak("hm", text="One thing. Another thing.", kind="hotmic"))
            await eventually(lambda: bool(calls))
            assert len(rig.service._jobs["hm"].line.segments) == 1
            await rig.play_out()
            assert len(calls) == 1
    run(test())
