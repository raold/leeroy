"""Track S4: the night stage `arc`, the plan she floats and what he says about it."""
import asyncio
import logging
from datetime import timezone

import pytest

from leeroy_mind.arc.night import ENDING_RE, NO_PLAN, READ_PROMPT, READ_SCHEMA, stage_arc
from leeroy_mind.arc.plan import PLAN_KINDS, plan_summary
from leeroy_mind.arc.state import Arc, get_arc, put_arc
from leeroy_mind.nightly import budget
from leeroy_mind.nightly.llm import INVARIANTS
from leeroy_mind.nightly.pipeline import STAGES
from leeroy_mind.nightly.settings import night_bounds

run = asyncio.run
UTC = timezone.utc
DAY = 86400
HOUR = 3600


def plan(h, kind="event", **over):
    return {"kind": kind, "target": h.T + 12 * DAY, "worries": ["money"], "cover": ""} | over


def on_the_arc(h, guid=101, stance="lust", *, stage="planning", state="hoped", kind="event",
               since=None, **persona):
    """A woman at `stage` toward him with an arc row: a plan of `kind` in `state`, floated
    three days before the harness time unless `since` says when."""
    h.woman(guid, stance, **persona)
    h.rel(guid, 1, attraction=99, affinity=99, trust=99, romance_stage=stage)
    at = h.T - 3 * DAY if since is None else since
    put_arc(h.store, Arc(guid, 1, plan=plan(h, kind) if state != "none" else {},
                         plan_state=state, planning_since=at, updated=at))


def whisper(h, t, text, to=101):
    h.his(t, text, channel="whisper", to=to)


def answer(h, t, guid, text, trigger, *, channel="guild", dropped=None):
    """Her line answering his line `trigger`, as the pipeline stores a said reply."""
    h.clock.set(t)
    sid = h.store.open_session()
    if dropped is None:
        h.store.insert_chat_line(h._chat(t, guid, text, channel, 0), session_id=sid,
                                 origin="mind")
    return h.store.log_utterance(t, guid, channel, "reply", text, [], dropped, sid,
                                 trigger_line=trigger)


def night(h, replies=None, **kw):
    """The stage once, the morning after the harness night's window."""
    h.clock.set(night_bounds(h.night, UTC)[1] + HOUR)
    ctx = h.ctx(replies, **kw)
    run(stage_arc(ctx))
    assert not ctx.failures
    return ctx


def by_name(answers):
    """A read's reply chosen by the woman the lines are with ({name: reply}): the first line
    of the read's question names the two of them."""
    def reply(text):
        return next(r for name, r in answers.items() if name in text.split("\n", 1)[0])
    return reply


def read(plan="none", breakup=False):
    return {"plan": plan, "breakup": breakup}


def reads(ctx):
    return [c for c in ctx.reasoner.llm.calls
            if '"title": "arc_read"' in c.get("messages", [{}])[-1].get("content", "")]


def test_arc_stage_sits_after_romance_before_jealousy():
    names = [s.name for s in STAGES]
    assert names.index("romance") + 1 == names.index("arc") < names.index("jealousy")
    assert next(s for s in STAGES if s.name == "arc").model == "reflection"


def test_read_schema_and_the_ending_words():
    assert READ_SCHEMA == {
        "title": "arc_read", "type": "object",
        "properties": {"plan": {"type": "string",
                                "enum": ["yes", "not_yet", "elsewhere", "later", "none"]},
                       "breakup": {"type": "boolean"}},
        "required": ["plan", "breakup"], "additionalProperties": False}
    for said in ("I think we should break up", "Breaking up is hard", "it's over", "its over",
                 "we're done", "were done", "I want to end this", "let's end it",
                 "time to end us", "I'm not doing this anymore", "you should see other people"):
        assert ENDING_RE.search(said), said
    for said in ("break a leg", "the raid's over", "endgame gear", "the end of it",
                 "doing this again tomorrow"):
        assert ENDING_RE.search(said) is None, said


