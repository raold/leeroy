"""The night's real plan constraints, including the named server clock."""
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


def test_rivals_never_share_a_block(store):
    from conftest import seed_test_cast
    seed_test_cast(store)
    store.conn.execute("UPDATE relationship SET affinity=-80 WHERE a=102 AND b=101")
    store.conn.execute("INSERT INTO relationship(a,b,affinity) VALUES(102,101,-80) "
                       "ON CONFLICT(a,b) DO UPDATE SET affinity=-80")
    assert plan.partner(store.conn, 101, (102,)) is None
    store.conn.execute("INSERT INTO relationship(a,b,affinity) VALUES(101,103,80) "
                       "ON CONFLICT(a,b) DO UPDATE SET affinity=80")
    assert plan.partner(store.conn, 101, (102,104,103)) == 103


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
