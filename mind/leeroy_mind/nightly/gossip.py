"""Bounded retellings and imperfect corrections, inside an accepted scene's transaction."""
import random

from leeroy_mind.memory.writer import clean_memory_text
from leeroy_mind.nightly import knowledge
from leeroy_mind.nightly.evidence import Evidence, load_facts, load_items
from leeroy_mind.nightly.ground import Claim, check, log_verdicts
from leeroy_mind.nightly.motives import trait_today
from leeroy_mind.nightly.relations import apply_capped
from leeroy_mind.storage.rows import MemoryRow
from leeroy_mind.text import strip_wow_markup, truncate_words

OPS = ("level", "sharpen", "assimilate")
OP_WORDS = {
    "level": "leaving out the details",
    "sharpen": "making one detail a bit bigger",
    "assimilate": "bending it toward what {hearer} already thinks of {about}",
}
TELL_MIN = 0.3
MAX_TELLS = 3
CORRECTION_RESIDUE = 0.5
FADE_DAYS = 14
HOPS_MAX = 3
DISCRETION_BASE = 0.8


def choose_op(teller_traits, hearer_affinity_to_about) -> str:
    if abs(hearer_affinity_to_about) >= 30:
        return "assimilate"
    if teller_traits["extraversion"] >= 7 or teller_traits["neuroticism"] >= 7:
        return "sharpen"
    return "level"


def tell_score(importance, confidence, teller_traits) -> float:
    return (importance / 10 * (1.5 - confidence)
            * (0.5 + (teller_traits["extraversion"] + teller_traits["neuroticism"]) / 20))


def discretion(p, traits_today) -> float:
    return max(0.2, min(0.95, DISCRETION_BASE
                       + 0.05 * (traits_today["conscientiousness"] - 5.5)
                       - 0.05 * (traits_today["extraversion"] - 5.5)))


def discretion_roll(teller, root) -> float:
    return random.Random(f"discretion|{teller}|{root}").random()


def _privacy(store, memory):
    # One graph walk is shared with scene visibility and bystander selection.
    from leeroy_mind.nightly.encounters import _privacy as inspect
    return inspect(store, memory)


def _traits(ctx, teller):
    return trait_today(ctx.store, ctx.night, teller, ctx.persona(teller))


def _disclosable(ctx, memory, teller, *, state=None, historical=False) -> bool:
    complete, scopes, roots, secret, private = state or _privacy(ctx.store, memory)
    statuses = {"active", "superseded"} if historical else {"active"}
    if not complete or memory.status not in statuses or secret:
        return False
    ancestors = ctx.store.get_memories([ident for ident, _, _ in scopes if ident != memory.id])
    if any(m.status not in {"active", "superseded"} for m in ancestors):
        return False
    if any(scope == "secret" for _, scope, _ in scopes):
        return False
    # A fresh witnessed memory may precede stage_knows. Only an actual prohibition
    # blocks it; an absent ledger row is not a prohibition.
    if any(ctx.store.conn.execute("SELECT 1 FROM knows WHERE root=? AND guid=? AND may_tell=0",
                                  (root, teller)).fetchone() for root in roots):
        return False
    if private or any(scope == "private" for _, scope, _ in scopes):
        if not private:
            return False  # no original whisper whose participants authorize this copy
        for ident, scope, _ in scopes:
            if scope == "private":
                own = memory if ident == memory.id else ctx.store.get_memories([ident])[0]
                if not _privacy(ctx.store, own)[4]:
                    # One authorized whisper must not lend its permission to an
                    # unrelated scope-only private branch of the same summary.
                    return False
        threshold = discretion(ctx.persona(teller), _traits(ctx, teller))
        if any(teller not in knowledge.parties(ctx.store, root)
               or discretion_roll(teller, root) < threshold for root in private):
            return False
    return True


