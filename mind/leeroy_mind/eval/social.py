"""Read-only owner diagnostics. Rumour and confidence contents never enter leak reports."""
import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import datetime

from leeroy_mind.nightly import knowledge
from leeroy_mind.nightly.attraction import GAME_WORDS
from leeroy_mind.nightly.audience import audience_of, cast_names
from leeroy_mind.nightly.evidence import content_words
from leeroy_mind.nightly.critic import _safe
from leeroy_mind.nightly.feuds import open_feuds
from leeroy_mind.nightly.mood import evidence_scope, octant
from leeroy_mind.nightly.reactions import ANSWERED
from leeroy_mind.nightly.romance import jealousy_rows
from leeroy_mind.nightly.settings import night_of_ts
from leeroy_mind.nightly.wowlife import band, get_wow_life
from leeroy_mind.personas.generate import mix_report
from leeroy_mind.personas.io import load_persona
from leeroy_mind.personas.stances import STANCE_SHARES, is_woman
from leeroy_mind.storage.rows import ROMANCE_STAGES
from leeroy_mind.talk.filters import find_tells
from leeroy_mind.text import strip_wow_markup

REBALANCE_START = {"lust": 13, "drawn": 19, "fond": 26, "none": 6}


def _ratio(n, d):
    return n / d if d else None


def _rows(store, sql, params=()):
    return [dict(row) for row in store.conn.execute(sql, params)]


def _sent(store, bounds):
    cast = cast_names(store)
    return [r for r in store.conn.execute("SELECT * FROM utterance WHERE dropped IS NULL "
        "AND ts>=? AND ts<? ORDER BY ts,id", bounds) if r["guid"] in cast]


def _nights(store, bounds):
    tz = store.clock.local().tzinfo
    return night_of_ts(bounds[0], tz), night_of_ts(math.nextafter(bounds[1], -math.inf), tz)


def _json(raw, default):
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return default


def texture(store, bounds, *, human, speed):
    ties = _rows(store, "SELECT * FROM relationship WHERE a!=? AND a!=b", (human,))
    deltas = _rows(store, "SELECT * FROM rel_delta WHERE ts>=? AND ts<?", bounds)
    first, last = _nights(store, bounds)
    caps = _rows(store, "SELECT * FROM rel_cap WHERE night>=? AND night<=?", (first, last))
    return {"pairs": len(ties), "non_neutral_share": _ratio(sum(abs(r["affinity"]) >= 15 for r in ties), len(ties)),
        "negative_ties": sum(r["affinity"] <= -15 for r in ties),
        "romance_moves": sum(r["dim"] == "romance_stage" for r in deltas),
        "unexplained_deltas": sum(not _json(r["evidence"], []) for r in deltas),
        "cap_breaches": sum(abs(r["used"]) > speed + 1e-9 for r in caps)}


def callbacks(store, bounds):
    rows, count, invalid = _sent(store, bounds), 0, 0
    for row in rows:
        ids = _json(row["memory_ids"], None)
        if not isinstance(ids, list) or any(type(i) is not int or i <= 0 for i in ids):
            invalid += 1
            continue
        memories = store.get_memories(ids)
        if len(memories) != len(set(ids)):
            invalid += 1
        if any(m.t_event < row["ts"] - 7 * 86400 for m in memories):
            count += 1
    return {"lines": len(rows), "callbacks": count, "share": _ratio(count, len(rows)),
            "incomplete_memory_refs": invalid}


def _online_hours(store, bounds):
    intervals = []
    for row in store.conn.execute("SELECT started,ended FROM session WHERE started<? "
                                   "AND (ended IS NULL OR ended>?)", (bounds[1], bounds[0])):
        start = max(bounds[0], row["started"])
        end = min(bounds[1], row["ended"] if row["ended"] is not None else store.clock.now())
        if end > start:
            intervals.append((start, end))
    merged = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return sum(end - start for start, end in merged) / 3600


