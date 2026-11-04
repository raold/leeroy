import asyncio
import dataclasses
import json
from types import SimpleNamespace

import pytest

from leeroy_mind.training import store
from leeroy_mind.training.score import (
    adjusted, bootstrap_diff, context_stats, is_farm, pull_score, score_pull, standardize,
)
from .test_tg_store import context, record


def scored(ident, score=0.0, **changes):
    return dict(id=str(ident), score=score, complete=1, scoreable=1, outcome="killed",
                map=631, boss_id=0, start_ms=1000, end_ms=301000, champion=0, score_basis="deaths_time",
                covariates=dict(ilvl=250, healers=2, drunk=0, flask_skips=0,
                                human_in_raid=0, assisted=0)) | changes


def test_score_formula():
    assert pull_score("wiped", 9, 9, 999, 300) == -100
    assert pull_score("killed", 2, 3, 330, 300) == pytest.approx(-25)
    assert pull_score("aborted", 0, 0, 1, 1) is None
    assert pull_score("killed", 0, 0, 300, None) == 0


@pytest.mark.parametrize("duration,median", [(0, None), (-1, 300), (300, 0), (float("nan"), 300)])
def test_score_rejects_invalid_durations(duration, median):
    assert pull_score("killed", 0, 0, duration, median) is None


def test_farm_rule():
    assert is_farm(["killed"] * 3)
    assert not is_farm(["killed"] * 2)
    assert is_farm(["wiped"] * 2 + ["killed"] * 8)
    assert not is_farm(["killed"] * 9 + ["wiped"])
    assert not is_farm(["wiped"] * 3 + ["killed"] * 7)


def test_covariate_adjustment_removes_a_gear_shift():
    rows = [scored(i, 2 * (i - 15)) for i in range(30)]
    for i, row in enumerate(rows):
        row["covariates"]["ilvl"] += i - 15
    out = adjusted(rows)
    assert list(out.values()) == pytest.approx([-1] * 30)
    assert adjusted(rows[:19]) == {r["id"]: r["score"] for r in rows[:19]}


def test_adjustment_includes_his_presence_and_aids():
    rows = [scored(i, -20 * (i % 2) + 8 * (i % 3)) for i in range(30)]
    for i, row in enumerate(rows):
        row["covariates"].update(human_in_raid=i % 2, assisted=i % 3)
    assert list(adjusted(rows).values()) == pytest.approx([-2] * 30)
    for row in rows:
        row["covariates"]["assisted"] = 0
        row["score"] = -20 * row["covariates"]["human_in_raid"]
    assert list(adjusted(rows).values()) == pytest.approx([-10] * 30)


def test_missing_covariates_and_singular_fits_keep_raw_scores():
    rows = [scored(i, i) for i in range(30)]
    for i, row in enumerate(rows):
        row["covariates"].update(ilvl=250 + i, healers=i)
    raw = {r["id"]: r["score"] for r in rows}
    assert adjusted(rows) == raw
    rows[0]["covariates"]["drunk"] = "unknown"
    assert adjusted(rows) == raw
    del rows[0]["covariates"]["drunk"]
    assert adjusted(rows) == raw


def test_standardize_floors_sd_and_prefers_champion():
    rows = [scored(i, 20, champion=1) for i in range(3)] + [scored(3, -100)]
    stats = context_stats(rows)
    assert (stats.n, stats.kills, stats.median_kill_s, stats.mean, stats.sd) == (3, 3, 300, 20, 10)
    assert standardize(30, stats) == 1
    assert context_stats(rows[1:]).n == 3
    assert context_stats([]).median_kill_s is None


def test_statistics_exclude_unscoreable_and_nonfight_rows():
    rows = [scored(0)] + [scored(i + 1, -100) | bad for i, bad in enumerate([
        dict(complete=0), dict(scoreable=0), dict(outcome="aborted"), dict(boss_id=13), dict(score=None),
    ])]
    assert context_stats(rows).n == 1
    assert adjusted(rows) == {"0": 0}


def test_bootstrap_is_seeded_and_brackets():
    result = bootstrap_diff([1, 2, 3], [10, 11, 12], seed="2026-W44:631:0:0")
    assert result == bootstrap_diff([1, 2, 3], [10, 11, 12], seed="2026-W44:631:0:0")
    mean, low, high = result
    assert 0 < low <= mean <= high
    with pytest.raises(ValueError):
        bootstrap_diff([], [1], seed="empty")


def test_score_pull_persists_independent_observations_and_is_idempotent(fake):
    rec, ctx = record(), context()
    store.put_pull(fake.conn, rec, ctx, {101: "tank"}, received=10)
    result = SimpleNamespace(failures=(), causes=("unknown",), failed_mechanics=0)
    assert asyncio.run(score_pull(fake, rec, ctx, result)) == -8
    row = store.pull_rows(fake.conn)[0]
    assert row["score"] == -8 and row["checks_version"] >= 1
    assert asyncio.run(score_pull(fake, rec, ctx, result)) == -8


