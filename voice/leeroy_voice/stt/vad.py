"""The one speech gate both recognizers hear through (settled item 14, M25: the draft's "our own
VAD"): onnx-asr 0.12.0's Silero model on the CPU, on one thread. Push-to-talk already marks a
press's start and end, so the gate has two small jobs (draft section 6.3): say when a press holds
no speech (recognizers invent "Thank you." on silence), and cut away what is not speech.

speech() asks Silero for the press's speech segments with no padding and no minimum length of
their own (segment_batch with speech_pad_ms=0 and min_speech_duration_ms=0; its other settings
are onnx-asr's defaults: threshold 0.5, segments under 100 ms apart joined). The speech is their
total, in whole ms, unpadded; under min_speech_ms it is no speech (None), whether it came as one
segment or several. Otherwise the audio is cut to the segments, each padded by pad_ms, padded
segments that touch joined, so a pause in the line longer than 2 x pad_ms is shortened to that.

onnx-asr and onnxruntime are imported only when a gate is made (M9). The model is loaded from
its folder, which must exist: onnx-asr then resolves the file offline and never downloads (its
fallback download takes no revision, I4)."""
from pathlib import Path

import numpy as np

from . import MIN_SPEECH_MS, SAMPLE_RATE

PAD_MS = 100  # each segment's padding in the cut
VAD_MODEL = "silero"
WINDOW = 576  # samples: Silero's first window at 16 kHz (64 of context, 512 of hop), the least
# onnx-asr's segment_batch can read


def _load_silero(vad_dir: Path):
    import onnx_asr
    import onnxruntime as rt

    options = rt.SessionOptions()
    options.intra_op_num_threads = 1  # 32 ms frames, one at a time: more threads only spin
    options.inter_op_num_threads = 1
    return onnx_asr.load_vad(VAD_MODEL, vad_dir, sess_options=options,
                             providers=["CPUExecutionProvider"])


def _ms(value, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name}: expected whole milliseconds, 0 or more, got {value!r}")
    return value


def _audio(audio16k) -> np.ndarray:
    """A press as the gate reads it: mono float32, finite."""
    try:
        x = np.ascontiguousarray(audio16k, dtype=np.float32)
    except TypeError as e:  # numpy's ValueError (a string, say) is one already
        raise ValueError(f"audio: expected samples, got {type(audio16k).__name__}") from e
    if x.ndim != 1:
        raise ValueError(f"audio: expected mono samples, one dimension; got shape {x.shape}")
    if not np.isfinite(x).all():
        raise ValueError("audio: expected finite samples, got NaN or infinity")
    return x


class SpeechGate:
    def __init__(self, vad_dir, *, min_speech_ms: int = MIN_SPEECH_MS, pad_ms: int = PAD_MS):
        self.min_speech_ms = _ms(min_speech_ms, "min_speech_ms")
        self.pad_ms = _ms(pad_ms, "pad_ms")
        d = Path(vad_dir)
        if not d.is_dir():
            raise FileNotFoundError(f"{d}: no Silero model there (fetch-models --only silero)")
        self.vad_dir = d
        self._vad = _load_silero(d)

    def _segments(self, x: np.ndarray) -> list[tuple[int, int]]:
        """Silero's speech segments in `x`, in samples, unpadded."""
        [found] = list(self._vad.segment_batch(x[None, :], np.array([len(x)], np.int64),
                                               SAMPLE_RATE, speech_pad_ms=0,
                                               min_speech_duration_ms=0))
        return [(int(start), int(end)) for start, end in found]

    def speech(self, audio16k) -> tuple[np.ndarray, int] | None:
        """The press cut to its speech, each segment padded by pad_ms, and the speech in ms
        (unpadded); None under min_speech_ms. Raises ValueError for audio it cannot take."""
        x = _audio(audio16k)
        if len(x) < WINDOW or len(x) * 1000 < self.min_speech_ms * SAMPLE_RATE:
            return None  # too short to hold the minimum, or for Silero to read
        segments = self._segments(x)
        speech_ms = sum(end - start for start, end in segments) * 1000 // SAMPLE_RATE
        if speech_ms < self.min_speech_ms or not segments:
            return None
        pad = self.pad_ms * SAMPLE_RATE // 1000
        spans: list[list[int]] = []
        for start, end in segments:
            lo, hi = max(start - pad, 0), min(end + pad, len(x))
            if spans and lo <= spans[-1][1]:
                spans[-1][1] = max(spans[-1][1], hi)
            else:
                spans.append([lo, hi])
        return np.concatenate([x[lo:hi] for lo, hi in spans]), int(speech_ms)
