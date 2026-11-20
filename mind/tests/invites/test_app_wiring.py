"""His invitation through a real MindApp over the FakeBridge and FakeLLM: his whisper "meet me at
the inn in dalaran" reaches the game as one string_do go_to marked as an invitation, and her
answer is written after it, told that she is there."""
import asyncio
import contextlib
import itertools
from types import SimpleNamespace

import pytest

from conftest import HUMAN, run
from leeroy_mind.app import MindApp
from leeroy_mind.config import load_config
from leeroy_mind.invites import policy
from leeroy_mind.testing.fake_bridge import FakeBridge
from leeroy_mind.testing.fake_llm import FakeLLM

from test_app import CAST, ORION, Harness, chat, hello, seed_db, toml

LINES = itertools.cycle(["ooh yes, see you there", "omw, grabbing a seat", "be right there"])


@contextlib.asynccontextmanager
async def running(tmp_path, clock, *, features, prompts, invites=True):
    def script(model, messages):
        prompts.append("\n".join(m.get("content", "") for m in messages))
        return next(LINES)
    bridge = FakeBridge([{**hello(HUMAN), "features": features}, CAST])
    port = await bridge.start()
    text = toml(port)
    if not invites:
        text = text.replace("[talk]\n", "[talk]\ninvites = false\n", 1)
    (tmp_path / "mind.toml").write_text(text, encoding="utf-8")
    seed_db(tmp_path / "mind.db", clock)
    llm = FakeLLM(script, models={ORION})
    app = MindApp(load_config(tmp_path / "mind.toml"), tmp_path, llm=llm, clock=clock, rng_seed=1)
    task = asyncio.create_task(app.run())
    try:
        yield Harness(app, bridge, llm, clock, task)
    finally:
        await app.stop()
        await asyncio.wait({task}, timeout=5.0)
        await bridge.stop()


@pytest.fixture
def yes(monkeypatch):
    monkeypatch.setattr(policy, "seeded", lambda line, guid: SimpleNamespace(random=lambda: 0.0))


def done(cmd, reason):
    return {"type": "job", "id": cmd["id"], "kind": "string_do", "state": "done", "reason": reason,
            "stage": "", "guids": [cmd["guid"]], "skipped": [], "kills": 0, "wipes": 0,
            "instance_id": 0, "guid": cmd["guid"], "level": 0, "avg_ilvl": 0}


def test_her_body_moves_first_and_her_answer_says_so(tmp_path, clock, yes):
    prompts = []

    async def main():
        async with running(tmp_path, clock, features=["item_links", "invites"], prompts=prompts) as h:
            assert h.app.invites is not None
            await h.wait(lambda: h.app.world.online and h.app.world.human_online)
            await h.bridge.push(chat("meet me at the inn in dalaran", channel="whisper", to_guid=101))
            cmd = await h.bridge.wait_for_command("string_do")
            assert (cmd["guid"], cmd["kind"], cmd["spot"], cmd["via"]) == (101, "go_to", "dal_inn", "invite")
            assert "slot" not in cmd  # slot 0 is native's default: only a second member's goes
            await h.bridge.push(done(cmd, "went"))
            await h.until(lambda: any(s["guid"] == 101 and s["channel"] == "whisper" for s in h.says()),
                          timeout=10.0)
            assert any("Legerdemain Lounge" in p and "waiting for" in p for p in prompts)
            row = h.rows("SELECT origin, thread_id FROM chat_line WHERE text = ?",
                         ("meet me at the inn in dalaran",))[0]
            assert row["origin"] == "human" and row["thread_id"] is not None  # still his talk
    run(main())


def test_switched_off_his_line_is_only_talk(tmp_path, clock, yes):
    prompts = []

    async def main():
        async with running(tmp_path, clock, features=["invites"], prompts=prompts,
                           invites=False) as h:
            await h.wait(lambda: h.app.world.online and h.app.world.human_online)
            await h.bridge.push(chat("meet me at the inn in dalaran", channel="whisper", to_guid=101))
            await h.until(lambda: any(s["guid"] == 101 for s in h.says()), timeout=10.0)
            assert not [c for c in h.bridge.commands if c["type"] == "string_do"]
            assert not any("Legerdemain Lounge" in p for p in prompts)
    run(main())
