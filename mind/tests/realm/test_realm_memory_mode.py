"""Complete public-memory closure tests using actual Store/FactStore rows."""
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from leeroy_mind.realm.memory_eligibility import (
    merge_realm_inputs_in_unit, realm_inputs_in_unit, revalidate_realm_inputs_in_unit,
)
from leeroy_mind.realm.provenance import audit_realm_reads_in_unit
from leeroy_mind.realm.records import RealmInputs, Rejected
from test_realm_fact_stamps import admit, annotation, make_fact, node_hash, revalidate
from test_realm_live_proof import HUMAN, canonical, insert, roster

MEMORY_COLUMNS = ('id,owner,kind,text,t_event,t_created,actors,place,boss_id,raid_id,'
                  'importance,valence,source,evidence,confidence,status,superseded_by,key,scope,scope_with')


def memory(store, fact, *, evidence=None, dependencies=None, **changes):
    values = dict(owner=0, kind='event', text=fact.text, t_event=int(fact.ts),
                  t_created=1010, t_accessed=1010, importance=5, valence=0,
                  source='game', evidence=canonical([f'realm:fact:{fact.id}'] if evidence is None else evidence))
    values.update(changes)
    identifier = insert(store, 'memory', **values)
    for dependency in (() if dependencies is None else dependencies):
        insert(store, 'realm_memory_dependency', memory=identifier, fact=dependency.id)
    return identifier


def shown(store, identifier, *extra):
    return admit(store, f'memory:{identifier}', *extra)


@pytest.mark.parametrize('policy', ['bg_result_v1', 'arena_result_v1', 'wg_result_v1',
    'pvp_result_v1', 'channel_raid_topic_v1', 'verified_raid_kill_v1'])
def test_exact_notable_and_dependency_only_copy_have_complete_stamps(roster, policy):
    fact = make_fact(roster, policy)
    with roster.transaction():
        original = memory(roster, fact)
        copy = memory(roster, fact, evidence=[f'memory:{original}'], dependencies=[fact])
    manifest = shown(roster, copy)
    assert type(manifest) is RealmInputs
    assert tuple(stamp.memory_id for stamp in manifest.stamps) == (copy,)
    assert manifest.stamps[0].fact_ids == (fact.id,)
    assert tuple(stamp.fact_id for stamp in manifest.fact_stamps) == (fact.id,)
    assert revalidate(roster, manifest)
    with roster.transaction():
        roster.conn.execute("UPDATE memory SET evidence='[]' WHERE id=?", (copy,))
    assert not revalidate(roster, manifest)
    assert type(shown(roster, copy)) is RealmInputs


def test_memory_hash_and_aggregate_are_exact_and_independent_of_other_shown_roots(roster):
    first = make_fact(roster)
    second = make_fact(roster, 'arena_result_v1')
    with roster.transaction():
        child = memory(roster, first)
        root = memory(roster, first, evidence=[f'memory:{child}'], dependencies=[first, second])
        other = memory(roster, second)
        annotation(roster, f'memory:{child}', first.text)
    alone = shown(roster, root)
    together = shown(roster, root, f'memory:{other}')
    assert type(alone) is RealmInputs and type(together) is RealmInputs
    assert alone.stamps[0] == next(stamp for stamp in together.stamps if stamp.memory_id == root)
    with roster.transaction():
        projections = []
        for identifier in (child, root):
            row = dict(roster.conn.execute(f'SELECT {MEMORY_COLUMNS} FROM memory WHERE id=?', (identifier,)).fetchone())
            row['actors'], row['evidence'] = json.loads(row['actors']), json.loads(row['evidence'])
            projections.append(dict(memory_id=identifier, sha256=node_hash('memory', str(identifier), row)))
        refs = sorted([f'memory:{child}', f'memory:{root}', f'realm:fact:{first.id}', f'realm:fact:{second.id}'])
        annotations = [dict(row) for row in roster.conn.execute('SELECT id,kind,root,about,text,secret,holder,created FROM fact ORDER BY id')]
    key = hashlib.sha256(canonical(refs).encode()).hexdigest()
    body = dict(schema='realm_memory_read_v1', root=f'memory:{root}', memories=projections,
        facts=[dict(fact_id=stamp.fact_id, revision=stamp.revision, fact_sha256=stamp.fact_sha256,
                    proof_sha256=stamp.proof_sha256) for stamp in alone.fact_stamps],
        dependency_rows=[[root, first.id], [root, second.id]],
        privacy=dict(kind='privacy_set', key=key, projection_version=1, sha256=node_hash('privacy_set', key, annotations)))
    assert alone.stamps[0].memory_sha256 == projections[1]['sha256']
    assert alone.stamps[0].proof_sha256 == hashlib.sha256(b'realm-memory-read-v1\0' + canonical(body).encode()).hexdigest()


