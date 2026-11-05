"""Reproducible weekly observations, with unevidenced learning milestones false."""

import json
import re
import statistics
from collections import defaultdict
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from leeroy_mind.wow import BOSS_NAMES, boss_name, is_raid_fight

from .score import SCORE_BASES, _duration, _finite, _scored_rows, bootstrap_diff, is_farm, pull_score


BASELINE_SECONDS = 28 * 86400
MAX_ROWS = 200
MAX_PULLS = 20000
MAX_ARMS = 16
MAX_RECEIPT_BYTES = 8 * 1024 * 1024
MAX_RECEIPT_NODES = 100000
_SECTIONS = ("trials", "promotions", "rollbacks", "suspended", "rotations", "spread", "mentees", "rivals")
_MILESTONES = ("four_weeks", "won_tactic", "won_rotation", "gate_within_1ms", "voice_run", "cast_callouts")


def _blank():
    return dict(since=None, gate=None, contexts=[], nothing_improved=[],
        **{key: [] for key in _SECTIONS}, done_when=dict.fromkeys(_MILESTONES, False),
        evidence=dict(schema="available", truncated=[], voice="unverified",
            scope="bounded records through report cutoff; card statuses and gate are current snapshots",
            limits=dict(pulls=MAX_PULLS, rows=MAX_ROWS, arms_per_trial=MAX_ARMS),
            rotation="unavailable: requested_pre_ai is not verified boss damage",
            tactic="offline-validated tactic adopted in a human raid; no causal human improvement claim",
            spread="desired setups; live adoption requires separate application proof",
            mentees="within-week effective-healing z observations; not causal mentoring effects"))


def _json(value):
    try:
        if not isinstance(value, (str, bytes)) or len(value) > MAX_RECEIPT_BYTES:
            return None
        parsed = json.loads(value, parse_constant=_invalid_constant)
        pending, count = [(parsed, 0)], 0
        while pending:
            item, depth = pending.pop()
            count += 1
            if depth > 16 or count > MAX_RECEIPT_NODES:
                return None
            if isinstance(item, (dict, list)):
                if len(item) > MAX_PULLS:
                    return None
                pending.extend((v, depth + 1) for v in (item.values() if isinstance(item, dict) else item))
        return parsed
    except (ValueError, TypeError, RecursionError):
        return None


def _records(conn, prefix, limit=MAX_ROWS):
    return [(r["key"], _json(r["value"])) for r in conn.execute(
        "SELECT key,CASE WHEN length(value)<=? THEN value END AS value FROM meta "
        "WHERE key GLOB ? ORDER BY key DESC LIMIT ?", (MAX_RECEIPT_BYTES, prefix + "*", limit))]


def _time(value, cutoff):
    return type(value) in (int, float) and _finite(value) and 0 <= value <= cutoff


def _positive(value):
    return type(value) is int and 0 < value <= 0xffffffff


def _pull_valid(row, cutoff):
    """Validate raw ledger types before score helpers, including their wipe path."""
    return (isinstance(row, dict) and type(row.get("id")) is str and 0 < len(row["id"]) <= 200
        and all(type(row.get(k)) is int and row[k] >= 0 for k in
                ("start_ms", "end_ms", "deaths", "failed_mechanics", "map", "boss_id"))
        and _time(row.get("received"), cutoff)
        and 0 <= row["start_ms"] < row["end_ms"] <= row["received"] * 1000
        and type(row.get("difficulty")) is int and row["difficulty"] in (0, 1, 2, 3)
        and type(row.get("size")) is int and row["size"] == (25 if row["difficulty"] & 1 else 10)
        and row.get("context") == f"{row['map']}:{row['boss_id']}:{row['difficulty']}"
        and is_raid_fight(row["map"], row["boss_id"])
        and type(row.get("members")) is int and 0 < row["members"] <= row["size"]
        and all(type(row.get(k)) is int and row[k] in (0, 1) for k in
                ("complete", "scoreable", "assisted", "checks_complete"))
        and row["complete"] == row["scoreable"] == 1
        and type(row.get("outcome")) is str and row["outcome"] in {"killed", "wiped"}
        and type(row.get("kind")) is str and row["kind"] in {"his", "offline", "benchmark"}
        and type(row.get("score_basis")) is str and row["score_basis"] in SCORE_BASES
        and type(row.get("score")) in (int, float) and _finite(row["score"]))


def _cleanup(value):
    from .protocol import TrainingCleanup
    try:
        if (not isinstance(value, dict) or len(value) > MAX_ROWS
                or not isinstance(value.get("members"), list) or len(value["members"]) > 25):
            return None
        TrainingCleanup(**(value | {"members": tuple(value["members"])}))
        return value
    except (TypeError, ValueError):
        return None


def _safe(value, depth=0):
    """Bound untrusted receipt display without exposing embeddings/raw prompts."""
    if depth > 8:
        return None
    if isinstance(value, dict):
        return {str(k)[:80]: _safe(v, depth+1) for k, v in list(value.items())[:MAX_ROWS]
                if k not in {"embedding", "prompt"}}
    if isinstance(value, (list, tuple)):
        return [_safe(v, depth+1) for v in value[:MAX_ROWS]]
    if isinstance(value, str):
        return value[:500]
    return value if value is None or type(value) is bool or _finite(value) else None


