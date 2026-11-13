"""Owner-controlled GPU pause; no timer or background work may end it."""
import asyncio
import json
import math

from .llm.pause import PauseGate
from .logs import log

KEY = 'gpu:paused'
IN_GAME = 'Log out of WoW first: your game keeps the model loaded'
UNKNOWN = "Can't tell whether you're in WoW yet; try again in a moment."


def since_value(raw):
    if raw is None:
        return None
    try:
        at = json.loads(raw)['at']
        return float(at) if type(at) in (float, int) and math.isfinite(at) and at >= 0 else 0.0
    except (ValueError, TypeError, KeyError):
        return 0.0  # a malformed stored pause must not silently enable the GPU


def arguments(args):
    if not isinstance(args, dict) or set(args) != {'action'} or args['action'] not in (
            'status', 'pause', 'resume'):
        raise ValueError('gpu needs action: status, pause or resume')
    return dict(args)


async def loaded(admin):
    from .nightly.gpu import HttpOllamaAdmin
    if isinstance(admin, HttpOllamaAdmin):
        return await admin.loaded(timeout=.5)
    if callable(getattr(admin, 'loaded', None)):
        return await admin.loaded()
    return [{'name': n, 'size_vram': 0} for n in await admin.loaded_models()]


class GpuControl:
    def __init__(self, app):
        self.app = app
        self.since = since_value(app.store.get_meta(KEY))
        self.gate = PauseGate(self.since is not None)
        self._lock = asyncio.Lock()
        self._restore_tasks = set()
        self._loaded = []
        self.unload_failed = self.gate.paused
        self._status_task = None
        self._status_at = -float('inf')
        app.arbiter.paused = self.gate.paused
        app.arbiter.on_login = lambda: self.resume_now('login')
        app.scheduler.gpu_gate = self.gate
        app.extractor.gpu_gate = self.gate
        app.writer.gpu_gate = self.gate
        if hasattr(app.llm, 'gpu_gate'):
            app.llm.gpu_gate = self.gate
        if hasattr(app.arbiter.admin, 'gpu_gate'):
            app.arbiter.admin.gpu_gate = self.gate
        voice = getattr(app, 'voice', None)
        if voice is not None and hasattr(voice, 'gpu_pins'):
            voice.gpu_pins.gpu_gate = self.gate
        app.register_shutdown_drain(self.close)

    @property
    def paused(self):
        return self.gate.paused

    def refusal(self):
        app = self.app
        holders = app.arbiter.holders
        if 'human' in holders:
            return 'gpu_in_game', IN_GAME
        try:
            # Known only from a hello on a live bridge: before the first one, while the bridge
            # is down or when the session cannot be read, he may be in WoW.
            known = getattr(app, 'hello', None) is not None and app.bridge.connected
            online = app.world.human_online, app.night_scheduler.presence.online
        except Exception:
            known = False
        if known and any(online):
            return 'gpu_in_game', IN_GAME
        if not known or online[1] is None:
            return 'gpu_presence_unknown', UNKNOWN
        if 'offline_raid' in holders:
            return 'gpu_held', 'An offline raid holds the GPU. Stop it in the Raid tab first.'
        other = holders - {'server', 'discord', 'voice_gpu'}
        if other:
            # No training job takes a GPU lease today: lab trials and benchmarks are native body
            # jobs with no model. The words stay as a defence should one ever take a lease.
            return 'gpu_held', ('A Training lab trial holds the GPU. Stop it in the Training tab first.'
                if any('lab' in h or 'train' in h for h in other) else
                'A live GPU job holds the GPU (' + ', '.join(sorted(other)) + '). Stop it first.')
        return None, None

    def snapshot(self):
        return {'paused': self.paused, 'since': self.since, 'loaded': list(self._loaded),
                'holders': sorted(self.app.arbiter.holders), 'refusal': self.refusal()[1],
                'enabled': True, 'mind_down': False, 'unload_failed': self.unload_failed}

    async def status(self):
        try:
            self._loaded = await loaded(self.app.arbiter.admin)
        except Exception:
            if self.paused:
                self.unload_failed = True
            return {'ok': False, **self.snapshot(), 'error': 'gpu_status_failed',
                    'message': 'Ollama did not answer the GPU status request.'}
        if self.paused and not self._loaded:
            self.unload_failed = False
        return {'ok': True, **self.snapshot()}

    def refresh_status(self):
        now = asyncio.get_running_loop().time()
        if self._status_task is not None and not self._status_task.done():
            return
        if now - self._status_at < 5:
            return
        self._status_at = now
        self._status_task = asyncio.create_task(self._refresh_status(), name='gpu-residency')

    async def _refresh_status(self):
        try:
            await asyncio.wait_for(self.status(), 1)
        except TimeoutError:
            if self.paused:
                self.unload_failed = True

    async def action(self, args):
        action = arguments(args)['action']
        if action == 'status':
            return await self.status()
        if action == 'resume':
            self.resume_now('console')
            await self.restore()
            return {'ok': True, **self.snapshot(), 'message': 'GPU use resumed.'}
        async with self._lock:
            code, reason = self.refusal()
            if code:
                log('gpu_pause_refused', reason=code)
                return {'ok': False, **self.snapshot(), 'error': code, 'message': reason}
            if not self.paused:
                self.since = self.app.clock.now()
                with self.app.store.transaction():
                    self.app.store.set_meta(KEY, json.dumps({'at': self.since}))
            self.unload_failed = True
            self.gate.set(True)
            self.app.arbiter.paused = True
            self.app.arbiter.needs_live_reload = True
            names, freed = [], 0
            try:
                server = getattr(self.app, 'server', None)
                if server is not None:
                    await server.lease._release('gpu_pause')
                    await server.engine.tick(self.app.clock.now())
                if not self.paused:
                    return self._interrupted()
                discord = getattr(self.app, 'discord_talk', None)
                if discord is not None:
                    await discord._release()
                voice = getattr(self.app, 'voice', None)
                tasks = tuple(getattr(voice, '_gpu_tasks', ()))
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
                # The existing preempt waits for the night and photo worker's whole tree.
                # Unlike login's bounded preempt, do not claim success before cleanup ends.
                async with self.app.arbiter._lock:
                    if not self.paused:
                        return self._interrupted()
                    if self.app.arbiter._preempt is not None:
                        await self.app.arbiter._preempt()
                    if not self.paused:
                        return self._interrupted()
                    await self.gate.drain()
                    self._loaded = await loaded(self.app.arbiter.admin)
                    for model in tuple(self._loaded):
                        if not self.paused:
                            break  # a login or his post superseded this pause
                        await self.app.arbiter.admin.unload(model['name'])
                        names.append(model['name'])
                        freed += model['size_vram']
                        self._loaded = [m for m in self._loaded if m['name'] != model['name']]
                    self._loaded = [m for m in self._loaded if m['name'] not in names]
            except Exception:
                self.unload_failed = True
                return {'ok': False, **self.snapshot(), 'error': 'gpu_pause_failed',
                        'unloaded': names, 'freed_bytes': freed,
                        'message': 'GPU use is paused, but unloading did not finish. Try Free GPU again.'}
            if not self.paused:
                return self._interrupted()
            self.unload_failed = False
            log('gpu_paused', unloaded=len(names), vram_mb=round(freed / 1048576))
            return {'ok': True, **self.snapshot(), 'unloaded': names, 'freed_bytes': freed}

    def _interrupted(self):
        return {'ok': False, **self.snapshot(), 'error': 'gpu_resumed',
                'message': 'GPU use resumed after your login or Discord post.'}

    def resume_now(self, reason):
        if reason not in ('console', 'login', 'discord'):
            raise ValueError('invalid GPU resume reason')
        if not self.paused:
            return False
        with self.app.store.transaction():
            self.app.store.conn.execute('DELETE FROM meta WHERE key=?', (KEY,))
        self.since = None
        self.unload_failed = False
        self.app.arbiter.paused = False
        self.gate.set(False)
        log('gpu_resumed', reason=reason)
        if reason != 'console':
            task = asyncio.create_task(self.restore(), name='gpu-resume')
            self._restore_tasks.add(task)
            task.add_done_callback(self._restored)
        return True

    def _restored(self, task):
        self._restore_tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log('gpu_resume_failed', reason=type(task.exception()).__name__)

    async def restore(self):
        async with self._lock:
            if self.paused or self.app.stopping:
                return
            server = getattr(self.app, 'server', None)
            if server is not None and server.config.enabled:
                await server.lease.tick(self.app.clock.now())
                await server.lease.handback()

    async def close(self):
        tasks = tuple(self._restore_tasks) + ((self._status_task,) if self._status_task is not None else ())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
