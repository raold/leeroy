"""Recognition: the Silero speech gate (stt/vad.py) and Parakeet behind it (stt/parakeet.py).

On the fetched models, on the CPU: marked `engines` (deselected by default; the `parakeet` and
`kokoro` fixtures skip when the files are not fetched). Without a model, in the default run:
how the gate cuts and measures what Silero found, how the recognizer turns onnx-asr's result
into a Transcript, how both are wired into onnx-asr (skipped where it is not installed, as in
CI), and FakeRecognizer, Task 11's stand-in."""
import math
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pytest
import soxr

from fakes import FakeRecognizer
from leeroy_voice import wire
from leeroy_voice.stt import MIN_SPEECH_MS, SAMPLE_RATE, Transcript
from leeroy_voice.stt import parakeet as parakeet_mod
from leeroy_voice.stt import vad as vad_mod
from leeroy_voice.stt.names import RosterCorrector, common_words
from leeroy_voice.stt.parakeet import MODEL, ParakeetRecognizer
from leeroy_voice.stt.phantoms import is_phantom
from leeroy_voice.stt.vad import PAD_MS, SpeechGate

MS = SAMPLE_RATE // 1000  # samples per millisecond at 16 kHz


class FakeVad:
    """onnx-asr's Silero VAD as the gate uses it: segment_batch() hands back the segments it
    was given, in samples, and records each call."""

    def __init__(self, segments=()):
        self.segments = list(segments)
        self.calls: list = []

    def segment_batch(self, waveforms, waveforms_len, sample_rate, **kw):
        self.calls.append((waveforms.copy(), waveforms_len.copy(), sample_rate, kw))
        yield iter(self.segments)


@pytest.fixture
def make_gate(tmp_path, monkeypatch):
    """A SpeechGate on a FakeVad: make_gate(segments, **settings) -> (gate, vad)."""
    (tmp_path / "silero").mkdir()

    def make(segments=(), **kw):
        vad = FakeVad(segments)
        monkeypatch.setattr(vad_mod, "_load_silero", lambda d: vad)
        return SpeechGate(tmp_path / "silero", **kw), vad
    return make


def ramp(n: int) -> np.ndarray:
    """Audio whose every sample names its own place, so a cut shows what it kept."""
    return (np.arange(n, dtype=np.float32) / n).astype(np.float32)


# -- the gate ---------------------------------------------------------------------------------

def test_the_gate_cuts_to_the_speech_padded(make_gate):
    gate, _ = make_gate([(16000, 24000), (40000, 48000)])         # 1.0-1.5 s and 2.5-3.0 s
    x = ramp(64000)
    audio, speech_ms = gate.speech(x)
    pad = PAD_MS * MS
    assert PAD_MS == 100 and speech_ms == 1000                   # the segments, unpadded
    assert np.array_equal(audio, np.concatenate([x[16000 - pad:24000 + pad],
                                                 x[40000 - pad:48000 + pad]]))
    assert audio.dtype == np.float32 and type(speech_ms) is int


def test_padded_segments_that_touch_are_joined(make_gate):
    gate, _ = make_gate([(16000, 24000), (27000, 30000)])         # 187 ms apart: under 2 pads
    x = ramp(48000)
    audio, speech_ms = gate.speech(x)
    assert np.array_equal(audio, x[16000 - 1600:30000 + 1600])   # nothing twice
    assert speech_ms == 687


def test_the_pads_stop_at_the_edges(make_gate):
    gate, _ = make_gate([(0, 8000), (40000, 48000)])
    x = ramp(48000)
    audio, _ = gate.speech(x)
    assert np.array_equal(audio, np.concatenate([x[:9600], x[38400:]]))


def test_its_own_pad(make_gate):
    gate, _ = make_gate([(16000, 24000)], pad_ms=0)
    x = ramp(32000)
    assert np.array_equal(gate.speech(x)[0], x[16000:24000])


