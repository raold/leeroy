import random
from datetime import datetime, timedelta, timezone

from conftest import make_persona
from leeroy_mind.talk.states import (EFFECTS, Altered, advance_state, in_window,
                                     roll_session_state, state_line, substance_of)

UTC = timezone.utc
EVE = datetime(2026, 10, 29, 20, 0, tzinfo=UTC)  # a Thursday


def habit(substance="alcohol", when="daily", odds=1.0, max_state="drunk"):
    return {"substance": substance, "when": when, "odds": odds, "max_state": max_state}


def persona(*habits, guid=101):
    return make_persona(guid, "Arkon", habits=list(habits))


def roll(p, local, last=None, seed=0):
    return roll_session_state(p, local, last, local.timestamp(), random.Random(seed))


def run_session(p, start, seed, minutes=360, state=None):
    """Advance every 10 minutes for `minutes`; returns the state after each check."""
    rng = random.Random(seed)
    s = state if state is not None else roll(p, start, seed=seed)
    out = []
    for m in range(10, minutes + 1, 10):
        s = advance_state(p, s, m, start + timedelta(minutes=m), rng)
        out.append((m, s))
    return out


def test_effects_table():
    assert set(EFFECTS) == set(Altered)
    assert EFFECTS[Altered.SOBER] == (1, 1, 1, 1, 1)
    assert EFFECTS[Altered.TIPSY] == (1.5, 1.2, 1.3, 1.0, 1.3)
    assert EFFECTS[Altered.DRUNK] == (2.5, 1.4, 1.6, 1.1, 1.5)
    assert EFFECTS[Altered.HIGH] == (1.2, 1.3, 1.2, 1.6, 0.8)
    assert EFFECTS[Altered.WIRED] == (1.1, 1.0, 1.0, 0.9, 1.1)  # coffee is a mild lift
    assert EFFECTS[Altered.HUNGOVER] == (1.0, 0.7, 0.8, 1.2, 0.6)
    assert EFFECTS[Altered.DRUNK].typo_mult == 2.5 and EFFECTS[Altered.HUNGOVER].talk_mult == 0.6
    assert Altered("drunk") is Altered.DRUNK  # the stored altered_state round-trips


def test_sober_persona_stays_sober():
    p = persona()
    for hour in range(24):
        local = EVE.replace(hour=hour)
        for seed in range(10):
            assert roll(p, local, seed=seed) is Altered.SOBER
            assert all(s is Altered.SOBER for _, s in run_session(p, local, seed))


def test_hungover_after_a_drunk_night():
    p = persona(habit())
    now = EVE.timestamp()
    recent = [roll(p, EVE, (Altered.DRUNK, now - 10 * 3600), s) for s in range(1000)]
    assert Altered.HUNGOVER in recent
    assert 0.5 < recent.count(Altered.HUNGOVER) / 1000 < 0.7  # p 0.6
    assert set(recent) == {Altered.HUNGOVER, Altered.SOBER}  # 20:00: a drinker starts sober
    stale = [roll(p, EVE, (Altered.DRUNK, now - 30 * 3600), s) for s in range(300)]
    assert Altered.HUNGOVER not in stale
    tipsy = [roll(p, EVE, (Altered.TIPSY, now - 10 * 3600), s) for s in range(300)]
    assert Altered.HUNGOVER not in tipsy
    stored = [roll(p, EVE, ("drunk", now - 10 * 3600), s) for s in range(50)]  # a db string
    assert Altered.HUNGOVER in stored


def test_alcohol_starts_sober_then_tipsy_from_22():
    p = persona(habit())
    assert roll(p, EVE.replace(hour=21, minute=59)) is Altered.SOBER
    assert roll(p, EVE.replace(hour=22, minute=0)) is Altered.TIPSY
    assert roll(p, datetime(2026, 10, 30, 1, 30, tzinfo=UTC)) is Altered.TIPSY


def test_advance_never_exceeds_max_state():
    tipsy_only = persona(habit(max_state="tipsy"))
    seen = set()
    for seed in range(50):
        for m, s in run_session(tipsy_only, EVE, seed):
            assert s is not Altered.DRUNK
            seen.add(s)
    assert Altered.TIPSY in seen  # positive control: the drinker does get tipsy

    drunk_ok = persona(habit(max_state="drunk"))
    reached = set()
    for seed in range(50):
        for m, s in run_session(drunk_ok, EVE, seed):
            if s is Altered.TIPSY:
                assert m >= 60
            if s is Altered.DRUNK:
                assert m >= 150
            reached.add(s)
    assert Altered.DRUNK in reached


