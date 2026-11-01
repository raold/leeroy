"""Seeded off-screen scenes, with bounded company and evidence-scoped memories."""
import asyncio
import json
import math
from collections import Counter
from itertools import combinations

from leeroy_mind.nightly import gossip, knowledge
from leeroy_mind.nightly.evidence import Evidence, Item, load_facts, load_items, retrieve
from leeroy_mind.nightly.ground import Claim, Verdict, check, ground, log_verdicts
from leeroy_mind.nightly.llm import schema, system_prompt
from leeroy_mind.nightly.motives import trait_today
from leeroy_mind.nightly.places import PLACES, place_kind
from leeroy_mind.nightly.relations import apply_capped, copresence
from leeroy_mind.personas.screens import has_word, redact_real_name
from leeroy_mind.storage.rows import MemoryRow
from leeroy_mind.talk.prompt import relationship_card
from leeroy_mind.text import strip_wow_markup, truncate_words

STAGE = "encounters"
PAIR_SETTINGS = (
    "officer chat, arguing about the raid",
    "the healers' channel after the raid",
    "voice chat at 1am, the smokers' corner",
)
LATE_SETTINGS = frozenset({"voice chat late at night", PAIR_SETTINGS[2]})
VOLITION_SETTINGS = {"recruit": "planning a mount run", "needle": "comparing meters after the raid"}


def _smoker(p):
    return any(h.substance in {"tobacco", "weed", "alcohol"} for h in p.habits)


def pair_setting(ctx, a, b) -> str | None:
    pa, pb = ctx.persona(a), ctx.persona(b)
    if pa.play.guild_role == pb.play.guild_role == "officer":
        return PAIR_SETTINGS[0]
    if pa.play.raid_role == pb.play.raid_role == "healer":
        return PAIR_SETTINGS[1]
    if _smoker(pa) and _smoker(pb) and (pa.real_life.night_owl or pb.real_life.night_owl):
        return PAIR_SETTINGS[2]
    return None


def layers(ctx, guid) -> dict[int, float]:
    with ctx.store.transaction():
        known = {r["b"]: r["score"] for r in ctx.store.conn.execute(
            "SELECT b,affinity+familiarity AS score FROM relationship WHERE a=?", (guid,))}
        scores = {g: known.get(g, 0) for g in ctx.members() if g != guid}
    ranked = sorted(scores, key=lambda g: (-scores[g], g))
    return {g: 3.0 if i < 5 else 1.0 if i < 20 else 0.3 for i, g in enumerate(ranked)}


def pair_bonus(ctx, a, b) -> float:
    return 0.0


def pair_weight(ctx, a, b, co, *, shared, pair_setting) -> float:
    company = sum(0.7 + 0.06 * trait_today(ctx.store, ctx.night, g,
                                          ctx.persona(g))["extraversion"]
                  for g in (a, b)) / 2
    layer = (layers(ctx, a).get(b, 0) + layers(ctx, b).get(a, 0)) / 2
    return _weight(layer, company, co, shared, pair_setting) + pair_bonus(ctx, a, b)


def _weight(layer, company, co, shared, setting):
    together = 0 if co is None else co.boss_events + co.bgs
    return layer * (1 + min(1, together / 3) + 0.5 * min(2, shared)
                    + (0.5 if setting else 0)) * company


def sample_pairs(weights, n, per_member, rng) -> list[tuple[int, int]]:
    if n <= 0 or per_member <= 0:
        return []
    ranked = [(rng.random() ** (1 / w), pair) for pair, w in sorted(weights.items())
              if pair[0] != pair[1] and math.isfinite(w) and w > 0]
    ranked.sort(key=lambda x: (-x[0], x[1]))
    chosen, used = [], Counter()
    for _, (a, b) in ranked:
        if used[a] < per_member and used[b] < per_member:
            chosen.append((a, b))
            used.update((a, b))
            if len(chosen) >= n:
                break
    return chosen


