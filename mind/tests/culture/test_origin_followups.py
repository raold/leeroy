"""Real origin, transaction and hot-path boundaries; all events and names are invented."""
import asyncio
import json
import sqlite3
import statistics
import time
from types import SimpleNamespace

import pytest

from conftest import run
from culture.test_origins import receipt, wipes
from leeroy_mind.culture import origins
from leeroy_mind.culture.config import CultureConfig
from leeroy_mind.culture.seeds import _culture_seeds, culture_seeds
from leeroy_mind.nightly import language
from leeroy_mind.nightly.llm import ReflectionError
from leeroy_mind.nightly.runner import NightRunner, Stage
from leeroy_mind.server.engine import Conversation
from leeroy_mind.server.runtime import snapshot as real_snapshot
from nightly.test_lexicon_wear import enable
from server.test_engine import app


def recorded_wipes(store, start, boss):
    for offset in (0, 20):
        for second, kind, payload in (
            (0, 'boss', dict(map=631, instance_id=1, boss_id=boss, boss_name='', state='engaged', members=[101,102])),
            (1, 'death', dict(map=631, guid=101, killer_entry=1)),
            (2, 'death', dict(map=631, guid=102, killer_entry=1)),
            (3, 'boss', dict(map=631, instance_id=1, boss_id=boss, boss_name='', state='wiped', members=[101,102])),
        ):
            at = start + offset + second
            store.conn.execute('INSERT INTO game_event(ts,type,payload) VALUES(?,?,?)',
                               (at, kind, json.dumps(dict(ts=int(at*1000), **payload))))


def test_nonfatal_coin_failure_leaves_origin_retryable_and_later_stages_run(h, monkeypatch):
    enable(h)
    recorded_wipes(h.store, h.T-100, 11)
    recorded_wipes(h.store, h.T-60, 12)
    ctx = h.ctx()
    calls, later = [], []

    async def reply(*args):
        calls.append(True)
        if len(calls) == 1:
            raise ReflectionError('invalid_reply', fatal=False)
        return {'term': 'floor inspector' if len(calls) == 2 else 'floor surveyor'}

    monkeypatch.setattr(ctx.reasoner, 'json', reply)
    for unit in ('vocab', 'slang', 'jokes', 'spread', 'fade'):
        monkeypatch.setattr(language, unit, lambda ctx, name=unit: later.append(name))
    async def candidates(ctx):
        ctx.skip('language', 'candidates')
    monkeypatch.setattr(language, 'candidates', candidates)
    async def stories(ctx):
        later.append('stories')
        ctx.skip('stories', 'after_culture')
    runner = NightRunner([Stage('language', language.stage_language, None), Stage('stories', stories, None)],
                         lambda *args: ctx)
    run(runner.run(ctx.night, ctx.window))
    assert len(calls) == 2 and ctx.failures['language'] == 1
    assert later[-3:] == ['spread', 'fade', 'stories']
    assert ctx.done('stories', 'after_culture') and not ctx.done('language', 'events')
    assert h.count('lexicon') == 1
    run(origins.events(ctx))
    assert len(calls) == 3 and h.count('lexicon') == 2 and ctx.done('language', 'events')


def test_origin_discovery_failure_is_guarded_and_does_not_finish_events(h, monkeypatch):
    enable(h)
    ctx = h.ctx()
    def fail(*args):
        raise sqlite3.OperationalError('unavailable')
    monkeypatch.setattr(origins, 'event_origins', fail)
    run(origins.events(ctx))
    assert ctx.failures['language'] == 1 and not ctx.done('language', 'events')


def test_fatal_coin_failure_still_stops_the_night(h):
    enable(h)
    wipes(h)
    ctx = h.ctx()
    async def fail(*args):
        raise ReflectionError('model_unavailable', fatal=True)
    ctx.reasoner.json = fail
    with pytest.raises(ReflectionError):
        run(origins.events(ctx))
    assert not ctx.done('language', 'events')


def test_cancelled_coin_does_not_continue_to_other_origins(h):
    enable(h)
    recorded_wipes(h.store,h.T-100,11)
    recorded_wipes(h.store,h.T-60,12)
    ctx = h.ctx()
    calls = []
    async def scenario():
        entered = asyncio.Event()
        async def pending(*args):
            calls.append(True)
            entered.set()
            await asyncio.Event().wait()
        ctx.reasoner.json = pending
        task = asyncio.create_task(origins.events(ctx))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    run(scenario())
    assert calls == [True] and not ctx.done('language','events')


