import asyncio
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timezone
import json
import sqlite3

import pytest

from claims.conftest import context  # noqa: F401 - synthetic fixture, guids 101-103 and owner 1
from leeroy_mind.claims.fake import reply
from leeroy_mind.eval.talk import contradiction, nonsequitur, repetition, unawareness
from leeroy_mind.eval.talk.common import Flag, Line
from leeroy_mind.eval.talk.report import build, compare
from leeroy_mind.storage.rows import pack_vec
from leeroy_mind.testing.fake_llm import fake_embed

NOW = datetime(2026, 10, 29, 23, tzinfo=timezone.utc).timestamp()


def line(key, text, *, guid=101, ts=NOW, **over):
    return Line(key, ts, guid, 0, 1, 'guild', 'mind', 0, 0, text, **over)


def vectors(lines):
    return {item.id: pack_vec(fake_embed(item.text)) for item in lines}


def insert(store, items):
    with store.transaction():
        for item in items:
            store.conn.execute(
                'INSERT INTO chat_line(id,ts,from_guid,from_name,to_guid,channel,origin,map,zone,text) '
                'VALUES (?,?,?,?,?,?,?,?,?,?)',
                (item.id, item.ts, item.guid, 'Invented', item.to_guid, item.channel, item.origin,
                 item.map, item.zone, item.text))


def test_contradiction_replays_before_learning_and_does_not_call_optional_judge(store, context):
    items = [line(1, 'I love Saurfang'), line(2, 'I hate Saurfang', ts=NOW + 1),
             line(3, 'I hate broccoli', ts=NOW + 2), line(4, 'I love Saurfang', guid=102)]
    insert(store, items)

    async def run():
        async with context(reply) as (ctx, fake):
            flags, coverage = await contradiction.detect(ctx, items, vectors(items))
            assert [hit.line_id for hit in flags] == [2]
            assert coverage['checked'] == 4
            assert not any('claims_judge' in json.dumps(call.get('format', {})) for call in fake.calls)
    asyncio.run(run())


def test_contradiction_near_miss_and_clean_subjects(store, context):
    items = [line(1, 'I love tea'), line(2, 'I dislike coffee'), line(3, 'Do I hate tea?')]
    insert(store, items)

    async def run():
        async with context(reply) as (ctx, _fake):
            assert (await contradiction.detect(ctx, items, vectors(items)))[0] == []
    asyncio.run(run())


def test_repetition_across_days_not_same_day_or_another_member(store):
    items = [line(1, 'the moon over the harbour looks beautiful', ts=NOW - 86400),
             line(2, 'the moon over the harbour looks beautiful'),
             line(3, 'the clock above the doorway is broken'),
             line(4, 'the clock above the doorway is broken', ts=NOW + 2),
             line(5, 'the moon over the harbour looks beautiful', guid=102)]
    flags, _ = repetition.detect(store.conn, items, vectors(items))
    assert [(flag.line_id, flag.related_id) for flag in flags if flag.rule == 'member_across_days'] == [(2, 1)]


def test_repetition_embedding_threshold_and_lexical_route_are_independent(store):
    items = [line(1, 'the little boat reached the harbour', ts=NOW - 86400),
             line(2, 'a small vessel arrived at port'), line(3, 'a distant planet circles the sun')]
    vec = vectors(items)
    vec[2] = vec[1]  # Controlled semantic embedding, not a claim about word-hash accuracy.
    flags, _ = repetition.detect(store.conn, items, vec)
    assert [(flag.line_id, flag.rule) for flag in flags] == [(2, 'member_across_days')]


def test_repetition_guild_evening_needs_three_members_and_stock_rate(store):
    items = [line(1, 'keep your boots off the table please', guid=101),
             line(2, 'keep your boots off the table thanks', guid=102),
             line(3, 'keep your boots off the table tonight', guid=103),
             line(4, 'by the way the ferry is late', guid=104),
             line(5, 'by the way the bread is fresh', guid=104),
             line(6, 'by the way the door is open', guid=104),
             line(7, 'the rain stopped at dawn', guid=105)]
    flags, _ = repetition.detect(store.conn, items, vectors(items))
    assert {flag.line_id for flag in flags if flag.rule == 'guild_evening_phrase'} == {1, 2, 3}
    assert {flag.line_id for flag in flags if flag.rule == 'stock_opening'} == {4, 5, 6}
    assert all(flag.line_id != 7 for flag in flags)
    flags, _ = repetition.detect(store.conn, items[:2], vectors(items[:2]))
    assert flags == []


def replies(text):
    parent = replace(line(1, 'Where is the ferry departure point?', guid=1), to_guid=101, origin='human')
    answer = replace(line(2, text, ts=NOW + 1), to_guid=1)
    return parent, answer


@pytest.mark.parametrize('text,rule', [('lol', 'filler_after_question'),
                                    ('purple turnips study distant galaxies', 'low_parent_and_thread_relevance'),
                                    ('the ferry departure point is by the ferry terminal', None), ('yes', None)])
