"""Source-bound public proofs and exact active-origin readback.

A proof is a snapshot of actual rows, never authority supplied by a DTO or a
caller digest. Callers own the Store unit and the current privacy identity.
Only the source claim helper writes; publication and current live barriers are
separate callers' responsibilities. Importing this module attaches no hooks.
"""
from __future__ import annotations

from collections import OrderedDict
import hashlib
import json
import math
import re
from dataclasses import fields

from leeroy_mind.human import HumanIdentity
from leeroy_mind.ingress import RowReceipt, WireOrigin, game_row_binding
from leeroy_mind.personas.schema import persona_from_dict
from leeroy_mind.personas.screens import ROMANCE_RE, names_real_name
from leeroy_mind.protocol import BgEnd, sanitize_say_text
from leeroy_mind.storage.store import Store
from leeroy_mind.wow import BOSS_NAMES, DUNGEON_BOSS_NAMES, DUNGEONS, RAID_INSTANCES, is_raid_fight

from .actions import ActionContext
from .protocol import (ArenaEnd, ChannelChat, DuelEnd, PvpKill, RealmAchievement, RealmDungeonKill,
                       RealmKill, Wg)
from .records import (ActorQualification, FactDraft, FactRow, ForumCandidate, ProofNode,
                      PublicProof, QualificationSnapshot, RealmFactReadStamp, RealmObservation,
                      RealmReadStamp, Rejected, StoredSource)

_POLICY = "stored_forum_topic_v1"
_UNIT_ERROR = "Realm public proof requires Store.transaction()"
_SQLITE_MAX = (1 << 63) - 1
_U32 = (1 << 32) - 1
_MAX_MS = 253402300799999
_MAX_SECONDS = 253402300799.999
_MEMORY_COLUMNS = ("id,owner,kind,text,t_event,t_created,actors,place,boss_id,raid_id,"
                   "importance,valence,source,evidence,confidence,status,superseded_by,key,scope,scope_with")
_RUMOR_COLUMNS = "id,root,about,canonical,source,status,created,last_told"
_EVENT_COLUMNS = "id,ts,type,guid,payload,session_id"
_FACT_COLUMNS = "id,kind,root,about,text,secret,holder,created"
_REF = re.compile(r"(memory|event):([1-9][0-9]*)")
_PAYLOAD_KEYS = {"ts", "map", "instance_id", "boss_id", "boss_name", "state", "members"}


class _Evidence(Exception):
    def __init__(self, reason):
        self.reason = reason


def _require_unit(store):
    if not isinstance(store, Store) or not store.in_unit:
        raise RuntimeError(_UNIT_ERROR)


def _fail(reason="invalid_row"):
    raise _Evidence(reason)


def _integer(value, minimum=0, maximum=_U32):
    if type(value) is not int or not minimum <= value <= maximum:
        _fail()
    return value


def _seconds(value):
    if (type(value) not in (int, float) or not 0 <= value <= _MAX_SECONDS
            or not math.isfinite(value)):
        _fail()


def _nullable_id(value, maximum=_SQLITE_MAX):
    if value is not None:
        _integer(value, 1, maximum)


def _utf8(value):
    if type(value) is not str:
        _fail()
    try:
        return value.encode("utf-8")
    except UnicodeEncodeError:
        _fail()


def _canonical(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeEncodeError, RecursionError):
        _fail()


def _strict_json(raw):
    _utf8(raw)

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                _fail()
            result[key] = value
        return result

    def constant(_):
        _fail()

    try:
        parsed = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, RecursionError):
        _fail()
    _canonical(parsed)  # Reject nonfinite exponents and escaped lone surrogates, too.
    return parsed


def _reference(value):
    if len(_utf8(value)) > 128:
        _fail("graph_limit")
    matched = _REF.fullmatch(value)
    if matched is None:
        _fail("invalid_input")
    identifier = int(matched[2])
    _integer(identifier, 1, _SQLITE_MAX)
    return matched[1], identifier


def _identity(human):
    if type(human) is not HumanIdentity:
        _fail("invalid_input")
    # Identity is used only for screening, never projected or persisted.
    if type(human.real_name) is not str:
        _fail("invalid_input")
    try:
        human.real_name.encode("utf-8")
    except UnicodeEncodeError:
        _fail("invalid_input")


def _screen(text, human):
    _utf8(text)
    if names_real_name(text, human):
        _fail("real_name")
    if ROMANCE_RE.search(text):
        _fail("romance")


def _topic(text, boss, human):
    _screen(text, human)  # Always inspect raw prose before the one allowed trim.
    text = text.strip(" ")
    name = re.escape(boss)
    count = r"(?:[2-9]|[1-9][0-9]{1,2})"
    forms = (rf"We killed {name}\.", rf"We wiped on {name} tonight\.",
             rf"We wiped {count}× on {name} tonight\.",
             rf"We wiped once on {name}, then killed it\.",
             rf"We wiped {count}× on {name}, then killed it\.")
    if not any(re.fullmatch(form, text) for form in forms):
        _fail("unsupported_text")


def _output(fields):
    if type(fields) is not dict or set(fields) != {"boss_id", "catalogue", "map"}:
        _fail("invalid_input")
    if fields["catalogue"] != "raid_catalogue_v1" or type(fields["catalogue"]) is not str:
        _fail("invalid_input")
    map_id = _integer(fields["map"])
    boss_id = _integer(fields["boss_id"])
    pair = (map_id, boss_id)
    if pair not in BOSS_NAMES or not is_raid_fight(*pair):
        _fail("topic_mismatch")
    return f"A rumor concerns the raid encounter with {BOSS_NAMES[pair]}."


def render_public(policy, output_fields):
    """Render a finite template. Rendering alone grants no authority."""
    try:
        if type(policy) is not str:
            _fail("unsupported_policy")
        text = _output(output_fields) if policy == _POLICY else _live_output(policy, output_fields)
        if len(text) > 200:
            _fail()
        return text
    except _Evidence:
        raise ValueError("invalid Realm public rendering") from None


def _proof_body(roots, nodes, output, *, policy=_POLICY, assurance="stored_topic", sources=(), qualification=None):
    return dict(policy=policy, assurance=assurance, roots=roots, sources=sources,
                nodes=[dict(kind=n.kind, key=n.key, projection_version=n.projection_version,
                            sha256=n.sha256) for n in nodes],
                qualification={} if qualification is None else qualification, output_fields=output)


