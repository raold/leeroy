"""Read-only raid evidence, scoped by stored raid/pull identity; no model or bridge calls."""
from collections import Counter, defaultdict
import json
from pathlib import Path
import re
import time

from leeroy_mind.guild.raids import CATALOG
from leeroy_mind.raidplay import playbook_for
from leeroy_mind.wow import BOSS_NAMES, boss_name, is_raid_fight
from .debrief_logs import CODE, IDENT, read_logs
from .hurt import EVIDENCE
from .mechanics import boss_mechanics

MAX_PULLS = 256
MAX_ROWS = 10000
MAX_EVENTS = 256
MAX_HITS = 20000
POST_RAID_S = 3600  # bound terminal logging may follow the persisted session close
CHECK_NAMES = frozenset(name for rows in boss_mechanics(True).values() for row in rows for name in row.names)


def code(value, default="unknown"):
    return value if isinstance(value, str) and re.fullmatch(CODE, value) else default


def coverage_code(value):
    if isinstance(value, str):
        prefix, _, name = value.rpartition(":")
        if name in CHECK_NAMES and code(prefix) != "unknown":
            return value
    return code(value)


def obj(value, default=None):
    try:
        return json.loads(value) if isinstance(value, str) and len(value) <= 65536 else default
    except (ValueError, RecursionError):
        return default


def meta(conn, key):
    row = conn.execute("SELECT CASE WHEN length(value)<=65536 THEN value END FROM meta WHERE key=?", (key,)).fetchone()
    return obj(row[0], {}) if row else {}


def _rows(conn, sql, params, issues, label, limit=MAX_ROWS):
    rows = conn.execute(sql + " LIMIT ?", (*params, limit + 1)).fetchall()
    if len(rows) > limit:
        issues.add(label + "_truncated")
    return rows[:limit]


def _commands(conn, raid, end, issues):
    """Only an explicit saved raid->command binding, never map/time inference for jobs."""
    commands = set()
    owned = meta(conn, "guild:offline:owned")
    if isinstance(owned, dict) and owned.get("raid_id") == raid["raid_id"]:
        if isinstance(owned.get("id"), str):
            commands.add(owned["id"])
    # This event is emitted locally by OfflineRaids._memories, not by Discord ingestion.
    rows = _rows(conn, "SELECT CASE WHEN length(payload)<=65536 THEN payload END AS payload FROM game_event "
                 "WHERE type='guild_offline_raid' AND ts>=? AND ts<=? ORDER BY id", (raid["started"], end + POST_RAID_S), issues, "bindings", 256)
    for row in rows:
        value = obj(row["payload"], {})
        if isinstance(value, dict) and value.get("raid_id") == raid["raid_id"]:
            if isinstance(value.get("command"), str):
                commands.add(value["command"])
    return {c for c in commands if isinstance(c, str) and re.fullmatch(IDENT, c)}


def _summaries(conn, pulls, issues):
    """Per-member observed spell hits and checker failures, not inferred standing in fire."""
    owner = meta(conn, "human_guid")
    by_pull = {p["id"]: p for p in pulls}
    hits, failures, deaths = defaultdict(Counter), defaultdict(Counter), defaultdict(Counter)
    if not by_pull:
        return hits, failures, deaths
    marks = ",".join("?" * len(by_pull))
    keys = tuple(by_pull)
    seen_hits = 0
    for row in _rows(conn, f"SELECT pull,guid,CASE WHEN length(detail)<=65536 THEN detail END AS detail "
                     f"FROM tg_member WHERE pull IN ({marks}) ORDER BY pull,guid", keys, issues, "members"):
        if row["guid"] == owner:
            continue
        value = obj(row["detail"], {})
        taken = value.get("taken_by", []) if isinstance(value, dict) else []
        if row["detail"] is None or not isinstance(taken, list):
            issues.add("member_detail_unavailable")
            continue
        if len(taken) > 256:
            issues.add("member_spells_truncated")
        for item in taken[:256]:
            if not isinstance(item, dict) or not all(type(item.get(k)) is int and 0 <= item[k] < 2**63 for k in ("spell", "amount", "hits")):
                continue
            if seen_hits >= MAX_HITS:
                issues.add("member_hits_truncated")
                break
            seen_hits += 1
            key = (row["guid"], item["spell"])
            hits[row["pull"]][key + ("hits",)] += item["hits"]
            hits[row["pull"]][key + ("requested",)] += item["amount"]
    for row in _rows(conn, f"SELECT pull,guid,kind,key,count FROM tg_failure WHERE pull IN ({marks}) AND count>0 "
                     "ORDER BY id", keys, issues, "failures"):
        if row["guid"] != owner:
            # Checker keys can be spell names; don't expose arbitrary saved text through Ops.
            key = row["key"] if row["key"] in CHECK_NAMES else code(row["key"], "unnamed_check")
            failures[row["pull"]][row["guid"], code(row["kind"]), key] += row["count"]
    for row in _rows(conn, f"SELECT pull,guid,spell FROM tg_death WHERE pull IN ({marks}) ORDER BY id", keys, issues, "deaths"):
        if row["guid"] != owner:
            deaths[row["pull"]][row["guid"], row["spell"]] += 1
    return hits, failures, deaths


