"""The 2009 sound (draft section 5): each member's mic and codec profile, scaled by the one
comms_grit knob (0 studio clean, 10 full 2009), a room tone, loudness matching and the move to
the mixer's 48 kHz.

process() takes a dry clip at the engine's rate (a fresh render or a stock clip) through:
1. the spec's pitch shift (Rubber Band through pedalboard, formants kept);
2. loudness to the target, so the chain's compressor, clipping and codec meet speech at one level
   whatever the engine's output level;
3. the mic chain (chain_for);
4. loudness to the target again (the draft's -20 LUFS: pyloudnorm's BS.1770 meter on 400 ms
   blocks for a clip of 400 ms or more, its RMS below that);
5. the room tone, unless the clip is silent or a voice whisper;
6. resampling to 48 kHz (soxr).

The chain for a profile at grit g, with a = g / 10, in signal order: a high-pass at
80 + a(hp_hz - 80) Hz; the presence peak, a * peak_db at peak_hz; Clipping at clip_db from grit
7 (a hot preamp; clip_db 0 is a mic that never clips); a compressor of ratio 1 + a(comp_ratio - 1);
the codec; and the band limit, a low-pass at 12000 - a(12000 - lp_hz) Hz. The codec (I5, the
owner's light grit: band-limited mics, a mild codec): an mp3 profile carries LAME from grit 1 at
VBR quality 4 + (g - 1)/9 (mp3_vbr - 4), mild at the default grit 3 (pedalboard's MP3Compressor
takes LAME's VBR quality, 0 best to 10 worst, not a bitrate); a gsm profile carries GSM 06.10 full
rate, Ventrilo's default codec, from grit 5, then a 20 Hz DC blocker (its decoder turns silence
into an offset at -66 dBFS). Grit 0 is a bare 80 Hz high-pass. A plugin that would do nothing
(a 0 dB peak, a 1:1 compressor, a low-pass at 12 kHz) is left out.

pedalboard's LowpassFilter is first order, 6 dB an octave (9 dB down above 3.4 kHz for a 3.4 kHz
edge, measured): too soft for a band limit. So the low-pass is Resample down to twice its edge
and back, whose anti-aliasing filter is flat below the edge and steep above it (measured: 0 dB to
3 kHz and 37 dB down above 3.4 kHz). It comes last, so it also takes what the codec and the
clipping leave above the band.

The room tone is pink noise in the mic's band at room_db - 20(1 - a) dBFS, under speech at the
target loudness: the mic's open channel, quiet. A voice whisper skips the chain and the room: a
100 Hz high-pass and the loudness step only, dry and close."""
import math
from dataclasses import dataclass

import numpy as np
import pedalboard
import pyloudnorm
import soxr

OUT_RATE = 48000  # the mixer's
TARGET_LUFS = -20.0
LOUDNESS_BLOCK_S = 0.4  # pyloudnorm's gating block; a shorter clip is measured by its RMS
ABSOLUTE_GATE_DB = -70.0  # BS.1770's absolute gate, the RMS fallback's too: quieter is silence
GRIT_MAX = 10
TOP_HZ = 12000.0  # the low-pass at grit 0: none, at the engines' 24 kHz
BOTTOM_HZ = 80.0  # the high-pass at grit 0
WHISPER_HP_HZ = 100.0
PRESENCE_Q = 0.7071  # pedalboard's default: a broad bump
COMP_THRESHOLD_DB = -24.0  # under speech at the target, so the compressor works on every word
COMP_ATTACK_MS = 5.0
COMP_RELEASE_MS = 80.0
CLIP_FROM_GRIT = 7
MP3_FROM_GRIT = 1
GSM_FROM_GRIT = 5
MP3_MILD_VBR = 4.0  # the VBR quality at grit 1; an mp3 profile's mp3_vbr is its quality at 10
DC_BLOCK_HZ = 20.0
CODECS = ("none", "mp3", "gsm")


@dataclass(frozen=True, slots=True)
class MicProfile:
    hp_hz: float
    lp_hz: float
    peak_hz: float
    peak_db: float
    codec: str  # none, mp3 or gsm
    mp3_vbr: float  # LAME's VBR quality at grit 10, 0 best to 10 worst; an mp3 profile's only
    comp_ratio: float
    clip_db: float  # 0: never clips
    room_db: float  # the room tone at grit 10, dBFS

    def __post_init__(self):
        if self.codec not in CODECS:
            raise ValueError(f"codec: expected one of {', '.join(CODECS)}, got {self.codec!r}")
        if not BOTTOM_HZ <= self.hp_hz < self.lp_hz <= TOP_HZ:
            raise ValueError(f"hp_hz, lp_hz: expected {BOTTOM_HZ:g} <= hp_hz < lp_hz <= "
                             f"{TOP_HZ:g}, got {self.hp_hz:g} and {self.lp_hz:g}")
        if not 0 <= self.mp3_vbr <= 10:
            raise ValueError(f"mp3_vbr: expected LAME's VBR quality 0-10, got {self.mp3_vbr:g}")
        if self.comp_ratio < 1 or self.clip_db > 0 or self.room_db > 0:
            raise ValueError("comp_ratio, clip_db, room_db: expected a ratio of 1 or more and "
                             "levels at or under 0 dBFS")


