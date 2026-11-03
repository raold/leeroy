import dataclasses
import itertools
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from leeroy_mind.human import HumanIdentity
from leeroy_mind.realm import memory_eligibility as eligibility
from leeroy_mind.realm.records import RealmInputs, RealmReadStamp, Rejected
from leeroy_mind.storage.store import Store


HUMAN = HumanIdentity(901, "ExampleHuman", "Example", "Synthetic Person", False)
UNIT_ERROR = r"^Realm input eligibility requires Store\.transaction\(\)$"
NUMBERED = ("event", "session", "story", "chronicle", "encounter", "beat", "life",
            "away", "feud", "mentor", "award", "joke")
OPAQUE = (
    "rule:lands:902:left", "rule:wowlife:2026-11-03:902", "rule:fact:1",
    "rule:rumor:1", "rule:regression", "rule:real_name", "owner:moment-1_2",
    "rule:realm:fact:1",
)


def _insert(store, table, values):
    return store.conn.execute(
        f"INSERT INTO {table}({','.join(values)}) VALUES ({','.join('?' for _ in values)})",
        tuple(values.values()))


def _memory(store, identifier, **overrides):
    values = dict(id=identifier, owner=902, kind="event", text="An ordinary event.",
                  t_event=1000, t_created=1000, t_accessed=1000, importance=5,
                  valence=0, source="game", evidence="[]")
    values.update(overrides)
    _insert(store, "memory", values)


@pytest.fixture
def sample(store):
    with store.transaction():
        for identifier in range(1, 6):
            _memory(store, identifier)
            _insert(store, "chat_line", dict(id=identifier, ts=1000.0, channel="guild",
                from_guid=902, from_name="ExampleActor", to_guid=0, text="An ordinary line.",
                origin="mind"))
            _insert(store, "utterance", dict(id=identifier, ts=1000, guid=902,
                channel="guild", kind="reply", text="An ordinary line."))
        for identifier, public in ((1, 1), (2, 0), (3, 1)):
            _insert(store, "realm_fact", dict(id=identifier, kind="kill", text="A Realm fact.",
                source="event", public=public, ts=1000.0))
        _insert(store, "game_event", dict(id=1, ts=1000.0, type="guard", payload="{}"))
        _insert(store, "realm_source", dict(id=1, source_key="a" * 64, protocol_version=1,
            event_type="guard", game_event_id=1, row_sha256="b" * 64, server_ms=1000000,
            received_at=1000.0, guild=903, human=901, top_keys_json="[]"))
        _insert(store, "realm_fact_origin", dict(fact=3,
            producer_key="event:" + "a" * 64 + ":verified_raid_kill_v1", source=1,
            policy="verified_raid_kill_v1", assurance="verified_event", proof_json="{}",
            qualification_json="{}", text_sha256="b" * 64, revision=1, state="revoked"))
    return store


def _admit(store, refs, **kwargs):
    with store.transaction():
        return eligibility.realm_inputs_in_unit(store, refs, human=HUMAN, **kwargs)


def _final(store, inputs, *, human=HUMAN):
    with store.transaction():
        return eligibility.revalidate_realm_inputs_in_unit(store, inputs, human=human)


def _set_evidence(store, raw, identifier=1):
    with store.transaction():
        store.conn.execute("UPDATE memory SET evidence=? WHERE id=?", (raw, identifier))


def _mark(store, chat=1, utterance=1):
    store.conn.execute("INSERT INTO realm_speech_line(chat_line,utterance) VALUES (?,?)",
                       (chat, utterance))


def _forged(**overrides):
    record = object.__new__(RealmInputs)
    fields = dict(refs=(), stamps=(), mode="exclude", fact_stamps=())
    fields.update(overrides)
    for key, value in fields.items():
        object.__setattr__(record, key, value)
    return record


def test_manifest_keeps_complete_shown_union_and_normalizes_only_duplicates(sample):
    earlier = ("memory:1", "chat:1", "rule:regression")
    later = ("utterance:2", "memory:2", "chat:1", *OPAQUE,
             *(f"{namespace}:9223372036854775807" for namespace in NUMBERED))
    shown = earlier + later
    result = _admit(sample, shown)
    assert result == RealmInputs(tuple(sorted(set(shown))), (), "exclude")
    assert result.stamps == ()
    assert _final(sample, result)
    assert _admit(sample, ()) == RealmInputs((), (), "exclude")


