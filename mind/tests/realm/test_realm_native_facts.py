"""Native-only Realm facts: realm-first achievements, duels outside cities and 5-man guild kills.

Every path runs the real capture, source claim, audit and FactStore writer over actual Store
rows, or the real bridge reader and service. Migration 0014 is checked on disposable copies.
"""
import asyncio
import json
import sqlite3

import pytest

from leeroy_mind.memory.writer import clean_memory_text
from leeroy_mind.protocol import sanitize_say_text
from leeroy_mind.realm import service
from leeroy_mind.realm.fact_store import FactStore
from leeroy_mind.realm.kill_retention import PrivateKillRecord
from leeroy_mind.realm.provenance import public_fact_in_unit, render_public
from leeroy_mind.realm.records import FactRow, Rejected
from leeroy_mind.storage import db
from leeroy_mind.wow import BOSS_NAMES, DUNGEON_BOSS_NAMES, DUNGEONS, RAID_MAPS
from test_realm_foundation_schema import _announcement, _insert, _kill, _row, _source
from test_realm_live_proof import HUMAN, observe, roster
from test_realm_source_admission import KILL, PRIVATE, application, envelope, run

ACHIEVEMENT, DUEL, DUNGEON = 'realm_first_achievement_v1', 'duel_result_v1', 'verified_dungeon_kill_v1'
TABLES = ('realm_source', 'realm_fact', 'realm_fact_origin', 'realm_knows', 'realm_kill',
          'realm_dungeon_kill', 'realm_announcement')
CAST = [101, 102, 103, 104, 105]


def counts(store):
    with store.transaction():
        return {table: store.conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0] for table in TABLES}


NOTHING = dict.fromkeys(TABLES, 0)


def record(store, policy, **payload):
    with store.transaction():
        observation = observe(store, policy, payload=payload)
    return FactStore(store).record(observation, human=HUMAN), observation


def knows(store, fact):
    with store.transaction():
        return [list(r) for r in store.conn.execute(
            'SELECT guild,how FROM realm_knows WHERE fact=? ORDER BY guild', (fact.id,))]


# -- realm-first achievements ------------------------------------------------------------------

@pytest.mark.parametrize('guid', [10, 103])
def test_realm_first_of_him_or_a_cast_member_is_a_server_wide_fact(roster, guid):
    fact, _ = record(roster, ACHIEVEMENT, guid=guid, name='Realm First! Grand Master Alchemist',
                     achievement_id=1414, flags=0x100)
    assert type(fact) is FactRow
    assert (fact.kind, fact.text, fact.source, fact.public) == (
        'realm_first', f'Character {guid} earned Realm First! Grand Master Alchemist.', 'event', True)
    assert knows(roster, fact) == [[50, 'own'], [60, 'server']]
    with roster.transaction():
        assert public_fact_in_unit(roster, fact.id, human=HUMAN)[0] == fact


def test_non_realm_first_achievement_makes_no_fact(roster):
    # Native publishes every earned achievement outside its quiet scope; only a realm first counts.
    fact, _ = record(roster, ACHIEVEMENT, realm_first=False, flags=0, name='Level 80')
    assert fact == Rejected('private_evidence')
    # The DBC flag alone is not the core's realm-first rule (his account may not take one).
    fact, _ = record(roster, ACHIEVEMENT, realm_first=False, flags=0x100)
    assert fact == Rejected('private_evidence')
    assert counts(roster) == NOTHING


@pytest.mark.parametrize('guid', [201, 999])
def test_realm_first_outside_the_cast_makes_no_fact(roster, guid):
    fact, _ = record(roster, ACHIEVEMENT, guid=guid)
    assert fact == Rejected('private_evidence')
    assert counts(roster) == NOTHING


@pytest.mark.parametrize('name,reason', [
    ('Realm First! Synthetic Private', 'real_name'),
    ('Realm First! We kissed', 'romance'), ('Realm First! |cffff0000Level 80|r', 'unsupported_text'),
    (' Realm First! Level 80', 'unsupported_text'), ('Realm First!  Level 80', 'unsupported_text'),
    ('', 'unsupported_text'),
])
def test_achievement_name_goes_through_the_cleaning_and_real_name_screens(roster, name, reason):
    fact, _ = record(roster, ACHIEVEMENT, name=name)
    assert fact == Rejected(reason)
    assert counts(roster) == NOTHING


