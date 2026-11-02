"""Synthetic evidence: transport completeness is not measurement coverage."""

import asyncio
from dataclasses import replace
import os
from pathlib import Path

import pytest

from leeroy_mind.training import protocol as p, store
from leeroy_mind.training.checks import CHECKS_VERSION, rescore, run_checks
from leeroy_mind.training.ledger import PullContext, PullRecord
from leeroy_mind.training.mechanics import BOSS_MECHANICS, NO_BEHIND, watch_names


class Book:
    def __init__(self):
        names = sorted({name for rows in BOSS_MECHANICS.values() for row in rows
                        for name in (*row.names, *row.exempt)})
        self.names = {name.casefold(): i + 1 for i, name in enumerate(names)}

    def ids(self, name):
        value = self.names.get(name.casefold())
        return frozenset((value,)) if value else frozenset()

    def id(self, name):
        return next(iter(self.ids(name)))


@pytest.fixture
def book():
    return Book()


def member(guid=1101, **kw):
    return p.PullMember(**(dict(ts=0, pull="1000-77-0", guid=guid, class_=8, spec_tab=0,
        tank=False, ilvl=232, damage=0, damage_boss=0, healing=0, overheal=0, taken=0,
        deaths=0, interrupts=0, active_ms=0, done=(), taken_by=(), debuffs=(), casts=()) | kw))


def death(guid=1101, at_ms=5000, **kw):
    return p.PullDeath(**(dict(ts=0, pull="1000-77-0", guid=guid, at_ms=at_ms,
        spell=0, source=0, x=0, y=0, hits=()) | kw))


def track(seconds=range(20), *, x=0, y=0, hp=100, power=100, target=0, o=0):
    seconds = list(seconds)
    return dict(at_s=seconds, xs=[x] * len(seconds), ys=[y] * len(seconds),
        hp=[hp] * len(seconds), power=[power] * len(seconds),
        target=[target] * len(seconds), o=[o] * len(seconds))


def record(*members, boss=0, map_id=631, seconds=20, deaths=(), tracks=None, casts=(), **kw):
    h = p.Pull(**(dict(ts=1000 + seconds * 1000, id=f"1000-77-{boss}", map=map_id,
        instance_id=77, boss_id=boss, difficulty=0, size=10, outcome="killed", start_ms=1000,
        end_ms=1000 + seconds * 1000, members=len(members), deaths=len(deaths),
        tracks=len(tracks or {}), assisted=False, source="tracked", boss_entry=36612,
        upd_mean_ms=20, damage_basis="requested_pre_ai", spell_basis="best_effort_thread_pair") | kw))
    return PullRecord(h, tuple(replace(m, pull=h.id) for m in members),
        tuple(replace(d, pull=h.id) for d in deaths), tracks or {}, casts, True)


def kinds(result, kind):
    return [f for f in result.failures if f.kind == kind]


def test_mechanic_entries_include_difficulty_variants():
    adds = [m for m in BOSS_MECHANICS[649, 1] if m.kind == "adds"][0]
    assert set(adds.entries) == {34815, 35262, 35263, 35264, 34826, 35270, 35271, 35272}
    assert 34813 not in adds.entries and 34825 not in adds.entries
    assert NO_BEHIND == {(631, 2), (631, 10), (649, 2)}
    assert watch_names() == ("Inoculated",)


def test_requested_damage_and_best_effort_spells_never_prove_avoid_or_adds(book):
    m = member(taken=100, taken_by=(p.TakenTotal(book.id("Coldflame"), 36612, 100, 7),))
    result = run_checks(record(m), {1101: "ranged"}, book)
    assert not kinds(result, "avoid")
    for source in (38508, 38598):
        m = member(taken=100, taken_by=(p.TakenTotal(0, source, 30, 7),))
        assert not kinds(run_checks(record(m, boss=3), {1101: "tank"}, book), "adds")


