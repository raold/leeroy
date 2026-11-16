"""His addons' automatic whispers are not his words (2026-11-15: on 11-09, 40 boss-mod auto
replies were stored as his whispers and 34 were answered). They are never stored as his line,
never answered and reach no handler; his own "<3" and pasted <Flask> checks still are his.
Invented names only."""
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from conftest import HUMAN, run, seed_test_cast
from dbtemplate import open_migrated
from leeroy_mind.app import MindApp
from leeroy_mind.config import load_config
from leeroy_mind.protocol import Chat
from leeroy_mind.storage.store import Store
from leeroy_mind.talk.pastes import is_addon_whisper
from leeroy_mind.testing.fake_llm import FakeLLM

ADDON = ("<DBM> Testmage has wiped on Valithria Dreamwalker",
         "<Deadly Boss Mods> Testmage is busy fighting against Valithria Dreamwalker "
         "(unknown, 7/25 people alive)",
         "<BigWigs> Testmage is in combat", "<dbm> Testmage has defeated Sindragosa",
         "<RCLootCouncil> Testmage is not in a group", "[AutoReply] I'm away right now",
         "Auto-reply: back in 10", "<Details!> Testmage is busy")
# a tag he types himself is his (review #325): only known addons' tags are dropped
HIS = ("<3", "<3 you", "<sigh> fine", "<Sigh> fine", "<AFK> back in 5", "<RaidHelper> lol",
       "<Flask> missing: Arkon", "<insert joke here>", "dbm says i wiped lol",
       "auto attack is broken", "[Shadowmourne] dropped!")


@pytest.mark.parametrize("text", ADDON)
def test_addon_whispers_are_recognised(text):
    assert is_addon_whisper(text)


@pytest.mark.parametrize("text", HIS)
def test_his_own_lines_are_not(text):
    assert not is_addon_whisper(text)


@asynccontextmanager
async def application(tmp_path, clock):
    config = tmp_path / "mind.toml"
    config.write_text("[talk]\nchattiness = 0\n", encoding="utf-8")
    seed = Store(open_migrated(tmp_path / "mind.db"), clock=clock)
    seed_test_cast(seed)
    seed.set_meta("human_guid", str(HUMAN))
    seed.close()
    app = MindApp(load_config(config), tmp_path, clock=clock, llm=FakeLLM())
    app.engine.on_event = AsyncMock()
    app.engine.on_human_chat = AsyncMock()
    app._start_session = AsyncMock()
    try:
        yield app
    finally:
        await app.stop()


def whisper(text, to=102):
    return Chat(ts=0, channel="whisper", from_guid=HUMAN, from_name="Testmage", to_guid=to,
                text=text, map=0, zone=1519)


def test_his_addon_whisper_is_no_line_no_answer_and_no_handler(tmp_path, clock):
    async def scenario():
        async with application(tmp_path, clock) as app:
            seen = []

            async def handler(ev):
                seen.append(ev.text)
            app.register_event_handler("chat", handler)
            for text in ADDON:
                await app.handle(whisper(text))
            assert app.store.conn.execute("SELECT count(*) FROM chat_line").fetchone()[0] == 0
            app.engine.on_human_chat.assert_not_called()
            assert seen == []
            await app.handle(whisper("<3 you"))
            [row] = app.store.conn.execute("SELECT text, origin FROM chat_line").fetchall()
            assert tuple(row) == ("<3 you", "human")
            app.engine.on_human_chat.assert_called_once()
            assert seen == ["<3 you"]
            # a member's line that quotes a boss mod is hers, not filtered here
            await app.handle(Chat(ts=0, channel="guild", from_guid=102, from_name="Belra",
                                  to_guid=0, text="<DBM> lol", map=0, zone=1519))
            assert app.store.conn.execute("SELECT count(*) FROM chat_line").fetchone()[0] == 2
    run(scenario())
