"""The realm's ledger: prompt facts, forum material, former members and the way back in.

Every read is synchronous over one actual Store unit and writes nothing. A
selection is only a plan: put_notable_in_unit revalidates it inside the final
unit that writes it. Nothing here has a caller that shows Realm content to a
prompt yet. A notable memory is an ordinary memory (owner ruling 2026-11-04).
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3

from leeroy_mind.human import HumanIdentity
from leeroy_mind.memory.writer import MemoryWriter
from leeroy_mind.personas.schema import persona_from_dict
from leeroy_mind.storage.rows import MemoryRow
from leeroy_mind.storage.store import Store

from .calendar_time import local_write_day
from .fact_store import FactStore
from .memory_lineage import RealmLineageError
from .ports import RealmDisabled
from .provenance import (audit_realm_reads_in_unit, public_fact_in_unit,
                         stored_forum_candidate_in_unit)
from .records import (BackIn, FactRow, ForumCandidate, PreparedBackIn, RealmFactReadStamp,
                      RealmInputs, Rejected)

log = logging.getLogger(__name__)

_U32 = (1 << 32) - 1
_SQLITE_MAX = (1 << 63) - 1
_FORUM_LIMIT = 20
_FORUM_MOTIVATIONS = ("social", "competitor")
NOTABLE_IMPORTANCE = 4


def _require_unit(store):
    if not isinstance(store, Store) or not store.in_unit:
        raise RuntimeError("the realm ledger requires an active Store unit")


def _human(human_guid):
    if type(human_guid) is not int or not 0 <= human_guid <= _U32:
        raise ValueError("invalid human_guid")
    return human_guid


def _kicked(conn) -> dict[int, float]:
    return {row[0]: row[1] for row in conn.execute("SELECT guid, ts FROM realm_kick")}


def _readmitted(conn, kicked, roster) -> set[int]:
    """Kicked members his yes took back: the latest kick episode was admitted by native, no
    kick was observed after it (one while the Realm was off has no episode) and they are on
    the roster again."""
    latest = {}
    for guid, kick_ms, state in conn.execute(
            "SELECT guid, kick_ms, admission_state FROM realm_kick_episode ORDER BY guid, kick_ms, rowid"):
        latest[guid] = (kick_ms, state)
    return {guid for guid, (kick_ms, state) in latest.items()
            if state == "admitted" and guid in roster and guid in kicked
            and round(kicked[guid] * 1000) <= kick_ms}


def former_members_in_unit(store, *, human_guid) -> frozenset[int]:
    """Every stored persona off the whole roster, plus every kick he did not take back, never
    the human.

    The whole cast_member roster counts at any level: a member below 80 is not
    in the cast but has not left. An unreadable persona still counts as stored. A member
    readmitted after his yes is a full member again (owner ruling 4, 2026-11-04), but never
    an actor or applicant: see actor_exclusions_in_unit.
    """
    _require_unit(store)
    human_guid = _human(human_guid)
    conn = store.conn
    stored = {row[0] for row in conn.execute("SELECT guid FROM persona_core")}
    roster = {row[0] for row in conn.execute("SELECT guid FROM cast_member")}
    kicked = _kicked(conn)
    readmitted = _readmitted(conn, kicked, roster)
    return frozenset(((stored - roster) | (set(kicked) - readmitted)) - {human_guid})


def actor_exclusions_in_unit(store, *, human_guid) -> frozenset[int]:
    """Who may never be a realm actor or applicant: every former member and every guid he
    ever kicked, taken back or not. Never the human, whom native refuses anyway."""
    _require_unit(store)
    human_guid = _human(human_guid)
    kicked = set(_kicked(store.conn))
    return frozenset((former_members_in_unit(store, human_guid=human_guid) | kicked) - {human_guid})


def _forum_reader(store, guid):
    """Whether the member's stored persona reads the forum: social or competitive."""
    text = store.get_persona_json(guid)
    if text is None:
        return False
    try:
        persona = persona_from_dict(json.loads(text))
    except ValueError:  # Unreadable or newer persona data is nobody's audience.
        return False
    game = persona.game
    return persona.guid == guid and (game.motivation in _FORUM_MOTIVATIONS
                                     or game.thinks_as == "social")


def _holders(store, fact_id):
    """Members who already hold a memory resting on the fact, at any remove."""
    return {row[0] for row in store.conn.execute(
        "SELECT DISTINCT m.owner FROM realm_memory_dependency d JOIN memory m ON m.id = d.memory "
        "WHERE d.fact=?", (fact_id,))}


