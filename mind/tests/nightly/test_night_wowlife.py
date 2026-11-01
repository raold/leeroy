"""Hours, pressure and the grounded talk they cause, using invented members only."""
import json
import random
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import date, timedelta, timezone

import pytest

from conftest import run
from nightly.conftest import no_real_name, put_wow, yes
from leeroy_mind.nightly import wowlife
from leeroy_mind.nightly.settings import night_bounds
from leeroy_mind.nightly.store import active_away
from leeroy_mind.nightly.wowlife import (WowLife, band, get_wow_life, human_hours,
    member_hours, next_state, on_mind, put_wow_life, refresh_on_mind, stage_wowlife)


def quiet(ctx, **settings):
    ctx.settings = replace(ctx.settings, wow_lines_per_night=0, notice_members=0, **settings)
    return ctx


def line_calls(ctx):
    return [c for c in ctx.reasoner.llm.calls if "wow_line" in json.dumps(c.get("format") or {})]


def add_hours(h, night, guid, hours):
    h.store.conn.execute("INSERT INTO wow_hours(night,guid,hours) VALUES (?,?,?)", (night, guid, hours))


def test_default_and_round_trip(h):
    assert get_wow_life(h.store, 101) == WowLife(101)
    row = WowLife(101, .4, .5, True, "one more pull", h.night, h.T)
    with h.store.transaction():
        put_wow_life(h.store, row)
    assert get_wow_life(h.store, 101) == row
    put_wow(h, 101, guilt=.7)
    assert get_wow_life(h.store, 101) == replace(row, guilt=.7)


def test_public_write_commits_standalone_and_joins_rollback(h):
    path = h.store.conn.execute("PRAGMA database_list").fetchone()[2]
    put_wow_life(h.store, WowLife(101, pressure=.4))
    with closing(sqlite3.connect(path)) as reader:
        assert reader.execute("SELECT pressure FROM wow_life WHERE guid=101").fetchone()[0] == .4
        with pytest.raises(RuntimeError):
            with h.store.transaction():
                put_wow_life(h.store, WowLife(101, pressure=.9))
                assert get_wow_life(h.store, 101).pressure == .9
                raise RuntimeError("synthetic outer rollback")
        assert reader.execute("SELECT pressure FROM wow_life WHERE guid=101").fetchone()[0] == .4
    assert get_wow_life(h.store, 101).pressure == .4


def test_human_hours_from_sessions(h):
    s1 = h.session(h.T - 3600, h.T + 2 * 3600)
    s2 = h.session(h.T + 2.5 * 3600, h.T + 5 * 3600)
    h.session(h.T - 30 * 3600, h.T - 28 * 3600)
    hours, ids = human_hours(h.store, night_bounds(h.night, timezone.utc))
    assert hours == pytest.approx(5.5)
    assert ids == [s1, s2]


def test_human_hours_clips_half_open_and_open_sessions(h):
    a, b = night_bounds(h.night, timezone.utc)
    h.session(a - 7200, a)  # touching the start is outside
    clipped = h.session(a - 3600, a + 3600)
    h.session(b, b + 3600)  # touching the end is outside
    opened = h.store.start_session(b - 7200)
    assert human_hours(h.store, (a, b)) == (3.0, [clipped, opened])
    assert human_hours(h.store, (b, a)) == (0.0, [])


def test_member_hours_escapism_night_owl_away_and_cap(h):
    p = h.persona(101, night_owl=True)
    base = member_hours(p, human_hours=4, away=False, rng=random.Random(8))
    extra = member_hours(p, human_hours=4, away=False, rng=random.Random(8), escapism=.6, pressure=.9)
    assert extra - base == pytest.approx(1.62)
    assert base == pytest.approx(4 + random.Random(8).uniform(1, 4) + 1.5)
    assert member_hours(p, human_hours=15, away=False, rng=random.Random(8), escapism=.9, pressure=1) == 16
    rng = random.Random(8)
    assert member_hours(p, human_hours=4, away=True, rng=rng, escapism=.9, pressure=1) == 4
    assert rng.random() == random.Random(8).random()  # no fictional time sampled while away


