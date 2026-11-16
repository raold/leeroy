"""The sandbox's directory: where it may be, the copy of the live data it is made from, and the
copy the sandbox mind runs on.

DIR/snapshot holds what was taken from the live data dir once (mind.db through SQLite's online
backup, migrated to this code's schema; mind.toml with the sandbox's switches; personas/,
prompts/ and realm/). DIR/mind-data is the sandbox mind's data dir, rebuilt from the snapshot at
every start and every reset; its library.db is copied once (read only by the mind) and its logs
are kept. The live data dir is only read: mind.db and library.db through `mode=ro` connections,
the rest by copying files out of it."""
import json
import math
import os
import re
import shutil
import sqlite3
import time
import tomllib
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

MARKER = ".leeroy-sandbox"
DB = "mind.db"
TOML = "mind.toml"
LIBRARY = "library.db"
COPIED_DIRS = ("personas", "prompts", "realm")
SQLITE_SIDE = ("-wal", "-shm", "-journal")

# What the sandbox's mind.toml changes, table by table, so that nothing acts on its own and
# nothing leaves the sandbox. Every key here is a key its table's loader knows (an unknown key
# would make some loaders read the whole table as off, others as invalid): tests/sandbox checks
# each table against its loader.
QUIET: dict[str, dict] = {
    # members never start a conversation with him: no unprompted whispers or openers
    "talk": {"whispers_per_hour": 0, "openers_per_hour": 0},
    # the shared Ollama: at most two of its four slots, beside the live mind's
    "ollama": {"max_inflight": 2},
    "control": {"enabled": True, "port": 0, "ops_actions": False},
    "voice": {"enabled": False, "autostart": False},
    "voice_notes": {"enabled": False},
    "training": {"enabled": False, "lab": False},
    "realm": {"enabled": False},
    "world": {"enabled": False},
    "selfies": {"enabled": False, "booth": False, "snaps": False},
    "photos": {"send": False, "requests": False},
    "banter": {"enabled": False},
    # the server's knowledge stays (presence, cards, guards); its own conversations and DMs stop
    "server": {"conversations_per_hour": 0, "dms_per_day": 0},
    "culture": {"calendar": False, "loot": False, "bots": False, "memes": False},
    "outreach": {"enabled": False},
    "playstyle": {"enabled": False},
    "raidplay": {"respond": False},
    "personas": {"auto_newcomers": False},
    "guild": {"offline_raids_per_day": 0, "regear_to": 0},
    "bakeoff": {"capture": True},
}
# Tables set only where the live mind.toml has them: absent, the feature is off already (and
# the core loader would warn about a section it does not know).
ONLY_IF_PRESENT = frozenset({"raidplay"})


class Refused(Exception):
    """The sandbox will not use that directory or that data, in words."""


@dataclass(frozen=True)
class Layout:
    root: Path

    @property
    def data(self) -> Path:
        return self.root / "mind-data"

    @property
    def snapshot(self) -> Path:
        return self.root / "snapshot"

    @property
    def info(self) -> Path:
        return self.root / "sandbox.json"


def _repository() -> Path:
    """The checkout this code runs from (mind/leeroy_mind/sandbox/prepare.py)."""
    return Path(__file__).resolve().parents[3]


def check_out(out, live) -> Layout:
    """DIR, refused when it is reached through a link or junction, is (inside) the live data dir
    or holds it, is inside a git checkout or this code's own tree, is a file, or is a folder
    with something in it that is not a sandbox."""
    raw = Path(os.path.abspath(Path(out).expanduser()))
    if any(p.is_symlink() or p.is_junction() for p in (raw, *raw.parents)):
        raise Refused("--out goes through a link or junction")
    target = raw.resolve()
    live = Path(live).expanduser().resolve()
    if target == live or target.is_relative_to(live) or live.is_relative_to(target):
        raise Refused("--out is the live data dir, inside it or around it")
    if target.is_relative_to(_repository()) or any(
            (p / ".git").exists() for p in (target, *target.parents)):
        raise Refused("--out is inside a git checkout")
    if target.exists():
        if not target.is_dir():
            raise Refused("--out is a file")
        if any(target.iterdir()) and not (target / MARKER).is_file():
            raise Refused("--out has files in it and is not a sandbox")
    return Layout(target)


def check_live(live) -> Path:
    live = Path(live).expanduser().resolve()
    for name in (DB, TOML):
        if not (live / name).is_file():
            raise Refused(f"no {name} in the data dir")
    return live


def make_root(layout: Layout) -> None:
    layout.root.mkdir(parents=True, exist_ok=True)
    marker = layout.root / MARKER
    if not marker.exists():
        marker.write_bytes(b"leeroy sandbox: private copies of the guild's data\n")
    layout.data.mkdir(exist_ok=True)


def remove_db(path: Path) -> None:
    for suffix in ("", *SQLITE_SIDE):
        target = Path(str(path) + suffix)
        for attempt in range(20):  # Windows: a closing handle (or a scanner) can hold it briefly
            try:
                target.unlink(missing_ok=True)
                break
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(0.1)


