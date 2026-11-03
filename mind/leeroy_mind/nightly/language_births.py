"""Public running jokes and one checked lexicon proposal per laughed-at moment."""
import json
import re

from leeroy_mind.nightly import lexicon
from leeroy_mind.nightly.audience import cast_names
from leeroy_mind.nightly.evidence import Evidence, load_facts, load_items
from leeroy_mind.nightly.ground import Claim, Verdict, check, log_verdicts
from leeroy_mind.nightly.llm import schema, system_prompt
from leeroy_mind.nightly.reactions import LAUGH_RE, laughs_at
from leeroy_mind.nightly.stories import find_moments
from leeroy_mind.personas.screens import redact_real_name
from leeroy_mind.text import strip_wow_markup, truncate_words

STAGE = "language"
JOKE_LAUGHS = 2
CANDIDATE_SCHEMA = schema("candidate", {
    "kind": {"type": "string", "enum": ["nickname", "joke", "none"]},
    "term": {"type": "string", "maxLength": 40},
    "for": {"type": ["string", "null"]},
    "evidence": {"type": "array", "minItems": 1, "items": {"type": "integer"}},
})
CANDIDATE_PROMPT = ("Here is a moment the guild laughed about. If a nickname or a running joke "
    "would really come out of it, give one, made only from what is here; otherwise answer none.")


def _public_sources(store, refs):
    """Close public provenance over the memories recalled by actual sent chat output.

    A chat row alone cannot vouch that an old generated line had no private input. Every
    nested chat source must be a real eligible line, and paired recalls join the snapshot.
    The returned line records also notice a dropped/changed pairing after a model await.
    """
    refs = tuple(dict.fromkeys(refs))
    lines = {}
    while True:
        snapshot = lexicon.source_snapshot(store, refs)
        if snapshot is None:
            return None
        additions = []
        for ref, _ in snapshot:
            if not ref.startswith("chat:") or ref in lines:
                continue
            row = lexicon.public_line(store, int(ref.split(":", 1)[1]))
            if row is None:
                return None
            lines[ref] = json.dumps(row, sort_keys=True)
            additions.extend(f"memory:{i}" for i in row["memory_ids"])
        expanded = tuple(dict.fromkeys((*refs, *additions)))
        if expanded == refs:
            return refs, snapshot, tuple(sorted(lines.items()))
        if len(expanded) > 5000:
            return None
        refs = expanded


def _laughs(store, line):
    """Eligible distinct others and the actual public replies proving their laughter."""
    raw = store.conn.execute("SELECT * FROM chat_line WHERE id=?", (line,)).fetchone()
    if raw is None:
        return (), ()
    allowed = laughs_at(store, line) & lexicon.public_laughers(store, line)
    replies = store.conn.execute("SELECT id,from_guid,text FROM chat_line WHERE channel=? "
        "AND ts>? AND ts<=? AND from_guid!=? ORDER BY ts,id",
        (raw["channel"], raw["ts"], raw["ts"] + 60, raw["from_guid"])).fetchall()
    found, refs = set(), []
    for row in replies:
        if row["from_guid"] not in allowed or not LAUGH_RE.search(row["text"]):
            continue
        if _public_sources(store, [f"chat:{row['id']}"]) is not None:
            found.add(row["from_guid"])
            refs.append(f"chat:{row['id']}")
    return tuple(sorted(found)), tuple(refs)


def find_jokes(store, window, *, limit=5):
    """Public lines with two distinct eligible laughers, most laughs first (ties by id)."""
    with store.transaction():
        rows = store.conn.execute("SELECT id FROM chat_line WHERE channel!='whisper' "
            "AND ts>=? AND ts<? ORDER BY ts,id", window).fetchall()
        found = []
        for row in rows:
            ident = row["id"]
            if _public_sources(store, [f"chat:{ident}"]) is None:
                continue
            laughers, _ = _laughs(store, ident)
            if len(laughers) >= JOKE_LAUGHS:
                found.append((ident, set(laughers)))
        found.sort(key=lambda pair: (-len(pair[1]), pair[0]))
        return found[:max(0, limit)]


