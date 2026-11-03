"""Actual-row, disposable-Store coverage for the historical topic proof boundary."""
from dataclasses import FrozenInstanceError, replace
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from leeroy_mind.human import HumanIdentity
from leeroy_mind.realm.provenance import (
    audit_public_in_unit, render_public, revalidate_public_in_unit,
    stored_forum_candidate_in_unit,
)
from leeroy_mind.realm.records import ForumCandidate, ProofNode, PublicProof, Rejected
from leeroy_mind.storage.store import Store


POLICY = "stored_forum_topic_v1"
BOSS = "Lord Marrowgar"
TEXT = f"We killed {BOSS}."
HUMAN = HumanIdentity(10, "Testmaster", "Testmaster", "Synthetic Private", True)
FIELDS = {"boss_id": 0, "catalogue": "raid_catalogue_v1", "map": 631}
MEMORY_COLUMNS = ("id", "owner", "kind", "text", "t_event", "t_created", "actors", "place",
                  "boss_id", "raid_id", "importance", "valence", "source", "evidence",
                  "confidence", "status", "superseded_by", "key", "scope", "scope_with")


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def insert(store, table, **values):
    return store.conn.execute(
        f"INSERT INTO {table}({','.join(values)}) VALUES ({','.join('?' for _ in values)})",
        tuple(values.values())).lastrowid


def event(store, identifier=1, *, payload=None, raw=None, **changes):
    data = dict(ts=1000000, map=631, instance_id=9, boss_id=0, boss_name="", state="killed", members=[20, 21])
    if payload:
        data.update(payload)
    values = dict(id=identifier, ts=1000.0, type="boss", guid=None,
                  payload=json.dumps(data) if raw is None else raw, session_id=None)
    values.update(changes)
    return insert(store, "game_event", **values)


def memory(store, identifier=1, **changes):
    values = dict(id=identifier, owner=20, kind="event", text=TEXT, t_event=1000, t_created=1000,
        t_accessed=1000, actors="[20,21]", place=None, boss_id=0, raid_id=631, importance=5,
        valence=0.0, source="game", evidence='["event:1"]', confidence=1.0,
        status="active", superseded_by=None, key=None, scope="open", scope_with=0)
    values.update(changes)
    return insert(store, "memory", **values)


def rumor(store, identifier=1, **changes):
    values = dict(id=identifier, root="event:1", about=20, canonical=TEXT, source=1,
                  status="live", created=1000.0, last_told=1000.0)
    values.update(changes)
    return insert(store, "rumor", **values)


def annotation(store, **changes):
    values = dict(kind="noticed", root="event:1", about=20, text="", secret=0,
                  holder=0, created=1001.0)
    values.update(changes)
    return insert(store, "fact", **values)


def update(store, table, identifier=1, **values):
    store.conn.execute(f"UPDATE {table} SET {','.join(k + '=?' for k in values)} WHERE id=?",
                       (*values.values(), identifier))


@pytest.fixture
def graph(store):
    with store.transaction():
        event(store)
        memory(store)
        rumor(store)
    return store


def audit(store, *, human=HUMAN, **changes):
    values = dict(human=human, policy=POLICY, refs=("memory:1",), rumor_id=1)
    values.update(changes)
    return audit_public_in_unit(store, **values)


def accepted(store):
    proof = audit(store)
    assert type(proof) is PublicProof, proof
    return proof


def rejected(value, reason=None):
    assert type(value) is Rejected, value
    if reason is not None:
        assert value.reason == reason
    assert tuple(value.__slots__) == ("reason",)
    assert HUMAN.real_name not in repr(value)


def test_actual_legacy_candidate_is_read_only_and_has_no_live_source(graph):
    with graph.transaction():
        before = graph.conn.total_changes
        statements = []
        graph.conn.set_trace_callback(statements.append)
        try:
            candidate = stored_forum_candidate_in_unit(graph, 1, human=HUMAN)
            proof = accepted(graph)
            assert revalidate_public_in_unit(graph, proof, human=HUMAN) == proof
        finally:
            graph.conn.set_trace_callback(None)
        assert candidate == ForumCandidate(1, 1, "event:1", 20,
            f"A rumor concerns the raid encounter with {BOSS}.", proof)
        assert proof.policy == POLICY and proof.assurance == "stored_topic"
        assert proof.roots == ("memory:1",) and proof.sources == ()
        assert proof.qualification_json == "{}"
        assert proof.output_fields_json == canonical(FIELDS).decode()
        assert {n.kind for n in proof.nodes} == {"rumor", "memory", "game_event", "privacy_set"}
        assert len(proof.nodes) == 4
        assert all(s.lstrip().upper().startswith("SELECT ") for s in statements)
        assert graph.conn.total_changes == before
        assert graph.conn.execute("SELECT count(*) FROM realm_source").fetchone()[0] == 0
        assert graph.conn.execute("SELECT count(*) FROM realm_kill").fetchone()[0] == 0
        assert HUMAN.real_name not in repr(proof)
        assert TEXT not in repr(proof)