def test_claim_a_retains_unmentioned_mixed_policy_b_and_revocation_invalidates_parent(roster):
    first = make_fact(roster)
    second = make_fact(roster, 'channel_raid_topic_v1')
    with roster.transaction():
        child = memory(roster, second)
        root = memory(roster, first, evidence=[f'realm:fact:{first.id}', f'memory:{child}'])
    manifest = shown(roster, root)
    assert type(manifest) is RealmInputs and manifest.stamps[0].fact_ids == (first.id, second.id)
    with roster.transaction():
        roster.conn.execute("UPDATE realm_fact_origin SET state='revoked' WHERE fact=?", (second.id,))
    assert not revalidate(roster, manifest)


@pytest.mark.parametrize('mutation', ['new_child_link', 'remove_link', 'edge_order', 'duplicate_edge',
    'child_annotation', 'fact_revision', 'child_text', 'child_status', 'child_scope'])
def test_complete_nested_topology_and_annotations_are_snapshot_bound(roster, mutation):
    first = make_fact(roster)
    second = make_fact(roster, 'arena_result_v1')
    with roster.transaction():
        child = memory(roster, first, dependencies=[first])
        root = memory(roster, first, evidence=[f'memory:{child}', f'realm:fact:{first.id}'], dependencies=[second])
    manifest = shown(roster, root)
    assert type(manifest) is RealmInputs
    with roster.transaction():
        if mutation == 'new_child_link':
            insert(roster, 'realm_memory_dependency', memory=child, fact=second.id)
        elif mutation == 'remove_link':
            roster.conn.execute('DELETE FROM realm_memory_dependency WHERE memory=?', (child,))
        elif mutation in ('edge_order', 'duplicate_edge'):
            refs = [f'realm:fact:{first.id}', f'memory:{child}']
            if mutation == 'duplicate_edge':
                refs.append(f'memory:{child}')
            roster.conn.execute('UPDATE memory SET evidence=? WHERE id=?', (canonical(refs), root))
        elif mutation == 'child_annotation':
            annotation(roster, f'memory:{child}')
        elif mutation == 'fact_revision':
            roster.conn.execute('UPDATE realm_fact_origin SET revision=2 WHERE fact=?', (second.id,))
        else:
            field, value = {'child_text': ('text', 'Unsupported narrative.'),
                'child_status': ('status', 'archived'), 'child_scope': ('scope', 'private')}[mutation]
            roster.conn.execute(f'UPDATE memory SET {field}=? WHERE id=?', (value, child))
    assert not revalidate(roster, manifest)


def test_access_embedding_and_merge_counters_do_not_change_public_projection(roster):
    fact = make_fact(roster)
    with roster.transaction():
        root = memory(roster, fact)
    manifest = shown(roster, root)
    with roster.transaction():
        roster.conn.execute('UPDATE memory SET t_accessed=2000,access_n=9,merged_n=8,embedding=X\'00\' WHERE id=?', (root,))
    assert revalidate(roster, manifest)


@pytest.mark.parametrize('refs', [lambda f: [f.ref, f'realm:fact:{f.id}'],
                                   lambda f: [f'realm:fact:{f.id}', f.ref]])
def test_event_matching_uses_completed_node_local_fact_closure_in_either_order(roster, refs):
    fact = make_fact(roster)
    with roster.transaction():
        root = memory(roster, fact, evidence=refs(fact))
    assert type(shown(roster, root)) is RealmInputs


def test_unrelated_shown_fact_and_sibling_cannot_back_a_child_event_or_claim(roster):
    first = make_fact(roster)
    other = make_fact(roster, payload={'ts': 1001000}, captured=1011.0)
    with roster.transaction():
        child = memory(roster, first, evidence=[other.ref, f'realm:fact:{first.id}'])
        parent = memory(roster, first, evidence=[f'memory:{child}', f'realm:fact:{other.id}'])
    assert type(shown(roster, child, f'realm:fact:{other.id}')) is Rejected
    assert type(shown(roster, parent)) is Rejected
    arena = make_fact(roster, 'arena_result_v1')
    with roster.transaction():
        roster.conn.execute('UPDATE memory SET evidence=?,text=? WHERE id=?',
                            (canonical([f'realm:fact:{first.id}']), arena.text, child))
    assert type(shown(roster, child, f'realm:fact:{arena.id}')) is Rejected