@pytest.mark.parametrize("bad", [dict(complete=False), dict(outcome="aborted"), dict(boss=13)])
def test_score_pull_never_scores_ineligible_records(fake, bad):
    rec, ctx = record(**bad), context()
    store.put_pull(fake.conn, rec, ctx, {101: "tank"}, received=10)
    result = SimpleNamespace(failures=(), causes=("unknown",), failed_mechanics=0)
    assert asyncio.run(score_pull(fake, rec, ctx, result)) is None
    assert store.pull_rows(fake.conn)[0]["score"] is None


def test_score_pull_honors_ledger_veto(fake):
    rec, ctx = record(), context()
    store.put_pull(fake.conn, rec, ctx, {101: "tank"}, received=10, scoreable=False)
    result = SimpleNamespace(failures=(), causes=("unknown",), failed_mechanics=0)
    assert asyncio.run(score_pull(fake, rec, ctx, result)) is None


def test_adjusted_accepts_stored_json_covariates():
    row = scored(1)
    row["covariates"] = json.dumps(row["covariates"])
    assert adjusted([row]) == {"1": 0}


def test_statistical_helpers_never_mix_score_bases():
    rows = [scored(i, i) for i in range(30)]
    rows[0]["score_basis"] = "mechanics_deaths_time"
    with pytest.raises(ValueError, match="one score basis"):
        context_stats(rows)
    assert adjusted(rows) == {r["id"]: r["score"] for r in rows}
    assert context_stats([scored(0, score_basis="unknown")]).n == 0


@pytest.mark.parametrize("complete,expected,basis", [
    (False, -8, "deaths_time"), (True, -14, "mechanics_deaths_time"),
])
def test_partial_mechanics_are_stored_but_not_penalized(fake, complete, expected, basis):
    rec, ctx = record(), context()
    store.put_pull(fake.conn, rec, ctx, {101: "tank"}, received=10)
    result = SimpleNamespace(failures=(), causes=("unknown",), failed_mechanics=3,
                             complete=complete, coverage=() if complete else ("positions_missing",), version=1)
    assert asyncio.run(score_pull(fake, rec, ctx, result)) == expected
    row = store.pull_rows(fake.conn)[0]
    assert row["failed_mechanics"] == 3 and row["score_basis"] == basis
    assert bool(row["checks_complete"]) is complete
    assert json.loads(row["checks_coverage"]) == list(result.coverage)


def test_score_reference_uses_prior_same_basis_champion_pulls(fake):
    trial = store.open_trial(fake.conn, "631:0:0", "tactic", "raid", opened=0,
                             arms=[dict(champion=True), dict(champion=False)])
    arms = [r[0] for r in fake.conn.execute("SELECT id FROM tg_arm WHERE trial=? ORDER BY id", (trial,))]
    for i in range(5):
        rec = record(f"{1000 + i}-77-0", end=301000)
        rec.header = dataclasses.replace(rec.header, start_ms=1000 + i,
                                         end_ms=1000 + i + (100000 if i < 3 else 900000))
        ctx = context(arm_id=arms[0 if i < 3 else 1])
        store.put_pull(fake.conn, rec, ctx, {101: "tank"}, received=10 + i)
        store.set_score(fake.conn, rec.header.id, -8,
                         basis="mechanics_deaths_time" if i == 4 else "deaths_time")
    rec, ctx = record("2000000-77-0"), context()
    rec.header = dataclasses.replace(rec.header, start_ms=2000000, end_ms=2110000)
    store.put_pull(fake.conn, rec, ctx, {101: "tank"}, received=20)
    result = SimpleNamespace(failures=(), causes=("unknown",), failed_mechanics=0)
    assert asyncio.run(score_pull(fake, rec, ctx, result)) == pytest.approx(-11)


def test_new_check_version_resets_score_and_recomputes_basis(fake):
    rec, ctx = record(), context()
    store.put_pull(fake.conn, rec, ctx, {101: "tank"}, received=10)
    partial = SimpleNamespace(failures=(), causes=("unknown",), failed_mechanics=3,
                              complete=False, coverage=("positions_missing",), version=1)
    full = SimpleNamespace(failures=(), causes=("unknown",), failed_mechanics=3,
                           complete=True, coverage=(), version=2)
    assert asyncio.run(score_pull(fake, rec, ctx, partial)) == -8
    assert asyncio.run(score_pull(fake, rec, ctx, full)) == -14
    assert asyncio.run(score_pull(fake, rec, ctx, partial)) == -14
    assert store.pull_rows(fake.conn)[0]["score_basis"] == "mechanics_deaths_time"
