"""Keeps Ollama from evicting the live and embedding models while the voice service holds VRAM
beside them (Task 21), and lets them go when it does not.

A pin never loads or reloads anything: a model is touched only if /api/ps already lists it, and
with exactly the options the real calls use (the live chat's LIVE preset: num_ctx and think; the
embed call's dimensions and num_ctx), because Ollama reloads a runner whose options differ. Only
keep_alive changes: -1 to pin, "5m" (Ollama's default) to unpin."""
import asyncio
import json
import urllib.request

from leeroy_mind.llm.client import EMBED_DIMENSIONS, EMBED_NUM_CTX
from leeroy_mind.llm.presets import LIVE

TIMEOUT_S = 10.0  # the models are loaded already: a reply is quick, a hang is Ollama's
DEFAULT_KEEP_ALIVE = "5m"


def _listed(names: set[str], model: str) -> bool:
    return model in names or (":" not in model and model + ":latest" in names)


class OllamaPins:
    def __init__(self, url: str):
        self.url = url.rstrip("/")

    async def pin(self, live: str, embed: str) -> None:
        await self._keep(live, embed, -1)

    async def unpin(self, live: str, embed: str) -> None:
        await self._keep(live, embed, DEFAULT_KEEP_ALIVE)

    async def _keep(self, live: str, embed: str, keep_alive) -> None:
        """Raises OSError (urllib's errors are) when Ollama fails a call; does nothing when
        /api/ps cannot be read or does not list a model."""
        names = await asyncio.to_thread(self._loaded)
        if names is None:
            return
        if _listed(names, live):
            await asyncio.to_thread(self._post, "/api/chat", {
                "model": live, "messages": [], "stream": False, "think": LIVE.think,
                "options": dict(LIVE.options), "keep_alive": keep_alive})
        if _listed(names, embed):
            await asyncio.to_thread(self._post, "/api/embed", {
                "model": embed, "input": ["."], "dimensions": EMBED_DIMENSIONS,
                "keep_alive": keep_alive, "options": {"num_ctx": EMBED_NUM_CTX}})

    def _loaded(self) -> set[str] | None:
        try:
            with urllib.request.urlopen(self.url + "/api/ps", timeout=TIMEOUT_S) as reply:
                return {str(m["name"]) for m in json.loads(reply.read())["models"]}
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _post(self, path: str, body: dict) -> None:
        request = urllib.request.Request(
            self.url + path, json.dumps(body).encode(), {"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as reply:
            reply.read()
