RACES = {1: "Human", 2: "Orc", 3: "Dwarf", 4: "Night Elf", 5: "Undead", 6: "Tauren",
         7: "Gnome", 8: "Troll", 10: "Blood Elf", 11: "Draenei"}

CLASSES = {1: "Warrior", 2: "Paladin", 3: "Hunter", 4: "Rogue", 5: "Priest", 6: "Death Knight",
           7: "Shaman", 8: "Mage", 9: "Warlock", 11: "Druid"}

CAST_MIN_LEVEL = 80  # members at or above this level are the cast

TANK_CLASSES = frozenset({"Warrior", "Paladin", "Death Knight", "Druid"})
HEALER_CLASSES = frozenset({"Priest", "Paladin", "Shaman", "Druid"})

_HORDE = frozenset({2, 5, 6, 8, 10})
_ALLIANCE = frozenset({1, 3, 4, 7, 11})


def faction_of(race: int) -> str:
    return "horde" if race in _HORDE else "alliance"


def faction_known(race: int) -> str | None:
    """A playable race's faction, "horde" or "alliance"; None for any other race (0: his
    character is not on the roster)."""
    return "horde" if race in _HORDE else "alliance" if race in _ALLIANCE else None


def class_roles(cls: int | str) -> frozenset[str]:
    """The combat roles a class (by id or by name) can fill; every class can deal damage."""
    name = CLASSES.get(cls, "") if isinstance(cls, int) else cls
    return frozenset({"damage"} | ({"tank"} if name in TANK_CLASSES else set())
                     | ({"healer"} if name in HEALER_CLASSES else set()))


# The bridge sends boss_name "" and a boss_id that is the instance script's boss index
# (SetBossState), which repeats across raids, so a boss is named by (map, boss_id).
# Indexes are the BOSS_/DATA_ enums in the core's instance scripts; indexes that are not
# a fight (ICC gauntlets, Halion's intro stages) are left out on purpose and listed in
# NON_FIGHT_STEPS. Vault of Archavon never calls SetBossState (it uses SetData), so its
# entries are kept for completeness but the bridge cannot reach them.
BOSS_NAMES: dict[tuple[int, int], str] = {
    # Icecrown Citadel
    **{(631, i): n for i, n in enumerate((
        "Lord Marrowgar", "Lady Deathwhisper", "the Gunship Battle", "Deathbringer Saurfang",
        "Festergut", "Rotface", "Professor Putricide", "the Blood Prince Council",
        "Blood-Queen Lana'thel", "Sister Svalna", "Valithria Dreamwalker", "Sindragosa",
        "the Lich King"))},
    # Trial of the Crusader
    **{(649, i): n for i, n in enumerate((
        "the Northrend Beasts", "Lord Jaraxxus", "the Faction Champions", "the Twin Val'kyr",
        "Anub'arak"))},
    # Ulduar
    **{(603, i): n for i, n in enumerate((
        "Flame Leviathan", "Ignis the Furnace Master", "Razorscale", "XT-002 Deconstructor",
        "the Assembly of Iron", "Kologarn", "Auriaya", "Hodir", "Thorim", "Freya", "Mimiron",
        "General Vezax", "Yogg-Saron", "Algalon the Observer"))},
    # Naxxramas
    **{(533, i): n for i, n in enumerate((
        "Patchwerk", "Grobbulus", "Gluth", "Noth the Plaguebringer", "Heigan the Unclean",
        "Loatheb", "Anub'Rekhan", "Grand Widow Faerlina", "Maexxna", "Thaddius",
        "Instructor Razuvious", "Gothik the Harvester", "the Four Horsemen", "Sapphiron",
        "Kel'Thuzad"))},
    # Obsidian Sanctum
    **{(615, i): n for i, n in enumerate(("Sartharion", "Tenebron", "Vesperon", "Shadron"))},
    # Eye of Eternity
    (616, 0): "Malygos",
    # Vault of Archavon
    **{(624, i): n for i, n in enumerate((
        "Archavon the Stone Watcher", "Emalon the Storm Watcher", "Koralon the Flame Watcher",
        "Toravon the Ice Watcher"))},
    # Ruby Sanctum (3-5 are Halion's intro stages)
    (724, 0): "Baltharus the Warborn", (724, 1): "General Zarithrian",
    (724, 2): "Saviana Ragefire", (724, 6): "Halion",
    # Onyxia's Lair
    (249, 0): "Onyxia",
}

# The instance each raid map is (the library's chunks name it so), to tell apart two bosses of
# one name: Anub'arak in Trial of the Crusader, not in Azjol-Nerub.
RAID_INSTANCES = {631: "Icecrown Citadel", 649: "Trial of the Crusader", 603: "Ulduar",
                  533: "Naxxramas", 615: "Obsidian Sanctum", 616: "Eye of Eternity",
                  624: "Vault of Archavon", 724: "Ruby Sanctum", 249: "Onyxia's Lair"}

# Encounters of several bosses whose tactics, if any, are on the members' own pages: the
# library has no page for the encounter (the Twin Val'kyr have only lore and loot pages).
ENCOUNTER_MEMBERS: dict[str, tuple[str, ...]] = {
    "the Northrend Beasts": ("Gormok the Impaler", "Acidmaw and Dreadscale", "Icehowl"),
    "the Twin Val'kyr": ("Fjola Lightbane", "Eydis Darkbane"),
}


def boss_name(map_id: int, boss_id: int, given: str = "") -> str:
    """A non-empty name from the bridge wins, then the table, then a generic phrase."""
    given = (given or "").strip()
    return given or BOSS_NAMES.get((map_id, boss_id)) or "the boss"


RAID_MAPS = frozenset(m for m, _ in BOSS_NAMES)

# SetBossState steps that are not a fight: the Sindragosa gauntlet, the blood-prince trash and
# Halion's intro stages. They must never become kill or wipe memories.
NON_FIGHT_STEPS = frozenset({(631, 13), (631, 14), (724, 3), (724, 4), (724, 5)})


def is_raid_fight(map_id: int, boss_id: int) -> bool:
    return map_id in RAID_MAPS and (map_id, boss_id) not in NON_FIGHT_STEPS
