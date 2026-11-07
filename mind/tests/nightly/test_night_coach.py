"""Synthetic training coach coverage; no real model requests."""
import asyncio
import json
from dataclasses import replace

import pytest

from leeroy_mind.nightly import budget
from leeroy_mind.nightly.runner import NightRunner, Stage
from leeroy_mind.training import coach
from leeroy_mind.training.cards import stock_card
from leeroy_mind.training.store import put_card
from leeroy_mind.training.vocab import Vocab


CONTEXT = "631:0:0"


def pull(h, ident="p1", context=CONTEXT, outcome="killed", **over):
    map_id, boss, difficulty = map(int, context.split(":"))
    row = dict(id=ident, context=context, map=map_id, instance_id=1, boss_id=boss,
               difficulty=difficulty, size=10, outcome=outcome, start_ms=int((h.T-300)*1000),
               end_ms=int(h.T*1000), members=0, assisted=0, boss_entry=1, upd_mean_ms=0,
               kind="his", complete=1, scoreable=1, deaths=0, received=h.T,
               score=0, score_basis="deaths_time", checks_version=1,
               checks_coverage='["avoid:unverified_damage"]', damage_basis="requested_pre_ai")
    row.update(over)
    with h.store.transaction():
        h.store.conn.execute("INSERT INTO tg_pull (" + ",".join(row) + ") VALUES (" +
                             ",".join("?" for _ in row) + ")", tuple(row.values()))


def reply(**over):
    return dict(lesson="Review the observed pull.", cards=[], call="", points=[]) | over


def test_add_stage_after_stories():
    async def f(ctx):
        pass
    stages = (Stage("stories", f, "reflection"), Stage("last", f, None))
    runner = NightRunner(stages, lambda *args: None)
    runner.add_stage("coach", f, after="stories")
    assert [s.name for s in runner.stages] == ["stories", "coach", "last"]
    assert runner.stages[1].model == "reflection"
    assert NightRunner(stages, lambda *args: None).stages == stages
    for name, after in (("coach", "stories"), ("new", "absent")):
        with pytest.raises(ValueError):
            runner.add_stage(name, f, after=after)


def test_units_order_and_cap(h):
    pull(h, context="631:1:0")
    pull(h, "wipe", outcome="wiped")
    pull(h, "trial", context="631:3:0")
    with h.store.transaction():
        h.store.conn.execute("INSERT INTO tg_trial(scope,lever,metric,status,opened,closed) "
                             "VALUES ('631:3:0','formation','raid','retired',?,?)", (h.T-10, h.T))
    ctx = h.ctx()
    ctx.settings = replace(ctx.settings, coach_units=2)
    assert coach.coach_units(ctx) == [CONTEXT, "631:3:0"]


def test_one_think_and_one_json_per_unit_and_replay(h):
    pull(h)
    ctx = h.ctx(replies={"coach": [reply()]})
    asyncio.run(coach.stage_coach(ctx))
    assert ctx.reasoner.calls == 2
    assert h.count("tg_lesson") == 1
    asyncio.run(coach.stage_coach(ctx))
    assert ctx.reasoner.calls == 2
    assert not ctx.failures


def test_validator_drops_forbidden(h):
    bad = reply(cards=[dict(lever="formation", value="circle", roles=["ranged"],
                            call="Follow Testmage", why="Try spacing"),
                       dict(lever="mana save level", value=99, roles=["healer"], call="", why="")],
                call="a"*70)
    result = coach.validate_output(bad, Vocab({}, {}, {}, frozenset(), 0, complete=True),
                                   names=set(), human=h.ident, facts={})
    assert not result.cards and not result.call
    assert {"call_names_someone", "bad_value", "call_too_long"} <= set(result.reasons)
    assert "maxLength" not in json.dumps(coach.COACH_SCHEMA)


def test_lesson_over_200_is_cut(h):
    out = coach.validate_output(reply(lesson="A complete first sentence. " + "word "*60),
        Vocab({}, {}, {}, frozenset(), 0), names=set(), human=h.ident, facts={})
    assert out.lesson == "A complete first sentence."


