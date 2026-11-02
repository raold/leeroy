"""Resumable, owner-only persona measurements with explicit sampling coverage."""
import json
import math
from collections import defaultdict
from itertools import combinations

from leeroy_mind.nightly.critic import _line, _members, _safe, sample_lines
from leeroy_mind.nightly.growth import rollback_week
from leeroy_mind.nightly.llm import check_schema, schema, system_prompt
from leeroy_mind.nightly.settings import iso_week, night_of_ts
from leeroy_mind.personas.card import render_prompt_card
from leeroy_mind.personas.io import load_persona
from leeroy_mind.storage.rows import cosine, unpack_vec
from leeroy_mind.wow import CLASSES, RACES

CONSISTENCY_SCHEMA = schema("consistency", {"scores": {"type": "array", "items": {
    "type": "object", "properties": {"i": {"type": "integer"},
    "score": {"type": "integer", "minimum": 1, "maximum": 5}},
    "required": ["i", "score"], "additionalProperties": False}}})
WHO_SCHEMA = schema("who_said", {"pick": {"type": "integer", "minimum": 1, "maximum": 3}})


def _bounds(bounds):
    if len(bounds) != 2 or not all(math.isfinite(x) for x in bounds) or bounds[0] >= bounds[1]:
        raise ValueError("Evaluation bounds must be a finite nonempty interval")
    return list(bounds)


def _card(store, guid):
    if guid not in _members(store):
        return None
    member = store.cast()[guid]
    persona = load_persona(store, guid)
    if persona is None:
        return None
    card = render_prompt_card(persona, race=RACES.get(member.race, ""),
                              class_name=CLASSES.get(member.class_, ""))
    return (store.get_persona_json(guid), store.persona_versions()[guid], member.name,
            member.race, member.class_, member.level), card


def _current(ctx, ids, cards, bounds):
    """Bypass context caches after awaits; private dialogue stays in the owner's audit."""
    rows, snapshots, rendered = [], [], {}
    try:
        for guid in sorted(set(cards)):
            value = _card(ctx.store, guid)
            if value is None:
                return None
            snapshots.append((guid, value))
            rendered[guid] = value[1]
        for ident in ids:
            row = ctx.store.conn.execute("SELECT * FROM utterance WHERE id=?", (ident,)).fetchone()
            if (row is None or row["guid"] not in rendered or row["dropped"] is not None
                    or not bounds[0] <= row["ts"] < bounds[1]):
                return None
            snapshots.append(_line(ctx.store, row))
            rows.append(row)
    except (ValueError, TypeError, KeyError):
        return None
    return tuple(snapshots), rows, rendered


def _freeze(store, key, bounds, make):
    with store.transaction():
        raw = store.get_meta(key)
        if raw is not None:
            plan = json.loads(raw)
            if plan["bounds"] != _bounds(bounds):
                raise ValueError("Evaluation window changed on resume")
            return plan
        plan = {"bounds": _bounds(bounds), **make()}
        store.set_meta(key, json.dumps(plan))
        return plan


def _receipt(store, key):
    raw = store.get_meta(key)
    return None if raw is None else json.loads(raw)


def _vectors(packed, expected):
    if len(packed) != expected:
        raise ValueError("Evaluation embedding batch is incomplete")
    result = [unpack_vec(value) for value in packed]
    if (any(not v or not all(math.isfinite(x) for x in v) or not any(v) for v in result)
            or len({len(v) for v in result}) > 1):
        raise ValueError("Evaluation embeddings are invalid")
    return result


def consistency_coverage(store, week):
    return _receipt(store, f"eval:consistency-coverage:{week}") or {
        "sampled_lines": 0, "scored_lines": 0, "sampled_members": 0, "scored_members": 0}


