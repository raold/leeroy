"""What keeps a dojo reset from reaching anything but its own schemas: a separate server, its schema set and the
restricted user's exact grants, each checked against the running dojo instance (never a live one)."""

from __future__ import annotations

import secrets

from . import mysql
from .layout import Layout

SYSTEM_SCHEMAS = {"information_schema", "mysql", "performance_schema", "sys"}


def expected_grants(layout: Layout) -> set[str]:
    user = f"`{layout.db_user}`@`127.0.0.1`"
    grants = {f"GRANT USAGE ON *.* TO {user}"}
    for schema in layout.schemas.values():
        grants.add(f"GRANT ALL PRIVILEGES ON `{schema.replace('_', chr(92) + '_')}`.* TO {user}")
    return grants


def db_check(layout: Layout) -> dict:
    root = mysql.Client(layout, layout.server.root_cnf)
    dojo = mysql.Client(layout, layout.server.dojo_cnf)
    checks: list[dict] = []

    def add(ok: bool, check: str, detail: str) -> None:
        checks.append({"status": "ok" if ok else "FAIL", "check": check, "detail": detail})

    ident = mysql.verify_identity(layout, root)
    add(True, "instance", f"port {ident['port']}, datadir {ident['datadir']}, bind {ident['bind_address']}, "
                          f"server_uuid recorded")
    add(ident["log_bin"] == "0" and ident["performance_schema"] == "0", "instance options",
        f"log_bin {ident['log_bin']}, performance_schema {ident['performance_schema']}")
    add(ident["named_pipe"] == "0" and ident["shared_memory"] == "0", "instance transports",
        f"named_pipe {ident['named_pipe']}, shared_memory {ident['shared_memory']} (TCP on loopback only)")
    problems = mysql.check_my_ini(layout.server, layout.server.my_ini.read_text(encoding="utf-8"))
    add(not problems, "my.ini", "; ".join(problems) or "loopback port, own datadir, X plugin and pipes off")

    schemas = {r[0] for r in root.query("SHOW DATABASES;")} - SYSTEM_SCHEMAS
    want = set(layout.schemas.values())
    add(schemas == want, "schemas", f"{sorted(schemas)}" + ("" if schemas == want else f", expected {sorted(want)}"))
    live = sorted(s for s in schemas if s.lower() in layout.live_schemas or s.lower().startswith("acore_"))
    add(not live, "no live schema", "none" if not live else ", ".join(live))

    users = sorted(f"{u}@{h}" for u, h in root.query("SELECT user, host FROM mysql.user;")
                   if not u.startswith("mysql."))
    want_users = sorted(["root@localhost", "root@127.0.0.1", f"{layout.db_user}@127.0.0.1"])
    add(users == want_users, "users", ", ".join(users))
    grants = {r[0] for r in root.query(f"SHOW GRANTS FOR {mysql.sql_literal(layout.db_user)}@'127.0.0.1';")}
    add(grants == expected_grants(layout), f"{layout.db_user} grants",
        f"{len(grants)} grants: USAGE plus ALL on exactly the four dojo schemas (_ escaped)"
        if grants == expected_grants(layout) else f"unexpected: {sorted(grants ^ expected_grants(layout))}")

    visible = {r[0] for r in dojo.query("SHOW DATABASES;")} - {"information_schema", "performance_schema"}
    add(visible == want, f"{layout.db_user} sees", f"{sorted(visible)}")
    probe = "dojo_probe_" + secrets.token_hex(4)
    for label, sql in (("create a schema", f"CREATE DATABASE `{probe}`;"),
                       ("read mysql.user", "SELECT COUNT(*) FROM mysql.user;"),
                       ("grant itself more", f"GRANT ALL ON *.* TO {mysql.sql_literal(layout.db_user)}@'127.0.0.1';"),
                       ("drop another schema's table", "DROP TABLE IF EXISTS mysql.db;")):
        try:
            dojo.query(sql)
            add(False, f"{layout.db_user} cannot {label}", "the statement was allowed")
        except mysql.MysqlError as e:
            add("denied" in str(e).lower(), f"{layout.db_user} cannot {label}", str(e).split(": ", 2)[-1][:100])
    root.query(f"DROP DATABASE IF EXISTS `{probe}`;")
    return {"ok": all(c["status"] == "ok" for c in checks), "checks": checks}
