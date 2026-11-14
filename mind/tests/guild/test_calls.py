import asyncio
from dataclasses import replace
from functools import wraps

import pytest

from leeroy_mind.guild.boss_calls import BOSS_CALLS, DcState, call_for, parse_dc_status
from leeroy_mind.guild.calls import CallGap, RaidCalls
from leeroy_mind.guild.config import GuildConfig
from leeroy_mind.guild.loot import _get, _put
from leeroy_mind.guild.protocol import BotDo, JobSkip
from leeroy_mind.guild.raid_night import RaidRun
from leeroy_mind.guild.session import RaidPlan, RaidSession
from leeroy_mind.protocol import Boss, DcStatus, Death, Resurrect
from .fakes import FakeArmory, job, member, persona


def _run_async(test):
    @wraps(test)
    def run(*args, **kwargs):
        return asyncio.run(test(*args, **kwargs))
    return run


@pytest.fixture
def setup(fake):
    specs = {101: (1, 2), 102: (11, 2), 104: (7, 1), 107: (1, 2),
             108: (8, 1), 109: (8, 1), 110: (8, 1), 111: (8, 1), 120: (9, 1)}
    for guid, (cls, spec) in specs.items():
        fake.members[guid] = member(guid, f"Member{guid}", cls, spec, map=631)
        fake.personas[guid] = persona(guid, f"Member{guid}")
    plan = RaidPlan("icc", 10, False, False, 101, 107, 120, list(specs), [], True)
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
    calls = RaidCalls(fake, gap=CallGap(fake.clock, min_gap_s=0))
    return fake, run, calls


def stamp(ports):
    return int(ports.clock.now() * 1000)


async def advance(ports, seconds):
    await ports.clock.advance(seconds, step=max(seconds, 1))


def boss(ports, run, state="engaged", boss_id=0, **kwargs):
    fields = dict(ts=stamp(ports), map=631, instance_id=77, boss_id=boss_id, boss_name="",
                  state=state, members=tuple(run.plan.members))
    fields.update(kwargs)
    return Boss(**fields)


def dc(ports, name="Festergut", entry=36626, **kwargs):
    fields = dict(ts=stamp(ports), tank_guid=107, enabled=True, paused=False,
                  payload=f"STATUS\t1\t{entry}\t{name}\t\t0\tresting\tdrinking\t2\t1\t0")
    fields.update(kwargs)
    return DcStatus(**fields)


async def start(setup):
    ports, run, calls = setup
    await calls.on_start(run)
    ports.sent.clear()
    ports.said.clear()
    return ports, run, calls


def test_exact_boss_plans_and_status_parser():
    payload = "STATUS\t1\t36612\tLord Marrowgar\t\t0\tresting\tdrinking\t2\t1\t0"
    assert parse_dc_status(payload) == DcState("resting", 36612, "Lord Marrowgar")
    assert call_for(631, "The Lich King")[1].pre.startswith("lich king:")
    assert call_for(631, "blood prince council")[0] == "the Blood Prince Council"
    assert "Onyxia next" in call_for(249, "Onyxia")[1].pre
    assert len(BOSS_CALLS) == 18 and all(len(c.pre) <= 60 for c in BOSS_CALLS.values())
    assert {key for key, call in BOSS_CALLS.items() if not call.lust} == {(631, 2), (631, 10)}


@pytest.mark.parametrize("payload", [None, "BOSS\t1", "STATUS\t1", "STATUS\t1\t-1\tx\t\t0\tidle",
                                      "STATUS\t1\t1.2\tx\t\t0\tidle", "STATUS\t1\t4294967296\tx\t\t0\tidle",
                                      "STATUS\t1\t1\tx\nunsafe\t\t0\tidle", "x" * 4097])
def test_bad_status_is_ignored(payload):
    assert parse_dc_status(payload) is None


def test_call_gap_handles_boundaries_and_clock_rollback(clock):
    gap = CallGap(clock)
    assert gap.ok(100)
    gap.mark(100)
    assert not gap.ok(99) and not gap.ok(119.9) and gap.ok(120)
    gap.mark(90)
    assert not gap.ok(110)
    with pytest.raises(ValueError):
        CallGap(clock, min_gap_s=float("nan"))


