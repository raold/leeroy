import asyncio
import os
from pathlib import Path

import pytest

from conftest import run, seed_test_cast
from console.rig import http
from console.test_console_discord import build, served, FakeLink
from discord.test_discord_desk import desk_app, Talk
from leeroy_mind.discord.desk import DiscordDesk
from leeroy_mind.endpoint import discord_args, BadRequest
from leeroy_mind.photos.images import MAX_IMAGE_BYTES
from photos.test_images import jpeg

HEADERS = {'Content-Type': 'image/jpeg', 'Sec-Fetch-Site': 'same-origin'}


@pytest.fixture
def data(store):
    root = Path(store.conn.execute('PRAGMA database_list').fetchone()['file']).parent
    (root / 'mind.toml').write_text('[photos]\nenabled=true')
    return root


def upload(app, data, **kw):
    return http(app, 'POST', '/api/discord/upload', body=data, origin=True,
                headers=kw.pop('headers', HEADERS), **kw)


def test_photo_only_post_arguments():
    args = {'action': 'post', 'channel': 'pics', 'text': '', 'image': 'a'*32}
    assert discord_args(args) == args
    for token in ('../a', 'a'*31, 1, None):
        with pytest.raises(BadRequest):
            discord_args(args | {'image': token})
    with pytest.raises(BadRequest):
        discord_args({k: v for k, v in args.items() if k != 'image'})


def test_upload_post_feed_serve_delete_keeps_files(data, store, clock):
    seed_test_cast(store)
    mind = desk_app(store, clock, data_dir=data, discord_talk=Talk())
    async def go():
        async with served(build(data, link=FakeLink())) as app:
            response = await upload(app, jpeg())
            assert response.status == 201, response.body
            token = response.json()['image']
            reply = await mind.discord.handle(discord_args(
                {'action':'post', 'channel':'pics', 'text':'', 'image':token}))
            assert reply['ok'], reply
            mid = reply['id']
            feed = (await http(app, 'GET', '/api/discord?channel=pics')).json()
            assert feed['photos'] is True
            assert feed['messages'][0]['image'] == {'w':32, 'h':24, 'kind':'upload'}
            for size in ('sm', 'lg'):
                image = await http(app, 'GET', f'/api/discord/image?id={mid}&size={size}')
                assert image.status == 200 and image.body == jpeg()
                assert image.header('Cross-Origin-Resource-Policy') == 'same-origin'
            before = {p: p.read_bytes() for p in (data/'discord').rglob('*') if p.is_file()}
            assert (await mind.discord.handle({'action':'delete', 'message':mid}))['ok']
            assert (await http(app, 'GET', f'/api/discord/image?id={mid}&size=lg')).status == 404
            assert before == {p: p.read_bytes() for p in (data/'discord').rglob('*') if p.is_file()}
    run(go())


@pytest.mark.parametrize('body,status', [(b'PNG',400), ('oversized',413), (None,411)])
def test_upload_refusals(data, body, status):
    async def go():
        async with served(build(data, link=FakeLink())) as app:
            if body == 'oversized':
                # Send only the declared length: rejection happens before body reads.
                response = await upload(app, None, headers=HEADERS | {'Content-Length': str(MAX_IMAGE_BYTES+1)})
            else:
                response = await upload(app, body)
            assert response.status == status
    run(go())


@pytest.mark.parametrize('mode', ['off', 'control', 'site', 'key', 'type', 'chunks'])
def test_upload_gates(data, mode):
    async def go():
        if mode == 'off':
            (data/'mind.toml').write_text('')
        app = build(data, link=FakeLink())
        if mode == 'control':
            app.controls.enabled = False
        headers = dict(HEADERS)
        expected = 403
        if mode == 'site':
            headers.pop('Sec-Fetch-Site')
        if mode == 'type':
            headers['Content-Type'] = 'application/json'
            expected = 415
        if mode == 'chunks':
            headers['Transfer-Encoding'] = 'chunked'
            expected = 411
        async with served(app):
            assert (await upload(app, jpeg(), headers=headers, key=mode!='key')).status == expected
    run(go())


