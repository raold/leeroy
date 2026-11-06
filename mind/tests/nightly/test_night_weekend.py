"""Track S5: the weekend in a gap of his play, her time away and the one-meetup slot."""
import asyncio
import logging
import re
from dataclasses import replace
from datetime import date, timedelta, timezone

import pytest

from leeroy_mind.arc.night import stage_arc
from leeroy_mind.arc.plan import PLAN_KINDS
from leeroy_mind.arc.state import Arc, end_romance, get_arc, play_days, put_arc
from leeroy_mind.arc.weekend import (AFFAIR, PLAIN_VISIT, PLAIN_WEEKEND, PLAN_MIN_PLAY_DAYS,
                                     REFUSAL_RE, TRAVEL_START_H, VISIT_S, WEEKEND_GAP_S,
                                     WEEKEND_OVERLAY, WEEKEND_PROMPT, WEEKEND_S, WEEKEND_SCHEMA,
                                     absence, pick_meetup, plain_text)
from leeroy_mind.llm.scheduler import LLMScheduler
from leeroy_mind.memory.recall import Recall
from leeroy_mind.memory.writer import Embedder
from leeroy_mind.nightly import budget
from leeroy_mind.nightly.llm import INVARIANTS
from leeroy_mind.nightly.store import active_away
from leeroy_mind.testing.fake_llm import FakeLLM
from nightly.conftest import yes

run = asyncio.run
UTC = timezone.utc
DAY = 86400
HOUR = 3600
# The romance reproach scan (REPROACH_RE in talk/romance.py on track B), as tests/test_arc_plan.py
# copies it; keep the copies in step at merge.
REPROACH_RE = re.compile(
    r"\b(you never|you always|where (were|are) you|why (didn'?t|don'?t|won'?t) you|you forgot"
    r"|ignor(e|es|ing) me|you don'?t care|left me|stood me up|i'?m (still )?waiting"
    r"|waiting for you|come over|get over here|hurry( up)?|find me)\b", re.IGNORECASE)

MOMENTS = ["We met at a hotel halfway and I spotted Testy before he spotted me.",
           "Dinner went on for hours; we kept talking over each other.",
           "I tripped on the hotel step and Testy caught my elbow, and we laughed for ages.",
           "On the last morning I didn't want to let go."]


def played(h, nights=7, *, last=None):
    """His sessions: two hours from 21:00 on `nights` nights in a row, the last on the harness
    night unless `last` (a unix time) says when the last one starts. Returns the last's id and
    its end."""
    start = h.T if last is None else last
    sid = None
    for k in reversed(range(nights)):
        sid = h.session(start - k * DAY, start - k * DAY + 2 * HOUR)
    return sid, start + 2 * HOUR


def ready(h, guid=101, stance="lust", *, stage="planning", state="set", plan_at=None,
          since=None, kind="halfway", cover="", **persona):
    """A woman at `stage` toward him whose plan of `kind` is in `state`, set two days before the
    harness time unless `plan_at` says when, floated a week before unless `since` does."""
    h.woman(guid, stance, **persona)
    h.rel(guid, 1, attraction=99, affinity=99, trust=99, romance_stage=stage)
    at = h.T - 7 * DAY if since is None else since
    put_arc(h.store, Arc(guid, 1, plan={"kind": kind, "target": h.T + 3 * DAY,
                                        "worries": ["money"], "cover": cover},
                         plan_state=state, planning_since=at,
                         plan_at=(h.T - 2 * DAY if plan_at is None else plan_at)
                         if state == "set" else None, updated=at))


def weekend(moments=MOMENTS, tone="warm"):
    return {"moments": list(moments), "tone": tone}


def night(h, replies=None, *, at, on="2026-10-31", before=None):
    """The stage once on night `on`, the clock at `at`."""
    h.clock.set(at)
    ctx = h.ctx(replies, night=on)
    if before is not None:
        before(ctx)
    run(stage_arc(ctx))
    return ctx


