"""Real realm_source INSERT/replay/rollback tests on the actual Store unit."""
from dataclasses import fields, replace
import sqlite3

import pytest

from test_realm_live_proof import (
    CONTEXT, HUMAN, PAYLOADS, canonical, changed_observation, insert, observe, origin_rows, prepare, roster,
)
from leeroy_mind import protocol
from leeroy_mind.ingress import RowReceipt, game_row_binding
from leeroy_mind.realm.protocol import Wg
from leeroy_mind.realm.provenance import (
    audit_public_in_unit, capture_qualification_in_unit, public_fact_in_unit,
    revalidate_public_in_unit, validate_live_binding_in_unit, validate_live_observation_in_unit,
)
from leeroy_mind.realm.records import QualificationSnapshot, RealmObservation, Rejected
from leeroy_mind.realm.source_store import claim_source_in_unit
from leeroy_mind.storage.store import Store


def test_actual_insert_outcome_is_true_once_then_false_even_in_same_unit(roster):
    with roster.transaction():
        observation = observe(roster)
        statements = []
        roster.conn.set_trace_callback(statements.append)
        try:
            first = claim_source_in_unit(roster, observation, human=HUMAN, policy='bg_result_v1')
            repeat = claim_source_in_unit(roster, observation, human=HUMAN, policy='bg_result_v1')
        finally:
            roster.conn.set_trace_callback(None)
        assert first[1] is True and repeat == (first[0], False)
        assert sum(statement.startswith('INSERT INTO realm_source') for statement in statements) == 1
        assert not any(statement.lstrip().upper().startswith(('COMMIT', 'REPLACE', 'UPDATE')) for statement in statements)
        assert roster.conn.execute('SELECT count(*) FROM realm_source').fetchone()[0] == 1
        assert roster.conn.execute('SELECT count(*) FROM realm_fact').fetchone()[0] == 0
    with roster.transaction():
        assert claim_source_in_unit(roster, observation, human=HUMAN, policy='bg_result_v1') == (first[0], False)


def test_replay_checks_new_row_but_retains_first_fk_binding_received_time_and_qualification(roster):
    with roster.transaction():
        original, source, proof = prepare(roster)
        fact_id = origin_rows(roster, source, proof)
        roster.conn.execute("UPDATE realm_actor SET status='retired'")
        roster.conn.execute('DELETE FROM persona_core WHERE guid=101')
        replay = observe(roster, received=9999.0, captured=9999.0, transport=44, backlog=True)
        assert replay.receipt.wire.envelope_sha256 == source.source_key
        assert replay.receipt.row_id != source.row_id and replay.qualification != original.qualification
        claimed = claim_source_in_unit(roster, replay, human=HUMAN, policy=proof.policy)
        assert claimed == (source, False)
        assert claimed[0].received_at == 1010.0
        assert claimed[0].row_sha256 == original.receipt.row_sha256
        assert public_fact_in_unit(roster, fact_id, human=HUMAN)[1] == proof
        assert revalidate_public_in_unit(roster, proof, human=HUMAN) == proof
        assert type(audit_public_in_unit(roster, human=HUMAN, policy=proof.policy,
            refs=proof.roots, observation=replay)) is Rejected
        # The replay's own row is independently bound, despite not being retained.
        roster.conn.execute("UPDATE game_event SET type='boss' WHERE id=?", (replay.receipt.row_id,))
        assert type(claim_source_in_unit(roster, replay, human=HUMAN, policy=proof.policy)) is Rejected


def test_existing_source_without_origin_is_not_promoted_by_later_roster_replay(roster):
    with roster.transaction():
        observation = observe(roster)
        source, inserted = claim_source_in_unit(roster, observation, human=HUMAN, policy='bg_result_v1')
        assert inserted is True
        insert(roster, 'realm_guild', id=80, name='Later', role='progression', faction='horde', added=9000.0)
        replay = observe(roster, captured=9001.0, received=9001.0)
        assert 80 in replay.qualification.rival_guilds
        assert claim_source_in_unit(roster, replay, human=HUMAN, policy='bg_result_v1') == (source, False)
        for table in ('realm_fact', 'realm_fact_origin', 'realm_knows'):
            assert roster.conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == 0
        assert public_fact_in_unit(roster, 1, human=HUMAN) == Rejected('missing_row')


def test_new_backlog_cannot_capture_fresh_public_qualification(roster):
    with roster.transaction():
        observation = observe(roster, backlog=True)
        assert claim_source_in_unit(roster, observation, human=HUMAN, policy='bg_result_v1') == Rejected('invalid_input')
        assert roster.conn.execute('SELECT count(*) FROM realm_source').fetchone()[0] == 0


