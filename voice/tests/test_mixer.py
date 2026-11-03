"""The mixer (Task 7): gains, fades, the -1 dBFS limiter, ends reported once, mono by default,
and a scheduler Plan applied as the scheduler means it (Task 6's carries). Signals are made in
code; nothing here touches a device."""
import math
import random
import threading
from datetime import datetime, timezone

import numpy as np
import pytest

from fakes import FakeClock
from leeroy_voice.mixer import CEILING_DB, Limiter, Mixer
from leeroy_voice.schedule import P0_GAIN_DB, PTT_DUCK_DB, Line, Plan, PlaybackScheduler

SR = 48000
CEILING = 10 ** (CEILING_DB / 20)  # -1 dBFS, 0.8913: the brief's 0.891
START = datetime(2026, 10, 31, 21, 0, tzinfo=timezone.utc)
T0 = START.timestamp()


def const(value: float, frames: int) -> np.ndarray:
    return np.full(frames, value, np.float32)


def sine(hz: float, seconds: float, amp: float = 1.0, phase: float = 0.0) -> np.ndarray:
    t = np.arange(round(seconds * SR)) / SR
    return (amp * np.sin(2 * np.pi * hz * t + phase)).astype(np.float32)


def render(m: Mixer, frames: int, block: int = 480) -> np.ndarray:
    """`frames` of the master, asked for in blocks as the audio callback asks for them."""
    parts = [m.render(min(block, frames - i)) for i in range(0, frames, block)]
    return np.concatenate(parts) if parts else np.zeros((0, 2), np.float32)


def sched() -> PlaybackScheduler:
    """A scheduler with his session online and him in guild_hall."""
    s = PlaybackScheduler(FakeClock(START), random.Random(1))
    s.set_session(True, T0)
    s.set_human_room("guild_hall", T0)
    return s


def line(id, guid, tier=4, *, segments, ttl=15.0) -> Line:
    return Line(id=id, guid=guid, tier=tier, kind="callout" if tier == 0 else "open",
                channel="guild_hall", whisper=False, expires_at=T0 + ttl, after="",
                segments=segments)


def serve(m: Mixer, s: PlaybackScheduler, lines, frames: int, now: float, own=None):
    """The service's loop in short: render a block, then hand each end the mixer reports to the
    scheduler and apply what comes back. Returns the audio and the clock after it."""
    parts = []
    for _ in range(0, frames, 480):
        parts.append(m.render(480))
        now += 480 / SR
        for line_id in m.ended():
            m.apply(s.finished(line_id, now), lines, own or {})
    return np.concatenate(parts), now


# -- gains, fades and the limiter --------------------------------------------------------------

def test_mix_with_gains():
    m = Mixer()
    m.play("a", const(0.4, SR), -6.0, guid=101)
    m.play("b", const(0.3, SR), -6.0, guid=102)
    middle = render(m, SR)[2400:SR - 2400, 0]  # away from the 10 ms fades and the end
    g = 10 ** (-6 / 20)
    np.testing.assert_allclose(middle, g * 0.4 + g * 0.3, rtol=0, atol=1e-6)
    np.testing.assert_allclose(middle, 0.5 * 0.4 + 0.5 * 0.3, rtol=0.005)


def test_fade_in():
    m = Mixer()
    m.play("a", const(0.5, SR), 0.0)
    out = render(m, 2400)[m.latency:, 0]
    np.testing.assert_allclose(out[:480], 0.5 * np.arange(1, 481) / 480, atol=1e-6)  # 10 ms
    np.testing.assert_allclose(out[480:], 0.5, atol=1e-6)


