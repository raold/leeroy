"""The dojo layout file, the live endpoints it must stay away from, and the paths of one training realm."""

from __future__ import annotations

import ipaddress
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

MARKER = "leeroy-dojo-training"
INSTANCE_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
SCHEMA_RE = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
SCHEMA_KINDS = ("auth", "characters", "world", "playerbots")
# AzerothCore's compiled-in ports, which a live server binds when its .conf leaves a key out.
DEFAULT_LIVE_PORTS = {3306: "MySQL default", 33060: "MySQL X default", 3724: "authserver default",
                      8085: "worldserver default", 3443: "RA default", 7878: "SOAP default"}
# Keys whose value is host;port;user;password;schema. The password is dropped as soon as a line is split.
DB_INFO_KEYS = ("LoginDatabaseInfo", "WorldDatabaseInfo", "CharacterDatabaseInfo", "PlayerbotsDatabaseInfo",
                "Playerbots.DatabaseInfo", "AiPlayerbot.DatabaseInfo")


class LayoutError(Exception):
    """The layout cannot be used: a missing key, a bad path or something that points at the live install."""


def norm(path: str | os.PathLike) -> str:
    """An absolute path with forward slashes and no trailing slash, compared case-insensitively on Windows."""
    text = os.path.abspath(os.fspath(path)).replace("\\", "/")
    if len(text) > 3 and text.endswith("/"):
        text = text.rstrip("/")
    return text


def same(a: str, b: str) -> bool:
    return (a.lower() == b.lower()) if os.name == "nt" else a == b


def under(child: str, parent: str) -> bool:
    child, parent = norm(child), norm(parent)
    if same(child, parent):
        return True
    prefix = parent if parent.endswith("/") else parent + "/"
    return child.lower().startswith(prefix.lower()) if os.name == "nt" else child.startswith(prefix)


def is_loopback(host: str) -> bool:
    h = host.strip().strip("[]")
    if h == "localhost":
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def read_conf(path: Path) -> dict[str, str]:
    """AzerothCore .conf: Key = "value" lines; quotes stripped, later keys win."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = re.match(r"^\s*([A-Za-z0-9_.]+)\s*=\s*(.*?)\s*$", line)
        if not m:
            continue
        value = m.group(2)
        if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
            value = value[1:-1]
        values[m.group(1)] = value
    return values


def db_endpoint(value: str) -> tuple[str, int, str] | None:
    """host;port;user;password;schema -> (host, port, schema), never the password; None when not TCP."""
    parts = value.split(";")
    if len(parts) < 5:
        return None
    try:
        port = int(parts[1])
    except ValueError:
        return None
    if not 1 <= port <= 65535 or not parts[0].strip() or not parts[4].strip():
        return None
    return parts[0].strip(), port, parts[4].strip()


@dataclass(frozen=True)
class MysqlServer:
    """The dojo's own MySQL instance: binaries from an installed server, everything else under root."""

    basedir: str
    root: str
    port: int
    buffer_pool: str = "256M"

    @property
    def mysqld(self) -> str:
        return f"{self.basedir}/bin/mysqld.exe" if os.name == "nt" else f"{self.basedir}/bin/mysqld"

    def client(self, name: str) -> str:
        return f"{self.basedir}/bin/{name}.exe" if os.name == "nt" else f"{self.basedir}/bin/{name}"

    @property
    def my_ini(self) -> Path:
        return Path(self.root) / "my.ini"

    @property
    def datadir(self) -> Path:
        return Path(self.root) / "data"

    @property
    def secrets(self) -> Path:
        return Path(self.root) / "secrets"

    @property
    def root_cnf(self) -> Path:
        return self.secrets / "root.cnf"

    @property
    def dojo_cnf(self) -> Path:
        return self.secrets / "dojo.cnf"

    @property
    def logs(self) -> Path:
        return Path(self.root) / "logs"

    @property
    def run(self) -> Path:
        return Path(self.root) / "run"

    @property
    def files(self) -> Path:
        return Path(self.root) / "files"

    @property
    def receipt(self) -> Path:
        return Path(self.root) / "dojo-mysql.json"


