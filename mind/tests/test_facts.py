"""The library in live talk (fix wave part E): opening it read-only, the boss tactics lookup,
the game-question detector, the facts block and the boss mechanics, the capped lookup per
human line, and the prompt budget. Every library here is a small one built from invented
chunks (or a fake): no test reads the real library.db."""
import asyncio
import logging
import sqlite3
import time

import pytest

from conftest import run
from engine_rig import FakeLibrary, chunk
from leeroy_mind.human import HumanIdentity
from leeroy_mind.library.chunks import ChunkDraft, SourceResult
from leeroy_mind.library.core import Library
from leeroy_mind.library.db import latest_version
from leeroy_mind.llm.scheduler import Priority
from leeroy_mind.talk.facts import (FACTS_CAP_S, FACTS_TOKENS, SEARCH_K, LibraryFacts,
                                    boss_mechanics, chunk_faction, clean_text, fact_lines,
                                    for_side, lead_with, relevant, salient)
from leeroy_mind.talk.names import sides_named
from leeroy_mind.talk.questions import game_question
from leeroy_mind.talk.prompt import (MIN_CHAT, MIN_MEMORIES, PromptParts, facts_head,
                                     redact_for, render_messages, trim_to_budget)
from leeroy_mind.text import estimate_tokens

LICENSE = "CC BY-SA 4.0"


class ListSource:
    def __init__(self, name, chunks):
        self.name, self.chunks = name, list(chunks)

    def load(self, library):
        return SourceResult(self.name, list(self.chunks))


def tactics(page: int, title: str, sections: list[tuple[str, str]], *,
            instance: str | None = None) -> list[ChunkDraft]:
    return [ChunkDraft(f"wiki:{page}:{i}", "strategy", title, text, section=section,
                       url=f"https://warcraft.wiki.gg/wiki/{page}", license=LICENSE,
                       instance=instance)
            for i, (section, text) in enumerate(sections)]


TACTICS = [
    *tactics(10, "Lord Grimbone", [("Abilities", "- Bone Whirl: Spins and hits everyone close."),
                                   ("Strategy", "Tanks should stack to soak Bone Whirl.")],
             instance="Icecrown Citadel"),
    *tactics(9, "Frostwing", [("Abilities", "- Frost Burst: Freezes the area. (A lore page.)")]),
    *tactics(11, "Frostwing (tactics)", [("Phase One", "Spread out for Frost Burst.")],
             instance="Icecrown Citadel"),
    *tactics(12, "Skitterak (tactics)", [("Tactics", "Kill the adds in the five-player one.")],
             instance="Azjol-Nerub"),
    *tactics(13, "Skitterak (Trial of the Crusader tactics)",
             [("Strategy", "Stand on the frost patches when he burrows.")],
             instance="Trial of the Crusader"),
    *tactics(14, "Icecrown Airship Battle", [("Strategy", "Board the enemy ship in pairs.")],
             instance="Icecrown Citadel"),
    ChunkDraft("wiki:15:0", "lore", "Lord Grimbone", "A lore page, not tactics.",
               license=LICENSE),
    # review Important 1: a tactics page whose title starts with "The", beside the NPC's page
    *tactics(16, "The Frost King", [("Abilities", "- Cold Grip: Holds a player. (An NPC page.)")],
             instance="Icecrown Citadel"),
    *tactics(17, "The Frost King (tactics)", [("Strategy", "Stand behind him for Cold Grip.")],
             instance="Icecrown Citadel"),
    *tactics(18, "Fall of the Frost King", [("Strategy", "Another page that only ends so.")],
             instance="Icecrown Citadel"),
]


@pytest.fixture
def library_db(tmp_path, clock):
    path = tmp_path / "library.db"
    lib = Library(path, clock=clock)
    lib.build([ListSource("wiki", TACTICS)])
    lib.close()
    return path


# -- the library additions -----------------------------------------------------------------

def test_a_read_only_library_searches_and_never_writes(library_db, clock):
    lib = Library(library_db, clock=clock, readonly=True)
    try:
        assert [c.title for c in lib.search_text("Bone Whirl", k=1)] == ["Lord Grimbone"]
        with pytest.raises(sqlite3.OperationalError):
            lib.set_meta("x", "y")
    finally:
        lib.close()
    lib = Library(library_db, clock=clock)  # still a library the CLI can write
    try:
        assert lib.get_meta("x") is None
    finally:
        lib.close()


def test_a_read_only_open_never_creates_or_migrates_a_library(tmp_path, clock):
    with pytest.raises(sqlite3.OperationalError):
        Library(tmp_path / "absent.db", clock=clock, readonly=True)
    assert not (tmp_path / "absent.db").exists()
    old = tmp_path / "old.db"
    conn = sqlite3.connect(old)
    conn.execute(f"PRAGMA user_version = {latest_version() - 1}")
    conn.close()
    with pytest.raises(RuntimeError, match="schema"):
        Library(old, clock=clock, readonly=True)
    conn = sqlite3.connect(old)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == latest_version() - 1
    conn.close()


def test_strategy_for_finds_the_bosss_own_tactics_page(library_db, clock):
    lib = Library(library_db, clock=clock, readonly=True)
    try:
        page = lib.strategy_for("Lord Grimbone")
        assert [c.section for c in page] == ["Abilities", "Strategy"]  # page order
        assert {c.kind for c in page} == {"strategy"}  # never the lore page of that name
        # the tactics page, not the lore page's ability list that has the bare name
        assert [c.text for c in lib.strategy_for("frostwing")] == ["Spread out for Frost Burst."]
        assert [c.text for c in lib.strategy_for("Frostwing", "Icecrown Citadel")] == [
            "Spread out for Frost Burst."]
        assert lib.strategy_for("the Icecrown Airship Battle")  # "the" dropped
        assert [c.title for c in lib.strategy_for("Airship Battle")] == ["Icecrown Airship Battle"]
        toc = lib.strategy_for("Skitterak", "Trial of the Crusader")
        assert [c.instance for c in toc] == ["Trial of the Crusader"]
        five = lib.strategy_for("Skitterak", "Azjol-Nerub")
        assert [c.instance for c in five] == ["Azjol-Nerub"]
        assert lib.strategy_for("Nobody") == [] and lib.strategy_for("  ") == []
        assert lib.strategy_for("Lord_Grimbone%") == []  # LIKE wildcards are literal
    finally:
        lib.close()


@pytest.mark.parametrize("name", ["the Frost King", "The Frost King", "Frost King"])
def test_strategy_for_finds_a_tactics_page_titled_with_the(library_db, clock, name):
    """Review Important 1: "The Frost King (tactics)" was never found, so the NPC page won."""
    lib = Library(library_db, clock=clock, readonly=True)
    try:
        assert [c.text for c in lib.strategy_for(name, "Icecrown Citadel")] == [
            "Stand behind him for Cold Grip."]
    finally:
        lib.close()


def test_a_rejected_text_query_is_logged_at_debug_without_his_words(library_db, clock, caplog,
                                                                    monkeypatch):
    """Review minor 6: his live lines (whispers too) reach the library's text search."""
    lib = Library(library_db, clock=clock, readonly=True)
    try:
        def broken(sql, params=()):
            raise sqlite3.OperationalError("database disk image is malformed")
        monkeypatch.setattr(lib, "_all", broken)
        with caplog.at_level(logging.DEBUG, logger="leeroy_mind"):
            assert lib._fts('"quillonsecret"', None, 5) == []
        rejected = [r for r in caplog.records if "rejected" in r.getMessage()]
        assert rejected and all(r.levelno == logging.DEBUG for r in rejected)
        assert "quillonsecret" not in caplog.text
    finally:
        lib.close()


