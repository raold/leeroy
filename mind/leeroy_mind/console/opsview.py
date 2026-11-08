"""Read-only Ops sources. No actions, locks, migrations, model loads or bridge connections.

Each source has one daemon worker at most, a response deadline and a cache. A blocked disk,
share or process cannot serialize the other panels or create more workers on each poll.
SQLite reads also have a VM deadline. Only explicit scalar projections leave this module.

Budgets: health 6 s, Git and Task Scheduler 3 s each, other sources 1 s each, concurrently.
Health is cached for 60 s; others for 15 s. A timed-out worker remains single-flight until
it returns (Python cannot interrupt a blocked filesystem call); its late result can be read
on a later poll. Git/PowerShell children are killed on their own deadline. SQLite has a
50 ms busy timeout, query_only and a progress deadline. Lists and files have separate caps.

Persisted running nights may be paused, and guild raid rows do not store an encounter phase
or deadline. Missing live evidence stays unknown. Desk dates mean recorded operations, not
idle passes. Backup checksum/verification receipts are not a fresh payload or restore test.
"""
import asyncio
import hashlib
import json
import math
import os
import re
import sqlite3
import subprocess
import threading
import time
import tomllib
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from .. import health
from ..nightly.settings import NightlySettings
from . import mindlog, views

PANELS = ("health", "night", "realm", "raids", "selfies", "backups", "drift", "schedules")
BUDGETS = {"health": 6.0, "night": 1.0, "realm": 1.0, "raids": 1.0,
           "selfies": 1.0, "backups": 1.0, "drift": 3.0, "schedules": 3.0}
ROWS = 128
FILE_BYTES = 2 * 1024 * 1024
_CODE = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]{0,63}\Z")
_ID = re.compile(r"[0-9a-f]{32}\Z")
_DAY = re.compile(r"\d{4}-\d\d-\d\d\Z")
_HASH = re.compile(r"[0-9a-f]{40,64}\Z")
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class Missing(Exception):
    """A fixed, public explanation; never construct this from external error text."""


class Budget(Exception):
    pass


def number(value):
    return value if type(value) in (int, float) and math.isfinite(value) else None


def code(value, default="unknown"):
    return value if isinstance(value, str) and _CODE.fullmatch(value) else default


def ident(value):
    return value if isinstance(value, str) and _ID.fullmatch(value) else None


def object_json(value):
    if not isinstance(value, str) or len(value) > FILE_BYTES:
        return {}
    try:
        result = json.loads(value)
        return result if isinstance(result, dict) else {}
    except (ValueError, RecursionError):
        return {}


def members(value):
    if isinstance(value, str):
        try:
            value = json.loads(value) if len(value) <= 65536 else None
        except (ValueError, RecursionError):
            value = None
    return {x for x in value if type(x) is int and 0 < x <= 4294967295} if isinstance(value, list) else set()


def check(deadline):
    if time.monotonic() >= deadline:
        raise Budget()


def panel(status="ok", why="", **data):
    return {"status": status, "why": why, **data}


@contextmanager
def database(data, deadline):
    path = data / "mind.db"
    if not path.exists():
        raise Missing("mind.db has not been created.")
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.05)
    conn.row_factory = sqlite3.Row
    conn.set_progress_handler(lambda: time.monotonic() >= deadline, 1000)
    try:
        conn.execute("PRAGMA query_only=ON")
        yield conn
    finally:
        conn.close()


def require(conn, *tables):
    if not set(tables) <= views.tables(conn):
        raise Missing("this mind.db predates the panel's tables.")


def meta(conn, key):
    rows = views.rows(conn, "SELECT value FROM meta WHERE key=? AND length(value)<=?", (key, FILE_BYTES))
    return object_json(rows[0][0]) if rows else {}


def _read(path, limit=FILE_BYTES):
    # Never follow a symlink/junction into a different store or private file.
    if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
        raise OSError("linked source")
    with path.open("rb") as f:
        raw = f.read(limit + 1)
    if len(raw) > limit:
        raise Budget()
    return raw