class _Resolver:
    def __init__(self, store, human):
        self.store = store
        self.human = human
        self.nodes = {}
        self.states = {}
        self.heights = {}
        self.memories = {}
        self.leaves = set()
        self.pair = None
        self.edges = 0
        self.text_bytes = 0

    def _row(self, table, columns, identifier):
        row = self.store.conn.execute(
            f"SELECT {columns} FROM {table} WHERE id=?", (identifier,)).fetchone()
        if row is None:
            _fail("missing_row")
        row = dict(row)
        self._strings(row)
        return row

    def _strings(self, row):
        # Count distinct fetched rows once, before parsing potentially large JSON.
        for value in row.values():
            if type(value) is str:
                if len(value) > 65536 - self.text_bytes:
                    _fail("graph_limit")
                self.text_bytes += len(_utf8(value))
                if self.text_bytes > 65536:
                    _fail("graph_limit")

    def _node(self, kind, key, projection):
        if len(self.nodes) >= 200:
            _fail("graph_limit")
        body = _canonical(dict(kind=kind, key=key, projection_version=1, projection=projection))
        digest = hashlib.sha256(b"realm-proof-node-v1\0" + len(body).to_bytes(8, "big") + body).hexdigest()
        self.nodes[kind, key] = ProofNode(kind, key, 1, digest)

    def _edge(self):
        self.edges += 1
        if self.edges > 800:
            _fail("graph_limit")

    def _event(self, identifier):
        row = self._row("game_event", _EVENT_COLUMNS, identifier)
        _integer(row["id"], 1, _SQLITE_MAX)
        _seconds(row["ts"])
        _nullable_id(row["guid"], _U32)
        _nullable_id(row["session_id"])
        if row["type"] != "boss":
            _fail("topic_mismatch")
        payload = _strict_json(row["payload"])
        if type(payload) is not dict or set(payload) != _PAYLOAD_KEYS:
            _fail()
        _integer(payload["ts"], 1, _MAX_MS)
        _integer(payload["map"])
        _integer(payload["boss_id"])
        _integer(payload["instance_id"], 1)
        if type(payload["state"]) is not str or payload["state"] not in ("engaged", "wiped", "killed"):
            _fail()
        members = payload["members"]
        if type(members) is not list or len(members) > 128:
            _fail()
        for member in members:
            _integer(member, 1)
        if len(set(members)) != len(members):
            _fail()
        _screen(payload["boss_name"], self.human)
        pair = (payload["map"], payload["boss_id"])
        if pair not in BOSS_NAMES or not is_raid_fight(*pair):
            _fail("topic_mismatch")
        if payload["boss_name"] not in ("", BOSS_NAMES[pair]):
            _fail("topic_mismatch")
        if self.pair is not None and self.pair != pair:
            _fail("topic_mismatch")
        self.pair = pair
        self.leaves.add(f"event:{identifier}")
        row["payload"] = payload
        self._node("game_event", str(identifier), row)

    def _memory(self, identifier, *, realm=False):
        row = self._row("memory", _MEMORY_COLUMNS, identifier)
        _integer(row["id"], 1, _SQLITE_MAX)
        _integer(row["owner"], 0)  # Zero denotes a guild-owned memory.
        for field in ("t_event", "t_created"):
            _integer(row[field], 0, int(_MAX_SECONDS))
        if row["kind"] not in ("observation", "conversation", "event", "reflection", "plan"):
            _fail()
        if row["source"] not in ("game", "chat", "extract", "reflection", "offscreen", "hearsay"):
            _fail("private_evidence")
        if row["scope"] != "open" or type(row["scope_with"]) is not int or row["scope_with"] != 0:
            _fail("private_evidence")
        if row["status"] not in ("active", "superseded"):
            _fail()
        _screen(row["text"], self.human)
        if len(row["text"]) > 200:
            _fail()
        for field in ("place", "key"):
            if row[field] is not None:
                _utf8(row[field])
        for field in ("boss_id", "raid_id"):
            if row[field] is not None:
                _integer(row[field])
        _integer(row["importance"], 1, 10)
        for field, low, high in (("valence", -1, 1), ("confidence", 0, 1)):
            value = row[field]
            if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
                _fail()
        _nullable_id(row["superseded_by"])
        actors = _strict_json(row["actors"])
        if type(actors) is not list or len(actors) > 128:
            _fail()
        for actor in actors:
            _integer(actor, 1)
        if len(set(actors)) != len(actors):
            _fail()
        refs = _strict_json(row["evidence"])
        if type(refs) is not list or (not refs and not realm):
            _fail()
        if len(refs) > 64:
            _fail("graph_limit")
        for ref in refs:
            (_realm_reference if realm else _reference)(ref)
        row["actors"], row["evidence"] = actors, refs
        self.memories[identifier] = row
        self._node("memory", str(identifier), row)
        return refs

    def _walk(self, ref, depth):
        if depth > 32:
            _fail("graph_limit")
        kind, identifier = _reference(ref)
        state = self.states.get(ref)
        if state == "visiting":
            _fail("cycle")
        if state == "done":
            if depth + self.heights[ref] > 32:
                _fail("graph_limit")
            return self.heights[ref]
        # Reserve the final privacy_set node before doing more row work.
        if len(self.nodes) >= 199:
            _fail("graph_limit")
        self.states[ref] = "visiting"
        height = 0
        if kind == "event":
            self._event(identifier)
        else:
            for child in self._memory(identifier):
                self._edge()  # Duplicates consume work even when the child is memoized.
                height = max(height, 1 + self._walk(child, depth + 1))
        self.states[ref] = "done"
        self.heights[ref] = height
        return height

    def _privacy(self, boss):
        refs = sorted(self.states)
        marks = ",".join("?" for _ in refs)
        rows = self.store.conn.execute(
            f"SELECT {_FACT_COLUMNS} FROM fact WHERE root IN ({marks}) ORDER BY id LIMIT 201", refs).fetchall()
        if len(rows) > 200:
            _fail("graph_limit")
        projection = []
        for raw in rows:
            row = dict(raw)
            self._strings(row)
            _integer(row["id"], 1, _SQLITE_MAX)
            if row["root"] not in self.states:
                _fail()
            _nullable_id(row["about"], _U32)
            _integer(row["holder"])
            _seconds(row["created"])
            if type(row["secret"]) is not int or row["secret"] not in (0, 1):
                _fail()
            _screen(row["text"], self.human)
            if row["secret"] or row["kind"] in ("whisper_scene", "confession", "life_news", "real_name"):
                _fail("private_evidence")
            if row["kind"] not in ("said_about", "feud_cause", "noticed"):
                _fail()
            if row["text"]:
                _topic(row["text"], boss, self.human)
            projection.append(row)
        key = hashlib.sha256(_canonical(refs)).hexdigest()
        self._node("privacy_set", key, projection)

    def resolve(self, rumor_id, refs):
        rumor = self._row("rumor", _RUMOR_COLUMNS, rumor_id)
        _integer(rumor["id"], 1, _SQLITE_MAX)
        _integer(rumor["source"], 1, _SQLITE_MAX)
        _integer(rumor["about"], 1)
        _seconds(rumor["created"])
        _seconds(rumor["last_told"])
        if rumor["status"] != "live":
            _fail()
        if _reference(rumor["root"])[0] != "event":
            _fail("invalid_input")
        _screen(rumor["canonical"], self.human)
        expected = (f"memory:{rumor['source']}",)
        if refs is not None and refs != expected:
            _fail("invalid_input")
        self._node("rumor", str(rumor_id), rumor)
        self._edge()
        self._walk(expected[0], 1)
        source = self.memories[rumor["source"]]
        if source["kind"] != "event" or source["source"] != "game":
            _fail()
        if rumor["about"] not in source["actors"]:
            _fail()
        if rumor["root"] not in self.leaves or self.pair is None:
            _fail("topic_mismatch")
        map_id, boss_id = self.pair
        if source["boss_id"] != boss_id or source["raid_id"] != map_id:
            _fail("topic_mismatch")
        boss = BOSS_NAMES[self.pair]
        for row in self.memories.values():
            if ((row["boss_id"] is not None and row["boss_id"] != boss_id)
                    or (row["raid_id"] is not None and row["raid_id"] != map_id)):
                _fail("topic_mismatch")
            _topic(row["text"], boss, self.human)
        _topic(rumor["canonical"], boss, self.human)
        self._privacy(boss)
        output = dict(boss_id=boss_id, catalogue="raid_catalogue_v1", map=map_id)
        text = render_public(_POLICY, output)
        _screen(text, self.human)
        nodes = tuple(sorted(self.nodes.values(), key=lambda n: (n.kind, n.key, n.projection_version)))
        digest = hashlib.sha256(b"realm-proof-v1\0" + _canonical(_proof_body(expected, nodes, output))).hexdigest()
        proof = PublicProof(_POLICY, "stored_topic", expected, (), nodes, "{}", _canonical(output).decode("utf-8"), digest)
        return proof, rumor, text


def _input_id(identifier):
    if type(identifier) is not int or not 1 <= identifier <= _SQLITE_MAX:
        _fail("invalid_input")


def audit_public_in_unit(store, *, human: HumanIdentity, policy, refs,
                         observation=None, rumor_id=None):
    """Audit one direct live source or the complete historical rumor graph."""
    _require_unit(store)
    try:
        _identity(human)
        if type(policy) is str and policy in _LIVE:
            if (type(refs) in (tuple, list) and len(refs) == 1 and type(refs[0]) is str
                    and refs[0].startswith(('memory:', 'realm:'))):
                _fail("unsupported_policy")
            if rumor_id is not None:
                _fail("invalid_input")
            return _audit_live(store, observation, human, policy, refs)
        if type(policy) is not str or policy != _POLICY:
            _fail("unsupported_policy")
        _input_id(rumor_id)
        if observation is not None or type(refs) not in (tuple, list) or len(refs) != 1:
            _fail("invalid_input")
        if _reference(refs[0])[0] != "memory":
            _fail("invalid_input")
        return _Resolver(store, human).resolve(rumor_id, tuple(refs))[0]
    except _Evidence as error:
        return Rejected(error.reason)


def stored_forum_candidate_in_unit(store, rumor_id, *, human: HumanIdentity):
    """Select the exact stored rumor using the same complete audit machinery."""
    _require_unit(store)
    try:
        _identity(human)
        _input_id(rumor_id)
        proof, rumor, text = _Resolver(store, human).resolve(rumor_id, None)
        return ForumCandidate(rumor["id"], rumor["source"], rumor["root"], rumor["about"], text, proof)
    except _Evidence as error:
        return Rejected(error.reason)


def _proof_shape(proof):
    if type(proof) is not PublicProof:
        _fail("invalid_input")
    try:
        if any(type(value) is not tuple for value in (proof.roots, proof.sources, proof.nodes)):
            _fail("invalid_input")
        if len(proof.nodes) > 200:
            _fail("invalid_input")
        for node in proof.nodes:
            if type(node) is not ProofNode:
                _fail("invalid_input")
            ProofNode(node.kind, node.key, node.projection_version, node.sha256)
        checked = PublicProof(proof.policy, proof.assurance, proof.roots, proof.sources, proof.nodes,
                              proof.qualification_json, proof.output_fields_json, proof.sha256)
    except (ValueError, TypeError, AttributeError, RecursionError):
        _fail("invalid_input")
    if checked != proof:
        _fail("invalid_input")
    if proof.policy != _POLICY:
        _fail("unsupported_policy")
    qualification = _strict_json(proof.qualification_json)
    output = _strict_json(proof.output_fields_json)
    _output(output)
    if (proof.assurance != "stored_topic" or proof.sources or len(proof.roots) != 1
            or qualification != {} or proof.qualification_json != "{}"
            or _canonical(output).decode("utf-8") != proof.output_fields_json):
        _fail("invalid_input")
    if _reference(proof.roots[0])[0] != "memory":
        _fail("invalid_input")
    rumors = [node for node in proof.nodes if node.kind == "rumor"]
    if len(rumors) != 1 or re.fullmatch(r"[1-9][0-9]{0,18}", rumors[0].key) is None:
        _fail("invalid_input")
    identifier = int(rumors[0].key)
    _input_id(identifier)
    return identifier


def revalidate_public_in_unit(store, proof, *, human: HumanIdentity):
    """Reconstruct current evidence and accept only the complete original value."""
    _require_unit(store)
    try:
        _identity(human)
        _checked(proof, PublicProof)
        if proof.policy in _LIVE:
            return _revalidate_live(store, proof, human)
        rumor_id = _proof_shape(proof)
        current = _Resolver(store, human).resolve(rumor_id, proof.roots)[0]
        if current != proof:
            _fail("proof_changed")
        return current
    except _Evidence as error:
        return Rejected(error.reason)