@dataclass(frozen=True)
class Layout:
    path: str
    instance: str
    roots: dict[str, str]
    host: str
    db_port: int
    db_user: str
    schemas: dict[str, str]
    server: MysqlServer
    ports: dict[str, int]
    assets: str
    toolchain: dict[str, str]
    production: dict
    live_ports: dict[int, str] = field(default_factory=dict)
    live_schemas: frozenset[str] = frozenset()
    protected: tuple[str, ...] = ()

    @property
    def runtime(self) -> Path:
        return Path(self.roots["runtime"])

    @property
    def data(self) -> Path:
        return Path(self.roots["data"])

    @property
    def configs(self) -> Path:
        return self.runtime / "configs"

    @property
    def bin(self) -> Path:
        return self.runtime / "bin"

    @property
    def logs(self) -> Path:
        return self.runtime / "logs"

    @property
    def tmp(self) -> Path:
        return self.runtime / "tmp"

    @property
    def run_dir(self) -> Path:
        return self.runtime / "run"

    @property
    def secrets(self) -> Path:
        return self.runtime / "secrets"

    @property
    def build_bin(self) -> Path:
        return Path(self.roots["build"]) / "bin" / "RelWithDebInfo"

    def check_port(self, port: int, what: str) -> None:
        """Refuses a live or reserved port before anything connects to or binds it."""
        if port in self.live_ports:
            raise LayoutError(f"{what} port {port} is a live port ({self.live_ports[port]})")

    def check_db_target(self, host: str, port: int, schema: str | None = None) -> None:
        if not is_loopback(host):
            raise LayoutError(f"database host {host} is not loopback")
        self.check_port(port, "database")
        if port != self.db_port:
            raise LayoutError(f"database port {port} is not the dojo instance's port {self.db_port}")
        if schema is not None and schema not in self.schemas.values():
            raise LayoutError(f"schema {schema} is not one of the dojo's schemas")


def _abs(value, what: str) -> str:
    if not isinstance(value, str) or not value or not os.path.isabs(value):
        raise LayoutError(f"{what} must be an absolute path, not {value!r}")
    return norm(value)