@pytest.mark.parametrize('changes', [dict(text='We won everything.'), dict(text='Synthetic Private'),
    dict(text='We kissed.'), dict(t_event=1001), dict(boss_id=0), dict(raid_id=631),
    dict(source='owner'), dict(kind='story'), dict(scope='secret'), dict(scope_with=101),
    dict(status='archived'), dict(status='rejected'), dict(actors='[1,1]'),
    dict(actors='[true]'), dict(actors='{}'), dict(evidence='[null]'), dict(evidence='[NaN]')])
def test_unsupported_prose_time_metadata_privacy_and_malformed_nodes_fail(roster, changes):
    fact = make_fact(roster)
    with roster.transaction():
        root = memory(roster, fact, dependencies=[fact], **changes)
    assert type(shown(roster, root)) is Rejected


@pytest.mark.parametrize('policy,boss,raid,accepted', [
    ('verified_raid_kill_v1', None, None, True), ('verified_raid_kill_v1', 0, 631, True),
    ('verified_raid_kill_v1', 1, 631, False), ('verified_raid_kill_v1', 0, 649, False),
    ('channel_raid_topic_v1', None, None, True), ('channel_raid_topic_v1', None, 631, True),
    ('channel_raid_topic_v1', 0, 631, False), ('channel_raid_topic_v1', None, 649, False),
])
def test_optional_catalogue_fields_match_one_supporting_policy(roster, policy, boss, raid, accepted):
    fact = make_fact(roster, policy)
    with roster.transaction():
        root = memory(roster, fact, boss_id=boss, raid_id=raid)
    assert (type(shown(roster, root)) is RealmInputs) is accepted


def test_text_time_and_metadata_cannot_be_taken_from_different_facts(roster):
    first = make_fact(roster, 'verified_raid_kill_v1')
    second = make_fact(roster, 'channel_raid_topic_v1', payload={'ts': 1001000}, captured=1011.0)
    with roster.transaction():
        root = memory(roster, first, dependencies=[first, second], t_event=int(second.ts), raid_id=631)
    assert type(shown(roster, root)) is Rejected
    # Equal text from two source times still requires one coherent source time.
    other = make_fact(roster, payload={'ts': 1002000}, captured=1012.0)
    with roster.transaction():
        roster.conn.execute('UPDATE memory SET text=?,t_event=?,boss_id=NULL,raid_id=NULL WHERE id=?',
                            (other.text, int(second.ts), root))
        insert(roster, 'realm_memory_dependency', memory=root, fact=other.id)
    assert type(shown(roster, root)) is Rejected


@pytest.mark.parametrize('branch', ['memory:999', 'realm:fact:999', 'chat:1', 'utterance:1',
    'story:1', 'encounter:1', 'session:1', 'life:1', 'owner:a', 'rule:a', 'stance:1',
    'attraction:1', 'unknown:1', 'realm:fact:01', 'event:01'])
def test_no_valid_branch_can_hide_a_missing_or_unsupported_branch(roster, branch):
    fact = make_fact(roster)
    with roster.transaction():
        root = memory(roster, fact, evidence=[f'realm:fact:{fact.id}', branch])
    assert type(shown(roster, root)) is Rejected


def test_unbacked_child_cycle_and_unrelated_lower_api_input_fail(roster):
    fact = make_fact(roster)
    with roster.transaction():
        child = memory(roster, fact, evidence=[])
        root = memory(roster, fact, evidence=[f'realm:fact:{fact.id}', f'memory:{child}'])
        assert audit_realm_reads_in_unit(roster, (f'memory:{child}',), human=HUMAN) == Rejected('invalid_input')
    assert type(shown(roster, root)) is Rejected
    with roster.transaction():
        roster.conn.execute('UPDATE memory SET evidence=? WHERE id=?', (canonical([f'memory:{root}']), child))
    assert shown(roster, root) == Rejected('cycle')


def test_memory_annotation_support_is_node_local_and_includes_empty_sets(roster):
    first = make_fact(roster)
    other = make_fact(roster, 'arena_result_v1')
    with roster.transaction():
        root = memory(roster, first)
    manifest = shown(roster, root)
    with roster.transaction():
        annotation(roster, f'memory:{root}', other.text)
    assert type(shown(roster, root, f'realm:fact:{other.id}')) is Rejected
    assert not revalidate(roster, manifest)
    with roster.transaction():
        roster.conn.execute('UPDATE fact SET text=? WHERE root=?', (first.text, f'memory:{root}'))
    assert type(shown(roster, root)) is RealmInputs
    assert not revalidate(roster, manifest)


