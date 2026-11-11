"""Helpers the tabs' read-only views share: which tables mind.db has, a query that reads as empty
on a table an older mind.db lacks, a meta value read as JSON and the members' names."""
import json
import sqlite3

from leeroy_mind.personas.schema import persona_from_dict
from leeroy_mind.personas.tiers import is_cast


def tables(conn) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def rows(conn, sql: str, params=()) -> list[sqlite3.Row]:
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError as e:
        if str(e).startswith(("no such table", "no such column")):
            return []
        raise


def meta_json(conn, key: str):
    r = rows(conn, "SELECT value FROM meta WHERE key = ?", (key,))
    if not r:
        return None
    try:
        return json.loads(r[0][0])
    except (TypeError, ValueError):
        return None


def names(conn) -> dict[int, str]:
    """guid -> name: the cast's, then the character names of stored personas not in the cast."""
    out = {r["guid"]: r["name"] for r in rows(conn, "SELECT guid, name FROM cast_member") if is_cast(conn,r["guid"])}
    for r in rows(conn, "SELECT guid, json FROM persona_core"):
        if not is_cast(conn,r["guid"]):
            continue
        if r["guid"] in out:
            continue
        try:
            out[r["guid"]] = persona_from_dict(json.loads(r["json"])).character_name
        except (TypeError, ValueError):
            continue
    return out
