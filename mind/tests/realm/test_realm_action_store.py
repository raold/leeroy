"""Durable reducers use the real ledger records and disposable SQLite only."""
from dataclasses import replace
import json
from pathlib import Path
import re
import sqlite3

import pytest

from leeroy_mind.guild.protocol import BotDo, Job, JobSkip
from leeroy_mind.protocol import Ack
from leeroy_mind.realm import action_store, protocol as p
from leeroy_mind.realm.action_policy import classify, decode, document
from leeroy_mind.realm.action_store import (ActionCapacity, ActionSpec, ActionStore, ActionUnavailable, BODY_FENCES,
                                            REFUSALS)

TOKEN = "a" * 32 + "-" + "b" * 16
REPO = Path(__file__).resolve().parents[3]


@pytest.fixture
def store():
    conn = sqlite3.connect(":memory:", autocommit=False)
    migration = Path(__file__).parents[2] / "leeroy_mind/storage/migrations/0010_realm_actions.sql"
    conn.executescript(migration.read_text(encoding="utf-8-sig"))
    yield ActionStore(conn)
    conn.close()


def prepare(store, cmd=None, now=0, owner="realm_day", rank=4):
    cmd = cmd or p.RealmMove("move", 2, "home")
    c = classify(cmd)
    return store.prepare(ActionSpec(cmd.id, None, "root", c.policy, owner, rank, c.kind,
                                  c.payload, c.reserved_manifest, 50, 1), now=now)


def dispatch(store, record, now=1):
    return store.begin_dispatch(record.command_id, payload=record.payload, process_id="process",
                                connection_epoch=2**64-1, now=now)


def job(command_id="move", kind="realm_move", state="done", reason="", guid=2, value=0, token=""):
    return p.RealmJob(0, command_id, kind, state, reason, guid, value, token)


def bot(command_id="defaults", *, guids=(2, 3), skipped=(), state="done", reason=""):
    return Job(0, command_id, "bot_do", state, reason, "", guids, skipped, 0, 0, 0, 0, 0, 0)


def wg(store, *, settle=True):
    root = dispatch(store, prepare(store, p.WgJoin("wg", (4, 3, 2))))
    if settle: store.observe_ack(Ack(0, "wg", True, ""), now=2)
    store.observe_realm_job(job("wg", "wg_join", guid=0, value=2, token=TOKEN), now=3)
    return store.get(root.command_id)


def snapshot(store, *, command_id="query", now=10, stage="active", revision=1,
             cleanup_id="", fenced=False, native=(2, 3), ack=True, origin_epoch=2**64-1,
             guild=50, human=1, token=TOKEN):
    query = store.prepare_query("wg", p.GetRealmPvp(command_id, origin_id="wg"), now=now)
    dispatch(store, query, now=now)
    ev = p.RealmPvp(0, command_id, token, "wg", "wintergrasp", origin_epoch, revision, guild,
                    human, native, stage, fenced, "", cleanup_id)
    store.observe_pvp(ev, now=now)
    if ack: store.observe_ack(Ack(0, command_id, True, ""), now=now)
    return ev


def cleanup(store, command_id="cleanup", now=11):
    root = store.get("wg")
    record = store.prepare_cleanup("wg", p.LeavePvp(command_id, root.native_manifest, TOKEN), now=now)
    return dispatch(store, record, now=now)


def legacy_announce(store, *, command_id="legacy", state="prepared", transport="not_entered", **changes):
    """Insert actual old persisted bytes, without passing new-command admission."""
    payload = document(dict(type="announce", id=command_id, map=631, instance_id=123, boss_id=0,
                            boss_name="Example", attempts=0, fight_s=0, first="week"))
    entered = transport != "not_entered"
    values = dict(command_id=command_id, parent_id=None, role="root", policy="ack", owner="realm_day", rank=4,
                  kind="announce", payload=payload, reserved_manifest="[]", native_manifest=None, guild=50, human=1,
                  state=state, transport_state=transport, process_id="old_process" if entered else "",
                  connection_epoch="7" if entered else "0", send_error="", evidence="{}", result_ok=None,
                  result_reason="", created=0.0, updated=1.0, dispatched=1.0 if entered else None)
    if state == "completed":
        values.update(evidence=document({"ack": {"ok": True, "error": ""}}), result_ok=1)
    values.update(changes)
    with store.conn:
        store.conn.execute("INSERT INTO realm_action(" + ",".join(values) + ") VALUES("
                           + ",".join("?" for _ in values) + ")", tuple(values.values()))
    return payload


@pytest.mark.parametrize("job_first", [False, True])
def test_ordinary_ack_job_both_orders_and_idempotence(store, job_first):
    r = dispatch(store, prepare(store))
    handlers = [lambda: store.observe_ack(Ack(0, "move", True, ""), now=2),
                lambda: store.observe_realm_job(job(value=2**64-1), now=3)]
    if job_first: handlers.reverse()
    assert not handlers[0]().result_ready
    assert store.holds()[0].scope_id == "move"
    t = handlers[1]()
    assert (t.result_ready, t.result_ok, t.value) == (True, True, 2**64-1)
    assert store.get("move").state == "completed"
    assert store.holds() == ()
    assert not handlers[0]().result_ready
    assert store.get("move").connection_epoch == 2**64-1


def test_dispatch_barrier_immutable_payload_and_prepared_has_no_hold(store):
    r = prepare(store)
    assert store.holds() == ()
    with pytest.raises(ValueError): store.begin_dispatch("move", payload=r.payload + " ", process_id="p", connection_epoch=1, now=1)
    assert store.get("move").state == "prepared"
    dispatch(store, r)
    with pytest.raises(ValueError): dispatch(store, r)
    hold, = store.holds()
    assert (hold.owner, hold.rank, hold.retained, hold.guids) == ("realm_day", 4, True, frozenset({2}))


def test_restart_never_replays_prepared_or_dispatched_and_retains_unknown(store):
    prepare(store, p.Rearm("prepared", (2,)))
    dispatch(store, prepare(store, p.Rearm("written", (3,))))
    unresolved = store.recover(process_id="new_process", now=20)
    assert [r.command_id for r in unresolved] == ["written"]
    assert store.get("prepared").state == "not_sent"
    assert (store.get("written").state, store.get("written").transport_state) == ("unknown", "settled")
    assert store.holds()[0].guids == frozenset({3})


@pytest.mark.parametrize("reason", sorted(REFUSALS))
def test_exact_ordinary_no_admission_refusal_allowlist(store, reason):
    dispatch(store, prepare(store))
    t = store.observe_ack(Ack(0, "move", False, reason), now=2)
    assert t.result_ready and t.result_ok is False
    assert store.get("move").state == "completed"
    assert store.holds() == ()


@pytest.mark.parametrize("reason", ["busy:combat", "busy:duplicate_operation", "operation_receipt_available", "refused", "busy:queue_full:extra"])
def test_nonallowlisted_refusal_retains_scope(store, reason):
    dispatch(store, prepare(store))
    store.observe_ack(Ack(0, "move", False, reason), now=2)
    assert store.get("move").state == "unknown"
    assert store.holds()[0].guids == frozenset({2})


