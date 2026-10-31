from dataclasses import replace

from leeroy_mind.protocol import Boss, Cast, Group, Hello
from leeroy_mind.talk.raid import RaidPhase, RaidState
from leeroy_mind.talk.turns import TurnKind
from leeroy_mind.talk.world import WorldView
from conftest import HUMAN, OFFICERS, make_persona, mem, run

ICC, TOC = 631, 649
MARROWGAR, SINDRAGOSA, GAUNTLET = 0, 11, 13


def boss(state, *, map=ICC, boss_id=MARROWGAR, instance=5, members=(101, 102)):
    return Boss(ts=0, map=map, instance_id=instance, boss_id=boss_id, boss_name="", state=state,
                members=members)


def setup(clock, lead_titles=None, leader=HUMAN):
    w = WorldView(is_officer=lambda g: g in OFFICERS)
    w.apply(Hello(ts=0, server="s", version="1", guild_id=1, human_guid=HUMAN), 0.0)
    w.apply(Cast(ts=0, members=tuple(mem(g) for g in (101, 102, 103, 104))), 0.0)
    titles = lead_titles or {}
    r = RaidState(world=w, clock=clock, personas=lambda g: make_persona(
        g, f"P{g}", officer_title=titles.get(g, "")))
    r.on_group(Group(ts=0, leader_guid=leader, raid=True, members=(HUMAN, 101, 102, 103, 104)))
    return w, r


def test_raid_phases_over_time(clock):
    t = clock.now()
    w, r = setup(clock)
    assert r.phase(t) is RaidPhase.TRASH
    r.on_boss(boss("engaged"), t)
    assert r.phase(t + 100) is RaidPhase.COMBAT
    r.on_boss(boss("wiped"), t + 100)
    assert r.phase(t + 109) is RaidPhase.WIPE_SILENCE
    assert r.phase(t + 111) is RaidPhase.WIPE_DEBRIEF
    assert r.phase(t + 159) is RaidPhase.WIPE_DEBRIEF
    assert r.phase(t + 161) is RaidPhase.TRASH
    r.on_boss(boss("engaged"), t + 200)
    r.on_boss(boss("killed"), t + 300)
    assert r.phase(t + 344) is RaidPhase.KILL
    assert r.phase(t + 346) is RaidPhase.TRASH
    r.on_group(Group(ts=0, leader_guid=HUMAN, raid=False, members=(HUMAN, 101)))
    assert r.phase(t + 301) is RaidPhase.NONE
    r.on_group(Group(ts=0, leader_guid=0, raid=False, members=()))
    assert w.group is None and r.phase(t) is RaidPhase.NONE


def test_state_is_keyed_by_map_and_boss(clock):
    t = clock.now()
    _, r = setup(clock)
    # boss index 0 exists in ICC and in Trial of the Crusader
    r.on_boss(boss("engaged", map=ICC, boss_id=0, instance=5), t)
    r.on_boss(boss("wiped", map=TOC, boss_id=0, instance=9), t + 10)  # never engaged there
    assert r.phase(t + 11) is RaidPhase.COMBAT  # the ICC fight is still on
    r.on_boss(boss("wiped", map=ICC, boss_id=0, instance=5), t + 20)
    assert r.wipes_tonight(ICC, 0) == 1 and r.wipes_tonight(TOC, 0) == 1
    assert r.wipes_tonight(ICC, 1) == 0
    r.on_boss(boss("engaged", boss_id=0, instance=5), t + 300)
    r.on_boss(boss("engaged", boss_id=0, instance=6), t + 301)  # another instance
    r.on_boss(boss("killed", boss_id=0, instance=5), t + 400)
    assert r.phase(t + 401) is RaidPhase.COMBAT


def test_non_fight_steps_and_other_states_are_ignored(clock):
    t = clock.now()
    _, r = setup(clock)
    r.on_boss(boss("engaged", boss_id=GAUNTLET), t)
    assert r.phase(t + 1) is RaidPhase.TRASH
    r.on_boss(boss("wiped", boss_id=GAUNTLET), t + 2)
    assert r.wipes_tonight(ICC, GAUNTLET) == 0
    r.on_boss(boss("engaged", map=36, boss_id=1), t)  # not a raid map
    r.on_boss(boss("started"), t)
    assert r.phase(t + 1) is RaidPhase.TRASH


