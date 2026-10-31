"""The world database (read-only) as library chunks: items, boss and chest loot by instance and
difficulty, quests with their chains, and one overview per instance. Names the database does not
store (maps, zones, encounters, sets, factions) come from the server's DBC files."""
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from leeroy_mind.library.chunks import ChunkDraft, ItemRow, QuestRow, SourceResult
from leeroy_mind.library.dbc import ClientNames, load_client_names
from leeroy_mind.personas.roster import DEFAULT_CONF, DEFAULT_MYSQL, read_db_info

Runner = Callable[[str], list[list[str | None]]]

QUALITY = ("poor", "common", "uncommon", "rare", "epic", "legendary", "artifact", "heirloom")
_SLOTS = {1: "head", 2: "neck", 3: "shoulder", 4: "shirt", 5: "chest", 6: "waist", 7: "legs",
          8: "feet", 9: "wrist", 10: "hands", 11: "finger", 12: "trinket", 13: "one-hand",
          14: "off hand", 15: "ranged", 16: "back", 17: "two-hand", 18: "bag", 19: "tabard",
          20: "chest", 21: "main hand", 22: "off hand", 23: "held in off hand", 24: "ammo",
          25: "thrown", 26: "ranged", 27: "quiver", 28: "relic"}
_WEAPONS = {0: "axe", 1: "axe", 2: "bow", 3: "gun", 4: "mace", 5: "mace", 6: "polearm",
            7: "sword", 8: "sword", 10: "staff", 13: "fist weapon", 14: "weapon", 15: "dagger",
            16: "thrown weapon", 17: "spear", 18: "crossbow", 19: "wand", 20: "fishing pole"}
_ARMOR = {1: "cloth", 2: "leather", 3: "mail", 4: "plate", 6: "shield", 7: "libram", 8: "idol",
          9: "totem", 10: "sigil"}
_CLASSES = {0: "consumable", 1: "container", 3: "gem", 5: "reagent", 6: "projectile",
            7: "trade good", 9: "recipe", 11: "quiver", 12: "quest item", 13: "key", 16: "glyph"}
_CLASS_WEIGHT = {3: 0.8, 16: 0.7, 0: 0.6, 1: 0.5, 13: 0.5}  # others (goods, recipes...) 0.4
QUEST_WEIGHT = 0.85  # nine thousand short chunks that name people and places all the time
_MISC = {2: "companion pet", 3: "holiday item", 5: "mount"}
_STATS = {0: "mana", 1: "health", 3: "Agility", 4: "Strength", 5: "Intellect", 6: "Spirit",
          7: "Stamina", 12: "defense rating", 13: "dodge rating", 14: "parry rating",
          15: "block rating", 16: "melee hit rating", 17: "ranged hit rating",
          18: "spell hit rating", 19: "melee crit rating", 20: "ranged crit rating",
          21: "spell crit rating", 28: "melee haste rating", 29: "ranged haste rating",
          30: "spell haste rating", 31: "hit rating", 32: "critical strike rating",
          35: "resilience rating", 36: "haste rating", 37: "expertise rating",
          38: "attack power", 39: "ranged attack power", 40: "feral attack power",
          41: "spell healing", 42: "spell damage", 43: "mana per 5 sec",
          44: "armor penetration rating", 45: "spell power", 46: "health per 5 sec",
          47: "spell penetration", 48: "block value"}
_CLASS_NAMES = {1: "Warrior", 2: "Paladin", 3: "Hunter", 4: "Rogue", 5: "Priest",
                6: "Death Knight", 7: "Shaman", 8: "Mage", 9: "Warlock", 11: "Druid"}
_BONDING = {1: "binds when picked up", 2: "binds when equipped", 3: "binds when used",
            4: "quest item"}
_SOCKETS = {1: "meta", 2: "red", 4: "yellow", 8: "blue"}
_REP_RANKS = ("Hated", "Hostile", "Unfriendly", "Neutral", "Friendly", "Honored", "Revered",
              "Exalted")
_ALLIANCE, _HORDE = 1101, 690
_EXPANSIONS = ("classic", "Burning Crusade", "Wrath of the Lich King")
# mod-individual-progression puts the 40-player vanilla raids back on the Wrath maps as its own
# creatures (ONYXIA_40 = 301000, Naxxramas 351000 on): base entries there would read as the
# map's 10-player difficulty. They are recognised by that id range or a vanilla boss level
# (60, shown as 63) on a 10/25-player raid map, and labelled as the tier they are.
PROGRESSION_FIRST_ENTRY = 300000
VANILLA_MAX_LEVEL = 63
VANILLA_TIER = "40-player (vanilla, mod-individual-progression tier)"
_JUNK_NAME = re.compile(r"^(?:OLD|zz|ZZ|Deprecated|DEPRECATED|Monster -|TEST|Test |QA|"
                        r"NPC Equip|Unused|UNUSED|DEBUG|\[PH\])|\[PH\]|\(old\)|\(OLD\)|"
                        r"\bDEPRECATED\b|\bDeprecated\b|\(DND\)|\bTEST\b|"
                        r"\bMonster (?:Offhand|Mainhand|Off Hand|Main Hand)\b")
