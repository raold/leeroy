import asyncio
from dataclasses import replace
from functools import wraps
import json
from types import SimpleNamespace

import pytest

from leeroy_mind.guild.config import GuildConfig
from leeroy_mind.guild.offline import next_raid_key
from leeroy_mind.guild.raid_night import RaidRun
from leeroy_mind.guild.raids import split_key
from leeroy_mind.guild.session import RaidPlan
from leeroy_mind.protocol import Ack, Boss
from leeroy_mind.storage.store import Store
from leeroy_mind.training.gate import Gate
from leeroy_mind.training.protocol import GetStats, ServerStats, Telemetry
from leeroy_mind.training.store import perf_rows, put_perf


def _run_async(test):
    @wraps(test)
    def run(*args, **kwargs):
        return asyncio.run(test(*args, **kwargs))
    return run


def run_for(fake, ident=1, *, offline=True, instance=100, size=10, heroic=False, map_id=631):
    plan = RaidPlan("icc", size, heroic, offline, 101, 101, 101, [101, 102], [], not offline)
    session = SimpleNamespace(raid_id=ident, started=fake.clock.now(), _closing=False,
                              ctx=SimpleNamespace(map_id=map_id, instance_id=instance))
    return RaidRun(plan, session, not offline, False)


def boss(fake, index=0, state="engaged", **kw):
    return replace(Boss(int(fake.clock.now() * 1000), 631, 100, index, "", state, (101, 102)), **kw)


async def family_fight(fake, gate, run, index):
    ctx = run.session.ctx
    at = dict(map=ctx.map_id, instance_id=ctx.instance_id)
    await gate.on_muster(run, index)
    await gate.on_boss(boss(fake, index, **at))
    await collect(fake, gate, gate.record_flag(run, index))
    await gate.on_boss(boss(fake, index, "killed", **at))
    await fake.clock.advance(1, step=1)


def stats(fake, record, value=41, **kw):
    return replace(ServerStats(int(fake.clock.now() * 1000), "", value, value, record, int(record)), **kw)


async def collect(fake, gate, armed, values=(41, 41)):
    for value in values:
        await fake.clock.advance(30, step=30)
        fake.events["server_stats"].append(stats(fake, armed, value))
        await gate.sample()


async def fight(fake, gate, run, index=0, values=(41, 41), outcome="killed"):
    await gate.on_muster(run, index)
    await gate.on_boss(boss(fake, index))
    await collect(fake, gate, gate.record_flag(run, index), values)
    await gate.on_boss(boss(fake, index, outcome))
    await fake.clock.advance(1, step=1)


def metadata(fake, key="tg_gate"):
    value = Store(fake.conn, clock=fake.clock).get_meta(key)
    return None if value is None else json.loads(value)


@_run_async
async def test_ab_alternates_by_pull_and_starts_on_other_side_each_raid(fake):
    gate = Gate(fake)
    first, second, human = run_for(fake), run_for(fake, 2), run_for(fake, 3, offline=False)
    await gate.on_start(first)
    await gate.on_start(first)
    assert [gate.record_flag(first, i) for i in range(4)] == [True, False, True, False]
    await gate.on_start(human)
    assert not any(gate.record_flag(human, i) for i in range(4))
    await gate.on_start(second)
    assert [gate.record_flag(second, i) for i in range(4)] == [False, True, False, True]
    assert metadata(fake) == dict(phase="ab", raids=2, verdict_ms=None)
    restarted = Gate(fake)
    await restarted.on_start(second)
    assert not restarted.record_flag(second, 0)
    assert metadata(fake)["raids"] == 2


@_run_async
async def test_rotating_offline_raids_compare_each_boss_on_both_sides(fake):
    # Guild life rotates four offline raids, least recently run first, so a
    # raid's next visit is four raids later. Parity counted over all raids
    # would keep every boss on one side and the gate could never decide.
    gate, last = Gate(fake), {}
    sides = {}
    for ident in range(1, 9):
        key = next_raid_key(GuildConfig().offline_rotation, last)
        raid, size, heroic = split_key(key)
        run = run_for(fake, ident, size=size, heroic=heroic, map_id=raid.map_id, instance=100 + ident)
        await gate.on_start(run)
        for index in range(3):
            sides.setdefault((key, index), []).append(gate.record_flag(run, index))
            await family_fight(fake, gate, run, index)
        last[key] = fake.clock.now()
    assert all(sorted(seen) == [False, True] for seen in sides.values()) and len(sides) == 12
    assert gate.verdict() == ("passed", 0.0)
    assert metadata(fake)["raids"] == 8
    restarted = Gate(fake)
    assert restarted.verdict() == ("passed", 0.0)


