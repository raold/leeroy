"""A tiny reader for the 3.3.5a client DBC files the server keeps in its DataDir, for the names
the world database does not store: maps, zones, encounters, quest sorts, item sets, factions."""
import logging
import struct
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("leeroy_mind")


class DbcError(Exception):
    pass


class Dbc:
    """One WDBC file: records as tuples of uint32 fields plus the string block."""

    def __init__(self, data: bytes, name: str = "dbc"):
        if len(data) < 20 or data[:4] != b"WDBC":
            raise DbcError(f"{name}: not a WDBC file")
        _, count, fields, size, strings = struct.unpack("<4s4I", data[:20])
        if size != fields * 4 or 20 + count * size + strings != len(data):
            raise DbcError(f"{name}: header does not match the file size")
        body = data[20:20 + count * size]
        self.fields = fields
        self.records = [struct.unpack_from(f"<{fields}I", body, i * size) for i in range(count)]
        self._strings = data[20 + count * size:]

    @classmethod
    def read(cls, path: Path) -> "Dbc":
        return cls(Path(path).read_bytes(), Path(path).name)

    def string(self, offset: int) -> str:
        if offset <= 0 or offset >= len(self._strings):
            return ""
        end = self._strings.find(b"\0", offset)
        return self._strings[offset:end if end >= 0 else None].decode("utf-8", "replace")


@dataclass(frozen=True)
class MapInfo:
    id: int
    name: str
    instance_type: int  # 0 world, 1 dungeon, 2 raid, 3 battleground, 4 arena
    expansion: int      # 0 classic, 1 Burning Crusade, 2 Wrath of the Lich King
    max_players: int


@dataclass(frozen=True)
class Encounter:
    id: int
    map_id: int
    difficulty: int
    order: int
    name: str


@dataclass
class ClientNames:
    maps: dict[int, MapInfo] = field(default_factory=dict)
    sizes: dict[tuple[int, int], int] = field(default_factory=dict)   # (map, difficulty) -> players
    areas: dict[int, str] = field(default_factory=dict)
    quest_sorts: dict[int, str] = field(default_factory=dict)
    quest_infos: dict[int, str] = field(default_factory=dict)
    encounters: dict[int, Encounter] = field(default_factory=dict)
    item_sets: dict[int, str] = field(default_factory=dict)
    factions: dict[int, str] = field(default_factory=dict)
    extended_costs: dict[int, tuple] = field(default_factory=dict)   # id -> record
    loaded: list[str] = field(default_factory=list)


# file -> (expected field count, loader)
def _maps(d: Dbc, n: ClientNames):
    for r in d.records:
        n.maps[r[0]] = MapInfo(r[0], d.string(r[5]), r[2], r[63], r[65])


def _sizes(d: Dbc, n: ClientNames):
    for r in d.records:
        n.sizes[(r[1], r[2])] = r[21]


def _areas(d: Dbc, n: ClientNames):
    n.areas.update({r[0]: d.string(r[11]) for r in d.records})


def _sorts(d: Dbc, n: ClientNames):
    n.quest_sorts.update({r[0]: d.string(r[1]) for r in d.records})


def _infos(d: Dbc, n: ClientNames):
    n.quest_infos.update({r[0]: d.string(r[1]) for r in d.records})


def _encounters(d: Dbc, n: ClientNames):
    for r in d.records:
        n.encounters[r[0]] = Encounter(r[0], r[1], r[2], r[3], d.string(r[5]))


def _sets(d: Dbc, n: ClientNames):
    n.item_sets.update({r[0]: d.string(r[1]) for r in d.records})


def _factions(d: Dbc, n: ClientNames):
    n.factions.update({r[0]: d.string(r[23]) for r in d.records})


def _costs(d: Dbc, n: ClientNames):
    n.extended_costs.update({r[0]: r for r in d.records})


_FILES = {
    "Map.dbc": (66, _maps), "MapDifficulty.dbc": (23, _sizes), "AreaTable.dbc": (36, _areas),
    "QuestSort.dbc": (18, _sorts), "QuestInfo.dbc": (18, _infos),
    "DungeonEncounter.dbc": (23, _encounters), "ItemSet.dbc": (53, _sets),
    "Faction.dbc": (57, _factions), "ItemExtendedCost.dbc": (16, _costs),
}


def load_client_names(dbc_dir: Path | None) -> ClientNames:
    """Whatever of the known files exist and have the 3.3.5a layout; a missing or odd file is
    logged and skipped, so names fall back to ids."""
    names = ClientNames()
    if dbc_dir is None:
        return names
    for fname, (fields, loader) in _FILES.items():
        path = Path(dbc_dir) / fname
        try:
            d = Dbc.read(path)
        except (OSError, DbcError) as e:
            log.warning("library: skipping %s: %s", fname, e)
            continue
        if d.fields != fields:
            log.warning("library: %s has %d fields, expected %d; skipped", fname, d.fields, fields)
            continue
        loader(d, names)
        names.loaded.append(fname)
    return names