def _forum_owner(store, guid, *, human_guid, former):
    """A current cast member at 80 who reads the forum: never the human or a former member."""
    return (guid != human_guid and guid not in former
            and store.conn.execute("SELECT 1 FROM cast_member WHERE guid=? AND level>=80",
                                   (guid,)).fetchone() is not None
            and _forum_reader(store, guid))


class _SlotTaken(Exception):
    pass


def put_notable_in_unit(store, writer, prepared, *, human, now) -> int | Rejected:
    """Write one planned notable memory and take its owner's slot for the local write day.

    The caller embeds first and holds the Store unit; nothing here awaits. The fact is read
    again and must be exactly the planned one, the owner must still be in the audience
    without the fact and without today's notable, and the memory goes through the writer.
    The memory and its realm_notable row commit together or not at all: a primary-key clash
    rolls both back. The caller owns the Realm switch (the live path is remember_back).
    Returns the memory id, or why nothing was written.
    """
    _require_unit(store)
    if type(prepared) is not PreparedBackIn or type(prepared.plan) is not BackIn:
        raise ValueError("invalid prepared notable")
    if not isinstance(writer, MemoryWriter) or writer.store is not store:
        raise ValueError("the notable writer must use this store")
    if type(human) is not HumanIdentity:
        raise ValueError("invalid human")
    day = local_write_day(now)
    plan = prepared.plan
    if plan.reason != "forum":
        return Rejected("unsupported_policy")  # A channel audience is not recorded.
    if prepared.embedded_text_sha256 != hashlib.sha256(plan.text.encode("utf-8")).hexdigest():
        return Rejected("invalid_input")  # Never store a vector made for another text.
    if type(human.guid) is not int or not 1 <= human.guid <= _U32:
        return Rejected("ineligible")  # Without his guid he could be the owner.
    read = public_fact_in_unit(store, plan.fact_id, human=human)
    if type(read) is Rejected:
        return read
    fact, proof = read
    if (fact.revision != plan.fact_revision or fact.text != plan.text or fact.ts != plan.source_at
            or proof != plan.proof
            or plan.actors != tuple(json.loads(proof.qualification_json)["participants"])):
        return Rejected("proof_changed")
    former = former_members_in_unit(store, human_guid=human.guid)
    if (not _forum_owner(store, plan.owner, human_guid=human.guid, former=former)
            or plan.owner in _holders(store, fact.id)):
        return Rejected("ineligible")
    if store.conn.execute("SELECT 1 FROM realm_notable WHERE day=? AND guid=?",
                          (day, plan.owner)).fetchone() is not None:
        return Rejected("capped")
    ref = f"realm:fact:{fact.id}"
    written = int(now)
    row = MemoryRow(
        id=None, owner=plan.owner, kind="event", text=plan.text, t_event=int(plan.source_at),
        t_created=written, t_accessed=written, access_n=0, actors=tuple(sorted(set(plan.actors))),
        place=None, boss_id=None, raid_id=None, importance=NOTABLE_IMPORTANCE, valence=0.0,
        source="game", evidence=(ref,), embedding=prepared.embedding)
    # The writer audits the fact once more against the plan's own stamp, then the memory it
    # wrote, inside this savepoint.
    inputs = RealmInputs((ref,), (), "memory", (RealmFactReadStamp(
        plan.fact_id, plan.fact_revision, plan.fact_sha256, plan.proof.sha256),))
    try:
        with store.transaction():
            memory = writer.put(row, realm_inputs=inputs)
            try:
                store.conn.execute("INSERT INTO realm_notable(day, guid, memory) VALUES (?,?,?)",
                                   (day, plan.owner, memory))
            except sqlite3.IntegrityError:
                raise _SlotTaken from None
    except _SlotTaken:
        return Rejected("capped")
    except RealmLineageError as error:
        return error.rejection
    return memory