def _setting(ctx, a, b, rng, volition=None):
    if volition in VOLITION_SETTINGS:
        return VOLITION_SETTINGS[volition]
    paired = pair_setting(ctx, a, b)
    if paired and rng.random() < 0.5:
        return paired
    aa, bb = ctx.persona(a).game.outside_raids, ctx.persona(b).game.outside_raids
    shared = [x for x in aa if x in bb and x in PLACES]
    activity = shared[0] if shared else next((x for x in aa if x in PLACES), "idling")
    return rng.choice(PLACES[activity])


def _rows(ctx):
    with ctx.store.transaction():
        return ctx.store.conn.execute("SELECT * FROM encounter WHERE night=? ORDER BY idx",
                                      (ctx.night,)).fetchall()


async def plan(ctx):
    if ctx.done(STAGE, "plan"):
        return _rows(ctx)
    from leeroy_mind.nightly.storyteller import choose, reset_want, volitions

    def write(store):
        members = ctx.members()
        limit, cap = ctx.settings.encounters_per_night, ctx.settings.encounters_per_member
        used, paired, scenes = Counter(), set(), []
        selected = choose(ctx, volitions(ctx))
        # Only persisted scenes consume wants or remain marked chosen when budgets differ.
        store.conn.execute("UPDATE volition SET chosen=0 WHERE night=?", (ctx.night,))
        for v in selected:
            if len(scenes) >= limit:
                break
            if (v.guid == v.target or v.guid not in members or v.target not in members
                    or used[v.guid] >= cap or used[v.target] >= cap):
                continue
            scenes.append((v.guid, v.target, v.kind, v.ref))
            used.update((v.guid, v.target))
            paired.add(tuple(sorted((v.guid, v.target))))
        co = copresence(store, ctx.window, set(members))
        # Read each member's layers and sampled trait once, not once per possible pair.
        social = {g: layers(ctx, g) for g in members}
        company = {g: 0.7 + 0.06 * trait_today(store, ctx.night, g,
                                              ctx.persona(g))["extraversion"] for g in members}
        weights = {(a, b): _weight((social[a][b] + social[b][a]) / 2,
                    (company[a] + company[b]) / 2, co.get((a, b)),
                    len(set(ctx.persona(a).game.outside_raids)
                        & set(ctx.persona(b).game.outside_raids)), pair_setting(ctx, a, b))
                    + pair_bonus(ctx, a, b)
                   for a, b in combinations(members, 2) if (a, b) not in paired}
        # Rank once without clipping, then apply the capacities left by the volitions.
        for a, b in sample_pairs(weights, len(weights), len(members), ctx.rng("enc", "pairs")):
            if len(scenes) >= limit:
                break
            if used[a] >= cap or used[b] >= cap:
                continue
            scenes.append((a, b, None, ""))
            used.update((a, b))
        for idx, (a, b, kind, ref) in enumerate(scenes):
            setting = _setting(ctx, a, b, ctx.rng("enc", idx), kind)
            store.conn.execute("INSERT INTO encounter(night,idx,a,b,setting,status,volition,ref) "
                               "VALUES (?,?,?,?,?,'planned',?,?)",
                               (ctx.night, idx, a, b, setting, kind, ref))
            if kind:
                store.conn.execute("UPDATE volition SET chosen=1 WHERE night=? AND guid=? "
                                   "AND kind=? AND target=?", (ctx.night, a, kind, b))
                reset_want(store, a, kind, now=ctx.clock.now())
    return _rows(ctx) if ctx.commit(STAGE, "plan", write) else []


ENCOUNTER_SCHEMA = schema("encounter", {
    "scene": {"type": "string", "maxLength": 450},
    "memories": {"type": "array", "minItems": 2, "maxItems": 2, "items": schema("memory", {
        "who": {"type": "string", "enum": ["a", "b"]},
        "text": {"type": "string", "maxLength": 200},
        "importance": {"type": "integer", "minimum": 1, "maximum": 10},
        "valence": {"type": "number"}})},
    "deltas": {"type": "array", "maxItems": 4, "items": schema("delta", {
        "from": {"type": "string", "enum": ["a", "b"]},
        "dim": {"type": "string", "enum": ["affinity", "trust", "respect", "familiarity"]},
        "delta": {"type": "integer"}})},
    "bystanders": {"type": "array", "maxItems": 3, "items": schema("bystander", {
        "who": {"type": "string"}, "noticed": {"type": "string", "maxLength": 120}})},
    "gossip": schema("gossip", {
        "teller": {"type": "string", "enum": ["a", "b"]},
        "about": {"type": "string"}, "source": {"type": "integer"},
        "text": {"type": "string", "maxLength": 200}}) | {"type": ["object", "null"]},
})


