import asyncio
import copy
import hashlib
import json
import math
import re
from typing import Callable, Iterable

from leeroy_mind.llm.client import ChatResult, LLMError


def fake_embed(text: str, dim: int = 512) -> list[float]:
    v = [0.0] * dim
    for w in re.findall(r"\w+", text.lower()):
        v[int.from_bytes(hashlib.md5(w.encode()).digest()[:4], "big") % dim] += 1.0
    norm = math.sqrt(sum(x * x for x in v))
    if norm == 0:
        v[0] = 1.0
        return v
    return [x / norm for x in v]


def script_from(texts: Iterable[str]) -> Callable[[str, list[dict]], str]:
    it = iter(texts)

    def script(model: str, messages: list[dict]) -> str:
        try:
            return next(it)
        except StopIteration:
            raise AssertionError("FakeLLM script exhausted") from None
    return script


class FakeLLM:
    def __init__(self, script: Callable[[str, list[dict]], str] | None = None, *,
                 models=frozenset(), gate: asyncio.Event | None = None):
        self._script = script
        self._models = set(models)
        self._gate = gate
        self.calls: list[dict] = []
        self.inflight = 0
        self.inflight_peak = 0
        self._fail = 0

    def fail_next(self, n: int = 1) -> None:
        self._fail += n

    def set_models(self, models) -> None:
        """What list_models reports from now on: a model pulled, or one gone."""
        self._models = set(models)

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
        self.calls.append({"model": model, "messages": copy.deepcopy(messages),
                           "options": json.loads(json.dumps(dict(options))), "think": think,
                           "format": copy.deepcopy(format), "keep_alive": keep_alive})
        try:
            await self._enter()
            text = self._script(model, messages) if self._script else "(nothing)"
            return ChatResult(text.strip(), "", len(text.split()), 0.0)
        finally:
            self.inflight -= 1

    async def embed(self, model, inputs, *, dimensions=512, timeout=None):
        self.calls.append({"model": model, "input": list(inputs), "dimensions": dimensions})
        try:
            await self._enter()
            return [fake_embed(t, dimensions) for t in inputs]
        finally:
            self.inflight -= 1

    async def list_models(self) -> set[str]:
        return set(self._models)
