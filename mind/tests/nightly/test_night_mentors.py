"""Mentorship uses roster facts and relationship state, without model calls."""
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from conftest import run
from leeroy_mind.nightly.mentors import active_pair, is_mentee, is_veteran, stage_mentors
from leeroy_mind.nightly.settings import iso_week
from leeroy_mind.nightly.storyteller import arc_key

DAY = 86400


def people(h):
    # Keep the test's candidate pool explicit: most of the synthetic cast is neither.
    for guid in (101, 102, 103, 104, 105):
        h.persona(guid, years_played=4, skill="solid", guild_role="social")
    h.persona(101, years_played=8, skill="solid", guild_role="raider")
    h.persona(104, years_played=4, skill="sloppy", guild_role="social")
    h.store.conn.execute('UPDATE cast_member SET class=1 WHERE guid=104')


def pairs(h):
    return h.store.conn.execute("SELECT * FROM mentor ORDER BY id").fetchall()


def later_ctx(h, seconds):
    h.clock.advance(seconds)
    night = datetime.fromtimestamp(h.clock.now(), timezone.utc).date().isoformat()
    return h.ctx(night=night)


@pytest.mark.parametrize("years,skill,role,expected", [
    (5, "solid", "raider", True), (8, "clutch", "officer", True),
    (4, "clutch", "officer", False), (8, "sloppy", "raider", False),
    (8, "solid", "social", False),
])
def test_veteran_rule(h, years, skill, role, expected):
    assert is_veteran(h.persona(101, years_played=years, skill=skill, guild_role=role)) is expected


@pytest.mark.parametrize("years,skill,expected", [(3, "solid", True), (4, "sloppy", True),
                                                (4, "solid", False)])
def test_mentee_rule(h, years, skill, expected):
    assert is_mentee(h.persona(104, years_played=years, skill=skill)) is expected


def test_pair_has_both_memories_duration_and_arc_without_model(h):
    people(h)
    ctx = h.ctx()
    run(stage_mentors(ctx))
    pair, = pairs(h)
    assert (pair["mentor"], pair["mentee"]) == (101, 104)
    assert h.T + 28 * DAY <= pair["ends"] <= h.T + 56 * DAY
    assert pair["started"] == h.T and pair["ended"] is None
    veteran, = h.rows(101)
    mentee, = h.rows(104)
    assert veteran.text == "Took Dunna under my wing."
    assert mentee.text == "Arkon is showing me the ropes."
    assert veteran.actors == (104,) and mentee.actors == (101,)
    for memory in (veteran, mentee):
        assert (memory.kind, memory.source, memory.importance) == ("reflection", "reflection", 5)
        assert memory.evidence == (f"mentor:{pair['id']}",)
        assert (memory.scope, memory.scope_with) == ("open", 0)
    arc = h.store.conn.execute("SELECT * FROM story_arc").fetchone()
    assert (arc["kind"], arc["key"], arc["status"]) == ("mentor", arc_key("mentor", 101, 104), "open")
    assert ctx.done("mentors", iso_week(ctx.night))
    assert ctx.reasoner.llm.calls == [] and h.rows(0) == h.rows(h.human) == []


def test_once_per_week_across_different_nights_and_fresh_context(h):
    people(h)
    first = h.ctx()
    run(stage_mentors(first))
    run(stage_mentors(h.ctx()))
    # Add a new eligible player after the week's run: they wait until next week.
    h.persona(105, years_played=3, skill="solid", guild_role="social")
    tomorrow = later_ctx(h, DAY)
    assert iso_week(tomorrow.night) == iso_week(first.night)
    run(stage_mentors(tomorrow))
    assert len(pairs(h)) == 1 and h.rows(105) == []
    next_week = later_ctx(h, 7 * DAY)
    run(stage_mentors(next_week))
    assert len(pairs(h)) == 2 and len(h.rows(105)) == 1


def test_same_class_beats_more_affine_other_class_then_affinity_breaks_ties(h):
    people(h)
    h.persona(102, years_played=8, skill="clutch", guild_role="officer")
    h.persona(103, years_played=8, skill="clutch", guild_role="officer")
    h.store.conn.execute('UPDATE cast_member SET class=1 WHERE guid=103')
    h.rel(101, 104, affinity=20)
    h.rel(102, 104, affinity=90)
    h.rel(103, 104, affinity=40)
    run(stage_mentors(h.ctx()))
    assert pairs(h)[0]["mentor"] == 103


def test_same_role_fallback_and_affinity_is_directed_from_mentor(h):
    people(h)
    h.store.conn.execute('UPDATE cast_member SET class=2 WHERE guid=104')
    h.persona(102, years_played=8, skill="clutch", guild_role="officer")
    h.rel(101, 104, affinity=30)
    h.rel(102, 104, affinity=20)
    h.rel(104, 102, affinity=90)
    run(stage_mentors(h.ctx()))
    assert pairs(h)[0]["mentor"] == 101


