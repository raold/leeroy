"""Atomic qualified public Realm facts over actual Store transaction units.

This reducer is deliberately unattached: caller-supplied observations do not
establish the later service's current-writer admission barrier.
"""
from __future__ import annotations

from dataclasses import fields
import hashlib
import json

from leeroy_mind.protocol import BgEnd
from .protocol import ArenaEnd, ChannelChat, DuelEnd, PvpKill, RealmAchievement, RealmDungeonKill, RealmKill, Wg
from .provenance import (audit_public_in_unit, public_fact_draft,
                         public_fact_in_unit, public_proof_to_json, render_public,
                         validate_live_binding_in_unit)
from .records import FactRow, RealmObservation, Rejected
from .rule_facts import rule_fact_in_unit
from .source_store import claim_source_in_unit

_POLICIES = {
    BgEnd: ('bg_result_v1', 'bg_result', 'event'),
    ArenaEnd: ('arena_result_v1', 'arena_result', 'event'),
    Wg: ('wg_result_v1', 'wg_result', 'event'),
    PvpKill: ('pvp_result_v1', 'pvp_kill', 'event'),
    ChannelChat: ('channel_raid_topic_v1', 'channel_topic', 'channel'),
    RealmKill: ('verified_raid_kill_v1', 'kill', 'event'),
    RealmAchievement: ('realm_first_achievement_v1', 'realm_first', 'event'),
    DuelEnd: ('duel_result_v1', 'duel_result', 'event'),
    RealmDungeonKill: ('verified_dungeon_kill_v1', 'kill', 'event'),
}
# Each native guild kill keeps its own row, by kill id, beside its fact: raids in realm_kill
# (which announcements reference), 5-man kills in realm_dungeon_kill (never announced).
_KILL_TABLES = {'verified_raid_kill_v1': 'realm_kill', 'verified_dungeon_kill_v1': 'realm_dungeon_kill'}


def _reject(reason='proof_changed'):
    raise FactWriteRejected(Rejected(reason))


def _accepted(value):
    if type(value) is Rejected:
        raise FactWriteRejected(value)
    return value


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _qualification_hash(raw):
    return hashlib.sha256(b'realm-qualification-v1\0' + raw.encode('utf-8')).hexdigest()


def _projection(event):
    return {field.name: (list(getattr(event, field.name)) if field.name == 'cast_members'
                         else getattr(event, field.name)) for field in fields(type(event))}


def _kill_values(payload, source_id, fact_id, qualification):
    return (payload['kill_id'], source_id, payload['kill_id'].split(':', 1)[0],
            str(payload['native_epoch']), payload['guild_id'], payload['human_guid'],
            payload['map'], payload['instance_id'], payload['boss_id'], payload['difficulty'],
            payload['size'], int(payload['heroic']), payload['killed_at_ms'],
            payload['verified_at_ms'], payload['ts'], _json(payload['cast_members']),
            int(payload['human_present']), _json(json.loads(qualification)['qualified_cast']),
            _qualification_hash(qualification), fact_id)


_KILL_COLUMNS = ('kill_id,source,native_boot,native_epoch,guild,human,map,instance_id,boss_id,'
                 'difficulty,size,heroic,killed_ms,verified_ms,published_ms,native_cast_json,'
                 'human_present,qualified_cast_json,qualification_sha256,fact')


class FactWriteRejected(Exception):
    """A finite semantic rejection; must escape the complete write unit."""
    def __init__(self, rejection: Rejected):
        self.rejection = rejection
        super().__init__(rejection.reason)