def test_titled_finds_the_chunks_a_name_titles_also_with_the_or_a_qualifier(tmp_path, clock):
    path = tmp_path / "titled.db"
    lib = Library(path, clock=clock)
    lib.build([
        ListSource("world", [
            ChunkDraft("zone:1", "zone", "Frost Keep", "Frost Keep is a raid."),
            ChunkDraft("quest:1", "quest", "The Lancer's Trial (Alliance)", "Quest one."),
            ChunkDraft("quest:2", "quest", "The Lancer's Trial (Horde)", "Quest two."),
            ChunkDraft("item:1", "item", "Emblem of Ice", "Emblem of Ice: epic item.")]),
        ListSource("wiki", [
            ChunkDraft("wiki:1:0", "zone", "Frost Keep (instance)", "The raid's layout."),
            ChunkDraft("wiki:1:1", "zone", "Frost Keep (instance)", "The raid's bosses."),
            ChunkDraft("wiki:2:0", "lore", "Frost Keep", "The keep's history."),
            ChunkDraft("wiki:3:0", "zone", "Frost", "A place called Frost."),
            ChunkDraft("wiki:4:0", "item", "Emblem of Ice Rewards", "What emblems buy.")])])
    lib.close()
    lib = Library(path, clock=clock, readonly=True)
    try:
        assert [(c.kind, c.text) for c in lib.titled(["frost keep"], ["zone"])] == [
            ("zone", "Frost Keep is a raid."), ("zone", "The raid's layout."),
            ("zone", "The raid's bosses.")]  # page order, its kinds only
        assert len(lib.titled(["Frost Keep"])) == 4
        assert {c.text for c in lib.titled(["lancer's trial"], ["quest"])} == {
            "Quest one.", "Quest two."}  # "The ... (Alliance)" and "(Horde)"
        assert [c.title for c in lib.titled(["emblem of ice"])] == ["Emblem of Ice"]
        assert [c.title for c in lib.titled(["Frost"])] == ["Frost"]
        assert lib.titled([]) == [] and lib.titled(["%"]) == [] and lib.titled(["  "]) == []
        assert len(lib.titled(["frost keep"], limit=2)) == 2
    finally:
        lib.close()


# -- the facts block -----------------------------------------------------------------------------

QUEST = chunk("The Lancer's Trial",
              "Quest The Lancer's Trial (level 80, requires level 77, Tournament, daily). Starts "
              "with Marshal Brightlance. Ends with Marshal Brightlance. Objective: Joust three "
              "champions at the Lancer Grounds. Needs: 3 x Champion Bested. Rewards: Lancer's "
              "Seal. Text: Long ago, before the war, the knights rode out every morning and the "
              "squires polished the lances until they gleamed like ice in the sun.",
              kind="quest", source="world", section="Tournament")
PLACE = chunk("Lancer Grounds",
              "The Lancer Grounds are a {{tournament}} field east of [[Frost Peak|the Frost Peak]] "
              "in Icecrown, where the Lancer's Trial dailies start, see "
              "https://warcraft.wiki.gg/wiki/Lancer_Grounds for the map. "
              "“The squires” — young riders — train there every day.\n"
              "- Marshal Brightlance\n- Squire Pell\nN.B: This section needs clarification.",
              kind="zone", section="Geography")
LOOT = chunk("Lord Grimbone",
             "What Lord Grimbone drops (Frost Keep, 10-player loot table). Always: 2 x Emblem of "
             "Ice. 2 items from: Grim Edge (251 one-hand axe), Bone Ward (251 shield), Chill "
             "Band (251 finger), Marrow Cloak (251 back), Frost Hood (251 cloth head).",
             kind="loot", source="world", section="10-player")


def test_a_fact_is_one_plain_line_without_urls_markup_or_boilerplate():
    lines = fact_lines([PLACE], "where do the squires train at the lancer grounds")
    assert len(lines) == 1
    line = lines[0]
    assert line.startswith("The Lancer Grounds are a field east of the Frost Peak in Icecrown, "
                           "where the Lancer's Trial dailies start.")
    assert "\n" not in line and line.isascii()
    for junk in ("http", "wiki.gg", "{{", "}}", "[[", "]]", "N.B", "clarification", "for the map"):
        assert junk not in line, junk
    assert '"The squires" - young riders - train there' in line  # folded, not dropped


def test_a_fact_from_the_middle_of_a_page_is_labelled_with_its_page():
    mid = chunk("Lancer Grounds (tactics)", "Completing these dailies pays a Lancer's Seal each.",
                section="Dailies")
    assert fact_lines([mid], "what do the dailies pay") == [
        "Lancer Grounds: Completing these dailies pays a Lancer's Seal each."]


def test_a_quest_keeps_who_starts_it_and_its_objective_but_not_its_story():
    (line,) = fact_lines([QUEST], "where does the lancer's trial daily start")
    assert line.startswith("Quest The Lancer's Trial (level 80")
    assert "Starts with Marshal Brightlance." in line and "Objective: Joust three" in line
    assert "Long ago" not in line and "squires" not in line


def test_the_block_stays_in_its_budget_and_puts_the_best_hit_first():
    many = [QUEST, PLACE, LOOT, chunk("Frost Peak", "Frost Peak is a mountain. " * 40)]
    lines = fact_lines(many, "where does the lancer's trial daily start")
    assert lines[0].startswith("Quest The Lancer's Trial")
    assert estimate_tokens("\n".join(lines)) <= FACTS_TOKENS
    assert len(set(lines)) == len(lines) and all(lines)
    assert fact_lines([], "anything") == []
    assert fact_lines([chunk("Empty", "N.B: see above.")], "anything") == []


def test_a_loot_table_reads_as_its_drops():
    (line,) = fact_lines([LOOT], "what drops from grimbone")
    assert line.startswith("What Lord Grimbone drops (Frost Keep, 10-player loot table).")
    assert "Grim Edge (251 one-hand axe)" in line


def test_a_long_drop_list_is_cut_not_left_out_and_a_boss_gets_two_tables():
    items = ", ".join(f"Grim Item {i} (251 plate legs)" for i in range(30))
    tables = [chunk("Lord Grimbone", f"What Lord Grimbone drops (Frost Keep, {mode} loot table). "
                    f"Always: 2 x Emblem of Ice. 2 items from: {items}.", kind="loot",
                    source="world", section=mode)
              for mode in ("10-player", "25-player", "10-player heroic", "25-player heroic")]
    lines = fact_lines(tables, "what drops from grimbone")
    assert len(lines) == 2 and "10-player loot" in lines[0] and "25-player loot" in lines[1]
    assert all("2 items from 30 or more, among them: Grim Item 0 (251 plate legs), Grim Item 1"
               in x for x in lines)  # the library writes a list in parts of 30
    assert all(x.endswith("(251 plate legs)") for x in lines)  # cut after an item, not in one


def test_a_cut_drop_list_says_how_many_it_holds():
    """Judge B (fix 7): a cut list read as the whole table ("2 items from: A, B, C" of a table of
    eleven). A cut list says its full count, a name with a comma in it counting once, and "or
    more" at 30, the most a part of a library list holds; a list that fits stays as it is."""
    items = ", ".join(["Apolyon, the Soul-Render (264 two-hand axe)",
                       *(f"Grim Item {i} (264 plate legs)" for i in range(10))])
    world = chunk("Lord Grimbone", "What Lord Grimbone drops (Frost Keep, 25-player loot table). "
                  f"Always: 2 x Emblem of Ice. One item from: {items}.", kind="loot",
                  source="world", section="25-player")
    (line,) = fact_lines([world], "what drops from grimbone")
    assert "Always: 2 x Emblem of Ice. One item from 11, among them: Apolyon, the Soul-Render " \
           "(264 two-hand axe), Grim Item 0 (264 plate legs)" in line
    assert line.endswith("(264 plate legs)") and "Grim Item 9" not in line
    for n, count in ((12, "12 items"), (30, "30 or more items")):
        atlas = chunk("Lord Grimbone", "What Lord Grimbone drops (Frost Keep, 10-player loot table "
                      "in AtlasLoot): " + ", ".join(f"Grim Item {i} (251 plate legs)"
                                                     for i in range(n)) + ".",
                      kind="loot", source="atlasloot", section="10-player")
        (line,) = fact_lines([atlas], "what drops from grimbone")
        assert line.startswith("What Lord Grimbone drops (Frost Keep, 10-player loot table in "
                               f"AtlasLoot): {count}, among them: Grim Item 0 (251 plate legs), ")
        assert line.endswith("(251 plate legs)") and f"Grim Item {n - 1}" not in line
    colon = chunk("Lord Grimbone", "What Lord Grimbone drops (Frost Keep: The Old Halls loot table "
                  "in AtlasLoot): " + ", ".join(f"Grim Item {i} (200 plate legs)" for i in range(9))
                  + ".", kind="loot", source="atlasloot")
    (line,) = fact_lines([colon], "what drops from grimbone")
    assert "(Frost Keep: The Old Halls loot table in AtlasLoot): 9 items, among them: " in line
    assert fact_lines([LOOT], "what drops from grimbone")[0].endswith(
        "2 items from: Grim Edge (251 one-hand axe), Bone Ward (251 shield), Chill Band (251 "
        "finger), Marrow Cloak (251 back), Frost Hood (251 cloth head).")


