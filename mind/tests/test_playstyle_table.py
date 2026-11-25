"""The play-style table (playstyle_table): one command for the whole cast, and native styles each member
whenever she is free, instead of one bot_do a member at his login (playstyle/README.md)."""
import asyncio
import json
import logging
from dataclasses import replace
from types import SimpleNamespace

from conftest import mem, run
from leeroy_mind.guild.protocol import BotDo, PlayStyleEntry, PlayStyleTable, register
from leeroy_mind.personas.io import PersonaCache
from leeroy_mind.playstyle import style
from leeroy_mind.playstyle.service import KEY, TABLE_FEATURE, PlayStyles
from leeroy_mind.protocol import Ack, Boss, Cast, Group, Hello, Login
from leeroy_mind.testing.wire import bridge_send
from test_playstyle import app_for

TABLE_HELLO = ('playstyle', TABLE_FEATURE)


def table_rig(store, tmp_path, owned=(), *, refuse=lambda cmd: ''):
    register()
    (tmp_path/'mind.toml').write_text('[playstyle]\nenabled=true')
    app = app_for(store, tmp_path)
    store.set_meta(KEY, json.dumps(list(owned)))
    service = PlayStyles(app)
    sent = []

    @bridge_send
    async def send(cmd, **kwargs):
        sent.append(cmd)
        if error := refuse(cmd):
            return Ack(0, cmd.id, False, error)
        if isinstance(cmd, BotDo):
            service.jobs.feed(SimpleNamespace(id=cmd.id, kind='bot_do', state='done', reason='', skipped=(),
                                              guids=cmd.guids))
        return Ack(0, cmd.id, True, '')
    app.bridge = SimpleNamespace(send=send)
    return app, service, sent


async def drain(service):
    """Run the service until it has been idle for a while: nothing pending, no table wanted."""
    task = asyncio.create_task(service.run())
    idle = 0
    for _ in range(2000):
        await asyncio.sleep(0)
        idle = idle + 1 if not service.pending and not service.wake.is_set() and not service.table_due else 0
        if idle >= 50:
            break
    task.cancel()
    result, = await asyncio.gather(task, return_exceptions=True)
    assert isinstance(result, asyncio.CancelledError), result


def tables(sent):
    return [cmd for cmd in sent if isinstance(cmd, PlayStyleTable)]


def bot_dos(sent):
    return [(cmd.action, cmd.guids) for cmd in sent if isinstance(cmd, BotDo)]


def online_cast(app):
    return Cast(0, tuple(replace(m, online=True) for m in app.world.members.values()))


def test_a_hello_that_lists_the_table_gets_the_whole_cast_in_one_command(store, tmp_path, caplog):
    async def scenario():
        app, service, sent = table_rig(store, tmp_path)
        await service.connection(True)
        await service.event(Hello(0, 'fake', '1', 21, 1, TABLE_HELLO))
        await service.event(Login(0, 1, True))
        await drain(service)
        assert bot_dos(sent) == [] and len(tables(sent)) == 1
        styles = tables(sent)[0].styles
        assert styles == tuple(PlayStyleEntry(g, style(app.personas.get(g)).param) for g in (101, 102, 103, 104, 105))
        assert service.owned == set() and store.get_meta(KEY) == '[]'   # native keeps its own journals
        await service.event(Login(0, 103, False))                        # unchanged: nothing sent again
        await service.event(online_cast(app))
        await drain(service)
        assert len(sent) == 1
        await service.event(Login(0, 1, True))                           # his login always resyncs it
        await drain(service)
        assert len(sent) == 2 and tables(sent)[1].styles == styles
    caplog.set_level(logging.INFO, 'leeroy_mind')
    run(scenario())
    lines = [r.getMessage() for r in caplog.records if 'playstyle' in r.getMessage()]
    assert lines == ['playstyle_table_sent: styles=5 on=1 owner=1'] * 2


def test_the_table_goes_while_he_is_away_and_waits_for_a_cast(store, tmp_path):
    async def scenario():
        app, service, sent = table_rig(store, tmp_path)
        app.world.human_online = False
        members, app.world.members = app.world.members, {}
        await service.connection(True)
        await service.event(Hello(0, 'fake', '1', 21, 0, TABLE_HELLO))
        await drain(service)
        assert sent == []                                                # no cast yet: nothing to send
        app.world.members = members
        await service.event(online_cast(app))
        await drain(service)
        assert len(tables(sent)) == 1 and len(tables(sent)[0].styles) == 5 and bot_dos(sent) == []
    run(scenario())


