import random
from dataclasses import replace

import pytest

from leeroy_mind.guild.offline import OfflineRaids
from leeroy_mind.guild.protocol import StopOfflineRaid
from leeroy_mind.guild.offline import CLEANUP_GRACE_S, LOST_TERMINAL_S
from leeroy_mind.guild.ports import JobWaiter, LivePorts
from leeroy_mind.protocol import Boss
from leeroy_mind.bridge import AckTimeout
from .test_offline import setup, start, event
from .test_raid_night import _run_async, advance, setup as raid_setup


async def heartbeat(setup, *, age=0, inside=25, engaged=False, **changes):
    ports, _, controller = setup
    now = int(ports.clock.now() * 1000)
    fields = dict(ts=now, last_progress_ts=now - age * 1000,
                  members_in_instance=inside, boss_engaged=engaged)
    fields.update(changes)
    await controller.on_job(event(controller, **fields))
    await controller.tick()


def stops(ports):
    return [c for c in ports.sent if isinstance(c, StopOfflineRaid)]


@_run_async
async def test_stall_waits_for_native_terminal_and_finishes_once(setup):
    ports, _, controller = setup
    await start(setup)
    await heartbeat(setup)
    raid_id = controller.session.raid_id
    await advance(ports, 1800)
    await heartbeat(setup, age=1800)
    assert len(stops(ports)) == 1
    assert stops(ports)[0].raid_id == controller._owned['id']
    assert controller.active and controller.session is not None
    terminal = event(controller, state='done', stage='', reason='stopped', kills=1)
    await controller.on_job(terminal)
    await controller.tick()
    assert not controller.active
    assert ports.conn.execute('SELECT outcome FROM guild_raid WHERE raid_id=?', (raid_id,)).fetchone()[0] == 'stalled'
    memories = len(ports.memories)
    await controller.on_job(terminal)
    await controller.tick()
    assert len(ports.memories) == memories and len(stops(ports)) == 1
    assert ports.gpu.count('release:offline_raid') == 1


@pytest.mark.parametrize('kind', ['progress', 'engaged', 'stale', 'future', 'malformed', 'replayed'])
@_run_async
async def test_watchdog_needs_fresh_valid_disengaged_stall_evidence(setup, kind):
    ports, _, controller = setup
    await start(setup)
    await heartbeat(setup)
    first_ts = int(ports.clock.now() * 1000)
    await advance(ports, 1800)
    if kind == 'progress':
        await heartbeat(setup)
    elif kind == 'engaged':
        await heartbeat(setup, age=1800, engaged=True)
    elif kind == 'stale':
        await controller.tick()
    elif kind == 'future':
        await heartbeat(setup, age=1800, ts=int((ports.clock.now() + 1) * 1000))
    elif kind == 'malformed':
        await heartbeat(setup, age=1800, members_in_instance=True)
    else:
        await heartbeat(setup, age=1800, ts=first_ts)
    assert not stops(ports) and controller.active


@_run_async
async def test_engagement_and_native_race_refusal_defer_stop(setup):
    ports, _, controller = setup
    await start(setup)
    await heartbeat(setup)
    await advance(ports, 1800)
    await heartbeat(setup, age=1800, engaged=True)
    assert not stops(ports)
    ports.acks['offline_raid_stop'].append((False, 'boss_engaged'))
    await advance(ports, 60)
    await heartbeat(setup, age=1860)
    assert controller.active and not controller._owned['stop']['accepted']
    await advance(ports, 60)
    await heartbeat(setup, age=1920)
    assert len(stops(ports)) == 2 and stops(ports)[0].id != stops(ports)[1].id


@pytest.mark.parametrize('old', [True, False])
@_run_async
async def test_unsupported_native_logs_once_and_keeps_existing_timeout(setup, caplog, old):
    ports, _, controller = setup
    await start(setup)
    with caplog.at_level('INFO'):
        if old:
            await controller.on_job(event(controller))
            await controller.tick()
        else:
            await heartbeat(setup)
            ports.acks['offline_raid_stop'].append((False, 'unknown type: offline_raid_stop'))
        await advance(ports, 1800)
        for _ in range(3):
            if old:
                await controller.on_job(event(controller))
                await controller.tick()
            else:
                await heartbeat(setup, age=1800)
            await advance(ports, 60)
    assert len(stops(ports)) == (0 if old else 1)
    assert sum('offline_raid_stop_unsupported' in r.message for r in caplog.records) == 1
    assert controller.active