def test_list_scraps_lead_ins_and_tables_are_not_facts():
    page = chunk("Lancer Grounds", "Squire Pell (quest) -or-Squire Dun\n"
                 "The grounds are east of the Frost Peak in Icecrown.\n"
                 "The following quests are given here:\n"
                 "Quest - Type - Players - Reward - Lancer's Trial - daily - 1 - 2 - Seal - x",
                 section="Quests")
    assert fact_lines([page], "where are the lancer grounds quests") == [
        "Lancer Grounds: The grounds are east of the Frost Peak in Icecrown."]


def test_a_macro_or_a_slash_command_is_never_a_fact():
    """Rehearsal 2: the historian read "/tar Blood /cast (Name of ability) /tar Death" out as
    "targets blood, drops slows, targets death". A macro goes with its lead-in ("use this
    macro:") and the line that only explains it; a single emote in a quest stays."""
    page = chunk("Lord Grimbone", "Bone Hounds are immune to crowd control. Classes with a root "
                 "should use this macro whenever the hounds spawn:\n- showtooltip\n/tar Bone /cast "
                 "(Name of ability) /tar Grim This immediately uses the ability on the closest "
                 "Bone Hound and then targets Grimbone.\nRanged should kite the hounds away from "
                 "the healers.", kind="strategy", section="Strategy > Bone Hounds")
    (line,) = fact_lines([page], "whats the trick to grimbone, we keep losing it to the hounds")
    assert line == ("Lord Grimbone: Bone Hounds are immune to crowd control. Ranged should kite "
                    "the hounds away from the healers.")
    assert boss_mechanics([page]) == ["Ranged should kite the hounds away from the healers."]
    split = chunk("Lord Grimbone", "Every caster should bind this macro to a key:\n#showtooltip\n"
                  "/castsequence reset=3 Frost Bolt, Ice Lance\n/startattack\nThis makes the "
                  "rotation one button.\nHealers should stand behind the boss at all times.",
                  kind="strategy", section="Strategy")
    assert boss_mechanics([split]) == ["Healers should stand behind the boss at all times."]
    assert fact_lines([split], "any tips for grimbone") == [
        "Lord Grimbone: Healers should stand behind the boss at all times."]
    only = chunk("Lord Grimbone", "/target Bone Hound\n/cast Frost Nova\n/target Lord Grimbone",
                 kind="strategy", section="Macros")
    assert fact_lines([only], "any tips for grimbone") == []
    before = chunk("Dual Talents", "Action bars change with each spec, so bind it in both. "
                   "/usetalents 2", kind="class")
    assert fact_lines([before], "how does dual spec work") == [
        "Dual Talents: Action bars change with each spec, so bind it in both."]
    slashes = chunk("Lord Grimbone", "Healers alternate Mend (3)/Life Burst (4) and Flame Ward "
                    "(2)/Engulf (5) on the drakes.", kind="strategy", section="Strategy")
    assert fact_lines([slashes], "any tips for grimbone") == [f"Lord Grimbone: {slashes.text}"]
    emote = chunk("The Hound's Due", "Quest The Hound's Due (level 80). Objective: /cheer at the "
                  "Bone Hound before you feed it.", kind="quest", source="world")
    assert "/cheer at the Bone Hound" in fact_lines([emote], "how do i do the hound's due")[0]


def test_the_sentence_that_answers_is_kept_when_not_all_fit():
    long = chunk("Grimbone", "Grimbone was a hero once. " + "He liked long walks. " * 30
                 + "His tomb lies under the Frost Keep in Icecrown.")
    (line,) = fact_lines([long], "where is grimbone's tomb")
    assert line == "Grimbone was a hero once. His tomb lies under the Frost Keep in Icecrown."


# -- the boss mechanics --------------------------------------------------------------------------

GRIMBONE = [
    chunk("Lord Grimbone", "- Bone Whirl: Spins and hits everyone close. (3 sec cast.)\n"
          "- Frost Lance: Impales a random player. (10 sec cooldown.)\n"
          "- Cold Trail: Leaves a line of frost behind.", kind="strategy", section="Abilities"),
    chunk("Lord Grimbone", "Lord Grimbone is a simple fight for a prepared raid. Tanks must "
          "stack on each other to soak Bone Whirl. Ranged should free anyone hit by Frost Lance "
          "right away. They are slow, so this rarely matters. On our first kill we just zerged "
          "him.", kind="strategy", section="Strategy"),
    chunk("Lord Grimbone", "Everyone should move out of Cold Trail as soon as it appears under "
          "them.", kind="strategy", section="Strategy > Movement"),
    chunk("Lord Grimbone", "\"Bone and frost!\" Everyone must hear this line.", kind="strategy",
          section="Quotes"),
]


def test_the_mechanics_are_the_tactics_pages_advice_on_its_abilities():
    lines = boss_mechanics(GRIMBONE)
    assert lines == ["Tanks must stack on each other to soak Bone Whirl.",
                     "Ranged should free anyone hit by Frost Lance right away.",
                     "Everyone should move out of Cold Trail as soon as it appears under them."]
    assert all(line.isascii() and len(line.split()) <= 28 for line in lines)
    assert boss_mechanics(GRIMBONE, n=2) == lines[:2]
    assert boss_mechanics([]) == []
    no_list = [c for c in GRIMBONE if c.section != "Abilities"]
    assert boss_mechanics(no_list)[0] == "Tanks must stack on each other to soak Bone Whirl."
    scraps = [chunk("Lord Grimbone", "Can stack up to 99 times on a single target. Players can "
                    "defeat him but never kill him for good. Because of this, players should "
                    "stand in groups.", kind="strategy", section="Strategy")]
    assert boss_mechanics(scraps) == []  # leans on the sentence before, or no advice at all


# -- the lookup: once per line, capped -----------------------------------------------------------

def question(text="anyone know where the lancer's trial dailies start?"):
    q = game_question(text)
    assert q is not None
    return q


def test_one_lookup_per_line_is_shared_by_every_reply():
    async def main():
        lib = FakeLibrary([QUEST, PLACE])
        facts = LibraryFacts(lib, llm="SCHEDULER")
        facts.start(7, question())
        facts.start(7, question())  # the same line again: still one search
        got = await asyncio.gather(*(facts.for_line(7) for _ in range(3)))
        assert got[0] == got[1] == got[2] and got[0][0].startswith("Quest The Lancer's Trial")
        assert len(lib.searches) == 1 and lib.text_searches == []
        s = lib.searches[0]
        assert s["k"] == SEARCH_K and s["kinds"] == list(question().kinds)
        assert s["llm"] == "SCHEDULER" and s["priority"] is Priority.REPLY_HUMAN
        assert await facts.for_line(8) == []  # a line with no lookup has no facts
    run(main())


