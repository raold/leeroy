"""GPU pause uses synthetic apps and transports; no installed service or model is contacted."""
import asyncio
import json
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from conftest import run
from leeroy_mind.gpucontrol import KEY, GpuControl, arguments, since_value
from leeroy_mind.llm.client import GpuPaused, OllamaClient
from leeroy_mind.llm.pause import PauseGate
from leeroy_mind.llm.scheduler import Priority
from leeroy_mind.nightly.gpu import HttpOllamaAdmin
from selfies.fakes import known_offline, mind_app


class Admin:
    def __init__(self):
        self.models = [{'name': 'invented-live', 'size_vram': 2**30},
                       {'name': 'unmanaged-embed', 'size_vram': 2**29}]
        self.calls = []
        self.fail = None

    async def loaded(self):
        return list(self.models)

    async def loaded_models(self):
        return [m['name'] for m in self.models]

    async def unload(self, name):
        self.calls.append(('unload', name))
        if name == self.fail:
            raise OSError('invented failure')
        self.models = [m for m in self.models if m['name'] != name]

    async def preload(self, model, keep_alive, *, options=None):
        self.calls.append(('preload', model))
        if model not in await self.loaded_models():
            self.models.append({'name': model, 'size_vram': 100})


def app_at(tmp_path, *, server=False, hour=12):
    app = mind_app(tmp_path, toml='[server]\nenabled=true\n' if server else '')
    app.clock._t = datetime(2026, 11, 13, hour, tzinfo=timezone.utc).timestamp()
    app.arbiter.admin = Admin()
    return known_offline(app)


def test_pause_all_models_persists_and_restart_stays_paused(tmp_path):
    async def go():
        app = app_at(tmp_path)
        try:
            reply = await app.gpu.action({'action': 'pause'})
            assert reply['ok'] and reply['paused'] and reply['freed_bytes'] == 3*2**29
            assert reply['unloaded'] == ['invented-live', 'unmanaged-embed']
            assert not app.arbiter.reflection_allowed()
            assert json.loads(app.store.get_meta(KEY)) == {'at': app.gpu.since}
            # A fresh controller reconstructs admission from the stored marker.
            restarted = GpuControl(app)
            assert restarted.paused and restarted.since == app.gpu.since
            with pytest.raises(GpuPaused):
                await app.scheduler.submit(Priority.BACKGROUND, lambda llm: llm.chat('', []))
            assert not app.llm.calls
        finally:
            await app.stop()
    run(go())


@pytest.mark.parametrize('holder', ['server', 'discord', 'offline_raid', 'training_lab', 'voice', 'preview', 'other'])
def test_every_nonlogin_lease_refused(tmp_path, holder):
    async def go():
        app = app_at(tmp_path)
        try:
            await app.gpu.action({'action': 'pause'})
            with pytest.raises(GpuPaused):
                await app.arbiter.acquire_live(holder)
            assert holder not in app.arbiter.holders
            assert not app.arbiter.reflection_allowed()
        finally:
            await app.stop()
    run(go())


@pytest.mark.parametrize('holder,words', [('human', 'Log out of WoW'), ('offline_raid', 'Raid tab'),
                                       ('training_lab', 'Training tab')])
def test_refusal_does_not_pause_or_unload(tmp_path, holder, words):
    async def go():
        app = app_at(tmp_path)
        try:
            app.arbiter.hold(holder)
            reply = await app.gpu.action({'action': 'pause'})
            assert not reply['ok'] and words in reply['message']
            assert not app.gpu.paused and app.store.get_meta(KEY) is None
            assert not app.arbiter.admin.calls
        finally:
            await app.stop()
    run(go())


