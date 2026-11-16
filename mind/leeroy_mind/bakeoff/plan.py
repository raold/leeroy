"""Which captured turns a bake-off asks again, read from mind.db opened read-only.

A turn counts only when every line it became is in the Turing corpus (turing.corpus: typed, not
spoken, not deleted, one family and one conversation), so its lines can be swapped for a
candidate's in exactly the data the judge measures. First come the blind judge's windows
(judge.segments, 8-12 lines of one conversation, alternating windows with and without his lines),
taken only where every member line in the window belongs to a whole captured turn, so all of
them can be swapped; their turns are asked again. Then single turns fill the rest of the budget,
drawn evenly across the families and, within one, across the members (the stylometry caps any
member at an equal share). Seeded: the same database and seed give the same plan."""
import json
import random
from collections import defaultdict
from dataclasses import dataclass, field

from leeroy_mind.turing import judge
from leeroy_mind.turing.corpus import FAMILIES, Line

from .capture import available

KINDS = ("whisper", "dm", "gdm", "discord", "guild", "officer", "party", "raid", "say", "yell")
CONTEXT_LINES = 6       # the sheet's context: his line and the chat before it
HIM = "you"             # how the sheet labels his own lines


def kind_of(channel: str) -> str:
    """The bake-off's kind of a talk channel id (--kinds)."""
    if channel.startswith("dm:"):
        return "dm"
    if channel.startswith("gdm:"):
        return "gdm"
    if channel.startswith("discord:"):
        return "discord"
    if channel == "raid_warning":
        return "raid"
    if channel.startswith("voice:"):
        return "voice"
    return channel


@dataclass(frozen=True)
class Captured:
    id: int
    ts: float
    refs: tuple[str, ...]
    guid: int
    channel: str
    turn_kind: str
    model: str
    think: bool | None
    options: dict
    messages: list
    reply: str
    shape: dict


def read_captured(conn, *, since: float, until: float, ids=None) -> list[Captured]:
    if not available(conn):
        return []
    sql = ("SELECT id, ts, refs, guid, channel, kind, model, think, options, messages, reply, shape "
           "FROM bakeoff_turn WHERE ts >= ? AND ts < ?")
    args: tuple = (since, until)
    if ids is not None:
        sql = ("SELECT id, ts, refs, guid, channel, kind, model, think, options, messages, reply, "
               "shape FROM bakeoff_turn WHERE id IN (SELECT value FROM json_each(?))")
        args = (json.dumps(sorted(ids)),)
    out = []
    for r in conn.execute(sql + " ORDER BY id", args):
        try:
            refs = tuple(x for x in json.loads(r[2]) if isinstance(x, str))
            options, messages, shape = json.loads(r[8]), json.loads(r[9]), json.loads(r[11])
        except (ValueError, TypeError):
            continue
        if not isinstance(options, dict) or not isinstance(messages, list) or not isinstance(shape, dict):
            continue
        out.append(Captured(r[0], r[1], refs, r[3], r[4], r[5], r[6],
                            None if r[7] is None else bool(r[7]), options, messages, r[10] or "",
                            shape))
    return out


@dataclass
class TurnPlan:
    captured: Captured
    family: str
    kind: str
    lines: list[Line]                # the corpus lines it became, in order
    answers: bool                    # the line just before it is his
    context: list[Line]              # the conversation before it, oldest first
    labels: dict[int, str] = field(default_factory=dict)  # guid -> the sheet's speaker label

    @property
    def id(self) -> int:
        return self.captured.id

    @property
    def refs(self) -> tuple[str, ...]:
        return self.captured.refs


@dataclass
class Plan:
    turns: dict[int, TurnPlan]       # in the order they are asked
    windows: list[dict]              # {unit, family, refs, turns}
    lines: list[Line]                # the corpus the run measures
    since: float
    until: float
    seed: str
    by_ref: dict[str, Line] = field(default_factory=dict)

    def __post_init__(self):
        self.by_ref = {line.ref: line for line in self.lines}


def scorable(captured: list[Captured], lines: list[Line], kinds) -> dict[int, Captured]:
    by_ref = {line.ref: line for line in lines}
    out = {}
    for c in captured:
        if kind_of(c.channel) not in kinds or not c.refs:
            continue
        found = [by_ref.get(r) for r in c.refs]
        if any(x is None or x.human or x.speaker != c.guid for x in found):
            continue
        if len({x.family for x in found}) != 1 or len({x.conv for x in found}) != 1:
            continue
        out[c.id] = c
    return out


def _window_candidates(segs, size, with_his, used, owner, turns) -> dict[str, list[list[Line]]]:
    out: dict[str, list[list[Line]]] = {}
    for seg in segs:
        for s in range(len(seg) - size + 1):
            win = seg[s:s + size]
            his = sum(line.human for line in win)
            if (his > 0) != with_his or his == size:
                continue
            if len({line.speaker for line in win}) < 2 or any(line.ref in used for line in win):
                continue
            members = [line for line in win if not line.human]
            if any(line.ref not in owner for line in members):
                continue
            inside = {line.ref for line in win}
            if any(r not in inside for line in members for r in turns[owner[line.ref]].refs):
                continue  # a turn cut by the window's edge
            out.setdefault(win[0].family, []).append(win)
    return out


