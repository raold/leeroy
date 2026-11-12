"""The Personas tab's profile photos, read from mind.db and the selfie files read-only (asked
through feed.read). The running mind writes the rows and files through its control endpoint's
`selfie` op; the console only reads them:

- queue_view: the sets waiting for his choice, how many members have a photo, are waiting, are
  queued for the night's render or cannot be rendered yet (no persona, no looks, no photo
  kind), and the newest changes with each one's undoability;
- set_images: one ready set's four thumbnails as data URLs;
- member_photos: one member's photo in force, sets and change history.

Images are read only from `files` paths, which are built from integers, and a file over
MAX_IMAGE_BYTES is left out. A mind.db without the selfie tables says so in `missing`."""
import base64
import json
from pathlib import Path

from leeroy_mind.selfies import files, store
from leeroy_mind.personas.tiers import is_cast
from leeroy_mind.wow import CAST_MIN_LEVEL

from .feed import human_guid

TABLES = ("selfie_set", "selfie_candidate", "selfie_pick", "selfie_current")
MAX_IMAGE_BYTES = 300 * 1024
RECENT = 20


def _have(conn) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _missing(conn) -> list[str]:
    have = _have(conn)
    return [t for t in TABLES if t not in have]


def _names(conn) -> dict[int, str]:
    try:
        return {r[0]: r[1] for r in conn.execute("SELECT guid, name FROM cast_member")}
    except Exception:
        return {}


def _kinds(conn, human: int) -> dict[str, int]:
    """Cast members (level 80 or more, not the owner) by what keeps the night from rendering."""
    out = {"no_persona": 0, "no_looks": 0, "no_kind": 0}
    have = _have(conn)
    if "cast_member" not in have:
        return out
    stored = {}
    if "persona_core" in have:
        for guid, text in conn.execute("SELECT guid, json FROM persona_core"):
            if not is_cast(conn,guid):
                continue
            try:
                stored[guid] = json.loads(text)
            except (ValueError, TypeError):
                stored[guid] = {}
    for (guid,) in conn.execute("SELECT guid FROM cast_member WHERE level >= ? AND guid != ?",
                                (CAST_MIN_LEVEL, human)):
        if not is_cast(conn,guid):
            continue
        p = stored.get(guid)
        if p is None:
            out["no_persona"] += 1
        elif not isinstance(p, dict) or p.get("appearance") is None:
            out["no_looks"] += 1
        elif p.get("photo") is None:
            out["no_kind"] += 1
    return out


def queue_view(conn, data_dir: Path, *, now: float) -> dict:
    """The photo queue as the page shows it. `data_dir` and `now` are for the page's other
    questions; the queue itself is rows only."""
    missing = _missing(conn)
    out = {"available": not missing, "missing": missing,
           "counts": {"chosen": 0, "waiting": 0, "queued": 0, "no_persona": 0, "no_looks": 0,
                      "no_kind": 0},
           "queue": [], "recent": []}
    if missing:
        return out
    names = _names(conn)
    out["counts"].update(store.counts(conn))
    out["counts"].update(_kinds(conn, human_guid(conn)))
    out["queue"] = [{"guid": r[0], "name": names.get(r[0], f"#{r[0]}"), "set": r[1],
                     "reason": r[2], "rendered": r[3]}
                    for r in conn.execute(
                        "SELECT guid, set_no, reason, rendered FROM selfie_set WHERE "
                        "status = 'ready' AND decided IS NULL ORDER BY rendered, guid, set_no") if is_cast(conn,r[0])]
    out["recent"] = [{**p, "name": names.get(p["guid"], f"#{p['guid']}")}
                     for p in store.picks(conn, None, RECENT)]
    return out


def _data_url(path: Path, root: Path) -> str | None:
    from .faces import read_image
    data = read_image(path, root, maximum=MAX_IMAGE_BYTES)
    if data is None:
        return None
    return "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")


def set_images(conn, data_dir: Path, guid: int, set_no: int) -> dict | None:
    """The thumbnails of the member's ready set, None unless that set is ready and theirs.
    A missing or oversize file is left out; each image carries its place in the set."""
    if not is_cast(conn,guid) or _missing(conn):
        return None
    try:
        files.set_dir(data_dir, guid, set_no)  # integers in range, else not a set of theirs
    except (TypeError, ValueError):
        return None
    if conn.execute("SELECT 1 FROM selfie_set WHERE guid = ? AND set_no = ? AND "
                    "status = 'ready'", (guid, set_no)).fetchone() is None:
        return None
    cur = conn.execute("SELECT set_no, idx FROM selfie_current WHERE guid = ?",
                       (guid,)).fetchone()
    images = []
    for idx in conn.execute("SELECT idx FROM selfie_candidate WHERE guid = ? AND set_no = ? "
                            "ORDER BY idx", (guid, set_no)).fetchall():
        src = _data_url(files.candidate(data_dir, guid, set_no, idx[0], thumb=True), Path(data_dir) / "selfies")
        if src is not None:
            images.append({"index": idx[0], "src": src})
    return {"guid": guid, "set": set_no,
            "current": cur[1] if cur is not None and cur[0] == set_no else None,
            "images": images}


def member_photos(conn, data_dir: Path, guid: int) -> dict | None:
    """One member's photo in force, their sets and their changes; None for no such member."""
    if _missing(conn) or not is_cast(conn,guid):
        return None
    if conn.execute("SELECT 1 FROM cast_member WHERE guid = ?", (guid,)).fetchone() is None:
        return None
    cur = conn.execute("SELECT set_no, idx, pick FROM selfie_current WHERE guid = ?",
                       (guid,)).fetchone()
    return {"guid": guid,
            "current": None if cur is None else {"set": cur[0], "index": cur[1], "pick": cur[2]},
            "sets": [{"set": r[0], "status": r[1], "decided": r[2]} for r in conn.execute(
                "SELECT set_no, status, decided FROM selfie_set WHERE guid = ? "
                "ORDER BY set_no", (guid,))],
            "history": store.picks(conn, guid)}