def test_repair_once_then_drop(h):
    pull(h)
    bad = reply(call="a"*70)
    ctx = h.ctx(replies={"coach": [bad, bad]})
    asyncio.run(coach.stage_coach(ctx))
    assert ctx.reasoner.calls == 3
    assert h.count("tg_lesson") == 1
    assert h.count("tg_card") == 0


def test_points_text_comes_from_facts(h):
    pull(h)
    def answer(prompt):
        data = json.JSONDecoder().raw_decode(prompt)[0]
        return reply(points=[data["facts"][0]["id"]], lesson="Our DPS doubled and nobody dies to fire.")
    ctx = h.ctx(replies={"coach": [answer]})
    asyncio.run(coach.stage_coach(ctx))
    point = h.store.conn.execute("SELECT * FROM tg_point").fetchone()
    assert point and "DPS" not in point["text"]
    assert "DPS" not in h.store.conn.execute("SELECT text FROM tg_lesson").fetchone()[0]
    assert h.count("memory") == 0


def test_commit_failure_rolls_back_and_retries(h):
    pull(h)
    with h.store.transaction():
        h.store.conn.execute("CREATE TRIGGER reject_coach BEFORE INSERT ON tg_lesson "
                             "BEGIN SELECT RAISE(ABORT,'synthetic write failure'); END")
    ctx = h.ctx(replies={"coach": [reply(), reply()]})
    asyncio.run(coach.stage_coach(ctx))
    assert ctx.failures["coach"] == 1 and h.count("tg_lesson") == 0
    assert not ctx.done("coach", CONTEXT)
    with h.store.transaction():
        h.store.conn.execute("DROP TRIGGER reject_coach")
    asyncio.run(coach.stage_coach(ctx))
    assert h.count("tg_lesson") == 1 and ctx.done("coach", CONTEXT)


def test_stock_call_version_preserves_winning_champion(h):
    pull(h)
    card_id = put_card(h.store.conn, replace(stock_card(CONTEXT), status="champion",
                                           call="Hold the line", origin="coach"))
    ctx = h.ctx(replies={"coach": [reply(call="Spread out")]})
    asyncio.run(coach.stage_coach(ctx))
    row = h.store.conn.execute("SELECT * FROM tg_card WHERE id=?", (card_id,)).fetchone()
    assert row["status"] == "champion" and row["call"] == "Hold the line"


def test_degrade_cuts_coach_after_the_realm_to_five(h, monkeypatch):
    ctx = h.ctx()
    monkeypatch.setattr(budget, "COUNTERS", {"coach": lambda c: c.settings.coach_units,
                                           "encounters": lambda c: c.settings.encounters_per_night})
    monkeypatch.setattr(budget, "night_budget_s", lambda *args: 100)
    plan = asyncio.run(budget.plan_night(ctx))
    # The realm's units go first; the photos come between the coach and the people.
    assert plan["steps"][:6] == ["realm", "scenes", "snaps", "coach", "selfies", "encounters"]
    assert ctx.settings.coach_units == 5


def test_candidates_embedded_and_stock_call_replaced(h):
    pull(h)
    original = put_card(h.store.conn, replace(stock_card(CONTEXT), status="champion",
                                            call="Hold position"))
    sources = coach.CoachSources(lambda: Vocab({}, {}, {}, frozenset(), 0, complete=True),
                                 lambda context: ["Avoid the ground effect."])
    good = reply(cards=[dict(lever="formation", value="circle", roles=["ranged"],
                            call="Spread out", why="Spacing proposal")], call="Focus adds")
    ctx = h.ctx(replies={"coach": [good]})
    asyncio.run(coach.stage_coach(ctx, sources=sources))
    assert not ctx.failures
    rows = h.store.conn.execute("SELECT * FROM tg_card ORDER BY id").fetchall()
    assert len(rows) == 3 and rows[0]["status"] == "retired"
    candidate, champion = rows[1:]
    assert candidate["origin"] == "coach" and candidate["status"] == "candidate"
    assert candidate["embedding"] and champion["embedding"]
    assert champion["origin"] == "stock" and champion["directives"] == "[]"
    assert champion["prev_champion"] == original and champion["call"] == "Focus adds"
    assert json.loads(candidate["evidence"]) == ["pull:p1"]


