import asyncio
import sqlite3
from datetime import datetime, timezone

import pytest

from leeroy_mind.guild.config import DkpConfig
from leeroy_mind.guild.dkp import (
    Award, Bidder, LedgerEntry, apply_due_decay, attendance_entries, award, bid,
    decay_boundaries, decay_entries, kill_entries,
)
from leeroy_mind.guild.store import (
    add_ledger, attendance_counts, balances, bench_streaks, clear_flag, end_raid,
    flags, last_led, last_run, new_raid, offline_raid_between, record_attendance, set_flag,
)
from leeroy_mind.storage.db import migrate


def raid(conn, started, **over):
    fields = dict(started=started, raid_key="icc", size=10, heroic=False, offline=False,
                  lead_guid=1101, tank_guid=1101, loot_guid=1102)
    fields.update(over)
    return new_raid(conn, **fields)


def test_kill_and_attendance_entries():
    assert [(e.guid, e.delta, e.reason) for e in kill_entries([1101, 1102], 7, 100., DkpConfig())] == [
        (1101, 10., "kill"), (1102, 10., "kill")]
    assert [(e.guid, e.delta, e.reason) for e in attendance_entries(
        [1101], [1105], 7, 100., DkpConfig())] == [(1101, 5., "attendance"), (1105, 5., "standby")]
    assert kill_entries([1101, 1101], 7, 100., DkpConfig()) == [
        LedgerEntry(100., 1101, 10., "kill", 7)]
    with pytest.raises(ValueError, match="both"):
        attendance_entries([1101], [1101], 7, 100., DkpConfig())


def test_decay_boundaries():
    assert decay_boundaries(datetime(2026, 10, 26, 4), datetime(2026, 11, 10, 12), 0, 4) == [
        datetime(2026, 11, 2, 4), datetime(2026, 11, 9, 4)]
    assert decay_boundaries(datetime(2026, 11, 2, 4), datetime(2026, 11, 9, 4), 0, 4) == [
        datetime(2026, 11, 9, 4)]
    assert decay_boundaries(None, datetime(2026, 11, 9, 4), 0, 4) == []
    assert decay_boundaries(datetime(2026, 11, 9, 4), datetime(2026, 11, 9, 3, 59), 0, 4) == []
    with pytest.raises(ValueError, match="naive"):
        decay_boundaries(None, datetime(2026, 11, 9, 4, tzinfo=timezone.utc), 0, 4)


def test_decay_uses_local_week_boundaries_across_dst_and_years():
    # US fallback is November1; the naive-local interface keeps the Monday4AM schedule.
    assert decay_boundaries(datetime(2026, 10, 26, 4), datetime(2026, 11, 2, 4), 0, 4) == [
        datetime(2026, 11, 2, 4)]
    assert decay_boundaries(datetime(2026, 12, 28, 4), datetime(2027, 1, 4, 4), 0, 4) == [
        datetime(2027, 1, 4, 4)]


def test_decay_compounds_toward_zero():
    entries = decay_entries({1101: 100., 1102: -20., 1103: 0.}, 2, .10, 1.)
    assert [(e.guid, e.delta) for e in entries] == [
        (1101, -10.), (1101, -9.), (1102, 2.), (1102, 1.8)]
    assert all(e.reason == "decay" and e.ts == 1. for e in entries)
    assert decay_entries({1101: 10.}, 2, 0., 1.) == []


def test_apply_due_decay_is_idempotent(fake):
    add_ledger(fake.conn, [LedgerEntry(1., 1101, 100., "kill")])
    assert apply_due_decay(fake.conn, datetime(2026, 11, 3, 12), DkpConfig()) == 0
    assert fake.conn.execute("SELECT value FROM meta WHERE key='dkp_last_decay'").fetchone()[0] == (
        "2026-11-02T04:00:00")
    assert apply_due_decay(fake.conn, datetime(2026, 11, 10, 12), DkpConfig()) == 1
    assert apply_due_decay(fake.conn, datetime(2026, 11, 10, 12), DkpConfig()) == 0
    assert balances(fake.conn)[1101] == pytest.approx(90.)


