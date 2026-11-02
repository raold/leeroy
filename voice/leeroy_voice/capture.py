"""His side of push-to-talk (draft section 6.2): the microphone, a 300 ms ring in front of every
press, and the clip the recognizer gets.

Capture: the input stream runs into a ring of preroll_ms, overwritten in place, so audio
outside a press is gone within 300 ms and never handed on (the privacy rule). begin() takes the
whole ring as the clip's start, so at the default gate (the Press comes 150 ms after the key
went down) the first syllable is never cut; end() dates the key-up, and the clip runs tail_ms
on after it; take() hands it over, downmixed to mono and resampled to 16 kHz, once its tail is
in. A press records at most max_s after its begin(), its tail on top, so PttMachine's capped
hold (max_hold_s from the key-down) is never cut short and a missing end() never grows it.
discard() drops a press and any clip not yet taken (the session went offline, Review Focus 2);
reset() is a break in the stream: the ring empties, a press across the break is dropped, a clip
whose tail was still coming is finished as it is, and the stream's format may change. end() says
whether a clip is coming, so the service never waits for one a dropped press will not give.

feed() runs on PortAudio's thread and takes only a lock and a copy; begin, end, discard, take
and reset run on the event loop. take() resamples outside the lock.

Mic: the microphone the local voice.toml names, by that name and never another device (the
owner's answer, PR15), kept open by a StreamKeeper with the output's rules (review-task7 M1): a
stream the output's refresh closed comes back at the next poll, one that died is opened again,
a missing one is tried every RETRY_S. Each new stream resets the capture to its rate and
channels, and blocks that reach it before that are dropped: they may be in another format.
`state` is what `status.mic` reports: "ok", "missing", "unset" (no name in the config),
"closed". The device's name is never logged."""
import logging
import math
import threading
import time
from collections import deque
from dataclasses import dataclass, field

import numpy as np
import soxr

from .audio_io import DeviceApi, Stream, StreamKeeper
from .logs import log

STT_RATE = 16000  # what the recognizers take


def _count(v, what: str) -> int:
    if isinstance(v, bool) or not isinstance(v, (int, np.integer)) or v < 1:
        raise ValueError(f"{what} must be a whole number, 1 or more: {v!r}")
    return int(v)


@dataclass(slots=True, eq=False)
class _Clip:
    """A press being recorded, at the stream's rate: frames counted from its first, the ring."""
    rate: int
    seed: int  # the ring's frames at its start
    limit: int  # where the recording stops if no end() comes: seed + max_s
    tail: int
    began: float  # begin()'s time
    blocks: list[np.ndarray] = field(default_factory=list)
    frames: int = 0
    end: int | None = None  # where it ends, its tail included; None while the key is down

    def add(self, block: np.ndarray) -> bool:
        """Records a block; True once the clip is whole."""
        if self.end is None and self.frames + len(block) >= self.limit:
            self.end = self.limit + self.tail  # the press ran to max_s: its tail follows
        if self.end is not None:
            block = block[:max(0, self.end - self.frames)]
        if len(block):
            self.blocks.append(block)
            self.frames += len(block)
        return self.end is not None and self.frames >= self.end

    def cut(self) -> None:
        """Drop what lies past `end`."""
        extra = self.frames - self.end
        while extra > 0:
            last = self.blocks.pop()
            if len(last) > extra:
                self.blocks.append(last[:len(last) - extra])
            extra -= len(last)
        self.frames = min(self.frames, self.end)


