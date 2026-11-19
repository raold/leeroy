"""`lab judge-eval --gold DIR`: how well each judge agrees with a strong grader's grades.

Reads a directory of gold conversations (lab/gold.py), splits it by conversation with the fixed
seed, runs the judges on one part's graded replies and prints numbers only: Spearman's rho
against the gold 1-5 with its 90% interval by conversation, by length band and per gold defect
letter, the agreement on which of two resampled arms is better (reported only), the calls and
model seconds per reply, and each judge's verdict (`verdict`). It writes nothing and never
prints, logs or stores a word of the gold.

The bar (`verdict`): a judge passes when the lower end of rho's 90% interval, resampling whole
conversations, is PASS_RHO or more, on the test part (train and dev chose the designs and fit
the weights), measured on at least MIN_CONVERSATIONS conversations and MIN_REPLIES replies;
below that the verdict is "not enough gold". A point estimate on a handful of conversations can
clear a bar by chance, and so can arm agreement, which is why neither is the criterion
(tests/lab/test_judge_eval.py simulates it). A pass is a reason to grade a fresh gold set, not
to turn `[lab] promote` on by itself (lab/README.md, "The judge").

The judges: lab/judge.py's modes with the shipped weights ("features", "checklist", "fewshot",
"pairwise", and "tells", the combination the lab uses) and "rubric" (lab/grade.py's old 0-3
rubric, in chunks of RUBRIC_TURNS replies with the conversation's card when it has one, as the
lab graded a battery pair). `refit` makes new weights from the train and dev parts (never the
test part): numbers only.

Every judge call is at temperature 0, so the seed only breaks exact ties in the model's
sampling; the same seed is used for every reply. The rubric is asked the way the lab asks it
(unseeded, temperature 0.2), so its numbers move from run to run."""
import asyncio

from leeroy_mind.nightly.llm import ReflectionError

from . import gold, grade, judge

DESIGNS = (*judge.MODES, "rubric")
RUBRIC_TURNS = 2
CARD_CHARS = 1500
PASS_RHO = 0.35   # the lower end of rho's 90% interval by conversation (gold.CI_LEVEL)
MIN_CONVERSATIONS, MIN_REPLIES = 12, 150
PASS, FAIL, TOO_LITTLE, NOT_TEST = "pass", "fail", "not enough gold", "not the test part"


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
                                        rewritten=item.rewritten, seed=seed)
                out[item.key] = got["score"]
            except judge.JudgeError:
                out[item.key] = None

    async def chunk(rows: list) -> None:
        async with slots:
            turns = [{"his": i.his, "reply": i.reply} for i in rows]
            try:  # asked as the lab's grade stage asks it (Reasoner.json: unseeded, 0.2)
                reply = await reasoner.json(grade.SYSTEM,
                                            grade.rubric_prompt(rows[0].card[:CARD_CHARS], turns),
                                            grade.rubric_schema(len(turns)))
                grades = grade.rubric_parse(reply, len(turns))
            except ReflectionError as err:
                if err.fatal:
                    raise
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


def verdict(convs: list[str], judged: list[float], graded: list[float], *, part: str = "test",
            seed: int = gold.SPLIT_SEED, draws: int = gold.BOOT_DRAWS) -> dict:
    """Whether a judge's scores clear the bar against the gold's: {"conversations", "ci" (rho's
    90% interval by conversation, or None), "verdict" (PASS, FAIL, TOO_LITTLE or NOT_TEST),
    "passes"}. `convs[i]` names item i's conversation."""
    n = len(set(convs))
    ci = gold.cluster_bootstrap(convs, judged, graded, seed=seed, draws=draws)
    if part != "test":
        found = NOT_TEST
    elif n < MIN_CONVERSATIONS or len(convs) < MIN_REPLIES:
        found = TOO_LITTLE
    elif ci is not None and ci[0] >= PASS_RHO:
        found = PASS
    else:
        found = FAIL
    return {"conversations": n, "ci": ci, "verdict": found, "passes": found == PASS}


