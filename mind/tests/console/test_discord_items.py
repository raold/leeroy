"""Culture Part B's item tooltips in the console's Discord view: bracketed names resolved exactly in
library.db's item table, read-only, only with [culture] loot on. Every name is invented."""
import asyncio
from contextlib import closing
from datetime import datetime
import sqlite3

import pytest

from console.test_console_discord import NOW, build, cast, data, eastern_pc, get, served  # noqa: F401
from leeroy_mind.console import discordview, items
from leeroy_mind.discord import store as dstore

LOOT_ON = "[server]\nenabled=true\n[culture]\nenabled=true\nloot=true\n"


def library(data):
    path = data / "library.db"
    with closing(sqlite3.connect(path)) as c:
        c.execute("CREATE TABLE item (id INTEGER PRIMARY KEY, name TEXT NOT NULL, quality INTEGER NOT NULL, "
                  "ilvl INTEGER NOT NULL, req_level INTEGER NOT NULL, slot TEXT NOT NULL DEFAULT '')")
        c.executemany("INSERT INTO item VALUES (?,?,?,?,?,?)", [
            (1, "Invented Blade", 4, 251, 80, "one-hand sword"), (2, "Invented Blade", 4, 264, 80, "one-hand sword"),
            (3, "Invented Ring", 3, 200, 78, "finger"), (4, "Sindragosa", 4, 300, 80, ""),
            (5, "Deprecated Invented Hat", 1, 1, 1, "head"), (6, "Invented Shield of Words", 2, 100, 50, "")])
        # The library's loot chunks say which copy drops where: 1 in 25-player, 2 in 25-player heroic.
        c.execute("CREATE TABLE chunks (id INTEGER PRIMARY KEY, kind TEXT, title TEXT, section TEXT)")
        c.execute("CREATE TABLE chunk_ref (chunk_id INTEGER, ref TEXT)")
        c.executemany("INSERT INTO chunks VALUES (?,?,?,?)",
                      [(1, "loot", "Invented Boss", "25-player"), (2, "loot", "Invented Boss", "25-player heroic")])
        c.executemany("INSERT INTO chunk_ref VALUES (?,?)", [(1, "item:1"), (2, "item:2")])
        c.commit()
    return path


def test_item_tooltips_resolve_exact_names_only(data, cast):
    path = library(data)
    text = ("Won [invented blade]! Also [Invented] and [Sindragosa], [Deprecated Invented Hat], "
            "[Invented Ring], [Invented Shield of Words].")
    dstore.post(cast, channel="general", author=101, origin="mind", text=text, now=NOW)
    gone = dstore.post(cast, channel="general", author=1, origin="human", text="[Invented Ring]", now=NOW + 1)
    dstore.delete(cast, gone, author=1, now=NOW + 2)
    many = " ".join(f"[Invented Ring {i}]" for i in range(9)) + " [Invented Ring]"
    dstore.post(cast, channel="general", author=102, origin="mind", text=many, now=NOW + 3)
    cast.conn.commit()
    view = discordview.discord_view(cast.conn, owner_now=datetime.fromtimestamp(NOW),
                                    trial_rank=4, channel="general", items=path)
    first, deleted, crowded = view["messages"]
    assert first["items"] == [
        {"text": "invented blade", "name": "Invented Blade", "quality": 4, "ilvl": None, "slot": "one-hand sword",
         "ilvls": [251, 264]},
        {"text": "Invented Ring", "name": "Invented Ring", "quality": 3, "ilvl": 200, "slot": "finger"},
        {"text": "Invented Shield of Words", "name": "Invented Shield of Words", "quality": 2, "ilvl": 100, "slot": ""}]
    assert deleted["items"] == []
    assert crowded["items"] == []  # only the first eight bracketed names are looked up
    assert items.bracketed(many)[-1] == "Invented Ring 7"
    plain = discordview.discord_view(cast.conn, owner_now=datetime.fromtimestamp(NOW),
                                     trial_rank=4, channel="general")
    assert all("items" not in m for m in plain["messages"])
    assert items.tooltips(data / "missing.db", ["Invented Ring"]) == {}


@pytest.mark.parametrize("toml,expected", [(LOOT_ON, True), ("[server]\nenabled=true\n[culture]\nenabled=true\n", False),
                                           ("", False)])
def test_the_console_adds_items_only_with_the_loot_switch(data, cast, toml, expected):
    library(data)
    (data / "mind.toml").write_text(toml, encoding="utf-8")
    dstore.post(cast, channel="general", author=101, origin="mind", text="look: [Invented Ring]", now=NOW)
    cast.conn.commit()

    async def go():
        async with served(build(data)) as app:
            return (await get(app, "?channel=general")).json()
    message = asyncio.run(go())["messages"][0]
    assert ("items" in message) is expected
    if expected:
        assert [i["name"] for i in message["items"]] == ["Invented Ring"]
    with closing(sqlite3.connect(data / "library.db")) as c:
        assert c.execute("SELECT count(*) FROM item").fetchone()[0] == 6  # read-only: nothing written


def test_a_shared_name_resolves_to_the_guilds_item_or_its_raids_copy(data, cast):
    path = library(data)
    def blade(**kw):
        return items.tooltips(path, ["Invented Blade"], **kw)["Invented Blade"]["ilvl"]
    assert blade() is None  # nothing says which copy: both levels, never a guess
    assert blade(raid=(25, False)) == 251  # the guild raids 25 normal: never the heroic level
    assert blade(raid=(25, True)) == 264
    assert blade(raid=(25, False), prefer_ids={2}) == 264  # the guild's own receipt names the item
    # through the view: his raid (offline = 0) of 25 normal, and a receipt for another item
    cast.conn.execute("INSERT INTO guild_raid(raid_id,started,ended,raid_key,size,heroic,offline,lead_guid,"
                      "tank_guid,loot_guid,kills,wipes) VALUES (9,?,?,'icc',25,0,0,101,101,102,3,1)",
                      (NOW - 86400, NOW - 80000))
    cast.set_meta("guild:loot:9:abc", '{"item_id": 3, "received": %r, "state": "charged"}' % (NOW - 3600))
    dstore.post(cast, channel="general", author=101, origin="mind", text="[Invented Blade]", now=NOW)
    cast.conn.commit()
    prefer, raid = items.guild_items(cast.conn, NOW)
    assert prefer == {3} and raid == (25, False)
    view = discordview.discord_view(cast.conn, owner_now=datetime.fromtimestamp(NOW),
                                    trial_rank=4, channel="general", items=path)
    assert view["messages"][0]["items"][0]["ilvl"] == 251
