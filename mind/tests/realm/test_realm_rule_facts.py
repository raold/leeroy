"""Rule-derived Realm facts over a real Store, and known() over both origin tables."""
from dataclasses import replace
import hashlib
import json

import pytest

from leeroy_mind.realm.fact_store import FactStore
from leeroy_mind.realm.records import FactRow, Rejected
from leeroy_mind.realm.rule_facts import put_rule_fact_in_unit, rule_fact_in_unit
from test_realm_fact_store import committed_observation
from test_realm_live_proof import HUMAN, roster

KEY = "60:631:12:2026-11-03"
TEXT = "Synthetic Guild killed The Lich King."
TABLES = ("realm_fact", "realm_rule_origin", "realm_knows")


def _put(store, **overrides):
    values = dict(policy="race_rule_kill_v1", key=KEY, kind="rival_kill", text=TEXT, public=True,
                  known_by=[(60, "own"), (50, "server")], inputs={"guild": 60, "map": 631, "boss": 12},
                  ts=1000.0, human=HUMAN)
    values.update(overrides)
    with store.transaction():
        return put_rule_fact_in_unit(store, **values)


def _counts(store):
    return tuple(store.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in TABLES)


def _read(store, fact_id, human=HUMAN):
    with store.transaction():
        return rule_fact_in_unit(store, fact_id, human=human)


def test_rule_fact_written_with_origin_and_knows(roster):
    fact = _put(roster)
    key = f"rule:race_rule_kill_v1:{KEY}"
    assert fact == FactRow(fact.id, "rival_kill", TEXT, "rule", key, True, 1000.0, 1)
    origin = roster.conn.execute("SELECT * FROM realm_rule_origin").fetchone()
    assert dict(origin) == dict(fact=fact.id, producer_key=key, policy="race_rule_kill_v1",
                                inputs_json='{"boss":12,"guild":60,"map":631}',
                                known_json='[[50,"server"],[60,"own"]]',
                                text_sha256=hashlib.sha256(TEXT.encode()).hexdigest(), revision=1, state="active")
    assert [tuple(r) for r in roster.conn.execute("SELECT fact,guild,since,how FROM realm_knows ORDER BY guild")] == [
        (fact.id, 50, 1000.0, "server"), (fact.id, 60, 1000.0, "own")]
    assert _read(roster, fact.id) == fact
    assert FactStore(roster).known(50, human=HUMAN) == (fact,)
    with pytest.raises(RuntimeError):  # The caller owns the unit.
        put_rule_fact_in_unit(roster, policy="race_rule_kill_v1", key="other", kind="rival_kill", text=TEXT,
                              public=True, known_by=[(60, "own")], inputs={}, ts=1.0, human=HUMAN)
    with pytest.raises(RuntimeError):
        rule_fact_in_unit(roster, fact.id, human=HUMAN)


@pytest.mark.parametrize("policy,source", [
    ("benchmark_loss_v1", "benchmark"), ("poach_offer_v1", "rule"),
    ("night_beat_v1", "night"), ("forum_post_v1", "night"),
])
def test_rule_fact_policies_carry_their_source(roster, policy, source):
    fact = _put(roster, policy=policy)
    assert (fact.source, fact.ref) == (source, f"rule:{policy}:{KEY}")


def test_rule_fact_cleans_its_text_as_a_memory(roster):
    fact = _put(roster, text="  Synthetic Guild   killed |cffff0000The Lich King|r. ")
    assert fact.text == TEXT


@pytest.mark.parametrize("text,reason", [
    ("Synthetic Private carried the raid.", "real_name"),
    ("SYNTHETIC PRIVATE carried the raid.", "real_name"),
    ("she left the guild lol", "unsupported_text"),
    ("Two of them quit the guild after the wipe.", "unsupported_text"),
    ("x" * 201, "unsupported_text"),
    ("", "unsupported_text"),
    ("   ", "unsupported_text"),
    ("Rivalname is dating their healer.", "romance"),
    (None, "invalid_input"),
])
def test_rule_fact_rejects_real_name_leaves_and_length(roster, text, reason):
    assert _put(roster, text=text) == Rejected(reason)
    assert _counts(roster) == (0, 0, 0)
    assert type(_put(roster, text="x" * 200)) is FactRow


@pytest.mark.parametrize("overrides,reason", [
    (dict(policy="verified_raid_kill_v1"), "unsupported_policy"),
    (dict(key=""), "invalid_input"),
    (dict(key="has space"), "invalid_input"),
    (dict(key="x" * 97), "invalid_input"),
    (dict(kind="Rival Kill"), "invalid_input"),
    (dict(public=1), "invalid_input"),
    (dict(known_by=[]), "invalid_input"),
    (dict(known_by=[(60, "own"), (60, "server")]), "invalid_input"),
    (dict(known_by=[(0, "own")]), "invalid_input"),
    (dict(known_by=[(60, "heard")]), "invalid_input"),
    (dict(inputs=[1]), "invalid_input"),
    (dict(inputs={"x": float("nan")}), "invalid_input"),
    (dict(inputs={"x": "y" * 8200}), "invalid_input"),
    (dict(ts=float("inf")), "invalid_input"),
    (dict(ts=float("nan")), "invalid_input"),
    (dict(ts=10 ** 400), "invalid_input"),
    (dict(ts=-1.0), "invalid_input"),
    (dict(ts=True), "invalid_input"),
    (dict(human=None), "invalid_input"),
])
def test_rule_fact_rejects_malformed_inputs(roster, overrides, reason):
    assert _put(roster, **overrides) == Rejected(reason)
    assert _counts(roster) == (0, 0, 0)