@_run_async
async def test_family_parity_survives_restart_and_skips_failed_setups(fake):
    gate = Gate(fake)
    first = run_for(fake, 1, size=25, instance=201)
    await gate.on_start(first)
    other = run_for(fake, 2, size=10, instance=202)
    await gate.on_start(other)
    # Raid 3 failed in setup and never started; raid 4 is the family's second visit.
    second = run_for(fake, 4, size=25, instance=204)
    restarted = Gate(fake)
    await restarted.on_start(second)
    assert [restarted.record_flag(second, i) for i in range(2)] == [False, True]
    again = Gate(fake)
    await again.on_start(second)
    assert [again.record_flag(second, i) for i in range(2)] == [False, True]
    assert metadata(fake)["raids"] == 3


@_run_async
async def test_corrupt_family_counter_fails_closed_for_that_family(fake):
    with fake.conn:
        fake.conn.execute("INSERT INTO meta(key,value) VALUES ('tg_gate:family:631:1','\"x\"')")
    gate = Gate(fake)
    run = run_for(fake, 1, size=25)
    await gate.on_start(run)
    assert not any(gate.record_flag(run, i) for i in range(4))
    assert metadata(fake) is None
    fresh = run_for(fake, 2, size=10)
    await gate.on_start(fresh)
    assert [gate.record_flag(fresh, i) for i in range(2)] == [True, False]


@_run_async
async def test_samples_only_offline_fights_on_thirty_second_ticks(fake):
    gate, run = Gate(fake), run_for(fake)
    await gate.on_start(run)
    await gate.on_muster(run, 0)
    await fake.clock.advance(90, step=90)
    await gate.sample()
    assert not any(isinstance(c, GetStats) for c in fake.sent)
    await gate.on_boss(boss(fake))
    await fake.clock.advance(29, step=29)
    await gate.sample()
    assert not any(isinstance(c, GetStats) for c in fake.sent)
    await fake.clock.advance(1, step=1)
    fake.events["server_stats"].append(stats(fake, True))
    await gate.sample()
    await gate.sample()
    assert len([c for c in fake.sent if isinstance(c, GetStats)]) == 1
    await collect(fake, gate, True)
    await gate.on_boss(boss(fake, state="killed"))
    rows = perf_rows(fake.conn)
    assert len(rows) == 1 and rows[0]["armed"] == 1
    receipt = metadata(fake, f"tg_gate:perf:{rows[0]['fight']}")
    assert len(receipt["samples"]) == 3
    assert receipt["basis"] == "rolling_world_500_updates_v1"
    await fake.clock.advance(90, step=90)
    await gate.sample()
    assert len([c for c in fake.sent if isinstance(c, GetStats)]) == 3
    human = run_for(fake, 2, offline=False)
    await gate.on_start(human)
    await fight(fake, gate, human)
    assert len(perf_rows(fake.conn)) == 1
    assert len([c for c in fake.sent if isinstance(c, GetStats)]) == 3


@_run_async
async def test_verdict_from_twelve_per_boss_pairs_and_persists(fake):
    gate = Gate(fake)
    for raid in range(2):
        run = run_for(fake, raid + 1)
        await gate.on_start(run)
        for index in range(12):
            values = (41, 41) if gate.record_flag(run, index) else (40, 41)
            await fight(fake, gate, run, index, values)
    assert gate.verdict() == ("passed", 0.5)
    assert Gate(fake).verdict() == ("passed", 0.5)
    human = run_for(fake, 3, offline=False)
    await gate.on_start(human)
    await gate.on_muster(human, 0)
    assert gate.record_flag(human, 0) and gate.recording()
    fake.cfg = replace(fake.cfg, telemetry=False)
    assert not gate.record_flag(human, 0) and not gate.recording()


@_run_async
async def test_context_needs_both_sides_and_two_samples_per_completed_fight(fake):
    gate = Gate(fake)
    first = run_for(fake)
    await gate.on_start(first)
    for index in range(12):
        await fight(fake, gate, first, index)
    assert gate.verdict() == ("ab", None)
    second = run_for(fake, 2)
    await gate.on_start(second)
    for index in range(11):
        await fight(fake, gate, second, index)
    await fight(fake, gate, second, 11, values=(41,))
    assert len(perf_rows(fake.conn)) == 23
    assert gate.verdict() == Gate(fake).verdict() == ("ab", None)


