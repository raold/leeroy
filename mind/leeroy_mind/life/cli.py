"""`python -m leeroy_mind life day|now|intents|shapes|state|places`: the members' day plans,
where they are now, what they want on a turn (talk/intent.py), their reply shapes, how they feel
right now (life/statebook.py) and where they live (geography/: her place, zone and local time),
read-only on mind.db opened mode=ro (shapes also reads mind.toml). Prints guids, codes, times,
numbers, counts and percentages only (places --guid adds her code-built "where you live" line),
never persona text, names, his [owner] home or the line given with --line.

`shapes --guid N --family dm --his-words 28 [--owed] [--samples 1000]` draws her reply shapes
(talk/shape.py) from her stored typing profile and prints, as JSON, each shape's share and the
words' p10/p50/p90 of the shapes that write something. `[talk]` discord_words, dm_words and
realism come from mind.toml; `--dm-mirror` (default 5), `--kind` and `--realism`/`--no-realism`
override."""
import argparse
import json
import math
import random
import sqlite3
import sys
import time
from collections import Counter
from datetime import date, datetime
from pathlib import Path

from leeroy_mind.clock import local_at
from leeroy_mind.config import ConfigError, default_data_dir, load_config
from leeroy_mind.discord.presence import her_local
from leeroy_mind.personas.schema import Persona, persona_from_dict
from leeroy_mind.personas.tiers import cast_sql
from leeroy_mind.talk.intent import Intent, IntentContext, draw_intent
from leeroy_mind.talk.romance import forward
from leeroy_mind.talk.shape import Shape, ShapeContext, draw_shape
from leeroy_mind.talk.turns import TurnKind

from .day import DayEvent, day_plan
from .presence import Attention, LifePresence, Where, presence_at
from .seed import seeded
from .state import AXES, decayed
from .statebook import STATE_KEY, arousal_of, baseline_at, decode, toward_seed_of


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


FAMILIES = ("game", "whisper", "discord", "dm", "group")
DISCORD_FAMILIES = frozenset({"discord", "dm", "group"})
TO_HIM = frozenset({TurnKind.REPLY_HUMAN, TurnKind.WHISPER, TurnKind.FLIRT})
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


def _switch(root: Path) -> None:
    """[talk] coherence for this command, as the running mind has it (LifeService.configure):
    a process that builds no LifeService reads the day plan's module switch. [geography] too:
    her own clock as the running mind keeps it (never his [owner] home: nothing here shows it)."""
    from . import day as day_module
    from leeroy_mind.geography import config as geo_config
    geo_config.pin(geo_config.load_geography_config(root / "mind.toml").enabled)
    try:
        talk = load_config(root / "mind.toml").talk
    except (ConfigError, OSError):
        return  # the default: off
    day_module.COHERENT = getattr(talk, "coherence", False) is True


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