def test_a_woman_at_planning_gets_a_plan_without_the_model(h):
    h.woman(101, "lust")
    h.rel(101, 1, attraction=99, affinity=99, trust=99, romance_stage="planning")
    h.woman(102, "drawn", region="US")
    h.rel(102, 1, attraction=99, affinity=99, trust=99, romance_stage="planning")
    h.woman(103, "fond")                                                 # fond: never a plan
    h.rel(103, 1, romance_stage="planning")
    h.woman(104, "lust")                                                 # not at planning yet
    h.rel(104, 1, romance_stage="confession")
    h.persona(105, stance="lust")                                        # a man
    h.rel(105, 1, romance_stage="planning")
    ctx = night(h)
    now = h.clock.now()
    for g in (101, 102):
        arc = get_arc(h.store, g, 1)
        assert (arc.plan_state, arc.planning_since, arc.updated) == ("hoped", now, now)
        assert arc.plan["kind"] in PLAN_KINDS
        assert 10 * DAY <= arc.plan["target"] - now <= 18 * DAY
    assert get_arc(h.store, 101, 1).plan["kind"] != "halfway"           # she is in the UK
    for g in (103, 104, 105):
        assert get_arc(h.store, g, 1).plan_state == "none"
    assert ctx.reasoner.calls == 0 and ctx.done("arc", "propose")
    first = get_arc(h.store, 101, 1)
    night(h)                                                             # done: nothing again
    assert get_arc(h.store, 101, 1) == first


def test_a_plan_floated_tonight_is_not_read_against_older_lines(h):
    h.woman(101, "lust")
    h.rel(101, 1, attraction=99, affinity=99, trust=99, romance_stage="planning")
    whisper(h, h.T + HOUR, "yes, absolutely")
    ctx = night(h, {"arc_read": [read("yes")]})
    arc = get_arc(h.store, 101, 1)
    assert arc.plan_state == "hoped" and arc.plan_at is None
    assert NO_PLAN in reads(ctx)[0]["messages"][0]["content"]


def test_his_yes_sets_the_plan(h):
    on_the_arc(h)
    whisper(h, h.T + HOUR, "BlizzCon with you? yes, let's do it")
    h.say(h.T + HOUR + 60, 101, "Really? I'm booking it", channel="whisper", to=1)
    before = get_arc(h.store, 101, 1)
    ctx = night(h, {"arc_read": [read("yes")]})
    now = h.clock.now()
    arc = get_arc(h.store, 101, 1)
    assert (arc.plan_state, arc.plan_at, arc.plan, arc.updated) == ("set", now, before.plan, now)
    assert arc.planning_since == before.planning_since
    (call,) = reads(ctx)
    system, user = (m["content"] for m in call["messages"])
    assert plan_summary(before.plan, "UK") in system and "Arkon" in system
    assert system.rstrip().endswith(INVARIANTS.format(human="Testy"))
    assert user.index("Testy: BlizzCon with you?") < user.index("Arkon: Really? I'm booking it")
    ctx = night(h, {"arc_read": [read("yes")]})                          # the night is done
    assert reads(ctx) == [] and get_arc(h.store, 101, 1) == arc


def test_a_second_yes_keeps_her_place_in_line(h):
    on_the_arc(h, state="set")
    with h.store.transaction():
        h.store.conn.execute("UPDATE romance_arc SET plan_at = ? WHERE guid = 101", (h.T - DAY,))
    whisper(h, h.T + HOUR, "still on, yes")
    night(h, {"arc_read": [read("yes")]})
    arc = get_arc(h.store, 101, 1)
    assert (arc.plan_state, arc.plan_at) == ("set", h.T - DAY)


def test_not_yet_and_later_push_the_target(h):
    on_the_arc(h, 101)
    on_the_arc(h, 102, "drawn", state="set")
    whisper(h, h.T + HOUR, "not so soon, maybe a bit after", to=101)
    whisper(h, h.T + 2 * HOUR, "can we push it way back", to=102)
    night(h, {"arc_read": [by_name({"Arkon": read("not_yet"), "Belra": read("later")})] * 2})
    first, second = get_arc(h.store, 101, 1), get_arc(h.store, 102, 1)
    assert (first.plan_state, first.plan["target"]) == ("talked", h.T + 19 * DAY)
    assert (second.plan_state, second.plan["target"]) == ("talked", h.T + 26 * DAY)
    assert second.plan_at is None                                        # no longer set
    assert first.plan["kind"] == second.plan["kind"] == "event"


