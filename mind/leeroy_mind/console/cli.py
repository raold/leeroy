"""`python -m leeroy_mind console`: build the console from the data dir and run it until
Ctrl+C."""
import argparse
import asyncio
import secrets
import sys
from pathlib import Path

from . import DEFAULT_PORT, VOICE_UI_PORT

CHECKOUT = Path(__file__).resolve().parents[3]  # mind/leeroy_mind/console/cli.py


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="leeroy_mind console",
                                description="the guild console on http://127.0.0.1:<port>/")
    p.add_argument("--data-dir", default=None,
                   help="the mind's data dir (default: $LEEROY_MIND_DATA, else the mind-data "
                        "folder beside this code)")
    p.add_argument("--port", type=int, default=DEFAULT_PORT,
                   help=f"the console's port on 127.0.0.1 (default {DEFAULT_PORT})")
    p.add_argument("--root", type=Path, default=None,
                   help="the checkout whose mind and voice venvs it controls and recognises "
                        "(default: this code's checkout)")
    p.add_argument("--no-control", action="store_true",
                   help="show status only: never start, restart or stop anything")
    p.add_argument("--voice-ui-port", type=int, default=None,
                   help="the voice service's page port (default: what `leeroy_voice where` "
                        f"says, else {VOICE_UI_PORT})")
    p.add_argument("--open", action="store_true", help="open the console in the browser")
    return p


def settings_from(data: Path, root: Path, port: int, key: str, voice_where=None,
                  voice_ui_port: int | None = None):
    """Settings from mind.toml (read-only; a bad one leaves the defaults and says so)."""
    from ..config import ConfigError, load_config
    from ..voice.config import load_voice_config
    from .app import Settings
    s = Settings(data_dir=data, root=root, port=port, key=key)
    try:
        cfg = load_config(data / "mind.toml")
    except ConfigError as e:
        s.config_problem = f"mind.toml: {e}"
    else:
        s.bridge_host, s.bridge_port = cfg.bridge.host, cfg.bridge.port
        s.ollama_url = cfg.ollama.url
        s.roles = {"live": cfg.models.live, "embedding": cfg.models.embedding,
                   "reflection": cfg.models.reflection}
    try:
        s.voice_enabled = load_voice_config(data / "mind.toml").enabled
    except (ConfigError, TypeError):
        s.voice_enabled = False
    if voice_where is not None:
        s.voice_ui_port, s.voice_mind_port = voice_where.ui_port, voice_where.mind_port
        s.voice_data_dir = voice_where.data_dir
    if voice_ui_port is not None:
        s.voice_ui_port = voice_ui_port
    return s


def build(args, *, platform: str = sys.platform):
    """The app for parsed args: (ConsoleApp, problems), or (None, refusal)."""
    from .. import lifecycle
    from ..__main__ import _data_dir_problem
    from ..app import instance_lock_held
    from ..config import default_data_dir
    from .app import ConsoleApp
    from .control import Controls, MindHandle, VoiceHandle, control_problems
    data = (Path(args.data_dir) if args.data_dir else default_data_dir()).resolve()
    root = (args.root or CHECKOUT).resolve()
    if not 0 < args.port < 65536 or (args.voice_ui_port is not None
                                       and not 0 < args.voice_ui_port < 65536):
        return None, "ports are 1-65535"
    problem, _ = _data_dir_problem(data)
    if problem:
        return None, problem
    windows = platform == "win32"
    processes = lifecycle.WindowsProcesses() if windows else None
    voice = lifecycle.VoiceControl(root, processes=processes)
    where = None
    if voice.missing() is None:
        try:
            where = voice.where()
        except lifecycle.OwnershipError:
            where = None
    settings = settings_from(data, root, args.port, secrets.token_urlsafe(24), where,
                             args.voice_ui_port)
    mind = MindHandle(root, data, python=lifecycle.venv_python(root),
                      base_python=getattr(sys, "_base_executable", sys.executable),
                      processes=processes, locked=instance_lock_held)
    problems = control_problems(root, platform=platform)
    controls = Controls(enabled=not args.no_control,
                        reason="process control is off (--no-control)" if args.no_control else "",
                        mind=mind, voice=VoiceHandle(voice), problems=problems)
    return ConsoleApp(settings, mind=mind, controls=controls, voice=voice), None


async def run(app, *, open_browser: bool = False) -> int:
    try:
        await app.start()
    except OSError as e:
        print(f"console: cannot listen on 127.0.0.1:{app.s.port} ({type(e).__name__}): another "
              "console, or another program, has it", file=sys.stderr)
        return 1
    control = app.controls.describe()
    state = "on" if control["enabled"] else "off"
    print(f"console: {app.url}\n  process control {state}; data dir {app.s.data_dir}; "
          "Ctrl+C stops the console (the mind and voice keep running)", flush=True)
    if open_browser:
        import webbrowser
        webbrowser.open(app.url)
    try:
        await asyncio.Event().wait()
    finally:
        await app.close()
    return 0


def main(argv: list[str]) -> int:
    args = parser().parse_args(argv)
    app, problem = build(args)
    if app is None:
        print(f"console: {problem}; pass --data-dir with the mind's data dir (or set "
              "LEEROY_MIND_DATA)", file=sys.stderr)
        return 1
    try:
        return asyncio.run(run(app, open_browser=args.open))
    except KeyboardInterrupt:
        return 0
