import asyncio
import dataclasses
import json
import logging
import sqlite3
from types import SimpleNamespace

import pytest

from leeroy_mind.training import protocol as p, store
from leeroy_mind.training.ledger import Ledger, PullAssembler, combat_role, context_key
from leeroy_mind.training.ports import RaidRow


def header(fake, *, start=None, duration=60_000, boss=0, members=1, deaths=0, tracks=0, **over):
    start = int(fake.clock.now() * 1000) if start is None else start
    return p.Pull(**(dict(ts=start + duration, id=f"{start}-77-{boss}", map=631, instance_id=77,
        boss_id=boss, difficulty=0, size=10, outcome="killed", start_ms=start, end_ms=start + duration,
        members=members, deaths=deaths, tracks=tracks, assisted=False, source="tracked",
        boss_entry=36612, upd_mean_ms=30, damage_basis="requested_pre_ai",
        healing_basis="effective_gain_with_pre_absorb_raw", spell_basis="best_effort_thread_pair",
        interrupt_basis="successful_interrupt_spell_cast") | over))


def member(h, guid=101, **over):
    return p.PullMember(**(dict(ts=h.ts, pull=h.id, guid=guid, class_=8, spec_tab=0, tank=False,
        ilvl=232, damage=100, damage_boss=80, healing=0, overheal=0, taken=10, deaths=0,
        interrupts=0, active_ms=0, done=(), taken_by=(), debuffs=(), casts=()) | over))


def death(h, guid=101, at_ms=500, **over):
    return p.PullDeath(**(dict(ts=h.ts, pull=h.id, guid=guid, at_ms=at_ms, spell=10, source=36612,
        x=-1, y=3, hits=(p.LastHit(at_ms - 1, 10, 36612, 10, 10),)) | over))


def track(h, who=101, start=0, n=2, **over):
    return p.PullTrack(**(dict(ts=h.ts, pull=h.id, who=who, entry=0 if who else h.boss_entry,
        from_s=start, xs=tuple(range(start, start+n)), ys=(2,) * n,
        o=() if who else (0,) * n, hp=(90,) * n, power=(80,) * n if who else (),
        target=() if who else (101,) * n, casts=()) | over))


def run(fake, *, human=True, started=None, ended=None, raid="icc", instance=77, heroic=False, size=10):
    started = fake.clock.now() - 10 if started is None else started
    with fake.conn:
        rid = fake.conn.execute("INSERT INTO guild_raid(started,ended,raid_key,size,heroic,offline,lead_guid,tank_guid,loot_guid) "
            "VALUES (?,?,?,?,?,?,?,?,?)", (started, ended, raid, size, int(heroic), int(not human), 101, 101, 102)).lastrowid
        if instance is not None:
            fake.conn.execute("INSERT INTO meta(key,value) VALUES (?,?)",
                              (f"guild:raid:{rid}:instance", json.dumps(instance)))
    return SimpleNamespace(session=SimpleNamespace(raid_id=rid, started=started,
        ctx=SimpleNamespace(map_id=631 if raid == "icc" else 649, instance_id=instance)),
        human_in_raid=human)


async def feed(ledger, *events):
    for ev in events:
        await ledger.on_event(ev)


def row(fake, ident):
    return fake.conn.execute("SELECT * FROM tg_pull WHERE id=?", (ident,)).fetchone()


def test_parts_then_header_make_one_complete_record(fake):
    h = header(fake, members=2, deaths=1, tracks=3, duration=1_200_000)
    a = PullAssembler(fake.clock)
    parts = (member(h), member(h, 102), death(h), track(h, start=600, n=600),
             track(h, n=600), track(h, who=0, casts=(p.TrackCast(50, 1),)))
    for part in parts:
        assert a.add(part) is None
    rec = a.add(h)
    assert rec.complete
    assert len(rec.members) == 2 and len(rec.deaths) == 1
    assert rec.tracks[101, 0]["xs"] == list(range(1200))
    assert rec.tracks[101, 0]["at_s"] == list(range(1200))
    assert rec.casts == (p.TrackCast(50, 1),)
    assert a.add(h) is None


def test_missing_part_and_late_parts_never_repair_final_header(fake):
    h = header(fake, members=2)
    a = PullAssembler(fake.clock)
    a.add(member(h))
    rec = a.add(h)
    assert not rec.complete and len(rec.members) == 1
    assert a.add(member(h, 102)) is None
    assert a.add(h) is None