def _windows(lines, turns, n, budget, rng) -> tuple[list[dict], list[int]]:
    owner = {r: c.id for c in turns.values() for r in c.refs}
    segs = judge.segments(lines)
    used: set[str] = set()
    windows, chosen = [], []
    for i in range(max(0, n)):
        size = rng.randint(judge.WINDOW_MIN, judge.WINDOW_MAX)
        found: dict = {}
        for with_his in (i % 2 == 0, i % 2 != 0):
            found = _window_candidates(segs, size, with_his, used, owner, turns)
            if found:
                break
        if not found:
            break
        family = rng.choice(sorted(found))
        win = rng.choice(found[family])
        ids = list(dict.fromkeys(owner[line.ref] for line in win if not line.human))
        if len(chosen) + len(ids) > budget:
            break
        used.update(line.ref for line in win)
        chosen += ids
        windows.append({"unit": f"w{len(windows) + 1}", "family": family,
                        "refs": [line.ref for line in win], "turns": ids})
    return windows, chosen


def _fill(turns, lines, chosen, budget, rng) -> list[int]:
    family_of = {line.ref: line.family for line in lines}
    pools: dict[str, dict[int, list[int]]] = defaultdict(lambda: defaultdict(list))
    taken = set(chosen)
    for c in sorted(turns.values(), key=lambda c: c.id):
        if c.id not in taken:
            pools[family_of[c.refs[0]]][c.guid].append(c.id)
    queues = {}
    for family in sorted(pools):
        members = sorted(pools[family])
        rng.shuffle(members)
        for g in members:
            rng.shuffle(pools[family][g])
        order = []
        while any(pools[family][g] for g in members):
            for g in members:
                if pools[family][g]:
                    order.append(pools[family][g].pop())
        queues[family] = order
    out = list(chosen)
    families = [f for f in FAMILIES if f in queues]
    while len(out) < budget and any(queues[f] for f in families):
        for f in families:
            if queues[f] and len(out) < budget:
                out.append(queues[f].pop(0))
    return out


def _conversations(lines: list[Line]) -> tuple[dict[str, list[Line]], dict[str, int]]:
    convs: dict[str, list[Line]] = defaultdict(list)
    for line in lines:
        convs[line.conv].append(line)
    index = {line.ref: i for conv in convs.values() for i, line in enumerate(conv)}
    return convs, index


def _context(conv: list[Line], at: int) -> list[Line]:
    out: list[Line] = []
    j = at - 1
    while j >= 0 and len(out) < CONTEXT_LINES:
        nxt = out[0] if out else conv[at]
        if nxt.ts - conv[j].ts > judge.WINDOW_GAP_S:
            break
        out.insert(0, conv[j])
        j -= 1
    return out


def _labels(context: list[Line], speaker: int, human: int) -> dict[int, str]:
    labels = {human: HIM, speaker: "P1"}
    for line in context:
        if line.speaker not in labels:
            labels[line.speaker] = f"P{len(labels)}"
    return labels


def build(captured: list[Captured], lines: list[Line], *, human: int, turns: int, windows: int,
          kinds, seed: str, since: float, until: float, fixed: dict | None = None) -> Plan:
    """The plan; `fixed` ({"turns": [...], "windows": [...]}, a stored plan) keeps a resumed
    run on the turns and windows it began with."""
    by_ref = {line.ref: line for line in lines}
    if fixed is not None:
        wanted = [int(t) for t in fixed["turns"]]
        pool = {c.id: c for c in captured if c.id in set(wanted)
                and all(r in by_ref for r in c.refs)}
        order = [t for t in wanted if t in pool]
        chosen_windows = [w for w in fixed["windows"] if all(t in pool for t in w["turns"])
                          and all(r in by_ref for r in w["refs"])]
    else:
        pool = scorable(captured, lines, kinds)
        rng = random.Random(f"bakeoff|{seed}")
        chosen_windows, chosen = _windows(lines, pool, windows, turns, rng)
        order = _fill(pool, lines, chosen, turns, rng)
    convs, index = _conversations(lines)
    plans: dict[int, TurnPlan] = {}
    for tid in order:
        c = pool[tid]
        own = [by_ref[r] for r in c.refs]
        conv = convs[own[0].conv]
        at = index[own[0].ref]
        context = _context(conv, at)
        answers = bool(context) and context[-1].human
        plans[tid] = TurnPlan(c, own[0].family, kind_of(c.channel), own, answers, context,
                              _labels(context, c.guid, human))
    return Plan(plans, chosen_windows, lines, since, until, seed)


def stored(plan: Plan) -> dict:
    return {"turns": list(plan.turns), "windows": plan.windows}


def _inline(turn: TurnPlan) -> dict[int, str]:
    """A name inside a line as its speaker's label in brackets ("[you]", "[P2]'s"), anyone not
    in the conversation as [name], as the judge masks."""
    return {g: f"[{label}]" for g, label in turn.labels.items()}


def mask_lines(texts, turn: TurnPlan, *, pattern, owners) -> list[str]:
    inline = _inline(turn)
    return [" ".join(judge.mask(t, pattern, owners, inline).split()) for t in texts]


def context_rows(turn: TurnPlan, *, pattern, owners) -> list[list]:
    """[[seconds before the turn, label, masked text], ...] for the sheet."""
    start = turn.lines[0].ts
    inline = _inline(turn)
    return [[round(start - line.ts), turn.labels.get(line.speaker, "P?"),
             " ".join(judge.mask(line.text, pattern, owners, inline).split())]
            for line in turn.context]
