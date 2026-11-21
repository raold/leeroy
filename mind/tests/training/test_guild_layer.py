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
        strategy = card(Directive("PullPlayerReleaseDelay", 125))
        current = 17
        ports.applier.guild_allowed = lambda context, ident: ident == current
        perform = ports.applier._perform
        async def replaced_before_dispatch(receipt, command):
            nonlocal current
            current = 18
            await perform(receipt, command)
        ports.applier._perform = replaced_before_dispatch
        from .test_tg_applier import RUN, CTX, MEMBERS
        assert not await ports.applier.apply(RUN,"guild",CTX,strategy,MEMBERS,tank_guid=101,lead_is_human=False,
                                             guild_strategy=17)
        assert current == 18 and not ports.sent
    asyncio.run(go())


def test_decision_refusal_leaves_colliding_champion_intact(store, clock):
    import asyncio
    from leeroy_mind.training import store as tg
    from .test_tg_applier import Fake, card
    from leeroy_mind.training.cards import Directive
    ports = Fake(store, clock)
    champion = tg.put_card(store.conn, card(Directive("lust_at", 85)).to_row())
    ports.applier.guild_allowed = lambda context, ident: ident == champion
    ports.fail_ack[""] = "bad_value"
    async def apply():
        from .test_tg_applier import RUN, CTX, MEMBERS
        return await ports.applier.apply(RUN, "guild", CTX, card(Directive("PullPlayerReleaseDelay",125)),
                                         MEMBERS, tank_guid=101, lead_is_human=False, guild_strategy=champion)
    assert not asyncio.run(apply())
    assert ports.sent and tg.champion(store.conn, "631:0:0")["id"] == champion


def test_guild_dc_requires_online_tank(store, clock):
    import asyncio
    from dataclasses import replace
    from .test_tg_applier import Fake, card, RUN, CTX, MEMBERS
    from leeroy_mind.training.cards import Directive
    ports = Fake(store, clock)
    ports.state = replace(ports.state, online=ports.state.online-{101})
    ports.applier.guild_allowed = lambda context, ident: True
    assert not asyncio.run(ports.applier.apply(RUN,"guild",CTX,card(Directive("PullPlayerReleaseDelay",125)),
                                               MEMBERS,tank_guid=101,lead_is_human=False,guild_strategy=19))
    assert not ports.sent


@pytest.mark.parametrize("cause", ["hold", "refused"])
def test_guild_inverse_retains_modified_history_until_proven(fake, cause):
    import asyncio
    from unittest.mock import Mock
    from leeroy_mind.training.cards import Directive
    from .test_tg_applier import card
    from .test_tg_listener import real_listener, status

    async def play():
        listener, run, ports = real_listener(fake)
        proposed = card(Directive("PullPlayerReleaseDelay",125), context="631:4:0")
        selected = (19, proposed)
        listener.guild_layer = lambda context: selected
        listener.guild_played = Mock()
        ports.applier.guild_allowed = lambda context, ident: bool(selected and ident == selected[0])
        holding = False
        listener.guild_hold = ports.applier.guild_hold = lambda: holding
        await listener.on_start(run)
        await listener.on_dc_status(status(fake))
        listener.guild_played.assert_called_once_with(19)
        assert any(getattr(c,"op","") == "scale" for c in ports.sent)
        selected = None
        listener._muster = None
        perform = ports.applier._perform
        async def enter_hold(receipt, command):
            nonlocal holding
            if cause == "hold" and receipt.envelope.get("guild_inverse"):
                holding = True
            await perform(receipt, command)
        ports.applier._perform = enter_hold
        if cause == "refused":
            ports.fail_ack[""] = "bad_value"
        before = len(ports.sent)
        await listener.on_dc_status(status(fake))
        if cause == "hold":
            assert len(ports.sent) == before
        assert ports.applier.guild_pending(listener.run_key(run))
        assert listener.ledger.set_in_play.call_args.kwargs["guild_strategy"] == 19
        listener.ledger.mark_unscoreable.assert_called_with("631:4:0")
        holding = False
        ports.applier._perform = perform
        await listener.on_dc_status(status(fake))
        assert not ports.applier.guild_pending(listener.run_key(run))
        assert "guild_strategy" not in listener.ledger.set_in_play.call_args.kwargs
        assert any(getattr(c,"op","") == "reset" for c in ports.sent[before:])
        await listener.close()
    asyncio.run(play())


