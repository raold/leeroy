"""The weekly cycle on invented members only (101-103, owner 1); no model and no live data."""
import asyncio
from dataclasses import replace
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


def gen(store, ts, *, arm=None, ids=None, lesson=None, guid=101, channel='guild', verdict='send', text=None,
        value=None, key=KEY):
    lid = store.insert_chat_line(Chat(0, channel, guid, 'Invented', 0, text or fresh(), 0, 10),
                                 session_id=None, origin='mind', ts=ts)
    ids = list(ids) if ids else [arm] if arm else []
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


def winning(store, arm=ARM, *, n=30, start=START + 3600, text=None, key=KEY, guid=101):
    """An arm whose whole per-line rewards clearly beat its family control's in the week."""
    control_id = arms.controls()[arm.split('.')[0]]
    for i in range(n):
        ts = start + i * 600
        gen(store, ts, arm=arm, value=1., text=text(i) if text else None, key=key, guid=guid)
        gen(store, ts + 60, arm=control_id, value=0., key=key, guid=guid)


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


def status(store, aid):
    return store.conn.execute('SELECT status FROM learn_arm WHERE id=?', (aid,)).fetchone()[0]


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
    (dict(held='rolled_back'), ['rolled_back']),
    (dict(rose=[('game', 'repetition')]), ['diversity']),
])
def test_promotion_gates_at_their_boundaries(over, reasons):
    args = dict(uses=30, probability=.95, carrier=tally(2), comparison=tally(2)) | over
    assert cycle.gate(**args) == reasons


@pytest.mark.parametrize('before,after,rose', [
    ((0, 1000), (9, 1000), False),        # nine more flagged lines: under ten
    ((0, 1000), (10, 1000), True),        # ten more, +1.0 point, significant
    ((100, 1000), (110, 1000), False),    # ten more and +1.0 point, but noise at this level
    ((0, 10000), (10, 10000), False),     # ten more, significant, but only +0.1 point
    ((1, 1602), (13, 1723), True),        # the live W45 -> W46 repetition counts
    ((0, 0), (20, 100), False),           # nothing measured the week before
])
def test_a_rise_needs_significance_half_a_point_and_ten_lines(before, after, rose):
    assert cycle.significant_rise(before[0], before[1], after[0], after[1]) is rose


# -- promotion on whole rewards, the veto and freezes ----------------------------------------------

def test_a_clear_winner_is_promoted_in_its_context_and_reported_without_text(store):
    winning(store)
    value = run(store)
    assert [(p['kind'], p['id'], p['context']) for p in value['promoted']] == [('arm', ARM, json.loads(KEY))]
    assert cycle.promoted(store.conn) == {('arm', ARM, KEY)}
    assert value['promoted'][0]['probability'] > .9 and value['promoted'][0]['uses'] == 30
    stored = store.get_meta(control.REPORT_KEY) + store.conn.execute('SELECT state FROM learn_version').fetchone()[0]
    for text in (r[0] for r in store.conn.execute("SELECT text FROM chat_line")):
        assert text not in stored


