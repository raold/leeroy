"""AtlasLoot's Wrath of the Lich King tables (the client addon, read-only) as library chunks: one
per boss and difficulty, plus the faction, PvP and collection tables. Item names are checked
against the world database's item table when it has been built."""
import re
from dataclasses import dataclass
from pathlib import Path

from leeroy_mind.library.chunks import ChunkDraft, SourceResult
from leeroy_mind.library.lua import LuaTable, assignments

DEFAULT_ADDONS = Path(r"F:\azerothcore\Wow355a\Interface\AddOns")
WOTLK_DIR = "AtlasLoot_WrathoftheLichKing"
REGISTRY = Path("AtlasLoot") / "TableRegister" / "loottables.en.lua"
TEXT_PARSING = Path("AtlasLoot") / "Core" / "TextParsing.lua"

_BANNER = re.compile(r"^(\t*)---\s+(.+?)\s+---\s*$", re.MULTILINE)
_TABLE = re.compile(r'AtlasLoot_Data\["([^"]+)"\]\s*=')
_CODE = re.compile(r"#[a-z0-9_]+#")
_QUALITY = re.compile(r"=q(\d)=|=ec1=|=ds=")
_GSUB = re.compile(r'gsub\(text,\s*"(#[a-z0-9_]+#)",\s*(?:AL|Babble\w+)\["([^"]+)"\]\)')
_GSUB_CLASS = re.compile(r'gsub\(text,\s*"(#[a-z0-9_]+#)",\s*LOCALIZED_CLASS_NAMES_MALE\["(\w+)"\]')
_GSUB_SPELL = re.compile(r'gsub\(text,\s*"(#[a-z0-9_]+#)",\s*\(GetSpellInfo\(\d+\)\)\);\s*--\s*(.+)')

# Currency and icon codes are textures in the addon; these are what the icons stand for.
_ICON_CODES = {
    "#faction#": "honor", "#arena#": "arena points", "#gold#": "gold", "#silver#": "silver",
    "#copper#": "copper", "#eofheroism#": "Emblem of Heroism", "#eofvalor#": "Emblem of Valor",
    "#eofconquest#": "Emblem of Conquest", "#eoftriumph#": "Emblem of Triumph",
    "#eoffrost#": "Emblem of Frost", "#trophyofthecrusade#": "Trophy of the Crusade",
    "#champseal#": "Champion's Seal", "#champwrit#": "Champion's Writ",
    "#wintergrasp#": "Stone Keeper's Shard", "#wintergraspmark#": "Wintergrasp Mark of Honor",
    "#venturecoin#": "Venture Coin", "#winterfinclam#": "Winterfin Clam",
    "#horde#": "Horde", "#alliance#": "Alliance", "#t10mark#": "Mark of Sanctification",
    "#heroic#": "Badge of Justice", "#darkmoon#": "Darkmoon Faire Prize Ticket",
    "#brewfest#": "Brewfest Prize Token", "#fireflower#": "Burning Blossom",
    "#valentineday#": "Love Token", "#reqrating#": "rating"}
# Used when TextParsing.lua is not there; the file, when present, is the authority.
_FALLBACK_CODES = {
    "#j1#": "Normal Mode", "#j2#": "Raid", "#j3#": "Heroic Mode", "#j37#": "10 Man",
    "#j38#": "25 Man", "#j46#": "Hard Mode", "#j47#": "Heroic", "#j53#": "Hard Mode",
    "#a1#": "Cloth", "#a2#": "Leather", "#a3#": "Mail", "#a4#": "Plate", "#s1#": "Head",
    "#s2#": "Neck", "#s3#": "Shoulder", "#s4#": "Back", "#s5#": "Chest", "#s6#": "Shirt",
    "#s7#": "Tabard", "#s8#": "Wrist", "#s9#": "Hands", "#s10#": "Waist", "#s11#": "Legs",
    "#s12#": "Feet", "#s13#": "Ring", "#s14#": "Trinket", "#s15#": "Held in Off-Hand",
    "#s16#": "Relic", "#h1#": "One-Hand", "#h2#": "Two-Hand", "#h3#": "Main Hand",
    "#h4#": "Off Hand", "#w1#": "Axe", "#w2#": "Bow", "#w3#": "Crossbow", "#w4#": "Dagger",
    "#w5#": "Gun", "#w6#": "Mace", "#w7#": "Polearm", "#w8#": "Shield", "#w9#": "Staff",
    "#w10#": "Sword", "#w11#": "Thrown", "#w12#": "Wand", "#w13#": "Fist Weapon",
    "#w14#": "Idol", "#w15#": "Totem", "#w16#": "Libram", "#w21#": "Sigil", "#e1#": "Bag",
    "#e6#": "Trade Goods", "#e7#": "Gem", "#e8#": "Reagent", "#e9#": "Key", "#e12#": "Mount",
    "#e13#": "Pet", "#e15#": "Token", "#e17#": "Enchant", "#m2#": "This Item Begins a Quest",
    "#m3#": "Quest Item", "#m4#": "Quest Reward", "#m6#": "Horde", "#m7#": "Alliance",
    "#r1#": "Neutral", "#r2#": "Friendly", "#r3#": "Honored", "#r4#": "Revered",
    "#r5#": "Exalted", "#p2#": "Blacksmithing", "#p5#": "Engineering", "#p7#": "Leatherworking",
    "#p8#": "Tailoring", "#p12#": "Jewelcrafting"}

