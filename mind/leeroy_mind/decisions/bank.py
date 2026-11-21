"""Members' item requests. Each handoff uses its own durable direct-ledger reference."""
import asyncio
from datetime import timedelta
import json
import logging
from uuid import uuid4

from leeroy_mind.clock import local_at
from leeroy_mind.discord import store as discord
from leeroy_mind.guild.roles import role_of, stat_family
from leeroy_mind.guild.upgrades import upgrade_gain
from leeroy_mind.realm import protocol
from leeroy_mind.realm.bank import BankLedger, STOCK
from leeroy_mind.realm.ports import _title_matches
from leeroy_mind.realm.steps import Steps
from . import ledger
from .lineup import raid_events, server_quiet
from .loot_rules import raid_holds

log = logging.getLogger(__name__)
REQUEST_KINDS = ("raid_flasks", "boe_upgrade", "enchant_mats")
FRESH_S = 60


def members(app):
    return {g: m for g, m in app.world.members.items() if g != app.human_guid and m.rank and m.level >= 80}


def officer(app):
    cast = members(app)
    for title in ("bank", "dkp"):
        holders = [g for g, m in cast.items() if m.rank == 1 and (p := app.personas.get(g))
                   and _title_matches(p.play.officer_title, title)]
        if holders:
            return min(holders)
    return None


def slots(bank, tab):
    return next((t["slots"] for t in bank["tabs"] if t["tab"] == tab), [])


def approve(request, bank, *, given_today: int, cap: int) -> tuple[bool, str]:
    if request["guid"] == request["human"]:
        return False, "him"
    if not request["member"]:
        return False, "non_member"
    if bank is None:
        return False, "no_fresh_bank"
    if given_today >= cap:
        return False, "cap"
    tab = slots(bank, request["tab"])
    item, count = request["item"], request["count"]
    stacks = [n for _, entry, n in sorted(tab) if entry == item]
    if count <= 0 or sum(stacks) < count:
        return False, "not_in_bank"
    if len(stacks) == len(tab) and sum(stacks) == count:
        return False, "empty_tab"
    if sum(stacks[:-1]) < count:
        return False, "last_stack"
    return True, "approved"


def given_today(conn, now):
    day = local_at(now).date()
    # At a DST boundary the offset at now can differ from the offset at midnight.
    # A backward clock step can put an already recorded debit later on this same date.
    # Bound both sides of the scan, then classify each instant using its own local date.
    rows = conn.execute("SELECT coalesce(dispatched_ms,prepared_ms) FROM realm_direct "
                        "WHERE owner='decision' AND kind='bank_withdraw' "
                        "AND state IN ('prepared','dispatched','acked','unknown') "
                        "AND coalesce(dispatched_ms,prepared_ms) BETWEEN ? AND ?",
                        (int((now-48*3600)*1000), int((now+48*3600)*1000)))
    return sum(local_at(ts/1000).date() == day for ts, in rows)


def raid_tomorrow(conn, now):
    tomorrow = local_at(now).date() + timedelta(days=1)
    raid_guids = set()
    for event in raid_events(conn, since=now, until=now + 49 * 3600):
        if local_at(event["at"]).date() == tomorrow:
            row = ledger.live(conn, "lineup", f"raid:{event['id']}")
            if row and row["status"] in ("settled", "in_effect") and row["message"]:
                raid_guids.update(json.loads(row["value"])["members"])
    return raid_guids


def boe_gain(bank, guid, item, member, persona):
    info = bank.get("items", {}).get(item)
    gear = bank.get("equipped", {}).get(guid)
    if not info or info.bonding != 2 or info.item_class not in (2, 4) or not info.inventory_type or gear is None:
        return None
    role = role_of(member.class_, member.spec_tab, persona.play.raid_role)
    return upgrade_gain(info, member.class_, stat_family(member.class_, member.spec_tab, role), gear, role=role)


def timely(req, proposed, now):
    day, opened = local_at(now).date(), local_at(proposed).date()
    return day == opened if req.get("kind") == "raid_flasks" else day.isocalendar()[:2] == opened.isocalendar()[:2]


def open_request(conn, req, proposed, now):
    return timely(req, proposed, now) and (req.get("kind") != "raid_flasks"
                                           or req.get("guid") in raid_tomorrow(conn, now))


