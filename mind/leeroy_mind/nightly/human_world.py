"""Follow the human's life and learn bounded, individual conversational preferences.

Inputs are frozen per logical night. Reaction receipts survive overlapping catch-up windows;
every counter, memory and receipt is committed together. Private examples never enter the
unscoped preference profile, and romance never enters an optimising bandit.
"""
import hashlib
import json
import re
from collections import defaultdict
from dataclasses import asdict, replace
from datetime import datetime

from leeroy_mind.memory.privacy import stricter
from leeroy_mind.memory.writer import clean_memory_text
from leeroy_mind.nightly.attraction import reflection_row
from leeroy_mind.nightly.audience import audience_of, bit_of, cast_names, reason_of
from leeroy_mind.nightly.evidence import Evidence, Item, load_facts, load_items
from leeroy_mind.nightly.forget import _node
from leeroy_mind.nightly.ground import Claim, ground, log_verdicts
from leeroy_mind.nightly.lexicon import public_sources
from leeroy_mind.nightly.llm import schema, system_prompt
from leeroy_mind.nightly.mood import evidence_scope, secret_memory_ok
from leeroy_mind.nightly.reactions import (ANSWERED, LAUGH_RE, LIFE_RE, NO_RE, REPLY_S,
                                          WHISPER_BACK_S, first_after, reactions)
from leeroy_mind.nightly.settings import night_of_ts
from leeroy_mind.nightly.store import active_away
from leeroy_mind.nightly.stories import PROVENANCE_MAX
from leeroy_mind.nightly.wowlife import TIES_RE
from leeroy_mind.personas.io import load_persona
from leeroy_mind.personas.screens import has_word
from leeroy_mind.personas.stances import is_woman
from leeroy_mind.talk.prompt import is_close
from leeroy_mind.talk.turns import TurnKind
from leeroy_mind.text import strip_wow_markup

STAGE = "human_world"
DAY_S = 86400
LAND_WORDS = {"bit": "running jokes", "flirt": "flirting", "whisper": "whispers",
              "open": "stories", "continue": "comebacks", "aside": "asides", "gz": "congrats",
              "reply_human": "answers", "kill": "kill calls", "wipe_react": "wipe jokes",
              "greet": "hellos", "goodnight": "goodnights"}
ARM_EXCLUDED = frozenset({"flirt", "whisper", "callout", "urgent"})
FLATTERY_RE = re.compile(r"\b(you'?re (the|so) (best|good|amazing|awesome|smart|funny)|best (gm|guild ?master|leader|raid ?lead)|you carried|carried us|so lucky to have you|couldn'?t do it without you|you'?re a genius|legend)\b", re.I)
EXPLORE_FLOOR, FLATTERY_CAP, WEAK_NO, STRONG_NO = 0.15, 0.5, 0.5, 1.5
FOLLOW_SCHEMA = schema("followup", {"question": {"type": "string", "maxLength": 120}})
LANDS_PROFILE_SCHEMA = schema("lands_profile", {
    "profile": {"type": "string", "maxLength": 400},
    "evidence": {"type": "array", "items": {"type": "integer"}, "minItems": 1}})
_PREFIX = "nightly:human-world:"


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _pair(store, line):
    return store.conn.execute("SELECT * FROM utterance WHERE guid=? AND channel=? AND text=? "
        "AND to_guid=? AND ts BETWEEN ? AND ? ORDER BY abs(ts-?),id LIMIT 1",
        (line["from_guid"], line["channel"], line["text"], line["to_guid"],
         line["ts"] - 2, line["ts"] + 2, line["ts"])).fetchone()


