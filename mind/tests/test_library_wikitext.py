import json
from pathlib import Path

import pytest

from leeroy_mind.library.wiki import page_content
from leeroy_mind.library.wikitext import (
    expand_templates, is_disambiguation, is_era_heading, is_later_era, is_later_heading,
    is_noise_heading, is_period_heading, kept_wikitext, page_is_later_era, wikitext_to_sections)

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "library" / "wiki_page.json")
                     .read_text(encoding="utf-8"))["query"]["pages"][0]


def _text(sections):
    return "\n".join(p for s in sections for p in s.paragraphs)


def test_fixture_page_becomes_clean_sections():
    log: list[str] = []
    secs = wikitext_to_sections(page_content(FIXTURE), log=log)
    labels = [s.label for s in secs]
    assert labels == ["", "Biography > Early days", "Biography > Wrath of the Lich King",
                      "Strategy"]
    lead = secs[0].paragraphs[0]
    assert lead.startswith("Sindragosa was the first consort of Malygos in the blue flight.")
    text = _text(secs)
    for gone in ("Npcbox", "{{", "}}", "[[", "]]", "<ref", "made-up citation", "editor note",
                 "Category:", "'''", "&nbsp;", "wikitable", "\x00"):
        assert gone not in text
    assert "- Spread out when she casts Blistering Cold." in text
    assert "frost dragon in an old tongue" not in text  # "Notes" is trivia now
    assert any("Biography > Cataclysm: later era" in line for line in log)
    assert any("References: off-topic" in line for line in log)
    assert any("Notes: off-topic" in line for line in log)


def test_era_filter_drops_later_sections_and_sentences():
    text = _text(wikitext_to_sections(page_content(FIXTURE)))
    assert "later-era paragraph" not in text and "Nor must this one" not in text
    assert "made easier" not in text and "level 85" not in text
    # the paragraph is cut at its first later-era sentence: what follows has moved on too
    assert "The tank faces her away from the raid." in text
    assert "Players killed her many times" not in text