def entitled(app, req, bank, proposed):
    guid = req["guid"]
    member = members(app).get(guid)
    persona = app.personas.get(guid)
    if not member or not persona or not timely(req, proposed, app.clock.now()):
        return False
    if req["kind"] == "raid_flasks":
        return guid in raid_tomorrow(app.store.conn, app.clock.now())
    if req["kind"] == "enchant_mats":
        return 333 in bank.get("professions", {}).get(guid, {})
    return req["kind"] == "boe_upgrade" and bool(boe_gain(bank, guid, req["item"], member, persona))


def requests_due(conn, bank, cast, personas, *, now):
    if not bank:
        return []
    day = local_at(now).date()
    raid_guids = raid_tomorrow(conn, now)
    out = []
    for guid, m in sorted(cast.items()):
        persona = personas.get(guid)
        if not persona:
            continue
        choices = []
        for kind, tab, stock, eligible in (
                ("raid_flasks", 0, STOCK[0][2:], guid in raid_guids),
                ("enchant_mats", 2, STOCK[2], 333 in bank.get("professions", {}).get(guid, {}))):
            if eligible:
                amounts = {item: sum(n for _, i, n in slots(bank, tab) if i == item) for item, _ in stock}
                item, count = max(stock, key=lambda pair: amounts[pair[0]] / pair[1])
                if amounts[item]:
                    choices.append((kind, tab, item, count, {}))
        for _, item, _ in slots(bank, 3):
            gain = boe_gain(bank, guid, item, m, persona)
            if gain:
                choices.append(("boe_upgrade", 3, item, 1, {"gain": gain}))
        for kind, tab, item, count, extra in choices:
            period = day.strftime("%G-W%V")
            subject = f"bank:{kind}:{guid}:{period}" + (f":{item}" if kind == "boe_upgrade" else "")
            if not conn.execute("SELECT 1 FROM guild_decision WHERE subject=?", (subject,)).fetchone():
                value = dict(subject=subject, guid=guid, kind=kind, tab=tab, item=item, count=count, **extra)
                if value not in out:
                    out.append(value)
    return out


def next_step(row, withdraw, mail):
    for step, state in (("withdraw", withdraw), ("mail", mail)):
        if state is None or state[0] == "abandoned":
            return step
        status, reason, value = state
        if status in ("unknown", "refused"):
            return "unknown" if status == "unknown" else "refused"
        if status != "acked" or value is None:
            return "wait"
        if step == "withdraw" and value != json.loads(row["value"])["count"]:
            return "unknown"
    return "done"


async def read(app, *, details=False, extra_items=()):
    guild = getattr(app, "guild", None)
    armory = getattr(guild, "armory", None)
    guild_id = int(app.store.get_meta("guild_id") or 0)
    if not guild_id or not armory or not getattr(armory, "available", True):
        return None
    started = app.clock.now()
    cast = sorted(members(app))
    def fetch():
        bank = armory.bank(guild_id)
        if details and cast:
            if hasattr(armory, "professions"):
                bank["professions"] = armory.professions(cast)
            boes = set(extra_items) | {item for _, item, _ in slots(bank, 3)}
            if boes:
                bank.update(items=armory.bank_items(boes), equipped=armory.equipped(cast))
        return bank
    try:
        bank = await asyncio.to_thread(fetch)
    except Exception as error:
        log.warning("decisions_bank_unread error=%s", type(error).__name__)
        return None
    if 0 <= app.clock.now() - started <= FRESH_S:
        return dict(bank, read_at=started, guild=guild_id)
    return None


def ready(app):
    realm = getattr(app, "realm", None)
    return bool(realm and Steps.ready(realm.action_context()) and not raid_holds(app, app.clock.now()))


def delivery(conn, row):
    """Operator-visible irreversible state at this instant, even before the next minute tick."""
    ident = row["id"]
    refs = (f"decision:{ident}:withdraw", f"decision:{ident}:mail")
    receipts = {r["ref"]: r for r in conn.execute(
        "SELECT ref,state,value FROM realm_direct WHERE owner='decision' AND ref IN (?,?) ORDER BY rowid", refs)}
    withdraw, mail = (receipts.get(ref) for ref in refs)
    if mail and mail["state"] in ("dispatched", "acked", "unknown"):
        if mail["state"] == "acked" and mail["value"] is not None:
            return True, f"Delivery confirmed by native mail #{mail['value']}. The posted mail cannot be recalled."
        return True, "Mail already entered transport. Delivery is unconfirmed and may still complete."
    if withdraw and withdraw["state"] in ("dispatched", "acked", "unknown"):
        if withdraw["state"] == "acked" and withdraw["value"] is not None:
            return True, "Withdrawal confirmed into the officer's bags. No mail delivery was admitted."
        return True, "Withdrawal entered transport and remains unconfirmed. No mail delivery was admitted."
    return False, "No bank transfer was admitted."


