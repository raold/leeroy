"""His video in a DM: the desk adopts it with the post, the look sees its stills, and the member
answers knowing it is a short video she saw stills of (videos/README.md). ffmpeg is faked."""
import asyncio
import json

import pytest

from conftest import run
from leeroy_mind.discord import store as ds
from leeroy_mind.endpoint import BadRequest, discord_args
from leeroy_mind.videos import files
from selfies.fakes import mind_app
from videos.test_video_files import MP4, TOOLS, FakeTools

ON = '[photos]\nenabled=true\n[videos]\nenabled=true\n[talk]\naware=true'


@pytest.fixture(autouse=True)
def fake_ffmpeg(monkeypatch):
    monkeypatch.setattr(files, '_run', FakeTools(ms=12_400))


async def photo_tasks(app):
    while app.discord._photo_tasks:
        await asyncio.gather(*list(app.discord._photo_tasks))


def video_post(app, text='look at this', channel='dm:101'):
    token, _ = files.stage(app.data_dir, MP4, 'video/mp4', TOOLS)
    return discord_args({'action': 'post', 'channel': channel, 'text': text, 'video': token})


def test_post_arguments_take_one_video_token():
    token = 'a' * 32
    args = {'action': 'post', 'channel': 'dm:101', 'text': '', 'video': token}
    assert discord_args(args) == args
    for bad in ({'video': '../x'}, {'video': 7}, {'image': 'b' * 32}, {'voice': 'b' * 32}):
        with pytest.raises(BadRequest):
            discord_args(args | bad)


def test_his_video_is_stored_looked_at_and_answered_as_a_video(tmp_path):
    async def go():
        app = mind_app(tmp_path, toml=ON)
        prompts = []
        def look(model, messages):
            prompts.append(messages[-1])
            return 'A cat jumps onto a kitchen counter and knocks a cup over.'
        app.llm._script = look
        queued = []
        app.engine._queue = lambda items: queued.extend(items)
        try:
            reply = await app.discord.handle(video_post(app, text=''))
            assert reply['ok'], reply
            mid = reply['id']
            await photo_tasks(app)
            photo = ds.attachments(app.store.conn, [mid])[mid]
            clip = files.by_messages(app.store.conn, [mid])[mid]
            assert photo['kind'] == 'upload' and photo['path'] == f'discord/images/{mid}.jpg'
            assert clip['path'] == f'discord/videos/{mid}.mp4' and clip['ms'] == 12_400 and clip['frames'] == 3
            assert (app.data_dir / clip['path']).read_bytes() == MP4
            day = app.clock.local().date().isoformat()
            assert app.store.get_meta(files.DAY_KEY.format(day)) == '1'
            # the look saw the stills as one video, in time order
            assert 'stills from one short video' in prompts[0]['content'] and len(prompts[0]['images']) == 1
            assert len(queued) == 1
            instruction = queued[0][0].instruction
            assert 'sent a short video (12 seconds)' in instruction
            assert 'cannot hear its sound' in instruction and 'What happens in it: A cat jumps' in instruction
            assert 'posted a photo' not in instruction
            lines = app.discord_talk.context_lines(app.store.conn, 'dm:101', None)
            assert '[video, 12 seconds: A cat jumps onto a kitchen counter' in lines[-1]
            # her memory says video, kept private to their DM
            turn, meta = queued[0]
            p = app.personas.get(turn.speaker)
            assert app.engine._deliver_discord(turn, meta, p, 'that cat has no regrets', []) is None
            await photo_tasks(app)
            with app.store.transaction():
                memory = app.store.conn.execute('SELECT text, scope FROM memory WHERE key=?',
                                                (f'discord_photo:{mid}',)).fetchone()
            assert memory is not None and 'sent me a video: A cat jumps' in memory['text']
            assert memory['scope'] == 'private'
        finally:
            await app.stop()
    run(go())


@pytest.mark.parametrize('toml,code', [('[photos]\nenabled=true', 'videos_off'), ('[videos]\nenabled=true', 'photos_off')])
def test_a_video_needs_both_switches(tmp_path, toml, code):
    async def go():
        app = mind_app(tmp_path, toml=toml)
        try:
            post = video_post(app)
            before = sorted(p.name for p in (app.data_dir / files.INCOMING).iterdir())
            reply = await app.discord.handle(post)
            assert not reply['ok']
            assert app.store.conn.execute('SELECT count(*) FROM discord_message').fetchone()[0] == 0
            assert sorted(p.name for p in (app.data_dir / files.INCOMING).iterdir()) == before
        finally:
            await app.stop()
    run(go())


def test_the_day_allowance_and_a_reused_token_write_no_post(tmp_path):
    async def go():
        app = mind_app(tmp_path, toml=ON)
        app.engine._queue = lambda items: None
        try:
            post = video_post(app)
            assert (await app.discord.handle(post))['ok']
            again = await app.discord.handle(post)
            assert not again['ok'] and 'already used' in again['error']
            day = app.clock.local().date().isoformat()
            app.store.set_meta(files.DAY_KEY.format(day), str(files.DAY_MAX))
            full = await app.discord.handle(video_post(app))
            assert not full['ok'] and '50-video' in full['error']
            assert app.store.conn.execute('SELECT count(*) FROM discord_message').fetchone()[0] == 1
            await photo_tasks(app)
        finally:
            await app.stop()
    run(go())


def test_a_failed_look_still_answers_his_video(tmp_path):
    async def go():
        app = mind_app(tmp_path, toml=ON)
        queued = []
        app.engine._queue = lambda items: queued.extend(items)
        app.llm.fail_next()
        try:
            assert (await app.discord.handle(video_post(app)))['ok']
            await photo_tasks(app)
            assert len(queued) == 1
            instruction = queued[0][0].instruction
            assert 'sent a short video (12 seconds)' in instruction and "can't make it out well" in instruction
        finally:
            await app.stop()
    run(go())


def test_older_code_reading_the_post_sees_the_stills_as_a_photo(tmp_path):
    """Rollback: code from before videos reads the attachment row only, so his video is a photo of
    its stills there; the facts it does not know are in meta, not a table."""
    async def go():
        app = mind_app(tmp_path, toml=ON)
        app.engine._queue = lambda items: None
        try:
            mid = (await app.discord.handle(video_post(app)))['id']
            await photo_tasks(app)
            row = ds.attachments(app.store.conn, [mid])[mid]
            assert row['kind'] == 'upload' and 16 <= row['width'] <= 8192 and row['bytes'] <= 4 * 1024 * 1024
            meta = json.loads(app.store.get_meta(files.META_KEY.format(mid)))
            assert set(meta) == {'mime', 'bytes', 'ms', 'w', 'h', 'frames', 'path'}
        finally:
            await app.stop()
    run(go())
