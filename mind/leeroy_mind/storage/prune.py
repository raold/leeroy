"""Retention for what only grows: the rules, a dry run that counts, and a bounded apply.

Nothing here is a judgement on content. Each rule names rows no reader can reach any more:

- `events`: `game_event` rows of the high-volume world types, kept as long as their longest
  reader looks back (KEEP_DAYS), and for ever while anything cites them: a `event:<id>` reference
  in a memory, a knowledge root, a relationship delta or any other evidence column, or a
  `realm_source` row. Every other type (boss, loot, groups, emotes, jobs, ...) is never pruned.
- `realm_facts`: world PvP facts (`pvp_result_v1`) older than FACT_DAYS that no guild's prompt
  can still show (outside each guild's newest FACT_KEEP known facts), that no memory, post, kill
  or rule row refers to, with their `realm_knows`, `realm_fact_origin` and `realm_source` rows
  and the `game_event` row behind the source. The Realm's action journal, its direct ledger and
  every `realm:` meta receipt (a grow's row, a release receipt) are never touched.
- `meta`: per-line receipts whose stage reads only the night's window.
- `review`: drift flags nobody resolved leave the open count once the critic's week is long past
  (the rows stay: they are the owner's audit records).

Files (the snapshots' orphan sidecars, scratch's database copies and a lab copy left behind) are
`prune_files`; `pre-release-*` snapshots are the release script's and are never touched here.

`plan` and `gather` only read (they run on a read-only connection). `apply` deletes in small
transactions of its own and can be bounded per rule; the night runs `steps` itself."""
import json
import re
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path

DAY_S = 86400
# game_event: days kept per type, by the longest look-back of anything that reads the type.
#   pvp_flag   no reader at all
#   resurrect  only the awareness card's recent window (minutes) and the live raid listeners
#   pvp_kill   the Realm takes it at arrival (its source row then keeps the event); nothing reads
#              the raw row after the night's window
#   zone       the awareness card's "where is she" looks back 7 days; `eval talk` replays stored
#              talk candidates, 14 days by default
#   death      awards and culture origins read deaths on raid maps (kept for ever, see
#              KEEP_DEATH_MAPS); the others are read by the awareness card's recent window only
KEEP_DAYS = {"pvp_flag": 1, "resurrect": 7, "pvp_kill": 14, "zone": 30, "death": 30}
FACT_POLICY = "pvp_result_v1"
FACT_DAYS = 30      # the Realm night takes up facts a week old at most; four times that
FACT_KEEP = 50      # a guild's prompt shows its newest 5 known facts; ten times that
META_DAYS = 30
REVIEW_DAYS = 14    # the critic counts a member's flags over 7 nights
BATCH = 500
# where a stored reference to an event or a fact can live (found by scanning every text column
# of a live database); a column an older database lacks is skipped
EVIDENCE_COLUMNS = (("memory", "evidence"), ("knows", "root"), ("thinks_knows", "root"),
                    ("rel_delta", "evidence"), ("emotion", "evidence"), ("story", "evidence"),
                    ("persona_state", "self_notes"), ("persona_ledger", "evidence"),
                    ("attraction_act", "ref"), ("chronicle", "evidence"), ("encounter", "ref"),
                    ("outreach_item", "ref"), ("lexicon", "origin_ref"), ("lexicon", "origin_refs"),
                    ("fact", "root"), ("rumor", "root"), ("feud", "evidence"),
                    ("jealousy", "evidence"), ("disclosure", "ref"), ("volition", "ref"),
                    ("layer_open", "ref"), ("review_queue", "evidence"), ("meta", "key"),
                    ("meta", "value"), ("realm_post", "thread"))
# Every one of them must exist in the migrated schema (tests/test_prune.py): a misspelt column
# is skipped by _cited as an older database's would be, and protects nothing.
# A reference may follow a prefix ("banter:used:event:7") and carry a tag ("event:7:kind:wipe").
_EVENT_REF = re.compile(r"(?<![A-Za-z0-9_])event:([0-9]{1,18})(?![0-9a-f])")
_FACT_REF = re.compile(r"(?<![A-Za-z0-9_])fact:([0-9]{1,18})(?![0-9a-f])")
# meta receipts: (name, key prefix, what its id is, the table and time column that date it)
META_FAMILIES = (
    # nightly/lexicon.py spread(): "this public line was attributed"; the stage scans the night's
    # window only, the weekly evaluation reads at most the last two weeks
    ("lexicon-line", "nightly:lexicon-line:", "chat_line", "ts"),
    # nightly/human_world.py: "this line's reaction was recorded" (the reaction itself is kept:
    # the profile stage reads every one)
    ("lands", "nightly:human-world:lands:", "chat_line", "ts"),
)
_BENCH = re.compile(r"^guild:raid:([0-9]+):bench-memory:[0-9]+$")


