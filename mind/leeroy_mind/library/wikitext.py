"""Wikitext to plain text for the library, stdlib only, and the era filter that keeps the guild in
2009: sections and sentences clearly about Cataclysm or later are dropped.

Templates are expanded by name: a few carry text the library wants (boss abilities, quotes, yells,
item and NPC names), the wiki's era tags ({{legion-section}}, {{cata-inline}}) mark later content,
and everything else (infoboxes, navboxes, icons, citations) is dropped.

Wiki prose runs in time order, and later events often follow an earlier marker sentence with no
marker of their own ("After the fall of the Lich King ... Thrall stepped down." then "With his
victory over the corrupted aspect behind him, ..."). So a paragraph is cut at its first later-era
sentence, and the page's lead (which retells the whole life, up to the present day) is cut for
good at the first later-era sentence after its opening paragraph, or right after it mentions the
fall of the Lich King. Sections that mix every era with no headings to tell them apart
(personality, relationships, titles, trivia) are dropped whole. A sentence that only retells old
events with later lore (Azeroth's worldsoul, the mogu, Odyn) goes with the sentences that carry
it on ("He ..."), and a later-era aside ("some specs favor Mastery") goes alone: what follows
them is still old.

A later-era section also takes its later siblings when its heading names a later expansion, or
when the siblings before it have already reached Wrath's time (biographies run in story order);
a later story set in the past ("Dawn of the Aspects (past)") goes alone. A sibling whose heading
names Wrath or an earlier era is never cut that way."""
import html
import re
from dataclasses import dataclass, field

from leeroy_mind.library.chunks import split_sentences

# -- the era filter ----------------------------------------------------------------------

_EXPANSIONS = ("Cataclysm|Mists of Pandaria|Pandaria|Warlords of Draenor|Battle for Azeroth|"
               "Shadowlands|The War Within|The Last Titan|Worldsoul Saga|Cata|MoP|WoD|BfA|TWW")
# Later stories, systems and places. Fury of the Sunwell is not here: it is patch 2.4.
_LATER_STORIES = (
    "Edge of Night|Tides of War|War Crimes|Wolfheart|Before the Storm|Shadows Rising|Elegy|"
    "A Good War|Chains of Domination|Eternity's End|Embers of Neltharion|"
    "Guardians of the Dream|Seeds of Renewal|Visions of N'Zoth|Rise of Azshara|"
    "Siege of Orgrimmar|Fourth War|War of (?:the )?Thorns|Battle for Lordaeron|"
    "Burning of Teldrassil|Twilight of the Aspects|Rage of the Firelands|Charge of the Aspects|"
    "Hour of Twilight|The Shattering|Trial of Garrosh|Dragon Isles|Zereth Mortis|Khaz Algar|"
    "Dawn of the Aspects|Shadows of the Horde|Elemental Unrest|Purge of Dalaran|"
    "Deaths of Chromie|Death Rising|Blood of Our Fathers|Prophet's Lesson|"
    "Lords of War|The Iron Tide|Son of the Wolf|World of Warcraft: Forever|"
    "Iron Horde|Legion [Ii]nvasions?|Argus Campaign|Rise of the Zandalari|Group Finder|"
    "Dornogal|Hallowfall|Azj-Kahet|Ny'alotha|Mechagon|Revendreth|Maldraxx\\w*|Ardenweald|"
    "Oribos|Korthia|Torghast|Sinfall|Valdrakken|Thaldraszus|Ohn'ahran|Zaralek|"
    "Mythic\\+?|Mythic Keystone|Raid Finder|Looking for Raid|"
    "Dungeon Journal|Encounter Journal|Adventure Guide|Personal loot|Warfronts?|"
    "Island Expeditions?|Heart of Azeroth|Azerite|[Aa]rtifact weapons?|[Oo]rder [Hh]alls?|"
    "[Cc]lass [Hh]alls?|[Aa]llied races?|[Bb]attle pets?|[Pp]et battles?|Brawler's Guild|"
    "Archaeology|Reforging|Void Storage|[Tt]oy [Bb]ox|Wrathion|N'Zoth|Y'Shaarj|Zovaal|"
    "[Tt]he Jailer(?! of the Damned)|"
    "Xal'atath|King Anduin|Warchief Sylvanas|Warchief Garrosh|Warchief Vol'jin|"
    "Wrath Classic|Wrath of the Lich King Classic|WotLK Classic|Burning Crusade Classic|"
    "Cataclysm Classic|WoW Classic|World of Warcraft Classic|Classic Era|"
    "Season of Discovery|Season of Mastery|Remix|Plunderstorm|"
    "Brewfest Banquet|Brewfest Chowdown|Gala of Gifts")