@pytest.mark.parametrize('reason', ['console', 'login', 'discord'])
@pytest.mark.parametrize('hour,server,expected', [(12, True, True), (4, True, False), (12, False, False)])
def test_only_explicit_resume_restores_server_outside_window(tmp_path, reason, hour, server, expected):
    async def go():
        app = app_at(tmp_path, server=server, hour=hour)
        try:
            if server:
                await app.server.lease.tick(app.clock.now())
                await app.server.lease.handback()
            await app.gpu.action({'action': 'pause'})
            assert 'server' not in app.arbiter.holders
            if server:
                await app.server.lease.tick(app.clock.now())
                assert not app.server.lease.held
            app.arbiter.admin.calls.clear()
            if reason == 'login':
                await app.arbiter.acquire_live('human')
            elif reason == 'console':
                await app.gpu.action({'action': 'resume'})
            else:
                app.gpu.resume_now(reason)
            await app.gpu.restore()
            assert not app.gpu.paused and app.store.get_meta(KEY) is None
            assert ('server' in app.arbiter.holders) == expected
            assert len([c for c in app.arbiter.admin.calls if c[0] == 'preload']) == int(expected or reason == 'login')
        finally:
            await app.stop()
    run(go())


def test_partial_unload_is_retryable_and_honest(tmp_path):
    async def go():
        app = app_at(tmp_path)
        try:
            app.arbiter.admin.fail = 'unmanaged-embed'
            reply = await app.gpu.action({'action': 'pause'})
            assert not reply['ok'] and reply['paused'] and reply['unload_failed']
            assert reply['freed_bytes'] == 2**30
            app.arbiter.admin.fail = None
            reply = await app.gpu.action({'action': 'pause'})
            assert reply['ok'] and not reply['unload_failed'] and not reply['loaded']
        finally:
            await app.stop()
    run(go())


def test_cancel_and_drain_inflight_before_unload(tmp_path):
    async def go():
        app = app_at(tmp_path)
        entered, ended = asyncio.Event(), asyncio.Event()
        async def job(llm):
            entered.set()
            try:
                await asyncio.Future()
            finally:
                await asyncio.sleep(0)
                ended.set()
        try:
            task = asyncio.create_task(app.scheduler.submit(Priority.BACKGROUND, job))
            await entered.wait()
            old = app.arbiter.admin.unload
            async def unload(name):
                assert ended.is_set()
                await old(name)
            app.arbiter.admin.unload = unload
            assert (await app.gpu.action({'action': 'pause'}))['ok']
            with pytest.raises(GpuPaused):
                await task
        finally:
            await app.stop()
    run(go())


@pytest.mark.parametrize('path', ['/api/chat', '/api/generate', '/api/embed'])
def test_client_refuses_before_http(monkeypatch, path):
    async def go():
        client = OllamaClient('http://127.0.0.1:1')
        client.gpu_gate = PauseGate(True)
        sent = []
        monkeypatch.setattr(client, '_request', lambda *args: sent.append(args))
        with pytest.raises(GpuPaused):
            await client._call(path, {'model': 'fake'}, 1)
        assert sent == []
    run(go())


def test_admin_preload_refuses_before_http(monkeypatch):
    async def go():
        admin = HttpOllamaAdmin('http://127.0.0.1:1')
        admin.gpu_gate = PauseGate(True)
        sent = []
        monkeypatch.setattr(admin, '_request', lambda *args: sent.append(args))
        with pytest.raises(GpuPaused):
            await admin.preload('fake', -1)
        assert sent == []
    run(go())


@pytest.mark.parametrize('args', [None, {}, {'action': 'timer'}, {'action': []}, {'action': 'pause', 'extra': 1}])
def test_gpu_arguments_are_exact(args):
    with pytest.raises(ValueError):
        arguments(args)


@pytest.mark.parametrize('raw', ['', 'not-json', '{}', '{"at":true}', '{"at":NaN}'])
def test_corrupt_pause_marker_never_silently_enables_gpu(raw):
    assert since_value(raw) == 0
    assert since_value(None) is None


def test_night_pause_resumes_same_attempt(tmp_path):
    from leeroy_mind.nightly.store import run_get
    async def go():
        app = app_at(tmp_path)
        scheduler = app.night_scheduler
        scheduler.settings = replace(scheduler.settings, grace_minutes=0,
                                     min_window_hours=0, min_gap_hours=0)
        entered = asyncio.Event()
        starts = []
        async def night(night, window):
            starts.append(night)
            entered.set()
            if len(starts) == 1:
                await asyncio.Future()
        scheduler.run_night = night
        try:
            await scheduler.tick()
            await entered.wait()
            assert (await app.gpu.action({'action': 'pause'}))['ok']
            assert scheduler.running_night() is None and scheduler._task.cancelled()
            assert run_get(app.store, starts[0]).attempts == 1
            await scheduler.tick()
            assert len(starts) == 1
            await app.gpu.action({'action': 'resume'})
            await scheduler.tick()
            await scheduler.wait_idle()
            assert starts == [starts[0], starts[0]]
            row = run_get(app.store, starts[0])
            assert row.status == 'done' and row.attempts == 1
        finally:
            await scheduler.cancel_run()
            await app.stop()
    run(go())


