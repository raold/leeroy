"""The weekly cycle on invented members only (101-103, owner 1); no model and no live data."""
import asyncio
from datetime import datetime, timezone
import itertools
import json
import logging
import random

import pytest

from tests.claims.conftest import context  # noqa: F401 (fixture)
from leeroy_mind.app import acquire_instance_lock
from leeroy_mind.learn import arms, bandit, control, cycle, night, weights
from leeroy_mind.learn.cli import learn_main
from leeroy_mind.learn.config import LearnCfg, load_learn_config
from leeroy_mind.nightly.pipeline import STAGES
from leeroy_mind.nightly.runner import NightRunner
from leeroy_mind.protocol import Chat
from leeroy_mind.testing.fake_clock import FakeClock

UTC = timezone.utc
WEEK, LAST = '2026-W44', '2026-W43'
START, END = cycle.week_bounds(WEEK, UTC)
LAST_START = START - 7 * 86400
NOW = END + 21 * 3600  # Tuesday 03:00, during the Monday night's run
ON = LearnCfg(enabled=True, bandit=True, cycle=True)
KEY = bandit.Context('guild').key
ARM, OTHER = 'length.1.v1', 'questions.1.v1'
CONTROL = arms.controls()['length']
_words = itertools.count(1)


@pytest.fixture
def clock():
    return FakeClock(datetime(2026, 11, 3, 3, 0, tzinfo=UTC))


def fresh():
    n = next(_words)
    return ' '.join(f'w{n}x{k}' for k in range(6))


def gen(store, ts, *, arm=None, lesson=None, guid=101, channel='guild', verdict='send', text=None,
        value=None, key=KEY):
    lid = store.insert_chat_line(Chat(0, channel, guid, 'Invented', 0, text or fresh(), 0, 10),
                                 session_id=None, origin='mind', ts=ts)
    ids = [arm] if arm else []
    with store.transaction():
        arms.ensure(store.conn, now=ts)
        store.conn.execute('INSERT INTO talk_recipe VALUES (?,?,?,?,?,?,?,?,?)',
                           (lid, 'test', '0' * 16, '0' * 64, 'fake', verdict, json.dumps(ids),
                            json.dumps([lesson] if lesson else []), key if ids else None))
        store.conn.execute('INSERT INTO talk_recipe_line VALUES (?,?)', (lid, lid))
        if value is not None:
            version = weights.ensure(store.conn, now=ts).version
            store.conn.execute('INSERT INTO talk_reward(line_id,guid,value,cap_weight,parts,weights_version,'
                               'scored_at) VALUES (?,?,?,1,?,?,?)', (lid, guid, value, '{}', version, ts))
    return lid


def credit(store, lid, aid, ts, *, success, failure, bad=0, key=KEY, guid=101):
    with store.transaction():
        store.conn.execute('INSERT INTO bandit_credit VALUES (?,?,?,?,?,?,?,?,?)',
                           (lid, aid, guid, key, 0, ts, success, failure, bad))


def winning(store, arm=ARM, *, n=30, start=START + 3600, text=None, key=KEY):
    """An arm that clearly beats its family control in the evaluated week."""
    control_id = arms.controls()[arm.split('.')[0]]
    for i in range(n):
        ts = start + i * 600
        credit(store, gen(store, ts, arm=arm, value=1., text=text(i) if text else None, key=key), arm, ts,
               success=1., failure=0., key=key)
        credit(store, gen(store, ts + 60, arm=control_id, value=0., key=key), control_id, ts + 60,
               success=.5, failure=.5, key=key)


def lesson(store, *, guid=101, status='active', expires=NOW + 86400, text='She keeps replies brief.'):
    with store.transaction():
        return store.conn.execute('INSERT INTO talk_lesson(guid,scope,text,status,expires,origin_night) '
                                  "VALUES (?,'guild',?,?,?,'2026-10-30')", (guid, text, status, expires)).lastrowid


def promote(store, kind, ident, key, *, at, week=LAST):
    with store.transaction():
        store.conn.execute("INSERT INTO learn_promotion VALUES (?,?,?,'promoted',?,?,?)",
                           (kind, ident, key, week, at, at))


