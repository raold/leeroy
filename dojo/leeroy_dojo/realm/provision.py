"""Provision a training realm: staged binaries, training configs, synthetic accounts and the realm row."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import string
import time
from pathlib import Path

from . import conf, mysql, srp6
from .layout import Layout, LayoutError, norm, under

RULES = "scripts/dojo/training-rules.json"
# E-cores 16-23 of the 14900KS (Windows lists P-core threads first): the training worker stays off the cores the
# owner's game runs on. A bitmask that also fits AzerothCore's int32 UseProcessors.
DEFAULT_AFFINITY = 0x00FF0000
ACCOUNT_RE = re.compile(r"^[A-Z0-9]{3,16}$")
RA_ACCOUNT = "DOJOADMIN"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def rules_path(layout: Layout) -> Path:
    return Path(layout.roots["source"]) / RULES


def staged_files(layout: Layout) -> dict[str, Path]:
    """What the instance's bin/ needs: the dojo build's worldserver and the DLLs it and libmysql load."""
    mysql_base = Path(layout.server.basedir)
    openssl = Path(layout.toolchain.get("openssl", "C:/Program Files/OpenSSL-Win64"))
    return {
        "worldserver.exe": layout.build_bin / "worldserver.exe",
        "worldserver.pdb": layout.build_bin / "worldserver.pdb",
        "libmysql.dll": mysql_base / "lib" / "libmysql.dll",
        "libcrypto-3-x64.dll": mysql_base / "bin" / "libcrypto-3-x64.dll",
        "libssl-3-x64.dll": mysql_base / "bin" / "libssl-3-x64.dll",
        "libcrypto-4-x64.dll": openssl / "bin" / "libcrypto-4-x64.dll",
        "libssl-4-x64.dll": openssl / "bin" / "libssl-4-x64.dll",
        "legacy.dll": openssl / "bin" / "legacy.dll",
    }


def stage_binaries(layout: Layout, log=print) -> dict:
    """Copies the build's worldserver and its DLLs into the instance's bin/ (never the live bin) with a receipt."""
    live_bin = norm(Path(layout.production["build"]) / "bin")
    layout.bin.mkdir(parents=True, exist_ok=True)
    receipt = {"kind": "leeroy-dojo-staged", "format": 1, "staged_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                                                      time.gmtime()), "files": {}}
    for name, source in staged_files(layout).items():
        if under(norm(source), live_bin):
            raise LayoutError(f"refusing to stage {name} from the live build {source}")
        if not source.is_file():
            raise LayoutError(f"missing {source}")
        target = layout.bin / name
        shutil.copy2(source, target)
        receipt["files"][name] = {"source": norm(source), "bytes": target.stat().st_size,
                                  "sha256": sha256_file(target)}
    (layout.bin / "staged.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    log(f"staged {len(receipt['files'])} files into {norm(layout.bin)}")
    return receipt


def db_info(layout: Layout, kind: str, password: str) -> str:
    return f"{layout.host};{layout.db_port};{layout.db_user};{password};{layout.schemas[kind]}"


def isolation_values(layout: Layout, manifest: tuple[Path, str] | None) -> dict[str, dict[str, str]]:
    dojo_pw = mysql.read_client_cnf(layout.server.dojo_cnf)["password"]
    world = {
        "LoginDatabaseInfo": db_info(layout, "auth", dojo_pw),
        "WorldDatabaseInfo": db_info(layout, "world", dojo_pw),
        "CharacterDatabaseInfo": db_info(layout, "characters", dojo_pw),
        "WorldServerPort": str(layout.ports["world"]),
        "Ra.Port": str(layout.ports["ra"]),
        "DataDir": norm(layout.assets),
        "LogsDir": norm(layout.logs),
        "TempDir": norm(layout.tmp),
        "SourceDirectory": layout.roots["core"],
        "MySQLExecutable": layout.server.client("mysql"),
        "PidFile": norm(layout.run_dir / "worldserver.pid"),
        "UseProcessors": str(DEFAULT_AFFINITY),
    }
    dojo = {"Dojo.Instance": layout.instance, "Dojo.Scenario.Manifest": "", "Dojo.Scenario.Sha256": ""}
    if manifest:
        dojo["Dojo.Scenario.Manifest"] = norm(manifest[0])
        dojo["Dojo.Scenario.Sha256"] = manifest[1]
    return {"worldserver.conf": world,
            "modules/playerbots.conf": {"PlayerbotsDatabaseInfo": db_info(layout, "playerbots", dojo_pw)},
            "modules/mod_dojo.conf": dojo}


def write_configs(layout: Layout, manifest: tuple[Path, str] | None = None, only: set[str] | None = None,
                  log=print) -> list[str]:
    """Writes the instance's training configs from the dojo build's .dist files and checks each against the rules."""
    if "world" not in layout.ports or "ra" not in layout.ports:
        raise LayoutError("the layout needs ports.world and ports.ra")
    rules = conf.load_rules(rules_path(layout))
    values = isolation_values(layout, manifest)
    dist_dir = layout.build_bin / "configs"
    written = []
    for name, file_rules in rules.items():
        if only is not None and name not in only:
            continue
        dist_path = dist_dir / file_rules.dist
        if not dist_path.is_file():
            raise LayoutError(f"no {dist_path}; build the dojo core first")
        dist_text = dist_path.read_text(encoding="utf-8")
        overrides = dict(file_rules.training)
        overrides.update(values[name])
        text = conf.render(dist_text, overrides)
        problems = conf.check(file_rules, conf.parse(text), conf.parse(dist_text))
        if problems:
            raise LayoutError(f"{name} would break the training rules: " + "; ".join(problems[:5]))
        target = layout.configs / name
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, target)
        written.append(norm(target))
    for d in (layout.logs, layout.tmp, layout.run_dir, layout.secrets):
        d.mkdir(parents=True, exist_ok=True)
    mysql.restrict_to_owner(layout.secrets)
    mysql.restrict_to_owner(layout.tmp)
    note = mysql.restrict_to_owner(layout.configs)
    log(f"wrote {len(written)} training configs into {norm(layout.configs)} ({note})")
    return written


def ra_credentials(layout: Layout) -> tuple[str, str]:
    path = layout.secrets / "ra.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["user"], data["password"]


def new_account_password() -> str:
    # AzerothCore upper-cases account passwords and caps them at 16 characters.
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(16))


def ensure_accounts(layout: Layout, fixture_account: str, log=print) -> dict[str, int]:
    """The RA admin (gm level 3 on all realms) and the fixture account, whose password is thrown away."""
    for name in (RA_ACCOUNT, fixture_account):
        if not ACCOUNT_RE.match(name):
            raise LayoutError(f"account name {name!r} must be 3-16 of A-Z 0-9")
    root = mysql.Client(layout, layout.server.root_cnf)
    mysql.verify_identity(layout, root)
    auth = layout.schemas["auth"]
    ids = {}
    ra_path = layout.secrets / "ra.json"
    for name, gm in ((RA_ACCOUNT, 3), (fixture_account, 0)):
        rows = root.query(f"SELECT id FROM account WHERE username = {mysql.sql_literal(name)};", database=auth)
        if rows:
            ids[name] = int(rows[0][0])
            if name == RA_ACCOUNT and not ra_path.is_file():
                raise mysql.MysqlError(f"{name} exists but {ra_path} is missing; recreate the auth schema")
            continue
        password = new_account_password()
        salt, verifier = srp6.make(name, password)
        root.query(f"INSERT INTO account (username, salt, verifier, expansion, email, reg_mail) VALUES "
                   f"({mysql.sql_literal(name)}, 0x{salt.hex()}, 0x{verifier.hex()}, 2, '', '');", database=auth)
        ids[name] = int(root.query(f"SELECT id FROM account WHERE username = {mysql.sql_literal(name)};",
                                   database=auth)[0][0])
        if gm:
            root.query(f"INSERT INTO account_access (id, gmlevel, RealmID, comment) VALUES "
                       f"({ids[name]}, {gm}, -1, 'dojo RA admin');", database=auth)
            ra_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = ra_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"user": name, "password": password}) + "\n", encoding="utf-8")
            os.replace(tmp, ra_path)
        log(f"created account {name} (id {ids[name]}){' with RA access' if gm else ', password discarded'}")
    return ids


def set_realm(layout: Layout, log=print) -> None:
    root = mysql.Client(layout, layout.server.root_cnf)
    mysql.verify_identity(layout, root)
    name = mysql.sql_literal(f"Dojo {layout.instance}")
    root.query(f"UPDATE realmlist SET name = {name}, address = '127.0.0.1', localAddress = '127.0.0.1', "
               f"port = {layout.ports['world']} WHERE id = 1;", database=layout.schemas["auth"])
    log(f"realm 1 is now {name[1:-1]} on 127.0.0.1:{layout.ports['world']}")
