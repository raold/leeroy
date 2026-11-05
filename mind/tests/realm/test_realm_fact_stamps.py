"""Current fact snapshots built through the real qualified FactStore producer."""
from dataclasses import replace
import hashlib
import json
import sqlite3

import pytest

from leeroy_mind.realm.fact_store import FactStore
from leeroy_mind.realm.memory_eligibility import (
    merge_realm_inputs_in_unit, realm_inputs_in_unit, revalidate_realm_inputs_in_unit,
)
from leeroy_mind.realm.provenance import audit_realm_reads_in_unit, public_fact_in_unit
from leeroy_mind.realm.records import FactRow, RealmFactReadStamp, RealmInputs, Rejected
from leeroy_mind.storage.store import Store
from test_realm_live_proof import HUMAN, TYPES, canonical, insert, observe, roster


def make_fact(store, policy='bg_result_v1', **changes):
    with store.transaction():
        fact = FactStore(store).put_fact_in_unit(observe(store, policy, **changes), human=HUMAN)
        assert type(fact) is FactRow
        return fact


def admit(store, *refs):
    with store.transaction():
        return realm_inputs_in_unit(store, refs, human=HUMAN, mode='memory')


def revalidate(store, manifest):
    with store.transaction():
        return revalidate_realm_inputs_in_unit(store, manifest, human=HUMAN)


def annotation(store, ref, text='', **changes):
    values = dict(kind='noticed', root=ref, about=None, text=text, secret=0, holder=0, created=1000.0)
    values.update(changes)
    return insert(store, 'fact', **values)


def node_hash(kind, key, projection):
    body = canonical(dict(kind=kind, key=key, projection_version=1, projection=projection)).encode()
    return hashlib.sha256(b'realm-proof-node-v1\0' + len(body).to_bytes(8, 'big') + body).hexdigest()


@pytest.mark.parametrize('policy', tuple(TYPES))
def test_fact_only_has_exact_origin_revision_projection_and_empty_annotation_stamp(roster, policy):
    fact = make_fact(roster, policy)
    manifest = admit(roster, f'realm:fact:{fact.id}')
    assert type(manifest) is RealmInputs
    assert manifest.stamps == () and manifest.refs == (f'realm:fact:{fact.id}',)
    with roster.transaction():
        row, proof = public_fact_in_unit(roster, fact.id, human=HUMAN)
        raw = dict(roster.conn.execute('SELECT id,kind,text,source,ref,public,ts FROM realm_fact WHERE id=?', (fact.id,)).fetchone())
        origin = dict(roster.conn.execute('SELECT * FROM realm_fact_origin WHERE fact=?', (fact.id,)).fetchone())
    privacy_key = hashlib.sha256(canonical([f'realm:fact:{fact.id}']).encode()).hexdigest()
    privacy = dict(kind='privacy_set', key=privacy_key, projection_version=1,
                   sha256=node_hash('privacy_set', privacy_key, []))
    projected = dict(raw, revision=origin['revision'], policy=origin['policy'], assurance=origin['assurance'],
                     text_sha256=origin['text_sha256'], qualification=json.loads(origin['qualification_json']),
                     proof_sha256=proof.sha256, privacy=privacy)
    assert manifest.fact_stamps == (RealmFactReadStamp(fact.id, row.revision,
        node_hash('realm_fact', str(fact.id), projected), proof.sha256),)
    assert revalidate(roster, manifest)


def test_same_text_revision_change_never_refreshes_a_shown_fact(roster):
    fact = make_fact(roster)
    shown = admit(roster, f'realm:fact:{fact.id}')
    with roster.transaction():
        roster.conn.execute('UPDATE realm_fact_origin SET revision=revision+1 WHERE fact=?', (fact.id,))
    changed = admit(roster, f'realm:fact:{fact.id}')
    assert type(changed) is RealmInputs
    assert changed.fact_stamps[0].proof_sha256 == shown.fact_stamps[0].proof_sha256
    assert changed.fact_stamps[0].fact_sha256 != shown.fact_stamps[0].fact_sha256
    assert not revalidate(roster, shown)
    with roster.transaction():
        assert merge_realm_inputs_in_unit(roster, (shown, changed), human=HUMAN) == Rejected('proof_changed')