class BankRules:
    kind = "bank"

    def __init__(self):
        self.features = None
        self.checks = {}

    def on_hello(self, ev):
        self.features = frozenset(ev.features)

    def on_connection(self, connected):
        if not connected:
            self.features = None

    def reversible(self, row):
        return False

    async def undo(self, app, row):
        raise ValueError("bank_not_reversible")

    async def send(self, app, cmd, ref, check):
        if not ready(app) or not check():
            return
        realm = app.realm
        direct = realm.part("direct")
        if direct.prepare(cmd, owner="decision", ref=ref, context=realm.action_context(), expects_job=True) is None:
            return
        self.checks[cmd.id] = lambda: ready(app) and check()
        try:
            await realm.ports.send(cmd)
        finally:
            self.checks.pop(cmd.id, None)
            direct.abandon_unsent(cmd)

    def valid(self, app, bank):
        return (ready(app) and app.realm.action_context().guild == bank["guild"]
                and 0 <= app.clock.now() - bank["read_at"] <= FRESH_S)

    def reconcile(self, app):
        # A late completion still moved stock even if the owner overruled the request or the
        # officer left. Fold the native proof into both ledgers without admitting another command.
        store = app.store
        rows = store.conn.execute(
            "SELECT d.id,d.value,d.effect_ref,r.value AS moved FROM realm_direct r "
            "JOIN guild_decision d ON r.ref='decision:'||d.id||':withdraw' AND d.kind='bank' "
            "LEFT JOIN meta m ON m.key='decisions:bank:out:'||d.id "
            "WHERE r.owner='decision' AND r.kind='bank_withdraw' AND r.state='acked' "
            "AND r.value IS NOT NULL AND m.key IS NULL").fetchall()
        for row in rows:
            req, ident = json.loads(row["value"]), row["id"]
            if int(row["moved"]) != req["count"]:
                continue
            with store.transaction():
                rid = BankLedger(store, app.clock).record("out", "request", req["guid"], req["item"], req["count"])
                store.set_meta(f"decisions:bank:out:{ident}", str(rid))
                effect = json.loads(row["effect_ref"] or "{}")
                effect.update(refs=[f"decision:{ident}:withdraw"], realm_bank=rid)
                store.conn.execute("UPDATE guild_decision SET effect_ref=? WHERE id=?", (json.dumps(effect), ident))

        for result in store.conn.execute(
                "SELECT d.id,d.effect_ref,r.ref,r.value FROM realm_direct r JOIN guild_decision d "
                "ON r.ref='decision:'||d.id||':mail' WHERE d.kind='bank' AND r.owner='decision' AND r.kind='mail_send' "
                "AND r.state='acked' AND r.value IS NOT NULL AND json_extract(d.effect_ref,'$.mail_id') IS NULL").fetchall():
            with store.transaction():
                effect = json.loads(result["effect_ref"] or "{}")
                effect["mail_id"] = int(result["value"])
                effect["refs"] = list(dict.fromkeys([*effect.get("refs", []), result["ref"]]))
                store.conn.execute("UPDATE guild_decision SET effect_ref=? WHERE id=?", (json.dumps(effect), result["id"]))

    async def tick(self, app, now):
        self.reconcile(app)
        if self.features is None:
            return
        conn = app.store.conn
        by = officer(app)
        for pending in conn.execute("SELECT * FROM guild_decision WHERE kind='bank' AND status='proposed'").fetchall():
            req = json.loads(pending["value"])
            if not open_request(conn, req, pending["proposed"], now) or (getattr(app.world, "_cast_seen", False)
                    and (req.get("officer") != by or req.get("guid") not in members(app))):
                ledger.expire(app.store, pending["id"], now=now)
        if not by:
            return
        rows = conn.execute("SELECT * FROM guild_decision WHERE kind='bank' AND status='settled' ORDER BY id").fetchall()
        for row in rows:
            if await self.advance(app, row):
                return  # one new wire admission per tick; unresolved refs cannot starve discovery
        if server_quiet(app) or raid_holds(app, now):
            return
        bank = await read(app, details=True)
        by = officer(app)
        if not bank or not by:
            return
        cap = app.decisions.config.bank_items_per_day
        cast = members(app)
        for req in requests_due(conn, bank, cast, app.personas, now=now):
            if req["guid"] == by:
                continue
            from .positions import bank_positions
            personas = {g: app.personas.get(g) for g in cast if app.personas.get(g)}
            positions = bank_positions(conn, dict(req, at=now), [g for g, m in cast.items() if m.rank == 1], personas)
            req.update(officer=by, cap=cap)
            ledger.propose(app.store, kind="bank", subject=req["subject"], value=req, positions=positions, now=now)

    async def advance(self, app, row):
        store, ident = app.store, row["id"]
        if not ready(app):
            return False
        direct = app.realm.part("direct")
        by, req = row["settled_by"], json.loads(row["value"])
        now = app.clock.now()
        refs = [f"decision:{ident}:{part}" for part in ("withdraw", "mail")]
        withdrawal, mail = [direct.state("decision", ref) for ref in refs]
        step = next_step(row, withdrawal, mail)
        receipt = f"decisions:bank:out:{ident}"
        if step == "done":
            ledger.take_effect(store, ident, effect_ref={"refs": refs, "realm_bank": int(store.get_meta(receipt)),
                                                       "mail_id": mail[2]}, now=now)
        elif step in ("unknown", "refused"):
            ledger.fail(store, ident, reason=step, now=now)
        elif step in ("withdraw", "mail"):
            def actors():
                return ("member_bank_mail" in (self.features or ()) and officer(app) == by
                        and req["guid"] in members(app) and req["guid"] != by
                        and ledger.get(store.conn, ident)["status"] == "settled")
            if not actors():
                ledger.fail(store, ident, reason="member_or_officer_changed", now=now)
                return False
            extra = (req["item"],) if req["kind"] == "boe_upgrade" else ()
            bank = await read(app, details=req["kind"] != "raid_flasks", extra_items=extra)
            if not bank or not actors() or not self.valid(app, bank):
                return False
            if not entitled(app, req, bank, row["proposed"]):
                ledger.fail(store, ident, reason="entitlement_changed", now=now)
                return False
            def admitted():
                return actors() and self.valid(app, bank) and entitled(app, req, bank, row["proposed"])
            if step == "withdraw":
                cap = app.decisions.config.bank_items_per_day
                valid, reason = approve(dict(req, human=app.human_guid, member=True), bank,
                                        given_today=given_today(store.conn, app.clock.now()), cap=cap)
                if not valid:
                    ledger.fail(store, ident, reason=reason, now=now)
                    return False
                cmd = protocol.BankWithdraw(uuid4().hex, by, req["item"], req["count"], req["tab"], req["guid"])
                await self.send(app, cmd, refs[0], admitted)
            else:
                cmd = protocol.MailSend(uuid4().hex, by, "Guild bank request",
                                        items=(protocol.MailItem(req["item"], req["count"], "bags"),), to_guid=req["guid"])
                await self.send(app, cmd, refs[1], admitted)
            return True
        return False


def record_post(app, seed, speaker, message, now):
    """Same transaction as the officer's published words and exact factual block."""
    from leeroy_mind.server.seeds import mark_used
    if speaker != officer(app):
        raise ValueError("bank_officer_changed")
    if seed.kind == "bank_request":
        ident = int(seed.key.split(":")[1])
        row = ledger.get(app.store.conn, ident)
        req = json.loads(row["value"])
        if (not open_request(app.store.conn, req, row["proposed"], now) or req.get("officer") != speaker
                or req.get("guid") not in members(app)):
            raise ValueError("bank_request_stale")
        ledger.settle(app.store, ident, settled_by=speaker, message=message, now=now)
        rules = app.decisions.rules_for("bank")
        if "member_bank_mail" not in (rules.features or ()) or not getattr(getattr(app, "realm", None), "enabled", False):
            ledger.talk_only(app.store, ident, reason="bank_mail_off", now=now)
    else:
        key = "decisions:" + seed.key
        state = json.loads(app.store.get_meta(key))
        if state.get("message"):
            raise ValueError("restock_already_posted")
        state["message"] = message
        app.store.set_meta(key, json.dumps(state))
    mark_used(app.store, seed, now=now)


def bank_rules():
    return BankRules()
