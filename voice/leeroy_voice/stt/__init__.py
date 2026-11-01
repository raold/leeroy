"""Recognition: his push-to-talk presses into text (draft section 6).

- stt/vad.py: the one speech gate every recognizer hears through (settled item 14, M25), Silero
  through onnx-asr on the CPU.
- stt/parakeet.py: Parakeet TDT 0.6B v2 int8 through onnx-asr on the CPU, the baseline. Whisper
  on the GPU (stt/whisper.py, Task 21) is the upgrade, behind the same gate. FakeRecognizer
  (tests/fakes.py) stands in for both in tests.
- stt/names.py: the roster correction of the text, since onnx-asr has no hotwords.
- stt/phantoms.py: the hallucination guard, the same for both.

A recognizer is handed a whole press, mono float32 at 16 kHz as the capture gives it (Task 8),
and returns a Transcript, or None when the gate finds no speech in it. Nothing here logs or keeps
what he said (logs.py: no line text), and no audio is written."""
from dataclasses import dataclass
from typing import Protocol

import numpy as np

SAMPLE_RATE = 16000  # the capture's, and both recognizers'
MIN_SPEECH_MS = 250  # the gate's default minimum (draft section 6.3); less is no speech


@dataclass(frozen=True, slots=True)
class Transcript:
    """What a recognizer heard in a press: `text` as it wrote it (before the name correction),
    `confidence` 0-1 (the mean token probability, for Parakeet) and `speech_ms`, the speech the
    gate found, unpadded. Plain Python values only, as the wire's `heard` takes them: an int
    confidence becomes a float; anything else is a ValueError."""

    text: str
    confidence: float
    speech_ms: int

    def __post_init__(self):
        if type(self.text) is not str:
            raise ValueError(f"text: expected a str, got {type(self.text).__name__}")
        c = self.confidence
        if type(c) not in (int, float) or not 0 <= c <= 1:  # NaN fails it too
            raise ValueError(f"confidence: expected a number 0-1, got {c!r}")
        object.__setattr__(self, "confidence", float(c))
        if type(self.speech_ms) is not int or self.speech_ms < 0:
            raise ValueError(f"speech_ms: expected whole milliseconds, 0 or more, got "
                             f"{self.speech_ms!r}")


class Recognizer(Protocol):
    name: str  # what status.stt reports: "parakeet" or "whisper"

    def transcribe(self, audio16k: np.ndarray, hotwords: list[str]) -> Transcript | None:
        """The press `audio16k` (mono float32, 16 kHz) through the speech gate, then the model;
        None when the gate finds no speech. `hotwords` bias a recognizer that takes them
        (Whisper, Task 21) toward the moment's names; Parakeet ignores them. Runs on the
        recognizer's own thread, never the event loop."""
        ...
