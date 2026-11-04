"""Direct live proofs over disposable actual Store rows, never approval stubs."""
import copy
from dataclasses import asdict, fields, replace
import hashlib
import json

import pytest

from conftest import make_persona, seed_test_cast
from test_realm_provenance import graph as stored_graph
from leeroy_mind import protocol
from leeroy_mind.human import HumanIdentity
from leeroy_mind.ingress import RowReceipt, _wire_origin, game_row_binding
from leeroy_mind.realm.actions import ActionContext
from leeroy_mind.realm.protocol import ArenaEnd, ChannelChat, PvpKill, RealmKill, Wg
from leeroy_mind.realm.provenance import (
    audit_public_in_unit, capture_qualification_in_unit, public_fact_draft,
    public_fact_in_unit, public_proof_from_json, public_proof_to_json,
    render_public, revalidate_public_in_unit, validate_live_observation_in_unit,
)
from leeroy_mind.realm.records import PublicProof, QualificationSnapshot, RealmObservation, Rejected
from leeroy_mind.realm.source_store import claim_source_in_unit

HUMAN = HumanIdentity(10, 'Testmaster', 'Private Nickname', 'Synthetic Private', True)
CONTEXT = ActionContext(True, True, True, 3, 'proof-test-process', 11, 50, 10)
TYPES = {
    'bg_result_v1': ('bg_end', protocol.BgEnd),
    'arena_result_v1': ('arena_end', ArenaEnd),
    'wg_result_v1': ('wg', Wg),
    'pvp_result_v1': ('pvp_kill', PvpKill),
    'channel_raid_topic_v1': ('channel_chat', ChannelChat),
    'verified_raid_kill_v1': ('realm_kill', RealmKill),
}
PAYLOADS = {
    'bg_result_v1': dict(ts=1000000, bg_type=1, winner='horde', members=[201, 101, 10]),
    'arena_result_v1': dict(ts=1000000, arena_type=3, winner_team=91, loser_team=92,
                            winner_change=0, loser_change=-15, members=[201, 10]),
    'wg_result_v1': dict(ts=1000000, state='end', defender='alliance', seconds=0, update_ms=1.0),
    'pvp_result_v1': dict(ts=1000000, killer=201, victim=10, map=0, zone=0),
    'channel_raid_topic_v1': dict(ts=1000000, channel='trade', from_guid=10,
        from_name='Testmaster', zone=0, text='Ready for Icecrown Citadel.', via_bridge=True),
    'verified_raid_kill_v1': dict(ts=1000000, kill_id='a' * 32 + ':1', native_epoch=29,
        guild_id=50, human_guid=10, killed_at_ms=999990, verified_at_ms=999999,
        fight_started_at_ms=999000, fight_ms=2345, attempts=0, map=631, instance_id=9, boss_id=0,
        difficulty=0, size=10, heroic=False, human_present=True, cast_members=[101, 102, 103, 104, 105]),
}
EXPECTED = {
    'bg_result_v1': ('Horde won battleground type 1.', [[50, 'present'], [60, 'present']]),
    'arena_result_v1': ('Arena team 91 beat team 92 in 3v3.', [[50, 'present'], [60, 'present']]),
    'wg_result_v1': ('Alliance controls Wintergrasp after the battle.', [[50, 'own'], [60, 'server']]),
    'pvp_result_v1': ('Character 201 defeated character 10 in zone 0.', [[50, 'present'], [60, 'present']]),
    'channel_raid_topic_v1': ('A public trade message discussed Icecrown Citadel.', [[50, 'own']]),
    'verified_raid_kill_v1': ('The guild killed Lord Marrowgar.', [[50, 'own'], [60, 'server']]),
}


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def insert(store, table, **values):
    return store.conn.execute(f"INSERT INTO {table}({','.join(values)}) VALUES ({','.join('?' for _ in values)})",
                              tuple(values.values())).lastrowid


@pytest.fixture
def roster(store):
    seed_test_cast(store)
    with store.transaction():
        insert(store, 'cast_member', guid=10, name='Testmaster', race=2, **{'class': 1},
               gender=0, level=80, rank=0, online=1, updated=1000.0)
        for identifier, status in ((50, 'active'), (60, 'active'), (70, 'retired')):
            insert(store, 'realm_guild', id=identifier, role='progression', faction='horde',
                   name='Synthetic Guild', added=1000.0, status=status)
        for guid, guild, status in ((201, 60, 'active'), (202, 60, 'retired'), (203, 70, 'active')):
            insert(store, 'realm_actor', guid=guid, guild=guild, name='Rivalname',
                   role='officer', added=1000.0, status=status)
    return store


def observe(store, policy='bg_result_v1', *, payload=None, raw_extra=None, transport=7,
            received=1010.0, backlog=False, human=HUMAN, context=CONTEXT, captured=1010.0):
    data = copy.deepcopy(PAYLOADS[policy])
    if payload:
        data.update(payload)
    wire_type, cls = TYPES[policy]
    typed = {key: tuple(value) if key in ('members', 'cast_members') else value for key, value in data.items()}
    event = cls(**typed)
    envelope = dict(v=1, type=wire_type, **data, **(raw_extra or {}))
    wire = _wire_origin(canonical(envelope), received_at=received, transport_epoch=transport)
    assert wire is not None
    row_id = insert(store, 'game_event', ts=received, type=wire_type, guid=None,
                    payload=canonical(data), session_id=None)
    receipt = RowReceipt('game_event', row_id, game_row_binding(row_id, wire_type, data), wire, backlog)
    qualification = capture_qualification_in_unit(store, human=human, event=event, receipt=receipt,
        context=context, confirmed_human_guid=human.guid, transport_epoch=transport, captured_at=captured)
    assert type(qualification) is QualificationSnapshot, qualification
    return RealmObservation(event, receipt, context, qualification)