# Places, people, factions and titles that exist only from Cataclysm on (Kul Tiras itself is old;
# its Lord Admiral, Boralus and the Proudmoores' BfA family are not).
_LATER_NAMES = (
    "Broken Shore|Boralus|Tiragarde|Drustvar|Stormsong Valley|Zuldazar|Dazar'alor|Nazmir|"
    "Vol'dun|Nazjatar|Eternal Palace|Stormheim|Azsuna|Val'sharah|"
    "Thalyssra|Elisande|Antorus|Antoran|Krokuun|Mac'Aree|Vindicaar|Mardum|Felsaber|Gamescom|"
    "Tirisgarde|Horde Council|Lei Shen|Lord Admiral|Tandred|Katherine Proudmoore|Ashvane|"
    "Taelia|Cyrus Crestfall|Zekhan|Aggra|Kinndy|Kairoz|Zaela|Blackfuse|Ebyssian|Merithra|"
    "Fyrakk|Amirdrassil|Goldrinn|Grand Alliance|High Thane|Three Hammers|"
    "War Mode|Darkmoon Island|"
    # Cataclysm
    "Deepholm|Vashj'ir|Twilight Highlands|Kezan|Lost Isles|Firelands|Molten Front|Skywall|"
    "End Time|Maw of Shu'ma|Gallywix|Ramkahen|Guardians of Hyjal|Avengers of Hyjal|"
    "Throne of the Tides|Stonecore|Vortex Pinnacle|Halls of Origination|"
    "Gilneas Liberation Front|Zephras Isle|Baradin's Wardens|Hellscream's Reach|"
    "(?:Justice|Valor|Conquest) Points?|[Pp]rime [Gg]lyphs?|Vanishing Powder|"
    "Dust of Disappearance|"
    # the old world as the Cataclysm rebuilt it
    "Northern Barrens|Southern Barrens|Northern Stranglethorn|Cape of Stranglethorn|Bogpaddle|"
    "New Kargath|Dragon's Mouth|Lor'danel|Bilgewater Harbor|Desolation Hold|Fort Triumph|"
    "Silverwind Refuge|Fort Livingston|"
    # Mists of Pandaria
    "Jade Forest|Valley of the Four Winds|Krasarang|Kun-Lai|Townlong Steppes|Dread Wastes|"
    "Timeless Isle|Isle of Thunder|Isle of Giants|Wandering Isle|Shado-Pan|Klaxxi|"
    "Golden Lotus|August Celestials|Order of the Cloud Serpent|Lorewalkers?|Lorewalking|"
    "Taran Zhu|Thunder King|Shrine of (?:Seven Stars|Two Moons)|Grand Commendation|"
    "Commendation of [Tt]he|Dominance Offensive|Operation: Shieldwall|Kirin Tor Offensive|"
    "Sunreaver Onslaught|Scarlet Halls|"
    # Warlords of Draenor
    "Frostfire Ridge|Gorgrond|Talador|Spires of Arak|Tanaan|Ashran|Warspear|Lunarfall|"
    "Frostwall|Yrel|Embaari Village|"
    # Legion
    "Eye of Azshara|Darkheart Thicket|Neltharion's Lair|Court of Stars|Maw of Souls|Legionfall|"
    "Army of the Light|Xe'ra|Argussian Reach|Court of Farondis|Dreamweavers|[Tt]he Deathlord|"
    "Etraeus|Aluneth|Nightwine|Bonich|Shattered Locus|[Ss]hardmaidens?|"
    # Battle for Azeroth
    "War Campaign|Talanji|Rastakhan|Bwonsamdi|G'huun|Mythrax|Honorbound|Proudmoore Admiralty|"
    "Order of Embers|Storm's Wake|Rezan|Tortollan|Champions of Azeroth|Rustbolt|"
    "Uldum Accord|Warscrolls?|Voidwine|Voidblend|Kul Tiran druids?|Thornspeakers?|"
    # Shadowlands (the 9.0 pre-patch reanimated Wrath's bosses as "Mawsworn kyrian" foes)
    "Mawsworn|Maw Walker|the Maw|Devos|the Archon|Kyrestia|the Primus|First Ones|Denathrius|"
    "Winter Queen|(?<!Gretta )the Arbiter|Pelagos|[Ss]hards of Domination|Soulbinds?|"
    "Necrolord [Cc]ovenant|Necrolords? [Cc]ampaign|"
    "House of (?:Eyes|Rituals|Constructs|Plagues?|the Chosen)|"
    # Dragonflight, The War Within, Midnight
    "Forbidden Reach|Azure Span|Waking Shores|Dawn of the Infinite|Raszageth|Iridikron|"
    "Vyranoth|Primal Incarnates?|Iskaara|Maruuk|Valdrakken Accord|Loamm Niffen|Dream Wardens|"
    "Tyrhold|Isle of Dorn|Ringing Deeps|Liberation of Undermine|Siren Isle|Manaforge Omega|"
    "Council of Dornogal|Assembly of the Deeps|Hallowfall Arathi|Severed Threads|"
    "Devouring Host|Vanguard of the Light|Harandar|Voidstorm|Ansurek|Sureki|Grand Splicer|"
    "N'Ruby|"
    # later raids and dungeons, other games
    "Baradin Hold|Tol Barad|Blackwing Descent|Bastion of Twilight|Throne of the Four Winds|"
    "Mogu'shan Vaults|Heart of Fear|Terrace of Endless Spring|Throne of Thunder|Highmaul|"
    "Blackrock Foundry|Trial of Valor|Nighthold|Uldir|Crucible of Storms|Castle Nathria|"
    "Sanctum of Domination|Sepulcher of the First Ones|Vault of the Incarnates|Aberrus|"
    "Nerub-ar Palace|Assault on Violet Hold|"
    "Reforged|Heroes of the Storm|Warcraft Rumble|Diablo III|Overwatch")
# Races, covenants and systems the wiki writes in any case ("Mawsworn kyrian", "Hozen").
# "Trag Highmountain" is a Wrath NPC; the Legion zone and its tauren are not. Necrolords are
# also a Wrath Scourge rank (Waking Halls, Wintergarde Crypt), so only their covenant counts.
_LATER_ANYCASE = (
    "kyrian|venthyr|night fae|dracthyr|evokers?(?!')|vulpera|void elf|void elves|"
    "nightborne|nightfallen|lightforged|(?<!Trag )highmountain|haranir|yaungol|hozen|mantids?|"
    "scourge resurgence|(?:level|item|stat) squish|classic realms?|hero talents?|apex talents?|"
    "timewalking|chromie time|dragonriding|skyriding|transmog|archaeology (?:dig ?)?sites?|"
    "heirloom (?:\\w+ )?(?:upgrades?|casings?|scabbards?)|upgrade tokens?|mount customizations?|"
    "warband(?:-wide| map| bank)|"
    "(?:within|across) (?:your |their )?warbands?|pandaren brews?|"
    "pandaren (?:cannot|can|players?|characters?|death knights?|monks?)|"
    "(?:shared )?class trees?|spec trees?|talent loadouts?")
_LATER_PHRASES = (
    "destruction of Theramore|Theramore's destruction|attack on Theramore|fall of Theramore|"
    "mana bomb|invasion of Gilneas|battle for Gilneas|Garrosh's trial|trial of Garrosh|"
    "alternate Draenor|alternate version of Draenor|war in Draenor|latest invasion|"
    "death of (?:King )?Varian|Varian(?: Wrynn)?'s death|death of Vol'jin|Vol'jin's death|"
    "Korialstrasz's sacrifice|death of Korialstrasz|Korialstrasz's death|"
    "Varok(?: Saurfang)?'s (?:soul|death|spirit)|death of (?:Varok|High Overlord) Saurfang|"
    "former Warchief|former (?:Dragon )?Aspect|current Aspect|new Four Horsemen|"
    "new generation of death knights|war against Deathwing|widow of (?:Archmage )?Rhonin|"
    "(?:single|same|per|each) Battle.net account|archaeological dig-?sites?|"
    "Well of Eternity (?:instances?|dungeons?|version)|except Well of Eternity|"
    "former Highlord of the (?:Knights of the )?Ebon Blade|former leader of the Kirin Tor|"
    "High King of the Alliance|transmogrif\\w*|account-wide|world quests?|"
    "(?:player's|your) garrison|no longer the Lich King|"
    "Bronzebeard brothers (?:journeyed|traveled)|"
    "(?:shatter|destroy|broke|break|sunder)\\w*\\s+(?:the\\s+)?Helm of Domination|"
    "Helm of Domination\\b[^.;]{0,30}\\b(?:was|were|had been)\\s+"
    "(?:shattered|destroyed|broken|sundered)|"
    "(?:shatter|pierc|tore|tear)\\w*\\s+the Veil|"
    "the Veil (?:was|had been) (?:shattered|pierced|torn)|"
    # the wiki speaking of today's game
    "in the (?:modern|current|live) game|in (?:modern|current) (?:WoW|World of Warcraft)|"
    "the current expansion|"
    # playable worgen and goblins (4.0), Legion's death knights, post-squish starting levels
    "worgen death knights?|undead worgen|(?:worgen|goblin) (?:female|male)|"
    "(?:risen|raised) by (?:Highlord )?Bolvar|(?:begins?|starts?) at level 8|"
    "from (?:8[5-9]|9\\d|1[0-2]\\d) to (?:8[6-9]|9\\d|1[0-2]\\d)|"
    "item levels? (?:2(?:8[5-9]|9\\d)|[3-9]\\d\\d)")
_MONTHS = ("January|February|March|April|May|June|July|August|September|October|November|"
           "December")