def test_achievement_row_binds_its_earner(roster):
    fact, observation = record(roster, ACHIEVEMENT)
    assert type(fact) is FactRow
    with roster.transaction():
        # The App keeps the event's own guid on its row; another earner is another row.
        assert roster.conn.execute('SELECT guid FROM game_event WHERE id=?',
                                   (observation.receipt.row_id,)).fetchone()[0] == 101
        roster.conn.execute('UPDATE game_event SET guid=102 WHERE id=?', (observation.receipt.row_id,))
        assert public_fact_in_unit(roster, fact.id, human=HUMAN) == Rejected('invalid_row')


# -- duels -------------------------------------------------------------------------------------

@pytest.mark.parametrize('result,text', [
    ('won', 'Character 201 beat character 10 in a duel in zone 14.'),
    ('fled', 'Character 10 fled a duel with character 201 in zone 14.'),
])
def test_duel_outside_a_city_is_a_result_its_duelists_guilds_know(roster, result, text):
    fact, _ = record(roster, DUEL, result=result)
    assert type(fact) is FactRow and (fact.kind, fact.text) == ('duel_result', text)
    assert knows(roster, fact) == [[50, 'present'], [60, 'present']]


def test_duel_between_two_cast_members_is_known_to_the_guild_only(roster):
    fact, _ = record(roster, DUEL, winner=101, loser=102)
    assert type(fact) is FactRow and knows(roster, fact) == [[50, 'present']]


@pytest.mark.parametrize('changes', [dict(city=True), dict(city=True, zone=1637),
                                     dict(result='interrupted'), dict(winner=300, loser=301)])
def test_city_interrupted_or_unwatched_duel_makes_no_fact(roster, changes):
    fact, _ = record(roster, DUEL, **changes)
    assert fact == Rejected('private_evidence')
    assert counts(roster) == NOTHING


@pytest.mark.parametrize('changes', [dict(result='draw'), dict(city=1), dict(winner=10),
                                     dict(winner=0), dict(zone=-1), dict(map=True)])
def test_malformed_duel_makes_no_fact(roster, changes):
    fact, _ = record(roster, DUEL, **changes)
    assert type(fact) is Rejected
    assert counts(roster) == NOTHING


# -- 5-man guild kills -------------------------------------------------------------------------

def solo_cast(**changes):
    return dict(human_present=False, fight_started_at_ms=0, fight_ms=-1, cast_members=CAST, **changes)


def test_dungeon_kill_is_a_public_fact_with_its_own_row(roster):
    fact, observation = record(roster, DUNGEON)
    assert type(fact) is FactRow
    assert (fact.kind, fact.text) == ('kill', "The guild defeated Anub'arak in Azjol-Nerub.")
    assert knows(roster, fact) == [[50, 'own']]
    assert counts(roster) == {**NOTHING, 'realm_source': 1, 'realm_fact': 1, 'realm_fact_origin': 1,
                              'realm_knows': 1, 'realm_dungeon_kill': 1}
    with roster.transaction():
        row = dict(roster.conn.execute('SELECT * FROM realm_dungeon_kill').fetchone())
    assert {k: row[k] for k in ('kill_id', 'map', 'boss_id', 'difficulty', 'size', 'heroic', 'human_present',
                                'native_cast_json', 'qualified_cast_json', 'fact')} == dict(
        kill_id='c' * 32 + ':1', map=601, boss_id=2, difficulty=1, size=5, heroic=1, human_present=1,
        native_cast_json='[101,102,103,104]', qualified_cast_json='[101,102,103,104]', fact=fact.id)


