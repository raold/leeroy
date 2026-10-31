"""The game's names as the mind reads them in his chat (fix wave part E): the bosses, raids,
places, lore figures, items, events and factions of the lists the library is built from
(wow.BOSS_NAMES, library.wiki's seed pages) and the nicknames raiders type, with raider
shorthand written out (ALIASES). A class is never a name (a fire mage is not a place or a
boss) and neither is a continent.

A name matches the way he types it (name_pattern): any case, a hyphen or a space between its
words, an apostrophe dropped or typed as a space, a singular word also as a plural (a plural
entry never as its singular: "aspects" is no "aspect"), and the whole maybe possessive
("lanathel", "titans", "Marrowgar's"). names_in gives back the list's own spelling, name_level
how specific a name is and sides_named the side a line names. talk/select.py reads game_thing
from here, talk/questions.py and talk/facts.py the rest."""
import re
from typing import Sequence

from leeroy_mind.library.wiki import INSTANCE_PAGES, SEEDS
from leeroy_mind.wow import BOSS_NAMES, CLASSES, RAID_INSTANCES

# Raider shorthand and the names it stands for (the probe: "icc" and "t10" reached the search
# as they were typed). Two-letter shorthand that is also a word ("os", "up", "an") is left out;
# WORDLIKE shorthand is written out only where it is the line's game term (talk/questions.py).
ALIASES: dict[str, str] = {
    # raids and dungeons
    "icc": "Icecrown Citadel", "toc": "Trial of the Crusader",
    "togc": "Trial of the Grand Crusader", "naxx": "Naxxramas", "voa": "Vault of Archavon",
    "eoe": "Eye of Eternity", "uld": "Ulduar", "ony": "Onyxia", "onyxias lair": "Onyxia's Lair",
    "hor": "Halls of Reflection", "pos": "Pit of Saron", "fos": "Forge of Souls",
    "toc5": "Trial of the Champion", "vh": "Violet Hold", "dtk": "Drak'Tharon Keep",
    "draktharon": "Drak'Tharon Keep", "hol": "Halls of Lightning", "occ": "Oculus",
    "azjol nerub": "Azjol-Nerub", "ahnkahet": "Ahn'kahet",
    # bosses
    "lk": "Lich King", "bq": "Blood-Queen Lana'thel", "bql": "Blood-Queen Lana'thel",
    "blood queen": "Blood-Queen Lana'thel", "dbs": "Deathbringer Saurfang",
    "putri": "Professor Putricide", "sindy": "Sindragosa", "sindra": "Sindragosa",
    "vdw": "Valithria Dreamwalker", "bpc": "Blood Prince Council",
    "blood princes": "Blood Prince Council", "ldw": "Lady Deathwhisper",
    "kt": "Kel'Thuzad", "kelthuzad": "Kel'Thuzad", "sarth": "Sartharion", "sapph": "Sapphiron",
    "yogg": "Yogg-Saron", "yogg saron": "Yogg-Saron", "xt": "XT-002 Deconstructor",
    "anub": "Anub'arak", "anubarak": "Anub'arak", "anubrekhan": "Anub'Rekhan",
    "iron council": "Assembly of Iron", "faction champs": "Faction Champions",
    "jormungars": "Acidmaw and Dreadscale",
    # tiers and modes
    "t7": "Tier 7", "t8": "Tier 8", "t9": "Tier 9", "t10": "Tier 10", "hc": "heroic",
    "10m": "10-player", "25m": "25-player", "10man": "10-player", "25man": "25-player",
    "10-man": "10-player", "25-man": "25-player",
    # places, factions and the rest
    "wg": "Wintergrasp", "dal": "Dalaran", "zuldrak": "Zul'Drak",
    "ebon blade": "Knights of the Ebon Blade", "kaluak": "Kalu'ak",
    "frenzyheart": "Frenzyheart Tribe", "cwf": "Cold Weather Flying", "rep": "reputation",
}
# Shorthand that is also a word or slang: "hol up", "this boss is pos", "back up the dbs" (the
# owner runs this server's databases), "gotta rep my school", "brb 10m", "good dal around here".
WORDLIKE = frozenset({"hol", "pos", "dbs", "rep", "10m", "25m", "dal"})


def bare(name: str) -> str:
    """A seed page's title as a name: "Icecrown Citadel (instance)" -> "Icecrown Citadel"."""
    return re.sub(r"\s*\([^()]*\)$", "", name).strip()


def _word(w: str) -> str:
    """One word of a name as a pattern: an apostrophe in it optional or a space, and a
    singular word of four letters or more with or without a plural ending ("titan" matches
    "titans"). A word written with an "s" is matched as written: a plural entry never matches
    its singular ("aspects" never "aspect"), and "pos" never "poses"."""
    pat = re.escape(w).replace("'", r"[\s']?")
    if len(w.replace("'", "")) < 4:
        return pat
    if w.endswith("ss"):
        return pat + r"(?:es)?"
    return pat if w.endswith("s") else pat + r"(?:e?s)?"


