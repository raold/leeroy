"""The realm's places: the CSV mod-leeroy reads (`Leeroy.Realm.PlacesFile`), parsed exactly as
strictly as the native ParsePlaces (LeeroyRealmCommands.cpp), and the capital zones copied from
LeeroyRealmCommands.h. Pure: nothing here talks to the game.

A file the native module would refuse is refused whole here too (PlacesError). The native side
also drops a place whose map, coordinates and zone disagree (ValidPlace, against the maps); the
mind cannot check that, so such a place is listed here but never moves anyone."""
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
import math
from pathlib import Path
import re
import struct

HORDE_CAPITALS = (1637, 1497, 1638, 3487)  # Orgrimmar, Undercity, Thunder Bluff, Silvermoon
ALLIANCE_CAPITALS = (1519, 1537, 1657, 3557)  # Stormwind, Ironforge, Darnassus, the Exodar
NEUTRAL_CITIES = (3703, 4395)  # Shattrath, Dalaran
# All ten carry AREA_FLAG_SLAVE_CAPITAL, the flag the core tests before it lets a player into
# the City channel (Player::CanJoinConstantChannelInZone, Player.cpp:5079). There is one City
# channel per faction (ChannelMgr::forTeam), so a Horde line in Trade reaches every Horde capital
# and both neutral cities, and no Alliance one.
_TRADE = frozenset((*HORDE_CAPITALS, *ALLIANCE_CAPITALS, *NEUTRAL_CITIES))

PLACE_KINDS = ("city", "inn", "fish", "duel", "holiday", "arena", "arenamaster", "herb", "ore",
               "camp", "show", "home", "photo")
HEADER = "name,map,zone,x,y,z,o,kind"
MAX_BYTES, MAX_ROWS, MAX_LINE_BYTES = 1024 * 1024, 4096, 1024
FILE_NAME = "leeroy_places.csv"
DIST = Path(__file__).resolve().parents[3] / "modules" / "mod-leeroy" / "conf" / "leeroy_places.csv.dist"

_NAME = re.compile(r"[a-z0-9_]{1,64}")
_UINT = re.compile(r"[0-9]+")
# std::from_chars(float, general): no sign but '-', no whitespace, no digit separators.
_FLOAT = re.compile(r"-?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
_TRIM = " \t\r"


class PlacesError(ValueError):
    """The file is missing or the native module would refuse it; the message names a line only."""


@dataclass(frozen=True, slots=True)
class Place:
    name: str
    map: int
    zone: int
    x: float
    y: float
    z: float
    o: float
    kind: str


def trade_visible(zone: int) -> bool:
    """A zone where the core opens the City channel (Trade): the ten cities."""
    return type(zone) is int and zone in _TRADE


def trade_cities(faction: str) -> frozenset[int]:
    """The zones a faction's Trade line is heard in: its own capitals and both neutral cities."""
    own = {"horde": HORDE_CAPITALS, "alliance": ALLIANCE_CAPITALS}.get(faction)
    if own is None:
        raise ValueError("faction must be horde or alliance")
    return frozenset((*own, *NEUTRAL_CITIES))


def _f32(value: float) -> float | None:
    """The value as the native float holds it, or None when a float cannot hold it."""
    try:
        return struct.unpack("<f", struct.pack("<f", value))[0]
    except OverflowError:
        return None


def _uint(text: str, low: int, high: int) -> int | None:
    if _UINT.fullmatch(text) is None:
        return None
    value = int(text)
    return value if low <= value <= high else None


def _float(text: str, low: float, high: float) -> float | None:
    if _FLOAT.fullmatch(text) is None:
        return None
    value = float(text)
    held = _f32(value)
    if held is None or not math.isfinite(held) or not _f32(low) <= held <= _f32(high):
        return None
    return value


def _row(fields: list[str], names: set[str]) -> Place | str:
    if len(fields) != 8:
        return "expected 8 columns"
    name = fields[0]
    if _NAME.fullmatch(name) is None or name == "near_human":
        return "invalid name"
    if name in names:
        return "duplicate name"
    names.add(name)
    map_id, zone = _uint(fields[1], 0, 999), _uint(fields[2], 1, 65535)
    if map_id is None:
        return "invalid map"
    if zone is None:
        return "invalid zone"
    x, y, z = (_float(f, -20000, 20000) for f in fields[3:6])
    if x is None or y is None or z is None:
        return "invalid coordinates"
    o = _float(fields[6], 0, 6.283185307179586)
    if o is None:
        return "invalid orientation"
    if fields[7] not in PLACE_KINDS:
        return "invalid kind"
    return Place(name, map_id, zone, x, y, z, o, fields[7])


def parse_places(data: bytes) -> dict[str, Place]:
    """The places of one file's bytes, in file order, or PlacesError("line N: why") as the native
    ParsePlaces words it. Bytes are read one to one (latin-1), as the native side does."""
    if len(data) > MAX_BYTES:
        raise PlacesError("line 1: file too large")
    out: dict[str, Place] = {}
    names: set[str] = set()
    seen_header = False
    for number, raw in enumerate(data.decode("latin-1").split("\n"), 1):
        if len(raw) > MAX_LINE_BYTES:
            raise PlacesError(f"line {number}: line too long")
        line = raw.strip(_TRIM)
        if not line or line.startswith("#"):
            continue
        if "\0" in line or '"' in line:
            raise PlacesError(f"line {number}: invalid characters")
        if not seen_header:
            if line != HEADER:
                raise PlacesError(f"line {number}: invalid header")
            seen_header = True
            continue
        if len(out) >= MAX_ROWS:
            raise PlacesError(f"line {number}: too many rows")
        place = _row([f.strip(_TRIM) for f in line.split(",")], names)
        if isinstance(place, str):
            raise PlacesError(f"line {number}: {place}")
        out[place.name] = place
    if not seen_header:
        raise PlacesError("line 1: missing header")
    return out


def load_places(path: Path) -> dict[str, Place]:
    """The places in the file at `path` (parse_places); a file that cannot be read is
    PlacesError too, without its path."""
    try:
        data = Path(path).read_bytes()
    except OSError:
        raise PlacesError("places file unavailable") from None
    return parse_places(data)


def places_path(worldserver_conf: Path | None) -> Path:
    """The live file beside the module configs (`<configs>/modules/leeroy_places.csv`, next to
    `[guild] worldserver_conf`) when it exists, else the repo's dist."""
    if worldserver_conf is not None:
        live = Path(worldserver_conf).parent / "modules" / FILE_NAME
        if live.is_file():
            return live
    return DIST


class Places(Mapping[str, Place]):
    """The loaded places by name, in file order."""

    def __init__(self, places: Mapping[str, Place]):
        self._places = dict(places)

    @classmethod
    def load(cls, path: Path) -> "Places":
        return cls(load_places(path))

    def __getitem__(self, name: str) -> Place:
        return self._places[name]

    def __iter__(self) -> Iterator[str]:
        return iter(self._places)

    def __len__(self) -> int:
        return len(self._places)

    def by_kind(self, kind: str, *, zone: int | None = None) -> list[Place]:
        """The places of one kind, in file order; with `zone`, only those in it."""
        if kind not in PLACE_KINDS:
            raise ValueError("unknown place kind")
        return [p for p in self._places.values()
                if p.kind == kind and (zone is None or p.zone == zone)]

    def zone_of(self, name: str) -> int:
        """The zone of a named place (KeyError for a name the file does not have)."""
        return self._places[name].zone
