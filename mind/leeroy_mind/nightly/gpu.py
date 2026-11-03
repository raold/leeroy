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
  loaded, and the live model is preloaded and kept loaded, at the num_ctx live requests use;
- release_live: drops the holder and touches no model, because the next night's ensure_model
  makes room for the model that stage needs;
- ensure_model: unloads whichever of the live and reflection models is loaded and is not the one
  asked for, and nothing else (the embedding model is never unloaded);
- foreign_models: what else is loaded, for the night's count of loads it did not ask for.

A lease counts from the call, so it is held even if the GPU work fails or its caller is
cancelled. Use `async with arbiter.live(holder)`, or a bare acquire_live inside the try whose
finally calls release_live (releasing a lease nobody holds does nothing)."""
import asyncio
import contextlib
import http.client
import json
import logging
import urllib.error
import urllib.request
from typing import AsyncIterator, Awaitable, Callable

from leeroy_mind.llm.client import LLMError
from leeroy_mind.llm.presets import LIVE as LIVE_PRESET
from leeroy_mind.logs import log
from leeroy_mind.nightly.settings import NightlySettings

# Seconds the preempt callback gets before the hand-over goes on without it.
PREEMPT_TIMEOUT = 30.0


def norm(name: str) -> str:
    """`name` with Ollama's default tag: a name without a tag means :latest. The tag follows the
    last colon after the last slash, so a registry port (localhost:5000/m) is not one."""
    return name if ":" in name.rsplit("/", 1)[-1] else name + ":latest"


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
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _request(self, path: str, body: dict | None, timeout: float) -> bytes:
        try:
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

    async def loaded_models(self) -> list[str]:
        """The names GET /api/ps lists: the models in memory now."""
        raw = await asyncio.to_thread(self._request, "/api/ps", None, self._timeout)
        try:
            out = json.loads(raw)
        except ValueError as e:
            raise LLMError("/api/ps: reply is not JSON") from e
        if not isinstance(out, dict):
            raise LLMError("/api/ps: reply is not an object")
        return [m["name"] for m in out.get("models") or ()
                if isinstance(m, dict) and isinstance(m.get("name"), str)]

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
        await asyncio.to_thread(self._request, "/api/generate", body, self._preload_timeout)


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

    def reflection_allowed(self) -> bool:
        return not self._holders

    def hold(self, holder: str) -> bool:
        """Take a live lease now, with no await: `holders` and `reflection_allowed()` show it
        from here on. True for the first holder, whose lease still needs hand_over(); a later
        holder, or the same one again, gets False and has nothing more to do. A bridge handler
        calls this inline and runs hand_over() as a task; release_live then ends the lease
        inline, in event order."""
        first = not self._holders
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
        his login finds it loaded. It does nothing if the lease has ended by the time it gets the
        lock. A failed step raises; the lease stays held."""
        async with self._lock:
            if not self._holders:
                return
            await self._run_preempt()
            while self._holders:
                revision, target = self._revision, self._live
                listed = await self.admin.loaded_models()
                if revision != self._revision:
                    continue
                remaining = list(listed)
                for name in listed:
                    if norm(name) in self._managed and norm(name) not in {
                            norm(target), norm(self._embedding)}:
                        await self.admin.unload(name)
                        remaining.remove(name)
                        if revision != self._revision:
                            break
                if revision != self._revision:
                    continue
                if not self._holders:
                    self._prune_managed(remaining)
                    return
                await self.admin.preload(target, -1,
                                         options={"num_ctx": LIVE_PRESET.options["num_ctx"]})
                if revision == self._revision:
                    self._prune_managed([*remaining, target])
                    return

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
        holds none is ignored."""
        self._holders.discard(holder)

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
        first request."""
        revision = self._revision
        async with self._lock:
            if revision != self._revision:
                raise RuntimeError("nightly models changed during model admission")
            listed = await self.admin.loaded_models()
            if revision != self._revision:
                raise RuntimeError("nightly models changed during model admission")
            remaining = list(listed)
            for other in self._swappable():
                if norm(other) != norm(model):
                    await self._unload(other, listed)
                    remaining = [n for n in remaining if norm(n) != norm(other)]
                    if revision != self._revision:
                        raise RuntimeError("nightly models changed during model admission")
            self._prune_managed(remaining)

    async def foreign_models(self, expected: str) -> list[str]:
        """The loaded models other than `expected` and the embedding model, as Ollama lists
        them. It only looks."""
        keep = {norm(expected), norm(self._embedding)}
        return [n for n in await self.admin.loaded_models() if norm(n) not in keep]
