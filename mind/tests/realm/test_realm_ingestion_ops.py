"""World facts are recorded for a guild past 128 and without a speech blocklist; refusals are said."""
import logging

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
            assert 'unrelated' not in caplog.text
    run(scenario())
