"""The stored nights in words and numbers: what `turing report` prints and the Lab's Turing panel
shows. Read only; a database without the tables reads as not set up. The judge's phrases come
only from windows on public channels (judge.public) and are cut short."""
import json
from datetime import datetime, timedelta

from .corpus import FAMILIES, FAMILY_LABELS
from .judge import TELLS
from .store import available
from .stylo import describe

PHRASES_PER_TELL = 3
TOP_TELLS = 6


def _cutoff(now: float, days: int) -> str:
    return (datetime.fromtimestamp(now).date() - timedelta(days=days)).isoformat()


def _json(value, default):
    try:
        out = json.loads(value) if value else default
    except (ValueError, TypeError):
        return default
    return out if isinstance(out, type(default)) else default


def report(conn, *, now: float, days: int = 14) -> dict:
    if not available(conn):
        return {"available": False, "days": days, "families": []}
    since = _cutoff(now, days)
    rows = conn.execute(
        "SELECT night, family, status, human_lines, member_lines, sample, auc, auc_low, auc_high, "
        "features, judge_windows, judge_speakers, judge_correct, judge_his, judge_his_correct, tells "
        "FROM turing_night WHERE night >= ? ORDER BY night", (since,)).fetchall()
    phrases: dict[tuple[str, str], list[str]] = {}
    for family, value in conn.execute(
            "SELECT family, tells FROM turing_judgement WHERE night >= ? AND status='ok' "
            "ORDER BY night DESC, ts DESC", (since,)):
        for t in _json(value, []):
            if (isinstance(t, dict) and t.get("phrase") and t.get("truth") == "bot"
                    and t.get("judged") == "bot" and t.get("code") in TELLS):
                kept = phrases.setdefault((family, t["code"]), [])
                if len(kept) < PHRASES_PER_TELL and t["phrase"] not in kept:
                    kept.append(t["phrase"])
    out = []
    for family in FAMILIES:
        mine = [r for r in rows if r[1] == family]
        if not mine:
            continue
        points, caught = [], {}
        totals = {"speakers": 0, "correct": 0, "his": 0, "his_correct": 0, "windows": 0}
        latest = None
        for (night, _, status, human, member, sample, auc, low, high, feats, windows, speakers,
             correct, his, his_correct, tells) in mine:
            points.append({"night": night, "auc": auc, "low": low, "high": high,
                           "judge": correct / speakers if speakers else None, "speakers": speakers})
            totals["speakers"] += speakers
            totals["correct"] += correct
            totals["his"] += his
            totals["his_correct"] += his_correct
            totals["windows"] += windows
            for code, n in _json(tells, {}).get("caught", {}).items():
                if code in TELLS and isinstance(n, int):
                    caught[code] = caught.get(code, 0) + n
            if status is not None:
                features = [f for f in _json(feats, []) if isinstance(f, dict)]
                latest = {"night": night, "status": status, "human_lines": human,
                          "member_lines": member, "sample": sample, "auc": auc, "low": low,
                          "high": high, "features": [dict(f, words=describe(f)) for f in features]}
        judge = dict(totals, accuracy=totals["correct"] / totals["speakers"] if totals["speakers"] else None,
                     his_accuracy=totals["his_correct"] / totals["his"] if totals["his"] else None)
        tells = [{"code": code, "label": TELLS[code], "count": n,
                  "phrases": phrases.get((family, code), [])}
                 for code, n in sorted(caught.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP_TELLS]]
        out.append({"family": family, "label": FAMILY_LABELS[family], "points": points,
                    "latest": latest, "judge": judge, "tells": tells})
    return {"available": True, "days": days, "families": out}


def panel(conn, *, now: float, days: int = 14) -> dict:
    """The Lab's Turing panel: the same report."""
    return report(conn, now=now, days=days)


def _pct(value) -> str:
    return "n/a" if value is None else f"{100 * value:.0f}%"


def _auc(value, low, high) -> str:
    if value is None:
        return "n/a"
    band = f" ({low:.2f}-{high:.2f})" if low is not None and high is not None else ""
    return f"{value:.2f}{band}"


def render_report(data: dict) -> str:
    if not data.get("available"):
        return ("Not set up yet: no Turing nights are stored. Turn on [turing] enabled in "
                "mind.toml; each night then writes one. `turing run` measures now.")
    lines = [f"Turing judge, the last {data['days']} days. AUC 0.50 means members read like you; "
             "1.00 means they are always told apart."]
    if not data["families"]:
        lines.append("No nights stored in that period.")
    for fam in data["families"]:
        lines += ["", fam["label"]]
        for p in fam["points"]:
            judge = (f"  judge {_pct(p['judge'])} of {p['speakers']} speakers" if p["speakers"] else "")
            lines.append(f"  {p['night']}  AUC {_auc(p['auc'], p['low'], p['high'])}{judge}")
        latest = fam["latest"]
        if latest is not None:
            if latest["status"] == "ok":
                lines.append(f"  Style tells on {latest['night']} ({latest['sample']} lines each):")
                lines += [f"    {f['words']}" for f in latest["features"]] or ["    none stand out"]
            else:
                lines.append(f"  {latest['night']}: not measured ({latest['status']}: "
                             f"{latest['human_lines']} of your lines, {latest['member_lines']} of theirs)")
        j = fam["judge"]
        if j["speakers"]:
            lines.append(f"  Blind judge: {_pct(j['accuracy'])} right over {j['speakers']} speakers "
                         f"in {j['windows']} windows; you read as human {_pct(j['his_accuracy'])} "
                         f"of {j['his']} times.")
        for t in fam["tells"]:
            quoted = "".join(f' "{p}"' for p in t["phrases"])
            lines.append(f"    {t['label']} ({t['count']}){quoted}")
    return "\n".join(lines)


def render_run(data: dict) -> str:
    """`turing run`: the stylometric part now, in words."""
    end = datetime.fromtimestamp(data["end"]).strftime("%Y-%m-%d %H:%M")
    lines = [f"Style distinguishability over the {data['days']} days to {end}. "
             "AUC 0.50 means members read like you; 1.00 means they are always told apart."]
    for family in FAMILIES:
        r = data["families"][family]
        lines += ["", f"{FAMILY_LABELS[family]}: {r['human_lines']} of your lines, "
                      f"{r['member_lines']} of theirs"]
        if r["status"] != "ok":
            lines.append(f"  not measured ({r['status']}: needs at least 30 of each)")
            continue
        lines.append(f"  AUC {_auc(r['auc'], r['low'], r['high'])} from {r['sample']} lines each, "
                     f"{r['groups']} conversation stretches")
        lines += [f"  {describe(f)}" for f in r["features"]] or ["  no single feature stands out"]
    return "\n".join(lines)