def source_root(store, memory) -> str | None:
    """Keep a retelling on its original root, ahead of its later scene provenance."""
    state = _privacy(store, memory)
    if not state[0]:
        return None
    roots = set(state[2])
    existing = [r["root"] for r in store.conn.execute("SELECT root FROM rumor ORDER BY id")
                if r["root"] in roots]
    if existing:
        return existing[0] if len(existing) == 1 else None
    seen = set()

    def first(row):
        if row.id in seen:
            return None
        seen.add(row.id)
        # A hearsay copy cites the source memory before the new encounter. Follow the
        # source first even when another caller stored those refs in a different order.
        ids = [int(r.split(":", 1)[1]) for r in row.evidence
               if r.startswith("memory:") and r.split(":", 1)[1].isdecimal()]
        for child in store.get_memories(ids):
            found = first(child)
            if found is not None:
                return found
        return next((r for r in row.evidence if r in roots), None)

    return first(memory)


def source_allowed(ctx, memory, teller, hearer, *, require_score=True) -> bool:
    """The same eligibility gate is used before prompting and before committing."""
    if not teller or not hearer or teller == hearer or memory.owner != teller:
        return False
    if not _disclosable(ctx, memory, teller):
        return False
    root = source_root(ctx.store, memory)
    if root is None or knowledge.hops(ctx.store, root, teller) >= HOPS_MAX:
        return False
    rumor = ctx.store.conn.execute("SELECT id,status FROM rumor WHERE root=?", (root,)).fetchone()
    if rumor is not None:
        if rumor["status"] != "live":
            return False
        state = ctx.store.conn.execute("SELECT state,tells FROM rumor_state WHERE rumor=? AND guid=?",
                                        (rumor["id"], teller)).fetchone()
        if state and (state["state"] == "stifler" or state["tells"] >= MAX_TELLS):
            return False
    return not require_score or tell_score(memory.importance, memory.confidence,
                                           _traits(ctx, teller)) >= TELL_MIN


def rumor_offer(ctx, enc):
    """A comparable snapshot of an intention's version and its complete source evidence."""
    enc = dict(enc)
    kind, ref = enc.get("volition"), enc.get("ref", "")
    if kind not in {"gossip", "correct"} or not ref.startswith("rule:rumor:"):
        return None
    number = ref.removeprefix("rule:rumor:")
    if not number.isdecimal():
        return None
    store, teller, hearer = ctx.store, enc["a"], enc["b"]
    rumor = store.conn.execute("SELECT * FROM rumor WHERE id=? AND status='live'", (int(number),)).fetchone()
    if rumor is None or knowledge.is_secret(store, rumor["root"]):
        return None
    holder = teller if kind == "gossip" else hearer
    state = store.conn.execute("SELECT * FROM rumor_state WHERE rumor=? AND guid=?",
                                (rumor["id"], holder)).fetchone()
    if state is None or (kind == "gossip" and (state["state"] != "spreader"
                                               or state["tells"] >= MAX_TELLS)):
        return None
    version = store.conn.execute("SELECT * FROM rumor_version WHERE id=? AND rumor=?",
                                  (state["version"], rumor["id"])).fetchone()
    if version is None or (kind == "correct" and version["op"] == "correct"):
        return None
    ids = tuple(dict.fromkeys((rumor["source"], version["source"])))
    sources = tuple(store.get_memories(ids))
    # An earlier hearer's correction supersedes their local copy; it cannot erase
    # the provenance of the version somebody else still holds. Rejected/archived
    # rows remain unavailable, and historical rows retain every privacy check.
    if len(sources) != len(ids) or any(not _disclosable(ctx, m, teller, historical=True)
                                      for m in sources):
        return None
    if any(rumor["root"] not in _privacy(store, m)[2] for m in sources):
        return None
    if kind == "gossip":
        # version.source belongs to the previous teller; the current teller repeats
        # their own held copy, which retrieval need not happen to have returned.
        owned = store.conn.execute("SELECT id FROM memory WHERE owner=? AND status='active' "
                                   "ORDER BY t_created DESC,id DESC", (teller,)).fetchall()
        candidates = [m for m in store.get_memories([r["id"] for r in owned])
                      if rumor["about"] in m.actors and source_root(store, m) == rumor["root"]
                      and source_allowed(ctx, m, teller, hearer)]
        if not candidates:
            return None
        held = next((m for m in candidates if m.text == version["text"]), candidates[0])
        if held.id not in ids:
            sources += (held,)
    op = ("correct" if kind == "correct" else choose_op(
        _traits(ctx, teller), store.get_relationship(hearer, rumor["about"]).affinity))
    rumor, version = dict(rumor), dict(version)
    rumor["canonical"] = clean_memory_text(rumor["canonical"], ctx.ident)
    version["text"] = clean_memory_text(version["text"], ctx.ident)
    return {"rumor": rumor, "version": version, "state": dict(state),
            "sources": sources, "teller": teller, "hearer": hearer,
            "about": rumor["about"], "op": op}


