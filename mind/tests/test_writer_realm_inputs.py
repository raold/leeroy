"""Writer checks use the original reader manifests and the actual output reader."""
from dataclasses import replace
from pathlib import Path
import sys
import sqlite3

import pytest

sys.path.insert(0, str(Path(__file__).parent / 'realm'))
from test_realm_fact_stamps import admit, annotation, make_fact
from test_realm_live_proof import HUMAN, roster
from test_realm_memory_mode import memory
from conftest import make_mem, run
from leeroy_mind.llm.client import LLMError
from leeroy_mind.memory.writer import MemoryCandidate, MemoryWriter
from leeroy_mind.realm.memory_eligibility import realm_inputs_in_unit
from leeroy_mind.realm.memory_lineage import RealmLineageError
from leeroy_mind.realm.records import RealmInputs
from leeroy_mind.storage.rows import pack_vec


class Embed:
    def __init__(self, effect=None, outage=False, vector=None):
        self.effect, self.outage, self.vector = effect, outage, vector
    async def embed(self, texts, priority):
        if self.effect:
            self.effect()
        if self.outage:
            raise LLMError('offline')
        return [self.vector]


def row(fact, **changes):
    return make_mem(owner=0, kind='event', text=fact.text, t_event=int(fact.ts), evidence=(), **changes)


def candidate(fact, **changes):
    values = dict(owner=0, kind='event', text=fact.text, t_event=int(fact.ts), actors=(),
                  importance=5, valence=0, source='game', evidence=())
    values.update(changes)
    return MemoryCandidate(**values)


def links(store, mid):
    return tuple(r[0] for r in store.conn.execute(
        'SELECT fact FROM realm_memory_dependency WHERE memory=? ORDER BY fact', (mid,)))


def test_checked_put_persists_omitted_manifest_and_finite_output(roster, clock):
    fact = make_fact(roster)
    inputs = admit(roster, f'realm:fact:{fact.id}')
    writer = MemoryWriter(roster, Embed(), clock, human=lambda: HUMAN)
    assert writer.store is roster
    mid = writer.put(row(fact), realm_inputs=inputs)
    assert links(roster, mid) == (fact.id,)
    with roster.transaction():
        output = realm_inputs_in_unit(roster, (f'memory:{mid}',), human=HUMAN, mode='memory')
    assert type(output) is RealmInputs and output.stamps[0].memory_id == mid


@pytest.mark.parametrize('changes', [dict(text='Invented narrative.'), dict(t_event=1001),
    dict(boss_id=1), dict(scope='private')])
def test_output_rejection_rolls_back_row_and_prior_outer_write(roster, clock, changes):
    fact = make_fact(roster)
    inputs = admit(roster, f'realm:fact:{fact.id}')
    writer = MemoryWriter(roster, Embed(), clock, human=lambda: HUMAN)
    with pytest.raises(RealmLineageError):
        with roster.transaction():
            roster.set_meta('before', 'must roll back')
            writer.put(replace(row(fact), **changes), realm_inputs=inputs)
    assert roster.get_meta('before') is None
    assert roster.conn.execute('SELECT count(*) FROM memory').fetchone()[0] == 0
    assert roster.conn.execute('SELECT count(*) FROM realm_memory_dependency').fetchone()[0] == 0


def test_no_human_and_unmanifested_input_reject_checked_only(roster, clock):
    fact = make_fact(roster)
    inputs = admit(roster, f'realm:fact:{fact.id}')
    writer = MemoryWriter(roster, Embed(), clock)
    with pytest.raises(RealmLineageError) as error:
        writer.put(row(fact), realm_inputs=inputs)
    assert error.value.rejection.reason == 'invalid_input'
    assert writer.put(row(fact)) > 0
    writer = MemoryWriter(roster, Embed(), clock, human=lambda: HUMAN)
    with pytest.raises(RealmLineageError):
        writer.put(replace(row(fact), evidence=('event:1',)), realm_inputs=inputs)