def test_decay_rollback_clock_cannot_reapply_an_old_boundary(fake):
    add_ledger(fake.conn, [LedgerEntry(1., 1101, 100., "adjust")])
    apply_due_decay(fake.conn, datetime(2026, 11, 2, 4), DkpConfig())
    assert apply_due_decay(fake.conn, datetime(2026, 11, 16, 4), DkpConfig()) == 2
    assert apply_due_decay(fake.conn, datetime(2026, 11, 9, 4), DkpConfig()) == 0
    assert apply_due_decay(fake.conn, datetime(2026, 11, 16, 4), DkpConfig()) == 0
    assert balances(fake.conn)[1101] == pytest.approx(81.)


@pytest.mark.parametrize("failure", ["metadata", "second_entry"])
def test_decay_receipt_and_every_entry_roll_back_then_retry(fake, failure):
    add_ledger(fake.conn, [LedgerEntry(1., 1101, 100., "adjust"),
                           LedgerEntry(1., 1102, -20., "adjust")])
    apply_due_decay(fake.conn, datetime(2026, 11, 2, 4), DkpConfig())
    with fake.conn:
        if failure == "metadata":
            fake.conn.execute("CREATE TEMP TRIGGER fail_decay BEFORE UPDATE ON meta "
                              "WHEN NEW.key='dkp_last_decay' BEGIN SELECT RAISE(ABORT,'injected'); END")
        else:
            fake.conn.execute("CREATE TEMP TRIGGER fail_decay BEFORE INSERT ON dkp_ledger "
                              "WHEN NEW.reason='decay' AND NEW.guid=1102 "
                              "BEGIN SELECT RAISE(ABORT,'injected'); END")
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        apply_due_decay(fake.conn, datetime(2026, 11, 9, 4), DkpConfig())
    assert balances(fake.conn) == {1101: 100., 1102: -20.}
    assert fake.conn.execute("SELECT value FROM meta WHERE key='dkp_last_decay'").fetchone()[0] == (
        "2026-11-02T04:00:00")
    with fake.conn:
        fake.conn.execute("DROP TRIGGER fail_decay")
    assert apply_due_decay(fake.conn, datetime(2026, 11, 9, 4), DkpConfig()) == 1
    assert apply_due_decay(fake.conn, datetime(2026, 11, 9, 4), DkpConfig()) == 0
    assert balances(fake.conn) == {1101: 90., 1102: -18.}


def test_initial_decay_baseline_failure_leaves_no_receipt(fake):
    with fake.conn:
        fake.conn.execute("CREATE TEMP TRIGGER fail_decay BEFORE INSERT ON meta "
                          "WHEN NEW.key='dkp_last_decay' BEGIN SELECT RAISE(ABORT,'injected'); END")
    with pytest.raises(sqlite3.IntegrityError):
        apply_due_decay(fake.conn, datetime(2026, 11, 9, 4), DkpConfig())
    assert fake.conn.execute("SELECT value FROM meta WHERE key='dkp_last_decay'").fetchone() is None


def test_zero_rate_advances_receipt_without_ledger_noise(fake):
    add_ledger(fake.conn, [LedgerEntry(1., 1101, 100., "adjust")])
    apply_due_decay(fake.conn, datetime(2026, 11, 2, 4), DkpConfig(decay=0))
    assert apply_due_decay(fake.conn, datetime(2026, 11, 9, 4), DkpConfig(decay=0)) == 1
    assert fake.conn.execute("SELECT count(*) FROM dkp_ledger").fetchone()[0] == 1


@pytest.mark.parametrize(("balance", "price", "gain", "wanted", "expected"), [
    (200., 30, 13, False, 50.), (200., 30, 13, True, 65.),
    (20., 30, 13, False, 20.), (200., 30, None, True, 45.),
    (200., 30, 40, False, 60.), (-5., 30, 10, False, 0.),
    (200., 30, 40, True, 75.), (1.5, 30, 13, False, 1.5),
])
def test_bid(balance, price, gain, wanted, expected):
    assert bid(balance, price, gain, wanted) == expected


