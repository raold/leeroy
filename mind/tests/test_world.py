from conftest import HUMAN, OFFICERS, mem
from leeroy_mind.protocol import (AfkEvent, Cast, Chat, Death, Group, Hello, Login, Logout,
                                  Resurrect, Zone)
from leeroy_mind.storage.rows import ChatLineRow
from leeroy_mind.talk.world import OFFLINE_ACK_S, WorldView, looks_like_bot_command

def row(channel, from_guid, text="hi", to_guid=0, zone=10, i=1, ts=0.0):
    return ChatLineRow(id=i, ts=ts, channel=channel, from_guid=from_guid, from_name="x",
                       to_guid=to_guid, text=text, map=0, zone=zone, session_id=None,
                       thread_id=None, origin="game")


def make_world(members=None):
    w = WorldView(is_officer=lambda g: g in OFFICERS)
    w.apply(Hello(ts=0, server="s", version="1", guild_id=1, human_guid=HUMAN), 0.0)
    members = members or [mem(101), mem(102), mem(103), mem(104),
                          mem(105, online=False)]
    w.apply(Cast(ts=0, members=tuple(members)), 0.0)
    return w


def test_bot_command_detection():
    for t in ("follow", "dc on", "co +wait for attack", "attack my target", "summon Arkon",
              "stay", "do pull", "Follow.", "nc +dps", "wait", "follow me to bank"):
        assert looks_like_bot_command(t), t
    for t in ("follow me to the bank guys, drinks are on me", "evening all",
              "who's up for ICC", "do you think we can pull it tonight", "", "   ",
              "do you need heals", "do u need heals", "do we have a tank", "ready?",
              "wait what", "follow me to the bank", "stay on the boss?"):
        assert not looks_like_bot_command(t), t


def test_audience_per_channel():
    w = make_world()
    w.apply(Group(ts=0, leader_guid=HUMAN, raid=False, members=(HUMAN, 101, 103)), 0.0)
    assert w.audience("guild", from_guid=101, to_guid=0, zone=10) == {101, 102, 103, 104}
    assert w.audience("officer", from_guid=102, to_guid=0, zone=10) == {102, 104}
    assert w.audience("whisper", from_guid=HUMAN, to_guid=103, zone=10) == {103}
    assert w.audience("whisper", from_guid=101, to_guid=103, zone=10) == {101, 103}
    assert w.audience("party", from_guid=101, to_guid=0, zone=10) == {101, 103}
    assert w.audience("say", from_guid=HUMAN, to_guid=0, zone=10) == {101, 103}
    assert w.audience("say", from_guid=HUMAN, to_guid=0, zone=99) == frozenset()
    assert w.audience("emote", from_guid=101, to_guid=0, zone=10) == frozenset()
    for ch in ("guild", "officer", "party", "say", "whisper"):
        assert HUMAN not in w.audience(ch, from_guid=HUMAN, to_guid=101, zone=10)


def test_visible_to_respects_audience_at_the_time():
    w = make_world()
    off = w.add_line(row("officer", 102, "keep it quiet", i=1), 1.0)
    whisper = w.add_line(row("whisper", 101, "psst", to_guid=103, i=2), 2.0)
    guild = w.add_line(row("guild", 101, "evening", i=3), 3.0)
    assert w.visible_to(off, 104) and not w.visible_to(off, 103)
    assert w.visible_to(whisper, 103) and w.visible_to(whisper, 101)
    assert not w.visible_to(whisper, 102)
    w.apply(Login(ts=0, guid=105, human=False), 4.0)  # arrives late: sees nothing earlier
    assert not w.visible_to(guild, 105)
    assert [r.text for r in w.recent_visible(103)] == ["psst", "evening"]
    assert [r.text for r in w.recent_visible(103, channel="guild")] == ["evening"]
    assert [r.text for r in w.recent_visible(104, n=1)] == ["evening"]
    assert w.last_spoke[101] == 3.0