def test_nonsequitur_hit_near_miss_and_clean(text, rule):
    parent, answer = replies(text)
    flags, coverage = nonsequitur.detect([answer], [parent, answer], vectors([parent, answer]))
    assert [flag.rule for flag in flags] == ([rule] if rule else [])
    assert coverage['stored_parent_links'] == 0
    assert coverage['directed_parent_candidates'] == 1


def test_nonsequitur_ambiguous_guild_adjacency_is_not_a_parent_and_thread_can_explain_reply():
    parent, answer = replies('purple turnips study distant galaxies')
    other = replace(line(0, answer.text, guid=102, ts=NOW - 1), to_guid=0)
    flags, _ = nonsequitur.detect([answer], [other, parent, answer], vectors([other, parent, answer]))
    assert flags == []
    ambiguous = [replace(parent, to_guid=0), replace(answer, to_guid=0)]
    flags, counts = nonsequitur.detect([ambiguous[1]], ambiguous, vectors(ambiguous))
    assert flags == [] and counts['directed_parent_candidates'] == 0


@pytest.fixture
def facts():
    # Query-shaped scratch tables keep tests about temporal evidence, not unrelated
    # native receipt constraints (covered by Realm's own suite).
    with closing(sqlite3.connect(':memory:')) as conn:
        conn.executescript('''
            CREATE TABLE meta(key TEXT, value TEXT);
            INSERT INTO meta VALUES ('guild_id','10');
            CREATE TABLE realm_kill(map INT,boss_id INT,published_ms INT,source INT,guild INT,fact INT);
            CREATE TABLE realm_fact_origin(fact INT,state TEXT,assurance TEXT,policy TEXT);
            CREATE TABLE guild_raid(raid_id INT,started REAL,ended REAL,kills INT,wipes INT);
            CREATE TABLE game_event(id INT,ts REAL,guid INT,payload TEXT,type TEXT);
        ''')
        yield conn


def test_unawareness_verified_kill_denial_true_hit_near_miss_clean_and_no_future(facts):
    facts.execute('INSERT INTO realm_kill VALUES (631,3,?,7,10,9)', ((NOW - 1) * 1000,))
    facts.execute("INSERT INTO realm_fact_origin VALUES (9,'active','verified_event','verified_raid_kill_v1')")
    items = [line(1, 'We have never killed Deathbringer Saurfang.'),
             line(2, 'We have never killed Deathbringer Saurfang.', ts=NOW - 2),
             line(3, 'Deathbringer Saurfang is alive again.'),
             line(4, 'We have never killed Onyxia.'),
             line(5, 'Have we never killed Deathbringer Saurfang?'),
             line(6, 'We have not never killed Deathbringer Saurfang.')]
    flags, _ = unawareness.detect(facts, items)
    assert [(flag.line_id, flag.rule) for flag in flags] == [(1, 'never_killed_after_verified_kill')]
    facts.execute("UPDATE realm_fact_origin SET state='revoked'")
    assert unawareness.detect(facts, items)[0] == []


def test_unawareness_recorded_combat_and_location_use_past_evidence(facts):
    facts.execute('INSERT INTO guild_raid VALUES (1,?,?,1,0)', (NOW - 3600, NOW - 100))
    for key, guid, zone, name in ((1, 102, 4395, 'Dalaran'), (2, 101, 1519, 'Stormwind')):
        facts.execute('INSERT INTO game_event VALUES (?,?,?,?,?)',
                      (key, NOW - 10, guid, json.dumps(dict(zone_id=zone, zone_name=name)), 'zone'))
    items = [line(1, 'We did not raid on 2026-10-29.'), line(2, 'We raided on 2026-10-28.'),
             line(3, "I'm in Dalaran."), line(4, "I'm in Stormwind."),
             line(5, "I'm in Dalaran.", ts=NOW - 1000), line(6, 'If I was in Dalaran I could help.')]
    flags, _ = unawareness.detect(facts, items)
    assert [(flag.line_id, flag.rule) for flag in flags] == [(1, 'raid_denial_after_recorded_combat'), (3, 'location_mismatch')]


def test_report_math_deduplicates_rules_and_compares_compatible_baselines():
    items = [line(1, 'first', ts=NOW - 86400), line(2, 'second'), line(3, 'third', guid=102)]
    results = {name: ([], {}) for name in ('contradiction', 'repetition', 'nonsequitur', 'unawareness')}
    results['repetition'] = ([Flag(2, 101, 'stock_opening'), Flag(2, 101, 'stock_closing')], {})
    report = build(items, results, since=NOW - 86400, until=NOW, days=14, fake=True,
                   judged=False, embedding_fingerprint='fixture')
    report['parameters'] = {}
    assert report['overall'] == dict(lines=3, flagged_lines=1, per_100_lines=33.333333)
    assert report['detectors']['repetition']['per_member']['101']['per_100_lines'] == 50
    assert report['detectors']['repetition']['by_day']['2026-10-28']['flagged_lines'] == 0
    assert report['top_offenders'][0]['guid'] == 101
    baseline = json.loads(json.dumps(report))
    baseline['detectors']['repetition']['per_100_lines'] = 25
    assert compare(report, baseline)['repetition']['delta_per_100_lines'] == 8.333333
    baseline['fake'] = False
    with pytest.raises(ValueError):
        compare(report, baseline)