@pytest.mark.parametrize("text_fail,expected", [(False, True), (True, False)])
def test_a_slow_library_falls_back_to_text_search_or_nothing_within_the_cap(text_fail,
                                                                            expected):
    async def main():
        lib = FakeLibrary([QUEST], gate=asyncio.Event(), text_fail=text_fail)  # never set
        facts = LibraryFacts(lib, llm=None)
        t0 = time.monotonic()
        facts.start(1, question())
        lines = await facts.for_line(1)
        took = time.monotonic() - t0
        assert took < FACTS_CAP_S + 0.15, took
        assert bool(lines) is expected and len(lib.text_searches) == 1
    run(main())


def test_a_failing_search_falls_back_to_text_search():
    async def main():
        lib = FakeLibrary([QUEST], fail=True)
        facts = LibraryFacts(lib, llm=None)
        facts.start(1, question())
        assert (await facts.for_line(1))[0].startswith("Quest The Lancer's Trial")
        both = FakeLibrary([QUEST], fail=True, text_fail=True)
        facts = LibraryFacts(both, llm=None)
        facts.start(2, question())
        assert await facts.for_line(2) == []
    run(main())


def test_each_lookup_is_logged_once_without_any_library_text(caplog):
    async def main():
        facts = LibraryFacts(FakeLibrary([QUEST, PLACE]), llm=None)
        facts.start(5, question())
        await facts.for_line(5)
        await facts.for_line(5)
    with caplog.at_level(logging.DEBUG, logger="leeroy_mind"):
        run(main())
    found = [r for r in caplog.records if r.getMessage().startswith("facts_lookup:")]
    assert len(found) == 1 and found[0].levelno == logging.DEBUG
    msg = found[0].getMessage()
    assert "line=5" in msg and "topic=quest" in msg and "facts=2" in msg
    for text in ("Lancer", "lancer", "Brightlance", "Icecrown", "squires", "dailies"):
        assert text not in caplog.text, text  # neither the facts nor his line


def test_clearing_cancels_a_pending_lookup():
    async def main():
        facts = LibraryFacts(FakeLibrary([QUEST], gate=asyncio.Event()), llm=None)
        facts.start(3, question())
        await asyncio.sleep(0)
        facts.clear()
        assert await facts.for_line(3) == []
    run(main())


def test_a_bosss_mechanics_are_looked_up_once():
    lib = FakeLibrary(strategy=GRIMBONE)
    facts = LibraryFacts(lib, llm=None)
    first = facts.mechanics("Lord Grimbone", "Frost Keep")
    assert first and facts.mechanics("lord grimbone", "Frost Keep") == first
    assert lib.strategy_calls == [("Lord Grimbone", "Frost Keep")]


def test_an_encounter_without_a_page_of_its_own_takes_its_members_tactics():
    """Review item 2: the Northrend Beasts have no page, their members do; the Twin Val'kyr
    have only lore and loot pages, so no mechanics at all (never mined from lore)."""
    def page(boss, tip):
        return [chunk(boss, tip, kind="strategy", section="Strategy")]
    lib = FakeLibrary(strategy={
        "Gormok the Impaler": page("Gormok the Impaler", "Tanks must swap when Impale stacks."),
        "Acidmaw and Dreadscale": page("Acidmaw and Dreadscale",
                                       "Players with Paralytic Toxin must run to Burning Bile."),
        "Icehowl": page("Icehowl", "Everyone must move out of the way of his Massive Crash.")})
    facts = LibraryFacts(lib, llm=None)
    assert facts.mechanics("the Northrend Beasts", "Trial of the Crusader") == [
        "Tanks must swap when Impale stacks.",
        "Players with Paralytic Toxin must run to Burning Bile.",
        "Everyone must move out of the way of his Massive Crash."]
    assert facts.mechanics("the Twin Val'kyr", "Trial of the Crusader") == []
    assert ("Fjola Lightbane", "Trial of the Crusader") in lib.strategy_calls


def test_a_reply_cancelled_while_its_lookup_is_cancelled_stays_cancelled():
    """Review minor 5: a cancelled lookup means no facts, but never for a cancelled reply."""
    async def main():
        facts = LibraryFacts(FakeLibrary([QUEST], gate=asyncio.Event()), llm=None)
        facts.start(4, question())
        lookup = facts._lines[4]
        reply = asyncio.create_task(facts.for_line(4))
        await asyncio.sleep(0)  # the reply waits on the lookup
        facts.clear()
        while not lookup.cancelled():  # the lookup is gone before the reply hears of it
            await asyncio.sleep(0)
        reply.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reply
    run(main())


def test_out_of_time_after_a_slow_search_means_no_text_search(caplog):
    """Review minor 8: the cap ran out while the hybrid search hung: no facts, no text search."""
    async def main():
        lib = FakeLibrary([QUEST], gate=asyncio.Event())
        times = iter([0.0])
        facts = LibraryFacts(lib, llm=None, timer=lambda: next(times, 1.0))  # then past the cap
        facts.start(6, question())
        assert await facts.for_line(6) == [] and lib.text_searches == []
    with caplog.at_level(logging.DEBUG, logger="leeroy_mind"):
        run(main())
    assert "outcome=timeout+out_of_time" in caplog.text


# -- relevance: better no facts than wrong facts (the controller's live probe) -------------------

ICC = chunk("Icecrown Citadel", "Icecrown Citadel is the fortress of the Lich King in Icecrown. "
            "Its southern entrance faces the Wrathgate.", kind="zone")
VAULT = chunk("Nexus Vault", "The entrance to it was broken off when the vault fell.", kind="zone")
MAIL = chunk("Dalaran", "There are mailboxes outside the inn and by the eastern entrance.",
             kind="zone")


def lookup(lib, line: str) -> list[str]:
    async def main():
        facts = LibraryFacts(lib, llm=None)
        facts.start(1, question(line))
        return await facts.for_line(1)
    return run(main())


def test_raider_shorthand_is_searched_written_out_and_off_topic_hits_are_left_out():
    """The probe: "where is the entrance to icc?" got the Nexus Vault and Dalaran mailboxes."""
    lib = FakeLibrary([VAULT, MAIL, ICC])
    assert lookup(lib, "where is the entrance to icc?") == [ICC.text]
    assert lib.searches[0]["query"] == "where is the entrance to Icecrown Citadel?"
    assert lib.searches[0]["k"] > 4  # more candidates than facts: the gate chooses


def test_no_facts_at_all_when_none_is_on_topic(caplog):
    with caplog.at_level(logging.DEBUG, logger="leeroy_mind"):
        assert lookup(FakeLibrary([VAULT, MAIL]), "where is the entrance to icc?") == []
    assert "hits=2 facts=0" in caplog.text


def test_the_text_fallback_searches_the_written_out_query_too():
    lib = FakeLibrary([ICC], fail=True)
    assert lookup(lib, "anyone know where the icc entrance is?") == [ICC.text]
    assert lib.text_searches[0]["query"] == "anyone know where the Icecrown Citadel entrance is?"


def test_a_chunk_his_line_names_by_title_leads_the_facts():
    """The probe: the Emblem of Frost and Tier 10 chunks existed but were not ranked first."""
    tier = chunk("Tier 10", "Tier 10 is the armor set of Icecrown Citadel; its gloves cost 60 "
                 "Emblems of Frost.", kind="item")
    triumph = chunk("Emblem of Triumph", "Emblem of Triumph buys tier 9 items.", kind="item")
    old_tier = chunk("Tier 7", "The token for the gloves of each set drops from Sartharion.",
                     kind="item")
    lib = FakeLibrary([triumph, old_tier], named=[tier])
    assert lookup(lib, "how many emblems do i need for t10 gloves?") == [tier.text]
    names, kinds = lib.titled_calls[0]
    assert "tier 10" in names and kinds == ["loot", "item"]


def test_without_a_name_a_fact_needs_two_of_his_three_words():
    """The probe: "where do i get the frost wyrm mount from?" got a quest and a ram mount."""
    wyrm = chunk("Deadly Gladiator's Frost Wyrm", "Deadly Gladiator's Frost Wyrm: epic mount. "
                 "This is an extremely fast mount.", kind="item", source="world")
    ram = chunk("Swift Gray Ram", "Swift Gray Ram: epic mount.", kind="item", source="world")
    gloves = chunk("Frostwoven Gloves", "Frostwoven Gloves: cloth hands.", kind="item",
                   source="world")
    assert lookup(FakeLibrary([ram, gloves, wyrm]),
                  "where do i get the frost wyrm mount from?") == [wyrm.text]


