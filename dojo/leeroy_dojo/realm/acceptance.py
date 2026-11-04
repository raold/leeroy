"""Task 5 acceptance: both fixture actors in the training arena with identical verified loadouts, then back out with
no progression side effect, the live server untouched and every database connection on the dojo instance.

The evidence is written under <data root>/runs/<stamp>-realm-acceptance/. It holds ids, hashes, counts and positions,
never a credential.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from pathlib import Path

from . import mysql, world
from .layout import Layout, norm

# Tables keyed by the fixture characters' guid, and the column naming it.
FIXTURE_TABLES = {
    "characters": "guid",
    "character_achievement": "guid",
    "character_achievement_progress": "guid",
    "character_achievement_offline_updates": "guid",
    "character_arena_stats": "guid",
    "arena_team_member": "guid",
    "battleground_deserters": "guid",
    "log_arena_memberstats": "guid",
    "pvpstats_players": "character_guid",
    "character_queststatus": "guid",
    "character_queststatus_rewarded": "guid",
    "character_queststatus_daily": "guid",
    "character_queststatus_weekly": "guid",
    "character_queststatus_monthly": "guid",
    "character_queststatus_seasonal": "guid",
    "character_reputation": "guid",
    "character_skills": "guid",
    "character_spell": "guid",
    "character_talent": "guid",
    "character_glyphs": "guid",
    "character_inventory": "guid",
    "item_instance": "owner_guid",
    "mail": "receiver",
    "character_spell_cooldown": "guid",
    "character_aura": "guid",
    "character_homebind": "guid",
    "character_social": "guid",
    "character_equipmentsets": "guid",
    "character_instance": "guid",
    "guild_member": "guid",
    "group_member": "memberGuid",
    # Bookkeeping a login, a teleport or a logout writes; reported, never a failure.
    "character_entry_point": "guid",
    "character_stats": "guid",
    "character_action": "guid",
    "character_account_data": "guid",
    "character_settings": "guid",
}
BOOKKEEPING_TABLES = {"character_entry_point", "character_stats", "character_action", "character_account_data",
                      "character_settings"}
# Columns of progression tables that change with time, place or the last session, not with progress.
BOOKKEEPING_COLUMNS = {
    "characters": {"position_x", "position_y", "position_z", "map", "instance_id", "instance_mode_mask", "orientation",
                   "online", "totaltime", "leveltime", "logout_time", "is_logout_resting", "rest_bonus", "zone",
                   "death_expire_time", "taxi_path", "latency", "health", "power1", "power2", "power3", "power4",
                   "power5", "power6", "power7", "extra_flags", "cinematic", "restState", "creation_date",
                   "order", "deleteInfos_Account", "deleteInfos_Name", "deleteDate"},
    "item_instance": {"playedTime"},
}
# Whole-realm counts that an arena outcome, a random-bot population or a public queue would change.
REALM_COUNTS = {
    "characters": "SELECT COUNT(*) FROM {characters}.characters",
    "arena_team": "SELECT COUNT(*) FROM {characters}.arena_team",
    "log_arena_fights": "SELECT COUNT(*) FROM {characters}.log_arena_fights",
    "pvpstats_battlegrounds": "SELECT COUNT(*) FROM {characters}.pvpstats_battlegrounds",
    "accounts": "SELECT COUNT(*) FROM {auth}.account",
    "random_bot_accounts": "SELECT COUNT(*) FROM {auth}.account WHERE username LIKE 'RNDBOT%'",
    "playerbots_random_bots": "SELECT COUNT(*) FROM {playerbots}.playerbots_random_bots",
    "playerbots_account_type": "SELECT COUNT(*) FROM {playerbots}.playerbots_account_type",
    "guilds": "SELECT COUNT(*) FROM {characters}.guild",
}


def fixture_guids(client: mysql.Client, layout: Layout, names: list[str]) -> list[int]:
    listed = ", ".join(mysql.sql_literal(n) for n in names)
    rows = client.query(f"SELECT guid FROM characters WHERE name IN ({listed}) ORDER BY guid;",
                        database=layout.schemas["characters"])
    return [int(r[0]) for r in rows]


def snapshot(client: mysql.Client, layout: Layout, guids: list[int]) -> dict:
    chars = layout.schemas["characters"]
    ids = ", ".join(str(g) for g in guids)
    tables: dict[str, dict] = {}
    for table, key in FIXTURE_TABLES.items():
        cols = [r[0] for r in client.query(
            f"SELECT column_name FROM information_schema.columns WHERE table_schema = {mysql.sql_literal(chars)} "
            f"AND table_name = {mysql.sql_literal(table)} ORDER BY ordinal_position;")]
        if not cols:
            continue
        quoted = ", ".join(f"`{c}`" for c in cols)
        rows = client.query(f"SELECT {quoted} FROM `{table}` WHERE `{key}` IN ({ids});", database=chars)
        tables[table] = {"columns": cols, "rows": sorted(rows)}
    names = {k: layout.schemas[k] for k in ("auth", "characters", "playerbots")}
    counts = {name: int(client.query(sql.format(**names) + ";")[0][0]) for name, sql in REALM_COUNTS.items()}
    return {"tables": tables, "counts": counts}


def _project(table: str, data: dict) -> list[tuple]:
    skip = BOOKKEEPING_COLUMNS.get(table, set())
    keep = [i for i, c in enumerate(data["columns"]) if c not in skip]
    return sorted(tuple(row[i] for i in keep) for row in data["rows"])


def compare(before: dict, after: dict) -> dict:
    """Progression differences (failures) and bookkeeping differences (reported) between two snapshots."""
    progression, bookkeeping = [], []
    for table in sorted(set(before["tables"]) | set(after["tables"])):
        b = before["tables"].get(table, {"columns": [], "rows": []})
        a = after["tables"].get(table, {"columns": [], "rows": []})
        if table in BOOKKEEPING_TABLES:
            if b["rows"] != a["rows"]:
                bookkeeping.append(table)
            continue
        if _project(table, b) != _project(table, a):
            gained = len(a["rows"]) - len(b["rows"])
            progression.append(f"{table} changed ({len(b['rows'])} -> {len(a['rows'])} rows, {gained:+d})")
        elif b["rows"] != a["rows"]:
            cols = [c for i, c in enumerate(b["columns"]) if any(rb[i] != ra[i] for rb, ra in zip(b["rows"], a["rows"]))]
            bookkeeping.append(f"{table}: {', '.join(cols)}")
    for name in sorted(before["counts"]):
        if before["counts"][name] != after["counts"].get(name):
            progression.append(f"realm count {name} {before['counts'][name]} -> {after['counts'].get(name)}")
    return {"progression": progression, "bookkeeping": bookkeeping, "ok": not progression}


def _sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def live_fingerprint(layout: Layout) -> dict:
    """What would show any touch of the live install: its config hashes, its Data folder's listing and its servers'
    process ids and listening ports. Read only; nothing here connects to a live port."""
    prod = layout.production
    configs = Path(prod["configs"])
    conf = {rel: _sha256(configs / rel) for rel in ("worldserver.conf", "authserver.conf", "modules/playerbots.conf",
                                                    "modules/mod_leeroy.conf")}
    data = Path(layout.assets)
    files, size, newest = 0, 0, 0.0
    for root, _, names in os.walk(data):
        for n in names:
            st = os.stat(os.path.join(root, n))
            files += 1
            size += st.st_size
            newest = max(newest, st.st_mtime)
    procs = {}
    if os.name == "nt":
        script = ("Get-CimInstance Win32_Process -Filter \"Name='worldserver.exe' OR Name='authserver.exe' OR "
                  "Name='mysqld.exe'\" | ForEach-Object { \"$($_.ProcessId)`t$($_.Name)`t$($_.ExecutablePath)\" }")
        out = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True,
                             text=True, timeout=60).stdout
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) == 3:
                procs[parts[0]] = {"name": parts[1], "path": norm(parts[2]) if parts[2] else ""}
        service = subprocess.run(["sc", "query", "MySQL84"], capture_output=True, text=True).stdout
        state = next((line.split(":", 1)[1].strip() for line in service.splitlines() if "STATE" in line), "unknown")
    else:
        state = "n/a"
    listening = {}
    if os.name == "nt":
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True).stdout
        for line in out.splitlines():
            parts = line.split()
            if len(parts) == 5 and parts[3] == "LISTENING":
                port = int(parts[1].rsplit(":", 1)[1])
                if port in layout.live_ports or port in (layout.db_port, layout.ports.get("world"), layout.ports.get("ra")):
                    listening.setdefault(str(port), sorted(set(listening.get(str(port), []) + [parts[4]])))
    return {"configs": conf, "data": {"files": files, "bytes": size, "newest_mtime": int(newest)},
            "processes": procs, "mysql84_service": state, "listening": listening}


def worker_peers(pid: int) -> list[str]:
    """The dojo worldserver's established TCP peers (it must only talk to the dojo MySQL instance)."""
    out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True).stdout
    peers = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 5 and parts[4] == str(pid) and parts[3] == "ESTABLISHED":
            peers.add(parts[2])
    return sorted(peers)


