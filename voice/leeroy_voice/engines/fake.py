"""FakeEngine: a deterministic engine for tests. A sine tone whose pitch follows the member's
guid, 60 ms per character at speed 1.0; speed divides the length, as Kokoro's divides its
durations. The style is not used."""
import numpy as np

MS_PER_CHAR = 60
AMPLITUDE = 0.25


class FakeEngine:
    name = "fake"

    def __init__(self, sample_rate: int = 24000):
        self.sample_rate = sample_rate

    @staticmethod
    def pitch_hz(guid: int) -> float:
        return 110.0 + 10.0 * (guid % 32)

    def synth(self, text: str, spec, style, speed: float) -> np.ndarray:
        n = round(len(text) * MS_PER_CHAR * self.sample_rate / (1000 * speed))
        t = np.arange(n) / self.sample_rate
        return (AMPLITUDE * np.sin(2 * np.pi * self.pitch_hz(spec.guid) * t)).astype(np.float32)
