import random
from datetime import datetime

from leeroy_mind.talk.budget import PHASE_MODS, BurstBudget, DayPhase, day_phase
from leeroy_mind.talk.raid import RaidPhase

P = RaidPhase


def simulate(chattiness, *, phase=DayPhase.EVENING, idle=False, raid=P.NONE, hours=50, seed=7):
    b = BurstBudget(chattiness, random.Random(seed))
    now, end, lines, plans = 0.0, hours * 3600.0, 0, []
    while now < end:
        plan = b.tick(now, phase=phase, human_idle=idle, raid=raid)
        if plan is None:
            now += 1.0
            continue
        plans.append((now, plan))
        lines += plan.size
        if raid is P.NONE:
            now += plan.size * 20.0
            b.burst_done(now)
    return lines / hours, plans


def test_day_phase_bounds():
    def d(h):
        return day_phase(datetime(2026, 10, 30, h, 0))
    assert [d(10), d(16), d(17), d(22), d(23), d(2), d(3), d(5)] == [
        DayPhase.DAY, DayPhase.DAY, DayPhase.EVENING, DayPhase.EVENING, DayPhase.LATE,
        DayPhase.LATE, DayPhase.DEAD, DayPhase.DEAD]
    assert PHASE_MODS[DayPhase.LATE].max_speakers == 2


def test_mornings_are_not_the_small_hours():
    """Prompts minor 10: the small hours ran 03:00-10:00, so a 09:00 prompt said "It's the
    small hours." and the romance text "It's late". Morning is 06:00-10:00, as quiet as the
    small hours but with ordinary line lengths."""
    def d(h, m=0):
        return day_phase(datetime(2026, 10, 30, h, m))
    assert [d(5, 59), d(6), d(9), d(9, 59), d(10)] == [
        DayPhase.DEAD, DayPhase.MORNING, DayPhase.MORNING, DayPhase.MORNING, DayPhase.DAY]
    mods = PHASE_MODS[DayPhase.MORNING]
    assert mods.max_speakers == 2 and mods.length_mult == 1.0
    assert mods.gap_mult == PHASE_MODS[DayPhase.DEAD].gap_mult


def test_budget_rate_at_4():
    assert 20 <= simulate(4)[0] <= 30


def test_budget_scales_and_zero_is_silent():
    assert simulate(0, hours=10)[0] == 0 and simulate(8)[0] >= 40


def test_first_burst_is_one_gap_after_first_tick():
    b = BurstBudget(4, random.Random(1))
    assert b.tick(1000.0, phase=DayPhase.EVENING, human_idle=False, raid=P.NONE) is None
    for seed in range(5):
        b = BurstBudget(4, random.Random(seed))
        b.tick(1000.0, phase=DayPhase.EVENING, human_idle=False, raid=P.NONE)
        assert b.tick(1239.0, phase=DayPhase.EVENING, human_idle=False, raid=P.NONE) is None
        assert b.tick(1721.0, phase=DayPhase.EVENING, human_idle=False, raid=P.NONE) is not None


def test_budget_late_and_idle():
    evening, _ = simulate(4)
    late, plans = simulate(4, phase=DayPhase.LATE)
    assert late < evening and all(p.max_speakers == 2 for _, p in plans)
    assert simulate(4, idle=True)[0] < evening / 2


def test_budget_raid():
    _, plans = simulate(4, raid=P.TRASH, hours=2)
    assert all(p.size == 1 and p.channel_hint == "raid" for _, p in plans)
    gaps = [b[0] - a[0] for a, b in zip(plans, plans[1:])]
    assert gaps and all(90 <= g <= 155 for g in gaps)
    for ph in (P.COMBAT, P.WIPE_SILENCE, P.WIPE_DEBRIEF):
        assert simulate(4, raid=ph, hours=2)[1] == []