def changed_observation(store, observation, policy, **changes):
    """Rebind an adversarial complete row to an exact real protocol class."""
    event = copy.copy(observation.event)
    for key, value in changes.items():
        object.__setattr__(event, key, tuple(value) if key in ('members', 'cast_members') else value)
    payload = {field.name: getattr(event, field.name) for field in fields(type(event))}
    payload = json.loads(canonical(payload))
    wire_type = TYPES[policy][0]
    wire = _wire_origin(canonical(dict(v=1, type=wire_type, **payload)),
                        received_at=1011.0, transport_epoch=7)
    assert wire is not None
    row_id = insert(store, 'game_event', ts=1011.0, type=wire_type, guid=None,
                    payload=canonical(payload), session_id=None)
    receipt = RowReceipt('game_event', row_id, game_row_binding(row_id, wire_type, payload), wire, False)
    return replace(observation, event=event, receipt=receipt)


def prepare(store, policy='bg_result_v1', **changes):
    observation = observe(store, policy, **changes)
    claimed = claim_source_in_unit(store, observation, human=HUMAN, policy=policy)
    assert type(claimed) is tuple and claimed[1] is True, claimed
    source = claimed[0]
    proof = audit_public_in_unit(store, human=HUMAN, policy=policy,
        refs=(f'event:{source.row_id}',), observation=observation)
    assert type(proof) is PublicProof, proof
    return observation, source, proof


def origin_rows(store, source, proof, *, revision=1):
    """Raw persisted-origin fixtures exercise the reader, not a producer stub."""
    draft = public_fact_draft(proof, source)
    text = render_public(proof.policy, dict(draft.output_fields))
    kind = {'bg_result_v1': 'bg_result', 'arena_result_v1': 'arena_result', 'wg_result_v1': 'wg_result',
            'pvp_result_v1': 'pvp_kill', 'channel_raid_topic_v1': 'channel_topic',
            'verified_raid_kill_v1': 'kill'}[proof.policy]
    fact_id = insert(store, 'realm_fact', kind=kind, text=text,
        source='channel' if proof.policy == 'channel_raid_topic_v1' else 'event',
        ref=proof.roots[0], public=1, ts=source.server_ms / 1000)
    insert(store, 'realm_fact_origin', fact=fact_id, producer_key=draft.producer_key, source=source.id,
        policy=proof.policy, assurance=proof.assurance, proof_json=public_proof_to_json(proof),
        qualification_json=proof.qualification_json, text_sha256=hashlib.sha256(text.encode()).hexdigest(),
        revision=revision, state='active')
    for guild, how in json.loads(proof.qualification_json)['known_by']:
        insert(store, 'realm_knows', fact=fact_id, guild=guild, since=source.server_ms / 1000, how=how)
    return fact_id


@pytest.mark.parametrize('policy', list(TYPES))
def test_six_actual_direct_sources_have_fixed_text_derived_knowledge_and_exact_four_nodes(roster, policy):
    with roster.transaction():
        observation, source, proof = prepare(roster, policy)
        qualification = json.loads(proof.qualification_json)
        assert render_public(policy, json.loads(proof.output_fields_json)) == EXPECTED[policy][0]
        assert qualification['known_by'] == EXPECTED[policy][1]
        assert qualification['snapshot']['cast'] == [101, 102, 103, 104, 105]
        assert [actor['guid'] for actor in qualification['snapshot']['actors']] == [201]
        assert len(proof.nodes) == 4
        assert {node.kind for node in proof.nodes} == {'game_event', 'realm_source', 'qualification', 'privacy_set'}
        assert proof.roots == (f'event:{observation.receipt.row_id}',) and proof.sources == (source.id,)
        assert revalidate_public_in_unit(roster, proof, human=HUMAN) == proof
        assert public_proof_from_json(public_proof_to_json(proof)) == proof
        draft = public_fact_draft(proof, source)
        assert draft.producer_key == f'event:{source.source_key}:{policy}'
        assert draft.source_at == 1000.0 and draft.template_id == policy and draft.public is True
        assert draft.output_fields == tuple(sorted(json.loads(proof.output_fields_json).items()))
        fact_id = origin_rows(roster, source, proof, revision=9)
        row, read_proof = public_fact_in_unit(roster, fact_id, human=HUMAN)
        assert row.revision == 9 and row.public is True and read_proof == proof
        for table in ('realm_kill', 'realm_announcement', 'realm_memory_dependency'):
            assert roster.conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == 0


def test_captured_epochs_are_three_independent_namespaces_and_later_roster_cannot_repair_history(roster):
    with roster.transaction():
        observation, source, proof = prepare(roster, 'verified_raid_kill_v1')
        qualification = json.loads(proof.qualification_json)
        assert qualification['snapshot']['transport_epoch'] == '7'
        assert qualification['context']['connection_epoch'] == '11'
        assert observation.event.native_epoch == 29
        roster.conn.execute("UPDATE realm_actor SET status='retired'")
        roster.conn.execute("UPDATE realm_guild SET status='retired'")
        roster.conn.execute('DELETE FROM persona_core')
        assert revalidate_public_in_unit(roster, proof, human=replace(HUMAN, guid=99)) == proof
        assert source.received_at == 1010.0


