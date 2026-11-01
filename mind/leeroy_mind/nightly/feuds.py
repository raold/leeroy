"""Member feuds and remembered sides, inside an accepted encounter's transaction."""
import json
import math

from leeroy_mind.memory.writer import clean_memory_text
from leeroy_mind.nightly import knowledge
from leeroy_mind.nightly.relations import apply_capped
from leeroy_mind.nightly.settings import night_bounds
from leeroy_mind.nightly.storyteller import arc_key
from leeroy_mind.storage.rows import MemoryRow

SIDES_MAX = 3
FEUD_AFFINITY = -30
CLOSE_AFFINITY = 40
OTHER_AFFINITY = 15
DELTA_DIMS = frozenset({"affinity", "trust", "respect", "familiarity"})


def open_feuds(store):
    with store.transaction():
        return store.conn.execute("SELECT * FROM feud WHERE ended IS NULL ORDER BY id").fetchall()


def _accepted(store, ctx, enc):
    """Re-read the accepted pair; private scenes and the human never enter these hooks."""
    row = store.conn.execute("SELECT * FROM encounter WHERE id=?", (enc.get("id"),)).fetchone()
    if row is None or row["private"] or row["status"] != "done" or row["night"] != ctx.night:
        return None
    if any(enc.get(key) != row[key] for key in ("a", "b", "night", "idx", "scene", "status")):
        return None
    a, b = sorted((row["a"], row["b"]))
    members = set(ctx.members())
    if a == b or ctx.human in (a, b) or not {a, b} <= members:
        return None
    if knowledge.is_secret(store, f"encounter:{row['id']}"):
        return None
    return a, b


def _total(deltas):
    total = 0.0
    for delta in deltas:
        if (not isinstance(delta, dict) or delta.get("from") not in {"a", "b"}
                or delta.get("dim") not in DELTA_DIMS):
            return None
        amount = delta.get("delta")
        if isinstance(amount, bool) or not isinstance(amount, (int, float)) or not math.isfinite(amount):
            return None
        total += max(-3, min(3, amount))
    return total


def _shown_open(store, ctx, owner, shown):
    if ctx.writer.scope_of(owner, shown) != ("open", 0):
        return False
    # The live writer reads direct memory scopes. Legacy open wrappers may still
    # contain a private ancestor, so use the scene's bounded complete graph check.
    from leeroy_mind.nightly.encounters import _privacy
    ids = []
    for ref in shown:
        if ref.startswith("memory:"):
            number = ref.removeprefix("memory:")
            if not number.isdecimal():
                return False
            ids.append(int(number))
    memories = store.get_memories(ids)
    if len(memories) != len(ids):
        return False
    for memory in memories:
        complete, scopes, _, secrets, private = _privacy(store, memory)
        if not complete or secrets or private or any(scope != "open" for _, scope, _ in scopes):
            return False
    return not any(knowledge.is_secret(store, root) or knowledge.is_private(store, root)
                   for root in knowledge.roots(store, shown))


def _opened_tonight(store, ctx):
    """Encounter provenance survives a delayed resume beyond the calendar night."""
    count = 0
    start, end = night_bounds(ctx.night, ctx.tz)
    for row in store.conn.execute("SELECT started,evidence FROM feud"):
        refs = json.loads(row["evidence"])
        ids = [int(ref.removeprefix("encounter:")) for ref in refs
               if ref.startswith("encounter:") and ref.removeprefix("encounter:").isdecimal()]
        nights = [found[0] for ident in ids if (found := store.conn.execute(
            "SELECT night FROM encounter WHERE id=?", (ident,)).fetchone()) is not None]
        if ctx.night in nights or (not nights and start <= row["started"] < end):
            count += 1
    return count


def _remember(ctx, owner, text, actors, evidence, now, *, importance, valence,
              scope=("open", 0), shown=()):
    return ctx.writer.put(MemoryRow(id=None, owner=owner, kind="reflection",
        text=clean_memory_text(text, ctx.ident), t_event=int(now), t_created=int(now),
        t_accessed=int(now), access_n=0, actors=tuple(actors), place=None, boss_id=None,
        raid_id=None, importance=importance, valence=valence, source="reflection",
        evidence=tuple(evidence), scope=scope[0], scope_with=scope[1]), shown=shown)


def _makeup_context(store, ctx, enc, owner, other, evidence):
    """A short makeup memory still inherits everything the accepted prompt showed."""
    from leeroy_mind.nightly.encounters import _privacy
    shown = tuple(dict.fromkeys([*enc.get("_shown_refs", ()),
                                *(f"memory:{m.id}" for m in enc.get("_shown", ()))]))
    expanded, requested, complete = list(shown), "open", True
    for ref in shown:
        if ref.startswith("memory:"):
            number = ref.removeprefix("memory:")
            memories = store.get_memories([int(number)]) if number.isdecimal() else []
            if not memories:
                complete = False
                continue
            valid, scopes, _, secrets, private = _privacy(store, memories[0])
            complete = complete and valid
            expanded.extend(f"memory:{ident}" for ident, _, _ in scopes)
            if secrets or any(scope == "secret" for _, scope, _ in scopes):
                requested = "secret"
            elif requested == "open" and (private or any(scope == "private" for _, scope, _ in scopes)):
                requested = "private"
    roots = knowledge.roots(store, shown)
    if any(knowledge.is_secret(store, root) for root in roots):
        requested = "secret"
    elif requested == "open" and any(knowledge.is_private(store, root) for root in roots):
        requested = "private"
    if not complete:
        requested = "secret"
    expanded = tuple(dict.fromkeys(expanded))
    scope = ctx.writer.scope_of(owner, evidence, shown=expanded, scope=requested,
                                scope_with=other if complete and requested != "open" else 0)
    return scope, expanded


