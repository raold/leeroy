from dataclasses import replace

import pytest

from leeroy_mind.decisions import ledger, ranks
from leeroy_mind.guild.roster import rank_changes

RANKS = {"officer": 1, "raider": 2, "social": 3}


def activate(store, guid, rank, now):
    ident = ledger.propose(store, kind="rank", subject=f"rank:{guid}", value={"guid": guid, "rank": rank},
                           positions=[], now=now)
    ledger.settle(store, ident, settled_by=101, now=now)
    ledger.take_effect(store, ident, effect_ref={"rank": rank}, now=now)
    return ident


def test_sync_ranks_honours_the_overlay(store, guild, clock):
    activate(store, 103, 4, clock.now())
    cast = store.cast()
    cast[103] = replace(cast[103], rank=2)
    assert (103, 4) in rank_changes(cast, guild.get, 1, RANKS, ranks.rank_overlay(store.conn))


def test_newcomer_becomes_trial_and_is_promoted_after_four_raids(store, guild, clock):
    now, cast = clock.now(), store.cast()
    old = {g: m for g, m in cast.items() if g != 105}
    ranks.observe_cast(store, old, now=now-3600)
    ranks.observe_cast(store, cast, now=now)
    due = ranks.review_due(store.conn, cast, guild, RANKS, 4, now=now)
    assert [r["guid"] for r in due if r["why"] == "newcomer"] == [105]
    cast[105] = replace(cast[105], rank=4)
    for i in range(4):
        rid = i+10
        store.conn.execute("INSERT INTO guild_raid VALUES (?,?,?,'icc',10,0,0,102,101,102,1,0,NULL)",
                           (rid, now-3600, now))
        store.conn.execute("INSERT INTO guild_attendance VALUES (?,105,'raided')", (rid,))
    result = next(r for r in ranks.review_due(store.conn, cast, guild, RANKS, 4, now=now) if r["guid"] == 105)
    assert result["rank"] == 2 and result["raids"] == 4


def test_never_his_rank_never_rank_zero(store, guild, clock):
    now = clock.now()
    activate(store, 1, 4, now)
    activate(store, 103, 0, now)
    activate(store, 104, 2, now)
    store.conn.execute("UPDATE cast_member SET rank=0 WHERE guid=104")
    assert ranks.rank_overlay(store.conn) == {}


def test_undo_restores_the_persona_rank(store, guild, clock):
    now = clock.now()
    ident = activate(store, 103, 4, now)
    cast = store.cast()
    cast[103] = replace(cast[103], rank=4)
    ledger.overrule(store, ident, now=now)
    changes = rank_changes(cast, guild.get, 1, RANKS, ranks.rank_overlay(store.conn))
    assert (103, RANKS[guild[103].play.guild_role]) in changes


def test_departed_officers_and_departed_target_cannot_settle_or_apply(store, guild, clock):
    import asyncio
    from types import SimpleNamespace as NS
    from leeroy_mind.decisions import polls
    cast = {g:replace(m, rank=1 if g in (102,104) else 3) for g,m in store.cast().items()}
    app = NS(store=store, human_guid=1, clock=clock,world=NS(members=cast),personas=guild,
             guild=NS(cfg=NS(ranks=RANKS),roster=NS(_ranks_due=False)))
    rules = ranks.rank_rules()
    rules.seen = True
    async def review(target, remove, after):
        now = clock.now()
        voters = [102,104]
        poll = polls.open_poll(store,channel="applications",question="Approve?", options=["approve","keep"],
                               voters=voters,closes=now+60,now=now,kind="rank",subject=f"rank:{target}")
        ident = ledger.propose(store,kind="rank",subject=f"rank:{target}", value={"guid":target,"rank":4},
                               positions=[],now=now,poll=poll)
        for g in voters:
            assert polls.vote(store,poll,g,"approve",now=now)
        clock._t += 60
        polls.close_due(store,now=clock.now())
        previous = app.world.members
        changed = {g:m for g,m in previous.items() if g not in remove}
        if not after:
            app.world.members = changed
        await rules.closed(app,polls.get_poll(store.conn,poll),"approve")
        if after:
            assert ledger.get(store.conn,ident)["status"] == "settled"
            app.world.members = changed
            await rules.tick(app,clock.now())
        assert ledger.get(store.conn,ident)["status"] == "expired"
        app.world.members = previous
    ranks.observe_cast(store, {g:m for g,m in cast.items() if g != 105}, now=clock.now()-60)
    ranks.observe_cast(store, cast, now=clock.now())
    for remove, after in [({102,104},False),({105},False),({102,104},True)]:
        asyncio.run(review(105,remove,after))
    assert not app.guild.roster._ranks_due


