"""The conversation lab's test rig: a live-looking data dir with the invented test cast, a
NightContext on it with a scripted reflection FakeLLM, and an in-process sandbox child (the real
Sandbox and Api with a FakeLLM for the live model and a fast fake clock)."""
import asyncio
import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from conftest import HUMAN, TEST_CAST, seed_test_cast
from engine_rig import save_member
from leeroy_mind.clock import SystemClock
from leeroy_mind.config import load_config
from leeroy_mind.human import human_identity
from leeroy_mind.protocol import Member
from leeroy_mind.storage.db import checkpoint, open_db
from leeroy_mind.storage.store import Store
from leeroy_mind.testing.fake_clock import FakeClock
from leeroy_mind.testing.fake_llm import FakeLLM

LIVE_TOML = """\
[talk]
chattiness = 4
[human]
nickname = "Zan"
real_name = "Quillon"
pronouns = "he"
[voice]
enabled = false
[server]
enabled = true
[signals]
enabled = true
[lab]
enabled = true
minutes = 30
pairs = 4
judge = 2
variants = 3
"""
OWNER_OVERLAY = b"the owner's own wording, never read by the lab"
TITLE = re.compile(r'"title":\s*"([^"]+)"')
NIGHT = "2026-11-16"


def make_live(tmp_path: Path, toml: str = LIVE_TOML) -> Path:
    live = tmp_path / "live"
    live.mkdir()
    (live / "mind.toml").write_bytes(toml.encode())
    store = Store(open_db(live / "mind.db"), clock=SystemClock())
    try:
        seed_test_cast(store)
        for guid in TEST_CAST:
            save_member(store, guid, region="US", player_gender="female" if guid in (102, 104) else "male",
                        stance="drawn" if guid == 102 else "none")
        store.set_meta("human_guid", str(HUMAN))
        store.set_meta("human_name", "Testmage")
        store.upsert_cast([Member(guid=HUMAN, name="Testmage", race=10, class_=8, gender=0,
                                  level=80, rank=0, online=False, map=0, zone=1519, spec_tab=-1)],
                          1000.0)
        store.conn.commit()
        checkpoint(store.conn)
    finally:
        store.close()
    for name in ("personas", "prompts", "realm"):
        (live / name).mkdir()
    (live / "prompts" / "romance_explicit.txt").write_bytes(OWNER_OVERLAY)
    return live


def digest(root: Path) -> dict:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*"))
            if p.is_file() and not p.name.endswith(("-shm", "-wal"))}


def title_of(messages) -> str | None:
    text = messages[-1]["content"]
    found = TITLE.search(text.rsplit("matching this schema:", 1)[-1]) or TITLE.search(text)
    return found.group(1) if found else None


class StepClock:
    def __init__(self, t: float):
        self._t = t

    def now(self) -> float:
        return self._t

    def local(self) -> datetime:
        return datetime.fromtimestamp(self._t, timezone.utc)

    async def sleep(self, s: float) -> None:
        await asyncio.sleep(0)

    def advance(self, s: float) -> None:
        self._t += s


class FakeAdmin:
    def __init__(self, loaded):
        self.loaded = list(loaded)
        self.calls = []

    async def loaded_models(self):
        return list(self.loaded)

    async def unload(self, model):
        self.calls.append(("unload", model))
        if model in self.loaded:
            self.loaded.remove(model)

    async def preload(self, model, keep_alive, *, options=None):
        self.calls.append(("preload", model))
        self.loaded.append(model)


def night_ctx(live: Path, script, *, gate: asyncio.Event | None = None):
    """A NightContext on `live`'s mind.db (open for writing), its reflection model a FakeLLM
    answering `script(model, messages)`."""
    from leeroy_mind.llm.scheduler import LLMScheduler
    from leeroy_mind.memory.writer import Embedder, MemoryWriter
    from leeroy_mind.nightly.gpu import GpuArbiter
    from leeroy_mind.nightly.llm import Reasoner
    from leeroy_mind.nightly.runner import NightContext
    from leeroy_mind.nightly.settings import load_settings, night_bounds
    from leeroy_mind.personas.io import PersonaCache
    cfg = load_config(live / "mind.toml")
    settings = load_settings(cfg, live)
    clock = StepClock(datetime(2026, 11, 17, 4, tzinfo=timezone.utc).timestamp())
    store = Store(open_db(live / "mind.db"), clock=clock)
    llm = FakeLLM(script, models={settings.reflection_model, settings.live_model,
                                  settings.embedding_model}, gate=gate)
    embedder = Embedder(LLMScheduler(llm, clock=clock), settings.embedding_model)
    ident = human_identity(cfg, store, HUMAN)
    ctx = NightContext(store=store, night=NIGHT, window=night_bounds(NIGHT, timezone.utc),
                       settings=settings, reasoner=Reasoner(llm, settings.reflection_model),
                       embedder=embedder, writer=MemoryWriter(store, embedder, clock, human=lambda: ident),
                       arbiter=GpuArbiter(FakeAdmin([settings.embedding_model]), settings),
                       clock=clock, ident=ident, personas=PersonaCache(store))
    return ctx, llm


async def _tick(clock):
    while True:
        await clock.advance(1, 0.5)


class InProcessChild:
    """The lab's sandbox child in this process: the real Sandbox and Api on the lab's layout,
    the live model a FakeLLM answering `answer(model, messages)`, a fast fake clock."""
    started: list = []

    def __init__(self, live: Path, seed: int, answer, tmp: Path):
        from leeroy_mind.sandbox import prepare
        self.layout = prepare.lab_layout(live)
        self.seed, self.answer, self.tmp = seed, answer, tmp
        self.sandbox = self.api = self.ticker = None
        self.killed = False
        InProcessChild.started.append(self)

    async def start(self) -> str:
        from leeroy_mind.sandbox.api import Api
        from leeroy_mind.sandbox.runtime import Sandbox
        clock = FakeClock(datetime(2026, 11, 16, 20, tzinfo=ZoneInfo("America/New_York")))
        self.ticker = asyncio.create_task(_tick(clock))
        cfg = load_config(self.tmp / "defaults.toml")
        self.llm = FakeLLM(self.answer, models={cfg.models.live, cfg.models.embedding})
        self.sandbox = Sandbox(self.layout, make_llm=lambda c: self.llm, clock=clock, seed=self.seed,
                               ollama_conf=self.tmp / "no-ollama-chat.conf", fast=True)
        self.api = Api(self.sandbox)
        logger = logging.getLogger("leeroy_mind")
        self._level = logger.level
        logger.setLevel(logging.INFO)  # as the sandbox's CLI sets it: the lab reads its events
        await self.sandbox.start()
        await self.api.start()
        return self.api.url

    def kill(self) -> None:
        self.killed = True
        if self.ticker is not None:
            self.ticker.cancel()

    async def stop(self) -> None:
        logging.getLogger("leeroy_mind").setLevel(getattr(self, "_level", logging.NOTSET))
        if self.api is not None:
            await self.api.close()
        if self.sandbox is not None:
            await self.sandbox.close()
        if self.ticker is not None:
            self.ticker.cancel()


def variant_text(marker: str) -> str:
    from leeroy_mind.talk.pieces import builtin
    text = builtin("reply_human")
    assert "Respond to what" in text
    return text.replace("Respond to what", f"{marker} respond to what", 1)


def judge_reply(messages) -> str:
    letters = re.findall(r"^Speakers: (.+)\.$", messages[-1]["content"], re.MULTILINE)[0].split(", ")
    return json.dumps({"speakers": [{"speaker": x, "human": x == "A", "tells": []} for x in letters]})