def _privacy(store, memory):
    """Inspect the full memory chain; incomplete/oversized graphs fail closed.

    Scope-only secrets have no ledger root to check, so they cannot be shared yet.
    The bounded traversal also prevents a private intermediate memory becoming open.
    """
    pending, seen, scopes, refs = [memory], set(), [], set()
    complete = True
    while pending:
        m = pending.pop()
        if m.id in seen:
            continue
        seen.add(m.id)
        if len(seen) > 200:
            complete = False
            break
        scopes.append((m.id, m.scope, m.scope_with))
        ids = []
        for ref in m.evidence:
            if ref.startswith("memory:"):
                try:
                    ids.append(int(ref.split(":", 1)[1]))
                except ValueError:
                    complete = False
            else:
                refs.add(ref)
        children = store.get_memories(ids)
        if {x.id for x in children} != set(ids):
            complete = False
        pending.extend(x for x in children if x.id not in seen)
    roots = knowledge.roots(store, refs)
    secret = tuple(sorted(r for r in roots if knowledge.is_secret(store, r)))
    private = tuple(sorted(r for r in roots if knowledge.is_private(store, r)))
    return (complete, tuple(sorted(scopes)), tuple(sorted(roots)), secret, private)


def _visible(ctx, memory, other):
    state = _privacy(ctx.store, memory)
    complete, scopes, _, secrets, private = state
    if not complete or memory.status != "active":
        return False, False, state
    secret = bool(secrets) or any(scope == "secret" for _, scope, _ in scopes)
    if secret:
        allowed = (not private and bool(secrets)
                   and knowledge.secret_probe_passed(ctx.store, now=ctx.clock.now(),
                                                     max_rate=ctx.settings.probe_max_rate)
                   and all(other in knowledge.knowers(ctx.store, root) for root in secrets))
        # An unrelated nested scope-only secret must not borrow a known root's permission.
        for ident, scope, _ in scopes:
            if scope != "open":
                own = ctx.store.get_memories([ident])[0]
                own_state = _privacy(ctx.store, own)
                allowed = allowed and bool(own_state[3])
        return bool(allowed), True, state
    return (not private and all(scope == "open" for _, scope, _ in scopes)), False, state


def bystander_candidates(ctx, row, rng, *, shown) -> list[int]:
    with ctx.store.transaction():
        states = [_privacy(ctx.store, m) for m in shown]
        if row["private"] or any(not state[0] or any(scope != "open" for _, scope, _ in state[1])
                                  or state[3] or state[4] for state in states):
            return []
        setting = row["setting"]
        candidates = []
        for g in ctx.members():
            if g in (row["a"], row["b"]):
                continue
            p = ctx.persona(g)
            plausible = (p.play.guild_role == "officer" if setting == PAIR_SETTINGS[0] else
                         p.play.raid_role == "healer" if setting == PAIR_SETTINGS[1] else
                         _smoker(p) if setting == PAIR_SETTINGS[2] else
                         place_kind(setting) in p.game.outside_raids)
            if plausible:
                candidates.append(g)
    return rng.sample(candidates, min(6, len(candidates)))


def _text(text):
    return " ".join(strip_wow_markup(text).split())


def _gossip_text(ctx, text):
    return redact_real_name(_text(text), ctx.ident, ctx.ident.called)


