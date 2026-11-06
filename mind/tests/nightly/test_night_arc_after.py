"""Track S6: what follows the weekend (milestones and the next visit), how a romance ends (the
affair found out at home) and the arc's built-in texts."""
import asyncio
import json
import logging
import re
from dataclasses import replace
from datetime import date, timedelta, timezone

import pytest

from conftest import make_persona
from leeroy_mind.arc import after
from leeroy_mind.arc.after import (AFFAIR_RISK_NIGHT, AFFAIR_RISK_TRIP, FOUND_OUT_ENDS_ROMANCE_P,
                                   FOUND_TEXT, HOUSEHOLD_SEPARATED, HOUSEHOLD_SEPARATED_KIDS,
                                   MILESTONE_AGAIN, MILESTONE_GAP, MILESTONE_IMPORTANCE,
                                   MILESTONE_TEXT, MILESTONES, MONTH_S, separated_household)
from leeroy_mind.arc.night import stage_arc
from leeroy_mind.arc.plan import has_partner, propose_plan
from leeroy_mind.arc.state import Arc, get_arc, play_days_after, put_arc
from leeroy_mind.arc.texts import arc_texts
from leeroy_mind.arc.weekend import (AFFAIR, PLAIN_VISIT, PLAIN_WEEKEND, WEEKEND_GAP_S,
                                     WEEKEND_PROMPT, WEEKEND_S)
from leeroy_mind.human import THEY_FORMS, _FORMS
from leeroy_mind.nightly.growth import undo
from leeroy_mind.nightly.romance import stage_romance
from leeroy_mind.nightly.settings import night_bounds
from leeroy_mind.personas.io import load_persona, save_persona
from nightly.conftest import yes

run = asyncio.run
UTC = timezone.utc
DAY = 86400
HOUR = 3600
# The romance reproach scan: REPROACH_RE in talk/romance.py on track B (B3). Copied here, as
# tests/test_arc_plan.py and tests/nightly/test_night_weekend.py copy it; keep them in step.
REPROACH_RE = re.compile(
    r"\b(you never|you always|where (were|are) you|why (didn'?t|don'?t|won'?t) you|you forgot"
    r"|ignor(e|es|ing) me|you don'?t care|left me|stood me up|i'?m (still )?waiting"
    r"|waiting for you|come over|get over here|hurry( up)?|find me)\b", re.IGNORECASE)
MARRIED = "married, two kids at home"


def label(d: int) -> str:
    return (date.fromisoformat("2026-10-29") + timedelta(days=d)).isoformat()


def morning(h, d=0, replies=None):
    """Night `d` (the harness night plus d days), run at 07:00 the next morning, past its
    window."""
    h.clock.set(night_bounds(label(d), UTC)[1] + HOUR)
    ctx = h.ctx(replies, night=label(d))
    run(stage_arc(ctx))
    return ctx


def plays(h, d):
    """His session on day `d`: 21:00 to 23:00."""
    start = h.T + d * DAY
    return h.session(start, start + 2 * HOUR), start + 2 * HOUR


def home_from(h, guid=101, stance="lust", *, back, kind="neutral", milestone=0,
              milestone_at=None, **persona):
    """A woman home from her weekend with him since `back`: her away row (closed) and its
    meetup, stage after, her plan done. Returns the away id."""
    h.woman(guid, stance, **persona)
    h.rel(guid, 1, attraction=99, affinity=99, trust=99, romance_stage="after")
    with h.store.transaction():
        aid = h.store.conn.execute(
            "INSERT INTO away(guid, reason, left_at, returns_at, returned_at) "
            "VALUES (?, 'weekend', ?, ?, ?)", (guid, back - WEEKEND_S, back, back)).lastrowid
        h.store.conn.execute("INSERT INTO arc_meetup(absence, guid, away_id, kind, written) "
                             "VALUES (?, ?, ?, 'weekend', ?)", (9000 + guid, guid, aid, back))
    put_arc(h.store, Arc(guid, 1, plan={"kind": kind, "target": back - DAY, "worries": [],
                                        "cover": ""},
                         plan_state="done", planning_since=back - 9 * DAY, weekends=1,
                         last_meetup=back, milestone=milestone, milestone_at=milestone_at,
                         updated=back))
    return aid


