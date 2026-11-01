"""Task 21: Whisper turbo behind the speech gate. The model is a stand-in; the `gpu` test loads
the real one, and skips unless admit() passes at that moment."""
import os
import sys
from types import SimpleNamespace

import numpy as np
import pytest

from leeroy_voice.stt import SAMPLE_RATE, Transcript
from leeroy_voice.stt import whisper as w
from test_parakeet import corrector, loopback  # noqa: F401 (fixtures)
from leeroy_voice.stt.whisper import HOTWORD_TOKENS, WOW_WORDS, WhisperRecognizer, build_hotwords


class Gate:
    def __init__(self, cut):
        self.cut = cut
        self.heard = []

    def speech(self, audio):
        self.heard.append(audio)
        return self.cut


class Model:
    def __init__(self, segments):
        self.segments = segments
        self.calls = []

    def transcribe(self, audio, **kw):
        self.calls.append((audio, kw))
        return iter(self.segments), SimpleNamespace(language="en")


def seg(text, avg_logprob=-0.1, no_speech_prob=0.01):
    return SimpleNamespace(text=text, avg_logprob=avg_logprob, no_speech_prob=no_speech_prob)


@pytest.fixture
def made(monkeypatch, tmp_path):
    """WhisperRecognizer over a stand-in model; `made.args` is what the loader was given."""
    box = SimpleNamespace(model=Model([seg(" Chorus, you're on adds. ")]), args=None)

    def load(model_dir, compute_type):
        box.args = (model_dir, compute_type)
        return box.model
    monkeypatch.setattr(w, "_load_whisper", load)
    (tmp_path / "whisper").mkdir()
    box.dir = tmp_path / "whisper"
    return box


def audio():
    return np.zeros(SAMPLE_RATE, np.float32)


def test_transcribes_the_gated_speech_with_the_pinned_arguments(made):
    cut = np.ones(8000, np.float32)
    gate = Gate((cut, 900))
    rec = WhisperRecognizer(made.dir, gate)
    assert made.args == (made.dir, "int8") and rec.name == "whisper"
    got = rec.transcribe(audio(), ["Arkon", "Belra"])
    assert isinstance(got, Transcript)
    assert got.text == "Chorus, you're on adds." and got.speech_ms == 900
    assert 0.89 < got.confidence < 0.91  # exp(-0.1)
    (heard, kw), = made.model.calls
    assert heard is cut
    assert kw["language"] == "en" and kw["beam_size"] == 1
    assert kw["condition_on_previous_text"] is False and kw["vad_filter"] is False
    assert kw["hotwords"].startswith("Arkon Belra")


def test_no_speech_never_reaches_the_model(made):
    rec = WhisperRecognizer(made.dir, Gate(None))
    assert rec.transcribe(audio(), []) is None
    assert made.model.calls == []


def test_an_empty_transcript_has_zero_confidence(made):
    made.model.segments = [seg("  ")]
    got = WhisperRecognizer(made.dir, Gate((audio(), 500))).transcribe(audio(), [])
    assert got == Transcript("", 0.0, 500)


def test_confidence_is_the_mean_over_segments_and_clipped(made):
    made.model.segments = [seg("a", -0.1), seg("b", -0.3), seg("c", 0.5)]  # 0.5 is junk, clipped
    got = WhisperRecognizer(made.dir, Gate((audio(), 500))).transcribe(audio(), [])
    assert got.text == "a b c"
    expected = (np.exp(-0.1) + np.exp(-0.3) + 1.0) / 3
    assert abs(got.confidence - expected) < 1e-6


def test_a_missing_model_folder(tmp_path):
    with pytest.raises(FileNotFoundError):
        WhisperRecognizer(tmp_path / "none", Gate(None))


def test_close_lets_go_of_the_model(made):
    rec = WhisperRecognizer(made.dir, Gate(None))
    rec.close()
    rec.close()  # twice is fine
    with pytest.raises(RuntimeError):
        rec.transcribe(audio(), [])


# -- the hotwords -------------------------------------------------------------------------------

def count(text: str) -> int:
    """A stand-in tokenizer: three tokens a word."""
    return 3 * len(text.split())


