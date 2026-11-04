import asyncio
from collections import deque
import copy
from dataclasses import asdict, replace
from datetime import datetime
import json
import sqlite3
import threading
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from conftest import make_persona, mem
from leeroy_mind.guild.config import GuildConfig
from leeroy_mind.guild import protocol as guild_protocol
from leeroy_mind.guild.raid_night import RaidRun, State
from leeroy_mind.guild.session import RaidPlan
from leeroy_mind.llm.client import ChatResult, LLMError
from leeroy_mind.llm.presets import LIVE
from leeroy_mind.llm.scheduler import LLMScheduler, Priority
from leeroy_mind.memory.writer import MemoryCandidate, WriteResult
from leeroy_mind.personas.io import PersonaCache, save_persona
from leeroy_mind.protocol import Ack, Boss
from leeroy_mind.realm.ports import (LivePorts, RealmDisabled, RealmEvidenceUnavailable,
                                      TITLE_WORDS, title_holders)
from leeroy_mind.testing.fake_clock import FakeClock
from leeroy_mind.training import protocol as training_protocol
from leeroy_mind.training.benchmark import BenchmarkSchedule, Benchmarks
from leeroy_mind.training.config import TrainingConfig
from leeroy_mind.training.ledger import Ledger


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def app(store, clock):
    return NS(store=store, clock=clock, human_guid=1,
        cfg=NS(human=NS(nickname="", real_name="", pronouns="")), personas=PersonaCache(store),
        world=NS(human_online=True, human_zone=12, in_group=lambda guid: guid == 1),
        raid=NS(phase=lambda now: "trash"), guild=None, training=None, voice=None,
        bridge=None, engine=None, writer=None)


def guild(app):
    app.guild = NS(cfg=GuildConfig(), raid_night=NS(state=State.RUNNING, run=None, session=None, _closing=deque()),
                   offline=NS(_owned=None, run=None, session=None))
    return app.guild


def raid(app, *, instance=41, raid_id=1, offline=False, heroic=False, size=10):
    plan = RaidPlan("icc", size, heroic, offline, 11, 11, 12, [11, 12], [], not offline)
    with app.store.conn:
        app.store.conn.execute("INSERT INTO guild_raid(raid_id,started,raid_key,size,heroic,offline,lead_guid,tank_guid,loot_guid) "
            "VALUES (?,?,?,?,?,?,?,?,?)", (raid_id, app.clock.now() - 100, "icc", size, int(heroic), int(offline), 11, 11, 12))
    session = NS(raid_id=raid_id, started=app.clock.now() - 100, plan=plan,
                 ctx=NS(instance_id=instance, map_id=631))
    current = RaidRun(plan, session, not offline, False)
    controller = app.guild.offline if offline else app.guild.raid_night
    controller.run, controller.session = current, session
    return current


def event(app, state, *, after=-50, instance=41, boss=0, members=(11,)):
    return Boss(int((app.clock.now() + after) * 1000), 631, instance, boss, "SyntheticBoss", state, members)


def test_cast_and_titles_separate_current_members_from_stored_personas(app):
    for guid, title, role in [(1, "raid lead", "officer"), (11, "Guild BAKER", "raider"),
                              (12, "dkpish", "officer"), (13, "DKP and raid lead", "raider")]:
        save_persona(app.store, make_persona(guid, f"Synthetic{guid}", officer_title=title, guild_role=role),
                     source="seed", ts=1, race="Human", class_name="Warrior")
    app.store.upsert_cast([mem(1), mem(11, rank=0), mem(12), mem(13, level=79), mem(14)], 1)
    ports = LivePorts(app)
    assert [m.guid for m in ports.cast()] == [11, 12]
    assert ports.by_title("chef") == ports.by_title("cook") == [11]
    assert ports.by_title("dkp") == []
    assert ports.officers() == [12]  # Persona role, never native rank or title.
    app.store.replace_cast([mem(12)], 2, keep=(1,))
    app.store.replace_cast([], 3, keep=(1,))
    assert [m.guid for m in ports.cast()] == [12]
    assert ports.has_persona(11)
    holders = title_holders(app.store.conn)
    assert tuple(holders) == TITLE_WORDS
    assert holders["chef"] == ((11, "Synthetic11"),)
    assert holders["dkp"] == ((13, "Synthetic13"),)


def test_optional_rows_are_normalized_and_broken_sql_is_not_absence(app):
    ports = LivePorts(app)
    with app.store.conn:
        app.store.conn.execute("INSERT INTO rumor(root,about,canonical,created,last_told) VALUES ('root',11,'fact',2,3)")
        app.store.conn.executemany("INSERT INTO encounter(night,idx,a,b,status,private) VALUES ('night',?,11,12,?,?)",
                                  [(1, "planned", 0), (2, "done", 1), (3, "done", 0)])
        app.store.conn.execute("INSERT INTO award(season,title,guid,value) VALUES ('season','title',11,2.5)")
    assert ports.open_rumors()[0].created_s == 2.0
    assert ports.encounters("night") == [(11, 12)]
    assert ports.awards("season")[0].value == 2.5
    with app.store.conn:
        app.store.conn.execute("DROP TABLE rumor")
    assert ports.open_rumors() == []
    with app.store.conn:
        app.store.conn.execute("CREATE TABLE rumor(unrelated TEXT)")
    with pytest.raises(sqlite3.OperationalError):
        ports.open_rumors()