def _provenance(store, refs, *, limit=200):
    """Comparable full ancestry, including sent-output recall and retained joke origins."""
    nodes, active = {}, set()

    def visit(ref):
        if not isinstance(ref, str) or ref in active:
            raise ValueError("cyclic or invalid human-world source")
        if ref in nodes:
            return
        if len(nodes) >= limit:
            raise ValueError("oversized human-world source")
        node = _node(store, ref)
        if node is None:
            raise ValueError("missing human-world source")
        value, children, holder, scope, parties = node
        extra = ()
        if ref.startswith("chat:"):
            line = store.conn.execute("SELECT * FROM chat_line WHERE id=?", (int(ref[5:]),)).fetchone()
            if line["origin"] == "mind":
                paired = _pair(store, line)
                if paired is None or paired["dropped"] is not None:
                    raise ValueError("unsent human-world source")
                ids = json.loads(paired["memory_ids"])
                if not isinstance(ids, list) or any(type(i) is not int or i <= 0 for i in ids):
                    raise ValueError("invalid recalled source")
                children = (*children, *(f"memory:{i}" for i in ids))
                extra = tuple(paired)
        elif ref.startswith("joke:"):
            saved = json.loads(store.get_meta(f"nightly:lexicon-sources:{ref[5:]}") or "[]")
            if not isinstance(saved, list) or any(not isinstance(r, str) for r in saved):
                raise ValueError("invalid term source")
            children = (*children, *saved)
            extra = tuple(saved)
        nodes[ref] = (value, extra, holder, scope, tuple(sorted(parties)))
        active.add(ref)
        for child in children:
            visit(child)
        active.remove(ref)

    try:
        for ref in refs:
            visit(ref)
    except (ValueError, TypeError, KeyError):
        return None
    return nodes if nodes else None


def _human_roots(store, memory, human):
    nodes = _provenance(store, [f"memory:{memory.id}"])
    if nodes is None:
        return []
    roots = []
    for ref in nodes:
        if ref.startswith("chat:"):
            row = store.conn.execute("SELECT * FROM chat_line WHERE id=?", (int(ref[5:]),)).fetchone()
            if row["from_guid"] == human and row["origin"] == "human":
                roots.append((row["ts"], row["id"], ref))
    return [r[2] for r in sorted(roots, reverse=True)]


def his_life_facts(store, window, human, *, limit):
    """Newest active life memories, one per original human chat line."""
    if limit <= 0:
        return []
    ids = [r[0] for r in store.conn.execute("SELECT id FROM memory WHERE owner!=0 "
        "AND status='active' AND t_created>=? AND t_created<? ORDER BY t_created DESC,id DESC", window)]
    selected, seen = [], set()
    for memory in store.get_memories(ids):
        if not (LIFE_RE.search(memory.text) or TIES_RE.search(memory.text)):
            continue
        roots = _human_roots(store, memory, human)
        if not roots or any(root in seen for root in roots):
            continue
        seen.update(roots)
        selected.append(memory)
        if len(selected) >= limit:
            break
    return selected


def _names(store, human):
    names = cast_names(store)
    roster = store.cast()
    names[human] = roster[human].name if human in roster else ""
    return names


def arm_of(store, utterance, persona, human, *, called=""):
    row = store.conn.execute("SELECT * FROM utterance WHERE id=?",
        (utterance if isinstance(utterance, int) else utterance["id"],)).fetchone()
    if row is None or row["dropped"] is not None or row["guid"] != persona.guid:
        return None
    kind = row["kind"]
    if row["channel"] == "whisper" or kind in ARM_EXCLUDED or kind not in {k.value for k in TurnKind}:
        return None
    names = _names(store, human)
    why = reason_of(store, row) or ""
    addressed = (row["to_guid"] == human or why.startswith("reply:")
        or kind in {"reply_human", "greet", "goodnight"}
        or any(has_word(row["text"], n) for n in (names[human], called) if n))
    # `why` describes a turn's trigger, never a proof that its text carried no romance.
    if is_woman(persona) and persona.romance.stance in {"fond", "drawn", "lust"} and addressed:
        return None
    if addressed and FLATTERY_RE.search(row["text"]):
        return "flattery"
    if kind == "bit":
        bit = bit_of(store, row)
        return f"bit:{bit}" if bit in {b.id for b in persona.bits} else None
    return kind