def evaluate(store, cfg=ON, now=NOW):
    return cycle.evaluate(store.conn, week=WEEK, cfg=cfg, now=now, tz=UTC, owner=1)


def run(store, cfg=ON, now=NOW):
    plan = evaluate(store, cfg, now)
    with store.transaction():
        return cycle._apply(store.conn, plan, now=now, settings=cfg, posteriors=plan['posteriors'])


def tally(lines=1, kinds=(), trips=0):
    t = cycle.Tally()
    for i in range(lines):
        t.add(set(kinds) if i == 0 else set(), anchor=i, verdict='rewrite' if i < trips else 'send')
    return t


# -- timing, the posterior and the gates at their boundaries --------------------------------------

def test_the_cycle_is_due_after_the_sunday_evaluation_and_never_on_sunday():
    assert cycle.due_week('2026-11-02') == WEEK  # Monday: the week that just ended
    assert cycle.due_week('2026-11-07') == WEEK  # later nights of the week retry it
    assert cycle.due_week('2026-11-01') is None  # Sunday is the weekly evaluation's night
    assert cycle.previous_week(WEEK) == LAST and cycle.previous_week('2026-W01') == '2025-W52'


def test_posterior_probability_is_deterministic_and_ordered():
    assert cycle.beats((1, 1), (1, 1)) == pytest.approx(.5, abs=1e-3)
    assert cycle.beats((31, 1), (16, 16)) > .99
    assert cycle.beats((16, 16), (31, 1)) < .01
    assert cycle.beats((20, 10), (10, 20)) == cycle.beats((20, 10), (10, 20))


@pytest.mark.parametrize('over,reasons', [
    ({}, []),
    (dict(uses=29), ['uses']),
    (dict(probability=.9), ['probability']),
    (dict(probability=.9000001), []),
    (dict(carrier=tally(0)), ['no_week_lines']),
    (dict(comparison=tally(0)), ['no_control_lines']),
    (dict(carrier=tally(2, {'stock'})), ['detectors']),
    (dict(carrier=tally(2, {'stock'}), comparison=tally(2, {'stock'})), []),
    (dict(carrier=tally(2, trips=1)), ['guard']),
    (dict(guard_worse=True), ['guard']),
    (dict(vetoed=True), ['veto']),
    (dict(rolled_back=True), ['rolled_back']),
    (dict(rose=['repetition']), ['diversity']),
])
def test_promotion_gates_at_their_boundaries(over, reasons):
    args = dict(uses=30, probability=.95, carrier=tally(2), comparison=tally(2)) | over
    assert cycle.gate(**args) == reasons


# -- promotion, the veto and freezes --------------------------------------------------------------

def test_a_clear_winner_is_promoted_in_its_context_and_reported_without_text(store):
    winning(store)
    value = run(store)
    assert [(p['kind'], p['id'], p['context']) for p in value['promoted']] == [('arm', ARM, json.loads(KEY))]
    assert cycle.promoted_arms(store.conn, KEY) == {ARM}
    assert value['promoted'][0]['probability'] > .9 and value['promoted'][0]['uses'] == 30
    stored = store.get_meta(control.REPORT_KEY) + store.conn.execute('SELECT state FROM learn_version').fetchone()[0]
    for text in (r[0] for r in store.conn.execute("SELECT text FROM chat_line")):
        assert text not in stored


def test_29_rewarded_uses_wait(store):
    winning(store, n=29)
    plan = evaluate(store)
    assert not [p for p in plan['promote'] if p['kind'] == 'arm']
    assert 'uses' in next(h for h in plan['held'] if h['id'] == ARM)['reasons']


def test_worse_detector_rates_hold_a_winner(store):
    winning(store, text=lambda i: 'same old opening ' + fresh())  # a stock opening on every arm line
    held = next(h for h in evaluate(store)['held'] if h['id'] == ARM)
    assert 'detectors' in held['reasons']


