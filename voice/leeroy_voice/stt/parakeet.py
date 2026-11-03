"""Parakeet TDT 0.6B v2, int8, through onnx-asr 0.12.0 on the CPU: the baseline recognizer
(draft section 6.4), English only. It hears only what the speech gate passes (stt/vad.py), and
onnxruntime's CPU provider alone runs it, on `threads` intra-op threads: the GPU is never
touched.

onnx-asr and onnxruntime are imported only when a recognizer is made (M9). The model is loaded
from its folder, which must exist: onnx-asr then resolves its files offline and never downloads
(its fallback download takes no revision, I4). onnx-asr has no hotwords, so transcribe() ignores
them, and names are the corrector's job (stt/names.py).

The Transcript: the text as Parakeet wrote it, whitespace folded; `confidence` the mean
probability of the tokens it emitted (exp of each one's log-probability, from
with_timestamps()'s `logprobs`), 0 when there are no words; `speech_ms` the gate's."""
from pathlib import Path

import numpy as np

from . import SAMPLE_RATE, Transcript

MODEL = "nemo-parakeet-tdt-0.6b-v2"
QUANTIZATION = "int8"


def _load_parakeet(model_dir: Path, threads: int):
    import onnx_asr
    import onnxruntime as rt

    options = rt.SessionOptions()
    options.intra_op_num_threads = threads
    return onnx_asr.load_model(MODEL, model_dir, quantization=QUANTIZATION, sess_options=options,
                               providers=["CPUExecutionProvider"]).with_timestamps()


def _transcript(result, speech_ms) -> Transcript:
    text = " ".join(result.text.split())
    logprobs = [] if result.logprobs is None else list(result.logprobs)
    if not text:
        return Transcript("", 0.0, int(speech_ms))
    if not logprobs:
        raise RuntimeError("onnx-asr gave a transcript without its log-probabilities")
    confidence = float(np.mean(np.exp(np.asarray(logprobs, dtype=np.float64))))
    return Transcript(text, confidence, int(speech_ms))  # log-softmax: each exp is 0-1


class ParakeetRecognizer:
    name = "parakeet"

    def __init__(self, model_dir, gate, *, threads: int):
        if type(threads) is not int or not 1 <= threads <= 64:
            raise ValueError(f"threads: expected an integer 1-64, got {threads!r}")
        d = Path(model_dir)
        if not d.is_dir():
            raise FileNotFoundError(f"{d}: no Parakeet model there "
                                    "(fetch-models --only parakeet)")
        self.model_dir, self.gate, self.threads = d, gate, threads
        self._asr = _load_parakeet(d, threads)

    def transcribe(self, audio16k, hotwords) -> Transcript | None:
        cut = self.gate.speech(audio16k)
        if cut is None:
            return None
        audio, speech_ms = cut
        return _transcript(self._asr.recognize(audio, sample_rate=SAMPLE_RATE), speech_ms)
