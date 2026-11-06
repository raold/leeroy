"""`python -m leeroy_mind console`: build the console from the data dir and run it until
Ctrl+C. `python -m leeroy_mind console rotate-key --key-file PATH` writes a new stable key.

--allow-host NAME (repeatable, with [console] allow_hosts in mind.toml) also serves the console
at https://NAME/ through `tailscale serve` on this machine (app.py's gate); the console still
listens on 127.0.0.1 only. --key-file PATH uses the stable key in PATH (keyfile.py) instead of
a new one at each start. The key is printed in the start line's URLs (and rotate-key's) and
nowhere else."""
import argparse
import asyncio
import os
import re
import secrets
import sys
import tomllib
from pathlib import Path

from . import DEFAULT_PORT, VOICE_UI_PORT

CHECKOUT = Path(__file__).resolve().parents[3]  # mind/leeroy_mind/console/cli.py
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


def host_name(value) -> str | None:
    """value as an allowed tailnet name (lower case): a DNS name of two labels or more, with no
    port, scheme or path, that is not an IP address; None otherwise."""
    if not isinstance(value, str) or not value.isascii():
        return None
    name = value.lower()
    labels = name.split(".")
    if (len(name) > 253 or len(labels) < 2 or not all(_LABEL.fullmatch(x) for x in labels)
            or labels[-1].isdigit()):
        return None
    return name


def _absolute(path: Path) -> Path:
    """path made absolute without following a link, so the key file's own checks see it."""
    return Path(os.path.abspath(path))


def _allow_host(value: str) -> str:
    name = host_name(value)
    if name is None:
        raise argparse.ArgumentTypeError(
            f"{value!r} is not a host name (a tailnet name such as fractal.example.ts.net, "
            "with no scheme, port or path)")
    return name


def load_allow_hosts(path: Path) -> tuple[tuple[str, ...], str | None]:
    """[console] allow_hosts from the mind.toml at path: (names, None), or ((), why not). A
    missing file, table or key allows none; an unknown key in the table is a warning."""
    from ..config import log
    try:
        data = tomllib.loads(Path(path).read_bytes().decode("utf-8-sig"))
    except FileNotFoundError:
        return (), None
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        return (), f"mind.toml: cannot read [console] ({type(e).__name__})"
    table = data.get("console", {})
    if not isinstance(table, dict):
        return (), "mind.toml: console: expected a table"
    for key in table:
        if key != "allow_hosts":
            log.warning("%s: unknown key console.%s", path, key)
    values = table.get("allow_hosts", [])
    names = [host_name(v) for v in values] if isinstance(values, list) else [None]
    if None in names:
        return (), ("mind.toml: console.allow_hosts: expected a list of host names (no scheme, "
                    "port or path), so it allows none")
    return tuple(dict.fromkeys(names)), None


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
                   help="show status only: never start, restart or stop anything, send no "
                        "raid action, and turn the reply box off (no line is said in game)")
    p.add_argument("--voice-ui-port", type=int, default=None,
                   help="the voice service's page port (default: what `leeroy_voice where` "
                        f"says, else {VOICE_UI_PORT})")
    p.add_argument("--open", action="store_true", help="open the console in the browser")
    p.add_argument("--allow-host", action="append", default=[], type=_allow_host, metavar="NAME",
                   help="also serve it at https://NAME/ through `tailscale serve` on this "
                        "machine (repeatable; adds to [console] allow_hosts in mind.toml)")
    p.add_argument("--key-file", type=Path, default=None, metavar="PATH",
                   help="use the stable key in PATH, made private when missing, instead of a "
                        "new key at each start (`console rotate-key --key-file PATH` replaces it)")
    return p


def rotate_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="leeroy_mind console rotate-key",
                                description="write a new stable console key to --key-file and "
                                            "print the URLs to bookmark; a running console keeps "
                                            "its key until it restarts")
    p.add_argument("--key-file", type=Path, required=True, metavar="PATH")
    p.add_argument("--port", type=int, default=DEFAULT_PORT,
                   help=f"the console's port, for the printed URL (default {DEFAULT_PORT})")
    p.add_argument("--allow-host", action="append", default=[], type=_allow_host, metavar="NAME",
                   help="also print the tailnet URL for NAME (repeatable)")
    return p