def test_paused_extraction_does_not_dequeue_model_work(tmp_path):
    from leeroy_mind.memory.threads import ClosedThread
    async def go():
        app = app_at(tmp_path)
        seen = []
        async def extract(closed):
            seen.append(closed)
        app.extractor.extract = extract
        worker = None
        try:
            await app.gpu.action({'action': 'pause'})
            app._extract_queue.put_nowait(ClosedThread(7, 'guild', (), 'flush'))
            worker = asyncio.create_task(app._extract_worker())
            for _ in range(5):
                await asyncio.sleep(0)
            assert seen == [] and app._extract_queue._unfinished_tasks == 1
            await app.gpu.action({'action': 'resume'})
            await asyncio.wait_for(app._extract_queue.join(), 2)
            assert len(seen) == 1
        finally:
            if worker:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
            await app.stop()
    run(go())


def test_health_pause_is_only_a_note(store):
    from leeroy_mind.health import _gpu_pause, _when, NOTE, OK
    from leeroy_mind.nightly.store import run_begin, run_finish
    assert _gpu_pause(store.conn, 100000).status == OK
    store.set_meta(KEY, json.dumps({'at': 1000}))
    check = _gpu_pause(store.conn, 100000)
    assert check.status == NOTE and 'no night has finished yet' in check.text
    run_begin(store, '1970-01-01', 0, 500, now=500)
    run_finish(store, '1970-01-01', now=800, status='done')
    check = _gpu_pause(store.conn, 100000)
    # The last finished night, not the pause's own time.
    assert check.status == NOTE and check.text.endswith(f'the night has not run since {_when(800)}')
    assert _when(1000) in check.text and _gpu_pause(store.conn, 2000).text == f'GPU paused since {_when(1000)}'


def test_pause_during_extraction_keeps_pending_item_until_resume(store, clock, caplog):
    from test_extract import Rig, SPEC
    async def go():
        rig = Rig(store, clock, ['{"memories":[],"overheard":null}'])
        gate = PauseGate()
        rig.ex.gpu_gate = gate
        rig.ex._scheduler.gpu_gate = gate
        rig.llm._gate = asyncio.Event()
        queue = asyncio.Queue()
        thread = rig.thread(SPEC)
        queue.put_nowait(thread)
        worker = asyncio.create_task(rig.ex.run(queue))
        try:
            while not rig.llm.inflight:
                await asyncio.sleep(0)
            gate.set(True)
            await gate.drain()
            for _ in range(5):
                await asyncio.sleep(0)
            assert store.thread_status(thread.thread_id) != 'failed'
            assert queue._unfinished_tasks == 1 and len(rig.llm.calls) == 1
            assert 'failed' not in caplog.text and 'crashed' not in caplog.text
            rig.llm._gate = None
            gate.set(False)
            await asyncio.wait_for(queue.join(), 2)
            assert store.thread_status(thread.thread_id) == 'extracted'
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
            await rig.ex._scheduler.close()
    run(go())


def test_pause_during_memory_embed_waits_without_unvectorized_write(store, clock, caplog):
    from test_extract import Rig
    from test_memory_writer import cand
    async def go():
        rig = Rig(store, clock, [])
        gate = PauseGate()
        writer = rig.ex._writer
        writer.gpu_gate = rig.ex._scheduler.gpu_gate = gate
        rig.llm._gate = asyncio.Event()
        task = asyncio.create_task(writer.write(cand('A synthetic promise about spare flasks.')))
        try:
            while not rig.llm.inflight:
                await asyncio.sleep(0)
            gate.set(True)
            await gate.drain()
            for _ in range(5):
                await asyncio.sleep(0)
            assert not task.done() and not store.conn.execute('SELECT 1 FROM memory').fetchone()
            assert 'embed failed' not in caplog.text
            rig.llm._gate = None
            gate.set(False)
            result = await asyncio.wait_for(task, 2)
            assert store.get_memories([result.id])[0].embedding is not None
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await rig.ex._scheduler.close()
    run(go())


