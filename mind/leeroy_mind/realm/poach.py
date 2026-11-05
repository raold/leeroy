"""Poaching: at most one offer a week across the realm, always refused, never sent to the game.

Once a product week a progression officer whispers an offer to a member with a grievance: a
bench streak of two or more, a close loot loss within seven days, or (once Task 27's stances
exist) the losing side of a settled issue, a stance below -0.5. Before stances exist that
source is simply empty. The officer is an active actor of an active progression guild, never
anyone he kicked, a former member or one of his guild; the member is a current cast member,
never him or a former member.

The offer is a rule fact (poach_offer_v1) that is not public and known to that guild alone
('own'): the rules may read it, a prompt never sees it. Her memory of it goes through the
ports' writer, private with that officer:
"{officer} from {guild} whispered me an offer to join them. I said no." The answer is always
no, by rule, and nothing is said or sent in game. The week's offer and its memory are kept in
meta (realm:poach:week); the memory is keyed by the week, so a retry never writes it twice.
"""
from __future__ import annotations

import hashlib
import json
import logging
import random

from leeroy_mind.memory.writer import MemoryCandidate

from .calendar_time import product_period
from .ports import RealmDisabled
from .records import Rejected
from .rule_facts import put_rule_fact_in_unit

log = logging.getLogger(__name__)

WEEK_KEY = "realm:poach:week"
TICK_S = 600
MEMORY_RETRY_S = 60
LOSS_WINDOW_S = 7 * 86400.0
BENCH_STREAK = 2
STANCE_LOST = -0.5
OFFER = "{officer} from {guild} whispered me an offer to join them. I said no."
FACT = "{officer} from {guild} whispered {member} an offer to join them."
IMPORTANCE, VALENCE = 5, -0.1
_U32 = 0xFFFFFFFF