_LATER = re.compile(
    rf"\b(?:{_EXPANSIONS}|{_LATER_STORIES}|{_LATER_NAMES})\b"
    rf"|\b(?i:{_LATER_ANYCASE})\b"
    rf"|\b(?i:{_LATER_PHRASES})\b"
    r"|\b(?:[Ii]n|[Ff]or|[Ff]rom|[Oo]f) Hearthstone\b"
    r"|\bHearthstone(?:'s)? (?:card|expansion|set|art)"
    r"|\bas of (?:\w+ )?20(?:1[1-9]|[23]\d)\b"
    r"|\b[Aa]s of\s*(?=[,.;:)])|\([Aa]s of\s*\)|\b[Ss]ince\s+(?=,)"   # a date the wiki left out
    r"|(?<!item )\b[Ll]evel[- ](?:8[5-9]|9\d|1[0-2]\d)\b"   # Wrath ends at 80 (bosses at 83)
    r"|World of Warcraft: (?:Legion|Dragonflight|Midnight)\b"
    r"|\b(?:[Ii]n|[Dd]uring|[Ss]ince|[Bb]efore|[Aa]fter|[Uu]ntil|[Bb]y)\s+"
    r"(?:Legion|Dragonflight|Midnight)\b"
    r"(?!\s+(?:of|forces|army|armies|demons?|agents?|invasion|invaders|assault|ships?|"
    r"portals?|warships?|commanders?|lieutenants?|attack|incursion|scouts?|troops))"
    r"|\b(?:Legion|Dragonflight|Midnight) (?:expansion|beta|alpha|launch|pre-?patch|"
    r"Timewalking|Remix|Season|season)\b"
    r"|(?<!Burning )\bLegion (?i:skinning|mining|herbalism|fishing|cooking|archaeology|"
    r"professions?|world quests?|class halls?|campaign|artifacts?|zones?|dungeons?|raids?|"
    r"legendar(?:y|ies)|normal|heroic|mythic)\b"
    r"|\b(?:Draenor - (?:Shadowmoon Valley|Nagrand|Frostfire Ridge|Gorgrond|Talador|Spires of "
    r"Arak|Tanaan Jungle)|Kalimdor - Mount Hyjal)\b"     # table rows of WoD and Cata zones
    r"|\b(?:Cataclysm|Legion|Dragonflight|Shadowlands|Pandaria|Warlords|Mists)-era\b"
    r"|\b(?:[Tt]hird|[Ff]ourth) invasion of the Burning Legion|Legion's (?:third|fourth) invasion"
    r"|\bExploring Azeroth\b"
    r"|\bPath of the Titans \(feature\)"    # Cataclysm's cancelled glyph system, not the road
    r"|\b(?:[Ii]n|[Tt]o|[Ii]nto) Undermine\b"  # the War Within zone; "to undermine" is a verb
    r"|\b(?:[Ii]n|[Tt]o|[Ff]rom) Zandalar\b(?! [Tt]ribe)"  # Battle for Azeroth's continent
    r"|\bthe Shattering\b(?! (?:Throw|Stomp))"                 # the Cataclysm, not the ability
    r"|\b(?:[Aa]s of|[Ss]ince) Dragon Soul\b"                    # the 4.3 raid, not the artifact
    r"|\b(?:in|to|from|into|of|on) Bastion\b(?! of)"            # the kyrian's realm
    r"|\b[Pp]atch(?:es)?\s+(?:[4-9]|[1-9]\d)\.\d+"
    r"|(?<![\d.])(?<!pre-)[4-9]\.[0-3]\.\d{1,2}[a-z]?(?![\d.])"  # "5.2.0 heirlooms", not pre-4.0.1
    # Cataclysm's pre-patch (4.0.1) went live on October 12, 2010
    r"|\b(?:October (?:1[2-9]|2\d|3[01])|(?:November|December) \d{1,2})(?:st|nd|rd|th)?,? 2010\b"
    r"|\b(?:(?:1[2-9]|2\d|3[01]) October|\d{1,2} (?:November|December)),? 2010\b"
    r"|\b(?:[Ll]ate|November|December) 2010\b"
    r"|(?:\b(?:[Ii]n|[Ss]ince|[Dd]uring|[Uu]ntil|[Bb]y|[Ff]rom|[Oo]n|[Ee]arly|[Ll]ate|[Mm]id|"
    rf"{_MONTHS}|BlizzCon)\s+|\()20(?:1[1-9]|[23]\d)\b"
    r"|\b20(?:1[1-9]|[23]\d)-\d\d-\d\d\b"
    rf"|\b20(?:1[1-9]|[23]\d)(?=\s*[-\u2013\u2014]\s*(?:{_MONTHS})\b)"   # a dates table row
    r"|\b20(?:1[1-9]|[23]\d)\s+(?:onwards?|and onwards?|iteration|edition|revamp)\b"
    r"|\b(?:2[89]|[3-9]\d) ADP\b"            # the wiki dates Wrath 27 ADP, the Cataclysm 28 ADP
    r"|\b[Rr]etail\b")
# Lore that later books and expansions told about old times. Such a sentence goes, with the
# sentences that carry it on ("He ..."), but it does not end its paragraph the way a later event
# does. Legion made Odyn and Helya the masters of the vrykul's past (Loken's coup, the Halls of
# Valor), written in any case ("ODYN!"); Chronicle added the dead Pantheon and Galakrond's secret;
# Exploring Azeroth (2023) has the Bronzebeards comment on the old places.
_RETCON = re.compile(r"(?i:\bworld-?souls?\b|\b(?:dormant|nascent|slumbering|unborn) titan\b"
                     r"|\bmogu\b|\bodyn\b|\bhelya\b|\bvalarjar\b|\bhalls of valor\b|\beyir\b"
                     r"|\baccording to (?:Magni|Brann|Muradin) Bronzebeard\b"
                     r"|\bMuradin Bronzebeard (?:believes|thinks|suspects)\b)"
                     r"|\bVale of Eternal Blossoms\b|\bWar of the Scaleborn\b"
                     r"|\bChronicle\b|\bThal'ena\b|\bTherazane\b|\bVisage Days?\b"
                     r"|\bChronormu's visage\b|\btruth about Galakrond\b"
                     r"|\bPantheon (?:was|were|had been) (?:dead|slain|killed)\b"
                     r"|\bKith'ix\b|\bZakajz\b|\bMereldar\b|\bDrust\b"
                     r"|\bharvest-witch(?:es)?\b|(?<!Apothecary )\bCelestine\b")  # Cata's Gilneas
_CARRIED_ON = re.compile(r"^(?:He|She|They|It|His|Her|Their|Its)\b")
# A later-era aside in old text ("though some specs may favor Mastery"): the sentence goes, and
# what follows it is not part of it.
_ASIDE = re.compile(r"\bfavou?r Mastery\b|\b(?:Haste|Critical Strike|Versatility),? (?:and|or) "
                    r"Mastery\b|\bMastery,? (?:and|or) (?:Haste|Critical Strike|Versatility)\b"
                    r"|\bVersatility\b|(?i:\bmastery (?:ability|rating)\b)")