def ready(h, guid=101, stance="lust", *, stage="planning", **persona):
    """A woman at `stage` whose halfway plan he said yes to two days ago, floated a week ago."""
    h.woman(guid, stance, **persona)
    h.rel(guid, 1, attraction=99, affinity=99, trust=99, romance_stage=stage)
    put_arc(h.store, Arc(guid, 1, plan={"kind": "halfway", "target": h.T + 3 * DAY,
                                        "worries": ["money"], "cover": "a work trip"},
                         plan_state="set", planning_since=h.T - 7 * DAY, plan_at=h.T - 2 * DAY,
                         updated=h.T - 2 * DAY))


def offscreen(h, guid=101):
    return [m for m in h.rows(guid) if m.source == "offscreen"]


def text_of(name, like="quiet runs", forms=_FORMS["he"], nth=None):
    """Her milestone memory: the first cycle's words, or with `nth` a later cycle's."""
    template = MILESTONE_TEXT[name] if nth is None else MILESTONE_AGAIN[name]
    return template.format(called="Testy", him=forms[1], his=forms[2], like=like, nth=nth)


def milestone_rows(h, guid=101):
    return [m for m in offscreen(h, guid) if m.importance == MILESTONE_IMPORTANCE]


def daily(h, days):
    """He plays each day in `days` and each night runs the next morning."""
    for d in days:
        plays(h, d)
        ctx = morning(h, d)
        assert not ctx.failures


def meetups(h):
    return [dict(r) for r in h.store.conn.execute("SELECT * FROM arc_meetup ORDER BY written")]


def test_the_values():
    assert MILESTONES == ("missing", "inside_joke", "month", "gift", "friends_on_voice",
                          "fight_and_make_up", "next_visit")
    assert set(MILESTONE_TEXT) == set(MILESTONE_AGAIN) == set(MILESTONES)
    assert all("{nth}" in t for t in MILESTONE_AGAIN.values())
    assert not any("first" in t for t in MILESTONE_AGAIN.values())
    assert (MILESTONE_GAP, MONTH_S) == ((4, 8), 30 * DAY)
    assert (AFFAIR_RISK_NIGHT, AFFAIR_RISK_TRIP, FOUND_OUT_ENDS_ROMANCE_P) == (0.005, 0.04, 0.5)
    for text in (HOUSEHOLD_SEPARATED, HOUSEHOLD_SEPARATED_KIDS):
        assert len(text) <= 120 and "things blew up at home" in text


def test_milestones_come_in_order_and_a_month_waits_for_the_calendar(h):
    back = h.T - HOUR                                    # she came home an hour before he logged in
    aid = home_from(h, back=back)
    for d in range(80):                                  # he plays every day
        plays(h, d)
        ctx = morning(h, d)
        assert not ctx.failures and get_arc(h.store, 101, 1).ended is None
    arc = get_arc(h.store, 101, 1)
    events = list(arc.events)
    assert [k for k, _ in events] == list(MILESTONES)    # one cycle, then it waits
    months = [t for k, t in events if k == "month"]
    assert len(months) == 1
    assert months[0] - back >= MONTH_S                   # the calendar, not his play
    since = back
    for k, t in events:
        gap = play_days_after(h.store, since, t, UTC)
        if t == months[0]:
            assert gap > MILESTONE_GAP[1], gap           # it waited past its play days
        else:
            assert MILESTONE_GAP[0] <= gap <= MILESTONE_GAP[1], (k, gap)
        since = t
    rows = offscreen(h)
    assert [m.text for m in rows] == [text_of(k) for k, _ in events]
    assert [m.t_event for m in rows] == [int(t) for _, t in events]
    for m in rows:
        assert (m.scope, m.scope_with, m.actors, m.evidence) == ("private", 1, (1,),
                                                                 (f"away:{aid}",))
        assert m.embedding is not None and "Arkon" not in m.text and "Quillon" not in m.text
    visit = next(t for k, t in events if k == "next_visit")
    assert (arc.plan_state, arc.planning_since, arc.plan_at) == ("hoped", visit, None)
    assert arc.plan["kind"] != "neutral"                 # the kind is picked again
    assert arc.weekends == 1 and h.store.get_relationship(101, 1).romance_stage == "after"
    assert arc.milestone == len(MILESTONES)              # the cycle is spent
    before = (len(h.rows(101)), get_arc(h.store, 101, 1))
    assert not morning(h, 79).failures                   # the night again: nothing new
    assert (len(h.rows(101)), get_arc(h.store, 101, 1)) == before


