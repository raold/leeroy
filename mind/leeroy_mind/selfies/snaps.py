"""Situational and guild photos in mind.db (migrations/*_selfie_snaps.sql). The images are files
(files.py); these rows say what each photo shows, when it may go out and what became of it.

- planned: the night chose a scene; ready: rendered, waiting for a turn to carry it; sent: a
  member's line carried it (kept); expired: it never went out (`ended` says why).
- A situation keeps the member's chosen photo's pick and the looks' hash it was planned from;
  when either changes, `sweep` expires it. A guild photo has no guid and lists its tiles.

Each writer runs in `store.transaction()`. A refusal is SnapError. The mind writes; the console
reads (`counts` takes a plain connection for that)."""
import hashlib
import json
from dataclasses import asdict, dataclass, replace
from typing import Callable

from ..personas.schema import Appearance

WEEK_S = 7 * 86400.0
_COLS = ("id, kind, guid, source, ref, night, status, ended, scene, audience, phases, members, "
         "pick, looks, created, not_before, expires, rendered, width, height, seed, tries, nsfw, "
         "prompt, sent, sender, channel, line_id")


class SnapError(ValueError):
    """A photo change the store refuses; its text is the answer."""


@dataclass(frozen=True)
class SnapRow:
    id: int
    kind: str
    guid: int
    source: str
    ref: str
    night: str
    status: str
    ended: str | None
    scene: dict
    audience: str
    phases: tuple[str, ...]
    members: tuple[tuple[int, int], ...]
    pick: int | None
    looks: str | None
    created: float
    not_before: float
    expires: float
    rendered: float | None
    width: int | None
    height: int | None
    seed: int | None
    tries: int | None
    nsfw: float | None
    prompt: str | None
    sent: float | None
    sender: int | None
    channel: str | None
    line_id: int | None


def _row(r) -> SnapRow:
    v = list(tuple(r))
    v[8] = json.loads(v[8])
    v[10] = tuple(json.loads(v[10]))
    v[11] = tuple((int(a), int(b)) for a, b in json.loads(v[11] or "[]"))
    return SnapRow(*v)


def _read(store, sql: str, params=()) -> list[SnapRow]:
    with store.transaction():  # ends the read, so it never pins a WAL snapshot
        return [_row(r) for r in store.conn.execute(sql, params).fetchall()]


def _one(store, sql: str, params=()):
    with store.transaction():
        return store.conn.execute(sql, params).fetchone()[0]


def looks_hash(a: Appearance) -> str:
    return hashlib.sha256(json.dumps(asdict(a), sort_keys=True).encode("utf-8")).hexdigest()


def _tiles(members) -> str:
    return json.dumps([[int(g), int(p)] for g, p in members])


def plan_snap(store, *, kind, guid, source, ref, night, scene, audience, phases, not_before,
              expires, now, pick=None, looks=None, members=()) -> int | None:
    """A photo planned for the night to render; its id, or None when that guid and ref exist.
    The table's checks refuse the rest (sqlite3.IntegrityError)."""
    with store.transaction():
        cur = store.conn.execute(
            "INSERT INTO selfie_snap(kind, guid, source, ref, night, status, scene, "
            "audience, phases, members, pick, looks, created, not_before, expires) "
            "VALUES (?, ?, ?, ?, ?, 'planned', ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(guid, ref) DO NOTHING",
            (kind, guid, source, ref, night, json.dumps(scene, sort_keys=True), audience,
             json.dumps(list(phases)), _tiles(members) if kind == "guild" else None, pick,
             looks, float(now), float(not_before), float(expires)))
        return cur.lastrowid if cur.rowcount == 1 else None


def planned(store, limit: int) -> list[SnapRow]:
    """Photos waiting for a render: guild photos first, then by id."""
    return _read(store, f"SELECT {_COLS} FROM selfie_snap WHERE status='planned' "
                 "ORDER BY kind='guild' DESC, id LIMIT ?", (int(limit),))


def mark_rendered(store, snap_id: int, *, width, height, seed, tries, nsfw, prompt, now,
                  members=None) -> None:
    """A planned photo rendered: its size, seed, tries, NSFW score and prompt; a guild photo may
    pass the tiles it ended up with."""
    with store.transaction():
        moved = store.conn.execute(
            "UPDATE selfie_snap SET status='ready', rendered=?, width=?, height=?, seed=?, "
            "tries=?, nsfw=?, prompt=?, members=COALESCE(?, members) "
            "WHERE id=? AND status='planned'",
            (float(now), width, height, seed, tries, float(nsfw), prompt,
             None if members is None else _tiles(members), snap_id)).rowcount
        if moved != 1:
            raise SnapError(f"photo {snap_id} is not planned")