def test_all_supplied_entries_count_before_dedup_and_generator_is_bounded(store):
    assert _admit(store, itertools.repeat("rule:one", 256)) == RealmInputs(("rule:one",), (), "exclude")
    seen = []

    def unbounded():
        for index in itertools.count():
            seen.append(index)
            yield "rule:one"

    assert _admit(store, unbounded()) == Rejected("graph_limit")
    assert seen == list(range(257))


def test_exact_aggregate_and_ascii_byte_limits(store):
    refs = tuple(f"rule:{index:03d}:" + "a" * 119 for index in range(256))
    assert all(len(ref.encode("utf-8")) == 128 for ref in refs)
    assert sum(len(ref.encode("utf-8")) for ref in refs) == 32768
    assert _admit(store, refs) == RealmInputs(refs, (), "exclude")
    assert _admit(store, ["rule:" + "a" * 124]) == Rejected("graph_limit")
    assert _admit(store, refs + ("rule:extra",)) == Rejected("graph_limit")


def test_reference_limits_count_utf8_bytes_and_never_normalize(store):
    ref = "rule:" + "é" * 61 + "x"
    assert len(ref.encode("utf-8")) == 128
    assert _admit(store, [ref]) == RealmInputs((ref,), (), "exclude")
    assert _admit(store, [ref + "x"]) == Rejected("graph_limit")
    distinct = ("owner:é", "owner:e\u0301")
    assert _admit(store, distinct) == RealmInputs(tuple(sorted(distinct)), (), "exclude")


@pytest.mark.parametrize("refs", [None, 12, True, object(), "rule:a", b"rule:a",
                                  {"rule:a": 1}, [1], [True], [b"rule:a"]])
def test_wrong_reference_containers_and_elements_are_rejected(store, refs):
    assert _admit(store, refs) == Rejected("invalid_input")


@pytest.mark.parametrize("ref", ["", "missing", "attraction:1", "stance:1", "Realm:fact:1",
    "realm:fact", "realm:other:1", "realm:fact:01", "realm:fact:0", "realm:fact:+1",
    "realm:fact:9223372036854775808", "rule:", "owner:", "memory:0", "chat:01",
    "utterance:-1", "event:+1", "session:1:2", "story:١", "beat:１",
    "joke:9223372036854775808", "event:1 ", " rule:a", "rule:a\t", "owner:a\n",
    "rule:a\x00", "rule:a\u200b", "rule:a\ud800", "rule:a\u00a0"])
def test_direct_grammar_is_exact_and_reserved_realm_never_becomes_opaque(store, ref):
    assert _admit(store, [ref]) == Rejected("invalid_input")


def test_exact_string_type_required(store):
    class Ref(str):
        pass

    assert _admit(store, [Ref("rule:one")]) == Rejected("invalid_input")


@pytest.mark.parametrize("ref", ["realm:fact:1", "realm:fact:2", "realm:fact:3", "realm:fact:999",
                                  "realm:fact:9223372036854775807"])
def test_canonical_realm_facts_are_excluded_without_lookup(sample, ref):
    statements = []
    with sample.transaction():
        sample.conn.set_trace_callback(statements.append)
        try:
            assert eligibility.realm_inputs_in_unit(sample, [ref], human=HUMAN) == Rejected("unsupported_policy")
            assert statements == []
        finally:
            sample.conn.set_trace_callback(None)


@pytest.mark.parametrize("fact", [1, 2, 3])
def test_any_dependency_excludes_even_without_usable_origin_or_evidence(sample, fact):
    with sample.transaction():
        sample.conn.execute("UPDATE memory SET evidence='not-json', scope='secret', status='rejected' WHERE id=1")
        sample.conn.execute("INSERT INTO realm_memory_dependency(memory,fact) VALUES (1,?)", (fact,))
    assert _admit(sample, ["memory:1"]) == Rejected("unsupported_policy")


