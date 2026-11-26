import asyncio
from dataclasses import replace
from datetime import timedelta, timezone
from functools import wraps
import random

import pytest

from leeroy_mind.guild.calls import CallGap
from leeroy_mind.guild.config import GuildConfig
from leeroy_mind.guild.habits import BetweenPulls, FEASTS, FLASK_FOR, MAX_COMMANDS_PER_TICK
from leeroy_mind.guild.loot import _get, _put
from leeroy_mind.guild.protocol import BotDo, JobSkip, StockItems
from leeroy_mind.guild.raid_night import RaidRun
from leeroy_mind.guild.session import RaidPlan, RaidSession
from leeroy_mind.protocol import Ack, Boss, DcStatus, Death, Resurrect
from leeroy_mind.talk.turns import TurnKind
from .fakes import FakeArmory, job, member, persona


def _run_async(test):
    @wraps(test)
    def run(*args, **kwargs):
        return asyncio.run(test(*args, **kwargs))
    return run


class FixedRandom:
    def __init__(self, value=0.1):
        self.value, self.calls = value, 0

    def random(self):
        self.calls += 1
        return self.value


@pytest.fixture
def setup(fake):
    specs = {101: (1, 2, "tank"), 102: (5, 1, "healer"), 103: (11, 2, "healer"),
             104: (3, 1, "ranged"), 105: (8, 1, "ranged"), 106: (4, 1, "melee"),
             107: (1, 1, "melee"), 109: (9, 1, "ranged"), 110: (8, 1, "ranged")}
    for guid, (cls, spec, role) in specs.items():
        fake.members[guid] = member(guid, f"Member{guid}", cls, spec, map=631)
        fake.personas[guid] = persona(guid, f"Member{guid}", raid_role=role,
                                     officer_title={103: "Guild Baker", 110: "Raid Chef"}.get(guid, ""),
                                     skill="sloppy" if guid == 106 else "solid",
                                     thinks_as="theorycrafter" if guid == 107 else "social")
    plan = RaidPlan("icc", 10, False, False, 101, 101, 109, list(specs), [], True)
    session = asyncio.run(RaidSession.open(fake, GuildConfig(), FakeArmory({}, {}), plan))
    session.bind_instance(77)
    run = RaidRun(session.plan, session, True, False)

    async def wait_job(cmd_id, timeout_s):
        command = next(c for c in fake.sent if c.id == cmd_id)
        script = fake.jobs["bot_do"]
        if script:
            result = script.popleft()
            if callable(result):
                result = result(command)
            if result is None:
                return None
            return replace(result, id=cmd_id) if not result.id else result
        return job("bot_do", "done", id=cmd_id, guids=command.guids, ts=stamp(fake))

    fake.wait_job = wait_job
    habits = BetweenPulls(fake, FixedRandom(), gap=CallGap(fake.clock))
    return fake, run, habits


def stamp(ports):
    return int(ports.clock.now() * 1000)


async def advance(ports, seconds):
    await ports.clock.advance(seconds, step=max(seconds, 1))


async def drain(habits):
    for _ in range(8):
        await habits.tick()


async def start(setup):
    ports, run, habits = setup
    await habits.on_start(run)
    await drain(habits)
    return ports, run, habits


@_run_async
async def test_flask_skip_snapshot_comes_from_this_raids_persisted_choices(setup):
    ports, run, habits = setup
    habits.flask_skips = {999}
    await habits.on_start(run)
    expected = {guid for guid in run.plan.members
        if _get(ports.conn, f"guild:habits:skip:{habits._night}:{guid}") is True}
    assert habits.flask_skips == expected and 999 not in habits.flask_skips


def boss(ports, run, state="engaged", **changes):
    fields = dict(ts=stamp(ports), map=631, instance_id=77, boss_id=0, boss_name="",
                  state=state, members=tuple(run.plan.members))
    fields.update(changes)
    return Boss(**fields)


async def wipe(ports, run, habits):
    await habits.on_boss(boss(ports, run))
    await advance(ports, 1)
    event = boss(ports, run, "wiped")
    await habits.on_boss(event)
    return event


def dc(ports, *, entry=36627, name="Rotface", **changes):
    fields = dict(ts=stamp(ports), tank_guid=101, enabled=True, paused=False,
                  payload=f"STATUS\t1\t{entry}\t{name}\t\t0\tresting\tdrinking\t2\t1\t0")
    fields.update(changes)
    return DcStatus(**fields)