@pytest.mark.parametrize("sentence", [
    "In Cataclysm, the zone was flooded.",
    "World of Warcraft: Legion added a new questline.",
    "During Legion, Dalaran moved.",
    "The city flew south to the Broken Isles in the Legion's third invasion.",
    "Legion skinning quest: Hides of Legend",
    "the Legion-era Dalaran floats elsewhere",
    "The encounter was retuned in patch 4.0.1.",
    "In 2019, the realm was re-released.",
    "In retail, this boss drops nothing.",
    "Wrath Classic changed the tuning.",
    "Timewalking versions exist.",
    "She later served Warchief Sylvanas.",
    "Players in Mythic+ dungeons meet him.",
    "The Shadowlands await the dead.",
    "Mists of Pandaria brought the pandaren.",
    # the shapes of leaks found in the first build (self-written, not the wiki's sentences)
    "Now Lord Admiral of the Kul Tiran fleet, she once led the Kirin Tor.",
    "After the War of the Thorns, she sailed home.",
    "Following the battle on the Broken Shore, the council let the Horde back in.",
    "The ship hovers over the Broken Shore now.",
    "Sylvanas, a former Warchief, left the Horde.",
    "Ysera, a former Dragon Aspect, guards the Dream.",
    "His flight gave rise to the Iron Horde.",
    "In a duel with Lei Shen he would lose.",
    "She attended the Elemental Unrest meeting.",
    "Two achievements count for the meta (as of 2022).",
    "During the Purge of Dalaran, an archmage summoned images of old foes.",
    "Kalecgos took up the mantle of Aspect of the blue flight.",
    "She met her sister aboard the Vindicaar.",
    "Thrall joined the Horde Council.",
    "A bonfire burns in Azsuna.",
    "Jaina changed after the destruction of Theramore.",
    "In Reforged, the mission gained a new boss.",
    "He appears as a card in Hearthstone.",
    "Dawn of the Aspects retells the ancient war.",
    "Chronicle Volume 1 calls them godlike.",
    "The Nightborne of Suramar made a pact.",
    "His wife Katherine and son Tandred waited at home.",
    "Odyn raised a part of Ulduar into the sky as the Halls of Valor.",
    "Muradin is the High Thane of his clan.",
    "Anduin is the King of Stormwind now.",
    "Only level 90 characters may join the battle.",
    "Prize tickets buy armor for transmogrification.",
    "Argaloth guards the first chamber of Baradin Hold.",
    "The faire moved to Darkmoon Island.",
    # second review: Shadowlands, Legion and Cataclysm shapes the first filter missed
    "During the Scourge resurgence, the prince rose again under Mawsworn kyrian in Icecrown.",
    "In his final moments he saw the kyrian Devos come for his soul.",
    "When she took her worry to the Archon, she was sent away.",
    "The heroes met the Primus in the Runecarver's Oubliette.",
    "He calls on Maldraxxi magic to mend an ally.",
    "The heroes carried the shards to a chamber in Bastion.",
    "When Sylvanas shattered the Veil, the sky over the glacier split open.",
    "Years later she destroyed the Helm of Domination and left the throne empty.",
    "Once the Veil had been shattered (35 ADP), the knights met again.",
    "The town was walled in 28 ADP, after the Cataclysm.",
    "Under the Deathlord, the order gathered its frost wyrms.",
    "Bolvar sent his knights to the Maw of the Damned.",
    "FOR ODYN!",
    "Death knights keep those flight paths even after the level squish.",
    "The class was shown at Gamescom with a starting zone in Mardum.",
    "Their class mount is the Felsaber.",
    "During the War Campaign the darkfallen asked to join the Horde.",
    "During the End Time instance the shrine is lifeless.",
    "As of Dragon Soul, the lake is gone.",
    "Once her sire came back and the Shattering struck, her brother raised her.",
    "A mage helped him open a portal to Skywall.",
    "Two of them stand guard on the Isle of Thunder now.",
    "At Lunarfall or Frostwall, quest givers tell of a ghost ship.",
    "The vendor sells Kun-Lai Kicker and Shado-Pan Brew.",
    "The camp is a vrykul archaeology dig site.",
    "On Classic realms only two vendors appear.",
    "Hero talents unlock for a character at level 71.",
    "The heirloom upgrade tokens cost gold.",
    "Flight points are shared within warbands now.",
    "Their loa Rezan granted the prelates their power.",
    "The taunka descend from the yaungol.",
    "The undying knights served the Necrolord covenant.",
    "The Sunreavers fought for the Sunreaver Onslaught.",
    "- 2016 - February 7 - February 1 - February 14",
    "A new portal came with the 2024 iteration.",
    "- Some Souvenir Quest 2011 onward",
    "Many years later, after the death of Arthas, she saw herself in him.",
    "Under Bolvar's orders, the Ebon Blade obeyed the champions.",
    "The world hid a worldsoul, a dormant titan, from its dragons.",
    "The dragons fought the Primalists in the War of the Scaleborn.",
    "The titans made Sholazar, Un'Goro and the Vale of Eternal Blossoms.",
    "The keepers made the earthen and the mogu.",
    "The spear comes from the Legion archaeology quest.",
    "Random Legion Normal dungeons open at level 10.",
    "The shield is a Legion legendary item.",
    "Star Augur Etraeus watches the stars.",
    "The House of Eyes sent a baroness of the Necrolords.",
    "According to Magni Bronzebeard, the land is full of death.",
    "Much later the Bronzebeard brothers journeyed across the north.",
    "With Archaedas she defeated Therazane the Stonemother.",
    "- Harkor - Zul'Aman, Grizzly Hills, Uldum",
    "- (Horde) - Kalimdor - Northern Barrens - Crossroads",
    "- (Neutral) - Kalimdor - Mount Hyjal - Nordrassil",
    "- (Neutral) - Draenor - Nagrand - Ring of Trials",
    "Many years later Aluneth was bound in a greatstaff.",
    "- Jun Soo Pandaren Brew Vendor",
    "Pandaren cannot finish this quest outside neutral cities.",
    "- Mystical Warscroll",
    "- Boxed Nightwine - Bottle of Voidwine",
    "Players get other account\u2011wide conveniences.",
    "(Cata Heroic) - XP - (MoP) - Money",
    "Chronormu could not choose a form for his Visage Day.",
    "One mount customization can drop from the satchel.",
    "- Ancient Heirloom Armor Casing",
    "Crystal dryads live in the Nexus and on Bonich.",
    # fix pass 3: what the re-review's scan found
    "As of , the prince is 25 years old.",
    "He was eighteen when the Argus Campaign was over.",
    "Killed first, each drake gives Justice Points.",
    "The weekly bonus asks for 140 Valor Points.",
    "The Rise of the Zandalari heroics paid more.",
    "Call to Arms covers the random level-90 heroic.",
    "Run Scarlet Halls in five-player heroic.",
    "The queue opens from the Group Finder.",
    "Queen Ansurek gave her Sureki a new form.",
    "A splicer worked beside the Grand Splicer.",
    "A spider pet is sold in the Tyrhold Reservoir.",
    "Demon hunters start at level 8.",
    "The knights risen by Bolvar Fordragon serve a new king.",
    "A worgen death knight might serve the Forsaken.",
    "In the modern game every spec can fill the role.",
    "There are no clear sub-roles in the current game.",
    "- Worgen Female Mask",
    "As of November 16, 2010, the buff no longer applies.",
    "A weekly quest in Undermine pokes fun at it.",
    "- 5.2.0 heirlooms",
    "Leveling from 85 to 86 raises the heirloom a lot.",
    "Its item level climbs to item level 384.",
    "Each class has a shared class tree and a spec tree.",
    "Prime glyphs were the strongest.",
    "Remove a glyph with Vanishing Powder.",
    "Ancient glyphs came with the Path of the Titans (feature).",
    "Most stats are Haste and Critical Strike, though some specs favor Mastery.",
    "A new generation of death knights joined the order.",
    "No Aspect of Magic was named during the war against Deathwing.",
    "She is the widow of Archmage Rhonin.",
    "Only the first kill on a single Battle.net account can drop it.",
    "The League keeps archaeological dig-sites on Draenor.",
    "The Well of Eternity version of the buff differs.",
    "In Zandalar, the loa's followers turned to druidism.",
    "Modern Kul Tiran druids look to the Drust.",
    "The C'Thrax Kith'ix fled the battle.",
    "The priestess Mereldar founded the empire.",
    "A band of hozen raided the camp.",
    "The nerubians thought the mantid were mindless.",
    "Gilnean druids were called harvest-witches.",
    "Some vrykul crossed the sea and became the Drust.",
])
def test_later_era_sentences(sentence):
    assert is_later_era(sentence)