@pytest.mark.parametrize("text", [
    f"We killed {BOSS}.", f"We wiped on {BOSS} tonight.",
    f"We wiped 2× on {BOSS} tonight.", f"We wiped 999× on {BOSS} tonight.",
    f"We wiped once on {BOSS}, then killed it.",
    f"We wiped 2× on {BOSS}, then killed it.",
    f"We wiped 999× on {BOSS}, then killed it.", f"  We killed {BOSS}.  ",
])
def test_finite_grammar_and_legal_count_boundaries(graph, text):
    with graph.transaction():
        update(graph, "memory", text=text)
        update(graph, "rumor", canonical=text)
        accepted(graph)


@pytest.mark.parametrize("text", [
    f"We wiped 1× on {BOSS} tonight.", f"We wiped 0× on {BOSS} tonight.",
    f"We wiped 02× on {BOSS} tonight.", f"We wiped 1000× on {BOSS} tonight.",
    f"We wiped 2x on {BOSS} tonight.", f"we killed {BOSS}.",
    f"We killed {BOSS}. extra", f"I died first on {BOSS}.",
    f"|cffffffffWe killed {BOSS}.|r", f"\t{TEXT}", f"{TEXT}\n", f"{TEXT}\u200b",
    f"{TEXT}\x00", f"We killed {BOSS}!", f"We killed Rotface.",
])
@pytest.mark.parametrize("table,field", [("memory", "text"), ("rumor", "canonical")])
def test_whole_string_grammar_rejects_every_altered_form(graph, text, table, field):
    with graph.transaction():
        update(graph, table, **{field: text})
        rejected(audit(graph), "unsupported_text")


def test_complete_dag_old_canonical_and_multiple_instances(graph):
    with graph.transaction():
        event(graph, 2, payload={"instance_id": 99, "state": "wiped"})
        memory(graph, 2, evidence='["event:1","event:2"]', kind="reflection", source="reflection",
               boss_id=None, raid_id=None, status="superseded")
        memory(graph, 3, evidence='["memory:2","event:2"]', kind="conversation", source="hearsay")
        update(graph, "memory", evidence='["memory:2","memory:3"]',
               text=f"We wiped 3× on {BOSS}, then killed it.", status="superseded", superseded_by=3)
        proof = accepted(graph)
        assert len(proof.nodes) == 7
        assert revalidate_public_in_unit(graph, proof, human=HUMAN) == proof
        assert graph.conn.execute("SELECT canonical FROM rumor").fetchone()[0] == TEXT


@pytest.mark.parametrize("kind,source", [
    ("observation", "chat"), ("conversation", "extract"), ("event", "offscreen"),
    ("reflection", "reflection"), ("plan", "hearsay"),
])
def test_typed_intermediate_copies_need_real_complete_ancestry(graph, kind, source):
    with graph.transaction():
        memory(graph, 2, kind=kind, source=source)
        update(graph, "memory", evidence='["memory:2"]')
        accepted(graph)
        update(graph, "memory", 2, evidence='[]')
        rejected(audit(graph))


@pytest.mark.parametrize("table", ["memory", "rumor", "game_event"])
def test_missing_rows_fail_closed(graph, table):
    with graph.transaction():
        graph.conn.execute(f"DELETE FROM {table} WHERE id=1")
        rejected(audit(graph), "missing_row")


@pytest.mark.parametrize("table,changes", [
    ("rumor", {"status": "faded"}), ("rumor", {"source": 0}), ("rumor", {"about": 99}),
    ("rumor", {"root": "event:2"}), ("rumor", {"source": 2}),
    ("memory", {"kind": "reflection"}), ("memory", {"source": "hearsay"}),
    ("memory", {"boss_id": None}), ("memory", {"raid_id": None}),
    ("memory", {"boss_id": 1}), ("memory", {"raid_id": 603}),
    ("memory", {"scope": "private"}), ("memory", {"scope": "secret"}),
    ("memory", {"scope_with": 20}), ("memory", {"status": "archived"}),
    ("memory", {"status": "rejected"}), ("memory", {"actors": "[]"}),
    ("memory", {"actors": "[20,20]"}), ("memory", {"actors": "[true]"}),
    ("memory", {"actors": "{}"}), ("memory", {"evidence": "[]"}),
    ("memory", {"evidence": "{}"}), ("game_event", {"type": "death"}),
    ("game_event", {"ts": -1}),
])
def test_invalid_stored_relationships_and_metadata(graph, table, changes):
    with graph.transaction():
        update(graph, table, **changes)
        rejected(audit(graph))