@_run_async
async def test_marks_and_soulstone_require_jobs_and_address_main_tank(setup):
    ports, run, calls = setup
    await calls.on_start(run)
    assert [(c.action, c.param) for c in ports.sent] == [("co", "+mark rti"), ("rti", "skull"), ("nc", "+ss tank")]
    assert ports.sent[0].guids == (107,)
    assert set(ports.sent[1].guids) == {104, 108, 109, 110, 111, 120}
    assert ports.sent[2].guids == (120,)
    assert [s[2] for s in ports.said] == ["kill skull first, then x", "ss on Member107"]
    await calls.on_start(run)
    assert len(ports.sent) == 3


def owner_led(ports, lead):
    """A raid he drives (lead_is_human): lead 1 is his own lead, any other the member he named."""
    plan = RaidPlan("icc", 10, False, False, lead, 107, 120,
                    [101, 102, 104, 107, 108, 109, 110, 111, 120], [], True)
    ports.conn.execute("UPDATE guild_raid SET ended=0 WHERE ended IS NULL")
    session = asyncio.run(RaidSession.open(ports, GuildConfig(), FakeArmory({}, {}), plan))
    session.bind_instance(77)
    return RaidRun(session.plan, session, True, True)


def test_owner_led_run_suppresses_every_hook(setup):
    ports, _, calls = setup
    run = owner_led(ports, 1)
    asyncio.run(_owner_led_silent(ports, run, calls))


async def _owner_led_silent(ports, run, calls):
    await calls.on_start(run)
    await calls.on_ready(run)
    await calls.on_dc_status(dc(ports))
    await calls.on_boss(boss(ports, run))
    await calls.on_death(Death(stamp(ports), 102, 631, 0))
    await advance(ports, 5)
    await calls.tick()
    assert ports.sent == ports.said == []


def test_a_raid_he_drives_is_called_by_the_member_he_named(setup):
    """raidlead: his own group's raid keeps him driving it (no countdown, no dungeon-clear), and
    the member he named calls the marks, the soulstone, the lust and the battle rez."""
    ports, _, calls = setup
    run = owner_led(ports, 101)
    asyncio.run(_named_lead_calls(ports, run, calls))


async def _named_lead_calls(ports, run, calls):
    await calls.on_start(run)
    assert [(c.action, c.param) for c in ports.sent] == [("co", "+mark rti"), ("rti", "skull"), ("nc", "+ss tank")]
    assert {s[0] for s in ports.said} == {101}
    ports.sent.clear()
    ports.said.clear()
    await calls.on_boss(boss(ports, run))
    assert [(c.action, c.param) for c in ports.sent] == [("cast", "bloodlust")]
    assert ports.said[-1][:3] == (101, "raid", "lust now")


@_run_async
async def test_countdown_uses_tick_and_never_sleeps_in_ready(setup, monkeypatch):
    ports, run, calls = await start(setup)
    async def forbidden_sleep(seconds):
        raise AssertionError("ready must not wait on a timer")
    monkeypatch.setattr(ports.clock, "sleep", forbidden_sleep)
    await calls.on_ready(run)
    assert [s[2] for s in ports.said] == ["pulling in 5"]
    await advance(ports, 4)
    await calls.tick()
    assert len(ports.said) == 1
    await advance(ports, 1)
    await calls.tick()
    await calls.tick()
    await calls.on_ready(run)
    assert [s[2] for s in ports.said] == ["pulling in 5", "go"]


@pytest.mark.parametrize("stop", ["end", "logout", "closed", "late", "engaged"])
@_run_async
async def test_countdown_drops_when_stale_or_raid_has_ended(setup, stop):
    ports, run, calls = await start(setup)
    await calls.on_ready(run)
    if stop == "end":
        await calls.on_end("called")
    elif stop == "logout":
        ports.online = False
    elif stop == "closed":
        ports.conn.execute("UPDATE guild_raid SET ended=?", (ports.clock.now(),))
    elif stop == "engaged":
        await calls.on_boss(boss(ports, run))
    await advance(ports, 11 if stop == "late" else 5)
    await calls.tick()
    assert "go" not in [s[2] for s in ports.said]


@_run_async
async def test_pre_calls_are_scoped_deduplicated_and_dropped_by_shared_gap(setup):
    ports, run, calls = await start(setup)
    calls.gap = CallGap(ports.clock)
    await calls.on_dc_status(dc(ports, tank_guid=101))
    assert not ports.said
    await calls.on_dc_status(dc(ports))
    await calls.on_dc_status(dc(ports))
    await advance(ports, 5)
    await calls.on_dc_status(dc(ports, "Rotface", 36627))
    await advance(ports, 20)
    await calls.on_dc_status(dc(ports, "Rotface", 36627))
    assert [s[2] for s in ports.said] == [BOSS_CALLS[631, 4].pre]
    await calls.on_dc_status(dc(ports, "Sindragosa", 36853))
    assert ports.said[-1][2] == BOSS_CALLS[631, 11].pre


