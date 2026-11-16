"""Per-line style features, each with a fixed label for the report's words. Text features read
the line alone; the context features (latency, bursts) read its conversation; the hour reads
its time in the given zone. Nothing here keeps or returns text."""
import math
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, tzinfo
from typing import Sequence

from .corpus import Line

LATENCY_CAP_S = 3600.0  # a first line, or one after a longer gap, reads as this
BURST_GAP_S = 300.0     # the same speaker again within this is one burst

# (key, label, kind): kind "share" is a yes/no per line, shown as the share of lines; "median"
# and "seconds" are shown as medians, "hour" as the circular mean hour.
FEATURES: tuple[tuple[str, str, str], ...] = (
    ("words", "words per line", "median"),
    ("apostrophe", "contractions with an apostrophe (i'm, don't)", "share"),
    ("bare", "contractions without one (im, dont)", "share"),
    ("lower", "all-lowercase lines", "share"),
    ("capital", "lines starting with a capital", "share"),
    ("end_period", "lines ending in a full stop", "share"),
    ("end_question", "lines ending in a question mark", "share"),
    ("end_exclaim", "lines ending in !", "share"),
    ("end_ellipsis", "lines ending in ...", "share"),
    ("inner_period", "full stops inside a line (fragment chains)", "share"),
    ("comma", "lines with a comma", "share"),
    ("abbrev", "chat shorthand (u, idk, rn)", "share"),
    ("laugh", "laughter (lol, lmao, haha)", "share"),
    ("emoji", "emoji", "share"),
    ("emoticon", "emoticons like :) or <3", "share"),
    ("question", "questions", "share"),
    ("you", "lines about 'you'", "share"),
    ("addressed", "lines naming someone", "share"),
    ("latency", "seconds after the line before", "seconds"),
    ("hour", "hour of day", "hour"),
    ("followup", "lines that follow the speaker's own line", "share"),
    ("burst", "lines inside a burst of two or more", "share"),
)
LABELS = {key: label for key, label, _ in FEATURES}
KINDS = {key: kind for key, _, kind in FEATURES}
# The model's columns: the hour is two (its sine and cosine), every other feature one.
COLUMNS: tuple[str, ...] = tuple(c for key, _, _ in FEATURES
                                 for c in ((key + "_sin", key + "_cos") if key == "hour" else (key,)))

_TOKEN = re.compile(r"[a-z0-9']+")
APOSTROPHE = frozenset("""i'm don't can't won't didn't doesn't isn't wasn't weren't aren't haven't
    hasn't hadn't couldn't wouldn't shouldn't that's what's it's you're they're we're i've you've
    we've they've i'll you'll we'll they'll he'll she'll i'd you'd he'd she'd we'd they'd let's
    there's here's he's she's who's where's how's ain't y'all""".split())
# Only the forms that are no other word ("its", "were", "ill", "id", "lets" are left out).
BARE = frozenset("""im dont cant wont didnt doesnt isnt wasnt werent arent havent hasnt hadnt
    couldnt wouldnt shouldnt thats whats youre theyre ive youve weve theyve youll theyll youd
    theyd theres heres hes shes whos wheres aint yall""".split())
ABBREV = frozenset("""u ur ppl tho rn idk ngl tbh ty np omg wtf imo imho def prob srs bc cuz coz
    brb afk gg gz grats thx pls plz nvm btw jk irl ya yea dunno gonna wanna gotta lemme tmrw gn
    wb ofc fr smh ikr bruh asap fyi""".split())
_LAUGH = re.compile(r"^(?:lol+|lo+l|lmf?ao+|rofl|xd+|(?:ha){2,}h?|(?:he){2,}h?|heh)$")
YOU = frozenset("you u ur your yours you're youre ya y'all yall you've youve you'll youll you'd youd".split())
_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿⬀-⯿❤♥]")
_EMOTICON = re.compile(r"(?:^|(?<=\s))(?:[:;=][-'^o]?[)(\]\[dDpPoO/\\|3*$@]+|[xX][dD]+|<3+|\^_?\^|"
                       r"[oO]_[oO]|-_-|[tT]_[tT])(?=$|\s|[.,!?])")
_INNER = re.compile(r"[A-Za-z0-9]\.\s+[A-Za-z0-9]")


def tokens(text: str) -> list[str]:
    return _TOKEN.findall(text.casefold().replace("’", "'"))