def test_marks_count_across_raid(book):
    members = tuple(member(g, debuffs=(p.DebuffTotal(book.id("Mark of the Fallen Champion"), 1, 1),))
                    for g in range(1101, 1105))
    result = run_checks(record(*members, boss=3), {m.guid: "tank" for m in members}, book)
    failures = kinds(result, "debuff")
    assert len(failures) == 1
    assert (failures[0].guid, failures[0].count, failures[0].key) == (0, 1, "Mark of the Fallen Champion")
    assert not kinds(run_checks(record(*members[:3], boss=3), {}, book), "debuff")


def test_debuff_stack_excess_and_role_filter(book):
    m = member(debuffs=(p.DebuffTotal(book.id("Instability"), 1, 7),))
    failures = kinds(run_checks(record(m, boss=11), {1101: "melee"}, book), "debuff")
    assert [(f.key, f.count) for f in failures] == [("Instability", 2)]


def test_absent_or_partial_watched_buff_cannot_prove_missing_stack(book):
    for stack in (0, 2, 3):
        debuffs = (p.DebuffTotal(book.id("Inoculated"), 1, stack),) if stack else ()
        rec = record(member(debuffs=debuffs), boss=4, seconds=160,
                     tracks={(1101, 0): track(range(160))})
        assert not kinds(run_checks(rec, {1101: "tank"}, book), "buff")


def test_melee_in_front_uses_orientation_and_tenths_of_a_yard(book):
    tracks = {(1101, 0): track(x=30), (0, 36612): track()}
    result = run_checks(record(member(), tracks=tracks), {1101: "melee"}, book)
    assert len(kinds(result, "behind")) == 1
    tracks[1101, 0] = track(x=-30)
    assert not kinds(run_checks(record(member(), tracks=tracks), {1101: "melee"}, book), "behind")
    tracks[1101, 0] = track(x=30)
    assert not kinds(run_checks(record(member(), boss=2, tracks=tracks), {1101: "melee"}, book), "behind")
    tracks[0, 36612] = track(o=314)
    assert not kinds(run_checks(record(member(), tracks=tracks), {1101: "melee"}, book), "behind")


def test_ranged_stacked_only_on_spread_boss(book):
    tracks = {(1101, 0): track(), (1102, 0): track(x=30)}
    roles = {1101: "ranged", 1102: "ranged"}
    rec = record(member(), member(1102), boss=3, tracks=tracks)
    assert {f.guid for f in kinds(run_checks(rec, roles, book), "spread")} == set(roles)
    assert not kinds(run_checks(replace(rec, header=replace(rec.header, boss_id=5)), roles, book), "spread")


@pytest.mark.parametrize("boss,spell,window", [(4, "Gas Spore", 12), (8, "Pact of the Darkfallen", 15)])
def test_spread_exempts_cast_window(book, boss, spell, window):
    roles = {1101: "ranged", 1102: "ranged"}
    tracks = {(1101, 0): track(), (1102, 0): track(x=100)}
    tracks[1102, 0]["xs"][:window] = [30] * window
    rec = record(member(), member(1102), boss=boss, tracks=tracks,
                 casts=(p.TrackCast(book.id(spell), 0),))
    assert not kinds(run_checks(rec, roles, book), "spread")
    tracks[1102, 0]["xs"] = [30] * 20
    assert len(kinds(run_checks(rec, roles, book), "spread")) == 2


def test_aggro_and_healer_oom_require_consecutive_observed_living_seconds(book):
    tracks = {(1101, 0): track(range(12), power=9),
              (0, 36612): track(range(12), target=1101)}
    rec = record(member(), tracks=tracks)
    result = run_checks(rec, {1101: "healer"}, book)
    assert len(kinds(result, "aggro")) == len(kinds(result, "healer_oom")) == 1
    for data in tracks.values():
        data["at_s"] = list(range(0, 24, 2))
    rec = replace(rec, header=replace(rec.header, end_ms=25000))
    result = run_checks(rec, {1101: "healer"}, book)
    assert not kinds(result, "aggro") and not kinds(result, "healer_oom")


