"""Read-only public proofs for historical raid-topic rumors.

A proof is a snapshot of actual rows, never authority supplied by a DTO or a
caller digest. Callers own the Store unit and the current privacy identity.
Live policies and publication/knowledge effects are deliberately unsupported.
"""
from __future__ import annotations

import hashlib
import json
import math
import re

from leeroy_mind.human import HumanIdentity
from leeroy_mind.personas.screens import ROMANCE_RE, names_real_name
from leeroy_mind.storage.store import Store
from leeroy_mind.wow import BOSS_NAMES, is_raid_fight

from .records import ForumCandidate, ProofNode, PublicProof, Rejected

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
    """Render the fixed topic template. Rendering alone grants no authority."""
    try:
        if type(policy) is not str or policy != _POLICY:
            _fail("unsupported_policy")
        text = _output(output_fields)
        if len(text) > 200:
            _fail()
        return text
    except _Evidence:
        raise ValueError("invalid Realm public rendering") from None


def _proof_body(roots, nodes, output):
    return dict(policy=_POLICY, assurance="stored_topic", roots=roots, sources=(),
                nodes=[dict(kind=n.kind, key=n.key, projection_version=n.projection_version,
                            sha256=n.sha256) for n in nodes], qualification={}, output_fields=output)


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

    def _memory(self, identifier):
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
        if type(refs) is not list or not refs:
            _fail()
        if len(refs) > 64:
            _fail("graph_limit")
        for ref in refs:
            _reference(ref)
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
    """Audit one complete stored rumor graph in the caller's actual Store unit."""
    _require_unit(store)
    try:
        _identity(human)
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
        rumor_id = _proof_shape(proof)
        current = _Resolver(store, human).resolve(rumor_id, proof.roots)[0]
        if current != proof:
            _fail("proof_changed")
        return current
    except _Evidence as error:
        return Rejected(error.reason)
