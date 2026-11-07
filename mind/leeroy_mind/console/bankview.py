"""The Bank tab's view: the mind's bank snapshot (it ran the read-only SELECTs; the console never
sees a database credential) merged with member names, item names from library.db (opened read-only;
absent or unreadable means no names) and the ledger of what officers stocked or withdrew
(mind.db `realm_bank`, shown whatever the mind's state)."""
from pathlib import Path

from . import items as library_items
from . import views
from .feed import human_guid

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


def bank_view(conn, *, snapshot: dict | None, why: str | None, data_dir: Path) -> dict:
    missing = [t for t in ("realm_bank",) if t not in views.tables(conn)]
    if not (Path(data_dir) / "library.db").is_file():
        missing.append("library.db")  # item names fall back to "Item #id"
    ledger = [{"ts": r["ts"], "dir": r["dir"], "reason": r["reason"], "guid": r["guid"],
               "item_id": r["item_id"], "count": r["count"], "money": r["money"]}
              for r in views.rows(conn, "SELECT ts, dir, reason, guid, item_id, count, money "
                                        "FROM realm_bank ORDER BY ts DESC, id DESC LIMIT ?",
                                  (LEDGER_MAX,))]
    ids = {row["item_id"] for row in ledger}
    if snapshot:
        ids |= {s[1] for t in snapshot["tabs"] for s in t["slots"]}
    known = library_items.item_names(Path(data_dir) / "library.db", ids)
    return {"human": human_guid(conn),
            "names": {str(g): n for g, n in views.names(conn).items()}, "missing": missing,
            "bank": snapshot, "why": why,
            "items": {str(k): v for k, v in known.items()}, "ledger": ledger}