_JUNK_QUEST = re.compile(r"^(?:<|\[|ZZ|zz|OLD|DEPRECATED|Deprecated|DEPRECAED|TEST|Test |UNUSED|"
                         r"REUSE|NYI|NOT A QUEST|DESTROY THIS QUEST|TEMP\b)|<(?:UNUSED|TXT|NYI|"
                         r"CHANGE)|\bDEPRECATED\b|\[PH\]|\bTEST\b|\bFLAG\b")
_TOKEN_G = re.compile(r"\$[Gg]\s*([^:;]*):([^;]*);")
_TOKENS = {"n": "<name>", "c": "<class>", "r": "<race>", "b": "\n"}

SQL_INSTANCES = "SELECT map FROM instance_template"
SQL_CONDITIONS = ("SELECT SourceTypeOrReferenceId, SourceGroup, SourceEntry, "
                  "ConditionTypeOrReference, ConditionValue1, NegativeCondition FROM conditions "
                  "WHERE SourceTypeOrReferenceId IN (1, 4, 10)")
SQL_EVENTS = "SELECT eventEntry, description FROM game_event"
SQL_ENCOUNTERS = "SELECT entry, creditType, creditEntry FROM instance_encounters"
SQL_BOSSES = (
    "SELECT entry, name, subname, `rank`, lootid, difficulty_entry_1, difficulty_entry_2, "
    "difficulty_entry_3, maxlevel FROM creature_template WHERE entry IN "
    "(SELECT creditEntry FROM instance_encounters WHERE creditType = 0) OR (entry IN "
    "(SELECT id FROM creature WHERE map IN (SELECT map FROM instance_template)) "
    "AND (`rank` = 3 OR (flags_extra & 1) = 1) AND lootid > 0)")
SQL_SPAWNS = ("SELECT id, MIN(map) FROM creature WHERE map IN (SELECT map FROM instance_template) "
              "GROUP BY id")
SQL_LOOTIDS = "SELECT entry, lootid FROM creature_template WHERE entry IN ({ids})"
_LOOT_COLS = "Entry, Item, Reference, Chance, QuestRequired, LootMode, GroupId, MinCount, MaxCount"
SQL_CREATURE_LOOT = f"SELECT {_LOOT_COLS} FROM creature_loot_template WHERE Entry IN ({{ids}})"
SQL_REFERENCE_LOOT = f"SELECT {_LOOT_COLS} FROM reference_loot_template"
SQL_CHESTS = ("SELECT gt.entry, gt.name, gt.Data1, COALESCE(MIN(g.map), -1), "
              "COALESCE(BIT_OR(g.spawnMask), 0) FROM gameobject_template gt "
              "LEFT JOIN gameobject g ON g.id = gt.entry WHERE gt.type = 3 AND gt.Data1 > 0 "
              "GROUP BY gt.entry, gt.name, gt.Data1")
SQL_CHEST_LOOT = f"SELECT {_LOOT_COLS} FROM gameobject_loot_template WHERE Entry IN ({{ids}})"
_ITEM_STATS = ", ".join(f"stat_type{i}, stat_value{i}" for i in range(1, 11))
SQL_ITEMS = (
    "SELECT entry, name, Quality, ItemLevel, RequiredLevel, InventoryType, class, subclass, "
    "AllowableClass, bonding, armor, dmg_min1, dmg_max1, delay, socketColor_1, socketColor_2, "
    "socketColor_3, itemset, description, RequiredReputationFaction, RequiredReputationRank, "
    "maxcount, startquest, spelltrigger_1, spellid_1, spelltrigger_2, spellid_2, "
    f"{_ITEM_STATS} FROM item_template")
SQL_VENDORS = ("SELECT nv.item, nv.ExtendedCost, ct.name, ct.subname FROM npc_vendor nv "
               "JOIN creature_template ct ON ct.entry = nv.entry")
SQL_QUESTS = (
    "SELECT q.ID, q.LogTitle, q.QuestLevel, q.MinLevel, q.QuestSortID, q.QuestInfoID, q.Flags, "
    "q.AllowableRaces, q.SuggestedGroupNum, q.LogDescription, q.QuestDescription, "
    "q.RewardNextQuest, q.RewardItem1, q.RewardItem2, q.RewardItem3, q.RewardItem4, "
    "q.RewardChoiceItemID1, q.RewardChoiceItemID2, q.RewardChoiceItemID3, "
    "q.RewardChoiceItemID4, q.RewardChoiceItemID5, q.RewardChoiceItemID6, "
    "q.RequiredNpcOrGo1, q.RequiredNpcOrGo2, q.RequiredNpcOrGo3, q.RequiredNpcOrGo4, "
    "q.RequiredNpcOrGoCount1, q.RequiredNpcOrGoCount2, q.RequiredNpcOrGoCount3, "
    "q.RequiredNpcOrGoCount4, q.RequiredItemId1, q.RequiredItemId2, q.RequiredItemId3, "
    "q.RequiredItemId4, q.RequiredItemId5, q.RequiredItemId6, q.RequiredItemCount1, "
    "q.RequiredItemCount2, q.RequiredItemCount3, q.RequiredItemCount4, q.RequiredItemCount5, "
    "q.RequiredItemCount6, COALESCE(a.PrevQuestID, 0), COALESCE(a.NextQuestID, 0), "
    "COALESCE(a.ExclusiveGroup, 0), COALESCE(a.AllowableClasses, 0) FROM quest_template q "
    "LEFT JOIN quest_template_addon a ON a.ID = q.ID")