def test_a_trainer_question_without_a_name_gets_only_facts_about_training():
    """Re-review 4 (minor N5): "where can i learn to fish?" got a pet fish and a staff for its one
    word, "where do i find the weapon master?" an axe that drops from a War Master and the quest
    A Lost Master."""
    trainers = chunk("Patch 3.1.0", "You can learn the Artisan skill from the trainers in the "
                     "capital cities. The time needed to catch fish has been reduced.",
                     section="Professions")
    pet = chunk("Pet Fish", "Pet Fish: common companion pet. Binds when used.", kind="item",
                source="world")
    staff = chunk("Nat Pagle's Fish Terminator", "Nat Pagle's Fish Terminator: epic two-hand "
                  "staff, item level 65. Reward from a fishing contest.", kind="item",
                  source="world")
    assert question("where can i learn to fish?").topic == "trainer"
    (fact,) = lookup(FakeLibrary([pet, staff, trainers]), "where can i learn to fish?")
    assert fact.startswith("Patch 3.1.0: ") and "catch fish" in fact
    axe = chunk("Flightblade Throwing Axe", "Flightblade Throwing Axe: rare thrown weapon, item "
                "level 60. Drops from: War Master Voone (Blackrock Spire, normal).", kind="item",
                source="world")
    lost = chunk("A Lost Master", "Quest A Lost Master (level 20, requires level 10, Darkshore). "
                 "Starts with Terenthis. Ends with Volcor.", kind="quest", source="world")
    masters = chunk("Weapon master", "A weapon master trains the weapon skills a class can learn, "
                    "in every capital city.", section="Overview")
    assert lookup(FakeLibrary([axe, lost, masters]), "where do i find the weapon master?") == [
        masters.text]
    assert lookup(FakeLibrary([axe, lost]), "where do i find the weapon master?") == []


def test_his_words_match_whole_words_only():
    """The re-review (minor 4): "fire" matched "firework" and "tank" "tankard"; a plural or a
    possessive is the same word."""
    def passes(text: str, *words: str) -> bool:
        return relevant(chunk("Some page", text), text, (), set(words))
    assert not passes("A firework lights up the night.", "fire")
    assert not passes("A tankard of ale for the road.", "tank")
    assert not passes("Reins of the Icebound Frostbrood Vanquisher: epic mount.", "frost", "wyrm")
    assert passes("Stand in the fire and you die.", "fire")
    assert passes("Both tanks taunt in turn.", "tank") and passes("The tank's threat.", "tank")


def test_a_fact_never_passes_on_a_generic_adjective_alone():
    """The re-review (minor 4): "Swift Zhevra: ... the fastest ground mount" passed for "what's
    the fastest way to level fishing?" on "fastest"."""
    zhevra = chunk("Swift Zhevra", "Swift Zhevra: epic mount. This is the fastest ground mount a "
                   "recruited friend can get.", kind="item", source="world")
    pole = chunk("Fishing Pole", "Fishing Pole: a pole for fishing, sold by the fishing "
                 "trainers.", kind="item", source="world")
    assert lookup(FakeLibrary([zhevra, pole]), "what's the fastest way to level fishing?") == [
        pole.text]
    assert {"fastest", "best", "easiest", "cheapest"}.isdisjoint(
        salient("what's the fastest, best, easiest and cheapest way to level fishing?"))


# A boss, a raid, people and places as the library writes them, for questions that type them
# another way (the re-review's gate.py).
BQ = chunk("Blood-Queen Lana'thel (tactics)", "Blood-Queen Lana'thel bites a random player at "
           "the start. Players with Pact of the Darkfallen must run together to the same spot.",
           kind="strategy", section="Strategy")
BQ_LOOT = chunk("Blood-Queen Lana'thel", "What Blood-Queen Lana'thel drops (Icecrown Citadel, "
                "10-player loot table). Always: 2 x Emblem of Frost. 2 items from: Grim Edge "
                "(251 one-hand axe), Bone Ward (251 shield).", kind="loot", source="world")
MAL = chunk("Mal'Ganis", "Mal'Ganis was a dreadlord who led the Scourge in Stratholme and "
            "tempted Arthas to Northrend.")
NER = chunk("Ner'zhul", "Ner'zhul was the elder shaman of the Shadowmoon clan who became the "
            "first Lich King.")
CHAPEL = chunk("Light's Hope Chapel", "Light's Hope Chapel is in the Eastern Plaguelands, at the "
               "eastern edge of the zone. The Argent Dawn keeps it.", kind="zone")
HALLOW = chunk("Hallow's End", "Hallow's End dailies start with the innkeepers, who ask you to "
               "put out the Headless Horseman's fires.", kind="event")
TITAN = chunk("Titan", "A titan is one of the godlike beings who shaped Azeroth. The Keepers of "
              "Ulduar were made by them.")
HEIR = chunk("Heirloom", "Heirloom items are bought with Emblems of Heroism from Enchanter Erodin "
             "and Krom Stoutarm in Dalaran.", kind="item")


@pytest.mark.parametrize("line,hit", [
    ("any tips for lanathel?", BQ), ("what does lanathel drop?", BQ_LOOT),
    ("who was malganis?", MAL), ("who was nerzhul?", NER),
    ("where is lights hope chapel?", CHAPEL), ("how do i do the hallows end dailies?", HALLOW),
    ("who were the titans?", TITAN), ("where do i buy heirlooms?", HEIR),
])
def test_a_name_typed_another_way_still_gets_its_facts(line, hit):
    """The re-review (Important 1): the gate dropped the right answer for a name typed without
    its apostrophe or as a plural ("any tips for bq?" had it, "any tips for lanathel?" not)."""
    assert lookup(FakeLibrary([hit]), line) == [hit.text]


def test_a_possessive_names_the_name():
    """The re-review (Important 1): "Lord Marrowgar's Bone Storm" did not name Lord Marrowgar."""
    for text, name in (("Lord Marrowgar's Bone Storm hits everyone.", "lord marrowgar"),
                       ("Marrowgar's Bone Spike impales a player.", "marrowgar"),
                       ("The Northrend Beasts' last is Icehowl.", "the northrend beasts")):
        assert relevant(chunk("Some page", text), text, [name], set()), text


def test_a_single_name_is_looked_up_as_a_title_singular_too():
    lib = FakeLibrary([])
    lookup(lib, "who are the aspects?")
    assert {"aspects", "aspect"} <= set(lib.titled_calls[0][0])


def test_a_name_is_looked_up_as_a_title_plural_too():
    """Fix round 4 (the re-review's minor 3): names read "oracle" and "faction champion" now, and
    the pages titled "The Oracles" and "Faction Champions" were no longer found by title."""
    oracles = chunk("The Oracles", "The Oracles are a faction of Sholazar Basin.", kind="lore")
    lib = FakeLibrary([], named=[oracles])
    assert lookup(lib, "how do i get exalted with the oracles?") == [oracles.text]
    assert "oracles" in lib.titled_calls[0][0]
    lib = FakeLibrary([])
    lookup(lib, "which faction champion do we kill first?")
    assert "faction champions" in lib.titled_calls[0][0]


