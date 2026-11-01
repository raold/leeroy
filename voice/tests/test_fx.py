"""The mic and codec chains, the room tone, loudness matching, the pitch shift and the 48 kHz
output, on signals made in code: white noise and sine tones."""
import numpy as np
import pedalboard
import pyloudnorm
import pytest
from scipy.signal import lfilter

from conftest import BANK_SHA
from leeroy_voice import fx
from leeroy_voice.voices import spec_from_dict

SR = 24000  # the engines' rate
OUT = 48000
CODECS = (pedalboard.MP3Compressor, pedalboard.GSMFullRateCompressor)


def spec(**kw):
    return spec_from_dict({"version": 1, "guid": 101, "name": "Arkon", "engine": "kokoro",
                           "lang": "a", "timbre": {"am_michael": 1.0}, "prosody": {},
                           "speed": 1.0, "pitch_st": 0.0, "mic": "headset_cheap", "gain_db": 0.0,
                           "locked": False, "voices_sha256": BANK_SHA, "ref": "", **kw})


def noise(seconds: float, rms: float = 0.01, seed: int = 5) -> np.ndarray:
    """White noise; at the default 0.01 (-40 dBFS) it stays under the compressor's threshold,
    so a chain's filters and codec are all that act on it."""
    n = round(seconds * SR)
    return (rms * np.random.default_rng(seed).standard_normal(n)).astype(np.float32)


def tone(hz: float, seconds: float, amp: float = 0.25) -> np.ndarray:
    t = np.arange(round(seconds * SR)) / SR
    return (amp * np.sin(2 * np.pi * hz * t)).astype(np.float32)


def band(x, lo: float, hi: float, sr: int = SR) -> float:
    """The energy of x from lo to hi Hz, in dB."""
    power = np.abs(np.fft.rfft(np.asarray(x, np.float64))) ** 2
    f = np.fft.rfftfreq(len(x), 1 / sr)
    return float(10 * np.log10(power[(f >= lo) & (f < hi)].sum() + 1e-30))


def drop(x, y, lo: float, hi: float, sr: int = SR) -> float:
    """How far the chain took the band down, in dB (positive: quieter)."""
    return band(x, lo, hi, sr) - band(y, lo, hi, sr)


def rms_db(x) -> float:
    return float(10 * np.log10(np.mean(np.square(np.asarray(x, np.float64))) + 1e-30))


def peak_hz(x, sr: int) -> float:
    mag = np.log(np.abs(np.fft.rfft(np.asarray(x, np.float64) * np.hanning(len(x)))) + 1e-30)
    k = int(np.argmax(mag[1:])) + 1
    a, b, c = mag[k - 1:k + 2]
    return (k + 0.5 * (a - c) / (a - 2 * b + c)) * sr / len(x)


def codecs(board) -> list[type]:
    return [type(p) for p in board if isinstance(p, CODECS)]


def test_grit_zero_is_nearly_clean():
    x = noise(2.0)
    y = fx.chain_for("headset_cheap", 0, whisper=False)(x, SR)
    assert abs(drop(x, y, 4000, 6000)) <= 1.0


def test_grit_ten_band_limits():
    x = noise(2.0)
    y = fx.chain_for("headset_cheap", 10, whisper=False)(x, SR)
    assert drop(x, y, 3400, SR / 2) >= 30.0


def test_grit_scales_between():
    x = noise(2.0)
    at = {g: drop(x, fx.chain_for("headset_cheap", g, whisper=False)(x, SR), 3400, SR / 2)
          for g in (3, 10)}
    assert 0 < at[3] < at[10]


@pytest.mark.parametrize("mic, grit", [("headset_cheap", 3), ("gaming_headset", 5),
                                       ("desk_mic", 7), ("hot_mic", 8), ("soft_mic", 10)])
def test_the_low_pass_moves_with_the_grit(mic, grit):   # its edge at 12000 - a(12000 - lp_hz)
    p, x = fx.MIC_PROFILES[mic], noise(2.0)
    edge = 12000 - grit / 10 * (12000 - p.lp_hz)
    y = fx.chain_for(mic, grit, whisper=False)(x, SR)
    assert abs(drop(x, y, 0.5 * edge, 0.9 * edge)) <= 1.5
    assert drop(x, y, 1.1 * edge, SR / 2) >= 20.0