SQL_QUEST_ENDS = (
    "SELECT 'start', q.quest, t.name FROM creature_queststarter q "
    "JOIN creature_template t ON t.entry = q.id UNION ALL "
    "SELECT 'end', q.quest, t.name FROM creature_questender q "
    "JOIN creature_template t ON t.entry = q.id UNION ALL "
    "SELECT 'start', q.quest, t.name FROM gameobject_queststarter q "
    "JOIN gameobject_template t ON t.entry = q.id UNION ALL "
    "SELECT 'end', q.quest, t.name FROM gameobject_questender q "
    "JOIN gameobject_template t ON t.entry = q.id")
SQL_NPC_NAMES = "SELECT entry, name FROM creature_template WHERE entry IN ({ids})"
SQL_GO_NAMES = "SELECT entry, name FROM gameobject_template WHERE entry IN ({ids})"


def _i(v) -> int:
    try:
        return int(float(v)) if v not in (None, "") else 0
    except ValueError:
        return 0


def _f(v) -> float:
    try:
        return float(v) if v not in (None, "") else 0.0
    except ValueError:
        return 0.0


def quest_text(text: str | None) -> str:
    """Quest text tokens made readable: $N, $C, $R as <name>, <class>, <race>, $B as a line
    break and $Gmale:female; as male/female."""
    if not text:
        return ""
    text = _TOKEN_G.sub(lambda m: f"{m.group(1).strip()}/{m.group(2).strip()}", text)
    text = re.sub(r"\$([NnCcRrBb])", lambda m: _TOKENS[m.group(1).lower()], text)
    return " ".join(text.split())


def _cap_words(text: str, n: int) -> str:
    words = text.split()
    return text if len(words) <= n else " ".join(words[:n]) + " ..."


@dataclass
class Item:
    id: int
    name: str
    quality: int
    ilvl: int
    req_level: int
    inv_type: int
    cls: int
    sub: int
    row: list = field(repr=False, default_factory=list)

    def slot(self) -> str:
        """'two-hand axe', 'plate legs', 'trinket', 'mount', '' for plain goods."""
        if self.cls == 2:
            hand = _SLOTS.get(self.inv_type, "")
            kind = _WEAPONS.get(self.sub, "weapon")
            if hand in ("ranged", "thrown") or kind in ("bow", "gun", "crossbow", "wand"):
                return kind
            return f"{hand} {kind}".strip()
        if self.cls == 4:
            where = _SLOTS.get(self.inv_type, "")
            what = _ARMOR.get(self.sub, "")
            if what in ("shield", "libram", "idol", "totem", "sigil"):
                return what
            if self.inv_type in (2, 11, 12, 16, 23, 4, 19):  # neck, ring, trinket, back, ...
                return where
            return f"{what} {where}".strip()
        if self.cls == 15:
            return _MISC.get(self.sub, "")
        return _CLASSES.get(self.cls, "")

    def short(self) -> str:
        """Name plus a hint for gear: 'Bryntroll, the Bone Arbiter (264 two-hand axe)'."""
        s = self.slot()
        if self.cls in (2, 4) and s:
            return f"{self.name} ({self.ilvl} {s})"
        return f"{self.name} ({s})" if s in ("mount", "companion pet", "recipe") else self.name

    def weight(self) -> float:
        """Search weight of its chunk: gear by quality, mounts, pets and emblems high, then
        gems, glyphs, consumables, and trade goods, recipes, reagents and quest items low."""
        if self.quality == 0:
            return 0.3
        if self.cls in (2, 4):
            return {1: 0.5, 2: 0.75, 3: 0.9}.get(self.quality, 1.0)
        if self.cls == 10 or (self.cls == 15 and self.sub in (2, 5)):
            return 0.9
        w = _CLASS_WEIGHT.get(self.cls, 0.4)
        return max(w, 0.7) if self.quality >= 4 else w


@dataclass
class LootRow:
    entry: int
    item: int
    reference: int
    chance: float
    quest: bool
    mode: int
    group: int
    min_count: int
    max_count: int


@dataclass
class LootLine:
    item: int
    pool: int = 0          # the reference table it came from, 0 when listed directly
    picks: int = 1         # how many items the pool gives
    chance: float = 100.0
    quest: bool = False
    mode: int = 1
    count: int = 1
    origin: tuple[int, int] = (0, 0)   # (conditions source type, template entry)
    condition: str = ""                # why it drops only sometimes, from the conditions table


def _loot_rows(rows) -> dict[int, list[LootRow]]:
    out: dict[int, list[LootRow]] = {}
    for r in rows:
        lr = LootRow(_i(r[0]), _i(r[1]), _i(r[2]), _f(r[3]), _i(r[4]) != 0, _i(r[5]), _i(r[6]),
                     _i(r[7]), _i(r[8]))
        out.setdefault(lr.entry, []).append(lr)
    return out


