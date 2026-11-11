"""A member's retained photos, read-only. URLs name integer ids, never disk paths.

Expired situations remain in the owner's gallery; this does not make them sendable again.
Guild composites are not a member's portrait. Missing tables and files read as empty.
"""
import json
from pathlib import Path

from leeroy_mind.personas.schema import persona_from_dict
from leeroy_mind.selfies import files
from leeroy_mind.selfies.store import lust_set
from leeroy_mind.selfies.style import suggestive

from .faces import MAX_FACE_BYTES, image_info, read_image

TABLES = ("selfie_set", "selfie_candidate", "selfie_current", "selfie_snap")


def _have(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _present(path: Path, root: Path) -> bool:
    return image_info(path, root) is not None


def _lust_held(conn, have, guid) -> bool:
    """She no longer has the lust style (or her stored persona cannot be read): photos of sets
    rendered under it are neither listed nor served, unless one is already her photo."""
    if "persona_core" not in have:
        return True
    row = conn.execute("SELECT json FROM persona_core WHERE guid=?", (guid,)).fetchone()
    try:
        return not suggestive(persona_from_dict(json.loads(row[0]))) if row else True
    except (TypeError, ValueError):
        return True


def _entry(data_dir, guid, kind, number, index, rendered, cover):
    if kind == "candidate":
        full = files.candidate(data_dir, guid, number, index)
        thumb = files.candidate(data_dir, guid, number, index, thumb=True)
        query = f"guid={guid}&kind=candidate&set={number}&index={index}"
        ident = f"candidate:{number}:{index}"
    else:
        full = files.snap(data_dir, guid, number)
        thumb = files.snap(data_dir, guid, number, thumb=True)
        query = f"guid={guid}&kind=snap&id={number}"
        ident = f"snap:{number}"
    root = Path(data_dir) / "selfies"
    if not _present(full, root):
        return None
    url = "/api/selfies/photo?" + query
    return {"id": ident, "kind": kind, "set": number if kind == "candidate" else None,
            "index": index, "rendered": rendered, "cover": cover,
            "thumbnail": url + "&thumb=1" if _present(thumb, root) else None,
            "image": url}


def gallery(conn, data_dir, guid: int) -> dict:
    have = _have(conn)
    out = {"guid": guid, "missing": [t for t in TABLES if t not in have], "photos": []}
    from leeroy_mind.personas.tiers import is_cast
    if not is_cast(conn,guid):
        return out
    try:
        files.member_dir(data_dir, guid)
    except (TypeError, ValueError):
        return out
    current = None
    if "selfie_current" in have:
        row = conn.execute("SELECT set_no, idx FROM selfie_current WHERE guid=?", (guid,)).fetchone()
        current = tuple(row) if row else None
    photos = out["photos"]
    held = _lust_held(conn, have, guid)
    if {"selfie_set", "selfie_candidate"} <= have:
        for row in conn.execute(
                "SELECT s.set_no, c.idx, s.rendered FROM selfie_set s JOIN selfie_candidate c "
                "ON c.guid=s.guid AND c.set_no=s.set_no WHERE s.guid=? AND s.status='ready'",
                (guid,)):
            is_cover = current == (row[0], row[1])
            if held and not is_cover and lust_set(conn, guid, row[0]):
                continue  # a lust-style set of a member who left that style: not listed
            entry = _entry(data_dir, guid, "candidate", row[0], row[1], row[2], is_cover)
            if entry:
                photos.append(entry)
    if "selfie_snap" in have:
        for row in conn.execute("SELECT id, rendered FROM selfie_snap WHERE guid=? "
                                "AND kind='situation' AND rendered IS NOT NULL", (guid,)):
            entry = _entry(data_dir, guid, "snap", row[0], None, row[1], False)
            if entry:
                photos.append(entry)
    photos.sort(key=lambda p: (-(p["rendered"] or 0), p["id"]))
    return out


def _is_cover(conn, have, guid, number, index) -> bool:
    if "selfie_current" not in have:
        return False
    row = conn.execute("SELECT set_no, idx FROM selfie_current WHERE guid=?", (guid,)).fetchone()
    return row is not None and tuple(row) == (number, index)


def photo(conn, data_dir, guid, kind, number, index, *, thumb=False) -> bytes | None:
    """Only a rendered row belonging to this member can expose a bounded JPEG."""
    have = _have(conn)
    from leeroy_mind.personas.tiers import is_cast
    if not is_cast(conn,guid):
        return None
    try:
        if kind == "candidate" and {"selfie_set", "selfie_candidate"} <= have:
            found = conn.execute(
                "SELECT 1 FROM selfie_candidate c JOIN selfie_set s ON s.guid=c.guid AND "
                "s.set_no=c.set_no WHERE c.guid=? AND c.set_no=? AND c.idx=? AND s.status='ready'",
                (guid, number, index)).fetchone()
            path = files.candidate(data_dir, guid, number, index, thumb=thumb)
        elif kind == "snap" and "selfie_snap" in have:
            found = conn.execute("SELECT 1 FROM selfie_snap WHERE guid=? AND id=? AND "
                                 "kind='situation' AND rendered IS NOT NULL", (guid, number)).fetchone()
            path = files.snap(data_dir, guid, number, thumb=thumb)
        else:
            return None
        if found and kind == "candidate" and lust_set(conn, guid, number)                 and _lust_held(conn, have, guid) and not _is_cover(conn, have, guid, number, index):
            return None
        if not found or not _present(path, Path(data_dir) / "selfies"):
            return None
        return read_image(path, Path(data_dir) / "selfies")
    except (OSError, TypeError, ValueError):
        return None
