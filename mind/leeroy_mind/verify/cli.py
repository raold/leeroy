"""`python -m leeroy_mind verify status [--json]`: which shipped features have been seen working
live. Read-only: bounded log tails, mind.db opened read-only, the configured switches."""
import argparse
import json
import time
from pathlib import Path

from .ledger import STATUSES, ledger

LABEL = {"failed": "FAILED", "seen_working": "working", "unverified": "unverified",
         "switched_off": "off"}


def _when(at) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(at)) if at is not None else "never"


def _mb(size) -> str:
    return f"{size / (1024 * 1024):.1f} MB"


def _evidence_line(kind, rows) -> str | None:
    seen = [r for r in rows if r["kind"] == kind and r["count"]]
    if not seen:
        return None
    parts = []
    for r in seen:
        codes = ", ".join(f"{c['code']} {c['count']}" for c in r["codes"])
        span = (_when(r["last"]) if r["first"] == r["last"]
                else f"{_when(r['first'])} .. {_when(r['last'])}")
        parts.append(f"{r['id']} {r['count']} ({span}{'; ' + codes if codes else ''})")
    return f"{kind}: " + "; ".join(parts)


def lines(result: dict) -> list[str]:
    counts = result["counts"]
    out = [f"verification ledger: {len(result['features'])} features: "
           + ", ".join(f"{counts[s]} {s.replace('_', ' ')}" for s in STATUSES)]
    for name, c in result["coverage"].items():
        issues = ", ".join(f"{k} {v}" for k, v in c["issues"].items())
        out.append(f"logs: {name} {c['files']} files, {_mb(c['bytes'])} back to {_when(c['from'])}"
                   f" ({'whole' if c['complete'] else 'partial'}{': ' + issues if issues else ''})")
    out.append(f"mind.db: {result['database']}")
    for status in STATUSES:
        for f in result["features"]:
            if f["status"] != status:
                continue
            prs = " ".join(f"#{n}" for n in f["prs"]) or "no PR"
            out.append("")
            out.append(f"{LABEL[status]:<11}{f['id']} - {f['name']} ({prs}"
                       + (f", since {_when(f['since'])})" if f["since"] is not None else ")"))
            pad = " " * 11
            off = [s for s in f["switches"] if s["state"] != "on"]
            if off:
                out.append(pad + "switches: " + "; ".join(
                    f"{s['key']} {s['state']} (configured {s['configured']}, default "
                    f"{'on' if s['default'] else 'off'})" for s in off))
            for kind in ("failed", "working", "observed"):
                said = _evidence_line(kind, f["evidence"])
                if said:
                    out.append(pad + said)
            missing = [e["id"] + " " + e["why"] for e in f["evidence"] if not e["available"]]
            if missing:
                out.append(pad + "unavailable: " + ", ".join(missing))
            if status == "unverified" and f["note"]:
                out.append(pad + f["note"])
    out.append("")
    out.append(result["basis"])
    return out


def verify_main(argv=None) -> int:
    from leeroy_mind.__main__ import _add_data_dir, _root
    parser = argparse.ArgumentParser(prog="leeroy_mind verify",
                                     description="which shipped features have been seen working "
                                                 "live (read-only)")
    _add_data_dir(parser)
    subs = parser.add_subparsers(dest="command", required=True)
    status = subs.add_parser("status", help="each feature in verify/features.toml: unverified, "
                                            "seen working, failed or switched off")
    _add_data_dir(status, after_action=True)
    status.add_argument("--native-dir", type=Path, default=None,
                        help="the worldserver's folder with Server.log, Playerbots.log and "
                             "configs/modules (default: build/bin/RelWithDebInfo beside the data dir)")
    status.add_argument("--json", action="store_true", help="print the ledger as one JSON object")
    args = parser.parse_args(argv)
    data = _root(args)
    native = args.native_dir or data.parent / "build" / "bin" / "RelWithDebInfo"
    result = ledger(data, native)
    if args.json:
        print(json.dumps(result, ensure_ascii=True))
    else:
        for line in lines(result):
            print(line.encode("ascii", "replace").decode("ascii"))
    return 0
