"""The 3.3.5a spell book, loaded once before training in a worker thread.

Spell.dbc is deliberately not decoded by the generic row reader: only the columns
used here survive in compact, read-only int32 buffers. SpellInfo values are made
on demand. These are client base values, not effective costs, damage or cast times
after talents, gear, auras and server corrections.
"""
import logging
import re
import struct
from array import array
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from leeroy_mind.library.dbc import Dbc, DbcError
from leeroy_mind.library.mysql import query
from leeroy_mind.personas.roster import DbInfo

log = logging.getLogger("leeroy_mind")

# DBCStructure.h, SpellEntry (234 fields), SpellCastTimesEntry (4),
# SkillLineAbilityEntry (14), SpellRuneCostEntry (5), client build 12340.
_SPELL_FIELDS = 234
_NUMERIC = (0, 28, 29, 30, 39, 41, 42, 71, 72, 73, 74, 75, 76,
            80, 81, 82, 95, 96, 97, 116, 117, 118, 204, 206, 225, 226)
_MAX_RECORDS = 200_000
_MAX_FILE_BYTES = 256 * 1024 * 1024
_MAX_SMALL_BYTES = 16 * 1024 * 1024
_MAX_STRINGS = 16 * 1024 * 1024
_MAX_NAME_BYTES = 4096
_U32 = 0xFFFFFFFF
_INTERRUPT = 68
_BONUS_SQL = ("SELECT entry, ROUND(direct_bonus*100), ROUND(dot_bonus*100) "
              "FROM spell_bonus_data")


@dataclass(frozen=True, slots=True)
class Effect:
    kind: int
    aura: int
    base: int
    die: int
    trigger: int


@dataclass(frozen=True, slots=True)
class SpellInfo:
    id: int
    name: str
    rank: str
    cast_ms: int
    cooldown_ms: int
    gcd_ms: int
    power_type: int
    cost: int
    cost_pct: int
    rune_cost: tuple[int, int, int]
    school: int
    level: int
    effects: tuple[Effect, ...]
    interrupt: bool


def _empty_mapping():
    return MappingProxyType({})


@dataclass(frozen=True, slots=True)
class SpellBook:
    """Immutable column storage; public lookups never expose mutable containers.

    Missing ancillary rows use zero (including cast time and rune costs), matching
    client defaults. Check loaded for source-table coverage before interpreting a
    zero; availability of a table still does not prove an individual row exists.
    Unknown bonus rows return (0, 0); explicit negative DB sentinel values survive.
    """
    _columns: Mapping[int, memoryview] = field(default_factory=_empty_mapping, repr=False)
    _rows: Mapping[int, int] = field(default_factory=_empty_mapping, repr=False)
    _names: tuple[str, ...] = field(default=(), repr=False)
    _ranks: tuple[str, ...] = field(default=(), repr=False)
    _named_ids: Mapping[str, frozenset[int]] = field(default_factory=_empty_mapping, repr=False)
    _casts: Mapping[int, int] = field(default_factory=_empty_mapping, repr=False)
    _runes: Mapping[int, tuple[int, int, int]] = field(default_factory=_empty_mapping, repr=False)
    _abilities: tuple[tuple[int, int], ...] = field(default=(), repr=False)
    _successors: Mapping[int, frozenset[int]] = field(default_factory=_empty_mapping, repr=False)
    _world: Mapping[int, tuple[int, int]] = field(default_factory=_empty_mapping, repr=False)
    loaded: frozenset[str] = frozenset()

    def _uint(self, col: int, row: int) -> int:
        return self._columns[col][row] & _U32

    def info(self, id: int) -> SpellInfo | None:
        row = self._rows.get(id)
        if row is None:
            return None
        u = lambda col: self._uint(col, row)
        effects = tuple(Effect(u(71 + i), u(95 + i), self._columns[80 + i][row],
                               self._columns[74 + i][row], u(116 + i)) for i in range(3))
        return SpellInfo(id, self._names[row], self._ranks[row], self._casts.get(u(28), 0),
                         max(u(29), u(30)), u(206), self._columns[41][row], u(42), u(204),
                         self._runes.get(u(226), (0, 0, 0)), u(225), u(39), effects,
                         any(e.kind == _INTERRUPT for e in effects))

    def name(self, id: int) -> str:
        row = self._rows.get(id)
        return self._names[row] if row is not None else f"spell {id}"

    def ids(self, name: str) -> frozenset[int]:
        return self._named_ids.get(name.casefold(), frozenset())

    def class_spells(self, cls: int, level: int = 80) -> list[SpellInfo]:
        """One highest available rank per name, sorted by name then id.

        SkillLineAbility supplies class membership and supersession. A direct
        available successor wins before the numeric enUS rank label, level and id
        break ties. Malformed cycles fall back to that same stable ordering; no
        graph walk can loop or grow with an adversarial supersession chain.
        """
        if type(cls) is not int or cls not in (1, 2, 3, 4, 5, 6, 7, 8, 9, 11):
            return []
        if type(level) is not int or level < 0:
            return []
        mask = 1 << (cls - 1)
        groups: dict[str, set[int]] = {}
        for id, classes in self._abilities:
            row = self._rows.get(id)
            if classes & mask and row is not None and self._uint(39, row) <= level:
                # Unnamed malformed entries must not collapse into one rank chain.
                key = self._names[row].casefold() or f"\0{id}"
                groups.setdefault(key, set()).add(id)

        def rank_key(id):
            row = self._rows[id]
            match = re.fullmatch(r"Rank\s+(\d{1,9})", self._ranks[row], re.IGNORECASE)
            return (int(match[1]) if match else 0, self._uint(39, row), id)

        chosen = []
        for ids in groups.values():
            terminal = [id for id in ids if not self._successors.get(id, frozenset()) & ids]
            chosen.append(max(terminal or ids, key=rank_key))
        return [self.info(id) for id in sorted(chosen, key=lambda id: (
            self._names[self._rows[id]].casefold(), id))]

    def coefficient(self, id: int) -> tuple[int, int]:
        return self._world.get(id, (0, 0))

    def cast_time_known(self, id: int) -> bool:
        """Whether this spell references an actually available cast-time row.

        Neither a loaded table nor the zero returned by info() proves that row.
        A signed negative time is retained by info(), but is not usable timing.
        """
        row = self._rows.get(id)
        return row is not None and self._casts.get(self._uint(28, row), -1) >= 0

    def coefficient_known(self, id: int) -> bool:
        """Whether an explicit, non-sentinel direct coefficient is available.

        This says nothing about periodic coefficients; a negative periodic
        sentinel does not erase a known direct coefficient, including zero.
        """
        values = self._world.get(id)
        return values is not None and values[0] >= 0