def _shown_refs(shown, enc):
    return tuple(dict.fromkeys([*(f"memory:{m.id}" for m in shown),
                                *dict(enc).get("_shown_refs", ())]))


def _copy_scope(ctx, hearer, evidence, shown, teller, memories):
    # The live writer derives direct refs. Raise its request for a legacy open-scoped
    # memory with a private/secret ancestor; do not flatten those ancestors into evidence.
    states = [_privacy(ctx.store, m) for m in memories]
    if any(not s[0] for s in states):
        return "secret", 0
    secret = any(s[3] or any(scope == "secret" for _, scope, _ in s[1]) for s in states)
    private = any(s[4] or any(scope == "private" for _, scope, _ in s[1]) for s in states)
    scope = "secret" if secret else "private" if private else "open"
    return ctx.writer.scope_of(hearer, evidence, shown=shown, scope=scope,
                               scope_with=teller if scope != "open" else 0)


def _memory(owner, text, *, actors, evidence, now, importance, confidence, valence=0,
            scope=("open", 0), key=None):
    return MemoryRow(id=None, owner=owner, kind="conversation", text=text,
        t_event=int(now), t_created=int(now), t_accessed=int(now), access_n=0,
        actors=tuple(actors), place=None, boss_id=None, raid_id=None, importance=importance,
        valence=valence, source="hearsay", evidence=tuple(evidence), confidence=confidence,
        scope=scope[0], scope_with=scope[1], key=key)


def _check(ctx, teller, text, source, about, *, now):
    ref = f"memory:{source.id}"
    ev = Evidence()
    for item in load_items(ctx.store, [ref], ident=ctx.ident):
        ev.add(item)
    claim = Claim("gossip", teller, text, [ref], about=about)
    verdict = check(claim, ev, load_facts(ctx))
    log_verdicts(ctx.store, ctx.night, "encounters", [(claim, verdict)], now, ctx.ident)
    return verdict.ok


def _shift(store, ctx, hearer, about, amount, reason, refs, now):
    return apply_capped(store, night=ctx.night, a=hearer, b=about, dim="affinity", delta=amount,
                        speed=ctx.settings.relationship_speed, reason=reason, evidence=refs,
                        ts=now, human=ctx.human)


