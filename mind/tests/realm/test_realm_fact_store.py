"""Atomic FactStore behavior with real SQLite, source capture, and proof reads."""
from dataclasses import replace
import json
import sqlite3

import pytest

from leeroy_mind.realm import fact_store as module
from leeroy_mind.realm.fact_store import FactStore, FactWriteRejected
from leeroy_mind.realm.records import FactRow, Rejected
from leeroy_mind.realm.source_store import claim_source_in_unit
from leeroy_mind.storage.store import Store
from test_realm_live_proof import HUMAN, observe, roster

TABLES = ('realm_source', 'realm_fact', 'realm_fact_origin', 'realm_knows', 'realm_kill')


def counts(store):
    return tuple(store.conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0] for table in TABLES)


def committed_observation(store, policy='bg_result_v1', **kw):
    with store.transaction():
        return observe(store, policy, **kw)


def test_store_units_and_replay_do_not_promote_tombstones(roster):
    obs = committed_observation(roster)
    facts = FactStore(roster)
    assert facts.store is roster
    with pytest.raises(RuntimeError):
        facts.put_fact_in_unit(obs, human=HUMAN)
    wrapped = Store(roster.conn, clock=roster.clock)
    with roster.transaction():
        with pytest.raises(RuntimeError):
            FactStore(wrapped).put_fact_in_unit(obs, human=HUMAN)
        with pytest.raises(RuntimeError):
            facts.record(obs, human=HUMAN)
        source, created = claim_source_in_unit(roster, obs, human=HUMAN, policy='bg_result_v1')
        assert created
        with pytest.raises(FactWriteRejected):
            facts.put_fact_in_unit(obs, human=HUMAN)
    assert type(facts.record(obs, human=HUMAN)) is Rejected
    assert counts(roster) == (1, 0, 0, 0, 0)


def test_first_decision_replayed_without_new_knowledge(roster):
    obs = committed_observation(roster)
    facts = FactStore(roster)
    first = facts.record(obs, human=HUMAN)
    with roster.transaction():
        before = tuple(tuple(r) for r in roster.conn.execute('SELECT * FROM realm_knows ORDER BY guild'))
        roster.conn.execute("UPDATE realm_actor SET status='retired'")
        replay = observe(roster, received=1011.0, captured=1011.0)
    assert facts.record(replay, human=HUMAN) == first
    with roster.transaction():
        assert counts(roster) == (1, 1, 1, 2, 0)
        assert tuple(tuple(r) for r in roster.conn.execute('SELECT * FROM realm_knows ORDER BY guild')) == before
        assert roster.conn.execute('SELECT received_at FROM realm_source').fetchone()[0] == 1010.0
        assert facts.put_fact_in_unit(replay, human=HUMAN) == first


@pytest.mark.parametrize('table,when', [
    ('realm_source', '1'), ('realm_fact', '1'), ('realm_fact_origin', '1'),
    ('realm_knows', 'NEW.guild=50'), ('realm_knows', 'NEW.guild=60'), ('realm_kill', '1')])
def test_sql_write_failures_roll_back_every_realm_row(roster, table, when):
    with roster.transaction():
        roster.conn.execute("INSERT INTO realm_guild(id,role,faction,name,added) VALUES (80,'gankers','horde','X',1)")
    obs = committed_observation(roster, 'verified_raid_kill_v1')
    with roster.transaction():
        roster.conn.execute(f"CREATE TEMP TRIGGER fail_write BEFORE INSERT ON {table} WHEN {when} "
                            "BEGIN SELECT RAISE(ABORT,'injected'); END")
    with pytest.raises(sqlite3.IntegrityError):
        FactStore(roster).record(obs, human=HUMAN)
    assert counts(roster) == (0, 0, 0, 0, 0)
    assert roster.conn.execute('SELECT count(*) FROM game_event').fetchone()[0] == 1


def test_rejection_after_claim_and_final_read_rolls_back(roster, monkeypatch):
    obs = committed_observation(roster)
    facts = FactStore(roster)
    real_audit = module.audit_public_in_unit
    monkeypatch.setattr(module, 'audit_public_in_unit', lambda *a, **kw: Rejected('proof_changed'))
    assert type(facts.record(obs, human=HUMAN)) is Rejected
    assert counts(roster) == (0, 0, 0, 0, 0)
    monkeypatch.setattr(module, 'audit_public_in_unit', real_audit)
    monkeypatch.setattr(module, 'public_fact_in_unit', lambda *a, **kw: Rejected('proof_changed'))
    assert type(facts.record(obs, human=HUMAN)) is Rejected
    assert counts(roster) == (0, 0, 0, 0, 0)


