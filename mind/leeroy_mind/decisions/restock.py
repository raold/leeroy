"""Weekly craft receipts, bounded by the original bank stock and the live tab."""
import asyncio
from collections import Counter
from fractions import Fraction
import json
import logging
from uuid import uuid4

from leeroy_mind.guild.bankread import SLOTS
from leeroy_mind.realm.bank import BankLedger, STACKS_PER_TAB, STOCK, items_path, load_items
from leeroy_mind.realm.protocol import BankStock
from . import bank as requests

log = logging.getLogger(__name__)
MAKER = {43015: 185, 34753: 185, 46377: 171, 46376: 171, 46379: 171, 46378: 171,
         40111: 755, 40112: 755, 40113: 755, 40118: 755, 40123: 755, 40133: 755,
         34054: 333, 34055: 333, 34056: 333, 34057: 333, 34052: 333, 34053: 333,
         47586: 197, 47601: 197, 47590: 164, 47592: 164, 47596: 165, 47600: 165}
MIN_SKILL = 375
PREFIX = "decisions:restock:"


def targets() -> dict[int, tuple[int, int, int]]:
    result = {}
    for tab, entries in STOCK.items():
        stacks, extra = divmod(STACKS_PER_TAB, len(entries))
        for index, (item, count) in enumerate(entries):
            result[item] = (tab, stacks + (index < extra), count)
    return result


def _plan(bank, makers, cap, limits):
    if not bank or not cap:
        return [], []
    free = {t["tab"]: SLOTS - len(t["slots"]) for t in bank["tabs"]}
    candidates, no_maker = {}, []
    for item, (tab, stacks, count) in targets().items():
        if limits is not None:
            count = min(count, limits.get(item, 0))
        if not count or not free.get(tab) or item not in MAKER:
            continue
        current = sum(n for _, entry, n in requests.slots(bank, tab) if entry == item)
        total = stacks * count
        if current >= total:
            continue
        eligible = [g for g, skills in makers.items() if skills.get(MAKER[item], 0) >= MIN_SKILL]
        if not eligible:
            no_maker.append(item)
            continue
        candidates[item] = (tab, count, total, total-current, eligible)
    planned, credit = [], Counter()
    while candidates and len(planned) < cap:
        item = min(candidates, key=lambda i: (-Fraction(candidates[i][3], candidates[i][2]), i))
        tab, count, total, missing, eligible = candidates.pop(item)
        if not free[tab]:
            continue
        maker = min(eligible, key=lambda g: (credit[g], g))
        amount = min(count, missing)
        planned.append(dict(tab=tab, item=item, count=amount, maker=maker))
        free[tab] -= 1
        credit[maker] += 1
        if missing > amount:
            candidates[item] = (tab, count, total, missing-amount, eligible)
    return planned, sorted(no_maker)


def plan(bank, makers, *, cap, week):
    return _plan(bank, makers, cap, None)[0]


def week_plan(store, bank, makers, *, cap, week, limits):
    if not cap or bank is None:
        return None
    with store.transaction():
        raw = store.get_meta(PREFIX + week)
        if raw is not None:
            return json.loads(raw)
        stacks, missing = _plan(bank, makers, cap, limits)
        for item in missing:
            log.info("decisions_restock_no_maker item=%d", item)
        state = {"stacks": stacks, "no_maker": missing, "results": {}, "week": week}
        store.set_meta(PREFIX + week, json.dumps(state))
        return state


def verdict(state, count):
    if state is None or state[0] == "abandoned":
        return "send"
    if state[0] == "acked" and state[2] is not None:
        return "credited" if state[2] == count else "unknown"
    if state[0] in ("unknown", "refused"):
        return state[0]
    return "wait"


def allowance(bank, row, limits):
    item = row["item"]
    tab, stacks, size = targets()[item]
    count = min(size, limits.get(item, 0))
    slots = next((t["slots"] for t in bank["tabs"] if t["tab"] == tab), None)
    if slots is None or len(slots) >= SLOTS or not 0 < row["count"] <= count:
        return False
    return sum(n for _, i, n in slots if i == item) + row["count"] <= count * stacks


