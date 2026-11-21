from dataclasses import replace

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