def test_outer_and_whole_reducer_savepoint_rollback(roster):
    obs = committed_observation(roster)
    facts = FactStore(roster)
    with pytest.raises(RuntimeError):
        with roster.transaction():
            facts.put_fact_in_unit(obs, human=HUMAN)
            raise RuntimeError('outer failure')
    assert counts(roster) == (0, 0, 0, 0, 0)
    with roster.transaction():
        roster.conn.execute("INSERT INTO realm_guild(id,role,faction,name,added) VALUES (80,'gankers','horde','X',1)")
        try:
            with roster.transaction():
                facts.put_fact_in_unit(obs, human=HUMAN)
                raise RuntimeError('savepoint failure')
        except RuntimeError:
            pass
    assert counts(roster) == (0, 0, 0, 0, 0)
    assert roster.conn.execute('SELECT count(*) FROM realm_guild WHERE id=80').fetchone()[0] == 1


def test_replay_rejects_revoked_or_corrupt_current_origin(roster):
    obs = committed_observation(roster)
    facts = FactStore(roster)
    fact = facts.record(obs, human=HUMAN)
    with roster.transaction():
        roster.conn.execute("UPDATE realm_fact_origin SET state='revoked' WHERE fact=?", (fact.id,))
    assert type(facts.record(obs, human=HUMAN)) is Rejected
    with roster.transaction():
        assert type(facts.get_in_unit(fact.id, human=HUMAN)) is Rejected
        assert facts.known_in_unit(50, human=HUMAN) == ()
        assert facts.known_all_in_unit(50)[0]['id'] == fact.id


def test_kill_duplicate_reuses_original_and_native_changes_reject(roster):
    obs = committed_observation(roster, 'verified_raid_kill_v1')
    facts = FactStore(roster)
    first = facts.record(obs, human=HUMAN)
    replay = committed_observation(roster, 'verified_raid_kill_v1', received=1011.0, captured=1011.0)
    assert facts.record(replay, human=HUMAN) == first
    changed = committed_observation(roster, 'verified_raid_kill_v1', payload={'fight_ms': 2346}, received=1012.0)
    assert type(facts.record(changed, human=HUMAN)) is Rejected
    assert counts(roster) == (1, 1, 1, 2, 1)
    with roster.transaction():
        roster.conn.execute("UPDATE realm_kill SET qualification_sha256=?", ('0' * 64,))
    assert type(facts.record(replay, human=HUMAN)) is Rejected
    assert counts(roster) == (1, 1, 1, 2, 1)


def test_missing_kill_cannot_be_repaired_on_source_replay(roster):
    obs = committed_observation(roster, 'verified_raid_kill_v1')
    facts = FactStore(roster)
    assert type(facts.record(obs, human=HUMAN)) is FactRow
    with roster.transaction():
        roster.conn.execute('DELETE FROM realm_kill')
    assert type(facts.record(obs, human=HUMAN)) is Rejected
    assert counts(roster) == (1, 1, 1, 2, 0)


def test_known_skips_invalid_before_limit_and_rechecks_current_rank(roster, monkeypatch):
    facts = FactStore(roster)
    first = facts.record(committed_observation(roster, payload={'ts': 1000000}), human=HUMAN)
    second = facts.record(committed_observation(roster, payload={'ts': 1001000}, captured=1011.0), human=HUMAN)
    invalid = []
    for n in range(7):
        fact = facts.record(committed_observation(roster, payload={'ts': 1002000 + n * 1000}, captured=1020.0), human=HUMAN)
        invalid.append(fact)
    with roster.transaction():
        for fact in invalid:
            roster.conn.execute("UPDATE realm_fact SET text='tampered' WHERE id=?", (fact.id,))
    real = module.public_fact_in_unit
    calls = []
    def counted(*args, **kwargs):
        calls.append(args[1])
        return real(*args, **kwargs)
    monkeypatch.setattr(module, 'public_fact_in_unit', counted)
    assert facts.known(50, human=HUMAN, limit=1) == (second,)
    assert len(calls) == 8
    calls.clear()
    assert facts.known(50, human=HUMAN, limit=5) == (second, first)
    assert len(calls) == 9
    with roster.transaction():
        roster.conn.execute("UPDATE realm_knows SET how='server' WHERE fact=? AND guild=50", (first.id,))
    assert facts.known(50, human=HUMAN, limit=5) == (second,)
    assert len(facts.known_all(50)) == 9