@pytest.mark.parametrize("sentence", [
    "The Burning Legion invaded Azeroth.",
    "Arthas gathered the 1st Legion at the harbor.",
    "Alexstrasza, leader of the red dragonflight, called the Accord.",
    "The black dragonflight schemed in the Obsidian Sanctum.",
    "Deals 2015 Frost damage to nearby enemies.",
    "Patch 3.3.0 added the raid.",
    "The Legion's nathrezim aided the Lich King.",
    "Released in 2009 with patch 3.0.2.",
    "Illidan sought the Tomb of Sargeras.",
    "The Silver Covenant opposed the Sunreavers.",
    "Before the storm broke, the fleet set sail.",
    "Lady Vashj led Illidan to the Broken Isles, where the tomb of Sargeras lay.",
    "Trag Highmountain held Taunka'le Village against the nerubians.",
    "Malygos, the Aspect of Magic, declared war on all spellcasters.",
    "Kalecgos was the investigator of Malygos before the Nexus War.",
    "Jaina fled her home of Kul Tiras with the survivors.",
    "The eredar first arose on the world of Argus.",
    "Dreadlords raised dead kaldorei among Suramar's ruins in the War of the Ancients.",
    "Keep your Hearthstone in your bags.",
    "After the Lich King's death, Krasus called for help at the Ruby Sanctum.",
    "Bryntroll is an item level 277 axe.",
    "Prince Anduin waited in Stormwind while his father was away.",
    "Eventually, Arthas laid siege to Silvermoon.",
    "Shadowfang Keep was overrun by worgen.",
    # Wrath's own uses of words the later eras reused
    'Calling himself the "Jailer of the Damned," Bolvar kept the Scourge leashed.',
    "Bolvar intervened and named himself the Jailer of the Damned.",
    "After the ascension of Bolvar on the Frozen Throne, Darion saluted the heroes for the "
    "Ebon Blade.",
    "Highlord Bolvar Fordragon led the Alliance at the Wrathgate.",
    "The Fury of the Sunwell patch opened the Isle of Quel'Danas.",
    "Warriors learn Shattering Throw at level 71.",
    "The vrykul sleep while the necrolords wake them.",
    "The 7th Legion held Wintergarde Keep.",
    "Mimiron's mechagnome followers built a new body.",
    "After Neltharion created the Dragon Soul, the flights were broken.",
    "The boss warns that we will fall into the maw of madness.",
    "Gretta the Arbiter lives in Brunnhildar Village.",
    "The Scarlet Bastion fell to the Scourge.",
    "The keep stood as a bastion of the Light.",
    "Dalaran hung over Northrend in 27 ADP, while the war on the Lich King raged.",
    "The Zandalari sent an expedition to chronicle the end of the Drakkari.",
    "Old shoulder enchants will now soulbind items.",
    "The Evoker's Silk Amice drops in Stratholme.",
    "Years later, Arthas and Anub'arak passed through Azjol-Nerub.",
    "Hits for 1800 to 2025 Nature damage each tick.",
    "Brann studied archaeology at the Explorers' League.",
    "Reply-Code Omega starts the re-origination from the Forge of Origination in Uldum.",
    "- (Horde) - Outland - Nagrand - Garadar",
    "The Battle of Mount Hyjal ended the Third War.",
    "The Catacombs lie beneath Stratholme.",
    "Chen Stormstout is a pandaren brewmaster.",
    "Legion Commander Yorik used the phylactery.",
    "Grand Necrolord Antiok rides Thiassi.",
    "The goblins of Undermine light the sky at midsummer.",
    "Macs need OS X version 10.5.7 at least.",
    "A hotfix went live on October 11, 2010.",
    "Talents come one point per level from level 10.",
    "Bosses in heroic dungeons are level 82.",
    "Emblems of Valor drop in Ulduar.",
    "Their camp is by the Path of the Titans road.",
    "Loken worked to undermine the titans' works across Northrend.",
    "Crypt fiends of the Nerubis band live in the Ghostlands.",
    "Honor point (pre-4.0.1)",
    "Members of the Zandalar tribe came to Zul'Drak.",
    "- Apothecary Celestine",
])
def test_period_sentences_stay(sentence):
    assert not is_later_era(sentence)