def test_award():
    a, b = Bidder(1101, 100., 10, False), Bidder(1102, 40., 20, False)
    def result(bidders, human=False):
        return award(30, bidders, human_claimed=human, human_guid=1)
    assert result([a, b]) == Award(1101, 41., 45., 1, 1102, 40., False)
    assert result([a, Bidder(1102, 100., 8, False)]) == Award(1101, 43., 45., 1, 1102, 42., True)
    assert result([a, Bidder(1103, 10., 5, True)]) == Award(1103, 10., 10., 0, 1101, 45., False)
    assert result([a, Bidder(1104, 200., None, True)]).guid == 1101
    assert result([a], True) == Award(1, 30., 30., -1, 1101, 45., False)
    assert result([Bidder(1105, 200., None, False)]) is None
    assert result([Bidder(1102, 100., 10, False), a]).guid == 1101
    assert result([Bidder(1105, 0., 10, False)]) == Award(1105, 0., 0., 1, None, None, False)


def test_award_balance_tiebreak_and_close_boundary():
    # Bids both cap at45; the larger balance wins before guid ordering.
    result = award(30, [Bidder(1101, 100., 10, False), Bidder(1102, 101., 10, False)],
                   human_claimed=False, human_guid=1)
    assert result == Award(1102, 45., 45., 1, 1101, 45., True)
    result = award(30, [Bidder(1101, 100., 20, False), Bidder(1102, 54., 20, False)],
                   human_claimed=False, human_guid=1)
    assert result.close is True and result.paid == 55.
    assert award(30, [], human_claimed=True, human_guid=1) == Award(1, 30., 30., -1, None, None, False)
    with pytest.raises(ValueError, match="duplicate"):
        award(30, [Bidder(1101, 100., 10, False)] * 2, human_claimed=False, human_guid=1)


def test_append_only_ledger_retains_identical_legitimate_awards(fake):
    entry = LedgerEntry(1., 1101, 10., "kill", raid_id=7)
    add_ledger(fake.conn, [entry, entry])
    add_ledger(fake.conn, [entry])
    assert balances(fake.conn) == {1101: 30.}


def test_ledger_batch_failure_cannot_partially_commit(fake):
    add_ledger(fake.conn, [LedgerEntry(1., 1101, 5., "adjust")])
    with pytest.raises(sqlite3.IntegrityError):
        add_ledger(fake.conn, [LedgerEntry(2., 1101, 10., "kill"),
                               LedgerEntry(2., 1102, 10., "invalid")])
    assert balances(fake.conn) == {1101: 5.}


def test_bench_streaks_and_attendance_counts_ignore_absent_raids(fake):
    conn = fake.conn
    r1, r2, r3, r4, r5 = [raid(conn, n) for n in range(1, 6)]
    record_attendance(conn, r1, [], [1101])
    record_attendance(conn, r2, [], [1101])
    record_attendance(conn, r3, [1101], [1102])
    record_attendance(conn, r4, [], [1101, 1102])
    record_attendance(conn, r5, [1103], [])
    assert bench_streaks(conn, [1101, 1102, 1199]) == {1101: 1, 1102: 2, 1199: 0}
    assert attendance_counts(conn, [1101, 1102, 1103, 1199], 3) == {
        1101: 1, 1102: 0, 1103: 1, 1199: 0}
    assert attendance_counts(conn, [1101], 4) == {1101: 0}


def test_attendance_replay_and_disjoint_validation(fake):
    run = raid(fake.conn, 1.)
    record_attendance(fake.conn, run, [1101, 1101], [1102])
    record_attendance(fake.conn, run, [1101], [1102])
    assert fake.conn.execute("SELECT count(*) FROM guild_attendance").fetchone()[0] == 2
    with pytest.raises(ValueError, match="both"):
        record_attendance(fake.conn, run, [1102], [1102])
    assert bench_streaks(fake.conn, [1101, 1102]) == {1101: 0, 1102: 1}
    with pytest.raises(sqlite3.IntegrityError):
        record_attendance(fake.conn, 999, [1103], [])