def test_5_man_kill_is_the_guilds_own_and_never_known_server_wide(roster):
    # Controller ruling: a 5-man kill is the guild's own, never 'server'; raid kills stay server-wide.
    facts = FactStore(roster)
    dungeons = [record(roster, DUNGEON)[0],
                record(roster, DUNGEON, **solo_cast(kill_id='c' * 32 + ':2', instance_id=13, boss_id=0))[0]]
    raid, _ = record(roster, KILL)
    assert all(type(fact) is FactRow for fact in (*dungeons, raid))
    assert [knows(roster, fact) for fact in dungeons] == [[[50, 'own']]] * 2
    assert knows(roster, raid) == [[50, 'own'], [60, 'server']]
    assert facts.known(60, human=HUMAN) == (raid,)
    assert set(facts.known(50, human=HUMAN)) == {raid, *dungeons}
    # A rival guild's actor in the group makes his guild know that kill as present, no more.
    joined, _ = record(roster, DUNGEON, kill_id='c' * 32 + ':3', instance_id=14, cast_members=[101, 102, 103, 201])
    assert knows(roster, joined) == [[50, 'own'], [60, 'present']]
    assert facts.known(60, human=HUMAN) == (raid, joined)
    with roster.transaction():
        assert roster.conn.execute("SELECT count(*) FROM realm_knows k JOIN realm_fact_origin o ON o.fact=k.fact "
                                   "WHERE o.policy=? AND k.how='server'", (DUNGEON,)).fetchone()[0] == 0


def test_dungeon_kill_without_him_needs_five_qualified_cast(roster):
    fact, _ = record(roster, DUNGEON, **solo_cast())
    assert type(fact) is FactRow
    with roster.transaction():
        roster.conn.execute('UPDATE cast_member SET level=79 WHERE guid=105')
    # Native saw five cast; only four are the mind's cast: a dungeon kill is a fact or nothing.
    fact, _ = record(roster, DUNGEON, **solo_cast(kill_id='c' * 32 + ':2', instance_id=13))
    assert fact == Rejected('private_evidence')
    assert counts(roster)['realm_dungeon_kill'] == 1 and counts(roster)['realm_kill'] == 0


@pytest.mark.parametrize('changes', [dict(boss_id=5), dict(map=36, boss_id=0), dict(map=575, boss_id=3),
                                     dict(map=599, boss_id=4), dict(map=668, boss_id=2), dict(map=631, boss_id=0)])
def test_dungeon_boss_outside_the_catalogue_makes_no_fact(roster, changes):
    fact, _ = record(roster, DUNGEON, **changes)
    assert fact == Rejected('topic_mismatch')
    assert counts(roster) == NOTHING


def test_same_dungeon_kill_once_and_a_changed_field_never_another(roster):
    first, observation = record(roster, DUNGEON)
    # Another envelope of the same native kill (another server ts) is refused, as is any change.
    for changes in (dict(ts=1000001), dict(attempts=3, ts=1000002), dict(boss_id=1, ts=1000003)):
        assert type(record(roster, DUNGEON, **changes)[0]) is Rejected
    assert counts(roster)['realm_dungeon_kill'] == 1 and counts(roster)['realm_fact'] == 1
    with roster.transaction():
        assert FactStore(roster).put_fact_in_unit(observation, human=HUMAN) == first


def test_every_catalogued_dungeon_fight_renders_clean_text():
    assert not set(DUNGEONS) & RAID_MAPS and {m for m, _ in DUNGEON_BOSS_NAMES} == set(DUNGEONS)
    assert not set(DUNGEON_BOSS_NAMES) & set(BOSS_NAMES)
    for (map_id, boss), name in DUNGEON_BOSS_NAMES.items():
        text = render_public(DUNGEON, dict(catalogue='dungeon_catalogue_v1', map=map_id, boss_id=boss))
        assert text == f'The guild defeated {name} in {DUNGEONS[map_id]}.'
        assert len(text) <= 200 and sanitize_say_text(text) == text == clean_memory_text(text, HUMAN)
    with pytest.raises(ValueError):
        render_public(DUNGEON, dict(catalogue='raid_catalogue_v1', map=601, boss_id=2))


# -- through the bridge reader and the service ---------------------------------------------------

