"""`python -m leeroy_mind health`: a read-only check of the live install, for after a release and
for the scheduled task scripts/ac-health-schedule.ps1 installs (every 15 minutes, with --toast).

One ok, FAIL or note line per check, as `doctor` prints them; --json prints the same checks as
one JSON object. It exits 1 when anything FAILs. run_checks() returns the checks themselves.

What it looks at:
- authserver.exe and worldserver.exe, running from the install's build/bin/RelWithDebInfo
  (AC_BIN in scripts/common.sh);
- Server.log there, since worldserver started: the `mod-leeroy: cast N members, pinned N` line,
  both N at least [health] min_cast (a note instead when the log begins after the start: a
  `.reload config` reopens it empty, and the endpoint's cast stands in), and no realm places or
  items file that failed to load (`so no places`, `so no items`);
- the mind: its process family, found by leeroy_mind.lifecycle's rules (those of
  `scripts/ac-mind.py status`), and control.json's pid;
- mind.log since the mind started (rotated files included): its bridge came up and did not go
  down again, and nothing was logged at CRITICAL;
- the mind's control endpoint `status`, through the console's MindLink: control.json's key goes
  only to the listener the TCP table shows is the running mind's own pid;
- the guild console on 127.0.0.1:7881 (a note when it is not running);
- the voice service, located by its own `where` (data dir and ports) as scripts/ac-voice.sh
  locates it: the pid in voice.pid must hold both ports in the TCP table (a note when it is
  stopped, as doctor says);
- Ollama's GET /api/ps, which loads nothing, and the models it has resident;
- activity in mind.db, opened read-only: telemetry on and the training overhead gate passed but
  no tg_pull in 48 h (a note while the gate is in its A/B phase, which records only alternate
  bosses of offline raids, or has failed), a realm body hold or an unknown root older than 1 h,
  cast members without a persona. A table an older mind.db lacks is unavailable, a note;
- the Realm on in mind.toml with a screen that cannot be ready (no realm/blocklist.txt entry, or
  no cached guild name: a FAIL), the newest weekly evaluation incomplete, no secret probe ever
  recorded while secrets exist, and offline raids blocked on each of the last 3 days (notes).

[health] in mind.toml: min_cast (default 100), the floor for both counts in the cast line and
for the endpoint's cast; grace_minutes (default 6), how long after a server or mind start a
missing start line is a note rather than a FAIL. A missing table means the defaults.

It never connects to the bridge port (7878, and mind.toml's [bridge] port): the bridge takes one
mind, and a second connection would knock the live one off. It loads no model, never touches
the GPU, starts and stops nothing and prints no key. It takes no lock either: mind.lock and
voice.lock are never probed (a probe holds the lock for a moment, and a service starting just
then would exit "already running"), so "running" comes from the process list, control.json,
voice.pid and the TCP table. The only file it writes is health-state.json in the data dir, and
only with --toast: the failing checks it last toasted, so a failure gets one toast until it
clears. A read-only open of mind.db can leave SQLite's empty mind.db-wal and mind.db-shm
behind while the mind is stopped, as doctor's and the console's do."""
import argparse
import json
import logging
import math
import os
import re
import sqlite3
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime
from http.client import HTTPException
from pathlib import Path
from typing import Callable

from . import endpoint, lifecycle
from .config import ConfigError, _build as _config_from, default_data_dir, load_config
from .logs import log as log_event

CHECKOUT = Path(__file__).resolve().parents[2]  # mind/leeroy_mind/health.py
STATE_FILE = "health-state.json"
VOICE_PID_FILE = "voice.pid"  # leeroy_voice.lock.PID_FILE (another venv: not importable here)
BRIDGE_PORT = 7878  # mod-leeroy's default bridge port: refused whatever mind.toml says
CONSOLE_PORT = 7881
OK, FAIL, NOTE = "ok", "fail", "note"
MARK = {OK: "ok  ", FAIL: "FAIL", NOTE: "note"}
PULL_S = 48 * 3600  # telemetry on: a tg_pull at least this often
HOLD_S = 3600  # a realm body hold or unknown root older than this is stuck
OFFLINE_OVERDUE_S = 30 * 60
RAID_OPEN_S = 6 * 3600
SELFIE_QUEUED_S = 30 * 3600
CYCLE_ROWS = 100000
CYCLE_SECONDS = 0.8
NIGHT_LOG_BYTES = 4 * 1024 * 1024
NIGHT_LOG_SECONDS = 0.3
HTTP_S = 3.0
STATUS_S = 3.0
SLACK_S = 2.0  # Server.log's whole seconds against a process's exact start
# worldserver's start writes its first Server.log line within seconds; a log that begins later
# was reopened since: `.reload config` rebuilds the appenders, and Server.log's `w` mode starts
# it empty, taking the cast line with it
REOPENED_S = 60.0
FILETIME_EPOCH = 11644473600  # seconds from 1601-01-01 to 1970-01-01
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_STAMP = re.compile(rb"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) ")
_CAST = re.compile(rb"mod-leeroy: cast (\d+) members, pinned (\d+)")
_NO_LIST = re.compile(rb"mod-leeroy: realm (places|items) file .*so no (?:places|items)")
_ROTATED = re.compile(r"mind\.log\.(\d{4}-\d\d-\d\d)\Z")
_IDENT = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
TOAST_APP = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"
# Windows PowerShell 5.1's WinRT toast, as PowerShell's own app. The text comes from the
# environment, never from the script, and goes in as text nodes.
TOAST_PS = r"""$ErrorActionPreference = 'Stop'
$null = [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
$null = [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom, ContentType = WindowsRuntime]
$xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$text = $xml.GetElementsByTagName('text')
$null = $text.Item(0).AppendChild($xml.CreateTextNode($env:LEEROY_HEALTH_TITLE))
$null = $text.Item(1).AppendChild($xml.CreateTextNode($env:LEEROY_HEALTH_BODY))
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($env:LEEROY_HEALTH_APP).Show($toast)
"""
# The game servers' start times and images; nothing is interpolated.
_SERVERS_PS = """$ErrorActionPreference='Stop';
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false);
@(Get-CimInstance Win32_Process -Filter "Name='authserver.exe' OR Name='worldserver.exe'" |
ForEach-Object { [pscustomobject]@{name=$_.Name;pid=$_.ProcessId;
created=$_.CreationDate.ToUniversalTime().ToFileTimeUtc();exe=$_.ExecutablePath} }) |
ConvertTo-Json -Compress"""


@dataclass(frozen=True)
class Check:
    name: str  # a short fixed identifier: the toast names failing checks by it
    status: str  # ok, fail or note
    text: str

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class HealthCfg:
    min_cast: int = 100
    grace_minutes: int = 6


_RULES = {"min_cast": (0, 10000), "grace_minutes": (0, 240)}


def load_health_config(path: Path) -> HealthCfg:
    """[health] in the mind.toml at `path`; a missing file or table means the defaults.
    ConfigError for a file it cannot read, an unknown key or a bad value."""
    try:
        raw = Path(path).read_bytes()
    except FileNotFoundError:
        return HealthCfg()
    except OSError as e:
        raise ConfigError(f"health: cannot read mind.toml ({type(e).__name__})") from None
    try:
        data = tomllib.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"health: cannot read mind.toml ({type(e).__name__})") from None
    table = data.get("health", {})
    if not isinstance(table, dict):
        raise ConfigError("health: expected a table")
    values = {}
    for key, v in table.items():
        if key not in _RULES:
            raise ConfigError(f"health.{key}: not a setting (min_cast, grace_minutes)")
        lo, hi = _RULES[key]
        if type(v) is not int or not lo <= v <= hi:
            raise ConfigError(f"health.{key}: expected an integer {lo}-{hi}")
        values[key] = v
    return HealthCfg(**values)