def noise(store, bounds, *, chattiness, names, ident=None):
    rows = _sent(store, bounds)
    hours = _online_hours(store, bounds)
    phrases, fatal, soft = defaultdict(set), 0, 0
    tz = store.clock.local().tzinfo
    for row in rows:
        # Screen before lowercasing: a legacy proper name must not become an ordinary
        # lower-case word before the established name matcher can recognise it.
        text = _safe(row["text"], ident) if ident is not None else strip_wow_markup(row["text"])
        words = re.findall(r"[\w']+", text.casefold())
        day = datetime.fromtimestamp(row["ts"], tz).date().isoformat()
        for n in range(len(words) - 3):
            phrases[row["guid"], " ".join(words[n:n + 4])].add(day)
        tells = find_tells(text, speaker=names.get(row["guid"], ""))
        fatal += any(t.fatal for t in tells)
        soft += bool(tells) and all(t.soft for t in tells)
    return {"lines": len(rows), "online_hours": hours,
        "lines_per_online_hour": _ratio(len(rows), hours), "target_per_hour": 25 * chattiness / 4,
        "repeated_phrases": [[g, p, len(days)] for (g, p), days in sorted(phrases.items()) if len(days) >= 3],
        "assistant_speak": fatal, "soft_tells": soft}


def cast_mix(ctx, *, start=REBALANCE_START):
    people = [p for g in sorted(cast_names(ctx.store)) if (p := load_persona(ctx.store, g)) is not None]
    report = asdict(mix_report(people))
    women = [p for p in people if is_woman(p)]
    attitudes = Counter(p.romance.stance for p in women)
    stages = {stance: {stage: 0 for stage in ROMANCE_STAGES} for stance in STANCE_SHARES if stance != "none"}
    for p in women:
        if p.romance.stance in stages:
            stage = ctx.store.get_relationship(p.guid, ctx.human).romance_stage
            stages[p.romance.stance][stage] = stages[p.romance.stance].get(stage, 0) + 1
    wow = Counter(band(s.pressure, s.guilt) for p in people for s in [get_wow_life(ctx.store, p.guid)])
    return {**report, "female_target": ctx.settings.female_share,
        "attitudes": {stance: _ratio(attitudes[stance], len(women)) for stance in STANCE_SHARES},
        "attitude_counts": dict(attitudes), "attitude_target": dict(STANCE_SHARES), "rebalance_start": dict(start),
        "men_with_stance": [p.guid for p in people if not is_woman(p) and p.romance.stance != "none"],
        "stages": stages, "away": [r[0] for r in ctx.store.conn.execute("SELECT guid FROM away "
            "WHERE returned_at IS NULL AND left_at<=? ORDER BY guid", (ctx.clock.now(),))],
        "wow_bands": dict(wow), "dials": {"romance_dial": ctx.settings.romance_dial,
            "jealousy": ctx.settings.jealousy, "wow_vs_life": ctx.settings.wow_vs_life}}


def _bids(store, bounds):
    weekly = defaultdict(lambda: [0, 0])
    for row in store.conn.execute("SELECT value FROM meta WHERE key LIKE 'nightly:human-world:reaction:%'"):
        record = _json(row[0], {})
        if (not bounds[0] <= record.get("ts", -math.inf) < bounds[1] or not record.get("arm")
                or record.get("reaction") == "none" or store.get_meta(
                    f"nightly:human-world:bandit:{record.get('line_id')}") is None):
            continue
        counts = weekly[record["speaker"], record["arm"]]
        counts[0] += 1
        counts[1] += record["reaction"] in ANSWERED
    posteriors = defaultdict(lambda: defaultdict(lambda: [0, 0, 0]))
    for row in store.conn.execute("SELECT * FROM bandit WHERE n>0"):
        counts = posteriors[row["guid"]][row["arm"]]
        for i, field in enumerate(("alpha", "beta", "n")):
            counts[i] += row[field]
    tops = []
    for guid, arms in posteriors.items():
        weights = {arm: (1 + a) / (2 + a + b) for arm, (a, b, _) in arms.items()}
        largest = max(weights.values())
        tops.append("flattery" in weights and weights["flattery"] == largest)
    return {"members": {g: {arm: {"n": n, "answered": yes, "answer_rate": _ratio(yes, n)}
        for (owner, arm), (n, yes) in sorted(weekly.items()) if owner == g}
        for g in sorted({owner for owner, _ in weekly})},
        "weekly_receipted_bids": sum(n for n, _ in weekly.values()),
        "flattery_share": _ratio(sum(tops), len(tops)), "flattery_members": len(tops),
        "flattery_basis": "current unclipped posterior mean; sampled weights are not stored"}


