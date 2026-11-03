"""The playback scheduler (Task 6): tiers, expiry, slots, push-to-talk, raid silence, moves,
`after` (I7) and segments, on a FakeClock with fake audio. Nothing here touches a device."""
import random
from datetime import datetime, timezone

import numpy as np
import pytest

from conftest import run
from fakes import FakeClock
from leeroy_voice.schedule import (AFTER_MEMORY_S, CUT_FADE_MS, SEGMENT_WAIT_S, Line,
                                   PlaybackScheduler)
from leeroy_voice.wire import encode

START = datetime(2026, 10, 31, 21, 0, tzinfo=timezone.utc)
T0 = START.timestamp()
ROOM = "guild_hall"
KINDS = {0: "callout", 1: "urgent", 2: "callout", 3: "reply_human", 4: "open"}


def tone(seconds: float = 1.0) -> np.ndarray:
    return np.full(int(seconds * 48000), 0.1, np.float32)


def line(id, guid, tier=4, *, kind=None, channel=ROOM, whisper=None, ttl=15.0, at=T0, after="",
         segments=None, template=False) -> Line:
    """A ready one-segment line of 1 s in his room, expiring `ttl` s after `at`."""
    return Line(id=id, guid=guid, tier=tier, kind=kind or KINDS[tier], channel=channel,
                whisper=channel == "whisper" if whisper is None else whisper,
                expires_at=at + ttl, after=after,
                segments=[tone()] if segments is None else segments, template=template)


def sched(*, room=ROOM, max_voices=2) -> PlaybackScheduler:
    """A scheduler with his session online and him in `room`."""
    s = PlaybackScheduler(FakeClock(START), random.Random(1), max_voices=max_voices)
    s.set_session(True, T0)
    s.set_human_room(room, T0)
    return s


def started(plan) -> list[str]:
    return [i for i, _, _ in plan.starts]


def starts(plan) -> list[tuple[str, float]]:
    return [(i, gain) for i, _, gain in plan.starts]


def played(plan) -> list[tuple[str, str, str]]:
    return [(p.id, p.state, p.reason) for p in plan.played]


def first_start(s, line_id, since, *, within=1.0, step=0.005):
    """The first tick after `since`, in steps of `step`, at which `line_id` starts."""
    for n in range(1, int(within / step) + 1):
        now = since + n * step
        if line_id in started(s.tick(now)):
            return now
    return None


def started_within(s, since, *, within=0.5, step=0.005) -> list[str]:
    """The lines started on the ticks after `since`, in order."""
    return [i for n in range(1, int(within / step) + 1) for i in started(s.tick(since + n * step))]


def test_one_fifo_per_member():
    s = sched()
    assert started(s.submit(line("a", 101), T0)) == ["a"]
    p = s.submit(line("b", 101), T0)
    assert started(p) == [] and s.waiting() == 1          # a slot is free, but 101 is talking
    assert started(s.tick(T0 + 0.9)) == []
    p = s.finished("a", T0 + 1.0)
    assert played(p) == [("a", "done", ""), ("b", "started", "")]   # 101 goes on: no gap


def test_at_most_two_audible():
    s = sched()
    plans = [s.submit(line(i, g), T0) for i, g in (("a", 101), ("b", 102), ("c", 103))]
    assert [started(p) for p in plans] == [["a"], ["b"], []]
    assert s.playing() == {"a", "b"} and s.waiting() == 1
    assert played(s.finished("a", T0 + 1.0)) == [("a", "done", "")]   # another speaker waits
    when = first_start(s, "c", T0 + 1.0)
    assert when is not None and 0.15 <= when - (T0 + 1.0) <= 0.405
    assert s.playing() == {"b", "c"}


def test_turn_taking_gap_varies():
    """Two members taking turns on one voice: each start comes 0.15-0.40 s after the other
    speaker's end, a fresh draw each time."""
    s = sched(max_voices=1)
    for n in range(8):
        s.submit(line(f"l{n}", 101 + n % 2, ttl=60.0), T0)
    gaps, end = [], T0 + 1.0
    for n in range(7):
        assert s.playing() == {f"l{n}"}
        assert started(s.finished(f"l{n}", end)) == []
        when = first_start(s, f"l{n + 1}", end)
        assert when is not None
        gaps.append(when - end)
        end = when + 1.0
    assert all(0.15 <= g <= 0.405 for g in gaps), gaps
    assert len({round(g, 3) for g in gaps}) > 2, gaps


def test_p0_cuts_the_lowest():
    s = sched()
    s.submit(line("a", 101), T0)
    s.submit(line("b", 102), T0 + 0.1)
    p = s.submit(line("call", 105, 0), T0 + 0.5)
    assert p.stops == [("b", 40, "")]                     # the later-started p4
    assert played(p) == [("b", "cut", ""), ("call", "started", "")]
    assert starts(p) == [("call", 2.0)] and s.playing() == {"a", "call"}
    s = sched()                                           # the lowest tier before the later start
    s.submit(line("c", 101, 4), T0)
    s.submit(line("d", 102, 3), T0 + 0.1)
    p = s.submit(line("call", 105, 0), T0 + 0.5)
    assert p.stops == [("c", 40, "")] and s.playing() == {"d", "call"}


