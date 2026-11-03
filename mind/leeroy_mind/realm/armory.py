"""Minimal read-only game armory; construction performs no I/O."""

from pathlib import Path
import re

from leeroy_mind.library import mysql
from leeroy_mind.personas.roster import DbInfo

_GUILD_RANKS = "SELECT rid FROM guild_rank WHERE guildid={guild_id:d} ORDER BY rid"


class RealmArmoryUnavailable(RuntimeError):
    """A safe diagnostic, containing no configuration or database error text."""


class RealmArmory:
    def __init__(self, char_db: DbInfo, world_db: DbInfo, auth_db: DbInfo, mysql_exe: Path):
        for db in (char_db, world_db, auth_db):
            if type(db.name) is not str or re.fullmatch(r"[A-Za-z0-9_]+", db.name) is None:
                raise RealmArmoryUnavailable("armory configuration unavailable")
        self.chars, self.world, self.auth = char_db, world_db, auth_db
        self.mysql_exe = Path(mysql_exe)

    def guild_ranks(self, guild_id: int) -> list[int]:
        if type(guild_id) is not int or not 0 < guild_id <= 0xFFFFFFFF:
            raise ValueError("guild id must be a positive uint32 integer")
        sql = _GUILD_RANKS.format(guild_id=guild_id)
        try:
            mysql.check_select(sql)
            rows = mysql.query(self.chars, sql, self.mysql_exe, timeout=10, max_ms=5000)
        except mysql.QueryError:
            raise RealmArmoryUnavailable("armory query unavailable") from None
        ranks = []
        for row in rows:
            if (len(row) != 1 or type(row[0]) is not str
                    or re.fullmatch(r"[0-9]", row[0]) is None):
                raise RealmArmoryUnavailable("armory rank evidence unavailable")
            ranks.append(int(row[0]))
        if len(ranks) != len(set(ranks)):
            raise RealmArmoryUnavailable("armory rank evidence unavailable")
        return sorted(ranks)
