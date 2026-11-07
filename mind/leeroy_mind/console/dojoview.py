"""The Dojo tab's read-only question to the dojo's own files (not mind.db). Only regular files
whose resolved path lies under the layout's data or runtime root are read, each capped in size;
a file that is missing, huge or unparsable becomes a row's `why`, never an error. Nothing is
written, no socket is opened, nothing is started. From the layout only format, marker, instance,
roots.data, roots.runtime, provenance and ports.world are read; never `database`, secrets,
configs or logs."""
import json
import os
import re
import shutil
import stat
import subprocess
import time
from pathlib import Path

from .netinfo import LISTEN

MARKER = "leeroy-dojo-training"
MAX_JSON = 4 * 1024 * 1024
MAX_TEXT = 64 * 1024
RUNS = 30
EPISODES = 60
CACHE_S = 15.0
GIT_S = 5.0
GATES = ("G0", "G1", "G2", "G3", "G4", "G5")
RUN_REPORTS = {"realm-acceptance": "acceptance.json", "control-acceptance": "report.json",
               "reset-acceptance": "report.json"}
LATER = {"policies": "not yet", "curves": "not yet", "live_adapter": "not yet"}
NOT_DOJO = "that file is not a dojo training layout"
NO_REPORT, UNREADABLE, TOO_LARGE = "no report", "unreadable", "too large to read"
RUN_NAME = re.compile(r"^(\d{8}_\d{6})-([A-Za-z0-9][A-Za-z0-9-]*)$")
HEAD = re.compile(r"^[0-9a-f]{7,64}$")
SAFE_NAME = re.compile(r"^[\w.-]{1,200}$")
BASELINES = ("install_root", "core", "playerbots")

_CACHE: dict = {}


def report_name(kind: str) -> str | None:
    if kind in RUN_REPORTS:
        return RUN_REPORTS[kind]
    m = re.fullmatch(r"g(\d+)", kind)
    return f"g{m.group(1)}-report.json" if m else None


def run_git(path, *args) -> str | None:
    """`git -C <path> ...` read-only; its output, or None on any failure or without git."""
    exe = shutil.which("git")
    if exe is None:
        return None
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
    try:
        r = subprocess.run([exe, "-c", "core.fsmonitor=false", "-C", str(path), *args],
                           capture_output=True, text=True, timeout=GIT_S, env=env,
                           stdin=subprocess.DEVNULL,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def _s(v, n=200) -> str:
    return v[:n] if isinstance(v, str) else ""


def _inside(path: Path, roots) -> bool:
    try:
        real = path.resolve()
    except (OSError, RuntimeError):
        return False
    for root in roots:
        try:
            real.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _read_bytes(path: Path, roots, limit: int):
    """(bytes, "") or (None, why)."""
    try:
        st = path.lstat()
    except FileNotFoundError:
        return None, NO_REPORT
    except OSError:
        return None, UNREADABLE
    if not stat.S_ISREG(st.st_mode) or not _inside(path, roots):
        return None, UNREADABLE
    if st.st_size > limit:
        return None, TOO_LARGE
    try:
        with open(path, "rb") as f:
            raw = f.read(limit + 1)
    except OSError:
        return None, UNREADABLE
    if len(raw) > limit:
        return None, TOO_LARGE
    return raw, ""


def _read_json(path: Path, roots):
    raw, why = _read_bytes(path, roots, MAX_JSON)
    if raw is None:
        return None, why
    try:
        value = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):
        return None, UNREADABLE
    if not isinstance(value, dict):
        return None, UNREADABLE
    return value, ""


def _abs(v) -> Path | None:
    if not isinstance(v, str) or not v or len(v) > 1024 or "\0" in v:
        return None
    p = Path(v)
    return p if p.is_absolute() else None