@pytest.mark.parametrize('mutation', [
    'UPDATE realm_fact SET public=0', "UPDATE realm_fact_origin SET state='revoked'",
    'UPDATE realm_fact SET ts=ts+1', "UPDATE realm_fact SET text='Changed.'",
    "UPDATE realm_fact_origin SET proof_json='{}'", 'DELETE FROM realm_fact_origin',
    'DELETE FROM realm_knows WHERE guild=60', "UPDATE realm_knows SET how='server' WHERE guild=50",
    'UPDATE realm_knows SET since=since+1', "UPDATE game_event SET payload='{}'",
    "UPDATE realm_source SET row_sha256='0000000000000000000000000000000000000000000000000000000000000000'",
])
def test_current_origin_source_and_complete_knowledge_mutations_fail(roster, mutation):
    fact = make_fact(roster)
    shown = admit(roster, f'realm:fact:{fact.id}')
    with roster.transaction():
        roster.conn.execute(mutation)
    assert not revalidate(roster, shown)
    assert type(admit(roster, f'realm:fact:{fact.id}')) is Rejected


@pytest.mark.parametrize('text', ['', 'Horde won battleground type 1.'])
def test_fact_annotation_insertion_changes_stamp_even_when_still_public(roster, text):
    fact = make_fact(roster)
    shown = admit(roster, f'realm:fact:{fact.id}')
    with roster.transaction():
        annotation(roster, f'realm:fact:{fact.id}', text)
    current = admit(roster, f'realm:fact:{fact.id}')
    assert type(current) is RealmInputs
    assert current.fact_stamps[0].proof_sha256 == shown.fact_stamps[0].proof_sha256
    assert current.fact_stamps[0].fact_sha256 != shown.fact_stamps[0].fact_sha256
    assert not revalidate(roster, shown)


@pytest.mark.parametrize('changes', [dict(secret=1), dict(kind='life_news'), dict(kind='real_name'),
    dict(text='Synthetic Private'), dict(text='We kissed.'), dict(text='An unsupported claim.')])
def test_fact_annotations_are_screened_from_current_rows(roster, changes):
    fact = make_fact(roster)
    shown = admit(roster, f'realm:fact:{fact.id}')
    with roster.transaction():
        annotation(roster, f'realm:fact:{fact.id}', **changes)
    assert not revalidate(roster, shown)


def test_same_policy_distinct_qualifications_share_one_batch_without_virtual_key_collision(roster):
    first = make_fact(roster)
    second = make_fact(roster, payload={'ts': 1001000}, captured=1011.0)
    both = admit(roster, f'realm:fact:{first.id}', f'realm:fact:{second.id}')
    assert type(both) is RealmInputs and len(both.fact_stamps) == 2
    assert both.fact_stamps[0].proof_sha256 != both.fact_stamps[1].proof_sha256
    assert revalidate(roster, both)


def test_many_individually_valid_origins_share_one_string_budget(roster):
    facts = [make_fact(roster, payload={'ts': 1000000 + n}, captured=1020.0) for n in range(30)]
    with roster.transaction():
        # Whitespace does not alter the strict parsed event projection, but its
        # raw bytes must still consume the one pre-parse operation budget.
        roster.conn.execute('UPDATE game_event SET payload=payload || ?', (' ' * 3000,))
    singles = [admit(roster, f'realm:fact:{fact.id}') for fact in facts]
    assert all(type(item) is RealmInputs for item in singles)
    assert admit(roster, *(f'realm:fact:{fact.id}' for fact in facts)) == Rejected('graph_limit')
    with roster.transaction():
        assert merge_realm_inputs_in_unit(roster, tuple(singles), human=HUMAN) == Rejected('graph_limit')


def test_privacy_annotation_limit_is_global_across_origin_and_fact_queries(roster):
    fact = make_fact(roster)
    with roster.transaction():
        # Raw-event annotations change the source proof, so preserve that exact
        # original proof and spread the global overrun across two fact sets.
        for index in range(101):
            annotation(roster, f'realm:fact:{fact.id}', holder=index)
    other = make_fact(roster, payload={'ts': 1000001})
    with roster.transaction():
        for index in range(100):
            annotation(roster, f'realm:fact:{other.id}', holder=index)
    assert type(admit(roster, f'realm:fact:{fact.id}')) is RealmInputs
    assert type(admit(roster, f'realm:fact:{other.id}')) is RealmInputs
    assert admit(roster, f'realm:fact:{fact.id}', f'realm:fact:{other.id}') == Rejected('graph_limit')


