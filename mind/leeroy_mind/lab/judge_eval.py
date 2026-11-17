"""`lab judge-eval --gold DIR`: how well each judge agrees with a strong grader's grades.

Reads a directory of gold conversations (lab/gold.py), splits it by conversation with the fixed
seed, runs the judges on one part's graded replies and prints numbers only: Spearman's rho
against the gold 1-5 with a bootstrap interval, by length band and per gold defect letter, the
agreement on which of two resampled arms is better, and the calls and model seconds per reply.
It writes nothing and never prints, logs or stores a word of the gold.

The judges: "features", "tells", "fewshot", "pairwise" (lab/judge.py with the shipped weights)
and "rubric" (lab/grade.py's old 0-3 rubric, in chunks of RUBRIC_TURNS replies with the
conversation's card when it has one, as the lab graded a battery pair)."""
import asyncio

from . import gold, grade, judge

DESIGNS = (*judge.MODES, "rubric")
RUBRIC_TURNS = 2
CARD_CHARS = 1500
PASS_RHO, PASS_ARM = 0.35, 0.70


async def judge_items(reasoner, items: list, design: str, *, seed: int = 0,
                      parallel: int = 2) -> dict[str, float | None]:
    """{item key: the design's score, None where a call failed twice}."""
    slots = asyncio.Semaphore(max(1, parallel))
    out: dict[str, float | None] = {}

    async def one(item) -> None:
        async with slots:
            try:
                got = await judge.score(reasoner, item.his, item.reply, mode=design,
                                        context=item.context, earlier=item.earlier,
                                        rewritten=item.rewritten,
                                        seed=judge.seed_of(seed, item.conv, item.index))
                out[item.key] = got["score"]
            except judge.JudgeError:
                out[item.key] = None

    async def chunk(rows: list) -> None:
        async with slots:
            turns = [{"his": i.his, "reply": i.reply} for i in rows]
            try:
                reply = await judge.ask(reasoner, grade.SYSTEM,
                                        grade.rubric_prompt(rows[0].card[:CARD_CHARS], turns),
                                        grade.rubric_schema(len(turns)),
                                        seed=judge.seed_of(seed, rows[0].conv, rows[0].index),
                                        predict=64 * len(turns))
                grades = grade.rubric_parse(reply, len(turns))
            except judge.JudgeError:
                grades = None
            for i, item in enumerate(rows):
                out[item.key] = None if grades is None else float(grades[i][0])

    if design == "rubric":
        by: dict[str, list] = {}
        for item in items:
            by.setdefault(item.conv, []).append(item)
        work = [chunk(rows[i:i + RUBRIC_TURNS]) for rows in by.values()
                for i in range(0, len(rows), RUBRIC_TURNS)]
    else:
        work = [one(item) for item in items]
    await asyncio.gather(*work)
    return out


def table(items: list, scores: dict[str, float | None], *, calls: int = 0,
          seconds: float = 0.0, seed: int = gold.SPLIT_SEED) -> dict:
    """One design's numbers against the gold, over the items it scored."""
    kept = [i for i in items if scores.get(i.key) is not None]
    g = [float(i.score) for i in kept]
    j = [float(scores[i.key]) for i in kept]
    rho = gold.spearman(j, g)
    ci = gold.bootstrap(j, g, seed=seed)
    bands = {}
    for name, low, high in gold.BANDS:
        rows = [(a, b) for i, a, b in zip(kept, j, g) if low <= len(i.reply.split()) <= high]
        if rows:
            bands[name] = {"n": len(rows), "judge": round(sum(a for a, _ in rows) / len(rows), 3),
                           "gold": round(sum(b for _, b in rows) / len(rows), 3)}
    defects = {}
    for letter in sorted({f for i in kept for f in i.flags}):
        has = [float(letter in i.flags) for i in kept]
        r = gold.spearman(j, has)
        if sum(has) >= 3 and r is not None:
            defects[letter] = {"n": int(sum(has)), "rho": round(r, 3)}
    arm = gold.arm_agreement([i.conv for i in kept], g, j, seed=seed)
    n = len(items) or 1
    return {"n": len(kept), "failed": len(items) - len(kept),
            "spearman": None if rho is None else round(rho, 3),
            "ci": None if ci is None else [round(ci[0], 3), round(ci[1], 3)],
            "bands": bands, "defects": defects, "arm": arm,
            "calls_per_reply": round(calls / n, 2), "seconds_per_reply": round(seconds / n, 2),
            "passes": bool(rho is not None and rho >= PASS_RHO and arm["agree"] is not None
                           and arm["agree"] >= PASS_ARM)}


async def evaluate(reasoner, directory, *, designs=DESIGNS, part: str = "test", seed: int = 0,
                   parallel: int = 2) -> dict:
    """The whole evaluation: {"gold": sizes, "part", "designs": {design: table}}."""
    convs = gold.load(directory)
    parts = gold.split(convs)
    items = [i for p in parts.values() for i in p] if part == "all" else parts[part]
    out = {"gold": {"conversations": len(convs),
                    "replies": {p: len(v) for p, v in parts.items()}},
           "part": part, "seed": seed, "weights": judge.weights()["version"], "designs": {}}
    for design in designs:
        before = (reasoner.calls, reasoner.seconds)
        scores = await judge_items(reasoner, items, design, seed=seed, parallel=parallel)
        out["designs"][design] = table(items, scores, calls=reasoner.calls - before[0],
                                       seconds=reasoner.seconds - before[1])
    return out


def _f(v) -> str:
    return "n/a" if v is None else f"{v:+.3f}" if isinstance(v, float) else str(v)


def render(data: dict) -> str:
    sizes = data["gold"]["replies"]
    lines = [f"gold: {data['gold']['conversations']} conversations, graded replies "
             + ", ".join(f"{p} {n}" for p, n in sizes.items())
             + f"; part {data['part']}, seed {data['seed']}, weights v{data['weights']}",
             f"{'judge':10s} {'n':>4s} {'rho':>7s} {'95% interval':>18s} {'arm agree':>10s} "
             f"{'calls':>6s} {'s/reply':>8s}  pass"]
    for design, t in data["designs"].items():
        ci = "n/a" if t["ci"] is None else f"{t['ci'][0]:+.3f}..{t['ci'][1]:+.3f}"
        arm = t["arm"]
        agree = "n/a" if arm["agree"] is None else f"{100 * arm['agree']:.0f}% of {arm['pairs']}"
        lines.append(f"{design:10s} {t['n']:4d} {_f(t['spearman']):>7s} {ci:>18s} {agree:>10s} "
                     f"{t['calls_per_reply']:6.2f} {t['seconds_per_reply']:8.2f}  "
                     + ("yes" if t["passes"] else "no"))
    for design, t in data["designs"].items():
        lines.append(f"{design}: mean by her words (judge / gold): "
                     + ", ".join(f"{b} {v['judge']:.2f} / {v['gold']:.2f} (n={v['n']})"
                                 for b, v in t["bands"].items()))
        if t["defects"]:
            lines.append(f"{design}: rho with a gold defect (negative: the judge marks it down): "
                         + ", ".join(f"{k} {v['rho']:+.2f} (n={v['n']})" for k, v in t["defects"].items()))
    lines.append(f"a judge passes at rho >= {PASS_RHO} and arm agreement >= {round(100 * PASS_ARM)}% "
                 f"(arms of {gold.ARM_SIZE} replies whose gold means differ by {gold.ARM_MARGIN}+)")
    return "\n".join(lines)