def _schema(conn):
    required = {
        "meta": {"key", "value"},
        "tg_pull": set("id context map instance_id boss_id difficulty size outcome start_ms end_ms members assisted boss_entry upd_mean_ms kind lead_guid raid_id card_id arm_id change complete scoreable deaths failed_mechanics score covariates checks_version checks_complete checks_coverage score_basis received damage_basis healing_basis spell_basis interrupt_basis".split()),
        "tg_trial": set("id scope lever metric status opened closed verdict".split()),
        "tg_arm": set("id trial card champion status wipes".split()),
        "tg_obs": {"arm", "pull", "context"},
        "tg_card": set("id context prev_champion created status version call directives assignment vetoes".split()),
        "tg_rotation": set("id name class spec_tab status origin parent created".split()),
        "tg_setup": {"id", "guid", "copied_from", "since"},
        "tg_dummy": set("id rotation guid round seconds dps dropped ts damage_basis spell_basis".split()),
        "tg_member": set("pull guid healing class spec_tab role active_ms".split()),
        "tg_benchmark": set("week raid_key guild_id members bosses outcome kills wipes ts".split()),
        "guild_raid": set("raid_id started ended offline lead_guid raid_key size heroic".split()),
    }
    return sorted(table for table, fields in required.items()
                  if not fields <= {r[1] for r in conn.execute(f"PRAGMA table_info({table})")})


def _human_use(conn, row, card, closed, basis):
    from leeroy_mind.guild.raids import CATALOG
    if (row["kind"] != "his" or row["card_id"] != card or row["arm_id"] is not None
            or row["score_basis"] != basis or row["start_ms"] < closed * 1000):
        return False
    raid = conn.execute("SELECT * FROM guild_raid WHERE raid_id=?", (row["raid_id"],)).fetchone()
    catalog = CATALOG.get(raid["raid_key"]) if raid else None
    plan = _meta(conn, f"guild:raid:{row['raid_id']}:plan")
    if (raid is None or catalog is None or catalog.map_id != row["map"]
            or raid["size"] != row["size"] or bool(raid["heroic"]) != (row["difficulty"] >= 2)
            or not isinstance(plan, dict) or plan.get("human_in_raid") is not True or plan.get("offline") is not False
            or any(plan.get(k) != raid[k] for k in ("raid_key", "size", "heroic", "lead_guid"))
            or raid["offline"] != 0 or raid["lead_guid"] != row["lead_guid"]
            or not _time(raid["started"], row["start_ms"] / 1000)
            or raid["ended"] is not None and not _time(raid["ended"], float("inf"))
            or raid["started"] * 1000 > row["start_ms"]
            or raid["ended"] is not None and raid["ended"] * 1000 < row["end_ms"]
            or _meta(conn, f"guild:raid:{row['raid_id']}:instance") != row["instance_id"]):
        return False
    history = _meta(conn, f"tg:in-play:{row['raid_id']}:{row['context']}")
    if not isinstance(history, list) or len(history) > 64:
        return False
    prior = [r for r in history if isinstance(r, dict) and type(r.get("at_ms")) is int
             and r["at_ms"] <= row["start_ms"]] if isinstance(history, list) else []
    chosen = max(prior, key=lambda r: r["at_ms"], default={})
    return (chosen.get("card_id") == card and chosen.get("arm_id") is None
            and chosen.get("change") == row["change"] == ""
            and chosen["at_ms"] >= raid["started"] * 1000)


def _source_matches(conn, source, row, *, champion, median):
    if not isinstance(source, dict) or row is None:
        return False
    roles = [dict(r) for r in conn.execute(
        "SELECT guid,role,class,spec_tab FROM tg_member WHERE pull=? ORDER BY guid LIMIT 26", (row["id"],))]
    expected_score = pull_score(row["outcome"], row["deaths"],
        row["failed_mechanics"] if row["score_basis"] == "mechanics_deaths_time" else 0,
        _duration(row), median)
    return (len(roles) <= 25 and source.get("member_roles") == roles
        and source.get("champion") == int(champion) and source.get("score") == expected_score
        and source.get("ledger_score") == row["score"]
        and all(source.get(k) == value for k, value in row.items() if k != "score"))


def _trial_references(conn, saved, trial, pulls, cutoff):
    """Every context retains its exact admission cohort and fixed score basis."""
    contexts, refs = saved.get("contexts"), saved.get("references")
    if (not isinstance(contexts, list) or not 1 <= len(contexts) <= MAX_ROWS
            or not all(type(c) is str for c in contexts) or len(set(contexts)) != len(contexts)
            or not isinstance(refs, dict) or len(refs) != len(contexts) or set(refs) != set(contexts)
            or type(saved.get("basis")) is not str or saved["basis"] not in SCORE_BASES
            or not _time(trial["opened"], cutoff)):
        return False
    for context in contexts:
        ref = refs[context]
        if (not isinstance(ref, dict) or set(ref) != {"median_kill_s", "rows"}
                or not isinstance(ref["rows"], list) or not 1 <= len(ref["rows"]) <= MAX_ROWS):
            return False
        history = [p for p in pulls if p["context"] == context and p["score_basis"] == saved["basis"]
                   and p["received"] <= trial["opened"]][-MAX_ROWS:]
        durations = [_duration(p) for p in history if p["outcome"] == "killed"]
        median = statistics.median(durations) if durations else None
        if (ref["median_kill_s"] != median or len(ref["rows"]) != len(history)
                or not is_farm([p["outcome"] for p in history])
                or any(not _source_matches(conn, source, row,
                    champion=row["card_id"] == saved.get("champion"), median=median)
                    for source, row in zip(ref["rows"], history))):
            return False
    return True


