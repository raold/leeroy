"""The actual controllers keep their original command while the native day body is in transit."""
import asyncio
from dataclasses import replace

import pytest

from godmode.test_dayrun import Rehearsal
from leeroy_mind.guild.protocol import ClearLockout, FormRaid, StartOfflineRaid
from leeroy_mind.storage.store import Store
from .test_offline import setup as offline_setup
from .test_raid_night import form, setup as raid_setup


async def awaiting(rig):
    async def wait():
        while rig.day.permit(1)["state"] != "revoking":
            await asyncio.sleep(.005)
    await asyncio.wait_for(wait(), 2)


@pytest.mark.parametrize("variant", ["raid", "offline_raid"])
def test_real_raid_request_hands_back_once_before_its_first_mutation(tmp_path, clock, raid_setup, offline_setup, variant):
    async def go():
        ports, _, controller = raid_setup if variant == "raid" else offline_setup
        if variant == "raid":
            ports.online = True
        rig = Rehearsal(tmp_path, Store(ports.conn, clock=clock), clock)
        move = await rig.begin(activity="inn", place="org_inn")
        ports.app = rig.app
        task = asyncio.create_task(form(raid_setup) if variant == "raid" else controller.tick())
        await awaiting(rig)
        assert not ports.sent
        assert rig.day.rows(1)[0]["reason"] == variant
        await rig.app.event(rig.receipt("closed", variant, released=(move.id,), succeeded=()))
        await asyncio.wait_for(task, 2)
        expected = ClearLockout if variant == "raid" else StartOfflineRaid
        assert isinstance(ports.sent[0], expected)
        assert sum(isinstance(c, expected) for c in ports.sent) == 1
        ident, status = ports.conn.execute("SELECT request_id,state FROM day_handoff").fetchone()
        assert (ident, status) == (ports.sent[0].id, "sent")
        if variant == "raid":
            assert sum(isinstance(c, FormRaid) for c in ports.sent) == 1
        await rig.realm.close()
    asyncio.run(go())


def test_unsupported_offline_raid_leaves_the_actual_day_operation_open(tmp_path, clock, offline_setup):
    async def go():
        ports, _, controller = offline_setup
        rig = Rehearsal(tmp_path, Store(ports.conn, clock=clock), clock)
        await rig.begin()
        ports.app = rig.app
        controller.cfg = replace(controller.cfg, offline_rotation=("toc25",))
        await controller.tick()
        assert not ports.sent and rig.day.permit(1)["state"] == "open"
        assert ports.conn.execute("SELECT count(*) FROM day_handoff").fetchone()[0] == 0
        await rig.realm.close()
    asyncio.run(go())


def test_offline_raid_revalidates_selected_tank_after_native_release(tmp_path, clock, offline_setup):
    async def go():
        ports, _, controller = offline_setup
        rig = Rehearsal(tmp_path, Store(ports.conn, clock=clock), clock)
        move = await rig.begin(activity="inn", place="org_inn")
        ports.app = rig.app
        task = asyncio.create_task(controller.tick())
        await awaiting(rig)
        tank = controller._owned["plan"]["tank_guid"]
        ports.members[tank] = replace(ports.members[tank], online=False)
        await rig.app.event(rig.receipt("closed", "offline_raid", released=(move.id,)))
        await asyncio.wait_for(task, 2)
        assert not ports.sent
        assert ports.conn.execute("SELECT state FROM day_handoff").fetchone()[0] == "expired"
        await rig.realm.close()
    asyncio.run(go())
