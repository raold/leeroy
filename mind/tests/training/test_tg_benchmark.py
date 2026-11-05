import asyncio
from dataclasses import asdict, replace
from datetime import datetime, timezone
import json
import re
from types import SimpleNamespace

import pytest

from leeroy_mind.bridge import Backoff, BridgeClient
from leeroy_mind.guild import protocol as guild_protocol
from leeroy_mind.guild.protocol import Job
from leeroy_mind.memory.writer import WriteResult
from leeroy_mind.protocol import Ack, encode_command, parse_event
from leeroy_mind.testing.fake_bridge import FakeBridge
from leeroy_mind.testing.fake_clock import FakeClock
from leeroy_mind.training import protocol as training_protocol
from leeroy_mind.training.benchmark import (BenchmarkSchedule, Benchmarks, DPS_UNAVAILABLE,
    allowed_bosses, percentiles, pick_guild, rival_candidates)
from leeroy_mind.training.config import TrainingConfig
from leeroy_mind.training.ports import LivePorts
from leeroy_mind.training.protocol import GetRivals, GetTrainingCleanup, Pull, Rival, Rivals, StartBenchmark


def run(coro):
    return asyncio.run(coro)


def roster():
    specs = [(1, 2), (2, 1), (5, 0), (7, 2), (11, 2), (8, 2), (9, 0), (3, 1), (4, 1), (6, 1)]
    return [Rival(1001 + i, f"Synthetic{i}", cls, spec, 20, 264, 1519, 0)
            for i, (cls, spec) in enumerate(specs)]


def wire_row(row):
    return {k.rstrip("_"): v for k, v in asdict(row).items()}


class Ports:
    def __init__(self, conn):
        guild_protocol.register()
        training_protocol.register()
        self.conn = conn
        self.clock = FakeClock(datetime(2026, 11, 4, 9, tzinfo=timezone.utc))
        self.cfg = TrainingConfig(enabled=True)
        self.human_guid = 1
        self.online = False
        self.cast_members = {201: SimpleNamespace(name="SyntheticOfficer")}
        self.rivals = roster()
        self.holders, self.claims, self.released, self.sent, self.memories = {}, [], [], [], []
        self.away_guids = set()
        self.realm = lambda map_, boss, size, heroic: boss == 0
        self.schedule_changes = {}
        self.partial = False
        self.request_hook = None
        self.send_hook = None
        self.claim_hook = None
        self.cleanup_state = "open"
        self.proof_changes = {}
        self.memory_result = WriteResult(1, "inserted")
        self.runner = Benchmarks(self, self.cfg, schedule_provider=self.schedule)

    def schedule(self):
        return BenchmarkSchedule(**(dict(observed_at=self.clock.now(), human_offline_since=self.clock.now() - 7200,
            next_offline_window=self.clock.now() + 150 * 60, cast_guild=10,
            guild_busy=False, lab_busy=False, opening=False, benchmark_busy=False, bodies_present=True)
            | self.schedule_changes))

    def cast(self):
        return dict(self.cast_members)

    def human_online(self):
        return self.online

    def away(self):
        return set(self.away_guids)

    def holder(self, guid):
        return self.holders.get(guid)

    def claim(self, owner, guids, *, until, rank=None):
        granted = {g for g in guids if self.holder(g) in {None, owner}}
        if self.partial and granted:
            granted.remove(max(granted))
        self.holders.update(dict.fromkeys(granted, owner))
        self.claims.append((owner, set(granted), until))
        if self.claim_hook:
            self.claim_hook()
        return granted

    def release(self, owner, guids=None):
        self.released.append((owner, set(guids or ())))
        removed = set()
        for guid in guids or ():
            assert self.holder(guid) == owner, "must not release a preempting owner"
            self.holders.pop(guid)
            removed.add(guid)
        return removed

    def claims_for(self, owner):
        return frozenset(g for g, held in self.holders.items() if held == owner)

    def benchmark_allowed(self, *args):
        return self.realm(*args)

    def standing_lead(self):
        return 201

    def persona(self, guid):
        return SimpleNamespace(play=SimpleNamespace(guild_role="officer")) if guid in self.cast_members else None

    async def remember(self, candidate):
        self.memories.append(candidate)
        return self.memory_result

    def serialized(self, cmd):
        raw = json.loads(encode_command(cmd, int(self.clock.now() * 1000)))
        assert re.fullmatch(r"[A-Za-z0-9_-]{1,128}", raw["id"])
        self.sent.append(raw)
        return raw

    def job(self, *, state="progress", reason="", **changes):
        owned = self.runner._state
        data = dict(v=1, type="job", ts=int(self.clock.now() * 1000), id=owned["command_id"], kind="benchmark",
            state=state, reason=reason, stage="running" if state == "progress" else "teardown",
            guids=owned["members"], skipped=[], kills=0, wipes=0, instance_id=77, guid=0, level=0,
            avg_ilvl=0, training_run="tr_native_7") | changes
        return parse_event(json.dumps(data))

    def cleanup(self, query_id):
        state = self.runner._state
        data = dict(v=1, type="training_cleanup", ts=int(self.clock.now() * 1000), id=query_id,
            receipt_version=1, training_run=state["handle"], job_id=state["command_id"], training_token="7",
            leader_guid=state["leader"], guild_id=10, human_guid=1, map_id=631, instance_id=77,
            group_id="9007199254740993", members=state["members"], state=self.cleanup_state)
        if self.cleanup_state == "unknown":
            data.update(job_id="", training_token="0", leader_guid=0, guild_id=0, human_guid=0,
                map_id=0, instance_id=0, group_id="0", members=[])
        return data | self.proof_changes

    async def request(self, cmd, event_type, *, until, timeout_s):
        self.serialized(cmd)
        if isinstance(cmd, GetRivals):
            row = parse_event(json.dumps(dict(v=1, type="rivals", ts=int(self.clock.now() * 1000),
                id=cmd.id, members=[wire_row(r) for r in self.rivals])))
            await self.runner.on_rivals(row)
        else:
            assert isinstance(cmd, GetTrainingCleanup) and event_type == "training_cleanup"
            row = parse_event(json.dumps(self.cleanup(cmd.id)))
        if self.request_hook:
            await self.request_hook(cmd)
        assert until(row)
        return [row]

    async def send(self, cmd):
        raw = self.serialized(cmd)
        saved = json.loads(self.conn.execute("SELECT value FROM meta WHERE key='tg:benchmark:active'").fetchone()[0])
        assert saved["dispatched"] and saved["command"]["id"] == raw["id"]
        assert set(raw["members"]) | {raw["leader_guid"]} == set(saved["members"])
        assert raw["leader_guid"] not in raw["members"]
        await self.runner.on_job(self.job())  # Actual event may arrive before its ACK.
        if self.send_hook:
            return await self.send_hook(cmd)
        return Ack(int(self.clock.now() * 1000), cmd.id, True, "")


