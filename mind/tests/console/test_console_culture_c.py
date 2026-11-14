"""The console's side of culture Part C: a server bot's post names its bot, the guild's own emoji
reach the page only while [culture] emoji is on, and a custom reaction is refused while it is off,
before the mind is asked. A real server on a free port; the mind and its link are fakes."""
import asyncio

from console.test_console_discord import FakeLink, build, cast, data, eastern_pc, get, post, served  # noqa: F401
from leeroy_mind.discord import store as dstore
from leeroy_mind.discord.desk import WORDS

NOW = 1.8e9
ON = "[server]\nenabled = true\n[culture]\nenabled = true\nemoji = true\n"


def test_a_bot_post_names_its_bot_and_nothing_else_gains_a_field(data, cast):
    member = dstore.post(cast, channel="loot-and-dkp", author=101, origin="mind", text="grats", now=NOW)
    bot = dstore.post_bot(cast, channel="loot-and-dkp", bot="loot", text="[Invented Blade] -> Arkon, 47 dkp",
                          now=NOW)
    reply = dstore.post(cast, channel="loot-and-dkp", author=102, origin="mind", text="nice",
                        reply_to=bot, now=NOW)
    cast.conn.commit()

    async def go():
        async with served(build(data)) as app:
            return (await get(app, "?channel=loot-and-dkp")).json()
    v = asyncio.run(go())
    rows = {m["id"]: m for m in v["messages"]}
    assert rows[bot]["bot"] == "loot" and rows[bot]["author"] == 0 and rows[bot]["origin"] == "bot"
    assert "bot" not in rows[member] and "bot" not in rows[reply]
    assert [q.get("bot") for q in v["quotes"] if q["id"] == bot] == ["loot"]  # his reply's quote too
    assert "custom_emoji" not in v


def test_custom_emoji_reach_the_page_only_when_on(data, cast):
    async def go():
        async with served(build(data)) as app:
            return (await get(app)).json()
    assert "custom_emoji" not in asyncio.run(go())
    (data / "mind.toml").write_text(ON)
    assert asyncio.run(go())["custom_emoji"] is True


def test_a_custom_reaction_is_refused_while_off_and_relayed_while_on(data, cast):
    link = FakeLink({"ok": True})

    async def go():
        async with served(build(data, link=link)) as app:
            return await post(app, {"action": "react", "message": 3, "emoji": ":tank:"})
    r = asyncio.run(go())
    assert r.status == 400 and r.json() == {"ok": False, "error": WORDS["not_emoji"]} and link.calls == []
    (data / "mind.toml").write_text(ON)
    assert asyncio.run(go()).status == 200
    assert link.calls == [("discord", {"action": "react", "message": 3, "emoji": ":tank:"})]