@_run_async
async def test_lust_once_per_pull_and_again_only_after_fresh_wipe(setup):
    ports, run, calls = await start(setup)
    engaged = boss(ports, run)
    await calls.on_boss(engaged)
    await calls.on_boss(engaged)
    assert len(ports.sent) == 1 and ports.sent[0].param == "bloodlust" and ports.sent[0].guids == (104,)
    await advance(ports, 1)
    wiped = boss(ports, run, "wiped")
    await calls.on_boss(wiped)
    await calls.on_boss(engaged)
    assert len(ports.sent) == 1
    await advance(ports, 1)
    await calls.on_boss(boss(ports, run))
    assert len(ports.sent) == 2 and [s[2] for s in ports.said] == ["lust now", "lust now"]


@pytest.mark.parametrize("boss_id", [2, 10, 13, 14])
@_run_async
async def test_non_lust_bosses_and_non_fights_never_cast(setup, boss_id):
    ports, run, calls = await start(setup)
    await calls.on_boss(boss(ports, run, boss_id=boss_id))
    assert ports.sent == ports.said == []


@_run_async
async def test_alliance_shaman_uses_heroism(setup):
    ports, run, calls = await start(setup)
    ports.members[104] = replace(ports.members[104], race=11)
    await calls.on_boss(boss(ports, run))
    assert ports.sent[0].param == "heroism"


@pytest.mark.parametrize("change", ["foreign_map", "foreign_instance", "no_tank", "outsider", "stale", "future", "zero_ts", "unbound"])
@_run_async
async def test_untrusted_engage_cannot_issue_actions(setup, change):
    ports, run, calls = await start(setup)
    changes = {"foreign_map": {"map": 649}, "foreign_instance": {"instance_id": 78},
               "no_tank": {"members": (104,)}, "outsider": {"members": (107, 104, 999)},
               "stale": {"ts": stamp(ports) - 61000}, "future": {"ts": stamp(ports) + 6000}, "zero_ts": {"ts": 0}}
    if change == "unbound":
        run.session.ctx.instance_id = None
    await calls.on_boss(boss(ports, run, **changes.get(change, {})))
    assert ports.sent == ports.said == []


@_run_async
async def test_dead_only_druid_cannot_resurrect_themself(setup):
    ports, run, calls = await start(setup)
    await calls.on_boss(boss(ports, run))
    await calls.on_death(Death(stamp(ports), 102, 631, 0))
    assert not any(c.param == "rebirth" for c in ports.sent)


@_run_async
async def test_brez_once_per_fight_ignores_gap_and_preserves_deaths_after_wipe(setup):
    ports, run, calls = setup
    ports.members[111] = replace(ports.members[111], class_=11, spec_tab=0)
    await start(setup)
    calls.gap = CallGap(ports.clock)
    calls.gap.mark(ports.clock.now())
    await calls.on_boss(boss(ports, run))
    await calls.on_death(Death(stamp(ports), 102, 631, 0))
    await calls.on_death(Death(stamp(ports), 107, 631, 0))
    res = [c for c in ports.sent if c.param == "rebirth"]
    assert len(res) == 1 and res[0].guids == (111,) and res[0].target_guid == 102
    assert [s[2] for s in ports.said] == ["lust now", "brez on Member102"]
    await advance(ports, 1)
    await calls.on_boss(boss(ports, run, "wiped"))
    assert set(calls._dead) == {102, 107}
    await calls.on_resurrect(Resurrect(stamp(ports) - 2000, 102))
    assert 102 in calls._dead
    await calls.on_resurrect(Resurrect(stamp(ports), 102))
    assert 102 not in calls._dead