def _trials(conn, result, pulls, cutoff):
    from .drift import known
    by_id = {r["id"]: r for r in pulls}
    rows = conn.execute("SELECT * FROM tg_trial WHERE opened<=? ORDER BY opened DESC,id DESC LIMIT ?",
                        (cutoff, MAX_ROWS+1)).fetchall()
    if len(rows) > MAX_ROWS:
        result["evidence"]["truncated"].append("trials")
    for row in rows[:MAX_ROWS]:
        saved = _meta(conn, f"tg:trial:{row['id']}")
        saved = saved if isinstance(saved, dict) else {}
        if (not isinstance(saved.get("contexts"), list) or not 1 <= len(saved["contexts"]) <= MAX_ROWS
                or not all(type(c) is str for c in saved["contexts"])
                or not isinstance(saved.get("arms"), dict) or len(saved["arms"]) > MAX_ARMS):
            saved = {}
        arms = [dict(a) for a in conn.execute("SELECT * FROM tg_arm WHERE trial=? ORDER BY id LIMIT ?",
                                            (row["id"], MAX_ARMS+1))]
        if len(arms) > MAX_ARMS:
            result["evidence"]["truncated"].append("trial_arms")
            continue
        references_valid = _trial_references(conn, saved, row, pulls, cutoff)
        observation_cutoff = row["closed"] if _time(row["closed"], cutoff) else cutoff
        observations = []
        for arm in arms:
            valid = []
            recorded = conn.execute("SELECT * FROM tg_obs WHERE arm=? LIMIT ?", (arm["id"], MAX_ROWS+1)).fetchall()
            if len(recorded) > MAX_ROWS:
                result["evidence"]["truncated"].append("trial_observations")
                references_valid = False
            for obs in recorded[:MAX_ROWS]:
                pull = by_id.get(obs["pull"])
                receipt = _meta(conn, f"tg:obs:{arm['id']}:{obs['pull']}")
                if (references_valid and pull is not None and isinstance(receipt, dict)
                        and saved.get("basis") == pull["score_basis"]
                        and pull["context"] in saved.get("contexts", [])
                        and row["opened"] * 1000 <= pull["start_ms"]
                        and pull["received"] <= observation_cutoff
                        and obs["context"] == pull["context"] and pull["card_id"] == arm["card"]
                        and (pull["kind"] == "offline" and pull["arm_id"] == arm["id"]
                             or pull["kind"] == "his" and pull["arm_id"] is None and arm["champion"] == 1)
                        and _source_matches(conn, receipt, pull, champion=bool(arm["champion"]),
                            median=saved["references"][pull["context"]]["median_kill_s"])):
                    valid.append(obs["pull"])
            arm["verified_observations"] = len(valid)
            observations.extend(valid)
        result["trials"].append(dict(id=row["id"], scope=row["scope"], lever=row["lever"],
            status=row["status"], metric=row["metric"], opened=row["opened"], closed=row["closed"],
            basis=saved.get("basis"), treatment=saved.get("treatment"), revisions=_safe(saved.get("revisions")),
            arms=arms, provenance="recorded" if saved else "unavailable"))
        if (row["status"] != "promoted" or row["metric"] != "raid" or row["lever"] in {"stock", "rotation"}
                or not _time(row["closed"], cutoff) or not known(saved.get("revisions"))
                or not references_valid
                or not isinstance(saved.get("arms"), dict)
                or saved.get("treatment") != "tactic_only_fixed_speech_and_assignments"):
            continue
        winners = [a for a in arms if a["status"] == "won" and a["champion"] == 0 and a["verified_observations"] > 0]
        if len(winners) != 1:
            continue
        winner = winners[0]
        verdicts = _records(conn, f"tg:verdict:{row['id']}:", MAX_ROWS+1)
        if len(verdicts) > MAX_ROWS:
            result["evidence"]["truncated"].append("trial_verdicts")
            continue
        valid_verdict = any(isinstance(v, dict) and v.get("action") == "promote" and v.get("arm") == winner["id"]
            and v.get("basis") == saved.get("basis") and key.split(":", 3)[-1] in observations
            and v.get("card") == by_id[key.split(":", 3)[-1]]["card_id"]
            and v.get("context") == by_id[key.split(":", 3)[-1]]["context"]
            and by_id[key.split(":", 3)[-1]]["received"] <= row["closed"]
            for key, v in verdicts)
        card = conn.execute("SELECT * FROM tg_card WHERE id=?", (winner["card"],)).fetchone()
        signature = saved["arms"].get(str(winner["id"]))
        champions = [a["card"] for a in arms if a["champion"]]
        if (not valid_verdict or card is None or card["context"] != row["scope"] or card["prev_champion"] not in champions
                or not isinstance(signature, dict)
                or any(signature.get(k) != card[k] for k in ("id", "context", "version", "call"))
                or any(signature.get(k) != _json(card[k]) for k in ("directives", "assignment"))):
            continue
        vetoes = _json(card["vetoes"])
        if not isinstance(vetoes, list) or len(vetoes) > MAX_ROWS:
            continue
        used = [p for p in pulls if p["context"] in saved.get("contexts", [])
                and p["context"] not in vetoes
                and _human_use(conn, p, card["id"], row["closed"], saved.get("basis"))]
        result["promotions"].append(dict(trial=row["id"], card=card["id"], context=row["scope"],
            at=row["closed"], basis=saved.get("basis"), human_use_pulls=[p["id"] for p in used[:MAX_ROWS]],
            human_outcomes=dict(pulls=len(used), kills=sum(p["outcome"] == "killed" for p in used),
                                deaths_per_pull=statistics.fmean(p["deaths"] for p in used) if used else None)))
    result["done_when"]["won_tactic"] = any(p["human_use_pulls"] for p in result["promotions"])


