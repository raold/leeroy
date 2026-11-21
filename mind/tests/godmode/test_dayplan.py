"""The night's real plan constraints, including the named server clock."""
import asyncio
import json
from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo
from types import SimpleNamespace

import pytest

from leeroy_mind.godmode import dayplan as plan
from leeroy_mind.nightly.llm import check_schema
from leeroy_mind.realm.places import Places, DIST

ZONE = ZoneInfo("America/New_York")


def test_plan_schema_and_pairings():
    places = Places.load(DIST)
    choices = plan.choices(places, holiday=False, paired=False)
    assert set(choices) <= set(plan.PAIRINGS)
    assert "holiday" not in choices and "duel_practice" not in choices
    assert all(places[p].kind == "fish" for p in choices["fishing"])
    assert all(places[p].kind in ("herb", "ore") for p in choices["gathering"])
    schema = plan.schema(choices, ("08", "10", "12"))
    blocks = [{"slot": "08", "activity": "fishing", "place": choices["fishing"][0]},
              {"slot": "10", "activity": "offline", "place": "none"}]
    check_schema(schema, {"blocks": blocks})
    with pytest.raises(ValueError):
        check_schema(schema, {"blocks": blocks[:1]})
    with pytest.raises(ValueError):
        check_schema(schema, {"blocks": [blocks[0], dict(blocks[1], place="off the list")]})


@pytest.mark.parametrize("night,start,end", [
    ("2026-03-06", "2026-03-07T08:00:00-05:00", "2026-03-08T03:00:00-04:00"),
    ("2026-10-30", "2026-10-31T08:00:00-04:00", "2026-11-01T02:00:00-05:00")])
def test_raid_nights_always_win(store, night, start, end):
    slots = plan.slots_for(store.conn, night)
    assert datetime.fromtimestamp(min(v[0] for v in slots.values()), ZONE).isoformat() == start
    assert datetime.fromtimestamp(plan.window(night)[1], ZONE).isoformat() == end
    raid = slots["12"][0] + 1800
    store.conn.execute("INSERT INTO realm_calendar(kind,creator,at,title,updated,state,event_id) "
                       "VALUES('raid',1,?,'Raid',0,'posted',1)", (raid,))
    limited = plan.slots_for(store.conn, night)
    assert limited and all(b <= raid for a, b in limited.values())


def test_off_means_no_stage():
    from leeroy_mind.config import LifeCfg
    assert LifeCfg().day_plans is False
    runner = SimpleNamespace(add_stage=lambda *a, **kw: pytest.fail("disabled stage added"))
    plan.attach(runner, SimpleNamespace(day_plans=False), lambda: pytest.fail("places loaded"))


from nightly.conftest import h  # the real night context, with a scripted reflection client


def test_nightly_stage_commits_shared_enum_blocks_once(h):
    import asyncio
    places = Places.load(DIST)
    inn = places.by_kind("inn")[0].name
    ctx = h.ctx({"day_plans": [{"blocks": [{"slot": "08", "activity": "inn", "place": inn},
                                           {"slot": "12", "activity": "offline", "place": "none"}]}] * 5})
    ctx.settings = replace(ctx.settings, day_plans=True)
    async def go():
        await plan.stage_day_plans(ctx, places)
        rows = h.store.conn.execute("SELECT member,starts,ends,partners FROM day_plan WHERE activity='inn'").fetchall()
        assert len(rows) == len(ctx.members())
        import json
        couples = {r["member"]: json.loads(r["partners"]) for r in rows}
        assert any(couples.values())
        for guid, others in couples.items():
            for other in others:
                assert couples[other] == [guid]
        calls = len(ctx.reasoner.llm.calls)
        assert calls > 0
        await plan.stage_day_plans(ctx, places)
        assert len(ctx.reasoner.llm.calls) == calls
        assert h.store.conn.execute("SELECT count(*) FROM day_plan").fetchone()[0] == 2 * len(rows)
    asyncio.run(go())


def day_context(h):
    blocks = [{"slot": "08", "activity": "inn", "place": "org_inn"},
              {"slot": "10", "activity": "offline", "place": "none"}]
    ctx = h.ctx({"day_plans": [{"blocks": blocks}] * 5})
    ctx.settings = replace(ctx.settings, day_plans=True)
    with ctx.store.transaction():
        ctx.store.conn.execute("DELETE FROM relationship")
    return ctx