def test_live_like_seven_family_rewards_reach_promotion_without_touching_the_bandit(store):
    """Seven ids per recipe and mostly-zero rewards (max 0.7), credited through bandit.update:
    the bandit's split credit (1/7 a line) could never pass 0.9; whole rewards can."""
    others = [arms.controls()[f] for f in arms.FAMILIES if f != 'length']
    pattern, control_pattern = (0., 0., 0., .5, .7), (0.,) * 9 + (.3,)
    lines = []
    for i in range(100):
        lines.append(gen(store, START + 3600 + i * 1800, ids=[ARM, *others], value=pattern[i % 5]))
    for i in range(200):
        lines.append(gen(store, START + 4500 + i * 900, ids=list(arms.controls().values()),
                         value=control_pattern[i % 10]))
    with store.transaction():
        for lid in lines:
            bandit.update(store.conn, lid, settings=ON, now=NOW)
    split = bandit.posterior(store.conn, None, KEY, now=NOW)
    assert cycle.beats((split[ARM]['alpha'], split[ARM]['beta']),
                       (split[CONTROL]['alpha'], split[CONTROL]['beta'])) < .9
    plan = evaluate(store)
    promoted = [p for p in plan['promote'] if p['id'] == ARM]
    assert promoted and promoted[0]['uses'] == 100 and promoted[0]['probability'] > .9
    run(store)
    assert cycle.promoted(store.conn) == {('arm', ARM, KEY)}

    # Promotion is a status only: the bandit samples exactly as it does with the cycle off.
    def picks(cfg):
        rng = random.Random(11)
        with store.transaction():
            store.conn.execute('DELETE FROM bandit_turn')
            store.conn.execute('DELETE FROM bandit_share')
            return [bandit.choose(store.conn, 101, bandit.Context('guild', settings=cfg), rng=rng, now=NOW)
                    for _ in range(60)]
    assert picks(ON) == picks(replace(ON, cycle=False))


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
    assert not run(store)['promoted'] and not cycle.promoted(store.conn)


def lesson_week(store, lid, *, verdict='send', guid=101):
    for i in range(30):
        gen(store, START + 3600 + i * 600, lesson=lid, value=1., verdict=verdict, guid=guid)
        gen(store, START + 3900 + i * 600, value=0., guid=guid)


def test_a_vetoed_lesson_is_never_promoted(store):
    lid = lesson(store)
    lesson_week(store, lid)
    plan = evaluate(store)
    assert [p['id'] for p in plan['promote'] if p['kind'] == 'lesson'] == [lid]
    store.set_meta(control.PREFIX + f'veto:lesson:{lid}', str(NOW))
    assert not [p for p in run(store)['promoted'] if p['kind'] == 'lesson']


def test_a_lesson_respects_its_own_guard_trips(store):
    lid = lesson(store)
    lesson_week(store, lid, verdict='rewrite')
    held = next(h for h in evaluate(store)['held'] if h['kind'] == 'lesson')
    assert 'guard' in held['reasons']


def test_a_promoted_lesson_leads_the_live_style_notes(store):
    first, second = lesson(store, text='She keeps replies brief.'), lesson(store, text='She asks one question.')
    promote(store, 'lesson', str(second), '', at=NOW)
    notes = [{'id': first}, {'id': second}]
    assert [n['id'] for n in cycle.promoted_first(store.conn, notes)] == [second, first]


def test_frozen_family_and_member_are_never_promoted(store):
    winning(store)
    lid = lesson(store)
    lesson_week(store, lid)
    plan = evaluate(store, replace(ON, frozen_families=('length',), frozen_members=(101,)))
    assert not plan['promote'] and all(h['id'] != ARM for h in plan['held'])


def test_a_frozen_members_lines_do_not_count_toward_an_arms_gate(store):
    winning(store, n=20)
    winning(store, n=20, start=START + 2 * 86400, guid=102)
    assert [p['id'] for p in evaluate(store)['promote']] == [ARM]
    held = next(h for h in evaluate(store, replace(ON, frozen_members=(102,)))['held'] if h['id'] == ARM)
    assert held['uses'] == 20 and 'uses' in held['reasons']


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

def measured_arm(store, value, control_value=0.):
    """Thirty rewarded uses before both weeks, against controls with their own rewards."""
    for i in range(30):
        ts = LAST_START - 86400 - i * 60
        gen(store, ts, arm=ARM, value=value)
        gen(store, ts - 30, arm=CONTROL, value=control_value)


def small_shares(store, week_controls=24, last_controls=24):
    for begin, controls in ((START, week_controls), (LAST_START, last_controls)):
        gen(store, begin + 3600, arm=ARM)
        for i in range(controls):
            gen(store, begin + 7200 + i * 60, arm=CONTROL)


@pytest.mark.parametrize('week_controls,last_controls,retired', [(24, 24, True), (19, 24, False),
                                                                 (24, 15, False)])
