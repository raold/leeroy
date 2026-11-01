"""The engines that turn a line into audio: Kokoro (engines/kokoro.py) on the CPU, the baseline;
FakeEngine (engines/fake.py) in its place in tests; an expressive engine only if the listening
test and the owner choose one (Task 22)."""
from typing import Protocol

import numpy as np

from ..voices import VoiceSpec


class EngineError(RuntimeError):
    """An engine failed on a line. Its message names the failure's type and counts only, never
    the line's text or phonemes (logs.py: no line text), so it may be logged as it is."""


class Engine(Protocol):
    name: str
    sample_rate: int

    def synth(self, text: str, spec: VoiceSpec, style, speed: float) -> np.ndarray:
        """Mono float32 audio at `sample_rate` for `text`, the line as the ear and the levers
        wrote it (for Kokoro with misaki's markup), in the voice `spec` and `style` give (for
        Kokoro, StyleBank.style), at `speed`: the spec's own speed times the line's levers.
        Raises EngineError when the engine fails on the line."""
        ...