def _evidence(ctx, refs):
    ev = Evidence()
    for item in load_items(ctx.store, refs, ident=ctx.ident):
        ev.add(item)
    return ev


def _clean(text):
    return " ".join(strip_wow_markup(text).split())


def _screen(ctx, kind, owner, raw, refs, ev, facts):
    """Screen raw input first: cleanup or truncation must not hide a prohibited suffix."""
    # WoW's texture/link opener is glued to its payload (|TName|t). Make only that
    # structural boundary visible to the ordinary case-sensitive real-name matcher;
    # retain the entire payload so stripping it cannot hide names or other invariants.
    screened_raw = re.sub(r"\|([TH])", r"|\1 ", raw)
    claim = Claim(kind, owner, screened_raw, list(refs))
    verdict = check(claim, ev, facts)
    if not verdict.ok:
        return claim, verdict
    claim = Claim(kind, owner, _clean(raw), list(refs))
    return claim, check(claim, ev, facts)


def jokes(ctx):
    """The deterministic unit's writes; join the caller's atomic unit transaction."""
    def write(store):
        facts = load_facts(ctx)
        for ident, laughers in find_jokes(store, ctx.window, limit=ctx.settings.jokes_per_night):
            origin = f"chat:{ident}"
            if store.conn.execute("SELECT 1 FROM lexicon WHERE origin_ref=?", (origin,)).fetchone():
                continue
            line = lexicon.public_line(store, ident)
            if line is None:
                continue
            _, laugh_refs = _laughs(store, ident)
            sources = _public_sources(store, [origin, *laugh_refs])
            if sources is None:
                continue
            ev = _evidence(ctx, [origin])
            claim, verdict = _screen(ctx, "joke", line["from_guid"], line["text"], [origin], ev, facts)
            if verdict.ok and not lexicon.era_ok(claim.text):
                verdict = Verdict(False, "deterministic", "modern slang")
            log_verdicts(store, ctx.night, STAGE, [(claim, verdict)], ctx.clock.now(), ctx.ident)
            if not verdict.ok:
                continue
            term = truncate_words(redact_real_name(claim.text, ctx.ident, ctx.ident.called), 120)
            lexicon.create_term(store, ctx, kind="joke", term=term, origin_ref=origin,
                originator=line["from_guid"], adopters={line["from_guid"], *laughers},
                now=ctx.clock.now(), refs=sources[0])

    with ctx.store.transaction():
        write(ctx.store)


def _pack(moment):
    return {"key": moment.key, "owners": sorted(moment.owners),
            "memory_ids": moment.memory_ids, "refs": moment.refs}


def _sources(ctx, moment):
    """All shown inputs, source pairings, laughter, and membership for a single proposal."""
    cast = set(cast_names(ctx.store))
    if len(moment["owners"]) < 3 or not set(moment["owners"]) <= cast:
        return None
    chats = [ref for ref in moment["refs"] if ref.startswith("chat:")]
    if not chats:
        return None
    origin = lexicon.public_line(ctx.store, int(chats[0].split(":", 1)[1]))
    if origin is None:
        return None
    laughed = []
    for ref in chats:
        people, replies = _laughs(ctx.store, int(ref.split(":", 1)[1]))
        if people:
            laughed.append((ref, people, replies))
    if not laughed:
        return None
    shown = tuple(dict.fromkeys([*(f"memory:{i}" for i in moment["memory_ids"]),
                                *moment["refs"],
                                *(ref for _, _, replies in laughed for ref in replies)]))
    sources = _public_sources(ctx.store, shown)
    if sources is None:
        return None
    return {"shown": shown, "sources": sources, "origin": chats[0],
            "originator": origin["from_guid"], "adopters": tuple(moment["owners"]),
            "laughs": tuple(laughed)}


