"""Real serve(), extension and client; synthetic cast, FakeBridge and FakeLLM only."""
import asyncio
import json
import random
import sys
from dataclasses import asdict

import leeroy_mind
import conftest as mind_fixtures
from conftest import TEST_CAST, seed_test_cast
from leeroy_mind.__main__ import serve
from leeroy_mind.clock import SystemClock
from leeroy_mind.config import load_config
from leeroy_mind.llm.presets import LIVE
from leeroy_mind.nightly.gpu import NullAdmin
from leeroy_mind.nightly.scheduler import NightScheduler
from leeroy_mind.protocol import Member
from leeroy_mind.storage.db import open_db
from leeroy_mind.storage.store import Store
from leeroy_mind.talk.turns import TurnKind
from leeroy_mind.testing.fake_bridge import FakeBridge
from leeroy_mind.testing.fake_llm import FakeLLM
from leeroy_mind.voice import wire
from rehearsal_common import (CUT_LINE, FIRST_LINE, RUN, WORKTREE, endpoint, package_path, publish,
                              stage, under, until)


def test_mind_leg(tmp_path, monkeypatch, live_guard):
    assert sys.version_info[:2] >= (3, 14)
    imported = package_path(leeroy_mind, "mind")
    under(mind_fixtures.__file__, WORKTREE / "mind" / "tests")
    root = under(tmp_path / "mind-data", RUN)
    root.mkdir()
    monkeypatch.setenv("LEEROY_MIND_DATA", str(root))
    monkeypatch.setenv("LEEROY_VOICE_DATA", str(tmp_path / "unused-voice-data"))
    assert not (root / "mind.db").exists()

    async def run():
        ready = await stage("voice-ready")
        port = endpoint(ready["ports"][0])
        specs = root / "voices"
        specs.mkdir()
        for spec in ready["specs"]:
            assert spec["guid"] in TEST_CAST and spec["engine"] == "kokoro" and not spec["ref"]
            (specs / f"{spec['guid']}.json").write_text(json.dumps(spec), encoding="utf-8")

        unexpected_ollama = []

        def refused_probe(reader, writer):
            unexpected_ollama.append("connection")
            writer.close()

        trap = await asyncio.start_server(refused_probe, "127.0.0.1", 0)
        ollama_port = endpoint(trap.sockets[0].getsockname()[1])
        cast = [dict(guid=g, name=n, race=r, **{"class": c}, gender=0, level=80, rank=3,
                     online=True, map=0, zone=1519, spec_tab=0)
                for g, (n, r, c) in TEST_CAST.items()]
        cast.append(dict(guid=1, name="Testmage", race=10, **{"class": 8}, gender=0, level=80,
                         rank=0, online=True, map=0, zone=1519, spec_tab=0))
        bridge = FakeBridge(cast=cast, human_guid=1)
        bridge_port = endpoint(await bridge.start())
        assert len({bridge_port, ollama_port, *ready["ports"]}) == 4
        (root / "mind.toml").write_text(
            '[models]\nlive="rehearsal-chat"\nlive_fallback="rehearsal-fallback"\n'
            'reflection="rehearsal-reflection"\nembedding="rehearsal-embed"\n'
            f'[bridge]\nhost="127.0.0.1"\nport={bridge_port}\n'
            f'[ollama]\nurl="http://127.0.0.1:{ollama_port}"\n'
            '[talk]\nchattiness=0\n[voice]\nenabled=true\nautostart=false\n'
            'hot_mic_per_month=0\nelsewhere_lines_per_10min=0\n'
            f'host="127.0.0.1"\nport={port}\non_comms_raider=1.0\non_comms_social=1.0\n',
            encoding="utf-8")
        cfg = load_config(root / "mind.toml")
        store = Store(open_db(root / "mind.db"), clock=SystemClock())
        try:
            seed_test_cast(store)
            store.upsert_cast([Member(1, "Testmage", 10, 8, 0, 80, 0, True, 0, 1519, 0)], 1000)
            assert set(store.cast()) == {1, *TEST_CAST}
        finally:
            store.close()
        fake_llm = FakeLLM(lambda model, messages: "", models=asdict(cfg.models).values())
        conf = root / "fake-ollama-chat.conf"
        conf.write_text('OllamaChat.Model = "rehearsal-chat"\n'
                        f'OllamaChat.NumCtx = {LIVE.options["num_ctx"]}\n', encoding="utf-8")
        apps, received = [], []

        def app_ready(app):
            apps.append(app)
            # Exercise the production attachment with fake model administration.
            assert isinstance(app.nightly, NightScheduler) and not hasattr(app, "guild")
            assert app.nightly.arbiter is app.arbiter
            assert isinstance(app.arbiter.admin, NullAdmin)
            assert app._background.count(app.nightly.run_forever) == 1
            assert app.nightly.running is None and not app._bg_tasks
            assert app.data_dir == root and app.voice.cfg.autostart is False
            assert app.cfg.bridge.port == bridge_port and app.voice.cfg.port == port
            # Production remains random; this transport rehearsal must not roll an initial
            # penalty visit before the other process checks the six-seat snapshot.
            app.voice.movement.rng = random.Random(0)
            original = app.voice.client._on_message

            async def observe(message):
                received.append(message)
                await original(message)

            app.voice.client._on_message = observe

        task = asyncio.create_task(serve(root, make_llm=lambda config: fake_llm,
                                         ready=app_ready, ollama_conf=conf))
        app = None
        try:
            await until(lambda: bool(apps), "mind construction")
            app = apps[0]
            await until(lambda: app.world.human_online and app.voice.comms.available(),
                        "real voice greeting and healthy status")
            assert "human" in app.arbiter.holders and app.nightly.due() is None
            await stage("snapshot-checked")
            assert app.voice.rooms.where == {g: "guild_hall" for g in (1, *TEST_CAST)}

            # Real bridge events drive the room policy and its Moved messages. A party uses
            # raid comms too, AFK returns to it, and disband returns the party home.
            await bridge.push({"type": "group", "leader_guid": 102, "raid": False,
                               "members": [1, 101, 102]})
            await until(lambda: all(app.voice.rooms.where[g] == "raid" for g in (1, 101, 102)),
                        "party room drag")
            await bridge.push({"type": "afk", "guid": 101, "on": True})
            await until(lambda: app.voice.rooms.where[101] == "afk", "member AFK room")
            await bridge.push({"type": "afk", "guid": 101, "on": False})
            await until(lambda: app.voice.rooms.where[101] == "raid", "member AFK return")
            await bridge.push({"type": "group", "leader_guid": 0, "raid": False, "members": []})
            await until(lambda: all(app.voice.rooms.where[g] == "guild_hall" for g in (1, 101, 102)),
                        "party disband room return")
            await bridge.push({"type": "logout", "guid": 105, "human": False})
            await until(lambda: 105 not in app.voice.rooms.where, "member leaves comms")
            await bridge.push({"type": "login", "guid": 105, "human": False})
            await until(lambda: app.voice.rooms.where.get(105) == "guild_hall", "member returns to comms")
            assert app.nightly.presence.online is True and app.arbiter.holders == {"human"}

            def lines():
                return app.store.conn.execute("SELECT * FROM voice_line ORDER BY rowid").fetchall()

            assert await app.engine.say_as(101, "guild", FIRST_LINE, kind=TurnKind.OPEN)
            await until(lambda: any(row["state"] == "done" for row in lines()), "Played done in DB")
            first = next(row for row in lines() if row["state"] == "done")
            first_states = [m.state for m in received if isinstance(m, wire.Played)
                            and m.id == first["speak_id"]]
            assert first_states == ["started", "done"]
            assert first["room"] == "guild_hall" and first["reason"] is None
            assert not [cmd for cmd in bridge.commands if cmd.get("type") == "say"]
            publish("first-played", states=first_states)
            await stage("joins-checked")
            assert [m.channel for m in received if isinstance(m, wire.Join)] == ["lore", "guild_hall"]
            assert app.voice.rooms.human_room() == "guild_hall"

            def heard_rows():
                return app.store.conn.execute("SELECT c.channel, c.text, h.* FROM voice_heard h "
                                              "JOIN chat_line c ON c.id=h.line_id").fetchall()

            await until(lambda: bool(heard_rows()), "Heard injected and persisted")
            heard = heard_rows()
            assert len(heard) == 1 and heard[0]["channel"] == "voice:guild_hall"
            assert heard[0]["text"] == heard[0]["raw"] == "hello team"
            assert heard[0]["speech_ms"] == 1100 and heard[0]["confidence"] == 0.9
            publish("heard-stored", count=1, channel=heard[0]["channel"])
            await stage("ptt-checked")
            await until(lambda: not app.voice.comms.ptt_down, "PTT released")
            # Use another speaker so the chatter cooldown does not gate the test.
            assert await app.engine.say_as(102, "guild", CUT_LINE, kind=TurnKind.OPEN)
            publish("cut-requested")
            await stage("logout-ready")
            await until(lambda: app.voice.comms.ptt_down, "second PTT received by mind")
            await bridge.push({"type": "logout", "guid": 1, "human": True})
            await stage("logout-checked")
            await until(lambda: any(row["state"] == "cut" for row in lines()), "Played cut in DB")
            cut = next(row for row in lines() if row["state"] == "cut")
            assert cut["reason"] == "offline"
            assert not app.voice.comms.ptt_down and not app.voice.rooms.where
            assert app.nightly.presence.online is False and not app.arbiter.holders
            assert app.nightly.due() is None  # the offline grace period still applies
            assert len(heard_rows()) == 1
            assert app.store.conn.execute("SELECT COUNT(*) FROM voice_presence WHERE until IS NULL").fetchone()[0] == 0
            publish("stop-voice")
            await stage("voice-result")
            await until(lambda: not app.voice.client.greeted, "real service disconnect")
            await bridge.push({"type": "login", "guid": 1, "human": True})
            await until(lambda: app.world.human_online, "synthetic relogin")
            assert "human" in app.arbiter.holders and app.nightly.due() is None
            assert not app.voice.comms.available()
            before = len(lines())
            assert await app.engine.say_as(103, "guild", "still here", kind=TurnKind.OPEN)
            says = [cmd for cmd in bridge.commands if cmd.get("type") == "say"]
            assert says and says[-1]["guid"] == 103 and says[-1]["channel"] == "guild"
            assert len(lines()) == before
            assert not unexpected_ollama and not app.voice._gpu_tasks
            assert app.store.conn.execute("SELECT COUNT(*) FROM night_run").fetchone()[0] == 0
            assert not any(call["model"] == cfg.models.reflection for call in fake_llm.calls)
            assert not any(cmd.get("type") == "afk" for cmd in bridge.commands)
            summary = dict(passed=True, imported=imported, python=sys.version,
                           ports=[bridge_port, ollama_port], synthetic_personas=5,
                           played_states=first_states, logout_state=cut["state"],
                           logout_reason=cut["reason"], transcripts=1, ui_joins=2,
                           typed_fallback=True, ollama_connections=0, guard_hits=0,
                           party_drag=True, afk_return=True, member_relogin=True,
                           nightly_extension="attached with fake model administration",
                           nightly_presence_leases=True, nightly_runs=0)
        finally:
            if app is not None:
                await app.stop()
            if not task.done():
                await asyncio.wait_for(task, 20)
            await bridge.stop()
            trap.close()
            await trap.wait_closed()
        assert task.result() == 0 and app._closed and not app.running
        assert app.nightly.running is None and not app.nightly._handbacks
        assert all(t.done() for t in app._bg_tasks)
        assert bridge._server is None and all(t.done() for t in bridge._handlers)
        assert not live_guard.hits and not unexpected_ollama
        publish("mind-result", **summary, app_stopped=True, bridge_stopped=True)

    asyncio.run(asyncio.wait_for(run(), 110))