def close_previous_weeks(app, week):
    store = app.store
    for state in history(store.conn):
        stacks, results = state["stacks"], state["results"]
        if state["week"] >= week or len(results) == len(stacks):
            continue
        direct = app.realm.part("direct")
        for index, row in enumerate(stacks):
            token = str(index)
            if token in results:
                continue
            item, maker = row["item"], row["maker"]
            ref = f"restock:{state['week']}:{item}:{index}"
            outcome = verdict(direct.state("decision", ref), row["count"])
            if outcome == "wait":
                continue
            with store.transaction():
                if outcome == "credited":
                    reason = "gathering" if MAKER[item] == 333 else "craft"
                    BankLedger(store, app.clock).record_once(ref, [("in", reason, maker, item, row["count"])])
                results[token] = "skipped" if outcome == "send" else outcome
                store.set_meta(PREFIX + state["week"], json.dumps(state))


async def run(app, *, start=False):
    cap = app.decisions.config.bank_restock_per_week
    week = app.clock.local().strftime("%G-W%V")
    close_previous_weeks(app, week)
    if (not cap or not requests.ready(app) or "bank_stock_target" not in
            (app.decisions.rules_for("bank").features or ())):
        return
    store, key = app.store, PREFIX + week
    raw = store.get_meta(key)
    if not start and not raw:
        return
    state = json.loads(raw) if raw else None
    if state and len(state["results"]) == len(state["stacks"]):
        return
    by = requests.officer(app)
    if not by:
        return
    bank = await requests.read(app, details=True)
    rules = app.decisions.rules_for("bank")
    if not bank or not rules.valid(app, bank) or requests.officer(app) != by:
        return
    limits = await asyncio.to_thread(lambda: load_items(items_path(app.guild.cfg.worldserver_conf)))
    makers = {g: s for g, s in bank.get("professions", {}).items() if g in requests.members(app)}
    if not rules.valid(app, bank):
        return
    state = week_plan(store, bank, makers, cap=cap, week=week, limits=limits)
    direct = app.realm.part("direct")
    # The stage and the minute tick can overlap across an armory read. Only admit an unclaimed ref.
    for index, row in enumerate(state["stacks"]):
        token = str(index)
        if token in state["results"]:
            continue
        item, maker = row["item"], row["maker"]
        ref = f"restock:{week}:{item}:{index}"
        status = verdict(direct.state("decision", ref), row["count"])
        if status == "wait":
            return
        if status == "send":
            def valid():
                return ("bank_stock_target" in (rules.features or ()) and app.clock.local().strftime("%G-W%V") == week
                        and rules.valid(app, bank)
                        and requests.officer(app) == by and maker in requests.members(app)
                        and makers.get(maker, {}).get(MAKER[item], 0) >= MIN_SKILL and allowance(bank, row, limits))
            if valid() and index < cap:
                state["officer"] = by
                store.set_meta(key, json.dumps(state))
                await rules.send(app, BankStock(uuid4().hex, by, row["tab"], item, row["count"],
                                                maker=maker, target=targets()[item][1] * min(targets()[item][2], limits[item])),
                                 ref, valid)
                return
            status = "skipped"
        with store.transaction():
            if status == "credited":
                reason = "gathering" if MAKER[item] == 333 else "craft"
                BankLedger(store, app.clock).record_once(ref, [("in", reason, maker, item, row["count"])])
            state["results"][token] = status
            state["officer"] = by
            store.set_meta(key, json.dumps(state))


def history(conn):
    for row in conn.execute("SELECT value FROM meta WHERE key GLOB ? ORDER BY key DESC", (PREFIX + "*",)):
        yield json.loads(row[0])


def deliveries(state, names):
    counts = Counter((row["item"], row["maker"]) for i, row in enumerate(state["stacks"])
                     if state["results"].get(str(i)) == "credited")
    return [f"{names.get(g, f'#{g}')} · item {item} · {n} stack(s)" for (item, g), n in sorted(counts.items())]


def announcement(state, names):
    """At 60 stacks even 60 different 12-character makers fit in a single Discord post."""
    pairs = Counter((row["maker"], row["item"]) for i, row in enumerate(state["stacks"])
                    if state["results"].get(str(i)) == "credited")
    grouped = {}
    for (guid, item), count in sorted(pairs.items()):
        grouped.setdefault(guid, []).append(f"{item}×{count}")
    lines = []
    for guid, items in grouped.items():
        name = names.get(guid, "")
        name = name if 0 < len(name) <= 12 else f"#{guid}"
        lines.append(f"{name}: {', '.join(items)}")
    return "\n".join(lines)
