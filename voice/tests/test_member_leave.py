"""A member leaving comms removes only that seat across the existing v1 wire/UI path."""
from conftest import eventually, run
from test_service import HUMAN, RAID_ROOM, ROOM, Rig
from leeroy_voice.wire import Channels, Invite, Moved, encode, parse


def test_explicit_leave_uses_existing_v1_codec():
    line = b'{"v":1,"type":"moved","ts":1,"guid":101,"channel":"","by":0}\n'
    assert parse(line) == Moved(101, "")
    assert encode(Moved(101, ""), 1) == line


def test_member_leave_preserves_other_seats_and_removes_invite(tmp_path, fake_bank):
    async def main():
        async with Rig(tmp_path, fake_bank) as rig:
            await rig.snapshot()
            page = await rig.page()
            await rig.mind.send(Moved(102, RAID_ROOM))
            await rig.mind.send(Invite(101, ROOM))
            await eventually(lambda: bool(rig.service.state()["invites"]))
            await rig.mind.send(Moved(101, ""))
            await eventually(lambda: {"type": "moved", "guid": 101, "channel": ""} in page.received)
            state = rig.service.state()
            assert state["where"] == {HUMAN: ROOM, 102: RAID_ROOM}
            assert not state["invites"]
            # A later channel-lock update must not empty the remaining seats either.
            await rig.mind.send(Channels(tuple(rig.service.tree.rooms.values())))
            await rig.tick()
            assert rig.service.state()["where"] == {HUMAN: ROOM, 102: RAID_ROOM}
    run(main())