def _wipe_at(clock, r, start, hours):
    run(clock.advance(start + hours * 3600 - clock.now(), step=3600))
    r.on_boss(boss("wiped"), clock.now())


def test_wipes_count_per_night(clock):
    """The night runs 06:00 to 06:00 local (Task 9's night_of). The clock starts at 20:00."""
    t0 = clock.now()
    _, r = setup(clock)
    _wipe_at(clock, r, t0, 3.5)    # 23:30
    _wipe_at(clock, r, t0, 9.75)   # 05:45 next day: the same night
    assert r.wipes_tonight(ICC, MARROWGAR) == 2
    assert r.wipes_tonight(TOC, MARROWGAR) == 0
    _wipe_at(clock, r, t0, 33.0)   # 05:00 a day later: still the second night
    _wipe_at(clock, r, t0, 35.0)   # 07:00: a new night
    assert r.wipes_tonight(ICC, MARROWGAR) == 1
    assert r.wipes_tonight(MARROWGAR, ICC) == 0  # argument order is (map, boss)


def test_combat_ends_after_the_maximum_fight_age(clock):
    t = clock.now()
    _, r = setup(clock)
    r.on_boss(boss("engaged"), t)
    assert r.phase(t + 1199) is RaidPhase.COMBAT
    assert r.phase(t + 1201) is RaidPhase.TRASH


def test_boss_events_from_another_group_are_ignored(clock):
    t = clock.now()
    _, r = setup(clock)  # group: the human and 101-104
    r.on_boss(boss("engaged", members=(901, 902)), t)
    assert r.phase(t + 1) is RaidPhase.TRASH
    r.on_boss(boss("engaged", members=(104, 901)), t)
    assert r.phase(t + 1) is RaidPhase.COMBAT


def test_a_raid_he_is_not_in_ignores_other_groups_fights_too(clock):
    """Task 12 carry: the foreign-fight check applied only while he was in the group, so a
    raid group he had left could be put in COMBAT by another group's boss."""
    t = clock.now()
    w, r = setup(clock)
    r.on_group(Group(ts=0, leader_guid=102, raid=True, members=(101, 102, 103)))
    assert not w.in_group(HUMAN)
    r.on_boss(boss("engaged", members=(901, 902)), t)
    assert r.phase(t + 1) is RaidPhase.TRASH
    r.on_boss(boss("engaged", members=(103, 901)), t)
    assert r.phase(t + 1) is RaidPhase.COMBAT


def test_a_raid_that_forms_keeps_only_its_own_fights(clock):
    """Task 12 carry: a boss engaged while there was no raid group (another group's pull, or a
    stale event) put the raid that formed afterwards straight into COMBAT for twenty minutes."""
    t = clock.now()
    w, r = setup(clock)
    r.on_group(Group(ts=0, leader_guid=0, raid=False, members=()))  # no group
    r.on_boss(boss("engaged", members=(901, 902)), t)  # someone else's pull
    r.on_group(Group(ts=0, leader_guid=HUMAN, raid=True, members=(HUMAN, 101, 102)))
    assert r.phase(t + 1) is RaidPhase.TRASH
    r.on_group(Group(ts=0, leader_guid=0, raid=False, members=()))
    r.on_boss(boss("engaged", members=(101, 102)), t + 2)  # his raid-to-be is already pulling
    r.on_group(Group(ts=0, leader_guid=HUMAN, raid=True, members=(HUMAN, 101, 102)))
    assert r.phase(t + 3) is RaidPhase.COMBAT


def test_a_replayed_fight_counts_when_it_happened(clock):
    """Correctness M2: replayed backlog events were taken at replay time, so a crash mid-raid
    and a restart the next evening put last night's wipes into tonight's count, and a replayed
    pull whose end was lost kept the raid in COMBAT for twenty minutes. A boss event's own
    server time counts (ts 0, as in these tests, means the mind's)."""
    t = clock.now()
    w, r = setup(clock)
    yesterday = int((t - 86400) * 1000)
    r.on_boss(replace(boss("engaged"), ts=yesterday), t)
    assert r.phase(t + 1) is RaidPhase.TRASH
    r.on_boss(replace(boss("engaged", boss_id=SINDRAGOSA), ts=yesterday), t)
    r.on_boss(replace(boss("wiped", boss_id=SINDRAGOSA), ts=yesterday + 95_000), t + 1)
    assert r.wipes_tonight(ICC, SINDRAGOSA) == 0 and r.phase(t + 2) is RaidPhase.TRASH
    assert r.last_fight(ICC, SINDRAGOSA) == ("wiped", 95.0)
    r.on_boss(boss("engaged"), t + 10)
    assert r.phase(t + 11) is RaidPhase.COMBAT


