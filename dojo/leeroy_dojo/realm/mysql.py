"""The dojo's own MySQL instance: a plain process on loopback with its own data folder, never a Windows service.

Credentials are generated here and written only to client option files under <server root>/secrets, which every
client program reads through --defaults-file, so no password appears on a command line or in output. Before any
statement runs, the client refuses a live port and, for anything that changes data, checks the server's datadir and
port (and, once recorded, its server_uuid) so a statement can only ever reach the dojo instance.
"""

from __future__ import annotations

import json
import os
import secrets
import socket
import string
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .layout import Layout, LayoutError, MysqlServer, norm, same

PASSWORD_ALPHABET = string.ascii_letters + string.digits
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
CREATE_BREAKAWAY_FROM_JOB = 0x01000000


class MysqlError(Exception):
    """A dojo MySQL operation failed or was refused. Messages never contain a credential."""


def new_password(length: int = 24) -> str:
    """Letters and digits only: safe inside a .conf DatabaseInfo string, an option file and SQL quotes."""
    return "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(length))


def sql_literal(text: str) -> str:
    if any(c in text for c in "'\\\x00\n\r"):
        raise MysqlError("refusing a literal with quote, backslash or control characters")
    return f"'{text}'"


def grant_db_pattern(schema: str) -> str:
    """A GRANT database name with its _ and % escaped, so `dojo_auth` cannot also match `dojoXauth`."""
    if not all(c.isalnum() or c == "_" for c in schema):
        raise MysqlError(f"refusing schema name {schema!r}")
    return "`" + schema.replace("_", "\\_") + "`"


def render_my_ini(server: MysqlServer) -> str:
    q = lambda p: '"' + norm(p) + '"'  # noqa: E731
    return "\n".join([
        "# leeroy dojo: the training realm's own MySQL instance. Written by python -m leeroy_dojo realm db-init.",
        "# A plain process started and stopped by that tool; never install it as a Windows service.",
        "[mysqld]",
        f"basedir={q(server.basedir)}",
        f"datadir={q(server.datadir)}",
        f"port={server.port}",
        "bind-address=127.0.0.1",
        "mysqlx=OFF",
        "named-pipe=OFF",
        "shared-memory=OFF",
        "skip-name-resolve=ON",
        f"log-error={q(server.logs / 'mysqld.err')}",
        f"pid-file={q(server.run / 'mysqld.pid')}",
        f"secure-file-priv={q(server.files)}",
        "local-infile=OFF",
        "disable-log-bin",
        "performance-schema=OFF",
        f"innodb-buffer-pool-size={server.buffer_pool}",
        "innodb-flush-log-at-trx-commit=2",
        "max-connections=64",
        "max-allowed-packet=256M",
        "character-set-server=utf8mb4",
        "collation-server=utf8mb4_general_ci",
        "default-password-lifetime=0",
        "",
    ])


def parse_my_ini(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    section = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().lower()
            continue
        if section != "mysqld":
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] == '"':
            value = value[1:-1]
        values[key.strip().lower().replace("_", "-")] = value
    return values


def check_my_ini(server: MysqlServer, text: str) -> list[str]:
    """Why a my.ini is not the dojo instance's (empty when it is)."""
    v = parse_my_ini(text)
    problems = []
    if v.get("port") != str(server.port):
        problems.append(f"port is {v.get('port')!r}, not {server.port}")
    if v.get("bind-address") not in ("127.0.0.1", "::1"):
        problems.append(f"bind-address is {v.get('bind-address')!r}, not loopback")
    if not v.get("datadir") or not same(norm(v["datadir"]), norm(server.datadir)):
        problems.append(f"datadir is {v.get('datadir')!r}, not {norm(server.datadir)}")
    if v.get("mysqlx", "").upper() not in ("OFF", "0"):
        problems.append("the X plugin is not off (it would bind 33060)")
    for key in ("named-pipe", "shared-memory"):
        if v.get(key, "").upper() not in ("OFF", "0"):
            problems.append(f"{key} is not off")
    return problems


def write_client_cnf(path: Path, user: str, password: str, port: int) -> None:
    text = "\n".join(["[client]", f"user={user}", f'password="{password}"', "host=127.0.0.1", f"port={port}",
                      "protocol=TCP", ""])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def read_client_cnf(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        key, sep, value = raw.partition("=")
        if sep:
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] == '"':
                value = value[1:-1]
            values[key.strip()] = value
    return values


def restrict_to_owner(path: Path) -> str:
    """Best effort: on Windows, drop inherited ACLs so only this user (and SYSTEM) can read path. Returns a note."""
    if os.name != "nt":
        os.chmod(path, 0o700)
        return "mode 700"
    user = os.environ.get("USERNAME")
    domain = os.environ.get("USERDOMAIN")
    if not user:
        return "USERNAME unset; ACL left as inherited"
    who = f"{domain}\\{user}" if domain else user
    result = subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"{who}:(OI)(CI)F",
                             "/grant:r", "SYSTEM:(OI)(CI)F"], capture_output=True, text=True)
    return "owner-only ACL" if result.returncode == 0 else f"icacls failed ({result.returncode}); ACL inherited"


