"""Deterministic pleasure/arousal/dominance moods and private targeted feelings."""
import json
import logging
import math
from collections import Counter, defaultdict
from dataclasses import replace
from datetime import date

from leeroy_mind.logs import log
from leeroy_mind.memory.privacy import stricter
from leeroy_mind.memory.writer import clean_memory_text
from leeroy_mind.nightly import knowledge
from leeroy_mind.nightly.attraction import FINAL_BOSSES, reflection_row
from leeroy_mind.nightly.forget import _node as _provenance_node
from leeroy_mind.nightly.motives import trait_today
from leeroy_mind.nightly.settings import night_of_ts
from leeroy_mind.wow import boss_name, is_raid_fight

STAGE = "mood"
OCC_PAD = {
    "joy": (0.4, 0.2, 0.1), "distress": (-0.4, -0.2, -0.5),
    "pride": (0.4, 0.3, 0.3), "shame": (-0.3, 0.1, -0.6),
    "admiration": (0.5, 0.3, -0.2), "reproach": (-0.3, -0.1, 0.4),
    "gratitude": (0.4, 0.2, -0.3), "anger": (-0.51, 0.59, 0.25),
    "hope": (0.2, 0.2, -0.1), "fear": (-0.64, 0.6, -0.43),
    "happy_for": (0.4, 0.2, 0.2), "resentment": (-0.2, -0.3, -0.2),
    "gloating": (0.3, -0.3, -0.1), "pity": (-0.4, -0.2, -0.5),
}
OCTANTS = {"+P+A+D": "exuberant", "-P-A-D": "bored", "+P+A-D": "dependent",
           "-P-A+D": "disdainful", "+P-A+D": "relaxed", "-P+A-D": "anxious",
           "+P-A-D": "docile", "-P+A+D": "hostile"}
MOOD_HALF_LIFE_NIGHTS = 2.0
PULL = 0.1
MOOD_CAP = 0.3
GUILD_MOOD_CARRY = 0.5
MOOD_WEIGHT = 0.5
CONTAGION = 0.2
HARD_KILL_WIPES = 6
EMOTION_LINES = {
    "joy": "Glad about {target}.", "distress": "Upset about {target}.",
    "pride": "Proud of {target}.", "shame": "Embarrassed in front of {target}.",
    "admiration": "Impressed by {target}.", "reproach": "{target} let me down.",
    "gratitude": "Owe {target} one.", "anger": "Still angry at {target}.",
    "hope": "Rooting for {target}.", "fear": "Wary of {target}.",
    "happy_for": "Happy for {target}.", "resentment": "A bit sore about {target}.",
    "gloating": "Bit smug about {target}.", "pity": "Feel bad for {target}.",
}


def _clamp(value, lo=-1.0, hi=1.0):
    return max(lo, min(hi, value))


def baseline(p):
    """Mehrabian temperament: emotional stability is the negative of neuroticism."""
    z = {name: (getattr(p.big_five, name).score - 5.5) / 4.5 for name in
         ("openness", "conscientiousness", "extraversion", "agreeableness", "neuroticism")}
    o, c, e, a, s = (z["openness"], z["conscientiousness"], z["extraversion"],
                      z["agreeableness"], -z["neuroticism"])
    return tuple(_clamp(v) for v in (0.21 * e + 0.59 * a + 0.19 * s,
        0.15 * o + 0.30 * a - 0.57 * s, 0.25 * o + 0.17 * c + 0.60 * e - 0.32 * a))


def octant(p, a, d):
    """Zero belongs to the positive half of an axis."""
    return OCTANTS["".join(("+" if value >= 0 else "-") + axis
                           for axis, value in zip("PAD", (p, a, d)))]