def test_source_identity_conflict_and_retained_first_row_corruption_reject(roster):
    with roster.transaction():
        original, source, proof = prepare(roster)
        changed_context = replace(CONTEXT, guild=60)
        changed_snapshot = replace(original.qualification, guild=60, rival_guilds=(50,))
        conflict = replace(original, context=changed_context, qualification=changed_snapshot)
        assert type(claim_source_in_unit(roster, conflict, human=HUMAN, policy=proof.policy)) is Rejected
        replay = observe(roster, received=9999.0)
        roster.conn.execute("UPDATE game_event SET payload=? WHERE id=?",
                            (canonical({**PAYLOADS[proof.policy], 'winner': 'alliance'}), source.row_id))
        assert claim_source_in_unit(roster, replay, human=HUMAN, policy=proof.policy) == Rejected('proof_changed')
        assert roster.conn.execute('SELECT game_event_id FROM realm_source').fetchone()[0] == source.row_id


@pytest.mark.parametrize('surface', ['row_digest', 'top_extra', 'top_missing', 'wrong_type', 'wrong_guid',
                                    'missing_row', 'unbound', 'transport', 'snapshot', 'event_class'])
def test_binding_and_exact_runtime_dto_contract_reject_before_source_writes(roster, surface):
    with roster.transaction():
        observation = observe(roster)
        receipt = observation.receipt
        if surface == 'row_digest': observation = replace(observation, receipt=replace(receipt, row_sha256='0' * 64))
        elif surface == 'top_extra': observation = replace(observation, receipt=replace(receipt,
            wire=replace(receipt.wire, top_keys=tuple(sorted((*receipt.wire.top_keys, 'discarded_text'))))))
        elif surface == 'top_missing': observation = replace(observation, receipt=replace(receipt,
            wire=replace(receipt.wire, top_keys=tuple(key for key in receipt.wire.top_keys if key != 'members'))))
        elif surface == 'wrong_type': roster.conn.execute("UPDATE game_event SET type='boss' WHERE id=?", (receipt.row_id,))
        elif surface == 'wrong_guid': roster.conn.execute('UPDATE game_event SET guid=10 WHERE id=?', (receipt.row_id,))
        elif surface == 'missing_row': roster.conn.execute('DELETE FROM game_event WHERE id=?', (receipt.row_id,))
        elif surface == 'unbound': observation = replace(observation, receipt=None)
        elif surface == 'transport': observation = replace(observation,
            qualification=replace(observation.qualification, transport_epoch=8))
        elif surface == 'snapshot': object.__setattr__(observation.qualification, 'human_confirmed', 1)
        else: observation = replace(observation, event=object())
        assert type(claim_source_in_unit(roster, observation, human=HUMAN, policy='bg_result_v1')) is Rejected
        assert roster.conn.execute('SELECT count(*) FROM realm_source').fetchone()[0] == 0


def test_old_seventeen_key_kill_envelope_does_not_acquire_duration_defaults(roster):
    with roster.transaction():
        observation = observe(roster, 'verified_raid_kill_v1')
        receipt = observation.receipt
        wire = replace(receipt.wire, top_keys=tuple(key for key in receipt.wire.top_keys
                                                  if key not in ('fight_started_at_ms', 'fight_ms')))
        legacy = replace(observation, receipt=replace(receipt, wire=wire))
        assert type(claim_source_in_unit(roster, legacy, human=HUMAN, policy='verified_raid_kill_v1')) is Rejected
        assert roster.conn.execute('SELECT count(*) FROM realm_source').fetchone()[0] == 0


def test_actual_wg_parser_coercion_does_not_reconstruct_original_envelope_key(roster, monkeypatch):
    with roster.transaction():
        sources = []
        for raw_update in (1, 1.0):
            envelope = dict(v=1, type='wg', **{**PAYLOADS['wg_result_v1'], 'update_ms': raw_update})
            with monkeypatch.context() as patch:
                patch.setitem(protocol._events, 'wg', Wg)
                event, wire = protocol.parse_event_with_origin(canonical(envelope),
                    received_at=1010.0, transport_epoch=7)
            assert type(event.update_ms) is float
            payload = {field.name: getattr(event, field.name) for field in fields(Wg)}
            row_id = insert(roster, 'game_event', ts=1010.0, type='wg', guid=None,
                            payload=canonical(payload), session_id=None)
            receipt = RowReceipt('game_event', row_id, game_row_binding(row_id, 'wg', payload), wire, False)
            qualification = capture_qualification_in_unit(roster, human=HUMAN, event=event, receipt=receipt,
                context=CONTEXT, confirmed_human_guid=10, transport_epoch=7, captured_at=1010.0)
            assert type(qualification) is QualificationSnapshot
            observation = RealmObservation(event, receipt, CONTEXT, qualification)
            source, inserted = claim_source_in_unit(roster, observation, human=HUMAN, policy='wg_result_v1')
            assert inserted is True and source.source_key == wire.envelope_sha256
            sources.append(source)
        assert sources[0].source_key != sources[1].source_key


