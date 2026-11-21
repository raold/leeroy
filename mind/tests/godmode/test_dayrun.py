"""The day block must follow exact dispatch and completion evidence."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from leeroy_mind.godmode import dayrun
from leeroy_mind.guild.protocol import BotDo
from leeroy_mind.realm.action_policy import classify
from leeroy_mind.realm.protocol import RealmMove, RpgStatus, Duel

PERMIT = "a" * 32


def test_execution_uses_only_realm_moves_and_skips_refusals():
    move = RealmMove("first", 101, "north_pond", day_permit=PERMIT)
    assert classify(move).reserved_manifest == (101,)
    fishing = BotDo("cast", (101,), "do", "go fishing", day_permit=PERMIT)
    assert classify(fishing).policy == "ordinary"
    with pytest.raises(ValueError):
        classify(replace(fishing, param="other"))
    with pytest.raises(ValueError):
        classify(replace(fishing, day_permit=""))
    assert dayrun.disposition(SimpleNamespace(state="completed", result_ok=False,
        result_reason="refused")) == "skipped"
    assert dayrun.disposition(SimpleNamespace(state="unknown", result_ok=False,
        result_reason="job_failed")) == "pending"


def test_plan_never_moves_into_his_group_or_instance():
    with pytest.raises(ValueError):
        classify(RealmMove("move", 101, "near_human", day_permit=PERMIT))
    with pytest.raises(ValueError):
        classify(RealmMove("move", 101, "ironforge", social=True, day_permit=PERMIT))
    with pytest.raises(ValueError):
        classify(RpgStatus("rpg", 101, "travel flight", day_permit=PERMIT))
    assert classify(Duel("duel", 101, 102, day_permit=PERMIT)).reserved_manifest == (101,102)


def test_his_pull_preempts_the_block(store):
    for guid, partner in ((101,102),(102,101)):
        store.conn.execute("INSERT INTO day_plan(night,member,starts,ends,activity,place,partners,status) "
                           "VALUES('2026-11-20',?,10,20,'duel_practice','duel_site',?,'planned')",
                           (guid, f"[{partner}]"))
    rows = store.conn.execute("SELECT * FROM day_plan ORDER BY id").fetchall()
    dayrun.preempt_rows(store, rows, "his_group")
    dayrun.preempt_rows(store, rows, "his_group")
    assert [tuple(r) for r in store.conn.execute("SELECT status,reason FROM day_plan")] == [
        ("preempted","his_group"), ("preempted","his_group")]
    assert store.conn.execute("SELECT count(*) FROM memory").fetchone()[0] == 0


def test_plan_block_reaches_card_and_memory(store):
    from leeroy_mind.awareness.card import card, CardContext
    store.conn.execute("INSERT INTO day_plan(night,member,starts,ends,activity,place,status) "
                       "VALUES('2026-11-20',101,10,20,'fishing','north_pond','planned')")
    for channel in ("guild", "discord:general"):
        context = CardContext(channel=channel, now=15, day_plans=True)
        assert "north pond" not in card(store.conn,101,context=context).render().lower()
    assert dayrun.disposition(SimpleNamespace(state="dispatched", result_ok=None,
        result_reason=None)) == "pending"
