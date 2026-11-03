"""Kokoro-82M through kokoro-onnx on the CPU, with misaki's G2P (draft section 7.2), as the
listening kit renders it: misaki turns the line, with the lexicon's and the levers' markup, into
phonemes in the voice's accent, and Kokoro speaks the phonemes with the voice's style.

onnxruntime, kokoro_onnx, misaki and spaCy are imported only when an engine is made (M9). The
session runs on onnxruntime's CPU provider alone: the GPU is never touched.

Nothing of a line leaves the engine but its audio: a failure is an EngineError naming its type
(kokoro-onnx's own messages quote the phonemes), and what misaki prints (a number it cannot
read, with the word) is caught instead of reaching stdout."""
import contextlib
import io
import logging
import threading
from pathlib import Path

import numpy as np

from . import EngineError

SAMPLE_RATE = 24000
LANGS = ("a", "b")  # a US English, b British English: VoiceSpec.lang
SPACY_MODEL = "en_core_web_sm"


class KokoroEngine:
    name = "kokoro"
    sample_rate = SAMPLE_RATE

    def __init__(self, model: Path, voices: Path, *, threads: int):
        import onnxruntime as rt
        import spacy
        from kokoro_onnx import Kokoro
        from misaki import en, espeak

        if not spacy.util.is_package(SPACY_MODEL):  # misaki would download it
            raise RuntimeError(f"{SPACY_MODEL} is not installed: install the tts extra")
        # kokoro-onnx logs each line's phonemes at DEBUG when LOG_LEVEL asks: never line text
        logging.getLogger("kokoro_onnx").setLevel(logging.WARNING)
        self.model, self.voices, self.threads = Path(model), Path(voices), threads
        options = rt.SessionOptions()
        options.intra_op_num_threads = threads
        session = rt.InferenceSession(str(self.model), sess_options=options,
                                      providers=["CPUExecutionProvider"])
        self._kokoro = Kokoro.from_session(session, str(self.voices))
        self._g2p = {lang: en.G2P(trf=False, british=lang == "b",
                                  fallback=espeak.EspeakFallback(british=lang == "b"))
                     for lang in LANGS}
        self._g2p_lock = threading.Lock()  # spaCy, misaki and espeak-ng keep shared state

    @property
    def providers(self) -> list[str]:
        return self._kokoro.sess.get_providers()

    @property
    def threads_in_use(self) -> int:
        """The intra-op thread count the session itself runs with."""
        return self._kokoro.sess.get_session_options().intra_op_num_threads

    def phonemes(self, text: str, lang: str) -> str:
        """misaki's phonemes for `text` in the accent of `lang`; its [word](/IPA/) and
        [word](+n) markup is honoured."""
        if lang not in LANGS:
            raise ValueError(f"lang: expected one of {', '.join(LANGS)}, got {lang!r}")
        # the redirect swaps sys.stdout for the whole process, for the length of the call
        with self._g2p_lock, contextlib.redirect_stdout(io.StringIO()):
            try:
                phonemes, _ = self._g2p[lang](text)
            except Exception as e:
                raise EngineError(f"misaki: {type(e).__name__} on a line of {len(text)} "
                                  "characters") from None
        return phonemes

    def synth(self, text: str, spec, style, speed: float) -> np.ndarray:
        phonemes = self.phonemes(text, spec.lang)
        try:
            audio, sample_rate = self._kokoro.create(phonemes, voice=style, speed=speed,
                                                     is_phonemes=True)
        except Exception as e:
            raise EngineError(f"kokoro: {type(e).__name__} on {len(phonemes)} phonemes") from None
        if sample_rate != self.sample_rate:
            raise EngineError(f"kokoro: {sample_rate} Hz, not {self.sample_rate}")
        return np.asarray(audio, dtype=np.float32)