def resolve_loot(rows: list[LootRow], refs: dict[int, list[LootRow]], *, source_type: int = 1,
                 conditions: dict[tuple[int, int, int], str] | None = None) -> list[LootLine]:
    """Flatten a loot template: reference rows become pools (the reference id and how many items
    it gives); nested references fold into the outer pool. `source_type` is the conditions
    table's type for the template (1 creature, 4 gameobject; references are 10)."""
    out: list[LootLine] = []
    conditions = conditions or {}

    def walk(rs, pool, picks, depth, seen, stype):
        for r in rs:
            if r.reference:
                if depth > 6 or r.reference in seen:
                    continue
                sub = refs.get(r.reference, [])
                walk(sub, pool or r.reference, picks if pool else max(1, r.max_count),
                     depth + 1, seen | {r.reference}, 10)
            elif r.item:
                out.append(LootLine(r.item, pool, picks, r.chance, r.quest, r.mode,
                                    max(1, r.max_count), (stype, r.entry),
                                    conditions.get((stype, r.entry, r.item), "")))

    walk(rows, 0, 1, 0, frozenset(), source_type)
    return out


def condition_text(ctype: int, value: int, negative: bool, events: dict[int, str]) -> str:
    """A short reason a loot row is conditional (conditions.ConditionTypeOrReference)."""
    if ctype == 12:
        event = events.get(value) or "a world event"
        return f"except during {event}" if negative else f"during {event}"
    if ctype in (8, 28) and value >= 66000:
        return "by progression tier"
    if ctype == 8:
        return "after a quest"
    if ctype == 9:
        return "while on a quest"
    if ctype == 1:
        return "with Essence of Wintergrasp" if value in (57940, 58045) else "with a buff"
    if ctype == 7:
        return "with a profession skill"
    if ctype == 6:
        return {469: "Alliance only", 67: "Horde only"}.get(value, "one faction only")
    return "conditional"


@dataclass
class _Source:
    """One loot table owner: a boss at a difficulty, or a chest."""
    key: str
    title: str
    section: str
    map_id: int | None
    instance: str | None
    refs: tuple[str, ...]
    lines: list[LootLine]
    hard_mode_map: bool = False