def test_a_table_with_a_later_era_header_goes_whole():
    """Columns per later expansion cannot be split by row; rows of other tables are judged one by
    one."""
    wt = ("== Rewards ==\n{| class=x\n|-\n! Level !! (WotLK Heroic) Money !! (Cata Heroic) Money\n"
          "|-\n| 80 || 19 gold || 26 gold\n|}\n"
          "== Bonfires ==\n{| class=x\n! Zone !! Region\n|-\n| Dragonblight || Wintergarde Keep\n"
          "|-\n| Northern Barrens || Crossroads\n|}\n"
          "== Plain ==\n{| class=x\n| Deepholm || x\n|-\n| Dun Morogh || Kharanos\n|}\n")
    assert [(s.label, s.paragraphs) for s in wikitext_to_sections(wt)] == [
        ("Bonfires", ["- Zone - Region\n- Dragonblight - Wintergarde Keep"]),
        ("Plain", ["- Dun Morogh - Kharanos"])]


def test_heading_rules():
    assert is_later_heading("Legion") and is_later_heading("World of Warcraft: Dragonflight")
    assert is_later_heading("Mists of Pandaria") and is_later_heading("2019")
    assert is_later_heading("Exploring Azeroth: Northrend") and is_later_heading("New in 2026")
    assert is_later_heading("Elemental Unrest") and is_later_heading("Kul Tiras / Zandalar")
    assert is_later_heading("Dragon Soul") and is_later_heading("Kolramas Digsite")
    assert is_later_heading("After the fall of the Lich King") and is_later_heading("Trading Post")
    assert not is_later_heading("The Burning Legion")
    assert not is_later_heading("Fury of the Sunwell")
    assert not is_later_heading("Wrath of the Lich King") and not is_later_heading("Third War")
    assert is_era_heading("Cataclysm era") and is_era_heading("Legion") and is_era_heading("2019")
    assert is_era_heading("World of Warcraft: Legion") and is_era_heading("The War Within")
    assert not is_era_heading("Dawn of the Aspects (past)") and not is_era_heading("Tides of War")
    assert not is_era_heading("The Burning Legion") and not is_era_heading("Exploring Azeroth")
    assert is_period_heading("Wrath of the Lich King") and is_period_heading("The Nexus War")
    assert is_period_heading("Battle for the Undercity") and is_period_heading("Second War")
    assert is_period_heading("Scourge Invasion") and not is_period_heading("Aftermath")
    assert is_noise_heading("References") and is_noise_heading("In the RPG")
    assert is_noise_heading("Patch changes") and is_noise_heading("Notes and trivia")
    for h in ("Trivia", "Notes", "Personality", "Relationships", "Titles", "Powers and abilities",
              "Appearance", "Legacy", "Notable members"):
        assert is_noise_heading(h), h
    assert not is_noise_heading("Legacy of the Damned") and not is_noise_heading("Abilities")
    assert is_noise_heading("Abilities", frozenset({"abilities"}))