def _after_opening(store, feud, enc):
    # Encounters have durable night/index order but no per-row timestamp. Their
    # opening provenance is more reliable than now when a night resumes late.
    for ref in json.loads(feud["evidence"]):
        number = ref.removeprefix("encounter:") if ref.startswith("encounter:") else ""
        if not number.isdecimal():
            continue
        opening = store.conn.execute("SELECT night,idx,id FROM encounter WHERE id=?", (int(number),)).fetchone()
        if opening is not None and (enc["night"], enc["idx"], enc["id"]) <= tuple(opening):
            return False
    return True


def maybe_open(store, ctx, enc, deltas, shown):
    """Open a feud and its sides after the scene's capped relationship updates.

    `deltas` are the scene's normalized delta dictionaries (each bounded to +/-3);
    their sign is the scene's mood, while stored affinities decide eligibility.
    `shown` contains every shown memory ref. No models or independent unit commits.
    """
    enc = dict(enc)
    if enc.get("private"):
        return None
    total = _total(deltas)
    if total is None or total >= 0:
        return None
    shown = tuple(dict.fromkeys(shown))
    with store.transaction():
        pair = _accepted(store, ctx, enc)
        if pair is None:
            return None
        a, b = pair
        root = f"encounter:{enc['id']}"
        if store.conn.execute("SELECT 1 FROM feud WHERE a=? AND b=? AND ended IS NULL", pair).fetchone():
            return None
        # A delayed replay of the old negative scene must not reopen a settled feud.
        if store.conn.execute("SELECT 1 FROM feud f,json_each(f.evidence) e WHERE e.value=?",
                              (root,)).fetchone():
            return None
        if (store.get_relationship(a, b).affinity > FEUD_AFFINITY
                or store.get_relationship(b, a).affinity > FEUD_AFFINITY
                or _opened_tonight(store, ctx) >= ctx.settings.feuds_per_night):
            return None
        now = ctx.clock.now()
        cause = clean_memory_text(enc["scene"], ctx.ident)[:120] if _shown_open(store, ctx, a, shown) else ""
        ident = store.conn.execute("INSERT INTO feud(a,b,started,cause,evidence) VALUES (?,?,?,?,?)",
                                   (a, b, now, cause, json.dumps([root]))).lastrowid
        store.conn.execute("INSERT INTO story_arc(kind,key,tension,opened,last_beat,status) "
            "VALUES ('feud',?,0.5,?,?,'open') ON CONFLICT(key) DO UPDATE SET "
            "tension=0.5,opened=excluded.opened,last_beat=excluded.last_beat,status='open'",
            (arc_key("feud", a, b), now, now))
        knowledge.add_fact(store, kind="feud_cause", root=root, about=a, text=cause, secret=0, now=now)
        members = [g for g in ctx.members() if g not in {a, b, ctx.human}]
        sides = {}
        feud_root = f"feud:{ident}"
        for ally, other in ((a, b), (b, a)):
            eligible = [g for g in members if store.get_relationship(g, ally).affinity >= CLOSE_AFFINITY
                        and store.get_relationship(g, other).affinity < OTHER_AFFINITY]
            eligible.sort(key=lambda g: (-store.get_relationship(g, ally).affinity, g))
            sides[str(ally)] = eligible[:SIDES_MAX]
            for guid in sides[str(ally)]:
                _remember(ctx, guid, f"Sided with {ctx.name(ally)} when {ctx.name(ally)} and {ctx.name(other)} fell out.",
                          (ally, other), (feud_root,), now, importance=5, valence=-0.2)
                apply_capped(store, night=ctx.night, a=guid, b=other, dim="affinity", delta=-2,
                             speed=ctx.settings.relationship_speed, reason="sided", evidence=(feud_root,),
                             ts=now, human=ctx.human)
                knowledge.learn(store, feud_root, guid, "told", via=ally, ts=now)
        store.conn.execute("UPDATE feud SET sides=? WHERE id=?", (json.dumps(sides, sort_keys=True), ident))
        return ident


def maybe_end(store, ctx, enc, deltas):
    """A positive public encounter ends the pair's feud and remembers the makeup."""
    enc = dict(enc)
    if enc.get("private"):
        return None
    total = _total(deltas)
    if total is None or total <= 0:
        return None
    with store.transaction():
        pair = _accepted(store, ctx, enc)
        if pair is None:
            return None
        feud = store.conn.execute("SELECT id,evidence FROM feud WHERE a=? AND b=? AND ended IS NULL", pair).fetchone()
        if feud is None or not _after_opening(store, feud, enc):
            return None
        ident, now = feud["id"], ctx.clock.now()
        store.conn.execute("UPDATE feud SET ended=? WHERE id=?", (now, ident))
        store.conn.execute("UPDATE story_arc SET status='resolved',last_beat=? WHERE key=? AND kind='feud'",
                           (now, arc_key("feud", *pair)))
        evidence = (f"feud:{ident}", f"encounter:{enc['id']}")
        a, b = pair
        for owner, other in ((a, b), (b, a)):
            scope, shown = _makeup_context(store, ctx, enc, owner, other, evidence)
            _remember(ctx, owner, f"Made up with {ctx.name(other)}.", (other,), evidence, now,
                      importance=6, valence=0.5, scope=scope, shown=shown)
        return ident