def _read_checked(path: Path, fields: int, limit: int) -> tuple[bytes, int, int]:
    """Validate bounds before allocating the file or asking Dbc to build row tuples."""
    with path.open("rb") as stream:
        header = stream.read(20)
        if len(header) != 20 or header[:4] != b"WDBC":
            raise DbcError(f"{path.name}: not a WDBC file")
        _, count, actual_fields, size, strings = struct.unpack("<4s4I", header)
        if actual_fields != fields or size != fields * 4:
            raise DbcError(f"{path.name}: unexpected field count or record size")
        expected = 20 + count * size + strings
        if count > _MAX_RECORDS or strings > _MAX_STRINGS or expected > limit:
            raise DbcError(f"{path.name}: table exceeds spell reader bounds")
        stream.seek(0, 2)
        if stream.tell() != expected:
            raise DbcError(f"{path.name}: header does not match the file size")
        stream.seek(0)
        data = stream.read(expected + 1)
        if len(data) != expected:
            raise DbcError(f"{path.name}: file changed while reading")
    return data, count, size


def _spell_columns(path: Path):
    data, count, size = _read_checked(path, _SPELL_FIELDS, _MAX_FILE_BYTES)
    strings = data[20 + count * size:]
    string_cache: dict[int, str] = {0: ""}

    def string(offset: int) -> str:
        if offset not in string_cache:
            if offset >= len(strings):
                raise DbcError("Spell.dbc: name offset outside string block")
            end = strings.find(b"\0", offset, offset + _MAX_NAME_BYTES + 1)
            if end < 0:
                raise DbcError("Spell.dbc: unterminated or oversized name")
            string_cache[offset] = strings[offset:end].decode("utf-8", "replace")
        return string_cache[offset]

    # Never unpack a complete Spell row. array('i') preserves each 32-bit word;
    # unsigned columns are masked at lookup, signed base/die/power stay signed.
    columns = {col: array("i") for col in _NUMERIC}
    for col, values in columns.items():
        for row in range(count):
            values.append(struct.unpack_from("<i", data, 20 + row * size + col * 4)[0])
    names, ranks, rows, named_ids = [], [], {}, {}
    for row in range(count):
        id = columns[0][row] & _U32
        if id in rows:
            raise DbcError("Spell.dbc: duplicate spell id")
        rows[id] = row
        name = string(struct.unpack_from("<I", data, 20 + row * size + 136 * 4)[0])
        rank = string(struct.unpack_from("<I", data, 20 + row * size + 153 * 4)[0])
        names.append(name)
        ranks.append(rank)
        named_ids.setdefault(name.casefold(), set()).add(id)
    # bytes owns the storage; even a caller inspecting private memoryviews cannot
    # write into it, unlike a readonly view over a surviving mutable array.
    frozen = {col: memoryview(values.tobytes()).cast("i") for col, values in columns.items()}
    return frozen, rows, tuple(names), tuple(ranks), {
        name: frozenset(ids) for name, ids in named_ids.items()}