def test_with_several_names_a_fact_names_the_most_specific():
    """The re-review (minor 4): "where in dalaran is the cold weather flying trainer?" passed
    every Dalaran fact: a place that only locates the question is not what it asks about, nor
    is the raid a boss is in."""
    portal = chunk("Dalaran", "Dalaran has a portal trainer at the Violet Gate.", kind="zone")
    flying = chunk("Hira Snowdawn", "Hira Snowdawn, the Cold Weather Flying trainer, stands in "
                   "Dalaran.", kind="zone")
    assert lookup(FakeLibrary([portal, flying]),
                  "where in dalaran is the cold weather flying trainer?") == [flying.text]
    raid = chunk("Icecrown Citadel", "Icecrown Citadel holds the Lich King's twelve champions.",
                 kind="zone")
    drops = chunk("Lord Marrowgar", "What Lord Marrowgar drops (Icecrown Citadel, 10-player "
                  "loot table). Always: 2 x Emblem of Frost.", kind="loot", source="world")
    assert lookup(FakeLibrary([raid, drops]), "what does marrowgar drop in icc?") == [drops.text]
    road = chunk("Sholazar Basin", "Sholazar Basin is reached from Dalaran by the road south.",
                 kind="zone")
    city = chunk("Dalaran", "Dalaran floats above Crystalsong Forest.", kind="zone")
    assert lookup(FakeLibrary([road, city]), "how do i get to sholazar basin from dalaran?") == [
        road.text, city.text]  # two places: either


# The guild is Horde (fix round 2): a quest or a place of the Alliance is no fact for it.
ALLY_QUEST = chunk("The Lancer's Oath", "Quest The Lancer's Oath (level 80, requires level 77, "
                   "Tournament, daily, Alliance). Starts with Marshal Brightlance.", kind="quest",
                   source="world")
HORDE_QUEST = chunk("The Lancer's Vow", "Quest The Lancer's Vow (level 80, requires level 77, "
                    "Tournament, daily, Horde). Starts with Warden Ashtusk.", kind="quest",
                    source="world")
ANY_QUEST = chunk("The Lancer's Due", "Quest The Lancer's Due (level 80, Tournament, daily). "
                  "Starts with Squire Pell, for both sides.", kind="quest", source="world")
ALLY_CAMP = chunk("Lancer Grounds", "- Marshal Brightlance - The Lancer's Oath (Alliance) - "
                  "Mastery of the Lance (Alliance) at the lancer dailies.", kind="zone",
                  section="NPCs > Quest NPCs > Alliance")
BOTH_CAMP = chunk("Lancer Grounds", "The lancer dailies are given by (Alliance) Marshal "
                  "Brightlance and (Horde) Warden Ashtusk at the Lancer Grounds.", kind="zone")
ALLY_LORE = chunk("Lancer Lore", "The Alliance lancers ride for the dailies (Alliance) there.")
LANCER = "anyone know where the lancer dailies start?"


def test_a_horde_guild_gets_no_alliance_quests_or_places():
    def facts_for(faction):
        async def main():
            facts = LibraryFacts(FakeLibrary([ALLY_QUEST, HORDE_QUEST, ANY_QUEST, ALLY_CAMP,
                                              BOTH_CAMP, ALLY_LORE]), llm=None,
                                 faction=lambda: faction)
            facts.start(1, question(LANCER))
            return await facts.for_line(1)
        return run(main())
    horde = facts_for("horde")
    assert not any("Marshal Brightlance - The Lancer's Oath" in f or "Lancer's Oath" in f
                   for f in horde)
    assert any("Warden Ashtusk" in f for f in horde) and any("Squire Pell" in f for f in horde)
    assert any("(Horde) Warden Ashtusk" in f for f in horde)  # both sides: kept
    assert any("Alliance lancers" in f for f in horde)  # lore is not a quest or a place
    assert not any("Warden Ashtusk" in f and "Vow" in f for f in facts_for("alliance"))
    assert sum("Quest The Lancer's" in f for f in facts_for(None)) == 3  # unknown: both sides


def test_a_chunks_faction_is_its_quest_tag_its_section_or_its_icons():
    assert chunk_faction(ALLY_QUEST) == "alliance" and chunk_faction(HORDE_QUEST) == "horde"
    assert chunk_faction(ANY_QUEST) is None
    assert chunk_faction(ALLY_CAMP) == "alliance" and chunk_faction(BOTH_CAMP) is None
    flights = chunk("Valgarde", "- (Alliance) Fort Wildervar\n- (Alliance) Westguard Keep\n"
                    "- (Neutral) Unu'pe", kind="zone", section="Travel connections")
    assert chunk_faction(flights) == "alliance"
    told = chunk("Lancer Grounds", "The Alliance and the Horde both joust here.", kind="zone")
    assert chunk_faction(told) is None  # a word in the story is no tag
    # a loot table, a page or a section of one side, of any kind: the probe's Alliance chain
    # for "how do i start the onyxia quest again?"
    table = chunk("The Twin Watchers (Horde)", "What The Twin Watchers (Horde) drops (Frost "
                  "Keep, 10-player loot table in AtlasLoot): Grim Edge.", kind="loot",
                  source="atlasloot")
    chain = chunk("Frost Keep attunement", "To start the quest line, speak with the warden.",
                  section="Obtaining the Frost Key (Alliance)")
    quotes = chunk("Frost Keep", "\"For the Horde!\"", section="Quotes > Horde")
    both = chunk("Frost Keep", "Each side has its own dailies.",
                 section="Quests > Silver Covenant (Alliance) / Sunreavers (Horde) dailies")
    lore = chunk("Frost Keep", "The (Alliance) camp stands by the keep.")
    assert [chunk_faction(c) for c in (table, chain, quotes, both, lore)] == [
        "horde", "alliance", "horde", None, None]  # icons tag only a quest or a place
    assert for_side([table, chain, quotes, both, lore], "horde") == [table, quotes, both, lore]
    assert for_side([table, chain], None) == [table, chain]


def test_a_place_a_neutral_faction_holds_is_no_sides_for_its_icons():
    """Fix round 3 (the re-review's minor 6): one "(Alliance) Garithos' Enforcers" made the
    Violet Hold's inmates the Alliance's, and Light's Breach was the Horde's by its flights."""
    hold = chunk("Violet Gaol", "- Warden Aldren\n- Kirin Tor Protectors\n- (Alliance) Garrison "
                 "Enforcers\n- Jailor Kassan", kind="zone", section="Forces; Inmates")
    breach = chunk("Light's Gap", "Light's Gap is a small encampment belonging to the Argent "
                   "Crusade.\n- (Neutral) Ebon Watch\n- (Horde) Venomspite\n- (Horde) Conquest "
                   "Hold", kind="zone", section="Travel connections > Flight")
    base = chunk("Fordragon Keep", "- Stormwind Captains\n- Fordragon Footmen\n- (Alliance) "
                 "Fleeing Alliance Soldiers", kind="zone", section="NPCs")
    assert [chunk_faction(c) for c in (hold, breach, base)] == [None, None, "alliance"]
    sectioned = chunk("Violet Gaol", "Kirin Tor guards.", kind="zone",
                      section="NPCs > Quest NPCs > Alliance")
    assert chunk_faction(sectioned) == "alliance"  # a section of one side still tags it


def test_a_quest_head_with_a_bracket_inside_still_gives_its_side():
    """Fix round 3 (the re-review's minor 8d): "raid (10)" in the head hid the tag."""
    raid = chunk("Into the Frost Keep", "Quest Into the Frost Keep (level 80, requires level 80, "
                 "Alliance, raid (10)). Starts with Marshal Brightlance.", kind="quest",
                 source="world")
    titled = chunk("The Keep (Old)", "Quest The Keep (Old) (level 80, Horde). Starts with Warden "
                   "Ashtusk.", kind="quest", source="world")
    assert chunk_faction(raid) == "alliance" and chunk_faction(titled) == "horde"


# A faction's tables and quartermasters, by side (the re-review's faction_line.py).
TABLE = chunk("Faction Champions (Alliance)", "What Faction Champions (Alliance) drops (Trial of "
              "the Crusader, 10-player loot table in AtlasLoot): Sandals of the Silver Magus (232 "
              "cloth feet), Gloves of the Silver Assassin (232 leather hands).", kind="loot",
              source="atlasloot")
TABLE_H = chunk("Faction Champions (Horde)", "What Faction Champions (Horde) drops (Trial of the "
                "Crusader, 10-player loot table in AtlasLoot): Sunreaver Magus' Sandals (232 cloth "
                "feet), Sunreaver Assassin's Gloves (232 leather hands).", kind="loot",
                source="atlasloot")
