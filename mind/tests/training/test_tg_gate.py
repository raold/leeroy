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
from leeroy_mind.training.gate import HOLD_S, Gate
from leeroy_mind.training.protocol import GetStats, ServerStats, Telemetry
from leeroy_mind.training.store import full_since_ms, perf_rows, put_perf


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
    await settle(fake, gate)


async def settle(fake, gate):
    """After a terminal: the hold on a disarm ends and the sampler's ticker sends it."""
    await fake.clock.advance(HOLD_S, step=HOLD_S)
    await gate.sample()


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
    await settle(fake, gate)


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
    # His raid alternates too, on its own family counter: the offline count does not move it.
    await gate.on_start(human)
    assert [gate.record_flag(human, i) for i in range(4)] == [True, False, True, False]
    await gate.on_start(second)
    assert [gate.record_flag(second, i) for i in range(4)] == [False, True, False, True]
    assert metadata(fake) == dict(phase="ab", raids=3, verdict_ms=None)
    assert metadata(fake, "tg_gate:family:631:0") == 2
    assert metadata(fake, "tg_gate:family:his:631:0") == 1
    restarted = Gate(fake)
    await restarted.on_start(second)
    assert not restarted.record_flag(second, 0)
    assert metadata(fake)["raids"] == 3


@_run_async
async def test_rotating_offline_raids_compare_each_boss_on_both_sides(fake):
    # Guild life rotates its offline raids, least recently run first, so a
    # raid's next visit is a full rotation later. Parity counted over all raids
    # would keep every boss on one side and the gate could never decide.
    gate, last = Gate(fake), {}
    sides = {}
    rotation = GuildConfig().offline_rotation
    bosses = -(-12 // len(rotation))  # Twelve compared contexts, whatever the rotation's length.
    for ident in range(1, 2 * len(rotation) + 1):
        key = next_raid_key(rotation, last)
        raid, size, heroic = split_key(key)
        run = run_for(fake, ident, size=size, heroic=heroic, map_id=raid.map_id, instance=100 + ident)
        await gate.on_start(run)
        for index in range(bosses):
            sides.setdefault((key, index), []).append(gate.record_flag(run, index))
            await family_fight(fake, gate, run, index)
        last[key] = fake.clock.now()
    assert all(sorted(seen) == [False, True] for seen in sides.values()) and len(sides) == bosses * len(rotation)
    assert gate.verdict() == ("passed", 0.0)
    assert metadata(fake)["raids"] == 2 * len(rotation)
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
async def test_samples_offline_and_his_fights_on_thirty_second_ticks(fake):
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
    his = perf_rows(fake.conn, kind="his")
    assert len(his) == 1 and his[0]["armed"] == 1 and gate._eligible(his[0])
    assert metadata(fake, f"tg_gate:perf:{his[0]['fight']}")["kind"] == "his"
    assert len([c for c in fake.sent if isinstance(c, GetStats)]) == 5


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


def his_run(fake, ident=1, *, instance=100, size=10, heroic=False, map_id=631, lead=True):
    """His raid: a raid night or his own add-command group. When he leads, no dungeon-clear muster."""
    plan = RaidPlan("icc", size, heroic, False, 1 if lead else 101, 101, 101, [101, 102], [], True)
    session = SimpleNamespace(raid_id=ident, started=fake.clock.now(), _closing=False,
                              ctx=SimpleNamespace(map_id=map_id, instance_id=instance))
    return RaidRun(plan, session, True, lead)


async def his_pull(fake, gate, index=0, outcome="killed", values=(41, 41)):
    """One pull of his lead: no muster; the gate's own flag decides armed or not."""
    armed = flags(fake)[-1]
    ctx = gate._run.session.ctx
    at = dict(map=ctx.map_id, instance_id=ctx.instance_id)
    await gate.on_boss(boss(fake, index, **at))
    await collect(fake, gate, armed, values)
    await gate.on_boss(boss(fake, index, outcome, **at))
    await settle(fake, gate)
    return armed


def flags(fake):
    return [cmd.record for cmd in fake.sent if isinstance(cmd, Telemetry)]


@_run_async
async def test_his_lead_alternates_by_kill_position_without_a_muster(fake):
    gate, run = Gate(fake), his_run(fake)
    await gate.on_start(run)
    assert flags(fake) == [True] and gate.recording()
    # Slot = kills so far + wipes since the last kill: the wipe's retry is on the other side,
    # and each boss's first pull keeps its kill position (boss 1 at slot 1, boss 2 at slot 2).
    sides = [await his_pull(fake, gate, 0, "wiped"), await his_pull(fake, gate, 0),
             await his_pull(fake, gate, 1), await his_pull(fake, gate, 2, "wiped")]
    assert sides == [True, False, False, True]
    assert flags(fake) == [True, False, True, False]
    rows = perf_rows(fake.conn, kind="his")
    assert [(r["context"], r["armed"]) for r in rows] == [
        ("631:0:0", 1), ("631:0:0", 0), ("631:1:0", 0), ("631:2:0", 1)]
    assert all(gate._eligible(r) for r in rows) and not perf_rows(fake.conn)
    await gate.on_end("ended", run=run)
    assert flags(fake)[-1] is False and not gate.recording()
    # His next visit of this raid starts on the other side.
    again = his_run(fake, 2, instance=101)
    await gate.on_start(again)
    assert flags(fake)[-1] is False
    assert metadata(fake) == dict(phase="ab", raids=2, verdict_ms=None)


ICC = (0, 1, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12)  # the twelve fights; 2 (the gunship) left out


@_run_async
async def test_a_repeated_route_pairs_in_two_nights_whatever_the_wipes(fake):
    fake.cfg = replace(fake.cfg, gate_pulls=12)
    gate = Gate(fake)
    for night, wipe in ((1, 1), (2, None)):  # one wipe on the second boss, first night only
        run = his_run(fake, night, instance=100 + night)
        await gate.on_start(run)
        for index in ICC:
            if index == wipe:
                await his_pull(fake, gate, index, "wiped")
            await his_pull(fake, gate, index)
        if gate.phase == "ab":
            await gate.on_end("ended", run=run)
    sides = {}
    for row in perf_rows(fake.conn, kind="his"):
        sides.setdefault(row["context"], set()).add(row["armed"])
    assert sorted(sides) == sorted(f"631:{i}:0" for i in ICC)
    assert all(seen == {0, 1} for seen in sides.values())
    assert gate.verdict() == ("passed", 0.0)


@_run_async
async def test_his_lead_terminal_without_engage_or_foreign_boss_does_not_advance(fake):
    gate, run = Gate(fake), his_run(fake)
    await gate.on_start(run)
    await gate.on_boss(boss(fake, 0, "killed"))
    await gate.on_boss(boss(fake, 13))  # a non-fight step is never a pull
    await gate.on_boss(boss(fake, 0, map=649))
    await gate.on_boss(boss(fake, 13, "killed"))
    await settle(fake, gate)
    assert flags(fake) == [True]
    await gate.on_boss(boss(fake, 3))
    await gate.on_boss(boss(fake, 4, "killed"))  # another boss's terminal is not this pull's
    await settle(fake, gate)
    assert flags(fake) == [True]
    await gate.on_boss(boss(fake, 3, "killed"))
    await settle(fake, gate)
    assert flags(fake) == [True, False]


def header(fake, index, engaged_ms, *, instance=100):
    return SimpleNamespace(map=631, instance_id=instance, boss_id=index, start_ms=engaged_ms + 200)


@_run_async
async def test_a_disarm_waits_for_the_armed_pulls_record(fake):
    gate, run = Gate(fake), his_run(fake)
    await gate.on_start(run)
    engaged = boss(fake, 0)
    await gate.on_boss(engaged)
    await fake.clock.advance(60, step=60)
    await gate.on_boss(boss(fake, 0, "killed"))
    assert flags(fake) == [True]  # never on the terminal itself: native may still be encoding it
    await fake.clock.advance(5, step=5)
    await gate.sample()
    gate.pull_stored(header(fake, 0, engaged.ts, instance=101))  # another instance's pull
    gate.pull_stored(header(fake, 1, engaged.ts))                # another boss's pull
    await gate.sample()
    assert flags(fake) == [True]
    gate.pull_stored(header(fake, 0, engaged.ts))  # its header is stored: the disarm may go
    await gate.sample()
    assert flags(fake) == [True, False]
    # Arming is never held: the unarmed pull's kill arms the next one at once.
    await gate.on_boss(boss(fake, 1))
    await fake.clock.advance(60, step=60)
    await gate.on_boss(boss(fake, 1, "killed"))
    assert flags(fake) == [True, False, True]
    # Without a stored header the hold ends after HOLD_S; ending the run is held too.
    await fake.clock.advance(1, step=1)
    await gate.on_boss(boss(fake, 2))
    await fake.clock.advance(60, step=60)
    await gate.on_boss(boss(fake, 2, "killed"))
    await gate.on_end("ended", run=run)
    await fake.clock.advance(HOLD_S - 1, step=1)
    await gate.sample()
    assert flags(fake) == [True, False, True]
    await fake.clock.advance(1, step=1)
    await gate.sample()
    assert flags(fake) == [True, False, True, False]
    await gate.sample()
    assert flags(fake) == [True, False, True, False]


@_run_async
async def test_a_dungeon_clear_muster_after_an_armed_kill_holds_its_disarm(fake):
    gate, run = Gate(fake), run_for(fake)
    await gate.on_start(run)
    await gate.on_muster(run, 0)
    await gate.on_boss(boss(fake, 0))
    await collect(fake, gate, True)
    await gate.on_boss(boss(fake, 0, "killed"))
    await gate.on_muster(run, 1)  # the tank rests with the next boss named
    assert flags(fake) == [True] and not gate.recording()
    await gate.on_boss(boss(fake, 1))  # pulled within the hold: no unarmed sample from it
    await settle(fake, gate)
    assert flags(fake) == [True, False]
    await gate.on_boss(boss(fake, 1, "killed"))
    assert len(perf_rows(fake.conn)) == 1


@_run_async
async def test_bot_led_his_raid_keeps_the_boss_muster(fake):
    gate, run = Gate(fake), his_run(fake, lead=False)
    await gate.on_start(run)
    assert not fake.sent  # Dungeon-clear's muster names the boss, as in offline raids.
    await fight(fake, gate, run, 0)
    await fight(fake, gate, run, 1)
    assert flags(fake) == [True, False]
    assert [r["armed"] for r in perf_rows(fake.conn, kind="his")] == [1, 0]


@_run_async
async def test_verdict_pairs_only_within_one_kind(fake):
    fake.cfg = replace(fake.cfg, gate_pulls=4)
    gate = Gate(fake)
    offline = run_for(fake, 1)
    await gate.on_start(offline)
    for index in range(4):  # offline raid 0: bosses 0 and 2 armed, 1 and 3 not
        await fight(fake, gate, offline, index, values=(41, 41))
    await gate.on_end("ended", run=offline)
    order = (1, 0, 3, 2)

    async def his_night(ident):
        run = his_run(fake, ident, instance=100 + ident)
        await gate.on_start(run)
        for index in order:
            await his_pull(fake, gate, index, values=(41, 41) if fake.sent[-1].record else (40, 40))
        return run

    # His first night arms bosses 1 and 3: every context now holds an armed and an
    # unarmed fight, but each pair would cross kinds, so there is no verdict.
    first = await his_night(2)
    sides = {(r["context"], r["kind"]): r["armed"] for r in perf_rows(fake.conn) + perf_rows(fake.conn, kind="his")}
    assert all(sides[c, "offline"] != sides[c, "his"] for c in ("631:0:0", "631:1:0", "631:2:0", "631:3:0"))
    assert gate.verdict() == ("ab", None)
    await gate.on_end("ended", run=first)
    await his_night(3)  # his next visit starts on the other side
    assert gate.verdict() == ("passed", 1.0)
    saved = metadata(fake)
    assert saved["phase"] == "passed" and saved["passed_at"] <= fake.clock.now()
    # The pass time survives a restart; from it on, stored pulls are every pull.
    Gate(fake)._save()
    assert metadata(fake)["passed_at"] == saved["passed_at"]
    assert full_since_ms(fake.conn) == int(saved["passed_at"] * 1000)


@_run_async
async def test_passed_records_every_pull_of_his_lead(fake):
    Store(fake.conn, clock=fake.clock).set_meta("tg_gate", json.dumps(dict(phase="passed", raids=4, verdict_ms=0.2)))
    gate, run = Gate(fake), his_run(fake)
    await gate.on_start(run)
    for index in range(3):
        await gate.on_boss(boss(fake, index))
        assert gate.recording()
        await gate.on_boss(boss(fake, index, "killed"))
        await fake.clock.advance(1, step=1)
    assert flags(fake) == [True]  # armed once, kept for every pull
    assert not any(isinstance(c, GetStats) for c in fake.sent) and not perf_rows(fake.conn, kind="his")
    assert metadata(fake, "tg_gate:raid:1") is None


@_run_async
async def test_failed_gate_records_none_of_his_pulls(fake):
    Store(fake.conn, clock=fake.clock).set_meta("tg_gate", json.dumps(dict(phase="failed", raids=4, verdict_ms=1.5)))
    gate = Gate(fake)
    for run in (his_run(fake), his_run(fake, 2, lead=False), run_for(fake, 3)):
        await gate.on_start(run)
        await gate.on_muster(run, 0)
        await gate.on_boss(boss(fake, 0))
        assert not gate.recording()
        await gate.on_boss(boss(fake, 0, "killed"))
        await fake.clock.advance(1, step=1)
        assert not any(gate.record_flag(run, i) for i in range(4))
    assert flags(fake) and not any(flags(fake))
    assert not perf_rows(fake.conn) and not perf_rows(fake.conn, kind="his")


@_run_async
async def test_failed_verdict_from_his_raids_disarms_his_lead(fake):
    fake.cfg = replace(fake.cfg, gate_pulls=4)
    gate = Gate(fake)
    for ident in (1, 2):
        run = his_run(fake, ident, instance=100 + ident)
        await gate.on_start(run)
        for index in range(4):
            await his_pull(fake, gate, index, values=(43, 43) if fake.sent[-1].record else (41, 41))
        if gate.phase == "ab":
            await gate.on_end("ended", run=run)
    assert gate.verdict() == ("failed", 2.0)
    assert flags(fake)[-1] is False and not gate.recording()
    await gate.on_boss(boss(fake, 5))
    await gate.on_boss(boss(fake, 5, "killed"))
    assert flags(fake)[-1] is False


@_run_async
async def test_legacy_receipt_without_kind_counts_as_offline_only(fake):
    gate, run = Gate(fake), run_for(fake)
    await gate.on_start(run)
    await fight(fake, gate, run)
    row = perf_rows(fake.conn)[0]
    key = f"tg_gate:perf:{row['fight']}"
    receipt = metadata(fake, key)
    legacy = {k: v for k, v in receipt.items() if k != "kind"}
    Store(fake.conn, clock=fake.clock).set_meta(key, json.dumps(legacy))
    assert gate._eligible(row)
    Store(fake.conn, clock=fake.clock).set_meta(key, json.dumps({**receipt, "kind": "his"}))
    assert not gate._eligible(row)