def _cards_and_rotations(conn, result, cutoff):
    for key, value in _records(conn, "tg:rollback:"):
        try:
            context, stamp = key[len("tg:rollback:"):].rsplit(":", 1)
            stamp = float(stamp)
        except (ValueError, TypeError):
            continue
        if (not isinstance(value, dict) or not _time(stamp, cutoff)
                or not all(_positive(value.get(k)) for k in ("old", "new"))):
            continue
        cards = [conn.execute("SELECT context FROM tg_card WHERE id=?", (value.get(k),)).fetchone() for k in ("old", "new")]
        if all(card is not None and card[0] == context for card in cards) and value.get("old") != value.get("new"):
            result["rollbacks"].append(dict(context=context, at=stamp, old=value["old"], new=value["new"], commands_undone="unverified"))
    for table, kind in (("tg_card", "card"), ("tg_rotation", "rotation")):
        for row in conn.execute(f"SELECT * FROM {table} WHERE status='suspended' AND created<=? ORDER BY id DESC LIMIT ?", (cutoff, MAX_ROWS)):
            result["suspended"].append(dict(kind=kind, id=row["id"], context=row["context"] if kind == "card" else None,
                reason="current_vocabulary_or_validation_refusal; exact cause unavailable"))
    attempts = {}
    for _, receipt in _records(conn, "tg:lab:attempt:"):
        if (not isinstance(receipt, dict) or receipt.get("metric") != "dummy_requested_damage_proxy"
                or receipt.get("restoration") != "complete_no_desired_setup"
                or receipt.get("ack") != "accepted" or receipt.get("bad")
                or not isinstance(receipt.get("rows"), dict) or len(receipt["rows"]) > MAX_ROWS):
            continue
        for guid, ident in receipt["rows"].items():
            if type(ident) is int:
                attempts[ident] = (str(guid), receipt)
    for row in conn.execute("SELECT id,name,class,spec_tab,status,origin,parent,created FROM tg_rotation "
                            "WHERE created<=? ORDER BY id DESC LIMIT ?", (cutoff, MAX_ROWS)):
        rates = []
        samples = conn.execute("SELECT * FROM tg_dummy WHERE rotation=? AND ts<=? ORDER BY id LIMIT ?",
                               (row["id"], cutoff, MAX_ROWS)).fetchall()
        for sample in samples:
            guid, receipt = attempts.get(sample["id"], (None, {}))
            terminal, measurements = receipt.get("terminal"), receipt.get("results")
            observed = measurements.get(guid) if isinstance(measurements, dict) else None
            if (not isinstance(terminal, dict) or not isinstance(observed, dict)
                    or terminal.get("state") != "done" or terminal.get("reason")
                    or not isinstance(terminal.get("guids"), list) or len(terminal["guids"]) > 25
                    or type(sample["seconds"]) not in (int, float) or not _time(sample["seconds"], 86400)
                    or receipt.get("candidate_name") != row["name"] or observed.get("guid") != sample["guid"]
                    or sample["guid"] not in terminal.get("guids", []) or sample["dropped"] or observed.get("dropped")
                    or observed.get("damage_basis") != "requested_pre_ai" or sample["damage_basis"] != "requested_pre_ai"
                    or observed.get("spell_basis") != "best_effort_thread_pair"
                    or sample["spell_basis"] != "best_effort_thread_pair"
                    or type(observed.get("ms")) is not int or not sample["seconds"] * 1000 <= observed["ms"] <= sample["seconds"] * 1000 + 5000
                    or type(observed.get("ts")) is not int or type(terminal.get("ts")) is not int
                    or type(receipt.get("sent_ms")) is not int or sample["ts"] != receipt["sent_ms"] / 1000
                    or observed["ms"] > observed["ts"] - receipt["sent_ms"] or observed["ts"] > terminal["ts"]
                    or not _time(terminal["ts"] / 1000, cutoff)
                    or not _time(observed.get("damage"), float("inf")) or observed["ms"] <= 0):
                continue
            rate = observed["damage"] * 1000 // observed["ms"]
            if 0 < rate == sample["dps"]:
                rates.append(rate)
        result["rotations"].append(dict(row) | dict(field_performance="unavailable", metric="dummy_requested_damage_proxy",
            recorded_dummy_rows=len(samples), verified_proxy_samples=len(rates),
            requested_damage_per_second=statistics.fmean(rates) if rates else None))


