import random

from leeroy_mind.talk.guards import Guards
from leeroy_mind.talk.turns import TurnKind

K = TurnKind


def make(seed=1):
    return Guards(rng=random.Random(seed))


def test_guards():
    g = make()
    assert g.allow(101, K.OPEN, 1000.0)
    g.record("t1", 101, 1000.0, K.OPEN)
    assert not g.allow(101, K.OPEN, 1030.0)  # same person twice within 60 s
    assert g.allow(101, K.REPLY_HUMAN, 1030.0)  # replies to the human use 10 s
    assert g.allow(101, K.URGENT, 1030.0)  # urgent skips the per-person limit
    assert g.allow(101, K.CALLOUT, 1030.0)
    assert g.allow(101, K.OPEN, 1061.0)
    # 4 turns in the window: social is blocked, replies are not
    g = make()
    for i, guid in enumerate((101, 102, 103, 104)):
        assert g.allow(guid, K.OPEN, 1000.0 + i)
        g.record(f"t{i}", guid, 1000.0 + i, K.OPEN)
    assert not g.allow(105, K.OPEN, 1005.0)
    for k in (K.CONTINUE, K.BIT, K.ASIDE, K.FLIRT, K.GZ, K.KILL, K.WIPE_REACT):
        assert not g.allow(105, k, 1005.0), k
    for k in (K.REPLY_HUMAN, K.GREET, K.GOODNIGHT, K.WHISPER):
        assert g.allow(105, k, 1005.0), k
    g.record("t5", 105, 1005.0, K.REPLY_HUMAN)
    g.record("t6", 106, 1006.0, K.REPLY_HUMAN)
    assert not g.allow(107, K.REPLY_HUMAN, 1007.0)  # 6 in the window
    assert not g.allow(107, K.URGENT, 1007.0)  # urgent still counts toward the 6
    assert g.allow(107, K.REPLY_HUMAN, 1000.0 + 61)  # the window slides


def test_window_drops_old_turns():
    g = make()
    for i in range(4):
        g.record(f"w{i}", 100 + i, 1000.0, K.OPEN)
    assert not g.allow(200, K.OPEN, 1059.0)
    assert g.allow(200, K.OPEN, 1060.0)


def test_chain_cap_and_human_reset():
    g = make()
    assert g.chain_open("t1")
    for _ in range(3):
        g.chain_line("t1", from_human=False)
    assert g.chain_open("t1")  # 3 cast lines never close it (cap is 4-6)
    closed_at = None
    for n in range(4, 8):
        g.chain_line("t1", from_human=False)
        if not g.chain_open("t1"):
            closed_at = n
            break
    assert closed_at in (4, 5, 6)
    g.chain_line("t1", from_human=True)
    assert g.chain_open("t1")
    assert g.chain_open("other")


def test_chain_cap_is_rolled_in_four_to_six():
    caps = set()
    for seed in range(60):
        g = make(seed)
        n = 0
        while True:
            g.chain_line("k", from_human=False)
            n += 1
            if not g.chain_open("k"):
                break
        caps.add(n)
    assert caps == {4, 5, 6}


def test_replies_to_the_human_use_a_ten_second_interval():
    g = make()
    g.record("q1", 101, 1000.0, K.REPLY_HUMAN)
    assert not g.allow(101, K.REPLY_HUMAN, 1005.0)
    assert g.allow(101, K.REPLY_HUMAN, 1015.0)
    assert not g.allow(101, K.OPEN, 1015.0)  # social talk still waits the full minute
    assert not g.allow(101, K.WHISPER, 1015.0)  # an unprompted whisper does not
    for i in range(5):  # replies still count toward the 6 per minute
        g.record(f"r{i}", 200 + i, 1016.0 + i, K.REPLY_HUMAN)
    assert not g.allow(101, K.REPLY_HUMAN, 1030.0)


def test_the_person_intervals_end_exactly_on_time():
    """Tests lens M1: only 5 s and 15 s were checked, so a reply interval of 6 or 14 s, or
    "<=" for "<", passed every test. Ruling I3's 10 s is the pace of his answers."""
    g = make()
    g.record("q", 101, 1000.0, K.REPLY_HUMAN)
    assert not g.allow(101, K.REPLY_HUMAN, 1009.9) and g.allow(101, K.REPLY_HUMAN, 1010.0)
    g.record("o", 102, 2000.0, K.OPEN)
    assert not g.allow(102, K.OPEN, 2059.9) and g.allow(102, K.OPEN, 2060.0)


def test_an_answer_to_his_whisper_always_may_go_out():
    """Rehearsal 3, check 4: her reply to his whisper was dropped because a guild bit of hers
    had gone out 4 s before (REPLY_S). An answer to his whisper is never held back by her
    lines anywhere else nor by the guild's pace; in guild chat the guards keep their job."""
    g = make()
    g.record("bit", 101, 1000.0, K.BIT)
    assert g.allow(101, K.REPLY_HUMAN, 1004.0, reply=True)
    assert not g.allow(101, K.REPLY_HUMAN, 1004.0)  # a guild answer still waits
    g.record("w", 101, 1005.0, K.REPLY_HUMAN)  # her whisper back
    assert g.allow(101, K.REPLY_HUMAN, 1006.0, reply=True)  # his next whisper is answered
    assert not g.allow(101, K.REPLY_HUMAN, 1006.0)
    for i in range(6):  # the guild's six a minute
        g.record(f"r{i}", 200 + i, 1007.0, K.REPLY_HUMAN)
    assert not g.allow(102, K.REPLY_HUMAN, 1008.0)
    assert g.allow(102, K.REPLY_HUMAN, 1008.0, reply=True)


def test_urgent_and_callout_do_not_set_the_person_clock():
    g = make()
    g.record("c1", 101, 1000.0, K.CALLOUT)
    g.record("c2", 102, 1001.0, K.URGENT)
    assert g.allow(101, K.REPLY_HUMAN, 1002.0)
    assert g.allow(101, K.OPEN, 1002.0)
    g.record("o1", 103, 1003.0, K.OPEN)
    assert not g.allow(103, K.OPEN, 1004.0)


def test_continuation_skips_person_and_guild_checks_and_record_is_idempotent():
    g = make()
    g.record("turn-1", 101, 1000.0, K.OPEN)
    g.record("turn-1", 101, 1002.0, K.OPEN)  # a repeat for the same turn is ignored
    assert len(g._window) == 1
    assert g.allow(101, K.OPEN, 1003.0, continuation=True)
    for i in range(6):
        g.record(f"x{i}", 300 + i, 1004.0, K.OPEN)
    assert not g.allow(101, K.OPEN, 1005.0)
    assert g.allow(101, K.OPEN, 1005.0, continuation=True)
