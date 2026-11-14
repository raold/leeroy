"""Loot rules by officer poll (D4 Part B): DKP, a loot council or gear priority, each optionally
with role priorities on the slots fought over most ("tanks first on trinkets").

An officer opens the poll in #officer-chat no sooner than [decisions] loot_poll_days after the
last one, or after EARLY_FLOOR_DAYS when a close award or his claim made drama since. It runs a day,
or until every officer has voted; members vote their code-built positions (positions.py). The
winning rule is announced in #loot-and-dkp and takes effect once no raid is open, so a poll that
closes mid-raid changes nothing until the next raid. Each raid runs under the rule in effect when
it opened (fixed in meta at its start), and the loot system applies it (guild/loot.py). His claim
always wins, charged at that rule's price; its top losing bidder remembers it, and the guild talks
about it (the his_loot seed). His undo restores the rule it replaced, from the next raid.

Off (decisions disabled), nothing here runs and the loot system is today's DKP exactly. Logs carry
decision and poll ids, modes and counts only."""
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
import json
import logging
import math
from types import MappingProxyType

from leeroy_mind.guild.dkp import Award, award, bid
from leeroy_mind.guild.roles import is_loot_officer, role_of
from . import ledger, polls
from .positions import loot_rule_positions, member_facts

log = logging.getLogger(__name__)

MODES = ("dkp", "loot_council", "gear_priority")
SLOTS = ("weapon_2h", "weapon_1h", "trinket", "token", "other")   # = [dkp] prices keys
ROLES = ("tank", "healer", "melee", "ranged")
SUBJECT = "loot"
POLL_CHANNEL = "officer-chat"
RULE_CHANNEL = "loot-and-dkp"
POLL_S = 24 * 3600.0
DAY_S = 86400.0
DRAMA_DAYS = 7
EARLY_FLOOR_DAYS = 3      # an early poll still waits this long after the last one
RECEIPT_RAIDS = 4         # the role_first proposal reads the last raids' receipts
CONTESTED_SLOTS = 2       # role priorities for at most this many slots
RAID_KEY = "decisions:loot_rule:raid:{raid_id}"
PROPOSAL_KEY = "decisions:loot_poll:{poll}"
MODE_WORDS = {"dkp": "DKP", "loot_council": "a loot council", "gear_priority": "gear priority"}
SLOT_WORDS = {"weapon_2h": "two-handers", "weapon_1h": "one-handers", "trinket": "trinkets",
              "token": "tier tokens", "other": "other gear"}
ROLE_WORDS = {"tank": "tanks", "healer": "healers", "melee": "melee", "ranged": "ranged"}
_EMPTY = MappingProxyType({})


@dataclass(frozen=True, slots=True)
class LootRule:
    mode: str = "dkp"
    prices: Mapping[str, float] | None = None              # overrides [dkp] prices while in effect
    role_first: Mapping[str, tuple[str, ...]] = field(default_factory=lambda: _EMPTY)  # slot -> roles
    decision: int = 0                                      # the decision it comes from; 0: [dkp] alone

    def as_dict(self) -> dict:
        return {"mode": self.mode, "prices": dict(self.prices) if self.prices is not None else None,
                "role_first": {s: list(r) for s, r in self.role_first.items()}, "decision": self.decision}


def rule_from_value(value, decision: int = 0) -> LootRule:
    """A rule from a decision's stored value; anything malformed reads as its default."""
    if not isinstance(value, Mapping):
        return LootRule(decision=decision)
    mode = value.get("mode") if value.get("mode") in MODES else "dkp"
    prices = value.get("prices")
    if isinstance(prices, Mapping) and prices and all(
            s in SLOTS and type(p) in (int, float) and math.isfinite(p) and 0 <= p <= 10000
            for s, p in prices.items()):
        prices = MappingProxyType({s: float(p) for s, p in prices.items()})
    else:
        prices = None
    first = {}
    raw = value.get("role_first")
    for slot, roles in (raw.items() if isinstance(raw, Mapping) else ()):
        if (slot in SLOTS and isinstance(roles, (list, tuple)) and roles
                and all(r in ROLES for r in roles) and len(set(roles)) == len(roles)):
            first[slot] = tuple(roles)
    return LootRule(mode, prices, MappingProxyType(first), decision)


