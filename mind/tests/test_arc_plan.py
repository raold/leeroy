"""Track S3: the arc's stored state and the plan a woman proposes."""
import itertools
import random
import re

from conftest import make_persona
from leeroy_mind.arc.plan import PLAN_KINDS, has_partner, plan_lines, plan_summary, propose_plan
from leeroy_mind.arc.state import Arc, add_event, get_arc, put_arc
from leeroy_mind.human import THEY_FORMS, _FORMS

NOW = 1_800_000_000.0
DAY = 86400
FORMS = [_FORMS["he"], _FORMS["she"], THEY_FORMS]
# The romance reproach scan: REPROACH_RE in talk/romance.py on track B (B3). Copied here so S3
# stands alone; keep the two in step at merge.
REPROACH_RE = re.compile(
    r"\b(you never|you always|where (were|are) you|why (didn'?t|don'?t|won'?t) you|you forgot"
    r"|ignor(e|es|ing) me|you don'?t care|left me|stood me up|i'?m (still )?waiting"
    r"|waiting for you|come over|get over here|hurry( up)?|find me)\b", re.IGNORECASE)


def woman(region="US", household="Lives alone with a cat.", **over):
    return make_persona(101, "Arkon", region=region, household=household, **over)


def every_text():
    out = []
    for region, kind, cover, forms, state in itertools.product(
            ("UK", "US"), PLAN_KINDS, ("", "a work trip"), FORMS, ("hoped", "talked", "set")):
        for target in (NOW + 12 * DAY, NOW - DAY):
            plan = {"kind": kind, "target": target, "worries": ["money", "time off"],
                    "cover": cover}
            out += plan_lines(plan, state, now=NOW, forms=forms, region=region)
            out.append(plan_summary(plan, region))
    return out


def test_plans_never_name_a_city_and_a_uk_woman_flies():
    caps = set()
    for text in every_text():
        for sentence in re.split(r"(?<=[.;:]) ", text):
            caps |= set(re.findall(r"\b[A-Z]\w*", sentence)[1:])
        assert "UK" not in text and "US" not in text
    assert caps <= {"BlizzCon"}
    uk = {"kind": "she_travels", "target": NOW + 12 * DAY, "worries": [], "cover": ""}
    assert "a flight over" in plan_lines(uk, "talked", now=NOW, forms=_FORMS["he"], region="UK")[0]
    assert "flight" not in plan_lines(uk, "talked", now=NOW, forms=_FORMS["he"], region="US")[0]
    rng = random.Random(3)
    kinds = {propose_plan(woman("UK"), rng, now=NOW)["kind"] for _ in range(80)}
    assert kinds == {"she_travels", "event", "neutral"}
    us = {propose_plan(woman("US"), rng, now=NOW)["kind"] for _ in range(80)}
    assert us == set(PLAN_KINDS)


def test_proposals_are_in_range_and_avoid_the_excluded_kind():
    rng = random.Random(1)
    for _ in range(60):
        plan = propose_plan(woman(), rng, now=NOW, exclude="event")
        assert plan["kind"] != "event" and plan["cover"] == ""
        assert 10 * DAY <= plan["target"] - NOW <= 18 * DAY
        assert 1 <= len(plan["worries"]) <= 2 and set(plan["worries"]) <= {
            "money", "time off", "the home front"}


def test_plan_lines_by_state_and_the_countdown():
    plan = {"kind": "event", "target": NOW + 12 * DAY, "worries": ["money"], "cover": ""}
    lines = lambda state, now=NOW, forms=_FORMS["he"]: plan_lines(
        plan, state, now=now, forms=forms, region="US")
    assert lines("hoped") == ["BlizzCon is a hope you've floated; you leave it with him."]
    assert "them" in lines("hoped", forms=THEY_FORMS)[0]
    assert lines("talked") == ["You two have talked about BlizzCon, but nothing is settled."]
    assert lines("set")[0] == "It's happening: BlizzCon, about 12 days off."
    assert lines("set", now=NOW + 12 * DAY)[0] == "It's happening: BlizzCon, any weekend now."
    assert lines("set", now=NOW + 30 * DAY)[0].endswith("any weekend now.")
    assert lines("set")[1] == "On your mind: money, nerves, what you'll wear."
    assert lines("none") == lines("done") == []
    assert len(lines("set")) <= 2