@dataclass
class Finding:
    rule: str
    rows: int = 0
    bytes: int = 0
    note: str = ""


def _has(conn, table: str, column: str | None = None) -> bool:
    try:
        cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]
    except sqlite3.Error:
        return False
    return bool(cols) and (column is None or column in cols)


def _tables(conn) -> list[str]:
    return [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                       "AND name NOT LIKE 'sqlite_%'")]


def _referrers(conn, target: str) -> list[tuple[str, str]]:
    """(table, column) of every foreign key that points at `target`."""
    out = []
    for table in _tables(conn):
        try:
            rows = conn.execute(f'PRAGMA foreign_key_list("{table}")').fetchall()
        except sqlite3.Error:
            continue  # a virtual table whose module this connection has not loaded
        out += [(table, r[3]) for r in rows if r[2] == target]
    return out


def _cited(conn, pattern, needle: str) -> set[int]:
    found = set()
    for table, column in EVIDENCE_COLUMNS:
        if not _has(conn, table, column):
            continue
        for (text,) in conn.execute(f'SELECT "{column}" FROM "{table}" WHERE "{column}" LIKE ?',
                                    (f"%{needle}%",)):
            if isinstance(text, str):
                found.update(int(m) for m in pattern.findall(text))
    return found


def _chunks(ids, size=BATCH):
    ids = list(ids)
    for i in range(0, len(ids), size):
        yield ids[i:i + size]


# -- game_event ------------------------------------------------------------------------------------

def _raid_maps() -> frozenset[int]:
    from leeroy_mind.wow import DUNGEONS, RAID_INSTANCES, RAID_MAPS
    return frozenset(RAID_MAPS) | frozenset(RAID_INSTANCES) | frozenset(DUNGEONS)


def event_candidates(conn, now: float) -> dict[str, list[tuple[int, int]]]:
    """type -> [(id, payload bytes)] of the rows past their type's days that nothing cites."""
    if not _has(conn, "game_event"):
        return {}
    keep = _cited(conn, _EVENT_REF, "event:")
    for table, column in _referrers(conn, "game_event"):
        keep.update(r[0] for r in conn.execute(
            f'SELECT "{column}" FROM "{table}" WHERE "{column}" IS NOT NULL'))
    maps = _raid_maps()
    out = {}
    for kind, days in KEEP_DAYS.items():
        rows = []
        for ident, size, payload in conn.execute(
                "SELECT id, length(payload), CASE WHEN type='death' THEN payload END "
                "FROM game_event WHERE type=? AND ts<? ORDER BY id", (kind, now - days * DAY_S)):
            if ident in keep:
                continue
            if kind == "death":
                try:
                    where = json.loads(payload).get("map")
                except (ValueError, AttributeError):
                    continue  # unreadable: kept
                if type(where) is not int or where in maps:
                    continue
            rows.append((ident, size or 0))
        if rows:
            out[kind] = rows
    return out


# -- realm facts ---------------------------------------------------------------------------------

def fact_candidates(conn, now: float) -> list[tuple[int, int]]:
    """[(fact id, provenance bytes)] of the world PvP facts nobody can cite any more."""
    if not (_has(conn, "realm_fact") and _has(conn, "realm_fact_origin") and _has(conn, "realm_knows")):
        return []
    keep = _cited(conn, _FACT_REF, "fact:")
    for table in _tables(conn):
        if table in ("realm_fact_origin", "realm_knows") or not _has(conn, table, "fact"):
            continue
        keep.update(r[0] for r in conn.execute(
            f'SELECT fact FROM "{table}" WHERE typeof(fact)=\'integer\''))
    for (guild,) in conn.execute("SELECT DISTINCT guild FROM realm_knows").fetchall():
        keep.update(r[0] for r in conn.execute(
            "SELECT f.id FROM realm_knows k JOIN realm_fact f ON f.id=k.fact WHERE k.guild=? "
            "ORDER BY f.ts DESC, f.id DESC LIMIT ?", (guild, FACT_KEEP)))
    return [(r[0], r[1] or 0) for r in conn.execute(
        "SELECT f.id, length(o.proof_json)+length(o.qualification_json)+length(o.producer_key)"
        "+length(f.text) FROM realm_fact f JOIN realm_fact_origin o ON o.fact=f.id "
        "WHERE o.policy=? AND f.ts<? ORDER BY f.id", (FACT_POLICY, now - FACT_DAYS * DAY_S))
        if r[0] not in keep]


