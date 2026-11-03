"""Independent boundary and preservation checks for the guild foundation."""

import itertools
import shutil
import sqlite3
from datetime import datetime

import pytest

from conftest import make_mem
from leeroy_mind.config import ConfigError
from leeroy_mind.guild.config import DkpConfig, load_guild_config
from leeroy_mind.guild.dkp import Award, Bidder, LedgerEntry, apply_due_decay, award
from leeroy_mind.guild.store import (
    add_ledger, attendance_counts, balances, bench_streaks, last_led, last_run,
    new_raid, record_attendance,
)
from leeroy_mind.storage import db as db_mod
from leeroy_mind.storage.rows import pack_vec
from leeroy_mind.storage.store import Store
from leeroy_mind.testing.fake_llm import fake_embed


def _raid(conn, started, **overrides):
    values = dict(started=started, raid_key="icc", size=10, heroic=False,
                  offline=False, lead_guid=101, tank_guid=102, loot_guid=103)
    values.update(overrides)
    return new_raid(conn, **values)


@pytest.mark.parametrize(("section", "key"), [
    ("guild", "friends_add"), ("guild", "friends_remove"),
    ("dkp", "kill"), ("dkp", "decay"),
])
def test_review_unrepresentable_number_is_a_named_config_error(tmp_path, section, key):
    path = tmp_path / "mind.toml"
    path.write_text(f"[{section}]\n{key} = {'9' * 400}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match=rf"{section}\.{key}"):
        load_guild_config(path)


def test_review_offline_window_requires_parseable_ascii_clock_digits(tmp_path):
    path = tmp_path / "mind.toml"
    path.write_text('[guild]\noffline_window = ["0\u0661:3\u0664", "18:00"]\n', encoding="utf-8")
    with pytest.raises(ConfigError, match=r"guild\.offline_window"):
        load_guild_config(path)


def test_review_attendance_history_uses_time_then_id_and_skips_absences(fake):
    # Imported historical rows arrive after the latest run; their IDs are not chronology.
    newest = _raid(fake.conn, 30)
    oldest = _raid(fake.conn, 10)
    middle = _raid(fake.conn, 20)
    absent = _raid(fake.conn, 40)
    record_attendance(fake.conn, newest, [], [101, 102])
    record_attendance(fake.conn, oldest, [101], [102])
    record_attendance(fake.conn, middle, [], [101, 102])
    record_attendance(fake.conn, absent, [103], [])
    assert bench_streaks(fake.conn, [101, 102, 104]) == {101: 2, 102: 3, 104: 0}
    assert attendance_counts(fake.conn, [101, 102, 103], 10) == {101: 1, 102: 0, 103: 1}
    assert last_run(fake.conn) == {"icc10": 40}
    assert last_led(fake.conn) == {101: 40}

    # Equal starts have a stable newest-ID tie break.
    tie = _raid(fake.conn, 30, offline=True, lead_guid=104)
    record_attendance(fake.conn, tie, [101], [])
    assert bench_streaks(fake.conn, [101, 102]) == {101: 0, 102: 3}
    assert 104 not in last_led(fake.conn)


def test_review_generator_failure_rolls_back_all_new_ledger_entries(fake):
    add_ledger(fake.conn, [LedgerEntry(1, 101, 20, "adjust")])

    def incomplete_awards():
        yield LedgerEntry(2, 101, 10, "kill")
        yield LedgerEntry(2, 102, 10, "kill")
        raise RuntimeError("synthetic producer failure")

    with pytest.raises(RuntimeError, match="synthetic producer failure"):
        add_ledger(fake.conn, incomplete_awards())
    assert balances(fake.conn) == {101: 20}
    assert fake.conn.execute("SELECT count(*) FROM dkp_ledger").fetchone()[0] == 1


def test_review_decay_receipt_and_duplicate_awards_survive_reopen(fake, tmp_path):
    entry = LedgerEntry(1, 101, 50, "adjust")
    add_ledger(fake.conn, [entry, entry])
    apply_due_decay(fake.conn, datetime(2026, 11, 2, 4), DkpConfig())
    assert apply_due_decay(fake.conn, datetime(2026, 11, 16, 4), DkpConfig()) == 2
    other = db_mod.open_db(tmp_path / "mind.db")
    try:
        assert balances(other) == {101: 81}
        assert other.execute("SELECT count(*) FROM dkp_ledger WHERE reason='adjust'").fetchone()[0] == 2
        assert apply_due_decay(other, datetime(2026, 11, 9, 4), DkpConfig()) == 0
        assert apply_due_decay(other, datetime(2026, 11, 16, 4), DkpConfig()) == 0
        assert balances(other) == {101: 81}
    finally:
        other.close()


def test_review_award_is_permutation_invariant_across_all_tiebreaks():
    bidders = [Bidder(101, 100, 10, False), Bidder(102, 120, 10, False),
               Bidder(103, 120, 10, False), Bidder(104, 1000, None, True)]
    expected = Award(102, 45, 45, 1, 103, 45, True)
    for order in itertools.permutations(bidders):
        assert award(30, order, human_claimed=False, human_guid=1) == expected


def test_review_fractional_balance_caps_payment_and_human_claim_ignores_debt():
    bidders = [Bidder(101, 7.5, 10, True), Bidder(102, 7.4, 10, True)]
    assert award(30, bidders, human_claimed=False, human_guid=1) == Award(
        101, 7.5, 7.5, 0, 102, 7.4, True)
    assert award(30, [Bidder(1, -500, None, False), *bidders],
                 human_claimed=True, human_guid=1) == Award(1, 30, 30, -1, 101, 7.5, False)


def _preserved_rows(conn):
    tables = ("meta", "memory", "memory_actor", "chat_line", "voice_presence",
              "voice_line", "voice_heard", "night_run", "night_step", "probe_run")
    rows = {table: tuple(tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1"))
            for table in tables}
    rows["memory_vec"] = tuple(tuple(row) for row in conn.execute(
        "SELECT memory_id,owner,embedding FROM memory_vec ORDER BY memory_id"))
    return rows


@pytest.mark.parametrize("fail_first", [False, True])
def test_review_guild_migration_preserves_voice_nightly_and_private_memory(
        tmp_path, monkeypatch, clock, fail_first):
    files = db_mod._migration_files()
    prior = tmp_path / "prior-migrations"
    prior.mkdir()
    for version, source in files:
        if version < 7:
            shutil.copy(source, prior / source.name)
    path = tmp_path / "migration-review.db"
    with monkeypatch.context() as patch:
        patch.setattr(db_mod, "MIGRATIONS_DIR", prior)
        conn = db_mod.open_db(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 6
        with conn:
            conn.execute("INSERT INTO meta(key,value) VALUES ('review:preserve','synthetic')")
            conn.execute("INSERT INTO chat_line(ts,channel,from_guid,from_name,to_guid,text,origin) "
                         "VALUES (1,'whisper',1,'Testmage',101,'synthetic private note','human')")
            Store(conn, clock=clock).insert_memory(make_mem(
                owner=101, text="A synthetic confidence.", evidence=("chat:1",),
                scope="secret", scope_with=1, embedding=pack_vec(fake_embed("synthetic"))))
            conn.execute("INSERT INTO voice_presence(guid,room,since) VALUES (101,'guild',1)")
            conn.execute("INSERT INTO voice_line(speak_id,line_id,room,state,ts) "
                         "VALUES ('review.1',1,'whisper','done',2)")
            conn.execute("INSERT INTO voice_heard VALUES (1,'synthetic private note',0.9,500,100)")
            conn.execute("INSERT INTO night_run(night,window_start,window_end,status,started) "
                         "VALUES ('2026-11-01',1,2,'done',3)")
            conn.execute("INSERT INTO night_step VALUES ('2026-11-01','reflect','101',4)")
            conn.execute("INSERT INTO probe_run(ts,model,n,leaks,rate) "
                         "VALUES (5,'synthetic-model',36,0,0)")
        before = _preserved_rows(conn)
    finally:
        conn.close()

    if fail_first:
        broken = tmp_path / "broken-migrations"
        broken.mkdir()
        for version, source in files:
            if version <= 7:
                target = broken / source.name
                shutil.copy(source, target)
                if version == 7:
                    with target.open("a", encoding="utf-8") as out:
                        out.write("\nINSERT INTO nonexistent_review_table VALUES (1);\n")
        with monkeypatch.context() as patch:
            patch.setattr(db_mod, "MIGRATIONS_DIR", broken)
            with pytest.raises(sqlite3.OperationalError, match="nonexistent_review_table"):
                db_mod.open_db(path)
            patch.setattr(db_mod, "MIGRATIONS_DIR", prior)
            conn = db_mod.open_db(path)
            try:
                assert conn.execute("PRAGMA user_version").fetchone()[0] == 6
                assert _preserved_rows(conn) == before
                assert conn.execute("SELECT count(*) FROM sqlite_master WHERE name IN "
                                    "('guild_raid','guild_attendance','dkp_ledger','guild_flag')").fetchone()[0] == 0
            finally:
                conn.close()

    conn = db_mod.open_db(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db_mod.latest_version()
        assert _preserved_rows(conn) == before
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name IN "
                            "('guild_raid','guild_attendance','dkp_ledger','guild_flag')").fetchone()[0] == 4
        assert conn.execute("SELECT memory_id FROM memory_vec WHERE embedding MATCH ? "
                            "AND k=1 AND owner=101", (pack_vec(fake_embed("synthetic")),)).fetchone()[0] == 1
    finally:
        conn.close()