class Capture:
    def __init__(self, device_rate: int, channels: int, *, preroll_ms: int = 300,
                 tail_ms: int = 200, max_s: float = 30.0):
        for name, v in (("preroll_ms", preroll_ms), ("tail_ms", tail_ms)):
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v < math.inf:
                raise ValueError(f"{name} must be 0 or more: {v!r}")
        if (isinstance(max_s, bool) or not isinstance(max_s, (int, float))
                or not 0 < max_s < math.inf):
            raise ValueError(f"max_s must be more than 0: {max_s!r}")
        self._preroll_ms, self._tail_ms, self._max_s = preroll_ms, tail_ms, float(max_s)
        self._lock = threading.Lock()
        self._rec: _Clip | None = None  # the press in progress, or its tail still coming
        self._done: deque[_Clip] = deque()  # whole clips not yet taken, oldest first
        self._format(_count(device_rate, "device_rate"), _count(channels, "channels"))

    @property
    def device_rate(self) -> int:
        return self._rate

    @property
    def channels(self) -> int:
        return self._channels

    @property
    def ready(self) -> bool:
        """A whole clip waits for take()."""
        with self._lock:
            return bool(self._done)

    def feed(self, block) -> None:
        """One block from the stream: (frames, channels) float32, a mono one flat or not.
        Raises ValueError for any other shape."""
        a = np.array(block, dtype=np.float32)  # a copy: PortAudio reuses its buffers
        if a.ndim == 1:
            a = a[:, None]
        with self._lock:
            if a.ndim != 2 or a.shape[1] != self._channels:
                raise ValueError(f"a block of {self._channels} channels, not of shape "
                                 f"{np.shape(block)}")
            self._remember(a)
            clip = self._rec
            if clip is not None and clip.add(a):
                self._finish(clip)

    def begin(self, now: float) -> None:
        """A Press: the clip starts with the whole ring. A press still recording, or a tail
        still coming in, is finished where the audio has got to."""
        with self._lock:
            if self._rec is not None:
                self._close_early(self._rec)
            seed = self._ring_audio()
            clip = _Clip(self._rate, seed=len(seed), limit=len(seed) + self._max_frames,
                         tail=self._tail_frames, began=now)
            if len(seed):
                clip.blocks.append(seed)
                clip.frames = len(seed)
            self._rec = clip

    def end(self, now: float) -> bool:
        """A Release, the key-up dated `now`: the clip ends tail_ms after it, or tail_ms of audio
        after where the stream has got to, whichever is earlier, so a late call never adds audio
        from after the key-up and a stalled stream never makes the clip reach past it. True when
        a clip is coming; False when the press was dropped (reset, discard), so take() will
        never give it."""
        with self._lock:
            clip = self._rec
            if clip is None:
                return bool(self._done)
            if clip.end is None:
                by_time = clip.seed + round(max(0.0, now - clip.began) * clip.rate) + clip.tail
                clip.end = min(clip.frames + clip.tail, by_time)  # frames < limit: max_s holds
                if clip.frames >= clip.end:
                    clip.cut()
                    self._finish(clip)
            return True

    def discard(self) -> None:
        with self._lock:
            self._rec = None
            self._done.clear()

    def take(self) -> np.ndarray | None:
        """The oldest whole clip, mono 16 kHz float32, once; None while there is none."""
        with self._lock:
            if not self._done:
                return None
            clip = self._done.popleft()
        audio = np.concatenate(clip.blocks)
        mono = audio.mean(axis=1, dtype=np.float32) if audio.shape[1] > 1 else audio[:, 0]
        if clip.rate != STT_RATE:
            mono = soxr.resample(mono, clip.rate, STT_RATE, quality="HQ")
        mono = np.ascontiguousarray(mono, dtype=np.float32)
        return mono if len(mono) else None

    def reset(self, *, device_rate: int | None = None, channels: int | None = None) -> None:
        """A break in the stream (it closed, or a new one opened), at a new format if given."""
        rate = None if device_rate is None else _count(device_rate, "device_rate")
        count = None if channels is None else _count(channels, "channels")
        with self._lock:
            clip = self._rec
            if clip is not None:
                if clip.end is not None:
                    self._close_early(clip)  # only its tail was still coming
                else:
                    self._rec = None  # a press across the break is dropped
            self._format(self._rate if rate is None else rate,
                         self._channels if count is None else count)

    # under the lock

    def _format(self, rate: int, channels: int) -> None:
        """An empty ring for the stream's format; the old one is overwritten first."""
        old = getattr(self, "_ring", None)
        if old is not None:
            old.fill(0.0)
        size = round(rate * self._preroll_ms / 1000)
        if old is None or old.shape != (size, channels):
            self._ring = np.zeros((size, channels), np.float32)
        self._rate, self._channels = rate, channels
        self._ring_at = 0  # where the next frame goes
        self._ring_fill = 0  # how many frames it holds
        self._tail_frames = round(rate * self._tail_ms / 1000)
        self._max_frames = round(rate * self._max_s)

    def _remember(self, a: np.ndarray) -> None:
        """Into the ring, over its oldest frames."""
        ring = self._ring
        size, n = len(ring), len(a)
        if size == 0:
            return
        if n >= size:
            ring[:] = a[n - size:]
            self._ring_at, self._ring_fill = 0, size
            return
        first = min(n, size - self._ring_at)
        ring[self._ring_at:self._ring_at + first] = a[:first]
        ring[:n - first] = a[first:]
        self._ring_at = (self._ring_at + n) % size
        self._ring_fill = min(size, self._ring_fill + n)

    def _ring_audio(self) -> np.ndarray:
        """The ring's frames, oldest first, as a copy."""
        ring = self._ring
        if self._ring_fill < len(ring):
            return ring[:self._ring_fill].copy()
        return np.concatenate([ring[self._ring_at:], ring[:self._ring_at]])

    def _close_early(self, clip: _Clip) -> None:
        clip.end = clip.frames
        self._finish(clip)

    def _finish(self, clip: _Clip) -> None:
        if self._rec is clip:
            self._rec = None
        if clip.frames:
            self._done.append(clip)