@pytest.mark.parametrize("failure", ["ack", "none", "wrong_id", "wrong_kind", "progress", "skipped", "duplicate", "stale", "exception"])
@_run_async
async def test_queued_failed_or_malformed_jobs_never_announce_or_retry(setup, failure):
    ports, run, calls = await start(setup)
    if failure == "ack":
        ports.acks["bot_do"].append((False, "failed"))
    elif failure == "none":
        ports.jobs["bot_do"].append(None)
    elif failure == "exception":
        async def broken(cmd_id, timeout_s):
            raise RuntimeError("synthetic transport failure")
        ports.wait_job = broken
    else:
        changes = {"wrong_id": {"id": "different"}, "wrong_kind": {"kind": "summon"},
                   "progress": {"state": "progress"}, "skipped": {"skipped": (JobSkip(104, "dead"),)},
                   "duplicate": {"guids": (104, 104)}, "stale": {"ts": stamp(ports) - 6000}}
        ports.jobs["bot_do"].append(replace(job("bot_do", "done", guids=(104,), ts=stamp(ports)), **changes[failure]))
    event = boss(ports, run)
    await calls.on_boss(event)
    await calls.on_boss(event)
    # A newly constructed listener respects durable uncertain-send receipts too.
    other = RaidCalls(ports, gap=CallGap(ports.clock))
    await other.on_start(run)
    await other.on_boss(event)
    assert len(ports.sent) == 1 and ports.said == []


@pytest.mark.parametrize("mutation", ["end", "logout", "member_map", "instance", "closed", "death"])
@_run_async
async def test_post_await_changes_suppress_success_announcements(setup, mutation):
    ports, run, calls = await start(setup)
    async def changed(cmd_id, timeout_s):
        if mutation == "end":
            await calls.on_end("called")
        elif mutation == "logout":
            ports.online = False
        elif mutation == "member_map":
            ports.members[104] = replace(ports.members[104], map=1)
        elif mutation == "instance":
            run.session.ctx.instance_id = 78
            with ports.conn:
                _put(ports.conn, f"guild:raid:{run.session.raid_id}:instance", 78)
        elif mutation == "closed":
            ports.conn.execute("UPDATE guild_raid SET ended=?", (ports.clock.now(),))
        else:
            calls._dead[104] = stamp(ports)
        return job("bot_do", "done", id=cmd_id, guids=(104,), ts=stamp(ports))
    ports.wait_job = changed
    await calls.on_boss(boss(ports, run))
    assert len(ports.sent) == 1 and ports.said == []


@_run_async
async def test_cancellation_propagates_and_keeps_attempt_receipt(setup):
    ports, run, calls = await start(setup)
    entered = asyncio.Event()
    async def blocked(cmd_id, timeout_s):
        entered.set()
        await asyncio.Event().wait()
    ports.wait_job = blocked
    event = boss(ports, run)
    task = asyncio.create_task(calls.on_boss(event))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await calls.on_boss(event)
    assert len(ports.sent) == 1 and ports.said == []


@_run_async
async def test_timeout_is_bounded_and_never_retries_queued_action(setup, monkeypatch):
    ports, run, calls = await start(setup)
    monkeypatch.setattr("leeroy_mind.guild.calls.ACTION_TIMEOUT_S", 0.01)
    async def blocked(cmd_id, timeout_s):
        await asyncio.Event().wait()
    ports.wait_job = blocked
    event = boss(ports, run)
    await asyncio.wait_for(calls.on_boss(event), 1)
    await calls.on_boss(event)
    assert len(ports.sent) == 1 and ports.said == []


@_run_async
async def test_killed_boss_cannot_be_replayed_as_a_new_pull(setup):
    ports, run, calls = await start(setup)
    await calls.on_boss(boss(ports, run))
    await advance(ports, 1)
    await calls.on_boss(boss(ports, run, "killed"))
    await advance(ports, 1)
    await calls.on_boss(boss(ports, run))
    assert len(ports.sent) == 1
    await calls.on_boss(boss(ports, run, boss_id=1))
    assert len(ports.sent) == 2


@_run_async
async def test_dead_shaman_stays_excluded_after_wipe_until_resurrected(setup):
    ports, run, calls = await start(setup)
    await calls.on_death(Death(stamp(ports), 104, 631, 0))
    await calls.on_boss(boss(ports, run))
    assert not ports.sent
    await advance(ports, 1)
    await calls.on_boss(boss(ports, run, "wiped"))
    await advance(ports, 1)
    await calls.on_boss(boss(ports, run))
    assert not ports.sent
    await advance(ports, 1)
    await calls.on_boss(boss(ports, run, "wiped"))
    await calls.on_resurrect(Resurrect(stamp(ports), 104))
    await advance(ports, 1)
    await calls.on_boss(boss(ports, run))
    assert len(ports.sent) == 1


@_run_async
async def test_missing_instance_or_current_tank_prevents_start_actions(setup):
    ports, run, calls = setup
    ports.members[107] = replace(ports.members[107], map=1)
    await calls.on_start(run)
    assert ports.sent == ports.said == []