class Poach:
    """The poach part: the week's one refused offer and her private memory of it."""

    name = "poach"

    def __init__(self, realm, *, salt: str = "poach_v1"):
        self.realm = realm
        self._salt = salt
        self._next = 0.0
        self._next_memory = 0.0
        self._task = None

    @property
    def _store(self):
        return self.realm.app.store

    def _live(self) -> bool:
        """Enabled, with a valid hello that names him."""
        if self.realm.enabled is not True:
            return False
        ctx = self.realm.action_context()
        return (ctx.enabled is True and ctx.connected is True and ctx.presence_known is True
                and type(ctx.human) is int and ctx.human > 0)

    def _marker(self):
        try:
            value = json.loads(self._store.get_meta(WEEK_KEY) or "null")
        except ValueError:
            return None
        return value if type(value) is dict else None

    # -- who -----------------------------------------------------------------------------

    def officers(self) -> list[tuple[int, str, int, str]]:
        """(guid, name, guild, guild name) of each officer who may make the offer."""
        human = self.realm.ports.human().guid
        excluded = self.realm.ledger.actor_exclusions()
        conn = self._store.conn
        roster = {row[0] for row in conn.execute("SELECT guid FROM cast_member")}
        return [(row[0], row[1], row[2], row[3]) for row in conn.execute(
            "SELECT a.guid,a.name,g.id,g.name FROM realm_actor a JOIN realm_guild g ON g.id=a.guild "
            "WHERE a.role='officer' AND a.status='active' AND g.role='progression' AND g.status='active' "
            "ORDER BY a.guid")
            if type(row[0]) is int and 0 < row[0] <= _U32 and row[0] != human and row[0] not in roster
            and row[0] not in excluded and type(row[1]) is str and type(row[3]) is str]

    def _stance_losers(self) -> set[int]:
        """The losing side of a settled issue: empty until Task 27 writes stances (M32)."""
        return {row[0] for row in self._store.conn.execute(
            "SELECT s.guid FROM realm_stance s JOIN realm_issue i ON i.id=s.issue "
            "WHERE i.status='settled' AND s.value<?", (STANCE_LOST,))}

    def grievances(self, now) -> dict[int, tuple[str, tuple[str, ...]]]:
        """Each member with a grievance: guid -> (name, the grievances)."""
        ports = self.realm.ports
        human = ports.human().guid
        former = self.realm.ledger.former_members()
        stances = self._stance_losers()
        found = {}
        for member in ports.cast():
            guid = member.guid
            if guid == human or guid in former:
                continue
            reasons = tuple(reason for reason, held in (
                ("bench", ports.bench_streak(guid) >= BENCH_STREAK),
                ("loot", ports.close_loot_losses(guid, now - LOSS_WINDOW_S) >= 1),
                ("stance", guid in stances)) if held)
            if reasons:
                found[guid] = (member.name, reasons)
        return found

    # -- the week's offer ------------------------------------------------------------------

    def _rng(self, period) -> random.Random:
        seed = hashlib.sha256(f"{self._salt}:{period}".encode("utf-8")).digest()
        return random.Random(int.from_bytes(seed[:8], "big"))

    def weekly(self, now) -> dict | None:
        """This product week's one offer, when nobody has had one yet, there is an officer to
        make it and a member with a grievance: its record, or None. Nothing goes to the game."""
        if not self._live():
            return None
        period = product_period(now).key
        marker = self._marker()
        if marker is not None and marker.get("period") == period:
            return None
        officers = self.officers()
        targets = self.grievances(now) if officers else {}
        if not targets:
            return None
        rng = self._rng(period)
        officer, officer_name, guild, guild_name = rng.choice(officers)
        member = rng.choice(sorted(targets))
        member_name, reasons = targets[member]
        human = self.realm.ports.human()
        store = self._store
        with store.transaction():
            marker = self._marker()
            if marker is not None and marker.get("period") == period:
                return None
            fact = put_rule_fact_in_unit(
                store, policy="poach_offer_v1", key=period, kind="poach_offer",
                text=FACT.format(officer=officer_name, guild=guild_name, member=member_name),
                public=False, known_by=[(guild, "own")],
                inputs={"period": period, "guild": guild, "officer": officer, "member": member,
                        "grievance": list(reasons)},
                ts=now, human=human)
            offer = {"period": period, "guild": guild, "officer": officer, "member": member,
                     "grievance": list(reasons), "ts": now, "fact": None, "memory": None,
                     "text": OFFER.format(officer=officer_name, guild=guild_name)}
            if type(fact) is Rejected:
                log.warning("realm_poach_fact_refused reason=%s", fact.reason)
                offer.update(memory=0, refused=fact.reason)  # No offer to remember either.
            else:
                offer["fact"] = fact.id
            store.set_meta(WEEK_KEY, json.dumps(offer, sort_keys=True))
        log.info("realm_poach_offer guild=%d officer=%d member=%d", guild, officer, member)
        return offer

    async def remember_pending(self) -> int | None:
        """Write her memory of the week's offer once: the memory's id (0 when the writer
        refused it for good), or None when there is nothing to write or it must wait."""
        offer = self._marker()
        if offer is None or offer.get("memory") is not None:
            return None
        member, officer = offer.get("member"), offer.get("officer")
        if type(member) is not int or type(officer) is not int or type(offer.get("text")) is not str:
            return None
        candidate = MemoryCandidate(
            owner=member, kind="event", text=offer["text"], t_event=int(offer["ts"]), actors=(officer,),
            importance=IMPORTANCE, valence=VALENCE, source="game", evidence=(),
            key=f"realm:poach:{offer['period']}", scope="private", scope_with=officer)
        try:
            result = await self.realm.ports.remember(candidate)
        except RealmDisabled:
            return None
        memory = result.id if type(result.id) is int and result.id > 0 else 0
        current = self._marker()
        if current is None or current.get("period") != offer["period"]:
            return None
        current["memory"] = memory
        self._store.set_meta(WEEK_KEY, json.dumps(current, sort_keys=True))
        if not memory:
            log.warning("realm_poach_memory_refused action=%s", result.action)
        return memory

    # -- the Realm's loop -----------------------------------------------------------------

    async def tick(self) -> None:
        """The week's offer (checked every 600 s) and its memory from an owned child, while the
        Realm is live. Never the Realm loop's failure."""
        try:
            if not self._live():
                return
            now = self.realm.app.clock.now()
            if now >= self._next:
                self._next = now + TICK_S
                self.weekly(now)
            offer = self._marker()
            if (offer is not None and offer.get("memory") is None and now >= self._next_memory
                    and (self._task is None or self._task.done())):
                self._next_memory = now + MEMORY_RETRY_S
                self._task = self.realm.spawn(self.remember_pending)
        except Exception as error:
            log.warning("realm_poach_failed: %s", type(error).__name__)


def attach(realm):
    """The poach part, with no I/O until the Realm's own tick."""
    return Poach(realm), ()
