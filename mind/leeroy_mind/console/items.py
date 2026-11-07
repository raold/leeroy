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
