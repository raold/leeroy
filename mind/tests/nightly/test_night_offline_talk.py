"""Offline event recording continues without starting ordinary live-model reactions."""
import asyncio
from types import SimpleNamespace

import pytest

from conftest import run
from leeroy_mind.app import MindApp
from leeroy_mind.config import load_config
from leeroy_mind.memory.threads import ClosedThread
from leeroy_mind.protocol import Achievement, Boss, Group, Hello, Login, Logout, LootWon
from leeroy_mind.testing.fake_llm import FakeLLM


def app_for(h):
    return MindApp(load_config(h.settings.data_dir / "mind.toml"), h.settings.data_dir,
                   llm=FakeLLM(models={h.LIVE, h.REFL, h.EMB}), clock=h.clock)


@pytest.mark.parametrize("online", [False, True])
def test_fresh_game_events_keep_storage_and_world_updates_but_only_react_online(h, online):
    async def go():
        app = app_for(h)
        calls = []
        async def react(ev, now):
            calls.append(type(ev).__name__)
        app.engine.on_event = react
        app.world.human_online = online
        events = [Group(0, 101, True, (1, 101, 102)),
                  Boss(0, 533, 7, 15956, "Anub'Rekhan", "killed", (101, 102)),
                  LootWon(0, 101, 12345, "Practice Ring", "need"),
                  Achievement(0, 101, 123, "Practice achievement"),
                  Login(0, 103, False), Logout(0, 103, False)]
        before = app.store.conn.execute("SELECT count(*) FROM game_event").fetchone()[0]
        try:
            for ev in events:
                await app.handle(ev)
            assert calls == ([type(ev).__name__ for ev in events] if online else [])
            assert app.store.conn.execute("SELECT count(*) FROM game_event").fetchone()[0] == before + len(events)
            assert app.world.group.members == (1, 101, 102)
            assert 103 not in app.world.online
            assert app.raid.last_fight(533, 15956)[0] == "killed"
            assert app._memory_queue.qsize() == 3
            assert app.llm.calls == []
        finally:
            await app.stop()
    run(go())


def test_offline_ticker_still_expires_threads_and_embeds_then_resumes_live_tick(h):
    async def go():
        steps, ticks, expired = asyncio.Queue(), [], []
        embedded, live_tick = asyncio.Event(), asyncio.Event()
        class Clock:
            t = 0
            def now(self):
                return self.t
            async def sleep(self, _seconds):
                self.t += await steps.get()
        async def tick(now):
            ticks.append(now)
            live_tick.set()
        def expire(now):
            expired.append(now)
            return [ClosedThread(1, "guild", (), "silence")]
        async def embed():
            embedded.set()
        app = SimpleNamespace(clock=Clock(), world=SimpleNamespace(human_online=False),
            engine=SimpleNamespace(tick=tick), threads=SimpleNamespace(expire=expire),
            _extract_queue=asyncio.Queue(), _embed_task=None, _embed_pending=embed)
        task = asyncio.create_task(MindApp._ticker(app))
        try:
            steps.put_nowait(11)
            await asyncio.wait_for(embedded.wait(), 1)
            assert ticks == [] and expired == [11] and app._extract_queue.qsize() == 1
            app.world.human_online = True
            steps.put_nowait(1)
            await asyncio.wait_for(live_tick.wait(), 1)
            assert ticks == [12]
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            if app._embed_task is not None:
                await app._embed_task
    run(go())


def test_explicit_human_login_and_logout_keep_core_presence_dispatch(h):
    async def go():
        app = app_for(h)
        calls = []
        async def react(ev, _now):
            calls.append((type(ev).__name__, ev.human))
        app.engine.on_event = react
        try:
            await app.handle(Login(0, h.human, True))
            assert app.world.human_online and app.session_id is not None
            await app.handle(Logout(0, h.human, True))
            assert not app.world.human_online and app.session_id is None
            assert calls == [("Login", True), ("Logout", True)]
            await app.nightly.handback()
        finally:
            await app.stop()
    run(go())


def test_bridge_disconnect_stops_live_admission_until_hello_without_ending_session(h):
    async def go():
        app = app_for(h)
        try:
            await app.handle(Login(0, h.human, True))
            await app.nightly.handback()
            sid = app.session_id
            await app._on_connection(False)
            assert not app.world.human_online and app.nightly.presence.online is False
            assert not app.arbiter.holders and app.session_id == sid
            await app._on_connection(True)
            assert not app.world.human_online
            await app.handle(Hello(1, "test", "1", 21, h.human))
            await app.nightly.handback()
            assert app.world.human_online and app.session_id == sid
        finally:
            await app.stop()
    run(go())
