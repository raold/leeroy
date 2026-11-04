"""The guild bank: two data steps, each command sent exactly once, and the bank's own ledger.

Tab rights can be set only on tabs that exist, and only from his session (I11), so the bank is
two steps, each a plan he approves (steps.py):

  bank        bank_money (50,000 gold plus the tabs' prices, read from worldserver.conf's
              Guild.BankTabCost0-5), then bank_tab_buy for tabs 0..5, one a second, bought by
              the DKP officer from that gold. The plan lists the rights he sets on the new tabs
              afterwards (H5b); none is ever sent.
  bank_stock  about twenty stacks a stocked tab, from the items file's bank rows. The first
              stack is the probe: no_rights (native has destroyed the stack again) stops the
              step there. After it the rest go one at a time, each after its own job, at most
              two a second.

Every command goes once through the direct ledger, as the data step's item (owner "step", ref
step:<step>:<sha>:<index>); bank_stock expects its job. Each item waits for the one before it.
An unknown outcome is never sent again, and neither is a refusal native gives when it cannot
prove what it did (bank_outcome_unknown and the like): the direct ledger frees such a ref, the
bank does not. The step then ends as needs_controller, and the controller writes a fresh plan
for what is left (plan bank --no-money, --first-tab). Only a refusal native gives before
anything moved is asked again, at the step's next look.

A step's apply only reads the ledger, starts the sends in an owned child (realm.spawn) and
returns, so the Realm's loop never waits on the bank's pacing or its jobs.
"""
from __future__ import annotations

import argparse
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
import json
import logging
from pathlib import Path
import re
import uuid

from leeroy_mind.storage.store import Store

from . import protocol
from .action_policy import classify
from .direct import busy
from .places import MAX_BYTES, MAX_LINE_BYTES, MAX_ROWS
from .ports import title_holders
from .steps import StepError, StepPending, Steps

log = logging.getLogger(__name__)

STEP, STOCK_STEP = "bank", "bank_stock"
OWNER = "step"  # The direct ledger's owner of a data step's items (direct.OWNERS).
TABS = ("raid consumables", "gems & enchants", "mats", "BoEs", "officers", "vanity")
ICONS = ("INV_Misc_Food_15", "INV_Misc_Gem_01", "INV_Misc_Dust_01", "INV_Chest_Chain",
         "INV_Misc_Key_03", "INV_Box_PetCarrier_01")
GOLD = 50_000
MONEY_MAX = 60_000  # Gold one bank_money carries (native and action_policy).
COPPER = 10_000
# Guild.BankTabCost0-5 in copper: the core's compiled-in defaults (WorldConfig.cpp).
CORE_TAB_COSTS = (1_000_000, 2_500_000, 5_000_000, 10_000_000, 25_000_000, 50_000_000)
RIGHTS_NOTE = ("He sets these in the guild control panel once the tabs exist (H5b); the mind "
               "sends none. Every other right stays his.")
# Each tab's stock: (item, count per stack), the stacks spread evenly over the tab's items. A
# count is kept to one stack of the item, so a stack native refuses moved nothing.
STOCK = {
    0: ((43015, 10), (34753, 10), (46377, 20), (46376, 20), (46379, 20), (46378, 20)),  # feasts, flasks
    1: ((40111, 20), (40112, 20), (40113, 20), (40118, 20), (40123, 20), (40133, 20)),  # cut gems
    2: ((34054, 20), (34055, 10), (34056, 10), (34057, 10), (34052, 10), (34053, 10)),  # enchanting
    3: ((47586, 1), (47590, 1), (47592, 1), (47596, 1), (47600, 1), (47601, 1)),  # crafted BoEs
    5: ((8485, 1), (8486, 1), (8487, 1), (8488, 1), (8489, 1), (8490, 1)),  # cats
}
STACKS_PER_TAB = 20
REASONS = ("stock", "gathering", "donation", "prize", "craft", "gift", "consumables")
JOB_KINDS = frozenset({"bank_stock", "bank_deposit", "bank_withdraw"})
BANK_GAP_S, STOCK_GAP_S = 1.0, 0.5
POLL_S, SETTLE_S = 0.25, 120.0
# Refusals native gives before anything moved (busy:* too, which the ledger stores as busy_*),
# and the bridge's own before the wire: the same item may be asked again.
RETRY = frozenset({"pretransport_refused", "native_refused", "job_failed", "realm_off",
                   "realm_not_started", "pin_rejected", "not_officer", "not_cast", "not_member"})