def context_key(store, ts, human):
    raid = False
    for event in store.conn.execute("SELECT payload FROM game_event WHERE type='group' AND ts<=? "
                                    "ORDER BY ts DESC,id DESC", (ts,)):
        payload = json.loads(event[0])
        # The bridge's group events describe the human's current group, including departures.
        members = payload.get("members", [])
        raid = bool(payload.get("raid")) and human in members
        break
    busy = store.conn.execute("SELECT count(*) FROM chat_line WHERE from_guid=? AND origin='human' "
                             "AND ts>=? AND ts<?", (human, ts - 600, ts)).fetchone()[0] >= 3
    hour = datetime.fromtimestamp(ts, store.clock.local().tzinfo).hour
    return f"{'raid' if raid else 'free'}:{'busy' if busy else 'quiet'}:{'late' if 1 <= hour < 6 else 'day'}"


def arm_weights(store, guid, arms, context, rng):
    """Sample only the requested member's repertoire; never borrow another member's rows."""
    persona = load_persona(store, guid)
    if persona is None or guid not in cast_names(store):
        return {}
    allowed = ({k.value for k in TurnKind} - ARM_EXCLUDED - {"bit"}) | {"flattery"}
    allowed |= {f"bit:{b.id}" for b in persona.bits}
    weights = {}
    for arm in dict.fromkeys(arms):
        if arm not in allowed:
            continue
        row = store.conn.execute("SELECT alpha,beta FROM bandit WHERE guid=? AND arm=? AND context=?",
                                 (guid, arm, context)).fetchone()
        weights[arm] = rng.betavariate(1 + (row[0] if row else 0), 1 + (row[1] if row else 0))
    floor = EXPLORE_FLOOR * max(weights.values(), default=0)
    weights = {arm: max(floor, value) for arm, value in weights.items()}
    if "flattery" in weights:
        weights["flattery"] = min(weights["flattery"], FLATTERY_CAP *
                                   max((v for a, v in weights.items() if a != "flattery"), default=0))
    return weights


def _reaction_refs(ctx, reaction, line):
    """The bids and actual feedback that justify a reaction, including private replies."""
    names = cast_names(ctx.store) | {ctx.human: ctx.ident.called}
    answers = ctx.store.conn.execute("SELECT * FROM chat_line WHERE origin='human' AND from_guid=? "
        "AND (ts>? OR (ts=? AND id>?)) AND ts<=? ORDER BY ts,id",
        (ctx.human, line["ts"], line["ts"], line["id"], line["ts"] + WHISPER_BACK_S)).fetchall()
    direct = [a for a in answers if (a["channel"] == "whisper" and a["to_guid"] == reaction.speaker) or (
        a["channel"] == line["channel"] and a["channel"] != "whisper" and a["ts"] <= line["ts"] + REPLY_S
        and (has_word(a["text"], names[reaction.speaker]) or (
            not any(has_word(a["text"], n) for g, n in names.items() if g not in (reaction.speaker, ctx.human))
            and first_after(ctx.store, line, a))))]
    chosen = {"laugh": lambda a: LAUGH_RE.search(a["text"]), "rebuff": lambda a: NO_RE.search(a["text"]),
              "whisper": lambda a: a["channel"] == "whisper", "reply": lambda a: True}
    refs = [f"chat:{line['id']}"]
    if reaction.reaction in chosen:
        refs += [f"chat:{a['id']}" for a in direct if chosen[reaction.reaction](a)][:1]
    elif reaction.reaction == "ignored":
        refs += [f"chat:{a['id']}" for a in answers if a["ts"] <= line["ts"] + REPLY_S][:1]
    elif reaction.reaction == "emote":
        for event in ctx.store.conn.execute("SELECT * FROM game_event WHERE type='text_emote' "
                "AND ts>? AND ts<=? ORDER BY ts,id", (line["ts"], line["ts"] + REPLY_S)):
            payload = json.loads(event["payload"])
            if payload.get("guid") == ctx.human and payload.get("target_guid") == reaction.speaker:
                refs.append(f"event:{event['id']}")
                break
    return refs