def test_night_window_uses_mind_timezone_and_dst(app):
    app.clock = FakeClock(datetime(2026, 11, 1, 5, 30, tzinfo=ZoneInfo("America/New_York")))
    ports = LivePorts(app)
    start, end = ports.night_window(app.clock.now())
    assert end - start == 25 * 3600
    assert datetime.fromtimestamp(start, app.clock.local().tzinfo).hour == 6


@pytest.mark.parametrize("state", list(State))
def test_guild_state_and_hangout_marker_are_distinct(app, state):
    controller = guild(app).raid_night
    controller.state = state
    ports = LivePorts(app)
    assert ports.raid_state() == state.name
    assert ports.raid_holds_all() == (state in {State.WAITING_CALL, State.CALLED, State.FORMING, State.ASSEMBLING, State.RUNNING})
    assert ports.raid_called_or_running() == (state in {State.CALLED, State.FORMING, State.ASSEMBLING, State.RUNNING})
    assert not ports.hangout_tonight()
    from leeroy_mind.clock import night_of
    with app.store.conn:
        app.store.set_meta("guild:raid-night:quiet-night", json.dumps(night_of(app.clock.local())))
    assert ports.hangout_tonight()
    assert ports.offline_window()[0].hour == 13


@pytest.mark.parametrize("phase", ["acquiring", "awaiting_ack", "accepted", "uncertain", "tearing_down"])
def test_offline_reservations_survive_detachment_and_corrupt_acceptance(app, phase):
    controller = guild(app).offline
    controller._owned = dict(id="operation", phase=phase, plan=dict(members=[11, 12]), accepted=None, cancelled=True)
    ports = LivePorts(app)
    assert ports.offline_raid_members() == {11, 12}
    controller._owned["accepted"] = []
    assert ports.offline_raid_members() == set()
    controller._owned["accepted"] = [99]
    assert ports.offline_raid_members() == {11, 12}
    with app.store.conn:
        app.store.set_meta("guild:offline:owned", json.dumps(controller._owned))
    app.guild = None
    assert ports.offline_raid_members() == {11, 12}
    with app.store.conn:
        app.store.set_meta("guild:offline:owned", "unreadable")
    with pytest.raises(RealmEvidenceUnavailable):
        ports.offline_raid_members()


def test_retained_matching_offline_session_only_and_terminal_barrier(app):
    controller = guild(app).offline
    current = raid(app, offline=True)
    controller._owned = dict(phase="accepted", raid_id=1, plan=dict(members=[11, 12]), accepted=[])
    ports = LivePorts(app)
    assert ports.offline_raid_members() == {11, 12}
    controller._owned["raid_id"] = 2
    assert ports.offline_raid_members() == set()
    controller._owned.update(raid_id=1, phase="complete")
    assert ports.offline_raid_members() == set()
    controller._owned["phase"] = "rejected"
    assert ports.offline_raid_members() == set()


def test_open_session_modes_and_human_membership_need_bound_instance(app):
    guild(app)
    current = raid(app, heroic=True, size=25)
    ports = LivePorts(app)
    assert ports.raid_difficulty(41) == 3 and ports.in_his_raid(41)
    assert ports.raid_difficulty(0) == -1 and not ports.in_his_raid(0)
    assert ports.raid_difficulty(True) == -1
    raid(app, raid_id=2, offline=True, instance=41)
    assert ports.raid_difficulty(41) == -1
    app.guild.raid_night.state = State.ENDED
    assert not ports.in_his_raid(41)
    current.session.ctx.instance_id = None
    assert not ports.in_his_raid(41)


def test_attendance_denominator_excludes_active_offline_and_nonhuman_sessions(app):
    guild(app)
    for ident in range(1, 6):
        raid(app, raid_id=ident, offline=ident == 3)
        with app.store.conn:
            if ident != 4:
                app.store.conn.execute("UPDATE guild_raid SET ended=? WHERE raid_id=?", (app.clock.now(), ident))
            if ident != 5:
                app.store.conn.execute("INSERT INTO guild_attendance VALUES (?,1,'raided')", (ident,))
            app.store.conn.execute("INSERT INTO guild_attendance VALUES (?,11,?)", (ident, "raided" if ident != 2 else "benched"))
    ports = LivePorts(app)
    assert ports.attendance(11, app.clock.now() - 200) == 0.5
    assert ports.attendance(12, app.clock.now() - 200) == 0
    assert ports.attendance(11, app.clock.now() + 1) == 0