@pytest.mark.parametrize("ref", [
    "event:01", "memory:+1", "event:0", "event:-1", "event: 1", "event:1\n",
    "event:9223372036854775808", "realm:fact:1", "chat:1", "life:1", "owner:20",
    "stance:20", "attraction:20", "rule:real_name", "narrative:1", "encounter:1", "story:1", "session:1",
])
def test_only_canonical_complete_boss_ancestry_is_supported(graph, ref):
    with graph.transaction():
        update(graph, "memory", evidence=json.dumps([ref]))
        rejected(audit(graph))


@pytest.mark.parametrize("changes", [
    {"kind": "story"}, {"source": "owner"}, {"scope": "private"},
    {"scope_with": 21}, {"status": "archived"}, {"boss_id": 1}, {"raid_id": 603},
    {"evidence": '["event:404"]'}, {"evidence": '["memory:404"]'},
])
def test_no_bad_secondary_branch_can_hide_behind_valid_primary(graph, changes):
    with graph.transaction():
        memory(graph, 2, **changes)
        update(graph, "memory", evidence='["event:1","memory:2"]')
        rejected(audit(graph))


@pytest.mark.parametrize("payload", [
    {"map": 1}, {"boss_id": 999}, {"boss_name": "supplied fallback"},
    {"ts": 0}, {"ts": -1}, {"ts": 253402300800000}, {"ts": True}, {"ts": 1.0},
    {"map": True}, {"boss_id": False}, {"instance_id": 0}, {"instance_id": 1.0},
    {"state": "dead"}, {"members": [20, 20]}, {"members": [True]},
    {"members": [0]}, {"members": [0, 20]}, {"members": [-1]},
    {"members": [4294967296]}, {"members": list(range(1, 130))},
    {"members": "20"}, {"boss_name": None}, {"boss_name": "\ud800"},
])
def test_exact_boss_payload_types_and_catalogue(graph, payload):
    with graph.transaction():
        data = json.loads(graph.conn.execute("SELECT payload FROM game_event WHERE id=1").fetchone()[0])
        data.update(payload)
        update(graph, "game_event", payload=json.dumps(data))
        rejected(audit(graph))


@pytest.mark.parametrize("raw", [
    "{}", "[]", "null", "{bad", '{"ts":1,"ts":2}',
    '{"ts":NaN}', '{"ts":Infinity}', '{"ts":1e999}',
])
def test_strict_payload_json(graph, raw):
    with graph.transaction():
        update(graph, "game_event", payload=raw)
        rejected(audit(graph))


@pytest.mark.parametrize("mutation", ["extra", "missing", "duplicate"])
def test_even_otherwise_valid_payload_cannot_smuggle_shape_changes(graph, mutation):
    with graph.transaction():
        raw = graph.conn.execute("SELECT payload FROM game_event WHERE id=1").fetchone()[0]
        data = json.loads(raw)
        if mutation == "extra":
            data["private"] = "anything"
            raw = json.dumps(data)
        elif mutation == "missing":
            del data["boss_name"]
            raw = json.dumps(data)
        else:
            raw = raw[:-1] + ',"state":"killed"}'
        update(graph, "game_event", payload=raw)
        rejected(audit(graph), "invalid_row")


@pytest.mark.parametrize("second", [{"map": 631, "boss_id": 1}, {"map": 603, "boss_id": 0}])
def test_mixed_catalogued_bosses_reject(graph, second):
    with graph.transaction():
        event(graph, 2, payload=second)
        update(graph, "memory", evidence='["event:1","event:2"]')
        rejected(audit(graph), "topic_mismatch")


@pytest.mark.parametrize("surface", ["memory", "rumor", "boss_name", "annotation"])
@pytest.mark.parametrize("name", ["sYnThEtIc", "Synthetic", "SYNTHETIC", "SYnThEtIc"])
def test_raw_private_name_screen_precedes_cleaning_and_topic_checks(graph, surface, name):
    human = replace(HUMAN, real_name="sYnThEtIc")
    raw = f"|cffff0000 {name}|r {TEXT}"
    with graph.transaction():
        if surface == "memory":
            update(graph, "memory", text=raw)
        elif surface == "rumor":
            update(graph, "rumor", canonical=raw)
        elif surface == "boss_name":
            payload = json.loads(graph.conn.execute("SELECT payload FROM game_event").fetchone()[0])
            payload["boss_name"] = raw
            update(graph, "game_event", payload=json.dumps(payload))
        else:
            annotation(graph, text=raw)
        rejected(audit(graph, human=human), "real_name")