def _small_table(dbc_dir: Path, name: str, fields: int) -> Dbc | None:
    try:
        data, _, _ = _read_checked(dbc_dir / name, fields, _MAX_SMALL_BYTES)
        return Dbc(data, name)
    except (OSError, DbcError) as error:
        log.warning("training: skipping %s: %s", name, error)
        return None


def _bonus_values(world) -> dict[int, tuple[int, int]]:
    if len(world) > _MAX_RECORDS:
        raise ValueError("invalid spell bonus data: too many rows")
    copied = {}
    for id, values in world.items():
        if (type(id) is not int or not 0 <= id <= _U32
                or not isinstance(values, (tuple, list)) or len(values) != 2
                or any(type(v) is not int for v in values)):
            raise ValueError("invalid spell bonus data")
        copied[id] = tuple(values)
    return copied


def load_spellbook(dbc_dir: Path, *, world: dict[int, tuple[int, int]] | None = None) -> SpellBook:
    """Read local files only; optional bonus data is copied, never fetched implicitly.

    Obtain this folder with library.world.dbc_dir_of(worldserver_conf). Missing or
    malformed tables are logged and skipped, without preventing training startup.
    """
    bonuses = MappingProxyType(_bonus_values(world or {}))
    loaded = {"spell_bonus_data"} if world is not None else set()
    dbc_dir = Path(dbc_dir)
    try:
        columns, rows, names, ranks, named_ids = _spell_columns(dbc_dir / "Spell.dbc")
    except (OSError, DbcError) as error:
        log.warning("training: skipping Spell.dbc: %s", error)
        return SpellBook(_world=bonuses, loaded=frozenset(loaded))
    loaded.add("Spell.dbc")
    casts, runes, abilities, successors = {}, {}, set(), {}
    d = _small_table(dbc_dir, "SpellCastTimes.dbc", 4)
    if d is not None:
        loaded.add("SpellCastTimes.dbc")
        for r in d.records:
            casts[r[0]] = r[1] if r[1] < 0x80000000 else r[1] - 0x100000000
    d = _small_table(dbc_dir, "SpellRuneCost.dbc", 5)
    if d is not None:
        loaded.add("SpellRuneCost.dbc")
        runes = {r[0]: (r[1], r[2], r[3]) for r in d.records}
    d = _small_table(dbc_dir, "SkillLineAbility.dbc", 14)
    if d is not None:
        loaded.add("SkillLineAbility.dbc")
        for r in d.records:
            abilities.add((r[2], r[4]))
            if r[8]:
                successors.setdefault(r[2], set()).add(r[8])
    return SpellBook(MappingProxyType(columns), MappingProxyType(rows), names, ranks,
                     MappingProxyType(named_ids), MappingProxyType(casts), MappingProxyType(runes),
                     tuple(sorted(abilities)), MappingProxyType({
                         id: frozenset(ids) for id, ids in successors.items()}), bonuses,
                     loaded=frozenset(loaded))


def read_bonus_data(db: DbInfo, mysql_exe: Path) -> dict[int, tuple[int, int]]:
    """Read the world's direct/periodic coefficients in integer hundredths."""
    values = {}
    for row in query(db, _BONUS_SQL, mysql_exe):
        try:
            if len(row) != 3 or any(v is None for v in row):
                raise ValueError
            id, direct, dot = (int(v) for v in row)
            if id in values or not 0 <= id <= _U32 or len(values) >= _MAX_RECORDS:
                raise ValueError
        except (TypeError, ValueError):
            # Do not echo DB rows, connection information or credentials.
            raise ValueError("invalid spell bonus data") from None
        values[id] = (direct, dot)
    return values