@_run_async
async def test_lost_ack_keeps_target_and_terminal_survives_restart(setup):
    ports, armory, controller = setup
    await start(setup)
    await heartbeat(setup)
    original = ports.send
    raid_id = controller.session.raid_id

    async def lost(cmd):
        await original(cmd)
        return None

    ports.send = lost
    await advance(ports, 1800)
    await heartbeat(setup, age=1800)
    assert controller._owned['stop'] and controller.active
    ports.send = original
    restored = OfflineRaids(ports, controller.cfg, armory, random.Random(7))
    await restored.tick()
    assert len(stops(ports)) == 1  # Saved observations never authorize a stop after restart.
    await advance(ports, 60)
    await heartbeat((ports, armory, restored), age=1860)
    assert len(stops(ports)) == 2 and stops(ports)[0].id != stops(ports)[1].id
    assert stops(ports)[0].raid_id == stops(ports)[1].raid_id
    await restored.on_job(event(restored, state='done', stage='', reason='stopped'))
    again = OfflineRaids(ports, restored.cfg, armory, random.Random(7))
    await again.tick()
    assert not again.active
    assert ports.conn.execute('SELECT outcome FROM guild_raid WHERE raid_id=?', (raid_id,)).fetchone()[0] == 'stalled'


@_run_async
async def test_terminal_failure_keeps_native_cleanup_reason(setup):
    ports, _, controller = setup
    await start(setup)
    await heartbeat(setup)
    raid_id = controller.session.raid_id
    await advance(ports, 1800)
    await heartbeat(setup, age=1800)
    await controller.on_job(event(controller, state='failed', stage='', reason='cleanup_failed:stopped'))
    await controller.tick()
    assert ports.conn.execute('SELECT outcome FROM guild_raid WHERE raid_id=?', (raid_id,)).fetchone()[0] == 'cleanup_failed:stopped'


@pytest.mark.parametrize('state', ['engaged', 'killed', 'wiped'])
@_run_async
async def test_boss_evidence_between_heartbeats_prevents_stale_stop(setup, state):
    ports, _, controller = setup
    await start(setup)
    await heartbeat(setup)
    await advance(ports, 1799)
    await heartbeat(setup, age=1799)
    await controller.on_boss(Boss(int(ports.clock.now() * 1000), 631, 77, 1, 'Invented boss', state, (101, 104)))
    await advance(ports, 1)
    await controller.tick()
    assert not stops(ports)


@_run_async
async def test_new_run_never_inherits_the_previous_progress(setup):
    ports, _, controller = setup
    controller.cfg = replace(controller.cfg, offline_raids_per_day=2)
    await start(setup)
    await heartbeat(setup)
    old_id = controller._owned['id']
    await controller.on_job(event(controller, state='done', stage='', reason='cleared'))
    await controller.tick()
    await advance(ports, 60)
    await controller.tick()
    assert controller.active and controller._owned['id'] != old_id
    assert controller._progress is None


@_run_async
async def test_real_bridge_timeout_and_new_progress_do_not_stop_or_release_locally(setup):
    ports, _, controller = setup
    await start(setup)
    await heartbeat(setup)
    original = ports.send

    async def timeout(cmd):
        await original(cmd)
        raise AckTimeout('synthetic timeout')

    ports.send = timeout
    await advance(ports, 1800)
    await heartbeat(setup, age=1800)
    assert controller.active and not controller._owned['cancelled']
    assert 'release:offline_raid' not in ports.gpu
    await advance(ports, 60)
    await heartbeat(setup)
    assert len(stops(ports)) == 1


