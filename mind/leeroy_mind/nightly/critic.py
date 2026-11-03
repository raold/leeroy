"""A speaker's emitted dialogue checked against their card, for the owner's audit only.

Whispers remain eligible. Neither the critic's text nor its reasons become guild memories
or another persona's context; review_queue and persona_ledger are owner audit/undo records.
"""
import json
import re
from collections import defaultdict
from datetime import date, timedelta

from leeroy_mind.nightly.forget import _node
from leeroy_mind.nightly.ground import screen_text
from leeroy_mind.nightly.growth import rollback_week
from leeroy_mind.nightly.llm import check_schema, schema, system_prompt
from leeroy_mind.nightly.settings import night_of_ts
from leeroy_mind.nightly.store import enqueue_review, ledger_add, ledger_rows
from leeroy_mind.personas.card import render_prompt_card
from leeroy_mind.personas.io import load_persona
from leeroy_mind.text import strip_wow_markup
from leeroy_mind.wow import CAST_MIN_LEVEL, CLASSES, RACES

STAGE = "critic"
PROVENANCE_MAX = 5000
CRITIC_SCHEMA = schema("critic", {"scores": {"type": "array", "items": {
    "type": "object", "properties": {
        "i": {"type": "integer"}, "score": {"type": "integer", "minimum": 1, "maximum": 5},
        "why": {"type": "string", "maxLength": 120}},
    "required": ["i", "score", "why"], "additionalProperties": False}}})
PROMPT = ("You know {name} well. Score how much each line sounds like them: "
          "5 is exactly them, 1 is something they would never say.\n{card}")


def _members(store):
    human = int(store.get_meta("human_guid") or 0)
    versions = store.persona_versions()
    return {guid for guid, member in store.cast().items()
            if guid not in {0, human} and guid in versions and member.level >= CAST_MIN_LEVEL}


def sample_lines(store, window, rng, *, per_member, total):
    """Seeded, capped samples of sent dialogue; quiet/noncast owners never participate."""
    if per_member <= 0 or total <= 0:
        return {}
    with store.transaction():
        members = _members(store)
        grouped = defaultdict(list)
        for row in store.conn.execute("SELECT * FROM utterance WHERE dropped IS NULL "
                "AND ts>=? AND ts<? ORDER BY guid,ts,id", window):
            if row["guid"] in members:
                grouped[row["guid"]].append(row)
        owners = sorted(guid for guid, rows in grouped.items() if len(rows) >= 3)
        rng.shuffle(owners)
        picked = {}
        remaining = total
        for guid in owners:
            if not remaining:
                break
            rows = rng.sample(grouped[guid], min(per_member, remaining, len(grouped[guid])))
            picked[guid] = sorted(rows, key=lambda row: (row["ts"], row["id"]))
            remaining -= len(rows)
        return dict(sorted(picked.items()))


def _safe(text, ident):
    # WoW markup boundaries must not hide a real name in an invisible payload.
    raw = re.sub(r"\|([TH])", r"|\1 ", text)
    raw = screen_text(raw, ident)
    return screen_text(" ".join(strip_wow_markup(raw).split()), ident)


def _ids(raw):
    values = json.loads(raw)
    if not isinstance(values, list) or any(type(i) is not int or i <= 0 for i in values):
        raise ValueError("Invalid critic source memory IDs")
    return values


def _paired(store, line):
    return store.conn.execute("SELECT * FROM utterance WHERE guid=? AND channel=? AND text=? "
        "AND to_guid=? AND ts BETWEEN ? AND ? ORDER BY abs(ts-?),id LIMIT 1",
        (line["from_guid"], line["channel"], line["text"], line["to_guid"],
         line["ts"] - 2, line["ts"] + 2, line["ts"])).fetchone()


