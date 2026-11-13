"""The console's GPU control boundary and direct fallback use fake loopback services."""
import asyncio
import json

import pytest

from conftest import run
from console.rig import http, make_app, running
from console.test_console_library import FakeLink
from leeroy_mind.console import gpuview


def test_route_gates_and_no_control_never_forward(tmp_path):
    async def go():
        link = FakeLink({'ok': True, 'paused': True, 'since': 1000, 'loaded': [],
                         'holders': [], 'refusal': None})
        app = make_app(tmp_path, mind=running(tmp_path), link=link)
        await app.start()
        try:
            for kwargs in ({'key': False, 'origin': True}, {'origin': False},
                           {'origin': 'https://elsewhere.invalid'},
                           {'origin': True, 'host': 'wrong.invalid'},
                           {'origin': True, 'headers': {'Sec-Fetch-Site': 'cross-site'}}):
                assert (await http(app, 'POST', '/api/gpu', body={'action': 'pause'}, **kwargs)).status == 403
            assert not link.calls
            app.controls.enabled = False
            for action in ('pause', 'resume'):
                response = await http(app, 'POST', '/api/gpu', origin=True, body={'action': action})
                assert response.status == 403 and response.json()['error'] == 'control_disabled'
            assert not link.calls
            app.controls.enabled = True
            response = await http(app, 'POST', '/api/gpu', origin=True, body={'action': 'pause'})
            assert response.status == 200 and response.json()['paused']
            assert link.calls[-1][1:3] == ('gpu', {'action': 'pause'})
            response = await http(app, 'GET', '/api/gpu')
            assert response.status == 200 and link.calls[-1][1:3] == ('gpu', {'action': 'status'})
            assert app.strip()['gpu']['paused']
        finally:
            await app.close()
    run(go())


@pytest.mark.parametrize('url', ['http://example.invalid:11434', 'http://127.0.0.1.example.invalid',
                                'ftp://127.0.0.1', 'http://localhost:nope', 'http://localhost:65536',
                                'http://user:password@localhost', 'http://[::ffff:192.0.2.1]'])
def test_nonlocal_direct_unload_refused_without_transport(url):
    def forbidden(*args, **kwargs):
        pytest.fail('nonlocal URL reached admin')
    result = run(gpuview.stopped(url, 'pause', admin_factory=forbidden))
    assert result['error'] == 'gpu_nonlocal'


def test_stopped_mind_unloads_every_fake_ollama_model_and_does_not_write_db(tmp_path):
    async def go():
        requests = []
        models = [{'name': 'synthetic-live', 'size_vram': 2**30},
                  {'name': 'synthetic-embed', 'size_vram': 2**29}]
        async def handler(reader, writer):
            try:
                header = (await reader.readuntil(b'\r\n\r\n')).decode()
                method, path, _ = header.split('\r\n')[0].split()
                length = next((int(line.split(':', 1)[1]) for line in header.split('\r\n')
                               if line.lower().startswith('content-length:')), 0)
                body = json.loads(await reader.readexactly(length)) if length else None
                requests.append((method, path, body))
                if path == '/api/ps':
                    payload = {'models': list(models)}
                else:
                    assert path == '/api/generate' and body['keep_alive'] == 0
                    models[:] = [m for m in models if m['name'] != body['model']]
                    payload = {'done': True}
                data = json.dumps(payload).encode()
                writer.write(f'HTTP/1.1 200 OK\r\nContent-Length: {len(data)}\r\nConnection: close\r\n\r\n'.encode() + data)
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
        server = await asyncio.start_server(handler, '127.0.0.1', 0)
        url = f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}'
        app = make_app(tmp_path, ollama_url=url)
        await app.start()
        try:
            response = await http(app, 'POST', '/api/gpu', origin=True, body={'action': 'pause'})
            value = response.json()
            assert response.status == 200 and value['mind_down']
            assert value['message'] == 'The mind is stopped; unloaded 2 models.'
            assert value['freed_bytes'] == 3 * 2**29 and models == []
            assert not (tmp_path / 'mind.db').exists()
            assert requests == [('GET', '/api/ps', None),
                                ('POST', '/api/generate', {'model': 'synthetic-live', 'keep_alive': 0}),
                                ('POST', '/api/generate', {'model': 'synthetic-embed', 'keep_alive': 0})]
            assert (await http(app, 'POST', '/api/gpu', origin=True,
                               body={'action': 'resume'})).status == 409
        finally:
            await app.close()
            server.close()
            await server.wait_closed()
    run(go())


def test_direct_unload_reserves_both_console_and_external_start_locks(tmp_path, monkeypatch):
    from leeroy_mind.app import acquire_instance_lock, AlreadyRunning
    from leeroy_mind.console.control import Refused
    async def go():
        entered, release = asyncio.Event(), asyncio.Event()
        async def stopped(url, action):
            entered.set()
            await release.wait()
            return {'ok': True, 'loaded': [], 'unloaded': [], 'freed_bytes': 0}
        monkeypatch.setattr(gpuview, 'stopped', stopped)
        app = make_app(tmp_path)
        await app.start()
        task = asyncio.create_task(http(app, 'POST', '/api/gpu', origin=True,
                                        body={'action': 'pause'}))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            assert app.controls.busy == ('mind', 'gpu')
            with pytest.raises(Refused):
                await app.controls.act('mind', 'start')
            with pytest.raises(AlreadyRunning):
                acquire_instance_lock(tmp_path)
            release.set()
            assert (await task).status == 200
            with acquire_instance_lock(tmp_path):
                pass
            assert app.controls.busy is None
        finally:
            release.set()
            await task
            await app.close()
    run(go())