def test_a_silent_voice_goes_first():
    """A line taking a slot takes a silent voice (one waiting for its next segment) before an
    audible one, whatever their tiers and start order."""
    for urgent, tier in (("call", 0), ("adds", 1)):
        s = sched()
        s.submit(line("silent", 101, 3, segments=[tone(), None]), T0)
        s.submit(line("audible", 102, 4), T0 + 0.1)
        s.finished("silent", T0 + 1.0)
        p = s.submit(line(urgent, 105, tier), T0 + 1.05)
        assert played(p) == [("silent", "cut", ""), (urgent, "started", "")], urgent
        assert p.stops == [] and s.playing() == {"audible", urgent}


def test_p1_ducks_and_takes_a_slot():
    s = sched()
    s.submit(line("a", 101), T0)
    s.submit(line("b", 102), T0 + 0.1)
    p = s.submit(line("adds", 103, 1), T0 + 0.5)
    assert p.stops == [("b", 40, "")]
    assert played(p) == [("b", "cut", ""), ("adds", "started", "")]
    assert starts(p) == [("adds", 0.0)] and p.gains == {"a": -10.0}
    assert s.tick(T0 + 1.0).gains.get("a", -10.0) == -10.0     # until the p1 finishes
    assert s.finished("adds", T0 + 1.5).gains == {"a": 0.0}


def test_only_p0_takes_a_p2_slot():
    """A p1 takes a slot only from p3 or p4 (settled item 10), a p0 from anything below it. An
    urgent line starts the moment a slot frees, and never ducks the lead's call."""
    s = sched(room="raid")
    s.submit(line("o1", 104, 2, channel="raid"), T0)
    s.submit(line("o2", 103, 2, channel="raid"), T0 + 0.1)
    p = s.submit(line("adds", 101, 1, channel="raid"), T0 + 0.2)
    assert p.starts == [] and p.stops == []
    p = s.submit(line("call", 105, 0, channel="raid"), T0 + 0.3)
    assert p.stops == [("o2", 40, "")] and starts(p) == [("call", 2.0)]
    p = s.finished("o1", T0 + 0.5)
    assert starts(p) == [("adds", 0.0)]                   # no turn-taking gap for it
    assert p.gains.get("call", 0.0) == 0.0


def test_urgent_interrupts_its_own_banter():
    """An "adds" never dies behind its own member's banter: it cuts it and goes first."""
    s = sched()
    s.submit(line("a", 101), T0)
    s.submit(line("q", 101), T0)
    p = s.submit(line("adds", 101, 1), T0 + 0.5)
    assert p.stops == [("a", 40, "")]
    assert played(p) == [("a", "cut", ""), ("adds", "started", "")]
    assert started(s.finished("adds", T0 + 1.5)) == ["q"]


def test_member_queue_most_urgent_first():
    s = sched(max_voices=1)
    s.submit(line("x", 102), T0)
    s.submit(line("b4", 101, 4), T0)
    s.submit(line("b3", 101, 3), T0 + 0.1)
    s.finished("x", T0 + 1.0)
    assert first_start(s, "b3", T0 + 1.0) is not None and s.playing() == {"b3"}
    assert started(s.finished("b3", T0 + 3.0)) == ["b4"]


def test_most_urgent_line_gets_the_slot():
    """Across members too, a freed slot goes to the most urgent waiting line, not the oldest."""
    s = sched(max_voices=1, room="raid")
    s.submit(line("x", 101, channel="raid"), T0)
    s.submit(line("banter", 102, 4, channel="raid"), T0)
    s.submit(line("officer", 103, 2, channel="raid"), T0 + 0.1)
    s.finished("x", T0 + 1.0)
    assert first_start(s, "officer", T0 + 1.0) is not None and s.playing() == {"officer"}


def test_crossed_answers_play_in_dependency_order():
    """R2: an `after` dependency beats tier order. Two members each have a p4 line waiting and
    a p3 line answering the other's p4. Each answer lets its line's chain through: the p4 lines
    play first, then the answers, instead of all four waiting on each other until they
    expire."""
    s = sched()
    s.submit(line("busy1", 103), T0)
    s.submit(line("busy2", 104), T0)
    s.submit(line("w", 101), T0)
    s.submit(line("y", 102), T0)
    s.submit(line("x", 101, 3, after="y"), T0 + 0.1)
    s.submit(line("z", 102, 3, after="w"), T0 + 0.1)
    s.finished("busy1", T0 + 1.0)
    s.finished("busy2", T0 + 1.0)
    assert sorted(started_within(s, T0 + 1.0)) == ["w", "y"]
    later = started(s.finished("w", T0 + 2.5)) + started(s.finished("y", T0 + 2.5))
    assert sorted(later + started_within(s, T0 + 2.5)) == ["x", "z"]