def test_a_leave_refused_for_one_fenced_body_frees_the_whole_cast(store):
    cast = tuple(range(101, 207))
    dispatch(store, prepare(store, p.LeavePvp("leave", cast), owner="raid", rank=0))
    assert store.holds()[0].guids == frozenset(cast)
    t = store.observe_ack(Ack(0, "leave", False, "busy:raid_owner"), now=2)
    assert (t.result_ready, t.result_ok, t.reason) == (True, False, "refused")
    assert store.get("leave").state == "completed"
    assert store.holds() == ()


def test_recovery_settles_a_body_fence_an_older_build_kept_unknown(store, monkeypatch):
    monkeypatch.setattr(action_store, "REFUSALS", REFUSALS - BODY_FENCES)  # as the previous build read acks
    cast = tuple(range(101, 207))
    dispatch(store, prepare(store, p.LeavePvp("leave", cast), owner="raid", rank=0))
    store.observe_ack(Ack(0, "leave", False, "busy:raid_owner"), now=2)
    dispatch(store, prepare(store, p.Rearm("duplicate", (301,))))
    store.observe_ack(Ack(0, "duplicate", False, "busy:duplicate_operation"), now=3)
    dispatch(store, prepare(store, p.Rearm("lost", (302,))))
    store.observe_ack(Ack(0, "lost", False, "busy:raid_owner"), now=4)
    store.observe_send_error("lost", "TimeoutError", now=5)
    dispatch(store, prepare(store, p.GuildGrow("grow", 13, (303, 304))))
    store.observe_ack(Ack(0, "grow", True, ""), now=6)
    store.observe_realm_job(job("grow", "guild_grow", "failed", "operation_transition_unknown", 0, 1), now=7)
    assert [r.state for r in store.records()] == ["unknown"] * 4
    old = store.get("leave")
    monkeypatch.undo()
    unresolved = store.recover(process_id="new", now=20)
    leave = store.get("leave")
    assert (leave.state, leave.result_ok, leave.result_reason) == ("completed", False, "refused")
    assert json.loads(leave.evidence) == json.loads(old.evidence)  # the same native ack, read again
    assert [r.command_id for r in unresolved] == ["duplicate", "lost", "grow"]
    assert {h.scope_id: h.guids for h in store.holds()} == {
        "duplicate": frozenset({301}), "lost": frozenset({302}), "grow": frozenset({303, 304})}
    assert [r.command_id for r in store.recover(process_id="again", now=30)] == ["duplicate", "lost", "grow"]
    assert store.get("leave") == leave


def test_body_fences_are_native_admission_refusals_before_any_reservation():
    ports = (REPO / "modules/mod-leeroy/src/Realm/RealmPorts.h").read_text(encoding="utf-8")
    module = (REPO / "modules/mod-leeroy/src/Realm/RealmModule.cpp").read_text(encoding="utf-8")

    def body(text, signature):
        start = text.index(signature)
        return text[start:text.index("\n}\n", start)]

    fences = set()
    for text, signature in ((ports, "inline std::string NativeOwnershipReason("),
                            (ports, "inline std::string NativeMutationReason("),
                            (module, "std::string RealmModule::MutationReason("),
                            (module, "bool RealmModule::RegisterExecutor(")):
        fences |= set(re.findall(r'"([a-z][a-z_:]*)"', body(text, signature)))
    assert fences == BODY_FENCES
    accept = body(module, "bool RealmModule::AcceptDomain(")
    reserve = accept.index("_state->holders.emplace(")
    assert accept.index(".admit(entry, guid)") < reserve and accept.index("MutationReason(guid)") < reserve


@pytest.mark.parametrize("first", ["ack", "job"])
def test_late_contradictory_ack_is_absorbing_and_reinstates_hold(store, first):
    dispatch(store, prepare(store))
    if first == "ack": store.observe_ack(Ack(0, "move", True, ""), now=2)
    store.observe_realm_job(job(), now=3)
    store.observe_ack(Ack(0, "move", False, "realm_off"), now=4)
    assert store.get("move").state == "conflict"
    store.observe_ack(Ack(0, "move", True, ""), now=5)
    assert store.get("move").state == "conflict"
    assert store.holds()[0].guids == frozenset({2})


def test_failed_job_with_positive_ack_remains_unknown_and_conflicting_jobs_hold(store):
    dispatch(store, prepare(store))
    store.observe_ack(Ack(0, "move", True, ""), now=2)
    t = store.observe_realm_job(job(state="failed", reason="busy:body_identity"), now=3)
    assert t.result_ready and t.result_ok is False
    assert store.get("move").state == "unknown"
    store.observe_realm_job(job(), now=4)
    assert store.get("move").state == "conflict"
    assert store.holds()


CAST = tuple(range(101, 207))


@pytest.mark.parametrize("reason", ["busy:combat", "busy:dead", "busy:offline", "busy:teleport", "busy:duel",
                                    "busy:raid_owner", "busy:body_identity_changed", "not_actor"])
@pytest.mark.parametrize("job_first", [False, True])
def test_a_rearm_native_refused_at_one_step_settles_refused_and_holds_nobody(store, reason, job_first):
    dispatch(store, prepare(store, p.Rearm("rearm", CAST)))
    handlers = [lambda: store.observe_ack(Ack(0, "rearm", True, ""), now=2),
                lambda: store.observe_realm_job(job("rearm", "rearm", "failed", reason, CAST[27]), now=3)]
    if job_first: handlers.reverse()
    assert not handlers[0]().result_ready
    t = handlers[1]()
    assert (t.result_ready, t.result_ok, t.reason) == (True, False, "step_refused")
    record = store.get("rearm")
    assert (record.state, record.result_ok, record.result_reason) == ("completed", False, "step_refused")
    assert "failed_at" in json.loads(record.evidence)  # the usual back-off before the same rearm again
    assert store.holds() == ()


@pytest.mark.parametrize("cmd,reason,guid,value", [
    (p.Rearm("rearm", CAST), "operation_transition_unknown", CAST[27], 0),  # the post-step check failed
    (p.Rearm("rearm", CAST), "operation_outcome_unknown", CAST[27], 0),
    (p.Rearm("rearm", CAST), "recorded_weapon_unavailable", CAST[27], 0),  # Refuse or, after a swap, Unknown
    (p.Rearm("rearm", CAST), "equipment_changed", CAST[27], 0),
    (p.Rearm("rearm", CAST), "realm_off", CAST[27], 0),
    (p.Rearm("rearm", CAST), "busy:combat", 999, 0),  # not a body of this rearm
    (p.Rearm("rearm", CAST), "busy:combat", CAST[27], 1),  # a count native never sends with a refusal
    (p.RealmMove("rearm", 2, "home"), "busy:combat", 2, 0),  # a pending walk's recheck is Unproved
    (p.LeavePvp("rearm", CAST), "busy:combat", CAST[27], 0),  # cleanup_mutated makes it Unknown
    (p.GuildGrow("rearm", 13, (303, 304)), "busy:combat", 304, 1),  # a grow retains past a join
    (p.Duel("rearm", 2, 3), "busy:combat", 2, 0),
])
def test_a_failed_job_native_may_still_hold_stays_unknown(store, cmd, reason, guid, value):
    c = classify(cmd)
    dispatch(store, prepare(store, cmd))
    store.observe_ack(Ack(0, "rearm", True, ""), now=2)
    t = store.observe_realm_job(job("rearm", c.kind, "failed", reason, guid, value), now=3)
    assert (t.result_ready, t.result_ok, t.reason) == (True, False, "job_failed")
    assert store.get("rearm").state == "unknown"
    assert store.holds()[0].guids == frozenset(c.reserved_manifest)