def _places(personas: dict[int, Persona], args) -> int:
    """Where each member lives ([geography]): her place on the sheet, its zone and her local time
    now; with --guid, her "where you live, right now" line too."""
    from leeroy_mind.geography import config as geo_config
    from leeroy_mind.geography.lines import now_parts
    from leeroy_mind.geography.places import place_of
    if args.guid is not None:
        if args.guid not in personas:
            print(f"life places: no persona for guid {args.guid}", file=sys.stderr)
            return 1
        personas = {args.guid: personas[args.guid]}
    owner_now = args.at or local_at(time.time())
    on = geo_config.on()
    counts = Counter()
    for guid, p in sorted(personas.items()):
        place = place_of(p) if on else None
        her = her_local(owner_now, p)
        counts[place.zone if place else "owner clock"] += 1
        print(f"{guid} {p.identity.region} place={place.key if place else '-'} "
              f"zone={place.zone if place else 'owner clock'} local={her:%Y-%m-%d %H:%M}")
        if args.guid is not None and place is not None:
            print(" ".join(now_parts(place, her)))
    print(f"geography {'on' if on else 'off'}: "
          + " ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    return 0


def _counts(counter: Counter, keys) -> str:
    return " ".join(f"{k}={counter[k]}" for k in keys)


def _now(personas: dict[int, Persona], args) -> int:
    owner_now = args.at or local_at(time.time())
    where, game, discord, events = Counter(), Counter(), Counter(), Counter()
    for p in personas.values():
        her = her_local(owner_now, p)
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
    her = her_local(args.at or local_at(time.time()), p)
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


def _shapes(personas: dict[int, Persona], args, root: Path) -> int:
    if args.guid not in personas:
        print(f"life shapes: no persona for guid {args.guid}", file=sys.stderr)
        return 1
    try:
        talk = load_config(root / "mind.toml").talk
    except (ConfigError, OSError) as exc:
        print(f"life shapes failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    dm_mirror = args.dm_mirror if args.dm_mirror is not None else getattr(
        talk, "dm_mirror", DM_MIRROR_DEFAULT)
    ctx = ShapeContext(
        family=args.family, kind=TurnKind(args.kind), owed=args.owed, intent=None,
        typing=personas[args.guid].typing, attention=None, state=None, late_by=0.0,
        his_words=args.his_words, discord_words=talk.discord_words, dm_words=talk.dm_words,
        dm_mirror=dm_mirror, realism=talk.realism if args.realism is None else args.realism,
        two_lines=False)
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


def _numbers(state) -> str:
    return " ".join(f"{axis}={getattr(state, axis):.2f}" for axis in AXES)


def _state(db: Path, personas: dict[int, Persona], args) -> int:
    """Each member's feeling now (the stored state eased toward her baseline) and the baseline,
    at this PC's time; what is stored is never changed."""
    from leeroy_mind.tuning.edits import fold_mood, mood_overlay
    if args.guid is not None:
        if args.guid not in personas:
            print(f"life state: no persona for guid {args.guid}", file=sys.stderr)
            return 1
        personas = {args.guid: personas[args.guid]}
    now = time.time()
    owner_now = local_at(now)
    conn = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True, timeout=5.0)
    try:
        meta = lambda key: conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        raw = meta("human_guid")
        human = int(raw[0]) if raw and str(raw[0]).isdecimal() else 0
        for guid, p in personas.items():
            row = conn.execute("SELECT mood FROM persona_state WHERE guid = ?", (guid,)).fetchone()
            mood = fold_mood(float(row[0]) if row else 0.0, mood_overlay(conn, guid, now))
            base = baseline_at(p, owner_local=owner_now, mood=mood, arousal=arousal_of(conn, guid),
                               toward_seed=toward_seed_of(conn, guid, human, owner_now))
            stored = meta(STATE_KEY.format(guid=guid))
            kept = decode(stored[0] if stored else None)
            state = base if kept is None else decayed(kept[0], base, now - kept[1])
            age = "none" if kept is None else f"{max(0, round(now - kept[1]))}s ago"
            print(f"{guid} now {_numbers(state)} stored={age}")
            print(f"{guid} baseline {_numbers(base)}")
    except sqlite3.Error as exc:
        print(f"life state failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    return 0


ACTIONS = {"day": _day, "now": _now, "intents": _intents, "places": _places}  # shapes also reads mind.toml (root)


def life_main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="leeroy_mind life",
                                     description="The members' day plans, presence, wants and "
                                                 "reply shapes (read-only).")
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
    p_shapes = sub.add_parser("shapes", help="her reply shapes' shares and words (numbers only)")
    p_shapes.add_argument("--guid", type=int, required=True)
    p_shapes.add_argument("--family", choices=FAMILIES, required=True)
    p_shapes.add_argument("--his-words", type=int, default=12)
    p_shapes.add_argument("--owed", action="store_true")
    p_shapes.add_argument("--kind", choices=[k.value for k in TurnKind],
                          default=TurnKind.REPLY_HUMAN.value)
    p_shapes.add_argument("--dm-mirror", type=int, choices=range(0, 11), default=None)
    p_shapes.add_argument("--realism", action=argparse.BooleanOptionalAction, default=None)
    p_shapes.add_argument("--samples", type=_samples, default=1000)
    p_state = sub.add_parser("state", help="how each member feels right now, and her baseline")
    p_state.add_argument("--guid", type=int)
    p_places = sub.add_parser("places", help="where each member lives, her zone and local time")
    p_places.add_argument("--guid", type=int)
    p_places.add_argument("--at", type=_at, help="YYYY-MM-DDTHH:MM on this PC's clock (default: now)")
    for p in (p_day, p_now, p_int, p_shapes, p_state, p_places):
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
    _switch(root)
    if args.action == "shapes":
        return _shapes(personas, args, root)
    if args.action == "state":
        return _state(db, personas, args)
    return ACTIONS[args.action](personas, args)