def test_weed_after_midnight_goes_high():
    p = persona(habit("weed", "after_midnight", 1.0, "high"))
    late = datetime(2026, 10, 30, 0, 30, tzinfo=UTC)
    for seed in range(20):
        assert roll(p, late, seed=seed) is Altered.HIGH
        assert roll(p, EVE.replace(hour=21), seed=seed) is not Altered.HIGH
    # A session that started at 21:00 lights up once the window opens, never before it.
    start = EVE.replace(hour=21)
    for seed in range(20):
        states = run_session(p, start, seed, minutes=300)
        assert all(s is not Altered.HIGH for m, s in states if m < 180)
        assert any(s is Altered.HIGH for m, s in states)


def test_caffeine_wires_and_tobacco_changes_nothing():
    coffee = persona(habit("caffeine", "daily", 1.0, "wired"))
    nights = [roll(coffee, EVE + timedelta(days=d)) for d in range(60)]
    assert Altered.WIRED in nights and set(nights) <= {Altered.WIRED, Altered.SOBER}
    smoker = persona(habit("tobacco", "daily", 1.0, "wired"))
    for seed in range(20):
        assert roll(smoker, EVE, seed=seed) is Altered.SOBER
        assert all(s is Altered.SOBER for _, s in run_session(smoker, EVE, seed))
    assert substance_of(smoker, Altered.SOBER) == "tobacco"
    assert substance_of(persona(), Altered.SOBER) is None
    assert substance_of(coffee, Altered.WIRED) == "caffeine"
    assert substance_of(persona(), Altered.HUNGOVER) == "alcohol"


def test_daily_coffee_is_a_mild_lift_on_some_nights_only():
    """The rehearsal: 19 personas roll a daily caffeine habit every night, and every one of
    them read "You're wired on caffeine: quick, restless, jumping in fast." Most nights a
    coffee drinker has just had their usual; on a few they are wired, mildly."""
    coffee = persona(habit("caffeine", "daily", 1.0, "wired"))
    nights = [roll(coffee, EVE + timedelta(days=d)) for d in range(200)]
    share = nights.count(Altered.WIRED) / 200
    assert 0.2 <= share <= 0.45, share
    assert nights == [roll(coffee, EVE + timedelta(days=d), seed=9) for d in range(200)]  # seeded
    others = [roll(persona(habit("caffeine", "daily", 1.0, "wired"), guid=102),
                   EVE + timedelta(days=d)) for d in range(200)]
    assert others != nights  # per member
    wired = EFFECTS[Altered.WIRED]
    assert wired.typo_mult <= 1.15 and wired.delay_mult >= 0.9 and wired.talk_mult <= 1.15
    # a sober coffee night stays sober: the lift is decided for the night, not every check
    for d, s in enumerate(nights[:40]):
        if s is Altered.SOBER:
            n = EVE + timedelta(days=d)
            assert all(x is Altered.SOBER for _, x in run_session(coffee, n, d, state=s))


def test_wired_lines_vary_and_nothing_is_caricature():
    lines = {state_line(Altered.WIRED, "caffeine", variant=v) for v in range(12)}
    assert len(lines) >= 3
    assert not any("jumping in fast" in x for x in lines)
    for s in Altered:
        if s is Altered.SOBER:
            continue
        line = state_line(s, substance_of(persona(), s), variant=3)
        assert "sloppy" not in line and "drifting off topic" not in line
        assert line.endswith("It shows in how you type, and you don't have to mention it.")


def test_a_smokers_line_only_on_a_night_the_habit_is_on():
    """Prompts minor 8: every sober smoker's openers carried "You smoke, and go afk for one now
    and then", whether or not the habit was on tonight."""
    on = persona(habit("tobacco", "daily", 1.0, "wired"))
    off = persona(habit("tobacco", "daily", 0.0, "wired"))
    weekends = persona(habit("tobacco", "weekends", 1.0, "wired"))
    assert substance_of(on, Altered.SOBER, EVE) == "tobacco"
    assert substance_of(off, Altered.SOBER, EVE) is None
    assert substance_of(weekends, Altered.SOBER, EVE) is None  # a Thursday
    assert substance_of(weekends, Altered.SOBER, EVE + timedelta(days=1)) == "tobacco"  # Friday