def log_records(data, deadline):
    """A bounded tail, never returned verbatim. Absence is not proof that nothing happened."""
    path = data / "logs" / "mind.log"
    try:
        with mindlog._open_shared(path) as f:
            f.seek(0, 2)
            start = max(0, f.tell() - FILE_BYTES)
            f.seek(start)
            if start:
                f.readline(FILE_BYTES)
            raw = f.read(FILE_BYTES)
    except FileNotFoundError:
        return []
    out = []
    for line in raw.splitlines()[-12000:]:
        check(deadline)
        r = object_json(line.decode("utf-8", "replace"))
        try:
            ts = datetime.fromisoformat(r.get("ts", "")).timestamp()
        except (ValueError, TypeError, OverflowError, OSError):
            continue
        if number(ts) is not None:
            r["_time"] = ts
            out.append(r)
    return out


def stages(conn, night, failure_counts=None, errors=None, planned=None, complete=True):
    errors, planned = errors or {}, planned or {}
    unit = {r[0]: r[1] for r in views.rows(conn, "SELECT stage,count(*) FROM night_step "
            "WHERE night=? GROUP BY stage LIMIT 128", (night,))}
    out = []
    rows = [dict(r) for r in views.rows(conn, "SELECT stage,started,finished,calls,call_s FROM night_stage "
                              "WHERE night=? ORDER BY started,stage LIMIT 128", (night,))]
    recorded = {r["stage"] for r in rows}
    for name in sorted((set(unit) | set(failure_counts or {})) - recorded):
        rows.append({"stage": name, "started": None, "finished": None, "calls": None, "call_s": None})
    for r in rows[:ROWS]:
        out.append({"stage": code(r["stage"]), "started": number(r["started"]),
                    "finished": number(r["finished"]), "units": unit.get(r["stage"], 0),
                    "calls": number(r["calls"]), "seconds": number(r["call_s"]),
                    "planned_units": number(planned.get(r["stage"])),
                    "failures": number(failure_counts.get(r["stage"], 0 if complete else None)) if failure_counts is not None else None,
                    "error_code": errors.get(r["stage"])})
    return out


def night_view(conn, data, now, deadline, runtime):
    require(conn, "night_run", "night_stage", "night_step")
    r = conn.execute("SELECT night,status,started,finished,attempts,"
                     "CASE WHEN length(error)<=65536 THEN error WHEN error IS NULL THEN NULL ELSE 'unavailable' END AS error,"
                     "CASE WHEN length(plan)<=2097152 THEN plan ELSE '{}' END AS plan FROM night_run "
                     "ORDER BY night DESC LIMIT 1").fetchone()
    if r is None:
        raise Missing("no night has run yet.")
    errors = {}
    observed = None
    for event in log_records(data, deadline):
        if event.get("night") != r["night"] or event["_time"] < r["started"]:
            continue
        if event.get("event") == "nightly_unit_failed":
            value = event.get("error")
            if isinstance(value, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}(?:Error|Exception)", value):
                errors[code(event.get("stage"))] = value
        if event.get("event") in {"nightly_started", "nightly_paused", "nightly_done", "nightly_failed"}:
            observed = event["event"]
    state = code(r["status"])
    running = False if state in {"done", "failed", "abandoned", "interrupted"} else None
    if observed in {"nightly_paused", "nightly_done", "nightly_failed"}:
        running = False
    # The log has no heartbeat and a process restart can leave a running receipt behind.
    # A start marker alone cannot prove that its task is still running now.
    if runtime.get("mind") in {"stopped", "failed"}:
        running = False
    last = conn.execute("SELECT max(finished) FROM night_run WHERE status='done'").fetchone()[0]
    plan = object_json(r["plan"])
    stored = object_json(r["error"])
    failures = {k: n for k, n in stored.items() if code(k, None) is not None and type(n) is int and n >= 0}
    complete = len(failures) == len(stored)
    source = "stored"
    if not failures and r["error"] != "{}":
        # NULL on older nights does not prove zero failed units. Reuse health's bounded,
        # rotation-aware coverage check; incomplete observations remain lower bounds.
        logged, _, partial, seen, covered = health._night_log_counts(
            data, r["night"], r["started"], r["finished"] or now, mindlog._open_shared)
        failures, complete, source = dict(logged), seen and covered and not partial, "logs"
    fatal = re.match(r"([A-Za-z][A-Za-z0-9_]*(?:Error|Exception))\(", r["error"] or "")
    return panel(night=r["night"] if isinstance(r["night"], str) and _DAY.fullmatch(r["night"]) else None,
                 saved_state=state, started=number(r["started"]), finished=number(r["finished"]),
                 running=running, running_basis="Saved running rows can be paused. Without a current scheduler heartbeat, active work is unknown.",
                 stages=stages(conn, r["night"], failures, errors,
                               plan.get("units") if isinstance(plan.get("units"), dict) else {}, complete),
                 failures_complete=complete, failure_source=source,
                 error_code=fatal[1] if fatal else None,
                 budget={k: number(plan.get(k)) for k in ("budget_s", "estimate_s")},
                 next_after=last + NightlySettings.min_gap_hours * 3600 if number(last) is not None else None,
                 next_rule=f"At least {NightlySettings.min_gap_hours:g} hours after the last completed night, "
                    f"with the owner offline {NightlySettings.grace_minutes} minutes and no live lease "
                    "(owner, offline raid or voice). An unfinished recent night resumes first. A new night "
                    f"needs {NightlySettings.min_window_hours:g} hours of events; failed runs wait "
                    f"{NightlySettings.retry_minutes} minutes, at most {NightlySettings.max_attempts} attempts. "
                    "The time shown is a lower bound, not a scheduled start; current leases are not persisted.")