def test_real_unique_insert_contention_rereads_original_source_and_returns_false(roster):
    with roster.transaction():
        observation = observe(roster)
        wire, receipt = observation.receipt.wire, observation.receipt
        fired = []

        def concurrent_insert(statement):
            if statement.startswith('INSERT INTO realm_source') and not fired:
                fired.append(True)
                insert(roster, 'realm_source', source_key=wire.envelope_sha256, protocol_version=1,
                    event_type='bg_end', game_event_id=receipt.row_id, chat_line_id=None,
                    row_sha256=receipt.row_sha256, server_ms=wire.server_ms, received_at=wire.received_at,
                    guild=50, human=10, top_keys_json=canonical(list(wire.top_keys)))

        roster.conn.set_trace_callback(concurrent_insert)
        try:
            source, inserted = claim_source_in_unit(roster, observation, human=HUMAN, policy='bg_result_v1')
        finally:
            roster.conn.set_trace_callback(None)
        assert fired == [True] and inserted is False
        assert source.row_id == receipt.row_id
        assert roster.conn.execute('SELECT count(*) FROM realm_source').fetchone()[0] == 1


def test_post_claim_rejection_raises_inside_outer_unit_and_rolls_back_only_domain_rows(roster):
    with roster.transaction():
        observation = observe(roster)

    class Refused(Exception):
        pass

    with pytest.raises(Refused):
        with roster.transaction():
            source, inserted = claim_source_in_unit(roster, observation, human=HUMAN, policy='bg_result_v1')
            assert inserted is True
            insert(roster, 'fact', kind='noticed', root=f'event:{source.row_id}', about=10,
                   text='', secret=1, holder=0, created=1012.0)
            result = audit_public_in_unit(roster, human=HUMAN, policy='bg_result_v1',
                refs=(f'event:{source.row_id}',), observation=observation)
            assert result == Rejected('private_evidence')
            raise Refused(result.reason)
    assert roster.conn.execute('SELECT count(*) FROM realm_source').fetchone()[0] == 0
    assert roster.conn.execute('SELECT count(*) FROM fact').fetchone()[0] == 0
    assert roster.conn.execute('SELECT count(*) FROM game_event').fetchone()[0] == 1


def test_actual_store_unit_required_wrapper_depth_cannot_borrow_another_stores_unit(roster):
    with roster.transaction():
        observation = observe(roster)
        other = Store(roster.conn, clock=roster.clock)
        for target in (other, object()):
            with pytest.raises(RuntimeError, match=r'^Realm public proof requires Store.transaction\(\)$'):
                claim_source_in_unit(target, observation, human=HUMAN, policy='bg_result_v1')
    with pytest.raises(RuntimeError):
        validate_live_observation_in_unit(roster, observation, human=HUMAN, policy='bg_result_v1')
    with pytest.raises(RuntimeError):
        validate_live_binding_in_unit(roster, observation, human=HUMAN, policy='bg_result_v1')
    with pytest.raises(RuntimeError):
        public_fact_in_unit(roster, 1, human=HUMAN)


def test_database_and_programming_failures_propagate_and_outer_unit_rolls_back(roster):
    with roster.transaction():
        observation = observe(roster)
    with pytest.raises(sqlite3.OperationalError):
        with roster.transaction():
            roster.conn.execute('DROP TABLE realm_source')
            claim_source_in_unit(roster, observation, human=HUMAN, policy='bg_result_v1')
    assert roster.conn.execute('SELECT count(*) FROM realm_source').fetchone()[0] == 0
    with pytest.raises(sqlite3.OperationalError):
        with roster.transaction():
            roster.conn.execute('DROP TABLE persona_core')
            capture_qualification_in_unit(roster, human=HUMAN, event=observation.event,
                receipt=observation.receipt, context=CONTEXT, confirmed_human_guid=10,
                transport_epoch=7, captured_at=1010.0)