QM = chunk("Argent Tournament Grounds", "- Rillie Spindlenut - Silver Covenant Quartermaster "
           "(Alliance) at the Silver Covenant Pavilion - Vasarin Redmorn.", kind="zone",
           section="NPCs > Quest NPCs > Alliance")
QM_H = chunk("Argent Tournament Grounds", "- Vasarin Redmorn - Sunreaver Quartermaster (Horde) "
             "at the Sunreaver Pavilion.", kind="zone", section="NPCs > Quest NPCs > Horde")


def test_a_side_named_in_his_line_is_the_side_of_its_facts():
    """Fix round 3 (the re-review's minor 7: it misleads): "what do the alliance faction
    champions drop?" got the Horde table. His line's side wins, named or by a faction of one
    side only; both sides named: no filter."""
    def facts_for(line, chunks):
        async def main():
            facts = LibraryFacts(FakeLibrary(chunks), llm=None, faction=lambda: "horde")
            facts.start(1, question(line))
            return await facts.for_line(1)
        return run(main())
    assert facts_for("what do the alliance faction champions drop?", [TABLE, TABLE_H]) == [
        TABLE.text]
    assert facts_for("what do the faction champions drop?", [TABLE, TABLE_H]) == [TABLE_H.text]
    both = facts_for("what do the alliance and horde faction champions drop?", [TABLE, TABLE_H])
    assert both == [TABLE.text, TABLE_H.text]
    assert all("Silver Covenant" in f for f in facts_for(
        "where's the alliance quartermaster at the argent tournament?", [QM, QM_H]))
    assert all("Silver Covenant" in f for f in facts_for(
        "where is the silver covenant quartermaster?", [QM, QM_H]))


def test_ally_is_a_side_and_a_horde_of_adds_is_none():
    """Fix round 4 (the re-review's minor 6a): a Horde guild's "where's the ally quartermaster at
    the argent tournament?" got the Sunreaver one, and an Alliance guild's "the horde of adds"
    filtered to the Horde."""
    assert sides_named("where's the ally quartermaster at the argent tournament?") == {"alliance"}
    assert sides_named("what do the alli faction champs drop?") == {"alliance"}
    assert sides_named("how do we deal with the horde of adds on lich king?") == set()
    assert sides_named("a horde of them, any tips?") == set()
    assert sides_named("she is my ally in this") == set()
    assert sides_named("what does the horde gunship drop?") == {"horde"}

    def facts_for(line, chunks, faction):
        async def main():
            facts = LibraryFacts(FakeLibrary(chunks), llm=None, faction=lambda: faction)
            facts.start(1, question(line))
            return await facts.for_line(1)
        return run(main())
    assert all("Silver Covenant" in f for f in facts_for(
        "where's the ally quartermaster at the argent tournament?", [QM, QM_H], "horde"))
    assert facts_for("what do the ally faction champs drop?", [TABLE, TABLE_H], "horde") == [
        TABLE.text]
    assert facts_for("what do the faction champions drop? a horde of them", [TABLE, TABLE_H],
                     "alliance") == [TABLE.text]


def test_the_raid_a_boss_is_in_does_not_order_its_facts():
    """Fix round 4 (the re-review's minor 6b): "how do we beat marrowgar in icc?" led with the
    raid's overview, ahead of Lord Marrowgar's own tactics; only a place locates a question."""
    own = chunk("Lord Marrowgar", "Lord Marrowgar's tanks should stack to share his Saber Lash.",
                kind="strategy")
    overview = chunk("Icecrown Citadel", "Icecrown Citadel's first boss is Lord Marrowgar, in "
                     "the Lower Spire.", kind="strategy")
    assert lookup(FakeLibrary([own, overview]), "how do we beat marrowgar in icc?") == [
        own.text, overview.text]


def test_the_place_that_locates_his_question_orders_its_facts():
    """Fix round 3 (the re-review's minor 8c): "where in dalaran is the cold weather flying
    trainer?" led with the trainer in Sholazar Basin."""
    vic = chunk("Lakeside Landing", "Lakeside Landing is in Sholazar Basin. Pilot Vic, a Cold "
                "Weather Flying trainer, is found here.", kind="zone")
    hira = chunk("Hira Snowdawn", "Hira Snowdawn, the Cold Weather Flying trainer, stands at the "
                 "landing in Dalaran.", kind="zone")
    assert lookup(FakeLibrary([vic, hira]),
                  "where in dalaran is the cold weather flying trainer?") == [hira.text, vic.text]


def test_superlatives_and_most_never_tie_a_fact():
    """Fix round 3 (the re-review's minor 8b): "toughest", "rarest" and "most" counted."""
    graded = salient("the toughest, rarest, weakest, nicest, biggest, easiest, most and least")
    assert graded == set()
    kept = salient("conquest forest harvest priest wyrmrest tempest")
    assert {"conquest", "forest", "harvest", "priest", "wyrmrest", "tempest"} <= kept


def test_only_a_wiki_title_with_a_slash_is_a_subpage():
    """Re-review nit (a): "/" skipped every title with a slash, a world item's too."""
    sub = chunk("Underdev/Frost Keep Notes", "The Frost Keep is a raid of many frost wyrms and "
                "drakes, with a mount at its end.")
    item = chunk("Frost Wyrm Rider / Reins", "Frost Wyrm Rider / Reins: epic mount. This is an "
                 "extremely fast frost wyrm.", kind="item", source="world")
    assert fact_lines([sub, item], "where do i get the frost wyrm mount from?") == [item.text]


def test_a_url_in_capitals_is_cut_too():
    """Re-review nit (d): the guard before the URL clause pattern was case-sensitive."""
    assert "WIKI" not in clean_text("The grounds are east, see HTTP://WIKI.EXAMPLE/X for a map.")


def test_a_question_is_never_a_fact():
    """The probe: "The Black Knight of Westfall? The Black Knight of Silverpine?" was a fact."""
    page = chunk("Lancer Grounds", "The Black Rider of Westfall? The Black Rider of Silverpine?\n"
                 "The grounds are east of the Frost Peak, past the old lancer camp.", kind="zone")
    assert fact_lines([page], "where are the lancer grounds") == [
        "Lancer Grounds: The grounds are east of the Frost Peak, past the old lancer camp."]


def test_list_scraps_asides_lead_ins_and_swearing_are_not_facts():
    """The probe after the gate: "See this article for ...", "3211 Reputation # To the Rise
    with all Due Haste!", "Kamagua: (wait until after you pick up ...)" and a joke transcript."""
    page = chunk("Lancer Grounds", "See this article for a side-by-side comparison of the "
                 "grounds.\n3211 Reputation # Ride to the Grounds with all due haste!\n"
                 "The Story Thus Far...\nKamagua: (wait until after you speak to the elder "
                 "first.)\nYou lose 50 DKP for not being where the fuck you were told.\n"
                 "The grounds are east of the Frost Peak, past the old lancer camp.", kind="zone")
    assert fact_lines([page], "where are the lancer grounds") == [
        "Lancer Grounds: The grounds are east of the Frost Peak, past the old lancer camp."]


def test_the_sentence_with_his_own_words_beats_one_with_only_the_name():
    """The probe: for "where is the entrance to icc?" the line said what the citadel is."""
    long = chunk("Icecrown Citadel", "Icecrown Citadel is the greatest fortress of the Lich King "
                 "and the last bastion of his Scourge. Located in the frozen wastes of Icecrown, "
                 "the Citadel rises up from the glacier, a cathedral of blades and claws made of "
                 "saronite. The citadel's southern entrance, the Wrathgate, faces the Court of "
                 "Skulls, while its northern entrance faces the Court of Bones.", kind="zone")
    (line,) = fact_lines([long], "where is the entrance to Icecrown Citadel?",
                         names=["icecrown citadel"])
    assert "The citadel's southern entrance, the Wrathgate" in line