def _record(ctx, reaction):
    store = ctx.store
    line = store.conn.execute("SELECT * FROM chat_line WHERE id=?", (reaction.line_id,)).fetchone()
    paired = _pair(store, line)
    if paired is None or paired["dropped"] is not None:
        return None
    refs = _reaction_refs(ctx, reaction, line)
    nodes = _provenance(store, refs)
    if nodes is None:
        return None
    persona = load_persona(store, reaction.speaker)
    return asdict(replace(reaction, kind=paired["kind"])) | {"utterance": paired["id"],
        "refs": refs, "stamp": _digest(nodes),
        "arm": arm_of(store, paired, persona, ctx.human, called=ctx.ident.called),
        "context": context_key(store, reaction.ts, ctx.human)}


def _valid(ctx, record):
    nodes = _provenance(ctx.store, record["refs"])
    return (record["speaker"] in cast_names(ctx.store) and nodes is not None
            and _digest(nodes) == record["stamp"])


def _learning(ctx, record):
    persona = load_persona(ctx.store, record["speaker"])
    return (persona is not None and record["arm"] is not None and record["reaction"] != "none"
            and _valid(ctx, record) and arm_of(ctx.store, record["utterance"], persona, ctx.human,
                called=ctx.ident.called) == record["arm"])


def _input_plan(ctx):
    """Read the durable plan, or preview it without writing during budget planning."""
    key = _PREFIX + "inputs:" + ctx.night
    saved = ctx.store.get_meta(key)
    if saved is not None:
        plan = json.loads(saved)
        if plan["window"] != list(ctx.window):
            raise ValueError("human-world window changed on resume")
        return plan
    facts = []
    for memory in his_life_facts(ctx.store, ctx.window, ctx.human, limit=ctx.settings.follow_facts):
        roots = _human_roots(ctx.store, memory, ctx.human)
        facts.append({"id": memory.id, "root": roots[0]})
    observed = []
    names = cast_names(ctx.store) | {ctx.human: ctx.ident.called}
    for reaction in reactions(ctx.store, ctx.window, ctx.human, names):
        # Reuse the first observation if another catch-up window already counted this line.
        prior = ctx.store.get_meta(_PREFIX + f"reaction:{reaction.line_id}")
        record = json.loads(prior) if prior is not None else _record(ctx, reaction)
        if record is not None:
            observed.append(record)
    return {"window": list(ctx.window), "facts": facts, "reactions": observed}


def _inputs(ctx):
    key = _PREFIX + "inputs:" + ctx.night
    plan = _input_plan(ctx)
    if ctx.store.get_meta(key) is not None:
        return plan
    if not ctx.commit(STAGE, "inputs", lambda store: store.set_meta(key, json.dumps(plan))):
        return None
    return plan


def count_follow(ctx):
    count = 0
    for fact in _input_plan(ctx)["facts"]:
        if ctx.done(STAGE, "follow:" + fact["root"]):
            continue
        state = _follow_state(ctx, fact)
        if state is not None and state[2] and state[3]:
            count += 1
    return count


def count_profile(ctx):
    if ctx.settings.profile_per_night <= 0 or ctx.done(STAGE, "profile"):
        return 0
    # Current-night bandit rows do not exist until the stage runs. Count the potentially
    # usable bids now; insufficient cumulative rates can only make this estimate smaller.
    records = _input_plan(ctx)["reactions"]
    return int(sum(_learning(ctx, r) and public_sources(ctx.store, r["refs"]) is not None
                   for r in records) >= 10)


def _eligible(ctx):
    return set(cast_names(ctx.store)) - set(active_away(ctx.store)) - {ctx.human, 0}


def _close(ctx, limit):
    return sorted((g for g in _eligible(ctx) if is_close(ctx.store.get_relationship(g, ctx.human))),
                  key=lambda g: (-ctx.store.get_relationship(g, ctx.human).affinity, g))[:limit]