def test_the_owner_veto_is_honoured(store):
    winning(store)
    store.set_meta(control.PREFIX + 'veto:arm:' + ARM, str(NOW))
    plan = evaluate(store)
    assert 'veto' in next(h for h in plan['held'] if h['id'] == ARM)['reasons']
    assert not run(store)['promoted'] and not cycle.promoted_arms(store.conn, KEY)


def test_a_vetoed_lesson_is_never_promoted(store):
    lid = lesson(store)
    for i in range(30):
        gen(store, START + 3600 + i * 600, lesson=lid, value=1.)
        gen(store, START + 3900 + i * 600, value=0.)
    plan = evaluate(store)
    assert [p['id'] for p in plan['promote'] if p['kind'] == 'lesson'] == [lid]
    store.set_meta(control.PREFIX + f'veto:lesson:{lid}', str(NOW))
    assert not [p for p in run(store)['promoted'] if p['kind'] == 'lesson']


def test_a_promoted_lesson_leads_the_live_style_notes(store):
    first, second = lesson(store, text='She keeps replies brief.'), lesson(store, text='She asks one question.')
    promote(store, 'lesson', str(second), '', at=NOW)
    notes = [{'id': first}, {'id': second}]
    assert [n['id'] for n in cycle.promoted_first(store.conn, notes)] == [second, first]


def test_frozen_family_and_member_are_never_promoted(store):
    winning(store)
    lid = lesson(store)
    for i in range(30):
        gen(store, START + 3600 + i * 600, lesson=lid, value=1.)
        gen(store, START + 3900 + i * 600, value=0.)
    plan = evaluate(store, LearnCfg(enabled=True, bandit=True, cycle=True, frozen_families=('length',),
                                    frozen_members=(101,)))
    assert not plan['promote'] and all(h['id'] != ARM for h in plan['held'])


def test_global_freeze_runs_nothing(context, store, tmp_path, caplog):
    (tmp_path / 'mind.toml').write_text('[learn]\nenabled=true\nbandit=true\ncycle=true\n')
    winning(store)
    store.set_meta(control.PREFIX + 'freeze', 'true')

    async def go():
        async with context(night='2026-11-02') as (ctx, _):
            with caplog.at_level(logging.INFO):
                await cycle.stage(ctx)
    asyncio.run(go())
    assert not store.conn.execute('SELECT count(*) FROM learn_version').fetchone()[0]
    assert not cycle.done(store.conn, WEEK) and store.get_meta(control.REPORT_KEY) is None
    assert 'learn_cycle_frozen' in caplog.text


# -- retirement -----------------------------------------------------------------------------------

@pytest.mark.parametrize('week_controls,last_controls,retired', [(24, 24, True), (19, 24, False),
                                                                 (24, 15, False)])
def test_share_under_five_percent_for_two_weeks_retires_a_measured_arm(store, week_controls, last_controls, retired):
    for i in range(30):  # long-measured: 30 rewarded uses before both weeks
        ts = LAST_START - 86400 - i * 60
        credit(store, gen(store, ts, arm=ARM, value=0.), ARM, ts, success=.5, failure=.5)
    for begin, controls in ((START, week_controls), (LAST_START, last_controls)):
        gen(store, begin + 3600, arm=ARM)
        for i in range(controls):
            gen(store, begin + 7200 + i * 60, arm=CONTROL)
    value = run(store)
    assert any(r['id'] == ARM and r['reason'] == 'share' for r in value['retired']) is retired
    status = store.conn.execute('SELECT status FROM learn_arm WHERE id=?', (ARM,)).fetchone()[0]
    assert status == ('retired' if retired else 'active')


def test_contexts_where_an_arm_was_never_measured_do_not_dilute_its_share(store):
    for i in range(30):
        ts = LAST_START - 86400 - i * 60
        credit(store, gen(store, ts, arm=ARM, value=0.), ARM, ts, success=.5, failure=.5)
    discord = bandit.Context('discord:general').key
    for begin in (START, LAST_START):
        gen(store, begin + 3600, arm=ARM)
        for i in range(19):
            gen(store, begin + 7200 + i * 60, arm=CONTROL)
        for i in range(200):  # Discord turns never offer a length arm: forced control
            gen(store, begin + 9000 + i * 30, arm=CONTROL, channel='discord:general', key=discord)
    plan = evaluate(store)
    assert not plan['retire_arms']
    assert next(a for a in plan['arms'] if a['id'] == ARM)['share'] == .05