def _delete_facts(conn, ids: list[int], cited: set[int], others=None) -> int:
    """One batch, in the caller's transaction: the facts, what knows them, their origins, and
    each source (with its event row) that nothing else stands on."""
    marks = ",".join("?" * len(ids))
    sources = [r[0] for r in conn.execute(
        f"SELECT source FROM realm_fact_origin WHERE fact IN ({marks})", ids)]
    conn.execute(f"DELETE FROM realm_knows WHERE fact IN ({marks})", ids)
    conn.execute(f"DELETE FROM realm_fact_origin WHERE fact IN ({marks})", ids)
    gone = conn.execute(f"DELETE FROM realm_fact WHERE id IN ({marks})", ids).rowcount
    others = _referrers(conn, "realm_source") if others is None else others
    for source in sources:
        if any(conn.execute(f'SELECT 1 FROM "{t}" WHERE "{c}"=? LIMIT 1', (source,)).fetchone()
               for t, c in others):
            continue
        row = conn.execute("SELECT game_event_id FROM realm_source WHERE id=?", (source,)).fetchone()
        conn.execute("DELETE FROM realm_source WHERE id=?", (source,))
        if row is not None and row[0] is not None and row[0] not in cited:
            if not conn.execute("SELECT 1 FROM realm_source WHERE game_event_id=? LIMIT 1",
                                (row[0],)).fetchone():
                conn.execute("DELETE FROM game_event WHERE id=? AND type='pvp_kill'", (row[0],))
    return gone


# -- meta and the review queue ---------------------------------------------------------------------

def meta_candidates(conn, now: float) -> dict[str, list[tuple[str, int]]]:
    """family -> [(key, bytes)]: receipts of lines and raids older than META_DAYS."""
    out, cutoff = {}, now - META_DAYS * DAY_S
    for name, prefix, table, column in META_FAMILIES:
        if not _has(conn, table, column):
            continue
        rows = []
        for key, size in conn.execute(
                "SELECT key, length(key)+length(coalesce(value,'')) FROM meta "
                "WHERE key >= ? AND key < ?", (prefix, prefix + "￿")):
            ident = key[len(prefix):]
            if not ident.isdecimal():
                continue
            line = conn.execute(f'SELECT "{column}" FROM "{table}" WHERE id=?', (int(ident),)).fetchone()
            if line is not None and line[0] is not None and line[0] < cutoff:
                rows.append((key, size))
        if rows:
            out[name] = rows
    if _has(conn, "guild_raid", "ended"):
        # guild/session.py: "this benched member's memory of the raid was written", read only
        # while that raid's session is open or resumes
        ended = dict(conn.execute("SELECT raid_id, ended FROM guild_raid WHERE ended IS NOT NULL"))
        rows = [(key, size) for key, size in conn.execute(
            "SELECT key, length(key)+length(coalesce(value,'')) FROM meta "
            "WHERE key >= 'guild:raid:' AND key < 'guild:raid;'")
            if (m := _BENCH.match(key)) and ended.get(int(m.group(1)), now) < cutoff]
        if rows:
            out["bench-memory"] = rows
    return out


def review_candidates(conn, now: float) -> list[int]:
    if not _has(conn, "review_queue", "resolved"):
        return []
    return [r[0] for r in conn.execute("SELECT id FROM review_queue WHERE resolved IS NULL "
                                       "AND created<? ORDER BY id", (now - REVIEW_DAYS * DAY_S,))]


# -- the database: plan and apply ------------------------------------------------------------------

def plan(conn, now: float) -> list[Finding]:
    """What `apply` would do, read only."""
    out = []
    for kind, rows in event_candidates(conn, now).items():
        out.append(Finding(f"events:{kind}", len(rows), sum(s for _, s in rows),
                           f"older than {KEEP_DAYS[kind]} d, uncited"))
    facts = fact_candidates(conn, now)
    out.append(Finding("realm_facts", len(facts), sum(s for _, s in facts),
                       f"{FACT_POLICY} older than {FACT_DAYS} d, outside each guild's newest "
                       f"{FACT_KEEP}, unreferenced; with knows, origin, source and event rows"))
    for name, rows in meta_candidates(conn, now).items():
        out.append(Finding(f"meta:{name}", len(rows), sum(s for _, s in rows),
                           f"receipts older than {META_DAYS} d"))
    review = review_candidates(conn, now)
    out.append(Finding("review", len(review), 0,
                       f"unresolved drift flags older than {REVIEW_DAYS} d, marked resolved (kept)"))
    return [f for f in out if f.rows or f.rule in ("realm_facts", "review")]


