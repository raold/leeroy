"""The GPU arbiter (P1c Task 3). The reflection model (27B) cannot sit on the 4090 beside the live
chat model (about 16 GB), so the two take turns: the reflection model may run only while nobody
holds a live lease. The owner being online is one lease, and a P1d offline raid is another.

HttpOllamaAdmin is Ollama's model admin over HTTP, what `ollama ps` and `ollama stop` do. The
arbiter takes anything with its three coroutines: tests pass a fake, and NullAdmin stands in
where there is no Ollama.

GpuArbiter keeps the leases and does the GPU work. A lease changes at once, in call order: hold
and release_live have no await, so `holders` and `reflection_allowed()` are right the moment they
return. That lets a bridge handler flip the lease inline and run the GPU work as a task of its
own. The lock serializes only the GPU work:

- hold / acquire_live: the first holder needs the GPU handed over (hand_over): the preempt
  callback (set_preempt) cancels the night's run, the reflection model is unloaded if it is
  loaded, and the live model is preloaded and kept loaded, at the num_ctx live requests use.
  After an image stage shared the GPU (make_room, below), a live model it finds loaded is
  unloaded first, so the preload loads it whole;
- release_live: drops the holder; when it was the last, a let-go task (under the lock, if nobody
  holds a lease by then) leaves the live model loaded for only RELEASED_KEEP_ALIVE more, instead
  of the pin that kept it until the next night's ensure_model;
- ensure_model: unloads whichever of the live and reflection models is loaded and is not the one
  asked for, and nothing else (the embedding model is never unloaded);
- make_room: for a stage that takes the whole GPU (an image stage), unloads every model Ollama
  lists but the embedding model. The live model goes like any other, whether the let-go kept it
  or mod-ollama-chat loaded it at night. It loads nothing and may be called again mid-stage.
  While any live lease is held it refuses (LiveLeaseHeld) and unloads nothing more. Once it
  has begun, the GPU counts as shared: a live model Ollama loads while the stage's worker holds
  its VRAM goes partly onto the CPU, and Ollama would reuse that runner for a preload at the
  same num_ctx. So the next hand_over unloads a loaded live model before its preload; the mark
  ends with that preload, or with an ensure_model that unloads the live model;
- foreign_models: what else is loaded, for the night's count of loads it did not ask for (with
  None, every loaded model but the embedding model).

A lease counts from the call, so it is held even if the GPU work fails or its caller is
cancelled. Use `async with arbiter.live(holder)`, or a bare acquire_live inside the try whose
finally calls release_live (releasing a lease nobody holds does nothing)."""
import asyncio
import contextlib
import http.client
import json
import logging
import time
import urllib.error
import urllib.request
from typing import AsyncIterator, Awaitable, Callable

from leeroy_mind.llm.client import LLMError, GpuPaused, OllamaClient
from leeroy_mind.llm.presets import LIVE as LIVE_PRESET
from leeroy_mind.logs import log
from leeroy_mind.nightly.settings import NightlySettings

# Seconds the preempt callback gets before the hand-over goes on without it.
PREEMPT_TIMEOUT = 30.0
# How long Ollama keeps the live model once the last lease ends: a relog finds it loaded, and an
# evening with nobody online gives the 16 GB back.
RELEASED_KEEP_ALIVE = "10m"


def norm(name: str) -> str:
    """`name` with Ollama's default tag: a name without a tag means :latest. The tag follows the
    last colon after the last slash, so a registry port (localhost:5000/m) is not one."""
    return name if ":" in name.rsplit("/", 1)[-1] else name + ":latest"


class LiveLeaseHeld(RuntimeError):
    """make_room refused: a live lease is held (his login, an offline raid, voice, a preview,
    his Discord), so nothing that needs the whole GPU may run now. `holders` names the leases."""

    def __init__(self, holders: frozenset[str]):
        self.holders = frozenset(holders)
        super().__init__(f"a live lease is held by {', '.join(sorted(self.holders))}: "
                         "no room for a stage that needs the whole GPU")


