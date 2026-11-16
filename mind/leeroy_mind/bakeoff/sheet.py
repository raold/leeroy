"""The owner's blind sheet: `bakeoff sheet` writes sheet.md (n turns that answer his line, each
with the conversation before it, masked as the judge masks, and the replies of every candidate and
of the three baselines: what was sent, the live model's first draft and the reply whose words were
sent, shuffled and labelled A, B, C...) and key.json beside it (which label is which). Replies
whose masked words are the same are shown once, under one label that counts for each of them (a
turn the live pipeline answered on its first try has `first` and `live` alike). He rates every
option 0-3 and may pick one; `bakeoff tally --picks picks.json` scores both against the key: the
mean rating and the pick rate per candidate. The sheet holds his private conversations, explicit
ones too: it stays on this machine."""
import json
import random
import statistics
from collections import Counter, defaultdict
from pathlib import Path

from leeroy_mind.turing.corpus import FAMILIES, FAMILY_LABELS

from .score import BASELINES

LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
SCALE = (0, 1, 2, 3)
HEADER = ("# Bake-off: rate the replies\n\n"
          "**Local only.** This sheet quotes your own whispers, DMs and guild chat (explicit ones "
          "too). Never post, upload or commit it; delete it when you are done.\n\n"
          "For each turn, read the conversation (you are `you`; the member who answers is `P1`; "
          "other names are masked) and rate every reply 0-3: 0 empty, wrong, a bot tell or out of "
          "character; 1 generic or sycophantic; 2 specific and in her voice; 3 would pass as her. "
          "Then, if you like, pick the one you would believe a real player typed. Write "
          "`picks.json` beside this file:\n\n"
          "    {\"1\": {\"scores\": {\"A\": 2, \"B\": 0, \"C\": 3}, \"pick\": \"C\"}, "
          "\"2\": {\"scores\": {\"A\": 1, \"B\": 1}}}\n\n"
          "(`\"pick\": \"-\"` means none of them; `{\"1\": \"C\"}` alone is a pick without "
          "ratings.) Then run `bakeoff tally --picks picks.json --out <this folder>`. The key is in "
          "key.json: don't open it first.\n")


def _offset(seconds: int) -> str:
    m, s = divmod(max(0, int(seconds)), 60)
    return f"-{m:02d}:{s:02d}"


def choose(turns: list, n: int, seed: str) -> list:
    """`n` turns that answer his line, spread across the families, the private ones first."""
    rng = random.Random(f"bakeoff-sheet|{seed}")
    pools = {f: [t for t in turns if t["family"] == f] for f in FAMILIES}
    for pool in pools.values():
        rng.shuffle(pool)
    order = ["private", "game", "discord"]
    out = []
    while len(out) < n and any(pools[f] for f in order):
        for f in order:
            if pools[f] and len(out) < n:
                out.append(pools[f].pop())
    return out


def _said(r) -> str:
    lines = json.loads(r["masked"]) if r["status"] == "ok" else []
    return " / ".join(lines) if lines else "(says nothing)"


def write(conn, out: Path, *, n: int, names: list[str], seed: str) -> dict:
    """sheet.md and key.json in `out`; the counts."""
    turns = [dict(r) for r in conn.execute("SELECT * FROM turn WHERE answers=1 ORDER BY id")]
    rows = {}
    for r in conn.execute("SELECT candidate, turn, status, masked FROM result"):
        if r["candidate"] in names and r["status"] in ("ok", "silent"):
            rows.setdefault(r["turn"], {})[r["candidate"]] = r
    usable = [t for t in turns if len({_said(r) for r in rows.get(t["id"], {}).values()}) >= 2]
    picked = choose(usable, n, seed)
    rng = random.Random(f"bakeoff-sheet-order|{seed}")
    parts, key = [HEADER], {}
    for i, t in enumerate(picked, 1):
        grouped: dict[str, list[str]] = defaultdict(list)
        for c in names:
            if c in rows[t["id"]]:
                grouped[_said(rows[t["id"]][c])].append(c)
        options = sorted(grouped)
        rng.shuffle(options)
        key[str(i)] = {"turn": t["id"], "family": t["family"],
                       "options": {LETTERS[j]: grouped[said] for j, said in enumerate(options)}}
        parts.append(f"\n## {i}. {FAMILY_LABELS[t['family']]} ({t['kind']})\n")
        for seconds, label, text in json.loads(t["context"]):
            parts.append(f"    [{_offset(seconds)}] {label}: {text}")
        parts.append("\nP1 answers:\n")
        for j, said in enumerate(options):
            parts.append(f"- **{LETTERS[j]}**: {said}")
    text = "\n".join(parts) + "\n"
    out.mkdir(parents=True, exist_ok=True)
    (out / "sheet.md").write_bytes(text.encode("utf-8"))
    (out / "key.json").write_bytes((json.dumps({"names": names, "turns": key}, indent=2,
                                               ensure_ascii=False) + "\n").encode("utf-8"))
    return {"turns": len(picked), "candidates": len(names)}