def current_rule(conn) -> LootRule:
    """The loot rule in effect, else LootRule() ([dkp] as configured)."""
    row = ledger.current(conn, "loot_rule", SUBJECT)
    if row is None:
        return LootRule()
    try:
        return rule_from_value(json.loads(row["value"]), row["id"])
    except (TypeError, ValueError):
        return LootRule(decision=row["id"])


def rule_for_raid(conn, raid_id: int) -> LootRule | None:
    """The rule this raid runs under, fixed when it opened; None before that."""
    row = conn.execute("SELECT value FROM meta WHERE key=?", (RAID_KEY.format(raid_id=raid_id),)).fetchone()
    if row is None:
        return None
    try:
        value = json.loads(row[0])
    except (TypeError, ValueError):
        return LootRule()
    return rule_from_value(value, value.get("decision", 0) if isinstance(value, dict)
                           and type(value.get("decision")) is int else 0)


def snapshot(conn, raid_id: int) -> LootRule:
    """Fix the rule in effect now as this raid's, once (the caller owns the transaction)."""
    conn.execute("INSERT OR IGNORE INTO meta(key,value) VALUES (?,?)",
                 (RAID_KEY.format(raid_id=raid_id), json.dumps(current_rule(conn).as_dict(), sort_keys=True)))
    return rule_for_raid(conn, raid_id)


def price_of(rule: LootRule, category: str, prices: Mapping[str, float]):
    """An item's price under the rule: its prices while it sets one for the slot, else [dkp]'s."""
    if rule.prices is not None and category in rule.prices:
        return rule.prices[category]
    return prices[category]


def loot_role(cls: int, spec_tab: int, raid_role) -> str:
    """tank, healer, melee or ranged, by the guild's own role rule (roles.role_of)."""
    role = role_of(cls, spec_tab, raid_role)
    if role != "damage":
        return role
    return "melee" if cls in (1, 2, 4, 6) or cls in (7, 11) and spec_tab == 1 else "ranged"


def _rank(rule: LootRule, slot: str, role) -> int:
    order = rule.role_first.get(slot, ())
    return order.index(role) if role in order else len(order)


def _council_weight(rule: LootRule, slot: str, role) -> float:
    order = rule.role_first.get(slot, ())
    return 1.0 + 0.25 * (len(order) - order.index(role)) if role in order else 1.0


def award_with(rule: LootRule, price, bidders, *, human_claimed: bool, human_guid: int, slot: str = "other",
               roles: Mapping[int, str] | None = None, shares: Mapping[int, float] | None = None) -> Award | None:
    """Who gets an item under the rule. dkp is today's award. gear_priority ranks by the slot's role
    priority, then the biggest upgrade, then today's order, charged as DKP. loot_council gives it to
    the highest gain x (0.5 + 30-day attendance share) x role priority, for no DKP. His claim always
    wins, at the rule's price (none under a council)."""
    roles, shares = roles or {}, shares or {}
    if rule.mode == "dkp":
        return award(price, bidders, human_claimed=human_claimed, human_guid=human_guid)
    ranked, seen = [], set()
    for person in bidders:
        if person.guid in seen:
            raise ValueError("duplicate bidder guid")
        seen.add(person.guid)
        if person.guid == human_guid or person.gain is None and not person.wanted:
            continue
        gain = person.gain if person.gain is not None else 0
        role = roles.get(person.guid)
        if rule.mode == "loot_council":
            share = shares.get(person.guid) or 0.0
            score = round(max(gain, 0) * (0.5 + share) * _council_weight(rule, slot, role), 6)
            ranked.append(((-score, 0 if person.wanted else 1, -share, person.guid), person.guid, score))
        else:
            priority = (0 if person.wanted else 1) if person.gain is not None else 2
            amount = bid(person.balance, price, person.gain, person.wanted)
            ranked.append(((_rank(rule, slot, role), -gain, priority, -amount, -person.balance, person.guid),
                           person.guid, amount))
    ranked.sort()
    council = rule.mode == "loot_council"
    if human_claimed:
        runner = ranked[0] if ranked else None
        paid = 0.0 if council else float(price)
        return Award(human_guid, paid, paid, -1, runner[1] if runner else None,
                     None if council or runner is None else runner[2], False)
    if not ranked:
        return None
    best, runner = ranked[0], ranked[1] if len(ranked) > 1 else None
    if council:
        return Award(best[1], 0.0, 0.0, 0, runner[1] if runner else None, None, False)
    amount = best[2]
    same = runner is not None and runner[0][:3] == best[0][:3]
    paid = min(amount, max(min(price, amount), runner[2] + 1 if same else 0))
    close = bool(same and runner[2] >= 0.9 * amount and amount > 0)
    return Award(best[1], float(paid), amount, best[0][2], runner[1] if runner else None,
                 runner[2] if runner else None, close)