def step_mood(mood, base, emotions, *, nights):
    """Decay, then take one bounded emotional pull/push in PAD space.

    The affective step is capped after the specified exponential decay; normal decay
    across many absent nights can naturally move farther than one emotional step.
    """
    factor = 2 ** (-max(0, nights) / MOOD_HALF_LIFE_NIGHTS)
    point = tuple(b + (m - b) * factor for m, b in zip(mood, base))
    known = [(OCC_PAD[kind], intensity) for kind, intensity in emotions
             if kind in OCC_PAD and intensity > 0]
    total = sum(intensity for _, intensity in known)
    if not total:
        return tuple(_clamp(v) for v in point)
    centre = tuple(sum(p[axis] * weight for p, weight in known) / total for axis in range(3))
    push = octant(*point) == octant(*centre) and math.dist(point, (0, 0, 0)) >= math.dist(centre, (0, 0, 0))
    direction = centre if push else tuple(c - m for c, m in zip(centre, point))
    length = math.dist(direction, (0, 0, 0))
    distance = min(MOOD_CAP, PULL * total)
    return tuple(_clamp(m + (_clamp(v / length * distance, -MOOD_CAP, MOOD_CAP) if length else 0))
                 for m, v in zip(point, direction))


def night_mood(events):
    """Raid-fight outcomes in chronological order; one nightly hard/final-kill bonus."""
    rows = [dict(row) for row in events]
    rows = sorted(enumerate(rows), key=lambda pair: (pair[1].get("ts", pair[0]), pair[1].get("id", pair[0])))
    wipes, kills, per_boss, special = 0, 0, Counter(), []
    for _, row in rows:
        if row.get("type", "boss") != "boss":
            continue
        payload = row.get("payload", row)
        payload = json.loads(payload) if isinstance(payload, str) else payload
        map_id, boss = payload.get("map", 0), payload.get("boss_id", -1)
        if not is_raid_fight(map_id, boss):
            continue
        pair = (map_id, boss)
        if payload.get("state") == "wiped":
            wipes += 1
            per_boss[pair] += 1
        elif payload.get("state") == "killed":
            kills += 1
            if per_boss[pair] >= HARD_KILL_WIPES:
                special.append("hard kill: " + boss_name(map_id, boss))
            elif pair in FINAL_BOSSES:
                special.append("final boss: " + boss_name(map_id, boss))
    reasons = list(dict.fromkeys(special))
    if kills:
        reasons.append(f"{kills} kill" + ("s" if kills != 1 else ""))
    if wipes:
        reasons.append(f"{wipes} wipe" + ("s" if wipes != 1 else ""))
    rough = wipes >= 8 and kills <= 1
    if rough:
        reasons.append("rough night")
    value = min(0.3, 0.1 * kills) + (0.3 if special else 0) - min(0.4, 0.05 * wipes) - (0.2 if rough else 0)
    return _clamp(value, -0.5, 0.5), reasons


