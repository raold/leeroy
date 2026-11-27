"""World facts are recorded for a guild past 128 and without a speech blocklist; refusals are said."""
import asyncio
import logging
import sqlite3

import pytest

from conftest import make_persona
from leeroy_mind.personas.io import save_persona
from leeroy_mind.realm import service
from leeroy_mind.realm.records import FactRow, Rejected
from leeroy_mind.realm.screen import Screen
from test_realm_live_proof import insert
from test_realm_source_admission import application, counts, envelope, run

PVP = 'pvp_result_v1'


def grow_guild(store, members):
    with store.transaction():
        for index in range(members):
            guid = 1000 + index
            insert(store, 'cast_member', guid=guid, name='Synthetic', race=2, **{'class': 1},
                   gender=0, level=80, rank=3, online=1, updated=1000.0)
            save_persona(store, make_persona(guid, 'Synthetic'), source='seed', ts=1000.0,
                         race='Orc', class_name='Warrior')


def rejections(caplog):
    return [record for record in caplog.records
            if record.getMessage().startswith('realm_source_rejected')]


def test_a_guild_past_128_records_world_facts_while_the_speech_screen_is_not_ready(
        tmp_path, clock, monkeypatch, caplog):
    async def scenario():
        async with application(tmp_path, clock, monkeypatch) as native:
            app = native.app
            grow_guild(app.store, 140)  # 145 qualified members, as the live guild has
            # No blocklist, the owner's choice: the screen that gates every rival line is shut.
            assert not (tmp_path / 'realm' / 'blocklist.txt').exists()
            assert not Screen(tmp_path, lambda: 'Synthetic Guild', lambda: app.realm.ports.human()).ready
            with caplog.at_level(logging.INFO):
                await native.deliver(envelope(PVP))
            assert type(native.results[0]) is FactRow
            assert len(native.captures[0][0].qualification.cast) == 145
            assert counts(app.store)[:3] == (1, 1, 1)
            assert not rejections(caplog)
            assert not any(writer.sent for writer in native.writers)  # recorded, nothing said
    run(scenario())


def test_refused_sources_are_counted_and_said_once_a_minute_by_code(
        tmp_path, clock, monkeypatch, caplog):
    async def scenario():
        async with application(tmp_path, clock, monkeypatch) as native:
            app = native.app
            monkeypatch.setattr(service, 'capture_qualification_in_unit',
                                lambda *a, **kw: Rejected('graph_limit'))
            with caplog.at_level(logging.INFO):
                for n in range(3):
                    await native.deliver(envelope(PVP, ts=1000000 + n))
                first = rejections(caplog)
                assert len(first) == 1 and first[0].levelno == logging.WARNING
                assert first[0].getMessage() == (
                    'realm_source_rejected n=1 codes=qualification:pvp_kill:graph_limit=1')
                await clock.advance(service.REJECTION_LOG_S + 1)
                await app.realm.tick()
                said = rejections(caplog)
                assert len(said) == 2 and said[1].getMessage() == (
                    'realm_source_rejected n=2 codes=qualification:pvp_kill:graph_limit=2')
                await app.realm.tick()
                assert len(rejections(caplog)) == 2  # nothing new, nothing said
            assert counts(app.store) == (0, 0, 0, 0, 0)
    run(scenario())


def test_an_expected_refusal_is_an_info_line_and_names_where_it_stopped(
        tmp_path, clock, monkeypatch, caplog):
    async def scenario():
        async with application(tmp_path, clock, monkeypatch) as native:
            with caplog.at_level(logging.INFO):
                await native.deliver(envelope('duel_result_v1', city=True))
                await native.deliver(envelope('channel_raid_topic_v1', text='unrelated public chatter'))
                await clock.advance(service.REJECTION_LOG_S + 1)
                await native.app.realm.tick()
            said = rejections(caplog)
            assert said[0].levelno == logging.INFO
            assert said[0].getMessage() == 'realm_source_rejected n=1 codes=event:duel_end:private_evidence=1'
            assert len(said) == 2 and 'admission:channel_chat:' in said[1].getMessage()
            assert said[1].levelno == logging.INFO  # talk off the topic is the rule working
            assert 'unrelated' not in caplog.text
    run(scenario())