def test_rule_fact_key_idempotent(roster):
    fact = _put(roster)
    assert _put(roster) == fact
    assert _put(roster, ts=2000.0, known_by=[(50, "server"), (60, "own")]) == fact  # Same knowers, any time.
    assert _counts(roster) == (1, 1, 2)
    for changed in (dict(text="Synthetic Guild killed Halion."), dict(inputs={"guild": 60}),
                    dict(known_by=[(60, "own")]), dict(public=False), dict(kind="rival_progress")):
        assert _put(roster, **changed) == Rejected("proof_changed")
    assert _counts(roster) == (1, 1, 2)
    other = _put(roster, key="60:631:13:2026-11-03", text="Synthetic Guild killed Halion.")
    assert other.id != fact.id and _counts(roster) == (2, 2, 4)


@pytest.mark.parametrize("mutation,reason", [
    ("UPDATE realm_fact SET text='Synthetic Guild killed Halion.'", "proof_changed"),
    ("UPDATE realm_fact SET ts=ts+1", "proof_changed"),
    ("UPDATE realm_fact SET source='night'", "invalid_row"),
    ("UPDATE realm_fact SET ref='rule:race_rule_kill_v1:other'", "invalid_row"),
    ("UPDATE realm_rule_origin SET state='revoked'", "private_evidence"),
    ("UPDATE realm_knows SET how='forum' WHERE guild=50", "proof_changed"),
    ("DELETE FROM realm_knows WHERE guild=60", "proof_changed"),
    ("INSERT INTO realm_knows(fact,guild,since,how) SELECT id,999,ts,'own' FROM realm_fact", "proof_changed"),
    ("UPDATE realm_rule_origin SET known_json='[[60,\"own\"],[50,\"server\"]]'", "invalid_row"),
])
def test_rule_fact_read_rechecks_its_origin(roster, mutation, reason):
    fact = _put(roster)
    with roster.transaction():
        roster.conn.execute(mutation)
    assert _read(roster, fact.id) == Rejected(reason)
    assert FactStore(roster).known(50, human=HUMAN) == ()
    assert _put(roster) == Rejected(reason)  # A replay never repairs it.


def test_rule_fact_read_screens_against_his_current_identity(roster):
    fact = _put(roster, text="Rivalname says the Lich King was easy.")
    renamed = replace(HUMAN, real_name="Rivalname")
    assert _read(roster, fact.id, renamed) == Rejected("real_name")
    with roster.transaction():
        assert FactStore(roster).known_in_unit(50, human=renamed) == ()
    assert _read(roster, 999) == Rejected("missing_row")
    assert _read(roster, 0) == Rejected("invalid_input")


def test_known_unions_event_and_rule_facts(roster):
    facts = FactStore(roster)
    event = facts.record(committed_observation(roster), human=HUMAN)  # ts 1000, guild 50 present
    server = _put(roster, key="a", ts=900.0, known_by=[(50, "server")])
    newer = _put(roster, key="b", ts=2000.0, text="Synthetic Guild killed Halion.", known_by=[(50, "own")])
    private = _put(roster, policy="poach_offer_v1", key="c", ts=3000.0, kind="poach_offer", public=False,
                   text="Rivalname whispered an offer.", known_by=[(50, "own")])
    other = _put(roster, key="d", ts=4000.0, text="Synthetic Guild wiped on Halion.", known_by=[(60, "own")])
    assert type(event) is FactRow and type(private) is FactRow and type(other) is FactRow
    assert facts.known(50, human=HUMAN) == (server, newer, event)
    assert facts.known(50, human=HUMAN, limit=2) == (server, newer)
    for n in range(5):
        _put(roster, key=f"e{n}", ts=5000.0 + n, text=f"Synthetic Guild wiped {n + 2} times.", known_by=[(50, "own")])
    known = facts.known(50, human=HUMAN)
    assert len(known) == 5 and known[0] == server and [f.ts for f in known[1:]] == [5004.0, 5003.0, 5002.0, 5001.0]
    with roster.transaction():  # A rule origin and an event origin on one fact: neither counts.
        roster.conn.execute("INSERT INTO realm_rule_origin SELECT ?,'rule:night_beat_v1:x','night_beat_v1',"
                            "'{}','[[50,\"present\"]]',text_sha256,1,'active' FROM realm_fact_origin WHERE fact=?",
                            (event.id, event.id))
        roster.conn.execute("DELETE FROM realm_knows WHERE fact IN (SELECT fact FROM realm_rule_origin "
                            "WHERE producer_key LIKE 'rule:race_rule_kill_v1:e%')")
        roster.conn.execute("DELETE FROM realm_rule_origin WHERE producer_key LIKE 'rule:race_rule_kill_v1:e%'")
    assert facts.known(50, human=HUMAN) == (server, newer)


def test_private_rule_fact_only_in_known_all(roster):
    facts = FactStore(roster)
    private = _put(roster, policy="poach_offer_v1", key="60:101:2026-W45", kind="poach_offer", public=False,
                   text="Rivalname whispered Testmaster's officer an offer.", known_by=[(60, "own")])
    assert private.public is False
    assert _read(roster, private.id) == private  # The rules may read it.
    assert facts.known(60, human=HUMAN) == ()
    assert [row["id"] for row in facts.known_all(60)] == [private.id]
    assert json.loads(roster.conn.execute("SELECT known_json FROM realm_rule_origin").fetchone()[0]) == [[60, "own"]]