REPLAY_CASES = [
    ('bg_result_v1', {'members': [201]}, 'actor'),
    ('bg_result_v1', {'members': [201]}, 'guild'),
    ('verified_raid_kill_v1', {'human_present': False, 'fight_started_at_ms': 0, 'fight_ms': -1}, 'cast'),
    ('verified_raid_kill_v1', {'human_present': False, 'fight_started_at_ms': 0, 'fight_ms': -1}, 'persona'),
    ('channel_raid_topic_v1', {'from_guid': 201, 'from_name': 'Rivalname'}, 'actor'),
    ('channel_raid_topic_v1', {'from_guid': 201, 'from_name': 'Rivalname'}, 'guild'),
]


def lose_current_qualification(store, loss):
    if loss == 'actor':
        store.conn.execute("UPDATE realm_actor SET status='retired' WHERE guid=201")
    elif loss == 'guild':
        store.conn.execute("UPDATE realm_guild SET status='retired' WHERE id=60")
    elif loss == 'cast':
        store.conn.execute('DELETE FROM cast_member WHERE guid=101')
    else:
        store.conn.execute("UPDATE persona_core SET json='{' WHERE guid=101")


def domain_counts(store):
    return tuple(store.conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
                 for table in ('realm_source', 'realm_fact', 'realm_fact_origin', 'realm_knows', 'realm_kill'))


@pytest.mark.parametrize('policy,payload,loss', REPLAY_CASES)
def test_retired_qualifiers_replay_only_the_original_active_proof(roster, policy, payload, loss):
    with roster.transaction():
        original, source, proof = prepare(roster, policy, payload=payload)
        fact_id = origin_rows(roster, source, proof)
        counts = domain_counts(roster)
        lose_current_qualification(roster, loss)
        replay = observe(roster, policy, payload=payload, received=9999.0, captured=9999.0, backlog=True)
        assert replay.receipt.wire.envelope_sha256 == source.source_key
        assert replay.qualification != original.qualification
        if policy == 'channel_raid_topic_v1':
            snapshot = replay.qualification
            assert (snapshot.speaker_kind, snapshot.speaker_guid, snapshot.speaker_guild,
                    snapshot.speaker_name, snapshot.speaker_name_source, snapshot.speaker_name_sha256) == (
                        'none', 0, 0, '', 'none', None)
        assert validate_live_binding_in_unit(roster, replay, human=HUMAN, policy=policy) is None
        assert validate_live_observation_in_unit(roster, replay, human=HUMAN, policy=policy) == Rejected('private_evidence')
        assert claim_source_in_unit(roster, replay, human=HUMAN, policy=policy) == (source, False)
        assert revalidate_public_in_unit(roster, proof, human=HUMAN) == proof
        assert public_fact_in_unit(roster, fact_id, human=HUMAN)[1] == proof
        assert roster.conn.execute('SELECT qualification_json FROM realm_fact_origin WHERE fact=?',
                                   (fact_id,)).fetchone()[0] == proof.qualification_json
        assert domain_counts(roster) == counts
        assert type(audit_public_in_unit(roster, human=HUMAN, policy=policy,
                                        refs=proof.roots, observation=replay)) is Rejected


@pytest.mark.parametrize('policy,payload,loss', REPLAY_CASES)
@pytest.mark.parametrize('backlog', [False, True])
def test_first_unqualified_source_never_inserts_even_when_raw_binding_is_valid(roster, policy, payload, loss, backlog):
    with roster.transaction():
        lose_current_qualification(roster, loss)
        observation = observe(roster, policy, payload=payload, backlog=backlog)
        assert validate_live_binding_in_unit(roster, observation, human=HUMAN, policy=policy) is None
        assert validate_live_observation_in_unit(roster, observation, human=HUMAN, policy=policy) == Rejected('private_evidence')
        result = claim_source_in_unit(roster, observation, human=HUMAN, policy=policy)
        assert result == Rejected('invalid_input' if backlog else 'private_evidence')
        assert domain_counts(roster) == (0, 0, 0, 0, 0)