def test_under_the_minimum_is_no_speech(make_gate):
    """The total counts: two short segments that make 250 ms between them are speech."""
    assert MIN_SPEECH_MS == 250
    x = ramp(32000)
    assert make_gate([(1000, 1000 + 249 * MS)])[0].speech(x) is None
    assert make_gate([(1000, 1000 + 250 * MS)])[0].speech(x)[1] == 250
    assert make_gate([(1000, 1000 + 125 * MS), (20000, 20000 + 125 * MS)])[0].speech(x)[1] == 250
    assert make_gate([])[0].speech(x) is None
    assert make_gate([], min_speech_ms=0)[0].speech(x) is None   # no minimum, still no speech
    gate, _ = make_gate([(1000, 1000 + 400 * MS)], min_speech_ms=500)
    assert gate.speech(x) is None


def test_the_gate_asks_silero_for_bare_segments(make_gate):
    gate, vad = make_gate([(0, 16000)])
    x = ramp(32000).astype(np.float64)
    gate.speech(x)
    [(waveforms, lens, rate, kw)] = vad.calls
    assert waveforms.shape == (1, 32000) and waveforms.dtype == np.float32
    assert lens.tolist() == [32000] and lens.dtype == np.int64 and rate == SAMPLE_RATE
    assert kw == {"speech_pad_ms": 0, "min_speech_duration_ms": 0}


def test_a_press_too_short_to_hold_speech_never_reaches_silero(make_gate):
    gate, vad = make_gate([(0, 4000)])
    assert gate.speech(np.zeros(249 * MS, np.float32)) is None   # shorter than the minimum
    assert gate.speech(np.zeros(0, np.float32)) is None
    gate0, vad0 = make_gate([(0, 300)], min_speech_ms=0)
    assert gate0.speech(np.zeros(575, np.float32)) is None       # shorter than Silero's window
    assert vad.calls == [] and vad0.calls == []
    assert gate.speech(np.zeros(250 * MS, np.float32))[1] == 250  # long enough: Silero asked
    assert gate0.speech(np.zeros(576, np.float32))[1] == 18
    assert len(vad.calls) == len(vad0.calls) == 1


@pytest.mark.parametrize("bad", [np.zeros((2, 8000), np.float32),
                                 np.array([0.0, math.nan] * 8000, np.float32),
                                 np.array([0.0, math.inf] * 8000, np.float32),
                                 np.array(["a"] * 16000), object(), [object()] * 16000])
def test_bad_audio_is_refused(make_gate, bad):
    gate, vad = make_gate([(0, 8000)])
    with pytest.raises(ValueError):
        gate.speech(bad)
    assert vad.calls == []


@pytest.mark.parametrize("kw", [{"min_speech_ms": -1}, {"min_speech_ms": 250.0},
                                {"min_speech_ms": True}, {"pad_ms": -1}, {"pad_ms": "100"}])
def test_bad_gate_settings_are_refused(make_gate, kw):
    with pytest.raises(ValueError):
        make_gate(**kw)


def test_a_missing_model_folder_is_refused_before_onnx_asr(tmp_path, monkeypatch):
    """onnx-asr downloads (unpinned) when the folder it is given does not exist (I4): a
    missing folder never reaches it."""
    def never(*a):
        raise AssertionError("onnx-asr reached")
    monkeypatch.setattr(vad_mod, "_load_silero", never)
    monkeypatch.setattr(parakeet_mod, "_load_parakeet", never)
    with pytest.raises(FileNotFoundError, match="fetch-models --only silero"):
        SpeechGate(tmp_path / "absent")
    with pytest.raises(FileNotFoundError, match="fetch-models --only parakeet"):
        ParakeetRecognizer(tmp_path / "absent", object(), threads=4)
    (tmp_path / "a-file").write_text("")
    with pytest.raises(FileNotFoundError):
        SpeechGate(tmp_path / "a-file")


# -- the recognizer ---------------------------------------------------------------------------

class FakeResult:
    """onnx-asr's TimestampedResult, as far as the recognizer reads it."""

    def __init__(self, text, logprobs):
        self.text = text
        self.logprobs = logprobs


class FakeAsr:
    """onnx-asr's with_timestamps() adapter: recognize() hands back `result`, recorded."""

    def __init__(self, result):
        self.result = result
        self.calls: list = []

    def recognize(self, waveform, **kw):
        self.calls.append((waveform, kw))
        return self.result


class FakeGate:
    def __init__(self, out):
        self.out = out
        self.calls: list = []

    def speech(self, audio16k):
        self.calls.append(audio16k)
        return self.out