@pytest.mark.parametrize("which", ["member", "death", "track"])
def test_duplicates_cannot_satisfy_counts_or_rewrite_first_observation(fake, which):
    h = header(fake, deaths=1, tracks=1)
    a = PullAssembler(fake.clock)
    parts = {"member": member(h), "death": death(h), "track": track(h)}
    for part in parts.values():
        a.add(part)
    a.add(parts[which])
    assert not a.add(h).complete


def test_sparse_tracks_preserve_seconds_and_cast_only_boss(fake):
    h = header(fake, tracks=3)
    a = PullAssembler(fake.clock)
    for part in (member(h), track(h, start=1, n=1), track(h, start=9, n=1),
                 track(h, who=0, n=0, casts=(p.TrackCast(50, 8),))):
        a.add(part)
    rec = a.add(h)
    assert rec.complete and rec.tracks[101, 0]["at_s"] == [1, 9]
    assert rec.tracks[101, 0]["xs"] == [1, 9]
    assert rec.tracks[0, h.boss_entry]["at_s"] == []
    assert rec.casts == (p.TrackCast(50, 8),)


@pytest.mark.parametrize("bad", [
    {"ys": (1,)}, {"hp": (101, 80)}, {"from_s": 1199}, {"o": (0, 0)},
    {"entry": 123}, {"casts": (p.TrackCast(40, 1),)}, {"power": (True, 80)},
])
def test_malformed_tracks_preserve_other_evidence_but_not_completeness(fake, bad):
    h = header(fake, tracks=1, deaths=1)
    a = PullAssembler(fake.clock)
    for part in (member(h), death(h), track(h, **bad)):
        a.add(part)
    rec = a.add(h)
    assert not rec.complete and len(rec.members) == len(rec.deaths) == 1


@pytest.mark.parametrize("case", ["overlap", "foreign_member", "foreign_boss", "late_cast", "late_death"])
def test_cross_part_consistency_is_required(fake, case):
    h = header(fake, deaths=int(case == "late_death"), tracks=2 if case == "overlap" else int(case != "late_death"))
    a = PullAssembler(fake.clock)
    a.add(member(h))
    if case == "overlap":
        a.add(track(h, n=3))
        a.add(track(h, start=2))
    elif case == "foreign_member":
        a.add(track(h, who=102))
    elif case == "foreign_boss":
        a.add(track(h, who=0, entry=42))
    elif case == "late_cast":
        a.add(track(h, who=0, casts=(p.TrackCast(50, 61),)))
    else:
        a.add(death(h, at_ms=60_001))
    assert not a.add(h).complete


@pytest.mark.parametrize("bad", [{"members": -1}, {"deaths": 81}, {"tracks": 24601},
    {"size": 25}, {"difficulty": 4}, {"source": "unknown"}, {"outcome": "won"},
    {"end_ms": 0}, {"instance_id": 78}, {"spell_basis": True}])
def test_invalid_header_cannot_reach_database(fake, bad):
    h = header(fake)
    ledger = Ledger(fake)
    asyncio.run(feed(ledger, member(h), dataclasses.replace(h, **bad)))
    assert fake.conn.execute("SELECT count(*) FROM tg_pull").fetchone()[0] == 0


def test_orphans_expire_from_receipt_time_not_old_wire_time(fake, caplog):
    h = header(fake, start=1000)
    a = PullAssembler(fake.clock)
    caplog.set_level(logging.INFO)
    a.add(member(h))
    assert a.expire(fake.clock.now() + 599) == 0
    assert a.expire(fake.clock.now() + 600) == 1
    assert a.expire(fake.clock.now() + 601) == 0
    assert a.add(h) is None
    assert "tg_parts_expired" in caplog.text


def test_pending_and_point_caps_fail_closed(fake, monkeypatch):
    a = PullAssembler(fake.clock)
    monkeypatch.setattr(a, "MAX_PENDING", 2)
    hs = [header(fake, start=1000 + i, tracks=1) for i in range(3)]
    for h in hs:
        a.add(member(h))
    assert len(a._parts) == 2 and a.add(hs[0]) is None
    monkeypatch.setattr(a, "MAX_POINTS", 1)
    a.add(track(hs[2], n=2))
    assert not a.add(hs[2]).complete


def test_connection_change_cannot_combine_two_partial_streams(fake):
    async def scenario():
        ledger = Ledger(fake)
        h = header(fake, members=2)
        await ledger.on_event(member(h))
        await ledger.on_connection(False)
        await ledger.on_connection(True)
        await feed(ledger, member(h, 102), h)
        assert row(fake, h.id)["complete"] == row(fake, h.id)["scoreable"] == 0
        assert [r["guid"] for r in store.member_rows(fake.conn, h.id)] == [102]
    asyncio.run(scenario())


