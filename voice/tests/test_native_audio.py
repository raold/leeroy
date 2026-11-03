"""Native output preserves the engine waveform apart from level and playback sample rate.

Synthetic signals, fake synthesis and fake devices only; no live audio or model access.
"""
from dataclasses import replace

import numpy as np
import pytest

from conftest import run
from leeroy_voice import __main__ as cli
from leeroy_voice import cast, fx
from leeroy_voice.config import load_config
from leeroy_voice.decision import Decision
from leeroy_voice.engines import EngineError
from leeroy_voice.engines import expressive
from leeroy_voice.engines.expressive import ExpressiveEngine
from leeroy_voice.engines.fake import FakeEngine
from leeroy_voice.stock import STOCK, StockCache
from test_fx import spec
from test_service import Rig


def signal(sr):
    t = np.arange(sr, dtype=np.float64) / sr
    # Low fundamentals, a high overtone, unequal word levels and true pauses all survive.
    x = (0.2 * np.sin(2 * np.pi * 45 * t) + 0.07 * np.sin(2 * np.pi * 6500 * t))
    x[sr // 4:sr // 2] *= 0.1
    x[:sr // 8] = x[-sr // 8:] = 0
    return x.astype(np.float32)


@pytest.mark.parametrize("mic", list(fx.MIC_PROFILES))
@pytest.mark.parametrize("whisper", [False, True])
@pytest.mark.parametrize("sr", [24000, 48000])
def test_native_waveform_has_no_pitch_filter_compression_or_added_noise(mic, whisper, sr):
    dry = signal(sr)
    voice = spec(mic=mic, pitch_st=2.0)
    result = fx.process(dry, sr, voice, grit=10, whisper=whisper)
    expected = fx.to_48k(fx.normalize_loudness(dry, sr), sr)
    assert np.array_equal(result, expected)
    assert result.dtype == np.float32 and result.shape == (48000,)
    assert np.max(np.abs(result[:4000])) < 1e-7  # no added room; allow resampler roundoff
    if sr == 48000:
        nonzero = np.abs(dry) > 0.01
        gains = result[nonzero] / dry[nonzero]
        assert np.allclose(gains, gains[0], rtol=2e-7)  # one scalar, not compression


def test_native_path_never_constructs_legacy_effects(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("native output reached optional voice effects")
    for name in ("chain_for", "shift_pitch", "room_tone"):
        monkeypatch.setattr(fx, name, forbidden)
    assert np.isfinite(fx.process(signal(24000), 24000, spec(), grit=10, whisper=True)).all()


@pytest.mark.parametrize("invalid", [1, "false", None])
def test_effects_flag_is_not_truthy_coerced(invalid):
    with pytest.raises(ValueError, match="effects_enabled"):
        fx.process(signal(24000), 24000, spec(), grit=0, whisper=False,
                   effects_enabled=invalid)


@pytest.mark.parametrize("kind", ["open", "whisper", "stock", "hotmic"])
def test_service_default_bypasses_effects_for_every_playback_path(tmp_path, fake_bank, kind):
    async def exercise():
        async with Rig(tmp_path, fake_bank, comms_grit=10) as rig:
            await rig.snapshot()
            await rig.stock_rendered()
            before = {p: p.read_bytes() for p in rig.data.rglob("*.wav")}
            said_before = len(rig.engine.said)
            seen = []
            real = rig.service._fx

            def process(audio, sr, voice, **kwargs):
                assert kwargs["effects_enabled"] is False
                actual = real(audio, sr, voice, **kwargs)
                assert np.array_equal(actual, fx.to_48k(fx.normalize_loudness(audio, sr), sr))
                seen.append(kwargs)
                return actual

            rig.service._fx = process
            rig.service._hotmic = lambda *a, **kw: pytest.fail("native hotmic added a scene")
            options = {"kind": kind}
            if kind == "stock":
                options = {"kind": "open", "text": "Ready."}
            elif kind == "whisper":
                options.update(channel="whisper", priority="p3")
            await rig.mind.send(rig.speak("native", **options))
            await rig.sees("native", "started")
            assert seen and seen[0]["whisper"] is (kind == "whisper")
            await rig.play_out()
            await rig.sees("native", "done")
            assert {p: p.read_bytes() for p in rig.data.rglob("*.wav")} == before
            if kind == "stock":
                assert len(rig.engine.said) == said_before  # existing dry stock still used
    run(exercise())


@pytest.mark.parametrize("effects_enabled", [False, True])
def test_saved_member_selftest_honors_effects_config(tmp_path, fake_bank, fake_engine,
                                                  monkeypatch, effects_enabled):
    folder = tmp_path / "specs"
    folder.mkdir()
    cast.write_spec(folder, spec())
    monkeypatch.setattr(cli, "load_engine", lambda *a: (fake_engine, fake_bank))
    seen = []
    original = fx.process

    def process(audio, sr, voice, **kwargs):
        seen.append(kwargs["effects_enabled"])
        return original(audio, sr, voice, **kwargs)

    monkeypatch.setattr(fx, "process", process)
    cfg = replace(load_config(tmp_path / "absent.toml"), effects_enabled=effects_enabled)
    clip, path, who = cli._render_member(tmp_path, cfg, 101, folder, cpu=True)
    assert seen == [effects_enabled]
    assert who == "kokoro" and path.is_file() and np.isfinite(clip).all()


@pytest.mark.parametrize("speed", [0.9, 1.0, 1.15])
def test_native_expressive_keeps_exact_worker_samples_and_model_prosody(tmp_path, monkeypatch,
                                                                     speed):
    made = ExpressiveEngine("chatterbox-turbo", tmp_path / "python", need_gb=3)
    dry, calls = signal(24000), []

    class Worker:
        up = True

        def ask(self, text, ref, params, *, timeout):
            calls.append((text, params, timeout))
            return dry, 24000

    made.worker = Worker()
    monkeypatch.setattr(made, "_reference", lambda voice: tmp_path / "reference.wav")
    monkeypatch.setattr(expressive, "change_tempo",
                        lambda *a, **kw: pytest.fail("native synthesis time-stretched audio"))
    params = {"temperature": 0.7}
    result = made.synth("Um, [chuckle] let me think.", spec(), params, speed)
    assert result is dry
    assert calls == [("Um, [chuckle] let me think.", params, expressive.LINE_TIMEOUT_S)]
    assert made.render_policy == "native-v1"


@pytest.mark.parametrize("bad", [np.array([np.nan], np.float32), np.ones((2, 12), np.float32)])
def test_native_expressive_still_rejects_invalid_worker_audio(tmp_path, monkeypatch, bad):
    made = ExpressiveEngine("chatterbox-turbo", tmp_path / "python", need_gb=3)

    class Worker:
        up = True

        def ask(self, *a, **kw):
            return bad, 24000

    made.worker = Worker()
    monkeypatch.setattr(made, "_reference", lambda voice: tmp_path / "reference.wav")
    with pytest.raises(EngineError, match="invalid audio"):
        made.synth("Hello.", spec(), None, 1.0)


@pytest.mark.parametrize("device,live_cpu", [("cuda", False), ("cpu", False), ("cpu", True)])
@pytest.mark.parametrize("effects_enabled", [False, True])
def test_every_expressive_loader_applies_native_policy(tmp_path, monkeypatch, device, live_cpu,
                                                      effects_enabled):
    python = tmp_path / "engines/chatterbox-turbo/.venv/Scripts/python.exe"
    python.parent.mkdir(parents=True)
    python.touch()
    monkeypatch.setattr(cli, "_require", lambda *a: None)
    cfg = replace(load_config(tmp_path / "absent.toml"), effects_enabled=effects_enabled)
    made = cli.load_expressive(tmp_path, cfg, Decision(engine="chatterbox-turbo"),
                               device=device, refs_dir=tmp_path, live_cpu=live_cpu)
    assert made.render_policy == (None if effects_enabled else "native-v1")


def test_native_cache_rejects_old_tempo_audio_and_preserves_original_assets(tmp_path, fake_bank,
                                                                        monkeypatch):
    class PreNativeEngine(FakeEngine):
        name = "chatterbox-turbo"

        def reference_fingerprint(self, voice):
            return "synthetic-reference-hash"

    root = tmp_path / "stock"
    voice = spec(engine="chatterbox-turbo", ref="101.ref.wav")
    decision = Decision(engine="chatterbox-turbo")
    old = StockCache(root, PreNativeEngine(), fake_bank, decision)
    assert old.render_missing(voice) == len(STOCK)
    originals = {p: p.read_bytes() for p in root.rglob("*.wav")}
    native_engine = ExpressiveEngine("chatterbox-turbo", tmp_path / "python", need_gb=3)
    legacy_engine = ExpressiveEngine("chatterbox-turbo", tmp_path / "python", need_gb=3,
                                     effects_enabled=True)
    for made in (native_engine, legacy_engine):
        monkeypatch.setattr(made, "reference_fingerprint", old.engine.reference_fingerprint)

    class Worker:
        up = True

        def ask(self, *a, **kw):
            return signal(24000), 24000

    native_engine.worker = Worker()
    monkeypatch.setattr(native_engine, "_reference", lambda voice: tmp_path / "reference.wav")
    native = StockCache(root, native_engine, fake_bank, decision)
    legacy = StockCache(root, legacy_engine, fake_bank, decision)
    assert native.root == root / "native-v1" and legacy.root == root
    assert native.voice_hash(voice) != old.voice_hash(voice) == legacy.voice_hash(voice)
    assert native.get(voice, "ready") is None
    assert np.array_equal(legacy.get(voice, "ready"), old.get(voice, "ready"))
    assert native.drop_stale(voice) == 0
    assert native.render_missing(voice) == len(STOCK)
    assert np.array_equal(native.get(voice, "ready"), signal(24000))
    assert native.render_missing(voice) == 0
    assert native.drop_stale(replace(voice, speed=1.1)) == 1  # only its own namespace
    assert all(p.read_bytes() == original for p, original in originals.items())