def test_the_window_skips_what_keep_refuses_and_still_fills_up():
    w = make_world()
    for i, (ch, frm, to) in enumerate([("guild", 101, 0), ("whisper", HUMAN, 103),
                                       ("guild", 102, 0), ("whisper", 103, 101),
                                       ("guild", 104, 0)], 1):
        w.add_line(row(ch, frm, f"l{i}", to_guid=to, i=i), float(i))
    assert [r.text for r in w.recent_visible(103, n=2)] == ["l4", "l5"]
    public = [r.text for r in w.recent_visible(103, n=2, keep=lambda r: r.channel != "whisper")]
    assert public == ["l3", "l5"]


def test_cast_logout_zone_and_idle():
    w = make_world()
    assert w.online == {101, 102, 103, 104} and w.human_online
    w.apply(Logout(ts=0, guid=103, human=False), 5.0)
    assert 103 not in w.online and not w.members[103].online
    w.apply(Zone(ts=0, guid=101, zone_id=77, zone_name="Somewhere"), 5.0)
    assert w.members[101].zone == 77
    w.apply(Cast(ts=0, members=(mem(105),)), 6.0)  # a push replaces
    assert set(w.members) == {105} and w.online == {105}
    w.apply(Chat(ts=0, channel="guild", from_guid=HUMAN, from_name="h", to_guid=0, text="yo",
                 map=0, zone=10), 1000.0)
    assert not w.human_idle(1000.0 + 599)
    assert w.human_idle(1000.0 + 601)
    assert w.human_idle(1000.0 + 301, idle_s=300)


def test_afk_events():
    w = make_world()
    w.apply(Zone(ts=0, guid=HUMAN, zone_id=10, zone_name="z"), 100.0)
    assert not w.human_idle(101.0)
    w.apply(AfkEvent(ts=0, guid=HUMAN, on=True), 102.0)
    assert w.human_afk and w.human_idle(102.0)
    w.apply(AfkEvent(ts=0, guid=HUMAN, on=False), 103.0)
    assert not w.human_afk and not w.human_idle(103.0)
    w.apply(AfkEvent(ts=0, guid=101, on=True), 104.0)
    assert w.afk == {101}
    w.apply(AfkEvent(ts=0, guid=101, on=False), 105.0)
    assert w.afk == set()


def test_group_empty_event_clears_group():
    w = make_world()
    w.apply(Group(ts=0, leader_guid=HUMAN, raid=True, members=(HUMAN, 101)), 0.0)
    assert w.group is not None and w.in_group(101) and not w.in_group(103)
    w.apply(Group(ts=0, leader_guid=0, raid=False, members=()), 1.0)
    assert w.group is None and not w.in_group(101)


def test_hello_keeps_last_nonzero_human_guid():
    w = make_world()
    w.apply(Hello(ts=0, server="s", version="1", guild_id=1, human_guid=0), 1.0)
    assert w.human_guid == HUMAN


def test_production_startup_order_attributes_events_to_the_human():
    """The mind starts first: hello carries guid 0, the human is not in the cast."""
    w = WorldView(is_officer=lambda g: g in OFFICERS)
    w.apply(Hello(ts=0, server="s", version="1", guild_id=1, human_guid=0), 0.0)
    w.apply(Cast(ts=0, members=(mem(101), mem(102))), 0.0)
    assert not w.human_online and w.human_idle(5000.0)
    w.apply(Login(ts=0, guid=HUMAN, human=True), 5000.0)
    assert w.human_guid == HUMAN and w.human_online and not w.human_idle(5001.0)
    w.apply(AfkEvent(ts=0, guid=HUMAN, on=True), 5002.0)
    assert w.human_afk and w.afk == set()
    w.apply(AfkEvent(ts=0, guid=HUMAN, on=False), 5003.0)
    w.apply(Zone(ts=0, guid=HUMAN, zone_id=44, zone_name="z"), 5100.0)
    assert w.human_zone == 44 and w.human_last_active == 5100.0
    w.apply(Chat(ts=0, channel="guild", from_guid=HUMAN, from_name="h", to_guid=0, text="yo",
                 map=0, zone=45), 5200.0)
    assert w.human_last_active == 5200.0 and w.human_zone == 45
    w.apply(Logout(ts=0, guid=HUMAN, human=True), 5300.0)
    assert not w.human_online