async def consistency(ctx, bounds, rng, week, *, lines=100):
    def choose():
        picked = sample_lines(ctx.store, bounds, rng, per_member=10, total=max(0, lines))
        return {"members": {str(g): [r["id"] for r in rs] for g, rs in picked.items() if len(rs) >= 3}}

    key = f"eval:consistency-plan:{week}"
    plan = _freeze(ctx.store, key, bounds, choose)
    members = {int(g): ids for g, ids in plan["members"].items()}
    entries = [(g, ident) for g, ids in sorted(members.items()) for ident in ids]
    batches = [entries[i:i + 10] for i in range(0, len(entries), 10)]

    async def batch(unit):
        n = int(unit.rsplit(":", 1)[1])
        result_key = f"eval:consistency-batch:{week}:{n}"
        if _receipt(ctx.store, result_key) is not None:
            return
        selected = batches[n]
        ids, guids = [i for _, i in selected], [g for g, _ in selected]
        state = _current(ctx, ids, guids, bounds)
        if state is None:
            return  # unavailable input is not a score or a completed batch
        before, rows, cards = state
        system = system_prompt("consistency", "Score each numbered line against its speaker's card: "
            "5 is exactly them; 1 is something they would never say. Score every index once.\n{cards}",
            ctx.settings.data_dir, human=ctx.ident,
            cards="\n\n".join(f"Speaker {g}\n{_safe(card, ctx.ident)}" for g, card in cards.items()))
        user = "\n".join(f"{i} | speaker {r['guid']}: {json.dumps(_safe(r['text'], ctx.ident))}"
                         for i, r in enumerate(rows))
        answer = await ctx.reasoner.json(system, user, CONSISTENCY_SCHEMA)
        check_schema(CONSISTENCY_SCHEMA, answer)
        scores = answer["scores"]
        if len(scores) != len(rows) or sorted(s["i"] for s in scores) != list(range(len(rows))):
            raise ValueError("Consistency must score each displayed index exactly once")
        scores = sorted(scores, key=lambda score: score["i"])

        def write(store):
            fresh = _current(ctx, ids, guids, bounds)
            if fresh is None or fresh[0] != before:
                raise ValueError("Consistency sources changed during scoring")
            if store.get_meta(result_key) is None:
                store.set_meta(result_key, json.dumps([{ "id": r["id"], "guid": r["guid"], "score": s["score"]}
                    for r, s in zip(rows, scores)]))
        ctx.commit("eval", unit, write)

    await ctx.map_units("eval", [f"consistency:{week}:{i}" for i in range(len(batches))], batch)
    found = {}
    for n in range(len(batches)):
        for row in _receipt(ctx.store, f"eval:consistency-batch:{week}:{n}") or []:
            found[row["id"]] = row
    scores = {g: sum(found[i]["score"] for i in ids) / len(ids) / 5 for g, ids in members.items()
              if len(ids) >= 3 and all(i in found and found[i]["guid"] == g for i in ids)}
    with ctx.store.transaction():
        for guid, value in scores.items():
            ctx.store.conn.execute("INSERT INTO eval_score(week,guid,metric,value) VALUES (?,?,'consistency',?) "
                "ON CONFLICT(week,guid,metric) DO UPDATE SET value=excluded.value", (week, guid, value))
        ctx.store.set_meta(f"eval:consistency-coverage:{week}", json.dumps({
            "sampled_lines": len(entries), "scored_lines": len(found),
            "sampled_members": len(members), "scored_members": len(scores)}))
    return scores