def _spread_scores(conn, receipt, cutoff):
    """Reproduce the complete scorer, including sparse peers absent from ranks.

    Returned scores alone are not a snapshot of every normalization contributor.
    Changed/missing original SQL therefore invalidates the saved numeric trend.
    """
    cohort = receipt.get("score_cohort")
    bounds, offset, key = receipt.get("week_bounds"), receipt.get("utc_offset_seconds"), receipt.get("timezone_key")
    if (type(receipt.get("normalization_version")) is not int or receipt["normalization_version"] != 1
            or receipt.get("cohort_kind") != "rank_eligible_scores_requires_original_sql"
            or not isinstance(cohort, dict) or not 1 <= len(cohort) <= MAX_ROWS
            or not isinstance(bounds, list) or len(bounds) != 2
            or not all(_time(t, float("inf")) for t in bounds)
            or not bounds[0] <= receipt["at"] < bounds[1]
            or type(offset) not in (int, float) or not _finite(offset) or not -86400 < offset < 86400
            or key is not None and (type(key) is not str or len(key) > 100)):
        return {}
    for guid, item in cohort.items():
        if (type(guid) is not str or not guid.isascii() or not guid.isdigit() or not _positive(int(guid))
                or not isinstance(item, dict) or type(item.get("score")) not in (int, float) or not _finite(item["score"])
                or not isinstance(item.get("identity"), list) or len(item["identity"]) != 3
                or not isinstance(item.get("pulls"), list) or not 4 <= len(item["pulls"]) <= MAX_ROWS):
            return {}
        seen = set()
        for point in item["pulls"]:
            if (not isinstance(point, dict) or type(point.get("pull")) is not str or point["pull"] in seen
                    or point.get("guid") != int(guid) or point.get("identity") != item["identity"]
                    or type(point.get("z")) not in (int, float) or not _finite(point["z"])):
                return {}
            seen.add(point["pull"])
        if item["score"] != statistics.fmean(p["z"] for p in item["pulls"]):
            return {}
    # The producer walks all historical guild rows. Refuse work beyond this
    # reader's bounds rather than recomputing from an incomplete SQL subset.
    for table, limit in (("tg_pull", MAX_PULLS), ("guild_raid", MAX_PULLS), ("tg_member", MAX_PULLS * 25)):
        if conn.execute(f"SELECT count(*) FROM (SELECT 1 FROM {table} LIMIT ?)", (limit+1,)).fetchone()[0] > limit:
            return {}
    try:
        from .spread import _week_bounds, score_evidence
        tz = ZoneInfo(key) if key is not None else timezone(timedelta(seconds=offset))
        if (datetime.fromtimestamp(receipt["at"], tz).utcoffset().total_seconds() != offset
                or list(_week_bounds(receipt["week"], tz)) != bounds):
            return {}
        actual = score_evidence(conn, receipt["week"], now=receipt["at"], tz=tz)
        if _json(json.dumps(actual, allow_nan=False)) != cohort:
            return {}
    except (ImportError, ZoneInfoNotFoundError, ValueError, TypeError, KeyError, OverflowError):
        return {}
    return {int(guid): dict(z=item["score"], pulls=len(item["pulls"]), identity=item["identity"])
            for guid, item in cohort.items()}