def expire(store, snap_id: int, ended: str, *, now: float) -> None:
    """An open (planned or ready) photo ended for the reason `ended`."""
    with store.transaction():
        moved = store.conn.execute(
            "UPDATE selfie_snap SET status='expired', ended=? "
            "WHERE id=? AND status IN ('planned','ready')", (ended, snap_id)).rowcount
        if moved != 1:
            raise SnapError(f"photo {snap_id} is not open")


def sweep(store, *, now: float, pick_of: Callable[[int], int | None],
          looks_of: Callable[[int], str | None]) -> list[SnapRow]:
    """Open photos at or past their expiry end `passed`; situations whose member's photo or looks
    are no longer what they were planned from end `photo_changed` or `looks_changed`. Returns
    the rows it expired."""
    gone: list[SnapRow] = []
    with store.transaction():
        for r in _read(store, f"SELECT {_COLS} FROM selfie_snap "
                       "WHERE status IN ('planned','ready') ORDER BY id"):
            if r.expires <= now:
                why = "passed"
            elif r.kind == "situation" and pick_of(r.guid) != r.pick:
                why = "photo_changed"
            elif r.kind == "situation" and looks_of(r.guid) != r.looks:
                why = "looks_changed"
            else:
                continue
            expire(store, r.id, why, now=now)
            gone.append(replace(r, status="expired", ended=why))
    return gone


def ready_for(store, guid: int, *, now: float, phase: str) -> list[SnapRow]:
    """The member's ready situations that may go out now in `phase`, soonest expiry first."""
    return [r for r in _read(
        store, f"SELECT {_COLS} FROM selfie_snap WHERE kind='situation' AND guid=? "
        "AND status='ready' AND not_before <= ? AND ? < expires ORDER BY expires, id",
        (guid, now, now)) if phase in r.phases]


def ready_guild(store, *, now: float, phase: str) -> SnapRow | None:
    rows = [r for r in _read(
        store, f"SELECT {_COLS} FROM selfie_snap WHERE kind='guild' AND status='ready' "
        "AND not_before <= ? AND ? < expires ORDER BY expires, id", (now, now))
        if phase in r.phases]
    return rows[0] if rows else None


def mark_sent(store, snap_id: int, *, sender: int, channel: str, line_id: int,
              now: float) -> bool:
    """A ready photo went out with line `line_id`; False unless it was ready."""
    with store.transaction():
        return store.conn.execute(
            "UPDATE selfie_snap SET status='sent', sent=?, sender=?, channel=?, line_id=? "
            "WHERE id=? AND status='ready'",
            (float(now), sender, channel, line_id, snap_id)).rowcount == 1


def sent_count(store, since: float) -> int:
    return _one(store, "SELECT count(*) FROM selfie_snap WHERE status='sent' AND sent >= ?",
                (since,))


def last_sent(store, guid: int) -> float | None:
    """When the member last sent a situational photo, to anyone."""
    return _one(store, "SELECT max(sent) FROM selfie_snap WHERE kind='situation' "
                "AND status='sent' AND sender=?", (guid,))


def last_sent_to_him(store, guid: int, since: float) -> SnapRow | None:
    """The member's newest photo whispered to him at or after `since`."""
    rows = _read(store, f"SELECT {_COLS} FROM selfie_snap WHERE kind='situation' "
                 "AND status='sent' AND channel='whisper' AND sender=? AND sent >= ? "
                 "ORDER BY sent DESC, id DESC LIMIT 1", (guid, since))
    return rows[0] if rows else None


def open_stock(store, guid: int | None = None) -> int:
    """Planned plus ready situations, one member's or everyone's."""
    if guid is None:
        return _one(store, "SELECT count(*) FROM selfie_snap WHERE kind='situation' "
                    "AND status IN ('planned','ready')")
    return _one(store, "SELECT count(*) FROM selfie_snap WHERE kind='situation' AND guid=? "
                "AND status IN ('planned','ready')", (guid,))


def guild_photo_since(store, since: float) -> bool:
    """A guild photo created at or after `since`, whatever became of it."""
    return _one(store, "SELECT count(*) FROM selfie_snap WHERE kind='guild' AND created >= ?",
                (since,)) > 0


def counts(conn, *, now: float) -> dict[str, int]:
    """Photos planned, ready, sent in the last week and expired in the last week (by their
    expiry). Reads any connection, the console's read-only one too."""
    def one(sql, *p):
        return conn.execute(sql, p).fetchone()[0]
    return {"planned": one("SELECT count(*) FROM selfie_snap WHERE status='planned'"),
            "ready": one("SELECT count(*) FROM selfie_snap WHERE status='ready'"),
            "sent_week": one("SELECT count(*) FROM selfie_snap WHERE status='sent' AND sent >= ?",
                             now - WEEK_S),
            "expired_week": one("SELECT count(*) FROM selfie_snap WHERE status='expired' "
                                "AND expires >= ?", now - WEEK_S)}