@pytest.mark.parametrize("when", [1, 2])
@pytest.mark.parametrize("hostility", ["rival", "feud"])
def test_new_hostility_during_model_draw_reselects_legal_cohorts(h, when, hostility):
    from leeroy_mind.nightly.budget import plan_night

    ctx = day_context(h)
    asyncio.run(plan_night(ctx, stages=frozenset({"day_plans"})))
    llm = ctx.reasoner.llm
    script = llm._script

    def reply(model, messages):
        if len(llm.calls) == when:
            with ctx.store.transaction():
                if hostility == "rival":
                    ctx.store.conn.execute("INSERT INTO relationship(a,b,affinity) VALUES(104,103,-80)")
                else:
                    ctx.store.conn.execute("INSERT INTO feud(a,b,started,cause) VALUES(103,104,1,'synthetic')")
        return script(model, messages)

    llm._script = reply
    asyncio.run(plan.stage_day_plans(ctx, Places.load(DIST)))
    partners = {g: json.loads(p) for g, p in ctx.store.conn.execute(
        "SELECT member,partners FROM day_plan WHERE activity='inn'")}
    assert partners[101] == [102] and partners[102] == [101]
    assert partners[103] == [105] and partners[105] == [103]
    assert partners.get(104, []) == []
    assert len(llm.calls) == ctx.plan["day_calls"] == 3
    asyncio.run(plan.stage_day_plans(ctx, Places.load(DIST)))
    assert len(llm.calls) == 3


@pytest.mark.parametrize("scenario,expected", [
    ("neutral", [(101,102), (103,104), (105,)]),
    ("friends", [(102,103), (101,104), (105,)]),
    ("positive_tie", [(102,103), (101,104), (105,)]),
    ("hostile", [(101,102), (103,104), (105,)]),
    ("singletons", [(101,), (102,), (103,), (104,), (105,)]),
    ("existing", [(102,103), (104,105)]),
    ("raid", []),
])
def test_nightly_reserves_and_plans_the_same_reciprocal_cohorts(h, scenario, expected):
    from itertools import combinations
    from leeroy_mind.nightly.budget import plan_night

    ctx = day_context(h)
    conn = ctx.store.conn
    if scenario in {"friends", "positive_tie", "hostile"}:
        conn.execute("INSERT INTO relationship(a,b,affinity) VALUES(103,102,80)")
    if scenario == "positive_tie":
        conn.execute("INSERT INTO relationship(a,b,romance_stage) VALUES(104,102,'notices')")
    if scenario == "hostile":
        conn.execute("INSERT INTO relationship(a,b,affinity) VALUES(102,103,-80)")
    if scenario == "singletons":
        conn.executemany("INSERT INTO relationship(a,b,affinity) VALUES(?,?,-80)", combinations(ctx.members(), 2))
    if scenario == "existing":
        conn.execute("INSERT INTO day_plan(night,member,starts,ends,activity,status) VALUES(?,101,1,2,'offline','done')",
                     (ctx.night,))
    if scenario == "raid":
        conn.execute("INSERT INTO realm_calendar(kind,creator,at,title,updated,state,event_id) "
                     "VALUES('raid',1,?,'Raid',0,'posted',1)", (plan.boundary(ctx.night, 10),))
    budget = asyncio.run(plan_night(ctx, stages=frozenset({"day_plans"})))
    assert budget["units"].get("day_plans", 0) == len(expected)
    assert ctx.reasoner.calls == 0
    asyncio.run(plan.stage_day_plans(ctx, Places.load(DIST)))
    assert ctx.reasoner.calls == len(expected)
    rows = {g: json.loads(mates) for g, mates in conn.execute("SELECT member,partners FROM day_plan WHERE activity='inn'")}
    assert rows == {g: [p for p in group if p != g] for group in expected for g in group}
    assert plan.remaining_schedule(ctx)[1] == []


def test_actual_calls_stay_within_reservation_when_pairs_become_hostile(h):
    from itertools import combinations
    from leeroy_mind.nightly.budget import plan_night
    ctx = day_context(h)
    budget = asyncio.run(plan_night(ctx, stages=frozenset({"day_plans"})))
    assert budget["units"]["day_plans"] == 3
    ctx.store.conn.executemany("INSERT INTO relationship(a,b,affinity) VALUES(?,?,-80)", combinations(ctx.members(), 2))
    assert len(plan.remaining_schedule(ctx)[1]) == 5
    asyncio.run(plan.stage_day_plans(ctx, Places.load(DIST)))
    assert ctx.reasoner.calls == 3
    asyncio.run(plan.stage_day_plans(ctx, Places.load(DIST)))
    assert ctx.reasoner.calls == 3