def test_pressure_rises_with_hours_and_eases_without():
    p = g = 0.0
    for _ in range(3):
        p, g = next_state(p, g, hours_week=60, ties=1.0, conscientious=True, dial=5)
    assert band(p, g) == "some"
    for _ in range(2):
        p, g = next_state(p, g, hours_week=60, ties=1.0, conscientious=True, dial=5)
    assert band(p, g) == "high"
    for _ in range(10):
        p, g = next_state(p, g, hours_week=10, ties=1.0, conscientious=True, dial=5)
    assert band(p, g) == "none"
    assert next_state(.5, .5, hours_week=60, ties=1, conscientious=True, dial=0) == pytest.approx((.4, .425))
    assert next_state(0, 0, hours_week=40, ties=1, conscientious=True, dial=5, escapism=1)[0] == pytest.approx(.15)
    assert next_state(1, 1, hours_week=100, ties=1, conscientious=True, dial=10, escapism=1) == (1, 1)
    assert next_state(0, 0, hours_week=20, ties=1, conscientious=True, dial=10) == (0, 0)


@pytest.mark.parametrize("pressure,guilt,expected", [(0, .299, "none"), (.3, 0, "some"),
    (0, .599, "some"), (.6, 0, "high"), (0, .8, "high")])
def test_band_boundaries(pressure, guilt, expected):
    assert band(pressure, guilt) == expected


def test_on_mind_composition_drops_empty_and_caps():
    assert on_mind("thesis", "too much raiding", "high") == "too much raiding; thesis"
    assert on_mind("thesis", "too much raiding", "some") == "thesis; too much raiding"
    assert on_mind("thesis", "too much raiding", "none") == "thesis"
    assert on_mind("", "too much raiding", "some") == "too much raiding"
    assert on_mind("thesis", "", "high") == "thesis"
    assert len(on_mind("x" * 180, "y" * 100, "some")) == 200


def test_refresh_uses_active_thread_and_preserves_other_state(h):
    h.persona(101, life_thread="original plan")
    original = replace(h.store.get_state(101), mood=.6, goals=("keep helping",), altered_state="tipsy")
    h.store.put_state(original)
    h.store.conn.execute("INSERT INTO life_thread(guid,title,status) VALUES (101,'new course','active')")
    put_wow(h, 101, pressure=.7, line="too much raiding")
    with h.store.transaction():
        refresh_on_mind(h.store, 101, dial=5)
    assert h.store.get_state(101) == replace(original, life_thread="too much raiding; new course", updated=h.T)
    with h.store.transaction():
        refresh_on_mind(h.store, 101, dial=0)
    assert h.store.get_state(101).life_thread == "new course"
    assert get_wow_life(h.store, 101).line == "too much raiding"


def test_dial_zero_no_lines_no_vows_and_upkeep_idempotent(h):
    h.persona(101, life_thread="thesis deadline")
    put_wow(h, 101, pressure=.9, guilt=.9, line="old line", vow=True)
    h.session(h.T - 3600, h.T + 8 * 3600)
    ctx = h.ctx()
    ctx.settings = replace(ctx.settings, wow_vs_life=0)
    run(stage_wowlife(ctx))
    assert not line_calls(ctx)
    assert h.store.get_state(101).life_thread == "thesis deadline"
    assert get_wow_life(h.store, 101).vow is False
    assert h.count("wow_hours") == 6 and h.count("trait_state") == 25 and h.count("motive") == 50
    assert not ctx.done("wowlife", "hours")
    before = {table: [tuple(r) for r in h.store.conn.execute(f"SELECT * FROM {table}")]
              for table in ("wow_hours", "wow_life", "trait_state", "motive", "persona_state", "night_step")}
    run(stage_wowlife(ctx))
    assert all([tuple(r) for r in h.store.conn.execute(f"SELECT * FROM {table}")] == rows
               for table, rows in before.items())
    assert not ctx.failures


def test_away_keeps_human_hours_without_fictional_time(h):
    h.session(h.T - 3600, h.T + 8 * 3600)
    h.store.conn.execute("INSERT INTO away(guid,reason,left_at,returns_at) VALUES (101,'work',?,?)", (h.T, h.T + 86400))
    ctx = quiet(h.ctx(), wow_vs_life=0)
    run(stage_wowlife(ctx))
    rows = dict(h.store.conn.execute("SELECT guid,hours FROM wow_hours WHERE night=?", (h.night,)))
    assert rows[101] == rows[1] == 9
    assert rows[102] > 9
    assert len(rows) == 6 and not ctx.failures