def test_a_changed_persona_or_switch_sends_the_table_again(store, tmp_path):
    def shaped(persona, role, score):
        five = persona.big_five
        return replace(persona, play=replace(persona.play, raid_role=role, skill='solid'),
                       big_five=replace(five, conscientiousness=replace(five.conscientiousness, score=score),
                                        neuroticism=replace(five.neuroticism, score=score)))

    async def scenario():
        app, service, sent = table_rig(store, tmp_path)
        cache = PersonaCache(store)
        tank, careless = shaped(cache.get(102), 'tank', 10), shaped(cache.get(102), 'ranged', 1)
        assert style(tank).param == '0:1' and style(careless).param.endswith(':0')
        current = {'102': tank}
        app.personas = SimpleNamespace(get=lambda g: current['102'] if g == 102 else cache.get(g))
        await service.connection(True)
        await service.event(Hello(0, 'fake', '1', 21, 1, TABLE_HELLO))
        await drain(service)
        assert dict((e.guid, e.param) for e in tables(sent)[0].styles)[102] == '0:1'
        current['102'] = careless
        await service.event(Login(0, 102, False))
        await drain(service)
        assert len(tables(sent)) == 2
        assert dict((e.guid, e.param) for e in tables(sent)[1].styles)[102] == style(careless).param
        (tmp_path/'mind.toml').write_text('[playstyle]\nenabled=false')
        await service.event(Login(0, 104, False))
        await drain(service)
        assert len(tables(sent)) == 3 and tables(sent)[2].styles == ()  # off: native takes the table's styles back
        assert bot_dos(sent) == []
    run(scenario())


def test_receipts_from_one_bot_do_a_member_are_restored_before_the_table(store, tmp_path):
    async def scenario():
        app, service, sent = table_rig(store, tmp_path, owned=[101, 102])
        await service.connection(True)
        await service.event(Hello(0, 'fake', '1', 21, 1, TABLE_HELLO))
        await drain(service)
        assert [type(c).__name__ for c in sent] == ['BotDo', 'BotDo', 'PlayStyleTable']
        assert bot_dos(sent) == [('restore', (101,)), ('restore', (102,))]   # never reapplied one by one
        assert service.owned == set()
    run(scenario())


def test_a_table_refused_for_itself_keeps_one_bot_do_a_member_until_his_next_login(store, tmp_path, caplog):
    async def scenario():
        refusing = {'on': True}
        app, service, sent = table_rig(
            store, tmp_path,
            refuse=lambda cmd: 'playstyle_table_off' if isinstance(cmd, PlayStyleTable) and refusing['on'] else '')
        await service.connection(True)
        await service.event(Hello(0, 'fake', '1', 21, 1, TABLE_HELLO))
        await service.event(online_cast(app))
        await drain(service)
        assert len(tables(sent)) == 1 and service.mode == 'legacy'
        assert sorted(bot_dos(sent)) == [('playstyle', (g,)) for g in (101, 102, 103, 104, 105)]
        assert service.owned == {101, 102, 103, 104, 105}
        sent.clear()
        await service.event(Login(0, 103, False))                        # the old path for this connection
        await drain(service)
        assert bot_dos(sent) == [('playstyle', (103,))] and not tables(sent)
        sent.clear()
        refusing['on'] = False
        await service.event(Login(0, 1, True))                           # his login tries the table again,
        await drain(service)                                             # after undoing the one-bot_do styles
        assert [type(c).__name__ for c in sent] == ['BotDo'] * 5 + ['PlayStyleTable']
        assert sorted(bot_dos(sent)) == [('restore', (g,)) for g in (101, 102, 103, 104, 105)]
        assert service.mode == 'table' and service.owned == set()
    caplog.set_level(logging.INFO, 'leeroy_mind')
    run(scenario())
    assert 'playstyle_table_refused: error=playstyle_table_off mode=legacy' in caplog.text


def test_a_table_refused_as_one_bot_do_would_be_waits_for_his_login(store, tmp_path):
    async def scenario():
        refusing = {'error': 'playstyle_off'}
        app, service, sent = table_rig(store, tmp_path, refuse=lambda cmd: refusing['error'])
        await service.connection(True)
        await service.event(Hello(0, 'fake', '1', 21, 1, TABLE_HELLO))
        await drain(service)
        assert len(sent) == 1 and service.mode == 'off'
        await service.event(Login(0, 102, False))
        await service.event(Group(0, 1, False, (1, 101, 102)))
        await service.event(online_cast(app))
        await drain(service)
        assert len(sent) == 1                                            # neither a table nor a bot_do
        refusing['error'] = ''
        await service.event(Login(0, 1, True))
        await drain(service)
        assert len(tables(sent)) == 2 and service.mode == 'table' and bot_dos(sent) == []
    run(scenario())


def test_in_table_mode_groups_raids_and_bosses_queue_nothing_but_old_receipts(store, tmp_path):
    async def scenario():
        app, service, sent = table_rig(store, tmp_path)
        await service.connection(True)
        await service.event(Hello(0, 'fake', '1', 21, 1, TABLE_HELLO))
        await drain(service)
        service.owned.add(104)                                           # an old receipt still held
        await service.event(Group(0, 1, False, (1, 101, 102)))
        await service.event(Boss(0, 0, 1, 4, 'Invented world boss', 'engaged', (101,)))
        assert not service.pending
        await service.event(Group(0, 1, True, (1, 101, 102, 104)))
        assert service.pending == {104: True}
    run(scenario())