def flex(name: str) -> str:
    """A name as a pattern for text in any case (compile it with re.IGNORECASE): a hyphen or a
    space between its words, each as _word."""
    return r"[\s-]+".join(_word(w) for w in re.split(r"[\s-]+", name.casefold()) if w)


def words_re(*phrases: str) -> str:
    """One alternation of the phrases as flex patterns, longest first."""
    return "|".join(flex(p) for p in sorted(set(phrases), key=len, reverse=True))


POSSESSIVE = r"(?:'s?)?"  # "Marrowgar's", "the Northrend Beasts'"


def name_pattern(name: str) -> str:
    """A regex for `name` as a whole name in a text, maybe possessive (re.IGNORECASE)."""
    return r"(?<![\w'])" + flex(name) + POSSESSIVE + r"(?![\w'])"


# The probe's raid shorthand and the boss nicknames raiders type; flex reads them in any
# spelling ("lanathel", "onyxias lair", "valkyr twins").
INSTANCE_WORDS = (
    "icc", "toc", "togc", "naxx", "eoe", "voa", "ony", "hor", "pos", "fos", "toc5", "uld",
    "vh", "dtk", "hol", "occ",
    "icecrown citadel", "trial of the crusader", "trial of the grand crusader", "ulduar",
    "naxxramas", "obsidian sanctum", "eye of eternity", "vault of archavon", "ruby sanctum",
    "onyxia's lair", "halls of reflection", "pit of saron", "forge of souls",
    "trial of the champion", "utgarde keep", "utgarde pinnacle", "the nexus", "oculus",
    "gundrak", "drak'tharon", "violet hold", "azjol-nerub", "ahn'kahet", "halls of lightning",
    "halls of stone", "culling of stratholme", "frozen halls",
    *(bare(p).casefold() for p in INSTANCE_PAGES))
BOSS_WORDS = (
    "marrowgar", "deathwhisper", "gunship", "saurfang", "festergut", "rotface", "putricide",
    "putri", "blood princes", "blood prince", "prince council", "lana'thel",
    "blood queen", "blood-queen", "bq", "bql", "dbs", "svalna", "valithria", "dreamwalker",
    "sindragosa", "sindy", "sindra", "vdw", "bpc", "ldw", "lich king", "lk", "gormok",
    "jormungar", "acidmaw", "dreadscale", "icehowl", "jaraxxus", "faction champs",
    "faction champions", "faction champion", "val'kyr twins", "anub'arak", "anub", "leviathan",
    "flame leviathan", "ignis", "razorscale", "xt", "xt-002", "iron council", "assembly of iron",
    "kologarn", "auriaya", "hodir", "thorim", "freya", "mimiron", "vezax", "yogg", "yogg-saron",
    "algalon", "patchwerk", "grobbulus", "gluth", "noth", "heigan", "loatheb", "anub'rekhan",
    "faerlina", "maexxna", "thaddius", "razuvious", "gothik", "four horsemen", "horsemen",
    "sapphiron", "sapph", "kel'thuzad", "kt", "sartharion", "sarth", "tenebron",
    "vesperon", "shadron", "malygos", "archavon", "emalon", "koralon", "toravon", "baltharus",
    "zarithrian", "saviana", "halion", "onyxia", "ony",
    *(re.sub(r"^the ", "", n.casefold()) for n in BOSS_NAMES.values()))
LORE_WORDS = (
    "arthas", "tirion", "bolvar", "sylvanas", "jaina", "thrall", "varian", "garrosh", "illidan",
    "uther", "muradin", "brann", "kael'thas", "darion", "mograine", "saurfang",
    "putress", "ner'zhul", "mal'ganis", "deathwing", "neltharion",
    "titans", "old gods", "old god", "frostmourne", "scourge", "forsaken", "naaru",
    "burning legion",
    "quel'delar", "lich king", "wrathgate", "wrath gate", "nerubians",
    "dragonflight", "aspects", "ashbringer", "shadowmourne")
PLACE_WORDS = (
    "wrath gate", "wrathgate", "tournament grounds", "argent tournament", "the shadow vault",
    "dark portal", "isle of quel'danas", "quel'danas", "sunwell", "stormwind", "ironforge",
    "darnassus", "exodar", "orgrimmar", "thunder bluff", "silvermoon", "shattrath", "moa'ki",
    "moa'ki harbor", "kamagua", "frosthold", "bouldercrag", "brunnhildar", "nesingwary",
    "argent vanguard", "ebon hold", "acherus", "light's hope", "zul'drak", "sholazar",
    "crystalsong", "storm peaks", "wyrmrest", "wg", "dal")