def port_listening(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], capture_output=True,
                             text=True).stdout
        return f'"{pid}"' in out
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def spawn_detached(args: list[str], cwd: Path, stdout: Path | None, env: dict[str, str] | None = None) -> int:
    """Starts a hidden process that outlives this tool, at below-normal priority so the owner's game keeps the CPU."""
    out = open(stdout, "ab") if stdout else subprocess.DEVNULL
    try:
        flags = 0
        if os.name == "nt":
            flags = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP | BELOW_NORMAL_PRIORITY_CLASS
            try:
                proc = subprocess.Popen(args, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=out,
                                        stderr=subprocess.STDOUT, creationflags=flags | CREATE_BREAKAWAY_FROM_JOB,
                                        close_fds=True)
                return proc.pid
            except OSError:
                pass  # the job this tool runs in forbids breakaway; the child then lives as long as the job does
        proc = subprocess.Popen(args, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=out,
                                stderr=subprocess.STDOUT, creationflags=flags, close_fds=True)
        return proc.pid
    finally:
        if stdout:
            out.close()


@dataclass
class Client:
    """mysql.exe against the dojo instance with one option file. The port is checked before every call."""

    layout: Layout
    cnf: Path

    def _base(self, program: str) -> list[str]:
        if not self.cnf.is_file():
            raise MysqlError(f"no credentials at {self.cnf}; run realm db-init first")
        values = read_client_cnf(self.cnf)
        try:
            port = int(values.get("port", "0"))
        except ValueError:
            raise MysqlError(f"{self.cnf} has no usable port") from None
        try:
            self.layout.check_db_target(values.get("host", ""), port)
        except LayoutError as e:
            raise MysqlError(f"{self.cnf}: {e}") from None
        return [self.layout.server.client(program), f"--defaults-file={self.cnf}"]

    def query(self, sql: str, database: str | None = None, timeout: float = 120) -> list[list[str]]:
        args = self._base("mysql") + ["--batch", "--raw", "--skip-column-names", "--default-character-set=utf8mb4"]
        if database:
            if database not in self.layout.schemas.values():
                raise MysqlError(f"{database} is not a dojo schema")
            args.append(database)
        result = subprocess.run(args, input=sql, capture_output=True, text=True, encoding="utf-8",
                                timeout=timeout)
        if result.returncode != 0:
            raise MysqlError(f"mysql exited {result.returncode}: {result.stderr.strip()[:500]}")
        return [line.split("\t") for line in result.stdout.splitlines()]

    def admin(self, *command: str, timeout: float = 60) -> subprocess.CompletedProcess:
        args = self._base("mysqladmin") + list(command)
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def identity(client: Client) -> dict[str, str]:
    rows = client.query("SELECT @@port, @@datadir, @@server_uuid, @@bind_address, @@version, @@log_bin, "
                        "@@performance_schema, @@named_pipe, @@shared_memory;")
    port, datadir, uuid, bind, version, log_bin, perf, pipe, shm = rows[0]
    return {"port": port, "datadir": norm(datadir), "server_uuid": uuid, "bind_address": bind, "version": version,
            "log_bin": log_bin, "performance_schema": perf, "named_pipe": pipe, "shared_memory": shm}


def verify_identity(layout: Layout, client: Client) -> dict[str, str]:
    """The server answering on the dojo port is the dojo instance; raises otherwise."""
    ident = identity(client)
    server = layout.server
    problems = []
    if ident["port"] != str(server.port):
        problems.append(f"port {ident['port']}")
    if not same(ident["datadir"], norm(server.datadir)):
        problems.append(f"datadir {ident['datadir']}")
    if ident["bind_address"] not in ("127.0.0.1", "::1"):
        problems.append(f"bind_address {ident['bind_address']}")
    if server.receipt.is_file():
        recorded = json.loads(server.receipt.read_text(encoding="utf-8")).get("server_uuid")
        if recorded and recorded != ident["server_uuid"]:
            problems.append("server_uuid differs from the recorded dojo instance")
    if problems:
        raise MysqlError("the server on the dojo port is not the dojo instance: " + ", ".join(problems))
    return ident


def bootstrap_sql(layout: Layout, root_pw: str, dojo_pw: str) -> str:
    """Statements --init-file runs while initializing: passwords, the dojo schemas and the restricted user."""
    user = sql_literal(layout.db_user)
    lines = [
        f"ALTER USER 'root'@'localhost' IDENTIFIED BY {sql_literal(root_pw)};",
        f"CREATE USER 'root'@'127.0.0.1' IDENTIFIED BY {sql_literal(root_pw)};",
        "GRANT ALL PRIVILEGES ON *.* TO 'root'@'127.0.0.1' WITH GRANT OPTION;",
        f"CREATE USER {user}@'127.0.0.1' IDENTIFIED BY {sql_literal(dojo_pw)};",
    ]
    for kind in ("auth", "characters", "world", "playerbots"):
        schema = layout.schemas[kind]
        lines.append(f"CREATE DATABASE `{schema}` DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci;")
        lines.append(f"GRANT ALL PRIVILEGES ON {grant_db_pattern(schema)}.* TO {user}@'127.0.0.1';")
    return "\n".join(lines) + "\n"