# Direct live policies deliberately have no composite/memory traversal. The
# exact source binding and four nodes below share the stored proof hash framing.
_LIVE = {
    'bg_result_v1': (BgEnd, 'bg_end', 'bg_result', 'observed_event'),
    'arena_result_v1': (ArenaEnd, 'arena_end', 'arena_result', 'observed_event'),
    'wg_result_v1': (Wg, 'wg', 'wg_result', 'observed_event'),
    'pvp_result_v1': (PvpKill, 'pvp_kill', 'pvp_kill', 'observed_event'),
    'channel_raid_topic_v1': (ChannelChat, 'channel_chat', 'channel_topic', 'observed_topic'),
    'verified_raid_kill_v1': (RealmKill, 'realm_kill', 'kill', 'verified_event'),
    'realm_first_achievement_v1': (RealmAchievement, 'realm_achievement', 'realm_first', 'observed_event'),
    'duel_result_v1': (DuelEnd, 'duel_end', 'duel_result', 'observed_event'),
    'verified_dungeon_kill_v1': (RealmDungeonKill, 'realm_dungeon_kill', 'kill', 'verified_event'),
}
# Native guild kills (one wire body for both) and the catalogue each one's boss must be in.
_KILLS = {'verified_raid_kill_v1': 'raid_catalogue_v1', 'verified_dungeon_kill_v1': 'dungeon_catalogue_v1'}
# Facts the whole server knows: the guild itself as 'own', every rival guild as 'server'. A 5-man
# kill is not one: it is the guild's own, and present for a rival actor's guild that took part.
_SERVER_WIDE = ('wg_result_v1', 'realm_first_achievement_v1', 'verified_raid_kill_v1')
_DUEL_RESULTS = ('won', 'fled')  # 'interrupted' has no winner, so it is no result
_SOURCE_COLUMNS = ('id,source_key,protocol_version,event_type,game_event_id,chat_line_id,'
                   'row_sha256,server_ms,received_at,guild,human,top_keys_json')
_U64 = (1 << 64) - 1
_SNAPSHOT_KEYS = {'guild', 'human', 'captured_at', 'human_confirmed', 'cast', 'actors',
    'rival_guilds', 'speaker_kind', 'speaker_guid', 'speaker_guild', 'transport_epoch',
    'speaker_name', 'speaker_name_source', 'speaker_name_sha256'}
_CONTEXT_KEYS = {'enabled', 'connected', 'presence_known', 'generation', 'process_id',
                 'connection_epoch', 'guild', 'human'}
_QUALIFICATION_KEYS = {'schema', 'snapshot', 'context', 'participants', 'native_present',
                       'qualified_cast', 'known_by'}


def _checked(value, cls):
    """Recheck exact runtime DTO types, including objects bypassing constructors."""
    if type(value) is not cls:
        _fail('invalid_input')
    try:
        copy = cls(**{field.name: getattr(value, field.name) for field in fields(cls)})
    except (ValueError, TypeError, AttributeError):
        _fail('invalid_input')
    if copy != value:
        _fail('invalid_input')
    return value


def _keys(value, expected):
    if type(value) is not dict or set(value) != set(expected):
        _fail()


def _hash(value):
    if type(value) is not str or re.fullmatch(r'[0-9a-f]{64}', value) is None:
        _fail()
    return value


def _epoch(value):
    if type(value) is not str or re.fullmatch(r'[1-9][0-9]{0,19}', value) is None:
        _fail()
    return _integer(int(value), 1, _U64)


def _ids(value, *, ordered=True):
    if type(value) is not list or len(value) > 128:
        _fail()
    for identifier in value:
        _integer(identifier, 1)
    if len(set(value)) != len(value) or (ordered and value != sorted(value)):
        _fail()
    return value


def _tagged_hash(tag, value):
    return hashlib.sha256(tag + _canonical(value)).hexdigest()


def _actor_projection(actor):
    return {key: getattr(actor, key) for key in ('guid', 'guild', 'role', 'status')}


def _speaker_projection(snapshot):
    result = dict(source=snapshot.speaker_name_source, guid=snapshot.speaker_guid,
                  name=snapshot.speaker_name)
    if snapshot.speaker_kind == 'actor':
        actor = next((actor for actor in snapshot.actors if actor.guid == snapshot.speaker_guid), None)
        if actor is None or actor.guild != snapshot.speaker_guild:
            _fail()
        result.update(guild=actor.guild, role=actor.role, status=actor.status)
    return result


def _snapshot_context(snapshot, context):
    _checked(snapshot, QualificationSnapshot)
    _checked(context, ActionContext)
    if any(type(getattr(snapshot, key)) is not tuple for key in ('cast', 'actors', 'rival_guilds')):
        _fail('invalid_input')
    if (snapshot.human_confirmed is not True or snapshot.human in snapshot.cast
            or snapshot.guild in snapshot.rival_guilds
            or (context.guild, context.human) != (snapshot.guild, snapshot.human)
            or any(getattr(context, key) is not True for key in ('enabled', 'connected', 'presence_known'))):
        _fail('invalid_input')
    _integer(context.generation, 0, _SQLITE_MAX)
    _integer(context.connection_epoch, 1, _U64)
    if not 1 <= len(_utf8(context.process_id)) <= 256:
        _fail()
    for actor in snapshot.actors:
        _checked(actor, ActorQualification)
        if (actor.status != 'active' or actor.guid == snapshot.human or actor.guid in snapshot.cast
                or actor.guild not in (*snapshot.rival_guilds, snapshot.guild)
                or actor.row_sha256 != _tagged_hash(b'realm-actor-qualification-v1\0', _actor_projection(actor))):
            _fail()
    if snapshot.speaker_kind == 'human':
        if (snapshot.speaker_guid, snapshot.speaker_guild) != (snapshot.human, snapshot.guild):
            _fail()
    if snapshot.speaker_kind != 'none':
        if snapshot.speaker_name_sha256 != _tagged_hash(b'realm-speaker-name-v1\0', _speaker_projection(snapshot)):
            _fail()
    snap = {field.name: getattr(snapshot, field.name) for field in fields(QualificationSnapshot)}
    snap['captured_at'] = float(snapshot.captured_at)
    snap['transport_epoch'] = str(snapshot.transport_epoch)
    snap['cast'], snap['rival_guilds'] = list(snapshot.cast), list(snapshot.rival_guilds)
    snap['actors'] = [dict(**_actor_projection(actor), row_sha256=actor.row_sha256) for actor in snapshot.actors]
    ctx = {field.name: getattr(context, field.name) for field in fields(ActionContext)}
    ctx['connection_epoch'] = str(context.connection_epoch)
    return snap, ctx


def _decode_qualification(value):
    _keys(value, _QUALIFICATION_KEYS)
    if value['schema'] != 'realm_qualification_v1' or type(value['schema']) is not str:
        _fail()
    snap, ctx = value['snapshot'], value['context']
    _keys(snap, _SNAPSHOT_KEYS)
    _keys(ctx, _CONTEXT_KEYS)
    if type(snap['captured_at']) is not float:
        _fail()
    for key in ('cast', 'rival_guilds'):
        _ids(snap[key])
    actors = snap['actors']
    if type(actors) is not list or len(actors) > 128:
        _fail()
    parsed = []
    try:
        for actor in actors:
            _keys(actor, {'guid', 'guild', 'role', 'status', 'row_sha256'})
            parsed.append(ActorQualification(**actor))
        snapshot = QualificationSnapshot(**{**snap, 'actors': tuple(parsed),
            'cast': tuple(snap['cast']), 'rival_guilds': tuple(snap['rival_guilds']),
            'transport_epoch': _epoch(snap['transport_epoch'])})
        context = ActionContext(**{**ctx, 'connection_epoch': _epoch(ctx['connection_epoch'])})
    except (ValueError, TypeError):
        _fail()
    rebuilt_snap, rebuilt_ctx = _snapshot_context(snapshot, context)
    if rebuilt_snap != snap or rebuilt_ctx != ctx:
        _fail()
    for key in ('participants', 'native_present', 'qualified_cast'):
        _ids(value[key])
    known = value['known_by']
    if type(known) is not list or not known or len(known) > 129:
        _fail()
    for pair in known:
        if type(pair) is not list or len(pair) != 2:
            _fail()
        _integer(pair[0], 1)
        if type(pair[1]) is not str or pair[1] not in ('own', 'present', 'server'):
            _fail()
    if [pair[0] for pair in known] != sorted({pair[0] for pair in known}):
        _fail()
    if len(_canonical(value)) > 8192:
        _fail('graph_limit')
    return snapshot, context


def _qualification(snapshot, context, participants, native_present, qualified_cast, known_by):
    snap, ctx = _snapshot_context(snapshot, context)
    result = dict(schema='realm_qualification_v1', snapshot=snap, context=ctx,
                  participants=participants, native_present=native_present,
                  qualified_cast=qualified_cast, known_by=known_by)
    _decode_qualification(result)
    return result


