"""Real queue/Recall seams for lost acknowledgements and native process changes."""
import asyncio
from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from conftest import run
from leeroy_mind.guild.protocol import BotDo, Job, JobSkip
from leeroy_mind.health import _realm_holds, FAIL, OK
from leeroy_mind.protocol import Ack, Hello
from leeroy_mind.realm import protocol as p
from leeroy_mind.realm.action_store import ActionUnavailable
from leeroy_mind.realm.recall import Recall
from realm.test_realm_action_store import prepare, dispatch, snapshot, cleanup, TOKEN
from realm.test_realm_actions import Harness
from realm.test_realm_recall import pipeline
from realm.test_unknown_roots import ledger, NOW, START, NEXT_START


def native_dispatch(ledger, cmd, *, now=NOW, started=START):
    row = prepare(ledger, cmd, now=now)
    return ledger.begin_dispatch(cmd.id, payload=row.payload, process_id='process',
                                 connection_epoch=1, now=now, started_ms=started)


def terminal(cmd, *, skipped=(), refused=False):
    if isinstance(cmd, BotDo):
        return Job(0, cmd.id, 'bot_do', 'done', '', '', tuple(g for g in cmd.guids if g not in skipped),
                   tuple(JobSkip(g, 'busy') for g in skipped), 0, 0, 0, 0, 0, 0)
    return p.RealmJob(0, cmd.id, 'rearm' if isinstance(cmd, p.Rearm) else 'leave_pvp',
                      'failed' if refused else 'done', 'busy:combat' if refused else '',
                      cmd.guids[0] if refused or len(cmd.guids) == 1 else 0, 0,
                      skipped=skipped, changed=False if refused else None)


@pytest.mark.parametrize('first', ['job', 'timeout'])
@pytest.mark.parametrize('kind', ['rearm', 'leave', 'defaults'])
@pytest.mark.parametrize('outcome', ['done', 'skipped'])
def test_recall_terminal_callbacks_and_later_batches_match_in_both_orders(ledger, first, kind, outcome):
    async def scenario():
        h = Harness(ledger)
        h.clock.value = NOW
        h.queue.on_hello(Hello(0, 'fixture', '1', 50, 1, started_ms=START))
        members = tuple(range(101, 144))  # enough synthetic members for two defaults batches
        ports = SimpleNamespace(cast=lambda: [SimpleNamespace(guid=g, online=True) for g in members],
                                human_online=lambda: True)
        recall = Recall(ports, h.queue, h.bodies, h.clock)
        callbacks, notices = [], []
        original_done = recall._done

        def observe(chain, step, ok, reason, past, command_id=None):
            (notices if reason == 'transport_unknown' else callbacks).append((step, ok, reason))
            original_done(chain, step, ok, reason, past, command_id)

        recall._done = observe
        if kind == 'leave': recall.on_login(NOW)
        else: recall.restore_defaults(members)
        index, selected = 0, None
        try:
            for tick in range(10):
                await h.cycle(NOW + tick * 2)
                if index == len(h.sent): break
                cmd = h.sent[index][1]
                index += 1
                step = 'defaults' if isinstance(cmd, BotDo) else 'rearm' if isinstance(cmd, p.Rearm) else 'leave'
                skipped = (102,) if step == kind and selected is None and outcome == 'skipped' else ()
                event = terminal(cmd, skipped=skipped)
                deliver = h.queue.on_job if isinstance(cmd, BotDo) else h.queue.on_realm_job
                if step == kind and selected is None:
                    selected = cmd
                    if first == 'job': deliver(event)
                    h.waits[cmd.id].set_exception(TimeoutError())
                    await h.cycle()
                    if first == 'timeout':
                        assert cmd.id in h.queue._entries
                        chain, = recall._chains.values()
                        assert chain.waiting and chain.awaiting_terminal
                        recall.tick(h.clock.now())
                        h.bodies.poll()
                        recall.tick(h.clock.now())
                        assert recall._chains.get(chain.key) is chain
                        assert len(h.sent) == index
                        deliver(event)
                    # Duplicate terminal receipts must not notify or enqueue again.
                    delivered = len(callbacks)
                    deliver(event)
                    assert len(callbacks) == delivered
                else:
                    h.ack(cmd.id)
                    await h.cycle()
                    deliver(event)
            remaining = tuple(g for g in members if not (kind == 'leave' and outcome == 'skipped' and g == 102))
            expected = [(p.LeavePvp, members)] if kind == 'leave' else []
            expected += [(p.Rearm, remaining)]
            expected += [(BotDo, tuple(g for g in members[i:i + 40] if g in remaining))
                         for i in range(0, len(members), 40)]
            assert [(type(cmd), cmd.guids) for _, cmd in h.sent] == expected
            expected_callbacks = [('leave', True, '')] if kind == 'leave' else []
            expected_callbacks += [('rearm', True, ''),
                ('defaults', not (kind == 'defaults' and outcome == 'skipped'),
                 'defaults_failed' if kind == 'defaults' and outcome == 'skipped' else ''),
                ('defaults', True, '')]
            assert callbacks == expected_callbacks
            assert notices == ([(kind, False, 'transport_unknown')] if first == 'timeout' else [])
            assert not recall._chains and not h.queue._entries and not ledger.holds()
        finally:
            await h.finish()
    run(scenario())