def test_a_waiting_answer_keeps_its_place_in_its_tier():
    """R2: a later line of the same member and tier never overtakes an earlier one still
    waiting on its `after`; it waits behind it, with a slot free (FIFO within a tier)."""
    s = sched()
    s.submit(line("aside", 102, kind="aside"), T0)
    s.submit(line("a", 101, after="aside"), T0 + 0.1)
    assert started(s.submit(line("b", 101), T0 + 0.2)) == []
    assert started(s.tick(T0 + 0.5)) == [] and s.playing() == {"aside"}
    s.finished("aside", T0 + 1.0)
    assert first_start(s, "a", T0 + 1.0) is not None and s.playing() == {"a"}
    assert started(s.finished("a", T0 + 3.0)) == ["b"]


def test_a_more_urgent_line_goes_ahead_of_a_waiting_answer():
    """Tier order still holds: a later p1 of the same member starts while their p3 answer
    waits on its `after`."""
    s = sched()
    s.submit(line("aside", 102, kind="aside"), T0)
    s.submit(line("a", 101, 3, after="aside"), T0 + 0.1)
    assert started(s.submit(line("adds", 101, 1), T0 + 0.2)) == ["adds"]
    assert s.playing() == {"aside", "adds"} and s.waiting() == 1


def test_answer_waits_behind_its_own_members_line():
    """A line answering its own member's earlier line never goes ahead of it, whatever its
    tier, so the two cannot wait on each other until both expire."""
    s = sched(max_voices=1)
    s.submit(line("x", 102), T0)
    s.submit(line("first", 101, 4), T0)
    s.submit(line("then", 101, 1, after="first"), T0 + 0.1)
    s.finished("x", T0 + 1.0)
    assert first_start(s, "first", T0 + 1.0) is not None
    assert started(s.finished("first", T0 + 3.0)) == ["then"]


def test_expiry():
    s = sched()
    s.submit(line("a", 101), T0)
    s.submit(line("b", 102), T0)
    s.submit(line("late", 103, ttl=1.0), T0)
    assert played(s.tick(T0 + 0.99)) == []
    p = s.tick(T0 + 1.01)
    assert played(p) == [("late", "dropped", "expired")] and s.waiting() == 0
    assert "late" not in started(s.finished("a", T0 + 2.0))
    assert played(s.tick(T0 + 20.0)) == [] and s.playing() == {"b"}   # it plays to its end
    p = s.submit(line("stale", 104, at=T0 + 20.0, ttl=-0.5), T0 + 20.0)
    assert played(p) == [("stale", "dropped", "expired")]


def test_ptt_holds_and_ducks():
    s = sched(max_voices=3)
    s.submit(line("a", 101), T0)
    p = s.set_ptt(True, T0 + 0.2)
    assert p.gains == {"a": -12.0} and p.stops == []      # ducked, not cut
    assert started(s.submit(line("r", 102, 3), T0 + 0.3)) == []   # a slot is free: he talks
    p = s.submit(line("adds", 103, 1), T0 + 0.4)
    assert starts(p) == [("adds", -12.0)]
    assert p.gains.get("a", -12.0) == -12.0               # the deepest duck, not -22
    assert starts(s.submit(line("call", 105, 0), T0 + 0.5)) == [("call", -10.0)]
    s.finished("adds", T0 + 1.4)
    s.finished("call", T0 + 1.5)
    assert started(s.tick(T0 + 2.0)) == []
    p = s.set_ptt(False, T0 + 2.0)
    assert p.gains == {"a": 0.0} and started(p) == []
    assert started(s.tick(T0 + 2.29)) == []
    assert started(s.tick(T0 + 2.31)) == ["r"]


def test_template_line_is_never_held():
    """R1, settled item 13: a template line plays while he talks, ducked under him, at any
    tier, and in the grace after key-up; the same line from the model waits for both."""
    for tier in (2, 3, 4):
        s = sched()
        s.set_ptt(True, T0)
        assert starts(s.submit(line("cheer", 101, tier, template=True), T0 + 0.1)) == [
            ("cheer", -12.0)], tier
    s = sched()
    s.set_ptt(True, T0)
    assert started(s.submit(line("reply", 102, 3), T0 + 0.1)) == []
    s.set_ptt(False, T0 + 1.0)
    assert started(s.submit(line("gz", 103, 4, template=True), T0 + 1.1)) == ["gz"]
    assert started(s.tick(T0 + 1.29)) == []
    assert started(s.tick(T0 + 1.31)) == ["reply"]


def test_a_held_line_steps_aside_for_its_members_template():
    """A member's line held by the key does not hold up that member's template line."""
    s = sched()
    s.set_ptt(True, T0)
    s.submit(line("reply", 101, 3), T0 + 0.1)
    assert starts(s.submit(line("cheer", 101, 4, template=True), T0 + 0.2)) == [
        ("cheer", -12.0)]
    s.finished("cheer", T0 + 1.2)
    s.set_ptt(False, T0 + 2.0)
    assert started(s.tick(T0 + 2.29)) == []
    assert started(s.tick(T0 + 2.31)) == ["reply"]