def test_ordinary_lowercase_word_and_current_identity_semantics(graph):
    with graph.transaction():
        # "on" is a real-name spelling here; lowercase prose keeps its ordinary meaning.
        update(graph, "memory", text=f"We wiped on {BOSS} tonight.")
        update(graph, "rumor", canonical=f"We wiped on {BOSS} tonight.")
        proof = audit(graph, human=replace(HUMAN, real_name="On"))
        assert type(proof) is PublicProof
        rejected(revalidate_public_in_unit(graph, proof, human=replace(HUMAN, real_name="Marrowgar")), "real_name")
        assert revalidate_public_in_unit(graph, proof, human=replace(HUMAN, real_name="")) == proof


def test_caseless_real_name_is_screened(graph):
    with graph.transaction():
        update(graph, "rumor", canonical=TEXT + " 测试")
        rejected(audit(graph, human=replace(HUMAN, real_name="测试")), "real_name")


@pytest.mark.parametrize("surface", ["memory", "rumor", "boss_name", "annotation"])
def test_romance_screen_covers_every_raw_prose_surface(graph, surface):
    with graph.transaction():
        text = "a crush on someone " + TEXT
        if surface == "memory":
            update(graph, "memory", text=text)
        elif surface == "rumor":
            update(graph, "rumor", canonical=text)
        elif surface == "boss_name":
            payload = json.loads(graph.conn.execute("SELECT payload FROM game_event").fetchone()[0])
            payload["boss_name"] = text
            update(graph, "game_event", payload=json.dumps(payload))
        else:
            annotation(graph, text=text)
        rejected(audit(graph), "romance")


@pytest.mark.parametrize("kind", ["whisper_scene", "confession", "life_news", "real_name"])
@pytest.mark.parametrize("root", ["event:1", "memory:1"])
def test_every_private_annotation_kind_blocks_even_empty_text(graph, kind, root):
    with graph.transaction():
        annotation(graph, kind=kind, root=root)
        rejected(audit(graph), "private_evidence")


@pytest.mark.parametrize("kind", ["said_about", "feud_cause", "noticed"])
@pytest.mark.parametrize("text", ["", TEXT])
def test_public_annotation_holder_is_not_automatically_private(graph, kind, text):
    with graph.transaction():
        annotation(graph, kind=kind, text=text, holder=77)
        accepted(graph)


@pytest.mark.parametrize("changes", [{"secret": 1}, {"text": "unreviewed free prose"}, {"text": " "}])
def test_other_annotation_prohibitions(graph, changes):
    with graph.transaction():
        annotation(graph, **changes)
        rejected(audit(graph))


@pytest.mark.parametrize("root", ["event:1", "memory:1", "memory:2"])
def test_empty_privacy_set_and_every_reached_branch_revalidate(graph, root):
    with graph.transaction():
        memory(graph, 2)
        update(graph, "memory", evidence='["event:1","memory:2"]')
        proof = accepted(graph)
        annotation(graph, root=root)
        changed = accepted(graph)
        assert changed != proof
        rejected(revalidate_public_in_unit(graph, proof, human=HUMAN), "proof_changed")
        annotation(graph, root=root, kind="real_name")
        rejected(revalidate_public_in_unit(graph, changed, human=HUMAN), "private_evidence")


@pytest.mark.parametrize("table,changes", [
    ("rumor", {"last_told": 1001.0}), ("rumor", {"created": 1001.0}),
    ("rumor", {"canonical": f"We wiped on {BOSS} tonight."}),
    ("memory", {"text": f"We wiped 2× on {BOSS} tonight."}),
    ("memory", {"t_created": 1001}), ("memory", {"t_event": 1001}),
    ("memory", {"place": "Icecrown"}), ("memory", {"importance": 6}),
    ("memory", {"valence": 0.5}), ("memory", {"confidence": 0.5}),
    ("memory", {"key": "boss:631:0:2026-11-03"}),
    ("memory", {"actors": "[21,20]"}), ("memory", {"status": "superseded"}),
    ("memory", {"evidence": '["event:1","event:1"]'}), ("game_event", {"ts": 1001.0}),
    ("game_event", {"guid": 20}),
])
def test_even_valid_relevant_row_changes_are_new_selection_decisions(graph, table, changes):
    with graph.transaction():
        proof = accepted(graph)
        update(graph, table, **changes)
        changed = accepted(graph)
        assert changed != proof
        rejected(revalidate_public_in_unit(graph, proof, human=HUMAN), "proof_changed")