@pytest.fixture
def bench(store):
    return Ports(store.conn)


async def step(ports):
    ports.runner._next_poll = 0
    await ports.runner.tick()
    if ports.runner._task:
        await asyncio.wait_for(asyncio.shield(ports.runner._task), 2)


def record(ports, ident, *, kind="benchmark", boss=0, outcome="killed", instance=77,
           difficulty=0, start=None, end=None, complete=1, scoreable=1):
    state = ports.runner._state
    now_ms = int(ports.clock.now() * 1000)
    start = now_ms - 60000 if start is None else start
    end = now_ms if end is None else end
    members = state["members"] if state and kind == "benchmark" else [201]
    values = dict(id=ident, context=f"631:{boss}:{difficulty}", map=631, instance_id=instance, boss_id=boss,
        difficulty=difficulty, size=10, outcome=outcome, start_ms=start, end_ms=end, members=len(members),
        assisted=0, boss_entry=36612, upd_mean_ms=40, kind=kind, complete=complete, scoreable=scoreable,
        received=ports.clock.now())
    with ports.conn:
        ports.conn.execute("INSERT INTO tg_pull(" + ",".join(values) + ") VALUES(" + ",".join("?" for _ in values) + ")",
            tuple(values.values()))
        for guid in members:
            ports.conn.execute("INSERT INTO tg_member(pull,guid,class,spec_tab,role,ilvl,damage,damage_boss,healing,"
                "overheal,taken,deaths,interrupts,active_ms,detail) VALUES(?,?,8,2,'ranged',264,1000,1000,0,0,0,0,0,60000,'{}')",
                (ident, guid))
    return Pull(now_ms, ident, 631, instance, boss, difficulty, 10, outcome, start, end,
        len(members), 0, 0, False, "benchmark" if kind == "benchmark" else "tracked", 36612, 40)


def meta(ports, key):
    row = ports.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else None


def test_guild_choice_and_candidates_never_mix_factions_or_invent_a_feral_tank():
    rows = roster()
    rows += [replace(rows[0], guid=2001, guild_id=0), replace(rows[1], guid=2002, guild_id=0, team=1)]
    guild, chosen = pick_guild(rows, cast_guild=10, held=lambda guid: guid == 1001)
    assert guild == 20 and 1001 not in {r.guid for r in chosen}
    assert 2001 in {r.guid for r in chosen} and 2002 not in {r.guid for r in chosen}
    assert all(r.team == 0 for r in chosen)
    assert pick_guild(rows, cast_guild=20) == (0, [])
    assert pick_guild(rows, cast_guild=0) == (0, [])
    assert rival_candidates([replace(rows[0], class_=11, spec_tab=1)])[0].dc_tank is False
    assert rival_candidates([replace(rows[0], spec_tab=-1), replace(rows[1], class_=10)]) == []
    assert len(pick_guild(rows + [rows[0]], cast_guild=10)[1]) == 10  # Duplicate GUID refused.