def fresh_dungeon_kill(clock, **changes):
    at = int(clock.now() * 1000) - 2000
    return envelope(DUNGEON, **{'ts': at + 20, 'killed_at_ms': at, 'verified_at_ms': at + 10,
                                'fight_started_at_ms': at - 251_000, 'fight_ms': 251_000, **changes})


def test_dungeon_kill_through_the_service_is_evidence_never_announced(tmp_path, clock, monkeypatch):
    async def scenario():
        async with application(tmp_path, clock, monkeypatch) as native:
            await native.deliver(fresh_dungeon_kill(clock))
            for _ in range(20):
                await asyncio.sleep(0)
            assert type(native.results[-1]) is FactRow
            assert not any(json.loads(line).get('type') == 'announce'
                           for writer in native.writers for line in writer.sent)
            assert counts(native.app.store) == {**NOTHING, 'realm_source': 1, 'realm_fact': 1,
                'realm_fact_origin': 1, 'realm_knows': 1, 'realm_dungeon_kill': 1}
            assert not native.app.realm._children
    run(scenario())


@pytest.mark.parametrize('policy,changes', [
    (ACHIEVEMENT, dict(realm_first=False, flags=0)), (ACHIEVEMENT, dict(realm_first=False)),
    (DUEL, dict(city=True)), (DUEL, dict(result='interrupted')),
])
def test_evidence_that_can_never_qualify_is_refused_before_any_capture_unit(
        tmp_path, clock, monkeypatch, policy, changes):
    async def scenario():
        async with application(tmp_path, clock, monkeypatch) as native:
            realm, store = native.app.realm, native.app.store
            capture, transaction, qualify = (realm._capture_source_observation, store.transaction,
                                             service.capture_qualification_in_unit)
            inside, units, qualified = [], [], []

            def tracked_capture(ev):
                inside.append(type(ev).__name__)
                try:
                    return capture(ev)
                finally:
                    inside.pop()

            def tracked_transaction():
                if inside:
                    units.append(inside[-1])
                return transaction()

            def tracked_qualify(*args, **kwargs):
                qualified.append(type(kwargs['event']).__name__)
                return qualify(*args, **kwargs)

            monkeypatch.setattr(realm, '_capture_source_observation', tracked_capture)
            monkeypatch.setattr(store, 'transaction', tracked_transaction)
            monkeypatch.setattr(service, 'capture_qualification_in_unit', tracked_qualify)
            await native.deliver(envelope(policy, **changes))
            assert (units, qualified, native.captures, native.results) == ([], [], [], [])
            assert counts(store) == NOTHING
            # The same path still captures evidence that can qualify, in one Store unit.
            await native.deliver(envelope(policy, ts=1000001))
            name = 'RealmAchievement' if policy == ACHIEVEMENT else 'DuelEnd'
            assert (units, qualified, len(native.captures)) == ([name], [name], 1)
            assert type(native.results[-1]) is FactRow
    run(scenario())


@pytest.mark.parametrize('policy,changes', [(KILL, {}), (KILL, PRIVATE), (DUNGEON, {})],
                         ids=['public_raid', 'private_raid', 'dungeon'])
def test_attempts_only_change_of_a_kill_is_refused_as_proof_changed(tmp_path, clock, monkeypatch, policy, changes):
    async def scenario():
        async with application(tmp_path, clock, monkeypatch) as native:
            body = envelope(policy, **changes)
            await native.deliver(body)
            assert type(native.results[-1]) is (PrivateKillRecord if changes else FactRow)
            before = counts(native.app.store)
            # The same kill at the same server ts: only native's attempt count differs.
            await native.deliver({**body, 'attempts': body['attempts'] + 3})
            assert native.results[-1] == Rejected('proof_changed')
            assert counts(native.app.store) == before
    run(scenario())


@pytest.mark.parametrize('policy', [ACHIEVEMENT, DUEL, DUNGEON])
def test_disabled_realm_makes_no_native_fact(tmp_path, clock, monkeypatch, policy):
    async def scenario():
        async with application(tmp_path, clock, monkeypatch, enabled=False) as native:
            await native.deliver(envelope(policy))
            for _ in range(20):
                await asyncio.sleep(0)
            assert native.captures == [] and native.results == []
            assert counts(native.app.store) == NOTHING
    run(scenario())