def test_dead_samples_break_oom_and_aggro(book):
    tracks = {(1101, 0): track(range(12), power=9, hp=0),
              (0, 36612): track(range(12), target=1101)}
    result = run_checks(record(member(), tracks=tracks), {1101: "healer"}, book)
    assert not kinds(result, "aggro") and not kinds(result, "healer_oom")


def test_tracks_join_by_second_and_missing_tracks_do_not_prove_position(book):
    tracks = {(1101, 0): track([1], x=30), (0, 36612): track([9])}
    result = run_checks(record(member(), tracks=tracks), {1101: "melee"}, book)
    assert not kinds(result, "behind")


def test_sparse_positions_use_possible_alive_time_not_observed_denominator(book):
    tracks = {(1101, 0): track([1], x=30), (0, 36612): track([1])}
    result = run_checks(record(member(), tracks=tracks), {1101: "melee"}, book)
    assert not kinds(result, "behind")
    assert not result.complete and "behind:tracks:1101" in result.coverage
    tracks = {(1101, 0): track(range(7), x=30), (0, 36612): track(range(7))}
    result = run_checks(record(member(), tracks=tracks), {1101: "melee"}, book)
    assert len(kinds(result, "behind")) == 1
    assert not result.complete


def test_positional_thresholds_are_strict_and_side_is_not_front(book):
    tracks = {(1101, 0): track(x=-30), (0, 36612): track()}
    tracks[1101, 0]["xs"][:6] = [30] * 6
    assert not kinds(run_checks(record(member(), tracks=tracks), {1101: "melee"}, book), "behind")
    tracks[1101, 0] = track(y=30)
    assert not kinds(run_checks(record(member(), tracks=tracks), {1101: "melee"}, book), "behind")


def test_fractional_tail_does_not_supply_a_whole_second(book):
    tracks = {(1101, 0): track(range(10), power=0), (0, 36612): track(range(10), target=1101)}
    rec = record(member(), seconds=9.5, tracks=tracks)
    assert not kinds(run_checks(rec, {1101: "healer"}, book), "healer_oom")


def test_missing_timestamps_and_unresolved_exemptions_are_unavailable(book):
    tracks = {(1101, 0): track(), (1102, 0): track(x=30)}
    del tracks[1101, 0]["at_s"]
    result = run_checks(record(member(), member(1102), boss=3, tracks=tracks),
                        {1101: "ranged", 1102: "ranged"}, book)
    assert not kinds(result, "spread") and not result.complete
    tracks[1101, 0] = track()
    book.names.pop("gas spore")
    result = run_checks(record(member(), member(1102), boss=4, tracks=tracks),
                        {1101: "ranged", 1102: "ranged"}, book)
    assert not kinds(result, "spread") and "spread:exemption_coverage" in result.coverage


def test_tank_range_requires_observed_healers_at_death(book):
    roles = {1101: "tank", 1102: "healer"}
    tracks = {(1102, 0): track(x=401)}
    rec = record(member(), member(1102), deaths=(death(),), tracks=tracks)
    assert len(kinds(run_checks(rec, roles, book), "tank_range")) == 1
    tracks[1102, 0] = track(x=400)
    assert not kinds(run_checks(rec, roles, book), "tank_range")


def test_twenty_five_man_needs_two_observed_healers(book):
    roles = {1101: "tank", 1102: "healer", 1103: "healer"}
    tracks = {(1102, 0): track(x=400), (1103, 0): track(x=401)}
    rec = record(member(), member(1102), member(1103), deaths=(death(),), tracks=tracks,
                 size=25, difficulty=1)
    assert len(kinds(run_checks(rec, roles, book), "tank_range")) == 1
    tracks[1103, 0] = track(x=400)
    assert not kinds(run_checks(rec, roles, book), "tank_range")
    tracks.clear()
    assert not kinds(run_checks(rec, roles, book), "tank_range")


def test_untrusted_last_hit_cannot_establish_death_cause_or_impact(book):
    deaths = (death(spell=book.id("Coldflame"), source=36612),
              death(at_ms=6000, source=38508))
    result = run_checks(record(member(deaths=2), deaths=deaths), {1101: "ranged"}, book)
    assert result.causes == ("unknown", "unknown")
    assert all(f.deaths == f.damage_share == 0 for f in result.failures)