def test_event_full_payload_hash_and_harmless_bookkeeping(graph):
    with graph.transaction():
        proof = accepted(graph)
        update(graph, "memory", t_accessed=9999, access_n=90, merged_n=20, embedding=b"unused vector")
        assert audit(graph) == proof
        assert revalidate_public_in_unit(graph, proof, human=HUMAN) == proof
        raw = json.loads(graph.conn.execute("SELECT payload FROM game_event").fetchone()[0])
        raw["instance_id"] = 123
        update(graph, "game_event", payload=json.dumps(raw))
        assert accepted(graph) != proof
        rejected(revalidate_public_in_unit(graph, proof, human=HUMAN), "proof_changed")


def test_node_and_whole_proof_hash_framing_and_projection_bytes(graph):
    with graph.transaction():
        proof = accepted(graph)
        projections = {
            ("game_event", "1"): dict(graph.conn.execute("SELECT id,ts,type,guid,payload,session_id FROM game_event").fetchone()),
            ("memory", "1"): dict(graph.conn.execute(f"SELECT {','.join(MEMORY_COLUMNS)} FROM memory").fetchone()),
            ("rumor", "1"): dict(graph.conn.execute("SELECT id,root,about,canonical,source,status,created,last_told FROM rumor").fetchone()),
        }
        projections["game_event", "1"]["payload"] = json.loads(projections["game_event", "1"]["payload"])
        for name in ("actors", "evidence"):
            projections["memory", "1"][name] = json.loads(projections["memory", "1"][name])
        privacy_key = hashlib.sha256(canonical(["event:1", "memory:1"])).hexdigest()
        projections["privacy_set", privacy_key] = []
        for node in proof.nodes:
            body = canonical(dict(kind=node.kind, key=node.key, projection_version=1,
                                  projection=projections[node.kind, node.key]))
            assert node.sha256 == hashlib.sha256(b"realm-proof-node-v1\0" + len(body).to_bytes(8, "big") + body).hexdigest()
        body = dict(policy=POLICY, assurance="stored_topic", roots=["memory:1"], sources=[],
            nodes=[dict(kind=n.kind, key=n.key, projection_version=1, sha256=n.sha256) for n in proof.nodes],
            qualification={}, output_fields=FIELDS)
        assert proof.sha256 == hashlib.sha256(b"realm-proof-v1\0" + canonical(body)).hexdigest()
        # JSON whitespace/key order is representation noise; array order is source content.
        raw = projections["game_event", "1"]["payload"]
        update(graph, "game_event", payload=json.dumps(raw, sort_keys=True, indent=2))
        assert audit(graph) == proof


@pytest.mark.parametrize("changes", [
    {"assurance": "verified_event"}, {"qualification_json": '{"extra":1}'},
    {"qualification_json": '{"x":1,"x":2}'}, {"output_fields_json": '{"boss_id":0,"catalogue":"raid_catalogue_v1","map":631,"x":1}'},
    {"output_fields_json": '{"boss_id":0,"catalogue":"raid_catalogue_v1","map":631,"map":631}'},
    {"output_fields_json": '{ "boss_id":0,"catalogue":"raid_catalogue_v1","map":631}'},
    {"sources": (1,)}, {"roots": ("memory:2",)}, {"sha256": "a" * 64},
])
def test_forged_proof_semantics_are_rejected(graph, changes):
    with graph.transaction():
        proof = replace(accepted(graph), **changes)
        rejected(revalidate_public_in_unit(graph, proof, human=HUMAN))


def test_recomputed_digest_does_not_authorize_forged_node(graph):
    with graph.transaction():
        proof = accepted(graph)
        nodes = tuple(replace(n, sha256="a" * 64) if n.kind == "memory" else n for n in proof.nodes)
        body = dict(policy=POLICY, assurance="stored_topic", roots=proof.roots, sources=proof.sources,
            nodes=[dict(kind=n.kind, key=n.key, projection_version=1, sha256=n.sha256) for n in nodes],
            qualification={}, output_fields=FIELDS)
        forged = replace(proof, nodes=nodes, sha256=hashlib.sha256(b"realm-proof-v1\0" + canonical(body)).hexdigest())
        rejected(revalidate_public_in_unit(graph, forged, human=HUMAN), "proof_changed")


