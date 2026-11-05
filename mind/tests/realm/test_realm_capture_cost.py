"""Source capture reuses persona parses only for identical stored text (review F3).

Every answer is checked against the uncached `load_persona` reading the capture used
before, through each kind of roster, kick and persona row change.
"""
import statistics
import time

import pytest

from conftest import TEST_CAST, make_persona
from leeroy_mind.personas.io import canonical_json, load_persona, save_persona
from leeroy_mind.realm import provenance
from leeroy_mind.realm.provenance import capture_qualification_in_unit
from leeroy_mind.realm.records import QualificationSnapshot, Rejected
from test_realm_live_proof import CONTEXT, HUMAN, insert, observe, roster
from test_realm_source_admission import application, envelope, run


@pytest.fixture
def fresh_rows(monkeypatch):
    rows = provenance._PersonaRows()
    monkeypatch.setattr(provenance, '_persona_rows', rows)
    return rows


@pytest.fixture
def parses(monkeypatch):
    calls = []
    original = provenance.persona_from_dict

    def counted(value):
        calls.append(value.get('guid') if isinstance(value, dict) else None)
        return original(value)

    monkeypatch.setattr(provenance, 'persona_from_dict', counted)
    return calls


def capture(store, observation):
    return capture_qualification_in_unit(store, human=HUMAN, event=observation.event,
        receipt=observation.receipt, context=CONTEXT, confirmed_human_guid=HUMAN.guid,
        transport_epoch=7, captured_at=1010.0)


def uncached_cast(store):
    """The capture's cast loop as it was, with a fresh load_persona per member."""
    kicked = {row['guid'] for row in store.conn.execute('SELECT guid FROM realm_kick')}
    cast = []
    for row in store.conn.execute('SELECT guid FROM cast_member WHERE level>=80 ORDER BY guid'):
        guid = row['guid']
        if guid == HUMAN.guid or guid in kicked:
            continue
        try:
            persona = load_persona(store, guid)
        except ValueError:
            continue
        if persona is not None:
            if persona.guid != guid:
                return Rejected('invalid_row')
            cast.append(guid)
    return tuple(cast)


def add_member(store, guid, *, level=80, persona=True):
    insert(store, 'cast_member', guid=guid, name='Synthetic', race=2, **{'class': 1},
           gender=0, level=level, rank=3, online=0, updated=1000.0)
    if persona:
        save_persona(store, make_persona(guid, 'Synthetic'), source='seed', ts=1000.0,
                     race='Orc', class_name='Warrior')


def set_json(store, guid, text):
    store.conn.execute('UPDATE persona_core SET json=? WHERE guid=?', (text, guid))


def stored(guid, name=None):
    return canonical_json(make_persona(guid, name or TEST_CAST.get(guid, ('Synthetic',))[0]))


def newer_schema(guid):
    text = stored(guid)
    assert text.count('"schema":1') == 1
    return text.replace('"schema":1', '"schema":2')  # Same length, one byte apart.


STEPS = [
    ('initial', lambda s: None),
    ('unreadable json', lambda s: set_json(s, 101, '{')),
    ('repaired', lambda s: set_json(s, 101, stored(101))),
    ('newer schema, same length', lambda s: set_json(s, 104, newer_schema(104))),
    ('schema restored', lambda s: set_json(s, 104, stored(104))),
    ('not an object', lambda s: set_json(s, 103, '[]')),
    ('object restored', lambda s: set_json(s, 103, stored(103))),
    ('another member persona', lambda s: set_json(s, 102, stored(103))),
    ('own persona again', lambda s: set_json(s, 102, stored(102))),
    ('persona deleted', lambda s: s.conn.execute('DELETE FROM persona_core WHERE guid=105')),
    ('persona saved again', lambda s: save_persona(s, make_persona(105, 'Ebbo'), source='seed',
                                                   ts=1000.0, race='Orc', class_name='Warrior')),
    ('changed persona, same guid', lambda s: set_json(s, 105, stored(105, 'Ebbor'))),
    ('member below 80 added', lambda s: add_member(s, 1001, level=79)),
    ('member reaches 80', lambda s: s.conn.execute('UPDATE cast_member SET level=80 WHERE guid=1001')),
    ('member without persona', lambda s: add_member(s, 1002, persona=False)),
    ('member kicked', lambda s: insert(s, 'realm_kick', guid=1001, ts=10.0, rank=3)),
    ('member left the roster', lambda s: s.conn.execute('DELETE FROM cast_member WHERE guid=103')),
    ('member back on the roster', lambda s: add_member(s, 103, persona=False)),
    ('same text written again', lambda s: set_json(s, 101, stored(101))),
    ('human gains a persona', lambda s: save_persona(s, make_persona(10, 'Testmaster'), source='seed',
                                                     ts=1000.0, race='Orc', class_name='Warrior')),
]