def test_ducks_combine_as_the_deepest():
    """Each voice takes the deepest duck that applies, never the sum: the key -12 on every
    voice, a p1 -10 on every p2-p4 voice (a lower tier never ducks a higher one, R3, so urgent
    lines do not duck each other), and a whisper -6 on every other voice in his channel."""
    s = sched(max_voices=4)
    s.submit(line("a", 101), T0)
    assert s.submit(line("w", 102, 3, channel="whisper"), T0).gains == {"a": -6.0}
    p = s.submit(line("u", 103, 1), T0)
    assert p.gains == {"a": -10.0, "w": -10.0} and starts(p) == [("u", -6.0)]
    p = s.submit(line("u2", 104, 1), T0)
    assert starts(p) == [("u2", -6.0)] and p.gains.get("u", -6.0) == -6.0
    p = s.set_ptt(True, T0 + 0.1)
    assert p.gains == {"a": -12.0, "w": -12.0, "u": -12.0, "u2": -12.0}
    p = s.set_ptt(False, T0 + 0.5)
    assert p.gains == {"a": -10.0, "w": -10.0, "u": -6.0, "u2": -6.0}


LINES = {"p0": (0, "callout", 105), "p1": (1, "urgent", 101), "p2": (2, "callout", 104),
         "p3": (3, "reply_human", 102), "p4": (4, "open", 103), "react": (4, "wipe_react", 103)}
EVERYTHING = set(LINES)


@pytest.mark.parametrize("phase, passing", [
    ("combat", {"p0", "p1", "p2"}), ("wipe_silence", {"p1"}),
    ("wipe_debrief", {"p0", "p1", "p2", "react"}),
    ("trash", EVERYTHING), ("kill", EVERYTHING), ("none", EVERYTHING)])
def test_raid_mirror(phase, passing):
    """I1: the lead's post-wipe CALLOUT is p0 and is heard in the debrief."""
    for name, (tier, kind, guid) in LINES.items():
        s = sched(room="raid")
        s.set_raid(phase, 105, T0)
        p = s.submit(line(name, guid, tier, kind=kind, channel="raid"), T0)
        want = [(name, "started", "")] if name in passing else [(name, "dropped", "raid")]
        assert played(p) == want, (phase, name)


def test_raid_phase_drops_waiting_lines():
    """A phase change drops the waiting lines it silences; one already playing plays on (the
    mind's Cancel(below="p3") at a pull cuts banter)."""
    s = sched(room="raid")
    s.submit(line("a", 101, channel="raid"), T0)
    s.submit(line("c", 104, 2, channel="raid"), T0)
    s.submit(line("q", 102, channel="raid"), T0)
    p = s.set_raid("combat", 105, T0 + 0.5)
    assert played(p) == [("q", "dropped", "raid")] and p.stops == []
    assert s.playing() == {"a", "c"} and s.waiting() == 0
    assert played(s.set_raid("trash", 105, T0 + 1.0)) == []


def test_moved():
    s = sched()
    s.submit(line("a", 101), T0)
    s.submit(line("w", 102, 3, channel="whisper"), T0)
    s.submit(line("b", 103), T0)
    p = s.set_human_room("raid", T0 + 0.5)
    assert p.stops == [("a", 150, "moved")]
    assert played(p) == [("a", "cut", "moved"), ("b", "dropped", "moved")]
    assert s.playing() == {"w"}                           # a whisper follows him
    assert played(s.submit(line("c", 104), T0 + 0.6)) == [("c", "dropped", "not_his_channel")]


def test_not_his_channel():
    s = sched(max_voices=4)
    assert played(s.submit(line("s", 104, channel="smoke_room"), T0)) == [
        ("s", "dropped", "not_his_channel")]
    assert started(s.submit(line("w1", 102, 3, channel="whisper"), T0)) == ["w1"]
    s.set_human_room("raid", T0)
    assert started(s.submit(line("w2", 103, 3, channel="whisper"), T0)) == ["w2"]
    s.set_human_room("", T0)                              # off comms
    assert started(s.submit(line("w3", 105, 3, channel="whisper"), T0)) == ["w3"]
    assert played(s.submit(line("g", 101, channel=""), T0)) == [
        ("g", "dropped", "not_his_channel")]


