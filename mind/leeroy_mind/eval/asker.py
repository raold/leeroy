"""Read the real talk prompt on an isolated database copy; never send a bridge command."""
import asyncio
import random
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from leeroy_mind.human import HumanIdentity
from leeroy_mind.llm.presets import LIVE
from leeroy_mind.llm.scheduler import LLMScheduler
from leeroy_mind.memory.recall import Recall
from leeroy_mind.memory.writer import Embedder
from leeroy_mind.personas.io import PersonaCache
from leeroy_mind.protocol import Cast, Chat, Hello
from leeroy_mind.storage.db import open_db, snapshot
from leeroy_mind.storage.store import Store
from leeroy_mind.talk.engine import TalkEngine
from leeroy_mind.talk.prompt import LIVE_BUDGET
from leeroy_mind.talk.raid import RaidState
from leeroy_mind.talk.turns import Turn, TurnKind
from leeroy_mind.talk.world import WorldView
from leeroy_mind.text import estimate_tokens
from leeroy_mind.wow import CAST_MIN_LEVEL

ASK_TIMEOUT_S = 120.0  # includes recall embedding and the live-model answer
QUESTION_MAX_CHARS = 2000


@dataclass
class AskResult:
    text: str
    memory_ids: list[int]


class LiveAsker(Protocol):
    ident: HumanIdentity

    async def ask(self, guid: int, question: str) -> AskResult: ...


async def _never_send(_command):
    raise RuntimeError("scratch evaluation cannot send bridge commands")


class ScratchAsker:
    """Owns its copied Store and scheduler, never the caller's database or model client.

    Questions are serialized so one quiz cannot contaminate another question's composition.
    close() is immediate when idle. During a request it cancels only owned work and schedules
    cleanup; await aclose() to join that cleanup before leaving an async caller's finally block.
    """

    def __init__(self, *, cfg, ident, clock, llm, store, path, engine, scheduler):
        self.cfg, self.ident, self.clock, self.llm = cfg, ident, clock, llm
        self.store, self.path, self.engine, self.scheduler = store, path, engine, scheduler
        self._lock = asyncio.Lock()
        self._requests = set()
        self._closing = False
        self._closed = False
        self._cleanup = None

    @classmethod
    async def open(cls, *, cfg, data_dir, store, llm, ident, clock, label):
        if type(ident.guid) is not int or ident.guid <= 0:
            raise ValueError("scratch evaluation requires a human identity")
        data_dir = Path(data_dir)
        path = snapshot(store.conn, data_dir / "scratch", label, now=clock.now())
        copied = Store(open_db(path), clock=clock)
        try:
            roster = copied.cast()
            if ident.guid not in roster:
                raise ValueError("scratch evaluation human is absent from the copied roster")
            # The caller's identity is authoritative, including when a test supplies a
            # different config. These identity writes happen only in the new copy.
            cfg = replace(cfg, human=replace(cfg.human, nickname=ident.nickname,
                real_name=ident.real_name, pronouns=ident.pronouns))
            with copied.transaction():
                copied.set_meta("human_guid", str(ident.guid))
                copied.set_meta("human_name", ident.character_name)
            personas = PersonaCache(copied)
            world = WorldView(is_officer=lambda guid: bool(
                (p := personas.get(guid)) and p.play.guild_role == "officer"))
            now = clock.now()
            world.apply(Hello(ts=0, server="evaluation", version="1", guild_id=0,
                              human_guid=ident.guid), now)
            world.apply(Cast(ts=0, members=tuple(replace(m, online=True) for m in roster.values())), now)
            scheduler = LLMScheduler(llm, clock=clock)
            recall = Recall(copied, Embedder(scheduler, cfg.models.embedding), clock,
                            tau_days=cfg.memory.depth_days)
            engine = TalkEngine(store=copied, world=world,
                raid=RaidState(world=world, personas=personas.get, clock=clock),
                recall=recall, scheduler=scheduler, personas=personas, cfg=cfg,
                data_dir=data_dir, clock=clock, rng=random.Random(label), send=_never_send,
                human_guid=ident.guid)
            return cls(cfg=cfg, ident=ident, clock=clock, llm=llm, store=copied,
                       path=path, engine=engine, scheduler=scheduler)
        except BaseException:
            copied.close()
            raise

    async def ask(self, guid, question):
        if self._closing:
            raise RuntimeError("scratch asker is closed")
        if not isinstance(question, str) or not question.strip() or len(question) > QUESTION_MAX_CHARS:
            raise ValueError("invalid scratch question")
        member = self.store.cast().get(guid)
        if (type(guid) is not int or guid == self.ident.guid or member is None
                or member.level < CAST_MIN_LEVEL or self.engine.personas.get(guid) is None):
            raise ValueError("scratch question requires an eligible cast member")
        task = asyncio.create_task(self._ask(guid, question), name="scratch-eval-question")
        self._requests.add(task)
        try:
            return await task
        finally:
            self._requests.discard(task)

    async def _ask(self, guid, question):
        async with self._lock:
            if self._closing:
                raise RuntimeError("scratch asker is closed")
            async with asyncio.timeout(ASK_TIMEOUT_S):
                line = Chat(ts=int(self.clock.now() * 1000), channel="whisper",
                    from_guid=self.ident.guid, from_name=self.ident.character_name, to_guid=guid,
                    text=question, map=0, zone=0)
                line_id = self.store.insert_chat_line(line, session_id=None, origin="human")
                self.engine.world.add_line(self.store.chat_lines([line_id])[0], self.clock.now())
                messages, ids = await self.engine.compose(Turn(kind=TurnKind.REPLY_HUMAN,
                    speaker=guid, channel="whisper", to_guid=self.ident.guid,
                    addressee=self.ident.guid, trigger_line=line_id))
                if self._closing:
                    raise asyncio.CancelledError
                if sum(estimate_tokens(m["content"]) for m in messages) > LIVE_BUDGET:
                    raise ValueError("scratch prompt exceeds the live budget")
                answer = await self.llm.chat(self.cfg.models.live, messages,
                                             options=dict(LIVE.options), think=False)
                if self._closing:
                    raise asyncio.CancelledError
                if not isinstance(answer.text, str) or not answer.text.strip():
                    raise ValueError("scratch question returned no answer")
                return AskResult(answer.text, list(ids))

    async def _finish_close(self, requests):
        try:
            if requests:
                await asyncio.gather(*requests, return_exceptions=True)
            await self.scheduler.close()
        finally:
            if not self._closed:
                self.store.close()
                self._closed = True

    def close(self):
        if self._closing:
            return
        self._closing = True
        requests = tuple(task for task in self._requests if not task.done())
        for task in requests:
            task.cancel()
        if not requests:
            self.store.close()
            self._closed = True
        # A completed ask's scheduler callback may still be pending. Join it as well,
        # without touching the caller-owned client. No task is needed outside a loop.
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._cleanup = loop.create_task(self._finish_close(requests), name="scratch-eval-close")

    async def aclose(self):
        self.close()
        if self._cleanup is not None:
            await asyncio.shield(self._cleanup)