ITEMS_FILE = "leeroy_items.csv"
ITEMS_HEADER = "item_id,use,max_count"
ITEMS_DIST = Path(__file__).resolve().parents[3] / "modules" / "mod-leeroy" / "conf" / "leeroy_items.csv.dist"
ITEM_USES = ("gift", "bank", "holiday", "prize")
MAX_ITEM_COUNT = 1000
_U32 = 0xFFFFFFFF
_TRIM = " \t\r"
_DIGITS = re.compile(r"[0-9]+")
_COST = re.compile(r"^[ \t]*Guild\.BankTabCost([0-5])[ \t]*=[ \t]*(.*?)[ \t\r]*$", re.MULTILINE)


# -- the plans ----------------------------------------------------------------------------------

def _officer(guid) -> int:
    if type(guid) is not int or not 0 < guid <= _U32:
        raise StepError("the officer must be a character guid")
    return guid


def _gold(copper: int) -> int:
    return -(-copper // COPPER)


def tab_prices(worldserver_conf) -> tuple[int, ...]:
    """Guild.BankTabCost0-5 in copper from worldserver.conf, a key it leaves out being the core's
    default. Only those keys are read; an error names a key, never a value or the path."""
    try:
        text = Path(worldserver_conf).read_text(encoding="utf-8", errors="replace")
    except OSError:
        raise StepError("worldserver.conf unavailable") from None
    prices = list(CORE_TAB_COSTS)
    for match in _COST.finditer(text):
        tab, value = int(match.group(1)), match.group(2)
        if len(value) >= 2 and value[0] == value[-1] == '"':
            value = value[1:-1]
        if _DIGITS.fullmatch(value) is None or int(value) > _U32:
            raise StepError(f"worldserver.conf: Guild.BankTabCost{tab} is not a whole number of copper")
        prices[tab] = int(value)
    return tuple(prices)


def _rights() -> list[dict]:
    return [{"ranks": "1 (officer)", "view": list(TABS), "deposit": list(TABS),
             "withdraw_slots": ["mats", "BoEs"]},
            {"ranks": "2 and below, the trial rank included", "view": ["mats"], "deposit": ["mats"],
             "withdraw_slots": ["mats", "BoEs"]}]


def plan_bank(officer_guid, *, prices=CORE_TAB_COSTS, money=True, first_tab=0) -> dict:
    """Step bank: the money (none with money=False), then the tabs from first_tab on, by the
    officer; and the rights he will set, listed, never sent."""
    officer = _officer(officer_guid)
    if (type(prices) not in (tuple, list) or len(prices) != len(TABS)
            or not all(type(p) is int and 0 <= p <= _U32 for p in prices)):
        raise StepError("six tab prices in copper are needed")
    if type(first_tab) is not int or not 0 <= first_tab < len(TABS):
        raise StepError("the first tab must be 0-5")
    if type(money) is not bool:
        raise StepError("money must be true or false")
    tabs = [{"tab": t, "name": TABS[t], "icon": ICONS[t], "price": _gold(prices[t])}
            for t in range(first_tab, len(TABS))]
    amount = GOLD + sum(tab["price"] for tab in tabs) if money else 0
    if amount > MONEY_MAX:
        raise StepError(f"the bank money would be {amount} gold; one bank_money carries at most {MONEY_MAX}")
    return {"officer": officer, "money": amount, "tabs": tabs, "rights": _rights(), "rights_note": RIGHTS_NOTE}


def _items_error(line, why):
    raise StepError(f"items file: line {line}: {why}")


def _uint(text, low, high):
    return int(text) if _DIGITS.fullmatch(text) is not None and low <= int(text) <= high else None


def parse_items(data: bytes) -> dict[int, int]:
    """The bank rows of an items file's bytes (item id: max_count a command), parsed as strictly
    as native's ParseItems (LeeroyRealmCommands.cpp): a file it would refuse is refused whole."""
    if len(data) > MAX_BYTES:
        _items_error(1, "file too large")
    seen, rows, header = set(), {}, False
    for number, raw in enumerate(data.decode("latin-1").split("\n"), 1):
        if len(raw) > MAX_LINE_BYTES:
            _items_error(number, "line too long")
        line = raw.strip(_TRIM)
        if not line or line.startswith("#"):
            continue
        if "\0" in line or '"' in line:
            _items_error(number, "invalid characters")
        if not header:
            if line != ITEMS_HEADER:
                _items_error(number, "invalid header")
            header = True
            continue
        if len(seen) >= MAX_ROWS:
            _items_error(number, "too many rows")
        fields = [field.strip(_TRIM) for field in line.split(",")]
        if len(fields) != 3:
            _items_error(number, "expected 3 columns")
        item, use, limit = _uint(fields[0], 1, _U32), fields[1], _uint(fields[2], 1, MAX_ITEM_COUNT)
        if item is None:
            _items_error(number, "invalid item_id")
        if use not in ITEM_USES:
            _items_error(number, "invalid use")
        if (item, use) in seen:
            _items_error(number, "duplicate item/use")
        if limit is None:
            _items_error(number, "invalid max_count")
        seen.add((item, use))
        if use == "bank":
            rows[item] = limit
    if not header:
        _items_error(1, "missing header")
    return rows


def load_items(path) -> dict[int, int]:
    try:
        data = Path(path).read_bytes()
    except OSError:
        raise StepError("items file unavailable") from None
    return parse_items(data)


def items_path(worldserver_conf) -> Path:
    """The live items file native reads, beside the module configs next to worldserver.conf."""
    live = Path(worldserver_conf).parent / "modules" / ITEMS_FILE
    if not live.is_file():
        raise StepError("the live items file is missing; name one with --items")
    return live


def plan_bank_stock(items_file, officer_guid) -> dict:
    """Step bank_stock: STOCK's stacks, tab by tab, each at most its item's max_count; stacks[0]
    is the probe. A stock item the file has no bank row for is a plan error naming it."""
    officer = _officer(officer_guid)
    limits = load_items(items_file)
    missing = sorted({item for entries in STOCK.values() for item, _ in entries if item not in limits})
    if missing:
        raise StepError("not a bank item in the items file: " + ", ".join(map(str, missing)))
    stacks = []
    for tab, entries in STOCK.items():
        each, extra = divmod(STACKS_PER_TAB, len(entries))
        for n, (item, count) in enumerate(entries):
            stacks.extend([tab, item, min(count, limits[item])] for _ in range(each + (n < extra)))
    return {"officer": officer, "tabs": list(TABS), "stacks": stacks}


def dkp_officer(conn, chosen=None) -> tuple[int, str]:
    """The DKP officer: the one holder of the dkp title on the roster, or `chosen` among them."""
    holders = dict(title_holders(conn)["dkp"])
    roster = {row[0] for row in conn.execute("SELECT guid FROM cast_member")}
    current = sorted(guid for guid in holders if guid in roster)
    if chosen is not None:
        if chosen not in current:
            raise StepError("--officer must name a dkp title holder on the roster")
        return chosen, holders[chosen]
    if len(current) != 1:
        raise StepError(f"the dkp title has {len(current)} holders on the roster; name one with --officer")
    return current[0], holders[current[0]]


def _arguments(argv, stock):
    parser = argparse.ArgumentParser(prog=f"realm plan {'bank-stock' if stock else 'bank'}",
                                     add_help=False, allow_abbrev=False, exit_on_error=False)
    parser.add_argument("--officer", type=int)
    parser.add_argument("--conf")
    if stock:
        parser.add_argument("--items")
    else:
        parser.add_argument("--no-money", action="store_true")
        parser.add_argument("--first-tab", type=int, default=0)
    known = "--officer, --conf, " + ("--items" if stock else "--no-money, --first-tab")
    try:
        args, rest = parser.parse_known_args(list(argv))
    except argparse.ArgumentError:
        raise StepError(f"invalid arguments; known: {known}") from None
    if rest:
        raise StepError(f"unknown arguments; known: {known}")
    return args


def _conf(ctx, args) -> Path:
    if args.conf:
        return Path(args.conf)
    from leeroy_mind.guild.config import load_guild_config
    return load_guild_config(ctx.root / "mind.toml").worldserver_conf


def plan_cli(ctx) -> int:
    """realm plan bank | bank-stock: reads mind.db read-only and worldserver.conf's tab costs or
    the items file, and writes only its plan file."""
    stock = ctx.name == "bank-stock"
    args = _arguments(ctx.argv, stock)
    with closing(ctx.connect()) as conn:
        officer, name = dkp_officer(conn, args.officer)
    if stock:
        step = STOCK_STEP
        payload = plan_bank_stock(Path(args.items) if args.items else items_path(_conf(ctx, args)), officer)
    else:
        step = STEP
        payload = plan_bank(officer, prices=tab_prices(_conf(ctx, args)), money=not args.no_money,
                            first_tab=args.first_tab)
    path = ctx.write_plan(step, payload)
    sha = json.loads(path.read_text(encoding="utf-8"))["sha256"]
    print(json.dumps({"plan": path.name, "sha256": sha, "step": step, "officer_name": name,
                      "payload": payload}, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


# -- the ledger ---------------------------------------------------------------------------------

def _count(value, *, low=0):
    if type(value) is not int or not low <= value <= 0x7FFFFFFF:
        raise ValueError("invalid bank count")
    return value


class BankLedger:
    """What went into the guild bank and what came out (realm_bank). Raids never run short (P1d
    tops them up); this is what a real guild would have spent, so "low on Fish Feasts" can be
    true talk. Given the mind's Store it joins the caller's unit; given a bare connection, each
    write commits by itself."""

    def __init__(self, conn, clock):
        self._store = conn if isinstance(conn, Store) else None
        self._conn = conn.conn if self._store is not None else conn
        self._clock = clock

    def _unit(self):
        return self._store.transaction() if self._store is not None else self._conn

    @staticmethod
    def _row(dir, reason, guid, item_id, count, money):
        if dir not in ("in", "out") or reason not in REASONS:
            raise ValueError("invalid bank direction or reason")
        if type(guid) is not int or not 0 <= guid <= _U32 or type(item_id) is not int or not 0 <= item_id <= _U32:
            raise ValueError("invalid bank guid or item")
        count, money = _count(count), _count(money)
        if (count > 0) != (item_id > 0) or not (count or money):
            raise ValueError("a bank row moves an item count or money")
        return dir, reason, guid, item_id, count, money

    def _insert(self, row):
        return self._conn.execute(
            "INSERT INTO realm_bank(ts,dir,reason,guid,item_id,count,money) VALUES (?,?,?,?,?,?,?)",
            (self._clock.now(), *row)).lastrowid

    def record(self, dir, reason, guid, item_id, count, money=0) -> int:
        row = self._row(dir, reason, guid, item_id, count, money)
        with self._unit():
            return self._insert(row)

    def record_once(self, key: str, rows) -> bool:
        """Record rows (dir, reason, guid, item_id, count[, money]) together with the meta key
        that says they were; False, and nothing written, when that key is there already."""
        checked = [self._row(*row) if len(row) == 6 else self._row(*row, 0) for row in rows]
        with self._unit():
            if self._conn.execute("SELECT 1 FROM meta WHERE key=?", (key,)).fetchone() is not None:
                return False
            for row in checked:
                self._insert(row)
            self._conn.execute("INSERT INTO meta(key,value) VALUES (?,?)",
                               (key, json.dumps({"rows": len(checked), "at": self._clock.now()})))
        return True

    def balance(self, item_id) -> int:
        row = self._conn.execute(
            "SELECT coalesce(sum(CASE dir WHEN 'in' THEN count ELSE -count END),0) FROM realm_bank "
            "WHERE item_id=?", (item_id,)).fetchone()
        return int(row[0])

    def low(self, item_id, threshold) -> bool:
        return self.balance(item_id) < threshold


# -- the steps' applies -------------------------------------------------------------------------

@dataclass(frozen=True, slots=True, eq=False)
class _Item:
    index: int  # the step's ledger index: ref step:<step>:<sha>:<index>
    kind: str
    make: Callable[[], object]  # a fresh command (its own id) for each attempt
    item_id: int = 0


def _new_id() -> str:
    return uuid.uuid4().hex


def _plan_int(value, low, high) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ValueError("invalid bank plan value")
    return value


def _checked(items: list[_Item]) -> list[_Item]:
    """Each item's command as native's bounds take it (action_policy): a plan it would refuse is
    the step's own error, before anything is sent."""
    for item in items:
        classify(item.make())
    return items


def bank_items(payload) -> list[_Item]:
    """The bank step's commands in order: index 0 the money (when there is any), 1 + tab each tab."""
    if type(payload) is not dict or type(payload.get("tabs")) is not list or not payload["tabs"]:
        raise ValueError("invalid bank plan")
    officer = _plan_int(payload.get("officer"), 1, _U32)
    money = _plan_int(payload.get("money"), 0, MONEY_MAX)
    items = [_Item(0, "bank_money", lambda: protocol.BankMoney(_new_id(), money))] if money else []
    for tab in payload["tabs"]:
        if type(tab) is not dict or type(tab.get("name")) is not str or type(tab.get("icon")) is not str:
            raise ValueError("invalid bank plan tab")
        number = _plan_int(tab.get("tab"), 0, len(TABS) - 1)
        if items and items[-1].kind == "bank_tab_buy" and number != items[-1].index:  # 1 + previous
            raise ValueError("bank plan tabs out of order")
        items.append(_Item(1 + number, "bank_tab_buy", lambda t=number, n=tab["name"], i=tab["icon"]:
                           protocol.BankTabBuy(_new_id(), officer, t, n, i)))
    return _checked(items)


def stock_items(payload) -> tuple[int, list[_Item]]:
    """The stock step's officer and its stacks in order; stacks[0] is the probe."""
    if type(payload) is not dict or type(payload.get("stacks")) is not list or not payload["stacks"]:
        raise ValueError("invalid bank stock plan")
    officer = _plan_int(payload.get("officer"), 1, _U32)
    items = []
    for index, stack in enumerate(payload["stacks"]):
        if type(stack) is not list or len(stack) != 3:
            raise ValueError("invalid bank stock stack")
        tab, item, count = (_plan_int(stack[0], 0, len(TABS) - 1), _plan_int(stack[1], 1, _U32),
                            _plan_int(stack[2], 1, MAX_ITEM_COUNT))
        items.append(_Item(index, "bank_stock", lambda t=tab, i=item, c=count:
                           protocol.BankStock(_new_id(), officer, t, i, c), item))
    return officer, _checked(items)


def _verdict(kind, state) -> str:
    """What the ledger's word on one item means for its step: ok, wait, send, no_rights or stop."""
    if state is None:
        return "send"
    name, reason, value = state
    if name == "acked":
        return "ok" if kind not in JOB_KINDS or value is not None else "wait"
    if name in ("prepared", "dispatched"):
        return "wait"
    if name == "abandoned":  # It never reached the wire.
        return "send"
    if name == "refused":
        if reason == "no_rights":
            return "no_rights"
        return "send" if reason in RETRY or busy(reason) else "stop"
    return "stop"  # unknown: it may have been done, so it is never sent again


class Bank:
    """The bank's part: the applies of its two steps, and the guild bank's ledger."""

    name = "bank"

    def __init__(self, realm):
        self.realm = realm
        self.ledger = BankLedger(realm.app.store, realm.app.clock)
        self._runs = {}

    async def apply_bank(self, payload, progress):
        return self._apply(STEP, bank_items(payload), progress, gap=BANK_GAP_S, officer=payload["officer"])

    async def apply_bank_stock(self, payload, progress):
        officer, items = stock_items(payload)
        return self._apply(STOCK_STEP, items, progress, gap=STOCK_GAP_S, officer=officer)

    def _direct(self):
        direct = self.realm.part("direct")
        if direct is None:
            raise StepPending("no direct ledger")
        return direct

    def _first_open(self, direct, items, progress):
        """The first item the ledger does not call done: (position, item, verdict, state)."""
        for position, item in enumerate(items):
            state = direct.state(OWNER, progress.ref(item.index))
            verdict = _verdict(item.kind, state)
            if verdict != "ok":
                return position, item, verdict, state
        return None

    def _apply(self, step, items, progress, *, gap, officer):
        key = (step, progress.sha256)
        running = self._runs.get(key)
        if running is not None and not running.done():
            raise StepPending("bank sends in flight")
        self._runs.pop(key, None)
        direct = self._direct()
        first = self._first_open(direct, items, progress)
        if first is None or first[2] in ("stop", "no_rights"):
            return self._finish(step, direct, items, progress, first, officer)
        task = self.realm.spawn(lambda: self._run(items, progress, gap))
        if task is not None:
            self._runs[key] = task
        raise StepPending("bank sends started")

    def _finish(self, step, direct, items, progress, first, officer):
        done = len(items) if first is None else first[0]
        if step == STOCK_STEP:
            rows = []
            for item in items[:done]:
                value = direct.state(OWNER, progress.ref(item.index))[2]
                if value:
                    rows.append(("in", "stock", officer, item.item_id, value))
            self.ledger.record_once(f"realm:bank_stock:{progress.sha256}", rows)
        if first is None:
            log.info("realm_bank_done step=%s items=%d", step, len(items))
            return {"state": "done", "done": done, "items": len(items), "at": None}
        _, item, verdict, (state, reason, _) = first
        if verdict == "no_rights":
            log.warning("realm_bank_no_rights step=%s index=%d", step, item.index)
        else:
            log.warning("realm_bank_needs_controller step=%s index=%d kind=%s state=%s reason=%s",
                        step, item.index, item.kind, state, reason)
        return {"state": "no_rights" if verdict == "no_rights" else "needs_controller", "done": done,
                "items": len(items), "at": {"index": item.index, "kind": item.kind, "state": state,
                                            "reason": reason}}

    async def _run(self, items, progress, gap):
        """Send what is left in order, each item once and only after the one before it is done;
        stop at the first that is not. The next look reads where it stopped."""
        direct = self.realm.part("direct")
        if direct is None:
            return
        clock, last = self.realm.app.clock, None
        for item in items:
            ref = progress.ref(item.index)
            verdict = _verdict(item.kind, direct.state(OWNER, ref))
            if verdict == "send":
                if last is not None:
                    await clock.sleep(last + gap - clock.now())
                last = clock.now()
                if not await self._send(direct, ref, item):
                    return
                verdict = await self._settled(direct, ref, item.kind)
            elif verdict == "wait":
                verdict = await self._settled(direct, ref, item.kind)
            if verdict != "ok":
                return

    async def _send(self, direct, ref, item) -> bool:
        context = self.realm.action_context()
        if not Steps.ready(context):
            return False
        cmd = item.make()
        if direct.prepare(cmd, owner=OWNER, ref=ref, context=context,
                          expects_job=item.kind in JOB_KINDS) is None:
            return False
        try:
            await self.realm.ports.send(cmd)
        except Exception as error:  # Its ledger row records what became of it.
            log.debug("realm_bank_send_failed kind=%s error=%s", item.kind, type(error).__name__)
        finally:
            direct.abandon_unsent(cmd)  # Only a row that never reached the barrier.
        return True

    async def _settled(self, direct, ref, kind) -> str:
        clock = self.realm.app.clock
        until = clock.now() + SETTLE_S
        while True:
            verdict = _verdict(kind, direct.state(OWNER, ref))
            if verdict != "wait" or clock.now() >= until:
                return verdict
            await clock.sleep(POLL_S)


def attach(realm):
    """The bank's part, its two steps registered with Steps; no I/O."""
    bank = Bank(realm)
    steps = realm.part("steps")
    if steps is not None:
        steps.register(STEP, bank.apply_bank)
        steps.register(STOCK_STEP, bank.apply_bank_stock)
    else:
        log.info("realm_bank_no_steps")
    return bank, ()
