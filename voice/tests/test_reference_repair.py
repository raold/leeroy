"""Reference repair and stock identity, on synthetic audio in temporary directories only."""
import dataclasses
from contextlib import nullcontext
import json
import os
import sys
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

from conftest import BANK_SHA
from leeroy_voice import cast
from leeroy_voice.decision import Decision
from leeroy_voice.engines import EngineError
from leeroy_voice.engines.expressive import ExpressiveEngine, validate_reference
from leeroy_voice.stock import STOCK, StockCache
from leeroy_voice.voices import spec_from_dict

RATE = 24000
TURBO = "chatterbox-turbo"
DECISION = Decision(engine=TURBO, voice_source="qwen3-voicedesign", vram_trade="slots")
ENTRY = cast.BriefEntry(101, "Arkon", "male", "en-us", 30, ("warm",), 1.0,
                       "member", "damage", False)


def tone(hz=180.0, seconds=6.0):
    return (0.2 * np.sin(2 * np.pi * hz * np.arange(round(RATE * seconds)) / RATE)).astype(np.float32)


def spec():
    return spec_from_dict({"version": 1, "guid": 101, "name": "Arkon", "engine": TURBO,
                           "lang": "a", "timbre": {"am_michael": 1.0}, "prosody": {},
                           "speed": 1.0, "pitch_st": 0.0, "mic": "desk_mic", "gain_db": 0.0,
                           "locked": False, "voices_sha256": BANK_SHA, "ref": "101.ref.wav"})


class Designer:
    def __init__(self, audio=None):
        self.audio = tone(260) if audio is None else audio
        self.calls = 0
        self.closed = False

    def clip(self, entry, voice):
        self.calls += 1
        return self.audio, RATE

    def close(self):
        self.closed = True


