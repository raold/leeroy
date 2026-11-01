"""The persona schema: frozen dataclasses, one per group of fields. Parsing, validation and the
JSON schema are all generated from the annotations, so a field is declared exactly once.

`persona_from_dict` checks shape and enums only (raising PersonaError with dotted paths);
`validate_persona` checks ranges, lengths and content rules and returns messages as
"path: problem" so a caller can show every problem at once."""
import math
import types
import typing as _t
from dataclasses import MISSING, dataclass, field, fields, is_dataclass, replace
from typing import Literal, get_args, get_origin, get_type_hints

from ..text import has_meta

SCHEMA_VERSION = 1
MIN_AGE = 18
DEFAULT_MAX_LEN = 300
MM_DD_PATTERN = "^(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])$"  # the JSON schema's; see _is_mm_dd


class PersonaError(ValueError):
    def __init__(self, errors: list[str]):
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


def _f(*, lo=None, hi=None, min_items=None, max_items=None, max_len=None, doc=None,
       mm_dd=False, enrichment=False, default=MISSING):
    """doc: a description the JSON schema carries, which the model reads with it. mm_dd: a
    month and day as MM-DD (the strings, or each string of a list). enrichment: a field the
    nightly looks pass fills (P1c Task 7), which the JSON schema leaves out unless asked."""
    meta = {"lo": lo, "hi": hi, "min_items": min_items, "max_items": max_items, "max_len": max_len,
            "doc": doc, "mm_dd": mm_dd, "enrichment": enrichment}
    if default is MISSING:
        return field(metadata=meta)
    return field(default=default, metadata=meta)


@dataclass(frozen=True)
class Voice:
    base: tuple[str, ...] = _f(max_items=4, max_len=60)
    speed: float = _f(lo=0.5, hi=2.0, default=1.0)


@dataclass(frozen=True)
class Identity:
    real_name: str = _f(max_len=40)
    age: int = _f(lo=MIN_AGE, hi=99)
    player_gender: Literal["female", "male"]
    region: Literal["UK", "US"]
    accent: Literal["en-gb", "en-us"]
    voice: Voice


@dataclass(frozen=True)
class Trait:
    score: int = _f(lo=1, hi=10)
    behaviour: str = _f(max_len=160)


@dataclass(frozen=True)
class BigFive:
    openness: Trait
    conscientiousness: Trait
    extraversion: Trait
    agreeableness: Trait
    neuroticism: Trait


# Which way round typing goes (rehearsal 1 found most stored maps backwards): the styler looks
# for each key in what the model wrote and types its value instead, so a key is the full form
# and its value the shorter chat form. Both are plain ASCII: the 3.3.5 client shows no emoji.
ABBREVIATIONS_DOC = ('each full word or phrase mapped to the shorter way this person types it, '
                     'e.g. {"you": "u", "laughing out loud": "lol"}')
# Marks only (fix wave G1, from rehearsal 2: "lmao", "gg", "aww" and "brb" sat among the
# emoticons and were bolted onto finished lines). A chat word belongs in abbreviations or
# speech_habits.
EMOTICONS_DOC = ('plain-text marks such as ":)", ";)", ":D", "xD", ":P", "<3" or "o7", never '
                 'emoji and never words like lol or gg (those go in speech_habits)')
TYPING_RULE = (f'In "typing", "abbreviations" holds {ABBREVIATIONS_DOC}, and "emoticons" are '
               f'{EMOTICONS_DOC}.')


@dataclass(frozen=True)
class Typing:
    case: Literal["lower", "mixed", "proper"]
    end_punct: Literal["none", "some", "full"]
    emoticons: tuple[str, ...] = _f(max_items=6, max_len=12, doc=EMOTICONS_DOC)
    emoticon_rate: float = _f(lo=0.0, hi=1.0)
    abbreviations: dict[str, str] = _f(max_items=12, max_len=20, doc=ABBREVIATIONS_DOC)
    typo_rate: float = _f(lo=0.0, hi=0.15)
    words_per_line: tuple[int, ...] = _f(lo=1, hi=60, min_items=2, max_items=2)
    split_rate: float = _f(lo=0.0, hi=1.0)
    chars_per_sec: float = _f(lo=3.0, hi=8.0)