def test_stop_fades():
    m = Mixer()
    m.play("a", const(0.5, 2 * SR), 0.0)
    render(m, 9600)  # past the fade-in
    m.stop("a", 40)
    out = render(m, 4800)[:, 0]
    lat = m.latency  # the limiter's look-ahead: what was already on its way
    np.testing.assert_allclose(out[:lat], 0.5, atol=1e-6)
    np.testing.assert_allclose(out[lat:lat + 1920], 0.5 * (1 - np.arange(1, 1921) / 1920),
                               atol=1e-6)  # 40 ms: to 0 over 1920 frames
    assert not out[lat + 1920:].any()
    assert m.active() == set() and m.ended() == []  # stopped, not finished


def test_set_gain_ramps():
    m = Mixer()
    m.play("a", const(0.5, SR), 0.0, fade_in_ms=0)
    render(m, 960)
    m.set_gain("a", 20 * math.log10(0.5))  # halve it, over the default 50 ms
    out = render(m, 4800)[m.latency:, 0]
    np.testing.assert_allclose(out[:2400], 0.5 - 0.25 * np.arange(1, 2401) / 2400, atol=1e-6)
    np.testing.assert_allclose(out[2400:], 0.25, atol=1e-6)


def peak_of(*clips, gain_db=0.0, fade_in_ms=0) -> float:
    m = Mixer()
    for i, clip in enumerate(clips):
        m.play(f"c{i}", clip, gain_db, fade_in_ms=fade_in_ms)
    return float(np.abs(render(m, max(len(c) for c in clips) + 4800)).max())


def test_limiter_ceiling():
    # two full-scale clips
    peak = peak_of(sine(440, 1.0), sine(660, 1.0, phase=0.3))
    assert peak <= CEILING and round(peak, 3) <= 0.891
    assert peak > 0.85  # held at the ceiling, not crushed below it
    assert peak_of(const(1.0, SR), const(1.0, SR)) == pytest.approx(CEILING)
    # one full-scale clip alone: 0 dBFS is over -1 dBFS too
    assert peak_of(sine(440, 1.0)) <= CEILING
    # one hot clip alone (Task 5's review): a click at +13 dBFS on a quiet vowel
    vowel = sine(200, 1.0, amp=0.03)
    vowel[24000] = 10 ** (13 / 20)
    assert peak_of(vowel, fade_in_ms=10) <= CEILING
    # and a loud voice on its own: +12 dB on a full-scale tone
    assert peak_of(sine(300, 0.5), gain_db=12.0, fade_in_ms=10) <= CEILING


def test_limiter_leaves_quiet_audio_alone():
    x = sine(440, 0.5, amp=0.5)
    m = Mixer()
    m.play("a", x, 0.0, fade_in_ms=0)
    out = render(m, len(x) + 960)
    assert np.array_equal(out[m.latency:m.latency + len(x), 0], x)  # only delayed
    assert not out[:m.latency].any() and not out[m.latency + len(x):].any()


def test_limiter_scales_a_hot_tone_rather_than_clipping_it():
    """A steady tone 6 dB over full scale comes out the same tone at the ceiling: one gain, so
    no clipped tops."""
    x = sine(1000, 1.0, amp=2.0)
    m = Mixer()
    m.play("a", x, 0.0, fade_in_ms=0)
    out = render(m, SR)[:, 0]
    lat = m.latency
    y, xs = out[9600:SR - 960], x[9600 - lat:SR - 960 - lat]
    big = np.abs(xs) > 0.5
    np.testing.assert_allclose(y[big] / xs[big], CEILING / 2.0, rtol=1e-4)


def test_limiter_attack_is_smooth():
    """The gain is down before a hot onset arrives, in a ramp over the look-ahead, not a step:
    the quiet audio just before it slopes down rather than jumping."""
    x = np.concatenate([const(0.5, 4800), const(2.0, 4800)])
    m = Mixer()
    m.play("a", x, 0.0, fade_in_ms=0)
    out = render(m, 9600)[:, 0]
    w = m.latency + 1  # the look-ahead, in frames
    onset = 4800 + m.latency  # where the first hot frame comes out
    before = out[onset - 2 * w:onset]
    assert before[0] == 0.5  # untouched until the look-ahead sees the onset
    np.testing.assert_allclose(before[-w:], 0.5 * (1 - np.arange(w) / w * (1 - CEILING / 2.0)),
                               rtol=1e-5)  # in w even steps
    assert out[onset] == pytest.approx(CEILING, rel=1e-5)  # the onset arrives at the ceiling
    step = 0.5 * (1 - CEILING / 2.0) / w
    assert np.abs(np.diff(before)).max() <= step * 1.01


