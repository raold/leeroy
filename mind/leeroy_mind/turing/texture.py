"""`turing texture`: what the typing texture ([talk] texture, talk/style.py) would make of the
members' stored game and private lines, measured as the night's stylometry measures them.

The texture is applied after generation, so stored lines typed under the old rule (every
abbreviation every time, no habits) can be typed again offline: a member's own abbreviations are
put back into words, then her last full stop, her run-ons, shorthand, spelling habits and
apostrophes are drawn as style.style_lines draws them, with a `style.Calibration`. His lines are
never changed. The result is the family's AUC over the words alone (stylo.TEXT_KEYS: timing and
bursts are not the texture's) and each mechanical feature's rate, his beside the members' as
stored and as typed again, on all the lines and on two halves split by day (`split`: fit on one,
confirm on the other).

Only lines from before the texture went on are a fair input (`until`): a line already typed with
it would get it twice. What cannot be replayed: case (stored lines are already lower-cased), old
word-by-word typos (left in), and how the model's drafts drift once its own textured lines are
its chat history. Read only; numbers and fixed labels only, never a line's text."""
import json
import random
import re
import sqlite3
from collections import Counter
from dataclasses import replace
from datetime import datetime
from typing import Sequence

from leeroy_mind.personas.schema import PersonaError, persona_from_dict
from leeroy_mind.talk import style
from leeroy_mind.talk.states import EFFECTS, Altered

from . import stylo
from .corpus import FAMILY_LABELS, GAME, Line, name_pattern, names_by_guid, read_lines
from .features import APOSTROPHE, BARE, LABELS, text_traits, tokens

FAMILIES = ("game", "private")
# The features the texture types: the rest of a line is the model's words.
MECHANICAL = ("apostrophe", "bare", "comma", "inner_period", "end_period", "abbrev")
SEEDS = ("a", "b", "c")  # the retyping is a draw: its AUC is the mean over these
PARTS = {"all": "All days", "fit": "Half the days (fit)", "confirm": "The other half (confirm)"}


def typings(conn: sqlite3.Connection) -> dict[int, tuple[str, object]]:
    """guid -> (character name, Typing) of every stored persona that reads."""
    out = {}
    for guid, text in conn.execute("SELECT guid, json FROM persona_core"):
        try:
            p = persona_from_dict(json.loads(text))
        except (ValueError, TypeError, PersonaError):
            continue
        out[guid] = (p.character_name, p.typing)
    return out


def _expand(text: str, typing) -> str:
    """Her own abbreviations back in words (the old rule wrote every one, every time)."""
    back: dict[str, str] = {}
    for key, val in style._shortening(typing.abbreviations).items():
        if not val.isdigit():
            back.setdefault(val.casefold(), key)
    if not back:
        return text
    pat = re.compile(style._WB + "(?:" + "|".join(
        re.escape(v) for v in sorted(back, key=len, reverse=True)) + ")" + style._WE, re.IGNORECASE)
    return style._outside_links(text, lambda seg: pat.sub(lambda m: back[m.group(0).casefold()], seg))


def retype(line: Line, name: str, typing, cal: style.Calibration, protect: frozenset[str],
           seed: str) -> str:
    """One stored member line as the texture would type it."""
    tex = style._texture(name, typing.case, typing.end_punct, typing.typo_rate > 0, cal)
    rng = random.Random(f"{seed}:{line.ref}")
    plain = _expand(line.text, typing)
    if typing.end_punct in ("full", "some") and plain.endswith(".") and not plain.endswith(".."):
        # stored with its last full stop always (full) or on half the lines (some)
        keep = tex.stops if typing.end_punct == "full" else min(1.0, tex.stops / style.SOME_DROP_P)
        if rng.random() >= keep:
            plain = plain[:-1].rstrip()
    plain = style._run_on(plain, tex, rng, protect, typing.words_per_line[1])
    typed, _, _ = style._textured(plain, replace(typing, typo_rate=0.0), tex,
                                  EFFECTS[Altered.SOBER], rng, protect,
                                  phone=line.channel != "whisper" and line.channel not in GAME)
    return typed or line.text


def retyped(lines: Sequence[Line], cast: dict, cal: style.Calibration, protect: frozenset[str],
            seed: str) -> list[Line]:
    return [line if line.human else replace(line, text=retype(line, *cast[line.speaker], cal,
                                                              protect, seed)) for line in lines]


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def split(lines: Sequence[Line]) -> dict[str, int]:
    """His days dealt into two halves, the busiest first, each to the half with fewer of his
    lines so far: local date -> 0 or 1."""
    days = Counter(_day(line.ts) for line in lines if line.human)
    side, total = {}, [0, 0]
    for day, n in sorted(days.items(), key=lambda kv: (-kv[1], kv[0])):
        k = 0 if total[0] <= total[1] else 1
        side[day], total[k] = k, total[k] + n
    return side


