"""The raid lead in a real MindApp over the FakeBridge and FakeLLM: his raid-chat order is no talk,
reaches the game as a bot_do "order", and the lead's acknowledgement waits for the job."""

import asyncio
import contextlib
import itertools

import pytest

from conftest import HUMAN, run
from leeroy_mind.app import MindApp
from leeroy_mind.config import load_config
from leeroy_mind.testing.fake_bridge import FakeBridge
from leeroy_mind.testing.fake_llm import FakeLLM

from test_app import CAST, ORION, Harness, chat, hello, seed_db, toml

LINES = itertools.cycle(["leading it, let's go", "on you, stacking up now", "copy that", "got it boss"])


def script(model, messages):
    return next(LINES)  # a fresh line each time: the repeat guard drops a line said twice


@contextlib.asynccontextmanager
async def running(tmp_path, clock, *, raidlead):
    bridge = FakeBridge([{**hello(HUMAN), "features": ["item_links", "playstyle", "raidlead"]}, CAST])
    port = await bridge.start()
    extra = "\n[raidlead]\nenabled = true\n" if raidlead else ""
    (tmp_path / "mind.toml").write_text(toml(port) + extra, encoding="utf-8")
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


def group(*members):
    return {"type": "group", "leader_guid": HUMAN, "raid": True, "members": [HUMAN, *members]}


def test_an_order_is_carried_out_then_acknowledged(tmp_path, clock):
    async def main():
        async with running(tmp_path, clock, raidlead=True) as h:
            assert h.app.raidlead is not None
            await h.wait(lambda: h.app.world.online and h.app.raidlead.supported)
            await h.bridge.push(group(101, 102, 103, 104, 105))
            # native's switch answers its sync first: until then the raid lead holds nothing
            await h.until(lambda: h.app.raidlead.native is True and h.app.raidlead.members)
            await h.bridge.push(chat("Arkon leads", channel="raid"))
            await h.wait(lambda: h.app.raidlead.lead() == 101)
            await h.advance(30)
            before = len(h.says())
            await h.bridge.push(chat("everyone stack on me", channel="raid"))
            cmd = await h.bridge.wait_for_command("bot_do")
            assert (cmd["action"], cmd["param"], sorted(cmd["guids"])) == ("order", "stack", [101, 102, 103, 104, 105])
            await h.advance(10)
            assert len(h.says()) == before  # nothing is said before the game's job confirms it
            await h.bridge.push({"type": "job", "id": cmd["id"], "kind": "bot_do", "state": "done",
                                 "reason": "", "stage": "", "guids": cmd["guids"], "skipped": [],
                                 "kills": 0, "wipes": 0, "instance_id": 0, "guid": 0, "level": 0,
                                 "avg_ilvl": 0})
            await h.until(lambda: len(h.says()) > before)
            said = h.says()[before:]
            assert said[0]["guid"] == 101 and said[0]["channel"] == "raid"
            # his order is stored, and never threaded as talk
            row = h.rows("SELECT origin, thread_id FROM chat_line WHERE text = ?", ("everyone stack on me",))[0]
            assert row["origin"] == "human" and row["thread_id"] is None
    run(main())


def test_off_the_same_line_is_talk(tmp_path, clock):
    async def main():
        async with running(tmp_path, clock, raidlead=False) as h:
            assert h.app.raidlead is None
            await h.wait(lambda: h.app.world.online)
            await h.bridge.push(group(101, 102, 103, 104, 105))
            await h.bridge.push(chat("everyone stack on me", channel="raid"))
            await h.wait(lambda: h.count("chat_line", "WHERE text = 'everyone stack on me'") == 1)
            await h.advance(30)
            assert not [c for c in h.bridge.commands if c["type"] == "bot_do"]
            row = h.rows("SELECT thread_id FROM chat_line WHERE text = ?", ("everyone stack on me",))[0]
            assert row["thread_id"] is not None
    run(main())


def _bot_dos(h):
    return [c for c in h.bridge.commands if c["type"] == "bot_do"]


def _done(cmd):
    return {"type": "job", "id": cmd["id"], "kind": "bot_do", "state": "done", "reason": "",
            "stage": "", "guids": cmd["guids"], "skipped": [], "kills": 0, "wipes": 0,
            "instance_id": 0, "guid": 0, "level": 0, "avg_ilvl": 0}


@pytest.mark.parametrize("text", ["dps, hold", "wait for my pull", "dps stop"])
def test_orders_that_start_with_a_playerbots_word_reach_the_raid_lead(tmp_path, clock, text):
    """P2-3, through _on_chat: "wait" and "dps" are playerbots words (his_command), yet these
    lines are the raid lead's orders. The invented cast's damage dealers (by persona role) hold."""
    async def main():
        async with running(tmp_path, clock, raidlead=True) as h:
            await h.wait(lambda: h.app.world.online and h.app.raidlead.supported)
            await h.bridge.push(group(101, 102, 103, 104, 105))
            await h.until(lambda: h.app.raidlead.native is True and h.app.raidlead.members)
            await h.bridge.push(chat(text, channel="raid"))
            cmd = await h.bridge.wait_for_command("bot_do")
            assert (cmd["action"], cmd["param"]) == ("order", "hold")
            await h.bridge.push(_done(cmd))
            row = h.rows("SELECT thread_id FROM chat_line WHERE text = ?", (text,))[0]
            assert row["thread_id"] is None
    run(main())


@pytest.mark.parametrize("text", ["follow", "stay", "attack"])
def test_playerbots_own_words_still_pass_through(tmp_path, clock, text):
    async def main():
        async with running(tmp_path, clock, raidlead=True) as h:
            await h.wait(lambda: h.app.world.online and h.app.raidlead.supported)
            await h.bridge.push(group(101, 102, 103, 104, 105))
            await h.until(lambda: h.app.raidlead.native is True and h.app.raidlead.members)
            await h.bridge.push(chat(text, channel="raid"))
            await h.wait(lambda: h.count("chat_line", "WHERE text = ?", (text,)) == 1)
            await h.advance(10)
            assert _bot_dos(h) == []
            row = h.rows("SELECT thread_id FROM chat_line WHERE text = ?", (text,))[0]
            assert row["thread_id"] is None  # playerbots' command, as before: no talk either
    run(main())