@pytest.fixture
def make_parakeet(tmp_path, monkeypatch):
    """A ParakeetRecognizer on a FakeAsr and a FakeGate: make_parakeet(result, gate_out)."""
    (tmp_path / "parakeet").mkdir()

    def make(result, gate_out):
        asr = FakeAsr(result)
        monkeypatch.setattr(parakeet_mod, "_load_parakeet", lambda d, threads: asr)
        return ParakeetRecognizer(tmp_path / "parakeet", FakeGate(gate_out), threads=4), asr
    return make


def test_transcribe_gates_then_recognizes(make_parakeet):
    cut = ramp(8000)
    p, asr = make_parakeet(FakeResult("Belra, pull.", [math.log(.9), math.log(.5)]), (cut, 1234))
    press = ramp(16000)
    t = p.transcribe(press, ["Belra"])
    assert p.name == "parakeet" and p.gate.calls == [press]
    [(waveform, kw)] = asr.calls
    assert waveform is cut and kw == {"sample_rate": SAMPLE_RATE}
    assert (t.text, t.speech_ms) == ("Belra, pull.", 1234)
    assert t.confidence == pytest.approx(0.7)                     # the mean of .9 and .5


def test_no_speech_never_reaches_parakeet(make_parakeet):
    p, asr = make_parakeet(FakeResult("Thank you.", [0.0]), None)
    assert p.transcribe(ramp(16000), []) is None
    assert asr.calls == []
    p.gate.out = (ramp(8000), 600)
    assert p.transcribe(ramp(16000), []) == Transcript("Thank you.", 1.0, 600)
    assert len(asr.calls) == 1


def test_hotwords_are_ignored(make_parakeet):
    p, _ = make_parakeet(FakeResult("pull", [math.log(.8)]), (ramp(8000), 500))
    assert p.transcribe(ramp(16000), []) == p.transcribe(ramp(16000), ["Belra", "Kel'Thuzad"])
    assert p.transcribe(ramp(16000), ["Belra"]).text == "pull"


def test_no_words_is_an_empty_transcript(make_parakeet):
    p, _ = make_parakeet(FakeResult("", []), (ramp(8000), 400))
    assert p.transcribe(ramp(16000), []) == Transcript("", 0.0, 400)
    p, _ = make_parakeet(FakeResult(" ", [math.log(.9)]), (ramp(8000), 400))   # a lone space
    assert p.transcribe(ramp(16000), []) == Transcript("", 0.0, 400)


def test_text_without_log_probabilities_is_an_error(make_parakeet):
    """A result with words but no log-probabilities would make every line a phantom without a
    word: it fails loudly instead."""
    p, _ = make_parakeet(FakeResult("pull", None), (ramp(8000), 400))
    with pytest.raises(RuntimeError, match="log-probabilities"):
        p.transcribe(ramp(16000), [])


def test_the_transcript_is_plain_python_for_the_wire(make_parakeet):
    logprobs = np.log(np.array([.9, .8, .99], np.float32))
    p, _ = make_parakeet(FakeResult("  Belra,   pull  now. ", logprobs),
                         (ramp(8000), np.int64(900)))
    t = p.transcribe(ramp(16000), [])
    assert t.text == "Belra, pull now."                          # whitespace folded
    assert type(t.confidence) is float and type(t.speech_ms) is int
    assert 0 < t.confidence <= 1
    wire.encode(wire.Heard(t.text, t.text, t.confidence, "guild_hall", t.speech_ms, 420), 1)


@pytest.mark.parametrize("threads", [0, 65, 4.0, "4", True])
def test_bad_thread_counts_are_refused(make_parakeet, threads, tmp_path):
    make_parakeet(FakeResult("", []), None)
    with pytest.raises(ValueError):
        ParakeetRecognizer(tmp_path / "parakeet", FakeGate(None), threads=threads)


# -- the wiring into onnx-asr (where it is installed) -----------------------------------------

def test_the_gate_loads_silero_on_one_cpu_thread(tmp_path, monkeypatch):
    onnx_asr = pytest.importorskip("onnx_asr")
    calls = []
    monkeypatch.setattr(onnx_asr, "load_vad", lambda *a, **kw: calls.append((a, kw)) or FakeVad())
    (tmp_path / "silero").mkdir()
    SpeechGate(tmp_path / "silero")
    [(args, kw)] = calls
    assert args == ("silero", tmp_path / "silero")
    assert kw["providers"] == ["CPUExecutionProvider"]
    assert kw["sess_options"].intra_op_num_threads == 1
    assert kw["sess_options"].inter_op_num_threads == 1
    assert set(kw) == {"providers", "sess_options"}