def _authorized(store, guid, nodes):
    for ref, (_, _, holder, scope, parties) in nodes.items():
        if holder is not None and scope[0] != "open" and guid not in (holder, scope[1]):
            return False
        if holder is None and scope[0] != "open" and guid not in parties:
            return False
        if ref.startswith("chat:"):
            line = store.conn.execute("SELECT * FROM chat_line WHERE id=?", (int(ref[5:]),)).fetchone()
            if line["channel"] == "whisper":
                if guid not in (line["from_guid"], line["to_guid"]):
                    return False
            elif guid not in audience_of(store, line["id"]) and guid != line["from_guid"]:
                return False
    return True


def _follow_state(ctx, fact):
    rows = ctx.store.get_memories([fact["id"]])
    if not rows or rows[0].status != "active":
        return None
    memory = rows[0]
    nodes = _provenance(ctx.store, [f"memory:{memory.id}"])
    if nodes is None or fact["root"] not in _human_roots(ctx.store, memory, ctx.human):
        return None
    line = ctx.store.conn.execute("SELECT * FROM chat_line WHERE id=?", (int(fact["root"][5:]),)).fetchone()
    heard = {line["to_guid"]} if line["channel"] == "whisper" else audience_of(ctx.store, line["id"])
    key = "followup:" + fact["root"]
    candidates = []
    for guid in _close(ctx, len(_eligible(ctx))):
        if guid not in heard or not _authorized(ctx.store, guid, nodes):
            continue
        if ctx.store.conn.execute("SELECT 1 FROM memory WHERE owner=? AND key=?", (guid, key)).fetchone():
            continue
        scope = evidence_scope(ctx.store, guid, [f"memory:{memory.id}"])
        if scope is None or scope[0] != "open" and scope[1] != ctx.human:
            continue
        candidates.append((guid, scope))
    candidates = candidates[:ctx.settings.follow_members]
    # Relationships, eligibility and the source graph are re-read after all awaits.
    permitted = all(secret_memory_ok(ctx, guid, [f"memory:{memory.id}"])
                    for guid, _ in candidates)
    return memory, nodes, candidates, permitted


def _follow_stamp(state):
    return None if state is None else _digest((state[1], state[2], state[3]))


async def _follow(ctx, fact):
    unit = "follow:" + fact["root"]
    state = _follow_state(ctx, fact)
    if state is None or not state[2]:
        ctx.skip(STAGE, unit)
        return
    if not state[3]:
        return  # keep the frozen unit resumable when the live secret probe later passes
    memory, nodes, candidates, _ = state
    stamp = _follow_stamp(state)
    ref = f"memory:{memory.id}"
    ev = Evidence()
    for item in load_items(ctx.store, [ref], ident=ctx.ident):
        ev.add(replace(item, actors=frozenset({ctx.human})))
    answer = await ctx.reasoner.json(system_prompt("followup",
        "Write one short question a guildmate would ask {called} next time, to follow up on this. "
        "Casual, specific, in chat style.", ctx.settings.data_dir, human=ctx.ident, called=ctx.ident.called),
        ev.render(ctx.tz), FOLLOW_SCHEMA, temperature=0.6)
    if _follow_stamp(_follow_state(ctx, fact)) != stamp:
        raise ValueError("follow-up source or audience changed")
    claim = Claim("followup", 0, strip_wow_markup(answer["question"]).strip(), [ref])
    pairs = await ground(ctx, [claim], ev, load_facts(ctx))
    if _follow_stamp(_follow_state(ctx, fact)) != stamp:
        raise ValueError("follow-up source or audience changed")
    text = clean_memory_text(f"Ask {ctx.ident.called}: {claim.text}", ctx.ident)
    vector = (await ctx.embed([text]))[0] if pairs[0][1].ok else None

    def write(store):
        if _follow_stamp(_follow_state(ctx, fact)) != stamp:
            raise ValueError("follow-up source or audience changed")
        log_verdicts(store, ctx.night, STAGE, pairs, ctx.clock.now(), ctx.ident)
        if vector is None:
            return
        for guid, scope in candidates:
            row = reflection_row(guid, text, now=ctx.clock.now(), actors=[ctx.human],
                evidence=[ref], key="followup:" + fact["root"], importance=6, valence=0)
            written = ctx.writer.scope_of(guid, [ref], shown=[ref], scope=scope[0], scope_with=scope[1])
            if stricter(written, scope) != written:
                continue
            ctx.writer.put(replace(row, kind="plan", embedding=vector, scope=scope[0], scope_with=scope[1]),
                           shown=[ref])
    ctx.commit(STAGE, unit, write)