def test_a_step_refusal_without_its_ack_waits_and_a_negative_ack_stays_unknown(store):
    dispatch(store, prepare(store, p.Rearm("rearm", CAST)))
    assert not store.observe_realm_job(job("rearm", "rearm", "failed", "busy:combat", CAST[0]), now=2).result_ready
    assert store.get("rearm").state == "dispatched" and store.holds()
    store.recover(process_id="new", now=3)  # its ack never came: lost, so it may have run
    assert store.get("rearm").state == "unknown"
    dispatch(store, prepare(store, p.Rearm("other", (301,))))
    store.observe_ack(Ack(0, "other", False, "busy:combat"), now=3)
    store.observe_realm_job(job("other", "rearm", "failed", "busy:combat", 301), now=4)
    assert store.get("other").state == "unknown"
    assert {h.scope_id for h in store.holds()} == {"rearm", "other"}


def _stored_unknown(store, command_id, cmd, ev, *, reason="job_failed", seq_time=1793900000.0):
    """A row exactly as an earlier build left it: unknown, settled, its native ack and job kept."""
    c = classify(cmd)
    values = dict(command_id=command_id, parent_id=None, role="root", policy=c.policy, owner="realm_day", rank=4,
                  kind=c.kind, payload=c.payload, reserved_manifest=document(list(c.reserved_manifest)),
                  native_manifest=None, guild=21, human=1, state="unknown", transport_state="settled",
                  process_id="a80968d4", connection_epoch="1", send_error="", evidence=document(ev),
                  result_ok=0, result_reason=reason, created=seq_time, updated=seq_time + 0.1,
                  dispatched=seq_time + 0.01)
    with store.conn:
        store.conn.execute("INSERT INTO realm_action(" + ",".join(values) + ") VALUES("
                           + ",".join("?" for _ in values) + ")", tuple(values.values()))


def _failed(command_id, kind, reason, guid, value, at):
    return {"ack": {"error": "", "ok": True}, "failed_at": at,
            "job": {"guid": guid, "kind": kind, "pvp_owner": "", "reason": reason, "state": "failed", "value": value}}


def test_recovery_settles_stored_rearms_native_refused_at_a_step_and_keeps_a_real_unknown(store):
    raiders = tuple(range(401, 424))
    late = (501, 502, 503, 504, 505)
    _stored_unknown(store, "grow", p.GuildGrow("grow", 13, raiders),
                    _failed("grow", "guild_grow", "operation_transition_unknown", raiders[1], 1, 1793800000.1))
    _stored_unknown(store, "rearm", p.Rearm("rearm", CAST),
                    _failed("rearm", "rearm", "busy:combat", CAST[27], 0, 1793900000.1))
    _stored_unknown(store, "late", p.Rearm("late", late),
                    _failed("late", "rearm", "busy:combat", late[4], 0, 1793901000.1))
    _stored_unknown(store, "walk", p.RealmMove("walk", 601, "home"),
                    _failed("walk", "realm_move", "busy:combat", 601, 0, 1793902000.1))
    before = {r.command_id: r for r in store.records()}
    assert sum(len(h.guids) for h in store.holds()) == 23 + 106 + 5 + 1
    unresolved = store.recover(process_id="new", now=1794000000.0)
    assert [r.command_id for r in unresolved] == ["grow", "walk"]
    for command_id in ("rearm", "late"):
        record = store.get(command_id)
        assert (record.state, record.result_ok, record.result_reason) == ("completed", False, "step_refused")
        assert json.loads(record.evidence) == json.loads(before[command_id].evidence)  # the same receipts, read again
    assert {h.scope_id: h.guids for h in store.holds()} == {"grow": frozenset(raiders), "walk": frozenset({601})}
    assert store.get("grow") == before["grow"] and store.get("walk") == before["walk"]
    again = {r.command_id: r for r in store.records()}
    assert [r.command_id for r in store.recover(process_id="again", now=1794000100.0)] == ["grow", "walk"]
    assert {r.command_id: r for r in store.records()} == again


def test_recovery_leaves_other_stored_failures_alone(store):
    _stored_unknown(store, "lost", p.Rearm("lost", (301,)),
                    {"ack": {"error": "", "ok": True}, "failed_at": 5.0,
                     "job": {"guid": 301, "kind": "rearm", "pvp_owner": "", "reason": "busy:combat",
                             "state": "failed", "value": 0}})
    with store.conn:
        store.conn.execute("UPDATE realm_action SET send_error='TimeoutError' WHERE command_id='lost'")
    _stored_unknown(store, "transport", p.Rearm("transport", (302,)), {"failed_at": 5.0}, reason="transport_unknown")
    assert [r.command_id for r in store.recover(process_id="new", now=20)] == ["lost", "transport"]
    assert len(store.holds()) == 2


def test_step_refusals_are_native_refusals_whose_reservation_is_released():
    policy = (REPO / "modules/mod-leeroy/src/Lib/LeeroyRealmPolicy.cpp").read_text(encoding="utf-8")
    bodies = (REPO / "modules/mod-leeroy/src/Realm/RealmBodies.cpp").read_text(encoding="utf-8")
    module = (REPO / "modules/mod-leeroy/src/Realm/RealmModule.cpp").read_text(encoding="utf-8")

    def body(text, signature):
        start = text.index(signature)
        return text[start:text.index("\n}\n", start)]

    busy = {"busy:" + r for r in re.findall(r'return "([a-z_]+)";', body(policy, "std::string BusyReason("))}
    eligible = set(re.findall(r'return "([a-z_:]+)";', body(bodies, "std::string Eligible(")))
    owned = set(re.findall(r'return "([a-z_:]+)";', body(module, "std::string RealmModule::ValidateOwnedBody(")))
    assert action_store.STEP_REFUSALS == BODY_FENCES | busy | eligible | owned
    # Each such kind takes one step per body: Eligible's refusal is a Refuse before anything changes,
    # and it never goes pending, declares a scope, starts a continuation or reports one as Unknown.
    executors = {"rearm": "RealmStep Rearm(", "rpg_status": "RealmStep Rpg(", "equip": "RealmStep Equip(",
                 "set_profession": "RealmStep Profession("}
    assert set(executors) == action_store.STEP_REFUSAL_KINDS
    registry = body(bodies, "void RegisterRealmBodyHandlers(")
    for kind, signature in executors.items():
        step = body(bodies, signature)
        assert re.search(r"if \(auto why = Eligible\(entry, guid, Purpose::\w+(, true)?\); !why\.empty\(\)\) "
                         r"return Refuse\(why\);", step.split("\n")[2])
        assert step.count("Eligible(") == 1
        for forbidden in ("Pending", "Unproved", "Unknown(why", "Declare", "Charge", "continuation", "MutationReason"):
            assert forbidden not in step
        assert not set(re.findall(r'Unknown\("([a-z_:]+)"\)', step)) & action_store.STEP_REFUSALS
        handler = re.search(r'QueueHandler\("' + kind + r'",.*?\}, (\w+)\);\n', registry, re.S)
        assert handler and handler.group(1) == signature.split()[1].rstrip("(")  # the default admission only
    # Tick: a fence before the step and a Refused step both release every holder of the command,
    # unless its body is pending, it declared a scope, joined a guild or holds a continuation.
    tick = body(module, "void RealmModule::Tick(")
    assert "if (!reason.empty()) return finish(reason, pending);" in tick
    assert 'return finish(result.reason.empty() ? "refused" : result.reason, false, true);' in tick
    assert "if (retain) reservation.retained = true;\n            else _state->Release(entry.id);" in tick
    assert "if (reservation.guildJoinAttempts.empty()) return true;" in tick