def test_next_visit_starts_a_new_plan_and_a_later_gap_writes_a_visit(h):
    home_from(h, back=h.T - 40 * DAY, milestone=MILESTONES.index("next_visit"),
              milestone_at=h.T - 20 * DAY)
    for d in range(-9, 1):
        plays(h, d)
    ctx = morning(h, 0)                                  # next_visit is due: a new hope
    floated = h.clock.now()
    arc = get_arc(h.store, 101, 1)
    assert not ctx.failures and ctx.reasoner.calls == 0
    assert (arc.plan_state, arc.planning_since, arc.plan_at) == ("hoped", floated, None)
    assert arc.plan["kind"] != "neutral" and arc.milestone == len(MILESTONES)
    assert arc.events[-1] == ("next_visit", floated)
    (m,) = offscreen(h)
    assert (m.text, m.kind, m.scope, m.scope_with) == (text_of("next_visit"), "plan",
                                                        "private", 1)
    sid, end = plays(h, 1)                               # he says yes the next evening
    h.his(h.T + DAY + HOUR, "another trip? yes, let's do it", channel="whisper", to=101)
    ctx = morning(h, 1, {"arc_read": [{"plan": "yes", "breakup": False}]})
    arc = get_arc(h.store, 101, 1)
    assert not ctx.failures and (arc.plan_state, arc.plan_at) == ("set", h.clock.now())
    assert len(offscreen(h)) == 1                        # no milestone yet: one play day
    visit = ["Seeing Testy again was easier than the first time.",
             "We argued over the last pastry and both lost it laughing."]
    h.clock.set(end + WEEKEND_GAP_S + 60)                # then two days away: the visit
    ctx = h.ctx({"arc_weekend": [{"moments": visit, "tone": "warm"}], "judge": [yes]},
                night="2026-11-01")
    run(stage_arc(ctx))
    assert not ctx.failures
    assert [(r["guid"], r["kind"]) for r in meetups(h)] == [(101, "weekend"), (101, "visit")]
    assert meetups(h)[1]["absence"] == sid
    returned = h.store.conn.execute("SELECT returns_at FROM away WHERE reason = 'visit'"
                                    ).fetchone()[0]
    arc = get_arc(h.store, 101, 1)
    assert (arc.plan_state, arc.weekends, arc.last_meetup) == ("done", 2, returned)
    assert (arc.milestone, arc.milestone_at) == (0, None)                # a new cycle armed
    assert h.store.get_relationship(101, 1).romance_stage == "after"
    assert [(m.text, m.importance) for m in offscreen(h)][1:] == [(t, 7) for t in visit]


def test_after_next_visit_nothing_is_written_until_the_next_meetup(h):
    home_from(h, back=h.T - 40 * DAY, milestone=MILESTONES.index("next_visit"),
              milestone_at=h.T - 20 * DAY)
    for d in range(-9, 0):
        plays(h, d)
    daily(h, range(120))                                 # his yes never comes: a hope
    arc = get_arc(h.store, 101, 1)
    assert [k for k, _ in arc.events] == ["next_visit"]
    assert [m.text for m in offscreen(h)] == [text_of("next_visit")]
    assert (arc.plan_state, arc.milestone) == ("hoped", len(MILESTONES))