def test_burst_waits_for_burst_done():
    b = BurstBudget(8, random.Random(3))
    now = 0.0
    plan = None
    while plan is None:
        plan = b.tick(now, phase=DayPhase.EVENING, human_idle=False, raid=P.NONE)
        now += 5
    assert b.tick(now + 200, phase=DayPhase.EVENING, human_idle=False, raid=P.NONE) is None


def test_set_chattiness_zero_goes_silent():
    b = BurstBudget(4, random.Random(5))
    b.set_chattiness(0)
    for t in range(0, 20000, 5):
        assert b.tick(float(t), phase=DayPhase.EVENING, human_idle=False, raid=P.NONE) is None


def t(b, now, **kw):
    kw = {"phase": DayPhase.EVENING, "human_idle": False, "raid": P.NONE, **kw}
    return b.tick(now, **kw)


def test_combat_first_tick_then_raid_end_draws_fresh_gap():
    b = BurstBudget(4, random.Random(2))
    assert t(b, 0.0, raid=P.COMBAT) is None
    assert t(b, 100.0) is None
    assert t(b, 100.0 + 239) is None  # a fresh social gap, not 60 s


def test_combat_hold_defers_a_due_burst():
    b = BurstBudget(8, random.Random(2))
    t(b, 0.0)
    assert t(b, 5000.0, raid=P.COMBAT) is None
    assert t(b, 5030.0) is None  # pushed back at least 60 s from the fight tick
    assert t(b, 5061.0) is not None


def test_abandoned_burst_times_out_once():
    b = BurstBudget(4, random.Random(9))
    now = 0.0
    while t(b, now) is None:
        now += 5
    started = now
    assert t(b, started + 299) is None
    fires = [x for x in range(int(started) + 300, int(started) + 300 + 200)
             if t(b, float(x)) is not None]
    assert fires == []  # a fresh gap (>= 240 s) follows the timeout, no instant re-fire


def test_mode_switch_redraws():
    b = BurstBudget(4, random.Random(4))
    t(b, 0.0)
    assert t(b, 10.0, raid=P.TRASH) is None  # raid cadence drawn from now
    assert t(b, 10.0 + 151, raid=P.TRASH) is not None
    assert t(b, 10.0 + 151 + 1) is None  # back to social: fresh long gap


def test_idle_to_active_pulls_the_gap_in():
    b = BurstBudget(4, random.Random(6))
    t(b, 0.0, human_idle=True)  # gap >= 720 s
    assert t(b, 100.0, human_idle=False) is None
    assert any(t(b, 100.0 + x, human_idle=False) is not None for x in range(0, 900))


def test_set_chattiness_unchanged_and_rescale():
    b = BurstBudget(4, random.Random(8))
    t(b, 0.0)
    nxt = b._next_at
    b.set_chattiness(4)
    assert b._next_at == nxt
    b.set_chattiness(8)
    assert abs(b._next_at - nxt / 2) < 1e-6
    b.set_chattiness(0)
    assert t(b, 99999.0) is None
    b.set_chattiness(4)
    assert t(b, 100000.0) is None  # a fresh gap after being silent


def test_a_burst_due_in_the_login_quiet_waits_for_its_end():
    """His login's quiet (reactions.LOGIN_QUIET_S): the budget keeps its schedule from the
    login, and a burst that falls due inside the quiet comes when it ends, not a gap later."""
    def tick(b, now, until=None):
        return b.tick(now, phase=DayPhase.EVENING, human_idle=False, raid=P.NONE,
                      quiet_until=until)
    for seed in range(8):
        b = BurstBudget(10, random.Random(seed))  # gaps of 96-288 s
        assert tick(b, 0.0, 280.0) is None
        assert all(tick(b, t, 280.0) is None for t in range(1, 280))
        assert any(tick(b, t) is not None for t in range(280, 290))
    late = BurstBudget(10, random.Random(1))  # a burst due well after the quiet is not moved
    assert tick(late, 0.0, 30.0) is None
    first = next(t for t in range(1, 400) if tick(late, float(t), 30.0) is not None)
    assert 96 <= first <= 288