def test_after():
    s = sched()                                           # waits while "a" plays, then goes
    s.submit(line("a", 101, kind="aside"), T0)
    assert started(s.submit(line("d", 102, after="a"), T0 + 0.1)) == []
    assert started(s.tick(T0 + 0.5)) == []
    s.finished("a", T0 + 1.0)
    when = first_start(s, "d", T0 + 1.0)
    assert when is not None and 0.15 <= when - (T0 + 1.0) <= 0.405
    s = sched()                                           # "a" dropped while "d" waited
    s.submit(line("x", 101), T0)
    s.submit(line("y", 103), T0)
    s.submit(line("a", 104, kind="aside", ttl=1.0), T0)
    s.submit(line("d", 105, after="a"), T0)
    assert played(s.tick(T0 + 1.1)) == [("a", "dropped", "expired"),
                                        ("d", "dropped", "after_dropped")]
    s.submit(line("a2", 104, kind="aside", channel="smoke_room"), T0 + 1.2)   # dropped at once
    assert played(s.submit(line("d2", 102, after="a2"), T0 + 1.2)) == [
        ("d2", "dropped", "after_dropped")]
    s = sched()                                           # "a" cut
    s.submit(line("a", 101, kind="aside"), T0)
    s.submit(line("d", 102, after="a"), T0)
    assert played(s.cancel(id="a", now=T0 + 0.5)) == [("a", "cut", "cancelled"),
                                                      ("d", "dropped", "after_dropped")]
    assert played(s.submit(line("d2", 104, after="a"), T0 + 0.6)) == [   # after the cut
        ("d2", "dropped", "after_dropped")]
    assert played(s.submit(line("d3", 103, after="never-seen"), T0 + 1.0)) == [
        ("d3", "dropped", "after_dropped")]
    s = sched()                                           # known 119 s on, forgotten at 121 s
    s.submit(line("a", 101, kind="aside"), T0)
    s.finished("a", T0 + 1.0)
    assert started(s.submit(line("d4", 102, after="a", at=T0 + 120.0), T0 + 120.0)) == ["d4"]
    assert played(s.submit(line("d5", 103, after="a", at=T0 + 122.0), T0 + 122.0)) == [
        ("d5", "dropped", "after_dropped")]


def test_whisper_ducks_his_room():
    """The room recedes under a whisper; another whisper is close too, and is not ducked."""
    s = sched(max_voices=4)
    s.submit(line("a", 101), T0)
    s.submit(line("b", 103), T0)
    p = s.submit(line("w", 102, 3, channel="whisper"), T0 + 0.1)
    assert starts(p) == [("w", 0.0)] and p.gains == {"a": -6.0, "b": -6.0}
    p = s.submit(line("w2", 104, 3, channel="whisper"), T0 + 0.2)
    assert starts(p) == [("w2", 0.0)] and p.gains.get("w", 0.0) == 0.0
    s.finished("w2", T0 + 1.2)
    assert s.finished("w", T0 + 1.3).gains == {"a": 0.0, "b": 0.0}


def test_whisper_ducks_the_raid_calls_too():
    """A whisper to him ducks his channel by 6 dB whatever tier plays there, as the draft says;
    R3's rule is for tier ducking only."""
    s = sched(room="raid", max_voices=3)
    s.submit(line("call", 105, 0, channel="raid"), T0)
    s.submit(line("officer", 104, 2, channel="raid"), T0)
    p = s.submit(line("w", 102, 3, channel="whisper"), T0 + 0.1)
    assert starts(p) == [("w", 0.0)] and p.gains == {"call": -6.0, "officer": -6.0}


def test_segments():
    s = sched()
    s0, s1 = tone(), tone()
    p = s.submit(line("a", 101, segments=[s0, None]), T0)
    assert [(i, audio is s0) for i, audio, _ in p.starts] == [("a", True)] and p.appends == []
    s.ready("a", 1, s1)
    p = s.tick(T0 + 0.5)
    assert [(i, audio is s1) for i, audio in p.appends] == [("a", True)] and p.starts == []
    assert played(s.finished("a", T0 + 2.0)) == [("a", "done", "")]
    s = sched()                                           # an unready segment 1 holds the end
    s.submit(line("b", 101, segments=[tone(), None]), T0)
    s.submit(line("next", 101), T0)
    p = s.finished("b", T0 + 1.0)
    assert played(p) == [] and p.starts == [] and s.playing() == {"b"}
    assert s.tick(T0 + 2.0).starts == []
    late = tone()
    s.ready("b", 1, late)
    p = s.tick(T0 + 2.5)                                  # it resumes: no second "started"
    assert [(i, audio is late) for i, audio, _ in p.starts] == [("b", True)] and played(p) == []
    assert played(s.finished("b", T0 + 3.5)) == [("b", "done", ""), ("next", "started", "")]


def test_segments_go_in_order():
    s = sched()
    a0, a1 = tone(), tone()
    p = s.submit(line("both", 101, segments=[a0, a1]), T0)    # both ready: 0 starts, 1 appends
    assert [audio is a0 for _, audio, _ in p.starts] == [True]
    assert [audio is a1 for _, audio in p.appends] == [True]
    t1, t2 = tone(), tone()
    s.submit(line("c", 102, segments=[tone(), None, None]), T0)
    s.ready("c", 2, t2)
    assert s.tick(T0 + 0.1).appends == []                 # segment 2 waits for segment 1
    s.ready("c", 1, t1)
    appends = s.tick(T0 + 0.2).appends
    assert [i for i, _ in appends] == ["c", "c"]
    assert appends[0][1] is t1 and appends[1][1] is t2


