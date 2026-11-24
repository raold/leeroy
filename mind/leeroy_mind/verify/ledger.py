"""The verification ledger: each shipped feature in features.toml with its status, computed
read-only from bounded log tails (logscan.py), mind.db opened read-only and the configured
switches. Nothing is written, nothing is connected to, no model is loaded.

Status, in this order: `switched_off` when a switch is configured off; `failed` when failure
evidence is the newest; `seen_working` when working evidence is the newest; else `unverified`.
`observed` evidence (an engage, a stage, a death's last recorded hit) is shown but never decides
a status. Only ids, counts, times, switch keys and short codes leave this module, never a log
line or any text anyone wrote."""
import math
import re
import sqlite3
import time
import tomllib
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from leeroy_mind.console.native_switch_keys import LEEROY, PLAYERBOTS
from leeroy_mind.console.native_switches import values as conf_values
from leeroy_mind.console.switches import SWITCHES, default as switch_default, is_number

from .logscan import CODE, SOURCES, Signature, Tally, scan

FEATURES = Path(__file__).with_name("features.toml")
STATUSES = ("failed", "seen_working", "unverified", "switched_off")
KINDS = ("working", "failed", "observed")
LOGS = tuple(SOURCES)
CONF = {"native": "mod_leeroy.conf", "playerbots": "playerbots.conf"}
CONF_BYTES = 512 * 1024
MAX_CODES = 8
_ID = re.compile(r"[a-z][a-z0-9_]{0,47}\Z")
_MIND_ROWS = {f"{row[0]}.{row[1]}": row for row in SWITCHES if not is_number(row)}
BASIS = ("Counts are lower bounds: native logs and mind.log are read newest first within a byte budget "
        "and keep 14 days, so older evidence drops out; mind.db rows stay. Native switches are the "
        "configured values in the module files, not proof of the running worldserver's state.")


@dataclass(frozen=True)
class Switch:
    source: str  # mind, native or playerbots
    key: str
    default: bool


@dataclass(frozen=True)
class Evidence:
    id: str
    kind: str
    what: str
    log: str | None = None
    token: str | None = None
    pattern: re.Pattern | None = None
    example: str | None = None
    db: dict | None = None


@dataclass(frozen=True)
class Feature:
    id: str
    name: str
    prs: tuple[int, ...]
    since: str | None
    note: str
    switches: tuple[Switch, ...]
    evidence: tuple[Evidence, ...]

    def since_at(self) -> float | None:
        """`since` as epoch seconds; a naive value is this host's local time, as native logs are."""
        return datetime.fromisoformat(self.since).timestamp() if self.since else None


def _text(value, where, name, limit, *, required=True):
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"features.toml: {where}: {name} must be text of at most {limit} characters")
    return value


def _keys(table, allowed, where):
    if not isinstance(table, dict):
        raise ValueError(f"features.toml: {where}: expected a table")
    extra = set(table) - set(allowed)
    if extra:
        raise ValueError(f"features.toml: {where}: unknown keys {sorted(extra)}")


def _switch(raw, where) -> Switch:
    _keys(raw, ("source", "key", "default"), where)
    source, key = raw.get("source"), raw.get("key")
    if source == "mind":
        row = _MIND_ROWS.get(key)
        if row is None or "default" in raw:
            raise ValueError(f"features.toml: {where}: a mind switch is a boolean row of "
                             "console/switches.py and takes its default from there")
        return Switch("mind", key, switch_default(row))
    if source in CONF:
        if key not in (LEEROY if source == "native" else PLAYERBOTS):
            raise ValueError(f"features.toml: {where}: {key!r} is not in console/native_switch_keys.py")
        if type(raw.get("default")) is not bool:
            raise ValueError(f"features.toml: {where}: a {source} switch names its compiled default")
        return Switch(source, key, raw["default"])
    raise ValueError(f"features.toml: {where}: source must be mind, native or playerbots")


