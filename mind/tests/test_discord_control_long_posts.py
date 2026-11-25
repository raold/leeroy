"""Full Discord messages cross the real control socket without the #457 test helpers."""
import asyncio

import pytest

from leeroy_mind.discord.text import MAX_TEXT
from test_endpoint import ask, served


def action_args(action, text):
    largest_id = 2**63 - 1
    if action == "post":
        return {"action": action, "channel": "dm:4294967295", "text": text,
                "reply_to": largest_id, "thread": largest_id}
    return {"action": action, "message": largest_id, "text": text}


@pytest.mark.parametrize("character", ["a", "é", "漢", "\U0001F600"],
                         ids=["ascii", "accent", "cjk", "astral"])
@pytest.mark.parametrize("action", ["post", "edit"])
def test_full_length_discord_text_reaches_the_desk(tmp_path, action, character):
    args = action_args(action, character * MAX_TEXT)

    async def go():
        async with served(tmp_path) as (app, control):
            # ask uses endpoint.request: the real encoder, TCP client and server.
            assert await ask(control, "discord", args) == {"ok": True, "id": 1}
            assert app.discord.asks == [args]

    asyncio.run(go())


@pytest.mark.parametrize("character", ["a", "é", "漢", "\U0001F600"],
                         ids=["ascii", "accent", "cjk", "astral"])
@pytest.mark.parametrize("action", ["post", "edit"])
def test_longer_discord_text_is_still_refused(tmp_path, action, character):
    args = action_args(action, character * (MAX_TEXT + 1))

    async def go():
        async with served(tmp_path) as (app, control):
            reply = await ask(control, "discord", args)
            assert reply["ok"] is False
            assert reply["error"]
            assert app.discord.asks == []

    asyncio.run(go())
