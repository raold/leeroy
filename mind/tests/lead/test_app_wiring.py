"""His "you lead" through a real MindApp over the FakeBridge and FakeLLM: his party line reaches the
game as one lead_do, her answer is written after it and told that she leads; "follow me" ends it
and stays a playerbots command (no answer); switched off, his line is only talk."""
import asyncio
import contextlib
import itertools

from conftest import HUMAN, run
from leeroy_mind.app import MindApp
from leeroy_mind.config import load_config
from leeroy_mind.testing.fake_bridge import FakeBridge
from leeroy_mind.testing.fake_llm import FakeLLM

from test_app import CAST, ORION, Harness, chat, hello, seed_db, toml

LINES = itertools.cycle(["ok follow me", "sure, this way", "on it"])
GROUP = {"type": "group", "leader_guid": HUMAN, "raid": False, "members": [HUMAN, 101, 102]}


@contextlib.asynccontextmanager
async def running(tmp_path, clock, *, features, prompts, lead=True):
    def script(model, messages):
        prompts.append("\n".join(m.get("content", "") for m in messages))
        return next(LINES)
    bridge = FakeBridge([{**hello(HUMAN), "features": features}, CAST])
    port = await bridge.start()
    text = toml(port)
    if not lead:
        text = text.replace("[talk]\n", "[talk]\nlead = false\n", 1)
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


def done(cmd):
    return {"type": "job", "id": cmd["id"], "kind": "lead_do", "state": "done", "reason": "leading",
            "stage": "", "guids": [102], "skipped": [], "kills": 0, "wipes": 0,
            "instance_id": 0, "guid": cmd["guid"], "level": 0, "avg_ilvl": 0}


def test_she_takes_the_lead_and_says_so(tmp_path, clock):
    prompts = []

    async def main():
        async with running(tmp_path, clock, features=["item_links", "lead"], prompts=prompts) as h:
            assert h.app.leading is not None
            await h.wait(lambda: h.app.world.online and h.app.world.human_online)
            await h.bridge.push(GROUP)
            await h.wait(lambda: h.app.world.group is not None)
            # the hello's clearing end of an earlier mind's lead
            first = await h.bridge.wait_for_command("lead_do")
            assert first["action"] == "end"
            await h.bridge.push(chat("Arkon, take the lead", channel="party"))
            cmd = await h.bridge.wait_for_command("lead_do")
            while cmd["action"] != "start":
                cmd = await h.bridge.wait_for_command("lead_do")
            assert (cmd["guid"], cmd["activity"]) == (101, "quest") and "spot" not in cmd
            await h.bridge.push(done(cmd))
            await h.until(lambda: any(s["guid"] == 101 and s["channel"] == "party" for s in h.says()),
                          timeout=10.0)
            assert any("you're leading his group out questing" in p for p in prompts)
            row = h.rows("SELECT origin, thread_id FROM chat_line WHERE text = ?",
                         ("Arkon, take the lead",))[0]
            assert row["origin"] == "human" and row["thread_id"] is not None  # still his talk
            # "follow me" ends it: a playerbots command, so nobody answers it
            said = len(h.says())
            await h.bridge.push(chat("follow me", channel="party"))
            end = await h.bridge.wait_for_command("lead_do")
            while end["action"] != "end":
                end = await h.bridge.wait_for_command("lead_do")
            assert end["guid"] == 101
            await asyncio.sleep(0.3)
            assert len(h.says()) == said
    run(main())


def test_switched_off_his_line_is_only_talk(tmp_path, clock):
    prompts = []

    async def main():
        async with running(tmp_path, clock, features=["lead"], prompts=prompts, lead=False) as h:
            await h.wait(lambda: h.app.world.online and h.app.world.human_online)
            await h.bridge.push(GROUP)
            await h.bridge.push(chat("Arkon, take the lead", channel="party"))
            await h.until(lambda: any(s["channel"] == "party" for s in h.says()), timeout=10.0)
            assert not [c for c in h.bridge.commands
                        if c["type"] == "lead_do" and c.get("action") == "start"]
            assert not any("leading his group" in p for p in prompts)
    run(main())