@dataclass(frozen=True)
class Server:
    image: str  # authserver.exe or worldserver.exe
    pid: int
    started: float  # unix seconds
    exe: str  # "" when it cannot be read


@dataclass(frozen=True)
class MindRun:
    started: float
    pids: tuple[int, ...]


@dataclass
class Probes:
    """Everything the checks look through. real_probes() builds the real ones; tests pass fakes."""
    now: Callable[[], float]
    servers: Callable[[], list[Server]]  # raises when there is no process list
    processes: object | None  # .snapshot() -> [lifecycle.Process]; None: no process list here
    python: Path  # the install's mind venv python
    base_python: str  # the base interpreter its launcher starts
    tcp: Callable[[], list | None]  # console.netinfo.tcp_table
    request: Callable  # endpoint.request
    http_status: Callable[[str, float], int]  # an HTTP GET's status; OSError when none answers
    fetch_json: Callable[[str, float], object]  # an HTTP GET's JSON
    voice: object | None  # lifecycle.VoiceControl, for missing() and where() only; None: none
    open_log: Callable[[Path], object]  # a binary read handle that never blocks a rotation


def filetime_epoch(filetime: int) -> float:
    return filetime / 1e7 - FILETIME_EPOCH


def base_python() -> str:
    """The base interpreter a venv launcher starts. Under pythonw.exe (the scheduled task) it is
    the python.exe beside it, which is what runs the mind."""
    base = Path(getattr(sys, "_base_executable", sys.executable))
    return str(base.with_name("python.exe")) if base.name.lower() == "pythonw.exe" else str(base)


def windows_servers() -> list[Server]:
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _SERVERS_PS],
        capture_output=True, text=True, encoding="utf-8", timeout=15, check=True,
        creationflags=_CREATE_NO_WINDOW)
    rows = json.loads(result.stdout or "[]")
    if isinstance(rows, dict):
        rows = [rows]
    return [Server(str(r["name"]).lower(), int(r["pid"]), filetime_epoch(int(r["created"])),
                   str(r.get("exe") or "")) for r in rows]


def _no_servers() -> list[Server]:
    raise OSError("the game servers' processes are listed on Windows only")


def http_status(url: str, timeout: float) -> int:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # this machine only
    try:
        with opener.open(url, timeout=timeout) as reply:
            return reply.status
    except urllib.error.HTTPError as e:
        return e.code


def real_probes(root: Path) -> Probes:
    from .console import netinfo, status
    from .console.mindlog import _open_shared
    windows = os.name == "nt"
    processes = lifecycle.WindowsProcesses() if windows else None
    return Probes(now=time.time, servers=windows_servers if windows else _no_servers,
                  processes=processes, python=lifecycle.venv_python(root),
                  base_python=base_python(),
                  tcp=netinfo.tcp_table, request=endpoint.request, http_status=http_status,
                  fetch_json=status._fetch_json,
                  voice=lifecycle.VoiceControl(root, processes=processes),
                  open_log=_open_shared)


def bin_dir(root: Path) -> Path:
    return Path(root) / "build" / "bin" / "RelWithDebInfo"


def _when(t: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))


def _ago(seconds: float) -> str:
    s = max(0, round(seconds))
    return f"{s} s" if s < 120 else f"{s // 60} min" if s < 7200 else f"{s // 3600} h"


def _same_file(a, b) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _port(url: str) -> int | None:
    try:
        u = urllib.parse.urlsplit(url)
        return u.port or (443 if u.scheme == "https" else 80)
    except ValueError:
        return None


def _refusing(request, forbidden: frozenset[int]):
    """endpoint.request that refuses the bridge's port before any socket is made."""
    def call(port, key, op, args=None, *, timeout=STATUS_S):
        if port in forbidden:
            raise OSError("the bridge's port is never dialled")
        return request(port, key, op, args, timeout=timeout)
    return call


# -- the checks ------------------------------------------------------------------------------

def _servers(p: Probes, root: Path) -> tuple[list[Check], float | None]:
    """authserver and worldserver, and worldserver's start (None when it is not running)."""
    try:
        rows = p.servers()
    except Exception as e:
        why = f"the process list is unavailable ({type(e).__name__})"
        return [Check("authserver", FAIL, f"authserver.exe: {why}"),
                Check("worldserver", FAIL, f"worldserver.exe: {why}")], None
    out, world = [], None
    where = bin_dir(root)
    for image in ("authserver.exe", "worldserver.exe"):
        name = image.removesuffix(".exe")
        mine = [r for r in rows if r.image == image]
        live = [r for r in mine if r.exe and _same_file(r.exe, where / image)]
        if not live:
            unread = sum(not r.exe for r in mine)
            other = [f"{len(mine) - unread} running from elsewhere"] if len(mine) > unread else []
            other += [f"{unread} whose path cannot be read"] if unread else []
            out.append(Check(name, FAIL, f"{image} is not running from {where}"
                             + (f" ({', '.join(other)})" if other else "")))
            continue
        r = max(live, key=lambda r: r.started)
        if image == "worldserver.exe":
            world = r.started
        out.append(Check(name, OK, f"{image} running (pid {r.pid}, started {_when(r.started)})"))
    return out, world


def _server_log(root: Path, world: float | None, now: float, cfg: HealthCfg) -> list[Check]:
    path = bin_dir(root) / "Server.log"
    if world is None:
        return [Check("server-log", NOTE, "Server.log: not checked, worldserver.exe is not "
                                          "running")]
    try:
        data = path.read_bytes()
    except OSError as e:
        return [Check("server-log", FAIL, f"Server.log cannot be read at {path} "
                                          f"({type(e).__name__})")]
    since = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(world - SLACK_S)).encode()
    cast, failed, fresh, first = None, set(), False, None
    for line in data.splitlines():
        m = _STAMP.match(line)
        if m:
            first = first or m.group(1)
            fresh = m.group(1) >= since  # the same format sorts by time
        if not fresh:
            continue
        if (c := _CAST.search(line)) is not None:
            cast = (int(c.group(1)), int(c.group(2)))
        if (n := _NO_LIST.search(line)) is not None:
            failed.add(n.group(1).decode())
    if cast is not None:
        members, pinned = cast
        low = members < cfg.min_cast or pinned < cfg.min_cast
        line = (f"Server.log: cast {members} members, pinned {pinned}"
                + (f", below the floor of {cfg.min_cast} ([health] min_cast)" if low
                   else f" (floor {cfg.min_cast})"))
        out = [Check("server-log", FAIL if low else OK, line)]
    elif first is not None and first > time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime(world + REOPENED_S)).encode():
        out = [Check("server-log", NOTE, f"Server.log: reopened at {first.decode()}, after "
                                         f"worldserver started at {_when(world)} (a `.reload "
                                         "config` starts it empty), so the cast line is gone; "
                                         "the control endpoint's cast stands in")]
    elif now - world < cfg.grace_minutes * 60:
        out = [Check("server-log", NOTE, f"Server.log: no cast line yet; worldserver started "
                                         f"{_ago(now - world)} ago")]
    else:
        out = [Check("server-log", FAIL, "Server.log: no `mod-leeroy: cast N members, pinned N` "
                                         f"line since worldserver started at {_when(world)}")]
    if failed:
        lists = " and ".join(sorted(failed))
        out.append(Check("realm-lists", FAIL, f"Server.log: the realm {lists} "
                         f"file{'s' if len(failed) > 1 else ''} did not load since worldserver "
                         f"started (`so no {sorted(failed)[0]}`)"))
    else:
        out.append(Check("realm-lists", OK, "Server.log: no `so no places` or `so no items` "
                                            "since worldserver started"))
    return out