def _evidence(raw, where) -> Evidence:
    _keys(raw, ("id", "kind", "what", "log", "token", "pattern", "example", "db"), where)
    ident, kind = raw.get("id"), raw.get("kind")
    if not isinstance(ident, str) or not _ID.fullmatch(ident) or kind not in KINDS:
        raise ValueError(f"features.toml: {where}: id must be an identifier and kind one of {KINDS}")
    what = _text(raw.get("what"), where, "what", 200)
    if ("log" in raw) == ("db" in raw):
        raise ValueError(f"features.toml: {where}: give either log (with token and pattern) or db")
    if "db" in raw:
        db = raw["db"]
        _keys(db, ("from", "where", "time", "unit", "code"), where + ".db")
        for name in ("from", "time"):
            _text(db.get(name), where + ".db", name, 600)
        for name in ("where", "code"):
            _text(db.get(name), where + ".db", name, 600, required=False)
        if db.get("unit") not in ("s", "ms") or any(";" in str(v) for v in db.values()):
            raise ValueError(f"features.toml: {where}.db: unit must be s or ms, and no statement separators")
        if "token" in raw or "pattern" in raw:
            raise ValueError(f"features.toml: {where}: token and pattern belong to log evidence")
        return Evidence(ident, kind, what, db=dict(db))
    if raw["log"] not in LOGS:
        raise ValueError(f"features.toml: {where}: log must be one of {LOGS}")
    token = _text(raw.get("token"), where, "token", 64)
    if not token.isascii() or not token.isprintable():
        raise ValueError(f"features.toml: {where}: token must be printable ASCII")
    try:
        pattern = re.compile(_text(raw.get("pattern"), where, "pattern", 600))
    except re.error as e:
        raise ValueError(f"features.toml: {where}: pattern does not compile ({e.msg})") from None
    if set(pattern.groupindex) - {"code"}:
        raise ValueError(f"features.toml: {where}: the only named group is code")
    example = _text(raw.get("example"), where, "example", 600, required=False)
    return Evidence(ident, kind, what, log=raw["log"], token=token, pattern=pattern, example=example)


def load_features(path: Path = FEATURES) -> tuple[Feature, ...]:
    doc = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    _keys(doc, ("feature",), "top level")
    out, seen = [], set()
    for n, raw in enumerate(doc.get("feature") or ()):
        where = f"feature {n + 1}"
        _keys(raw, ("id", "name", "prs", "since", "note", "switch", "evidence"), where)
        ident = raw.get("id")
        if not isinstance(ident, str) or not _ID.fullmatch(ident) or ident in seen:
            raise ValueError(f"features.toml: {where}: id must be a new identifier")
        seen.add(ident)
        where = f"feature {ident}"
        prs = raw.get("prs")
        if not isinstance(prs, list) or not all(type(p) is int and 0 < p < 100000 for p in prs):
            raise ValueError(f"features.toml: {where}: prs must be a list of pull request numbers")
        since = _text(raw.get("since"), where, "since", 40, required=False)
        if since is not None:
            try:
                datetime.fromisoformat(since)
            except ValueError:
                raise ValueError(f"features.toml: {where}: since must be an ISO date or date and time") from None
        evidence = tuple(_evidence(e, f"{where} evidence {i + 1}") for i, e in enumerate(raw.get("evidence") or ()))
        if not evidence or len({e.id for e in evidence}) != len(evidence):
            raise ValueError(f"features.toml: {where}: needs evidence, each with its own id")
        if not any(e.kind != "observed" for e in evidence) and not raw.get("note"):
            raise ValueError(f"features.toml: {where}: with only observed evidence a note must say why")
        switches = tuple(_switch(s, f"{where} switch {i + 1}") for i, s in enumerate(raw.get("switch") or ()))
        out.append(Feature(ident, _text(raw.get("name"), where, "name", 100), tuple(prs), since,
                           _text(raw.get("note"), where, "note", 400, required=False) or "",
                           switches, evidence))
    if not out:
        raise ValueError("features.toml: no features")
    return tuple(out)


# -- switches -------------------------------------------------------------------------------------

