"""Whole-job no-change receipts free only ordinary roots with consistent evidence."""
from dataclasses import replace
import json
from pathlib import Path
import sqlite3

import pytest

from leeroy_mind.protocol import Ack, ProtocolError, parse_event
from leeroy_mind.realm import protocol as p
from leeroy_mind.realm.action_policy import classify, document
from leeroy_mind.realm.action_store import ActionSpec, ActionStore

CAST = tuple(range(101, 245))  # Invented manifest; the owner is guid 1.


@pytest.fixture
def actions():
    conn = sqlite3.connect(":memory:", autocommit=False)
    migration = Path(__file__).parents[2] / "leeroy_mind/storage/migrations/0010_realm_actions.sql"
    conn.executescript(migration.read_text(encoding="utf-8"))
    yield ActionStore(conn)
    conn.close()


def start(actions, cmd=None):
    cmd = cmd or p.LeavePvp("leave", CAST)
    c = classify(cmd)
    spec = ActionSpec(cmd.id, None, "root", c.policy, "realm_day", 4, c.kind,
                      c.payload, c.reserved_manifest, 50, 1)
    record = actions.prepare(spec, now=1)
    actions.begin_dispatch(cmd.id, payload=record.payload, process_id="test", connection_epoch=1, now=2)
    return record


def failure(**changes):
    return replace(p.RealmJob(0, "leave", "leave_pvp", "failed", "wg_private_state_unavailable", 101, 0,
                             changed=False), **changes)


@pytest.mark.parametrize("job_first", [False, True])
@pytest.mark.parametrize("reason", ["wg_private_state_unavailable", "cleanup_member_not_admitted"])
def test_no_change_frees_all_144_members_after_both_receipts(actions, job_first, reason, caplog):
    start(actions)
    caplog.set_level("INFO", logger="leeroy_mind.realm.action_store")
    receipts = [lambda: actions.observe_ack(Ack(0, "leave", True, ""), now=3),
                lambda: actions.observe_realm_job(failure(reason=reason), now=4)]
    if job_first: receipts.reverse()
    assert not receipts[0]().result_ready
    assert actions.holds()[0].guids == frozenset(CAST)
    transition = receipts[1]()
    assert (transition.result_ready, transition.result_ok, transition.reason) == (True, False, "step_refused")
    assert actions.get("leave").state == "completed" and actions.holds() == ()
    actions.observe_realm_job(failure(reason=reason), now=5)
    actions.recover(process_id="next", now=6)
    assert caplog.messages == ["realm_action_step_refused id=leave kind=leave_pvp "
                               f"reason={reason} members=144"]


@pytest.mark.parametrize("changed", [None, True])
def test_old_or_changed_failure_still_holds_everybody(actions, changed):
    start(actions)
    actions.observe_ack(Ack(0, "leave", True, ""), now=3)
    actions.observe_realm_job(failure(changed=changed), now=4)
    assert actions.get("leave").state == "unknown"
    assert actions.holds()[0].guids == frozenset(CAST)


@pytest.mark.parametrize("cmd,reason", [
    (p.Rearm("leave", (101,)), "recorded_weapon_unavailable"),
    (p.Equip("leave", 101, 5976), "cannot_equip"),
])
@pytest.mark.parametrize("changed,expected", [(False, "completed"), (True, "unknown"), (None, "unknown")])
def test_other_ordinary_moves_require_the_same_whole_job_proof(actions, cmd, reason, changed, expected):
    start(actions, cmd)
    actions.observe_ack(Ack(0, "leave", True, ""), now=3)
    actions.observe_realm_job(failure(kind=classify(cmd).kind, reason=reason, changed=changed), now=4)
    assert actions.get("leave").state == expected


def test_explicit_change_overrides_a_legacy_reason_only_refusal(actions):
    start(actions, p.Rearm("leave", (101,)))
    actions.observe_ack(Ack(0, "leave", True, ""), now=3)
    actions.observe_realm_job(failure(kind="rearm", reason="busy:combat", changed=True), now=4)
    assert actions.get("leave").state == "unknown" and actions.holds()


@pytest.mark.parametrize("changes", [{"guid": 999}, {"value": 1}, {"pvp_owner": "a" * 32 + "-" + "b" * 16}])
def test_false_is_not_enough_with_an_unbound_receipt(actions, changes):
    start(actions)
    actions.observe_ack(Ack(0, "leave", True, ""), now=3)
    actions.observe_realm_job(failure(**changes), now=4)
    assert actions.get("leave").state != "completed" and actions.holds()