def test_capture_excludes_all_stored_personas_kicks_and_unreadable_cast_without_cache(roster):
    with roster.transaction():
        roster.conn.execute("UPDATE persona_core SET json='{' WHERE guid=101")
        roster.conn.execute('DELETE FROM cast_member WHERE guid=102')
        insert(roster, 'realm_kick', guid=103, ts=10.0, rank=3)
        for guid in (101, 102, 103):
            insert(roster, 'realm_actor', guid=guid, guild=60, name='Former', role='ganker', added=1.0)
        insert(roster, 'realm_actor', guid=204, guild=60, name='Kicked', role='applicant', added=1.0)
        insert(roster, 'realm_kick', guid=204, ts=10.0, rank=3)
        observation = observe(roster)
        assert observation.qualification.cast == (104, 105)
        assert tuple(actor.guid for actor in observation.qualification.actors) == (201,)
        # A fresh read sees a repair in this same unit, without cache invalidation.
        from leeroy_mind.personas.io import canonical_json
        roster.conn.execute('UPDATE persona_core SET json=? WHERE guid=101', (canonical_json(make_persona(101, 'Arkon')),))
        assert 101 in observe(roster).qualification.cast


def test_human_speaker_name_follows_meta_then_cast_and_hashes_only_declared_projection(roster):
    with roster.transaction():
        roster.set_meta('human_guid', '10'); roster.set_meta('human_name', 'Chatmaster')
        human = replace(HUMAN, character_name='Chatmaster')
        observation = observe(roster, 'channel_raid_topic_v1', human=human, payload={'from_name': 'Chatmaster'})
        snapshot = observation.qualification
        assert snapshot.speaker_name_source == 'human_meta'
        expected = hashlib.sha256(b'realm-speaker-name-v1\0' + canonical(
            dict(source='human_meta', guid=10, name='Chatmaster')).encode()).hexdigest()
        assert snapshot.speaker_name_sha256 == expected
        roster.set_meta('human_guid', '999')
        fallback = observe(roster, 'channel_raid_topic_v1')
        assert fallback.qualification.speaker_name_source == 'cast_member'
        rejected = capture_qualification_in_unit(roster, human=human, event=fallback.event,
            receipt=fallback.receipt, context=CONTEXT, confirmed_human_guid=10, transport_epoch=7, captured_at=1.0)
        assert type(rejected) is Rejected
        assert HUMAN.nickname not in repr(snapshot) and HUMAN.real_name not in repr(snapshot)


@pytest.mark.parametrize('form', ['Ready for Icecrown Citadel.', 'Looking for a group for Icecrown Citadel.',
                                  'Anyone for Icecrown Citadel?', '  aNyOnE fOr iCeCrOwN cItAdEl?  '])
def test_channel_three_whole_forms_and_actor_speaker_are_topics_only(roster, form):
    with roster.transaction():
        observation, source, proof = prepare(roster, 'channel_raid_topic_v1',
            payload=dict(from_guid=201, from_name='Rivalname', text=form, via_bridge=True))
        assert json.loads(proof.qualification_json)['known_by'] == [[60, 'own']]
        assert observation.qualification.speaker_name_source == 'realm_actor'
        assert 'Rivalname' not in render_public(proof.policy, json.loads(proof.output_fields_json))
        assert source.event_type == 'channel_chat'


@pytest.mark.parametrize('text', ['Ready for Icecrown Citadel. extra', '|cffffffffReady for Icecrown Citadel.|r',
    '\tReady for Icecrown Citadel.', 'Ready  for Icecrown Citadel.', 'Ready for Icecrown Citadel.\n',
    'Ready for Icecrown Citadel.\u200b', 'Synthetic Private Ready for Icecrown Citadel.',
    'I love you. Ready for Icecrown Citadel.', 'Ready for Icecrown Citadel?'])
def test_channel_raw_privacy_sanitizer_and_whole_input_fail_before_claim(roster, text):
    with roster.transaction():
        observation = observe(roster, 'channel_raid_topic_v1', payload={'text': text})
        result = claim_source_in_unit(roster, observation, human=HUMAN, policy='channel_raid_topic_v1')
        assert type(result) is Rejected
        assert roster.conn.execute('SELECT count(*) FROM realm_source').fetchone()[0] == 0


@pytest.mark.parametrize('policy,changes', [
    ('bg_result_v1', {'bg_type': True}), ('bg_result_v1', {'winner': 'Horde'}),
    ('bg_result_v1', {'members': [10, 10]}), ('bg_result_v1', {'members': [0]}),
    ('bg_result_v1', {'members': list(range(1, 130))}), ('bg_result_v1', {'bg_type': 1 << 32}),
    ('arena_result_v1', {'arena_type': 4}), ('arena_result_v1', {'winner_team': 92}),
    ('arena_result_v1', {'winner_change': 1 << 31}), ('arena_result_v1', {'loser_change': True}),
    ('wg_result_v1', {'state': 'start'}), ('wg_result_v1', {'seconds': True}),
    ('wg_result_v1', {'update_ms': -1.0}), ('wg_result_v1', {'update_ms': 1}),
    ('pvp_result_v1', {'victim': 201}), ('pvp_result_v1', {'map': -1}),
    ('pvp_result_v1', {'zone': True}), ('channel_raid_topic_v1', {'via_bridge': 1}),
    ('channel_raid_topic_v1', {'from_name': 'Othername'}),
    ('channel_raid_topic_v1', {'channel': 'guild'}),
    ('verified_raid_kill_v1', {'boss_id': 999}), ('verified_raid_kill_v1', {'guild_id': 60}),
    ('verified_raid_kill_v1', {'fight_started_at_ms': 0, 'fight_ms': 0}),
    ('verified_raid_kill_v1', {'fight_started_at_ms': 999991}),
    ('verified_raid_kill_v1', {'fight_ms': 3600001}), ('verified_raid_kill_v1', {'fight_ms': True}),
    ('verified_raid_kill_v1', {'human_present': False}),
    ('verified_raid_kill_v1', {'difficulty': 2}),
    ('verified_raid_kill_v1', {'native_epoch': 1 << 64}),
    ('verified_raid_kill_v1', {'kill_id': 'a' * 32 + ':01'}),
])
def test_every_policy_rejects_exact_type_schema_and_catalogue_violations(roster, policy, changes):
    with roster.transaction():
        observation = observe(roster, policy)
        changed = changed_observation(roster, observation, policy, **changes)
        assert type(validate_live_observation_in_unit(roster, changed, human=HUMAN, policy=policy)) is Rejected
        assert type(claim_source_in_unit(roster, changed, human=HUMAN, policy=policy)) is Rejected
        assert roster.conn.execute('SELECT count(*) FROM realm_source').fetchone()[0] == 0