def realm_view(conn, now):
    require(conn, "realm_action")
    rows = conn.execute("SELECT command_id,kind,state,transport_state,"
                        "CASE WHEN length(reserved_manifest)<=65536 THEN reserved_manifest END AS reserved_manifest,"
                        "created,substr(evidence,1,65537) AS evidence,"
                        "result_reason FROM realm_action WHERE role='root' "
                        "AND state NOT IN ('completed','not_sent') ORDER BY created LIMIT 129").fetchall()
    if len(rows) > ROWS:
        raise Budget()
    held, out = set(), []
    for r in rows:
        raw_manifest = json.loads(r["reserved_manifest"])
        if not isinstance(raw_manifest, list) or any(type(g) is not int or not 0 < g <= 4294967295 for g in raw_manifest):
            raise ValueError("invalid manifest")
        guids = members(r["reserved_manifest"]) if r["transport_state"] != "not_entered" else set()
        if not guids and r["state"] not in {"unknown", "conflict"}:
            continue
        held.update(guids)
        evidence = object_json(r["evidence"])
        job = evidence.get("job") if isinstance(evidence.get("job"), dict) else {}
        ack = evidence.get("ack") if isinstance(evidence.get("ack"), dict) else {}
        command = ident(r["command_id"])
        out.append({"id": command, "kind": code(r["kind"]), "state": code(r["state"]),
                    "age_s": max(0, now - r["created"]) if number(r["created"]) is not None else None,
                    "members": len(guids), "native_reason": code(job.get("reason") or ack.get("error")),
                    "reason": code(r["result_reason"]),
                    "release_command": f"realm release {command} --reason reviewed_stale_hold" if command else None})
    direct_total = views.rows(conn, "SELECT count(*) FROM realm_direct WHERE state='unknown'")
    direct = [{"id": ident(r[0]), "kind": code(r[1]), "reason": code(r[2]),
               "at": r[3] / 1000 if number(r[3]) is not None else None} for r in views.rows(conn,
        "SELECT command_id,kind,reason,prepared_ms FROM realm_direct WHERE state='unknown' "
        "ORDER BY prepared_ms LIMIT 128")]
    desks = {}
    for owner in ("calendar", "mail"):
        r = views.rows(conn, "SELECT max(coalesce(settled_ms,dispatched_ms,prepared_ms)) "
                            "FROM realm_direct WHERE owner=?", (owner,))
        desks[owner] = r[0][0] / 1000 if r and number(r[0][0]) is not None else None
    return panel(roots=out, held_members=len(held), direct_unknowns=direct,
                 direct_unknown_total=direct_total[0][0] if direct_total else None, desks=desks,
                 desks_basis="Last recorded desk operation; idle passes have no durable timestamp.",
                 release_note="Inspect native state and stop the mind first. Copying text does nothing. "
                    "The CLI refuses ineligible roots, including conflicts and roots with children.",
                 direct_available="realm_direct" in views.tables(conn))