@pytest.mark.parametrize('first', ['job', 'timeout'])
def test_rearm_refusal_after_lost_ack_still_sends_defaults(ledger, first):
    async def scenario():
        h = Harness(ledger)
        ports = SimpleNamespace(cast=lambda: [SimpleNamespace(guid=101, online=True)])
        recall = Recall(ports, h.queue, h.bodies, h.clock)
        recall.restore_defaults((101,))
        await h.cycle()
        cmd = h.sent[0][1]
        event = terminal(cmd, refused=True)
        if first == 'job': h.queue.on_realm_job(event)
        h.waits[cmd.id].set_exception(TimeoutError())
        await h.cycle()
        if first == 'timeout': h.queue.on_realm_job(event)
        await h.cycle(2)
        assert isinstance(h.sent[1][1], BotDo)
        await h.finish()
    run(scenario())


def test_transport_unknown_chain_waits_until_ticket_change_without_replaying(ledger):
    async def scenario():
        h = Harness(ledger)
        ports = SimpleNamespace(cast=lambda: [SimpleNamespace(guid=101, online=True)], human_online=lambda: True)
        recall = Recall(ports, h.queue, h.bodies, h.clock)
        recall.restore_defaults((101,))
        await h.cycle()
        cmd = h.sent[0][1]
        h.waits[cmd.id].set_exception(TimeoutError())
        await h.cycle()
        for now in (1, 60, 3600):
            h.clock.value = now
            recall.tick(now)
            await h.cycle()
            assert recall._chains and len(h.sent) == 1
        recall.invalidate()
        h.current = replace(h.current, generation=2)
        h.queue.context_changed()
        assert not recall._chains and not h.queue._entries
        assert ledger.holds()[0].guids == frozenset({101})
        await h.finish()
    run(scenario())


@pytest.mark.parametrize('kind', ['rearm', 'leave_pvp', 'bot_do'])
def test_acked_dispatched_root_is_released_once_by_later_native_process(ledger, kind):
    cmd = {'rearm': p.Rearm('root', (101, 102)), 'leave_pvp': p.LeavePvp('root', (101, 102)),
           'bot_do': BotDo('root', (101, 102), 'nc', '-bg,-lfg')}[kind]
    native_dispatch(ledger, cmd)
    ledger.observe_ack(Ack(0, cmd.id, True, ''), now=NOW + 1)
    before = ledger.get(cmd.id)
    assert before.state == 'dispatched'
    with pytest.raises(ValueError, match='this one is dispatched'):
        ledger.abandon(cmd.id, reason='fixture', now=NOW + 2)
    check = _realm_holds(ledger.conn, NOW + 4000)
    assert check.status == FAIL and 'a worldserver restart frees it' in check.text
    assert f'realm release {cmd.id}' not in check.text
    assert not ledger.restart_release(started_ms=START, guild=50, human=1, now=NOW + 2)
    ledger.recover(process_id='recovered', now=NOW + 3)
    assert ledger.restart_release(started_ms=NEXT_START, guild=50, human=1, now=NOW + 20) == ('root',)
    assert not ledger.restart_release(started_ms=NEXT_START, guild=50, human=1, now=NOW + 21)
    assert not ledger.holds() and _realm_holds(ledger.conn, NOW + 4000).status == OK
    receipt = json.loads(ledger.conn.execute("SELECT value FROM meta WHERE key='realm:action:restart_released:root'").fetchone()[0])
    assert receipt['before']['state'] == 'dispatched'
    assert receipt['before']['evidence'] == before.evidence
    assert ledger.prune(keep=0, now=NOW + 100000) == 0