@_run_async
async def test_failed_verdict_disarms_and_is_final(fake):
    fake.cfg = replace(fake.cfg, gate_pulls=4)
    gate = Gate(fake)
    for raid in range(2):
        run = run_for(fake, raid + 1)
        await gate.on_start(run)
        for index in range(4):
            await fight(fake, gate, run, index,
                        values=(42, 43) if gate.record_flag(run, index) else (41, 41))
    assert gate.verdict() == ("failed", 1.5)
    assert not gate.recording() and not gate.record_flag(run, 0)
    assert isinstance(fake.sent[-1], Telemetry) and not fake.sent[-1].record
    fake.cfg = replace(fake.cfg, gate_max_ms=10)
    assert Gate(fake).verdict() == ("failed", 1.5)


@_run_async
async def test_fight_means_have_equal_weight_not_sample_count(fake):
    fake.cfg = replace(fake.cfg, gate_pulls=100)
    gate = Gate(fake)
    for raid in range(2):
        run = run_for(fake, raid + 1)
        await gate.on_start(run)
        for index in range(4):
            await fight(fake, gate, run, index, values=(40, 40))
            if gate.phase == "ab" and gate.record_flag(run, index):
                await fight(fake, gate, run, index, values=(42,) * 4)
    # Each armed side contains fight means 40 and 42 (mean 41), even though
    # the second fight has twice as many samples. Unarmed side is 40.
    fake.cfg = replace(fake.cfg, gate_pulls=4)
    assert gate.verdict() == ("passed", 1.0)


@_run_async
async def test_hello_resends_flag_and_new_watch_and_clears_old_fight(fake):
    watched = [123, 456]
    gate, run = Gate(fake, watch=lambda: tuple(watched)), run_for(fake)
    await gate.on_start(run)
    await gate.on_muster(run, 0)
    await gate.on_muster(run, 0)
    assert len(fake.sent) == 1
    await gate.on_boss(boss(fake))
    await collect(fake, gate, True)
    watched.append(789)
    await gate.on_hello()
    assert fake.sent[-1].record and fake.sent[-1].watch == (123, 456, 789)
    assert gate.recording()
    await gate.on_boss(boss(fake, state="killed"))
    assert not perf_rows(fake.conn)


@_run_async
@pytest.mark.parametrize("failure", ["telemetry_off", "wrong_id", "missing", "timeout"])
async def test_ack_failure_never_records_or_supplies_an_unarmed_control(fake, failure):
    gate, run = Gate(fake), run_for(fake)
    await gate.on_start(run)

    async def rejected(cmd):
        if failure == "timeout":
            raise TimeoutError
        if failure == "missing":
            return None
        return Ack(0, "wrong" if failure == "wrong_id" else cmd.id,
                   failure != "telemetry_off", "telemetry_off" if failure == "telemetry_off" else "")

    fake.send = rejected
    await gate.on_muster(run, 0)
    assert not gate.recording()
    await gate.on_boss(boss(fake))
    await collect(fake, gate, True)
    await gate.on_boss(boss(fake, state="killed"))
    await gate.on_muster(run, 1)
    await gate.on_boss(boss(fake, 1))
    await collect(fake, gate, False)
    await gate.on_boss(boss(fake, 1, "killed"))
    assert not perf_rows(fake.conn)


@_run_async
async def test_late_ack_cannot_arm_replaced_run_or_survive_reconnect(fake):
    gate, old = Gate(fake), run_for(fake)
    await gate.on_start(old)
    entered, release = asyncio.Event(), asyncio.Event()
    original = fake.send

    async def delayed(cmd):
        entered.set()
        await release.wait()
        return await original(cmd)

    fake.send = delayed
    pending = asyncio.create_task(gate.on_muster(old, 0))
    await entered.wait()
    await gate.on_connection(False)
    new = run_for(fake, 2)
    await gate.on_start(new)
    release.set()
    await pending
    assert not gate.recording()
    await gate.on_muster(new, 0)
    assert not gate.recording() and fake.sent[-1].record is False
    await gate.on_end("ended", run=old)
    assert gate.record_flag(new, 1)


@_run_async
async def test_late_same_run_ack_is_rejected_until_new_flag_ack(fake):
    gate, run = Gate(fake), run_for(fake)
    await gate.on_start(run)
    entered, release = asyncio.Event(), asyncio.Event()
    original = fake.send

    async def delayed(cmd):
        if cmd.record:
            entered.set()
            await release.wait()
        return await original(cmd)

    fake.send = delayed
    old = asyncio.create_task(gate.on_muster(run, 0))
    await entered.wait()
    new = asyncio.create_task(gate.on_muster(run, 1))
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(old, new)
    assert [cmd.record for cmd in fake.sent] == [True, False]
    assert not gate.recording()


