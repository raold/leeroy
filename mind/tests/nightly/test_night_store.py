"""Nightly settings and the [life] dial, the night/week/season helpers, the P1c tables, unit
commits, the persona ledger and the memory scope as a column (P1c Task 1)."""
import logging
import sqlite3
from datetime import datetime, timezone

import pytest

from conftest import make_mem
from leeroy_mind.config import ConfigError, load_config
from leeroy_mind.memory.recall import PRIVATE_TAIL, format_memory
from leeroy_mind.nightly.settings import (is_season_end, is_sunday, iso_week, load_settings,
                                          night_bounds, night_of_ts, season_of, week_bounds)
from leeroy_mind.nightly.store import (active_away, commit_unit, last_window_end, ledger_add,
                                       ledger_rows, run_begin, run_finish, step_done)

UTC = timezone.utc


def test_night_and_week_helpers():
    assert night_of_ts(datetime(2026, 10, 30, 5, 59, tzinfo=UTC).timestamp(), UTC) == "2026-10-29"
    start, end = night_bounds("2026-10-29", UTC)
    assert end - start == 86400 and night_of_ts(start, UTC) == "2026-10-29"
    assert iso_week("2026-11-01") == "2026-W44" and is_sunday("2026-11-01") and not is_sunday("2026-10-29")
    assert week_bounds("2026-W44", UTC)[0] == datetime(2026, 10, 26, 6, tzinfo=UTC).timestamp()


def test_season_helpers():
    assert season_of("2026-11-01", 8) == "2026-S6" and season_of("2026-10-04", 8) == "2026-S5"
    assert is_season_end("2026-10-04", 8) and not is_season_end("2026-10-11", 8)       # W40 ends a season, W41 does not
    assert not is_season_end("2026-10-03", 8)                                           # a Saturday
    assert is_season_end("2027-01-03", 8) and season_of("2027-01-03", 8) == "2026-S7"   # the Sunday of 2026's last ISO week (W53)


def test_settings_from_config(tmp_path):
    s = load_settings(load_config(tmp_path / "missing.toml"), tmp_path)
    assert (s.relationship_speed, s.romance_dial, s.jealousy, s.encounters_per_night, s.wow_vs_life) == (10, 3, 5, 150, 5)
    assert (s.reflection_model, s.live_model, s.data_dir) == (
        "qwen3.8:27b-q4_K_M", "hf.co/bartowski/TheDrummer_Orion-26B-A4B-v1.1-GGUF:IQ4_XS", tmp_path)
    assert (s.parallel, s.live_rumors, s.probe_max_rate) == (2, 3, 0.1)


def test_life_section(tmp_path, caplog):
    toml = tmp_path / "mind.toml"
    toml.write_text("[life]\nwow_vs_life = 0\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        s = load_settings(load_config(toml), tmp_path)
    assert s.wow_vs_life == 0 and "life" not in caplog.text          # P1b's loader skips the section
    for bad in ("11", "true", '"5"'):
        toml.write_text(f"[life]\nwow_vs_life = {bad}\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="life.wow_vs_life"):
            load_settings(load_config(toml), tmp_path)


def test_runs_resume_with_original_window(h):
    r = run_begin(h.store, "2026-10-29", 0.0, 100.0, now=100.0)
    r2 = run_begin(h.store, "2026-10-29", 0.0, 500.0, now=500.0)
    assert (r2.attempts, r2.window_end, r2.status) == (2, 100.0, "running") and last_window_end(h.store) is None
    run_finish(h.store, "2026-10-29", now=600.0, status="done")
    assert last_window_end(h.store) == 100.0


def test_unit_commit_is_atomic_and_joins_store_writes(h):
    def boom(store):
        store.add_rel_delta(101, 102, "trust", 1, "t", ["memory:1"], 1, applied=True)   # a Store write that commits on its own
        raise RuntimeError
    with pytest.raises(RuntimeError):
        commit_unit(h.store, "n", "s", "u", boom, now=1.0)
    assert h.count("rel_delta") == 0 and not step_done(h.store, "n", "s", "u")
    commit_unit(h.store, "n", "s", "u", lambda s: s.add_rel_delta(101, 102, "trust", 1, "t", ["memory:1"], 1, applied=True), now=1.0)
    assert h.count("rel_delta") == 1 and step_done(h.store, "n", "s", "u")


def test_only_one_member_away(h):
    with h.store.transaction():
        h.store.conn.execute("INSERT INTO away(guid, reason, left_at, returns_at) VALUES (101, 'x', 0, 1)")
    with pytest.raises(sqlite3.IntegrityError), h.store.transaction():
        h.store.conn.execute("INSERT INTO away(guid, reason, left_at, returns_at) VALUES (102, 'y', 0, 1)")
    assert active_away(h.store) == {101}


def test_ledger_rows(h):
    with h.store.transaction():
        a = ledger_add(h.store, guid=101, kind="note_added", text="I lead when nobody else will.", evidence=["memory:1"], now=1.0)
        ledger_add(h.store, guid=102, kind="stance_band", text="drawn -> lust: act flirt", evidence=["chat:3"], now=2.0)
        ledger_add(h.store, guid=101, kind="undo", text="", evidence=[], ref=a, now=3.0)
        ledger_add(h.store, guid=103, kind="stance_band", text="fond -> drawn: hand move", evidence=[], now=4.0)
        ledger_add(h.store, guid=103, kind="stance_band", text="drawn -> lust: act flirt", evidence=["chat:4"], now=4.0)
    assert [r["kind"] for r in ledger_rows(h.store, 101)] == ["undo", "note_added"] and len(ledger_rows(h.store)) == 5
    assert [r["text"] for r in ledger_rows(h.store, 103)] == ["drawn -> lust: act flirt", "fond -> drawn: hand move"]   # a tie: latest written first (B4)


def test_vector_tables_replace_the_old_ones(h):
    names = {r[0] for r in h.store.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"fact", "thinks_knows", "rumor", "lexicon", "bandit", "pad_mood", "motive", "volition",
            "attraction_state", "probe_run", "night_stage"} <= names and not {"joke", "nickname"} & names
    with pytest.raises(sqlite3.IntegrityError), h.store.transaction():
        h.store.conn.execute("INSERT INTO knows(root, guid, how, learned) VALUES ('chat:1', 101, 'saw', 0)")   # revision 2's 'saw' is gone


def test_scope_keeps_memory_text_within_200(h):                    # controller rulings C2 and N4: a column, not a mark
    text = "x" * 200
    mid = h.store.insert_memory(make_mem(owner=101, text=text, scope="secret", scope_with=102))
    (m,) = h.store.get_memories([mid])
    assert (m.scope, m.scope_with, m.text) == ("secret", 102, text)
    assert format_memory(m, h.T, tz=timezone.utc).endswith(text + PRIVATE_TAIL)    # what P1b's prompt shows
    (plain,) = h.store.get_memories([h.mem(101, "Fished in Dalaran.")])
    assert (plain.scope, plain.scope_with) == ("open", 0)
    assert not format_memory(plain, h.T, tz=timezone.utc).endswith(PRIVATE_TAIL)
    with pytest.raises(sqlite3.IntegrityError), h.store.transaction():
        h.store.conn.execute("UPDATE memory SET scope = 'hidden' WHERE id = ?", (mid,))