def _language(store, bounds):
    terms = _rows(store, "SELECT * FROM lexicon ORDER BY id")
    holders = defaultdict(list)
    cast = cast_names(store)
    for row in store.conn.execute("SELECT term,guid FROM lexicon_adopter WHERE active=1 ORDER BY term,guid"):
        if row["guid"] in cast:
            holders[row["term"]].append(row["guid"])
    active = [dict(id=r["id"], kind=r["kind"], term=r["term"], uses=r["uses"], adopters=holders[r["id"]])
              for r in terms if r["status"] == "active"]
    uses = Counter()
    for row in store.conn.execute("SELECT key,value FROM meta WHERE key LIKE 'nightly:lexicon-line:%'"):
        line = store.conn.execute("SELECT ts,from_guid FROM chat_line WHERE id=?", (row["key"].rsplit(":", 1)[1],)).fetchone()
        if line is None or line["from_guid"] not in cast or not bounds[0] <= line["ts"] < bounds[1]:
            continue
        for term in set(_json(row["value"], [])):
            uses[line["from_guid"], term, night_of_ts(line["ts"], store.clock.local().tzinfo)] += 1
    holding = {g for r in active for g in r["adopters"]}
    return {"active": active, "uses_basis": "current lifetime term counters",
        "cast_with_term": len(holding), "cast": len(cast), "adopter_share": _ratio(len(holding), len(cast)),
        "dormant": [r["id"] for r in terms if r["status"] == "dormant"],
        "revived": None, "revival_history": "status transitions were not recorded",
        "vetoed": [r["id"] for r in terms if r["status"] == "vetoed"],
        "overuse": [{"guid": g, "term": term, "night": night, "uses": n}
                    for (g, term, night), n in sorted(uses.items()) if n > 5]}