@pytest.mark.parametrize('acked', [False, True])
def test_backward_wall_clock_never_makes_same_process_a_restart(ledger, acked):
    native_dispatch(ledger, p.Rearm('root', (101,)), now=NOW - 200)
    if acked: ledger.observe_ack(Ack(0, 'root', True, ''), now=NOW - 199)
    else: ledger.observe_send_error('root', 'AckTimeout', now=NOW - 199)
    before = ledger.get('root')
    assert START > before.dispatched * 1000  # old clock-only proof would release this
    assert not ledger.restart_release(started_ms=START, guild=50, human=1, now=NOW)
    assert not ledger.restart_release(started_ms=START - 1, guild=50, human=1, now=NOW)
    assert ledger.get('root') == before and ledger.holds()
    assert ledger.restart_release(started_ms=NEXT_START, guild=50, human=1, now=NOW + 20) == ('root',)


@pytest.mark.parametrize('acked', [False, True])
def test_missing_dispatch_identity_stays_held(ledger, acked):
    native_dispatch(ledger, p.Rearm('root', (101,)), started=0)
    if acked: ledger.observe_ack(Ack(0, 'root', True, ''), now=NOW + 1)
    else: ledger.observe_send_error('root', 'AckTimeout', now=NOW + 1)
    assert not ledger.restart_release(started_ms=NEXT_START, guild=50, human=1, now=NOW + 20)
    assert ledger.holds()
    if acked:
        assert 'identity unavailable' in _realm_holds(ledger.conn, NOW + 4000).text


def test_pre_identity_restart_receipt_stays_readable_and_durable(ledger):
    native_dispatch(ledger, p.Rearm('root', (101,)))
    ledger.observe_send_error('root', 'AckTimeout', now=NOW + 1)
    ledger.restart_release(started_ms=NEXT_START, guild=50, human=1, now=NOW + 20)
    evidence = json.loads(ledger.get('root').evidence)
    del evidence['dispatch_started_ms']
    # Exact old evidence shape; no migration should be needed to keep it readable.
    from leeroy_mind.realm.action_policy import document
    with ledger.conn:
        ledger.conn.execute("UPDATE realm_action SET evidence=? WHERE command_id='root'", (document(evidence),))
    ledger.recover(process_id='new', now=NOW + 30)
    assert ledger.get('root').result_reason == 'restart_released' and not ledger.holds()
    assert ledger.prune(keep=0, now=NOW + 100000) == 0


@pytest.mark.parametrize('started', [True, -1, 2**63, 'invalid'])
def test_malformed_dispatch_identity_is_refused_before_transport(ledger, started):
    with pytest.raises(ValueError):
        native_dispatch(ledger, p.Rearm('root', (101,)), started=started)
    assert ledger.get('root').state == 'prepared' and not ledger.holds()


@pytest.mark.parametrize('change', ['connection', 'guild', 'human', 'malformed_hello'])
def test_dispatch_never_inherits_an_unrelated_or_malformed_hello(ledger, change):
    async def scenario():
        h = Harness(ledger)
        h.queue.on_hello(Hello(0, 'fixture', '1', 50, 1, started_ms=START))
        if change == 'connection': h.current = replace(h.current, connection_epoch=2)
        elif change == 'guild': h.current = replace(h.current, guild=51)
        elif change == 'human': h.current = replace(h.current, human=2)
        else: h.queue.on_hello(Hello(0, 'fixture', '1', 50, 1, started_ms=-1))
        h.queue.submit(p.Rearm('root', (101,)), owner='realm_day')
        await h.cycle()
        assert 'dispatch_started_ms' not in json.loads(ledger.get('root').evidence)
        await h.finish()
    run(scenario())


