import asyncio
from contextlib import asynccontextmanager, suppress
import json
import sqlite3
from types import SimpleNamespace

import pytest

from conftest import HUMAN, seed_test_cast
from leeroy_mind.endpoint import ControlCfg, ControlEndpoint, Forbidden, parse_request
from leeroy_mind.protocol import Chat
from leeroy_mind.signals.attach import Signals
from leeroy_mind.signals.capture import Capture, Observation, QUEUE_LIMIT
from leeroy_mind.signals.cli import signals_main
from leeroy_mind.signals.config import load_signals_config
from leeroy_mind.signals import store as S


@asynccontextmanager
async def worker(store, clock):
    path = store.conn.execute('PRAGMA database_list').fetchone()[2]
    capture = Capture(path, clock)
    task = asyncio.create_task(capture.run())
    try:
        yield capture
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        await capture.close()


def line(store, clock, guid=101, channel='guild', to=0, text='invented private test words', thread=None):
    chat = Chat(int(clock.now()*1000), channel, guid, 'Testmage' if guid == HUMAN else 'Arkon',
                to, text, 0, 10)
    lid = store.insert_chat_line(chat, session_id=None, origin='human' if guid == HUMAN else 'mind')
    if thread:
        store.set_thread([lid], thread)
    return lid


def observe(capture, clock, kind, ids=(), values=()):
    return capture.offer(Observation(kind, clock.now(), HUMAN, tuple(ids), tuple(values)))


def saved(store):
    store.conn.commit()
    return [tuple(r) for r in store.conn.execute('SELECT line_id,guid,kind,value,source FROM talk_signal ORDER BY id')]


@pytest.mark.parametrize('text,enabled', [('', False), ('[signals]\nenabled=true', True),
    ('[signals]\nenabled=false', False), ('signals="invented secret"', False),
    ('[signals]\nenabled=1', False), ('[signals]\nenabled="yes"', False),
    ('[signals]\nenabled=true\nunknown=1', False), ('[signals', False)])
def test_config_off_or_valid_boolean_only(tmp_path, text, enabled):
    path = tmp_path / 'mind.toml'
    path.write_text(text)
    assert load_signals_config(path).enabled is enabled


def test_latest_reply_normalized_units_and_thread_continued(store, clock):
    async def go():
        tid = store.open_thread('guild', clock.now())
        async with worker(store, clock) as c:
            first = line(store, clock, 102, thread=tid)
            observe(c, clock, 'sent', (first,), (False, True, True))
            latest = line(store, clock, thread=tid)
            observe(c, clock, 'sent', (latest,), (True, True, True))
            await clock.advance(30)
            answer = line(store, clock, HUMAN, text='x'*100, thread=tid)
            observe(c, clock, 'owner', (answer,), (.2, False))
            await c.flush()
            assert saved(store) == [(latest, 101, 'replied', 1., 'implicit'),
                (latest, 101, 'reply_latency_s', .25, 'implicit'),
                (latest, 101, 'reply_len', .2, 'implicit')]
            next_line = line(store, clock, thread=tid)
            observe(c, clock, 'sent', (next_line,), (True, True, True))
            await clock.advance(10)
            next_answer = line(store, clock, HUMAN, thread=tid)
            observe(c, clock, 'owner', (next_answer,), (1., False))
            await c.flush()
            assert (next_line, 101, 'thread_continued', 1., 'implicit') in saved(store)
    asyncio.run(go())


@pytest.mark.parametrize('other_channel,other_to', [('guild', 0), ('whisper', 102), ('officer', 0)])
def test_private_reply_never_crosses_audience(store, clock, other_channel, other_to):
    async def go():
        async with worker(store, clock) as c:
            lid = line(store, clock, channel='whisper', to=HUMAN)
            observe(c, clock, 'sent', (lid,), (True, True, True))
            answer = line(store, clock, HUMAN, other_channel, other_to)
            observe(c, clock, 'owner', (answer,), (.1, False))
            await c.flush()
            assert not saved(store)
    asyncio.run(go())


@pytest.mark.parametrize('block', ['none', 'unknown', 'late_rest', 'inactive', 'combat', 'afk', 'offline', 'unaddressed', 'unheard', 'replied', 'group'])
def test_ignored_requires_active_uninterrupted_addressed_heard_line(store, clock, block):
    async def go():
        async with worker(store, clock) as c:
            observe(c, clock, 'presence', values=(True,))
            if block not in ('unknown', 'late_rest'):
                observe(c, clock, 'combat', values=(False,))
            lid = line(store, clock)
            observe(c, clock, 'sent', (lid,), (block != 'unaddressed', True, block != 'unheard'))
            if block == 'combat':
                observe(c, clock, 'combat', values=(True,))
                observe(c, clock, 'combat', values=(False,))
            elif block == 'afk':
                observe(c, clock, 'afk', values=(True,))
            elif block == 'offline':
                observe(c, clock, 'presence', values=(False,))
            elif block == 'group':
                observe(c, clock, 'group')
            elif block == 'replied':
                answer = line(store, clock, HUMAN)
                observe(c, clock, 'owner', (answer,), (.1, False))
            if block != 'inactive':
                observe(c, clock, 'activity')
            await clock.advance(119)
            await c.flush()
            assert not [r for r in saved(store) if r[2] == 'ignored']
            await clock.advance(1)
            if block not in ('combat', 'unknown'):
                observe(c, clock, 'combat', values=(False,))
            await c.flush()
            assert bool([r for r in saved(store) if r[2] == 'ignored']) is (block == 'none')
    asyncio.run(go())