@pytest.mark.parametrize("mutation", ["absent_rumor", "multiple_rumors", "invalid_node", "unsorted", "version"])
def test_revalidation_rechecks_bounded_dto_shapes(graph, mutation):
    with graph.transaction():
        proof = accepted(graph)
        if mutation == "absent_rumor":
            nodes = tuple(n for n in proof.nodes if n.kind != "rumor")
        elif mutation == "multiple_rumors":
            nodes = tuple(sorted((*proof.nodes, ProofNode("rumor", "2", 1, "a" * 64)), key=lambda n: (n.kind, n.key, n.projection_version)))
        elif mutation == "invalid_node":
            nodes = proof.nodes
            object.__setattr__(nodes[-1], "key", 99)
        elif mutation == "version":
            nodes = proof.nodes
            object.__setattr__(nodes[-1], "projection_version", 2)
        else:
            nodes = proof.nodes[::-1]
        object.__setattr__(proof, "nodes", nodes)
        rejected(revalidate_public_in_unit(graph, proof, human=HUMAN), "invalid_input")


def test_cycle_and_depth_limits_do_not_accept_valid_prefix(graph):
    with graph.transaction():
        memory(graph, 2, evidence='["memory:1"]')
        update(graph, "memory", evidence='["event:1","memory:2"]')
        rejected(audit(graph), "cycle")
        update(graph, "memory", 2, evidence='["event:1"]')
        accepted(graph)
        # source depth 1, memory 31 at depth 31, event at depth 32 is legal.
        for identifier in range(3, 32):
            memory(graph, identifier, evidence=json.dumps([f"memory:{identifier - 1}"]))
        update(graph, "memory", evidence='["memory:31"]')
        accepted(graph)
        memory(graph, 32, evidence='["memory:31"]')
        update(graph, "memory", evidence='["memory:32"]')
        rejected(audit(graph), "graph_limit")


def test_memoized_dag_still_checks_longest_path_depth(graph):
    with graph.transaction():
        for identifier in range(2, 33):
            memory(graph, identifier, evidence=json.dumps([f"memory:{identifier - 1}" if identifier > 2 else "event:1"]))
        update(graph, "memory", evidence='["event:1","memory:32"]')
        rejected(audit(graph), "graph_limit")


@pytest.mark.parametrize("refs", [["event:1"] * 65, ["event:" + "1" * 123]])
def test_per_node_ref_and_byte_limits_reject_whole_graph(graph, refs):
    with graph.transaction():
        update(graph, "memory", evidence=json.dumps(refs))
        rejected(audit(graph), "graph_limit")


def test_duplicate_edges_consume_complete_work_budget(graph):
    with graph.transaction():
        for identifier in range(2, 14):
            memory(graph, identifier, evidence=json.dumps(["event:1"] * 64))
        update(graph, "memory", evidence=json.dumps(["event:1"] * 19 + [f"memory:{i}" for i in range(2, 14)]))
        accepted(graph)  # 1 rumor edge + 31 source edges + 768 child edges = 800.
        update(graph, "memory", evidence=json.dumps(["event:1"] * 20 + [f"memory:{i}" for i in range(2, 14)]))
        rejected(audit(graph), "graph_limit")


def test_node_limit_includes_rumor_and_empty_privacy_set(graph):
    with graph.transaction():
        for identifier in range(2, 198):
            event(graph, identifier)
        memory(graph, 2, evidence=json.dumps([f"event:{i}" for i in range(65, 129)]))
        memory(graph, 3, evidence=json.dumps([f"event:{i}" for i in range(129, 193)]))
        memory(graph, 4, evidence=json.dumps([f"event:{i}" for i in range(193, 195)]))
        update(graph, "memory", evidence=json.dumps([f"event:{i}" for i in range(1, 62)] + ["memory:2", "memory:3", "memory:4"]))
        # 191 events + 4 memories + rumor + privacy_set = 197 nodes.
        proof = accepted(graph)
        assert len(proof.nodes) == 197
        update(graph, "memory", 4, evidence=json.dumps([f"event:{i}" for i in range(193, 198)]))
        proof = accepted(graph)
        assert len(proof.nodes) == 200
        event(graph, 198)
        update(graph, "memory", 4, evidence=json.dumps([f"event:{i}" for i in range(193, 199)]))
        rejected(audit(graph), "graph_limit")