def text_traits(text: str, *, names=None, own: str = "") -> dict[str, float]:
    """The features of one line's text. `names` is a compiled name pattern (corpus.name_pattern);
    a match that is not `own` (the speaker's name) makes the line one that names someone."""
    words = tokens(text)
    letters = [c for c in text if c.isalpha()]
    end = text.rstrip()
    last = end[-1:] if end else ""
    named = False
    if names is not None:
        own = own.casefold()
        named = any(m.group(1).casefold() != own for m in names.finditer(text))
    return {
        "words": float(len(text.split())),
        "apostrophe": float(any(w in APOSTROPHE for w in words)),
        "bare": float(any(w in BARE for w in words)),
        "lower": float(bool(letters) and not any(c.isupper() for c in letters)),
        "capital": float(bool(letters) and letters[0].isupper()),
        "end_period": float(last == "." and not end.endswith("..")),
        "end_question": float(last == "?"),
        "end_exclaim": float(last == "!"),
        "end_ellipsis": float(end.endswith("..") or last == "…"),
        "inner_period": float(bool(_INNER.search(text))),
        "comma": float("," in text),
        "abbrev": float(any(w in ABBREV for w in words)),
        "laugh": float(any(_LAUGH.match(w) for w in words)),
        "emoji": float(bool(_EMOJI.search(text))),
        "emoticon": float(bool(_EMOTICON.search(text))),
        "question": float("?" in text),
        "you": float(any(w in YOU for w in words)),
        "addressed": float(named),
    }


@dataclass(frozen=True)
class Context:
    latency: float | None  # seconds since the line before in its conversation; None for the first
    followup: bool         # the same speaker's line came just before, within BURST_GAP_S
    burst: bool            # part of such a run of two or more


def contexts(lines: Sequence[Line]) -> list[Context]:
    """Each line's place in its conversation (lines in time order, any conversations mixed)."""
    by_conv: dict[str, list[int]] = defaultdict(list)
    for i, line in enumerate(lines):
        by_conv[line.conv].append(i)
    latency: list[float | None] = [None] * len(lines)
    follow = [False] * len(lines)
    for idx in by_conv.values():
        for a, b in zip(idx, idx[1:]):
            gap = lines[b].ts - lines[a].ts
            latency[b] = gap
            follow[b] = lines[a].speaker == lines[b].speaker and gap <= BURST_GAP_S
    burst = list(follow)
    for idx in by_conv.values():
        for a, b in zip(idx, idx[1:]):
            if follow[b]:
                burst[a] = True
    return [Context(latency[i], follow[i], burst[i]) for i in range(len(lines))]


def hour_of(ts: float, tz: tzinfo | None) -> float:
    t = datetime.fromtimestamp(ts, tz) if tz is not None else datetime.fromtimestamp(ts)
    return t.hour + t.minute / 60


@dataclass
class Table:
    """The model's columns (COLUMNS order, one value per line) and the raw values the report
    shows: words, latency (None for a first line), the hour and each yes/no feature."""
    columns: list[list[float]]
    raw: dict[str, list]


def table(lines: Sequence[Line], *, names=None, names_by_guid: dict[int, str] | None = None,
          tz: tzinfo | None = None) -> Table:
    names_by_guid = names_by_guid or {}
    ctx = contexts(lines)
    raw: dict[str, list] = {key: [] for key, _, _ in FEATURES}
    for line, c in zip(lines, ctx):
        traits = text_traits(line.text, names=names, own=names_by_guid.get(line.speaker, ""))
        for key, value in traits.items():
            raw[key].append(value)
        raw["latency"].append(c.latency)
        raw["hour"].append(hour_of(line.ts, tz))
        raw["followup"].append(float(c.followup))
        raw["burst"].append(float(c.burst))
    columns: list[list[float]] = []
    for key, _, _ in FEATURES:
        if key == "words":
            columns.append([math.log1p(v) for v in raw[key]])
        elif key == "latency":
            columns.append([math.log1p(min(LATENCY_CAP_S, v if v is not None else LATENCY_CAP_S))
                            for v in raw[key]])
        elif key == "hour":
            columns.append([math.sin(2 * math.pi * h / 24) for h in raw[key]])
            columns.append([math.cos(2 * math.pi * h / 24) for h in raw[key]])
        else:
            columns.append(list(raw[key]))
    return Table(columns, raw)
