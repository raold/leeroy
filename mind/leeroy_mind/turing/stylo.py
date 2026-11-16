"""Part A: stylometric distinguishability, with no model. Per family: his lines against an
equal, seeded sample of the members' from the same window, a grouped cross-validated logistic
regression over features.FEATURES, its AUC with a cluster-bootstrap interval, and the features
that separate the two most, each with his value and the members' (shown in words by
`describe`). Deterministic for a given database, window and seed."""
import math
import random
import statistics
from dataclasses import asdict, dataclass, field
from datetime import tzinfo
from typing import Sequence

from . import classify
from .corpus import FAMILIES, Line, name_pattern, names_by_guid, read_lines
from .features import COLUMNS, FEATURES, KINDS, LABELS, table

MIN_LINES = 30          # of each class before a family is measured
MAX_PER_CLASS = 1500    # the balanced sample's cap, per class
SEGMENT_GAP_S = 1800.0  # a conversation silent this long starts a new cross-validation group
TOP = 6                 # features reported
MIN_SEPARATION = 0.05   # |univariate AUC - 0.5| below this is not a tell
MEMBER_SHARE = 0.05     # one member's most of the members' sample (an equal share if fewer speak)


@dataclass
class FamilyResult:
    family: str
    status: str                 # ok, too_few or one_class
    human_lines: int
    member_lines: int
    sample: int = 0             # lines per class in the balanced sample
    groups: int = 0
    auc: float | None = None
    low: float | None = None
    high: float | None = None
    features: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def groups_of(lines: Sequence[Line]) -> list[str]:
    """A cross-validation group per conversation stretch: its conversation key and a counter
    that moves on after SEGMENT_GAP_S of silence."""
    last: dict[str, tuple[float, int]] = {}
    out = []
    for line in lines:
        ts, seg = last.get(line.conv, (None, 0))
        if ts is not None and line.ts - ts > SEGMENT_GAP_S:
            seg += 1
        last[line.conv] = (line.ts, seg)
        out.append(f"{line.conv}#{seg}")
    return out


def member_sample(members: list[int], speaker_of: Sequence[int], n: int,
                  rng: random.Random) -> list[int]:
    """At most `n` of the members' lines with no member above MEMBER_SHARE of them, or above an
    equal share when fewer than 1/MEMBER_SHARE members spoke, so one chatty member cannot set
    the tells. When the capped lines fall short, the sample shrinks to what the cap allows
    (and his side with it)."""
    by_member: dict[int, list[int]] = {}
    for i in members:
        by_member.setdefault(speaker_of[i], []).append(i)
    if not by_member or n <= 0:
        return []
    for _ in range(64):
        cap = max(math.ceil(MEMBER_SHARE * n), math.ceil(n / len(by_member)))
        total = sum(min(len(v), cap) for v in by_member.values())
        if total >= n:
            break
        n = total
    pool: list[int] = []
    for g in sorted(by_member):
        lines = by_member[g]
        pool += lines if len(lines) <= cap else rng.sample(lines, cap)
    return sorted(rng.sample(pool, min(n, len(pool))))


def _circular_hour(hours: list[float]) -> float | None:
    if not hours:
        return None
    s = math.fsum(math.sin(2 * math.pi * h / 24) for h in hours)
    c = math.fsum(math.cos(2 * math.pi * h / 24) for h in hours)
    if abs(s) < 1e-9 and abs(c) < 1e-9:
        return None
    return (math.atan2(s, c) * 24 / (2 * math.pi)) % 24


def _value(key: str, raw: list, rows: list[int]):
    kind = KINDS[key]
    values = [raw[i] for i in rows]
    if kind == "share":
        return round(math.fsum(values) / len(values), 4) if values else None
    if kind == "median":
        return float(statistics.median(values)) if values else None
    if kind == "seconds":
        known = [v for v in values if v is not None]
        return round(float(statistics.median(known)), 1) if known else None
    hour = _circular_hour(values)
    return None if hour is None else round(hour, 2)