def test_limiter_holds_its_gain_between_peaks():
    """A hot 100 Hz tone, whose peaks are 5 ms apart, beyond the 2 ms look-ahead: the release
    holds the gain down between them (it creeps up 2 % at most), so the tone keeps its shape.
    A release 100 times faster lets it swing 28 %."""
    x = sine(100, 1.0, amp=2.0)
    m = Mixer()
    m.play("a", x, 0.0, fade_in_ms=0)
    out = render(m, SR)[:, 0]
    lat = m.latency
    y, xs = out[9600:SR - 960], x[9600 - lat:SR - 960 - lat]
    big = np.abs(xs) > 0.2
    ratio = y[big] / xs[big] / (CEILING / 2.0)
    assert ratio.min() >= 1 - 1e-4 and ratio.max() <= 1.03


def test_limiter_releases():
    m = Mixer()
    m.play("loud", const(1.0, 4800), 6.0, fade_in_ms=0)
    m.play("quiet", sine(500, 2.0, amp=0.1), 0.0, fade_in_ms=0)
    out = render(m, 2 * SR)[:, 0]
    assert np.abs(out).max() <= CEILING
    tail = out[SR:]  # 0.9 s after the loud clip ended: the quiet tone at its own level again
    assert 0.0995 <= np.abs(tail).max() <= 0.1


# -- ends, channels and input ------------------------------------------------------------------

def test_a_voices_clips_join_inside_a_block():
    m = Mixer()
    m.play("a", const(0.2, 300), 0.0, fade_in_ms=0)
    m.append("a", const(0.3, 300))
    m.append("a", const(0.4, 300))
    out = render(m, 1440)[m.latency:, 0]
    np.testing.assert_array_equal(out[:900], np.repeat(np.float32([0.2, 0.3, 0.4]), 300))
    assert not out[900:].any()
    assert m.ended() == ["a"]


def test_an_empty_clip_ends_at_once():
    m = Mixer()
    m.play("a", np.zeros(0, np.float32), 0.0)
    m.render(480)
    assert m.ended() == ["a"]


def test_ended_once():
    m = Mixer()
    m.play("a", const(0.1, 1000), 0.0)
    assert m.active() == {"a"} and m.ended() == []
    m.render(480)
    m.render(480)
    assert m.ended() == []
    m.render(480)  # it runs out in here
    assert m.ended() == ["a"]
    assert m.ended() == [] and m.active() == set()
    m.render(480)
    assert m.ended() == []


def test_ends_in_the_order_they_came():
    m = Mixer()
    m.play("long", const(0.1, 900), 0.0)
    m.play("short", const(0.1, 300), 0.0)
    m.render(480)
    m.render(480)
    assert m.ended() == ["short", "long"]


def test_mono_by_default():
    rng = np.random.default_rng(7)
    m = Mixer()
    m.play("a", (0.2 * rng.standard_normal(SR)).astype(np.float32), 0.0, guid=101)
    m.play("b", (0.2 * rng.standard_normal(SR)).astype(np.float32), -3.0, guid=102)
    out = render(m, SR)
    assert np.abs(out).max() > 0.1
    assert np.array_equal(out[:, 0], out[:, 1])


