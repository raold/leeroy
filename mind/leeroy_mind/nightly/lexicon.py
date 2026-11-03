"""Shared lexicon state: public origins, adoption, remembered variants and quiet years."""
import json
import re
from datetime import date

from leeroy_mind.nightly import knowledge
from leeroy_mind.nightly.audience import audience_of, cast_names
from leeroy_mind.nightly.evidence import Evidence, load_facts, load_items
from leeroy_mind.nightly.ground import Claim, check, log_verdicts
from leeroy_mind.nightly.motives import trait_today
from leeroy_mind.nightly.reactions import LAUGH_RE
from leeroy_mind.nightly.settings import night_of_ts
from leeroy_mind.nightly.stories import PROVENANCE_MAX, _public_node
from leeroy_mind.personas.io import load_persona
from leeroy_mind.personas.screens import has_word, names_real_name
from leeroy_mind.storage.rows import MemoryRow
from leeroy_mind.text import has_meta, strip_wow_markup, truncate_words, word_jaccard

JOKE_LAUGHS = 2
MUTATE_JACCARD = 0.6
ADOPT_BASE, ADOPT_PRESTIGE, ADOPT_AFFINITY, ADOPT_AGREE, ADOPT_TENURE = 0.10, 0.20, 0.20, 0.10, 0.10
ADOPT_RANGE = (0.02, 0.60)
HEAT_DECAY = 0.85
DORMANT_HEAT = 0.5
DORMANT_DAYS = 21
MODERN_SLANG = ("rizz", "sus", "no cap", "bussin", "yeet", "stan", "lowkey", "highkey", "slay", "based", "ratio", "goated", "vibe check", "fr fr", "ong", "delulu", "skibidi", "gyat", "main character", "touch grass", "it's giving", "rent free")
_JOKE = re.compile(r"joke:([0-9]+)")
_MEMORY = re.compile(r"memory:([0-9]+)")


def _text(value):
    return " ".join(strip_wow_markup(value).split())


def era_ok(term):
    return not any(has_word(_text(term), phrase) for phrase in MODERN_SLANG)


def prestige(store, guid, human):
    if guid == human:
        return 1.0
    p = load_persona(store, guid)
    return 1.0 if p and (p.play.guild_role == "officer" or "raid lead" in p.play.officer_title.casefold()) else 0.3


def adopt_chance(prestige, affinity, agreeableness, years_played):
    return max(ADOPT_RANGE[0], min(ADOPT_RANGE[1], ADOPT_BASE + ADOPT_PRESTIGE * prestige
        + ADOPT_AFFINITY * max(0, affinity) / 100 + ADOPT_AGREE * (agreeableness - 5.5) / 4.5
        - ADOPT_TENURE * min(1, years_played / 10)))


def _list(text):
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, list) else None


def _term(store, ident):
    return store.conn.execute("SELECT * FROM lexicon WHERE id=?", (ident,)).fetchone()


def _origins(store, term):
    saved = _list(store.get_meta(f"nightly:lexicon-sources:{term['id']}") or "[]")
    if saved is None or any(not isinstance(ref, str) for ref in saved):
        return None
    return tuple(dict.fromkeys((term["origin_ref"], *saved)))


def source_snapshot(store, refs):
    """Task18-style complete public snapshot, additionally following lexicon origins."""
    refs = tuple(refs)
    if any(not isinstance(ref, str) for ref in refs):
        return None
    with store.transaction():
        saved, visiting, done = {}, set(), set()
        stack = [(ref, False) for ref in reversed(tuple(dict.fromkeys(refs)))]
        durable = False
        while stack:
            ref, leaving = stack.pop()
            if leaving:
                visiting.remove(ref)
                done.add(ref)
                continue
            if ref in visiting:
                return None
            if ref in done:
                continue
            if len(saved) >= PROVENANCE_MAX:
                return None
            match = _JOKE.fullmatch(ref)
            if match:
                term = _term(store, int(match[1]))
                if (term is None or term["status"] == "vetoed" or knowledge.is_secret(store, ref)
                        or knowledge.is_private(store, ref)):
                    return None
                children = _origins(store, term)
                if children is None:
                    return None
                value = (term["id"], term["kind"], term["term"], term["target"], term["origin_ref"],
                         term["originator"], term["status"], children)
            else:
                node = _public_node(store, ref)
                if node is None:
                    return None
                value, children, original = node
                durable = durable or original
            if any(not isinstance(child, str) for child in children):
                return None
            saved[ref] = value
            visiting.add(ref)
            stack.append((ref, True))
            stack.extend((child, False) for child in reversed(children))
        return tuple(sorted(saved.items())) if durable else None


