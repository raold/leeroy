"""Item links (talk/items.py): the mind finds a line's item mentions, keeps them as "[Name]" in
every record and sends each one to the bridge as {{item:<id>}}, which native makes a link."""
import asyncio
import logging
import random

import pytest

from conftest import run
from engine_rig import make_rig
from leeroy_mind.library.chunks import ItemRow, SourceResult
from leeroy_mind.library.core import Library
from leeroy_mind.personas.schema import Typing
from leeroy_mind.protocol import (Chat, Hello, LootDrop, LootItem, MAX_TEXT_BYTES,
                                  parse_event, sanitize_say_text)
from leeroy_mind.bridge import AckTimeout
from leeroy_mind.talk.filters import strip_copied
from leeroy_mind.library.chunks import ChunkDraft
from leeroy_mind.talk.items import ItemLinker, defuse, fold, free_name, key, link_bytes
from leeroy_mind.talk.states import EFFECTS, Altered
from leeroy_mind.talk.style import style_lines
from leeroy_mind.talk.turns import Turn, TurnKind
from leeroy_mind.text import strip_wow_markup

HUMAN = 1

ITEMS = [
    (49623, "Shadowmourne", 5, 284, 80),
    (50362, "Deathbringer's Will", 4, 264, 80),
    (50363, "Deathbringer's Will", 4, 277, 80),
    (46017, "Val'anyr, Hammer of Ancient Kings", 5, 245, 80),
    (6948, "Hearthstone", 1, 1, 0),
    (2589, "Linen Cloth", 1, 5, 0),
    (3775, "Mana Potion", 1, 22, 12),
    (90001, "The Key", 1, 1, 0),
    (90002, "Gold Bar", 2, 40, 0),
    (90003, "Cold Snap", 1, 1, 0),
    (90004, "Fight Club", 1, 1, 0),
    (90005, "Arena Points", 1, 1, 0),
    (90006, "Monster - Sword, Long", 1, 1, 0),
    (90007, "Deprecated Frost Ring", 3, 60, 60),
    (90008, "The Black Knight", 1, 1, 0),
    (90009, "Lord Marrowgar", 1, 1, 0),
    (90010, "Raider's Mantle", 4, 300, 85),
    (90011, "Raider's Mantle", 3, 200, 70),
    (90012, "Lightning Rod", 2, 50, 40),
    (43102, "Frozen Orb", 3, 80, 0),
    (90013, "Heavy Boots", 1, 10, 5),
    (90014, "Dark Desire", 1, 1, 0),
    (90015, "Fast Test 1H Axe", 2, 60, 0),
    (90016, "1500 Test sword 63 blue", 3, 63, 0),
]


def rows(items=ITEMS):
    return [dict(id=i, name=n, quality=q, ilvl=il, req_level=rl) for i, n, q, il, rl in items]


def linker(items=ITEMS, now=lambda: 1000.0, *, native=True, places=None):
    """A linker over ITEMS; `native`: the bridge's hello listed item_links (the new native)."""
    lk = ItemLinker(lambda: rows(items), now, (lambda: places) if places is not None else None)
    lk.native = native
    return lk


# -- finding mentions ---------------------------------------------------------------------------

def test_a_bracketed_name_in_any_case_becomes_the_items_own_name():
    lk = linker()
    assert lk.canonical("finally got [shadowmourne] lol", 80) == "finally got [Shadowmourne] lol"
    assert lk.canonical("[ SHADOWMOURNE ]!", 80) == "[Shadowmourne]!"
    assert lk.canonical("[Val'anyr, Hammer of Ancient Kings] dropped",
                        80) == "[Val'anyr, Hammer of Ancient Kings] dropped"
    assert lk.canonical("my [Hearthstone] is on cd", 80) == "my [Hearthstone] is on cd"


def test_an_exact_unbracketed_name_of_two_words_or_more_is_a_mention():
    lk = linker()
    assert lk.canonical("grats on deathbringer's will!", 80) == "grats on [Deathbringer's Will]!"
    assert lk.canonical("Deathbringer’s Will again", 80) == "[Deathbringer's Will] again"
    assert lk.canonical("val'anyr, hammer of ancient kings is mine",
                        80) == "[Val'anyr, Hammer of Ancient Kings] is mine"
    assert lk.canonical("need a frozen orb", 80) == "need a [Frozen Orb]"
    # whole names only: no part of one, no name inside a longer word
    assert lk.canonical("deathbringer's wills everywhere", 80) == "deathbringer's wills everywhere"
    assert lk.canonical("hammer of ancient kings", 80) == "hammer of ancient kings"
    assert lk.canonical("supermana potion", 80) == "supermana potion"