def _evidence_scope_state(store, owner, refs):
    """Full lineage scope, including legacy open wrappers; None means fail closed.

    Reuse forgetting's persisted-node reader so private encounter/fact roots and story,
    chronicle, feud and joke ancestry have one definition. Each memory owner's boundary
    preserves the partner of a legitimate private retelling.
    """
    nodes, active, computed = {}, set(), {}

    def visit(ref, viewer):
        key = (ref, viewer)
        if key in computed:
            return computed[key]
        if ref in active:
            raise ValueError("cyclic emotion evidence")
        if ref not in nodes:
            if len(nodes) >= 200:
                raise ValueError("oversized emotion evidence")
            nodes[ref] = _provenance_node(store, ref)
        node = nodes[ref]
        if node is None:
            raise ValueError("missing emotion evidence")
        _, children, holder, scope, parties = node
        if ref.startswith("chat:"):
            line = store.conn.execute("SELECT * FROM chat_line WHERE id=?",
                                     (int(ref.split(":", 1)[1]),)).fetchone()
            if line["origin"] == "mind":
                paired = store.conn.execute("SELECT memory_ids,dropped FROM utterance "
                    "WHERE guid=? AND channel=? AND text=? AND to_guid=? AND ts BETWEEN ? AND ? "
                    "ORDER BY abs(ts-?),id LIMIT 1", (line["from_guid"], line["channel"], line["text"],
                    line["to_guid"], line["ts"] - 2, line["ts"] + 2, line["ts"])).fetchone()
                if paired is None or paired["dropped"] is not None:
                    raise ValueError("unpaired emotion output")
                memories = json.loads(paired["memory_ids"])
                if not isinstance(memories, list) or any(type(i) is not int or i <= 0 for i in memories):
                    raise ValueError("invalid emotional output provenance")
                children = (*children, *(f"memory:{i}" for i in memories))
        elif ref.startswith("joke:"):
            # Task20 retains every public input of a term, beyond its first origin.
            retained = json.loads(store.get_meta(f"nightly:lexicon-sources:{ref.split(':', 1)[1]}") or "[]")
            if not isinstance(retained, list) or any(not isinstance(r, str) for r in retained):
                raise ValueError("invalid emotion term provenance")
            children = (*children, *retained)
        children = tuple(dict.fromkeys(children))
        nodes[ref] = (node[0], children, holder, scope, parties)
        active.add(ref)
        if holder is None:
            if scope[0] != "open":
                partners = parties - {viewer} if viewer in parties else set()
                scope = scope[0], next(iter(partners)) if len(partners) == 1 else 0
            for child in children:
                scope = stricter(scope, visit(child, viewer))
        else:
            for child in children:
                scope = stricter(scope, visit(child, holder))
            if scope[0] != "open" and holder != viewer:
                scope = scope[0], holder if scope[1] else 0
        active.remove(ref)
        computed[key] = scope
        return scope

    if not refs:
        return None
    try:
        result = ("open", 0)
        for ref in refs:
            result = stricter(result, visit(ref, owner))
        return result, nodes
    except (ValueError, TypeError, KeyError):
        return None


def evidence_scope(store, owner, refs):
    """The strictest full-lineage scope, or None for incomplete evidence."""
    state = _evidence_scope_state(store, owner, refs)
    return None if state is None else state[0]


def secret_memory_ok(ctx, owner, refs):
    """A new secret-bearing copy waits for the probe, except the subject's own secret.

    Holding a source or speaking in a whisper does not prove whose secret it is. An
    exemption needs explicit secret facts naming this owner for every secret branch;
    unknown subjects and scope-only secrets remain gated. This uses the same complete
    graph as scope derivation, including recalled memories behind public chat.
    """
    refs = tuple(refs)
    state = _evidence_scope_state(ctx.store, owner, refs)
    if state is None:
        return False
    scope, nodes = state
    if scope[0] != "secret" or knowledge.secret_probe_passed(ctx.store, now=ctx.clock.now(),
                                                          max_rate=ctx.settings.probe_max_rate,
                                                          model=ctx.settings.live_model):
        return True
    proved = {}

    def own_secret(ref):
        if ref in proved:
            return proved[ref]
        _, children, _, local, _ = nodes[ref]
        descendants = [own_secret(child) for child in children]
        inherited = any(secret for secret, _ in descendants)
        safe = all(ok for _, ok in descendants)
        secret = local[0] == "secret" or inherited
        if local[0] == "secret":
            subjects = [r[0] for r in ctx.store.conn.execute(
                "SELECT about FROM fact WHERE root=? AND secret=1", (ref,))]
            explicit = bool(subjects) and all(subject == owner for subject in subjects)
            # A wrapper may inherit a proved secret; an unknown secret leaf cannot.
            safe = safe and (explicit or inherited)
            if subjects and not explicit:
                safe = False
        proved[ref] = secret, safe
        return proved[ref]

    return all(own_secret(ref)[1] for ref in refs)


def _elapsed(night, earlier):
    return max(1, (date.fromisoformat(night) - date.fromisoformat(earlier)).days)