def public_line(store, row_or_id, *, human=None):
    """A real public line, with sent-output attribution and its recalled memory IDs.

    Game-origin cast chat and actual human lines need no mind utterance. Mind output
    uses the closest paired utterance, including dropped rows before selecting it.
    """
    human = int(store.get_meta("human_guid") or 0) if human is None else human
    ident = row_or_id if isinstance(row_or_id, int) else row_or_id["id"]
    row = store.conn.execute("SELECT * FROM chat_line WHERE id=?", (ident,)).fetchone()
    if row is None or row["channel"] == "whisper":
        return None
    author = row["from_guid"]
    if author == human:
        if row["origin"] != "human":
            return None
    elif author not in cast_names(store) or row["origin"] not in {"mind", "game"}:
        return None
    utterance, memories = None, ()
    if row["origin"] == "mind":
        utterance = store.conn.execute("SELECT * FROM utterance WHERE guid=? AND channel=? AND text=? "
            "AND to_guid=? AND ts BETWEEN ? AND ? ORDER BY abs(ts-?),id LIMIT 1",
            (author, row["channel"], row["text"], row["to_guid"], row["ts"] - 2,
             row["ts"] + 2, row["ts"])).fetchone()
        if utterance is None or utterance["dropped"] is not None:
            return None
        ids = _list(utterance["memory_ids"])
        if ids is None or any(type(i) is not int or i <= 0 for i in ids):
            return None
        memories = tuple(ids)
    refs = (f"chat:{ident}", *(f"memory:{i}" for i in memories))
    if source_snapshot(store, refs) is None:
        return None
    return dict(row) | {"utterance_id": None if utterance is None else utterance["id"],
                       "memory_ids": memories, "refs": refs}


def public_laughers(store, line_id):
    line = public_line(store, line_id)
    if line is None:
        return set()
    rows = store.conn.execute("SELECT id,text,from_guid FROM chat_line WHERE channel=? "
        "AND ts>? AND ts<=? AND from_guid!=? ORDER BY ts,id",
        (line["channel"], line["ts"], line["ts"] + 60, line["from_guid"])).fetchall()
    return {r["from_guid"] for r in rows if LAUGH_RE.search(r["text"])
            and public_sources(store, (f"chat:{r['id']}",)) is not None}


def public_sources(store, refs):
    """Close actual origins over every nested chat's paired recalled memories."""
    refs = list(dict.fromkeys(refs))
    checked = set()
    while len(refs) <= PROVENANCE_MAX:
        snapshot = source_snapshot(store, refs)
        if snapshot is None:
            return None
        chats = [ref for ref, _ in snapshot if ref.startswith("chat:") and ref not in checked]
        if not chats:
            return tuple(refs)
        for ref in chats:
            checked.add(ref)
            line = public_line(store, int(ref.split(":")[1]))
            if line is None:
                return None
            refs.extend(r for r in line["refs"] if r not in refs)
    return None


def memory_key(term):
    return (f"nick:{term['target']}" if term["kind"] == "nickname" else
            f"slang:{term['term']}" if term["kind"] == "slang" else f"joke:{term['id']}")


def _matches(memory, term):
    if memory is None or memory.key != memory_key(term):
        return False
    tagged = [ref for ref in memory.evidence if _JOKE.fullmatch(ref)]
    if tagged:
        return tagged[0] == f"joke:{term['id']}"
    # Task11 coined nickname memories predate joke-ref tags. Match the actual term,
    # not every nickname sharing this target's key.
    return term["kind"] == "nickname" and f"'{term['term']}'" in memory.text


def _archive(store, term, guid):
    memory = store.keyed_memory(guid, memory_key(term))
    if _matches(memory, term):
        store.conn.execute("UPDATE memory SET status='archived' WHERE id=?", (memory.id,))


def _safe_refs(store, term_id, refs):
    """Expand prior copies of this term before keyed rewrites can make them cyclic."""
    self_ref = f"joke:{term_id}"
    output, seen = [], set()
    pending = list(reversed(tuple(refs)))
    while pending:
        ref = pending.pop()
        if ref in seen or ref == self_ref:
            continue
        seen.add(ref)
        snapshot = source_snapshot(store, [ref])
        if snapshot is None:
            return None
        if self_ref in dict(snapshot):
            memory, joke = _MEMORY.fullmatch(ref), _JOKE.fullmatch(ref)
            if memory:
                children = store.get_memories([int(memory[1])])[0].evidence
            elif joke:
                children = _origins(store, _term(store, int(joke[1])))
            else:
                node = _public_node(store, ref)
                children = node[1] if node is not None else ()
            pending.extend(reversed(children))
        else:
            output.append(ref)
    return tuple(output)