def _provenance(store, refs):
    """Full comparable provenance, with private rows allowed only inside this audit.

    Nested emitted chat includes its actual paired recalled memories; missing, rejected,
    cyclic or dropped output is not silently treated as a complete source.
    """
    saved, visiting, finished = {}, set(), set()
    stack = [(ref, False) for ref in reversed(tuple(dict.fromkeys(refs)))]
    while stack:
        ref, leaving = stack.pop()
        if leaving:
            visiting.remove(ref)
            finished.add(ref)
            continue
        if ref in visiting:
            raise ValueError("Cyclic critic provenance")
        if ref in finished:
            continue
        if not isinstance(ref, str) or len(saved) >= PROVENANCE_MAX:
            raise ValueError("Invalid critic provenance")
        node = _node(store, ref)
        if node is None:
            raise ValueError("Incomplete critic provenance")
        value, children, holder, scope, parties = node
        extras = ()
        if ref.startswith("chat:"):
            line = store.conn.execute("SELECT * FROM chat_line WHERE id=?",
                                      (int(ref.split(":", 1)[1]),)).fetchone()
            if line["origin"] == "mind":
                pair = _paired(store, line)
                if pair is None or pair["dropped"] is not None:
                    raise ValueError("Unpaired critic source output")
                children = (*children, *(f"memory:{i}" for i in _ids(pair["memory_ids"])))
                extras = (tuple(line), tuple(pair))
            else:
                extras = (tuple(line),)
        elif ref.startswith("joke:"):
            retained = store.get_meta(f"nightly:lexicon-sources:{ref.split(':', 1)[1]}") or "[]"
            origins = json.loads(retained)
            if not isinstance(origins, list) or any(not isinstance(r, str) for r in origins):
                raise ValueError("Invalid critic term provenance")
            children = (*children, *origins)
            extras = (retained,)
        if any(not isinstance(child, str) for child in children):
            raise ValueError("Invalid critic source references")
        saved[ref] = (value, tuple(children), holder, scope, tuple(sorted(parties)), extras)
        visiting.add(ref)
        stack.append((ref, True))
        stack.extend((child, False) for child in reversed(children))
    return tuple(sorted(saved.items()))


def _line(store, row):
    refs = tuple(f"memory:{ident}" for ident in _ids(row["memory_ids"]))
    echo = store.conn.execute("SELECT * FROM chat_line WHERE origin='mind' AND from_guid=? "
        "AND channel=? AND text=? AND to_guid=? AND ts BETWEEN ? AND ? "
        "ORDER BY abs(ts-?),id LIMIT 1", (row["guid"], row["channel"], row["text"], row["to_guid"],
        row["ts"] - 2, row["ts"] + 2, row["ts"])).fetchone()
    if echo is not None:
        pair = _paired(store, echo)
        if pair is None or pair["id"] != row["id"] or pair["dropped"] is not None:
            raise ValueError("Mismatched critic emitted source")
        refs = (f"chat:{echo['id']}", *refs)
    # Sent utterance rows are authoritative even when their bridge echo was not retained.
    return tuple(row), _provenance(store, refs), (f"utterance:{row['id']}", *refs)


def _current(ctx, guid, ids):
    store = ctx.store
    if guid == ctx.human or guid not in _members(store) or not ids or len(set(ids)) != len(ids):
        return None
    if store.conn.execute("SELECT count(*) FROM utterance WHERE guid=? AND dropped IS NULL "
            "AND ts>=? AND ts<?", (guid, *ctx.window)).fetchone()[0] < 3:
        return None
    rows = []
    for ident in ids:
        row = store.conn.execute("SELECT * FROM utterance WHERE id=?", (ident,)).fetchone()
        if (row is None or row["guid"] != guid or row["dropped"] is not None
                or not ctx.window[0] <= row["ts"] < ctx.window[1]):
            return None
        rows.append(row)
    member = store.cast()[guid]
    persona = load_persona(store, guid)  # bypass NightContext/PersonaCache after awaits
    card = render_prompt_card(persona, race=RACES.get(member.race, ""),
                              class_name=CLASSES.get(member.class_, ""))
    identity = (store.get_persona_json(guid), store.persona_versions()[guid], member.name,
                member.race, member.class_, member.level, store.get_meta("human_guid"))
    state = json.dumps(store.get_state(guid).self_notes, sort_keys=True)
    sources = tuple(_line(store, row) for row in rows)
    return (identity, card, state, sources), rows, card, member.name


def _flag_key(ident):
    return f"nightly:critic-flag-night:{ident}"


def _recent_flags(ctx, guid):
    first = (date.fromisoformat(ctx.night) - timedelta(days=6)).isoformat()
    rows = ledger_rows(ctx.store, guid)
    undone = {row["ref"] for row in rows if row["kind"] == "undo"}
    flags = []
    for row in rows:
        if row["kind"] != "drift_flag" or row["id"] in undone:
            continue
        night = ctx.store.get_meta(_flag_key(row["id"])) or night_of_ts(row["ts"], ctx.tz)
        if not first <= night <= ctx.night:
            continue
        if row["ref"] is not None:
            review = ctx.store.conn.execute("SELECT resolved FROM review_queue WHERE id=?",
                                            (row["ref"],)).fetchone()
            if review is not None and review["resolved"] is not None:
                continue
        flags.append(row)
    return flags