def raids_view(conn, now):
    require(conn, "guild_raid", "guild_attendance")
    out = {}
    for key, offline in (("guild", 0), ("offline", 1)):
        r = conn.execute("SELECT raid_id,raid_key,started,ended,kills,wipes,outcome FROM guild_raid "
                         "WHERE offline=? ORDER BY (ended IS NULL) DESC,started DESC LIMIT 1", (offline,)).fetchone()
        out[key] = None if r is None else {"id": r["raid_id"], "raid": code(r["raid_key"]),
            "started": number(r["started"]), "ended": number(r["ended"]),
            "phase": "open" if r["ended"] is None else "complete", "instance": None,
            "kills": number(r["kills"]), "wipes": number(r["wipes"]),
            "members": conn.execute("SELECT count(*) FROM guild_attendance WHERE raid_id=? "
                                    "AND status='raided'", (r["raid_id"],)).fetchone()[0],
            "deadline": None, "outcome": code(r["outcome"], "pending")}
    owned = meta(conn, "guild:offline:owned")
    if owned and (out["offline"] is None or owned.get("raid_id") == out["offline"]["id"]
                  or owned.get("phase") not in {"complete", "rejected"}):
        old = out["offline"] if out["offline"] and owned.get("raid_id") == out["offline"]["id"] else {}
        plan = owned.get("plan") if isinstance(owned.get("plan"), dict) else {}
        terminal = owned.get("terminal") if isinstance(owned.get("terminal"), dict) else {}
        out["offline"] = {**old, "command_id": ident(owned.get("id")), "id": number(owned.get("raid_id")),
            "raid": code(plan.get("raid_key")), "phase": code(owned.get("phase")),
            "instance": number(owned.get("instance")), "deadline": number(owned.get("deadline")),
            "members": len(members(owned.get("accepted"))), "started": number(owned.get("sent_at")),
            "outcome": old.get("outcome", code(terminal.get("reason"), "pending")),
            "kills": old.get("kills", number(terminal.get("kills"))),
            "wipes": old.get("wipes", number(terminal.get("wipes"))),
            "progress": None}
        # Native progress is journaled as a game event on builds that retain it. No chat rows.
        for r in views.rows(conn, "SELECT ts,payload FROM game_event WHERE type='job' "
                                 "ORDER BY id DESC LIMIT 128"):
            event = object_json(r["payload"])
            if (event.get("id") != owned.get("id") or event.get("kind") != "offline_raid"
                    or event.get("state") != "progress" or event.get("stage") != "running"
                    or number(event.get("last_progress_ts")) is None):
                continue
            out["offline"]["progress"] = {k: number(event.get(k)) for k in
                ("members_in_instance", "last_progress_ts", "kills", "wipes")}
            out["offline"]["progress"].update(observed=number(r["ts"]),
                boss_engaged=event.get("boss_engaged") if type(event.get("boss_engaged")) is bool else None,
                stale=owned.get("phase") in {"complete", "rejected"} or not
                    (number(r["ts"]) is not None and 0 <= now - r["ts"] <= 120))
            break
    return panel(**out, note="Saved raid state. Open does not prove a running encounter. "
                 "Progress is unavailable when no matching recent job was retained; old observations are marked stale.")