class ReferenceEngine(ExpressiveEngine):
    """Keep the real reference lookup/fingerprint; stock synthesis returns a reference excerpt."""
    def __init__(self, folder):
        super().__init__(TURBO, sys.executable, need_gb=0, refs_dir=folder)
        self.calls = 0

    def synth(self, text, voice, style, speed):
        self.calls += 1
        audio, rate = sf.read(self._reference(voice), dtype="float32")
        return validate_reference(audio, rate)[:RATE // 5]


@pytest.fixture
def setup(tmp_path, fake_bank):
    folder = tmp_path / "voices"
    voice = spec()
    cast.write_spec(folder, voice)
    path = folder / voice.ref
    cast.write_reference(path, tone(), RATE)
    engine = ReferenceEngine(folder)
    cache = StockCache(tmp_path / "stock", engine, fake_bank, DECISION)
    return folder, voice, path, engine, cache


@pytest.mark.parametrize("audio,rate", [
    (np.full(RATE * 6, np.nan), RATE),
    (np.full(RATE * 6, np.inf), RATE),
    (np.zeros(RATE * 6), RATE),
    (np.full(RATE * 6, 1e-9), RATE),
    (np.zeros((RATE * 6, 2)), RATE),
    (tone(), 0),
    (tone(), -1),
    (tone(seconds=5.0), RATE),
])
def test_invalid_generated_audio_never_replaces_a_reference(setup, audio, rate):
    folder, voice, path, engine, cache = setup
    before = path.read_bytes()
    with pytest.raises(EngineError):
        cast.write_reference(path, audio, rate)
    assert path.read_bytes() == before
    assert {p.name for p in folder.iterdir()} == {"101.json", "101.ref.wav"}


def test_reference_replacement_invalidates_stock_even_when_metadata_matches(setup):
    folder, voice, path, engine, cache = setup
    assert cache.render_missing(voice) == len(STOCK)
    old_audio = cache.get(voice, "oom")
    old_hash = cache.voice_hash(voice)
    original_stat = path.stat()
    cast.write_reference(path, tone(260), RATE)
    os.utime(path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    assert path.stat().st_size == original_stat.st_size
    assert path.stat().st_mtime_ns == original_stat.st_mtime_ns
    assert cache.voice_hash(voice) != old_hash
    assert cache.get(voice, "oom") is None
    assert cache.drop_stale(voice) == 1
    assert cache.render_missing(voice) == len(STOCK)
    assert not np.array_equal(cache.get(voice, "oom"), old_audio)
    assert cache.render_missing(voice) == 0


@pytest.mark.parametrize("damage", ["missing", "corrupt", "silent", "nonfinite", "stereo", "short"])
def test_invalid_reference_is_a_stock_miss_and_skips_rendering(setup, damage):
    folder, voice, path, engine, cache = setup
    assert cache.render_missing(voice) == len(STOCK)
    old_folder = cache.root / str(voice.guid) / cache.voice_hash(voice)
    if damage == "missing":
        path.unlink()
    elif damage == "corrupt":
        path.write_bytes(b"not a WAV")
    else:
        audio = {"silent": np.zeros(RATE * 6), "nonfinite": np.full(RATE * 6, np.nan),
                 "stereo": np.column_stack((tone(), tone())), "short": tone(seconds=4)}[damage]
        sf.write(path, audio, RATE, subtype="FLOAT")
    assert cache.get(voice, "oom") is None
    assert cache.render_missing(voice) == 0
    assert engine.calls == len(STOCK)
    assert cache.drop_stale(voice) == 0
    assert old_folder.is_dir()  # the invalid reference does not destroy the last valid cache


def test_resume_repairs_a_corrupt_reference_with_an_unchanged_spec(setup):
    folder, voice, path, engine, cache = setup
    cache.render_missing(voice)
    old_audio = cache.get(voice, "oom")
    path.write_bytes(b"interrupted reference")
    source = Designer()
    report = cast.render_designed_references([ENTRY], source, folder, decision=DECISION,
                                             stock=lambda: cache)
    assert report["written"] == [101] and not report["kept"] and not report["failed"]
    assert source.calls == 1 and source.closed
    assert cast.load_existing(folder)[101] == voice
    assert not np.array_equal(cache.get(voice, "oom"), old_audio)


def test_resume_after_reference_repair_finishes_stock_without_redesigning(setup):
    folder, voice, path, engine, cache = setup
    cache.render_missing(voice)
    old_folder = cache.root / str(voice.guid) / cache.voice_hash(voice)
    old_audio = cache.get(voice, "oom")
    path.write_bytes(b"damaged reference")
    source = Designer()

    def interrupted():
        raise RuntimeError("interrupted before stock")

    with pytest.raises(RuntimeError, match="interrupted"):
        cast.render_designed_references([ENTRY], source, folder, decision=DECISION, stock=interrupted)
    assert source.calls == 1 and source.closed and old_folder.is_dir()
    assert cache.get(voice, "oom") is None
    resumed = Designer(np.zeros(RATE * 6))  # would fail if resume tried to design again
    report = cast.render_designed_references([ENTRY], resumed, folder, decision=DECISION,
                                             stock=lambda: cache)
    assert report["kept"] == [101] and not report["written"] and not report["failed"]
    assert resumed.calls == 0 and resumed.closed and not old_folder.exists()
    assert not np.array_equal(cache.get(voice, "oom"), old_audio)


def test_failed_repair_preserves_existing_files_and_skips_stock(setup):
    folder, voice, path, engine, cache = setup
    path.write_bytes(b"damaged reference")
    before = {p.name: p.read_bytes() for p in folder.iterdir()}
    source = Designer(np.zeros(RATE * 6))

    def no_stock():
        pytest.fail("failed reference repair must not load a stock engine")

    report = cast.render_designed_references([ENTRY], source, folder, decision=DECISION, stock=no_stock)
    assert report["failed"] == [101] and not report["written"] and not report["kept"]
    assert source.closed
    assert {p.name: p.read_bytes() for p in folder.iterdir()} == before


def test_locked_invalid_reference_is_left_untouched(setup):
    folder, voice, path, engine, cache = setup
    cast.write_spec(folder, dataclasses.replace(voice, locked=True))
    path.write_bytes(b"damaged reference")
    before = {p.name: p.read_bytes() for p in folder.iterdir()}
    source = Designer()
    report = cast.render_designed_references([ENTRY], source, folder, decision=DECISION,
                                             stock=lambda: pytest.fail("locked voice must not render"))
    assert report["locked"] == [101] and source.calls == 0 and source.closed
    assert {p.name: p.read_bytes() for p in folder.iterdir()} == before


def test_turbo_reloads_conditioning_when_reference_contents_change(setup, tmp_path, monkeypatch):
    """No model needed: exercise the real worker's cache with a stand-in TTS implementation."""
    from leeroy_voice.engines import worker_chatterbox_turbo as turbo

    folder, voice, path, engine, cache = setup
    model = tmp_path / "model"
    model.mkdir()
    (model / "added_tokens.json").write_text(json.dumps({"[chuckle]": 1}), encoding="utf-8")

    class Wav:
        def __init__(self, audio):
            self.audio = audio

        def squeeze(self, axis):
            return self

        def cpu(self):
            return self

        def numpy(self):
            return self.audio

    class TTS:
        sr = RATE
        loads = 0

        def prepare_conditionals(self, ref):
            self.loads += 1
            self.conds = sf.read(ref, dtype="float32")[0][:RATE // 5]

        def generate(self, text, **kwargs):
            return Wav(self.conds)

    tts = TTS()
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(set_num_threads=lambda _: None,
                                                             inference_mode=nullcontext))
    monkeypatch.setitem(sys.modules, "chatterbox", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "chatterbox.tts_turbo", SimpleNamespace(
        ChatterboxTurboTTS=SimpleNamespace(from_local=lambda *args: tts)))
    speak, rate = turbo.load(model, "cpu", 1)
    before = speak("Pulling.", str(path), {})
    assert np.array_equal(speak("Ready.", str(path), {}), before) and tts.loads == 1
    previous = path.stat()
    cast.write_reference(path, tone(260), RATE)
    os.utime(path, ns=(previous.st_atime_ns, previous.st_mtime_ns))
    assert path.stat().st_size == previous.st_size
    assert path.stat().st_mtime_ns == previous.st_mtime_ns
    after = speak("Pulling.", str(path), {})
    assert rate == RATE and tts.loads == 2 and not np.array_equal(after, before)