async def critic_member(ctx, guid, lines):
    unit = str(guid)
    if ctx.done(STAGE, unit):
        return
    ids = [row["id"] for row in lines]
    with ctx.store.transaction():
        before = _current(ctx, guid, ids)
        if before is None:
            ctx.skip(STAGE, unit)
            return
    snapshot, lines, card, name = before
    system = system_prompt("critic", PROMPT, ctx.settings.data_dir, human=ctx.ident,
                           name=_safe(name, ctx.ident), card=_safe(card, ctx.ident))
    user = "\n".join(f"{i}: {json.dumps(_safe(row['text'], ctx.ident), ensure_ascii=False)}"
                     for i, row in enumerate(lines))
    reply = await ctx.reasoner.json(system, user, CRITIC_SCHEMA)
    if ctx.done(STAGE, unit):
        return  # another accepted attempt may have finished while this one awaited
    check_schema(CRITIC_SCHEMA, reply)
    scores = reply["scores"]
    if len(scores) != len(lines) or sorted(score["i"] for score in scores) != list(range(len(lines))):
        raise ValueError("Critic must score every displayed index exactly once")
    scores = sorted(scores, key=lambda score: score["i"])

    def write(store):
        fresh = _current(ctx, guid, ids)
        if fresh is None or fresh[0] != snapshot:
            raise ValueError("Critic persona or emitted evidence changed during scoring")
        now = ctx.clock.now()
        store.conn.execute("INSERT INTO critic_score(night,guid,n,mean) VALUES (?,?,?,?)",
            (ctx.night, guid, len(scores), sum(score["score"] for score in scores) / len(scores)))
        for row, score, source in zip(lines, scores, snapshot[3]):
            if score["score"] > 2:
                continue
            text, reason = _safe(row["text"], ctx.ident), _safe(score["why"], ctx.ident)
            refs = source[2]
            review = enqueue_review(store, guid=guid, kind="drift_line", text=text,
                                    evidence=refs, reason=reason, now=now)
            flag = ledger_add(store, guid=guid, kind="drift_flag", text=text,
                              evidence=refs, ref=review, now=now)
            store.set_meta(_flag_key(flag), ctx.night)
        flags = _recent_flags(ctx, guid)
        if len(flags) >= 3:
            since = min(row["ts"] for row in flags)
            if any(note.get("added", 0) >= since for note in store.get_state(guid).self_notes):
                rollback_week(store, guid, since=since, now=now)

    ctx.commit(STAGE, unit, write)


def _plan_key(night):
    return f"nightly:critic-plan:{night}"


def _plan(ctx, *, freeze):
    raw = ctx.store.get_meta(_plan_key(ctx.night))
    if raw is not None:
        plan = json.loads(raw)
        if plan["window"] != list(ctx.window):
            raise ValueError("Critic sample window changed on resume")
        return plan["members"]
    picked = sample_lines(ctx.store, ctx.window, ctx.rng(STAGE, "sample"),
                          per_member=ctx.settings.critic_per_member, total=ctx.settings.critic_lines)
    members = {str(guid): [row["id"] for row in rows] for guid, rows in picked.items()}
    if freeze:
        ctx.store.set_meta(_plan_key(ctx.night), json.dumps({"window": list(ctx.window), "members": members}))
    return members


def count_pending(ctx):
    """Read-only count of unfinished frozen/prospective member calls for the budget."""
    with ctx.store.transaction():
        return sum(not ctx.done(STAGE, unit) for unit in _plan(ctx, freeze=False))


async def stage_critic(ctx):
    with ctx.store.transaction():
        plan = _plan(ctx, freeze=True)

    async def one(unit):
        rows = [ctx.store.conn.execute("SELECT * FROM utterance WHERE id=?", (ident,)).fetchone()
                for ident in plan[unit]]
        if any(row is None for row in rows):
            ctx.skip(STAGE, unit)
            return
        await critic_member(ctx, int(unit), rows)

    await ctx.map_units(STAGE, list(plan), one)