def test_stereo_spread_places_each_member():
    def placed(guid, spread=0.2):
        m = Mixer(stereo_spread=spread)
        m.play("a", const(0.25, SR), 0.0, guid=guid, fade_in_ms=0)
        left, right = render(m, 4800)[2400]
        return left / 0.25, right / 0.25
    for guid in (101, 102, 103, 104, 105):
        left, right = placed(guid)
        assert left != right
        assert left ** 2 + right ** 2 == pytest.approx(2.0)  # constant power: no louder or softer
        pan = 4 / math.pi * math.atan2(right, left) - 1  # back to -1 (left) .. 1 (right)
        assert abs(pan) <= 0.2 + 1e-9
        assert placed(guid) == (left, right)  # a member always sits in one place
    assert placed(101) != placed(102)
    assert placed(101, spread=0.0) == (1.0, 1.0)
    assert placed(0) == (1.0, 1.0)  # no member: the centre


def test_a_plan_places_each_voice_by_its_line():
    m = Mixer(stereo_spread=0.2)
    m.apply(Plan(starts=[("a", const(0.1, SR), 0.0)]), {"a": line("a", 101, segments=[None])})
    left, right = render(m, 2400)[-1]
    alone = Mixer(stereo_spread=0.2)
    alone.play("a", const(0.1, SR), 0.0, guid=101)
    assert (left, right) == tuple(render(alone, 2400)[-1]) and left != right


def test_bad_input_is_refused():
    m = Mixer()
    for bad in (np.zeros((480, 2), np.float32), np.array([0.1, np.nan], np.float32),
                np.array([np.inf], np.float32)):
        with pytest.raises(ValueError):
            m.play("a", bad, 0.0)
        with pytest.raises(ValueError):
            m.append("a", bad)
    for bad in (float("nan"), float("inf"), "6", True, None):
        with pytest.raises(ValueError):
            m.play("a", const(0.1, 10), bad)
    with pytest.raises(ValueError):
        m.stop("a", -1)
    for bad in (-1, 480.0, True):
        with pytest.raises(ValueError):
            m.render(bad)
    m.play("n", const(0.1, 960), np.float32(-3.0), np.int64(10))  # numpy's numbers will do
    assert m.render(np.int64(480)).shape == (480, 2)
    m.stop("n", 0)
    with pytest.raises(ValueError):
        Mixer(stereo_spread=-0.1)
    with pytest.raises(ValueError):
        Mixer(sample_rate=0)
    assert Limiter().process(np.zeros((0, 2), np.float32)).shape == (0, 2)
    m.play("b", const(0.1, SR), 0.0, fade_in_ms=0)
    with pytest.raises(ValueError):  # a bad clip anywhere in a Plan: none of it is applied
        m.apply(Plan(stops=[("b", 40, "")], starts=[("c", const(np.nan, 10), 0.0)]), {})
    assert m.active() == {"b"}
    np.testing.assert_allclose(render(m, 4800)[m.latency:, 0], 0.1)  # b was not stopped
    assert m.render(0).shape == (0, 2)


def test_render_shape():
    m = Mixer()
    out = m.render(480)
    assert out.shape == (480, 2) and out.dtype == np.float32 and not out.any()
    clip = sine(300, 0.5, amp=1.5)  # hot, so the limiter works across the pieces too
    whole, pieces = Mixer(), Mixer()
    whole.play("a", clip, 0.0)
    pieces.play("a", clip, 0.0)
    once = whole.render(12000)  # longer than one internal piece
    assert once.shape == (12000, 2)
    np.testing.assert_array_equal(once, render(pieces, 12000))


def test_render_runs_on_the_audio_thread():
    """Thread-safe: the event loop plays, appends, stops and ducks while the audio callback
    renders."""
    m = Mixer()
    done = threading.Event()
    trouble: list[BaseException] = []

    def audio():
        try:
            while not done.is_set():
                out = m.render(480)
                assert out.shape == (480, 2) and np.isfinite(out).all()
                assert np.abs(out).max() <= CEILING
        except BaseException as e:  # reported below, on the test's own thread
            trouble.append(e)

    t = threading.Thread(target=audio)
    t.start()
    try:
        rng = np.random.default_rng(3)
        for i in range(300):
            clip = (0.5 * rng.standard_normal(960)).astype(np.float32)
            m.play(f"v{i}", clip, 3.0, guid=101 + i % 5)
            m.append(f"v{i}", clip)
            m.set_gain(f"v{i - 1}", -6.0)
            m.stop(f"v{i - 2}", 5)
            m.ended()
            m.active()
    finally:
        done.set()
        t.join(5)
    assert not t.is_alive() and not trouble


