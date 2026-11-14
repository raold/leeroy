import logging
import shutil
import sqlite3

import pytest

from conftest import seed_test_cast
from leeroy_mind.outreach import store as outreach
from leeroy_mind.outreach.config import load_outreach_config
from leeroy_mind.protocol import Chat
from leeroy_mind.storage import db
from leeroy_mind.storage.store import Store


@pytest.fixture
def ready(store):
    seed_test_cast(store)
    store.set_meta('human_guid', '1')
    return store


def add(store, **over):
    now = store.clock.now()
    args = dict(guid=101, kind='life', ref='life:1', about='The new garden is growing.',
                audience='pair', weight=3, created=now, not_before=now, expires=now + 3600)
    args.update(over)
    return outreach.add(store, **args)


def line(store, **over):
    args = dict(ts=int(store.clock.now() * 1000), channel='whisper', from_guid=101,
                from_name='Arkon', to_guid=1, text='A little garden news.', map=0, zone=0)
    origin = over.pop('origin', 'mind')
    args.update(over)
    return store.insert_chat_line(Chat(**args), session_id=None, origin=origin)


@pytest.mark.parametrize('text,expected', [
    ('', False), ('[outreach]\nenabled=true', True),
    ('[outreach]\nenabled=false', False), ('[outreach]\nenabled=1', False),
    ('[outreach]\nenabled="yes"', False), ('outreach=true', False),
    ('[outreach]\nenabled=true\nextra=true', False), ('[outreach', False),
])
def test_config_is_strict_and_off_by_default(tmp_path, text, expected):
    path = tmp_path / 'mind.toml'
    assert not load_outreach_config(path).enabled
    path.write_text(text, encoding='utf-8')
    assert load_outreach_config(path).enabled is expected


def test_config_invalid_logs_codes_once_without_values(tmp_path, caplog):
    path = tmp_path / 'mind.toml'
    path.write_text('[outreach]\nenabled="private-value"', encoding='utf-8')
    with caplog.at_level(logging.WARNING):
        load_outreach_config(path)
        load_outreach_config(path)
    assert caplog.text.count('outreach_config_invalid') == 1
    assert 'private-value' not in caplog.text


def test_member_must_be_cast_with_persona_and_cannot_be_owner(ready):
    store = ready
    assert add(store) is not None
    assert add(store, guid=1) is None
    assert add(store, guid=999) is None
    with store.transaction():
        store.conn.execute('DELETE FROM persona_core WHERE guid=102')
    assert add(store, guid=102) is None
    with store.transaction():
        store.conn.execute('DELETE FROM cast_member WHERE guid=103')
    assert add(store, guid=103) is None
    with store.transaction():
        store.conn.execute("INSERT INTO persona_tier(guid,tier) VALUES (104,'world')")
    assert add(store, guid=104) is None
    # An owner row can have a persona and roster membership too; metadata decides.
    store.set_meta('human_guid', '105')
    assert add(store, guid=105) is None
    store.set_meta('human_guid', 'invalid')
    assert add(store, ref='life:2') is None


@pytest.mark.parametrize('over', [
    {'kind': 'other'}, {'audience': 'officers'}, {'ref': ''}, {'about': '  '},
    {'weight': 0}, {'weight': 11}, {'weight': float('nan')}, {'weight': True},
    {'created': float('inf')}, {'not_before': float('-inf')}, {'expires': 0},
    {'kind': 'dm_nudge', 'audience': 'guild'}, {'guid': True},
])
def test_invalid_item_is_not_inserted(ready, over):
    assert add(ready, **over) is None
    assert outreach.open_items(ready) == []


def test_dedupe_retains_original_even_after_drop_and_caps_across_kinds(ready):
    first = add(ready, about='x' * 220)
    assert len(first.about) == 200
    assert add(ready, about='Replacement') is None
    assert outreach.get(ready, first.id).about == 'x' * 200
    assert outreach.drop(ready, first.id, 'superseded')
    assert add(ready) is None
    for i, kind in enumerate(['life', 'plan', 'followup', 'his_day', 'birthday']):
        assert add(ready, ref=f'{kind}:{i+2}', kind=kind, weight=i+1).state == 'open'
    new = add(ready, kind='miss_you', ref='miss_you:today', weight=6)
    assert new.state == 'open'
    assert len(outreach.open_items(ready, 101)) == 5
    assert ready._one("SELECT ended FROM outreach_item WHERE ref='life:2'")['ended'] == 'capped'
    low = add(ready, ref='life:lower', weight=.1)
    assert (low.state, low.ended) == ('dropped', 'capped')
    assert add(ready, guid=102) is not None
    assert len(outreach.open_items(ready)) == 6


