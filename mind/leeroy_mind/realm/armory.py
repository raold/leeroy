"""Minimal read-only game armory; construction performs no I/O.

Every query is one SELECT constant (QUERIES) whose only parameters are validated integers, run
through library.mysql.query's read-only session with a server-side time limit. The methods are
synchronous: a caller in the event loop runs them with asyncio.to_thread. Rows are parsed
strictly, and anything unexpected is RealmArmoryUnavailable with a safe message, never the
database's own text."""

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
import re

from leeroy_mind.guild.armory import average_item_level
from leeroy_mind.library import mysql
from leeroy_mind.personas.roster import DbInfo
from leeroy_mind.wow import RACES, faction_known

GUILD_ID = 21  # the guild mod-leeroy serves (app.GUILD_ID; app imports the realm, not the reverse)
_U32 = 0xFFFFFFFF
_CHUNK = 500
_RACES = {f: tuple(sorted(r for r in RACES if faction_known(r) == f)) for f in ("horde", "alliance")}

_GUILD_RANKS = "SELECT rid FROM guild_rank WHERE guildid={guild_id:d} ORDER BY rid"
QUERIES = {
    "guild_ranks": _GUILD_RANKS,
    "guild_name": "SELECT name FROM guild WHERE guildid={guild_id:d}",
    "guild_size": "SELECT COUNT(*) FROM guild_member WHERE guildid={guild_id:d}",
    "guild_leaders": (
        "SELECT g.guildid, g.name, g.leaderguid, c.account, c.race FROM guild g "
        "JOIN characters c ON c.guid = g.leaderguid WHERE g.guildid <> {own:d} ORDER BY g.guildid"),
    # Playerbots' random-bot accounts (Playerbots.RandomBotAccountPrefix "rndbot"; the
    # collation ignores case).
    "random_bot_accounts": (
        "SELECT id FROM account WHERE username LIKE 'RNDBOT%' AND id IN ({ids}) ORDER BY id"),
    "guild_member_levels": (
        "SELECT gm.guildid, c.level, c.race, COUNT(*) FROM guild_member gm "
        "JOIN characters c ON c.guid = gm.guid WHERE gm.guildid IN ({ids}) "
        "GROUP BY gm.guildid, c.level, c.race ORDER BY gm.guildid, c.level, c.race"),
    "guildless_80s": (
        "SELECT c.guid, c.name, c.race, c.class, c.account FROM characters c "
        "LEFT JOIN guild_member gm ON gm.guid = c.guid WHERE c.level = 80 AND gm.guid IS NULL "
        "AND c.deleteInfos_Account IS NULL AND c.race IN ({races}) ORDER BY c.guid"),
    "equipped": (
        "SELECT ci.guid, ci.slot, ii.itemEntry FROM character_inventory ci "
        "JOIN item_instance ii ON ii.guid = ci.item "
        "WHERE ci.bag = 0 AND ci.slot < 19 AND ci.guid IN ({ids}) ORDER BY ci.guid, ci.slot"),
    "item_levels": "SELECT entry, ItemLevel FROM item_template WHERE entry IN ({ids}) ORDER BY entry",
    # Every row counts, an orphaned one too: the core's realm-first check reads them all.
    "achievement_holders": (
        "SELECT ca.achievement, ca.guid, c.name, gm.guildid FROM character_achievement ca "
        "LEFT JOIN characters c ON c.guid = ca.guid LEFT JOIN guild_member gm ON gm.guid = ca.guid "
        "WHERE ca.achievement IN ({ids}) ORDER BY ca.achievement, ca.guid"),
    "character_account": "SELECT account FROM characters WHERE guid={guid:d}",
    "gm_level": "SELECT MAX(gmlevel) FROM account_access WHERE id={account:d}",
    "account_permission": (
        "SELECT granted FROM rbac_account_permissions "
        "WHERE accountId={account:d} AND permissionId={perm:d}"),
}


class RealmArmoryUnavailable(RuntimeError):
    """A safe diagnostic, containing no configuration or database error text."""


@dataclass(frozen=True, slots=True)
class BotGuild:
    """A guild led by a random bot. `members` counts its members by (level, faction)."""
    guild_id: int
    name: str
    leader_guid: int
    faction: str  # the leader's
    members: dict[tuple[int, str], int]

    def count(self, *, level: int | None = None, faction: str | None = None) -> int:
        return sum(n for (lv, fa), n in self.members.items()
                   if (level is None or lv == level) and (faction is None or fa == faction))