def after_gossip(store, ctx, enc, gossip, shown, *, now):
    """Apply a grounded telling; the caller owns the encounter's atomic commit."""
    enc = dict(enc)
    if not gossip or gossip.get("teller") not in {"a", "b"}:
        return None
    teller = enc[gossip["teller"]]
    hearer = enc["b"] if teller == enc["a"] else enc["a"]
    if teller == ctx.human or hearer == ctx.human:
        return None
    if enc.get("_gossip_teller", teller) != teller:
        return None
    source = next((m for m in shown if m.id == gossip.get("source") and m.owner == teller), None)
    if (source is None or source.id not in enc.get("_gossip_sources", (source.id,))
            or not source_allowed(ctx, source, teller, hearer)):
        return None
    about_text = " ".join(strip_wow_markup(gossip.get("about", "")).split()).casefold()
    about = load_facts(ctx).names.get(about_text)
    if about not in source.actors or about in {enc["a"], enc["b"]}:
        return None
    text = " ".join(strip_wow_markup(gossip.get("text", "")).split())
    if not _check(ctx, teller, text, source, about, now=now):
        return None
    root, ref = source_root(store, source), f"memory:{source.id}"
    evidence = (ref, f"encounter:{enc['id']}")
    shown_refs = _shown_refs(shown, enc)
    scope = _copy_scope(ctx, hearer, evidence, shown_refs, teller, shown)
    existing = store.conn.execute("SELECT * FROM rumor WHERE root=?", (root,)).fetchone()
    corrected = None if existing is None else store.conn.execute(
        "SELECT 1 FROM rumor_state s JOIN rumor_version v ON v.id=s.version AND v.rumor=s.rumor "
        "WHERE s.rumor=? AND s.guid=? AND s.state='stifler' AND v.op='correct'",
        (existing["id"], hearer)).fetchone()
    if corrected and scope != ("open", 0):
        return None  # private context cannot reintroduce a corrected public rumor either
    rumor = None
    if scope == ("open", 0):
        rumor = existing
        if rumor is None:
            if len(live_rumors(store)) >= ctx.settings.live_rumors:
                return None
            canonical = clean_memory_text(source.text, ctx.ident)
            if not _check(ctx, teller, canonical, source, about, now=now):
                return None
            ident = store.conn.execute("INSERT INTO rumor(root,about,canonical,source,created,last_told) "
                "VALUES (?,?,?,?,?,?)", (root, about, canonical, source.id, now, now)).lastrowid
            rumor = store.conn.execute("SELECT * FROM rumor WHERE id=?", (ident,)).fetchone()
        elif rumor["status"] != "live" or rumor["about"] != about:
            return None
        if corrected:
            # A correction leaves residue, but hearing the same rumor again must not
            # resurrect its wrong version or make that old residue correctable twice.
            # The teller discovers an informed hearer and goes quiet as usual.
            store.conn.execute("INSERT INTO rumor_state(rumor,guid,state,tells,updated) "
                "VALUES (?,?,'stifler',1,?) ON CONFLICT(rumor,guid) DO UPDATE SET "
                "state='stifler',tells=rumor_state.tells+1,updated=excluded.updated",
                (rumor["id"], teller, now))
            knowledge.believe(store, teller, hearer, root, "yes", now=now)
            knowledge.believe(store, hearer, teller, root, "yes", now=now)
            return None
    knew = hearer in knowledge.knowers(store, root)
    importance = min(5, source.importance)
    if not knew and knowledge.known_since(store, root, before=now - 86400) >= ctx.settings.last_to_know_min:
        text = truncate_words("Last to hear: " + text, 200)
        importance = min(6, importance + 1)
    confidence = max(0.25, source.confidence * 0.5)
    memory = ctx.writer.put(_memory(hearer, text, actors=(teller, about), evidence=evidence,
        now=now, importance=importance, confidence=confidence, valence=source.valence,
        scope=scope), shown=shown_refs)
    knowledge.learn(store, root, hearer, "told", via=teller, confidence=confidence, ts=now)
    knowledge.believe(store, teller, hearer, root, "yes", now=now)
    knowledge.believe(store, hearer, teller, root, "yes", now=now)
    shift = 0.0
    if about != ctx.human and store.get_relationship(hearer, teller).trust >= ctx.settings.gossip_trust_min:
        sign = (source.valence > 0) - (source.valence < 0)
        shift = _shift(store, ctx, hearer, about, 3 * sign, "gossip", evidence, now)
    if rumor is not None:
        op = choose_op(_traits(ctx, teller), store.get_relationship(hearer, about).affinity - shift)
        version = store.conn.execute("INSERT INTO rumor_version(rumor,teller,hearer,op,text,ts,source) "
            "VALUES (?,?,?,?,?,?,?)", (rumor["id"], teller, hearer, op,
                                       " ".join(strip_wow_markup(gossip["text"]).split()), now, source.id)).lastrowid
        store.conn.execute("UPDATE rumor SET last_told=? WHERE id=?", (now, rumor["id"]))
        previous = store.conn.execute("SELECT * FROM rumor_state WHERE rumor=? AND guid=?",
                                       (rumor["id"], teller)).fetchone()
        tells = (previous["tells"] if previous else 0) + 1
        state = "stifler" if knew or tells >= MAX_TELLS else "spreader"
        store.conn.execute("INSERT INTO rumor_state(rumor,guid,state,tells,version,shift,updated) "
            "VALUES (?,?,?,?,?,?,?) ON CONFLICT(rumor,guid) DO UPDATE SET state=excluded.state, "
            "tells=excluded.tells,version=excluded.version,updated=excluded.updated",
            (rumor["id"], teller, state, tells, version, 0, now))
        store.conn.execute("INSERT INTO rumor_state(rumor,guid,state,tells,version,shift,updated) "
            "VALUES (?,?,'spreader',0,?,?,?) ON CONFLICT(rumor,guid) DO UPDATE SET "
            "state=CASE WHEN rumor_state.tells>=? THEN 'stifler' ELSE 'spreader' END,"
            "version=excluded.version,shift=rumor_state.shift+excluded.shift,updated=excluded.updated",
            (rumor["id"], hearer, version, shift, now, MAX_TELLS))
    return memory