def top_features(columns: list[list[float]], raw: dict[str, list], his: list[int],
                 members: list[int]) -> list[dict]:
    """The features whose single-feature AUC is furthest from 0.5 on the balanced rows, at
    most TOP, each with his value and the members' (a share, a median or an hour)."""
    rows = his + members
    labels = [0] * len(his) + [1] * len(members)
    by_column = dict(zip(COLUMNS, columns))
    out = []
    for key, _, _ in FEATURES:
        cols = [key + "_sin", key + "_cos"] if key == "hour" else [key]
        aucs = [classify.auc([by_column[c][i] for i in rows], labels) for c in cols]
        aucs = [a for a in aucs if a is not None]
        if not aucs:
            continue
        a = max(aucs, key=lambda v: abs(v - 0.5))
        separation = abs(a - 0.5)
        if separation < MIN_SEPARATION:
            continue
        out.append({"key": key, "human": _value(key, raw[key], his),
                    "member": _value(key, raw[key], members),
                    "separation": round(separation, 4), "higher": "members" if a > 0.5 else "him"})
    out.sort(key=lambda f: (-f["separation"], f["key"]))
    return out[:TOP]


def analyse(lines: Sequence[Line], family: str, *, names=None, by_guid: dict[int, str] | None = None,
            tz: tzinfo | None = None, seed: str = "turing") -> FamilyResult:
    fam = [line for line in lines if line.family == family]
    his = [i for i, line in enumerate(fam) if line.human]
    members = [i for i, line in enumerate(fam) if not line.human]
    result = FamilyResult(family, "too_few", len(his), len(members))
    if len(his) < MIN_LINES or len(members) < MIN_LINES:
        return result
    rng = random.Random(f"{seed}|{family}")
    pick_members = member_sample(members, [line.speaker for line in fam],
                                 min(len(his), len(members), MAX_PER_CLASS), rng)
    n = len(pick_members)
    if n < MIN_LINES:
        return result
    pick_his = sorted(rng.sample(his, n))
    tab = table(fam, names=names, names_by_guid=by_guid, tz=tz)
    rows = pick_his + pick_members
    columns = [[col[i] for i in rows] for col in tab.columns]
    labels = [0] * n + [1] * n
    groups = groups_of(fam)
    cv = classify.cross_validate(columns, labels, [groups[i] for i in rows], seed=f"{seed}|{family}|cv")
    result.sample, result.groups = n, cv.groups
    if cv.auc is None:
        result.status = "one_class"
        return result
    result.status = "ok"
    result.auc = round(cv.auc, 4)
    result.low = None if cv.low is None else round(cv.low, 4)
    result.high = None if cv.high is None else round(cv.high, 4)
    result.features = top_features(tab.columns, tab.raw, pick_his, pick_members)
    return result


def compute(conn, *, human: int, end: float, days: int, tz: tzinfo | None = None,
            his_names: Sequence[str] = (), seed: str = "turing") -> dict:
    """Every family's result for the `days` before `end`, as plain data."""
    start = end - days * 86400
    lines = read_lines(conn, human=human, start=start, end=end)
    by_guid = names_by_guid(conn, human=human, his=his_names)
    pattern = name_pattern([*by_guid.values(), *his_names])
    return {"end": end, "days": days,
            "families": {f: analyse(lines, f, names=pattern, by_guid=by_guid, tz=tz,
                                    seed=seed).as_dict() for f in FAMILIES}}


def fmt(kind: str, value) -> str:
    if value is None:
        return "n/a"
    if kind == "share":
        pct = 100 * value
        return "<1%" if 0 < pct < 0.5 else f"{pct:.0f}%"
    if kind == "median":
        return f"{value:g}"
    if kind == "seconds":
        if value < 90:
            return f"{value:.0f} s"
        if value < 5400:
            return f"{value / 60:.0f} min"
        return f"{value / 3600:.1f} h"
    hour = int(value) % 24
    return f"around {hour:02d}:{int(round((value % 1) * 60)) % 60:02d}"


def describe(feature: dict) -> str:
    """One tell in words: "contractions with an apostrophe (i'm, don't): him 3%, members 31%"."""
    key = feature.get("key")
    if key not in LABELS:
        return ""
    kind = KINDS[key]
    return f"{LABELS[key]}: him {fmt(kind, feature.get('human'))}, members {fmt(kind, feature.get('member'))}"