# The factions a reputation question is about (library.wiki's seeds have their pages); "oracle"
# is the Oracles' own word ("oracle rep"), which their plural entry no longer matches.
FACTION_WORDS = (
    "argent crusade", "argent dawn", "kirin tor", "knights of the ebon blade", "ebon blade",
    "kalu'ak", "frenzyheart tribe", "frenzyheart", "oracles", "oracle", "sons of hodir",
    "wyrmrest accord", "ashen verdict", "alliance vanguard", "valiance expedition", "valiance",
    "explorers' league", "silver covenant", "frostborn", "horde expedition",
    "warsong offensive", "hand of vengeance", "sunreavers", "sunreaver", "taunka",
    "shattered sun offensive")
# A side, as raiders call it too ("ally", "alli"), and the factions of that side only, as the
# library's own pages list them: the Alliance Vanguard's four and the Horde Expedition's four
# (facts.LibraryFacts: a line that names one side gets that side's facts).
SIDE_WORDS = {
    "alliance": ("alliance", "ally", "alli", "alliance vanguard", "valiance expedition",
                 "valiance", "explorers' league", "frostborn", "silver covenant"),
    "horde": ("horde", "horde expedition", "warsong offensive", "hand of vengeance", "taunka",
              "sunreavers", "sunreaver"),
}
# The factions of neither side: a place one of them holds is everyone's (facts.chunk_faction).
NEUTRAL_FACTIONS = tuple(f for f in FACTION_WORDS
                         if not any(f in words for words in SIDE_WORDS.values()))
CLASS_NAMES = frozenset(
    {c.casefold() for c in CLASSES.values()} | {f"{c.casefold()}s" for c in CLASSES.values()})
# Too broad to tie a fact to: everything in the game is somewhere on these, or in some mode.
BROAD = frozenset({"northrend", "azeroth", "outland", "kalimdor", "eastern kingdoms", "draenor",
                   "heroic", "10-player", "25-player", "reputation"})
_SEED_NAMES = tuple(bare(n).casefold() for kind in ("zone", "lore", "item", "event")
                    for n in SEEDS[kind])


def _key(name: str) -> str:
    """A name as all its spellings share it: no "the" first, no apostrophes, words by spaces."""
    words = re.split(r"[\s-]+", name.casefold().replace("'", ""))
    if words and words[0] == "the":
        words = words[1:]
    return " ".join(w for w in words if w)


def _spelt(names) -> list[str]:
    """One spelling of each name, longest first: the one with the most apostrophes and
    hyphens ("lana'thel" over "lanathel"), and no plural entry whose singular is a name too
    and matches it ("titans" goes, "titan" matches it; "old gods" stays, "god" is too short to
    take a plural)."""
    best: dict[str, str] = {}
    for n in names:
        k = _key(n)
        rank = (n.count("'") + n.count("-"), -len(n))
        if k not in best or rank > (best[k].count("'") + best[k].count("-"), -len(best[k])):
            best[k] = n
    plural = {k for k in best if k.endswith("s") and not k.endswith("ss") and any(
        s in best and re.fullmatch(flex(s), k)
        for s in (k[:-1], k[:-2] if k.endswith("es") else ""))}
    return sorted((n for k, n in best.items() if k not in plural), key=len, reverse=True)


NAME_LIST = _spelt(
    n for n in (*INSTANCE_WORDS, *BOSS_WORDS, *LORE_WORDS, *PLACE_WORDS, *FACTION_WORDS,
                *_SEED_NAMES, *(v.casefold() for v in ALIASES.values()))
    if n and n not in CLASS_NAMES and n not in BROAD and n not in ALIASES)
_NAMES = re.compile(r"(?<![\w'])(?:" + "|".join(
    f"(?P<n{i}>{flex(n)})" for i, n in enumerate(NAME_LIST)) + r")" + POSSESSIVE + r"(?![\w'])",
    re.IGNORECASE)
_ALIAS = re.compile(r"(?<![\w'-])(?:" + "|".join(
    re.escape(a) for a in sorted(ALIASES, key=len, reverse=True)) + r")(?![\w'-])", re.IGNORECASE)
_GAME_ALIAS = re.compile(r"(?<![\w'-])(?:" + "|".join(
    re.escape(a) for a in sorted(set(ALIASES) - WORDLIKE, key=len, reverse=True))
    + r")(?![\w'-])", re.IGNORECASE)


def names_in(text: str) -> tuple[str, ...]:
    """The names in `text`, in its order, as the list spells them (lower case, each once),
    the longest at each place: never a class or a continent."""
    out = []
    for m in _NAMES.finditer(text):
        name = NAME_LIST[int(m.lastgroup[1:])]
        if name not in out:
            out.append(name)
    return tuple(out)