def test_close_loot_loss_requires_one_charged_matching_debit(app):
    ports = LivePorts(app)
    key = "guild:loot:1:abcdef"
    record = dict(state="charged", item_id=100, received=0,
                  award=dict(guid=12, runner_up=11, close=True, paid=50))
    with app.store.conn:
        app.store.set_meta(key, json.dumps(record))
        app.store.conn.execute("INSERT INTO dkp_ledger(ts,guid,delta,reason,raid_id,item_id,note) VALUES (?,12,-50,'item',1,100,?)",
                               (app.clock.now(), key))
    assert ports.close_loot_losses(11, app.clock.now() - 1) == 1
    record["state"] = "acknowledged"
    with app.store.conn:
        app.store.set_meta(key, json.dumps(record))
    assert ports.close_loot_losses(11, 0) == 0
    record["state"] = "charged"
    with app.store.conn:
        app.store.set_meta(key, json.dumps(record))
        app.store.conn.execute("INSERT INTO dkp_ledger(ts,guid,delta,reason,raid_id,item_id,note) VALUES (?,12,-50,'item',1,100,?)",
                               (app.clock.now(), key))
    assert ports.close_loot_losses(11, 0) == 0


def test_boss_evidence_is_scoped_and_retained_ends_require_exact_saved_event(app):
    guild(app)
    current = raid(app)
    ports = LivePorts(app)
    ports.set_enabled(True)
    ports.observe(event(app, "engaged", after=-50), "boss")
    terminal = event(app, "wiped", after=-20)
    app.guild.raid_night._closing.append((current.session, current, "logout", deque([(current, "boss", terminal)]), False))
    app.guild.raid_night.run = app.guild.raid_night.session = None
    app.guild.raid_night.state = State.ENDED
    ports.observe(replace(terminal), "boss")  # Equal values do not prove admission.
    assert ports.last_fight_s() == 0 and ports.wipes_tonight(631, 0) == 0
    ports.observe(terminal, "boss")
    ports.observe(terminal, "boss")
    assert ports.last_fight_s() == 30 and ports.wipes_tonight(631, 0) == 1
    ports.observe(event(app, "engaged", after=-15), "boss")
    app.guild.raid_night._closing[0][3].clear()
    ports.observe(event(app, "wiped", after=-5), "boss")
    assert ports.wipes_tonight(631, 0) == 1


def test_unmatched_current_end_has_zero_duration_and_disable_drops_starts(app):
    guild(app)
    raid(app)
    ports = LivePorts(app)
    ports.set_enabled(True)
    for bad in [event(app, "engaged", instance=0), event(app, "engaged", instance=99),
                event(app, "engaged", members=(99,))]:
        ports.observe(bad, "boss")
    ports.observe(event(app, "wiped", after=-30), "boss")
    assert ports.last_fight_s() == 0 and ports.wipes_tonight(631, 0) == 1
    ports.observe(event(app, "engaged", after=-20), "boss")
    ports.set_enabled(False)
    ports.set_enabled(True)
    ports.observe(event(app, "killed", after=-10), "boss")
    assert ports.last_fight_s() == 0
    app.human_guid = 2
    assert ports.last_fight_s() == 0 and ports.wipes_tonight(631, 0) == 0


def measured_pull(app, ident="measured", **changes):
    now = int(app.clock.now() * 1000)
    row = dict(id=ident, context="631:0:0", map=631, instance_id=41, boss_id=0, difficulty=0, size=10,
        outcome="killed", start_ms=now - 30000, end_ms=now - 10000, members=1, assisted=0,
        boss_entry=1, upd_mean_ms=100, kind="his", complete=1, scoreable=1, received=app.clock.now(),
        damage_basis="requested_pre_ai", healing_basis="effective_gain_with_pre_absorb_raw",
        spell_basis="best_effort_thread_pair", interrupt_basis="successful_interrupt_spell_cast")
    row.update(changes)
    with app.store.conn:
        app.store.conn.execute("INSERT INTO tg_pull(" + ",".join(row) + ") VALUES (" + ",".join("?" * len(row)) + ")", tuple(row.values()))
        app.store.conn.execute("INSERT INTO tg_member(pull,guid,class,spec_tab,role,ilvl,damage,damage_boss,healing,overheal,taken,deaths,interrupts,active_ms,detail) "
            "VALUES (?,11,1,0,'tank',264,1000,900,200,20,300,0,2,10000,'{}')", (ident,))
    return row


def test_meters_select_one_complete_exact_instance_and_preserve_raw_bases(app):
    measured_pull(app)
    measured_pull(app, "other_instance", instance_id=42, end_ms=int(app.clock.now() * 1000) - 1)
    measured_pull(app, "incomplete", complete=0, end_ms=int(app.clock.now() * 1000) - 2)
    ports = LivePorts(app)
    meters = ports.pull_meters(631, 41, 0)
    assert len(meters) == 1 and meters[0].pull_id == "measured"
    assert meters[0].duration_s == 20 and meters[0].active_ms == 10000 and meters[0].damage == 1000
    assert meters[0].damage_basis == "requested_pre_ai" and not hasattr(meters[0], "dps")
    assert ports.pull_meters(631, 43, 0) == []
    with app.store.conn:
        app.store.conn.execute("UPDATE tg_pull SET members=2 WHERE id='measured'")
    assert ports.pull_meters(631, 41, 0) == []