def test_a_young_arm_with_a_small_share_stays_on_trial(store):
    for begin in (START, LAST_START):
        gen(store, begin + 3600, arm=ARM)
        for i in range(30):
            gen(store, begin + 7200 + i * 60, arm=CONTROL)
    assert not run(store)['retired']


def test_a_lesson_not_reinforced_in_14_days_is_retired_with_its_promotion(store):
    lapsed = lesson(store, expires=START + 2 * 86400)
    promote(store, 'lesson', str(lapsed), '', at=LAST_START)
    value = run(store)
    assert {'kind': 'lesson', 'id': lapsed, 'guid': 101, 'reason': 'not_reinforced'} in value['retired']
    assert store.conn.execute("SELECT status FROM learn_promotion WHERE ident=?", (str(lapsed),)).fetchone()[0] == 'retired'


# -- the diversity floor --------------------------------------------------------------------------

def test_rising_repetition_rolls_back_only_the_newest_correlated_promotions(store):
    for begin, repeat in ((LAST_START, False), (START, True)):
        for day in range(4):
            ts = begin + 3600 + day * 86400
            text = 'the very same words about the boss tonight' if repeat else None
            gen(store, ts, arm=ARM, text=text)
            gen(store, ts + 60, arm=OTHER, text='another repeated line about the dungeon run' if repeat else None)
            gen(store, ts + 120, arm=CONTROL)
            gen(store, ts + 180, arm=arms.controls()['questions'])
    clean = lesson(store)
    promote(store, 'arm', ARM, KEY, at=NOW - 86400, week=LAST)        # newest, correlated
    promote(store, 'arm', OTHER, KEY, at=NOW - 8 * 86400, week='2026-W42')  # older, correlated
    promote(store, 'lesson', str(clean), '', at=NOW - 86400, week=LAST)    # newest, not correlated
    winning(store, 'humour.1.v1', start=START + 5 * 86400)
    plan = evaluate(store)
    assert plan['rose'] == ['repetition']
    assert 'diversity' in next(h for h in plan['held'] if h['id'] == 'humour.1.v1')['reasons']
    value = run(store)
    assert [(r['kind'], r['id']) for r in value['rolled_back']] == [('arm', ARM)]
    assert value['diversity'] == {'ok': 0, 'repetition_rose': 1, 'stock_rose': 0}
    states = dict(store.conn.execute('SELECT ident,status FROM learn_promotion'))
    assert states == {ARM: 'rolled_back', OTHER: 'promoted', str(clean): 'promoted'}
    assert not value['promoted']
    # A rolled-back promotion is never promoted again by the cycle.
    assert cycle.gate(uses=30, probability=1, carrier=tally(), comparison=tally(), rolled_back=True)


def test_a_frozen_family_is_never_rolled_back(store):
    for begin, repeat in ((LAST_START, False), (START, True)):
        for day in range(4):
            gen(store, begin + 3600 + day * 86400, arm=ARM,
                text='the very same words about the boss tonight' if repeat else None)
            gen(store, begin + 3700 + day * 86400, arm=CONTROL)
    promote(store, 'arm', ARM, KEY, at=NOW - 86400)
    value = run(store, LearnCfg(enabled=True, bandit=True, cycle=True, frozen_families=('length',)))
    assert not value['rolled_back'] and value['frozen'] == {'families': ['length'], 'members': 0}


# -- the report, Discord and group DMs ------------------------------------------------------------