def test_later_sibling_sections_are_cut():
    wt = ("Lead.\n== Biography ==\n=== Third War ===\nOld deeds.\n=== Cataclysm ===\nNew.\n"
          "=== The Siege ===\nAlso new, unmarked.\n=== Later still ===\nMore.\n"
          "== Geography ==\nCold.\n")
    secs = wikitext_to_sections(wt)
    assert [s.label for s in secs] == ["", "Biography > Third War", "Geography"]


def test_a_later_story_set_in_the_past_goes_alone():
    """Wiki biographies run in story order, so a later book told about the past comes first."""
    wt = ("== Biography ==\n=== Dawn of the Aspects (past) ===\nA later book.\n"
          "=== The Winterskorn War ===\nThe aspects fought the vrykul.\n"
          "=== Wrath of the Lich King ===\nThe Nexus War.\n==== The Eye ====\nHe fell.\n"
          "=== Cataclysm ===\nLater.\n=== Unmarked after ===\nAlso later.\n")
    log: list[str] = []
    assert [s.label for s in wikitext_to_sections(wt, log=log)] == [
        "Biography > The Winterskorn War", "Biography > Wrath of the Lich King",
        "Biography > Wrath of the Lich King > The Eye"]
    assert "dropped section Biography > Dawn of the Aspects (past): later era" in log
    assert ("dropped section Biography > Unmarked after: later era (after a later-era sibling)"
            in log)
    seasons = ("== History ==\n=== Season of Discovery ===\nA Classic season.\n"
               "=== The Burning Crusade ===\nOutland.\n=== Wrath of the Lich King ===\nNorth.\n")
    assert [s.label for s in wikitext_to_sections(seasons)] == [
        "History > The Burning Crusade", "History > Wrath of the Lich King"]
    sunwell = ("== Biography ==\n=== Fury of the Sunwell ===\nPatch 2.4.\n"
               "=== Northrend ===\nLater.\n")
    assert len(wikitext_to_sections(sunwell)) == 2


def test_a_later_story_after_wraths_time_cuts_what_follows():
    wt = ("== Biography ==\n=== Wrath of the Lich King ===\nNorthrend.\n"
          "=== Elemental Unrest ===\nAfter the war.\n=== A New Book ===\nUnmarked, still later.\n"
          "== Locations ==\nDalaran.\n")
    assert [s.label for s in wikitext_to_sections(wt)] == [
        "Biography > Wrath of the Lich King", "Locations"]


def test_wrath_and_older_siblings_are_never_cut():
    wt = ("== History ==\n=== Cataclysm ===\nLater.\n=== Wrath of the Lich King ===\nOld.\n"
          "==== Fall of the Lich King ====\nThe end.\n==== Aftermath ====\nQuiet.\n"
          "=== Third War ===\nOlder.\n=== Unmarked ===\nLater again.\n")
    assert [s.label for s in wikitext_to_sections(wt)] == [
        "History > Wrath of the Lich King",
        "History > Wrath of the Lich King > Fall of the Lich King",
        "History > Wrath of the Lich King > Aftermath", "History > Third War"]