@pytest.mark.parametrize('kind', ['emote_friendly', 'emote_romantic', 'emote_rude'])
@pytest.mark.parametrize('delay', [60, 61])
def test_emote_kinds_and_window(store, clock, kind, delay):
    async def go():
        async with worker(store, clock) as c:
            lid = line(store, clock)
            observe(c, clock, 'sent', (lid,), (False, False, True))
            await clock.advance(delay)
            observe(c, clock, 'emote', (101,), (kind,))
            await c.flush()
            assert saved(store) == ([(lid, 101, kind, 1., 'implicit')] if delay == 60 else [])
    asyncio.run(go())


@pytest.mark.parametrize('kind', ['dial_down', 'nudged_against'])
def test_intervention_is_bounded_and_cannot_target_another_private_pair(store, clock, kind):
    async def go():
        async with worker(store, clock) as c:
            public = line(store, clock)
            observe(c, clock, 'sent', (public,), (False, False, True))
            line(store, clock, channel='whisper', to=102)
            observe(c, clock, kind, (101,))
            await c.flush()
            assert saved(store) == [(public, 101, kind, 1., 'implicit')]
    asyncio.run(go())


def test_muted_receipt_once_and_no_deafen_or_other_drop(store, clock):
    async def go():
        async with worker(store, clock) as c:
            lid = line(store, clock)
            with store.transaction():
                store.conn.execute("INSERT INTO voice_line(speak_id,line_id,room,state,reason,ts) VALUES(?,?,?,?,?,?)",
                                   ('v1', lid, 'guild', 'dropped', 'muted', clock.now()))
            observe(c, clock, 'muted', ('v1',))
            observe(c, clock, 'muted', ('v1',))
            observe(c, clock, 'muted', ('missing',))
            await c.flush()
            assert saved(store) == [(lid, 101, 'muted', 1., 'implicit')]
    asyncio.run(go())


def test_feedback_key_control_and_enable_gates_and_replace_vote(store, clock, tmp_path):
    async def go():
        async with worker(store, clock) as c:
            app = SimpleNamespace(data_dir=tmp_path, stopping=False, human_guid=HUMAN, clock=clock)
            app.signals = Signals.__new__(Signals)
            app.signals.app, app.signals.capture = app, c
            ep = ControlEndpoint(app, ControlCfg(), key='K'*43)
            lid = line(store, clock)
            args = {'line_id': lid, 'value': 1, 'reasons': ['funny', 'in-character']}
            request = {'v': 1, 'key': 'K'*43, 'op': 'feedback', 'args': args}
            with pytest.raises(Forbidden):
                parse_request((json.dumps({**request, 'key': 'wrong'})+'\n').encode(), ep.key)
            off = ControlEndpoint(app, ControlCfg(enabled=False))
            assert not (await off.feedback(args))['ok']
            reply, callback = await ep._answer((json.dumps(request)+'\n').encode())
            assert reply['ok'] and reply['recorded'] == 3 and callback is None
            args['value'], args['reasons'] = -1, ['boring']
            assert (await ep.feedback(args))['ok']
            assert saved(store) == [(lid, 101, 'explicit', -1., 'explicit'),
                                     (lid, 101, 'reason_boring', 1., 'explicit')]
            owner_line = line(store, clock, HUMAN)
            assert not (await ep.feedback({**args, 'line_id': owner_line}))['ok']
            app.signals = None
            assert not (await ep.feedback(args))['ok']
    asyncio.run(go())


@pytest.mark.parametrize('args', [None, {}, {'line_id': True, 'value': 1, 'reasons': []},
    {'line_id': 1, 'value': True, 'reasons': []}, {'line_id': 1, 'value': 0, 'reasons': []},
    {'line_id': 1, 'value': 1, 'reasons': ['invented secret']},
    {'line_id': 1, 'value': 1, 'reasons': ['funny', 'funny']}])
def test_feedback_shape_is_closed(args):
    with pytest.raises(ValueError) as error:
        S.feedback_args(args)
    assert 'invented secret' not in str(error.value)


def test_retention_and_no_text_columns(store, clock):
    lid = line(store, clock)
    with store.transaction():
        S.put(store.conn, lid, 101, 'replied', 1, clock.now() - S.RETENTION_S - 1)
        S.put(store.conn, lid, 101, 'muted', 1, clock.now())
        assert S.prune(store.conn, clock.now()) == 1
    assert [r[2] for r in saved(store)] == ['muted']
    assert {r[1] for r in store.conn.execute('PRAGMA table_info(talk_signal)')} == {
        'id', 'line_id', 'act_id', 'guid', 'kind', 'value', 'ts', 'source'}