def test_a_second_meetup_arms_a_second_cycle_in_new_words(h):
    back = h.T - HOUR
    home_from(h, back=back)
    daily(h, range(70))                                  # the first cycle, to next_visit
    assert [m.text for m in milestone_rows(h)] == [text_of(k) for k in MILESTONES]
    sid, end = plays(h, 70)                              # his yes to the next visit
    h.his(h.T + 70 * DAY + HOUR, "yes, come out again", channel="whisper", to=101)
    assert not morning(h, 70, {"arc_read": [{"plan": "yes", "breakup": False}]}).failures
    assert get_arc(h.store, 101, 1).plan_state == "set"
    visit = ["Seeing Testy again felt easy from the first minute.",
             "We got caught in the rain and laughed the whole way back."]
    h.clock.set(end + WEEKEND_GAP_S + 60)
    ctx = h.ctx({"arc_weekend": [{"moments": visit, "tone": "warm"}], "judge": [yes]},
                night=label(72))
    run(stage_arc(ctx))
    assert not ctx.failures and get_arc(h.store, 101, 1).weekends == 2
    second = get_arc(h.store, 101, 1).last_meetup
    visit_kind = get_arc(h.store, 101, 1).plan["kind"]
    daily(h, range(73, 150))                             # the second cycle
    texts = [m.text for m in milestone_rows(h)]
    assert texts == ([text_of(k) for k in MILESTONES]
                     + [text_of(k, nth="second") for k in MILESTONES])
    assert len(set(texts)) == len(texts)                 # never the same words twice
    assert not any("first" in t for t in texts[len(MILESTONES):])
    months = [m.t_event for m in milestone_rows(h)
              if m.text in (text_of("month"), text_of("month", nth="second"))]
    assert len(months) == 2                              # one month for each meetup
    assert months[0] - back >= MONTH_S and months[1] - second >= MONTH_S
    arc = get_arc(h.store, 101, 1)
    assert (arc.plan_state, arc.milestone) == ("hoped", len(MILESTONES))
    assert arc.plan["kind"] != visit_kind


def test_a_next_visit_while_a_plan_is_open_floats_no_second_one(h, caplog):
    home_from(h, back=h.T - 40 * DAY, milestone=MILESTONES.index("next_visit"),
              milestone_at=h.T - 20 * DAY)
    put_arc(h.store, replace(get_arc(h.store, 101, 1), plan_state="talked"))
    kept = get_arc(h.store, 101, 1).plan
    for d in range(-9, 1):
        plays(h, d)
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        assert not morning(h, 0).failures
    arc = get_arc(h.store, 101, 1)
    assert (arc.plan_state, arc.plan) == ("talked", kept)              # left as it was
    assert (arc.milestone, arc.milestone_at) == (len(MILESTONES), h.clock.now())
    assert offscreen(h) == [] and arc.events == ()
    assert [r.leeroy_fields for r in caplog.records
            if getattr(r, "leeroy_event", "") == "arc_after"] == [
        {"night": label(0), "guid": 101, "milestone": "next_visit", "found": None,
         "trip": False, "skipped": "plan_open"}]


def test_neglect_never_ends_it(h):
    h.woman(101, "lust")
    h.rel(101, 1, attraction=99, affinity=99, trust=99, romance_stage="whispers")
    ready(h, 102, "drawn")
    home_from(h, 103, back=h.T - 3 * DAY)
    for d in range(-7, 1):
        plays(h, d)
    with h.store.transaction():                          # her attraction has been cooling
        for d in range(-7, 50):
            h.store.conn.execute(
                "INSERT INTO attraction_log(night, guid, gained, decayed, neglected, hurt, "
                "half_life, before, after, stance_before, stance_after) "
                "VALUES (?, 102, 0, 4, 1, 0, 10, 50, 46, 'drawn', 'drawn')", (label(d),))
    awkward = ["We were both so nervous the first dinner was mostly silence.",
               "I spilled tea on Testy's sleeve and wanted the floor to swallow me.",
               "By the last evening it was easier, but it never quite clicked.",
               "On the train home I didn't know what to feel."]
    h.clock.set(h.T + 2 * HOUR + WEEKEND_GAP_S + 60)     # a bad weekend in his first gap
    ctx = h.ctx({"arc_weekend": [{"moments": awkward, "tone": "awkward"}], "judge": [yes]},
                night=label(2))
    run(stage_arc(ctx))
    assert not ctx.failures and h.store.get_relationship(102, 1).romance_stage == "after"
    for g in (101, 102, 103):                            # then he stops playing; scores fall
        h.rel(g, 1, attraction=0, affinity=0, trust=0)
    for d in range(3, 45):
        h.clock.set(night_bounds(label(d), UTC)[1] + HOUR)
        ctx = h.ctx(night=label(d))
        run(stage_romance(ctx))
        run(stage_arc(ctx))
        assert not ctx.failures
    assert {g: h.store.get_relationship(g, 1).romance_stage for g in (101, 102, 103)} == {
        101: "whispers", 102: "after", 103: "after"}
    assert all(get_arc(h.store, g, 1).ended is None for g in (101, 102, 103))
    assert h.store.conn.execute("SELECT count(*) FROM rel_delta WHERE reason LIKE '%->ex'"
                                ).fetchone()[0] == 0