@pytest.mark.parametrize("condition,cohort", [("human_lead",0), ("shadowed",0), ("offline_tank",0), ("played",23)])
def test_muster_selects_the_farm_cohort_it_can_play(fake, condition, cohort):
    import asyncio
    from dataclasses import replace
    from unittest.mock import Mock
    from leeroy_mind.training.cards import Directive
    from .test_tg_applier import card
    from .test_tg_listener import real_listener, plan_card, status

    async def play():
        listener, run, ports = real_listener(fake)
        if condition == "human_lead":
            run = replace(run, human_in_raid=True, lead_is_human=True)
            ports.state = replace(ports.state, lead_is_human=True)
        if condition == "shadowed":
            plan_card(fake,Directive("PullPlayerReleaseDelay",75))
        proposed = card(Directive("PullPlayerReleaseDelay",125), context="631:4:0")
        listener.guild_layer = lambda context: (23, proposed)
        listener.guild_played = Mock()
        ports.applier.guild_allowed = lambda context, ident: ident == 23
        # Three real kills make the unmodified boss farm. There are no strategy-23 receipts.
        from .test_tg_experiments import pull
        for n in range(3):
            pull(fake, "farm"+str(n), boss=4, when=fake.clock.now()-100*(n+1))
        await listener.on_start(run)
        if condition == "offline_tank":
            ports.state = replace(ports.state, online=ports.state.online-{101})
        await listener.on_dc_status(status(fake))
        cache = listener._farm_at_entry
        assert cache[(listener.run_key(run),"631:4:0",cohort)] is (cohort == 0)
        if cohort:
            listener.guild_played.assert_called_once_with(cohort)
        else:
            listener.guild_played.assert_not_called()
        await listener.close()
    asyncio.run(play())


@pytest.mark.parametrize("late_loss", [None, "shaman", "provider"])
def test_lust_carrier_at_effect_boundary_controls_timer_and_recorded_cohort(fake, late_loss):
    import asyncio
    import json
    from dataclasses import replace
    from unittest.mock import Mock
    from leeroy_mind.training.cards import Directive
    from leeroy_mind.training.ledger import Ledger
    from leeroy_mind.training.protocol import PullPulse
    from .test_tg_applier import card
    from .test_tg_gate import boss
    from .test_tg_ledger import header, member, feed, run as open_raid
    from .test_tg_listener import real_listener, status

    async def play():
        listener, run, ports = real_listener(fake)
        run.session.raid_id = open_raid(fake, human=False, instance=100).session.raid_id
        listener.ledger = Ledger(fake)
        team = dict(ports.state.members)
        team[102] = replace(team[102], cls=7)
        ports.state = replace(ports.state, members=team, run_key=listener.run_key(run))
        guild = (19, card(Directive("lust_at", 85), context="631:4:0"))
        listener.guild_layer = lambda context: guild
        listener.guild_played = Mock()
        ports.applier.guild_allowed = lambda context, ident: ident == guild[0]
        provider = listener.lust_provider
        await listener.on_start(run)
        revert = ports.applier.revert

        async def lose_carrier(*args, **kwargs):
            await revert(*args, **kwargs)
            if late_loss == "provider":
                listener.lust_provider = None
            elif late_loss == "shaman":
                ports.state = replace(ports.state, online=ports.state.online - {102})

        ports.applier.revert = lose_carrier
        await listener.on_dc_status(status(fake))
        engaged = boss(fake, 4)
        await listener.on_boss(engaged)
        await listener.on_pulse(PullPulse(engaged.ts, f"{engaged.ts}-100-4", 631, 100, 4, 0, 80, 2))
        await listener.tick()
        h = header(fake, instance_id=100, id=f"{engaged.ts}-100-4", boss=4)
        await feed(listener.ledger, member(h), h)
        row = fake.conn.execute("SELECT covariates,scoreable FROM tg_pull WHERE id=?", (h.id,)).fetchone()
        if late_loss:
            listener.guild_played.assert_not_called()
            provider.assert_not_awaited()
            assert "guild_strategy" not in json.loads(row["covariates"])
            assert not row["scoreable"]
            assert (listener.run_key(run), "631:4:0", 0) in listener._farm_at_entry
        else:
            listener.guild_played.assert_called_once_with(19)
            provider.assert_awaited_once_with(run)
            assert json.loads(row["covariates"])["guild_strategy"] == 19
        await listener.close()
    asyncio.run(play())
