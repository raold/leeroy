"""What each raider would want from the guild's next raid, by the loot system's own rule.

Gear lives in the game's database, never in mind.db, so the wishes are worked out off the
event loop (library drops, then the guild armory's item and equipment reads) and cached in
meta for the Discord seeds to read. Nothing is guessed: no BiS lists, no legendary progress
(neither has any data here), and a member whose equipment cannot be read wishes for nothing."""
from dataclasses import dataclass
import json
import math
import re

from leeroy_mind.guild.raids import CATALOG
from leeroy_mind.guild.roles import role_of, stat_family
from leeroy_mind.guild.upgrades import upgrade_gain
from leeroy_mind.wow import BOSS_NAMES

WISH_KEY = "culture:loot:wish"
WISH_REFRESH_S = 12 * 3600.0
WISH_AHEAD_S = 3 * 86400.0
RAID_RECENT_S = 14 * 86400.0
RAIDERS_FROM = 4
MAX_RAIDERS = 40
MIN_QUALITY = 4


@dataclass(frozen=True, slots=True)
class Target:
    raid_key: str
    map_id: int
    size: int
    heroic: bool


def next_raid(conn, now: float) -> int | None:
    """The next raid night the calendar desk posted, within WISH_AHEAD_S."""
    row = conn.execute("SELECT id FROM realm_calendar WHERE state='posted' AND kind='raid' "
                       "AND at BETWEEN ? AND ? ORDER BY at,id LIMIT 1", (now, now + WISH_AHEAD_S)).fetchone()
    return row[0] if row else None


def target_raid(conn, now: float) -> Target | None:
    """A raid night names no instance; the guild's newest raid within RAID_RECENT_S says which
    raid it is running now."""
    row = conn.execute("SELECT raid_key,size,heroic FROM guild_raid WHERE started BETWEEN ? AND ? "
                       "ORDER BY started DESC,raid_id DESC LIMIT 1", (now - RAID_RECENT_S, now)).fetchone()
    raid = CATALOG.get(row[0]) if row and isinstance(row[0], str) else None
    if raid is None or row[1] not in raid.sizes or not any(m == raid.map_id for m, _ in BOSS_NAMES):
        return None
    return Target(raid.key, raid.map_id, row[1], bool(row[2]))


def raiders(conn, human: int) -> list[int]:
    """Members who raided in the guild's last RAIDERS_FROM ended raids."""
    ids = [r[0] for r in conn.execute("SELECT raid_id FROM guild_raid WHERE ended IS NOT NULL "
                                      "ORDER BY ended DESC,raid_id DESC LIMIT ?", (RAIDERS_FROM,))]
    if not ids:
        return []
    rows = conn.execute("SELECT DISTINCT guid FROM guild_attendance WHERE status='raided' AND raid_id IN "
                        "(SELECT value FROM json_each(?)) ORDER BY guid", (json.dumps(ids),))
    return [g for g, in rows if type(g) is int and g > 0 and g != human][:MAX_RAIDERS]


def _difficulty(section, size: int, heroic: bool) -> bool:
    """world.py's loot sections: '<size>-player', ' heroic' from difficulty 2, ' (part n)'."""
    found = re.fullmatch(r"(10|25)-player( heroic)?(?: \(part [0-9]+\))?", section or "")
    return found is not None and int(found[1]) == size and bool(found[2]) == heroic


def _bare(name) -> str:
    return re.sub(r"^the\s+", "", " ".join(str(name).split()).casefold())


def boss_drops(library, target: Target) -> dict[str, tuple[int, ...]]:
    """{boss: item ids} from the library's loot chunks of that raid map and difficulty whose
    title names the boss (its loot or its chest): drops_from's word-match fallback can return
    another source, and an item is never put on a boss that does not drop it."""
    out = {}
    for (map_id, _), boss in sorted(BOSS_NAMES.items()):
        if map_id != target.map_id:
            continue
        ids = []
        for chunk in library.drops_from(boss):
            if (chunk.map_id != target.map_id or _bare(boss) not in _bare(chunk.title)
                    or not _difficulty(chunk.section, target.size, target.heroic)):
                continue
            ids += [int(ref[5:]) for ref in chunk.ref_ids
                    if isinstance(ref, str) and re.fullmatch(r"item:[1-9][0-9]{0,9}", ref)]
        if ids:
            out[boss] = tuple(dict.fromkeys(ids))
    return out


def compute_wishes(conn, library, armory, *, now: float, raid_roles: dict[int, str | None],
                   human: int) -> dict | None:
    """The cache value for WISH_KEY, or None with no raid night posted ahead. A wish is a raider's
    largest upgrade_gain among the drops of the target raid's bosses; ties go to the lower item id."""
    event = next_raid(conn, now)
    if event is None:
        return None
    value = {"computed": now, "event": event, "raid_key": None, "wishes": []}
    target = target_raid(conn, now)
    if target is None:
        return value
    value.update(raid_key=target.raid_key, size=target.size, heroic=target.heroic)
    cast = {g: (cls, spec) for g, cls, spec in conn.execute("SELECT guid,class,spec_tab FROM cast_member")}
    members = [g for g in raiders(conn, human) if g in cast and g in raid_roles]
    drops = boss_drops(library, target)
    if not members or not drops:
        return value
    items = armory.items(sorted({i for ids in drops.values() for i in ids}))
    equipped = armory.equipped(tuple(members))
    for guid in members:
        worn = equipped.get(guid) or {}
        if not worn:
            continue  # no equipment read: every slot would look like an upgrade
        cls, spec = cast[guid]
        family = stat_family(cls, spec, role_of(cls, spec, raid_roles[guid]))
        best = None
        for boss, ids in drops.items():
            for item_id in ids:
                item = items.get(item_id)
                if item is None or item.quality < MIN_QUALITY:
                    continue
                gain = upgrade_gain(item, cls, family, worn)
                if gain is not None and gain > 0 and (best is None or (-gain, item_id) < (-best[2], best[0])):
                    best = (item_id, boss, gain)
        if best is not None:
            value["wishes"].append({"guid": guid, "item_id": best[0], "boss": best[1], "gain": best[2]})
    return value


def load_wishes(raw) -> dict | None:
    """WISH_KEY's value checked; None when it cannot be read."""
    try:
        value = json.loads(raw) if isinstance(raw, str) else None
    except ValueError:
        return None
    if (not isinstance(value, dict) or type(value.get("event")) is not int
            or type(value.get("computed")) not in (int, float) or not math.isfinite(value["computed"])
            or not isinstance(value.get("wishes"), list)):
        return None
    bosses = set(BOSS_NAMES.values())
    value["wishes"] = [w for w in value["wishes"][:MAX_RAIDERS] if isinstance(w, dict)
                       and type(w.get("guid")) is int and type(w.get("item_id")) is int and w["item_id"] > 0
                       and w.get("boss") in bosses and type(w.get("gain")) is int and w["gain"] > 0]
    return value


def wish_due(cached: dict | None, event: int | None, now: float) -> bool:
    return event is not None and (cached is None or cached["event"] != event
                                  or not 0 <= now - cached["computed"] < WISH_REFRESH_S)
