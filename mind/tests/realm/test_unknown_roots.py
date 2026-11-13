"""Synthetic copies of the lost-ACK and process-restart evidence shapes."""
from dataclasses import asdict
import json
import logging

import pytest

from leeroy_mind.guild.protocol import BotDo, JobSkip
from leeroy_mind.protocol import Ack, Hello
from leeroy_mind.realm import protocol as p
from leeroy_mind.realm.action_store import ActionStore, skipped_backoff
from realm.test_realm_action_store import prepare, dispatch, bot
from realm.test_realm_actions import Harness


@pytest.fixture
def ledger(store):
    return ActionStore(store.conn)


def defaults(ledger):
    return dispatch(ledger, prepare(ledger, BotDo('defaults', (101, 102), 'nc', '-bg,-lfg')))


@pytest.mark.parametrize('first', ['job', 'timeout'])
@pytest.mark.parametrize('skips', [False, True])
def test_defaults_terminal_outranks_lost_ack_in_both_orders(ledger, first, skips):
    defaults(ledger)
    ev = bot(guids=(101,) if skips else (101, 102), skipped=(JobSkip(102, 'busy'),) if skips else ())
    handlers = [lambda: ledger.observe_bot_job(ev, now=3),
                lambda: ledger.observe_send_error('defaults', 'AckTimeout', now=4)]
    if first == 'timeout': handlers.reverse()
    for handler in handlers: handler()
    row = ledger.get('defaults')
    assert (row.state, row.result_ok, row.result_reason) == ('completed', not skips, 'defaults_failed' if skips else '')
    assert not ledger.holds()
    h = Harness(ledger)
    assert h.queue._backed_off('realm_day', 'bot_do', (102,), 10) is skips
    assert not h.queue._backed_off('realm_day', 'bot_do', (102,), 605)
    ledger.recover(process_id='again', now=5)
    assert ledger.get('defaults').state == 'completed'


@pytest.mark.parametrize('first', ['job', 'timeout'])
@pytest.mark.parametrize('kind', ['rearm', 'leave_pvp'])
@pytest.mark.parametrize('outcome', ['done', 'step_refused', 'unproved_failure'])
def test_realm_terminal_settles_by_its_evidence_not_the_transport(ledger, first, kind, outcome):
    cmd = (p.Rearm if kind == 'rearm' else p.LeavePvp)('root', (101, 102))
    dispatch(ledger, prepare(ledger, cmd))
    ev = p.RealmJob(0, 'root', kind, 'done' if outcome == 'done' else 'failed',
        '' if outcome == 'done' else 'busy:combat', 0 if outcome == 'done' else 102, 0,
        changed=False if outcome == 'step_refused' else True if outcome == 'unproved_failure' else None,
        skipped=(102,) if outcome == 'done' else ())
    handlers = [lambda: ledger.observe_realm_job(ev, now=3),
                lambda: ledger.observe_send_error('root', 'AckTimeout', now=4)]
    if first == 'timeout': handlers.reverse()
    for handler in handlers: handler()
    row = ledger.get('root')
    assert row.result_reason == {'done':'', 'step_refused':'step_refused', 'unproved_failure':'job_failed'}[outcome]
    assert row.state == ('unknown' if outcome == 'unproved_failure' else 'completed')
    if outcome == 'done' and kind == 'leave_pvp':
        assert skipped_backoff(row, 10) == (102,)
        assert skipped_backoff(row, 605) == ()


@pytest.mark.parametrize('already_unknown', [False, True])
def test_mind_recovery_reduces_stored_terminal_even_without_an_ack(ledger, already_unknown):
    defaults(ledger)
    ledger.observe_bot_job(bot(guids=(101,102)), now=3)
    # Stored shape left by the previous build, without consulting any live data.
    if already_unknown:
        with ledger.conn:
            ledger.conn.execute("UPDATE realm_action SET state='unknown',transport_state='settled',"
                "result_ok=0,result_reason='transport_unknown',send_error='AckTimeout' WHERE command_id='defaults'")
    ledger.recover(process_id='again', now=5)
    assert ledger.get('defaults').state == 'completed'
    assert not ledger.holds()


def unknown(ledger, cmd=None):
    cmd = cmd or p.Rearm('root', (101,102,103,104,105))
    dispatch(ledger, prepare(ledger, cmd), now=10)
    ledger.observe_send_error(cmd.id, 'ConnectionError', now=11)
    return ledger.get(cmd.id)


@pytest.mark.parametrize('kind', ['rearm','leave_pvp','bot_do'])
def test_later_native_start_releases_once_keeps_prior_row_and_never_prunes(ledger, kind, caplog):
    cmd = {'rearm':p.Rearm('root',(101,102)), 'leave_pvp':p.LeavePvp('root',(101,102)),
           'bot_do':BotDo('root',(101,102),'nc','-bg,-lfg')}[kind]
    before = unknown(ledger,cmd)
    assert not ledger.restart_release(started_ms=10000, guild=50, human=1, now=20)
    caplog.set_level(logging.INFO)
    assert ledger.restart_release(started_ms=15000, guild=50, human=1, now=20) == ('root',)
    row = ledger.get('root')
    assert (row.state,row.result_ok,row.result_reason) == ('completed',False,'restart_released')
    assert not ledger.holds()
    receipt = json.loads(ledger.conn.execute("SELECT value FROM meta WHERE key='realm:action:restart_released:root'").fetchone()[0])
    assert receipt['started_ms'] == 15000 and receipt['at'] == 20
    assert receipt['before'] == json.loads(json.dumps(asdict(before)))
    assert not ledger.restart_release(started_ms=15000, guild=50, human=1, now=21)
    assert caplog.text.count('realm_action_restart_released') == 1
    assert ledger.prune(keep=0, now=100000) == 0
    assert ledger.get('root').result_reason == 'restart_released'
    h = Harness(ledger)
    assert not h.queue._backed_off('realm_day',kind,(101,102),21)
    with pytest.raises(ValueError,match='only an unknown root'):
        ledger.abandon('root',reason='fixture',now=22)