def _save_sources(store, term, refs):
    current = _origins(store, term)
    complete = None if current is None else public_sources(store, (*current, *refs))
    if complete is None:
        return False
    safe = _safe_refs(store, term["id"], complete)
    if not safe:
        return False
    store.set_meta(f"nightly:lexicon-sources:{term['id']}", json.dumps(safe))
    return True


def refresh_term(store, ctx, term_id, *, now, refs=()):
    """Rewrite active holders' kind-specific memories; never create a human copy."""
    term = _term(store, term_id)
    if term is None or term["status"] != "active" or not _save_sources(store, term, refs):
        return
    evidence = (f"joke:{term_id}", *_origins(store, term))
    holders = store.conn.execute("SELECT * FROM lexicon_adopter WHERE term=? AND active=1 ORDER BY guid",
                                 (term_id,)).fetchall()
    eligible = set(ctx.members(include_away=True))
    for holder in holders:
        guid = holder["guid"]
        if guid == ctx.human or guid not in eligible:
            continue
        existing = store.keyed_memory(guid, memory_key(term))
        if existing is not None and existing.status == "active" and not _matches(existing, term):
            competing = store.conn.execute("SELECT l.* FROM lexicon l JOIN lexicon_adopter a ON a.term=l.id "
                "WHERE a.guid=? AND a.active=1 AND l.status='active' AND l.id!=?", (guid, term_id)).fetchall()
            if any(_matches(existing, other) for other in competing):
                continue
        via = holder["via"] or term["originator"]
        if via not in eligible | {ctx.human}:
            continue
        if term["kind"] == "joke":
            text, kind, importance, valence = f"Running joke: {term['term']}", "story", 5, 0.5
            actors = [h["guid"] for h in holders if h["guid"] != guid][:8]
        elif term["kind"] == "nickname":
            name = ctx.name(term["target"])
            text = (f"Everyone calls {name} '{term['term']}' now." if len(holders) >= 3
                    else f"I call {name} '{term['term']}'.")
            kind, importance, valence, actors = "reflection", 4, 0, [term["target"]]
        else:
            text = f"Picked up '{term['term']}' from {ctx.name(via)}."
            kind, importance, valence, actors = "reflection", 3, 0, [via]
        ctx.writer.put(MemoryRow(id=None, owner=guid, kind=kind, text=truncate_words(text, 200),
            t_event=int(now), t_created=int(ctx.clock.now()), t_accessed=int(now), access_n=0,
            actors=tuple(actors), place=None, boss_id=None, raid_id=None, importance=importance,
            valence=valence, source="chat", evidence=evidence, key=memory_key(term)), shown=evidence[1:])


def adopt(store, ctx, term_id, guid, *, via, now, refs=(), use=False):
    """Join an active term; True includes existing holders and human bookkeeping."""
    term = _term(store, term_id)
    members = set(ctx.members(include_away=True)) | {ctx.human}
    if (term is None or term["status"] != "active" or guid not in members or via not in members
            or (guid != ctx.human and ctx.settings.adopt_max <= 0)
            or not _save_sources(store, term, refs)):
        return False
    # Older vetoes left adopter flags set. They must not reserve attention forever.
    store.conn.execute("UPDATE lexicon_adopter SET active=0 WHERE guid=? AND term IN "
        "(SELECT id FROM lexicon WHERE status='vetoed')", (guid,))
    if term["kind"] == "nickname":
        conflicts = store.conn.execute("SELECT l.* FROM lexicon l JOIN lexicon_adopter a ON a.term=l.id "
            "WHERE a.guid=? AND a.active=1 AND l.kind='nickname' AND l.target=? AND l.id!=?",
            (guid, term["target"], term_id)).fetchall()
        for old in conflicts:
            store.conn.execute("UPDATE lexicon_adopter SET active=0 WHERE term=? AND guid=?", (old["id"], guid))
            _archive(store, old, guid)
    store.conn.execute("INSERT INTO lexicon_adopter(term,guid,first_use,uses,via,active) VALUES (?,?,?,?,?,1) "
        "ON CONFLICT(term,guid) DO UPDATE SET active=1,uses=lexicon_adopter.uses+excluded.uses,"
        "via=CASE WHEN lexicon_adopter.active=0 THEN excluded.via ELSE lexicon_adopter.via END",
        (term_id, guid, now, int(use), via))
    if guid != ctx.human:
        active = store.conn.execute("SELECT term FROM lexicon_adopter WHERE guid=? AND active=1 "
            "ORDER BY uses,first_use,term", (guid,)).fetchall()
        for old in active[:max(0, len(active) - ctx.settings.adopt_max)]:
            store.conn.execute("UPDATE lexicon_adopter SET active=0 WHERE term=? AND guid=?", (old["term"], guid))
            prior = _term(store, old["term"])
            if prior is not None:
                _archive(store, prior, guid)
    refresh_term(store, ctx, term_id, now=now)
    return True