def _live_output(policy, output):
    if policy not in _LIVE:
        _fail('unsupported_policy')
    if policy == 'bg_result_v1':
        _keys(output, {'bg_type', 'winner'})
        _integer(output['bg_type'], 1)
        if type(output['winner']) is not str or output['winner'] not in ('horde', 'alliance'):
            _fail()
        return f"{output['winner'].title()} won battleground type {output['bg_type']}."
    if policy == 'arena_result_v1':
        _keys(output, {'arena_type', 'winner_team', 'loser_team'})
        _integer(output['arena_type'], 2, 5)
        if output['arena_type'] not in (2, 3, 5):
            _fail()
        _integer(output['winner_team'], 1)
        _integer(output['loser_team'], 1)
        if output['winner_team'] == output['loser_team']:
            _fail()
        return (f"Arena team {output['winner_team']} beat team {output['loser_team']} in "
                f"{output['arena_type']}v{output['arena_type']}.")
    if policy == 'wg_result_v1':
        _keys(output, {'defender'})
        if type(output['defender']) is not str or output['defender'] not in ('horde', 'alliance'):
            _fail()
        return f"{output['defender'].title()} controls Wintergrasp after the battle."
    if policy == 'pvp_result_v1':
        _keys(output, {'killer', 'victim', 'zone'})
        _integer(output['killer'], 1); _integer(output['victim'], 1); _integer(output['zone'])
        if output['killer'] == output['victim']:
            _fail()
        return f"Character {output['killer']} defeated character {output['victim']} in zone {output['zone']}."
    if policy == 'channel_raid_topic_v1':
        _keys(output, {'catalogue', 'channel', 'raid_id'})
        _integer(output['raid_id'])
        if (type(output['channel']) is not str or output['channel'] not in ('trade', 'general', 'lfg', 'recruitment')
                or output['catalogue'] != 'raid_catalogue_v1' or type(output['catalogue']) is not str
                or output['raid_id'] not in RAID_INSTANCES):
            _fail()
        return f"A public {output['channel']} message discussed {RAID_INSTANCES[output['raid_id']]}."
    if policy == 'realm_first_achievement_v1':
        _keys(output, {'achievement_id', 'guid', 'name'})
        _integer(output['achievement_id'], 1); _integer(output['guid'], 1)
        # Native DBC text; the fact text it makes goes through every screen and cleaning step.
        if type(output['name']) is not str or not 1 <= len(_utf8(output['name'])) <= 128:
            _fail('unsupported_text')
        return f"Character {output['guid']} earned {output['name']}."
    if policy == 'duel_result_v1':
        _keys(output, {'winner', 'loser', 'result', 'zone'})
        _integer(output['winner'], 1); _integer(output['loser'], 1); _integer(output['zone'])
        if (output['winner'] == output['loser'] or type(output['result']) is not str
                or output['result'] not in _DUEL_RESULTS):
            _fail()
        if output['result'] == 'won':
            return (f"Character {output['winner']} beat character {output['loser']} in a duel "
                    f"in zone {output['zone']}.")
        return (f"Character {output['loser']} fled a duel with character {output['winner']} "
                f"in zone {output['zone']}.")
    if policy == 'verified_dungeon_kill_v1':
        _keys(output, {'catalogue', 'map', 'boss_id'})
        _integer(output['map']); _integer(output['boss_id'])
        if (type(output['catalogue']) is not str or output['catalogue'] != 'dungeon_catalogue_v1'
                or (output['map'], output['boss_id']) not in DUNGEON_BOSS_NAMES):
            _fail('topic_mismatch')
        # Defeated: some 5-man fights end without a death (Mal'Ganis, the Argent Champion).
        return (f"The guild defeated {DUNGEON_BOSS_NAMES[output['map'], output['boss_id']]} "
                f"in {DUNGEONS[output['map']]}.")
    _keys(output, {'catalogue', 'map', 'boss_id'})
    _integer(output['map']); _integer(output['boss_id'])
    if (type(output['catalogue']) is not str or output['catalogue'] != 'raid_catalogue_v1'
            or (output['map'], output['boss_id']) not in BOSS_NAMES
            or not is_raid_fight(output['map'], output['boss_id'])):
        _fail('topic_mismatch')
    return f"The guild killed {BOSS_NAMES[output['map'], output['boss_id']]}."


def _raid_forms(raid_id):
    name = RAID_INSTANCES[raid_id]
    return (f'Ready for {name}.', f'Looking for a group for {name}.', f'Anyone for {name}?')


def _channel_raid(text, human):
    _screen(text, human)
    if len(text) > 200:
        _fail('unsupported_text')
    core = text.strip(' ')
    if sanitize_say_text(core) != core:
        _fail('unsupported_text')
    for raid_id in RAID_INSTANCES:
        if core.casefold() in tuple(form.casefold() for form in _raid_forms(raid_id)):
            return raid_id
    _fail('unsupported_text')


def _derive(policy, payload, snapshot, human, *, qualify=True):
    """Validate every payload field, optionally admitting fresh qualification."""
    cls = _LIVE[policy][0]
    _keys(payload, {field.name for field in fields(cls)})
    _integer(payload['ts'], 1, _MAX_MS)
    participants, native, qualified = [], [], []
    if policy != 'channel_raid_topic_v1' and snapshot.speaker_kind != 'none':
        _fail()
    if policy in ('bg_result_v1', 'arena_result_v1'):
        participants = sorted(_ids(payload['members'], ordered=False))
        names = ('bg_type', 'winner') if policy == 'bg_result_v1' else ('arena_type', 'winner_team', 'loser_team')
        output = {key: payload[key] for key in names}
        if policy == 'arena_result_v1':
            _integer(payload['winner_change'], -(1 << 31), (1 << 31) - 1)
            _integer(payload['loser_change'], -(1 << 31), (1 << 31) - 1)
    elif policy == 'pvp_result_v1':
        _integer(payload['map'])
        output = {key: payload[key] for key in ('killer', 'victim', 'zone')}
        participants = sorted([_integer(payload['killer'], 1), _integer(payload['victim'], 1)])
    elif policy == 'wg_result_v1':
        if type(payload['state']) is not str or payload['state'] != 'end' or type(payload['seconds']) is not int or payload['seconds'] != 0:
            _fail()
        if type(payload['update_ms']) is not float or not math.isfinite(payload['update_ms']) or payload['update_ms'] < 0:
            _fail()
        output = {'defender': payload['defender']}
    elif policy == 'channel_raid_topic_v1':
        _integer(payload['from_guid'], 1); _integer(payload['zone'])
        if type(payload['via_bridge']) is not bool:
            _fail()
        if qualify:
            if snapshot.speaker_kind == 'none':
                _fail('private_evidence')
            if (payload['from_guid'] != snapshot.speaker_guid
                    or payload['from_name'] != snapshot.speaker_name):
                _fail()
        _screen(payload['from_name'], human)
        if re.fullmatch(r'[A-Za-z]{2,12}', payload['from_name']) is None:
            _fail('unsupported_text')
        output = dict(catalogue='raid_catalogue_v1', channel=payload['channel'],
                      raid_id=_channel_raid(payload['text'], human))
        participants = [payload['from_guid']]
    elif policy == 'realm_first_achievement_v1':
        # The real wire class checks every field, and that a realm first carries its DBC flag.
        try:
            RealmAchievement(**payload)
        except (ValueError, TypeError):
            _fail()
        # Every earned achievement is published (never a quiet grant); only a realm first is a fact.
        if payload['realm_first'] is not True:
            _fail('private_evidence')
        participants = [payload['guid']]
        if qualify and payload['guid'] not in (snapshot.human, *snapshot.cast):
            _fail('private_evidence')
        output = {key: payload[key] for key in ('achievement_id', 'guid', 'name')}
    elif policy == 'duel_result_v1':
        winner, loser = _integer(payload['winner'], 1), _integer(payload['loser'], 1)
        _integer(payload['map']); _integer(payload['zone'])
        if (winner == loser or type(payload['city']) is not bool or type(payload['result']) is not str
                or payload['result'] not in (*_DUEL_RESULTS, 'interrupted')):
            _fail()
        # A duel in a city zone (either duelist's) is never a fact, nor one without a winner.
        if payload['city'] or payload['result'] not in _DUEL_RESULTS:
            _fail('private_evidence')
        output = {key: payload[key] for key in ('winner', 'loser', 'result', 'zone')}
        participants = sorted([winner, loser])
    else:
        # The real wire class (raid or 5-man, one body) checks every field: the exact native
        # mode, the duration pair, the attempt count, timestamps, kill id, native epoch and
        # the sorted bounded native manifest.
        try:
            cls(**{**payload, 'cast_members': tuple(_ids(payload['cast_members']))})
        except (ValueError, TypeError):
            _fail()
        if (payload['guild_id'], payload['human_guid']) != (snapshot.guild, snapshot.human):
            _fail()
        native = sorted(payload['cast_members'] + ([snapshot.human] if payload['human_present'] else []))
        participants = native[:]
        qualified = sorted(set(payload['cast_members']) & set(snapshot.cast))
        if qualify and not payload['human_present'] and len(qualified) < 5:
            _fail('private_evidence')
        output = dict(catalogue=_KILLS[policy], map=payload['map'], boss_id=payload['boss_id'])
    text = _live_output(policy, output)
    _screen(text, human)
    from leeroy_mind.memory.writer import clean_memory_text
    if len(text) > 200 or sanitize_say_text(text) != text or clean_memory_text(text, human) != text:
        _fail('unsupported_text')
    if not qualify:
        # Replay still validates every raw field and current privacy rule. Its
        # original active origin alone supplies participant/speaker admission.
        return output, participants, native, qualified, []
    known = _knowers(policy, snapshot, participants)
    if not known:
        _fail('private_evidence')
    return output, participants, native, qualified, [[guild, known[guild]] for guild in sorted(known)]


def _knowers(policy, snapshot, participants):
    """Each guild that knows a fact, and how."""
    if policy in _SERVER_WIDE:
        return {snapshot.guild: 'own', **{guild: 'server' for guild in snapshot.rival_guilds}}
    if policy == 'channel_raid_topic_v1':
        return {snapshot.speaker_guild: 'own'}
    known = {snapshot.guild: 'own'} if policy == 'verified_dungeon_kill_v1' else {}
    if set(participants) & {snapshot.human, *snapshot.cast}:
        known.setdefault(snapshot.guild, 'present')
    for actor in snapshot.actors:
        if actor.guid in participants:
            known.setdefault(actor.guild, 'present')
    return known


def _policy_event(event):
    for policy, (cls, _, _, _) in _LIVE.items():
        if type(event) is cls:
            return policy
    _fail('unsupported_policy')


