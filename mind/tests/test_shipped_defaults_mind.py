"""A mind whose mind.toml sets no feature switch at all: every shipped feature is on (2026-11-25),
and none of their prerequisites is here (no voice service or its venv, no selfie worker or GPU, no
Discord, no ffmpeg, no realm blocklist, no native capability in the bridge's hello). Each feature
attaches or says why it is off, and the mind starts, meets the game and stops cleanly, launching
and reaching nothing live (the live guard fails the test otherwise). Invented data only."""
import asyncio
import logging
import socket

from conftest import HUMAN
from test_app import CAST, ORION, Harness, hello, seed_db
from leeroy_mind.app import MindApp
from leeroy_mind.config import load_config
from leeroy_mind.testing.fake_bridge import FakeBridge
from leeroy_mind.testing.fake_llm import FakeLLM
from leeroy_mind.voice.attach import VoiceExtension
from shipped_off import keep_pins


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_every_shipped_feature_on_without_its_prerequisites_starts_and_stops(tmp_path, clock,
                                                                              monkeypatch, caplog):
    monkeypatch.setattr(VoiceExtension, "_pythonw", staticmethod(lambda: None))  # no voice venv
    keep_pins(monkeypatch)

    async def main():
        bridge = FakeBridge([hello(HUMAN), CAST])
        port = await bridge.start()
        # the voice service's port only: nothing listens there (the live one is never probed)
        (tmp_path / "mind.toml").write_text(f"[bridge]\nport = {port}\n[voice]\nport = {free_port()}\n",
                                            encoding="utf-8")
        seed_db(tmp_path / "mind.db", clock)
        llm = FakeLLM(lambda model, messages: "ok then", models={ORION})
        app = MindApp(load_config(tmp_path / "mind.toml"), tmp_path, llm=llm, clock=clock)
        task = None
        try:
            assert app.disabled_features == {}  # every feature attached
            assert app.voice.cfg.enabled and app.voice.cfg.autostart and app.selfies.cfg.enabled
            for name in ("server", "photos", "outreach", "decisions", "godmode", "raidlead",
                         "realm", "world_people", "banter", "playstyle", "training"):
                assert getattr(app, name) is not None, name
            task = asyncio.create_task(app.run())
            h = Harness(app, bridge, llm, clock, task)
            await h.wait(lambda: app.world.human_online and 101 in app.world.members)
            await h.advance(120)
        finally:
            await app.stop()
            if task is not None:
                done, _ = await asyncio.wait({task}, timeout=10)
                assert task in done and not task.cancelled() and task.exception() is None
            await bridge.stop()

    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        asyncio.run(main())
    assert "voice_autostart_skipped" in caplog.text  # its venv missing: said so, nothing started
    assert "feature_off" not in caplog.text