def test_report_contents_numbers_and_ids_with_every_medium_counted(store):
    private = 'Invented group DM secret about the raid'
    for channel in ('guild', 'whisper', 'dm:101', 'gdm:7', 'discord:general', 'officer'):
        store.insert_chat_line(Chat(0, channel, 101, 'Invented', 0, f'{private} {channel} ' + fresh(), 0, 0),
                               session_id=None, origin='mind', ts=START + 7200)
    store.insert_chat_line(Chat(0, 'guild', 101, 'Invented', 0, fresh(), 0, 0), session_id=None,
                           origin='mind', ts=LAST_START + 7200)
    value = run(store)
    assert value['week'] == WEEK and value['previous_week'] == LAST
    assert value['rates']['week']['lines'] == 6 and value['rates']['previous']['lines'] == 1
    assert set(value['rates']['week']) == {'lines', 'flagged', *cycle.DETECTORS, 'guard', 'guard_screened'}
    assert value['measured']['contradiction'] == 0 and value['measured']['repetition'] == 1
    assert value['state_version'] == store.conn.execute('SELECT max(version) FROM learn_version').fetchone()[0]
    assert set(value) >= {'promoted', 'retired', 'rolled_back', 'held', 'arms', 'lessons', 'diversity'}
    stored = store.get_meta(control.REPORT_KEY) + store.conn.execute('SELECT state FROM learn_version').fetchone()[0]
    assert private not in stored and 'Invented' not in stored

    def leaves(node):
        if isinstance(node, dict):
            for item in node.values():
                yield from leaves(item)
        elif isinstance(node, list):
            for item in node:
                yield from leaves(item)
        else:
            yield node
    assert all(type(v) in (int, float) or v is None or isinstance(v, str) and len(v) <= 100
               and ' ' not in v for v in leaves(json.loads(store.get_meta(control.REPORT_KEY))))


# -- rollback ---------------------------------------------------------------------------------------

def changed_state(store, now):
    with store.transaction():
        store.conn.execute("UPDATE learn_arm SET status='retired' WHERE id='humour.1.v1'")
        store.conn.execute("UPDATE talk_lesson SET status='active',reinforced=3,expires=? WHERE status='proposed'",
                           (now + 9 * 86400,))
        store.conn.execute('DELETE FROM learn_promotion')
        store.conn.execute("INSERT INTO learn_promotion VALUES ('arm',?,?,'promoted',?,?,?)", (OTHER, KEY, WEEK, now, now))
        weights.change(store.conn, dict(explicit=.5, engagement=.2, emote=.1, quality=.2), now=now)
        proposal = arms.propose(store.conn, 'questions', {'rate': .3}, now=now)
        store.conn.execute("UPDATE learn_arm SET status='active' WHERE id=?", (proposal,))
    lesson(store, text='A newer note about pacing.', expires=NOW + 5 * 86400)
    return proposal


def test_rollback_restores_the_state_exactly_and_can_itself_be_undone(store):
    t0 = START + 86400
    with store.transaction():
        arms.ensure(store.conn, now=t0)
        weights.ensure(store.conn, now=t0)
    lesson(store, status='proposed', expires=NOW + 3 * 86400)
    lesson(store, text='She jokes about wipes.', expires=NOW + 4 * 86400)
    promote(store, 'arm', ARM, KEY, at=t0)
    lid = gen(store, t0, arm=ARM, value=1.)
    credit(store, lid, ARM, t0, success=1., failure=0.)
    with store.transaction():
        version = cycle.save_version(store.conn, cycle.state(store.conn, now=t0), reason='cycle', now=t0, week=WEEK)
    target = json.loads(store.conn.execute('SELECT state FROM learn_version WHERE version=?', (version,)).fetchone()[0])
    proposal = changed_state(store, t0 + 3600)
    before = cycle.governed(cycle.state(store.conn, now=NOW))
    evidence = store.conn.execute('SELECT * FROM bandit_credit').fetchall()
    with store.transaction():
        restored, saved = cycle.rollback(store.conn, now=NOW)
    assert restored == version
    after = cycle.governed(cycle.state(store.conn, now=NOW))
    wanted = cycle.governed(target)
    assert {k: after['arms'][k] for k in wanted['arms']} == wanted['arms']
    assert after['arms'][proposal] == 'retired'  # not live in that state
    assert after['lessons'] == wanted['lessons'] and after['promotions'] == wanted['promotions']
    assert after['weights'] == wanted['weights'] == dict(weights.DEFAULTS)
    assert store.conn.execute('SELECT * FROM bandit_credit').fetchall() == evidence  # evidence untouched
    with store.transaction():
        cycle.rollback(store.conn, saved, now=NOW)
    assert cycle.governed(cycle.state(store.conn, now=NOW)) == before