def _event_payload(event, policy):
    _checked(event, _LIVE[policy][0])
    result = {}
    for field in fields(type(event)):
        value = getattr(event, field.name)
        if field.name in ('members', 'cast_members'):
            if type(value) is not tuple:
                _fail('invalid_input')
            value = list(value)
        result[field.name] = value
    return result


def _receipt(receipt):
    _checked(receipt, RowReceipt)
    _checked(receipt.wire, WireOrigin)
    if receipt.row_kind != 'game_event' or type(receipt.wire.top_keys) is not tuple:
        _fail('invalid_input')
    return receipt.wire


def _live_row(store, row_id, policy, *, counter=None):
    if counter is not None:
        row = counter._row('game_event', _EVENT_COLUMNS, row_id)
    else:
        row = store.conn.execute(f'SELECT {_EVENT_COLUMNS} FROM game_event WHERE id=?', (row_id,)).fetchone()
        if row is None:
            _fail('missing_row')
        row = dict(row)
        if sum(len(_utf8(value)) for value in row.values() if type(value) is str) > 65536:
            _fail('graph_limit')
    _integer(row['id'], 1, _SQLITE_MAX); _seconds(row['ts']); _nullable_id(row['session_id'])
    if type(row['type']) is not str or row['type'] != _LIVE[policy][1]:
        _fail()
    row['payload'] = _strict_json(row['payload'])
    _keys(row['payload'], {field.name for field in fields(_LIVE[policy][0])})
    # The App's row keeps an event's own guid field (an achievement's earner), else none.
    guid = row['payload'].get('guid')
    if type(row['guid']) is not type(guid) or row['guid'] != guid:
        _fail()
    return row


def _bound_receipt(store, receipt, event, policy):
    wire = _receipt(receipt)
    payload = _event_payload(event, policy)
    expected_keys = tuple(sorted({'v', 'type', *payload}))
    if (wire.event_type != _LIVE[policy][1] or wire.top_keys != expected_keys
            or wire.server_ms != payload['ts']):
        _fail('invalid_input')
    row = _live_row(store, receipt.row_id, policy)
    if (_canonical(row['payload']) != _canonical(payload)
            or game_row_binding(row['id'], row['type'], row['payload']) != receipt.row_sha256):
        _fail('proof_changed')
    return row, payload


class _PersonaRows:
    """How each member's stored persona row loads, reused by that row's exact text.

    `load_persona` is a pure function of the guid and the persona_core.json text. The
    text is still read in the caller's unit for every capture; only a parse of identical
    text is reused, so a changed, added or removed persona is never answered from an
    older row. Results: None (no row), 'ok', 'unreadable' (ValueError) or 'mismatch'.
    Any other exception is raised every time, as before, and nothing is remembered.
    """

    LIMIT = 4096

    def __init__(self):
        self._rows: OrderedDict[int, tuple[object, str]] = OrderedDict()

    def clear(self):
        self._rows.clear()

    def __call__(self, store, guid):
        text = store.get_persona_json(guid)
        if text is None:
            return None
        known = self._rows.get(guid)
        if known is not None and type(known[0]) is type(text) and known[0] == text:
            self._rows.move_to_end(guid)
            return known[1]
        try:
            persona = persona_from_dict(json.loads(text))
        except ValueError:  # Expected unreadable/newer persona data only.
            result = 'unreadable'
        else:
            result = 'ok' if persona.guid == guid else 'mismatch'
        self._rows[guid] = (text, result)
        self._rows.move_to_end(guid)
        while len(self._rows) > self.LIMIT:
            self._rows.popitem(last=False)
        return result


_persona_rows = _PersonaRows()


def cannot_qualify(event):
    """The refusal for native evidence that no capture could make a fact, else None: an
    achievement that is not a realm first, a duel without a winner or in a city. It reads the
    event alone, so the caller can refuse before any receipt read or Store unit."""
    if type(event) is RealmAchievement and event.realm_first is not True:
        return Rejected('private_evidence')
    if type(event) is DuelEnd and (event.city is not False or event.result not in _DUEL_RESULTS):
        return Rejected('private_evidence')
    return None


def capture_qualification_in_unit(store, *, human: HumanIdentity, event,
        receipt: RowReceipt, context: ActionContext, confirmed_human_guid: int,
        transport_epoch: int, captured_at: float):
    """Capture historical classification from complete real rows before await.

    The dispatcher must supply its actual confirmed identity and receipt, and
    separately prove current Bridge writer/observed transport equality. A local
    snapshot is not a signature or a substitute for that later current barrier.
    An unavailable actor speaker remains absent; strict first-source admission
    rejects it, while replay binding may still reach its original active origin.
    """
    _require_unit(store)
    try:
        _identity(human)
        _integer(human.guid, 1)
        policy = _policy_event(event)
        _checked(context, ActionContext)
        _integer(confirmed_human_guid, 1); _integer(transport_epoch, 1, _U64)
        if (human.guid != confirmed_human_guid or context.human != confirmed_human_guid
                or any(getattr(context, key) is not True for key in ('enabled', 'connected', 'presence_known'))
                or _receipt(receipt).transport_epoch != transport_epoch):
            _fail('invalid_input')
        _bound_receipt(store, receipt, event, policy)
        stored = {row['guid'] for row in store.conn.execute('SELECT guid FROM persona_core')}
        kicked = {row['guid'] for row in store.conn.execute('SELECT guid FROM realm_kick')}
        for guid in stored | kicked:
            _integer(guid, 1)
        cast = []
        for row in store.conn.execute('SELECT guid FROM cast_member WHERE level>=80 ORDER BY guid'):
            guid = _integer(row['guid'], 1)
            if guid == confirmed_human_guid or guid in kicked:
                continue
            loaded = _persona_rows(store, guid)
            if loaded == 'mismatch':
                _fail()
            if loaded == 'ok':
                cast.append(guid)
        guilds = [_integer(row['id'], 1) for row in store.conn.execute(
            "SELECT id FROM realm_guild WHERE status='active' ORDER BY id")]
        rivals = tuple(guild for guild in guilds if guild != context.guild)
        actor_rows = store.conn.execute(
            "SELECT a.guid,a.guild,a.role,a.status,a.name FROM realm_actor a "
            "JOIN realm_guild g ON g.id=a.guild WHERE a.status='active' AND g.status='active' ORDER BY a.guid").fetchall()
        actors, eligible_names = [], {}
        for row in actor_rows:
            guid = _integer(row['guid'], 1)
            if guid == confirmed_human_guid or guid in stored or guid in kicked:
                continue
            projection = {key: row[key] for key in ('guid', 'guild', 'role', 'status')}
            try:
                actor = ActorQualification(**projection,
                    row_sha256=_tagged_hash(b'realm-actor-qualification-v1\0', projection))
            except (ValueError, TypeError):
                _fail()
            actors.append(actor)
            eligible_names[guid] = row['name']
        kind, guid, guild, name, name_source, name_hash = 'none', 0, 0, '', 'none', None
        if policy == 'channel_raid_topic_v1':
            guid = _integer(event.from_guid, 1)
            if guid == confirmed_human_guid:
                kind, guild = 'human', context.guild
                meta_name = (store.get_meta('human_name')
                    if store.get_meta('human_guid') == str(confirmed_human_guid) else None)
                if meta_name:
                    name, name_source = meta_name, 'human_meta'
                else:
                    row = store.conn.execute('SELECT name FROM cast_member WHERE guid=?', (guid,)).fetchone()
                    if row is None:
                        _fail('missing_row')
                    name, name_source = row['name'], 'cast_member'
                if human.character_name != name:
                    _fail('invalid_input')
                projection = dict(source=name_source, guid=guid, name=name)
            else:
                actor = next((actor for actor in actors if actor.guid == guid), None)
                if actor is None:
                    # No current speaker qualification is available. Do not
                    # invent historical identity from the replay's raw name.
                    guid = 0
                else:
                    kind, guild, name, name_source = 'actor', actor.guild, eligible_names[guid], 'realm_actor'
                    projection = dict(source=name_source, name=name, **_actor_projection(actor))
            if kind != 'none':
                name_hash = _tagged_hash(b'realm-speaker-name-v1\0', projection)
        try:
            snapshot = QualificationSnapshot(context.guild, confirmed_human_guid, float(captured_at)
                if type(captured_at) in (int, float) else captured_at, True, tuple(cast), tuple(actors),
                rivals, kind, guid, guild, transport_epoch, name, name_source, name_hash)
        except (ValueError, TypeError, OverflowError):
            _fail('invalid_input')
        # Validate the complete serialized snapshot without requiring this event
        # to qualify for a public policy yet; reducers decide that separately.
        _qualification(snapshot, context, [], [], [], [[snapshot.guild, 'own']])
        return snapshot
    except _Evidence as error:
        return Rejected(error.reason)


def _observation(store, observation, human, policy, *, qualify=True):
    if policy not in _LIVE:
        _fail('unsupported_policy')
    _checked(observation, RealmObservation)
    snapshot, context = observation.qualification, observation.context
    _integer(human.guid, 1)
    _snapshot_context(snapshot, context)
    wire = _receipt(observation.receipt)
    if (snapshot.human != human.guid or wire.transport_epoch != snapshot.transport_epoch):
        _fail('invalid_input')
    row, payload = _bound_receipt(store, observation.receipt, observation.event, policy)
    output, participants, native, qualified, known = _derive(policy, payload, snapshot, human, qualify=qualify)
    if qualify:
        qualification = _qualification(snapshot, context, participants, native, qualified, known)
    else:
        # Apply the same complete capture size bound without manufacturing new
        # participant/knowledge authority for a retained source.
        _qualification(snapshot, context, [], [], [], [[snapshot.guild, 'own']])
        qualification = None
    return row, payload, output, qualification