class PausedRoom(GpuPaused, LiveLeaseHeld):
    """make_room refused by the owner's GPU pause. A GpuPaused, so the night and the booth count
    it as an interruption and never a failure; and a LiveLeaseHeld, so every image path that
    stops for a lease (worker start, a slot's room after an oom, the snaps) stops for the pause
    the same way, without failing a unit or a set."""

    def __init__(self):
        self.holders = frozenset({"gpu_pause"})
        self.diagnostics = None
        Exception.__init__(self, "The GPU is paused.")


def _error_body(e: urllib.error.HTTPError) -> str:
    try:
        return e.read()[:200].decode("utf-8", "replace")
    except Exception:  # an error that carries no body
        return ""


class HttpOllamaAdmin:
    """Ollama's model admin over HTTP. Each call runs urllib in a worker thread, so a slow model
    load never blocks the event loop, and returns once Ollama has answered in full.

    `timeout` bounds each wait on the socket for /api/ps and an unload. A preload gets
    `preload_timeout`: Ollama sends nothing until the model is loaded and drops a load whose
    client has gone, so a 16 GB load from disk must not be cut off at 30 s. Requests go through
    an opener that ignores the system proxy settings, as P1b's client, which talks to Ollama
    directly, does. A failure (Ollama down, an HTTP error, a reply that is not JSON) is an
    LLMError, as in P1b's client."""

    def __init__(self, base_url: str, *, timeout: float = 30.0, preload_timeout: float = 120.0):
        self._base = base_url.rstrip("/")
        self._timeout = timeout
        self._preload_timeout = preload_timeout
        self.gpu_gate = None
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _request(self, path: str, body: dict | None, timeout: float) -> bytes:
        try:
            if self.gpu_gate is not None and body is not None and body.get('keep_alive') != 0:
                self.gpu_gate.check()
            data = None if body is None else json.dumps(body).encode("utf-8")
            req = urllib.request.Request(
                self._base + path, data=data, method="GET" if body is None else "POST",
                headers={} if data is None else {"Content-Type": "application/json"})
            with self._opener.open(req, timeout=timeout) as reply:
                return reply.read()
        except urllib.error.HTTPError as e:
            raise LLMError(f"{path}: HTTP {e.code} {_error_body(e)}".strip()) from e
        except (OSError, http.client.HTTPException, ValueError) as e:
            raise LLMError(f"{path}: {e}") from e

    async def loaded(self, *, timeout: float | None = None) -> list[dict]:
        """The names GET /api/ps lists: the models in memory now."""
        raw = await asyncio.to_thread(self._request, "/api/ps", None, self._timeout if timeout is None else timeout)
        try:
            out = json.loads(raw)
        except ValueError as e:
            raise LLMError("/api/ps: reply is not JSON") from e
        if not isinstance(out, dict):
            raise LLMError("/api/ps: reply is not an object")
        return [{"name": m["name"], "size_vram": max(0, m.get("size_vram", 0))
                 if type(m.get("size_vram")) is int else 0} for m in out.get("models") or ()
                if isinstance(m, dict) and isinstance(m.get("name"), str)]

    async def loaded_models(self) -> list[str]:
        return [m["name"] for m in await self.loaded()]

    async def unload(self, model: str) -> None:
        """What `ollama stop` does: keep_alive 0."""
        await asyncio.to_thread(self._request, "/api/generate",
                                {"model": model, "keep_alive": 0}, self._timeout)

    async def preload(self, model: str, keep_alive: int | str, *,
                      options: dict | None = None) -> None:
        """Load the model and keep it for `keep_alive` (-1: until something unloads it). Ollama
        reloads a model whose next request has a different num_ctx, so a preload that is to
        survive names that context in `options`; they ride along only when given."""
        body = {"model": model, "keep_alive": keep_alive}
        if options:
            body["options"] = dict(options)
        gate = self.gpu_gate
        if gate is None:
            await asyncio.to_thread(self._request, "/api/generate", body, self._preload_timeout)
            return

        async def run():
            gate.check()
            work = asyncio.create_task(asyncio.to_thread(
                self._request, "/api/generate", body, self._preload_timeout))
            try:
                await asyncio.shield(work)
            except asyncio.CancelledError:
                # A pause unloads next, so it waits for this load to land first. Any other
                # cancel (a hand-over stopped at shutdown) returns at once, as it always did;
                # the gate keeps the load in flight for a later pause's drain.
                if gate.paused:
                    await OllamaClient._drain(work)
                else:
                    gate.linger(work)
                raise
        await gate.run(run)