def test_rollback_keeps_the_owners_vetoes(store):
    t0 = START + 86400
    with store.transaction():
        arms.ensure(store.conn, now=t0)
        weights.ensure(store.conn, now=t0)
    lid = lesson(store, expires=NOW + 86400)
    with store.transaction():
        cycle.save_version(store.conn, cycle.state(store.conn, now=t0), reason='cycle', now=t0, week=WEEK)
    control.change(store, {'action': 'arm', 'id': ARM, 'verb': 'retire'}, now=t0 + 60)
    control.change(store, {'action': 'lesson', 'id': lid, 'verb': 'veto'}, now=t0 + 60)
    reply = control.change(store, {'action': 'rollback', 'version': None}, now=NOW)
    assert reply['ok'] and 'vetoes' in reply['message']
    assert store.conn.execute('SELECT status FROM learn_arm WHERE id=?', (ARM,)).fetchone()[0] == 'retired'
    assert store.conn.execute('SELECT expires FROM talk_lesson WHERE id=?', (lid,)).fetchone()[0] == t0 + 60


@pytest.mark.parametrize('args', [{'action': 'rollback'}, {'action': 'rollback', 'version': 0},
                                  {'action': 'rollback', 'version': '3'}, {'action': 'rollback', 'version': 1, 'x': 1}])
def test_the_lab_rollback_action_is_strict(args):
    with pytest.raises(ValueError):
        control.arguments(args)


def test_a_missing_version_is_refused_in_words(store):
    with pytest.raises(ValueError, match='No weekly'):
        control.change(store, {'action': 'rollback', 'version': None}, now=NOW)
    with pytest.raises(ValueError, match='not saved'):
        control.change(store, {'action': 'rollback', 'version': 99}, now=NOW)


def test_cli_rollback_holds_the_service_lock_and_lists_versions(store, tmp_path, capsys):
    with store.transaction():
        arms.ensure(store.conn, now=START)
        weights.ensure(store.conn, now=START)
        version = cycle.save_version(store.conn, cycle.state(store.conn, now=START), reason='cycle', now=START,
                                     week=WEEK)
        store.conn.execute("UPDATE learn_arm SET status='retired' WHERE id=?", (ARM,))
    lock = acquire_instance_lock(tmp_path)
    try:
        assert learn_main(['rollback', '--data-dir', str(tmp_path)]) == 1
        assert 'mind.lock is held' in capsys.readouterr().out
    finally:
        lock.release()
    assert learn_main(['rollback', '--data-dir', str(tmp_path), '--to', str(version)]) == 0
    assert json.loads(capsys.readouterr().out)['restored'] == version
    assert store.conn.execute('SELECT status FROM learn_arm WHERE id=?', (ARM,)).fetchone()[0] == 'active'
    assert learn_main(['versions', '--data-dir', str(tmp_path)]) == 0
    listed = json.loads(capsys.readouterr().out)['versions']
    assert [v['reason'] for v in listed] == ['rollback', 'cycle'] and listed[0]['restored_from'] == version


# -- the stage, its switch and the live consumers ----------------------------------------------------

def stage_names(tmp_path, store, text):
    (tmp_path / 'mind.toml').write_text(text)
    runner = NightRunner(STAGES, lambda *_: None)
    night.attach(runner, tmp_path, store)
    return [s.name for s in runner.stages]


def test_off_means_no_stage_and_on_runs_after_the_bandit(tmp_path, store):
    assert load_learn_config(tmp_path / 'absent.toml').cycle is False
    assert 'learn_cycle' not in stage_names(tmp_path, store, '[learn]\nenabled=true\nbandit=true\n')
    assert 'learn_cycle' not in stage_names(tmp_path, store, '[learn]\nenabled=false\ncycle=true\n')
    assert 'learn_cycle' not in stage_names(tmp_path, store, '[learn]\nenabled=true\ncycle="yes"\n')
    names = stage_names(tmp_path, store, '[learn]\nenabled=true\nbandit=true\ncoach=true\ncycle=true\n')
    assert names[names.index('rewards'):names.index('rewards') + 4] == ['rewards', 'talk_coach', 'bandit', 'learn_cycle']