def test_raw_string_budget_checks_metadata_before_parsing(graph):
    with graph.transaction():
        rows = [
            graph.conn.execute(f"SELECT {','.join(MEMORY_COLUMNS)} FROM memory").fetchone(),
            graph.conn.execute("SELECT id,ts,type,guid,payload,session_id FROM game_event").fetchone(),
            graph.conn.execute("SELECT id,root,about,canonical,source,status,created,last_told FROM rumor").fetchone(),
        ]
        used = sum(len(value.encode("utf-8")) for row in rows for value in row if type(value) is str)
        update(graph, "memory", key="x" * (65536 - used))
        accepted(graph)
        update(graph, "memory", key="x" * (65537 - used))
        rejected(audit(graph), "graph_limit")


def test_annotation_row_bound_is_complete_not_a_prefix(graph):
    with graph.transaction():
        for holder in range(200):
            annotation(graph, holder=holder)
        accepted(graph)
        annotation(graph, holder=200)
        rejected(audit(graph), "graph_limit")


@pytest.mark.parametrize("policy", [
    "verified_raid_kill_v1", "bg_result_v1", "arena_result_v1", "wg_result_v1",
    "pvp_result_v1", "channel_raid_topic_v1", "unknown",
])
def test_all_live_and_unknown_policies_are_explicitly_unsupported(graph, policy):
    with graph.transaction():
        rejected(audit(graph, policy=policy), "unsupported_policy")
        with pytest.raises(ValueError, match="^invalid Realm public rendering$"):
            render_public(policy, FIELDS)


@pytest.mark.parametrize("changes", [
    {"rumor_id": 0}, {"rumor_id": True}, {"rumor_id": "1"}, {"refs": ()},
    {"refs": ("memory:1", "event:1")}, {"refs": ("memory:01",)}, {"refs": ("event:1",)},
    {"refs": ("memory:2",)}, {"refs": "memory:1"}, {"observation": object()},
    {"human": None}, {"human": object()},
])
def test_entry_contract_does_not_guess_missing_authority(graph, changes):
    with graph.transaction():
        rejected(audit(graph, **changes), "invalid_input")


@pytest.mark.parametrize("fields", [
    {}, {**FIELDS, "extra": True}, {**FIELDS, "boss_id": False}, {**FIELDS, "map": "631"},
    {**FIELDS, "catalogue": "v2"}, {**FIELDS, "boss_id": 999}, tuple(FIELDS.items()),
])
def test_render_is_strict_and_has_one_fixed_error(fields):
    with pytest.raises(ValueError, match="^invalid Realm public rendering$"):
        render_public(POLICY, fields)


def test_rejected_is_finite_immutable_and_has_no_arbitrary_diagnostics():
    with pytest.raises(ValueError):
        Rejected("arbitrary private detail")
    with pytest.raises(ValueError):
        Rejected(1)
    with pytest.raises(FrozenInstanceError):
        Rejected("invalid_row").reason = "real_name"


def test_every_in_unit_api_rejects_before_sql_and_actual_unit_ownership(graph, clock):
    statements = []
    graph.conn.set_trace_callback(statements.append)
    try:
        for call in (
            lambda: audit(graph),
            lambda: stored_forum_candidate_in_unit(graph, 1, human=HUMAN),
            lambda: revalidate_public_in_unit(graph, None, human=HUMAN),
        ):
            with pytest.raises(RuntimeError, match=r"^Realm public proof requires Store.transaction\(\)$"):
                call()
        assert statements == []
        with graph.transaction():
            peer = Store(graph.conn, clock=clock)
            with pytest.raises(RuntimeError):
                audit(peer)
            with graph.transaction():
                accepted(graph)
    finally:
        graph.conn.set_trace_callback(None)


def test_helpers_preserve_the_callers_later_rollback(graph):
    with pytest.raises(RuntimeError, match="caller rollback"), graph.transaction():
        update(graph, "rumor", last_told=2000.0)
        accepted(graph)
        raise RuntimeError("caller rollback")
    assert graph.conn.execute("SELECT last_told FROM rumor WHERE id=1").fetchone()[0] == 1000.0


def test_database_faults_are_not_evidence_rejections(graph):
    with graph.transaction():
        graph.conn.execute("ALTER TABLE fact RENAME TO unavailable_fact")
        with pytest.raises(sqlite3.OperationalError):
            audit(graph)