def test_parts_from_different_encoder_receipts_do_not_form_complete_pull(fake):
    h = header(fake, members=2)
    a = PullAssembler(fake.clock)
    a.add(member(h))
    a.add(member(h, 102, ts=h.ts + 1))
    assert not a.add(h).complete


def test_new_connection_can_receive_a_complete_backlog(fake):
    async def scenario():
        ledger = Ledger(fake)
        h = header(fake, start=1000)
        await ledger.on_event(member(h))
        await ledger.on_connection(False)
        await ledger.on_connection(True)
        await feed(ledger, member(h), h)
        assert row(fake, h.id)["complete"] == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("outcome,complete,boss", [("aborted", True, 0), ("killed", False, 0), ("killed", True, 13)])
def test_aborted_incomplete_and_non_fight_pulls_never_scored(fake, outcome, complete, boss):
    h = header(fake, outcome=outcome, members=1 if complete else 2, boss=boss)
    asyncio.run(feed(Ledger(fake), member(h), h))
    assert row(fake, h.id)["scoreable"] == 0


@pytest.mark.parametrize("human,source,expected", [(True, "tracked", "his"),
    (False, "tracked", "offline"), (True, "benchmark", "benchmark")])
def test_kind_from_active_persisted_run(fake, human, source, expected):
    ledger = Ledger(fake)
    active = run(fake, human=human)
    ledger.set_run(active)
    h = header(fake, source=source)
    asyncio.run(feed(ledger, member(h), h))
    saved = row(fake, h.id)
    assert saved["kind"] == expected
    assert saved["raid_id"] == (None if source == "benchmark" else active.session.raid_id)


@pytest.mark.parametrize("mismatch", ["instance", "map", "size", "difficulty", "future", "ended"])
def test_foreign_or_out_of_span_pull_does_not_borrow_active_run(fake, mismatch):
    active = run(fake, instance=78 if mismatch == "instance" else 77,
        raid="toc" if mismatch == "map" else "icc", size=25 if mismatch == "size" else 10,
        heroic=mismatch == "difficulty", started=fake.clock.now() + 1 if mismatch == "future" else None,
        ended=fake.clock.now() if mismatch == "ended" else None)
    ledger = Ledger(fake)
    ledger.set_run(active)
    h = header(fake)
    asyncio.run(feed(ledger, member(h), h))
    assert row(fake, h.id)["kind"] == "other"


def test_backlog_uses_historical_run_and_does_not_borrow_current_state(fake):
    h = header(fake, start=1000)
    fake.raids = [RaidRow(7, True, 631, 10, False, 102, started=0.5, ended=2)]
    fake.drunk_set = {101}
    fake.skips = {101}
    ledger = Ledger(fake)
    ledger.set_run(run(fake))
    asyncio.run(feed(ledger, member(h), h))
    saved = row(fake, h.id)
    assert (saved["kind"], saved["raid_id"], saved["lead_guid"]) == ("offline", 7, 102)
    cov = json.loads(saved["covariates"])
    assert cov["drunk"] == cov["flask_skips"] == "unknown"
    assert ledger.first_pull_ts() == fake.clock.now()  # Old ts does not waive the observation period.


def test_covariates_and_provenance(fake):
    h = header(fake, members=6, assisted=True)
    ledger = Ledger(fake)
    ledger.set_run(run(fake))
    fake.members = {101: object(), 102: object()}
    fake.drunk_set, fake.skips = {101, 999}, {102, 999}
    parts = [member(h, 101, class_=1, spec_tab=2, tank=True),
        member(h, 102, class_=11, spec_tab=1, tank=True),
        member(h, 103, class_=5), member(h, 104, class_=2),
        member(h, 105, class_=7, spec_tab=2), member(h, 106)]
    asyncio.run(feed(ledger, *parts, h))
    saved = row(fake, h.id)
    cov = json.loads(saved["covariates"])
    assert cov == dict(ilvl=232, tanks=2, healers=3, drunk=1, flask_skips=1, comp="2-3-1",
                       human_in_raid=1, assisted=1, state_basis="receipt_time",
                       role_basis="recorded_and_receipt_persona")
    assert saved["spell_basis"] == "best_effort_thread_pair"
    assert saved["interrupt_basis"] == "successful_interrupt_spell_cast"
    assert saved["scoreable"] == 1