def test_unknown_vocab_admits_no_cards_or_memories(h):
    pull(h)
    bad = reply(cards=[dict(lever="formation", value="circle", roles=["ranged"],
                           call="Spread out", why="We stopped dying because of this")],
                lesson="We stopped dying to fire once we changed formation.")
    ctx = h.ctx(replies={"coach": [bad, bad]})
    asyncio.run(coach.stage_coach(ctx))
    assert h.count("tg_card") == h.count("memory") == 0
    assert "stopped dying" not in h.store.conn.execute("SELECT text FROM tg_lesson").fetchone()[0]


def test_daily_cap_survives_new_contexts_and_restart(h):
    pull(h)
    ctx = h.ctx(replies={"coach": [reply()]})
    ctx.settings = replace(ctx.settings, coach_units=1)
    asyncio.run(coach.stage_coach(ctx))
    pull(h, "late", context="631:1:0")
    fresh = h.ctx()
    fresh.settings = ctx.settings
    assert coach.coach_units(fresh) == []
    tomorrow = h.ctx(night="2026-10-30")
    assert coach.coach_units(tomorrow) == ["631:1:0"]


def test_late_pull_and_previously_covered_rows(h):
    pull(h)
    ctx = h.ctx(replies={"coach": [reply()]})
    asyncio.run(coach.stage_coach(ctx))
    pull(h, "late", start_ms=int((h.T-900)*1000), end_ms=int((h.T-600)*1000))
    ctx = h.ctx(night="2026-10-30", replies={"coach": [reply()]})
    asyncio.run(coach.stage_coach(ctx))
    evidence = json.loads(h.store.conn.execute("SELECT evidence FROM tg_lesson WHERE night=?",
                                               (ctx.night,)).fetchone()[0])
    assert evidence == ["pull:late"]
    assert coach.coach_units(h.ctx(night="2026-10-31")) == []


@pytest.mark.parametrize("over", [dict(complete=0), dict(scoreable=0), dict(outcome="aborted"),
    dict(score=None), dict(score_basis="unknown"), dict(end_ms=0), dict(context="631:13:0")])
def test_unusable_records_are_never_units(h, over):
    pull(h, **over)
    assert coach.coach_units(h.ctx()) == []


def test_recheck_during_model_request_refuses_entire_unit(h):
    pull(h)
    def changed(prompt):
        with h.store.transaction():
            h.store.conn.execute("UPDATE tg_pull SET score=NULL,checks_version=2")
        return reply()
    ctx = h.ctx(replies={"coach": [changed]})
    asyncio.run(coach.stage_coach(ctx))
    assert ctx.failures["coach"] == 1
    assert not ctx.done("coach", CONTEXT)
    assert h.count("tg_lesson") == h.count("tg_point") == h.count("tg_card") == 0


def test_partial_card_writes_rollback_with_failed_lesson(h):
    pull(h)
    sources = coach.CoachSources(lambda: Vocab({}, {}, {}, frozenset(), 0, complete=True))
    with h.store.transaction():
        h.store.conn.execute("CREATE TRIGGER reject_coach BEFORE INSERT ON tg_lesson "
                             "BEGIN SELECT RAISE(ABORT,'synthetic write failure'); END")
    good = reply(cards=[dict(lever="formation", value="circle", roles=["ranged"],
                            call="Spread out", why="Try spacing")])
    ctx = h.ctx(replies={"coach": [good]})
    asyncio.run(coach.stage_coach(ctx, sources=sources))
    assert ctx.failures["coach"] == 1
    assert h.count("tg_card") == h.count("tg_lesson") == 0
    assert not ctx.done("coach", CONTEXT)


def test_unknown_fact_and_all_identity_surfaces_rejected(h):
    vocab = Vocab({}, {}, {}, frozenset(), 0, complete=True)
    for name in ("Syntheticmember", h.ident.called, h.ident.character_name, h.ident.real_name):
        raw = reply(lesson=f"We learned from {name}.", call=f"Follow {name}",
                    points=["invented"], cards=[dict(lever="formation", value="circle",
                    roles=["ranged"], call="Spread out", why=f"Advice from {name}")])
        out = coach.validate_output(raw, vocab, names={"Syntheticmember"}, human=h.ident, facts={})
        assert not out.lesson and not out.call and not out.cards and not out.points


