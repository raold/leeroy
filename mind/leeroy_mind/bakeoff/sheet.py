"""The owner's blind pick: `bakeoff sheet` writes sheet.md (n turns that answer his line, each
with the conversation before it, masked as the judge masks, and every candidate's reply, the
live model's included, in a shuffled order labelled A, B, C...) and key.json beside it (which
label is which candidate). `bakeoff tally --picks picks.json` scores his picks against the key.
The sheet holds his private conversations, explicit ones too: it stays on this machine."""
import json
import random
from collections import Counter
from pathlib import Path

from leeroy_mind.turing.corpus import FAMILIES, FAMILY_LABELS

from .score import BASELINES

LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
HEADER = ("# Bake-off: pick the reply that reads most like a real person\n\n"
          "**Local only.** This sheet quotes your own whispers, DMs and guild chat (explicit ones "
          "too). Never post, upload or commit it; delete it when you are done.\n\n"
          "For each turn, read the conversation (you are `you`; the member who answers is `P1`; "
          "other names are masked) and pick the one reply you would believe a real player typed. "
          "Write your picks in `picks.json` beside this file, e.g. `{\"1\": \"B\", \"2\": \"A\"}`; "
          "`\"-\"` means none of them. Then run `bakeoff tally --picks picks.json --out <this "
          "folder>`. The key is in key.json: don't open it first.\n")


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


def write(conn, out: Path, *, n: int, names: list[str], seed: str) -> dict:
    """sheet.md and key.json in `out`; the counts."""
    turns = [dict(r) for r in conn.execute("SELECT * FROM turn WHERE answers=1 ORDER BY id")]
    rows = {}
    for r in conn.execute("SELECT candidate, turn, status, masked FROM result"):
        if r["candidate"] in names:
            rows.setdefault(r["turn"], {})[r["candidate"]] = r
    usable = [t for t in turns if sum(rows.get(t["id"], {}).get(c) is not None
                                      and rows[t["id"]][c]["status"] in ("ok", "silent")
                                      for c in names) >= 2]
    picked = choose(usable, n, seed)
    rng = random.Random(f"bakeoff-sheet-order|{seed}")
    parts, key = [HEADER], {}
    for i, t in enumerate(picked, 1):
        options = [c for c in names if rows[t["id"]].get(c) is not None
                   and rows[t["id"]][c]["status"] in ("ok", "silent")]
        rng.shuffle(options)
        key[str(i)] = {"turn": t["id"], "family": t["family"],
                       "options": {LETTERS[j]: c for j, c in enumerate(options)}}
        parts.append(f"\n## {i}. {FAMILY_LABELS[t['family']]} ({t['kind']})\n")
        for seconds, label, text in json.loads(t["context"]):
            parts.append(f"    [{_offset(seconds)}] {label}: {text}")
        parts.append("\nP1 answers:\n")
        for j, c in enumerate(options):
            r = rows[t["id"]][c]
            lines = json.loads(r["masked"]) if r["status"] == "ok" else []
            said = " / ".join(lines) if lines else "(says nothing)"
            parts.append(f"- **{LETTERS[j]}**: {said}")
    text = "\n".join(parts) + "\n"
    out.mkdir(parents=True, exist_ok=True)
    (out / "sheet.md").write_bytes(text.encode("utf-8"))
    (out / "key.json").write_bytes((json.dumps({"names": names, "turns": key}, indent=2,
                                               ensure_ascii=False) + "\n").encode("utf-8"))
    return {"turns": len(picked), "candidates": len(names)}


def tally(key: dict, picks) -> dict:
    """His picks scored against the key: per candidate, how often it was picked and how often
    it was on offer."""
    if isinstance(picks, list):
        picks = {str(i): p for i, p in enumerate(picks, 1)}
    if not isinstance(picks, dict):
        raise ValueError("picks must be an object of turn number to letter")
    picked: Counter = Counter()
    offered: Counter = Counter()
    none = unknown = 0
    for number, entry in key["turns"].items():
        for c in entry["options"].values():
            offered[c] += 1
        choice = picks.get(number)
        if choice is None:
            unknown += 1
            continue
        choice = str(choice).strip().upper()
        if choice in ("-", "", "NONE"):
            none += 1
        elif choice in entry["options"]:
            picked[entry["options"][choice]] += 1
        else:
            unknown += 1
    rows = [{"candidate": c, "picked": picked[c], "offered": offered[c],
             "share": round(picked[c] / offered[c], 4) if offered[c] else None}
            for c in key["names"]]
    rows.sort(key=lambda r: (-r["picked"], r["candidate"]))
    return {"turns": len(key["turns"]), "picked": sum(picked.values()), "none": none,
            "unanswered": unknown, "candidates": rows}


def render_tally(t: dict) -> str:
    lines = [f"{t['picked']} picks over {t['turns']} turns ({t['none']} none, "
             f"{t['unanswered']} unanswered or unreadable)."]
    for r in t["candidates"]:
        share = "n/a" if r["share"] is None else f"{100 * r['share']:.0f}%"
        base = " (baseline)" if r["candidate"] in BASELINES else ""
        lines.append(f"  {r['candidate']}{base}: picked {r['picked']} of {r['offered']} ({share})")
    return "\n".join(lines)