@pytest.mark.parametrize("cls,tab,declared,tank,expected", [
    (11, 1, None, True, "tank"), (11, 1, None, False, "melee"),
    (6, 0, None, False, "melee"), (6, 0, "melee", False, "melee"), (6, 2, None, True, "tank"),
    (1, 2, "tank", False, "tank"), (5, 1, None, False, "healer"), (7, 1, None, False, "melee"),
    (8, 2, "melee", False, "melee"), (4, 1, None, False, "melee"), (3, 2, None, False, "ranged"),
])
def test_combat_role_refines_recorded_role(cls, tab, declared, tank, expected):
    assert combat_role(cls, tab, declared, tank=tank) == expected


def test_in_play_and_veto_are_timestamped_run_scoped_and_consumed_once(fake):
    async def scenario():
        ledger = Ledger(fake)
        active = run(fake)
        ledger.set_run(active)
        key = context_key(631, 0, 0)
        ledger.set_in_play(key, card_id=7, arm_id=3, change="Spread farther apart")
        ledger.mark_unscoreable(key)
        old = header(fake, start=int(fake.clock.now() * 1000) - 1)
        await feed(ledger, member(old), old)
        assert row(fake, old.id)["card_id"] is None and row(fake, old.id)["scoreable"] == 1
        h = header(fake)
        await feed(ledger, member(h), h)
        assert tuple(row(fake, h.id)[k] for k in ("card_id", "arm_id", "change", "scoreable")) == (
            7, 3, "Spread farther apart", 0)
        await feed(ledger, member(h), h)
        later = header(fake, start=h.start_ms + 1)
        await feed(ledger, member(later), later)
        assert row(fake, later.id)["scoreable"] == 1
    asyncio.run(scenario())


def test_old_run_backlog_cannot_consume_new_run_veto(fake):
    async def scenario():
        now = fake.clock.now()
        old = run(fake, started=now - 30, ended=now - 20)
        current = run(fake)
        ledger = Ledger(fake)
        ledger.set_run(current)
        ledger.mark_unscoreable("631:0:0")
        fake.raids = [RaidRow(old.session.raid_id, False, 631, 10, False, 101, now - 30, now - 20)]
        h = header(fake, start=int((now - 25) * 1000))
        await feed(ledger, member(h), h)
        assert row(fake, h.id)["scoreable"] == 1
        new = header(fake)
        await feed(ledger, member(new), new)
        assert row(fake, new.id)["scoreable"] == 0
    asyncio.run(scenario())


def test_atomic_store_failure_retries_frozen_record_without_losing_veto(fake):
    async def scenario():
        ledger = Ledger(fake)
        active = run(fake)
        ledger.set_run(active)
        ledger.mark_unscoreable("631:0:0")
        h = header(fake)
        with fake.conn:
            fake.conn.execute("CREATE TRIGGER reject_member BEFORE INSERT ON tg_member BEGIN SELECT RAISE(ABORT,'test'); END")
        await ledger.on_event(member(h))
        with pytest.raises(sqlite3.IntegrityError):
            await ledger.on_event(h)
        assert row(fake, h.id) is None and ledger.first_pull_ts() is None
        assert fake.conn.execute("SELECT count(*) FROM meta WHERE key LIKE 'tg:unscoreable:%'").fetchone()[0] == 1
        with fake.conn:
            fake.conn.execute("DROP TRIGGER reject_member")
        await ledger.on_event(h)
        assert row(fake, h.id)["scoreable"] == 0
        assert ledger.first_pull_ts() == fake.clock.now()
        assert fake.conn.execute("SELECT count(*) FROM meta WHERE key LIKE 'tg:unscoreable:%'").fetchone()[0] == 0
    asyncio.run(scenario())


def test_context_read_failure_keeps_sealed_record_for_exact_header_retry(fake, monkeypatch):
    async def scenario():
        ledger = Ledger(fake)
        h = header(fake)
        with monkeypatch.context() as patch:
            def unavailable():
                raise sqlite3.OperationalError("temporary context failure")
            patch.setattr(fake, "cast", unavailable)
            await ledger.on_event(member(h))
            with pytest.raises(sqlite3.OperationalError):
                await ledger.on_event(h)
        assert row(fake, h.id) is None
        await ledger.on_event(h)
        assert row(fake, h.id)["complete"] == 1
        assert len(store.member_rows(fake.conn, h.id)) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("replace_marker", [False, True])