def _port(value, what: str, allow_zero: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not (0 if allow_zero else 1) <= value <= 65535:
        raise LayoutError(f"{what} must be a port number, not {value!r}")
    return value


def live_endpoints(production: dict) -> tuple[dict[int, str], frozenset[str]]:
    """Every port the live install uses or would use, and its schemas, from the layout and the live configs."""
    ports: dict[int, str] = dict(DEFAULT_LIVE_PORTS)
    schemas: set[str] = set()
    for p in production.get("ports", []):
        if isinstance(p, int) and not isinstance(p, bool):
            ports[p] = "production"
    for d in production.get("databases", []):
        if isinstance(d, dict):
            if isinstance(d.get("port"), int):
                ports[d["port"]] = "live database"
            if isinstance(d.get("schema"), str):
                schemas.add(d["schema"].lower())
    configs = production.get("configs")
    if isinstance(configs, str) and os.path.isabs(configs):
        for rel in ("worldserver.conf", "authserver.conf", "modules/playerbots.conf"):
            conf = read_conf(Path(configs) / rel)
            for key in DB_INFO_KEYS:
                if key in conf:
                    endpoint = db_endpoint(conf[key])
                    if endpoint:
                        ports[endpoint[1]] = "live database"
                        schemas.add(endpoint[2].lower())
            for key in ("WorldServerPort", "RealmServerPort", "Ra.Port", "SOAP.Port"):
                if conf.get(key, "").isdigit():
                    ports[int(conf[key])] = f"live {key}"
    return ports, frozenset(schemas)


def load(path: str | os.PathLike) -> Layout:
    path = norm(path)
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise LayoutError(f"cannot read layout {path}: {e}") from e
    if not isinstance(data, dict):
        raise LayoutError(f"{path} is not a JSON object")
    if data.get("marker") != MARKER:
        raise LayoutError(f"layout marker must be {MARKER!r}")
    instance = data.get("instance")
    if not isinstance(instance, str) or not INSTANCE_RE.match(instance):
        raise LayoutError(f"layout instance {instance!r} is not a valid name")
    roots_in = data.get("roots") or {}
    roots = {name: _abs(roots_in.get(name), f"roots.{name}")
             for name in ("source", "core", "playerbots", "build", "runtime", "data")}
    production = data.get("production") or {}
    if not isinstance(production, dict) or not production.get("install_root"):
        raise LayoutError("layout production section must name the live install")
    live_ports, live_schemas = live_endpoints(production)
    protected = tuple(_abs(p, "production.protected entry") for p in production.get("protected", []))
    live_areas = [norm(production[k]) for k in ("core", "build", "configs", "modules") if production.get(k)]

    db = data.get("database") or {}
    host = db.get("host")
    if not isinstance(host, str) or not is_loopback(host):
        raise LayoutError(f"database.host {host!r} must be a loopback address")
    db_port = _port(db.get("port"), "database.port")
    if db_port in live_ports:
        raise LayoutError(f"database.port {db_port} is a live port ({live_ports[db_port]}); the dojo has its own "
                          "MySQL instance")
    user = db.get("user")
    if not isinstance(user, str) or not re.match(r"^[A-Za-z][A-Za-z0-9_]{0,31}$", user) or user.lower() == "root":
        raise LayoutError(f"database.user {user!r} must name a restricted user, not root")
    schemas_in = db.get("schemas") or {}
    schemas = {}
    for kind in SCHEMA_KINDS:
        name = schemas_in.get(kind)
        if not isinstance(name, str) or not SCHEMA_RE.match(name):
            raise LayoutError(f"database.schemas.{kind} {name!r} is not a plain lower-case schema name")
        if name.lower() in live_schemas or name.lower().startswith("acore_"):
            raise LayoutError(f"database.schemas.{kind} {name} is a live schema name")
        schemas[kind] = name
    if len(set(schemas.values())) != len(schemas):
        raise LayoutError("database.schemas must be four different schemas")
    srv = db.get("server") or {}
    server = MysqlServer(basedir=_abs(srv.get("basedir"), "database.server.basedir"),
                         root=_abs(srv.get("root"), "database.server.root"),
                         port=db_port, buffer_pool=str(srv.get("buffer_pool", "256M")))
    if not re.match(r"^[0-9]{2,5}M$", server.buffer_pool):
        raise LayoutError(f"database.server.buffer_pool {server.buffer_pool!r} must be like 256M")
    for area in [norm(production["install_root"])] + live_areas + list(protected):
        if under(area, server.root):
            raise LayoutError(f"database.server.root {server.root} contains {area}")
    for area in live_areas + list(protected):
        if under(server.root, area):
            raise LayoutError(f"database.server.root {server.root} is inside {area}")
    if under(server.root, roots["source"]):
        raise LayoutError("database.server.root is inside the source checkout")

    ports_in = data.get("ports") or {}
    ports: dict[str, int] = {}
    for name, value in ports_in.items():
        if name == "dojo_host":
            continue
        ports[name] = _port(value, f"ports.{name}", allow_zero=(name == "dojo"))
        if ports[name] and ports[name] in live_ports:
            raise LayoutError(f"ports.{name} {ports[name]} is a live port ({live_ports[ports[name]]})")
    used = [p for p in list(ports.values()) + [db_port] if p]
    if len(used) != len(set(used)):
        raise LayoutError("the layout's ports must all differ")
    toolchain = {k: norm(v) for k, v in (data.get("toolchain") or {}).items() if isinstance(v, str)}
    return Layout(path=path, instance=instance, roots=roots, host=host, db_port=db_port, db_user=user,
                  schemas=schemas, server=server, ports=ports, assets=_abs(data.get("assets"), "assets"),
                  toolchain=toolchain, production=production, live_ports=live_ports, live_schemas=live_schemas,
                  protected=protected)
