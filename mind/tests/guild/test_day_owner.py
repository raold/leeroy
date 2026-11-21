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


@pytest.mark.parametrize("variant,refusal", [("raid","busy:realm_owner"), ("offline","keep_cast_off"),
                                            ("offline","training_journal_pending")])
def test_independently_refused_native_owner_does_not_preempt_the_day(tmp_path, clock, raid_setup, offline_setup,
                                                                     variant, refusal):
    from leeroy_mind.protocol import Ack
    async def go():
        ports, _, controller = raid_setup if variant == "raid" else offline_setup
        ports.online = variant == "raid"
        rig = Rehearsal(tmp_path, Store(ports.conn, clock=clock), clock)
        await rig.begin(activity="inn", place="org_inn")
        ports.app = rig.app
        original_send = rig.app.send
        checks = []
        async def reply(cmd, **kwargs):
            if getattr(cmd, "day_check", None):
                checks.append(cmd)
                return Ack(0, cmd.id, False, refusal)
            return await original_send(cmd, **kwargs)
        rig.app.send = reply
        if variant == "raid":
            try:
                await form(raid_setup)
            except RuntimeError:
                pass
        else:
            await controller.tick()
        assert len(checks) == 1 and len(checks[0].guids if variant == "raid" else checks[0].members) > 1
        assert not ports.sent and rig.day.permit(1)["state"] == "open"
        assert rig.day.rows(1)[0]["status"] != "preempted"
        await rig.realm.close()
    asyncio.run(go())


@pytest.mark.parametrize("driver", ["pending", "immediate", "unavailable", "invalid", "check_lost", "prep_lost",
                                    "expired", "replaced", "original_lost"])
def test_real_offline_cold_driver_preparation_is_distinct_from_the_original_start(tmp_path, clock, offline_setup, driver):
    import json
    from leeroy_mind.bridge import AckTimeout
    from leeroy_mind.protocol import Ack, encode_command
    from .test_raid_night import advance

    async def go():
        ports, _, controller = offline_setup
        rig = Rehearsal(tmp_path, Store(ports.conn, clock=clock), clock)
        move = await rig.begin()
        ports.app = rig.app
        controls = []
        ready = driver == "original_lost"
        if driver == "original_lost":
            async def lost_start(original, **guard):
                ports.sent.append(original)
                raise AckTimeout("lost original")
            ports.send = lost_start

        async def native(cmd, **guard):
            nonlocal ready
            assert isinstance(cmd, StartOfflineRaid) and cmd.day_check
            assert guard["expected_epoch"] == rig.day.epoch
            wire = json.loads(encode_command(cmd, 0))
            controls.append((cmd, wire))
            if cmd.day_driver_ticket:
                prior = controls[-2][0]
                assert cmd.day_driver_ticket == prior.id and cmd.day_check == prior.day_check
                assert replace(cmd, id=prior.id, day_driver_ticket="") == prior
                assert wire["type"] == "day_prepare_offline_driver"
                if driver == "prep_lost":
                    raise AckTimeout("lost driver preparation")
                if driver == "unavailable":
                    return Ack(0, cmd.id, False, "driver_unavailable")
                ready = True
                return Ack(0, cmd.id, driver == "immediate", "" if driver == "immediate" else "driver_pending")
            assert wire["type"] == "day_check_start_offline_raid"
            if driver == "expired":
                await clock.advance(31)
            if driver == "replaced":
                await rig.app.connection(False)
                await rig.connect()
            if driver == "check_lost":
                raise AckTimeout("lost check")
            if driver == "invalid":
                return Ack(0, cmd.id, False, "training_journal_pending")
            return Ack(0, cmd.id, ready, "" if ready else "day_driver_required")
        rig.app.send = native
        task = asyncio.create_task(controller.tick())
        if driver == "original_lost":
            await awaiting(rig)
            await rig.app.event(rig.receipt("closed", "offline_raid", released=(move.id,), succeeded=()))
            await asyncio.wait_for(task, 2)
            assert len(ports.sent) == 1 and ports.sent[0].day_prepared == controls[0][0].id
            assert controller._owned["phase"] == "uncertain" and controller._day_state(controller._day())["blocked"]
            await rig.realm.close()
            return
        if driver != "immediate":
            await asyncio.wait_for(task, 2)
            assert not ports.sent and controller._owned["phase"] == "rejected"
            assert not controller.active and not controller._lease
            state = controller._day_state(controller._day())
            assert state.get("accepted", 0) == 0
            if driver in {"expired", "replaced"}:
                assert state["refused"] == "day_takeover_expired" and len(controls) == 1
                await rig.realm.close()
                return
            if driver in {"check_lost", "prep_lost"}:
                assert state["refused"] == "day_check_unknown" and not state["blocked"]
                assert rig.day.permit(1)["state"] == ("verifying" if driver == "check_lost" else "open")
            else:
                assert rig.day.permit(1)["state"] == "open"
            if driver != "pending":
                assert sum(bool(c.day_driver_ticket) for c, _ in controls) == int(driver in {"unavailable", "prep_lost"})
                assert state["blocked"] == (driver == "unavailable")
                await rig.realm.close()
                return
            assert state["refused"] == "driver_pending" and not state["blocked"]
            await advance(ports, 60)
            task = asyncio.create_task(controller.tick())
        await awaiting(rig)
        assert not ports.sent and len(controls) == 3
        await rig.app.event(rig.receipt("closed", "offline_raid", released=(move.id,), succeeded=()))
        await asyncio.wait_for(task, 2)
        [start] = ports.sent
        last = controls[-1][0]
        assert start.id == last.day_check.request_id and start.day_prepared == last.id
        assert len({c.id for c, _ in controls} | {start.id}) == 4
        assert controller._owned["phase"] == "accepted"
        await rig.realm.close()
    asyncio.run(go())