async def telemetry(ports, *, members, start_ms, end_ms, source="tracked", outcome="killed", specs=None):
    header = training_protocol.Pull(end_ms, f"{start_ms}-77-0", 631, 77, 0, 0, 10, outcome,
        start_ms, end_ms, len(members), 0, 0, False, source, 36612, 40,
        "requested_pre_ai", "effective_gain_with_pre_absorb_raw", "best_effort_thread_pair",
        "successful_interrupt_spell_cast")
    ledger = Ledger(ports)
    if source == "tracked":
        ledger.set_run(ports.run)
    for guid in members:
        member = training_protocol.PullMember(header.ts, header.id, guid, 8, (specs or {}).get(guid, 0),
            False, 264, 1000, 900, 200, 20, 300, 0, 2, 10000, (), (), (), ())
        await ledger.on_event(member)
    await ledger.on_event(header)
    stored = ports.conn.execute("SELECT complete,scoreable FROM tg_pull WHERE id=?", (header.id,)).fetchone()
    assert tuple(stored) == (1, 1)
    return header


def ledger_ports(app):
    guild(app)
    current = raid(app, instance=77)
    current.session.started = app.clock.now() - 10000
    with app.store.conn:
        app.store.conn.execute("UPDATE guild_raid SET started=? WHERE raid_id=1", (current.session.started,))
    return NS(conn=app.store.conn, clock=app.clock, run=current, cast=lambda: {}, persona=lambda guid: None,
              drunk=lambda guid: 0, flask_skips=lambda: set(), raid_at=lambda ms: None)


def test_unknown_spec_from_real_ledger_retains_known_peer_and_raw_bases(app):
    async def scenario():
        producer = ledger_ports(app)
        end = int(app.clock.now() * 1000)
        header = await telemetry(producer, members=[11, 12], start_ms=end - 20000, end_ms=end,
                                 specs={11: -1, 12: 2})
        meters = LivePorts(app).pull_meters(631, 77, 0)
        assert [(m.guid, m.spec_tab) for m in meters] == [(11, -1), (12, 2)]
        assert all(m.pull_id == header.id and m.damage == 1000 and m.active_ms == 10000 for m in meters)
        assert all(m.damage_basis == "requested_pre_ai" and m.healing_basis == "effective_gain_with_pre_absorb_raw"
                   and m.spell_basis == "best_effort_thread_pair" and m.interrupt_basis == "successful_interrupt_spell_cast"
                   for m in meters)
        with app.store.conn:
            app.store.conn.execute("UPDATE tg_member SET spec_tab=-2 WHERE guid=11")
        assert LivePorts(app).pull_meters(631, 77, 0) == []
    run(scenario())


class BenchmarkProducer:
    """In-process native responses; real Benchmarks owns start, seal and publish."""
    def __init__(self, app):
        self.conn, self.clock, self.human_guid = app.store.conn, app.clock, 1
        self.telemetry_ports = ledger_ports(app) if app.guild is None else NS(
            conn=self.conn, clock=self.clock, run=app.guild.raid_night.run, cast=lambda: {},
            persona=lambda guid: None, drunk=lambda guid: 0, flask_skips=lambda: set(), raid_at=lambda ms: None)
        self.cfg = TrainingConfig(enabled=True, benchmark_weekday=self.clock.local().weekday(),
                                  benchmark_window=("00:00", "23:59"), benchmark_raids=("icc10",))
        self.holders, self.cleanup_state, self.skew_ms = {}, "open", 0
        specs = [(1, 2), (2, 1), (5, 0), (7, 2), (11, 2), (8, 2), (9, 0), (3, 1), (4, 1), (6, 1)]
        self.roster = tuple(training_protocol.Rival(1001 + i, f"Synthetic{i}", cls, spec, 20, 264, 0, 0)
                            for i, (cls, spec) in enumerate(specs))
        guild_protocol.register()
        training_protocol.register()
        self.runner = Benchmarks(self, self.cfg, schedule_provider=lambda: BenchmarkSchedule(
            self.clock.now(), self.clock.now() - 7200, self.clock.now() + 9000, 10,
            False, False, False, False, True))

    def cast(self):
        return {}

    def away(self):
        return set()

    def human_online(self):
        return False

    def holder(self, guid):
        return self.holders.get(guid)

    def claim(self, owner, guids, *, until):
        granted = set(guids)
        self.holders.update(dict.fromkeys(granted, owner))
        return granted

    def release(self, owner, guids):
        removed = set()
        for guid in guids:
            assert self.holders.pop(guid) == owner
            removed.add(guid)
        return removed

    def claims_for(self, owner):
        return frozenset(g for g, held in self.holders.items() if held == owner)

    def standing_lead(self):
        return 0

    def benchmark_allowed(self, map_id, boss, size, heroic):
        return boss == 0

    def job(self, state="progress"):
        owned = self.runner._state
        return guild_protocol.Job(int(self.clock.now() * 1000), owned["command_id"], "benchmark", state,
            "", "running" if state == "progress" else "teardown", tuple(owned["members"]), (),
            0, 0, 77, 0, 0, 0, "tr_native_7")

    async def send(self, cmd):
        await self.runner.on_job(self.job())  # Exact native scope before Ack.
        return Ack(int(self.clock.now() * 1000), cmd.id, True, "")

    async def request(self, cmd, event_type, *, until, timeout_s):
        if isinstance(cmd, training_protocol.GetRivals):
            return [training_protocol.Rivals(int(self.clock.now() * 1000), cmd.id, self.roster)]
        assert isinstance(cmd, training_protocol.GetTrainingCleanup)
        owned = self.runner._state
        return [training_protocol.TrainingCleanup(int(self.clock.now() * 1000) + self.skew_ms,
            cmd.id, 1, owned["handle"], owned["command_id"], "7", owned["leader"], 10, 1,
            631, 77, "9007199254740993", tuple(owned["members"]), self.cleanup_state)]

    async def step(self):
        self.runner._next_poll = 0
        await self.runner.tick()
        if self.runner._task is not None:
            await self.runner._task
        assert not self.runner._corrupt


