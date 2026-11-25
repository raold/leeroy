import asyncio
import base64
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
from dataclasses import dataclass, field
from typing import Protocol

from leeroy_mind.llm.diagnostics import ResponseDiagnostics, capture_response

log = logging.getLogger("leeroy_mind")

_THINK_RE = re.compile(r"<think>(.*?)</think>", re.DOTALL)
_OPEN_THINK_RE = re.compile(r"<think>.*\Z", re.DOTALL)


class LLMError(Exception):
    def __init__(self, *args, diagnostics: ResponseDiagnostics | None = None):
        super().__init__(*args)
        self.diagnostics = diagnostics


class GpuPaused(LLMError):
    """Intentional model unavailability; callers should quietly wait or drop their turn."""


class BadRequest(LLMError):
    """A request Ollama would refuse, or would quietly drop a part of: never sent."""


@dataclass(frozen=True)
class ChatResult:
    text: str
    thinking: str
    eval_count: int
    seconds: float
    diagnostics: ResponseDiagnostics | None = field(default=None, compare=False)


EMBED_DIMENSIONS = 512
EMBED_NUM_CTX = 2048
# Inputs per /api/embed request. Ollama 0.40.15's llama-server backend failed a request of
# several hundred inputs part way (HTTP 400, its runner refusing a connection) while every
# request of up to 160 passed; the library and the writer's backfill already send 32.
EMBED_BATCH = 32

# Ollama's /api/chat and /api/embed contract (api/types.go): a message's role and fields, and
# each option's type. A wrong type is an error; an unknown option or message field is dropped
# with no more than a server-side warning, which here is a refusal too.
ROLES = frozenset({"system", "user", "assistant", "tool"})
MESSAGE_KEYS = frozenset({"role", "content", "thinking", "images", "tool_calls", "tool_name"})
_INT_OPTIONS = frozenset({"num_keep", "seed", "num_predict", "top_k", "repeat_last_n", "num_ctx",
                          "num_batch", "num_gpu", "main_gpu", "num_thread"})
_FLOAT_OPTIONS = frozenset({"top_p", "min_p", "typical_p", "temperature", "repeat_penalty",
                            "presence_penalty", "frequency_penalty"})
_DURATION_RE = re.compile(r"[-+]?(0|((\d+(\.\d*)?|\.\d+)(ns|us|µs|μs|ms|s|m|h))+)")


def _bad(why: str):
    raise BadRequest(f"bad request: {why}")


def _number(v) -> bool:
    return type(v) in (int, float) and math.isfinite(v)


def _options(options) -> dict:
    try:
        out = dict(options)
    except (TypeError, ValueError):
        _bad("options is not a mapping")
    for key, v in out.items():
        if key in _INT_OPTIONS:
            ok = type(v) is int
        elif key in _FLOAT_OPTIONS:
            ok = _number(v)
        elif key == "use_mmap":
            ok = type(v) is bool
        elif key == "stop":
            ok = type(v) in (list, tuple) and all(type(s) is str for s in v)
        else:
            _bad(f"unknown option {key!r}")
        if not ok:
            _bad(f"option {key!r} has the wrong type")
    return out


def _keep_alive(v) -> None:
    if not (_number(v) or (type(v) is str and _DURATION_RE.fullmatch(v))):
        _bad("keep_alive is not seconds or a duration")


