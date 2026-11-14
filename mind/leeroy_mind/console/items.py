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


def tooltips(library: Path, names) -> dict[str, dict]:
    """{written name: {"name", "quality", "ilvl", "slot"}} for names the item table holds exactly
    (any case, as Library.lookup_item's exact branch; a name several items share shows the highest
    item level). Never a word-match fallback: a miss stays plain text. Junk, test items and the
    names of bosses, raids, dungeons, classes and races are never items here (talk/items.py)."""
    from leeroy_mind.library.world import _JUNK_NAME
    from leeroy_mind.talk.items import _NOT_ITEMS, _TEST_ITEM, key
    wanted = {}
    for name in names:
        if (isinstance(name, str) and name.strip() and key(name) not in _NOT_ITEMS
                and not _JUNK_NAME.search(name) and not _TEST_ITEM.search(name)):
            wanted.setdefault(_norm(name), []).append(name)
    if not wanted or not Path(library).is_file():
        return {}
    found: dict[str, dict] = {}
    try:
        with closing(sqlite3.connect(f"{Path(library).resolve().as_uri()}?mode=ro", uri=True)) as c:
            keys = sorted(wanted)
            for i in range(0, len(keys), 500):
                chunk = keys[i:i + 500]
                rows = c.execute("SELECT name, quality, ilvl, slot FROM item WHERE name COLLATE NOCASE IN (%s) "
                                 "ORDER BY ilvl DESC, quality DESC, id" % ",".join("?" * len(chunk)), chunk)
                for r in rows:
                    for written in wanted.get(_norm(str(r[0])), ()):
                        found.setdefault(written, {"name": str(r[0]), "quality": int(r[1]),
                                                   "ilvl": int(r[2]), "slot": str(r[3] or "")})
    except (sqlite3.Error, ValueError, TypeError):
        return {}
    return found