def _layout(layout_path):
    """(layout dict, "") or (None, why)."""
    if layout_path is None:
        return None, "no layout is configured"
    path = Path(layout_path)
    try:
        path.lstat()
    except OSError:
        return None, f"no layout at {path}"
    raw, why = _read_bytes(path, [path.resolve().parent], MAX_JSON)
    if raw is None:
        return None, NOT_DOJO
    try:
        lay = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):
        return None, NOT_DOJO
    if not isinstance(lay, dict) or lay.get("format") != 1 or lay.get("marker") != MARKER:
        return None, NOT_DOJO
    roots = lay.get("roots")
    roots = roots if isinstance(roots, dict) else {}
    data, runtime = _abs(roots.get("data")), _abs(roots.get("runtime"))
    prov = _abs(lay.get("provenance"))
    ports = lay.get("ports")
    world = ports.get("world") if isinstance(ports, dict) else None
    if (data is None or runtime is None or prov is None or not isinstance(lay.get("instance"), str)
            or type(world) is not int or not 0 < world < 65536):
        return None, NOT_DOJO
    if ".." in prov.parts or not _inside(prov, [data.resolve()]):
        return None, NOT_DOJO
    if not data.is_dir():
        return None, "the layout's data root is missing"
    return {"instance": _s(lay["instance"], 64), "data": data.resolve(), "runtime": runtime.resolve(),
            "provenance": prov, "world": world}, ""


def _stamp_runs(data: Path, roots):
    """Run folders newest first: (stamp, kind, path). Links and files are skipped."""
    folder = data / "runs"
    out = []
    try:
        with os.scandir(folder) as it:
            for e in it:
                m = RUN_NAME.match(e.name)
                if not m:
                    continue
                try:
                    if e.is_symlink() or (hasattr(e, "is_junction") and e.is_junction()) or not e.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                p = Path(e.path)
                if _inside(p, roots):
                    out.append((m.group(1), m.group(2), p))
    except OSError:
        return []
    out.sort(key=lambda r: (r[0], r[1]), reverse=True)
    return out


def _count(v) -> int:
    if isinstance(v, list):
        return len(v)
    return v if type(v) is int and v >= 0 else 0


def _build(data: Path, roots):
    folder = data / "builds"
    names = []
    try:
        with os.scandir(folder) as it:
            names = [e.name for e in it if re.fullmatch(r"build-[\w.-]+\.json", e.name)]
    except OSError:
        return None
    if not names:
        return None
    name = max(names)
    rec, why = _read_json(folder / name, roots)
    if rec is None:
        return {"file": name, "why": why, "ok": False}
    verified = rec.get("verified")
    verified = verified if isinstance(verified, dict) else {}
    problems = rec.get("problems")
    problems = [_s(p, 200) for p in problems[:10]] if isinstance(problems, list) else []
    passed = sum(1 for x in verified.values() if x is True)
    return {"file": name, "why": "", "variant": _s(rec.get("variant"), 40),
            "created_utc": _s(rec.get("created_utc"), 40), "exit_code": rec.get("exit_code") if type(rec.get("exit_code")) is int else None,
            "compiler_errors": _count(rec.get("compiler_errors")), "problems": problems,
            "verified": {"passed": passed, "total": len(verified)},
            "core": _s(rec.get("core"), 64), "playerbots": _s(rec.get("playerbots"), 64),
            "ok": rec.get("exit_code") == 0 and not problems and bool(verified) and passed == len(verified)}


def _moved(path, head, git) -> tuple[str | None, str]:
    folder = _abs(path)
    if folder is None or not isinstance(head, str) or not HEAD.fullmatch(head) or not folder.is_dir():
        return None, "unknown"
    now = git(folder, "rev-parse", "--verify", "HEAD")
    if not isinstance(now, str) or not HEAD.fullmatch(now):
        return None, "unknown"
    if now == head or (len(head) >= 7 and (now.startswith(head) or head.startswith(now))):
        return now, "same"
    if git(folder, "merge-base", "--is-ancestor", head, "HEAD") is not None:
        return now, "past"
    if git(folder, "cat-file", "-e", head + "^{commit}") is not None:
        return now, "diverged"
    return now, "unknown"