def test_a_tag_inside_a_section_drops_its_run():
    wt = ("== Inmates ==\n{{wotlk-section}}\n* Erekem\n{{cata-section}}\n* Argaloth\n"
          "{{legion-section}}\n* Shivermaw\n{{tww-section}}\n* Kereke\n{{wc3-section}}\n"
          "* Furbolg\n{{rpg-section}}\n* An RPG beast\n== Fishing ==\n* An eel\n")
    secs = wikitext_to_sections(wt)
    assert [(s.label, s.paragraphs) for s in secs] == [
        ("Inmates", ["- Erekem", "- Furbolg"]), ("Fishing", ["- An eel"])]
    kept = kept_wikitext("== Bosses ==\n{{wotlk-section}}\n[[A]]\n{{legion-section}}\n[[B]]\n"
                         "{{wotlk-section}}\n[[C]]\n== Next ==\n[[D]]\n")
    assert all(f"[[{x}]]" in kept for x in "ACD") and "[[B]]" not in kept


def test_later_lore_about_old_times_goes_alone():
    wt = ("== Prelude ==\nMalygos began the Nexus War. The world hid a worldsoul, a dormant "
          "titan. The Kirin Tor moved Dalaran to Northrend. In Cataclysm, it changed. Gone.\n"
          "== Ulduar ==\nLoken turned on his brother Thorim. Odyn had shaped the vrykul long "
          "before. He wanted an army. Thorim fled to the Storm Peaks.\n")
    prelude, ulduar = wikitext_to_sections(wt)
    (aside,) = wikitext_to_sections("== Class ==\nThey wear plate. Most use Haste and Critical "
                                    "Strike, though some favor Mastery. Their runes heal them.\n")
    # a later-era aside goes alone; what follows is not part of it
    assert aside.paragraphs == ["They wear plate. Their runes heal them."]
    assert prelude.paragraphs == ["Malygos began the Nexus War. The Kirin Tor moved Dalaran to "
                                  "Northrend."]
    # Legion's Odyn retells the vrykul's past: the sentence goes with the one that carries it on
    # ("He wanted ..."), and the Wrath story stays
    assert ulduar.paragraphs == ["Loken turned on his brother Thorim. Thorim fled to the Storm "
                                 "Peaks."]
    (drust,) = wikitext_to_sections("== Kin ==\nSome sailed off and became the Drust. Humans "
                                    "came back later and settled the Grizzly Hills.\n")
    assert drust.paragraphs == ["Humans came back later and settled the Grizzly Hills."]


def test_kept_wikitext_drops_later_and_off_topic_sections():
    wt = ("Lead [[A]].\n== History ==\n=== Third War ===\n[[B]]\n=== Legion ===\n[[C]]\n"
          "==== Deeper ====\n[[D]]\n=== {{Cata-inline}} Shattering ===\n[[E]]\n"
          "== In the RPG ==\n[[F]]\n== Bosses ==\n[[G]]\n")
    kept = kept_wikitext(wt)
    assert all(f"[[{x}]]" in kept for x in "ABG")
    assert not any(f"[[{x}]]" in kept for x in "CDEF")


def test_era_mixed_sections_are_dropped_whole():
    wt = ("Lead.\n== Personality ==\nOnce kind, later bitter.\n== Relationships ==\n"
          "=== A friend ===\nThey met long ago.\n== Notes and trivia ==\n* A server is named "
          "after him.\n== History ==\nOld deeds.\n")
    assert [s.label for s in wikitext_to_sections(wt)] == ["", "History"]
    custom = wikitext_to_sections("== Abilities ==\n* Strike\n== Quests ==\n* Q\n",
                                  drop_headings=frozenset({"abilities"}))
    assert [s.label for s in custom] == ["Quests"]


def test_a_paragraph_is_cut_at_its_first_later_era_sentence():
    wt = ("== Biography ==\nHe fought at the Wrathgate. In Cataclysm, he went home. He built a "
          "farm there.\n* A list line.\n* {{cata-inline}} A later list line.\n* Another line.\n"
          "Before that he served Tirion. {{Cata-inline}}Then he left.\n")
    (sec,) = wikitext_to_sections(wt)
    assert sec.paragraphs == ["He fought at the Wrathgate.", "- A list line.\n- Another line.",
                              "Before that he served Tirion."]