def test_approved_consumables_are_exact():
    assert FLASK_FOR == {"tank": 46379, "healer": 46376, "int": 46376, "agi": 46377, "str": 46377}
    assert FEASTS == (43015, 34753)


@_run_async
async def test_event_handlers_never_wait_on_bridge(setup):
    ports, run, habits = setup
    await habits.on_start(run)
    await habits.on_ready(run)
    await habits.on_dc_status(dc(ports))
    await wipe(ports, run, habits)
    assert ports.sent == ports.proposed == ports.said == []


@_run_async
async def test_flasks_stock_two_and_explicitly_use_exact_role_item_except_skippers(setup):
    ports, run, habits = await start(setup)
    stock = {c.guid: c.items[0] for c in ports.sent if isinstance(c, StockItems) and c.items[0] not in FEASTS}
    assert stock[101] == 46379 and stock[102] == 46376 and stock[104] == 46377
    assert 106 not in stock and 107 in stock and 1 not in stock
    assert all(c.count == 2 for c in ports.sent if isinstance(c, StockItems))
    use = [c for c in ports.sent if isinstance(c, BotDo) and int(c.param) not in FEASTS]
    assert {g: int(c.param) for c in use for g in c.guids} == stock
    assert all(c.action == "use" for c in use)
    assert not any(isinstance(c, BotDo) and c.param == "buff" for c in ports.sent)


@_run_async
async def test_each_tick_has_a_fixed_remote_command_budget(setup):
    ports, run, habits = setup
    await habits.on_start(run)
    for _ in range(6):
        before = len(ports.sent)
        await habits.tick()
        assert len(ports.sent) - before <= MAX_COMMANDS_PER_TICK
    assert habits._get("initial-done") is True


@_run_async
async def test_feast_requires_successful_stock_and_matching_use_then_same_cooks_aside(setup):
    ports, run, habits = await start(setup)
    stocks = [c for c in ports.sent if isinstance(c, StockItems) and c.items == (43015,)]
    uses = [c for c in ports.sent if isinstance(c, BotDo) and c.param == "43015"]
    assert len(stocks) == len(uses) == len(ports.proposed) == 1
    cook = stocks[0].guid
    assert cook in {103, 110} and uses[0].guids == (cook,)
    assert ports.proposed == [(cook, "raid", "You put down Fish Feast. Let the raid know in a few words, your way.",
                               TurnKind.ASIDE)]


@_run_async
async def test_cooks_alternate_by_logical_night_not_clock_at_retry(setup):
    ports, run, habits = await start(setup)
    first = next(c.guid for c in ports.sent if isinstance(c, StockItems) and c.items == (43015,))
    await habits.on_end("done")
    with ports.conn:
        ports.conn.execute("UPDATE guild_raid SET ended=?", (ports.clock.now(),))
    await advance(ports, 86400)
    session = await RaidSession.open(ports, GuildConfig(), FakeArmory({}, {}), run.plan)
    session.bind_instance(78)
    next_run = RaidRun(session.plan, session, True, False)
    ports.sent.clear()
    await habits.on_start(next_run)
    await drain(habits)
    second = next(c.guid for c in ports.sent if isinstance(c, StockItems) and c.items == (43015,))
    assert first != second


@_run_async
async def test_no_cook_never_stocks_or_announces_feast(setup):
    ports, run, habits = setup
    for p in ports.personas.values():
        p.play.officer_title = ""
    await start(setup)
    assert not any(isinstance(c, StockItems) and c.items[0] in FEASTS for c in ports.sent)
    assert not ports.proposed


@_run_async
async def test_restart_never_repeats_initial_actions_or_rerolls_skips(setup):
    ports, run, habits = await start(setup)
    before = len(ports.sent)
    rng = FixedRandom(.99)
    reopened = BetweenPulls(ports, rng, gap=CallGap(ports.clock))
    await reopened.on_start(run)
    await drain(reopened)
    assert len(ports.sent) == before and rng.calls == 0 and len(ports.proposed) == 1
    assert "106" not in reopened._get("flasks")