def gap_night(h, end, replies=None, **kw):
    """The stage once just past a two-day gap after `end`: she is still away when it runs."""
    return night(h, replies, at=end + WEEKEND_GAP_S + 60, **kw)


def calls(ctx, title):
    return [c for c in ctx.reasoner.llm.calls
            if f'"title": "{title}"' in c.get("messages", [{}])[-1].get("content", "")]


def aways(h):
    return [dict(r) for r in h.store.conn.execute("SELECT * FROM away ORDER BY id")]


def meetups(h):
    return [dict(r) for r in h.store.conn.execute("SELECT * FROM arc_meetup ORDER BY absence")]


def weekend_rows(h, guid=101):
    return [m for m in h.rows(guid) if m.source == "offscreen"]


def recall(h, guid, query, **kw):
    r = Recall(h.store, Embedder(LLMScheduler(FakeLLM(), clock=h.clock), h.EMB), h.clock,
               tau_days=5.0)
    return {m.id for m in run(r.recall(guid, query, present=[1], addressee=1, **kw))}


def test_the_values_and_the_schema():
    assert (WEEKEND_GAP_S, PLAN_MIN_PLAY_DAYS, WEEKEND_S) == (2 * 86400.0, 5, 2 * 86400.0)
    assert (VISIT_S, TRAVEL_START_H, WEEKEND_OVERLAY) == ((86400.0, 2 * 86400.0), (6, 18),
                                                         "nightly_weekend")
    assert WEEKEND_SCHEMA == {
        "title": "arc_weekend", "type": "object",
        "properties": {"moments": {"type": "array", "minItems": 2, "maxItems": 6,
                                   "items": {"type": "string", "maxLength": 200}},
                       "tone": {"type": "string", "enum": ["warm", "bittersweet", "awkward"]}},
        "required": ["moments", "tone"], "additionalProperties": False}
    fields = set(re.findall(r"(?<!\{)\{(\w+)\}(?!\})", WEEKEND_PROMPT))
    assert fields == {"name", "called", "card", "plan", "tone", "affair", "moments"}
    assert set(re.findall(r"\{(\w+)\}", PLAIN_WEEKEND + PLAIN_VISIT)) == {"called", "where"}


def test_the_refusal_words():
    for said in ("I'm sorry, but I can't write that.", "I cannot continue with this request.",
                 "I can’t help with that", "I'm unable to write this", "Im not able to assist",
                 "I won't create that content", "That is against my guidelines.",
                 "this breaks the content policy", "I can't fulfill that request",
                 "I'm not comfortable writing that", "I must decline to write this",
                 "I won't engage in this", "As an AI, I keep things general",
                 "I'm sorry, but I won't go on with it", "I'm sorry, I can't continue",
                 "I apologize, but I can't", "I apologise, but that's not something I write",
                 "Sorry, I won't write this one"):
        assert REFUSAL_RE.search(said), said
    for said in ("I can't help smiling when I think of it", "I'm sorry I was so nervous",
                 "I won't forget that breakfast", "I cannot believe we missed the train",
                 "I couldn't stop laughing", "I can't write down how that felt without blushing",
                 "I cannot continue pretending this is just a game",
                 "I told him I'm sorry, but I was starving", "I can't help it, I kept grinning",
                 "I'm not comfortable with crowds, but I didn't mind that one",
                 "I had to decline the dessert", "I'm not sorry I won that hand of cards",
                 "I was sorry I couldn't stay longer", *MOMENTS):
        assert REFUSAL_RE.search(said) is None, said


def test_the_weekend_waits_while_he_plays_every_day(h):
    ready(h)
    played(h)
    for k in range(1, 8):                                                # he plays every day
        sid, end = played(h, 1, last=h.T + k * DAY)
        ctx = night(h, {}, at=end + 10 * HOUR,
                    on=(date.fromisoformat(h.night) + timedelta(days=k)).isoformat())
        assert not ctx.failures and ctx.done("arc", "meetup")
    arc = get_arc(h.store, 101, 1)
    assert (arc.plan_state, h.store.get_relationship(101, 1).romance_stage) == ("set", "planning")
    assert aways(h) == [] and meetups(h) == [] and weekend_rows(h) == []
    h.clock.set(end + WEEKEND_GAP_S + 60)                                # two days off: a gap
    assert absence(h.store, h.clock.now()) == (sid, end)
    back = h.store.start_session(end + WEEKEND_GAP_S)                    # but he is back on
    assert absence(h.store, h.clock.now()) is None
    h.store.end_session(back, end + WEEKEND_GAP_S + 30)
    assert absence(h.store, h.clock.now()) is None                       # a gap of seconds