@_run_async
@pytest.mark.parametrize("transition", ["end", "reconnect", "replace", "muster", "same_muster"])
async def test_late_stats_cannot_move_to_another_fight_or_generation(fake, transition):
    gate, run = Gate(fake), run_for(fake)
    await gate.on_start(run)
    await gate.on_muster(run, 0)
    await gate.on_boss(boss(fake))
    await collect(fake, gate, True, values=(41,))
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed(cmd, *args, **kw):
        result = stats(fake, True, id=cmd.id)
        entered.set()
        await release.wait()
        return [result]

    fake.request = delayed
    await fake.clock.advance(30, step=30)
    pending = asyncio.create_task(gate.sample())
    await entered.wait()
    if transition == "end":
        await gate.on_boss(boss(fake, state="killed"))
    elif transition == "reconnect":
        await gate.on_hello()
    elif transition == "replace":
        await gate.on_start(run_for(fake, 2))
    elif transition == "muster":
        await gate.on_muster(run, 1)
    else:
        await gate.on_muster(run, 0)
    release.set()
    await pending
    await gate.on_boss(boss(fake, state="killed"))
    assert not perf_rows(fake.conn)


@_run_async
@pytest.mark.parametrize("change", [dict(map=649), dict(instance_id=999), dict(members=(999,)),
                                   dict(boss_id=13), dict(ts=1)])
async def test_foreign_stale_and_nonfight_events_do_not_open_samples(fake, change):
    gate, run = Gate(fake), run_for(fake)
    await gate.on_start(run)
    await gate.on_muster(run, change.get("boss_id", 0))
    await gate.on_boss(boss(fake, **change))
    await collect(fake, gate, True)
    assert not any(isinstance(c, GetStats) for c in fake.sent)


@_run_async
@pytest.mark.parametrize("change", [dict(telemetry=False), dict(armed=0), dict(armed=2),
                                   dict(mean_ms=-1), dict(mean_ms=float("nan")), dict(ts=1)])
async def test_mismatched_or_invalid_stats_invalidate_fight(fake, change):
    gate, run = Gate(fake), run_for(fake)
    await gate.on_start(run)
    await gate.on_muster(run, 0)
    await gate.on_boss(boss(fake))
    await collect(fake, gate, True, values=(41,))
    await fake.clock.advance(30, step=30)
    fake.events["server_stats"].append(stats(fake, True, **change))
    await gate.sample()
    await collect(fake, gate, True)
    await gate.on_boss(boss(fake, state="killed"))
    assert not perf_rows(fake.conn)


@_run_async
async def test_unmatched_stats_and_rejected_request_are_not_samples(fake):
    gate, run = Gate(fake), run_for(fake)
    await gate.on_start(run)
    await gate.on_muster(run, 0)
    await gate.on_boss(boss(fake))
    await fake.clock.advance(30, step=30)
    fake.events["server_stats"].append(stats(fake, True, id="another-command"))
    await gate.sample()
    fake.acks["get_stats"].append((False, "unavailable"))
    await collect(fake, gate, True, values=(41,))
    await gate.on_boss(boss(fake, state="killed"))
    assert not perf_rows(fake.conn)


@_run_async
async def test_aborted_run_and_replayed_terminal_do_not_add_evidence(fake):
    gate, run = Gate(fake), run_for(fake)
    await gate.on_start(run)
    await fight(fake, gate, run)
    terminal = boss(fake, state="killed")
    await gate.on_boss(terminal)
    assert len(perf_rows(fake.conn)) == 1
    await gate.on_muster(run, 1)
    await gate.on_boss(boss(fake, 1))
    await collect(fake, gate, False)
    gate.invalidate(run)
    await gate.on_end("aborted", run=run)
    await gate.on_boss(boss(fake, 1, "wiped"))
    assert len(perf_rows(fake.conn)) == 1 and not gate.recording()


@_run_async
async def test_receiptless_and_tampered_perf_rows_cannot_pass_gate(fake):
    fake.cfg = replace(fake.cfg, gate_pulls=4)
    gate, run = Gate(fake), run_for(fake)
    await gate.on_start(run)
    await fight(fake, gate, run)
    row = perf_rows(fake.conn)[0]
    key = f"tg_gate:perf:{row['fight']}"
    receipt = metadata(fake, key)
    assert gate._eligible(row)
    for field, value in (("generation", ""), ("armed", False), ("context", "649:0:0"),
                         ("mean_ms", 99), ("outcome", "aborted"), ("basis", "unknown"),
                         ("count", 1), ("instance_id", 0),
                         ("samples", [receipt["samples"][0]] * 2)):
        Store(fake.conn, clock=fake.clock).set_meta(key, json.dumps({**receipt, field: value}))
        assert not gate._eligible(row)
    for index in range(4):
        for armed in (True, False):
            put_perf(fake.conn, fake.clock.now(), f"orphan-{index}-{armed}", f"631:{index}:0",
                     "offline", armed, 40)
    assert Gate(fake).verdict() == ("ab", None)