def test_hotwords():
    out = build_hotwords(["Arkon", "Belra"], ["Dunna", "Arkon", "Cess"], WOW_WORDS, count)
    words = out.split()
    assert words[:5] == ["Arkon", "Belra", "Dunna", "Cess", WOW_WORDS[0]]  # no repeat of Arkon
    assert count(out) <= HOTWORD_TOKENS == 223


def test_hotwords_cut_at_the_token_limit_keeping_the_names():
    names = [f"Name{i}" for i in range(40)]
    out = build_hotwords(names, [f"Old{i}" for i in range(10)], WOW_WORDS, count)
    assert count(out) <= 223
    assert out.split() == (names + [f"Old{i}" for i in range(10)] + list(WOW_WORDS))[:len(
        out.split())]
    assert out.split()[:40] == names  # the room's names are never cut for the rest
    assert len(out.split()) == 223 // 3


def test_hotwords_are_empty_with_nothing_to_say():
    assert build_hotwords([], [], (), count) == ""


def test_the_recognizer_passes_the_callers_names_then_wow_words(made):
    rec = WhisperRecognizer(made.dir, Gate((audio(), 500)))
    rec.transcribe(audio(), ["Arkon", "Dunna"])
    hot = made.model.calls[0][1]["hotwords"].split()
    assert hot[:2] == ["Arkon", "Dunna"] and hot[2] == WOW_WORDS[0]


# -- the CUDA libraries -------------------------------------------------------------------------

@pytest.mark.skipif(sys.platform != "win32", reason="Windows' DLL search")
def test_the_cublas_folder_is_added_once(monkeypatch, tmp_path, made):
    folder = tmp_path / "site" / "nvidia" / "cublas" / "bin"
    folder.mkdir(parents=True)
    added = []
    monkeypatch.setattr(w, "_cublas_dir", lambda: folder)
    monkeypatch.setattr(os, "add_dll_directory", lambda p: added.append(p) or object())
    monkeypatch.setattr(w, "_dll_handles", [])
    WhisperRecognizer(made.dir, Gate(None))
    WhisperRecognizer(made.dir, Gate(None))
    assert added == [str(folder)]


def test_no_cublas_folder_is_not_an_error(monkeypatch, made):
    monkeypatch.setattr(w, "_cublas_dir", lambda: None)
    monkeypatch.setattr(w, "_dll_handles", [])
    WhisperRecognizer(made.dir, Gate(None))


# -- the real model -----------------------------------------------------------------------------

@pytest.mark.gpu
@pytest.mark.ollama
def test_whisper_on_the_4090(loopback, corrector):
    """Task 9's loopback line (Belra, adds) through the real model. Skips, never passes, unless
    admit() passes now: Orion and the embedding model are up and fully on the GPU, WoW runs and
    2.5 GB are free. The load and a press cost at most 2.0 GB of VRAM."""
    from conftest import REAL_VOICE_DATA, SESSION_VOICE_DATA
    from pathlib import Path
    from leeroy_voice import gpu, models
    from leeroy_voice.stt.vad import SpeechGate
    pytest.importorskip("faster_whisper")
    loaded = gpu.OllamaPs().loaded()
    if not loaded:
        pytest.skip("Ollama has nothing loaded")
    names = sorted(loaded, key=lambda n: -loaded[n][0])
    embed = next((n for n in names if "embed" in n), None)
    if embed is None:
        pytest.skip("the embedding model is not loaded")
    reason = gpu.admit(ps=loaded, free_gb=gpu.free_vram_gb(), wow=gpu.wow_running(),
                       live=names[0], embed=embed, need_gb=1.5, margin_gb=1.0)
    if reason is not None:
        pytest.skip(f"admit() refuses: {reason}")
    root = Path(SESSION_VOICE_DATA) if SESSION_VOICE_DATA else REAL_VOICE_DATA
    for name in ("whisper", "silero"):
        if not all(ok is True for ok, _ in models.check(root, name)):
            pytest.skip(f"{name} is not fetched (fetch-models --only {name})")
    before = gpu.free_vram_gb()
    rec = WhisperRecognizer(models.model_dir(root, "whisper"),
                            SpeechGate(models.model_dir(root, "silero")))
    try:
        got = rec.transcribe(loopback["af_heart"], ["Belra"])
        used = before - gpu.free_vram_gb()
    finally:
        rec.close()
    assert got is not None and "Belra" in corrector.correct(got.text), got
    assert used <= 2.0, f"{used:.2f} GB"