# Headings that are later-era beyond what is_later_era() finds in any sentence.
_LATER_HEADING = re.compile(
    r"^(?:World of Warcraft: )?(?:Legion|Dragonflight|Midnight)\b"
    r"|^(?:New in|Yearly updates|Toys|Decor|Appearances|Hozen)\b|^20(?:1[1-9]|[23]\d)\b"
    r"|^(?:Kul Tiras|Zandalar)(?:\s*/\s*(?:Kul Tiras|Zandalar))?$"
    r"|^(?:Dragon Soul|Trading Post|Well of Eternity|Quasi-heirlooms)$|(?i:\bdig ?site)$"
    r"|(?i:^(?:After|Following|Since) the (?:fall|death|defeat) of (?:the Lich King|Arthas)\b)")
# A later-era heading that names a later expansion or era: biographies run in story order, so the
# siblings after it are later too.
_ERA_HEADING = re.compile(
    r"^(?:World of Warcraft: |The )?(?:Cataclysm|Mists of Pandaria|Pandaria|Warlords of Draenor|"
    r"Legion|Battle for Azeroth|Shadowlands|Dragonflight|War Within|Midnight|Last Titan)\b"
    r"|\b(?:Cataclysm|Legion|Pandaria|Shadowlands|Dragonflight)[- ]era\b"
    r"|^(?:Post-)?20(?:1[1-9]|[23]\d)\b"
    r"|^(?:After|Following|Since) the (?:fall|death|defeat) of (?:the Lich King|Arthas)\b",
    re.IGNORECASE)
# A sibling heading that shows the story has reached Wrath's time.
_WRATH_ANCHOR = re.compile(
    r"\b(?:Wrath of the Lich King|(?:Fall|Death|Defeat) of the Lich King|"
    r"[Ww]ar against the Lich King|War in Northrend|Nexus War|Battle for the Undercity|Wrathgate|Angrathar|Argent Tournament|"
    r"Icecrown Citadel|Assault on Icecrown|Secrets of Ulduar|Call of the Crusade)\b|^Stormrage\b")
# Headings that name Wrath, a Wrath event or an earlier era: never cut as a later sibling.
_PERIOD_HEADING = re.compile(
    r"\b(?:Wrath of the Lich King|(?:Fall|Rise|Death|Defeat) of the Lich King|"
    r"war against the Lich King|Northrend|Nexus War|Battle for the Undercity|Scourge Invasion|"
    r"Wrathgate|Angrathar|Argent Tournament|Icecrown|Ulduar|Naxxramas|World of Warcraft|"
    r"Burning Crusade|Outland|Warcraft(?: I{1,3})?|Reign of Chaos|Frozen Throne|"
    r"Beyond the Dark Portal|Day of the Dragon|(?:First|Second|Third) War|War of the Ancients|"
    r"War of the Shifting Sands|Sundering|Troll Wars|Sunwell)\b", re.IGNORECASE)
# A sentence naming both is about Kalecgos as the new Aspect (Cataclysm on), or Bolvar after his
# reign (Legion on), unless the third pattern places it in Wrath: Bolvar took the Frozen Throne as
# the "Jailer of the Damned" in 3.3 with the Ebon Blade beside him. "Many years later" after the
# death of Arthas is past Wrath. The Broken Isles are old only as Illidan's goal in TFT.
_LATER_PAIRS = (
    (re.compile(r"\bKalec(?:gos)?\b"), re.compile(r"\bAspects?\b"), None),
    (re.compile(r"\bBolvar\b"),
     re.compile(r"\bthe Jailer\b(?! of the Damned)|\bZovaal\b|\bformer Lich King\b|\bthe Maw\b"
                r"|\bDeathlord\b"), None),
    (re.compile(r"\bBolvar\b"), re.compile(r"\bEbon Blade\b"),
     re.compile(r"\bascension of Bolvar\b|\bBolvar\b[^.;]{0,40}\bon the Frozen Throne\b")),
    (re.compile(r"\bAnduin\b"), re.compile(r"\bKing of Stormwind\b|\bHigh King\b"), None),
    (re.compile(r"\bMagni\b"),
     re.compile(r"\bformer (?:dwarven )?king\b|\bSpeaker\b|\bdiamond\b"), None),
    (re.compile(r"\b(?:[Mm]any|[Ss]everal) years later\b"),
     re.compile(r"(?i)\b(?:after|following)\b[^.;]{0,40}\b(?:death|fall|defeat) of "
                r"(?:Arthas|the Lich King)\b"), None))
# Uldum is old as the titans' re-origination site (Ulduar's Reply-Code, 3.1), later as a zone.
_LATER_UNLESS = ((re.compile(r"\bBroken Isles\b"),
                  re.compile(r"Second War|Third War|Illidan|Sargeras|Vashj|Frozen Throne")),
                 (re.compile(r"\bUldum\b"),
                  re.compile(r"Origination|Ulduar|Algalon|Reply-Code|[Tt]itan|Pedestal|Brann")))
# The end of the Lich King: a lead that tells what came after it has reached the later era.
_AFTER_THE_FALL = re.compile(
    r"(?i)\b(?:after|following|since|with)\b[^.;]{0,40}?\b(?:fall|death|defeat|demise|destruction)"
    r"\s+of\s+(?:Arthas|the Lich King)\b"
    r"|\b(?:after|following|since)\b[^.;]{0,40}?(?:\bArthas(?:'s?)?|\bthe Lich King's)\s+"
    r"(?:fall|death|defeat|demise)\b"
    r"|\bwith (?:Arthas|the Lich King) (?:defeated|dead|gone|slain|vanquished)\b")
_NOISE_HEADING = re.compile(
    r"^(?:gallery|images?|videos?|media|screenshots|see also|references|notes and references|"
    r"sources|external links|patch changes|patch history|version history|fan art|cosplay|"
    r"speculation|in the rpg|rpg|in other media|trading card game|tcg|hearthstone|"
    r"world of warcraft trading card game|starcraft|diablo|overwatch|"
    r"heroes of the storm|warcraft rumble|in hearthstone|inspiration|kill count|"
    r"alternate timelines?|character development|music|loot|navigation|bugs|past bugs|"
    r"fixed bugs|known bugs|external sites|further reading|guides)\b", re.IGNORECASE)
# Whole sections that mix every era with nothing to tell them apart, or are trivia.
NOISE_HEADINGS = frozenset({
    "notes", "notes and trivia", "trivia", "titles", "relationships", "personality",
    "powers and abilities", "appearance", "inconsistencies", "romance", "bug fixes",
    "foreign names", "notable", "notable members", "notable characters", "notable npcs",
    "legacy", "in other games", "cameos", "misc. character information"})


def _later_event(text: str) -> bool:
    text = text.replace("\u2011", "-")  # a non-breaking hyphen: "account‑wide"
    if _LATER.search(text):
        return True
    if any(a.search(text) and b.search(text) and not (u and u.search(text))
           for a, b, u in _LATER_PAIRS):
        return True
    return any(a.search(text) and not b.search(text) for a, b in _LATER_UNLESS)


def is_later_era(text: str) -> bool:
    """True when a sentence or line clearly talks about Cataclysm or later: a later expansion,
    story, zone, character or system, a patch from 4.0 on, a year from 2011 on, or 'retail', or
    retells old times with later lore (Azeroth's worldsoul), or makes a later-era aside."""
    return (_later_event(text) or _RETCON.search(text) is not None
            or _ASIDE.search(text) is not None)