def encode_body(body: dict) -> bytes:
    """A request body as it goes on the wire: strict JSON (no NaN), UTF-8."""
    try:
        return json.dumps(body, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as e:
        raise BadRequest(f"bad request: not JSON ({type(e).__name__})") from None


def chat_body(model, messages, *, options, think=None, format=None, keep_alive=None) -> dict:
    """The /api/chat body, checked against Ollama's contract; BadRequest when it breaks it."""
    if type(model) is not str or not model:
        _bad("model is not a name")
    if type(messages) not in (list, tuple):
        _bad("messages is not a list of messages")
    for i, m in enumerate(messages):
        if not isinstance(m, dict) or not set(m) <= MESSAGE_KEYS:
            _bad(f"messages[{i}] is not a message object")
        if m.get("role") not in ROLES:
            _bad(f"messages[{i}].role is not one of {', '.join(sorted(ROLES))}")
        if type(m.get("content")) is not str:
            _bad(f"messages[{i}].content is not a string")
        if "thinking" in m and type(m["thinking"]) is not str:
            _bad(f"messages[{i}].thinking is not a string")
        images = m.get("images", [])
        if type(images) not in (list, tuple) or not all(type(x) is str for x in images):
            _bad(f"messages[{i}].images is not a list of base64 strings")
        for x in images:
            try:
                base64.b64decode(x, validate=True)
            except ValueError:
                _bad(f"messages[{i}].images holds one that is not base64")
    body: dict = {"model": model, "messages": messages, "stream": False,
                  "options": _options(options)}
    if think is not None:
        if type(think) is not bool:
            _bad("think is not true, false or left out")
        body["think"] = think
    if format is not None:
        if format != "json" and not isinstance(format, dict):
            _bad('format is not "json" or a JSON schema object')
        body["format"] = format
    if keep_alive is not None:
        _keep_alive(keep_alive)
        body["keep_alive"] = keep_alive
    encode_body(body)
    return body


def embed_inputs(inputs) -> list:
    """An embed call's texts as a list: a lone string is refused, never split into letters."""
    if isinstance(inputs, (str, bytes)):
        _bad("input is one string, not a list of them")
    return list(inputs)


def embed_body(model, inputs, *, dimensions=EMBED_DIMENSIONS) -> dict:
    """One /api/embed request's body (at most EMBED_BATCH inputs), checked as chat_body is."""
    if type(model) is not str or not model:
        _bad("model is not a name")
    if type(inputs) not in (list, tuple) or not all(type(s) is str for s in inputs):
        _bad("input is not a list of strings")
    if type(dimensions) is not int or dimensions <= 0:
        _bad("dimensions is not a positive integer")
    body = {"model": model, "input": list(inputs), "dimensions": dimensions, "keep_alive": -1,
            "options": {"num_ctx": EMBED_NUM_CTX}}
    encode_body(body)
    return body


class LLMClient(Protocol):
    async def chat(self, model: str, messages: list[dict], *, options: dict,
                   think: bool | None = None, format: dict | None = None,
                   keep_alive: str | int | None = None,
                   timeout: float | None = None) -> ChatResult: ...

    async def embed(self, model: str, inputs: list[str], *, dimensions: int = EMBED_DIMENSIONS,
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
        self.gpu_gate = None
        self.live_borrow = None

    def _request(self, path: str, body: dict | None, timeout: float, attempt: _Attempt) -> dict:
        data = None if body is None else encode_body(body)
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
            gated = self.gpu_gate if path in ('/api/chat', '/api/generate', '/api/embed') else None
            if gated:
                gated.check()
            # Admission closes without taking a socket/thread lock on the event loop.
            # A send already admitted here remains tracked until abort/drain finishes.
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
        async def admitted():
            if self.live_borrow is not None and path in ('/api/chat', '/api/generate'):
                return await self.live_borrow.run(lambda: self._exchange(path, body, timeout))
            return await self._exchange(path, body, timeout)
        if self.gpu_gate is not None and path in ('/api/chat', '/api/generate', '/api/embed'):
            return await self.gpu_gate.run(admitted)
        return await admitted()

    async def _exchange(self, path: str, body: dict | None, timeout: float | None) -> dict:
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
        body = chat_body(model, messages, options=options, think=think, format=format,
                         keep_alive=keep_alive)
        t0 = time.monotonic()
        out = await self._call("/api/chat", body, timeout)
        seconds = time.monotonic() - t0
        diagnostics = capture_response(out)
        msg = out.get("message")
        if not isinstance(msg, dict) or not isinstance(msg.get("content", ""), str):
            raise LLMError("/api/chat: no message in reply", diagnostics=diagnostics)
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
            # Counts only. Thinking counts against num_predict, so a thinking request can
            # spend all of it before the answer begins.
            why = ("is think:false sent and honoured?" if not think else
                   "the thinking used up num_predict" if diagnostics.done_reason == "length"
                   else "nothing came after the thinking")
            log.warning("chat: empty content but the model thought (%s; think=%s, done_reason=%s, "
                        "eval_count=%s, num_predict=%s); %s", model, think, diagnostics.done_reason,
                        diagnostics.eval_count, body["options"].get("num_predict"), why)
        count = out.get("eval_count")
        return ChatResult(content, thinking, count if isinstance(count, int) else 0, seconds, diagnostics)

    async def embed(self, model, inputs, *, dimensions=EMBED_DIMENSIONS, timeout=None):
        """One vector per input, in order, from requests of at most EMBED_BATCH inputs each.
        Any failed request fails the whole call; `timeout` applies to each request."""
        inputs = embed_inputs(inputs)
        result = []
        for at in range(0, len(inputs), EMBED_BATCH):
            result += await self._embed_batch(model, inputs[at:at + EMBED_BATCH],
                                              dimensions, timeout)
        return result

    async def _embed_batch(self, model, inputs, dimensions, timeout):
        out = await self._call("/api/embed", embed_body(model, inputs, dimensions=dimensions),
                               timeout)
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