def _pad_key(guid):
    return f"nightly:mood-night:{guid}"


def _feeling_key(guid, target):
    return f"nightly:emotion-night:{guid}:{target}"


def _pending_key(guid, target):
    return f"nightly:emotion-pending:{guid}:{target}"


def _targeted(ctx, emotions, members, now):
    strongest = {}
    for row in emotions:
        if (row["guid"] not in members or row["target"] not in members | {ctx.human}
                or row["intensity"] < 2 or row["type"] not in EMOTION_LINES):
            continue
        pair = row["guid"], row["target"]
        old = strongest.get(pair)
        if old is None or (row["intensity"], row["ts"], row["id"]) > (old["intensity"], old["ts"], old["id"]):
            strongest[pair] = row
    for pending in ctx.store.conn.execute("SELECT key,value FROM meta WHERE key LIKE 'nightly:emotion-pending:%'").fetchall():
        row = ctx.store.conn.execute("SELECT * FROM emotion WHERE id=?", (pending["value"],)).fetchone()
        if (row is None or row["guid"] not in members or row["target"] not in members | {ctx.human}
                or row["type"] not in EMOTION_LINES or row["intensity"] < 2
                or pending["key"] != _pending_key(row["guid"], row["target"])
                or (date.fromisoformat(ctx.night) - date.fromisoformat(row["night"])).days >= 7):
            ctx.store.conn.execute("DELETE FROM meta WHERE key=?", (pending["key"],))
        elif row["night"] <= ctx.night:
            # Today's new feeling supersedes an older deferred one, irrespective of intensity.
            strongest.setdefault((row["guid"], row["target"]), row)
    for (guid, target), emotion in sorted(strongest.items()):
        pending_key = _pending_key(guid, target)
        refs = json.loads(emotion["evidence"])
        scope = evidence_scope(ctx.store, guid, refs)
        if scope is None:
            ctx.store.conn.execute("DELETE FROM meta WHERE key=?", (pending_key,))
            continue
        if not secret_memory_ok(ctx, guid, refs):
            ctx.store.set_meta(pending_key, str(emotion["id"]))
            continue
        written = ctx.writer.scope_of(guid, refs, scope=scope[0], scope_with=scope[1])
        if stricter(written, scope) != written:
            continue  # direct evidence must not override a stricter ancestry partner
        line = EMOTION_LINES[emotion["type"]].format(target=ctx.name(target))
        text = clean_memory_text(line + (" " + emotion["why"] if emotion["why"] else ""), ctx.ident)
        row = reflection_row(guid, text, now=now, actors=[target], evidence=refs,
            key=f"emotion:{target}", importance=4, valence=OCC_PAD[emotion["type"]][0])
        ctx.writer.put(replace(row, scope=scope[0], scope_with=scope[1]), shown=refs)
        ctx.store.set_meta(_feeling_key(guid, target), emotion["night"])
        ctx.store.conn.execute("DELETE FROM meta WHERE key=?", (pending_key,))
    ids = [r[0] for r in ctx.store.conn.execute("SELECT id FROM memory WHERE status='active' "
        "AND substr(key,1,8)='emotion:'")]
    for memory in ctx.store.get_memories(ids):
        if memory.owner not in members:
            continue
        target = memory.key.removeprefix("emotion:")
        renewed = ctx.store.get_meta(_feeling_key(memory.owner, target)) or night_of_ts(memory.t_event, ctx.tz)
        if (date.fromisoformat(ctx.night) - date.fromisoformat(renewed)).days >= 7:
            ctx.store.conn.execute("UPDATE memory SET status='archived' WHERE id=?", (memory.id,))


