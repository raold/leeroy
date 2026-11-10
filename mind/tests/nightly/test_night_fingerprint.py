"""Invented text only; no service, model, dictionary download or live database."""
import json
import logging
import sqlite3
from dataclasses import asdict, replace

import pytest

from conftest import run
from leeroy_mind.fingerprint import (Profile, distinctiveness, fingerprint, measure, quantile,
                                      render_style_hint, seed, update)
from leeroy_mind.fingerprint.config import enabled
from leeroy_mind.fingerprint.night import attach, report_section, stage_fingerprint
from leeroy_mind.nightly.pipeline import STAGES
from leeroy_mind.nightly.runner import NightRunner
from leeroy_mind.text import estimate_tokens


def test_exact_profile_math_and_markup():
    p = measure(['lol teh tank?', 'GO now!', 'hi :)', '|cffFFFFFFone two three four|r', 'haha 😂'])
    assert (p.samples,p.p25,p.p50,p.p90) == (5,1,2,3.6)
    assert p.lowercase == .8 and p.no_punctuation == .6
    assert p.emoji == .2 and p.emoticon == .2 and p.laughter == .4
    assert p.typo == pytest.approx(1/11)
    assert p.question == .2 and p.exclamation == .2 and p.emphasis == .2 and p.slang == .2
    assert quantile([4],.9) == 4
    assert measure([]).available == 0


def test_word_typos_are_only_known_misspellings_and_marks_are_whole():
    from leeroy_mind.fingerprint import _phrases
    p = measure(['Arkon zqxw university', 'LOL haha LMAO', 'xD o7 <3', '👩🏽‍💻 🇬🇧'])
    assert p.typo == 0 and p.laughter == .25 and p.emoticon == .25 and p.emoji == .25
    phrases = _phrases([('👩🏽‍💻 🇬🇧','whisper',1)],set())
    assert set(phrases['emoji']) == {'👩🏽‍💻','🇬🇧'}


@pytest.mark.parametrize('age', [25,35,75,99])
@pytest.mark.parametrize('talk', [0,5,10])
def test_seed_mapping_is_deterministic_bounded_and_never_uses_text(age,talk):
    raw = {'identity': {'age': age, 'region':'UK', 'real_name':'Private Canary'},
           'humour':'dry', 'education':'university', 'secret':'private content'}
    p = seed(raw,talkative=talk)
    assert p == seed(raw,talkative=talk)
    assert p.p50 == 6+talk+(age-35)/10+2
    assert p.laughter == .08 and p.exclamation == pytest.approx(.05+.01*talk)
    assert p.seeded == p.available == 1 and p.samples == 0
    assert all(0 <= getattr(p,k) <= 1 for k in ('lowercase','emoji','emoticon','laughter'))
    assert 'Private' not in json.dumps(asdict(p))
    us = seed({**raw, 'identity':{**raw['identity'],'region':'US'}},talkative=talk,humour_dial=0)
    assert us.exclamation == pytest.approx(p.exclamation+.02) and us.laughter == 0


def test_distance_has_honest_missing_and_mean_pairwise_basis():
    a = Profile(available=1)
    b = replace(a,p50=40,lowercase=.9)
    assert distinctiveness([]) is None and distinctiveness([a]) is None
    assert distinctiveness([a,a]) == 0
    assert distinctiveness([a,b]) == pytest.approx(1/13)
    assert distinctiveness([a,a,b]) == pytest.approx(2/39)
    assert distinctiveness([a,b,Profile()]) == distinctiveness([a,b])


@pytest.mark.parametrize('p', [Profile(), measure(['LOL WHY?!']*50), measure(['hi']*50),
                              seed({'identity':{'age':99,'region':'US'}},talkative=10)])
def test_hint_has_no_names_phrases_or_explicit_text_and_fits_cap(p):
    hint = render_style_hint(replace(p, opener_ids=(999,), closer_ids=(998,)))
    assert estimate_tokens(hint) <= 40
    assert len(hint.split()) <= 40
    assert not any(x in hint for x in ('999','998','Arkon','Private','LOL','WHY'))
    assert hint.startswith('Typing: ')