async def published_benchmark(app, *, baseline_outcomes=(), outcomes=("killed", "killed", "wiped"), skew_ms=0):
    producer = BenchmarkProducer(app)
    now_ms = int(app.clock.now() * 1000)
    for i, outcome in enumerate(baseline_outcomes):
        await telemetry(producer.telemetry_ports, members=list(range(11, 21)),
            start_ms=now_ms - 900000 + i * 60000, end_ms=now_ms - 900000 + i * 60000 + (30 + i * 10) * 1000,
            outcome=outcome)
    await producer.step()  # Actual schedule, roster selection, claim and start.
    assert Benchmarks._valid_state(producer.runner._state)
    assert len(producer.runner._state["members"]) == producer.runner._state["size"] == 10
    await producer.step()  # Correlated open native binding.
    for i, outcome in enumerate(outcomes):
        duration = (20 + i * 20) * 1000
        start = int(app.clock.now() * 1000)
        await app.clock.advance(duration / 1000, step=duration / 1000)
        header = await telemetry(producer.telemetry_ports, members=producer.runner._state["members"],
            start_ms=start, end_ms=start + duration, source="benchmark", outcome=outcome)
        await producer.runner.on_pull(header)
    await producer.runner.on_job(producer.job("done"))
    producer.cleanup_state, producer.skew_ms = "complete", skew_ms
    await producer.step()  # Real reconcile -> freeze -> validate -> publish -> handback.
    if skew_ms > 15000:
        assert producer.runner._state is not None and producer.runner._state["proof"]["state"] == "open"
        return producer, None
    assert producer.runner._state is None and not producer.holders
    row = app.store.conn.execute("SELECT week FROM tg_benchmark ORDER BY ts DESC,id DESC LIMIT 1").fetchone()
    payload = json.loads(app.store.get_meta("tg:benchmark:result:" + row[0]))
    return producer, payload


def put_publication(app, payload):
    with app.store.conn:
        app.store.set_meta("tg:benchmark:result:" + payload["result"]["week"], json.dumps(payload))


def test_benchmark_reads_real_publication_and_empty_completion_without_mutable_rejoin(app):
    async def scenario():
        producer, payload = await published_benchmark(app, baseline_outcomes=("killed",) * 3)
        ports = LivePorts(app)
        result = ports.benchmark_results(0)
        assert len(result) == 1 and len(result[0].members) == 10 and len(result[0].pulls) == 3
        assert result[0].comparisons[0].rival_kill_seconds == 30.0
        assert result[0].comparisons[0].guild_farm_kill_seconds == 40.0
        with app.store.conn:
            app.store.conn.execute("UPDATE tg_pull SET instance_id=99,scoreable=0")
        assert ports.benchmark_results(0) == result
        await app.clock.advance(7 * 86400, step=7 * 86400)
        empty_producer, empty = await published_benchmark(app, outcomes=())
        assert empty["admitted_pulls"] == empty["comparisons"] == []
        assert ports.benchmark_results(0)[-1].pulls == ()
    run(scenario())


@pytest.mark.parametrize("damage", ["rival_median", "rival_missing", "guild_median", "baseline_subset", "baseline_order"])
def test_contradictory_frozen_comparisons_are_omitted(app, caplog, damage):
    async def scenario():
        producer, payload = await published_benchmark(app, baseline_outcomes=("killed",) * 3)
        assert len(LivePorts(app).benchmark_results(0)) == 1
        rec = payload["comparisons"][0]
        if damage == "rival_median":
            rec["rival_kill_seconds"] = 1.0
        elif damage == "rival_missing":
            rec["rival_kill_seconds"] = None
        elif damage == "guild_median":
            rec["guild_farm_kill_seconds"] = 1.0
        elif damage == "baseline_subset":
            rec["baseline_pulls"] = rec["baseline_pulls"][:-1]
        else:
            rec["baseline_pulls"].reverse()
        put_publication(app, payload)
        caplog.clear()
        assert LivePorts(app).benchmark_results(0) == []
        assert [r.getMessage() for r in caplog.records] == ["realm_benchmark_evidence_unavailable"]
    run(scenario())


@pytest.mark.parametrize("baseline_outcomes,farm", [
    ((), False), (("killed", "killed", "wiped"), False),
    (("wiped", "wiped", "killed", "killed", "killed"), False),
    (("wiped", "killed", "killed", "killed", "killed"), True)])