@pytest.mark.parametrize("change", ["incomplete", "aborted", "nonfight"])
def test_ineligible_pull_never_creates_failures(book, change):
    rec = record(member(debuffs=(p.DebuffTotal(book.id("Impaled"), 5, 1),)))
    if change == "incomplete":
        rec = replace(rec, complete=False)
    else:
        rec = replace(rec, header=replace(rec.header, **({"outcome": "aborted"} if change == "aborted" else {"map": 0})))
    assert run_checks(rec, {1101: "tank"}, book).failures == ()


def test_missing_spell_name_does_not_match_unknown_spell_zero(book):
    book.names.clear()
    rec = record(member(debuffs=(p.DebuffTotal(0, 99, 99),)))
    assert not run_checks(rec, {1101: "tank"}, book).failures


def test_failed_mechanics_is_distinct_observed_lower_bound(book):
    m = member(debuffs=(p.DebuffTotal(book.id("Impaled"), 5, 1),))
    tracks = {(1101, 0): track(x=30), (0, 36612): track(target=1101)}
    result = run_checks(record(m, tracks=tracks), {1101: "melee"}, book)
    assert result.failed_mechanics == 2
    assert {f.kind for f in result.failures} == {"behind", "debuff", "aggro"}
    assert not result.complete and "avoid:unverified_damage" in result.coverage
    assert result.version == CHECKS_VERSION


def test_generic_only_observation_does_not_claim_boss_mechanic_coverage(book):
    tracks = {(1101, 0): track(x=-30), (0, 36612): track()}
    result = run_checks(record(member(), boss=0, map_id=533, tracks=tracks), {1101: "melee"}, book)
    assert not result.complete and result.coverage == ("mechanics:generic_only",)
    assert result.failed_mechanics == 0


def test_capped_aura_table_does_not_prove_absent_debuff_pass(book):
    m = member(debuffs=tuple(p.DebuffTotal(1000 + i, 1, 1) for i in range(24)))
    result = run_checks(record(m), {1101: "tank"}, book)
    assert "debuff:capped:1101:Impaled" in result.coverage
    assert not kinds(result, "debuff")


def test_rescore_replays_stored_records_and_skips_ineligible(fake, book):
    rec = record(member(debuffs=(p.DebuffTotal(book.id("Impaled"), 5, 1),)))
    ctx = PullContext("offline", 0, None, None, None, "", {})
    store.put_pull(fake.conn, rec, ctx, {1101: "tank"}, received=1)
    assert asyncio.run(rescore(fake, book)) == 1
    row = store.pull_rows(fake.conn)[0]
    assert row["checks_version"] == CHECKS_VERSION
    assert row["checks_complete"] == 0
    assert fake.conn.execute("SELECT count FROM tg_failure").fetchone()[0] == 3
    assert asyncio.run(rescore(fake, book)) == 0
    assert asyncio.run(rescore(fake, book, since_ms=2000)) == 0


def test_rescore_keeps_unscoreable_rows_unchecked(fake, book):
    ctx = PullContext("offline", 0, None, None, None, "", {})
    rec = record(member())
    store.put_pull(fake.conn, rec, ctx, {1101: "tank"}, received=1, scoreable=False)
    assert asyncio.run(rescore(fake, book)) == 0
    assert store.pull_rows(fake.conn)[0]["checks_version"] == 0


@pytest.mark.live
def test_mechanic_names_resolve():
    from leeroy_mind.training.spells import load_spellbook
    directory = os.environ.get("LEEROY_TEST_DBC_DIR")
    if not directory:
        pytest.skip("Set LEEROY_TEST_DBC_DIR for the explicit read-only DBC check")
    book = load_spellbook(Path(directory))
    names = {name for rows in BOSS_MECHANICS.values() for row in rows for name in (*row.names, *row.exempt)}
    assert not sorted(name for name in names if not book.ids(name))
