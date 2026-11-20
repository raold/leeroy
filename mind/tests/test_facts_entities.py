"""Library facts for what a raid or guild turn names ([talk] wow_grounding): entity_question reads
the bosses, raids, places and bracketed items a line names, never a game question (that has its
own lookup) and never a word that only looks like shorthand; LibraryFacts.named takes the best
one or two facts from the pages titled with those names, with no search or model. Invented
chunks only."""
import asyncio

import pytest

from engine_rig import FakeLibrary, chunk
from leeroy_mind.talk.facts import (ENTITY_LINES, ENTITY_TOKENS, LibraryFacts, entity_question,
                                    entity_titles)
from leeroy_mind.text import estimate_tokens


@pytest.mark.parametrize("text, names", [
    ("anubarak was brutal last night", ("anub'arak",)),
    ("icc tonight, bring flasks", ("icecrown citadel",)),
    ("finally got [Signet of Testing] lol", ("Signet of Testing",)),
    ("putri is a nightmare", ("professor putricide",)),
])
def test_what_a_line_names(text, names):
    q = entity_question(text)
    assert q is not None and q.names == names and q.topic == "lore"


@pytest.mark.parametrize("text", [
    "lol nice", "hol up", "this boss is pos", "back up the dbs", "",
    "what does anubarak drop?",  # his game question: its own lookup has the facts
])
def test_nothing_named_or_a_question(text):
    assert entity_question(text) is None


def test_a_raid_boss_also_asks_for_its_raids_tactics_page():
    titles = entity_titles(entity_question("anubarak was brutal last night"))
    assert "anub'arak" in titles and "anub'arak (trial of the crusader tactics)" in titles


def lookup(facts, text):
    return asyncio.run(facts.named(text))


def test_the_titled_pages_give_one_or_two_facts_tactics_first():
    lib = FakeLibrary(named=[
        chunk("Anub'arak", "Anub'arak was a king of an old empire, long before the raid ever came."),
        chunk("Anub'arak (Trial of the Crusader tactics)", "Anub'arak burrows at intervals and "
              "the raid must kite the spike that chases a marked player across the ice.",
              kind="strategy"),
        chunk("Anub'arak", "What Anub'arak drops: rings and cloaks for the whole raid to share.",
              kind="loot")])
    facts = LibraryFacts(lib, None)
    lines = lookup(facts, "anubarak was brutal last night")
    assert 1 <= len(lines) <= ENTITY_LINES
    assert "kite the spike" in lines[0]
    assert estimate_tokens("\n".join(lines)) <= ENTITY_TOKENS
    assert not lib.searches and not lib.text_searches  # titles only: no search, no model
    names, kinds = lib.titled_calls[0]
    assert "anub'arak" in names and "strategy" in kinds


def test_one_lookup_per_text_and_none_for_unnamed_text():
    lib = FakeLibrary(named=[chunk("Icecrown Citadel", "Icecrown Citadel is the raid at the top of "
                                   "the glacier, ten or twenty-five players, with twelve bosses.",
                                   kind="zone")])
    facts = LibraryFacts(lib, None)

    async def go():
        a = await facts.named("icc tonight, bring flasks")
        b = await facts.named("icc tonight, bring flasks")
        c = await facts.named("lol nice")
        return a, b, c
    a, b, c = asyncio.run(go())
    assert a == b and a and c == [] and len(lib.titled_calls) == 1


def test_a_failing_library_gives_no_facts():
    class Broken(FakeLibrary):
        def titled(self, names, kinds=None, *, limit=60):
            raise OSError("disk")
    assert lookup(LibraryFacts(Broken(), None), "icc tonight") == []


def test_closed_facts_give_nothing():
    facts = LibraryFacts(FakeLibrary(), None)
    asyncio.run(facts.aclose())
    assert lookup(facts, "icc tonight") == []
