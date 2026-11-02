"""A short, distant household scene, made in memory on the synthesis thread.

The TV uses the ordinary base engine, never an expressive worker or a stock cache.
Every call owns its DSP state; no device, file, model loader or shared plugin is opened.
"""
from dataclasses import replace

import numpy as np
import pedalboard
from scipy.signal import butter, sosfilt

from . import fx
from .decision import Decision
from .voices import BASES

BEDS = ("room", "tv", "kettle")
TV_LINE = "We'll be right back after the break."
RATE = 48000
LEAD_S = 0.5
MAX_S = 6.0
MIN_S = 2.0
CLICK_S = 0.007


def _band(audio, rate, low, high):
    sos = butter(6, (low, high), btype="bandpass", fs=rate, output="sos")
    return np.asarray(sosfilt(sos, audio), dtype=np.float32)


def _tv(spec, rng, engine, bank):
    choices = [name for name in bank.names() if name in BASES
               and name not in spec.timbre and name not in spec.prosody]
    if not choices:
        return np.zeros(0, dtype=np.float32)
    base = str(rng.choice(choices))
    television = replace(spec, guid=2147483647, name="Television", engine="kokoro", ref="",
                         lang=BASES[base][1], timbre={base: 1.0}, prosody={}, speed=1.0,
                         pitch_st=0.0, gain_db=0.0, locked=False)
    style = bank.style(television, "none", Decision())
    audio = fx._mono(engine.synth(TV_LINE, television, style, 1.0))
    audio = fx.normalize_loudness(audio, engine.sample_rate)
    return _band(fx.to_48k(audio, engine.sample_rate), RATE, 300, 3000) * np.float32(0.1)


def render_hotmic(line: np.ndarray, sr: int, spec, *, rng, engine, bank) -> np.ndarray:
    """Return a 2-6 second mono float32 scene at 48 kHz, never cached."""
    if type(sr) is not int or sr <= 0:
        raise ValueError("hotmic: invalid sample rate")
    audio = fx._mono(line)
    if not len(audio):
        raise ValueError("hotmic: empty line")
    # Pitch is the member's; distance replaces the usual communications chain.
    audio = fx.shift_pitch(audio, sr, spec.pitch_st)
    audio = fx.to_48k(fx.normalize_loudness(audio, sr), sr)
    length = round(min(MAX_S, max(MIN_S, LEAD_S + len(audio) / RATE + 0.15 + CLICK_S)) * RATE)
    lead, click = round(LEAD_S * RATE), round(CLICK_S * RATE)
    capacity = length - lead - click
    speech = np.pad(audio[:capacity], (0, max(0, capacity - len(audio))))
    reverb = pedalboard.Reverb(room_size=0.3, wet_level=0.25, dry_level=1.0, width=0.0)
    speech = np.asarray(reverb(speech, RATE), dtype=np.float32).reshape(-1)
    speech = sosfilt(butter(8, 2500, fs=RATE, output="sos"), speech).astype(np.float32)
    speech *= np.float32(10 ** (-10 / 20))
    fade = min(round(0.005 * RATE), len(speech))
    speech[-fade:] *= np.linspace(1, 0, fade, dtype=np.float32)
    result = fx.room_tone(length, RATE, spec.mic, 3, seed=int(rng.integers(0, 2**31)))
    result[lead:lead + len(speech)] += speech
    bed = str(rng.choice(BEDS))
    if bed == "tv":
        television = _tv(spec, rng, engine, bank)[:capacity]
        result[lead:lead + len(television)] += television
    elif bed == "kettle":
        kettle = _band(rng.standard_normal(capacity), RATE, 900, 3000)
        rms = float(np.sqrt(np.mean(kettle.astype(np.float64) ** 2)))
        if rms:
            rise = np.minimum(np.arange(capacity, dtype=np.float32) / (3 * RATE), 1)
            result[lead:lead + capacity] += kettle * (10 ** (-45 / 20) / rms) * rise
    # The release is recognisably a key-up: a 2 ms transient followed by 5 ms of noise.
    transient = round(0.002 * RATE)
    result[-click:] = 0
    result[-click:-click + transient] = 0.05 * np.hanning(transient) * np.where(
        np.arange(transient) < transient // 2, 1, -1)
    result[-click + transient:] = 0.008 * rng.standard_normal(click - transient)
    if not np.isfinite(result).all():
        raise ValueError("hotmic: nonfinite DSP output")
    peak = float(np.abs(result).max())
    if peak > 0.97:
        result *= np.float32(0.97 / peak)
    return np.ascontiguousarray(result, dtype=np.float32)
