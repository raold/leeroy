import asyncio
import copy
import json
import logging
import sqlite3
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from console.rig import http, make_app, running
from leeroy_mind import endpoint, insight_check
from leeroy_mind.awareness import CardContext, card
from leeroy_mind.claims.store import add, supersede
from leeroy_mind.console import feed, insightreports as reports, insightview as view
from leeroy_mind.console.control import Controls
from leeroy_mind.storage.rows import pack_vec
from leeroy_mind.protocol import Chat

NOW = 1_793_304_000.0
SECRET = 'PRIVATE_TYPED_SENTINEL'
VECTOR = pack_vec([1.] + [0.] * 511)


@pytest.fixture
def data(store):
    return Path(store.conn.execute('PRAGMA database_list').fetchone()['file']).parent


def claim(store, **kwargs):
    return add(store, 101, subject='saurfang', text='I love saurfang', embedding=VECTOR, source='owner',
               **({'kind': 'opinion', 'polarity': 1, 'now': NOW} | kwargs))


def report(until=NOW, *, fake=True):
    summary = dict(lines=10, flagged_lines=1, per_100_lines=10.)
    return dict(version=1, fake=fake, judged=False, since_days=14, window={'until': until},
        embedding_fingerprint='fake', parameters={'threshold': .8},
        per_member={str(i): copy.deepcopy(summary) for i in range(101, 121)},
        detectors={name: {'per_member': {str(i): copy.deepcopy(summary) for i in range(101, 121)},
                         'hits': [{'text': SECRET}]} for name in reports.DETECTORS})


def save(data, value, name='report.json'):
    (data / 'scratch').mkdir(exist_ok=True)
    (data / 'scratch' / name).write_text(json.dumps(value), encoding='utf-8')


def test_claims_live_history_expiry_and_read_only(store, data):
    old = claim(store)
    new = claim(store, polarity=-1)
    supersede(store, old, new)
    claim(store, kind='plan', expires=NOW)
    before = store.conn.total_changes
    result = feed.read(data / 'mind.db', view.insight_view, 101, data_dir=data, now=NOW)
    assert [r['id'] for r in result['claims']['live']] == [new]
    assert result['claims']['history'][0]['id'] == old
    assert 'text' not in result['claims']['live'][0]
    assert store.conn.total_changes == before
    assert result['guards']['state'] == 'missing'
    assert 'not recorded' in result['guards']['why']


def test_old_database_independent_sources(tmp_path):
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    result = view.insight_view(conn, 101, data_dir=tmp_path, now=NOW)
    assert result['claims']['state'] == 'missing'
    assert result['awareness']['state'] == 'missing'
    assert result['quality']['state'] == 'missing'
    assert '--fake' in result['quality']['command']
    conn.close()


def test_awareness_exact_package_render(store):
    claim(store)
    result = view.awareness(store.conn, 101, NOW)
    expected = card(store.conn, 101, context=CardContext(channel='guild', now=NOW))
    for name, budget in view.BUDGETS.items():
        assert result['variants'][name]['text'] == expected.render(budget)


def test_awareness_excludes_private_claim_provenance(store):
    event = Chat(ts=int(NOW*1000), channel='whisper', from_guid=101, from_name='Invented',
                 to_guid=102, text=SECRET, map=0, zone=0)
    line = store.insert_chat_line(event, session_id=None, origin='mind')
    add(store, 101, kind='opinion', subject='secret', text=SECRET, polarity=1,
        embedding=VECTOR, source_line=line, now=NOW)
    result = view.awareness(store.conn, 101, NOW)
    assert SECRET not in json.dumps(result)


def test_missing_claim_table_does_not_hide_repetition_badge(tmp_path):
    raw = report()
    raw['detectors']['repetition']['per_member']['101'] = dict(lines=10, flagged_lines=8, per_100_lines=80.)
    run = reports._project(raw)
    conn = sqlite3.connect(':memory:')
    badges, why = view.badges(conn, [run], NOW)
    assert badges['101'] == {'conflicts':0, 'repetition':8}
    assert why
    conn.close()