def test_imports_do_not_initialize_service_config_ports_or_realm_wire(tmp_path):
    package_root = Path(__file__).resolve().parents[2]
    script = """
import sys
sys.path.insert(0, sys.argv[1])
import leeroy_mind.realm.records
import leeroy_mind.realm.provenance
for name in ('leeroy_mind.realm.service', 'leeroy_mind.realm.ports',
             'leeroy_mind.realm.protocol', 'leeroy_mind.config'):
    assert name not in sys.modules, name
"""
    probe = tmp_path / "import_probe.py"
    probe.write_text(script, encoding="utf-8")
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    subprocess.run([sys.executable, "-I", str(probe), str(package_root)], cwd=tmp_path,
        env={**os.environ, "LEEROY_MIND_DATA": str(data_dir)}, timeout=10,
        check=True, capture_output=True, text=True)


def test_derived_safe_output_is_screened_with_the_current_identity(graph):
    with graph.transaction():
        # Only the fixed output introduces this whole word; all source prose is safe.
        rejected(audit(graph, human=replace(HUMAN, real_name="A")), "real_name")


def test_identity_must_be_exact_not_a_duck_type_or_subclass(graph):
    class SimilarIdentity(HumanIdentity):
        pass

    similar = SimilarIdentity(10, "Testmaster", "Testmaster", "", True)
    with graph.transaction():
        rejected(audit(graph, human=similar), "invalid_input")


@pytest.mark.parametrize("field,value", [
    ("secret", 2), ("secret", -1), ("holder", -1), ("about", -1),
    ("created", -1.0), ("created", float("inf")),
])
def test_malformed_annotation_metadata_rejects_complete_set(graph, field, value):
    with graph.transaction():
        annotation(graph, **{field: value})
        rejected(audit(graph))


def test_unknown_annotation_kind_cannot_be_treated_as_empty_public_metadata(graph):
    with graph.transaction():
        graph.conn.execute("PRAGMA ignore_check_constraints=ON")
        try:
            annotation(graph, kind="future_private_kind")
        finally:
            graph.conn.execute("PRAGMA ignore_check_constraints=OFF")
        rejected(audit(graph), "invalid_row")


def test_non_boss_secondary_leaf_rejects_the_entire_graph(graph):
    with graph.transaction():
        event(graph, 2, type="death")
        update(graph, "memory", evidence='["event:1","event:2"]')
        rejected(audit(graph), "topic_mismatch")


def test_catalogue_name_and_payload_endpoints_are_accepted(graph):
    with graph.transaction():
        payload = json.loads(graph.conn.execute("SELECT payload FROM game_event").fetchone()[0])
        payload.update(boss_name=BOSS, ts=253402300799999, members=[1, 4294967295],
                       instance_id=4294967295, state="engaged")
        update(graph, "game_event", payload=json.dumps(payload))
        accepted(graph)


def test_every_annotation_projection_change_requires_new_selection(graph):
    with graph.transaction():
        identifier = annotation(graph)
        proof = accepted(graph)
        update(graph, "fact", identifier, holder=22, created=1002.0, text=TEXT)
        assert accepted(graph) != proof
        rejected(revalidate_public_in_unit(graph, proof, human=HUMAN), "proof_changed")


@pytest.mark.parametrize("identifier", [1, 2])
def test_guild_owned_source_or_ancestor_is_valid_public_game_evidence(graph, identifier):
    with graph.transaction():
        memory(graph, 2)
        update(graph, "memory", evidence='["memory:2"]')
        update(graph, "memory", identifier, owner=0)
        proof = accepted(graph)
        assert revalidate_public_in_unit(graph, proof, human=HUMAN) == proof


@pytest.mark.parametrize("identifier", [1, 2])
@pytest.mark.parametrize("owner", [-1, 4294967296])
def test_guild_owner_sentinel_does_not_allow_invalid_owner_bounds(graph, identifier, owner):
    with graph.transaction():
        memory(graph, 2)
        update(graph, "memory", evidence='["memory:2"]')
        update(graph, "memory", identifier, owner=owner)
        rejected(audit(graph), "invalid_row")


@pytest.mark.parametrize("surface", ["memory", "rumor", "boss_name", "annotation"])
def test_attached_markup_name_prefix_still_rejects_unsupported_raw_prose(graph, surface):
    human = replace(HUMAN, real_name="sYnThEtIc")
    raw = f"|cffff0000Synthetic|r {TEXT}"
    with graph.transaction():
        if surface == "memory":
            update(graph, "memory", text=raw)
        elif surface == "rumor":
            update(graph, "rumor", canonical=raw)
        elif surface == "boss_name":
            payload = json.loads(graph.conn.execute("SELECT payload FROM game_event").fetchone()[0])
            payload["boss_name"] = raw
            update(graph, "game_event", payload=json.dumps(payload))
        else:
            annotation(graph, text=raw)
        rejected(audit(graph, human=human))