@pytest.mark.parametrize('settlement', ['ack', 'timeout'])
def test_new_hello_before_transport_settlement_is_rechecked_without_another_hello(ledger, settlement):
    async def scenario():
        h = Harness(ledger)
        h.clock.value = NOW
        h.queue.on_hello(Hello(0, 'fixture', '1', 50, 1, started_ms=START))
        cmd = p.Rearm('root', (101, 102))
        h.queue.submit(cmd, owner='realm_day')
        await h.cycle()
        assert json.loads(ledger.get(cmd.id).evidence)['dispatch_started_ms'] == START
        h.current = replace(h.current, connection_epoch=2)
        h.queue.on_hello(Hello(0, 'fixture', '1', 50, 1, started_ms=NEXT_START))
        assert ledger.get(cmd.id).transport_state == 'inflight' and ledger.holds()
        if settlement == 'ack': h.ack(cmd.id)
        else: h.waits[cmd.id].set_exception(TimeoutError())
        await h.cycle()
        assert ledger.get(cmd.id).result_reason == 'restart_released'
        assert not ledger.holds() and len(h.sent) == 1
        await h.finish()
    run(scenario())


@pytest.mark.parametrize('first', ['job', 'timeout'])
def test_lost_cleanup_ack_and_done_job_complete_wg_root_in_both_orders(ledger, first):
    dispatch(ledger, prepare(ledger, p.WgJoin('wg', (101, 102, 103))))
    ledger.observe_ack(Ack(0, 'wg', True, ''), now=2)
    ledger.observe_realm_job(p.RealmJob(0, 'wg', 'wg_join', 'done', '', 0, 2, TOKEN), now=3)
    snapshot(ledger, native=(101, 102))
    cleanup(ledger)
    event = p.RealmJob(0, 'cleanup', 'leave_pvp', 'done', '', 0, 2, TOKEN)
    handlers = [lambda: ledger.observe_realm_job(event, now=12),
                lambda: ledger.observe_send_error('cleanup', 'AckTimeout', now=13)]
    if first == 'timeout': handlers.reverse()
    assert not handlers[0]().result_ready and ledger.holds()
    result = handlers[1]()
    assert result.command_id == 'wg' and result.result_ready and result.result_ok
    assert ledger.get('cleanup').state == ledger.get('wg').state == 'completed'
    assert not ledger.holds()
    assert not ledger.observe_realm_job(event, now=14).result_ready


@pytest.mark.parametrize('acked', [False, True])
def test_service_adopts_release_before_recall_and_starts_freed_members(pipeline, monkeypatch, acked):
    async def scenario():
        async with pipeline as h:
            started = int((h.clock.now() - 100) * 1000)
            await h.app.event(Hello(0, 'fixture', '1', 21, 0, started_ms=started))
            h.cast(101, 102)
            cmd = p.Rearm('interrupted', (101, 102))
            h.realm.queue.submit(cmd, owner='realm_day')
            await h.cycle()
            if not acked: h.realm.queue.on_send_error(cmd, 'AckTimeout')
            await h.clock.advance(4000, step=4000)
            check = _realm_holds(h.app.store.conn, h.clock.now())
            assert check.status == FAIL and 'members=2' in check.text
            if not acked: assert 'realm release interrupted' in check.text
            seen = []
            original = h.realm.recall.on_hello

            def recall_after_release(ev):
                assert h.realm.action_store.get(cmd.id).result_reason == 'restart_released'
                assert not h.realm.action_store.holds()
                seen.append(ev.started_ms)
                original(ev)

            monkeypatch.setattr(h.realm.recall, 'on_hello', recall_after_release)
            await h.app.event(Hello(0, 'fixture', '1', 21, 1, started_ms=started + 200000))
            assert seen == [started + 200000]
            await h.cycle()
            assert isinstance(h.app.sent[-1], p.LeavePvp) and h.app.sent[-1].guids == (101, 102)
            assert _realm_holds(h.app.store.conn, h.clock.now()).status == OK
    run(scenario())


@pytest.mark.parametrize('failure', [ActionUnavailable('fixture'), ValueError('fixture')])
def test_restart_release_failure_keeps_service_hello_and_recall_running(pipeline, monkeypatch, caplog, failure):
    async def scenario():
        async with pipeline as h:
            h.cast(101)
            def fail(**kwargs): raise failure
            monkeypatch.setattr(h.realm.action_store, 'restart_release', fail)
            await h.app.event(Hello(0, 'fixture', '1', 21, 1, started_ms=START))
            await h.cycle()
            assert isinstance(h.app.sent[-1], p.LeavePvp)
            assert f'realm_action_restart_release_failed: {type(failure).__name__}' in caplog.text
    run(scenario())
