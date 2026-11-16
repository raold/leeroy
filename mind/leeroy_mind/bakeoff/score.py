"""Scoring a bake-off, per candidate and per channel family, the three baselines included:

- `sent`: the lines the guild actually sent for those turns (its real typing, after every guard,
  retry and repair);
- `first`: the live model's first draft of the generation, before any retry hint, repaired and
  typed as every candidate's is;
- `live`: the live model's reply whose words were sent, typed the same way.

So `first` against a candidate asked the same first request is sampling and model; `first`
against `live` is what the pipeline's retries and guards changed; `live` against `sent` is what
happened after generation (typing, corrections). Every score is also split by turns the live
pipeline asked more than once (`retried`) and turns it answered on the first try.

(a) The stylometric AUC against his real lines (turing.stylo.analyse: the same features and
sampling caps), where a candidate's lines stand in the corpus in place of the members' lines of
those turns; only those lines are sampled on the members' side, every other line stays as context.
(b) The blind judge's balanced accuracy (turing.judge, the reflection model) on windows whose
member lines are all the candidate's. (c) Detectors that need no model (eval.talk): repeats of
her own earlier lines, phrasing several members share, stock openings, filler after his question
and, only when the embedding model is already loaded, replies that drift from his line. (d) How
often a candidate's answer differs from what was sent, and its median length."""
import json
import statistics
from collections import Counter, defaultdict

from leeroy_mind.eval.talk import nonsequitur, repetition
from leeroy_mind.eval.talk.common import words
from leeroy_mind.turing import judge, stylo
from leeroy_mind.turing.corpus import FAMILIES, FAMILY_LABELS, Line
from leeroy_mind.turing.judge import TELLS
from leeroy_mind.turing.report import judge_rates

BASELINES = ("sent", "first", "live")
SUBSETS = ("retried", "first_try")
RECENT_OWN = 20       # her earlier lines a reply is checked against for repeats
BURST_STEP_S = 2.0    # a candidate's extra line comes this long after the one before
TOP_STYLE = 3
TOP_TELLS = 5
PHRASES = 2


def texts(conn, candidate: str) -> dict[int, list[str]]:
    """The candidate's typed lines per turn it answered (status ok)."""
    return {r["turn"]: json.loads(r["lines"]) for r in conn.execute(
        "SELECT turn, lines FROM result WHERE candidate=? AND status='ok'", (candidate,))}


def substitute(plan, answers: dict[int, list[str]]) -> tuple[list[Line], set[str]]:
    """The corpus with every planned turn's lines swapped for `answers` (none for a turn it did
    not answer), and the refs of the lines put in."""
    replaced = {r for t in plan.turns.values() for r in t.refs}
    out = [line for line in plan.lines if line.ref not in replaced]
    added = set()
    for tid, turn in plan.turns.items():
        out += _put(turn, answers.get(tid, []), added)
    out.sort(key=lambda line: (line.ts, line.ref))
    return out, added


def _put(turn, lines: list[str], added: set[str]) -> list[Line]:
    base = turn.lines
    out = []
    for i, text in enumerate(lines):
        ts = base[i].ts if i < len(base) else base[-1].ts + BURST_STEP_S * (i - len(base) + 1)
        ref = f"x:{turn.id}:{i}"
        added.add(ref)
        out.append(Line(ref, ts, base[0].family, base[0].conv, base[0].channel, base[0].speaker,
                        False, text))
    return out


def subset(plan, name: str) -> set[int]:
    """The planned turns the live pipeline asked more than once (`retried`), or the others."""
    want = name == "retried"
    return {tid for tid, t in plan.turns.items() if t.captured.retried == want}


def stylometry(plan, answers, *, names, by_guid, tz, seed: str, only: set[int] | None = None,
               timing: bool = True) -> dict[str, dict]:
    """Per family the AUC from the words alone (`text`, stylo.TEXT_KEYS: what a model changes)
    and from every feature, timing included (`all`, the night's own measure, where a candidate
    shares the live pipeline's timing). `only`: sample the lines of these turns alone (every
    planned turn is still swapped, so the context is the same)."""
    lines, added = substitute(plan, answers)
    if only is not None:
        added = {ref for ref in added if int(ref.split(":")[1]) in only}
    out = {}
    for f in FAMILIES:
        text, every = (stylo.analyse(lines, f, names=names, by_guid=by_guid, tz=tz,
                                     seed=f"bakeoff|{seed}", eligible=lambda line: line.ref in added,
                                     keys=keys).as_dict() if keys is not None or timing else None
                       for keys in (stylo.TEXT_KEYS, None))
        out[f] = dict(text, all=every)
    return out