def _follow_done(ctx):
    ids = [r[0] for r in ctx.store.conn.execute("SELECT id FROM memory WHERE status='active' "
                                              "AND substr(key,1,9)='followup:'")]
    for memory in ctx.store.get_memories(ids):
        cited = ctx.store.conn.execute("SELECT 1 FROM utterance u,json_each(u.memory_ids) m "
            "WHERE u.guid=? AND u.dropped IS NULL AND u.ts<? AND u.ts>=? AND m.value=? LIMIT 1",
            (memory.owner, ctx.window[0], memory.t_created, memory.id)).fetchone()
        if cited or ctx.clock.now() - memory.t_created > 7 * DAY_S:
            ctx.store.conn.execute("UPDATE memory SET status='archived' WHERE id=?", (memory.id,))


def _absence(ctx):
    session = ctx.store.conn.execute("SELECT * FROM session ORDER BY started DESC,id DESC LIMIT 1").fetchone()
    if session is None or session["ended"] is None:
        return
    days = (ctx.clock.now() - session["ended"]) / DAY_S
    if days < ctx.settings.absent_days:
        return
    key = "absent:" + night_of_ts(session["ended"], ctx.tz)
    for guid in _close(ctx, ctx.settings.notice_members):
        row = reflection_row(guid, clean_memory_text(f"{ctx.ident.called} hasn't been on in {int(days)} days.", ctx.ident),
            now=ctx.clock.now(), actors=[ctx.human], evidence=[f"session:{session['id']}"],
            key=key, importance=5, valence=-0.2)
        ctx.writer.put(replace(row, kind="observation", source="game"))


def _lands(ctx, records):
    touched = {tuple(json.loads(row[0])) for row in ctx.store.conn.execute(
        "SELECT value FROM meta WHERE key LIKE ? AND value!=''", (_PREFIX + "lands-pending:%",))}
    for record in records:
        key = _PREFIX + f"lands:{record['line_id']}"
        if ctx.store.get_meta(key) is not None or not _valid(ctx, record):
            continue
        guid, kind, reaction = record["speaker"], record["kind"], record["reaction"]
        for counted in ((kind, "*") if kind in LAND_WORDS else ("*",)):
            ctx.store.conn.execute("INSERT INTO lands(guid,kind,n,laughs,replies,ignored,updated) "
                "VALUES (?,?,1,?,?,?,?) ON CONFLICT(guid,kind) DO UPDATE SET n=n+1,"
                "laughs=laughs+excluded.laughs,replies=replies+excluded.replies,"
                "ignored=ignored+excluded.ignored,updated=excluded.updated",
                (guid, counted, int(reaction == "laugh"), int(reaction in {"reply", "whisper", "emote"}),
                 int(reaction in {"ignored", "rebuff"}), ctx.clock.now()))
        history_key = _PREFIX + f"lands-history:{guid}:{kind}"
        history = json.loads(ctx.store.get_meta(history_key) or "[]")
        history = sorted({r["line_id"]: r for r in [*history, record]}.values(),
                         key=lambda r: (r["ts"], r["line_id"]))[-3:]
        ctx.store.set_meta(history_key, json.dumps(history))
        ctx.store.set_meta(key, ctx.night)
        ctx.store.set_meta(_PREFIX + f"reaction:{record['line_id']}", json.dumps(record))
        touched.add((guid, kind))
    for guid, kind in sorted(touched):
        if kind not in LAND_WORDS:
            continue
        row = ctx.store.conn.execute("SELECT * FROM lands WHERE guid=? AND kind=?", (guid, kind)).fetchone()
        rate = (row["laughs"] + row["replies"]) / row["n"]
        if row["n"] >= 5 and rate >= 0.5:
            verb = "laughs at" if row["laughs"] >= row["replies"] else "answers"
            text = f"{ctx.ident.called} usually {verb} my {LAND_WORDS[kind]}."
        elif row["n"] >= 8 and rate <= 0.1:
            text = f"My {LAND_WORDS[kind]} don't land with {ctx.ident.called}."
        else:
            continue
        history = json.loads(ctx.store.get_meta(_PREFIX + f"lands-history:{guid}:{kind}"))
        if not all(_valid(ctx, r) for r in history):
            continue
        refs = [f"chat:{r['line_id']}" for r in history]
        shown = list(dict.fromkeys(ref for r in history for ref in r["refs"]))
        scope = evidence_scope(ctx.store, guid, shown)
        if scope is None:
            continue
        pending = _PREFIX + f"lands-pending:{guid}:{kind}"
        if not secret_memory_ok(ctx, guid, shown):
            ctx.store.set_meta(pending, json.dumps([guid, kind]))
            continue
        written = ctx.writer.scope_of(guid, refs, shown=shown, scope=scope[0], scope_with=scope[1])
        if stricter(written, scope) != written:
            continue
        memory = reflection_row(guid, clean_memory_text(text, ctx.ident), now=ctx.clock.now(), actors=[ctx.human], evidence=refs,
                                key=f"lands:{kind}", importance=4, valence=0)
        ctx.writer.put(replace(memory, scope=scope[0], scope_with=scope[1]), shown=shown)
        if ctx.store.get_meta(pending) is not None:
            ctx.store.set_meta(pending, "")


