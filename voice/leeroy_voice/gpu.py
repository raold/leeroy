"""The GPU admission rule (Task 21, voice-common "GPU, CPU and where each model runs").

One RTX 4090 carries live chat (Orion, about 16 GB) and the embedding model, and live chat must
always win. A voice model may sit on the card only while Ollama lists both models, fully on the
GPU, WoW is running, and nvidia-smi shows its need plus a margin free. If Ollama cannot be asked,
the answer is no.

- OllamaPs.loaded(): GET /api/ps, size and size_vram by name; None when it cannot be asked.
- free_vram_gb(), wow_running(): the other two probes. None / False on any failure.
- admit(): the pure rule; None admits, else the reason.
- GpuGuard: checks every CHECK_S while he is online. It loads Whisper when admitted (recognizer
  "auto") and lets go at once when /api/ps shows either model split or missing, or Ollama cannot
  be asked, and on his logout. Another engine (Task 22) uses `acquire(name, need_gb, unload,
  priority=1)` (admission and reservation in one locked step), then `confirm(name)` once loaded
  and `drop(name)` when it stops; see GpuGuard.acquire for the contract, including that an
  engine preempts a held Whisper. Every probe is guarded: a
  failure costs that check, never the service loop. It logs `gpu_budget` every REPORT_S while he
  is online; nothing here logs a model's text or a path."""
import asyncio
import inspect
import json
import logging
import subprocess
import urllib.request
from dataclasses import dataclass
from typing import Callable

from .logs import log

CHECK_S = 10.0
REPORT_S = 600.0
RETRY_S = 60.0  # after a load that failed, and after any release
RESERVE_S = 120.0  # an unconfirmed reservation lapses
UNLOAD_TIMEOUT_S = 10.0
PS_TIMEOUT_S = 2.0
SMI_TIMEOUT_S = 5.0
WOW_PROCESS = "wow.exe"
OLLAMA_URL = "http://127.0.0.1:11434"
_MIB_PER_GB = 1024.0


class OllamaPs:
    def __init__(self, url: str = OLLAMA_URL):
        self.url = url.rstrip("/")

    def loaded(self) -> dict[str, tuple[int, int]] | None:
        """Size and size_vram by name of the models Ollama has loaded; None when it cannot be
        asked or answers something else."""
        try:
            with urllib.request.urlopen(self.url + "/api/ps", timeout=PS_TIMEOUT_S) as reply:
                data = json.loads(reply.read())
            out = {}
            for m in data["models"]:
                out[str(m["name"])] = (_size(m["size"]), _size(m["size_vram"]))
            return out
        except (OSError, ValueError, KeyError, TypeError):  # urllib's errors are OSErrors
            return None


def _size(v) -> int:
    if type(v) is not int or v < 0:
        raise ValueError("size")
    return v


def free_vram_gb() -> float | None:
    """The first GPU's free memory, from nvidia-smi; None when it cannot be read."""
    try:
        done = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=SMI_TIMEOUT_S, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if done.returncode != 0:
            return None
        return float(done.stdout.splitlines()[0].strip()) / _MIB_PER_GB
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None


def wow_running() -> bool:
    try:
        import psutil
        return any((p.info.get("name") or "").lower() == WOW_PROCESS
                   for p in psutil.process_iter(["name"]))
    except Exception:  # psutil's own errors, or no psutil: not running as far as we can tell
        return False


def _find(ps: dict, name: str):
    """The (size, size_vram) of a model; Ollama lists a name with no tag as name:latest."""
    if not name:
        return None
    if name in ps:
        return ps[name]
    if ":" not in name:
        return ps.get(name + ":latest")
    return None


def models_reason(ps, live: str, embed: str) -> str | None:
    """Why live chat is not fully on the GPU, or None when it is."""
    if ps is None:
        return "cannot ask Ollama"
    for label, name in (("live", live), ("embedding", embed)):
        found = _find(ps, name)
        if found is None:
            return f"{label} model not loaded"
        size, in_vram = found
        if in_vram < size:
            return f"{label} model split"
    return None


def admit(*, ps, free_gb, wow, live, embed, need_gb, margin_gb) -> str | None:
    """None admits a model of `need_gb`; else the reason."""
    reason = models_reason(ps, live, embed)
    if reason is not None:
        return reason
    if not wow:
        return "WoW not running"
    if free_gb is None:
        return "free VRAM unreadable"
    need = need_gb + margin_gb
    if free_gb < need:
        return f"{free_gb:.1f} GB free, need {need:.1f}"
    return None