def test_no_plan_text_reproaches_or_summons():
    texts = every_text()
    assert len(texts) > 100
    assert [t for t in texts if REPROACH_RE.search(t)] == []
    assert REPROACH_RE.search("you never push it")  # the scan does catch the rejected wording


def test_has_partner_reads_her_household():
    for text in ("Lives with her husband and a dog.", "Married, two cats.",
                 "Her boyfriend moved in.", "Living with her Partner.", "Engaged to her fiancée."):
        assert has_partner(woman(household=text)), text
    for text in ("Lives alone with a cat.", "Shares a flat with two friends.", "",
                 "Her parents' spare room."):
        assert not has_partner(woman(household=text)), text


def test_a_woman_with_a_partner_gets_a_cover_story():
    rng = random.Random(5)
    plan = propose_plan(woman(household="Lives with her husband."), rng, now=NOW)
    assert plan["cover"]
    for state in ("hoped", "talked", "set"):
        got = plan_lines(plan, state, now=NOW, forms=_FORMS["he"], region="US")
        assert plan["cover"] in " ".join(got) and len(got) <= 2
    assert plan["cover"] in plan_summary(plan, "US")
    solo = propose_plan(woman(), rng, now=NOW)
    assert solo["cover"] == "" and "over story" not in plan_summary(solo, "US")


def test_the_arc_row_round_trips_and_keeps_twelve_events(store):
    assert get_arc(store, 101, 1) == Arc(101, 1)
    arc = Arc(101, 1, plan={"kind": "event", "target": NOW}, plan_state="hoped", plan_at=NOW,
              weekends=2, partner="together", updated=NOW)
    for i in range(15):
        arc = add_event(arc, "e", NOW + i)
    assert len(arc.events) == 12 and arc.events[0] == ("e", NOW + 3)
    put_arc(store, arc)
    assert get_arc(store, 101, 1) == arc
    put_arc(store, Arc(101, 1, weekends=3, updated=NOW))
    assert get_arc(store, 101, 1).weekends == 3


def test_has_partner_ignores_exes_and_non_partners():
    for text in ("Lives with her ex-husband's old dog.", "Her ex-wife visits.", "An ex-boyfriend nearby.",
                 "Ex-girlfriend next door.", "Her ex-partner has the house.", "Not married.",
                 "Never married, lives alone.", "Divorced, lives alone.", "Widowed, two cats.",
                 "Runs the shop with a business partner."):
        assert not has_partner(woman(household=text)), text
    assert has_partner(woman(household="Divorced once, now lives with her husband."))
    assert has_partner(woman(household="Married to her boyfriend of ten years."))


def test_a_woman_living_alone_never_worries_about_the_home_front():
    for household, front in (("Lives alone with a cat.", False), ("Lives with her husband.", True)):
        seen = set()
        rng = random.Random(7)
        for _ in range(200):
            seen |= set(propose_plan(woman(household=household), rng, now=NOW)["worries"])
        assert ("the home front" in seen) is front
        assert {"money", "time off"} <= seen


def test_the_countdown_is_singular_for_one_day():
    plan = {"kind": "event", "target": NOW + DAY, "worries": [], "cover": ""}
    got = lambda now: plan_lines(plan, "set", now=now, forms=_FORMS["he"], region="US")[0]
    assert got(NOW) == "It's happening: BlizzCon, about 1 day off."
    assert got(NOW - DAY) == "It's happening: BlizzCon, about 2 days off."
