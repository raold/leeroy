from collections import Counter

from leeroy_mind.decisions import restock
from leeroy_mind.realm.bank import ITEMS_DIST, plan_bank_stock


def empty():
    return {"tabs": [{"tab": 0, "slots": []}]}


def test_targets_match_plan_bank_stock():
    stock = plan_bank_stock(ITEMS_DIST, 101)["stacks"]
    assert restock.targets() == {item: (tab, n, count) for (tab, item, count), n
                                 in Counter(tuple(s) for s in stock).items()}


def test_restock_never_above_target_or_into_a_full_tab():
    bank = empty()
    bank["tabs"][0]["slots"] = [[n, 43015, 10] for n in range(4)]
    planned = restock.plan(bank, {101: {185: 450}}, cap=60, week="2026-W47")
    assert {s["item"] for s in planned} == {34753}
    bank["tabs"][0]["slots"] = [[n, 43015, 10] for n in range(98)]
    assert not restock.plan(bank, {101: {185: 450}}, cap=60, week="2026-W47")


def test_weekly_cap_and_stored_plan_survive_restart(store):
    makers = {101: {185: 450}, 102: {185: 400}}
    state = restock.week_plan(store, empty(), makers, cap=2, week="2026-W47", limits={43015: 10})
    assert [r["maker"] for r in state["stacks"]] == [101, 102]
    assert restock.week_plan(store, empty(), {}, cap=40, week="2026-W47", limits={}) == state


def test_unknown_stock_is_never_resent(store):
    assert restock.verdict(("unknown", "recovered_dispatched", None), 10) == "unknown"


def test_no_maker_no_restock():
    assert not restock.plan(empty(), {101: {185: 374}}, cap=4, week="2026-W47")


def test_no_fresh_bank_read_restocks_nothing(store):
    assert restock.week_plan(store, None, {}, cap=20, week="2026-W47", limits={}) is None
    assert store.get_meta("decisions:restock:2026-W47") is None


def test_cap_zero_turns_it_off(store):
    assert restock.week_plan(store, empty(), {101: {185: 450}}, cap=0, week="2026-W47", limits={43015: 10}) is None


def test_enchant_mats_needs_enchanting_from_professions(store, guild, clock):
    from leeroy_mind.decisions.bank import requests_due
    bank = {"tabs": [{"tab": 2, "slots": [[0, 34054, 20], [1, 34054, 20]]}]}
    assert not requests_due(store.conn, bank, store.cast(), guild, now=clock.now())
    bank["professions"] = {102: {333: 375}}
    requests = requests_due(store.conn, bank, store.cast(), guild, now=clock.now())
    assert [(r["kind"], r["guid"], r["item"]) for r in requests] == [("enchant_mats", 102, 34054)]
