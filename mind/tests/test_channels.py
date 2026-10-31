import random

from conftest import HUMAN, OFFICERS, mem
from leeroy_mind.protocol import Boss, Cast, Group, Hello
from leeroy_mind.talk.channels import choose_channel
from leeroy_mind.talk.raid import RaidState
from leeroy_mind.talk.turns import TurnKind
from leeroy_mind.talk.world import WorldView

K = TurnKind


class Rng:
    def __init__(self, value):
        self.value = value

    def random(self):
        return self.value


def setup(clock, members=(101, 102, 103, 104)):
    w = WorldView(is_officer=lambda g: g in OFFICERS)
    w.apply(Hello(ts=0, server="s", version="1", guild_id=1, human_guid=HUMAN), 0.0)
    w.apply(Cast(ts=0, members=tuple(mem(g) for g in members)), 0.0)
    return w, RaidState(world=w, clock=clock, personas=lambda g: None)


def pick(kind, speaker, w, r, now=1000.0, rng=None, **kw):
    return choose_channel(kind, speaker=speaker, world=w, raid=r, now=now,
                          rng=rng or Rng(0.99), **kw)


def boss(state):
    return Boss(ts=0, map=631, instance_id=1, boss_id=0, boss_name="", state=state, members=(101, 102))


def test_channels(clock):
    w, r = setup(clock)
    assert pick(K.OPEN, 101, w, r) == "guild"
    assert pick(K.GREET, 101, w, r) == "guild"
    assert pick(K.WHISPER, 101, w, r) == "whisper"
    # 5-man with the human
    w.apply(Group(ts=0, leader_guid=HUMAN, raid=False, members=(HUMAN, 101, 103)), 0.0)
    assert pick(K.OPEN, 101, w, r) == "party"
    assert pick(K.CONTINUE, 101, w, r, opener_channel="party") == "party"
    assert pick(K.OPEN, 104, w, r) == "guild"  # not in the group
    assert pick(K.GZ, 101, w, r) == "guild"
    # raid: trash goes to raid, combat social goes nowhere
    r.on_group(Group(ts=0, leader_guid=HUMAN, raid=True, members=(HUMAN, 101, 102, 103)))
    assert pick(K.OPEN, 101, w, r) == "raid"
    assert pick(K.OPEN, 104, w, r) == "guild"
    r.on_boss(boss("engaged"), 1000.0)
    for k in (K.OPEN, K.CONTINUE, K.BIT, K.ASIDE, K.FLIRT, K.GREET, K.GZ, K.WHISPER):
        assert pick(k, 101, w, r, now=1001.0) is None, k
    assert pick(K.URGENT, 101, w, r, now=1001.0) == "raid"
    assert pick(K.CALLOUT, 102, w, r, now=1001.0) == "raid"
    r.on_boss(boss("wiped"), 1010.0)
    assert pick(K.OPEN, 101, w, r, now=1015.0) is None  # silence
    assert pick(K.OPEN, 101, w, r, now=1040.0) is None  # debrief
    assert pick(K.WIPE_REACT, 101, w, r, now=1040.0) == "raid"
    assert pick(K.OPEN, 101, w, r, now=1100.0) == "raid"  # trash again


def test_officer_branch_is_seeded(clock):
    w, r = setup(clock)
    assert pick(K.OPEN, 102, w, r, rng=Rng(0.05)) == "officer"
    assert pick(K.OPEN, 102, w, r, rng=Rng(0.5)) == "guild"
    assert pick(K.OPEN, 101, w, r, rng=Rng(0.05)) == "guild"  # not an officer
    assert pick(K.CONTINUE, 102, w, r, rng=Rng(0.05), opener_channel="guild") == "guild"
    # one officer online: nobody to talk to
    w2, r2 = setup(clock, members=(101, 102, 103))
    assert pick(K.OPEN, 102, w2, r2, rng=Rng(0.05)) == "guild"
    # a real seeded rng gives a stable share near 10%
    rng = random.Random(7)
    hits = sum(pick(K.OPEN, 102, w, r, rng=rng) == "officer" for _ in range(2000))
    assert 120 < hits < 280


def test_yell_and_say_are_never_chosen(clock):
    w, r = setup(clock)
    w.apply(Group(ts=0, leader_guid=HUMAN, raid=False, members=(HUMAN, 101)), 0.0)
    rng = random.Random(1)
    for k in K:
        for g in (101, 102, 104):
            assert pick(k, g, w, r, rng=rng, opener_channel="guild") not in ("say", "yell")


def test_reply_and_continue_take_the_channel_they_are_given(clock):
    w, r = setup(clock)
    assert pick(K.REPLY_HUMAN, 101, w, r) is None  # the caller must say where
    assert pick(K.REPLY_HUMAN, 101, w, r, reply_to="say") == "say"
    assert pick(K.REPLY_HUMAN, 101, w, r, reply_to="whisper") == "whisper"
    assert pick(K.CONTINUE, 101, w, r) is None
    assert pick(K.CONTINUE, 101, w, r, opener_channel="officer") == "officer"
    w.apply(Group(ts=0, leader_guid=HUMAN, raid=True, members=(HUMAN, 101)), 0.0)
    assert pick(K.CONTINUE, 101, w, r, opener_channel="guild") == "guild"