def test_reused_parses_answer_exactly_as_fresh_load_persona_through_every_row_change(roster, fresh_rows):
    with roster.transaction():
        observation = observe(roster)
        for label, change in STEPS:
            change(roster)
            warm = capture(roster, observation)
            rows, provenance._persona_rows = provenance._persona_rows, provenance._PersonaRows()
            try:
                cold = capture(roster, observation)
            finally:
                provenance._persona_rows = rows
            assert warm == cold, label
            expected = uncached_cast(roster)
            if type(expected) is Rejected:
                assert warm == expected, label
            else:
                assert type(warm) is QualificationSnapshot and warm.cast == expected, label


def test_unchanged_rows_are_never_parsed_again_and_a_changed_row_is(roster, fresh_rows, parses):
    with roster.transaction():
        observation = observe(roster)
        assert sorted(parses) == [101, 102, 103, 104, 105]
        parses.clear()
        for _ in range(3):
            assert capture(roster, observation).cast == (101, 102, 103, 104, 105)
        assert parses == []
        set_json(roster, 104, newer_schema(104))
        assert capture(roster, observation).cast == (101, 102, 103, 105)
        assert parses == [104]
        set_json(roster, 104, stored(104))
        assert capture(roster, observation).cast == (101, 102, 103, 104, 105)
        assert parses == [104, 104]


def test_unexpected_parse_faults_are_raised_every_time_and_never_remembered(roster, fresh_rows, monkeypatch):
    original = provenance.persona_from_dict
    faults = []

    def broken(value):
        if value.get('guid') == 103 and len(faults) < 2:
            faults.append(103)
            raise RecursionError('synthetic')
        return original(value)

    with roster.transaction():
        observation = observe(roster)
        fresh_rows.clear()
        monkeypatch.setattr(provenance, 'persona_from_dict', broken)
        for _ in range(2):
            with pytest.raises(RecursionError):
                capture(roster, observation)
        assert capture(roster, observation).cast == (101, 102, 103, 104, 105)
        assert faults == [103, 103]


def test_remembered_rows_stay_bounded_and_still_answer_correctly(roster, fresh_rows, monkeypatch):
    monkeypatch.setattr(fresh_rows, 'LIMIT', 3)
    with roster.transaction():
        observation = observe(roster)
        for _ in range(2):
            assert capture(roster, observation).cast == (101, 102, 103, 104, 105)
            assert len(fresh_rows._rows) == 3
        set_json(roster, 101, '{')
        assert capture(roster, observation).cast == (102, 103, 104, 105)


def test_dispatcher_capture_sees_each_persona_change_between_channel_lines(tmp_path, clock, monkeypatch):
    async def scenario():
        async with application(tmp_path, clock, monkeypatch) as native:
            store = native.app.store
            line = 0

            async def cast():
                nonlocal line
                line += 1
                await native.deliver(envelope('channel_raid_topic_v1', ts=1000000 + line,
                                              text=f'unrelated chatter {line}'))
                return native.captures[-1][0].qualification.cast

            assert await cast() == (101, 102, 103, 104, 105)
            with store.transaction():
                set_json(store, 101, '{')
            assert await cast() == (102, 103, 104, 105)
            with store.transaction():
                set_json(store, 101, stored(101))
                store.conn.execute('UPDATE cast_member SET level=79 WHERE guid=102')
            assert await cast() == (101, 103, 104, 105)
            with store.transaction():
                set_json(store, 104, stored(105, 'Ebbo'))
            await native.deliver(envelope('channel_raid_topic_v1', ts=1000000 + line + 1, text='after'))
            assert len(native.captures) == 3  # The mismatched row rejects capture.
    run(scenario())


def test_capture_cost_regression_guard_with_106_maximal_personas(roster):
    """Coarse: about 59 ms per capture before the fix and 1-2 ms after, on the dev box."""
    with roster.transaction():
        for guid in range(101, 106):
            save_persona(roster, make_persona(guid, TEST_CAST[guid][0], maximal=True), source='seed',
                         ts=1000.0, race='Orc', class_name='Warrior')
        for index in range(101):
            guid = 1001 + index
            insert(roster, 'cast_member', guid=guid, name='Synthetic', race=2, **{'class': 1},
                   gender=0, level=80, rank=3, online=0, updated=1000.0)
            save_persona(roster, make_persona(guid, 'Synthetic', maximal=True), source='seed',
                         ts=1000.0, race='Orc', class_name='Warrior')
        observation = observe(roster)
        assert len(observation.qualification.cast) == 106
        times = []
        for _ in range(15):
            start = time.perf_counter()
            result = capture(roster, observation)
            times.append(time.perf_counter() - start)
            assert result == observation.qualification
        assert statistics.median(times) < 0.015, times