def _mind(p: Probes, data: Path, now: float, cfg: HealthCfg) -> tuple[Check, MindRun | None]:
    """The mind's process family by lifecycle's identity rules, and control.json's pid. Not the
    lock probe `ac-mind.py status` adds: it holds mind.lock for a moment, and a mind starting
    just then would exit "already running"."""
    if p.processes is None:
        return Check("mind", FAIL, "mind: no process list here (Windows only)"), None
    try:
        rows = p.processes.snapshot()
        found = lifecycle.family(rows, p.python, p.base_python, data)
    except lifecycle.OwnershipError as e:
        return Check("mind", FAIL, f"mind: {e}"), None
    except Exception as e:
        return Check("mind", FAIL, f"mind: the process list is unavailable "
                                   f"({type(e).__name__})"), None
    record = endpoint.read_key_file(data / endpoint.KEY_FILE)
    if not found:
        if record is not None and any(r.pid == record["pid"] for r in rows):
            return Check("mind", FAIL, f"mind: not running from this checkout, but control.json "
                                       f"names pid {record['pid']}, another Python process (a "
                                       "night run or another checkout)"), None
        return Check("mind", FAIL, "mind: stopped"), None
    run = MindRun(filetime_epoch(min(x.created for x in found)), tuple(x.pid for x in found))
    head, since = f"mind: pid {found[0].pid}", f"started {_when(run.started)}"
    if record is not None and record["pid"] in run.pids:
        return Check("mind", OK, f"{head} running (control.json names it), {since}"), run
    try:
        control = endpoint.load_control_config(data / "mind.toml").enabled
    except ConfigError:
        control = True
    if not control:
        return Check("mind", OK, f"{head} running (its control endpoint is off), {since}"), run
    if now - run.started < cfg.grace_minutes * 60:
        return Check("mind", NOTE, f"{head} starting (no control.json from it yet), {since}"), run
    return Check("mind", OK, f"{head} running (control.json is not its own), {since}"), run


def _log_files(logs: Path, started: float) -> list[Path]:
    """mind.log and the files the midnight rotation made since `started`, oldest first."""
    day = time.strftime("%Y-%m-%d", time.localtime(started))
    rotated = []
    if logs.is_dir():
        for entry in logs.iterdir():
            m = _ROTATED.match(entry.name)
            if m and m.group(1) >= day:
                rotated.append((m.group(1), entry))
    return [e for _, e in sorted(rotated)] + [logs / "mind.log"]


def _records(p: Probes, logs: Path, since: float) -> list[dict]:
    """mind.log's bridge and CRITICAL records since `since`; OSError when mind.log cannot be
    read."""
    out = []
    for path in _log_files(logs, since):
        try:
            with p.open_log(path) as fh:
                data = fh.read()
        except FileNotFoundError:
            if path.name == "mind.log":
                raise
            continue  # rotated away meanwhile
        for line in data.splitlines():
            if b'"bridge_' not in line and b'"CRITICAL"' not in line:
                continue
            try:
                r = json.loads(line)
                at = datetime.fromisoformat(r["ts"]).timestamp()
            except (ValueError, TypeError, KeyError, RecursionError):
                continue
            if isinstance(r, dict) and at >= since:
                r["_at"] = at
                out.append(r)
    return out


def _mind_log(p: Probes, data: Path, run: MindRun | None, now: float,
              cfg: HealthCfg) -> list[Check]:
    if run is None:
        return [Check("mind-log", NOTE, "mind.log: not checked, the mind is not running")]
    try:
        records = _records(p, data / "logs", run.started - 1)
    except OSError as e:
        return [Check("mind-log", FAIL, f"mind.log cannot be read ({type(e).__name__})")]
    up, critical = None, []
    for r in records:
        event = r.get("event")
        if event in ("bridge_connected", "bridge_hello"):
            up = (True, event, r["_at"])
        elif event == "bridge_disconnected":
            up = (False, event, r["_at"])
        if r.get("level") == "CRITICAL":
            critical.append(event if isinstance(event, str) and _IDENT.match(event) else "log")
    if up is None and now - run.started < cfg.grace_minutes * 60:
        out = [Check("mind-log", NOTE, "mind.log: no bridge_connected or bridge_hello yet; the "
                                       f"mind started {_ago(now - run.started)} ago")]
    elif up is None:
        out = [Check("mind-log", FAIL, "mind.log: no bridge_connected or bridge_hello since the "
                                       f"mind started at {_when(run.started)}")]
    elif not up[0]:
        out = [Check("mind-log", FAIL, f"mind.log: the bridge disconnected at {_when(up[2])} "
                                       "and has not come back")]
    else:
        out = [Check("mind-log", OK, f"mind.log: {up[1]} at {_when(up[2])}, since the mind "
                                     "started")]
    if critical:
        out.append(Check("mind-critical", FAIL, f"mind.log: {len(critical)} CRITICAL "
                         f"record{'s' if len(critical) > 1 else ''} since the mind started "
                         f"(last {critical[-1]})"))
    else:
        out.append(Check("mind-critical", OK, "mind.log: no CRITICAL record since the mind "
                                              "started"))
    return out


def _endpoint(p: Probes, data: Path, run: MindRun | None, now: float, cfg: HealthCfg,
              forbidden: frozenset[int]) -> Check:
    from .console.mindlink import LinkError, MindLink
    if run is None:
        return Check("endpoint", NOTE, "control endpoint: not checked, the mind is not running")
    try:
        control = endpoint.load_control_config(data / "mind.toml")
    except ConfigError as e:
        return Check("endpoint", FAIL, f"control endpoint: {e}")
    if not control.enabled:
        return Check("endpoint", NOTE, "control endpoint: off in mind.toml ([control] enabled "
                                       "= false)")
    starting = now - run.started < cfg.grace_minutes * 60
    link = MindLink(data, tcp=p.tcp, request=_refusing(p.request, forbidden))
    try:
        record = link.locate(run.pids)
        if record["port"] in forbidden:
            return Check("endpoint", FAIL, f"control endpoint: control.json names port "
                                           f"{record['port']}, the bridge's; it is not dialled")
        reply = link.call(run.pids, "status", timeout=STATUS_S)
    except LinkError as e:
        return Check("endpoint", NOTE if starting and not e.unanswered else FAIL,
                     f"control endpoint: {e}")
    if reply.get("ok") is not True:
        return Check("endpoint", FAIL, "control endpoint: status refused "
                                       f"({str(reply.get('error') or 'no reason')[:80]})")
    if "bridge_connected" not in reply:
        return Check("endpoint", OK, "control endpoint: status answered; this mind does not "
                                     "report its bridge, cast or schema yet")
    connected, cast, schema = (reply.get(k) for k in ("bridge_connected", "cast",
                                                      "schema_version"))
    said = ", ".join([
        {True: "bridge connected", False: "bridge NOT connected"}.get(connected,
                                                                      "bridge unknown"),
        f"cast {cast}" if type(cast) is int else "cast unknown",
        f"schema v{schema}" if type(schema) is int else "schema unknown"])
    if reply.get("stopping") is True:
        return Check("endpoint", NOTE, f"control endpoint: the mind is stopping ({said})")
    low = connected is True and type(cast) is int and cast < cfg.min_cast
    if connected is False or low:
        floor = f", below the floor of {cfg.min_cast}" if low else ""
        return Check("endpoint", NOTE if starting else FAIL,
                     f"control endpoint: status answered: {said}{floor}")
    return Check("endpoint", OK, f"control endpoint: status answered: {said}")