class WorldSource:
    name = "world"

    def __init__(self, *, conf: Path = DEFAULT_CONF, mysql: Path = DEFAULT_MYSQL,
                 dbc_dir: Path | None = None, run: Runner | None = None,
                 names: ClientNames | None = None):
        self._conf = Path(conf)
        self._mysql = Path(mysql)
        self._dbc_dir = dbc_dir
        self._run = run
        self._names = names

    def __repr__(self) -> str:
        return f"WorldSource(conf={str(self._conf)!r})"

    def _runner(self) -> Runner:
        if self._run is not None:
            return self._run
        from leeroy_mind.library.mysql import query
        db = read_db_info(self._conf, "WorldDatabaseInfo")
        return lambda sql: query(db, sql, self._mysql)

    def _client_names(self) -> ClientNames:
        if self._names is not None:
            return self._names
        d = self._dbc_dir
        if d is None:
            m = re.search(r'^\s*DataDir\s*=\s*"([^"]*)"',
                          self._conf.read_text(encoding="utf-8", errors="replace"), re.MULTILINE)
            d = Path(m.group(1)) / "dbc" if m else None
        return load_client_names(d)

    # -- helpers -----------------------------------------------------------------------

    def _difficulty(self, n: ClientNames, map_id: int, diff: int) -> str:
        m = n.maps.get(map_id)
        if m is None:
            return f"difficulty {diff}" if diff else ""
        if m.instance_type == 1:
            return "heroic" if diff == 1 else "normal"
        if m.instance_type == 2:
            size = n.sizes.get((map_id, diff)) or m.max_players
            return f"{size}-player" + (" heroic" if diff >= 2 else "") if size else ""
        return ""

    @staticmethod
    def _vanilla_tier(n: ClientNames, entry: int, level: int, map_id: int) -> bool:
        """A mod-individual-progression 40-player vanilla boss on a Wrath raid map (one with
        10- and 25-player modes, as Naxxramas and Onyxia's Lair have in 3.3.5)."""
        m = n.maps.get(map_id)
        wrath_raid = (m is not None and m.instance_type == 2
                      and n.sizes.get((map_id, 0)) == 10 and n.sizes.get((map_id, 1)) == 25)
        return wrath_raid and (entry >= PROGRESSION_FIRST_ENTRY or 0 < level <= VANILLA_MAX_LEVEL)

    def _map_name(self, n: ClientNames, map_id: int | None) -> str | None:
        if map_id is None or map_id < 0:
            return None
        m = n.maps.get(map_id)
        return m.name if m else f"map {map_id}"

    # -- load --------------------------------------------------------------------------

    def load(self, library=None) -> SourceResult:
        run = self._runner()
        n = self._client_names()
        res = SourceResult(self.name, items=[], quests=[])
        if n.loaded:
            res.log.append("client names from " + ", ".join(n.loaded))
        else:
            res.log.append("no DBC files found: instances and zones are named by id")

        items = self._items(run, res)
        vendors = self._vendors(run, n, items)
        events = {_i(r[0]): (r[1] or "").strip() for r in run(SQL_EVENTS)}
        self._conditions: dict[tuple[int, int, int], str] = {}
        for r in run(SQL_CONDITIONS):
            key = (_i(r[0]), _i(r[1]), _i(r[2]))
            if key not in self._conditions:
                self._conditions[key] = condition_text(_i(r[3]), _i(r[4]), _i(r[5]) != 0, events)
        sources = self._bosses(run, n, res) + self._chests(run, n, items, res)
        drops: dict[int, list[str]] = {}
        for s in sources:
            text = self._loot_text(s, items)
            if not text:
                continue
            for part, body in enumerate(text):
                key = s.key if part == 0 else f"{s.key}:{part + 1}"
                section = s.section if len(text) == 1 else \
                    (f"{s.section} (part {part + 1})" if s.section else f"part {part + 1}")
                res.chunks.append(ChunkDraft(
                    key=key, kind="loot", title=s.title, section=section, text=body,
                    map_id=s.map_id, instance=s.instance,
                    ref_ids=s.refs + tuple(f"item:{ln.item}" for ln in s.lines
                                           if ln.item in items)))
            where = f"{s.title} ({', '.join(x for x in (s.instance, s.section) if x)})" \
                if s.instance or s.section else s.title
            for ln in s.lines:
                drops.setdefault(ln.item, [])
                if where not in drops[ln.item]:
                    drops[ln.item].append(where)
        rewards = self._quests(run, n, items, res)
        self._item_chunks(items, n, drops, vendors, rewards, res)
        self._instance_chunks(n, res)
        return res

    # items

    def _items(self, run: Runner, res: SourceResult) -> dict[int, Item]:
        items: dict[int, Item] = {}
        for r in run(SQL_ITEMS):
            it = Item(_i(r[0]), (r[1] or "").strip(), _i(r[2]), _i(r[3]), _i(r[4]), _i(r[5]),
                      _i(r[6]), _i(r[7]), row=r)
            if it.name:
                items[it.id] = it
        res.log.append(f"{len(items)} items in item_template")
        return items

    def _vendors(self, run: Runner, n: ClientNames, items: dict[int, Item]
                 ) -> dict[int, list[str]]:
        out: dict[int, list[str]] = {}
        for r in run(SQL_VENDORS):
            item, cost, who, sub = _i(r[0]), _i(r[1]), (r[2] or "").strip(), (r[3] or "").strip()
            if item not in items or not who:
                continue
            seller = f"{who} <{sub}>" if sub else who
            price = self._price(n, cost, items)
            entry = f"{seller} for {price}" if price else seller
            lst = out.setdefault(item, [])
            if entry not in lst:
                lst.append(entry)
        return out

    def _price(self, n: ClientNames, cost: int, items: dict[int, Item]) -> str:
        rec = n.extended_costs.get(cost)
        if not rec:
            return ""
        parts = []
        if rec[1]:
            parts.append(f"{rec[1]} honor")
        if rec[2]:
            parts.append(f"{rec[2]} arena points")
        for item, count in zip(rec[4:9], rec[9:14]):
            if item and count:
                parts.append(f"{count} {items[item].name if item in items else f'item {item}'}")
        if len(rec) > 14 and rec[14]:
            parts.append(f"a {rec[14]} arena rating")
        return ", ".join(parts)

    # bosses and chests

    def _bosses(self, run: Runner, n: ClientNames, res: SourceResult) -> list[_Source]:
        instance_maps = {_i(r[0]) for r in run(SQL_INSTANCES)}
        enc_by_creature: dict[int, list] = {}
        for r in run(SQL_ENCOUNTERS):
            if _i(r[1]) == 0:
                enc = n.encounters.get(_i(r[0]))
                enc_by_creature.setdefault(_i(r[2]), []).append(enc)
        spawn_map = {_i(r[0]): _i(r[1]) for r in run(SQL_SPAWNS)}
        bosses = run(SQL_BOSSES)
        diff_ids = sorted({_i(x) for r in bosses for x in r[5:8] if _i(x)})
        diff_loot = {_i(r[0]): _i(r[1]) for r in run(SQL_LOOTIDS.format(
            ids=",".join(map(str, diff_ids))))} if diff_ids else {}
        owners: list[tuple] = []   # (creature entry, name, map, difficulty, lootid, vanilla)
        vanilla = 0
        for r in bosses:
            entry, name = _i(r[0]), (r[1] or "").strip()
            encs = [e for e in enc_by_creature.get(entry, []) if e is not None]
            map_id = encs[0].map_id if encs else spawn_map.get(entry, -1)
            if map_id not in instance_maps and not encs:
                continue
            tier = self._vanilla_tier(n, entry, _i(r[8]), map_id)
            vanilla += tier
            for diff, e in enumerate((entry, _i(r[5]), _i(r[6]), _i(r[7]))):
                loot = _i(r[4]) if diff == 0 else diff_loot.get(e, 0)
                if e and loot:
                    owners.append((entry, name, map_id, diff, loot, tier))
        lootids = sorted({o[4] for o in owners})
        rows = _loot_rows(run(SQL_CREATURE_LOOT.format(ids=",".join(map(str, lootids)))) if lootids
                          else [])
        refs = _loot_rows(run(SQL_REFERENCE_LOOT))
        self._refs = refs
        out = []
        for entry, name, map_id, diff, loot, tier in owners:
            lines = resolve_loot(rows.get(loot, []), refs, source_type=1,
                                 conditions=self._conditions)
            if not lines:
                continue
            section = VANILLA_TIER if tier else self._difficulty(n, map_id, diff)
            out.append(_Source(
                key=f"loot:npc:{entry}:{diff}", title=name, section=section,
                map_id=map_id if map_id >= 0 else None, instance=self._map_name(n, map_id),
                refs=(f"npc:{entry}",), lines=lines, hard_mode_map=map_id == 603))
        res.log.append(f"{len(out)} boss loot tables; {vanilla} bosses are "
                       f"mod-individual-progression's vanilla raid tier ({VANILLA_TIER})")
        return out

    def _chests(self, run: Runner, n: ClientNames, items: dict[int, Item],
                res: SourceResult) -> list[_Source]:
        chests = run(SQL_CHESTS)
        lootids = sorted({_i(r[2]) for r in chests})
        rows = _loot_rows(run(SQL_CHEST_LOOT.format(ids=",".join(map(str, lootids))))
                          if lootids else [])
        refs = getattr(self, "_refs", None) or _loot_rows(run(SQL_REFERENCE_LOOT))
        instance_maps = {_i(r[0]) for r in run(SQL_INSTANCES)}
        out = []
        for r in chests:
            entry, name, loot, map_id, mask = _i(r[0]), (r[1] or "").strip(), _i(r[2]), \
                _i(r[3]), _i(r[4])
            # Spawned in an instance, or never spawned (a script places it, like Gunship Armory).
            if not name or (map_id >= 0 and map_id not in instance_maps):
                continue
            lines = resolve_loot(rows.get(loot, []), refs, source_type=4,
                                 conditions=getattr(self, "_conditions", {}))
            # Herb and ore nodes are chests too: keep only those holding real epics.
            if not any(items[ln.item].quality >= 4 and (ln.pool or ln.chance >= 1)
                       for ln in lines if ln.item in items):
                continue
            diffs = [d for d in range(4) if mask & (1 << d)]
            section = self._difficulty(n, map_id, diffs[0]) if map_id >= 0 and len(diffs) == 1 \
                else ""
            out.append(_Source(
                key=f"loot:go:{entry}", title=name, section=section,
                map_id=map_id if map_id >= 0 else None, instance=self._map_name(n, map_id),
                refs=(f"go:{entry}",), lines=lines, hard_mode_map=map_id == 603))
        res.log.append(f"{len(out)} chests with epic loot")
        return out

    def _loot_text(self, s: _Source, items: dict[int, Item], per_chunk: int = 30) -> list[str]:
        """'What Sindragosa drops (Icecrown Citadel, 25-player loot table). Always: ... 2 items
        from: ...' (the words players ask with: drops, loot table)."""
        def name(ln: LootLine) -> str:
            it = items.get(ln.item)
            return it.short() if it else f"item {ln.item}"

        where = ", ".join(x for x in (s.instance, s.section) if x)
        head = f"What {s.title} drops ({where + ' ' if where else ''}loot table)."
        parts: list[str] = []
        special = "hard mode" if s.hard_mode_map else "special"
        direct = [ln for ln in s.lines if not ln.pool]
        always = [ln for ln in direct
                  if ln.chance >= 100 and not ln.quest and ln.mode <= 1 and not ln.condition]
        if always:
            parts.append("Always: " + ", ".join(
                (f"{ln.count} x " if ln.count > 1 else "") + name(ln) for ln in always) + ".")
        pools: dict[int, list[LootLine]] = {}
        for ln in s.lines:
            if ln.pool:
                pools.setdefault(ln.pool, []).append(ln)
        for pool, lns in pools.items():
            picks = lns[0].picks
            uniq = list(dict.fromkeys(
                name(ln) + "".join(f" ({x})" for x in (special if ln.mode > 1 else "",
                                                        ln.condition) if x)
                for ln in lns))
            lead = f"{picks} items from" if picks > 1 else "One item from"
            for i in range(0, len(uniq), per_chunk):
                parts.append(f"{lead}: " + ", ".join(uniq[i:i + per_chunk]) + ".")
        quest = [ln for ln in direct if ln.quest]
        if quest:
            parts.append("Quest items: " + ", ".join(dict.fromkeys(name(ln) for ln in quest)) + ".")
        other = [ln for ln in direct if ln not in always and not ln.quest]
        if other:
            def tail(ln):
                bits = [f"{ln.chance:g}%"] if 0 < ln.chance < 100 else []
                if ln.mode > 1:
                    bits.append(special)
                if ln.condition:
                    bits.append(ln.condition)
                lead = f"{ln.count} x " if ln.count > 1 else ""
                return lead + name(ln) + (f" ({', '.join(bits)})" if bits else "")
            names = list(dict.fromkeys(tail(ln) for ln in other))
            for i in range(0, len(names), per_chunk):
                parts.append("Sometimes: " + ", ".join(names[i:i + per_chunk]) + ".")
        if not parts:
            return []
        chunks, cur = [], head
        for p in parts:
            if len((cur + " " + p).split()) > 260 and cur != head:
                chunks.append(cur)
                cur = head
            cur += " " + p
        chunks.append(cur)
        return chunks

    # quests

    def _quests(self, run: Runner, n: ClientNames, items: dict[int, Item],
                res: SourceResult) -> dict[int, list[str]]:
        ends: dict[int, dict[str, list[str]]] = {}
        for kind, quest, who in run(SQL_QUEST_ENDS):
            if who:
                lst = ends.setdefault(_i(quest), {}).setdefault(kind, [])
                if who not in lst:
                    lst.append(who)
        starters = {_i(it.row[22]): it.name for it in items.values() if _i(it.row[22])}
        rows = run(SQL_QUESTS)
        npc_ids = sorted({_i(x) for r in rows for x in r[22:26] if _i(x) > 0})
        go_ids = sorted({-_i(x) for r in rows for x in r[22:26] if _i(x) < 0})
        objectives: dict[tuple[str, int], str] = {}
        for kind, sql, ids in (("npc", SQL_NPC_NAMES, npc_ids), ("go", SQL_GO_NAMES, go_ids)):
            if ids:
                for r in run(sql.format(ids=",".join(map(str, ids)))):
                    objectives[(kind, _i(r[0]))] = (r[1] or "").strip()
        titles = {_i(r[0]): (r[1] or "").strip() for r in rows}
        followers: dict[int, list[int]] = {}
        for r in rows:
            if (p := abs(_i(r[42]))):
                followers.setdefault(p, []).append(_i(r[0]))
        rewards: dict[int, list[str]] = {}
        kept = 0
        for r in rows:
            qid, title = _i(r[0]), (r[1] or "").strip()
            if not title or _JUNK_QUEST.search(title):
                continue
            kept += 1
            level, min_level = _i(r[2]), _i(r[3])
            sort, info, flags, races = _i(r[4]), _i(r[5]), _i(r[6]), _i(r[7])
            prev = abs(_i(r[42]))
            nxt = _i(r[43]) or _i(r[11]) or min(followers.get(qid, []), default=0)
            zone = (n.areas.get(sort, "") if sort > 0 else n.quest_sorts.get(-sort, "")) or ""
            res.quests.append(QuestRow(qid, title, level, zone, prev if prev in titles else 0,
                                       nxt if nxt in titles else 0, _i(r[44])))
            tags = [f"level {level}" if level > 0 else "scales with level"]
            if min_level:
                tags.append(f"requires level {min_level}")
            if zone:
                tags.append(zone)
            if info and n.quest_infos.get(info):
                tags.append(n.quest_infos[info].lower())
            if flags & 0x1000:
                tags.append("daily")
            if flags & 0x8000:
                tags.append("weekly")
            if races == _ALLIANCE:
                tags.append("Alliance")
            elif races == _HORDE:
                tags.append("Horde")
            classes = [c for bit, c in _CLASS_NAMES.items() if _i(r[45]) & (1 << (bit - 1))]
            if classes and len(classes) < len(_CLASS_NAMES):
                tags.append("/".join(classes) + " only")
            sents = [f"Quest {title} ({', '.join(tags)})."]
            e = ends.get(qid, {})
            start = e.get("start", []) + ([starters[qid]] if qid in starters else [])
            if start:
                sents.append(f"Starts with {', '.join(start[:3])}.")
            if e.get("end"):
                sents.append(f"Ends with {', '.join(e['end'][:3])}.")
            goal = quest_text(r[9])
            if goal:
                sents.append(f"Objective: {goal}")
            needs = []
            for i in range(4):
                who, cnt = _i(r[22 + i]), _i(r[26 + i])
                if who:
                    nm = objectives.get(("npc", who) if who > 0 else ("go", -who), "")
                    if nm:
                        needs.append(f"{cnt} x {nm}" if cnt > 1 else nm)
            for i in range(6):
                it, cnt = _i(r[30 + i]), _i(r[36 + i])
                if it in items:
                    needs.append(f"{cnt} x {items[it].name}" if cnt > 1 else items[it].name)
            if needs:
                sents.append("Needs: " + ", ".join(needs) + ".")
            fixed = [items[i].short() for i in map(_i, r[12:16]) if i in items]
            choice = [items[i].short() for i in map(_i, r[16:22]) if i in items]
            if fixed:
                sents.append("Rewards: " + ", ".join(fixed) + ".")
            if choice:
                sents.append("Choice of: " + ", ".join(choice) + ".")
            for i in [*map(_i, r[12:16]), *map(_i, r[16:22])]:
                if i in items:
                    lst = rewards.setdefault(i, [])
                    if title not in lst:
                        lst.append(title)
            if prev in titles:
                sents.append(f"Follows: {titles[prev]}.")
            if nxt in titles:
                sents.append(f"Leads to: {titles[nxt]}.")
            story = quest_text(r[10])
            if story:
                sents.append("Text: " + _cap_words(story.replace("\n", " "), 90))
            refs = [f"quest:{qid}"] + [f"item:{i}" for i in [*map(_i, r[12:22])] if i in items]
            res.chunks.append(ChunkDraft(key=f"quest:{qid}", kind="quest", title=title,
                                         section=zone, text=" ".join(sents),
                                         ref_ids=tuple(dict.fromkeys(refs)), weight=QUEST_WEIGHT))
        res.log.append(f"{kept} quests ({len(rows) - kept} placeholder titles left out)")
        return rewards

    # item chunks

    def _item_chunks(self, items: dict[int, Item], n: ClientNames, drops, vendors, rewards,
                     res: SourceResult) -> None:
        skipped = 0
        for it in items.values():
            res.items.append(ItemRow(it.id, it.name, it.quality, it.ilvl, it.req_level, it.slot()))
            if _JUNK_NAME.search(it.name) or (it.quality == 0 and it.id not in drops):
                skipped += 1
                continue
            r = it.row
            q = QUALITY[it.quality] if 0 <= it.quality < len(QUALITY) else "unknown"
            slot = it.slot()
            sents = [f"{it.name}: {q} {slot or 'item'}"
                     + (f", item level {it.ilvl}" if it.ilvl and it.cls in (2, 4) else "")
                     + (f", requires level {it.req_level}" if it.req_level > 1 else "") + "."]
            bits = []
            if _BONDING.get(_i(r[9])):
                bits.append(_BONDING[_i(r[9])])
            if _i(r[21]) == 1 and it.cls in (2, 4):
                bits.append("unique")
            if _i(r[10]):
                bits.append(f"{_i(r[10])} armor")
            lo, hi, delay = _f(r[11]), _f(r[12]), _i(r[13])
            if hi:
                bits.append(f"{lo:g}-{hi:g} damage" + (f", speed {delay / 1000:.2f}" if delay else ""))
            stats = []
            for i in range(10):
                st, val = _i(r[27 + 2 * i]), _i(r[28 + 2 * i])
                if val and st in _STATS:
                    stats.append(f"{val:+d} {_STATS[st]}")
            if stats:
                bits.append(", ".join(stats))
            sockets = [_SOCKETS.get(_i(c)) for c in r[14:17] if _i(c)]
            if sockets:
                bits.append("sockets: " + ", ".join(s for s in sockets if s))
            if bits:
                line = "; ".join(bits)
                sents.append(line[:1].upper() + line[1:] + ".")
            effects = {0: "use", 1: "equip", 2: "chance on hit"}
            fx = [effects[_i(r[23 + 2 * k])] for k in range(2)
                  if _i(r[24 + 2 * k]) > 0 and _i(r[23 + 2 * k]) in effects]
            if fx and it.cls in (0, 2, 4, 15):
                sents.append("Has " + " and ".join(f"{e} effect" for e in dict.fromkeys(fx)) + ".")
            classes = [c for bit, c in _CLASS_NAMES.items() if _i(r[8]) > 0
                       and _i(r[8]) & (1 << (bit - 1))]
            if classes and len(classes) < len(_CLASS_NAMES):
                sents.append("Classes: " + ", ".join(classes) + ".")
            if _i(r[17]) and n.item_sets.get(_i(r[17])):
                sents.append(f"Part of the {n.item_sets[_i(r[17])]} set.")
            fac, rank = _i(r[19]), _i(r[20])
            if fac:
                fname = n.factions.get(fac, f"faction {fac}")
                rname = _REP_RANKS[rank] if 0 <= rank < len(_REP_RANKS) else ""
                sents.append(f"Requires {rname} with {fname}.".replace("  ", " "))
            if (desc := (r[18] or "").strip()):
                sents.append(f'"{desc}"')
            src = drops.get(it.id, [])
            if src:
                sents.append("Drops from: " + "; ".join(src[:6])
                             + (f"; and {len(src) - 6} more" if len(src) > 6 else "") + ".")
            if vendors.get(it.id):
                v = vendors[it.id]
                sents.append("Sold by " + "; ".join(v[:3]) + (" and others" if len(v) > 3 else "")
                             + ".")
            if rewards.get(it.id):
                w = rewards[it.id]
                sents.append("Reward from the quest " + "; ".join(w[:3])
                             + (" and others" if len(w) > 3 else "") + ".")
            if _i(r[22]):
                sents.append("Starts a quest.")
            res.chunks.append(ChunkDraft(key=f"item:{it.id}", kind="item", title=it.name,
                                         section=f"{q} {slot}".strip(), text=" ".join(sents),
                                         ref_ids=(f"item:{it.id}",), weight=it.weight()))
        res.log.append(f"{skipped} junk, placeholder or grey items left out")

    def _instance_chunks(self, n: ClientNames, res: SourceResult) -> None:
        by_map: dict[int, list] = {}
        for e in n.encounters.values():
            if e.difficulty == 0:
                by_map.setdefault(e.map_id, []).append(e)
        for map_id, encs in sorted(by_map.items()):
            m = n.maps.get(map_id)
            if m is None or m.instance_type not in (1, 2):
                continue
            kind = "dungeon" if m.instance_type == 1 else "raid"
            exp = _EXPANSIONS[m.expansion] if 0 <= m.expansion < len(_EXPANSIONS) else ""
            modes = sorted({d for (mid, d) in n.sizes if mid == map_id})
            labels = [self._difficulty(n, map_id, d) for d in modes]
            names = [e.name for e in sorted(encs, key=lambda e: (e.order, e.id))]
            text = (f"{m.name} is a {exp} {kind}".replace("  ", " ")
                    + (f" with {', '.join(x for x in labels if x)} modes" if labels else "")
                    + f". Its encounters, in order: {', '.join(names)}.")
            res.chunks.append(ChunkDraft(key=f"instance:{map_id}", kind="zone", title=m.name,
                                         section=kind, text=text, map_id=map_id,
                                         instance=m.name))