@dataclass
class _Holder:
    unload: Callable[[], object]
    need_gb: float
    priority: int
    confirmed: bool  # False: reserved, not loaded yet, so the card does not show it
    deadline: float  # an unconfirmed reservation lapses here


class GpuGuard:
    """See the module's docstring. `service` offers `online`, `live_model`, `embed_model`,
    `clock`, `wants_whisper`, `build_whisper()` (a thread), `hold_whisper(recognizer)` and
    `drop_whisper()` (the event loop). `ps` has loaded(); `free` and `wow` are callables."""

    def __init__(self, service, *, ps, free, wow, need_gb: float, margin_gb: float):
        self._service = service
        self._ps, self._free, self._wow = ps, free, wow
        self.need_gb, self.margin_gb = need_gb, margin_gb
        self.holders: dict[str, _Holder] = {}
        self.loading = False
        self._lock = asyncio.Lock()
        self._unloading: set[asyncio.Task] = set()
        self._retry_at = 0.0
        self._report_at = 0.0
        self._was_online = False

    # -- for engines (Task 22) --------------------------------------------------------------

    async def admits(self, need_gb: float) -> str | None:
        """Advisory: the rule for a model of `need_gb`, asked now. Nothing is reserved; to load
        something use acquire()."""
        ps, free, wow = await asyncio.to_thread(self._read)
        s = self._service
        return admit(ps=ps, free_gb=free, wow=wow, live=s.live_model, embed=s.embed_model,
                     need_gb=need_gb, margin_gb=self.margin_gb)

    async def acquire(self, name: str, need_gb: float, unload, *, priority: int = 1) -> str | None:
        """Admit and reserve in one step, under a lock: None when `name` may load `need_gb`
        (it is now a holder, unconfirmed), else the reason. Two loads asking at once cannot both
        pass on the same free gigabytes, because a reservation counts against the next request
        until confirm() (the card then shows it) or drop(), or RESERVE_S.

        Precedence: a higher `priority` wins. If the request fails only for want of VRAM and
        releasing the lower-priority holders (Whisper is 0; an expressive engine takes the
        default 1) would make it fit, they are released first: Whisper's recognition falls back
        to Parakeet at once, a press it is hearing still finishes (service.drop_whisper), and
        Whisper comes back only when it fits beside the engine. The freed amount is counted
        from the holders' declared needs, since the card shows it a moment later. A peer or a
        lower rank never preempts.

        `unload` may be a plain callable or a coroutine function; it runs on the event loop,
        once, within UNLOAD_TIMEOUT_S, when a model splits or goes missing, Ollama cannot be
        asked, he logs out, or a higher rank preempts. After a successful load call confirm();
        on a failed load or a normal stop call drop()."""
        async with self._lock:
            s = self._service
            now = s.clock.now()
            ps, free, wow = await asyncio.to_thread(self._read)
            pending = sum(h.need_gb for h in self.holders.values()
                          if not h.confirmed and h.deadline > now)
            args = dict(ps=ps, wow=wow, live=s.live_model, embed=s.embed_model,
                        margin_gb=self.margin_gb)
            reason = admit(free_gb=free, need_gb=need_gb + pending, **args)
            if reason is not None and free is not None:
                lower = {n: h for n, h in self.holders.items()
                         if h.priority < priority and h.confirmed}
                freed = sum(h.need_gb for h in lower.values())
                if lower and admit(free_gb=free + freed, need_gb=need_gb + pending,
                                   **args) is None:
                    self._release(list(lower), "preempted")
                    await self.settle()
                    reason = None
            if reason is None:
                self.holders[name] = _Holder(unload, need_gb, priority, False, now + RESERVE_S)
            return reason

    def hold(self, name: str, unload, *, need_gb: float = 0.0, priority: int = 1) -> None:
        """Register an already-loaded holder (no admission). A name held twice keeps the newer
        entry. See acquire() for `unload`."""
        self.holders[name] = _Holder(unload, need_gb, priority, True, 0.0)

    def confirm(self, name: str) -> None:
        """The reserved model is loaded: the card shows it now."""
        if name in self.holders:
            self.holders[name].confirmed = True

    def drop(self, name: str) -> None:
        """Forget a holder without calling its unload (it has stopped, or never loaded)."""
        self.holders.pop(name, None)

    def release_all(self, why: str) -> None:
        """Every holder lets go at once (his logout; a model gone). Never raises."""
        self._release(list(self.holders), why)

    def _release(self, names: list[str], why: str) -> None:
        held = [(n, self.holders.pop(n)) for n in names if n in self.holders]
        self._retry_at = self._service.clock.now() + RETRY_S  # no reload flapping
        for name, holder in held:
            try:
                result = holder.unload()
                if inspect.isawaitable(result):
                    task = asyncio.ensure_future(self._await_unload(name, result))
                    self._unloading.add(task)
                    task.add_done_callback(self._unloading.discard)
            except Exception as e:
                log("gpu_unload_failed", level=logging.WARNING, holder=name,
                    error=type(e).__name__)
        if held:
            log("gpu_released", holders=",".join(n for n, _ in held), why=why)

    async def _await_unload(self, name: str, awaitable) -> None:
        try:
            await asyncio.wait_for(awaitable, UNLOAD_TIMEOUT_S)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log("gpu_unload_failed", level=logging.WARNING, holder=name, error=type(e).__name__)

    async def settle(self) -> None:
        """Until every asynchronous unload has finished (or timed out)."""
        while self._unloading:
            await asyncio.gather(*list(self._unloading), return_exceptions=True)

    # -- the loop -------------------------------------------------------------------------

    def _read(self):
        """The three probes, each guarded: (ps or None, free GB or None, WoW running)."""
        try:
            ps = self._ps.loaded()
        except Exception:
            ps = None
        try:
            free = self._free()
        except Exception:
            free = None
        try:
            wow = bool(self._wow())
        except Exception:
            wow = False
        return ps, free, wow

    async def run(self) -> None:
        service = self._service
        while True:
            try:
                await self.step()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # nothing here may end the loop
                log("gpu_check_failed", level=logging.WARNING, error=type(e).__name__)
            await service.clock.sleep(CHECK_S)

    async def step(self) -> None:
        """One check. Offline: nothing is held. Online: holds are kept while both models are
        fully on the GPU, else released; with Whisper wanted and not held, it loads when
        admitted."""
        s = self._service
        now = s.clock.now()
        if not s.online:
            self._was_online = False
            if self.holders:
                self.release_all("offline")
            return
        if not self._was_online:
            self._was_online, self._report_at = True, now + REPORT_S
        ps, free, wow = await asyncio.to_thread(self._read)
        if not s.online:
            return
        if now >= self._report_at:
            self._report_at = now + REPORT_S
            self._log_budget(ps, free, wow)
        if self.holders:
            reason = models_reason(ps, s.live_model, s.embed_model)
            if reason is not None:
                self.release_all(reason)
                return
        if not s.wants_whisper or "whisper" in self.holders or now < self._retry_at:
            return
        if await self.acquire("whisper", self.need_gb, s.drop_whisper, priority=0) is None:
            await self._load_whisper()

    async def _load_whisper(self) -> None:
        s = self._service
        self.loading = True
        try:
            recognizer = await asyncio.to_thread(s.build_whisper)
        except Exception as e:
            self.drop("whisper")
            self._retry_at = s.clock.now() + RETRY_S
            log("gpu_load_failed", level=logging.WARNING, error=type(e).__name__)
            return
        finally:
            self.loading = False
        ps, _, _ = await asyncio.to_thread(self._read)  # the card may have moved during the load
        if ("whisper" in self.holders and s.online
                and models_reason(ps, s.live_model, s.embed_model) is None):
            self.confirm("whisper")
            s.hold_whisper(recognizer)
            log("gpu_loaded", holder="whisper")
        else:  # released while it loaded (logout, a split, an engine): keep nothing
            self.drop("whisper")
            await asyncio.to_thread(close_quietly, recognizer)

    def _log_budget(self, ps, free, wow) -> None:
        s = self._service
        log("gpu_budget", free_gb=None if free is None else round(free, 1),
            live=_state(ps, s.live_model), embed=_state(ps, s.embed_model), wow=wow,
            held=",".join(self.holders) or "none")


def _state(ps, name: str) -> str:
    if ps is None:
        return "unknown"
    found = _find(ps, name)
    if found is None:
        return "missing"
    return "gpu" if found[1] >= found[0] else "split"


def close_quietly(recognizer) -> None:
    close = getattr(recognizer, "close", None)
    if close is not None:
        try:
            close()
        except Exception as e:
            log("gpu_unload_failed", level=logging.WARNING, error=type(e).__name__)