@pytest.mark.parametrize('duration', [(0, -1), (1, 0), (999990, 3600000)])
def test_native_duration_pair_is_retained_without_inferred_subtraction(roster, duration):
    with roster.transaction():
        _, _, proof = prepare(roster, 'verified_raid_kill_v1', payload={
            'fight_started_at_ms': duration[0], 'fight_ms': duration[1]})
        assert render_public(proof.policy, json.loads(proof.output_fields_json)) == 'The guild killed Lord Marrowgar.'
        assert set(json.loads(proof.output_fields_json)) == {'catalogue', 'map', 'boss_id'}


def test_kill_native_without_human_requires_five_captured_readable_cast(roster):
    with roster.transaction():
        _, _, proof = prepare(roster, 'verified_raid_kill_v1', payload={
            'human_present': False, 'fight_started_at_ms': 0, 'fight_ms': -1})
        qualification = json.loads(proof.qualification_json)
        assert qualification['participants'] == [101, 102, 103, 104, 105]
        assert qualification['native_present'] == qualification['qualified_cast']
        observation = observe(roster, 'verified_raid_kill_v1', payload={
            'human_present': False, 'fight_started_at_ms': 0, 'fight_ms': -1, 'cast_members': [101, 102, 103, 104]})
        assert type(claim_source_in_unit(roster, observation, human=HUMAN, policy=proof.policy)) is Rejected


def test_zero_unmeasured_arena_deltas_never_become_rating_prose(roster):
    with roster.transaction():
        _, _, proof = prepare(roster, 'arena_result_v1', payload={'winner_change': 0, 'loser_change': 0})
        assert json.loads(proof.output_fields_json) == {'arena_type': 3, 'winner_team': 91, 'loser_team': 92}
        assert render_public(proof.policy, json.loads(proof.output_fields_json)) == 'Arena team 91 beat team 92 in 3v3.'


@pytest.mark.parametrize('name', ['Éowyn', 'A', 'Abcdefghijklm', 'Two Words', 'Name\u200b'])
def test_channel_name_conservative_ascii_subset_is_checked_after_real_capture(roster, name):
    with roster.transaction():
        roster.conn.execute('UPDATE realm_actor SET name=? WHERE guid=201', (name,))
        observation = observe(roster, 'channel_raid_topic_v1', payload={'from_guid': 201, 'from_name': name})
        assert type(validate_live_observation_in_unit(roster, observation, human=HUMAN,
                                                      policy='channel_raid_topic_v1')) is Rejected


def test_current_privacy_screens_both_raw_names_and_derived_catalogue_output(roster):
    with roster.transaction():
        roster.conn.execute('UPDATE realm_actor SET name=? WHERE guid=201', (HUMAN.real_name,))
        observation = observe(roster, 'channel_raid_topic_v1', payload={
            'from_guid': 201, 'from_name': HUMAN.real_name})
        assert validate_live_observation_in_unit(roster, observation, human=HUMAN,
            policy='channel_raid_topic_v1') == Rejected('real_name')
        _, source, proof = prepare(roster, 'verified_raid_kill_v1')
        changed_human = replace(HUMAN, real_name='Marrowgar')
        assert revalidate_public_in_unit(roster, proof, human=changed_human) == Rejected('real_name')
        fact_id = origin_rows(roster, source, proof)
        assert public_fact_in_unit(roster, fact_id, human=changed_human) == Rejected('real_name')


def test_empty_annotation_insertion_changes_old_proof_and_secret_annotation_revokes(roster):
    with roster.transaction():
        observation, source, proof = prepare(roster)
        annotation = dict(kind='noticed', root=proof.roots[0], about=10, text='', secret=0, holder=0, created=1012.0)
        insert(roster, 'fact', **annotation)
        assert revalidate_public_in_unit(roster, proof, human=HUMAN) == Rejected('proof_changed')
        current = audit_public_in_unit(roster, human=HUMAN, policy=proof.policy,
            refs=proof.roots, observation=observation)
        assert type(current) is PublicProof and current != proof
        roster.conn.execute('UPDATE fact SET secret=1')
        assert revalidate_public_in_unit(roster, current, human=HUMAN) == Rejected('private_evidence')


@pytest.mark.parametrize('text,accepted', [('Horde won battleground type 1.', True),
    ('Horde won battleground type 1. extra', False), ('A private tale.', False),
    ('Synthetic Private', False), ('', True)])