def shares_of(conn, guids, *, now) -> dict[int, float]:
    """Each member's share of the finished raids of the last 30 days she raided (0 without any)."""
    return {g: f.share or 0.0 for g, f in member_facts(conn, guids, now=now).items()}


# -- the poll ----------------------------------------------------------------------------------

def _records(conn, keys):
    out = {}
    for key, value in conn.execute("SELECT key,value FROM meta WHERE key IN (SELECT value FROM json_each(?))",
                                   (json.dumps(sorted(set(keys))),)):
        try:
            record = json.loads(value)
        except (TypeError, ValueError):
            continue
        if isinstance(record, dict):
            out[key] = record
    return out


def _interested(bidders) -> list[dict]:
    return [b for b in bidders if isinstance(b, dict) and type(b.get("guid")) is int
            and (b.get("gain") is not None or b.get("wanted") is True)]


def drama_since(conn, since: float, human: int) -> int:
    """Charged awards since `since` that made drama: a close award, or his claim over a member who
    wanted the item."""
    rows = conn.execute("SELECT note FROM dkp_ledger WHERE reason='item' AND ts>=? AND "
                        "(note GLOB 'guild:loot:*' OR note GLOB 'council:guild:loot:*')", (since,)).fetchall()
    found = 0
    for record in _records(conn, [r[0].removeprefix("council:") for r in rows]).values():
        award_ = record.get("award")
        if record.get("state") != "charged" or not isinstance(award_, dict):
            continue
        if award_.get("close") is True or (record.get("human_claimed") is True and award_.get("guid") == human
                                           and type(award_.get("runner_up")) is int):
            found += 1
    return found


def role_first_proposal(conn, cast, personas) -> dict[str, list[str]]:
    """The role priorities a poll proposes: for the slots fought over most in the last raids'
    receipts (two or more members interested), the roles in order of the upgrade they sought."""
    raids = [r[0] for r in conn.execute("SELECT raid_id FROM guild_raid WHERE ended IS NOT NULL "
                                        "ORDER BY ended DESC,raid_id DESC LIMIT ?", (RECEIPT_RAIDS,))]
    contested, weight = {}, {}
    for raid in raids:
        for key, value in conn.execute("SELECT key,value FROM meta WHERE key GLOB ?", (f"guild:loot:{raid}:*",)):
            try:
                record = json.loads(value)
            except (TypeError, ValueError):
                continue
            if not isinstance(record, dict) or record.get("slot") not in SLOTS:
                continue
            interested = _interested(record.get("bidders") or [])
            if len(interested) < 2:
                continue
            slot = record["slot"]
            contested[slot] = contested.get(slot, 0) + 1
            for b in interested:
                role = b.get("role") if b.get("role") in ROLES else None
                member, persona = cast.get(b["guid"]), personas.get(b["guid"])
                if role is None and member is not None and persona is not None:
                    role = loot_role(member.class_, member.spec_tab, persona.play.raid_role)
                if role is not None:
                    gain = b.get("gain") if type(b.get("gain")) is int else 0
                    weight[slot, role] = weight.get((slot, role), 0) + max(gain, 0) + (5 if b.get("wanted") else 0) + 1
    top = sorted(contested, key=lambda s: (-contested[s], SLOTS.index(s)))[:CONTESTED_SLOTS]
    out = {}
    for slot in top:
        roles = sorted((r for r in ROLES if weight.get((slot, r), 0) > 0),
                       key=lambda r: (-weight[slot, r], ROLES.index(r)))
        if roles:
            out[slot] = roles
    return out