def validate_live_binding_in_unit(store, observation, *, human: HumanIdentity, policy):
    """Check exact replay binding/shape/privacy, never fresh qualification."""
    _require_unit(store)
    try:
        _identity(human)
        if type(policy) is not str:
            _fail('unsupported_policy')
        _observation(store, observation, human, policy, qualify=False)
        return None
    except _Evidence as error:
        return Rejected(error.reason)


def validate_live_observation_in_unit(store, observation, *, human: HumanIdentity, policy):
    """Check binding plus fresh qualification; None is not publication approval."""
    _require_unit(store)
    try:
        _identity(human)
        if type(policy) is not str:
            _fail('unsupported_policy')
        _observation(store, observation, human, policy)
        return None
    except _Evidence as error:
        return Rejected(error.reason)


def _read_source(store, *, key=None, identifier=None, policy, counter=None):
    column, value = ('source_key', key) if key is not None else ('id', identifier)
    if counter is not None and column == 'id':
        projection = counter._row('realm_source', _SOURCE_COLUMNS, identifier)
    else:
        raw = store.conn.execute(f'SELECT {_SOURCE_COLUMNS} FROM realm_source WHERE {column}=?', (value,)).fetchone()
        if raw is None:
            _fail('missing_row')
        projection = dict(raw)
        if counter is not None:
            counter._strings(projection)
    top = _strict_json(projection['top_keys_json'])
    if type(top) is not list or any(type(key) is not str for key in top):
        _fail()
    if _canonical(top).decode('utf-8') != projection['top_keys_json']:
        _fail()
    if projection['chat_line_id'] is not None or projection['game_event_id'] is None:
        _fail()
    try:
        source = StoredSource(projection['id'], projection['source_key'], projection['protocol_version'],
            projection['event_type'], 'game_event', projection['game_event_id'], projection['row_sha256'],
            projection['server_ms'], projection['received_at'], projection['guild'], projection['human'], tuple(top))
    except (ValueError, TypeError):
        _fail()
    row = _live_row(store, source.row_id, policy, counter=counter)
    if (source.event_type != _LIVE[policy][1] or source.server_ms != row['payload']['ts']
            or source.top_keys != tuple(sorted({'v', 'type', *row['payload']}))
            or game_row_binding(row['id'], row['type'], row['payload']) != source.row_sha256):
        _fail('proof_changed')
    projection['top_keys_json'] = top
    return source, projection, row


class _LiveResolver(_Resolver):
    def __init__(self, store, human, *, context=None, origin=None):
        super().__init__(store, human)
        self.context = context
        self.origin = origin

    def _row(self, table, columns, identifier):
        if self.context is not None:
            return self.context._row(table, columns, identifier)
        return super()._row(table, columns, identifier)

    def _node(self, kind, key, projection):
        if self.context is None:
            super()._node(kind, key, projection)
        else:
            scope = self.origin if kind in ('qualification', 'privacy_set') else None
            self.nodes[kind, key] = self.context._node(kind, key, projection, scope=scope)

    def resolve_live(self, source, source_projection, row, policy, qualification):
        snapshot, context = _decode_qualification(qualification)
        if (snapshot.guild, snapshot.human) != (source.guild, source.human):
            _fail()
        output, participants, native, qualified, known = _derive(policy, row['payload'], snapshot, self.human)
        if qualification != _qualification(snapshot, context, participants, native, qualified, known):
            _fail('proof_changed')
        roots = (f'event:{source.row_id}',)
        self._node('game_event', str(source.row_id), row)
        self._node('realm_source', str(source.id), source_projection)
        if self.context is None:  # Origin JSON was already charged by the shared reader.
            self._strings({'qualification': _canonical(qualification).decode('utf-8')})
        self._node('qualification', policy, qualification)
        if self.context is None:  # Batch fact topology is charged once by its reader.
            self._edge(); self._edge(); self._edge()
        text = _live_output(policy, output)
        self._privacy_live(roots, text, output, policy)
        nodes = tuple(sorted(self.nodes.values(), key=lambda node: (node.kind, node.key, node.projection_version)))
        body = _proof_body(roots, nodes, output, policy=policy, assurance=_LIVE[policy][3],
                           sources=(source.id,), qualification=qualification)
        digest = _tagged_hash(b'realm-proof-v1\0', body)
        return PublicProof(policy, _LIVE[policy][3], roots, (source.id,), nodes,
            _canonical(qualification).decode('utf-8'), _canonical(output).decode('utf-8'), digest)

    def _privacy_live(self, roots, text, output, policy):
        if self.context is None:
            rows = self.store.conn.execute(
                f'SELECT {_FACT_COLUMNS} FROM fact WHERE root=? ORDER BY id LIMIT 201', roots).fetchall()
            if len(rows) > 200:
                _fail('graph_limit')
        else:
            rows = self.context.annotations(roots[0])
        projection = []
        for raw in rows:
            row = dict(raw)
            if self.context is None:
                self._strings(row)
            _integer(row['id'], 1, _SQLITE_MAX); _nullable_id(row['about'], _U32)
            _integer(row['holder']); _seconds(row['created'])
            if row['root'] != roots[0] or type(row['secret']) is not int or row['secret'] not in (0, 1):
                _fail()
            _screen(row['text'], self.human)
            if row['secret'] or row['kind'] in ('whisper_scene', 'confession', 'life_news', 'real_name'):
                _fail('private_evidence')
            if row['kind'] not in ('said_about', 'feud_cause', 'noticed'):
                _fail()
            if row['text'] and row['text'] != text:
                if (policy != 'channel_raid_topic_v1'
                        or _channel_raid(row['text'], self.human) != output['raid_id']):
                    _fail('unsupported_text')
            projection.append(row)
        self._node('privacy_set', hashlib.sha256(_canonical(list(roots))).hexdigest(), projection)


def _audit_live(store, observation, human, policy, refs):
    if type(refs) not in (tuple, list) or len(refs) != 1:
        _fail('invalid_input')
    if type(refs[0]) is str and refs[0].startswith(('memory:', 'realm:')):
        _fail('unsupported_policy')
    _reference(refs[0])
    _, _, _, qualification = _observation(store, observation, human, policy)
    resolver = _LiveResolver(store, human)
    source, projection, row = _read_source(store, key=observation.receipt.wire.envelope_sha256,
                                          policy=policy, counter=resolver)
    wire, receipt = observation.receipt.wire, observation.receipt
    if (tuple(refs) != (f'event:{source.row_id}',) or receipt.row_id != source.row_id
            or receipt.row_sha256 != source.row_sha256 or wire.received_at != source.received_at
            or (source.guild, source.human) != (observation.qualification.guild, observation.qualification.human)
            or receipt.backlog):
        _fail('invalid_input')
    return resolver.resolve_live(source, projection, row, policy, qualification)


def _qualification_sets(policy, qualification, snapshot):
    """Structural consistency only; raw-source revalidation is still required."""
    participants = qualification['participants']
    native, qualified = qualification['native_present'], qualification['qualified_cast']
    if policy in _KILLS:
        if (snapshot.speaker_kind != 'none' or participants != native or not native
                or qualified != sorted((set(native) - {snapshot.human}) & set(snapshot.cast))
                or (snapshot.human not in native and len(qualified) < 5)):
            _fail()
    elif native or qualified:
        _fail()
    if policy == 'channel_raid_topic_v1':
        if snapshot.speaker_kind == 'none' or participants != [snapshot.speaker_guid]:
            _fail()
    else:
        if snapshot.speaker_kind != 'none':
            _fail()
        if policy == 'wg_result_v1' and participants:
            _fail()
        if policy in ('pvp_result_v1', 'duel_result_v1') and len(participants) != 2:
            _fail()
        if policy == 'realm_first_achievement_v1' and (
                len(participants) != 1 or participants[0] not in (snapshot.human, *snapshot.cast)):
            _fail()
    known = _knowers(policy, snapshot, participants)
    if qualification['known_by'] != [[guild, known[guild]] for guild in sorted(known)] or not known:
        _fail()


def _validated_proof(proof):
    _checked(proof, PublicProof)
    if any(type(getattr(proof, key)) is not tuple for key in ('roots', 'sources', 'nodes')):
        _fail('invalid_input')
    for node in proof.nodes:
        _checked(node, ProofNode)
    qualification, output = _strict_json(proof.qualification_json), _strict_json(proof.output_fields_json)
    if (_canonical(qualification).decode('utf-8') != proof.qualification_json
            or _canonical(output).decode('utf-8') != proof.output_fields_json):
        _fail('invalid_input')
    if proof.policy == _POLICY:
        _proof_shape(proof)
        kinds = [node.kind for node in proof.nodes]
        if (set(kinds) != {'rumor', 'memory', 'game_event', 'privacy_set'}
                or kinds.count('privacy_set') != 1 or kinds.count('rumor') != 1
                or not any(node.kind == 'memory' and node.key == proof.roots[0][7:] for node in proof.nodes)):
            _fail()
        for node in proof.nodes:
            if node.kind == 'privacy_set':
                _hash(node.key)
            else:
                _reference(('event:' if node.kind == 'game_event' else 'memory:') + node.key)
    elif proof.policy in _LIVE:
        if (proof.assurance != _LIVE[proof.policy][3] or len(proof.sources) != 1
                or len(proof.roots) != 1 or len(proof.nodes) != 4):
            _fail('invalid_input')
        if _reference(proof.roots[0])[0] != 'event':
            _fail('unsupported_policy')
        expected = {('game_event', proof.roots[0][6:]), ('realm_source', str(proof.sources[0])),
                    ('qualification', proof.policy),
                    ('privacy_set', hashlib.sha256(_canonical(list(proof.roots))).hexdigest())}
        if {(node.kind, node.key) for node in proof.nodes} != expected:
            _fail('invalid_input')
        snapshot, _ = _decode_qualification(qualification)
        _qualification_sets(proof.policy, qualification, snapshot)
        body = _canonical(dict(kind='qualification', key=proof.policy, projection_version=1,
                               projection=qualification))
        qualification_hash = hashlib.sha256(b'realm-proof-node-v1\0' + len(body).to_bytes(8, 'big') + body).hexdigest()
        if next(node for node in proof.nodes if node.kind == 'qualification').sha256 != qualification_hash:
            _fail('proof_changed')
        _live_output(proof.policy, output)
        if proof.policy == 'pvp_result_v1' and qualification['participants'] != sorted([output['killer'], output['victim']]):
            _fail()
        if proof.policy == 'duel_result_v1' and qualification['participants'] != sorted([output['winner'], output['loser']]):
            _fail()
        if proof.policy == 'realm_first_achievement_v1' and qualification['participants'] != [output['guid']]:
            _fail()
    else:
        _fail('unsupported_policy')
    body = _proof_body(proof.roots, proof.nodes, output, policy=proof.policy,
                       assurance=proof.assurance, sources=proof.sources, qualification=qualification)
    if proof.sha256 != _tagged_hash(b'realm-proof-v1\0', body):
        _fail('proof_changed')
    return qualification, output