def test_common_words_that_name_items_are_never_linked_without_brackets():
    lk = linker()
    for line in ("the key is patience", "that was the last word", "cold snap on cd",
                 "first rule of fight club", "sold a gold bar", "need arena points",
                 "hearthstone in 5", "shadowmourne when", "need a mana potion",
                 "my heavy boots", "her dark desire"):
        assert lk.canonical(line, 80) == line, line
    assert lk.canonical("20 linen cloth", 80) == "20 [Linen Cloth]"  # "linen" is no chat word
    assert not free_name(key("The Key")) and not free_name(key("Gold Bar"))
    assert not free_name(key("Hearthstone")) and not free_name(key("Arena Points"))
    assert not free_name(key("Mana Potion")) and not free_name(key("Dark Desire"))
    assert free_name(key("Frozen Orb")) and free_name(key("Deathbringer's Will"))
    assert not free_name(key("For the Light!"))  # no word at its end
    # in brackets they are what the member meant
    assert lk.canonical("[the key] and [gold bar]", 80) == "[The Key] and [Gold Bar]"


def test_junk_boss_and_place_names_are_never_items():
    lk = linker()
    assert lk.canonical("[Monster - Sword, Long] [Deprecated Frost Ring]",
                        80) == "Monster - Sword, Long Deprecated Frost Ring"
    assert lk.canonical("the black knight hits hard, [Lord Marrowgar] too",
                        80) == "the black knight hits hard, Lord Marrowgar too"
    assert lk.resolve("The Black Knight", 80) is None and lk.resolve("Shadowmourne", 80).id == 49623
    assert lk.canonical("[Fast Test 1H Axe] or 1500 test sword 63 blue",
                        80) == "Fast Test 1H Axe or 1500 test sword 63 blue"


def test_brackets_that_name_no_item_lose_them():
    lk = linker()
    assert lk.canonical("[lol] that [Epic Sword of Doom] tho", 80) == "lol that Epic Sword of Doom tho"
    assert lk.canonical("ok :] [x]", 80) == "ok :] x"


def test_at_most_two_links_and_brackets_go_first():
    lk = linker()
    assert lk.canonical("[Shadowmourne] [Hearthstone] [Linen Cloth]",
                        80) == "[Shadowmourne] [Hearthstone] Linen Cloth"
    # the bracketed mentions take the slots before an earlier unbracketed one
    assert lk.canonical("frozen orb, then [Shadowmourne] and [Hearthstone]",
                        80) == "frozen orb, then [Shadowmourne] and [Hearthstone]"
    assert lk.canonical("frozen orb and [Shadowmourne], deathbringer's will",
                        80) == "[Frozen Orb] and [Shadowmourne], deathbringer's will"


def test_a_shared_name_is_the_copy_that_dropped_else_usable_then_highest_item_level():
    clock = [1000.0]
    lk = linker(now=lambda: clock[0])
    assert lk.resolve("deathbringer's will", 80).id == 50363  # heroic: the higher item level
    assert lk.resolve("Raider's Mantle", 80).id == 90011  # the level-85 copy is out of reach
    assert lk.resolve("Raider's Mantle", 85).id == 90010
    lk.seen([50362])
    assert lk.resolve("deathbringer's will", 80).id == 50362  # the one that just dropped
    clock[0] += 6 * 3600 + 1
    assert lk.resolve("deathbringer's will", 80).id == 50363


def test_no_item_table_links_nothing_and_keeps_the_brackets(caplog):
    empty = linker(items=[])
    assert empty.canonical("[lol] [Shadowmourne]", 80) == "[lol] [Shadowmourne]"
    assert empty.wire("[Shadowmourne]", 80) == "[Shadowmourne]"

    def broken():
        raise RuntimeError("no such table: item")
    with caplog.at_level(logging.WARNING):
        lk = ItemLinker(broken, lambda: 0.0)
        assert lk.canonical("[Shadowmourne]", 80) == "[Shadowmourne]"
    assert "item_links_unavailable" in caplog.text
    assert ItemLinker.for_library(object(), lambda: 0.0) is None