class Mic:
    def __init__(self, devices: DeviceApi, name: str, capture: Capture, *, clock=None):
        self._name = name
        self._capture = capture
        self._now = clock.now if clock is not None else time.monotonic
        self._opened = False
        self._formatted = False  # the capture has this stream's format: its blocks may pass
        self._feed_failures = 0  # counted on PortAudio's thread, logged at the next poll
        self._keeper = StreamKeeper(devices, "input", self._feed, label="mic",
                                    on_stream=self._on_stream)

    @property
    def ok(self) -> bool:
        return self._opened and bool(self._name) and self._keeper.ok

    @property
    def state(self) -> str:
        if not self._name:
            return "unset"
        if not self._opened:
            return "closed"
        return "ok" if self._keeper.ok else "missing"

    def open(self, now: float | None = None) -> None:
        """At session online: tried at once, then kept open by poll()."""
        self._opened = True
        self._keeper.fresh()
        self.poll(now)

    def close(self) -> None:
        """At session offline: the stream closes and the capture forgets its ring."""
        self._opened = False
        self._keeper.close("closed")

    def poll(self, now: float | None = None) -> None:
        """Reopen a stream that stopped, retry a missing device. Cheap while the stream runs, so
        the service may call it at every tick."""
        if not self._opened or not self._name:
            return
        now = self._now() if now is None else now
        if self._feed_failures:
            failures, self._feed_failures = self._feed_failures, 0
            log("mic_feed_failed", level=logging.WARNING, blocks=failures)
        self._keeper.keep(self._name, now)

    def _feed(self, block: np.ndarray) -> None:
        """The stream's callback, on PortAudio's thread: an error escaping it would abort the
        stream."""
        if not self._formatted:
            return
        try:
            self._capture.feed(block)
        except Exception:
            self._feed_failures += 1

    def _on_stream(self, stream: Stream | None) -> None:
        self._formatted = False
        if stream is None:
            self._capture.reset()
        else:
            self._capture.reset(device_rate=round(stream.samplerate), channels=stream.channels)
            self._formatted = True