def signals(store, bounds, *, human):
    first, last = _nights(store, bounds)
    women = {g for g in cast_names(store) if (p := load_persona(store, g)) is not None and is_woman(p)}
    attraction = {}
    for guid in sorted(women):
        acts = _rows(store, "SELECT act,gain,mult FROM attraction_act WHERE guid=? AND ts>=? AND ts<?", (guid, *bounds))
        nights = _rows(store, "SELECT * FROM attraction_log WHERE guid=? AND night>=? AND night<=?", (guid, first, last))
        grouped = defaultdict(lambda: {"n": 0, "gain": 0, "effective_gain": 0})
        for act in acts:
            grouped[act["act"]]["n"] += 1
            grouped[act["act"]]["gain"] += act["gain"]
            grouped[act["act"]]["effective_gain"] += act["gain"] * act["mult"]
        changes = [{"night": r["night"], "from": r["stance_before"], "to": r["stance_after"],
                    "reason": "decay" if r["decayed"] and not r["gained"] else "act"}
                   for r in nights if r["stance_before"] != r["stance_after"]]
        for row in store.conn.execute("SELECT ts,text FROM persona_ledger WHERE guid=? AND kind='stance_band' AND ts>=? AND ts<?", (guid, *bounds)):
            if "hand move" in row["text"] and " -> " in row["text"]:
                before, after = row["text"].split(":", 1)[0].split(" -> ", 1)
                if before in STANCE_SHARES and after in STANCE_SHARES:
                    changes.append({"ts": row["ts"], "from": before, "to": after, "reason": "hand move"})
        attraction[guid] = {"acts": dict(grouped), "decay": sum(r["decayed"] for r in nights),
            "neglected_nights": sum(bool(r["neglected"]) for r in nights),
            "hurt_nights": sum(bool(r["hurt"]) for r in nights), "stance_changes": changes}
    lands = [dict(r, rate=(r["laughs"] + r["replies"]) / r["n"]) for r in
             _rows(store, "SELECT * FROM lands WHERE n>=5 AND kind!='*'")]
    lands.sort(key=lambda r: (r["rate"], r["guid"], r["kind"]))
    latest = store.conn.execute("SELECT * FROM lands_profile WHERE night<=? ORDER BY night DESC LIMIT 1", (last,)).fetchone()
    rumours = []
    for row in store.conn.execute("SELECT id,status FROM rumor ORDER BY id"):
        ops = Counter(r[0] for r in store.conn.execute("SELECT op FROM rumor_version WHERE rumor=? AND ts<?", (row["id"], bounds[1])))
        rumours.append({"id": row["id"], "status": row["status"], "versions": sum(ops.values()),
                        "ops": dict(ops), "corrected": bool(ops["correct"])})
    disclosure = _rows(store, "SELECT guid,other,depth,response FROM disclosure WHERE ts>=? AND ts<?", bounds)
    moods = {r["guid"]: octant(r["p"], r["a"], r["d"]) for r in store.conn.execute("SELECT * FROM pad_mood")}
    critic = _rows(store, "SELECT guid,sum(n*mean)/sum(n) AS mean,sum(n) AS lines,count(*) AS nights "
        "FROM critic_score WHERE night>=? AND night<=? GROUP BY guid ORDER BY guid", (first, last))
    flags = []
    for row in store.conn.execute("SELECT id,guid,ts,ref FROM persona_ledger WHERE kind='drift_flag' ORDER BY id"):
        night = store.get_meta(f"nightly:critic-flag-night:{row['id']}") or night_of_ts(row["ts"], store.clock.local().tzinfo)
        if first <= night <= last:
            flags.append(dict(row))
    return {"attraction": attraction, "lands": {"top": lands[-5:][::-1], "bottom": lands[:5],
            "profile": dict(latest) if latest is not None else None, "basis": "current cumulative counts"},
        "bids": _bids(store, bounds), "jealousy_top": sorted(jealousy_rows(store), key=lambda r: (-r[3], r[0], r[1]))[:5],
        "language": _language(store, bounds), "rumours": rumours,
        "feuds": [{"id": r["id"], "a": r["a"], "b": r["b"], "sides": _json(r["sides"], {})} for r in open_feuds(store)],
        "mentors": _rows(store, "SELECT id,mentor,mentee,started,ends FROM mentor WHERE ended IS NULL AND started<=? AND ends>? ORDER BY id", (store.clock.now(), store.clock.now())),
        "stories": {"volitions": dict(store.conn.execute("SELECT kind,count(*) FROM volition WHERE chosen=1 AND night>=? AND night<=? GROUP BY kind", (first, last))),
            "blowups": store.conn.execute("SELECT count(*) FROM encounter WHERE blowup=1 AND night>=? AND night<=?", (first, last)).fetchone()[0],
            "open_arcs": _rows(store, "SELECT id,kind,tension FROM story_arc WHERE status='open' ORDER BY id")},
        "disclosure": {"to_human": dict(Counter(f"{r['depth']}:{r['response']}" for r in disclosure if r["other"] == human)),
                       "between_members": dict(Counter(r["depth"] for r in disclosure if r["other"] != human and r["guid"] != human))},
        "moods": {"members": moods, "guild": [dict(r, reasons=_json(r["reasons"], [])) for r in
            _rows(store, "SELECT * FROM guild_mood WHERE night>=? AND night<=? ORDER BY night", (first, last))]},
        "motives": _rows(store, "SELECT guid,motive,weight,deficit FROM motive WHERE weight>0 ORDER BY guid,motive"),
        "critic": {"members": critic, "flags": flags}}