def test_waits_for_its_first_segment():
    """A line not yet synthesized holds no slot and starts once it is ready; it holds up only
    its own member's later lines."""
    s = sched(max_voices=1)
    assert started(s.submit(line("u", 101, segments=[None]), T0)) == []
    s.submit(line("u2", 101), T0)
    assert started(s.submit(line("r", 102), T0 + 0.1)) == ["r"]
    s.finished("r", T0 + 1.1)
    assert started(s.tick(T0 + 2.0)) == []
    first = tone()
    s.ready("u", 0, first)
    p = s.tick(T0 + 2.1)
    assert [(i, audio is first) for i, audio, _ in p.starts] == [("u", True)]


def test_stalled_line_ages_out():
    """A segment that never comes cannot hold a slot for good (Task 2's review: every line
    ends)."""
    s = sched()
    s.submit(line("a", 101, segments=[tone(), None]), T0)
    s.finished("a", T0 + 1.0)
    assert played(s.tick(T0 + 1.0 + SEGMENT_WAIT_S - 0.01)) == []
    p = s.tick(T0 + 1.0 + SEGMENT_WAIT_S + 0.01)
    assert played(p) == [("a", "cut", "engine_error")] and p.stops == []
    assert s.playing() == set()


def test_cancel():
    s = sched()                                           # by id: waiting dropped, playing cut
    for i, g in (("a", 101), ("b", 102), ("c", 103)):
        s.submit(line(i, g), T0)
    p = s.cancel(id="c", now=T0 + 0.1)
    assert played(p) == [("c", "dropped", "cancelled")] and p.stops == []
    p = s.cancel(id="a", now=T0 + 0.2)
    assert p.stops == [("a", 40, "cancelled")] and played(p) == [("a", "cut", "cancelled")]
    s = sched()                                           # by guid: all of that member's
    s.submit(line("a", 101), T0)
    s.submit(line("a2", 101), T0)
    s.submit(line("b", 102), T0)
    p = s.cancel(guid=101, now=T0 + 0.1)
    assert p.stops == [("a", 40, "cancelled")]
    assert played(p) == [("a", "cut", "cancelled"), ("a2", "dropped", "cancelled")]
    assert s.playing() == {"b"} and s.waiting() == 0
    s = sched(max_voices=5)                               # below=3: p3 and p4 go, p0-p2 stay
    for i, g, t in (("call", 105, 0), ("adds", 101, 1), ("c", 104, 2), ("r", 102, 3),
                    ("a", 103, 4), ("c2", 104, 2), ("r2", 102, 3), ("a2", 103, 4)):
        s.submit(line(i, g, t), T0)
    p = s.cancel(below=3, now=T0 + 0.1)
    assert sorted(p.stops) == [("a", 40, "cancelled"), ("r", 40, "cancelled")]
    assert sorted(played(p)) == [("a", "cut", "cancelled"), ("a2", "dropped", "cancelled"),
                                 ("r", "cut", "cancelled"), ("r2", "dropped", "cancelled")]
    assert s.playing() == {"call", "adds", "c"} and s.waiting() == 1


def test_session_off_fades_everything():
    """Review Focus 2: logout mid-line."""
    s = sched()
    s.submit(line("a", 101), T0)
    s.submit(line("w", 102, 3, channel="whisper"), T0)
    s.submit(line("q", 103), T0)
    s.set_ptt(True, T0 + 0.2)
    p = s.set_session(False, T0 + 0.5)
    assert p.stops == [("a", 150, "offline"), ("w", 150, "offline")]
    assert played(p) == [("a", "cut", "offline"), ("w", "cut", "offline"),
                         ("q", "dropped", "offline")]
    assert s.playing() == set() and s.waiting() == 0
    assert played(s.submit(line("late", 104), T0 + 0.6)) == [("late", "dropped", "offline")]
    s.set_session(True, T0 + 1.0)
    assert started(s.submit(line("b", 105), T0 + 1.1)) == ["b"]   # the press ended with it


def test_bad_input_raises():
    s = sched()
    for bad in (line("x", 101, 5, kind="open"), line("x", 101, segments=[]), line("", 101),
                line("x", 101, channel="whisper", whisper=False), line("x", 101, whisper=True)):
        with pytest.raises(ValueError):
            s.submit(bad, T0)
    s.submit(line("a", 101), T0)
    with pytest.raises(ValueError):
        s.submit(line("a", 102), T0)                      # the id is taken
    for kw in ({}, {"id": "a", "guid": 101}, {"below": 5}):
        with pytest.raises(ValueError):
            s.cancel(now=T0, **kw)
    with pytest.raises(ValueError):
        s.set_raid("wipe", 105, T0)
    for index in (1, 0):                                  # no such segment; already ready
        with pytest.raises(ValueError):
            s.ready("a", index, tone())
    with pytest.raises(ValueError):
        PlaybackScheduler(FakeClock(START), random.Random(1), max_voices=0)
    s.ready("gone", 0, tone())                            # a line it does not hold: ignored
    assert s.playing() == {"a"} and s.waiting() == 0