def create_term(store, ctx, *, kind, term, target=0, origin_ref, originator, adopters=(), now, refs=()):
    """Create once; an active duplicate reuses its ID, a vetoed/dormant one returns None.

    Empty adopters really means empty: callers decide whether the originator adopts.
    Every source must already be screened and public; this helper repeats core gates.
    """
    raw = re.sub(r"\|([TH])", r"|\1 ", term)
    if has_meta(raw) or names_real_name(raw, ctx.ident):
        return None
    term = _text(term)
    members = set(ctx.members(include_away=True)) | {ctx.human}
    if (kind not in {"joke", "nickname", "slang"} or not term or len(term) > (24 if kind == "nickname" else 120)
            or not era_ok(term) or names_real_name(term, ctx.ident) or originator not in members
            or (kind == "nickname" and has_word(term, ctx.ident.real_name))
            or (kind == "nickname" and target not in members)
            or (kind != "nickname" and target != 0)
            or public_sources(store, (origin_ref, *refs)) is None):
        return None
    old = store.conn.execute("SELECT * FROM lexicon WHERE kind=? AND term=? AND target=?",
                             (kind, term, target)).fetchone()
    if old is not None and old["status"] != "active":
        return None
    if old is None:
        ident = store.conn.execute("INSERT INTO lexicon(kind,term,target,origin_ref,originator,born,last_used) "
            "VALUES (?,?,?,?,?,?,?)", (kind, term, target, origin_ref, originator, now, now)).lastrowid
        store.set_meta(f"nightly:lexicon-born-night:{ident}", ctx.night)
    else:
        ident = old["id"]
    if not _save_sources(store, _term(store, ident), (origin_ref, *refs)):
        return None
    for guid in dict.fromkeys(adopters):
        adopt(store, ctx, ident, guid, via=originator, now=now, refs=refs)
    return ident


def _validate_variant(ctx, line, term):
    refs = list(line["refs"])
    ev = Evidence()
    for item in load_items(ctx.store, refs, ident=ctx.ident):
        ev.add(item)
    text = _text(line["text"])
    facts = load_facts(ctx)
    results = []
    for value in dict.fromkeys((re.sub(r"\|([TH])", r"|\1 ", line["text"]), text)):
        claim = Claim("joke", line["from_guid"], value, [f"chat:{line['id']}"])
        verdict = check(claim, ev, facts)
        results.append((claim, verdict))
    log_verdicts(ctx.store, ctx.night, "language", results, ctx.clock.now(), ctx.ident)
    return truncate_words(text, 120) if all(v.ok for _, v in results) and era_ok(text) else None