def is_later_heading(heading: str) -> bool:
    h = heading.strip()
    return _LATER_HEADING.search(h) is not None or is_later_era(h)


def is_era_heading(heading: str) -> bool:
    """A later-era heading naming a later expansion or era (Cataclysm through Midnight, a year
    from 2011, "After the fall of the Lich King"), not just a later story or system."""
    return _ERA_HEADING.search(heading.strip()) is not None


def is_period_heading(heading: str) -> bool:
    """A heading naming Wrath of the Lich King, a Wrath event or an earlier era."""
    return _PERIOD_HEADING.search(heading.strip()) is not None


def is_noise_heading(heading: str, extra: frozenset[str] = frozenset()) -> bool:
    """Off-topic sections: media, references, other games, and the era-mixed trivia,
    personality, relationship and title sections (NOISE_HEADINGS), plus `extra` (lower case)."""
    h = " ".join(heading.split()).lower()
    return _NOISE_HEADING.match(h) is not None or h in NOISE_HEADINGS or h in extra


_ADDED_PATCH = re.compile(r"^Category:Added in patch (\d+)\.(\d+)")
_LATER_PAGE_CATEGORIES = frozenset({"Category:Unique to Classic", "Category:Scenarios"})
_LATER_LEVEL = re.compile(r"\{\{\s*[Ll]evel\s*\|\s*(?:cata|mop|wod|legion|bfa|sl|df|tww|midnight|"
                          r"tlt)\b", re.IGNORECASE)


def _era_patch(major: int, minor: int) -> bool:
    """A patch of the original game up to 3.3.5 (Classic re-releases are 1.13+, 2.5 and 3.4)."""
    return (major, minor) <= (1, 12) or major == 2 and minor <= 4 or major == 3 and minor <= 3


def page_is_later_era(categories: list[str], wikitext: str = "") -> bool:
    """Later content as a whole: every "Added in patch" category is 4.0 or later or a Classic
    re-release patch (1.13+, 2.5, 3.4), the page is unique to Classic or a scenario (Mists on),
    or its infobox levels it by a later expansion ({{level|legion-max}})."""
    if _LATER_PAGE_CATEGORIES.intersection(categories):
        return True
    patches = [(int(m.group(1)), int(m.group(2))) for c in categories
               if (m := _ADDED_PATCH.match(c))]
    if patches and not any(_era_patch(*p) for p in patches):
        return True
    head = re.split(r"\n==", wikitext, maxsplit=1)[0]
    return _LATER_LEVEL.search(head) is not None


# -- templates ---------------------------------------------------------------------------

LATER = "\x00LATER\x00"           # marks a line or sentence as later-era
LATER_SECTION = "\x00LATERSEC\x00"  # marks a whole section as later-era
NOISE_SECTION = "\x00NOISESEC\x00"  # marks a section as off-topic (RPG, Hearthstone, speculation)
OTHER_SECTION = "\x00OTHERSEC\x00"  # any other section tag ({{wotlk-section}}): ends a tagged run
QUOTE = "\x00QUOTE\x00"           # starts each line of a quote template (epigraphs go)
# Sections whose quotes are the content; elsewhere a quote template is an epigraph.
_QUOTE_SECTION = re.compile(r"\b(?:quotes?|quotations?|dialogue|gossip|on-click|yells?|speech|"
                            r"transcript)\b", re.IGNORECASE)

# The wiki's era tags: {{cata-section}} ... {{mn-section}} (Midnight), {{forever-section}} (World
# of Warcraft: Forever), {{sod-section}} and the Classic seasons; {{Classic only-section|cata}} is
# Cataclysm Classic.
_ERA_TAGS = ("cata", "cataclysm", "mop", "wod", "legion", "bfa", "sl", "shadowlands", "df",
             "dragonflight", "tww", "mn", "midnight", "tlt")
_LATER_TAGS = _ERA_TAGS + ("forever", "sod", "classic season only")
_NOISE_SECTION_TAGS = frozenset({"hs-section", "hearthstone-section", "rpg-section", "speculation",
                                 "tcg-section", "hots-section", "rumble-section", "film-section",
                                 "movie-section", "wa-section", "rfb-section", "rfptr-section",
                                 "rfa-section"})
_FACTION_ICONS = {"alliance": "(Alliance)", "horde": "(Horde)", "neutral": "(Neutral)",
                  "combat": "", "kia": ""}
_FIRST_ARG = frozenset({
    "mob", "spell", "quest", "questlong", "achievement", "faction", "zone", "currency",
    "talent", "glyph", "aura", "debuff", "buff", "au", "wp", "wplink", "portal", "nowrap",
    "nobr", "small", "tooltip", "quality", "lang", "ability", "spelllink", "itemlink",
    "item", "expac", "rep", "reputation", "npcname", "creature", "object", "instance",
    "subzone", "location", "loc", "skill", "profession", "title", "u", "highlight"})
_QUALITY = frozenset({"poor", "common", "uncommon", "rare", "epic", "legendary", "artifact",
                      "heirloom", "junk", "q0", "q1", "q2", "q3", "q4", "q5", "q6", "q7"})
# {{g|wotlk}} names a game or expansion by its short name and {{expac|Legion}} by its full one.
# Both are spelled out, and a later one marks its sentence: dropped, the template left "As of ,
# Anduin is 25 years old." behind.
_GAMES = {
    "wow": "World of Warcraft", "bc": "The Burning Crusade", "tbc": "The Burning Crusade",
    "wotlk": "Wrath of the Lich King", "wc1": "Warcraft: Orcs & Humans", "wc2": "Warcraft II",
    "wc3": "Warcraft III", "roc": "Warcraft III: Reign of Chaos", "tft": "The Frozen Throne",
    "tcg": "World of Warcraft Trading Card Game", "cata": "Cataclysm", "mop": "Mists of Pandaria",
    "wod": "Warlords of Draenor", "legion": "Legion", "bfa": "Battle for Azeroth",
    "sl": "Shadowlands", "df": "Dragonflight", "tww": "The War Within", "mn": "Midnight",
    "tlt": "The Last Titan", "sod": "Season of Discovery", "hc": "Hardcore",
    "forever": "World of Warcraft: Forever", "hs": "Hearthstone", "hots": "Heroes of the Storm",
    "reforged": "Warcraft III: Reforged", "rumble": "Warcraft Rumble"}
_LATER_GAMES = frozenset({"cata", "mop", "wod", "legion", "bfa", "sl", "df", "tww", "mn", "tlt",
                          "sod", "hc", "forever", "hs", "hots", "reforged", "rumble"})
_LATER_EXPACS = frozenset({"cataclysm", "mists of pandaria", "warlords of draenor", "legion",
                           "battle for azeroth", "shadowlands", "dragonflight", "the war within",
                           "midnight", "the last titan", "wrath of the lich king classic",
                           "cataclysm classic", "burning crusade classic"})


@dataclass
class _Tpl:
    name: str
    args: list[str]
    named: dict[str, str] = field(default_factory=dict)

    def pos(self, i: int) -> str:
        return self.args[i].strip() if i < len(self.args) else ""