@pytest.mark.parametrize("call", ["Our DPS doubled", "Fire killed us", "Spread out because fire killed us",
    "We stopped dying", "Focus adds; our healing efficiency improved", "Wait 90 seconds"])
def test_unverified_claims_and_unbounded_waits_never_become_calls(h, call):
    raw = reply(call=call, cards=[dict(lever="formation", value="circle", roles=["ranged"],
                                    call=call, why="Try spacing")])
    out = coach.validate_output(raw, Vocab({}, {}, {}, frozenset(), 0, complete=True),
                                names=set(), human=h.ident, facts={})
    assert not out.call and out.cards and not out.cards[0].call
    assert "call_not_prospective" in out.reasons


@pytest.mark.parametrize("call", ["Spread out", "Stay behind the boss.", "Focus adds; save mana",
    "Hold position", "Use lust at 30 percent", "Wait 5 seconds before attacking", "Stack on the tank"])
def test_prospective_tactics_remain_available(h, call):
    out = coach.validate_output(reply(call=call), Vocab({}, {}, {}, frozenset(), 0),
                                names=set(), human=h.ident, facts={})
    assert out.call == call and not out.reasons


def test_real_coach_units_are_priced_and_cut_before_people(h, monkeypatch):
    for boss in (0, 1, 3, 4, 5):
        for difficulty in range(4):
            context = f"631:{boss}:{difficulty}"
            pull(h, ident=context, context=context)
    monkeypatch.setattr(budget, "COUNTERS", {
        "coach": lambda ctx: len(coach.coach_units(ctx)),
        "encounters": lambda ctx: ctx.settings.encounters_per_night})
    monkeypatch.setattr(budget, "night_budget_s", lambda *args: 10000)
    roomy = asyncio.run(budget.plan_night(h.ctx()))
    assert roomy["units"] == {"coach": 20, "encounters": 150}
    assert roomy["estimate_s"] == 20 * 130 + 150 * 40
    assert roomy["steps"] == []
    monkeypatch.setattr(budget, "night_budget_s", lambda *args: 6650)
    ctx = h.ctx()
    tight = asyncio.run(budget.plan_night(ctx))
    assert tight["steps"] == ["realm", "scenes", "snaps", "coach"]  # No counter here: the floor, 0.
    assert tight["settings"] == {"realm_units": 0, "scenes_per_night": 0, "snaps_per_night": 0,
                                 "coach_units": 5}
    assert tight["units"] == {"coach": 5, "encounters": 150}
    assert tight["estimate_s"] == tight["budget_s"] == 6650
    assert ctx.settings.encounters_per_night == h.settings.encounters_per_night


def test_attached_coach_budget_is_isolated_from_disabled_runner_and_resume(h, monkeypatch):
    from leeroy_mind.nightly.store import run_begin
    for boss in (0, 1, 3, 4, 5):
        for difficulty in range(4):
            context = f"631:{boss}:{difficulty}"
            pull(h, ident=context, context=context)
    monkeypatch.setattr(budget, "COUNTERS", {
        "coach": budget.COUNTERS["coach"],
        "encounters": lambda ctx: ctx.settings.encounters_per_night})
    monkeypatch.setattr(budget, "night_budget_s", lambda *args: 6000)
    seen = []

    async def capture(ctx):
        seen.append(ctx)

    enabled = NightRunner((Stage("stories", capture, None),),
                          lambda night, window: h.ctx(night=night, window=window))
    enabled.add_stage("coach", capture, after="stories", model=None)
    ctx = h.ctx()
    run_begin(h.store, ctx.night, *ctx.window, now=h.clock.now())
    asyncio.run(enabled.run(ctx.night, ctx.window))
    assert seen[-1].plan["units"]["coach"] == 5
    assert seen[-1].settings.encounters_per_night == 133
    assert seen[-1].plan["extensions"] == ["coach"]

    disabled = NightRunner((Stage("stories", capture, None),),
                           lambda night, window: h.ctx(night=night, window=window))
    # Same-night restart must discard the enabled runner's reduced people budget.
    asyncio.run(disabled.run(ctx.night, ctx.window))
    resumed = seen[-1]
    assert resumed.plan["units"] == {"encounters": 150}
    assert resumed.plan["steps"] == [] and resumed.plan["settings"] == {}
    assert resumed.plan["extensions"] == []
    assert resumed.settings.encounters_per_night == 150
    assert resumed.plan["estimate_s"] == 6000
    # A fresh disabled run in the same process has the identical ordinary budget.
    tomorrow = h.ctx(night="2026-10-30")
    asyncio.run(disabled.run(tomorrow.night, tomorrow.window))
    assert seen[-1].plan == resumed.plan
    # Registration is still present for enabled runners; isolation is per instance.
    assert "coach" in budget.COUNTERS
    assert [stage.name for stage in disabled.stages] == ["stories"]