def test_batch_actual_unit_exact_inputs_and_database_faults(roster):
    fact = make_fact(roster)
    ref = f'realm:fact:{fact.id}'
    with pytest.raises(RuntimeError):
        audit_realm_reads_in_unit(roster, (ref,), human=HUMAN)
    peer = Store(roster.conn, clock=roster.clock)
    with roster.transaction():
        with pytest.raises(RuntimeError):
            audit_realm_reads_in_unit(peer, (ref,), human=HUMAN)
        for refs in ([ref], (ref, ref), ('event:1',)):
            assert audit_realm_reads_in_unit(roster, refs, human=HUMAN) == Rejected('invalid_input')
        with pytest.raises(RuntimeError):
            merge_realm_inputs_in_unit(peer, (), human=HUMAN)
        roster.conn.execute('DROP TABLE realm_knows')
        with pytest.raises(sqlite3.OperationalError):
            audit_realm_reads_in_unit(roster, (ref,), human=HUMAN)


def test_exclusion_cannot_carry_fact_snapshots_and_memory_forgery_fails(roster):
    fact = make_fact(roster)
    real = admit(roster, f'realm:fact:{fact.id}')
    assert not revalidate(roster, replace(real, mode='exclude'))
    assert not revalidate(roster, replace(real, fact_stamps=()))
    forged = replace(real.fact_stamps[0], fact_sha256='a' * 64)
    assert not revalidate(roster, replace(real, fact_stamps=(forged,)))
    object.__setattr__(forged, 'revision', True)
    assert not revalidate(roster, replace(real, fact_stamps=(forged,)))


def test_another_independently_valid_claim_cannot_replace_an_old_shown_fact(roster):
    first = make_fact(roster)
    old = admit(roster, f'realm:fact:{first.id}')
    second = make_fact(roster, payload={'ts': 1001000, 'winner': 'alliance'}, captured=1011.0)
    with roster.transaction():
        values = dict(roster.conn.execute('SELECT * FROM realm_fact_origin WHERE fact=?', (second.id,)).fetchone())
        values.pop('fact')
        values['revision'] = 2
        roster.conn.execute('DELETE FROM realm_fact_origin WHERE fact=?', (second.id,))
        roster.conn.execute('UPDATE realm_fact_origin SET ' + ','.join(f'{key}=?' for key in values) + ' WHERE fact=?',
                            (*values.values(), first.id))
        roster.conn.execute('UPDATE realm_fact SET kind=?,text=?,source=?,ref=?,public=1,ts=? WHERE id=?',
                            (second.kind, second.text, second.source, second.ref, second.ts, first.id))
        roster.conn.execute('DELETE FROM realm_knows WHERE fact=?', (first.id,))
        roster.conn.execute('INSERT INTO realm_knows(fact,guild,since,how) '
                            'SELECT ?,guild,since,how FROM realm_knows WHERE fact=?', (first.id, second.id))
    current = admit(roster, f'realm:fact:{first.id}')
    assert type(current) is RealmInputs
    assert current.fact_stamps[0].fact_id == first.id and current != old
    assert not revalidate(roster, old)


def test_annotation_cap_exactly_200_then_201_rejects_without_prefix(roster):
    fact = make_fact(roster)
    with roster.transaction():
        for holder in range(200):
            annotation(roster, f'realm:fact:{fact.id}', holder=holder)
    assert type(admit(roster, f'realm:fact:{fact.id}')) is RealmInputs
    with roster.transaction():
        annotation(roster, f'realm:fact:{fact.id}', holder=200)
    assert admit(roster, f'realm:fact:{fact.id}') == Rejected('graph_limit')


def test_global_annotation_cap_also_counts_complete_raw_event_privacy(roster):
    facts = []
    for number in range(2):
        with roster.transaction():
            observation = observe(roster, payload={'ts': 1000000 + number})
            for holder in range(101):
                annotation(roster, f'event:{observation.receipt.row_id}', holder=holder)
            facts.append(FactStore(roster).put_fact_in_unit(observation, human=HUMAN))
    assert all(type(fact) is FactRow for fact in facts)
    assert all(type(admit(roster, f'realm:fact:{fact.id}')) is RealmInputs for fact in facts)
    assert admit(roster, *(f'realm:fact:{fact.id}' for fact in facts)) == Rejected('graph_limit')
