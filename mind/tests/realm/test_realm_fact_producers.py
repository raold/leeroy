"""Six real producer paths, using captured observations and source proofs."""
import json

import pytest

from leeroy_mind.realm.fact_store import FactStore
from leeroy_mind.realm.records import FactRow, Rejected
from test_realm_live_proof import EXPECTED, HUMAN, TYPES, observe, roster


@pytest.mark.parametrize('policy', list(TYPES))
def test_producers_use_the_actual_audit_and_complete_initial_knowledge(roster, policy):
    with roster.transaction():
        observation = observe(roster, policy)
    facts = FactStore(roster)
    fact = facts.record(observation, human=HUMAN)
    assert type(fact) is FactRow
    text, knowledge = EXPECTED[policy]
    kind = {'bg_result_v1': 'bg_result', 'arena_result_v1': 'arena_result',
            'wg_result_v1': 'wg_result', 'pvp_result_v1': 'pvp_kill',
            'channel_raid_topic_v1': 'channel_topic', 'verified_raid_kill_v1': 'kill'}[policy]
    assert (fact.kind, fact.text, fact.source, fact.ref, fact.ts, fact.revision) == (
        kind, text, 'channel' if policy == 'channel_raid_topic_v1' else 'event',
        f'event:{observation.receipt.row_id}', 1000.0, 1)
    with roster.transaction():
        row = roster.conn.execute('SELECT o.*,s.source_key FROM realm_fact_origin o '
                                  'JOIN realm_source s ON s.id=o.source WHERE o.fact=?', (fact.id,)).fetchone()
        assert row['producer_key'] == f"event:{row['source_key']}:{policy}"
        assert row['state'] == 'active' and row['revision'] == 1
        assert json.loads(row['proof_json'])['qualification'] == json.loads(row['qualification_json'])
        assert [list(r) for r in roster.conn.execute('SELECT guild,how FROM realm_knows '
                    'WHERE fact=? ORDER BY guild', (fact.id,))] == knowledge
        assert facts.get_in_unit(fact.id, human=HUMAN)[0] == fact
        assert roster.conn.execute('SELECT count(*) FROM realm_kill').fetchone()[0] == (policy == 'verified_raid_kill_v1')


def test_four_native_cast_without_human_rejected_without_realm_output(roster):
    with roster.transaction():
        obs = observe(roster, 'verified_raid_kill_v1', payload={
            'human_present': False, 'cast_members': [101, 102, 103, 104],
            'fight_started_at_ms': 0, 'fight_ms': -1})
    assert type(FactStore(roster).record(obs, human=HUMAN)) is Rejected
    with roster.transaction():
        for table in ('realm_source', 'realm_fact', 'realm_fact_origin', 'realm_knows', 'realm_kill'):
            assert roster.conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == 0


def test_five_native_cast_without_human_are_qualified(roster):
    with roster.transaction():
        obs = observe(roster, 'verified_raid_kill_v1', payload={
            'human_present': False, 'fight_started_at_ms': 0, 'fight_ms': -1})
    assert type(FactStore(roster).record(obs, human=HUMAN)) is FactRow