def _spread(conn, result, week, cutoff, names):
    weekly = {}
    for key, receipt in _records(conn, "tg:spread:week:", 8):
        if (not isinstance(receipt, dict) or receipt.get("state") != "desired"
                or not isinstance(receipt.get("week"), str) or not re.fullmatch(r"[0-9]{4}-W[0-9]{2}", receipt["week"])
                or key != "tg:spread:week:" + receipt["week"]
                or not _time(receipt.get("at"), cutoff) or not isinstance(receipt.get("changes"), list)
                or len(receipt["changes"]) > MAX_ROWS):
            continue
        verified = _spread_scores(conn, receipt, cutoff)
        scores = {}
        for change in receipt["changes"]:
            if not isinstance(change, dict) or not _positive(change.get("setup")):
                continue
            setup = conn.execute("SELECT * FROM tg_setup WHERE id=?", (change.get("setup"),)).fetchone()
            if (setup is None or setup["guid"] != change.get("guid") or setup["copied_from"] != change.get("copied_from")
                    or setup["since"] != receipt["at"] or change.get("application") != "deferred"):
                continue
            if receipt.get("week") == week:
                result["spread"].append(dict(setup=setup["id"], guid=setup["guid"], name=names(setup["guid"]),
                    copied_from=setup["copied_from"], application="desired_only", reason=receipt.get("reason")))
            evidence = change.get("evidence")
            if not isinstance(evidence, dict) or len(evidence) > 2:
                continue
            sources = {str(g) for g in (change.get("guid"), change.get("copied_from")) if _positive(g)}
            cohort = receipt.get("score_cohort")
            cohort = cohort if isinstance(cohort, dict) else {}
            if set(evidence) != sources or any(cohort.get(g) != item for g, item in evidence.items()):
                continue
            scores.update({int(g): verified[int(g)] for g in evidence if int(g) in verified})
        weekly[receipt.get("week")] = scores
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='mentor'").fetchone():
        return
    for pair in conn.execute("SELECT * FROM mentor WHERE started<=? AND ends>? AND (ended IS NULL OR ended>?) LIMIT ?",
                             (cutoff, cutoff, cutoff, MAX_ROWS)):
        weeks = []
        monday = date.fromisocalendar(int(week[:4]), int(week[6:]), 1)
        for delta in (3, 2, 1, 0):
            year, number, _ = (monday - timedelta(weeks=delta)).isocalendar()
            label = f"{year}-W{number:02}"
            point = weekly.get(label, {}).get(pair["mentee"])
            weeks.append(dict(week=label, z=point["z"] if point else None,
                              pulls=point["pulls"] if point else 0, identity=point["identity"] if point else None))
        complete = all(p["z"] is not None for p in weeks) and all(p["identity"] == weeks[0]["identity"] for p in weeks)
        result["mentees"].append(dict(mentor=pair["mentor"], mentee=pair["mentee"], weeks=weeks,
            four_week_change=weeks[-1]["z"] - weeks[0]["z"] if complete else None,
            coverage="four_comparable_weekly_receipts" if complete else "unavailable_or_partial"))


def _rivals(conn, result, week, cutoff):
    row = conn.execute("SELECT * FROM tg_benchmark WHERE week=? AND ts<=?", (week, cutoff)).fetchone()
    if row is None:
        return
    receipt = _meta(conn, "tg:benchmark:result:" + week)
    status = "unavailable"
    comparisons = []
    if isinstance(receipt, dict):
        saved, state = receipt.get("result"), receipt.get("receipt")
        proof = _cleanup(state.get("proof")) if isinstance(state, dict) else None
        binding = {k: v for k, v in proof.items() if k not in {"id", "ts", "state"}} if isinstance(proof, dict) else {}
        if isinstance(binding.get("members"), list):
            binding["members"] = sorted(binding["members"])
        if (isinstance(saved, dict) and isinstance(proof, dict) and proof.get("state") == "complete"
                and _time(state.get("started"), cutoff) and state["started"] * 1000 <= proof["ts"] <= cutoff * 1000
                and type(state.get("map")) is int and type(state.get("instance")) is int
                and isinstance(state.get("pulls"), list) and len(state["pulls"]) <= 256
                and all(type(p) is str for p in state["pulls"])
                and len(set(state["pulls"])) == len(state["pulls"])
                and isinstance(state.get("bosses"), list) and 0 < len(state["bosses"]) <= MAX_ROWS
                and all(type(b) is int
                    and (state.get("map"), b) in BOSS_NAMES and is_raid_fight(state["map"], b) for b in state["bosses"])
                and len(set(state["bosses"])) == len(state["bosses"])
                and type(state.get("size")) is int and state["size"] in (10, 25) and type(state.get("heroic")) is bool
                and state.get("query_receipt") == proof and state.get("conflict") is False
                and isinstance(state.get("query"), dict) and state["query"].get("id") == proof.get("id")
                and binding == state.get("binding") and proof.get("receipt_version") == 1
                and proof.get("job_id") == state.get("command_id") and proof.get("training_run") == state.get("handle")
                and proof.get("leader_guid") == state.get("leader") and proof.get("guild_id") == state.get("cast_guild")
                and proof.get("human_guid") == state.get("human") and proof.get("map_id") == state.get("map")
                and proof.get("instance_id") == state.get("instance") and isinstance(state.get("members"), list)
                and 0 < len(state["members"]) <= 25
                and all(_positive(g) for g in state["members"]) and sorted(proof["members"]) == sorted(state["members"])
                and len(set(state["members"])) == len(state["members"])
                and receipt.get("provenance") == "complete_exact_native_scope_and_explicit_pull_receipts"
                and all(saved.get(k) == row[k] for k in ("week", "raid_key", "guild_id", "outcome", "kills", "wipes", "ts"))
                and all(saved.get(k) == state.get(k) for k in ("week", "raid_key", "guild_id", "members", "bosses"))
                and saved.get("members") == _json(row["members"]) and saved.get("bosses") == _json(row["bosses"])):
            comparisons = _frozen_comparisons(receipt, state, saved, cutoff)
            if comparisons is not None:
                status = "exact_scope_cleanup_receipt"
            else:
                comparisons = []
    result["rivals"].append(dict(week=week, guild_id=row["guild_id"], provenance=status,
        comparisons=comparisons, actual_dps_percentiles=None,
        percentile_reason="requested_pre_ai is not verified boss damage"))