def test_dependency_membership_does_not_require_current_fact_row(sample):
    with pytest.raises(RuntimeError, match="discard stale dependency"), sample.transaction():
        sample.conn.execute("INSERT INTO realm_memory_dependency(memory,fact) VALUES (1,1)")
        sample.conn.execute("PRAGMA defer_foreign_keys=ON")
        sample.conn.execute("DELETE FROM realm_fact WHERE id=1")
        assert eligibility.realm_inputs_in_unit(sample, ["memory:1"], human=HUMAN) == Rejected("unsupported_policy")
        raise RuntimeError("discard stale dependency")


@pytest.mark.parametrize("child", ["realm:fact:1", "realm:fact:2", "realm:fact:3", "realm:fact:999"])
def test_direct_realm_evidence_excludes_without_dependency_or_fact_acceptance(sample, child):
    _set_evidence(sample, json.dumps(["event:999", child]))
    assert _admit(sample, ["memory:1"]) == Rejected("unsupported_policy")


@pytest.mark.parametrize("child", ["realm:", "realm:fact:0", "realm:fact:01", "realm:rumor:1",
                                    "realm:fact:9223372036854775808"])
def test_malformed_reserved_child_rejects_the_entire_batch(sample, child):
    _set_evidence(sample, json.dumps([child]))
    assert _admit(sample, ["rule:earlier", "memory:1", "owner:later"]) == Rejected("invalid_row")


@pytest.mark.parametrize("ref", ["memory:999", "chat:999", "utterance:999"])
def test_relevant_missing_rows_reject_complete_batches(sample, ref):
    assert _admit(sample, ["rule:earlier", ref, "owner:later"]) == Rejected("missing_row")


@pytest.mark.parametrize("ref", ["chat:1", "utterance:2"])
def test_speech_marker_uses_each_exact_key_and_equal_text_other_ids_stay_ordinary(sample, ref):
    with sample.transaction():
        _mark(sample, chat=1, utterance=2)
        sample.conn.execute("UPDATE chat_line SET text='Changed counterpart.', from_guid=904, ts=2000 WHERE id=1")
        sample.conn.execute("UPDATE utterance SET text='Different payload.', guid=905, dropped='refused' WHERE id=2")
    assert _admit(sample, [ref]) == Rejected("unsupported_policy")
    ordinary = ("chat:2", "utterance:1", "chat:3", "utterance:3")
    assert _admit(sample, ordinary) == RealmInputs(tuple(sorted(ordinary)), (), "exclude")


@pytest.mark.parametrize("ref,table,identifier", [
    ("chat:1", "utterance", 2), ("utterance:2", "chat_line", 1),
    ("chat:1", "chat_line", 1), ("utterance:2", "utterance", 2),
])
def test_marker_exclusion_precedes_missing_current_or_counterpart_rows(sample, ref, table, identifier):
    # Deferred FK checks permit observing stale membership within this disposable
    # unit; rollback preserves the real schema's permanent pair constraints.
    with pytest.raises(RuntimeError, match="discard stale fixture"), sample.transaction():
        _mark(sample, chat=1, utterance=2)
        sample.conn.execute("PRAGMA defer_foreign_keys=ON")
        sample.conn.execute(f"DELETE FROM {table} WHERE id=?", (identifier,))
        assert eligibility.realm_inputs_in_unit(sample, [ref], human=HUMAN) == Rejected("unsupported_policy")
        raise RuntimeError("discard stale fixture")


def test_direct_only_inspection_keeps_legacy_ancestry_opaque_and_does_not_parse_other_fields(sample):
    with sample.transaction():
        sample.conn.execute("INSERT INTO realm_memory_dependency(memory,fact) VALUES (2,1)")
        _mark(sample, chat=1, utterance=1)
        sample.conn.execute("UPDATE memory SET evidence=?, actors='broken-json', embedding=X'00', scope='secret', status='archived' WHERE id=1",
            (json.dumps(["memory:1", "memory:2", "chat:1", "utterance:1", "event:999",
                         "unknown:any:ancestor", "event:01", "Realm:fact:1", "opaque"]),))
    assert _admit(sample, ["memory:1"]) == RealmInputs(("memory:1",), (), "exclude")
    assert _admit(sample, ["memory:1", "memory:2"]) == Rejected("unsupported_policy")