@pytest.mark.parametrize('kind', ['wipe', 'laugh'])
def test_origin_and_seed_reads_preserve_callers_transaction(h, kind):
    enable(h)
    if kind == 'wipe':
        wipes(h)
    else:
        h.say(h.T-100,101,'floor inspector')
        h.say(h.T-99,102,'haha')
        h.say(h.T-98,103,'haha')
    ctx = h.ctx()
    before = h.count('lexicon')
    with pytest.raises(RuntimeError, match='rollback'):
        with h.store.transaction():
            h.store.set_meta('culture:rollback-probe', 'pending')
            origin = origins.event_origins(h.store, (h.T-200,h.T))[0]
            assert origins.origin_text(h.store, origin, {})
            from leeroy_mind.nightly.lexicon import create_term
            assert create_term(h.store,ctx,kind='joke',term='floor inspector',origin_ref=origin.refs[0],
                originator=origin.originator,now=origin.when,butt=origin.butt,origin_kind='event',
                origin_refs=origin.refs,refs=origin.refs)
            queries = []
            h.store.conn.set_trace_callback(queries.append)
            try:
                assert culture_seeds(h.store, now=h.T+4*86400, local=h.clock.local(),
                    personas={g:ctx.personas.get(g) for g in ctx.members()},human=1)
            finally:
                h.store.conn.set_trace_callback(None)
            assert not any(sql.strip().upper() in {'COMMIT','ROLLBACK'} for sql in queries)
            raise RuntimeError('rollback')
    assert h.store.get_meta('culture:rollback-probe') is None and h.count('lexicon') == before


@pytest.mark.parametrize('source', ['receipt', 'attached', 'path', 'object', 'unknown'])
def test_loot_facts_use_screened_names_and_never_numeric_ids(h, tmp_path, source):
    key = receipt(h)
    record = json.loads(h.store.get_meta(key))
    if source == 'receipt':
        record['name'] = '|cffaabbcc[Invented Beacon]|r'
    h.store.set_meta(key, json.dumps(record))
    library = None
    if source in {'attached','path'}:
        path = tmp_path/'names.db'
        with sqlite3.connect(path) as conn:
            conn.execute('CREATE TABLE item(id INTEGER PRIMARY KEY,name TEXT)')
            conn.execute("INSERT INTO item VALUES(123,'Invented Beacon')")
        if source == 'attached':
            h.store.conn.execute('ATTACH DATABASE ? AS library', (str(path),))
        else:
            library = path
    elif source == 'object':
        library = SimpleNamespace(item_info=lambda ids: {123: {'name':'Invented Beacon'}})
    for origin in origins.event_origins(h.store,(h.T-200,h.T)):
        text = origins.origin_text(h.store,origin,{},library=library,ident=h.ident)
        assert ('an item' if source == 'unknown' else 'Invented Beacon') in text
        assert '123' not in text and '|c' not in text


@pytest.mark.parametrize('name', ['Quillon', '|TQuillon|t', 'as an AI'])
def test_unsafe_item_labels_fall_back_before_prompt_or_seed(h, name):
    key = receipt(h)
    record = json.loads(h.store.get_meta(key))
    record['name'] = name
    h.store.set_meta(key, json.dumps(record))
    library = SimpleNamespace(item_info=lambda ids: {123: {'name': name}})
    for origin in origins.event_origins(h.store,(h.T-200,h.T)):
        text = origins.origin_text(h.store,origin,{},library=library,ident=h.ident)
        assert 'an item' in text and name not in text


@pytest.mark.parametrize('boss,expected', [(11,'Sindragosa'), (999,'a boss')])
def test_wipe_fact_resolves_encounter_name(h, boss, expected):
    wipes(h,boss=boss)
    origin = origins.event_origins(h.store,(h.T-200,h.T))[0]
    text = origins.origin_text(h.store,origin,{},ident=h.ident)
    assert text.endswith(f'on {expected}.') and str(boss) not in text


def seed_wipe_jokes(store, now):
    start = now-5*86400
    for index, boss in enumerate((11,12)):
        recorded_wipes(store,start+index*40,boss)
    for index, origin in enumerate(origins.event_origins(store,(start,start+100))):
        ident = store.conn.execute("INSERT INTO lexicon(kind,term,origin_ref,originator,born,last_used,butt,origin_kind,origin_refs) "
            "VALUES('joke',?,?,101,?,?,101,'event',?)",(f'floor inspector {index}',origin.refs[0],origin.when,
            origin.when,json.dumps(origin.refs))).lastrowid
        store.set_meta(f'nightly:lexicon-sources:{ident}',json.dumps(origin.refs))
    return start


