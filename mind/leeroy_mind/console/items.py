"""Item names from library.db's `item` table, opened read-only for one question and closed."""
import sqlite3
from contextlib import closing
from pathlib import Path


def item_names(library: Path, ids) -> dict[int, dict]:
    """{id: {"name", "quality"}} for the ids library.db knows; {} when it can't say."""
    ids = sorted({i for i in ids if type(i) is int})
    if not ids or not Path(library).is_file():
        return {}
    out: dict[int, dict] = {}
    try:
        with closing(sqlite3.connect(f"{Path(library).resolve().as_uri()}?mode=ro", uri=True)) as c:
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                for r in c.execute("SELECT id, name, quality FROM item WHERE id IN (%s)"
                                   % ",".join("?" * len(chunk)), chunk):
                    out[int(r[0])] = {"name": str(r[1]), "quality": int(r[2])}
    except (sqlite3.Error, ValueError, TypeError):
        return {}
    return out


MESSAGE_ITEMS = 8  # tooltips a message carries at most


def _norm(name: str) -> str:
    return " ".join(name.replace("’", "'").split()).casefold()


def bracketed(text) -> list[str]:
    """The [Name] spans of a message as written, first MESSAGE_ITEMS distinct ones."""
    from leeroy_mind.talk.items import _BRACKET
    if not isinstance(text, str):
        return []
    return list(dict.fromkeys(m.group(1) for m in _BRACKET.finditer(text)))[:MESSAGE_ITEMS]


def tooltips(library: Path, names, *, prefer_ids=(), raid=None) -> dict[str, dict]:
    """{written name: {"name", "quality", "ilvl", "slot"}} for names the item table holds exactly
    (any case, as Library.lookup_item's exact branch). Never a word-match fallback: a miss stays
    plain text. Junk, test items and the names of bosses, raids, dungeons, classes and races are
    never items here (talk/items.py).

    Several items share a name across 10/25 and normal/heroic. The guild's own item (an id in
    `prefer_ids`: its loot receipts, awards and wishes) wins, else the copy that drops at `raid`'s
    (size, heroic) by the library's loot chunks; failing both, "ilvl" is None and "ilvls" lists
    the levels rather than guessing one."""
    from leeroy_mind.culture.loot import _difficulty
    from leeroy_mind.library.world import _JUNK_NAME
    from leeroy_mind.talk.items import _NOT_ITEMS, _TEST_ITEM, key
    wanted = {}
    for name in names:
        if (isinstance(name, str) and name.strip() and key(name) not in _NOT_ITEMS
                and not _JUNK_NAME.search(name) and not _TEST_ITEM.search(name)):
            wanted.setdefault(_norm(name), []).append(name)
    if not wanted or not Path(library).is_file():
        return {}
    prefer = {i for i in prefer_ids if type(i) is int}
    found: dict[str, dict] = {}
    try:
        with closing(sqlite3.connect(f"{Path(library).resolve().as_uri()}?mode=ro", uri=True)) as c:
            groups: dict[str, list] = {}
            keys = sorted(wanted)
            for i in range(0, len(keys), 500):
                chunk = keys[i:i + 500]
                for r in c.execute("SELECT id, name, quality, ilvl, slot FROM item WHERE name COLLATE NOCASE IN (%s) "
                                   "ORDER BY quality DESC, ilvl DESC, id" % ",".join("?" * len(chunk)), chunk):
                    groups.setdefault(_norm(str(r[1])), []).append(r)
            shared = [r[0] for rows in groups.values() if len({r[3] for r in rows}) > 1 for r in rows]
            sections: dict[int, set] = {}
            if shared and raid is not None:
                for i in range(0, len(shared), 500):
                    chunk = [f"item:{n}" for n in shared[i:i + 500]]
                    try:
                        for ref, section in c.execute(
                                "SELECT r.ref, c.section FROM chunk_ref r JOIN chunks c ON c.id = r.chunk_id "
                                "WHERE c.kind = 'loot' AND r.ref IN (%s)" % ",".join("?" * len(chunk)), chunk):
                            sections.setdefault(int(ref[5:]), set()).add(section)
                    except sqlite3.OperationalError:
                        sections = {}
                        break
            for k, rows in groups.items():
                levels = sorted({int(r[3]) for r in rows})
                pick, ilvl = rows[0], levels[0] if len(levels) == 1 else None
                if ilvl is None:
                    for chosen in ([r for r in rows if r[0] in prefer],
                                   [r for r in rows if raid is not None and any(
                                       _difficulty(s, raid[0], raid[1]) for s in sections.get(r[0], ()))]):
                        if chosen and len({r[3] for r in chosen}) == 1:
                            pick, ilvl = chosen[0], int(chosen[0][3])
                            break
                info = {"name": str(pick[1]), "quality": int(pick[2]), "ilvl": ilvl, "slot": str(pick[4] or "")}
                if ilvl is None:
                    info["ilvls"] = levels
                for written in wanted.get(k, ()):
                    found[written] = info
    except (sqlite3.Error, ValueError, TypeError):
        return {}
    return found


def guild_items(conn, now: float) -> tuple[set[int], tuple[int, bool] | None]:
    """(the item ids the guild's own records name: loot receipts and awards of the last 30 days,
    and the raiders' wishes; the (size, heroic) of the raid it is running), read-only."""
    import json
    from leeroy_mind.culture.loot import WISH_KEY, load_wishes, target_raid
    ids: set[int] = set()
    try:
        ids.update(r[0] for r in conn.execute("SELECT DISTINCT item_id FROM dkp_ledger WHERE reason='item' "
                                              "AND item_id IS NOT NULL AND ts>=?", (now - 30 * 86400,)))
        for (raw,) in conn.execute("SELECT value FROM meta WHERE key GLOB 'guild:loot:*'"):
            try:
                record = json.loads(raw)
            except ValueError:
                continue
            if (isinstance(record, dict) and type(record.get("item_id")) is int
                    and type(record.get("received")) in (int, float) and record["received"] >= now - 30 * 86400):
                ids.add(record["item_id"])
        row = conn.execute("SELECT value FROM meta WHERE key=?", (WISH_KEY,)).fetchone()
        wishes = load_wishes(row[0]) if row else None
        ids.update(w["item_id"] for w in (wishes or {}).get("wishes", ()))
        target = target_raid(conn, now)
    except sqlite3.Error:
        return ids, None
    return ids, ((target.size, target.heroic) if target is not None else None)
