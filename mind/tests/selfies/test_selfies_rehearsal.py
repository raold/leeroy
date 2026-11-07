"""P2 Task 15: the headless rehearsal of profile photos. A real MindApp on a scratch data dir with
its real control endpoint, a FakeBridge, a FakeLLM, the fake worker (no torch, no model, no GPU)
and a real ConsoleApp with a real MindLink, each on a free loopback port of its own. It touches
no live data, bridge, Ollama, voice service or console. Every person here is invented."""
import asyncio
import json
import os
import socket
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from conftest import HUMAN, TEST_CAST, run, seed_test_cast
from console.rig import FakeMind, http, make_app
from leeroy_mind import endpoint
from leeroy_mind.app import GUILD_ID, MindApp
from leeroy_mind.clock import night_of
from leeroy_mind.config import load_config
from leeroy_mind.console.mindlink import MindLink
from leeroy_mind.console.netinfo import LISTEN, Conn
from leeroy_mind.nightly.store import run_get
from leeroy_mind.personas.io import load_persona, save_persona
from leeroy_mind.protocol import Member
from leeroy_mind.selfies import files, stage
from leeroy_mind.selfies.job import pid_alive
from leeroy_mind.selfies.store import ready_set
from leeroy_mind.storage.db import open_db
from leeroy_mind.storage.store import Store
from leeroy_mind.testing.fake_bridge import FakeBridge, load_fixture
from leeroy_mind.testing.fake_clock import FakeClock
from leeroy_mind.testing.fake_llm import FakeLLM
from leeroy_mind.wow import CLASSES, RACES
from nightly.conftest import FakeAdmin, generic
from nightly.test_night_enrich import APPEARANCE
from nightly.test_night_photo_kind import KIND

from .fakes import CLOCK, fake_command, until