def _rez(events):
    grouped = {}
    for event in events:
        key = (event["reason"], event.get("fence", "none"))
        item = grouped.setdefault(key, dict(reason=key[0], fence=key[1], records=0,
                                           max_dead=0, max_casters=0, first=event["at"], last=event["at"]))
        item["records"] += 1
        item["max_dead"] = max(item["max_dead"], event["dead"])
        item["max_casters"] = max(item["max_casters"], event["casters"])
        item["last"] = max(item["last"], event["at"])
    return sorted(grouped.values(), key=lambda r: (r["reason"], r["fence"]))


def raid_debrief(conn, *, data: Path, native_dir: Path, raid_id=None, now=None, deadline=None, tz=None):
    """Latest completed raid by default (or current raid if none completed). No writes.

    Callers supply a read-only SQLite connection and enforce its query deadline. Logs get
    a separate bounded read below. Historical raid IDs never fall back to another raid.
    """
    now = time.time() if now is None else now
    deadline = time.monotonic() + 4 if deadline is None else deadline
    if raid_id is not None and (type(raid_id) is not int or raid_id <= 0):
        raise ValueError("invalid raid id")
    raid = conn.execute("SELECT * FROM guild_raid WHERE raid_id=?", (raid_id,)).fetchone() if raid_id is not None else conn.execute(
        "SELECT * FROM guild_raid ORDER BY (ended IS NOT NULL) DESC,started DESC,raid_id DESC LIMIT 1").fetchone()
    result = dict(raid=None, bosses=[], offline=[], group_rez=[], hazards=[], unassigned_responses=[],
                  issues=[], evidence={**EVIDENCE,
                      "responder": "Completed responder activations. Moved counts are bots issued movement per activation, not arrivals or distinct bots across activations.",
                      "group_rez": "Rate-limited observations. Native group-rez lines lack raid, boss and member IDs; every observed reason remains unassigned.",
                      "hazards": "Hazard logs lack map, boss or instance IDs. These are unassigned observations within the raid window, not proof of hits or successful avoidance.",
                      "clock": "Native logs have whole-second host-local timestamps; an ambiguous boundary is never assigned to a boss."})
    if raid is None:
        result["issues"] = ["raid_not_found" if raid_id is not None else "no_recorded_raid"]
        return result
    end = raid["ended"] if raid["ended"] is not None else now
    if end < raid["started"]:
        raise ValueError("invalid raid interval")
    catalog = CATALOG.get(raid["raid_key"])
    result["raid"] = dict(id=raid["raid_id"], name=catalog.name if catalog else "Unknown raid", key=code(raid["raid_key"]),
                          started=raid["started"], ended=raid["ended"], offline=bool(raid["offline"]),
                          outcome=code(raid["outcome"], "pending" if raid["ended"] is None else "unstructured_outcome"),
                          kills=raid["kills"], wipes=raid["wipes"])
    issues = set()
    pulls = _rows(conn, "SELECT id,map,instance_id,boss_id,difficulty,outcome,start_ms,end_ms,complete,checks_complete,"
                  "CASE WHEN length(checks_coverage)<=65536 THEN checks_coverage END AS checks_coverage "
                  "FROM tg_pull WHERE raid_id=? ORDER BY start_ms,id", (raid["raid_id"],), issues, "pulls", MAX_PULLS)
    hits, failures, deaths = _summaries(conn, pulls, issues)
    other = _rows(conn, "SELECT map,instance_id,start_ms,end_ms FROM tg_pull WHERE (raid_id IS NULL OR raid_id<>?) "
                  "AND start_ms<=? AND end_ms>=? ORDER BY start_ms", (raid["raid_id"], int(end*1000), int(raid["started"]*1000)),
                  issues, "overlapping_pulls", MAX_PULLS)
    commands = _commands(conn, raid, end, issues)
    if raid["offline"] and not commands:
        issues.add("native_job_binding_unavailable")
    logs = read_logs(native_dir, data, raid["started"], min(now, end + POST_RAID_S), deadline=deadline, tz=tz)
    window_events = [e for e in logs["events"] if e["at"] <= end]
    result["logs"] = logs["coverage"]
    result["logs"]["records"] = len(logs["events"])
    if not logs["events"]:
        issues.add("no_matching_log_evidence")
    grouped = defaultdict(list)
    for pull in pulls:
        grouped[pull["map"], pull["boss_id"], pull["difficulty"]].append(pull)
    if catalog:
        difficulty = (1 if raid["size"] == 25 else 0) + (2 if raid["heroic"] else 0)
        for map_id, boss_id in BOSS_NAMES:
            if map_id == catalog.map_id and is_raid_fight(map_id, boss_id):
                grouped.setdefault((map_id, boss_id, difficulty), [])
    responses = defaultdict(list)
    for event in window_events:
        if event["event"] != "mechanic_response":
            continue
        # Require the whole native timestamp second to lie in exactly one stored pull.
        intersecting = [p for p in pulls if p["map"] == event["map"] and p["instance_id"] == event["instance"]
                        and p["start_ms"] < (event["at"]+1)*1000 and p["end_ms"] >= event["at"]*1000]
        matches = [p for p in intersecting if p["start_ms"] <= event["at"]*1000 and (event["at"]+1)*1000 <= p["end_ms"]]
        conflict = bool({"pulls_truncated", "overlapping_pulls_truncated"} & issues) or any(p["map"] == event["map"] and p["instance_id"] == event["instance"]
                    and p["start_ms"] < (event["at"]+1)*1000 and p["end_ms"] >= event["at"]*1000 for p in other)
        book = playbook_for(matches[0]["map"], matches[0]["boss_id"], matches[0]["difficulty"]) if len(matches) == 1 else None
        if book and len(intersecting) == 1 and not conflict and event["id"] in {m.id for m in book.mechanics}:
            p = matches[0]
            responses[p["map"], p["boss_id"], p["difficulty"]].append(event)
        else:
            if len(result["unassigned_responses"]) < MAX_EVENTS:
                result["unassigned_responses"].append(event)
            else:
                issues.add("unassigned_responses_truncated")
    for key, group in sorted(grouped.items()):
        map_id, boss_id, difficulty = key
        book = playbook_for(*key)
        mechanics, spell_labels = {}, defaultdict(set)
        for m in book.mechanics if book else ():
            mechanics[m.id] = dict(id=m.id, name=m.name, kind=m.kind, activations=0, moved_total=0,
                                   moved_max=0, cancelled=0, evidence="no_response_logged" if m.area else "no_direct_action_telemetry")
            for spell in m.hit_ids(book.difficulty) or m.spell_ids(book.difficulty):
                spell_labels[spell].add(m.name)
        for event in responses[key]:
            entry = mechanics[event["id"]]
            entry["activations"] += 1
            entry["moved_total"] += event["members"]
            entry["moved_max"] = max(entry["moved_max"], event["members"])
            entry["cancelled"] += int(event["cancelled"])
            entry["evidence"] = "response_logged"
        taken, failed, dead = Counter(), Counter(), Counter()
        coverage = set()
        for p in group:
            taken.update(hits[p["id"]])
            failed.update(failures[p["id"]])
            dead.update(deaths[p["id"]])
            if not p["checks_complete"]:
                coverage.add("checks_incomplete")
            notes = obj(p["checks_coverage"], [])
            coverage.update(coverage_code(v) for v in (notes if isinstance(notes, list) else []))
        result["bosses"].append(dict(map=map_id, boss_id=boss_id, difficulty=difficulty, boss=boss_name(map_id, boss_id),
            pulls=len(group), kills=sum(p["outcome"] == "killed" for p in group),
            wipes=sum(p["outcome"] == "wiped" for p in group), aborted=sum(p["outcome"] == "aborted" for p in group),
            incomplete=sum(not p["complete"] for p in group), checks=sorted(coverage),
            mechanics=list(mechanics.values()),
            hits=[dict(guid=g, spell=s, mechanics=sorted(spell_labels[s]), hits=taken[g,s,"hits"], requested=taken[g,s,"requested"])
                  for g,s in sorted({(g,s) for g,s,_ in taken})],
            failures=[dict(guid=g, kind=k, key=c, count=n) for (g,k,c),n in sorted(failed.items())],
            deaths=[dict(guid=g, spell=s, count=n) for (g,s),n in sorted(dead.items())]))
    result["group_rez"] = _rez([e for e in window_events if e["event"] == "group_rez_blocked"])
    hazard_groups = {}
    for e in window_events:
        if not e["event"].startswith("hazard"):
            continue
        key = (e["event"], e.get("guid"), e.get("reason"))
        h = hazard_groups.setdefault(key, dict(event=key[0], guid=key[1], reason=key[2], records=0, maxima={}))
        h["records"] += 1
        for field in ("aware", "zones", "spot_members", "detour_members", "exit_members", "readds"):
            if field in e:
                h["maxima"][field] = max(h["maxima"].get(field, 0), e[field])
    result["hazards"] = list(hazard_groups.values())
    for event in logs["events"]:
        if event["event"].startswith("offline"):
            scoped = event.get("raid_id") == raid["raid_id"] if event["source"] == "mind" else event.get("command") in commands
            if scoped:
                if len(result["offline"]) < MAX_EVENTS:
                    result["offline"].append(event)
                else:
                    issues.add("offline_events_truncated")
            elif event["source"] == "native" and event["at"] <= end:
                issues.add("unbound_native_offline_events")
    result["issues"] = sorted(issues)
    return result