def test_share_under_five_percent_for_two_weeks_retires_a_measured_loser(store, week_controls, last_controls, retired):
    measured_arm(store, 0., control_value=.3)
    small_shares(store, week_controls, last_controls)
    value = run(store)
    entry = next((r for r in value['retired'] if r['id'] == ARM), None)
    assert (entry is not None) is retired
    if retired:
        assert entry['reason'] == 'share' and entry['probability'] < .5 and entry['uses'] == 30
    assert status(store, ARM) == ('retired' if retired else 'active')


def test_a_small_share_never_retires_an_arm_that_is_not_losing(store):
    measured_arm(store, .3, control_value=0.)
    small_shares(store)
    assert not run(store)['retired'] and status(store, ARM) == 'active'


def test_a_tie_with_control_is_not_a_loser(store):
    measured_arm(store, 0., control_value=0.)  # both all-zero rewards: identical posteriors
    small_shares(store)
    assert not run(store)['retired'] and status(store, ARM) == 'active'


def test_a_promoted_arm_is_never_retired_for_its_share(store):
    measured_arm(store, 0., control_value=.3)
    small_shares(store)
    promote(store, 'arm', ARM, KEY, at=LAST_START)
    assert not run(store)['retired'] and status(store, ARM) == 'active'


def test_contexts_where_an_arm_was_never_used_do_not_dilute_its_share(store):
    measured_arm(store, 0., control_value=.3)
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
    small_shares(store, 30, 30)
    assert not run(store)['retired']


def test_a_retired_arm_can_be_accepted_again_from_the_lab(store):
    measured_arm(store, 0., control_value=.3)
    small_shares(store)
    run(store)
    assert status(store, ARM) == 'retired'
    reply = control.change(store, {'action': 'arm', 'id': ARM, 'verb': 'accept'}, now=NOW)
    assert reply == {'ok': True, 'message': 'Arm accepted again.'} and status(store, ARM) == 'active'
    control.change(store, {'action': 'arm', 'id': ARM, 'verb': 'retire'}, now=NOW + 1)
    control.change(store, {'action': 'arm', 'id': ARM, 'verb': 'accept'}, now=NOW + 2)
    assert status(store, ARM) == 'active' and not control.vetoed(store.conn, 'arm', ARM)


def test_a_lesson_not_reinforced_in_14_days_is_retired_with_its_promotion(store):
    lapsed = lesson(store, expires=START + 2 * 86400)
    promote(store, 'lesson', str(lapsed), '', at=LAST_START)
    value = run(store)
    assert {'kind': 'lesson', 'id': lapsed, 'guid': 101, 'reason': 'not_reinforced'} in value['retired']
    assert store.conn.execute("SELECT status FROM learn_promotion WHERE ident=?", (str(lapsed),)).fetchone()[0] == 'retired'


# -- the diversity floor --------------------------------------------------------------------------

SAME, OTHER_SAME = 'the very same words about the boss tonight', 'another repeated line about the dungeon run'


def repeating_week(store, *, arm_text=SAME, other_text=OTHER_SAME, channel='guild', key=KEY):
    """Two lines a day for seven days per arm; the evaluated week repeats them across days."""
    for begin, repeat in ((LAST_START, False), (START, True)):
        for day in range(7):
            for slot in range(2):
                ts = begin + 3600 + day * 86400 + slot * 600
                gen(store, ts, arm=ARM, text=arm_text if repeat else None, channel=channel, key=key)
                gen(store, ts + 60, arm=OTHER, text=other_text if repeat else None, channel=channel, key=key)
                gen(store, ts + 120, arm=CONTROL, channel=channel, key=key)
                gen(store, ts + 180, arm=arms.controls()['questions'], channel=channel, key=key)