@dataclass(frozen=True)
class RealLife:
    job: str = _f(max_len=80)
    rhythm: str = _f(max_len=120)
    household: str = _f(max_len=120)
    money: str = _f(max_len=120)
    sleep: str = _f(max_len=120)
    life_thread: str = _f(max_len=200)
    birthday: str = _f(max_len=5, mm_dd=True)
    night_owl: bool


@dataclass(frozen=True)
class Habit:
    substance: Literal["alcohol", "weed", "caffeine", "tobacco"]
    when: Literal["raid_nights", "weekends", "days_off", "after_midnight", "daily"]
    odds: float = _f(lo=0.0, hi=1.0)
    max_state: Literal["tipsy", "drunk", "high", "wired"]


@dataclass(frozen=True)
class Romance:
    stance: Literal["none", "fond", "drawn", "lust"]
    style: Literal["bold", "shy", "earnest", "theatrical", "guarded", "competitive"] | None
    jealousy_mode: Literal["good_sport", "sulker", "grumbler", "teaser", "play_by_play"] | None
    dial: int | None = _f(lo=1, hi=10, default=None)  # hers alone, set by hand; None: mind.toml's
    drawn_to: tuple[str, ...] = _f(max_items=6, max_len=80, default=(), enrichment=True)
    turned_off_by: tuple[str, ...] = _f(max_items=6, max_len=80, default=(), enrichment=True)


# how a man takes it when someone else gets the human's attention (Romance.jealousy_mode)
JEALOUSY_MODES = ("good_sport", "sulker", "grumbler", "teaser", "play_by_play")


@dataclass(frozen=True)
class Game:
    motivation: Literal["achiever", "social", "explorer", "competitor"]
    thinks_as: Literal["theorycrafter", "lore", "social", "collector", "competitor"]
    wants: tuple[str, ...] = _f(max_items=4, max_len=100)
    outside_raids: tuple[Literal["battlegrounds", "arena", "gathering", "crafting", "dungeons",
                                 "idling", "auction_house", "fishing", "collecting"], ...] = \
        _f(max_items=4)
    favourite_spot: str = _f(max_len=80)
    years_played: int = _f(lo=3, hi=30)


@dataclass(frozen=True)
class Play:
    main_spec: str = _f(max_len=30)
    off_spec: str = _f(max_len=30)
    raid_role: Literal["tank", "healer", "melee", "ranged"]
    skill: Literal["solid", "sloppy", "clutch"]
    guild_role: Literal["officer", "raider", "social"]
    officer_title: str = _f(max_len=30)


@dataclass(frozen=True)
class SampleLine:
    text: str = _f(max_len=255)
    approved: bool


@dataclass(frozen=True)
class Bit:
    id: str = _f(max_len=40)
    instruction: str = _f(max_len=300)
    min_days_between: float = _f(lo=0.0, hi=365.0)
    voice_bit: bool
    contexts: tuple[Literal["any", "org", "trash", "after_kill", "after_wipe", "late_night",
                            "loot"], ...] = _f(max_items=7)
    lines: int = _f(lo=1, hi=3)
    dates: tuple[str, ...] = _f(max_items=4, max_len=5, mm_dd=True)


@dataclass(frozen=True)
class Relationship:
    other: str = _f(max_len=40)
    kind: Literal["friend", "friction", "stance"]
    note: str = _f(max_len=200)


# The looks and the disclosure ladder (P1c Task 7), written by the nightly looks pass and
# reviewable by hand like every other field. Optional: a persona without them still loads.
@dataclass(frozen=True)
class Appearance:
    height_cm: int = _f(lo=140, hi=210)
    build: Literal["slim", "athletic", "average", "curvy", "stocky", "heavy"]
    weight_kg: int = _f(lo=40, hi=180)
    hair_colour: str = _f(max_len=30)
    hair_length: Literal["shaved", "short", "chin", "shoulder", "long", "very_long"]
    hair_style: str = _f(max_len=40)
    skin_tone: str = _f(max_len=30)
    eye_colour: str = _f(max_len=20)
    face: tuple[str, ...] = _f(max_items=4, max_len=40)      # freckles, glasses, piercings, tattoos, a beard
    at_the_pc: str = _f(max_len=120)                         # what they wear at the keyboard