def test_latest_300_are_mind_only_and_phrase_history_is_bounded(h):
    h.persona(101)
    for i in range(350):
        h.say(h.T+i,101,'old words ' * 8 if i<50 else 'hey there lol')
    h.say(h.T+400,101,'foreign origin '+ 'x '*50,origin='game')
    with h.store.transaction():
        p = update(h.store.conn,101,now=h.T+500)
    assert p.samples == 300 and p.seeded == 0 and p.p50 == 3
    assert fingerprint(h.store.conn,101) == p
    assert all(type(x) is int for key,value in asdict(p).items() if key.endswith('_ids') for x in value)
    assert h.store.conn.execute('SELECT count(*) FROM phrase WHERE guid=101').fetchone()[0]<=25
    old = p.opener_ids
    for i in range(300):
        h.say(h.T+600+i,101,'new ending today')
    with h.store.transaction():
        later = update(h.store.conn,101,now=h.T+1000)
    assert old != later.opener_ids
    assert h.store.conn.execute("SELECT count(*) FROM phrase WHERE text LIKE '%hey%'").fetchone()[0] == 0


def test_seed_below_50_then_measured_and_real_names_private_phrases_never_escape(h):
    h.persona(101)
    for i in range(49):
        h.say(h.T+i,101,'private canary phrase',channel='whisper',to=1)
    with h.store.transaction():
        p = update(h.store.conn,101,now=h.T+50)
    assert p.samples == 49 and p.seeded == 1 and not p.opener_ids
    h.say(h.T+50,101,'officer secret canary',channel='officer')
    h.say(h.T+51,101,'Arkon Quillon public',channel='guild')
    with h.store.transaction():
        p = update(h.store.conn,101,now=h.T+52,blocked_names=('Quillon',))
    assert p.seeded == 0 and p.samples == 51
    text = ' '.join(row[0] for row in h.store.conn.execute('SELECT text FROM phrase'))
    assert all(word not in text for word in ('private','canary','officer','secret','arkon','quillon'))
    assert 'public' in text


def test_stage_resumes_atomically_without_model_or_text_logs(h,caplog):
    h.persona(101)
    h.persona(102)
    for i in range(50):
        h.say(h.T+i,101,'private canary words!',channel='whisper',to=1)
    ctx = h.ctx()
    caplog.set_level(logging.INFO,'leeroy_mind')
    run(stage_fingerprint(ctx))
    assert ctx.done('fingerprint','101') and ctx.done('fingerprint','102')
    raw = h.store.conn.execute('SELECT profile FROM voice_fingerprint WHERE guid=101').fetchone()[0]
    h.say(h.T+100,101,'Changed words.')
    run(stage_fingerprint(ctx))
    assert h.store.conn.execute('SELECT profile FROM voice_fingerprint WHERE guid=101').fetchone()[0] == raw
    assert ctx.reasoner.calls == 0 and not ctx.arbiter.admin.calls
    assert 'fingerprint_updated' in caplog.text
    assert all(x not in caplog.text for x in ('private','canary','words','Arkon','Quillon'))
    report = report_section(ctx,'2026-W44')
    assert report['members'] == 5 and report['seeded'] == 4
    assert 0 <= report['mean_pairwise_distance'] <= 1


def test_failed_unit_rolls_back_phrases_and_sanitizes_error(h,monkeypatch,caplog):
    from leeroy_mind.fingerprint import night
    h.persona(101)
    def fail(conn,*args,**kwargs):
        conn.execute("INSERT INTO phrase(guid,kind,text) VALUES(101,'opener','private canary')")
        raise ValueError('private canary')
    monkeypatch.setattr(night,'update',fail)
    ctx = h.ctx()
    caplog.set_level(logging.INFO,'leeroy_mind')
    run(stage_fingerprint(ctx))
    assert not ctx.done('fingerprint','101') and ctx.failures['fingerprint'] == 5
    assert h.store.conn.execute('SELECT count(*) FROM phrase').fetchone()[0] == 0
    assert 'fingerprint_unit_failed' in caplog.text and 'private' not in caplog.text


@pytest.mark.parametrize('text', ['', '[fingerprint]\nenabled=false', '[fingerprint]\nenabled="true"',
                                  '[fingerprint]\nenabled=true\nunknown=1', 'broken ['])