@dataclass(frozen=True, slots=True)
class Candidate:
    """A guildless level-80 character on a random-bot account. `avg_ilvl` is the mean item
    level of its equipped items, shirt and tabard aside (0.0 with nothing equipped)."""
    guid: int
    name: str
    race: int
    class_id: int
    faction: str
    avg_ilvl: float


def _positive(value, high: int = _U32, what: str = "id") -> int:
    if type(value) is not int or not 0 < value <= high:
        raise ValueError(f"{what} must be a positive integer up to {high}")
    return value


def _ids(values: Iterable[int], high: int = _U32) -> tuple[int, ...]:
    return tuple(sorted({_positive(v, high) for v in values}))


def _chunks(values: tuple[int, ...]):
    for start in range(0, len(values), _CHUNK):
        yield ",".join(map(str, values[start:start + _CHUNK]))


def _int(value, low: int = 0, high: int = _U32) -> int:
    if type(value) is not str or re.fullmatch(r"-?[0-9]+", value) is None:
        raise RealmArmoryUnavailable("armory evidence unavailable")
    number = int(value)
    if not low <= number <= high:
        raise RealmArmoryUnavailable("armory evidence unavailable")
    return number


def _text(value, *, empty: bool = False) -> str:
    if type(value) is not str or (not empty and not value):
        raise RealmArmoryUnavailable("armory evidence unavailable")
    return value


def _columns(rows, n: int):
    for row in rows:
        if len(row) != n:
            raise RealmArmoryUnavailable("armory evidence unavailable")
        yield row


def _faction(race: int, want: str | None = None) -> str:
    faction = faction_known(race)
    if faction is None or (want is not None and faction != want):
        raise RealmArmoryUnavailable("armory evidence unavailable")
    return faction