def test_a_later_era_list_item_takes_its_sub_items_and_a_term_its_items():
    wt = ("== Pets ==\n;Northrend\n*Wolvar Pup\n*Oracle Hatchling\n;Kul Tiras / Zandalar\n"
          "*Saurolisk Scale\n*Crab Shell\n;Outland\n*Elekk Collar\n"
          "== Mounts ==\n*Old Ram\n*{{Legion-inline}} New Wolf\n**Its armored variant\n"
          "*Old Kodo\n")
    pets, mounts = wikitext_to_sections(wt)
    assert pets.paragraphs == ["- Northrend\n- Wolvar Pup\n- Oracle Hatchling\n- Outland\n"
                               "- Elekk Collar"]
    assert mounts.paragraphs == ["- Old Ram\n- Old Kodo"]
    # an off-topic term (another game) takes its items too
    refs = ("== Blizzard references ==\n;''World of Warcraft''\n*Two achievements quote it.\n"
            ";''Hearthstone''\n[[File:Card.jpg|thumb|A card.]]\n*A minion quotes it.\n"
            ";''StarCraft''\n*A cheat code quotes it.\n")
    (sec,) = wikitext_to_sections(refs)
    assert sec.paragraphs == ["- World of Warcraft\n- Two achievements quote it."]


def test_the_lead_keeps_its_opening_and_stops_at_the_later_era():
    wt = ("{{Npcbox|name=Aria}}\n{{Quote|Never forget the harbor.|Aria}}\n"
          "'''Aria''' is the Lord Admiral of the fleet. She is a gifted mage.\n\n"
          "She fought in the Third War and founded a city.\n\n"
          "During the Cataclysm she lost her post. Afterwards she grieved alone.\n\n"
          "Her mother forgave her in the end.\n\n"
          "== Quotes ==\n{{Quote|For the Alliance!|Aria}}\n")
    secs = wikitext_to_sections(wt)
    assert secs[0].paragraphs == ["She is a gifted mage.",
                                  "She fought in the Third War and founded a city."]
    assert secs[1].label == "Quotes" and secs[1].paragraphs == ['"For the Alliance!" - Aria']
    fall = wikitext_to_sections(
        "Bolvar was a paladin.\n\nHe led the Alliance at the Wrathgate. After the death of "
        "Arthas, Bolvar took up the helm. He ruled the dead from then on.\n\nMuch later he "
        "found new allies.\n")
    assert fall[0].paragraphs == ["Bolvar was a paladin.", "He led the Alliance at the Wrathgate. "
                                  "After the death of Arthas, Bolvar took up the helm."]


def test_era_tag_templates():
    wt = ("== One ==\n{{wotlk-section}}\nKept.\n== Two ==\n{{legion-section}}\nDropped.\n"
          "== Three ==\n* Kept item\n* {{legion-inline}} Dropped item\n"
          "== Four ==\n{{hs-section}}\nHearthstone card text.\n")
    secs = wikitext_to_sections(wt)
    assert [s.label for s in secs] == ["One", "Three"]
    assert secs[1].paragraphs == ["- Kept item"]
    # Midnight's own tag is "mn"; WoW: Forever, Shadowlands spelled out, Cataclysm Classic
    more = ("== A ==\n{{mn-section}}\nx\n== B ==\n{{shadowlands-section}}\nx\n"
            "== C ==\n{{forever-section}}\nx\n"
            "== D ==\n{{Classic only-section|cata|patch=5.0.4}}\nx\n"
            "== E ==\n{{Classic only-section|patch=4.0.3a}}\nThe old game had this one.\n"
            "== F ==\n{{hearthstone-section}}\nx\n== G ==\n* {{mn-inline}} x\n* Kept\n")
    assert [(s.label, s.paragraphs) for s in wikitext_to_sections(more)] == [
        ("E", ["The old game had this one."]), ("G", ["- Kept"])]