@_run_async
async def test_reopened_raid_retains_its_first_logical_night_even_if_local_offset_changes(setup):
    ports, run, habits = await start(setup)
    night, day = habits._night, habits._day
    original = ports.clock.local
    ports.clock.local = lambda: original().astimezone(timezone(timedelta(hours=13)))
    reopened = BetweenPulls(ports, random.Random(), gap=CallGap(ports.clock))
    await reopened.on_start(run)
    assert reopened._night == night and reopened._day == day


@_run_async
async def test_second_raid_same_night_reuses_member_skip_decision(setup):
    ports, run, habits = await start(setup)
    with ports.conn:
        ports.conn.execute("UPDATE guild_raid SET ended=?", (ports.clock.now(),))
    await habits.on_end("done")
    session = await RaidSession.open(ports, GuildConfig(), FakeArmory({}, {}), run.plan)
    session.bind_instance(78)
    rng = FixedRandom(.99)
    second = BetweenPulls(ports, rng, gap=CallGap(ports.clock))
    await second.on_start(RaidRun(session.plan, session, True, False))
    assert "106" not in second._get("flasks") and rng.calls == 0


@_run_async
async def test_wipe_waits_sixty_seconds_and_reuses_flasks_without_restock(setup):
    ports, run, habits = await start(setup)
    await advance(ports, 300)
    event = await wipe(ports, run, habits)
    ports.sent.clear()
    await advance(ports, 59)
    await habits.tick()
    assert not ports.sent
    await advance(ports, 1)
    await drain(habits)
    assert ports.sent and all(isinstance(c, BotDo) and c.action == "use" for c in ports.sent)
    assert all(c.param != "43015" for c in ports.sent) and len(ports.proposed) == 1
    count = len(ports.sent)
    await habits.on_boss(event)
    await drain(habits)
    assert len(ports.sent) == count


@_run_async
async def test_feast_after_later_wipe_and_cooldown_survives_reconstruction(setup):
    ports, run, habits = await start(setup)
    await advance(ports, 700)
    await wipe(ports, run, habits)
    ports.sent.clear()
    reopened = BetweenPulls(ports, random.Random(10), gap=CallGap(ports.clock))
    await reopened.on_start(run)
    await advance(ports, 60)
    await drain(reopened)
    assert len(ports.proposed) == 2
    stocks = [c for c in ports.sent if isinstance(c, StockItems)]
    assert len(stocks) == 1 and stocks[0].items == (43015,)


@_run_async
async def test_new_engagement_cancels_recovery_consumables(setup):
    ports, run, habits = await start(setup)
    await advance(ports, 700)
    await wipe(ports, run, habits)
    await advance(ports, 1)
    await habits.on_boss(boss(ports, run))
    ports.sent.clear()
    await advance(ports, 60)
    await drain(habits)
    assert not ports.sent and len(ports.proposed) == 1


@_run_async
async def test_known_deaths_excluded_until_ordered_resurrection_after_reopen(setup):
    ports, run, habits = await start(setup)
    await habits.on_boss(boss(ports, run))
    dead = Death(stamp(ports), 102, 631, 0)
    await habits.on_death(dead)
    await advance(ports, 1)
    await habits.on_boss(boss(ports, run, "wiped"))
    await advance(ports, 1)
    await habits.on_resurrect(Resurrect(stamp(ports), 102))
    await habits.on_death(dead)
    reopened = BetweenPulls(ports, random.Random(), gap=CallGap(ports.clock))
    await reopened.on_start(run)
    await reopened.on_death(dead)
    await reopened.on_death(Death(stamp(ports), 104, 631, 0))
    ports.sent.clear()
    await advance(ports, 60)
    await drain(reopened)
    used = {g for c in ports.sent if isinstance(c, BotDo) for g in c.guids}
    assert 102 in used and 104 not in used


@_run_async
async def test_only_pull_participants_get_recovery_flasks(setup):
    ports, run, habits = await start(setup)
    participants = tuple(g for g in run.plan.members if g != 104)
    await habits.on_boss(boss(ports, run, members=participants))
    await advance(ports, 1)
    await habits.on_boss(boss(ports, run, "wiped", members=participants))
    ports.sent.clear()
    await advance(ports, 60)
    await drain(habits)
    assert 104 not in {g for c in ports.sent if isinstance(c, BotDo) for g in c.guids}