def _render(t: _Tpl) -> str:
    n = t.name
    tag = n.removesuffix("-section").removesuffix("-inline")
    later = tag in _LATER_TAGS or (tag == "classic only" and t.pos(0).lower() in _ERA_TAGS)
    if n.endswith("-section"):
        if later:
            return LATER_SECTION
        return NOISE_SECTION if n in _NOISE_SECTION_TAGS else OTHER_SECTION
    if n.endswith("-inline"):
        return LATER if later else ""
    if n in _NOISE_SECTION_TAGS:
        return NOISE_SECTION
    if n.startswith("patch "):
        note = t.named.get("note") or t.named.get("comment") or ""
        return f"Patch {n[6:]}: {note}".strip().rstrip(":")
    if n == "abilities":
        name, desc = t.pos(0), t.pos(1)
        notes = t.named.get("notes", "").strip()
        out = f"{name}: {desc}" if desc else name
        return f"{out} ({notes})" if notes else out
    if n in ("quote", "quotation", "cquote", "bquote", "q", "quotewiki", "blockquote"):
        text, who = t.pos(0), t.pos(1)
        if not text:
            return ""
        out = f'"{text}"' + (f" - {who}" if who else "")
        return "\n".join(QUOTE + ln for ln in out.split("\n"))
    if n == "text":
        kind = t.pos(0)
        if len(t.args) >= 3:
            return f'{t.pos(1)} {kind}s: "{t.pos(2)}"'
        return f'"{t.pos(1)}"' if t.pos(1) else ""
    if n in ("dialogue", "gossip"):
        return f'"{t.pos(0)}"' if t.pos(0) else ""
    if n == "npc":
        return t.pos(3) or t.pos(1) or t.pos(0)
    if n == "loot":
        return t.pos(1) or t.pos(0)
    if n == "item":
        return t.pos(1) or t.pos(0)
    if n == "achievement":
        return t.pos(0)
    if n == "g":
        key = t.pos(0).lower()
        return _GAMES.get(key, "") + (LATER if key in _LATER_GAMES else "")
    if n == "expac":
        return t.pos(0) + (LATER if t.pos(0).lower() in _LATER_EXPACS else "")
    if n == "cost":
        parts = [f"{v} {u}" for v, u in zip(t.args, ("gold", "silver", "copper")) if v.strip()]
        return " ".join(parts)
    if n == "costitem":
        return f"{t.pos(0)} {t.pos(1)}".strip()
    if n == "lootbox":
        items = [a.strip() for a in t.args[1:] if a.strip()]
        return f"{t.pos(0)}: {', '.join(items)}" if items else ""
    if n == "#data:achievementtip":
        name, desc = t.named.get("name", ""), t.named.get("description", "")
        return f"{name}: {desc}" if desc else name
    if n in ("columns", "col-begin", "div col"):
        return "\n".join(a for a in t.args[1:] if a.strip())
    if n in _FACTION_ICONS:
        return _FACTION_ICONS[n]
    if n in ("nbsp", "space"):
        return " "
    if n == "!":
        return "|"
    if n in _FIRST_ARG:
        for a in t.args:
            a = a.strip()
            if a and not a.isdigit() and a.lower() not in _QUALITY and a.lower() != "notext":
                return a
        return ""
    return ""


def _split_args(inner: str) -> list[str]:
    """Split template content at top-level pipes (not inside nested [[ ]] or {{ }})."""
    parts, depth_b, depth_t, cur, i = [], 0, 0, [], 0
    while i < len(inner):
        two = inner[i:i + 2]
        if two == "[[":
            depth_b += 1
            cur.append(two)
            i += 2
            continue
        if two == "]]" and depth_b:
            depth_b -= 1
            cur.append(two)
            i += 2
            continue
        if two == "{{":
            depth_t += 1
            cur.append(two)
            i += 2
            continue
        if two == "}}" and depth_t:
            depth_t -= 1
            cur.append(two)
            i += 2
            continue
        ch = inner[i]
        if ch == "|" and not depth_b and not depth_t:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
        i += 1
    parts.append("".join(cur))
    return parts


def _matching(text: str, start: int, open_: str, close: str) -> int:
    """Index just past the `close` matching the `open_` at `start`, or -1."""
    depth, i = 0, start
    while i < len(text):
        if text.startswith(open_, i):
            depth += 1
            i += len(open_)
        elif text.startswith(close, i):
            depth -= 1
            i += len(close)
            if depth == 0:
                return i
        else:
            i += 1
    return -1


def expand_templates(text: str, _depth: int = 0) -> str:
    """Replace every {{template}} (innermost first) with its rendering. Table markup "{|" is
    left alone; an unclosed "{{" is dropped with the rest of its line."""
    if _depth > 20 or "{{" not in text:
        return text
    out, i = [], 0
    while True:
        j = text.find("{{", i)
        if j < 0:
            out.append(text[i:])
            break
        out.append(text[i:j])
        end = _matching(text, j, "{{", "}}")
        if end < 0:
            nl = text.find("\n", j)
            i = len(text) if nl < 0 else nl
            continue
        inner = text[j + 2:end - 2]
        if inner.startswith("{") and inner.endswith("}"):  # {{{param}}}: never in articles
            i = end
            continue
        args = [expand_templates(a, _depth + 1) for a in _split_args(inner)]
        head = args[0].strip()
        name = " ".join(head.replace("_", " ").split()).lower()
        if ":" in name and not name.startswith("#"):
            prefix, rest = name.split(":", 1)
            if prefix in ("template", "t"):
                name = rest.strip()
        tpl = _Tpl(name=name, args=[])
        for a in args[1:]:
            k, eq, v = a.partition("=")
            if eq and re.fullmatch(r"\s*[\w -]+\s*", k) and not k.strip().isdigit():
                tpl.named[k.strip().lower()] = v.strip()
            elif eq and k.strip().isdigit():
                idx = int(k.strip()) - 1
                while len(tpl.args) <= idx:
                    tpl.args.append("")
                tpl.args[idx] = v
            else:
                tpl.args.append(a)
        if name.startswith("#data:"):
            out.append(_render(tpl))
        elif name.startswith("#"):
            out.append("")
        else:
            out.append(_render(tpl))
        i = end
    return "".join(out)


# -- markup ------------------------------------------------------------------------------

_COMMENT = re.compile(r"<!--.*?(?:-->|\Z)", re.DOTALL)
_DROP_BLOCKS = re.compile(
    r"<(ref|gallery|noinclude|includeonly|math|syntaxhighlight|source|score|timeline|imagemap|"
    r"templatedata|mapframe|maplink|inputbox|categorytree)\b[^>]*?(?:/>|>.*?</\1\s*>)",
    re.DOTALL | re.IGNORECASE)
_SELF_CLOSING_REF = re.compile(r"<ref\b[^>]*/>|<references\b[^>]*/?>(?:.*?</references>)?",
                               re.DOTALL | re.IGNORECASE)
