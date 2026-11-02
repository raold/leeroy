"""Independent integration regressions with synthetic guild ports only."""

import asyncio
from dataclasses import replace

import pytest

from leeroy_mind.guild.protocol import Summon
from leeroy_mind.protocol import Boss

from .test_raid_night import setup as raid_setup, form, inside


@pytest.mark.parametrize("terminal_during_wait", [False, True])
def test_early_pull_during_late_summon_defers_remaining_members_without_resending_completed(raid_setup,
                                                                                        terminal_during_wait):
    async def go():
        ports, _, night = raid_setup
        night.summon_plan = lambda members: (members[:-2], [(g, 3600) for g in members[-2:]])
        await form(raid_setup)
        ports.members = {g: replace(member, map=631) for g, member in ports.members.items()}
        await inside(raid_setup)
        first, second = [guid for guid, _ in night._late]
        night._late = [(first, ports.clock.now()), (second, ports.clock.now())]
        assert first not in night._arrived and second not in night._arrived
        ports.sent.clear()
        ports.proposed.clear()
        original = ports.wait_job
        engaged = Boss(int(ports.clock.now() * 1000), 631, 77, 0, "Synthetic Boss", "engaged",
                       tuple(night.run.plan.members))
        injected = False

        async def wait_job(cmd_id, timeout_s):
            nonlocal injected
            command = next(command for command in reversed(ports.sent) if command.id == cmd_id)
            if isinstance(command, Summon) and command.guids == (first,) and not injected:
                injected = True
                await night.on_boss(engaged)
                if terminal_during_wait:
                    await night.on_boss(replace(engaged, ts=engaged.ts + 1, state="killed"))
            return await original(cmd_id, timeout_s)

        ports.wait_job = wait_job
        await night.tick()
        assert injected
        assert [command.guids for command in ports.sent if isinstance(command, Summon)] == [(first,)]
        assert first in night._arrived and second not in night._arrived
        assert [guid for guid, _ in night._late] == [second]
        assert not any(guid == first and channel == "raid" for guid, channel, *_ in ports.proposed)
        if not terminal_during_wait:
            await night.tick()
            assert [command.guids for command in ports.sent if isinstance(command, Summon)] == [(first,)]
            await night.on_boss(replace(engaged, ts=engaged.ts + 1, state="killed"))
        await night.tick()
        assert [command.guids for command in ports.sent if isinstance(command, Summon)] == [(first,), (second,)]
        assert first in night._arrived and second in night._arrived
        assert night._late == []

    asyncio.run(go())