# -- the schema and migration 0014 -------------------------------------------------------------

def _dungeon_kill(**overrides):
    values = {**_kill(kill_id='c' * 32 + ':1', native_boot='c' * 32, map=601, boss_id=2, difficulty=1,
                      size=5, heroic=1, native_cast_json='[101,102,103,104]',
                      qualified_cast_json='[101,102,103,104]', fact=1), **overrides}
    return values


def _origin(fact, source, policy, assurance, key=None):
    return {**_row('realm_fact_origin'), 'fact': fact, 'source': source, 'policy': policy,
            'assurance': assurance, 'producer_key': f'event:{key or source:064x}:{policy}'}


def _v13(tmp_path, monkeypatch):
    files = db._migration_files()
    with monkeypatch.context() as patch:
        patch.setattr(db, '_migration_files', lambda: [(n, p) for n, p in files if n <= 13])
        conn = db.open_db(tmp_path / 'v13.db')
    with conn:
        for identifier in range(1, 6):
            _insert(conn, 'game_event', dict(id=identifier, ts=1000.0, type='guard', payload='{}'))
            _insert(conn, 'realm_source', _source(identifier))
        for identifier in (1, 2):
            _insert(conn, 'realm_fact', dict(id=identifier, kind='kill', text='A claim.', source='event', ts=1000.0))
            _insert(conn, 'realm_knows', dict(fact=identifier, guild=50, since=1000.0, how='own'))
        _insert(conn, 'realm_fact_origin', _origin(1, 1, 'verified_raid_kill_v1', 'verified_event'))
        _insert(conn, 'realm_fact_origin', {**_origin(2, 2, 'bg_result_v1', 'observed_event'), 'state': 'revoked',
                                            'revision': 3})
        _insert(conn, 'realm_kill', _kill(kill_id='a' * 32 + ':1', source=1, fact=1))
        _insert(conn, 'realm_kill', _kill(kill_id='a' * 32 + ':3', source=3))
        _insert(conn, 'realm_announcement', _announcement(kill_id='a' * 32 + ':1', state='sent',
                dispatched_at=1001.0, settled_at=1002.0))
    return conn, files