def spread(ctx):
    """One frozen attribution per public line, with rule-driven conversions and variants."""
    store, now = ctx.store, ctx.clock.now()
    rows = store.conn.execute("SELECT id FROM chat_line WHERE channel!='whisper' AND ts>=? AND ts<? "
                              "ORDER BY ts,id", ctx.window).fetchall()
    for raw in rows:
        receipt = f"nightly:lexicon-line:{raw['id']}"
        if store.get_meta(receipt) is not None:
            continue
        line = public_line(store, raw["id"], human=ctx.human)
        if line is None or (line["from_guid"] != ctx.human and line["utterance_id"] is None):
            continue
        complete = public_sources(store, line["refs"])
        if complete is None:
            continue
        line["refs"] = complete
        recalled = {int(match[1]) for memory in store.get_memories(line["memory_ids"])
                    for ref in (*memory.evidence, memory.key or "") if (match := _JOKE.fullmatch(ref))}
        terms = store.conn.execute("SELECT * FROM lexicon WHERE status IN ('active','dormant') ORDER BY id").fetchall()
        used = []
        for term in terms:
            ident = term["id"]
            cited_joke = term["kind"] == "joke" and ident in recalled
            if not has_word(line["text"], term["term"]) and not cited_joke:
                continue
            if source_snapshot(store, (f"joke:{ident}", *line["refs"])) is None:
                continue
            if term["status"] == "dormant":
                store.conn.execute("UPDATE lexicon SET status='active' WHERE id=?", (ident,))
            if not adopt(store, ctx, ident, line["from_guid"], via=line["from_guid"], now=line["ts"],
                         refs=line["refs"], use=True):
                if term["status"] == "dormant":
                    store.conn.execute("UPDATE lexicon SET status='dormant' WHERE id=?", (ident,))
                continue
            store.conn.execute("UPDATE lexicon SET uses=uses+1,last_used=? WHERE id=?", (line["ts"], ident))
            key = f"nightly:lexicon-uses:{ctx.night}:{ident}"
            store.set_meta(key, str(int(store.get_meta(key) or 0) + 1))
            used.append(ident)
            if term["kind"] == "joke" and word_jaccard(line["text"], term["term"]) < MUTATE_JACCARD and public_laughers(store, line["id"]):
                variant = _validate_variant(ctx, line, term)
                if variant is not None and not store.conn.execute("SELECT 1 FROM lexicon WHERE kind='joke' "
                        "AND term=? AND target=0 AND id!=?", (variant, ident)).fetchone():
                    store.conn.execute("UPDATE lexicon SET term=? WHERE id=?", (variant, ident))
                    refresh_term(store, ctx, ident, now=now, refs=line["refs"])
            for hearer in sorted(audience_of(store, line["id"])):
                if hearer == ctx.human or store.conn.execute("SELECT 1 FROM lexicon_adopter "
                        "WHERE term=? AND guid=? AND active=1", (ident, hearer)).fetchone():
                    continue
                p = ctx.persona(hearer)
                chance = adopt_chance(prestige(store, line["from_guid"], ctx.human),
                    store.get_relationship(hearer, line["from_guid"]).affinity,
                    trait_today(store, ctx.night, hearer, p)["agreeableness"], p.game.years_played)
                if ctx.rng("adopt", ident, hearer, line["id"]).random() < chance:
                    adopt(store, ctx, ident, hearer, via=line["from_guid"], now=line["ts"], refs=line["refs"])
        store.set_meta(receipt, json.dumps(used))


def fade(ctx):
    store, now = ctx.store, ctx.clock.now()
    for term in store.conn.execute("SELECT * FROM lexicon WHERE status!='vetoed' ORDER BY id").fetchall():
        key = f"nightly:lexicon-heat:{term['id']}"
        state = json.loads(store.get_meta(key) or "null")
        previous = (state["night"] if state else
                    store.get_meta(f"nightly:lexicon-born-night:{term['id']}")
                    or night_of_ts(term["born"], ctx.tz))
        nights = (date.fromisoformat(ctx.night) - date.fromisoformat(previous)).days
        if nights < 0:
            continue
        uses = int(store.get_meta(f"nightly:lexicon-uses:{ctx.night}:{term['id']}") or 0)
        added = state["uses"] if state and nights == 0 else 0
        heat = term["heat"] * HEAT_DECAY ** nights + max(0, uses - added)
        status = term["status"]
        if status == "active" and heat < DORMANT_HEAT and term["last_used"] < now - DORMANT_DAYS * 86400:
            status = "dormant"
        store.conn.execute("UPDATE lexicon SET heat=?,status=? WHERE id=?", (heat, status, term["id"]))
        if status == "dormant":
            for holder in store.conn.execute("SELECT guid FROM lexicon_adopter WHERE term=?", (term["id"],)).fetchall():
                _archive(store, term, holder["guid"])
        store.set_meta(key, json.dumps({"night": ctx.night, "uses": uses}))


def veto(store, term_id, *, now):
    with store.transaction():
        term = _term(store, term_id)
        if term is None or term["status"] == "vetoed":
            return False
        store.conn.execute("UPDATE lexicon SET status='vetoed' WHERE id=?", (term_id,))
        store.conn.execute("UPDATE lexicon_adopter SET active=0 WHERE term=?", (term_id,))
        copies = store.conn.execute("SELECT id FROM memory WHERE key=?", (memory_key(term),)).fetchall()
        for memory in store.get_memories([copy["id"] for copy in copies]):
            if _matches(memory, term):
                store.conn.execute("UPDATE memory SET status='archived' WHERE id=?", (memory.id,))
        return True