def _entry(raw) -> tuple[str | None, dict]:
    """(pick, scores) from one turn's answer: a letter, or {"pick": .., "scores": {..}}."""
    if isinstance(raw, dict):
        pick, scores = raw.get("pick"), raw.get("scores") or {}
        if not isinstance(scores, dict):
            raise ValueError("scores must be an object of letter to 0-3")
    else:
        pick, scores = raw, {}
    return (None if pick is None else str(pick).strip().upper()), scores


def tally(key: dict, picks) -> dict:
    """His ratings and picks scored against the key: per candidate the mean rating over the
    turns he rated it, how often it was picked and how often it was on offer."""
    if isinstance(picks, list):
        picks = {str(i): p for i, p in enumerate(picks, 1)}
    if not isinstance(picks, dict):
        raise ValueError("picks must be an object of turn number to a letter or {pick, scores}")
    picked: Counter = Counter()
    offered: Counter = Counter()
    ratings: dict[str, list[int]] = defaultdict(list)
    none = unanswered = unreadable = made = 0
    for number, entry in key["turns"].items():
        options = {letter: (names if isinstance(names, list) else [names])
                   for letter, names in entry["options"].items()}
        for names in options.values():
            for c in names:
                offered[c] += 1
        if number not in picks:
            unanswered += 1
            continue
        choice, scores = _entry(picks[number])
        for letter, value in scores.items():
            letter = str(letter).strip().upper()
            if letter not in options or type(value) is not int or value not in SCALE:
                unreadable += 1
                continue
            for c in options[letter]:
                ratings[c].append(value)
        if choice is None:
            continue
        if choice in ("-", "", "NONE"):
            none += 1
        elif choice in options:
            made += 1
            for c in options[choice]:
                picked[c] += 1
        else:
            unreadable += 1
    rows = [{"candidate": c, "picked": picked[c], "offered": offered[c],
             "pick_rate": round(picked[c] / offered[c], 4) if offered[c] else None,
             "rated": len(ratings[c]),
             "mean": round(statistics.fmean(ratings[c]), 3) if ratings[c] else None,
             "good": round(sum(v >= 2 for v in ratings[c]) / len(ratings[c]), 4) if ratings[c] else None}
            for c in key["names"]]
    rows.sort(key=lambda r: (-(r["mean"] if r["mean"] is not None else -1), -r["picked"], r["candidate"]))
    return {"turns": len(key["turns"]), "picked": made, "none": none, "unanswered": unanswered,
            "unreadable": unreadable, "candidates": rows}


def render_tally(t: dict) -> str:
    lines = [f"{t['turns']} turns: {t['picked']} picks, {t['none']} none, {t['unanswered']} "
             f"unanswered, {t['unreadable']} ratings or picks unreadable."]
    for r in t["candidates"]:
        rate = "n/a" if r["pick_rate"] is None else f"{100 * r['pick_rate']:.0f}%"
        mean = "n/a" if r["mean"] is None else f"{r['mean']:.2f}"
        good = "" if r["good"] is None else f", {100 * r['good']:.0f}% rated 2+"
        base = " (baseline)" if r["candidate"] in BASELINES else ""
        lines.append(f"  {r['candidate']}{base}: mean {mean} over {r['rated']}{good}; "
                     f"picked {r['picked']} of {r['offered']} ({rate})")
    return "\n".join(lines)