def test_officer_exemption_needs_the_raid(clock):
    t = clock.now()
    w, r = setup(clock, leader=HUMAN)
    r.on_group(Group(ts=0, leader_guid=HUMAN, raid=True, members=(HUMAN, 101, 103, 104)))
    r.on_boss(boss("engaged", members=(101,)), t)
    assert r.allows(104, TurnKind.CALLOUT, t + 1)
    assert not r.allows(102, TurnKind.CALLOUT, t + 1)  # an officer outside the raid
    assert not r.allows(102, TurnKind.REPLY_HUMAN, t + 1)


def test_raid_allows_matrix(clock):
    t = clock.now()
    w, r = setup(clock, leader=HUMAN)
    r.set_lead(101)
    member, lead, officer = 103, 101, 102
    r.on_boss(boss("engaged"), t)
    now = t + 5
    K = TurnKind
    assert not r.allows(member, K.OPEN, now)
    assert r.allows(member, K.URGENT, now)
    assert r.allows(lead, K.REPLY_HUMAN, now)
    assert r.allows(officer, K.CALLOUT, now)
    assert not r.allows(member, K.CALLOUT, now)
    for k in (K.FLIRT, K.ASIDE, K.BIT):
        for g in (member, lead, officer):
            assert not r.allows(g, k, now)
    r.on_boss(boss("wiped"), t + 10)
    for k in K:
        assert r.allows(member, k, t + 15) == (k is K.URGENT), k
    assert r.allows(lead, K.CALLOUT, t + 30)
    assert r.allows(member, K.WIPE_REACT, t + 30)
    assert not r.allows(member, K.CALLOUT, t + 30)
    assert not r.allows(member, K.OPEN, t + 30)
    assert r.allows(member, K.OPEN, t + 100)  # TRASH
    r.on_boss(boss("engaged"), t + 200)
    r.on_boss(boss("killed"), t + 250)
    assert all(r.allows(member, k, t + 260) for k in K)
    r.on_group(Group(ts=0, leader_guid=0, raid=False, members=()))
    r.on_boss(boss("engaged"), t + 300)
    assert all(r.allows(member, k, t + 301) for k in K)


def test_lead_resolution_and_override(clock):
    _, r = setup(clock, leader=101)
    assert r.lead() == 101  # the group leader, a cast member
    _, r = setup(clock, leader=HUMAN)
    assert r.lead() == 102  # first officer in the group
    _, r = setup(clock, leader=HUMAN, lead_titles={104: "Raid Lead"})
    assert r.lead() == 104
    r.set_lead(103)
    assert r.lead() == 103
    r.set_lead(None)
    assert r.lead() == 104
    w, r = setup(clock, leader=HUMAN)
    r.on_group(Group(ts=0, leader_guid=HUMAN, raid=True, members=(HUMAN, 101, 103)))
    assert r.lead() is None  # no officer in the group


def test_the_last_fight_keeps_its_length(clock):
    """What the lead's call and the reactions are told: how long the pull lasted."""
    t = clock.now()
    _, r = setup(clock)
    assert r.last_fight(ICC, MARROWGAR) is None
    r.on_boss(boss("engaged"), t)
    r.on_boss(boss("wiped"), t + 26)
    assert r.last_fight(ICC, MARROWGAR) == ("wiped", 26.0)
    r.on_boss(boss("engaged"), t + 100)
    r.on_boss(boss("killed"), t + 195)
    assert r.last_fight(ICC, MARROWGAR) == ("killed", 95.0)
    assert r.last_fight(ICC, SINDRAGOSA) is None and r.last_fight(TOC, MARROWGAR) is None
    r.on_boss(boss("killed", boss_id=SINDRAGOSA), t + 300)  # its pull was never seen
    assert r.last_fight(ICC, SINDRAGOSA) == ("killed", None)