@pytest.mark.parametrize("prefix", [199, 200])
def test_lesson_sentence_boundary_never_exceeds_sql_limit(h, prefix):
    text = "x" * prefix + ". extra"
    out = coach.validate_output(reply(lesson=text), Vocab({}, {}, {}, frozenset(), 0),
                                names=set(), human=h.ident, facts={})
    assert len(out.lesson) == 200
    assert out.lesson.endswith(".") is (prefix == 199)
    fact = coach.CoachFact("f", text, ("pull:p",))
    durable = coach._durable_lesson(replace(out, points=("f",)), {"f": fact})
    assert durable == out.lesson
    with h.store.transaction():
        h.store.conn.execute("INSERT INTO tg_lesson(context,night,text,evidence) VALUES (?,?,?,?)",
                             (CONTEXT, h.night, durable, "[]"))
    assert len(h.store.conn.execute("SELECT text FROM tg_lesson").fetchone()[0]) <= 200


def test_prompt_and_repair_advertise_only_supported_role_value_combinations(h):
    from leeroy_mind.training.protocol import DcKey
    from leeroy_mind.training.vocab import ClassVocab
    pull(h)
    vocab = Vocab(
        classes={cls: ClassVocab(strategies={"threat"}, actions={"attack"}, class_=cls)
                 for cls in (1, 2, 4, 6, 7, 11)}, probes={},
        dc={"PullCombatSpread": DcKey("PullCombatSpread", 0, 1, 0, 4000, -1)},
        whitelist=frozenset({"threat"}), at=h.T, complete=True)
    seen = []

    def answer(prompt):
        data = json.JSONDecoder().raw_decode(prompt)[0]
        menu = data["vocabulary"]["levers"]
        seen.append(menu)
        assert "PartyMaxSpread" not in menu
        assert "PullCombatSpread" in menu
        assert "focus tanks" not in menu and "dps aoe" not in menu
        assert menu["threat"] == [{"roles": ["melee"], "values": ["+"]}]
        assert any(option["roles"] == ["ranged"] and "circle" in option["values"]
                   for option in menu["formation"])
        if len(seen) == 1:
            return reply(cards=[dict(lever="threat", value="+", roles=["ranged"],
                                    call="Focus adds", why="Try this")])
        return reply(cards=[dict(lever="formation", value="circle", roles=["ranged"],
                                call="Spread out", why="Try spacing")])

    ctx = h.ctx(replies={"coach": [answer, answer]})
    asyncio.run(coach.stage_coach(ctx, sources=coach.CoachSources(lambda: vocab)))
    assert not ctx.failures and len(seen) == 2
    assert seen[0] == seen[1]
    assert h.count("tg_card") == 1


def test_unknown_snapshot_advertises_no_actionable_options(h):
    pull(h)
    def answer(prompt):
        data = json.JSONDecoder().raw_decode(prompt)[0]
        assert data["vocabulary"] == {"complete": False, "levers": {}}
        return reply()
    ctx = h.ctx(replies={"coach": [answer]})
    asyncio.run(coach.stage_coach(ctx))
    assert not ctx.failures
