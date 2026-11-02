"""The mixer (draft section 5): every member's voice summed onto one stereo master, at the gains
the scheduler and the service give it, under a -1 dBFS limiter.

It is thread-safe: the service plays, appends, stops and sets gains from its event loop while
the audio callback renders on PortAudio's thread. One lock keeps each call whole, and apply()
takes a whole Plan under it, so a block never hears half a Plan.

- play(id, audio, gain_db, fade_in_ms=10, *, guid=0) starts a voice: 48 kHz mono samples, faded
  in linearly. A play for an id it holds replaces that voice: the scheduler resumes a line whose
  audio ran out with a start of the same id, a fresh voice.
- append(id, audio) queues more audio behind a voice. A voice whose audio ran out stays held,
  silent, until ended() reports it, and an append before that revives it and withdraws its end.
  So an end is reported only once everything handed over has played (Task 6's contract: a stale
  end would let the member overlap themself). An append for an id it does not hold, or one it
  is stopping, is dropped: the scheduler never sends one, as it hears of each end before it
  plans the next segment.
- stop(id, fade_ms) fades a voice out linearly and drops it. A stopped voice did not finish,
  so ended() does not report it. A stop or set_gain for an id it does not hold does nothing.
- set_gain(id, db, ramp_ms=50) moves a voice's gain linearly over ramp_ms.
- apply(plan, lines, own_db) applies a scheduler Plan in its order: the stops, the starts, the
  appends, then the ducks in `gains`. A start plays at the service's own gain for the line
  (own_db: the spec, his UI volume, the levers) plus the start's gain (the tier offset and the
  duck); a duck sets a voice to its own gain plus its tier offset plus that duck, since `gains`
  carry the duck alone. The tier offset is the scheduler's own rule, and the guid that places a
  voice comes from `lines`. The mixer ducks nothing itself: the scheduler decides who ducks whom.
- render(frames) gives the next frames of the master, stereo float32 (frames, 2), for the
  audio callback. ended() lists the voices that played to their end since the last call, each
  once, in order; active() the voices still sounding (fading out included).
- Mono and centred, like vent: with stereo_spread s (0 by default) each member sits at a fixed
  place within s of the centre (-1 left, 1 right), chosen by guid, on a constant-power pan.
- The master passes a look-ahead limiter: no sample leaves above CEILING_DB, however hot a clip
  (fx.process sets loudness, not peaks), for a delay of `latency` frames (about 2 ms). It holds
  the gain needed over a window of the look-ahead's length, releases it over RELEASE_MS, and
  smooths it with a moving average of the same length, so the gain is down before a peak
  arrives and a steady hot tone comes out the same tone, at the ceiling, unclipped."""
import math
import numbers
import threading
from collections import deque
from types import MappingProxyType
from typing import Mapping

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from .schedule import Line, Plan, _tier_gain

SAMPLE_RATE = 48000
CEILING_DB = -1.0
LOOKAHEAD_MS = 2.0
RELEASE_MS = 100.0  # the limiter's release time constant
FADE_IN_MS = 10
GAIN_RAMP_MS = 50
MAX_BLOCK = 4800  # render works in pieces this long at most
_GOLDEN = (math.sqrt(5) - 1) / 2  # spreads consecutive guids evenly across the field
_NONE = 1e-7  # a gain reduction this small is none: the limiter idles
_EMPTY: Mapping = MappingProxyType({})


def _real(v) -> bool:
    """A finite real number (numpy's too), not a bool."""
    return isinstance(v, numbers.Real) and not isinstance(v, bool) and math.isfinite(v)


def _db(db, what: str) -> float:
    if not _real(db):
        raise ValueError(f"{what}: expected a finite number of dB, got {db!r}")
    return float(db)


def _ms(ms, what: str) -> float:
    if not _real(ms) or ms < 0:
        raise ValueError(f"{what}: expected a duration of 0 ms or more, got {ms!r}")
    return float(ms)


def _clip(audio) -> np.ndarray:
    x = np.ascontiguousarray(audio, dtype=np.float32)
    if x.ndim != 1:
        raise ValueError(f"audio: expected mono samples, one dimension; got shape {x.shape}")
    if not np.isfinite(x).all():
        raise ValueError("audio: expected finite samples, got NaN or infinity")
    return x


