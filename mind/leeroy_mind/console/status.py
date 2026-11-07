"""How the console tells the mind's state, the owner's game state and the machine's health.

The mind: its process family (leeroy_mind.lifecycle's identity rules) and mind.lock.
- running: the family is there and holds the lock;
- starting: the family is there without the lock yet (stopping, while the console stops it);
- stopped: neither, and the last run did not log a failure;
- crashed: neither, and the last run logged one (mind_failed, loop_stopped and the like), or a
  mind the console launched died of an exception status or a signal (an ordinary error code
  is only shown: ac-server.sh's stop terminates the mind with 1);
- unknown: the lock is held by a process that is not this checkout's mind (a night run or
  another checkout), or the processes could not be listed.

The owner's game state comes from what the running mind knows, and only from that: in game
while it has his session open (mind.db `session`, ended NULL) and its game bridge is up, not in
game while the bridge is up and no session is open. While the mind is not running, or its
bridge is down, or its session cannot be read, nobody knows: unknown, with the reason. Never
"not in game" for a mind that is merely down or a database that did not answer."""
import json
import shutil
import subprocess
import urllib.request
from dataclasses import asdict, dataclass

from . import netinfo

IN_GAME, NOT_IN_GAME, UNKNOWN = "in_game", "not_in_game", "unknown"
WORLD_PORT, AUTH_PORT = 8085, 3724  # the live confs' worldserver and authserver ports
OLLAMA_S = 3.0
GPU_S = 5.0
CRASH_STATUS = 0xC0000000  # an NTSTATUS error code as an exit code: an access violation, ...


@dataclass(frozen=True)
class MindView:
    state: str  # running, starting, stopping, stopped, crashed, unknown
    pid: int | None = None
    pids: tuple[int, ...] = ()
    lock: bool = False
    detail: str = ""

    def as_dict(self) -> dict:
        return {**asdict(self), "pids": list(self.pids)}


@dataclass(frozen=True)
class GameView:
    state: str  # in_game, not_in_game, unknown
    reason: str = ""
    since: float | None = None


def mind_view(found, held: bool, *, error: str | None = None, run_failed: bool = False,
              failure: dict | None = None, launch_exit: int | None = None,
              stopping: bool = False) -> MindView:
    """`found`: the family (lifecycle.family), None when the processes could not be listed;
    `error`: why the family is ambiguous (an OwnershipError's message)."""
    if error is not None:
        return MindView("unknown", lock=held, detail=error)
    if found is None:  # no process list: the lock alone
        if held:
            return MindView("running", lock=True, detail="process list unavailable")
        found = []
    pids = tuple(p.pid for p in found)
    pid = pids[0] if pids else None
    if found and held:
        return MindView("running", pid, pids, True)
    if found:
        return MindView("stopping" if stopping else "starting", pid, pids, False)
    if held:
        return MindView("unknown", lock=True, detail="mind.lock is held by a process that is "
                        "not this checkout's mind (a night run or another checkout)")
    if run_failed:
        event = (failure or {}).get("event", "a failure")
        when = (failure or {}).get("ts", "")
        return MindView("crashed", detail=f"its last run logged {event}"
                                          + (f" at {when}" if when else ""))
    if launch_exit is not None and not stopping:
        exited = f"the mind the console started exited with code {launch_exit}"
        # A Windows exception status, or a signal: it died. Any other code may be a stop
        # from elsewhere (ac-server.sh terminates it with 1), so it is only said.
        if launch_exit >= CRASH_STATUS or launch_exit < 0:
            return MindView("crashed", detail=exited)
        return MindView("stopped", detail=exited if launch_exit else "")
    return MindView("stopped")


def game_view(mind: str, bridge: bool | None, session: dict | None, *,
              unread: str | None = None) -> GameView:
    """`unread`: why the mind's session could not be read (an exception's class), if it could
    not; that is never "not in game"."""
    if mind != "running":
        return GameView(UNKNOWN, "the mind is not running" if mind in ("stopped", "crashed")
                        else f"the mind is {mind}")
    if bridge is not True:
        return GameView(UNKNOWN, "the mind's game bridge is down" if bridge is False
                        else "the mind's game bridge state is unknown")
    if unread is not None:
        return GameView(UNKNOWN, f"the mind's session could not be read ({unread})")
    if session is not None:
        return GameView(IN_GAME, since=session.get("started"))
    return GameView(NOT_IN_GAME)


def _fetch_json(url: str, timeout: float):
    # No proxy: this is the machine's own Ollama.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=timeout) as reply:
        return json.loads(reply.read())


def ollama_models(url: str, roles: dict[str, str], *, fetch=_fetch_json) -> dict:
    """The models Ollama has resident (GET /api/ps: read-only, it loads nothing), each with its
    role for the mind (live, embedding, reflection) when it is one of the configured ones."""
    try:
        reply = fetch(url.rstrip("/") + "/api/ps", OLLAMA_S)
        models = reply["models"]
        if not isinstance(models, list):
            raise TypeError("models")
    except (OSError, ValueError, KeyError, TypeError) as e:
        return {"ok": False, "url": url, "error": f"not reachable ({type(e).__name__})",
                "models": []}
    out = []
    for m in models:
        if not isinstance(m, dict):
            continue
        name = str(m.get("name", ""))
        role = next((r for r, n in roles.items() if n and (name == n or name == n + ":latest")),
                    None)
        out.append({"name": name, "size": m.get("size") if type(m.get("size")) is int else None,
                    "vram": m.get("size_vram") if type(m.get("size_vram")) is int else None,
                    "expires": m.get("expires_at") if isinstance(m.get("expires_at"), str)
                    else None, "role": role})
    return {"ok": True, "url": url, "models": out}


def gpu_headroom(*, which=shutil.which, run=subprocess.run) -> dict:
    """nvidia-smi's memory and use per GPU; ok False without nvidia-smi."""
    exe = which("nvidia-smi")
    if not exe:
        return {"ok": False, "error": "nvidia-smi not found", "gpus": []}
    try:
        result = run([exe, "--query-gpu=name,memory.total,memory.used,memory.free,utilization.gpu",
                      "--format=csv,noheader,nounits"], capture_output=True, text=True,
                     timeout=GPU_S, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError) as e:
        return {"ok": False, "error": f"nvidia-smi failed ({type(e).__name__})", "gpus": []}
    if result.returncode != 0:
        return {"ok": False, "error": f"nvidia-smi exited with code {result.returncode}",
                "gpus": []}
    gpus = []
    for line in (result.stdout or "").splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 5:
            continue
        try:
            total, used, free, util = (int(float(p)) for p in parts[1:])
        except ValueError:
            continue
        gpus.append({"name": parts[0][:64], "total_mb": total, "used_mb": used,
                     "free_mb": free, "util": util})
    return {"ok": bool(gpus), "gpus": gpus, **({} if gpus else {"error": "no GPU listed"})}


def server_view(conns, *, bridge_port: int) -> dict:
    """Whether the game's worldserver, authserver and mod-leeroy's bridge listen, from the
    machine's TCP table; None for each when the table could not be read."""
    return {"world": netinfo.listening(conns, WORLD_PORT),
            "auth": netinfo.listening(conns, AUTH_PORT),
            "bridge": netinfo.listening(conns, bridge_port)}