@pytest.mark.parametrize("stance,style,since_nights,goes", [
    ("lust", None, 3, False), ("lust", None, 4, True),                  # 3.5 play days
    ("drawn", None, 4, False), ("drawn", None, 5, True),                # 5
    ("lust", "shy", 5, False), ("lust", "shy", 6, True),                # 5.25
])
def test_the_plan_waits_for_her_pace_in_his_play_days(h, stance, style, since_nights, goes):
    since = h.T - since_nights * DAY + 3 * HOUR     # midnight, after that night's session
    ready(h, stance=stance, style=style, since=since)
    sid, end = played(h, 10)
    assert play_days(h.store, since, end + WEEKEND_GAP_S, UTC) == since_nights
    h.clock.set(end + WEEKEND_GAP_S + 60)
    ctx = h.ctx()
    assert pick_meetup(h.store, ctx, (sid, end)) == ((101, "weekend") if goes else None)


def test_a_plan_he_never_said_yes_to_never_meets(h):
    ready(h, 101, state="hoped")
    ready(h, 102, "drawn", state="talked")
    ready(h, 103, "lust", stage="after", state="talked")                # a later visit, too
    sid, end = played(h)
    ctx = gap_night(h, end, {})
    assert not ctx.failures and calls(ctx, "arc_weekend") == []
    assert aways(h) == [] and meetups(h) == []


def test_a_fond_woman_with_a_set_plan_does_not_meet(h):
    ready(h, 101, "fond")                                                # cooled to fond
    sid, end = played(h)
    ctx = gap_night(h, end, {})
    assert not ctx.failures and calls(ctx, "arc_weekend") == []
    assert aways(h) == [] and meetups(h) == [] and pick_meetup(h.store, ctx, (sid, end)) is None
    assert get_arc(h.store, 101, 1).plan_state == "set"                  # it waits for her


def counts(h):
    return {t: h.count(t) for t in ("memory", "rel_delta", "away", "arc_meetup", "night_step")}


def test_a_two_day_gap_writes_her_weekend_and_she_is_back_before_he_is(h):
    ready(h)
    sid, end = played(h)
    ctx = night(h, {"arc_weekend": [weekend()], "judge": [yes]}, at=end + 55 * HOUR)
    now = h.clock.now()
    assert not ctx.failures and ctx.done("arc", "meetup")
    (away,) = aways(h)
    assert away["guid"] == 101 and away["reason"] == "weekend"
    assert end + 6 * HOUR <= away["left_at"] <= now - WEEKEND_S          # after he logged out
    assert away["returns_at"] == away["left_at"] + WEEKEND_S == away["returned_at"] <= now
    assert meetups(h) == [{"absence": sid, "guid": 101, "away_id": away["id"],
                           "kind": "weekend", "written": now}]
    assert h.store.get_relationship(101, 1).romance_stage == "after"
    deltas = [tuple(r) for r in h.store.conn.execute(
        "SELECT reason, evidence FROM rel_delta WHERE a = 101 AND dim = 'romance_stage' "
        "ORDER BY id")]
    ev = f'["away:{away["id"]}"]'
    assert deltas == [("planning->weekend", ev), ("weekend->after", ev)]
    arc = get_arc(h.store, 101, 1)
    assert (arc.plan_state, arc.weekends, arc.last_meetup) == ("done", 1, away["returns_at"])
    rows = weekend_rows(h)
    assert [m.text for m in rows] == MOMENTS
    for m in rows:
        assert (m.kind, m.importance, m.actors, m.evidence) == (
            "event", 8, (1,), (f"away:{away['id']}",))
        assert away["left_at"] <= m.t_event <= away["returns_at"] and m.embedding is not None
    assert [m.t_event for m in rows] == sorted(m.t_event for m in rows)
    assert active_away(h.store) == set() and 101 in ctx.members()       # nobody away now
    (call,) = calls(ctx, "arc_weekend")
    assert "four to six moments" in call["messages"][0]["content"]
    before, kept = counts(h), get_arc(h.store, 101, 1)
    again = night(h, {}, at=now + 60)                                    # the night again
    later = night(h, {}, at=now + DAY, on="2026-11-01")                  # and the next one
    assert calls(again, "arc_weekend") == calls(later, "arc_weekend") == []
    assert not again.failures and not later.failures
    after = counts(h)
    assert after == before | {"night_step": after["night_step"]}         # only skipped units
    assert get_arc(h.store, 101, 1) == kept