def test_day_switch_reloads_in_both_directions_on_the_real_app(h):
    from leeroy_mind.app import MindApp
    from leeroy_mind.config import load_config
    from leeroy_mind.nightly.budget import COUNTERS

    script = day_context(h)
    path = h.settings.data_dir / "mind.toml"
    def write(on):
        path.write_text(f"[realm]\nenabled=true\n[life]\nday_plans={str(on).lower()}\n")
    def stages():
        return [s for s in app.night_runner.stages if s.name == "day_plans"]
    def context():
        return app.night_runner.make_ctx(script.night, script.window)
    write(False)
    app = MindApp(load_config(path), h.settings.data_dir, llm=script.reasoner.llm, clock=h.clock)
    async def run():
        try:
            assert not stages() and "day" not in app.realm.parts
            write(True)
            on = context()
            stage, = stages()
            runner = app.realm.parts["day"]
            assert runner.admission and app.engine.cfg.life.day_plans and app.file_cfg.life.day_plans
            assert COUNTERS["day_plans"](on) == 3
            # A reload also retires an active model request, including the reasoner's retry.
            llm = script.reasoner.llm
            llm._gate = asyncio.Event()
            pending = asyncio.create_task(stage.fn(on))
            async with asyncio.timeout(2):
                while not llm.inflight:
                    await asyncio.sleep(0)
            assert len(llm.calls) == 1
            write(False)
            off = context()
            llm._gate.set()
            await pending
            assert not stages() and not runner.admission and not off.settings.day_plans
            assert not app.engine.cfg.life.day_plans and not app.file_cfg.life.day_plans
            assert COUNTERS["day_plans"](off) == 0
            await stage.fn(on)  # A callback captured by the original night is also disabled.
            await plan.stage_day_plans(off, Places.load(DIST))
            assert len(llm.calls) == 1 and not app.store.conn.execute("SELECT 1 FROM day_plan").fetchone()
            write(True)
            app._reload_config()  # Session-start reload, before the next night or bridge greeting.
            assert runner.admission and app.nightly.settings.day_plans and app.engine.cfg.life.day_plans
            fresh = context()
            new_stage, = stages()
            await stage.fn(on)
            assert len(llm.calls) == 1
            await new_stage.fn(fresh)
            assert fresh.reasoner.calls == 3 and app.store.conn.execute("SELECT count(*) FROM day_plan").fetchone()[0] == 10
            assert app.realm.parts["day"] is runner and sum(p is runner for p, _ in app.realm._parts) == 1
            write(False)
            app._reload_config()
            assert not runner.admission and not stages() and not app.nightly.settings.day_plans
        finally:
            await app.stop()
    asyncio.run(run())


def test_rivals_never_share_a_block(h):
    ctx = day_context(h)
    conn = ctx.store.conn
    conn.execute("INSERT INTO relationship(a,b,romance_stage) VALUES(101,102,'notices')")
    conn.execute("INSERT INTO relationship(a,b,affinity) VALUES(102,101,-80)")
    assert (101, 102) not in plan.remaining_schedule(ctx)[1]
    conn.execute("UPDATE relationship SET affinity=0 WHERE a=102 AND b=101")
    conn.execute("INSERT INTO feud(a,b,started,cause) VALUES(101,102,1,'synthetic')")
    assert (101, 102) not in plan.remaining_schedule(ctx)[1]


def test_correction_and_resume_spend_the_saved_day_call_reservation(h):
    from leeroy_mind.nightly.budget import plan_night
    from leeroy_mind.nightly.store import run_begin, run_get
    ctx = day_context(h)
    run_begin(ctx.store, ctx.night, *ctx.window, now=ctx.clock.now())
    budget = asyncio.run(plan_night(ctx, stages=frozenset({"day_plans"})))
    assert budget["units"]["day_plans"] == 3
    original = ctx.reasoner.llm._script
    def correct(model, messages):
        if len(ctx.reasoner.llm.calls) == 1:
            return "{}"  # One malformed reply, then the reasoner's normal JSON correction.
        return original(model, messages)
    ctx.reasoner.llm._script = correct
    asyncio.run(plan.stage_day_plans(ctx, Places.load(DIST)))
    assert len(ctx.reasoner.llm.calls) == 3
    assert ctx.store.conn.execute("SELECT count(*) FROM day_plan").fetchone()[0] == 8
    resumed = h.ctx()
    resumed.settings = replace(resumed.settings, day_plans=True)
    resumed.plan = run_get(ctx.store, ctx.night).plan
    assert resumed.plan["day_calls"] == 3 and len(plan.remaining_schedule(resumed)[1]) == 1
    asyncio.run(plan.stage_day_plans(resumed, Places.load(DIST)))
    assert not resumed.reasoner.llm.calls