def test_a_lost_send_or_a_disconnect_leaves_the_table_to_the_next_connection(store, tmp_path, caplog):
    async def scenario():
        app, service, sent = table_rig(store, tmp_path)

        async def lost(cmd, **kwargs):
            raise ConnectionError('private transport text')
        app.bridge = SimpleNamespace(send=lost)
        await service.connection(True)
        await service.event(Hello(0, 'fake', '1', 21, 1, TABLE_HELLO))
        await drain(service)
        assert service.mode is None and service.sent is None
        await service.connection(False)
        assert not service.table_supported and service.mode is None
    caplog.set_level(logging.INFO, 'leeroy_mind')
    run(scenario())
    assert 'playstyle_table_failed: error=ConnectionError' in caplog.text and 'private' not in caplog.text


def test_malformed_receipts_send_no_table(store, tmp_path):
    async def scenario():
        app, service, sent = table_rig(store, tmp_path)
        store.set_meta(KEY, '"private canary"')
        broken = PlayStyles(app)
        await broken.connection(True)
        await broken.event(Hello(0, 'fake', '1', 21, 1, TABLE_HELLO))
        await drain(broken)
        assert sent == []
    run(scenario())


def test_an_older_native_gets_no_table(store, tmp_path):
    async def scenario():
        app, service, sent = table_rig(store, tmp_path)
        await service.connection(True)
        await service.event(Hello(0, 'fake', '1', 21, 1, ('playstyle',)))
        await service.event(online_cast(app))
        await drain(service)
        assert not tables(sent) and len(bot_dos(sent)) == 5
    run(scenario())


def test_a_large_cast_is_one_command_instead_of_one_a_member(store, tmp_path):
    """The measured case: a 145-member cast at his login (the live cast's size on 2026-11-25)."""
    async def scenario():
        cast = {g: mem(g) for g in range(2000, 2145)}
        app, service, sent = table_rig(store, tmp_path)
        template = app.personas.get(101)
        personas = SimpleNamespace(get=lambda g: replace(template, guid=g) if g in cast else None)
        app.world.members, app.world.online, app.personas = cast, set(cast), personas
        await service.connection(True)
        await service.event(Hello(0, 'fake', '1', 21, 1, TABLE_HELLO))
        await service.event(Login(0, 1, True))
        await drain(service)
        assert len(sent) == 1 and len(tables(sent)[0].styles) == 145
        legacy_app, legacy, legacy_sent = table_rig(store, tmp_path)
        legacy_app.world.members, legacy_app.world.online, legacy_app.personas = cast, set(cast), personas
        await legacy.connection(True)
        await legacy.event(Hello(0, 'fake', '1', 21, 1, ('playstyle',)))
        await legacy.event(Cast(0, tuple(cast.values())))
        await legacy.event(Login(0, 1, True))
        await drain(legacy)
        assert len(legacy_sent) == 145 and all(isinstance(c, BotDo) for c in legacy_sent)
    run(scenario())


def test_real_mind_fake_bridge_sends_one_table_and_no_bot_do(tmp_path, clock, caplog):
    from leeroy_mind.app import MindApp
    from leeroy_mind.config import load_config
    from leeroy_mind.testing.fake_bridge import CORE_COMMAND_TYPES, FakeBridge
    from leeroy_mind.testing.fake_llm import FakeLLM
    from test_app import CAST, HUMAN, hello, seed_db, toml

    async def scenario():
        bridge = FakeBridge([{**hello(HUMAN), 'features': ['playstyle', TABLE_FEATURE]}, CAST],
                            known_types=CORE_COMMAND_TYPES | {'bot_do', 'playstyle_table'})
        port = await bridge.start()
        (tmp_path/'mind.toml').write_text(toml(port, chattiness=0)+'\n[playstyle]\nenabled=true\n')
        seed_db(tmp_path/'mind.db', clock)
        cfg = load_config(tmp_path/'mind.toml')
        llm = FakeLLM(models={cfg.models.live, cfg.models.reflection, cfg.models.embedding})
        app = MindApp(cfg, tmp_path, llm=llm, clock=clock)
        runner = asyncio.create_task(app.run())
        try:
            async with asyncio.timeout(5):
                while 'playstyle_table_sent' not in caplog.text:
                    await asyncio.sleep(.01)
            await app.stop()
        finally:
            await app.stop()
            await runner
            await bridge.stop()
        styled = [c for c in bridge.commands if c['type'] == 'playstyle_table']
        assert len(styled) == 1 and {s['guid'] for s in styled[0]['styles']} == set(app.world.cast_guids())
        assert styled[0]['styles'] and all(c['type'] != 'bot_do' for c in bridge.commands)
    caplog.set_level(logging.INFO, 'leeroy_mind')
    run(scenario())