def _gossip_sources(ctx, row, memories, offer):
    """Pick one teller before admitting anything private to a shared scene prompt."""
    a, b = row["a"], row["b"]
    named = row["volition"] in {"gossip", "correct"}
    if named and (offer is None or row["volition"] == "correct"):
        return a, []
    known = set(load_facts(ctx).names.values())
    options = []
    for m in memories:
        if m.owner not in {a, b}:
            continue
        other = b if m.owner == a else a
        about = sorted(g for g in m.actors if g not in {a, b} and g in known)
        if not about or not gossip.source_allowed(ctx, m, m.owner, other):
            continue
        if named and (m.owner != a or gossip.source_root(ctx.store, m) != offer["rumor"]["root"]):
            continue
        traits = trait_today(ctx.store, ctx.night, m.owner, ctx.persona(m.owner))
        score = gossip.tell_score(m.importance, m.confidence, traits)
        options.append((score, m, about, traits))
    options.sort(key=lambda x: (-x[0], x[1].owner != a, x[1].id))
    teller = a if named or not options else options[0][1].owner
    return teller, [entry for entry in options if entry[1].owner == teller]


def _gossip_prompt(ctx, row, ev, teller, options, offer):
    named = row["volition"] in {"gossip", "correct"}
    if named and offer is None:
        return "The intended rumor is unavailable; return gossip null.\n"
    hearer = row["b"] if teller == row["a"] else row["a"]
    lines = []
    if offer is not None:
        rumor, version = offer["rumor"], offer["version"]
        held = version["text"] if version else rumor["canonical"]
        lines.append(f"Rumor {rumor['id']} about {ctx.name(offer['about'])}: "
                     f"{_gossip_text(ctx, held)}")
        if row["volition"] == "correct":
            lines.append(f"Operation: correct. {ctx.name(teller)} explains the canonical account "
                         f"to {ctx.name(hearer)}: {_gossip_text(ctx, rumor['canonical'])}. "
                         "Return gossip null.")
            return "\n".join(lines) + "\n"
    if not options:
        return "\n".join([*lines, "No memory may be passed on; return gossip null."]) + "\n"
    key = "a" if teller == row["a"] else "b"
    lines.append(f"Only {ctx.name(teller)} (teller {key}) may pass on one of these memories. "
                 "If one of them passes on something from their memories, retell it in one "
                 "sentence by the operation below. Use the source's bracketed evidence alias, "
                 "the named person as about, and return gossip null if nothing is passed on.")
    for _, m, abouts, traits in options:
        for about in abouts:
            if offer is not None and about != offer["about"]:
                continue
            op = gossip.choose_op(traits, ctx.store.get_relationship(hearer, about).affinity)
            words = gossip.OP_WORDS[op].format(hearer=ctx.name(hearer), about=ctx.name(about))
            lines.append(f"Source [{ev.alias(f'memory:{m.id}')}], about {ctx.name(about)}: "
                         f"operation {op}, {words}.")
    return "\n".join(lines) + "\n"


def _title(ctx, guid):
    row = ctx.store.conn.execute("SELECT title FROM life_thread WHERE guid=? AND status='active' "
                                 "ORDER BY id DESC LIMIT 1", (guid,)).fetchone()
    return row["title"] if row else ctx.persona(guid).real_life.life_thread


def _memory(owner, text, *, other, evidence, importance, valence, now, embedding,
            kind="conversation", scope="open"):
    return MemoryRow(id=None, owner=owner, kind=kind, text=text, t_event=int(now),
        t_created=int(now), t_accessed=int(now), access_n=0, actors=tuple(other), place=None,
        boss_id=None, raid_id=None, importance=importance, valence=valence, source="offscreen",
        evidence=tuple(evidence), embedding=embedding, scope=scope,
        scope_with=other[0] if scope != "open" and len(other) == 1 else 0)


