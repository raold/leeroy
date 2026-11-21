"""Need-first master loot with durable per-slot receipts and serialized awards."""

import asyncio
from dataclasses import asdict
import hashlib
import json
import logging
from typing import TYPE_CHECKING
from weakref import WeakValueDictionary

from leeroy_mind.bridge import NotConnected, valid_preserved_id

from .dkp import Award, Bidder, LedgerEntry, award
from .lines import member_name, public_text, template
from .pick import is_claim
from .ports import TransportUnavailable
from .protocol import GiveLoot
from .roles import role_of, stat_family
from .store import _insert_ledger, balances
from .upgrades import upgrade_gain

if TYPE_CHECKING:
    from .session import RaidContextInfo


log = logging.getLogger(__name__)

# Reopening an active session must not create a second pricing/remote-give lane.
# Weak values release locks when their session wrappers disappear.
_LOCKS: WeakValueDictionary = WeakValueDictionary()

# A hand-out whose outcome is unknown (no ack, a lost connection, a wrong ack, a cancelled give) is
# sent once more, to the same winner, after this long. The corpse is the guard against a second
# copy: a give that went through left its slot looted, so the second answers `no_item`.
RETRY_AFTER_S = 30
MAX_ATTEMPT = 1


def command_id(raid_id, key, attempt=0) -> str:
    """A slot's give_loot identity on the wire: letters, digits and dashes (the bridge refuses
    anything else; until 2026-11-21 the id carried colons and every hand-out was refused before
    it left the mind). Each attempt has its own: the bridge never reissues an identity."""
    ident = f"guild-loot-{int(raid_id)}-{key[-24:]}" + (f"-r{attempt}" if attempt else "")
    if not valid_preserved_id(ident):
        raise ValueError("invalid loot command identity")
    return ident


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
        self._rule = None

    def _loot_rule(self):
        """The guild's loot rule for this raid (decisions.loot_rules, fixed when it opened), or
        None while the guild's decisions are off: today's DKP."""
        if self._rule is None:
            hook = getattr(self.ports, "loot_rule", None)
            self._rule = hook(self.ctx.raid_id) if hook is not None else None
        return self._rule

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
            rule = self._loot_rule()
            if rule is not None:
                from leeroy_mind.decisions.loot_rules import loot_role, price_of, shares_of
                shares = shares_of(self.ports.conn, members, now=self.ports.clock.now())
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
                                            equipment.get(guid, {}), role=role)
                        bids.append({"guid": guid, "gain": gain,
                                     "wanted": wants_item(persona.game.wants, item.name)})
                        if rule is not None:
                            bids[-1].update(role=loot_role(member.class_, member.spec_tab, persona.play.raid_role),
                                            share=round(shares.get(guid, 0.0), 4))
                now = self.ports.clock.now()
                ordinal = max((row.get("ordinal", 0) for _, row in self._records()), default=0) + 1
                record = dict(state="pending", map=ev.map, instance_id=ev.instance_id,
                              source_entry=ev.source_entry, index=slot.index, item_id=slot.item_id,
                              name=public_text(self.ports, item.name if item else slot.name)[:100],
                              price=item_price(item, self.cfg.dkp.prices) if item else 0, known=item is not None,
                              bidders=bids, human_claimed=False,
                              due=now + (self.cfg.dkp.claim_window_s if self.ctx.plan.human_in_raid else 3),
                              received=now, ordinal=ordinal,
                              command_id=command_id(self.ctx.raid_id, key))
                if rule is not None:
                    # The guild's rule: its price for the slot, and the slot its role priorities name
                    record.update(mode=rule.mode, slot=price_category(item) if item else "other")
                    if item is not None:
                        record["price"] = price_of(rule, record["slot"], self.cfg.dkp.prices)
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
        # A loot council charges nothing: its row says so (decisions.loot_rules)
        council = record.get("mode") == "loot_council"
        with self.ports.conn:
            current = _get(self.ports.conn, key)
            if current is None or current["state"] != "acknowledged":
                return
            _insert_ledger(self.ports.conn, [LedgerEntry(self.ports.clock.now(), result.guid,
                                                       0.0 if council else -result.paid, "item", self.ctx.raid_id,
                                                       record["item_id"], "council:" + key if council else key)])
            current["state"] = "charged"
            _put(self.ports.conn, key, current)
        facts = dict(item=record["name"], winner=member_name(self.ports, result.guid), paid=result.paid)
        if result.close:
            facts.update(runner_up=member_name(self.ports, result.runner_up), runner_bid=result.runner_up_bid)
        line = "loot_award_council" if council else "loot_award_close" if result.close else "loot_award"
        await self.ports.say(self.ctx.plan.loot_guid, "raid", template(line, **facts))
        if result.close and result.runner_up is not None:
            await self.ports.loot_lost(result.runner_up, record["name"])
        elif (record.get("mode") is not None and record.get("human_claimed") is True
              and result.guid == self.ports.human_guid and result.runner_up is not None):
            # His claim over a member who wanted it: she remembers losing it (decisions)
            await self.ports.loot_lost(result.runner_up, record["name"])

    def _held(self) -> dict[int, float]:
        """DKP this raid's unsettled hand-outs may still charge, by member: an acknowledged give
        not charged yet, and a give of unknown outcome that will be sent again. Later bids leave
        it aside, so one balance never pays for two drops."""
        held = {}
        for _, row in self._records():
            if row.get("mode") == "loot_council":
                continue  # a council charges nothing
            if row.get("state") == "acknowledged":
                pay = row.get("award")
            elif row.get("state") in {"sending", "uncertain"} and row.get("attempt", 0) < MAX_ATTEMPT:
                pay = row.get("intent")
            else:
                continue
            if isinstance(pay, dict) and type(pay.get("guid")) is int and isinstance(pay.get("paid"), (int, float)):
                held[pay["guid"]] = held.get(pay["guid"], 0) + pay["paid"]
        return held

    def _here(self, record) -> bool:
        return (self.ctx.instance_id is not None and record["instance_id"] == self.ctx.instance_id
                and record["map"] == self.ctx.map_id)

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
                if record["state"] in {"uncertain", "sending"}:
                    # "sending" under this lock is a give an earlier process never settled
                    if (not isinstance(record.get("intent"), dict) or record.get("attempt", 0) >= MAX_ATTEMPT
                            or record.get("retry_at", 0) > self.ports.clock.now() or not self._here(record)):
                        continue
                    attempt = record.get("attempt", 0) + 1
                    with self.ports.conn:
                        record.update(state="sending", attempt=attempt)
                        _put(self.ports.conn, key, record)
                    if await self._give(key, record, Award(**record["intent"]), attempt):
                        return
                    continue
                if record["state"] != "pending" or record["due"] > self.ports.clock.now():
                    continue
                if not self._here(record):
                    continue
                current_balances = balances(self.ports.conn)
                held = self._held()
                cast = self.ports.cast()
                bidders = [Bidder(b["guid"], current_balances.get(b["guid"], 0) - held.get(b["guid"], 0),
                                  b["gain"], b["wanted"])
                           for b in record["bidders"] if b["guid"] in cast and cast[b["guid"]].online]
                if not record["known"]:
                    result = None
                elif record.get("mode") is None:
                    result = award(record["price"], bidders, human_claimed=record["human_claimed"],
                                   human_guid=self.ports.human_guid)
                else:
                    from leeroy_mind.decisions.loot_rules import award_with, rule_for_raid, rule_from_value
                    # A drop recorded under the guild's rule is awarded under it, switch or no switch
                    rule = (self._loot_rule() or rule_for_raid(self.ports.conn, self.ctx.raid_id)
                            or rule_from_value({"mode": record["mode"]}))
                    rows = {b["guid"]: b for b in record["bidders"]}
                    result = award_with(rule, record["price"], bidders,
                                        human_claimed=record["human_claimed"], human_guid=self.ports.human_guid,
                                        slot=record.get("slot", "other"),
                                        roles={g: b.get("role") for g, b in rows.items()},
                                        shares={g: b.get("share", 0.0) for g, b in rows.items()})
                with self.ports.conn:
                    current = _get(self.ports.conn, key)
                    if current["state"] != "pending":
                        continue
                    if result is None:
                        current["state"] = "unclaimed"
                    else:
                        # The intended award rides with the give, so an unknown outcome can be sent
                        # again; `award` is written only by a matching ack (the console's winner)
                        current.update(state="sending", intent=asdict(result), attempt=0)
                    _put(self.ports.conn, key, current)
                if result is None:
                    await self.ports.say(self.ctx.plan.loot_guid, "raid", template("loot_unclaimed", item=record["name"]))
                    continue
                if await self._give(key, current, result, 0):
                    return

    async def _give(self, key, record, result, attempt) -> bool:
        """Send one hand-out and settle its receipt; True ends this tick (the transport is in
        trouble). Only a matching ack charges. An unknown outcome is sent again once (MAX_ATTEMPT):
        native's loot slot is the guard, so a give that went through cannot go twice."""
        command = GiveLoot(id=command_id(self.ctx.raid_id, key, attempt), map=record["map"],
                           instance_id=record["instance_id"], source_entry=record["source_entry"],
                           index=record["index"], item_id=record["item_id"], to_guid=result.guid)
        try:
            ack = await self.ports.send(command)
        except asyncio.CancelledError:
            # Cancellation can hide a successful remote give: unknown, and no line while cancelling
            self._unknown(key, record, command, attempt, "cancelled")
            raise
        except (NotConnected, TransportUnavailable):
            # Refused before it was written: it never left the mind, so it goes again as it was
            with self.ports.conn:
                if attempt:
                    record.update(state="uncertain", attempt=attempt - 1)
                else:
                    record["state"] = "pending"
                    record.pop("intent", None)
                    record.pop("attempt", None)
                _put(self.ports.conn, key, record)
            log.info("guild_loot_give_unsent raid=%d item=%d command=%s", self.ctx.raid_id, record["item_id"],
                     command.id)
            return True
        except Exception as error:
            self._unknown(key, record, command, attempt, type(error).__name__)
            await self._tell_unknown(record, result, attempt)
            return True
        if ack.id != command.id:
            self._unknown(key, record, command, attempt, "wrong_ack")
            await self._tell_unknown(record, result, attempt)
            return True
        if ack.ok is True:
            record.update(state="acknowledged", award=asdict(result), attempt=attempt)
            record.pop("retry_at", None)
            with self.ports.conn:
                _put(self.ports.conn, key, record)
            await self._finish(key, record)
            return False
        # After an unknown first give, an empty slot (or a corpse with nothing left to loot) is most
        # likely that give: nobody is charged
        gone = attempt > 0 and ack.error in {"no_item", "no_source"}
        record.update(state="gone" if gone else "failed", error=public_text(self.ports, ack.error)[:80])
        with self.ports.conn:
            _put(self.ports.conn, key, record)
        log.info("guild_loot_give_refused raid=%d item=%d command=%s attempt=%d gone=%s", self.ctx.raid_id,
                 record["item_id"], command.id, attempt, gone)
        line = (template("loot_give_gone", item=record["name"]) if gone
                else template("loot_give_failed", item=record["name"], error=record["error"]))
        await self.ports.say(self.ctx.plan.loot_guid, "raid", line)
        return False

    def _unknown(self, key, record, command, attempt, code):
        record.update(state="uncertain", attempt=attempt, error=code,
                      retry_at=self.ports.clock.now() + RETRY_AFTER_S)
        with self.ports.conn:
            _put(self.ports.conn, key, record)
        # Ids and a code only: no item or member text
        log.warning("guild_loot_give_unknown raid=%d item=%d to=%d command=%s attempt=%d code=%s",
                    self.ctx.raid_id, record["item_id"], command.to_guid, command.id, attempt, code)

    async def _tell_unknown(self, record, result, attempt):
        if attempt < MAX_ATTEMPT:
            line = template("loot_give_unknown", item=record["name"])
        else:
            line = template("loot_give_lost", item=record["name"], winner=member_name(self.ports, result.guid))
        await self.ports.say(self.ctx.plan.loot_guid, "raid", line)

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