def test_annotation_prose_is_finite_and_screened_before_output(roster, text, accepted):
    with roster.transaction():
        observation, source, proof = prepare(roster)
        insert(roster, 'fact', kind='noticed', root=proof.roots[0], about=10, text=text,
               secret=0, holder=0, created=1012.0)
        result = audit_public_in_unit(roster, human=HUMAN, policy=proof.policy,
                                     refs=proof.roots, observation=observation)
        assert (type(result) is PublicProof) is accepted


def test_complete_privacy_rows_and_examined_text_budget_are_never_prefix_truncated(roster):
    with roster.transaction():
        observation, source, proof = prepare(roster)
        for holder in range(200):
            insert(roster, 'fact', kind='noticed', root=proof.roots[0], about=10, text='',
                   secret=0, holder=holder, created=1012.0)
        assert type(audit_public_in_unit(roster, human=HUMAN, policy=proof.policy,
            refs=proof.roots, observation=observation)) is PublicProof
        insert(roster, 'fact', kind='noticed', root=proof.roots[0], about=10, text='',
               secret=0, holder=200, created=1012.0)
        assert audit_public_in_unit(roster, human=HUMAN, policy=proof.policy,
            refs=proof.roots, observation=observation) == Rejected('graph_limit')
        roster.conn.execute('DELETE FROM fact')
        roster.conn.execute('UPDATE game_event SET payload=? WHERE id=?', (' ' * 65537, source.row_id))
        assert revalidate_public_in_unit(roster, proof, human=HUMAN) == Rejected('graph_limit')


def test_source_binding_refuses_mutation_while_new_independent_envelope_can_qualify(roster):
    with roster.transaction():
        observation, source, proof = prepare(roster)
        payload = copy.deepcopy(PAYLOADS[proof.policy]); payload['winner'] = 'alliance'
        roster.conn.execute('UPDATE game_event SET payload=? WHERE id=?', (canonical(payload), source.row_id))
        assert revalidate_public_in_unit(roster, proof, human=HUMAN) == Rejected('proof_changed')
        assert type(claim_source_in_unit(roster, observation, human=HUMAN, policy=proof.policy)) is Rejected
        _, _, independent = prepare(roster, payload={'winner': 'alliance'})
        assert render_public(independent.policy, json.loads(independent.output_fields_json)) == 'Alliance won battleground type 1.'


@pytest.mark.parametrize('field,value', [('ts', 1015.0), ('session_id', None)])
def test_mutable_row_projection_is_pinned_separately_from_native_milliseconds(roster, field, value):
    with roster.transaction():
        observation, source, proof = prepare(roster)
        if field == 'session_id':
            # A valid concrete session id, rather than inventing native/receipt equality.
            value = insert(roster, 'session', started=1000.0)
        roster.conn.execute(f'UPDATE game_event SET {field}=? WHERE id=?', (value, source.row_id))
        assert revalidate_public_in_unit(roster, proof, human=HUMAN) == Rejected('proof_changed')
        assert validate_live_observation_in_unit(roster, observation, human=HUMAN, policy=proof.policy) is None


def _rehash_document(value):
    body = {key: value[key] for key in ('policy', 'assurance', 'roots', 'sources', 'nodes', 'qualification', 'output_fields')}
    value['sha256'] = hashlib.sha256(b'realm-proof-v1\0' + canonical(body).encode()).hexdigest()
    return canonical(value)


@pytest.mark.parametrize('mutation', ['schema', 'extra', 'node_extra', 'node_kind', 'mixed_root', 'multiple_sources',
    'qualification_extra', 'context_epoch', 'snapshot_transport', 'actor_hash', 'actor_status', 'actor_overlap',
    'known_by', 'output_extra', 'output_bool', 'hash', 'qualification_node'])
def test_strict_codec_rejects_hidden_keys_coercion_and_incoherent_shapes_even_with_new_whole_hash(roster, mutation):
    with roster.transaction():
        _, _, proof = prepare(roster)
        document = json.loads(public_proof_to_json(proof))
        q = document['qualification']
        if mutation == 'schema': document['schema'] = 'other'
        elif mutation == 'extra': document['hidden'] = 'discarded'
        elif mutation == 'node_extra': document['nodes'][0]['hidden'] = True
        elif mutation == 'node_kind': document['nodes'][0]['kind'] = 'memory'
        elif mutation == 'mixed_root': document['roots'] = ['memory:1']
        elif mutation == 'multiple_sources': document['sources'] = [1, 2]
        elif mutation == 'qualification_extra': q['snapshot']['hidden'] = 'unvalidated'
        elif mutation == 'context_epoch': q['context']['connection_epoch'] = '011'
        elif mutation == 'snapshot_transport': q['snapshot']['transport_epoch'] = 7
        elif mutation == 'actor_hash': q['snapshot']['actors'][0]['row_sha256'] = '0' * 64
        elif mutation == 'actor_status': q['snapshot']['actors'][0]['status'] = 'retired'
        elif mutation == 'actor_overlap': q['snapshot']['actors'][0]['guid'] = 101
        elif mutation == 'known_by': q['known_by'] = [[70, 'server']]
        elif mutation == 'output_extra': document['output_fields']['text'] = 'Caller prose'
        elif mutation == 'output_bool': document['output_fields']['bg_type'] = True
        elif mutation == 'qualification_node':
            next(node for node in document['nodes'] if node['kind'] == 'qualification')['sha256'] = '0' * 64
        else: document['nodes'][0]['sha256'] = 'malformed'
        assert type(public_proof_from_json(_rehash_document(document))) is Rejected