def proposal_words(first: Mapping[str, Sequence[str]]) -> str:
    return "; ".join(f"{ROLE_WORDS[roles[0]]} first on {SLOT_WORDS[slot]}" for slot, roles in first.items() if roles)


def rule_words(value: Mapping) -> str:
    rule = rule_from_value(value)
    words = MODE_WORDS[rule.mode]
    if rule.mode != "dkp" and rule.role_first:
        words += ", " + proposal_words(rule.role_first)
    return words


def last_poll(conn):
    return conn.execute("SELECT * FROM guild_poll WHERE kind='loot_rule' ORDER BY opened DESC,id DESC LIMIT 1").fetchone()


def poll_due(conn, *, now: float, poll_days: int, human: int) -> bool:
    """No sooner than poll_days after the last loot poll, or EARLY_FLOOR_DAYS after it when an
    award made drama since (in the last DRAMA_DAYS); never beside an open poll or a rule waiting to
    take effect, and only once the guild has raided in the last 30 days."""
    if conn.execute("SELECT 1 FROM guild_poll WHERE kind='loot_rule' AND status='open'").fetchone():
        return False
    if ledger.pending(conn, "loot_rule", SUBJECT) is not None:
        return False
    if not conn.execute("SELECT 1 FROM guild_raid WHERE ended IS NOT NULL AND ended>=?",
                        (now - 30 * DAY_S,)).fetchone():
        return False
    last = last_poll(conn)
    if last is None or now - last["opened"] >= poll_days * DAY_S:
        return True
    return (now - last["opened"] >= EARLY_FLOOR_DAYS * DAY_S
            and drama_since(conn, max(last["opened"], now - DRAMA_DAYS * DAY_S), human) > 0)


def raid_holds(app, now: float) -> bool:
    """A raid is open (his, or an offline raid) or forming, or his raid is in a fight, its wipe
    silence or debrief: no loot rule takes effect now."""
    if app.store.conn.execute("SELECT 1 FROM guild_raid WHERE ended IS NULL LIMIT 1").fetchone():
        return True
    guild = getattr(app, "guild", None)
    if guild is not None and (guild.raid_night.active or guild.offline.active):
        return True
    engine = getattr(app, "engine", None)
    raid = getattr(engine, "raid", None)
    if raid is not None:
        from leeroy_mind.talk.budget import _HOLD
        if raid.phase(now) in _HOLD or engine._in_boss_fight(now):
            return True
    return False


