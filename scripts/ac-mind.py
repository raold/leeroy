"""Windows mind lifecycle helper for ac-server.sh; never selects Python by name alone.

Stopping a console service is forced, as for the server script's existing console
apps. Captured process handles and creation times prevent PID reuse from targeting
another process. An unidentified held lock always requires manual intervention.

The identity rules and the actions live in leeroy_mind.lifecycle, which the guild
console uses too; this script is their command line.
"""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mind"))

try:
    from leeroy_mind.lifecycle import (  # this checkout's own module
        OwnershipError, Outcome, Process, WindowsProcesses, canonical, family, launch_problem,
        matches, mind_action, run_action, spawn_mind)
except Exception as exc:  # main() reports it in one line, as it reports every other failure
    _UNLOADED = exc
else:
    _UNLOADED = None

__all__ = ["OwnershipError", "Outcome", "Process", "WindowsProcesses", "canonical", "family",
           "launch_problem", "matches", "mind_action", "run_action", "spawn_mind", "main"]


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("start", "stop", "status"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    if _UNLOADED is not None:
        print(f"mind: {type(_UNLOADED).__name__}; no unverified process was stopped",
              file=sys.stderr)
        return 1
    if os.name != "nt":
        parser.error("mind process control is supported only on Windows")
    root, data = args.root.resolve(), args.data_dir.resolve()
    python = root / "mind" / ".venv" / "Scripts" / "python.exe"
    if canonical(sys.executable) != canonical(python):
        print("mind: helper must use this checkout's mind venv", file=sys.stderr)
        return 1
    sys.path.insert(0, str(root / "mind"))
    try:
        from leeroy_mind.app import instance_lock_held
        return run_action(args.action, root, data, python, sys._base_executable,
                          processes=WindowsProcesses(), locked=instance_lock_held)
    except OwnershipError as exc:
        print(f"mind: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        # CIM failures can include whole command lines. Never print their raw payload.
        print(f"mind: {type(exc).__name__}; no unverified process was stopped", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