class FactStore:
    def __init__(self, store):
        self._store = store

    @property
    def store(self):
        return self._store

    def _require_unit(self):
        if not self.store.in_unit:
            raise RuntimeError('FactStore requires an active Store unit')

    def record(self, observation: RealmObservation, *, human):
        if self.store.in_unit:
            raise RuntimeError('record requires an outer Store unit')
        try:
            with self.store.transaction():
                return self.put_fact_in_unit(observation, human=human)
        except FactWriteRejected as error:
            return error.rejection

    def put_fact_in_unit(self, observation: RealmObservation, *, human):
        self._require_unit()
        if type(observation) is not RealmObservation:
            _reject('invalid_input')
        choice = _POLICIES.get(type(observation.event))
        if choice is None:
            _reject('unsupported_policy')
        if choice[0] in _KILL_TABLES:
            return self._record_kill_in_unit(observation, human, choice)
        return self._claim_and_write(observation, human, choice)

    def record_verified_kill_in_unit(self, observation: RealmObservation, *, human):
        self._require_unit()
        if type(observation) is not RealmObservation or type(observation.event) is not RealmKill:
            _reject('invalid_input')
        return self._record_kill_in_unit(observation, human, _POLICIES[RealmKill])

    def _record_kill_in_unit(self, observation, human, choice):
        policy = choice[0]
        _accepted(validate_live_binding_in_unit(self.store, observation, human=human, policy=policy))
        existing = self.store.conn.execute(f'SELECT {_KILL_COLUMNS} FROM {_KILL_TABLES[policy]} WHERE kill_id=?',
                                           (observation.event.kill_id,)).fetchone()
        if existing is not None:
            # An already claimed incoming envelope still needs its source binding
            # checked, but a new envelope must not acquire a second source.
            key = observation.receipt.wire.envelope_sha256
            if self.store.conn.execute('SELECT id FROM realm_source WHERE source_key=?', (key,)).fetchone():
                _, created = _accepted(claim_source_in_unit(self.store, observation, human=human, policy=policy))
                if created:
                    _reject()
            return self._existing_kill(existing, observation, human, policy)
        return self._claim_and_write(observation, human, choice)

    def _existing_kill(self, kill, observation, human, policy):
        if kill['fact'] is None:
            _reject()
        read = _accepted(public_fact_in_unit(self.store, kill['fact'], human=human))
        fact, proof = read
        origin = self.store.conn.execute('SELECT source,policy,qualification_json FROM realm_fact_origin WHERE fact=?',
                                         (kill['fact'],)).fetchone()
        raw = self.store.conn.execute('SELECT e.payload FROM realm_source s JOIN game_event e ON e.id=s.game_event_id '
                                      'WHERE s.id=?', (kill['source'],)).fetchone()
        if (origin is None or raw is None or origin['source'] != kill['source']
                or origin['policy'] != policy or proof.policy != policy
                or proof.sources != (kill['source'],) or proof.qualification_json != origin['qualification_json']):
            _reject()
        try:
            payload = json.loads(raw['payload'])
            # The current reader has already validated this exact source and its
            # complete payload; compare every native field including durations.
            if type(payload) is not dict or payload != _projection(observation.event):
                _reject()
            expected = _kill_values(payload, kill['source'], fact.id, proof.qualification_json)
        except (ValueError, TypeError, KeyError, UnicodeError):
            _reject('invalid_row')
        if tuple(kill) != expected:
            _reject()
        return fact

    def _claim_and_write(self, observation, human, choice):
        policy, kind, source_kind = choice
        source, created = _accepted(claim_source_in_unit(self.store, observation, human=human, policy=policy))
        producer_key = f'event:{source.source_key}:{policy}'
        origin = self.store.conn.execute('SELECT fact,source,policy FROM realm_fact_origin WHERE producer_key=?',
                                         (producer_key,)).fetchone()
        if not created:
            if origin is None or origin['source'] != source.id or origin['policy'] != policy:
                _reject()
            # A kill without its native row must never be repaired on replay.
            if policy in _KILL_TABLES:
                _reject()
            fact, proof = _accepted(public_fact_in_unit(self.store, origin['fact'], human=human))
            if proof.sources != (source.id,) or proof.policy != policy:
                _reject()
            return fact
        if origin is not None:
            _reject()
        root = f'event:{source.row_id}'
        proof = _accepted(audit_public_in_unit(self.store, human=human, policy=policy,
                                                refs=(root,), observation=observation, rumor_id=None))
        if proof.sources != (source.id,) or proof.roots != (root,) or proof.policy != policy:
            _reject()
        try:
            draft = public_fact_draft(proof, source)
            text = render_public(policy, dict(draft.output_fields))
            qualification = json.loads(proof.qualification_json)
            proof_json = public_proof_to_json(proof)
        except (ValueError, TypeError, KeyError, UnicodeError):
            _reject('invalid_row')
        if draft.producer_key != producer_key or draft.policy != policy or draft.template_id != policy or not draft.public:
            _reject()
        cursor = self.store.conn.execute('INSERT INTO realm_fact(kind,text,source,ref,public,ts) VALUES (?,?,?,?,1,?)',
                                         (kind, text, source_kind, root, draft.source_at))
        fact_id = cursor.lastrowid
        self.store.conn.execute('INSERT INTO realm_fact_origin(fact,producer_key,source,policy,assurance,proof_json,'
            'qualification_json,text_sha256,revision,state) VALUES (?,?,?,?,?,?,?,?,1,\'active\')',
            (fact_id, producer_key, source.id, policy, proof.assurance, proof_json, proof.qualification_json,
             hashlib.sha256(text.encode('utf-8')).hexdigest()))
        for guild, how in qualification['known_by']:
            self.store.conn.execute('INSERT INTO realm_knows(fact,guild,since,how) VALUES (?,?,?,?)',
                                    (fact_id, guild, draft.source_at, how))
        if policy in _KILL_TABLES:
            values = _kill_values(_projection(observation.event), source.id, fact_id, proof.qualification_json)
            self.store.conn.execute(f'INSERT INTO {_KILL_TABLES[policy]}({_KILL_COLUMNS}) '
                                    f'VALUES ({",".join("?" for _ in values)})', values)
        fact, actual_proof = _accepted(public_fact_in_unit(self.store, fact_id, human=human))
        expected = FactRow(fact_id, kind, text, source_kind, root, True, draft.source_at, 1)
        if fact != expected or actual_proof != proof:
            _reject()
        return fact

    def get_in_unit(self, fact_id, *, human):
        self._require_unit()
        return public_fact_in_unit(self.store, fact_id, human=human)

    @staticmethod
    def _guild(guild):
        if type(guild) is not int or not 1 <= guild <= 0xffffffff:
            raise ValueError('invalid guild')

    def known_in_unit(self, guild, *, human, limit=5):
        """The guild's prompt facts: public, with an active event or rule origin, server-wide
        first, then the newest. Each is read again in full before it counts; a fact with both
        kinds of origin has no single one and never counts."""
        self._require_unit()
        self._guild(guild)
        if type(limit) is not int or not 1 <= limit <= 5:
            raise ValueError('invalid limit')
        cursor = self.store.conn.execute('SELECT f.id,o.state AS event,r.state AS rule FROM realm_knows AS k '
            'JOIN realm_fact AS f ON f.id=k.fact LEFT JOIN realm_fact_origin AS o ON o.fact=f.id '
            'LEFT JOIN realm_rule_origin AS r ON r.fact=f.id '
            "WHERE k.guild=? AND f.public=1 AND (o.state='active' OR r.state='active') "
            "ORDER BY CASE k.how WHEN 'server' THEN 0 ELSE 1 END, f.ts DESC, f.id DESC", (guild,))
        result = []
        try:
            for row in cursor:
                if row['event'] is not None and row['rule'] is not None:
                    continue
                if row['event'] is not None:
                    current = public_fact_in_unit(self.store, row['id'], human=human)
                    current = current if type(current) is Rejected else current[0]
                else:
                    current = rule_fact_in_unit(self.store, row['id'], human=human)
                if type(current) is FactRow and current.public is True:
                    result.append(current)
                    if len(result) == limit:
                        break
        finally:
            cursor.close()
        return tuple(result)

    def known_all_in_unit(self, guild):
        self._require_unit()
        self._guild(guild)
        return tuple(self.store.conn.execute('SELECT f.id,f.kind,f.text,f.source,f.ref,f.public,f.ts '
            'FROM realm_knows AS k JOIN realm_fact AS f ON f.id=k.fact WHERE k.guild=? '
            'ORDER BY f.ts DESC,f.id DESC', (guild,)))

    def known(self, guild, *, human, limit=5):
        with self.store.transaction():
            return self.known_in_unit(guild, human=human, limit=limit)

    def known_all(self, guild):
        with self.store.transaction():
            return self.known_all_in_unit(guild)