def test_failed_pull_token_cannot_veto_twice_or_consume_replacement(fake, replace_marker):
    async def scenario():
        ledger = Ledger(fake)
        ledger.set_run(run(fake))
        ledger.mark_unscoreable("631:0:0")
        a = header(fake)
        with fake.conn:
            fake.conn.execute("CREATE TRIGGER reject_member BEFORE INSERT ON tg_member BEGIN SELECT RAISE(ABORT,'test'); END")
        await ledger.on_event(member(a))
        with pytest.raises(sqlite3.IntegrityError):
            await ledger.on_event(a)
        with fake.conn:
            fake.conn.execute("DROP TRIGGER reject_member")
        fake.clock._t += 1
        if replace_marker:
            ledger.mark_unscoreable("631:0:0")
        else:
            b = header(fake)
            await feed(ledger, member(b), b)
            assert row(fake, b.id)["scoreable"] == 0
        await ledger.on_event(a)
        assert row(fake, a.id)["scoreable"] == 1
        if replace_marker:
            assert fake.conn.execute("SELECT count(*) FROM meta WHERE key LIKE 'tg:unscoreable:%'").fetchone()[0] == 1
            b = header(fake)
            await feed(ledger, member(b), b)
            assert row(fake, b.id)["scoreable"] == 0
        assert fake.conn.execute("SELECT count(*) FROM meta WHERE key LIKE 'tg:unscoreable:%'").fetchone()[0] == 0
    asyncio.run(scenario())


def test_negative_recent_hit_is_not_valid_death_evidence(fake):
    h = header(fake, deaths=1)
    a = PullAssembler(fake.clock)
    a.add(member(h))
    a.add(death(h, hits=(p.LastHit(-1, 10, 36612, 10, 10),)))
    rec = a.add(h)
    assert not rec.complete and rec.deaths == ()


def test_historical_roles_do_not_use_today_persona_even_with_same_open_run(fake):
    async def scenario():
        ledger = Ledger(fake)
        ledger.set_run(run(fake, started=fake.clock.now() - 600))
        fake.members = {101: object()}
        fake.personas = {101: SimpleNamespace(play=SimpleNamespace(raid_role="ranged"))}
        old = header(fake, start=int((fake.clock.now() - 300) * 1000))
        await feed(ledger, member(old, class_=4), old)
        assert store.member_rows(fake.conn, old.id)[0]["role"] == "melee"
        assert json.loads(row(fake, old.id)["covariates"])["role_basis"] == "recorded_tank_and_spec"
        fresh = header(fake)
        await feed(ledger, member(fresh, class_=4), fresh)
        assert store.member_rows(fake.conn, fresh.id)[0]["role"] == "ranged"
        assert json.loads(row(fake, fresh.id)["covariates"])["role_basis"] == "recorded_and_receipt_persona"
    asyncio.run(scenario())


def test_replay_after_recreation_never_repeats_callbacks_or_replaces_receipt(fake, caplog):
    async def scenario():
        ledger = Ledger(fake)
        h = header(fake)
        calls = []
        async def fail(rec, ctx):
            calls.append("failed")
            raise RuntimeError("private diagnostic must not be logged")
        async def good(rec, ctx):
            calls.append("good")
        ledger.after_store.extend([fail, good])
        await feed(ledger, member(h), h)
        assert calls == ["failed", "good"] and "tg_after_store_failed" in caplog.text
        assert "private diagnostic" not in caplog.text
        receipt = row(fake, h.id)["received"]
        rebuilt = Ledger(fake)
        rebuilt.after_store.append(good)
        await feed(rebuilt, member(h, damage=1, damage_boss=1), h)
        assert calls == ["failed", "good"] and row(fake, h.id)["received"] == receipt
        assert store.member_rows(fake.conn, h.id)[0]["damage"] == 100
        assert rebuilt.first_pull_ts() == receipt
    asyncio.run(scenario())


def test_first_receipt_is_set_once_and_partial_evidence_is_persisted(fake):
    async def scenario():
        ledger = Ledger(fake)
        h = header(fake, members=2, deaths=1)
        await feed(ledger, member(h), death(h), h)
        first = ledger.first_pull_ts()
        fake.clock._t += 100
        next_h = header(fake)
        await feed(ledger, member(next_h), next_h)
        assert ledger.first_pull_ts() == first
        assert row(fake, h.id)["complete"] == 0
        assert len(store.member_rows(fake.conn, h.id)) == len(store.death_rows(fake.conn, h.id)) == 1
    asyncio.run(scenario())