def _bandits(ctx, records):
    for record in records:
        key = _PREFIX + f"bandit:{record['line_id']}"
        if ctx.store.get_meta(key) is not None or not _valid(ctx, record):
            continue
        persona = load_persona(ctx.store, record["speaker"])
        arm = arm_of(ctx.store, record["utterance"], persona, ctx.human, called=ctx.ident.called)
        if arm != record["arm"] or arm is None or record["reaction"] == "none":
            continue
        reaction = record["reaction"]
        ctx.store.conn.execute("INSERT INTO bandit(guid,arm,context,alpha,beta,n,updated) "
            "VALUES (?,?,?,?,?,1,?) ON CONFLICT(guid,arm,context) DO UPDATE SET "
            "alpha=alpha+excluded.alpha,beta=beta+excluded.beta,n=n+1,updated=excluded.updated",
            (record["speaker"], arm, record["context"], int(reaction in ANSWERED),
             STRONG_NO if reaction == "rebuff" else WEAK_NO if reaction == "ignored" else 0,
             ctx.clock.now()))
        ctx.store.set_meta(key, ctx.night)
        ctx.store.set_meta(_PREFIX + f"reaction:{record['line_id']}", json.dumps(record))


def _profile_input(ctx):
    groups = defaultdict(list)
    for row in ctx.store.conn.execute("SELECT value FROM meta WHERE key LIKE ?", (_PREFIX + "reaction:%",)):
        record = json.loads(row[0])
        if (not _learning(ctx, record)
                or record["ts"] >= ctx.window[1]
                or ctx.store.get_meta(_PREFIX + f"bandit:{record['line_id']}") is None):
            continue
        if public_sources(ctx.store, record["refs"]) is not None:
            groups[record["speaker"], record["arm"]].append(record)
    rates = [(sum(r["reaction"] in ANSWERED for r in rs) / len(rs), guid, arm, rs)
             for (guid, arm), rs in groups.items() if len(rs) >= 5]
    rates.sort(key=lambda r: (r[0], r[1], r[2]))
    selected = {(r[1], r[2]): r for r in [*rates[-5:][::-1], *rates[:5]]}
    ev, summary, shown = Evidence(), [], []
    for rate, guid, arm, records in selected.values():
        laughs = sum(r["reaction"] == "laugh" for r in records)
        replies = sum(r["reaction"] in {"reply", "whisper", "emote"} for r in records)
        tally = (f"{ctx.name(guid)} / {arm}: {rate:.0%} answered ({len(records)} bids; "
                 f"{laughs} laughs, {replies} other answers)")
        summary.append(tally)
        # The judge sees the same deterministic tally as generation. Its backing inputs
        # are all retained below, rather than pretending a bid alone proves a response rate.
        ev.add(Item(f"rule:lands:{guid}:{arm}", tally, frozenset({guid, ctx.human}),
                    max(r["ts"] for r in records)))
        for record in sorted(records, key=lambda r: (r["ts"], r["line_id"]), reverse=True)[:2]:
            ref = f"chat:{record['line_id']}"
            for item in load_items(ctx.store, [ref], ident=ctx.ident):
                ev.add(item)
        # The summary itself depends on every reaction in its rate, not only its examples.
        shown.extend(ref for record in records for ref in record["refs"])
    refs = public_sources(ctx.store, tuple(dict.fromkeys(shown))) if shown else None
    nodes = _provenance(ctx.store, refs, limit=PROVENANCE_MAX) if refs else None
    return ev, "\n".join(summary), refs, _digest(nodes) if nodes else None