_BR = re.compile(r"<br\s*/?>", re.IGNORECASE)
_TAG = re.compile(r"</?[A-Za-z][A-Za-z0-9]*\b[^<>]*>")
_MAGIC = re.compile(r"__[A-Z]+__")
_EXT_LINK = re.compile(r"\[(?:https?:)?//[^\s\]]+(?:\s+([^\]]*))?\]")
_BARE_URL = re.compile(r"(?:https?:)?//[^\s<>\]|]+")
_BOLD_ITALIC = re.compile(r"'{2,5}")
_HEADING = re.compile(r"^(={1,6})\s*(.*?)\s*\1\s*$")
_SPACES = re.compile(r"[ \t ]+")
# what a dropped template leaves: "( )", "(now {{level|...}})"; not a call such as "Foo()"
_EMPTY_PARENS = re.compile(r"\s*\((?=\s|now\b|currently\b)(?:\s|now\b|currently\b)*\)")
_FILE_NS = ("file", "image", "media", "category")


def _links(text: str) -> str:
    """[[File:...]] and [[Category:...]] go (with nested links in captions); [[a|b]] -> b,
    [[a]] -> a, [[a]]s -> as, [[:Category:x|y]] -> y. A link whose page is later-era marks its
    sentence: [[Third invasion of the Burning Legion|invasion of Azeroth]]."""
    out, i = [], 0
    while True:
        j = text.find("[[", i)
        if j < 0:
            out.append(text[i:])
            break
        out.append(text[i:j])
        end = _matching(text, j, "[[", "]]")
        if end < 0:
            out.append(text[j:j + 2])
            i = j + 2
            continue
        inner = text[j + 2:end - 2]
        target, bar, label = inner.partition("|")
        ns = target.split(":", 1)[0].strip().lower() if ":" in target else ""
        trail = re.match(r"[a-z]*", text[end:]).group(0)
        if ns in _FILE_NS and not target.startswith(":"):
            out.append("")
        else:
            shown = _links(label) if bar else target.lstrip(":").split("#", 1)[0]
            if not bar and ":" in shown:
                shown = shown.split(":", 1)[1]
            later = bool(bar) and is_later_era(target) and not is_later_era(shown)
            out.append(shown.strip() + trail + (LATER if later else ""))
            end += len(trail)
        i = end
    return "".join(out)


def _table_to_lines(lines: list[str]) -> list[str]:
    """Wiki tables become one line per row, cells joined with ' - '; lists and text inside
    cells stay as their own lines. A table whose first row (its header) is later-era goes whole:
    its columns are later expansions ("(Cata Heroic)", "(MoP)")."""
    out: list[str] = []
    table: list[str] = []   # the lines of the open top-level table
    header: list[str] = []  # its first row, when that row is made of "!" header cells
    depth = 0
    row: list[str] = []
    row_header = False      # the row so far holds "!" cells only

    def flush():
        nonlocal row, row_header
        cells = [c for c in (x.strip() for x in row) if c]
        if cells:
            if row_header and not table:
                header.append(" - ".join(cells))
            table.append("* " + " - ".join(cells))
        row, row_header = [], False

    def close():
        if not (header and _later(_inline(header[0]))):
            out.extend(table)
        table.clear()
        header.clear()

    for line in lines:
        s = line.strip()
        if s.startswith("{|"):
            depth += 1
            continue
        if depth == 0:
            out.append(line)
            continue
        if s.startswith("|}"):
            flush()
            depth -= 1
            if depth == 0:
                close()
            continue
        if s.startswith("|-") or s.startswith("|+"):
            flush()
            continue
        if s.startswith("!") or s.startswith("|"):
            sep = "!!" if s.startswith("!") else "||"
            row_header = s.startswith("!") and (row_header or not row)
            for cell in s[1:].split(sep):
                body = cell
                if "|" in cell:
                    attrs, _, rest = cell.partition("|")
                    if "=" in attrs or not attrs.strip():
                        body = rest
                row.append(body)
            continue
        if s.startswith(("*", "#", ":", ";")):
            flush()
            table.append(s)
            continue
        if s:
            row.append(s)
    flush()
    if table:   # an unclosed table
        close()
    return out


def _inline(text: str) -> str:
    text = _links(text)
    text = _EXT_LINK.sub(lambda m: m.group(1) or "", text)
    text = _BARE_URL.sub("", text)
    text = _BOLD_ITALIC.sub("", text)
    text = _TAG.sub("", text)
    text = html.unescape(text).replace("\u2011", "-")  # a non-breaking hyphen: "account‑wide"
    return _EMPTY_PARENS.sub("", _SPACES.sub(" ", text)).strip()


@dataclass
class WikiSection:
    path: tuple[str, ...]
    paragraphs: list[str]

    @property
    def label(self) -> str:
        return " > ".join(self.path)


def _paragraphs(lines: list[str]) -> list[str]:
    """Plain lines join into paragraphs; list items become "- item" lines, one block per run.
    A later-era list item goes with the items nested under it, and a later-era or off-topic
    definition term (";Kul Tiras / Zandalar", ";Hearthstone") with the items that follow it up
    to the next term."""
    paras: list[str] = []
    text: list[str] = []
    items: list[str] = []
    skip: tuple[bool, int] | None = None  # (after a term, depth) of a later-era list item

    def flush_text():
        if text:
            paras.append(" ".join(text))
            text.clear()

    def flush_items():
        if items:
            paras.append("\n".join(items))
            items.clear()

    for raw in lines:
        s = raw.strip()
        if not s:
            flush_text()
            flush_items()
            skip = None
            continue
        m = re.match(r"^([*#:;]+)\s*(.*)$", s)
        if m:
            flush_text()
            marks = m.group(1)
            term, depth = marks.endswith(";"), len(marks)
            if skip is not None and ((skip[0] and not term) or (not skip[0] and depth > skip[1])):
                continue
            skip = None
            body = _inline(m.group(2))
            if term and ":" in body:
                body = body.replace(":", ": ", 1)
            # a term heads what follows: ";Kul Tiras / Zandalar", ";Hearthstone"
            if _later(body) or (term and (is_later_heading(body)
                                          or _NOISE_HEADING.match(" ".join(body.lower().split())))):
                skip = (term, depth)
                continue
            if body and body.strip(" -:,.;"):
                items.append("- " + body)
            continue
        flush_items()
        body = _inline(s)
        if body:  # a thumbnail or a dropped template has no text: a term's skip goes on
            skip = None
            text.append(body)
    flush_text()
    flush_items()
    return paras


def _drop_tagged_runs(lines: list[str]) -> list[str]:
    """Inside a section, a later-era or off-topic tag ({{legion-section}} under "Inmates")
    starts a run that lasts until the next other section tag ({{wotlk-section}}) or the end of
    the section; the run goes."""
    out: list[str] = []
    skipping = False
    for ln in lines:
        if LATER_SECTION in ln or NOISE_SECTION in ln:
            skipping = True
        elif OTHER_SECTION in ln:
            skipping = False
        if not skipping:
            out.append(ln.replace(OTHER_SECTION, ""))
    return out


def _later(unit: str) -> bool:
    return LATER in unit or is_later_era(unit)


def _retcon_only(unit: str) -> bool:
    """Later lore about old times, or a later-era aside (and nothing later than that): the
    sentence goes alone."""
    return (LATER not in unit and not _later_event(unit)
            and (_RETCON.search(unit) is not None or _ASIDE.search(unit) is not None))