@pytest.mark.parametrize("ending,p", [("ended", 1.0), ("separated", 0.0)])
def test_found_out_ends_the_romance_or_her_marriage(h, monkeypatch, caplog, ending, p):
    monkeypatch.setattr(after, "AFFAIR_RISK_NIGHT", 1.0)
    monkeypatch.setattr(after, "FOUND_OUT_ENDS_ROMANCE_P", p)
    h.woman(101, "lust", household=MARRIED)
    h.rel(101, 1, attraction=99, affinity=99, trust=99, romance_stage="late_nights")
    h.woman(102, "drawn", household="lives with her boyfriend")
    h.rel(102, 1, romance_stage="whispers")                            # not that far yet
    h.woman(103, "fond", household="married")
    h.rel(103, 1, romance_stage="late_nights")                         # fond: not on the arc
    h.woman(104, "lust")
    h.rel(104, 1, romance_stage="confession")                          # a flatmate, no partner
    version = h.store.persona_versions()[101]
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        ctx = morning(h, 0)
    now = h.clock.now()
    assert not ctx.failures
    arc = get_arc(h.store, 101, 1)
    rows = offscreen(h)
    assert len(rows) == 2
    shared, theirs = rows
    assert (shared.text, shared.scope, shared.scope_with, shared.actors) == (
        FOUND_TEXT["open"], "open", 0, ())
    assert "Testy" not in shared.text and "Arkon" not in shared.text
    assert (theirs.text, theirs.scope, theirs.scope_with, theirs.actors) == (
        FOUND_TEXT[ending].format(called="Testy", him="him", his="his"), "private", 1, (1,))
    if ending == "ended":
        assert h.store.get_relationship(101, 1).romance_stage == "ex"
        assert (arc.ended, arc.partner, arc.events[-1]) == ("found_out", "none",
                                                            ("found_out", now))
        assert [r[0] for r in h.store.conn.execute(
            "SELECT reason FROM rel_delta WHERE a = 101")] == ["late_nights->ex"]
        assert h.store.persona_versions()[101] == version
        assert h.count("persona_ledger") == 0
    else:
        assert h.store.get_relationship(101, 1).romance_stage == "late_nights"
        assert (arc.ended, arc.partner, arc.events[-1]) == (None, "separated",
                                                            ("separated", now))
        p = load_persona(h.store, 101)
        assert p.real_life.household == HOUSEHOLD_SEPARATED_KIDS       # she has kids
        assert h.store.persona_versions()[101] == version + 1
        assert not has_partner(p) and not has_partner(p, arc.partner)
        assert ctx.persona(101).real_life.household == HOUSEHOLD_SEPARATED_KIDS
        (led,) = h.store.conn.execute("SELECT * FROM persona_ledger").fetchall()
        assert (led["guid"], led["kind"]) == (101, "household")
        assert json.loads(led["text"]) == {"household": [MARRIED, HOUSEHOLD_SEPARATED_KIDS],
                                           "partner": "none", "cover": "",
                                           "memory": theirs.id, "event": ["separated", now]}
    for g in (102, 103, 104):
        assert offscreen(h, g) == [] and get_arc(h.store, g, 1) == Arc(g, 1)
    fields = [r.leeroy_fields for r in caplog.records
              if getattr(r, "leeroy_event", "") == "arc_after"]
    assert fields == [{"night": label(0), "guid": 101, "milestone": None, "found": ending,
                       "trip": False, "skipped": None}]
    later = morning(h, 1)                                # found out once: never again
    assert not later.failures and len(offscreen(h)) == 2


