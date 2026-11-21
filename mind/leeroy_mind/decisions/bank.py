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
    day = local_at(now).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    return conn.execute("SELECT count(*) FROM realm_direct WHERE owner='decision' AND kind='bank_withdraw' "
                        "AND state IN ('prepared','dispatched','acked','unknown') AND coalesce(dispatched_ms, "
                        "prepared_ms)>=?", (int(day * 1000),)).fetchone()[0]


def requests_due(conn, bank, cast, personas, *, now):
    if not bank:
        return []
    day = local_at(now).date()
    tomorrow = day + timedelta(days=1)
    raid_guids = set()
    for event in raid_events(conn, since=now, until=now + 49 * 3600):
        if local_at(event["at"]).date() == tomorrow:
            row = ledger.live(conn, "lineup", f"raid:{event['id']}")
            if row and row["status"] in ("settled", "in_effect") and row["message"]:
                raid_guids.update(json.loads(row["value"])["members"])
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
        role = role_of(m.class_, m.spec_tab, persona.play.raid_role)
        equipment = bank.get("equipped", {}).get(guid)
        for _, item, _ in slots(bank, 3):
            info = bank.get("items", {}).get(item)
            if info and equipment is not None:
                gain = upgrade_gain(info, m.class_, stat_family(m.class_, m.spec_tab, role), equipment, role=role)
                if gain and gain > 0:
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


async def read(app, *, details=False):
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
            boes = {item for _, item, _ in slots(bank, 3)}
            if boes:
                bank.update(items=armory.items(boes), equipped=armory.equipped(cast))
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
        self.checks[cmd.id] = check
        try:
            await realm.ports.send(cmd)
        finally:
            self.checks.pop(cmd.id, None)
            direct.abandon_unsent(cmd)

    def valid(self, app, bank):
        return (ready(app) and app.realm.action_context().guild == bank["guild"]
                and 0 <= app.clock.now() - bank["read_at"] <= FRESH_S)

    async def tick(self, app, now):
        if self.features is None or not officer(app):
            return
        conn = app.store.conn
        rows = conn.execute("SELECT * FROM guild_decision WHERE kind='bank' AND status='settled' ORDER BY id").fetchall()
        if rows:
            for row in rows:
                if await self.advance(app, row):
                    break  # one new wire admission per tick
            return
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
        if withdrawal and withdrawal[0] == "acked" and withdrawal[2] == req["count"] and not store.get_meta(receipt):
            with store.transaction():
                rid = BankLedger(store, app.clock).record("out", "request", req["guid"], req["item"], req["count"])
                store.set_meta(receipt, str(rid))
                store.conn.execute("UPDATE guild_decision SET effect_ref=? WHERE id=?",
                                   (json.dumps({"refs": refs, "realm_bank": rid}), ident))
        if step == "done":
            ledger.take_effect(store, ident, effect_ref={"refs": refs, "realm_bank": int(store.get_meta(receipt))}, now=now)
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
            if step == "withdraw":
                bank = await read(app)
                if not bank or not actors() or not self.valid(app, bank):
                    return False
                cap = app.decisions.config.bank_items_per_day
                valid, reason = approve(dict(req, human=app.human_guid, member=True), bank,
                                        given_today=given_today(store.conn, app.clock.now()), cap=cap)
                if not valid:
                    ledger.fail(store, ident, reason=reason, now=now)
                    return False
                cmd = protocol.BankWithdraw(uuid4().hex, by, req["item"], req["count"], req["tab"], req["guid"])
                await self.send(app, cmd, refs[0], lambda: actors() and self.valid(app, bank))
            else:
                cmd = protocol.MailSend(uuid4().hex, by, "Guild bank request",
                                        items=(protocol.MailItem(req["item"], req["count"], "bags"),), to_guid=req["guid"])
                await self.send(app, cmd, refs[1], actors)
            return True
        return False


def record_post(app, seed, speaker, message, now):
    """Same transaction as the officer's published words and exact factual block."""
    from leeroy_mind.server.seeds import mark_used
    if speaker != officer(app):
        raise ValueError("bank_officer_changed")
    if seed.kind == "bank_request":
        ident = int(seed.key.split(":")[1])
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