def test_exclude_cannot_rewrite_tagged_target(roster, clock):
    fact = make_fact(roster)
    writer = MemoryWriter(roster, Embed(), clock, human=lambda: HUMAN)
    mid = writer.put(replace(row(fact), key='key'), shown=(f'realm:fact:{fact.id}',))
    with roster.transaction():
        inputs = realm_inputs_in_unit(roster, (), human=HUMAN)
    old = roster.get_memories([mid])[0]
    with pytest.raises(RealmLineageError) as error:
        writer.put(replace(row(fact), key='key', text='changed'), realm_inputs=inputs)
    assert error.value.rejection.reason == 'unsupported_policy'
    assert roster.get_memories([mid])[0] == old and links(roster, mid) == (fact.id,)


@pytest.mark.parametrize('mutation', ['revision', 'revocation', 'annotation'])
@pytest.mark.parametrize('outage', [False, True])
def test_async_revalidates_same_original_after_await(roster, clock, mutation, outage):
    fact = make_fact(roster)
    inputs = admit(roster, f'realm:fact:{fact.id}')
    def change():
        with roster.transaction():
            if mutation == 'revision':
                roster.conn.execute('UPDATE realm_fact_origin SET revision=revision+1 WHERE fact=?', (fact.id,))
            elif mutation == 'revocation':
                roster.conn.execute("UPDATE realm_fact_origin SET state='revoked' WHERE fact=?", (fact.id,))
            else:
                annotation(roster, f'realm:fact:{fact.id}')
    writer = MemoryWriter(roster, Embed(change, outage=outage), clock, human=lambda: HUMAN)
    result = run(writer.write(candidate(fact), realm_inputs=inputs))
    assert result.action == 'rejected' and result.id is None
    assert roster.conn.execute('SELECT count(*) FROM memory').fetchone()[0] == 0


def test_keyed_and_outage_checked_paths(roster, clock):
    fact = make_fact(roster)
    inputs = admit(roster, f'realm:fact:{fact.id}')
    writer = MemoryWriter(roster, Embed(outage=True), clock, human=lambda: HUMAN)
    result = run(writer.write(candidate(fact), realm_inputs=inputs))
    assert result.action == 'inserted' and links(roster, result.id) == (fact.id,)
    keyed = run(writer.write(candidate(fact, key='other'), realm_inputs=inputs))
    assert keyed.action == 'updated' and links(roster, keyed.id) == (fact.id,)
    with roster.transaction():
        roster.conn.execute('UPDATE realm_fact_origin SET revision=revision+1 WHERE fact=?', (fact.id,))
    assert run(writer.write(candidate(fact, key='other'), realm_inputs=inputs)).action == 'rejected'


def test_shown_generator_affects_scope_and_lineage(roster, clock):
    fact = make_fact(roster)
    writer = MemoryWriter(roster, Embed(), clock)
    source = writer.put(replace(row(fact), scope='private'), shown=(f'realm:fact:{fact.id}',))
    target = writer.put(row(fact), shown=(x for x in [f'memory:{source}']))
    assert roster.get_memories([target])[0].scope == 'private'
    assert links(roster, target) == (fact.id,)


def test_manifest_memory_closure_is_carried_when_citations_omitted(roster, clock):
    fact = make_fact(roster)
    writer = MemoryWriter(roster, Embed(), clock, human=lambda: HUMAN)
    child = writer.put(row(fact), shown=(f'realm:fact:{fact.id}',))
    inputs = admit(roster, f'memory:{child}')
    assert type(inputs) is RealmInputs and inputs.stamps[0].fact_ids == (fact.id,)
    result = writer.put(row(fact), realm_inputs=inputs)
    assert links(roster, result) == (fact.id,)