@pytest.mark.parametrize('guild,limit', [(0, 1), (True, 1), (1, 0), (1, 6), (1, True)])
def test_read_arguments_are_exact_bounded_integers(roster, guild, limit):
    with pytest.raises(ValueError):
        FactStore(roster).known(guild, human=HUMAN, limit=limit)

@pytest.mark.parametrize('mutation', [
    "UPDATE realm_fact SET text='changed'",
    "UPDATE realm_fact SET ts=ts+1",
    "UPDATE realm_fact SET public=0",
    "UPDATE realm_fact_origin SET proof_json='{}'",
    "UPDATE realm_fact_origin SET text_sha256='0000000000000000000000000000000000000000000000000000000000000000'",
    "UPDATE realm_fact_origin SET state='revoked'",
    "UPDATE realm_knows SET since=since+1 WHERE guild=50",
    "UPDATE realm_knows SET how='server' WHERE guild=50",
    "DELETE FROM realm_knows WHERE guild=60",
    "INSERT INTO realm_knows(fact,guild,since,how) SELECT id,999,ts,'own' FROM realm_fact",
    "UPDATE game_event SET payload='{}'",
])
def test_current_get_and_known_reject_changed_complete_proof(roster, mutation):
    obs = committed_observation(roster)
    facts = FactStore(roster)
    fact = facts.record(obs, human=HUMAN)
    with roster.transaction():
        roster.conn.execute(mutation)
        assert type(facts.get_in_unit(fact.id, human=HUMAN)) is Rejected
        assert facts.known_in_unit(50, human=HUMAN) == ()


@pytest.mark.parametrize('mutation', [
    'UPDATE realm_kill SET instance_id=instance_id+1',
    "UPDATE realm_kill SET native_cast_json='[]'",
    "UPDATE realm_kill SET qualified_cast_json='[]'",
    "UPDATE realm_kill SET qualification_sha256='0000000000000000000000000000000000000000000000000000000000000000'",
    'UPDATE realm_kill SET fact=NULL',
])
def test_kill_stored_projection_must_match_original(roster, mutation):
    obs = committed_observation(roster, 'verified_raid_kill_v1')
    facts = FactStore(roster)
    assert type(facts.record(obs, human=HUMAN)) is FactRow
    with roster.transaction():
        roster.conn.execute(mutation)
    assert type(facts.record(obs, human=HUMAN)) is Rejected
    assert counts(roster) == (1, 1, 1, 2, 1)


def test_unsupported_class_missing_receipt_and_backlog_never_write(roster):
    obs = committed_observation(roster)
    facts = FactStore(roster)
    for bad in (replace(obs, event=object()), replace(obs, receipt=None),
                replace(obs, receipt=replace(obs.receipt, backlog=True))):
        assert type(facts.record(bad, human=HUMAN)) is Rejected
        assert counts(roster) == (0, 0, 0, 0, 0)


def test_server_rank_precedes_newer_present_and_revisions_are_current(roster):
    facts = FactStore(roster)
    present = facts.record(committed_observation(roster, payload={'ts': 1001000}), human=HUMAN)
    server = facts.record(committed_observation(roster, 'wg_result_v1'), human=HUMAN)
    assert facts.known(60, human=HUMAN) == (server, present)
    with roster.transaction():
        roster.conn.execute('UPDATE realm_fact_origin SET revision=2 WHERE fact=?', (server.id,))
        reread, _ = facts.get_in_unit(server.id, human=HUMAN)
        assert reread.revision == 2
    assert facts.known(60, human=HUMAN)[0].revision == 2


def test_real_parser_canonicalizes_equivalent_kill_envelopes_for_replay(roster):
    from dataclasses import asdict
    from leeroy_mind.ingress import _wire_origin
    obs = committed_observation(roster, 'verified_raid_kill_v1')
    raw = {'v': 1, 'type': 'realm_kill', **asdict(obs.event)}
    wire = _wire_origin(json.dumps(raw, indent=2), received_at=1020.0, transport_epoch=7)
    assert wire is not None and wire.envelope_sha256 == obs.receipt.wire.envelope_sha256
    replay = replace(obs, receipt=replace(obs.receipt, wire=wire))
    facts = FactStore(roster)
    first = facts.record(obs, human=HUMAN)
    assert facts.record(replay, human=HUMAN) == first
    assert counts(roster) == (1, 1, 1, 2, 1)