FIXTURE = Path(__file__).parents[1] / "fixtures" / "session_basic.jsonl"
CAST = load_fixture(FIXTURE)[1]  # 101-105, invented
SCHEMA = "matching this schema:"
MEMBERS = sorted(TEST_CAST)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def closed(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(2)
        return s.connect_ex(("127.0.0.1", port)) != 0


def answer(model: str, messages: list[dict]) -> str:
    """A photo kind for the photo pass, a quiet generic reply for any other schema."""
    text = messages[-1]["content"]
    if SCHEMA not in text:
        return "evening"
    shape, _ = json.JSONDecoder().raw_decode(text.rsplit(SCHEMA, 1)[-1].lstrip())
    return json.dumps(KIND if shape["title"] == "photo" else generic(text))


def with_looks(store: Store) -> None:
    """The synthetic cast with looks and no photo kind: the night must choose the kinds."""
    for guid, (_, race, cls) in TEST_CAST.items():
        p = load_persona(store, guid)
        assert p.photo is None
        save_persona(store, replace(p, appearance=APPEARANCE), source="seed", ts=1000.0,
                     race=RACES[race], class_name=CLASSES[cls])


def test_profile_photos_end_to_end(tmp_path, monkeypatch):
    data = tmp_path / "mind-data"
    data.mkdir()
    env: dict[str, str] = {}  # the fake worker's behaviour for the night under way
    monkeypatch.setattr(stage, "worker_command", lambda cfg: fake_command(tmp_path)[0])
    monkeypatch.setattr(stage, "worker_env", lambda cfg: {**fake_command(tmp_path)[1], **env})
    pids, log = tmp_path / "pids.json", tmp_path / "requests.log"
    hello = {"type": "hello", "server": "mod-leeroy", "version": "0.1.0", "guild_id": GUILD_ID,
             "human_guid": HUMAN}
    out = {}

    async def go():
        clock = FakeClock(CLOCK, yields=64)
        bridge = FakeBridge([hello, CAST])
        bridge_port = await bridge.start()
        out["ports"] = [bridge_port]
        (data / "mind.toml").write_text(
            f"[bridge]\nport = {bridge_port}\n[talk]\nchattiness = 0\n"
            "[selfies]\nenabled = true\n", encoding="utf-8")
        store = Store(open_db(data / "mind.db"), clock=clock)
        try:
            seed_test_cast(store)
            with_looks(store)
            store.upsert_cast([Member(guid=HUMAN, name="Testmage", race=10, class_=8, gender=0,
                                      level=80, rank=0, online=False, map=0, zone=0,
                                      spec_tab=-1)], 1000.0)
        finally:
            store.close()
        cfg = load_config(data / "mind.toml")
        llm = FakeLLM(answer, models={cfg.models.live, cfg.models.reflection,
                                      cfg.models.embedding})
        app = MindApp(cfg, data, llm=llm, clock=clock)
        admin = FakeAdmin([cfg.models.embedding])
        app.arbiter.admin = admin
        seen = {}
        preload = admin.preload

        async def checked_preload(model, keep_alive, *, options=None):
            if model == cfg.models.live and pids.exists():
                got = json.loads(pids.read_text())
                seen["alive"] = (pid_alive(got["pid"]), pid_alive(got["child"]))
            await preload(model, keep_alive, options=options)

        admin.preload = checked_preload
        task = asyncio.create_task(app.run())
        console = None
        try:
            await until(lambda: app.world.human_online, 10)
            await bridge.push({"type": "logout", "guid": HUMAN, "human": True})
            await until(lambda: app.nightly.presence.online is False, 10)

            # the first night: past the grace period it runs on the idle GPU, to its end
            async def advance_until(done, step=30.0):
                while not done():
                    await clock.advance(step, step)
                    await asyncio.sleep(0.02)

            def night_done():
                run_ = run_get(app.store, night_of(clock.local()))
                return run_ is not None and run_.status == "done"

            await asyncio.wait_for(advance_until(night_done), 120)
            for guid in MEMBERS:
                assert load_persona(app.store, guid).photo is not None
                assert ready_set(app.store, guid, 1)
                assert files.candidate(data, guid, 1, 3).exists()
                assert files.candidate(data, guid, 1, 3, thumb=True).exists()

            # the console lists them and picks one through the mind's real endpoint; the TCP table
            # (Windows only, None elsewhere) is given as the endpoint's own listener in this process
            ui_port = free_port()
            found = [SimpleNamespace(pid=os.getpid())]
            mind_port = json.loads((data / endpoint.KEY_FILE).read_text())["port"]
            listening = [Conn("127.0.0.1", mind_port, "0.0.0.0", 0, LISTEN, os.getpid())]
            console = make_app(data, mind=FakeMind(data, found=found, held=True),
                               link=MindLink(data, tcp=lambda: listening), voice_ui_port=ui_port)
            await console.start()
            out["ports"] += [console.s.port]
            assert console.s.port not in (ui_port, bridge_port)
            listed = (await http(console, "GET", "/api/selfies")).json()
            assert listed["view"]["available"] and listed["mind"]["ok"] is True
            assert listed["mind"]["enabled"] is True
            assert listed["view"]["counts"]["waiting"] == len(MEMBERS)
            sets = (await http(console, "GET", "/api/selfies/member?guid=101")).json()["sets"]
            assert [(s["set"], s["status"]) for s in sets] == [(1, "ready")]

            async def post(**body):
                r = await http(console, "POST", "/api/selfie", body=body, origin=True)
                assert r.status == 200, r.body
                return r.json()

            picked = await post(action="pick", guid=101, set=1, index=2)
            assert picked["ok"] is True and picked["guid"] == 101
            chosen = files.chosen(data, 101)
            assert chosen.read_bytes() == files.candidate(data, 101, 1, 2).read_bytes()
            back = await post(action="undo", pick=picked["pick"])
            assert back["ok"] is True and not chosen.exists()
            more = await post(action="more", guid=101)
            assert more["ok"] is True and more["set"] == 2

            # the second night hangs mid-render; his login ends the worker's whole tree before
            # the live model is preloaded
            env.update(FAKE_SELFIE_HANG="1", FAKE_SELFIE_CHILD="1",
                       FAKE_SELFIE_PIDS=str(pids), FAKE_SELFIE_LOG=str(log))
            await bridge.push({"type": "logout", "guid": HUMAN, "human": True})
            await asyncio.wait_for(advance_until(
                lambda: log.exists() and "101:2:0" in log.read_text(), 600.0), 120)
            assert app.nightly.running is not None
            assert all(pid_alive(p) for p in json.loads(pids.read_text()).values())
            await bridge.push({"type": "login", "guid": HUMAN, "human": True})
            await until(lambda: "alive" in seen, 20)
            assert seen["alive"] == (False, False)
            await app.nightly.handback()
            assert not ready_set(app.store, 101, 2)
            out["ports"] += [json.loads((data / endpoint.KEY_FILE).read_text())["port"]]
        finally:
            if console is not None:
                await console.close()
            await app.stop()
            await asyncio.wait({task}, timeout=10)
            await bridge.stop()

    run(go())
    assert not (data / endpoint.KEY_FILE).exists()
    assert len(out["ports"]) == 3 and len(set(out["ports"])) == 3
    assert all(closed(p) for p in out["ports"])