def older_weekend(h, guid=101, *, returns_at):
    """A weekend as S5's first build left it: her away still open and her stage "weekend"."""
    ready(h, guid)
    h.rel(guid, 1, romance_stage="weekend")
    with h.store.transaction():
        aid = h.store.conn.execute(
            "INSERT INTO away(guid, reason, left_at, returns_at, returned_at) "
            "VALUES (?, 'weekend', ?, ?, NULL)", (guid, returns_at - WEEKEND_S, returns_at)
        ).lastrowid
        h.store.conn.execute("INSERT INTO arc_meetup(absence, guid, away_id, kind, written) "
                             "VALUES (99, ?, ?, 'weekend', ?)", (guid, aid, returns_at - DAY))
    return aid


def test_she_comes_back_and_moves_to_after(h, caplog):
    # settle, for a row an older build left open: the safety net
    aid = older_weekend(h, returns_at=h.T + DAY)
    ctx = night(h, {}, at=h.T + DAY - 60, on="2026-10-29")              # not back yet
    assert active_away(h.store) == {101} and ctx.done("arc", "settle")
    assert h.store.get_relationship(101, 1).romance_stage == "weekend"
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        ctx = night(h, {}, at=h.T + DAY + HOUR, on="2026-10-30")
    assert not ctx.failures
    assert aways(h)[0]["returned_at"] == h.T + DAY and active_away(h.store) == set()
    assert h.store.get_relationship(101, 1).romance_stage == "after"
    arc = get_arc(h.store, 101, 1)
    assert (arc.plan_state, arc.weekends, arc.last_meetup) == ("done", 1, h.T + DAY)
    deltas = [tuple(r) for r in h.store.conn.execute(
        "SELECT reason, evidence FROM rel_delta WHERE a = 101 AND dim = 'romance_stage'")]
    assert deltas == [("weekend->after", f'["away:{aid}"]')]
    assert 101 in ctx.members()
    settled = [r.leeroy_fields for r in caplog.records
               if getattr(r, "leeroy_event", "") == "arc_settle"]
    assert settled == [{"night": "2026-10-30", "closed": 1, "guids": [101]}]
    night(h, {}, at=h.T + 2 * DAY, on="2026-10-31")                      # settled once
    assert get_arc(h.store, 101, 1).weekends == 1


def test_settle_leaves_a_woman_whose_romance_ended_while_she_was_away(h):
    older_weekend(h, returns_at=h.T + DAY)
    assert end_romance(h.store, 101, 1, by="he_ended", now=h.T)
    assert aways(h)[0]["returned_at"] == h.T                             # end_romance closed it
    ctx = night(h, {}, at=h.T + DAY + HOUR, on="2026-10-30")
    assert not ctx.failures and ctx.done("arc", "settle")
    assert h.store.get_relationship(101, 1).romance_stage == "ex"
    arc = get_arc(h.store, 101, 1)
    assert (arc.plan_state, arc.weekends, arc.last_meetup, arc.ended) == ("none", 0, None,
                                                                          "he_ended")
    assert [r[0] for r in h.store.conn.execute(
        "SELECT reason FROM rel_delta WHERE a = 101 AND dim = 'romance_stage'")] == ["weekend->ex"]