@pytest.mark.parametrize('change', ['text', 'scope', 'link'])
def test_async_rejects_changed_original_memory(roster, clock, change):
    fact = make_fact(roster)
    writer = MemoryWriter(roster, Embed(), clock, human=lambda: HUMAN)
    child = writer.put(row(fact), shown=(f'realm:fact:{fact.id}',))
    inputs = admit(roster, f'memory:{child}')
    def mutate():
        with roster.transaction():
            if change == 'text':
                roster.conn.execute("UPDATE memory SET text='changed' WHERE id=?", (child,))
            elif change == 'scope':
                roster.conn.execute("UPDATE memory SET scope='private' WHERE id=?", (child,))
            else:
                roster.conn.execute('DELETE FROM realm_memory_dependency WHERE memory=?', (child,))
    writer._embedder = Embed(mutate)
    assert run(writer.write(candidate(fact), realm_inputs=inputs)).action == 'rejected'
    assert roster.conn.execute('SELECT count(*) FROM memory').fetchone()[0] == 1


def test_async_exclude_detects_new_realm_link_on_ordinary_input(roster, clock):
    fact = make_fact(roster)
    writer = MemoryWriter(roster, Embed(), clock, human=lambda: HUMAN)
    child = writer.put(row(fact))
    with roster.transaction():
        inputs = realm_inputs_in_unit(roster, (f'memory:{child}',), human=HUMAN)
    assert type(inputs) is RealmInputs
    def mutate():
        with roster.transaction():
            roster.conn.execute('INSERT INTO realm_memory_dependency(memory,fact) VALUES (?,?)', (child, fact.id))
    writer._embedder = Embed(mutate)
    assert run(writer.write(candidate(fact), realm_inputs=inputs)).action == 'rejected'
    assert roster.conn.execute('SELECT count(*) FROM memory').fetchone()[0] == 1


def test_exclude_cannot_merge_tagged_twin(roster, clock):
    fact = make_fact(roster)
    writer = MemoryWriter(roster, Embed(outage=True), clock, human=lambda: HUMAN)
    twin = writer.put(replace(row(fact), t_created=int(clock.now()), t_accessed=int(clock.now())),
                      shown=(f'realm:fact:{fact.id}',))
    before = roster.get_memories([twin])[0]
    with roster.transaction():
        inputs = realm_inputs_in_unit(roster, (), human=HUMAN)
    result = run(writer.write(candidate(fact), realm_inputs=inputs))
    assert result.action == 'rejected' and result.id is None
    assert roster.get_memories([twin])[0] == before and links(roster, twin) == (fact.id,)


def test_keyed_output_audit_failure_restores_old_row_and_links(roster, clock):
    fact = make_fact(roster)
    writer = MemoryWriter(roster, Embed(), clock, human=lambda: HUMAN)
    inputs = admit(roster, f'realm:fact:{fact.id}')
    mid = writer.put(replace(row(fact), key='key'), realm_inputs=inputs)
    before = roster.get_memories([mid])[0]
    with pytest.raises(RealmLineageError):
        writer.put(replace(row(fact), key='key', text='unsupported'), realm_inputs=inputs)
    assert roster.get_memories([mid])[0] == before and links(roster, mid) == (fact.id,)


def test_output_unsupported_ancestry_rolls_back(roster, clock):
    fact = make_fact(roster)
    with roster.transaction():
        inputs = realm_inputs_in_unit(roster, (f'realm:fact:{fact.id}', 'story:1'),
                                      human=HUMAN, mode='memory')
    assert type(inputs) is RealmInputs
    writer = MemoryWriter(roster, Embed(), clock, human=lambda: HUMAN)
    with pytest.raises(RealmLineageError):
        writer.put(replace(row(fact), evidence=('story:1',)), realm_inputs=inputs)
    assert roster.conn.execute('SELECT count(*) FROM memory').fetchone()[0] == 0