def test_a_new_hello_clears_every_afk_flag():
    """Correctness I2 (ruling): after a worldserver restart (ac-server.sh stop is a hard kill,
    so no logout arrives) the bridge republishes only the players who ARE afk. The mind's old
    flags would leave him idle all evening: a hello forgets them all."""
    w = make_world()
    w.apply(AfkEvent(ts=0, guid=HUMAN, on=True), 1.0)
    w.apply(AfkEvent(ts=0, guid=101, on=True), 1.0)
    assert w.human_afk and w.afk == {101}  # positive control
    w.apply(Hello(ts=0, server="s", version="1", guild_id=1, human_guid=0), 2.0)
    w.apply(Cast(ts=0, members=(mem(101), mem(102), mem(103), mem(104))), 2.0)
    w.apply(Login(ts=0, guid=HUMAN, human=True), 3.0)
    assert not w.human_afk and not w.human_idle(3.5) and w.afk == set()
    w.apply(AfkEvent(ts=0, guid=101, on=True), 4.0)  # the bridge's poll re-sends a real one
    w.apply(AfkEvent(ts=0, guid=HUMAN, on=True), 4.0)
    assert w.afk == {101} and w.human_afk
    w.apply(Hello(ts=0, server="s", version="1", guild_id=1, human_guid=HUMAN), 5.0)
    assert w.afk == set() and not w.human_afk  # a hello with him online, too


def test_restart_hello_with_guid_marks_the_human_online():
    w = WorldView(is_officer=lambda g: False)
    w.apply(Hello(ts=0, server="s", version="1", guild_id=1, human_guid=HUMAN), 700.0)
    w.apply(Cast(ts=0, members=(mem(101),)), 700.0)
    assert w.human_online and not w.human_idle(701.0)
    assert w.cast_guids() == {101}


def test_an_offline_ack_takes_her_out_until_she_shows_up_again():
    """Correctness M3, liveops M4: one "offline" ack (a bot between maps on a far teleport)
    took her out of `online` until the next cast or reconnect, and pinned bots never log out,
    so the cast would fall silent one by one. Any event of hers brings her back, and the mark
    runs out after OFFLINE_ACK_S."""
    w = make_world()
    w.mark_offline(101, 1000.0)
    assert 101 not in w.online
    w.apply(Zone(ts=0, guid=101, zone_id=12, zone_name="Dalaran"), 1001.0)
    assert 101 in w.online and w.members[101].zone == 12
    for ev in (Death(ts=0, guid=101, map=631, killer_entry=0), Resurrect(ts=0, guid=101),
               AfkEvent(ts=0, guid=101, on=False), Chat(ts=0, channel="guild", from_guid=101,
                                                       from_name="x", to_guid=0, text="back",
                                                       map=0, zone=12)):
        w.mark_offline(101, 1100.0)
        w.apply(ev, 1101.0)
        assert 101 in w.online, ev
    w.mark_offline(101, 1200.0)
    w.refresh(1200.0 + OFFLINE_ACK_S - 1)
    assert 101 not in w.online
    w.refresh(1200.0 + OFFLINE_ACK_S)
    assert 101 in w.online
    w.mark_offline(102, 1300.0)  # a real logout is never undone
    w.apply(Logout(ts=0, guid=102, human=False), 1301.0)
    w.refresh(2000.0)
    assert 102 not in w.online
    w.mark_offline(103, 1300.0)
    w.apply(AfkEvent(ts=0, guid=HUMAN, on=True), 1301.0)  # his events are not hers
    assert 103 not in w.online