def test_cap_keeps_oldest_at_equal_weight_and_cap_does_not_erase_dedupe(ready):
    old = [add(ready, ref=f'life:{i}') for i in range(5)]
    new = add(ready, ref='life:new')
    assert new.ended == 'capped'
    assert outreach.open_items(ready) == old
    outreach.drop(ready, old[0].id, 'he_raised')
    assert add(ready, ref='life:new') is None


@pytest.mark.parametrize('via,expected', [
    ('whisper', 'dm_nudge'), ('dm', 'life'), ('guild', 'his_day'), ('party', 'his_day'),
    ('officer', None), ('discord:general', None), ('gdm:1', None), ('say', None),
])
def test_best_fences_pair_and_nudge_by_actual_medium(ready, via, expected):
    add(ready, kind='dm_nudge', ref='dm_nudge:7', weight=6)
    add(ready, ref='life:2', weight=4)
    add(ready, kind='his_day', ref='his_day:3', audience='guild', weight=2)
    found = outreach.best(ready, 101, via=via, now=ready.clock.now())
    assert (found.kind if found else None) == expected


def test_best_respects_due_expiry_weight_then_age(ready):
    now = ready.clock.now()
    future = add(ready, ref='life:future', weight=10, not_before=now+10)
    expired = add(ready, ref='life:expired', weight=10, not_before=now-10, expires=now)
    younger = add(ready, ref='life:younger', created=now-2)
    older = add(ready, ref='life:older', created=now-3)
    assert outreach.best(ready, 101, via='dm', now=now) == older
    assert outreach.best(ready, 101, via='dm', now=now+10) == future
    assert outreach.expire(ready, now) == 1
    assert outreach.get(ready, expired.id).ended == 'expired'
    assert outreach.expire(ready, now) == 0
    assert outreach.get(ready, younger.id).state == 'open'


@pytest.mark.parametrize('via,channel', [('whisper', 'whisper'), ('dm', 'dm:101'),
                                        ('guild', 'guild'), ('party', 'party')])
def test_delivery_is_terminal_across_both_clocks_and_same_receipt_is_idempotent(ready, via, channel):
    item = add(ready, audience='guild')
    now = ready.clock.now()
    with ready.transaction():
        receipt = line(ready, channel=channel, to_guid=0 if via in ('guild', 'party') else 1)
        assert outreach.told(ready, item.id, via=via, line_id=receipt, now=now)
    told = outreach.get(ready, item.id)
    assert (told.state, told.ended, told.told_via, told.told_at, told.line_id) == (
        'told', 'told', via, now, receipt)
    for medium in ['whisper', 'dm', 'guild', 'party']:
        assert outreach.best(ready, 101, via=medium, now=now) is None
    with ready.transaction():
        assert outreach.told(ready, item.id, via=via, line_id=receipt, now=now+1)
        assert not outreach.told(ready, item.id, via=via, line_id=line(ready, channel=channel), now=now)
    assert not outreach.drop(ready, item.id, 'superseded')
    assert add(ready) is None


@pytest.mark.parametrize('via,over', [
    ('guild', {'channel': 'guild'}), ('party', {'channel': 'party'}),
    ('whisper', {'from_guid': 102}), ('whisper', {'to_guid': 102}),
    ('whisper', {'origin': 'human'}), ('whisper', {'channel': 'officer'}),
    ('dm', {'channel': 'dm:102'}), ('dm', {'channel': 'discord:general'}),
    ('dm', {'channel': 'gdm:1'}), ('dm', {'channel': 'whisper'}),
])
def test_receipt_requires_her_actual_private_line_to_owner(ready, via, over):
    item = add(ready)
    with ready.transaction():
        receipt = line(ready, **over)
        assert not outreach.told(ready, item.id, via=via, line_id=receipt, now=ready.clock.now())
    assert outreach.get(ready, item.id).state == 'open'


def test_nudge_cannot_be_delivered_in_dm_and_line_cannot_credit_two_items(ready):
    nudge = add(ready, kind='dm_nudge', ref='dm_nudge:7')
    ordinary = add(ready, ref='life:2')
    other = add(ready, ref='life:3')
    with ready.transaction():
        receipt = line(ready, channel='dm:101')
        assert not outreach.told(ready, nudge.id, via='dm', line_id=receipt, now=ready.clock.now())
        assert outreach.told(ready, ordinary.id, via='dm', line_id=receipt, now=ready.clock.now())
        assert not outreach.told(ready, other.id, via='dm', line_id=receipt, now=ready.clock.now())