def _read_conf(path: Path) -> str:
    """One module .conf, never through a link, at most CONF_BYTES."""
    if any(p.is_symlink() or p.is_junction() for p in (path, path.parent)):
        raise OSError("linked source")
    with path.open("rb") as f:
        raw = f.read(CONF_BYTES + 1)
    if len(raw) > CONF_BYTES:
        raise OSError("too large")
    return raw.decode("utf-8-sig")


def switch_states(features, data: Path, native_dir: Path, deadline: float) -> dict:
    """{(source, key): (state, configured)}: state on, off or unknown. A mind table its loader
    refuses is off; a native key unset in its .conf is its compiled default."""
    wanted = {(s.source, s.key): s for f in features for s in f.switches}
    out = {}
    if any(source == "mind" for source, _ in wanted):
        from leeroy_mind.console import opsview
        try:
            rows = opsview.switches_view(data, deadline, mind_started=None)["rows"]
            states = {f"{r['table']}.{r['key']}": r["state"] for r in rows}
        except Exception:
            states = {}
        for (source, key), s in wanted.items():
            if source == "mind":
                configured = states.get(key, "unknown")
                out[source, key] = ({"on": "on", "off": "off", "invalid": "off"}.get(configured, "unknown"),
                                    configured)
    for source, filename in CONF.items():
        keys = {key for src, key in wanted if src == source}
        if not keys:
            continue
        try:
            configured = conf_values(_read_conf(native_dir / "configs" / "modules" / filename), keys)
            readable = True
        except (OSError, UnicodeError):
            configured, readable = {}, False
        for key in keys:
            value = configured.get(key, "unset" if readable else "unknown")
            if value == "unset":
                state = "on" if wanted[source, key].default else "off"
            else:
                state = value if value in ("on", "off") else "unknown"
            out[source, key] = (state, value)
    return out


def dungeonclear_elsewhere(native_dir: Path) -> bool | None:
    """True when mod_dungeon_clear.conf routes playerbots.dungeonclear away from Playerbots.log,
    to its own file, which carries no times and is not read: after a `.reload config` its lines
    go there; before one they fall back to the playerbots channel. None when it cannot be read."""
    try:
        text = _read_conf(native_dir / "configs" / "modules" / "mod_dungeon_clear.conf")
    except (OSError, UnicodeError):
        return None
    for line in text.splitlines():
        key, _, value = line.strip().partition("=")
        if key.strip() == "Logger.playerbots.dungeonclear":
            appenders = value.replace('"', "").partition(",")[2]
            return "Playerbots" not in appenders.split()
    return False


# -- mind.db --------------------------------------------------------------------------------------

def _connect(data: Path, deadline: float):
    path = data / "mind.db"
    if not path.is_file():
        return None
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1.0)
    conn.set_progress_handler(lambda: time.monotonic() >= deadline, 1000)
    conn.execute("PRAGMA query_only=ON")
    return conn


def _sql(db: dict) -> str:
    when = db["time"]
    code = db.get("code") or "NULL"
    where = f"({db['where']}) AND " if db.get("where") else ""
    return (f"SELECT {code}, count(*), min({when}), max({when}) FROM {db['from']} "
            f"WHERE {where}({when}) >= ? GROUP BY 1 LIMIT 64")


def _number(value):
    return value if type(value) in (int, float) and math.isfinite(value) else None


def db_tally(conn, db: dict, since: float | None) -> Tally:
    """Counts, first and last time and codes of one db signature. sqlite3.Error is the caller's."""
    scale = 1000 if db["unit"] == "ms" else 1
    tally = Tally()
    for code, count, first, last in conn.execute(_sql(db), ((since or 0) * scale,)).fetchall():
        first, last = _number(first), _number(last)
        if type(count) is not int or count <= 0 or first is None or last is None:
            continue
        tally.count += count
        tally.first = first / scale if tally.first is None else min(tally.first, first / scale)
        tally.last = last / scale if tally.last is None else max(tally.last, last / scale)
        if code is not None:
            tally.codes[code if isinstance(code, str) and CODE.fullmatch(code) else "other"] += count
    return tally


