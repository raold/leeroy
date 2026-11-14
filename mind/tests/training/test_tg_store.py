import asyncio
import dataclasses
import json
import shutil
import sqlite3
from types import SimpleNamespace

import pytest

from conftest import make_mem
from test_migrations import _old_memory, _vectors
from leeroy_mind.config import ConfigError, DEFAULT_TOML, EXTENSION_SECTIONS, load_config
from leeroy_mind.storage import db
from leeroy_mind.storage.rows import pack_vec
from leeroy_mind.training import protocol as p, store as s
from leeroy_mind.training.config import TrainingConfig, load_training_config
from leeroy_mind.training.ports import EventWaiter, RaidRow


TABLES = {"tg_" + name for name in (
    "pull member death track failure card lesson point trial arm obs applied rotation dummy setup benchmark perf".split())}


def record(ident="1000-77-0", *, complete=True, outcome="killed", boss=0, end=2000):
    head = p.Pull(2000, ident, 631, 77, boss, 0, 10, outcome, 1000, end, 1, 1, 2, False, "tracked", 36612, 30)
    member = p.PullMember(2000, ident, 101, 1, 2, True, 232, 100, 90, 0, 0, 5, 1, 1, 900,
                          (p.SpellTotal(1, 100, 1),), (), (), ())
    death = p.PullDeath(2000, ident, 101, 800, 10, 20, -1, 2, (p.LastHit(790, 10, 20, 5, 5),))
    return SimpleNamespace(header=head, members=(member,), deaths=(death,), complete=complete,
        tracks={(101, 0): {"xs": [-1], "ys": [2]}, (0, 36612): {"xs": [0], "ys": [3]}},
        casts=(p.TrackCast(42, 1),))


def context(**over):
    return SimpleNamespace(**(dict(kind="offline", lead_guid=101, raid_id=None, card_id=None,
        arm_id=None, change="", covariates={"ilvl": 232, "human_in_raid": 0}) | over))


def card(**over):
    return dict(context="631:0:0", version=1, status="candidate", origin="stock", directives=[],
                created=10.0, changed=10.0) | over


def test_checks_coverage_and_score_basis_are_explicit_and_versioned(fake):
    rec, ctx = record(), context()
    s.put_pull(fake.conn, rec, ctx, {101: "tank"}, received=10)
    row = s.pull_rows(fake.conn)[0]
    assert row["checks_complete"] == 0 and row["checks_coverage"] == "[]"
    assert row["score_basis"] == "unknown"
    s.set_checks(fake.conn, rec.header.id, [], ["unknown"], deaths=1, failed=3, version=1,
                 complete=False, coverage=("positions_missing",))
    s.set_score(fake.conn, rec.header.id, -8, basis="deaths_time")
    row = s.pull_rows(fake.conn)[0]
    assert row["failed_mechanics"] == 3 and row["score"] == -8
    assert row["checks_complete"] == 0 and json.loads(row["checks_coverage"]) == ["positions_missing"]
    assert row["score_basis"] == "deaths_time"
    s.set_checks(fake.conn, rec.header.id, [], ["unknown"], deaths=1, failed=0, version=1, complete=True)
    assert s.pull_rows(fake.conn)[0]["checks_complete"] == 0
    s.set_checks(fake.conn, rec.header.id, [], ["unknown"], deaths=1, failed=0, version=2, complete=True)
    row = s.pull_rows(fake.conn)[0]
    assert row["checks_complete"] == 1 and row["checks_coverage"] == "[]"
    assert row["score"] is None and row["score_basis"] == "unknown"
    s.set_score(fake.conn, rec.header.id, -8)  # Legacy callers cannot imply verification.
    assert s.pull_rows(fake.conn)[0]["score_basis"] == "unknown"