def test_keyed_checks_current_human_again_at_actual_write(roster, clock):
    fact = make_fact(roster)
    inputs = admit(roster, f'realm:fact:{fact.id}')
    people = iter((HUMAN, None))
    writer = MemoryWriter(roster, Embed(), clock, human=lambda: next(people, None))
    # The text screen can also consult the provider; either way the checked sink fails closed.
    result = run(writer.write(candidate(fact, key='key'), realm_inputs=inputs))
    assert result.action == 'rejected' and result.id is None
    assert roster.conn.execute('SELECT count(*) FROM memory').fetchone()[0] == 0


def test_original_manifest_full_nested_closure_and_direct_fact_survive_omitted_citations(roster, clock):
    first = make_fact(roster)
    second = make_fact(roster, 'arena_result_v1')
    third = make_fact(roster, 'channel_raid_topic_v1')
    with roster.transaction():
        leaf = memory(roster, second, evidence=[f'realm:fact:{second.id}'])
        root = memory(roster, first, evidence=[f'realm:fact:{first.id}', f'memory:{leaf}'])
    before = roster.get_memories([leaf, root])
    inputs = admit(roster, f'memory:{root}', f'realm:fact:{third.id}')
    assert type(inputs) is RealmInputs
    assert inputs.stamps[0].fact_ids == tuple(sorted((first.id, second.id)))
    assert third.id in {stamp.fact_id for stamp in inputs.fact_stamps}
    assert links(roster, leaf) == links(roster, root) == ()
    writer = MemoryWriter(roster, Embed(), clock, human=lambda: HUMAN)
    output = writer.put(row(first), realm_inputs=inputs)
    assert links(roster, output) == tuple(sorted((first.id, second.id, third.id)))
    assert roster.get_memories([leaf, root]) == before
    assert links(roster, leaf) == links(roster, root) == ()


def packed(index):
    values = [0.0] * 512
    values[index] = 1.0
    return pack_vec(values)


def indexes(store, mid):
    vectors = [tuple(r) for r in store.conn.execute(
        'SELECT memory_id,owner,embedding FROM memory_vec WHERE memory_id=?', (mid,))]
    actors = [tuple(r) for r in store.conn.execute(
        'SELECT memory_id,guid FROM memory_actor WHERE memory_id=? ORDER BY guid', (mid,))]
    fts = [tuple(r) for r in store.conn.execute(
        'SELECT rowid,text FROM memory_fts WHERE rowid=?', (mid,))]
    return vectors, actors, fts


def test_async_output_audit_failure_removes_inserted_vector_and_indexes(roster, clock):
    fact = make_fact(roster)
    inputs = admit(roster, f'realm:fact:{fact.id}')
    writer = MemoryWriter(roster, Embed(vector=packed(0)), clock, human=lambda: HUMAN)
    result = run(writer.write(candidate(fact, text='unsupportedvectorword', actors=(101,)),
                              realm_inputs=inputs))
    assert result.action == 'rejected' and result.id is None
    assert roster.conn.execute('SELECT count(*) FROM memory').fetchone()[0] == 0
    assert roster.conn.execute('SELECT count(*) FROM memory_vec').fetchone()[0] == 0
    assert roster.conn.execute('SELECT count(*) FROM memory_actor').fetchone()[0] == 0
    assert roster.conn.execute("SELECT count(*) FROM memory_fts WHERE memory_fts MATCH 'unsupportedvectorword'").fetchone()[0] == 0
    assert roster.conn.execute('SELECT count(*) FROM realm_memory_dependency').fetchone()[0] == 0