@pytest.mark.parametrize("op", ["lockout", "form"])
def test_real_raid_lost_control_never_arms_a_group_or_waits_for_its_job(tmp_path, clock, raid_setup, op):
    from leeroy_mind.bridge import AckTimeout
    from leeroy_mind.guild.raid_night import FORMED_GROUP_KEY
    from leeroy_mind.realm.actions import ActionRejected

    async def go():
        ports, _, controller = raid_setup
        ports.online = True
        rig = Rehearsal(tmp_path, Store(ports.conn, clock=clock), clock)
        await rig.begin()
        ports.app = rig.app
        command = (ClearLockout("owner", 533, (101,)) if op == "lockout"
                   else FormRaid("owner", 1, (101,), 10, 533, 101))
        writes = []
        async def drop(check, **guard):
            assert check.day_check.request_id == command.id
            writes.append(check)
            raise AckTimeout("lost check")
        rig.app.send = drop
        with pytest.raises(ActionRejected, match="day_check_unknown"):
            await controller._job(command, op, controller._epoch)
        assert len(writes) == 1 and not ports.sent
        assert not controller._forming_group
        assert not ports.conn.execute("SELECT 1 FROM meta WHERE key=?", (FORMED_GROUP_KEY,)).fetchone()
        await rig.realm.close()
    asyncio.run(go())


def test_raid_night_rechecks_cast_only_after_multi_guid_lockout_refusal(tmp_path, clock, raid_setup):
    from leeroy_mind.protocol import Ack

    async def go():
        ports, _, controller = raid_setup
        ports.online = True
        rig = Rehearsal(tmp_path, Store(ports.conn, clock=clock), clock)
        move = await rig.begin(activity="inn", place="org_inn")
        ports.app = rig.app
        inside = {101, ports.human_guid}
        checks = []
        previous = rig.app.send
        async def native(cmd, **guard):
            if not isinstance(cmd, ClearLockout):
                return await previous(cmd, **guard)
            checks.append(cmd)
            present = [g for g in cmd.guids if g in inside]
            if present:
                assert set(present) == inside and rig.day.permit(1)["state"] == "open"
                inside.remove(101)  # Cast member finishes the outbound transfer before the fresh request.
                return Ack(0, cmd.id, False, "in_instance:" + ",".join(map(str, present)))
            return Ack(0, cmd.id, True, "")
        rig.app.send = native
        pending = asyncio.create_task(form(raid_setup))
        await awaiting(rig)
        first, retry = checks
        assert set(retry.guids) == set(first.guids) - {ports.human_guid}
        assert first.day_check.request_id != retry.day_check.request_id
        assert not ports.sent
        await rig.app.event(rig.receipt("closed", "raid", released=(move.id,), succeeded=()))
        await asyncio.wait_for(pending, 2)
        [original] = [cmd for cmd in ports.sent if isinstance(cmd, ClearLockout)]
        assert original.id == retry.day_check.request_id and original.day_prepared == retry.id
        assert original.guids == retry.guids and any(isinstance(cmd, FormRaid) for cmd in ports.sent)
        assert not rig.realm.action_store.holds()
        await rig.realm.close()
    asyncio.run(go())


@pytest.mark.parametrize("suffix", ["", "101,", "101,,1", "101,101", "001,101", "0", "4294967296",
                                    "101,999", "101,1:extra", "101,1 detail", "１", "1," * 500])
def test_lockout_refusal_accepts_only_distinct_canonical_requested_guids(suffix):
    from leeroy_mind.godmode.dayrun import check_refusal
    from leeroy_mind.guild.protocol import StringDo
    command = ClearLockout("clear", 533, (101, 1, 4294967295))
    assert check_refusal(command, "in_instance:" + suffix) == "refused"
    assert check_refusal(command, "in_instance:4294967295,1") == "in_instance:4294967295,1"
    assert check_refusal(StringDo("string", 101, "come_to_me"), "in_instance:101,1") == "refused"