def test_run_history_online_leads_and_half_open_offline_window(fake):
    r1 = raid(fake.conn, 10.)
    raid(fake.conn, 20., size=25, heroic=True, lead_guid=1103)
    raid(fake.conn, 30., size=25, heroic=True, lead_guid=1104, offline=True)
    raid(fake.conn, 40., raid_key="toc", lead_guid=1101)
    end_raid(fake.conn, r1, ended=15., kills=2, wipes=1, outcome="called")
    row = fake.conn.execute("SELECT * FROM guild_raid WHERE raid_id=?", (r1,)).fetchone()
    assert (row["ended"], row["kills"], row["wipes"], row["outcome"]) == (15., 2, 1, "called")
    assert last_run(fake.conn) == {"icc10": 10., "icc25h": 30., "toc10": 40.}
    assert last_led(fake.conn) == {1101: 40., 1103: 20.}
    assert offline_raid_between(fake.conn, 30., 31.) is True
    assert offline_raid_between(fake.conn, 0., 30.) is False
    assert offline_raid_between(fake.conn, 31., 40.) is False


def test_flags_update_and_clear_separately(fake):
    set_flag(fake.conn, "geared", 1101, 1.)
    set_flag(fake.conn, "spec", 1101, 2., 1)
    set_flag(fake.conn, "spec", 1101, 3., 2)
    assert flags(fake.conn, "geared") == {1101}
    assert tuple(fake.conn.execute("SELECT ts,value FROM guild_flag WHERE kind='spec'").fetchone()) == (3., 2.)
    clear_flag(fake.conn, "geared", 1101)
    clear_flag(fake.conn, "geared", 1101)
    assert flags(fake.conn, "geared") == set() and flags(fake.conn, "spec") == {1101}
    with pytest.raises(sqlite3.IntegrityError):
        set_flag(fake.conn, "unknown", 1101, 4.)


def test_migration_is_repeatable_without_replacing_existing_history(fake):
    run = raid(fake.conn, 1.)
    add_ledger(fake.conn, [LedgerEntry(1., 1101, 10., "attendance", raid_id=run)])
    assert migrate(fake.conn) >= 7
    assert balances(fake.conn) == {1101: 10.}


def test_fake_ports_scripted_ack_job_and_distinct_turn_kinds(fake):
    from leeroy_mind.guild.protocol import FormRaid
    from leeroy_mind.talk.turns import TurnKind
    from .fakes import job, member, persona

    async def scenario():
        cmd = FormRaid(id="r1", leader_guid=1, members=(1101,), size=10,
                       map_id=631, main_tank_guid=1101)
        fake.acks["form_raid"].extend([(False, "busy"), (True, "")])
        assert not (await fake.send(cmd)).ok
        assert (await fake.send(cmd)).ok
        fake.jobs["form_raid"].append(job("form_raid", "done", guids=(1101,)))
        assert (await fake.wait_job("r1", 5)).id == "r1"
        assert await fake.wait_job("r1", 5) is None
        await fake.acquire_gpu("offline_raid")
        await fake.release_gpu("offline_raid")
        assert await fake.say(1101, "raid", "ready check") is True
    asyncio.run(scenario())
    fake.propose(1101, "raid", "lead the ready check")
    fake.propose(1102, "raid", "explain the delay", kind=TurnKind.ASIDE)
    assert [row[3] for row in fake.proposed] == [TurnKind.CALLOUT, TurnKind.ASIDE]
    assert fake.gpu == ["acquire:offline_raid", "release:offline_raid"]
    assert fake.human_name() == "Testy" and fake.persona_state(1101).altered_state == "sober"
    fake.members[1101] = member(1101, "Testguard", 1, 2)
    fake.personas[1101] = persona(1101, "Testguard", raid_role="tank")
    assert fake.cast()[1101].class_ == 1 and fake.persona(1101).play.raid_role == "tank"