def selfies_view(conn, now):
    from ..selfies import snaps, store
    from ..selfies.cli import _stages
    from ..selfies.kind import chosen_style
    from ..selfies.prompt import renderable
    from ..personas.schema import persona_from_dict
    require(conn, "selfie_set", "selfie_current")
    queue = conn.execute("SELECT guid,set_no,requested FROM selfie_set WHERE status='queued' "
                         "ORDER BY requested LIMIT 129").fetchall()
    if len(queue) > ROWS:
        raise Budget()
    held = []
    def get_meta(key):
        rows = views.rows(conn, "SELECT value FROM meta WHERE key=?", (key,))
        return rows[0][0] if rows else None
    proxy = SimpleNamespace(conn=conn, get_meta=get_meta)
    for r in queue:
        rows = views.rows(conn, "SELECT json FROM persona_core WHERE guid=? AND length(json)<=?", (r[0], FILE_BYTES))
        try:
            persona = persona_from_dict(json.loads(rows[0][0])) if rows else None
            why = renderable(persona, chosen_style(proxy, persona))
        except (ValueError, TypeError, KeyError):
            why = "invalid_persona"
        if why:
            held.append({"guid": r[0], "set": r[1], "reason": {
                "no persona": "no_persona", "under age": "under_age", "no looks yet": "no_looks",
                "no photo kind yet": "no_kind"}.get(why, code(why, "held_kind"))})
    stage_rows = []
    for name, r in _stages(conn).items():
        stage_rows.append({"stage": code(name), **{k: number(r.get(k)) for k in
                           ("started", "finished", "units", "calls", "call_s")}})
    booth = meta(conn, "selfies:booth:last")
    return panel(counts=store.counts(conn), queue=[{"guid": r[0], "set": r[1], "requested": number(r[2])} for r in queue],
        held=held, stages=stage_rows,
        snaps=snaps.counts(conn, now=now) if "selfie_snap" in views.tables(conn) else None,
        booth={**{k: number(booth.get(k)) for k in ("started", "finished", "sets", "attempts", "seconds")},
               "outcome": code(booth.get("outcome"))} if booth else None)


def _entries(path, deadline):
    if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
        raise OSError("linked source")
    if not path.exists():
        return []
    out = []
    with os.scandir(path) as entries:
        for entry in entries:
            check(deadline)
            if len(out) >= 4096:
                raise Budget()
            p = Path(entry.path)
            if not entry.is_symlink() and not (hasattr(p, "is_junction") and p.is_junction()):
                out.append(p)
    return out


def feature_state(data, section):
    try:
        value = tomllib.loads(_read(data / "mind.toml", 65536).decode("utf-8-sig")).get(section, {})
    except FileNotFoundError:
        return "off"
    except (OSError, ValueError, Budget):
        return "unknown"
    if not isinstance(value, dict) or type(value.get("enabled", False)) is not bool:
        return "unknown"
    return "on" if value.get("enabled", False) else "off"


def backups_view(root, data, deadline, config=None):
    labels, total, count = {}, 0, 0
    for path in _entries(data / "snapshots", deadline):
        match = re.fullmatch(r"mind-([A-Za-z0-9_-]{1,231})-(\d{8}-\d{6})\.db", path.name)
        if match and path.is_file():
            info = path.stat()
            count += 1
            total += info.st_size
            # A numeric suffix may be part of the caller's label, or a same-second
            # collision counter. Filenames alone cannot distinguish the two.
            label = match[1]
            if label not in labels or match[2] > labels[label]["stamp"]:
                labels[label] = {"stamp": match[2], "at": info.st_mtime, "bytes": info.st_size}
    game = []
    for path in _entries(root / "db_backup", deadline):
        m = re.fullmatch(r"[A-Za-z0-9_-]{1,80}_(\d{8}_\d{6})", path.name)
        if m and path.is_dir():
            game.append({"name": path.name, "at": path.stat().st_mtime, "stamp": m[1]})
    off = panel("missing", "the off-machine backup tool has not been installed in this checkout.")
    if (root / "scripts" / "guild_backup.py").is_file():
        if config is None and not os.environ.get("LOCALAPPDATA"):
            off = panel("missing", "LOCALAPPDATA is unavailable, so the backup configuration cannot be located.")
        else:
            off = offmachine_view(config or Path(os.environ["LOCALAPPDATA"]) / "leeroy" / "backup.toml", deadline)
    return panel(snapshots={"count": count, "bytes": total, "labels": labels},
                 game=max(game, key=lambda r: r["stamp"]) if game else None, offmachine=off,
                 note="Snapshot labels come from filenames. Numeric suffixes are preserved: "
                      "they may be part of the label or a same-second copy counter.")