def test_a_placeholder_the_words_carry_is_defused():
    lk = linker()
    assert defuse("{{item:49623}} {{{x}}}") == "{item:49623}} {x}}}"
    assert lk.canonical("look {{item:49623}}", 80) == "look {item:49623}}"
    assert "{{" not in lk.wire("look {{item:49623}} [Shadowmourne]", 80).replace("{{item:49623}}", "", 1)
    assert lk.wire("{{item:1}}", 80) == "{item:1}}"


# -- the wire form ------------------------------------------------------------------------------

def test_wire_sends_each_bracketed_item_as_its_placeholder():
    lk = linker()
    assert lk.wire("grats on [Deathbringer's Will] and [Shadowmourne]!",
                   80) == "grats on {{item:50363}} and {{item:49623}}!"
    assert lk.wire("[Shadowmourne] [Hearthstone] [Mana Potion]",
                   80) == "{{item:49623}} {{item:6948}} [Mana Potion]"
    assert lk.wire("[not an item] here", 80) == "[not an item] here"


def test_wire_keeps_the_line_within_the_limit_once_native_links_it():
    lk = linker()
    ref = lk.resolve("Shadowmourne", 80)
    assert link_bytes(ref, 80) == len(
        "|cffff8000|Hitem:49623:0:0:0:0:0:0:0:80|h[Shadowmourne]|h|r")
    fits = "a" * (MAX_TEXT_BYTES - link_bytes(ref, 80) - 1) + " [Shadowmourne]"
    assert lk.wire(fits, 80).endswith(" {{item:49623}}")
    over = "a" + fits
    assert lk.wire(over, 80) == over
    two = "b" * 130 + " [Shadowmourne] [Deathbringer's Will]"  # 167 bytes, 257 linked
    assert lk.wire(two, 80) == "b" * 130 + " {{item:49623}} [Deathbringer's Will]"
    assert lk.wire(two[10:], 80) == "b" * 120 + " {{item:49623}} {{item:50363}}"


# -- what the other steps make of it ------------------------------------------------------------

def test_the_bracket_tag_strip_keeps_item_names():
    """Commit 6c86ba1 strips copied "[whisper]" and "[Whisper to Zan]:" tags; an item name in
    brackets, in front of a line or inside it, is no tag."""
    for line in ("[Shadowmourne] finally", "[Deathbringer's Will]: mine", "got [Val'anyr, Hammer of "
                 "Ancient Kings] tonight", "[Linen Cloth] x20"):
        assert strip_copied(line, ["Arkon"], him=["Testmage", "Zan"]) == line
    assert strip_copied("[whisper] [Shadowmourne] for you", ["Arkon"]) == "[Shadowmourne] for you"


def test_typing_never_touches_a_bracketed_item_name():
    typing = Typing(case="lower", typo_rate=1.0, abbreviations={"will": "wil", "ancient": "anc"},
                    end_punct="none", emoticon_rate=0.0, emoticons=(), split_rate=0.0,
                    words_per_line=(3, 30), chars_per_sec=8.0)
    lk = linker()
    plain = lk.canonical("Grats on deathbringer's will and [val'anyr, hammer of ancient kings]", 80)
    lines = style_lines(plain, typing, EFFECTS[Altered.SOBER], random.Random(3))
    joined = " ".join(s.line for s in lines)
    assert "[Deathbringer's Will]" in joined and "[Val'anyr, Hammer of Ancient Kings]" in joined
    assert lk.wire(joined, 80).count("{{item:") == 2


def test_the_library_gives_its_item_rows(tmp_path, clock):
    class World:
        name = "world"

        def load(self, library):
            return SourceResult("world", [], items=[ItemRow(49623, "Shadowmourne", 5, 284, 80, "axe")],
                                log=[])
    lib = Library(tmp_path / "library.db", clock=clock)
    try:
        lib.build([World()])
        lk = ItemLinker.for_library(lib, clock.now)
        assert lk is not None and lk.wire(lk.canonical("[shadowmourne]", 80), 80) == "[Shadowmourne]"
        lk.native = True  # a hello listed item_links
        assert lk.wire(lk.canonical("[shadowmourne]", 80), 80) == "{{item:49623}}"
    finally:
        lib.close()


# -- through the pipeline -----------------------------------------------------------------------