def chain(store, fact, count):
    tail = memory(store, fact)
    for _ in range(count - 1):
        tail = memory(store, fact, evidence=[f'memory:{tail}'], dependencies=[fact])
    return tail


def test_longest_path_includes_source_event_and_memoized_subtree_height(roster):
    fact = make_fact(roster)
    with roster.transaction():
        valid = chain(roster, fact, 30)
    assert type(shown(roster, valid)) is RealmInputs
    with roster.transaction():
        too_deep = memory(roster, fact, evidence=[f'memory:{valid}'], dependencies=[fact])
    assert shown(roster, too_deep) == Rejected('graph_limit')
    # A short path visits the same subtree first; a later deep path must still
    # include that subtree's memoized height in the operation's depth limit.
    with roster.transaction():
        subtree = chain(roster, fact, 2)
        long_path = subtree
        for _ in range(28):
            long_path = memory(roster, fact, evidence=[f'memory:{long_path}'], dependencies=[fact])
        root = memory(roster, fact, evidence=[f'memory:{subtree}', f'memory:{long_path}'], dependencies=[fact])
    assert shown(roster, root) == Rejected('graph_limit')


def test_duplicate_declared_edges_count_while_physical_fact_rows_are_reused(roster):
    fact = make_fact(roster)
    with roster.transaction():
        leaves = [memory(roster, fact, evidence=[f'realm:fact:{fact.id}'] * 64) for _ in range(12)]
        root = memory(roster, fact, evidence=[f'memory:{leaf}' for leaf in leaves], dependencies=[fact])
        statements = []
        roster.conn.set_trace_callback(statements.append)
        try:
            accepted = audit_realm_reads_in_unit(roster, (f'memory:{root}',), human=HUMAN)
        finally:
            roster.conn.set_trace_callback(None)
    assert type(accepted) is tuple
    for table in ('realm_fact', 'realm_fact_origin', 'realm_source', 'game_event', 'realm_knows'):
        assert sum(f' FROM {table} ' in sql for sql in statements) == 1
    with roster.transaction():
        extra = memory(roster, fact, evidence=[f'realm:fact:{fact.id}'] * 64)
        roster.conn.execute('UPDATE memory SET evidence=? WHERE id=?',
                            (canonical([*(f'memory:{leaf}' for leaf in leaves), f'memory:{extra}']), root))
    assert shown(roster, root) == Rejected('graph_limit')


def test_64_evidence_limit_and_dependency_rows_have_separate_bounds(roster):
    fact = make_fact(roster)
    with roster.transaction():
        root = memory(roster, fact, evidence=[f'realm:fact:{fact.id}'] * 65)
    assert shown(roster, root) == Rejected('graph_limit')
    # An overfull retained set must be rejected before trying its first origin;
    # dependency rows are allowed their own 200-row bound, not the evidence cap.
    with roster.transaction():
        roster.conn.execute("UPDATE memory SET evidence='[]' WHERE id=?", (root,))
        for index in range(201):
            identifier = insert(roster, 'realm_fact', kind='bg_result', text=fact.text, source='event', ts=1000)
            insert(roster, 'realm_memory_dependency', memory=root, fact=identifier)
    assert shown(roster, root) == Rejected('graph_limit')


def test_node_budget_is_global_and_large_union_never_accepts_a_prefix(roster):
    fact = make_fact(roster)
    with roster.transaction():
        leaves = [memory(roster, fact) for _ in range(100)]
    singles = [shown(roster, leaf) for leaf in leaves]
    assert all(type(item) is RealmInputs for item in singles)
    with roster.transaction():
        # 97 memory + 97 aggregate privacy + 6 shared fact/proof nodes = 200.
        assert type(merge_realm_inputs_in_unit(roster, tuple(singles[:97]), human=HUMAN)) is RealmInputs
        assert merge_realm_inputs_in_unit(roster, tuple(singles), human=HUMAN) == Rejected('graph_limit')


def test_union_keeps_every_earlier_shown_ref_and_independent_manifests(roster):
    fact = make_fact(roster)
    other = make_fact(roster, 'arena_result_v1')
    with roster.transaction():
        root = memory(roster, fact)
    left, right = shown(roster, root, 'rule:earlier'), admit(roster, f'realm:fact:{other.id}', 'owner:later')
    with roster.transaction():
        merged = merge_realm_inputs_in_unit(roster, (left, right, left), human=HUMAN)
        assert type(merged) is RealmInputs
        assert merged.refs == tuple(sorted(set(left.refs + right.refs)))
        assert merged.stamps == left.stamps and len(merged.fact_stamps) == 2
        assert merge_realm_inputs_in_unit(roster, (left, replace(right, mode='exclude', fact_stamps=())), human=HUMAN) == Rejected('invalid_input')
        roster.conn.execute("UPDATE realm_fact_origin SET state='revoked' WHERE fact=?", (other.id,))
    assert revalidate(roster, left)
    assert not revalidate(roster, right) and not revalidate(roster, merged)


