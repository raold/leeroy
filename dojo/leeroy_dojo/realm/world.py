"""Start, check and stop the dojo worldserver: staged binary, instance configs, loopback ports, dojo MySQL only."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from . import mysql
from .layout import Layout, LayoutError, norm
from .provision import ra_credentials
from .ra import RaClient, RaError

PREFLIGHT = "scripts/dojo/preflight.ps1"


class WorldError(Exception):
    pass


def child_env() -> dict[str, str]:
    """This environment without AzerothCore's AC_* overrides (which would beat the instance's configs)."""
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("AC_")}
    env["AC_DISABLE_INTERACTIVE"] = "1"
    return env


def preflight(layout: Layout) -> dict:
    """Runs scripts/dojo/preflight.ps1 on the layout; raises unless it passes."""
    script = Path(layout.roots["source"]) / PREFLIGHT
    result = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(script), "-Layout", layout.path,
                             "-Json"], capture_output=True, text=True, encoding="utf-8", timeout=600)
    try:
        report = json.loads(result.stdout)
    except ValueError:
        raise WorldError(f"preflight produced no report (exit {result.returncode})") from None
    if result.returncode != 0 or not report.get("ok"):
        fails = [f"{c['check']}: {c['detail']}" for c in report.get("checks", []) if c.get("status") == "FAIL"]
        raise WorldError("preflight did not pass: " + "; ".join(fails[:10]))
    return report


def worldserver_args(layout: Layout, dry_run: bool = False) -> list[str]:
    exe = layout.bin / "worldserver.exe"
    if not exe.is_file():
        raise WorldError(f"no staged {exe}; run realm provision first")
    conf = layout.configs / "worldserver.conf"
    if not conf.is_file():
        raise WorldError(f"no {conf}; run realm provision first")
    args = [str(exe), "-c", str(conf)]
    if dry_run:
        args.append("--dry-run")
    return args


def _ports_free(layout: Layout) -> None:
    for name in ("world", "ra"):
        port = layout.ports[name]
        layout.check_port(port, name)
        if mysql.port_listening(port):
            raise WorldError(f"{name} port {port} is already in use")


def _require_mysql(layout: Layout) -> None:
    if not mysql.port_listening(layout.server.port):
        raise WorldError("the dojo MySQL instance is not running; run realm db-start")
    mysql.verify_identity(layout, mysql.Client(layout, layout.server.root_cnf))


def clean_tmp(layout: Layout) -> None:
    """AzerothCore's updater leaves its mysql option file (with the dojo password) in TempDir."""
    (layout.tmp / "mysql_ac.conf").unlink(missing_ok=True)


def dry_run(layout: Layout, timeout: float = 3600, log=print) -> int:
    """Builds or updates the dojo schemas with the core's own auto-setup, loads the world once and exits."""
    preflight(layout)
    _require_mysql(layout)
    _ports_free(layout)
    stdout = layout.logs / f"worldserver-dry-run-{time.strftime('%Y%m%d_%H%M%S')}.log"
    layout.logs.mkdir(parents=True, exist_ok=True)
    log(f"worldserver --dry-run (auto-setup from {layout.roots['core']}); output -> {norm(stdout)}")
    flags = 0x08000000 | 0x00004000 if os.name == "nt" else 0  # no window, below-normal priority
    try:
        with open(stdout, "wb") as out:
            result = subprocess.run(worldserver_args(layout, dry_run=True), cwd=layout.runtime, env=child_env(),
                                    stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                    timeout=timeout, creationflags=flags)
    finally:
        clean_tmp(layout)
    verdict = dry_run_verdict(layout.logs / "Server.log", result.returncode)
    log(f"worldserver --dry-run exited {result.returncode}: {verdict[1]}")
    return 0 if verdict[0] else 1


DRY_RUN_DONE = "AzerothCore Dry Run Completed, Terminating."
DRY_RUN_FAILED = "Dry Run Completed With"


def dry_run_verdict(server_log: Path, code: int) -> tuple[bool, str]:
    """The server's own completion line decides: worldserver calls exit() from inside its world thread after a dry
    run, and on Windows the static teardown that follows can fault (0xC0000005) after every update has applied."""
    try:
        text = server_log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    if DRY_RUN_FAILED in text:
        return False, "the dry run reported failed database updates"
    if DRY_RUN_DONE in text:
        if code == 0:
            return True, "completed"
        return True, f"completed (the process then faulted at exit, code {code:#x})"
    return False, "no completion line in Server.log"


def pid(layout: Layout) -> int:
    try:
        return int((layout.run_dir / "worldserver.pid").read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return 0


def running(layout: Layout) -> bool:
    return mysql.port_listening(layout.ports["ra"]) or mysql.pid_alive(pid(layout))


def start(layout: Layout, timeout: float = 900, log=print) -> int:
    preflight(layout)
    _require_mysql(layout)
    _ports_free(layout)
    stdout = layout.logs / f"worldserver-{time.strftime('%Y%m%d_%H%M%S')}.out"
    p = mysql.spawn_detached(worldserver_args(layout), cwd=layout.runtime, stdout=stdout, env=child_env())
    log(f"started worldserver pid {p}; waiting for RA on 127.0.0.1:{layout.ports['ra']}")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if mysql.port_listening(layout.ports["ra"]):
            try:
                with ra(layout) as client:
                    client.command("server info")
                log("worldserver is up")
                return p
            except (RaError, OSError):
                pass
        if not mysql.pid_alive(p):
            clean_tmp(layout)
            raise WorldError(f"worldserver exited during start; see {norm(stdout)} and {norm(layout.logs)}")
        time.sleep(2)
    raise WorldError(f"worldserver did not open RA within {timeout:.0f} s")


def ra(layout: Layout) -> RaClient:
    user, password = ra_credentials(layout)
    return RaClient(layout.ports["ra"], user, password)


def stop(layout: Layout, timeout: float = 180, log=print) -> None:
    """Asks the worldserver to shut down through RA (saving players), then waits for the process to exit."""
    p = pid(layout)
    if not mysql.port_listening(layout.ports["ra"]):
        if mysql.pid_alive(p):
            raise WorldError(f"worldserver pid {p} runs but RA is closed; stop it by hand")
        log("not running")
        clean_tmp(layout)
        return
    with ra(layout) as client:
        log(client.command("server shutdown 1") or "shutdown requested")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not mysql.pid_alive(p) and not mysql.port_listening(layout.ports["ra"]):
            clean_tmp(layout)
            log("worldserver stopped")
            return
        time.sleep(1)
    raise WorldError(f"worldserver pid {p} still running {timeout:.0f} s after shutdown")


def check_layout_ports(layout: Layout) -> None:
    for name in ("world", "ra"):
        if name not in layout.ports or not layout.ports[name]:
            raise LayoutError(f"the layout needs ports.{name}")
