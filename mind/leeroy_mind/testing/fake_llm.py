import asyncio
import copy
import hashlib
import json
import math
import re
from typing import Callable, Iterable

from leeroy_mind.llm.client import (EMBED_BATCH, EMBED_DIMENSIONS, BadRequest, ChatResult,
                                    LLMError, chat_body, embed_body, embed_inputs, encode_body)


def fake_embed(text: str, dim: int = 512) -> list[float]:
    v = [0.0] * dim
    for w in re.findall(r"\w+", text.lower()):
        v[int.from_bytes(hashlib.md5(w.encode()).digest()[:4], "big") % dim] += 1.0
    norm = math.sqrt(sum(x * x for x in v))
    if norm == 0:
        v[0] = 1.0
        return v
    return [x / norm for x in v]


def check_chat(model, messages, *, options, think=None, format=None, keep_alive=None,
               timeout=None) -> dict:
    """The body OllamaClient.chat would send for these arguments, as Ollama reads it; the
    client's BadRequest for one it refuses. For a test's own stand-in that answers by itself."""
    return json.loads(encode_body(chat_body(model, messages, options=options, think=think,
                                            format=format, keep_alive=keep_alive)))


def check_embed(model, inputs, *, dimensions=EMBED_DIMENSIONS, timeout=None) -> list[str]:
    """check_chat for OllamaClient.embed: each request's body checked, the inputs returned."""
    inputs = embed_inputs(inputs)
    for at in range(0, len(inputs), EMBED_BATCH):
        encode_body(embed_body(model, inputs[at:at + EMBED_BATCH], dimensions=dimensions))
    return inputs


def script_from(texts: Iterable[str]) -> Callable[[str, list[dict]], str]:
    it = iter(texts)

    def script(model: str, messages: list[dict]) -> str:
        try:
            return next(it)
        except StopIteration:
            raise AssertionError("FakeLLM script exhausted") from None
    return script


class FakeLLM:
    """Stands in for OllamaClient. Each request is built and encoded by the client's own
    chat_body/embed_body and encode_body, so one Ollama would refuse raises the client's
    BadRequest here too (kept in `refused`, never in `calls`)."""

    def __init__(self, script: Callable[[str, list[dict]], str] | None = None, *,
                 models=frozenset(), gate: asyncio.Event | None = None):
        self._script = script
        self._models = set(models)
        self._gate = gate
        self.calls: list[dict] = []
        self.refused: list[BadRequest] = []
        self.inflight = 0
        self.inflight_peak = 0
        self._fail = 0
        self.live_borrow = None

    def fail_next(self, n: int = 1) -> None:
        self._fail += n

    def set_models(self, models) -> None:
        """What list_models reports from now on: a model pulled, or one gone."""
        self._models = set(models)

    def _sent(self, check, *args, **kw):
        try:
            return check(*args, **kw)
        except BadRequest as e:
            self.refused.append(e)
            raise

    async def _enter(self) -> None:
        self.inflight += 1
        self.inflight_peak = max(self.inflight_peak, self.inflight)
        if self._gate is not None:
            await self._gate.wait()
        if self._fail > 0:
            self._fail -= 1
            raise LLMError("fake failure")

    async def chat(self, model, messages, *, options, think=None, format=None, keep_alive=None,
                   timeout=None):
        if self.live_borrow is not None:
            return await self.live_borrow.run(lambda: self._chat(model,messages,options=options,
                think=think,format=format,keep_alive=keep_alive,timeout=timeout))
        return await self._chat(model,messages,options=options,think=think,format=format,
                                keep_alive=keep_alive,timeout=timeout)

    async def _chat(self, model, messages, *, options, think=None, format=None, keep_alive=None,
                    timeout=None):
        body = self._sent(check_chat, model, messages, options=options, think=think,
                          format=format, keep_alive=keep_alive)
        self.calls.append({"model": model, "messages": copy.deepcopy(messages),
                           "options": body["options"], "think": think,
                           "format": copy.deepcopy(format), "keep_alive": keep_alive})
        try:
            await self._enter()
            text = self._script(model, messages) if self._script else "(nothing)"
            return ChatResult(text.strip(), "", len(text.split()), 0.0)
        finally:
            self.inflight -= 1

    async def embed(self, model, inputs, *, dimensions=512, timeout=None):
        inputs = self._sent(check_embed, model, inputs, dimensions=dimensions)
        self.calls.append({"model": model, "input": inputs, "dimensions": dimensions})
        try:
            await self._enter()
            return [fake_embed(t, dimensions) for t in inputs]
        finally:
            self.inflight -= 1

    async def list_models(self) -> set[str]:
        return set(self._models)