def test_frontier_prefix_requires_exact_complete_context_and_strict_realm_answer(bench):
    bench.realm = lambda *args: None
    for boss in (0, 1, 3):
        record(bench, f"farm{boss}", kind="offline", boss=boss)
    record(bench, "otherdifficulty", kind="offline", boss=2, difficulty=2)
    assert allowed_bosses(bench, "icc10") == (0, 1)
    bench.realm = lambda *args: False
    assert allowed_bosses(bench, "icc10") == ()
    bench.realm = lambda *args: "yes"
    assert allowed_bosses(bench, "icc10") == ()
    bench.realm = lambda map_, boss, size, heroic: True if boss < 3 else False
    assert allowed_bosses(bench, "icc10") == (0, 1, 2)


@pytest.mark.parametrize("change", [{"observed_at": float("nan")}, {"observed_at": 0},
    {"guild_busy": True}, {"lab_busy": True}, {"opening": True}, {"benchmark_busy": True},
    {"bodies_present": None}, {"guild_busy": 0}, {"cast_guild": 0}])
def test_missing_stale_or_unknown_schedule_never_starts(bench, change):
    async def go():
        bench.schedule_changes.update(change)
        await step(bench)
        assert not bench.sent and not bench.claims
        bench.runner.schedule_provider = None
        await step(bench)
        assert not bench.sent
    run(go())


def test_window_margin_weekday_and_human_offline_hour(bench):
    async def go():
        bench.online = True
        await step(bench)
        bench.online = False
        for change in ({"human_offline_since": bench.clock.now() - 3599},
                       {"next_offline_window": bench.clock.now() + 65 * 60}):
            bench.schedule_changes = change
            await step(bench)
        assert not bench.sent
        bench.schedule_changes = {}
        bench.runner.cfg = replace(bench.cfg, benchmark_weekday=1)
        await step(bench)
        assert not bench.sent
        bench.runner.cfg = bench.cfg
        await step(bench)
        starts = [row for row in bench.sent if row["type"] == "start_benchmark"]
        assert len(starts) == 1 and starts[0]["timeout_min"] == 140
        assert bench.runner.next_due > bench.clock.now()
        await bench.runner.close()
    run(go())


@pytest.mark.parametrize("reason", ["frontier", "gear", "no_tank", "roster"])
def test_skips_are_durable_once_a_week(bench, reason):
    async def go():
        if reason == "frontier":
            bench.realm = lambda *args: False
        elif reason == "gear":
            bench.rivals = [replace(r, ilvl=200) for r in bench.rivals]
        elif reason == "no_tank":
            bench.rivals = [replace(r, class_=8, spec_tab=2) for r in bench.rivals]
        else:
            bench.rivals = bench.rivals[:-1]
        await step(bench)
        assert meta(bench, "tg:benchmark:week:2026-W45")["status"] == reason
        before = len(bench.sent)
        await step(bench)
        assert len(bench.sent) == before and not bench.runner.members()
    run(go())


def test_partial_grant_and_after_await_preemption_never_dispatch(bench):
    async def go():
        bench.partial = True
        await step(bench)
        assert not any(row["type"] == "start_benchmark" for row in bench.sent)
        assert not bench.holders and not bench.runner.members()
        assert len(bench.released[0][1]) == 9
    run(go())


def test_schedule_change_during_rivals_response_is_rechecked(bench):
    async def hook(cmd):
        bench.schedule_changes["opening"] = True
    async def go():
        bench.request_hook = hook
        await step(bench)
        assert not bench.claims and not any(r["type"] == "start_benchmark" for r in bench.sent)
    run(go())


def test_claim_preemption_never_releases_the_new_holder(bench):
    async def go():
        bench.claim_hook = lambda: bench.holders.update({1001: "new_owner"})
        await step(bench)
        assert bench.holders == {1001: "new_owner"}
        assert not any(r["type"] == "start_benchmark" for r in bench.sent)
    run(go())


def test_explicit_no_arbiter_is_supported_without_inventing_a_holder(bench):
    async def go():
        bench.schedule_changes["bodies_present"] = False
        bench.claim = lambda owner, guids, *, until: set(guids)
        await step(bench)
        assert bench.runner.members() and not bench.holders
        bench.cleanup_state = "complete"
        await step(bench)
        assert not bench.runner.members() and not bench.released
    run(go())