def rates(lines: Sequence[Line], family: str, human: bool) -> dict:
    """Each mechanical feature's share of the lines, and of the contractions the share typed
    without their apostrophe."""
    texts = [line.text for line in lines if line.family == family and line.human == human]
    traits = [text_traits(t) for t in texts]
    out = {"lines": len(texts)}
    for key in MECHANICAL:
        out[key] = round(sum(t[key] for t in traits) / len(traits), 4) if traits else None
    words = [w for t in texts for w in tokens(t)]
    kept, bare = sum(w in APOSTROPHE for w in words), sum(w in BARE for w in words)
    out["dropped"] = round(bare / (kept + bare), 4) if kept + bare else None
    return out


def _auc(lines, family, pattern, by_guid) -> float | None:
    return stylo.analyse(lines, family, names=pattern, by_guid=by_guid, seed="turing|texture",
                         keys=stylo.TEXT_KEYS).auc


def spread(cast: dict, speakers, cal: style.Calibration) -> dict:
    """The habits' spread over the members who spoke: how many keep, mix or drop apostrophes,
    how many type their own one-word shorthand as a habit, and how many of those who declare
    full stops type a line's last one."""
    tex = {g: style._texture(n, t.case, t.end_punct, t.typo_rate > 0, cal)
           for g, (n, t) in cast.items() if g in speakers}
    drops = [x.apostrophes for x in tex.values()]
    full = [tex[g].stops for g in tex if cast[g][1].end_punct == "full"]
    return {"members": len(tex),
            "apostrophes": {"keep": sum(d < .4 for d in drops),
                            "mixed": sum(.4 <= d < .8 for d in drops),
                            "drop": sum(d >= .8 for d in drops)},
            "texters": sum(x.shorthand > .5 for x in tex.values()),
            "full_stops": {"declare": len(full), "type": sum(s >= .6 for s in full)}}


def replay(conn: sqlite3.Connection, *, human: int, start: float, until: float,
           his_names: Sequence[str] = (), cal: style.Calibration = style.CAL) -> dict:
    """The stored lines from `start` to `until`, measured as stored and typed again with `cal`:
    per part ("all", "fit", "confirm") and family his rates, the members' both ways and the
    words-only AUC both ways (None when a side has too few lines)."""
    cast = typings(conn)
    by_guid = names_by_guid(conn, human=human, his=his_names)
    pattern = name_pattern([*by_guid.values(), *his_names])
    protect = style._words_cf(frozenset(by_guid.values()))
    lines = [line for line in read_lines(conn, human=human, start=start, end=until)
             if line.family in FAMILIES and (line.human or line.speaker in cast)]
    side = split(lines)
    parts = {"all": lines, "fit": [x for x in lines if side.get(_day(x.ts), 0) == 0],
             "confirm": [x for x in lines if side.get(_day(x.ts), 0) == 1]}
    out: dict = {"start": start, "until": until, "parts": {},
                 "spread": spread(cast, {x.speaker for x in lines if not x.human}, cal)}
    for name, part in parts.items():
        typed = [retyped(part, cast, cal, protect, seed) for seed in SEEDS]
        fams = {}
        for family in FAMILIES:
            aucs = [a for a in (_auc(t, family, pattern, by_guid) for t in typed) if a is not None]
            fams[family] = {
                "him": rates(part, family, True), "stored": rates(part, family, False),
                "typed": rates(typed[0], family, False),
                "auc_stored": _auc(part, family, pattern, by_guid),
                "auc_typed": round(sum(aucs) / len(aucs), 4) if len(aucs) == len(SEEDS) else None}
        out["parts"][name] = fams
    return out


def _pct(value) -> str:
    return "n/a" if value is None else f"{100 * value:.0f}%"


def _two(value) -> str:
    return "n/a" if value is None else f"{value:.2f}"


def render(data: dict) -> str:
    out = [f"Typing texture replayed on the stored lines from {_day(data['start'])} to "
           f"{_day(data['until'])}: you, the members as stored, and as the texture would type them. "
           "AUC is over the words alone (0.50: they read like you)."]
    for part, fams in data["parts"].items():
        out += ["", PARTS[part]]
        for family, r in fams.items():
            out.append(f"  {FAMILY_LABELS[family]}: {r['him']['lines']} of your lines, "
                       f"{r['stored']['lines']} of theirs; AUC {_two(r['auc_stored'])} stored, "
                       f"{_two(r['auc_typed'])} typed again")
            for key in MECHANICAL:
                out.append(f"    {LABELS[key]}: you {_pct(r['him'][key])}, stored "
                           f"{_pct(r['stored'][key])}, typed again {_pct(r['typed'][key])}")
            out.append(f"    contractions typed without their apostrophe: you "
                       f"{_pct(r['him']['dropped'])}, stored {_pct(r['stored']['dropped'])}, "
                       f"typed again {_pct(r['typed']['dropped'])}")
    s = data["spread"]
    out += ["", f"The habits of the {s['members']} members who spoke: {s['apostrophes']['drop']} "
                f"drop most apostrophes, {s['apostrophes']['mixed']} some, "
                f"{s['apostrophes']['keep']} keep most; {s['texters']} type their one-word "
                f"shorthand as a habit; {s['full_stops']['type']} of the "
                f"{s['full_stops']['declare']} who declare full stops type a line's last one."]
    return "\n".join(out)