# -- the scheduler's Plans (Task 6's carries) --------------------------------------------------

def test_an_append_after_the_audio_ran_out_still_plays():
    """Segment 1 reaches the mixer just after segment 0 ran out, before its end was taken. It
    still plays, and the end is held back until it has: an end reported while it plays would
    let the member overlap themself."""
    m = Mixer()
    m.play("a", const(0.2, 600), 0.0, fade_in_ms=0)
    m.render(960)  # segment 0 runs out in this block
    assert m.active() == set()  # silent, its end not taken yet
    m.append("a", const(0.4, 960))
    assert m.ended() == [] and m.active() == {"a"}  # no end: it has audio again
    out = render(m, 1920)
    assert np.count_nonzero(np.isclose(out[:, 0], 0.4)) == 960  # every appended frame played
    assert m.ended() == ["a"] and m.ended() == []


def test_a_stale_end_never_reaches_the_scheduler():
    """(c) end to end: 101's two-segment line runs out of segment 0 just before segment 1 goes
    out as an append. The scheduler hears of its end only once segment 1 has played, so 101's
    next line never starts over it."""
    s, m = sched(), Mixer()
    a = line("a", 101, segments=[const(0.2, 2400), None])
    b = line("b", 101, segments=[const(0.3, 2 * SR)])
    lines = {"a": a, "b": b}
    m.apply(s.submit(a, T0), lines)
    m.apply(s.submit(b, T0), lines)
    assert s.playing() == {"a"}
    m.render(2880)  # segment 0 runs out in here; its end waits, untaken
    s.ready("a", 1, const(0.2, 2400))
    plan = s.tick(T0 + 0.06)
    assert [i for i, _ in plan.appends] == ["a"] and not plan.starts
    m.apply(plan, lines)
    assert m.ended() == []
    heard, _ = serve(m, s, lines, 9600, T0 + 0.06)
    assert s.playing() == {"b"}  # b started once a was done ...
    assert np.abs(heard[:, 0]).max() < 0.31  # ... and never over a (0.2 + 0.3)
    assert np.count_nonzero(np.isclose(heard[:, 0], 0.2)) >= 2300  # a's segment 1 played whole


def test_a_line_whose_end_was_taken_resumes_as_a_fresh_voice():
    """The other order: the end is taken before segment 1 is ready. The scheduler holds the line
    and resumes it with a start of the same id, which the mixer plays as a new voice."""
    s, m = sched(), Mixer()
    a = line("a", 101, segments=[const(0.2, 2400), None])
    lines = {"a": a}
    m.apply(s.submit(a, T0), lines)
    m.render(2880)
    assert m.ended() == ["a"]
    plan = s.finished("a", T0 + 0.06)
    assert plan.played == [] and s.playing() == {"a"}  # it keeps its slot
    s.ready("a", 1, const(0.2, 2400))
    plan = s.tick(T0 + 0.1)
    assert [i for i, _, _ in plan.starts] == ["a"]
    m.apply(plan, lines)
    assert m.active() == {"a"}
    heard, _ = serve(m, s, lines, 4800, T0 + 0.1)
    assert np.count_nonzero(np.isclose(heard[:, 0], 0.2)) >= 1900  # past its 10 ms fade-in
    assert s.playing() == set() and m.active() == set()