# high-pass, low-pass, presence (one 2 kHz peak for every mic), codec, compression ratio; the
# room at -55 dBFS unless stated (at grit 10). A profile without mp3 keeps the mild base as its
# mp3_vbr, unused.
MIC_PROFILES: dict[str, MicProfile] = {
    "desk_mic": MicProfile(120, 8000, 2000, 1, "none", MP3_MILD_VBR, 2.0, 0.0, -55),
    "usb_headset": MicProfile(150, 6000, 2000, 2, "mp3", 7.0, 3.0, 0.0, -55),
    "headset_cheap": MicProfile(300, 3400, 2000, 5, "gsm", MP3_MILD_VBR, 4.0, 0.0, -55),
    "gaming_headset": MicProfile(150, 7000, 2000, 3, "none", MP3_MILD_VBR, 3.0, 0.0, -55),
    "laptop": MicProfile(200, 5000, 2000, 4, "mp3", 8.5, 3.5, 0.0, -55),
    "webcam": MicProfile(250, 4500, 2000, 4, "gsm", MP3_MILD_VBR, 3.5, 0.0, -55),
    "hot_mic": MicProfile(120, 7000, 2000, 3, "none", MP3_MILD_VBR, 4.0, -3.0, -55),
    "soft_mic": MicProfile(150, 6000, 2000, 0, "none", MP3_MILD_VBR, 2.0, 0.0, -50),
}


def _profile(name) -> MicProfile:
    p = MIC_PROFILES.get(name) if isinstance(name, str) else None
    if p is None:
        raise ValueError(f"mic: expected one of {', '.join(MIC_PROFILES)}, got {name!r}")
    return p


def _grit(grit) -> int:
    if type(grit) is not int or not 0 <= grit <= GRIT_MAX:
        raise ValueError(f"grit: expected a whole number 0 to {GRIT_MAX}, got {grit!r}")
    return grit


def _mono(audio) -> np.ndarray:
    x = np.asarray(audio, dtype=np.float32)
    if x.ndim != 1:
        raise ValueError(f"audio: expected mono samples, one dimension; got shape {x.shape}")
    if not np.isfinite(x).all():
        raise ValueError("audio: expected finite samples, got NaN or infinity")
    return x


def _band(p: MicProfile, grit: int) -> tuple[float, float]:
    """The mic's high-pass and low-pass edges at this grit."""
    a = grit / GRIT_MAX
    return BOTTOM_HZ + a * (p.hp_hz - BOTTOM_HZ), TOP_HZ - a * (TOP_HZ - p.lp_hz)


def _codec(p: MicProfile, grit: int) -> list:
    if p.codec == "mp3" and grit >= MP3_FROM_GRIT:
        vbr = MP3_MILD_VBR + (grit - 1) / 9 * (p.mp3_vbr - MP3_MILD_VBR)
        return [pedalboard.MP3Compressor(vbr_quality=vbr)]
    if p.codec == "gsm" and grit >= GSM_FROM_GRIT:  # its decoder leaves a DC offset (-66 dBFS)
        return [pedalboard.GSMFullRateCompressor(),
                pedalboard.HighpassFilter(cutoff_frequency_hz=DC_BLOCK_HZ)]
    return []


def chain_for(profile: str, grit: int, *, whisper: bool) -> pedalboard.Pedalboard:
    """The mic chain of `profile` (a MIC_PROFILES name) at `grit` (0-10); a voice whisper's is a
    100 Hz high-pass alone. A new board each call: a board is not shared between threads."""
    p, grit = _profile(profile), _grit(grit)
    if whisper:
        return pedalboard.Pedalboard([pedalboard.HighpassFilter(cutoff_frequency_hz=WHISPER_HP_HZ)])
    a = grit / GRIT_MAX
    hp, lp = _band(p, grit)
    board = [pedalboard.HighpassFilter(cutoff_frequency_hz=hp)]
    if a * p.peak_db:
        board.append(pedalboard.PeakFilter(cutoff_frequency_hz=p.peak_hz, gain_db=a * p.peak_db,
                                           q=PRESENCE_Q))
    if grit >= CLIP_FROM_GRIT and p.clip_db < 0:
        board.append(pedalboard.Clipping(threshold_db=p.clip_db))
    ratio = 1 + a * (p.comp_ratio - 1)
    if ratio > 1:
        board.append(pedalboard.Compressor(threshold_db=COMP_THRESHOLD_DB, ratio=ratio,
                                           attack_ms=COMP_ATTACK_MS, release_ms=COMP_RELEASE_MS))
    board += _codec(p, grit)
    if lp < TOP_HZ:
        board.append(pedalboard.Resample(target_sample_rate=2 * lp,
                                         quality=pedalboard.Resample.Quality.WindowedSinc32))
    return pedalboard.Pedalboard(board)


