"""Feed the candidate mind's real playbooks over its real isolated native bridge.

No model, user data, chat handlers, or live endpoint are involved. The worker
retains the connection because native invalidates tables when it disconnects.
"""
import asyncio
from pathlib import Path
import sys
import threading
from types import SimpleNamespace


class MechanicConnection:
    def __init__(self, layout, *, guild, map_id):
        self.port = layout.ports["leeroy"]
        self.guild = guild
        self.map_id = map_id
        self.ready = False
        self.error = ""
        self.features = []
        self.rules = 0
        self.stop = threading.Event()
        # Use the same candidate source whose clean commit the build attests.
        self.mind = str(Path(layout.roots["source"]) / "mind")
        self.thread = threading.Thread(target=self._thread, daemon=True, name="dojo-mechanic-feed")

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.stop.set()
        self.thread.join(10)
        if self.thread.is_alive():
            self.ready = False
            self.error = "mechanic feed did not stop"

    def _thread(self):
        try:
            sys.path.insert(0, self.mind)
            asyncio.run(self._run())
        except Exception as error:
            self.ready = False
            self.error = type(error).__name__

    async def _run(self):
        from leeroy_mind.bridge import BridgeClient
        from leeroy_mind.protocol import Hello, register_command
        from leeroy_mind.raidplay.model import load
        from leeroy_mind.raidplay.respond import MechanicFeed, RaidMechanics

        register_command("raid_mechanics", RaidMechanics)

        async def connection(connected):
            if not connected:
                self.ready = False
            await feed.connection(connected)

        async def event(ev):
            if not isinstance(ev, Hello):
                return
            self.ready = False
            if ev.guild_id != self.guild:
                self.error = "bridge greeted a different guild"
                return
            self.features = sorted(ev.features)
            if "mechanics" not in ev.features or (self.map_id == 649 and "mechanics_cast_radius" not in ev.features):
                self.error = "required native mechanic capability is missing"
                return
            await feed.event(ev)
            key = (self.map_id, 0)
            if key not in feed.tables or not await feed.send(key):
                self.error = "native did not acknowledge the candidate mechanic table"
                return
            self.rules = len(feed.tables[key])
            self.ready = True

        client = BridgeClient("127.0.0.1", self.port, event, on_connection=connection)
        feed = MechanicFeed(SimpleNamespace(bridge=client, is_backlog=lambda ev: False), load())
        worker = asyncio.create_task(client.run())
        try:
            while not self.stop.is_set():
                await asyncio.sleep(0.1)
        finally:
            await client.close()
            await asyncio.gather(worker, return_exceptions=True)