def _retold(unit: str) -> bool:
    """A retold story (not an aside): the sentences after it that carry it on go with it."""
    return _retcon_only(unit) and _RETCON.search(unit) is not None


def _era_filter_paragraph(para: str) -> str:
    """List lines are judged one by one; prose ends at its first later-era sentence (the story
    has moved on by then), except one that only retells old times with later lore: that one goes
    alone, with the sentences that carry it on ("Odyn resented this. He did not trust ...")."""
    if para.startswith("- "):
        return "\n".join(ln for ln in para.split("\n") if not _later(ln))
    keep: list[str] = []
    retold = False
    for s in split_sentences(para):
        if _later(s):
            if _retcon_only(s):
                retold = _retold(s)
                continue
            break
        if retold and _CARRIED_ON.match(s):
            continue
        retold = False
        keep.append(s)
    return " ".join(keep)


def _lead_filter(paras: list[str]) -> list[str]:
    """The lead retells the whole life in order, ending in the present day. Its opening
    paragraph only loses later-era sentences (it states the present titles, then goes back to
    the start); after it, the first later-era sentence ends the lead, and so does, just after
    it, a sentence about the fall of the Lich King."""
    out: list[str] = []
    for n, para in enumerate(paras):
        is_list = para.startswith("- ")
        keep: list[str] = []
        stop = False
        retold = False
        for unit in (para.split("\n") if is_list else split_sentences(para)):
            if _later(unit):
                if n == 0 or _retcon_only(unit):
                    retold = not is_list and _retold(unit)
                    continue
                stop = True
                break
            if retold and _CARRIED_ON.match(unit):
                continue
            retold = False
            keep.append(unit)
            if _AFTER_THE_FALL.search(unit):
                stop = True
                break
        if keep:
            out.append(("\n" if is_list else " ").join(keep))
        if stop:
            break
    return out


def wikitext_to_sections(wikitext: str, *, log: list[str] | None = None,
                         drop_headings: frozenset[str] = frozenset(),
                         drop_lead: bool = False) -> list[WikiSection]:
    """The page as sections of clean paragraphs, era-filtered. `drop_headings` (lower case) are
    left out with everything under them, like the noise headings, and with `drop_lead` so is the
    text before the first heading. Dropped sections are noted in `log` when given."""
    text = _COMMENT.sub("", wikitext)
    text = _SELF_CLOSING_REF.sub("", text)
    text = _DROP_BLOCKS.sub("", text)
    text = _MAGIC.sub("", text)
    text = _BR.sub("\n", text)
    text = expand_templates(text)
    lines = _table_to_lines(text.split("\n"))

    sections: list[tuple[int, str, list[str]]] = [(1, "", [])]
    for line in lines:
        m = _HEADING.match(line.strip())
        if m:
            h = _inline(m.group(2))
            tagged = LATER in h or LATER_SECTION in h  # an era tag or a later-era page's link
            for marker in (LATER, LATER_SECTION, NOISE_SECTION, OTHER_SECTION):
                h = h.replace(marker, "")
            if tagged:
                sections.append((len(m.group(1)), h.strip() or "?", [LATER_SECTION]))
            else:
                sections.append((len(m.group(1)), h, []))
        else:
            sections[-1][2].append(line)

    out: list[WikiSection] = []
    path: list[tuple[int, str]] = []
    skip_below: int | None = None    # drop everything nested deeper than this level
    cut_level: int | None = None     # later-era sibling cut: drop the rest at this level
    shield: int | None = None        # a Wrath or older section kept inside a cut: not its children
    anchored: set[int] = set()       # levels whose earlier siblings reached Wrath's time
    for level, heading, body in sections:
        anchored = {lv for lv in anchored if lv <= level}
        if skip_below is not None and level > skip_below:
            continue
        skip_below = None
        if cut_level is not None and level < cut_level:
            cut_level = None
        if shield is not None and level <= shield:
            shield = None
        path = [(lv, h) for lv, h in path if lv < level] + ([(level, heading)] if heading else [])
        first = next((ln.strip() for ln in body if ln.strip()), "")
        reason = None
        if heading and is_noise_heading(heading, drop_headings):
            reason = "off-topic"
        elif first.startswith(NOISE_SECTION):
            reason = "off-topic"
        elif heading and is_later_heading(heading) or first.startswith(LATER_SECTION):
            reason = "later era"
        elif cut_level is not None and level >= cut_level and shield is None:
            if heading and is_period_heading(heading):
                shield = level
            else:
                reason = "later era (after a later-era sibling)"
        if heading and reason != "later era" and _WRATH_ANCHOR.search(heading):
            anchored.add(level)
        if reason:
            if log is not None and heading:
                log.append(f"dropped section {' > '.join(h for _, h in path)}: {reason}")
            skip_below = level
            # Biographies run in story order: an expansion's own heading ends the period, and so
            # does any later story once the siblings have reached Wrath ("Elemental Unrest" after
            # "Wrath of the Lich King"); before that it may be set in the past and goes alone.
            if (reason == "later era" and level >= 3
                    and (is_era_heading(heading) or level in anchored)):
                cut_level = level if cut_level is None else min(cut_level, level)
            continue
        lead = not heading and level == 1
        if lead and drop_lead:
            continue
        cleaned = _drop_tagged_runs(body)
        if not any(_QUOTE_SECTION.search(h) for _, h in path):
            # an epigraph: a quote at the head of a section, often from a later expansion
            cleaned = [ln for ln in cleaned if not ln.lstrip().startswith(QUOTE)]
        paras = _paragraphs([ln.replace(QUOTE, "") for ln in cleaned])
        paras = _lead_filter(paras) if lead else [_era_filter_paragraph(p) for p in paras]
        paras = [p for p in paras if p.strip()]
        if paras:
            out.append(WikiSection(tuple(h for _, h in path), paras))
    return out


def kept_wikitext(wikitext: str) -> str:
    """The raw wikitext without the sections the era filter drops by their heading (later-era
    and off-topic ones, with what is under them) and without the runs a later-era or off-topic
    tag starts inside a section, so a link in a Legion section or under "In the RPG" does not
    count as one of the page's own."""
    out: list[str] = []
    skip_below: int | None = None
    run = False   # inside a {{legion-section}} run
    for line in wikitext.split("\n"):
        m = _HEADING.match(line.strip())
        if m:
            level = len(m.group(1))
            if skip_below is not None and level > skip_below:
                continue
            skip_below, run = None, False
            h = _inline(expand_templates(m.group(2)))
            if LATER in h or LATER_SECTION in h or is_later_heading(h) or is_noise_heading(h):
                skip_below = level
                continue
        elif skip_below is not None:
            continue
        elif "-section" in line.lower():
            tags = expand_templates(line)
            if LATER_SECTION in tags or NOISE_SECTION in tags:
                run = True
            elif OTHER_SECTION in tags:
                run = False
        if not run:
            out.append(line)
    return "\n".join(out)


def is_disambiguation(wikitext: str, categories: list[str]) -> bool:
    return (any("isambiguation" in c for c in categories)
            or re.search(r"\{\{\s*(?:disambig|disambiguation|dab)\b", wikitext, re.I) is not None)
