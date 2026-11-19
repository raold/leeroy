"""What the romantic-threads recorder has stored (drama/pulls.py), as counts, guids and codes:
the CLI's `drama threads report`, the console's "Threads (recording)" panel and health read
this. Read-only; a database from before the tables reads as not set up. Nothing here is in
effect: no member's behaviour reads these rows."""
import json
import sqlite3
from collections import Counter

from leeroy_mind.drama.pulls import (FADE_PULL, HALF_LIFE, OPEN_AFFINITY, OPEN_PULL, OPEN_SOCIAL,
                                     OPENS_PER_NIGHT, TAKEN_MAX, THREADS_MAX)

TABLES = ("pull_event", "pull_shadow", "pull_night")
NIGHTS_MAX = 90
TOP = 15
WHY_WORDS = {
    "draw": "the night's draw said not yet",
    "affinity": f"affinity under {OPEN_AFFINITY:g} either way (one dislikes the other)",
    "social": f"under {OPEN_SOCIAL:g} of the pull is social (presence alone never opens one)",
    "cap_night": f"{OPENS_PER_NIGHT} opening a night",
    "feud": "the two are feuding",
    "ex": "exes inside their cooldown",
    "pair_open": "the pair's other direction holds the thread",
    "cap_member": "one of them is already in a thread",
    "cap_live": f"{THREADS_MAX} threads are live",
    "cap_taken": f"{TAKEN_MAX} thread with a taken member is live",
}


def _summary(text) -> dict:
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _thread(r) -> dict:
    return {"a": r["a"], "b": r["b"], "pull": round(r["pull"], 1),
            "public_pull": round(r["public_pull"], 1), "social_pull": round(r["social_pull"], 1),
            "events": r["events"],
            "first_night": r["first_night"], "last_event_night": r["last_event_night"],
            "state": r["thread_state"], "night": r["thread_night"], "why": r["thread_why"],
            "shape": r["shape"], "ceiling": r["ceiling"], "taken": bool(r["taken"])}


def _empty(rules) -> dict:
    return {"set_up": False, "in_effect": False, "rules": rules, "nights": [], "kinds": {},
            "scopes": {}, "skipped": {}, "waiting": {}, "top": [], "open": [], "ended": [],
            "held": [], "pairs": 0, "events": 0}


def threads_report(conn, *, nights: int = 14, top: int = TOP) -> dict:
    nights = max(1, min(NIGHTS_MAX, int(nights)))
    rules = {"open_pull": OPEN_PULL, "open_affinity": OPEN_AFFINITY, "fade_pull": FADE_PULL,
             "half_life_nights": HALF_LIFE, "threads_max": THREADS_MAX, "taken_max": TAKEN_MAX,
             "open_social": OPEN_SOCIAL, "opens_per_night": OPENS_PER_NIGHT}
    have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if any(t not in have for t in TABLES):
        return _empty(rules)
    was = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        recorded = []
        for r in conn.execute("SELECT night, summary FROM pull_night ORDER BY night DESC LIMIT ?",
                              (nights,)).fetchall():
            s = _summary(r["summary"])
            recorded.append({"night": r["night"], "events": int(s.get("events", 0)),
                             "kinds": dict(s.get("kinds", {})), "scopes": dict(s.get("scopes", {})),
                             "skipped": dict(s.get("skipped", {})), "pairs": int(s.get("pairs", 0)),
                             "threshold": int(s.get("threshold", 0)),
                             "opened": int(s.get("opened", 0)), "open": int(s.get("open", 0)),
                             "open_taken": int(s.get("open_taken", 0)),
                             "faded": int(s.get("faded", 0)),
                             "waiting": dict(s.get("waiting", {})),
                             "shapes": dict(s.get("shapes", {})),
                             "at": dict(s.get("at", {})), "max": dict(s.get("max", {}))})
        recorded.reverse()
        kinds, scopes, skipped, waiting = Counter(), Counter(), Counter(), Counter()
        for n in recorded:
            kinds.update(n["kinds"])
            scopes.update(n["scopes"])
            skipped.update(n["skipped"])
            waiting.update(n["waiting"])
        rows = conn.execute("SELECT * FROM pull_shadow ORDER BY pull DESC, a, b").fetchall()

        def by_state(state):
            return [_thread(r) for r in rows if r["thread_state"] == state]

        return {"set_up": True, "in_effect": False, "rules": rules, "nights": recorded,
                "kinds": dict(sorted(kinds.items())), "scopes": dict(sorted(scopes.items())),
                "skipped": dict(sorted(skipped.items())), "waiting": dict(sorted(waiting.items())),
                "events": sum(n["events"] for n in recorded),
                "pairs": sum(1 for r in rows if r["pull"] >= 1.0),
                "top": [_thread(r) for r in rows[:max(0, top)]],
                "open": by_state("open"), "ended": by_state("faded"), "held": by_state("waiting")}
    finally:
        conn.row_factory = was