def test_parakeet_loads_int8_on_the_cpu_with_its_threads(tmp_path, monkeypatch):
    onnx_asr = pytest.importorskip("onnx_asr")
    calls, adapter = [], FakeAsr(FakeResult("", []))

    class Loaded:
        def with_timestamps(self):
            calls.append("with_timestamps")
            return adapter
    monkeypatch.setattr(onnx_asr, "load_model",
                        lambda *a, **kw: calls.append((a, kw)) or Loaded())
    (tmp_path / "parakeet").mkdir()
    p = ParakeetRecognizer(tmp_path / "parakeet", FakeGate(None), threads=3)
    [(args, kw), step] = calls
    assert MODEL == "nemo-parakeet-tdt-0.6b-v2"
    assert args == (MODEL, tmp_path / "parakeet") and step == "with_timestamps"
    assert kw["quantization"] == "int8" and kw["providers"] == ["CPUExecutionProvider"]
    assert kw["sess_options"].intra_op_num_threads == 3
    assert set(kw) == {"quantization", "providers", "sess_options"}
    assert p._asr is adapter and p.threads == 3


def test_recognition_imports_nothing_heavy_until_it_loads():
    """M9: the stt modules import without onnx-asr, onnxruntime, huggingface_hub or misaki (a
    fresh interpreter, so no other test's imports count)."""
    code = ("import sys\n"
            "import leeroy_voice.stt, leeroy_voice.stt.vad, leeroy_voice.stt.parakeet\n"
            "import leeroy_voice.stt.names, leeroy_voice.stt.phantoms\n"
            "print(sorted(m for m in ('onnx_asr', 'onnxruntime', 'huggingface_hub', 'misaki')"
            " if m in sys.modules))\n")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "[]"


def test_the_stt_extra_pins_onnxruntime():
    """faster-whisper in the same extra takes onnxruntime unpinned (>=1.14,<2): the extra pins
    it, at the tts extra's version (review M3)."""
    import tomllib
    from leeroy_voice import config
    project = tomllib.loads((Path(config.__file__).parents[1] / "pyproject.toml")
                            .read_text(encoding="utf-8"))["project"]["optional-dependencies"]
    pins = {extra: [p for p in project[extra] if p.startswith("onnxruntime")]
            for extra in ("stt", "tts")}
    assert pins == {"stt": ["onnxruntime==1.31.1"], "tts": ["onnxruntime==1.31.1"]}


# -- FakeRecognizer (Task 11's) ---------------------------------------------------------------

def test_fake_recognizer():
    heard = Transcript("pull", .9, 600)
    r = FakeRecognizer([heard, None])
    assert r.name == "parakeet" and FakeRecognizer(name="whisper").name == "whisper"
    press = ramp(16000)
    assert r.transcribe(press, ["Belra"]) is heard
    assert r.called.is_set()
    press[:] = 0                                                  # the caller's buffer, reused
    assert r.transcribe(press, []) is None
    assert r.transcribe(press, []) is None                        # then `default`
    assert [h for _, h in r.calls] == [["Belra"], [], []]
    assert np.array_equal(r.calls[0][0], ramp(16000))             # each call's audio, copied
    r.default = heard
    assert r.transcribe(press, []) is heard
    r.error = RuntimeError("broken")
    with pytest.raises(RuntimeError):
        r.transcribe(press, [])
    assert len(r.calls) == 5


# -- on the models (engines) ------------------------------------------------------------------

LINE = "Belra, you're on adds tonight."
ROSTER = ["Belra", "Coros", "Dunna"]
VOICES = [("af_heart", "a", "desk_mic"), ("am_michael", "a", "usb_headset"),
          ("bf_emma", "b", "headset_cheap")]


def spec_for(bank, base: str, lang: str, mic: str):
    from leeroy_voice.voices import spec_from_dict
    return spec_from_dict({"version": 1, "guid": 102, "name": "Belra", "engine": "kokoro",
                           "lang": lang, "timbre": {base: 1.0}, "prosody": {}, "speed": 1.0,
                           "pitch_st": 0.0, "mic": mic, "gain_db": 0.0, "locked": False,
                           "voices_sha256": bank.sha256, "ref": ""})