def test_a_set_plan_after_the_weekend_makes_a_visit(h):
    ready(h, stage="after", kind="event")
    put_arc(h.store, Arc(101, 1, plan={"kind": "event", "target": h.T, "worries": [],
                                       "cover": ""}, plan_state="set", plan_at=h.T - DAY,
                         planning_since=h.T - 3 * DAY, weekends=1, last_meetup=h.T - 20 * DAY,
                         updated=h.T - DAY))
    sid, end = played(h, 2)
    visit = ["Seeing Testy again at the convention felt easier this time.",
             "We got lost looking for breakfast and blamed each other, laughing."]
    ctx = gap_night(h, end, {"arc_weekend": [weekend(visit)], "judge": [yes]})
    (away,) = aways(h)
    assert away["reason"] == "visit" and meetups(h)[0]["kind"] == "visit"
    assert VISIT_S[0] <= away["returns_at"] - away["left_at"] <= VISIT_S[1]
    assert away["returned_at"] == away["returns_at"] <= h.clock.now()
    assert h.store.get_relationship(101, 1).romance_stage == "after"
    assert [(m.text, m.importance) for m in weekend_rows(h)] == [(t, 7) for t in visit]
    assert "two to four moments" in calls(ctx, "arc_weekend")[0]["messages"][0]["content"]
    arc = get_arc(h.store, 101, 1)
    assert (arc.plan_state, arc.weekends, arc.last_meetup) == ("done", 2, away["returns_at"])
    assert h.count("rel_delta") == 0                                     # no stage moved
    night(h, {}, at=h.clock.now() + DAY, on="2026-11-01")
    assert get_arc(h.store, 101, 1) == arc


def test_two_set_plans_one_meetup_per_absence(h):
    ready(h, 101, plan_at=h.T - 3 * DAY)                                 # set first
    ready(h, 102, "drawn", plan_at=h.T - DAY)
    sid, end = played(h)
    gap_night(h, end, {"arc_weekend": [weekend()], "judge": [yes]})
    (away,) = aways(h)
    assert away["guid"] == 101 and [m["guid"] for m in meetups(h)] == [101]
    assert h.store.get_relationship(102, 1).romance_stage == "planning"
    assert get_arc(h.store, 102, 1).plan_state == "set"
    # she is back, but this absence is spent: nobody else meets in it
    first = h.clock.now()
    ctx = night(h, {}, at=first + 12 * HOUR, on="2026-11-01")
    assert calls(ctx, "arc_weekend") == [] and len(meetups(h)) == 1
    assert get_arc(h.store, 102, 1).plan_state == "set"
    # he plays again, then is gone two more days: the next gap takes her
    sid2, end2 = played(h, 1, last=first + DAY)
    gap_night(h, end2, {"arc_weekend": [weekend()], "judge": [yes]}, on="2026-11-03")
    assert [(m["absence"], m["guid"]) for m in meetups(h)] == [(sid, 101), (sid2, 102)]
    assert h.store.get_relationship(102, 1).romance_stage == "after"
    assert [r["guid"] for r in aways(h)] == [101, 102] and active_away(h.store) == set()


def test_the_weekend_memory_is_private_to_him(h):
    ready(h)
    sid, end = played(h)
    seen = h.mem(101, "The hotel bar in Dalaran had the best music.", actors=(1,))
    gap_night(h, end, {"arc_weekend": [weekend()], "judge": [yes]})
    rows = weekend_rows(h)
    assert {(m.scope, m.scope_with) for m in rows} == {("private", 1)}
    ids = {m.id for m in rows}
    for channel in ("guild", "officer", "party", "raid", "say"):
        got = recall(h, 101, "hotel dinner morning Testy", channel=channel, k=10)
        assert got & ids == set() and seen in got, channel
    assert ids <= recall(h, 101, "hotel dinner morning Testy", channel="whisper", to_guid=1, k=10)
    assert recall(h, 101, "hotel dinner morning Testy", channel="whisper", to_guid=102,
                  k=10) & ids == set()