@pytest.mark.parametrize('policy,payload,loss', REPLAY_CASES[::2])
@pytest.mark.parametrize('origin_state', ['never_created', 'missing', 'revoked'])
def test_binding_only_replay_cannot_create_or_restore_an_origin(roster, policy, payload, loss, origin_state):
    with roster.transaction():
        original, source, proof = prepare(roster, policy, payload=payload)
        if origin_state == 'never_created':
            fact_id = 1
        else:
            fact_id = origin_rows(roster, source, proof)
            if origin_state == 'missing':
                roster.conn.execute('DELETE FROM realm_fact_origin WHERE fact=?', (fact_id,))
            else:
                roster.conn.execute("UPDATE realm_fact_origin SET state='revoked' WHERE fact=?", (fact_id,))
        counts = domain_counts(roster)
        lose_current_qualification(roster, loss)
        replay = observe(roster, policy, payload=payload, received=9999.0, captured=9999.0)
        assert claim_source_in_unit(roster, replay, human=HUMAN, policy=policy) == (source, False)
        assert type(public_fact_in_unit(roster, fact_id, human=HUMAN)) is Rejected
        assert type(audit_public_in_unit(roster, human=HUMAN, policy=policy,
                                        refs=proof.roots, observation=replay)) is Rejected
        assert domain_counts(roster) == counts


@pytest.mark.parametrize('policy,payload,loss', REPLAY_CASES[::2])
def test_retirement_replay_still_rejects_changed_binding_identity_and_current_privacy(roster, policy, payload, loss):
    with roster.transaction():
        _, source, proof = prepare(roster, policy, payload=payload)
        fact_id = origin_rows(roster, source, proof)
        counts = domain_counts(roster)
        lose_current_qualification(roster, loss)
        replay = observe(roster, policy, payload=payload, received=9999.0, captured=9999.0)
        bad_receipt = replace(replay.receipt, row_sha256='0' * 64)
        assert type(claim_source_in_unit(roster, replace(replay, receipt=bad_receipt), human=HUMAN, policy=policy)) is Rejected
        assert type(claim_source_in_unit(roster, replay, human=replace(HUMAN, guid=99), policy=policy)) is Rejected
        private_word = {'bg_result_v1': 'Horde', 'verified_raid_kill_v1': 'Marrowgar',
                        'channel_raid_topic_v1': 'Rivalname'}[policy]
        current_human = replace(HUMAN, real_name=private_word)
        assert validate_live_binding_in_unit(roster, replay, human=current_human, policy=policy) == Rejected('real_name')
        assert claim_source_in_unit(roster, replay, human=current_human, policy=policy) == Rejected('real_name')
        roster.conn.execute("UPDATE game_event SET payload='{}' WHERE id=?", (replay.receipt.row_id,))
        assert type(claim_source_in_unit(roster, replay, human=HUMAN, policy=policy)) is Rejected
        assert public_fact_in_unit(roster, fact_id, human=HUMAN)[1] == proof
        assert domain_counts(roster) == counts


@pytest.mark.parametrize('policy,changes', [
    ('bg_result_v1', {'bg_type': True}), ('bg_result_v1', {'members': [201, 201]}),
    ('arena_result_v1', {'winner_team': 92}), ('arena_result_v1', {'winner_change': 1 << 31}),
    ('wg_result_v1', {'state': 'start'}), ('wg_result_v1', {'update_ms': 1}),
    ('pvp_result_v1', {'victim': 201}), ('pvp_result_v1', {'zone': True}),
    ('channel_raid_topic_v1', {'via_bridge': 1}), ('channel_raid_topic_v1', {'channel': 'guild'}),
    ('channel_raid_topic_v1', {'from_name': 'Two Words'}),
    ('channel_raid_topic_v1', {'text': 'Ready for Icecrown Citadel. extra'}),
    ('channel_raid_topic_v1', {'text': '|cffffffffReady for Icecrown Citadel.|r'}),
    ('channel_raid_topic_v1', {'text': 'Synthetic Private'}),
    ('channel_raid_topic_v1', {'text': 'I love you.'}),
    ('verified_raid_kill_v1', {'boss_id': 999}), ('verified_raid_kill_v1', {'guild_id': 60}),
    ('verified_raid_kill_v1', {'fight_started_at_ms': 0, 'fight_ms': 0}),
    ('verified_raid_kill_v1', {'human_present': False}),
    ('verified_raid_kill_v1', {'difficulty': 2}), ('verified_raid_kill_v1', {'native_epoch': 1 << 64}),
])
def test_binding_validator_keeps_complete_policy_shape_native_and_raw_privacy_checks(roster, policy, changes):
    with roster.transaction():
        observation = observe(roster, policy)
        changed = changed_observation(roster, observation, policy, **changes)
        assert type(validate_live_binding_in_unit(roster, changed, human=HUMAN, policy=policy)) is Rejected
        assert domain_counts(roster) == (0, 0, 0, 0, 0)