@_run_async
async def test_corrupt_metadata_fails_closed(fake):
    store = Store(fake.conn, clock=fake.clock)
    for raw in ("not-json", "[]", '{"phase":"passed","raids":0,"verdict_ms":null}'):
        store.set_meta("tg_gate", raw)
        gate, run = Gate(fake), run_for(fake)
        await gate.on_start(run)
        await gate.on_muster(run, 0)
        assert gate.phase == "failed" and not gate.recording()


@_run_async
@pytest.mark.parametrize("invalid", [[], {}], ids=["list", "dict"])
async def test_container_values_in_gate_metadata_fail_closed(fake, invalid):
    store = Store(fake.conn, clock=fake.clock)
    for field in ("phase", "raids", "verdict_ms"):
        saved = dict(phase="passed", raids=2, verdict_ms=0.5)
        saved[field] = invalid
        store.set_meta("tg_gate", json.dumps(saved))
        gate, run = Gate(fake), run_for(fake)
        await gate.on_start(run)
        await gate.on_muster(run, 0)
        assert gate.verdict() == ("failed", None)
        assert not gate.recording() and not gate.record_flag(run, 0)


@_run_async
@pytest.mark.parametrize("invalid", [[], {}], ids=["list", "dict"])
async def test_container_values_in_persisted_receipt_are_ignored(fake, invalid):
    gate, run = Gate(fake), run_for(fake)
    await gate.on_start(run)
    await fight(fake, gate, run)
    row = perf_rows(fake.conn)[0]
    key = f"tg_gate:perf:{row['fight']}"
    receipt = metadata(fake, key)
    assert gate._eligible(row)
    store = Store(fake.conn, clock=fake.clock)
    for field in receipt:
        store.set_meta(key, json.dumps({**receipt, field: invalid}))
        assert not gate._eligible(row)
        assert Gate(fake).verdict() == ("ab", None)


@_run_async
async def test_absent_gate_metadata_starts_ab_but_json_null_fails_closed(fake):
    assert metadata(fake) is None
    assert Gate(fake).phase == "ab"
    Store(fake.conn, clock=fake.clock).set_meta("tg_gate", "null")
    gate, run = Gate(fake), run_for(fake)
    await gate.on_start(run)
    await gate.on_muster(run, 0)
    assert gate.verdict() == ("failed", None)
    assert not gate.record_flag(run, 0) and not gate.recording()
    assert fake.sent[-1].record is False


@_run_async
@pytest.mark.parametrize("transition", ["disconnect", "hello"])
@pytest.mark.parametrize("same_ms", [False, True], ids=["older", "same-ms"])
async def test_unseen_engaged_before_connection_fence_cannot_qualify(fake, transition, same_ms):
    gate, run = Gate(fake), run_for(fake)
    await gate.on_start(run)
    await gate.on_muster(run, 0)
    await fake.clock.advance(1, step=1)
    delayed = boss(fake)  # This engagement has never reached Gate.
    if not same_ms:
        await fake.clock.advance(1, step=1)
    boundary_ms = int(fake.clock.now() * 1000)
    if transition == "disconnect":
        await gate.on_connection(False)
        await gate.on_connection(True)
    await gate.on_hello()
    assert gate.recording()  # New ACK alone cannot rehabilitate the old pull.
    if same_ms:
        await gate.on_muster(run, 0)  # Cannot lower the connection's timestamp floor.
    await gate.on_boss(delayed)
    for _ in range(2):
        await fake.clock.advance(30, step=30)
        await gate.sample()
    await gate.on_boss(boss(fake, state="killed"))
    assert not perf_rows(fake.conn)
    assert not any(isinstance(cmd, GetStats) for cmd in fake.sent)

    # A newly observed post-fence pull still follows the normal workflow.
    await gate.on_muster(run, 0)
    fresh = boss(fake)
    assert fresh.ts > boundary_ms
    await gate.on_boss(fresh)
    await collect(fake, gate, True)
    await gate.on_boss(boss(fake, state="killed"))
    rows = perf_rows(fake.conn)
    assert len(rows) == 1 and gate._eligible(rows[0])
    assert metadata(fake, f"tg_gate:perf:{rows[0]['fight']}")["start_ms"] == fresh.ts
