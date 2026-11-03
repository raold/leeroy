"""Task 16 room policy over an invented cast and isolated database."""
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from conftest import TEST_CAST, run
from leeroy_mind.personas.io import save_persona
from leeroy_mind.protocol import AfkEvent, Group
from leeroy_mind.voice import movement, wire
from leeroy_mind.voice.movement import Movement
from leeroy_mind.wow import CLASSES, RACES
from voice.conftest import CAST, HUMAN, status


class QuietRng:
    def random(self):
        return 1.0

    def uniform(self, low, high):
        return low

    def randint(self, low, high):
        return low

    def sample(self, values, count):
        return list(values)[:count]

    def choice(self, values):
        return list(values)[0]


async def ready(v):
    v.fake.status = status()
    await v.hello()
    v.app.world.members = dict(v.app.store.cast())
    v.app.world.online = set(CAST)
    v.app.world.human_online = True
    v.ext.rooms.clear()
    v.ext.on_comms = set(CAST)
    v.ext.store.close_all(v.clock.now())
    for guid in (*CAST, HUMAN):
        v.ext.rooms.move(guid, "guild_hall")
        v.ext.store.enter(guid, "guild_hall", 0, v.clock.now(), v.app.session_id)
    result = Movement(v.ext, rng=QuietRng())
    v.ext.movement = result
    v.sent.clear()
    return result


def set_persona(v, guid, **sections):
    p = v.app.personas.get(guid)
    p = replace(p, **{name: replace(getattr(p, name), **values) for name, values in sections.items()})
    _, race, cls = TEST_CAST[guid]
    save_persona(v.app.store, p, source="seed", ts=v.clock.now(), race=RACES[race], class_name=CLASSES[cls])
    v.app.personas.invalidate(guid)
    return p


def elapse(v, seconds):
    v.clock._t += seconds
    return v.clock.now()


def group(v, policy, members=(1, 101, 102), *, leader=102, raid=True):
    event = Group(v._ts(), leader, raid, members)
    v.app.raid.on_group(event)
    policy.on_group(event)


@pytest.mark.parametrize("raid", [False, True])
def test_group_drag_and_disband_preserve_private_and_afk(vapp, raid):
    async def check():
        m = await ready(vapp)
        m.move(102, "private1")
        m.on_afk(AfkEvent(vapp._ts(), 103, True))
        group(vapp, m, (1, 101, 102, 103), raid=raid)
        assert [vapp.ext.rooms.where[g] for g in (1, 101, 102, 103)] == ["raid", "raid", "private1", "afk"]
        assert any(isinstance(msg, wire.Moved) and msg.guid == 101 and msg.by == 102 for msg in vapp.sent)
        group(vapp, m, (), leader=0, raid=False)
        assert vapp.ext.rooms.where[1] == vapp.ext.rooms.where[101] == "guild_hall"
        assert vapp.ext.rooms.where[102] == "private1"
        assert vapp.rows("SELECT room FROM voice_presence WHERE guid=101 AND until IS NULL")[0][0] == "guild_hall"
    run(check())


def test_afk_restore_does_not_restore_a_disbanded_raid(vapp):
    async def check():
        m = await ready(vapp)
        group(vapp, m)
        m.on_afk(AfkEvent(vapp._ts(), 101, True))
        group(vapp, m, (), leader=0, raid=False)
        m.on_afk(AfkEvent(vapp._ts(), 101, False))
        assert vapp.ext.rooms.where[101] == "guild_hall"
        m.move(101, "lore")
        m.on_afk(AfkEvent(vapp._ts(), 101, True))
        m.on_afk(AfkEvent(vapp._ts(), 101, True))
        m.on_afk(AfkEvent(vapp._ts(), 101, False))
        assert vapp.ext.rooms.where[101] == "lore"
    run(check())


def test_officer_huddle_excludes_lead_and_pull_ends_it(vapp, monkeypatch):
    async def check():
        m = await ready(vapp)
        set_persona(vapp, 103, play={"guild_role": "officer"})
        group(vapp, m, (1, 101, 102, 103, 104))
        phase = ["trash"]
        monkeypatch.setattr(vapp.app.raid, "phase", lambda now: phase[0])
        m.tick(elapse(vapp, movement.HUDDLE_EVERY_S))
        assert vapp.ext.rooms.where[102] == "raid"
        assert vapp.ext.rooms.occupants("officers") == {103, 104}
        phase[0] = "combat"
        m.tick(elapse(vapp, 1))  # emergency return does not wait for the 10-second cadence
        assert vapp.ext.rooms.occupants("officers") == set()
        assert vapp.ext.rooms.where[103] == vapp.ext.rooms.where[104] == "raid"
    run(check())


