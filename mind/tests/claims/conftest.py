"""Invented people only; the shared Store and FakeClock never touch live data."""
from contextlib import asynccontextmanager

import pytest

from conftest import make_persona
from leeroy_mind.config import load_config
from leeroy_mind.human import HumanIdentity
from leeroy_mind.llm.scheduler import LLMScheduler
from leeroy_mind.memory.writer import Embedder, MemoryWriter
from leeroy_mind.nightly.gpu import GpuArbiter, NullAdmin
from leeroy_mind.nightly.llm import Reasoner
from leeroy_mind.nightly.runner import NightContext
from leeroy_mind.nightly.settings import load_settings
from leeroy_mind.nightly.store import run_begin
from leeroy_mind.personas.io import PersonaCache, save_persona
from leeroy_mind.protocol import Chat, Member
from leeroy_mind.testing.fake_llm import FakeLLM


def say(store, guid, text, *, origin='mind'):
    ev = Chat(ts=0, channel='guild', from_guid=guid, from_name=f'Member{guid}',
              to_guid=0, text=text, map=0, zone=0)
    return store.insert_chat_line(ev, session_id=None, origin=origin)


def raw(**over):
    return dict(kind='opinion', subject='saurfang', text='loves the Saurfang fight',
                polarity=1, horizon_days=None) | over


@pytest.fixture
def context(store, clock, tmp_path):
    for guid, name in ((1, 'Ownerly'), (101, 'Ashlen'), (102, 'Brelva'), (103, 'Cendrel')):
        store.upsert_cast([Member(guid=guid, name=name, race=2, class_=1, gender=0, level=80,
                                  rank=0 if guid == 1 else 3, online=False,
                                  map=0, zone=0, spec_tab=0)], clock.now())
        if guid != 1:
            save_persona(store, make_persona(guid, name), source='seed', ts=clock.now(),
                         race='Orc', class_name='Warrior')
    store.set_meta('human_guid', '1')
    ident = HumanIdentity(1, 'Ownerly', '', 'Avery Example', True)
    cfg = load_config(tmp_path / 'mind.toml')
    settings = load_settings(cfg, tmp_path)

    @asynccontextmanager
    async def make(script=None, *, night='2026-10-29', **over):
        from dataclasses import replace
        llm = FakeLLM(script or (lambda *_: '{"claims": []}'))
        scheduler = LLMScheduler(llm, clock=clock)
        embedder = Embedder(scheduler, cfg.models.embedding)
        ctx = NightContext(store, night, (0, clock.now() + 86400), replace(settings, **over),
                           Reasoner(llm, cfg.models.reflection), embedder,
                           MemoryWriter(store, embedder, clock), GpuArbiter(NullAdmin(), settings),
                           clock, ident, PersonaCache(store))
        run_begin(store, night, *ctx.window, now=clock.now())
        try:
            yield ctx, llm
        finally:
            await scheduler.close()
    return make
