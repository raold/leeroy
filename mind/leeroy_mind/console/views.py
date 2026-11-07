"""Helpers the tabs' read-only views share: which tables mind.db has, a query that reads as empty
on a table an older mind.db lacks, a meta value read as JSON and the members' names."""
import json
import sqlite3

from leeroy_mind.personas.schema import persona_from_dict


def tables(conn) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def rows(conn, sql: str, params=()) -> list[sqlite3.Row]:
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return []


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
    out = {r["guid"]: r["name"] for r in rows(conn, "SELECT guid, name FROM cast_member")}
    for r in rows(conn, "SELECT guid, json FROM persona_core"):
        if r["guid"] in out:
            continue
        try:
            out[r["guid"]] = persona_from_dict(json.loads(r["json"])).character_name
        except (TypeError, ValueError):
            continue
    return out