def test_unsolicited_mismatched_kind_guid_and_token_are_not_enrolled(store):
    dispatch(store, prepare(store))
    for ev in (job("other"), job(kind="craft"), job(guid=3), job(token=TOKEN)):
        assert not store.observe_realm_job(ev, now=2).matched
    assert store.get("move").state == "dispatched"
    assert len(store.records()) == 1


def test_duel_is_ordinary_pair_and_requires_empty_token_and_generic_done(store):
    dispatch(store, prepare(store, p.Duel("duel", 3, 2)))
    store.observe_ack(Ack(0, "duel", True, ""), now=2)
    assert not store.observe_realm_job(job("duel", "duel", guid=0, token=TOKEN), now=3).matched
    assert store.holds()[0].guids == frozenset({2, 3})
    assert store.observe_realm_job(job("duel", "duel", guid=0), now=4).result_ok


@pytest.mark.parametrize("cmd", [p.GuildAdmit("direct", 2), p.GuildNote("direct", 2, "note"),
                                p.BankTabBuy("direct", 2, 0, "T", ""), p.GrantAchievement("direct", 2, 3117)])
def test_synchronous_ack_known_target_holds_through_settlement(store, cmd):
    dispatch(store, prepare(store, cmd))
    assert store.holds()[0].guids == frozenset({2})
    store.observe_ack(Ack(0, "direct", True, ""), now=2)
    assert store.holds() == ()


@pytest.mark.parametrize("reason", ["realm_off", "database_error"])
def test_direct_negative_ack_only_realm_off_proves_no_admission(store, reason):
    dispatch(store, prepare(store, p.GuildAdmit("direct", 2)))
    store.observe_ack(Ack(0, "direct", False, reason), now=2)
    assert bool(store.holds()) == (reason != "realm_off")


def test_no_body_operations_never_release_unrelated_hold(store):
    dispatch(store, prepare(store))
    dispatch(store, prepare(store, p.BankMoney("money", 1)))
    store.observe_ack(Ack(0, "money", True, ""), now=2)
    assert [h.scope_id for h in store.holds()] == ["move"]


@pytest.mark.parametrize("kind", ["item_count", "calendar_post"])
def test_direct_result_requires_positive_ack_correct_guid_and_calendar_event_id(store, kind):
    cmd = p.ItemCount("result", 2, (1,)) if kind == "item_count" else p.CalendarPost("result", 2, "T", "raid", 1)
    dispatch(store, prepare(store, cmd))
    assert not store.observe_realm_job(job("result", kind, guid=3, value=1), now=2).matched
    if kind == "calendar_post": assert not store.observe_realm_job(job("result", kind, value=0), now=2).matched
    assert not store.observe_realm_job(job("result", kind, value=1), now=3).result_ready
    assert store.observe_ack(Ack(0, "result", True, ""), now=4).result_ok


@pytest.mark.parametrize("guids,skipped,state,reason,terminal,ok", [
    ((2, 3), (), "done", "", True, True),
    ((2,), (JobSkip(3, "offline"),), "done", "", True, False),
    ((2,), (JobSkip(3, "offline"),), "failed", "failed", True, False),
    ((2,), (), "done", "", False, False),
    ((2, 3), (JobSkip(3, "offline"),), "failed", "failed", False, False),
    ((2, 4), (), "done", "", False, False),
])
def test_restricted_defaults_full_accounting_only_hands_back(store, guids, skipped, state, reason, terminal, ok):
    dispatch(store, prepare(store, BotDo("defaults", (2, 3), "nc", "-bg,-lfg")))
    store.observe_ack(Ack(0, "defaults", True, ""), now=2)
    t = store.observe_bot_job(bot(guids=guids, skipped=skipped, state=state, reason=reason), now=3)
    assert t.result_ok is ok
    assert (store.get("defaults").state == "completed") is terminal
    assert bool(store.holds()) is not terminal


def test_defaults_full_result_without_ack_stays_reserved(store):
    dispatch(store, prepare(store, BotDo("defaults", (2, 3), "nc", "-bg,-lfg")))
    assert not store.observe_bot_job(bot(), now=2).result_ready
    assert store.holds()
    assert store.observe_ack(Ack(0, "defaults", True, ""), now=3).result_ok


@pytest.mark.parametrize("reason", ["disabled", "busy", "busy:realm_owner", "not_cast:2"])
def test_restricted_defaults_refusal_allowlist(store, reason):
    dispatch(store, prepare(store, BotDo("defaults", (2, 3), "nc", "-bg,-lfg")))
    store.observe_ack(Ack(0, "defaults", False, reason), now=2)
    assert store.holds() == ()


def test_defaults_invalid_duplicate_accounting_rejects_without_overwriting_evidence(store):
    dispatch(store, prepare(store, BotDo("defaults", (2, 3), "nc", "-bg,-lfg")))
    with pytest.raises(ValueError): store.observe_bot_job(bot(guids=(2, 2)), now=2)
    assert store.get("defaults").evidence == "{}"


def test_wg_requested_superset_retained_and_status_waits_for_its_query_ack(store):
    wg(store)
    snapshot(store, ack=False)
    assert store.get("wg").native_manifest is None
    assert store.holds()[0].guids == frozenset({2, 3, 4})
    store.observe_ack(Ack(0, "query", True, ""), now=10)
    assert store.get("wg").native_manifest == (2, 3)
    assert store.holds()[0].guids == frozenset({2, 3, 4})
    record = cleanup(store)
    assert json.loads(record.payload)["guids"] == [2, 3]
    assert len(store.holds()) == 1
    with pytest.raises(ValueError): store.prepare_cleanup("wg", p.LeavePvp("split", (2,), TOKEN), now=12)