def test_unrelated_legacy_memory_stays_opaque_then_new_relation_invalidates_old_manifest(roster):
    fact = make_fact(roster)
    with roster.transaction():
        root = memory(roster, fact, evidence=['memory:999', 'chat:999', 'unknown:legacy'],
                      text='Ordinary legacy narrative.', scope='private')
    manifest = shown(roster, root)
    assert manifest == RealmInputs((f'memory:{root}',), (), 'memory')
    with roster.transaction():
        insert(roster, 'realm_memory_dependency', memory=root, fact=fact.id)
    assert not revalidate(roster, manifest)


def test_batch_is_select_only_and_does_not_cache_acceptance_between_calls(roster):
    fact = make_fact(roster)
    with roster.transaction():
        root = memory(roster, fact)
        before = roster.conn.total_changes
        statements = []
        roster.conn.set_trace_callback(statements.append)
        try:
            accepted = realm_inputs_in_unit(roster, (f'memory:{root}',), human=HUMAN, mode='memory')
        finally:
            roster.conn.set_trace_callback(None)
        assert type(accepted) is RealmInputs
        assert roster.conn.total_changes == before
        assert statements and all(sql.lstrip().upper().startswith('SELECT ') for sql in statements)
        roster.conn.execute('UPDATE realm_fact_origin SET revision=2 WHERE fact=?', (fact.id,))
        assert not revalidate_realm_inputs_in_unit(roster, accepted, human=HUMAN)


def test_import_has_no_configuration_service_or_consumer_attachment(tmp_path):
    package_root = Path(__file__).resolve().parents[2]
    probe = tmp_path / 'memory_mode_import_probe.py'
    probe.write_text('''
import sys
sys.path.insert(0, sys.argv[1])
from leeroy_mind import protocol
prior = dict(protocol._events), dict(protocol._commands)
from leeroy_mind.realm.memory_eligibility import merge_realm_inputs_in_unit
assert prior == (protocol._events, protocol._commands)
for name in ('leeroy_mind.realm.provenance', 'leeroy_mind.realm.service',
             'leeroy_mind.realm.fact_store', 'leeroy_mind.config',
             'leeroy_mind.memory.writer', 'leeroy_mind.memory.recall', 'leeroy_mind.nightly.runner'):
    assert name not in sys.modules, name
''', encoding='utf-8')
    subprocess.run([sys.executable, '-I', str(probe), str(package_root)], cwd=tmp_path,
        env={**os.environ, 'LEEROY_MIND_DATA': str(tmp_path)}, check=True,
        timeout=10, capture_output=True, text=True)


@pytest.mark.parametrize('raw', ['[null]', '[NaN]', '["realm:fact:01"]', '{"evidence":[]}',
                                '[' * 2000 + ']' * 2000])
def test_raw_memory_evidence_is_strict_after_retained_link_classification(roster, raw):
    fact = make_fact(roster)
    with roster.transaction():
        root = memory(roster, fact, dependencies=[fact])
        roster.conn.execute('UPDATE memory SET evidence=? WHERE id=?', (raw, root))
    assert type(shown(roster, root)) is Rejected


def test_retained_dependency_cap_is_not_the_64_evidence_cap(roster):
    fact = make_fact(roster)
    with roster.transaction():
        root = memory(roster, fact, evidence=[])
        for _ in range(65):
            unproved = insert(roster, 'realm_fact', kind='bg_result', text=fact.text, source='event', ts=1000)
            insert(roster, 'realm_memory_dependency', memory=root, fact=unproved)
    # Reading the first missing origin proves that the complete 65-link set was
    # allowed by its own bound. The lack of authority must still reject it.
    assert shown(roster, root) == Rejected('missing_row')


@pytest.mark.parametrize('changes', [dict(secret=1), dict(kind='life_news'), dict(text='We kissed.'),
                                    dict(text='Synthetic Private')])
def test_nested_memory_annotations_cannot_hide_in_valid_parent(roster, changes):
    fact = make_fact(roster)
    with roster.transaction():
        child = memory(roster, fact)
        root = memory(roster, fact, evidence=[f'memory:{child}'], dependencies=[fact])
        annotation(roster, f'memory:{child}', **changes)
    assert type(shown(roster, root)) is Rejected