def test_wipe_hot_paths_remain_bounded_with_two_jokes_and_50000_chat_lines(app, monkeypatch):
    now = app.clock.now()
    app.culture_config = CultureConfig(enabled=True)
    start = seed_wipe_jokes(app.store,now)
    with app.store.transaction():
        app.store.conn.executemany("INSERT INTO chat_line(ts,channel,from_guid,from_name,to_guid,text,origin) "
            "VALUES(?,'guild',101,'Member101',0,'Invented raid chatter','game')",
            ((start+i/1000,) for i in range(50000)))
    from leeroy_mind.nightly import language_births
    def forbidden(*args,**kwargs):
        raise AssertionError('hot path attempted full origin discovery')
    monkeypatch.setattr(language_births,'find_jokes',forbidden)
    monkeypatch.setattr(origins,'event_origins',forbidden)
    before = time.perf_counter()
    observed = run(real_snapshot(app,now,include_seeds=True))
    snapshot_s = time.perf_counter()-before
    jokes = [s for s in observed.seeds if s.kind == 'joke']
    assert len(jokes) == 2
    conversation = Conversation('bounded','general',jokes[0],[102,101],now,5)
    monkeypatch.setattr(app.server.engine,'_tease',lambda *args,**kwargs: True)
    original = origins.origin_text
    checked = []
    def track(store,origin,names,**kwargs):
        checked.append(origin.refs)
        return original(store,origin,names,**kwargs)
    monkeypatch.setattr('leeroy_mind.culture.seeds.origin_text',track)
    timings = []
    for _ in range(7):
        before = time.perf_counter()
        assert app.server.engine._joke_valid(conversation,102)
        timings.append(time.perf_counter()-before)
    assert len(checked) == 7 and len(set(checked)) == 1
    print(f'culture_hot_path lines=50000 jokes=2 snapshot_ms={snapshot_s*1000:.3f} '
          f'valid_median_ms={statistics.median(timings)*1000:.3f} valid_max_ms={max(timings)*1000:.3f}')
    assert snapshot_s < 2 and statistics.median(timings) < .01


def test_wipe_recheck_rejects_overlarge_event_window(h, monkeypatch):
    wipes(h)
    origin = origins.event_origins(h.store,(h.T-200,h.T))[0]
    monkeypatch.setattr(origins,'WIPE_RECHECK_ROWS',6)
    assert origins.origin_text(h.store,origin,{}) is None


def test_fresh_banter_reservation_blocks_new_seed_but_not_current_replies(h):
    from nightly.test_night_lexicon import term
    ident = term(h)
    ctx = h.ctx()
    args = dict(now=h.T+4*86400,personas={g:ctx.personas.get(g) for g in ctx.members()},human=1,only=ident)
    h.store.set_meta(f'banter:used:joke:{ident}',str(args['now']))
    assert not _culture_seeds(h.store,**args)
    assert _culture_seeds(h.store,**args,include_recent=True)
    h.store.set_meta(f'banter:used:joke:{ident}',str(args['now']-86400))
    assert _culture_seeds(h.store,**args)


@pytest.mark.parametrize('channel', ['officer','voice:officers','discord:officer-chat','dm:101','gdm:1'])
def test_banter_fold_leaves_restricted_origins_frozen(h,channel):
    from nightly.test_night_lexicon import term
    ident = term(h)
    h.store.conn.execute('UPDATE chat_line SET channel=? WHERE id=(SELECT CAST(substr(origin_ref,6) AS INTEGER) '
                         'FROM lexicon WHERE id=?)',(channel,ident))
    h.store.set_meta(f'banter:joke-use:joke:{ident}',json.dumps({'at':h.T,'count':3}))
    before = tuple(h.store.conn.execute('SELECT * FROM lexicon WHERE id=?',(ident,)).fetchone())
    with h.store.transaction():
        origins.fold_banter_uses(h.store)
    assert tuple(h.store.conn.execute('SELECT * FROM lexicon WHERE id=?',(ident,)).fetchone()) == before
    assert h.store.get_meta(f'culture:banter-fold:joke:{ident}') is None