class LootRules:
    """The loot_rule kind: its poll, the decision the poll settles, and when that takes effect.
    Reversible while in effect: undoing it puts the rule it replaced back (the ledger's restore),
    which every raid opened after reads."""
    kind = "loot_rule"

    def reversible(self, row) -> bool:
        return row["status"] == "in_effect"

    async def undo(self, app, row) -> None:
        """Nothing in game holds a rule: a raid reads the rule in effect when it opens."""
        log.info("loot_rule_undone id=%d", row["id"])

    async def tick(self, app, now: float) -> None:
        config = getattr(app.decisions, "config", None)
        days = getattr(config, "loot_poll_days", 14)
        self._close_when_all_voted(app, now)
        from .lineup import server_quiet
        if not server_quiet(app) and poll_due(app.store.conn, now=now, poll_days=days, human=app.human_guid):
            self._open(app, now)
        self._take_effect(app, now)

    def _officers(self, app):
        cast = dict(app.world.discord_members())
        return {g: p for g in cast if g != app.human_guid and app.world.is_officer(g)
                and (p := app.personas.get(g)) is not None}

    def _open(self, app, now):
        store = app.store
        officers = self._officers(app)
        if len(officers) < 2:
            return
        opener = min(officers, key=lambda g: (not is_loot_officer(officers[g]), g))
        cast = dict(store.cast())
        first = role_first_proposal(store.conn, cast, {g: app.personas.get(g) for g in cast})
        question = "Loot rules from the next raid: keep DKP, a loot council, or gear priority?"
        if first:
            question += f" Gear priority or a council would put {proposal_words(first)}."
        question = question[:200]
        positions = loot_rule_positions(store.conn, set(officers), officers, now=now)
        from leeroy_mind.discord import store as dstore
        with store.transaction():
            poll = polls.open_poll(store, channel=POLL_CHANNEL, question=question, options=MODES,
                                   voters=sorted(officers), closes=now + POLL_S, now=now, kind="loot_rule",
                                   subject=SUBJECT, positions=positions)
            store.set_meta(PROPOSAL_KEY.format(poll=poll),
                           json.dumps({"role_first": first, "by": opener}, sort_keys=True))
            message = dstore.post(store, channel=POLL_CHANNEL, author=opener, origin="mind",
                                  text=f"Poll: {question}\nVotes close in 24 hours.", now=now)
            polls.attach_message(store, poll, message)
        log.info("loot_rule_poll_opened poll=%d officers=%d slots=%d", poll, len(officers), len(first))

    def _close_when_all_voted(self, app, now):
        conn = app.store.conn
        for row in conn.execute("SELECT * FROM guild_poll WHERE kind='loot_rule' AND status='open'").fetchall():
            officers = polls.voters_of(row) - {app.human_guid}
            voted = {r[0] for r in conn.execute("SELECT member FROM guild_poll_vote WHERE poll=?", (row["id"],))}
            if officers and officers <= voted:
                polls.close_early(app.store, row["id"], now=now)

    async def closed(self, app, poll, winner) -> None:
        """The poll's result: a tie or no votes keeps the rule in force; a winner is announced in
        #loot-and-dkp and waits, settled, for the next raid."""
        store, now = app.store, app.clock.now()
        if winner not in MODES:
            log.info("loot_rule_poll_no_result poll=%d", poll["id"])
            return
        try:
            saved = json.loads(store.get_meta(PROPOSAL_KEY.format(poll=poll["id"])) or "{}")
        except (TypeError, ValueError):
            saved = {}
        first = saved.get("role_first") if isinstance(saved, dict) else None
        value = {"mode": winner}
        if winner != "dkp" and isinstance(first, dict) and first:
            checked = rule_from_value({"mode": winner, "role_first": first}).role_first
            if checked:
                value["role_first"] = {s: list(r) for s, r in checked.items()}
        author = saved.get("by") if isinstance(saved, dict) and type(saved.get("by")) is int else None
        officers = self._officers(app)
        if author not in officers:
            author = min(officers, key=lambda g: (not is_loot_officer(officers[g]), g)) if officers else None
        from leeroy_mind.discord import store as dstore
        with store.transaction():
            message = None
            if author is not None:
                message = dstore.post(store, channel=RULE_CHANNEL, author=author, origin="mind", now=now,
                                      text=f"Loot rules from the next raid: {rule_words(value)}. "
                                           "Settled by the officers' poll.")
            ident = ledger.propose(store, kind="loot_rule", subject=SUBJECT, value=value,
                                   positions=polls.positions_of(poll), now=now, poll=poll["id"])
            ledger.settle(store, ident, settled_by=0, poll=poll["id"], message=message, now=now)
            polls.mark_handled(store, poll["id"], now=now)
        log.info("loot_rule_settled id=%d poll=%d mode=%s", ident, poll["id"], winner)

    def _take_effect(self, app, now):
        row = ledger.pending(app.store.conn, "loot_rule", SUBJECT)
        if row is None or row["status"] != "settled" or raid_holds(app, now):
            return
        rule = rule_from_value(json.loads(row["value"]))
        effect = {"rule": rule.mode}
        if rule.role_first:
            effect["role_first"] = {s: ">".join(r) for s, r in rule.role_first.items()}
        ledger.take_effect(app.store, row["id"], now=now, effect_ref=effect)


def loot_rules() -> LootRules:
    return LootRules()
