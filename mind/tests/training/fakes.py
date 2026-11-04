"""In-process training ports. No bridge sockets, models or game data are opened."""

import dataclasses
import json
from collections import defaultdict, deque
from pathlib import Path

from leeroy_mind.guild import protocol as guild
from leeroy_mind.human import HumanIdentity
from leeroy_mind.protocol import Ack, encode_command
from leeroy_mind.storage.db import open_db
from leeroy_mind.storage.rows import PersonaStateRow
from leeroy_mind.talk.turns import TurnKind
from leeroy_mind.training.config import TrainingConfig
from leeroy_mind.training.ports import EventWaiter
from leeroy_mind.training.protocol import register


class FakeTrainingPorts:
    def __init__(self, tmp_path, clock):
        self.clock = clock
        self.conn = open_db(Path(tmp_path) / "mind.db")
        self.cfg = TrainingConfig()
        self.human_guid = 1
        self.ident = HumanIdentity(1, "Testmage", "Testy", "", True, "he")
        self.online = False
        self.members, self.personas, self.states = {}, {}, {}
        self.drunk_set, self.skips, self.comms, self.away_set = set(), set(), set(), set()
        self.mentors, self.holders, self.affinities = {}, {}, {}
        self.allowed, self.lead = None, None
        self.raids, self.mechanics = [], {}
        self.sent, self.said, self.proposed, self.memories = [], [], [], []
        self.claims, self.released = [], []
        self.acks, self.jobs, self.events = defaultdict(deque), defaultdict(deque), defaultdict(deque)
        self._commands = {}
        guild.register()
        register()

    def close(self):
        self.conn.close()

    def human(self):
        return self.ident

    def human_online(self):
        return self.online

    def cast(self):
        return dict(self.members)

    def persona(self, guid):
        return self.personas.get(guid)

    def persona_state(self, guid):
        return self.states.get(guid, PersonaStateRow(guid))

    def drunk(self, guid):
        return guid in self.drunk_set

    def flask_skips(self):
        return set(self.skips)

    def mentor_of(self, guid):
        return self.mentors.get(guid)

    def affinity(self, a, b):
        return self.affinities.get((a, b), 0)

    def on_comms(self, guid):
        return guid in self.comms

    def away(self):
        return set(self.away_set)

    async def send(self, cmd):
        name = json.loads(encode_command(cmd, int(self.clock.now() * 1000)))["type"]
        self.sent.append(cmd)
        self._commands[cmd.id] = (name, cmd)
        ok, error = self.acks[name].popleft() if self.acks[name] else (True, "")
        return Ack(int(self.clock.now() * 1000), cmd.id, ok, error)

    async def wait_job(self, cmd_id, timeout_s):
        if cmd_id not in self._commands:
            return None
        name, cmd = self._commands[cmd_id]
        if self.jobs[name]:
            result = self.jobs[name].popleft()
            return dataclasses.replace(result, id=cmd_id) if result is not None and not result.id else result
        return guild.Job(int(self.clock.now() * 1000), cmd_id,
                         "benchmark" if name == "start_benchmark" else name, "done", "", "",
                         tuple(getattr(cmd, "guids", ())), (), 0, 0, 0, 0, 0, 0)

    async def request(self, cmd, event_type, *, until, timeout_s):
        ack = await self.send(cmd)
        if not ack.ok:
            return []
        waiter = EventWaiter()
        queue = self.events[event_type]
        while queue:
            ev = queue.popleft()
            if hasattr(ev, "id") and not ev.id:
                ev = dataclasses.replace(ev, id=cmd.id)
            waiter.feed(ev)
        # Scripts represent everything arriving before the synthetic deadline.
        return await waiter.wait(cmd.id, event_type, until=until, timeout_s=0)

    def propose(self, guid, channel, instruction, *, kind=TurnKind.ASIDE):
        self.proposed.append((guid, channel, instruction, kind))

    async def say(self, guid, channel, text, *, kind=TurnKind.CALLOUT, to_guid=0):
        self.said.append((guid, channel, text, kind, to_guid))
        return True

    async def remember(self, c):
        self.memories.append(c)

    def standing_lead(self):
        return self.lead

    def boss_mechanics(self, boss, instance):
        return list(self.mechanics.get((boss, instance), ()))

    def claim(self, owner, guids, *, until):
        guids = tuple(guids)
        self.claims.append((owner, guids, until))
        granted = {g for g in guids if self.holders.get(g) in (None, owner)}
        self.holders.update(dict.fromkeys(granted, owner))
        return granted

    def release(self, owner, guids=None):
        guids = tuple(self.holders) if guids is None else tuple(guids)
        self.released.append((owner, guids))
        for guid in guids:
            if self.holders.get(guid) == owner:
                del self.holders[guid]

    def holder(self, guid):
        return self.holders.get(guid)

    def benchmark_allowed(self, map_id, boss_id, size, heroic):
        return None if self.allowed is None else self.allowed.get((map_id, boss_id, size, heroic), False)

    def raid_at(self, ms):
        candidates = [row for row in self.raids if row.started * 1000 <= ms
                      and (row.ended is None or ms < row.ended * 1000)]
        return max(candidates, key=lambda row: (row.started, row.raid_id), default=None)