def reply_to_human(guid: int, channel: str = "guild") -> Turn:
    return Turn(kind=TurnKind.REPLY_HUMAN, speaker=guid, channel=channel, addressee=HUMAN)


async def spoken(r, turn: Turn, seconds: float = 40) -> list[str]:
    task = asyncio.create_task(r.engine.speak(turn))
    await r.advance(seconds)
    assert task.done()
    return task.result()


def plain_rig(store, clock, tmp_path, **kw):
    return make_rig(store, clock, tmp_path, items=linker(now=clock.now), **kw)


def test_only_the_bridge_command_carries_the_placeholder(store, clock, tmp_path):
    async def main():
        r = plain_rig(store, clock, tmp_path, reply="gz on [shadowmourne] and deathbringer's will")
        said = await spoken(r, reply_to_human(101))
        assert said and all("{{" not in s for s in said)
        assert any("[Shadowmourne]" in s and "[Deathbringer's Will]" in s for s in said)
        wire = " ".join(c.text for c in r.sent.says())
        assert "{{item:49623}}" in wire and "{{item:50363}}" in wire
        assert "[Shadowmourne]" not in wire
        for row in store.conn.execute("SELECT text FROM chat_line WHERE origin='mind'"):
            assert "{{" not in row["text"] and "[Shadowmourne]" in row["text"]
        for u in r.utterances():
            assert "{{" not in u["text"]
        assert "[Shadowmourne]" in " ".join(u["text"] for u in r.utterances())
        assert all("{{" not in h.row.text for h in r.world.history)
        line_id = store.conn.execute("SELECT max(id) FROM chat_line").fetchone()[0]
        assert "{{" not in (r.engine.said_words(line_id) or "")
    run(main())


def test_a_line_without_item_data_goes_out_as_before(store, clock, tmp_path):
    async def main():
        r = make_rig(store, clock, tmp_path, reply="gz on [shadowmourne]")
        await spoken(r, reply_to_human(101))
        assert all("{{" not in c.text for c in r.sent.says())
        assert any("[shadowmourne]" in c.text for c in r.sent.says())
    run(main())


def test_an_echo_with_the_link_settles_the_line_in_its_plain_form(store, clock, tmp_path):
    def ack(cmd):
        raise AckTimeout("slow")

    async def main():
        r = plain_rig(store, clock, tmp_path, ack=ack, reply="gz on [shadowmourne]")
        assert await spoken(r, reply_to_human(101), seconds=15) == []
        sent = r.sent.says()[0].text
        assert "{{item:49623}}" in sent
        link = "|cffff8000|Hitem:49623:0:0:0:0:0:0:0:80|h[Shadowmourne]|h|r"
        echo = Chat(ts=0, channel="guild", from_guid=101, from_name="Arkon", to_guid=0,
                    text=sent.replace("{{item:49623}}", link), map=0, zone=10, via_bridge=True)
        before = store.conn.execute("SELECT count(*) FROM chat_line").fetchone()[0]
        assert r.engine.is_echo(echo, clock.now())
        rows = store.conn.execute("SELECT text FROM chat_line ORDER BY id").fetchall()
        assert len(rows) == before + 1
        assert rows[-1]["text"] == strip_wow_markup(echo.text) and "|" not in rows[-1]["text"]
        assert "[Shadowmourne]" in rows[-1]["text"]
    run(main())


def test_a_template_links_the_copy_that_just_dropped(store, clock, tmp_path):
    async def main():
        r = plain_rig(store, clock, tmp_path)
        await r.engine.on_event(LootDrop(ts=0, map=631, instance_id=7, source_entry=1, items=(
            LootItem(item_id=50362, name="Deathbringer's Will", quality=4, index=0),)), clock.now())
        task = asyncio.create_task(r.engine.say_as(101, "guild", "Deathbringer's Will: bids to me",
                                                   kind=TurnKind.CALLOUT))
        await r.advance(30)
        assert task.result()
        assert r.sent.says()[-1].text.startswith("{{item:50362}}")
        assert "[Deathbringer's Will]" in r.utterances()[-1]["text"]
    run(main())


def test_a_transport_barrier_line_is_sent_exactly_but_defused(store, clock, tmp_path):
    async def main():
        r = plain_rig(store, clock, tmp_path)
        task = asyncio.create_task(r.engine.say_as(101, "guild", "[shadowmourne] {{item:6948}}",
                                                   kind=TurnKind.CALLOUT,
                                                   before_transport=lambda: True))
        await r.advance(30)
        assert task.result()
        assert r.sent.says()[-1].text == "[shadowmourne] {item:6948}}"
    run(main())