RAIDS = frozenset({"Naxxramas", "The Obsidian Sanctum", "The Eye of Eternity",
                   "Vault of Archavon", "Ulduar", "Trial of the Crusader", "Onyxia's Lair",
                   "Icecrown Citadel", "The Ruby Sanctum"})
_RENAME = {"Obsidian Sanctum": "The Obsidian Sanctum", "Ruby Sanctum": "The Ruby Sanctum",
           "Onyxia (Level 80)": "Onyxia's Lair",
           "Caverns of Time: Old Stratholme": "The Culling of Stratholme",
           "Trial of the Grand Crusader": "Trial of the Crusader",
           "Icecrown Citadel HEROIC": "Icecrown Citadel"}
_WINGS = {"Naxxramas": {"Construct Quarter", "Arachnid Quarter", "Military Quarter",
                        "Plague Quarter", "Frostwyrm Lair"},
          "Icecrown Citadel": {"Lower Spire", "The Plagueworks", "The Crimson Hall",
                               "The Frozen Halls", "The Frozen Throne"}}
_CATEGORY_LOOT = "Dungeons & Raids"


def load_codes(text_parsing: str | None) -> dict[str, str]:
    """Code -> enUS text from AtlasLoot's TextParsing.lua (AL/Babble keys are the enUS text,
    class names come from the token, professions from the line comment), over the fallback."""
    codes = dict(_FALLBACK_CODES)
    if text_parsing:
        codes.update({m.group(1): m.group(2) for m in _GSUB.finditer(text_parsing)})
        codes.update({m.group(1): m.group(2).title().replace("Deathknight", "Death Knight")
                      for m in _GSUB_CLASS.finditer(text_parsing)})
        codes.update({m.group(1): m.group(2).strip() for m in _GSUB_SPELL.finditer(text_parsing)})
    codes.update(_ICON_CODES)
    return codes


def decode(text, codes: dict[str, str]) -> str:
    """Strip colour codes and replace #codes# with their text."""
    if not isinstance(text, str):
        return ""
    text = _QUALITY.sub("", text)
    text = re.sub(r"#reqrating#\s*(\d+)", r"(requires \1 rating)", text)
    text = _CODE.sub(lambda m: codes.get(m.group(0), ""), text)
    return " ".join(text.split()).strip(" ,")


@dataclass
class _Place:
    category: str
    instance: str | None
    heroic_banner: bool


def table_places(src: str) -> dict[str, _Place]:
    """Each table key's category and instance, from the banner comments above it."""
    src = re.sub(r"--\[(=*)\[.*?\]\1\]", "", src, flags=re.DOTALL)  # block comments
    events = sorted([(m.start(), "banner", m) for m in _BANNER.finditer(src)]
                    + [(m.start(), "table", m) for m in _TABLE.finditer(src)],
                    key=lambda e: e[0])
    category, instance, heroic = "", None, False
    out: dict[str, _Place] = {}
    for _, kind, m in events:
        if kind == "banner":
            depth, title = len(m.group(1)), m.group(2).strip()
            if depth <= 1:
                category, instance, heroic = title, None, False
            elif instance is not None and title in _WINGS.get(instance, ()):
                continue
            else:
                heroic = "HEROIC" in title or "Grand Crusader" in title
                instance = _RENAME.get(title, title)
        else:
            out.setdefault(m.group(1), _Place(category, instance, heroic))
    return out


def difficulty(key: str, place: _Place) -> str:
    """'25-player heroic', '10-player', or '' for a dungeon (its headers say Normal/Heroic)."""
    if place.category != _CATEGORY_LOOT or place.instance not in RAIDS:
        return ""
    size = "25" if "25Man" in key else "10"
    heroic = "HEROIC" in key or place.heroic_banner
    return f"{size}-player" + (" heroic" if heroic else "")


def _faction_suffix(key: str) -> str:
    return " (Alliance)" if key.endswith("_A") else " (Horde)" if key.endswith("_H") else ""