async def stage_mood(ctx):
    if ctx.done(STAGE, "all"):
        return
    unknown = set()

    def write(store):
        now = ctx.clock.now()
        members = set(ctx.members(include_away=True)) - {0, ctx.human}
        if any((store.get_meta(_pad_key(guid)) or "") > ctx.night for guid in members):
            raise ValueError("A later logical night already updated the guild mood")
        personas = {g: ctx.persona(g) for g in members}
        traits = {g: trait_today(store, ctx.night, g, personas[g]) for g in members}
        events = store.conn.execute("SELECT id,ts,type,payload FROM game_event WHERE type='boss' "
                                    "AND ts>=? AND ts<? ORDER BY ts,id", ctx.window).fetchall()
        value, reasons = night_mood(events)
        previous = store.conn.execute("SELECT night,value FROM guild_mood WHERE night<? "
                                       "ORDER BY night DESC LIMIT 1", (ctx.night,)).fetchone()
        if previous is not None:
            value += GUILD_MOOD_CARRY ** _elapsed(ctx.night, previous["night"]) * previous["value"]
        value = _clamp(value, -0.5, 0.5)
        store.conn.execute("INSERT INTO guild_mood(night,value,reasons) VALUES (?,?,?) "
            "ON CONFLICT(night) DO UPDATE SET value=excluded.value,reasons=excluded.reasons",
            (ctx.night, value, json.dumps(reasons)))
        if value:
            for guid in sorted(members):
                neuroticism = traits[guid]["neuroticism"]
                intensity = min(3, 1 + round(4 * abs(value)))
                intensity += 1 if neuroticism >= 7 else -1 if neuroticism <= 3 else 0
                store.conn.execute("INSERT INTO emotion(guid,night,type,target,intensity,why,source,evidence,ts) "
                    "VALUES (?,?,?,0,?,'','guild',?,?)", (guid, ctx.night, "joy" if value > 0 else "distress",
                    max(1, min(3, intensity)), json.dumps([f"event:{r['id']}" for r in events]), now))
        emotions = store.conn.execute("SELECT * FROM emotion WHERE night=? ORDER BY id", (ctx.night,)).fetchall()
        unknown.update(row["type"] for row in emotions if row["type"] not in OCC_PAD)
        by_guid = defaultdict(list)
        for row in emotions:
            if row["guid"] in members:
                by_guid[row["guid"]].append((row["type"], row["intensity"]))
        bases, snapshot = {}, {}
        for guid in sorted(members):
            bases[guid] = base = baseline(personas[guid])
            prior = store.conn.execute("SELECT * FROM pad_mood WHERE guid=?", (guid,)).fetchone()
            earlier = None if prior is None else (store.get_meta(_pad_key(guid)) or night_of_ts(prior["updated"], ctx.tz))
            mood = base if prior is None else (prior["p"], prior["a"], prior["d"])
            snapshot[guid] = step_mood(mood, base, by_guid[guid],
                                      nights=1 if earlier is None else _elapsed(ctx.night, earlier))
        for guid in sorted(members):
            donors = [other for other in sorted(members - {guid})
                      if store.get_relationship(other, guid).affinity >= 40]
            point = snapshot[guid]
            if donors:
                mean = tuple(sum(snapshot[other][axis] for other in donors) / len(donors) for axis in range(3))
                rate = CONTAGION * (0.5 if traits[guid]["openness"] <= 3 else 1)
                point = tuple(_clamp(m + rate * (friend - m)) for m, friend in zip(point, mean))
            store.conn.execute("INSERT INTO pad_mood(guid,p,a,d,updated) VALUES (?,?,?,?,?) "
                "ON CONFLICT(guid) DO UPDATE SET p=excluded.p,a=excluded.a,d=excluded.d,updated=excluded.updated",
                (guid, *point, now))
            store.set_meta(_pad_key(guid), ctx.night)
            store.put_state(replace(store.get_state(guid), mood=point[0], mood_baseline=bases[guid][0], updated=int(now)))
        _targeted(ctx, emotions, members, now)

    if ctx.commit(STAGE, "all", write) and unknown:
        log("emotion_type_unknown", level=logging.WARNING, night=ctx.night, count=len(unknown))
