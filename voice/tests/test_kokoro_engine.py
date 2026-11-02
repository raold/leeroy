"""The Kokoro engine. On the fetched model files, on the CPU: marked `engines` (deselected by
default, and skipped by the `kokoro` fixture when the files are not fetched). Without a model,
in the default run: how synth wires its arguments into kokoro-onnx, and what its errors and
misaki's prints may carry."""
import threading
import time
import traceback

import numpy as np
import pytest

from leeroy_voice.decision import Decision
from leeroy_voice.engines import EngineError
from leeroy_voice.engines.kokoro import KokoroEngine
from leeroy_voice.voices import StyleBank, spec_from_dict

LINE = "Pulling in three, healers on the tank."
LIVE_LINE = "Pulling in three, healers on the tank, everyone else stack up on me."
ACCENTED = "I can't find the tomato."  # can't and tomato: two readings each


@pytest.fixture(scope="module")
def bank(kokoro):
    return StyleBank(kokoro.voices)


def voice(sha, lang, base, guid):
    name = {101: "Arkon", 102: "Belra"}[guid]
    return spec_from_dict({"version": 1, "guid": guid, "name": name, "engine": "kokoro",
                           "lang": lang, "timbre": {base: 1.0}, "prosody": {}, "speed": 1.0,
                           "pitch_st": 0.0, "mic": "desk_mic", "gain_db": 0.0, "locked": False,
                           "voices_sha256": sha, "ref": ""})


def rms_dbfs(audio) -> float:
    return 20 * np.log10(np.sqrt(np.mean(np.square(audio.astype(np.float64)))))


def same(a, b) -> bool:
    return len(a) == len(b) and np.allclose(a, b, atol=1e-4)


@pytest.mark.engines
def test_kokoro_renders_both_accents(kokoro, bank):    # 24 kHz, RMS above -40 dBFS, US and UK
    assert kokoro.phonemes("tomato", "a") != kokoro.phonemes("tomato", "b")
    assert kokoro.sample_rate == 24000 and kokoro.providers == ["CPUExecutionProvider"]
    for lang, base in (("a", "af_heart"), ("b", "bf_emma")):
        s = voice(bank.sha256, lang, base, 102)
        audio = kokoro.synth(LINE, s, bank.style(s, "none", Decision()), 1.0)
        assert audio.dtype == np.float32 and audio.ndim == 1
        assert 1.0 < len(audio) / kokoro.sample_rate < 6.0, lang
        assert rms_dbfs(audio) > -40, lang


@pytest.mark.engines
def test_kokoro_is_live_capable_on_6_threads(kokoro, bank):    # a ~3 s line in under 1.5 s
    assert kokoro.threads_in_use == 6                             # the session's own setting
    s = voice(bank.sha256, "a", "am_michael", 101)
    style = bank.style(s, "none", Decision())
    kokoro.synth(LINE, s, style, 1.0)                              # warm up the session
    t0 = time.perf_counter()
    audio = kokoro.synth(LIVE_LINE, s, style, 1.0)
    took = time.perf_counter() - t0
    seconds = len(audio) / kokoro.sample_rate
    assert 2.0 <= seconds <= 5.0
    assert took < 1.5, f"{seconds:.2f} s of audio took {took:.2f} s"


@pytest.mark.engines
def test_kokoro_follows_speed(kokoro, bank):
    s = voice(bank.sha256, "a", "af_heart", 102)
    style = bank.style(s, "none", Decision())
    ratio = len(kokoro.synth(LINE, s, style, 0.9)) / len(kokoro.synth(LINE, s, style, 1.15))
    assert 1.10 < ratio < 1.35, ratio     # 1.15 / 0.9 is 1.28, less the pauses speed leaves alone


@pytest.mark.engines
def test_kokoro_follows_the_style(kokoro, bank):
    her, him = voice(bank.sha256, "a", "af_heart", 102), voice(bank.sha256, "a", "am_michael", 101)
    style = bank.style(her, "none", Decision())
    base = kokoro.synth(LINE, her, style, 1.0)
    assert same(base, kokoro.synth(LINE, her, style, 1.0))        # the same input, the same audio
    assert not same(base, kokoro.synth(LINE, her, bank.style(him, "none", Decision()), 1.0))


