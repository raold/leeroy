"""python -m leeroy_dojo: protocol-smoke and verify-episode."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .recording import verify_episode
from .smoke import run_fake_smoke


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m leeroy_dojo")
    sub = parser.add_subparsers(dest="command", required=True)
    smoke = sub.add_parser("protocol-smoke", help="run the worker contract end to end and record its episodes")
    smoke.add_argument("--fake", action="store_true", help="use the in-process fake worker (the only one so far)")
    smoke.add_argument("--out", type=Path, help="output directory (default: a new temporary directory)")
    smoke.add_argument("--seed", type=int, default=7, help="scenario seed of the replayed episode")
    smoke.add_argument("--json", action="store_true", help="print the report as JSON")
    verify = sub.add_parser("verify-episode", help="check a recorded episode's chunks, checksums and footer")
    verify.add_argument("path", type=Path)
    verify.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.command == "protocol-smoke":
        if not args.fake:
            print("protocol-smoke: only --fake exists until the native worker is built", file=sys.stderr)
            return 2
        report = run_fake_smoke(args.out, seed=args.seed)
        if args.json:
            print(json.dumps(report.to_json(), indent=2))
        else:
            for c in report.checks:
                print(f"{c['status']:<4} {c['check']}: {c['detail']}")
            print(f"protocol-smoke: {'ok' if report.ok else 'FAILED'} ({report.out})")
        return 0 if report.ok else 1

    check = verify_episode(args.path)
    if args.json:
        print(json.dumps({"path": str(check.path), "complete": check.complete, "usable": check.usable,
                          "outcome": check.outcome, "transitions": check.transitions,
                          "valid_chunks": check.valid_chunks, "problems": check.problems}, indent=2))
    else:
        for problem in check.problems:
            print(f"FAIL {problem}")
        state = "usable" if check.usable else "complete, not usable" if check.complete else "incomplete"
        print(f"{check.path}: {state}, {check.transitions} records in {check.valid_chunks} chunks, "
              f"outcome {check.outcome}")
    return 0 if check.usable else 1


if __name__ == "__main__":
    sys.exit(main())