def test_badges_exclude_superseded_and_expired(store):
    first = claim(store)
    claim(store, kind='plan', polarity=-1, expires=NOW)
    assert view.conflict_badges(store.conn, NOW) == {}
    second = claim(store, polarity=-1)
    assert view.conflict_badges(store.conn, NOW) == {'101': 1}
    supersede(store, first, second)
    assert view.conflict_badges(store.conn, NOW) == {}


def test_claims_bounded(store):
    for i in range(110):
        claim(store, now=NOW + i)
    result = view.claims(store.conn, 101, NOW + 200)
    assert len(result['live']) == 100 and result['truncated']


def test_report_rates_rank_trend_and_no_private_hits(data):
    current = report()
    current['detectors']['repetition']['per_member']['101'] = dict(lines=10, flagged_lines=8, per_100_lines=80.)
    save(data, current)
    save(data, report(NOW-100), 'before.json')
    save(data, report(NOW-50, fake=False), 'different-mode.json')
    runs, why = reports.reports(data, now=NOW)
    result = reports.quality(runs, 101, now=NOW, why=why)
    assert result['rates']['repetition']['per_100_lines'] == 80
    assert result['rank'] == 1 and result['guild_members'] == 20
    assert len(result['trend']) == 2
    assert SECRET not in json.dumps(result)
    assert reports.repetition_badges(runs, now=NOW) == {'101': 8}
    assert reports.repetition_badges(runs, now=NOW + 8 * 86400) == {}
    assert reports.quality(runs, 101, now=NOW + 8 * 86400)['state'] == 'stale'
    assert reports.quality(runs, 999, now=NOW)['state'] == 'missing'


@pytest.mark.parametrize('broken', [[], {}, {'version': 100}, {'fake': 'true'}, {'window': {'until': float('nan')}}])
def test_bad_report_is_words(data, broken):
    save(data, broken)
    runs, why = reports.reports(data, now=NOW)
    assert runs == [] and why


def test_missing_and_oversize_report(data, monkeypatch):
    assert reports.reports(data, now=NOW)[0] == []
    save(data, report())
    monkeypatch.setattr(reports, 'MAX_BYTES', 10)
    assert reports.reports(data, now=NOW+30)[0] == []


def test_report_scan_budget_and_cache(data, monkeypatch):
    save(data, report())
    monkeypatch.setattr(reports, 'MAX_FILES', 0)
    runs, why = reports.reports(data, now=NOW)
    assert not runs and 'Too many' in why
    monkeypatch.setattr(reports, 'MAX_FILES', 256)
    assert reports.reports(data, now=NOW)[0] == []  # cached for this 30-second bucket
    assert len(reports.reports(data, now=NOW+30)[0]) == 1


def test_invalid_report_does_not_hide_valid_older_run(data):
    save(data, report(NOW-10), 'valid.json')
    save(data, {'window':{'until': NOW}}, 'broken.json')
    runs, why = reports.reports(data, now=NOW)
    assert len(runs) == 1 and why


def test_query_error_is_local_to_one_part(store, data, monkeypatch):
    def interrupted(*_):
        raise sqlite3.OperationalError('interrupted')
    monkeypatch.setattr(view, 'claims', interrupted)
    result = view.insight_view(store.conn, 101, data_dir=data, now=NOW)
    assert result['claims']['state'] == 'error'
    assert result['quality']['state'] == 'missing' and result['awareness']['variants']


@pytest.mark.parametrize('bad', [None, {}, {'guid': True, 'text':'x'}, {'guid':0, 'text':'x'},
    {'guid':101, 'text':''}, {'guid':101, 'text':'a'*501}, {'guid':101, 'text':'\x00'},
    {'guid':101, 'text':'\ud800'}, {'guid':101, 'text':'x', 'extra':True}])
def test_check_args_refuse_without_echo(bad):
    with pytest.raises(endpoint.BadRequest):
        endpoint.claim_check_args(bad)


class Admin:
    def __init__(self, loaded=('embed',)):
        self.loaded, self.calls = loaded, 0

    async def loaded_models(self):
        self.calls += 1
        return self.loaded


class Embedder:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    async def embed(self, texts, priority):
        self.calls.append(texts)
        if self.fail:
            raise RuntimeError(SECRET)
        return [VECTOR]


def app_for(store, *, loaded=('embed',), fail=False):
    return NS(stopping=False, store=store, clock=NS(now=lambda: NOW),
              cfg=NS(models=NS(embedding='embed')), arbiter=NS(admin=Admin(loaded)),
              embedder=Embedder(fail))


