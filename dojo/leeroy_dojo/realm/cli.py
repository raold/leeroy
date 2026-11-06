"""python -m leeroy_dojo realm --layout <layout.json> <command>: the dojo's training realm."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import mysql, provision, world
from .layout import LayoutError, load
from .ra import RaError


def add_parser(sub) -> None:
    realm = sub.add_parser("realm", help="provision and run the dojo's disposable training realm")
    realm.add_argument("--layout", required=True, help="the dojo layout file")
    cmds = realm.add_subparsers(dest="realm_command", required=True)
    cmds.add_parser("db-init", help="create the dojo MySQL instance: my.ini, credentials, a fresh data folder")
    cmds.add_parser("db-start", help="start the dojo MySQL instance (a plain hidden process)")
    cmds.add_parser("db-stop", help="shut the dojo MySQL instance down cleanly")
    cmds.add_parser("db-status", help="report whether the dojo MySQL instance runs and is the dojo's")
    cmds.add_parser("db-check", help="check the instance's identity, schema set and the restricted user's grants")
    acc_run = cmds.add_parser("acceptance", help="open the training arena, verify both actors, close it, compare")
    acc_run.add_argument("--scenario", type=Path, required=True)
    acc_run.add_argument("--keep-running", action="store_true", help="leave a worldserver this command started")
    ctl = cmds.add_parser("control-acceptance",
                          help="exclusive control and ordinary execution in the training arena, then shutdown while owned")
    ctl.add_argument("--scenario", type=Path, required=True)
    rst = cmds.add_parser("reset-acceptance",
                          help="consecutive episode resets from every condition, against a restart-based reference")
    rst.add_argument("--scenario", type=Path, required=True)
    rst.add_argument("--resets", type=int, default=100, help="how many resets (default 100)")
    rst.add_argument("--cases", help="a comma-separated trial list of conditions instead of the schedule")
    g1 = cmds.add_parser("g1", help="scripted diagnostic episodes through the endpoint, the stock baseline and the G1 report")
    g1.add_argument("--scenario", type=Path, required=True)
    g1.add_argument("--quick", action="store_true", help="one episode of each kind instead of the whole plan")
    g1.add_argument("--reset-run", type=Path, help="the reset-acceptance run to cite (default: the latest)")
    g1.add_argument("--control-run", type=Path, help="the control-acceptance run to cite (default: the latest)")
    prov = cmds.add_parser("provision", help="stage the dojo build's binaries and write the training configs")
    prov.add_argument("--scenario", type=Path, help="scenario manifest the worldserver loads (pins its hash)")
    prov.add_argument("--configs-only", action="store_true", help="rewrite the configs, keep the staged binaries")
    prov.add_argument("--only", action="append", help="write only this config (repeatable), e.g. worldserver.conf")
    cmds.add_parser("schema-build", help="build or update the dojo schemas with the core's auto-setup (dry run)")
    acc = cmds.add_parser("accounts", help="create the RA admin and the fixture account; point realm 1 at the dojo")
    acc.add_argument("--fixture-account", default="DOJOFIXTURE")
    cmds.add_parser("world-start", help="start the dojo worldserver (preflight first) and wait for RA")
    cmds.add_parser("world-stop", help="shut the dojo worldserver down through RA")
    cmds.add_parser("world-status", help="report whether the dojo worldserver runs")
    ra_cmd = cmds.add_parser("ra", help="run one console command on the dojo worldserver")
    ra_cmd.add_argument("text", nargs="+")


def run(args: argparse.Namespace) -> int:
    try:
        layout = load(args.layout)
    except LayoutError as e:
        print(f"realm: {e}", file=sys.stderr)
        return 2
    command = args.realm_command
    try:
        if command == "db-init":
            mysql.init(layout)
        elif command == "db-start":
            ident = mysql.start(layout)
            print(f"dojo mysql: {ident['version']} on 127.0.0.1:{ident['port']}, datadir {ident['datadir']}")
        elif command == "db-stop":
            mysql.stop(layout)
        elif command == "db-status":
            print(json.dumps(mysql.status(layout), indent=2))
        elif command == "db-check":
            from .checks import db_check
            report = db_check(layout)
            for c in report["checks"]:
                print(f"{c['status']:<4} {c['check']}: {c['detail']}")
            return 0 if report["ok"] else 1
        elif command == "acceptance":
            from ..scenario import load_manifest
            from .acceptance import run as run_acceptance
            report = run_acceptance(layout, load_manifest(args.scenario).data, stop_after=not args.keep_running)
            arena = report.get("arena") or {}
            print(f"open: {report['open_result']}")
            print(f"close: {report['close_result']}")
            print(f"identical loadouts: {arena.get('identical')}; arena problems: {arena.get('problems')}")
            print(f"progression side effects: {report['side_effects']['progression'] or 'none'}")
            print(f"bookkeeping changes: {report['side_effects']['bookkeeping'] or 'none'}")
            print(f"live untouched: {report['live_untouched']}; worker peers {report['worker_peers_open']}")
            print(f"acceptance: {'ok' if report['ok'] else 'FAILED'} ({report['out']})")
            return 0 if report["ok"] else 1
        elif command == "control-acceptance":
            from ..scenario import load_manifest
            from .control import ControlError, run as run_control
            try:
                report = run_control(layout, load_manifest(args.scenario).data)
            except ControlError as e:
                print(f"realm control-acceptance: {e}", file=sys.stderr)
                return 1
            for case in report["cases"]:
                print(f"{'ok  ' if case['ok'] else 'FAIL'} {case['case']}")
            print(f"control acceptance: {'ok' if report['ok'] else 'FAILED'} ({report['out']})")
            return 0 if report["ok"] else 1
        elif command == "reset-acceptance":
            from ..scenario import load_manifest
            from .control import ControlError
            from .reset import ResetError, run as run_resets
            if not 1 <= args.resets <= 1000:
                print("realm reset-acceptance: --resets must be 1-1000", file=sys.stderr)
                return 2
            try:
                report = run_resets(layout, load_manifest(args.scenario).data, resets=args.resets,
                                    cases=args.cases.split(",") if args.cases else None)
            except (ControlError, ResetError) as e:
                print(f"realm reset-acceptance: {e}", file=sys.stderr)
                return 1
            for check in report["checks"]:
                print(f"{'ok  ' if check['ok'] else 'FAIL'} {check['check']}")
            print(f"summary: {json.dumps(report['summary'])}")
            print(f"reset acceptance: {'ok' if report['ok'] else 'FAILED'} ({report['out']})")
            return 0 if report["ok"] else 1
        elif command == "g1":
            from ..client import DojoError
            from ..scenario import load_manifest
            from .control import ControlError
            from .g1 import G1Error, run as run_g1
            try:
                report = run_g1(layout, load_manifest(args.scenario).data, quick=args.quick, reset_run=args.reset_run,
                                control_run=args.control_run)
            except (ControlError, G1Error, DojoError) as e:
                print(f"realm g1: {e}", file=sys.stderr)
                return 1
            for check in report["checks"]:
                print(f"{'ok  ' if check['ok'] else 'FAIL'} {check['check']}")
            print(f"G1: {'passed' if report['ok'] else 'NOT passed'} ({report['out']})")
            return 0 if report["ok"] else 1
        elif command == "provision":
            manifest = None
            if args.scenario:
                from ..scenario import load_manifest
                m = load_manifest(args.scenario)
                manifest = (args.scenario.resolve(), m.sha256)
            if not args.configs_only:
                provision.stage_binaries(layout)
            provision.write_configs(layout, manifest, only=set(args.only) if args.only else None)
        elif command == "schema-build":
            return 0 if world.dry_run(layout) == 0 else 1
        elif command == "accounts":
            provision.ensure_accounts(layout, args.fixture_account)
            provision.set_realm(layout)
        elif command == "world-start":
            world.start(layout)
        elif command == "world-stop":
            world.stop(layout)
        elif command == "world-status":
            print(json.dumps({"running": world.running(layout), "pid": world.pid(layout)}, indent=2))
        elif command == "ra":
            with world.ra(layout) as client:
                print(client.command(" ".join(args.text)))
        return 0
    except (mysql.MysqlError, LayoutError, world.WorldError, RaError, OSError) as e:
        print(f"realm {command}: {e}", file=sys.stderr)
        return 1