def _frozen_comparisons(receipt, state, saved, cutoff):
    """Verify the producer's sealed result without consulting mutable pull SQL."""
    rows, baselines = receipt.get("admitted_pulls"), receipt.get("baselines")
    terminal = state.get("terminal")
    if ("terminal" not in state or terminal is not None and (
            not isinstance(terminal, dict) or len(terminal) > MAX_ROWS
            or type(terminal.get("state")) is not str or terminal["state"] not in {"done", "failed"}
            or type(terminal.get("reason")) is not str)):
        return None
    if (not isinstance(rows, list) or len(rows) > MAX_ROWS
            or not isinstance(baselines, dict) or len(baselines) > MAX_ROWS
            or not isinstance(receipt.get("comparisons"), list) or len(receipt["comparisons"]) > MAX_ROWS
            or not _time(saved.get("ts"), cutoff) or saved["ts"] * 1000 < state["proof"]["ts"]):
        return None
    try:
        from .benchmark import DPS_UNAVAILABLE, _comparisons, _result, _sealed_pull

        def valid(row, *, baseline=False):
            return (isinstance(row, dict)
                and all(type(row.get(k)) is int for k in ("map", "boss_id", "size", "difficulty", "start_ms", "end_ms"))
                and _time(row.get("received"), state["started"] if baseline else cutoff)
                and row["end_ms"] <= row["received"] * 1000
                and row.get("context") == f"{row['map']}:{row['boss_id']}:{row['difficulty']}"
                and _sealed_pull(row, state, baseline=baseline))

        if (not all(valid(r) for r in rows) or len({r["id"] for r in rows}) != len(rows)
                or set(baselines) != {str(r["boss_id"]) for r in rows}):
            return None
        for boss, sources in baselines.items():
            if (not isinstance(sources, list) or len(sources) > MAX_ROWS
                    or not all(valid(r, baseline=True) and str(r["boss_id"]) == boss for r in sources)
                    or len({r["id"] for r in sources}) != len(sources)
                    or sources != sorted(sources, key=lambda r: (r["start_ms"], r["id"]))):
                return None
        comparisons = _comparisons(state, rows, baselines)
        if (saved != _result(state, rows, saved["ts"]) or receipt["comparisons"] != comparisons
                or receipt.get("percentiles") != {} or receipt.get("percentile_reason") != DPS_UNAVAILABLE
                or type(receipt.get("stopped_at_frontier")) is not bool
                or receipt["stopped_at_frontier"] != (saved["outcome"] == "frontier")):
            return None
        return [item | dict(baseline="sealed_farm_receipts" if item["baseline_pulls"] else "unavailable",
                            causal_comparison=False) for item in comparisons]
    except (ImportError, AttributeError, KeyError, TypeError, ValueError, OverflowError):
        return None


def _meta(conn, key):
    row = conn.execute("SELECT CASE WHEN length(value)<=? THEN value END FROM meta WHERE key=?",
                       (MAX_RECEIPT_BYTES, key)).fetchone()
    return _json(row[0]) if row else None


def _invalid_constant(value):
    raise ValueError("nonfinite metadata")


def _mean_interval(values, seed):
    if not values:
        return None, None
    mean, low, high = bootstrap_diff([0], values, seed=seed)
    return mean, [low, high]