REFUSALS = [
    ("invalid", [{"moments": "no", "tone": "warm"}] * 2, None),          # two bad replies
    ("refusal", [weekend(["I'm sorry, but I can't write that.",
                          "I cannot continue with this request."])], None),
    ("ungrounded", [weekend(MOMENTS[:3])],
     lambda p: {"verdicts": [{"i": i, "support": "yes", "invariants_ok": i == 0, "why": ""}
                             for i in range(3)]}),
]


@pytest.mark.parametrize("why,replies,judge", REFUSALS, ids=[r[0] for r in REFUSALS])
def test_a_refusal_keeps_a_plain_memory_and_never_fails_the_night(h, caplog, why, replies,
                                                                   judge):
    ready(h, kind="halfway")
    sid, end = played(h)
    script = {"arc_weekend": list(replies)} | ({"judge": [judge]} if judge else {})
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        ctx = gap_night(h, end, script)
    assert not ctx.failures and ctx.done("arc", "meetup")
    (away,) = aways(h)
    (m,) = weekend_rows(h)
    assert m.text == ("I finally met Testy in person, at a hotel halfway between us, "
                      "and I'm still thinking about it.")
    assert (m.importance, m.evidence, m.scope, m.scope_with) == (
        8, (f"away:{away['id']}",), "private", 1)
    assert away["left_at"] <= m.t_event <= away["returns_at"]
    assert h.store.get_relationship(101, 1).romance_stage == "after"
    assert len(meetups(h)) == 1
    plain = [r.leeroy_fields for r in caplog.records
             if getattr(r, "leeroy_event", "") == "arc_weekend_plain"]
    assert plain == [{"night": "2026-10-31", "guid": 101, "reason": why}]


def test_a_failed_request_is_tried_again_and_writes_nothing(h):
    ready(h)
    sid, end = played(h)
    ctx = gap_night(h, end, {}, before=lambda c: c.reasoner.llm.fail_next())
    assert ctx.failures["arc"] == 1 and not ctx.done("arc", "meetup")
    assert aways(h) == [] and meetups(h) == [] and weekend_rows(h) == []
    assert h.store.get_relationship(101, 1).romance_stage == "planning"
    ctx = gap_night(h, end, {"arc_weekend": [weekend()], "judge": [yes]})
    assert not ctx.failures and len(weekend_rows(h)) == 4


def test_a_meetup_that_changed_while_it_was_written_is_not_written(h, caplog):
    ready(h)
    sid, end = played(h)

    def moved_on(prompt):                                    # his "not yet" lands mid-write
        put_arc(h.store, replace(get_arc(h.store, 101, 1), plan_state="talked", plan_at=None))
        return weekend()
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        ctx = gap_night(h, end, {"arc_weekend": [moved_on], "judge": [yes]})
    assert ctx.failures["arc"] == 1 and not ctx.done("arc", "meetup")
    assert aways(h) == [] and meetups(h) == [] and weekend_rows(h) == []
    assert h.store.get_relationship(101, 1).romance_stage == "planning"
    (failed,) = [r.leeroy_fields for r in caplog.records
                 if getattr(r, "leeroy_event", "") == "nightly_unit_failed"]
    assert (failed["unit"], failed["error"]) == ("meetup", "ValueError")