def test_now_defaults_to_the_clock():
    clock = FakeClock(START, yields=1)
    s = PlaybackScheduler(clock, random.Random(1))
    s.set_session(True)
    s.set_human_room(ROOM)
    s.submit(line("a", 101))
    s.submit(line("b", 102))
    s.submit(line("late", 103, ttl=1.0))
    assert played(s.tick()) == []
    run(clock.advance(2.0))
    assert played(s.tick()) == [("late", "dropped", "expired")]


# random traffic against a fake mixer

DUCKS = {0.0, -6.0, -10.0, -12.0}
GUIDS = (101, 102, 103, 104, 105)  # P1b's invented cast
PHASES = ("none", "trash", "combat", "wipe_silence", "wipe_debrief", "kill")
ROOMS = (ROOM, ROOM, ROOM, "raid", "smoke_room", "")
CHANNELS = (ROOM, "raid", "whisper", "whisper", "smoke_room")
FAKE_RATE = 1000  # the fake mixer's samples per second: small clips, the same timing


def clip(rng) -> np.ndarray:
    return np.zeros(rng.randint(100, 1500), np.float32)


def traffic(seed: int, steps: int = 150) -> None:
    rng = random.Random(seed)
    s = PlaybackScheduler(FakeClock(START), random.Random(seed), max_voices=2)
    now = T0
    lines: dict[str, Line] = {}
    history: dict[str, list[str]] = {}
    mixer: dict[str, float] = {}       # each sounding voice, and when what it was handed runs out
    stopped: list[str] = []            # stopped voices, whose end a mixer reports after the fade

    def apply(plan):
        for p in plan.played:
            assert p.id in lines, p
            encode(p, 1)                                  # a state and reason the protocol has
            history.setdefault(p.id, []).append(p.state)
        begun = {i for i, _, _ in plan.starts}
        assert {p.id for p in plan.played if p.state == "started"} <= begun   # it sounds
        assert not begun & {i for i, _, _ in plan.stops}
        for i, fade, _ in plan.stops:
            assert i in mixer and fade in (40, 150), (i, fade)
            del mixer[i]
            stopped.append(i)
        assert set(plan.gains) <= set(mixer) and not set(plan.gains) & begun
        assert set(plan.gains.values()) <= DUCKS
        for i, audio, gain in plan.starts:
            assert i not in mixer and history[i][-1] == "started"
            assert gain - (2.0 if lines[i].tier == 0 else 0.0) in DUCKS
            mixer[i] = now + len(audio) / FAKE_RATE
        for i, audio in plan.appends:
            assert i in mixer
            mixer[i] += len(audio) / FAKE_RATE
        playing = s.playing()
        assert set(mixer) <= playing and len(playing) <= 2
        assert len({lines[i].guid for i in playing}) == len(playing)   # nobody overlaps themself

    def mix():
        while due := sorted(i for i, end in mixer.items() if end <= now):
            del mixer[due[0]]
            apply(s.finished(due[0], now))

    room, online = ROOM, True
    apply(s.set_session(True, now))
    apply(s.set_human_room(room, now))
    for _ in range(steps):
        now += rng.uniform(0.0, 0.3)
        mix()
        if not online and rng.random() < 0.25:            # he comes back soon
            online = True
            apply(s.set_session(True, now))
        r = rng.random()
        if r < 0.4:
            lid, guid = f"L{len(lines)}", rng.choice(GUIDS)
            tier = rng.choice((0, 1, 2, 3, 3, 4, 4, 4))
            channel = room if room and rng.random() < 0.7 else rng.choice(CHANNELS)
            theirs = [i for i, ln in lines.items() if ln.guid == guid]
            after = ""
            if lines and rng.random() < 0.25:                 # often their own member's line
                after = rng.choice(theirs if theirs and rng.random() < 0.5
                                   else [*lines, "never-seen"])
            kind = "wipe_react" if rng.random() < 0.15 else KINDS[tier]
            segments = [clip(rng) if rng.random() < 0.7 else None
                        for _ in range(rng.randint(1, 3))]
            lines[lid] = Line(lid, guid, tier, kind, channel, channel == "whisper",
                              now + rng.uniform(-0.5, 20.0), after, segments,
                              template=rng.random() < 0.2)
            apply(s.submit(lines[lid], now))
        elif r < 0.6:                                     # mostly lines still live, some ended
            live = [i for i in lines if history.get(i) in (None, ["started"])]
            pending = [(i, k) for i in (live if rng.random() < 0.8 else list(lines))
                       for k, seg in enumerate(lines[i].segments) if seg is None]
            if pending:
                s.ready(*rng.choice(pending), clip(rng))
            apply(s.tick(now))
        elif r < 0.65:
            which = rng.randrange(3)
            if which == 0 and lines:
                apply(s.cancel(id=rng.choice(list(lines)), now=now))
            elif which == 1:
                apply(s.cancel(guid=rng.choice(GUIDS), now=now))
            else:
                apply(s.cancel(below=rng.randrange(5), now=now))
        elif r < 0.73:
            apply(s.set_ptt(rng.random() < 0.5, now))
        elif r < 0.78:
            apply(s.set_raid(rng.choice(PHASES), rng.choice(GUIDS), now))
        elif r < 0.83:
            room = rng.choice(ROOMS)
            apply(s.set_human_room(room, now))
        elif r < 0.85:
            online = False
            apply(s.set_session(False, now))
        elif r < 0.9 and stopped:
            apply(s.finished(stopped.pop(rng.randrange(len(stopped))), now))
        elif r < 0.92:                                    # a quiet stretch: ids are forgotten
            now += rng.uniform(30.0, 150.0)
            mix()
            apply(s.tick(now))
        else:
            apply(s.tick(now))
    apply(s.set_ptt(False, now))
    for _ in range(4000):                                 # up to 200 s more, no new lines
        if not (s.playing() or s.waiting() or mixer):
            break
        now += 0.05
        mix()
        apply(s.tick(now))
    assert not (s.playing() or s.waiting() or mixer), seed
    for lid in lines:
        assert history.get(lid) in (["dropped"], ["started", "done"], ["started", "cut"]), (
            seed, lid, history.get(lid))