async def run_encounter(ctx, row):
    unit = f"enc:{row['idx']}"
    if ctx.done(STAGE, unit) or row["status"] != "planned" or row["night"] != ctx.night:
        return
    a, b = row["a"], row["b"]
    if a == b or a not in ctx.members() or b not in ctx.members():
        return
    from leeroy_mind.nightly.storyteller import BLOWUP_DELTA, INTENT, arc_key

    found = await asyncio.gather(retrieve(ctx, a, ctx.name(b), 3),
                                 retrieve(ctx, b, ctx.name(a), 3))
    shown, privacy, marked, admission = [], {}, set(), {}
    with ctx.store.transaction():
        # Either retrieval can await while the other finishes. Re-read both together before
        # filtering, so stale scope/text never enters the scene prompt in the first place.
        current_memories = {m.id: m for m in ctx.store.get_memories(
            [m.id for memories in found for m in memories if m.id is not None])}
        offer = gossip.rumor_offer(ctx, row)
        forced = [] if offer is None else offer["sources"]
        available = {m.id: m for m in [*forced, *current_memories.values()]}
        teller, options = _gossip_sources(ctx, row, available.values(), offer)
        offered = {entry[1].id for entry in options}
        # Named rumors carry both canonical and held-version provenance, even when ordinary
        # retrieval did not find it. Their helper separately checks disclosure permission.
        for m in forced:
            if m.id not in privacy:
                shown.append(m)
                privacy[m.id] = _privacy(ctx.store, m)
                admission[m.id] = "rumor"
        for owner, other, memories in ((a, b, found[0]), (b, a, found[1])):
            for retrieved_memory in memories:
                m = current_memories.get(retrieved_memory.id)
                if m is None or m.owner != owner:
                    continue
                visible, secret, state = _visible(ctx, m, other)
                private_offer = m.owner == teller and m.id in offered
                if (visible or private_offer) and m.id not in privacy:
                    shown.append(m)
                    privacy[m.id] = state
                    admission[m.id] = "context" if visible else "gossip"
                    if secret:
                        marked.add(f"memory:{m.id}")
        candidates = bystander_candidates(ctx, row, ctx.rng("enc", row["idx"], "around"), shown=shown)
        root = f"encounter:{row['id']}"
        shown_refs = [f"memory:{m.id}" for m in shown]
        refs = [root, *shown_refs]
        ev = Evidence()
        ev.add(Item(root, row["setting"], frozenset({a, b}), ctx.clock.now()))
        for item in load_items(ctx.store, shown_refs, ident=ctx.ident):
            if item.ref in marked:
                item = Item(item.ref, "(a secret you share: never passed on) " + item.text,
                            item.actors, item.t, item.importance, item.valence, item.knowers)
            ev.add(item)
        facts = load_facts(ctx)
        relationships = "\n".join(f"{ctx.name(g)} toward " + relationship_card(ctx.name(other),
            ctx.store.get_relationship(g, other), is_human=False) for g, other in ((a, b), (b, a)))
        titles = f"{ctx.name(a)}: {_title(ctx, a)}\n{ctx.name(b)}: {_title(ctx, b)}"
        gossip_prompt = _gossip_prompt(ctx, row, ev, teller, options, offer)
    system = system_prompt("encounter", "Write a small off-screen scene between two guildmates; "
        "no romance or flirting between them.\n{cards}", ctx.settings.data_dir, human=ctx.ident,
        cards=f"{ctx.card(a)}\n{ctx.card(b)}")
    user = (f"a: {ctx.name(a)}; b: {ctx.name(b)}\nSetting: {row['setting']}\n{relationships}\n"
            f"Life threads:\n{titles}\nEvidence:\n{ev.render(ctx.tz)}\n")
    if row["volition"] in INTENT and (row["volition"] not in {"gossip", "correct"} or offer):
        user += INTENT[row["volition"]].format(a=ctx.name(a), b=ctx.name(b)) + "\n"
    user += gossip_prompt
    if candidates:
        user += "People who might be around: " + ", ".join(ctx.name(g) for g in candidates) + ".\n"
    else:
        user += "Nobody else is around; return no bystanders.\n"
    user += ("Write what happened in at most 3 sentences, not a dialogue. Name at most three "
             "of the people around who noticed, and what they noticed. Give exactly one memory "
             "for a and one for b.")
    reply = await ctx.reasoner.json(system, user, ENCOUNTER_SCHEMA, temperature=0.8)
    memories = {m["who"]: m for m in reply["memories"]}
    scene = Claim("scene", a, _text(reply["scene"]), refs)
    if set(memories) != {"a", "b"}:
        pairs = [(scene, Verdict(False, "deterministic", "one memory per participant required"))]
        claims = []
    else:
        claims = [Claim("scene", g, _text(memories[key]["text"]), refs,
                        valence=max(-1, min(1, memories[key]["valence"])))
                  for key, g in (("a", a), ("b", b))]
        pairs = await ground(ctx, [scene, *claims], ev, facts)
    accepted = bool(claims) and all(verdict.ok for _, verdict in pairs)
    telling = None
    if reply.get("gossip") is not None and row["volition"] != "correct":
        payload = reply["gossip"]
        source = ev.ref(payload["source"])
        # Only model-facing aliases are accepted; a coincident database ID is never a fallback.
        source_id = int(source.removeprefix("memory:")) if source.startswith("memory:") else None
        if source_id not in offered or payload["teller"] != ("a" if teller == a else "b"):
            pairs.append((Claim("gossip", teller, _gossip_text(ctx, payload["text"]), [source]),
                          Verdict(False, "deterministic", "gossip source not offered")))
        else:
            telling = {**payload, "source": source_id,
                       "about": _gossip_text(ctx, payload["about"]),
                       "text": _gossip_text(ctx, payload["text"])}
    around, seen = [], set()
    names = {ctx.name(g).casefold(): g for g in candidates}
    if accepted:
        for observer in reply["bystanders"]:
            g = names.get(_text(observer["who"]).casefold())
            claim = Claim("bystander", g or a, _text(observer["noticed"]), [root])
            if g is None:
                pairs.append((claim, Verdict(False, "deterministic", "bystander not around")))
                continue
            if g in seen:
                continue
            seen.add(g)
            local = Evidence()
            local.add(Item(root, row["setting"], frozenset({a, b, g}), ctx.clock.now()))
            verdict = check(claim, local, facts)
            # Ground.check permits the human generally; an observer may only name this trio.
            if any(guid not in {a, b, g} and has_word(claim.text, name)
                   for name, guid in facts.names.items()):
                verdict = Verdict(False, "deterministic", "bystander name not in scene")
            pairs.append((claim, verdict))
            if verdict.ok:
                text = truncate_words(f"Noticed {ctx.name(a)} and {ctx.name(b)}: {claim.text}", 200)
                around.append((g, claim, text))
    texts = [claim.text for claim in claims] + [text for _, _, text in around] if accepted else []
    vectors = await ctx.embed(texts) if texts else []
    if len(vectors) != len(texts):
        raise ValueError("Encounter received an incomplete embedding batch")

    def write(store):
        current = store.conn.execute("SELECT * FROM encounter WHERE id=?", (row["id"],)).fetchone()
        if current is None or dict(current) != dict(row):
            raise ValueError("Encounter changed while its scene was generated")
        if a not in ctx.members() or b not in ctx.members():
            raise ValueError("Encounter participant became unavailable")
        if gossip.rumor_offer(ctx, row) != offer:
            raise ValueError("Encounter rumor changed while its scene was generated")
        fresh = {m.id: m for m in store.get_memories([m.id for m in shown])}
        for m in shown:
            if fresh.get(m.id) != m:
                raise ValueError("Encounter memory changed while its scene was generated")
            visible, _, state = _visible(ctx, m, b if m.owner == a else a)
            if admission[m.id] == "rumor":
                visible = offer is not None  # The fresh offer above rechecks its permissions.
            elif admission[m.id] == "gossip":
                visible = gossip.source_allowed(ctx, m, teller, b if teller == a else a)
            if not visible or state != privacy[m.id]:
                raise ValueError("Encounter privacy changed while its scene was generated")
        now = ctx.clock.now()
        log_verdicts(store, ctx.night, STAGE, pairs, now, ctx.ident)
        if not accepted:
            store.conn.execute("UPDATE encounter SET status='rejected' WHERE id=?", (row["id"],))
            return
        eligible = bystander_candidates(ctx, current, ctx.rng("enc", row["idx"], "around"), shown=shown)
        if any(g not in eligible for g, _, _ in around):
            raise ValueError("Encounter bystander became unavailable")
        scope = "secret" if row["private"] or marked or knowledge.secret_rooted(store, shown_refs) else "open"
        if scope == "open" and any(state[4] or any(s != "open" for _, s, _ in state[1])
                                   for state in privacy.values()):
            # Legacy rows can say open even though a deeper memory cites a whisper.
            # The writer derives direct refs, so explicitly preserve the graph's restriction.
            scope = "private"
        for i, (key, g, other) in enumerate((("a", a, b), ("b", b, a))):
            claim, data = claims[i], memories[key]
            ident = ctx.writer.put(_memory(g, claim.text, other=(other,), evidence=refs,
                importance=min(6, data["importance"]), valence=claim.valence, now=now,
                embedding=vectors[i], scope=scope), shown=shown_refs)
            if abs(claim.valence) >= 0.4:
                store.conn.execute("INSERT INTO emotion(guid,night,type,target,intensity,why,source,evidence,ts) "
                    "VALUES (?,?,?,?,?,?,'encounter',?,?)", (g, ctx.night,
                    "admiration" if claim.valence > 0 else "reproach", other,
                    2 if abs(claim.valence) >= 0.7 else 1, claim.text[:80],
                    json.dumps([f"memory:{ident}"]), now))
        total = 0
        for delta in reply["deltas"]:
            g, other = (a, b) if delta["from"] == "a" else (b, a)
            amount = max(-3, min(3, delta["delta"]))
            total += amount
            apply_capped(store, night=ctx.night, a=g, b=other, dim=delta["dim"], delta=amount,
                         speed=ctx.settings.relationship_speed, reason="encounter", evidence=refs,
                         ts=now, human=ctx.human)
        store.conn.execute("UPDATE encounter SET status='done',scene=?,blowup=? WHERE id=?",
            (scene.text, int(row["volition"] == "confront" and total <= BLOWUP_DELTA), row["id"]))
        if not row["private"]:
            knowledge.learn(store, root, a, "witnessed", ts=now)
            knowledge.learn(store, root, b, "witnessed", ts=now)
            knowledge.believe(store, a, b, root, "yes", now=now)
            knowledge.believe(store, b, a, root, "yes", now=now)
        for i, (g, claim, text) in enumerate(around, 2):
            knowledge.learn(store, root, g, "overheard", ts=now)
            knowledge.believe(store, a, g, root, "no", now=now)
            knowledge.believe(store, b, g, root, "no", now=now)
            knowledge.add_fact(store, kind="noticed", root=root, about=a, text=claim.text,
                               secret=0, holder=g, now=now)
            ctx.writer.put(_memory(g, text, other=(a, b), evidence=(root,), importance=3,
                                   valence=0, now=now, embedding=vectors[i], kind="observation"))
        if row["volition"] in {"confront", "make_up"}:
            store.conn.execute("UPDATE story_arc SET last_beat=? WHERE key=? AND status='open'",
                               (now, arc_key("feud", a, b)))
        gossip_enc = dict(row) | {"_gossip_teller": teller, "_gossip_sources": tuple(sorted(offered)),
                                  "_shown_refs": tuple(shown_refs), "_shown": tuple(shown)}
        if row["volition"] == "correct" and offer is not None:
            gossip.after_correction(store, ctx, gossip_enc, offer["rumor"]["id"], b, now=now)
        elif telling is not None:
            gossip.after_gossip(store, ctx, gossip_enc, telling, shown, now=now)
    ctx.commit(STAGE, unit, write)


async def stage_encounters(ctx):
    if not ctx.done(STAGE, "fade") and not ctx.commit(
            STAGE, "fade", lambda store: gossip.fade(store, now=ctx.clock.now())):
        return
    rows = {f"enc:{row['idx']}": row for row in await plan(ctx) if row["status"] == "planned"}
    await ctx.map_units(STAGE, list(rows), lambda unit: run_encounter(ctx, rows[unit]))