def window_lines(plan, window: dict, answers: dict[int, list[str]]) -> list[Line] | None:
    by_ref = plan.by_ref
    owner = {r: tid for tid in window["turns"] for r in plan.turns[tid].refs}
    out, done, added = [], set(), set()
    for ref in window["refs"]:
        line = by_ref[ref]
        if line.human:
            out.append(line)
            continue
        tid = owner[ref]
        if tid not in done:
            done.add(tid)
            out += _put(plan.turns[tid], answers.get(tid, []), added)
    out.sort(key=lambda line: (line.ts, line.ref))
    if not out or all(line.human for line in out) or len({line.speaker for line in out}) < 2:
        return None
    return out


def digest(lines: list[Line] | None) -> str:
    """What a window was judged on: a resumed run judges it again when its lines changed (a
    candidate's turn that failed before and answered since)."""
    import hashlib
    if lines is None:
        return "none"
    rows = [[line.speaker, round(line.ts, 3), line.text] for line in lines]
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode("utf-8")).hexdigest()[:32]


async def judge_window(reasoner, lines, *, by_guid, human: int, owners, pattern) -> dict:
    shown = judge.render(lines, by_guid=by_guid, human=human, owners=owners)
    reply = await reasoner.json(judge.SYSTEM, shown.prompt, judge.response_schema(list(shown.letters)))
    return judge.score(reply, shown.letters, human=human, keep_phrases=judge.public(lines),
                       pattern=pattern)


def judge_totals(conn, candidate: str) -> dict[str, dict]:
    out = {}
    for family in FAMILIES:
        rows = conn.execute("SELECT * FROM judged WHERE candidate=? AND family=?",
                            (candidate, family)).fetchall()
        ok = [r for r in rows if r["status"] == "ok"]
        t = {k: sum(r[k] for r in ok) for k in ("speakers", "correct", "his", "his_correct")}
        caught: Counter = Counter()
        phrases: dict[str, list[str]] = defaultdict(list)
        for r in ok:
            for tell in json.loads(r["tells"]):
                if tell.get("truth") == "bot" and tell.get("judged") == "bot" and tell.get("code") in TELLS:
                    caught[tell["code"]] += 1
                    p = tell.get("phrase")
                    if p and len(phrases[tell["code"]]) < PHRASES and p not in phrases[tell["code"]]:
                        phrases[tell["code"]].append(p)
        out[family] = dict(windows=len(ok), skipped=len(rows) - len(ok), **t,
                           **judge_rates(t["speakers"], t["correct"], t["his"], t["his_correct"]),
                           caught=dict(caught), phrases=dict(phrases))
    return out


def recent_own(plan) -> dict[int, list[tuple[str, str]]]:
    """Per planned turn, her own last RECENT_OWN lines before it in its family (as sent)."""
    by_member: dict[tuple[int, str], list[Line]] = defaultdict(list)
    for line in plan.lines:
        if not line.human:
            by_member[(line.speaker, line.family)].append(line)
    out = {}
    for tid, turn in plan.turns.items():
        mine = by_member[(turn.captured.guid, turn.family)]
        start = turn.lines[0].ts
        earlier = [line for line in mine if line.ts < start and line.ref not in turn.refs]
        out[tid] = [(line.ref, line.text) for line in earlier[-RECENT_OWN:]]
    return out


def _thread(turn) -> list[Line]:
    parent = turn.context[-1]
    near = [line for line in turn.context if parent.ts - line.ts <= nonsequitur.CONTEXT_SECONDS]
    return near[-nonsequitur.THREAD_LINES:]


def detectors(plan, answers: dict[int, list[str]], *, recent, vectors: dict | None) -> dict:
    vec = (vectors or {}).get
    spoken = {tid: " ".join(ls) for tid, ls in answers.items() if ls and tid in plan.turns}
    toks = {tid: words(text) for tid, text in spoken.items()}
    repeat = 0
    for tid, text in spoken.items():
        rec = [(ref, t, vec(t)) for ref, t in recent.get(tid, [])]
        if repetition.best_repeat(text, vec(text), rec):
            repeat += 1
    users: dict[tuple, set[int]] = defaultdict(set)
    keys = {tid: repetition.phrase_keys(tk) for tid, tk in toks.items()}
    for tid, phrases in keys.items():
        for ph in phrases:
            users[ph].add(plan.turns[tid].captured.guid)
    shared = sum(any(len(users[ph]) >= repetition.GUILD_MEMBERS for ph in phrases)
                 for phrases in keys.values())
    long = [tk for tk in toks.values() if len(tk) >= repetition.MIN_WORDS]
    openings = Counter(tk[:repetition.STOCK_WORDS] for tk in long)
    stock = sum(openings[tk[:repetition.STOCK_WORDS]] >= repetition.STOCK_COUNT for tk in long)
    answering = filler = low = 0
    for tid, text in spoken.items():
        turn = plan.turns[tid]
        if not turn.answers:
            continue
        answering += 1
        parent = turn.context[-1]
        thread = [vec(line.text) for line in _thread(turn)]
        rule, _ = nonsequitur.score_reply(text, vec(text), parent.text, vec(parent.text),
                                          [v for v in thread if v is not None])
        if rule == "filler_after_question":
            filler += 1
        elif rule:
            low += 1
    n = len(spoken)
    return {"spoken": n, "answering": answering,
            "repeat_own": _rate(repeat, n), "shared_phrase": _rate(shared, n),
            "stock_opening": _rate(stock, n), "filler_after_question": _rate(filler, answering),
            "low_relevance": _rate(low, answering) if vectors else None,
            "relevance_measured": bool(vectors)}


