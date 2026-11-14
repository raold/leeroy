"""Culture Part C: the guild's own role and class emoji. One registry in Python and its mirror in
the page, original SVGs, and a shortcode that counts as an emoji only while [culture] emoji is on.
Scratch databases and invented names only."""
import asyncio
import re
from pathlib import Path

import pytest

from conftest import seed_test_cast
from leeroy_mind.culture.config import CultureConfig
from leeroy_mind.discord import custom_emoji, store as dstore
from leeroy_mind.discord.desk import WORDS
from leeroy_mind.discord.text import is_one_emoji
from leeroy_mind.endpoint import BadRequest, discord_args

# The one list both registries are checked against: a change to either without the other fails.
NAMES = ["tank", "healer", "dps", "warrior", "paladin", "hunter", "rogue", "priest", "deathknight",
         "shaman", "mage", "warlock", "druid"]
STATIC = Path(custom_emoji.__file__).parents[1] / "console" / "static"


def js_list(name):
    text = (STATIC / "js" / "comms" / "emoji.mjs").read_text(encoding="utf-8")
    found = re.search(rf"export var {name} = \[([^\]]*)\];", text)
    assert found, name
    return re.findall(r'"([a-z0-9_]+)"', found.group(1))


def test_the_registry_is_one_list_in_python_and_the_page():
    assert list(custom_emoji.NAMES) == NAMES
    assert js_list("CUSTOM_ROLES") + js_list("CUSTOM_CLASSES") == NAMES
    assert custom_emoji.CUSTOM == {f":{n}:" for n in NAMES}
    assert all(custom_emoji.SHORTCODE_RE.fullmatch(c) for c in custom_emoji.CUSTOM)


def test_every_emoji_is_an_original_32_pixel_svg_with_nothing_that_runs():
    for name in NAMES:
        svg = (STATIC / "emoji" / f"{name}.svg").read_text(encoding="ascii")
        assert svg.startswith('<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32" viewBox="0 0 32 32">')
        assert not re.search(r"<script|<image|href|<foreignObject|\bon[a-z]+=|url\(", svg, re.I), name
    assert sorted(p.stem for p in (STATIC / "emoji").glob("*.svg")) == sorted(NAMES)


def test_a_shortcode_counts_only_when_registered_and_the_switch_is_on():
    assert not is_one_emoji(":tank:") and is_one_emoji(":tank:", custom=True)
    for bad in (":nope:", "tank", ":TANK:", ":tank: ", "::", ":tank::healer:", ":a:"):
        assert not is_one_emoji(bad, custom=True), bad
    assert is_one_emoji("\U0001F44D") and is_one_emoji("\U0001F44D", custom=True)
    assert custom_emoji.role_code("melee") == custom_emoji.role_code("ranged") == ":dps:"


def test_the_store_takes_a_custom_reaction_only_when_asked(store):
    seed_test_cast(store)
    mid = dstore.post(store, channel="general", author=101, origin="mind", text="pull at 8", now=1.0)
    with pytest.raises(dstore.DiscordRefused, match="not_emoji"):
        dstore.react(store, mid, member=102, emoji=":tank:", now=2.0)
    assert dstore.react(store, mid, member=102, emoji=":tank:", now=2.0, custom=True)
    with pytest.raises(dstore.DiscordRefused, match="not_emoji"):
        dstore.react(store, mid, member=102, emoji=":nope:", now=2.0, custom=True)
    assert dstore.reactions(store.conn, [mid])[mid] == [{"emoji": ":tank:", "members": [102], "count": 1}]


def test_the_shape_takes_a_registered_shortcode_and_the_desk_takes_it_only_when_on(store, clock):
    from discord.test_discord_desk import desk_app
    seed_test_cast(store)
    assert discord_args({"action": "react", "message": 1, "emoji": ":healer:"})["emoji"] == ":healer:"
    with pytest.raises(BadRequest):
        discord_args({"action": "react", "message": 1, "emoji": ":nope:"})
    mid = dstore.post(store, channel="general", author=101, origin="mind", text="who heals", now=1.0)
    off = desk_app(store, clock)
    reply = asyncio.run(off.discord.handle(discord_args({"action": "react", "message": mid, "emoji": ":healer:"})))
    assert reply == {"ok": False, "error": WORDS["not_emoji"]}
    on = desk_app(store, clock, culture_config=CultureConfig(enabled=True, emoji=True))
    reply = asyncio.run(on.discord.handle(discord_args({"action": "react", "message": mid, "emoji": ":healer:"})))
    assert reply["ok"] is True and dstore.reactions(store.conn, [mid])[mid][0]["emoji"] == ":healer:"
