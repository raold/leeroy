"""The console's side of his videos: POST /api/discord/video (staged and read once, gated as photo
uploads are), the Discord view's video facts and GET /api/discord/video?id= (videos/README.md).
A real console on a free port; the mind is the real desk, ffmpeg is faked."""
import asyncio
from pathlib import Path

import pytest

from conftest import run, seed_test_cast
from console.rig import http
from console.test_console_discord import FakeLink, build, served
from discord.test_discord_desk import Talk, desk_app
from leeroy_mind.console import httpd
from leeroy_mind.endpoint import discord_args
from leeroy_mind.videos import files
from videos.test_video_files import MP4, WEBM, FakeTools

HEADERS = {'Content-Type': 'video/mp4', 'Sec-Fetch-Site': 'same-origin'}
ON = '[photos]\nenabled=true\n[videos]\nenabled=true'


@pytest.fixture
def data(store, monkeypatch):
    root = Path(store.conn.execute('PRAGMA database_list').fetchone()['file']).parent
    (root / 'mind.toml').write_text(ON)
    monkeypatch.setattr(files, 'tools', lambda: ('ffmpeg', 'ffprobe'))  # found at console start
    monkeypatch.setattr(files, '_run', FakeTools())
    return root


def upload(app, body, **kw):
    return http(app, 'POST', '/api/discord/video', body=body, origin=True,
                headers=kw.pop('headers', HEADERS), **kw)


def test_upload_post_view_play_and_delete(data, store, clock):
    seed_test_cast(store)
    mind = desk_app(store, clock, data_dir=data, discord_talk=Talk())
    async def go():
        async with served(build(data, link=FakeLink())) as app:
            response = await upload(app, MP4)
            assert response.status == 201, response.body
            answer = response.json()
            assert answer | {'video': ''} == {'video': '', 'ms': 4000, 'w': 320, 'h': 180, 'frames': 3}
            reply = await mind.discord.handle(discord_args(
                {'action': 'post', 'channel': 'dm:101', 'text': '', 'video': answer['video']}))
            assert reply['ok'], reply
            mid = reply['id']
            view = (await http(app, 'GET', '/api/discord?channel=dm:101')).json()
            assert view['photos'] is True and view['videos'] is True and 'videos_off' not in view
            message = view['messages'][-1]
            assert message['video'] == {'ms': 4000, 'w': 320, 'h': 180, 'frames': 3, 'bytes': len(MP4)}
            assert message['image']['kind'] == 'upload'  # the stills: the page's poster
            played = await http(app, 'GET', f'/api/discord/video?id={mid}')
            assert played.status == 200 and played.body == MP4
            assert played.header('Content-Type') == 'video/mp4'
            assert played.header('Cross-Origin-Resource-Policy') == 'same-origin'
            assert (await http(app, 'GET', f'/api/discord/video?id={mid}', key=False)).status == 403
            for query in ('', '?id=0', '?id=x', f'?id={mid}&id={mid}', f'?id={mid}&size=sm'):
                assert (await http(app, 'GET', '/api/discord/video' + query)).status == 400
            before = {p: p.read_bytes() for p in (data / 'discord').rglob('*') if p.is_file()}
            assert (await mind.discord.handle({'action': 'delete', 'message': mid}))['ok']
            assert (await http(app, 'GET', f'/api/discord/video?id={mid}')).status == 404
            view = (await http(app, 'GET', '/api/discord?channel=dm:101')).json()
            assert 'video' not in view['messages'][-1]
            assert before == {p: p.read_bytes() for p in (data / 'discord').rglob('*') if p.is_file()}
    run(go())


@pytest.mark.parametrize('mode,expected', [
    ('off', 403), ('photos_off', 403), ('control', 403), ('site', 403), ('key', 403),
    ('type', 415), ('avi', 415), ('chunks', 411), ('oversized', 413), ('png', 400),
    ('webm_as_mp4', 400), ('tools', 503)])