@dataclass(frozen=True)
class Disclosure:
    depth: Literal["surface", "middle", "deep"]
    text: str = _f(max_len=160)


DEPTHS = get_args(get_type_hints(Disclosure)["depth"])  # the order the layers open in


@dataclass(frozen=True)
class Persona:
    guid: int = _f(lo=1)
    character_name: str = _f(max_len=12)
    core: bool
    identity: Identity
    big_five: BigFive
    attachment: Literal["secure", "anxious", "avoidant"]
    humour: Literal["dry", "self-deprecating", "absurd", "teasing", "puns", "dark"]
    typing: Typing
    real_life: RealLife
    habits: tuple[Habit, ...] = _f(max_items=4)
    romance: Romance
    game: Game
    play: Play
    opinions: tuple[str, ...] = _f(min_items=3, max_items=5, max_len=160)
    knowledge_gaps: tuple[str, ...] = _f(max_items=5, max_len=120)
    speech_habits: tuple[str, ...] = _f(max_items=5, max_len=100)
    likes: tuple[str, ...] = _f(max_items=6, max_len=80)
    dislikes: tuple[str, ...] = _f(max_items=6, max_len=80)
    values: tuple[str, ...] = _f(max_items=5, max_len=80)
    nevers: tuple[str, ...] = _f(max_items=5, max_len=120)
    secret: str = _f(max_len=300)
    sample_lines: tuple[SampleLine, ...] = _f(max_items=12)
    bits: tuple[Bit, ...] = _f(max_items=2)
    relationships: tuple[Relationship, ...] = _f(max_items=8)
    mock_interview: str = _f(max_len=4000, default="")
    appearance: Appearance | None = _f(default=None, enrichment=True)
    disclosures: tuple[Disclosure, ...] = _f(max_items=4, default=(), enrichment=True)


# -- type helpers ---------------------------------------------------------------------------

def _hints(cls) -> dict:
    return get_type_hints(cls)


def _is_union(t) -> bool:
    return get_origin(t) in (_t.Union, types.UnionType)


def _optional_inner(t):
    args = get_args(t)
    non_none = [a for a in args if a is not type(None)]
    return non_none[0] if len(args) == 2 and len(non_none) == 1 else None


def _has_default(f) -> bool:
    return f.default is not MISSING


def _type_name(t) -> str:
    if t in (list, tuple):
        return "a list"
    if t is dict or is_dataclass(t):
        return "an object"
    return {int: "an integer", float: "a number", str: "a string", bool: "true or false"}[t]


# -- dict -> dataclass ----------------------------------------------------------------------

def _parse(t, v, path: str, errs: list[str]):
    """Return the parsed value, or None after appending to errs."""
    if _is_union(t):
        if v is None:
            return None
        return _parse(_optional_inner(t), v, path, errs)
    origin = get_origin(t)
    if origin is Literal:
        opts = get_args(t)
        if isinstance(v, str) and v in opts:
            return v
        errs.append(f"{path}: must be one of {', '.join(map(str, opts))}")
        return None
    if t is bool:
        if isinstance(v, bool):
            return v
    elif t is int:
        if isinstance(v, int) and not isinstance(v, bool):
            return v
    elif t is float:
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
    elif t is str:
        if isinstance(v, str):
            return v
    elif origin is tuple:
        if isinstance(v, list):
            item_t = get_args(t)[0]
            out, ok = [], True
            for i, x in enumerate(v):
                n = len(errs)
                out.append(_parse(item_t, x, f"{path}[{i}]", errs))
                ok = ok and len(errs) == n
            return tuple(out) if ok else None
    elif origin is dict:
        if isinstance(v, dict):
            vt = get_args(t)[1]
            out, ok = {}, True
            for k, x in v.items():
                n = len(errs)
                out[k] = _parse(vt, x, f"{path}.{k}", errs)
                ok = ok and len(errs) == n
            return out if ok else None
    elif is_dataclass(t):
        if isinstance(v, dict):
            return _parse_obj(t, v, path, errs)
    else:  # a schema bug, not an input problem
        raise TypeError(f"unsupported schema type {t!r}")
    errs.append(f"{path}: must be {_type_name(origin or t)}")
    return None