def test_codec_requires_exact_canonical_utf8_and_has_fixed_safe_encoder_errors(roster):
    with roster.transaction():
        _, source, proof = prepare(roster)
        raw = public_proof_to_json(proof)
        for invalid in (' ' + raw, raw + '\n', raw.replace('"schema":', '"schema":"shadow","schema":', 1),
                        raw.replace('"bg_type":1', '"bg_type":NaN'), raw.replace('"bg_type":1', '"bg_type":1e999'),
                        raw.replace('"bg_type":1', '"bg_type":"\\ud800"'), 'x' * 32769):
            assert type(public_proof_from_json(invalid)) is Rejected
        for invalid in (object(), replace(proof, sha256='0' * 64)):
            with pytest.raises(ValueError, match='^invalid Realm public proof$'):
                public_proof_to_json(invalid)
            with pytest.raises(ValueError, match='^invalid Realm public draft$'):
                public_fact_draft(invalid, source)
        for change in ({'source_key': '0' * 64}, {'row_id': source.row_id + 1}, {'guild': 60},
                       {'row_sha256': '0' * 64}, {'server_ms': source.server_ms + 1}):
            with pytest.raises(ValueError, match='^invalid Realm public draft$'):
                public_fact_draft(proof, replace(source, **change))


@pytest.mark.parametrize('table,field,value', [
    ('realm_fact', 'text', 'Caller prose.'), ('realm_fact', 'kind', 'other'),
    ('realm_fact', 'source', 'rule'), ('realm_fact', 'ref', 'event:999'),
    ('realm_fact', 'public', 0), ('realm_fact', 'ts', 1001.0),
    ('realm_fact_origin', 'state', 'revoked'), ('realm_fact_origin', 'text_sha256', '0' * 64),
    ('realm_fact_origin', 'qualification_json', '{}'), ('realm_fact_origin', 'proof_json', '{}'),
    ('realm_fact_origin', 'revision', 0),
])
def test_origin_reader_refuses_private_revoked_changed_rows_and_invalid_revision(roster, table, field, value):
    with roster.transaction():
        _, source, proof = prepare(roster)
        fact_id = origin_rows(roster, source, proof)
        roster.conn.execute('PRAGMA ignore_check_constraints=ON')
        try:
            key = 'id' if table == 'realm_fact' else 'fact'
            roster.conn.execute(f'UPDATE {table} SET {field}=? WHERE {key}=?', (value, fact_id))
        finally:
            roster.conn.execute('PRAGMA ignore_check_constraints=OFF')
        assert type(public_fact_in_unit(roster, fact_id, human=HUMAN)) is Rejected


@pytest.mark.parametrize('mutation', ['missing_origin', 'wrong_source', 'wrong_policy', 'wrong_producer',
                                      'extra_knower', 'missing_knower', 'wrong_how', 'wrong_time'])
def test_origin_reader_requires_exact_source_policy_producer_and_complete_knowledge(roster, mutation):
    with roster.transaction():
        _, source, proof = prepare(roster)
        fact_id = origin_rows(roster, source, proof)
        if mutation == 'missing_origin': roster.conn.execute('DELETE FROM realm_fact_origin WHERE fact=?', (fact_id,))
        elif mutation == 'wrong_source':
            _, other, _ = prepare(roster, payload={'winner': 'alliance'})
            roster.conn.execute('UPDATE realm_fact_origin SET source=? WHERE fact=?', (other.id, fact_id))
        elif mutation in ('wrong_policy', 'wrong_producer'):
            roster.conn.execute('PRAGMA ignore_check_constraints=ON')
            try:
                field, value = ('policy', 'pvp_result_v1') if mutation == 'wrong_policy' else ('producer_key', 'event:' + '0' * 64 + ':bg_result_v1')
                roster.conn.execute(f'UPDATE realm_fact_origin SET {field}=? WHERE fact=?', (value, fact_id))
            finally:
                roster.conn.execute('PRAGMA ignore_check_constraints=OFF')
        elif mutation == 'extra_knower': insert(roster, 'realm_knows', fact=fact_id, guild=70, since=1000.0, how='server')
        elif mutation == 'missing_knower': roster.conn.execute('DELETE FROM realm_knows WHERE fact=? AND guild=60', (fact_id,))
        elif mutation == 'wrong_how': roster.conn.execute("UPDATE realm_knows SET how='server' WHERE fact=? AND guild=60", (fact_id,))
        else: roster.conn.execute('UPDATE realm_knows SET since=1000.1 WHERE fact=?', (fact_id,))
        assert type(public_fact_in_unit(roster, fact_id, human=HUMAN)) is Rejected


def test_origin_reader_reads_current_positive_revision_and_rechecks_raw_privacy_every_time(roster):
    with roster.transaction():
        _, source, proof = prepare(roster)
        fact_id = origin_rows(roster, source, proof)
        first, base = public_fact_in_unit(roster, fact_id, human=HUMAN)
        roster.conn.execute('UPDATE realm_fact_origin SET revision=2 WHERE fact=?', (fact_id,))
        second, same_base = public_fact_in_unit(roster, fact_id, human=HUMAN)
        assert first.revision == 1 and second.revision == 2 and base == same_base == proof
        insert(roster, 'fact', kind='noticed', root=proof.roots[0], about=10,
               text=HUMAN.real_name, secret=0, holder=0, created=1012.0)
        assert public_fact_in_unit(roster, fact_id, human=HUMAN) == Rejected('real_name')