@dataclass
class Work:
    """What one pass found: the ids each rule may remove, read in one go."""
    facts: list
    cited_events: set
    events: dict
    meta: dict
    review: list


def gather(conn, now: float) -> Work:
    """Read only: usable on a read-only connection, in a worker thread."""
    facts = fact_candidates(conn, now)
    return Work(facts, _cited(conn, _EVENT_REF, "event:") if facts else set(),
                event_candidates(conn, now), meta_candidates(conn, now),
                review_candidates(conn, now))


def steps(work: Work, now: float, *, limit=None):
    """(rule, write) for each batch of at most BATCH rows, at most `limit` rows per rule (an
    integer, or a function of the rule's name; None: all): the caller runs each `write(conn)`
    in a transaction of its own; it returns (rows, bytes). The facts go first: a source that
    goes frees its event row for the events rule."""
    def cut(ids, rule):
        cap = limit(rule) if callable(limit) else limit
        return _chunks(ids[:max(0, cap)] if cap is not None else ids)

    sizes = dict(work.facts)
    others = []  # the tables that stand on a source, read once (not once a batch)

    def facts(conn, c, z):
        if not others:
            others.append(_referrers(conn, "realm_source"))
        return _delete_facts(conn, c, work.cited_events, others[0]), sum(z[i] for i in c)
    for chunk in cut([i for i, _ in work.facts], "realm_facts"):
        yield "realm_facts", lambda conn, c=chunk, z=sizes: facts(conn, c, z)
    for kind, rows in work.events.items():
        sizes = dict(rows)
        for chunk in cut([i for i, _ in rows], f"events:{kind}"):
            yield f"events:{kind}", lambda conn, c=chunk, z=sizes, k=kind: (conn.execute(
                f"DELETE FROM game_event WHERE type=? AND id IN ({','.join('?' * len(c))})",
                (k, *c)).rowcount, sum(z[i] for i in c))
    for name, rows in work.meta.items():
        sizes = dict(rows)
        for chunk in cut([k for k, _ in rows], f"meta:{name}"):
            yield f"meta:{name}", lambda conn, c=chunk, z=sizes: (conn.execute(
                f"DELETE FROM meta WHERE key IN ({','.join('?' * len(c))})", c).rowcount,
                sum(z[k] for k in c))
    for chunk in cut(work.review, "review"):
        yield "review", lambda conn, c=chunk: (conn.execute(
            f"UPDATE review_queue SET resolved=? WHERE resolved IS NULL AND id IN "
            f"({','.join('?' * len(c))})", (now, *c)).rowcount, 0)


def fold(results) -> list[Finding]:
    """(rule, rows, bytes) triples summed by rule, in first-seen order."""
    out: dict[str, Finding] = {}
    for rule, rows, size in results:
        f = out.setdefault(rule, Finding(rule))
        f.rows += rows
        f.bytes += size
    return list(out.values())


def apply(conn, now: float, *, limit: int | None = None) -> list[Finding]:
    """Remove what `plan` counts, at most `limit` rows per rule (None: all), each batch in its
    own transaction on `conn`. A batch something has begun to stand on (a foreign key) is left
    whole for a later pass."""
    conn.commit()
    done = []
    for rule, write in steps(gather(conn, now), now, limit=limit):
        try:
            rows, size = write(conn)
            conn.commit()
            done.append((rule, rows, size))
        except sqlite3.IntegrityError:
            conn.rollback()
    return fold(done)


def indexed(conn) -> bool:
    """Whether the indexes a delete needs are there (the migration that came with this module):
    without them each removed row scans its child tables."""
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    return {"realm_source_game_event", "realm_fact_origin_source"} <= names


# -- files ---------------------------------------------------------------------------------------

SIDECARS = ("-wal", "-shm", "-journal")
SCRATCH_DB = re.compile(r"^mind-[A-Za-z0-9_-]+\.db$")
SCRATCH_DB_DAYS = 7
SCRATCH_CAP = 4 * 1024 ** 3   # the copies in scratch together; the oldest go first above it
LAB_COPY_DAYS = 2             # a lab copy its run did not remove (the mind stopped under it)
LAB_COPY_DIRS = ("mind-data", "snapshot")