@pytest.mark.parametrize("change", ["id", "kind", "state", "skipped", "duplicate", "stale", "none"])
@_run_async
async def test_feast_never_announced_for_unproven_use(setup, change):
    ports, run, habits = setup
    original = ports.wait_job

    async def result(cmd_id, timeout_s):
        command = next(c for c in ports.sent if c.id == cmd_id)
        if command.param != "43015":
            return await original(cmd_id, timeout_s)
        if change == "none":
            return None
        fields = {"id": {"id": "foreign"}, "kind": {"kind": "gear_up"}, "state": {"state": "progress"},
                  "skipped": {"skipped": (JobSkip(command.guids[0], "failed"),)},
                  "duplicate": {"guids": command.guids * 2}, "stale": {"ts": stamp(ports) - 6000}}
        return replace(job("bot_do", "done", id=cmd_id, guids=command.guids, ts=stamp(ports)), **fields[change])

    ports.wait_job = result
    await start(setup)
    assert not ports.proposed
    before = len(ports.sent)
    reopened = BetweenPulls(ports, random.Random(), gap=CallGap(ports.clock))
    await reopened.on_start(run)
    await drain(reopened)
    assert len(ports.sent) == before


@_run_async
async def test_failed_flask_stock_excludes_member_from_initial_and_recovery_use(setup):
    ports, run, habits = setup
    original = ports.send

    async def fail(command):
        ack = await original(command)
        return replace(ack, ok=False, error="bags_full") if isinstance(command, StockItems) and command.guid == 102 else ack

    ports.send = fail
    await start(setup)
    await wipe(ports, run, habits)
    await advance(ports, 60)
    await drain(habits)
    assert 102 not in {g for c in ports.sent if isinstance(c, BotDo) for g in c.guids}


@_run_async
async def test_failed_feast_stock_never_uses_or_announces_it(setup):
    ports, run, habits = setup
    original = ports.send

    async def fail(command):
        ack = await original(command)
        return replace(ack, ok=False, error="bags_full") if isinstance(command, StockItems) and command.items == (43015,) else ack

    ports.send = fail
    await start(setup)
    assert not ports.proposed and not any(isinstance(c, BotDo) and c.param == "43015" for c in ports.sent)


@pytest.mark.parametrize("mutation", ["end", "presence", "instance", "plan", "dead", "title"])
@_run_async
async def test_feast_revalidates_run_and_cook_after_job_await(setup, mutation):
    ports, run, habits = setup
    original = ports.wait_job

    async def changed(cmd_id, timeout_s):
        command = next(c for c in ports.sent if c.id == cmd_id)
        result = await original(cmd_id, timeout_s)
        if command.param == "43015":
            if mutation == "end":
                await habits.on_end("called")
            elif mutation == "presence":
                ports.online = False
            elif mutation == "instance":
                run.session.ctx.instance_id = 78
            elif mutation == "plan":
                with ports.conn:
                    _put(ports.conn, f"guild:raid:{run.session.raid_id}:plan", {})
            elif mutation == "dead":
                await habits.on_death(Death(stamp(ports), command.guids[0], 631, 0))
            else:
                ports.personas[command.guids[0]].play.officer_title = ""
        return result

    ports.wait_job = changed
    await start(setup)
    assert not ports.proposed


@_run_async
async def test_engagement_during_stock_ack_stops_consumption_until_between_pulls(setup):
    ports, run, habits = setup
    original = ports.send

    async def engaged(command):
        ack = await original(command)
        if isinstance(command, StockItems):
            await habits.on_boss(boss(ports, run))
        return ack

    ports.send = engaged
    await start(setup)
    assert len(ports.sent) == 1 and isinstance(ports.sent[0], StockItems)


@_run_async
async def test_cancelled_stock_is_not_resent_after_reconstruction(setup):
    ports, run, habits = setup
    original, entered = ports.send, asyncio.Event()

    async def hanging(command):
        await original(command)
        entered.set()
        await asyncio.Event().wait()

    ports.send = hanging
    await habits.on_start(run)
    task = asyncio.create_task(habits.tick())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    ports.send = original
    reopened = BetweenPulls(ports, random.Random(), gap=CallGap(ports.clock))
    await reopened.on_start(run)
    await drain(reopened)
    stocks = [c for c in ports.sent if isinstance(c, StockItems) and c.guid == 101]
    assert len(stocks) == 1 and 101 not in {g for c in ports.sent if isinstance(c, BotDo) for g in c.guids}


