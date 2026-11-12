import asyncio

import pytest

from conftest import run
from leeroy_mind.discord import store as ds
from leeroy_mind.photos.images import stage
from leeroy_mind.talk.turns import Turn, TurnKind
from leeroy_mind.talk.pipeline import TurnMeta
from leeroy_mind.discord.transport import DiscordTarget
from photos.test_images import jpeg
from selfies.fakes import mind_app


async def photo_tasks(app):
    while app.discord._photo_tasks:
        await asyncio.gather(*list(app.discord._photo_tasks))


async def post_photo(app, channel='dm:101', text='new profile pic'):
    return await app.discord.handle({'action':'post', 'channel':channel, 'text':text,
                                    'image':stage(app.data_dir, jpeg())})


def test_photo_ownership_survives_a_full_instruction(tmp_path):
    from leeroy_mind.discord.talk import INSTRUCTION_CHARS
    app = mind_app(tmp_path, toml='[photos]\nenabled=true')
    try:
        mid = ds.post(app.store, channel='dm:101', author=app.human_guid, origin='human',
            text='x' * 500, now=app.clock.now(),
            attachment=ds.AttachmentIn('upload', 'discord/images/1.jpg', 32, 24, 100))
        ds.set_look(app.store, mid, 'a' * 599, now=app.clock.now())
        instruction = app.discord_talk.instruction(101, 'dm:101', mid, None)
        assert len(instruction) <= INSTRUCTION_CHARS
        assert 'do not assume they are in it unless they say so' in instruction
        assert instruction.index('do not assume') < instruction.index('What it shows:')
        assert instruction.index('What it shows:') < instruction.index('Answer their message:')
    finally:
        run(app.stop())


def test_photo_look_precedes_answer_without_blocking_post(tmp_path):
    async def go():
        app = mind_app(tmp_path, toml='[photos]\nenabled=true\n[talk]\naware=true')
        gate = asyncio.Event()
        app.llm._gate = gate
        app.llm._script = lambda *_: 'A woman wearing a blue coat stands by a lake.'
        queued = []
        app.engine._queue = lambda items: queued.extend(items)
        try:
            reply = await asyncio.wait_for(post_photo(app), 1)
            assert reply['ok'] and queued == []
            assert 'discord' in app.arbiter.holders
            gate.set()
            await photo_tasks(app)
            assert len(queued) == 1
            turn, meta = queued[0]
            assert 'blue coat' in turn.instruction
            assert 'do not assume' in turn.instruction
            assert 'unless' in turn.instruction
            # The actual answer prompt carries only text, including its awareness card.
            composed = await app.engine._compose_prompt(turn, meta, live=False)
            assert all('images' not in message for message in composed.messages)
            assert 'blue coat' in str(composed.messages)
            card = app.engine.live_review.block(turn, meta, close=False)
            assert 'blue coat' in str(card)
            for channel, speaker in [('discord:general', 101), ('dm:102', 102), ('guild', 101)]:
                other = Turn(TurnKind.REPLY_HUMAN, speaker, channel)
                other.to_guid = app.human_guid
                other_meta = TurnMeta('test')
                assert 'blue coat' not in str(app.engine.live_review.block(other, other_meta, close=False))
            lines = app.discord_talk.context_lines(app.store.conn, 'dm:101', None)
            assert '[photo: A woman wearing a blue coat' in lines[-1]
            assert not any('blue coat' in x for x in app.discord_talk.context_lines(app.store.conn, 'general', None))
            assert app.discord_talk.instruction(102, 'dm:102', reply['id'], None) is None
        finally:
            await app.stop()
    run(go())


@pytest.mark.parametrize('failure', ['model', 'deleted'])
def test_failed_look_answers_and_deleted_photo_does_not(tmp_path, failure):
    async def go():
        app = mind_app(tmp_path, toml='[photos]\nenabled=true')
        queued = []
        app.engine._queue = lambda items: queued.extend(items)
        app.llm.fail_next()
        try:
            reply = await post_photo(app)
            if failure == 'deleted':
                ds.delete(app.store, reply['id'], author=app.human_guid, now=app.clock.now())
            await photo_tasks(app)
            if failure == 'deleted':
                assert queued == []
            else:
                assert len(queued) == 1 and "can't make it out well" in queued[0][0].instruction
        finally:
            await app.stop()
    run(go())


def test_successful_answer_writes_one_private_memory_from_his_line(tmp_path):
    async def go():
        app = mind_app(tmp_path, toml='[photos]\nenabled=true\n[talk]\naware=true')
        app.llm._script = lambda *_: 'A dog lies on a blue rug.'
        queued = []
        app.engine._queue = lambda items: queued.extend(items)
        try:
            reply = await post_photo(app)
            await photo_tasks(app)
            turn, meta = queued[0]
            p = app.personas.get(turn.speaker)
            assert app.engine._deliver_discord(turn, meta, p, 'that rug belongs to the dog now', []) is None
            await photo_tasks(app)
            with app.store.transaction():
                rows = app.store.conn.execute("SELECT * FROM memory WHERE key=?", (f"discord_photo:{reply['id']}",)).fetchall()
                assert len(rows) == 1
                m = rows[0]
                assert m['owner'] == 101 and m['scope'] == 'private' and m['scope_with'] == app.human_guid
                assert 'dog' in m['text'] and 'chat:' in m['evidence']
            # Another member cannot use the memory hook to see this DM.
            app.discord.remember_photo(102, reply['id'])
            await photo_tasks(app)
            assert app.store.conn.execute('SELECT count(*) FROM memory WHERE key=?',
                                          (f"discord_photo:{reply['id']}",)).fetchone()[0] == 1
        finally:
            await app.stop()
    run(go())


def test_off_leaves_existing_photo_out_of_prompts(tmp_path):
    app = mind_app(tmp_path)
    try:
        mid = ds.post(app.store, channel='dm:101', author=app.human_guid, origin='human', text='hello',
            now=app.clock.now(), attachment=ds.AttachmentIn('upload','discord/images/1.jpg',32,24,100))
        ds.set_look(app.store, mid, 'A secret blue rug.', now=app.clock.now())
        instruction = app.discord_talk.instruction(101, 'dm:101', mid, None)
        assert 'photo' not in instruction and 'blue rug' not in instruction
    finally:
        run(app.stop())