@pytest.mark.parametrize("grit", [1, 3, 7, 10])
@pytest.mark.parametrize("mic", list(fx.MIC_PROFILES))
def test_the_chain_scales_with_the_grit(mic, grit):     # the brief's scaling, plugin by plugin
    p, a = fx.MIC_PROFILES[mic], grit / 10
    board = {}
    for plugin in fx.chain_for(mic, grit, whisper=False):
        board.setdefault(type(plugin), plugin)          # the mic's high-pass, not GSM's DC blocker
    assert board[pedalboard.HighpassFilter].cutoff_frequency_hz == pytest.approx(
        80 + a * (p.hp_hz - 80), rel=1e-5)
    peak = board.get(pedalboard.PeakFilter)
    if p.peak_db:
        assert peak.gain_db == pytest.approx(a * p.peak_db, abs=1e-5)
        assert peak.cutoff_frequency_hz == p.peak_hz
    else:
        assert peak is None
    assert board[pedalboard.Compressor].ratio == pytest.approx(1 + a * (p.comp_ratio - 1),
                                                               rel=1e-5)
    assert board[pedalboard.Resample].target_sample_rate == pytest.approx(
        2 * (12000 - a * (12000 - p.lp_hz)), rel=1e-5)


@pytest.mark.parametrize("mic", list(fx.MIC_PROFILES))
def test_every_profile_band_limits_at_its_edge(mic):   # the table's edges reach the sound
    p, x = fx.MIC_PROFILES[mic], noise(2.0)
    assert abs(drop(x, fx.chain_for(mic, 0, whisper=False)(x, SR), 4000, 6000)) <= 1.0
    rough = fx.chain_for(mic, 10, whisper=False)(x, SR)
    assert drop(x, rough, p.lp_hz, SR / 2) >= 30.0
    assert drop(x, rough, 40, p.hp_hz / 2) >= 4.0     # a first-order high-pass an octave down


def test_mild_codec_at_light_grit():                    # I5
    mp3 = [m for m, p in fx.MIC_PROFILES.items() if p.codec == "mp3"]
    gsm = [m for m, p in fx.MIC_PROFILES.items() if p.codec == "gsm"]
    assert mp3 and gsm
    for mic in mp3:
        assert codecs(fx.chain_for(mic, 1, whisper=False)) == [pedalboard.MP3Compressor]
        assert codecs(fx.chain_for(mic, 3, whisper=False)) == [pedalboard.MP3Compressor]
    for mic in gsm:
        assert codecs(fx.chain_for(mic, 3, whisper=False)) == []
        assert codecs(fx.chain_for(mic, 4, whisper=False)) == []
        assert codecs(fx.chain_for(mic, 5, whisper=False)) == [pedalboard.GSMFullRateCompressor]
        assert codecs(fx.chain_for(mic, 10, whisper=False)) == [pedalboard.GSMFullRateCompressor]
    for mic, p in fx.MIC_PROFILES.items():
        assert codecs(fx.chain_for(mic, 0, whisper=False)) == []
        if p.codec == "none":
            assert all(codecs(fx.chain_for(mic, g, whisper=False)) == [] for g in range(11))


