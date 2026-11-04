"""The realm's ledger reads: prompt facts, forum material, former members and the way back in.

Every read is synchronous over one actual Store unit and writes nothing. A
selection is only a plan: a later writer revalidates it inside its own final
unit. Nothing here has a caller that shows Realm content to a prompt yet.
"""
from __future__ import annotations

import json

from leeroy_mind.personas.schema import persona_from_dict
from leeroy_mind.storage.store import Store

from .calendar_time import local_write_day
from .fact_store import FactStore
from .provenance import (audit_realm_reads_in_unit, public_fact_in_unit,
                         stored_forum_candidate_in_unit)
from .records import BackIn, FactRow, ForumCandidate, Rejected

_U32 = (1 << 32) - 1
_SQLITE_MAX = (1 << 63) - 1
_FORUM_LIMIT = 20
_FORUM_MOTIVATIONS = ("social", "competitor")


def _require_unit(store):
    if not isinstance(store, Store) or not store.in_unit:
        raise RuntimeError("the realm ledger requires an active Store unit")


def former_members_in_unit(store, *, human_guid) -> frozenset[int]:
    """Every stored persona off the whole roster, plus every kick, never the human.

    The whole cast_member roster counts at any level: a member below 80 is not
    in the cast but has not left. An unreadable persona still counts as stored.
    """
    _require_unit(store)
    if type(human_guid) is not int or not 0 <= human_guid <= _U32:
        raise ValueError("invalid human_guid")
    conn = store.conn
    stored = {row[0] for row in conn.execute("SELECT guid FROM persona_core")}
    roster = {row[0] for row in conn.execute("SELECT guid FROM cast_member")}
    kicked = {row[0] for row in conn.execute("SELECT guid FROM realm_kick")}
    return frozenset(((stored - roster) | kicked) - {human_guid})


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

    def back_in(self, fact_id, *, place_zone=None, forum=False) -> tuple[BackIn, ...]:
        """Who would get one open memory of a public fact; a plan only, nothing is written.

        A forum post reaches current cast members who are social or competitive,
        never a former member, the human or anyone already holding today's notable.
        A channel audience needs each member's place at the line's time, which no
        receipt records yet, so it is unsupported and nobody is chosen.
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
            owners = [row[0] for row in store.conn.execute(
                "SELECT guid FROM cast_member WHERE level>=80 ORDER BY guid").fetchall()]
            return tuple(
                BackIn(fact.id, fact.revision, stamps[0].fact_sha256, guid, "forum",
                       fact.ts, fact.text, actors, proof)
                for guid in owners
                if guid != human.guid and guid not in former and guid not in capped
                and _forum_reader(store, guid))
