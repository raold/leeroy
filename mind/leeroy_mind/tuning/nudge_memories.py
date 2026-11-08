"""Optional past-mood hints for reflection, never factual evidence or a forced memory.

Only everywhere nudges qualify: remembering a channel-limited instruction could carry
it outside its scope later. The ordinary reflection writer and provenance/privacy checks
still decide what, if anything, can be remembered.
"""
import json
import tomllib
from pathlib import Path

from . import nudges as N


def enabled(data_dir):
    path = Path(data_dir) / "mind.toml"
    try:
        if not path.exists():
            return True
        if path.stat().st_size > 1024 * 1024:
            return False
        table = tomllib.loads(path.read_text(encoding="utf-8-sig")).get("tuning", {})
        if not isinstance(table, dict) or set(table) - {"nudge_memories"}:
            return False
        value = table.get("nudge_memories", True)
        return value if type(value) is bool else False
    except (OSError, ValueError):
        return False


def past_moods(ctx, owner):
    records = N.values(ctx.store.conn)
    if not records or not enabled(ctx.settings.data_dir):
        return ""
    tuning = ctx.tuning or ctx.personas.tuning
    p = ctx.persona(owner)
    cast = ctx.store.cast()
    eligible = N.for_member((N.Nudge(**v) for v in records.values()), p, cast.get(owner),
                            tuning.groups, ctx.human)
    ended = [n for n in eligible if n.scope == "everywhere" and
             ctx.window[0] < min(n.ends, n.retired if n.retired is not None else n.ends)
             <= ctx.window[1]]
    if not ended:
        return ""
    # At most two short intentions. Never supply an evidence id for an instruction.
    lines = [json.dumps(n.text, ensure_ascii=False) for n in sorted(
        ended, key=lambda n: (-n.ends, -n.id))[:2]]
    return ("Temporary intentions that ended during this period: " + "; ".join(lines)
            + ". If one affected how you actually felt, you may remember it as a past mood. "
            "These are not evidence of events or of anyone else's feelings or actions, "
            "and they are no longer directions to follow.")
