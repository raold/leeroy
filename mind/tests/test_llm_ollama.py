import asyncio
from pathlib import Path

import pytest

from leeroy_mind.config import load_config
from leeroy_mind.llm.client import OllamaClient
from leeroy_mind.llm.presets import LIVE

CFG = load_config(Path("does-not-exist.toml"))


@pytest.mark.ollama
def test_live_embed_and_chat():
    async def go():
        c = OllamaClient(CFG.ollama.url, timeout=30.0)
        vecs = await c.embed(CFG.models.embedding, ["six wipes on the ice queen"])
        assert len(vecs) == 1 and len(vecs[0]) == 512
        r = await c.chat(
            CFG.models.live,
            [{"role": "user", "content": "Say hi to the guild in five words or fewer."}],
            options=dict(LIVE.options), think=LIVE.think)
        assert r.text.strip(), "empty text means the model thought despite think:false"
    asyncio.run(asyncio.wait_for(go(), 60))