class Ledger:
    def __init__(self, store, ports, facts: FactStore):
        if type(facts) is not FactStore or facts.store is not store:
            raise ValueError("the ledger's facts must use its own store")
        self._store = store
        self._ports = ports
        self._facts = facts

    @property
    def store(self):
        return self._store

    def known(self, guild) -> tuple[FactRow, ...]:
        """The five facts a guild's prompts may use: public, active origin, server-wide first."""
        return self._facts.known(guild, human=self._ports.human())

    def known_all(self, guild):
        """Every fact the guild knows, private ones included: for the rules, never a prompt."""
        return self._facts.known_all(guild)

    def forum_candidates(self, *, limit=_FORUM_LIMIT) -> tuple[ForumCandidate, ...]:
        """Live game-topic rumors with a complete open proof, the most recently told first.

        Invalid rumors are skipped before the limit counts, so they never hide valid ones.
        """
        if type(limit) is not int or not 1 <= limit <= _FORUM_LIMIT:
            raise ValueError("invalid limit")
        human = self._ports.human()
        result = []
        with self._store.transaction():
            cursor = self._store.conn.execute(
                "SELECT id FROM rumor WHERE status='live' ORDER BY last_told DESC, id DESC")
            try:
                for row in cursor:
                    candidate = stored_forum_candidate_in_unit(self._store, row[0], human=human)
                    if type(candidate) is ForumCandidate:
                        result.append(candidate)
                        if len(result) == limit:
                            break
            finally:
                cursor.close()
        return tuple(result)

    def former_members(self) -> frozenset[int]:
        human = self._ports.human().guid
        with self._store.transaction():
            return former_members_in_unit(self._store, human_guid=human)

    def actor_exclusions(self) -> frozenset[int]:
        human = self._ports.human().guid
        with self._store.transaction():
            return actor_exclusions_in_unit(self._store, human_guid=human)

    def back_in(self, fact_id, *, place_zone=None, forum=False) -> tuple[BackIn, ...]:
        """Who would get one open memory of a public fact; a plan only, nothing is written.

        A forum post reaches current cast members who are social or competitive,
        never a former member, the human, anyone already holding today's notable or
        anyone already holding a memory of this fact. A channel audience needs each
        member's place at the line's time, which no receipt records yet, so it is
        unsupported and nobody is chosen.
        """
        if type(fact_id) is not int or not 1 <= fact_id <= _SQLITE_MAX:
            raise ValueError("invalid fact_id")
        if type(forum) is not bool:
            raise ValueError("invalid forum")
        if place_zone is not None and (type(place_zone) is not int or not 0 <= place_zone <= _U32):
            raise ValueError("invalid place_zone")
        if forum and place_zone is not None:
            raise ValueError("a forum post has no place")
        if not forum:
            return ()
        human = self._ports.human()
        if type(human.guid) is not int or not 1 <= human.guid <= _U32:
            return ()  # Without his guid he could be in the audience.
        day = local_write_day(self._ports.now())
        store = self._store
        with store.transaction():
            read = public_fact_in_unit(store, fact_id, human=human)
            if type(read) is Rejected:
                return ()
            fact, proof = read
            audited = audit_realm_reads_in_unit(store, (f"realm:fact:{fact_id}",), human=human)
            if type(audited) is Rejected:
                return ()
            stamps = audited[1]
            if (len(stamps) != 1 or stamps[0].fact_id != fact.id or stamps[0].revision != fact.revision
                    or stamps[0].proof_sha256 != proof.sha256):
                return ()
            actors = tuple(json.loads(proof.qualification_json)["participants"])
            former = former_members_in_unit(store, human_guid=human.guid)
            capped = {row[0] for row in store.conn.execute(
                "SELECT guid FROM realm_notable WHERE day=?", (day,))}
            holders = _holders(store, fact.id)
            owners = [row[0] for row in store.conn.execute(
                "SELECT guid FROM cast_member WHERE level>=80 ORDER BY guid").fetchall()]
            return tuple(
                BackIn(fact.id, fact.revision, stamps[0].fact_sha256, guid, "forum",
                       fact.ts, fact.text, actors, proof)
                for guid in owners
                if guid != human.guid and guid not in former and guid not in capped
                and guid not in holders and _forum_reader(store, guid))

    async def remember_back(self, fact_id, *, place_zone=None, forum=False) -> tuple[int, ...]:
        """The live way back in: write back_in's plan, at most one notable a member a day.

        The text is embedded once, before the write; then one Store unit writes each
        member's memory with put_notable_in_unit, which revalidates everything the plan
        assumed. Nothing is embedded or written while the Realm is disabled, including a
        disable during the embed. Returns the ids of the memories written.
        """
        plans = self.back_in(fact_id, place_zone=place_zone, forum=forum)
        if not plans:
            return ()
        text = plans[0].text  # One fact: every plan carries its text.
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()

        def write(writer, embedding):
            try:
                items = tuple(PreparedBackIn(plan, embedding, digest) for plan in plans)
            except ValueError:  # A vector of another size: stored without one, embedded later.
                items = tuple(PreparedBackIn(plan, None, digest) for plan in plans)
            human, now = self._ports.human(), self._ports.now()
            written = []
            with self._store.transaction():
                for item in items:
                    result = put_notable_in_unit(self._store, writer, item, human=human, now=now)
                    if type(result) is Rejected:
                        log.debug("realm_notable_refused: fact=%d owner=%d reason=%s",
                                  item.plan.fact_id, item.plan.owner, result.reason)
                    else:
                        written.append(result)
            return tuple(written)

        try:
            return await self._ports.remember_notable(text, write)
        except RealmDisabled:
            return ()