def test_a_significant_rise_rolls_back_only_the_newest_correlated_promotion(store):
    repeating_week(store)
    clean = lesson(store)
    promote(store, 'arm', ARM, KEY, at=NOW - 86400, week=LAST)              # newest, correlated
    promote(store, 'arm', OTHER, KEY, at=NOW - 8 * 86400, week='2026-W42')  # older, correlated
    promote(store, 'lesson', str(clean), '', at=NOW - 86400, week=LAST)     # newest, no lines
    winning(store, 'humour.1.v1', start=START + 5 * 86400)
    plan = evaluate(store)
    assert plan['rose'] == [('game', 'repetition')]
    assert 'diversity' in next(h for h in plan['held'] if h['id'] == 'humour.1.v1')['reasons']
    value = run(store)
    assert [(r['kind'], r['id']) for r in value['rolled_back']] == [('arm', ARM)]
    assert value['diversity'] == {'ok': 0, 'repetition_rose': 1, 'stock_rose': 0, 'rose': ['game:repetition']}
    assert set(value['rates']['mediums']) == {'game', 'discord', 'dm'}
    states = dict(store.conn.execute('SELECT ident,status FROM learn_promotion'))
    assert states == {ARM: 'rolled_back', OTHER: 'promoted', str(clean): 'promoted'}
    assert not value['promoted']
    assert tuple(store.conn.execute('SELECT reason,since FROM learn_hold WHERE ident=?', (ARM,)).fetchone()) == ('diversity', NOW)


def test_a_rise_in_discord_does_not_blame_a_game_promotion(store):
    discord = bandit.Context('discord:general').key
    repeating_week(store, channel='discord:general', key=discord)
    promote(store, 'arm', ARM, KEY, at=NOW - 86400)  # a game context: none of its lines rose
    plan = evaluate(store)
    assert plan['rose'] == [('discord', 'repetition')] and not plan['rollback']


def test_a_diversity_rollback_waits_two_weeks_and_30_new_uses(store):
    winning(store)
    with store.transaction():
        store.conn.execute("INSERT INTO learn_promotion VALUES ('arm',?,?,'rolled_back','2026-W42',0,?)",
                           (ARM, KEY, START - 86400))
        store.conn.execute("INSERT INTO learn_hold VALUES ('arm',?,?,'diversity',?)", (ARM, KEY, START - 86400))
    assert 'rolled_back' in next(h for h in evaluate(store)['held'] if h['id'] == ARM)['reasons']
    with store.transaction():
        store.conn.execute('UPDATE learn_hold SET since=?', (START - 15 * 86400,))
    assert [p['id'] for p in evaluate(store)['promote']] == [ARM]  # 30 uses since, and two weeks
    run(store)
    assert cycle.promoted(store.conn) == {('arm', ARM, KEY)}
    assert not store.conn.execute('SELECT count(*) FROM learn_hold').fetchone()[0]


def test_a_frozen_family_is_never_rolled_back(store):
    repeating_week(store)
    promote(store, 'arm', ARM, KEY, at=NOW - 86400)
    value = run(store, replace(ON, frozen_families=('length',)))
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
    mediums = value['rates']['mediums']
    assert (mediums['game']['week']['lines'], mediums['discord']['week']['lines'], mediums['dm']['week']['lines']) == (3, 2, 1)
    assert set(value['rates']['week']) == {'lines', 'flagged', *cycle.DETECTORS, 'guard', 'guard_screened'}
    assert value['measured'] == {'repetition': 'measured', 'stock': 'measured', 'nonsequitur': 'partial',
                                 'unawareness': 'partial', 'contradiction': 'not_measured', 'guard': 'measured'}
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

def base_state(store, t0):
    with store.transaction():
        arms.ensure(store.conn, now=t0)
        weights.ensure(store.conn, now=t0)