def test_every_line_ends_once():
    """Random traffic against a fake mixer: every line ends exactly once (dropped, or started
    and then done or cut), nothing outlives its line, at most two members are audible and no
    member overlaps themself, and each plan hangs together: no stop, append or duck for a voice
    that is not sounding, and only the ducks and tier offsets the rules name."""
    for seed in range(100):
        traffic(seed, steps=200)


# -- what the service asks of it (Task 11) ---------------------------------------------------------

def test_it_knows_the_ids_in_use():
    """A speak id is taken from its submit until AFTER_MEMORY_S after it ended."""
    s = sched()
    assert not s.knows("a")
    s.submit(line("a", 101), T0)
    s.submit(line("b", 101), T0)
    assert s.knows("a") and s.knows("b")
    s.finished("a", T0 + 1.0)
    assert s.knows("a")
    s.tick(T0 + 1.0 + AFTER_MEMORY_S + 1.0)
    assert not s.knows("a")


def test_drop_waiting_lets_what_plays_finish():
    """The mind has gone: what waits is dropped with the reason, what plays plays to its end."""
    s = sched()
    s.submit(line("a", 101), T0)
    s.submit(line("b", 101), T0)
    p = s.drop_waiting("mind_gone", T0 + 0.1)
    assert played(p) == [("b", "dropped", "mind_gone")] and not p.stops
    assert s.playing() == {"a"} and s.waiting() == 0
    assert played(s.finished("a", T0 + 1.0)) == [("a", "done", "")]


def test_drop_waiting_takes_a_protocol_reason():
    with pytest.raises(ValueError):
        sched().drop_waiting("no such reason", T0)


def test_a_waiting_line_whose_synthesis_failed_is_dropped():
    s = sched()
    s.submit(line("a", 101, segments=[None, None]), T0)
    s.submit(line("b", 102, after="a"), T0)
    p = s.engine_failed("a", T0 + 0.1)
    assert played(p) == [("a", "dropped", "engine_error"), ("b", "dropped", "after_dropped")]
    assert s.waiting() == 0


def test_a_playing_line_whose_next_segment_failed_ends_with_its_audio():
    """It plays what it has, then is cut at once: its slot is not held for SEGMENT_WAIT_S."""
    s = sched()
    s.submit(line("a", 101, segments=[tone(), None]), T0)
    s.submit(line("b", 101), T0)
    p = s.engine_failed("a", T0 + 0.1)
    assert played(p) == [] and not p.stops and s.playing() == {"a"}
    p = s.finished("a", T0 + 1.0)
    assert played(p) == [("a", "cut", "engine_error"), ("b", "started", "")]
    assert not p.stops  # its audio had ended


def test_a_stalled_line_whose_segment_failed_is_cut_at_once():
    s = sched()
    s.submit(line("a", 101, segments=[tone(), None]), T0)
    s.finished("a", T0 + 1.0)
    assert played(s.engine_failed("a", T0 + 1.1)) == [("a", "cut", "engine_error")]
    assert s.playing() == set()


def test_a_failure_for_a_line_that_ended_is_ignored():
    s = sched()
    s.submit(line("a", 101), T0)
    s.finished("a", T0 + 1.0)
    assert played(s.engine_failed("a", T0 + 1.1)) == []
    assert played(s.engine_failed("never seen", T0 + 1.1)) == []


def test_cancel_may_name_its_reason():
    """His mute cuts what the member is saying, as "muted"."""
    s = sched()
    s.submit(line("a", 101), T0)
    s.submit(line("b", 101), T0)
    p = s.cancel(guid=101, reason="muted", now=T0 + 0.1)
    assert sorted(played(p)) == [("a", "cut", "muted"), ("b", "dropped", "muted")]
    assert p.stops == [("a", CUT_FADE_MS, "muted")]