async def _profile(ctx, records):
    if ctx.settings.profile_per_night <= 0:
        ctx.skip(STAGE, "profile")
        return
    count = sum(_learning(ctx, r)
                and public_sources(ctx.store, r["refs"]) is not None for r in records)
    ev, summary, shown, stamp = _profile_input(ctx)
    if count < 10 or not ev.items() or stamp is None:
        ctx.skip(STAGE, "profile")
        return
    answer = await ctx.reasoner.json(system_prompt("lands_profile",
        "Describe what conversational approaches the human answers or laughs at. "
        "Use only the supplied public examples and rates; cite their numbered evidence. "
        "Do not recommend flattery, romance or manipulative tactics.", ctx.settings.data_dir, human=ctx.ident),
        summary + "\n\n" + ev.render(ctx.tz), LANDS_PROFILE_SCHEMA)
    fresh = _profile_input(ctx)
    if (fresh[1], fresh[2], fresh[3]) != (summary, shown, stamp):
        raise ValueError("preference profile sources changed")
    claim = Claim("profile", 0, strip_wow_markup(answer["profile"]).strip(), [ev.ref(i) for i in answer["evidence"]])
    pairs = await ground(ctx, [claim], ev, load_facts(ctx))

    def write(store):
        fresh = _profile_input(ctx)
        if (fresh[1], fresh[2], fresh[3]) != (summary, shown, stamp):
            raise ValueError("preference profile sources changed")
        log_verdicts(store, ctx.night, STAGE, pairs, ctx.clock.now(), ctx.ident)
        if pairs[0][1].ok:
            store.conn.execute("INSERT INTO lands_profile(night,text,evidence) VALUES (?,?,?)",
                               (ctx.night, claim.text, json.dumps(shown)))
    ctx.commit(STAGE, "profile", write)


async def stage_human_world(ctx):
    plan = _inputs(ctx)
    if plan is None:
        return
    if not ctx.done(STAGE, "follow_done"):
        ctx.commit(STAGE, "follow_done", lambda _: _follow_done(ctx))
    facts = {"follow:" + fact["root"]: fact for fact in plan["facts"]}
    await ctx.map_units(STAGE, list(facts), lambda unit: _follow(ctx, facts[unit]))
    for unit, fn in (("absence", lambda: _absence(ctx)), ("lands", lambda: _lands(ctx, plan["reactions"])),
                     ("bandits", lambda: _bandits(ctx, plan["reactions"]))):
        if not ctx.done(STAGE, unit):
            ctx.commit(STAGE, unit, lambda _, fn=fn: fn())
    if ctx.done(STAGE, "lands") and ctx.done(STAGE, "bandits"):
        await ctx.map_units(STAGE, ["profile"], lambda _: _profile(ctx, plan["reactions"]))