def test_a_start_over_a_held_end_is_a_fresh_voice():
    m = Mixer()
    m.play("a", const(0.2, 300), 0.0, fade_in_ms=0)
    m.render(480)  # its audio ran out; the end waits
    m.play("a", const(0.3, 960), 0.0, fade_in_ms=0)
    assert m.ended() == [] and m.active() == {"a"}  # that end belonged to the old voice
    render(m, 1440)
    assert m.ended() == ["a"]


def test_ids_the_mixer_does_not_hold_are_harmless():
    m = Mixer()
    m.stop("nobody", 40)
    m.set_gain("nobody", -6.0)
    m.append("nobody", const(0.5, 480))
    m.apply(Plan(stops=[("gone", 40, "cut")], appends=[("gone", const(0.5, 480))],
                 gains={"gone": -6.0}), {})
    assert not render(m, 960).any()
    assert m.active() == set() and m.ended() == []


def test_a_start_and_its_append_in_one_plan_play_in_order():
    m = Mixer()
    m.apply(Plan(starts=[("a", const(0.2, 960), 0.0)], appends=[("a", const(0.3, 960))]), {})
    out = render(m, 2400)[m.latency:, 0]
    np.testing.assert_allclose(out[480:960], 0.2, atol=1e-6)  # after its 10 ms fade-in
    np.testing.assert_allclose(out[960:1920], 0.3, atol=1e-6)
    assert not out[1920:].any()


def test_a_p0_call_ducked_by_the_key_comes_back_to_its_level():
    """Task 6's review, minor 4: `gains` carry the duck alone, so a p0 voice that started
    ducked under the key comes back to its own gain plus P0_GAIN_DB at key-up."""
    s, m = sched(), Mixer()
    s.set_ptt(True, T0)
    call = line("call", 101, tier=0, segments=[const(0.1, 2 * SR)], ttl=3.0)
    lines, own = {"call": call}, {"call": -3.0}
    plan = s.submit(call, T0)
    assert [(i, g) for i, _, g in plan.starts] == [("call", P0_GAIN_DB + PTT_DUCK_DB)]
    m.apply(plan, lines, own)
    out = render(m, 4800)[:, 0]
    assert out[-1] == pytest.approx(0.1 * 10 ** ((-3.0 + P0_GAIN_DB + PTT_DUCK_DB) / 20))
    plan = s.set_ptt(False, T0 + 0.1)
    assert plan.gains == {"call": 0.0}
    m.apply(plan, lines, own)
    out = render(m, 9600)[:, 0]
    assert out[-1] == pytest.approx(0.1 * 10 ** ((-3.0 + P0_GAIN_DB) / 20))


def test_ducks_change_only_the_ducked_voice():
    """A whisper ducks the room's voice 6 dB; when it ends, the room voice comes back to its own
    level, and the whisper played at its own all along."""
    s, m = sched(), Mixer()
    room = line("room", 101, segments=[const(0.1, 2 * SR)])
    whisper = Line(id="w", guid=102, tier=3, kind="reply_human", channel="whisper", whisper=True,
                   expires_at=T0 + 30, after="", segments=[const(0.1, 9600)])
    lines, own = {"room": room, "w": whisper}, {"room": 2.0, "w": -1.0}
    m.apply(s.submit(room, T0), lines, own)
    plan = s.submit(whisper, T0 + 0.01)
    assert plan.gains == {"room": -6.0}
    m.apply(plan, lines, own)
    assert m.active() == {"room", "w"}
    heard, now = serve(m, s, lines, 7200, T0 + 0.01, own)
    np.testing.assert_allclose(heard[-1, 0], 0.1 * 10 ** ((2.0 - 6.0) / 20) +
                               0.1 * 10 ** (-1.0 / 20), rtol=1e-5)
    heard, _ = serve(m, s, lines, 9600, now, own)  # the whisper ends: the room voice comes back
    assert m.active() == {"room"}
    assert heard[-1, 0] == pytest.approx(0.1 * 10 ** (2.0 / 20))