def _console(p: Probes) -> Check:
    url = f"http://127.0.0.1:{CONSOLE_PORT}/"
    try:
        code = p.http_status(url, HTTP_S)
    except OSError as e:
        reason = getattr(e, "reason", e)
        if isinstance(reason, ConnectionRefusedError):
            return Check("console", NOTE, f"console: not running on 127.0.0.1:{CONSOLE_PORT}")
        return Check("console", FAIL, f"console: 127.0.0.1:{CONSOLE_PORT} did not answer "
                                      f"({type(reason).__name__})")
    except HTTPException as e:
        return Check("console", FAIL, f"console: 127.0.0.1:{CONSOLE_PORT} did not answer in "
                                      f"HTTP ({type(e).__name__})")
    if code != 200:
        return Check("console", FAIL, f"console: 127.0.0.1:{CONSOLE_PORT} answered HTTP {code}")
    return Check("console", OK, f"console answers on {url}")


def _voice_pid(folder: Path) -> int | None:
    """voice.pid in the voice service's data dir, as the service writes it at its start and
    removes at its end (leeroy_voice.lock.read_pid); None when there is none to read."""
    try:
        text = (Path(folder) / VOICE_PID_FILE).read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return int(text) if text.isdecimal() else None


def _voice(p: Probes, data: Path) -> Check:
    """Running is voice.pid's pid listening on both of `where`'s ports. Not `leeroy_voice held`,
    which ac-voice.sh status asks: it holds voice.lock for a moment, and a service starting just
    then would exit "already running"."""
    from .console.netinfo import LISTEN
    from .voice.config import load_voice_config
    try:
        if not load_voice_config(data / "mind.toml").enabled:
            return Check("voice", NOTE, "voice service: disabled in mind.toml")
    except (ConfigError, TypeError) as e:
        return Check("voice", NOTE, f"voice service: not checked ({type(e).__name__})")
    v = p.voice
    why = "no voice service here" if v is None else v.missing()
    if why:
        return Check("voice", NOTE, f"voice service: {why}")
    try:
        where = v.where()
    except lifecycle.OwnershipError as e:
        return Check("voice", NOTE, f"voice service: {e}")
    except (OSError, subprocess.SubprocessError) as e:
        return Check("voice", NOTE, f"voice service: not checked ({type(e).__name__})")
    pid, ports = _voice_pid(where.data_dir), (where.mind_port, where.ui_port)
    conns = p.tcp()
    if conns is None:
        return Check("voice", NOTE, "voice service: not checked, no TCP table here")
    owners = {port: {c.pid for c in conns if c.state == LISTEN and c.local_port == port}
              for port in ports}
    if not any(owners.values()):
        left = (f"; voice.pid names pid {pid} (starting, or left by a crash)"
                if pid is not None else "")
        return Check("voice", NOTE, f"voice service: stopped (ports {ports[0]} and {ports[1]} "
                                    f"closed){left}")
    if pid is None:
        return Check("voice", FAIL, "voice service: its ports are listening, but there is no "
                                    "voice.pid in its data dir")
    wrong = [f"port {port} is held by pid {', '.join(map(str, sorted(owners[port])))}"
             if owners[port] else f"port {port} is not listening"
             for port in ports if pid not in owners[port]]
    if wrong:
        return Check("voice", FAIL, f"voice service: pid {pid}, but {'; '.join(wrong)}")
    return Check("voice", OK, f"voice service: pid {pid}, listening on {ports[0]} and "
                              f"{ports[1]}")


def _ollama(p: Probes, cfg, forbidden: frozenset[int]) -> list[Check]:
    from .console.status import ollama_models
    url = cfg.ollama.url
    if _port(url) in forbidden:
        return [Check("ollama", FAIL, f"ollama: {url} is the bridge's port; it is not dialled")]
    m = cfg.models
    seen = ollama_models(url, {"live": m.live, "embedding": m.embedding,
                               "reflection": m.reflection}, fetch=p.fetch_json)
    if not seen["ok"]:
        return [Check("ollama", FAIL, f"ollama: {seen['error']} at {url}")]
    models = seen["models"]
    names = ", ".join(x["name"] + (f" ({x['role']})" if x["role"] else "") for x in models)
    return [Check("ollama", OK, f"ollama answers at {url}"),
            Check("ollama-models", NOTE, f"ollama: {len(models)} loaded: {names}" if models
                  else "ollama: no model loaded")]


def _unavailable(e: sqlite3.Error) -> bool:
    return isinstance(e, sqlite3.OperationalError) and "no such table" in str(e)


def _native_flag(root: Path, key: str) -> bool | None:
    """A boolean key of the live mod_leeroy.conf (only that key is read); None when the file
    cannot be read. A missing key is native's default, off."""
    path = bin_dir(root) / "configs" / "modules" / "mod_leeroy.conf"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    found = re.findall(rf'^[ \t]*{re.escape(key)}[ \t]*=[ \t]*"?([^"\s#]*)', text, re.MULTILINE)
    return bool(found) and found[-1].lower() in ("1", "true", "yes")


def _telemetry(conn, root: Path, data: Path, now: float) -> Check:
    from .training.config import load_training_config
    try:
        t = load_training_config(data / "mind.toml")
    except ConfigError as e:
        return Check("telemetry", NOTE, f"telemetry: not checked ({e})")
    if not (t.enabled and t.telemetry):
        return Check("telemetry", NOTE, "telemetry: off in mind.toml ([training]); no pulls "
                                        "expected")
    native = _native_flag(root, "Leeroy.Telemetry.Enable")
    if native is False:
        return Check("telemetry", NOTE, "telemetry: off in mod_leeroy.conf "
                                        "(Leeroy.Telemetry.Enable); no pulls expected")
    if native is None:
        return Check("telemetry", NOTE, "telemetry: not checked, mod_leeroy.conf cannot be read")
    try:
        phase, raids = _gate_phase(conn)
        last = conn.execute("SELECT max(received) FROM tg_pull").fetchone()[0]
    except sqlite3.Error as e:
        if _unavailable(e):
            return Check("telemetry", NOTE, "telemetry: unavailable (this mind.db has no tg_pull)")
        return Check("telemetry", FAIL, f"telemetry: tg_pull cannot be read ({type(e).__name__})")
    seen = _when(last) if last is not None else "never"
    if last is not None and now - last <= PULL_S:
        return Check("telemetry", OK, f"telemetry: last tg_pull {_ago(now - last)} ago "
                                      f"(gate {phase})")
    if phase == "ab":
        return Check("telemetry", NOTE, "telemetry: overhead gate in phase ab (A/B, "
                     f"{raids} offline raid{'s' if raids != 1 else ''} counted): only alternate "
                     "bosses of offline raids are recorded, so no tg_pull in 48 h is expected "
                     f"(last {seen})")
    if phase == "failed":
        return Check("telemetry", NOTE, "telemetry: overhead gate failed (or its state is "
                     f"unreadable), so nothing is recorded; no pulls expected (last {seen})")
    return Check("telemetry", FAIL, f"telemetry: on and the overhead gate passed, but no tg_pull "
                                    f"in 48 h (last {seen})")