class Limiter:
    """A stereo-linked look-ahead peak limiter: process() blocks of (n, 2) float32, at most
    MAX_BLOCK long, and every sample out is within the ceiling, `latency` frames late."""

    def __init__(self, sample_rate: int = SAMPLE_RATE, *, ceiling_db: float = CEILING_DB,
                 lookahead_ms: float = LOOKAHEAD_MS, release_ms: float = RELEASE_MS):
        self.ceiling = 10 ** (ceiling_db / 20)
        self._w = max(1, round(lookahead_ms * sample_rate / 1000))  # the window, in frames
        self.latency = self._w - 1
        self._beta = math.exp(-1000 / (release_ms * sample_rate))
        self._x = np.zeros((self.latency, 2), np.float32)  # the delay line
        self._r = np.ones(self.latency)  # the gains the window's last frames need
        self._h = np.ones(self.latency)  # the held gains the average still reads
        self._d = 0.0  # the gain reduction (1 - gain) the release has reached
        self._idle = True  # nothing anywhere asks for a reduction

    def process(self, x: np.ndarray) -> np.ndarray:
        n, c = len(x), self.ceiling
        if n == 0:
            return np.zeros((0, 2), np.float32)
        peak = np.abs(x).max(axis=1)
        xe = np.concatenate([self._x, x])
        self._x = xe[n:]
        if self._idle and peak.max() <= c:
            return xe[:n]  # nothing to limit: only the delay
        r = np.ones(n)
        hot = peak > c
        r[hot] = c / peak[hot]
        re = np.concatenate([self._r, r])
        held = sliding_window_view(re, self._w).min(axis=1)  # each frame's window, as the gain
        # the release, d[i] = max(1 - held[i], beta * d[i - 1]), unrolled into a running max
        undo = self._beta ** -np.arange(n)
        d = np.maximum.accumulate((1 - held) * undo)
        np.maximum(d, self._beta * self._d, out=d)
        d /= undo
        he = np.concatenate([self._h, 1 - d])
        sums = np.concatenate([[0.0], np.cumsum(he)])
        gain = (sums[self._w:] - sums[:-self._w]) / self._w  # the moving average: the attack
        y = (xe[:n] * gain[:, None]).astype(np.float32)
        np.clip(y, -c, c, out=y)  # trims rounding only: the gain has done the work
        self._r, self._h = re[n:], he[n:]
        self._d = float(d[-1]) if d[-1] >= _NONE else 0.0
        self._idle = (self._d == 0.0 and self._r.min(initial=1.0) >= 1.0
                      and self._h.min(initial=1.0) >= 1.0)
        return y


class _Voice:
    __slots__ = ("chunks", "pos", "g", "target", "step", "left", "pan", "base_db", "stopping",
                 "dry")

    def __init__(self, audio: np.ndarray, gain: float, fade: int, pan: tuple[float, float],
                 base_db: float):
        self.chunks = deque([audio]) if len(audio) else deque()
        self.pos = 0  # into the first chunk
        self.pan = pan
        self.base_db = base_db  # its level without a duck: what apply() ducks from
        self.stopping = False
        self.dry = False  # its audio ran out; held until ended() reports it
        self.g, self.target, self.step, self.left = 0.0, gain, 0.0, 0
        self.ramp_to(gain, fade)

    def ramp_to(self, target: float, frames: int) -> None:
        if frames <= 0:
            self.g = self.target = target
            self.step, self.left = 0.0, 0
        else:
            self.target, self.left = target, frames
            self.step = (target - self.g) / frames

    def read(self, n: int) -> np.ndarray:
        parts = []
        while n and self.chunks:
            chunk = self.chunks[0]
            take = min(len(chunk) - self.pos, n)
            parts.append(chunk[self.pos:self.pos + take])
            self.pos += take
            n -= take
            if self.pos == len(chunk):
                self.chunks.popleft()
                self.pos = 0
        if len(parts) == 1:
            return parts[0]
        return np.concatenate(parts) if parts else np.zeros(0, np.float32)

    def gains(self, m: int) -> float | np.ndarray:
        """The gain for its next m frames: a number when it holds still, else one per frame."""
        if self.left <= 0:
            return self.g
        k = min(m, self.left)
        curve = self.g + self.step * np.arange(1, k + 1)
        self.left -= k
        if self.left == 0:
            curve[-1] = self.target
        self.g = float(curve[-1])
        if k < m:
            curve = np.concatenate([curve, np.full(m - k, self.g)])
        return curve.astype(np.float32)