def save(store, at):
    with store.transaction():
        return cycle.save_version(store.conn, cycle.state(store.conn, now=at), reason='cycle', now=at, week=WEEK)


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
    base_state(store, t0)
    lesson(store, status='proposed', expires=NOW + 3 * 86400)
    lesson(store, text='She jokes about wipes.', expires=NOW + 4 * 86400)
    promote(store, 'arm', ARM, KEY, at=t0)
    gen(store, t0, arm=ARM, value=1.)
    version = save(store, t0)
    target = json.loads(store.conn.execute('SELECT state FROM learn_version WHERE version=?', (version,)).fetchone()[0])
    proposal = changed_state(store, t0 + 3600)
    before = cycle.governed(cycle.state(store.conn, now=NOW))
    evidence = store.conn.execute('SELECT * FROM talk_reward').fetchall()
    with store.transaction():
        done = cycle.rollback(store.conn, now=NOW, with_weights=True)
    assert done['restored'] == version and done['weights'] is True and done['kept'] == 0
    after = cycle.governed(cycle.state(store.conn, now=NOW))
    wanted = cycle.governed(target)
    assert {k: after['arms'][k] for k in wanted['arms']} == wanted['arms']
    assert after['arms'][proposal] == 'proposed'  # not live in that state, still reversible
    assert after['lessons'] == wanted['lessons'] and after['promotions'] == wanted['promotions']
    assert after['weights'] == wanted['weights'] == dict(weights.DEFAULTS)
    assert store.conn.execute('SELECT * FROM talk_reward').fetchall() == evidence  # evidence untouched
    with store.transaction():
        cycle.rollback(store.conn, done['saved'], now=NOW, with_weights=True)
    assert cycle.governed(cycle.state(store.conn, now=NOW)) == before


def test_rollback_keeps_current_weights_unless_asked(store):
    t0 = START + 86400
    base_state(store, t0)
    save(store, t0)
    control.change(store, {'action': 'weights', 'values': dict(explicit=.7, engagement=.1, emote=.1, quality=.1),
                           'version': 1}, now=t0 + 60)
    reply = control.change(store, {'action': 'rollback', 'version': None, 'weights': False}, now=NOW)
    assert 'Reward weights were kept as they are' in reply['message']
    assert weights.read(store.conn, at=NOW).values['explicit'] == .7


def test_rollback_applies_later_guard_and_owner_decisions_again(store):
    t0 = START + 86400
    base_state(store, t0)
    with store.transaction():
        rejected = arms.propose(store.conn, 'questions', {'rate': .4}, now=t0)
    expired = lesson(store, expires=NOW + 86400)
    approved = lesson(store, status='proposed', text='She asks one question.', expires=NOW + 86400)
    save(store, t0)
    with store.transaction():
        bandit.retire(store.conn, [('humour.2.v1', 20, 20, .5, .1)], now=t0 + 10)
    control.change(store, {'action': 'arm', 'id': rejected, 'verb': 'reject'}, now=t0 + 20)
    control.change(store, {'action': 'lesson', 'id': expired, 'verb': 'expire'}, now=t0 + 30)
    with store.transaction():
        later = arms.propose(store.conn, 'questions', {'rate': .25}, now=t0 + 40)
    control.change(store, {'action': 'arm', 'id': later, 'verb': 'accept'}, now=t0 + 50)
    lesson(store, text='A newer note about pacing.', expires=NOW + 86400)
    with store.transaction():
        newest = store.conn.execute('SELECT max(id) FROM talk_lesson').fetchone()[0]
        store.conn.execute("UPDATE talk_lesson SET status='proposed' WHERE id=?", (newest,))
    control.change(store, {'action': 'lesson', 'id': approved, 'verb': 'approve'}, now=t0 + 60)
    control.change(store, {'action': 'lesson', 'id': newest, 'verb': 'approve'}, now=t0 + 70)
    reply = control.change(store, {'action': 'rollback', 'version': None, 'weights': False}, now=NOW)
    assert reply['ok'] and '6 later decisions' in reply['message']
    assert status(store, 'humour.2.v1') == 'retired'  # the guards' retirement stands
    assert status(store, rejected) == 'retired'       # his rejection stands
    assert status(store, later) == 'active'           # his later accept stands
    rows = dict(store.conn.execute('SELECT id,status||":"||(expires>?) FROM talk_lesson', (NOW,)))
    assert rows[expired] == 'active:0' and rows[approved] == 'active:1' and rows[newest] == 'active:1'