@pytest.mark.parametrize('cmd', [p.WgJoin('root',(101,102)), p.RealmActors('root',(101,102)),
    p.ItemCount('root',101,(5976,)),p.GuildGrow('root',50,(101,102)),
    p.RealmMove('root',101,'home'),p.Duel('root',101,102)])
def test_restart_excludes_wg_ack_result_grow_scope_and_continuation_kinds(ledger,cmd):
    before = unknown(ledger,cmd)
    assert not ledger.restart_release(started_ms=15000,guild=50,human=1,now=20)
    assert ledger.get('root') == before


@pytest.mark.parametrize('field,value', [('guild',51),('human',2),('started_ms',0)])
def test_restart_does_not_use_an_unrelated_or_absent_hello(ledger,field,value):
    before=unknown(ledger)
    args=dict(started_ms=15000,guild=50,human=1,now=20)
    args[field]=value
    assert not ledger.restart_release(**args)
    assert ledger.get('root') == before


def test_restart_waits_for_transport_settlement_and_conflict_stays_held(ledger):
    dispatch(ledger,prepare(ledger,p.Rearm('root',(101,102))),now=10)
    assert not ledger.restart_release(started_ms=15000,guild=50,human=1,now=20)
    ledger.observe_send_error('root','AckTimeout',now=21)
    ledger.observe_ack(Ack(0,'root',True,''),now=22)
    ledger.observe_ack(Ack(0,'root',False,'realm_off'),now=23)
    assert ledger.get('root').state == 'conflict'
    assert not ledger.restart_release(started_ms=15000,guild=50,human=1,now=24)
    assert ledger.holds()


def test_queue_hello_releases_before_recall_and_health_and_ops_no_longer_report_a_hold(ledger):
    from leeroy_mind.health import _realm_holds, OK
    from leeroy_mind.console.opsview import realm_view
    unknown(ledger)
    h=Harness(ledger)
    h.clock.value=4000
    h.queue.on_hello(Hello(0,'fixture','1',50,1,started_ms=15000))
    assert not ledger.holds() and not ledger.unresolved()
    assert _realm_holds(ledger.conn,4000).status == OK
    view = realm_view(ledger.conn,4000)
    assert view['held_members'] == 0 and view['roots'] == []


def test_cli_explains_that_restart_released_root_is_no_longer_unknown(ledger,tmp_path,clock,capsys):
    from leeroy_mind.realm.cli import realm_main
    unknown(ledger)
    ledger.restart_release(started_ms=15000,guild=50,human=1,now=20)
    assert realm_main(['release','root','--reason','fixture','--yes','--data-dir',str(tmp_path)],clock=clock) == 1
    output = capsys.readouterr().out
    assert 'only an unknown root can be released; this one is completed' in output
    assert ledger.get('root').result_reason == 'restart_released'


def test_late_terminal_cannot_remove_restart_receipt_or_reintroduce_a_hold(ledger):
    unknown(ledger,p.Rearm('root',(101,102)))
    ledger.restart_release(started_ms=15000,guild=50,human=1,now=20)
    ledger.observe_realm_job(p.RealmJob(0,'root','rearm','done','',0,0),now=21)
    ledger.observe_ack(Ack(0,'root',True,''),now=22)
    assert not ledger.holds() and ledger.get('root').result_reason == 'restart_released'
    ledger.observe_ack(Ack(0,'root',False,'realm_off'),now=23)
    assert ledger.get('root').state == 'conflict' and ledger.holds()


def test_a_child_on_an_ordinary_root_is_inconsistent_and_cannot_be_released(ledger):
    from leeroy_mind.realm.action_store import ActionUnavailable
    before = unknown(ledger)
    dispatch(ledger,prepare(ledger,p.Rearm('child',(101,102))),now=10)
    with ledger.conn:
        ledger.conn.execute("UPDATE realm_action SET parent_id='root',role='cleanup' WHERE command_id='child'")
    with pytest.raises(ActionUnavailable):
        ledger.restart_release(started_ms=15000,guild=50,human=1,now=20)
    assert ledger.get('root') == before


def test_a_failing_restart_release_still_lets_the_hello_through(ledger, caplog, monkeypatch):
    h=Harness(ledger)
    seen=[]
    monkeypatch.setattr(ledger, 'restart_release', lambda **kw: (_ for _ in ()).throw(ValueError('bad')))
    monkeypatch.setattr(h.queue, 'context_changed', lambda: seen.append(True))
    with caplog.at_level(logging.WARNING):
        h.queue.on_hello(Hello(0,'fixture','1',50,1,started_ms=15000))
    assert seen == [True]
    assert 'realm_action_restart_release_failed: ValueError' in caplog.text
