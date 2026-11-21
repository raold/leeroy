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


def test_week_rollover_credits_late_jobs_without_starting_unissued_stacks(store, clock):
    import json
    from types import SimpleNamespace as NS
    week = "2026-W47"
    rows = [{"item": 43015, "count": 20, "maker": 101}] * 2
    key = restock.PREFIX + week
    store.set_meta(key, json.dumps(dict(week=week, stacks=rows, results={}, no_maker=[], officer=104)))
    direct = NS(state=lambda owner, ref: ("acked", None, 20) if ref.endswith(":0") else None)
    app = NS(store=store, clock=clock, realm=NS(part=lambda _: direct))
    for _ in range(2):
        restock.close_previous_weeks(app, "2026-W48")
    assert json.loads(store.get_meta(key))["results"] == {"0": "credited", "1": "skipped"}
    assert store.conn.execute("SELECT count(*) FROM realm_bank WHERE reason='craft'").fetchone()[0] == 1


def test_sixty_distinct_deliveries_fit_one_wrapped_discord_post():
    from leeroy_mind.discord.text import MAX_TEXT
    rows = [dict(maker=g, item=43015, count=20) for g in range(101, 161)]
    names = {g: "Ninelette"+str(g) for g in range(101, 161)}
    block = restock.announcement({"stacks":rows, "results":dict.fromkeys(map(str,range(60)), "credited")}, names)
    assert len("We filled the tab.\n```\n"+block+"\n```") <= MAX_TEXT
    assert all(name in block for name in names.values()) and block.count("43015×1") == 60


def test_unsettled_stack_does_not_block_an_independent_stack_or_late_credit(store, clock, monkeypatch):
    import asyncio
    import json
    from types import SimpleNamespace as NS
    from unittest.mock import AsyncMock
    week = clock.local().strftime("%G-W%V")
    stacks = [dict(item=43015, maker=101, tab=0, count=10), dict(item=34753, maker=102, tab=0, count=10)]
    state = dict(week=week, stacks=stacks, results={}, no_maker=[],officer=104)
    store.set_meta(restock.PREFIX+week,json.dumps(state))
    first, second = f"restock:{week}:43015:0", f"restock:{week}:34753:1"
    evidence = {first:("acked",None,None)}
    direct = NS(state=lambda owner,ref: evidence.get(ref))
    sent = []
    async def send(app,cmd,ref,check):
        assert check()
        sent.append(ref)
        evidence[ref] = ("acked",None,10)
    rules = NS(features={"bank_stock_target"},valid=lambda *_:True,send=send)
    app = NS(store=store,clock=clock,realm=NS(part=lambda _:direct), guild=NS(cfg=NS(worldserver_conf="mock")),
             decisions=NS(config=NS(bank_restock_per_week=2),rules_for=lambda _:rules))
    bank = dict(empty(),professions={101:{185:450},102:{185:450}})
    monkeypatch.setattr(restock.requests,"read",AsyncMock(return_value=bank))
    monkeypatch.setattr(restock.requests,"ready",lambda _: True)
    monkeypatch.setattr(restock.requests,"officer",lambda _:104)
    monkeypatch.setattr(restock.requests,"members",lambda _: {101,102,104})
    monkeypatch.setattr(restock,"load_items",lambda _: {43015:10,34753:10})
    monkeypatch.setattr(restock,"items_path",lambda _:None)
    asyncio.run(restock.run(app))
    assert sent == [second]
    evidence[first] = ("unknown","recovered_acked",None)
    asyncio.run(restock.run(app))
    assert json.loads(store.get_meta(restock.PREFIX+week))["results"] == {"0":"unknown","1":"credited"}
    evidence[first] = ("acked",None,10)
    for _ in range(2):
        restock.close_previous_weeks(app,"9999-W01")
    assert sent == [second]
    assert json.loads(store.get_meta(restock.PREFIX+week))["results"] == {"0":"credited","1":"credited"}
    assert store.conn.execute("SELECT count(*) FROM realm_bank WHERE reason='craft'").fetchone()[0] == 2
