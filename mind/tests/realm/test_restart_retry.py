"""Restart reconciliation retries need the same process proof as the first attempt."""
from dataclasses import replace
import json
import sqlite3

import pytest

from conftest import run
from leeroy_mind.guild.protocol import BotDo
from leeroy_mind.protocol import Hello
from leeroy_mind.realm import protocol as p
from leeroy_mind.realm.action_policy import classify
from leeroy_mind.realm.action_store import ActionSpec, ActionUnavailable
from realm.test_realm_actions import Harness
from realm.test_realm_recall import pipeline
from realm.test_unknown_root_followups import native_dispatch
from realm.test_unknown_roots import ledger, NOW, START, NEXT_START


def interrupted_defaults(ledger, ident="defaults", members=(101, 102)):
    command = BotDo(ident, members, "nc", "-bg,-lfg")
    native_dispatch(ledger, command)
    ledger.observe_send_error(ident, "AckTimeout", now=NOW + 1)
    return command


@pytest.mark.parametrize("failure", [sqlite3.OperationalError, ActionUnavailable])
def test_failed_restart_retries_after_five_seconds_and_stops_after_success(ledger, monkeypatch, failure):
    interrupted_defaults(ledger)
    h = Harness(ledger)
    h.clock.value = NOW + 20
    original = ledger.restart_release
    calls = []
    def release(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise failure("synthetic")
        return original(**kwargs)
    monkeypatch.setattr(ledger, "restart_release", release)
    h.queue.on_hello(Hello(0, "fixture", "1", 50, 1, started_ms=NEXT_START))
    assert ledger.holds()
    for _ in range(100):
        h.queue.tick()
    h.clock.value += 4.99
    h.queue.tick()
    assert len(calls) == 1 and ledger.holds()
    h.clock.value += 0.01
    h.queue.tick()
    assert len(calls) == 2 and not ledger.holds()
    assert ledger.get("defaults").result_reason == "restart_released"
    h.clock.value += 600
    for _ in range(100):
        h.queue.tick()
    assert len(calls) == 2
    assert not h.sent  # recovery is a release receipt, never a resend


def test_persistent_failure_is_rate_limited_and_retains_the_hold(ledger, monkeypatch, caplog):
    interrupted_defaults(ledger)
    h = Harness(ledger)
    h.clock.value = NOW + 20
    calls = []
    def fail(**kwargs):
        calls.append(kwargs)
        raise ActionUnavailable("synthetic")
    monkeypatch.setattr(ledger, "restart_release", fail)
    h.queue.on_hello(Hello(0, "fixture", "1", 50, 1, started_ms=NEXT_START))
    for offset in range(101):
        h.clock.value = NOW + 20 + offset / 10
        h.queue.tick()
    assert len(calls) == 3 and ledger.holds()
    assert caplog.text.count("realm_action_restart_release_failed: ActionUnavailable") == 3


@pytest.mark.parametrize("case", ["same_process", "missing_identity", "day_fishing"])
def test_retry_does_not_expand_native_restart_admission(ledger, monkeypatch, case):
    command = (BotDo("defaults", (101,), "do", "go fishing", day_permit="a" * 32)
               if case == "day_fishing" else BotDo("defaults", (101,), "nc", "-bg,-lfg"))
    native_dispatch(ledger, command, started=0 if case == "missing_identity" else START)
    ledger.observe_send_error(command.id, "AckTimeout", now=NOW + 1)
    h = Harness(ledger)
    h.clock.value = NOW + 20
    original = ledger.restart_release
    def fail(**kwargs): raise ActionUnavailable("synthetic")
    monkeypatch.setattr(ledger, "restart_release", fail)
    h.queue.on_hello(Hello(0, "fixture", "1", 50, 1,
                          started_ms=START if case == "same_process" else NEXT_START))
    monkeypatch.setattr(ledger, "restart_release", original)
    h.clock.value += 5
    h.queue.tick()
    assert ledger.holds() and ledger.get(command.id).state == "unknown"
    assert h.queue._restart_retry_at is None  # reconciliation succeeded; evidence is insufficient


@pytest.mark.parametrize("change", ["disabled", "disconnected", "connection", "guild", "human", "malformed_hello"])
def test_retry_never_reuses_proof_across_invalid_context(ledger, monkeypatch, change):
    interrupted_defaults(ledger)
    h = Harness(ledger)
    h.clock.value = NOW + 20
    original = ledger.restart_release
    def fail(**kwargs):
        raise ActionUnavailable("synthetic")
    monkeypatch.setattr(ledger, "restart_release", fail)
    h.queue.on_hello(Hello(0, "fixture", "1", 50, 1, started_ms=NEXT_START))
    monkeypatch.setattr(ledger, "restart_release", original)
    if change == "disabled": h.current = replace(h.current, enabled=False)
    elif change == "disconnected": h.current = replace(h.current, connected=False, presence_known=False)
    elif change == "connection": h.current = replace(h.current, connection_epoch=2)
    elif change == "guild": h.current = replace(h.current, guild=51)
    elif change == "human": h.current = replace(h.current, human=2)
    else: h.queue.on_hello(Hello(0, "fixture", "1", 50, 1, started_ms=-1))
    h.clock.value += 10
    h.queue.tick()
    assert ledger.holds() and ledger.get("defaults").state == "unknown"


def test_two_forty_member_defaults_batches_release_on_adopted_hello_after_atomic_retry(pipeline, monkeypatch):
    async def scenario():
        async with pipeline as h:
            store = h.realm.action_store
            started = int((h.clock.now() - 100) * 1000)
            commands = [BotDo(f"batch{i}", tuple(range(101 + i*40, 141 + i*40)), "nc", "-bg,-lfg")
                        for i in range(2)]
            for command in commands:
                c = classify(command)
                row = store.prepare(ActionSpec(command.id, None, "root", c.policy, "realm_day", 4,
                    c.kind, c.payload, c.reserved_manifest, 21, 1), now=h.clock.now())
                store.begin_dispatch(command.id, payload=row.payload, process_id="old", connection_epoch=1,
                                     now=h.clock.now(), started_ms=started)
                store.observe_send_error(command.id, "AckTimeout", now=h.clock.now())
            before = tuple(store.get(command.id) for command in commands)
            # First hello uses the same native process: a mind reconnect is not proof.
            await h.app.event(Hello(0, "fixture", "1", 21, 0, started_ms=started))
            assert sum(len(hold.guids) for hold in store.holds()) == 80
            original = store._save
            saved = []
            def save(row):
                if row.result_reason == "restart_released":
                    saved.append(row.command_id)
                    if len(saved) == 2:
                        raise sqlite3.OperationalError("synthetic transaction failure")
                return original(row)
            monkeypatch.setattr(store, "_save", save)
            await h.app.event(Hello(0, "fixture", "1", 21, 0, started_ms=started + 200000))
            assert tuple(store.get(command.id) for command in commands) == before
            assert not store.conn.execute("SELECT 1 FROM meta WHERE key LIKE 'realm:action:restart_released:%'").fetchall()
            await h.cycle(5)
            assert not store.holds()
            for command in commands:
                row = store.get(command.id)
                assert (row.state, row.result_ok, row.result_reason) == ("completed", False, "restart_released")
                assert json.loads(row.evidence)["restart_started_ms"] == started + 200000
            assert len(store.conn.execute("SELECT 1 FROM meta WHERE key LIKE 'realm:action:restart_released:%'").fetchall()) == 2
            assert not h.app.sent
    run(scenario())