def test_frozen_comparisons_keep_producer_farm_threshold_and_none_rules(app, baseline_outcomes, farm):
    async def scenario():
        producer, payload = await published_benchmark(app, baseline_outcomes=baseline_outcomes, outcomes=("wiped",))
        result = LivePorts(app).benchmark_results(0)[0].comparisons[0]
        assert result.rival_kill_seconds is None and result.rival_wipes == 1
        assert bool(result.baseline_pulls) is farm
        assert (result.guild_farm_kill_seconds is not None) is farm
        payload["comparisons"][0]["guild_farm_kill_seconds"] = None if farm else 1.0
        put_publication(app, payload)
        assert LivePorts(app).benchmark_results(0) == []
    run(scenario())


@pytest.mark.parametrize("baselines", [None, [], {"0": None}, {"0": [None]}, {"0": [{}]}])
def test_malformed_baselines_omit_only_bad_summary_with_safe_diagnostic(app, caplog, baselines):
    async def scenario():
        producer, bad = await published_benchmark(app)
        await app.clock.advance(7 * 86400, step=7 * 86400)
        later_producer, good = await published_benchmark(app)
        bad["baselines"] = baselines
        put_publication(app, bad)
        caplog.clear()
        results = LivePorts(app).benchmark_results(0)
        assert [r.week for r in results] == [good["result"]["week"]]
        assert [r.getMessage() for r in caplog.records] == ["realm_benchmark_evidence_unavailable"]
    run(scenario())


def test_real_native_completion_clock_skew_is_bounded_and_keeps_pull_time_proofs(app):
    async def scenario():
        producer, payload = await published_benchmark(app, skew_ms=16000)
        assert payload is None and LivePorts(app).benchmark_results(0) == []
        producer.skew_ms = 1000
        await producer.step()
        row = app.store.conn.execute("SELECT week FROM tg_benchmark").fetchone()
        payload = json.loads(app.store.get_meta("tg:benchmark:result:" + row[0]))
        assert payload["receipt"]["proof"]["ts"] == int(payload["result"]["ts"] * 1000) + 1000
        assert len(LivePorts(app).benchmark_results(0)) == 1
        for damage in ("future_completion", "early_pull", "late_pull"):
            bad = copy.deepcopy(payload)
            if damage == "future_completion":
                bad["receipt"]["proof"]["ts"] = int(payload["result"]["ts"] * 1000) + 15001
                bad["receipt"]["query_receipt"]["ts"] = bad["receipt"]["proof"]["ts"]
            elif damage == "early_pull":
                bad["admitted_pulls"][0]["start_ms"] = int(payload["receipt"]["started"] * 1000) - 1
            else:
                bad["admitted_pulls"][0]["end_ms"] = payload["receipt"]["proof"]["ts"] + 1
            put_publication(app, bad)
            assert LivePorts(app).benchmark_results(0) == []
    run(scenario())


def test_speech_queue_is_bounded_owned_and_old_generation_never_revives(app):
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        calls, predicates = [], []
        async def speak(turn, *, why, still_valid):
            calls.append(turn)
            predicates.append(still_valid)
            entered.set()
            await release.wait()
            return ["line"]
        app.engine = NS(speak=speak)
        ports = LivePorts(app)
        ports.propose(11, "guild", "disabled")
        await ports.speech_tick()
        assert not calls
        ports.set_enabled(True)
        for i in range(70):
            ports.propose(11, "guild", str(i), to_guid=12)
        work = asyncio.create_task(ports.speech_tick())
        await entered.wait()
        assert calls[0].instruction == "6" and calls[0].max_lines == 1 and calls[0].to_guid == 12
        ports.set_enabled(False)
        await work
        ports.set_enabled(True)
        assert not predicates[0]()
        release.set()
        await ports.speech_tick()
        assert len(calls) == 1
        await ports.close()
    run(scenario())


def test_memory_is_async_real_result_and_disable_cancels_pending_owned_work(app):
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        result = WriteResult(7, "inserted")
        async def write(candidate, *, enforce_limits):
            assert enforce_limits is False
            entered.set()
            await release.wait()
            return result
        app.writer = NS(write=write)
        ports = LivePorts(app)
        candidate = MemoryCandidate(11, "event", "Synthetic observation", 1, (11,), 1, 0.0, "game", ())
        with pytest.raises(RealmDisabled):
            await ports.remember(candidate)
        ports.set_enabled(True)
        work = asyncio.create_task(ports.remember(candidate))
        await entered.wait()
        ports.set_enabled(False)
        with pytest.raises(RealmDisabled):
            await work
        ports.set_enabled(True)
        release.set()
        assert await ports.remember(candidate) is result
        await ports.close()
    run(scenario())