def test_changed_next_offline_window_after_claim_cannot_overrun_the_margin(bench):
    async def go():
        bench.claim_hook = lambda: bench.schedule_changes.update(next_offline_window=bench.clock.now() + 100 * 60)
        await step(bench)
        assert not any(row["type"] == "start_benchmark" for row in bench.sent)
        assert not bench.holders and not bench.runner.members()
    run(go())


@pytest.mark.parametrize("error, retained", [("busy", False), ("failed", True)])
def test_only_proven_premutation_negative_ack_can_release_without_native_handle(bench, error, retained):
    async def send(command):
        bench.serialized(command)
        assert meta(bench, "tg:benchmark:active")["dispatched"]
        return Ack(int(bench.clock.now() * 1000), command.id, False, error)
    async def go():
        bench.send = send
        await step(bench)
        assert bool(bench.runner.members()) is retained
        assert bool(bench.holders) is retained
    run(go())


def test_successful_run_stores_exact_result_comparison_and_real_memory_receipt(bench):
    async def go():
        for i in range(3):
            record(bench, f"guild{i}", kind="offline", start=int(bench.clock.now() * 1000) - 500000 + i * 100000,
                end=int(bench.clock.now() * 1000) - 440000 + i * 100000)
        await step(bench)
        await step(bench)  # Bind open native scope.
        assert bench.runner._state["binding"]["group_id"] == "9007199254740993"
        await bench.clock.advance(61, step=61)
        pull = record(bench, "owned_pull")
        await bench.runner.on_pull(pull)
        await bench.runner.on_job(bench.job(state="done", reason="frontier"))
        assert bench.runner.members()  # Generic terminal is not cleanup proof.
        bench.cleanup_state = "complete"
        await step(bench)
        assert not bench.runner.members() and not bench.holders
        row = bench.conn.execute("SELECT * FROM tg_benchmark").fetchone()
        assert row["kills"] == 1 and row["wipes"] == 0 and row["outcome"] == "frontier"
        report = meta(bench, "tg:benchmark:result:2026-W45")
        assert report["comparisons"][0]["guild_farm_kill_seconds"] == 60
        assert report["comparisons"][0]["pulls"] == ["owned_pull"]
        assert report["percentiles"] == {} and report["percentile_reason"] == DPS_UNAVAILABLE
        assert report["stopped_at_frontier"]
        await step(bench)
        assert len(bench.memories) == 1 and "frontier" in bench.memories[0].text
        assert meta(bench, "tg:benchmark:memory:2026-W45:201")["accepted"] is True
        await step(bench)
        assert sum(row["type"] == "start_benchmark" for row in bench.sent) == 1
        assert {row["type"] for row in bench.sent} <= {"get_rivals", "start_benchmark", "get_training_cleanup"}
    run(go())


def test_lost_ack_early_terminal_and_restart_keep_ownership_until_exact_complete(bench):
    async def hook(cmd):
        await bench.runner.on_job(bench.job(state="failed", reason="cleanup:return_timeout"))
        raise TimeoutError
    async def go():
        bench.send_hook = hook
        await step(bench)
        members = bench.runner.members()
        assert members and not bench.released
        await bench.runner.close()
        bench.runner = Benchmarks(bench, bench.cfg, schedule_provider=bench.schedule)
        assert bench.runner.members() == members
        bench.cleanup_state = "unknown"
        await step(bench)
        assert bench.runner.members() == members and not bench.released
        bench.cleanup_state = "restoring"
        await step(bench)
        assert bench.runner.members() == members
        bench.cleanup_state = "complete"
        bench.holders[1001] = "realm:newer_run"
        await step(bench)
        assert not bench.runner.members() and bench.holders == {1001: "realm:newer_run"}
    run(go())


@pytest.mark.parametrize("foreign", [{"job_id": "other_job"}, {"instance_id": 78}, {"guild_id": 99},
    {"training_run": "tr_other_7"}])
def test_foreign_cleanup_cannot_settle_even_after_terminal(bench, foreign):
    async def go():
        await step(bench)
        await bench.runner.on_job(bench.job(state="done", reason="cleared"))
        bench.cleanup_state = "complete"
        bench.proof_changes = foreign
        await step(bench)
        assert bench.runner.members() and not bench.released
        bench.proof_changes = {}
        await step(bench)
        assert not bench.runner.members()
    run(go())


def test_changed_native_token_conflicts_and_different_job_is_ignored(bench):
    async def go():
        await step(bench)
        await step(bench)
        before = bench.runner._state["last_job"]
        await bench.runner.on_job(bench.job(id="foreign_job", state="done"))
        assert bench.runner._state["last_job"] == before
        bench.cleanup_state = "complete"
        bench.proof_changes = {"training_token": "8"}
        await step(bench)
        assert bench.runner.members() and bench.runner._state["conflict"] and not bench.released
    run(go())