def _echo(store, utterance):
    return store.conn.execute("SELECT * FROM chat_line WHERE origin='mind' AND from_guid=? AND channel=? "
        "AND to_guid=? AND text=? AND ts BETWEEN ? AND ? ORDER BY abs(ts-?),id LIMIT 1",
        (utterance["guid"], utterance["channel"], utterance["to_guid"], utterance["text"],
         utterance["ts"] - 2, utterance["ts"] + 2, utterance["ts"])).fetchone()


def leaks(store, bounds, *, names=(), model=None):
    """Count distinct emitted lines, never expose matched secret text or word fragments."""
    roster = store.cast()
    names = [*names, *(m.name for m in roster.values()), store.get_meta("human_name") or ""]
    rows = _sent(store, bounds)
    lines = Counter(r["guid"] for r in rows)
    secret, omniscient, private = defaultdict(set), defaultdict(set), defaultdict(set)
    unassessed = set()
    facts = [r for r in store.conn.execute("SELECT * FROM fact WHERE secret=1 AND kind!='real_name'")
             if len(content_words(r["text"], names) - GAME_WORDS) >= 2]
    held = defaultdict(list)
    for guid in lines:
        ids = [r[0] for r in store.conn.execute("SELECT id FROM memory WHERE owner=? AND status!='rejected'", (guid,))]
        for memory in store.get_memories(ids):
            scope = evidence_scope(store, guid, [f"memory:{memory.id}"])
            words = content_words(memory.text, names) - GAME_WORDS
            if scope is not None and scope[0] != "open" and len(words) >= 2:
                held[guid].append((memory.t_created, scope[1], words))
    for row in rows:
        guid, ts = row["guid"], row["ts"]
        words = content_words(strip_wow_markup(row["text"]), names) - GAME_WORDS
        echo = _echo(store, row)
        audience = audience_of(store, echo["id"]) if echo is not None else None
        for fact in facts:
            if fact["created"] > ts or len(words & (content_words(fact["text"], names) - GAME_WORDS)) < 2:
                continue
            knowers = {r[0] for r in store.conn.execute("SELECT guid FROM knows WHERE root=? AND learned<=?", (fact["root"], ts))}
            knowers |= set(knowledge.parties(store, fact["root"]))
            if guid not in knowers:
                omniscient[guid].add(row["id"])
            elif not knowledge.may_tell(store, guid, fact["root"]):
                if audience is None:
                    unassessed.add(row["id"])
                elif any(other not in knowers for other in audience):
                    secret[guid].add(row["id"])
        for created, partner, source_words in held[guid]:
            if (created <= ts and len(words & source_words) >= 2
                    and not (row["channel"] == "whisper" and partner and row["to_guid"] == partner)):
                private[guid].add(row["id"])
    latest = store.conn.execute("SELECT ts,n,rate FROM probe_run ORDER BY ts DESC,id DESC LIMIT 1").fetchone()
    return {"lines": dict(lines), "secret_leaks": {g: len(secret[g]) for g in lines},
        "omniscience": {g: len(omniscient[g]) for g in lines}, "private_leaks": {g: len(private[g]) for g in lines},
        "secret_score": {g: -10 * len(secret[g]) / n for g, n in lines.items()},
        "secret_line_ids": {g: sorted(ids) for g, ids in secret.items() if ids},
        "omniscience_line_ids": {g: sorted(ids) for g, ids in omniscient.items() if ids},
        "private_line_ids": {g: sorted(ids) for g, ids in private.items() if ids},
        "unassessed_audience_lines": len(unassessed), "scope_leaks": len(knowledge.scope_leaks(store)),
        "probe": {**(dict(latest) if latest is not None else {"ts": None, "n": 0, "rate": None}),
                  "gate_open": knowledge.secret_probe_passed(store, now=store.clock.now(), model=model)}}