def test_huddle_returns_on_timer_and_manual_move_wins(vapp):
    async def check():
        m = await ready(vapp)
        m.tick(elapse(vapp, movement.HUDDLE_EVERY_S))
        assert vapp.ext.rooms.occupants("officers") == {102, 104}
        m.move(102, "lore")
        m.tick(elapse(vapp, movement.HUDDLE_S[1]))
        assert vapp.ext.rooms.where[102] == "lore"
        assert vapp.ext.rooms.where[104] == "guild_hall"
    run(check())


def test_healers_after_wipe_and_never_during_combat(vapp, monkeypatch):
    async def check():
        m = await ready(vapp)
        set_persona(vapp, 101, play={"raid_role": "healer"})
        group(vapp, m)
        phase = ["wipe_debrief"]
        monkeypatch.setattr(vapp.app.raid, "phase", lambda now: phase[0])
        m.tick(vapp.clock.now())
        phase[0] = "trash"
        m.tick(elapse(vapp, 1))
        assert vapp.ext.rooms.where[101] == "healers"
        phase[0] = "combat"
        m.tick(elapse(vapp, 1))
        assert vapp.ext.rooms.where[101] == "raid"
    run(check())


def test_late_smoke_uses_tonights_state_and_excludes_group(vapp, monkeypatch):
    async def check():
        m = await ready(vapp)
        group(vapp, m, (1, 101), leader=101)
        m.rng.random = lambda: 0.0
        monkeypatch.setattr(movement, "PENALTY_P", 0)
        monkeypatch.setattr(movement, "substance_of", lambda p, altered, local: "tobacco" if p.guid in {101, 102} else None)
        m._next_huddle = float("inf")
        m.tick(elapse(vapp, 3 * 3600))
        assert vapp.ext.rooms.where[101] == "raid"
        assert vapp.ext.rooms.where[102] == "smoke_room"
    run(check())


def test_penalty_roll_once_per_night_and_returns(vapp):
    async def check():
        m = await ready(vapp)
        m.rng.random = lambda: 0.0
        m.tick(vapp.clock.now())
        assert vapp.ext.rooms.where[101] == "penalty_box"
        m.tick(elapse(vapp, movement.PENALTY_S[1]))
        assert vapp.ext.rooms.where[101] == "guild_hall"
        m.tick(elapse(vapp, 10))
        assert not vapp.ext.rooms.occupants("penalty_box")
    run(check())


def test_idle_drift_goes_to_members_class_room(vapp, monkeypatch):
    async def check():
        m = await ready(vapp)
        set_persona(vapp, 101, game={"thinks_as": "theorycrafter"})
        m.rng.random = lambda: 0.0
        monkeypatch.setattr(movement, "PENALTY_P", 0)
        m.tick(elapse(vapp, 600))
        assert vapp.ext.rooms.where[101] == "warriors"
    run(check())


async def invite(v, m, monkeypatch, *, ui=True):
    v.fake.status = status(ui=ui)
    seen = []
    monkeypatch.setattr(v.app.engine.whispers, "maybe", lambda now, trigger, pool: seen.append(pool) or 102)
    speak = AsyncMock()
    monkeypatch.setattr(v.app.engine, "speak", speak)
    await m.invite_tick(elapse(v, 600))
    assert 102 in seen[0] and seen[0][102][0] is v.app.personas.get(102)
    return speak


def test_invite_join_leave_and_private_lock(vapp, monkeypatch):
    async def check():
        m = await ready(vapp)
        speak = await invite(vapp, m, monkeypatch)
        assert vapp.ext.rooms.allowed("private1") == {102, HUMAN}
        assert vapp.ext.rooms.can_enter(103, "private1") == "locked"
        assert any(isinstance(msg, wire.Invite) for msg in vapp.sent)
        assert speak.await_args.kwargs == {"why": "invite:private"}
        m.move(HUMAN, "private1")
        m.tick_rooms(vapp.clock.now())
        m.move(HUMAN, "guild_hall")
        m.tick_rooms(elapse(vapp, 1))
        assert vapp.ext.rooms.allowed("private1") is None
        m.tick_rooms(elapse(vapp, 60))
        assert vapp.ext.rooms.where[102] == "guild_hall" and not m.invites
    run(check())


def test_invite_expires_and_reset_clears_lock(vapp, monkeypatch):
    async def check():
        m = await ready(vapp)
        await invite(vapp, m, monkeypatch)
        m.tick_rooms(elapse(vapp, movement.INVITE_WAIT_S))
        assert vapp.ext.rooms.where[102] == "guild_hall"
        assert vapp.ext.rooms.free_private() == "private1"
        await invite(vapp, m, monkeypatch)
        m.reset(vapp.clock.now())
        assert not m.invites and not m.together.open
        assert vapp.ext.rooms.allowed("private1") is None
    run(check())