class AtlasLootSource:
    name = "atlasloot"

    def __init__(self, addons_dir: Path = DEFAULT_ADDONS, *, per_chunk: int = 30):
        self.addons = Path(addons_dir)
        self.per_chunk = per_chunk

    def _read(self, rel: Path) -> str | None:
        p = self.addons / rel
        try:
            return p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    def load(self, library=None) -> SourceResult:
        res = SourceResult(self.name)
        codes = load_codes(self._read(TEXT_PARSING))
        names: dict[str, object] = {}
        reg = self._read(REGISTRY)
        if reg:
            names = assignments(reg, {"AtlasLoot_TableNames"})[0]["AtlasLoot_TableNames"]
        else:
            res.log.append(f"no {REGISTRY}: table keys stand in for boss names")
        files = sorted((self.addons / WOTLK_DIR).glob("*.lua"))
        if not files:
            res.log.append(f"no AtlasLoot tables under {self.addons / WOTLK_DIR}")
            return res
        tables: list[tuple[str, LuaTable, _Place]] = []
        for f in files:
            src = f.read_text(encoding="utf-8", errors="replace")
            data, errors = assignments(src, {"AtlasLoot_Data"})
            res.log += [f"{f.name}: {e}" for e in errors]
            places = table_places(src)
            for key, tbl in data["AtlasLoot_Data"].items():
                if isinstance(tbl, LuaTable):
                    tables.append((key, tbl, places.get(key, _Place("", None, False))))
        ids = [e.array[1] for _, t, _ in tables for e in t.array
               if isinstance(e, LuaTable) and len(e.array) > 1 and isinstance(e.array[1], int)]
        known = library.item_info(ids) if library is not None else {}
        if not known:
            res.log.append("no world item table yet: AtlasLoot names used as they are")
        for key, tbl, place in tables:
            res.chunks += self._chunks(key, tbl, place, names, codes, known, res)
        res.log.append(f"{len(tables)} AtlasLoot tables")
        return res

    def _chunks(self, key, tbl: LuaTable, place: _Place, names, codes, known, res) -> list:
        reg = names.get(key)
        title = reg.array[0] if isinstance(reg, LuaTable) and reg.array and \
            isinstance(reg.array[0], str) else None
        title = (title or (place.instance or key)).strip()
        title = re.sub(r"\s*\((?:Heroic|10 Man|25 Man)\)$", "", title) + _faction_suffix(key)
        diff = difficulty(key, place)
        loot_place = place.category == _CATEGORY_LOOT
        groups: list[tuple[str, list[tuple[int, str]]]] = [("", [])]
        refs: list[str] = []
        for e in tbl.array:
            if not isinstance(e, LuaTable) or len(e.array) < 4:
                continue
            item, name = e.array[1], e.array[3]
            if not isinstance(name, str):
                continue
            if not item or item == 0:
                header = decode(name, codes)
                if header:
                    groups.append((header, []))
                continue
            desc = decode(e.array[4] if len(e.array) > 4 else "", codes)
            extra = [decode(x, codes) for x in e.array[5:7] if isinstance(x, str) and x.strip()]
            shown = decode(name, codes)
            if isinstance(item, int) and item in known:
                row = known[item]
                shown = row["name"]
                if row["ilvl"] and row["slot"]:
                    desc = f"{row['ilvl']} {row['slot']}"
                refs.append(f"item:{item}")
            elif isinstance(item, int):
                refs.append(f"item:{item}")
            bits = [b for b in [desc, *extra] if b]
            groups[-1][1].append((item if isinstance(item, int) else 0,
                                  shown + (f" ({'; '.join(bits)})" if bits else "")))
        groups = [(h, items) for h, items in groups if items]
        out = []
        for gi, (header, items) in enumerate(groups):
            section = ", ".join(x for x in (diff, header) if x) or ""
            where = ", ".join(x for x in (place.instance if loot_place else None, section) if x)
            if loot_place:  # the words players ask with: drops, loot table
                head = f"What {title} drops ({where + ' ' if where else ''}loot table in AtlasLoot):"
            else:
                head = (f"{title} ({where})" if where else title) + " items in AtlasLoot:"
            for part in range(0, len(items), self.per_chunk):
                chunk = items[part:part + self.per_chunk]
                n = part // self.per_chunk
                text = f"{head} " + ", ".join(s for _, s in chunk) + "."
                ck = f"atlas:{key}:{gi}" + (f":{n}" if n else "")
                out.append(ChunkDraft(
                    key=ck, kind="loot", title=title,
                    section=section + (f" (part {n + 1})" if n else ""), text=text,
                    instance=place.instance if loot_place else None,
                    ref_ids=tuple(dict.fromkeys([f"atlas:{key}"]
                                                + [f"item:{i}" for i, _ in chunk if i]))))
        if not out:
            res.log.append(f"empty AtlasLoot table {key}")
        return out