def test_wg_enrollment_then_same_origin_failure_is_progression(store):
    wg(store)
    store.observe_realm_job(job("wg", "wg_join", "failed", "ending", 0, 0, TOKEN), now=5)
    root = store.get("wg"); ev = json.loads(root.evidence)
    assert root.state == "unknown"
    assert "enrollment_job" in ev and "origin_failure" in ev
    snapshot(store, stage="ending", fenced=True)
    cleanup(store)
    assert store.get("wg").state == "unknown"
    assert store.holds()


@pytest.mark.parametrize("change", [{"native": (2,)}, {"origin_epoch": 1}, {"token": "c"*32 + "-" + "d"*16},
                                     {"guild": 51}, {"human": 2}])
def test_wg_binding_is_immutable_and_identity_changes_conflict(store, change):
    wg(store); snapshot(store)
    snapshot(store, command_id="query2", now=20, revision=2, **change)
    assert store.get("wg").state == "conflict"
    assert store.get("wg").native_manifest == (2, 3)
    assert store.holds()[0].guids == frozenset({2, 3, 4})


def test_wg_rejects_foreign_manifest_and_same_revision_disagreement(store):
    wg(store)
    snapshot(store, native=(2, 5))
    assert store.get("wg").state == "conflict"


def test_status_same_revision_disagreement_conflicts_but_older_progression_is_ignored(store):
    wg(store); snapshot(store, revision=3)
    snapshot(store, command_id="query2", now=20, revision=2, stage="queued")
    assert json.loads(store.get("wg").evidence)["status"]["revision"] == 3
    snapshot(store, command_id="query3", now=30, revision=3, stage="ending", fenced=True)
    assert store.get("wg").state == "conflict"
    assert store.holds()


def test_application_fence_never_reverses_and_unknown_is_absorbing(store):
    wg(store); snapshot(store, stage="unknown", fenced=True)
    snapshot(store, command_id="query2", now=20, revision=2, stage="active")
    assert store.get("wg").state == "conflict"
    assert store.holds()


def test_clean_cannot_override_witnessed_unknown_even_at_new_revision(store):
    wg(store); snapshot(store); cleanup(store)
    snapshot(store, command_id="query2", now=20, stage="unknown", fenced=True, revision=2)
    store.observe_send_error("cleanup", "TimeoutError", now=21)
    snapshot(store, command_id="query3", now=30, stage="clean", cleanup_id="cleanup", fenced=True, revision=3)
    assert store.get("wg").state == "conflict"
    assert store.holds()


@pytest.mark.parametrize("waiting", ["origin", "cleanup", "both"])
def test_clean_lost_ack_proof_waits_for_actual_transport_settlement(store, waiting):
    wg(store, settle=waiting not in {"origin", "both"}); snapshot(store); cleanup(store)
    if waiting not in {"cleanup", "both"}: store.observe_send_error("cleanup", "TimeoutError", now=12)
    snapshot(store, command_id="query2", now=20, stage="clean", cleanup_id="cleanup", fenced=True, revision=2)
    assert store.holds()
    if waiting in {"origin", "both"}: store.observe_send_error("wg", "TimeoutError", now=21)
    if waiting in {"cleanup", "both"}: store.observe_send_error("cleanup", "TimeoutError", now=22)
    assert store.get("wg").state == "completed"
    assert store.holds() == ()


def test_restart_can_settle_old_inflight_clean_proof_without_replaying_mutations(store):
    wg(store, settle=False); snapshot(store); cleanup(store)
    snapshot(store, command_id="query2", now=20, stage="clean", cleanup_id="cleanup", fenced=True, revision=2)
    assert store.holds()
    store.recover(process_id="new", now=30)
    assert store.get("wg").state == "completed"
    assert store.holds() == ()


def test_cleanup_ack_plus_exact_success_hands_back_full_requested_superset(store):
    wg(store); snapshot(store); cleanup(store)
    store.observe_ack(Ack(0, "cleanup", True, ""), now=12)
    assert not store.observe_realm_job(job("cleanup", "leave_pvp", guid=0, value=1, token=TOKEN), now=13).matched
    assert store.holds()
    t = store.observe_realm_job(job("cleanup", "leave_pvp", guid=0, value=2, token=TOKEN), now=14)
    assert t.command_id == "wg" and t.result_ok
    assert store.holds() == ()


@pytest.mark.parametrize("target", ["wg", "cleanup"])
def test_negative_ack_cannot_be_bypassed_by_clean_query(store, target):
    wg(store, settle=False); snapshot(store); cleanup(store)
    # Admit the read-only query before the negative origin ACK can fence the
    # root. Its later clean response must never override that retained conflict.
    query = store.prepare_query("wg", p.GetRealmPvp("query2", origin_id="wg"), now=20)
    dispatch(store, query, now=20)
    other = "cleanup" if target == "wg" else "wg"
    store.observe_send_error(other, "TimeoutError", now=21)
    store.observe_ack(Ack(0, target, False, "owner_unavailable"), now=22)
    store.observe_pvp(p.RealmPvp(0, "query2", TOKEN, "wg", "wintergrasp", 2**64-1, 2,
                               50, 1, (2, 3), "clean", True, "", "cleanup"), now=23)
    store.observe_ack(Ack(0, "query2", True, ""), now=24)
    assert store.get("wg").state == "conflict"
    assert store.holds()


def test_conflicted_origin_refuses_new_query_dispatch_without_weakening_hold(store):
    wg(store, settle=False); snapshot(store)
    store.observe_ack(Ack(0, "wg", False, "owner_unavailable"), now=11)
    assert store.get("wg").state == "conflict"
    query = store.prepare_query("wg", p.GetRealmPvp("query2", origin_id="wg"), now=20)
    with pytest.raises(ValueError, match="invalid query dispatch"):
        dispatch(store, query, now=20)
    assert store.get("wg").state == "conflict"
    assert store.holds()[0].guids == frozenset({2, 3, 4})


def test_cleanup_uncertainty_never_retries_without_newer_fenced_cancellation(store):
    wg(store); snapshot(store); cleanup(store)
    store.observe_send_error("cleanup", "TimeoutError", now=12)
    with pytest.raises(ValueError): store.prepare_cleanup("wg", p.LeavePvp("retry", (2, 3), TOKEN), now=13)
    assert store.holds()


def test_legal_native_cleaning_to_ending_cancels_controller_and_allows_one_replacement(store):
    wg(store); snapshot(store); cleanup(store)
    store.observe_ack(Ack(0, "cleanup", True, ""), now=12)
    snapshot(store, command_id="query2", now=20, stage="cleaning", cleanup_id="cleanup", fenced=True, revision=2)
    snapshot(store, command_id="query3", now=30, stage="ending", fenced=True, revision=3)
    root = store.get("wg"); ev = json.loads(root.evidence)
    assert root.state != "conflict"
    assert ev["cleanup_id"] == "" and ev["canceled"] == ["cleanup"]
    cleanup(store, "replacement", now=31)
    store.observe_ack(Ack(0, "replacement", True, ""), now=32)
    store.observe_realm_job(job("replacement", "leave_pvp", guid=0, value=2, token=TOKEN), now=33)
    assert store.holds() == ()