@pytest.mark.parametrize("scope,status", tuple(itertools.product(
    ("open", "private", "secret"), ("active", "superseded", "archived", "rejected"))))
def test_ordinary_scope_and_status_remain_existing_consumer_responsibility(sample, scope, status):
    with sample.transaction():
        sample.conn.execute("UPDATE memory SET scope=?, status=? WHERE id=1", (scope, status))
    assert _admit(sample, ["memory:1"]) == RealmInputs(("memory:1",), (), "exclude")


@pytest.mark.parametrize("raw", ["not-json", "{}", '"memory:1"', "null", "123", "[null]", "[1]",
    "[true]", "[[]]", '[{"ref":"event:1"}]', "[NaN]", "[Infinity]", "[-Infinity]",
    '[""]', '["event:1\\t"]', '["rule:a\\u200b"]', '["rule:a\\ud800"]',
    json.dumps(["rule:" + "é" * 62]), b"[]", "[" * 2000 + "]" * 2000])
def test_malformed_direct_evidence_is_not_filtered_or_coerced(sample, raw):
    _set_evidence(sample, raw)
    assert _admit(sample, ["rule:before", "memory:1", "owner:after"]) == Rejected("invalid_row")


def test_more_than_64_ordinary_children_have_no_new_cardinality_cap(sample):
    _set_evidence(sample, json.dumps([f"old-opaque-child:{index}" for index in range(1024)]))
    assert _admit(sample, ["memory:1"]) == RealmInputs(("memory:1",), (), "exclude")


def test_raw_evidence_limit_is_exact_and_counts_utf8_before_json_parse(sample, monkeypatch):
    base = json.dumps(["rule:é"], ensure_ascii=False)
    exact = base + " " * (65536 - len(base.encode("utf-8")))
    _set_evidence(sample, exact)
    assert _admit(sample, ["memory:1"]) == RealmInputs(("memory:1",), (), "exclude")
    oversized = exact + " "
    assert len(oversized) <= 65536 < len(oversized.encode("utf-8"))
    _set_evidence(sample, oversized)

    def no_parse(*args, **kwargs):
        raise AssertionError("oversized evidence reached JSON parser")

    monkeypatch.setattr(eligibility.json, "loads", no_parse)
    assert _admit(sample, ["memory:1"]) == Rejected("graph_limit")


@pytest.mark.parametrize("mutation", ["dependency", "realm_child", "malformed_child", "bad_json", "oversized",
                                      "chat_marker", "utterance_marker", "memory_deleted", "chat_deleted", "utterance_deleted"])
def test_final_unit_reclassifies_every_shown_ref_after_real_mutation(sample, mutation):
    shown = ("memory:1", "chat:2", "utterance:3", "rule:prior-submission", "owner:later")
    admitted = _admit(sample, shown)
    assert type(admitted) is RealmInputs
    with sample.transaction():
        if mutation == "dependency":
            sample.conn.execute("INSERT INTO realm_memory_dependency(memory,fact) VALUES (1,1)")
        elif mutation in ("realm_child", "malformed_child", "bad_json", "oversized"):
            raw = {"realm_child": '["realm:fact:999"]', "malformed_child": '["realm:fact:01"]',
                   "bad_json": "not-json", "oversized": " " * 65537}[mutation]
            sample.conn.execute("UPDATE memory SET evidence=? WHERE id=1", (raw,))
        elif mutation == "chat_marker":
            _mark(sample, chat=2, utterance=4)
        elif mutation == "utterance_marker":
            _mark(sample, chat=4, utterance=3)
        else:
            table = {"memory_deleted": "memory", "chat_deleted": "chat_line", "utterance_deleted": "utterance"}[mutation]
            identifier = {"memory_deleted": 1, "chat_deleted": 2, "utterance_deleted": 3}[mutation]
            sample.conn.execute(f"DELETE FROM {table} WHERE id=?", (identifier,))
    assert _final(sample, admitted) is False
    assert admitted.refs == tuple(sorted(shown))