# -- round 1: the feature check ------------------------------------------------------------------

def test_wire_sends_the_plain_name_until_native_lists_the_feature():
    lk = linker(native=False)
    assert lk.wire("gz on [Shadowmourne]", 80) == "gz on [Shadowmourne]"
    lk.native = True
    assert lk.wire("gz on [Shadowmourne]", 80) == "gz on {{item:49623}}"


def test_hello_features_default_to_none_for_an_older_native():
    old = b'{"v":1,"type":"hello","ts":1,"server":"mod-leeroy","version":"0.1.0","guild_id":21,' \
          b'"human_guid":0}'
    assert parse_event(old).features == ()
    new = old[:-1] + b',"features":["item_links"]}'
    assert parse_event(new).features == ("item_links",)


def test_the_app_turns_links_on_only_for_a_hello_that_lists_them(tmp_path, clock):
    from leeroy_mind.app import MindApp
    from leeroy_mind.config import load_config
    from leeroy_mind.testing.fake_llm import FakeLLM

    async def main():
        item_library(tmp_path / "library.db", clock)
        app = MindApp(load_config(tmp_path / "mind.toml"), tmp_path, llm=FakeLLM(), clock=clock)
        try:
            items = app.engine.items
            assert items is not None and items.native is False
            await app.handle(Hello(ts=1, server="mod-leeroy", version="0.1.0", guild_id=21,
                                   human_guid=0, features=("item_links",)))
            assert items.native is True
            await app._on_connection(False)
            assert items.native is False
            await app.handle(Hello(ts=2, server="mod-leeroy", version="0.1.0", guild_id=21,
                                   human_guid=0))
            assert items.native is False
        finally:
            await app.stop()
    run(main())


def test_the_app_reads_the_item_names_before_the_bridge_connects(tmp_path, clock):
    from leeroy_mind.app import MindApp
    from leeroy_mind.config import load_config
    from leeroy_mind.testing.fake_llm import FakeLLM

    async def main():
        item_library(tmp_path / "library.db", clock)
        app = MindApp(load_config(tmp_path / "mind.toml"), tmp_path, llm=FakeLLM(), clock=clock)
        warm = []

        async def bridge_run():
            warm.append(app.engine.items._names is not None)
            await asyncio.Event().wait()
        app.bridge.run = bridge_run
        task = asyncio.create_task(app.run())
        for _ in range(50):
            await asyncio.sleep(0)
            if warm:
                break
        await app.stop()
        await asyncio.wait({task}, timeout=5.0)
        assert warm == [True]
    run(main())


# -- round 1: places and NPCs ---------------------------------------------------------------------

def test_a_place_npc_or_holiday_name_links_only_in_brackets():
    items = ITEMS + [(90020, "Feast of Winter Veil", 1, 1, 0), (90021, "Lancer Grounds Map", 1, 1, 0)]
    lk = linker(items, places=["The Feast of Winter Veil", "Lancer Grounds Map", "Frozen Orb"])
    assert lk.canonical("the feast of winter veil is on", 80) == "the feast of winter veil is on"
    assert lk.canonical("got a lancer grounds map", 80) == "got a lancer grounds map"
    assert lk.canonical("need a frozen orb", 80) == "need a frozen orb"
    assert lk.canonical("need a [frozen orb]", 80) == "need a [Frozen Orb]"
    assert "Frozen Orb" not in lk.free_names() and "Deathbringer's Will" in lk.free_names()


def test_the_library_names_its_places_npcs_and_holidays(tmp_path, clock):
    class World:
        name = "world"

        def load(self, library):
            return SourceResult("world", [
                ChunkDraft("instance:631", "zone", "Icecrown Citadel", "A raid."),
                ChunkDraft("loot:npc:36612:0", "loot", "Lord Marrowgar", "Loot."),
                ChunkDraft("event:1", "event", "Brewfest", "A holiday.")],
                items=[ItemRow(1, "Brewfest", 1, 1, 0, "")], log=[])
    lib = Library(tmp_path / "library.db", clock=clock)
    try:
        lib.build([World()])
        assert set(lib.not_item_names()) >= {"Icecrown Citadel", "Lord Marrowgar", "Brewfest"}
    finally:
        lib.close()