@pytest.mark.parametrize("changed", [{"training_run": "tr_conflicting_8"}, {"instance_id": 78}])
def test_conflicting_job_during_cleanup_wait_cannot_release_from_older_complete(bench, changed):
    async def go():
        await step(bench)
        await step(bench)
        members = bench.runner.members()
        entered, gate = asyncio.Event(), asyncio.Event()

        async def wait_for_receipt(cmd):
            if isinstance(cmd, GetTrainingCleanup):
                entered.set()
                await gate.wait()

        bench.cleanup_state = "complete"
        bench.request_hook = wait_for_receipt
        bench.runner._next_poll = 0
        await bench.runner.tick()
        await asyncio.wait_for(entered.wait(), 1)
        bench.runner.feed_job(bench.job(**changed))
        assert bench.runner._state["conflict"]
        gate.set()
        await asyncio.wait_for(bench.runner._task, 1)
        assert bench.runner.members() == members and not bench.released
        assert set(bench.holders) == members
        assert bench.conn.execute("SELECT COUNT(*) FROM tg_benchmark").fetchone()[0] == 0
        assert bench.runner._state["proof"]["state"] == "open"
        bench.runner.feed_job(bench.job())  # Older progress cannot undo a conflict.
        bench.request_hook = None
        await step(bench)
        assert bench.runner._state["conflict"] and not bench.released
    run(go())


@pytest.mark.parametrize("interrupted", ["before_result", "after_result"])
def test_durable_complete_resumes_without_native_cache_and_never_releases_new_owner(bench, interrupted):
    async def go():
        await step(bench)
        await step(bench)
        members = bench.runner.members()

        def fail_local_finalization():
            raise RuntimeError("synthetic interrupted finalization")

        if interrupted == "before_result":
            bench.runner._finish = fail_local_finalization
        else:
            bench.runner._release_owned = fail_local_finalization
        bench.cleanup_state = "complete"
        await step(bench)
        assert meta(bench, "tg:benchmark:active")["proof"]["state"] == "complete"
        assert bench.runner.members() == members and not bench.released
        await bench.runner.close()
        bench.holders[1001] = "realm:newer_run"
        bench.runner = Benchmarks(bench, bench.cfg, schedule_provider=bench.schedule)
        assert not bench.runner._corrupt

        async def native_cache_unavailable(*args, **kwargs):
            raise AssertionError("a saved accepted complete proof needs no native query")

        bench.request = native_cache_unavailable
        await step(bench)
        assert not bench.runner.members() and bench.holders == {1001: "realm:newer_run"}
        assert bench.conn.execute("SELECT COUNT(*) FROM tg_benchmark").fetchone()[0] == 1
        assert meta(bench, "tg:benchmark:week:2026-W45")["status"] == "complete"
        assert len(bench.released) == 1 and bench.released[0][1] == members - {1001}
    run(go())


@pytest.mark.parametrize("interrupted", ["after_seal", "after_publish"])
def test_finalization_snapshot_survives_late_receipts_and_restart_without_recounting(bench, interrupted):
    async def go():
        await step(bench)
        await step(bench)
        await bench.clock.advance(61, step=61)
        await bench.runner.on_pull(record(bench, "original_sealed_pull"))
        bench.runner.feed_job(bench.job(state="done", reason="cleared"))

        def stop_finalization(*args):
            raise RuntimeError("synthetic interrupted finalization")

        if interrupted == "after_seal":
            bench.runner._publish_finalization = stop_finalization
        else:
            bench.runner._release_owned = stop_finalization
        bench.cleanup_state = "complete"
        await step(bench)
        saved = meta(bench, "tg:benchmark:active")
        frozen = saved["finalization"]["payload"]
        assert frozen["result"]["kills"] == 1 and frozen["result"]["outcome"] == "cleared"
        assert frozen["comparisons"][0]["pulls"] == ["original_sealed_pull"]
        assert not bench.released
        await bench.clock.advance(30, step=30)
        end = saved["proof"]["ts"]
        await bench.runner.on_pull(record(bench, "late_sealed_pull", outcome="wiped", start=end-30000, end=end))
        bench.runner.feed_job(bench.job(state="done", reason="frontier"))
        assert bench.runner._state["terminal"]["reason"] == "frontier"
        assert "late_sealed_pull" not in bench.runner._state["pulls"]
        assert bench.runner._state["finalization"]["payload"] == frozen
        await bench.runner.close()
        bench.holders[1001] = "realm:new_owner"
        bench.runner = Benchmarks(bench, bench.cfg, schedule_provider=bench.schedule)
        assert not bench.runner._corrupt

        async def native_unavailable(*args, **kwargs):
            raise AssertionError("accepted complete seal needs no new native query")

        bench.request = native_unavailable
        await step(bench)
        report = meta(bench, "tg:benchmark:result:2026-W45")
        assert report == frozen
        row = dict(bench.conn.execute("SELECT * FROM tg_benchmark").fetchone())
        row.pop("id")
        row["members"], row["bosses"] = json.loads(row["members"]), json.loads(row["bosses"])
        assert row == frozen["result"]
        assert bench.conn.execute("SELECT COUNT(*) FROM tg_benchmark").fetchone()[0] == 1
        assert bench.holders == {1001: "realm:new_owner"} and not bench.runner.members()
        await step(bench)
        assert len(bench.memories) == 1
        assert "they killed" in bench.memories[0].text and "frontier" not in bench.memories[0].text
        assert bench.memories[0].t_event == int(frozen["result"]["ts"])
    run(go())