def test_ordinary_content_changes_and_unshown_labels_do_not_claim_snapshot_or_privacy_authority(sample):
    admitted = _admit(sample, ["memory:1", "chat:1", "utterance:1", "event:999"])
    with sample.transaction():
        sample.conn.execute("UPDATE memory SET text='Updated ordinary text.', scope='private', status='archived', t_accessed=2000, access_n=99, embedding=X'00' WHERE id=1")
        sample.conn.execute("UPDATE chat_line SET text='Updated ordinary line.' WHERE id=1")
        sample.conn.execute("UPDATE utterance SET text='Updated output.', dropped='private' WHERE id=1")
        sample.conn.execute("INSERT INTO realm_memory_dependency(memory,fact) VALUES (2,1)")
        _mark(sample, chat=2, utterance=2)
    changed_identity = dataclasses.replace(HUMAN, guid=999, real_name="Another Synthetic Person")
    assert _final(sample, admitted, human=changed_identity) is True


@pytest.mark.parametrize("refs", [(), ("rule:ordinary",)])
def test_memory_mode_empty_related_set_preserves_ordinary_inputs(store, refs):
    assert _admit(store, refs, mode="memory") == RealmInputs(refs, (), "memory")


def test_memory_mode_fact_requires_real_current_origin(store):
    assert _admit(store, ("realm:fact:1",), mode="memory") == Rejected("missing_row")


@pytest.mark.parametrize("mode", ["unknown", "Exclude", "", None, 1, True])
def test_unknown_or_nonstring_modes_are_fixed_rejections(store, mode):
    assert _admit(store, (), mode=mode) == Rejected("invalid_input")


def test_explicit_exact_human_required_but_never_used_for_prose_or_config(sample):
    class OtherHuman(HumanIdentity):
        pass

    invalid = (None, 901, object(), OtherHuman(**dataclasses.asdict(HUMAN)))
    with sample.transaction():
        for human in invalid:
            assert eligibility.realm_inputs_in_unit(sample, (), human=human) == Rejected("invalid_input")
            assert eligibility.revalidate_realm_inputs_in_unit(sample, RealmInputs((), (), "exclude"), human=human) is False
        with pytest.raises(TypeError):
            eligibility.realm_inputs_in_unit(sample, ())
        with pytest.raises(TypeError):
            eligibility.revalidate_realm_inputs_in_unit(sample, RealmInputs((), (), "exclude"))
    _set_evidence(sample, '["rule:real_name"]')
    assert _admit(sample, ["memory:1"]) == RealmInputs(("memory:1",), (), "exclude")


@pytest.mark.parametrize("inputs", [None, (), object(),
    RealmInputs(("memory:1",), (RealmReadStamp(1, "a" * 64, (1,), "b" * 64),), "exclude"),
    _forged(refs=("owner:z", "owner:a")), _forged(refs=("rule:one", "rule:one")),
    _forged(refs=("unknown:1",)), _forged(refs=("realm:fact:1",)),
    _forged(refs=("rule:one",) * 257), _forged(refs=["rule:one"]),
    _forged(stamps=[]), _forged(mode="unknown"), object.__new__(RealmInputs)])
def test_final_refuses_forged_modes_stamps_and_noncanonical_manifests(sample, inputs):
    assert _final(sample, inputs) is False


def test_revalidation_requires_exact_record_type(store):
    class OtherInputs(RealmInputs):
        pass

    assert _final(store, OtherInputs((), (), "exclude")) is False


def test_actual_store_unit_checked_before_sql_even_for_invalid_arguments(sample, clock):
    statements = []
    sample.conn.set_trace_callback(statements.append)
    try:
        assert sample.conn.in_transaction and sample.in_unit is False
        with pytest.raises(RuntimeError, match=UNIT_ERROR):
            eligibility.realm_inputs_in_unit(sample, ["memory:1"], human=None)
        with pytest.raises(RuntimeError, match=UNIT_ERROR):
            eligibility.revalidate_realm_inputs_in_unit(sample, None, human=None)
        assert statements == []
        peer = Store(sample.conn, clock=clock)
        with sample.transaction():
            statements.clear()
            with pytest.raises(RuntimeError, match=UNIT_ERROR):
                eligibility.realm_inputs_in_unit(peer, (), human=HUMAN)
            with pytest.raises(RuntimeError, match=UNIT_ERROR):
                eligibility.revalidate_realm_inputs_in_unit(peer, RealmInputs((), (), "exclude"), human=HUMAN)
            assert statements == []
    finally:
        sample.conn.set_trace_callback(None)