def test_the_trip_risk_applies_on_the_night_she_meets_him(h, monkeypatch):
    monkeypatch.setattr(after, "AFFAIR_RISK_NIGHT", 0.0)
    monkeypatch.setattr(after, "AFFAIR_RISK_TRIP", 1.0)
    monkeypatch.setattr(after, "FOUND_OUT_ENDS_ROMANCE_P", 0.0)
    ready(h, 101, household=MARRIED)
    h.woman(102, "lust", household="married")
    h.rel(102, 1, attraction=99, affinity=99, trust=99, romance_stage="confession")
    for d in range(-7, 1):
        _, end = plays(h, d)
    assert not morning(h, 0).failures                    # no gap yet, so no trip
    assert offscreen(h) == [] and get_arc(h.store, 101, 1).partner == "none"
    moments = ["We met at a hotel halfway and I saw Testy first.",
               "Dinner went on for hours."]
    h.clock.set(end + WEEKEND_GAP_S + 60)
    ctx = h.ctx({"arc_weekend": [{"moments": moments, "tone": "warm"}], "judge": [yes]},
                night=label(2))
    run(stage_arc(ctx))
    assert not ctx.failures and len(meetups(h)) == 1
    assert ("card", 101) not in ctx._memo                # the meetup's card is not kept
    assert ctx.persona(101).real_life.household == HOUSEHOLD_SEPARATED_KIDS
    arc = get_arc(h.store, 101, 1)
    assert (arc.partner, arc.plan["cover"], arc.plan_state) == ("separated", "", "done")
    assert h.store.get_relationship(101, 1).romance_stage == "after"
    assert get_arc(h.store, 102, 1).partner == "none" and offscreen(h, 102) == []


def separate(h, monkeypatch, household="lives with her husband"):
    """A woman at planning with a talked-about plan and its cover story, whose marriage the
    affair ends tonight. Returns the night's context."""
    monkeypatch.setattr(after, "AFFAIR_RISK_NIGHT", 1.0)
    monkeypatch.setattr(after, "FOUND_OUT_ENDS_ROMANCE_P", 0.0)
    h.woman(101, "lust", household=household)
    h.rel(101, 1, attraction=99, affinity=99, trust=99, romance_stage="planning")
    put_arc(h.store, Arc(101, 1, plan={"kind": "event", "target": h.T + 9 * DAY,
                                       "worries": ["money"], "cover": "a work trip"},
                         plan_state="talked", planning_since=h.T - 5 * DAY, updated=h.T))
    ctx = morning(h, 0)
    assert not ctx.failures
    return ctx


def test_the_household_rewrite_is_undone_whole(h, monkeypatch):
    ctx = separate(h, monkeypatch)
    now = h.clock.now()
    assert load_persona(h.store, 101).real_life.household == HOUSEHOLD_SEPARATED
    arc = get_arc(h.store, 101, 1)
    assert (arc.partner, arc.plan["cover"], arc.events) == ("separated", "",
                                                            (("separated", now),))
    shared, theirs = offscreen(h)
    (led,) = h.store.conn.execute("SELECT id FROM persona_ledger").fetchall()
    ctx.persona(101)                                     # cached
    assert undo(h.store, led["id"], now=now + 60, personas=ctx.personas)
    p = load_persona(h.store, 101)
    assert p.real_life.household == "lives with her husband" and has_partner(p)
    assert ctx.persona(101).real_life.household == "lives with her husband"
    arc = get_arc(h.store, 101, 1)
    assert (arc.partner, arc.plan["cover"], arc.plan_state) == ("none", "a work trip",
                                                                "talked")
    assert arc.events == () and has_partner(p, arc.partner)
    assert {m.id: m.status for m in h.rows(101)} == {shared.id: "active",
                                                     theirs.id: "rejected"}
    assert [r["kind"] for r in h.store.conn.execute(
        "SELECT kind FROM persona_ledger ORDER BY id")] == ["household", "undo"]
    assert not undo(h.store, led["id"], now=now + 120)                 # once only