def wait_job(layout: Layout, timeout: float = 180) -> tuple[str, str]:
    """Polls dojo status until no job runs; returns (result line, evidence json text)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with world.ra(layout) as client:
            status = client.command("dojo status")
        lines = status.splitlines()
        job = next((line for line in lines if line.startswith("job: ")), "")
        if job == "job: none":
            result = next((line[len("result: "):] for line in lines if line.startswith("result: ")), "")
            evidence = next((line[len("dojo-evidence "):] for line in lines if line.startswith("dojo-evidence ")), "")
            return result, evidence
        time.sleep(2)
    raise world.WorldError(f"the dojo job did not finish within {timeout:.0f} s")


def run(layout: Layout, manifest: dict, stop_after: bool = True, log=print) -> dict:
    stamp = time.strftime("%Y%m%d_%H%M%S")
    out_dir = layout.data / "runs" / f"{stamp}-realm-acceptance"
    out_dir.mkdir(parents=True, exist_ok=True)
    report: dict = {"kind": "leeroy-dojo-realm-acceptance", "format": 1, "started": stamp, "instance": layout.instance,
                    "scenario": manifest["id"]}
    root = mysql.Client(layout, layout.server.root_cnf)
    report["mysql"] = mysql.verify_identity(layout, root)
    report["live_before"] = live_fingerprint(layout)
    started = False
    if not world.running(layout):
        world.start(layout, log=log)
        started = True
    pid = world.pid(layout)
    report["worker_pid"] = pid
    names = [a["name"] for a in manifest["actors"]]
    guids = fixture_guids(root, layout, names)
    if len(guids) != len(names):
        raise world.WorldError("the fixtures do not exist; run dojo fixture create and build first")
    before = snapshot(root, layout, guids)
    with world.ra(layout) as client:
        report["open_command"] = client.command("dojo arena open")
    report["open_result"], evidence = wait_job(layout)
    report["arena"] = json.loads(evidence) if evidence else None
    report["worker_peers_open"] = worker_peers(pid)
    with world.ra(layout) as client:
        report["close_command"] = client.command("dojo arena close")
    report["close_result"], _ = wait_job(layout)
    with world.ra(layout) as client:
        report["status_after"] = client.command("dojo status").splitlines()[:8]
    after = snapshot(root, layout, guids)
    report["side_effects"] = compare(before, after)
    if stop_after and started:
        world.stop(layout, log=log)
    report["live_after"] = live_fingerprint(layout)
    lb, la = report["live_before"], report["live_after"]
    report["live_untouched"] = (lb["configs"] == la["configs"] and lb["data"] == la["data"]
                                and {k: v for k, v in lb["processes"].items() if "dojo-runtime" not in v["path"]}
                                == {k: v for k, v in la["processes"].items() if "dojo-runtime" not in v["path"]}
                                and lb["mysql84_service"] == la["mysql84_service"])
    peers_ok = all(p.endswith(f":{layout.db_port}") and p.startswith("127.0.0.1:") for p in report["worker_peers_open"])
    arena = report["arena"] or {}
    report["ok"] = bool(arena.get("ok")) and report["open_result"].startswith("open ok") \
        and report["close_result"].startswith("close ok") and report["side_effects"]["ok"] \
        and report["live_untouched"] and peers_ok
    report["worker_peers_only_dojo_mysql"] = peers_ok
    (out_dir / "acceptance.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    (out_dir / "snapshot-before.json").write_text(json.dumps(before, indent=1) + "\n", encoding="utf-8")
    (out_dir / "snapshot-after.json").write_text(json.dumps(after, indent=1) + "\n", encoding="utf-8")
    report["out"] = norm(out_dir)
    return report