@_run_async
async def test_marks_are_not_announced_when_one_damage_member_was_skipped(setup):
    ports, run, calls = setup
    ports.jobs["bot_do"].append(job("bot_do", "done", guids=(107,), ts=stamp(ports)))
    ports.jobs["bot_do"].append(job("bot_do", "done", guids=(104, 108, 109, 110, 120),
                                   skipped=(JobSkip(111, "offline"),), ts=stamp(ports)))
    await calls.on_start(run)
    assert "kill skull first, then x" not in [s[2] for s in ports.said]
    assert "ss on Member107" in [s[2] for s in ports.said]


@pytest.mark.parametrize("death_seen", [False, True])
@_run_async
async def test_resurrection_watermark_rejects_delayed_death_after_listener_recovery(setup, death_seen):
    ports, run, calls = await start(setup)
    await calls.on_boss(boss(ports, run))
    dead = Death(stamp(ports), 104, 631, 0)
    if death_seen:
        await calls.on_death(dead)
    await advance(ports, 1)
    await calls.on_resurrect(Resurrect(stamp(ports), 104))
    await calls.on_boss(boss(ports, run, "wiped"))
    await calls.on_death(dead)
    assert 104 not in calls._dead
    ports.sent.clear()
    recovered = RaidCalls(ports, gap=CallGap(ports.clock))
    await recovered.on_start(run)
    await recovered.on_death(dead)
    await advance(ports, 1)
    await recovered.on_boss(boss(ports, run))
    assert len(ports.sent) == 1 and ports.sent[0].param == "bloodlust" and ports.sent[0].guids == (104,)
    # A genuinely newer death still excludes the same caster.
    await recovered.on_death(Death(stamp(ports), 104, 631, 0))
    assert 104 in recovered._dead


@_run_async
async def test_older_saved_state_without_resurrection_watermark_still_loads_conservatively(setup):
    ports, run, calls = await start(setup)
    await calls.on_death(Death(stamp(ports), 104, 631, 0))
    key = f"guild:calls:{run.session.raid_id}:state"
    saved = _get(ports.conn, key)
    saved.pop("resurrected")
    with ports.conn:
        _put(ports.conn, key, saved)
    recovered = RaidCalls(ports, gap=CallGap(ports.clock))
    await recovered.on_start(run)
    assert 104 in recovered._dead and recovered._resurrected == {}
    await advance(ports, 1)
    await recovered.on_resurrect(Resurrect(stamp(ports), 104))
    await recovered.on_boss(boss(ports, run))
    assert len(ports.sent) == 1 and ports.sent[0].param == "bloodlust"


@_run_async
async def test_training_lust_provider_uses_one_shared_pull_receipt(setup):
    ports, run, calls = await start(setup)
    calls.lust_held = lambda map_id, boss_id: True
    await calls.on_boss(boss(ports, run))
    assert not ports.sent
    assert await calls.lust(run)
    assert not await calls.lust(run)
    assert len(ports.sent) == 1 and ports.sent[0].param == "bloodlust"
    await calls.on_boss(boss(ports, run, "wiped"))
    assert not await calls.lust(run)
    assert len(ports.sent) == 1


@_run_async
async def test_training_pre_call_resolves_encounter_index_from_validated_boss_name(setup):
    ports, run, calls = await start(setup)
    seen = []
    def pre_call(map_id, boss_id):
        seen.append((map_id, boss_id))
        return "use the tested formation"
    calls.pre_call = pre_call
    ev = DcStatus(stamp(ports), run.plan.tank_guid, True, False,
                  "STATUS\t0\t36612\tLord Marrowgar\t0\t0\tresting")
    await calls.on_dc_status(ev)
    assert seen == [(631, 0)]
    assert ports.said[-1][2] == "use the tested formation"


@_run_async
async def test_pre_calls_ask_the_raid_playbook_hook_first(setup, monkeypatch):
    """raidlead.callouts.boss_brief: None keeps the standing call; a playbook's brief replaces it."""
    from leeroy_mind.raidlead import callouts
    ports, run, calls = await start(setup)
    await calls.on_dc_status(dc(ports))
    assert ports.said[-1][2] == "festergut: ranged spread, stack on gas spores"
    seen = []
    monkeypatch.setattr(callouts, "boss_brief", lambda context: seen.append(context) or "spores: stack")
    await calls.on_dc_status(dc(ports, "Rotface", 36627))
    assert ports.said[-1][2] == "spores: stack"
    assert (seen[0].map_id, seen[0].boss_id, seen[0].size, seen[0].heroic) == (631, 5, 10, False)