def backup_db(src: Path, dest: Path) -> None:
    """dest becomes a copy of src through SQLite's online backup, read in one transaction from a
    `mode=ro` connection: a consistent copy while the live mind writes, which it never blocks."""
    part = dest.with_name(dest.name + ".part")
    remove_db(part)
    with closing(sqlite3.connect(f"{src.resolve().as_uri()}?mode=ro", uri=True)) as source, \
            closing(sqlite3.connect(part)) as copy:
        source.backup(copy)
    remove_db(dest)
    os.replace(part, dest)


def _migrate(path: Path) -> None:
    """Bring the copy to this code's schema, then make it one self-contained file."""
    from leeroy_mind.storage.db import open_db
    conn = open_db(path)
    try:
        conn.commit()
        conn.autocommit = True
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("PRAGMA journal_mode=DELETE")
    finally:
        conn.close()


# -- mind.toml ------------------------------------------------------------------------------------

def quiet_toml(live_text: str) -> dict:
    """The live mind.toml with QUIET over it (tables merged key by key)."""
    try:
        doc = tomllib.loads(live_text)
    except tomllib.TOMLDecodeError as e:
        raise Refused(f"the live mind.toml does not parse: {e}") from None
    for table, keys in QUIET.items():
        current = doc.get(table)
        if current is None and table in ONLY_IF_PRESENT:
            continue
        if current is not None and not isinstance(current, dict):
            raise Refused(f"the live mind.toml's [{table}] is not a table")
        doc[table] = {**(current or {}), **keys}
    return doc


def with_bridge(doc: dict, port: int) -> dict:
    out = dict(doc)
    out["bridge"] = {**doc.get("bridge", {}), "host": "127.0.0.1", "port": int(port)}
    return out


_BARE = re.compile(r"[A-Za-z0-9_-]+\Z")


def _key(k: str) -> str:
    return k if _BARE.match(k) else json.dumps(k, ensure_ascii=False)


def _value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if not math.isfinite(v):
            raise Refused("the live mind.toml has a number the sandbox cannot copy")
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)  # a TOML basic string
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_value(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{" + ", ".join(f"{_key(k)} = {_value(x)}" for k, x in v.items()) + "}"
    raise Refused("the live mind.toml has a value the sandbox cannot copy")


def dump_toml(doc: dict) -> str:
    """TOML for a mind.toml as tomllib reads it: tables of strings, numbers, booleans and
    arrays (comments are not kept)."""
    lines: list[str] = []

    def table(prefix: str, d: dict) -> None:
        plain = [(k, v) for k, v in d.items() if not isinstance(v, dict)]
        nested = [(k, v) for k, v in d.items() if isinstance(v, dict)]
        if prefix and (plain or not nested):
            lines.extend(("", f"[{prefix}]"))
        lines.extend(f"{_key(k)} = {_value(v)}" for k, v in plain)
        for k, v in nested:
            table(f"{prefix}.{_key(k)}" if prefix else _key(k), v)
    table("", doc)
    return "\n".join(lines).lstrip("\n") + "\n"


def write_toml(path: Path, doc: dict) -> None:
    path.write_bytes(dump_toml(doc).encode("utf-8"))


# -- snapshot and restore -------------------------------------------------------------------------

def take_snapshot(live: Path, layout: Layout, *, now: float | None = None) -> dict:
    """Copy the live data into DIR/snapshot (replacing any earlier snapshot there)."""
    snap = layout.snapshot
    staging = layout.root / "snapshot.part"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir()
    doc = quiet_toml((live / TOML).read_bytes().decode("utf-8-sig"))
    write_toml(staging / TOML, with_bridge(doc, 0))
    backup_db(live / DB, staging / DB)
    _migrate(staging / DB)
    for name in COPIED_DIRS:
        if (live / name).is_dir():
            shutil.copytree(live / name, staging / name, symlinks=False)
    info = {"taken": time.time() if now is None else now, "source": str(live)}
    (staging / "snapshot.json").write_bytes(json.dumps(info).encode("utf-8"))
    if snap.exists():
        shutil.rmtree(snap)
    os.replace(staging, snap)
    return info


def snapshot_info(layout: Layout) -> dict | None:
    try:
        return json.loads((layout.snapshot / "snapshot.json").read_bytes())
    except (OSError, ValueError):
        return None


def copy_library(live: Path, layout: Layout, *, fresh: bool = False) -> bool:
    """library.db for the sandbox mind, copied once (the mind only reads it); False without one."""
    dest = layout.data / LIBRARY
    if dest.exists() and not fresh:
        return True
    if not (live / LIBRARY).is_file():
        remove_db(dest)
        return False
    backup_db(live / LIBRARY, dest)
    return True


def restore(layout: Layout, *, bridge_port: int) -> None:
    """DIR/mind-data from the snapshot: mind.db, the copied folders and mind.toml with the
    bridge's port. library.db, logs and the lock file are left as they are."""
    snap, data = layout.snapshot, layout.data
    if not (snap / DB).is_file():
        raise Refused("the sandbox has no snapshot: run it without --reuse once")
    data.mkdir(parents=True, exist_ok=True)
    remove_db(data / DB)
    shutil.copyfile(snap / DB, data / DB)
    for name in COPIED_DIRS:
        if (data / name).exists():
            shutil.rmtree(data / name)
        if (snap / name).is_dir():
            shutil.copytree(snap / name, data / name, symlinks=False)
    doc = tomllib.loads((snap / TOML).read_bytes().decode("utf-8"))
    write_toml(data / TOML, with_bridge(doc, bridge_port))