def test_check_one_embed_no_writes_no_text_in_logs(store, data, caplog):
    claim(store)
    app = app_for(store)
    app.data_dir = data
    before = store.conn.total_changes
    caplog.set_level(logging.DEBUG)
    server = endpoint.ControlEndpoint(app, endpoint.ControlCfg(port=0), key='k'*32)
    result, after = asyncio.run(server._answer(endpoint.encode({'v':1, 'key':'k'*32,
        'op':'claim_check', 'args':{'guid':101, 'text':'I hate saurfang'}})))
    assert after is None
    assert result['ok'] and result['conflicts'][0]['id']
    assert len(app.embedder.calls) == app.arbiter.admin.calls == 1
    assert store.conn.total_changes == before
    assert 'claim_text' not in result['conflicts'][0]
    assert 'I hate saurfang' not in caplog.text


def test_check_timeout_releases_lock_without_embedding(store):
    claim(store)
    app = app_for(store)
    async def hung():
        await asyncio.Event().wait()
    app.arbiter.admin.loaded_models = hung
    async def run():
        lock = asyncio.Lock()
        reply = await insight_check.run(app, {'guid':101, 'text':SECRET}, lock, timeout=.01)
        assert not lock.locked()
        return reply
    assert 'timed out' in asyncio.run(run())['error']
    assert app.embedder.calls == []


@pytest.mark.parametrize('mode,words', [('unloaded','not loaded'), ('down','stopping'), ('empty','no live claims'),
                                       ('failure','could not'), ('busy','Another'), ('model','different')])
def test_check_refusals(store, caplog, mode, words):
    if mode != 'empty':
        claim(store)
    app = app_for(store, loaded=() if mode == 'unloaded' else ('embed',), fail=mode == 'failure')
    app.stopping = mode == 'down'
    if mode == 'model':
        store.set_meta('embed_model', 'other')
    async def run():
        lock = asyncio.Lock()
        if mode == 'busy':
            await lock.acquire()
        return await insight_check.run(app, {'guid':101, 'text':SECRET}, lock)
    result = asyncio.run(run())
    assert not result['ok'] and words in result['error']
    assert SECRET not in caplog.text + json.dumps(result)
    assert len(app.embedder.calls) == (1 if mode == 'failure' else 0)


def test_http_gates_read_only_and_down_refusal(store, data, caplog):
    claim(store)
    async def run():
        mind = running(data)
        app = make_app(data, mind=mind, controls=Controls(enabled=False, mind=mind, voice=NS(act=lambda _:None)),
                       clock=lambda:NOW)
        await app.start()
        try:
            path = '/api/persona/insight?guid=101'
            assert (await http(app, 'GET', path, key=False)).status == 403
            assert (await http(app, 'GET', path, host='evil.invalid')).status == 403
            assert (await http(app, 'GET', path)).json()['claims']['state'] == 'ok'
            assert (await http(app, 'GET', path + '&guid=102')).status == 400
            body = {'guid':101, 'text':SECRET}
            assert (await http(app, 'POST', '/api/persona/check', body=body)).status == 403
            response = await http(app, 'POST', '/api/persona/check', body=body, origin=True)
            assert response.status == 409 and SECRET not in response.body.decode()
        finally:
            await app.close()
    asyncio.run(run())
    assert SECRET not in caplog.text


def test_read_post_works_with_controls_disabled(store, data, caplog):
    class Link:
        def locate(self, pids):
            return {'pid':101, 'port':1, 'key':'x'}

        def call(self, pids, op, args, *, timeout):
            assert op == 'claim_check' and args == {'guid':101, 'text':SECRET}
            return {'ok':True, 'conflicts':[]}

    async def scenario():
        mind = running(data)
        app = make_app(data, mind=mind, link=Link(), controls=Controls(enabled=False, mind=mind))
        await app.start()
        try:
            response = await http(app, 'POST', '/api/persona/check', origin=True,
                                  body={'guid':101, 'text':SECRET})
            assert response.status == 200 and response.json()['conflicts'] == []
        finally:
            await app.close()
    before = store.conn.total_changes
    asyncio.run(scenario())
    assert before == store.conn.total_changes and SECRET not in caplog.text
