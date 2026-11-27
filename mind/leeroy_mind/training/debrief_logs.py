"""Bounded, content-free evidence from retained native and mind logs. Never a log tail API."""
from collections import Counter
from datetime import datetime
import json
import os
from pathlib import Path
import re
import time

from leeroy_mind.console.mindlog import _open_shared

MAX_BYTES = 32 * 1024 * 1024
MAX_FILES = 64
MAX_EVENTS = 12000
MAX_LINE = 8192
CODE = r"[A-Za-z][A-Za-z0-9_.:-]{0,95}"
IDENT = r"[a-f0-9]{32}"
STAMP = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) ")
NATIVE_FILE = re.compile(r"Server\.log(?:\.\d{4}-\d\d-\d\d \d\d-\d\d-\d\d)?\Z")
MIND_FILE = re.compile(r"mind\.log(?:\.\d{4}-\d\d-\d\d)?\Z")
HAZARD_SOURCE = (r"map=(?P<map>\d+) instance=(?P<instance>\d+) spell=(?P<spell>\d+) "
                 r"kind=(?P<kind>unknown|dynamic|trap|trigger|held_aura|proximity) "
                 r"entry=(?P<entry>\d+) hazard=(?P<hazard>\d+)")
RULES = (
    ("mechanic_response", re.compile(rf"mechanic_response id=(?P<id>{CODE}) spell=(?P<spell>\d+) kind=(?P<kind>{CODE}) members=(?P<members>\d+) map=(?P<map>\d+) instance=(?P<instance>\d+) cancelled=(?P<cancelled>true|false|0|1)")),
    ("group_rez_blocked", re.compile(rf"group_rez_blocked reason=(?P<reason>{CODE}) dead=(?P<dead>\d+) casters=(?P<casters>\d+)(?: fence=(?P<fence>{CODE}(?:,{CODE}){{0,15}}))?")),
    ("hazard_exit_yield", re.compile(r"hazard_exit_yield guid=(?P<guid>\d+) zones=(?P<zones>\d+)"
                                    rf"(?: {HAZARD_SOURCE} details=(?P<details>\d+))?")),
    ("hazard_exit_zone", re.compile(rf"hazard_exit_zone guid=(?P<guid>\d+) {HAZARD_SOURCE}")),
    ("hazards_minute", re.compile(r"hazards_minute aware=(?P<aware>\d+) zones=(?P<zones>\d+) spot_members=(?P<spot_members>\d+) detour_members=(?P<detour_members>\d+) exit_members=(?P<exit_members>\d+)")),
    ("hazard_avoid_on", re.compile(r"hazard_avoid_on guid=(?P<guid>\d+)")),
    ("hazard_avoid_readded", re.compile(r"hazard_avoid_readded guid=(?P<guid>\d+) readds=(?P<readds>\d+)")),
    ("hazard_avoid_off", re.compile(rf"hazard_avoid_off guid=(?P<guid>\d+) reason=(?P<reason>{CODE})")),
    ("offline_stopping", re.compile(rf"leeroy: raid (?P<command>{IDENT}) stopping: (?P<reason>{CODE}) elapsed_ms=(?P<elapsed_ms>\d+) instance=(?P<instance>\d+)")),
    ("offline_full_wipe", re.compile(rf"leeroy: raid (?P<command>{IDENT}) full_wipe dead=(?P<dead>\d+) inside=(?P<inside>\d+) restarts=(?P<restarts>\d+) kills=(?P<kills>\d+)")),
    ("offline_short", re.compile(rf"leeroy: raid (?P<command>{IDENT}) short leader_present=(?P<leader_present>true|false|0|1) present=(?P<present>\d+) min=(?P<min>\d+) members=(?P<members>\d+)")),
    ("offline_terminal", re.compile(rf"leeroy: offline_raid (?P<command>{IDENT}) (?P<state>{CODE}): (?P<reason>{CODE}) \((?P<kills>\d+) kills, (?P<wipes>\d+) wipes\); recovery retained=(?P<retained>true|false|0|1)")),
)
MIND_RULES = (
    ("offline_raid_ended", re.compile(rf"offline_raid_ended raid_id=(?P<raid_id>\d+) outcome=(?P<outcome>{CODE}) cause=(?P<cause>{CODE}|-) kills=(?P<kills>\d+) wipes=(?P<wipes>\d+) raided=(?P<raided>True|False)")),
    ("offline_raid_stop", re.compile(rf"offline_raid_stop raid_id=(?P<raid_id>\d+) reason=(?P<reason>{CODE}) cause=(?P<cause>{CODE}|-) idle_s=(?P<idle_s>\d+) inside=(?P<inside>\d+) members=(?P<members>\d+)")),
    ("offline_raid_terminal_lost", re.compile(r"offline_raid_terminal_lost raid_id=(?P<raid_id>\d+) members=(?P<members>\d+)")),
)
NUMERIC = frozenset("spell members map instance dead casters guid zones aware spot_members detour_members exit_members readds elapsed_ms inside restarts kills present min wipes raid_id idle_s entry hazard details".split())
BOOL = frozenset(("cancelled", "leader_present", "retained", "raided"))


def _match(text, rules):
    for event, pattern in rules:
        found = pattern.fullmatch(text)
        if found:
            fields = {k: v for k, v in found.groupdict().items() if v is not None}
            for k, v in fields.items():
                if k in NUMERIC:
                    if len(v) > (20 if k == "hazard" else 16):
                        return None
                    fields[k] = int(v)
                    if k == "hazard" and fields[k] > 0xffffffffffffffff:
                        return None
                elif k in BOOL:
                    fields[k] = v in ("true", "True", "1")
            return {"event": event, **fields}
    return None