def test_templates_that_carry_text():
    assert expand_templates("{{Abilities|Frost Breath|Deals frost damage.|notes=Cone.}}") == \
        "Frost Breath: Deals frost damage. (Cone.)"
    assert expand_templates("{{text|yell|You will die!}}") == '"You will die!"'
    assert expand_templates("{{text|say|Tirion|Stand fast.}}") == 'Tirion says: "Stand fast."'
    assert expand_templates("{{NPC|Alliance|Crok Scourgebane|icon=x}}") == "Crok Scourgebane"
    assert expand_templates("{{loot|epic|Shadowmourne}} and {{spell|Ice Tomb}}") == \
        "Shadowmourne and Ice Tomb"
    assert expand_templates("{{Npcbox|name=X|loc=[[Y|Z]]}}tail") == "tail"
    assert expand_templates("{{Patch 4.0.3|note=Changed.}}") == "Patch 4.0.3: Changed."
    assert expand_templates("{{expac|Wrath of the Lich King}}") == "Wrath of the Lich King"
    assert expand_templates("a {{unclosed b\nnext line") == "a \nnext line"


def test_game_names_are_spelled_out_and_later_ones_mark_their_sentence():
    """{{g|df}} and {{expac|Legion}} used to vanish, leaving "As of , he is 25 years old." """
    assert expand_templates("With {{g|wotlk}}, the crater") == \
        "With Wrath of the Lich King, the crater"
    assert expand_templates("{{expac|Classic}}") == "Classic"
    for later in ("{{g|df}}", "{{g|sod}}", "{{g|hs}}", "{{g|tww}}", "{{expac|Legion}}",
                  "{{expac|Mists of Pandaria}}"):
        (sec,) = wikitext_to_sections(f"== A ==\nHe was born. As of {later}, he is 25.\n")
        assert sec.paragraphs == ["He was born."], later
    wt = ("== Life ==\nHe was born in Stormwind. As of {{g|df}}, he is 25 years old.\n\n"
          "With {{g|wotlk}}, a crater took the dome's place. In {{g|sod}}, priests banded "
          "together.\n\nA keep for players of 70-72 (now {{level|keep}}).\n")
    (sec,) = wikitext_to_sections(wt)
    assert sec.paragraphs == ["He was born in Stormwind.",
                              "With Wrath of the Lich King, a crater took the dome's place.",
                              "A keep for players of 70-72."]


def test_the_lead_can_be_dropped():
    wt = "One may swap talents whenever one rests.\n== History ==\nThree trees per class.\n"
    assert [s.label for s in wikitext_to_sections(wt)] == ["", "History"]
    assert [s.label for s in wikitext_to_sections(wt, drop_lead=True)] == ["History"]


def test_page_level_checks():
    assert page_is_later_era(["Category:Added in patch 6.0.2", "Category:Bosses"])
    assert not page_is_later_era(["Category:Added in patch 3.3.0", "Category:Added in patch 7.0.3"])
    assert not page_is_later_era(["Category:Bosses"])
    # Classic re-releases (1.13+, 2.5, 3.4) are later content too; scenarios came with Mists
    assert page_is_later_era(["Category:Added in patch 3.4.1", "Category:Unique to Classic"])
    assert page_is_later_era(["Category:Added in patch 3.4.2"])
    assert page_is_later_era(["Category:Added in patch 1.13.2", "Category:Added in patch 2.5.1"])
    assert page_is_later_era(["Category:Scenarios"])
    assert page_is_later_era(["Category:Unique to Classic"])
    assert not page_is_later_era(["Category:Added in patch 3.3.0", "Category:Added in patch 3.4.0"])
    assert not page_is_later_era(["Category:Added in patch 1.12.1"])
    assert not page_is_later_era(["Category:Added in patch 2.4.3"])
    assert page_is_later_era([], "{{Infobox zone\n|level={{level|legion-max+2}}\n}}\nText.\n"
                                 "== A ==\n")
    assert not page_is_later_era([], "{{Infobox zone\n|level={{level|dragonblight}}\n}}\n"
                                     "== A ==\nOnce {{level|legion-max}} in a section.\n")
    assert is_disambiguation("{{disambig}}\n* [[A]]", [])
    assert is_disambiguation("x", ["Category:Disambiguations"])
    assert not is_disambiguation("Plain page.", ["Category:Bosses"])