def offmachine_view(config, deadline):
    try:
        cfg = tomllib.loads(_read(config, 16384).decode("utf-8-sig"))
    except FileNotFoundError:
        return panel("missing", "no off-machine destination has been configured.")
    destination = cfg.get("destination")
    if not isinstance(destination, str) or not Path(destination).is_absolute():
        return panel("error", "the backup destination configuration is invalid.")
    store = Path(destination) / "leeroy-guild-backups-v1"
    marker = object_json(_read(store / ".guild-backup-store.json").decode("utf-8"))
    candidates = [p for p in _entries(store / "sets", deadline)
                  if re.fullmatch(r"guild-\d{8}T\d{12}Z-[0-9a-f]{8}", p.name) and p.is_dir()]
    if not candidates:
        return panel("missing", "no completed off-machine set has been published.")
    latest = max(candidates, key=lambda p: p.name)
    raw = _read(latest / "manifest.json")
    manifest = object_json(raw.decode("utf-8"))
    receipt = object_json(_read(latest / ".guild-backup-set.json").decode("utf-8"))
    valid = (marker.get("owner") == "leeroy-guild-backup" and ident(marker.get("store_id")) is not None
             and type(marker.get("format")) is int and marker["format"] == 1
             and all(r.get("store_id") == marker["store_id"] and r.get("owner") == marker["owner"]
                     and r.get("set_id") == latest.name and type(r.get("format")) is int
                     and r["format"] == 1 for r in (manifest, receipt))
             and receipt.get("manifest_sha256") == hashlib.sha256(raw).hexdigest())
    last_verify = None
    try:
        verification = object_json(_read(store / ".verify-state.json", 16384).decode("utf-8"))
    except FileNotFoundError:
        verification = {}
    if (verification.get("owner") == marker.get("owner")
            and verification.get("store_id") == marker.get("store_id")
            and type(verification.get("format")) is int and verification["format"] == 1):
        value = number(verification.get("last_full_verify"))
        if value is not None and 0 < value <= time.time() + 60:
            last_verify = value
    return panel("ok" if valid else "error", "" if valid else "the latest backup manifest could not be verified.",
                 name=latest.name, at=latest.stat().st_mtime, manifest_verified=valid,
                 last_full_verify=last_verify,
                 verification="The writer hashes new copies and checks SQLite; existing objects get a full read weekly "
                              "or with --verify-all. This panel checks the manifest checksum and the recorded full-verification "
                              "time, not current payload contents. A separate restore-test result is not stored by the backup tool.")