def test_owner_abstention_and_explicit_vote_in_one_officer_review(store, guild, clock):
    import asyncio
    from types import SimpleNamespace as NS
    from leeroy_mind.decisions import polls
    cast = {g:replace(m, rank=1 if g == 102 else 3) for g,m in store.cast().items()}
    rules = ranks.rank_rules()
    rules.seen = True
    app = NS(store=store, clock=clock, world=NS(members=cast), human_guid=1, personas=guild,
             guild=NS(cfg=NS(ranks=RANKS)))
    ranks.observe_cast(store, {g:m for g,m in cast.items() if g != 105}, now=clock.now()-1)
    ranks.observe_cast(store, cast, now=clock.now())
    for owner_vote, expect in [("keep","expired"), ("approve","settled"), (None,"settled")]:
        now = clock.now()
        poll = polls.open_poll(store, channel="applications",question="Review?",options=("approve","keep"),
                               voters=[102],closes=now+60,now=now,kind="rank",subject="rank:105")
        ident = ledger.propose(store,kind="rank",subject="rank:105",value={"guid":105,"rank":4},positions=[],
                               now=now,poll=poll)
        assert polls.vote(store,poll,102,"approve",now=now)
        if owner_vote:
            assert polls.vote(store,poll,1,owner_vote,now=now)
        clock._t += 60
        polls.close_due(store,now=clock.now())
        stored = polls.get_poll(store.conn,poll)
        asyncio.run(rules.closed(app,stored,stored["result"]))
        assert ledger.get(store.conn,ident)["status"] == expect
        if expect == "settled":
            ledger.overrule(store,ident,now=clock.now())


@pytest.mark.parametrize("disconnect", [False, True])
def test_scheduled_rank_result_uses_current_electorate_after_reconnect(store, guild, clock, disconnect):
    import asyncio
    from types import SimpleNamespace as NS
    from leeroy_mind.decisions import polls
    from leeroy_mind.decisions.service import DecisionsService
    from leeroy_mind.console.decisionsview import poll_card

    cast = {g: replace(m, rank=1 if g in (102, 103, 104) else 3) for g, m in store.cast().items()}
    app = NS(store=store, human_guid=1, clock=clock, personas=guild, world=NS(members=cast),
             guild=NS(cfg=NS(ranks=RANKS), roster=NS(_ranks_due=False)))
    from leeroy_mind.decisions.config import DecisionsConfig
    service = DecisionsService(app, DecisionsConfig(enabled=True, rank_reviews=True))
    rules = service.rules_for("rank")
    ranks.observe_cast(store, {g: m for g, m in cast.items() if g != 105}, now=clock.now()-1)
    ranks.observe_cast(store, cast, now=clock.now())

    async def review(remaining, expected_result, expected_status):
        app.world.members = cast
        now = clock.now()
        poll = polls.open_poll(store, channel="applications", question="Promote?", options=("approve", "keep"),
                               voters=[102, 103, 104], closes=now+60, now=now, kind="rank", subject="rank:105")
        ident = ledger.propose(store, kind="rank", subject="rank:105", value={"guid":105, "rank":4},
                               positions=[], now=now, poll=poll)
        for guid, choice in [(102, "approve"), (103, "keep"), (104, "keep")]:
            assert polls.vote(store, poll, guid, choice, now=now)
        clock._t = now + 60
        polls.close_due(store, now=clock.now())
        if disconnect:
            await service.on_connection(False)
            await service.deliver()
            await service.on_connection(True)
            await service.deliver()
            waiting = polls.get_poll(store.conn, poll)
            assert waiting["status"] == "closed" and waiting["handled"] is None and waiting["result"] is None
            assert waiting["hook_tries"] == 0 and ledger.get(store.conn, ident)["status"] == "proposed"
            assert not polls.vote(store, poll, 1, "approve", now=clock.now())

        app.world.members = {g:m for g,m in cast.items() if g not in (102,103,104) or g in remaining}
        ranks.observe_cast(store, app.world.members, now=clock.now())
        rules.seen = True
        await service.deliver()
        closed = polls.get_poll(store.conn, poll)
        assert closed["handled"] is not None and closed["result"] == expected_result
        assert polls.tally(store.conn, poll) == {"approve": 1, "keep": 2}
        app.world.members = cast
        card = poll_card(store.conn, closed, app.human_guid)
        assert card["voters"] == len(remaining)
        assert {o["code"]: o["count"] for o in card["options"]} == {
            "approve": int(102 in remaining), "keep": len(remaining & {103, 104})}
        assert ledger.get(store.conn, ident)["status"] == expected_status
        if expected_status == "settled":
            ledger.overrule(store, ident, now=clock.now())

    for remaining, result, status in [({102}, "approve", "settled"), ({102,103}, None, "expired"),
                                      ({103,104}, "keep", "expired"), (set(), None, "expired")]:
        asyncio.run(review(remaining, result, status))