class RealmArmory:
    def __init__(self, char_db: DbInfo, world_db: DbInfo, auth_db: DbInfo, mysql_exe: Path):
        for db in (char_db, world_db, auth_db):
            if type(db.name) is not str or re.fullmatch(r"[A-Za-z0-9_]+", db.name) is None:
                raise RealmArmoryUnavailable("armory configuration unavailable")
        self.chars, self.world, self.auth = char_db, world_db, auth_db
        self.mysql_exe = Path(mysql_exe)

    def _rows(self, db: DbInfo, name: str, **values) -> list[list[str | None]]:
        sql = QUERIES[name].format(**values)
        try:
            mysql.check_select(sql)
            return mysql.query(db, sql, self.mysql_exe, timeout=10, max_ms=5000)
        except mysql.QueryError:
            raise RealmArmoryUnavailable("armory query unavailable") from None

    def _random_bot_accounts(self, accounts: Iterable[int]) -> set[int]:
        found = set()
        for ids in _chunks(tuple(sorted(set(accounts)))):
            found.update(_int(row[0], 1) for row in _columns(
                self._rows(self.auth, "random_bot_accounts", ids=ids), 1))
        return found

    def guild_ranks(self, guild_id: int) -> list[int]:
        if type(guild_id) is not int or not 0 < guild_id <= 0xFFFFFFFF:
            raise ValueError("guild id must be a positive uint32 integer")
        rows = self._rows(self.chars, "guild_ranks", guild_id=guild_id)
        ranks = []
        for row in rows:
            if (len(row) != 1 or type(row[0]) is not str
                    or re.fullmatch(r"[0-9]", row[0]) is None):
                raise RealmArmoryUnavailable("armory rank evidence unavailable")
            ranks.append(int(row[0]))
        if len(ranks) != len(set(ranks)):
            raise RealmArmoryUnavailable("armory rank evidence unavailable")
        return sorted(ranks)

    def guild_name(self, guild_id: int) -> str:
        """The guild's name, or "" when there is no such guild."""
        rows = self._rows(self.chars, "guild_name", guild_id=_positive(guild_id))
        if len(rows) > 1:
            raise RealmArmoryUnavailable("armory evidence unavailable")
        return _text(next(_columns(rows, 1))[0], empty=True) if rows else ""

    def guild_size(self, guild_id: int) -> int:
        rows = list(_columns(self._rows(self.chars, "guild_size", guild_id=_positive(guild_id)), 1))
        if len(rows) != 1:
            raise RealmArmoryUnavailable("armory evidence unavailable")
        return _int(rows[0][0])

    def bot_guilds(self) -> list[BotGuild]:
        """Guilds whose leader is on a random-bot account, guild 21 aside, by guild id."""
        leaders = [(_int(g, 1), _text(n), _int(lg, 1), _int(acc), _faction(_int(r, 1, 255)))
                   for g, n, lg, acc, r in _columns(self._rows(self.chars, "guild_leaders",
                                                               own=GUILD_ID), 5)]
        bots = self._random_bot_accounts(row[3] for row in leaders if row[3])
        leaders = [row for row in leaders if row[3] in bots]
        counts: dict[int, dict[tuple[int, str], int]] = {row[0]: {} for row in leaders}
        for ids in _chunks(tuple(sorted(counts))):
            for g, level, race, n in _columns(self._rows(self.chars, "guild_member_levels",
                                                         ids=ids), 4):
                guild = counts.get(_int(g, 1))
                if guild is None:
                    raise RealmArmoryUnavailable("armory evidence unavailable")
                key = (_int(level, 0, 255), _faction(_int(race, 1, 255)))
                guild[key] = guild.get(key, 0) + _int(n, 1)
        return [BotGuild(g, name, leader, faction, counts[g])
                for g, name, leader, _, faction in leaders]

    def guildless_80s(self, faction: str) -> list[Candidate]:
        """Level-80 characters of the faction on random-bot accounts with no guild row, online
        or not, by guid."""
        races = _RACES.get(faction) if type(faction) is str else None
        if races is None:
            raise ValueError("faction must be horde or alliance")
        rows = [(_int(g, 1), _text(n), _int(r, 1, 255), _int(c, 1, 255), _int(acc))
                for g, n, r, c, acc in _columns(self._rows(
                    self.chars, "guildless_80s", races=",".join(map(str, races))), 5)]
        for _, _, race, _, _ in rows:
            _faction(race, faction)
        bots = self._random_bot_accounts(row[4] for row in rows if row[4])
        rows = [row for row in rows if row[4] in bots]
        equipped: dict[int, dict[int, int]] = {row[0]: {} for row in rows}
        for ids in _chunks(tuple(sorted(equipped))):
            for g, slot, entry in _columns(self._rows(self.chars, "equipped", ids=ids), 3):
                slots = equipped.get(_int(g, 1))
                if slots is None:
                    raise RealmArmoryUnavailable("armory evidence unavailable")
                slots[_int(slot, 0, 18)] = _int(entry, 1)
        levels: dict[int, int] = {}
        for ids in _chunks(tuple(sorted({e for s in equipped.values() for e in s.values()}))):
            for entry, level in _columns(self._rows(self.world, "item_levels", ids=ids), 2):
                levels[_int(entry, 1)] = _int(level, 0, 0xFFFF)
        out = []
        for g, name, race, cls, _ in rows:
            known = {slot: levels[e] for slot, e in equipped[g].items() if e in levels}
            out.append(Candidate(g, name, race, cls, faction, average_item_level(known)))
        return out

    def achievement_holders(self, ids: Iterable[int]) -> dict[int, list[tuple[int, str, int]]]:
        """Who holds each achievement, as (guid, name, guild id); the name is "" for a row whose
        character is gone, the guild 0 for none."""
        wanted = _ids(ids, 0xFFFF)
        found: dict[int, list[tuple[int, str, int]]] = {a: [] for a in wanted}
        for chunk in _chunks(wanted):
            for a, g, name, guild in _columns(self._rows(self.chars, "achievement_holders",
                                                         ids=chunk), 4):
                holders = found.get(_int(a, 1, 0xFFFF))
                if holders is None:
                    raise RealmArmoryUnavailable("armory evidence unavailable")
                holders.append((_int(g, 1), "" if name is None else _text(name),
                                0 if guild is None else _int(guild, 1)))
        return found

    def _account(self, guid: int) -> int:
        rows = list(_columns(self._rows(self.chars, "character_account",
                                        guid=_positive(guid)), 1))
        if len(rows) != 1 or (account := _int(rows[0][0])) == 0:
            raise RealmArmoryUnavailable("armory character unavailable")
        return account

    def account_gm_level(self, guid: int) -> int:
        """The highest account_access.gmlevel of the character's account (0 for none)."""
        rows = list(_columns(self._rows(self.auth, "gm_level", account=self._account(guid)), 1))
        if len(rows) != 1:
            raise RealmArmoryUnavailable("armory evidence unavailable")
        return 0 if rows[0][0] is None else _int(rows[0][0], 0, 255)

    def account_has_permission(self, guid: int, perm: int) -> bool:
        """The character's account holds RBAC permission `perm` by an explicit account row:
        granted on some realm and denied on none (a denial wins, as in the core's RBAC). Roles
        are not expanded; permission 8 is linked to no role in the core's data."""
        perm = _positive(perm, what="permission")
        account = self._account(guid)
        granted = [_int(row[0], 0, 1) for row in _columns(
            self._rows(self.auth, "account_permission", account=account, perm=perm), 1)]
        return 1 in granted and 0 not in granted