def test_result_metadata_and_outbox_publish_atomically_from_durable_seal(bench):
    async def go():
        await step(bench)
        await step(bench)
        await bench.clock.advance(61, step=61)
        await bench.runner.on_pull(record(bench, "atomic_sealed_pull"))
        bench.cleanup_state = "complete"
        write = bench.runner._write

        def interrupted_write(key, value):
            write(key, value)
            if key.startswith("tg:benchmark:result:"):
                raise RuntimeError("synthetic crash between result and outbox")

        bench.runner._write = interrupted_write
        await step(bench)
        assert meta(bench, "tg:benchmark:active")["finalization"]
        assert bench.conn.execute("SELECT COUNT(*) FROM tg_benchmark").fetchone()[0] == 0
        assert meta(bench, "tg:benchmark:result:2026-W45") is None
        assert meta(bench, "tg:benchmark:memory_receipt:2026-W45") is None
        assert not bench.released
        await bench.runner.close()
        bench.runner = Benchmarks(bench, bench.cfg, schedule_provider=bench.schedule)
        assert not bench.runner._corrupt
        await step(bench)
        assert bench.conn.execute("SELECT COUNT(*) FROM tg_benchmark").fetchone()[0] == 1
        assert meta(bench, "tg:benchmark:result:2026-W45")
        assert meta(bench, "tg:benchmark:memory_receipt:2026-W45")["status"] == "queued"
        assert not bench.runner.members()
    run(go())


def test_conflicting_native_hint_after_sealing_still_blocks_restart_handback(bench):
    async def go():
        await step(bench)
        await step(bench)
        def stop_publication(*args):
            raise RuntimeError("synthetic interrupted publication")
        bench.runner._publish_finalization = stop_publication
        bench.cleanup_state = "complete"
        await step(bench)
        assert meta(bench, "tg:benchmark:active")["finalization"]
        bench.runner.feed_job(bench.job(training_run="tr_conflicting_99"))
        assert bench.runner._state["conflict"]
        members = bench.runner.members()
        await bench.runner.close()
        bench.runner = Benchmarks(bench, bench.cfg, schedule_provider=bench.schedule)
        assert not bench.runner._corrupt and bench.runner._state["conflict"]
        before = len(bench.sent)
        await step(bench)
        assert bench.runner.members() == members and set(bench.holders) == members
        assert not bench.released and len(bench.sent) == before
        assert bench.conn.execute("SELECT COUNT(*) FROM tg_benchmark").fetchone()[0] == 0
    run(go())


@pytest.mark.parametrize("phase", ["job", "open_proof", "finalization"])
def test_false_presend_flag_cannot_erase_native_admission_on_restart(bench, phase):
    async def go():
        await step(bench)
        if phase != "job":
            await step(bench)
        if phase == "finalization":
            def stop_release():
                raise RuntimeError("synthetic interrupted release")
            bench.runner._release_owned = stop_release
            bench.cleanup_state = "complete"
            await step(bench)
            assert meta(bench, "tg:benchmark:active")["finalization"]
        members = bench.runner.members()
        state = meta(bench, "tg:benchmark:active")
        state["dispatched"] = False
        raw = json.dumps(state)
        await bench.runner.close()
        with bench.conn:
            bench.conn.execute("UPDATE meta SET value=? WHERE key='tg:benchmark:active'", (raw,))
        bench.runner = Benchmarks(bench, bench.cfg, schedule_provider=bench.schedule)
        assert bench.runner._corrupt and bench.runner.active and bench.runner.members() == members
        before = len(bench.sent)
        bench.runner.feed_job(bench.job(state="done", reason="cleared"))
        await step(bench)
        assert set(bench.holders) == members and not bench.released and len(bench.sent) == before
        assert bench.conn.execute("SELECT value FROM meta WHERE key='tg:benchmark:active'").fetchone()[0] == raw
    run(go())


