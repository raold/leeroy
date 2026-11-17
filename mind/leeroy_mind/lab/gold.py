"""Gold conversations and the numbers that say how well a judge agrees with them.

A gold directory holds `conv-*.json` files in the self-play format (any file whose name contains
`conv-` and ends `.json`): {"guid", "case", "card"?, "turns": [...]}, each turn {"who": "him" |
"her", "text", "id"?, "answers"? (the id of his line a reply of hers answers), "grade"?}. A graded
reply carries "grade": {"score": 1-5 (a strong grader's "would a real person have texted this"),
"flags"?: defect letters, comma separated or a list}. Ungraded turns are context only.

Everything here is pure and model-free: the loader, the fixed split by conversation, Spearman's
rank correlation, a bootstrap interval, and the agreement on which of two arms is better (the
lab's actual decision) by resampling arms from the gold replies. Gold files are private: no text
of theirs is logged, stored or printed by this module, only numbers."""
import json
import math
import random
from dataclasses import dataclass, field
from pathlib import Path

SPLIT_SEED = 20261117
SPLIT = (("train", 0.6), ("dev", 0.2), ("test", 0.2))
BANDS = (("<=10", 0, 10), ("11-25", 11, 25), ("26-50", 26, 50), (">50", 51, 10 ** 9))
CONTEXT_TURNS = 6
ARM_SIZE, ARM_DRAWS, ARM_MARGIN = 24, 2000, 0.15
BOOT_DRAWS = 2000


@dataclass(frozen=True)
class Item:
    """One graded reply: his line, her reply, what came before, and the gold grade."""
    conv: str
    index: int
    his: str
    reply: str
    earlier: tuple[str, ...]          # her earlier replies in this conversation, oldest first
    context: tuple[tuple[str, str], ...]  # the last turns before his line, (who, text)
    score: int
    flags: frozenset = frozenset()
    card: str = field(default="", compare=False)
    rewritten: bool = False

    @property
    def key(self) -> str:
        return f"{self.conv}#{self.index}"


class GoldError(ValueError):
    pass


def _flags(raw) -> frozenset:
    if isinstance(raw, str):
        raw = raw.replace(",", " ").split()
    return frozenset(str(f).strip() for f in raw or () if str(f).strip())


def read_conversation(name: str, doc: dict) -> list[Item]:
    turns = doc.get("turns") if isinstance(doc, dict) else None
    if not isinstance(turns, list):
        raise GoldError(f"{name}: no turns")
    by_id, out, hers, seen = {}, [], [], []
    card = doc.get("card") if isinstance(doc.get("card"), str) else ""
    last_his = ""
    for n, t in enumerate(turns):
        if not isinstance(t, dict) or t.get("who") not in ("him", "her") or not isinstance(t.get("text"), str):
            raise GoldError(f"{name}: turn {n} is not a line")
        text = t["text"]
        if t["who"] == "him":
            if "id" in t:
                by_id[t["id"]] = (text, len(seen))
            last_his = text
            seen.append(("him", text))
            continue
        his, upto = by_id.get(t.get("answers"), (last_his, max(0, len(seen) - 1)))
        grade = t.get("grade")
        if isinstance(grade, dict) and type(grade.get("score")) is int and 1 <= grade["score"] <= 5:
            out.append(Item(name, n, his, text, tuple(hers), tuple(seen[:upto][-CONTEXT_TURNS:]),
                            grade["score"], _flags(grade.get("flags")), card,
                            bool(t.get("rewritten"))))
        hers.append(text)
        seen.append(("her", text))
    return out


def load(directory) -> dict[str, list[Item]]:
    """{conversation name: its graded replies}, conversations without one left out."""
    root = Path(directory)
    if not root.is_dir():
        raise GoldError("no such directory")
    out = {}
    for path in sorted(root.glob("*conv-*.json")):
        try:
            doc = json.loads(path.read_bytes())
        except (OSError, ValueError):
            raise GoldError(f"{path.name}: not JSON") from None
        items = read_conversation(path.name, doc)
        if items:
            out[path.name] = items
    return out