# -- round 1: no smuggled placeholder, on any path ----------------------------------------------

# Words that read as a placeholder only once something between the "{"s is dropped or folded:
# a pipe or a control (encode_command, native), fullwidth braces, a zero-width space or a
# CJK letter (the typing's ASCII fold).
SMUGGLED = ("{|{item:49623}}", "{\x01{item:49623}}", "\uff5b\uff5bitem:49623\uff5d\uff5d",
            "{\u200b{item:49623}}", "{\u4e2d{item:49623}}", "{{item:49623}}", "{\x7f{item:49623}}",
            "{\ufe5b{item:49623}}")


def native_view(text: str) -> str:
    """What native's sanitizer leaves of the text it gets: no pipe, no control character."""
    return "".join(c for c in text if c != "|" and ord(c) >= 0x20 and ord(c) != 0x7F)


def wire_view(text: str) -> str:
    """A say command's text as native gets it: encode_command's clean, then native's."""
    return native_view(sanitize_say_text(text))


@pytest.mark.parametrize("form", SMUGGLED)
def test_fold_and_wire_never_leave_a_placeholder_of_the_words(form):
    lk = linker()
    for text in (fold(f"wts {form} cheap"), lk.wire(f"wts {form} cheap", 80),
                 lk.wire(lk.canonical(f"wts {form} cheap", 80), 80)):
        assert "{{item:" not in native_view(text), text


async def say_through(r, clock, text, **kw):
    task = asyncio.create_task(r.engine.say_as(101, "guild", text, kind=TurnKind.CALLOUT, **kw))
    await r.advance(30)
    return task.result()


def assert_clean(store, r):
    for c in r.sent.says():
        assert "{{item:" not in wire_view(c.text), c.text
    for row in store.conn.execute("SELECT text FROM chat_line"):
        assert "{{item:" not in row["text"], row["text"]
    for u in r.utterances():
        assert "{{item:" not in u["text"], u["text"]


@pytest.mark.parametrize("path", ["model", "model_no_library", "template", "barrier"])
@pytest.mark.parametrize("form", SMUGGLED)
def test_no_smuggled_placeholder_reaches_the_bridge_or_the_log(store, clock, tmp_path, form, path):
    async def main():
        items = None if path == "model_no_library" else linker(now=clock.now)
        r = make_rig(store, clock, tmp_path, items=items, reply=f"wts {form} cheap")
        if path.startswith("model"):
            await spoken(r, reply_to_human(101))
        else:
            await say_through(r, clock, f"wts {form} cheap",
                              **({"before_transport": lambda: True} if path == "barrier" else {}))
        assert r.sent.says()
        assert_clean(store, r)
    run(main())


def test_the_linked_line_still_matches_its_echo_after_the_fold(store, clock, tmp_path):
    def ack(cmd):
        raise AckTimeout("slow")

    async def main():
        r = plain_rig(store, clock, tmp_path, ack=ack,
                      reply="gz on [shadowmourne] \uff5b\uff5bnot a token")
        assert await spoken(r, reply_to_human(101), seconds=15) == []
        sent = r.sent.says()[0].text
        link = "|cffff8000|Hitem:49623:0:0:0:0:0:0:0:80|h[Shadowmourne]|h|r"
        echo = Chat(ts=0, channel="guild", from_guid=101, from_name="Arkon", to_guid=0,
                    text=sent.replace("{{item:49623}}", link), map=0, zone=10, via_bridge=True)
        before = store.conn.execute("SELECT count(*) FROM chat_line").fetchone()[0]
        assert r.engine.is_echo(echo, clock.now())
        assert store.conn.execute("SELECT count(*) FROM chat_line").fetchone()[0] == before + 1
        assert sent == "gz on {{item:49623}} {not a token"  # the fold's "{{" broken, ours kept
        for row in store.conn.execute("SELECT text FROM chat_line"):
            assert "{{" not in row["text"]
    run(main())


def item_library(path, clock):
    class World:
        name = "world"

        def load(self, library):
            return SourceResult("world", [], items=[ItemRow(49623, "Shadowmourne", 5, 284, 80, "")],
                                log=[])
    lib = Library(path, clock=clock)
    try:
        lib.build([World()])
    finally:
        lib.close()
