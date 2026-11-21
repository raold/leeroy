import pytest

from leeroy_mind.training.applier import _RANK
from leeroy_mind.training.score import context_stats
from .test_tg_score import scored


def test_layer_sits_between_standing_and_boss():
    assert _RANK["standing"] < _RANK["guild"] == 1.5 < _RANK["boss"]


def test_pulls_under_a_guild_strategy_are_their_own_score_base():
    old = scored("old")
    new = scored("new", score=50)
    new["covariates"]["guild_strategy"] = 17
    with pytest.raises(ValueError):
        context_stats([old, new])
    assert context_stats([new]).mean == 50


def test_reference_median_and_champion_coach_do_not_cross_guild_strategies(fake):
    import asyncio
    from types import SimpleNamespace
    from dataclasses import replace
    from leeroy_mind.training import coach, store
    from leeroy_mind.training.score import score_pull
    from .test_tg_store import record, context

    for at, duration, strategy in [(1000, 100000, 4), (200000, 10000, 5), (300000, 200000, 4)]:
        r = record(f"{at}-77-0")
        r.header = replace(r.header, start_ms=at, end_ms=at+duration)
        ctx = context(covariates={"guild_strategy": strategy})
        store.put_pull(fake.conn, r, ctx, {101: "tank"}, received=at/1000+duration/1000)
        score = asyncio.run(score_pull(fake, r, ctx, SimpleNamespace(failures=(), causes=("unknown",), failed_mechanics=0)))
        assert not coach._eligible(store.pull_rows(fake.conn)[-1])
    assert score == -38  # twice strategy 4's reference duration (100 seconds)


def test_report_lists_separate_strategy_baselines(fake):
    from .test_tg_report import add, section
    add(fake, 28, deaths=2)
    add(fake, 29, deaths=4)
    fake.conn.execute("""UPDATE tg_pull SET covariates='{"guild_strategy":17}' WHERE deaths=4""")
    rows = section(fake)["contexts"]
    assert [(r.get("guild_strategy"), r["pulls"], r["baseline_pulls"]) for r in rows] == [(None, 1, 1), (17, 1, 1)]

def test_guild_layer_rechecks_decision_identity_at_transport(store, clock):
    import asyncio
    from .test_tg_applier import Fake, card
    from leeroy_mind.training.cards import Directive

    async def go():
        ports = Fake(store, clock)
        strategy = card(Directive("PullPlayerReleaseDelay", 125), id=17)
        current = 17
        ports.applier.guild_allowed = lambda context, ident: ident == current
        perform = ports.applier._perform
        async def replaced_before_dispatch(receipt, command):
            nonlocal current
            current = 18
            await perform(receipt, command)
        ports.applier._perform = replaced_before_dispatch
        assert not await ports.apply(strategy, layer="guild")
        assert current == 18 and not ports.sent
    asyncio.run(go())