@pytest.mark.parametrize("mic", ["headset_cheap", "webcam"])
def test_gsm_leaves_no_offset(mic):                     # its decoder turns silence into DC
    out = fx.chain_for(mic, 10, whisper=False)(np.zeros(SR, np.float32), SR)
    assert rms_db(out[SR // 10:]) < -80 and abs(float(np.mean(out[SR // 10:]))) < 1e-5


@pytest.mark.parametrize("mic, grit, vbr", [
    ("usb_headset", 1, 4.0), ("usb_headset", 3, 4.0 + 2 / 9 * 3.0), ("usb_headset", 10, 7.0),
    ("laptop", 1, 4.0), ("laptop", 3, 5.0), ("laptop", 10, 8.5),
])
def test_mp3_quality_follows_the_grit(mic, grit, vbr):  # LAME's VBR quality, not a bitrate
    (mp3,) = [p for p in fx.chain_for(mic, grit, whisper=False)
              if isinstance(p, pedalboard.MP3Compressor)]
    assert mp3.vbr_quality == pytest.approx(vbr, abs=1e-5)


def test_whisper_skips_the_codec():
    x, s = noise(2.0, rms=0.05), spec(mic="headset_cheap")
    whisper = fx.process(x, SR, s, grit=10, whisper=True)
    clean = fx.process(x, SR, s, grit=0, whisper=False)
    rough = fx.process(x, SR, s, grit=10, whisper=False)
    assert abs(band(whisper, 5500, 6500, OUT) - band(clean, 5500, 6500, OUT)) <= 3.0
    assert band(rough, 5500, 6500, OUT) < band(clean, 5500, 6500, OUT) - 30   # the same mic
    for mic in fx.MIC_PROFILES:
        board = fx.chain_for(mic, 10, whisper=True)
        assert codecs(board) == [] and [type(p) for p in board] == [pedalboard.HighpassFilter]


def test_loudness_target():
    x = noise(2.0, rms=0.2)
    meter = pyloudnorm.Meter(SR)
    assert meter.integrated_loudness(fx.normalize_loudness(x, SR)) == pytest.approx(-20, abs=0.5)
    gap = np.zeros(SR // 2, np.float32)                 # gated: the silence around it counts not
    burst = fx.normalize_loudness(np.concatenate([gap, x, gap]), SR)
    assert meter.integrated_loudness(burst) == pytest.approx(-20.0, abs=0.5)
    other = fx.normalize_loudness(x, SR, target_lufs=-26.0)
    assert meter.integrated_loudness(other) == pytest.approx(-26.0, abs=0.5)


def test_short_clip_rms_fallback():                     # under pyloudnorm's 400 ms block
    y = fx.normalize_loudness(tone(220, 0.3, amp=0.6), SR)
    assert rms_db(y) == pytest.approx(-20.0, abs=0.5)
    exact = fx.normalize_loudness(noise(0.4, rms=0.3), SR)          # 400 ms: the meter
    assert pyloudnorm.Meter(SR).integrated_loudness(exact) == pytest.approx(-20.0, abs=0.5)


def test_silence_is_left_alone():                       # no gain, NaN or warning from nothing
    for n in (0, 100, SR):
        assert np.array_equal(fx.normalize_loudness(np.zeros(n, np.float32), SR), np.zeros(n))
    faint = tone(220, 0.3, amp=1e-4)                    # -83 dBFS: under the -70 gate
    assert np.array_equal(fx.normalize_loudness(faint, SR), faint)
    out = fx.process(np.zeros(SR, np.float32), SR, spec(), grit=10, whisper=False)
    assert out.dtype == np.float32 and len(out) == 2 * SR and not out.any()   # no room tone
    assert len(fx.process(np.zeros(0, np.float32), SR, spec(), grit=3, whisper=False)) == 0


@pytest.mark.parametrize("speed", [0.9, 1.08, 1.15])
def test_tempo_changes_duration_without_changing_pitch(speed):
    x = tone(220, 2.0)
    y = fx.change_tempo(x, SR, speed)
    assert y.dtype == np.float32 and np.isfinite(y).all()
    assert len(y) == pytest.approx(len(x) / speed, abs=2)
    assert peak_hz(y[len(y) // 4:3 * len(y) // 4], SR) == pytest.approx(220, abs=2)


def test_tempo_unity_is_lossless_and_empty_stays_empty():
    x = tone(220, 0.5)
    assert np.array_equal(fx.change_tempo(x, SR, 1.0), x)
    assert fx.change_tempo([], SR, 0.9).shape == (0,)


@pytest.mark.parametrize("speed", [0, -1, np.nan, np.inf])
def test_tempo_rejects_invalid_speed(speed):
    with pytest.raises(ValueError, match="speed"):
        fx.change_tempo(tone(220, 0.1), SR, speed)


def test_pitch_shift():
    x = tone(220, 2.0)
    up = fx.shift_pitch(x, SR, 1.0)
    assert up.dtype == np.float32 and len(up) == len(x)
    mid = slice(len(x) // 4, 3 * len(x) // 4)
    assert peak_hz(up[mid], SR) == pytest.approx(233.0, abs=2)
    assert peak_hz(fx.shift_pitch(x, SR, -2.0)[mid], SR) == pytest.approx(196.0, abs=2)
    assert np.array_equal(fx.shift_pitch(x, SR, 0.0), x)
    assert len(fx.shift_pitch(x[:1], SR, 1.0)) == 1     # Rubber Band gives one sample back as none


def vowel(formant: float, f0: float = 120.0, seconds: float = 2.0) -> np.ndarray:
    """A pulse train at f0 through one resonance at `formant` Hz: a vowel's shape, made in code."""
    pulses = np.zeros(round(seconds * SR))
    pulses[::round(SR / f0)] = 1.0
    r, theta = np.exp(-np.pi * 100.0 / SR), 2 * np.pi * formant / SR
    y = lfilter([1 - r], [1, -2 * r * np.cos(theta), r * r], pulses)
    return (0.3 * y / np.abs(y).max()).astype(np.float32)


def centroid(x, lo: float = 500.0, hi: float = 3000.0) -> float:
    power = np.abs(np.fft.rfft(np.asarray(x, np.float64) * np.hanning(len(x)))) ** 2
    f = np.fft.rfftfreq(len(x), 1 / SR)
    band = (f >= lo) & (f < hi)
    return float((f[band] * power[band]).sum() / power[band].sum())


def test_pitch_shift_keeps_the_formants():              # the voice moves, its vowel shape stays
    v, mid = vowel(1200.0), slice(SR // 2, 3 * SR // 2)
    moved = centroid(fx.shift_pitch(v, SR, 2.0)[mid]) / centroid(v[mid]) - 1
    assert abs(moved) < 0.5 * (2 ** (2 / 12) - 1)       # measured: 3% kept, 12% when not


def test_process_shifts_the_spec_pitch():               # pitch first, at the engine's rate
    out = fx.process(tone(220, 2.0), SR, spec(pitch_st=1.0), grit=0, whisper=True)
    assert peak_hz(out[OUT // 2: 3 * OUT // 2], OUT) == pytest.approx(233.0, abs=2)


@pytest.mark.parametrize("mic", ["hot_mic", "laptop", "headset_cheap"])
def test_the_chain_hears_every_engine_alike(mic):       # loud or quiet, it meets one level
    x = np.concatenate([tone(220, 0.5, amp=0.5), tone(220, 0.5, amp=0.1)])   # a loud, a soft word
    loud = fx.process(x, SR, spec(mic=mic), grit=10, whisper=False)
    soft = fx.process(x * np.float32(0.05), SR, spec(mic=mic), grit=10, whisper=False)
    assert np.allclose(loud, soft, atol=1e-3)


@pytest.mark.parametrize("grit", [0, 3, 10])
@pytest.mark.parametrize("mic", list(fx.MIC_PROFILES))
def test_output_is_48k_float32(fake_engine, mic, grit):
    s = spec(mic=mic)
    dry = fake_engine.synth("Pulling in three.", s, None, 1.0)
    for whisper in (False, True):
        out = fx.process(dry, fake_engine.sample_rate, s, grit=grit, whisper=whisper)
        assert out.dtype == np.float32 and out.ndim == 1 and len(out) == 2 * len(dry)
        assert np.isfinite(out).all()
        assert pyloudnorm.Meter(OUT).integrated_loudness(out) == pytest.approx(-20.0, abs=0.5)
    assert np.array_equal(fx.to_48k(dry, OUT), dry) and len(fx.to_48k(dry, SR)) == 2 * len(dry)


@pytest.mark.parametrize("mic", ["desk_mic", "soft_mic", "headset_cheap"])
@pytest.mark.parametrize("grit", [0, 3, 10])
def test_room_tone_under_the_line(mic, grit):            # room_db - 20(1 - grit/10) dBFS
    gap = np.zeros(SR // 2, np.float32)
    out = fx.process(np.concatenate([gap, tone(220, 1.0), gap]), SR, spec(mic=mic), grit=grit,
                     whisper=False)
    before = out[:OUT // 2 - OUT // 20]                 # clear of the line's own onset
    p = fx.MIC_PROFILES[mic]
    assert rms_db(before) == pytest.approx(p.room_db - 20 * (1 - grit / 10), abs=1.5)
    dry = fx.process(np.concatenate([gap, tone(220, 1.0), gap]), SR, spec(mic=mic), grit=grit,
                     whisper=True)
    assert rms_db(dry[:OUT // 2 - OUT // 20]) < -100    # a whisper is dry: no room


def test_room_tone_follows_the_band():
    quiet = fx.room_tone(2 * SR, SR, "headset_cheap", 10, seed=101)
    assert quiet.dtype == np.float32 and len(quiet) == 2 * SR
    assert rms_db(quiet) == pytest.approx(-55.0, abs=0.1)
    assert band(quiet, 3600, SR / 2) < band(quiet, 300, 3400) - 60
    assert np.array_equal(quiet, fx.room_tone(2 * SR, SR, "headset_cheap", 10, seed=101))


def test_the_eight_profiles():                          # the brief's table
    want = {  # high-pass, low-pass, presence dB, codec, mp3 VBR, ratio, clip dB, room dBFS
        "desk_mic": (120, 8000, 1, "none", None, 2.0, 0.0, -55),
        "usb_headset": (150, 6000, 2, "mp3", 7.0, 3.0, 0.0, -55),
        "headset_cheap": (300, 3400, 5, "gsm", None, 4.0, 0.0, -55),
        "gaming_headset": (150, 7000, 3, "none", None, 3.0, 0.0, -55),
        "laptop": (200, 5000, 4, "mp3", 8.5, 3.5, 0.0, -55),
        "webcam": (250, 4500, 4, "gsm", None, 3.5, 0.0, -55),
        "hot_mic": (120, 7000, 3, "none", None, 4.0, -3.0, -55),
        "soft_mic": (150, 6000, 0, "none", None, 2.0, 0.0, -50),
    }
    assert list(fx.MIC_PROFILES) == list(want)
    for mic, (hp, lp, peak, codec, vbr, ratio, clip, room) in want.items():
        p = fx.MIC_PROFILES[mic]
        got = (p.hp_hz, p.lp_hz, p.peak_db, p.codec, p.mp3_vbr if codec == "mp3" else None,
               p.comp_ratio, p.clip_db, p.room_db)
        assert got == (hp, lp, peak, codec, vbr, ratio, clip, room), mic
        assert p.peak_hz == 2000


def test_the_compressor_squeezes_a_loud_word():        # two words 20 dB apart through desk_mic
    x = np.concatenate([tone(220, 0.5, amp=10 ** (-30 / 20)),       # peaks at -30 dBFS, then
                        tone(220, 0.5, amp=10 ** (-10 / 20))])      # -10 dBFS
    def levels(grit):
        y = fx.chain_for("desk_mic", grit, whisper=False)(x, SR)
        return rms_db(y[SR // 10:SR // 2]), rms_db(y[SR // 2 + SR // 10:])
    (soft0, loud0), (soft10, loud10) = levels(0), levels(10)
    assert loud0 - soft0 == pytest.approx(20.0, abs=0.5)    # grit 0: no compressor
    assert loud10 - soft10 < 17.0                           # grit 10: the loud word squeezed
    assert soft10 == pytest.approx(soft0, abs=1.5)          # the soft one, under its threshold


def test_hot_mic_clips_from_seven():
    def clips(g): return [p for p in fx.chain_for("hot_mic", g, whisper=False)
                          if isinstance(p, pedalboard.Clipping)]
    assert clips(6) == [] and [c.threshold_db for c in clips(7)] == [-3.0]
    assert all(not [p for p in fx.chain_for(m, 10, whisper=False)
                    if isinstance(p, pedalboard.Clipping)]
               for m, p in fx.MIC_PROFILES.items() if p.clip_db == 0)


@pytest.mark.parametrize("call", [
    lambda: fx.chain_for("studio_mic", 3, whisper=False),
    lambda: fx.chain_for("desk_mic", 11, whisper=False),
    lambda: fx.chain_for("desk_mic", -1, whisper=False),
    lambda: fx.chain_for("desk_mic", 3.0, whisper=False),
    lambda: fx.chain_for("desk_mic", True, whisper=False),
    lambda: fx.chain_for("studio_mic", 3, whisper=True),
    lambda: fx.process(np.array([0.1, np.nan], np.float32), SR, spec(), grit=3, whisper=False),
    lambda: fx.process(np.zeros((2, SR), np.float32), SR, spec(), grit=3, whisper=False),
    lambda: fx.shift_pitch(tone(220, 1.0), SR, float("inf")),
    lambda: fx.MicProfile(120, 8000, 2000, 1, "opus", 4.0, 2.0, 0.0, -55),
    lambda: fx.MicProfile(300, 200, 2000, 1, "none", 4.0, 2.0, 0.0, -55),
    lambda: fx.MicProfile(120, 8000, 2000, 1, "mp3", 11.0, 2.0, 0.0, -55),
], ids=["unknown-mic", "grit-11", "grit-minus-1", "grit-float", "grit-bool", "whisper-unknown-mic",
        "nan", "stereo", "pitch-inf", "codec-opus", "lp-under-hp", "vbr-11"])
def test_bad_input_raises(call):
    with pytest.raises(ValueError):
        call()