def test_live_composites_unknown_policies_and_missing_real_source_fail_closed(roster):
    with roster.transaction():
        observation = observe(roster)
        for root in ('memory:1', 'realm:fact:1'):
            assert audit_public_in_unit(roster, human=HUMAN, policy='bg_result_v1', refs=(root,),
                                        observation=observation) == Rejected('unsupported_policy')
        assert audit_public_in_unit(roster, human=HUMAN, policy='unknown', refs=('event:1',),
                                    observation=observation) == Rejected('unsupported_policy')
        assert audit_public_in_unit(roster, human=HUMAN, policy='bg_result_v1', refs=('event:1',),
                                    observation=observation) == Rejected('missing_row')
        assert public_fact_in_unit(roster, 999, human=HUMAN) == Rejected('missing_row')


def test_positive_numeric_boundaries_and_large_finite_wg_values_are_not_invented_limits(roster):
    with roster.transaction():
        _, _, bg = prepare(roster, payload={'bg_type': (1 << 32) - 1,
            'members': list(range(1, 128)) + [201]})
        assert len(json.loads(bg.qualification_json)['participants']) == 128
        _, _, arena = prepare(roster, 'arena_result_v1', payload={
            'arena_type': 5, 'winner_team': (1 << 32) - 1, 'loser_team': (1 << 32) - 2,
            'winner_change': -(1 << 31), 'loser_change': (1 << 31) - 1})
        assert 'change' not in arena.output_fields_json
        _, _, wg = prepare(roster, 'wg_result_v1', payload={'update_ms': 1e308})
        assert render_public(wg.policy, json.loads(wg.output_fields_json)) == EXPECTED[wg.policy][0]
        _, kill_source, kill = prepare(roster, 'verified_raid_kill_v1', payload={
            'ts': 1, 'kill_id': 'f' * 32 + ':' + str((1 << 64) - 1), 'native_epoch': (1 << 64) - 1,
            'killed_at_ms': 1, 'verified_at_ms': 1, 'fight_started_at_ms': 0, 'fight_ms': -1,
            'difficulty': -1, 'size': 25, 'heroic': True, 'cast_members': []})
        assert kill_source.server_ms == 1 and public_fact_draft(kill, kill_source).source_at == 0.001
        _, pvp_source, _ = prepare(roster, 'pvp_result_v1', payload={
            'ts': 253402300799999, 'killer': (1 << 32) - 1, 'victim': 10,
            'map': (1 << 32) - 1, 'zone': (1 << 32) - 1})
        assert pvp_source.server_ms == 253402300799999


@pytest.mark.parametrize('actor_count,guild_count', [(70, 0), (129, 0), (0, 129)])
def test_capture_rejects_complete_overflow_instead_of_truncating_rosters_or_8192_byte_snapshot(roster, actor_count, guild_count):
    with roster.transaction():
        original = observe(roster)
        for index in range(actor_count):
            insert(roster, 'realm_actor', guid=1000 + index, guild=60, name='Synthetic',
                   role='officer', added=1.0)
        for index in range(guild_count):
            insert(roster, 'realm_guild', id=1000 + index, name='Synthetic', role='progression',
                   faction='horde', added=1.0)
        result = capture_qualification_in_unit(roster, human=HUMAN, event=original.event,
            receipt=original.receipt, context=CONTEXT, confirmed_human_guid=10, transport_epoch=7, captured_at=1010.0)
        assert type(result) is Rejected
        assert roster.conn.execute('SELECT count(*) FROM realm_actor').fetchone()[0] == 3 + actor_count
        assert roster.conn.execute('SELECT count(*) FROM realm_guild').fetchone()[0] == 3 + guild_count


@pytest.mark.parametrize('changes', [
    {'confirmed_human_guid': True}, {'confirmed_human_guid': 99}, {'transport_epoch': 8},
    {'captured_at': float('inf')}, {'captured_at': True},
    {'context': replace(CONTEXT, enabled=False)}, {'context': replace(CONTEXT, generation=(1 << 63))},
])
def test_capture_checks_actual_confirmed_identity_context_transport_and_exact_time_types(roster, changes):
    with roster.transaction():
        observation = observe(roster)
        values = dict(human=HUMAN, event=observation.event, receipt=observation.receipt,
            context=CONTEXT, confirmed_human_guid=10, transport_epoch=7, captured_at=1010.0)
        assert type(capture_qualification_in_unit(roster, **{**values, **changes})) is Rejected


@pytest.mark.parametrize('raw', ['{"ts":1000000,"ts":1000000,"bg_type":1,"winner":"horde","members":[201,101,10]}',
    '{"ts":1000000,"bg_type":1,"winner":"horde","members":[201,101,10],"discarded":"Synthetic Private"}',
    '{"ts":1000000,"bg_type":1,"winner":"horde","members":[201,101,10],"hidden":NaN}',
    '{"ts":1000000,"bg_type":1,"winner":"horde","members":[201,101,10],"hidden":"\\ud800"}'])
def test_raw_row_strict_json_rejects_duplicate_hidden_nonfinite_and_surrogate_data(roster, raw):
    with roster.transaction():
        observation, source, proof = prepare(roster)
        roster.conn.execute('UPDATE game_event SET payload=? WHERE id=?', (raw, source.row_id))
        assert type(revalidate_public_in_unit(roster, proof, human=HUMAN)) is Rejected
        assert type(validate_live_observation_in_unit(roster, observation, human=HUMAN, policy=proof.policy)) is Rejected