def command(argv, root, deadline, *, input=None):
    check(deadline)
    # Bound pipe storage while reading, not just after communicate() allocated an arbitrary
    # patch. stderr is intentionally discarded: Git/PowerShell errors can carry private paths.
    limit, output = 8 * FILE_BYTES, []
    with subprocess.Popen(argv, cwd=root, stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          creationflags=_CREATE_NO_WINDOW,
                          env={**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"}) as process:
        def pipes():
            try:
                # Only patch-id has input, and it streams no output until consuming the patch.
                if input is not None:
                    process.stdin.write(input)
                    process.stdin.close()
                output.append(process.stdout.read(limit + 1))
            except (OSError, ValueError):
                pass
        reader = threading.Thread(target=pipes, daemon=True, name="console-ops-pipe")
        reader.start()
        reader.join(max(.01, deadline-time.monotonic()))
        if reader.is_alive() or not output or len(output[0]) > limit:
            process.kill()
            process.wait(timeout=1)
            reader.join(1)
            raise Budget()
        try:
            returncode = process.wait(timeout=max(.01, deadline-time.monotonic()))
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1)
            raise Budget() from None
        if returncode:
            raise OSError("read command failed")
    return output[0].decode("utf-8", "replace")


def drift_view(root, deadline):
    if not (root / ".git").exists():
        raise Missing("this root is not a Git checkout.")
    def git(*args, input=None):
        return command(["git", "--no-pager", *args], root, deadline, input=input)
    branch = git("branch", "--show-current").strip() or "detached HEAD"
    head = git("rev-parse", "HEAD").strip()
    try:
        main = git("rev-parse", "refs/remotes/origin/main").strip()
    except OSError:
        raise Missing("origin/main is not cached; fetch it outside this read-only panel.") from None
    base = git("merge-base", "HEAD", "origin/main").strip()
    # Compare only divergent history. First-parent diffs make a merge cherry-picked with
    # -m 1 comparable too. Disable external/textconv drivers; nothing executes a repo filter.
    def patches(ref):
        raw = git("log", "--first-parent", "-m", "-p", "--binary", "--no-ext-diff", "--no-textconv",
                  "--format=%H", "-n", "200", f"{base}..{ref}")
        return {parts[1]: parts[0] for line in git("patch-id", "--stable", input=raw.encode()).splitlines()
                if len(parts := line.split()) == 2}
    live_patches, main_patches = patches("HEAD"), patches("origin/main")
    live_values = set(live_patches.values())
    titles = git("log", "--first-parent", "--format=%H%x00%s%x00%b%x1e", "-n", "200", f"{base}..origin/main")
    prs = []
    for record in titles.split("\x1e"):
        parts = record.strip().split("\x00", 2)
        if len(parts) != 3 or parts[0] not in main_patches or main_patches[parts[0]] in live_values:
            continue
        sha, title, body = parts
        match = re.search(r"\(#(\d+)\)\s*$", title) or re.match(r"Merge pull request #(\d+)\b", title)
        if match:
            if title.startswith("Merge pull request"):
                title = body.strip().splitlines()[0] if body.strip() else f"PR #{match[1]}"
            prs.append({"number": int(match[1]), "title": title[:200]})
    return panel(branch=branch[:100], head=head if _HASH.fullmatch(head) else None,
                 main=main if _HASH.fullmatch(main) else None, prs=prs,
                 note="Cached origin/main; never fetched here. Compares first-parent patches in at most 200 divergent commits "
                      "on each branch, including merge cherry-picks. Older or edited patches need operator review.")


SCHEDULE_PS = """$ErrorActionPreference='Stop';
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false);
$tasks=@(Get-ScheduledTask -TaskPath '\\' -ErrorAction Stop);
@('leeroy-console','leeroy-health','leeroy-guild-backup') | ForEach-Object {
  $name=$_; $t=$tasks | Where-Object TaskName -EQ $name;
  if ($null -eq $t) { [pscustomobject]@{name=$name;installed=$false} }
  else { $i=$t | Get-ScheduledTaskInfo; [pscustomobject]@{name=$name;installed=$true;
    state=[string]$t.State;last_run=$i.LastRunTime.ToUniversalTime().ToString('o');result=$i.LastTaskResult} }
} | ConvertTo-Json -Compress"""


def schedules_view(root, deadline):
    if os.name != "nt":
        raise Missing("Windows Task Scheduler is unavailable on this platform.")
    raw = json.loads(command(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", SCHEDULE_PS], root, deadline))
    return panel(tasks=[{"name": r["name"], "installed": r.get("installed") is True,
        "state": code(r.get("state")), "result": number(r.get("result")),
        "last_run": datetime.fromisoformat(r["last_run"]).timestamp() if r.get("last_run") else None}
        for r in raw if r.get("name") in {"leeroy-console", "leeroy-health", "leeroy-guild-backup"}])


def health_view(root, data, run_checks=health.run_checks):
    checks = run_checks(root, data)
    out = []
    # Config exceptions, service answers and model names can contain user-controlled text.
    # Keep the CLI's normal lines; replace those few unsafe diagnostics with fixed words.
    private = {"mind-toml", "health-config", "ollama", "ollama-models", "voice", "data-dir"}
    for c in checks:
        name, state = code(c.name), c.status if c.status in {"ok", "fail", "note"} else "note"
        text = c.text
        if (name in private or (name in {"endpoint", "mind"} and state == "fail")
                or name == "telemetry" and "not checked" in text):
            text = f"{name}: {health.MARK[state].strip()} (details available in the local health CLI)."
        for path, word in ((data, "<mind-data>"), (root, "<install>")):
            text = text.replace(str(path), word).replace(path.as_posix(), word)
        out.append({"name": name, "status": state, "text": text[:1600]})
    if len(out) == 1 and out[0]["name"] == "data-dir":
        return panel("missing", "health needs mind.toml, mind.db and seeded personas.", checks=[], fails=0)
    return panel(checks=out, fails=sum(c["status"] == "fail" for c in out),
                 note="The installed health checks run in-process, at most once a minute. "
                      "Private configuration and service diagnostics stay in the local CLI.")


class OpsView:
    def __init__(self, root, data, *, sources=None, budgets=None, clock=time.time, monotonic=time.monotonic):
        self.root, self.data = Path(root), Path(data)
        self.clock, self.monotonic = clock, monotonic
        self.budgets = budgets or BUDGETS
        self.sources = sources
        self._lock = threading.Lock()
        self._workers, self._cache, self._started = {}, {}, {}

    def _source(self, name, deadline, runtime):
        if self.sources is not None:
            return self.sources[name]()
        if name == "health":
            return health_view(self.root, self.data)
        if name in {"night", "realm", "raids", "selfies"}:
            with database(self.data, deadline) as conn:
                now = self.clock()
                if name == "night":
                    return night_view(conn, self.data, now, deadline, runtime)
                if name == "realm":
                    result = realm_view(conn, now)
                    state = feature_state(self.data, "realm")
                    result["enabled"] = state
                    if state == "off" and not (result["roots"] or result["direct_unknowns"] or any(result["desks"].values())):
                        result.update(status="missing", why="the Realm is off and has no retained operations.")
                    return result
                if name == "raids":
                    return raids_view(conn, now)
                result = selfies_view(conn, now)
                state = feature_state(self.data, "selfies")
                result["enabled"] = state
                if state == "off" and not (any(result["counts"].values()) or result["booth"] or result["stages"]
                                            or any((result["snaps"] or {}).values())):
                    result.update(status="missing", why="selfies are off and no retained photo work exists.")
                return result
        if name == "backups":
            return backups_view(self.root, self.data, deadline)
        if name == "drift":
            return drift_view(self.root, deadline)
        return schedules_view(self.root, deadline)

    def _work(self, name, deadline, runtime):
        try:
            result = self._source(name, deadline, runtime)
        except Missing as e:
            result = panel("missing", str(e))
        except (Budget, subprocess.TimeoutExpired):
            result = panel("error", "Skipped: this source exceeded its time or read budget.", skipped="budget")
        except sqlite3.OperationalError as e:
            result = (panel("error", "Skipped: this source exceeded its query time budget.", skipped="budget")
                      if str(e) == "interrupted" else panel("missing", "this mind.db predates the panel's columns.")
                      if str(e).startswith(("no such table", "no such column")) else
                      panel("error", "This database source could not be read."))
        except Exception:
            result = panel("error", "This source could not be read.")
        result.update(checked_at=self.clock(), budget_s=self.budgets[name])
        with self._lock:
            self._cache[name] = result
            self._workers.pop(name, None)

    async def _one(self, name, runtime):
        start = self.monotonic()
        with self._lock:
            ttl = 60 if name == "health" else 15
            if name in self._cache and start - self._started[name] < ttl:
                return self._cache[name]
            if name not in self._workers:
                self._started[name] = start
                self._cache.pop(name, None)
                worker = threading.Thread(target=self._work, args=(name, time.monotonic()+self.budgets[name], runtime),
                                          daemon=True, name=f"console-ops-{name}")
                self._workers[name] = worker
                worker.start()
        while self.monotonic() - start < self.budgets[name]:
            with self._lock:
                if name not in self._workers:
                    return self._cache[name]
            await asyncio.sleep(.01)
        return panel("error", "Skipped: this source is still busy beyond its time budget.", skipped="budget",
                     budget_s=self.budgets[name], checked_at=self.clock())

    async def snapshot(self, runtime=None):
        results = await asyncio.gather(*(self._one(name, runtime or {}) for name in PANELS))
        panels = dict(zip(PANELS, results))
        return {"panels": panels, "at": self.clock(), "counts": {
            "fails": panels["health"].get("fails") if panels["health"]["status"] == "ok" else None,
            "held_members": panels["realm"].get("held_members") if panels["realm"]["status"] == "ok" else None}}