def test_nested_helpers_are_read_only_and_outer_rollback_owns_all_work(sample, tmp_path):
    observer = sqlite3.connect(tmp_path / "mind.db")
    statements = []
    try:
        with pytest.raises(RuntimeError, match="discard caller write"), sample.transaction():
            sample.conn.execute("INSERT INTO meta(key,value) VALUES ('eligibility-caller','pending')")
            with sample.transaction():
                before_changes = sample.conn.total_changes
                sample.conn.set_trace_callback(statements.append)
                try:
                    admitted = eligibility.realm_inputs_in_unit(sample, ["memory:1", "chat:1", "utterance:1"], human=HUMAN)
                    assert eligibility.revalidate_realm_inputs_in_unit(sample, admitted, human=HUMAN)
                finally:
                    sample.conn.set_trace_callback(None)
                assert sample.conn.total_changes == before_changes
                assert statements and all(sql.lstrip().upper().startswith("SELECT ") for sql in statements)
                assert observer.execute("SELECT value FROM meta WHERE key='eligibility-caller'").fetchone() is None
            assert sample.in_unit is True
            raise RuntimeError("discard caller write")
        assert sample.in_unit is False
        assert sample.conn.execute("SELECT value FROM meta WHERE key='eligibility-caller'").fetchone() is None
    finally:
        sample.conn.set_trace_callback(None)
        observer.close()


@pytest.mark.parametrize("ref,table", [("memory:1", "realm_memory_dependency"),
                                       ("chat:1", "realm_speech_line"), ("utterance:1", "realm_speech_line")])
def test_schema_faults_propagate_from_admission_and_final_check(sample, ref, table):
    admitted = _admit(sample, [ref])
    with sample.transaction():
        sample.conn.execute(f"DROP TABLE {table}")
        with pytest.raises(sqlite3.OperationalError):
            eligibility.realm_inputs_in_unit(sample, [ref], human=HUMAN)
        with pytest.raises(sqlite3.OperationalError):
            eligibility.revalidate_realm_inputs_in_unit(sample, admitted, human=HUMAN)


def test_database_authorization_failures_are_not_converted_to_rejections(sample):
    admitted = _admit(sample, ["memory:1"])
    with sample.transaction():
        sample.conn.set_authorizer(lambda action, *args: sqlite3.SQLITE_DENY
                                   if action == sqlite3.SQLITE_SELECT else sqlite3.SQLITE_OK)
        try:
            with pytest.raises(sqlite3.DatabaseError):
                eligibility.realm_inputs_in_unit(sample, ["memory:1"], human=HUMAN)
            with pytest.raises(sqlite3.DatabaseError):
                eligibility.revalidate_realm_inputs_in_unit(sample, admitted, human=HUMAN)
        finally:
            sample.conn.set_authorizer(None)


def test_import_does_not_attach_realm_or_load_config_consumers_or_provenance(tmp_path):
    package_root = Path(eligibility.__file__).parents[2]
    probe = tmp_path / "eligibility_import_probe.py"
    probe.write_text("""
import sys
sys.path.insert(0, sys.argv[1])
from leeroy_mind import protocol
events = dict(protocol._events)
commands = dict(protocol._commands)
import leeroy_mind.realm.memory_eligibility
assert protocol._events == events
assert protocol._commands == commands
for name in ('leeroy_mind.realm.protocol', 'leeroy_mind.realm.service',
             'leeroy_mind.realm.ports', 'leeroy_mind.realm.provenance',
             'leeroy_mind.realm.fact_store', 'leeroy_mind.config',
             'leeroy_mind.memory.writer', 'leeroy_mind.memory.recall',
             'leeroy_mind.nightly.runner'):
    assert name not in sys.modules, name
""", encoding="utf-8")
    data_dir = tmp_path / "isolated-data"
    data_dir.mkdir()
    subprocess.run([sys.executable, "-I", str(probe), str(package_root)], cwd=tmp_path,
        env={**os.environ, "LEEROY_MIND_DATA": str(data_dir)}, timeout=10,
        check=True, capture_output=True, text=True)