class NullAdmin:
    """The admin where there is no Ollama to administer (P1b's app tests inject a FakeLLM):
    nothing is loaded, and unload and preload do nothing."""

    async def loaded_models(self) -> list[str]:
        return []

    async def unload(self, model: str) -> None:
        return None

    async def preload(self, model: str, keep_alive: int | str, *,
                      options: dict | None = None) -> None:
        return None


class GpuArbiter:
    """Who may use the GPU. `holders` are the live leases (the owner online is "human", a P1d
    offline raid is "offline_raid"), and `reflection_allowed()` is true only when there are none.
    Model names come from the settings; they are compared with norm(), and a model is unloaded
    under the name Ollama lists it by. `preempt_timeout` bounds the preempt callback."""

    def __init__(self, admin, settings: NightlySettings, *,
                 preempt_timeout: float = PREEMPT_TIMEOUT):
        self.admin = admin
        self._live = settings.live_model
        self._reflection = settings.reflection_model
        self._embedding = settings.embedding_model
        self._url = settings.ollama_url.rstrip("/")
        self._revision = 0
        self._managed = {norm(m): m for m in (self._live, self._reflection)
                         if norm(m) != norm(self._embedding)}
        self._preempt_timeout = preempt_timeout
        self._holders: set[str] = set()
        self._preempt: Callable[[], Awaitable[None]] | None = None
        self._lock = asyncio.Lock()
        self._letting_go: set[asyncio.Task] = set()
        self.paused = False
        self.on_login = None
        self.needs_live_reload = False
        self._shared_gpu = False  # an image stage began: a loaded live model may be split
        self.live_admission = None  # opt-in requested-photo gate; absent leaves old paths alone
        self.borrow_busy = None
        self.borrow_timeout = 180.0
        self._borrow = None
        self._borrow_cleanup = None

    def reconfigure(self, settings: NightlySettings) -> bool:
        """Change the swappable names inline, without waiting for a model operation.

        The client endpoint and embedding model belong to the app's startup resources and
        require a restart. Retired live/reflection names remain ours until a successful
        reconciliation confirms them unloaded; unrelated and embedding models stay untouched.
        A hand-back in flight follows the newest revision. An in-flight ensure_model instead
        fails, so its old context cannot proceed with a model that the reload superseded.
        Callers must cancel an old night's run before reconfiguring it for a live login.
        """
        if settings.ollama_url.rstrip("/") != self._url:
            raise ValueError("changing the Ollama endpoint requires a restart")
        if norm(settings.embedding_model) != norm(self._embedding):
            raise ValueError("changing the embedding model requires a restart")
        changed = (norm(settings.live_model), norm(settings.reflection_model)) != (
            norm(self._live), norm(self._reflection))
        self._live, self._reflection = settings.live_model, settings.reflection_model
        for model in (self._live, self._reflection):
            if norm(model) != norm(self._embedding):
                self._managed[norm(model)] = model
        if changed:
            self._revision += 1
            self.cancel_borrow('models_changed')
        return changed

    def _prune_managed(self, listed: list[str]) -> None:
        """Keep the current pair and retired names still resident after reconciliation."""
        keep = {norm(self._live), norm(self._reflection), *(norm(n) for n in listed)}
        self._managed = {key: name for key, name in self._managed.items() if key in keep}

    def set_preempt(self, cb: Callable[[], Awaitable[None]]) -> None:
        """`cb` cancels the night's run and waits for it to stop. hand_over awaits it with the
        lock held, for at most preempt_timeout seconds: if it is still running then, the expiry
        is logged (nightly_preempt_timeout) and the hand-over goes on. So `cb` should return
        promptly, and neither it nor the run's cleanup, which it waits for, may call
        ensure_model or hand_over: they wait for the lock."""
        self._preempt = cb

    @property
    def holders(self) -> frozenset[str]:
        return frozenset(self._holders)

    def reflection_allowed(self, *, gate: bool = True) -> bool:
        return (not self.paused and not self._holders and
                not (gate and self.live_admission is not None and self.live_admission.blocked))

    def hold(self, holder: str) -> bool:
        """Take a live lease now, with no await: `holders` and `reflection_allowed()` show it
        from here on. True for the first holder, whose lease still needs hand_over(); a later
        holder, or the same one again, gets False and has nothing more to do. A bridge handler
        calls this inline and runs hand_over() as a task; release_live then ends the lease
        inline, in event order."""
        borrow_hard = self._borrow is not None and holder not in {'server', 'discord', self._borrow.holder}
        if borrow_hard:
            self.cancel_borrow('live_lease')
        if self.paused:
            if holder != 'human':
                raise GpuPaused('The GPU is paused.')
            if self.on_login is not None:
                self.on_login()
            self.paused = False
        first = borrow_hard or not self._holders or self.needs_live_reload
        self.needs_live_reload = False
        self._holders.add(holder)
        return first

    async def _run_preempt(self) -> None:
        if self._preempt is None:
            return
        timer = asyncio.timeout(self._preempt_timeout)
        try:
            async with timer:
                await self._preempt()
        except TimeoutError:
            if not timer.expired():
                raise  # the callback's own TimeoutError, not our deadline
        if timer.expired():
            log("nightly_preempt_timeout", level=logging.WARNING, seconds=self._preempt_timeout)

    async def _unload(self, model: str, listed: list[str]) -> None:
        for name in listed:
            if norm(name) == norm(model):
                await self.admin.unload(name)

    async def hand_over(self) -> None:
        """The GPU work of a first lease, under the lock: await the preempt callback, unload the
        reflection model if it is loaded (not when it is the live model too), then preload the
        live model with keep_alive -1 at the num_ctx live requests use, so the first reply after
        his login finds it loaded. After an image stage shared the GPU (make_room), a loaded
        live model is unloaded too, since it may have loaded beside the worker partly on the
        CPU: the preload then loads it whole, and the mark ends there. It does nothing if the
        lease has ended by the time it gets the lock. A failed step raises; the lease stays
        held."""
        async with self._lock:
            if self.paused or not self._holders:
                return
            await self._run_preempt()
            if self.live_admission is not None and self.live_admission.blocked and self._borrow is None:
                from leeroy_mind.selfies.worker import to_the_end
                await to_the_end(self._recover_borrow_locked())
            else:
                await self._hand_over_locked()

    async def _hand_over_locked(self, *, force=False):
        """Shared reconciliation; a borrow's cleanup already owns the lock."""
        while (self._holders or force) and not self.paused:
            revision, target = self._revision, self._live
            listed = await self.admin.loaded_models()
            if revision != self._revision:
                continue
            remaining = list(listed)
            for name in listed:
                split = self._shared_gpu and norm(name) == norm(target)
                if split or (norm(name) in self._managed and norm(name) not in {
                        norm(target), norm(self._embedding)}):
                    await self.admin.unload(name)
                    remaining.remove(name)
                    if revision != self._revision:
                        break
            if revision != self._revision:
                continue
            if self.paused or not (self._holders or force):
                self._prune_managed(remaining)
                return False
            await self.admin.preload(target, -1,
                                     options={"num_ctx": LIVE_PRESET.options["num_ctx"]})
            if revision == self._revision:
                self._shared_gpu = False
                self._prune_managed([*remaining, target])
                return not self.paused
        return False

    @property
    def borrow_holder(self):
        return self._borrow.holder if self._borrow is not None else None

    def cancel_borrow(self, reason='cancelled'):
        if self._borrow is not None:
            self._borrow.cancel(reason)

    async def wait_borrow(self):
        room = self._borrow
        if room is not None and room.owner is not asyncio.current_task():
            await room.done.wait()

    async def close_borrow(self):
        self.cancel_borrow('shutdown')
        await self.wait_borrow()
        if self._borrow_cleanup is not None:
            from leeroy_mind.selfies.worker import to_the_end
            async with self._lock:
                await to_the_end(self._borrow_cleanup.stop_workers())
                self._borrow_cleanup = None

    async def _recover_borrow_locked(self):
        from leeroy_mind.llm.borrow import LiveReloadFailed
        if self._borrow_cleanup is not None:
            try:
                await self._borrow_cleanup.stop_workers()
            except Exception:
                self.needs_live_reload = True
                self.live_admission.fail('worker_stop_failed')
                raise LiveReloadFailed('worker_stop_failed') from None
            self._borrow_cleanup = None
        if self.paused:
            raise GpuPaused('The GPU is paused.')
        # A transient load failure gets one bounded retry. Failed recovery keeps the
        # gate closed; a later live_ready tries recovery before waiting on that gate.
        for attempt in range(2):
            try:
                ready = await self._hand_over_locked(force=True)
                if not ready:
                    raise GpuPaused('The GPU is paused.')
                self.needs_live_reload = False
                self.live_admission.open()
                return
            except GpuPaused:
                self.needs_live_reload = True
                self.live_admission.fail('gpu_paused')
                raise
            except Exception:
                self.needs_live_reload = True
                if attempt:
                    self.live_admission.fail()
                    raise LiveReloadFailed('live_reload_failed') from None

    async def recover_borrow(self):
        gate = self.live_admission
        if gate is None or not gate.blocked:
            return
        if self._borrow is not None:
            await self.wait_borrow()
        if gate.blocked:
            from leeroy_mind.selfies.worker import to_the_end
            async with self._lock:
                if gate.blocked:
                    await to_the_end(self._recover_borrow_locked())

    @contextlib.asynccontextmanager
    async def borrow(self, holder, *, on_admitted=None):
        """Own an image pass, its worker teardown and live reload as one admission.

        Only server/Discord leases may coexist. The 180s work cap covers active-chat
        drain, unloading and the caller's render. Teardown/reload must still finish
        safely afterward; all of that closed-admission time credits waiting turns.
        """
        from leeroy_mind.llm.borrow import BorrowInterrupted, LiveReloadFailed
        from .borrow import BorrowRoom
        gate = self.live_admission
        if not isinstance(holder, str) or not holder or holder in {
                'server', 'discord', 'human', 'offline_raid', 'voice_gpu'}:
            raise ValueError('borrow needs its own request holder')
        if gate is None:
            raise LiveLeaseHeld(frozenset({'requests_disabled'}))
        if self._borrow is not None:
            raise LiveLeaseHeld(frozenset({self._borrow.holder}))
        async with self._lock:
            if self.paused:
                raise PausedRoom()
            if held := self.holders - {'server', 'discord'}:
                raise LiveLeaseHeld(held)
            if self.borrow_busy is not None and self.borrow_busy():
                raise LiveLeaseHeld(frozenset({'image_work'}))
            if gate.blocked:
                from leeroy_mind.selfies.worker import to_the_end
                await to_the_end(self._recover_borrow_locked())
            if self.paused:
                raise PausedRoom()
            if held := self.holders - {'server', 'discord'}:
                raise LiveLeaseHeld(held)
            if self.borrow_busy is not None and self.borrow_busy():
                raise LiveLeaseHeld(frozenset({'image_work'}))
            if on_admitted is not None and on_admitted() is False:
                raise LiveLeaseHeld(frozenset({'request_budget'}))
            room = BorrowRoom(self, holder)
            # Inline holder + gate close prevent a new night/booth or chat exchange
            # between the busy checks and the first await.
            self._borrow = room
            self._holders.add(holder)
            gate.close()
            error = None
            try:
                async with asyncio.timeout(self.borrow_timeout):
                    began = time.monotonic()
                    await gate.drain()
                    room.timings['drain_ms'] = round((time.monotonic() - began) * 1000)
                    await room.make_room()
                    yield room
            except BaseException as caught:
                error = caught
            finally:
                room.cleaning = True
                cleanup = asyncio.create_task(room.finish(), name='selfie-borrow-cleanup')
                cancelled = False
                while not cleanup.done():
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        cancelled = True
                    except BaseException:
                        break  # retrieve below, after the cleanup task has finished
                cleanup_error = None
                try:
                    cleanup.result()
                except BaseException as caught:
                    cleanup_error = caught
                    self._borrow_cleanup = room
                    self.needs_live_reload = True
                    gate.fail()
                self._borrow = None
                self._holders.discard(holder)
                room.done.set()
                if cancelled:
                    raise asyncio.CancelledError()
                if isinstance(error, asyncio.CancelledError) and room.cancelled_owner:
                    room.owner.uncancel()
                    if not room.owner.cancelling():
                        error = BorrowInterrupted(room.reason)
                if error is not None:
                    raise error
                if cleanup_error is not None:
                    raise LiveReloadFailed('live_reload_failed') from cleanup_error

    async def acquire_live(self, holder: str) -> None:
        """hold(holder) and, for the first lease, hand_over(): it returns once the GPU is the live
        model's. The lease counts from the call, so a caller that fails or is cancelled here still
        holds it: put the call inside the try whose finally calls release_live, or use live().
        To flip the lease inline and run the GPU work elsewhere, call hold() and spawn
        hand_over(): a spawned acquire_live takes its lease only when its task first runs."""
        if self.hold(holder):
            await self.hand_over()

    async def release_live(self, holder: str) -> None:
        """Drop a lease now, with no await, so it stays in event order with hold. A holder that
        holds none is ignored. The last lease to end spawns the let-go (settled() waits for it)."""
        if holder not in self._holders:
            return
        self._holders.discard(holder)
        if not self._holders:
            task = asyncio.get_running_loop().create_task(self._let_go(), name="gpu-let-go")
            self._letting_go.add(task)
            task.add_done_callback(self._let_go_done)

    async def _let_go(self) -> None:
        """The GPU work of the last release, under the lock: if nobody holds a lease by then and
        the live model is loaded, Ollama keeps it only RELEASED_KEEP_ALIVE longer, at the num_ctx
        live requests use so it is not reloaded. It never loads a model, and a login that came
        first keeps the pin its hand-over set."""
        async with self._lock:
            if (self.paused or self._holders or
                    self.live_admission is not None and self.live_admission.blocked):
                return
            for name in await self.admin.loaded_models():
                if norm(name) == norm(self._live):
                    await self.admin.preload(name, RELEASED_KEEP_ALIVE,
                                             options={"num_ctx": LIVE_PRESET.options["num_ctx"]})
                    return

    def _let_go_done(self, task: asyncio.Task) -> None:
        self._letting_go.discard(task)
        if not task.cancelled() and (e := task.exception()) is not None:
            log("gpu_let_go_failed", level=logging.WARNING, error=str(e))

    async def settled(self) -> None:
        """Wait for the let-gos in flight."""
        if self._letting_go:
            await asyncio.wait(set(self._letting_go))

    @contextlib.asynccontextmanager
    async def live(self, holder: str) -> AsyncIterator[None]:
        """`async with arbiter.live("offline_raid"):` holds the lease for the block, after the GPU
        is handed over, and gives it back however the block ends: it finishes, raises, or the
        caller is cancelled, and so when the hand-over itself fails or is cancelled."""
        try:
            await self.acquire_live(holder)
            yield
        finally:
            await self.release_live(holder)

    def _swappable(self) -> list[str]:
        """Current and not-yet-reconciled retired models, excluding the embedding model."""
        return list(self._managed.values())

    async def ensure_model(self, model: str) -> None:
        """Make room for `model`: unload whichever of the live and reflection models is loaded
        and is not `model`. No other model is touched, and `model` itself is left to load on its
        first request. A live model it unloads ends the shared-GPU mark (make_room): it can
        only load whole again."""
        revision = self._revision
        async with self._lock:
            if self.paused:
                raise GpuPaused('The GPU is paused.')
            if self.live_admission is not None and self.live_admission.blocked:
                from leeroy_mind.llm.borrow import LiveReloadFailed
                raise LiveReloadFailed('live_recovery_pending')
            if revision != self._revision:
                raise RuntimeError("nightly models changed during model admission")
            listed = await self.admin.loaded_models()
            if revision != self._revision:
                raise RuntimeError("nightly models changed during model admission")
            remaining = list(listed)
            for other in self._swappable():
                if norm(other) != norm(model):
                    loaded = any(norm(n) == norm(other) for n in listed)
                    await self._unload(other, listed)
                    remaining = [n for n in remaining if norm(n) != norm(other)]
                    if revision != self._revision:
                        raise RuntimeError("nightly models changed during model admission")
                    if loaded and norm(other) == norm(self._live):
                        self._shared_gpu = False
            self._prune_managed(remaining)

    def _refuse_if_held(self) -> None:
        if self.paused:
            raise PausedRoom()
        if self._holders:
            raise LiveLeaseHeld(frozenset(self._holders))
        if self.live_admission is not None and self.live_admission.blocked:
            raise LiveLeaseHeld(frozenset({'request_recovery'}))

    async def make_room(self) -> None:
        """Make room for a stage that needs the whole GPU: unload every model Ollama lists but
        the embedding model. That is the live and reflection models, current and retired, and
        any other chat model too, since one loaded at night takes the same VRAM. Nothing is
        loaded. A stage may call it again when something loaded a model meanwhile.

        It never runs while a live lease is held: LiveLeaseHeld, at the call or as soon as a
        lease is taken while it waits or works, and nothing more is unloaded. The live model is
        unloaded last, so a lease taken meanwhile finds it still loaded. A reload (reconfigure)
        while it waits or works fails it, as ensure_model. Past the lease check under the lock,
        it marks the GPU shared, so the next hand_over reloads the live model whole."""
        self._refuse_if_held()
        revision = self._revision
        async with self._lock:
            self._refuse_if_held()
            if revision != self._revision:
                raise RuntimeError("nightly models changed during model admission")
            self._shared_gpu = True  # a worker may hold VRAM from here on
            listed = await self.admin.loaded_models()
            self._refuse_if_held()
            if revision != self._revision:
                raise RuntimeError("nightly models changed during model admission")
            remaining = list(listed)
            for name in sorted(listed, key=lambda n: norm(n) == norm(self._live)):
                if norm(name) != norm(self._embedding):
                    await self.admin.unload(name)
                    remaining.remove(name)
                    self._refuse_if_held()
                    if revision != self._revision:
                        raise RuntimeError("nightly models changed during model admission")
            self._prune_managed(remaining)

    async def foreign_models(self, expected: str | None) -> list[str]:
        """The loaded models other than `expected` and the embedding model, as Ollama lists
        them; with None (an image stage expects no chat model), every loaded model but the
        embedding model. It only looks."""
        keep = {norm(self._embedding)}
        if expected is not None:
            keep.add(norm(expected))
        return [n for n in await self.admin.loaded_models() if norm(n) not in keep]