def test_old_refused_controller_does_not_poison_distinct_new_proved_controller(store):
    wg(store); snapshot(store); cleanup(store)
    store.observe_ack(Ack(0, "cleanup", False, "owner_unavailable"), now=12)
    snapshot(store, command_id="query2", now=20, stage="ending", fenced=True, revision=2)
    cleanup(store, "replacement", now=21)
    store.observe_send_error("replacement", "TimeoutError", now=22)
    snapshot(store, command_id="query3", now=30, stage="clean", cleanup_id="replacement", fenced=True, revision=3)
    assert store.get("wg").state == "completed"
    assert store.holds() == ()


def test_replacement_controller_history_has_hard_limit_without_eviction(store):
    wg(store); snapshot(store)
    for i in range(17):
        cleanup(store, f"cleanup{i}", now=11 + 20*i)
        store.observe_send_error(f"cleanup{i}", "TimeoutError", now=12 + 20*i)
        snapshot(store, command_id=f"query{i+2}", now=20+20*i, stage="ending", fenced=True, revision=i+2)
    with pytest.raises(ActionCapacity): cleanup(store, "overflow", now=400)
    assert len(json.loads(store.get("wg").evidence)["cleanup_ids"]) == 17
    assert store.holds()


def test_query_capacity_and_spacing_are_durable(store):
    wg(store)
    query = store.prepare_query("wg", p.GetRealmPvp("query", origin_id="wg"), now=10)
    dispatch(store, query, now=10)
    with pytest.raises(ValueError): store.prepare_query("wg", p.GetRealmPvp("query2", origin_id="wg"), now=20)
    store.observe_send_error("query", "TimeoutError", now=20)
    with pytest.raises(ValueError): store.prepare_query("wg", p.GetRealmPvp("query2", origin_id="wg"), now=19)
    store.prepare_query("wg", p.GetRealmPvp("query2", origin_id="wg"), now=20)
    assert store.get("query") is None
    assert len([r for r in store.records() if r.role == "query"]) == 1


def test_root_capacity_retains_all_unknown_ownership_across_recovery(store):
    for i in range(128): dispatch(store, prepare(store, p.Rearm(f"r{i}", (i+1,))))
    with pytest.raises(ActionCapacity): prepare(store, p.Rearm("overflow", (1000,)))
    store.recover(process_id="new", now=20)
    assert len(store.unresolved()) == len(store.holds()) == 128
    with pytest.raises(ActionCapacity): prepare(store, p.Rearm("overflow", (1000,)))
    assert store.prune(keep=0) == 0


def test_terminal_history_is_bounded_without_evicting_unknown(store):
    dispatch(store, prepare(store, p.Rearm("unknown", (2,))))
    for i in range(270):
        dispatch(store, prepare(store, p.BankMoney(f"money{i}", 1), now=i))
        store.observe_ack(Ack(0, f"money{i}", True, ""), now=i)
    store.prune()
    assert len(store.records()) == 257
    assert [h.scope_id for h in store.holds()] == ["unknown"]


@pytest.mark.parametrize("column,value", [("evidence", "{broken"), ("reserved_manifest", "[]"),
                                           ("payload", "{}"), ("state", "completed"),
                                           ("connection_epoch", "-1")])
def test_corrupt_recovery_and_holds_fail_closed(store, column, value):
    dispatch(store, prepare(store))
    with store.conn: store.conn.execute(f"UPDATE realm_action SET {column}=? WHERE command_id='move'", (value,))
    with pytest.raises(ActionUnavailable, match="evidence unavailable"): store.holds()
    with pytest.raises(ActionUnavailable): store.recover(process_id="new", now=20)


@pytest.mark.parametrize("error,expected", [("NotConnected", "not_sent"), ("RealmDisabled", "not_sent"),
                                           ("TimeoutError", "unknown"), ("OSError", "unknown")])
def test_only_explicit_prewrite_errors_prove_no_send(store, error, expected):
    dispatch(store, prepare(store))
    store.observe_send_error("move", error, now=2)
    assert store.get("move").state == expected
    assert bool(store.holds()) == (expected == "unknown")


def test_bound_sizes_and_unrelated_query_cannot_manufacture_scope(store):
    r = prepare(store)
    with pytest.raises(ValueError): store.prepare_query("move", p.GetRealmPvp("query", origin_id="move"), now=10)
    assert len(store.records()) == 1
    with pytest.raises(ValueError): store.prepare(replace(ActionSpec(r.command_id, None, "root", r.policy,
        r.owner, r.rank, r.kind, r.payload, r.reserved_manifest, r.guild, r.human), command_id="other", payload="x"*65537), now=1)
    assert len(store.records()) == 1


def test_ack_done_reducer_is_committed_before_transition_returns(store):
    dispatch(store, prepare(store))
    store.observe_realm_job(job(), now=2)
    transition = store.observe_ack(Ack(0, "move", True, ""), now=3)
    assert transition.result_ready
    # SQLite autocommit=False immediately begins a new transaction after commit;
    # rollback here must not undo the ownership decision that was returned.
    store.conn.rollback()
    assert store.get("move").state == "completed"
    assert store.holds() == ()


def test_refused_old_controller_conflicts_if_it_was_previously_proved_admitted(store):
    wg(store); snapshot(store); cleanup(store)
    snapshot(store, command_id="query2", now=20, stage="cleaning", cleanup_id="cleanup", fenced=True, revision=2)
    snapshot(store, command_id="query3", now=30, stage="ending", fenced=True, revision=3)
    cleanup(store, "replacement", now=31)
    store.observe_ack(Ack(0, "cleanup", False, "owner_unavailable"), now=32)
    assert store.get("wg").state == "conflict"
    assert store.holds()


def test_status_clean_for_unenrolled_controller_never_hands_back(store):
    wg(store); snapshot(store)
    snapshot(store, command_id="query2", now=20, stage="clean", cleanup_id="foreign", fenced=True, revision=2)
    assert store.get("wg").state != "completed"
    assert store.holds()


def test_false_query_ack_never_promotes_provisional_status(store):
    wg(store); snapshot(store, ack=False)
    store.observe_ack(Ack(0, "query", False, "realm_off"), now=11)
    assert store.get("wg").native_manifest is None
    assert store.holds()


def test_repeated_query_replacement_keeps_one_child_and_ignores_old_receipts(store):
    wg(store)
    first = None
    for i in range(40):
        ev = snapshot(store, command_id=f"q{i}", now=10*(i+1), revision=i+1)
        if first is None: first = ev
        assert len([r for r in store.records() if r.role == "query"]) == 1
        assert len(store.records()) == 2
        store.prune()
    assert not store.observe_pvp(first, now=500).matched
    assert not store.observe_ack(Ack(0, first.id, True, ""), now=500).matched
    assert store.get("wg").native_manifest == (2, 3)
    assert store.holds()


def test_restart_discards_prepared_cleanup_without_replaying_or_blocking_new_controller(store):
    wg(store); snapshot(store)
    old = store.prepare_cleanup("wg", p.LeavePvp("unsent", (2, 3), TOKEN), now=11)
    assert old.transport_state == "not_entered"
    store.recover(process_id="new", now=12)
    assert store.get("unsent").state == "not_sent"
    assert json.loads(store.get("wg").evidence)["cleanup_id"] == ""
    cleanup(store, "new_controller", now=13)
    assert store.holds()