def _plain(path: Path) -> bool:
    return not path.is_symlink() and not (hasattr(path, "is_junction") and path.is_junction())


def _tree_size(path: Path) -> int:
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file() and not p.is_symlink():
                total += p.stat().st_size
        except OSError:
            pass
    return total


def trial_running(data_dir: Path) -> bool:
    """Whether a trial holds one of scratch/locks/<label> (nightly/cli.py `_working_store`: a
    trial runs beside the live service on a `scratch/mind-<label>-*.db` copy under that lock).
    While one does, no scratch copy is removed: where an open file can be unlinked, removing
    its database would lose the trial's committed units."""
    locks = Path(data_dir) / "scratch" / "locks"
    if not locks.is_dir():
        return False
    from leeroy_mind.app import instance_lock_held
    for label in locks.iterdir():
        try:
            if label.is_dir() and instance_lock_held(label):
                return True
        except OSError:
            return True  # a lock that cannot be probed counts as held
    return False


def file_candidates(data_dir: Path, now: float) -> list[tuple[str, Path, int]]:
    """(rule, path, bytes): a snapshot's sidecar whose database is gone; scratch's database copies
    past SCRATCH_DB_DAYS, or the oldest while they exceed SCRATCH_CAP together (none while a
    trial holds its lock); a lab copy older than LAB_COPY_DAYS. Never a photo, a lock, a report
    or anything else in scratch."""
    data_dir = Path(data_dir)
    out = []
    snapshots = data_dir / "snapshots"
    if snapshots.is_dir() and _plain(snapshots):
        for p in sorted(snapshots.iterdir()):
            for suffix in SIDECARS:
                if (p.name.endswith(".db" + suffix) and p.is_file() and _plain(p)
                        and not (snapshots / p.name[:-len(suffix)]).exists()):
                    out.append(("snapshot-sidecar", p, p.stat().st_size))
    scratch = data_dir / "scratch"
    if scratch.is_dir() and _plain(scratch):
        copies = []
        idle = not trial_running(data_dir)
        for p in scratch.iterdir() if idle else ():
            if SCRATCH_DB.match(p.name) and p.is_file() and _plain(p):
                size = p.stat().st_size + sum(
                    s.stat().st_size for x in SIDECARS if (s := scratch / (p.name + x)).is_file())
                copies.append((p.stat().st_mtime, p, size))
        copies.sort()
        total = sum(size for _, _, size in copies)
        for mtime, p, size in copies:
            if mtime < now - SCRATCH_DB_DAYS * DAY_S or total > SCRATCH_CAP:
                out.append(("scratch-db", p, size))
                total -= size
        for p in scratch.iterdir() if idle else ():  # a sidecar whose copy is gone
            for suffix in SIDECARS:
                if (p.name.endswith(".db" + suffix) and SCRATCH_DB.match(p.name[:-len(suffix)])
                        and p.is_file() and _plain(p) and not (scratch / p.name[:-len(suffix)]).exists()):
                    out.append(("scratch-db", p, p.stat().st_size))
        lab = scratch / "lab"
        if lab.is_dir() and _plain(lab) and (lab / ".leeroy-sandbox").is_file():
            for name in LAB_COPY_DIRS:
                p = lab / name
                if p.is_dir() and _plain(p) and _newest(p) < now - LAB_COPY_DAYS * DAY_S:
                    out.append(("lab-copy", p, _tree_size(p)))
    return out


def _newest(path: Path) -> float:
    newest = path.stat().st_mtime
    for p in path.iterdir():
        try:
            newest = max(newest, p.stat().st_mtime)
        except OSError:
            pass
    return newest


def plan_files(data_dir: Path, now: float) -> list[Finding]:
    return _fold((rule, size) for rule, _, size in file_candidates(data_dir, now))


def prune_files(data_dir: Path, now: float) -> list[Finding]:
    done = []
    for rule, path, size in file_candidates(data_dir, now):
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
                for suffix in SIDECARS if rule == "scratch-db" else ():
                    Path(str(path) + suffix).unlink(missing_ok=True)
            done.append((rule, size))
        except OSError:
            pass  # held open: the next pass takes it
    return _fold(done)


def _fold(items) -> list[Finding]:
    return fold((f"files:{rule}", 1, size) for rule, size in items)


def remove_sidecars(db_path: Path) -> None:
    """A database file's -wal, -shm and -journal, for a caller that just removed the file."""
    for suffix in SIDECARS:
        try:
            Path(str(db_path) + suffix).unlink(missing_ok=True)
        except OSError:
            pass

