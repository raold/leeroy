"""Real mind/control/console, invented cast, fake bridge/model/admin, ephemeral ports only."""
import asyncio
import json
import os
import socket
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from conftest import HUMAN, TEST_CAST, seed_test_cast
from console.rig import FakeMind, http, make_app
from engine_rig import save_member
from leeroy_mind import endpoint
from leeroy_mind.app import GUILD_ID, MindApp
from leeroy_mind.config import load_config
from leeroy_mind.console.mindlink import MindLink
from leeroy_mind.console.netinfo import LISTEN, Conn
from leeroy_mind.discord import store as discord_store
from leeroy_mind.protocol import Member
from leeroy_mind.storage.db import open_db
from leeroy_mind.storage.store import Store
from leeroy_mind.testing.fake_bridge import FakeBridge, load_fixture
from leeroy_mind.testing.fake_clock import FakeClock
from leeroy_mind.testing.fake_llm import FakeLLM
from tests.discord.test_discord_talk import FakeAdmin

CAST = load_fixture(Path(__file__).parents[1] / "fixtures" / "session_basic.jsonl")[1]


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def closed(port):
    with socket.socket() as sock:
        sock.settimeout(2)
        return sock.connect_ex(("127.0.0.1", port)) != 0


async def until(check):
    async with asyncio.timeout(10):
        while not check():
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("cold", [False, True], ids=["after-logout", "before-first-bridge-cast"])
def test_discord_out_of_game_end_to_end(tmp_path, cold):
    data = tmp_path / "data"
    data.mkdir()
    ports = []

    async def go():
        clock = FakeClock(datetime(2026, 11, 5, 20, tzinfo=ZoneInfo("America/New_York")), yields=64)
        greeting = asyncio.Event()

        class DelayedBridge(FakeBridge):
            async def _write(self, writer, ev):
                if cold and ev["type"] == "hello":
                    await greeting.wait()
                await super()._write(writer, ev)

        bridge = DelayedBridge([{"type": "hello", "server": "mod-leeroy", "version": "0.1.0",
                                "guild_id": GUILD_ID, "human_guid": 0 if cold else HUMAN}, CAST])
        ports.append(await bridge.start())
        (data / "mind.toml").write_text(
            f"[bridge]\nport = {ports[0]}\n[talk]\nchattiness = 0\n"
            '[human]\nnickname = "Hero"\n', encoding="utf-8")
        store = Store(open_db(data / "mind.db"), clock=clock)
        try:
            seed_test_cast(store)
            for guid in TEST_CAST:
                save_member(store, guid, region="US", guild_role="officer" if guid == 102 else "raider")
            store.set_meta("human_guid", str(HUMAN))
            store.set_meta("human_name", "Testmage")
            store.upsert_cast([Member(guid=HUMAN, name="Testmage", race=10, class_=8,
                gender=0, level=80, rank=0, online=False, map=0, zone=0, spec_tab=-1)], clock.now())
        finally:
            store.close()
        cfg = load_config(data / "mind.toml")
        llm = FakeLLM(lambda *_: "🍕", models={cfg.models.live, cfg.models.reflection, cfg.models.embedding})
        app = MindApp(cfg, data, llm=llm, clock=clock, rng_seed=1)
        admin = FakeAdmin([cfg.models.embedding])
        app.arbiter.admin = admin
        task = asyncio.create_task(app.run())
        console = None
        try:
            if cold:
                await until(lambda: app.running and bridge.connections and
                            (data / endpoint.KEY_FILE).exists())
                assert not app.world.members and not app.world.online
            else:
                await until(lambda: app.world.human_online and (data / endpoint.KEY_FILE).exists())
                await bridge.push({"type": "logout", "guid": HUMAN, "human": True})
                await until(lambda: not app.world.human_online)
            port = json.loads((data / endpoint.KEY_FILE).read_text())["port"]
            ports.append(port)
            found = [SimpleNamespace(pid=os.getpid())]
            listening = [Conn("127.0.0.1", port, "0.0.0.0", 0, LISTEN, os.getpid())]
            console = make_app(data, mind=FakeMind(data, found=found, held=True),
                link=MindLink(data, tcp=lambda: listening), voice_ui_port=free_port(), clock=clock.now)
            await console.start()
            ports.append(console.s.port)
            assert not set(ports) & {7878, 7881, 11434}

            async def post(**body):
                response = await http(console, "POST", "/api/discord", body=body, origin=True)
                assert response.status == 200, response.body
                return response.json()

            async def answer_to(channel, mid):
                for _ in range(120):
                    rows = [m for m in discord_store.messages(app.store.conn, channel)
                            if m["origin"] == "mind" and m["reply_to"] == mid]
                    if rows:
                        return rows[0]
                    await clock.advance(1, 1)
                    await asyncio.sleep(0.005)
                raise AssertionError("no Discord answer within the deadline")

            sent = await post(action="post", channel="food", text="anyone around? 🍕")
            member = await answer_to("food", sent["id"])
            assert member["author"] in TEST_CAST and member["text"] == "🍕"
            assert "discord" in app.arbiter.holders
            await post(action="react", message=member["id"], emoji="😂")
            dm = await post(action="post", channel="dm:102", text="hello there")
            private = await answer_to("dm:102", dm["id"])
            assert private["author"] == 102 and private["channel"] == "dm:102"
            # A drawn post may still have a second answer queued. Let it finish and let
            # the normal per-person reply interval pass before the next public request.
            await clock.advance(60, 1)
            named = await post(action="post", channel="general", text="@Arkon are you around?")
            assert (await answer_to("general", named["id"]))["author"] == 101
            await clock.advance(60, 1)
            officer = await post(action="post", channel="officer-chat", text="@Arkon @Belra hello")
            assert (await answer_to("officer-chat", officer["id"]))["author"] == 102
            assert [t.speaker for t, _ in app.discord_talk.plan(officer["id"])] == [102]
            assert not [command for command in bridge.commands if command["type"] == "say"]
            view = (await http(console, "GET", "/api/discord?channel=food")).json()
            rows = {m["id"]: m for m in view["messages"]}
            assert rows[sent["id"]]["author"] == HUMAN
            assert rows[member["id"]]["reply_to"] == sent["id"]
            assert rows[member["id"]]["reactions"] == [{"emoji": "😂", "members": [HUMAN], "count": 1}]
            assert view["human"] == {"guid": HUMAN, "name": "Testmage"}
            assert not app.world.human_online
            if cold:
                assert not app.world.members and not app.world.online
                greeting.set()
                await until(lambda: bool(app.world.members))
                await bridge.push({"type": "cast", "members": []})
                await until(lambda: not app.world.members)
                assert not app.world.discord_members() and not app.world.online
                assert app.world.audience("discord:general", from_guid=HUMAN) == {HUMAN}
        finally:
            if console:
                await console.close()
            await app.stop()
            await asyncio.wait_for(task, 10)
            await bridge.stop()
        assert not app.arbiter.holders
    asyncio.run(go())
    assert not (data / endpoint.KEY_FILE).exists()
    assert len(set(ports)) == 3 and all(closed(port) for port in ports)