def test_elsewhere_repicks_the_kind(h):
    on_the_arc(h, kind="event")
    whisper(h, h.T + HOUR, "not a con, somewhere quieter")
    night(h, {"arc_read": [read("elsewhere")]})
    now = h.clock.now()
    arc = get_arc(h.store, 101, 1)
    assert arc.plan_state == "talked"
    assert arc.plan["kind"] in set(PLAN_KINDS) - {"event", "halfway"}   # UK: never halfway
    assert 10 * DAY <= arc.plan["target"] - now <= 18 * DAY


def test_no_engagement_keeps_it_a_hope(h):
    on_the_arc(h, 101)
    on_the_arc(h, 102, "drawn")
    whisper(h, h.T + HOUR, "how was work today", to=101)                # nothing about the plan
    before = {g: get_arc(h.store, g, 1) for g in (101, 102)}
    ctx = night(h, {"arc_read": [read("none")]})
    assert len(reads(ctx)) == 1                                          # nothing from him to 102
    assert {g: get_arc(h.store, g, 1) for g in (101, 102)} == before
    assert ctx.done("arc", "read:101") and not ctx.done("arc", "read:102")


BREAKUPS = [
    ("planning", "hoped", "I think we should break up", True, "ex"),
    ("planning", "hoped", "I think we should break up", False, "planning"),   # his words alone
    ("planning", "hoped", "you're sweet, goodnight", True, "planning"),      # the model alone
    ("after", "done", "it's over, I'm sorry", True, "ex"),                  # after the weekend
]


@pytest.mark.parametrize("stage,state,said,model,stage_after", BREAKUPS)
def test_a_breakup_needs_his_words_and_the_models_reading(h, stage, state, said, model,
                                                          stage_after):
    on_the_arc(h, stage=stage, state=state)
    whisper(h, h.T + HOUR, said)
    before = get_arc(h.store, 101, 1)
    ctx = night(h, {"arc_read": [read("none", model)]})
    arc = get_arc(h.store, 101, 1)
    assert h.store.get_relationship(101, 1).romance_stage == stage_after
    if stage_after == "ex":
        assert (arc.ended, arc.plan, arc.plan_state) == ("he_ended", {}, "none")
        row = h.store.conn.execute("SELECT reason FROM rel_delta WHERE a = 101 "
                                   "AND dim = 'romance_stage'").fetchone()
        assert row[0] == f"{stage}->ex"
    else:
        assert arc == before
    if state == "done":                                                  # no plan to read
        assert NO_PLAN in reads(ctx)[0]["messages"][0]["content"]


def test_the_read_sees_only_their_whispers_and_her_answers(h):
    on_the_arc(h)
    h.woman(102, "drawn")
    whisper(h, h.T + HOUR, "My name is Quillon, by the way")             # his real name
    h.say(h.T + HOUR + 10, 101, "Nice to meet you properly", channel="whisper", to=1)
    asked = h.say(h.T + 2 * HOUR, 1, "Arkon, still up for that trip?")
    answer(h, h.T + 2 * HOUR + 5, 101, "Always, you know that", asked)
    answer(h, h.T + 2 * HOUR + 6, 101, "A reply never said", asked, dropped="tell")
    unanswered = h.say(h.T + 2 * HOUR + 30, 1, "Arkon, did you see that drop?")
    answer(h, h.T + 2 * HOUR + 35, 101, "Another reply never said", unanswered, dropped="tell")
    other = h.say(h.T + 3 * HOUR, 1, "Anyone for heroics?")               # nobody she answered
    answer(h, h.T + 3 * HOUR + 5, 102, "Me, give me a minute", other)
    h.say(h.T + 3 * HOUR + 30, 101, "Off to bed soon")                   # hers, to nobody
    whisper(h, h.T + 4 * HOUR, "Belra, a secret for you", to=102)
    h.say(h.T + 4 * HOUR + 10, 102, "Belra's whisper back", channel="whisper", to=1)
    h.say(h.T + 4 * HOUR + 20, 102, "Belra tells Arkon a thing", channel="whisper", to=101)
    whisper(h, h.T - 2 * DAY, "an old whisper from before the window")
    ctx = night(h, {"arc_read": [by_name({"Arkon": read(), "Belra": read()})] * 2})
    (call,) = [c for c in reads(ctx) if "Arkon" in c["messages"][0]["content"]]
    user = call["messages"][1]["content"].split("matching this schema:")[0]
    lines = [ln for ln in user.splitlines() if ": " in ln]
    assert lines == ["Testy: My name is Testy, by the way",
                     "Arkon: Nice to meet you properly",
                     "Testy: Arkon, still up for that trip?",
                     "Arkon: Always, you know that"]
    assert "Quillon" not in user
    assert not any(word in user for word in ("heroics", "minute", "Off to bed", "secret",
                                             "Belra", "old whisper", "never said", "that drop"))


