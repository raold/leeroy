"""Read-only aggregate report, including schemas that predate learning."""
import argparse
from collections import defaultdict
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import time

from leeroy_mind.config import default_data_dir
from .reward import DAY, channel_kind, decayed
from .weights import available


def report(conn, *, days=14, now=None):
    now = time.time() if now is None else now
    if not available(conn):
        return {"available": False, "lines": 0, "members": [], "channels": [], "recipes": []}
    rows = conn.execute("SELECT r.guid,r.value,r.cap_weight,c.ts,c.channel,p.template_id,p.template_version,"
        "p.dials_hash,p.model_id,p.guard_verdict FROM talk_reward r "
        "JOIN chat_line c ON c.id=r.line_id LEFT JOIN talk_recipe p ON p.line_id=c.id "
        "WHERE c.ts>=? AND c.ts<=? ORDER BY c.id", (now - days * DAY, now)).fetchall()
    members, channels, recipes = (defaultdict(list) for _ in range(3))
    for guid, value, cap_weight, ts, channel, template, version, dials, model, verdict in rows:
        item = (value, decayed(value * (cap_weight if value > 0 else 1), age_seconds=now - ts))
        members[guid].append(item)
        channels[channel_kind(channel)].append(item)
        for key, field in (("template_id", template), ("template_version", version),
                           ("dials_hash", dials), ("model_id", model), ("guard_verdict", verdict)):
            recipes[(key, field)].append(item)
    def stats(values):
        return {"count": len(values), "mean": sum(v[0] for v in values) / len(values),
                "decayed_total": sum(v[1] for v in values)}
    return {"available": True, "lines": len(rows), "days": days,
            "members": [{"guid": k, **stats(v)} for k, v in sorted(members.items())],
            "channels": [{"kind": k, **stats(v)} for k, v in sorted(channels.items())],
            "recipes": [{"field": k[0], "value": k[1], **stats(v)} for k, v in
                        sorted(recipes.items(), key=lambda item: (item[0][0], str(item[0][1])))]}


def learn_main(argv=None):
    parser = argparse.ArgumentParser(prog="leeroy_mind learn")
    parser.add_argument("--data-dir")
    command = parser.add_subparsers(dest="action", required=True).add_parser("report")
    command.add_argument("--data-dir", default=argparse.SUPPRESS)
    command.add_argument("--days", type=int, default=14)
    args = parser.parse_args(argv)
    if not 1 <= args.days <= 3650:
        parser.error("--days must be between 1 and 3650")
    root = Path(args.data_dir).resolve() if args.data_dir else default_data_dir()
    try:
        with closing(sqlite3.connect(f"{(root / 'mind.db').resolve().as_uri()}?mode=ro", uri=True)) as conn:
            print(json.dumps(report(conn, days=args.days), sort_keys=True, allow_nan=False))
        return 0
    except (sqlite3.Error, ValueError, OSError):
        print("learn_report_unavailable", file=sys.stderr)
        return 1