@pytest.mark.parametrize("field", ["proof", "result", "pulls", "memory"])
def test_corrupt_finalization_seal_never_authorizes_handback(bench, field):
    async def go():
        await step(bench)
        await step(bench)
        await bench.clock.advance(61, step=61)
        await bench.runner.on_pull(record(bench, "guarded_sealed_pull"))
        def stop_release():
            raise RuntimeError("synthetic interrupted release")
        bench.runner._release_owned = stop_release
        bench.cleanup_state = "complete"
        await step(bench)
        state = meta(bench, "tg:benchmark:active")
        seal = state["finalization"]
        if field == "proof":
            seal["payload"]["receipt"]["proof"]["state"] = "open"
        elif field == "result":
            seal["payload"]["result"]["kills"] += 1
        elif field == "pulls":
            seal["payload"]["admitted_pulls"][0]["instance_id"] = 999
        else:
            seal["memories"][0]["text"] = "unsupported result"
        await bench.runner.close()
        with bench.conn:
            bench.conn.execute("UPDATE meta SET value=? WHERE key='tg:benchmark:active'", (json.dumps(state),))
        bench.runner = Benchmarks(bench, bench.cfg, schedule_provider=bench.schedule)
        assert bench.runner._corrupt and bench.runner.members()
        await step(bench)
        assert not bench.released
    run(go())


@pytest.mark.parametrize("damage", ["json", "nonmapping", "partial", "command_scope", "binding", "proof", "job"])
def test_corrupt_durable_state_keeps_receipt_callbacks_inert_and_preserves_manifest(bench, damage):
    async def go():
        await step(bench)
        await step(bench)
        await bench.clock.advance(61, step=61)
        pull = record(bench, "corrupt_state_pull")
        job = bench.job(state="done", reason="frontier")
        state = meta(bench, "tg:benchmark:active")
        members = set(state["members"])
        if damage == "partial":
            state = {k: state[k] for k in ("version", "command_id", "owner", "members", "leader",
                "dispatched", "bodies_present", "started")}
        elif damage == "command_scope":
            state["command"]["leader_guid"] = 9999
        elif damage == "binding":
            state["binding"]["training_token"] = "8"
        elif damage == "proof":
            state["proof"]["state"] = "complete"
        elif damage == "job":
            state["last_job"] = {}
        raw = "{" if damage == "json" else json.dumps([1] if damage == "nonmapping" else state)
        await bench.runner.close()
        with bench.conn:
            bench.conn.execute("UPDATE meta SET value=? WHERE key='tg:benchmark:active'", (raw,))
        bench.runner = Benchmarks(bench, bench.cfg, schedule_provider=bench.schedule)
        assert bench.runner._corrupt and bench.runner.active
        if damage not in {"json", "nonmapping"}:
            assert bench.runner.members() == members
        before = len(bench.sent)
        bench.runner.feed_job(job)
        bench.runner.feed_rivals(Rivals(int(bench.clock.now() * 1000), "ignored", tuple(bench.rivals)))
        await bench.runner.on_job(job)
        await bench.runner.on_pull(pull)
        await step(bench)
        assert len(bench.sent) == before and not bench.released
        assert bench.conn.execute("SELECT value FROM meta WHERE key='tg:benchmark:active'").fetchone()[0] == raw
    run(go())


def test_shutdown_sync_job_feed_retains_exact_native_receipt_without_dispatch(bench):
    async def go():
        await step(bench)
        job = bench.job(state="failed", reason="human_login")
        await bench.runner.close()
        before = len(bench.sent)
        assert bench.runner.feed_job(job) is None
        saved = meta(bench, "tg:benchmark:active")
        assert saved["terminal"]["reason"] == "human_login"
        assert saved["handle"] == job.training_run and bench.runner.members()
        assert len(bench.sent) == before and not bench.released
    run(go())


def test_results_do_not_adopt_recent_foreign_unforwarded_or_incomplete_pulls(bench):
    async def go():
        await step(bench)
        await step(bench)
        await bench.clock.advance(61, step=61)
        record(bench, "not_forwarded")
        for ident, kwargs in (("foreign_instance", {"instance": 78}), ("outside_frontier", {"boss": 1}),
                              ("incomplete", {"complete": 0}), ("wrong_difficulty", {"difficulty": 2})):
            await bench.runner.on_pull(record(bench, ident, **kwargs))
        await bench.runner.on_pull(record(bench, "owned_wipe", outcome="wiped"))
        await bench.runner.on_job(bench.job(state="failed", reason="wipe_cap"))
        bench.cleanup_state = "complete"
        await step(bench)
        result = meta(bench, "tg:benchmark:result:2026-W45")
        assert result["result"]["kills"] == 0 and result["result"]["wipes"] == 1
        assert result["baselines"] == {"0": []}
        assert result["admitted_pulls"][0]["id"] == "owned_wipe"
        assert result["comparisons"][0]["pulls"] == ["owned_wipe"]
        assert percentiles(bench.conn, 201) == {}
        bench.memory_result = WriteResult(None, "rejected")
        for _ in range(3):
            await step(bench)
        assert meta(bench, "tg:benchmark:memory:2026-W45:201")["accepted"] is False
    run(go())