def test_upload_gates(data, monkeypatch, mode, expected):
    async def go():
        if mode == 'off':
            (data / 'mind.toml').write_text('[photos]\nenabled=true')
        if mode == 'photos_off':
            (data / 'mind.toml').write_text('[videos]\nenabled=true')
        if mode == 'tools':
            monkeypatch.setattr(files, 'tools', lambda: None)
        app = build(data, link=FakeLink())
        if mode == 'control':
            app.controls.enabled = False
        headers, body = dict(HEADERS), MP4
        if mode == 'site':
            headers.pop('Sec-Fetch-Site')
        if mode == 'type':
            headers['Content-Type'] = 'image/jpeg'
        if mode == 'avi':
            headers['Content-Type'] = 'video/x-msvideo'
        if mode == 'chunks':
            headers['Transfer-Encoding'] = 'chunked'
        if mode == 'png':
            body = b'\x89PNG\r\n\x1a\n' + bytes(32)
        if mode == 'webm_as_mp4':
            body = WEBM
        async with served(app):
            if mode == 'oversized':  # only the declared length: refused before the body is read
                response = await upload(app, None, headers=headers | {'Content-Length': str(files.MAX_VIDEO_BYTES + 1)})
            else:
                response = await upload(app, body, headers=headers, key=mode != 'key')
            assert response.status == expected, response.body
            if expected == 400:
                assert response.json()['error'] == files.WORDS['not_video']
        assert not list((data / files.INCOMING).glob('*.json')) if (data / files.INCOMING).exists() else True
    run(go())


@pytest.mark.parametrize('refusal', ['key', 'site', 'type', 'origin'])
def test_a_refused_upload_is_answered_from_its_head_before_any_body(data, refusal):
    """Only the head of a 50 MiB upload, whose body never comes: the gate answers at once,
    rather than the console buffering (or waiting out) a body it would refuse."""
    async def go():
        async with served(build(data, link=FakeLink())) as app:
            headers = HEADERS | {'Content-Length': str(files.MAX_VIDEO_BYTES)}
            if refusal == 'site':
                headers['Sec-Fetch-Site'] = 'cross-site'
            if refusal == 'type':
                headers['Content-Type'] = 'text/plain'
            response = await asyncio.wait_for(http(app, 'POST', '/api/discord/video', headers=headers,
                key=refusal != 'key', origin='http://elsewhere.invalid' if refusal == 'origin' else True), 5)
            assert response.status == (415 if refusal == 'type' else 403)
    run(go())


def test_a_third_video_in_a_minute_waits(data):
    async def go():
        async with served(build(data, link=FakeLink(), monotonic=lambda: 100)) as app:
            for _ in range(files.MINUTE_MAX):
                assert (await upload(app, MP4)).status == 201
            refused = await upload(app, MP4)
            assert refused.status == 429 and refused.json()['error'] == files.WORDS['minute']
    run(go())


def test_the_day_count_is_rebuilt_from_staged_facts(data):
    import os
    from console.test_console_discord import NOW
    async def go():
        async with served(build(data, link=FakeLink())) as app:
            assert (await upload(app, MP4)).status == 201
        for path in (data / files.INCOMING).iterdir():  # staged on the console's (fixed) day
            os.utime(path, (NOW, NOW))
        async with served(build(data, link=FakeLink())) as app:
            assert (await upload(app, MP4)).status == 201
            assert app._video_day_total == 2
    run(go())


def test_the_view_says_why_videos_cannot_be_sent_without_ffmpeg(data, monkeypatch):
    monkeypatch.setattr(files, 'tools', lambda: None)
    async def go():
        async with served(build(data, link=FakeLink())) as app:
            view = (await http(app, 'GET', '/api/discord?channel=dm:101')).json()
            assert view['photos'] is True and view['videos'] is False
            assert view['videos_off'] == files.WORDS['tools']
    run(go())


def test_a_large_answer_arrives_whole_in_pieces():
    async def go():
        body = bytes(range(256)) * (3 * httpd.WRITE_CHUNK // 256 + 7)
        async def handler(req):
            return httpd.Response(200, body, 'application/octet-stream')
        server = await httpd.serve(handler, '127.0.0.1', 0)
        port = server.sockets[0].getsockname()[1]
        try:
            reader, writer = await asyncio.open_connection('127.0.0.1', port)
            writer.write(b'GET / HTTP/1.1\r\nHost: x\r\n\r\n')
            await writer.drain()
            raw = await asyncio.wait_for(reader.read(), 10)
            writer.close()
            head, _, got = raw.partition(b'\r\n\r\n')
            assert f'Content-Length: {len(body)}'.encode() in head and got == body
        finally:
            server.close()
            await server.wait_closed()
    run(go())