@pytest.fixture(scope="module")
def loopback(kokoro):
    """LINE in each test voice, through fx at grit 3 and resampled to 16 kHz, as the capture
    resamples: {voice: audio}."""
    from leeroy_voice import fx
    from leeroy_voice.decision import Decision
    from leeroy_voice.voices import StyleBank
    bank = StyleBank(kokoro.voices)
    out = {}
    for base, lang, mic in VOICES:
        spec = spec_for(bank, base, lang, mic)
        dry = kokoro.synth(LINE, spec, bank.style(spec, "none", Decision()), 1.0)
        wet = fx.process(dry, kokoro.sample_rate, spec, grit=3, whisper=False)
        out[base] = np.asarray(soxr.resample(wet, fx.OUT_RATE, SAMPLE_RATE, quality="HQ"),
                               np.float32)
    return out


@pytest.fixture(scope="module")
def corrector(tmp_path_factory):
    """The service's correction: the roster, the built-in lexicon's words and misaki's US
    dictionary as the common words."""
    from leeroy_voice.lexicon import load_lexicon
    words = load_lexicon(tmp_path_factory.mktemp("no-local-lexicon")).words()
    return RosterCorrector(ROSTER, words, common=common_words())


@pytest.mark.engines
@pytest.mark.parametrize("base", [v[0] for v in VOICES])
def test_loopback_through_kokoro(kokoro, parakeet, loopback, corrector, base, capsys):
    """LINE in a test voice through the 2009 chain, back through Parakeet: the corrected text
    holds "Belra" and "adds" (Parakeet writes "ads"), it is no phantom, and a press is heard in
    under 1 s."""
    audio = loopback[base]
    first = time.perf_counter()
    parakeet.transcribe(audio, [])
    cold = time.perf_counter() - first
    t0 = time.perf_counter()
    t = parakeet.transcribe(audio, [])
    took = time.perf_counter() - t0
    fixed = corrector.correct(t.text)
    with capsys.disabled():
        print(f"\n  {base}: {len(audio) / SAMPLE_RATE:.2f} s line, {t.speech_ms} ms speech, "
              f"heard in {took * 1000:.0f} ms (first call {cold * 1000:.0f} ms), confidence "
              f"{t.confidence:.3f}: {t.text!r} -> {fixed!r}")
    assert "Belra" in fixed and "adds" in fixed, (t.text, fixed)
    assert not is_phantom(t, min_confidence=0.4)
    assert t.confidence > 0.8
    assert 0 < t.speech_ms <= len(audio) // MS
    assert took < 1.0 and cold < 1.0


@pytest.mark.engines
def test_silence_is_no_speech(parakeet):
    noise = np.random.default_rng(60).standard_normal(2 * SAMPLE_RATE) * 10 ** (-60 / 20)
    assert parakeet.gate.speech(noise.astype(np.float32)) is None   # the gate says no speech
    assert parakeet.transcribe(noise.astype(np.float32), []) is None


@pytest.mark.engines
def test_gate_trims(parakeet, loopback):
    line = loopback["af_heart"]
    silence = np.zeros(SAMPLE_RATE, np.float32)
    press = np.concatenate([silence, line, silence])
    audio, speech_ms = parakeet.gate.speech(press)
    assert len(audio) < len(line) + 2 * PAD_MS * MS
    assert MIN_SPEECH_MS <= speech_ms <= len(line) // MS
    assert np.max(np.abs(audio)) > 0.1                            # the line, not the silence


@pytest.mark.engines
def test_recognition_runs_on_the_cpu_with_its_threads(parakeet):
    """Every session onnx-asr made for Parakeet runs on the CPU provider with the
    recognizer's 4 threads; Silero's on one."""
    adapter = parakeet._asr.asr
    for session in (adapter._encoder, adapter._decoder_joint):
        assert session.get_providers() == ["CPUExecutionProvider"]
        assert session.get_session_options().intra_op_num_threads == 4
    silero = parakeet.gate._vad._model
    assert silero.get_providers() == ["CPUExecutionProvider"]
    assert silero.get_session_options().intra_op_num_threads == 1