def test_ui_closed_keeps_invite_but_ask_uses_typed_path(vapp, monkeypatch):
    async def check():
        m = await ready(vapp)
        speak = await invite(vapp, m, monkeypatch, ui=False)
        turn = speak.await_args.args[0]
        meta = SimpleNamespace(why="invite:private", whisper_reply=False, elsewhere=False)
        assert turn.channel == "whisper" and turn.to_guid == HUMAN
        assert not vapp.ext.comms.voiced(turn, meta)
        assert any(isinstance(msg, wire.Invite) for msg in vapp.sent)
    run(check())


def test_no_free_private_room_does_not_consume_planner(vapp, monkeypatch):
    async def check():
        m = await ready(vapp)
        for room in movement.PRIVATE:
            vapp.ext.rooms.lock(room, {103})
        monkeypatch.setattr(vapp.app.engine.whispers, "maybe", lambda *args: pytest.fail("planner used with no free room"))
        await m.invite_tick(elapse(vapp, 600))
        assert not m.invites
    run(check())


def test_logout_removes_seat_and_presence(vapp):
    async def check():
        m = await ready(vapp)
        m.on_logout(102)
        assert 102 not in vapp.ext.rooms.where and 102 not in vapp.ext.on_comms
        assert not vapp.rows("SELECT 1 FROM voice_presence WHERE guid=102 AND until IS NULL")
        assert wire.Moved(102, "") in vapp.sent
        assert set(vapp.ext.rooms.where) == {HUMAN, *CAST} - {102}
    run(check())


def test_relogin_restores_only_previously_enrolled_member(vapp):
    async def check():
        m = await ready(vapp)
        group(vapp, m)
        m.on_logout(102)
        m.on_login(102)
        assert 102 in vapp.ext.on_comms and vapp.ext.rooms.where[102] == "raid"
        m.on_logout(102)
        m.reset(vapp.clock.now())
        m.on_login(102)
        assert 102 not in vapp.ext.on_comms
    run(check())


def test_afk_ends_a_temporary_visit_without_stranding_the_member(vapp):
    async def check():
        m = await ready(vapp)
        m.tick(elapse(vapp, movement.HUDDLE_EVERY_S))
        m.on_afk(AfkEvent(vapp._ts(), 102, True))
        m.tick(elapse(vapp, movement.HUDDLE_S[1]))
        m.on_afk(AfkEvent(vapp._ts(), 102, False))
        assert vapp.ext.rooms.where[102] == "guild_hall"
    run(check())


def test_clean_stop_returns_owned_visits_and_invitation_before_saving(vapp, monkeypatch):
    async def check():
        m = await ready(vapp)
        await invite(vapp, m, monkeypatch)
        m.move(HUMAN, "private1")
        m.tick_rooms(vapp.clock.now())
        m._visit(101, "penalty_box", 180, "penalty", vapp.clock.now())
        m._visit(104, "officers", 400, "officers", vapp.clock.now())
        m.move(103, "lore")
        m.on_afk(AfkEvent(vapp._ts(), 103, True))
        m._visit(105, "smoke_room", 600, "smoke", vapp.clock.now())
        m.move(105, "pvp")  # a later deliberate move still wins over the visit timer
        m.prepare_shutdown(elapse(vapp, 20))
        assert vapp.ext.rooms.where == {HUMAN: "guild_hall", 101: "guild_hall", 102: "guild_hall",
                                       103: "lore", 104: "guild_hall", 105: "pvp"}
        assert not m.visits and not m.invites and not m.afk_returns and not m.together.open
        assert vapp.ext.rooms.allowed("private1") is None
        seats = vapp.ext.store.open_presence(vapp.app.session_id)
        assert {guid: row[0] for guid, row in seats.items()} == vapp.ext.rooms.where
        assert m.restored_seats(seats, crashed=False) == seats
    run(check())


@pytest.mark.parametrize("room", ["private1", "officers", "penalty_box", "healers", "smoke_room", "lore"])
def test_crash_does_not_restore_orphaned_cast_policy_rooms(vapp, room):
    async def check():
        m = await ready(vapp)
        seats = {HUMAN: ("lore", 0), 102: (room, 104), 103: ("raid", 102)}
        restored = m.restored_seats(seats, crashed=True)
        assert restored == {HUMAN: ("lore", 0), 102: ("guild_hall", 0), 103: ("raid", 102)}
        assert seats[102] == (room, 104)  # caller's crash evidence is unchanged
    run(check())