def training_section(conn, week: str, *, now: float, names: Callable[[int], str]) -> dict:
    """Report UTC ISO-week pulls against a disjoint first-28-receipt-day baseline.

    Pull time chooses the reporting week. Receipt time bounds the baseline and
    observation-age milestones: importing an old backlog cannot age the system.
    Comparisons use deaths per pull; kill durations are shown separately, since
    deaths falling does not itself establish improved damage or mechanic skill.
    Outcome observations are descriptive. Acceptance requires its own proof.
    """
    if type(week) is not str or not re.fullmatch(r"[0-9]{4}-W[0-9]{2}", week) or not _time(now, now):
        raise ValueError("invalid report interval")
    monday = date.fromisocalendar(int(week[:4]), int(week[6:]), 1)
    start = datetime.combine(monday, datetime.min.time(), timezone.utc).timestamp()
    end = start + timedelta(days=7).total_seconds()
    result = _blank()
    missing = _schema(conn)
    if missing:
        result["evidence"]["schema"] = "unavailable_or_incompatible: " + ", ".join(missing)
        return result  # Read-only schema7 inspection never initializes/migrates.
    cutoff = min(now, end)
    raw = conn.execute("SELECT * FROM tg_pull WHERE received<=? ORDER BY start_ms,id LIMIT ?",
                       (cutoff, MAX_PULLS+1)).fetchall()
    if len(raw) > MAX_PULLS:
        result["evidence"]["truncated"].append("pulls")
    valid = [dict(r) for r in raw[:MAX_PULLS] if _pull_valid(dict(r), cutoff)]
    rows = [r for r in _scored_rows(valid) if r["kind"] in {"his", "offline"}]
    groups = defaultdict(list)
    for row in rows:
        groups[(row["context"], row["score_basis"])].append(row)
    contexts = []
    for (context, basis), history in sorted(groups.items()):
        current = [r for r in history if start * 1000 <= r["start_ms"] < end * 1000]
        if not current:
            continue
        # Descriptive weekly totals use every eligible scored pull; champion
        # preference belongs to the score normalization, not observation counts.
        first = min(r["received"] for r in history)
        baseline_end = first + BASELINE_SECONDS
        baseline = [r for r in history if r["received"] < baseline_end]
        later = [r for r in current if r["received"] >= baseline_end]
        seed = f"{week}:{context}:{basis}"
        deaths, deaths_ci = _mean_interval([r["deaths"] for r in current], seed + ":deaths")
        kills = [r for r in current if r["outcome"] == "killed"]
        kill_s, kill_ci = _mean_interval([_duration(r) for r in kills], seed + ":kill_s")
        base_kills = [_duration(r) for r in baseline if r["outcome"] == "killed"]
        delta, delta_ci, change = None, None, "too few"
        if len(baseline) >= 5 and len(later) >= 5:
            delta, low, high = bootstrap_diff([r["deaths"] for r in baseline],
                [r["deaths"] for r in later], seed=seed + ":change")
            delta_ci = [low, high]
            change = "better" if high < 0 else "worse" if low > 0 else "no change"
        example = current[0]
        coverage = set()
        for row in current:
            reasons = _json(row["checks_coverage"])
            if isinstance(reasons, list) and len(reasons) <= MAX_ROWS and all(isinstance(reason, str) for reason in reasons):
                coverage.update(reasons)
            if not row["checks_complete"] and not reasons:
                coverage.add("coverage_unavailable")
        contexts.append(dict(context=context, score_basis=basis,
            checks_complete_pulls=sum(bool(r["checks_complete"]) for r in current),
            checks_coverage=sorted(coverage), boss=boss_name(example["map"], example["boss_id"]),
            size=example["size"], farm=is_farm([r["outcome"] for r in history if r["start_ms"] < end * 1000]),
            pulls=len(current), kills=len(kills), wipes=len(current) - len(kills),
            deaths_per_pull=deaths, deaths_per_pull_ci=deaths_ci, kill_s=kill_s, kill_s_ci=kill_ci,
            baseline_deaths=statistics.fmean(r["deaths"] for r in baseline) if baseline else None,
            baseline_kill_s=statistics.fmean(base_kills) if base_kills else None,
            baseline_pulls=len(baseline), comparison_pulls=len(later), baseline_complete=cutoff >= baseline_end,
            deaths_change=delta, deaths_change_ci=delta_ci, change=change))
    since = _meta(conn, "tg_first_pull")
    if type(since) not in (int, float) or not _finite(since) or since < 0 or since > now:
        since = None
    gate = _meta(conn, "tg_gate")
    if not isinstance(gate, dict):
        gate = None
    receipts = [r["received"] for r in rows]
    four_weeks = bool(since is not None and receipts and now - since >= BASELINE_SECONDS
                      and max(receipts) - min(receipts) >= BASELINE_SECONDS)
    gate_ok = bool(gate and gate.get("phase") == "passed"
                   and type(gate.get("verdict_ms")) in (int, float) and _finite(gate.get("verdict_ms"))
                   and gate["verdict_ms"] <= 1.0)
    result.update(since=since, gate=_safe(gate), contexts=contexts[:MAX_ROWS],
                  nothing_improved=sorted({r["boss"] for r in contexts if r["change"] == "no change"})[:MAX_ROWS])
    if len(contexts) > MAX_ROWS:
        result["evidence"]["truncated"].append("contexts")
    result["done_when"].update(four_weeks=four_weeks, gate_within_1ms=gate_ok and gate["verdict_ms"] >= 0)
    optional = (
        (_trials, (rows, cutoff), ("trials", "promotions")),
        (_cards_and_rotations, (cutoff,), ("rollbacks", "suspended", "rotations")),
        (_spread, (week, cutoff, names), ("spread", "mentees")),
        (_rivals, (week, cutoff), ("rivals",)),
    )
    for reader, args, sections in optional:
        try:
            reader(conn, result, *args)
        except (KeyError, TypeError, ValueError, OverflowError):
            # Old/corrupt optional provenance cannot crash unrelated history or
            # leave a partially accumulated positive completion claim behind.
            for key in sections:
                result[key] = []
            result["evidence"].setdefault("unavailable_sections", []).extend(sections)
    voice = dict(briefing_proposals=0, callout_send_receipts=0, refused_callouts=0,
                 audible_delivery="unverified", fact_delivery="unverified", cast_coverage="unverified")
    for _, record in _records(conn, "tg_voice:"):
        events = record.get("events") if isinstance(record, dict) else None
        for event in events[:MAX_ROWS] if isinstance(events, list) else ():
            if isinstance(event, dict) and _time(event.get("ts"), cutoff) and event["ts"] >= start:
                voice["briefing_proposals"] += event.get("kind") == "briefing"
                voice["callout_send_receipts"] += event.get("kind") == "callout" and event.get("status") == "sent"
                voice["refused_callouts"] += event.get("kind") == "callout" and event.get("status") == "refused"
    result["evidence"]["voice"] = voice
    result["evidence"]["report_cutoff"] = cutoff
    if result["evidence"]["truncated"] or result["evidence"].get("unavailable_sections"):
        result["done_when"] = dict.fromkeys(_MILESTONES, False)
    return _safe(result)