class Mixer:
    def __init__(self, sample_rate: int = SAMPLE_RATE, *, stereo_spread: float = 0.0):
        if type(sample_rate) is not int or sample_rate <= 0:
            raise ValueError(f"sample_rate: expected a positive whole number, got {sample_rate!r}")
        if (type(stereo_spread) not in (int, float) or not math.isfinite(stereo_spread)
                or not 0 <= stereo_spread <= 1):
            raise ValueError(f"stereo_spread: expected 0 to 1, got {stereo_spread!r}")
        self.sample_rate = sample_rate
        self.stereo_spread = float(stereo_spread)
        self._lock = threading.Lock()
        self._voices: dict[str, _Voice] = {}
        self._ended: dict[str, None] = {}  # ids whose audio ran out, not yet reported, in order
        self._limiter = Limiter(sample_rate)
        self.latency = self._limiter.latency

    # the calls

    def play(self, id: str, audio, gain_db: float, fade_in_ms: float = FADE_IN_MS, *,
             guid: int = 0) -> None:
        audio, gain_db = _clip(audio), _db(gain_db, "gain_db")
        fade = self._frames(_ms(fade_in_ms, "fade_in_ms"))
        with self._lock:
            self._play(id, audio, gain_db, fade, guid, gain_db)

    def append(self, id: str, audio) -> None:
        audio = _clip(audio)
        with self._lock:
            self._append(id, audio)

    def stop(self, id: str, fade_ms: float) -> None:
        fade = self._frames(_ms(fade_ms, "fade_ms"))
        with self._lock:
            self._stop(id, fade)

    def set_gain(self, id: str, db: float, ramp_ms: float = GAIN_RAMP_MS) -> None:
        gain = 10 ** (_db(db, "db") / 20)
        frames = self._frames(_ms(ramp_ms, "ramp_ms"))
        with self._lock:
            voice = self._voices.get(id)
            if voice is not None and not voice.stopping:
                voice.ramp_to(gain, frames)

    def apply(self, plan: Plan, lines: Mapping[str, Line],
              own_db: Mapping[str, float] = _EMPTY) -> None:
        """Apply a scheduler Plan (see the module's docstring). `lines` holds the Line of each
        id the Plan starts (its guid and tier), `own_db` the service's gain for it (0 dB when
        absent). A Plan with a bad clip or gain raises ValueError before any of it applies."""
        starts = []
        for line_id, audio, gain_db in plan.starts:
            line = lines.get(line_id)
            own = _db(own_db.get(line_id, 0.0), "own_db")
            tier = _tier_gain(line.tier) if line is not None else 0.0
            starts.append((line_id, _clip(audio), own + _db(gain_db, "gain"), own + tier,
                           line.guid if line is not None else 0))
        appends = [(line_id, _clip(audio)) for line_id, audio in plan.appends]
        stops = [(line_id, self._frames(_ms(fade, "fade"))) for line_id, fade, _ in plan.stops]
        ducks = [(line_id, _db(duck, "duck")) for line_id, duck in plan.gains.items()]
        fade_in, ramp = self._frames(FADE_IN_MS), self._frames(GAIN_RAMP_MS)
        with self._lock:
            for line_id, fade in stops:
                self._stop(line_id, fade)
            for line_id, audio, gain_db, base_db, guid in starts:
                self._play(line_id, audio, gain_db, fade_in, guid, base_db)
            for line_id, audio in appends:
                self._append(line_id, audio)
            for line_id, duck in ducks:
                voice = self._voices.get(line_id)
                if voice is not None and not voice.stopping:
                    voice.ramp_to(10 ** ((voice.base_db + duck) / 20), ramp)

    def render(self, frames: int) -> np.ndarray:
        if not isinstance(frames, numbers.Integral) or isinstance(frames, bool) or frames < 0:
            raise ValueError(f"frames: expected a whole number 0 or more, got {frames!r}")
        frames = int(frames)
        out = np.empty((frames, 2), np.float32)
        with self._lock:
            for start in range(0, frames, MAX_BLOCK):
                n = min(MAX_BLOCK, frames - start)
                out[start:start + n] = self._block(n)
        return out

    def ended(self) -> list[str]:
        with self._lock:
            ids = list(self._ended)
            self._ended.clear()
            for line_id in ids:
                self._voices.pop(line_id, None)
            return ids

    def active(self) -> set[str]:
        with self._lock:
            return {line_id for line_id, voice in self._voices.items() if not voice.dry}

    # under the lock

    def _frames(self, ms: float) -> int:
        return round(ms * self.sample_rate / 1000)

    def _pan(self, guid: int) -> tuple[float, float]:
        if not self.stereo_spread or type(guid) is not int or guid <= 0:
            return 1.0, 1.0
        place = self.stereo_spread * (2 * ((guid * _GOLDEN) % 1.0) - 1)
        angle = (place + 1) * math.pi / 4
        return math.sqrt(2) * math.cos(angle), math.sqrt(2) * math.sin(angle)

    def _play(self, id: str, audio: np.ndarray, gain_db: float, fade: int, guid: int,
              base_db: float) -> None:
        self._ended.pop(id, None)
        self._voices[id] = _Voice(audio, 10 ** (gain_db / 20), fade, self._pan(guid), base_db)

    def _append(self, id: str, audio: np.ndarray) -> None:
        voice = self._voices.get(id)
        if voice is None or voice.stopping or not len(audio):
            return
        voice.chunks.append(audio)
        if voice.dry:  # it ran out just before this came: it plays on, and its end is withdrawn
            voice.dry = False
            self._ended.pop(id, None)

    def _stop(self, id: str, fade: int) -> None:
        voice = self._voices.get(id)
        if voice is None:
            return
        if voice.dry or fade <= 0:
            del self._voices[id]
            self._ended.pop(id, None)
            return
        voice.stopping = True
        voice.ramp_to(0.0, fade)

    def _block(self, n: int) -> np.ndarray:
        mix = np.zeros((n, 2), np.float32)
        for line_id, voice in list(self._voices.items()):
            if voice.dry:
                continue
            x = voice.read(n)
            m = len(x)
            if m:
                seg = x * voice.gains(m)
                left, right = voice.pan
                if left == right == 1.0:
                    mix[:m] += seg[:, None]
                else:
                    mix[:m, 0] += seg * left
                    mix[:m, 1] += seg * right
            if voice.stopping:
                if voice.left == 0 or not voice.chunks:  # faded out, or nothing left to fade
                    del self._voices[line_id]
            elif not voice.chunks:
                voice.dry = True
                self._ended[line_id] = None
        return self._limiter.process(mix)