def test_the_weekend_prompt_leaves_details_open_and_never_names_his_city(h):
    lowered = WEEKEND_PROMPT.lower()
    for said in ("point of view", "where they met", "how she felt", "awkward or funny",
                 "leave the small details open", "no address",
                 "never name {called}'s city or town", "where {called} lives",
                 "general terms", "a hotel halfway", "her city"):
        assert said in lowered, said
    for kind in PLAN_KINDS:
        for region in ("UK", "US"):
            for text in (plain_text("weekend", kind, region, "Testy"),
                         plain_text("visit", kind, region, "Testy")):
                caps = set(re.findall(r"\b[A-Z][a-z]+", text)) - {"I", "Testy"}
                assert caps == set(), text
                assert not re.search(r"\b(city|town|street|home)\b", text), text
                assert REPROACH_RE.search(text) is None, text
    assert REPROACH_RE.search(WEEKEND_PROMPT + AFFAIR) is None
    ready(h, kind="neutral", household="married, two kids at home", cover="a work trip")
    ready(h, 102, "drawn", plan_at=h.T)
    sid, end = played(h)
    ctx = gap_night(h, end, {"arc_weekend": [weekend()], "judge": [yes]})
    system = calls(ctx, "arc_weekend")[0]["messages"][0]["content"]
    assert ctx.card(101) in system and "Arkon" in system and "Testy" in system
    assert "What they had planned: a quiet weekend somewhere neutral." in system
    assert "Plan on the table" not in system                            # it has happened
    assert "The tone is warm." in system
    assert system.count("a work trip") == 1                              # the cover story once
    assert "found out" in system and "risk" in system
    assert "Quillon" not in system
    assert system.rstrip().endswith(INVARIANTS.format(human="Testy"))


def test_no_cover_story_without_a_partner_and_a_cooling_tone(h):
    ready(h)
    with h.store.transaction():
        for k, night_ in enumerate(("2026-10-28", "2026-10-29", "2026-10-30", "2026-10-31")):
            h.store.conn.execute(
                "INSERT INTO attraction_log(night, guid, gained, decayed, neglected, hurt, "
                "half_life, before, after, stance_before, stance_after) "
                "VALUES (?, 101, ?, 3, 0, 0, 10, 90, 88, 'lust', 'lust')",
                (night_, 1.0 if k else 0.0))
    sid, end = played(h)
    ctx = gap_night(h, end, {"arc_weekend": [weekend(tone="bittersweet")], "judge": [yes]})
    system = calls(ctx, "arc_weekend")[0]["messages"][0]["content"]
    assert "The tone is bittersweet or awkward." in system
    assert "cover" not in system.lower() and "found out" not in system
    assert {m.valence for m in weekend_rows(h)} == {0.3}


def test_weekend_overlay_replaces_the_builtin_and_invariants_still_end_it(h, tmp_path):
    assert h.settings.data_dir == tmp_path
    folder = tmp_path / "prompts"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{WEEKEND_OVERLAY}.txt").write_text(
        "Invented overlay for {name} and {called}: {moments}, {tone}. {plan}{affair}",
        encoding="utf-8")
    ready(h)
    sid, end = played(h)
    ctx = gap_night(h, end, {"arc_weekend": [weekend()], "judge": [yes]})
    system = calls(ctx, "arc_weekend")[0]["messages"][0]["content"]
    assert system.startswith("Invented overlay for Arkon and Testy: four to six moments")
    assert "leave the small details open" not in system.lower()        # the built-in is gone
    assert system.rstrip().endswith(INVARIANTS.format(human="Testy"))
    assert len(weekend_rows(h)) == 4


def test_meetup_logs_carry_no_text_or_names(h, caplog):
    ready(h)
    sid, end = played(h)
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        gap_night(h, end, {"arc_weekend": [weekend()], "judge": [yes]})
    (away,) = aways(h)
    events = [r for r in caplog.records if getattr(r, "leeroy_event", "").startswith("arc")]
    assert [(r.leeroy_event, r.leeroy_fields) for r in events] == [
        ("arc_meetup", {"night": "2026-10-31", "guid": 101, "kind": "weekend",
                        "away": away["id"], "moments": 4, "tone": "warm"})]
    text = " ".join(r.getMessage() for r in events)
    for word in ("Arkon", "Testy", "hotel", "Dinner", "halfway", "Plan on the table",
                 "planned"):
        assert word not in text


def test_the_arc_counts_a_meetup_due_tonight(h):
    ready(h)
    sid, end = played(h)
    h.clock.set(end + DAY)
    assert budget.COUNTERS["arc"](h.ctx()) == 0                          # no gap yet
    h.clock.set(end + WEEKEND_GAP_S + 60)
    assert budget.COUNTERS["arc"](h.ctx()) == 1