@_run_async
async def test_total_command_timeout_bounds_even_send_and_retains_attempt(setup, monkeypatch):
    ports, run, habits = setup
    monkeypatch.setattr("leeroy_mind.guild.habits.COMMAND_TIMEOUT_S", .01)
    original = ports.send

    async def hanging(command):
        await original(command)
        await asyncio.Event().wait()

    ports.send = hanging
    await habits.on_start(run)
    await asyncio.wait_for(habits.tick(), 1)
    assert len(ports.sent) <= MAX_COMMANDS_PER_TICK
    assert habits._get("stock:101")["state"] == "unknown"


@_run_async
async def test_drink_once_per_boss_and_shared_gap_drops_crowded_lines(setup):
    ports, run, habits = await start(setup)
    await habits.on_dc_status(dc(ports))
    await habits.on_dc_status(dc(ports))
    await habits.tick()
    assert ports.said == [(101, "raid", "drink up, pulling when everyone's full")]
    await advance(ports, 5)
    await habits.on_dc_status(dc(ports, entry=36626, name="Festergut"))
    await habits.tick()
    await advance(ports, 20)
    await habits.on_dc_status(dc(ports, entry=36626, name="Festergut"))
    await habits.tick()
    assert len(ports.said) == 1
    reopened = BetweenPulls(ports, random.Random(), gap=CallGap(ports.clock))
    await reopened.on_start(run)
    await reopened.on_dc_status(dc(ports))
    await reopened.tick()
    assert len(ports.said) == 1


@pytest.mark.parametrize("change", [{"tank_guid": 999}, {"enabled": False}, {"paused": True},
                                     {"ts": 0}, {"payload": "BOSS\t1"}])
@_run_async
async def test_foreign_or_disabled_status_does_not_queue_drink(setup, change):
    ports, run, habits = await start(setup)
    await habits.on_dc_status(dc(ports, **change))
    await habits.tick()
    assert not ports.said


@_run_async
async def test_human_led_raid_keeps_member_habits_and_tank_reminder_without_impersonation(setup):
    ports, run, habits = setup
    plan = replace(run.plan, lead_guid=1)
    run.session.plan = run.session.ctx.plan = plan
    with ports.conn:
        from dataclasses import asdict
        _put(ports.conn, f"guild:raid:{run.session.raid_id}:plan", asdict(plan))
    run = RaidRun(plan, run.session, True, True)
    await habits.on_start(run)
    await drain(habits)
    await habits.on_dc_status(dc(ports))
    await habits.tick()
    assert ports.proposed and ports.said[-1][0] == 101
    assert all(c.guid != 1 for c in ports.sent if isinstance(c, StockItems))
    assert all(1 not in c.guids for c in ports.sent if isinstance(c, BotDo))


@_run_async
async def test_old_stock_receipt_cannot_pollute_a_new_run_started_during_await(setup):
    ports, run, habits = setup
    original = ports.send
    old_raid = run.session.raid_id

    async def replace_run(command):
        ack = await original(command)
        with ports.conn:
            ports.conn.execute("UPDATE guild_raid SET ended=?", (ports.clock.now(),))
        await habits.on_end("done")
        session = await RaidSession.open(ports, GuildConfig(), FakeArmory({}, {}), run.plan)
        session.bind_instance(78)
        await habits.on_start(RaidRun(session.plan, session, True, False))
        return ack

    ports.send = replace_run
    await habits.on_start(run)
    await habits.tick()
    assert habits._raid != old_raid
    assert _get(ports.conn, f"guild:habits:{old_raid}:stock:101")["state"] == "done"
    assert habits._get("stock:101") is None and len(ports.sent) == 1


@_run_async
async def test_scoped_end_and_immediate_invalidation_do_not_retire_a_different_run(setup):
    ports, run, habits = await start(setup)
    other = replace(run)
    habits.invalidate(other)
    await habits.on_end("old", run=other)
    assert habits._run is run
    habits.invalidate(run)
    assert habits._run is None
    before = len(ports.sent)
    await habits.tick()
    assert len(ports.sent) == before
