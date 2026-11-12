"""Real app/control/HTTP/SSE, fake external boundaries, and owned loopback ports only."""
import asyncio
import json
import os
import socket
import sqlite3
from dataclasses import asdict
from types import SimpleNamespace

from conftest import HUMAN
from console.rig import FakeMind, make_app
from console.test_console_dojo import build, episode, make_layout, run_dir
from leeroy_mind import endpoint
from leeroy_mind.app import GUILD_ID, MindApp
from leeroy_mind.clock import SystemClock
from leeroy_mind.config import load_config
from leeroy_mind.console.control import Controls
from leeroy_mind.console import opsview
from leeroy_mind.console.mindlink import MindLink
from leeroy_mind.console.netinfo import Conn, ESTABLISHED, LISTEN
from leeroy_mind.lifecycle import Outcome
from leeroy_mind.testing.fake_bridge import FakeBridge
from leeroy_mind.testing.fake_llm import FakeLLM
from nightly.conftest import FakeAdmin
from selfies.fakes import fake_command, request, until
from leeroy_mind.selfies.worker import SelfieWorker

from .seed import members, seed


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class RehearsalOps(opsview.OpsView):
    """Read the scratch database/files; machine probes remain permanently synthetic."""
    def _source(self, name, deadline, runtime):
        if name == "health":
            return opsview.panel(checks=[
                {"name": "servers", "status": "ok", "text": "servers: ok (rehearsal)"},
                {"name": "mind", "status": "note", "text": "mind: note (rehearsal)"}], fails=0)
        if name == "schedules":
            return opsview.panel(tasks=[])
        return super()._source(name, deadline, runtime)


class Bridge(FakeBridge):
    async def _command(self, writer, command):
        await super()._command(writer, command)
        if command.get("type") == "owner_say":
            await self._write(writer, {"type": "chat", "channel": command["channel"],
                "from_guid": HUMAN, "from_name": "Testmage", "to_guid": command.get("to_guid", 0),
                "text": command["text"], "map": 0, "zone": 1519, "via_bridge": False})


class RehearsalMind(FakeMind):
    """OS process boundary only: controls actually start/stop this fixture's MindApp."""
    def __init__(self, rehearsal):
        super().__init__(rehearsal.data)
        self.rehearsal = rehearsal

    def act(self, action):
        self.acts.append(action)
        asyncio.run_coroutine_threadsafe(self.rehearsal.control(action),
                                         self.rehearsal.loop).result(timeout=30)
        return Outcome(0, f"rehearsal mind {action} done",
                       "running" if self._held else "stopped", os.getpid() if self._held else None)