def test_rollback_keeps_the_owners_vetoes(store):
    t0 = START + 86400
    base_state(store, t0)
    lid = lesson(store, expires=NOW + 86400)
    save(store, t0)
    control.change(store, {'action': 'arm', 'id': ARM, 'verb': 'retire'}, now=t0 + 60)
    control.change(store, {'action': 'lesson', 'id': lid, 'verb': 'veto'}, now=t0 + 60)
    reply = control.change(store, {'action': 'rollback', 'version': None, 'weights': False}, now=NOW)
    assert reply['ok'] and 'vetoes' in reply['message']
    assert status(store, ARM) == 'retired'
    assert store.conn.execute('SELECT expires FROM talk_lesson WHERE id=?', (lid,)).fetchone()[0] == t0 + 60


def test_an_owner_rollback_is_sticky_until_30_new_uses(store):
    winning(store)
    run(store)
    assert cycle.promoted(store.conn) == {('arm', ARM, KEY)}
    reply = control.change(store, {'action': 'rollback', 'version': None, 'weights': False}, now=NOW + 60)
    assert reply['ok'] and not cycle.promoted(store.conn)
    with store.transaction():
        store.conn.execute('DELETE FROM night_step')
    assert 'owner_rollback' in next(h for h in evaluate(store, now=NOW + 120)['held'] if h['id'] == ARM)['reasons']
    winning(store, start=NOW + 3600)
    later = NOW + 30 * 600 + 7200
    assert [p['id'] for p in cycle.evaluate(store.conn, week=WEEK, cfg=ON, now=later, tz=UTC, owner=1)['promote']] == [ARM]


@pytest.mark.parametrize('args', [{'action': 'rollback'}, {'action': 'rollback', 'version': None},
                                  {'action': 'rollback', 'version': 0, 'weights': False},
                                  {'action': 'rollback', 'version': '3', 'weights': False},
                                  {'action': 'rollback', 'version': 3, 'weights': 'no'},
                                  {'action': 'rollback', 'version': 1, 'weights': False, 'x': 1}])
def test_the_lab_rollback_action_is_strict(args):
    with pytest.raises(ValueError):
        control.arguments(args)


def test_a_missing_version_is_refused_in_words(store):
    with pytest.raises(ValueError, match='No weekly'):
        control.change(store, {'action': 'rollback', 'version': None, 'weights': False}, now=NOW)
    with pytest.raises(ValueError, match='not saved'):
        control.change(store, {'action': 'rollback', 'version': 99, 'weights': False}, now=NOW)


def test_cli_rollback_holds_the_service_lock_and_lists_versions(store, tmp_path, capsys):
    base_state(store, START)
    version = save(store, START)
    with store.transaction():
        store.conn.execute("UPDATE learn_arm SET status='retired' WHERE id=?", (ARM,))
    lock = acquire_instance_lock(tmp_path)
    try:
        assert learn_main(['rollback', '--data-dir', str(tmp_path)]) == 1
        assert 'mind.lock is held' in capsys.readouterr().out
    finally:
        lock.release()
    assert learn_main(['rollback', '--data-dir', str(tmp_path), '--to', str(version)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['restored'] == version and result['weights'] is False
    assert status(store, ARM) == 'active'
    assert learn_main(['rollback', '--data-dir', str(tmp_path), '--to', str(version), '--with-weights']) == 0
    assert json.loads(capsys.readouterr().out)['weights'] is True
    assert learn_main(['versions', '--data-dir', str(tmp_path)]) == 0
    listed = json.loads(capsys.readouterr().out)['versions']
    assert [v['reason'] for v in listed] == ['rollback', 'rollback', 'cycle'] and listed[0]['restored_from'] == version


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