def _db_why(error: sqlite3.Error) -> str:
    text = str(error)
    if text == "interrupted":
        return "deadline"
    if text.startswith(("no such table", "no such column")):
        return "not_in_this_database"
    return "unreadable"


# -- the ledger -----------------------------------------------------------------------------------

def _summary(tallies):
    seen = [t for t in tallies if t.count]
    return {"signatures": len(seen),
            "first": min((t.first for t in seen), default=None),
            "last": max((t.last for t in seen), default=None)}


def status_of(switches, working, failed) -> str:
    if any(s["state"] == "off" for s in switches):
        return "switched_off"
    if failed["last"] is not None and (working["last"] is None or failed["last"] >= working["last"]):
        return "failed"
    return "seen_working" if working["last"] is not None else "unverified"


def ledger(data: Path, native_dir: Path, *, features=None, now=None, deadline=None, budgets=None,
           tz=None) -> dict:
    """The ledger as one JSON-ready dict: features in features.toml's order, counts per status,
    log coverage per source and mind.db's availability."""
    features = load_features() if features is None else features
    now = time.time() if now is None else now
    deadline = time.monotonic() + 60 if deadline is None else deadline
    data, native_dir = Path(data), Path(native_dir)
    signatures = [Signature(f"{f.id}.{e.id}", e.log, e.token.encode("ascii"), e.pattern, f.since_at())
                  for f in features for e in f.evidence if e.log]
    directories = {"server": native_dir, "playerbots": native_dir, "mind": data / "logs"}
    tallies, coverage = scan(directories, signatures, deadline=deadline, budgets=budgets, tz=tz)
    if "playerbots" in coverage and dungeonclear_elsewhere(native_dir):
        coverage["playerbots"]["issues"]["dungeonclear_elsewhere"] = 1
        coverage["playerbots"]["complete"] = False
    states = switch_states(features, data, native_dir, deadline)
    database, unavailable = "ok", {}
    try:
        conn = _connect(data, deadline)
    except sqlite3.Error:
        conn, database = None, "unreadable"
    if conn is None and database == "ok":
        database = "missing"
    try:
        for f in features:
            for e in f.evidence:
                if not e.db:
                    continue
                key = f"{f.id}.{e.id}"
                if conn is None:
                    unavailable[key] = database
                    continue
                try:
                    tallies[key] = db_tally(conn, e.db, f.since_at())
                except sqlite3.Error as error:
                    unavailable[key] = _db_why(error)
    finally:
        if conn is not None:
            conn.close()
    out, counts = [], Counter()
    for f in features:
        switches = [{"source": s.source, "key": s.key, "state": states[s.source, s.key][0],
                     "configured": states[s.source, s.key][1], "default": s.default} for s in f.switches]
        evidence, by_kind = [], {kind: [] for kind in KINDS}
        for e in f.evidence:
            key = f"{f.id}.{e.id}"
            tally = tallies.get(key) or Tally()
            by_kind[e.kind].append(tally)
            evidence.append({"id": e.id, "kind": e.kind, "what": e.what, "source": e.log or "mind.db",
                             "count": tally.count, "first": tally.first, "last": tally.last,
                             "codes": [{"code": c, "count": n} for c, n in tally.codes.most_common(MAX_CODES)],
                             "available": key not in unavailable, "why": unavailable.get(key)})
        working, failed = _summary(by_kind["working"]), _summary(by_kind["failed"])
        status = status_of(switches, working, failed)
        counts[status] += 1
        out.append({"id": f.id, "name": f.name, "prs": list(f.prs), "since": f.since_at(),
                    "note": f.note, "status": status, "switches": switches, "working": working,
                    "failed": failed, "observed": _summary(by_kind["observed"]), "evidence": evidence})
    return {"checked_at": now, "features": out, "counts": {s: counts[s] for s in STATUSES},
            "coverage": coverage, "database": database, "basis": BASIS}