def test_stage_runs_once_per_week_and_logs_counts_only(context, store, tmp_path, caplog):
    (tmp_path / 'mind.toml').write_text('[learn]\nenabled=true\nbandit=true\ncycle=true\n')
    winning(store)

    async def go():
        for night_ in ('2026-11-02', '2026-11-03'):
            async with context(night=night_) as (ctx, llm):
                with caplog.at_level(logging.INFO):
                    await cycle.stage(ctx)
                assert not llm.calls
    asyncio.run(go())
    assert store.conn.execute('SELECT count(*) FROM learn_version').fetchone()[0] == 1
    assert cycle.done(store.conn, WEEK) and cycle.report(store.conn)['promoted']
    assert 'learn_cycle_done' in caplog.text and 'w1x' not in caplog.text


def test_with_the_cycle_on_only_a_promoted_arm_leaves_the_trial(store, clock):
    with store.transaction():
        arms.ensure(store.conn, now=clock.now())
    for i in range(30):
        ts = clock.now() - 3600 + i
        credit(store, gen(store, ts, arm=ARM, value=1.), ARM, ts, success=1., failure=0.)

    def picks(cfg):
        rng = random.Random(7)
        with store.transaction():
            store.conn.execute('DELETE FROM bandit_turn')
            return [bandit.choose(store.conn, 101, bandit.Context('guild', settings=cfg), rng=rng,
                                  now=clock.now())['length'] for _ in range(100)].count(ARM)
    assert picks(LearnCfg(enabled=True, bandit=True)) > 50  # cycle off: unchanged
    assert picks(ON) <= 20  # on, unpromoted: exploration only
    promote(store, 'arm', ARM, KEY, at=clock.now())
    assert picks(ON) > 50


def test_coach_receives_last_weeks_report_only_with_the_cycle_on(context, store, tmp_path):
    from tests.learn.test_coach import answer, exchanges
    from leeroy_mind.learn import coach
    with store.transaction():
        arms.ensure(store.conn, now=NOW)
    first = lesson(store, text='She keeps replies brief.')
    report = dict(version=1, week=WEEK, rates=dict(week=dict(repetition=.1, guard=.2), previous=dict(repetition=.05)),
                  diversity=dict(ok=1), promoted=[dict(kind='arm', id=ARM, context=json.loads(KEY)),
                                                  dict(kind='lesson', id=first, guid=101)],
                  retired=[dict(kind='arm', id='humour.2.v1', reason='share'), dict(kind='lesson', id=999, guid=102)],
                  rolled_back=[])
    store.set_meta(control.REPORT_KEY, json.dumps(report))
    seen = {}
    for name, toml in (('off', 'enabled=true\ncoach=true\n'), ('on', 'enabled=true\ncoach=true\ncycle=true\n')):
        (tmp_path / 'mind.toml').write_text('[learn]\n' + toml)

        async def go():
            async with context(lambda *_: json.dumps(answer([])), learn_enabled=True, learn_coach=True,
                               night='2026-11-02' if name == 'off' else '2026-11-04') as (ctx, llm):
                if name == 'off':
                    exchanges(ctx)
                await coach.stage(ctx)
                seen[name] = json.dumps(llm.calls)
        asyncio.run(go())
    assert 'weekly_cycle' not in seen['off']
    assert 'weekly_cycle' in seen['on']
    weekly = cycle.coach_input(store.conn, 101)
    assert weekly['promoted_arms'] == [{'family': 'length', 'params': {'min_words': 8, 'max_words': 30}}]
    assert weekly['retired_arms'] == [{'family': 'humour', 'params': {'level': 'playful'}}]
    assert weekly['lessons'] == {'promoted': [first], 'retired': [], 'rolled_back': []}
    assert weekly['rates']['repetition'] == {'week': .1, 'previous': .05}
    assert 'promoted_arms' in seen['on'] and 'She keeps replies brief' not in json.dumps(weekly)