def test_ticker_and_callbacks_do_not_block_event_lane_and_close_retains_dispatch(bench):
    async def go():
        entered, gate = asyncio.Event(), asyncio.Event()
        async def hook(cmd):
            entered.set()
            await gate.wait()
            return Ack(0, cmd.id, True, "")
        bench.send_hook = hook
        await bench.runner.tick()
        await asyncio.wait_for(entered.wait(), 1)
        for _ in range(10):
            await bench.runner.tick()
        assert sum(row["type"] == "start_benchmark" for row in bench.sent) == 1
        await asyncio.wait_for(bench.runner.on_job(bench.job(state="failed", reason="human_login")), .1)
        assert bench.runner.members()
        await bench.runner.close()
        assert bench.runner._task.done() and bench.runner.members() and not bench.released
        assert meta(bench, "tg:benchmark:active")["dispatched"]
    run(go())


def test_real_bridge_preserved_ids_mailboxes_and_serial_callbacks(bench):
    async def go():
        workers = set()
        native = {"start": None, "state": "open"}
        live = None
        async def respond(cmd):
            if cmd["type"] == "get_rivals":
                await bridge.push(dict(type="rivals", ts=int(bench.clock.now() * 1000), id=cmd["id"],
                    members=[wire_row(r) for r in bench.rivals]))
            elif cmd["type"] == "start_benchmark":
                native["start"] = cmd
                await bridge.push(dict(type="job", **asdict(bench.job())))
            elif cmd["type"] == "get_training_cleanup":
                bench.cleanup_state = native["state"]
                await bridge.push(bench.cleanup(cmd["id"]))
        def ack(cmd):
            if cmd["type"] in {"get_rivals", "start_benchmark", "get_training_cleanup"}:
                worker = asyncio.create_task(respond(cmd))
                workers.add(worker)
                worker.add_done_callback(workers.discard)
            return True, ""
        bridge = FakeBridge(cast=[], ack=ack)
        port = await bridge.start()
        async def on_event(ev):
            live.feed(ev)
            if isinstance(ev, Job):
                await bench.runner.on_job(ev)
            elif isinstance(ev, Rivals):
                await bench.runner.on_rivals(ev)
        client = BridgeClient("127.0.0.1", port, on_event, clock=bench.clock,
            backoff=Backoff(initial=.01, maximum=.02, jitter=0), ack_timeout=.5)
        async def send(cmd):
            return await client.send(cmd, preserve_id=True)
        bench.send = send
        app = SimpleNamespace(clock=bench.clock, store=SimpleNamespace(conn=bench.conn),
            guild=SimpleNamespace(ports=bench), bodies=bench, raid=SimpleNamespace(lead=lambda: 201),
            realm=SimpleNamespace(race=SimpleNamespace(benchmark_allowed=bench.benchmark_allowed)))
        live = LivePorts(app, cfg=bench.cfg)
        bench.runner = Benchmarks(live, bench.cfg, schedule_provider=bench.schedule)
        connection = asyncio.create_task(client.run())
        try:
            await client.wait_greeted(2)
            await step(bench)
            async def delivered():
                while not bench.runner._state["handle"]:
                    await asyncio.sleep(.005)
            await asyncio.wait_for(delivered(), 1)
            await step(bench)
            assert bench.runner._state["binding"]
            native["state"] = "complete"
            await bridge.push(dict(type="job", **asdict(bench.job(state="done", reason="frontier"))))
            await step(bench)
            assert not bench.runner.members() and not bench.holders
            commands = [r for r in bridge.commands if r["type"] not in {"ping", "get_cast"}]
            assert {r["type"] for r in commands} == {"get_rivals", "start_benchmark", "get_training_cleanup"}
            assert all(re.fullmatch(r"[A-Za-z0-9_-]{1,128}", r["id"]) for r in commands)
            assert len({r["id"] for r in commands}) == len(commands)
        finally:
            await bench.runner.close()
            await live.disconnect()
            await client.close()
            await asyncio.wait_for(connection, 2)
            if workers:
                await asyncio.gather(*workers)
            await bridge.stop()
    run(go())