def test_late_contradictory_cleanup_ack_reinstates_root_after_clean_release(store):
    wg(store); snapshot(store); cleanup(store)
    store.observe_ack(Ack(0, "cleanup", True, ""), now=12)
    store.observe_realm_job(job("cleanup", "leave_pvp", guid=0, value=2, token=TOKEN), now=13)
    assert store.holds() == ()
    store.observe_ack(Ack(0, "cleanup", False, "owner_unavailable"), now=14)
    assert store.get("wg").state == "conflict"
    assert store.holds()[0].guids == frozenset({2, 3, 4})


def test_delayed_query_uses_actual_dispatch_time_for_next_ten_second_spacing(store):
    wg(store)
    query = store.prepare_query("wg", p.GetRealmPvp("query", origin_id="wg"), now=10)
    dispatch(store, query, now=100)
    store.observe_send_error("query", "TimeoutError", now=101)
    with pytest.raises(ValueError): store.prepare_query("wg", p.GetRealmPvp("too_soon", origin_id="wg"), now=109)
    store.prepare_query("wg", p.GetRealmPvp("next", origin_id="wg"), now=110)


@pytest.mark.parametrize("reason", [*sorted(REFUSALS), "no_wintergrasp", "no_online_members"])
def test_wg_exact_fresh_no_admission_refusal_releases_without_cleanup(store, reason):
    dispatch(store, prepare(store, p.WgJoin("wg", (2, 3))))
    transition = store.observe_ack(Ack(0, "wg", False, reason), now=2)
    assert transition.result_ready and transition.result_ok is False
    assert store.get("wg").state == "completed"
    assert store.holds() == ()


def test_cleanup_ack_and_job_wait_for_origin_actual_settlement(store):
    wg(store, settle=False); snapshot(store); cleanup(store)
    store.observe_ack(Ack(0, "cleanup", True, ""), now=12)
    store.observe_realm_job(job("cleanup", "leave_pvp", guid=0, value=2, token=TOKEN), now=13)
    assert store.get("cleanup").state == "completed"
    assert store.get("wg").state != "completed"
    assert store.holds()
    store.observe_send_error("wg", "TimeoutError", now=14)
    assert store.get("wg").state == "completed"
    assert store.holds() == ()


def test_fresh_failure_backoffs_reserve_bounded_history_and_cannot_be_evicted(store):
    for i in range(256):
        command_id = f"failed{i}"
        dispatch(store, prepare(store, p.Rearm(command_id, (i+1,)), now=0))
        store.observe_ack(Ack(0, command_id, False, "realm_off"), now=1)
    with pytest.raises(ActionCapacity, match="backoff history"):
        prepare(store, p.Rearm("overflow", (1000,)), now=2)
    assert len(store.records()) == 256
    assert store.prune(keep=0, now=599) == 0
    assert store.prune(keep=0, now=601) == 256
    prepare(store, p.Rearm("after_expiry", (1000,)), now=601)


def test_local_backoff_cancellation_does_not_create_or_extend_native_failure_time(store):
    first = dispatch(store, prepare(store, p.Rearm("first", (2,))))
    store.observe_ack(Ack(0, first.command_id, False, "unsupported"), now=10)
    prepare(store, p.Rearm("victim", (2,)), now=11)
    transition = store.predispatch_failed("victim", reason="backoff", now=200)
    assert transition.result_ready and transition.reason == "backoff"
    assert json.loads(store.get("first").evidence)["failed_at"] == 10
    assert "failed_at" not in json.loads(store.get("victim").evidence)
    assert store.holds() == ()


@pytest.mark.parametrize("command_id,kind,state,scope", [
    ("foreign", "gear_up", "progress", ""),
    ("foreign", "gear_up", "done", ""),
    ("foreign", "offline_raid", "progress", "training-scope"),
    ("foreign", "offline_raid", "failed", "training-scope"),
    ("foreign", "benchmark", "progress", "training-scope"),
    ("foreign", "benchmark", "done", "training-scope"),
    ("foreign", "bot_do", "done", "training-scope"),
    ("foreign", "bot_do", "failed", "training-scope"),
    ("defaults", "gear_up", "progress", ""),
    ("defaults", "gear_up", "done", ""),
    ("defaults", "offline_raid", "progress", "training-scope"),
    ("defaults", "offline_raid", "failed", "training-scope"),
    ("defaults", "bot_do", "done", "training-scope"),
    ("defaults", "bot_do", "failed", "training-scope"),
    ("ordinary", "bot_do", "done", ""),
    ("ordinary", "bot_do", "progress", ""),
    ("ordinary", "bot_do", "done", "training-scope"),
    ("prepared", "bot_do", "done", ""),
    ("prepared", "bot_do", "progress", ""),
])
def test_shared_job_screens_unknown_policy_kind_and_scope_before_defaults_validation(store, command_id, kind, state, scope):
    dispatch(store, prepare(store, BotDo("defaults", (2, 3), "nc", "-bg,-lfg")))
    store.observe_ack(Ack(0, "defaults", True, ""), now=2)
    dispatch(store, prepare(store, p.RealmMove("ordinary", 4, "home")))
    prepare(store, BotDo("prepared", (5,), "nc", "-bg,-lfg"))
    records, holds = store.records(), store.holds()
    ev = replace(bot(command_id), kind=kind, state=state, training_run=scope,
                 stage="assembling" if kind == "offline_raid" and state == "progress" else "",
                 reason="cancelled" if state == "failed" else "")
    transition = store.observe_bot_job(ev, now=3)
    assert not transition.matched and not transition.changed and not transition.result_ready
    assert store.records() == records and store.holds() == holds
    assert store.get("defaults").state == "dispatched"


@pytest.mark.parametrize("change", [{"state": "progress"}, {"guids": (2, 2)}, {"ts": True},
                                     {"skipped": (JobSkip(3, "offline"), JobSkip(3, "offline"))}])
def test_matching_defaults_still_strictly_validates_evidence_after_correlation(store, change):
    dispatch(store, prepare(store, BotDo("defaults", (2, 3), "nc", "-bg,-lfg")))
    store.observe_ack(Ack(0, "defaults", True, ""), now=2)
    records, holds = store.records(), store.holds()
    with pytest.raises(ValueError):
        store.observe_bot_job(replace(bot(), **change), now=3)
    assert store.records() == records and store.holds() == holds