def _parse_obj(cls, d: dict, path: str, errs: list[str]):
    hints = _hints(cls)
    prefix = f"{path}." if path else ""
    known = {f.name for f in fields(cls)}
    extra = set(d) - known - ({"schema"} if cls is Persona else set())
    for k in sorted(extra):
        errs.append(f"{prefix}{k}: unknown field")
    kwargs, ok = {}, not extra
    for f in fields(cls):
        p = f"{prefix}{f.name}"
        if f.name not in d:
            if _has_default(f):
                continue
            errs.append(f"{p}: missing")
            ok = False
            continue
        n = len(errs)
        kwargs[f.name] = _parse(hints[f.name], d[f.name], p, errs)
        if len(errs) != n:
            ok = False
    return cls(**kwargs) if ok else None


def persona_from_dict(d) -> Persona:
    if not isinstance(d, dict):
        raise PersonaError(["persona: must be an object"])
    errs: list[str] = []
    v = d.get("schema")
    if type(v) is not int or v != SCHEMA_VERSION:  # not True, not 1.0
        errs.append(f"schema: must be {SCHEMA_VERSION}")
    p = _parse_obj(Persona, d, "", errs)
    if errs:
        raise PersonaError(errs)
    return _words_out_of_emoticons(p)


# -- emoticons are marks, chat words are words ----------------------------------------------

# Chat words a stored persona may hold among its emoticons, and the full form each shortens.
CHAT_WORD_FORMS = {"lol": "laughing out loud", "lmao": "laughing my ass off",
                   "rofl": "rolling on the floor laughing", "omg": "oh my god",
                   "brb": "be right back", "gg": "good game", "ily": "i love you",
                   "ty": "thank you", "np": "no problem", "jk": "just kidding",
                   "idk": "i don't know", "afk": "away from keyboard", "nvm": "never mind"}
def is_mark(e: str) -> bool:
    """A typed emoticon (":)", ";)", "xD", "xDDD", "<3", "o7"), not a chat word ("lol",
    "aww"), also with the punctuation typed after it ("gg!", "ty.": fix round 1, review minor
    4)."""
    t = e.strip()
    if t.rstrip(".,!?").isalpha():
        t = t.rstrip(".,!?")
    if not t:
        return False
    low = t.casefold()
    if low == "o7" or (len(low) >= 2 and low[0] == "x" and set(low[1:]) <= {"d"}) \
            or low in ("xp", "xpp"):
        return True
    return not t.isalpha() and not any(
        len(run) >= 3 for run in "".join(c if c.isalpha() else " " for c in t).split())


def _words_out_of_emoticons(p: Persona) -> Persona:
    """A stored persona with chat words among its emoticons still loads, and saves: each
    word is read as a word, into abbreviations with its full form when it has one that fits
    the key's length and there is room, else into one speech habit ('types "aww" in chat')
    as long as the habit's length allows and there is room, else left out (fix round 1,
    review I7: "rofl" made a 29-character key). The emoticons keep only the marks."""
    t = p.typing
    words = [e for e in t.emoticons if e.strip() and _plain(e) and not is_mark(e)]
    if not words:
        return p
    abbreviations, habits, rest = dict(t.abbreviations), list(p.speech_habits), []
    key_len = _meta(Typing, "abbreviations", "max_len")
    for w in words:
        full = CHAT_WORD_FORMS.get(w.strip().casefold())
        if (full and len(full) <= key_len and len(w.strip()) <= key_len
                and full not in {k.casefold() for k in abbreviations}
                and len(abbreviations) < _meta(Typing, "abbreviations", "max_items")):
            abbreviations[full] = w.strip()
        else:
            rest.append(w.strip())
    habit_len = _meta(Persona, "speech_habits", "max_len")
    while rest and len(_typed_habit(rest)) > habit_len:
        rest.pop()
    if rest and len(habits) < _meta(Persona, "speech_habits", "max_items"):
        habits.append(_typed_habit(rest))
    typing = replace(t, emoticons=tuple(e for e in t.emoticons if e not in words),
                     abbreviations=abbreviations)
    return replace(p, typing=typing, speech_habits=tuple(habits))


def _typed_habit(words: list[str]) -> str:
    return "types " + " and ".join(f'"{w}"' for w in words) + " in chat"