def _schema(conn):
    return [tuple(r) for r in conn.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name')]


def _rows(conn):
    tables = [r['name'] for r in conn.execute('PRAGMA table_list') if r['type'] == 'table'
              and r['schema'] == 'main' and not r['name'].startswith('sqlite_')]
    return {table: [tuple(r) for r in conn.execute(f'SELECT * FROM "{table}" ORDER BY 1')] for table in sorted(tables)}


def test_v13_upgrade_keeps_every_row_and_admits_the_native_facts(tmp_path, monkeypatch):
    conn, files = _v13(tmp_path, monkeypatch)
    try:
        schema, rows = _schema(conn), _rows(conn)
        info = [tuple(r) for r in conn.execute('PRAGMA table_info(realm_fact_origin)')]
        keys = [tuple(r) for r in conn.execute('PRAGMA foreign_key_list(realm_fact_origin)')]
        monkeypatch.setattr(db, '_migration_files', lambda: [(n, p) for n, p in files if n <= 14])
        assert db.migrate(conn) == 14 and db.migrate(conn) == 14
        after = _schema(conn)
        # Only the rebuilt origin table's own definition changes, and one table is added.
        assert ([e for e in after if e[2] not in ('realm_fact_origin', 'realm_dungeon_kill')]
                == [e for e in schema if e[2] != 'realm_fact_origin'])
        assert [e[:3] for e in after if e[2] == 'realm_fact_origin'] == [
            e[:3] for e in schema if e[2] == 'realm_fact_origin']
        assert _rows(conn) == {**rows, 'realm_dungeon_kill': []}
        assert [tuple(r) for r in conn.execute('PRAGMA table_info(realm_fact_origin)')] == info
        assert [tuple(r) for r in conn.execute('PRAGMA foreign_key_list(realm_fact_origin)')] == keys
        assert conn.execute("SELECT count(*) FROM sqlite_master WHERE name LIKE '%_v13'").fetchone()[0] == 0
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()
        assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        with conn:
            for fact in (3, 4, 5):
                _insert(conn, 'realm_fact', dict(id=fact, kind='kill', text='A claim.', source='event', ts=1000.0))
            _insert(conn, 'realm_fact_origin', _origin(3, 3, ACHIEVEMENT, 'observed_event'))
            _insert(conn, 'realm_fact_origin', _origin(4, 4, DUEL, 'observed_event'))
            _insert(conn, 'realm_fact_origin', _origin(5, 5, DUNGEON, 'verified_event'))
            _insert(conn, 'realm_dungeon_kill', _dungeon_kill(source=5, fact=5))
        for values in (_origin(6, 4, DUNGEON, 'observed_event', 99), _origin(6, 4, DUEL, 'verified_event', 99),
                       _origin(6, 4, ACHIEVEMENT, 'verified_event', 99),
                       _origin(6, 4, 'stored_forum_topic_v1', 'stored_topic', 99)):
            with pytest.raises(sqlite3.IntegrityError), conn:
                _insert(conn, 'realm_fact', dict(id=6, kind='kill', text='A claim.', source='event', ts=1000.0))
                _insert(conn, 'realm_fact_origin', values)
        # An announcement can only ever reference a raid kill.
        with pytest.raises(sqlite3.IntegrityError), conn:
            _insert(conn, 'realm_announcement', _announcement(command_id='announce_2', kill_id='c' * 32 + ':1',
                                                              period_key='2026-11-10'))
        with conn:
            _insert(conn, 'realm_announcement', _announcement(command_id='announce_2', kill_id='a' * 32 + ':3',
                                                              period_key='2026-11-10'))
    finally:
        conn.close()


@pytest.mark.parametrize('overrides', [
    dict(size=10), dict(size=25, difficulty=1, heroic=0), dict(difficulty=2), dict(difficulty=-1),
    dict(heroic=0), dict(fact=None), dict(fact=999), dict(source=999), dict(kill_id='a' * 32 + ':1'),
    dict(human_present=0), dict(human_present=0, qualified_cast_json='[1,2,3,4,'),
    dict(verified_ms=999999), dict(qualified_cast_json='{}'),
])
def test_dungeon_kill_table_admits_only_a_stored_public_5_man_guild_kill(tmp_path, overrides):
    conn = db.open_db(tmp_path / 'schema.db')
    try:
        with conn:
            for identifier in (1, 2):
                _insert(conn, 'game_event', dict(id=identifier, ts=1000.0, type='guard', payload='{}'))
                _insert(conn, 'realm_source', _source(identifier))
                _insert(conn, 'realm_fact', dict(id=identifier, kind='kill', text='A claim.', source='event',
                                                 ts=1000.0))
            # Five qualified cast without him is a guild kill.
            _insert(conn, 'realm_dungeon_kill', _dungeon_kill(human_present=0, qualified_cast_json='[1,2,3,4,5]'))
        second = _dungeon_kill(kill_id='c' * 32 + ':2', source=2, fact=2)
        with pytest.raises(sqlite3.IntegrityError), conn:
            _insert(conn, 'realm_dungeon_kill', {**second, **overrides})
        with conn:  # The same row without the change is a valid one.
            _insert(conn, 'realm_dungeon_kill', second)
    finally:
        conn.close()


def test_failed_0014_rolls_back_the_rebuild(tmp_path, monkeypatch):
    conn, files = _v13(tmp_path, monkeypatch)
    try:
        schema, rows = _schema(conn), _rows(conn)
        migration = next(path for version, path in files if version == 14)
        broken = tmp_path / '0014_realm_native_facts.sql'
        broken.write_text(migration.read_text(encoding='utf-8') + '\nCREATE TABLE realm_kill(x);', encoding='utf-8')
        monkeypatch.setattr(db, '_migration_files', lambda: [(n, p) for n, p in files if n <= 13] + [(14, broken)])
        with pytest.raises(sqlite3.OperationalError):
            db.migrate(conn)
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 13
        assert _schema(conn) == schema and _rows(conn) == rows
    finally:
        conn.close()