def test_notable_embeds_then_writes_under_one_ticket(app):
    async def scenario():
        entered, release, writes, texts = asyncio.Event(), asyncio.Event(), [], []
        mode = {"outage": False, "block": False}
        async def embed(batch, priority):
            assert priority == Priority.EXTRACTION
            texts.append(tuple(batch))
            if mode["block"]:
                entered.set()
                await release.wait()
            if mode["outage"]:
                raise LLMError("offline")
            return [b"v" * 2048]
        app.embedder, app.writer = NS(embed=embed), NS(name="writer")
        def write(writer, vector):
            writes.append((writer, vector))
            return 7
        ports = LivePorts(app)
        with pytest.raises(RealmDisabled):  # Disabled: nothing is embedded or written.
            await ports.remember_notable("Synthetic fact.", write)
        assert texts == [] and writes == []
        ports.set_enabled(True)
        assert await ports.remember_notable("Synthetic fact.", write) == 7
        assert texts == [("Synthetic fact.",)] and writes == [(app.writer, b"v" * 2048)]
        mode["outage"] = True  # An embed that fails still writes, without a vector.
        assert await ports.remember_notable("Synthetic fact.", write) == 7
        assert writes[-1] == (app.writer, None)
        mode.update(outage=False, block=True)
        work = asyncio.create_task(ports.remember_notable("Synthetic fact.", write))
        await entered.wait()
        ports.set_enabled(False)  # Disabled while the vector is awaited: never written.
        release.set()
        with pytest.raises(RealmDisabled):
            await work
        assert len(writes) == 2
        await ports.close()
    run(scenario())