def test_switch_missing_or_invalid_leaves_pipeline_identical(h,text):
    path = h.settings.data_dir/'mind.toml'
    path.write_text(text)
    runner = NightRunner(STAGES,lambda n,w:h.ctx())
    attach(runner,h.settings.data_dir)
    assert not enabled(path) and runner.stages == STAGES


def test_switch_attaches_to_this_runner_only_and_weekly_report_is_scoped(h):
    from leeroy_mind.eval import _context_sections
    path = h.settings.data_dir/'mind.toml'
    path.write_text('[fingerprint]\nenabled=true')
    runner = NightRunner(STAGES,lambda n,w:h.ctx())
    attach(runner,h.settings.data_dir)
    names = [s.name for s in runner.stages]
    assert names[names.index('threads')+1] == 'fingerprint'
    assert next(s for s in runner.stages if s.name=='fingerprint').model is None
    assert all(s.name!='fingerprint' for s in STAGES)
    assert 'voice_fingerprint' in _context_sections(h.ctx())
    path.write_text('[fingerprint]\nenabled=false')
    assert 'voice_fingerprint' not in _context_sections(h.ctx())


def test_application_attachment_and_weekly_artifact(h):
    from leeroy_mind.app import MindApp
    from leeroy_mind.config import load_config
    from leeroy_mind.eval import run_weekly
    from leeroy_mind.testing.fake_llm import FakeLLM
    root = h.settings.data_dir
    (root/'mind.toml').write_text('[fingerprint]\nenabled=true')
    app = MindApp(load_config(root/'mind.toml'),root,llm=FakeLLM(),clock=h.clock)
    try:
        assert any(s.name=='fingerprint' and s.model is None for s in app.night_runner.stages)
        assert not app._bg_tasks
    finally:
        run(app.stop())
    ctx = h.ctx()
    run(stage_fingerprint(ctx))
    run(run_weekly(ctx,'2026-W44',use_llm=False))
    artifacts = list(root.rglob('2026-W44.json'))
    assert len(artifacts)==1
    report = json.loads(artifacts[0].read_text())
    assert report['voice_fingerprint']['available']==5
    assert report['voice_fingerprint']['seeded']==5
    assert report['voice_fingerprint']['mean_pairwise_distance'] is not None


def test_seed_age_floor_and_malformed_optional_traits():
    assert seed({'identity':{'age':18}}) == seed({'identity':{'age':25}})
    p = seed({'identity':None,'humour':[],'education':{}},talkative=float('nan'))
    assert p.p50 == 11


def test_older_database_api_and_cli_are_read_only(tmp_path,capsys):
    from leeroy_mind.__main__ import main
    path = tmp_path/'mind.db'
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE persona_core(guid INTEGER,json TEXT)')
        conn.execute('INSERT INTO persona_core VALUES(101,?)', (json.dumps({'identity':{'age':35,'region':'UK'}}),))
    before = path.read_bytes()
    conn = sqlite3.connect(f'{path.as_uri()}?mode=ro',uri=True)
    try:
        assert fingerprint(conn,101).seeded == 1
        assert fingerprint(conn,102).available == 0
    finally:
        conn.close()
    assert main(['fingerprint','show','101','--data-dir',str(tmp_path)]) == 0
    body = json.loads(capsys.readouterr().out)
    assert body['profile']['seeded'] == 1
    assert path.read_bytes() == before
    assert not (tmp_path/'logs').exists()
    assert main(['fingerprint','show','-1','--data-dir',str(tmp_path)]) == 1


@pytest.mark.parametrize('raw', ['{}','[]','{"version":99}', '"private canary"'])
def test_corrupt_saved_profile_falls_back_without_repair_or_logging(h,raw,caplog):
    h.persona(101)
    with h.store.transaction():
        h.store.conn.execute('INSERT INTO voice_fingerprint VALUES(?,?,?,?)',(101,raw,1,h.T))
    p = fingerprint(h.store.conn,101)
    assert p.seeded and p.available
    assert h.store.conn.execute('SELECT profile FROM voice_fingerprint').fetchone()[0] == raw
    assert not caplog.text