def _eligible(ctx):
    return [packed for m in find_moments(ctx.store, ctx.window)
            if _sources(ctx, packed := _pack(m)) is not None]


def _plan_key(ctx):
    return f"nightly:language-candidates:{ctx.night}"


def _plan(ctx):
    saved = ctx.store.get_meta(_plan_key(ctx))
    return json.loads(saved) if saved is not None else _eligible(ctx)[:max(0, ctx.settings.lexicon_candidates)]


def _unit(moment):
    return f"candidate:{moment['key']}"


def _current(ctx, moment):
    current = next((_pack(m) for m in find_moments(ctx.store, ctx.window) if m.key == moment["key"]), None)
    return _sources(ctx, current) if current == moment else None


def count_candidates(ctx):
    """Pending eligible capped model proposals, without creating the durable night plan."""
    with ctx.store.transaction():
        if ctx.done(STAGE, "candidates"):
            return 0
        return sum(not ctx.done(STAGE, _unit(m)) and _current(ctx, m) is not None for m in _plan(ctx))


async def _candidate(ctx, moment):
    unit = _unit(moment)
    with ctx.store.transaction():
        before = _current(ctx, moment)
        if before is None:
            ctx.skip(STAGE, unit)
            return
        ev = _evidence(ctx, before["shown"])
        facts = load_facts(ctx)
    system = system_prompt("candidate", CANDIDATE_PROMPT, ctx.settings.data_dir, human=ctx.ident)
    user = (f"Evidence:\n{ev.render(ctx.tz)}\nUse the bracketed evidence aliases. "
            "For a nickname, name its member or human target in 'for' and use at most 24 characters.")
    reply = await ctx.reasoner.json(system, user, CANDIDATE_SCHEMA)
    refs = list(dict.fromkeys(ev.ref(a) for a in reply["evidence"]))
    target = 0
    claim = verdict = None
    if reply["kind"] != "none":
        claim, verdict = _screen(ctx, "candidate", before["originator"], reply["term"], refs, ev, facts)
        if verdict.ok and not lexicon.era_ok(claim.text):
            verdict = Verdict(False, "deterministic", "modern slang")
        if reply["kind"] == "nickname" and verdict.ok:
            target = facts.names.get(_clean(reply["for"] or "").lower(), 0)
            actors = set().union(*(ev[ref].actors for ref in refs if ref in ev))
            if target not in actors or target not in {*cast_names(ctx.store), ctx.human}:
                verdict = Verdict(False, "deterministic", "nickname target not in evidence")
            elif len(claim.text) > 24:
                verdict = Verdict(False, "deterministic", "nickname too long")

    def write(store):
        if _current(ctx, moment) != before:
            raise ValueError("Lexicon candidate evidence changed while it was generated")
        if claim is None:
            return
        log_verdicts(store, ctx.night, STAGE, [(claim, verdict)], ctx.clock.now(), ctx.ident)
        if verdict.ok:
            lexicon.create_term(store, ctx, kind=reply["kind"], term=claim.text, target=target,
                origin_ref=before["origin"], originator=before["originator"],
                adopters=before["adopters"], now=ctx.clock.now(), refs=before["sources"][0])

    ctx.commit(STAGE, unit, write)


async def candidates(ctx):
    """Freeze capped moments once; rejected/none proposals finish, failed units retry."""
    with ctx.store.transaction():
        if ctx.done(STAGE, "candidates"):
            return
        plan = _plan(ctx)
        if ctx.store.get_meta(_plan_key(ctx)) is None:
            ctx.store.set_meta(_plan_key(ctx), json.dumps(plan))
    by_unit = {_unit(m): m for m in plan}

    async def one(unit):
        await _candidate(ctx, by_unit[unit])

    await ctx.map_units(STAGE, list(by_unit), one)
    if all(ctx.done(STAGE, unit) for unit in by_unit) and not ctx.done(STAGE, "candidates"):
        ctx.skip(STAGE, "candidates")