def init(layout: Layout, log=print) -> None:
    """Creates my.ini, credentials and a fresh data folder. Refuses an existing data folder."""
    server = layout.server
    if not Path(server.mysqld).is_file():
        raise MysqlError(f"no mysqld at {server.mysqld}")
    if server.datadir.exists() and any(server.datadir.iterdir()):
        raise MysqlError(f"{server.datadir} already holds a data folder; db-init never reuses one")
    if port_listening(server.port):
        raise MysqlError(f"port {server.port} is already in use")
    for d in (server.logs, server.run, server.files, server.secrets):
        d.mkdir(parents=True, exist_ok=True)
    log(f"secrets: {restrict_to_owner(server.secrets)} on {norm(server.secrets)}")
    server.my_ini.write_text(render_my_ini(server), encoding="utf-8")
    problems = check_my_ini(server, server.my_ini.read_text(encoding="utf-8"))
    if problems:
        raise MysqlError("rendered my.ini fails its own check: " + "; ".join(problems))
    root_pw, dojo_pw = new_password(), new_password()
    write_client_cnf(server.root_cnf, "root", root_pw, server.port)
    write_client_cnf(server.dojo_cnf, layout.db_user, dojo_pw, server.port)
    init_file = server.secrets / "bootstrap.sql"
    init_file.write_text(bootstrap_sql(layout, root_pw, dojo_pw), encoding="utf-8")
    try:
        log(f"initializing {norm(server.datadir)} (this takes a minute)")
        result = subprocess.run([server.mysqld, f"--defaults-file={server.my_ini}", "--initialize-insecure",
                                 f"--init-file={init_file}"], capture_output=True, text=True, timeout=900,
                                creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0)
    finally:
        init_file.unlink(missing_ok=True)
    if result.returncode != 0:
        raise MysqlError(f"mysqld --initialize-insecure exited {result.returncode}; see {server.logs / 'mysqld.err'}")
    log("initialized")


def start(layout: Layout, timeout: float = 120, log=print) -> dict[str, str]:
    server = layout.server
    if not server.my_ini.is_file():
        raise MysqlError(f"no {server.my_ini}; run realm db-init first")
    problems = check_my_ini(server, server.my_ini.read_text(encoding="utf-8"))
    if problems:
        raise MysqlError(f"{server.my_ini} is not the dojo instance's: " + "; ".join(problems))
    client = Client(layout, server.root_cnf)
    if port_listening(server.port):
        ident = verify_identity(layout, client)
        log(f"already running on 127.0.0.1:{server.port}")
        return ident
    pid = spawn_detached([server.mysqld, f"--defaults-file={server.my_ini}"], cwd=Path(server.root), stdout=None)
    log(f"started mysqld pid {pid} on 127.0.0.1:{server.port}")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if port_listening(server.port):
            ping = client.admin("ping")
            if ping.returncode == 0:
                ident = verify_identity(layout, client)
                if not server.receipt.is_file():
                    receipt = {"kind": "leeroy-dojo-mysql", "format": 1, "server_uuid": ident["server_uuid"],
                               "port": server.port, "datadir": norm(server.datadir), "basedir": server.basedir,
                               "version": ident["version"], "recorded_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                                                         time.gmtime())}
                    server.receipt.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
                return ident
        if not pid_alive(pid):
            raise MysqlError(f"mysqld exited during start; see {server.logs / 'mysqld.err'}")
        time.sleep(1)
    raise MysqlError(f"mysqld did not answer within {timeout:.0f} s; see {server.logs / 'mysqld.err'}")


def server_pid(layout: Layout) -> int:
    pidfile = layout.server.run / "mysqld.pid"
    try:
        return int(pidfile.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return 0


def stop(layout: Layout, timeout: float = 120, log=print) -> None:
    server = layout.server
    if not port_listening(server.port):
        log("not running")
        return
    client = Client(layout, server.root_cnf)
    verify_identity(layout, client)
    pid = server_pid(layout)
    result = client.admin("shutdown")
    if result.returncode != 0:
        raise MysqlError(f"mysqladmin shutdown exited {result.returncode}: {result.stderr.strip()[:300]}")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not port_listening(server.port) and not pid_alive(pid):
            log("stopped")
            return
        time.sleep(1)
    raise MysqlError(f"mysqld (pid {pid}) still running {timeout:.0f} s after shutdown")


def status(layout: Layout) -> dict:
    server = layout.server
    info: dict = {"port": server.port, "root": server.root, "initialized": server.datadir.is_dir(),
                  "running": port_listening(server.port), "pid": server_pid(layout)}
    if info["running"]:
        info["identity"] = verify_identity(layout, Client(layout, server.root_cnf))
    return info