def test_a_titled_chunk_that_shares_none_of_his_other_words_does_not_lead():
    """The probe: "how do i start the onyxia quest again?" led with the dragon's biography."""
    bio = chunk("Onyxia", "Onyxia was the daughter of Deathwing and the broodmother of her kind.")
    start = chunk("Onyxia's Lair attunement", "To start the quest line to Onyxia, speak with the "
                  "warden at the keep.", kind="quest")
    q = question("how do i start the onyxia quest again?")
    assert lead_with([bio], [start], q.query, q.names) == [start, bio]
    where = question("where is the ruby sanctum?")  # nothing but the name: the page leads
    sanctum = chunk("Ruby Sanctum", "The Ruby Sanctum lies beneath the temple.", kind="zone")
    assert lead_with([sanctum], [start], where.query, where.names) == [sanctum, start]


def test_a_gear_question_without_a_name_needs_all_his_words():
    """The probe after the gate: "what's a good gem for fire mage?" got old-content fire gems."""
    gem = chunk("Delicate Fire Ruby", "Delicate Fire Ruby: epic gem. Binds when picked up.",
                kind="item", source="world")
    trinket = chunk("Mind Gem", "Mind Gem: epic trinket, item level 76. Classes: Mage.",
                    kind="item", source="world")
    assert lookup(FakeLibrary([gem, trinket]), "what's a good gem for fire mage?") == []
    cloth = chunk("Frostweave Cloth", "Frostweave Cloth: trade goods. Drops from humanoids in "
                  "Northrend.", kind="item", source="world")
    assert lookup(FakeLibrary([cloth]), "where can i buy frostweave?") == [cloth.text]


def test_a_where_question_prefers_where_things_are():
    """The probe: "where do i turn in emblems of frost?" filled up with what emblems buy, and
    the vendors' places were left out."""
    vendors = chunk("Emblem of Ice", "Emblem of Ice was a currency of the Frost Keep. Its armor is "
                    "sold by vendors in Dalaran and inside the Frost Keep.", kind="item")
    buys = [chunk("Emblem of Ice Rewards", f"Emblem of Ice Rewards items: Ice Cape {i} (264 back;"
                  f" 50 Emblem of Ice), Ice Robe {i} (264 cloth chest; 95 Emblem of Ice).",
                  kind="loot", source="atlasloot") for i in range(4)]
    q = question("where do i turn in emblems of ice?")
    led = lead_with([vendors], buys, q.query, ["emblem of ice"])
    assert led[0] is vendors


def test_wiki_subpages_and_near_copies_are_not_facts():
    """The probe: "Underdev/Ulduar Bosstiary" and three gladiators' frost wyrms in a row."""
    sub = chunk("Underdev/Frost Keep Notes", "The Frost Keep is a raid of many frost wyrms "
                "and drakes, with a mount at its end.", kind="lore")
    wyrms = [chunk(f"{rank} Gladiator's Frost Wyrm", f"{rank} Gladiator's Frost Wyrm: epic mount, "
                   "requires level 70. This is an extremely fast mount.", kind="item",
                   source="world") for rank in ("Deadly", "Furious", "Relentless")]
    lines = fact_lines([sub, *wyrms], "where do i get the frost wyrm mount from?")
    assert lines == [wyrms[0].text]


def test_cleaning_a_long_text_without_a_url_is_quick():
    """Review minor 4: the URL clause pattern took 386 ms on 8000 characters of plain words."""
    text = "word " * 2000
    t0 = time.perf_counter()
    assert clean_text(text) == text
    assert time.perf_counter() - t0 < 0.05


# -- the prompt: the facts block and the budget --------------------------------------------------

HUMAN_ID = HumanIdentity(guid=1, character_name="Testmage", nickname="Zan", real_name="Quillon",
                         is_guild_master=True)
FACTS = ["Quest The Lancer's Trial (level 80). Starts with Marshal Brightlance.",
         "The Lancer Grounds are east of the Frost Peak in Icecrown."]


def prompt_parts(**kw) -> PromptParts:
    base = dict(card="You are Tarvo. You play Arkon.", samples=["ok pulling in 5"],
                rules="How you talk here: short.", rel_cards=["Zan (the guild master): close"],
                memories=[f"- M{i}: " + "m" * 190 for i in range(5)], mood="It's evening.",
                chat=[f"[{i}m ago] P{i:02}: " + "x" * 200 for i in range(8, 0, -1)]
                + ["[just now] Zan: where do the lancer's trial dailies start?"],
                own_recent=["ok pulling in 5"], instruction="INSTRUCTION LAST",
                facts=list(FACTS), facts_head=facts_head("him"))
    base.update(kw)
    return PromptParts(**base)


def tokens_of(p: PromptParts) -> int:
    return sum(estimate_tokens(m["content"]) for m in render_messages(p))


def test_the_facts_come_after_the_chat_under_their_instruction():
    user = render_messages(prompt_parts())[1]["content"]
    head = facts_head("him")
    assert head.startswith("Facts you know (use them if they help, in your own words; never "
                           "invent anything beyond them; if they don't cover it, say you're not "
                           "sure or point him to someone who'd know)")
    assert user.index("Chat:") < user.index(head) < user.index(FACTS[0]) < user.index(FACTS[1])
    assert user.index(FACTS[1]) < user.index("Your last lines") and user.endswith(
        "INSTRUCTION LAST")
    assert head not in render_messages(prompt_parts(facts=[]))[1]["content"]


def test_the_budget_keeps_the_facts_and_cuts_older_chat_first():
    p = prompt_parts()
    t = trim_to_budget(p, budget=tokens_of(p) - 40)  # about one old chat line over
    assert t.facts == FACTS and t.memories == p.memories  # chat went, not memories
    assert len(t.chat) == len(p.chat) - 1 and t.chat == p.chat[1:]  # the oldest went
    newest = p.chat[-MIN_CHAT:]
    tight = trim_to_budget(p, budget=tokens_of(prompt_parts(chat=newest)))
    assert tight.facts == FACTS and tight.chat == newest and tight.memories == p.memories
    last = trim_to_budget(p, budget=200)  # the last resort still keeps every fact
    assert last.facts == FACTS and last.chat == p.chat[-1:]
    no_facts = prompt_parts(facts=[])  # as before part E: memories go first
    bare = trim_to_budget(no_facts, budget=tokens_of(prompt_parts(facts=[], chat=newest)))
    assert bare.memories == p.memories[:MIN_MEMORIES]


def test_a_fact_with_his_real_name_in_it_is_redacted_like_everything_else():
    p = prompt_parts(facts=["Quillon Peak is east of the Frost Keep."])
    assert redact_for(p, HUMAN_ID, False).facts == ["Zan Peak is east of the Frost Keep."]
    assert redact_for(p, HUMAN_ID, True).facts == p.facts


def test_with_facts_his_line_is_the_floor_of_the_chat():
    """Review minor 3: for the second answer the newest chat line is the first answer, so the
    last resort kept that and cut his question."""
    his = "[just now] Zan: where do the lancer's trial dailies start?"
    chat = [f"[{i}m ago] P{i:02}: " + "x" * 200 for i in range(6, 0, -1)] + [
        his, "[just now] Arkon: line 1 from me, at the lancer grounds"]
    p = prompt_parts(chat=chat, pinned=6)
    assert trim_to_budget(p, budget=200).chat == [his]
    middle = trim_to_budget(p, budget=tokens_of(p) - 60)
    assert his in middle.chat and len(middle.chat) < len(chat)
    assert middle.chat == chat[len(chat) - len(middle.chat):]  # the oldest went first
    assert middle.chat[middle.pinned] == his
    bare = dict(facts=[], memories=[], own_recent=[], rel_cards=[])  # without facts, as before:
    least = tokens_of(prompt_parts(chat=chat[-1:], **bare))         # the newest line stays
    assert trim_to_budget(prompt_parts(chat=chat, pinned=6, **bare), budget=least).chat == [
        chat[-1]]
