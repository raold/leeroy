import asyncio
import http.client
import json
import logging
import math
import re
import select
import socket
import threading
import time
import urllib.parse
from dataclasses import dataclass
from typing import Protocol

log = logging.getLogger("leeroy_mind")

_THINK_RE = re.compile(r"<think>(.*?)</think>", re.DOTALL)
_OPEN_THINK_RE = re.compile(r"<think>.*\Z", re.DOTALL)


class LLMError(Exception):
    pass


@dataclass(frozen=True)
class ChatResult:
    text: str
    thinking: str
    eval_count: int
    seconds: float


class LLMClient(Protocol):
    async def chat(self, model: str, messages: list[dict], *, options: dict,
                   think: bool | None = None, format: dict | None = None,
                   keep_alive: str | int | None = None,
                   timeout: float | None = None) -> ChatResult: ...

    async def embed(self, model: str, inputs: list[str], *, dimensions: int = 512,
                    timeout: float | None = None) -> list[list[float]]: ...

    async def list_models(self) -> set[str]: ...


def _readable(sock, timeout: float) -> bool:
    """Whether `sock` has data or EOF within `timeout` seconds. poll() where the platform has
    it: select() cannot watch a descriptor numbered 1024 or more on Linux."""
    if hasattr(select, "poll"):
        p = select.poll()
        p.register(sock, select.POLLIN | select.POLLPRI | select.POLLERR | select.POLLHUP)
        return bool(p.poll(int(timeout * 1000)))
    ready, _, _ = select.select([sock], [], [], timeout)
    return bool(ready)


class _Attempt:
    """One HTTP exchange running in a worker thread.

    abort() only sets a flag (and shuts the socket as a courtesy): the worker
    polls the flag between short select() slices, because on Windows a
    shutdown() from another thread does not wake a blocked recv().
    """

    def __init__(self):
        self.conn: http.client.HTTPConnection | None = None
        self.aborted = False
        self._lock = threading.Lock()

    def abort(self) -> None:
        with self._lock:
            self.aborted = True
            conn = self.conn
            sock = getattr(conn, "sock", None) if conn is not None else None
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass


class OllamaClient:
    # Extra seconds over the per-call timeout before the whole call is given up on.
    _CAP_SLACK = 5.0
    _SLICE = 0.25
    # Once the reply has begun, the longest a read may wait: a reply that stalls half way
    # holds a cancel or close (which wait for this thread) this long, not the call's timeout.
    READ_STALL_S = 5.0

    def __init__(self, base_url: str, *, timeout: float = 60.0):
        parts = urllib.parse.urlsplit(base_url)
        self._https = parts.scheme == "https"
        self._host = parts.hostname or "localhost"
        self._port = parts.port or (443 if self._https else 80)
        self._prefix = parts.path.rstrip("/")
        self._timeout = timeout
        self._warned_truncate = False

    def _request(self, path: str, body: dict | None, timeout: float, attempt: _Attempt) -> dict:
        data = None if body is None else json.dumps(body).encode("utf-8")
        cls = http.client.HTTPSConnection if self._https else http.client.HTTPConnection
        conn = cls(self._host, self._port, timeout=min(timeout, 5.0))
        resp = None
        deadline = time.monotonic() + timeout
        with attempt._lock:
            attempt.conn = conn
        try:
            conn.connect()
            with attempt._lock:
                if attempt.aborted:
                    raise LLMError(f"{path}: aborted")
                conn.sock.settimeout(timeout)
            conn.request("GET" if data is None else "POST", self._prefix + path, body=data,
                         headers={"Content-Type": "application/json"} if data is not None else {})
            # With stream:false the server sends nothing until generation ends, so
            # nearly all the time is spent here. Wait in short slices that notice an
            # abort or the deadline; only read once the first byte (or EOF) is there.
            while True:
                if attempt.aborted:
                    raise LLMError(f"{path}: aborted")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise LLMError(f"{path}: timed out")
                if _readable(conn.sock, min(self._SLICE, remaining)):
                    break
            conn.sock.settimeout(min(timeout, self.READ_STALL_S))
            resp = conn.getresponse()
            raw = resp.read()
            status = resp.status
        except (http.client.HTTPException, OSError, ValueError) as e:
            raise LLMError(f"{path}: {'aborted' if attempt.aborted else e}") from e
        finally:
            with attempt._lock:
                attempt.conn = None
            for c in (resp, conn):
                try:
                    if c is not None:
                        c.close()
                except Exception:
                    pass
        if attempt.aborted:
            raise LLMError(f"{path}: aborted")
        if status != 200:
            raise LLMError(f"{path}: HTTP {status} {raw[:200].decode('utf-8', 'replace')}".strip())
        try:
            out = json.loads(raw)
        except ValueError as e:
            raise LLMError(f"{path}: reply is not JSON") from e
        if not isinstance(out, dict):
            raise LLMError(f"{path}: reply is not an object")
        return out

    async def _call(self, path: str, body: dict | None, timeout: float | None) -> dict:
        """Run the exchange in a thread without blocking the loop.

        On cancellation or on the total-time cap the attempt is flagged aborted;
        the worker notices within one select slice (0.25 s), closes its socket
        (which makes Ollama drop the generation) and exits. The call returns only
        after the worker has exited, so a scheduler slot held by this call is
        never freed while its HTTP request is still alive.
        """
        timeout = self._timeout if timeout is None else timeout
        attempt = _Attempt()
        work = asyncio.ensure_future(
            asyncio.to_thread(self._request, path, body, timeout, attempt))
        try:
            return await asyncio.wait_for(asyncio.shield(work), timeout + self._CAP_SLACK)
        except asyncio.TimeoutError:
            attempt.abort()
            if await self._drain(work):
                raise asyncio.CancelledError() from None
            raise LLMError(f"{path}: timed out") from None
        except asyncio.CancelledError:
            attempt.abort()
            await self._drain(work)
            raise

    @staticmethod
    async def _drain(work: asyncio.Future) -> bool:
        """Wait for the worker to exit. Returns True if a cancel arrived meanwhile."""
        cancelled = False
        while not work.done():
            try:
                await asyncio.wait({work})
            except asyncio.CancelledError:
                cancelled = True
        if not work.cancelled():
            work.exception()  # mark retrieved
        return cancelled

    async def chat(self, model, messages, *, options, think=None, format=None,
                   keep_alive=None, timeout=None):
        body: dict = {"model": model, "messages": messages, "stream": False,
                      "options": dict(options)}
        if think is not None:
            body["think"] = think
        if format is not None:
            body["format"] = format
        if keep_alive is not None:
            body["keep_alive"] = keep_alive
        t0 = time.monotonic()
        out = await self._call("/api/chat", body, timeout)
        seconds = time.monotonic() - t0
        msg = out.get("message")
        if not isinstance(msg, dict) or not isinstance(msg.get("content", ""), str):
            raise LLMError("/api/chat: no message in reply")
        content = msg.get("content", "")
        thinking = msg.get("thinking") or ""
        tags = _THINK_RE.findall(content)
        if tags:
            content = _THINK_RE.sub("", content)
            if not thinking:
                thinking = "\n".join(t.strip() for t in tags)
        # A think block cut off by num_predict must never be spoken.
        content = _OPEN_THINK_RE.sub("", content).strip()
        if not content and thinking:
            log.warning("chat: empty content but the model thought (%s); is think:false honoured?", model)
        count = out.get("eval_count")
        return ChatResult(content, thinking, count if isinstance(count, int) else 0, seconds)

    async def embed(self, model, inputs, *, dimensions=512, timeout=None):
        out = await self._call("/api/embed", {
            "model": model, "input": inputs, "dimensions": dimensions, "keep_alive": -1,
            "options": {"num_ctx": 2048}}, timeout)
        vecs = out.get("embeddings")
        if not isinstance(vecs, list) or len(vecs) != len(inputs):
            raise LLMError("/api/embed: wrong number of vectors")
        result = []
        for v in vecs:
            if not isinstance(v, list) or len(v) < dimensions:
                raise LLMError(f"/api/embed: vector shorter than {dimensions}")
            try:
                v = [float(x) for x in v]
            except (TypeError, ValueError, OverflowError) as e:
                raise LLMError("/api/embed: non-numeric vector element") from e
            if not all(math.isfinite(x) for x in v):
                raise LLMError("/api/embed: non-finite vector element")
            if len(v) > dimensions:
                if not self._warned_truncate:
                    self._warned_truncate = True
                    log.warning("embed: server returned %d dims, truncating to %d", len(v), dimensions)
                v = v[:dimensions]
                norm = math.sqrt(sum(x * x for x in v))
                if norm == 0 or not math.isfinite(norm):
                    raise LLMError("/api/embed: bad vector after truncation")
                v = [x / norm for x in v]
            result.append(v)
        return result

    async def list_models(self) -> set[str]:
        out = await self._call("/api/tags", None, None)
        models = out.get("models")
        if not isinstance(models, list):
            raise LLMError("/api/tags: no models list")
        return {m["name"] for m in models if isinstance(m, dict) and isinstance(m.get("name"), str)}
