"""`selfies audit --labels DIR`: what a visual audit's labels say of the photos in force.

The labels are JSON lines (`*.jsonl` in DIR, one object a photo) written by whoever looked at the
rendered files: `guid`, `kind` (cover, set, snap), `path` (its tail names the photo:
`<guid>/chosen.jpg`, `<guid>/sets/<n>/c<i>.jpg`, `<guid>/snaps/<id>.jpg`), `severity` (0-3) and
`same_person_as_cover` (yes, probably, no, ...). A photo is defective at severity 2 or more, or
when it is not the person of the cover. The labels stay where they are: nothing of them is copied
into the database, and the report names guids, set numbers, photo ids and codes only.

Read-only by default (plan): the members whose cover in force is defective (the photo labelled as
`chosen.jpg`, or its own set candidate), and the stocked situational photos (planned or ready)
that are. A cover picked after the label file was written is not the photo that was looked at
and is left out (`cover_changed`).

With --go (the mind stopped): those stocked photos end `flagged` through the existing expiry
(snaps.expire: the row only; no file is ever deleted), and a fresh set is queued for each member
with a defective cover (store.queue_restyled: nothing while one is already queued; sets waiting
for his choice stay). A cover is never changed here: the owner picks."""
import json
import re
from pathlib import Path

from .snaps import SnapError, expire
from .store import queue_restyled

MAX_FILES = 64
MAX_BYTES = 16 * 1024 * 1024
DEFECT_SEVERITY = 2
_COVER = re.compile(r"(?:^|/)(\d+)/chosen\.jpg$")
_SET = re.compile(r"(?:^|/)(\d+)/sets/(\d+)/c([0-3])\.jpg$")
_SNAP = re.compile(r"(?:^|/)(\d+)/snaps/(\d+)\.jpg$")


def read_labels(folder) -> tuple[list[dict], dict]:
    """(the labels, counts) from every `*.jsonl` of `folder` (at most MAX_FILES files of
    MAX_BYTES each; a file's rows carry its mtime as `_mtime`). A line that is not a JSON object
    with an integer guid, a string path and an integer severity is counted `malformed` and
    skipped. ValueError("labels_missing") when the folder holds none."""
    folder = Path(folder)
    names = sorted(folder.glob("*.jsonl"))[:MAX_FILES] if folder.is_dir() else []
    if not names:
        raise ValueError("labels_missing")
    rows, counts = [], {"files": len(names), "rows": 0, "malformed": 0}
    for name in names:
        if name.stat().st_size > MAX_BYTES:
            raise ValueError("labels_too_large")
        mtime = name.stat().st_mtime
        for line in name.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                row = None
            if (type(row) is not dict or type(row.get("guid")) is not int
                    or type(row.get("path")) is not str or type(row.get("severity")) is not int):
                counts["malformed"] += 1
                continue
            rows.append({**row, "_mtime": mtime})
            counts["rows"] += 1
    return rows, counts


def defective(row: dict) -> list[str]:
    """The codes that make a labelled photo defective: `severity`, `identity`; none when fine."""
    codes = []
    if row["severity"] >= DEFECT_SEVERITY:
        codes.append("severity")
    if row.get("same_person_as_cover") == "no":
        codes.append("identity")
    return codes


def where(row: dict) -> tuple | None:
    """("cover", guid), ("set", guid, set_no, idx) or ("snap", guid, id) from the label's path;
    None for another path or one whose guid is not the label's."""
    path = row["path"].replace("\\", "/")
    for kind, rx in (("cover", _COVER), ("set", _SET), ("snap", _SNAP)):
        m = rx.search(path)
        if m:
            found = tuple(int(g) for g in m.groups())
            return (kind, *found) if found[0] == row["guid"] else None
    return None


def _cover(conn, guid: int):
    """(set_no, idx, picked at) of the cover in force, or None."""
    row = conn.execute("SELECT after_set, after_idx, ts FROM selfie_pick WHERE guid=? "
                       "ORDER BY id DESC LIMIT 1", (guid,)).fetchone()
    return None if row is None or row[0] is None else (row[0], row[1], row[2])


def plan(conn, labels: list[dict]) -> dict:
    """The report: `covers` (guid, set, index, codes) for each member whose cover in force the
    labels mark defective, `snaps` (id, guid, status, codes) for each defective situational photo
    that is still stocked, and counts of what was left out by code."""
    covers: dict[int, dict] = {}
    snaps: dict[int, dict] = {}
    left: dict[str, int] = {}

    def leave(code: str) -> None:
        left[code] = left.get(code, 0) + 1
    for row in labels:
        codes = defective(row)
        if not codes:
            continue
        at = where(row)
        if at is None:
            leave("path_unknown")
        elif at[0] == "snap":
            found = conn.execute("SELECT status, guid FROM selfie_snap WHERE id=? AND "
                                 "kind='situation'", (at[2],)).fetchone()
            if found is None or found[1] != at[1]:
                leave("snap_unknown")
            elif found[0] not in ("planned", "ready"):
                leave(f"snap_{found[0]}")
            else:
                snaps[at[2]] = {"id": at[2], "guid": at[1], "status": found[0], "codes": codes}
        else:
            cover = _cover(conn, at[1])
            if cover is None:
                leave("no_cover")
            elif at[0] == "cover" and cover[2] > row["_mtime"]:
                leave("cover_changed")
            elif at[0] == "set" and at[2:] != cover[:2]:
                leave("not_cover")
            else:
                entry = covers.setdefault(at[1], {"guid": at[1], "set": cover[0],
                                                  "index": cover[1], "codes": []})
                entry["codes"] = sorted(set(entry["codes"]) | set(codes))
    return {"dry_run": True, "covers": [covers[g] for g in sorted(covers)],
            "snaps": [snaps[i] for i in sorted(snaps)], "left_out": dict(sorted(left.items()))}


def apply(store, report: dict, *, now: float) -> dict:
    """--go: each stocked photo of the report ended `flagged` (the row only) and a fresh set
    queued for each member with a defective cover; what was done, by id and guid. One
    transaction."""
    expired, queued, already = [], [], []
    with store.transaction():
        for snap in report["snaps"]:
            try:
                expire(store, snap["id"], "flagged", now=now)
                expired.append(snap["id"])
            except SnapError:  # ended since the plan
                pass
        for cover in report["covers"]:
            made = queue_restyled(store, cover["guid"], now=now)
            (already if made is None else queued).append(
                cover["guid"] if made is None else {"guid": cover["guid"], "set": made.set_no})
    return {**report, "dry_run": False, "expired": expired, "queued": queued,
            "already_queued": already}
