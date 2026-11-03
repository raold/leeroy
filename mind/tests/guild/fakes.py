"""In-process guild ports: no sockets, models, game processes, or live databases."""

import dataclasses
import json
from collections import defaultdict, deque
from pathlib import Path
from types import SimpleNamespace

from leeroy_mind.guild.protocol import Job, register
from leeroy_mind.human import HumanIdentity
from leeroy_mind.protocol import Ack, Member, encode_command
from leeroy_mind.storage.db import open_db
from leeroy_mind.storage.rows import PersonaStateRow, Relationship
from leeroy_mind.talk.turns import TurnKind


def member(guid, name, cls, spec_tab, level=80, online=True, map=1, zone=1637):
    return Member(guid=guid, name=name, race=2, class_=cls, gender=1, level=level,
                  rank=3, online=online, map=map, zone=zone, spec_tab=spec_tab)


def persona(guid, name, guild_role="raider", officer_title="", raid_role="melee", wants=(),
             main_spec="", skill="solid", thinks_as="social", conscientiousness=5,
             gender="female"):
    return SimpleNamespace(
        guid=guid, character_name=name,
        identity=SimpleNamespace(player_gender=gender),
        big_five=SimpleNamespace(conscientiousness=SimpleNamespace(score=conscientiousness)),
        habits=(),
        play=SimpleNamespace(guild_role=guild_role, officer_title=officer_title,
                             raid_role=raid_role, main_spec=main_spec, skill=skill),
        game=SimpleNamespace(wants=tuple(wants), thinks_as=thinks_as))


def job(kind, state, **fields):
    defaults = dict(ts=0, id="", kind=kind, state=state, reason="", stage="", guids=(),
                    skipped=(), kills=0, wipes=0, instance_id=0, guid=0, level=0, avg_ilvl=0)
    defaults.update(fields)
    return Job(**defaults)


class FakePorts:
    def __init__(self, tmp_path, clock):
        self.clock = clock
        self.conn = open_db(Path(tmp_path) / "mind.db")
        self.human_guid = 1
        self.ident = HumanIdentity(1, "Testmage", "Testy", "", True, "he")
        self.online = True
        self.members = {}
        self.personas = {}
        self.rels = {}
        self.away_set = set()
        self.states = {}
        self.sent = []
        self.proposed = []
        self.said = []
        self.memories = []
        self.lost = []
        self.leads = []
        self.gpu = []
        self.acks = defaultdict(deque)
        self.jobs = defaultdict(deque)
        self._command_types = {}
        register()

    def close(self):
        self.conn.close()

    def human(self):
        return self.ident

    def human_name(self):
        return self.human().called

    def human_online(self):
        return self.online

    def cast(self):
        return dict(self.members)

    def persona(self, guid):
        return self.personas.get(guid)

    def persona_state(self, guid):
        return self.states.get(guid, PersonaStateRow(guid))

    async def send(self, cmd):
        type_name = json.loads(encode_command(cmd, int(self.clock.now() * 1000)))["type"]
        self.sent.append(cmd)
        self._command_types[cmd.id] = type_name
        script = self.acks[type_name]
        ok, error = script.popleft() if script else (True, "")
        return Ack(ts=int(self.clock.now() * 1000), id=cmd.id, ok=ok, error=error)

    async def wait_job(self, cmd_id, timeout_s):
        type_name = self._command_types.get(cmd_id)
        script = self.jobs.get(type_name)
        if script:
            result = script.popleft()
            return dataclasses.replace(result, id=cmd_id) if not result.id else result
        return None

    def propose(self, guid, channel, instruction, *, kind=TurnKind.CALLOUT):
        self.proposed.append((guid, channel, instruction, kind))

    async def say(self, guid, channel, text):
        self.said.append((guid, channel, text))
        return True

    def set_lead(self, guid):
        self.leads.append(guid)

    async def remember(self, c):
        self.memories.append(c)

    async def loot_lost(self, guid, item_name):
        self.lost.append((guid, item_name))

    def relationship(self, a, b):
        return self.rels.get((a, b), Relationship(a, b))

    def away(self):
        return set(self.away_set)

    async def acquire_gpu(self, holder):
        self.gpu.append(f"acquire:{holder}")

    async def release_gpu(self, holder):
        self.gpu.append(f"release:{holder}")


class FakeArmory:
    """Synthetic item templates and current equipment, with settable progression/names."""
    def __init__(self, items, equipped, tier=16):
        self._items = dict(items)
        self._equipped = {guid: dict(slots) for guid, slots in equipped.items()}
        self.tier = tier
        self.names = {1: "Testmage"}
        self.calls = []

    def items(self, ids):
        ids = tuple(ids)
        self.calls.append(("items", ids))
        return {entry: self._items[entry] for entry in ids if entry in self._items}

    def equipped(self, guids):
        guids = tuple(guids)
        self.calls.append(("equipped", guids))
        return {guid: dict(self._equipped.get(guid, {})) for guid in guids}

    def human_tier(self, guid):
        self.calls.append(("human_tier", guid))
        return self.tier

    def name(self, guid):
        self.calls.append(("name", guid))
        return self.names.get(guid)
