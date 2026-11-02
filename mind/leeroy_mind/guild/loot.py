"""Need-first master loot with durable per-slot receipts and serialized awards."""

import asyncio
from dataclasses import asdict
import hashlib
import json
from typing import TYPE_CHECKING
from weakref import WeakValueDictionary

from .dkp import Award, Bidder, LedgerEntry, award
from .lines import member_name, public_text, template
from .pick import is_claim
from .protocol import GiveLoot
from .roles import role_of, stat_family
from .store import _insert_ledger, balances
from .upgrades import upgrade_gain

if TYPE_CHECKING:
    from .session import RaidContextInfo


# Reopening an active session must not create a second pricing/remote-give lane.
# Weak values release locks when their session wrappers disappear.
_LOCKS: WeakValueDictionary = WeakValueDictionary()


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _get(conn, key):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row is not None else None


def _put(conn, key, value):
    """Join the caller's transaction; never commit a ledger separately from its receipt."""
    conn.execute("INSERT INTO meta(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, json.dumps(value, sort_keys=True, separators=(",", ":"))))


def _once(conn, key) -> bool:
    with conn:
        return conn.execute("INSERT OR IGNORE INTO meta(key,value) VALUES (?, 'true')", (key,)).rowcount == 1


def price_category(item) -> str:
    if item.inventory_type == 17:
        return "weapon_2h"
    if item.inventory_type in {13, 15, 21, 22, 25, 26}:
        return "weapon_1h"
    if item.inventory_type == 12:
        return "trinket"
    if item.item_class == 15 and item.allowable_class not in {-1, 0}:
        return "token"
    return "other"


def item_price(item, prices) -> int:
    return int(prices[price_category(item)])


def wants_item(wants, name) -> bool:
    name = name.strip().casefold()
    return bool(name) and any(isinstance(want, str) and bool(want.strip())
                             and (want.strip().casefold() in name or name in want.strip().casefold())
                             for want in wants)


class LootMaster:
    def __init__(self, ports, cfg, armory, ctx: "RaidContextInfo"):
        self.ports, self.cfg, self.armory, self.ctx = ports, cfg, armory, ctx
        self._members = tuple(dict.fromkeys(ctx.plan.members))
        self._prefix = f"guild:loot:{ctx.raid_id}:"
        lock_key = (id(ports.conn), ctx.raid_id)
        self._lock = _LOCKS.get(lock_key)
        if self._lock is None:
            self._lock = asyncio.Lock()
            _LOCKS[lock_key] = self._lock
        self._closed = False

    def _records(self):
        rows = self.ports.conn.execute("SELECT key,value FROM meta WHERE key LIKE ? ORDER BY key",
                                       (self._prefix + "%",)).fetchall()
        return [(key, json.loads(value)) for key, value in rows]

    def _active(self):
        row = self.ports.conn.execute("SELECT ended FROM guild_raid WHERE raid_id=?", (self.ctx.raid_id,)).fetchone()
        return not self._closed and row is not None and row[0] is None

    async def on_loot_drop(self, ev):
        # A LootDrop has no participant GUIDs. It cannot establish instance ownership.
        if (not self._active() or self.ctx.instance_id is None or ev.map != self.ctx.map_id
                or ev.instance_id != self.ctx.instance_id):
            return
        async with self._lock:
            if not self._active():
                return
            pending_n = sum(record.get("state") in {"pending", "sending", "acknowledged"}
                            for _, record in self._records())
            fresh = []
            seen = set()
            for slot in ev.items:
                if (type(slot.index) is not int or not 0 <= slot.index <= 255
                        or type(slot.item_id) is not int or slot.item_id <= 0 or slot.quality < 4):
                    continue
                identity = (ev.map, ev.instance_id, ev.source_entry, ev.ts, slot.index)
                key = self._prefix + _digest(identity)
                if key in seen or _get(self.ports.conn, key) is not None:
                    continue
                seen.add(key)
                fresh.append((key, slot))
                if pending_n + len(fresh) >= 128:
                    break
            if not fresh or pending_n >= 128:
                return
            members = tuple(g for g in self._members if g != self.ports.human_guid)
            items, equipment = await asyncio.gather(
                asyncio.to_thread(self.armory.items, tuple(slot.item_id for _, slot in fresh)),
                asyncio.to_thread(self.armory.equipped, members))
            if not self._active() or self.ctx.instance_id != ev.instance_id:
                return
            for key, slot in fresh:
                if not self._active():
                    break
                item = items.get(slot.item_id)
                bids = []
                if item is not None:
                    cast = self.ports.cast()
                    for guid in members:
                        member, persona = cast.get(guid), self.ports.persona(guid)
                        if member is None or not member.online or persona is None:
                            continue
                        role = role_of(member.class_, member.spec_tab, persona.play.raid_role)
                        gain = upgrade_gain(item, member.class_, stat_family(member.class_, member.spec_tab, role),
                                            equipment.get(guid, {}))
                        bids.append({"guid": guid, "gain": gain,
                                     "wanted": wants_item(persona.game.wants, item.name)})
                now = self.ports.clock.now()
                ordinal = max((row.get("ordinal", 0) for _, row in self._records()), default=0) + 1
                record = dict(state="pending", map=ev.map, instance_id=ev.instance_id,
                              source_entry=ev.source_entry, index=slot.index, item_id=slot.item_id,
                              name=public_text(self.ports, item.name if item else slot.name)[:100],
                              price=item_price(item, self.cfg.dkp.prices) if item else 0, known=item is not None,
                              bidders=bids, human_claimed=False,
                              due=now + (self.cfg.dkp.claim_window_s if self.ctx.plan.human_in_raid else 3),
                              received=now, ordinal=ordinal,
                              command_id="guild-loot:" + str(self.ctx.raid_id) + ":" + key[-24:])
                with self.ports.conn:
                    inserted = self.ports.conn.execute("INSERT OR IGNORE INTO meta(key,value) VALUES (?,?)",
                                                       (key, json.dumps(record))).rowcount
                if inserted:
                    await self.ports.say(self.ctx.plan.loot_guid, "raid", template("loot_call", item=record["name"]))

    def on_human_chat(self, text):
        if not self._active() or not self.ctx.plan.human_in_raid or not is_claim(text):
            return
        now = self.ports.clock.now()
        pending = [(key, row) for key, row in self._records() if row["state"] == "pending" and now < row["due"]]
        if pending:
            key, row = min(pending, key=lambda pair: (pair[1]["received"], pair[1]["ordinal"]))
            row["human_claimed"] = True
            with self.ports.conn:
                _put(self.ports.conn, key, row)

    async def _finish(self, key, record):
        result = Award(**record["award"])
        with self.ports.conn:
            current = _get(self.ports.conn, key)
            if current is None or current["state"] != "acknowledged":
                return
            _insert_ledger(self.ports.conn, [LedgerEntry(self.ports.clock.now(), result.guid, -result.paid,
                                                       "item", self.ctx.raid_id, record["item_id"], key)])
            current["state"] = "charged"
            _put(self.ports.conn, key, current)
        facts = dict(item=record["name"], winner=member_name(self.ports, result.guid), paid=result.paid)
        if result.close:
            facts.update(runner_up=member_name(self.ports, result.runner_up), runner_bid=result.runner_up_bid)
        await self.ports.say(self.ctx.plan.loot_guid, "raid",
                             template("loot_award_close" if result.close else "loot_award", **facts))
        if result.close and result.runner_up is not None:
            await self.ports.loot_lost(result.runner_up, record["name"])

    async def tick(self):
        async with self._lock:
            if not self._active():
                return
            due = sorted(self._records(), key=lambda pair: (pair[1]["due"], pair[1]["ordinal"]))
            for key, _ in due:
                if not self._active():
                    return
                record = _get(self.ports.conn, key)
                if record["state"] == "acknowledged":
                    await self._finish(key, record)
                    continue
                if record["state"] != "pending" or record["due"] > self.ports.clock.now():
                    continue
                if (self.ctx.instance_id is None or record["instance_id"] != self.ctx.instance_id
                        or record["map"] != self.ctx.map_id):
                    continue
                current_balances = balances(self.ports.conn)
                cast = self.ports.cast()
                bidders = [Bidder(b["guid"], current_balances.get(b["guid"], 0), b["gain"], b["wanted"])
                           for b in record["bidders"] if b["guid"] in cast and cast[b["guid"]].online]
                result = (award(record["price"], bidders, human_claimed=record["human_claimed"],
                                human_guid=self.ports.human_guid) if record["known"] else None)
                with self.ports.conn:
                    current = _get(self.ports.conn, key)
                    if current["state"] != "pending":
                        continue
                    current["state"] = "sending" if result is not None else "unclaimed"
                    _put(self.ports.conn, key, current)
                if result is None:
                    await self.ports.say(self.ctx.plan.loot_guid, "raid", template("loot_unclaimed", item=record["name"]))
                    continue
                command = GiveLoot(id=record["command_id"], map=record["map"], instance_id=record["instance_id"],
                                   source_entry=record["source_entry"], index=record["index"],
                                   item_id=record["item_id"], to_guid=result.guid)
                try:
                    ack = await self.ports.send(command)
                except BaseException:
                    # Cancellation/connection loss can hide a successful remote give.
                    # Retrying the action is unsafe; only an explicit matching ack charges.
                    with self.ports.conn:
                        current["state"] = "uncertain"
                        _put(self.ports.conn, key, current)
                    raise
                if ack.ok is True and ack.id == command.id:
                    record["state"], record["award"] = "acknowledged", asdict(result)
                    with self.ports.conn:
                        _put(self.ports.conn, key, record)
                    await self._finish(key, record)
                else:
                    current["state"] = "failed" if ack.id == command.id else "uncertain"
                    with self.ports.conn:
                        _put(self.ports.conn, key, current)
                    error = public_text(self.ports, ack.error if ack.id == command.id else "wrong_ack")[:80]
                    await self.ports.say(self.ctx.plan.loot_guid, "raid",
                                         template("loot_give_failed", item=record["name"], error=error))

    async def close(self):
        self._closed = True
        async with self._lock:
            # A matching ack is durable proof of the remote award. Reconcile that
            # local debt before the session becomes inactive, including after a
            # previous database failure. A further failure leaves close retryable.
            for key, record in self._records():
                if record["state"] == "acknowledged":
                    await self._finish(key, record)
            with self.ports.conn:
                for key, record in self._records():
                    if record["state"] == "pending":
                        record["state"] = "cancelled"
                        _put(self.ports.conn, key, record)