def public_proof_to_json(proof: PublicProof) -> str:
    """Serialize an integrity-checked DTO; it still needs current row validation."""
    try:
        qualification, output = _validated_proof(proof)
        body = _proof_body(proof.roots, proof.nodes, output, policy=proof.policy,
            assurance=proof.assurance, sources=proof.sources, qualification=qualification)
        encoded = _canonical(dict(schema='realm_public_proof_v1', **body, sha256=proof.sha256))
        if len(encoded) > 32768:
            _fail('graph_limit')
        return encoded.decode('utf-8')
    except _Evidence:
        raise ValueError('invalid Realm public proof') from None


def public_proof_from_json(raw: str):
    """Decode structural integrity only; never grants publication authority."""
    try:
        if len(_utf8(raw)) > 32768:
            _fail('graph_limit')
        value = _strict_json(raw)
        _keys(value, {'schema', 'policy', 'assurance', 'roots', 'sources', 'nodes',
                      'qualification', 'output_fields', 'sha256'})
        if value['schema'] != 'realm_public_proof_v1' or _canonical(value).decode('utf-8') != raw:
            _fail('invalid_input')
        if (type(value['roots']) is not list or type(value['sources']) is not list
                or type(value['nodes']) is not list or len(value['nodes']) > 200
                or type(value['qualification']) is not dict or type(value['output_fields']) is not dict):
            _fail('invalid_input')
        nodes = []
        try:
            for node in value['nodes']:
                _keys(node, {'kind', 'key', 'projection_version', 'sha256'})
                nodes.append(ProofNode(**node))
            proof = PublicProof(value['policy'], value['assurance'], tuple(value['roots']),
                tuple(value['sources']), tuple(nodes), _canonical(value['qualification']).decode('utf-8'),
                _canonical(value['output_fields']).decode('utf-8'), value['sha256'])
        except (ValueError, TypeError):
            _fail('invalid_input')
        _validated_proof(proof)
        return proof
    except _Evidence as error:
        return Rejected(error.reason)


def _revalidate_live(store, proof, human):
    qualification, _ = _validated_proof(proof)
    resolver = _LiveResolver(store, human)
    source, projection, row = _read_source(store, identifier=proof.sources[0], policy=proof.policy, counter=resolver)
    if proof.roots != (f'event:{source.row_id}',):
        _fail('invalid_input')
    current = resolver.resolve_live(source, projection, row, proof.policy, qualification)
    if current != proof:
        _fail('proof_changed')
    return current


def _read_public_fact(context, fact_id):
    """The single current-origin path, shared by standalone and graph reads."""
    if fact_id in context.facts:
        return context.facts[fact_id]
    store, human = context.store, context.human
    fact = context._row('realm_fact', 'id,kind,text,source,ref,public,ts', fact_id)
    origin = context.origin(fact_id)
    if (type(fact['public']) is not int or fact['public'] != 1
            or origin['state'] != 'active' or type(origin['policy']) is not str
            or origin['policy'] not in _LIVE or origin['assurance'] != _LIVE[origin['policy']][3]):
        _fail('private_evidence')
    _integer(origin['revision'], 1, _SQLITE_MAX); _integer(origin['source'], 1, _SQLITE_MAX)
    proof = public_proof_from_json(origin['proof_json'])
    if type(proof) is Rejected:
        _fail(proof.reason)
    if (proof.policy != origin['policy'] or proof.assurance != origin['assurance']
            or proof.sources != (origin['source'],) or proof.qualification_json != origin['qualification_json']):
        _fail('proof_changed')
    qualification, output = _validated_proof(proof)
    resolver = _LiveResolver(store, human, context=context, origin=fact_id)
    source, projection, row = _read_source(store, identifier=origin['source'], policy=proof.policy, counter=resolver)
    if (proof.roots != (f'event:{source.row_id}',)
            or origin['producer_key'] != f'event:{source.source_key}:{proof.policy}'):
        _fail('proof_changed')
    current = resolver.resolve_live(source, projection, row, proof.policy, qualification)
    if current != proof:
        _fail('proof_changed')
    text = _live_output(proof.policy, output)
    _screen(text, human)
    expected_source = 'channel' if proof.policy == 'channel_raid_topic_v1' else 'event'
    expected = dict(id=fact_id, kind=_LIVE[proof.policy][2], text=text, source=expected_source,
                    ref=proof.roots[0], public=1, ts=source.server_ms / 1000)
    _seconds(fact['ts'])
    if fact != expected or origin['text_sha256'] != hashlib.sha256(text.encode('utf-8')).hexdigest():
        _fail('proof_changed')
    rows = store.conn.execute('SELECT guild,since,how FROM realm_knows WHERE fact=? ORDER BY guild LIMIT 130', (fact_id,)).fetchall()
    if len(rows) > 129:
        _fail('graph_limit')
    actual = []
    for known in rows:
        context._strings(dict(known))
        _integer(known['guild'], 1); _seconds(known['since'])
        if known['since'] != source.server_ms / 1000:
            _fail('proof_changed')
        actual.append([known['guild'], known['how']])
    if actual != qualification['known_by']:
        _fail('proof_changed')
    privacy = context.privacy((f'realm:fact:{fact_id}',), {f'realm:fact:{fact_id}': {text}})
    node = context._node('realm_fact', str(fact_id), dict(fact,
        revision=origin['revision'], policy=origin['policy'], assurance=origin['assurance'],
        text_sha256=origin['text_sha256'], qualification=qualification, proof_sha256=proof.sha256,
        privacy=_node_metadata(privacy)))
    # Fact -> direct proof nodes + its annotation set; source -> exact event.
    for _ in range(6):
        context._edge()
    try:
        accepted = (FactRow(fact_id, fact['kind'], text, fact['source'], fact['ref'], True,
                           fact['ts'], origin['revision']), proof,
                    RealmFactReadStamp(fact_id, origin['revision'], node.sha256, proof.sha256), output)
    except (ValueError, TypeError):
        _fail()
    context.facts[fact_id] = accepted
    return accepted


def public_fact_in_unit(store, fact_id: int, *, human: HumanIdentity):
    """Read a current active origin; no acceptance is cached across calls."""
    _require_unit(store)
    try:
        _identity(human); _input_id(fact_id)
        fact, proof, _, _ = _read_public_fact(_RealmReader(store, human), fact_id)
        return fact, proof
    except _Evidence as error:
        return Rejected(error.reason)


def public_fact_draft(proof: PublicProof, source: StoredSource) -> FactDraft:
    """Pure derived draft for later atomic writers; no row or current authority."""
    try:
        _checked(source, StoredSource)
        qualification, output = _validated_proof(proof)
        if proof.policy not in _LIVE or proof.sources != (source.id,) or proof.roots != (f'event:{source.row_id}',):
            _fail('invalid_input')
        if (source.row_kind != 'game_event' or source.event_type != _LIVE[proof.policy][1]
                or (source.guild, source.human) != (qualification['snapshot']['guild'], qualification['snapshot']['human'])
                or source.top_keys != tuple(sorted({'v', 'type', *(field.name for field in fields(_LIVE[proof.policy][0]))}))):
            _fail('invalid_input')
        projection = dict(id=source.id, source_key=source.source_key, protocol_version=source.protocol_version,
            event_type=source.event_type, game_event_id=source.row_id, chat_line_id=None,
            row_sha256=source.row_sha256, server_ms=source.server_ms, received_at=source.received_at,
            guild=source.guild, human=source.human, top_keys_json=list(source.top_keys))
        body = _canonical(dict(kind='realm_source', key=str(source.id), projection_version=1, projection=projection))
        digest = hashlib.sha256(b'realm-proof-node-v1\0' + len(body).to_bytes(8, 'big') + body).hexdigest()
        if next(node for node in proof.nodes if node.kind == 'realm_source').sha256 != digest:
            _fail('proof_changed')
        return FactDraft(f'event:{source.source_key}:{proof.policy}', proof.policy,
                         source.server_ms / 1000, proof.policy, tuple(sorted(output.items())), True)
    except _Evidence:
        raise ValueError('invalid Realm public draft') from None


def _node_metadata(node):
    return dict(kind=node.kind, key=node.key, projection_version=node.projection_version,
                sha256=node.sha256)