@pytest.mark.parametrize("state,transport,expected", [
    ("prepared", "not_entered", "not_sent"),
    ("dispatched", "inflight", "unknown"),
    ("unknown", "settled", "unknown"),
    ("completed", "settled", "completed"),
])
def test_exact_legacy_announce_history_recovers_without_proof_replay_or_body_hold(store, state, transport, expected):
    payload = legacy_announce(store, state=state, transport=transport)
    before = store.get("legacy")
    assert before.payload == payload and before.reserved_manifest == () and before.native_manifest is None
    assert store.records() == (before,) and store.holds() == ()
    with pytest.raises((TypeError, ValueError)):
        decode(payload)
    unresolved = store.recover(process_id="new_process", now=20)
    after = store.get("legacy")
    assert after.payload == payload and after.state == expected
    assert after.evidence == before.evidence and after.native_manifest is None
    assert after.process_id == before.process_id and after.connection_epoch == before.connection_epoch
    assert after.reserved_manifest == () and store.holds() == ()
    assert [r.command_id for r in unresolved] == (["legacy"] if expected == "unknown" else [])
    if transport == "inflight":
        assert after.transport_state == "settled" and after.send_error == "ProcessRestart"
        assert after.result_ok is before.result_ok is None
    elif state == "prepared":
        assert after.transport_state == "not_entered" and after.result_reason == "restart_not_sent"
        assert after.result_ok is False and after.dispatched is None
    else:
        assert after == before
    assert not {"kill_id", "native_epoch", "guild_id", "human_guid", "difficulty", "size", "heroic"} & json.loads(after.payload).keys()


def test_legacy_prepared_cannot_dispatch_or_be_submitted_as_new_journal_action(store):
    payload = legacy_announce(store)
    before = store.get("legacy")
    with pytest.raises((TypeError, ValueError)):
        store.begin_dispatch("legacy", payload=payload, process_id="new", connection_epoch=8, now=2)
    assert store.get("legacy") == before
    raw = {**json.loads(payload), "id": "new_legacy"}
    spec = ActionSpec("new_legacy", None, "root", "ack", "realm_day", 4, "announce", document(raw), (), 50, 1)
    with pytest.raises((TypeError, ValueError)):
        store.prepare(spec, now=2)
    assert store.get("new_legacy") is None and store.records() == (before,)


def test_legacy_unknown_and_completed_history_preserve_actual_ack_authority(store):
    legacy_announce(store, state="dispatched", transport="inflight")
    transition = store.observe_ack(Ack(0, "legacy", True, ""), now=2)
    assert transition.result_ready and transition.result_ok is True
    completed = store.get("legacy")
    assert completed.state == "completed" and completed.transport_state == "settled"
    assert json.loads(completed.evidence) == {"ack": {"ok": True, "error": ""}}
    assert store.holds() == ()
    store.observe_ack(Ack(0, "legacy", False, "owner_unavailable"), now=3)
    conflict = store.get("legacy")
    assert conflict.state == "conflict" and json.loads(conflict.evidence)["conflict"] is True
    store.recover(process_id="new", now=4)
    assert store.get("legacy").state == "conflict" and store.holds() == ()


def test_legacy_unknown_no_body_history_does_not_release_unrelated_body_authority(store):
    dispatch(store, prepare(store, p.Rearm("body", (2, 3))))
    legacy_announce(store, state="unknown", transport="settled", send_error="TimeoutError",
                    result_ok=0, result_reason="transport_unknown", evidence=document({"failed_at": 1.0}))
    before = store.get("legacy")
    store.recover(process_id="new", now=20)
    assert store.get("legacy") == before
    hold, = store.holds()
    assert hold.scope_id == "body" and hold.guids == frozenset({2, 3})
    assert {r.command_id for r in store.unresolved()} == {"body", "legacy"}


@pytest.mark.parametrize("changes", [
    {"kind": "realm_move"}, {"kind": "unknown_body"}, {"policy": "ordinary"},
    {"reserved_manifest": "[2]"}, {"reserved_manifest": "[true]"}, {"native_manifest": "[]"},
    {"role": "query"}, {"role": "cleanup"}, {"parent_id": "parent"},
    {"command_id": "different"}, {"guild": 0}, {"human": 0}, {"rank": 0},
    {"state": "completed"}, {"state": "unknown"}, {"transport_state": "inflight"},
    {"evidence": '{"ack":{"ok":true,"error":"unexpected"}}'},
    {"evidence": '{"ack":{"ok":1,"error":""}}'},
    {"evidence": '{"ack":{"ok":true,"error":""},"ack":{"ok":true,"error":""}}'},
    {"evidence": '{"unknown":true}'}, {"evidence": '{"conflict":false}'},
    {"evidence": '{"job":{"kind":"announce","state":"done","reason":"","guid":0,"value":0,"pvp_owner":""}}'},
    {"evidence": '{"cleanup_ids":[]}'}, {"evidence": '{"status":{}}'},
])
def test_legacy_exception_never_accepts_corrupt_or_body_bearing_record(store, changes):
    legacy_announce(store)
    with store.conn:
        store.conn.execute("UPDATE realm_action SET " + ",".join(key + "=?" for key in changes),
                           tuple(changes.values()))
    with pytest.raises(ActionUnavailable):
        store.records()
    with pytest.raises(ActionUnavailable):
        store.holds()
    with pytest.raises(ActionUnavailable):
        store.recover(process_id="new", now=20)


@pytest.mark.parametrize("field,value", [
    ("type", "realm_move"), ("id", "bad id"), ("map", -1), ("map", True),
    ("instance_id", 0), ("instance_id", 2**32), ("boss_id", 2**32),
    ("boss_name", ""), ("boss_name", "é" * 25), ("boss_name", "bad\0name"),
    ("attempts", True), ("attempts", 100), ("fight_s", -1), ("fight_s", 3601),
    ("first", "month"), ("kill_id", "a" * 32 + ":1"), ("unknown", 1),
])
def test_legacy_payload_requires_exact_validated_old_schema(store, field, value):
    payload = legacy_announce(store)
    with store.conn:
        store.conn.execute("UPDATE realm_action SET payload=?", (document({**json.loads(payload), field: value}),))
    with pytest.raises(ActionUnavailable):
        store.recover(process_id="new", now=20)


@pytest.mark.parametrize("corruption", ["missing_attempts", "missing_fight_s", "duplicate", "noncanonical", "malformed"])
def test_legacy_reader_rejects_missing_fields_duplicates_and_noncanonical_bytes(store, corruption):
    payload = legacy_announce(store)
    raw = json.loads(payload)
    if corruption.startswith("missing_"):
        del raw[corruption.removeprefix("missing_")]
        payload = document(raw)
    elif corruption == "duplicate":
        payload = payload[:-1] + ',"id":"legacy"}'
    elif corruption == "noncanonical":
        payload += " "
    else:
        payload = "{broken"
    with store.conn:
        store.conn.execute("UPDATE realm_action SET payload=?", (payload,))
    with pytest.raises(ActionUnavailable):
        store.holds()
    with pytest.raises(ActionUnavailable):
        store.recover(process_id="new", now=20)


def test_new_exact_announce_keeps_existing_ack_no_body_policy(store):
    cmd = p.Announce("announce", "a" * 32 + ":1", 7, 50, 1, 631, 123, 0, 1, 25, False,
                     "Example", first="week")
    record = dispatch(store, prepare(store, cmd))
    assert record.policy == "ack" and record.reserved_manifest == () and store.holds() == ()
    assert not store.observe_realm_job(job(cmd.id, "announce", guid=0), now=2).matched
    assert store.observe_ack(Ack(0, cmd.id, True, ""), now=3).result_ok is True
    assert store.get(cmd.id).state == "completed" and store.holds() == ()