def _meta(cls, name: str, key: str) -> int:
    return next(f.metadata[key] for f in fields(cls) if f.name == name)


# -- dataclass -> dict ----------------------------------------------------------------------

def _dump(v):
    if is_dataclass(v):
        return {f.name: _dump(getattr(v, f.name)) for f in fields(v)}
    if isinstance(v, (tuple, list)):
        return [_dump(x) for x in v]
    if isinstance(v, dict):
        return {k: _dump(x) for k, x in v.items()}
    return v


def persona_to_dict(p: Persona) -> dict:
    return {"schema": SCHEMA_VERSION, **_dump(p)}


# -- validation -----------------------------------------------------------------------------

class _Elem:
    """Element-level constraints taken from the container's field: bounds and max_len apply to
    the elements, item counts to the container."""
    def __init__(self, f):
        m = f.metadata if f is not None else {}
        self.metadata = {"lo": m.get("lo"), "hi": m.get("hi"), "max_len": m.get("max_len"),
                         "mm_dd": m.get("mm_dd")}


def _check(t, f, v, path: str, errs: list[str]) -> None:
    meta = f.metadata if f is not None else {}
    if _is_union(t):
        if v is not None:
            _check(_optional_inner(t), f, v, path, errs)
        return
    origin = get_origin(t)
    if t is bool or origin is Literal:
        return
    if t is float and not math.isfinite(v):
        errs.append(f"{path}: must be a finite number")
    elif t in (int, float):
        lo, hi = meta.get("lo"), meta.get("hi")
        if lo is not None and v < lo:
            errs.append(f"{path}: must be at least {lo}")
        if hi is not None and v > hi:
            errs.append(f"{path}: must be at most {hi}")
    elif t is str:
        cap = meta.get("max_len") or DEFAULT_MAX_LEN
        if len(v) > cap:
            errs.append(f"{path}: must be at most {cap} characters")
        if has_meta(v):
            errs.append(f"{path}: reads like an assistant, not a person")
        if meta.get("mm_dd") and not _is_mm_dd(v):
            errs.append(f"{path}: must look like MM-DD")
    elif origin is tuple or origin is dict:
        mn, mx = meta.get("min_items"), meta.get("max_items")
        if mn is not None and len(v) < mn:
            errs.append(f"{path}: must have at least {mn} items")
        if mx is not None and len(v) > mx:
            errs.append(f"{path}: must have at most {mx} items")
        sub = _Elem(f)
        if origin is tuple:
            for i, x in enumerate(v):
                _check(get_args(t)[0], sub, x, f"{path}[{i}]", errs)
        else:
            for k, x in v.items():
                _check(str, sub, k, f"{path}.{k}", errs)
                _check(get_args(t)[1], sub, x, f"{path}.{k}", errs)
    elif is_dataclass(t):
        _check_obj(v, path, errs)


def _check_obj(obj, path: str, errs: list[str]) -> None:
    hints = _hints(type(obj))
    prefix = f"{path}." if path else ""
    for f in fields(obj):
        _check(hints[f.name], f, getattr(obj, f.name), f"{prefix}{f.name}", errs)


def _is_mm_dd(s: str) -> bool:
    if len(s) != 5 or s[2] != "-" or not (s[:2] + s[3:]).isdigit():
        return False
    m, d = int(s[:2]), int(s[3:])
    return 1 <= m <= 12 and 1 <= d <= (29 if m == 2 else 30 if m in (4, 6, 9, 11) else 31)


def validate_persona(p: Persona) -> list[str]:
    errs: list[str] = []
    _check_obj(p, "", errs)
    wpl = p.typing.words_per_line
    if len(wpl) == 2 and wpl[0] > wpl[1]:
        errs.append("typing.words_per_line: minimum must not exceed maximum")
    for i, r in enumerate(p.relationships):
        if r.other == p.character_name:
            errs.append(f"relationships[{i}].other: must be someone else or @human")
    errs += _typing_errors(p.typing)
    errs += _enrichment_errors(p)
    return errs


