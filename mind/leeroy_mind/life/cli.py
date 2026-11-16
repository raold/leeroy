"""`python -m leeroy_mind life intents`: what a member wants on a turn, as the code would draw it
(talk/intent.py), read-only on mind.db opened mode=ro. Prints guids, codes and percentages only,
never persona text, names or the line given with --line."""
import argparse
import json
import random
import sqlite3
import sys
from collections import Counter
from pathlib import Path

from leeroy_mind.config import default_data_dir
from leeroy_mind.personas.schema import Persona, persona_from_dict
from leeroy_mind.personas.tiers import cast_sql
from leeroy_mind.talk.intent import Intent, IntentContext, draw_intent
from leeroy_mind.talk.turns import TurnKind

FAMILIES = ("game", "whisper", "discord", "dm", "group")
EVENTS = ("none", "late_shift", "sick_kid", "night_out", "bad_day")  # life.day.DayEvent's codes
TO_HIM = frozenset({TurnKind.REPLY_HUMAN, TurnKind.WHISPER, TurnKind.FLIRT})


def _samples(text: str) -> int:
    value = int(text)
    if not 1 <= value <= 100_000:
        raise argparse.ArgumentTypeError("samples must be 1-100000")
    return value


def _personas(conn) -> tuple[dict[int, Persona], int]:
    """The cast's stored personas; one the code cannot read is counted, never shown."""
    try:
        rows = conn.execute(f"SELECT guid, json FROM persona_core WHERE {cast_sql(conn)} "
                            "ORDER BY guid").fetchall()
    except sqlite3.OperationalError as exc:
        if str(exc).startswith("no such table"):
            return {}, 0
        raise
    out, unreadable = {}, 0
    for guid, raw in rows:
        try:
            out[int(guid)] = persona_from_dict(json.loads(raw))
        except Exception:
            unreadable += 1
    return out, unreadable


def intent_context(p: Persona, *, kind: TurnKind, family: str, owed: bool, his_line: str,
                   tail: bool, event: str) -> IntentContext:
    """Her context from her stored persona. Where she is and how she feels (life.presence and
    life.state, plan Tasks 1 and 3) are not read here: attention, state and block stay None,
    kids False and the day's event is `event` (the live context is the pipeline's, Task 8)."""
    return IntentContext(
        kind=kind, to_him=kind in TO_HIM, owed=owed, his_line=his_line, family=family,
        female=p.identity.player_gender == "female", stance=p.romance.stance,
        style=p.romance.style, attachment=p.attachment, humour=p.humour,
        agreeableness=p.big_five.agreeableness.score, extraversion=p.big_five.extraversion.score,
        tail=tail, attention=None, state=None, event=event, block=None, kids=False, last=None)


def _intents(personas: dict[int, Persona], args) -> int:
    p = personas.get(args.guid)
    if p is None:
        print(f"life intents: no persona for guid {args.guid}", file=sys.stderr)
        return 1
    kind = TurnKind(args.kind)
    owed = kind is TurnKind.REPLY_HUMAN if args.owed is None else args.owed
    ctx = intent_context(p, kind=kind, family=args.family, owed=owed, his_line=args.line or "",
                         tail=args.tail, event=args.event)
    rng = random.Random(f"intents:{args.guid}")
    drawn = Counter(draw_intent(ctx, rng) for _ in range(args.samples))
    print(f"{args.guid} kind={kind} family={args.family} owed={'yes' if owed else 'no'} "
          f"samples={args.samples}")
    for i in Intent:
        print(f"{i} {100 * drawn[i] / args.samples:.1f}%")
    if drawn[None]:
        print(f"none {100 * drawn[None] / args.samples:.1f}%")
    return 0


def life_main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="leeroy_mind life",
                                     description="A member's day and wants (read-only).")
    parser.add_argument("--data-dir")
    sub = parser.add_subparsers(dest="action", required=True)
    p_int = sub.add_parser("intents", help="each want's share on a turn, as the code draws it")
    p_int.add_argument("--guid", type=int, required=True)
    p_int.add_argument("--kind", choices=[k.value for k in TurnKind],
                       default=TurnKind.REPLY_HUMAN.value)
    p_int.add_argument("--family", choices=FAMILIES, default="whisper")
    p_int.add_argument("--line", help="his line the turn answers (read, never printed)")
    p_int.add_argument("--owed", action=argparse.BooleanOptionalAction, default=None,
                       help="an answer owed to him (default: yes for reply_human)")
    p_int.add_argument("--tail", action="store_true",
                       help="his explicit whisper overlay applies")
    p_int.add_argument("--event", choices=EVENTS, default="none", help="the day's event")
    p_int.add_argument("--samples", type=_samples, default=500)
    p_int.add_argument("--data-dir", default=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    root = Path(args.data_dir).resolve() if args.data_dir else default_data_dir()
    db = root / "mind.db"
    if not db.is_file():
        print(f"life: no mind.db in {root}", file=sys.stderr)
        return 2
    try:
        conn = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True, timeout=5.0)
        try:
            personas, unreadable = _personas(conn)
        finally:
            conn.close()
    except sqlite3.Error as exc:
        print(f"life {args.action} failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    if unreadable:
        print(f"life: {unreadable} stored personas unreadable, left out", file=sys.stderr)
    return _intents(personas, args)