def table(items: list, scores: dict[str, float | None], *, calls: int = 0,
          seconds: float = 0.0, seed: int = gold.SPLIT_SEED, part: str = "test") -> dict:
    """One design's numbers against the gold, over the items it scored."""
    kept = [i for i in items if scores.get(i.key) is not None]
    g = [float(i.score) for i in kept]
    j = [float(scores[i.key]) for i in kept]
    rho = gold.spearman(j, g)
    bar = verdict([i.conv for i in kept], j, g, part=part, seed=seed)
    ci = bar["ci"]
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
            "conversations": bar["conversations"],
            "spearman": None if rho is None else round(rho, 3),
            "ci": None if ci is None else [round(ci[0], 3), round(ci[1], 3)],
            "bands": bands, "defects": defects, "arm": arm,
            "calls_per_reply": round(calls / n, 2), "seconds_per_reply": round(seconds / n, 2),
            "verdict": bar["verdict"], "passes": bar["passes"]}


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
                                       seconds=reasoner.seconds - before[1], part=part)
    return out


async def refit(reasoner, directory, *, seed: int = 0, parallel: int = 2,
                ridge: float = judge.RIDGE) -> dict:
    """New weights for every mode from the gold's train and dev parts: the shape of
    `judge_weights.json` with the next version (numbers only). The pairwise mode has nothing to
    fit: its score is the anchors' own."""
    parts = gold.split(gold.load(directory))
    items = parts["train"] + parts["dev"]
    targets = [float(i.score) for i in items]
    slots = asyncio.Semaphore(max(1, parallel))
    out = {"version": judge.weights()["version"] + 1, "fit_on": len(items), "ridge": ridge,
           "modes": {}}
    for mode in judge.MODES:
        if mode == "pairwise":
            out["modes"][mode] = {"bias": 0.0, "weights": {"anchors": 1.0}}
            continue

        async def one(item, mode=mode):
            async with slots:
                return await judge.inputs(reasoner, item.his, item.reply, mode=mode,
                                          context=item.context, earlier=item.earlier,
                                          rewritten=item.rewritten, seed=seed)
        rows = await asyncio.gather(*(one(i) for i in items))
        out["modes"][mode] = judge.fit(list(rows), targets, list(judge.inputs_of(mode)), ridge=ridge)
    return out


def _f(v) -> str:
    return "n/a" if v is None else f"{v:+.3f}" if isinstance(v, float) else str(v)


def render(data: dict) -> str:
    sizes = data["gold"]["replies"]
    lines = [f"gold: {data['gold']['conversations']} conversations, graded replies "
             + ", ".join(f"{p} {n}" for p, n in sizes.items())
             + f"; part {data['part']}, seed {data['seed']}, weights v{data['weights']}",
             f"{'judge':10s} {'n':>4s} {'convs':>5s} {'rho':>7s} {'90% by conv':>16s} "
             f"{'arm agree':>10s} {'calls':>6s} {'s/reply':>8s}  verdict"]
    for design, t in data["designs"].items():
        ci = "n/a" if t["ci"] is None else f"{t['ci'][0]:+.3f}..{t['ci'][1]:+.3f}"
        arm = t["arm"]
        agree = "n/a" if arm["agree"] is None else f"{100 * arm['agree']:.0f}% of {arm['pairs']}"
        lines.append(f"{design:10s} {t['n']:4d} {t['conversations']:5d} {_f(t['spearman']):>7s} "
                     f"{ci:>16s} {agree:>10s} {t['calls_per_reply']:6.2f} "
                     f"{t['seconds_per_reply']:8.2f}  {t['verdict']}")
    for design, t in data["designs"].items():
        lines.append(f"{design}: mean by her words (judge / gold): "
                     + ", ".join(f"{b} {v['judge']:.2f} / {v['gold']:.2f} (n={v['n']})"
                                 for b, v in t["bands"].items()))
        if t["defects"]:
            lines.append(f"{design}: rho with a gold defect (negative: the judge marks it down): "
                         + ", ".join(f"{k} {v['rho']:+.2f} (n={v['n']})" for k, v in t["defects"].items()))
    lines.append(f"a judge passes when the low end of rho's {round(100 * gold.CI_LEVEL)}% interval, "
                 f"resampling whole conversations, is {PASS_RHO} or more on the test part of "
                 f"{MIN_CONVERSATIONS}+ conversations and {MIN_REPLIES}+ replies (fewer: "
                 f"{TOO_LITTLE}); confirm a pass on a fresh gold set before [lab] promote")
    lines.append(f"arm agreement (arms of {gold.ARM_SIZE} replies whose gold means differ by "
                 f"{gold.ARM_MARGIN}+) is reported only: with few conversations a judge with no "
                 "agreement reaches 70% about one time in eight")
    return "\n".join(lines)