def split(convs: dict[str, list[Item]], seed: int = SPLIT_SEED) -> dict[str, list[Item]]:
    """train/dev/test by whole conversation, 60/20/20 of the graded replies as nearly as whole
    conversations allow: the names shuffled by `seed`, then the largest first (the shuffle
    breaks ties), each going to the part furthest below its share."""
    names = sorted(convs)
    random.Random(seed).shuffle(names)
    names.sort(key=lambda n: -len(convs[n]))
    total = sum(len(convs[n]) for n in names) or 1
    parts: dict[str, list[Item]] = {p: [] for p, _ in SPLIT}
    for name in names:
        part = min(SPLIT, key=lambda ps: len(parts[ps[0]]) / total - ps[1])[0]
        parts[part].extend(convs[name])
    return parts


# -- statistics ------------------------------------------------------------------------------------

def ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    out = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            out[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return out


def pearson(a: list[float], b: list[float]) -> float | None:
    n = len(a)
    if n < 3 or n != len(b):
        return None
    ma, mb = sum(a) / n, sum(b) / n
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((y - mb) ** 2 for y in b)
    if va <= 0 or vb <= 0:
        return None
    return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / math.sqrt(va * vb)


def spearman(a: list[float], b: list[float]) -> float | None:
    """Spearman's rho with average ranks for ties; None for fewer than 3 points or no spread."""
    if len(a) != len(b) or len(a) < 3:
        return None
    return pearson(ranks(list(a)), ranks(list(b)))


def bootstrap(a: list[float], b: list[float], *, seed: int = SPLIT_SEED,
              draws: int = BOOT_DRAWS) -> tuple[float, float] | None:
    """The 95% percentile interval of Spearman's rho over replies resampled with replacement."""
    n = len(a)
    if n < 8:
        return None
    rng = random.Random(seed)
    found = []
    for _ in range(draws):
        idx = [rng.randrange(n) for _ in range(n)]
        r = spearman([a[i] for i in idx], [b[i] for i in idx])
        if r is not None:
            found.append(r)
    if len(found) < draws // 2:
        return None
    found.sort()
    return found[int(0.025 * len(found))], found[min(len(found) - 1, int(0.975 * len(found)))]


def arm_agreement(convs: list[str], gold: list[float], judged: list[float], *,
                  seed: int = SPLIT_SEED, draws: int = ARM_DRAWS, size: int = ARM_SIZE,
                  margin: float = ARM_MARGIN) -> dict:
    """How often a judge picks the arm the gold picks. An arm is `size` replies drawn with
    replacement; the two arms of a draw come from two disjoint halves of the conversations
    (split anew each draw), so they differ as two prompt arms do. Counted over the draws whose
    gold means differ by `margin` or more (of the gold's 1-5 scale): {"agree", "pairs",
    "draws"}; `agree` is None when no draw cleared the margin. A judge that calls a draw a tie
    does not agree."""
    names = sorted(set(convs))
    by = {n: [i for i, c in enumerate(convs) if c == n] for n in names}
    rng = random.Random(seed)
    agree = pairs = 0
    if len(names) >= 2:
        for _ in range(draws):
            order = names[:]
            rng.shuffle(order)
            half = max(1, len(order) // 2)
            a_pool = [i for n in order[:half] for i in by[n]]
            b_pool = [i for n in order[half:] for i in by[n]]
            a = [a_pool[rng.randrange(len(a_pool))] for _ in range(size)]
            b = [b_pool[rng.randrange(len(b_pool))] for _ in range(size)]
            g = sum(gold[i] for i in a) / size - sum(gold[i] for i in b) / size
            if abs(g) < margin:
                continue
            j = sum(judged[i] for i in a) / size - sum(judged[i] for i in b) / size
            pairs += 1
            agree += int(j != 0 and (j > 0) == (g > 0))
    return {"agree": round(agree / pairs, 4) if pairs else None, "pairs": pairs, "draws": draws}


def band(words: int) -> str:
    return next(name for name, low, high in BANDS if low <= words <= high)