def test_fifth_upload_per_minute_is_refused(data):
    async def go():
        async with served(build(data, link=FakeLink(), monotonic=lambda:100)) as app:
            for _ in range(4):
                assert (await upload(app, jpeg())).status == 201
            assert (await upload(app, jpeg())).status == 429
            assert (await http(app, 'POST', '/api/discord', body=b'x'*16385, origin=True)).status == 413
    run(go())


def test_stalled_upload_times_out_and_frees_connection_slot(monkeypatch):
    from leeroy_mind.console import httpd
    monkeypatch.setattr(httpd, 'MAX_CONNECTIONS', 1)
    async def go():
        async def handler(req):
            return httpd.text(200, 'ok')
        server = await httpd.serve(handler, '127.0.0.1', 0, routes={
            ('POST', '/upload'): httpd.Route(MAX_IMAGE_BYTES, .05, True)})
        port = server.sockets[0].getsockname()[1]
        try:
            reader, writer = await asyncio.open_connection('127.0.0.1', port)
            writer.write(b'POST /upload HTTP/1.1\r\nContent-Length: 100\r\n\r\npartial')
            await writer.drain()
            assert await asyncio.wait_for(reader.read(), 1) == b''
            writer.close()
            await writer.wait_closed()
            await asyncio.sleep(.01)
            reader, writer = await asyncio.open_connection('127.0.0.1', port)
            writer.write(b'GET / HTTP/1.1\r\n\r\n')
            await writer.drain()
            assert (await reader.read()).startswith(b'HTTP/1.1 200')
            writer.close()
            await writer.wait_closed()
        finally:
            server.close()
            await server.wait_closed()
    run(go())


def test_daily_upload_count_survives_restart_and_refunds_refusals(data, store):
    from leeroy_mind.clock import local_at
    from leeroy_mind.photos import images, uploads
    from console.test_console_discord import NOW
    day = local_at(NOW).date().isoformat()
    tokens = [images.stage(data, jpeg()) for _ in range(3)]
    images.adopt(data, tokens[0], 1)
    for path in (data / images.INCOMING).iterdir():
        os.utime(path, (NOW, NOW))
    old = images.stage(data, jpeg())
    os.utime(data / images.INCOMING / f'{old}.jpg', (NOW-86400, NOW-86400))
    (data / images.INCOMING / 'unrelated.jpg').write_bytes(jpeg())
    # This counter measures posts, not staging. It cannot consume the upload budget.
    store.set_meta(uploads.DAY_KEY.format(day), '200')
    async def go():
        async with served(build(data, link=FakeLink())) as app:
            assert (await upload(app, b'PNG')).status == 400
            assert app._photo_day_total == 3
            assert (await upload(app, jpeg())).status == 201
            assert app._photo_day_total == 4
            assert store.get_meta(uploads.DAY_KEY.format(day)) == '200'
        # Reconstruct the counter from the same files after a second restart.
        for path in (data / images.INCOMING).iterdir():
            if path.stem not in (*tokens, old) and images.TOKEN_RE.fullmatch(path.stem):
                os.utime(path, (NOW, NOW))
        async with served(build(data, link=FakeLink())) as app:
            assert (await upload(app, b'PNG')).status == 400
            assert app._photo_day_total == 4
    run(go())


def test_concurrent_uploads_cannot_overbook_daily_budget(data, monkeypatch):
    from leeroy_mind.photos import uploads
    monkeypatch.setattr(uploads, 'DAY_MAX', 3)
    async def go():
        async with served(build(data, link=FakeLink())) as app:
            replies = await asyncio.gather(*(upload(app, jpeg()) for _ in range(4)))
            assert sorted(r.status for r in replies) == [201, 201, 201, 429]
            assert app._photo_day_total == 3
    run(go())