def _gate_phase(conn) -> tuple[str, int]:
    """The training overhead gate's phase (ab, passed or failed) and the offline raids it has
    counted, from meta tg_gate as `training gate` reads it (training.inspect.gate_state). A
    value the gate itself would not accept is "failed", as training.gate.Gate reads it:
    corrupt state never re-arms recording."""
    from .training.inspect import gate_state
    try:
        saved = gate_state(conn)
    except (ValueError, TypeError):
        return "failed", 0
    if not isinstance(saved, dict):
        return "failed", 0
    phase, raids, verdict = saved.get("phase"), saved.get("raids"), saved.get("verdict_ms")

    def number(v):
        return type(v) in (int, float) and math.isfinite(v)
    if (phase not in ("ab", "passed", "failed") or type(raids) is not int or raids < 0
            or (verdict is not None and not number(verdict))
            or (phase == "passed" and not number(verdict))):
        return "failed", 0
    return phase, raids


def _realm_holds(conn, now: float) -> Check:
    try:
        rows = conn.execute(
            "SELECT kind, state, transport_state, reserved_manifest, created, command_id, evidence, policy FROM realm_action "
            "WHERE role = 'root' AND state NOT IN ('completed', 'not_sent') AND created < ?",
            (now - HOLD_S,)).fetchall()
    except sqlite3.Error as e:
        if _unavailable(e):
            return Check("realm-holds", NOTE, "realm: unavailable (this mind.db has no "
                                              "realm_action)")
        return Check("realm-holds", FAIL, f"realm: realm_action cannot be read "
                                          f"({type(e).__name__})")
    from .realm.action_store import released_members
    from .realm.protocol import READBACK_CHECKS
    holds, unknown, bodies, oldest = 0, 0, set(), None
    kinds = Counter()
    details, quiet = [], []
    for kind, state, transport, manifest, created, ident, raw_evidence, policy in rows:
        try:
            guids = json.loads(manifest)
        except (TypeError, ValueError):
            guids = []
        try:
            evidence = json.loads(raw_evidence)
            job = evidence.get("job", {}) if isinstance(evidence, dict) else {}
            if not isinstance(job, dict): job = {}
        except (TypeError, ValueError):
            job = {}
        released = released_members("root", policy, kind, state, raw_evidence, guids)
        if released:
            guids = [g for g in guids if g not in released]
        held = transport != "not_entered" and bool(guids)
        if not held and state != "unknown":
            continue
        # Native's release proof covers every member: still unknown, but it holds nobody.
        wholly_released = bool(released) and not guids and state == "unknown"
        if not wholly_released:
            holds += held
            unknown += state == "unknown"
            if held and isinstance(guids, list):
                bodies.update(g for g in guids if type(g) is int)
            kinds[kind if isinstance(kind, str) and _IDENT.match(kind) else "?"] += 1
            oldest = created if oldest is None else min(oldest, created)
        # IDs/kinds only. Do not echo a malformed stored value into a shell command.
        if isinstance(ident, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", ident):
            count = len({g for g in guids if type(g) is int and g > 0}) if isinstance(guids, list) else 0
            code = kind if isinstance(kind, str) and _IDENT.fullmatch(kind) else "unknown_kind"
            detail = f"{ident} kind={code} members={count}: realm release {ident} --reason reviewed_stale_hold"
            member, check = job.get("readback_guid"), job.get("check")
            if type(member) is int and 0 < member <= 0xFFFFFFFF and type(check) is str and check in READBACK_CHECKS:
                detail += f" readback_guid={member} check={check}"
            (quiet if wholly_released else details).append(detail)
    nobody = (f"{len(quiet)} unknown root{'s' if len(quiet) != 1 else ''} older than 1 h "
              f"hold{'s' if len(quiet) == 1 else ''} nobody")
    if not holds and not unknown:
        if quiet:
            return Check("realm-holds", NOTE,
                         f"realm: {nobody} (native released every member); to tidy up, with the mind stopped: "
                         + "; ".join(quiet[:20])
                         + (f"; {len(quiet) - 20} more roots" if len(quiet) > 20 else ""))
        return Check("realm-holds", OK, "realm: no body hold or unknown root older than 1 h")
    return Check("realm-holds", FAIL,
                 f"realm: {holds} body hold{'s' if holds != 1 else ''} ({len(bodies)} members) "
                 f"and {unknown} unknown root{'s' if unknown != 1 else ''} older than 1 h, "
                 f"since {_when(oldest)}: "
                 + ", ".join(f"{k} {n}" for k, n in sorted(kinds.items()))
                 + "; inspect native state, then with the mind stopped: "
                 + "; ".join(details[:20])
                 + (f"; {len(details) - 20} more roots" if len(details) > 20 else "")
                 + (f"; {len(quiet)} more {nobody.partition(' ')[2]}" if quiet else "")
                 + "; on restart, realm release also frees eligible native holds when the worldserver supports realm_forget")


def _table_check(name, operation):
    """Older schemas are a note, other SQLite errors are a failed observation."""
    try:
        return operation()
    except sqlite3.Error as exc:
        if _unavailable(exc) or "no such column" in str(exc):
            return Check(name, NOTE, f"{name}: not set up (older database)")
        return Check(name, FAIL, f"{name}: cannot read ({type(exc).__name__})")


def _offline_raid(conn, now: float) -> Check:
    name = "offline-raid"

    def check():
        row = conn.execute("SELECT value FROM meta WHERE key='guild:offline:owned'").fetchone()
        if not row:
            return Check(name, OK, "offline raid: no owned run")
        try:
            receipt = json.loads(row[0])
            if not isinstance(receipt, dict):
                raise ValueError
            if receipt.get("phase") in ("complete", "rejected"):
                return Check(name, OK, "offline raid: owned run is terminal")
            ident, deadline = receipt["raid_id"], receipt["deadline"]
            if type(deadline) not in (int, float) or not math.isfinite(deadline):
                raise ValueError
            if ident is None and receipt.get("phase") in (
                    "acquiring", "awaiting_ack", "accepted", "uncertain"):
                # Native acknowledgement/progress precedes the local RaidSession.
                # The persisted UUID identifies that owned command until _open().
                command = receipt.get("id")
                if not isinstance(command, str) or not re.fullmatch(r"[a-fA-F0-9]{32}", command):
                    raise ValueError
                ident = f"command {command}"
            elif type(ident) is not int or ident <= 0:
                raise ValueError
        except (ValueError, TypeError, KeyError, RecursionError):
            return Check(name, FAIL, "offline raid: invalid owned receipt; inspect raid history")
        overdue = now - deadline
        if overdue > OFFLINE_OVERDUE_S:
            return Check(name, FAIL, f"offline raid {ident}: deadline {_ago(overdue)} ago; "
                         "inspect raid history and native state before releasing its hold")
        return Check(name, OK, f"offline raid {ident}: within deadline plus 30 min")
    return _table_check(name, check)


def _guild_raids(conn, now: float) -> Check:
    name = "guild-raids"

    def check():
        rows = conn.execute("SELECT raid_id, started FROM guild_raid WHERE ended IS NULL "
                            "AND started < ? ORDER BY started, raid_id LIMIT 21",
                            (now - RAID_OPEN_S,)).fetchall()
        if not rows:
            return Check(name, OK, "guild raids: none open longer than 6 h")
        detail = ", ".join(f"{int(ident)} open {_ago(now - started)}" for ident, started in rows[:20])
        return Check(name, FAIL, f"guild raids: {detail}"
                     + ("; more omitted" if len(rows) > 20 else "")
                     + "; inspect raid history and confirm the run ended before clearing its hold")
    return _table_check(name, check)


def _night_log_counts(data: Path, night: str, started: float, finished: float, open_log):
    """Bounded newest-first fallback, with timestamp coverage rather than file presence.

    A readable newer log proves nothing about an older night. Require records spanning
    both bounds and every intervening local day; a missing rotation is not a clean night.
    Never retain/print message or unit text.
    """
    counts, codes = Counter(), Counter()
    remaining, until = NIGHT_LOG_BYTES, time.monotonic() + NIGHT_LOG_SECONDS
    incomplete, seen = False, False
    before = after = False
    first_day = datetime.fromtimestamp(started).date().toordinal()
    last_day = datetime.fromtimestamp(finished).date().toordinal()
    days = set()
    for path in reversed(_log_files(data / "logs", started)):
        if remaining <= 0 or time.monotonic() >= until:
            incomplete = True
            break
        try:
            with open_log(path) as fh:
                size = fh.seek(0, os.SEEK_END)
                offset = max(0, size - remaining)
                fh.seek(offset)
                raw = fh.read(remaining)
                remaining -= len(raw)
                if offset:
                    raw = raw.partition(b"\n")[2]
                    incomplete = True
                seen = True
        except FileNotFoundError:
            continue
        except OSError:
            incomplete = True
            continue
        for line in raw.splitlines():
            if time.monotonic() >= until:
                incomplete = True
                break
            try:
                record = json.loads(line)
                at = datetime.fromisoformat(record["ts"]).timestamp()
                before |= at <= started
                after |= at >= finished
                day = datetime.fromtimestamp(at).date().toordinal()
                if first_day <= day <= last_day:
                    days.add(day)
                if record.get("event") != "nightly_unit_failed" or record.get("night") != night:
                    continue
                if not started <= at <= finished:
                    continue
                stage = record.get("stage")
                stage = stage if isinstance(stage, str) and _IDENT.fullmatch(stage) else "unknown_stage"
                counts[stage] += 1
                code = record.get("error")
                if isinstance(code, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}(?:Error|Exception)", code):
                    codes[code] += 1
            except (ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError,
                    OSError):
                incomplete = True
    covered = before and after and len(days) == last_day - first_day + 1
    return counts, codes, incomplete, seen, covered


def _night_failures(conn, data: Path, open_log) -> Check:
    name = "night-failures"

    def check():
        row = conn.execute("SELECT night, started, finished, error FROM night_run "
                           "WHERE finished IS NOT NULL AND status NOT IN ('running','interrupted') "
                           "ORDER BY finished DESC, night DESC LIMIT 1").fetchone()
        if row is None:
            return Check(name, NOTE, "night failures: no finished night yet")
        night, started, finished, error = row
        counts = None
        if error is not None:
            try:
                stored = json.loads(error)
                if not isinstance(stored, dict) or any(
                        not isinstance(k, str) or not _IDENT.fullmatch(k)
                        or type(n) is not int or n < 0 for k, n in stored.items()):
                    raise ValueError
                counts = Counter(stored)
            except (ValueError, TypeError, RecursionError):
                pass
        codes, incomplete, missing_coverage = Counter(), False, False
        source = "stored"
        if counts is None or sum(counts.values()):
            logged, codes, incomplete, seen, covered = _night_log_counts(
                data, night, started, finished, open_log)
            if counts is None:
                counts, source = logged, "logs"
                if not seen:
                    return Check(name, NOTE, "night failures: no stored counts; logs unavailable")
                missing_coverage = not covered
        total = sum(counts.values())
        status = FAIL if total >= 10 else NOTE if total or incomplete or missing_coverage else OK
        detail = ", ".join(f"{stage}={n}" for stage, n in sorted(counts.items()) if n)
        observed = " observed" if source == "logs" and (incomplete or missing_coverage) else ""
        text = f"night failures: latest finished night, {total}{observed} failed units ({source})"
        if detail:
            text += "; " + detail
        if codes:
            code, n = sorted(codes.items(), key=lambda item: (-item[1], item[0]))[0]
            text += f"; top error={code} ({n} logged)"
        if incomplete:
            text += "; log scan incomplete (budget or unreadable records)"
        if missing_coverage:
            text += "; historical log coverage incomplete"
        return Check(name, status, text)
    return _table_check(name, check)


def _evidence_cycles(conn, *, row_budget=CYCLE_ROWS, seconds=CYCLE_SECONDS) -> Check:
    """Count memories reaching cycles in the critic's shared persisted-edge graph.

    Cache completed acyclic subgraphs and proven cyclic ancestry. A budget or a
    malformed edge never becomes an acyclic result. No memory text is printed.
    """
    from .storage.provenance import ProvenanceError, canonical, edges
    name = "evidence-cycles"

    def check():
        until = time.monotonic() + seconds
        cache, safe, cyclic = {}, set(), set()
        checked = bad = invalid = 0
        incomplete = False
        conn.set_progress_handler(lambda: int(time.monotonic() >= until), 1000)

        def children(ref):
            if time.monotonic() >= until or len(cache) >= row_budget:
                raise TimeoutError
            if ref not in cache:
                cache[ref] = tuple(canonical(child) for child in edges(conn, ref).children)
            return iter(cache[ref])

        try:
            roots = conn.execute("SELECT id FROM memory ORDER BY id LIMIT ?", (row_budget + 1,)).fetchall()
            for row in roots[:row_budget]:
                if time.monotonic() >= until:
                    incomplete = True
                    break
                root = f"memory:{row[0]}"
                active, stack = {root}, []
                try:
                    if root not in safe and root not in cyclic:
                        stack.append((root, children(root)))
                    while stack:
                        ref, pending = stack[-1]
                        child = next(pending, None)
                        if child is None:
                            safe.add(ref)
                            active.remove(ref)
                            stack.pop()
                        elif child in cyclic or child in active:
                            cyclic.update(active)
                            break
                        elif child not in safe:
                            active.add(child)
                            stack.append((child, children(child)))
                    checked += 1
                    bad += root in cyclic
                except (ProvenanceError, ValueError, TypeError, RecursionError):
                    invalid += 1
                except TimeoutError:
                    incomplete = True
                    break
            incomplete |= len(roots) > row_budget
        except sqlite3.OperationalError as exc:
            if "interrupted" not in str(exc):
                raise
            incomplete = True
        finally:
            conn.set_progress_handler(None, 0)
        status = FAIL if bad >= 50 else NOTE if bad or incomplete or invalid else OK
        text = f"evidence cycles: {bad} cyclic memories among {checked} checked; {len(cache)} graph nodes"
        if incomplete:
            text += f"; incomplete (budget {row_budget} rows/{seconds:g}s)"
        if invalid:
            text += f"; incomplete ({invalid} invalid provenance rows)"
        return Check(name, status, text)
    return _table_check(name, check)


def _selfie_queue(conn, now: float) -> Check:
    name = "selfie-queue"

    def check():
        count, oldest = conn.execute("SELECT count(*), min(requested) FROM selfie_set "
                                    "WHERE status='queued' AND requested < ?",
                                    (now - SELFIE_QUEUED_S,)).fetchone()
        if count:
            return Check(name, NOTE, f"selfie queue: {count} sets older than 30 h, "
                         f"oldest {_ago(now - oldest)}; inspect the next selfie stage")
        return Check(name, OK, "selfie queue: no queued set older than 30 h")
    return _table_check(name, check)


def _unvoiced(conn) -> Check:
    from .__main__ import _doctor_unvoiced, _human_guid
    from .clock import SystemClock
    from .storage.store import Store
    store = Store(conn, clock=SystemClock())
    try:
        try:
            human = _human_guid(store, None)
        except SystemExit:
            human = None
        lines = _doctor_unvoiced(store, human)
    except sqlite3.Error as e:
        if _unavailable(e):
            return Check("unvoiced", NOTE, "personas: unavailable (this mind.db has no roster)")
        return Check("unvoiced", FAIL, f"personas: cannot be read ({type(e).__name__})")
    if lines:
        return Check("unvoiced", NOTE, lines[0][1])
    return Check("unvoiced", OK, "personas: every cast member has one")


BLOCKED_DAYS = 3
_CODE = re.compile(r"[A-Za-z0-9_:.-]{1,64}\Z")


def _code(value) -> str:
    """A stored refusal or failure code, or "?" for anything that is not one."""
    return value if isinstance(value, str) and _CODE.match(value) else "?"


def _realm_screen(conn, data: Path) -> Check:
    """The Realm is on in mind.toml but its screen (realm.screen.Screen.ready) cannot be ready:
    no blocklist with an entry, or no guild name cached (meta realm:guild_name, as
    realm.night.own_guild reads it). Every rival line, forum post and write-up then waits."""
    name = "realm-screen"
    from .realm.config import load_realm_config
    try:
        on = load_realm_config(data / "mind.toml").enabled
    except ConfigError:
        return Check(name, NOTE, "realm screen: not checked, [realm] in mind.toml is invalid")
    if not on:
        return Check(name, OK, "realm screen: the Realm is off in mind.toml")

    def check():
        missing = []
        try:
            lines = (data / "realm" / "blocklist.txt").read_text(encoding="utf-8-sig").splitlines()
        except FileNotFoundError:
            missing.append("realm/blocklist.txt is missing from the data dir")
        except (OSError, UnicodeError) as e:
            missing.append(f"realm/blocklist.txt cannot be read ({type(e).__name__})")
        else:
            if not any((s := line.strip()) and not s.startswith("#") for line in lines):
                missing.append("realm/blocklist.txt has no entry")
        row = conn.execute("SELECT value FROM meta WHERE key='realm:guild_name'").fetchone()
        try:
            cached = json.loads(row[0]) if row else None
        except (TypeError, ValueError, RecursionError):
            cached = None
        guild = cached.get("guild") if isinstance(cached, dict) else None
        title = cached.get("name") if isinstance(cached, dict) else None
        if not (type(guild) is int and 1 <= guild <= 0xFFFFFFFF and isinstance(title, str) and title.strip()):
            missing.append("the guild name is not cached (the mind reads it at each hello)")
        if not missing:
            return Check(name, OK, "realm screen: ready (blocklist and guild name)")
        return Check(name, FAIL, "realm screen: the Realm is on in mind.toml but its screen cannot "
                     "be ready: " + "; ".join(missing) + ". Rival lines, forum posts and write-ups "
                     "wait until it is (one word or phrase per blocklist line)")
    return _table_check(name, check)


def _weekly_eval(conn) -> Check:
    """The newest week's evaluation receipt (meta nightly:eval-result:<week>, written by
    eval.run_weekly); the nights retry an incomplete week until the next Sunday."""
    name = "weekly-eval"

    def check():
        row = conn.execute("SELECT key, value FROM meta WHERE key LIKE 'nightly:eval-result:%' "
                           "ORDER BY key DESC LIMIT 1").fetchone()
        if row is None:
            return Check(name, OK, "weekly evaluation: none run yet")
        week = row[0].removeprefix("nightly:eval-result:")
        week = week if re.fullmatch(r"\d{4}-W\d\d", week) else "?"
        try:
            result = json.loads(row[1])
        except (TypeError, ValueError, RecursionError):
            result = None
        if isinstance(result, dict) and result.get("complete") is True:
            return Check(name, OK, f"weekly evaluation {week}: complete")
        return Check(name, NOTE, f"weekly evaluation {week}: incomplete; see the failed sections "
                     f"in reports/{week}.json (the nights retry it until the next Sunday)")
    return _table_check(name, check)


def _secret_gate(conn) -> Check:
    """No secret probe was ever recorded (probe_run) while a secret memory or fact exists: the
    secret gate (nightly.knowledge.secret_probe_passed) stays closed, so secrets never spread,
    encounters cannot use them and mood ignores them."""
    name = "secret-gate"

    def check():
        runs, newest = conn.execute("SELECT count(*), max(ts) FROM probe_run").fetchone()
        if runs:
            return Check(name, OK, f"secret probe: {runs} run{'s' if runs != 1 else ''}, "
                                   f"newest {_when(newest)}")
        needed = conn.execute("SELECT EXISTS(SELECT 1 FROM memory WHERE scope='secret') "
                              "OR EXISTS(SELECT 1 FROM fact WHERE secret=1)").fetchone()[0]
        if not needed:
            return Check(name, OK, "secret probe: none run yet, and no secret is waiting on it")
        return Check(name, NOTE, "secret probe: probe_run is empty, so the secret gate stays closed "
                     "while secrets exist; it is measured by the weekly evaluation")
    return _table_check(name, check)


def _offline_days(conn, now: float) -> Check:
    """Offline raids blocked (meta guild:offline:day:<date> `blocked`) on each of the last
    BLOCKED_DAYS whole local days, yesterday and before."""
    name = "offline-days"
    from datetime import timedelta

    def check():
        today = datetime.fromtimestamp(now).date()
        days = [(today - timedelta(days=n)).isoformat() for n in range(1, BLOCKED_DAYS + 1)]
        codes = []
        for day in days:
            row = conn.execute("SELECT value FROM meta WHERE key=?", ("guild:offline:day:" + day,)).fetchone()
            try:
                state = json.loads(row[0]) if row else None
            except (TypeError, ValueError, RecursionError):
                state = None
            if not isinstance(state, dict) or state.get("blocked") is not True:
                return Check(name, OK, f"offline raids: not blocked on each of the last {BLOCKED_DAYS} days")
            codes.append(f"{day} {_code(state.get('refused') or state.get('failed'))}")
        return Check(name, NOTE, f"offline raids: blocked on each of the last {BLOCKED_DAYS} days "
                     f"({', '.join(codes)}); see offline_raid_refused in mind.log")
    return _table_check(name, check)


def _activity(root: Path, data: Path, now: float, open_log=None) -> list[Check]:
    path = data / "mind.db"
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error as e:
        return [Check("mind-db", FAIL, f"mind.db cannot be opened read-only "
                                       f"({type(e).__name__})")]
    conn.row_factory = sqlite3.Row
    try:
        return [_guarded("telemetry", _telemetry, conn, root, data, now),
                _guarded("realm-holds", _realm_holds, conn, now),
                _guarded("offline-raid", _offline_raid, conn, now),
                _guarded("guild-raids", _guild_raids, conn, now),
                _guarded("night-failures", _night_failures, conn, data,
                         open_log or (lambda path: open(path, "rb"))),
                _guarded("evidence-cycles", _evidence_cycles, conn),
                _guarded("selfie-queue", _selfie_queue, conn, now),
                _guarded("unvoiced", _unvoiced, conn),
                _guarded("realm-screen", _realm_screen, conn, data),
                _guarded("weekly-eval", _weekly_eval, conn),
                _guarded("secret-gate", _secret_gate, conn),
                _guarded("offline-days", _offline_days, conn, now),
                _guarded("training-lab", _training_lab, conn, now)]
    finally:
        conn.close()


def _guarded(name: str, check, *args):
    """A check that raised is a FAIL of its own, not the end of the run."""
    try:
        return check(*args)
    except Exception as e:
        return Check(name, FAIL, f"{name}: the check itself failed ({type(e).__name__})")


def run_checks(root: Path, data_dir: Path, *, probes: Probes | None = None) -> list[Check]:
    """Every check on the install at `root` (its build/ and venvs) with the mind's data dir
    `data_dir`, in order. Reads only."""
    from .__main__ import _data_dir_problem
    root, data = Path(root), Path(data_dir)
    problem, _ = _data_dir_problem(data)
    if problem:
        return [Check("data-dir", FAIL, f"data dir {data}: {problem}")]
    p = probes or real_probes(root)
    now = p.now()
    out = []
    try:
        cfg = load_config(data / "mind.toml")
    except ConfigError as e:
        out.append(Check("mind-toml", FAIL, f"mind.toml: {e}"))
        cfg = _config_from({}, "the defaults")  # for the bridge's port and Ollama's URL
    try:
        hcfg = load_health_config(data / "mind.toml")
    except ConfigError as e:
        out.append(Check("health-config", FAIL, f"mind.toml {e}; the defaults are used"))
        hcfg = HealthCfg()
    forbidden = frozenset({BRIDGE_PORT, cfg.bridge.port})
    try:
        servers, world = _servers(p, root)
    except Exception as e:
        servers, world = [Check("servers", FAIL, f"servers: the check itself failed "
                                                 f"({type(e).__name__})")], None
    out += servers
    out.append(_guarded("server-log", _server_log, root, world, now, hcfg))
    try:
        mind, run = _mind(p, data, now, hcfg)
    except Exception as e:
        mind, run = Check("mind", FAIL, f"mind: the check itself failed ({type(e).__name__})"), None
    out.append(mind)
    out.append(_guarded("mind-log", _mind_log, p, data, run, now, hcfg))
    out.append(_guarded("endpoint", _endpoint, p, data, run, now, hcfg, forbidden))
    out.append(_guarded("console", _console, p))
    out.append(_guarded("voice", _voice, p, data))
    out.append(_guarded("ollama", _ollama, p, cfg, forbidden))
    out += _activity(root, data, now, p.open_log)
    return [c for item in out for c in (item if isinstance(item, list) else [item])]


# -- the toast -------------------------------------------------------------------------------

def read_state(path: Path) -> frozenset[str]:
    """The failing checks the last toast named (health-state.json); none when it is missing or
    unreadable."""
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return frozenset()
    failing = value.get("failing") if isinstance(value, dict) else None
    if not isinstance(failing, list):
        return frozenset()
    return frozenset(x for x in failing if isinstance(x, str))


def show_toast(title: str, body: str) -> bool:
    """A Windows toast through Windows PowerShell's WinRT ToastNotificationManager; whether it
    was shown."""
    if os.name != "nt":
        return False
    env = dict(os.environ, LEEROY_HEALTH_TITLE=title, LEEROY_HEALTH_BODY=body,
               LEEROY_HEALTH_APP=TOAST_APP)
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", TOAST_PS], env=env,
            capture_output=True, timeout=30, creationflags=_CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def alert(checks: list[Check], data_dir: Path, *, show=show_toast, now=time.time) -> bool:
    """--toast: a toast when a check fails that the last toast did not name, with the title and
    the failing checks' names only. health-state.json keeps the failing set: the same failures
    get no second toast until they clear, and a failure that clears and comes back gets a new
    one. A toast that could not be shown leaves the state as it was, so the next run tries
    again. Returns whether a toast was shown."""
    failing = frozenset(c.name for c in checks if c.status == FAIL)
    data = Path(data_dir)
    path = data / STATE_FILE
    previous = read_state(path)
    shown = False
    if failing - previous:
        names = sorted(failing)
        title = f"Leeroy health: {len(names)} check{'s' if len(names) != 1 else ''} failing"
        if not show(title, ", ".join(names)):
            return False
        shown = True
    if failing != previous and data.is_dir():
        try:
            path.write_text(json.dumps({"failing": sorted(failing),
                                        "at": datetime.fromtimestamp(now()).astimezone()
                                        .isoformat(timespec="seconds")}) + "\n",
                            encoding="ascii")
        except OSError as e:  # the same failures toast again next time: say why, once
            log_event("health_state_unwritten", level=logging.WARNING, error=type(e).__name__)
    return shown


# -- the command -----------------------------------------------------------------------------

def _ascii(s: str) -> str:
    return s.encode("ascii", "backslashreplace").decode("ascii")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="leeroy_mind health",
                                description="a read-only check of the live install: one ok, "
                                            "FAIL or note line per check, exit 1 on any FAIL")
    p.add_argument("--data-dir", default=None,
                   help="the mind's data dir (default: $LEEROY_MIND_DATA, else the mind-data "
                        "folder beside this code)")
    p.add_argument("--root", type=Path, default=None,
                   help="the install: its build/ (the servers and Server.log) and its mind and "
                        "voice venvs (default: this code's checkout)")
    p.add_argument("--json", action="store_true", help="print the checks as one JSON object")
    p.add_argument("--toast", action="store_true",
                   help=f"show a Windows toast for a new failure ({STATE_FILE} in the data dir "
                        "remembers the last one)")
    return p


