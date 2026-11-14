"""An in-process app for the raid lead: a real store, world view and raid state, invented members,
a scripted bridge and guild ports, and the FakeLLM behind a real scheduler. No socket or model."""

import asyncio
from collections import deque
from dataclasses import replace
from types import SimpleNamespace

from conftest import make_persona
from leeroy_mind.guild.protocol import Job, JobSkip
from leeroy_mind.llm.scheduler import LLMScheduler
from leeroy_mind.personas.io import PersonaCache, save_persona
from leeroy_mind.protocol import Ack, Group, Hello, Member
from leeroy_mind.raidlead.service import RaidLeadService
from leeroy_mind.talk.raid import RaidState
from leeroy_mind.talk.world import WorldView
from leeroy_mind.testing.fake_llm import FakeLLM

HUMAN = 1
LIVE = "invented-live-model"
# guid: (name, race, class, spec tab, raid role, guild role)
CAST = {101: ("Arkon", 2, 1, 2, "tank", "officer"),        # protection warrior, orc
        102: ("Belra", 2, 7, 2, "healer", "raider"),        # restoration shaman, orc
        103: ("Coros", 10, 8, 0, "ranged", "officer"),      # arcane mage
        104: ("Dunna", 3, 4, 1, "melee", "raider"),         # combat rogue
        105: ("Ebbo", 2, 6, 0, "tank", "raider")}           # blood death knight


def seed(store):
    from leeroy_mind.wow import CLASSES, RACES
    members = [Member(guid=g, name=n, race=r, class_=c, gender=1, level=80, rank=3, online=True,
                      map=631, zone=4812, spec_tab=s) for g, (n, r, c, s, _, _) in CAST.items()]
    store.upsert_cast(members, 1000.0)
    for g, (n, r, c, _, role, guild_role) in CAST.items():
        persona = make_persona(g, n, raid_role=role, guild_role=guild_role)
        save_persona(store, persona, source="seed", ts=1000.0, race=RACES[r], class_name=CLASSES[c])
    return {m.guid: m for m in members}


class Source:
    def __init__(self):
        self.turns = []

    def add(self, turn, *, run=None):
        self.turns.append(turn)


class Ports:
    """Guild life's ports as the raid lead uses them: send/wait_job for bot_do, say."""

    def __init__(self, clock):
        self.clock = clock
        self.sent, self.said = [], []
        self.acks = deque()  # (ok, error) per send; default ok
        self.jobs = deque()  # Job, a callable(cmd) -> Job, or None (no terminal)
        self.waits = []

    def human_name(self):
        return "Rowan"

    async def send(self, cmd):
        self.sent.append(cmd)
        ok, error = self.acks.popleft() if self.acks else (True, "")
        return Ack(0, cmd.id, ok, error)

    async def wait_job(self, cmd_id, timeout_s):
        self.waits.append((cmd_id, timeout_s))
        if timeout_s == 0:
            return None
        cmd = next(c for c in self.sent if c.id == cmd_id)
        script = self.jobs.popleft() if self.jobs else (lambda c: done(c))
        result = script(cmd) if callable(script) else script
        return None if result is None else replace(result, id=cmd_id)

    async def say(self, guid, channel, text, *, kind=None, to_guid=0):
        self.said.append((guid, channel, text, kind))
        return True


class Bridge:
    def __init__(self):
        self.sent = []
        self.acks = deque()

    async def send(self, cmd, *, preserve_id=False):
        self.sent.append(cmd)
        ok, error = self.acks.popleft() if self.acks else (True, "")
        return Ack(0, cmd.id, ok, error)


def done(cmd, *, skip=()):
    return Job(ts=0, id=cmd.id, kind="bot_do", state="done", reason="", stage="",
               guids=tuple(g for g in cmd.guids if g not in dict(skip)),
               skipped=tuple(JobSkip(g, why) for g, why in skip if g in cmd.guids),
               kills=0, wipes=0, instance_id=0, guid=0, level=0, avg_ilvl=0)


class Arbiter:
    def __init__(self, loaded=(LIVE,)):
        self.paused = False
        self.loaded = list(loaded)
        self.admin = SimpleNamespace(loaded_models=self._loaded)

    async def _loaded(self):
        return list(self.loaded)


def make_app(store, clock, tmp_path, *, llm=None):
    cast = seed(store)
    world = WorldView(is_officer=lambda g: CAST.get(g, ("",) * 6)[5] == "officer")
    world.human_guid = HUMAN
    world.members = dict(cast)
    world.online = set(cast)
    world.human_online = True
    personas = PersonaCache(store)
    raid = RaidState(world=world, personas=personas.get, clock=clock)
    ports = Ports(clock)
    released, signals = [], []

    async def release_to_talk(ev, line_id):
        signals.append((line_id, False))
        released.append(line_id)

    def signal_his_line(ev, line_id, *, command):
        signals.append((line_id, command))
    llm = llm or FakeLLM(lambda model, messages: "{}")
    app = SimpleNamespace(
        store=store, clock=clock, data_dir=tmp_path, human_guid=HUMAN, world=world, raid=raid,
        personas=personas, stopping=False, is_backlog=lambda ev: False, bridge=Bridge(),
        guild=SimpleNamespace(ports=ports, source=Source(), offline=SimpleNamespace(active=False),
                              raid_night=SimpleNamespace(state=SimpleNamespace(value="idle"), run=None)),
        scheduler=LLMScheduler(llm, clock=clock), arbiter=Arbiter(),
        cfg=SimpleNamespace(models=SimpleNamespace(live=LIVE)), release_to_talk=release_to_talk,
        released=released, signal_his_line=signal_his_line, signals=signals, llm=llm)
    return app


async def ready(app, *, raid=True, members=tuple(CAST)):
    """The raid lead connected to a native that has it, with him leading his group."""
    service = RaidLeadService(app)
    await service.connection(True)
    await service.event(Hello(0, "mod-leeroy", "1", 21, HUMAN, ("item_links", "playstyle", "raidlead")))
    group = Group(0, HUMAN, raid, (HUMAN, *members))
    app.world.set_group(group)
    await service.event(group)
    await service._probe()  # native answers its switch: on
    return service


async def drain(service):
    """The worker's work, without its loop: one tick, the model's readings, then everything queued."""
    await service._tick()
    for held in list(service._pending):
        if held.task is not None:
            await asyncio.gather(held.task, return_exceptions=True)
    await service._settle_pending()
    while service._queue:
        generation, item = service._queue.popleft()
        await service._handle(generation, item)
    await asyncio.sleep(0)