def test_restore_clears_ownerless_private_and_stale_afk_but_keeps_current_afk(vapp):
    async def check():
        m = await ready(vapp)
        seats = {HUMAN: ("private1", 0), 101: ("afk", 0), 102: ("afk", 0)}
        vapp.app.world.afk = {102}
        assert m.restored_seats(seats, crashed=True) == {
            HUMAN: ("guild_hall", 0), 101: ("guild_hall", 0), 102: ("afk", 0)}
    run(check())


def test_bridge_hello_reset_restores_afk_seat_in_same_session(vapp):
    async def check():
        m = await ready(vapp)
        m.move(101, "lore")
        await vapp.app.handle(AfkEvent(vapp._ts(), 101, True))
        await vapp.app.handle(AfkEvent(vapp._ts(), HUMAN, True))
        sid = vapp.app.session_id
        assert vapp.ext.rooms.where[101] == vapp.ext.rooms.where[HUMAN] == "afk"
        await vapp.hello()
        assert vapp.app.session_id == sid and not vapp.app.world.afk
        assert vapp.ext.rooms.where[101] == "lore" and vapp.ext.rooms.human_room() == "guild_hall"
        assert not m.afk_returns
        assert vapp.ext.store.open_presence(sid)[101][0] == "lore"
    run(check())


def test_afk_during_invitation_returns_to_original_room(vapp, monkeypatch):
    async def check():
        m = await ready(vapp)
        m.move(102, "lore")
        await invite(vapp, m, monkeypatch)
        m.on_afk(AfkEvent(vapp._ts(), 102, True))
        m.tick_rooms(elapse(vapp, 600))
        assert not m.invites and vapp.ext.rooms.allowed("private1") is None
        m.on_afk(AfkEvent(vapp._ts(), 102, False))
        assert vapp.ext.rooms.where[102] == "lore"
    run(check())


def test_human_afk_does_not_restore_expired_private_invitation(vapp, monkeypatch):
    async def check():
        m = await ready(vapp)
        await invite(vapp, m, monkeypatch)
        m.move(HUMAN, "private1")
        m.tick_rooms(vapp.clock.now())
        m.on_afk(AfkEvent(vapp._ts(), HUMAN, True))
        m.tick_rooms(vapp.clock.now())
        m.tick_rooms(elapse(vapp, 60))
        assert vapp.ext.rooms.where[102] == "guild_hall"
        m.on_afk(AfkEvent(vapp._ts(), HUMAN, False))
        assert vapp.ext.rooms.human_room() == "guild_hall"
    run(check())


def test_extension_clean_restart_saves_returned_seats_without_presence_gap(vapp, monkeypatch):
    async def check():
        m = await ready(vapp)
        m.move(102, "lore")
        await invite(vapp, m, monkeypatch)
        m._visit(101, "penalty_box", 180, "penalty", vapp.clock.now())
        vapp.ext.shutdown()
        assert not vapp.ext.rooms.where
        assert not vapp.rows("SELECT 1 FROM voice_presence WHERE until IS NULL")
        restarted = elapse(vapp, 30)
        vapp.ext.begin_session()
        assert vapp.ext.rooms.where[101] == "guild_hall"
        assert vapp.ext.rooms.where[102] == "lore"
        assert vapp.ext.rooms.allowed("private1") is None
        assert {row[0] for row in vapp.rows("SELECT since FROM voice_presence WHERE until IS NULL")} == {restarted}
    run(check())


def test_extension_crash_restart_drops_lost_policy_timers_and_locks(vapp, monkeypatch):
    async def check():
        m = await ready(vapp)
        await invite(vapp, m, monkeypatch)
        m.move(HUMAN, "private1")
        m._visit(101, "penalty_box", 180, "penalty", vapp.clock.now())
        m._visit(104, "officers", 400, "officers", vapp.clock.now())
        # Simulate lost process memory while retaining the open presence rows of the session.
        vapp.ext.rooms.clear()
        vapp.ext._seated_sid = None
        vapp.ext.movement = Movement(vapp.ext, rng=QuietRng())
        vapp.ext.begin_session()
        assert vapp.ext.rooms.where == {guid: "guild_hall" for guid in (*CAST, HUMAN)}
        assert not vapp.ext.movement.visits and not vapp.ext.movement.invites
        assert vapp.ext.rooms.allowed("private1") is None
        assert {row[0] for row in vapp.rows("SELECT room FROM voice_presence WHERE until IS NULL")} == {"guild_hall"}
    run(check())
