"""`python -m leeroy_mind life day|now|intents`: the members' day plans, where they are now and what
they want on a turn (talk/intent.py), read-only on mind.db opened mode=ro. Prints guids, codes,
times, counts and percentages only, never persona text, names or the line given with --line."""
import argparse
import json
import sqlite3
import sys
import time
from collections import Counter
from datetime import date, datetime
from pathlib import Path

from leeroy_mind.clock import local_at
from leeroy_mind.config import default_data_dir
from leeroy_mind.discord.presence import local_time
from leeroy_mind.personas.schema import Persona, persona_from_dict
from leeroy_mind.personas.tiers import cast_sql
from leeroy_mind.talk.intent import Intent, IntentContext, draw_intent
from leeroy_mind.talk.romance import forward
from leeroy_mind.talk.turns import TurnKind

from .day import DayEvent, day_plan
from .presence import Attention, LifePresence, Where, presence_at
from .seed import seeded

FAMILIES = ("game", "whisper", "discord", "dm", "group")
DISCORD_FAMILIES = frozenset({"discord", "dm", "group"})
TO_HIM = frozenset({TurnKind.REPLY_HUMAN, TurnKind.WHISPER, TurnKind.FLIRT})


def _date(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError("date must be YYYY-MM-DD") from None


def _at(text: str) -> datetime:
    try:
        return datetime.fromisoformat(text).astimezone()
    except ValueError:
        raise argparse.ArgumentTypeError("at must be YYYY-MM-DDTHH:MM (this PC's clock)") from None


def _samples(text: str) -> int:
    value = int(text)
    if not 1 <= value <= 100_000:
        raise argparse.ArgumentTypeError("samples must be 1-100000")
    return value


def _hhmm(minute: int) -> str:
    return f"{minute // 60:02d}:{minute % 60:02d}"


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


def _day(personas: dict[int, Persona], args) -> int:
    if args.guid is not None:
        if args.guid not in personas:
            print(f"life day: no persona for guid {args.guid}", file=sys.stderr)
            return 1
        personas = {args.guid: personas[args.guid]}
    day = args.date or local_at(time.time()).date()
    for guid, p in personas.items():
        plan = day_plan(p, day)
        print(f"{guid} job={plan.job} event={plan.event}")
        for span in plan.spans:
            print(f"{_hhmm(span.start)}-{_hhmm(span.end)} {span.block}")
    return 0


def _counts(counter: Counter, keys) -> str:
    return " ".join(f"{k}={counter[k]}" for k in keys)


def _now(personas: dict[int, Persona], args) -> int:
    owner_now = args.at or local_at(time.time())
    where, game, discord, events = Counter(), Counter(), Counter(), Counter()
    for p in personas.values():
        her = local_time(owner_now, p.identity.region)
        on_game = presence_at(p, her, medium="game")
        where[on_game.where] += 1
        events[on_game.event] += 1
        game[on_game.attention.name.lower()] += 1
        discord[presence_at(p, her, medium="discord").attention.name.lower()] += 1
    attention = [a.name.lower() for a in Attention]
    print(f"now {owner_now:%Y-%m-%d %H:%M} (this PC's clock): {len(personas)} members")
    print("where: " + _counts(where, list(Where)))
    print("game attention: " + _counts(game, attention))
    print("discord attention: " + _counts(discord, attention))
    print("events: " + _counts(events, list(DayEvent)))
    return 0


def intent_context(p: Persona, lp: LifePresence, *, kind: TurnKind, family: str, owed: bool,
                   his_line: str, tail: bool, event: str | None = None) -> IntentContext:
    """Her context from her stored persona and her presence now (`lp`: her block, attention, the
    day's event and kids, from her day plan). `event` overrides the plan's event. Her feeling
    right now (life.state, plan Task 3) is not read: state stays None. Her forward dial is the
    stored persona's (romance.forward: 1 unless a dial rides on it). The live context is the
    pipeline's (plan Task 8)."""
    return IntentContext(
        kind=kind, to_him=kind in TO_HIM, owed=owed, his_line=his_line, family=family,
        female=p.identity.player_gender == "female", stance=p.romance.stance,
        style=p.romance.style, attachment=p.attachment, humour=p.humour,
        agreeableness=p.big_five.agreeableness.score, extraversion=p.big_five.extraversion.score,
        tail=tail, attention=lp.attention, state=None,
        event=DayEvent(event) if event else lp.event, block=lp.block, kids=lp.kids, last=None,
        forward=forward(p))


def _intents(personas: dict[int, Persona], args) -> int:
    p = personas.get(args.guid)
    if p is None:
        print(f"life intents: no persona for guid {args.guid}", file=sys.stderr)
        return 1
    kind = TurnKind(args.kind)
    owed = kind is TurnKind.REPLY_HUMAN if args.owed is None else args.owed
    her = local_time(args.at or local_at(time.time()), p.identity.region)
    lp = presence_at(p, her, medium="discord" if args.family in DISCORD_FAMILIES else "game")
    ctx = intent_context(p, lp, kind=kind, family=args.family, owed=owed,
                         his_line=args.line or "", tail=args.tail, event=args.event)
    rng = seeded("intents", her.date().isoformat(), args.guid, her.hour * 60 + her.minute)
    drawn = Counter(draw_intent(ctx, rng) for _ in range(args.samples))
    print(f"{args.guid} kind={kind} family={args.family} owed={'yes' if owed else 'no'} "
          f"block={ctx.block} attention={lp.attention.name.lower()} event={ctx.event} "
          f"samples={args.samples}")
    for i in Intent:
        print(f"{i} {100 * drawn[i] / args.samples:.1f}%")
    if drawn[None]:
        print(f"none {100 * drawn[None] / args.samples:.1f}%")
    return 0


ACTIONS = {"day": _day, "now": _now, "intents": _intents}


def life_main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="leeroy_mind life",
        description="The members' day plans, presence and wants (read-only).")
    parser.add_argument("--data-dir")
    sub = parser.add_subparsers(dest="action", required=True)
    p_day = sub.add_parser("day", help="her day plan: job shape, the day's event and the blocks")
    p_day.add_argument("--guid", type=int)
    p_day.add_argument("--date", type=_date, help="YYYY-MM-DD (default: today on this PC)")
    p_now = sub.add_parser("now", help="how many members are where, and their attention, now")
    p_now.add_argument("--at", type=_at, help="YYYY-MM-DDTHH:MM on this PC's clock (default: now)")
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
    p_int.add_argument("--event", choices=[e.value for e in DayEvent],
                       help="the day's event instead of her plan's")
    p_int.add_argument("--at", type=_at, help="YYYY-MM-DDTHH:MM on this PC's clock (default: now)")
    p_int.add_argument("--samples", type=_samples, default=500)
    for p in (p_day, p_now, p_int):
        p.add_argument("--data-dir", default=argparse.SUPPRESS)
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
    return ACTIONS[args.action](personas, args)