def _rate(n: int, total: int) -> float | None:
    return round(n / total, 4) if total else None


def _same(a: tuple, b: tuple) -> bool:
    if len(a) < repetition.MIN_WORDS or len(b) < repetition.MIN_WORDS:
        return a == b
    return repetition.shingle_overlap(repetition.ngrams(a), repetition.ngrams(b)) >= repetition.NGRAM_OVERLAP


def line_stats(conn, candidate: str, plan, sent: dict[int, list[str]],
               only: set[int] | None = None) -> dict:
    keep = plan.turns if only is None else only
    rows = [r for r in conn.execute("SELECT * FROM result WHERE candidate=?", (candidate,))
            if r["turn"] in keep]
    status = Counter(r["status"] for r in rows)
    spoken = {r["turn"]: " ".join(json.loads(r["lines"])) for r in rows if r["status"] == "ok"}
    compared = [tid for tid in spoken if sent.get(tid)]
    distinct = sum(not _same(words(spoken[tid]), words(" ".join(sent[tid]))) for tid in compared)
    seconds = [r["seconds"] for r in rows if r["seconds"] is not None]
    thinking = [r["thinking_tokens"] for r in rows if r["status"] != "error"]
    codes = Counter(r["code"] for r in rows if r["status"] == "error" and r["code"])
    return {"turns": len(keep), "answered": len(rows), "ok": status["ok"], "codes": dict(codes),
            "silent": status["silent"], "error": status["error"],
            "distinct": None if candidate == "sent" else _rate(distinct, len(compared)),
            "median_words": statistics.median(len(t.split()) for t in spoken.values()) if spoken else None,
            "median_seconds": round(statistics.median(seconds), 2) if seconds else None,
            "median_thinking_tokens": statistics.median(thinking) if thinking else None}


def tells(families: dict, judged: dict) -> dict:
    style = {}
    for family, r in families.items():
        if r["status"] == "ok":
            style[family] = [stylo.describe(f) for f in r["features"][:TOP_STYLE]]
    caught: Counter = Counter()
    phrases: dict[str, list[str]] = defaultdict(list)
    for j in judged.values():
        caught.update(j["caught"])
        for code, ps in j["phrases"].items():
            for p in ps:
                if len(phrases[code]) < PHRASES and p not in phrases[code]:
                    phrases[code].append(p)
    judge_tells = [{"code": code, "label": TELLS[code], "count": n, "phrases": phrases.get(code, [])}
                   for code, n in sorted(caught.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP_TELLS]]
    return {"style": style, "judge": judge_tells}


# -- the report --------------------------------------------------------------------------------

def _pct(v) -> str:
    return "n/a" if v is None else f"{100 * v:.0f}%"


def _auc(r: dict) -> str:
    if r["status"] != "ok":
        return f"{r['status']} ({r['human_lines']}/{r['member_lines']})"
    band = f" ({r['low']:.2f}-{r['high']:.2f})" if r["low"] is not None and r["high"] is not None else ""
    return f"{r['auc']:.2f}{band}"


def _judge(j: dict) -> str:
    if not j["windows"]:
        return "n/a"
    return f"{_pct(j['balanced'])} ({j['windows']}w)"


def _num(v) -> str:
    return "n/a" if v is None else f"{v:g}"