def test_receipt_and_chat_line_rollback_together(ready):
    item = add(ready)
    now = ready.clock.now()
    with pytest.raises(RuntimeError, match='receipt_requires_transaction'):
        outreach.told(ready, item.id, via='whisper', line_id=1, now=now)
    with pytest.raises(RuntimeError, match='after receipt'):
        with ready.transaction():
            receipt = line(ready)
            assert outreach.told(ready, item.id, via='whisper', line_id=receipt, now=now)
            raise RuntimeError('after receipt')
    assert ready.chat_lines([receipt]) == []
    assert outreach.get(ready, item.id).state == 'open'
    assert outreach.best(ready, 101, via='dm', now=now) == item


@pytest.mark.parametrize('ending', ['expired', 'he_raised', 'superseded', 'capped'])
def test_late_delivery_cannot_reopen_an_ended_item(ready, ending):
    item = add(ready)
    outreach.drop(ready, item.id, ending)
    with ready.transaction():
        receipt = line(ready)
        assert not outreach.told(ready, item.id, via='whisper', line_id=receipt, now=ready.clock.now())
    assert outreach.get(ready, item.id).ended == ending
    assert ready.chat_lines([receipt])


def test_drafts_do_not_change_list_and_logs_never_contain_about(ready, caplog):
    with caplog.at_level(logging.INFO):
        item = add(ready, about='private surprise text')
        assert outreach.best(ready, 101, via='whisper', now=ready.clock.now()) == item
        assert outreach.get(ready, item.id).state == 'open'
        outreach.drop(ready, item.id, 'he_raised')
    assert 'outreach_added guid=101 kind=life' in caplog.text
    assert 'outreach_ended guid=101 kind=life why=he_raised' in caplog.text
    assert 'private surprise text' not in caplog.text


def test_schema_constraints_preserve_receipt_foreign_key_and_state(ready):
    item = add(ready)
    for sql, params in [
        ("UPDATE outreach_item SET state='told' WHERE id=?", (item.id,)),
        ("UPDATE outreach_item SET state='dropped' WHERE id=?", (item.id,)),
        ("UPDATE outreach_item SET weight=11 WHERE id=?", (item.id,)),
        ("UPDATE outreach_item SET expires=not_before WHERE id=?", (item.id,)),
        ("UPDATE outreach_item SET state='told',ended='told',told_via='whisper',told_at=1,line_id=999 WHERE id=?", (item.id,)),
    ]:
        with pytest.raises(sqlite3.IntegrityError):
            with ready.transaction():
                ready.conn.execute(sql, params)
    assert ready._all('PRAGMA foreign_key_check') == []


@pytest.mark.parametrize('via', ['guild', 'party'])
def test_public_receipt_rejects_a_line_addressed_to_someone_else(ready, via):
    item = add(ready, audience='guild')
    with ready.transaction():
        receipt = line(ready, channel=via, to_guid=102)
        assert not outreach.told(ready, item.id, via=via, line_id=receipt, now=ready.clock.now())
    assert outreach.get(ready, item.id).state == 'open'


def test_cap_and_insert_roll_back_as_one_unit(ready):
    old = [add(ready, ref=f'life:{i}', weight=1) for i in range(5)]
    with pytest.raises(RuntimeError, match='after cap'):
        with ready.transaction():
            assert add(ready, ref='life:new', weight=10).state == 'open'
            raise RuntimeError('after cap')
    assert outreach.open_items(ready) == old
    assert ready._one("SELECT id FROM outreach_item WHERE ref='life:new'") is None


def test_migration_preserves_previous_lines_and_receipts_survive_reopen(tmp_path, clock, monkeypatch):
    before = tmp_path / 'before'
    before.mkdir()
    for migration in db.MIGRATIONS_DIR.glob('*.sql'):
        if migration.name != '0045_outreach.sql':
            shutil.copy2(migration, before / migration.name)
    path = tmp_path / 'upgrade.db'
    with monkeypatch.context() as patched:
        patched.setattr(db, 'MIGRATIONS_DIR', before)
        old = Store(db.open_db(path), clock=clock)
        try:
            seed_test_cast(old)
            old.set_meta('human_guid', '1')
            previous = line(old)
        finally:
            old.close()
    store = Store(db.open_db(path), clock=clock)
    try:
        assert store.chat_lines([previous])[0].text == 'A little garden news.'
        assert outreach.open_items(store) == []
        item = add(store)
        with store.transaction():
            receipt = line(store, channel='dm:101')
            assert outreach.told(store, item.id, via='dm', line_id=receipt, now=clock.now())
        assert store._all('PRAGMA foreign_key_check') == []
    finally:
        store.close()
    reopened = Store(db.open_db(path), clock=clock)
    try:
        assert outreach.get(reopened, item.id).line_id == receipt
        assert outreach.best(reopened, 101, via='whisper', now=clock.now()) is None
        assert add(reopened) is None
    finally:
        reopened.close()