def _letters(s: str) -> str:
    return re.sub(r"[\s'-]+", "", s.casefold())


def respell(text: str) -> str:
    """His names spelt as the list spells them where he dropped an apostrophe or a hyphen
    ("what does lanathel drop" -> "what does lana'thel drop"), so the searches find the
    library's spelling; a plural or a possessive stays as typed."""
    def sub(m: re.Match) -> str:
        name = NAME_LIST[int(m.lastgroup[1:])]
        typed = m.group(0)
        return name if _letters(typed) == _letters(name) and typed.casefold() != name else typed
    return _NAMES.sub(sub, text)


def expand(text: str, keep: Sequence[tuple[int, int]] = ()) -> str:
    """Raider shorthand written out (ALIASES): "where is the icc entrance" -> "where is the
    Icecrown Citadel entrance". Shorthand inside a span of `keep` (where it is a word, not the
    game) stays, and so does shorthand inside a name already written out ("ebon blade" in
    "knights of the ebon blade")."""
    names = [m.span() for m in _NAMES.finditer(text)]

    def sub(m: re.Match) -> str:
        a, b = m.span()
        if any(x <= a and b <= y for x, y in keep) or any(
                x <= a and b <= y and y - x > b - a for x, y in names):
            return m.group(0)
        return ALIASES[m.group(0).casefold()]
    return _ALIAS.sub(sub, text)


_INSTANCE_KEYS = frozenset(_key(n) for n in (
    *(w for w in INSTANCE_WORDS if w not in ALIASES), *(bare(p) for p in INSTANCE_PAGES),
    *RAID_INSTANCES.values(), "Trial of the Grand Crusader", "Drak'Tharon Keep",
    "Ahn'kahet: The Old Kingdom"))
_PLACE_KEYS = frozenset(_key(n) for n in (*(bare(z) for z in SEEDS["zone"]), *PLACE_WORDS)
                        ) - _INSTANCE_KEYS


def name_level(name: str) -> int:
    """How specific a name is: 1 a place (a zone, a city, a camp), 2 a dungeon or a raid, 3
    anything else (a boss, a person, an item, a faction, an event, a skill)."""
    k = _key(name)
    return 1 if k in _PLACE_KEYS else 2 if k in _INSTANCE_KEYS else 3


# Game nouns besides the names: "where do i ..." about one is about the game. Those that are
# also everyday words (a personal trainer, a tv mount, ski gear, a raid controller, an arcade
# token, a spreadsheet formula) count only with a second game cue.
_GAME_ONLY = re.compile(r"\b(?:" + words_re(
    "npc", "quartermaster", "innkeeper", "flight master", "reins", "schematic", "emblem",
    "tabard", "dailies", "attunement", "glyph", "battleground", "frostweave", "saronite",
    "eternium", "guild bank", "summoning stone", "meeting stone", "battlegear", "regalia")
    + r")\b", re.IGNORECASE)
_EVERYDAY = re.compile(r"\b(?:" + words_re(
    "trainer", "mount", "gear", "boss", "token", "formula", "pattern", "recipe", "mats", "vendor",
    "portal", "raid", "instance", "dungeon", "heroic", "armor", "weapon", "trinket", "talent",
    "loot", "elite", "daily", "achievement", "titanium", "flight path", "auction house",
    "turn in") + r")\b", re.IGNORECASE)


def game_thing(text: str) -> bool:
    """Whether `text` names something of the game: a name, raider shorthand that is not also
    a word (WORDLIKE), a noun only the game has, or two everyday nouns the game also uses
    ("raid gear", never "gear for skiing")."""
    if _NAMES.search(text) or _GAME_ALIAS.search(text) or _GAME_ONLY.search(text):
        return True
    return len({m.group(0).casefold() for m in _EVERYDAY.finditer(text)}) >= 2


_SIDES = {side: re.compile(r"(?<![\w'])(?:" + words_re(*words) + r")" + POSSESSIVE
                           + r"(?![\w'])", re.IGNORECASE) for side, words in SIDE_WORDS.items()}
# The words, not the sides: "a horde of adds", "my ally", "a strong ally".
_NO_SIDE = re.compile(r"\bhorde\s+of\b|\b(?:a|an|my|your|our|his|her|their|its|strong|great|good|"
                      r"true|loyal|close|powerful|valuable|best)\s+all[yi]\b", re.IGNORECASE)


def sides_named(text: str) -> frozenset[str]:
    """The sides `text` names ("alliance", "horde"), by name or by a faction of one side only
    (SIDE_WORDS): "the silver covenant quartermaster" and "the ally faction champs" are the
    Alliance's; "a horde of adds" is no side."""
    text = _NO_SIDE.sub(" ", text)
    return frozenset(side for side, pattern in _SIDES.items() if pattern.search(text))