def _pair(t) -> str:
    return f"{t['a']} -> {t['b']}"


def render(data: dict) -> str:
    """The report as text: guids, counts and codes only."""
    head = ("Romantic threads: recording only. Nothing below is in effect; no member's "
            "behaviour, relationship, scene, prompt or memory reads it.")
    if not data["set_up"]:
        return head + "\nNot set up yet: this mind.db has no pull tables (run `db migrate`)."
    if not data["nights"]:
        return head + '\nNo night recorded yet ([drama] threads = "record", then a night).'
    out = [head, f"\nEvents by kind, per night (last {len(data['nights'])}):"]
    kinds = sorted(data["kinds"])
    out.append("  night       events pairs  " + " ".join(kinds))
    for n in data["nights"]:
        out.append(f"  {n['night']}  {n['events']:6d} {n['pairs']:5d}  "
                   + " ".join(f"{n['kinds'].get(k, 0):>{len(k)}d}" for k in kinds))
    out.append(f"  total       {data['events']:6d}        "
               + " ".join(f"{data['kinds'][k]:>{len(k)}d}" for k in kinds))
    out.append("  by scope: " + (", ".join(f"{k} {v}" for k, v in data["scopes"].items()) or "none"))
    if data["skipped"]:
        out.append("  left out: " + ", ".join(f"{k} {v}" for k, v in data["skipped"].items()))
    out.append(f"\nTop shadow pulls (directed, of {data['pairs']} pairs at 1 or more):")
    for t in data["top"]:
        extra = " ".join(x for x in (t["state"] if t["state"] != "none" else "", t["shape"],
                                     "taken" if t["taken"] else "",
                                     f"ceiling={t['ceiling']}" if t["ceiling"] else "") if x)
        out.append(f"  {_pair(t):>14}  pull {t['pull']:5.1f}  social {t['social_pull']:5.1f}  "
                   f"public {t['public_pull']:5.1f}  events {t['events']:3d}  "
                   f"since {t['first_night']}  {extra}".rstrip())
    r = data["rules"]
    out.append(f"\nWould-be threads (pull {r['open_pull']:g}, {r['open_social']:g} of it social, "
               f"affinity {r['open_affinity']:g} or more both ways; caps {r['threads_max']} live, "
               f"{r['taken_max']} with a taken member, {r['opens_per_night']} opening a night):")
    out.append("  night       candidates opened live taken faded  shapes; held back")
    for n in data["nights"]:
        held = ", ".join(f"{k} {v}" for k, v in sorted(n["waiting"].items())) or "-"
        shapes = ", ".join(f"{k} {v}" for k, v in sorted(n["shapes"].items())) or "-"
        out.append(f"  {n['night']}  {n['threshold']:10d} {n['opened']:6d} {n['open']:4d} "
                   f"{n['open_taken']:5d} {n['faded']:5d}  {shapes}; {held}")
    out.append("\nShadow pulls per night (pulls at 10+/20+/35+, max pull men/women):")
    for n in data["nights"]:
        at, top = n["at"], n["max"]
        out.append(f"  {n['night']}  {at.get('10', 0):4d} {at.get('20', 0):4d} {at.get('35', 0):4d}   "
                   f"{top.get('men', 0):5.1f} {top.get('women', 0):5.1f}")
    for title, key in (("Would be open now", "open"), ("Would have faded", "ended")):
        out.append(f"  {title}:" + (" none" if not data[key] else ""))
        for t in data[key]:
            out.append(f"    {_pair(t)}  {t['shape'] or '-'}  since {t['night']}  pull {t['pull']:.1f}"
                       + ("  taken" if t["taken"] else "")
                       + (f"  ceiling={t['ceiling']}" if t["ceiling"] else ""))
    out.append("\nWhy candidates were held back (candidate-nights, over the nights shown):")
    if not data["waiting"]:
        out.append("  none")
    for why, count in sorted(data["waiting"].items(), key=lambda kv: (-kv[1], kv[0])):
        out.append(f"  {why:<11} {count:5d}  {WHY_WORDS.get(why, '')}")
    return "\n".join(out)