def test_his_breakup_words_count_only_in_their_own_lines(h):
    on_the_arc(h)
    h.woman(102, "drawn")
    whisper(h, h.T + HOUR, "it's over between me and my old guild", to=102)
    whisper(h, h.T + 2 * HOUR, "goodnight you")
    night(h, {"arc_read": [by_name({"Arkon": read(breakup=True), "Belra": read()})]})
    assert h.store.get_relationship(101, 1).romance_stage == "planning"


def test_arc_logs_carry_no_chat_text(h, caplog):
    on_the_arc(h, 101)
    on_the_arc(h, 102, "drawn", stage="after", state="done")
    whisper(h, h.T + HOUR, "BlizzCon? yes, sunshine", to=101)
    h.say(h.T + HOUR + 5, 101, "Marmalade skies then", channel="whisper", to=1)
    whisper(h, h.T + 2 * HOUR, "we're done, Belra", to=102)
    h.woman(103, "lust")
    h.rel(103, 1, romance_stage="planning")
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        night(h, {"arc_read": [by_name({"Arkon": read("yes"), "Belra": read(breakup=True)})] * 2})
    reads_logged = sorted((r.leeroy_fields for r in caplog.records
                           if getattr(r, "leeroy_event", "") == "arc_read"),
                          key=lambda f: f["guid"])
    assert reads_logged == [
        {"night": h.night, "guid": 101, "plan": "yes", "breakup": False, "ended": False},
        {"night": h.night, "guid": 102, "plan": "none", "breakup": True, "ended": True}]
    arc_events = [r for r in caplog.records if getattr(r, "leeroy_event", "").startswith("arc")]
    assert {r.leeroy_event for r in arc_events} == {"arc_read", "arc_propose"}
    text = " ".join(r.getMessage() for r in arc_events)
    for word in ("BlizzCon", "sunshine", "Marmalade", "done", "Arkon", "Belra", "Coros", "Testy",
                 "Plan on the table", "a hope"):
        assert word not in text


def test_the_owners_overlay_replaces_the_read_prompt(h):
    on_the_arc(h)
    whisper(h, h.T + HOUR, "yes")
    folder = h.settings.data_dir / "prompts"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "nightly_arc_read.txt").write_text("Invented overlay: {name}, {called}. {plan}",
                                                 encoding="utf-8")
    ctx = night(h, {"arc_read": [read()]})
    system = reads(ctx)[0]["messages"][0]["content"]
    assert system.startswith("Invented overlay: Arkon, Testy. Plan on the table: BlizzCon.")
    assert READ_PROMPT.split("{called}", 1)[1][:30] not in system        # the built-in is gone
    assert system.rstrip().endswith(INVARIANTS.format(human="Testy"))


def test_the_arc_counts_its_read_units(h):
    assert budget.UNIT_CALLS["arc"] == {"json": 1.5}
    assert budget.estimate({"arc": 2}, measured={}) == {"arc": 60.0}
    on_the_arc(h, 101)
    on_the_arc(h, 102, "drawn", stage="after", state="done")
    on_the_arc(h, 103, "lust")                                           # nothing from him
    h.woman(104, "fond")                                                 # fond: never read
    h.rel(104, 1, romance_stage="whispers")
    on_the_arc(h, 105, "lust", stage="confession", state="none")         # not on the arc yet
    for g in (101, 102, 104, 105):
        whisper(h, h.T + HOUR, "hello you", to=g)
    h.clock.set(night_bounds(h.night, UTC)[1] + HOUR)
    assert budget.COUNTERS["arc"](h.ctx()) == 2
