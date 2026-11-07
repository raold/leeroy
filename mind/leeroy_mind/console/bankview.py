"""The Bank tab's view: the mind's bank snapshot (it ran the read-only SELECTs; the console never
sees a database credential) merged with member names, item names from library.db (opened read-only;
absent or unreadable means no names) and the ledger of what officers stocked or withdrew
(mind.db `realm_bank`, shown whatever the mind's state)."""
import sqlite3
from contextlib import closing
from pathlib import Path

from . import views

LEDGER_MAX = 100


def _int(x) -> bool:
    return type(x) is int and x >= 0


def clean_snapshot(reply: dict) -> dict | None:
    """The fields of the mind's answer the page uses, or None when their shape is wrong."""
    tabs, log = reply.get("tabs"), reply.get("log")
    if not (_int(reply.get("money")) and isinstance(tabs, list) and isinstance(log, list)):
        return None
    out_tabs = []
    for t in tabs:
        if not (isinstance(t, dict) and _int(t.get("tab")) and isinstance(t.get("name"), str)
                and isinstance(t.get("slots"), list)
                and all(isinstance(s, list) and len(s) == 3 and all(_int(v) for v in s)
                        for s in t["slots"])):
            return None
        out_tabs.append({"tab": t["tab"], "name": t["name"], "slots": t["slots"]})
    if not all(isinstance(r, list) and len(r) == 4 and all(_int(v) for v in r) for r in log):
        return None
    read_at, guild = reply.get("read_at"), reply.get("guild")
    return {"money": reply["money"], "tabs": out_tabs, "log": log,
            "read_at": read_at if isinstance(read_at, (int, float)) else 0,
            "guild": guild if _int(guild) else 0}


def item_names(path: Path, ids) -> dict[str, dict]:
    """{str(id): {"name", "quality"}} for the ids library.db knows; {} when it can't say."""
    ids = sorted(set(ids))
    if not ids or not Path(path).is_file():
        return {}
    out: dict[str, dict] = {}
    try:
        with closing(sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True)) as c:
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                for r in c.execute("SELECT id, name, quality FROM item WHERE id IN (%s)"
                                   % ",".join("?" * len(chunk)), chunk):
                    out[str(r[0])] = {"name": str(r[1]), "quality": int(r[2])}
    except (sqlite3.Error, ValueError):
        return {}
    return out


def bank_view(conn, *, snapshot: dict | None, why: str | None, data_dir: Path) -> dict:
    missing = [t for t in ("realm_bank",) if t not in views.tables(conn)]
    ledger = [{"ts": r["ts"], "dir": r["dir"], "reason": r["reason"], "guid": r["guid"],
               "item_id": r["item_id"], "count": r["count"], "money": r["money"]}
              for r in views.rows(conn, "SELECT ts, dir, reason, guid, item_id, count, money "
                                        "FROM realm_bank ORDER BY ts DESC, id DESC LIMIT ?",
                                  (LEDGER_MAX,))]
    ids = {row["item_id"] for row in ledger}
    if snapshot:
        ids |= {s[1] for t in snapshot["tabs"] for s in t["slots"]}
    return {"names": {str(g): n for g, n in views.names(conn).items()}, "missing": missing,
            "bank": snapshot, "why": why,
            "items": item_names(Path(data_dir) / "library.db", ids), "ledger": ledger}