def test_undo_leaves_a_household_he_edited_by_hand(h, monkeypatch):
    separate(h, monkeypatch)
    stored = load_persona(h.store, 101)
    save_persona(h.store, replace(stored, real_life=replace(
        stored.real_life, household="lives alone with two cats")), source="owner",
        ts=h.clock.now(), race="Orc", class_name="Warrior")
    (led,) = h.store.conn.execute("SELECT id FROM persona_ledger").fetchall()
    assert undo(h.store, led["id"], now=h.clock.now() + 60)
    assert load_persona(h.store, 101).real_life.household == "lives alone with two cats"
    arc = get_arc(h.store, 101, 1)
    assert (arc.partner, arc.plan["cover"]) == ("none", "a work trip")


def test_only_her_own_children_stay_in_her_household():
    for text in ("married, two kids at home", "lives with her husband and their twins",
                 "married with a daughter", "two children and a dog", "she's got three kids",
                 "her son lives with her"):
        assert separated_household(text) == HOUSEHOLD_SEPARATED_KIDS, text
    for text in ("married, doesn't have kids", "married, no kids", "wants kids someday",
                 "kid-free and loving it", "lives with her boyfriend and his son",
                 "married, child-free by choice", "married, no children",
                 "lives with her husband; her sister's kids visit"):
        assert separated_household(text) == HOUSEHOLD_SEPARATED, text


def test_has_partner_reads_her_arc_before_her_card():
    married = make_persona(101, "Arkon", household=MARRIED)
    alone = make_persona(101, "Arkon", household="lives with a flatmate")
    assert has_partner(married) and has_partner(married, "none")
    assert not has_partner(married, "separated") and has_partner(alone, "together")
    assert not has_partner(alone)
    for text in (HOUSEHOLD_SEPARATED, HOUSEHOLD_SEPARATED_KIDS):
        assert not has_partner(make_persona(101, "Arkon", household=text))
    import random
    rng = random.Random(4)
    assert all(propose_plan(married, rng, now=0.0, partner="separated")["cover"] == ""
               for _ in range(20))
    assert all(propose_plan(married, rng, now=0.0)["cover"] for _ in range(20))


def fill(s: str, forms) -> str:
    """A built-in arc string with its fields filled: his called name and pronoun forms, her
    name, a like from her card; every other field empty."""
    class Fields(dict):
        def __missing__(self, key):
            return ""
    return s.format_map(Fields(called="Testy", name="Arkon", he=forms[0], him=forms[1],
                               his=forms[2], like="quiet runs", nth="second",
                               cover="a work trip",
                               where="at a hotel halfway between us"))


def test_arc_texts_never_reproach():
    texts = arc_texts()
    for s in (*MILESTONE_TEXT.values(), *MILESTONE_AGAIN.values(), *FOUND_TEXT.values(),
              HOUSEHOLD_SEPARATED,
              HOUSEHOLD_SEPARATED_KIDS, PLAIN_WEEKEND, PLAIN_VISIT, AFFAIR, WEEKEND_PROMPT):
        assert s in texts
    assert "It's happening: BlizzCon, about 12 days off." in texts      # the plan lines
    assert any("you leave it with {him}" in s for s in texts)
    hits = [(s, f) for f in (_FORMS["he"], _FORMS["she"], THEY_FORMS) for s in texts
            if REPROACH_RE.search(fill(s, f))]
    assert hits == []
    assert len(texts) == len(set(texts))


def test_the_after_logs_carry_no_text_or_names(h, caplog):
    aid = home_from(h, back=h.T - 10 * DAY, milestone_at=None)
    for d in range(-9, 1):
        plays(h, d)
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        morning(h, 0)
    events = [r for r in caplog.records if getattr(r, "leeroy_event", "") == "arc_after"]
    assert [r.leeroy_fields for r in events] == [
        {"night": label(0), "guid": 101, "milestone": "missing", "found": None, "trip": False,
         "skipped": None}]
    text = " ".join(r.getMessage() for r in events)
    for word in ("Testy", "Arkon", "quiet runs", "I miss", f"away:{aid}"):
        assert word not in text