@pytest.mark.parametrize("ack", [None, False])
def test_no_change_without_positive_admission_keeps_the_hold(actions, ack):
    start(actions)
    if ack is not None: actions.observe_ack(Ack(0, "leave", ack, "busy:combat"), now=3)
    actions.observe_realm_job(failure(), now=4)
    actions.recover(process_id="next", now=5)
    assert actions.get("leave").state == "unknown" and actions.holds()


def test_recovery_settles_only_an_uncontested_stored_no_change_failure(actions):
    start(actions)
    actions.observe_ack(Ack(0, "leave", True, ""), now=3)
    actions.observe_realm_job(failure(changed=None), now=4)
    old = actions.get("leave")
    evidence = json.loads(old.evidence)
    evidence["job"]["changed"] = False
    with actions.conn:
        actions.conn.execute("UPDATE realm_action SET evidence=? WHERE command_id='leave'", (document(evidence),))
    assert actions.recover(process_id="next", now=5) == ()
    assert actions.get("leave").result_reason == "step_refused" and actions.holds() == ()
    assert json.loads(actions.get("leave").evidence) == evidence


@pytest.mark.parametrize("contradiction", [failure(changed=True), failure(state="done", reason="", guid=0, value=144)])
def test_later_contradiction_restores_conflict_and_the_hold(actions, contradiction):
    start(actions)
    actions.observe_ack(Ack(0, "leave", True, ""), now=3)
    actions.observe_realm_job(failure(), now=4)
    assert actions.holds() == ()
    actions.observe_realm_job(contradiction, now=5)
    actions.observe_realm_job(failure(), now=6)
    actions.recover(process_id="next", now=7)
    assert actions.get("leave").state == "conflict"
    assert actions.holds()[0].guids == frozenset(CAST)


def test_no_change_never_settles_a_root_with_children(actions):
    start(actions)
    query = classify(p.GetRealmPvp("query", origin_id="leave"), child=True)
    with actions.conn:
        actions._insert(ActionSpec("query", "leave", "query", query.policy, "realm_day", 4, query.kind,
                                   query.payload, (), 50, 1), 3)
    actions.observe_ack(Ack(0, "leave", True, ""), now=4)
    actions.observe_realm_job(failure(), now=5)
    assert actions.get("leave").state == "unknown"


@pytest.mark.parametrize("cmd,receipt", [
    (p.WgJoin("leave", (101,)), failure(kind="wg_join", guid=0, pvp_owner="a" * 32 + "-" + "b" * 16)),
    (p.ItemCount("leave", 101, (5976,)), failure(kind="item_count")),
])
def test_wg_and_result_policies_do_not_use_no_change(actions, cmd, receipt):
    start(actions, cmd)
    actions.observe_ack(Ack(0, "leave", True, ""), now=3)
    actions.observe_realm_job(receipt, now=4)
    assert actions.get("leave").state == "unknown"


def test_ack_only_command_ignores_job_proof(actions):
    start(actions, p.BankMoney("leave", 10))
    assert not actions.observe_realm_job(failure(kind="bank_money"), now=3).matched
    assert actions.get("leave").state == "dispatched"


@pytest.mark.parametrize("bad", [0, 1, "false", [], {}])
def test_changed_is_a_strict_boolean_on_the_wire(bad):
    p.register()
    raw = dict(v=1, ts=0, type="realm_job", id="leave", kind="leave_pvp", state="failed",
               reason="wg_private_state_unavailable", guid=101, value=0, changed=bad)
    with pytest.raises(ProtocolError): parse_event(json.dumps(raw))


@pytest.mark.parametrize("private", ["invented private words", "InventedName", "inventedname"])
def test_new_and_old_wire_shapes_and_no_text_logs(actions, caplog, private):
    p.register()
    raw = dict(v=1, ts=0, type="realm_job", id="leave", kind="leave_pvp", state="failed",
               reason="wg_private_state_unavailable", guid=101, value=0)
    assert parse_event(json.dumps(raw)).changed is None
    assert parse_event(json.dumps({**raw, "changed": False})).changed is False
    start(actions)
    caplog.set_level("INFO", logger="leeroy_mind.realm.action_store")
    actions.observe_ack(Ack(0, "leave", True, ""), now=3)
    actions.observe_realm_job(failure(reason=private), now=4)
    assert caplog.messages == ["realm_action_step_refused id=leave kind=leave_pvp reason=native_refusal members=144"]