def after_correction(store, ctx, enc, rumor, hearer, *, now):
    """Correct the hearer's matching hearsay once, keeping all scene provenance private."""
    enc = dict(enc)
    ident = rumor if isinstance(rumor, int) else rumor["id"]
    corrector = enc["a"] if hearer == enc["b"] else enc["b"]
    if hearer not in {enc["a"], enc["b"]} or not corrector or hearer == ctx.human:
        return None
    offer = rumor_offer(ctx, {**enc, "a": corrector, "b": hearer,
                              "volition": "correct", "ref": f"rule:rumor:{ident}"})
    if offer is None:
        return None
    rumor, state = offer["rumor"], offer["state"]
    source = next(m for m in offer["sources"] if m.id == rumor["source"])
    canonical = clean_memory_text(rumor["canonical"], ctx.ident)
    # The correction is about the canonical actor, including when she corrects her own
    # story; the regular gossip rule excludes owner==about, so validate as the hearer.
    if not _check(ctx, hearer, canonical, source, rumor["about"], now=now):
        return None
    evidence = (f"encounter:{enc['id']}", rumor["root"])
    shown_refs = tuple(dict.fromkeys([*enc.get("_shown_refs", ()),
                                     *(f"memory:{m.id}" for m in offer["sources"])]))
    ids = [int(ref.split(":", 1)[1]) for ref in shown_refs
           if ref.startswith("memory:") and ref.split(":", 1)[1].isdecimal()]
    shown = store.get_memories(ids)
    if len(shown) != len(set(ids)):
        return None
    scope = _copy_scope(ctx, hearer, evidence, shown_refs, corrector, shown)
    text = truncate_words(f"Turns out {canonical}", 200)
    memory = ctx.writer.put(_memory(hearer, text, actors=(corrector, rumor["about"]),
        evidence=evidence, now=now, importance=4, confidence=0.9, scope=scope,
        key=f"correct:{ident}"), shown=shown_refs)
    candidates = store.conn.execute("SELECT id FROM memory WHERE owner=? AND source='hearsay' "
        "AND status='active' AND id!=?", (hearer, memory)).fetchall()
    for old in store.get_memories([r["id"] for r in candidates]):
        if rumor["about"] in old.actors and rumor["root"] in _privacy(store, old)[2]:
            store.conn.execute("UPDATE memory SET status='superseded',superseded_by=? WHERE id=?",
                               (memory, old.id))
    applied = 0.0 if rumor["about"] == ctx.human else _shift(store, ctx, hearer, rumor["about"],
        -state["shift"] * CORRECTION_RESIDUE, "corrected", evidence, now)
    version = store.conn.execute("INSERT INTO rumor_version(rumor,teller,hearer,op,text,ts,source) "
        "VALUES (?,?,?,'correct',?,?,?)", (ident, corrector, hearer, canonical, now, source.id)).lastrowid
    store.conn.execute("UPDATE rumor_state SET state='stifler',version=?,shift=?,updated=? "
        "WHERE rumor=? AND guid=?", (version, state["shift"] + applied, now, ident, hearer))
    return memory


def fade(store, *, now):
    with store.transaction():
        store.conn.execute("UPDATE rumor SET status='faded' WHERE status='live' AND last_told<=?",
                           (now - FADE_DAYS * 86400,))


def live_rumors(store):
    with store.transaction():
        return store.conn.execute("SELECT * FROM rumor WHERE status='live' ORDER BY id").fetchall()