def test_self_kill_keeps_raw_evidence_without_claiming_a_pvp_victory(
        tmp_path, clock, monkeypatch, caplog):
    async def scenario():
        async with application(tmp_path, clock, monkeypatch) as native:
            with caplog.at_level(logging.INFO):
                await native.deliver(envelope(PVP, killer=101, victim=101))
            assert counts(native.app.store) == (0, 0, 0, 0, 0)
            with native.app.store.transaction():
                assert native.app.store.conn.execute(
                    "SELECT count(*) FROM game_event WHERE type='pvp_kill'").fetchone()[0] == 1
            assert not native.failures and not native.captures
            said = rejections(caplog)
            assert len(said) == 1 and said[0].levelno == logging.INFO
            assert said[0].getMessage() == 'realm_source_rejected n=1 codes=event:pvp_kill:self_kill=1'
            assert 'invalid_row' not in caplog.text
    run(scenario())


@pytest.mark.parametrize('outcome', ['released', 'changed_owner', 'exhausted'])
def test_real_database_contention_retries_only_a_current_observation(
        tmp_path, clock, monkeypatch, caplog, outcome):
    async def scenario():
        async with application(tmp_path, clock, monkeypatch) as native:
            store, realm = native.app.store, native.app.realm
            rival = sqlite3.connect(tmp_path / 'mind.db', isolation_level=None, timeout=0)
            contended = asyncio.Event()
            attempts = []
            write_first, reduce = store.write_first, realm._on_source_observation

            def acquire(**kwargs):
                attempts.append(len(attempts) + 1)
                try:
                    return write_first(**kwargs)
                except sqlite3.OperationalError as error:
                    assert error.sqlite_errorcode & 0xff == sqlite3.SQLITE_BUSY
                    contended.set()
                    raise

            async def release_writer():
                await contended.wait()
                # Retrying must yield only after the entire failed unit rolls back.
                assert not store.in_unit
                if outcome != 'exhausted':
                    rival.execute('ROLLBACK')
                    if outcome == 'changed_owner':
                        realm._generation += 1

            monkeypatch.setattr(store, 'write_first', acquire)
            async def locked_reduce(observation):
                # The raw row and qualification already committed. Another real
                # SQLite connection now holds the lock needed by the owned reducer.
                rival.execute('BEGIN IMMEDIATE')
                try:
                    return await reduce(observation)
                finally:
                    if rival.in_transaction:
                        rival.execute('ROLLBACK')

            monkeypatch.setattr(realm, '_on_source_observation', locked_reduce)
            released = asyncio.create_task(release_writer())
            try:
                with caplog.at_level(logging.INFO):
                    await native.deliver(envelope(PVP))
                await released
            finally:
                released.cancel()
                await asyncio.gather(released, return_exceptions=True)
                rival.close()
            assert attempts == list(range(1, (service.SOURCE_WRITE_ATTEMPTS if outcome == 'exhausted' else 2) + 1))
            if outcome == 'released':
                assert not native.failures and len(native.results) == 1
                assert type(native.results[0]) is FactRow
                assert counts(store)[:3] == (1, 1, 1)
            else:
                assert counts(store) == (0, 0, 0, 0, 0)
                if outcome == 'changed_owner':
                    assert native.results == [Rejected('proof_changed')] and not native.failures
                else:
                    assert native.failures == [sqlite3.OperationalError] and not native.results
            assert 'realm_source_write_retry type=pvp_kill attempt=1 code=busy' in caplog.text
            assert not any(writer.sent for writer in native.writers)
    run(scenario())