@_run_async
async def test_finished_refusal_does_not_discard_uncertain_stop_reason(setup):
    ports, _, controller = setup
    await start(setup)
    await heartbeat(setup)
    raid_id = controller.session.raid_id
    ports.acks['offline_raid_stop'].append((False, 'raid_finished'))
    await advance(ports, 1800)
    await heartbeat(setup, age=1800)
    assert controller.active and controller._owned['stop']['await_terminal']
    await controller.on_job(event(controller, state='done', stage='', reason='stopped'))
    await controller.tick()
    assert ports.conn.execute('SELECT outcome FROM guild_raid WHERE raid_id=?', (raid_id,)).fetchone()[0] == 'stalled'


@pytest.mark.parametrize('error', [RuntimeError('guild transport unavailable'), ConnectionError('gone'), ValueError('x')])
@_run_async
async def test_send_failure_during_due_stop_does_not_raise_and_retries(setup, error):
    ports, _, controller = setup
    await start(setup)
    await heartbeat(setup)
    original = ports.send
    calls = []

    async def broken(cmd):
        calls.append(cmd)
        raise error

    ports.send = broken
    await advance(ports, 1800)
    await heartbeat(setup, age=1800)  # tick() must not raise
    assert len(calls) == 1 and controller.active and not controller._owned['cancelled']
    assert not controller._owned['stop']['accepted']
    ports.send = original
    await advance(ports, 60)
    await heartbeat(setup, age=1860)
    assert len(stops(ports)) == 1 and controller._owned['stop']['accepted']
    assert stops(ports)[0].id != calls[0].id


@_run_async
async def test_stop_retries_do_not_grow_the_job_mailbox():
    class App:
        class bridge:
            @staticmethod
            async def send(cmd, preserve_id=False):
                return None
    app = type('A', (), dict(clock=None, store=type('S', (), dict(conn=None))(), bridge=App.bridge))()
    ports = LivePorts(app, jobs=JobWaiter(capacity=4))
    for i in range(50):
        await ports.send(StopOfflineRaid(f's{i}', 'raid1'))
    assert not ports.jobs._jobs


@_run_async
async def test_accepted_stop_with_slow_teardown_does_not_block_the_day(setup):
    ports, _, controller = setup
    await start(setup)
    await heartbeat(setup)
    await advance(ports, 1800)
    await heartbeat(setup, age=1800)
    assert controller._owned['stop']['accepted']
    await advance(ports, CLEANUP_GRACE_S + 1)
    await controller.tick()
    assert 'release:offline_raid' in ports.gpu
    assert not controller._day_state(controller._owned['day'])['blocked']
    await controller.on_job(event(controller, state='done', stage='', reason='stopped'))
    await controller.tick()
    assert not controller.active
    assert not controller._day_state()['blocked']


@_run_async
async def test_kill_after_restart_defers_the_stop(setup):
    ports, armory, controller = setup
    await start(setup)
    await heartbeat(setup)
    restored = OfflineRaids(ports, controller.cfg, armory, random.Random(7))
    s2 = (ports, armory, restored)
    await advance(ports, 1795)
    await heartbeat(s2, age=1795)
    assert restored._owned['cancelled'] and not stops(ports)
    await restored.on_boss(Boss(int(ports.clock.now() * 1000), 631, 77, 1, 'Invented boss', 'killed', (101, 104)))
    await advance(ports, 10)
    await restored.tick()
    assert not stops(ports)


@_run_async
async def test_lost_terminal_after_accepted_stop_fires_from_the_stop_time(setup):
    ports, _, controller = setup
    await start(setup)
    await heartbeat(setup)
    raid_id = controller.session.raid_id
    await advance(ports, 1800)
    await heartbeat(setup, age=1800)
    assert controller._owned['stop']['accepted']
    await advance(ports, LOST_TERMINAL_S - 1)
    await controller.tick()
    assert controller.active
    await advance(ports, 2)
    await controller.tick()
    assert not controller.active
    assert controller._owned['terminal']['reason'] == 'terminal_lost'
    assert ports.conn.execute('SELECT outcome FROM guild_raid WHERE raid_id=?', (raid_id,)).fetchone()[0] == 'terminal_lost'