def test_tied_affinity_chooses_low_guid_and_no_role_match_skips(h):
    people(h)
    h.store.conn.execute('UPDATE cast_member SET class=2 WHERE guid=104')
    h.persona(102, years_played=8, skill="clutch", guild_role="officer")
    run(stage_mentors(h.ctx()))
    assert pairs(h)[0]["mentor"] == 101
    # A second mentee has neither a matching class nor a matching raid role.
    h.persona(105, years_played=3, raid_role="healer", guild_role="social")
    run(stage_mentors(later_ctx(h, 7 * DAY)))
    assert len(pairs(h)) == 1


def test_global_capacity_and_one_mentor_per_mentee(h):
    people(h)
    h.persona(105, years_played=3, guild_role="social")
    ctx = h.ctx()
    ctx.settings = replace(ctx.settings, mentors_max=1)
    run(stage_mentors(ctx))
    assert len(pairs(h)) == 1 and pairs(h)[0]["mentee"] == 104
    following = later_ctx(h, 7 * DAY)
    following.settings = replace(following.settings, mentors_max=1)
    run(stage_mentors(following))
    assert len(pairs(h)) == 1 and len(h.rows(104)) == 1 and h.rows(105) == []


def test_expired_pair_and_arc_end_then_same_pair_can_reopen(h):
    people(h)
    run(stage_mentors(h.ctx()))
    first = pairs(h)[0]
    # No replacement this week, so the completed arc remains visibly resolved.
    h.persona(104, years_played=4, skill="solid", guild_role="social")
    ctx = later_ctx(h, 57 * DAY)
    run(stage_mentors(ctx))
    assert pairs(h)[0]["ended"] == h.clock.now()
    assert h.store.conn.execute("SELECT status FROM story_arc").fetchone()[0] == "resolved"
    h.persona(104, years_played=4, skill="sloppy", guild_role="social")
    ctx = later_ctx(h, 7 * DAY)
    run(stage_mentors(ctx))
    old, new = pairs(h)
    assert old["id"] == first["id"] and new["ended"] is None
    assert h.store.conn.execute("SELECT status,opened FROM story_arc").fetchone()[:] == ("open", h.clock.now())
    assert len(h.rows(101)) == len(h.rows(104)) == 2


def test_expiration_still_runs_when_capacity_disabled(h):
    people(h)
    run(stage_mentors(h.ctx()))
    ctx = later_ctx(h, 57 * DAY)
    ctx.settings = replace(ctx.settings, mentors_max=0)
    run(stage_mentors(ctx))
    assert pairs(h)[0]["ended"] == h.clock.now()
    assert h.store.conn.execute("SELECT status FROM story_arc").fetchone()[0] == "resolved"


def test_active_pair_is_symmetric_and_excludes_future_expired_ended_and_human(h):
    people(h)
    ctx = h.ctx()
    run(stage_mentors(ctx))
    ident = pairs(h)[0]["id"]
    assert active_pair(ctx, 101, 104)["id"] == active_pair(ctx, 104, 101)["id"] == ident
    h.store.conn.execute("UPDATE mentor SET started=?", (h.T + 1,))
    assert active_pair(ctx, 101, 104) is None
    h.store.conn.execute("UPDATE mentor SET started=?,ends=?", (h.T, h.T))
    assert active_pair(ctx, 101, 104) is None
    h.store.conn.execute("UPDATE mentor SET ends=?,ended=?", (h.T + DAY, h.T))
    assert active_pair(ctx, 101, 104) is None
    h.store.conn.execute("UPDATE mentor SET mentor=?,ended=NULL", (h.human,))
    assert active_pair(ctx, h.human, 104) is None


def test_away_member_is_not_paired_and_existing_pairs_are_counted(h):
    people(h)
    h.store.conn.execute("INSERT INTO away(guid,left_at,returns_at,reason) VALUES (?,?,?,?)",
                        (104, h.T, h.T + DAY, "a trip"))
    run(stage_mentors(h.ctx()))
    assert pairs(h) == []


def test_writer_failure_rolls_back_pairs_arcs_expiration_and_week_marker(h, monkeypatch):
    people(h)
    run(stage_mentors(h.ctx()))
    before = dict(pairs(h)[0])
    ctx = later_ctx(h, 57 * DAY)
    original = ctx.writer.put
    calls = 0

    def fail_second(row, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ValueError("synthetic memory failure")
        return original(row, **kwargs)

    monkeypatch.setattr(ctx.writer, "put", fail_second)
    run(stage_mentors(ctx))
    assert ctx.failures["mentors"] == 1
    assert dict(pairs(h)[0]) == before and len(pairs(h)) == 1
    assert h.store.conn.execute("SELECT status FROM story_arc").fetchone()[0] == "open"
    assert len(h.rows(101)) == len(h.rows(104)) == 1
    assert not ctx.done("mentors", iso_week(ctx.night))
    monkeypatch.setattr(ctx.writer, "put", original)
    run(stage_mentors(ctx))
    assert len(pairs(h)) == 2 and ctx.done("mentors", iso_week(ctx.night))