@pytest.mark.engines
def test_kokoro_follows_the_accent(kokoro, bank):    # one style, a US and a UK spec
    us, uk = voice(bank.sha256, "a", "af_heart", 102), voice(bank.sha256, "b", "bf_emma", 102)
    style = bank.style(us, "none", Decision())
    assert not same(kokoro.synth(ACCENTED, us, style, 1.0), kokoro.synth(ACCENTED, uk, style, 1.0))


@pytest.mark.engines
def test_misaki_takes_the_markup(kokoro):            # M12: in a sentence, where "you" is unstressed
    assert kokoro.phonemes("I saved [you](+2) a spot.", "a") != kokoro.phonemes("I saved you a spot.", "a")
    assert "kˌɛlθˈuzɑd" in kokoro.phonemes("[Kel'Thuzad](/kˌɛlθˈuzɑd/)", "a")
    assert "kˌɛlθˈuzɑd" in kokoro.phonemes("[Kel'Thuzad](/kˌɛlθˈuzɑd/)", "b")
    with pytest.raises(ValueError):
        kokoro.phonemes("tomato", "c")


# -- without a model: the wiring, in the default run --------------------------------------------

SECRET = "a line nobody may read"


class FakeKokoro:
    """kokoro-onnx's Kokoro as synth uses it: create() is recorded, or raises `error`."""

    def __init__(self, error: Exception | None = None):
        self.calls: list = []
        self.error = error

    def create(self, phonemes, **kw):
        self.calls.append((phonemes, kw))
        if self.error is not None:
            raise self.error
        return np.zeros(2400, dtype=np.float32), 24000


def fake_g2p(lang):
    return lambda text: (f"{lang}:{text}", [])


def wired(kokoro_, g2p=None) -> KokoroEngine:
    """A KokoroEngine without its model: the parts __init__ builds, replaced."""
    engine = KokoroEngine.__new__(KokoroEngine)
    engine._kokoro = kokoro_
    engine._g2p = g2p or {lang: fake_g2p(lang) for lang in "ab"}
    engine._g2p_lock = threading.Lock()
    return engine


def test_synth_hands_kokoro_what_it_was_given():
    k = FakeKokoro()
    style = np.ones((510, 1, 256), dtype=np.float32)
    audio = wired(k).synth("Pulling.", voice("0" * 64, "b", "bf_emma", 102), style, 1.07)
    assert audio.dtype == np.float32 and len(audio) == 2400
    [(phonemes, kw)] = k.calls
    assert phonemes == "b:Pulling."                                # the spec's accent
    assert kw["voice"] is style and kw["speed"] == 1.07 and kw["is_phonemes"] is True


def test_engine_errors_carry_no_text():
    """kokoro-onnx's own messages quote the phonemes ("Nothing to synthesize, '...'"); an
    EngineError names only the error's type, and the original is not chained."""
    us = voice("0" * 64, "a", "af_heart", 102)
    k = FakeKokoro(error=ValueError(f"Nothing to synthesize, {SECRET!r} produced no phonemes"))
    with pytest.raises(EngineError) as info:
        wired(k).synth(SECRET, us, None, 1.0)
    shown = "".join(traceback.format_exception(info.value))
    assert "ValueError" in str(info.value)
    assert SECRET not in shown                                     # nor the phonemes, which hold it

    def broken(text):
        raise AssertionError(("a token", text))
    with pytest.raises(EngineError) as info:
        wired(FakeKokoro(), g2p={"a": broken, "b": broken}).synth(SECRET, us, None, 1.0)
    assert SECRET not in "".join(traceback.format_exception(info.value))


def test_misaki_prints_kept_out(capsys):
    """misaki prints a number it cannot read (en.py:440) with the word: never onto stdout."""
    def g2p(text):
        print("TODO:NUM", text)
        return "phonemes", []
    engine = wired(FakeKokoro(), g2p={"a": g2p, "b": g2p})
    assert engine.phonemes(SECRET, "a") == "phonemes"
    assert capsys.readouterr().out == ""