@pytest.mark.parametrize('changes', [
    {'native_epoch': 30}, {'guild_id': 51}, {'human_guid': 11},
    {'map': 724}, {'instance_id': 10}, {'boss_id': 1},
    {'difficulty': 1, 'size': 25}, {'difficulty': 2, 'heroic': True},
    {'killed_at_ms': 999989}, {'verified_at_ms': 999998}, {'ts': 1000001},
    {'cast_members': [101, 102, 103, 104, 105, 106]},
    {'human_present': False, 'fight_started_at_ms': 0, 'fight_ms': -1},
    {'fight_started_at_ms': 998999}, {'fight_ms': 2346},
])
def test_every_conflicting_native_projection_is_rejected(roster, changes):
    from leeroy_mind.ingress import _wire_origin
    from dataclasses import asdict
    facts = FactStore(roster)
    original = committed_observation(roster, 'verified_raid_kill_v1')
    assert type(facts.record(original, human=HUMAN)) is FactRow
    changed = committed_observation(roster, 'verified_raid_kill_v1', payload=changes)
    raw = {'v': 1, 'type': 'realm_kill', **asdict(changed.event)}
    wire = _wire_origin(json.dumps(raw), received_at=1010.0, transport_epoch=7)
    assert wire is not None and wire.envelope_sha256 == changed.receipt.wire.envelope_sha256
    assert wire.envelope_sha256 != original.receipt.wire.envelope_sha256
    assert type(facts.record(changed, human=HUMAN)) is Rejected
    assert counts(roster) == (1, 1, 1, 2, 1)


def test_import_does_not_attach_or_register(tmp_path):
    import os
    from pathlib import Path
    import subprocess
    import sys
    package_root = Path(__file__).resolve().parents[2]
    probe = tmp_path / 'fact_import_probe.py'
    probe.write_text('''\
import sys
sys.path.insert(0, sys.argv[1])
import leeroy_mind.protocol as protocol
before = dict(protocol._events), dict(protocol._commands)
import leeroy_mind.realm.fact_store
assert before == (dict(protocol._events), dict(protocol._commands))
for name in ("leeroy_mind.realm.service", "leeroy_mind.realm.ports", "leeroy_mind.config"):
    assert name not in sys.modules, name
''', encoding='utf-8')
    data_dir = tmp_path / 'data'
    data_dir.mkdir()
    subprocess.run([sys.executable, '-I', str(probe), str(package_root)], cwd=tmp_path,
        env={**os.environ, 'LEEROY_MIND_DATA': str(data_dir)}, timeout=10,
        check=True, capture_output=True, text=True)


def test_distinct_native_id_remains_an_independent_kill(roster):
    facts = FactStore(roster)
    first = facts.record(committed_observation(roster, 'verified_raid_kill_v1'), human=HUMAN)
    other = facts.record(committed_observation(roster, 'verified_raid_kill_v1',
        payload={'kill_id': 'a' * 32 + ':2'}), human=HUMAN)
    assert type(first) is FactRow and type(other) is FactRow and first.id != other.id
    assert counts(roster) == (2, 2, 2, 4, 2)


def test_last_knowledge_insert_failure_rolls_back(roster):
    with roster.transaction():
        roster.conn.execute("INSERT INTO realm_guild(id,role,faction,name,added) VALUES (80,'gankers','horde','X',1)")
    obs = committed_observation(roster, 'verified_raid_kill_v1')
    with roster.transaction():
        roster.conn.execute("CREATE TEMP TRIGGER fail_last BEFORE INSERT ON realm_knows WHEN NEW.guild=80 "
                            "BEGIN SELECT RAISE(ABORT,'injected'); END")
    with pytest.raises(sqlite3.IntegrityError):
        FactStore(roster).record(obs, human=HUMAN)
    assert counts(roster) == (0, 0, 0, 0, 0)


def test_outer_commit_failure_rolls_back_complete_reducer(roster):
    obs = committed_observation(roster)
    with roster.transaction():
        roster.conn.execute('CREATE TABLE deferred_test (fact INTEGER REFERENCES realm_fact(id) '
                            'DEFERRABLE INITIALLY DEFERRED)')
    with pytest.raises(sqlite3.IntegrityError):
        with roster.transaction():
            roster.conn.execute('INSERT INTO deferred_test(fact) VALUES (999999)')
            FactStore(roster).put_fact_in_unit(obs, human=HUMAN)
    assert counts(roster) == (0, 0, 0, 0, 0)
    assert roster.conn.execute('SELECT count(*) FROM game_event').fetchone()[0] == 1


