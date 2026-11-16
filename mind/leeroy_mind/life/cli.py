"""`python -m leeroy_mind life day|now`: the members' day plans and where they are now, read-only on
mind.db opened mode=ro. Prints guids, codes, times and counts only, never persona text or names."""
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

from .day import DayEvent, day_plan
from .presence import Attention, Where, presence_at


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


def life_main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="leeroy_mind life",
                                     description="The members' day plans and presence (read-only).")
    parser.add_argument("--data-dir")
    sub = parser.add_subparsers(dest="action", required=True)
    p_day = sub.add_parser("day", help="her day plan: job shape, the day's event and the blocks")
    p_day.add_argument("--guid", type=int)
    p_day.add_argument("--date", type=_date, help="YYYY-MM-DD (default: today on this PC)")
    p_now = sub.add_parser("now", help="how many members are where, and their attention, now")
    p_now.add_argument("--at", type=_at, help="YYYY-MM-DDTHH:MM on this PC's clock (default: now)")
    for p in (p_day, p_now):
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
    return _day(personas, args) if args.action == "day" else _now(personas, args)