def change_tempo(audio, sr: int, speed: float) -> np.ndarray:
    """Change speaking speed without shifting pitch or formants; 1.0 keeps every sample.

    Engines without a native speed control use this before the mic chain. A speed above
    one shortens the clip. Do not pad it back to its original length.
    """
    x = _mono(audio)
    if not math.isfinite(speed) or speed <= 0:
        raise ValueError("speed: expected a finite positive number")
    if speed == 1.0 or not len(x):
        return x.copy()
    y = pedalboard.time_stretch(x, sr, float(speed), preserve_formants=True)
    return _mono(np.asarray(y, dtype=np.float32).reshape(-1))


def shift_pitch(audio, sr: int, semitones: float) -> np.ndarray:
    """`audio` `semitones` higher (lower when negative), its length and formants kept."""
    x = _mono(audio)
    if not math.isfinite(semitones):
        raise ValueError(f"semitones: expected a finite number, got {semitones!r}")
    if semitones == 0 or not len(x):
        return x.copy()
    y = pedalboard.time_stretch(x, sr, 1.0, pitch_shift_in_semitones=float(semitones),
                                preserve_formants=True)
    y = np.asarray(y, dtype=np.float32).reshape(-1)[:len(x)]
    return np.pad(y, (0, len(x) - len(y)))  # Rubber Band may hand a sample or so back short


def loudness(audio, sr: int) -> float:
    """The clip's integrated loudness in LUFS (400 ms blocks, gated) when it lasts 400 ms or
    more, else its RMS in dBFS; -inf when it is silent (under the -70 dB gate)."""
    x = _mono(audio)
    if len(x) >= LOUDNESS_BLOCK_S * sr:  # pyloudnorm's own test, so the two never disagree
        return float(pyloudnorm.Meter(sr, block_size=LOUDNESS_BLOCK_S).integrated_loudness(x))
    mean_square = float(np.mean(np.square(x, dtype=np.float64))) if len(x) else 0.0
    level = 10 * math.log10(mean_square) if mean_square > 0 else -math.inf
    return level if level >= ABSOLUTE_GATE_DB else -math.inf


def _gained(x: np.ndarray, level: float, target: float) -> np.ndarray:
    if not math.isfinite(level):
        return x.copy()
    return x * np.float32(10 ** ((target - level) / 20))


def normalize_loudness(audio, sr: int, target_lufs: float = TARGET_LUFS) -> np.ndarray:
    """`audio` at `target_lufs` (dBFS RMS under 400 ms); a silent clip comes back as it was."""
    x = _mono(audio)
    return _gained(x, loudness(x, sr), target_lufs)


def room_tone(n: int, sr: int, profile: str, grit: int, *, seed: int) -> np.ndarray:
    """`n` samples of the mic's room: pink noise in the mic's band at this grit, at an RMS of
    room_db - 20(1 - grit/10) dBFS. The same seed gives the same noise."""
    p, grit = _profile(profile), _grit(grit)
    if n < 2:
        return np.zeros(n, dtype=np.float32)
    lo, hi = _band(p, grit)
    spectrum = np.fft.rfft(np.random.default_rng(seed).standard_normal(n))
    f = np.fft.rfftfreq(n, 1 / sr)
    inside = (f >= lo) & (f <= hi)
    spectrum[~inside] = 0
    spectrum[inside] /= np.sqrt(f[inside])  # pink: power falls 3 dB an octave
    noise = np.fft.irfft(spectrum, n)
    rms = math.sqrt(float(np.mean(np.square(noise))))
    if rms == 0:
        return np.zeros(n, dtype=np.float32)
    level_db = p.room_db - 20 * (1 - grit / GRIT_MAX)
    return (noise * (10 ** (level_db / 20) / rms)).astype(np.float32)


def to_48k(audio, sr: int) -> np.ndarray:
    x = _mono(audio)
    if sr == OUT_RATE:
        return x.copy()
    return np.asarray(soxr.resample(x, sr, OUT_RATE, quality="HQ"), dtype=np.float32)


def process(audio, sr: int, spec, *, grit: int, whisper: bool) -> np.ndarray:
    """A dry clip at `sr` as the mixer plays it: mono float32 at 48 kHz, through the spec's pitch
    and mic (a voice whisper through the whisper chain), at the target loudness. Raises
    ValueError for a mic profile, grit or clip it cannot take."""
    x = _mono(audio)
    board = chain_for(spec.mic, grit, whisper=whisper)  # the mic and grit checked first
    x = shift_pitch(x, sr, spec.pitch_st)
    level = loudness(x, sr)
    if not math.isfinite(level):  # nothing to say: no room either
        return to_48k(np.zeros(len(x), dtype=np.float32), sr)
    x = _gained(x, level, TARGET_LUFS)
    x = normalize_loudness(np.asarray(board(x, sr), dtype=np.float32).reshape(-1), sr)
    if not whisper:
        x = x + room_tone(len(x), sr, spec.mic, grit, seed=spec.guid)
    return to_48k(x, sr)
