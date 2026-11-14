"""The raid lineup (D4 Part B): built from rows, proposed ahead of a posted raid night, posted by
the night's lead, ended with its night. Invented members only; he is guid 1."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from conftest import make_persona
from leeroy_mind.decisions import ledger
from leeroy_mind.decisions.lineup import (BENCH_MAX, LINEUP_AHEAD_S, NIGHT_S, SETTLE_AFTER_S, LineupRules,
                                          build_lineup, roster_text, subject_of, tonight)
from leeroy_mind.guild.comp import Candidate, coverage, select_comp
from leeroy_mind.protocol import Ack, Member

HOUR = 3600.0


def move(clock, ts):
    clock._t = float(ts)  # the fake clock jumps; nothing here sleeps


def body(guid, cls, spec, *, level=80):
    return Member(guid=guid, name=f'N{guid}', race=1, class_=cls, gender=0, level=level, rank=3, online=True,
                  map=0, zone=0, spec_tab=spec)


@pytest.fixture
def cast(store):
    """Two tanks (a raid lead among them), four healers and nine damage; he is in the roster too."""
    members, personas = {1: body(1, 8, 1)}, {}
    spec = {**{g: (1, 2, 'tank') for g in (201, 202)}, **{g: (5, 0, 'healer') for g in (211, 212, 213, 214)},
            **{g: (8, 1, 'ranged') for g in range(221, 230)}}
    for guid, (cls, tab, role) in spec.items():
        members[guid] = body(guid, cls, tab)
        title = 'Raid Lead' if guid == 201 else 'DKP' if guid == 211 else ''
        personas[guid] = make_persona(guid, f'N{guid}', raid_role=role, guild_role='officer' if title else 'raider',
                                      officer_title=title)
    personas[1] = make_persona(1, 'Testmage')
    store.upsert_cast(list(members.values()), 1000.0)
    store.set_meta('human_guid', '1')
    return members, personas


def event(store, at, ident=7):
    with store.transaction():
        store.conn.execute("INSERT INTO realm_calendar(id,kind,creator,at,title,ref,updated,event_id,state) "
                           "VALUES (?,'raid',201,?,'Raid night','raid:x',?,?,'posted')", (ident, at, at, 900 + ident))
    return store.conn.execute('SELECT id,title,at FROM realm_calendar WHERE id=?', (ident,)).fetchone()


def test_lineup_never_includes_him_and_fills_roles(store, clock, cast):
    members, personas = cast
    cal = event(store, clock.now() + 20 * HOUR)
    value = build_lineup(store.conn, {}, cal, now=clock.now(), members=members, personas=personas, human_guid=1)
    assert (value['size'], value['heroic']) == (10, False)  # 14 available: a 10 (no raid of his to follow)
    assert 1 not in value['members'] and 1 not in value['bench']
    assert len(value['members']) == 9  # his seat is the tenth
    assert value['tanks'] == [201, 202] and len(value['healers']) == 3
    assert {value['lead'], value['tank'], value['loot']} <= set(value['members'])
    assert (value['lead'], value['tank'], value['loot']) == (201, 201, 211)
    assert not set(value['members']) & set(value['bench']) and 0 < len(value['bench']) <= BENCH_MAX
    assert value['coverage'] == ['no_bloodlust', 'no_replenishment']
    assert json.dumps(value)  # stored as the decision's value
    # The same inputs give the same lineup
    assert build_lineup(store.conn, {}, cal, now=clock.now(), members=members, personas=personas,
                        human_guid=1) == value
    # Members away or under 80 are left out
    away = build_lineup(store.conn, {}, cal, now=clock.now(), members={**members, 202: body(202, 1, 2, level=70)},
                        personas=personas, human_guid=1, away=frozenset({221}))
    assert 202 not in away['members'] and 221 not in away['members'] + away['bench']


def candidate(guid, cls, spec, *, ilvl=0, role='damage'):
    return Candidate(guid, f'N{guid}', cls, spec, role, role == 'tank', 'raider', '', 0, 0, True, ilvl)


def test_coverage_breaks_ties_toward_bloodlust_and_replenishment():
    tanks = [candidate(1001, 1, 2, role='tank'), candidate(1002, 1, 2, role='tank')]
    healers = [candidate(g, 5, 0, role='healer') for g in (1011, 1012, 1013)]
    # Equal priority damage: three plain mages, an Elemental shaman (Bloodlust) and a Shadow priest
    # (Replenishment), both with the highest guids so a guid order would never reach them
    damage = [candidate(1021, 8, 1), candidate(1022, 8, 1), candidate(1023, 8, 1), candidate(1029, 7, 0),
              candidate(1030, 5, 2), candidate(1031, 8, 1)]
    pool = tanks + healers + damage
    for seed in range(8):
        comp = select_comp(10, pool, must_include=(), human_in_raid=True, seed=seed, buff_ties=True)
        assert {1029, 1030} <= set(comp.members)
        assert coverage(10, [c for c in pool if c.guid in comp.members]) == ['bloodlust', 'replenishment']
    # A 25 wants a second Replenishment; one source reads as missing
    assert coverage(25, [candidate(1030, 5, 2), candidate(1029, 7, 0)]) == ['bloodlust', 'no_replenishment']
    # With every buff covered, the better gear breaks the tie
    geared = tanks + healers + [candidate(1041, 8, 1, ilvl=232), candidate(1042, 8, 1, ilvl=264),
                                candidate(1043, 8, 1, ilvl=245), candidate(1044, 8, 1, ilvl=251),
                                candidate(1045, 8, 1, ilvl=200)]
    comp = select_comp(10, geared, must_include=(), human_in_raid=True, seed=3, buff_ties=True)
    assert [g for g in comp.members if g > 1040] == [1042, 1044, 1043, 1041]
    # Off (the default), the order is today's: item levels and buffs play no part
    from dataclasses import replace
    plain = [replace(c, ilvl=0) for c in geared]
    for seed in range(8):
        assert (select_comp(10, geared, must_include=(), human_in_raid=True, seed=seed)
                == select_comp(10, plain, must_include=(), human_in_raid=True, seed=seed))


class Ports:
    def __init__(self, ok=True):
        self.enabled, self.sent, self.ok = True, [], ok

    async def send(self, command):
        self.sent.append(command)
        return Ack(0, command.id, self.ok, '' if self.ok else 'bad field: until_ms')


def app_for(store, clock, personas, ports=None):
    return SimpleNamespace(store=store, clock=clock, personas=personas, human_guid=1, server=None,
                           guild=SimpleNamespace(ports=ports or Ports(), armory=None))


def run(rules, app):
    asyncio.run(rules.tick(app, app.clock.now()))


def test_proposed_by_the_lead_posted_two_hours_later_and_ended_with_its_night(store, clock, cast):
    members, personas = cast
    at = clock.now() + LINEUP_AHEAD_S[1] + HOUR
    event(store, at)
    rules, app = LineupRules(), app_for(store, clock, personas)
    run(rules, app)
    assert ledger.history(store.conn) == []  # more than 30 h ahead
    move(clock, at - LINEUP_AHEAD_S[1] + 60)
    run(rules, app)
    row = ledger.live(store.conn, 'lineup', subject_of(7))
    assert row['status'] == 'proposed' and json.loads(row['value'])['lead'] == 201
    positions = json.loads(row['positions'])
    assert {p['side'] for p in positions} <= {'support', 'object'} and 1 not in {p['guid'] for p in positions}
    run(rules, app)
    assert len(ledger.history(store.conn)) == 1  # proposed once per night
    move(clock, row['proposed'] + SETTLE_AFTER_S)
    run(rules, app)
    row = ledger.live(store.conn, 'lineup', subject_of(7))
    assert (row['status'], row['settled_by']) == ('settled', 201)
    post = store.conn.execute('SELECT channel,author,origin,text FROM discord_message WHERE id=?',
                              (row['message'],)).fetchone()
    assert (post['channel'], post['author'], post['origin']) == ('raid-planning', 201, 'mind')
    assert post['text'].startswith('Lineup for Raid night') and '\nTanks: N201, N202\n' in post['text']
    assert 'Bench: ' in post['text'] and 'Testmage' not in post['text']
    # Its night over without a formation: talk only, and never again live
    move(clock, at + NIGHT_S)
    run(rules, app)
    row = ledger.get(store.conn, row['id'])
    assert row['status'] == 'talk_only' and json.loads(row['effect_ref']) == {'talk_only': 'not_formed'}


def test_no_raid_means_talk_only(store, clock, cast):
    members, personas = cast
    now = clock.now()
    rows = {}
    for ident, status in ((1, 'proposed'), (2, 'settled'), (3, 'in_effect')):
        event(store, now + ident * HOUR, ident)
        rows[status] = ledger.propose(store, kind='lineup', subject=subject_of(ident),
                                      value={'members': [201], 'bench': []}, positions=[], now=now)
        if status != 'proposed':
            ledger.settle(store, rows[status], settled_by=201, now=now)
        if status == 'in_effect':
            ledger.take_effect(store, rows[status], effect_ref={'raid': 4}, now=now)
    rules, app = LineupRules(), app_for(store, clock, personas)
    move(clock, now + 3 * HOUR + NIGHT_S)
    run(rules, app)
    assert ledger.get(store.conn, rows['proposed'])['status'] == 'expired'      # never posted
    assert ledger.get(store.conn, rows['settled'])['status'] == 'talk_only'     # posted, never formed
    formed = ledger.get(store.conn, rows['in_effect'])                          # formed: kept, not live
    assert formed['status'] == 'expired' and json.loads(formed['effect_ref']) == {'raid': 4}
    assert ledger.in_effect(store.conn, 'lineup') == []


def test_tonight_is_the_settled_lineup_of_the_night_at_hand(store, clock, cast):
    now = clock.now()
    event(store, now + 3 * HOUR)
    ident = ledger.propose(store, kind='lineup', subject=subject_of(7),
                           value={'members': [201, 211], 'bench': [229], 'tanks': [201], 'healers': [211]},
                           positions=[], now=now)
    assert tonight(store.conn, now) is None  # proposed, not posted
    ledger.settle(store, ident, settled_by=201, now=now)
    found = tonight(store.conn, now)
    assert (found.decision, found.members, found.bench) == (ident, (201, 211), frozenset({229}))
    assert tonight(store.conn, now - 4 * HOUR) is None  # its night is not at hand yet
    ledger.overrule(store, ident, now=now)
    assert tonight(store.conn, now) is None  # his overrule before it formed stops it


def test_the_post_is_built_from_rows():
    value = {'size': 25, 'heroic': True, 'members': [3, 4, 5], 'tanks': [3], 'healers': [4], 'bench': [],
             'coverage': ['bloodlust', 'no_replenishment']}
    from datetime import datetime, timezone
    text = roster_text(value, {3: 'Aa', 4: 'Bb', 5: 'Cc'}, title='Raid night',
                       at=datetime(2026, 11, 3, 20, tzinfo=timezone.utc))
    assert text == ('Lineup for Raid night, Tuesday 20:00 (25-man heroic):\nTanks: Aa\nHealers: Bb\nDamage: Cc\n'
                    'Bench: nobody\nMissing: Replenishment')