def _provenance(lay, roots, git):
    rec, why = _read_json(lay["provenance"], roots)
    if rec is None:
        return None
    base = rec.get("baseline")
    base = base if isinstance(base, dict) else {}
    rows = []
    for name in BASELINES:
        b = base.get(name)
        if not isinstance(b, dict):
            continue
        path, head = _s(b.get("path"), 500), _s(b.get("head"), 64)
        now, moved = _moved(path, head, git)
        rows.append({"name": name, "path": path, "head": head, "now": now or "", "moved": moved})
    return {"created_utc": _s(rec.get("created_utc"), 40), "baselines": rows}


def _episodes(report, data: Path, run: Path, roots):
    out = []
    items = report.get("episodes")
    for e in (items[:EPISODES] if isinstance(items, list) else []):
        if not isinstance(e, dict):
            continue
        name = _s(e.get("name"), 80)
        outcome = e.get("outcome")
        reason = _s(outcome.get("reason"), 80) if isinstance(outcome, dict) else _s(outcome, 80)
        winner = outcome.get("winner") if isinstance(outcome, dict) else e.get("winner")
        replay = e.get("replay")
        if not isinstance(replay, dict):
            ep = e.get("episode")
            replay = None
            if isinstance(ep, str) and SAFE_NAME.fullmatch(ep) and not e.get("stock"):
                replay, _ = _read_json(run / "replays" / f"{ep}.json", roots)
        pol = e.get("policies")
        out.append({"name": name, "policies": [_s(p, 40) for p in pol[:4]] if isinstance(pol, list) else [],
                    "outcome": reason, "winner": winner if type(winner) is int else None,
                    "replay_ok": replay.get("ok") if isinstance(replay, dict) and isinstance(replay.get("ok"), bool) else None,
                    "usable": replay.get("usable") if isinstance(replay, dict) and isinstance(replay.get("usable"), bool) else None})
    return out


def _files(lay, git):
    data, runtime = lay["data"], lay["runtime"]
    roots = [data, runtime]
    reports: dict = {}

    def report_of(run):
        stamp, kind, path = run
        key = (stamp, kind)
        if key not in reports:
            fname = report_name(kind)
            reports[key] = (None, "") if fname is None else _read_json(path / fname, roots)
        return reports[key]

    all_runs = _stamp_runs(data, roots)

    def newest(kind):
        return next((r for r in all_runs if r[1] == kind), None)

    rows = []
    for run in all_runs[:RUNS]:
        stamp, kind, path = run
        rep, why = report_of(run)
        row = {"stamp": stamp, "kind": kind, "ok": None, "report": report_name(kind), "why": why,
               "episodes": []}
        if rep is not None:
            row["ok"] = rep.get("ok") if isinstance(rep.get("ok"), bool) else None
            if kind == "g1":
                row["episodes"] = _episodes(rep, data, path, roots)
        rows.append(row)

    build = _build(data, roots)
    prov = _provenance(lay, roots, git)

    def acc(kind, label):
        run = newest(kind)
        if run is None:
            return f"{label}: none"
        rep, _ = report_of(run)
        ok = rep.get("ok") if rep else None
        return f"{label}: {run[0]} {'ok' if ok is True else 'failed' if ok is False else 'no report'}"

    gates = []
    if prov is not None and build is not None and build.get("ok"):
        gates.append({"gate": "G0", "state": "evidence", "run": "",
                      "detail": "the provenance manifest and a clean build receipt; the dojo writes no G0 report"})
    else:
        gates.append({"gate": "G0", "state": "not yet", "run": "",
                      "detail": "needs the provenance manifest and a clean newest build receipt"})
    g1 = newest("g1")
    g1rep = report_of(g1)[0] if g1 else None
    detail = "; ".join([acc("realm-acceptance", "realm acceptance"), acc("control-acceptance", "control acceptance"),
                        acc("reset-acceptance", "reset acceptance")])
    if g1 is None:
        gates.append({"gate": "G1", "state": "not yet", "run": "", "detail": "no G1 run; " + detail})
    elif g1rep is None or not isinstance(g1rep.get("ok"), bool):
        gates.append({"gate": "G1", "state": "not yet", "run": g1[0], "detail": "no readable report; " + detail})
    else:
        gates.append({"gate": "G1", "state": "passed" if g1rep["ok"] else "failed", "run": g1[0], "detail": detail})
    for n in range(2, 6):
        state, run_stamp, why = "not yet", "", "no G%d run" % n
        for run in (r for r in all_runs if r[1] == f"g{n}"):
            rep, w = report_of(run)
            if rep is not None and rep.get("gate") == f"G{n}" and isinstance(rep.get("ok"), bool):
                state, run_stamp, why = ("passed" if rep["ok"] else "failed"), run[0], ""
                break
        gates.append({"gate": f"G{n}", "state": state, "run": run_stamp, "detail": why})

    scenario = None
    if g1rep is not None:
        sc = g1rep.get("inputs")
        sc = sc.get("scenario") if isinstance(sc, dict) else None
        if isinstance(sc, dict):
            scenario = {"id": _s(sc.get("id"), 120), "sha256": _s(sc.get("sha256"), 64)}
    md, md_why = None, ""
    if g1 is not None:
        raw, md_why = _read_bytes(g1[2] / "G1.md", roots, MAX_TEXT)
        if raw is not None:
            md = raw.decode("utf-8", "replace")
    return {"build": build, "provenance": prov, "gates": gates, "runs": rows, "scenario": scenario,
            "g1_md": md, "g1_md_why": md_why if md is None else "", "roots": roots}


