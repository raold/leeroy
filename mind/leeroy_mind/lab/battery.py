"""The battery: owner-style scripts (lab/battery/*.toml, invented wording, no line of his) and the
night's seeded plan of (script, member, channel) pairs.

A script has an id, a kind (KINDS), the channels it may run on (CHANNELS) and one to four of his
lines; a line may say what its answer must hold in her first sentence (`ask`: yesno, number,
name or noun, with `nouns` for the words that count) and use `{name}`, her character name.

The plan draws up to SAMPLE members across the romance stances (a man's is "none"), seeded by the
night, then `pairs` pairs: scripts in a seeded order that spreads the kinds, members in a seeded
order, and one of the script's channels each. Every arm of the night runs the same plan."""
import random
import tomllib
from dataclasses import dataclass
from importlib import resources

KINDS = ("flirt", "logistics", "question", "smalltalk", "goodbye", "pushback", "photo", "silence",
         "raid", "meet")
CHANNELS = ("dm", "whisper", "guild", "party")
ASKS = ("yesno", "number", "name", "noun")
STANCES = ("none", "fond", "drawn", "lust")
SAMPLE = 20
MAX_LINES = 4
MAX_TEXT = 200


@dataclass(frozen=True)
class Line:
    text: str
    ask: str | None = None
    nouns: tuple[str, ...] = ()


@dataclass(frozen=True)
class Script:
    id: str
    kind: str
    channels: tuple[str, ...]
    lines: tuple[Line, ...]


class BatteryError(ValueError):
    """A script that does not parse, in words (its id or file, never his lines)."""


def _line(raw, where: str) -> Line:
    if not isinstance(raw, dict) or set(raw) - {"text", "ask", "nouns"}:
        raise BatteryError(f"{where}: a line is a table of text, ask and nouns")
    text, ask, nouns = raw.get("text"), raw.get("ask"), raw.get("nouns", [])
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT or "\n" in text:
        raise BatteryError(f"{where}: a line's text is one line of 1-{MAX_TEXT} characters")
    if "{" in text.replace("{name}", ""):
        raise BatteryError(f"{where}: only {{name}} may be filled in")
    if ask is not None and ask not in ASKS:
        raise BatteryError(f"{where}: ask is one of {', '.join(ASKS)}")
    if (not isinstance(nouns, list) or not all(isinstance(n, str) and n.strip() for n in nouns)
            or ask == "noun" and not nouns):
        raise BatteryError(f"{where}: nouns is a list of words (needed for ask = noun)")
    return Line(text.strip(), ask, tuple(n.strip().casefold() for n in nouns))


def parse(doc: dict, where: str = "battery") -> list[Script]:
    scripts = doc.get("script") if isinstance(doc, dict) else None
    if not isinstance(scripts, list) or set(doc) - {"script"}:
        raise BatteryError(f"{where}: a battery file is a list of [[script]] tables")
    out = []
    for raw in scripts:
        if not isinstance(raw, dict) or set(raw) != {"id", "kind", "channels", "lines"}:
            raise BatteryError(f"{where}: a script has id, kind, channels and lines")
        sid, kind, channels, lines = raw["id"], raw["kind"], raw["channels"], raw["lines"]
        if not isinstance(sid, str) or not sid.replace("-", "").isalnum() or len(sid) > 40:
            raise BatteryError(f"{where}: a script id is letters, digits and dashes")
        if kind not in KINDS:
            raise BatteryError(f"{where} {sid}: kind is one of {', '.join(KINDS)}")
        if (not isinstance(channels, list) or not channels or len(set(channels)) != len(channels)
                or any(c not in CHANNELS for c in channels)):
            raise BatteryError(f"{where} {sid}: channels are some of {', '.join(CHANNELS)}")
        if not isinstance(lines, list) or not 1 <= len(lines) <= MAX_LINES:
            raise BatteryError(f"{where} {sid}: 1-{MAX_LINES} lines")
        out.append(Script(sid, kind, tuple(channels),
                          tuple(_line(x, f"{where} {sid}") for x in lines)))
    return out


def load() -> list[Script]:
    """Every script of the package's battery, sorted by id; ids are unique."""
    out: list[Script] = []
    for entry in sorted(resources.files("leeroy_mind.lab").joinpath("battery").iterdir(),
                        key=lambda e: e.name):
        if not entry.name.endswith(".toml"):
            continue
        try:
            doc = tomllib.loads(entry.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as e:
            raise BatteryError(f"{entry.name}: {e}") from None
        out.extend(parse(doc, entry.name))
    ids = [s.id for s in out]
    if len(ids) != len(set(ids)):
        raise BatteryError("battery: script ids are unique")
    return sorted(out, key=lambda s: s.id)


def sample(members: dict[int, str], seed: str, n: int = SAMPLE) -> list[int]:
    """Up to `n` guids of `members` (guid -> stance), drawn in turn from each stance that has
    any, each stance's members in a seeded order: the same night draws the same members."""
    rng = random.Random(f"{seed}|members")
    pools = {s: sorted(g for g, st in members.items() if (st if st in STANCES else "none") == s)
             for s in STANCES}
    for pool in pools.values():
        rng.shuffle(pool)
    out: list[int] = []
    while len(out) < n and any(pools.values()):
        for s in STANCES:
            if pools[s] and len(out) < n:
                out.append(pools[s].pop())
    return out


def plan(scripts: list[Script], members: dict[int, str], seed: str, pairs: int, *,
         channels: tuple[str, ...] = CHANNELS) -> list[dict]:
    """The night's pairs: [{"pair", "script", "kind", "guid", "channel"}], `pairs` of them at
    most. Scripts go in a seeded order that takes one of each kind before a second; members in
    the sampled order, round and round; the channel is one of the script's (and `channels`)."""
    rng = random.Random(f"{seed}|plan")
    chosen = sample(members, seed)
    usable = [s for s in scripts if set(s.channels) & set(channels)]
    if not chosen or not usable:
        return []
    by_kind: dict[str, list[Script]] = {}
    for s in usable:
        by_kind.setdefault(s.kind, []).append(s)
    for pool in by_kind.values():
        rng.shuffle(pool)
    order: list[Script] = []
    kinds = sorted(by_kind)
    while any(by_kind.values()):
        rng.shuffle(kinds)
        for k in kinds:
            if by_kind[k]:
                order.append(by_kind[k].pop())
    out = []
    for i in range(min(pairs, len(order) * max(1, len(chosen)))):
        script = order[i % len(order)]
        guid = chosen[i % len(chosen)]
        options = [c for c in script.channels if c in channels]
        out.append({"pair": i + 1, "script": script.id, "kind": script.kind, "guid": guid,
                    "channel": rng.choice(options)})
    return out