@pytest.mark.parametrize("version", [5, 6, 7])
def test_actual_migration_ancestry_preserves_existing_rows_and_vectors(tmp_path, monkeypatch, version):
    latest = db.latest_version()
    assert [path.name for n, path in db._migration_files() if 5 <= n <= 8] == [
        "0005_voice.sql", "0006_nightly.sql", "0007_guild_life.sql", "0008_training.sql"]
    old = tmp_path / "old-migrations"
    old.mkdir()
    for n, path in db._migration_files():
        if n <= version:
            shutil.copyfile(path, old / path.name)
    path = tmp_path / "older.db"
    with monkeypatch.context() as patch:
        patch.setattr(db, "MIGRATIONS_DIR", old)
        conn = db.open_db(path)
    conn.execute("INSERT INTO meta(key,value) VALUES ('training_test','keep')")
    conn.execute("INSERT INTO voice_presence(guid,room,since) VALUES (101,'raid',2)")
    mid = _old_memory(conn, make_mem(owner=101, embedding=pack_vec([0.25] * 512)))
    if version >= 6:
        conn.execute("INSERT INTO night_step(night,stage,unit,finished) VALUES ('2026-01-01','mood','all',3)")
    if version >= 7:
        conn.execute("INSERT INTO guild_raid(started,raid_key,size,heroic,offline,lead_guid,tank_guid,loot_guid) "
                     "VALUES (4,'icc',10,0,1,101,101,102)")
    conn.commit()
    vectors = _vectors(conn)
    text = conn.execute("SELECT text,embedding FROM memory WHERE id=?", (mid,)).fetchone()
    conn.close()
    conn = db.open_db(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == latest
        assert {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name GLOB 'tg_*'")} == TABLES
        assert conn.execute("SELECT value FROM meta WHERE key='training_test'").fetchone()[0] == "keep"
        assert tuple(conn.execute("SELECT guid,room,since FROM voice_presence").fetchone()) == (101, "raid", 2)
        assert tuple(conn.execute("SELECT text,embedding FROM memory WHERE id=?", (mid,)).fetchone()) == tuple(text)
        assert _vectors(conn) == vectors
        if version >= 6:
            assert conn.execute("SELECT count(*) FROM night_step").fetchone()[0] == 1
        if version >= 7:
            assert conn.execute("SELECT count(*) FROM guild_raid").fetchone()[0] == 1
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.migrate(conn) == latest
    finally:
        conn.close()


def test_config_defaults_and_extension_are_off_and_unwired(tmp_path):
    assert load_training_config(tmp_path / "none.toml") == TrainingConfig()
    assert TrainingConfig().enabled is False
    assert "training" in EXTENSION_SECTIONS
    assert "[training]" not in DEFAULT_TOML
    path = tmp_path / "mind.toml"
    path.write_text('[training]\nenabled = false\nbenchmark_raids = ["ICC10H", "toc25"]\n', encoding="utf-8")
    load_config(path)  # The core parser accepts, but does not activate, the extension.
    assert load_training_config(path).benchmark_raids == ("icc10h", "toc25")


@pytest.mark.parametrize("key,value", [
    ("enabled", "1"), ("act", '"true"'), ("lab_bots", "0"), ("lab_bots", "11"),
    ("observe_days", "true"), ("observe_days", "366"), ("benchmark_weekday", "7"),
    ("benchmark_min_ilvl", "301"), ("gate_pulls", "3"), ("gate_pulls", "101"),
    ("gate_max_ms", "nan"), ("gate_max_ms", "inf"), ("gate_max_ms", "false"),
    ("gate_max_ms", "0.09"), ("gate_max_ms", "10.1"), ("lab_window", '["24:00", "01:00"]'),
    ("benchmark_window", '["8:00", "12:00"]'), ("benchmark_raids", '["voa10"]'),
    ("benchmark_raids", '["icc11"]'), ("benchmark_raids", "[]"), ("benchmark_raids", "[4]"),
    ("core_dir", '""'), ("unknown", "true"), ("playbooks", '"yes"'),
])
def test_config_rejects_bad_values_without_echoing_them(tmp_path, key, value):
    path = tmp_path / "mind.toml"
    path.write_text(f"[training]\n{key} = {value}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match=rf"training\.{key}"):
        load_training_config(path)


def test_pull_round_trip_replay_and_track_pruning(fake):
    rec = record()
    s.put_pull(fake.conn, rec, context(), {101: "tank"}, received=3)
    s.put_pull(fake.conn, rec, context(kind="other"), {101: "tank"}, received=99)
    row = s.pull_rows(fake.conn)[0]
    assert (row["kind"], row["received"], row["scoreable"]) == ("offline", 3, 1)
    assert len(s.member_rows(fake.conn, rec.header.id)) == len(s.death_rows(fake.conn, rec.header.id)) == 1
    boss = next(r for r in s.track_rows(fake.conn, rec.header.id) if r["who"] == 0)
    assert json.loads(boss["data"])["casts"] == [{"spell": 42, "at_s": 1}]
    assert s.prune_tracks(fake.conn, 2000) == 0
    assert s.prune_tracks(fake.conn, 2001) == 2
    s.put_pull(fake.conn, rec, context(), {101: "tank"}, received=100)
    assert not s.track_rows(fake.conn, rec.header.id)
    assert len(s.pull_rows(fake.conn)) == len(s.member_rows(fake.conn, rec.header.id)) == 1


@pytest.mark.parametrize("kwargs", [{"complete": False}, {"outcome": "aborted"}, {"boss": 13}])
def test_invalid_pull_never_scoreable_even_with_true_override(fake, kwargs):
    rec = record(**kwargs)
    s.put_pull(fake.conn, rec, context(), {101: "tank"}, received=3, scoreable=True)
    s.set_score(fake.conn, rec.header.id, 42)
    assert [(r["scoreable"], r["score"]) for r in s.pull_rows(fake.conn)] == [(0, None)]


def test_pull_veto_and_filters(fake):
    s.put_pull(fake.conn, record(), context(), {101: "tank"}, received=3, scoreable=False)
    assert len(s.pull_rows(fake.conn, context="631:0:0", kinds=["offline"], since_ms=1000, scoreable=False)) == 1
    assert s.pull_rows(fake.conn, kinds=[]) == s.pull_rows(fake.conn, since_ms=1001) == []


def test_pull_failure_rolls_back_every_child_and_can_retry(fake):
    rec = record()
    fake.conn.execute("CREATE TRIGGER fail_track BEFORE INSERT ON tg_track BEGIN SELECT RAISE(ABORT,'fault'); END")
    fake.conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        s.put_pull(fake.conn, rec, context(), {101: "tank"}, received=3)
    for table in ("tg_pull", "tg_member", "tg_death", "tg_track"):
        assert fake.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    fake.conn.execute("DROP TRIGGER fail_track")
    fake.conn.commit()
    s.put_pull(fake.conn, rec, context(), {101: "tank"}, received=3)
    assert len(s.pull_rows(fake.conn)) == 1


def test_checks_are_atomic_versioned_and_keep_audit_rows(fake):
    rec = record()
    s.put_pull(fake.conn, rec, context(), {101: "tank"}, received=3)
    failure = dict(kind="avoid", key="fire", guid=101, count=2, deaths=1, damage_share=0.5)
    s.set_checks(fake.conn, rec.header.id, [failure], ["fire"], deaths=1, failed=2, version=1)
    ident = fake.conn.execute("SELECT id FROM tg_failure").fetchone()[0]
    s.set_score(fake.conn, rec.header.id, 0.5)
    s.set_checks(fake.conn, rec.header.id, [failure], ["fire"], deaths=1, failed=2, version=1)
    assert s.pull_rows(fake.conn)[0]["score"] == 0.5
    with pytest.raises(ValueError):
        s.set_checks(fake.conn, rec.header.id, [], [], deaths=0, failed=0, version=2)
    assert s.pull_rows(fake.conn)[0]["checks_version"] == 1
    s.set_checks(fake.conn, rec.header.id, [], ["unknown"], deaths=0, failed=0, version=2)
    assert tuple(fake.conn.execute("SELECT id,count FROM tg_failure").fetchone()) == (ident, 0)
    assert s.death_rows(fake.conn, rec.header.id)[0]["cause"] == "unknown"
    assert s.pull_rows(fake.conn)[0]["score"] is None


def test_one_champion_and_bounded_call_enforced_by_database(fake):
    first = s.put_card(fake.conn, card(status="champion"))
    with pytest.raises(sqlite3.IntegrityError):
        s.put_card(fake.conn, card(status="champion", version=2))
    with pytest.raises(sqlite3.IntegrityError):
        s.put_card(fake.conn, card(call="x" * 61))
    assert s.champion(fake.conn, "631:0:0")["id"] == first
    s.set_card_status(fake.conn, first, "retired", ts=12)
    assert s.champion(fake.conn, "631:0:0") is None
    assert len(s.card_rows(fake.conn, "631:0:0", ["retired"])) == 1


def test_applied_log_undo_is_idempotent_and_preserves_commands(fake):
    ident = s.log_applied(fake.conn, "r1", "boss", "631:0:0", {"type": "bot_do", "param": "+threat"},
                          {"type": "bot_do", "action": "restore", "param": "threat"}, 10)
    row = s.open_applied(fake.conn, 10)[0]
    assert row["id"] == ident and json.loads(row["inverse"])["action"] == "restore"
    s.mark_undone(fake.conn, [ident], 11)
    s.mark_undone(fake.conn, [ident], 90)
    assert s.open_applied(fake.conn, 0) == []
    assert fake.conn.execute("SELECT undone FROM tg_applied").fetchone()[0] == 11


def test_lessons_points_observations_and_perf_do_not_double_on_replay(fake):
    rec = record()
    s.put_pull(fake.conn, rec, context(), {101: "tank"}, received=3)
    cid = s.put_card(fake.conn, card())
    trial = s.open_trial(fake.conn, "631:0:0", "threat", "raid", opened=10, arms=[{"card": cid, "champion": True}])
    arm = fake.conn.execute("SELECT id FROM tg_arm WHERE trial=?", (trial,)).fetchone()[0]
    for value in (1, 999):
        s.put_obs(fake.conn, arm, rec.header.id, "631:0:0", value, fails=0.2)
        s.put_lesson(fake.conn, "631:0:0", "2026-01-01", "Keep together.", [rec.header.id])
        s.put_points(fake.conn, "631:0:0", "2026-01-01", [{"fact": "one", "text": "One kill."}])
        s.put_perf(fake.conn, 10, rec.header.id, "631:0:0", "offline", True, 30)
    assert len(s.obs_rows(fake.conn, arm)) == len(s.lessons(fake.conn, "631:0:0")) == 1
    assert s.obs_rows(fake.conn, arm)[0]["value"] == 1
    assert len(s.unused_points(fake.conn, "631:0:0")) == len(s.perf_rows(fake.conn)) == 1
    s.close_trial(fake.conn, trial, "retired", ts=20, verdict="bounded")
    s.close_trial(fake.conn, trial, "promoted", ts=30)
    assert s.trial_rows(fake.conn, status="retired")[0]["closed"] == 20


def test_rotation_setup_dummy_benchmark_roundtrips(fake):
    rid = s.put_rotation(fake.conn, dict(name="tg_test_v1", class_=1, spec_tab=0,
        lines=["often>attack!6"], origin="seed", status="candidate", created=1))
    assert json.loads(s.rotation_rows(fake.conn, class_=1)[0]["lines"]) == ["often>attack!6"]
    s.put_dummy(fake.conn, dict(rotation=rid, guid=101, round=0, seconds=30, dps=100, stock_dps=90, ts=2))
    first = s.put_setup(fake.conn, dict(guid=101, since=3, rotation=rid))
    second = s.put_setup(fake.conn, dict(guid=101, since=3, toggles=["+threat"], vals={"formation": "near"}))
    assert second > first and s.latest_setups(fake.conn)[101]["id"] == second
    row = dict(week="2026-W01", raid_key="icc10", guild_id=7, members=[5001, 5002], bosses=[0],
               outcome="cleared", kills=1, wipes=0, ts=4)
    bid = s.put_benchmark(fake.conn, row)
    assert s.put_benchmark(fake.conn, row | {"kills": 99}) == bid
    assert fake.conn.execute("SELECT kills FROM tg_benchmark").fetchone()[0] == 1


def vocab(ident, cls):
    return p.Vocab(0, ident, cls, 0, (), (), (), False, (), (), ())


def test_waiter_interleaves_ids_and_types_and_stops_on_terminal():
    async def scenario():
        p.register()
        waiter = EventWaiter()
        waiter.feed(vocab("a", 1))  # before wait: send/dispatch race
        a = asyncio.create_task(waiter.wait("a", "vocab", until=lambda e: e.class_ == 0, timeout_s=1))
        await asyncio.sleep(0)
        waiter.feed(vocab("b", 0))
        waiter.feed(p.ServerStats(0, "a", 1, 1, False, 0))
        waiter.feed(vocab("a", 8))
        await asyncio.sleep(0)
        assert not a.done()
        waiter.feed(vocab("a", 0))
        waiter.feed(vocab("a", 11))
        assert [e.class_ for e in await a] == [1, 8, 0]
        assert [e.class_ for e in await waiter.wait("b", "vocab", until=lambda e: True, timeout_s=0)] == [0]
        with pytest.raises(ValueError):
            await waiter.wait("a", "vocab", until=lambda e: True, timeout_s=0)
    asyncio.run(scenario())


def test_waiter_timeout_partial_cancel_duplicate_wait_and_overflow():
    async def scenario():
        p.register()
        waiter = EventWaiter()
        waiter.feed(vocab("a", 1))
        rows = await waiter.wait("a", "vocab", until=lambda e: e.class_ == 0, timeout_s=0)
        assert [e.class_ for e in rows] == [1]
        task = asyncio.create_task(waiter.wait("b", "vocab", until=lambda e: False, timeout_s=20))
        await asyncio.sleep(0)
        with pytest.raises(ValueError, match="already has"):
            await waiter.wait("b", "vocab", until=lambda e: True, timeout_s=0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not waiter._streams
        for _ in range(waiter.MAX_EVENTS + 1):
            waiter.feed(vocab("c", 1))
        with pytest.raises(BufferError):
            await waiter.wait("c", "vocab", until=lambda e: True, timeout_s=0)
    asyncio.run(scenario())


def test_fake_holds_and_historical_raid_boundaries(fake):
    fake.holders[102] = "live"
    assert fake.claim("training", [101, 102], until=10) == {101}
    fake.release("training", [101, 102])
    assert fake.holders == {102: "live"}
    fake.raids = [RaidRow(1, True, 631, 10, False, 101, started=1, ended=2),
                  RaidRow(2, False, 631, 10, False, 102, started=2)]
    assert fake.raid_at(999) is None
    assert fake.raid_at(1000).raid_id == 1 and fake.raid_at(2000).raid_id == 2


def test_fake_scripted_request_and_default_job(fake):
    async def scenario():
        fake.events["vocab"].extend([vocab("", 1), vocab("other", 0), vocab("", 0)])
        rows = await fake.request(p.GetVocab("a", classes=(1,)), "vocab",
                                  until=lambda e: e.class_ == 0, timeout_s=60)
        assert [e.class_ for e in rows] == [1, 0]
        result = await fake.wait_job("a", 1)
        assert (result.id, result.kind, result.state, result.skipped) == ("a", "get_vocab", "done", ())
    asyncio.run(scenario())


def test_metric_bases_survive_storage_without_disabling_count_time_metrics(fake):
    rec = record()
    rec.header = dataclasses.replace(rec.header, damage_basis="requested_pre_ai",
        healing_basis="effective_gain_with_pre_absorb_raw", spell_basis="best_effort_thread_pair",
        interrupt_basis="successful_interrupt_spell_cast")
    s.put_pull(fake.conn, rec, context(), {101: "tank"}, received=3)
    row = s.pull_rows(fake.conn)[0]
    assert row["damage_basis"] == "requested_pre_ai"
    assert row["healing_basis"] == "effective_gain_with_pre_absorb_raw"
    assert row["spell_basis"] == "best_effort_thread_pair"
    assert row["interrupt_basis"] == "successful_interrupt_spell_cast"
    assert row["scoreable"] == 1  # death, cast, position and elapsed-time facts remain valid
    s.set_score(fake.conn, rec.header.id, 0.75)
    assert s.pull_rows(fake.conn)[0]["score"] == 0.75
    # A replay cannot silently upgrade provenance or revise its first receipt.
    replay = record()
    s.put_pull(fake.conn, replay, context(), {101: "tank"}, received=99)
    assert s.pull_rows(fake.conn)[0]["spell_basis"] == "best_effort_thread_pair"
    assert s.pull_rows(fake.conn)[0]["interrupt_basis"] == "successful_interrupt_spell_cast"
    legacy = record("2000-77-0")
    s.put_pull(fake.conn, legacy, context(), {101: "tank"}, received=4)
    old = s.pull_rows(fake.conn)[1]
    assert all(old[key] == "unknown" for key in (
        "damage_basis", "healing_basis", "spell_basis", "interrupt_basis"))
    s.put_dummy(fake.conn, dict(guid=101, round=0, seconds=30, dps=100, stock_dps=90,
                              ts=2, damage_basis="requested_pre_ai", spell_basis="best_effort_thread_pair"))
    s.put_dummy(fake.conn, dict(guid=102, round=0, seconds=30, dps=100, stock_dps=90, ts=2))
    assert [tuple(r) for r in fake.conn.execute("SELECT damage_basis,spell_basis FROM tg_dummy ORDER BY id")] == [
        ("requested_pre_ai", "best_effort_thread_pair"), ("unknown", "unknown")]