def _listening(conns, pid, port) -> bool:
    return any(c.state == LISTEN and c.local_port == port and c.pid == pid for c in conns)


def _realm(lay, conns, roots, scenario):
    run = lay["runtime"] / "run"
    raw, _ = _read_bytes(run / "worldserver.pid", roots, 64)
    pid = None
    if raw is not None:
        try:
            pid = int(raw.decode("ascii", "replace").strip())
        except ValueError:
            pid = None
        if pid is not None and pid <= 0:
            pid = None
    rec, _ = _read_json(run / "dojo-endpoint.json", roots)
    endpoint = None
    if rec is not None:
        epid, eport = rec.get("pid"), rec.get("port")
        epid = epid if type(epid) is int else None
        eport = eport if type(eport) is int else None
        endpoint = {"pid": epid, "port": eport, "scenario": _s(rec.get("scenario"), 120),
                    "sha256": _s(rec.get("scenario_sha256"), 64),
                    "up": bool(conns is not None and epid is not None and eport is not None
                               and _listening(conns, epid, eport))}
    if conns is None:
        state = "unknown"
    else:
        state = "running" if pid is not None and _listening(conns, pid, lay["world"]) else "stopped"
    return {"state": state, "pid": pid, "port": lay["world"], "endpoint": endpoint}


def dojo_view(layout_path, *, conns, git=run_git, cache=None, clock=time.monotonic) -> dict:
    """The tab's view. The files and git are read at most every CACHE_S; the realm's running state
    comes from `conns` (the console's TCP table, or None) at every call."""
    cache = _CACHE if cache is None else cache
    lay, why = _layout(layout_path)
    if lay is None:
        return {"setup": False, "why": why}
    key = str(layout_path)
    hit = cache.get(key)
    now = clock()
    if hit is None or now - hit[0] >= CACHE_S:
        hit = (now, _files(lay, git))
        cache[key] = hit
    f = hit[1]
    out = {k: f[k] for k in ("build", "provenance", "gates", "runs", "scenario", "g1_md", "g1_md_why")}
    out["setup"] = True
    out["instance"] = lay["instance"]
    out["realm"] = _realm(lay, conns, f["roots"], f["scenario"])
    out["later"] = dict(LATER)
    return out