async def distinctiveness(ctx, bounds, rng, *, trials=60):
    week = iso_week(night_of_ts(bounds[0], ctx.tz))

    def choose():
        members = sorted(_members(ctx.store))
        rows = [r for r in ctx.store.conn.execute("SELECT id,guid FROM utterance WHERE dropped IS NULL "
            "AND ts>=? AND ts<? ORDER BY ts,id", bounds) if r["guid"] in members]
        selected = rng.sample(rows, min(max(0, trials), len(rows))) if len(members) >= 3 else []
        chosen = []
        for row in selected:
            options = [row["guid"], *rng.sample([g for g in members if g != row["guid"]], 2)]
            rng.shuffle(options)
            chosen.append({"id": row["id"], "guid": row["guid"], "options": options})
        return {"trials": chosen, "requested": max(0, trials)}

    plan = _freeze(ctx.store, f"eval:distinctiveness-plan:{week}", bounds, choose)

    async def trial(unit):
        n = int(unit.rsplit(":", 1)[1])
        key = f"eval:distinctiveness-trial:{week}:{n}"
        if _receipt(ctx.store, key) is not None:
            return
        item = plan["trials"][n]
        state = _current(ctx, [item["id"]], item["options"], bounds)
        if state is None:
            return
        before, rows, cards = state
        system = system_prompt("who_said", "Which of these three people most likely said the line? "
            "Choose its numbered card.\n{cards}", ctx.settings.data_dir, human=ctx.ident,
            cards="\n\n".join(f"{i + 1}. {_safe(cards[g], ctx.ident)}" for i, g in enumerate(item["options"])))
        answer = await ctx.reasoner.json(system, json.dumps(_safe(rows[0]["text"], ctx.ident)), WHO_SCHEMA)
        check_schema(WHO_SCHEMA, answer)

        def write(store):
            fresh = _current(ctx, [item["id"]], item["options"], bounds)
            if fresh is None or fresh[0] != before:
                raise ValueError("Distinctiveness sources changed during scoring")
            if store.get_meta(key) is None:
                store.set_meta(key, json.dumps({"correct": item["options"][answer["pick"] - 1] == item["guid"]}))
        ctx.commit("eval", unit, write)

    await ctx.map_units("eval", [f"distinctiveness:{week}:{i}" for i in range(len(plan["trials"]))], trial)
    cos_key = f"eval:distinctiveness-cosine:{week}"
    metrics = _receipt(ctx.store, cos_key)
    if metrics is None and plan["trials"]:
        ids = [item["id"] for item in plan["trials"]]
        guids = [item["guid"] for item in plan["trials"]]
        state = _current(ctx, ids, guids, bounds)
        if state is not None:
            before, rows, _ = state
            vectors = _vectors(await ctx.embed([_safe(r["text"], ctx.ident) for r in rows]), len(rows))
            within, cross = [], []
            for a, b in combinations(range(len(rows)), 2):
                (within if rows[a]["guid"] == rows[b]["guid"] else cross).append(cosine(vectors[a], vectors[b]))
            measured = {"cross_member_cosine": sum(cross) / len(cross) if cross else None,
                "within_member_cosine": sum(within) / len(within) if within else None,
                "cross_member_pairs": len(cross), "within_member_pairs": len(within)}
            with ctx.store.transaction():
                fresh = _current(ctx, ids, guids, bounds)
                if fresh is None or fresh[0] != before:
                    raise ValueError("Distinctiveness sources changed during embedding")
                ctx.store.set_meta(cos_key, json.dumps(measured))
                metrics = measured
    results = [_receipt(ctx.store, f"eval:distinctiveness-trial:{week}:{i}") for i in range(len(plan["trials"]))]
    complete = [r for r in results if r is not None]
    return {"who_said_accuracy": sum(r["correct"] for r in complete) / len(complete) if complete else None,
        "trials_requested": plan["requested"], "trials_sampled": len(results), "trials_scored": len(complete),
        **(metrics or {"cross_member_cosine": None, "within_member_cosine": None,
                      "cross_member_pairs": 0, "within_member_pairs": 0})}


def drift_check(store, week, prev_week, *, since, now):
    if not all(math.isfinite(x) for x in (since, now)) or since > now:
        raise ValueError("Invalid drift rollback interval")
    key = f"eval:drift:{week}"
    with store.transaction():
        prior = _receipt(store, key)
        if prior is not None:
            return prior
        previous = dict(store.conn.execute("SELECT guid,value FROM eval_score WHERE week=? AND metric='consistency'", (prev_week,)))
        affected = []
        for row in store.conn.execute("SELECT guid,value FROM eval_score WHERE week=? AND metric='consistency' ORDER BY guid", (week,)):
            guid, value = row
            if guid not in _members(store) or value is None or not math.isfinite(value) or not 0 <= value <= 1:
                continue
            old = previous.get(guid)
            fell = old is not None and math.isfinite(old) and old - value > 0.15 + 1e-9
            if value < 0.6 or fell:
                rollback_week(store, guid, since, now)
                affected.append(guid)
        store.set_meta(key, json.dumps(affected))
        return affected