def test_state_uses_seven_recorded_nights_and_active_ties(h):
    h.persona(101, household="alone", job="artist", life_thread="new painting")
    h.store.conn.execute("INSERT INTO life_thread(guid,title,status) VALUES (101,'thesis deadline','active')")
    today = date.fromisoformat(h.night)
    for i in range(1, 7):
        add_hours(h, (today - timedelta(days=i)).isoformat(), 101, 8)
    add_hours(h, (today - timedelta(days=7)).isoformat(), 101, 1000)
    add_hours(h, (today + timedelta(days=1)).isoformat(), 101, 1000)
    ctx = quiet(h.ctx())
    run(stage_wowlife(ctx))
    current = h.store.conn.execute("SELECT hours FROM wow_hours WHERE night=? AND guid=101", (h.night,)).fetchone()[0]
    p, g = next_state(0, 0, hours_week=48 + current, ties=1, conscientious=True, dial=5, escapism=.3)
    assert get_wow_life(h.store, 101).pressure == pytest.approx(p)
    assert get_wow_life(h.store, 101).guilt == pytest.approx(g)
    assert not ctx.failures


def test_line_grounded_and_composed_on_mind(h):
    h.persona(101, life_thread="thesis deadline")
    put_wow(h, 101, pressure=.9, guilt=.2)
    ctx = h.ctx({"wow_line": [{"line": "14 hours today, gf is not amused", "mood": -.2}], "judge": [yes]})
    run(stage_wowlife(ctx))
    assert get_wow_life(h.store, 101).line == "14 hours today, gf is not amused"
    assert h.store.get_state(101).life_thread == "14 hours today, gf is not amused; thesis deadline"
    assert h.count("emotion") == 0 and not ctx.failures
    assert len(line_calls(ctx)) == 1
    assert line_calls(ctx)[0]["options"]["temperature"] == .8
    assert get_wow_life(h.store, 101).line_night == h.night


@pytest.mark.parametrize("text,reason", [("Coros kept me up all night", "name not in evidence: coros"),
    ("Quillon kept me up all night", "real name"), ("I'm leaving the guild", "leaves the guild")])
def test_rejected_line_keeps_old_line_and_no_emotion(h, text, reason):
    put_wow(h, 102, pressure=.9, line="old line")
    ctx = h.ctx({"wow_line": [{"line": text, "mood": -.8}]})
    run(stage_wowlife(ctx))
    assert get_wow_life(h.store, 102).line == "old line"
    assert h.count("emotion") == 0 and not no_real_name(h.store)
    assert h.store.conn.execute("SELECT reason FROM grounding_log").fetchone()[0] == reason
    assert ctx.done("wowlife", "line:102") and not ctx.failures


def test_negative_line_emotion_once_and_current_state_preserved(h, monkeypatch):
    put_wow(h, 101, pressure=.9)
    ctx = h.ctx({"wow_line": [{"line": "my deadline is tomorrow and I am still here", "mood": -.3}], "judge": [yes]})
    original = ctx.reasoner.json
    async def change_state(*args, **kwargs):
        value = await original(*args, **kwargs)
        if args[2].get("title") == "wow_line":
            put_wow(h, 101, pressure=.95, vow=True)
            h.store.put_state(replace(h.store.get_state(101), goals=("new goal",)))
        return value
    monkeypatch.setattr(ctx.reasoner, "json", change_state)
    run(stage_wowlife(ctx))
    assert get_wow_life(h.store, 101).pressure == .95
    assert get_wow_life(h.store, 101).vow
    assert h.store.get_state(101).goals == ("new goal",)
    row = h.store.conn.execute("SELECT * FROM emotion").fetchone()
    assert (row["type"], row["target"], row["intensity"], row["source"]) == ("distress", 0, 1, "wowlife")
    assert json.loads(row["evidence"]) == [f"rule:wowlife:{h.night}:101"]
    run(stage_wowlife(ctx))
    assert h.count("emotion") == 1 and len(line_calls(ctx)) == 1 and not ctx.failures


def test_line_limit_selects_most_strained_first(h):
    for guid, pressure in ((101, .4), (102, .7), (103, .9)):
        put_wow(h, guid, pressure=pressure)
    def reply(text):
        return {"line": "one more pull before my shift", "mood": 0} if '"title": "wow_line"' in text else yes(text)
    ctx = h.ctx(default=reply)
    ctx.settings = replace(ctx.settings, wow_lines_per_night=2)
    run(stage_wowlife(ctx))
    assert len(line_calls(ctx)) == 1
    assert get_wow_life(h.store, 103).line and not get_wow_life(h.store, 102).line
    put_wow(h, 103, pressure=0, guilt=0)
    put_wow(h, 101, pressure=.99)
    run(stage_wowlife(ctx))
    assert len(line_calls(ctx)) == 1 and not get_wow_life(h.store, 101).line
    assert not ctx.failures