class Rehearsal:
    def __init__(self, root, monkeypatch):
        self.root = root
        self.data = root / "mind-data"
        self.data.mkdir()
        self.monkeypatch = monkeypatch
        self.loop = asyncio.get_running_loop()
        self.app = self.task = self.console = None
        self.handle = RehearsalMind(self)
        self.ports = set()
        self.worker = None

    async def start(self, state="running", *, photos=False):
        cast = []
        for member in members():
            row = asdict(member)
            row["class"] = row.pop("class_")
            cast.append(row)
        self.bridge = Bridge([{"type": "hello", "server": "mod-leeroy", "version": "0.1.0",
                              "guild_id": GUILD_ID, "human_guid": HUMAN}], cast=cast, human_guid=HUMAN)
        port = await self.bridge.start()
        self.ports.add(port)
        (self.data / "mind.toml").write_text(
            f"[bridge]\nport = {port}\n[talk]\nchattiness = 0\n"
            "[selfies]\nenabled = true\n[realm]\nenabled = true\n"
            "[guild]\noffline_raids_per_day = 0\n"
            f'worldserver_conf = "{(self.root / "absent.conf").as_posix()}"\n'
            f'[photos]\nenabled = {str(photos).lower()}\n', encoding="utf-8")
        seed(self.data)
        command, env = fake_command(self.root)
        self.worker = SelfieWorker(command, env=env)
        await self.worker.start()
        rendered = await self.worker.render(request(self.root))
        assert rendered is not None and (self.root / "c0.jpg").exists()
        from leeroy_mind.selfies import stage
        self.monkeypatch.setattr(stage, "worker_command", lambda cfg: command)
        self.monkeypatch.setattr(stage, "worker_env", lambda cfg: env)
        await self.start_mind()
        if state != "running":
            await self.stop_mind()
        if state == "older":
            # Stop every writer before constructing a pre-feature SQLite database.
            # Console starts only afterwards, so no reader races the replacement.
            (self.data / "mind.db").unlink()
            sqlite3.connect(self.data / "mind.db").close()
            (self.data / "library.db").unlink()
        layout = None
        if state != "older":
            layout, dojo_data, _ = make_layout(self.root, ports={"world": free_port()})
            build(dojo_data, "20261105_100000")
            run_dir(dojo_data, "20261105_181500", "g1", {
                "gate": "G1", "ok": True, "episodes": [episode("rehearsal-episode")]})
        ui_port = free_port()
        self.link = MindLink(self.data, tcp=self.tcp)
        self.console = make_app(self.data, mind=self.handle,
            controls=Controls(enabled=True, mind=self.handle, problems={"voice": "No rehearsal voice service"}),
            link=self.link, tcp=self.tcp, voice_ui_port=ui_port, bridge_port=port,
            dojo_layout=layout)
        self.console._ops = RehearsalOps(self.root, self.data)
        await self.console.start()
        self.ports.add(self.console.s.port)
        self.url = f"http://127.0.0.1:{self.console.s.port}"

    def tcp(self):
        key = self.data / endpoint.KEY_FILE
        if not key.exists():
            return []
        port = json.loads(key.read_text())["port"]
        rows = [Conn("127.0.0.1", port, "0.0.0.0", 0, LISTEN, os.getpid())]
        if self.bridge._client is not None and self.bridge._client_ready.is_set():
            peer = self.bridge._client.get_extra_info("peername")
            rows.append(Conn("127.0.0.1", peer[1], "127.0.0.1", self.bridge.port,
                             ESTABLISHED, os.getpid()))
        return rows

    async def start_mind(self):
        if self.task is not None and not self.task.done():
            return
        cfg = load_config(self.data / "mind.toml")
        self.app = MindApp(cfg, self.data, llm=FakeLLM(), clock=SystemClock())
        self.app.arbiter.admin = FakeAdmin([])
        # The game database boundary is invented. Console and control still parse
        # and forward the real bank op; no MySQL settings or client are opened.
        self.app.guild.armory.available = True
        self.app.guild.armory.roster = lambda guild: {}
        self.app.guild.armory.friends = lambda human: set()
        self.app.guild.armory.equipped = lambda guids: {}
        self.app.guild.armory.kit = lambda human: {46376:2,40211:8,43015:10}
        self.app.guild.armory.bank = lambda guild: {
            "money": 1234567, "tabs": [{"tab": 0, "name": "Rehearsal supplies",
            "slots": [[0, 2589, 20]]}], "log": []}
        self.task = asyncio.create_task(self.app.run())
        await until(lambda: (self.data / endpoint.KEY_FILE).exists() and self.app.world.human_online, 15)
        self.ports.add(json.loads((self.data / endpoint.KEY_FILE).read_text())["port"])
        self.handle.found = [SimpleNamespace(pid=os.getpid())]
        self.handle._held = True
        await self.bridge.push({"type": "chat", "channel": "guild", "from_guid": 101,
            "from_name": "Arkon", "to_guid": 0, "text": "Rehearsal supplies are ready.",
            "map": 0, "zone": 1519})

    async def stop_mind(self):
        if self.app is not None:
            await asyncio.wait_for(self.app.stop(), 20)
        if self.task is not None:
            await asyncio.wait_for(self.task, 5)
        self.handle.found = []
        self.handle._held = False

    async def control(self, action):
        if action in ("stop", "restart"):
            await self.stop_mind()
        if action in ("start", "restart"):
            await self.start_mind()

    async def close(self):
        try:
            if self.console is not None:
                await self.console.close()
        finally:
            try:
                await self.stop_mind()
            finally:
                try:
                    if self.worker is not None:
                        await self.worker.stop()
                finally:
                    if hasattr(self, "bridge"):
                        await self.bridge.stop()
        assert not (self.data / endpoint.KEY_FILE).exists()
        for port in self.ports:
            with socket.socket() as sock:
                assert sock.connect_ex(("127.0.0.1", port)) != 0, f"leaked listener {port}"