def test_capture_accepts_all_128_readable_cast_then_rejects_the_129th(roster):
    from leeroy_mind.personas.io import save_persona

    with roster.transaction():
        original = observe(roster)
        for index in range(124):
            guid = 1000 + index
            insert(roster, 'cast_member', guid=guid, name='Synthetic', race=2, **{'class': 1},
                   gender=0, level=80, rank=3, online=1, updated=1000.0)
            save_persona(roster, make_persona(guid, 'Synthetic'), source='seed', ts=1000.0,
                         race='Orc', class_name='Warrior')
            if index == 122:
                complete = capture_qualification_in_unit(roster, human=HUMAN, event=original.event,
                    receipt=original.receipt, context=CONTEXT, confirmed_human_guid=10,
                    transport_epoch=7, captured_at=1010.0)
                assert type(complete) is QualificationSnapshot
                assert complete.cast == (*range(101, 106), *range(1000, 1123))
        rejected = capture_qualification_in_unit(roster, human=HUMAN, event=original.event,
            receipt=original.receipt, context=CONTEXT, confirmed_human_guid=10,
            transport_epoch=7, captured_at=1010.0)
        assert type(rejected) is Rejected
        assert roster.conn.execute('SELECT count(*) FROM persona_core').fetchone()[0] == 129


def test_capture_accepts_all_128_rivals_and_129_knowers_then_rejects_another_guild(roster):
    with roster.transaction():
        for index in range(127):
            insert(roster, 'realm_guild', id=1000 + index, name='Synthetic', role='progression',
                   faction='horde', added=1.0)
        observation, source, proof = prepare(roster, 'wg_result_v1')
        assert observation.qualification.rival_guilds == (60, *range(1000, 1127))
        knowledge = json.loads(proof.qualification_json)['known_by']
        assert knowledge == [[50, 'own'], [60, 'server'], *[[guild, 'server'] for guild in range(1000, 1127)]]
        fact_id = origin_rows(roster, source, proof)
        assert public_fact_in_unit(roster, fact_id, human=HUMAN)[1] == proof
        insert(roster, 'realm_guild', id=1127, name='Synthetic', role='progression',
               faction='horde', added=1.0)
        result = capture_qualification_in_unit(roster, human=HUMAN, event=observation.event,
            receipt=observation.receipt, context=CONTEXT, confirmed_human_guid=10,
            transport_epoch=7, captured_at=1010.0)
        assert type(result) is Rejected


def test_real_capture_and_codec_accept_exact_8192_bytes_but_never_8193(roster):
    with roster.transaction():
        # WG has no participants; keeping only the configured active guild also
        # makes capture's qualification size equal to its final proof size.
        roster.conn.execute("UPDATE realm_guild SET status='retired' WHERE id<>50")
        context = replace(CONTEXT, process_id='p')
        observation = observe(roster, 'wg_result_v1', context=context)
        previous = observation.qualification
        for index in range(128):
            guid = 1000 + index
            insert(roster, 'realm_actor', guid=guid, guild=50, name='Synthetic',
                   role='officer', added=1.0)
            candidate = capture_qualification_in_unit(roster, human=HUMAN, event=observation.event,
                receipt=observation.receipt, context=context, confirmed_human_guid=10,
                transport_epoch=7, captured_at=1010.0)
            if type(candidate) is Rejected:
                assert candidate == Rejected('graph_limit')
                assert index < 127  # The byte limit, rather than the actor cap.
                roster.conn.execute('DELETE FROM realm_actor WHERE guid=?', (guid,))
                break
            previous = candidate
        else:
            pytest.fail('complete actor snapshots must reach the qualification byte limit')
        snapshot = {**asdict(previous), 'transport_epoch': '7'}
        serialized_context = {**asdict(context), 'connection_epoch': '11'}
        size = len(canonical(dict(schema='realm_qualification_v1', snapshot=snapshot,
            context=serialized_context, participants=[], native_present=[], qualified_cast=[],
            known_by=[[50, 'own']])).encode('utf-8'))
        padding = 8192 - size
        assert 0 <= padding < 255
        exact_context = replace(context, process_id='p' * (padding + 1))
        exact = capture_qualification_in_unit(roster, human=HUMAN, event=observation.event,
            receipt=observation.receipt, context=exact_context, confirmed_human_guid=10,
            transport_epoch=7, captured_at=1010.0)
        assert type(exact) is QualificationSnapshot
        observation = replace(observation, context=exact_context, qualification=exact)
        source, inserted = claim_source_in_unit(roster, observation, human=HUMAN, policy='wg_result_v1')
        assert inserted is True
        proof = audit_public_in_unit(roster, human=HUMAN, policy='wg_result_v1',
            refs=(f'event:{source.row_id}',), observation=observation)
        assert type(proof) is PublicProof
        assert len(proof.qualification_json.encode('utf-8')) == 8192
        assert public_proof_from_json(public_proof_to_json(proof)) == proof
        overflow = capture_qualification_in_unit(roster, human=HUMAN, event=observation.event,
            receipt=observation.receipt, context=replace(exact_context, process_id=exact_context.process_id + 'p'),
            confirmed_human_guid=10, transport_epoch=7, captured_at=1010.0)
        assert overflow == Rejected('graph_limit')


def test_stored_topic_codec_roundtrip_preserves_the_existing_proof_and_semantics(stored_graph):
    with stored_graph.transaction():
        proof = audit_public_in_unit(stored_graph, human=HUMAN, policy='stored_forum_topic_v1',
                                     refs=('memory:1',), rumor_id=1)
        assert type(proof) is PublicProof
        decoded = public_proof_from_json(public_proof_to_json(proof))
        assert decoded == proof
        assert revalidate_public_in_unit(stored_graph, decoded, human=HUMAN) == proof
        assert proof.qualification_json == '{}' and proof.sources == ()
        assert render_public(proof.policy, json.loads(proof.output_fields_json)) == (
            'A rumor concerns the raid encounter with Lord Marrowgar.')
        assert stored_graph.conn.execute('SELECT count(*) FROM realm_source').fetchone()[0] == 0