def test_preload_waiting_in_thread_pool_rechecks_pause_at_transport(monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    async def go():
        loop = asyncio.get_running_loop()
        executor = ThreadPoolExecutor(max_workers=1)
        loop.set_default_executor(executor)
        started, release = threading.Event(), threading.Event()
        def blocker():
            started.set()
            release.wait(5)
        occupying = loop.run_in_executor(None, blocker)
        while not started.is_set():
            await asyncio.sleep(0)
        admin = HttpOllamaAdmin('http://127.0.0.1:1')
        gate = admin.gpu_gate = PauseGate()
        sent = []
        monkeypatch.setattr(admin._opener, 'open', lambda *a, **kw: sent.append(1))
        task = asyncio.create_task(admin.preload('fake', -1))
        try:
            while executor._work_queue.qsize() == 0:
                await asyncio.sleep(0)
            gate.set(True)
            drain = asyncio.create_task(gate.drain())
            await asyncio.sleep(0)
            release.set()
            await occupying
            await drain
            with pytest.raises(GpuPaused):
                await task
            assert sent == []
        finally:
            release.set()
            await occupying
    run(go())


def test_paused_status_is_fast_with_slow_admin_and_refresh_is_deduplicated(tmp_path):
    from leeroy_mind.endpoint import ControlEndpoint, ControlCfg, encode
    async def go():
        app = app_at(tmp_path)
        calls = []
        slow = asyncio.Event()
        try:
            await app.gpu.action({'action': 'pause'})
            async def loaded():
                calls.append(1)
                await slow.wait()
                return []
            app.arbiter.admin.loaded = loaded
            endpoint = ControlEndpoint(app, ControlCfg())
            body = encode({'v': 1, 'key': endpoint.key, 'op': 'status'})
            for _ in range(5):
                reply, _ = await asyncio.wait_for(endpoint._answer(body), .1)
                assert reply['gpu']['paused']
            for _ in range(3):
                await asyncio.sleep(0)
            assert len(calls) == 1
        finally:
            slow.set()
            await app.stop()
    run(go())


@pytest.mark.parametrize('hour,server', [(4, False), (4, True), (12, True)])
def test_voice_ownership_survives_pause_and_resume(tmp_path, hour, server):
    async def go():
        app = app_at(tmp_path, hour=hour, server=server)
        try:
            app.arbiter.hold('voice_gpu')
            assert (await app.gpu.action({'action': 'pause'}))['ok']
            assert 'voice_gpu' in app.arbiter.holders
            app.arbiter.admin.calls.clear()
            assert (await app.gpu.action({'action': 'resume'}))['ok']
            assert not app.arbiter.reflection_allowed()
            assert sum(c[0] == 'preload' for c in app.arbiter.admin.calls) == int(hour == 12 and server)
        finally:
            await app.stop()
    run(go())


def test_discord_resume_while_server_tick_waits_keeps_new_lease(tmp_path):
    async def go():
        app = app_at(tmp_path, server=True, hour=4)
        entered, release = asyncio.Event(), asyncio.Event()
        async def tick(now):
            entered.set()
            await release.wait()
        app.server.engine.tick = tick
        try:
            pause = asyncio.create_task(app.gpu.action({'action': 'pause'}))
            await entered.wait()
            app.gpu.resume_now('discord')
            app.discord_talk._take_lease()
            assert 'discord' in app.arbiter.holders
            release.set()
            assert (await pause)['error'] == 'gpu_resumed'
            assert 'discord' in app.arbiter.holders and not app.arbiter.reflection_allowed()
        finally:
            release.set()
            await app.stop()
    run(go())


def test_real_app_bridge_server_dm_and_booth_rest_then_answer_his_post(tmp_path):
    from leeroy_mind.app import GUILD_ID
    from leeroy_mind.bridge import BridgeClient
    from leeroy_mind.testing.fake_bridge import FakeBridge
    from leeroy_mind.protocol import Member
    from selfies.fakes import OWNER, until
    from selfies.test_photo_booth import MemoryWorker
    async def go():
        bridge = FakeBridge([{'type': 'hello', 'server': 'mod-leeroy', 'version': '0.1.0',
                              'guild_id': GUILD_ID, 'human_guid': 0}], human_guid=0)
        port = await bridge.start()
        app = mind_app(tmp_path, toml=f'[bridge]\nport={port}\n[server]\nenabled=true\n'
            '[selfies]\nenabled=true\nbooth=true\n[talk]\nchattiness=0\n[guards]\nenabled=true\n')
        app.clock._t = datetime(2026, 11, 13, 12, tzinfo=timezone.utc).timestamp()
        async def sleep(seconds):
            await asyncio.sleep(min(seconds, .01))
        app.clock.sleep = sleep
        app.arbiter.admin = Admin()
        app.llm.set_models({app.cfg.models.live, app.cfg.models.embedding})
        app.llm._script = lambda model, messages: 'The spare flasks are ready for the next raid.'
        worker = MemoryWorker()
        app.selfie_booth.worker = lambda: worker
        task = asyncio.create_task(app.run())
        try:
            await until(lambda: app.bridge.greeted, 5)
            assert (await app.gpu.action({'action': 'pause'}))['ok']
            assert not app.arbiter.admin.models
            # Long offline: only the pause keeps the booth from rendering now.
            app.night_scheduler.presence.changed = app.clock.now() - 1000
            app.llm.calls.clear()
            await app.server.lease.tick(app.clock.now())
            await app.server.engine.tick(app.clock.now())
            await app.server.dms.tick(app.clock.now())
            await app.selfie_booth.tick()
            assert not app.llm.calls and not worker.requests
            assert app.bridge.connected and app.gpu.paused
            app.arbiter.admin.calls.clear()
            # A real stored owner DM follows the desk -> lease -> pipeline -> transport path.
            reply = await app.discord.handle({'action': 'post', 'channel': 'dm:101',
                                             'text': 'Are the spare flasks ready for the next raid?'})
            assert reply['ok'] and not app.gpu.paused
            await until(lambda: app.store.conn.execute("SELECT 1 FROM discord_message WHERE channel='dm:101' "
                        "AND origin='mind' AND author=101").fetchone() is not None, 5)
            assert sum(c[0] == 'preload' for c in app.arbiter.admin.calls) == 1
            assert not [c for c in bridge.commands if c['type'] in ('say', 'emote')]
        finally:
            await app.stop()
            await asyncio.wait_for(task, 5)
            await bridge.stop()
    run(go())


def test_resume_during_cancel_cleanup_preserves_pause_reason():
    async def go():
        gate = PauseGate()
        started, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def job():
            started.set()
            try:
                await asyncio.Future()
            finally:
                cleaning.set()
                await release.wait()
        task = asyncio.create_task(gate.run(job))
        await started.wait()
        gate.set(True)
        drain = asyncio.create_task(gate.drain())
        await cleaning.wait()
        gate.set(False)
        release.set()
        await drain
        with pytest.raises(GpuPaused):
            await task
        assert not gate._active and not gate._pause_cancelled
    run(go())


def test_stalled_send_does_not_block_loop_and_unload_waits_for_worker(tmp_path, monkeypatch):
    import threading
    import time
    from test_llm import Transport
    from leeroy_mind.llm import client as client_module
    async def go():
        app = app_at(tmp_path)
        transport = Transport().install(monkeypatch)
        connection = client_module.http.client.HTTPConnection
        sending, release = threading.Event(), threading.Event()
        class Stalled(connection):
            def request(self, *args, **kwargs):
                sending.set()
                release.wait(2)  # bounded even if a regression blocks the event loop
                return super().request(*args, **kwargs)
        monkeypatch.setattr(client_module.http.client, 'HTTPConnection', Stalled)
        client = OllamaClient('http://127.0.0.1:1')
        client.gpu_gate = app.gpu.gate
        request = asyncio.create_task(client.chat('fake', [], options={}))
        pause = None
        try:
            while not sending.is_set():
                await asyncio.sleep(.001)
            old = app.arbiter.admin.unload
            async def unload(name):
                assert transport.active == 0
                await old(name)
            app.arbiter.admin.unload = unload
            start = time.monotonic()
            pause = asyncio.create_task(app.gpu.action({'action': 'pause'}))
            await asyncio.sleep(.03)
            assert time.monotonic() - start < .5
            assert app.gpu.paused and not pause.done()
            release.set()
            assert (await asyncio.wait_for(pause, 2))['ok']
            with pytest.raises(GpuPaused):
                await request
        finally:
            release.set()
            await asyncio.gather(request, *([pause] if pause else []), return_exceptions=True)
            await app.stop()
    run(go())


def test_delayed_hook_for_old_owner_post_cannot_resume_pause(tmp_path):
    from leeroy_mind.discord import store as discord
    async def go():
        app = app_at(tmp_path)
        try:
            message = discord.post(app.store, channel='dm:101', author=app.human_guid,
                                   origin='human', text='An older synthetic post.', now=app.clock.now()-60)
            await app.gpu.action({'action': 'pause'})
            app.discord_talk.on_his_post(message)
            await asyncio.sleep(0)
            assert app.gpu.paused and 'discord' not in app.arbiter.holders
            assert not app.llm.calls
        finally:
            await app.stop()
    run(go())


def _night_ready(app):
    scheduler = app.night_scheduler
    scheduler.settings = replace(scheduler.settings, grace_minutes=0,
                                 min_window_hours=0, min_gap_hours=0)
    return scheduler


def _events(caplog):
    return [getattr(r, 'leeroy_event', '') for r in caplog.records]


def test_pause_while_the_night_makes_room_interrupts_it_without_an_attempt(tmp_path, caplog):
    """The review's probe: Free GPU while the night waits on a slow unload in make_room."""
    import logging
    from leeroy_mind.nightly.store import UNFINISHED, run_get
    async def go():
        app = app_at(tmp_path)
        scheduler = _night_ready(app)
        admin = app.arbiter.admin
        entered, release = asyncio.Event(), asyncio.Event()
        original = admin.unload
        async def unload(name):
            if not entered.is_set():
                entered.set()
                await release.wait()
            await original(name)
        admin.unload = unload
        starts = []
        async def night(night, window):
            starts.append(night)
            if len(starts) == 1:
                await app.arbiter.make_room()
        scheduler.run_night = night
        try:
            await scheduler.tick()
            await entered.wait()
            pause = asyncio.create_task(app.gpu.action({'action': 'pause'}))
            for _ in range(5):
                await asyncio.sleep(0)
            release.set()
            assert (await asyncio.wait_for(pause, 5))['ok']
            row = run_get(app.store, starts[0])
            assert row.status in UNFINISHED and row.attempts == 1 and row.error is None
            await app.gpu.action({'action': 'resume'})
            await scheduler.tick()
            await scheduler.wait_idle()
            row = run_get(app.store, starts[0])
            assert starts == [starts[0]] * 2 and row.status == 'done' and row.attempts == 1
        finally:
            release.set()
            await scheduler.cancel_run()
            await app.stop()
    with caplog.at_level(logging.INFO, logger='leeroy_mind'):
        run(go())
    assert 'nightly_failed' not in _events(caplog)


def test_pause_reaching_a_night_step_without_its_stop_is_an_interruption(tmp_path, caplog):
    """Wherever GpuPaused still surfaces in the night, it is the pause's interruption."""
    import logging
    from leeroy_mind.nightly.runner import NightRunner, Stage
    from leeroy_mind.nightly.store import run_get
    async def go():
        app = app_at(tmp_path)
        scheduler = _night_ready(app)
        admin = app.arbiter.admin
        original = admin.unload
        async def unload(name):
            app.arbiter.paused = True  # the closed gate alone, with no stop request
            await original(name)
        admin.unload = unload
        runs = []
        async def stage(ctx):
            runs.append(ctx.night)
            if len(runs) < 3:
                await ctx.arbiter.make_room()
        scheduler.run_night = NightRunner([Stage('room', stage, None)], app.night_runner.make_ctx).run
        try:
            for expected in (1, 2):
                await scheduler.tick()
                await scheduler.wait_idle()
                row = run_get(app.store, runs[0])
                assert len(runs) == expected and row.status == 'interrupted'
                assert row.attempts == 1 and row.resumes == 0  # the resume was refunded
                app.arbiter.paused = False
            admin.unload = original
            await scheduler.tick()
            await scheduler.wait_idle()
            row = run_get(app.store, runs[0])
            assert row.status == 'done' and row.attempts == 1
        finally:
            await scheduler.cancel_run()
            await app.stop()
    with caplog.at_level(logging.INFO, logger='leeroy_mind'):
        run(go())
    assert 'nightly_failed' not in _events(caplog) and 'nightly_unit_failed' not in _events(caplog)


def test_a_unit_meeting_the_pause_ends_the_stage_instead_of_failing(tmp_path):
    from types import SimpleNamespace
    from leeroy_mind.nightly.llm import Reasoner
    async def go():
        app = app_at(tmp_path)
        try:
            ctx = app.night_runner.make_ctx('2026-11-13', (0, app.clock.now()))
            async def unit(name):
                if name == 'b':
                    raise GpuPaused('The GPU is paused.')
            with pytest.raises(GpuPaused):
                await ctx.map_units('stage', ['a', 'b'], unit)
            assert not ctx.failures['stage']
            async def chat(*args, **kwargs):
                raise GpuPaused('The GPU is paused.')
            reasoner = Reasoner(SimpleNamespace(chat=chat), 'invented-reflection')
            with pytest.raises(GpuPaused):
                await reasoner.think('system', 'user')
            with pytest.raises(GpuPaused):
                await reasoner.json('system', 'user', {'type': 'object'})
        finally:
            await app.stop()
    run(go())


@pytest.mark.parametrize('state', ['no_hello', 'bridge_down', 'presence_unknown', 'unreadable'])
def test_pause_refused_while_his_wow_presence_is_unknown(tmp_path, state):
    async def go():
        app = app_at(tmp_path)
        world = app.world
        try:
            if state == 'no_hello':
                app.hello = None
            elif state == 'bridge_down':
                app.bridge._connected = False
            elif state == 'presence_unknown':
                app.night_scheduler.presence.online = None
            else:
                class Unreadable:
                    def __getattr__(self, name):
                        raise RuntimeError('invented unreadable session')
                app.world = Unreadable()
            reply = await app.gpu.action({'action': 'pause'})
            assert not reply['ok'] and reply['error'] == 'gpu_presence_unknown'
            assert reply['message'] == "Can't tell whether you're in WoW yet; try again in a moment."
            assert reply['refusal'] == reply['message']
            assert not app.gpu.paused and app.store.get_meta(KEY) is None
            assert not app.arbiter.admin.calls
        finally:
            app.world = world
            await app.stop()
    run(go())


def test_slow_night_cleanup_pauses_now_and_unloads_once_it_has_stopped(tmp_path, monkeypatch):
    from leeroy_mind import gpucontrol
    from leeroy_mind.nightly.store import UNFINISHED, run_get
    monkeypatch.setattr(gpucontrol, 'PAUSE_WAIT_S', .05)
    async def go():
        app = app_at(tmp_path)
        scheduler = _night_ready(app)
        entered, cleanup = asyncio.Event(), asyncio.Event()
        starts = []
        async def night(night, window):
            starts.append(night)
            entered.set()
            try:
                await asyncio.Future()
            finally:
                await cleanup.wait()  # a worker tree that takes its time to end
        scheduler.run_night = night
        try:
            await scheduler.tick()
            await entered.wait()
            reply = await asyncio.wait_for(app.gpu.action({'action': 'pause'}), 2)
            assert reply['ok'] and reply['paused'] and reply['pending']
            assert reply['message'] == 'GPU use is paused and will finish once the night has stopped.'
            assert app.gpu.paused and not app.arbiter.reflection_allowed()
            with pytest.raises(GpuPaused):
                app.gpu.gate.check()
            assert not app.arbiter.admin.calls and app.arbiter.admin.models
            cleanup.set()
            await asyncio.wait_for(app.gpu._finishing, 2)
            assert not app.arbiter.admin.models and not app.gpu.unload_failed
            row = run_get(app.store, starts[0])
            assert row.status in UNFINISHED and row.attempts == 1
        finally:
            cleanup.set()
            await scheduler.cancel_run()
            await app.stop()
    run(go())


def test_login_resumes_even_when_the_stored_pause_cannot_be_cleared(tmp_path, monkeypatch, caplog):
    import contextlib
    import logging
    import sqlite3
    async def go():
        app = app_at(tmp_path)
        try:
            assert (await app.gpu.action({'action': 'pause'}))['ok']
            @contextlib.contextmanager
            def locked():
                raise sqlite3.OperationalError('database is locked')
                yield
            monkeypatch.setattr(app.store, 'transaction', locked)
            assert app.arbiter.hold('human')
            assert 'human' in app.arbiter.holders and not app.gpu.paused
            assert not app.arbiter.paused and not app.gpu.gate.paused
            monkeypatch.undo()
        finally:
            await app.stop()
    with caplog.at_level(logging.INFO, logger='leeroy_mind'):
        run(go())
    assert 'gpu_resume_store_failed' in _events(caplog)


def test_pause_reads_residency_with_the_admin_timeout(tmp_path):
    from leeroy_mind import gpucontrol
    async def go():
        seen = []
        class Admin(HttpOllamaAdmin):
            async def loaded(self, *, timeout=None):
                seen.append(timeout)
                return []
        app = app_at(tmp_path)
        try:
            app.arbiter.admin = Admin('http://127.0.0.1:1')
            assert (await app.gpu.action({'action': 'pause'}))['ok']
            assert seen == [None]  # the admin's own timeout, not the status strip's .5 s
            await gpucontrol.loaded(app.arbiter.admin)
            assert seen == [None, .5]
        finally:
            await app.stop()
    run(go())


def test_ordinary_preload_cancel_returns_at_once_and_a_later_pause_waits_for_it(monkeypatch):
    import threading
    import time
    async def go():
        admin = HttpOllamaAdmin('http://127.0.0.1:1')
        gate = admin.gpu_gate = PauseGate()
        started, release = threading.Event(), threading.Event()
        landed = []
        def request(path, body, timeout):
            started.set()
            release.wait(5)
            landed.append(path)
        monkeypatch.setattr(admin, '_request', request)
        task = asyncio.create_task(admin.preload('fake', -1))
        try:
            while not started.is_set():
                await asyncio.sleep(.001)
            task.cancel()
            began = time.monotonic()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert time.monotonic() - began < .5 and not landed
            gate.set(True)
            drain = asyncio.create_task(gate.drain())
            await asyncio.sleep(.05)
            assert not drain.done()  # the late preload must land before any unload
            release.set()
            await asyncio.wait_for(drain, 2)
            assert landed == ['/api/generate']
        finally:
            release.set()
    run(go())


def test_pause_after_a_resume_during_slow_cleanup_closes_the_gate_again(tmp_path, monkeypatch):
    from leeroy_mind import gpucontrol
    monkeypatch.setattr(gpucontrol, 'PAUSE_WAIT_S', .05)
    async def go():
        app = app_at(tmp_path)
        scheduler = _night_ready(app)
        entered, cleanup = asyncio.Event(), asyncio.Event()
        async def night(night, window):
            entered.set()
            try:
                await asyncio.Future()
            finally:
                await cleanup.wait()
        scheduler.run_night = night
        try:
            await scheduler.tick()
            await entered.wait()
            first = await app.gpu.action({'action': 'pause'})
            assert first['pending']
            assert (await app.gpu.action({'action': 'resume'}))['ok'] and not app.gpu.paused
            second = await app.gpu.action({'action': 'pause'})
            assert second['ok'] and second['pending'] and app.gpu.paused
            with pytest.raises(GpuPaused):
                app.gpu.gate.check()
            cleanup.set()
            await asyncio.wait_for(app.gpu._finishing, 2)
            assert app.gpu.paused and not app.arbiter.admin.models and not app.gpu.unload_failed
        finally:
            cleanup.set()
            await scheduler.cancel_run()
            await app.stop()
    run(go())