def native_record(line, tz=None):
    """Native times are host wall time. Reject ambiguous/nonexistent DST timestamps."""
    stamp = STAMP.match(line)
    if not stamp:
        return None
    event = _match(line[stamp.end():].strip(), RULES)
    if event is None:
        return None
    wall = datetime.strptime(stamp[1], "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz)
    if wall.replace(fold=0).timestamp() != wall.replace(fold=1).timestamp():
        raise ValueError("ambiguous_native_time")
    at = wall.timestamp()
    if datetime.fromtimestamp(at, tz).replace(tzinfo=None) != wall.replace(tzinfo=None):
        raise ValueError("nonexistent_native_time")
    return {"at": at, "source": "native", **event}


def mind_record(line):
    value = json.loads(line)
    if not isinstance(value, dict) or value.get("event") != "log":
        return None
    message = value.get("msg")
    event = _match(message, MIND_RULES) if isinstance(message, str) else None
    if event is None:
        return None
    at = datetime.fromisoformat(value["ts"])
    if at.tzinfo is None:
        raise ValueError("missing_mind_offset")
    return {"at": at.timestamp(), "source": "mind", **event}


def _linked(path):
    return any(p.is_symlink() or p.is_junction() for p in (path, *path.parents))


def read_logs(native_dir: Path, data: Path, start: float, end: float, *,
              deadline: float, tz=None, byte_limit=MAX_BYTES, file_limit=MAX_FILES,
              event_limit=MAX_EVENTS):
    """Newest retained files first, shared read handles, no full-file reads or raw text output.

    Counts are always lower bounds: rotation gaps and logging suppression cannot be proved
    absent. Native timestamps have whole-second precision; keep boundary seconds, leaving
    assignment to the report's conservative interval matcher.
    """
    events, issues, candidates, seen_files = [], Counter(), [], set()
    scanned = read_bytes = 0
    for source, directory, pattern in (("native", native_dir, NATIVE_FILE), ("mind", data / "logs", MIND_FILE)):
        if _linked(directory):
            issues[source + "_linked_path"] += 1
            continue
        try:
            with os.scandir(directory) as entries:
                for n, entry in enumerate(entries):
                    if n >= 4096 or time.monotonic() >= deadline:
                        issues["directory_budget"] += 1
                        break
                    if not pattern.fullmatch(entry.name):
                        continue
                    path = Path(entry.path)
                    if _linked(path) or not entry.is_file(follow_symlinks=False):
                        issues["linked_or_nonfile"] += 1
                        continue
                    # Windows DirEntry's cached stat may have zero file identity fields.
                    stat = path.stat(follow_symlinks=False)
                    candidates.append((stat.st_mtime, source, path, stat))
        except OSError:
            issues[source + "_unavailable"] += 1
    candidates.sort(key=lambda row: (row[0], row[2].name), reverse=True)
    if len(candidates) > file_limit:
        issues["file_limit"] += len(candidates) - file_limit
    sources = Counter()
    for _, source, path, before in candidates[:file_limit]:
        if read_bytes >= byte_limit or time.monotonic() >= deadline:
            issues["read_budget"] += 1
            break
        identity = (before.st_dev, before.st_ino)
        if identity in seen_files:
            continue
        seen_files.add(identity)
        try:
            with _open_shared(path) as stream:
                opened = os.fstat(stream.fileno())
                if _linked(path) or (opened.st_dev, opened.st_ino) != identity:
                    issues["changed_file"] += 1
                    continue
                take = min(opened.st_size, byte_limit - read_bytes)
                offset = opened.st_size - take
                stream.seek(offset)
                scanned += 1
                sources[source] += 1
                remaining = take
                if offset:
                    issues["truncated_file"] += 1
                    # A partial first line is never parsed as an event.
                    chunk = stream.readline(min(MAX_LINE + 1, remaining))
                    remaining -= len(chunk)
                    read_bytes += len(chunk)
                    discard = not chunk.endswith(b"\n")
                else:
                    discard = False
                while remaining:
                    if time.monotonic() >= deadline:
                        issues["read_budget"] += 1
                        break
                    chunk = stream.readline(min(MAX_LINE + 1, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    read_bytes += len(chunk)
                    was_discarded, discard = discard, not chunk.endswith(b"\n")
                    if was_discarded or discard or len(chunk) > MAX_LINE:
                        issues["partial_or_oversized_line"] += 1
                        continue
                    # Avoid JSON/timestamp work on the very numerous unrelated log lines.
                    if not any(token in chunk for token in (b"mechanic_response", b"group_rez_blocked", b"hazard", b"offline_raid", b"leeroy: raid ")):
                        continue
                    try:
                        text = chunk.decode("utf-8")
                        record = native_record(text.rstrip(), tz) if source == "native" else mind_record(text)
                    except (ValueError, TypeError, KeyError, OverflowError, OSError, RecursionError):
                        issues["invalid_or_ambiguous_record"] += 1
                        continue
                    if record and record["at"] <= end and record["at"] + (1 if source == "native" else 0) > start:
                        if len(events) >= event_limit:
                            issues["event_limit"] += 1
                            return _result(events, issues, scanned, read_bytes, sources, tz)
                        events.append(record)
        except OSError:
            issues[source + "_unreadable_file"] += 1
    return _result(events, issues, scanned, read_bytes, sources, tz)


def _result(events, issues, files, size, sources, tz):
    for source in ("native", "mind"):
        if not sources[source]:
            issues[source + "_no_files"] += 1
    return {"events": sorted(events, key=lambda r: r["at"]),
            "coverage": {"files": files, "bytes": size, "issues": dict(sorted(issues.items())),
                         "native_timezone": str(tz) if tz else "server host local time",
                         "complete": False,
                         "note": "Observed log counts are lower bounds. Missing rotations, suppressed messages and unlogged actions are not passes."}}