def _realm_reference(ref):
    raw = _utf8(ref)
    if len(raw) > 128:
        _fail('graph_limit')
    if ref.startswith('realm:'):
        suffix = ref[11:] if ref.startswith('realm:fact:') else ''
        if not re.fullmatch(r'[1-9][0-9]{0,18}', suffix) or int(suffix) > _SQLITE_MAX:
            _fail('invalid_row')
        return 'realm_fact', int(suffix)
    if ref.startswith(('memory:', 'event:')):
        return _reference(ref)
    _fail('unsupported_policy')


class _RealmReader(_Resolver):
    """One synchronous operation's rows, graph and budget; never caller supplied."""
    def __init__(self, store, human):
        super().__init__(store, human)
        self.rows = {}
        self.origins = {}
        self.facts = {}
        self.dependencies = {}
        self.annotation_sets = {}
        self.annotation_ids = set()
        self.closures = {}

    def _row(self, table, columns, identifier):
        key = table, identifier
        if key not in self.rows:
            self.rows[key] = (columns, super()._row(table, columns, identifier))
        prior_columns, row = self.rows[key]
        if prior_columns != columns:
            _fail('proof_changed')
        return dict(row)  # Parsers may replace JSON strings only in their own copy.

    def origin(self, identifier):
        if identifier not in self.origins:
            row = self.store.conn.execute('SELECT fact,producer_key,source,policy,assurance,proof_json,'
                'qualification_json,text_sha256,revision,state FROM realm_fact_origin WHERE fact=?',
                (identifier,)).fetchone()
            if row is None:
                _fail('missing_row')
            row = dict(row)
            self._strings(row)
            self.origins[identifier] = row
        return self.origins[identifier]

    def remember(self, node, *, scope=None):
        key = node.kind, node.key, node.projection_version, scope
        previous = self.nodes.get(key)
        if previous is not None:
            if previous != node:
                _fail('proof_changed')
        else:
            if len(self.nodes) >= 200:
                _fail('graph_limit')
            self.nodes[key] = node
        return node

    def _node(self, kind, key, projection, *, scope=None):
        body = _canonical(dict(kind=kind, key=key, projection_version=1, projection=projection))
        digest = hashlib.sha256(b'realm-proof-node-v1\0' + len(body).to_bytes(8, 'big') + body).hexdigest()
        return self.remember(ProofNode(kind, key, 1, digest), scope=scope)

    def annotations(self, ref):
        if ref not in self.annotation_sets:
            rows = self.store.conn.execute(
                f'SELECT {_FACT_COLUMNS} FROM fact WHERE root=? ORDER BY id LIMIT 201', (ref,)).fetchall()
            if len(rows) > 200:
                _fail('graph_limit')
            result = []
            for raw in rows:
                row = dict(raw)
                _integer(row['id'], 1, _SQLITE_MAX)
                if row['id'] not in self.annotation_ids:
                    self.annotation_ids.add(row['id'])
                    if len(self.annotation_ids) > 200:
                        _fail('graph_limit')
                    self._strings(row)
                result.append(row)
            self.annotation_sets[ref] = tuple(result)
        return self.annotation_sets[ref]

    def privacy(self, refs, support):
        refs = tuple(sorted(refs))
        projection = []
        for ref in refs:
            for row in self.annotations(ref):
                _nullable_id(row['about'], _U32); _integer(row['holder']); _seconds(row['created'])
                if row['root'] != ref or type(row['secret']) is not int or row['secret'] not in (0, 1):
                    _fail()
                _screen(row['text'], self.human)
                if row['secret'] or row['kind'] in ('whisper_scene', 'confession', 'life_news', 'real_name'):
                    _fail('private_evidence')
                if row['kind'] not in ('said_about', 'feud_cause', 'noticed'):
                    _fail()
                if row['text'] and row['text'] not in support[ref]:
                    _fail('unsupported_text')
                projection.append(row)
        projection.sort(key=lambda row: row['id'])
        return self._node('privacy_set', hashlib.sha256(_canonical(list(refs))).hexdigest(), projection)

    def links(self, identifier):
        if identifier not in self.dependencies:
            rows = self.store.conn.execute('SELECT memory,fact FROM realm_memory_dependency '
                'WHERE memory=? ORDER BY fact LIMIT 201', (identifier,)).fetchall()
            if len(rows) > 200:
                _fail('graph_limit')
            links = []
            for row in rows:
                _integer(row['memory'], 1, _SQLITE_MAX); _integer(row['fact'], 1, _SQLITE_MAX)
                if row['memory'] != identifier:
                    _fail()
                links.append((row['memory'], row['fact']))
            self.dependencies[identifier] = tuple(links)
        return self.dependencies[identifier]

    def related(self, identifier):
        row = self._row('memory', _MEMORY_COLUMNS, identifier)
        if self.links(identifier):
            return True
        refs = _strict_json(row['evidence'])
        if type(refs) is not list:
            _fail()
        for ref in refs:
            _utf8(ref)
            if ref.startswith('realm:'):
                _realm_reference(ref)
                return True
        return False

    def _support(self, row, fact_ids):
        for identifier in fact_ids:
            fact, proof, _, output = self.facts[identifier]
            if row['text'] != fact.text or row['t_event'] != int(fact.ts):
                continue
            boss, raid = row['boss_id'], row['raid_id']
            if proof.policy in _KILLS:
                if ((boss is None or boss == output['boss_id'])
                        and (raid is None or raid == output['map'])):
                    return
            elif proof.policy == 'channel_raid_topic_v1':
                if boss is None and (raid is None or raid == output['raid_id']):
                    return
            elif boss is None and raid is None:
                return
        _fail('unsupported_text')

    def walk(self, ref, depth=0):
        if depth > 32:
            _fail('graph_limit')
        kind, identifier = _realm_reference(ref)
        if kind == 'realm_fact':
            if depth + 2 > 32:
                _fail('graph_limit')
            _read_public_fact(self, identifier)
            return frozenset(), frozenset((identifier,)), 2
        if kind != 'memory':
            _fail('invalid_input')
        if self.states.get(ref) == 'visiting':
            _fail('cycle')
        if self.states.get(ref) == 'done':
            memories, facts, height = self.closures[identifier]
            if depth + height > 32:
                _fail('graph_limit')
            return memories, facts, height
        self.states[ref] = 'visiting'
        refs = self._memory(identifier, realm=True)
        links = self.links(identifier)
        if not refs and not links:
            _fail('unsupported_policy')
        memories, facts, events, height = {identifier}, set(), [], 0
        children = list(refs) + [f'realm:fact:{fact}' for _, fact in links]
        for child in children:
            self._edge()  # Declared duplicates still count, even with cached targets.
            child_kind, _ = _realm_reference(child)
            if child_kind == 'event':
                if depth + 1 > 32:
                    _fail('graph_limit')
                events.append(child)
                height = max(height, 1)
                continue
            child_memories, child_facts, child_height = self.walk(child, depth + 1)
            memories.update(child_memories); facts.update(child_facts)
            height = max(height, child_height + 1)
        # Resolve event leaves after this node's own complete fact closure. Sibling
        # roots and evidence ordering cannot provide or conceal authority.
        backed_events = {self.facts[fact][0].ref for fact in facts}
        if any(event not in backed_events for event in events):
            _fail('unsupported_policy')
        self._support(self.memories[identifier], facts)
        self.states[ref] = 'done'
        result = frozenset(memories), frozenset(facts), height
        self.closures[identifier] = result
        return result

    def stamp(self, identifier):
        memories, facts, _ = self.closures[identifier]
        support = {f'memory:{memory}': {self.facts[fact][0].text for fact in self.closures[memory][1]}
                   for memory in memories}
        support.update({f'realm:fact:{fact}': {self.facts[fact][0].text} for fact in facts})
        privacy = self.privacy(tuple(support), support)
        body = dict(schema='realm_memory_read_v1', root=f'memory:{identifier}',
            memories=[dict(memory_id=memory, sha256=self.nodes['memory', str(memory), 1, None].sha256)
                      for memory in sorted(memories)],
            facts=[dict(fact_id=fact, revision=self.facts[fact][2].revision,
                        fact_sha256=self.facts[fact][2].fact_sha256,
                        proof_sha256=self.facts[fact][2].proof_sha256) for fact in sorted(facts)],
            dependency_rows=sorted(link for memory in memories for link in self.links(memory)),
            privacy=_node_metadata(privacy))
        return RealmReadStamp(identifier, self.nodes['memory', str(identifier), 1, None].sha256,
                              tuple(sorted(facts)), _tagged_hash(b'realm-memory-read-v1\0', body))


def audit_realm_reads_in_unit(store, refs: tuple[str, ...], *, human: HumanIdentity):
    """Audit all classified roots atomically under one internal complete budget."""
    _require_unit(store)
    try:
        _identity(human)
        if type(refs) is not tuple or len(refs) > 256:
            _fail('invalid_input')
        if any(type(ref) is not str for ref in refs) or refs != tuple(sorted(set(refs))):
            _fail('invalid_input')
        if sum(len(_utf8(ref)) for ref in refs) > 32768:
            _fail('graph_limit')
        reader = _RealmReader(store, human)
        roots = []
        for ref in refs:
            kind, identifier = _realm_reference(ref)
            if kind == 'memory':
                if not reader.related(identifier):
                    _fail('invalid_input')
                roots.append(identifier)
            elif kind != 'realm_fact':
                _fail('invalid_input')
            reader.walk(ref)
        stamps = tuple(reader.stamp(identifier) for identifier in sorted(roots))
        fact_stamps = tuple(reader.facts[identifier][2] for identifier in sorted(reader.facts))
        return stamps, fact_stamps
    except _Evidence as error:
        return Rejected(error.reason)