def test_odds_are_decided_once_a_night():
    p = persona(habit("weed", "daily", 0.5, "high"))
    nights = [EVE + timedelta(days=d) for d in range(60)]
    states = [roll(p, n, seed=d) for d, n in enumerate(nights)]
    assert Altered.HIGH in states and Altered.SOBER in states
    for d, (n, s) in enumerate(zip(nights, states)):
        if s is Altered.SOBER:  # not smoking tonight: no later check changes that
            assert all(x is Altered.SOBER for _, x in run_session(p, n, d, state=s))
    never = persona(habit("weed", "daily", 0.0, "high"))
    assert all(roll(never, n) is Altered.SOBER for n in nights)


def test_windows():
    fri_1659 = datetime(2026, 10, 30, 16, 59, tzinfo=UTC)
    fri_1700 = datetime(2026, 10, 30, 17, 0, tzinfo=UTC)
    sun_2359 = datetime(2026, 11, 1, 23, 59, tzinfo=UTC)
    mon_0000 = datetime(2026, 11, 2, 0, 0, tzinfo=UTC)
    assert not in_window("weekends", EVE, 101) and not in_window("weekends", fri_1659, 101)
    assert in_window("weekends", fri_1700, 101) and in_window("weekends", sun_2359, 101)
    assert not in_window("weekends", mon_0000, 101)
    assert in_window("raid_nights", fri_1700, 101) and not in_window("raid_nights", fri_1659, 101)
    assert in_window("raid_nights", EVE.replace(hour=2, minute=59), 101)
    assert not in_window("raid_nights", EVE.replace(hour=3), 101)
    assert in_window("after_midnight", EVE.replace(hour=0), 101)
    assert in_window("after_midnight", EVE.replace(hour=3, minute=59), 101)
    assert not in_window("after_midnight", EVE.replace(hour=4), 101)
    assert all(in_window("daily", EVE.replace(hour=h), 101) for h in range(24))
    days = [EVE + timedelta(days=d) for d in range(700)]
    off = [in_window("days_off", d, 101) for d in days]
    assert 0.36 < sum(off) / len(off) < 0.50  # 3 days in 7
    assert off == [in_window("days_off", d, 101) for d in days]  # seeded, stable
    assert off != [in_window("days_off", d, 102) for d in days]  # per member
    # one answer for the whole night, also after midnight
    assert in_window("days_off", EVE, 101) == in_window("days_off", EVE + timedelta(hours=5), 101)


def test_state_line():
    assert state_line(Altered.SOBER, None) == ""
    assert "smok" in state_line(Altered.SOBER, "tobacco")
    assert "drunk" in state_line(Altered.DRUNK, "alcohol")
    assert "hungover" in state_line(Altered.HUNGOVER, None)
    for s in Altered:
        if s is not Altered.SOBER:
            line = state_line(s, substance_of(persona(), s))
            assert line and line == line.strip()
            assert not any(c in line for c in ";—–")  # prompt text must not model repairable tells


# -- the night's share of a state (fix wave G1, item 14; rehearsal 2: eleven members wired) ----

def test_no_more_than_a_share_of_the_guild_in_one_state_a_night():
    from leeroy_mind.talk.states import STATE_CAP_MIN, capped_states, state_cap
    assert state_cap(106) == 7 and state_cap(5) == STATE_CAP_MIN
    states = {g: Altered.WIRED for g in range(1, 21)} | {g: Altered.TIPSY for g in range(21, 24)}
    out = capped_states(states, 2, "2026-10-29")
    assert sum(s is Altered.WIRED for s in out.values()) == 2
    assert sum(s is Altered.TIPSY for s in out.values()) == 2
    assert sum(s is Altered.SOBER for s in out.values()) == 19
    assert capped_states(states, 2, "2026-10-29") == out  # the same members on a restart
    assert capped_states(states, 2, "2026-10-30") != out  # another night, others


def test_each_state_reads_several_ways():
    for state in (Altered.TIPSY, Altered.DRUNK, Altered.HIGH, Altered.WIRED, Altered.HUNGOVER):
        lines = {state_line(state, None, variant=v) for v in range(12)}
        assert len(lines) >= 3, state
    assert len({state_line(Altered.WIRED, "caffeine", variant=v) for v in range(16)}) >= 8