_REPLAY_CASES = [
    ('bg_result_v1', {'members': [201]}),
    ('verified_raid_kill_v1', {'human_present': False, 'fight_started_at_ms': 0, 'fight_ms': -1}),
    ('channel_raid_topic_v1', {'from_guid': 201, 'from_name': 'Rivalname'}),
]


def lose_current_eligibility(store, policy):
    if policy == 'verified_raid_kill_v1':
        store.conn.execute('DELETE FROM cast_member WHERE guid BETWEEN 101 AND 105')
    else:
        store.conn.execute("UPDATE realm_actor SET status='retired' WHERE guid=201")


def realm_rows(store):
    return tuple(tuple(tuple(row) for row in store.conn.execute(f'SELECT * FROM {table} ORDER BY 1'))
                 for table in TABLES)


@pytest.mark.parametrize('policy,payload', _REPLAY_CASES)
def test_replay_uses_original_decision_after_current_eligibility_is_lost(roster, policy, payload):
    from leeroy_mind.realm.provenance import validate_live_observation_in_unit
    facts = FactStore(roster)
    original = committed_observation(roster, policy, payload=payload)
    first = facts.record(original, human=HUMAN)
    assert type(first) is FactRow
    with roster.transaction():
        before = realm_rows(roster)
        original_qualification = roster.conn.execute('SELECT qualification_json FROM realm_fact_origin WHERE fact=?',
                                                       (first.id,)).fetchone()[0]
        lose_current_eligibility(roster, policy)
        replay = observe(roster, policy, payload=payload, received=1020.0, captured=1020.0)
        assert replay.receipt.row_id != original.receipt.row_id
        assert replay.receipt.wire.envelope_sha256 == original.receipt.wire.envelope_sha256
        if policy == 'channel_raid_topic_v1':
            assert replay.qualification.speaker_kind == 'none'
        elif policy == 'verified_raid_kill_v1':
            assert replay.qualification.cast == ()
        else:
            assert not any(actor.guid == 201 for actor in replay.qualification.actors)
        assert type(validate_live_observation_in_unit(roster, replay, human=HUMAN, policy=policy)) is Rejected
        assert facts.get_in_unit(first.id, human=HUMAN)[0] == first
    assert facts.record(replay, human=HUMAN) == first
    with roster.transaction():
        assert realm_rows(roster) == before
        assert roster.conn.execute('SELECT qualification_json FROM realm_fact_origin WHERE fact=?',
                                   (first.id,)).fetchone()[0] == original_qualification


@pytest.mark.parametrize('policy,payload', _REPLAY_CASES)
def test_first_unqualified_event_never_claims_source(roster, policy, payload):
    with roster.transaction():
        lose_current_eligibility(roster, policy)
        obs = observe(roster, policy, payload=payload)
    assert type(FactStore(roster).record(obs, human=HUMAN)) is Rejected
    assert counts(roster) == (0, 0, 0, 0, 0)


@pytest.mark.parametrize('policy,payload', _REPLAY_CASES)
def test_replay_after_eligibility_loss_still_checks_new_binding_and_original_revocation(roster, policy, payload):
    facts = FactStore(roster)
    original = committed_observation(roster, policy, payload=payload)
    first = facts.record(original, human=HUMAN)
    assert type(first) is FactRow
    with roster.transaction():
        lose_current_eligibility(roster, policy)
        bad = observe(roster, policy, payload=payload, received=1020.0, captured=1020.0)
        roster.conn.execute("UPDATE game_event SET payload='{}' WHERE id=?", (bad.receipt.row_id,))
        before = realm_rows(roster)
    assert type(facts.record(bad, human=HUMAN)) is Rejected
    with roster.transaction():
        assert realm_rows(roster) == before
        replay = observe(roster, policy, payload=payload, received=1021.0, captured=1021.0)
        roster.conn.execute("UPDATE realm_fact_origin SET state='revoked' WHERE fact=?", (first.id,))
        revoked_rows = realm_rows(roster)
    assert type(facts.record(replay, human=HUMAN)) is Rejected
    with roster.transaction():
        assert realm_rows(roster) == revoked_rows