def test_keyed_output_audit_failure_restores_old_vector_and_indexes(roster, clock):
    fact = make_fact(roster)
    ref = f'realm:fact:{fact.id}'
    inputs = admit(roster, ref)
    old = replace(row(fact), key='vector-key', actors=(101,), embedding=packed(0), evidence=(ref,))
    mid = roster.insert_memory(old)
    before, before_indexes, before_links = roster.get_memories([mid])[0], indexes(roster, mid), links(roster, mid)
    assert before_indexes[0] == [(mid, 0, packed(0))]
    writer = MemoryWriter(roster, Embed(), clock, human=lambda: HUMAN)
    new = replace(row(fact), key='vector-key', text='unsupportedreplacementword', actors=(102,), embedding=packed(1))
    with pytest.raises(RealmLineageError):
        writer.put(new, realm_inputs=inputs)
    assert roster.get_memories([mid])[0] == before
    assert indexes(roster, mid) == before_indexes and links(roster, mid) == before_links
    assert roster.conn.execute("SELECT count(*) FROM memory_fts WHERE memory_fts MATCH 'unsupportedreplacementword'").fetchone()[0] == 0


def test_async_dependency_db_failure_propagates_and_removes_vector(roster, clock):
    fact = make_fact(roster)
    inputs = admit(roster, f'realm:fact:{fact.id}')
    with roster.transaction():
        roster.conn.execute("CREATE TRIGGER fail_writer_dependency BEFORE INSERT ON realm_memory_dependency BEGIN SELECT RAISE(ABORT, 'injected'); END")
    writer = MemoryWriter(roster, Embed(vector=packed(0)), clock, human=lambda: HUMAN)
    with pytest.raises(sqlite3.IntegrityError):
        run(writer.write(candidate(fact, actors=(101,)), realm_inputs=inputs))
    assert roster.conn.execute('SELECT count(*) FROM memory').fetchone()[0] == 0
    assert roster.conn.execute('SELECT count(*) FROM memory_vec').fetchone()[0] == 0
    assert roster.conn.execute('SELECT count(*) FROM memory_actor').fetchone()[0] == 0
    assert roster.conn.execute('SELECT count(*) FROM realm_memory_dependency').fetchone()[0] == 0


def test_memory_mode_merges_selected_twin_and_unions_old_and_new_facts(roster, clock):
    first = make_fact(roster)
    second = make_fact(roster, 'arena_result_v1')
    writer = MemoryWriter(roster, Embed(vector=packed(0)), clock, human=lambda: HUMAN)
    old = replace(row(first), t_created=int(clock.now()), t_accessed=int(clock.now()), embedding=packed(1))
    twin = writer.put(old, shown=(f'realm:fact:{first.id}',))
    inputs = admit(roster, f'realm:fact:{first.id}', f'realm:fact:{second.id}')
    result = run(writer.write(candidate(first), realm_inputs=inputs))
    assert (result.id, result.action) == (twin, 'merged')
    assert links(roster, twin) == tuple(sorted((first.id, second.id)))
    assert roster.get_memories([twin])[0].merged_n == 2
    assert indexes(roster, twin)[0] == [(twin, 0, packed(1))]


def test_memory_mode_rejects_invalid_actual_twin_and_restores_everything(roster, clock):
    first = make_fact(roster)
    second = make_fact(roster, 'arena_result_v1')
    writer = MemoryWriter(roster, Embed(vector=packed(0)), clock, human=lambda: HUMAN)
    old = replace(row(first), t_event=int(first.ts) + 1, t_created=int(clock.now()),
                  t_accessed=int(clock.now()), embedding=packed(1))
    twin = writer.put(old, shown=(f'realm:fact:{first.id}',))
    before, before_indexes, before_links = roster.get_memories([twin])[0], indexes(roster, twin), links(roster, twin)
    inputs = admit(roster, f'realm:fact:{first.id}', f'realm:fact:{second.id}')
    result = run(writer.write(candidate(first), realm_inputs=inputs))
    assert result.action == 'rejected' and result.id is None
    assert roster.get_memories([twin])[0] == before
    assert indexes(roster, twin) == before_indexes and links(roster, twin) == before_links
    assert roster.conn.execute('SELECT count(*) FROM memory').fetchone()[0] == 1