def _enrichment_errors(p: Persona) -> list[str]:
    """Men stay friends (owner, 2026-10-30), so a man has no tastes; the disclosure layers run
    surface, then middle, then deep, with one deep layer (P1c Task 7)."""
    errs = []
    if p.identity.player_gender == "male":
        errs += [f"romance.{name}: men stay friends; leave it empty"
                 for name in ("drawn_to", "turned_off_by") if getattr(p.romance, name)]
    ranks = [DEPTHS.index(d.depth) for d in p.disclosures if d.depth in DEPTHS]
    if ranks != sorted(ranks):
        errs.append("disclosures: depths must run surface, then middle, then deep")
    if p.disclosures and sum(d.depth == "deep" for d in p.disclosures) != 1:
        errs.append("disclosures: exactly one deep layer")
    return errs


def _plain(s: str) -> bool:
    return bool(s.strip()) and s.isascii() and s.isprintable()


def _typing_errors(t: Typing) -> list[str]:
    errs = []
    for full, chat in t.abbreviations.items():
        f, c = full.strip(), chat.strip()
        if not (_plain(f) and _plain(c)) or len(c) >= len(f) or c.casefold() == f.casefold():
            errs.append(f'typing.abbreviations.{full}: map the full form to the shorter way '
                        f'they type it, e.g. "laughing out loud": "lol"')
    for i, e in enumerate(t.emoticons):
        if not _plain(e):
            errs.append(f"typing.emoticons[{i}]: plain text only, like :) or xD (the game cannot "
                        "show emoji)")
        elif not is_mark(e):
            errs.append(f"typing.emoticons[{i}]: a chat word, not a mark: put it in "
                        "speech_habits, or in abbreviations with its full form")
    return errs


# -- JSON schema ----------------------------------------------------------------------------

def _js(t, f, enrichment: bool = False) -> dict:
    meta = f.metadata if f is not None else {}
    out = _js_type(t, f, meta, enrichment)
    if meta.get("doc"):
        out["description"] = meta["doc"]
    return out


def _js_type(t, f, meta, enrichment: bool) -> dict:
    if _is_union(t):
        return {"anyOf": [_js(_optional_inner(t), f, enrichment), {"type": "null"}]}
    origin = get_origin(t)
    if origin is Literal:
        return {"enum": list(get_args(t))}
    if t is bool:
        return {"type": "boolean"}
    if t is int or t is float:
        out = {"type": "integer" if t is int else "number"}
        if meta.get("lo") is not None:
            out["minimum"] = meta["lo"]
        if meta.get("hi") is not None:
            out["maximum"] = meta["hi"]
        return out
    if t is str:
        out = {"type": "string", "maxLength": meta.get("max_len") or DEFAULT_MAX_LEN}
        if meta.get("mm_dd"):
            out["pattern"] = MM_DD_PATTERN
        return out
    if origin is tuple:
        out = {"type": "array", "items": _js(get_args(t)[0], _Elem(f), enrichment)}
        if meta.get("min_items") is not None:
            out["minItems"] = meta["min_items"]
        if meta.get("max_items") is not None:
            out["maxItems"] = meta["max_items"]
        return out
    if origin is dict:
        out = {"type": "object",
               "additionalProperties": _js(get_args(t)[1], _Elem(f), enrichment)}
        if meta.get("max_len") is not None:
            out["propertyNames"] = {"maxLength": meta["max_len"]}
        if meta.get("max_items") is not None:
            out["maxProperties"] = meta["max_items"]
        return out
    return _js_obj(t, enrichment)


def _js_obj(cls, enrichment: bool = False) -> dict:
    """The fields marked enrichment are left out unless `enrichment` is set."""
    hints = _hints(cls)
    shown = [f for f in fields(cls) if enrichment or not f.metadata.get("enrichment")]
    return {
        "type": "object",
        "properties": {f.name: _js(hints[f.name], f, enrichment) for f in shown},
        "required": [f.name for f in shown if not _has_default(f)],
        "additionalProperties": False,
    }


def persona_json_schema(*, enrichment: bool = False) -> dict:
    """The schema P1b's generator and seeder send (as the format and in the prompt). It leaves
    out the looks, tastes and disclosure layers, which only the nightly looks pass writes
    (controller ruling I9), unless `enrichment` is set."""
    s = _js_obj(Persona, enrichment)
    s["properties"] = {"schema": {"const": SCHEMA_VERSION}, **s["properties"]}
    s["required"] = ["schema", *s["required"]]
    return s
