"""No migrations, locks, model calls or bridge connection."""
import json
from . import view


def run(args):
    from leeroy_mind.__main__ import _root
    from leeroy_mind.console.feed import read
    from .config import load
    root = _root(args)
    try:
        result = read(root/"mind.db",view.status)
        if args.world_action == "plan":
            result["next"] = read(root/"mind.db",view.plan,cap=load(root/"mind.toml").world_per_night)
        print(json.dumps(result,sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps({"error":"world_status_unavailable","kind":type(exc).__name__}))
        return 1