def test_close_members_notice_long_nights(h):
    sid = h.session(h.T - 3600, h.T + 8 * 3600)
    h.rel(101, 1, affinity=50)
    h.rel(102, 1, affinity=10)
    ctx = h.ctx()
    run(stage_wowlife(ctx))
    mine = [m for m in h.rows(101) if m.key == f"hours:{h.night}"]
    assert [m.text for m in mine] == ["Testy was on for 9 hours last night."]
    assert mine[0].evidence == (f"session:{sid}",)
    assert (mine[0].scope, mine[0].kind, mine[0].source, mine[0].importance) == ("open", "observation", "game", 3)
    assert mine[0].actors == (1,) and mine[0].embedding is not None and mine[0].valence == 0
    assert not [m for m in h.rows(102) if (m.key or "").startswith("hours:")]
    run(stage_wowlife(h.ctx()))
    assert len([m for m in h.rows(101) if m.key == f"hours:{h.night}"]) == 1


def test_notice_ranking_close_stage_and_session_limit(h):
    for i in range(11):
        h.session(h.T + i * 1800, h.T + i * 1800 + 1200)
    h.rel(101, 1, affinity=50)
    h.rel(102, 1, affinity=70)
    h.rel(103, 1, affinity=5, romance_stage="banter")
    ctx = h.ctx()
    ctx.settings = replace(ctx.settings, notice_members=2, notice_night_hours=3)
    run(stage_wowlife(ctx))
    assert len(h.rows(101)) == len(h.rows(102)) == 1
    assert not h.rows(103)
    assert len(h.rows(101)[0].evidence) == 10
    assert not ctx.failures


def test_week_notice_upserts_same_iso_week_with_week_evidence(h):
    h.rel(101, 1, affinity=50)
    sid_old = h.session(h.T - 86400, h.T - 86400 + 8 * 3600)
    add_hours(h, "2026-10-28", 1, 8)
    sid = h.session(h.T, h.T + 8 * 3600)
    ctx = h.ctx()
    ctx.settings = replace(ctx.settings, notice_week_hours=10, notice_night_hours=20)
    run(stage_wowlife(ctx))
    first = [m for m in h.rows(101) if m.key == "hours:2026-W44"][0]
    assert first.text == "Testy has played 16 hours this week."
    assert first.evidence == (f"session:{sid_old}", f"session:{sid}")
    h.session(h.T + 86400, h.T + 86400 + 5 * 3600)
    later = h.ctx(night="2026-10-30")
    later.settings = ctx.settings
    run(stage_wowlife(later))
    weekly = [m for m in h.rows(101) if m.key == "hours:2026-W44"]
    assert len(weekly) == 1 and weekly[0].id == first.id
    assert weekly[0].text == "Testy has played 21 hours this week."
    assert len(weekly[0].evidence) == 3 and not later.failures


def test_vow_is_talk_only(h, monkeypatch):
    monkeypatch.setattr(wowlife, "VOW_CHANCE", 1)
    put_wow(h, 101, pressure=.9, guilt=.9)
    ctx = quiet(h.ctx())
    run(stage_wowlife(ctx))
    assert get_wow_life(h.store, 101).vow is True
    assert active_away(h.store) == set() and not ctx.failures


def test_state_failure_rolls_back_all_members_before_traits(h, monkeypatch):
    original = wowlife.put_wow_life
    def fail_second(store, row):
        if row.guid == 102:
            raise RuntimeError("synthetic failure")
        original(store, row)
    monkeypatch.setattr(wowlife, "put_wow_life", fail_second)
    ctx = quiet(h.ctx())
    run(stage_wowlife(ctx))
    assert h.count("wow_hours") == h.count("wow_life") == h.count("trait_state") == h.count("motive") == 0
    assert not ctx.done("wowlife", "state") and ctx.failures["wowlife"] == 1


def test_hours_embedding_failure_does_not_finish_unit(h, monkeypatch):
    h.session(h.T, h.T + 8 * 3600)
    h.rel(101, 1, affinity=50)
    ctx = h.ctx()
    original = ctx.embed
    async def fail(texts):
        raise RuntimeError("synthetic embedding outage")
    monkeypatch.setattr(ctx, "embed", fail)
    with pytest.raises(RuntimeError, match="embedding outage"):
        run(stage_wowlife(ctx))
    assert not h.rows(101) and not ctx.done("wowlife", "hours")
    assert ctx.done("wowlife", "state")
    monkeypatch.setattr(ctx, "embed", original)
    run(stage_wowlife(ctx))
    assert len(h.rows(101)) == 1 and ctx.done("wowlife", "hours")