def main(argv: list[str] | None = None, *, probes: Probes | None = None,
         show=show_toast) -> int:
    args = parser().parse_args(argv)
    data = (Path(args.data_dir) if args.data_dir else default_data_dir()).resolve()
    root = (args.root or CHECKOUT).resolve()
    checks = run_checks(root, data, probes=probes)
    if args.toast:
        alert(checks, data, show=show)
    failed = any(c.status == FAIL for c in checks)
    if args.json:
        print(json.dumps({"ok": not failed, "at": datetime.now().astimezone().isoformat(
            timespec="seconds"), "checks": [c.as_dict() for c in checks]}, ensure_ascii=True))
    else:
        for c in checks:
            print(f"{MARK[c.status]} {_ascii(c.text)}")
    return 1 if failed else 0


# -- the training lab ----------------------------------------------------------------------------

LAB_HALT_S = 24 * 3600  # a lab blocked for longer than one night has missed a night


def _training_lab(conn, now: float) -> Check:
    """A blocked dummy lab holds its members until the operator clears it on native evidence."""
    name = "training-lab"

    def check():
        row = conn.execute("SELECT value FROM meta WHERE key='tg:lab:owned'").fetchone()
        if not row:
            return Check(name, OK, "training lab: no lab record")
        try:
            owned = json.loads(row[0])
            if not isinstance(owned, dict):
                raise ValueError
            phase = owned.get("phase")
            if phase != "blocked":
                return Check(name, OK, "training lab: not blocked")
            since = owned.get("halted_at")
            if type(since) not in (int, float):
                since = owned.get("started")  # A record blocked before halted_at existed.
            if type(since) not in (int, float) or not math.isfinite(since):
                raise ValueError
            halt = owned.get("halt")
            code = halt if isinstance(halt, str) and _IDENT.fullmatch(halt) else "unknown_halt"
            night = owned.get("night")
            night = night if isinstance(night, str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", night) else "?"
            guids = owned.get("guids")
            held = len(guids) if isinstance(guids, list) else 0
            current = owned.get("current")
            trial = current.get("id") if isinstance(current, dict) else None
            trial = f", trial {trial}" if isinstance(trial, str) and re.fullmatch(r"[0-9a-f]{32}", trial) else ""
        except (ValueError, TypeError, RecursionError):
            return Check(name, FAIL, "training lab: invalid lab record; inspect it with training lab")
        text = (f"training lab: blocked since {_when(since)} (halt={code}, night {night}{trial}, "
                f"{held} members held)")
        if now - since <= LAB_HALT_S:
            return Check(name, NOTE, text)
        return Check(name, FAIL, text + "; once native's dummy_cleanup receipt or a worldserver restart is "
                     "recorded (training lab shows them), with the mind stopped: training lab-clear")
    return _table_check(name, check)