def render(report: dict) -> str:
    out = [
        "# Bake-off report",
        "",
        "Local only: it is built from the owner's own conversations. No line text here, only "
        "numbers, the style tells in words and the judge's own short phrases from public channels.",
        "",
        f"{report['turns']} captured turns asked again ({report['window_turns']} of them in "
        f"{report['windows']} judge windows), from {report['since_text']} to {report['until_text']}; "
        f"the live pipeline asked more than once on {report['retried']} of them. Candidates were "
        f"asked the {'first request of each generation (before any retry hint)' if report['source'] == 'first' else 'request whose words were sent'}. "
        "`sent` is what the guild sent; `first` is the live model's first draft and `live` the reply "
        "whose words were sent, both typed as every candidate's is: `first` against a candidate is "
        "the model, `first` against `live` the pipeline's retries and guards, `live` against `sent` "
        "what came after generation.",
        "",
        "AUC 0.50 means the lines read like his; 1.00 means always told apart. The first table's "
        "AUC reads the words alone (what a model changes); the last one adds when and after what "
        "each line came, the night's own measure, which every candidate shares with the live "
        "pipeline. The judge's balanced accuracy is 0.50 at chance. A difference under about 0.10 "
        "is within noise (turing/README.md, Limits); with a few dozen lines a family it is wider.",
    ]
    if report.get("beat_turns"):
        out += ["", f"{report['beat_turns']} of the turns had a beat live (talk/beat.py). Its lines are "
                "never stored, so every request here is the line's request without them."]
    if report["missing"]:
        out += ["", "Not pulled, so not run: " + ", ".join(f"`{m}`" for m in report["missing"]) + "."]
    if not report["judge_ran"]:
        out += ["", f"The blind judge did not run: {report['judge_note']}."]
    out += ["", "## Turing", "",
            "| candidate | model | think | prompt | " + " | ".join(
                f"AUC {FAMILY_LABELS[f]}" for f in FAMILIES) + " | " + " | ".join(
                f"judge {FAMILY_LABELS[f]}" for f in FAMILIES) + " |",
            "|" + "---|" * (4 + 2 * len(FAMILIES))]
    for c in report["candidates"]:
        think = ("no" if c["think"] is False else
                 f"{'yes' if c['think'] else 'default'} ({c['think_budget']}, ctx {c.get('num_ctx') or 'fit'})")
        out.append(f"| {c['name']} | {c['model']} | {think} | {c['prompt_variant']} | "
                   + " | ".join(_auc(c["families"][f]) for f in FAMILIES) + " | "
                   + " | ".join(_judge(c["judge"][f]) for f in FAMILIES) + " |")
    out += ["", "## Lines", "",
            "| candidate | ok | silent | errors | distinct from sent | median words | median s | "
            "thinking tokens | repeats own | shared phrasing | stock openings | filler after ? | "
            "drifts from his line |",
            "|" + "---|" * 13]
    for c in report["candidates"]:
        s, d = c["lines"], c["detectors"]
        out.append(f"| {c['name']} | {s['ok']} | {s['silent']} | {s['error']} | {_pct(s['distinct'])} | "
                   f"{_num(s['median_words'])} | {_num(s['median_seconds'])} | "
                   f"{_num(s['median_thinking_tokens'])} | {_pct(d['repeat_own'])} | "
                   f"{_pct(d['shared_phrase'])} | {_pct(d['stock_opening'])} | "
                   f"{_pct(d['filler_after_question'])} | "
                   f"{_pct(d['low_relevance']) if d['relevance_measured'] else 'not measured'} |")
    out += ["", "## Top tells", ""]
    for c in report["candidates"]:
        out.append(f"### {c['name']}")
        t = c["tells"]
        for family, words_ in t["style"].items():
            if words_:
                out.append(f"- {FAMILY_LABELS[family]}: " + "; ".join(words_))
        for j in t["judge"]:
            quoted = "".join(f' "{p}"' for p in j["phrases"])
            out.append(f"- judge caught: {j['label']} ({j['count']}){quoted}")
        if not t["style"] and not t["judge"]:
            out.append("- none measured")
        if c["lines"].get("codes"):
            out.append("- errors: " + ", ".join(f"{k} {n}" for k, n in sorted(c["lines"]["codes"].items())))
        out.append("")
    for name, title in (("retried", "Turns the live pipeline asked more than once"),
                        ("first_try", "Turns it answered on the first try")):
        out += [f"## {title}", "",
                "| candidate | ok | silent | errors | distinct from sent | median words | " + " | ".join(
                    f"AUC {FAMILY_LABELS[f]}" for f in FAMILIES) + " | repeats own | filler after ? |",
                "|" + "---|" * (8 + len(FAMILIES))]
        for c in report["candidates"]:
            sub = c["subsets"][name]
            s_, d = sub["lines"], sub["detectors"]
            out.append(f"| {c['name']} | {s_['ok']} | {s_['silent']} | {s_['error']} | "
                       f"{_pct(s_['distinct'])} | {_num(s_['median_words'])} | "
                       + " | ".join(_auc(sub["families"][f]) for f in FAMILIES)
                       + f" | {_pct(d['repeat_own'])} | {_pct(d['filler_after_question'])} |")
        out.append("")
    out += ["## All features, timing included", "",
            "| candidate | " + " | ".join(f"AUC {FAMILY_LABELS[f]}" for f in FAMILIES) + " |",
            "|" + "---|" * (1 + len(FAMILIES))]
    for c in report["candidates"]:
        out.append(f"| {c['name']} | " + " | ".join(_auc(c["families"][f]["all"]) for f in FAMILIES)
                   + " |")
    return "\n".join(out).rstrip() + "\n"
