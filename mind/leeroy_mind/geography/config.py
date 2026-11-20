"""[geography] and [owner] in mind.toml, read once at the mind's start (geography/attach.py), and
the values in force, pinned for the whole process (as life/config.py pins [life]).

[geography] enabled (default true): her home in her card, her "where you live, right now" line,
her own clock (presence, her day, "Right now"), the place guard, and where he lives in her private
turns with him. false: none of it, every prompt and clock as before. An invalid table is off.

[owner] home (optional): his state or region as the place sheet knows it (geography.places
.lookup: "Montana", "MT", "Scotland", "the North West of England"); home_detail (optional): a town
or city, at most DETAIL_MAX characters of letters, spaces and . , ' -. Unset, unknown or invalid,
he has no home here and nothing changes. Neither value is ever logged, shown or stored: only
whether one is set.

The console has no mind.toml of the mind's to read: the mind writes whether geography is on to
meta META_KEY, and the console's Discord presence reads it there (`stored`)."""
import logging
import re
import sqlite3
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from .places import Place, lookup

log = logging.getLogger(__name__)
META_KEY = "geography:enabled"
DETAIL_MAX = 40
_DETAIL = re.compile(r"[^\W\d_](?:[^\W\d_]|[ .,'-])*", re.UNICODE)
_warned: set[tuple[str, str]] = set()


@dataclass(frozen=True, slots=True)
class GeographyCfg:
    enabled: bool = True


class OwnerHome(NamedTuple):
    place: Place
    detail: str = ""


def _warn(path: Path, key: str, what: str) -> None:
    if (str(path), key) not in _warned:
        _warned.add((str(path), key))
        log.warning("%s key=%s", what, key)


def _tables(path: Path) -> dict | None:
    """mind.toml's tables; {} when there is no file, None when it cannot be read or parsed."""
    try:
        return tomllib.loads(Path(path).read_bytes().decode("utf-8-sig"))
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        return None


def load_geography_config(path: Path) -> GeographyCfg:
    """[geography]: missing means on; an unreadable file or an invalid table means off (logged
    once per file and key)."""
    path = Path(path)
    tables = _tables(path)
    if tables is None:
        _warn(path, "file", "geography_config_invalid")
        return GeographyCfg(enabled=False)
    table = tables.get("geography", {})
    key = None
    if not isinstance(table, dict):
        key = "geography"
    elif set(table) - {"enabled"}:
        key = "unknown"
    elif "enabled" in table and type(table["enabled"]) is not bool:
        key = "enabled"
    if key is not None:
        _warn(path, key, "geography_config_invalid")
        return GeographyCfg(enabled=False)
    return GeographyCfg(**table)


def parse_owner(table) -> OwnerHome | None:
    """[owner]'s home, or None (unset, unknown or invalid: logged by key, never by value)."""
    if not isinstance(table, dict) or not table:
        return None
    home = table.get("home", "")
    if home == "":
        return None
    place = lookup(home) if isinstance(home, str) and len(home) <= 60 else None
    if place is None:
        log.warning("owner_home_invalid key=home")
        return None
    detail = table.get("home_detail", "")
    if not isinstance(detail, str):
        log.warning("owner_home_invalid key=home_detail")
        detail = ""
    detail = " ".join(detail.split())
    if detail and (len(detail) > DETAIL_MAX or not _DETAIL.fullmatch(detail)):
        log.warning("owner_home_invalid key=home_detail")
        detail = ""
    if set(table) - {"home", "home_detail"}:
        log.warning("owner_home_invalid key=unknown")
    return OwnerHome(place, detail)


def load_owner_home(path: Path) -> OwnerHome | None:
    """[owner] in mind.toml (parse_owner); None without the file or the table."""
    tables = _tables(Path(path))
    return parse_owner(tables.get("owner", {})) if tables else None


_on = True
_owner: OwnerHome | None = None


def pin(on: bool, owner: OwnerHome | None = None) -> None:
    """The values in force from now on: geography on only when `on` is True, and his home only
    while it is on."""
    global _on, _owner
    _on = on is True
    _owner = owner if _on and isinstance(owner, OwnerHome) else None


def on() -> bool:
    return _on


def owner() -> OwnerHome | None:
    return _owner


def stored(conn) -> bool:
    """Whether the mind last put geography in force (meta META_KEY "0" is off; no row, any other
    value or an unreadable meta table reads as on, the default)."""
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (META_KEY,)).fetchone()
    except sqlite3.Error:
        return True
    return not (row and row[0] == "0")
