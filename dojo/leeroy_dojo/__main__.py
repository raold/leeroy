"""python -m leeroy_dojo: protocol-smoke, verify-episode, scenario and realm."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .realm import cli as realm_cli
from .recording import verify_episode
from .scenario import ScenarioError, load_manifest
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
    scenario = sub.add_parser("scenario", help="check a scenario manifest and print or pin its SHA-256")
    scenario.add_argument("action", choices=("check", "hash", "pin"),
                          help="check: structure only; hash: print the hash; pin: write <name>.sha256 next to it")
    scenario.add_argument("path", type=Path)
    realm_cli.add_parser(sub)
    args = parser.parse_args(argv)

    if args.command == "realm":
        return realm_cli.run(args)
    if args.command == "scenario":
        try:
            manifest = load_manifest(args.path)
        except (OSError, ScenarioError) as e:
            print(f"scenario: {e}", file=sys.stderr)
            return 1
        if args.action == "pin":
            args.path.with_suffix(".sha256").write_text(f"{manifest.sha256}  {args.path.name}\n", encoding="ascii",
                                                        newline="\n")
        print(manifest.sha256 if args.action != "check" else f"{args.path}: ok ({manifest.id})")
        return 0
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
