"""The guild roster: read from the world database (read-only), a JSON file or the bridge.

Credentials come from worldserver.conf, travel to mysql.exe through a temporary option file and
never appear on a command line, in a repr or in a log."""
import asyncio
import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from ..protocol import Cast, Member

DEFAULT_CONF = Path(r"F:\azerothcore\build\bin\RelWithDebInfo\configs\worldserver.conf")
DEFAULT_MYSQL = Path(r"C:\Program Files\MySQL\MySQL Server 8.4\bin\mysql.exe")

ROSTER_SQL = ("SELECT c.guid, c.name, c.race, c.class, c.gender, c.level, gm.rank, c.online, "
              "c.map, c.zone FROM guild_member gm JOIN characters c ON c.guid = gm.guid "
              "WHERE gm.guildid = {guild_id:d} ORDER BY c.guid")


class RosterError(Exception):
    pass


@dataclass(frozen=True)
class DbInfo:
    host: str
    port: int
    user: str
    password: str = field(repr=False)
    name: str

    def __repr__(self) -> str:
        return (f"DbInfo(host={self.host!r}, port={self.port}, user={self.user!r}, "
                f"password='***', name={self.name!r})")


def read_db_info(worldserver_conf: Path, key: str = "CharacterDatabaseInfo") -> DbInfo:
    text = Path(worldserver_conf).read_text(encoding="utf-8", errors="replace")
    m = re.search(rf'^\s*{re.escape(key)}\s*=\s*"([^"]*)"', text, re.MULTILINE)
    if m is None:
        raise RosterError(f"{key} not found in {worldserver_conf}")
    parts = m.group(1).split(";")
    if len(parts) < 5:
        raise RosterError(f"{key} must be host;port;user;password;database")
    try:
        port = int(parts[1])
    except ValueError:
        raise RosterError(f"{key}: the port is not a number") from None
    return DbInfo(parts[0], port, parts[2], ";".join(parts[3:-1]), parts[-1])


def parse_roster_tsv(text: str) -> list[Member]:
    """Rows of guid, name, race, class, gender, level, rank, online, map, zone."""
    out = []
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        f = line.rstrip("\r\n").split("\t")
        if len(f) != 10:
            raise RosterError(f"roster line {n}: expected 10 columns, got {len(f)}")
        try:
            out.append(Member(guid=int(f[0]), name=f[1], race=int(f[2]), class_=int(f[3]),
                              gender=int(f[4]), level=int(f[5]), rank=int(f[6]),
                              online=f[7] not in ("0", ""), map=int(f[8]), zone=int(f[9]),
                              spec_tab=-1))
        except ValueError:
            raise RosterError(f"roster line {n}: a numeric column is not a number") from None
    return out


def _quote(v: str) -> str:
    return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'


def query_roster(db: DbInfo, guild_id: int, mysql_exe: Path) -> list[Member]:
    tmp = tempfile.mkdtemp(prefix="leeroy-mysql-")
    try:
        cnf = Path(tmp) / "client.cnf"
        fd = os.open(cnf, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("[client]\n"
                     f"user={_quote(db.user)}\npassword={_quote(db.password)}\n"
                     f"host={_quote(db.host)}\nport={db.port}\n")
        try:
            r = subprocess.run(
                [str(mysql_exe), f"--defaults-extra-file={cnf}", "--batch",
                 "--skip-column-names", db.name, "-e", ROSTER_SQL.format(guild_id=guild_id)],
                capture_output=True, text=True, encoding="utf-8", timeout=60)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise RosterError(f"mysql did not run: {e.__class__.__name__}") from None
        if r.returncode != 0:
            raise RosterError(f"mysql failed ({r.returncode}): "
                              f"{(r.stderr.replace(db.password, '***') if db.password else r.stderr).strip()[:300]}")
        return parse_roster_tsv(r.stdout)
    finally:
        for p in Path(tmp).glob("*"):
            p.unlink(missing_ok=True)
        os.rmdir(tmp)


def _strict_bool(v) -> bool:
    if type(v) is bool or (type(v) is int and v in (0, 1)):
        return bool(v)
    raise ValueError(f"online must be true/false or 0/1, got {v!r}")


def members_from_json(text: str) -> list[Member]:
    """`roster --file` format: a JSON list of objects with the bridge's cast-member fields:
    guid, name, race, class, gender, level, rank, online, map, zone and optionally spec_tab
    (default -1). `class_` is accepted for `class`."""
    try:
        data = json.loads(text)
    except ValueError as e:
        raise RosterError(f"roster file is not valid JSON: {e}") from None
    if not isinstance(data, list):
        raise RosterError("roster file must be a JSON list of member objects")
    out = []
    for i, o in enumerate(data):
        if not isinstance(o, dict):
            raise RosterError(f"roster[{i}]: must be an object")
        try:
            out.append(Member(
                guid=int(o["guid"]), name=str(o["name"]), race=int(o["race"]),
                class_=int(o["class"] if "class" in o else o["class_"]),
                gender=int(o["gender"]), level=int(o["level"]), rank=int(o["rank"]),
                online=_strict_bool(o["online"]), map=int(o["map"]), zone=int(o["zone"]),
                spec_tab=int(o.get("spec_tab", -1))))
        except (KeyError, TypeError, ValueError) as e:
            raise RosterError(f"roster[{i}]: bad or missing field ({e.__class__.__name__}: "
                              f"{e})") from None
    return out


ROSTER_FILE_HELP = ("JSON list of objects with the bridge's cast-member fields: guid, name, "
                    "race, class, gender, level, rank, online, map, zone, optional spec_tab")


async def fetch_cast_from_bridge(host: str, port: int, timeout: float = 15.0) -> list[Member]:
    """Connect, wait for the greeting's cast, disconnect. Takes the bridge's single mind slot
    while it runs, so it is only for the owner's explicit use."""
    from ..bridge import BridgeClient
    got: asyncio.Future = asyncio.get_running_loop().create_future()

    async def on_event(ev) -> None:
        if isinstance(ev, Cast) and not got.done():
            got.set_result(list(ev.members))

    client = BridgeClient(host, port, on_event)
    task = asyncio.create_task(client.run())
    try:
        return await asyncio.wait_for(got, timeout)
    except asyncio.TimeoutError:
        raise RosterError("no cast arrived from the bridge") from None
    finally:
        await client.close()
        await asyncio.gather(task, return_exceptions=True)