def test_parent_speech_cancellation_propagates(app):
    async def scenario():
        entered = asyncio.Event()
        async def say_as(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        app.engine = NS(say_as=say_as)
        ports = LivePorts(app)
        ports.set_enabled(True)
        work = asyncio.create_task(ports.say(11, "guild", "line"))
        await entered.wait()
        work.cancel()
        with pytest.raises(asyncio.CancelledError):
            await work
        await ports.close()
    run(scenario())


def test_send_enrolls_before_early_job_and_settles_ack_after_disable_and_caller_cancel(app):
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        receipts, enrolled = [], set()
        def on_send(cmd):
            enrolled.add(cmd.id)
            receipts.append("send")
        async def send(cmd, *, preserve_id):
            assert preserve_id and cmd.id in enrolled
            receipts.append("job")  # Serial job callback can precede the Ack.
            entered.set()
            await release.wait()
            return Ack(1, cmd.id, True, "")
        app.bridge = NS(send=send)
        ports = LivePorts(app, on_send=on_send, on_ack=lambda cmd, ack: receipts.append("ack"),
                          on_send_error=lambda cmd, kind: receipts.append(kind))
        ports.set_enabled(True)
        work = asyncio.create_task(ports.send(NS(id="command")))
        await entered.wait()
        ports.set_enabled(False)
        work.cancel()
        with pytest.raises(asyncio.CancelledError):
            await work
        release.set()
        await ports.close()
        assert receipts == ["send", "job", "ack"]
    run(scenario())


def test_transport_queued_before_disable_never_enters_bridge_and_ack_callback_is_not_transport_error(app):
    async def scenario():
        calls = []
        async def send(cmd, *, preserve_id):
            calls.append("bridge")
            return Ack(1, cmd.id, True, "")
        def on_send(cmd):
            calls.append("enrolled")
        def on_ack(cmd, ack):
            raise LookupError("receipt callback failed")
        app.bridge = NS(send=send)
        ports = LivePorts(app, on_send=on_send, on_ack=on_ack,
                          on_send_error=lambda cmd, kind: calls.append(kind))
        ports.set_enabled(True)
        work = asyncio.create_task(ports.send(NS(id="command")))
        await asyncio.sleep(0)  # send scheduled settlement; settlement has not started.
        ports.set_enabled(False)
        with pytest.raises(RealmDisabled):
            await work
        assert calls == []
        ports.set_enabled(True)
        with pytest.raises(LookupError):
            await ports.send(NS(id="another"))
        assert calls == ["enrolled", "bridge"]
        await ports.close()
    run(scenario())


# -- the light minds' live line, the guild's name and the realm's directory ------------------

class FakeLLM:
    def __init__(self, text="gz on the kill"):
        self.text, self.calls, self.error = text, [], None

    async def chat(self, model, messages, *, options, think=None, format=None, keep_alive=None, timeout=None):
        self.calls.append(dict(model=model, messages=messages, options=options, think=think,
                               format=format, timeout=timeout))
        if self.error is not None:
            raise self.error
        return ChatResult(self.text, "", 4, 0.1)


class RecordingScheduler(LLMScheduler):
    def __init__(self, llm, clock):
        super().__init__(llm, clock=clock)
        self.submitted = []

    async def submit(self, priority, job, *, deadline=None):
        self.submitted.append((priority, deadline))
        return await super().submit(priority, job, deadline=deadline)


def live_app(app, *, ready=True, online=True):
    llm = FakeLLM()
    app.scheduler = RecordingScheduler(llm, app.clock)
    app.cfg.models = NS(live="synthetic-live-model")
    app.model_ready = ready
    app.world.human_online = online
    app.engine = NS(live_admission=AsyncMock())
    return llm


MESSAGES = [{"role": "system", "content": "One Trade line."}, {"role": "user", "content": "He pulled early."}]


def test_llm_line_background_think_false_live_model(app):
    llm = live_app(app)
    ports = LivePorts(app)

    async def scenario():
        assert await ports.llm_line(MESSAGES) is None  # Disabled: nothing asked.
        assert llm.calls == [] and app.scheduler.submitted == []
        ports.set_enabled(True)
        now = app.clock.now()
        assert await ports.llm_line(MESSAGES) == "gz on the kill"
        assert app.scheduler.submitted == [(Priority.BACKGROUND, now + 20)]
        call = llm.calls[0]
        assert call["model"] == "synthetic-live-model" and call["think"] is False and call["format"] is None
        assert call["options"] == dict(LIVE.options) and call["options"]["num_ctx"] == 4096
        assert call["timeout"] == LIVE.timeout and call["messages"] == MESSAGES
        app.engine.live_admission.assert_awaited_once()  # The night hands the live model back first.
        assert await ports.llm_line(MESSAGES, deadline_s=5) == "gz on the kill"
        assert app.scheduler.submitted[-1] == (Priority.BACKGROUND, now + 5)
        llm.text = "   "
        assert await ports.llm_line(MESSAGES) is None  # An empty answer is no line.
        llm.error = LLMError("synthetic outage")
        assert await ports.llm_line(MESSAGES) is None
        with pytest.raises(ValueError):
            await ports.llm_line([{"role": "user"}])
        await ports.close()
        await app.scheduler.close()
    run(scenario())


def test_llm_line_none_offline_or_model_away(app):
    llm = live_app(app, online=False)
    ports = LivePorts(app)
    ports.set_enabled(True)

    async def scenario():
        assert await ports.llm_line(MESSAGES) is None  # He is offline.
        app.world.human_online = True
        app.model_ready = False
        assert await ports.llm_line(MESSAGES) is None  # The live model's latch is down.
        del app.model_ready
        assert await ports.llm_line(MESSAGES) is None
        assert await ports.llm_line(MESSAGES, deadline_s=0) is None
        assert llm.calls == [] and app.scheduler.submitted == []
        app.model_ready = True

        async def logout():  # He leaves while the job waits for admission: never asked.
            app.world.human_online = False
        app.engine.live_admission = logout
        assert await ports.llm_line(MESSAGES) is None
        assert len(app.scheduler.submitted) == 1 and llm.calls == []
        app.world.human_online = True
        entered, release = asyncio.Event(), asyncio.Event()

        async def held():
            entered.set()
            await release.wait()
        app.engine.live_admission = held
        work = asyncio.create_task(ports.llm_line(MESSAGES))
        await entered.wait()
        ports.set_enabled(False)  # Disabled while it waits: None, and the model is never asked.
        release.set()
        assert await work is None and llm.calls == []
        await ports.close()
        await app.scheduler.close()
    run(scenario())


class FakeArmory:
    def __init__(self, name="Synthetic Guild"):
        self.name, self.threads, self.error = name, [], None

    def guild_name(self, guild_id):
        assert guild_id == 21
        self.threads.append(threading.get_ident())
        if self.error is not None:
            raise self.error
        return self.name


def test_guild_name_refresh_off_loop(app, monkeypatch, caplog):
    armory = FakeArmory()
    ports = LivePorts(app)
    monkeypatch.setattr(ports, "armory", lambda: armory)

    async def scenario():
        loop_thread = threading.get_ident()
        assert ports.guild_name() == ""
        assert await ports.refresh_guild_name(21) == "" and armory.threads == []  # Disabled.
        ports.set_enabled(True)
        assert await ports.refresh_guild_name(21) == "Synthetic Guild"
        assert armory.threads and loop_thread not in armory.threads  # The query ran off the loop.
        assert ports.guild_name() == "Synthetic Guild"
        cached = json.loads(app.store.get_meta("realm:guild_name"))
        assert cached == {"guild": 21, "name": "Synthetic Guild", "at": app.clock.now()}
        for bad in ("", " Padded", "x" * 25, None, "Tab\tGuild"):
            armory.name = bad
            assert await ports.refresh_guild_name(21) == "Synthetic Guild"  # The cache stays.
        armory.name, armory.error = "Renamed Guild", RuntimeError("private detail")
        assert await ports.refresh_guild_name(21) == "Synthetic Guild"
        assert "realm_guild_name_unavailable" in caplog.text and "private detail" not in caplog.text
        armory.error = None
        assert await ports.refresh_guild_name(21) == "Renamed Guild"
        with pytest.raises(ValueError):
            await ports.refresh_guild_name(0)
        app.store.set_meta("realm:guild_name", "not json")
        assert ports.guild_name() == ""
        monkeypatch.setattr(ports, "armory", lambda: None)
        assert await ports.refresh_guild_name(21) == ""
        await ports.close()
    run(scenario())


def test_guild_name_write_needs_the_ticket_after_the_thread(app, monkeypatch):
    ports = LivePorts(app)
    entered, release = threading.Event(), threading.Event()

    class Slow:
        def guild_name(self, guild_id):
            entered.set()
            release.wait(5)
            return "Synthetic Guild"
    monkeypatch.setattr(ports, "armory", lambda: Slow())

    async def scenario():
        ports.set_enabled(True)
        work = asyncio.create_task(ports.refresh_guild_name(21))
        await asyncio.to_thread(entered.wait, 5)
        ports.set_enabled(False)
        release.set()
        assert await work == ""
        assert app.store.get_meta("realm:guild_name") is None
        await ports.close()
    run(scenario())


def test_realm_dir_is_beside_the_minds_data(app, tmp_path):
    app.data_dir = tmp_path
    assert LivePorts(app).realm_dir() == tmp_path / "realm" and not (tmp_path / "realm").exists()