def settings_from(data: Path, root: Path, port: int, key: str, voice_where=None,
                  voice_ui_port: int | None = None, allow_hosts=(), stable_key: bool = False):
    """Settings from mind.toml (read-only; a bad one leaves the defaults and says so)."""
    from ..config import ConfigError, load_config
    from ..voice.config import load_voice_config
    from .app import Settings
    listed, listed_problem = load_allow_hosts(data / "mind.toml")
    s = Settings(data_dir=data, root=root, port=port, key=key, stable_key=stable_key,
                 allow_hosts=tuple(dict.fromkeys([*allow_hosts, *listed])))
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
    if listed_problem is not None:
        s.config_problem = "; ".join(filter(None, [s.config_problem, listed_problem]))
    return s


def build(args, *, platform: str = sys.platform):
    """The app for parsed args: (ConsoleApp, problems), or (None, refusal)."""
    from .. import lifecycle
    from ..__main__ import _data_dir_problem
    from ..app import instance_lock_held
    from ..config import default_data_dir
    from .app import ConsoleApp
    from .control import Controls, MindHandle, VoiceHandle, control_problems
    from .mindlink import MindLink
    data = (Path(args.data_dir) if args.data_dir else default_data_dir()).resolve()
    root = (args.root or CHECKOUT).resolve()
    if not 0 < args.port < 65536 or (args.voice_ui_port is not None
                                       and not 0 < args.voice_ui_port < 65536):
        return None, "ports are 1-65535"
    problem, _ = _data_dir_problem(data)
    if problem:
        return None, (f"{problem}; pass --data-dir with the mind's data dir (or set "
                      "LEEROY_MIND_DATA)")
    if args.key_file is not None:
        from .keyfile import KeyFileError, load
        try:
            key = load(_absolute(args.key_file))
        except KeyFileError as e:
            return None, str(e)
    else:
        key = secrets.token_urlsafe(24)
    windows = platform == "win32"
    processes = lifecycle.WindowsProcesses() if windows else None
    voice = lifecycle.VoiceControl(root, processes=processes)
    where = None
    if voice.missing() is None:
        try:
            where = voice.where()
        except lifecycle.OwnershipError:
            where = None
    settings = settings_from(data, root, args.port, key, where, args.voice_ui_port,
                             allow_hosts=args.allow_host, stable_key=args.key_file is not None)
    link = MindLink(data)
    mind = MindHandle(root, data, python=lifecycle.venv_python(root),
                      base_python=getattr(sys, "_base_executable", sys.executable),
                      processes=processes, locked=instance_lock_held, link=link)
    problems = control_problems(root, platform=platform)
    controls = Controls(enabled=not args.no_control,
                        reason="process control is off (--no-control)" if args.no_control else "",
                        mind=mind, voice=VoiceHandle(voice), problems=problems)
    return ConsoleApp(settings, mind=mind, controls=controls, voice=voice, link=link), None


async def run(app, *, open_browser: bool = False) -> int:
    try:
        await app.start()
    except OSError as e:
        print(f"console: cannot listen on 127.0.0.1:{app.s.port} ({type(e).__name__}): another "
              "console, or another program, has it", file=sys.stderr)
        return 1
    control = app.controls.describe()
    state = "on" if control["enabled"] else "off"
    tailnet = "".join(f"\n  tailnet: {url}" for url in app.tailnet_urls)
    print(f"console: {app.url}{tailnet}\n  process control {state}; data dir {app.s.data_dir}; "
          "Ctrl+C stops the console (the mind and voice keep running)", flush=True)
    if open_browser:
        import webbrowser
        webbrowser.open(app.url)
    try:
        await asyncio.Event().wait()
    finally:
        await app.close()
    return 0


def rotate_main(argv: list[str]) -> int:
    from .keyfile import KeyFileError, rotate
    args = rotate_parser().parse_args(argv)
    if not 0 < args.port < 65536:
        print("console: ports are 1-65535", file=sys.stderr)
        return 1
    path = _absolute(args.key_file)
    try:
        key = rotate(path)
    except KeyFileError as e:
        print(f"console: {e}", file=sys.stderr)
        return 1
    tailnet = "".join(f"\n  tailnet: https://{name}/#key={key}"
                      for name in dict.fromkeys(args.allow_host))
    print(f"console: a new key is in {path}; a running console keeps its old key until it "
          f"restarts. Bookmark:\n  http://127.0.0.1:{args.port}/#key={key}{tailnet}", flush=True)
    return 0


def main(argv: list[str]) -> int:
    if argv[:1] == ["rotate-key"]:
        return rotate_main(argv[1:])
    args = parser().parse_args(argv)
    app, problem = build(args)
    if app is None:
        print(f"console: {problem}", file=sys.stderr)
        return 1
    try:
        return asyncio.run(run(app, open_browser=args.open))
    except KeyboardInterrupt:
        return 0
