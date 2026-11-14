"""The raid lead's smaller parts: its switch, the default rule, the talk's lead, the control op."""

import pytest

from leeroy_mind.endpoint import BadRequest, raidlead_args
from leeroy_mind.protocol import Group
from leeroy_mind.raidlead.config import load
from leeroy_mind.raidlead.designate import default_lead, nights
from leeroy_mind.talk.raid import RaidState
from leeroy_mind.talk.world import WorldView


@pytest.mark.parametrize("text, on", [
    ("", False), ("[raidlead]\nenabled = true\n", True), ("[raidlead]\nenabled = false\n", False),
    ('[raidlead]\nenabled = "yes"\n', False), ("[raidlead]\nenabled = true\nmodel = 1\n", False),
    ("raidlead = 3\n", False), ("[raidlead\n", False),
])
def test_switch(tmp_path, text, on):
    (tmp_path / "mind.toml").write_text(text, encoding="utf-8")
    assert load(tmp_path / "mind.toml").enabled is on


def test_switch_without_a_file_is_off(tmp_path):
    assert load(tmp_path / "missing.toml").enabled is False


def test_default_rule_counts_nights_led_then_attended(store):
    conn = store.conn
    for lead in (102, 102, 103):
        conn.execute("INSERT INTO guild_raid(started,raid_key,size,heroic,offline,lead_guid,tank_guid,"
                     "loot_guid) VALUES (1,'icc',10,0,0,?,?,?)", (lead, lead, lead))
    # an offline raid led is no raid night he led
    conn.execute("INSERT INTO guild_raid(started,raid_key,size,heroic,offline,lead_guid,tank_guid,"
                 "loot_guid) VALUES (1,'icc',10,0,1,103,103,103)")
    for raid_id in (1, 2, 3):
        conn.execute("INSERT INTO guild_attendance(raid_id,guid,status) VALUES (?,104,'raided')", (raid_id,))
    conn.execute("INSERT INTO guild_attendance(raid_id,guid,status) VALUES (1,105,'benched')")
    conn.commit()
    assert nights(conn, [102, 103, 104, 105]) == {102: (2, 0), 103: (1, 0), 104: (0, 3), 105: (0, 0)}
    assert default_lead(conn, [102, 103, 104]) == 102
    assert default_lead(conn, [103, 104]) == 103
    assert default_lead(conn, [104, 105]) == 104
    assert default_lead(conn, [105, 106], titled=[106]) == 106
    assert default_lead(conn, [106, 105]) == 105
    assert default_lead(conn, []) is None


def test_the_named_lead_leads_the_talk_and_off_changes_nothing(clock):
    world = WorldView(is_officer=lambda g: g == 102)
    world.members = {}
    raid = RaidState(world=world, personas=lambda g: None, clock=clock)
    world.set_group(Group(0, 1, True, (1, 101, 102)))
    before = raid.lead()
    raid.set_designated(None)
    assert raid.lead() == before
    raid.set_lead(1)
    raid.set_designated(101)
    assert raid.lead() == 101
    raid.set_designated(None)
    assert raid.lead() == 1


@pytest.mark.parametrize("args, out", [
    ({"action": "set", "guid": 101}, {"action": "set", "guid": 101}),
    ({"action": "clear"}, {"action": "clear"}),
])
def test_control_op_arguments(args, out):
    assert raidlead_args(args) == out


@pytest.mark.parametrize("args", [
    None, {}, {"action": "promote"}, {"action": "set"}, {"action": "set", "guid": "101"},
    {"action": "set", "guid": 0}, {"action": "set", "guid": 1 << 32}, {"action": "set", "guid": True},
    {"action": "set", "guid": 101, "x": 1}, {"action": "clear", "guid": 101},
])
def test_control_op_refuses_anything_else(args):
    with pytest.raises(BadRequest):
        raidlead_args(args)


@pytest.mark.parametrize("text, state", [
    ("[raidlead]\nenabled = true\n", "on"), ("", "off"), ('[raidlead]\nenabled = "x"\n', "invalid"),
    ("[raidlead]\nenabled = true\nmore = 1\n", "invalid"),
])
def test_the_ops_switches_list_the_raid_lead(tmp_path, text, state):
    import time
    from leeroy_mind.console import opsview
    (tmp_path / "mind.toml").write_text(text, encoding="utf-8")
    rows = opsview.switches_view(tmp_path, time.monotonic() + 10, mind_started=None)["rows"]
    [row] = [r for r in rows if r["table"] == "raidlead" and r["key"] == "enabled"]
    assert row["state"] == state


def test_raids_she_led_because_he_named_her_are_no_nights_led(store):
    """P3-9: only raid nights the mind called count toward her nights led (and last_led)."""
    from leeroy_mind.guild.store import NAMED_LEAD_KEY, last_led
    conn = store.conn
    for lead in (102, 103):
        conn.execute("INSERT INTO guild_raid(started,raid_key,size,heroic,offline,lead_guid,tank_guid,"
                     "loot_guid) VALUES (1,'icc',10,0,0,?,?,?)", (lead, lead, lead))
    conn.execute("INSERT INTO meta(key,value) VALUES (?, '102')", (NAMED_LEAD_KEY.format(raid_id=1),))
    conn.commit()
    assert nights(conn, [102, 103]) == {102: (0, 0), 103: (1, 0)}
    assert 102 not in last_led(conn) and 103 in last_led(conn)


def test_boss_plan_prefers_the_heroic_call_on_heroic():
    from leeroy_mind.guild.boss_calls import BOSS_CALLS, HEROIC_CALLS
    from leeroy_mind.raidlead.callouts import BossContext, boss_plan
    key = next((k for k in HEROIC_CALLS if k in BOSS_CALLS and HEROIC_CALLS[k].pre != BOSS_CALLS[k].pre), None)
    if key is None:
        pytest.skip("no boss with a distinct heroic call in this build")
    assert boss_plan(BossContext(*key, heroic=True)) == HEROIC_CALLS[key].pre
    assert boss_plan(BossContext(*key)) == BOSS_CALLS[key].pre
