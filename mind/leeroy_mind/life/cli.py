"""`python -m leeroy_mind life ...`: read-only looks at the life models, on mind.db opened
mode=ro. No service, model, migration or write.

`shapes --guid N --family dm --his-words 28 [--owed] [--samples 1000]` draws her reply shapes
(talk/shape.py) from her stored typing profile and prints, as JSON, each shape's share and the
words' p10/p50/p90 of the shapes that write something: numbers and codes only, never persona
text. `[talk]` discord_words, dm_words and realism come from mind.toml; `--dm-mirror` (default
5), `--kind` and `--realism`/`--no-realism` override."""
import argparse
import json
import math
import random
import sqlite3
import sys
from collections import Counter
from pathlib import Path

from leeroy_mind.config import ConfigError, default_data_dir, load_config
from leeroy_mind.personas.schema import PersonaError, persona_from_dict
from leeroy_mind.talk.shape import Shape, ShapeContext, draw_shape
from leeroy_mind.talk.turns import TurnKind

FAMILIES = ("game", "whisper", "discord", "dm", "group")
DM_MIRROR_DEFAULT = 5


def _samples(text: str) -> int:
    value = int(text)
    if not 1 <= value <= 100_000:
        raise argparse.ArgumentTypeError("samples must be 1-100000")
    return value


def _pct(values: list[int], p: int) -> int:
    """The nearest-rank `p`th percentile of `values`; 0 when empty."""
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[max(0, math.ceil(p / 100 * len(ordered)) - 1)]


def _typing(root: Path, guid: int):
    conn = sqlite3.connect(f"{(root / 'mind.db').resolve().as_uri()}?mode=ro", uri=True)
    try:
        row = conn.execute("SELECT json FROM persona_core WHERE guid = ?", (guid,)).fetchone()
    finally:
        conn.close()
    return None if row is None else persona_from_dict(json.loads(row[0])).typing


def _shapes(args, root: Path) -> int:
    talk = load_config(root / "mind.toml").talk
    typing = _typing(root, args.guid)
    if typing is None:
        print("life shapes: no persona for that guid", file=sys.stderr)
        return 1
    dm_mirror = args.dm_mirror if args.dm_mirror is not None else getattr(
        talk, "dm_mirror", DM_MIRROR_DEFAULT)
    ctx = ShapeContext(
        family=args.family, kind=TurnKind(args.kind), owed=args.owed, intent=None, typing=typing,
        attention=None, state=None, late_by=0.0, his_words=args.his_words,
        discord_words=talk.discord_words, dm_words=talk.dm_words, dm_mirror=dm_mirror,
        realism=talk.realism if args.realism is None else args.realism, two_lines=False)
    rng = random.Random(args.guid)
    draws = [draw_shape(ctx, rng) for _ in range(args.samples)]
    counts = Counter(d.shape for d in draws)
    words = [d.words for d in draws if d.words > 0]
    print(json.dumps({
        "guid": args.guid, "family": args.family, "kind": args.kind, "owed": args.owed,
        "his_words": args.his_words, "dm_mirror": dm_mirror, "samples": args.samples,
        "shares": {s.value: round(counts[s] / args.samples, 4) for s in Shape},
        "words": {"p10": _pct(words, 10), "p50": _pct(words, 50), "p90": _pct(words, 90)},
    }, sort_keys=True))
    return 0


def life_main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="leeroy_mind life")
    parser.add_argument("--data-dir")
    sub = parser.add_subparsers(dest="action", required=True)
    shapes = sub.add_parser("shapes", help="her reply shapes' shares and words (numbers only)")
    shapes.add_argument("--data-dir", default=argparse.SUPPRESS)
    shapes.add_argument("--guid", type=int, required=True)
    shapes.add_argument("--family", choices=FAMILIES, required=True)
    shapes.add_argument("--his-words", type=int, default=12)
    shapes.add_argument("--owed", action="store_true")
    shapes.add_argument("--kind", choices=[k.value for k in TurnKind],
                        default=TurnKind.REPLY_HUMAN.value)
    shapes.add_argument("--dm-mirror", type=int, choices=range(0, 11), default=None)
    shapes.add_argument("--realism", action=argparse.BooleanOptionalAction, default=None)
    shapes.add_argument("--samples", type=_samples, default=1000)
    args = parser.parse_args(argv)
    root = Path(args.data_dir).resolve() if args.data_dir else default_data_dir()
    try:
        return _shapes(args, root)
    except (sqlite3.Error, ValueError, PersonaError, ConfigError, OSError) as exc:
        print(f"life {args.action} failed: {type(exc).__name__}", file=sys.stderr)
        return 1