def test_queue_is_bounded_and_offer_never_waits_for_writer(tmp_path, clock, monkeypatch):
    capture = Capture(tmp_path / 'absent.db', clock)
    monkeypatch.setattr(capture, '_process', lambda _: pytest.fail('offer ran the writer'))
    item = Observation('activity', clock.now(), HUMAN)
    for _ in range(QUEUE_LIMIT):
        assert capture.offer(item)
    assert not capture.offer(item) and capture.dropped == 1
    assert not (tmp_path / 'absent.db').exists()
    capture.executor.shutdown(wait=False)


def test_older_database_and_cli_never_migrate(tmp_path, clock, capsys):
    path = tmp_path / 'mind.db'
    with sqlite3.connect(path) as conn:
        conn.execute('PRAGMA user_version=23')
    assert signals_main(['status', '--data-dir', str(tmp_path)]) == 0
    assert 'not set up' in capsys.readouterr().out
    capture = Capture(path, clock)
    assert not capture._process(Observation('barrier', clock.now(), HUMAN))['ok']
    capture._process(Observation('close', clock.now(), HUMAN))
    capture.executor.shutdown(wait=False)
    with sqlite3.connect(path) as conn:
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 23
        assert not S.available(conn)


@pytest.mark.parametrize('block', ['stale', 'command', 'group', 'unheard'])
def test_reply_excludes_expired_commands_group_changes_and_unheard_lines(store, clock, block):
    async def go():
        async with worker(store, clock) as c:
            lid = line(store, clock, channel='party')
            observe(c, clock, 'sent', (lid,), (False, True, block != 'unheard'))
            if block == 'stale':
                await clock.advance(121)
            elif block == 'group':
                observe(c, clock, 'group')
            answer = line(store, clock, HUMAN, 'raid')
            observe(c, clock, 'owner', (answer,), (.1, block == 'command'))
            await c.flush()
            assert not saved(store)
    asyncio.run(go())


def test_unheard_database_rows_cannot_receive_emote_or_intervention(store, clock):
    async def go():
        async with worker(store, clock) as c:
            lid = line(store, clock, channel='officer')
            observe(c, clock, 'sent', (lid,), (False, False, False))
            for kind, values in (('emote', ('emote_friendly',)), ('dial_down', ()), ('nudged_against', ())):
                observe(c, clock, kind, (101,), values)
            await c.flush()
            assert not saved(store)
    asyncio.run(go())


def test_queue_loss_discards_pre_gap_attribution(store, clock):
    async def go():
        c = Capture(store.conn.execute('PRAGMA database_list').fetchone()[2], clock)
        lid = line(store, clock)
        observe(c, clock, 'sent', (lid,), (True, True, True))
        for _ in range(QUEUE_LIMIT - 1):
            observe(c, clock, 'activity')
        assert not observe(c, clock, 'group')  # even a lost boundary must invalidate old evidence
        task = asyncio.create_task(c.run())
        try:
            await c.queue.join()
            answer = line(store, clock, HUMAN)
            observe(c, clock, 'owner', (answer,), (.1, False))
            await c.flush()
            assert not saved(store) and not c.pending and not c.recent
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            await c.close()
    asyncio.run(go())


def test_write_failure_logs_only_class_and_resets_attribution(store, clock, caplog, monkeypatch):
    async def go():
        async with worker(store, clock) as c:
            lid = line(store, clock)
            observe(c, clock, 'sent', (lid,), (True, True, True))
            await c.flush()
            def failed(*_):
                raise RuntimeError('invented private source words')
            monkeypatch.setattr(c, '_apply', failed)
            assert not (await c.flush())['ok']
            assert not c.pending and not c.recent
            assert 'RuntimeError' in caplog.text and 'invented private source words' not in caplog.text
    asyncio.run(go())


def test_cli_reads_numeric_counts_without_mutating_or_printing_source(store, clock, capsys):
    lid = line(store, clock)
    with store.transaction():
        S.put(store.conn, lid, 101, 'replied', 1, clock.now())
    path = store.conn.execute('PRAGMA database_list').fetchone()[2]
    from pathlib import Path
    before = store.conn.total_changes
    assert signals_main(['member', '101', '--data-dir', str(Path(path).parent)]) == 0
    output = capsys.readouterr().out
    assert 'replied implicit 1' in output and 'invented private' not in output and 'Arkon' not in output
    assert store.conn.total_changes == before


@pytest.mark.parametrize('guid', ['0', '-1', '4294967296', '999999999999999999999999999', 'abc'])
def test_cli_member_guid_is_uint32(tmp_path, guid, capsys):
    with pytest.raises(SystemExit) as error:
        signals_main(['member', guid, '--data-dir', str(tmp_path)])
    assert error.value.code == 2
    assert 'member guid must be' in capsys.readouterr().err
    assert not (tmp_path / 'mind.db').exists()
